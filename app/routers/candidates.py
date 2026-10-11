"""候选收敛（T-05，P0-5）：AI 候选隐患按照片批量处理。AI 起草、人确认——每次提交都来自人点按钮，后端只做校验与留痕。

- `POST /api/candidates/batch`：`{items:[{id, action: confirm|onspot|dismiss, assignee_id?, due_date?, reason_code?, reason_note?,
  merge_into?, level?, category?, desc?, measure?}]}`。逐条校验 `can_decide` 与单位范围；混入非候选、已处理、跨单位或填写有误的条目时
  整批拒绝（含跨单位 403，其余 400），返回 `{"detail", "bad": [{id, reason}]}`，不部分生效；每条决定单独写审计。
  - confirm：确认并派单；不传责任人时取 `findings.default_assignee()`（F-16），算不出则要求人选；不传期限按风险等级默认。
  - onspot（D6）：当场已纠正，一键闭环入台账，标注“当场纠正、无整改照片”，计为 AI 采纳。
  - dismiss：不采纳，从选项选原因（`GET /api/candidates/reasons`），可补充说明；`merge_into` 表示并入已有的同一问题。
- `GET /api/candidates/hints?ids=1,2`：组件用的默认责任人与“可能是同一问题，并入 #N”提示（AC-5.4），只给有权决定的候选。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class BatchItem(BaseModel):
    id: int
    action: str
    assignee_id: int | None = None
    due_date: str | None = None
    reason_code: str | None = None
    reason_note: str | None = Field(None, max_length=200)
    merge_into: int | None = None
    level: str | None = None
    category: str | None = Field(None, max_length=40)
    desc: str | None = Field(None, max_length=200)
    measure: str | None = Field(None, max_length=200)


class BatchIn(BaseModel):
    items: list[BatchItem]


def register(app) -> None:
    from .. import main as M
    from ..services import findings as svc

    async def _rejected(_, exc: svc.BatchRejected):
        return JSONResponse({"detail": str(exc), "bad": exc.bad}, status_code=exc.status)

    app.add_exception_handler(svc.BatchRejected, _rejected)

    def _decider(user):
        if user["role"] == "leader":
            raise HTTPException(403, "领导账号只读")
        if not (svc.R.can(user["role"], "site_safety") or user["role"] == "sub_safety"):
            raise HTTPException(403, "只有本单位安全员或安全主管可以处理候选隐患")

    @app.post("/api/candidates/batch")
    def candidates_batch(body: BatchIn, user=Depends(M.current_user), conn=Depends(M.db)):
        _decider(user)
        return svc.decide_batch(conn, user, [i.model_dump() for i in body.items])

    @app.get("/api/candidates/hints")
    def candidates_hints(ids: str = "", user=Depends(M.current_user), conn=Depends(M.db)):
        _decider(user)
        try:
            idl = [int(x) for x in ids.split(",") if x.strip()][:svc.BATCH_MAX]
        except ValueError:
            raise HTTPException(400, "ids 格式不对") from None
        return {"items": svc.hints(conn, user, idl)}

    @app.get("/api/candidates/reasons")
    def candidates_reasons(user=Depends(M.current_user)):
        return [{"code": k, "label": v} for k, v in svc.DISMISS_REASONS.items()]
