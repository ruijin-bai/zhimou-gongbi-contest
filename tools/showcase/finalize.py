"""收尾：项目改名（仿真公路项目 / SIM-ROAD）、照片按新项目名重印水印、演示日改为 10-05、做残留与一致性自检。

用法（停掉连同一数据目录的服务）：
  python -m tools.showcase.finalize --data-dir data_showcase [--day 2026-10-05] [--assets 目录]
素材目录默认现生成到 <data-dir>/_assets（单据按新名字重画，随后用于重印水印）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data_showcase"))
    ap.add_argument("--day", default="2026-10-05")
    ap.add_argument("--assets", default="")
    a = ap.parse_args()
    data_dir = Path(a.data_dir).resolve()
    if data_dir == (ROOT / "data").resolve() or not (data_dir / "DEMO_COPY.json").exists():
        sys.exit("只能对演示副本运行")
    os.environ["ZB_DATA_DIR"] = str(data_dir)
    sys.path.insert(0, str(ROOT))
    from tools.showcase import rename as R
    assets = Path(a.assets) if a.assets else data_dir / "_assets"
    R.build_assets(assets)
    conn = sqlite3.connect(data_dir / "zhimou.db", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    pid = conn.execute("SELECT id FROM projects WHERE code LIKE 'DEMO%' OR code=?", (R.NEW_CODE,)).fetchone()[0]
    # 主项目编码改名（旧版项目若在也一并按文本替换）
    stats = R.rename_db(conn)
    conn.execute("COMMIT")
    print("改名涉及列", len(stats))
    # 照片重印
    from app.config import settings
    n = R.restamp_media(conn, settings.media_dir, assets, pid)
    print("重印水印照片", n)
    # 演示日
    mk = data_dir / "DEMO_COPY.json"
    d = json.loads(mk.read_text("utf-8"))
    d["day"] = a.day
    d["projects"] = [r[0] for r in conn.execute("SELECT code FROM projects ORDER BY id")]
    d["showcase"] = True
    mk.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    left = R.residue(conn)
    print("残留字样：", left or "无")
    conn.close()


if __name__ == "__main__":
    main()
