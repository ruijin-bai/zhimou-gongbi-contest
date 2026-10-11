"""智眸工笔 B 版：单进程 Web 服务（API + 前端静态页）。"""
from __future__ import annotations

import io
import ipaddress
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agents import AGENTS, KIND_TO_AGENT
from .ai.client import AIError, call_json, provider_status, transcribe
from .config import ROOT, settings
from .db import all_, audit, connect, dumps, event, init_db, insert, loads, now_iso, one, tx
from . import roles as roles_mod
from .roles import ROLES
from .security import hash_password, issue_token, read_token, verify_password
from .services import act_names
from .services import ai_trace
from .services import alerts as alert_svc
from .services import brief as brief_svc
from .services import feed as feed_svc
from .services import features as feat_svc
from .services import findings as find_svc
from .services import jobs
from .services import output as output_svc
from .services import progress_agent
from .services import schedule as sched_svc
from .services import site_agents
from .services import logs as log_svc
from .services import media as media_svc
from .services import metrics as metrics_svc
from .services import p6 as p6_svc
from .services import progress as prog_svc
from .services import demo as demo_svc
from .services import qa as qa_svc
from .services import voice as voice_svc
from .services import reports as report_svc
from .services import role_views

log = logging.getLogger("zhimou")
STATIC = Path(__file__).resolve().parent / "static"
VERSION = "B-1.0.0"

@asynccontextmanager
async def lifespan(_app):
    init_db()
    if os.environ.get("ZB_SCHEDULER", "1") != "0":
        threading.Thread(target=_scheduler, daemon=True, name="detect-scheduler").start()
    yield


app = FastAPI(title="智眸工笔", version=VERSION, docs_url=None, redoc_url=None, lifespan=lifespan)


# ----------------------------------------------------------------------------- 基础设施

def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _req_names(request: Request):
    """本次请求用户所在项目的作业简称表（services/act_names.py）；未登录或没有计划时为空。"""
    token = request.cookies.get("zb_token") or ""
    payload = read_token(token) if token else None
    if not payload:
        return None
    conn = connect()
    try:
        u = one(conn, "SELECT project_id FROM users WHERE id=?", (payload.get("u"),))
        return act_names.names(conn, u["project_id"]) if u and u["project_id"] else None
    finally:
        conn.close()


async def act_names_out(request: Request, call_next):
    """接口出口：展示文字里的 P6 作业代码换成简短中文名，带代码的对象补 short_name/activity_short（代码字段原样保留）。"""
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    try:
        mp = _req_names(request)
    except Exception:  # noqa: BLE001  取不到简称表不影响业务
        log.exception("作业简称表读取失败")
        mp = None
    if not mp:
        return await call_next(request)
    act_names.CURRENT.set(mp)
    resp = await call_next(request)
    if not (resp.headers.get("content-type") or "").startswith("application/json"):
        return resp
    body = b"".join([c async for c in resp.body_iterator])
    try:
        body = json.dumps(act_names.enrich(json.loads(body), mp), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError):
        pass
    out = Response(body, status_code=resp.status_code, media_type="application/json")
    out.raw_headers = [(k, v) for k, v in resp.raw_headers if k.lower() not in (b"content-length", b"content-type")] +         [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    return out


@app.middleware("http")
async def guard(request: Request, call_next):
    # 防 CSRF：所有写操作必须带自定义请求头（浏览器跨站表单无法附加）
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-zb") != "1":
        return JSONResponse({"detail": "缺少请求头"}, status_code=403)
    resp = await act_names_out(request, call_next)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@app.exception_handler(log_svc.Conflict)
async def _conflict(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(AIError)
async def _ai(_, exc):
    from .ai.client import explain
    return JSONResponse({"detail": explain(exc), "ai_error": getattr(exc, "kind", "other")}, status_code=502)


@app.exception_handler(ValueError)
async def _value(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def current_user(request: Request, conn=Depends(db)) -> dict:
    token = request.cookies.get("zb_token") or ""
    payload = read_token(token) if token else None
    if not payload:
        raise HTTPException(401, "请先登录")
    user = one(conn, "SELECT * FROM users WHERE id=? AND active=1", (payload["u"],))
    if not user or user["password_hash"][-12:] != payload.get("p"):
        raise HTTPException(401, "登录已失效")
    return demo_svc.act_as(user, request.cookies)   # 竞赛演示账号按所选岗位处理（services/demo.py）；其他账号原样返回


def need(cap: str):
    """按能力校验的依赖：能力表见 roles.CAPS。"""
    def dep(user=Depends(current_user)) -> dict:
        if not roles_mod.can(user["role"], cap):
            raise HTTPException(403, "当前角色无此操作权限")
        return user
    return dep


def viewer(user=Depends(current_user)) -> dict:
    """项目级汇总数据：港湾各角色、领导和管理员可看；分包单位只看自己单位范围（走专门接口）。"""
    if user["role"] not in roles_mod.VIEW_ROLES:
        raise HTTPException(403, "需要港湾项目部权限")
    return user


def admin(user=Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def sub_user(user=Depends(current_user)) -> dict:
    if user["role"] not in roles_mod.SUB_ROLES:
        raise HTTPException(403, "仅分包单位账号")
    return user


def public_user(u: dict) -> dict:
    return {**{k: u[k] for k in ("id", "username", "name", "role", "project_id", "wbs_scope", "email", "active", "company")},
            "role_name": roles_mod.role_name(u["role"]), "org": ROLES.get(u["role"], {}).get("org", "")}


def _log_or_404(conn, log_id: int, user: dict) -> dict:
    row = log_svc.get_log(conn, log_id)
    if not row or row["project_id"] != user["project_id"]:
        raise HTTPException(404, "日志不存在")
    if user["role"] == "sub_safety":
        raise HTTPException(403, "安全员不处理施工日志")
    if user["role"] == "tech" and row["author_id"] != user["id"]:
        raise HTTPException(403, "只能查看自己的日志")
    return row


# ----------------------------------------------------------------------------- 账号

class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response, conn=Depends(db)):
    name = body.username.strip()
    u = one(conn, "SELECT * FROM users WHERE username=? AND active=1", (name,))
    if not u or not verify_password(body.password, u["password_hash"]):
        audit(conn, u["id"] if u else None, "login.fail", detail=f"ip={client_ip(request)}" + ("" if u else f" 账号={name[:40]}"))
        time.sleep(0.5)
        raise HTTPException(401, "用户名或密码错误")
    response.set_cookie("zb_token", issue_token(u["id"], u["password_hash"]), httponly=True, samesite="lax", max_age=14 * 86400)
    audit(conn, u["id"], "login", detail=f"ip={client_ip(request)}")
    return public_user(u)


def client_ip(request: Request) -> str:
    """访客地址：经 cloudflared 隧道时取 CF-Connecting-IP，其次 X-Forwarded-For 第一个，都没有取直连地址。只用于审计记录，不做权限判断。"""
    ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
    return (ip.strip() or (request.client.host if request.client else ""))[:64]


# 开发态一键登录：默认关闭；仅在 .env 设置 ZB_DEV_LOGIN=1 且请求来自本机/局域网时可用。正式部署不设置该变量即可。
def _dev_login_allowed(request: Request) -> bool:
    if os.environ.get("ZB_DEV_LOGIN", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "testclient"
    return ip.is_loopback or ip.is_private


@app.get("/api/dev/accounts")
def dev_accounts(request: Request, conn=Depends(db)):
    if not _dev_login_allowed(request):
        return []
    rows = all_(conn, "SELECT u.id, u.username, u.name, u.role, u.company, p.name AS project, p.code AS project_code FROM users u "
                      "LEFT JOIN projects p ON p.id=u.project_id WHERE u.active=1 ORDER BY u.id")
    # ZB_DEV_PROJECT=项目编码：快捷登录只列这个项目的账号（如 SIM-ROAD）；库里没有该项目的账号时照常列全部
    code = os.environ.get("ZB_DEV_PROJECT", "").strip()
    rows = [r for r in rows if r["project_code"] == code] or rows if code else rows
    order = {r: i for i, r in enumerate(roles_mod.ROLE_ORDER)}
    return [{**r, "role_name": roles_mod.role_name(r["role"]), "org": ROLES.get(r["role"], {}).get("org", "")} for r in sorted(rows, key=lambda r: (order.get(r["role"], 99), r["id"]))]


class DevLoginIn(BaseModel):
    user_id: int


@app.post("/api/dev/login")
def dev_login(body: DevLoginIn, request: Request, response: Response, conn=Depends(db)):
    if not _dev_login_allowed(request):
        raise HTTPException(404, "未开启")
    u = one(conn, "SELECT * FROM users WHERE id=? AND active=1", (body.user_id,))
    if not u:
        raise HTTPException(404, "账号不存在")
    response.set_cookie("zb_token", issue_token(u["id"], u["password_hash"]), httponly=True, samesite="lax", max_age=14 * 86400)
    audit(conn, u["id"], "dev_login", detail=f"ip={client_ip(request)}")
    return public_user(u)


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("zb_token")
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user), conn=Depends(db)):
    project = one(conn, "SELECT id, name, code, config_json FROM projects WHERE id=?", (user["project_id"],))
    unread = one(conn, "SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read_at IS NULL", (user["id"],))["n"]
    imp = p6_svc.current_import(conn, user["project_id"])
    projects = all_(conn, "SELECT id, name, code FROM projects ORDER BY id") if user["role"] in ("admin", "leader") else []
    return {"user": public_user(user), "can": roles_mod.caps_of(user["role"]), "home": ROLES[user["role"]]["home"],
            "project": {**project, "config": loads(project["config_json"], {}), "features": feat_svc.flags_from_cfg(loads(project["config_json"], {}))} if project else None,
            "projects": projects, "demo": demo_svc.info(user),
            "ai": provider_status(), "unread": unread, "version": VERSION, "today": date.today().isoformat(),
            "p6": {"file": imp["file_name"], "data_date": imp["data_date"], "activities": imp["activity_count"]} if imp else None}


class PasswordIn(BaseModel):
    old: str
    new: str = Field(min_length=6)


@app.put("/api/me/password")
def change_password(body: PasswordIn, response: Response, user=Depends(current_user), conn=Depends(db)):
    if not verify_password(body.old, user["password_hash"]):
        raise HTTPException(400, "原密码错误")
    h = hash_password(body.new)
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (h, user["id"]))
    response.set_cookie("zb_token", issue_token(user["id"], h), httponly=True, samesite="lax", max_age=14 * 86400)
    return {"ok": True}


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=40)
    role: str
    password: str | None = None
    wbs_scope: str = ""
    email: str = ""
    company: str = Field("", max_length=60)
    active: bool = True


@app.get("/api/roles")
def roles_info(user=Depends(current_user)):
    return {"roles": [{"key": k, **ROLES[k], "caps": roles_mod.caps_of(k)} for k in roles_mod.ROLE_ORDER], "orgs": roles_mod.ORG_NAME}


@app.get("/api/users")
def list_users(user=Depends(viewer), conn=Depends(db)):
    rows = [public_user(u) for u in all_(conn, "SELECT * FROM users WHERE project_id=? ORDER BY id", (user["project_id"],))]
    order = {r: i for i, r in enumerate(roles_mod.ROLE_ORDER)}
    rows.sort(key=lambda u: (order.get(u["role"], 99), u["id"]))
    if user["role"] == "admin":
        last = {r["user_id"]: r["last"] for r in all_(conn, "SELECT user_id, MAX(created_at) last FROM audit WHERE action IN ('login','dev_login') GROUP BY user_id")}
        for u in rows:
            u["last_login"] = last.get(u["id"])
    return rows


@app.post("/api/users")
def create_user(body: UserIn, user=Depends(admin), conn=Depends(db)):
    if body.role not in ROLES:
        raise HTTPException(400, "角色无效")
    if not body.password or len(body.password) < 6:
        raise HTTPException(400, "初始密码至少6位")
    if one(conn, "SELECT 1 FROM users WHERE username=?", (body.username,)):
        raise HTTPException(400, "用户名已存在")
    uid = insert(conn, "users", {"username": body.username.strip(), "name": body.name.strip(), "role": body.role,
                                 "password_hash": hash_password(body.password), "project_id": user["project_id"],
                                 "wbs_scope": body.wbs_scope.strip(), "email": body.email.strip(), "active": 1, "created_at": now_iso(),
                                 "company": body.company.strip() if roles_mod.is_sub(body.role) else ""})
    audit(conn, user["id"], "user.create", f"user:{uid}", body.role)
    return public_user(one(conn, "SELECT * FROM users WHERE id=?", (uid,)))


@app.put("/api/users/{uid}")
def update_user(uid: int, body: UserIn, user=Depends(admin), conn=Depends(db)):
    target = one(conn, "SELECT * FROM users WHERE id=? AND project_id=?", (uid, user["project_id"]))
    if not target:
        raise HTTPException(404, "用户不存在")
    if body.role not in ROLES:
        raise HTTPException(400, "角色无效")
    if uid == user["id"] and (body.role != "admin" or not body.active):
        raise HTTPException(400, "不能降低或停用自己的管理员账号")
    ph = hash_password(body.password) if body.password else target["password_hash"]
    conn.execute("UPDATE users SET name=?, role=?, wbs_scope=?, email=?, active=?, password_hash=?, company=? WHERE id=?",
                 (body.name, body.role, body.wbs_scope, body.email, 1 if body.active else 0, ph,
                  body.company.strip() if roles_mod.is_sub(body.role) else "", uid))
    audit(conn, user["id"], "user.update", f"user:{uid}")
    return public_user(one(conn, "SELECT * FROM users WHERE id=?", (uid,)))


class ProjectIn(BaseModel):
    name: str
    thresholds: dict[str, float] = {}
    workdays_per_week: float | None = Field(None, ge=5, le=7)
    cadence: str | None = None
    daily_log: bool | None = None
    social_enabled: bool | None = None
    features: dict[str, bool] | None = None   # 增强能力的启用/隐藏；只认 services/features.py 里的键


@app.put("/api/project")
def update_project(body: ProjectIn, user=Depends(admin), conn=Depends(db)):
    p = one(conn, "SELECT * FROM projects WHERE id=?", (user["project_id"],))
    cfg = loads(p["config_json"], {})
    cfg["thresholds"] = {k: float(v) for k, v in body.thresholds.items() if k in prog_svc.DEFAULT_THRESHOLDS}
    if body.workdays_per_week:
        cfg["workdays_per_week"] = body.workdays_per_week
    if body.cadence in ("day", "week"):
        cfg["cadence"] = body.cadence
    if body.daily_log is not None:
        cfg["daily_log"] = body.daily_log
    if body.social_enabled is not None:
        cfg["social_enabled"] = body.social_enabled
    if body.features is not None:
        feat_svc.merge(cfg, body.features)
    conn.execute("UPDATE projects SET name=?, config_json=? WHERE id=?", (body.name.strip() or p["name"], dumps(cfg), p["id"]))
    audit(conn, user["id"], "project.update", f"project:{p['id']}", dumps(cfg))
    return {"ok": True, "thresholds": prog_svc.thresholds(conn, p["id"]), "features": feat_svc.flags_from_cfg(cfg)}


@app.get("/api/project/features")
def get_features(user=Depends(current_user), conn=Depends(db)):
    """增强能力开关（默认全部启用）：项目设置页读取；隐藏只影响导航入口与首页待办/提醒来源，旧路由仍可直达。"""
    fl = feat_svc.flags(conn, user["project_id"])
    return [{"key": k, "name": n, "desc": d, "enabled": fl[k]} for k, n, d in feat_svc.FEATURES]


@app.get("/api/project/thresholds")
def get_thresholds(user=Depends(viewer), conn=Depends(db)):
    return {"values": prog_svc.thresholds(conn, user["project_id"]), "defaults": prog_svc.DEFAULT_THRESHOLDS}


@app.post("/api/ai/test")
def ai_test(user=Depends(admin)):
    res = call_json("ai.test", system="只输出JSON。", user='请输出 {"ok": true, "model": "你的模型名称"}', prompt_version="test",
                    mock=lambda: {"ok": True, "model": "mock"}, project_id=user["project_id"], user_id=user["id"], max_tokens=60)
    return {"ok": True, "latency_ms": res.latency_ms, "provider": res.provider, "model": res.model, "reply": res.data}


# ----------------------------------------------------------------------------- 照片

def _safety_bg(media_ids: list[int], user_id: int) -> None:
    conn = connect()
    try:
        site_agents.safety_scan(conn, media_ids, user_id)
    except Exception:  # noqa: BLE001
        log.exception("安全Agent后台识别失败")
    finally:
        conn.close()


@app.post("/api/media")
def upload_media(bg: BackgroundTasks, files: list[UploadFile] = File(...), log_id: int | None = Form(None), purpose: str = Form("log"),
                 taken_at: list[str] = Form([]), geo_lat: list[str] = Form([]), geo_lon: list[str] = Form([]), geo_acc: list[str] = Form([]),
                 place: list[str] = Form([]), scan: int = Form(1), user=Depends(current_user), conn=Depends(db)):
    """scan=0：只上传不做隐患识别（拍摄者在上传界面选择）；识别在后台进行，上传本身不等识别。"""
    if len(files) > 20:
        raise HTTPException(400, "一次最多上传20张")
    lg = _log_or_404(conn, log_id, user) if log_id else None
    out = []
    for i, f in enumerate(files):
        raw = f.file.read(media_svc.MAX_UPLOAD + 1)
        try:
            m = media_svc.save_photo(conn, user["project_id"], user["id"], f.filename or "", raw, purpose,
                                     taken_hint=taken_at[i] if i < len(taken_at) else None,
                                     geo=media_svc.parse_geo(*(v[i] if i < len(v) else None for v in (geo_lat, geo_lon, geo_acc, place))),
                                     scan=bool(scan))
        except media_svc.MediaError as exc:
            raise HTTPException(400, f"{f.filename}: {exc}") from None
        if lg:
            log_svc.attach_photo(conn, lg, m["id"], user)
            m = one(conn, "SELECT * FROM media WHERE id=?", (m["id"],))
        out.append(media_svc.public(m))
    scan = [m["id"] for m in out if m["purpose"] in ("log", "safety") and m["safety_status"] == "pending"]
    if scan and purpose != "rectify":
        bg.add_task(_safety_bg, scan, user["id"])
    return out


def _media_file(conn, mid: int, user: dict, field: str):
    m = one(conn, "SELECT * FROM media WHERE id=? AND project_id=?", (mid, user["project_id"]))
    if not m:
        raise HTTPException(404, "照片不存在")
    # 分包单位可看本人/本单位范围的照片，以及现场圈里的照片（现场圈全项目可见）
    if roles_mod.is_sub(user["role"]) and not role_views.in_scope(conn, user, m["uploader_id"]) and not feed_svc.media_in_circle(conn, user, m):
        raise HTTPException(403, "无权查看")
    path = (settings.media_dir / m[field]).resolve()
    if settings.media_dir.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "文件缺失")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/media/{mid}/thumb")
def media_thumb(mid: int, user=Depends(current_user), conn=Depends(db)):
    return _media_file(conn, mid, user, "thumb")


@app.get("/api/media/{mid}/file")
def media_full(mid: int, user=Depends(current_user), conn=Depends(db)):
    return _media_file(conn, mid, user, "ai_file")


# ----------------------------------------------------------------------------- 日志

class LogCreate(BaseModel):
    log_date: str | None = None
    mode: str = "ai"
    period: str = "day"


# 项目经理不审核日志（AGENTS.md）：日志详情去掉数据审核、规则校验、审核状态与审核人/意见，退回失效的原数量（附退回原因）与逐日填报情况也不给
# 编写用时、相对 AI 草稿修改率属应用成效（roles.hides_metrics），同样不给项目经理
PM_LOG_HIDDEN = ("dq", "checks", "status", "reviewer", "reviewed_at", "review_note", "edit_seconds", "edit_ratio")


def _pm_log_view(v: dict) -> dict:
    for k in PM_LOG_HIDDEN:
        v.pop(k, None)
    v["dq_review"] = None  # 日志预审本来就只给工程主管（_log_view 里非工程主管为 None），保留空键与其他岗位一致
    v["days"] = None
    v["progress_rows"] = {"active": v["progress_rows"]["active"], "voided": []}
    return v


def log_view(conn, row: dict, viewer: dict) -> dict:
    v = _log_view(conn, row, viewer)
    return _pm_log_view(v) if viewer["role"] in roles_mod.PM_SCOPE else v


def _log_view(conn, row: dict, viewer: dict) -> dict:
    author = one(conn, "SELECT * FROM users WHERE id=?", (row["author_id"],))
    reviewer = one(conn, "SELECT name FROM users WHERE id=?", (row["reviewed_by"],)) if row["reviewed_by"] else None
    run = one(conn, "SELECT provider, model, latency_ms, prompt_version FROM ai_runs WHERE id=?", (row["ai_run_id"],)) if row["ai_run_id"] else None
    photos = [media_svc.public(m) for m in log_svc.photos_of(conn, row["id"])]
    acts = p6_svc.current_activities(conn, row["project_id"])
    if row["period"] == "week":
        window = p6_svc.range_activities(acts, row["log_date"], row["period_end"], author.get("wbs_scope", ""), limit=80)
    else:
        window = p6_svc.window_activities(acts, row["log_date"], author.get("wbs_scope", ""), limit=80)
    return {
        **{k: row[k] for k in ("id", "log_date", "mode", "status", "weather", "voice_text", "started_at", "drafted_at", "submitted_at",
                               "edit_seconds", "edit_ratio", "reviewed_at", "review_note", "version")},
        "author": {"id": author["id"], "name": author["name"]}, "reviewer": reviewer["name"] if reviewer else None,
        "content": loads(row["content_json"], {}), "has_ai_draft": bool(row["ai_draft_json"]),
        "imported": bool(one(conn, "SELECT 1 FROM xlsx_imports WHERE log_id=? AND confirmed_at IS NOT NULL LIMIT 1", (row["id"],))),  # 内容来自 Excel 日报导入
        "checks": loads(row["checks_json"], []), "ai_run": run, "photos": photos, "qa": loads(row["qa_json"], []),
        "dq": loads(row["dq_json"]), "hazards": [find_svc.view(f) for f in all_(conn, """SELECT f.* FROM findings f JOIN media m ON f.ref_type='media'
                    AND f.ref_id=m.id WHERE m.log_id=? AND f.agent='safety' ORDER BY f.id""", (row["id"],))],
        "activities": [{"code": a["code"], "name": a["name"], "wbs_path": a["wbs_path"], "is_critical": a["is_critical"],
                        "early_start": a["early_start"], "early_finish": a["early_finish"]} for a in window],
        "period": row["period"], "period_end": row["period_end"], "auto": bool(row["auto"]),
        "pending_notes": log_svc.pending_notes(conn, row),  # 草稿被人工改过后未并入的新随手记条数（界面提示，不自动覆盖）
        "progress_rows": log_svc.progress_rows_of(conn, row["id"]),  # 已计入的工程量 + 已退回失效的原数量（留痕）
        "days": log_svc.week_days(conn, row) if row["period"] == "week" else None,
        "notes": [{"id": n["id"], "time": n["created_at"][11:16], "text": n["text"], "media": loads(n["media_json"], [])}
                  for n in log_svc.notes_of(conn, row["project_id"], row["author_id"], row["log_date"])] if row["period"] == "day" else None,
        "week_log_id": (one(conn, """SELECT id FROM logs WHERE project_id=? AND author_id=? AND period='week' AND mode='ai' AND log_date<=? AND period_end>=?""",
                            (row["project_id"], row["author_id"], row["log_date"], row["log_date"])) or {}).get("id") if row["period"] == "day" else None,
        "editable": row["author_id"] == viewer["id"] and row["status"] in ("draft", "returned"),
        # “AI建议｜依据｜人工决定”核对行：周进度确认只给作者本人；日志预审只给工程主管（项目经理不审核日志）
        "week_review": log_svc.week_review(conn, row, author) if row["period"] == "week" and row["mode"] == "ai" and row["author_id"] == viewer["id"] else None,
        "dq_review": ({"rows": log_svc.dq_review_rows(conn, row), "reviewable": row["status"] == "submitted" and row["author_id"] != viewer["id"]}
                      if viewer["role"] == "eng_sup" and row["dq_json"] else None),
    }


@app.get("/api/logs")
def list_logs(status: str | None = None, date_from: str | None = None, date_to: str | None = None, mode: str = "ai",
              author_id: int | None = None, period: str = "", user=Depends(current_user), conn=Depends(db)):
    if user["role"] == "sub_safety":
        raise HTTPException(403, "安全员不处理施工日志")
    if user["role"] in roles_mod.PM_SCOPE:  # 分包日志列表（审核状态、数据审核风险）由工程主管跟进；项目经理只从进度/质量/安全的出处打开单份日志
        raise HTTPException(403, "施工日志与周确认不在项目经理的查看范围")
    q = """SELECT l.id, l.log_date, l.mode, l.status, l.submitted_at, l.edit_seconds, l.edit_ratio, l.content_json, l.checks_json, l.dq_json,
                  l.period, l.period_end, l.auto, l.voice_text, u.name author, u.id author_id, (SELECT COUNT(*) FROM media m WHERE m.log_id=l.id) photos,
                  (SELECT COUNT(*) FROM notes n WHERE n.project_id=l.project_id AND n.author_id=l.author_id AND n.note_date=l.log_date) notes
           FROM logs l JOIN users u ON u.id=l.author_id WHERE l.project_id=?"""
    params: list[Any] = [user["project_id"]]
    if mode in ("ai", "manual"):
        q += " AND l.mode=?"
        params.append(mode)
    if period in ("day", "week"):
        q += " AND l.period=?"
        params.append(period)
    if user["role"] == "tech":
        q += " AND l.author_id=?"
        params.append(user["id"])
    elif author_id:
        q += " AND l.author_id=?"
        params.append(author_id)
    for col, val, op in (("l.status", status, "="), ("l.log_date", date_from, ">="), ("l.log_date", date_to, "<=")):
        if val:
            q += f" AND {col}{op}?"
            params.append(val)
    q += " ORDER BY l.log_date DESC, l.id DESC LIMIT 300"
    out = []
    for r in all_(conn, q, params):
        c = loads(r.pop("content_json"), {})
        checks = loads(r.pop("checks_json"), [])
        dq = loads(r.pop("dq_json"))
        voice = (r.pop("voice_text") or "").strip()
        # 空草稿：草稿状态且没有任何内容（摘要/施工/进度/问题/口述/照片/当天随手记），由后端按内容判定，前端只认这个字段
        empty = r["status"] == "draft" and not (c.get("summary") or c.get("works") or c.get("progress") or c.get("issues") or voice
                                                or r["photos"] or (r["period"] == "day" and r["notes"]))
        out.append({**r, "empty": bool(empty), "summary": c.get("summary", ""), "issues": len(c.get("issues", [])),
                    "flags": sum(1 for x in checks if x["level"] in ("error", "warn")),
                    "dq_risk": dq["risk"] if dq else None, "dq_score": dq["score"] if dq else None, "dq_summary": dq["summary"] if dq else ""})
    return out


@app.post("/api/logs")
def create_log(body: LogCreate, user=Depends(current_user), conn=Depends(db)):
    row = log_svc.ensure_log(conn, user, body.log_date, body.mode, body.period)
    return log_view(conn, row, user)


@app.get("/api/logs/{log_id}")
def get_log(log_id: int, user=Depends(current_user), conn=Depends(db)):
    return log_view(conn, _log_or_404(conn, log_id, user), user)


@app.delete("/api/logs/{log_id}/photos/{mid}")
def detach(log_id: int, mid: int, user=Depends(current_user), conn=Depends(db)):
    row = _log_or_404(conn, log_id, user)
    log_svc.detach_photo(conn, row, mid, user)
    return log_view(conn, log_svc.get_log(conn, log_id), user)


class DraftIn(BaseModel):
    voice_text: str | None = Field(None, max_length=4000)
    weather: str | None = Field(None, max_length=100)


@app.post("/api/logs/{log_id}/draft")
def draft(log_id: int, body: DraftIn, user=Depends(current_user), conn=Depends(db)):
    row = _log_or_404(conn, log_id, user)
    return log_view(conn, log_svc.draft(conn, row, user, body.voice_text, body.weather), user)


class LogSave(BaseModel):
    content: dict
    version: int
    weather: str | None = None
    voice_text: str | None = None


@app.put("/api/logs/{log_id}")
def save_log(log_id: int, body: LogSave, user=Depends(current_user), conn=Depends(db)):
    row = _log_or_404(conn, log_id, user)
    saved = log_svc.save(conn, row, user, body.content, body.version, body.weather, body.voice_text)
    if saved["mode"] == "ai":
        log_svc.recheck(conn, saved, user)
    return log_view(conn, log_svc.get_log(conn, log_id), user)


class SubmitIn(BaseModel):
    version: int
    active_seconds: float | None = None
    decisions: list[dict] | None = None  # 周进度确认逐行决定：[{key, decision, final?, reason?}]，给出则须覆盖全部作业


def _after_submit(project_id: int, user_id: int, log_id: int) -> None:
    conn = connect()
    try:
        lg = one(conn, "SELECT * FROM logs WHERE id=?", (log_id,))
        pending = [m["id"] for m in all_(conn, "SELECT id FROM media WHERE log_id=? AND safety_status='pending'", (log_id,))]
        if pending:
            site_agents.safety_scan(conn, pending, user_id)
        if lg["period"] == "day":
            site_agents.quality_from_log(conn, lg)
        site_agents.dq_review(conn, log_id, user_id)
    except Exception:  # noqa: BLE001
        log.exception("提交后Agent处理失败")
    finally:
        conn.close()
    _detect_bg(project_id, user_id)


def _detect_bg(project_id: int, user_id: int | None) -> None:
    conn = connect()
    try:
        if p6_svc.current_import(conn, project_id):
            sched_svc.forecast(conn, project_id)
        res = prog_svc.detect(conn, project_id)
        if res["new"] or res["escalated"]:
            alert_svc.process_new(conn, res["new"] + res["escalated"], user_id)
        event(conn, "detect.auto", project_id=project_id, user_id=user_id,
              data={"new": len(res["new"]), "escalated": len(res["escalated"]), "cleared": res["cleared"]})
    except Exception:  # noqa: BLE001 — 后台判定失败不影响日志提交
        log.exception("后台预警判定失败")
    finally:
        conn.close()


@app.post("/api/logs/{log_id}/submit")
def submit_log(log_id: int, body: SubmitIn, bg: BackgroundTasks, user=Depends(current_user), conn=Depends(db)):
    row = _log_or_404(conn, log_id, user)
    saved = log_svc.submit(conn, row, user, body.version, body.active_seconds, body.decisions)
    if saved["mode"] == "ai":
        bg.add_task(_after_submit, user["project_id"], user["id"], log_id)
    return log_view(conn, saved, user)


class ReviewIn(BaseModel):
    action: str
    note: str = ""
    decisions: list[dict] | None = None  # 日志预审逐条决定：[{key, decision: true|false, reason?}]，误报必填原因


@app.post("/api/logs/{log_id}/review")
def review_log(log_id: int, body: ReviewIn, user=Depends(need("log_review")), conn=Depends(db)):
    row = _log_or_404(conn, log_id, user)
    out = log_svc.review(conn, row, user, body.action, body.note, body.decisions)
    if body.action == "return" and row["author_id"] != user["id"]:
        # 退回通知作者，点消息直达该日志（周确认单同样按 id 打开，不会落到别的周）
        what = f"{row['log_date'][5:]} 起一周进度确认" if row.get("period") == "week" else f"{row['log_date']} 施工日志"
        link = {"route": "log", "params": {"id": str(row["id"])}}
        insert(conn, "notifications", {"project_id": row["project_id"], "user_id": row["author_id"], "alert_id": None,
                                       "title": f"{what}被退回",
                                       "body": f"{user['name']}（{roles_mod.role_name(user['role'])}）退回：{body.note.strip()}。改好后重新提交。",
                                       "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(), "link_json": dumps(link)})
    return log_view(conn, out, user)


# ----------------------------------------------------------------------------- P6

@app.post("/api/p6/import")
def p6_import(bg: BackgroundTasks, file: UploadFile = File(...), data_date: str | None = Form(None),
                    user=Depends(need("progress_ops")), conn=Depends(db)):
    raw = file.file.read(60 * 1024 * 1024 + 1)
    if data_date:
        date.fromisoformat(data_date)
    res = p6_svc.import_schedule(conn, user["project_id"], user["id"], file.filename or "schedule", raw, data_date or None)
    bg.add_task(_detect_bg, user["project_id"], user["id"])
    return res


@app.get("/api/p6/imports")
def p6_imports(user=Depends(viewer), conn=Depends(db)):
    return all_(conn, """SELECT i.id, i.file_name, i.proj_name, i.data_date, i.activity_count, i.relation_count, i.is_current, i.imported_at,
                         u.name imported_by FROM p6_imports i LEFT JOIN users u ON u.id=i.imported_by WHERE i.project_id=? ORDER BY i.id DESC""",
                (user["project_id"],))


@app.get("/api/p6/activities")
def p6_activities(q: str = "", only: str = "", user=Depends(viewer), conn=Depends(db)):
    pid = user["project_id"]
    imp = p6_svc.current_import(conn, pid)
    if not imp:
        return {"import": None, "items": []}
    acts = p6_svc.current_activities(conn, pid)
    state = log_svc.progress_state(conn, pid, imp)
    ev = {r["activity"]["code"]: r for r in prog_svc.evaluate(conn, pid)["results"]}
    today, dd = date.today(), date.fromisoformat(imp["data_date"])
    items = []
    for a in acts:
        if q and q not in a["code"] and q not in a["name"] and q not in (a["wbs_path"] or ""):
            continue
        if only == "critical" and not a["is_critical"]:
            continue
        if only == "alert" and a["code"] not in ev:
            continue
        if only == "active" and (a["act_finish"] or not (a["act_start"] or a["code"] in state)):
            continue
        st = state.get(a["code"]) or {}
        r = ev.get(a["code"])
        plan = prog_svc.planned_pct(a, today, dd)
        items.append({**{k: a[k] for k in ("code", "name", "wbs_path", "status", "early_start", "early_finish", "act_start", "act_finish",
                                           "total_float_d", "phys_pct", "is_critical", "is_milestone", "orig_dur_d")},
                      "log_pct": st.get("pct"), "log_pct_date": st.get("pct_date"), "log_qty": round(st["qty"], 2) if st.get("qty") else None,
                      "log_qty_self": round(st["qty_self"], 2) if st.get("qty_self") else None,
                      "log_qty_approved": round(st["qty_approved"], 2) if st.get("qty_approved") else None,
                      "log_ids_self": st.get("log_ids_self", []), "log_ids_approved": st.get("log_ids_approved", []),
                      "log_trust": log_svc.trust_label(st),
                      "log_unit": st.get("unit"), "reports": st.get("reports", 0), "plan_pct": round(plan, 1) if plan is not None else None,
                      "level": r["level"] if r else None, "slip": r["facts"].get("slip_days") if r else None})
        if len(items) >= 800:
            break
    return {"import": imp, "items": items}


@app.get("/api/p6/export-progress")
def p6_export(user=Depends(need("progress_ops")), conn=Depends(db)):
    """导出日报确认的进度，供计划工程师在 P6 中更新（不直接写 P6 库）。"""
    from openpyxl import Workbook

    pid = user["project_id"]
    imp = p6_svc.current_import(conn, pid)
    if not imp:
        raise HTTPException(400, "尚未导入 P6 计划")
    state = log_svc.progress_state(conn, pid, imp)
    acts = {a["code"]: a for a in p6_svc.current_activities(conn, pid)}
    wb = Workbook()
    ws = wb.active
    ws.title = "TASK"
    ws.append(["task_code", "task_name", "act_start_date", "act_end_date", "phys_complete_pct", "日报最新日期", "数据日期后累计量", "单位", "依据日报数",
               "qty_self", "qty_approved", "approval_status"])
    ws.append(["作业代码", "作业名称", "实际开始", "实际完成", "实物完成%", "日报最新日期", "累计量", "单位", "日报份数",
               "其中分包自报", "其中港湾核定", "核定状态"])
    for code, st in sorted(state.items()):
        a = acts.get(code)
        if not a:
            continue
        act_start = a["act_start"] or st["first_date"]
        act_end = st["pct_date"] if st.get("pct") is not None and st["pct"] >= 100 else None
        ws.append([code, a["name"], act_start, act_end, st.get("pct"), st["last_date"], round(st["qty"], 2) if st["qty"] else None,
                   st["unit"], st["reports"], round(st["qty_self"], 2) or None, round(st["qty_approved"], 2) or None,
                   log_svc.trust_label(st)])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    audit(conn, user["id"], "p6.export", f"import:{imp['id']}")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename=p6_progress_{date.today().isoformat()}.xlsx"})


# ----------------------------------------------------------------------------- 预警

class DetectIn(BaseModel):
    as_of: str | None = None


@app.post("/api/alerts/detect")
def run_detect(body: DetectIn, bg: BackgroundTasks, user=Depends(need("progress_ops")), conn=Depends(db)):
    res = prog_svc.detect(conn, user["project_id"], body.as_of)
    ids = res["new"] + res["escalated"]
    if ids:
        def work():
            c = connect()
            try:
                alert_svc.process_new(c, ids, user["id"])
            finally:
                c.close()
        bg.add_task(work)
    event(conn, "detect.manual", project_id=user["project_id"], user_id=user["id"], data={"new": len(res["new"])})
    return {"as_of": res["as_of"], "monitored": res.get("monitored", 0), "triggered": len(res["results"]), "new": len(res["new"]),
            "escalated": len(res["escalated"]), "cleared": res["cleared"], "ai_pending": len(ids)}


@app.get("/api/alerts")
def list_alerts(status: str = "", user=Depends(viewer), conn=Depends(db)):
    q, params = "SELECT * FROM alerts WHERE project_id=?", [user["project_id"]]
    if user["role"] not in prog_svc.GAP_VIEW_ROLES:  # 历史“数据缺口”旧预警只给工程主管看，不出现在项目经理等岗位的预警列表
        q += " AND rule<>'stale'"
    if status:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, CASE level WHEN 'red' THEN 0 WHEN 'orange' THEN 1 ELSE 2 END, last_detected DESC LIMIT 300"
    return [prog_svc.serialize_alert(a, conn) for a in all_(conn, q, params)]


@app.get("/api/alerts/stats")
def alert_stats(user=Depends(viewer), conn=Depends(db)):
    return prog_svc.feedback_stats(conn, user["project_id"])


@app.get("/api/alerts/{aid}")
def get_alert(aid: int, user=Depends(viewer), conn=Depends(db)):
    a = one(conn, "SELECT * FROM alerts WHERE id=? AND project_id=?", (aid, user["project_id"]))
    if not a:
        raise HTTPException(404, "预警不存在")
    if a["rule"] == prog_svc.GAP_RULE and user["role"] not in prog_svc.GAP_VIEW_ROLES:
        raise HTTPException(404, "预警不存在")
    out = prog_svc.serialize_alert(a, conn)
    out["logs"] = prog_svc.related_logs(conn, a["project_id"], a["activity_code"], a["as_of"])
    out["notifications"] = all_(conn, """SELECT n.created_at, n.channels_json, u.name FROM notifications n JOIN users u ON u.id=n.user_id
                                          WHERE n.alert_id=? ORDER BY n.id""", (aid,))
    return out


@app.post("/api/alerts/{aid}/explain")
def explain_alert(aid: int, user=Depends(need("progress_ops")), conn=Depends(db)):
    if not one(conn, "SELECT 1 FROM alerts WHERE id=? AND project_id=?", (aid, user["project_id"])):
        raise HTTPException(404, "预警不存在")
    return alert_svc.explain(conn, aid, user["id"])


@app.post("/api/alerts/{aid}/notify")
def renotify(aid: int, user=Depends(need("progress_ops")), conn=Depends(db)):
    if not one(conn, "SELECT 1 FROM alerts WHERE id=? AND project_id=?", (aid, user["project_id"])):
        raise HTTPException(404, "预警不存在")
    return alert_svc.notify(conn, aid)


class FeedbackIn(BaseModel):
    status: str
    note: str = ""


@app.post("/api/alerts/{aid}/feedback")
def alert_feedback(aid: int, body: FeedbackIn, user=Depends(need("progress_ops")), conn=Depends(db)):
    return alert_svc.feedback(conn, aid, user, body.status, body.note)


_FINDING_TITLE_PREFIXES = ("新派单：", "催办：", "整改待复核：")   # 这几类消息的标题是“前缀 + 隐患名（截断）”
_NUDGE_TITLE_PREFIXES = ("催办提醒：", "协助跟进：", "升级提醒：", "升级到经理：", "待决策：")   # 催办Agent 单件事项的标题（含旧版“待决策：”）
_FINDING_DONE = ("resolved", "dismissed")


def _title_tail(title: str) -> str:
    return title.split("：", 1)[1] if "：" in title else ""


def _notice_enrich(conn, user: dict, rows: list[dict]) -> None:
    """消息读取时补全（S-5③⑤、F-07、U-14）：
    - 旧消息没存跳转目标的反查补上：催办按 nudge_log.notif_id 取所催事项；没记 notif_id 的旧催办和派单/催办/整改待复核按标题里的隐患名回查隐患；
      旧的“请确认本周进度”提醒（技术员）补到周确认页；
    - 标题在入库时被截断的补“…”（与催办事项名或隐患全名比对，确是前缀才补）；
    - 所链事项已处理的标 state=done“已处理”，预警已关闭/误报的标 state=cleared“已解除”，未读的同时计为已读（settled=True）。"""
    from .services import nudge as nudge_svc  # 只在消息区用，延迟导入
    ids = [r["id"] for r in rows]
    nudges: dict[int, list[dict]] = {}
    if ids:
        q = ",".join("?" * len(ids))
        for n in all_(conn, f"SELECT notif_id, obj_type, obj_key, subject, link FROM nudge_log WHERE notif_id IN ({q})", ids):
            nudges.setdefault(n["notif_id"], []).append(n)
    fids: set[int] = set()
    for r in rows:
        if not r["link"] and not r["alert_id"]:
            if nudges.get(r["id"]):
                r["link"] = nudge_svc._notif_link([{"link": n["link"], "overdue": 0} for n in nudges[r["id"]]], user)
            elif r["title"].startswith(_NUDGE_TITLE_PREFIXES) and _title_tail(r["title"]) and (g := one(
                    conn, "SELECT notif_id, obj_type, obj_key, subject, link FROM nudge_log WHERE project_id=? AND to_user_id=? AND substr(subject, 1, ?)=? "
                          "ORDER BY id DESC LIMIT 1", (user["project_id"], user["id"], len(_title_tail(r["title"])), _title_tail(r["title"])))):
                # 没记 notif_id 的旧催办：按接收人 + 事项名找回催办记录，按它所催的事项给跳转（数据缺口等非隐患事项不会被错配到同名隐患）
                nudges[r["id"]] = [g]
                r["link"] = nudge_svc._notif_link([{"link": g["link"], "overdue": 0}], user)
            elif r["title"] == "请确认本周进度" and user["role"] == "tech":
                r["link"] = {"route": "week", "params": {}}
            elif r["title"].startswith(_FINDING_TITLE_PREFIXES + _NUDGE_TITLE_PREFIXES) and _title_tail(r["title"]):
                tail = _title_tail(r["title"])
                f = one(conn, "SELECT id FROM findings WHERE project_id=? AND substr(title, 1, ?)=? ORDER BY (assignee_id=?) DESC, id DESC LIMIT 1",
                        (user["project_id"], len(tail), tail, user["id"]))
                if f:
                    r["link"] = {"route": "finding", "params": {"id": str(f["id"])}}
        link = r["link"] or {}
        if link.get("route") == "finding" and str((link.get("params") or {}).get("id", "")).isdigit():
            fids.add(int(link["params"]["id"]))
        fids.update(int(n["obj_key"]) for n in nudges.get(r["id"], []) if n["obj_type"] == "finding" and str(n["obj_key"]).isdigit())
    finds = {}
    if fids:
        q = ",".join("?" * len(fids))
        finds = {f["id"]: f for f in all_(conn, f"SELECT id, title, status, assignee_id FROM findings WHERE project_id=? AND id IN ({q})",
                                          (user["project_id"], *fids))}
    aids = sorted({r["alert_id"] for r in rows if r["alert_id"]})
    alerts = {}
    if aids:
        alerts = {a["id"]: a["status"] for a in all_(conn, f"SELECT id, status FROM alerts WHERE id IN ({','.join('?' * len(aids))})", aids)}

    def finding_done(f: dict | None, title: str, nudged: bool) -> bool | None:
        if not f:
            return None
        if title.startswith("整改待复核"):  # 安全主管的待复核：人工闭环或退回后即处理完
            return f["status"] != "rectifying"
        if nudged:  # 催办只催 open/rejected 的事项，离开这两态即不再需要催
            return f["status"] not in ("open", "rejected")
        return f["status"] in _FINDING_DONE or (f["status"] == "rectifying" and f["assignee_id"] == user["id"])

    settle: list[int] = []
    for r in rows:
        link, ns = r["link"] or {}, nudges.get(r["id"], [])
        state = None
        if r["alert_id"]:
            state = "cleared" if alerts.get(r["alert_id"]) in ("closed", "false_alarm") else None
        elif ns and all(n["obj_type"] == "finding" for n in ns):
            done = [finding_done(finds.get(int(n["obj_key"])) if str(n["obj_key"]).isdigit() else None, r["title"], True) for n in ns]
            state = "done" if done and all(done) else None
        elif link.get("route") == "finding":
            fid = str((link.get("params") or {}).get("id", ""))
            state = "done" if fid.isdigit() and finding_done(finds.get(int(fid)), r["title"], r["title"].startswith(_NUDGE_TITLE_PREFIXES)) else None
        elif link.get("route") == "log" and "退回" in r["title"]:
            lg = one(conn, "SELECT status FROM logs WHERE id=? AND author_id=?", ((link.get("params") or {}).get("id"), user["id"]))
            state = "done" if lg and lg["status"] in ("submitted", "approved") else None
        elif link.get("route") == "week":
            day = (r["created_at"] or "")[:10]
            wk = one(conn, "SELECT 1 FROM logs WHERE project_id=? AND author_id=? AND period='week' AND status IN ('submitted','approved') "
                           "AND log_date<=? AND COALESCE(period_end, log_date)>=?", (user["project_id"], user["id"], day, day))
            state = "done" if wk else None
        r["state"] = state
        r["state_text"] = {"done": "已处理", "cleared": "已解除"}.get(state)
        # 截断的标题补“…”
        tail = _title_tail(r["title"])
        fulls = [n["subject"] for n in ns]
        if link.get("route") == "finding" and str((link.get("params") or {}).get("id", "")).isdigit():
            fulls.append((finds.get(int(link["params"]["id"])) or {}).get("title") or "")
        if tail and any(x and len(x) > len(tail) and x.startswith(tail) for x in fulls):
            r["title"] += "…"
        if state and not r["read_at"]:
            r["read_at"], r["settled"] = now_iso(), True
            settle.append(r["id"])
    if settle:
        conn.execute(f"UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL AND id IN ({','.join('?' * len(settle))})",
                     (now_iso(), user["id"], *settle))


@app.get("/api/notifications")
def notifications(limit: int = 100, offset: int = 0, user=Depends(current_user), conn=Depends(db)):
    # S-5②：按时间倒序（同一时刻按 id），分页取“更早的消息”
    rows = all_(conn, "SELECT id, alert_id, title, body, created_at, read_at, link_json FROM notifications WHERE user_id=? "
                      "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (user["id"], max(1, min(limit, 200)), max(0, offset)))
    # link：跳转目标 {"route", "params"}（评论→现场圈、派单/催办→隐患、周确认提醒→周确认、退回→该日志）；旧记录读取时反查
    for r in rows:
        r["link"] = loads(r.pop("link_json"))
    _notice_enrich(conn, user, rows)
    return rows


def _unread_count(conn, uid: int) -> int:
    return one(conn, "SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read_at IS NULL", (uid,))["n"]


@app.post("/api/notifications/read")
def read_all(user=Depends(current_user), conn=Depends(db)):
    conn.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (now_iso(), user["id"]))
    return {"ok": True}


@app.post("/api/notifications/{nid}/read")
def read_one(nid: int, user=Depends(current_user), conn=Depends(db)):
    """F-07：点开即标为已读，返回最新未读数给红点。"""
    if not one(conn, "SELECT 1 FROM notifications WHERE id=? AND user_id=?", (nid, user["id"])):
        raise HTTPException(404, "消息不存在")
    conn.execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=? AND read_at IS NULL", (now_iso(), nid, user["id"]))
    return {"ok": True, "unread": _unread_count(conn, user["id"])}


# ----------------------------------------------------------------------------- 报表

class ReportIn(BaseModel):
    kind: str
    ref_date: str | None = None


def report_view(r: dict, role: str = "", conn=None) -> dict:
    from .ai import report_agent
    facts = loads(r["facts_json"])
    if roles_mod.hides_fill_rate(role):  # 项目经理口径：应报份数、按时份数是填报率的分子分母，不返回（冻结的事实本身不改）
        facts["logs"] = {k: v for k, v in facts["logs"].items() if k not in ("expected", "on_time", "weeks_expected")}
    tpl = facts.get("template") or report_agent.default_template(r["kind"])
    nar, checks = loads(r["narrative_json"], {}), loads(r["number_check_json"], [])
    split = not roles_mod.hides_review_split(role)
    if not split:  # 项目经理口径：不含工程量自报/核定拆分；产值节只读照常显示（RJ 10-10）
        facts, tpl, nar, checks = report_svc.pm_scope(facts, tpl, nar, checks)
    tables = {}
    for sec in tpl["sections"]:
        if sec.get("table"):
            h, rows = report_svc.table_rows(facts, sec["table"], split=split)
            tables[sec["key"]] = {"headers": h, "rows": rows}
    return {**{k: r[k] for k in ("id", "kind", "period_start", "period_end", "status", "created_at", "gen_seconds", "ai_run_id")},
            "facts": facts, "template": tpl, "tables": tables, "narrative": nar, "number_check": checks,
            "kpi10": output_svc.report_kpi10(conn, r) if conn is not None else None}   # 周报十项指标：已发布读发布时快照，草稿实时查询


@app.post("/api/reports")
def create_report(body: ReportIn, user=Depends(need("report_gen")), conn=Depends(db)):
    if body.kind not in ("week", "month"):
        raise HTTPException(400, "kind 只能是 week 或 month")
    return report_view(report_svc.generate(conn, user["project_id"], user, body.kind, body.ref_date), user["role"], conn)


@app.get("/api/reports")
def list_reports(user=Depends(viewer), conn=Depends(db)):
    rows = all_(conn, "SELECT * FROM reports WHERE project_id=? ORDER BY id DESC LIMIT 100", (user["project_id"],))
    if user["role"] == "leader":  # 领导只读：只看已发布的报告，草稿还在审核修改中
        rows = [r for r in rows if r["status"] == "published"]
    return [{k: r[k] for k in ("id", "kind", "period_start", "period_end", "status", "created_at", "gen_seconds")} |
            {"warnings": len(loads(r["number_check_json"], []))} for r in rows]


@app.get("/api/reports/{rid}")
def get_report(rid: int, user=Depends(viewer), conn=Depends(db)):
    r = one(conn, "SELECT * FROM reports WHERE id=? AND project_id=?", (rid, user["project_id"]))
    if not r or (user["role"] == "leader" and r["status"] != "published"):
        raise HTTPException(404, "报表不存在")
    return report_view(r, user["role"], conn)


class ReportEdit(BaseModel):
    narrative: dict
    status: str = "draft"
    verified: list[dict] = []  # 发布时逐条核实过的数字守卫项 [{field, number}]


@app.put("/api/reports/{rid}")
def edit_report(rid: int, body: ReportEdit, user=Depends(need("report_gen")), conn=Depends(db)):
    r = one(conn, "SELECT * FROM reports WHERE id=? AND project_id=?", (rid, user["project_id"]))
    if not r:
        raise HTTPException(404, "报表不存在")
    if r["status"] == "published":
        raise HTTPException(400, "已发布的报表不能修改，请重新生成")
    if body.status == "published" and not roles_mod.can(user["role"], "report_publish"):  # 起草权限不含发布（工程主管可编辑，不能直接调接口发布）
        raise HTTPException(403, "无权发布报表")
    from .ai import report_agent
    facts = loads(r["facts_json"])
    tpl = facts.get("template") or report_agent.default_template(r["kind"])
    given = dict(body.narrative or {})
    if given and roles_mod.output_readonly(user["role"]):  # 项目经理对产值只读：产值节保存/发布时原样保留，不被改写或清空
        old = loads(r["narrative_json"], {})
        given.update({k: old.get(k) for k in report_svc.output_keys(tpl)})
    nar = report_agent.normalize({**{s["key"]: given.get(s["key"]) for s in tpl["sections"]}}, tpl) if given else {}
    guard = report_agent.number_guard(nar, report_agent.build_prompt(facts))
    guard += [{"field": x["field"], "number": "", "issue": x["issue"]} for x in report_agent.template_check(nar, tpl)]
    status = "published" if body.status == "published" else "draft"
    if status == "published":  # 数字守卫后端强制：发布者能看到的每个未出自统计的数字都须逐条核实（项目经理看不到的产值节由工程主管核）
        seen = guard
        ok = {(str(v.get("field")), str(v.get("number"))) for v in body.verified if isinstance(v, dict)}
        left = [x for x in seen if x["number"] and (x["field"], x["number"]) not in ok]
        if left:
            raise HTTPException(400, f"还有 {len(left)} 处数字没核实，不能发布（先保存，再逐条勾选“已核实”）：" + "、".join(x["number"] for x in left[:5]))
    conn.execute("UPDATE reports SET narrative_json=?, number_check_json=?, status=? WHERE id=?", (dumps(nar), dumps(guard), status, rid))
    if status == "published":  # 周报发布即冻结十项产值指标快照（期间、单位、口径、来源版本）；已发布报告读快照
        output_svc.snapshot_report(conn, {**r, "status": status})
    audit(conn, user["id"], f"report.{status}", f"report:{rid}")
    return report_view(one(conn, "SELECT * FROM reports WHERE id=?", (rid,)), user["role"], conn)


@app.get("/api/reports/{rid}/docx")
def report_docx(rid: int, user=Depends(viewer), conn=Depends(db)):
    r = one(conn, "SELECT * FROM reports WHERE id=? AND project_id=?", (rid, user["project_id"]))
    if not r:
        raise HTTPException(404, "报表不存在")
    path = report_svc.to_docx(r, hide_split=roles_mod.hides_review_split(user["role"]))  # 项目经理导出的版本不含自报/核定拆分
    name = f"{'周报' if r['kind'] == 'week' else '月报'}_{r['period_start']}_{r['period_end']}.docx"
    return FileResponse(path, filename=name)


# ----------------------------------------------------------------------------- 问答与成效

class AskIn(BaseModel):
    question: str
    agent: str | None = None


@app.post("/api/ask")
def ask(body: AskIn, user=Depends(viewer), conn=Depends(db)):
    t0 = time.monotonic()
    try:
        res = qa_svc.ask(conn, user, body.question, body.agent)
    except PermissionError as e:  # 项目经理指定了不在其范围内的 Agent（产值、日志预审、日志整理）
        raise HTTPException(403, str(e)) from None
    event(conn, "qa.ask", project_id=user["project_id"], user_id=user["id"], value=round(time.monotonic() - t0, 1))
    return res


def metrics_viewer(user=Depends(viewer)) -> dict:
    """应用成效：项目经理不看（记录耗时、日志预审率、手工对照不属于进度、质量、安全）。"""
    if roles_mod.hides_metrics(user["role"]):
        raise HTTPException(403, "应用成效不在项目经理的查看范围")
    return user


@app.get("/api/metrics")
def metrics(user=Depends(metrics_viewer), conn=Depends(db)):
    return metrics_svc.summary(conn, user["project_id"], user["role"])


class BaselineIn(BaseModel):
    kind: str
    minutes: float = Field(gt=0, lt=24 * 60)
    note: str = Field("", max_length=300)
    who: str = Field("", max_length=40)
    sample_date: str | None = None


@app.post("/api/metrics/baseline")
def add_baseline(body: BaselineIn, user=Depends(need("metrics_ops")), conn=Depends(db)):
    if body.kind not in ("report_week", "report_month", "log_collect", "p6_update"):
        raise HTTPException(400, "类型无效")
    event(conn, f"baseline.{body.kind}", project_id=user["project_id"], user_id=user["id"], value=body.minutes,
          data={"note": body.note, "who": body.who, "sample_date": body.sample_date, "recorded_by": user["name"]})
    return {"ok": True}


@app.get("/api/ai/runs")
def ai_runs(user=Depends(need("metrics_ops")), conn=Depends(db)):
    return all_(conn, """SELECT id, kind, provider, model, prompt_version, ok, latency_ms, error, created_at, input_meta_json
                         FROM ai_runs WHERE project_id=? ORDER BY id DESC LIMIT 100""", (user["project_id"],))


# ----------------------------------------------------------------------------- 语音 / 现场交互

@app.post("/api/asr")
def asr(file: UploadFile = File(...), user=Depends(current_user)):
    import tempfile
    raw = file.file.read(25 * 1024 * 1024 + 1)
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(400, "录音不超过25MB")
    suffix = Path(file.filename or "a.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        path = tmp.name
    try:
        res = transcribe(path, file.filename or f"voice{suffix}", project_id=user["project_id"], user_id=user["id"])
    finally:
        Path(path).unlink(missing_ok=True)
    return {"text": res.data, "latency_ms": res.latency_ms}


class AnswerIn(BaseModel):
    index: int
    answer: str = Field(max_length=200)


@app.post("/api/logs/{log_id}/answer")
def answer_q(log_id: int, body: AnswerIn, user=Depends(current_user), conn=Depends(db)):
    row = _log_or_404(conn, log_id, user)
    return log_view(conn, log_svc.answer(conn, row, user, body.index, body.answer), user)


@app.post("/api/logs/{log_id}/carry")
def carry(log_id: int, user=Depends(current_user), conn=Depends(db)):
    row = _log_or_404(conn, log_id, user)
    return log_view(conn, log_svc.carry_over(conn, row, user), user)


class BatchIn(BaseModel):
    ids: list[int]


@app.post("/api/logs/batch-approve")
def batch_approve(body: BatchIn, user=Depends(need("log_review")), conn=Depends(db)):
    """一键通过数据审核Agent判定为低风险的日志；中高风险的不在此列，须逐份查看。"""
    done, skipped = 0, 0
    for i in body.ids[:100]:
        row = log_svc.get_log(conn, i)
        dq = loads(row["dq_json"]) if row else None
        if not row or row["project_id"] != user["project_id"] or row["status"] != "submitted" or not dq or dq.get("risk") != "low":
            skipped += 1
            continue
        try:
            log_svc.review(conn, row, user, "approve", "数据审核Agent预审低风险，批量通过")
            done += 1
        except ValueError:
            skipped += 1
    return {"approved": done, "skipped": skipped}


# ----------------------------------------------------------------------------- Agent 发现（安全/质量/数据）

@app.get("/api/findings")
def list_findings(agent: str = "", status: str = "active", mine: int = 0, candidates: str = "", user=Depends(current_user), conn=Depends(db)):
    """默认只含已确认事项（台账口径）；candidates=only 取待确认的 AI 候选隐患，candidates=all 两者都取。"""
    ids = role_views.scope_ids(conn, user)
    if mine or user["role"] == "tech":
        ids = [user["id"]]
    return find_svc.list_for(conn, user["project_id"], agent=agent or None, status=status or None, assignee_ids=ids,
                             candidates=candidates if candidates in ("only", "all") else "exclude")


@app.get("/api/findings/{fid}")
def get_finding(fid: int, user=Depends(current_user), conn=Depends(db)):
    f = one(conn, "SELECT * FROM findings WHERE id=? AND project_id=?", (fid, user["project_id"]))
    if not f or not role_views.in_scope(conn, user, f["assignee_id"] if roles_mod.is_sub(user["role"]) else user["id"]):
        raise HTTPException(404, "记录不存在")
    users = {u["id"]: u["name"] for u in all_(conn, "SELECT id, name FROM users WHERE project_id=?", (user["project_id"],))}
    out = find_svc.view(f, users)
    if f["ref_type"] == "media":
        m = one(conn, "SELECT * FROM media WHERE id=?", (f["ref_id"],))
        out["photo"] = media_svc.public(m) if m else None
    if f["rectify_media_id"]:
        m = one(conn, "SELECT * FROM media WHERE id=?", (f["rectify_media_id"],))
        out["rectify_photo"] = media_svc.public(m) if m else None
    return out


class FindingStatusIn(BaseModel):
    status: str
    note: str = ""


@app.post("/api/findings/{fid}/status")
def finding_status(fid: int, body: FindingStatusIn, user=Depends(current_user), conn=Depends(db)):
    if user["role"] == "leader":
        raise HTTPException(403, "领导账号只读")
    return find_svc.set_status(conn, fid, user, body.status, body.note)


class ConfirmIn(BaseModel):
    assignee_id: int | None = None
    due_date: str | None = None
    level: str | None = None
    category: str | None = Field(None, max_length=40)
    desc: str | None = Field(None, max_length=200)
    measure: str | None = Field(None, max_length=200)


@app.post("/api/findings/{fid}/confirm")
def finding_confirm(fid: int, body: ConfirmIn, user=Depends(current_user), conn=Depends(db)):
    """AI 候选隐患：确认并派单（带 level/category/desc/measure 任一即“修改后确认”）。本单位安全员、安全主管（及项目经理、管理员）可操作。"""
    if user["role"] == "leader":
        raise HTTPException(403, "领导账号只读")
    try:
        return find_svc.confirm_candidate(conn, fid, user, assignee_id=body.assignee_id, due_date=body.due_date,
                                          edits={k: getattr(body, k) for k in find_svc.EDIT_FIELDS})
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None


class DismissIn(BaseModel):
    reason: str = ""


@app.post("/api/findings/{fid}/dismiss")
def finding_dismiss(fid: int, body: DismissIn, user=Depends(current_user), conn=Depends(db)):
    """AI 候选隐患：不采纳，必须写原因（记为 dismissed 并写入 feedback_note）。"""
    if user["role"] == "leader":
        raise HTTPException(403, "领导账号只读")
    try:
        return find_svc.dismiss_candidate(conn, fid, user, body.reason)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None


class RectifyIn(BaseModel):
    media_id: int


@app.post("/api/findings/{fid}/rectify")
def finding_rectify(fid: int, body: RectifyIn, bg: BackgroundTasks, user=Depends(current_user), conn=Depends(db)):
    """提交整改照片：立即返回（状态“待复核”），AI 复核在响应发出后执行，结论只作建议，闭环 / 退回由安全主管确认。
    拍摄时间早于派单超过 24 小时返回 422 code=photo_before_dispatch（处理见 routers/rectify.py）。"""
    return site_agents.safety_rectify(conn, fid, user, body.media_id, bg)


@app.post("/api/safety/rescan")
def safety_rescan(user=Depends(need("site_safety")), conn=Depends(db)):
    ids = [m["id"] for m in all_(conn, "SELECT id FROM media WHERE project_id=? AND safety_status IN ('pending','failed') AND purpose<>'rectify' LIMIT 40", (user["project_id"],))]
    res = site_agents.safety_scan(conn, ids, user["id"])
    if ids:
        site_agents.after_rescan(conn, ids)
    return res | {"scanned": len(ids)}


@app.post("/api/quality/reminders")
def quality_rem(user=Depends(need("site_quality")), conn=Depends(db)):
    return {"created": site_agents.quality_reminders(conn, user["project_id"], user_id=user["id"])}


# ----------------------------------------------------------------------------- 进度Agent：预测与赶工

@app.get("/api/schedule/forecast")
def get_forecast(user=Depends(viewer), conn=Depends(db)):
    return sched_svc.latest_forecast(conn, user["project_id"]) or {"none": True}


@app.post("/api/schedule/forecast")
def run_forecast(body: DetectIn, user=Depends(need("progress_ops")), conn=Depends(db)):
    res = sched_svc.forecast(conn, user["project_id"], body.as_of)
    if not res:
        raise HTTPException(400, "尚未导入 P6 计划")
    return res


@app.post("/api/schedule/recovery")
def run_recovery(user=Depends(need("progress_ops")), conn=Depends(db)):
    return progress_agent.recovery(conn, user["project_id"], user["id"])


@app.get("/api/schedule/recovery")
def get_recovery(user=Depends(viewer), conn=Depends(db)):
    row = one(conn, "SELECT value_json FROM knowledge WHERE project_id=? AND kind='recovery' AND key='latest'", (user["project_id"],))
    return loads(row["value_json"]) if row else {"none": True}


# ----------------------------------------------------------------------------- 产值Agent

def output_viewer(user=Depends(viewer)) -> dict:
    """产值查询：港湾各岗位、领导、管理员可看；项目经理只读（RJ 10-10），写接口仍按 output_ops 校验。"""
    return user


@app.post("/api/output/boq")
def output_boq(file: UploadFile = File(...), user=Depends(need("output_ops")), conn=Depends(db)):
    return output_svc.import_boq(conn, user["project_id"], user["id"], file.file.read(30 * 1024 * 1024))


@app.post("/api/output/records")
def output_records(file: UploadFile = File(...), user=Depends(need("output_ops")), conn=Depends(db)):
    return output_svc.import_records(conn, user["project_id"], user["id"], file.file.read(30 * 1024 * 1024))


@app.post("/api/output/map/suggest")
def output_suggest(user=Depends(need("output_ops")), conn=Depends(db)):
    return output_svc.suggest_map(conn, user["project_id"], user["id"])


@app.get("/api/output/maps")
def output_maps(user=Depends(output_viewer), conn=Depends(db)):
    return output_svc.mappings(conn, user["project_id"])


class MapStatusIn(BaseModel):
    ids: list[int]
    status: str


@app.post("/api/output/maps")
def output_map_status(body: MapStatusIn, user=Depends(need("output_ops")), conn=Depends(db)):
    return {"updated": output_svc.set_map(conn, user["project_id"], user["id"], body.ids, body.status)}


@app.get("/api/output/overview")
def output_overview(user=Depends(output_viewer), conn=Depends(db)):
    ov = output_svc.overview(conn, user["project_id"])
    if roles_mod.hides_review_split(user["role"]):  # 项目经理：自报/核定拆分属日志审核数据，不给
        ov["series"] = output_svc.strip_review_split(ov["series"])
        if ov.get("analysis"):
            ov["analysis"] = {**ov["analysis"], "series": output_svc.strip_review_split(ov["analysis"].get("series")), "week": None}
    return ov


class OutputPlanIn(BaseModel):
    contract_amount: float | None = None
    boq_total: float | None = None   # 清单总产值：项目整体完成百分比的分母
    year: str | None = None
    year_plan: float | None = None


@app.put("/api/output/plan")
def output_plan(body: OutputPlanIn, user=Depends(need("output_ops")), conn=Depends(db)):
    try:
        return output_svc.save_plan_config(conn, user["project_id"], user["id"], body.contract_amount, body.year, body.year_plan, boq_total=body.boq_total)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/output/analyze")
def output_analyze(user=Depends(need("output_ops")), conn=Depends(db)):
    return output_svc.analyze(conn, user["project_id"], user["id"])


# ----------------------------------------------------------------------------- 报表模板

@app.get("/api/reports/template/{kind}")
def get_tpl(kind: str, user=Depends(viewer), conn=Depends(db)):
    if kind not in ("week", "month"):
        raise HTTPException(404, "模板不存在")
    return report_svc.get_template(conn, user["project_id"], kind)


@app.delete("/api/reports/template/{kind}")
def del_tpl(kind: str, user=Depends(admin), conn=Depends(db)):
    conn.execute("DELETE FROM knowledge WHERE project_id=? AND kind='report_template' AND key=?", (user["project_id"], kind))
    return {"ok": True}


@app.put("/api/reports/template/{kind}")
def put_tpl(kind: str, body: dict, user=Depends(admin), conn=Depends(db)):
    return report_svc.save_template(conn, user["project_id"], kind, body, user["id"])


# ----------------------------------------------------------------------------- 项目首页 / 项目总览 / 简报 / Agent 中心

def _pm_kpis(kpis: dict, role: str) -> dict:
    """项目经理口径：KPI 里不给日志/周确认填报数据（总览用）；产值只读可看（RJ 10-10）。"""
    if not roles_mod.hides_fill_rate(role):
        return kpis
    return {k: v for k, v in kpis.items() if k not in ("logs", "weeks")}


@app.get("/api/cockpit")
def cockpit(user=Depends(viewer), conn=Depends(db)):
    d = brief_svc.cockpit(conn, user["project_id"])
    if roles_mod.hides_fill_rate(user["role"]):  # 项目经理口径：不给日志/周确认填报数据；产值只读可看（RJ 10-10）
        d.pop("log_trend", None)
    if roles_mod.hides_review_split(user["role"]):  # 产值只读，但自报/核定拆分属日志审核数据
        d["output_series"] = output_svc.strip_review_split(d.get("output_series"))
        if isinstance(d["kpis"].get("output"), dict):
            d["kpis"]["output"] = output_svc.strip_review_split([d["kpis"]["output"]])[0]
    d["kpis"] = _pm_kpis(d["kpis"], user["role"])
    return d


@app.post("/api/brief")
def make_brief(user=Depends(viewer), conn=Depends(db)):
    if user["role"] == "leader":  # 公司领导只读：看已生成的简报，不触发生成、不写库
        raise HTTPException(403, "公司领导为只读岗位，不能生成简报")
    return brief_svc.generate(conn, user["project_id"], user["id"])


@app.get("/api/ai/trace")
def ai_trace_view(type: str, id: int, user=Depends(viewer), conn=Depends(db)):
    try:
        return ai_trace.trace(conn, user, type, id)
    except ai_trace.NotFound as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/ask/suggest")
def ask_suggest(user=Depends(viewer), conn=Depends(db)):
    return {"items": ai_trace.suggest_questions(conn, user)}


@app.get("/api/tech/home")
def tech_home(user=Depends(current_user), conn=Depends(db)):
    if user["role"] != "tech":
        raise HTTPException(403, "仅技术员使用")
    today = date.today().isoformat()
    ws, we = log_svc.week_of(today)
    day_log = one(conn, "SELECT id, status, drafted_at FROM logs WHERE project_id=? AND author_id=? AND log_date=? AND mode='ai' AND period='day'",
                  (user["project_id"], user["id"], today))
    wk = log_svc.ensure_log(conn, user, today, "ai", "week")
    prev_ws = (date.fromisoformat(ws) - timedelta(days=7)).isoformat()
    prev = one(conn, "SELECT id, status FROM logs WHERE project_id=? AND author_id=? AND log_date=? AND mode='ai' AND period='week'",
               (user["project_id"], user["id"], prev_ws))
    notes = [{"id": n["id"], "time": n["created_at"][11:16], "text": n["text"], "media": loads(n["media_json"], []), "source": n["source"],
              "geo": media_svc.geo_of(n)}
             for n in log_svc.notes_of(conn, user["project_id"], user["id"], today)]
    audio = voice_svc.audio_ids(conn, [n["id"] for n in notes])  # 保留的录音，随手记旁可回放
    for n in notes:
        n["audio"] = audio.get(n["id"], [])
    tasks = find_svc.list_for(conn, user["project_id"], status="active", assignee_id=user["id"], limit=50)
    return {"today": today, "notes": notes, "day_log": day_log, "week": {"id": wk["id"], "status": wk["status"], "start": ws, "end": we,
            "open_questions": sum(1 for q in loads(wk["qa_json"], []) if not q.get("answer")), "days": log_svc.week_days(conn, wk)},
            "prev_week": prev if prev and prev["status"] in ("draft", "returned") else None,
            "tasks": tasks, "daily_log": log_svc.daily_log_required(conn, user["project_id"]),
            "auto_organize": os.environ.get("ZB_SCHEDULER", "1") != "0"}   # 每晚自动整理是否开启（调度器关闭时页面如实写“未开启”）


class NoteIn(BaseModel):
    text: str = Field("", max_length=2000)
    media_ids: list[int] = []
    seconds: float | None = None
    source: str = "text"
    lat: float | None = None
    lon: float | None = None
    acc: float | None = None
    place: str = Field("", max_length=60)


def _organize_bg(user_id: int, day: str) -> None:
    conn = connect()
    try:
        u = one(conn, "SELECT * FROM users WHERE id=?", (user_id,))
        if log_svc.daily_log_required(conn, u["project_id"]):
            lg = log_svc.organize_day(conn, u, day)
            if lg:
                site_agents.quality_from_log(conn, log_svc.get_log(conn, lg["id"]))
    except Exception:  # noqa: BLE001 — 整理失败不影响随手记保存
        log.exception("每日日志整理失败")
    finally:
        conn.close()


@app.post("/api/notes")
def add_note(body: NoteIn, bg: BackgroundTasks, user=Depends(current_user), conn=Depends(db)):
    if user["role"] != "tech":
        raise HTTPException(403, "仅技术员记录随手记")
    n = log_svc.add_note(conn, user, body.text, body.media_ids, body.seconds, body.source,
                         geo=media_svc.parse_geo(body.lat, body.lon, body.acc, body.place))
    # 每条随手记后后台更新当日日志（同一天多条时只按最新内容整理）
    bg.add_task(_organize_bg, user["id"], n["note_date"])
    return {"id": n["id"], "time": n["created_at"][11:16], "text": n["text"], "media": loads(n["media_json"], []), "source": n["source"],
            "geo": media_svc.geo_of(n)}


@app.delete("/api/notes/{nid}")
def del_note(nid: int, bg: BackgroundTasks, user=Depends(current_user), conn=Depends(db)):
    r = log_svc.delete_note(conn, user, nid)  # 当日自动草稿先清空；还有其他随手记则后台按剩余记录重新整理
    voice_svc.drop_for_note(conn, nid)  # 这条随手记保留的录音一并删掉
    if r["reorganize"]:
        bg.add_task(_organize_bg, user["id"], r["day"])
    return {"ok": True, **({"hint": r["hint"]} if r.get("hint") else {})}  # 草稿有人工修改时不自动清空，提示技术员自己核对


class OrganizeIn(BaseModel):
    day: str | None = None


@app.post("/api/logs/organize")
def organize_now(body: OrganizeIn, user=Depends(current_user), conn=Depends(db)):
    lg = log_svc.organize_day(conn, user, body.day or date.today().isoformat(), force=True)
    if not lg:
        raise HTTPException(400, "今天还没有随手记")
    site_agents.quality_from_log(conn, log_svc.get_log(conn, lg["id"]))
    return log_view(conn, log_svc.get_log(conn, lg["id"]), user)


@app.get("/api/agents")
def agents(user=Depends(viewer), conn=Depends(db)):
    pid = user["project_id"]
    # 调用统计与“应用成效”页同一函数同一口径（ai_trace.call_stats）；完整统计表只在应用成效页，卡片只给“近 7 天 N 次”
    by_agent = ai_trace.call_stats(conn, pid)
    week = ai_trace.call_stats(conn, pid, since=ai_trace.since_days(7))
    proj = one(conn, "SELECT config_json FROM projects WHERE id=?", (pid,))
    simulation = bool((loads(proj["config_json"], {}) or {}).get("simulation")) if proj else False
    fstats = find_svc.stats(conn, pid)
    fb = ai_trace.feedback_summary(conn, pid)
    pm = user["role"] in roles_mod.PM_SCOPE  # 项目经理：只列进度、质量、安全相关的 Agent，不出现日志、产值的渗透率
    out = []
    for ag in AGENTS:
        if pm and ag["key"] not in roles_mod.PM_AGENTS:
            continue
        s = by_agent.get(ag["key"], {"calls": 0, "ok": 0, "last": None})
        f = fstats.get(ag["key"], {})
        decided = (f.get("resolved", 0) + f.get("dismissed", 0))
        if pm:  # 卡片说明里也不出现产值（问工笔对项目经理只查日志、预警、计划）
            ag = {**ag, **{k: re.sub(r"[、，,]?产值(记录|统计|分析)?", "", ag.get(k) or "") for k in ("duty", "inputs", "outputs")}}
        out.append({**ag, "runs": s["calls"], "ok": s["ok"], "runs_7d": week.get(ag["key"], {}).get("calls", 0),
                    "last": s["last"], "findings": f, "dismiss_rate": round(f.get("dismissed", 0) / decided, 3) if decided else None,
                    "feedback": fb.get(ag["key"]), "askable": qa_svc.can_ask(user, ag["key"])})   # 卡片上的“提问”按本岗位能否看到该智能体的数据
    # AI 渗透率：业务记录中由 AI 起草/预审/发现/分析的比例（口径随数据返回）
    q1 = one(conn, "SELECT COUNT(*) n, SUM(ai_draft_json IS NOT NULL) ai, SUM(dq_json IS NOT NULL) dq FROM logs WHERE project_id=? AND mode='ai' AND status IN ('submitted','approved','returned')", (pid,))
    q2 = one(conn, "SELECT COUNT(*) n, SUM(kind='hazard') ai FROM findings WHERE project_id=? AND agent='safety' AND confirmed_at IS NOT NULL", (pid,))
    q3 = one(conn, "SELECT COUNT(*) n, SUM(explanation_json IS NOT NULL) ai FROM alerts WHERE project_id=? AND rule<>'stale' AND level IN ('red','orange')", (pid,))
    q4 = one(conn, "SELECT COUNT(*) n, SUM(ai_run_id IS NOT NULL) ai FROM reports WHERE project_id=?", (pid,))
    q5 = one(conn, "SELECT COUNT(*) n, SUM(ai_run_id IS NOT NULL) ai FROM boq_map WHERE project_id=?", (pid,))
    def ratio(n, ai):  # 仿真项目不计、n=0 暂无样本：都不给比例（界面显示“—”并写明原因），不补零
        n, ai = n or 0, ai or 0
        note = "仿真项目不计" if simulation else ("暂无样本" if not n else None)
        return {"n": n, "ai": ai, "rate": None if note else round(ai / n, 3), "note": note}

    pen = {"日志由AI起草": ratio(q1["n"], q1["ai"]), "日志经AI预审": ratio(q1["n"], q1["dq"]),
           "安全隐患由AI发现": ratio(q2["n"], q2["ai"]), "红橙预警经AI分析": ratio(q3["n"], q3["ai"]),
           "报表由AI起草": ratio(q4["n"], q4["ai"]), "产值映射由AI建议": ratio(q5["n"], q5["ai"])}
    if pm:
        pen = {k: v for k, v in pen.items() if "日志" not in k and "产值" not in k}
    return {"agents": out, "penetration": pen, "simulation": simulation,
            "definition": "AI渗透率=该类业务记录中由Agent起草、预审、发现或分析的数量÷该类记录总数；最终确认均由人完成。"
                          "仿真项目不计渗透率，某类记录为 0 条时显示“—”（暂无样本）"}


class ProjectSwitchIn(BaseModel):
    project_id: int


@app.put("/api/me/project")
def switch_project(body: ProjectSwitchIn, user=Depends(current_user), conn=Depends(db)):
    if user["role"] not in ("admin", "leader"):
        raise HTTPException(403, "仅管理员和领导可切换项目")
    if not one(conn, "SELECT 1 FROM projects WHERE id=?", (body.project_id,)):
        raise HTTPException(404, "项目不存在")
    conn.execute("UPDATE users SET project_id=? WHERE id=?", (body.project_id, user["id"]))
    return {"ok": True}


# ----------------------------------------------------------------------------- 角色首页数据与派单/催办

@app.get("/api/safety/home")
def safety_home(user=Depends(sub_user), conn=Depends(db)):
    return role_views.safety_home(conn, user)


@app.get("/api/safety/overview")
def safety_overview(user=Depends(viewer), conn=Depends(db)):
    return role_views.safety_overview(conn, user["project_id"])


@app.get("/api/eng/overview")
def eng_overview(user=Depends(viewer), conn=Depends(db)):
    out = role_views.eng_overview(conn, user["project_id"])
    if user["role"] in roles_mod.PM_SCOPE:  # 日志预审队列、今日谁没记录、谁没周确认、数据缺口止于工程主管，不进项目经理视图
        out = {**out, "review": None, "missing_today": [], "week_unconfirmed": [], "gaps": [], "gaps_total": 0}
    return out


def _pm_companies(rows: list[dict], role: str) -> list[dict]:
    """项目经理口径：分包单位一览不给今日记录、本周确认（填报情况由工程主管跟进）。"""
    if not roles_mod.hides_fill_rate(role):
        return rows
    return [{k: v for k, v in r.items() if k not in ("record_today", "week_confirmed")} for r in rows]


@app.get("/api/production/overview")
def production_overview(user=Depends(viewer), conn=Depends(db)):
    out = role_views.production_overview(conn, user["project_id"])
    if roles_mod.hides_fill_rate(user["role"]):  # 项目经理口径：KPI 不给日志/周确认填报数
        out = {**out, "kpis": {k: v for k, v in out["kpis"].items() if k not in ("logs", "weeks")},
               "companies": _pm_companies(out["companies"], user["role"])}
    return out


@app.get("/api/companies")
def companies(user=Depends(viewer), conn=Depends(db)):
    return _pm_companies(role_views.company_rollup(conn, user["project_id"]), user["role"])


@app.get("/api/admin/overview")
def admin_overview(user=Depends(admin), conn=Depends(db)):
    return role_views.admin_overview(conn, user)


@app.get("/api/admin/audit")
def admin_audit(limit: int = 100, action: str = "", dev: bool = False, user=Depends(admin), conn=Depends(db)):
    """dev=1 才列出开发态登录（默认排除，免得刷屏）。"""
    return role_views.audit_list(conn, limit, action, dev)


class AssignIn(BaseModel):
    assignee_id: int


class NudgeIn(BaseModel):
    text: str = Field("", max_length=200)


class FeedLikeIn(BaseModel):
    key: str = Field(max_length=40)


class FeedCommentIn(BaseModel):
    key: str = Field(max_length=40)
    text: str = Field(max_length=400)
    reply_to: int | None = None
    quote: str = Field("", max_length=200)


def _guard(fn, *a):
    try:
        return fn(*a)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@app.put("/api/findings/{fid}/assignee")
def finding_assign(fid: int, body: AssignIn, user=Depends(current_user), conn=Depends(db)):
    return _guard(role_views.assign_finding, conn, fid, user, body.assignee_id)


@app.post("/api/findings/{fid}/nudge")
def finding_nudge(fid: int, body: NudgeIn, user=Depends(current_user), conn=Depends(db)):
    return _guard(role_views.nudge_finding, conn, fid, user, body.text)


@app.post("/api/users/{uid}/nudge")
def user_nudge(uid: int, body: NudgeIn, user=Depends(current_user), conn=Depends(db)):
    return _guard(role_views.nudge_user, conn, user, uid, body.text)


@app.get("/api/assignees")
def assignees(user=Depends(current_user), conn=Depends(db)):
    """可派单对象：分包单位账号只能看到本单位人员。"""
    ids = role_views.scope_ids(conn, user)
    rows = all_(conn, "SELECT id, name, role, company FROM users WHERE project_id=? AND active=1 AND role NOT IN ('admin','leader') ORDER BY id", (user["project_id"],))
    return [{"id": r["id"], "name": r["name"], "role_name": roles_mod.role_name(r["role"]),
             "company": role_views.company_label(r["company"]) if roles_mod.is_sub(r["role"]) else "港湾"}
            for r in rows if ids is None or r["id"] in ids]


# ----------------------------------------------------------------------------- 现场圈

def _feed_writer(user: dict) -> None:
    if user["role"] in ("admin", "leader"):
        raise HTTPException(403, "当前角色只能查看现场动态" if user["role"] == "leader" else "系统管理员不参与现场动态")


@app.get("/api/feed")
def get_feed(before: str | None = None, limit: int = 20, mine: int = 0, view: str = "", author: int | None = None, place: str = "",
             lat: float | None = None, lon: float | None = None, user=Depends(current_user), conn=Depends(db)):
    if user["role"] == "admin":
        raise HTTPException(403, "系统管理员不参与现场动态")
    geo = {"place": place.strip()[:30], "lat": lat, "lon": lon} if (place.strip() or lat is not None) else None
    return feed_svc.feed(conn, user, before, limit, bool(mine), view if view in ("follow", "fav", "ai") else "", author, geo)


@app.post("/api/feed/like")
def feed_like(body: FeedLikeIn, user=Depends(current_user), conn=Depends(db)):
    _feed_writer(user)
    return feed_svc.toggle_like(conn, user, body.key)


@app.post("/api/feed/comments")
def feed_comment(body: FeedCommentIn, user=Depends(current_user), conn=Depends(db)):
    _feed_writer(user)
    return feed_svc.add_comment(conn, user, body.key, body.text, body.reply_to, body.quote)


@app.delete("/api/feed/comments/{cid}")
def feed_comment_del(cid: int, user=Depends(current_user), conn=Depends(db)):
    _guard(feed_svc.delete_comment, conn, user, cid)
    return {"ok": True}


# ----------------------------------------------------------------------------- 定时判定 + 静态页

def _evening(project_id: int) -> None:
    """20点后：把当天随手记整理成每日日志；周五起提醒未确认本周的技术员。"""
    conn = connect()
    try:
        today = date.today()
        for u in all_(conn, "SELECT * FROM users WHERE project_id=? AND role='tech' AND active=1", (project_id,)):
            if log_svc.daily_log_required(conn, project_id):
                lg = log_svc.organize_day(conn, u, today.isoformat())
                if lg:
                    site_agents.quality_from_log(conn, log_svc.get_log(conn, lg["id"]))
            if today.weekday() >= 4 and log_svc.weekly_mode(conn, project_id):
                ws, _ = log_svc.week_of(today.isoformat())
                wk = one(conn, "SELECT status FROM logs WHERE author_id=? AND period='week' AND log_date=? AND mode='ai'", (u["id"], ws))
                if not wk or wk["status"] in ("draft", "returned"):
                    insert(conn, "notifications", {"project_id": project_id, "user_id": u["id"], "alert_id": None, "title": "请确认本周进度",
                                                   "body": "现场记录Agent 已按本周随手记整理好周确认单，核对后提交即可（约3分钟）。",
                                                   "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(),
                                                   "link_json": dumps({"route": "week", "params": {}})})
    except Exception:  # noqa: BLE001
        log.exception("晚间整理失败")
    finally:
        conn.close()


def _daily_agents(project_id: int) -> None:
    conn = connect()
    try:
        # 各步骤互不牵连：质控点失败不应让进度预测与每日简报也不跑
        for name, fn in (("质控提醒", lambda: site_agents.quality_reminders(conn, project_id)),
                         ("进度预测", lambda: sched_svc.forecast(conn, project_id)),
                         ("每日简报", lambda: brief_svc.generate(conn, project_id, None))):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("每日Agent任务失败：%s", name)
    finally:
        conn.close()


def _ai_retries() -> None:
    """定时补跑当天生成失败的质控点（每个调度周期一次，有次数与间隔上限）。
    多个进程同时补跑会重复调用 AI：先原子认领“当天第 N 次补跑”，这次没补跑成（无失败、间隔未到、AI 不可用、出错）就退回认领。
    安全识别失败/卡住的照片不自动补跑，由安全员点“重新识别”人工重试。"""
    conn = connect()
    try:
        day = date.today().isoformat()
        retried = lambda pid: one(conn, "SELECT COUNT(*) n FROM events WHERE project_id=? AND kind='qc.retry' AND substr(created_at,1,10)=?",  # noqa: E731
                                  (pid, day))["n"]
        for p in all_(conn, "SELECT id FROM projects"):
            slot = None
            try:
                n = retried(p["id"])
                if not jobs.claim(conn, p["id"], f"qc.retry#{n + 1}", day):
                    continue
                slot = f"qc.retry#{n + 1}"
                r = site_agents.retry_quality(conn, p["id"])
                if r.get("tried"):
                    log.info("质控点补跑：%s", r)
            except Exception:  # noqa: BLE001
                log.exception("质控点补跑失败")
            finally:
                if slot and retried(p["id"]) == n:
                    jobs.release(conn, p["id"], slot, day)
    finally:
        conn.close()


def _due_reminders(project_id: int) -> None:
    """早 8 点后：放行清单（临近开工缺项）与材料/砼（试块到期、复试、方量疑点）的到期提醒；各自幂等。"""
    from .services import gate as gate_svc
    from .services import tickets as ticket_svc
    conn = connect()
    try:
        for name, fn in (("放行清单", gate_svc.notify_due), ("材料与砼", ticket_svc.notify_due)):
            try:
                fn(conn, project_id)
            except Exception:  # noqa: BLE001
                log.exception("%s提醒失败", name)
    finally:
        conn.close()


def _scheduled_jobs(now: datetime) -> None:
    """一轮定时任务：7 点后每日判定、8 点后到期提醒、20 点后晚间整理。每项每个项目每天只认领一次（原子认领，多进程同时醒来也只跑一次）。"""
    if now.hour < 7:
        return
    day = now.date().isoformat()
    conn = connect()
    try:
        for p in all_(conn, "SELECT id FROM projects"):
            act_names.names(conn, p["id"])   # 预先载入作业简称表：后台线程里写给 AI 的事实也用中文简称
            if p6_svc.current_import(conn, p["id"]) and jobs.claim_daily(conn, p["id"], "detect.daily", day):
                _detect_bg(p["id"], None)
                _daily_agents(p["id"])
            if now.hour >= 8 and jobs.claim_daily(conn, p["id"], "due.remind", day):
                _due_reminders(p["id"])
            if now.hour >= 20 and jobs.claim_daily(conn, p["id"], "evening", day):
                _evening(p["id"])
    finally:
        conn.close()


def _scheduler() -> None:
    """每天 7 点后对每个项目自动判定一次（服务常驻即生效）。多个进程连同一个库时，只有持有调度锁的进程运行。"""
    while True:
        held = jobs.hold_scheduler()
        if held:
            try:
                _scheduled_jobs(datetime.now())
            except Exception:  # noqa: BLE001
                log.exception("定时判定失败")
            try:
                _ai_retries()
            except Exception:  # noqa: BLE001
                log.exception("AI 补跑失败")
        time.sleep(600 if held else jobs.RETRY_SECONDS)


from . import routers as _routers  # noqa: E402

_routers.load(app)  # 自动挂接 app/routers/*.py
class _RevalidatedStatic(StaticFiles):
    """静态资源每次按 ETag 校验（未改动返回 304），避免浏览器启发式缓存导致更新后仍显示旧版本。"""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _RevalidatedStatic(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """浏览器会固定请求 /favicon.ico；与页面里声明的图标是同一个 SVG，避免每次页面加载都出一条 404。"""
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def health():
    return {"ok": True, "version": VERSION}
