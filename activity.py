# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle —— 活跃度统计

只记录 (群, 用户, 天) 的有效消息条数，不保存消息正文。
防刷：两条消息间隔小于阈值不计入；每天计数封顶。
"""

import datetime
import math
import threading
from typing import Optional

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # 极端环境（无 tzdata）退化为 UTC+8 固定偏移
    TZ = datetime.timezone(datetime.timedelta(hours=8))


def now_local() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def window_days(n: int, ref: Optional[datetime.date] = None) -> list[str]:
    """返回最近 n 天（含今天）的日期字符串列表，旧 -> 新。"""
    ref = ref or now_local().date()
    return [(ref - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def day_cutoff(n_days_ago: int) -> str:
    return (now_local().date() - datetime.timedelta(days=n_days_ago)).strftime("%Y-%m-%d")


class ActivityTracker:
    def __init__(self, db, min_gap_seconds: int = 5, daily_cap: int = 30):
        self.db = db
        self.min_gap = max(0, int(min_gap_seconds))
        self.daily_cap = max(1, int(daily_cap))
        # (umo, uid) -> 上次计入时间戳；防短间隔刷屏
        self._last_ts: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def record_message(self, umo: str, uid: str, name: str, ts: float, is_admin: bool = False):
        """在消息钩子中调用。返回是否计入活跃度。"""
        uid = str(uid)
        self.db.upsert_user(umo, uid, name)
        if is_admin:
            try:
                self.db.upsert_admin(umo, uid, "admin", int(ts))
            except Exception:
                pass
        key = (umo, uid)
        with self._lock:
            last = self._last_ts.get(key)
            if last is not None and (ts - last) < self.min_gap:
                return False
            self._last_ts[key] = ts
        self.db.add_activity(umo, uid, today_str(), self.daily_cap)
        return True

    def counts(self, umo: str, window_days_count: int) -> dict[str, int]:
        return self.db.get_activity_counts(umo, window_days(window_days_count))

    def cleanup(self, retention_days: int):
        if retention_days and retention_days > 0:
            cutoff = day_cutoff(retention_days)
            return self.db.cleanup_activity(cutoff)
        return 0


def activity_weight(count: int, base: float = 1.0, k: float = 1.0) -> float:
    """消息条数 -> 权重。开平方压缩，避免刷屏者通吃；所有人至少有 base 权重。"""
    return max(0.0, float(base)) + float(k) * math.sqrt(max(0, int(count)))
