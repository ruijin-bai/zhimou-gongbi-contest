"""资料欠账（部位档案页）：完工作业缺隐蔽验收记录、已用未复试。

- GET /api/doc-debt   完工作业的资料欠账清单（确定性规则，不调用模型）；
  一键起草资料补齐通知走工笔文书：POST /api/gongbi/docs {kind: "docdebt_notice", ref: "<作业代码>|<单位>"}，工程主管确认后才下发。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException


def register(app) -> None:
    from .. import main as M
    from ..services import doc_debt as svc

    @app.get("/api/doc-debt")
    def doc_debt(user=Depends(M.current_user), conn=Depends(M.db)):
        if user["role"] not in svc.ROLES:
            raise HTTPException(403, "资料欠账只给工程主管")
        return svc.build(conn, user)
