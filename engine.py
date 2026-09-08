# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle —— 抽奖引擎

参与模式：
  equal    全员等权：候选 = 本群观测到的成员（冷启动无成员时报错提示）
  activity 活跃度加权：候选 = 活跃窗口内达最低条数的成员，权重 = base + k*sqrt(消息数)；
                      若窗口内无人达标，回落到等权抽全体观测成员
  signup   报名参与：候选 = 当前场次报名成员
支持多等次、多名额、冷却排除。
"""

import random
import time
from dataclasses import dataclass, field

try:
    from .settings import MODE_ACTIVITY, MODE_EQUAL, MODE_SIGNUP
    from .activity import activity_weight
except ImportError:  # 平铺加载
    from settings import MODE_ACTIVITY, MODE_EQUAL, MODE_SIGNUP  # type: ignore
    from activity import activity_weight  # type: ignore


@dataclass
class TierResult:
    prize: str
    winners: list[tuple[str, str]] = field(default_factory=list)  # [(uid, name)]
    shortage: int = 0  # 候选不足时缺的名额


@dataclass
class DrawResult:
    mode: str
    tiers: list[TierResult]
    pool_size: int
    excluded_cool: list[str]
    notes: list[str]
    signup_sid: int | None = None


class DrawError(Exception):
    pass


def _weighted_sample(
    candidates: list[tuple[str, str, float]], k: int, rng: random.Random
) -> list[tuple[str, str]]:
    """不放回加权抽样。candidates: [(uid, name, weight), ...]"""
    pool = list(candidates)
    picked: list[tuple[str, str]] = []
    for _ in range(k):
        total = sum(w for _, _, w in pool)
        if total <= 0 or not pool:
            break
        target = rng.random() * total
        acc = 0.0
        idx = len(pool) - 1
        for i, (_, _, w) in enumerate(pool):
            acc += w
            if acc >= target:
                idx = i
                break
        uid, name, _ = pool.pop(idx)
        picked.append((uid, name))
    return picked


def draw(
    *,
    mode: str,
    prizes: list[dict],
    candidates: list[tuple[str, str]],
    weights: dict[str, float] | None = None,
    cooldown_uids: set[str] | None = None,
    exclude_admins: bool = False,
    admin_uids: set[str] | None = None,
    rng: random.Random | None = None,
) -> DrawResult:
    """执行抽奖。candidates: [(uid, name), ...]；weights: uid -> 权重。"""
    rng = rng or random.SystemRandom()
    cooldown_uids = cooldown_uids or set()
    admin_uids = admin_uids or set()
    notes: list[str] = []
    excluded_cool: list[str] = []

    if not candidates:
        raise DrawError(
            "候选池为空：插件还没有观测到本群成员（等权/加权模式需要先有人发言），"
            "或当前场次还没有人报名（报名模式可先 抽奖 报名）。"
        )

    # 去重（按 uid 保留首次出现的昵称）
    seen: dict[str, str] = {}
    for uid, name in candidates:
        seen.setdefault(str(uid), name or str(uid))

    pool: list[tuple[str, str, float]] = []
    skipped_pool: list[str] = []
    for uid, name in seen.items():
        if uid in cooldown_uids:
            excluded_cool.append(name)
            continue
        if exclude_admins and uid in admin_uids:
            skipped_pool.append(name)
            continue
        w = float((weights or {}).get(uid, 1.0)) if mode == MODE_ACTIVITY else 1.0
        if w <= 0:
            w = 1e-6
        pool.append((uid, name, w))

    if exclude_admins and skipped_pool:
        notes.append(f"已排除管理员 {len(skipped_pool)} 人")
    if excluded_cool:
        notes.append(f"冷却期内跳过 {len(excluded_cool)} 人：{'、'.join(excluded_cool[:10])}")

    tiers: list[TierResult] = []
    for prize in prizes:
        pname = prize["name"]
        count = int(prize["count"])
        picked = _weighted_sample(pool, count, rng)
        picked_uids = {u for u, _ in picked}
        # 已中奖者移出候选池（同一人不同等次不重复中奖）
        pool = [c for c in pool if c[0] not in picked_uids]
        tiers.append(
            TierResult(prize=pname, winners=picked, shortage=max(0, count - len(picked)))
        )

    total_quota = sum(int(p["count"]) for p in prizes)
    if len(seen) - len(excluded_cool) - len(skipped_pool if exclude_admins else []) < total_quota:
        notes.append("候选人数少于总名额，部分等次未抽满（见各等次缺额）。")

    return DrawResult(
        mode=mode,
        tiers=tiers,
        pool_size=len(pool) + sum(len(t.winners) for t in tiers),
        excluded_cool=excluded_cool,
        notes=notes,
    )


def gather_candidates(
    *,
    mode: str,
    settings,
    db,
    activity,
    signup_sid: int | None,
):
    """根据模式收集候选与权重，返回 (candidates, weights, note)。"""
    umo = settings.umo
    if mode == MODE_SIGNUP:
        if signup_sid is None:
            raise DrawError("内部错误：报名模式缺少场次号")
        members = db.list_signups(umo, signup_sid)
        if not members:
            raise DrawError("当前场次还没有人报名，可发送「抽奖 报名」参与。")
        return members, None, f"报名 {len(members)} 人"

    observed = db.list_users(umo)

    if mode == MODE_ACTIVITY:
        counts = activity.counts(umo, settings.activity_window_days)
        name_map = dict(observed)
        qualified = [
            (uid, name_map.get(uid, uid))
            for uid, c in counts.items()
            if c >= settings.activity_min_count
        ]
        if qualified:
            weights = {
                uid: activity_weight(
                    counts[uid],
                    settings.activity_weight_base,
                    settings.activity_weight_k,
                )
                for uid, _ in qualified
            }
            return qualified, weights, (
                f"近 {settings.activity_window_days} 天活跃 {len(qualified)} 人"
            )
        # 回落等权
        if not observed:
            raise DrawError(
                "活跃窗口内暂无达到最低发言条数的成员，且插件还未观测到本群任何成员。"
            )
        return observed, None, "活跃窗口内无人达标，已回落到全员等权"

    # equal
    if not observed:
        raise DrawError(
            "插件还没有观测到本群成员：等权模式从发言过的成员中抽取，"
            "请先在群内聊几句，或改用报名模式（抽奖 模式 报名）。"
        )
    return observed, None, f"观测成员 {len(observed)} 人"
