"""历史回测接口（P0-2）：多期 P6 XER 回测进度预警规则，结果存 backtests 表。

权限（路由内本地校验，G-13）：
- 运行、查看、导出 Word：工程主管、系统管理员；项目经理、领导及其他岗位一律 403；
- 采用阈值：只给工程主管（业务决定，管理员不参与业务决策），写项目设置并留审计；只能采用“真实项目数据”的回测，测试来源返回 409。
每条回测必须标明数据来源（真实项目数据 / 仿真/测试数据，G-03）。回测是确定性计算，不调用 AI；XER 原文件不落盘。
"""
from __future__ import annotations

from fastapi import Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    source TEXT NOT NULL CHECK (source IN ('real','test')),
    label TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    files_json TEXT NOT NULL DEFAULT '[]',
    summary_json TEXT NOT NULL DEFAULT '{}',
    rows_json TEXT NOT NULL DEFAULT '{}',
    sensitivity_json TEXT NOT NULL DEFAULT '[]',
    adopted_at TEXT,
    adopted_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS ix_backtests_proj ON backtests(project_id, source, id);
"""


class AdoptIn(BaseModel):
    red_slip_days: float
    tolerance_days: float = 0.0


def register(app) -> None:
    from fastapi.responses import FileResponse

    from .. import main as M
    from ..services import backtest as svc

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except svc.BacktestError as exc:
            raise HTTPException(exc.status, exc.msg) from None

    def reader(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.READ_ROLES:
            raise HTTPException(403, "历史回测仅工程主管、系统管理员可用")
        return user

    def adopter(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.ADOPT_ROLES:
            raise HTTPException(403, "采用阈值是业务决定，仅工程主管可操作")
        return user

    @app.post("/api/backtest/run")
    def run(files: list[UploadFile] | None = File(None), files_alt: list[UploadFile] | None = File(None, alias="files[]"),
            source: str = Form(""), label: str = Form(""), tolerance_days: float = Form(0.0), exclude_periods: str = Form(""),
            user=Depends(reader), conn=Depends(M.db)):
        ups = (files or []) + (files_alt or [])
        if len(ups) > svc.MAX_FILES:
            raise HTTPException(400, f"一次最多上传 {svc.MAX_FILES} 期")
        items = [(f.filename or "", f.file.read(svc.MAX_BYTES + 1)) for f in ups]
        return guard(svc.run_files, conn, user, items, source, label, tolerance_days, exclude_periods)

    # 固定路径放在 {bid} 之前
    @app.get("/api/backtest/latest")
    def latest(source: str = "", user=Depends(reader), conn=Depends(M.db)):
        src = guard(svc.normalize_source, source) if source else None
        row = svc.latest(conn, user["project_id"], src)
        return svc.view(conn, row) if row else None

    @app.get("/api/backtest/{bid}")
    def detail(bid: int, user=Depends(reader), conn=Depends(M.db)):
        return svc.view(conn, guard(svc.get, conn, user["project_id"], bid))

    @app.get("/api/backtest/{bid}/docx")
    def docx(bid: int, user=Depends(reader), conn=Depends(M.db)):
        v = svc.view(conn, guard(svc.get, conn, user["project_id"], bid))
        path = svc.to_docx(v, user["project_id"])
        return FileResponse(path, filename=f"预警回测报告_{bid}.docx")

    @app.post("/api/backtest/{bid}/adopt")
    def adopt(bid: int, body: AdoptIn, user=Depends(adopter), conn=Depends(M.db)):
        row = guard(svc.get, conn, user["project_id"], bid)
        if row["source"] != "real":  # leader 决定（10-08）：仿真/测试数据只用于核对页面与口径，不能改线上预警阈值
            raise HTTPException(409, "测试数据不能用于调整线上预警，请用真实项目数据的回测")
        return guard(svc.adopt, conn, user, row, body.red_slip_days, body.tolerance_days)
