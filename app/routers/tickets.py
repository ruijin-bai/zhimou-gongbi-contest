"""材料Agent 接口：票据识别草稿 → 人确认 → 浇筑方量 / 试块 / 材料批次与复试台账 / 预警汇总。

- 拍小票：POST /api/tickets/upload（1~4 张同一张票的照片，purpose=ticket，不进安全识别队列），后台识别，前端轮询 GET /api/tickets/{id}；
- 草稿不计入任何累计；PUT /api/tickets/{id} 人核对、改字段后确认才入账；
- 权限：上传/确认 技术员、工程主管、项目经理（技术员只确认本人的）；录试块结果/改复试结果 需 site_quality（项目经理、工程主管）；
  改规则 仅项目经理/工程主管；分包单位只看自己范围；港湾岗位看全项目；领导只读；安全员、系统管理员无权。
"""
from __future__ import annotations

from fastapi import BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS pours (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    part TEXT NOT NULL,
    activity_code TEXT NOT NULL DEFAULT '',
    grade TEXT NOT NULL DEFAULT '',
    design_qty REAL,
    pour_date TEXT,
    status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','pouring','done')),
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pours_proj ON pours(project_id, pour_date);
CREATE TABLE IF NOT EXISTS material_batches (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    category TEXT NOT NULL DEFAULT 'other' CHECK (category IN ('rebar','cement','sand','stone','admixture','other')),
    name TEXT NOT NULL DEFAULT '',
    spec TEXT NOT NULL DEFAULT '',
    supplier TEXT NOT NULL DEFAULT '',
    batch_no TEXT NOT NULL DEFAULT '',
    qty REAL NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT '',
    arrived_at TEXT,
    retest_status TEXT NOT NULL DEFAULT 'pending' CHECK (retest_status IN ('not_required','pending','sampled','pass','fail')),
    report_no TEXT,
    reported_at TEXT,
    retest_note TEXT NOT NULL DEFAULT '',
    sampled_at TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    updated_by INTEGER
);
CREATE INDEX IF NOT EXISTS ix_batches_proj ON material_batches(project_id, category, arrived_at);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    doc_kind TEXT NOT NULL CHECK (doc_kind IN ('concrete_ticket','material_receipt','cert')),
    kind_hint TEXT NOT NULL DEFAULT 'auto',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','confirmed','void')),
    ai_status TEXT NOT NULL DEFAULT 'pending',
    ai_error TEXT,
    ai_json TEXT,
    confirmed_json TEXT,
    ticket_no TEXT, supplier TEXT, vehicle TEXT, issued_at TEXT, ticket_date TEXT, part TEXT, material TEXT, category TEXT,
    grade TEXT, spec TEXT, qty REAL, unit TEXT, batch_no TEXT, slump REAL, temp REAL, receiver TEXT, remark TEXT,
    pour_id INTEGER,
    batch_id INTEGER,
    media_ids_json TEXT NOT NULL DEFAULT '[]',
    sha_json TEXT NOT NULL DEFAULT '[]',
    ai_run_id INTEGER,
    provider TEXT,
    uploaded_by INTEGER NOT NULL REFERENCES users(id),
    confirmed_by INTEGER,
    confirmed_at TEXT,
    void_by INTEGER, void_at TEXT, void_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tickets_proj ON tickets(project_id, status, id);
CREATE INDEX IF NOT EXISTS ix_tickets_pour ON tickets(pour_id);
CREATE INDEX IF NOT EXISTS ix_tickets_batch ON tickets(batch_id);
CREATE TABLE IF NOT EXISTS specimens (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    pour_id INTEGER NOT NULL REFERENCES pours(id),
    group_no TEXT NOT NULL,
    grade TEXT NOT NULL DEFAULT '',
    curing TEXT NOT NULL DEFAULT '标准养护',
    molded_date TEXT NOT NULL,
    due_7 TEXT NOT NULL,
    due_28 TEXT NOT NULL,
    r7_mpa REAL, r7_report_no TEXT, r7_tested_at TEXT, r7_by INTEGER, r7_at TEXT,
    r28_mpa REAL, r28_report_no TEXT, r28_tested_at TEXT, r28_by INTEGER, r28_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (pour_id, group_no)
);
CREATE INDEX IF NOT EXISTS ix_specimens_proj ON specimens(project_id, due_28);
"""


class ConfirmIn(BaseModel):
    doc_kind: str | None = None
    fields: dict = Field(default_factory=dict)
    category: str | None = None
    pour_id: int | None = None
    new_pour: dict | None = None
    force_dup: bool = False
    decisions: list[dict] | None = None  # 逐字段决定（ReviewTable）：[{key, decision, final?}]；服务端据 AI 原值与最终值推导并保存


class VoidIn(BaseModel):
    reason: str = Field("", max_length=100)


class AssignIn(BaseModel):
    pour_id: int | None = None
    new_pour: dict | None = None


class PourIn(BaseModel):
    part: str | None = Field(None, max_length=60)
    grade: str | None = Field(None, max_length=20)
    design_qty: float | None = None
    pour_date: str | None = Field(None, max_length=10)
    activity_code: str | None = Field(None, max_length=40)
    status: str | None = None
    note: str | None = Field(None, max_length=200)


class SpecimenIn(BaseModel):
    group_no: str = Field("", max_length=20)
    molded_date: str = Field(max_length=10)
    curing: str = "标准养护"
    note: str = Field("", max_length=100)


class ResultIn(BaseModel):
    age: int
    mpa: float
    report_no: str = Field("", max_length=60)
    tested_at: str | None = Field(None, max_length=10)


class RetestIn(BaseModel):
    status: str
    report_no: str = Field("", max_length=60)
    reported_at: str | None = Field(None, max_length=10)
    note: str = Field("", max_length=120)


class RulesIn(BaseModel):
    concrete_rules: dict | None = None
    material_rules: dict | None = None


def register(app) -> None:
    from .. import main as M
    from ..services import media as media_svc
    from ..services import tickets as svc

    def g(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except svc.TicketError as exc:
            raise HTTPException(exc.status, exc.msg) from None

    def reader(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.READ_ROLES:
            raise HTTPException(403, "当前岗位无权查看材料与砼台账")
        return user

    def writer(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.WRITE_ROLES:
            raise HTTPException(403, "仅技术员、工程主管、项目经理可拍票据；领导只读")
        return user

    # ------------------------------------------------------------------ 票据：上传 / 汇总 / 规则（静态路径放在 {id} 之前）
    @app.post("/api/tickets/upload")
    def ticket_upload(bg: BackgroundTasks, files: list[UploadFile] = File(...), doc_kind: str = Form("auto"), taken_at: list[str] = Form([]),
                      geo_lat: list[str] = Form([]), geo_lon: list[str] = Form([]), geo_acc: list[str] = Form([]), place: list[str] = Form([]),
                      user=Depends(writer), conn=Depends(M.db)):
        if len(files) > svc.MAX_PHOTOS:
            raise HTTPException(400, f"一张票据最多 {svc.MAX_PHOTOS} 张照片")
        packed = []
        for i, f in enumerate(files):
            raw = f.file.read(media_svc.MAX_UPLOAD + 1)
            geo = media_svc.parse_geo(*(v[i] if i < len(v) else None for v in (geo_lat, geo_lon, geo_acc, place)))
            packed.append((f.filename or "", raw, taken_at[i] if i < len(taken_at) else None, geo))
        t = g(svc.create_draft, conn, user, packed, doc_kind)
        bg.add_task(svc.run_ocr, t["id"])
        return svc.ticket_view(conn, t, user, svc.scope_of(conn, user))

    @app.get("/api/tickets/risks")
    def ticket_risks(user=Depends(reader), conn=Depends(M.db)):
        return svc.risks(conn, user)

    @app.get("/api/tickets/rules")
    def ticket_rules(user=Depends(reader), conn=Depends(M.db)):
        return svc.rules_view(conn, user)

    @app.put("/api/tickets/rules")
    def ticket_rules_save(body: RulesIn, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.save_rules, conn, user, body.model_dump(exclude_none=True))

    @app.get("/api/tickets")
    def ticket_list(kind: str = "", status: str = "", pour_id: int | None = None, limit: int = 100, user=Depends(reader), conn=Depends(M.db)):
        return svc.list_tickets(conn, user, kind, status, pour_id, limit)

    @app.get("/api/tickets/{tid}")
    def ticket_get(tid: int, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.get_ticket, conn, user, tid)

    @app.put("/api/tickets/{tid}")
    def ticket_confirm(tid: int, body: ConfirmIn, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.confirm, conn, user, tid, body.model_dump(exclude_unset=True))

    @app.post("/api/tickets/{tid}/void")
    def ticket_void(tid: int, body: VoidIn, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.void, conn, user, tid, body.reason)

    @app.post("/api/tickets/{tid}/pour")
    def ticket_pour(tid: int, body: AssignIn, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.assign_pour, conn, user, tid, body.model_dump(exclude_unset=True))

    @app.post("/api/tickets/{tid}/reocr")
    def ticket_reocr(tid: int, bg: BackgroundTasks, user=Depends(writer), conn=Depends(M.db)):
        t = g(svc._row, conn, tid, user)
        if t["uploaded_by"] != user["id"] and not svc.is_quality(user):
            raise HTTPException(403, "只能重试本人上传的票据")
        if not svc.reocr_allowed(t):
            raise HTTPException(409, "票据正在识别中或已确认，不能重试")
        conn.execute("UPDATE tickets SET ai_status='pending', ai_error=NULL WHERE id=?", (tid,))
        bg.add_task(svc.run_ocr, tid)
        return g(svc.get_ticket, conn, user, tid)

    # ------------------------------------------------------------------ 浇筑单元与试块
    @app.get("/api/pours/candidates")
    def pour_candidates(ticket_id: int | None = None, part: str = "", grade: str = "", day: str = "", user=Depends(reader), conn=Depends(M.db)):
        return g(svc.candidates, conn, user, ticket_id, part, grade, day)

    @app.get("/api/pours")
    def pour_list(status: str = "", user=Depends(reader), conn=Depends(M.db)):
        return svc.list_pours(conn, user, status)

    @app.post("/api/pours")
    def pour_create(body: PourIn, user=Depends(reader), conn=Depends(M.db)):
        p = g(svc.create_pour, conn, user, body.model_dump(exclude_unset=True))
        return g(svc.get_pour, conn, user, p["id"])

    @app.get("/api/pours/{pid}")
    def pour_get(pid: int, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.get_pour, conn, user, pid)

    @app.put("/api/pours/{pid}")
    def pour_update(pid: int, body: PourIn, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.update_pour, conn, user, pid, body.model_dump(exclude_unset=True))

    @app.delete("/api/pours/{pid}")
    def pour_delete(pid: int, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.delete_pour, conn, user, pid)

    @app.post("/api/pours/{pid}/specimens")
    def specimen_add(pid: int, body: SpecimenIn, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.add_specimen, conn, user, pid, body.model_dump())

    @app.get("/api/specimens/due")
    def specimen_due(within: int = 14, user=Depends(reader), conn=Depends(M.db)):
        return svc.due_list(conn, user, max(0, min(within, 60)))

    @app.post("/api/specimens/{sid}/result")
    def specimen_result(sid: int, body: ResultIn, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.record_result, conn, user, sid, body.model_dump())

    @app.delete("/api/specimens/{sid}")
    def specimen_delete(sid: int, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.delete_specimen, conn, user, sid)

    # ------------------------------------------------------------------ 材料批次与复试
    @app.get("/api/batches")
    def batch_list(category: str = "", retest: str = "", user=Depends(reader), conn=Depends(M.db)):
        return svc.list_batches(conn, user, category, retest)

    @app.put("/api/batches/{bid}/retest")
    def batch_retest(bid: int, body: RetestIn, user=Depends(reader), conn=Depends(M.db)):
        return g(svc.set_retest, conn, user, bid, body.model_dump())
