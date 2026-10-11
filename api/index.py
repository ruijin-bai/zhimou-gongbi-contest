"""Vercel 入口：演示版。数据库每次冷启动从 vercel_seed/ 复制到 /tmp（实例内可写，实例间不共享）。"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = Path(os.environ.get("ZB_DATA_DIR") or "/tmp/zhimou")
os.environ["ZB_DATA_DIR"] = str(DATA)
os.environ.setdefault("ZB_SCHEDULER", "0")  # serverless 无常驻线程
if not os.environ.get("ZB_SECRET"):
    # 各实例须用同一密钥，会话令牌才能跨实例通用；正式演示请在 Vercel 环境变量里设 ZB_SECRET
    seed = os.environ.get("VERCEL_PROJECT_ID", "") + os.environ.get("VERCEL_URL", "zhimou")
    os.environ["ZB_SECRET"] = hashlib.sha256(("zhimou-demo|" + seed).encode()).hexdigest()

DATA.mkdir(parents=True, exist_ok=True)
if not (DATA / "zhimou.db").is_file():
    shutil.copyfile(ROOT / "vercel_seed" / "zhimou.db", DATA / "zhimou.db")

from app.main import app  # noqa: E402,F401
