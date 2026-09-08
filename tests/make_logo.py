# -*- coding: utf-8 -*-
"""生成插件 logo.png（一次性脚本，产物在插件根目录）。"""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logo.png")


def font(size, bold=True):
    for p in [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角方形渐变背景（橙色系，模拟锦鲤/抽奖氛围）
for y in range(S):
    t = y / S
    r = int(255 * (1 - t) + 240 * t)
    g = int(140 * (1 - t) + 90 * t)
    b = int(60 * (1 - t) + 40 * t)
    for_ = (r, g, b, 255)
    d.line([(0, y), (S, y)], fill=for_)

# 圆角遮罩
mask = Image.new("L", (S, S), 0)
md = ImageDraw.Draw(mask)
md.rounded_rectangle([0, 0, S - 1, S - 1], radius=96, fill=255)
bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bg.paste(img, (0, 0), mask)
img = bg
d = ImageDraw.Draw(img)

# 中心圆形奖盘
cx, cy, R = S // 2, S // 2 + 10, 150
d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=(255, 248, 235, 255),
         outline=(194, 87, 26, 255), width=10)

# “奖” 字
f_jiang = font(170)
text = "奖"
bbox = d.textbbox((0, 0), text, font=f_jiang)
w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
d.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1] - 6), text,
       font=f_jiang, fill=(194, 87, 26, 255))

# 四角星光点缀
def star(x, y, r, color=(255, 255, 255, 255)):
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.42
        pts.append((x + rad * math.cos(ang), y + rad * math.sin(ang)))
    d.polygon(pts, fill=color)

star(96, 104, 34)
star(418, 120, 26, (255, 240, 210, 255))
star(118, 400, 22, (255, 240, 210, 255))
star(408, 396, 32)

img.save(OUT)
print("LOGO:", OUT, os.path.getsize(OUT), "bytes")
