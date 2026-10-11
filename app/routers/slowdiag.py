"""工效偏低 / 进度滞后的原因诊断：预警详情里的“慢在哪”。

- GET /api/alerts/{id}/slow-diag   该预警作业近 14 天日志信号统计 + 一句归因（AI 写、核对日志编号与数字，规则兜底）。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from ..services.slow_diag import SCHEMA  # noqa: F401  建缓存表

VIEW = frozenset({"manager", "eng_sup", "leader"})


def register(app) -> None:
    from .. import main as M
    from ..db import one
    from ..services import slow_diag as svc

    @app.get("/api/alerts/{aid}/slow-diag")
    def slow_diag(aid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        if user["role"] not in VIEW:
            raise HTTPException(403, "当前岗位不查看进度预警")
        a = one(conn, "SELECT * FROM alerts WHERE id=? AND project_id=?", (aid, user["project_id"]))
        if not a:
            raise HTTPException(404, "预警不存在")
        return svc.diagnose(conn, user["project_id"], a["activity_code"], a["activity_name"], user["id"], ai=user["role"] != "leader")
