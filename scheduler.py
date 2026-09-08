# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle —— 定时调度

- AsyncIOScheduler，任务按群持久化在 group_settings 中，重启后自动重建。
- 支持：每日 / 每周 / 每两周 / 每月 / 自定义 cron。
- 错过的任务不补发（misfire grace 1 小时，coalesce）。
"""

from typing import Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

try:
    from .activity import TZ, now_local
    from .settings import (
        SCHED_BIWEEKLY,
        SCHED_CUSTOM,
        SCHED_DAILY,
        SCHED_MONTHLY,
        SCHED_NONE,
        SCHED_WEEKLY,
    )
except ImportError:  # 平铺加载
    from activity import TZ, now_local  # type: ignore
    from settings import (  # type: ignore
        SCHED_BIWEEKLY,
        SCHED_CUSTOM,
        SCHED_DAILY,
        SCHED_MONTHLY,
        SCHED_NONE,
        SCHED_WEEKLY,
    )

_JOB_PREFIX = "gr_raffle_"


def _job_id(umo: str) -> str:
    safe = umo.replace(":", "_").replace(" ", "_")
    return f"{_JOB_PREFIX}{safe}"


def _parse_hhmm(t: str) -> tuple[int, int]:
    h, m = t.strip().split(":")
    return int(h), int(m)


def build_trigger(schedule: dict):
    """根据群 schedule 配置构造 APScheduler trigger；type=none 返回 None。

    可能抛 ValueError（cron 非法 / 时间非法）。
    """
    t = schedule.get("type", SCHED_NONE)
    if t == SCHED_NONE:
        return None
    hour, minute = _parse_hhmm(schedule.get("time", "20:00"))
    if t == SCHED_DAILY:
        return CronTrigger(hour=hour, minute=minute, timezone=TZ)
    if t == SCHED_WEEKLY:
        return CronTrigger(day_of_week=int(schedule.get("weekday", 0)),
                           hour=hour, minute=minute, timezone=TZ)
    if t == SCHED_BIWEEKLY:
        # 每周该星期触发，再用"年内周序号为偶数"隔周跳过，效果=每两周
        wd = int(schedule.get("weekday", 0))  # Monday=0
        return CronTrigger(
            day_of_week=wd, hour=hour, minute=minute, timezone=TZ,
        )
    if t == SCHED_MONTHLY:
        return CronTrigger(day=int(schedule.get("monthday", 1)),
                           hour=hour, minute=minute, timezone=TZ)
    if t == SCHED_CUSTOM:
        cron = (schedule.get("cron") or "").strip()
        if not cron:
            raise ValueError("自定义 cron 为空")
        fields = cron.split()
        if len(fields) != 5:
            raise ValueError("cron 必须是 5 段：分 时 日 月 周")
        minute, hour, day, month, dow = fields
        # 标准 crontab 周字段：0/7=周日 … 6=周六；APScheduler 用 mon=0..sun=6，
        # 这里做一次换算，保证用户按通用 crontab 习惯填写
        def _conv_dow(token: str) -> str:
            if token == "*":
                return token
            out = []
            for part in token.split(","):
                step = None
                base = part
                if "/" in part:
                    base, step = part.split("/", 1)
                if base in ("*", ""):
                    mapped = "*"
                elif "-" in base:
                    a, b = base.split("-", 1)
                    mapped = f"{_map_one(a)}-{_map_one(b)}"
                else:
                    mapped = _map_one(base)
                out.append(f"{mapped}/{step}" if step is not None else mapped)
            return ",".join(out)

        def _map_one(v: str) -> str:
            if v in ("7", "0"):
                return "sun"
            names = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
            return names[int(v)] if v.isdigit() and 0 <= int(v) <= 7 else v

        return CronTrigger(
            minute=minute, hour=hour, day=day, month=month,
            day_of_week=_conv_dow(dow), timezone=TZ,
        )
    raise ValueError(f"未知的定时类型：{t}")


def preview_next(schedule: dict, n: int = 3) -> list[str]:
    """预览未来 n 次触发时间（字符串）。非法配置抛 ValueError。"""
    t = schedule.get("type", SCHED_NONE)
    trig = build_trigger(schedule)
    if trig is None:
        return []
    now = now_local()
    out = []
    prev_fire = None
    anchor_week = None
    need = n * (2 if t == SCHED_BIWEEKLY else 1) + 2  # 双周需多拉取
    for _ in range(need):
        nxt = trig.get_next_fire_time(prev_fire, prev_fire or now)
        if nxt is None:
            break
        try:
            nxt = nxt.astimezone(TZ)
        except Exception:
            pass
        if t == SCHED_BIWEEKLY:
            wk = int(nxt.isocalendar()[1])
            if anchor_week is None:
                anchor_week = wk
            if (wk - anchor_week) % 2 != 0:
                prev_fire = nxt
                continue
        out.append(nxt.strftime("%Y-%m-%d %H:%M (%a)"))
        if len(out) >= n:
            break
        prev_fire = nxt
    return out


class RaffleScheduler:
    def __init__(self, draw_callback: Callable[[str], object]):
        """draw_callback: async (umo: str) -> None，执行一次定时开奖。"""
        self._cb = draw_callback
        self.scheduler = AsyncIOScheduler(timezone=TZ)

    def start(self):
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self):
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception:
            pass

    def sync_group(self, umo: str, schedule: dict):
        """按群配置重建/移除定时任务。配置非法时抛 ValueError。"""
        jid = _job_id(umo)
        if self.scheduler.get_job(jid):
            self.scheduler.remove_job(jid)
        sched = schedule or {"type": SCHED_NONE}
        trig = build_trigger(sched)
        if trig is None:
            return
        args = [umo]
        if sched.get("type") == SCHED_BIWEEKLY:
            # 锚定周：用第一个触发日的 ISO 周序号作为偶数周基准
            first = trig.get_next_fire_time(None, now_local())
            args.append(int(first.astimezone(TZ).isocalendar()[1]))
        self.scheduler.add_job(
            self._fire,
            trigger=trig,
            id=jid,
            args=args,
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
            max_instances=1,
        )

    def remove_group(self, umo: str):
        jid = _job_id(umo)
        if self.scheduler.get_job(jid):
            self.scheduler.remove_job(jid)

    async def _fire(self, umo: str, anchor_week: int = -1):
        # 双周任务：锚定周之后每隔一周触发（周序号差为偶数）
        if anchor_week >= 0:
            cur_week = int(now_local().isocalendar()[1])
            if (cur_week - anchor_week) % 2 != 0:
                return
        try:
            await self._cb(umo)
        except Exception:
            # 定时开奖异常不应打挂调度器
            import traceback

            traceback.print_exc()
