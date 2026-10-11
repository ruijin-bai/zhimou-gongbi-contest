"""工笔文书接口 + 索赔通知时效接口（工笔文书方案 2026-10-08）。

- GET  /api/gongbi/types                    文书类型清单（含周报 / 月报，后者仍走 /api/reports）
- GET  /api/gongbi/docs?kind=&ref=          已起草的文书（领导只看已确认）
- POST /api/gongbi/docs {kind, ref}         起草（新版本）；ref：周 / 月的任一日期，或事件 key
- GET  /api/gongbi/docs/{id}                查看
- PUT  /api/gongbi/docs/{id} {narrative, confirm}   修改草稿 / 确认冻结
- GET  /api/gongbi/docs/{id}/docx           导出 Word
- GET  /api/gongbi/refs?kind=               事件类文书可选的事件（按通知时效排序）
- GET  /api/claims/notices                  每个事件的通知时效
- PUT  /api/claims/notices {key, aware_date?, status?, reason?, letter_id?}
- GET/PUT /api/claims/notice-rule           通知天数（项目规则，PUT 只限系统管理员）
- GET  /api/gongbi/letters                  致分包文书（分包只看发给本人的；分包安全员不看进度类）
- GET  /api/gongbi/letters/{id}             查看；POST .../ack 签收；POST .../reply 进度类回复；POST .../review 工程主管接受或退回
系统不对外发送任何文书；“已发出”由人在索赔证据页登记。索赔与通知函对分包单位不可见；致分包文书只给接收人看。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..services import claim_notice as cn
from ..services import gongbi_docs as svc
from ..services import sub_letters as sl

SCHEMA = cn.SCHEMA + svc.SCHEMA + sl.SCHEMA

READERS = frozenset({"manager", "eng_sup", "safety_sup", "leader", "admin"})
CLAIM_READERS = frozenset({"manager", "eng_sup", "safety_sup", "leader"})
CLAIM_WRITERS = frozenset({"manager", "eng_sup", "safety_sup"})


class DocIn(BaseModel):
    kind: str
    ref: str | None = Field(None, max_length=80)


class DocEdit(BaseModel):
    narrative: dict = Field(default_factory=dict)
    confirm: bool = False


class NoticeIn(BaseModel):
    key: str = Field(..., max_length=80)
    aware_date: str | None = None
    note: str = Field("", max_length=200)
    status: str | None = None
    reason: str = Field("", max_length=300)
    letter_id: int | None = None


class ReplyIn(BaseModel):
    text: str = Field(..., max_length=600)
    finish_date: str


class ReviewIn(BaseModel):
    accept: bool
    reason: str = Field("", max_length=300)


class RuleIn(BaseModel):
    days: int
    basis: str = Field("", max_length=120)


def register(app) -> None:
    from fastapi import Depends, HTTPException
    from fastapi.responses import FileResponse

    from .. import main as M
    from ..ai import docs_registry as reg

    def reader(user=Depends(M.current_user)) -> dict:
        if user["role"] not in READERS:
            raise HTTPException(403, "当前角色无权查看工笔文书")
        return user

    def claim_reader(user=Depends(M.current_user)) -> dict:
        if user["role"] not in CLAIM_READERS:
            raise HTTPException(403, "当前角色无权查看索赔事件")
        return user

    def claim_writer(user=Depends(M.current_user)) -> dict:
        if user["role"] not in CLAIM_WRITERS:
            raise HTTPException(403, "当前角色只读")
        return user

    def wrap(fn, *a, **k):
        try:
            return fn(*a, **k)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from None
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/gongbi/types")
    def gongbi_types(user=Depends(reader)):
        return {"types": reg.catalogue(user["role"]), "rules": ["数字只来自系统事实，数字核对不过的栏目退回规则版文字", "每句话带日志、预警或隐患编号",
                                                               "AI 只起草，人确认后冻结；对外发送由人完成", "对外文书不写人名、金额，任何文书不写产值"]}

    @app.get("/api/gongbi/docs")
    def gongbi_docs(kind: str | None = None, ref: str | None = None, user=Depends(reader), conn=Depends(M.db)):
        return svc.listing(conn, user, kind, ref)

    @app.post("/api/gongbi/docs")
    def gongbi_create(body: DocIn, user=Depends(reader), conn=Depends(M.db)):
        return wrap(svc.generate, conn, user, body.kind, body.ref)

    @app.get("/api/gongbi/docs/{did}")
    def gongbi_doc(did: int, user=Depends(reader), conn=Depends(M.db)):
        return wrap(svc.get, conn, user, did)

    @app.put("/api/gongbi/docs/{did}")
    def gongbi_edit(did: int, body: DocEdit, user=Depends(reader), conn=Depends(M.db)):
        return wrap(svc.edit, conn, user, did, body.narrative, body.confirm)

    @app.get("/api/gongbi/docs/{did}/docx")
    def gongbi_docx(did: int, user=Depends(reader), conn=Depends(M.db)):
        path, name = wrap(svc.to_docx, conn, user, did)
        return FileResponse(path, filename=name)

    @app.get("/api/gongbi/refs")
    def gongbi_refs(kind: str, user=Depends(reader), conn=Depends(M.db)):
        if reg.TYPES.get(kind, {}).get("ref") == "event" and user["role"] not in CLAIM_READERS:
            raise HTTPException(403, "当前角色无权查看索赔事件")
        return svc.ref_suggest(conn, user, kind)

    @app.get("/api/gongbi/letters")
    def gongbi_letters(user=Depends(M.current_user), conn=Depends(M.db)):
        return sl.listing(conn, user)

    @app.get("/api/gongbi/letters/{lid}")
    def gongbi_letter(lid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        return wrap(sl.view, conn, user, lid)

    @app.post("/api/gongbi/letters/{lid}/ack")
    def gongbi_letter_ack(lid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        return wrap(sl.ack, conn, user, lid)

    @app.post("/api/gongbi/letters/{lid}/reply")
    def gongbi_letter_reply(lid: int, body: ReplyIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return wrap(sl.reply, conn, user, lid, body.text, body.finish_date)

    @app.post("/api/gongbi/letters/{lid}/review")
    def gongbi_letter_review(lid: int, body: ReviewIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return wrap(sl.review, conn, user, lid, body.accept, body.reason)

    @app.get("/api/claims/notices")
    def claim_notices(user=Depends(claim_reader), conn=Depends(M.db)):
        st = cn.states(conn, user["project_id"])
        letters = {}
        for d in svc.listing(conn, user, "notice_of_claim"):
            letters.setdefault(d["ref"], d)          # 每个事件最新一版通知函
        return {"days": cn.notice_days(conn, user["project_id"]), "clause": cn.CLAUSE, "note": cn.SCOPE_NOTE, "can_write": user["role"] in CLAIM_WRITERS,
                "notices": {k: {**{x: v for x, v in n.items() if x != "event"}, "letter": letters.get(k)} for k, n in st.items()}}

    @app.put("/api/claims/notices")
    def claim_notice_set(body: NoticeIn, user=Depends(claim_writer), conn=Depends(M.db)):
        out = None
        if body.aware_date:
            out = wrap(cn.set_aware, conn, user, body.key, body.aware_date, body.note)
        if body.status:
            out = wrap(cn.decide, conn, user, body.key, body.status, body.reason, body.letter_id)
        if out is None:
            raise HTTPException(400, "未提供要修改的内容")
        return out

    @app.get("/api/claims/notice-rule")
    def claim_notice_rule(user=Depends(M.current_user), conn=Depends(M.db)):
        return {"days": cn.notice_days(conn, user["project_id"]), "default": cn.DEFAULT_DAYS, "clause": cn.CLAUSE, "can_edit": user["role"] == "admin"}

    @app.put("/api/claims/notice-rule")
    def claim_notice_rule_set(body: RuleIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return {"days": wrap(cn.set_notice_days, conn, user, body.days, body.basis)}
