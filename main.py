# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle

分群独立配置的群聊抽奖插件：
手动/定时开奖、全员等权/活跃度加权/报名参与、多等次多名额、
防连中冷却、@中奖者（平台降级）、自定义模板、卡片渲染（htmlrender→Pillow→文本）。
"""

import asyncio
import json
import time

from astrbot.api.event import filter
from astrbot.api.star import Star, Context
from astrbot.api import AstrBotConfig, logger

# 兼容不同 AstrBot 版本的注册器名称
import astrbot.api.star as _star_mod

register_star = getattr(_star_mod, "register", None) or getattr(_star_mod, "register_star", None)
if register_star is None:  # 兜底
    def register_star(name, author, desc, version="0.0.0"):
        def deco(cls):
            return cls
        return deco

try:
    from astrbot.api.event.filter import event_message_type, EventMessageType
except Exception:  # 兼容旧版本路径
    from astrbot.core.star.filter.event_message_type import (  # type: ignore
        event_message_type,
        EventMessageType,
    )

try:
    from .db import Database
    from .settings import (
        SettingsStore,
        MODE_EQUAL,
        MODE_ACTIVITY,
        MODE_SIGNUP,
        MODE_LABELS,
        SCHED_NONE,
        SCHED_DAILY,
        SCHED_WEEKLY,
        SCHED_BIWEEKLY,
        SCHED_MONTHLY,
        SCHED_CUSTOM,
        WEEK_CN,
        normalize_prizes,
        validate_time,
    )
    from .activity import ActivityTracker, now_local
    from .engine import draw, gather_candidates, DrawError
    from .render import render_result_card, render_help_image, render_info_card
    from .notifier import send_result, group_id_of
    from .scheduler import RaffleScheduler, preview_next
    from .help_data import help_rows_as_dicts, HELP_ROWS
    from .web_api import WebApiHandler
except ImportError:  # 平铺加载（AstrBot 直接加载 main.py）
    from db import Database  # type: ignore
    from settings import (  # type: ignore
        SettingsStore,
        MODE_EQUAL,
        MODE_ACTIVITY,
        MODE_SIGNUP,
        MODE_LABELS,
        SCHED_NONE,
        SCHED_DAILY,
        SCHED_WEEKLY,
        SCHED_BIWEEKLY,
        SCHED_MONTHLY,
        SCHED_CUSTOM,
        WEEK_CN,
        normalize_prizes,
        validate_time,
    )
    from activity import ActivityTracker, now_local  # type: ignore
    from engine import draw, gather_candidates, DrawError  # type: ignore
    from render import render_result_card, render_help_image, render_info_card  # type: ignore
    from notifier import send_result, group_id_of  # type: ignore
    from scheduler import RaffleScheduler, preview_next  # type: ignore
    from help_data import help_rows_as_dicts, HELP_ROWS  # type: ignore
    from web_api import WebApiHandler  # type: ignore

WEEK_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _parse_weekday(s: str) -> int:
    s = s.strip()
    if s.isdigit():
        v = int(s)
        if 0 <= v <= 6:
            return v
    for k, v in WEEK_MAP.items():
        if k in s:
            return v
    raise ValueError("星期格式错误（0-6，0=周一；或 周一/周日）")


def _on_off(s: str) -> bool:
    s = s.strip()
    if s in ("开", "on", "是", "1", "true", "启用"):
        return True
    if s in ("关", "off", "否", "0", "false", "停用"):
        return False
    raise ValueError("请用 开/关")


def _fallback_help_text() -> str:
    """图片渲染失败时的纯文本帮助（内容来源同 help_data）。"""
    lines = ["🎰 群抽奖助手 · 命令帮助",
             "主命令：抽奖（英文别名 raffle，等价，如 /raffle 开奖）", ""]
    for cmd, perm, desc in HELP_ROWS:
        lines.append(f"[{perm}] {cmd} —— {desc}")
    lines.append("")
    lines.append("所有设置按群独立保存，重启自动恢复；「抽奖 模拟」不影响真实数据。")
    return "\n".join(lines)


@register_star(
    "astrbot_plugin_group_raffle",
    "Eason4869",
    "群抽奖助手 GroupRaffle：分群配置/定时/活跃度加权/报名/多等次/@/卡片",
    "0.3.9-beta",
)
class GroupRafflePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.db = Database()
        self.store = SettingsStore(self.db, dict(config))
        self.tracker = ActivityTracker(
            self.db,
            min_gap_seconds=int(config.get("msg_min_gap_seconds", 5)),
            daily_cap=int(config.get("daily_msg_cap", 30)),
        )
        self.sched = RaffleScheduler(self._scheduled_draw)
        self._signup_sid: dict[str, int] = {}
        self.sched.start()
        self._reload_all_schedules()
        try:
            self.tracker.cleanup(int(config.get("data_retention_days", 60)))
        except Exception:
            pass
        self._register_web_apis()
        logger.info("[GroupRaffle] 插件已加载")

    def _register_web_apis(self):
        """注册分群配置 Web API（供插件配置页调用）。"""
        try:
            api = WebApiHandler(self)
            p = "astrbot_plugin_group_raffle"
            ctx = self.context
            ctx.register_web_api(
                f"/{p}/overview", api.overview, ["GET"], "总览统计")
            ctx.register_web_api(
                f"/{p}/winners", api.list_winners, ["GET"], "中奖记录")
            ctx.register_web_api(
                f"/{p}/global", api.get_global, ["GET"], "获取全局配置")
            ctx.register_web_api(
                f"/{p}/global", api.update_global, ["POST"], "更新全局配置")
            ctx.register_web_api(
                f"/{p}/groups", api.list_groups, ["GET"], "列出所有群")
            ctx.register_web_api(
                f"/{p}/config/<group_id>", api.get_config, ["GET"], "获取单群配置")
            ctx.register_web_api(
                f"/{p}/config", api.update_config, ["POST"], "更新单群配置")
            ctx.register_web_api(
                f"/{p}/enable", api.enable_group, ["POST"], "启用/停用群")
            ctx.register_web_api(
                f"/{p}/draw", api.draw_now, ["POST"], "触发开奖")
        except Exception as e:
            logger.warning(f"[GroupRaffle] Web API 注册失败（可能是旧版 AstrBot）：{e}")

    async def initialize(self):
        self._reload_all_schedules()

    async def terminate(self):
        try:
            self.sched.shutdown()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        logger.info("[GroupRaffle] 插件已卸载")

    # ---------------- 基础工具 ----------------

    def _is_admin(self, event) -> bool:
        """是否为管理员。

        Admin 判定 = AstrBot 管理员/宿主（event.is_admin()）+ 本插件 admins 配置
        + 群主/群管理员（部分平台通过消息里的角色标记）。此判定同样用于
        @filter.permission_type(PermissionType.ADMIN) 的补充兜底，保证不止宿主
        可操作管理员指令。
        """
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            uid = str(event.get_sender_id())
            if uid and uid in {str(x) for x in self.config.get("admins", [])}:
                return True
        except Exception:
            pass
        try:  # 群主 / 群管理员（平台提供 sender.role 时）
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            role = getattr(sender, "role", None)
            if str(role).lower() in ("owner", "admin", "groupowner", "groupadmin"):
                return True
        except Exception:
            pass
        return False

    def _group_enabled(self, umo: str) -> bool:
        try:
            gs = self.store.get(umo)
            if not gs.enabled:
                return False
            gid = group_id_of(umo)
            mode = self.config.get("enabled_groups_mode", "blacklist")
            wl = {str(x) for x in self.config.get("group_whitelist", [])}
            bl = {str(x) for x in self.config.get("group_blacklist", [])}
            if mode == "whitelist":
                return gid in wl
            return gid not in bl
        except Exception:
            return True

    def _sid(self, umo: str) -> int:
        return self._signup_sid.setdefault(umo, 1)

    def _reload_all_schedules(self):
        try:
            for row in self.db.list_settings_rows():
                umo = row["umo"]
                try:
                    gs = self.store.get(umo)
                    self.sched.sync_group(umo, gs.schedule)
                except Exception as e:
                    logger.warning(f"[GroupRaffle] 恢复定时任务失败 {umo}: {e}")
        except Exception as e:
            logger.warning(f"[GroupRaffle] 遍历群配置失败: {e}")

    # ---------------- 消息钩子（活跃度采集） ----------------

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event):
        try:
            umo = event.unified_msg_origin
            if not umo or event.get_group_id() is None:
                return
            if not self._group_enabled(umo):
                return
            uid = event.get_sender_id()
            name = event.get_sender_name() or str(uid)
            is_admin = False
            try:
                is_admin = bool(event.is_admin())
            except Exception:
                pass
            await asyncio.to_thread(
                self.tracker.record_message, umo, uid, name, time.time(), is_admin
            )
        except Exception as e:
            logger.debug(f"[GroupRaffle] 活跃度记录失败: {e}")

    # ---------------- 命令入口（指令组） ----------------
    #
    # AstrBot 对指令组只会「按子指令精确匹配后直接调用该子指令 handler」，
    # 指令组的父级方法（cmd_raffle）本身不会被调用，仅用于生成指令树 / 行为管理列表。
    # 因此把所有业务逻辑收敛到一个统一的 _dispatch_sub(event) 内部，子指令 handler
    # 只是「被 AstrBot 唤醒」的入口，再委托给 _dispatch_sub，避免逻辑重复。
    # 这样行为管理中可对每个子指令单独启停，README 中「管理员 / 所有人」权限也统一落实。

    @filter.command_group("抽奖", alias={"raffle"})
    async def cmd_raffle(self, event):
        """主指令组「抽奖」（别名 raffle）。仅发送「抽奖」时框架会提示指令列表。"""
        async for r in self._dispatch_command(event):
            yield r

    def _command_tokens(self, event) -> list[str]:
        """把完整消息还原成去掉引导词（/抽奖、/raffle）后的参数列表。"""
        parts = (event.message_str or "").strip().split()
        while parts and parts[0].lstrip("/").lower() in ("抽奖", "raffle"):
            parts = parts[1:]
        return parts

    async def _dispatch_command(self, event):
        """统一派发入口：执行子命令后终止事件，避免结果被 LLM 接管/重复回复。

        AstrBot 中插件的消息结果默认 result_type=CONTINUE，且只要事件未被
        stop、未发生真实 send，ProcessStage 就会继续调用 LLM。因此这里无论
        子命令以「yield 结果」还是「context.send_message 直发」返回，都在
        完成后 stop_event，确保命令由插件完整接管、不再触发 LLM 闲聊。
        """
        async for r in self._dispatch_inner(event):
            yield r
        try:
            event.stop_event()
        except Exception:
            pass

    async def _dispatch_inner(self, event):
        """统一派发：解析子命令 → 帮助/启用等特殊分支 → 权限检查 → 执行子命令。"""
        parts = self._command_tokens(event)
        sub = parts[0] if parts else "帮助"
        args = parts[1:] if len(parts) > 1 else []

        umo = event.unified_msg_origin
        try:
            gs = self.store.get(umo)
        except Exception as e:
            yield event.plain_result(f"抽奖插件初始化失败：{e}")
            return

        # 无需群启用即可用的命令：帮助、启用、状态、名单、定时预览（信息查询类）
        if sub in ("帮助", "help", ""):
            async for r in self._yield_help(event):
                yield r
            return

        if sub in ("启用", "on"):
            if not self._is_admin(event):
                yield event.plain_result("仅群主/管理员可操作。")
                return
            gs.set_enabled(True)
            self._group_enabled(umo)  # 预热
            yield event.plain_result("✅ 本群抽奖已启用。")
            return

        read_only = {"状态", "名单", "定时预览"}
        if not self._group_enabled(umo) and sub not in ("停用", "off") and sub not in read_only:
            yield event.plain_result("本群抽奖未启用（管理员发送「抽奖 启用」开启）。")
            return

        handler = {
            "停用": self._h_disable,
            "off": self._h_disable,
            "状态": self._h_status,
            "报名": self._h_join,
            "join": self._h_join,
            "取消报名": self._h_quit,
            "quit": self._h_quit,
            "名单": self._h_list,
            "开奖": self._h_draw,
            "draw": self._h_draw,
            "模拟": self._h_simulate,
            "simulate": self._h_simulate,
            "测试": self._h_simulate,
            "test": self._h_simulate,
            "模式": self._h_mode,
            "等次": self._h_prizes,
            "冷却": self._h_cooldown,
            "排除管理员": self._h_exclude_admins,
            "艾特": self._h_at,
            "at": self._h_at,
            "卡片": self._h_card,
            "模板": self._h_template,
            "窗口": self._h_window,
            "定时": self._h_schedule,
            "定时预览": self._h_schedule_preview,
        }.get(sub)

        if handler is None:
            yield event.plain_result(f"未知子命令：{sub}\n发送「抽奖 帮助」查看用法。")
            return

        # README 权限：状态 / 报名 / 取消报名 / 名单 / 定时预览 对所有人开放，其余默认仅管理员
        everyone = {"状态", "报名", "join", "取消报名", "quit", "名单", "定时预览"}
        if sub not in everyone and not self._is_admin(event):
            yield event.plain_result("仅群主/管理员可执行该操作。")
            return

        try:
            result = handler(event, args, gs)
            if asyncio.iscoroutine(result):
                result = await result
            if not result:
                return
            # 信息卡片：handler 返回 {"card": {...}, "fallback": str}。
            # 卡片优先：通过 context 直发图片；若渲染失败或图片发送失败(如 QQ
            # highway 921)，自动回退纯文本，保证命令一定有返回。
            if isinstance(result, dict) and "card" in result:
                spec = result["card"]
                fallback = result.get("fallback") or ""
                image_ok = False
                try:
                    from astrbot.core.message.components import Image
                    from astrbot.core.message.message_event_result import MessageChain

                    path = await render_info_card(
                        self, spec.get("title", ""), spec.get("subtitle", ""),
                        spec.get("sections", []),
                    )
                    if path:
                        try:
                            await self.context.send_message(
                                event.unified_msg_origin,
                                MessageChain(chain=[Image.fromFileSystem(path)]),
                            )
                            image_ok = True
                        except Exception as e:
                            logger.warning(
                                f"[GroupRaffle] 信息卡片图片发送失败，回退文本：{e}"
                            )
                except Exception as e:
                    logger.warning(f"[GroupRaffle] 信息卡片渲染失败，回退文本：{e}")
                if not image_ok:
                    yield event.plain_result(fallback)
            else:
                yield event.plain_result(result)
        except DrawError as e:
            yield event.plain_result(f"⚠️ {e}")
        except ValueError as e:
            yield event.plain_result(f"⚠️ 参数错误：{e}")
        except Exception as e:
            logger.error(f"[GroupRaffle] 命令执行失败: {e}")
            yield event.plain_result(f"⚠️ 执行失败：{e}")

    # ---- 子指令真实 handler ----
    # 每个子指令都由 AstrBot 直接派发，再统一委托给 _dispatch_command，
    # 这里只接收 event（args 在 _dispatch_command 里从 message_str 重新解析），
    # 从而保留「子命令可跟不定长参数」的能力（模板 / 定时 / 等次 等自由文本）。

    @cmd_raffle.command("帮助", alias={"help"})
    async def _sub_help(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("状态", alias={"status"})
    async def _sub_status(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("报名", alias={"signup", "join"})
    async def _sub_join(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("取消报名", alias={"quit"})
    async def _sub_quit(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("名单", alias={"list"})
    async def _sub_list(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("启用", alias={"on"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_enable(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("停用", alias={"off"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_disable(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("开奖", alias={"draw"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_draw(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("模拟", alias={"simulate", "测试", "test"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_simulate(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("模式", alias={"mode"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_mode(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("等次", alias={"prizes"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_prizes(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("冷却", alias={"cooldown"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_cooldown(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("排除管理员", alias={"exclude"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_exadmins(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("艾特", alias={"at"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_at(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("卡片", alias={"card"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_card(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("模板", alias={"template"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_template(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("窗口", alias={"window"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_window(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("定时", alias={"schedule"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _sub_schedule(self, e):
        async for r in self._dispatch_command(e):
            yield r

    @cmd_raffle.command("定时预览", alias={"sched_preview"})
    async def _sub_sched_preview(self, e):
        async for r in self._dispatch_command(e):
            yield r

    # ---- 未识别子命令的兜底 ----
    # 指令组只会精确匹配已注册子指令；遇到「抽奖 xxx」这类未知子命令时，
    # 用一个低优先级正则 handler 给出友好提示（已被子指令命中的消息本方法返回空，
    # 不会与子指令重复响应）。
    @filter.regex(r"^(?:抽奖|raffle)\s+\S+")
    async def _unknown_sub(self, event):
        parts = self._command_tokens(event)
        if not parts:
            return
        sub = parts[0].lstrip("/")
        known = {
            "管理", "help", "帮助", "状态", "status",
            "报名", "signup", "join", "取消报名", "quit",
            "名单", "list", "启用", "on", "停用", "off",
            "开奖", "draw", "模拟", "simulate", "测试", "test",
            "模式", "mode", "等次", "prizes", "冷却", "cooldown",
            "排除管理员", "exclude", "艾特", "at", "卡片", "card",
            "模板", "template", "窗口", "window", "定时", "schedule",
            "定时预览", "sched_preview",
        }
        if sub in known:
            return
        yield event.plain_result(f"未知子命令：{sub}\n发送「抽奖 帮助」查看全部命令。")

    # ---------------- 子命令处理 ----------------

    async def _yield_help(self, event):
        """帮助：优先发送命令表图片，失败回退纯文本。"""
        path = await render_help_image(self, help_rows_as_dicts())
        if path:
            try:
                yield event.image_result(path)
                return
            except Exception as e:
                logger.warning(f"[GroupRaffle] 帮助图片发送失败，回退文本：{e}")
        yield event.plain_result(_fallback_help_text())

    def _h_disable(self, event, args, gs):
        gs.set_enabled(False)
        self.sched.remove_group(gs.umo)
        return "⏸️ 本群抽奖已停用，定时任务已暂停。"

    def _h_status(self, event, args, gs):
        """本群配置：纯文本返回（可靠，不依赖图片上传）。"""
        umo = gs.umo
        mode_label = MODE_LABELS.get(gs.mode, gs.mode)
        prizes = "、".join(f"{p['name']}×{p['count']}" for p in gs.prizes) or "（未设置）"
        if gs.exclude_recent and gs.cooldown_days > 0:
            cool = f"{gs.cooldown_days} 天"
            cool += "（跨群）" if gs.cooldown_cross_group else ""
            cool += "，排除近期中奖"
        else:
            cool = "关闭"
        L = [
            "🎰 本群抽奖配置",
            f"状态：{'启用' if gs.enabled else '停用'}",
            f"参与模式：{mode_label}",
            f"中奖等次：{prizes}",
            f"防连中冷却：{cool}",
            f"排除管理员：{'是' if gs.exclude_admins else '否'}",
            f"开奖卡片：{'开启' if gs.card_enabled else '关闭'}",
            f"@中奖者：{'开启' if gs.at_winners else '关闭'}",
            f"定时开奖：{gs.describe_schedule()}",
        ]
        if gs.mode == MODE_ACTIVITY:
            L.append(f"活跃度加权：近 {gs.activity_window_days} 天"
                     f"（最低 {gs.activity_min_count} 条）")
        try:
            nxt = preview_next(gs.schedule, 3)
            if nxt:
                L.append("未来开奖：\n" + "\n".join(f"· {x}" for x in nxt))
        except Exception:
            pass
        sid = self._sid(umo)
        signups = self.db.list_signups(umo, sid)
        if gs.mode == MODE_SIGNUP:
            L.append(f"当前报名场次 #{sid}：{len(signups)} 人")
        return "\n".join(L)

    def _h_join(self, event, args, gs):
        umo = gs.umo
        sid = self._sid(umo)
        ok = self.db.add_signup(umo, sid, event.get_sender_id(),
                                event.get_sender_name() or str(event.get_sender_id()),
                                int(time.time()))
        n = len(self.db.list_signups(umo, sid))
        if ok:
            tip = "" if gs.mode == MODE_SIGNUP else f"（提示：当前模式为「{MODE_LABELS[gs.mode]}」，报名名单仅在报名模式开奖时使用）"
            return f"✅ 报名成功！当前场次 #{sid} 共 {n} 人。{tip}"
        return f"你已经报过名啦～当前场次 #{sid} 共 {n} 人。"

    def _h_quit(self, event, args, gs):
        umo = gs.umo
        sid = self._sid(umo)
        ok = self.db.remove_signup(umo, sid, event.get_sender_id())
        return "已取消报名。" if ok else "你当前没有报名记录。"

    def _h_list(self, event, args, gs):
        umo = gs.umo
        gid = group_id_of(umo)
        if gs.mode == MODE_SIGNUP:
            sid = self._sid(umo)
            members = self.db.list_signups(umo, sid)
            if not members:
                return f"当前场次 #{sid} 还没有人报名，发送「抽奖 报名」参与。"
            shown = members[:30]
            lines = [f"📝 群 {gid} 场次 #{sid} 报名 {len(members)} 人："]
            lines += [f"{i}. {n}" for i, (_, n) in enumerate(shown, 1)]
            if len(members) > 30:
                lines.append(f"…等共 {len(members)} 人（仅显示前 30）")
            return "\n".join(lines)
        if gs.mode == MODE_ACTIVITY:
            counts = self.tracker.counts(umo, gs.activity_window_days)
            if not counts:
                return f"近 {gs.activity_window_days} 天暂无活跃记录（插件安装后才开始统计）。"
            name_map = dict(self.db.list_users(umo))
            top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:20]
            lines = [f"📊 群 {gid} 近 {gs.activity_window_days} 天活跃榜（前 {len(top)} 名）："]
            lines += [f"{i}. {name_map.get(uid, uid)}：{c} 条"
                      for i, (uid, c) in enumerate(top, 1)]
            return "\n".join(lines)
        users = self.db.list_users(umo)
        if not users:
            return "插件还未观测到本群成员（群友发言后自动记录）。"
        shown = users[:30]
        lines = [f"👥 群 {gid} 已观测成员 {len(users)} 人："]
        lines += [f"{i}. {n}" for i, (_, n) in enumerate(shown, 1)]
        if len(users) > 30:
            lines.append(f"…等共 {len(users)} 人（仅显示前 30）")
        return "\n".join(lines)

    async def _h_draw(self, event, args, gs):
        # 始终按本群配置的等次开奖（避免用数字把配置的等次覆盖成单个“幸运奖”）
        err = await self.do_draw(gs.umo, trigger="manual")
        return err  # None 表示已成功发送开奖消息

    async def _h_simulate(self, event, args, gs):
        """模拟开奖：与真实开奖完全一样地发送卡片/@/模板中奖消息，
        但不写中奖记录、不写流水、不清空报名。"""
        err = await self.do_draw(gs.umo, trigger="manual",
                                 simulate=True)
        if err:
            # 模拟是预览工具：成员不足等情况给出更友好的提示，而不是冰冷报错
            return f"无法模拟开奖：{err}"
        return err  # None 表示已发送模拟开奖消息

    def _h_mode(self, event, args, gs):
        if not args:
            raise ValueError("用法：抽奖 模式 等权|加权|报名")
        m = {"等权": MODE_EQUAL, "加权": MODE_ACTIVITY, "报名": MODE_SIGNUP,
             "equal": MODE_EQUAL, "activity": MODE_ACTIVITY, "signup": MODE_SIGNUP}.get(args[0])
        if not m:
            raise ValueError("模式只能是：等权 / 加权 / 报名")
        gs.update({"mode": m})
        tip = ""
        if m == MODE_SIGNUP:
            tip = "\n已开启报名模式，群友发送「抽奖 报名」参与，开奖后自动清空名单并开启新场次。"
        elif m == MODE_ACTIVITY:
            tip = "\n加权模式从插件安装后采集的活跃数据统计，新群可能需要几天数据积累；无人达标时自动回落等权。"
        return f"✅ 参与模式已设为：{MODE_LABELS[m]}。{tip}"

    def _h_prizes(self, event, args, gs):
        if not args:
            raise ValueError("用法：抽奖 等次 一等奖:1,二等奖:2")
        prizes = normalize_prizes(" ".join(args))
        gs.update({"prizes": prizes})
        txt = "、".join(f"{p['name']}×{p['count']}" for p in prizes)
        return f"✅ 中奖等次已设为：{txt}（共 {sum(p['count'] for p in prizes)} 名）。"

    def _h_cooldown(self, event, args, gs):
        if not args:
            raise ValueError("用法：抽奖 冷却 7（天，0=关闭）｜抽奖 冷却 跨群 开|关")
        first = args[0]
        if first in ("跨群", "跨群冷却", "cross"):
            if len(args) < 2:
                raise ValueError("用法：抽奖 冷却 跨群 开|关")
            val = _on_off(args[1])
            gs.update({"cooldown_cross_group": val})
            return f"✅ 跨群防连中冷却已{'开启' if val else '关闭'}（开启后所有群共享冷却，防止跨群连续中奖）。"
        days = int(first)
        if days < 0:
            raise ValueError("天数不能为负")
        gs.update({"cooldown_days": days, "exclude_recent": days > 0})
        extra = "（跨群）" if gs.cooldown_cross_group else ""
        return (f"✅ 防连中冷却已设为 {days} 天{extra}。"
                if days else "✅ 已关闭防连中冷却。")

    def _h_exclude_admins(self, event, args, gs):
        if not args:
            raise ValueError("用法：抽奖 排除管理员 开|关")
        val = _on_off(args[0])
        gs.update({"exclude_admins": val})
        return f"✅ 开奖排除管理员已{'开启' if val else '关闭'}。"

    def _h_at(self, event, args, gs):
        if not args:
            raise ValueError("用法：抽奖 艾特 开|关")
        val = _on_off(args[0])
        gs.update({"at_winners": val})
        return f"✅ @中奖者已{'开启' if val else '关闭'}（不支持 @ 的平台会自动降级为文本）。"

    def _h_card(self, event, args, gs):
        if not args:
            raise ValueError("用法：抽奖 卡片 开|关")
        val = _on_off(args[0])
        gs.update({"card_enabled": val})
        if val:
            return "✅ 卡片渲染已开启。"
        return "✅ 卡片渲染已关闭，开奖以纯文本发送。"

    def _h_template(self, event, args, gs):
        if not args:
            raise ValueError("用法：抽奖 模板 恭喜<winners>中奖！<contact>")
        tpl = " ".join(args)
        if len(tpl) > 500:
            raise ValueError("模板过长（500 字以内）")
        gs.update({"template": tpl})
        return f"✅ 中奖模板已更新：\n{tpl}"

    def _h_window(self, event, args, gs):
        if not args:
            raise ValueError("用法：抽奖 窗口 7（天数）")
        n = int(args[0])
        if n < 1 or n > 365:
            raise ValueError("窗口天数需在 1-365 之间")
        gs.update({"activity_window_days": n})
        return f"✅ 活跃度统计窗口已设为近 {n} 天。"

    def _h_schedule(self, event, args, gs):
        if not args:
            raise ValueError("用法见「抽奖 帮助」：定时 关/每日/每周/双周/每月/cron ...")
        kind = args[0]
        sched = {"type": SCHED_NONE, "time": "20:00", "weekday": 0,
                 "monthday": 1, "cron": ""}
        if kind in ("关", "off", "关闭"):
            pass
        elif kind in ("每日", "daily"):
            if len(args) < 2:
                raise ValueError("用法：抽奖 定时 每日 20:00")
            sched.update(type=SCHED_DAILY, time=validate_time(args[1]))
        elif kind in ("每周", "weekly"):
            if len(args) < 3:
                raise ValueError("用法：抽奖 定时 每周 周一 20:00")
            sched.update(type=SCHED_WEEKLY, weekday=_parse_weekday(args[1]),
                         time=validate_time(args[2]))
        elif kind in ("双周", "双每周", "biweekly"):
            if len(args) < 3:
                raise ValueError("用法：抽奖 定时 双周 周一 20:00")
            sched.update(type=SCHED_BIWEEKLY, weekday=_parse_weekday(args[1]),
                         time=validate_time(args[2]))
        elif kind in ("每月", "monthly"):
            if len(args) < 3:
                raise ValueError("用法：抽奖 定时 每月 1 20:00（日期建议 1-28）")
            day = int(args[1])
            if not 1 <= day <= 28:
                raise ValueError("每月日期建议 1-28（避免部分月份无此日）")
            sched.update(type=SCHED_MONTHLY, monthday=day, time=validate_time(args[2]))
        elif kind in ("cron", "自定义"):
            if len(args) < 2:
                raise ValueError("用法：抽奖 定时 cron 0 20 * * 5（标准 5 段 cron）")
            cron = " ".join(args[1:])
            if len(cron.split()) != 5:
                raise ValueError("cron 必须是 5 段：分 时 日 月 周，例如 0 20 * * 5")
            sched.update(type=SCHED_CUSTOM, cron=cron)
            # 先校验能否构造 trigger
            try:
                from .scheduler import build_trigger
            except ImportError:  # 平铺加载
                from scheduler import build_trigger  # type: ignore

            build_trigger(sched)
        else:
            raise ValueError(f"未知定时类型：{kind}")

        gs.update({"schedule": sched})
        self.sched.sync_group(gs.umo, sched)
        if sched["type"] == SCHED_NONE:
            return "✅ 定时开奖已关闭。"
        nxt = preview_next(sched, 3)
        return "✅ 定时开奖已设置：\n" + "\n".join(f"· {x}" for x in nxt)

    def _h_schedule_preview(self, event, args, gs):
        try:
            nxt = preview_next(gs.schedule, 3)
        except ValueError as e:
            return f"⚠️ 当前定时配置无效：{e}"
        if not nxt:
            return "当前未开启定时开奖。"
        return "未来 3 次开奖时间：\n" + "\n".join(f"· {x}" for x in nxt)

    # ---------------- 开奖核心 ----------------

    async def do_draw(self, umo: str, trigger: str = "manual",
                      prizes_override=None, simulate: bool = False) -> str | None:
        """执行开奖并发送结果。返回 None=成功；str=错误信息（未发送开奖消息）。

        simulate=True：完整渲染卡片/@/模板发送，但不写中奖记录、不写开奖流水、
        不清空报名（模拟模式下强制展示卡片与 @，忽略群配置中的开关）。
        """
        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain

        gs = self.store.get(umo)
        mode = gs.mode
        prizes = prizes_override or gs.prizes
        signup_sid = None
        if mode == MODE_SIGNUP:
            signup_sid = self._sid(umo)

        try:
            candidates, weights, note = gather_candidates(
                mode=mode, settings=gs, db=self.db,
                activity=self.tracker, signup_sid=signup_sid,
            )
        except DrawError as e:
            msg = f"⚠️ 开奖失败：{e}"
            if trigger == "scheduled":
                try:
                    await self.context.send_message(
                        umo, MessageChain(chain=[Plain(msg)])
                    )
                except Exception:
                    pass
                return None
            return msg

        # 冷却排除（默认按本群；开启跨群冷却则统计所有群近期中奖者）
        cooldown = set()
        if gs.exclude_recent and gs.cooldown_days > 0:
            since = int(time.time()) - gs.cooldown_days * 86400
            cooldown = self.db.recent_winner_uids(
                None if gs.cooldown_cross_group else umo, since
            )
        admin_uids = self.db.list_admin_uids(umo) if gs.exclude_admins else set()

        result = draw(
            mode=mode,
            prizes=prizes,
            candidates=candidates,
            weights=weights,
            cooldown_uids=cooldown,
            exclude_admins=gs.exclude_admins,
            admin_uids=admin_uids,
        )

        winners_flat: list[tuple[str, str]] = []
        winner_rows = []
        for tier in result.tiers:
            for uid, name in tier.winners:
                winners_flat.append((uid, name))
                winner_rows.append((uid, name, tier.prize))

        ts = int(time.time())
        if not simulate:
            if winner_rows:
                self.db.add_winners(umo, winner_rows, ts)
            self.db.add_draw_log(
                umo, ts, mode, trigger,
                json.dumps({"note": note, "pool": result.pool_size,
                            "tiers": [{"prize": t.prize, "winners": [n for _, n in t.winners],
                                       "shortage": t.shortage} for t in result.tiers]},
                           ensure_ascii=False),
            )

            # 报名模式：清场并开启新场次
            if mode == MODE_SIGNUP and signup_sid is not None:
                self.db.clear_signups(umo, signup_sid)
                self._signup_sid[umo] = signup_sid + 1

        notes = list(result.notes)
        if note:
            notes.insert(0, note)
        if simulate:
            notes.insert(0, "本次为模拟开奖")

        # 卡片（@ 不进卡片）。模拟时强制出卡片；真实时跟随群配置。
        card_wanted = winners_flat and (gs.card_enabled or simulate)
        card_path = None
        if card_wanted:
            when = now_local().strftime("%Y-%m-%d %H:%M")
            card_path = await render_result_card(
                self,
                tier_results=result.tiers,
                group_name=group_id_of(umo),
                when_str=when,
                mode_label=MODE_LABELS.get(mode, mode),
                pool_size=result.pool_size,
                notes=notes,
                simulate=simulate,
            )

        contact = str(self.config.get("contact", "") or "")
        try:
            await send_result(
                context=self.context,
                umo=umo,
                settings=gs,
                tier_results=result.tiers,
                winners_flat=winners_flat,
                notes=notes,
                contact=contact,
                card_image_path=card_path,
                group_name=group_id_of(umo),
                simulate=simulate,
            )
        except Exception as e:
            logger.error(f"[GroupRaffle] 开奖消息发送失败: {e}")
            try:
                await self.context.send_message(
                    umo, MessageChain(chain=[Plain(f"开奖完成，但消息发送出错：{e}")])
                )
            except Exception:
                pass
        return None

    async def _scheduled_draw(self, umo: str):
        if not self._group_enabled(umo):
            return
        gs = self.store.get(umo)
        if not gs.enabled:
            return
        await self.do_draw(umo, trigger="scheduled")
