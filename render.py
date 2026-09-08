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
import time
import uuid
from pathlib import Path
from typing import Optional

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _uniq_name(base: str) -> str:
    """每次渲染生成唯一文件名，避免并发覆盖旧文件（覆盖可能使上传读到损坏/截断文件）。"""
    return f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}_{base}"


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
        "title": "模拟开奖（结果不记录）" if simulate else "开奖结果",
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
    # 结果卡片优先用 Pillow 本地绘制：布局完全可控（居中横幅+等次块），
    # 避免 html_render 在某些环境下输出“内容挤在左上角”的整页截图。
    # Pillow 不可用时才回退核心内置 html_render。
    try:
        p = await asyncio.to_thread(
            _render_pillow, tier_results, group_name, when_str, mode_label,
            pool_size, simulate, data["notes"],
        )
        if p:
            return p
    except Exception:
        pass
    # 兜底：核心内置 html_render
    path = await _render_via_core(star, "card.html", data, width=520)
    if path:
        return path
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


async def render_info_card(star, title: str, subtitle: str,
                           sections: list[dict]) -> Optional[str]:
    """通用信息卡片（状态 / 名单等），与帮助卡同一视觉风格。

    sections: [{heading: str|None, items: [str, ...]}, ...]
    返回图片路径或 None。
    """
    safe = [
        {
            "heading": html.escape(str(s.get("heading") or "")),
            "items": [html.escape(str(x)) for x in (s.get("items") or [])],
        }
        for s in sections
    ]
    data = {
        "title": html.escape(title),
        "subtitle": html.escape(subtitle or ""),
        "sections": safe,
    }
    path = await _render_via_core(star, "info.html", data, width=480)
    if path:
        return path
    try:
        return await asyncio.to_thread(_render_info_pillow, title, subtitle, sections)
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
        p = os.path.join(_out_dir(), _uniq_name("card.png"))
        with open(p, "wb") as f:
            f.write(result)
        return p
    return None


# ---------------- Pillow 兜底（开奖卡片） ----------------

def _find_cjk_font(size: int, bold: bool = False):
    from PIL import ImageFont

    if bold:
        candidates = [
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\msyh.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ]
    else:
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


def _grad_banner(draw_box, c_top, c_bottom, radius):
    """生成一张竖向渐变的圆角横幅图层（RGBA）。"""
    from PIL import Image, ImageDraw

    x0, y0, x1, y1 = draw_box
    w = int(x1 - x0)
    h = int(y1 - y0)
    grad = Image.new("RGBA", (w, h), c_top + (255,))
    px = grad.load()
    for yy in range(h):
        t = yy / max(h - 1, 1)
        r = int(c_top[0] + (c_bottom[0] - c_top[0]) * t)
        g = int(c_top[1] + (c_bottom[1] - c_top[1]) * t)
        b = int(c_top[2] + (c_bottom[2] - c_top[2]) * t)
        for xx in range(w):
            px[xx, yy] = (r, g, b, 255)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def _draw_crown(d, cx, cy, color):
    """在横幅标题上方画一个简约皇冠（纯几何图形，不依赖 emoji）。"""
    w, h = 40, 26
    x0, y0 = cx - w // 2, cy - h // 2
    pts = [
        (x0, y0 + h),          # 左下
        (x0, y0 + 9),
        (x0 + 10, y0 + 15),
        (x0 + w // 2, y0),
        (x0 + w - 10, y0 + 15),
        (x0 + w, y0 + 9),
        (x0 + w, y0 + h),
    ]
    d.polygon(pts, fill=color)
    # 三点圆珠
    for px_, py_ in ((x0, y0 + 9), (x0 + w // 2, y0), (x0 + w, y0 + 9)):
        d.ellipse([px_ - 3, py_ - 3, px_ + 3, py_ + 3], fill=color)
    d.rectangle([x0, y0 + h - 6, x0 + w, y0 + h + 1], fill=color)


# 等次奖牌配色：金 / 银 / 铜 / 暖橙（之后循环）
_BADGE_COLORS = [
    ((234, 179, 8), (255, 247, 224)),    # 金
    ((148, 158, 170), (244, 246, 248)),  # 银
    ((184, 115, 51), (250, 238, 224)),   # 铜
    ((230, 126, 34), (255, 243, 230)),   # 橙
]


def _render_pillow(tier_results, group_name, when_str, mode_label, pool_size,
                   simulate: bool = False, notes: Optional[list] = None) -> Optional[str]:
    """Pillow 绘制开奖卡片：暖色渐变 + 渐变横幅 + 奖牌等次 + 中奖者胶囊排版。

    全程不使用 emoji（中文字体无彩色 emoji 会渲染成方框），装饰用几何图形。
    """
    from PIL import Image, ImageDraw

    notes = list(notes or [])
    W = 820
    px = 44                     # 左右内边距
    MARGIN = 24                 # 卡片外边距（给圆角/描边留位）

    # ---- 配色：真实开奖=暖橙金；模拟=沉稳蓝（一眼区分） ----
    if simulate:
        BANNER_TOP, BANNER_BOT = (74, 111, 165), (45, 72, 120)
        BADGE_HEAD = (74, 111, 165)
    else:
        BANNER_TOP, BANNER_BOT = (245, 158, 11), (214, 94, 24)
        BADGE_HEAD = (230, 126, 34)
    BG_TOP, BG_BOT = (255, 251, 245), (253, 244, 233)
    INK = (61, 42, 22)
    SUB_INK = (147, 110, 72)
    GOLD = (193, 142, 62)
    LINE = (240, 224, 198)
    WHITE = (255, 255, 255)

    title_text = "模拟开奖（结果不记录）" if simulate else "开奖结果"

    f_title = _find_cjk_font(44, bold=True)
    f_banner_sub = _find_cjk_font(21)
    f_div = _find_cjk_font(20, bold=True)
    f_prize = _find_cjk_font(27, bold=True)
    f_name = _find_cjk_font(32, bold=True)
    f_rank = _find_cjk_font(22, bold=True)
    f_short = _find_cjk_font(19)
    f_note = _find_cjk_font(18)
    f_foot = _find_cjk_font(17)

    content_w = W - 2 * px

    # ========== 预测量，计算总高 ==========
    tmp = Image.new("RGB", (W, 10), BG_TOP)
    d0 = ImageDraw.Draw(tmp)

    def text_w(s, f):
        b = d0.textbbox((0, 0), s, font=f)
        return b[2] - b[0]

    # 先把每个等次的中奖者拆成行（胶囊布局）
    GAP = 14           # 胶囊间距
    CHIP_PAD = 16      # 胶囊左右内边距
    H = 0              # 占位
    tiers_layout = []
    y = 0
    for idx, t in enumerate(tier_results):
        raw_names = "、".join(n for _, n in t.winners)
        names = [n for _, n in t.winners]
        # 胶囊换行排版
        chip_ws = [text_w(n, f_name) + 2 * CHIP_PAD for n in names]
        lines = []
        cur = []
        cur_w = 0
        max_line_w = content_w - 56
        for n, cw in zip(names, chip_ws):
            add = cw + (GAP if cur else 0)
            if cur and cur_w + add > max_line_w:
                lines.append(cur)
                cur, cur_w = [n], cw
            else:
                cur.append(n)
                cur_w += add
        if cur:
            lines.append(cur)
        if not names:
            lines = []
        tiers_layout.append((t, names, lines))

    banner_h = 158
    cursor = MARGIN + banner_h + 34          # 横幅底 → 分隔标题
    cursor += 44                              # “中奖名单”分隔行
    # 等次卡片
    BLOCK_GAP = 20
    block_heights = []
    for t, names, lines in tiers_layout:
        name_area_h = (len(lines) * (52 + 12)) if lines else 52
        bh = 26 + 46 + 18 + name_area_h + 26  # 顶部留白+奖牌行+间距+名单+底部
        block_heights.append(bh)
        cursor += bh + BLOCK_GAP
    cursor += 6
    # 备注区
    note_lines = []
    for n in notes:
        wrapped = _wrap_text(d0, str(n), f_note, content_w - 56)
        note_lines.extend(wrapped)
    notes_h = 0
    if note_lines:
        notes_h = 22 + len(note_lines) * 30 + 18
        cursor += notes_h + 10
    cursor += 24
    foot_h = 40
    H = cursor + foot_h

    # ========== 正式绘制 ==========
    img = Image.new("RGBA", (W, H), BG_TOP + (255,))
    # 背景竖向暖色渐变
    bg_grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bp = bg_grad.load()
    for yy in range(H):
        tt = yy / max(H - 1, 1)
        r = int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * tt)
        g = int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * tt)
        b = int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * tt)
        for xx in range(W):
            bp[xx, yy] = (r, g, b, 255)
    img.alpha_composite(bg_grad)
    d = ImageDraw.Draw(img)

    # 外描边（细金边）
    d.rounded_rectangle([6, 6, W - 7, H - 7], radius=26, outline=(233, 209, 168), width=2)

    # ---- 顶部渐变横幅 ----
    bx0, bx1 = MARGIN, W - MARGIN
    by0, by1 = MARGIN, MARGIN + banner_h
    img.alpha_composite(
        _grad_banner((bx0, by0, bx1, by1), BANNER_TOP, BANNER_BOT, 24),
        (bx0, by0),
    )
    d = ImageDraw.Draw(img)

    def center(text, font, cy, fill, cx=W // 2):
        w = text_w(text, font)
        d.text((cx - w / 2, cy), text, font=font, fill=fill)

    # 皇冠 + 标题 + 副标题（无独立品牌行，整体垂直居中）
    _draw_crown(d, W // 2, by0 + 40, (255, 236, 190))
    center(title_text, f_title, by0 + 64, WHITE)
    sub = f"{group_name or '本群'}  ·  {mode_label}  ·  候选 {pool_size} 人  ·  {when_str}"
    center(sub, f_banner_sub, by1 - 42, (255, 238, 214))

    # ---- 分隔标题：— 中奖名单 — ----
    y = by1 + 28
    div = "中 奖 名 单"
    dw = text_w(div, f_div)
    dline_y = y + 14
    d.line([(px + 30, dline_y), (W // 2 - dw // 2 - 24, dline_y)], fill=(226, 196, 150), width=2)
    d.line([(W // 2 + dw // 2 + 24, dline_y), (W - px - 30, dline_y)], fill=(226, 196, 150), width=2)
    center(div, f_div, y, BADGE_HEAD)

    # ---- 等次卡片 ----
    y = y + 50
    for idx, (t, names, lines) in enumerate(tiers_layout):
        bh = block_heights[idx]
        # 卡片投影
        d.rounded_rectangle([px + 3, y + 5, W - px + 3, y + bh + 5], radius=20,
                            fill=(0, 0, 0, 18))
        # 卡片底
        d.rounded_rectangle([px, y, W - px, y + bh], radius=20, fill=WHITE)
        d.rounded_rectangle([px, y, W - px, y + bh], radius=20, outline=LINE, width=2)
        # 左侧奖牌色条
        accent = _BADGE_COLORS[idx % len(_BADGE_COLORS)][0]
        d.rounded_rectangle([px, y, px + 8, y + bh], radius=20, fill=accent)

        # 奖牌圆徽 + 等次名
        badge_cy = y + 48
        medal_cx = px + 44
        d.ellipse([medal_cx - 21, badge_cy - 21, medal_cx + 21, badge_cy + 21], fill=accent)
        rank_txt = str(idx + 1)
        rw = text_w(rank_txt, f_rank)
        d.text((medal_cx - rw / 2, badge_cy - 15), rank_txt, font=f_rank, fill=WHITE)

        d.text((px + 80, badge_cy - 19), str(t.prize), font=f_prize, fill=(58, 38, 16))
        cnt = len(names)
        cnt_txt = f"{cnt} 名"
        cw = text_w(cnt_txt, f_short)
        d.text((W - px - 24 - cw, badge_cy - 13), cnt_txt, font=f_short, fill=SUB_INK)

        # 中奖者胶囊
        ny = y + 26 + 46 + 18
        if lines:
            for li, line_names in enumerate(lines):
                row_w = sum(text_w(n, f_name) + 2 * CHIP_PAD for n in line_names) + GAP * (len(line_names) - 1)
                cx = px + 28
                chip_h = 52
                for n in line_names:
                    cwid = text_w(n, f_name) + 2 * CHIP_PAD
                    fillc = _BADGE_COLORS[idx % len(_BADGE_COLORS)][1]
                    edgec = _BADGE_COLORS[idx % len(_BADGE_COLORS)][0]
                    d.rounded_rectangle([cx, ny, cx + cwid, ny + chip_h], radius=16,
                                        fill=fillc, outline=edgec, width=1)
                    d.text((cx + CHIP_PAD, ny + (chip_h - 32) / 2 - 2), n,
                           font=f_name, fill=INK)
                    cx += cwid + GAP
                ny += chip_h + 12
        else:
            d.text((px + 28, ny), "本轮无人中奖", font=f_name, fill=SUB_INK)
            ny += 52

        if t.shortage:
            st = f"名额未抽满，缺 {t.shortage} 名（候选不足）"
            sw = text_w(st, f_short)
            d.rounded_rectangle([W - px - 24 - sw - 24, ny - 4, W - px - 24, ny + 30],
                                radius=12, fill=(253, 237, 227), outline=(238, 196, 160))
            d.text((W - px - 24 - sw - 12, ny + 2), st, font=f_short, fill=(184, 92, 36))

        y += bh + BLOCK_GAP

    # ---- 备注区 ----
    if note_lines:
        nh = notes_h
        d.rounded_rectangle([px, y, W - px, y + nh], radius=16,
                            fill=(255, 248, 238), outline=(236, 214, 178), width=1)
        ny = y + 22
        for ln in note_lines:
            d.text((px + 24, ny), "· " + ln if not ln.startswith("·") else ln,
                   font=f_note, fill=(150, 108, 60))
            ny += 30
        y += nh + 10

    # ---- 页脚 ----
    foot = "群抽奖助手 GroupRaffle"
    fw = text_w(foot, f_foot)
    d.text((W // 2 - fw / 2, H - 34), foot, font=f_foot, fill=(190, 160, 120))

    out = os.path.join(_out_dir(), _uniq_name("raffle_card.png"))
    img.convert("RGB").save(out, quality=95)
    return out


# ---------------- Pillow 兜底（信息卡：状态/名单） ----------------

def _render_info_pillow(title: str, subtitle: str,
                        sections: list[dict]) -> Optional[str]:
    from PIL import Image, ImageDraw

    W = 480
    pad = 26
    f_title = _find_cjk_font(26)
    f_sub = _find_cjk_font(13)
    f_head = _find_cjk_font(16)
    f_item = _find_cjk_font(15)

    tmp = Image.new("RGB", (W, 10), (255, 250, 243))
    d0 = ImageDraw.Draw(tmp)

    # 预估高度
    blocks = []
    y = 20 + 40 + 10 + (24 if subtitle else 0)
    for s in sections:
        lines = []
        head = s.get("heading")
        head_h = 30 if head else 8
        y += head_h
        for it in (s.get("items") or []):
            wrapped = _wrap_text(d0, "· " + it, f_item, W - 2 * pad - 24)
            lines.append(wrapped)
            y += len(wrapped) * 24 + 4
        blocks.append((head, lines))
        y += 12
    H = y + 34

    img = Image.new("RGB", (W, H), (255, 250, 243))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 6], fill=(230, 126, 34))

    def center_x(cy, text, font, fill):
        w = d.textbbox((0, 0), text, font=font)
        d.text(((W - (w[2] - w[0])) / 2, cy), text, font=font, fill=fill)

    center_x(18, title, f_title, (194, 87, 26))
    cy = 52
    if subtitle:
        center_x(cy, subtitle, f_sub, (154, 106, 58))
        cy = 76
    y = cy + 6
    for head, lines in blocks:
        if head:
            d.rounded_rectangle([pad - 10, y - 4, W - pad + 10, y + 27], radius=8,
                                fill=(255, 233, 214))
            d.text((pad, y + 2), head, font=f_head, fill=(160, 74, 18))
            y += 36
        else:
            y += 6
        for wrapped in lines:
            for line in wrapped:
                d.text((pad + 8, y), line, font=f_item, fill=(58, 42, 24))
                y += 24
            y += 4
        y += 10
    center_x(H - 28, "群抽奖助手 GroupRaffle", f_sub, (176, 138, 94))

    out = os.path.join(_out_dir(), _uniq_name("info.png"))
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

    out = os.path.join(_out_dir(), _uniq_name("help.png"))
    img.save(out)
    return out
