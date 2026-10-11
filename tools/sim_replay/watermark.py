"""水印相机的 Python 复刻（与 app/static/camera.js 的 wmDraw 同一模板）：
左上角面板 = logo + 蓝色项目名标题栏 + 工作地点 / 工作内容 / 拍摄时间。尺寸按 2000px 宽等比缩放。
仿真照片先按此印水印再上传，和现场用应用内相机拍的效果一致。"""
from __future__ import annotations

import io
import os
import re
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
LOGO = ROOT / "app" / "static" / "chec-logo.png"
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_B = "C:/Windows/Fonts/msyhbd.ttc"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    out, cur = [], ""
    for tok in re.findall(r"[A-Za-z0-9.,:;'’\-+#/()%~]+|\s+|[^\sA-Za-z0-9]", text or ""):
        if not cur and tok.isspace():
            continue
        if cur and draw.textlength(cur + tok, font=font) > max_w:
            out.append(cur.rstrip())
            cur = ""
            if tok.isspace():
                continue
        cur += tok
    if cur.strip():
        out.append(cur.rstrip())
    return out


def stamp(src: Path | Image.Image, when: datetime, place: str, work: str, project: str, max_edge: int = 2000) -> bytes:
    """返回带水印的 JPEG 字节；横图，长边不超过 max_edge。每次在末尾加入随机注释，保证字节不同。"""
    im = src if isinstance(src, Image.Image) else Image.open(src)
    im = im.convert("RGB")
    k = min(1.0, max_edge / max(im.size))
    if k < 1:
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    w, h = im.size
    u = w / 2000
    x0, y0 = 30 * u, 30 * u
    pw, hh, pad = min(746 * u, w * 0.5), 174 * u, 22 * u
    fs, lh, gap = 46 * u, 57 * u, 14 * u
    f_title = ImageFont.truetype(FONT_B, max(8, int(52 * u)))
    f_body = ImageFont.truetype(FONT, max(8, int(fs)))
    over = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    title = _wrap(d, project, f_title, pw - hh - 48 * u)[:2]
    lab_w = d.textlength("Work Content:", font=f_body) + 16 * u
    val_w = pw - pad * 2 - lab_w
    rows = [("Work Area:", _wrap(d, place, f_body, val_w)[:2] or [""]), ("Work Content:", _wrap(d, work, f_body, val_w)[:5] or [""])]
    body_h = sum(len(r[1]) * lh + gap for r in rows) + lh + gap + 6 * u
    ph = hh + body_h
    d.rounded_rectangle([x0, y0, x0 + pw, y0 + ph], radius=22 * u, fill=(240, 242, 246, 224))
    d.rectangle([x0 + hh, y0, x0 + pw, y0 + hh], fill=(0x63, 0x9b, 0xee, 255))
    d.rectangle([x0, y0, x0 + hh, y0 + hh], fill=(255, 255, 255, 255))
    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA").resize((int(hh - 14 * u), int(hh - 14 * u)), Image.LANCZOS)
        over.alpha_composite(logo, (int(x0 + 7 * u), int(y0 + 7 * u)))
    tl = 62 * u
    ty = y0 + hh / 2 - (len(title) - 1) * tl / 2 + 18 * u
    for i, line in enumerate(title):
        d.text((x0 + hh + 28 * u, ty + i * tl), line, font=f_title, fill=(255, 255, 255, 255), anchor="ls")
    y = y0 + hh + gap + fs * 0.95
    for key, lines in rows:
        d.text((x0 + pad, y), key, font=f_body, fill=(17, 17, 17, 255), anchor="ls")
        for i, line in enumerate(lines):
            d.text((x0 + pad + lab_w, y + i * lh), line, font=f_body, fill=(17, 17, 17, 255), anchor="ls")
        y += len(lines) * lh + gap
    lab = "拍 摄 时 间:"
    st = when.strftime("%Y.%m.%d %H:%M")
    d.text((x0 + pad, y), lab, font=f_body, fill=(17, 17, 17, 255), anchor="ls")
    d.text((x0 + pad + d.textlength(lab, font=f_body) + 14 * u, y), st, font=f_body, fill=(17, 17, 17, 255), anchor="ls")
    out = Image.alpha_composite(im.convert("RGBA"), over).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=88, comment=os.urandom(8).hex().encode())
    return buf.getvalue()
