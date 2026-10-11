"""Excel 日报导入（技术员）：上传识别 → 预览核对 → 逐行确认后生成当天日志草稿。规则见 app/services/xlsx_daily.py。

- POST /api/logs/xlsx            上传 .xlsx（multipart file），返回预览 {import_id, parsed, activities, day_log, imported_before}
- GET  /api/logs/xlsx/{id}       再次打开预览
- POST /api/logs/xlsx/{id}/confirm  {log_date, decisions:[{i, action: p6|text|skip, activity_code?, qty?}], keep?}
                                 → {log_id, log_date, progress, works}；只写草稿，不提交
"""
from __future__ import annotations

from fastapi import Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS xlsx_imports (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    decisions_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    log_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_xlsx_imports_user ON xlsx_imports(user_id, sha256);
CREATE INDEX IF NOT EXISTS ix_xlsx_imports_log ON xlsx_imports(log_id);
"""


class ConfirmIn(BaseModel):
    log_date: str = Field(..., max_length=10)
    decisions: list[dict] = []
    keep: dict[str, bool] = {}


def register(app) -> None:
    from .. import main as M
    from ..services import xlsx_daily as svc

    def tech(user=Depends(M.current_user)) -> dict:
        if user["role"] != "tech":
            raise HTTPException(403, "仅技术员导入 Excel 日报")
        return user

    def guard(fn, *a):
        try:
            return fn(*a)
        except svc.XlsxError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        except M.log_svc.LogError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/logs/xlsx")
    def xlsx_upload(file: UploadFile = File(...), user=Depends(tech), conn=Depends(M.db)):
        raw = file.file.read(svc.MAX_BYTES + 1)
        return guard(svc.preview, conn, user, raw, file.filename or "")

    @app.get("/api/logs/xlsx/{iid}")
    def xlsx_view(iid: int, user=Depends(tech), conn=Depends(M.db)):
        return guard(svc.view, conn, user, iid)

    @app.post("/api/logs/xlsx/{iid}/confirm")
    def xlsx_confirm(iid: int, body: ConfirmIn, user=Depends(tech), conn=Depends(M.db)):
        return guard(svc.confirm, conn, user, iid, body.log_date, body.decisions, body.keep)
