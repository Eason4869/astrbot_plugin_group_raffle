# -*- coding: utf-8 -*-
"""群抽奖助手 GroupRaffle —— 卡片渲染（三级降级）

优先级：astrbot_plugin_htmlrender（HTML 截图，最好看）
       → Pillow（本地绘制简版卡片，无需浏览器）
       → None（纯文本，由 notifier 发送）
注意：真实 @ 不渲染在卡片内，@ 组件由 notifier 放在图片之外。
"""

import asyncio
import html
import os
from typing import Optional


async def render_card(
    *,
    tier_results: list,
    group_name: str,
    when_str: str,
    mode_label: str,
    pool_size: int,
) -> Optional[str]:
    """返回图片文件路径；全部失败返回 None。"""
    # 1) htmlrender
    try:
        path = await _render_html(tier_results, group_name, when_str, mode_label, pool_size)
        if path:
            return path
    except Exception:
        pass
    # 2) Pillow（同步绘制放线程池）
    try:
        return await asyncio.to_thread(
            _render_pillow, tier_results, group_name, when_str, mode_label, pool_size
        )
    except Exception:
        return None


# ---------------- htmlrender ----------------

def _tier_rows(tier_results) -> str:
    rows = []
    for t in tier_results:
        names = "、".join(html.escape(n) for _, n in t.winners) or "—"
        rows.append(
            f'<div class="tier"><div class="prize">{html.escape(t.prize)}</div>'
            f'<div class="names">{names}'
            + (f'<span class="short">（缺 {t.shortage} 名）</span>' if t.shortage else "")
            + "</div></div>"
        )
    return "\n".join(rows)


async def _render_html(tier_results, group_name, when_str, mode_label, pool_size) -> Optional[str]:
    md = None
    try:
        import astrbot_plugin_htmlrender  # noqa: F401
        from astrbot_plugin_htmlrender import (
            md_to_pic,
            html_to_pic,
            template_to_pic,
            text_to_pic,
        )  # noqa: F401
        md = html_to_pic
    except Exception:
        try:
            from astrbot_plugin_htmlrender.render import html_to_pic as md  # type: ignore
        except Exception:
            return None

    page = f"""
    <div style="width:420px;padding:24px 28px;font-family:'PingFang SC','Microsoft YaHei',sans-serif;
                background:linear-gradient(160deg,#fff7ec 0%,#ffe9d6 55%,#ffd9b8 100%);
                border-radius:18px;color:#5a3210;">
      <div style="text-align:center;font-size:26px;font-weight:800;letter-spacing:2px;">🎊 开奖结果 🎊</div>
      <div style="text-align:center;font-size:13px;color:#9a6a3a;margin:6px 0 14px;">
        {html.escape(group_name or '本群')} · {html.escape(mode_label)} · 候选 {pool_size} 人 · {html.escape(when_str)}
      </div>
      {_tier_rows(tier_results)}
      <div style="text-align:center;font-size:12px;color:#b08a5e;margin-top:14px;">群抽奖助手 GroupRaffle</div>
    </div>
    <style>
      .tier{{background:rgba(255,255,255,.65);border-radius:12px;padding:10px 14px;margin:8px 0;}}
      .prize{{font-size:15px;font-weight:700;color:#c2571a;margin-bottom:4px;}}
      .names{{font-size:18px;font-weight:800;color:#3d2200;line-height:1.5;word-break:break-all;}}
      .short{{font-size:12px;color:#b05a2a;font-weight:400;}}
    </style>
    """
    try:
        out_path = os.path.join(_out_dir(), "raffle_card.png")
        data = await md(page, wait=2)  # html_to_pic(html: str, wait: int) -> bytes
        if isinstance(data, bytes):
            with open(out_path, "wb") as f:
                f.write(data)
            return out_path
        if isinstance(data, str) and data:
            return data
    except Exception:
        return None
    return None


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


# ---------------- Pillow ----------------

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


def _render_pillow(tier_results, group_name, when_str, mode_label, pool_size) -> Optional[str]:
    from PIL import Image, ImageDraw

    W = 460
    pad = 28
    f_title = _find_cjk_font(30)
    f_sub = _find_cjk_font(15)
    f_prize = _find_cjk_font(17)
    f_name = _find_cjk_font(22)

    # 先量高度
    tmp = Image.new("RGB", (W, 10), (255, 247, 236))
    d0 = ImageDraw.Draw(tmp)
    y = 24
    line_h = 34
    y += 44  # 标题
    y += 26  # 副标题
    blocks = []
    for t in tier_results:
        names = "、".join(n for _, n in t.winners) or "—"
        if t.shortage:
            names += f"（缺 {t.shortage} 名）"
        # 名字可能需要换行（简单按 16 字折行）
        wrapped = []
        s = names
        while s:
            wrapped.append(s[:16])
            s = s[16:]
        blocks.append((t.prize, wrapped))
        y += 20 + len(wrapped) * line_h + 16
    y += 30
    H = y + 10

    img = Image.new("RGB", (W, H), (255, 247, 236))
    d = ImageDraw.Draw(img)
    # 顶部装饰条
    d.rectangle([0, 0, W, 6], fill=(230, 126, 34))

    def center_text(cy, text, font, fill):
        bbox = d.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        d.text(((W - w) / 2, cy), text, font=font, fill=fill)

    center_text(20, "🎊 开奖结果 🎊", f_title, (194, 87, 26))
    sub = f"{group_name or '本群'} · {mode_label} · 候选{pool_size}人 · {when_str}"
    center_text(66, sub, f_sub, (154, 106, 58))

    y = 100
    for prize, wrapped in blocks:
        d.rounded_rectangle([pad - 8, y - 6, W - pad + 8, y + 14 + len(wrapped) * line_h],
                            radius=12, fill=(255, 255, 255))
        d.text((pad + 6, y), f"【{prize}】", font=f_prize, fill=(194, 87, 26))
        y += 26
        for line in wrapped:
            d.text((pad + 6, y), line, font=f_name, fill=(61, 34, 0))
            y += line_h
        y += 16
    center_text(H - 30, "群抽奖助手 GroupRaffle", f_sub, (176, 138, 94))

    out = os.path.join(_out_dir(), "raffle_card.png")
    img.save(out)
    return out
