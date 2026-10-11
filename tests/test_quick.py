from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.main import app
from app.services import quick
from conftest import confirm_hazards, jpeg, login
from test_roles import _add, _team


@pytest.fixture(autouse=True)
def _clean_limits():
    quick.LIMITER.reset()
    yield
    quick.LIMITER.reset()


def anon() -> TestClient:
    c = TestClient(app)
    c.headers.update({"x-zb": "1"})
    return c


def _shot(color=(120, 110, 100)):
    return ("files", ("p.jpg", jpeg(color), "image/jpeg"))


def _qr(c, label="3号墩承台", days=30):
    r = c.post("/api/quick/qrs", json={"label": label, "days": days})
    assert r.status_code == 200, r.text
    return r.json()


_seed = [0]


def _submit(a, qr, n=1, **data):
    _seed[0] += 7  # 每次用不同的图，避免按内容哈希去重
    return a.post(qr["path"] + "/submit", files=[_shot((20 + (_seed[0] * 5 + i * 40) % 200, 90, 160)) for i in range(n)], data=data)


def test_scan_submit_flow_and_scope(env):
    _team(env)
    sa, sb, ss, tech = login("sa_a"), login("sa_b"), login("ss"), login("tech1")
    qr = _qr(sa)
    assert qr["status"] == "active" and qr["path"].startswith("/q/") and len(qr["path"]) > 20

    a = anon()
    page = a.get(qr["path"])
    assert page.status_code == 200 and "3号墩承台" in page.text and 'capture="environment"' in page.text
    assert page.headers["cache-control"] == "no-store" and page.headers["x-frame-options"] == "DENY"
    # 页面里没有项目名、单位名等内部信息
    assert "示例大桥" not in page.text and "甲" not in page.text.replace("甲板", "")

    r = _submit(a, qr, 2, note="脚手架外侧没有挡脚板", team="钢筋班", name="老王", cid="abc123abc123")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "msg", "receipt"} and body["msg"] == "已收到" and body["receipt"].startswith("Q")

    # 本单位安全员看到待认领，识别按原有安全Agent逻辑进入隐患流程，来源标注"扫码"
    subs = sa.get("/api/quick/submissions").json()
    assert len(subs) == 1 and subs[0]["receipt"] == body["receipt"] and len(subs[0]["photos"]) == 2
    assert subs[0]["name"] == "老王" and subs[0]["team"] == "钢筋班" and subs[0]["label"] == "3号墩承台"
    assert subs[0]["scan"] == "hazard" and subs[0]["findings"]
    fid = subs[0]["findings"][0]["id"]
    f = sa.get(f"/api/findings/{fid}").json()
    assert f["body"]["source"] == "扫码" and f["body"]["reporter"] == "钢筋班 · 老王" and f["body"]["report_note"].startswith("脚手架")
    assert sa.get("/api/quick/summary").json() == {"new": 1}
    note = next(n for n in sa.get("/api/notifications").json() if "扫码上报" in n["title"])
    assert note["link"] == {"route": "qrcodes", "params": {}}  # F-06：点消息直达“现场扫码”认领
    # 匿名照片不以安全员的名义出现在现场圈
    assert not [p for p in sa.get("/api/feed").json()["posts"] if p["kind"] == "snap"]

    # 乙单位、技术员看不到也认领不了；港湾安全主管看全项目
    assert sb.get("/api/quick/submissions").json() == [] and sb.get("/api/quick/qrs").json() == []
    assert sb.post(f"/api/quick/submissions/{subs[0]['id']}/claim").status_code == 404
    assert sb.post(f"/api/quick/qrs/{qr['id']}/revoke").status_code == 404
    assert tech.get("/api/quick/submissions").status_code == 403 and tech.post("/api/quick/qrs", json={"label": "x"}).status_code == 403
    assert len(ss.get("/api/quick/submissions").json()) == 1

    # 认领；已处理的不能重复认领
    cl = sa.post(f"/api/quick/submissions/{subs[0]['id']}/claim")
    assert cl.status_code == 200 and cl.json()["status"] == "claimed" and cl.json()["claimed_by"] == "安全员甲"
    assert sa.post(f"/api/quick/submissions/{subs[0]['id']}/claim").status_code == 400
    assert sa.get("/api/quick/summary").json() == {"new": 0}
    assert len(sa.get("/api/quick/submissions?status=claimed").json()) == 1


def test_claim_by_colleague_reassigns_finding(env):
    _team(env)
    _add(env, "sa_a2", "sub_safety", "安全员甲二", "甲")
    sa, sa2, ss = login("sa_a"), login("sa_a2"), login("ss")
    qr = _qr(sa)
    _submit(anon(), qr)
    confirm_hazards()  # 候选隐患认领时不改派（见 test_candidate.py），这里测已确认隐患的认领改派
    sub = sa.get("/api/quick/submissions").json()[0]
    fid = sub["findings"][0]["id"]
    assert sa.get(f"/api/findings/{fid}").json()["assignee_id"] == env["users"]["sa_a"]
    cl = sa2.post(f"/api/quick/submissions/{sub['id']}/claim")
    assert cl.status_code == 200
    assert sa2.get(f"/api/findings/{fid}").json()["assignee_id"] == env["users"]["sa_a2"]
    # 港湾认领只做标记，不改责任人
    _submit(anon(), qr, 1, cid="second-one-xx")
    confirm_hazards()
    sub2 = next(s for s in ss.get("/api/quick/submissions").json() if s["status"] == "new")
    assert ss.post(f"/api/quick/submissions/{sub2['id']}/claim").status_code == 200
    assert sa.get(f"/api/findings/{sub2['findings'][0]['id']}").json()["assignee_id"] == env["users"]["sa_a"]
    # 标无效
    _submit(anon(), qr, 1, cid="third-one-xxx")
    sub3 = next(s for s in sa.get("/api/quick/submissions").json() if s["status"] == "new")
    assert sa.post(f"/api/quick/submissions/{sub3['id']}/invalid").json()["status"] == "invalid"


def test_anonymous_is_write_only(env):
    _team(env)
    qr = _qr(login("sa_a"))
    a = anon()
    # 匿名无法读取任何登录后的接口
    for path in ("/api/me", "/api/quick/qrs", "/api/quick/submissions", "/api/quick/summary", "/api/findings", "/api/feed",
                 "/api/media/1/thumb", "/api/quick/media-status?ids=1", "/api/tech/home"):
        assert a.get(path).status_code == 401, path
    assert a.post("/api/quick/qrs", json={"label": "x"}).status_code == 401
    assert a.post("/api/quick/sync/note", json={"client_id": "abcdefgh12", "text": "x"}).status_code == 401
    # 提交接口：不带 x-zb 头拒绝；无效令牌 404，且不泄露原因
    plain = TestClient(app)
    assert plain.post(qr["path"] + "/submit", files=[_shot()]).status_code == 403
    assert a.get("/q/" + "x" * 22).status_code == 404 and a.post("/q/" + "x" * 22 + "/submit", files=[_shot()]).status_code == 404
    assert a.get("/q/short").status_code == 404
    # 令牌路径不支持读取：提交接口只有 POST
    assert a.get(qr["path"] + "/submit").status_code == 405
    ok = _submit(a, qr)
    assert ok.status_code == 200 and "project" not in ok.text and "company" not in ok.text
    # 列表接口不泄露令牌给越权角色
    assert login("admin").get("/api/quick/qrs").status_code == 403
    assert login("eng").get("/api/quick/qrs").status_code == 403
    assert login("tech2").get("/api/quick/qrs").status_code == 403


def test_expiry_and_revoke(env):
    _team(env)
    sa = login("sa_a")
    qr, qr2 = _qr(sa, days=1), _qr(sa, "路基K1+200")
    a = anon()
    assert a.get(qr["path"]).status_code == 200
    # 过期
    conn = dbmod.connect()
    conn.execute("UPDATE quick_tokens SET expires_at=? WHERE id=?", ((datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds"), qr["id"]))
    conn.close()
    assert a.get(qr["path"]).status_code == 404 and _submit(a, qr).status_code == 404
    assert next(q for q in sa.get("/api/quick/qrs").json() if q["id"] == qr["id"])["status"] == "expired"
    # 作废
    assert a.get(qr2["path"]).status_code == 200
    rv = sa.post(f"/api/quick/qrs/{qr2['id']}/revoke")
    assert rv.status_code == 200 and rv.json()["status"] == "revoked"
    assert a.get(qr2["path"]).status_code == 404 and _submit(a, qr2).status_code == 404
    # 生成人停用后二维码同时失效
    qr3 = _qr(sa, "栈桥")
    conn = dbmod.connect()
    conn.execute("UPDATE users SET active=0 WHERE username='sa_a'")
    conn.close()
    assert a.get(qr3["path"]).status_code == 404


def test_rate_limits(env, monkeypatch):
    _team(env)
    qr = _qr(login("sa_a"))
    a = anon()
    monkeypatch.setattr(quick, "PER_TOKEN_HOUR", 2)
    assert _submit(a, qr, 1, cid="r-one-0000").status_code == 200
    assert _submit(a, qr, 1, cid="r-two-0000").status_code == 200
    r = _submit(a, qr, 1, cid="r-three-000")
    assert r.status_code == 429 and "频繁" in r.json()["detail"]
    # 重试同一条（相同 cid）不算新提交，仍返回原回执
    again = _submit(a, qr, 1, cid="r-two-0000")
    assert again.status_code == 200
    # 同一来源地址的总次数限制（跨二维码），失败尝试也计数
    monkeypatch.setattr(quick, "PER_TOKEN_HOUR", 99)
    quick.LIMITER.reset()
    monkeypatch.setattr(quick, "PER_IP_HOUR", 2)
    assert a.post(qr["path"] + "/submit", files=[("files", ("x.txt", b"hello", "text/plain"))]).status_code == 400
    assert _submit(a, qr, 1, cid="ip-one-0000").status_code == 200
    assert _submit(a, qr, 1, cid="ip-two-0000").status_code == 429
    # 反复猜令牌的来源被封一阵
    quick.LIMITER.reset()
    monkeypatch.setattr(quick, "BAD_TOKEN_10MIN", 3)
    for i in range(3):
        assert a.get("/q/" + "g" * 21 + str(i)).status_code == 404
    assert a.get("/q/" + "g" * 21 + "9").status_code == 429
    assert a.get(qr["path"]).status_code == 429


def test_upload_validation(env, monkeypatch):
    _team(env)
    qr = _qr(login("sa_a"))
    a = anon()
    url = qr["path"] + "/submit"
    assert a.post(url, files=[("files", ("x.txt", b"hello", "text/plain"))]).status_code == 400
    assert a.post(url, files=[("files", ("x.jpg", b"", "image/jpeg"))]).status_code == 400
    assert a.post(url, data={"note": "没有照片"}).status_code == 400
    assert _submit(a, qr, 5).status_code == 400  # 最多 4 张
    # 伪装成 jpg 的 GIF / BMP 不接受
    from PIL import Image
    import io
    for fmt in ("GIF", "BMP"):
        buf = io.BytesIO()
        Image.new("RGB", (30, 30), (1, 2, 3)).save(buf, fmt)
        assert a.post(url, files=[("files", ("x.jpg", buf.getvalue(), "image/jpeg"))]).status_code == 400, fmt
    # 单图与整体大小上限
    monkeypatch.setattr(quick, "MAX_IMAGE_BYTES", 500)
    assert a.post(url, files=[_shot()]).status_code == 413
    monkeypatch.setattr(quick, "MAX_BODY", 800)
    assert _submit(a, qr, 3).status_code == 413
    # 成功提交的次数没有被前面的失败污染
    monkeypatch.undo()
    assert _submit(a, qr, 1, cid="after-fail-0").status_code == 200
    assert len(login("sa_a").get("/api/quick/submissions").json()) == 1


def test_duplicate_cid_is_idempotent(env):
    _team(env)
    qr = _qr(login("sa_a"))
    a = anon()
    r1 = _submit(a, qr, 1, cid="dup-cid-0001").json()
    r2 = _submit(a, qr, 1, cid="dup-cid-0001").json()
    assert r1["receipt"] == r2["receipt"]
    assert len(login("sa_a").get("/api/quick/submissions").json()) == 1


def test_label_required_and_days_clamped(env):
    _team(env)
    sa = login("sa_a")
    assert sa.post("/api/quick/qrs", json={"label": "  ", "days": 30}).status_code == 400
    assert sa.post("/api/quick/qrs", json={"label": "ok", "days": 400}).status_code == 422
    q = sa.post("/api/quick/qrs", json={"label": "桥面<script>", "days": 7}).json()
    page = anon().get(q["path"]).text
    assert "<script>" not in page.split("</style>")[1].split("<script>")[0] and "&lt;script&gt;" in page  # 标签已转义


def test_sync_media_and_note_idempotent(env):
    _team(env)
    t1, t2 = login("tech1"), login("tech2")
    files = {"file": ("a.jpg", jpeg((10, 200, 10)), "image/jpeg")}
    data = {"client_id": "media-cid-0001", "purpose": "log", "taken_at": "2026-10-06T09:30"}
    m1 = t1.post("/api/quick/sync/media", files=files, data=data)
    m2 = t1.post("/api/quick/sync/media", files={"file": ("a.jpg", jpeg((10, 200, 10)), "image/jpeg")}, data=data)
    assert m1.status_code == 200 and m1.json()["id"] == m2.json()["id"]
    conn = dbmod.connect()
    assert conn.execute("SELECT COUNT(*) n FROM media").fetchone()["n"] == 1
    conn.close()
    # 整改照片也可暂存补传（P0-1 AC-2），提交整改另走 /api/quick/sync/rectify（见 test_offline_sync.py）
    r = t1.post("/api/quick/sync/media", files={"file": ("r.jpg", jpeg((10, 20, 200)), "image/jpeg")}, data={**data, "client_id": "media-cid-0002", "purpose": "rectify"})
    assert r.status_code == 200 and r.json()["purpose"] == "rectify" and r.json()["uploaded_at"]

    # 先传图、再传引用该图的笔记；离线时记的时间补传后仍归当天
    when = (datetime.now().astimezone() - timedelta(hours=30)).isoformat()
    body = {"client_id": "note-cid-00001", "text": "补传的随手记", "media_ids": [m1.json()["id"]], "source": "photo", "client_time": when}
    n1 = t1.post("/api/quick/sync/note", json=body)
    n2 = t1.post("/api/quick/sync/note", json=body)
    assert n1.status_code == 200 and n1.json()["id"] == n2.json()["id"] and n1.json()["media"] == [m1.json()["id"]]
    conn = dbmod.connect()
    rows = conn.execute("SELECT note_date, created_at FROM notes").fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0]["note_date"] == when[:10] or rows[0]["note_date"] == (datetime.fromisoformat(when).astimezone()).date().isoformat()
    assert rows[0]["created_at"][:10] == rows[0]["note_date"]

    # 未来时间不可信，按服务器收到的时间；早于 3 天的不再改记到收到当天，仍按记录时间入账（迟到补传，见 test_offline_sync.py）
    fut = (datetime.now().astimezone() + timedelta(days=2)).isoformat()
    n3 = t1.post("/api/quick/sync/note", json={"client_id": "note-cid-00002", "text": "未来时间", "client_time": fut})
    assert n3.status_code == 200 and n3.json()["late_days"] == 0
    old = (datetime.now().astimezone() - timedelta(days=6)).isoformat()
    n6 = t1.post("/api/quick/sync/note", json={"client_id": "note-cid-00006", "text": "六天前记的", "client_time": old})
    assert n6.status_code == 200 and n6.json()["late_days"] == 6
    conn = dbmod.connect()
    assert conn.execute("SELECT note_date FROM notes WHERE id=?", (n3.json()["id"],)).fetchone()["note_date"] == datetime.now().date().isoformat()
    assert conn.execute("SELECT note_date FROM notes WHERE id=?", (n6.json()["id"],)).fetchone()["note_date"] == datetime.fromisoformat(old).date().isoformat()
    conn.close()
    # 引用别人的图、空内容、非技术员
    assert t2.post("/api/quick/sync/note", json={"client_id": "note-cid-00003", "text": "借图", "media_ids": [m1.json()["id"]]}).status_code == 400
    assert t1.post("/api/quick/sync/note", json={"client_id": "note-cid-00004", "text": ""}).status_code == 400
    # 失败不占幂等位：同一 client_id 修正后可再提交
    assert t1.post("/api/quick/sync/note", json={"client_id": "note-cid-00004", "text": "补上内容"}).status_code == 200
    assert login("sa_a").post("/api/quick/sync/note", json={"client_id": "note-cid-00005", "text": "x"}).status_code == 403
    assert t1.post("/api/quick/sync/note", json={"client_id": "x", "text": "x"}).status_code == 422  # client_id 太短


def test_media_status_for_feedback_bubble(env):
    _team(env)
    t1, t2, sa, ss = login("tech1"), login("tech2"), login("sa_a"), login("ss")
    ms = t1.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))]).json()
    ids = ",".join(str(m["id"]) for m in ms)
    st = t1.get(f"/api/quick/media-status?ids={ids}").json()
    assert st["pending"] == 0 and st["items"][0]["status"] == "hazard" and st["findings"] and st["findings"][0]["level_name"]
    # 同单位安全员可查，其他单位技术员、港湾岗位查不到别人的照片
    assert sa.get(f"/api/quick/media-status?ids={ids}").json()["items"]
    assert t2.get(f"/api/quick/media-status?ids={ids}").json()["items"] == []
    assert ss.get(f"/api/quick/media-status?ids={ids}").json()["items"] == []
    assert t1.get("/api/quick/media-status?ids=a,b").status_code == 400
    assert t1.get("/api/quick/media-status").json() == {"items": [], "pending": 0, "failed": 0, "scenes": [], "findings": []}
    # 原有"误报"入口的权限不变：分包不能忽略，安全主管可以（反馈气泡据此隐藏/显示按钮）
    fid = st["findings"][0]["id"]
    assert t1.post(f"/api/findings/{fid}/status", json={"status": "dismissed", "note": "误报"}).status_code == 400
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "dismissed", "note": "拍摄者反馈：误报"}).status_code == 200


def test_qr_encoder_selfcheck():
    """前端二维码生成器：标准向量 + 独立解码回读（需要本机有 node，否则跳过）。"""
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node")
    r = subprocess.run([node, str(Path(__file__).with_name("qr_check.js"))], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "QR OK" in r.stdout, r.stdout + r.stderr


def test_upload_without_scan_is_skipped(env):
    """拍摄者在上传界面选“不识别”：照片入库但不进安全Agent队列，补扫/重新识别也不捡；同一张照片之后选识别则转入队列。"""
    _team(env)
    t1, ss = login("tech1"), login("ss")
    raw = jpeg((200, 50, 50))
    m = t1.post("/api/media", data={"purpose": "safety", "scan": "0"}, files=[("files", ("hz.jpg", raw, "image/jpeg"))]).json()[0]
    assert m["safety_status"] == "skipped"
    st = t1.get(f"/api/quick/media-status?ids={m['id']}").json()
    assert st["pending"] == 0 and st["items"][0]["status"] == "skipped" and st["findings"] == []
    assert ss.post("/api/safety/rescan").json()["scanned"] == 0
    conn = dbmod.connect()
    assert conn.execute("SELECT safety_status FROM media WHERE id=?", (m["id"],)).fetchone()["safety_status"] == "skipped"
    assert conn.execute("SELECT COUNT(*) n FROM findings").fetchone()["n"] == 0
    conn.close()
    # 弱网补传接口同样尊重选择
    s = t1.post("/api/quick/sync/media", data={"client_id": "media-cid-skip1", "purpose": "log", "scan": "0"},
                files={"file": ("b.jpg", jpeg((10, 90, 10)), "image/jpeg")}).json()
    assert s["safety_status"] == "skipped"
    # 默认（不传 scan）仍然识别；同一张照片这次选识别则重新入队
    d = t1.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", raw, "image/jpeg"))]).json()[0]
    assert d["id"] == m["id"] and d["safety_status"] == "pending"  # 响应不等识别，识别在后台完成
    st = t1.get(f"/api/quick/media-status?ids={m['id']}").json()
    assert st["pending"] == 0 and st["items"][0]["status"] in ("hazard", "clear")
