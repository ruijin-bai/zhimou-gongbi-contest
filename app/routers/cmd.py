"""指挥Agent 接口：一句话派单动作卡、例会纪要转行动项、上次例会闭环核对。

动作卡先解析、后确认：/api/cmd/parse 与 /api/cmd/minutes 只产出建议，不改任何业务数据（纪要会存一条未确认的会议草稿）；
人点确认后 /api/cmd/execute 与 /api/meetings/{id}/items 才落库，并写 audit/events。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    meeting_date TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    source_text TEXT NOT NULL DEFAULT '',
    ai_run_id INTEGER,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_meetings_proj ON meetings(project_id, meeting_date);
CREATE TABLE IF NOT EXISTS meeting_items (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
    src_idx INTEGER,
    text TEXT NOT NULL,
    owner_id INTEGER REFERENCES users(id),
    owner_name TEXT NOT NULL DEFAULT '',
    owner_company TEXT NOT NULL DEFAULT '',
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
    finding_id INTEGER REFERENCES findings(id),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    done_at TEXT,
    done_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS ix_mitems_meeting ON meeting_items(meeting_id);
CREATE INDEX IF NOT EXISTS ix_mitems_owner ON meeting_items(project_id, owner_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_mitems_src ON meeting_items(meeting_id, src_idx) WHERE src_idx IS NOT NULL;
"""


class ParseIn(BaseModel):
    text: str = Field(max_length=2000)


class IntentIn(BaseModel):
    text: str = Field(max_length=12000)


class ExecIn(BaseModel):
    type: str = Field(max_length=20)
    finding_id: int | None = None
    assignee_id: int | None = None
    due_date: str | None = Field(None, max_length=10)
    note: str = Field("", max_length=200)
    source: str = Field("", max_length=300)


class MinutesIn(BaseModel):
    text: str = Field(max_length=60000)
    title: str = Field("", max_length=60)
    date: str = Field("", max_length=10)


class ItemIn(BaseModel):
    idx: int | None = None
    text: str = Field(max_length=300)
    owner_id: int | None = None
    owner_name: str = Field("", max_length=60)
    due_date: str | None = Field(None, max_length=10)
    finding_id: int | None = None


class DoneIn(BaseModel):
    done: bool = True


def register(app) -> None:
    from .. import main as M
    from ..services import cmd as svc

    @app.post("/api/cmd/parse")
    def cmd_parse(body: ParseIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return M._guard(svc.parse_command, conn, user, body.text)

    @app.post("/api/ai/intent")
    def ai_intent(body: IntentIn, user=Depends(M.current_user), conn=Depends(M.db)):
        """统一输入框：判断一句话是提问、指令还是纪要。只判断，不执行任何动作。"""
        return M._guard(svc.classify_intent, conn, user, body.text)

    @app.get("/api/cmd/findings")
    def cmd_findings(q: str = "", user=Depends(M.current_user), conn=Depends(M.db)):
        """换对象时的搜索：只列进行中的隐患/整改单。"""
        M._guard(svc.require_commander, user)
        return svc.search_findings(conn, user["project_id"], q)

    @app.post("/api/cmd/execute")
    def cmd_execute(body: ExecIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return M._guard(svc.execute_card, conn, user, body.model_dump())

    @app.post("/api/cmd/minutes")
    def cmd_minutes(body: MinutesIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return M._guard(svc.parse_minutes, conn, user, body.text, body.title, body.date)

    @app.post("/api/meetings/{mid}/items")
    def meeting_item(mid: int, body: ItemIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return M._guard(svc.confirm_item, conn, user, mid, body.model_dump())

    @app.post("/api/meetings/{mid}/save")
    def meeting_save(mid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        return M._guard(svc.save_meeting, conn, user, mid)

    @app.get("/api/meetings/review")
    def meeting_review(meeting_id: int | None = None, user=Depends(M.current_user), conn=Depends(M.db)):
        return M._guard(svc.review, conn, user, meeting_id)

    @app.get("/api/meetings/mine")
    def meeting_mine(user=Depends(M.current_user), conn=Depends(M.db)):
        return M._guard(svc.my_items, conn, user)

    @app.post("/api/meetings/items/{iid}/done")
    def meeting_done(iid: int, body: DoneIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return M._guard(svc.mark_done, conn, user, iid, body.done)
