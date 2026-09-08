# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle —— 分群配置 Web API

供 pages/config 页面通过 window.AstrBotPluginPage.apiGet/apiPost 调用。
路由注册时以插件名为前缀（dashboard 处理），这里写相对路由即可。

约定：所有 handler 内通过 `astrbot.api.web.request` 获取当前请求，
用 `astrbot.api.web.json_response` 返回统一信封 {status, message, data}。
"""

import time

try:
    from .settings import MODE_LABELS, SCHED_NONE, normalize_prizes, validate_time
    from .scheduler import preview_next
except ImportError:  # 平铺加载
    from settings import MODE_LABELS, SCHED_NONE, normalize_prizes, validate_time  # type: ignore
    from scheduler import preview_next  # type: ignore


# 允许前端写入的分群配置键白名单
_PATCHABLE = {
    "enabled",
    "mode",
    "cooldown_days",
    "exclude_admins",
    "exclude_recent",
    "at_winners",
    "card_enabled",
    "template",
    "activity_window_days",
    "activity_weight_base",
    "activity_weight_k",
    "activity_min_count",
    "schedule",
}


def _json(ok=True, data=None, message=""):
    try:
        from astrbot.api.web import json_response

        return json_response({"status": "ok" if ok else "error",
                              "message": message, "data": data})
    except Exception:
        return {"status": "ok" if ok else "error", "message": message, "data": data}


def _req():
    from astrbot.api.web import request

    return request


def _group_summary(store, db, umo: str) -> dict:
    gs = store.get(umo)
    gid = umo.split(":")[-1] if ":" in umo else umo
    users = db.list_users(umo)
    try:
        nxt = preview_next(gs.schedule, 3)
    except Exception:
        nxt = []
    return {
        "umo": umo,
        "group_id": gid,
        "enabled": gs.enabled,
        "member_observed": len(users),
        "schedule_desc": gs.describe_schedule(),
        "next_fires": nxt,
    }


def _full_config(store, umo: str) -> dict:
    gs = store.get(umo)
    gid = umo.split(":")[-1] if ":" in umo else umo
    try:
        nxt = preview_next(gs.schedule, 3)
    except Exception:
        nxt = []
    return {
        "umo": umo,
        "group_id": gid,
        "enabled": gs.enabled,
        "mode": gs.mode,
        "mode_label": MODE_LABELS.get(gs.mode, gs.mode),
        "prizes": gs.prizes,
        "cooldown_days": gs.cooldown_days,
        "exclude_admins": gs.exclude_admins,
        "exclude_recent": gs.exclude_recent,
        "at_winners": gs.at_winners,
        "card_enabled": gs.card_enabled,
        "template": gs.template,
        "activity_window_days": gs.activity_window_days,
        "activity_weight_base": gs.activity_weight_base,
        "activity_weight_k": gs.activity_weight_k,
        "activity_min_count": gs.activity_min_count,
        "schedule": gs.schedule,
        "schedule_desc": gs.describe_schedule(),
        "next_fires": nxt,
    }


class WebApiHandler:
    """持有插件实例，方法作为路由 handler。"""

    def __init__(self, plugin):
        self.plugin = plugin

    # ---------- GET ----------

    async def list_groups(self, **path):
        """GET 列出所有出现过的群（观测成员或有配置的群）。"""
        try:
            store = self.plugin.store
            db = self.plugin.db
            umos = set()
            for r in db.list_settings_rows():
                umos.add(r["umo"])
            # 也纳入观测到的群（users 表）
            try:
                with db._LOCK:
                    rows = db.conn.execute(
                        "SELECT DISTINCT umo FROM users"
                    ).fetchall()
                umos.update(r["umo"] for r in rows)
            except Exception:
                pass
            data = sorted(_group_summary(store, db, u) for u in umos)
            return _json(ok=True, data={"groups": data})
        except Exception as e:
            return _json(ok=False, message=str(e))

    async def get_config(self, group_id: str = "", **path):
        """GET 获取单个群完整配置。group_id 为 unified_msg_origin 或纯群号。"""
        try:
            umo = self._resolve_umo(group_id)
            if not umo:
                return _json(ok=False, message=f"未找到群：{group_id}")
            return _json(ok=True, data=_full_config(self.plugin.store, umo))
        except Exception as e:
            return _json(ok=False, message=str(e))

    # ---------- POST ----------

    async def update_config(self, **path):
        """POST 更新单群配置。body: {group_id|umo, patch:{...}}"""
        try:
            body = await _req().json(default={}) or {}
            umo = self._resolve_umo(body.get("umo") or body.get("group_id"))
            if not umo:
                return _json(ok=False, message="缺少 umo/group_id")
            gs = self.plugin.store.get(umo)
            patch = body.get("patch") or {}

            # 开关
            if "enabled" in patch:
                gs.set_enabled(bool(patch["enabled"]))
                if not bool(patch["enabled"]):
                    self.plugin.sched.remove_group(umo)

            clean = {}
            for k, v in patch.items():
                if k in ("enabled",):
                    continue
                if k not in _PATCHABLE:
                    continue
                clean[k] = v

            # 等次特殊处理（前端传 "一等奖:1,二等奖:2" 或数组）
            if "prizes" in patch:
                clean["prizes"] = normalize_prizes(patch["prizes"])
            # 定时校验
            if "schedule" in patch and isinstance(clean.get("schedule"), dict):
                self._validate_schedule(clean["schedule"])

            if clean:
                gs.update(clean)

            # 定时变更后重建任务
            if "schedule" in clean:
                self.plugin.sched.sync_group(umo, gs.schedule)

            return _json(ok=True, data=_full_config(self.plugin.store, umo),
                         message="已保存")
        except ValueError as e:
            return _json(ok=False, message=str(e))
        except Exception as e:
            return _json(ok=False, message=f"保存失败：{e}")

    async def enable_group(self, **path):
        body = await _req().json(default={}) or {}
        umo = self._resolve_umo(body.get("umo") or body.get("group_id"))
        if not umo:
            return _json(ok=False, message="缺少 umo/group_id")
        on = bool(body.get("enabled", True))
        gs = self.plugin.store.get(umo)
        gs.set_enabled(on)
        if on:
            self.plugin.sched.sync_group(umo, gs.schedule)
        else:
            self.plugin.sched.remove_group(umo)
        return _json(ok=True, data={"umo": umo, "enabled": on}, message="已更新")

    async def draw_now(self, **path):
        """POST 触发一次真实开奖。body: {umo}"""
        body = await _req().json(default={}) or {}
        umo = self._resolve_umo(body.get("umo") or body.get("group_id"))
        if not umo:
            return _json(ok=False, message="缺少 umo/group_id")
        err = await self.plugin.do_draw(umo, trigger="manual")
        if err:
            return _json(ok=False, message=err)
        return _json(ok=True, message="开奖指令已执行，请在群内查看结果")

    async def overview(self, **path):
        """GET 全局总览统计。"""
        try:
            db = self.plugin.db
            stats = db.stats_overview()
            recent = db.list_winners(limit=10)
            for r in recent:
                r["time_str"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
                r["group_id"] = r["umo"].split(":")[-1]
            return _json(ok=True, data={"stats": stats, "recent": recent})
        except Exception as e:
            return _json(ok=False, message=str(e))

    async def list_winners(self, **path):
        """GET 中奖记录。query: group_id(可选)、limit。"""
        try:
            q = _req().query
            gid = q.get("group_id", "")
            limit = q.get("limit", "100", type=int) or 100
            umo = self._resolve_umo(gid) if gid else None
            rows = self.plugin.db.list_winners(umo, min(max(limit, 1), 500))
            for r in rows:
                r["time_str"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
                r["group_id"] = r["umo"].split(":")[-1]
            return _json(ok=True, data={"winners": rows, "total": len(rows)})
        except Exception as e:
            return _json(ok=False, message=str(e))

    async def get_global(self, **path):
        """GET 全局配置（WebUI _conf_schema 的当前值）。"""
        try:
            cfg = self.plugin.config
            keys = [
                "enabled_groups_mode", "group_whitelist", "group_blacklist", "admins",
                "card_enabled", "at_enabled", "default_template", "contact",
                "default_mode", "daily_msg_cap", "msg_min_gap_seconds",
                "data_retention_days",
            ]
            data = {k: cfg.get(k) for k in keys}
            return _json(ok=True, data=data)
        except Exception as e:
            return _json(ok=False, message=str(e))

    async def update_global(self, **path):
        """POST 更新全局配置（白名单字段）。body: {patch:{...}}"""
        try:
            body = await _req().json(default={}) or {}
            patch = body.get("patch") or {}
            allowed = {
                "enabled_groups_mode", "group_whitelist", "group_blacklist", "admins",
                "card_enabled", "at_enabled", "default_template", "contact",
                "default_mode", "daily_msg_cap", "msg_min_gap_seconds",
                "data_retention_days",
            }
            cfg = self.plugin.config
            changed = []
            for k, v in patch.items():
                if k not in allowed:
                    continue
                cfg[k] = v
                changed.append(k)
            try:
                cfg.save_config()
            except Exception:
                pass
            return _json(ok=True, data={"changed": changed}, message="全局配置已保存")
        except Exception as e:
            return _json(ok=False, message=f"保存失败：{e}")

    # ---------- 工具 ----------

    def _resolve_umo(self, key: str) -> str:
        """接受完整 unified_msg_origin 或纯群号；纯群号在已知群中匹配。"""
        if not key:
            return ""
        key = str(key).strip()
        if ":" in key:
            return key
        db = self.plugin.db
        candidates = set()
        for r in db.list_settings_rows():
            candidates.add(r["umo"])
        try:
            with db._LOCK:
                rows = db.conn.execute("SELECT DISTINCT umo FROM users").fetchall()
            candidates.update(r["umo"] for r in rows)
        except Exception:
            pass
        # 纯群号匹配末尾段
        for umo in candidates:
            if umo.split(":")[-1] == key:
                return umo
        # 未出现过的群，构造一个 aiocqhttp 形式的 umo（bot 账号未知时用 0）
        if key.isdigit():
            return f"aiocqhttp:0:{key}"
        return ""

    @staticmethod
    def _validate_schedule(sched: dict):
        t = sched.get("type", SCHED_NONE)
        if t in ("daily", "weekly", "biweekly", "monthly"):
            validate_time(sched.get("time", "20:00"))
        if t == "custom":
            cron = (sched.get("cron") or "").strip()
            if len(cron.split()) != 5:
                raise ValueError("cron 必须是 5 段：分 时 日 月 周")
        # 先构造一次 trigger 以暴露非法配置
        try:
            from .scheduler import build_trigger
        except ImportError:
            from scheduler import build_trigger  # type: ignore
        build_trigger(sched)
