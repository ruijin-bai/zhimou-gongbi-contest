"""竞赛演示账号的岗位切换接口（规则见 services/demo.py）。不用 from __future__ annotations：Response 在 register 内导入，注解须是真类型。"""

from pydantic import BaseModel


class AsIn(BaseModel):
    role: str


def register(app) -> None:
    from fastapi import Depends, HTTPException, Response

    from .. import main as M
    from ..db import audit
    from ..services import demo as svc

    @app.post("/api/demo/as")
    def demo_as(body: AsIn, response: Response, user=Depends(M.current_user), conn=Depends(M.db)):
        if not user.get("demo"):
            raise HTTPException(403, "只有竞赛演示账号可以切换岗位")
        if body.role not in svc.DEMO_ROLES:
            raise HTTPException(400, "只能切换为项目经理、工程主管或安全主管")
        response.set_cookie(svc.COOKIE, body.role, httponly=True, samesite="lax", max_age=14 * 86400)
        audit(conn, user["id"], "demo.as", f"user:{user['id']}", f"{user['role']} → {body.role}")
        return {"as": body.role}
