"""资料Agent：隐蔽工程影像记录自动成册。

权限（后端强制）：
- 查看：项目经理、工程主管、技术员（只看自己的记录，建档限自己负责范围）、领导与安全主管（只读）；安全员（分包）与系统管理员无权；
- 创建 / 拍照 / 编辑草稿 / 确认齐全 / 报验：技术员（自己范围）、工程主管、项目经理；
- 录入监理结果（通过/退回）、增删改验收模板：工程主管、项目经理；
- 所有写操作写审计；AI 只产出识别结果，不会自行改状态。
"""
from __future__ import annotations

from typing import Literal

from fastapi import Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS conceal_records (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    activity_code TEXT,
    part TEXT NOT NULL DEFAULT '',
    template_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','ready','submitted','passed','rejected')),
    inspection_date TEXT,
    fields_json TEXT NOT NULL DEFAULT '{}',
    photos_json TEXT NOT NULL DEFAULT '[]',
    ai_json TEXT NOT NULL DEFAULT '{}',
    missing_json TEXT NOT NULL DEFAULT '{}',
    supervisor_name TEXT NOT NULL DEFAULT '',
    submitted_at TEXT,
    result_note TEXT NOT NULL DEFAULT '',
    result_at TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT '',
    narration TEXT NOT NULL DEFAULT '',
    ready_note TEXT NOT NULL DEFAULT '',
    ready_by INTEGER,
    ready_at TEXT,
    submitted_by INTEGER,
    result_by INTEGER,
    template_json TEXT NOT NULL DEFAULT '',
    history_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_conceal_proj ON conceal_records(project_id, status);
CREATE INDEX IF NOT EXISTS ix_conceal_act ON conceal_records(project_id, activity_code);
CREATE INDEX IF NOT EXISTS ix_conceal_part ON conceal_records(project_id, part);
CREATE TABLE IF NOT EXISTS conceal_templates (
    project_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    body_json TEXT NOT NULL DEFAULT '{}',
    hidden INTEGER NOT NULL DEFAULT 0,
    updated_by INTEGER,
    updated_at TEXT,
    PRIMARY KEY (project_id, key)
);
"""

_EXTRA_COLS = {"narration": "TEXT NOT NULL DEFAULT ''", "ready_note": "TEXT NOT NULL DEFAULT ''", "ready_by": "INTEGER", "ready_at": "TEXT",
               "submitted_by": "INTEGER", "result_by": "INTEGER", "template_json": "TEXT NOT NULL DEFAULT ''",
               "history_json": "TEXT NOT NULL DEFAULT '[]'", "updated_at": "TEXT"}


def migrate(conn) -> None:
    """表若已被别处先建（列不全），幂等补列。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(conceal_records)").fetchall()}
    for name, ddl in _EXTRA_COLS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE conceal_records ADD COLUMN {name} {ddl}")


class CreateIn(BaseModel):
    part: str = Field("", max_length=40)
    template_key: str = Field(min_length=1, max_length=24)
    activity_code: str | None = Field(None, max_length=40)
    inspection_date: str | None = Field(None, max_length=10)
    title: str = Field("", max_length=60)


class MetaIn(BaseModel):
    title: str | None = Field(None, max_length=60)
    inspection_date: str | None = Field(None, max_length=10)
    narration: str | None = Field(None, max_length=1500)
    part: str | None = Field(None, max_length=40)


class FieldsIn(BaseModel):
    fields: dict[str, dict]


class AttachIn(BaseModel):
    media_ids: list[int] = Field(min_length=1, max_length=20)
    shot: str | None = Field(None, max_length=24)


class ShotIn(BaseModel):
    shot: str | None = Field(None, max_length=24)


class AcceptIn(BaseModel):
    media_ids: list[int] = []


class AnalyzeIn(BaseModel):
    narration: str | None = Field(None, max_length=1500)


class ReadyIn(BaseModel):
    note: str = Field("", max_length=300)


class SubmitIn(BaseModel):
    supervisor_name: str = Field("", max_length=30)


class ResultIn(BaseModel):
    result: Literal["passed", "rejected"]
    note: str = Field("", max_length=300)
    date: str | None = Field(None, max_length=10)
    supervisor_name: str = Field("", max_length=30)


def register(app) -> None:
    from fastapi.responses import FileResponse

    from .. import main as M
    from ..services import conceal as svc
    from ..services import media as media_svc

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except svc.ConcealError as exc:
            raise HTTPException(exc.status, exc.msg) from None

    def reader(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.READ_ROLES:
            raise HTTPException(403, "仅项目经理、工程主管、技术员、安全主管、领导可查看隐蔽验收")
        return user

    def writer(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.WRITE_ROLES:
            raise HTTPException(403, "仅技术员、工程主管、项目经理可编辑隐蔽验收资料" if user["role"] in svc.READ_ROLES else "无权操作隐蔽验收")
        return user

    def judge(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.RESULT_ROLES:
            raise HTTPException(403, "仅工程主管、项目经理可录入监理结果或修改验收模板")
        return user

    # ------------------------------------------------------------------ 模板
    @app.get("/api/conceal/templates")
    def tpl_list(user=Depends(reader), conn=Depends(M.db)):
        return {"templates": svc.templates(conn, user["project_id"], include_hidden=user["role"] in svc.RESULT_ROLES),
                "can_edit": user["role"] in svc.RESULT_ROLES, "notice": svc.NOTICE, "measure_note": svc.MEASURE_NOTE}

    @app.put("/api/conceal/templates/{key}")
    def tpl_save(key: str, body: dict = Body(...), user=Depends(judge), conn=Depends(M.db)):
        return guard(svc.save_template, conn, user, key, body)

    @app.delete("/api/conceal/templates/{key}")
    def tpl_delete(key: str, restore: bool = False, user=Depends(judge), conn=Depends(M.db)):
        return guard(svc.delete_template, conn, user, key, restore)

    # ------------------------------------------------------------------ 选项、缺资料扫描、候选照片、成册（固定路径放在 {id} 路由之前）
    @app.get("/api/conceal/options")
    def options(user=Depends(reader), conn=Depends(M.db)):
        return svc.options(conn, user)

    @app.get("/api/conceal/gaps")
    def gaps(user=Depends(reader), conn=Depends(M.db)):
        return svc.gaps(conn, user)

    @app.get("/api/conceal/candidates")
    def candidates(part: str, date: str | None = None, record_id: int | None = None, user=Depends(reader), conn=Depends(M.db)):
        return guard(svc.candidates, conn, user, part, date, record_id)

    @app.get("/api/conceal/docx")
    def part_docx(part: str, include_draft: bool = False, user=Depends(reader), conn=Depends(M.db)):
        path = guard(svc.part_docx, conn, user, part, include_draft)
        return FileResponse(path, filename=f"隐蔽验收资料册_{part.replace('/', '_').replace('#', '')}.docx")

    # ------------------------------------------------------------------ 记录
    @app.get("/api/conceal")
    def listing(status: str = "", part: str = "", activity: str = "", template: str = "", user=Depends(reader), conn=Depends(M.db)):
        return svc.list_records(conn, user, status, part, activity, template)

    @app.post("/api/conceal")
    def create(body: CreateIn, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.create, conn, user, body.part, body.template_key, body.activity_code, body.inspection_date, body.title)

    @app.get("/api/conceal/{rid}")
    def detail(rid: int, user=Depends(reader), conn=Depends(M.db)):
        return svc.view(conn, user, guard(svc.get_row, conn, user, rid))

    @app.put("/api/conceal/{rid}")
    def meta(rid: int, body: MetaIn, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.update_meta, conn, user, guard(svc.get_row, conn, user, rid), body.title, body.inspection_date, body.narration, body.part)

    @app.delete("/api/conceal/{rid}")
    def remove(rid: int, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.delete, conn, user, guard(svc.get_row, conn, user, rid))

    @app.put("/api/conceal/{rid}/fields")
    def fields(rid: int, body: FieldsIn, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.set_fields, conn, user, guard(svc.get_row, conn, user, rid), body.fields)

    # ---- 照片
    @app.post("/api/conceal/{rid}/photos")
    def upload(rid: int, files: list[UploadFile] = File(...), shot: str = Form(""), taken_at: list[str] = Form([]), geo_lat: list[str] = Form([]),
               geo_lon: list[str] = Form([]), geo_acc: list[str] = Form([]), place: list[str] = Form([]), user=Depends(writer), conn=Depends(M.db)):
        row = guard(svc.get_row, conn, user, rid)
        items = []
        for i, f in enumerate(files):
            items.append({"name": f.filename or "", "raw": f.file.read(media_svc.MAX_UPLOAD + 1), "taken_at": taken_at[i] if i < len(taken_at) else None,
                          "geo": media_svc.parse_geo(*(v[i] if i < len(v) else None for v in (geo_lat, geo_lon, geo_acc, place)))})
        return guard(svc.add_uploads, conn, user, row, items, shot or None)

    @app.post("/api/conceal/{rid}/photos/attach")
    def attach(rid: int, body: AttachIn, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.attach_existing, conn, user, guard(svc.get_row, conn, user, rid), body.media_ids, body.shot)

    @app.post("/api/conceal/{rid}/photos/accept-ai")
    def accept_ai(rid: int, body: AcceptIn, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.accept_ai_shots, conn, user, guard(svc.get_row, conn, user, rid), body.media_ids)

    @app.put("/api/conceal/{rid}/photos/{mid}")
    def photo_shot(rid: int, mid: int, body: ShotIn, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.set_photo_shot, conn, user, guard(svc.get_row, conn, user, rid), mid, body.shot)

    @app.delete("/api/conceal/{rid}/photos/{mid}")
    def photo_remove(rid: int, mid: int, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.remove_photo, conn, user, guard(svc.get_row, conn, user, rid), mid)

    # ---- AI 与状态流转
    @app.post("/api/conceal/{rid}/analyze")
    def analyze(rid: int, body: AnalyzeIn, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.analyze, conn, user, guard(svc.get_row, conn, user, rid), body.narration)

    @app.post("/api/conceal/{rid}/ready")
    def ready(rid: int, body: ReadyIn, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.set_ready, conn, user, guard(svc.get_row, conn, user, rid), body.note)

    @app.post("/api/conceal/{rid}/reopen")
    def reopen(rid: int, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.reopen, conn, user, guard(svc.get_row, conn, user, rid))

    @app.post("/api/conceal/{rid}/submit")
    def submit(rid: int, body: SubmitIn, user=Depends(writer), conn=Depends(M.db)):
        return guard(svc.submit, conn, user, guard(svc.get_row, conn, user, rid), body.supervisor_name)

    @app.post("/api/conceal/{rid}/result")
    def result(rid: int, body: ResultIn, user=Depends(judge), conn=Depends(M.db)):
        return guard(svc.record_result, conn, user, guard(svc.get_row, conn, user, rid), body.result, body.note, body.date, body.supervisor_name)

    @app.get("/api/conceal/{rid}/docx")
    def docx(rid: int, user=Depends(reader), conn=Depends(M.db)):
        row = guard(svc.get_row, conn, user, rid)
        return FileResponse(guard(svc.to_docx, conn, user, row), filename=f"隐蔽工程验收记录_{rid}.docx")
