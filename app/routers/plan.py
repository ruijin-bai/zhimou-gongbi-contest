"""计划深挖接口：计划体检（含体检智能体）、期间对比、计划工效对实测工效、控制节点倒计时、雨季提示。

读：进度页的岗位（项目经理、工程主管、领导）与系统管理员；运行体检智能体：progress_ops。
全部只读 P6 导入数据与最新预测，不改动 P6 计划；不涉及产值。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from ..services.p6ext import SCHEMA  # noqa: F401  p6_calendars / p6_act_ext 随 init_db 建表

READ_ROLES = frozenset({"manager", "eng_sup", "leader", "admin"})


def register(app) -> None:
    from .. import main as M
    from ..services import plan_health_agent, plan_insight, schedule

    def reader(user=Depends(M.current_user)) -> dict:
        if user["role"] not in READ_ROLES:
            raise HTTPException(403, "计划分析仅项目经理、工程主管、领导可看")
        return user

    @app.get("/api/plan/health")
    def health(user=Depends(reader), conn=Depends(M.db)):
        h = plan_insight.health(conn, user["project_id"])
        if not h.get("none"):
            ag = plan_health_agent.latest(conn, user["project_id"])
            h["agent"] = ag if ag and ag.get("import_id") == h["import_id"] else None
        return h

    @app.post("/api/plan/health/agent")
    def health_agent(user=Depends(M.need("progress_ops")), conn=Depends(M.db)):
        try:
            return plan_health_agent.run(conn, user["project_id"], user["id"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/plan/compare")
    def compare(user=Depends(reader), conn=Depends(M.db)):
        return plan_insight.compare(conn, user["project_id"])

    @app.get("/api/plan/productivity")
    def productivity(user=Depends(reader), conn=Depends(M.db)):
        return plan_insight.productivity(conn, user["project_id"])

    @app.get("/api/plan/countdown")
    def countdown(user=Depends(reader), conn=Depends(M.db)):
        return plan_insight.countdown(conn, user["project_id"], forecast=schedule.latest_forecast(conn, user["project_id"]))

    @app.get("/api/plan/gantt")
    def gantt(user=Depends(M.viewer), conn=Depends(M.db)):
        """横道图补充数据：当前批次的数据日期与各作业的目标计划日期（P6 target）；作业清单与筛选仍走 /api/p6/activities。"""
        from ..db import all_
        from ..services import p6
        imp = p6.current_import(conn, user["project_id"])
        if not imp:
            return {"none": True}
        rows = all_(conn, "SELECT code, target_start, target_finish FROM activities WHERE import_id=?", (imp["id"],))
        return {"data_date": imp["data_date"], "file": imp["file_name"],
                "baseline": {r["code"]: [r["target_start"], r["target_finish"]] for r in rows if r["target_start"] or r["target_finish"]}}

    @app.get("/api/plan/rain")
    def rain(user=Depends(reader), conn=Depends(M.db)):
        return plan_insight.rain(conn, user["project_id"])
