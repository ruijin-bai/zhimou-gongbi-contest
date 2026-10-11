"""一线减压三件套的接口。

1. 扫码免账号随手拍：公开页 `/q/{token}` 与匿名提交 `/q/{token}/submit`（只写不读、按令牌与来源限流）；
   二维码管理与待认领 `/api/quick/qrs`、`/api/quick/submissions`（需登录，安全员/安全主管/项目经理）。
2. 弱网/离线补传（P0-1）：`/api/quick/sync/note`、`/sync/media`、`/sync/voice`、`/sync/rectify` 是带 client_id 幂等的包装，
   原有 /api/notes、/api/media、/api/findings/{id}/rectify 不变。补传一律按拍摄或录音时间入账，不拒收。
   语音收下就返回，后台转写成随手记：`/api/quick/voice-pending` 本人未完成的转写，`/api/quick/voice/{id}/retry|discard`，
   `/api/quick/voice/{id}/audio` 回放保留的录音（本人、工程主管）；
   `/api/quick/late-notes` 迟到补传与“确认后才到”的随手记（D5，下一次周确认里由技术员决定是否补入）。
3. 拍完立刻有反馈：`/api/quick/media-status` 只读，给拍照后的反馈气泡轮询识别进展。
"""
from __future__ import annotations

from fastapi import BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS quick_tokens (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    token TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES users(id),
    org TEXT NOT NULL DEFAULT 'sub',
    company TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by INTEGER
);
CREATE TABLE IF NOT EXISTS quick_submissions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    token_id INTEGER NOT NULL REFERENCES quick_tokens(id),
    receipt TEXT NOT NULL UNIQUE,
    client_id TEXT,
    reporter_name TEXT NOT NULL DEFAULT '',
    reporter_team TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    media_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','claimed','invalid')),
    claimed_by INTEGER,
    claimed_at TEXT,
    ip_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quick_sub_token ON quick_submissions(token_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_quick_sub_cid ON quick_submissions(token_id, client_id) WHERE client_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS quick_idem (
    user_id INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, client_id)
);
CREATE TABLE IF NOT EXISTS quick_settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
-- 离线语音：录音到了先放这里，后台转写成功才写成随手记（notes 不加列，读随手记的各处不受影响）；音频一律保留供回放，
-- note_cid 指向拍照时同时录音的那条照片随手记（client_id），转写后文字补进那条
CREATE TABLE IF NOT EXISTS voice_pending (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    audio_file TEXT NOT NULL DEFAULT '',
    mime TEXT NOT NULL DEFAULT '',
    seconds REAL,
    recorded_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    geo_lat REAL, geo_lon REAL, geo_acc REAL, geo_place TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')),
    tries INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    note_id INTEGER,
    note_cid TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, client_id)
);
CREATE INDEX IF NOT EXISTS ix_voice_pending_user ON voice_pending(user_id, status);
CREATE INDEX IF NOT EXISTS ix_voice_pending_note ON voice_pending(note_id);
"""


def migrate(conn) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(voice_pending)").fetchall()}
    if "note_cid" not in cols:
        conn.execute("ALTER TABLE voice_pending ADD COLUMN note_cid TEXT NOT NULL DEFAULT ''")


class QrIn(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    days: int = Field(30, ge=1, le=90)


class BaseIn(BaseModel):
    base: str = Field("", max_length=200)


class SyncNoteIn(BaseModel):
    client_id: str = Field(min_length=8, max_length=64)
    text: str = Field("", max_length=2000)
    media_ids: list[int] = []
    seconds: float | None = None
    source: str = "text"
    lat: float | None = None
    lon: float | None = None
    acc: float | None = None
    place: str = Field("", max_length=60)
    client_time: str | None = Field(None, max_length=40)


class SyncRectifyIn(BaseModel):
    client_id: str = Field(min_length=8, max_length=64)
    finding_id: int
    media_id: int


_NO_STORE = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "X-Robots-Tag": "noindex",
             "Content-Security-Policy": "default-src 'none'; img-src 'self' blob: data:; style-src 'unsafe-inline'; "
                                        "script-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; base-uri 'none'"}


def register(app) -> None:
    from .. import main as M
    from ..db import all_, connect, one
    from ..services import logs as log_svc
    from ..services import media as media_svc
    from ..services import quick
    from ..services import site_agents
    from ..services import voice

    def guard(fn, *a):
        try:
            return fn(*a)
        except quick.QuickError as exc:
            raise HTTPException(exc.status, exc.msg) from None

    def qr_user(user=Depends(M.current_user)) -> dict:
        if user["role"] not in quick.QR_ROLES:
            raise HTTPException(403, "仅安全员、安全主管、项目经理可管理扫码上报")
        return user

    def ip_of(request: Request) -> str:
        return request.client.host if request.client else ""

    # ------------------------------------------------------------------ 公开页与匿名提交（不读任何项目数据）

    @app.get("/q/{token}", include_in_schema=False)
    def public_page(token: str, request: Request):
        if quick.bad_token_blocked(ip_of(request)):
            return HTMLResponse(quick.bad_page(), status_code=429, headers=_NO_STORE)
        conn = connect()
        try:
            t = quick.find_token(conn, token)
            if not t:
                quick.note_bad_token(ip_of(request))
                return HTMLResponse(quick.bad_page(), status_code=404, headers=_NO_STORE)
            return HTMLResponse(quick.public_page(t), headers=_NO_STORE)
        finally:
            conn.close()

    @app.post("/q/{token}/submit", include_in_schema=False)
    async def public_submit(token: str, request: Request, bg: BackgroundTasks):
        ip = ip_of(request)
        deny = {"Cache-Control": "no-store"}
        if request.headers.get("x-zb") != "1":
            return JSONResponse({"detail": "缺少请求头"}, status_code=403, headers=deny)
        if quick.bad_token_blocked(ip):
            return JSONResponse({"detail": "尝试次数过多，请稍后再试"}, status_code=429, headers=deny)
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if not length:
            return JSONResponse({"detail": "请求不完整"}, status_code=411, headers=deny)
        if length > quick.MAX_BODY:
            return JSONResponse({"detail": "照片太大，请少拍几张或降低画质"}, status_code=413, headers=deny)

        def token_ok() -> bool:
            conn = connect()
            try:
                return bool(quick.find_token(conn, token))
            finally:
                conn.close()

        if not await run_in_threadpool(token_ok):  # 先验令牌再解析上传内容，无效令牌不让服务器白白收大文件
            quick.note_bad_token(ip)
            return JSONResponse({"detail": "二维码无效、已过期或已作废"}, status_code=404, headers=deny)
        form = await request.form()
        try:
            uploads = [await f.read() for f in form.getlist("files") if hasattr(f, "read")][:quick.MAX_PHOTOS + 1]
            fields = {k: str(form.get(k) or "") for k in ("note", "team", "name", "cid")}
        finally:
            await form.close()

        def work():
            conn = connect()
            try:
                t = quick.find_token(conn, token)
                if not t:
                    quick.note_bad_token(ip)
                    raise quick.QuickError(404, "二维码无效、已过期或已作废")
                res = quick.submit(conn, t, uploads, fields["name"], fields["team"], fields["note"], fields["cid"], ip)
                pending = [m["id"] for m in all_(conn, f"SELECT id FROM media WHERE id IN ({','.join('?' for _ in res['media_ids']) or 'NULL'}) AND safety_status='pending'",
                                                  res["media_ids"])]
                return res, t["created_by"], pending
            finally:
                conn.close()

        try:
            res, creator, pending = await run_in_threadpool(work)
        except quick.QuickError as exc:
            return JSONResponse({"detail": exc.msg}, status_code=exc.status, headers=deny)
        if pending:
            bg.add_task(quick.scan_in_background, pending, creator, res["submission_id"])
        # 只回"已收到"和回执号，不回显任何项目内部数据
        return JSONResponse({"ok": True, "msg": "已收到", "receipt": res["receipt"]}, headers=deny)

    # ------------------------------------------------------------------ 二维码管理与待认领（需登录）

    @app.post("/api/quick/qrs")
    def qr_create(body: QrIn, user=Depends(qr_user), conn=Depends(M.db)):
        return guard(quick.create_token, conn, user, body.label, body.days)

    @app.get("/api/quick/base")
    def qr_base(request: Request, user=Depends(M.current_user), conn=Depends(M.db)):
        """二维码网址：管理员设置的部署地址（base），以及本机局域网地址建议（lan，取不到为空串）。只读。"""
        if user["role"] not in quick.QR_ROLES and user["role"] != "admin":
            raise HTTPException(403, "仅安全员、安全主管、项目经理可管理扫码上报")
        return {"base": quick.get_base(conn), "lan": quick.lan_base(request.url.scheme, request.url.port), "admin": user["role"] == "admin"}

    @app.put("/api/quick/base")
    def qr_base_set(body: BaseIn, user=Depends(M.admin), conn=Depends(M.db)):
        """部署配置：只有系统管理员能改；留空则按本机局域网地址自动生成。"""
        return {"ok": True, "base": guard(quick.set_base, conn, user, body.base)}

    @app.get("/api/quick/qrs")
    def qr_list(user=Depends(qr_user), conn=Depends(M.db)):
        return quick.list_tokens(conn, user)

    @app.post("/api/quick/qrs/{tid}/revoke")
    def qr_revoke(tid: int, user=Depends(qr_user), conn=Depends(M.db)):
        return guard(quick.revoke_token, conn, user, tid)

    @app.get("/api/quick/submissions")
    def sub_list(status: str = "new", user=Depends(qr_user), conn=Depends(M.db)):
        return quick.list_submissions(conn, user, status)

    @app.get("/api/quick/summary")
    def sub_summary(user=Depends(M.current_user), conn=Depends(M.db)):
        return {"new": quick.pending_count(conn, user) if user["role"] in quick.QR_ROLES else 0}

    @app.post("/api/quick/submissions/{sid}/claim")
    def sub_claim(sid: int, user=Depends(qr_user), conn=Depends(M.db)):
        return guard(quick.claim, conn, user, sid)

    @app.post("/api/quick/submissions/{sid}/invalid")
    def sub_invalid(sid: int, user=Depends(qr_user), conn=Depends(M.db)):
        return guard(quick.mark_invalid, conn, user, sid)

    # ------------------------------------------------------------------ 拍完立刻有反馈

    @app.get("/api/quick/media-status")
    def media_status(ids: str = "", user=Depends(M.current_user), conn=Depends(M.db)):
        try:
            id_list = [int(x) for x in ids.split(",") if x.strip()][:20]
        except ValueError:
            raise HTTPException(400, "ids 格式错误") from None
        return quick.media_status(conn, user, id_list)

    # ------------------------------------------------------------------ 弱网/离线补传（client_id 幂等）

    @app.post("/api/quick/sync/media")
    def sync_media(bg: BackgroundTasks, file: UploadFile = File(...), client_id: str = Form(...), purpose: str = Form("log"),
                   log_id: int | None = Form(None), taken_at: str = Form(""), geo_lat: str = Form(""), geo_lon: str = Form(""),
                   geo_acc: str = Form(""), place: str = Form(""), scan: int = Form(1), user=Depends(M.current_user), conn=Depends(M.db)):
        """整改照片（purpose=rectify）也可暂存补传，只入库、不识别、不挂整改；挂整改另走 /api/quick/sync/rectify。"""
        state, hit = guard(quick.idem_begin, conn, user["id"], client_id, "media")
        if state == "done":
            return hit
        if state == "busy":
            raise HTTPException(409, "同一张照片正在处理，请稍后重试")
        try:
            out = M.upload_media(bg, files=[file], log_id=log_id, purpose=purpose, taken_at=[taken_at], geo_lat=[geo_lat],
                                 geo_lon=[geo_lon], geo_acc=[geo_acc], place=[place], scan=scan, user=user, conn=conn)
            m = out[0]
            # “改作随手拍”（N-8）：同一张图按内容去重拿回了没挂上整改的整改照片 → 改成这次的用途并识别
            if purpose in ("log", "safety") and m["purpose"] == "rectify" and quick.reuse_rectify_photo(conn, m["id"], purpose, bool(scan)):
                m = media_svc.public(one(conn, "SELECT * FROM media WHERE id=?", (m["id"],)))
                if m["safety_status"] == "pending":
                    bg.add_task(M._safety_bg, [m["id"]], user["id"])
        except BaseException:
            quick.idem_abort(conn, user["id"], client_id)
            raise
        res = {**m, "client_id": client_id}
        quick.idem_done(conn, user["id"], client_id, res)
        return res

    @app.post("/api/quick/sync/rectify")
    def sync_rectify(body: SyncRectifyIn, bg: BackgroundTasks, user=Depends(M.current_user), conn=Depends(M.db)):
        """整改提交的幂等包装（离线暂存、超时重发）：同一 client_id 只提交一次，重发返回原来的整改单视图。
        复核按 4.4 契约挂在 bg 上，接口在 AI 返回之前就交回（T-13）。拍摄时间早于派单超过 24 小时：422
        {detail, code: photo_before_dispatch, media_id}（与 routers/rectify.py 同形），照片留在服务器上不挂整改，可“改作随手拍”；
        这个结果也记进幂等位，同一 client_id 再交返回同样的 422（N-8）。其他拒绝（400 等）不占幂等位，改正后可再交。"""
        state, hit = guard(quick.idem_begin, conn, user["id"], body.client_id, "rectify")
        if state == "done":
            if hit.get("_status") == 422:
                return JSONResponse({k: hit[k] for k in ("detail", "code", "media_id")}, status_code=422)
            return hit
        if state == "busy":
            raise HTTPException(409, "这条整改正在提交，请稍后重试")
        try:
            out = site_agents.safety_rectify(conn, body.finding_id, user, body.media_id, bg=bg)
        except site_agents.RectifyPhotoError as exc:
            res = {"_status": 422, "detail": str(exc), "code": exc.code, "media_id": exc.media_id}
            quick.idem_done(conn, user["id"], body.client_id, res)
            return JSONResponse({k: res[k] for k in ("detail", "code", "media_id")}, status_code=422)
        except BaseException:
            quick.idem_abort(conn, user["id"], body.client_id)
            raise
        quick.idem_done(conn, user["id"], body.client_id, out)
        return out

    @app.post("/api/quick/sync/voice")
    def sync_voice(bg: BackgroundTasks, file: UploadFile = File(...), client_id: str = Form(..., min_length=8, max_length=64),
                   recorded_at: str = Form(""), seconds: str = Form(""), geo_lat: str = Form(""), geo_lon: str = Form(""),
                   geo_acc: str = Form(""), place: str = Form(""), note_cid: str = Form("", max_length=64),
                   user=Depends(M.current_user), conn=Depends(M.db)):
        """离线语音补传：收下录音就返回 {pending_id, status:"pending", recorded_at, late_days, client_id}，转写在后台，
        成功后写成随手记（时间＝录音时间）；带 note_cid（拍照时同时录的）则补进那条照片随手记。同一条正在处理时 409，已收下的返回原结果。"""
        if user["role"] != "tech":
            raise HTTPException(403, "仅技术员记录随手记")
        voice.cleanup(conn)
        state, hit = guard(quick.idem_begin, conn, user["id"], client_id, "voice")
        if state == "done":
            return hit
        if state == "busy":
            raise HTTPException(409, "同一条录音正在处理，请稍后重试")
        try:
            raw = file.file.read(voice.MAX_AUDIO + 1)
            res = guard(voice.receive, conn, user, raw, client_id, recorded_at, seconds, media_svc.parse_geo(geo_lat, geo_lon, geo_acc, place), note_cid)
        except BaseException:
            quick.idem_abort(conn, user["id"], client_id)
            raise
        quick.idem_done(conn, user["id"], client_id, res)
        bg.add_task(voice.process, res["pending_id"], M._organize_bg)
        return res

    @app.get("/api/quick/voice-pending")
    def voice_pending(bg: BackgroundTasks, user=Depends(M.current_user), conn=Depends(M.db)):
        """本人未完成的转写 [{pending_id, status, recorded_at, seconds, last_error}]。顺带：清理过期录音（D13），
        把服务重启后搁置的转写重新排队。"""
        if user["role"] != "tech":
            return []
        voice.cleanup(conn)
        for pid in voice.requeue_ids(conn, user["id"]):
            bg.add_task(voice.process, pid, M._organize_bg)
        return voice.pending_of(conn, user)

    @app.post("/api/quick/voice/{pid}/retry")
    def voice_retry(pid: int, bg: BackgroundTasks, user=Depends(M.current_user), conn=Depends(M.db)):
        voice.cleanup(conn)
        res = guard(voice.retry, conn, user, pid)
        bg.add_task(voice.process, pid, M._organize_bg)
        return res

    @app.post("/api/quick/voice/{pid}/discard")
    def voice_discard(pid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        voice.cleanup(conn)
        return guard(voice.discard, conn, user, pid)

    @app.get("/api/quick/voice/{pid}/audio")
    def voice_audio(pid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        path, mime = guard(voice.audio_of, conn, user, pid)
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/quick/late-notes")
    def late_notes(days: int = 21, user=Depends(M.current_user), conn=Depends(M.db)):
        """迟到补传、所属周确认后才到的随手记 [{note_id, note_date, received_date, late_days, week_confirmed, text, time}]。"""
        return quick.late_notes(conn, user, days) if user["role"] == "tech" else []

    @app.post("/api/quick/sync/note")
    def sync_note(body: SyncNoteIn, bg: BackgroundTasks, user=Depends(M.current_user), conn=Depends(M.db)):
        if user["role"] != "tech":
            raise HTTPException(403, "仅技术员记录随手记")
        state, hit = guard(quick.idem_begin, conn, user["id"], body.client_id, "note")
        if state == "done":
            return hit
        if state == "busy":
            raise HTTPException(409, "同一条记录正在处理，请稍后重试")
        try:
            # 离线时记下的真实时间，补传后仍归到当时那天，不论晚到几天；所属日/周已确认的不改数量（整理跳过已提交的日志）
            stamp = quick.note_stamp(body.client_time)
            n = log_svc.add_note(conn, user, body.text, body.media_ids, body.seconds, body.source, note_date=stamp[0] if stamp else None,
                                 geo=media_svc.parse_geo(body.lat, body.lon, body.acc, body.place))
            if stamp:
                conn.execute("UPDATE notes SET created_at=? WHERE id=?", (stamp[1], n["id"]))
                n = one(conn, "SELECT * FROM notes WHERE id=?", (n["id"],))
        except BaseException:
            quick.idem_abort(conn, user["id"], body.client_id)
            raise
        bg.add_task(M._organize_bg, user["id"], n["note_date"])
        res = {"id": n["id"], "time": n["created_at"][11:16], "text": n["text"], "media": M.loads(n["media_json"], []), "source": n["source"],
               "geo": media_svc.geo_of(n), "client_id": body.client_id, "late_days": quick.late_days(n["note_date"], M.now_iso())}
        quick.idem_done(conn, user["id"], body.client_id, res)
        return res
