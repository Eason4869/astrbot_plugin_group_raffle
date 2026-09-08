# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle —— 卡片渲染（不依赖 astrbot_plugin_htmlrender）

优先级：AstrBot 核心内置 html_render（Jinja2 模板截图）
       → Pillow（本地绘制简版，无需浏览器）
       → None（纯文本，由 notifier 发送）
注意：真实 @ 不渲染在卡片内，@ 组件由 notifier 放在图片之外。
"""

import asyncio
import html
import os
from pathlib import Path
from typing import Optional

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _out_dir() -> str:
    try:
        try:
            from .db import _data_dir
        except ImportError:
            from db import _data_dir  # type: ignore

        d = os.path.join(_data_dir(), "cards")
    except Exception:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cards")
    os.makedirs(d, exist_ok=True)
    return d


# ---------------- 主入口 ----------------

async def render_result_card(star, tier_results, group_name, when_str,
                             mode_label, pool_size, notes=None,
                             simulate: bool = False) -> Optional[str]:
    """开奖卡片。star=插件实例（用于调用内置 html_render）。返回图片路径或 None。"""
    data = {
        "group": html.escape(group_name or "本群"),
        "when": html.escape(when_str),
        "mode_label": html.escape(mode_label),
        "pool_size": pool_size,
        "simulate": simulate,
        "title": "🧪 模拟开奖（不记录）" if simulate else "🎊 开奖结果 🎊",
        "tiers": [
            {
                "prize": t.prize,
                "winners": [n for _, n in t.winners],
                "shortage": t.shortage,
            }
            for t in tier_results
        ],
        "notes": (list(notes or []) + (["※ 模拟开奖，未记录中奖、未清空报名"] if simulate else [])),
    }
    # 1) 核心内置 html_render
    path = await _render_via_core(star, "card.html", data, width=430)
    if path:
        return path
    # 2) Pillow 兜底
    try:
        return await asyncio.to_thread(
            _render_pillow, tier_results, group_name, when_str, mode_label,
            pool_size, simulate,
        )
    except Exception:
        return None


async def render_help_image(star, rows: list[dict]) -> Optional[str]:
    """帮助命令表图片。rows: [{cmd, perm, desc}, ...]。返回图片路径或 None。"""
    data = {"rows": rows}
    path = await _render_via_core(star, "help.html", data, width=760)
    if path:
        return path
    try:
        return await asyncio.to_thread(_render_help_pillow, rows)
    except Exception:
        return None


async def _render_via_core(star, template_name: str, data: dict, width: int) -> Optional[str]:
    """调用 AstrBot 核心自带的 html_render（Jinja2）。成功返回文件路径。"""
    if star is None or not hasattr(star, "html_render"):
        return None
    try:
        tmpl = (TEMPLATES_DIR / template_name).read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        result = await star.html_render(
            tmpl,
            data,
            return_url=False,  # 让核心直接下载成 PNG 文件路径，而不是返回 URL
            options={
                "width": width,
                "full_page": True,
                "omit_background": False,
                "type": "png",
            },
        )
    except Exception:
        return None
    return _coerce_to_path(result)


def _coerce_to_path(result) -> Optional[str]:
    """html_render 可能返回本地路径或 file:// URL，统一成本地路径。"""
    if not result:
        return None
    s = str(result)
    if s.startswith("file://"):
        s = s[len("file://"):]
        s = s.lstrip("/") if os.name == "nt" and s[2:3] == ":" else "/" + s
    if os.path.isfile(s):
        return s
    # 部分实现返回 bytes / data-url，则落盘
    if isinstance(result, (bytes, bytearray)):
        p = os.path.join(_out_dir(), "card.png")
        with open(p, "wb") as f:
            f.write(result)
        return p
    return None


# ---------------- Pillow 兜底（开奖卡片） ----------------

def _find_cjk_font(size: int):
    from PIL import ImageFont

    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _wrap_text(draw, text, font, max_w):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines or [""]


def _render_pillow(tier_results, group_name, when_str, mode_label, pool_size,
                   simulate: bool = False) -> Optional[str]:
    from PIL import Image, ImageDraw

    W = 460
    pad = 28
    title_text = "🧪 模拟开奖（不记录）" if simulate else "🎊 开奖结果 🎊"
    f_title = _find_cjk_font(26 if simulate else 30)
    f_sub = _find_cjk_font(14)
    f_prize = _find_cjk_font(17)
    f_name = _find_cjk_font(21)

    tmp = Image.new("RGB", (W, 10), (255, 247, 236))
    d0 = ImageDraw.Draw(tmp)
    blocks = []
    y = 24 + 44 + 30
    for t in tier_results:
        names = "、".join(n for _, n in t.winners) or "—"
        if t.shortage:
            names += f"（缺 {t.shortage} 名）"
        wrapped = _wrap_text(d0, names, f_name, W - 2 * pad - 20)
        blocks.append((t.prize, wrapped))
        y += 18 + len(wrapped) * 30 + 18
    y += 34
    H = y

    img = Image.new("RGB", (W, H), (255, 247, 236))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 6], fill=(230, 126, 34))

    def center(cy, text, font, fill):
        w = d.textbbox((0, 0), text, font=font)
        d.text(((W - (w[2] - w[0])) / 2, cy), text, font=font, fill=fill)

    center(20, title_text, f_title, (194, 87, 26))
    center(66, f"{group_name or '本群'} · {mode_label} · 候选{pool_size}人 · {when_str}",
           f_sub, (154, 106, 58))

    y = 104
    for prize, wrapped in blocks:
        bh = 18 + len(wrapped) * 30 + 12
        d.rounded_rectangle([pad - 8, y - 6, W - pad + 8, y + bh], radius=12, fill=(255, 255, 255))
        d.text((pad + 6, y), f"【{prize}】", font=f_prize, fill=(194, 87, 26))
        y += 26
        for line in wrapped:
            d.text((pad + 6, y), line, font=f_name, fill=(61, 34, 0))
            y += 30
        y += 14
    center(H - 28, "群抽奖助手 GroupRaffle", f_sub, (176, 138, 94))

    out = os.path.join(_out_dir(), "raffle_card.png")
    img.save(out)
    return out


# ---------------- Pillow 兜底（帮助表） ----------------

def _render_help_pillow(rows: list[dict]) -> Optional[str]:
    from PIL import Image, ImageDraw

    W = 780
    pad = 20
    f_title = _find_cjk_font(26)
    f_sub = _find_cjk_font(14)
    f_head = _find_cjk_font(15)
    f_cell = _find_cjk_font(13)
    f_cmd = _find_cjk_font(13)

    col_cmd, col_perm = 250, 70
    col_desc = W - 2 * pad - col_cmd - col_perm

    tmp = Image.new("RGB", (W, 10), (255, 250, 243))
    d0 = ImageDraw.Draw(tmp)
    # 预估高度
    y = 24 + 40 + 26 + 40  # title + sub + head
    for r in rows:
        desc_lines = _wrap_text(d0, r["desc"], f_cell, col_desc - 16)
        cmd_lines = _wrap_text(d0, r["cmd"], f_cmd, col_cmd - 16)
        y += max(len(desc_lines), len(cmd_lines), 1) * 20 + 12
    H = y + 30

    img = Image.new("RGB", (W, H), (255, 250, 243))
    d = ImageDraw.Draw(img)

    def center_x(cy, text, font, fill):
        w = d.textbbox((0, 0), text, font=font)
        d.text(((W - (w[2] - w[0])) / 2, cy), text, font=font, fill=fill)

    center_x(20, "群抽奖助手 · 命令帮助", f_title, (194, 87, 26))
    center_x(56, "主命令：抽奖（英文别名 raffle，等价，如 /raffle 开奖）", f_sub, (154, 106, 58))

    # 表头
    hy = 92
    d.rectangle([pad, hy, W - pad, hy + 34], fill=(255, 233, 214))
    d.text((pad + 8, hy + 9), "命令", font=f_head, fill=(160, 74, 18))
    d.text((pad + col_cmd + 8, hy + 9), "权限", font=f_head, fill=(160, 74, 18))
    d.text((pad + col_cmd + col_perm + 8, hy + 9), "说明", font=f_head, fill=(160, 74, 18))
    y = hy + 34

    for i, r in enumerate(rows):
        desc_lines = _wrap_text(d, r["desc"], f_cell, col_desc - 16)
        cmd_lines = _wrap_text(d, r["cmd"], f_cmd, col_cmd - 16)
        rh = max(len(desc_lines), len(cmd_lines), 1) * 20 + 12
        if i % 2 == 1:
            d.rectangle([pad, y, W - pad, y + rh], fill=(255, 245, 234))
        d.rectangle([pad, y, W - pad, y + rh], outline=(240, 220, 194))
        ty = y + 8
        for cl in cmd_lines:
            d.text((pad + 8, ty), cl, font=f_cmd, fill=(176, 58, 18))
            ty += 20
        pcolor = (42, 122, 58) if r["perm"] == "所有人" else (194, 87, 26)
        d.text((pad + col_cmd + 8, y + 8), r["perm"], font=f_cell, fill=pcolor)
        ty = y + 8
        for dl in desc_lines:
            d.text((pad + col_cmd + col_perm + 8, ty), dl, font=f_cell, fill=(58, 42, 24))
            ty += 20
        y += rh

    out = os.path.join(_out_dir(), "help.png")
    img.save(out)
    return out
