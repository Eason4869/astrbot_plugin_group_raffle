# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle —— 数据层（SQLite）

所有抽奖设置按群独立存储（group_settings 表，JSON blob），
活跃度按「群 + 用户 + 天」聚合，不保存消息正文。
"""

import os
import sqlite3
import threading
from typing import Optional

_LOCK = threading.RLock()


def _data_dir() -> str:
    """定位插件数据目录。优先 AstrBot 的 plugin_data，失败则回退到插件目录下 data/。"""
    # 1) AstrBot 环境
    try:
        from astrbot.api.star import StarTools

        try:
            d = StarTools.get_data_dir("astrbot_plugin_group_raffle")
        except TypeError:
            d = StarTools.get_data_dir()  # 旧版本无参签名
        if d:
            os.makedirs(d, exist_ok=True)
            return d
    except Exception:
        pass
    # 2) 回退：插件目录/data
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(d, exist_ok=True)
    return d


_SCHEMA = [
    # 观测到的群成员（用于等权抽奖的候选池与昵称展示）
    """
    CREATE TABLE IF NOT EXISTS users (
        umo   TEXT NOT NULL,
        uid   TEXT NOT NULL,
        name  TEXT,
        PRIMARY KEY (umo, uid)
    )
    """,
    # 群管理员缓存（角色来自消息事件，用于"排除管理员"）
    """
    CREATE TABLE IF NOT EXISTS group_admins (
        umo   TEXT NOT NULL,
        uid   TEXT NOT NULL,
        role  TEXT,
        ts    INTEGER NOT NULL,
        PRIMARY KEY (umo, uid)
    )
    """,
    # 活跃度按天聚合（不存消息正文）
    """
    CREATE TABLE IF NOT EXISTS activity (
        umo       TEXT NOT NULL,
        uid       TEXT NOT NULL,
        day       TEXT NOT NULL,   -- YYYY-MM-DD（本地时区）
        msg_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (umo, uid, day)
    )
    """,
    # 报名记录（按群 + 场次）
    """
    CREATE TABLE IF NOT EXISTS signups (
        umo  TEXT NOT NULL,
        sid  INTEGER NOT NULL,
        uid  TEXT NOT NULL,
        name TEXT,
        ts   INTEGER NOT NULL,
        PRIMARY KEY (umo, sid, uid)
    )
    """,
    # 中奖记录（用于冷却/防连中与审计）
    """
    CREATE TABLE IF NOT EXISTS winners (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        umo   TEXT NOT NULL,
        uid   TEXT NOT NULL,
        name  TEXT,
        prize TEXT,
        ts    INTEGER NOT NULL
    )
    """,
    # 开奖流水
    """
    CREATE TABLE IF NOT EXISTS draws (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        umo         TEXT NOT NULL,
        ts          INTEGER NOT NULL,
        mode        TEXT,
        trigger     TEXT,          -- manual / scheduled
        result_json TEXT
    )
    """,
    # 分群独立配置（JSON blob）
    """
    CREATE TABLE IF NOT EXISTS group_settings (
        umo         TEXT PRIMARY KEY,
        enabled     INTEGER NOT NULL DEFAULT 1,
        config_json TEXT NOT NULL DEFAULT '{}',
        updated_ts  INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_activity_umo_day ON activity(umo, day)",
    "CREATE INDEX IF NOT EXISTS idx_winners_umo_ts ON winners(umo, ts)",
    "CREATE INDEX IF NOT EXISTS idx_signups_umo_sid ON signups(umo, sid)",
    "CREATE INDEX IF NOT EXISTS idx_draws_umo_ts ON draws(umo, ts)",
]


class Database:
    def __init__(self, path: Optional[str] = None):
        self.dir = _data_dir()
        self.path = path or os.path.join(self.dir, "raffle.db")
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with _LOCK:
            for sql in _SCHEMA:
                self.conn.execute(sql)
            self.conn.commit()

    # ---------- 成员 / 管理员 ----------

    def upsert_user(self, umo: str, uid: str, name: str):
        with _LOCK:
            self.conn.execute(
                "INSERT INTO users(umo, uid, name) VALUES(?,?,?) "
                "ON CONFLICT(umo, uid) DO UPDATE SET name=excluded.name",
                (umo, str(uid), name or str(uid)),
            )
            self.conn.commit()

    def list_users(self, umo: str):
        with _LOCK:
            rows = self.conn.execute(
                "SELECT uid, name FROM users WHERE umo=?", (umo,)
            ).fetchall()
        return [(r["uid"], r["name"] or r["uid"]) for r in rows]

    def upsert_admin(self, umo: str, uid: str, role: str, ts: int):
        with _LOCK:
            self.conn.execute(
                "INSERT INTO group_admins(umo, uid, role, ts) VALUES(?,?,?,?) "
                "ON CONFLICT(umo, uid) DO UPDATE SET role=excluded.role, ts=excluded.ts",
                (umo, str(uid), role or "admin", ts),
            )
            self.conn.commit()

    def list_admin_uids(self, umo: str):
        with _LOCK:
            rows = self.conn.execute(
                "SELECT uid FROM group_admins WHERE umo=?", (umo,)
            ).fetchall()
        return {r["uid"] for r in rows}

    # ---------- 活跃度 ----------

    def add_activity(self, umo: str, uid: str, day: str, cap: int):
        """当天有效消息数 +1，达到 cap 后不再增长。返回增长后的计数。"""
        with _LOCK:
            self.conn.execute(
                "INSERT INTO activity(umo, uid, day, msg_count) VALUES(?,?,?,1) "
                "ON CONFLICT(umo, uid, day) DO UPDATE SET "
                "msg_count = CASE WHEN msg_count < ? THEN msg_count + 1 ELSE msg_count END",
                (umo, str(uid), day, cap),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT msg_count FROM activity WHERE umo=? AND uid=? AND day=?",
                (umo, str(uid), day),
            ).fetchone()
        return row["msg_count"] if row else 0

    def get_activity_counts(self, umo: str, days: list):
        """统计给定日期列表（YYYY-MM-DD）内各用户的有效消息总数。"""
        if not days:
            return {}
        placeholders = ",".join("?" * len(days))
        with _LOCK:
            rows = self.conn.execute(
                f"SELECT uid, SUM(msg_count) AS c FROM activity "
                f"WHERE umo=? AND day IN ({placeholders}) GROUP BY uid",
                [umo] + days,
            ).fetchall()
        return {r["uid"]: int(r["c"] or 0) for r in rows}

    def cleanup_activity(self, before_day: str):
        with _LOCK:
            cur = self.conn.execute(
                "DELETE FROM activity WHERE day < ?", (before_day,)
            )
            self.conn.commit()
            return cur.rowcount

    # ---------- 报名 ----------

    def add_signup(self, umo: str, sid: int, uid: str, name: str, ts: int) -> bool:
        """报名；返回 False 表示已报过。"""
        with _LOCK:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO signups(umo, sid, uid, name, ts) VALUES(?,?,?,?,?)",
                (umo, sid, str(uid), name or str(uid), ts),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def remove_signup(self, umo: str, sid: int, uid: str) -> bool:
        with _LOCK:
            cur = self.conn.execute(
                "DELETE FROM signups WHERE umo=? AND sid=? AND uid=?",
                (umo, sid, str(uid)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def list_signups(self, umo: str, sid: int):
        with _LOCK:
            rows = self.conn.execute(
                "SELECT uid, name FROM signups WHERE umo=? AND sid=?",
                (umo, sid),
            ).fetchall()
        return [(r["uid"], r["name"] or r["uid"]) for r in rows]

    def clear_signups(self, umo: str, sid: int):
        with _LOCK:
            self.conn.execute("DELETE FROM signups WHERE umo=? AND sid=?", (umo, sid))
            self.conn.commit()

    # ---------- 中奖记录 ----------

    def add_winners(self, umo: str, items: list, ts: int):
        """items: [(uid, name, prize), ...]"""
        with _LOCK:
            self.conn.executemany(
                "INSERT INTO winners(umo, uid, name, prize, ts) VALUES(?,?,?,?,?)",
                [(umo, str(uid), name, prize, ts) for (uid, name, prize) in items],
            )
            self.conn.commit()

    def recent_winner_uids(self, umo: str, since_ts: int) -> set:
        if since_ts <= 0:
            return set()
        with _LOCK:
            rows = self.conn.execute(
                "SELECT DISTINCT uid FROM winners WHERE umo=? AND ts>=?",
                (umo, since_ts),
            ).fetchall()
        return {r["uid"] for r in rows}

    def add_draw_log(self, umo: str, ts: int, mode: str, trigger: str, result_json: str):
        with _LOCK:
            self.conn.execute(
                "INSERT INTO draws(umo, ts, mode, trigger, result_json) VALUES(?,?,?,?,?)",
                (umo, ts, mode, trigger, result_json),
            )
            self.conn.commit()

    # ---------- 分群配置 ----------

    def get_settings_row(self, umo: str):
        with _LOCK:
            return self.conn.execute(
                "SELECT enabled, config_json, updated_ts FROM group_settings WHERE umo=?",
                (umo,),
            ).fetchone()

    def list_settings_rows(self):
        with _LOCK:
            return self.conn.execute(
                "SELECT umo, enabled, config_json FROM group_settings"
            ).fetchall()

    def upsert_settings(self, umo: str, enabled: bool, config_json: str, ts: int):
        with _LOCK:
            self.conn.execute(
                "INSERT INTO group_settings(umo, enabled, config_json, updated_ts) VALUES(?,?,?,?) "
                "ON CONFLICT(umo) DO UPDATE SET enabled=excluded.enabled, "
                "config_json=excluded.config_json, updated_ts=excluded.updated_ts",
                (umo, 1 if enabled else 0, config_json, ts),
            )
            self.conn.commit()

    def close(self):
        with _LOCK:
            try:
                self.conn.close()
            except Exception:
                pass
