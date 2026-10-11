"""各岗位首页统一待办队列接口：GET /api/todo，以及两个就地完成的动作。

只汇总现有数据（候选隐患、整改与质控、预警、放行清单、日志预审、报表草稿、例会行动项、扫码待认领等），不新增业务流程。
按当前登录角色过滤（角色一律取自会话，不接受客户端传入），权限沿用 app/roles.py 的 can() 与既有数据范围：
分包单位只见本单位条目；项目经理只留拍板项（赶工决定、升级到经理的高风险隐患、周报发布、本人的例会行动项），其余合成“需要知道：N 项”；
领导（只读）与系统管理员不参与业务决策，返回空待办。

- POST /api/todo/alerts/{id}/crash-decision：项目经理对已确认属实的红色预警做赶工决定（采纳赶工方案 / 暂不处理），
  只留痕、记审计，不派单。路由内本地校验必须是项目经理（不改 roles.py）。决定记在 alert_decisions，失效条件见 services/todo.py（N-7）。
- POST /api/todo/remind-week：工程主管提醒技术员补交某一周的进度确认，消息直达那一周（F-08）。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

# 赶工决定：每次决定一行（只增不改），以最新一行为准；level_at/slip_at/feedback_at 是决定时预警的等级、滞后天数与确认时间，用于判断决定是否已失效
SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_decisions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    alert_id INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('adopt','defer')),
    level_at TEXT NOT NULL,
    slip_at REAL,
    feedback_at TEXT,
    plan_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    decided_by INTEGER NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alert_decisions ON alert_decisions(project_id, alert_id, id);
"""


class CrashIn(BaseModel):
    decision: str
    note: str = Field("", max_length=300)


class RemindWeekIn(BaseModel):
    user_id: int
    week: str


def register(app) -> None:
    from .. import main as M
    from ..services import todo as svc

    def g(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except svc.TodoError as exc:
            raise HTTPException(exc.status, exc.msg) from None

    @app.get("/api/todo")
    def todo(user=Depends(M.current_user), conn=Depends(M.db)):
        return svc.build(conn, user)

    @app.post("/api/todo/alerts/{aid}/crash-decision")
    def crash_decision(aid: int, body: CrashIn, user=Depends(M.current_user), conn=Depends(M.db)):
        if user["role"] != "manager":
            raise HTTPException(403, "赶工决定由项目经理做")
        return g(svc.crash_decide, conn, user, aid, body.decision, body.note)

    @app.post("/api/todo/remind-week")
    def remind_week(body: RemindWeekIn, user=Depends(M.current_user), conn=Depends(M.db)):
        if user["role"] != "eng_sup":
            raise HTTPException(403, "周进度确认提醒由工程主管发")
        return g(svc.remind_week, conn, user, body.user_id, body.week)
