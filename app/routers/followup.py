"""随手记追问一句（技术员）。程序判断缺什么、给候选；交融模型只把问句说顺，校验不过用规则问句。

- GET  /api/notes/followup?ids=1,2          今天随手记的追问状态（首次判断时建行；真实模型下润色在后台）；
- POST /api/notes/{id}/followup/answer      {code?, qty?, unit?, text?}：答案以“（补充：…）”写回同一条随手记；
- POST /api/notes/{id}/followup/dismiss     忽略：不扣分，周确认时再处理。
"""
from __future__ import annotations

from fastapi import BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS note_followup (
    note_id INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    gaps_json TEXT NOT NULL DEFAULT '{}',
    question TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('pending','asked','answered','dismissed')),
    source TEXT NOT NULL DEFAULT 'rule',
    note TEXT,
    ai_run_id INTEGER,
    answer_json TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_note_followup_day ON note_followup(user_id, day);
"""


class AnswerIn(BaseModel):
    code: str = Field("", max_length=40)
    qty: float | None = None
    unit: str = Field("", max_length=6)
    text: str = Field("", max_length=200)


def register(app) -> None:
    from .. import main as M
    from ..services import followup as svc

    def tech(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.ROLES:
            raise HTTPException(403, "仅技术员使用随手记")
        return user

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except svc.FollowupError as exc:
            raise HTTPException(exc.status, exc.msg) from None

    @app.get("/api/notes/followup")
    def followup_batch(bg: BackgroundTasks, ids: str = "", user=Depends(tech), conn=Depends(M.db)):
        want = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        out = []
        for it in svc.batch(conn, user, want):
            if it.pop("need_refine", False):
                bg.add_task(svc.refine, it["note_id"])
            out.append(it)
        return {"items": out}

    @app.post("/api/notes/{nid}/followup/answer")
    def followup_answer(nid: int, body: AnswerIn, user=Depends(tech), conn=Depends(M.db)):
        return guard(svc.answer, conn, user, nid, body.model_dump())

    @app.post("/api/notes/{nid}/followup/dismiss")
    def followup_dismiss(nid: int, user=Depends(tech), conn=Depends(M.db)):
        return guard(svc.dismiss, conn, user, nid)
