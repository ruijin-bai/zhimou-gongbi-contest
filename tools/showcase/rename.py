"""改名：仿真项目统一叫“仿真公路项目”（编码 SIM-ROAD），库里、素材里不再出现莱基/LEKKI 字样（RJ 2026-10-11）。

- rename_db：全库所有文本列按 NAME_MAP 逐条替换（只在演示副本上做）；
- build_assets：生成改名后的回放素材目录（随手记/单据清单改名、单据照片按新名字重画、语音原样复制）；
- restamp_media：库里已有的仿真照片按新项目名重印水印（原图取自 sim_assets），同时重做压缩件、缩略图与 sha256。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import types
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM_ASSETS = ROOT / "sim_assets"
NEW_NAME = "仿真公路项目"
NEW_CODE = "SIM-ROAD"

# 顺序有关：长的先换
NAME_MAP = [
    ("莱基深水港疏港路（仿真·旧版）", NEW_NAME + "（旧版）"),
    ("莱基深水港疏港路（仿真）", NEW_NAME),
    ("莱基深水港疏港路", NEW_NAME),
    ("莱基疏港路项目拌合站", NEW_NAME + "拌合站"),
    ("莱基疏港路", NEW_NAME),
    ("莱基半岛", "项目沿线"),
    ("莱基", "仿真"),
    ("LEKKI-SIM2", NEW_CODE),
    ("LEKKI ROAD PROJECT BATCHING PLANT", "SIM ROAD PROJECT BATCHING PLANT"),
    ("LEKKI BATCHING PLANT", "SIM ROAD BATCHING PLANT"),
    ("Lekki Coastal Steel Supply Ltd.", "Simroad Coastal Steel Supply Ltd."),
    ("LEKKI", "SIMROAD"),
    ("Lekki", "Simroad"),
    ("拉各斯泻湖商品混凝土有限公司", "滨湖商品混凝土有限公司（仿真）"),
]
BANNED = ("莱基", "LEKKI", "Lekki", "lekki", "拉各斯泻湖", "LOT1", "Lot1", "lot1")


def rename_text(s):
    if not isinstance(s, str):
        return s
    for a, b in NAME_MAP:
        if a in s:
            s = s.replace(a, b)
    return s


def rename_obj(o):
    if isinstance(o, str):
        return rename_text(o)
    if isinstance(o, list):
        return [rename_obj(x) for x in o]
    if isinstance(o, tuple):
        return tuple(rename_obj(x) for x in o)
    if isinstance(o, dict):
        return {rename_text(k): rename_obj(v) for k, v in o.items()}
    return o


def _tables(conn):
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]


def rename_db(conn) -> dict:
    """返回 {表.列: 改动行数}。"""
    stats = {}
    for t in _tables(conn):
        for _cid, col, typ, *_ in conn.execute(f'PRAGMA table_info("{t}")').fetchall():
            if typ and typ.upper() not in ("TEXT", ""):
                continue
            n = 0
            for a, b in NAME_MAP:
                cur = conn.execute(f'UPDATE "{t}" SET "{col}"=replace("{col}", ?, ?) WHERE instr("{col}", ?) > 0', (a, b, a))
                n += max(cur.rowcount, 0)
            if n:
                stats[f"{t}.{col}"] = n
    return stats


def residue(conn) -> list[str]:
    """改名后仍含莱基/LEKKI 字样的列（区分大小写）。"""
    out = []
    for t in _tables(conn):
        for _cid, col, *_ in conn.execute(f'PRAGMA table_info("{t}")').fetchall():
            for w in BANNED:
                n = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE instr(CAST("{col}" AS TEXT), ?) > 0', (w,)).fetchone()[0]
                if n:
                    out.append(f"{t}.{col} 含“{w}” {n} 行")
    return out


# ----------------------------------------------------------------------------- 素材

def _docs_render_renamed():
    """把 tools/sim_replay/docs_render.py 的源码按新名字载入（不改仓库里的文件）。"""
    src = (ROOT / "tools" / "sim_replay" / "docs_render.py").read_text("utf-8")
    mod = types.ModuleType("showcase_docs_render")
    mod.__file__ = str(ROOT / "tools" / "sim_replay" / "docs_render.py")
    exec(compile(rename_text(src), mod.__file__, "exec"), mod.__dict__)  # noqa: S102 — 仓库内自有代码，只换了名字
    return mod


def build_assets(dst: Path, seed: int = 20260907) -> Path:
    """dst/cache/{notes,docs}.json、dst/docs/*.jpg、dst/voice/*.wav；照片仍从 sim_assets/photos 读。"""
    dst = Path(dst)
    (dst / "cache").mkdir(parents=True, exist_ok=True)
    (dst / "docs").mkdir(parents=True, exist_ok=True)
    notes = json.loads((SIM_ASSETS / "cache" / "notes.json").read_text("utf-8"))
    (dst / "cache" / "notes.json").write_text(json.dumps(rename_obj(notes), ensure_ascii=False, indent=1), "utf-8")
    docs = rename_obj(json.loads((SIM_ASSETS / "cache" / "docs.json").read_text("utf-8")))
    (dst / "cache" / "docs.json").write_text(json.dumps(docs, ensure_ascii=False, indent=1), "utf-8")
    dr = _docs_render_renamed()
    for i, it in enumerate(docs):
        out = dst / "docs" / it["file"]
        if out.exists():
            continue
        img = {"concrete_ticket": dr.concrete_ticket, "material_receipt": dr.material_receipt, "cert": dr.cert}[it["kind"]](it["doc"])
        dr.as_phone_photo(img, seed + i).save(out, quality=88)
    if not (dst / "voice").exists():
        shutil.copytree(SIM_ASSETS / "voice", dst / "voice")
    return dst


# ----------------------------------------------------------------------------- 照片重印水印

def _photo_meta():
    from tools.sim_replay import scenario as S
    meta = {p[2]: {"place": p[3], "work": p[4]} for p in S.PHOTOS}
    for code, hz in S.HAZARDS.items():
        meta.setdefault(code, {"place": hz.get("place", ""), "work": "安全隐患"})
    return meta


def restamp_media(conn, media_dir: Path, assets: Path, project_id: int, name: str = NEW_NAME) -> int:
    """按 original_name 找回原图，用新项目名重印水印；文件路径不变，sha256/dhash/尺寸按新文件更新。"""
    from PIL import Image, ImageOps
    from tools.sim_replay.watermark import stamp
    from app.services.media import AI_EDGE, THUMB_EDGE, dhash
    meta = _photo_meta()
    n = 0
    rows = conn.execute("SELECT id, original_name, taken_at, uploaded_at, geo_place, file, ai_file, thumb, purpose FROM media WHERE project_id=?",
                        (project_id,)).fetchall()
    for mid, oname, taken, uploaded, place, f, af, th, purpose in rows:
        oname = oname or ""
        code = Path(oname).stem
        when = datetime.fromisoformat((taken or uploaded or "2026-09-07T12:00")[:16])
        if re.fullmatch(r"[CHMPR]\d\d", code) and (SIM_ASSETS / "photos" / f"{code}.jpg").exists():
            src = SIM_ASSETS / "photos" / f"{code}.jpg"
            m = meta.get(code) or meta.get("H" + code[1:], {})
            work = "隐患整改完成" if code.startswith("R") else m.get("work", "")
            raw = stamp(src, when, place or m.get("place", ""), work, name)
        elif (Path(assets) / "docs" / oname).exists():
            raw = stamp(Image.open(Path(assets) / "docs" / oname), when, place or "现场", "单据拍照", name)
        else:
            continue
        img = Image.open(__import__("io").BytesIO(raw))
        up = ImageOps.exif_transpose(img).convert("RGB")
        (media_dir / f).write_bytes(raw)
        ai = up.copy()
        ai.thumbnail((AI_EDGE, AI_EDGE))
        ai.save(media_dir / af, "JPEG", quality=82)
        t = up.copy()
        t.thumbnail((THUMB_EDGE, THUMB_EDGE))
        t.save(media_dir / th, "JPEG", quality=78)
        conn.execute("UPDATE media SET sha256=?, dhash=?, width=?, height=? WHERE id=?",
                     (hashlib.sha256(raw).hexdigest(), dhash(up), up.width, up.height, mid))
        n += 1
    return n
