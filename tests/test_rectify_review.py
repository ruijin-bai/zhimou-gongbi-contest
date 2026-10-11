"""T-13 整改复核改为人确认（F-01、F-05、G-06、N-2、N-8、N-9）。

- 提交整改照片同步完成、不调用 AI，响应发出后才由 BackgroundTasks 做 AI 复核；AI 结论只写进 verify_json 作建议，状态停在“待复核”；
- 安全主管收到带隐患跳转的消息；闭环 / 退回只能由人操作，整改提交人不能闭环自己的整改；
- 除人工点“重新让 AI 复核”外，每次整改只调用一次 AI（按 verify_json.state 原子认领）；
- 整改照片拍摄时间服务端校验，给手机时钟留容差（N-8）；
- running 超过 10 分钟视为未完成，安全主管 / 项目经理可“重新让 AI 复核”（N-9）；
- 来历里的复核一步写“AI 建议：……”，闭环前不出现肯定说法（N-2）。
"""
import threading
from datetime import datetime, timedelta

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from conftest import PW, jpeg, login
from test_roles import _team

from app import db as dbmod
from app.ai.client import AIError
from app.main import app
from app.services import site_agents as SA

_color = [0]


def _photo(c, taken_at: str | None = None, exif: str | None = None) -> dict:
    """上传一张整改照片（每次颜色不同，避免按 sha256 去重成同一张）。"""
    _color[0] += 1
    data = {"purpose": "rectify"}
    if taken_at:
        data["taken_at"] = taken_at
    r = c.post("/api/media", data=data, files=[("files", ("r.jpg", jpeg((20 + _color[0] % 200, 210, 20 + _color[0] // 200), exif_date=exif), "image/jpeg"))])
    assert r.status_code == 200, r.text
    return r.json()[0]


def _dispatched(env, assignee="tech1"):
    """甲单位技术员随手拍 → 候选 → 安全主管确认并派单。返回 (技术员, 安全主管, 隐患 id)。"""
    _team(env)
    tech, ss = login("tech1"), login("ss")
    tech.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))])
    fid = tech.get("/api/findings?candidates=only").json()[0]["id"]
    assert ss.post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"][assignee]}).status_code == 200
    return tech, ss, fid


def _row(sql, args=()):
    conn = dbmod.connect()
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def _finding(fid):
    return _row("SELECT * FROM findings WHERE id=?", (fid,))


def _verify(fid):
    return dbmod.loads(_finding(fid)["verify_json"], {}) or {}


def _set_verify(fid, **kw):
    conn = dbmod.connect()
    try:
        v = dbmod.loads(conn.execute("SELECT verify_json FROM findings WHERE id=?", (fid,)).fetchone()["verify_json"], {}) or {}
        conn.execute("UPDATE findings SET verify_json=? WHERE id=?", (dbmod.dumps({**v, **kw}), fid))
    finally:
        conn.close()


def _ai(monkeypatch, verdict=None, error=None, before=None):
    """替换整改复核的 AI：verdict 改写模拟结论、error 模拟调用失败、before 在调用前执行（阻塞用）。返回调用记录。"""
    from app.ai import client
    orig, calls = client.call_json, []

    def fake(kind, **kw):
        if kind == "safety.verify":
            calls.append(kw.get("input_meta"))
            if before:
                before()
            if error:
                raise AIError(error, "timeout")
            if verdict is not None:
                kw["mock"] = lambda: verdict
        return orig(kind, **kw)
    monkeypatch.setattr("app.services.site_agents.call_json", fake)
    return calls


def _ago(**kw) -> str:
    return (datetime.now() - timedelta(**kw)).isoformat(timespec="minutes")


def _aware_ago(**kw) -> str:
    return (datetime.now().astimezone() - timedelta(**kw)).isoformat(timespec="seconds")


def _verify_runs() -> int:
    return _row("SELECT COUNT(*) n FROM ai_runs WHERE kind='safety.verify'")["n"]


# ----------------------------------------------------------------------------- AC-F1 状态停在待复核，AI 只给建议

def test_ai_resolved_is_only_a_suggestion(env):
    tech, ss, fid = _dispatched(env)
    m = _photo(tech)
    r = tech.post(f"/api/findings/{fid}/rectify", json={"media_id": m["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rectifying"
    f = _finding(fid)
    assert f["status"] == "rectifying" and f["resolved_at"] is None and f["rectify_media_id"] == m["id"]
    v = _verify(fid)
    assert v["state"] == "done" and v["suggest"] == "resolved" and v["started_at"] and v["photo_check"] == "unknown"
    # 旧键保留，含义改为 AI 建议
    assert v["resolved"] is True and v["by"] == "safety_agent" and v["at"] and "reason" in v and "remaining" in v
    d = ss.get(f"/api/findings/{fid}").json()
    assert d["status"] == "rectifying" and d["status_name"] == "待复核" and d["verify"]["suggest"] == "resolved"
    assert _verify_runs() == 1


def test_ai_not_resolved_does_not_reject(env, monkeypatch):
    tech, ss, fid = _dispatched(env)
    _ai(monkeypatch, verdict={"resolved": False, "same_scene": True, "reason": "临边仍无防护", "remaining": "临边防护缺失"})
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech)["id"]}).status_code == 200
    f, v = _finding(fid), _verify(fid)
    assert f["status"] == "rectifying"  # 不再由 AI 置为“复核未通过”
    assert v["state"] == "done" and v["suggest"] == "not_resolved" and v["resolved"] is False and v["remaining"] == "临边防护缺失"


def test_ai_failure_waits_for_human(env, monkeypatch):
    tech, ss, fid = _dispatched(env)
    _ai(monkeypatch, error="模型超时")
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech)["id"]}).status_code == 200
    v = _verify(fid)
    assert _finding(fid)["status"] == "rectifying" and v["state"] == "failed" and v["suggest"] is None and v["resolved"] is None
    # 失败也能直接人工闭环
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 200


# ----------------------------------------------------------------------------- AC-F1 通知安全主管

def test_safety_sup_gets_message_with_finding_link(env):
    tech, ss, fid = _dispatched(env)
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech)["id"]}).status_code == 200
    msgs = [n for n in ss.get("/api/notifications").json() if "整改待复核" in n["title"]]
    assert len(msgs) == 1 and msgs[0]["link"] == {"route": "finding", "params": {"id": str(fid)}}
    assert "张工" in msgs[0]["body"]
    # 提交人自己、项目经理、工程主管都不收这条
    for who in ("tech1", "pm", "eng"):
        assert not [n for n in login(who).get("/api/notifications").json() if "整改待复核" in n["title"]]


# ----------------------------------------------------------------------------- AC-F1 闭环 / 退回只能由人，提交人不能闭环自己

def test_human_closes_or_returns_and_rectifier_cannot_close_own(env):
    tech, ss, fid = _dispatched(env)
    pm, sa_a = login("pm"), login("sa_a")
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech)["id"]}).status_code == 200
    assert _verify(fid)["suggest"] == "resolved" and _finding(fid)["status"] == "rectifying"
    # 分包（技术员、分包安全员）不能闭环安全隐患
    assert tech.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 400
    assert sa_a.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 400
    # 安全主管退回重改 → 再提交是一次新的整改，AI 再复核一次
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "open", "note": "防护栏高度不够"}).status_code == 200
    assert _finding(fid)["status"] == "open"
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech)["id"]}).status_code == 200
    assert _finding(fid)["status"] == "rectifying" and _verify_runs() == 2
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 200
    f = _finding(fid)
    assert f["status"] == "resolved" and f["resolved_at"] and f["feedback_by"] == env["users"]["ss"]
    # 安全主管自己拍的整改照片：自己不能闭环，由项目经理闭环
    fid2 = _second_finding(env, tech, ss)
    assert ss.post(f"/api/findings/{fid2}/rectify", json={"media_id": _photo(ss)["id"]}).status_code == 200
    r = ss.post(f"/api/findings/{fid2}/status", json={"status": "resolved"})
    assert r.status_code == 400 and "整改提交人" in r.json()["detail"]
    assert pm.post(f"/api/findings/{fid2}/status", json={"status": "resolved"}).status_code == 200


def _second_finding(env, tech, ss):
    tech.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz2.jpg", jpeg((180, 60, 60)), "image/jpeg"))])
    fid = tech.get("/api/findings?candidates=only").json()[0]["id"]
    assert ss.post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"]["tech1"]}).status_code == 200
    return fid


# ----------------------------------------------------------------------------- AC-F1 提交在 AI 返回之前响应（G-06、N-9）

def test_submit_responds_before_ai_returns(env, monkeypatch):
    tech, ss, fid = _dispatched(env)
    sent, seen = threading.Event(), []
    # AI 复核阻塞到“响应已发出”为止：若提交仍同步等 AI，这里会等满超时并记为 False
    _ai(monkeypatch, before=lambda: seen.append(sent.wait(20)))

    async def watched(scope, receive, send):
        async def snd(msg):
            await send(msg)
            if msg["type"] == "http.response.body" and not msg.get("more_body"):
                sent.set()
        await app(scope, receive, snd)

    c = TestClient(watched)
    c.headers.update({"x-zb": "1"})
    assert c.post("/api/login", json={"username": "tech1", "password": PW}).status_code == 200
    m = _photo(c)
    sent.clear()
    r = c.post(f"/api/findings/{fid}/rectify", json={"media_id": m["id"]})
    assert r.status_code == 200 and r.json()["status"] == "rectifying"
    assert r.json()["verify"]["state"] == "pending"  # 响应里还没有 AI 结论
    assert seen == [True]
    assert _verify(fid)["state"] == "done"  # TestClient 会等 BackgroundTasks 执行完


def test_service_with_bg_returns_before_ai_and_script_runs_sync(env, monkeypatch):
    tech, ss, fid = _dispatched(env)
    calls = _ai(monkeypatch)
    m = _photo(tech)
    conn = dbmod.connect()
    try:
        user = conn.execute("SELECT * FROM users WHERE username='tech1'").fetchone()
        bg = BackgroundTasks()
        out = SA.safety_rectify(conn, fid, user, m["id"], bg)
        assert out["status"] == "rectifying" and out["verify"]["state"] == "pending" and calls == []
        assert len(bg.tasks) == 1
        for t in bg.tasks:
            t.func(*t.args, **t.kwargs)
        assert len(calls) == 1 and _verify(fid)["state"] == "done"
        # 没有 bg 的调用方（脚本）：同步复核，返回时已有建议，状态仍是待复核
        conn.execute("UPDATE findings SET status='open' WHERE id=?", (fid,))
        out = SA.safety_rectify(conn, fid, user, _photo(tech)["id"])
        assert out["status"] == "rectifying" and out["verify"]["state"] == "done" and out["verify"]["suggest"] == "resolved"
        assert len(calls) == 2
    finally:
        conn.close()


# ----------------------------------------------------------------------------- AC-F1 每次整改只调用一次 AI（原子认领）

def test_verify_claimed_once_under_concurrency(env, monkeypatch):
    tech, ss, fid = _dispatched(env)
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(20)
    calls = _ai(monkeypatch, before=block)
    conn = dbmod.connect()
    try:
        user = conn.execute("SELECT * FROM users WHERE username='tech1'").fetchone()
        SA.rectify_submit(conn, fid, user, _photo(tech)["id"])
    finally:
        conn.close()
    assert _verify(fid)["state"] == "pending" and calls == []
    results = {}

    def run(name):
        c = dbmod.connect()
        try:
            results[name] = SA.rectify_verify(c, fid)
        finally:
            c.close()
    t1 = threading.Thread(target=run, args=("a",))
    t1.start()
    assert entered.wait(20)
    assert _verify(fid)["state"] == "running"
    t2 = threading.Thread(target=run, args=("b",))
    t2.start()
    t2.join(20)
    assert results["b"] is None  # 已被认领：不再调用 AI
    release.set()
    t1.join(20)
    assert results["a"] is not None and len(calls) == 1 and _verify(fid)["state"] == "done"
    # 已完成的再跑一次也不调用
    conn = dbmod.connect()
    try:
        assert SA.rectify_verify(conn, fid) is None
    finally:
        conn.close()
    assert len(calls) == 1


# ----------------------------------------------------------------------------- AC-F5 拍摄时间（N-8 容差）

def test_photo_time_rules(env):
    tech, ss, fid = _dispatched(env)
    # 早于派单不超过 24 小时：接收，标 early
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech, taken_at=_ago(hours=2))["id"]}).status_code == 200
    assert _verify(fid)["photo_check"] == "early"
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "open", "note": "重拍"}).status_code == 200
    # 早于派单超过 24 小时：422，照片仍在服务器上，但不挂到这条整改
    old = _photo(tech, taken_at=_ago(days=3))
    r = tech.post(f"/api/findings/{fid}/rectify", json={"media_id": old["id"]})
    assert r.status_code == 422 and r.json()["code"] == "photo_before_dispatch" and r.json()["detail"]
    f = _finding(fid)
    assert f["status"] == "open" and f["rectify_media_id"] != old["id"]
    assert _row("SELECT id FROM media WHERE id=?", (old["id"],)) and tech.get(f"/api/media/{old['id']}/thumb").status_code == 200
    assert _verify_runs() == 1  # 被拒的这次没有调用 AI
    # 晚于服务器时间超过 10 分钟（手机时钟异常）：接收，拍摄时间改记为收到时间
    fut = _photo(tech, exif="2031:01:01 08:00:00")
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": fut["id"]}).status_code == 200
    v, m = _verify(fid), _row("SELECT * FROM media WHERE id=?", (fut["id"],))
    assert v["photo_check"] == "clock_skew" and v["photo_taken_at"] == "2031-01-01T08:00"
    assert m["taken_at"] == m["uploaded_at"][:16]


def test_photo_time_unknown_and_late_upload(env):
    tech, ss, fid = _dispatched(env)
    # 派单在 3 天前；照片 2 天前拍、今天才补传：不早于派单，照常接收
    conn = dbmod.connect()
    conn.execute("UPDATE findings SET confirmed_at=? WHERE id=?", (_aware_ago(days=3), fid))
    conn.close()
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech, taken_at=_ago(days=2))["id"]}).status_code == 200
    assert _verify(fid)["photo_check"] == "ok"
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "open", "note": "重拍"}).status_code == 200
    # 没有拍摄时间：接收，标 unknown
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech)["id"]}).status_code == 200
    assert _verify(fid)["photo_check"] == "unknown"


def test_photo_check_boundaries():
    now = datetime(2026, 10, 7, 12, 0).astimezone()
    disp = now - timedelta(days=1)

    def chk(taken, confirmed=disp, created=None):
        f = {"confirmed_at": confirmed.isoformat() if confirmed else None, "created_at": (created or disp).isoformat()}
        m = {"taken_at": taken.replace(tzinfo=None).isoformat(timespec="minutes") if taken else None, "uploaded_at": now.isoformat(timespec="seconds")}
        return SA.photo_check(f, m, now)[0]
    assert chk(None) == "unknown"
    assert chk(disp + timedelta(hours=1)) == "ok"
    assert chk(disp - timedelta(hours=24)) == "early"            # 恰好 24 小时：放行
    assert chk(now + timedelta(minutes=10)) == "ok"              # 恰好 10 分钟：不算异常
    assert chk(now + timedelta(minutes=11)) == "clock_skew"
    try:
        chk(disp - timedelta(hours=24, minutes=1))
        raise AssertionError("应拒收")
    except SA.RectifyPhotoError as exc:
        assert exc.code == "photo_before_dispatch"
    # 没有 confirmed_at 时按 created_at 算派单时间
    assert chk(disp - timedelta(hours=2), confirmed=None, created=disp) == "early"
    try:
        chk(disp - timedelta(days=2), confirmed=None, created=disp)
        raise AssertionError("应拒收")
    except SA.RectifyPhotoError:
        pass


# ----------------------------------------------------------------------------- N-9 重新让 AI 复核

def test_reverify_roles_states_and_audit(env, monkeypatch):
    tech, ss, fid = _dispatched(env)
    pm, eng, sa_a = login("pm"), login("eng"), login("sa_a")
    calls = _ai(monkeypatch, error="模型超时")
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech)["id"]}).status_code == 200
    assert _verify(fid)["state"] == "failed" and len(calls) == 1
    for c in (tech, eng, sa_a):
        assert c.post(f"/api/findings/{fid}/reverify").status_code == 403
    calls = _ai(monkeypatch)
    r = ss.post(f"/api/findings/{fid}/reverify")
    assert r.status_code == 200, r.text
    assert len(calls) == 1 and _verify(fid)["state"] == "done" and _finding(fid)["status"] == "rectifying"
    assert _row("SELECT COUNT(*) n FROM audit WHERE action='finding.reverify' AND target=? AND user_id=?", (f"finding:{fid}", env["users"]["ss"]))["n"] == 1
    # 已完成：409
    assert ss.post(f"/api/findings/{fid}/reverify").status_code == 409
    # 进行中但未超过 10 分钟：409；超过 10 分钟：项目经理可重新复核
    _set_verify(fid, state="running", suggest=None, started_at=_aware_ago(minutes=2))
    assert pm.post(f"/api/findings/{fid}/reverify").status_code == 409
    _set_verify(fid, state="running", started_at=_aware_ago(minutes=11))
    assert pm.post(f"/api/findings/{fid}/reverify").status_code == 200
    assert len(calls) == 2 and _verify(fid)["state"] == "done"
    # 已闭环：409
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 200
    _set_verify(fid, state="failed")
    assert ss.post(f"/api/findings/{fid}/reverify").status_code == 409
    assert len(calls) == 2


def test_stuck_running_human_can_close_and_stale_result_is_dropped(env, monkeypatch):
    tech, ss, fid = _dispatched(env)
    pm = login("pm")
    entered, release = threading.Event(), threading.Event()
    state = {"n": 0}

    def block():
        state["n"] += 1
        if state["n"] == 1:  # 第一次（被“卡住”的复核）阻塞，重新复核那次直接返回
            entered.set()
            assert release.wait(20)
    calls = _ai(monkeypatch, before=block)
    conn = dbmod.connect()
    try:
        SA.rectify_submit(conn, fid, conn.execute("SELECT * FROM users WHERE username='tech1'").fetchone(), _photo(tech)["id"])
    finally:
        conn.close()
    out = {}

    def run():
        c = dbmod.connect()
        try:
            out["old"] = SA.rectify_verify(c, fid)
        finally:
            c.close()
    t = threading.Thread(target=run)
    t.start()
    assert entered.wait(20)
    _set_verify(fid, started_at=_aware_ago(minutes=11))  # 卡在 running 超过 10 分钟
    # 改为让重新复核给出“仍存在”，以区分新旧结果
    from app.ai import client
    orig = client.call_json

    def second(kind, **kw):
        if kind == "safety.verify":
            calls.append("second")
            kw["mock"] = lambda: {"resolved": False, "same_scene": True, "reason": "仍缺防护", "remaining": "临边"}
        return orig(kind, **kw)
    monkeypatch.setattr("app.services.site_agents.call_json", second)
    assert pm.post(f"/api/findings/{fid}/reverify").status_code == 200
    assert _verify(fid)["suggest"] == "not_resolved"
    release.set()
    t.join(20)
    assert out["old"] is None  # 旧的复核结果作废，不覆盖
    assert _verify(fid)["suggest"] == "not_resolved" and _verify(fid)["state"] == "done"
    # 人工随时可以直接闭环
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 200


# ----------------------------------------------------------------------------- N-2 统一文案、来历

def test_verify_label_contract():
    now = datetime.now().astimezone()
    L = SA.verify_label
    assert L({"state": "done", "suggest": "resolved"}) == "AI 建议：已消除，待安全主管确认"
    assert L({"state": "done", "suggest": "not_resolved", "remaining": "临边防护缺失"}) == "AI 建议：仍存在临边防护缺失"
    assert L({"state": "pending"}) == "AI 复核中"
    assert L({"state": "running", "started_at": (now - timedelta(minutes=3)).isoformat()}) == "AI 复核中"
    assert L({"state": "running", "started_at": (now - timedelta(minutes=11)).isoformat()}) == "AI 复核未完成，可人工复核"
    assert L({"state": "failed"}) == "AI 复核失败，待人工复核"
    assert L({"state": "done", "suggest": "resolved"}, "resolved") == "AI 建议：已消除"
    # T-13 之前的旧记录（没有 state）
    assert L({"resolved": True, "reason": "x"}) == "AI 建议：已消除，待安全主管确认"
    assert L({"resolved": None, "reason": "AI复核失败"}) == "AI 复核失败，待人工复核"
    assert L(None) == ""


def test_trace_step_is_ai_suggestion(env):
    tech, ss, fid = _dispatched(env)
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech)["id"]}).status_code == 200
    tr = ss.get(f"/api/ai/trace?type=finding&id={fid}").json()
    step = next(s for s in tr["steps"] if s["what"] == "整改照片复核")
    assert step["note"].startswith("AI 建议：已消除，待安全主管确认")
    assert "已确认消除" not in str(tr) and any(s["what"] == "提交整改" for s in tr["steps"])
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 200
    tr = ss.get(f"/api/ai/trace?type=finding&id={fid}").json()
    step = next(s for s in tr["steps"] if s["what"] == "整改照片复核")
    assert step["note"].startswith("AI 建议：已消除") and "待安全主管确认" not in step["note"]
    assert any(s["what"] == "确认闭环" for s in tr["steps"])
