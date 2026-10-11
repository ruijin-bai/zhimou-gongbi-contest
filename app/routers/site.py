"""档案Agent：部位档案 + 工期/费用索赔证据包。

权限（后端强制）：
- 部位档案：港湾岗位、领导看全项目；分包单位只看本单位（同公司）范围内的记录；系统管理员不参与业务；
- 部位概况/归并写法：项目经理、工程主管、安全主管；
- 索赔证据包：只对项目经理、工程主管、安全主管开放生成与导出，领导只读（看事件与已生成的证据包），分包单位与管理员不可见。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS site_summaries (
    project_id INTEGER NOT NULL,
    part TEXT NOT NULL,
    facts_hash TEXT NOT NULL,
    summary TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'ai',
    ai_run_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, part)
);
CREATE TABLE IF NOT EXISTS claim_packs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    event_keys_json TEXT NOT NULL DEFAULT '[]',
    snapshot_json TEXT NOT NULL DEFAULT '[]',
    narrative_json TEXT NOT NULL DEFAULT '{}',
    ai_run_id INTEGER,
    status TEXT NOT NULL DEFAULT 'draft',
    created_by INTEGER,
    created_at TEXT NOT NULL,
    exported_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_packs ON claim_packs(project_id, id);
"""

WRITERS = frozenset({"manager", "eng_sup", "safety_sup"})      # 生成概况、归并写法、生成/导出证据包
CLAIM_READERS = WRITERS | {"leader"}                            # 领导只读


class SummaryIn(BaseModel):
    part: str = Field(min_length=1, max_length=40)


class PackIn(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=8)
    title: str = Field(default="", max_length=60)


def register(app) -> None:
    from fastapi import Depends, HTTPException
    from fastapi.responses import FileResponse

    from .. import main as M
    from ..db import all_, one
    from ..services import claims as claim_svc
    from ..services import parts as part_svc

    def archive_user(user=Depends(M.current_user)) -> dict:
        if user["role"] == "admin":
            raise HTTPException(403, "系统管理员不参与业务档案")
        return user

    def writer(user=Depends(archive_user)) -> dict:
        if user["role"] not in WRITERS:
            raise HTTPException(403, "仅项目经理、工程主管、安全主管可操作")
        return user

    def claim_reader(user=Depends(M.current_user)) -> dict:
        if user["role"] not in CLAIM_READERS:
            raise HTTPException(403, "索赔证据包仅港湾项目部相关岗位可见")
        return user

    def claim_writer(user=Depends(M.current_user)) -> dict:
        if user["role"] not in WRITERS:
            raise HTTPException(403, "领导只读；生成与导出证据包需项目经理、工程主管或安全主管")
        return user

    # ------------------------------------------------------------------ 部位档案
    @app.get("/api/sites")
    def sites(user=Depends(archive_user), conn=Depends(M.db)):
        out = part_svc.list_parts(conn, user)
        out["can_write"] = user["role"] in WRITERS
        out["can_claims"] = user["role"] in CLAIM_READERS
        return out

    @app.get("/api/sites/detail")
    def site_detail(part: str, types: str = "", user=Depends(archive_user), conn=Depends(M.db)):
        ts = [t for t in types.split(",") if t in part_svc.TYPE_NAME]
        out = part_svc.detail(conn, user, part, ts or None)
        out["can_write"] = user["role"] in WRITERS
        return out

    @app.post("/api/sites/summary")
    def site_summary(body: SummaryIn, user=Depends(writer), conn=Depends(M.db)):
        return part_svc.make_summary(conn, user, body.part)

    @app.post("/api/sites/organize")
    def site_organize(user=Depends(writer), conn=Depends(M.db)):
        return part_svc.organize(conn, user)

    @app.get("/api/sites/docx")
    def site_docx(part: str, user=Depends(archive_user), conn=Depends(M.db)):
        path = part_svc.to_docx(conn, user, part)
        return FileResponse(path, filename=f"部位档案_{part.replace('/', '_').replace('#', '')}.docx")

    # ------------------------------------------------------------------ 索赔证据包
    @app.get("/api/claims/events")
    def claim_events(date_from: str | None = None, date_to: str | None = None, user=Depends(claim_reader), conn=Depends(M.db)):
        return {"events": claim_svc.list_events(conn, user, date_from, date_to), "can_write": user["role"] in WRITERS, "notice": claim_svc.NOTICE,
                "cats": {k: v["name"] for k, v in claim_svc.CATS.items()}}

    @app.get("/api/claims/events/detail")
    def claim_event_detail(key: str, user=Depends(claim_reader), conn=Depends(M.db)):
        ev = next((e for e in claim_svc.all_events(conn, user["project_id"]) if e["key"] == key), None)
        if not ev:
            raise HTTPException(404, "事件不存在")
        return claim_svc.evidence(conn, user, ev)

    @app.post("/api/claims/packs")
    def claim_pack_create(body: PackIn, user=Depends(claim_writer), conn=Depends(M.db)):
        return claim_svc.create_pack(conn, user, body.keys, body.title)

    @app.get("/api/claims/packs")
    def claim_packs(user=Depends(claim_reader), conn=Depends(M.db)):
        rows = all_(conn, "SELECT * FROM claim_packs WHERE project_id=? ORDER BY id DESC LIMIT 100", (user["project_id"],))
        return [claim_svc.pack_view(conn, r) for r in rows]

    def _pack(conn, user, pid: int) -> dict:
        row = one(conn, "SELECT * FROM claim_packs WHERE id=? AND project_id=?", (pid, user["project_id"]))
        if not row:
            raise HTTPException(404, "证据包不存在")
        return row

    @app.get("/api/claims/packs/{pid}")
    def claim_pack(pid: int, user=Depends(claim_reader), conn=Depends(M.db)):
        return claim_svc.pack_view(conn, _pack(conn, user, pid), full=True)

    @app.get("/api/claims/packs/{pid}/docx")
    def claim_pack_docx(pid: int, user=Depends(claim_writer), conn=Depends(M.db)):
        row = _pack(conn, user, pid)
        path = claim_svc.to_docx(conn, user, row)
        return FileResponse(path, filename=f"索赔证据包_{pid}.docx")
