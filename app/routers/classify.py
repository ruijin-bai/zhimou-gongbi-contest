"""随手记归类建议接口（技术员）。AI 只建议；人点“是”才转入既有流程生成草稿，点“不是”维持随手记。

- GET  /api/notes/classify?ids=1,2   今天（或指定）随手记的建议，一次取全，纯照片的视觉分类在后台进行（真实模型下）；
- POST /api/notes/{id}/classify      单条建议（同上规则）；
- POST /api/notes/{id}/classify/accept   {part?, template_key?, media_ids?}：转入草稿；缺部位/模板时返回 need 由前端就地补填；
- POST /api/notes/{id}/classify/dismiss  “不是”：记一笔，不再提示。
"""
from __future__ import annotations

from fastapi import BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS note_classify (
    note_id INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    vision_status TEXT,
    vision_kind TEXT,
    vision_reason TEXT,
    vision_run_id INTEGER,
    vision_at TEXT,
    decision TEXT CHECK (decision IN ('accepted','dismissed')),
    decided_kind TEXT,
    target_type TEXT,
    target_id INTEGER,
    decided_at TEXT,
    created_at TEXT NOT NULL
);
"""


class ManualIn(BaseModel):
    kind: str = Field(..., max_length=20)


class AcceptIn(BaseModel):
    kind: str | None = Field(None, max_length=20)
    part: str = Field("", max_length=40)
    template_key: str = Field("", max_length=40)
    media_ids: list[int] = []


def register(app) -> None:
    from .. import main as M
    from ..db import connect
    from ..services import classify as svc
    from ..services import tickets as ticket_svc

    def tech(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.ROLES:
            raise HTTPException(403, "仅技术员使用随手记")
        return user

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except svc.ClassifyError as exc:
            raise HTTPException(exc.status, exc.msg) from None

    def _vision_bg(nid: int) -> None:
        svc.vision_classify(nid)

    def _scan_bg(ids: list[int], uid: int) -> None:
        M._safety_bg(ids, uid)

    def _public(items: list[dict], bg: BackgroundTasks) -> list[dict]:
        out = []
        for it in items:
            if it.pop("need_vision", False):
                bg.add_task(_vision_bg, it["note_id"])
            out.append(it)
        return out

    @app.get("/api/notes/classify")
    def classify_batch(bg: BackgroundTasks, ids: str = "", user=Depends(tech), conn=Depends(M.db)):
        want = [int(x) for x in ids.split(",") if x.strip().isdigit()] if ids.strip() else None
        return {"items": _public(svc.batch(conn, user, want), bg)}

    @app.post("/api/notes/{nid}/classify")
    def classify_manual(nid: int, body: ManualIn, user=Depends(tech), conn=Depends(M.db)):
        """人工重新归类：技术员选类别后返回建议，仍要点“是”才转入（随手记页“归类”）。"""
        return guard(svc.manual, conn, user, nid, body.kind)

    @app.post("/api/notes/{nid}/classify/accept")
    def classify_accept(nid: int, body: AcceptIn, bg: BackgroundTasks, user=Depends(tech), conn=Depends(M.db)):
        r = guard(svc.accept, conn, user, nid, body.model_dump())
        todo = r.pop("bg", {}) or {}
        if todo.get("ocr"):
            bg.add_task(ticket_svc.run_ocr, todo["ocr"])
        if todo.get("scan"):
            bg.add_task(_scan_bg, todo["scan"], user["id"])
        return r

    @app.post("/api/notes/{nid}/classify/dismiss")
    def classify_dismiss(nid: int, user=Depends(tech), conn=Depends(M.db)):
        return guard(svc.dismiss, conn, user, nid)
