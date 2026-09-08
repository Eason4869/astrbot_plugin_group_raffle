# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle —— 消息构造与发送

规则（与产品约定一致）：
- @ 开启且平台支持时：使用真实 At 组件，并置于消息【文首】（每个 At 后跟空格，
  参考 astrbot_plugin_bilibili 的写法）；文本里的 <winners> 不再写成 "@名字"（避免假 @）。
- 平台不支持真实 @ 时：降级为文本昵称（<winners> 替换为 @名字）。
- 卡片开启时：只发卡片图片（中奖结果在图片内），不再追加文字版；
  真实 @ 独立于卡片之外（卡片图片前的 At）。
- 发送失败：若为图片(整体)发送失败，重试“真实@ + 纯文本”；再失败则纯文本兜底。
"""

from typing import Optional

try:
    from .activity import TZ
except ImportError:  # 平铺加载
    from activity import TZ  # type: ignore


def _components_module():
    """优先公开 API 路径，失败回退核心内部路径。"""
    try:
        from astrbot.api import message_components as mc
    except Exception:
        try:
            from astrbot.core.message import components as mc
        except Exception:
            mc = None
    return mc


def _result_cls():
    try:
        from astrbot.core.message.message_event_result import MessageEventResult
    except Exception:
        try:
            from astrbot.api.message_event_result import MessageEventResult
        except Exception:
            MessageEventResult = None
    return MessageEventResult


# 支持真实 @ 的平台前缀（aiocqhttp=OneBot；覆盖 NapCat/Lagrange/LLOneBot 等）
AT_CAPABLE_PLATFORMS = ("aiocqhttp",)


def platform_of(umo: str) -> str:
    return umo.split(":", 1)[0] if umo else ""


def group_id_of(umo: str) -> str:
    parts = umo.split(":")
    return parts[2] if len(parts) >= 3 else (parts[-1] if parts else umo)


def can_at(umo: str) -> bool:
    return platform_of(umo) in AT_CAPABLE_PLATFORMS


def build_at_parts(uid_list: list, mc=None) -> list:
    """构造 [At(qq), Plain(" "), ...] 置于文首。mc 缺失则返回空。"""
    mc = mc or _components_module()
    if mc is None:
        return []
    try:
        At = mc.At
        Plain = mc.Plain
    except Exception:
        return []
    parts = []
    for uid, _ in uid_list:
        try:
            parts.append(At(qq=str(uid)))
        except Exception:
            try:
                parts.append(At(user_id=str(uid)))
            except Exception:
                continue
        parts.append(Plain(" "))
    return parts


def render_template(template: str, winners_text: str, count: int, group_name: str,
                    contact: str, when=None) -> str:
    import datetime

    when = when or datetime.datetime.now(TZ)
    return (
        (template or "")
        .replace("<winners>", winners_text)
        .replace("<count>", str(count))
        .replace("<group>", group_name or "本群")
        .replace("<time>", when.strftime("%Y-%m-%d %H:%M"))
        .replace("<contact>", contact or "")
    )


def build_text(tier_lines: list, body: str, notes: list,
               simulate: bool = False) -> str:
    head = "🧪 模拟开奖（结果不会记录）🧪" if simulate else "🎊 开奖结果 🎊"
    parts = [head, ""]
    parts.extend(tier_lines)
    if body:
        parts.append("")
        parts.append(body)
    if notes:
        parts.append("")
        parts.extend(f"（{n}）" for n in notes)
    if simulate:
        parts.append("")
        parts.append("※ 本次为模拟开奖：未记录中奖、未清空报名，真实抽奖不受影响。")
    return "\n".join(parts).strip()


def _image_component(path: str, mc=None):
    mc = mc or _components_module()
    if mc is None:
        return None
    Image = getattr(mc, "Image", None)
    if Image is None:
        return None
    try:
        return Image.fromFileSystem(path)
    except Exception:
        pass
    try:
        return Image(file=path)  # type: ignore[call-arg]
    except Exception:
        return None


def _wrap_chain(parts: list):
    """包成 context.send_message 可用的结果对象。"""
    cls = _result_cls()
    if cls is not None:
        try:
            return cls(chain=parts)
        except Exception:
            pass
    return parts


async def send_result(
    *,
    context,
    umo: str,
    settings,
    tier_results: list,  # list[TierResult]
    winners_flat: list[tuple[str, str]],  # [(uid, name)]
    notes: list[str],
    contact: str,
    card_image_path: Optional[str] = None,
    group_name: str = "",
    simulate: bool = False,
):
    """发送开奖消息。返回 (used_at: bool, used_card: bool)。"""
    from astrbot.core.message.components import Plain as _CorePlain

    mc = _components_module()
    Plain = getattr(mc, "Plain", _CorePlain)

    tier_lines = []
    for t in tier_results:
        names = "、".join(n for _, n in t.winners) or "（未抽出）"
        line = f"【{t.prize}】{names}"
        if t.shortage:
            line += f"（缺 {t.shortage} 名）"
        tier_lines.append(line)

    # 模拟时始终展示 @ 效果（若平台支持），真实时跟随群配置
    at_enabled = settings.at_winners if not simulate else True
    want_at = bool(at_enabled) and bool(winners_flat)
    do_real_at = want_at and can_at(umo)

    # <winners> 占位符：真实@时用纯名字，否则用 @名字 文本降级（避免假 @ 与真 @ 并存）
    if do_real_at:
        winners_display = "、".join(n for _, n in winners_flat)
    else:
        winners_display = "、".join(f"@{n}" for _, n in winners_flat)
    body = render_template(
        settings.template,
        winners_display,
        len(winners_flat),
        group_name or group_id_of(umo),
        contact,
    )
    text = build_text(tier_lines, body, notes, simulate=simulate)

    at_parts = build_at_parts(winners_flat, mc) if do_real_at else []

    # ---- 卡片开启：只发卡片（真实@置于文首），不追加文字版 ----
    if card_image_path:
        img = _image_component(card_image_path, mc)
        if img is not None:
            parts = list(at_parts) + [img]
            try:
                await context.send_message(umo, _wrap_chain(parts))
                return (do_real_at, True)
            except Exception:
                # 图片发送失败：回退 真实@+纯文本（仍保证送达）
                fallback_parts = list(at_parts) + [Plain(text)]
                try:
                    await context.send_message(umo, _wrap_chain(fallback_parts))
                    return (do_real_at, False)
                except Exception:
                    pass
                try:
                    await context.send_message(umo, _wrap_chain([Plain(text)]))
                except Exception:
                    pass
                return (False, False)
        # 有卡路径但组件构造失败 → 落到纯文本
        parts = list(at_parts) + [Plain(text)]
        try:
            await context.send_message(umo, _wrap_chain(parts))
            return (do_real_at, False)
        except Exception:
            pass
        try:
            await context.send_message(umo, _wrap_chain([Plain(text)]))
        except Exception:
            pass
        return (False, False)

    # ---- 无卡片：真实@（文首）+ 文字版 ----
    parts = list(at_parts) + [Plain(text)]
    try:
        await context.send_message(umo, _wrap_chain(parts))
        return (do_real_at, False)
    except Exception:
        pass
    try:
        await context.send_message(umo, _wrap_chain([Plain(text)]))
        return (False, False)
    except Exception:
        return (False, False)
