"""列表页 AI 小结接口：POST /api/digest {kind, facts, fallback}。说明见 services/digest.py。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..services import digest as svc

SCHEMA = svc.SCHEMA


class DigestIn(BaseModel):
    kind: str
    facts: dict = Field(default_factory=dict)
    fallback: str = Field("", max_length=600)
    cached_only: bool = False   # 进页面、切筛选只取已有结果，不调用模型（验收 I9：列表 AI 小结人工点击才生成）


def register(app) -> None:
    from fastapi import Depends, HTTPException

    from .. import main as M

    @app.post("/api/digest")
    def list_digest(body: DigestIn, user=Depends(M.current_user), conn=Depends(M.db)):
        if user["role"] not in M.roles_mod.HARBOUR_ROLES:   # AI 小结只给港湾岗位（RJ 2026-10-09），分包岗位看规则归并文字
            raise HTTPException(403, "AI 小结只对港湾岗位开放")
        if body.kind not in svc.KINDS:
            raise HTTPException(400, "不支持的列表类型")
        if body.kind == "logs" and user["role"] == "manager":   # 项目经理不看日志审核（AGENTS.md 项目经理口径）
            raise HTTPException(403, "不在项目经理的查看范围")
        if len(str(body.facts)) > 6000:
            raise HTTPException(400, "归类结果过长")
        return svc.summarize(conn, user["project_id"], user["id"], body.kind, body.facts, body.fallback, cached_only=body.cached_only)
