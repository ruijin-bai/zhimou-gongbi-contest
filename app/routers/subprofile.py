"""分包综合履约画像（安全质量线的一个页签）。

- GET /api/sub-profiles   每家分包近 30 天的安全、质量、进度预警、联系单（工程主管另含周确认按时率）+ 一段 AI 评价（数字守卫，规则兜底）。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from ..services.sub_profile import SCHEMA  # noqa: F401  建缓存表


def register(app) -> None:
    from .. import main as M
    from ..services import sub_profile as svc

    @app.get("/api/sub-profiles")
    def sub_profiles(run: int = 0, user=Depends(M.current_user), conn=Depends(M.db)):
        try:
            return svc.listing(conn, user, run=bool(run))
        except PermissionError as e:
            raise HTTPException(403, str(e)) from None
