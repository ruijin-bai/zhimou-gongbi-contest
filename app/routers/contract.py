"""合同 / 规范条款库（报表与档案线的一个页签）。提问不在这里：统一输入框问到合同、条款、规范时，问工笔自动检索条款库并标 [C编号]。

- GET    /api/contract/docs              条款库文件清单
- POST   /api/contract/docs              导入（表单：title, kind, text 或 file=.txt/.docx）；仅工程主管
- DELETE /api/contract/docs/{id}         删除
- GET    /api/contract/search?q=         按条款号、关键词检索条款原文（不调用模型）
- GET    /api/contract/clause/{id}       单条条款原文（问答里 [C编号] 点开）
"""
from __future__ import annotations

from fastapi import Depends, File, Form, HTTPException, UploadFile

from ..services.contract_kb import SCHEMA  # noqa: F401  建表


def register(app) -> None:
    from .. import main as M
    from ..services import contract_kb as svc

    def reader(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.READERS:
            raise HTTPException(403, "条款库只给项目经理、工程主管、安全主管")
        return user

    def guard(fn, *a):
        try:
            return fn(*a)
        except svc.KbError as e:
            raise HTTPException(e.status, e.msg) from None

    @app.get("/api/contract/docs")
    def contract_docs(user=Depends(reader), conn=Depends(M.db)):
        return {"docs": svc.docs(conn, user["project_id"]), "kinds": svc.KINDS, "can_import": user["role"] in svc.IMPORTERS}

    @app.post("/api/contract/docs")
    async def contract_import(title: str = Form(""), kind: str = Form("other"), text: str = Form(""), file: UploadFile | None = File(None),
                              user=Depends(reader), conn=Depends(M.db)):
        if file is not None and file.filename:
            raw = await file.read()
            if len(raw) > 8 * 1024 * 1024:
                raise HTTPException(400, "文件超过 8MB，请分册导入")
            text = guard(svc.text_of_upload, file.filename, raw)
            title = title or file.filename.rsplit(".", 1)[0]
        return guard(svc.import_doc, conn, user, title, kind, text)

    @app.delete("/api/contract/docs/{did}")
    def contract_delete(did: int, user=Depends(reader), conn=Depends(M.db)):
        return guard(svc.delete_doc, conn, user, did)

    @app.get("/api/contract/clause/{cid}")
    def contract_clause(cid: int, user=Depends(reader), conn=Depends(M.db)):
        from ..db import one
        r = one(conn, """SELECT c.*, d.title doc_title FROM contract_clauses c JOIN contract_docs d ON d.id=c.doc_id WHERE c.id=? AND c.project_id=?""",
                (cid, user["project_id"]))
        if not r:
            raise HTTPException(404, "条款不存在（所在文件可能已删除）")
        return {k: r[k] for k in ("id", "doc_id", "doc_title", "clause_no", "title", "text")}

    @app.get("/api/contract/search")
    def contract_search(q: str = "", user=Depends(reader), conn=Depends(M.db)):
        return {"items": svc.search(conn, user["project_id"], q[:100], limit=12)}
