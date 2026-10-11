"""催办Agent 升级链 + 订阅式提问：接口与后台调度。

接口（均在 /api/nudge 下）：
- 规则/运行/日志/今日动态：工程主管、安全主管（项目经理只拍板，不进催办台，返回 403）；
- pins：固定到首页与订阅提问，仅港湾管理岗本人可见/可改，分包单位不开放。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from ..services import jobs
from ..services import nudge as svc

log = logging.getLogger("zhimou.nudge")

SCHEMA = svc.SCHEMA + jobs.SCHEMA   # 每日任务认领表随后台调度一起建
TICK_SECONDS = 30 * 60
SCHEDULER_OFF_NOTE = "后台自动催办未开启，目前只能手动运行。如需自动催办，请联系系统管理员。"
FIRST_DELAY = 90

_started = False
_lock = threading.Lock()


class RulesIn(BaseModel):
    enabled: bool | None = None
    lead_days: int | None = None
    gap_hours: list[float] | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None
    daily_cap: int | None = None
    sources: dict[str, bool] | None = None


class RunIn(BaseModel):
    dry: bool = False


class PinIn(BaseModel):
    question: str = Field(max_length=300)
    schedule: str = ""
    answer: dict | None = None


class PinPutIn(BaseModel):
    schedule: str = ""


def _loop() -> None:
    time.sleep(FIRST_DELAY)
    while True:
        wait = TICK_SECONDS
        if os.environ.get("ZB_SCHEDULER", "1") != "0":
            if jobs.hold_scheduler():  # 多个进程连同一个库时，只有持有调度锁的进程自动催办
                try:
                    from ..db import init_db
                    init_db()
                    svc.tick()
                except Exception:  # noqa: BLE001
                    log.exception("催办调度失败")
            else:
                wait = jobs.RETRY_SECONDS
        time.sleep(wait)


def start_scheduler() -> bool:
    """启动催办守护线程（ZB_SCHEDULER=0 时不启动；只启动一次）。"""
    global _started
    if os.environ.get("ZB_SCHEDULER", "1") == "0":
        return False
    with _lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=_loop, daemon=True, name="nudge-scheduler").start()
    return True


def register(app) -> None:
    from .. import main as M

    # 服务真正启动（lifespan）时才起线程，导入模块/命令行不会起
    orig = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan(a):
        async with orig(a) as state:
            start_scheduler()
            yield state
    app.router.lifespan_context = _lifespan

    def mgr(user=Depends(M.current_user)) -> dict:
        if user["role"] not in svc.MGMT_ROLES:
            raise HTTPException(403, "催办记录仅工程主管、安全主管可用")
        return user

    # 催办规则归系统管理员（导航编排 10-08，用户同意）：管理员在“项目与规则”里查看和修改；工程主管、安全主管只读，用来显示升级链
    def rules_reader(user=Depends(M.current_user)) -> dict:
        if user["role"] not in (*svc.MGMT_ROLES, "admin"):
            raise HTTPException(403, "催办记录仅工程主管、安全主管可用")
        return user

    def rules_writer(user=Depends(M.current_user)) -> dict:
        if user["role"] != "admin":
            raise HTTPException(403, "催办规则由系统管理员在“项目与规则”里设置")
        return user

    def guard(fn, *a):
        try:
            return fn(*a)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))

    @app.get("/api/nudge/rules")
    def get_rules(user=Depends(rules_reader), conn=Depends(M.db)):
        on = os.environ.get("ZB_SCHEDULER", "1") != "0"   # 环境变量名等技术细节只在系统管理员的系统概况里出现
        role = user["role"]
        return {"rules": svc.scope_rules(svc.load_rules(conn, user["project_id"]), role), "defaults": svc.scope_rules(svc.DEFAULT_RULES, role),
                "stages": svc.STAGE_NAME, "scheduler": on, "scheduler_note": "" if on else SCHEDULER_OFF_NOTE}

    @app.put("/api/nudge/rules")
    def put_rules(body: RulesIn, user=Depends(rules_writer), conn=Depends(M.db)):
        return {"rules": svc.scope_rules(svc.save_rules(conn, user["project_id"], user, body.model_dump(exclude_none=True)), user["role"])}

    @app.post("/api/nudge/run")
    def run_now(body: RunIn, user=Depends(mgr), conn=Depends(M.db)):
        rep = svc.run_project(conn, user["project_id"], dry=body.dry, actor=user)
        if not body.dry:
            M.audit(conn, user["id"], "nudge.run", f"project:{user['project_id']}", rep["summary"][:200])
        return svc.scope_report(rep, user["role"])

    @app.get("/api/nudge/log")
    def get_log(limit: int = 60, user=Depends(mgr), conn=Depends(M.db)):
        return svc.recent_log(conn, user["project_id"], max(1, min(limit, 200)), user["role"])

    @app.get("/api/nudge/today")
    def get_today(user=Depends(mgr), conn=Depends(M.db)):
        return svc.today_summary(conn, user["project_id"], role=user["role"])

    # ---- 固定到首页 / 订阅式提问
    @app.get("/api/nudge/pins")
    def get_pins(user=Depends(M.current_user), conn=Depends(M.db)):
        return guard(svc.list_pins, conn, user)

    @app.post("/api/nudge/pins")
    def post_pin(body: PinIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return guard(svc.create_pin, conn, user, body.question, body.schedule, body.answer)

    @app.put("/api/nudge/pins/{pid}")
    def put_pin(pid: int, body: PinPutIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return guard(svc.set_pin_schedule, conn, user, pid, body.schedule)

    @app.delete("/api/nudge/pins/{pid}")
    def del_pin(pid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        guard(svc.delete_pin, conn, user, pid)
        return {"ok": True}

    @app.post("/api/nudge/pins/{pid}/refresh")
    def refresh_pin(pid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        return guard(svc.refresh_pin, conn, user, pid)
