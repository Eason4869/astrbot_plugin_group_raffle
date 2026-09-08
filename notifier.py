# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle —— 消息构造与发送

- 模板占位符：<winners> <count> <group> <time> <contact>
- @ 能力按平台降级：OneBot 系支持 At 组件；不支持时（或发送失败）降级为纯文本昵称。
- 卡片是一张图片；真实 @ 只放在卡片外的消息链里。
"""

import datetime
from typing import Optional

try:
    from .activity import TZ
except ImportError:  # 平铺加载
    from activity import TZ  # type: ignore

# 支持真实 @ 的平台前缀（aiocqhttp=OneBot；aiocqhttp 也覆盖 NapCat/Lagrange 等）
AT_CAPABLE_PLATFORMS = ("aiocqhttp",)


def platform_of(umo: str) -> str:
    return umo.split(":", 1)[0] if umo else ""


def group_id_of(umo: str) -> str:
    parts = umo.split(":")
    return parts[2] if len(parts) >= 3 else (parts[-1] if parts else umo)


def can_at(umo: str) -> bool:
    return platform_of(umo) in AT_CAPABLE_PLATFORMS


def _at_component(uid: str):
    """尽力构造 At 组件，失败返回 None。"""
    try:
        from astrbot.core.message.components import At

        try:
            return At(qq=str(uid))
        except Exception:
            pass
        try:
            return At(user_id=str(uid))
        except Exception:
            pass
    except Exception:
        pass
    try:  # 兜底：个别版本组件路径不同
        from astrbot.api.message_components import At  # type: ignore

        return At(qq=str(uid))
    except Exception:
        return None


def render_template(template: str, winners_text: str, count: int, group_name: str,
                    contact: str, when: Optional[datetime.datetime] = None) -> str:
    when = when or datetime.datetime.now(TZ)
    return (
        (template or "")
        .replace("<winners>", winners_text)
        .replace("<count>", str(count))
        .replace("<group>", group_name or "本群")
        .replace("<time>", when.strftime("%Y-%m-%d %H:%M"))
        .replace("<contact>", contact or "")
    )


def build_text(tier_lines: list[str], body: str, notes: list[str],
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
    from astrbot.core.message.components import Plain, Image

    tier_lines = []
    for t in tier_results:
        names = "、".join(n for _, n in t.winners) or "（未抽出）"
        line = f"【{t.prize}】{names}"
        if t.shortage:
            line += f"（缺 {t.shortage} 名）"
        tier_lines.append(line)

    winners_text = "、".join(f"@{n}" for _, n in winners_flat)
    body = render_template(
        settings.template,
        winners_text,
        len(winners_flat),
        group_name or group_id_of(umo),
        contact,
    )
    text = build_text(tier_lines, body, notes, simulate=simulate)

    # 模拟时始终展示 @ 效果（若平台支持），真实时跟随群配置
    at_enabled = settings.at_winners if not simulate else True
    want_at = bool(at_enabled) and bool(winners_flat)
    do_at = want_at and can_at(umo)

    chain: list = []
    if card_image_path:
        try:
            chain.append(Image.fromFileSystem(card_image_path))
        except Exception:
            try:
                chain.append(Image(file=card_image_path))  # type: ignore[call-arg]
            except Exception:
                chain = []
    chain.append(Plain(text))
    if do_at:
        for uid, _ in winners_flat:
            at = _at_component(uid)
            if at is not None:
                chain.append(at)

    try:
        await context.send_message(umo, chain)
        return (do_at, bool(card_image_path))
    except Exception:
        # 整体发送失败：去掉 @ 与图片，纯文本兜底
        fallback = [Plain(text)]
        if want_at and do_at:
            # @ 失败时把昵称写进文本已包含 @name；再补发提示不必要
            pass
        await context.send_message(umo, fallback)
        return (False, False)
