"""放行Agent 接口：未来几天将开工作业的开工/浇筑条件清单。

权限（后端强制）：
- 查看：项目经理、工程主管、安全主管、公司领导（只读）；技术员只看自己负责范围内的作业；分包安全员、系统管理员不可见（403）；
- 修改：项目经理、工程主管全部；技术员仅限自己范围内，且只能设 已具备/受阻/撤销，“不适用”、增删自定义条件、指定责任人需工程主管或项目经理；
- 所有写操作写 audit。
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from ..services import gate as svc

SCHEMA = svc.SCHEMA


class StateIn(BaseModel):
    state: str = Field(max_length=10)
    note: str = Field("", max_length=200)


class CustomIn(BaseModel):
    label: str = Field(max_length=60)


class AssignIn(BaseModel):
    owner_id: int | None = None
    due_date: str | None = Field(None, max_length=10)


def register(app) -> None:
    from fastapi import Depends, HTTPException

    from .. import main as M

    def call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except LookupError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/gate")
    def gate_list(days: int = 14, user=Depends(M.current_user), conn=Depends(M.db)):
        return call(svc.build_list, conn, user, days)

    # 固定路径必须写在 {activity_code} 之前
    @app.post("/api/gate/notify")
    def gate_notify(user=Depends(M.current_user), conn=Depends(M.db)):
        if user["role"] not in svc.ADMINS:
            raise HTTPException(403, "仅项目经理、工程主管可以发放行预警")
        return svc.notify_due(conn, user["project_id"], date.today(), actor_id=user["id"])

    @app.get("/api/gate/{activity_code}")
    def gate_detail(activity_code: str, user=Depends(M.current_user), conn=Depends(M.db)):
        return call(svc.detail, conn, user, activity_code)

    @app.put("/api/gate/{activity_code}/items/{cond_key}")
    def gate_state(activity_code: str, cond_key: str, body: StateIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return call(svc.set_state, conn, user, activity_code, cond_key, body.state, body.note)

    @app.post("/api/gate/{activity_code}/items")
    def gate_add(activity_code: str, body: CustomIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return call(svc.add_custom, conn, user, activity_code, body.label)

    @app.delete("/api/gate/{activity_code}/items/{cond_key}")
    def gate_del(activity_code: str, cond_key: str, user=Depends(M.current_user), conn=Depends(M.db)):
        return call(svc.delete_custom, conn, user, activity_code, cond_key)

    @app.put("/api/gate/{activity_code}/items/{cond_key}/owner")
    def gate_owner(activity_code: str, cond_key: str, body: AssignIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return call(svc.assign, conn, user, activity_code, cond_key, body.owner_id, body.due_date)
