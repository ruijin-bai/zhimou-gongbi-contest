"""演示日时钟（T-07，D8）：只读接口 GET /api/demo/clock。

演示实例由 tools/demo_serve.py 启动：设置 ZB_DEMO_DAY，并把整个进程的时钟定在演示日（time-machine）。
前端据此在页头显示“演示日 MM-DD”；普通实例返回 {"demo": false}，不显示。
不需要登录（登录页也要看得出这是演示实例）；只回报演示日、服务器的今天、调度是否在跑和 AI 用法，不含业务数据，不写库。
"""
from __future__ import annotations

import os
import threading
from datetime import date


def register(app) -> None:
    from ..config import settings
    from ..db import now_iso

    @app.get("/api/demo/clock")
    def demo_clock():
        day = os.environ.get("ZB_DEMO_DAY", "").strip()
        if not day:
            return {"demo": False}
        return {"demo": True, "day": day, "today": date.today().isoformat(), "now": now_iso(),
                "scheduler": any(t.name == "detect-scheduler" and t.is_alive() for t in threading.enumerate()),
                "ai": settings.ai_provider}
