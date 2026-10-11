"""两周高风险作业安全预控（安全主管首页卡片）。

- GET /api/precontrol   未来 14 天开工的高风险作业 + 负责单位近 30 天同类隐患 + 一句预控重点（AI 写、数字守卫，规则兜底）；
  一键起草预控提醒走工笔文书：POST /api/gongbi/docs {kind: "precontrol_notice", ref: "<作业代码>|<单位>"}，安全主管确认后才下发。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from ..services.precontrol import SCHEMA  # noqa: F401  建缓存表


def register(app) -> None:
    from .. import main as M
    from ..services import features
    from ..services import precontrol as svc

    @app.get("/api/precontrol")
    def precontrol_card(user=Depends(M.current_user), conn=Depends(M.db)):
        if user["role"] not in svc.ROLES:
            raise HTTPException(403, "高风险作业预控只给安全主管")
        if not features.enabled(conn, user["project_id"], "gate"):
            return {"items": [], "days": svc.DAYS, "source": "rule", "note": "放行功能已关闭"}
        return svc.card(conn, user)
