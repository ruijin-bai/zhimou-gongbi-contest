import io
import time

from conftest import TODAY, backdate, confirm_hazards, jpeg, login, sample_xer

VOICE = "3#墩桩基施工今天完成2根桩，累计完成82%。钢筋工12人，汽车吊1台。旋挖钻故障停机半天。明天继续3#墩桩基施工"


def _setup():
    pm = login("pm")
    assert pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    return pm, login("tech1")


def _submit_today(tech, voice=VOICE, photos=1, purpose_safety=False):
    log = tech.post("/api/logs", json={}).json()
    if photos:
        tech.post("/api/media", data={"log_id": str(log["id"]), "purpose": "safety" if purpose_safety else "log"},
                  files=[("files", (f"p{i}.jpg", jpeg((100 + i * 30, 90, 80)), "image/jpeg")) for i in range(photos)])
    d = tech.post(f"/api/logs/{log['id']}/draft", json={"voice_text": voice}).json()
    backdate(log["id"])
    s = tech.post(f"/api/logs/{log['id']}/submit", json={"version": d["version"], "active_seconds": 200})
    assert s.status_code == 200, s.text
    return s.json()


def test_safety_scan_and_rectify_loop(env):
    pm, tech = _setup()
    up = tech.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))])
    assert up.status_code == 200
    assert tech.get("/api/findings?agent=safety&status=active&mine=1").json() == []  # AI 识别的隐患先是候选，确认前不进台账（见 test_candidate.py）
    confirm_hazards()
    hz = tech.get("/api/findings?agent=safety&status=active&mine=1").json()
    assert hz and hz[0]["kind"] == "hazard" and hz[0]["assignee"] == "张工" and hz[0]["due_date"]
    fid = hz[0]["id"]
    # 其他技术员无权整改
    other = login("tech2")
    rect = other.post("/api/media", data={"purpose": "rectify"}, files=[("files", ("r.jpg", jpeg((10, 200, 10)), "image/jpeg"))]).json()[0]
    assert other.post(f"/api/findings/{fid}/rectify", json={"media_id": rect["id"]}).status_code == 400
    mine = tech.post("/api/media", data={"purpose": "rectify"}, files=[("files", ("r2.jpg", jpeg((20, 210, 20)), "image/jpeg"))]).json()[0]
    r = tech.post(f"/api/findings/{fid}/rectify", json={"media_id": mine["id"]})
    assert r.status_code == 200, r.text
    # AI 复核只给建议，状态停在“待复核”，由人确认闭环（见 test_rectify_review.py）
    assert r.json()["status"] == "rectifying"
    v = pm.get(f"/api/findings/{fid}").json()
    assert v["status"] == "rectifying" and v["verify"]["suggest"] == "resolved" and v["verify"]["resolved"] is True
    assert tech.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 400  # 整改人不能闭环
    assert pm.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).json()["status"] == "resolved"
    # 整改照片不再被当作隐患照片识别
    assert all(f["ref_id"] != mine["id"] for f in pm.get("/api/findings?agent=safety&status=").json())
    assert pm.post(f"/api/findings/{fid}/status", json={"status": "dismissed"}).status_code == 400  # 需原因


def test_dq_review_and_batch_approve(env):
    pm, tech = _setup()
    s = _submit_today(tech)
    adm = login("admin")   # 日志预审与审核归工程主管（默认测试项目没有工程主管账号，用管理员）；项目经理不审核日志
    lg = adm.get(f"/api/logs/{s['id']}").json()
    assert lg["dq"] and lg["dq"]["risk"] in ("low", "medium", "high") and "score" in lg["dq"]
    assert pm.get("/api/queue").status_code == 404   # 旧待办队列接口已下线，项目经理待办走 /api/todo
    assert not [i for i in pm.get("/api/todo").json()["items"] if i["source"] in ("log_review", "log_low")]  # 项目经理不审核日志
    assert "reviewable" not in lg
    assert tech.post(f"/api/logs/{s['id']}/photos", json={"media_ids": []}).status_code in (404, 405)  # 旧挂照片接口已下线
    assert "dq" not in pm.get(f"/api/logs/{s['id']}").json()   # 项目经理看日志详情不含数据审核
    assert pm.post("/api/logs/batch-approve", json={"ids": [s["id"]]}).status_code == 403
    r = adm.post("/api/logs/batch-approve", json={"ids": [s["id"]]}).json()
    assert r["approved"] == (1 if lg["dq"]["risk"] == "low" else 0)
    # 复制照片到第二天的日志 → 重复照片
    tech2 = login("tech2")
    log2 = tech2.post("/api/logs", json={}).json()
    tech2.post("/api/media", data={"log_id": str(log2["id"])}, files=[("files", ("dup.jpg", jpeg((100, 90, 80)), "image/jpeg"))])
    d2 = tech2.post(f"/api/logs/{log2['id']}/draft", json={"voice_text": "路基土方开挖完成300方"}).json()
    backdate(log2["id"])
    tech2.post(f"/api/logs/{log2['id']}/submit", json={"version": d2["version"]})
    dq2 = adm.get(f"/api/logs/{log2['id']}").json()["dq"]
    assert any("高度相似" in c["message"] for c in dq2["checks"]) and dq2["risk"] != "low"


def test_quality_from_log_and_reminders(env):
    pm, tech = _setup()
    _submit_today(tech, voice="3#墩桩基施工完成1根，累计85%。发现钢筋保护层不足质量问题，已整改。明天继续")
    r = pm.post("/api/quality/reminders").json()
    assert r["created"] >= 1  # A1020 计划今天开工
    rem = pm.get("/api/findings?agent=quality&status=active").json()
    assert any(f["kind"] == "qc_reminder" and f["body"]["points"] for f in rem)


def test_forecast_whatif_recovery(env):
    pm, tech = _setup()
    _submit_today(tech)
    f = pm.post("/api/schedule/forecast", json={}).json()
    assert f["milestones"] and f["critical_path"] and f["project_finish"]["p80"] >= f["project_finish"]["p50"]
    m = f["milestones"][0]
    assert 0 <= m["on_time_prob"] <= 1
    assert pm.post("/api/schedule/whatif", json={"measures": []}).status_code in (404, 405)   # 假设分析接口已下线（RJ 2026-10-09），CPM 复算只在赶工方案里用
    rec = pm.post("/api/schedule/recovery").json()
    assert rec["verified"]["measures"] and "复算" in rec["note"]
    assert login("tech1").post("/api/schedule/recovery").status_code == 403


def test_output_agent_flow(env):
    from openpyxl import Workbook
    pm, tech = _setup()
    _submit_today(tech)
    wb = Workbook()
    ws = wb.active
    ws.append(["编码", "名称", "单位", "单价", "数量"])
    ws.append(["B01", "3#墩桩基施工", "根", 5000, 20])
    ws.append(["B02", "路基土方开挖", "m3", 6.5, 10000])
    buf = io.BytesIO()
    wb.save(buf)
    assert pm.get("/api/output/overview").status_code == 200   # 项目经理只读产值（RJ 10-10）；导入等操作由工程主管/管理员
    assert pm.post("/api/output/boq", files={"file": ("boq.xlsx", buf.getvalue())}).status_code == 403
    adm = login("admin")
    assert adm.post("/api/output/boq", files={"file": ("boq.xlsx", buf.getvalue())}).json()["items"] == 2
    s = adm.post("/api/output/map/suggest").json()
    assert s["suggested"] >= 1
    maps = adm.get("/api/output/maps").json()
    a = next(m for m in maps if m["activity_code"] == "A1010")
    assert a["boq_code"] == "B01" and a["status"] == "suggested"
    adm.post("/api/output/maps", json={"ids": [a["id"]], "status": "confirmed"})
    ov = adm.get("/api/output/overview").json()
    assert ov["maps_confirmed"] == 1 and ov["series"][-1]["system"] == 10000  # 2根×5000
    an = adm.post("/api/output/analyze").json()
    assert an["summary"]


def test_template_report_and_brief_and_agents(env):
    pm, tech = _setup()
    _submit_today(tech)
    tpl = pm.get("/api/reports/template/week").json()
    tpl["sections"] = [s for s in tpl["sections"] if s["key"] != "quality"]
    tpl["sections"][0]["max_chars"] = 10
    adm = login("admin")
    assert adm.put("/api/reports/template/week", json=tpl).status_code == 200
    rep = pm.post("/api/reports", json={"kind": "week", "ref_date": TODAY.isoformat()}).json()
    assert [s["key"] for s in rep["template"]["sections"]] == [s["key"] for s in tpl["sections"]]
    assert any(x.get("issue") for x in rep["number_check"])  # 概况超过10字
    assert "progress" in rep["tables"]
    doc = pm.get(f"/api/reports/{rep['id']}/docx")
    assert doc.status_code == 200
    assert adm.delete("/api/reports/template/week").status_code == 200
    assert any(s["key"] == "quality" for s in pm.get("/api/reports/template/week").json()["sections"])
    b = pm.post("/api/brief").json()
    assert "headline" in b
    c = pm.get("/api/cockpit").json()
    assert c["brief"] and "kpis" in c
    ag = adm.get("/api/agents").json()   # 全量卡片；项目经理只见 PM_AGENTS（见 test_pm_scope.py）
    keys = {a["key"] for a in ag["agents"]}
    assert {"scribe", "safety", "quality", "dq", "progress", "output", "report", "analyst", "brief"} <= keys
    assert ag["penetration"]["日志由AI起草"]["rate"] == 1.0


def test_leader_read_only(env):
    import sqlite3  # noqa: F401
    from app import db as dbmod
    from app.security import hash_password
    conn = dbmod.connect()
    dbmod.insert(conn, "users", {"username": "boss", "name": "领导", "role": "leader", "password_hash": hash_password("secret123"),
                                 "project_id": env["project_id"], "wbs_scope": "", "email": "", "active": 1, "created_at": dbmod.now_iso()})
    conn.close()
    ld = login("boss")
    assert ld.get("/api/cockpit").status_code == 200
    assert ld.get("/api/schedule/forecast").status_code == 200
    assert ld.post("/api/schedule/recovery").status_code == 403
    assert ld.post("/api/alerts/detect", json={}).status_code == 403
    assert ld.post("/api/output/map/suggest").status_code == 403


def test_answer_questions_and_carry(env):
    pm, tech = _setup()
    _submit_today(tech)
    # 第二天：口述未提人员机械 → 沿用昨日；AI 提问可点选回答
    from app import db as dbmod
    conn = dbmod.connect()
    conn.execute("UPDATE logs SET log_date=date(log_date,'-1 day')")
    conn.execute("UPDATE log_progress SET log_date=date(log_date,'-1 day')")
    conn.close()
    log = tech.post("/api/logs", json={}).json()
    d = tech.post(f"/api/logs/{log['id']}/draft", json={"voice_text": "3#墩桩基施工完成1根，累计90%"}).json()
    assert d["content"]["labor"] and all(x["sources"] == ["Y"] for x in d["content"]["labor"])
    if d["qa"]:
        r = tech.post(f"/api/logs/{log['id']}/answer", json={"index": 0, "answer": d["qa"][0]["options"][0] if d["qa"][0]["options"] else "是"})
        assert r.json()["qa"][0]["answer"]
    c = tech.post(f"/api/logs/{log['id']}/carry")
    assert c.status_code == 200 and c.json()["content"]["machinery"]


def test_asr_mock(env):
    tech = login("tech1")
    r = tech.post("/api/asr", files={"file": ("v.webm", b"\x1aE\xdf\xa3fake", "audio/webm")})
    assert r.status_code == 200 and r.json()["text"]


def test_simulation_isolated(env):
    from app import sim
    res = sim.build(weeks=4)
    assert res["logs"] > 20
    ag = sim.run_agents(res["project_id"], recent_days=3)
    assert ag["milestones"] >= 1
    sim_pm = None
    from fastapi.testclient import TestClient
    from app.main import app
    sim_pm = TestClient(app)
    sim_pm.headers["x-zb"] = "1"
    assert sim_pm.post("/api/login", json={"username": "sim_pm", "password": "sim12345"}).status_code == 200
    me = sim_pm.get("/api/me").json()
    assert me["project"]["config"]["simulation"] is True
    # 真实项目看不到仿真数据
    pm = login("pm")
    assert all(l["author"] != "钱工（仿真）" for l in login("admin").get("/api/logs?status=").json())  # 日志列表不给项目经理，用管理员核对
    assert sim.reset() is True
    t0 = time.time()
    assert time.time() - t0 < 5


def test_leader_is_read_only_for_brief(env):
    """公司领导只读：不能 POST /api/brief 触发生成（不写 briefs）。"""
    from conftest import login
    from test_roles import _add
    _add(env, "boss", "leader", "领导")
    assert login("boss").post("/api/brief").status_code == 403
