"""仿真单据照片：按给定字段画出混凝土小票 / 材料进场单 / 质量证明书，再做成“手机横拍”的样子。

票面上的每个数字都来自调用方给的字段（仿真的真实值），材料Agent识别后由仿真脚本按真实值核对。
只用 Pillow，不依赖 numpy。
"""
from __future__ import annotations

import os
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONTS = Path("C:/Windows/Fonts")
F_SONG = str(FONTS / "simsun.ttc")
F_HEI = str(FONTS / "simhei.ttf")
F_HAND = str(FONTS / "STXINGKA.TTF")  # 签名用行楷
F_PEN = str(FONTS / "Inkfree.ttf")    # 手写英文/数字


def _noise(w: int, h: int) -> Image.Image:
    """均匀随机噪点（Pillow 的 effect_noise 会出横纹）。"""
    return Image.frombytes("L", (w, h), os.urandom(w * h)).convert("RGB")


def _f(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _paper(w: int, h: int, tint=(250, 248, 240)) -> Image.Image:
    return Image.new("RGB", (w, h), tint)


def _table(d: ImageDraw.ImageDraw, x0: int, y0: int, rows: list[tuple[str, str]], colw: tuple[int, int], rh: int, font, vfont, hand: set[str] = frozenset()):
    for i, (k, v) in enumerate(rows):
        y = y0 + i * rh
        d.rectangle([x0, y, x0 + colw[0], y + rh], outline=(40, 40, 40), width=2)
        d.rectangle([x0 + colw[0], y, x0 + colw[0] + colw[1], y + rh], outline=(40, 40, 40), width=2)
        d.text((x0 + 10, y + rh // 2), k, font=font, fill=(30, 30, 30), anchor="lm")
        if k in hand:
            d.text((x0 + colw[0] + 16, y + rh // 2), v, font=_f(F_HAND, int(rh * 0.62)), fill=(25, 40, 120), anchor="lm")
        else:
            d.text((x0 + colw[0] + 12, y + rh // 2), v, font=vfont, fill=(20, 20, 20), anchor="lm")
    return y0 + len(rows) * rh


def concrete_ticket(t: dict) -> Image.Image:
    """t: supplier, ticket_no, vehicle, issued_at, part, grade, qty, slump, temp, receiver, remark, plant_en"""
    W, H = 900, 1180
    img = _paper(W, H)
    d = ImageDraw.Draw(img)
    d.text((W // 2, 60), t["supplier"], font=_f(F_HEI, 40), fill=(20, 20, 20), anchor="mm")
    d.text((W // 2, 105), t.get("plant_en", "READY-MIX CONCRETE DELIVERY TICKET"), font=_f(F_SONG, 22), fill=(40, 40, 40), anchor="mm")
    d.text((W // 2, 160), "混凝土发货单  Delivery Ticket", font=_f(F_HEI, 36), fill=(20, 20, 20), anchor="mm")
    d.text((60, 215), f"No. {t['ticket_no']}", font=_f(F_SONG, 30), fill=(180, 20, 20), anchor="lm")
    rows = [("工程名称 Project", "莱基深水港疏港路"), ("浇筑部位 Part", t["part"]), ("强度等级 Grade", t["grade"]),
            ("本车方量 Qty(m³)", f"{t['qty']:g}"), ("累计方量 Cum.(m³)", f"{t['cum']:g}"), ("坍落度 Slump(mm)", f"{t['slump']:g}"),
            ("出站时间 Dispatch", t["issued_at"]), ("车号 Truck No.", t["vehicle"]), ("司机 Driver", t.get("driver", "")),
            ("签收人 Received", t["receiver"]), ("备注 Remark", t.get("remark", ""))]
    y = _table(d, 50, 250, rows, (300, 500), 72, _f(F_SONG, 26), _f(F_SONG, 30), hand={"签收人 Received", "司机 Driver"})
    d.text((60, y + 40), "到场温度(℃)：", font=_f(F_SONG, 26), fill=(30, 30, 30), anchor="lm")
    d.text((260, y + 40), f"{t['temp']:g}", font=_f(F_PEN, 40), fill=(25, 40, 120), anchor="lm")
    d.text((60, y + 90), "白联：存根  红联：施工单位  黄联：司机", font=_f(F_SONG, 20), fill=(90, 90, 90), anchor="lm")
    # 盖章
    stamp = Image.new("RGBA", (220, 220), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp)
    sd.ellipse([8, 8, 212, 212], outline=(200, 30, 30, 170), width=6)
    sd.text((110, 110), "发货专用章", font=_f(F_HEI, 30), fill=(200, 30, 30, 170), anchor="mm")
    stamp = stamp.rotate(random.uniform(-25, 25), expand=False)
    img.paste(stamp, (W - 290, y - 230), stamp)
    return img


def material_receipt(t: dict) -> Image.Image:
    """t: supplier, ticket_no, vehicle, issued_at, material, spec, qty, unit, batch_no, receiver, remark"""
    W, H = 1000, 1100
    img = _paper(W, H, (247, 247, 250))
    d = ImageDraw.Draw(img)
    d.text((W // 2, 70), "材料进场验收单", font=_f(F_HEI, 44), fill=(20, 20, 20), anchor="mm")
    d.text((W // 2, 120), "MATERIAL RECEIVING NOTE", font=_f(F_SONG, 24), fill=(40, 40, 40), anchor="mm")
    d.text((60, 180), f"单号 No.: {t['ticket_no']}", font=_f(F_SONG, 28), fill=(20, 20, 20), anchor="lm")
    d.text((W - 60, 180), f"日期: {t['issued_at']}", font=_f(F_SONG, 28), fill=(20, 20, 20), anchor="rm")
    rows = [("供货单位 Supplier", t["supplier"]), ("材料名称 Material", t["material"]), ("规格型号 Spec", t["spec"]),
            ("数量 Qty", f"{t['qty']:g}"), ("单位 Unit", t["unit"]), ("炉批号 Heat/Batch No.", t["batch_no"]),
            ("车号 Truck No.", t["vehicle"]), ("使用部位 For", t.get("part", "")), ("外观检查 Visual", t.get("visual", "合格")),
            ("验收人 Received", t["receiver"]), ("备注 Remark", t.get("remark", ""))]
    _table(d, 50, 220, rows, (330, 570), 70, _f(F_SONG, 26), _f(F_SONG, 30), hand={"验收人 Received", "外观检查 Visual"})
    return img


def cert(t: dict) -> Image.Image:
    """t: supplier, ticket_no(证书号), issued_at, material, spec, batch_no, qty, unit, items[(项目, 标准, 实测)]"""
    W, H = 1000, 1250
    img = _paper(W, H, (252, 252, 252))
    d = ImageDraw.Draw(img)
    d.text((W // 2, 70), t["supplier"], font=_f(F_HEI, 36), fill=(20, 20, 20), anchor="mm")
    d.text((W // 2, 125), "产品质量证明书  MILL TEST CERTIFICATE", font=_f(F_HEI, 34), fill=(20, 20, 20), anchor="mm")
    head = [("证书编号 Cert No.", t["ticket_no"]), ("产品名称 Product", t["material"]), ("牌号规格 Grade/Size", t["spec"]),
            ("炉批号 Heat No.", t["batch_no"]), ("重量 Weight", f"{t['qty']:g} {t['unit']}"), ("签发日期 Date", t["issued_at"])]
    y = _table(d, 50, 180, head, (330, 570), 64, _f(F_SONG, 25), _f(F_SONG, 28))
    d.text((60, y + 40), "力学性能 Mechanical Properties", font=_f(F_HEI, 28), fill=(20, 20, 20), anchor="lm")
    y += 70
    cols = (300, 300, 300)
    for i, row in enumerate([("项目 Item", "标准 Std.", "实测 Result")] + list(t["items"])):
        x = 50
        for j, cell in enumerate(row):
            d.rectangle([x, y, x + cols[j], y + 60], outline=(40, 40, 40), width=2)
            d.text((x + 12, y + 30), cell, font=_f(F_HEI if i == 0 else F_SONG, 25), fill=(20, 20, 20), anchor="lm")
            x += cols[j]
        y += 60
    d.text((60, y + 50), "结论 Conclusion：符合 GB/T 1499.2 要求  Conforms", font=_f(F_SONG, 26), fill=(20, 20, 20), anchor="lm")
    stamp = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp)
    sd.ellipse([8, 8, 232, 232], outline=(200, 30, 30, 160), width=6)
    sd.text((120, 120), "质检专用章", font=_f(F_HEI, 32), fill=(200, 30, 30, 160), anchor="mm")
    img.paste(stamp.rotate(random.uniform(-20, 20)), (W - 320, y - 40), stamp.rotate(random.uniform(-20, 20)))
    return img


def as_phone_photo(doc: Image.Image, seed: int, out_w: int = 1600, out_h: int = 1200) -> Image.Image:
    """把单据放到桌面/车头/安全帽旁的背景上，轻微透视、光照不均、噪点和轻微模糊，横拍 4:3。"""
    rng = random.Random(seed)
    bg_col = rng.choice([(96, 78, 60), (70, 72, 76), (120, 104, 84), (58, 64, 58)])
    bg = Image.new("RGB", (out_w, out_h), bg_col)
    bg = Image.blend(bg, _noise(out_w, out_h), 0.12)
    # 单据缩放到画面高度的 ~92%
    s = out_h * rng.uniform(0.86, 0.95) / doc.height
    dw, dh = int(doc.width * s), int(doc.height * s)
    doc = doc.resize((dw, dh), Image.LANCZOS)
    # 透视：四角随机内收
    j = lambda a: rng.uniform(-a, a)
    quad = (j(30), j(25), j(25), dh + j(25), dw + j(30), dh + j(25), dw + j(30), j(25))
    doc_t = doc.transform((dw, dh), Image.QUAD, quad, Image.BICUBIC, fillcolor=bg_col)
    mask = Image.new("L", (dw, dh), 0)
    ImageDraw.Draw(mask).polygon([(0, 0), (0, dh), (dw, dh), (dw, 0)], fill=255)
    mask = mask.transform((dw, dh), Image.QUAD, quad, Image.BICUBIC)
    x = out_w - dw - rng.randint(30, 110)   # 靠右放，左上角留给水印
    y = (out_h - dh) // 2 + rng.randint(-20, 20)
    shadow = Image.new("RGB", (dw, dh), (20, 20, 20))
    bg.paste(shadow, (x + 14, y + 16), mask.filter(ImageFilter.GaussianBlur(14)))
    bg.paste(doc_t, (x, y), mask)
    bg = bg.rotate(rng.uniform(-3, 3), resample=Image.BICUBIC, fillcolor=bg_col)
    # 光照不均：一侧渐暗
    grad = Image.linear_gradient("L").resize((out_w, out_h))
    if rng.random() < 0.5:
        grad = ImageOps.mirror(grad.rotate(90, expand=False))
    dark = Image.new("RGB", (out_w, out_h), (0, 0, 0))
    k = rng.uniform(0.18, 0.32)
    bg = Image.composite(dark, bg, grad.point(lambda v: int(v * k)))
    bg = Image.blend(bg, _noise(out_w, out_h), 0.04)
    return bg.filter(ImageFilter.GaussianBlur(rng.uniform(0.4, 0.9)))


if __name__ == "__main__":  # 样张
    out = Path(__file__).resolve().parent.parent.parent / "sim_assets" / "docs_sample"
    out.mkdir(parents=True, exist_ok=True)
    t = {"supplier": "拉各斯泻湖商品混凝土有限公司", "plant_en": "LAGOON READYMIX LTD.  LEKKI BATCHING PLANT", "ticket_no": "LR-0917-0236", "vehicle": "LND 482 XA",
         "issued_at": "2026-09-17 15:42", "part": "立交桥 3# 桩基 (水下)", "grade": "C30 水下", "qty": 8.0, "cum": 24.0, "slump": 200, "temp": 29,
         "driver": "Adebayo", "receiver": "钱志远", "remark": "加缓凝剂"}
    as_phone_photo(concrete_ticket(t), 1).save(out / "ticket_sample.jpg", quality=88)
    r = {"supplier": "Lekki Coastal Steel Supply Ltd.", "ticket_no": "MR-20260915-03", "vehicle": "KJA 731 BC", "issued_at": "2026-09-15",
         "material": "热轧带肋钢筋", "spec": "HRB400E Φ25", "qty": 32.46, "unit": "t", "batch_no": "25H0912-117", "part": "立交桥桩基钢筋笼", "receiver": "钱志远"}
    as_phone_photo(material_receipt(r), 2).save(out / "receipt_sample.jpg", quality=88)
    c = dict(r, ticket_no="QC-2509-4471", items=[("屈服强度 MPa", "≥400", "455"), ("抗拉强度 MPa", "≥540", "612"), ("断后伸长率 %", "≥16", "21.5"), ("最大力总伸长率 %", "≥9", "12.8")])
    as_phone_photo(cert(c), 3).save(out / "cert_sample.jpg", quality=88)
    print(out)
