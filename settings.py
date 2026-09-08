# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle —— 分群独立配置

全局配置（_conf_schema.json）提供默认值；每个群的设置独立存 SQLite（JSON blob），
群级设置里的 None / 缺失键表示"跟随全局默认"。
"""

import copy
import json
import time
from typing import Any, Optional

# 模式
MODE_EQUAL = "equal"
MODE_ACTIVITY = "activity"
MODE_SIGNUP = "signup"
MODE_LABELS = {MODE_EQUAL: "全员等权", MODE_ACTIVITY: "活跃度加权", MODE_SIGNUP: "报名参与"}

# 定时类型
SCHED_NONE = "none"
SCHED_DAILY = "daily"
SCHED_WEEKLY = "weekly"
SCHED_BIWEEKLY = "biweekly"
SCHED_MONTHLY = "monthly"
SCHED_CUSTOM = "custom"  # cron

WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 群级默认（不含任何"全局"概念；main 层负责把全局默认灌进来）
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": None,              # None=跟随全局 default_mode
    "prizes": None,            # None=默认单等奖：[{"name":"幸运奖","count":1}]
    "cooldown_days": 7,
    "cooldown_cross_group": False,   # True=与所有群共享防连中冷却（跨群连续中奖保护）
    "exclude_admins": False,
    "exclude_recent": True,
    "at_winners": None,        # None=跟随全局 at_enabled
    "card_enabled": None,      # None=跟随全局 card_enabled
    "template": None,          # None=跟随全局 default_template
    "activity_window_days": 7,
    "activity_weight_base": 1.0,
    "activity_weight_k": 1.0,
    "activity_min_count": 0,
    "schedule": {
        "type": SCHED_NONE,
        "time": "20:00",       # HH:MM
        "weekday": 0,          # weekly: 0-6（周一=0）
        "monthday": 1,         # monthly: 1-28
        "cron": "",            # custom: 5 段 cron（分 时 日 月 周）
    },
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def default_prizes() -> list[dict]:
    return [{"name": "幸运奖", "count": 1}]


def normalize_prizes(raw: Any) -> list[dict]:
    """把各种输入规整成 [{'name': str, 'count': int}, ...]。非法输入抛 ValueError。"""
    if raw is None:
        return default_prizes()
    if isinstance(raw, str):
        # 形如  一等奖:1,二等奖:2
        out = []
        for part in raw.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                name, cnt = part.split(":", 1)
            elif "：" in part:
                name, cnt = part.split("：", 1)
            else:
                raise ValueError(f"等次格式错误：{part}（应为 名称:人数，用逗号分隔）")
            name = name.strip()
            cnt = cnt.strip()
            if not name:
                raise ValueError("等次名称不能为空")
            try:
                n = int(cnt)
            except ValueError:
                raise ValueError(f"等次人数必须是整数：{cnt}")
            if n <= 0:
                raise ValueError(f"等次人数必须大于 0：{cnt}")
            out.append({"name": name, "count": n})
        if not out:
            raise ValueError("等次不能为空")
        return out
    if isinstance(raw, list):
        out = []
        for i, item in enumerate(raw):
            if isinstance(item, dict) and "name" in item and "count" in item:
                n = int(item["count"])
                if n <= 0:
                    raise ValueError(f"第 {i+1} 个等次人数必须大于 0")
                out.append({"name": str(item["name"]), "count": n})
            else:
                raise ValueError(f"第 {i+1} 个等次格式错误")
        if not out:
            raise ValueError("等次不能为空")
        return out
    raise ValueError("等次格式无法识别")


def validate_time(hhmm: str) -> str:
    try:
        h, m = hhmm.strip().split(":")
        h, m = int(h), int(m)
        assert 0 <= h <= 23 and 0 <= m <= 59
        return f"{h:02d}:{m:02d}"
    except Exception:
        raise ValueError(f"时间格式错误：{hhmm}（应为 HH:MM，如 20:00）")


class GroupSettings:
    """单个群的有效设置（全局默认 + 群级覆盖已合并）。"""

    def __init__(self, umo: str, store: "SettingsStore"):
        self.umo = umo
        self._store = store
        self._reload()

    def _reload(self):
        row = self._store.db.get_settings_row(self.umo)
        self.enabled = True
        self._raw: dict = {}
        if row:
            self.enabled = bool(row["enabled"])
            try:
                self._raw = json.loads(row["config_json"] or "{}")
            except Exception:
                self._raw = {}
        merged = _deep_merge(DEFAULTS, self._raw)
        self._merged = merged
        self.mode = merged["mode"] or self._store.global_default_mode
        self.prizes = normalize_prizes(merged["prizes"])
        self.cooldown_days = int(merged["cooldown_days"] or 0)
        self.exclude_admins = bool(merged["exclude_admins"])
        self.exclude_recent = bool(merged["exclude_recent"])
        self.cooldown_cross_group = bool(merged["cooldown_cross_group"])
        self.at_winners = (
            merged["at_winners"] if merged["at_winners"] is not None
            else self._store.global_at_enabled
        )
        self.card_enabled = (
            merged["card_enabled"] if merged["card_enabled"] is not None
            else self._store.global_card_enabled
        )
        self.template = merged["template"] or self._store.global_template
        self.activity_window_days = max(1, int(merged["activity_window_days"] or 7))
        self.activity_weight_base = float(merged["activity_weight_base"] or 1.0)
        self.activity_weight_k = float(merged["activity_weight_k"] or 1.0)
        self.activity_min_count = int(merged["activity_min_count"] or 0)
        self.schedule = merged["schedule"] or {"type": SCHED_NONE}

    # ---- 群级覆盖写入（只存非 None 覆盖；None 表示回退默认）----

    def update(self, patch: dict):
        new_raw = _deep_merge(self._raw, patch)
        # 写前校验（失败抛 ValueError，不落库）
        if "prizes" in patch and patch["prizes"] is not None:
            normalize_prizes(patch["prizes"])
        sched = new_raw.get("schedule")
        if sched and sched.get("type") in (
            SCHED_DAILY, SCHED_WEEKLY, SCHED_BIWEEKLY, SCHED_MONTHLY
        ):
            validate_time(sched.get("time", "20:00"))
        self._store.db.upsert_settings(
            self.umo, self.enabled, json.dumps(new_raw, ensure_ascii=False), int(time.time())
        )
        self._reload()

    def set_enabled(self, on: bool):
        self._store.db.upsert_settings(
            self.umo, on, json.dumps(self._raw, ensure_ascii=False), int(time.time())
        )
        self._reload()

    # ---- 状态摘要 ----

    def describe_schedule(self) -> str:
        s = self.schedule or {}
        t = s.get("type", SCHED_NONE)
        if t == SCHED_NONE:
            return "未开启"
        tm = s.get("time", "20:00")
        if t == SCHED_DAILY:
            return f"每日 {tm}"
        if t == SCHED_WEEKLY:
            return f"每周{WEEK_CN[int(s.get('weekday', 0))]} {tm}"
        if t == SCHED_BIWEEKLY:
            return f"每两周（周{WEEK_CN[int(s.get('weekday', 0))]}）{tm}"
        if t == SCHED_MONTHLY:
            return f"每月 {int(s.get('monthday', 1))} 日 {tm}"
        if t == SCHED_CUSTOM:
            return f"自定义 cron：{s.get('cron', '')}"
        return "未开启"

    def describe(self) -> str:
        prize_txt = "、".join(f"{p['name']}×{p['count']}" for p in self.prizes)
        return (
            f"状态：{'启用' if self.enabled else '停用'}\n"
            f"参与模式：{MODE_LABELS.get(self.mode, self.mode)}\n"
            f"中奖等次：{prize_txt}\n"
            f"防连中冷却：{self.cooldown_days} 天"
            f"{'（跨群共享）' if self.cooldown_cross_group else ''}"
            f"{'（排除近期中奖者）' if self.exclude_recent else ''}\n"
            f"排除管理员：{'是' if self.exclude_admins else '否'}\n"
            f"@中奖者：{'是' if self.at_winners else '否'}\n"
            f"卡片渲染：{'是' if self.card_enabled else '否'}\n"
            f"活跃度窗口：{self.activity_window_days} 天"
            f"（最低 {self.activity_min_count} 条）\n"
            f"定时开奖：{self.describe_schedule()}\n"
            f"模板：{self.template}"
        )


class SettingsStore:
    def __init__(self, db, global_config: dict):
        self.db = db
        self.global_default_mode = global_config.get("default_mode", MODE_EQUAL)
        self.global_at_enabled = bool(global_config.get("at_enabled", True))
        self.global_card_enabled = bool(global_config.get("card_enabled", False))
        self.global_template = global_config.get(
            "default_template",
            "🎉 恭喜 <winners> 中奖！请尽快联系群主/管理员领取奖品。",
        )

    def get(self, umo: str) -> GroupSettings:
        return GroupSettings(umo, self)
