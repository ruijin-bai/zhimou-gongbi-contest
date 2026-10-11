"""生成 Vercel 演示版的种子库 vercel_seed/zhimou.db（全部为仿真数据，不读取 data/ 业务库）。

用法：python tools/build_vercel_seed.py [--password 演示密码]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", default="zhimou-demo-2026")
    args = ap.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="zb_seed_"))
    os.environ.update(ZB_DATA_DIR=str(scratch), ZB_SCHEDULER="0", ZB_ENV_FILE=str(scratch / "none.env"), ZB_AI_PROVIDER="mock")
    sys.path.insert(0, str(ROOT))

    from app import sim
    from app.db import connect, init_db

    init_db()
    res = sim.build(None, password=args.password)
    sim.run_agents(res["project_id"])
    conn = connect()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()

    out = ROOT / "vercel_seed"
    out.mkdir(exist_ok=True)
    shutil.copyfile(scratch / "zhimou.db", out / "zhimou.db")
    print(f"已生成 {out / 'zhimou.db'}；账号 {', '.join(res['accounts'])}，密码 {args.password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
