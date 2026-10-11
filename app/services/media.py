"""现场照片：原件不可改写（sha256 留证），另生成缩略图与发给模型的压缩件。

压缩件去除 EXIF（含 GPS），只把像素发给交融，满足最小数据发送原则。
手机定位（经纬度、精度、地点/部位）另存为结构化字段，随原件留在本地库，不发给模型。
"""
from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta
from pathlib import Path

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from ..config import settings
from ..db import insert, now_iso, one

MAX_UPLOAD = 25 * 1024 * 1024
AI_EDGE = 1280
THUMB_EDGE = 420
PURPOSES = ("log", "safety", "rectify", "ticket", "conceal", "talk")
NO_SAFETY_SCAN = ("ticket", "conceal", "talk")   # talk：现场圈讨论组的照片，只是交流，不做隐患识别


class MediaError(ValueError):
    pass


def parse_geo(lat, lon, acc=None, place: str | None = None) -> dict | None:
    """手机定位入库前校验：经纬度须在有效范围，精度缺失不补零；只有地点文字时保留文字；都没有返回 None。"""
    place = (place or "").strip()[:30]
    try:
        la, lo = float(lat), float(lon)
        ok = -90 <= la <= 90 and -180 <= lo <= 180 and not (la == 0 and lo == 0)
    except (TypeError, ValueError):
        ok = False
    if not ok:
        return {"lat": None, "lon": None, "acc": None, "place": place} if place else None
    try:
        ac = float(acc) if acc not in (None, "") else None
    except (TypeError, ValueError):
        ac = None
    if ac is not None and not (0 <= ac <= 100000):
        ac = None
    return {"lat": round(la, 6), "lon": round(lo, 6), "acc": round(ac) if ac is not None else None, "place": place}


def _gps_from_exif(img: Image.Image) -> dict | None:
    try:
        gps = img.getexif().get_ifd(ExifTags.IFD.GPSInfo)
        def deg(v):
            d, m, sec = (float(x) for x in v)
            return d + m / 60 + sec / 3600
        la, lo = deg(gps[2]), deg(gps[4])
        la = -la if str(gps.get(1, "N")).upper().startswith("S") else la
        lo = -lo if str(gps.get(3, "E")).upper().startswith("W") else lo
        return parse_geo(la, lo)
    except Exception:  # noqa: BLE001 — 没有或损坏的 GPS 信息按“无定位”处理
        return None


def _exif(img: Image.Image) -> tuple[str | None, bool]:
    try:
        exif = img.getexif()
    except Exception:  # noqa: BLE001 — 损坏的 EXIF 不影响照片入库
        return None, False
    taken = None
    sub = exif.get_ifd(ExifTags.IFD.Exif) if hasattr(ExifTags, "IFD") else {}
    raw = sub.get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
    if raw:
        try:
            taken = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").isoformat(timespec="minutes")
        except ValueError:
            taken = None
    has_gps = bool(exif.get_ifd(ExifTags.IFD.GPSInfo)) if hasattr(ExifTags, "IFD") else False
    return taken, has_gps


def dhash(img: Image.Image) -> str:
    """64 位差值哈希：识别同一张照片被重复/跨日使用（压缩、缩放后仍相近）。"""
    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = small.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return f"{bits:016x}"


def hamming(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _taken_hint(hint: str | None) -> str | None:
    """水印相机重绘的图没有 EXIF，用前端随文件带来的本地拍摄时间；非法或未来时间不采信。"""
    try:
        t = datetime.fromisoformat((hint or "").strip()[:16])
    except ValueError:
        return None
    return t.isoformat(timespec="minutes") if t <= datetime.now() + timedelta(minutes=10) else None


def save_photo(conn, project_id: int, user_id: int, filename: str, raw: bytes, purpose: str = "log",
               taken_hint: str | None = None, geo: dict | None = None, scan: bool = True) -> dict:
    """scan=False：拍摄者选择只上传不识别，记为 skipped，不进安全Agent队列（不会被后台补扫/批量重识别捡走）。"""
    if not raw:
        raise MediaError("空文件")
    if len(raw) > MAX_UPLOAD:
        raise MediaError("单张照片不超过 25MB")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise MediaError("无法识别的图片格式（支持 JPG/PNG/WEBP/HEIC 转出的 JPG）") from None
    if img.width * img.height > 60_000_000:
        raise MediaError("图片像素过大")
    taken_at, has_gps = _exif(img)
    taken_at = taken_at or _taken_hint(taken_hint)
    geo = geo or _gps_from_exif(img)
    sha = hashlib.sha256(raw).hexdigest()
    existing = one(conn, "SELECT * FROM media WHERE project_id=? AND sha256=? AND uploader_id=?", (project_id, sha, user_id))
    if existing:
        if scan and existing["safety_status"] == "skipped" and purpose in ("log", "safety"):  # 同一张照片这次选了识别
            conn.execute("UPDATE media SET safety_status='pending' WHERE id=?", (existing["id"],))
            return one(conn, "SELECT * FROM media WHERE id=?", (existing["id"],))
        return existing
    day_dir = Path(str(project_id)) / datetime.now().strftime("%Y%m")
    abs_dir = settings.media_dir / day_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(img.format or "", ".jpg")
    orig = day_dir / f"{sha}{ext}"
    if not (settings.media_dir / orig).exists():
        (settings.media_dir / orig).write_bytes(raw)
    upright = ImageOps.exif_transpose(img).convert("RGB")
    ai_img = upright.copy()
    ai_img.thumbnail((AI_EDGE, AI_EDGE))
    ai_rel = day_dir / f"{sha}_ai.jpg"
    ai_img.save(settings.media_dir / ai_rel, "JPEG", quality=82)  # 新建图像不带 EXIF
    th = upright.copy()
    th.thumbnail((THUMB_EDGE, THUMB_EDGE))
    th_rel = day_dir / f"{sha}_t.jpg"
    th.save(settings.media_dir / th_rel, "JPEG", quality=78)
    media_id = insert(conn, "media", {
        "project_id": project_id, "uploader_id": user_id, "sha256": sha, "file": str(orig), "ai_file": str(ai_rel),
        "thumb": str(th_rel), "original_name": (filename or "")[:120], "width": upright.width, "height": upright.height,
        "taken_at": taken_at, "has_gps": 1 if has_gps else 0, "uploaded_at": now_iso(),
        "geo_lat": geo["lat"] if geo else None, "geo_lon": geo["lon"] if geo else None, "geo_acc": geo["acc"] if geo else None,
        "geo_place": geo["place"] if geo else "", "dhash": dhash(upright), "purpose": purpose if purpose in PURPOSES else "log",
        # 票据/隐蔽验收照片不进安全Agent识别队列
        "safety_status": "na" if purpose in NO_SAFETY_SCAN else "pending" if scan else "skipped",
    })
    return one(conn, "SELECT * FROM media WHERE id=?", (media_id,))


def geo_of(row: dict) -> dict | None:
    """照片/随手记行里的定位；坐标与地点都没有时返回 None（前端显示“未取得定位”，不补默认值）。"""
    place = row.get("geo_place") or ""
    if row.get("geo_lat") is None or row.get("geo_lon") is None:
        return {"lat": None, "lon": None, "acc": None, "place": place} if place else None
    return {"lat": row["geo_lat"], "lon": row["geo_lon"], "acc": row.get("geo_acc"), "place": place}


def public(m: dict) -> dict:
    """uploaded_at 是服务器收到的时间：比拍摄时间（taken_at）晚 10 分钟以上的，前端标“补传”。"""
    return {"geo": geo_of(m), "id": m["id"], "thumb": f"/api/media/{m['id']}/thumb", "url": f"/api/media/{m['id']}/file",
            "taken_at": m.get("taken_at"), "uploaded_at": m.get("uploaded_at"), "name": m.get("original_name"), "sha256": m["sha256"][:12],
            "log_id": m.get("log_id"), "purpose": m.get("purpose"), "safety_status": m.get("safety_status")}
