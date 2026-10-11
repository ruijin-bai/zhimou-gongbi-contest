from datetime import date, timedelta

from conftest import DATA_DATE, START, TODAY, backdate, jpeg, login, sample_xer
from sample_xer import make_xer

from app.ai import report_agent
from app.ai.client import extract_json

VOICE = ("3#墩桩基施工今天完成2根桩，累计完成82%。钢筋工12人，混凝土工6人，汽车吊1台。"
         "旋挖钻故障停机半天，已联系维修。明天继续3#墩桩基施工")


def _import(pm, **kw):
    r = pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(**kw), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def test_write_requires_custom_header(env):
    c = login("pm")
    del c.headers["x-zb"]
    assert c.post("/api/logout").status_code == 403


def test_login_failure_and_role_guard(env):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    c.headers["x-zb"] = "1"
    assert c.post("/api/login", json={"username": "pm", "password": "bad"}).status_code == 401
    tech = login("tech1")
    assert tech.get("/api/metrics").status_code == 403
    assert tech.post("/api/p6/import", files={"file": ("a.xer", b"x")}).status_code == 403


def test_p6_import(env):
    pm = login("pm")
    res = _import(pm)
    assert res["activities"] == 12 and res["relations"] == 13 and res["data_date"] == DATA_DATE
    acts = pm.get("/api/p6/activities").json()["items"]
    a1010 = next(a for a in acts if a["code"] == "A1010")
    assert a1010["is_critical"] == 1 and a1010["wbs_path"].endswith("3#墩")
    bad = pm.post("/api/p6/import", files={"file": ("plan.txt", b"abc")})
    assert bad.status_code == 400


def _draft_today(tech, voice=VOICE, photo_date=None):
    log = tech.post("/api/logs", json={}).json()
    up = tech.post("/api/media", data={"log_id": str(log["id"])},
                   files=[("files", ("a.jpg", jpeg(exif_date=photo_date), "image/jpeg")), ("files", ("b.jpg", jpeg((90, 90, 90)), "image/jpeg"))])
    assert up.status_code == 200, up.text
    r = tech.post(f"/api/logs/{log['id']}/draft", json={"voice_text": voice, "weather": "晴 32℃"})
    assert r.status_code == 200, r.text
    return r.json()


def test_full_log_flow(env):
    pm = login("pm")
    _import(pm)
    tech = login("tech1")
    old = (TODAY - timedelta(days=5)).strftime("%Y:%m:%d 08:00:00")
    log = _draft_today(tech, photo_date=old)
    assert len(log["photos"]) == 2 and log["has_ai_draft"] and log["ai_run"]["provider"] == "mock"
    c = log["content"]
    prog = [p for p in c["progress"] if p["activity_code"] == "A1010"]
    assert prog and prog[0]["pct"] == 82 and prog[0]["sources"] == ["V"]
    assert any(i["type"] == "设备" for i in c["issues"])
    assert any(l["trade"] == "钢筋工" and l["count"] == 12 for l in c["labor"])
    msgs = [x["message"] for x in log["checks"]]
    assert any("不是日志当天" in m for m in msgs)  # 照片日期校验
    # 技术员修改：给问题挂接作业，补措施
    c["issues"][0]["activity_code"] = "A1010"
    c["issues"][0]["severity"] = "高"
    c["issues"][0]["measure"] = "调备用钻机"
    saved = tech.put(f"/api/logs/{log['id']}", json={"content": c, "version": log["version"]})
    assert saved.status_code == 200, saved.text
    # 版本冲突
    assert tech.put(f"/api/logs/{log['id']}", json={"content": c, "version": log["version"]}).status_code == 409
    v = saved.json()["version"]
    # 非法百分比拒绝提交
    bad = dict(c, progress=[{"activity_code": "A1010", "qty": None, "unit": "", "pct": 120, "note": "", "sources": ["V"]}])
    v2 = tech.put(f"/api/logs/{log['id']}", json={"content": bad, "version": v}).json()["version"]
    assert tech.post(f"/api/logs/{log['id']}/submit", json={"version": v2}).status_code == 400
    v3 = tech.put(f"/api/logs/{log['id']}", json={"content": c, "version": v2}).json()["version"]
    backdate(log["id"])
    sub = tech.post(f"/api/logs/{log['id']}/submit", json={"version": v3, "active_seconds": 240})
    assert sub.status_code == 200, sub.text
    s = sub.json()
    assert s["status"] == "submitted" and s["edit_seconds"] == 240 and 0 < s["edit_ratio"] < 0.5
    assert tech.put(f"/api/logs/{log['id']}", json={"content": c, "version": s["version"]}).status_code == 400
    # 技术员不能审核；其他技术员看不到
    assert tech.post(f"/api/logs/{log['id']}/review", json={"action": "approve"}).status_code == 403
    assert login("tech2").get(f"/api/logs/{log['id']}").status_code == 403
    assert pm.post(f"/api/logs/{log['id']}/review", json={"action": "approve"}).status_code == 403  # 项目经理不审核日志
    rv = login("admin")   # 日志由工程主管审核；默认测试项目没有工程主管账号，用管理员代审
    assert rv.post(f"/api/logs/{log['id']}/review", json={"action": "return"}).status_code == 400  # 退回需原因
    ok = rv.post(f"/api/logs/{log['id']}/review", json={"action": "approve"}).json()
    assert ok["status"] == "approved" and ok["reviewer"] == "管理员"
    # 进度回填到 P6 作业
    acts = pm.get("/api/p6/activities").json()["items"]
    a = next(x for x in acts if x["code"] == "A1010")
    assert a["log_pct"] == 82 and a["reports"] == 1
    xlsx = pm.get("/api/p6/export-progress")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"


def test_detection_alert_explain_feedback(env):
    pm = login("pm")
    _import(pm)
    tech = login("tech1")
    log = _draft_today(tech)
    c = log["content"]
    c["issues"][0]["activity_code"] = "A1010"
    v = tech.put(f"/api/logs/{log['id']}", json={"content": c, "version": log["version"]}).json()["version"]
    tech.post(f"/api/logs/{log['id']}/submit", json={"version": v})
    r = pm.post("/api/alerts/detect", json={"as_of": TODAY.isoformat()})
    assert r.status_code == 200, r.text
    alerts = pm.get("/api/alerts").json()
    a1010 = next(a for a in alerts if a["activity_code"] == "A1010")
    assert a1010["level"] == "red" and a1010["detail"]["slip_days"] > 1 and a1010["detail"]["actual_source"] == "日报确认"
    assert a1010["explanation"] is not None  # 后台任务已做 AI 分析
    assert any(c["evidence"] == [f"L{log['id']}"] for c in a1010["explanation"]["causes"])
    detail = pm.get(f"/api/alerts/{a1010['id']}").json()
    assert detail["notifications"], "预警应推送给项目部和责任技术员"
    names = {n["name"] for n in detail["notifications"]}
    assert "张工" in names and "李工" not in names
    assert pm.post(f"/api/alerts/{a1010['id']}/feedback", json={"status": "false_alarm"}).status_code == 400
    fb = pm.post(f"/api/alerts/{a1010['id']}/feedback", json={"status": "confirmed", "note": "属实"}).json()
    assert fb["status"] == "confirmed"
    stats = pm.get("/api/alerts/stats").json()
    assert stats["confirmed"] == 1 and stats["precision"] == 1.0
    # 重复判定不重复建预警
    pm.post("/api/alerts/detect", json={"as_of": TODAY.isoformat()})
    assert len([a for a in pm.get("/api/alerts").json() if a["activity_code"] == "A1010"]) == len([a for a in alerts if a["activity_code"] == "A1010"])
    assert tech.get("/api/notifications").json()


def test_report_and_qa_and_metrics(env):
    pm = login("pm")
    _import(pm)
    tech = login("tech1")
    log = _draft_today(tech)
    backdate(log["id"])
    tech.post(f"/api/logs/{log['id']}/submit", json={"version": log["version"], "active_seconds": 300})
    rep = pm.post("/api/reports", json={"kind": "week", "ref_date": TODAY.isoformat()})
    assert rep.status_code == 200, rep.text
    rj = rep.json()
    assert rj["facts"]["logs"]["count"] == 1 and rj["narrative"]["overview"]
    assert any(p["code"] == "A1010" for p in rj["facts"]["progress"])
    doc = pm.get(f"/api/reports/{rj['id']}/docx")
    assert doc.status_code == 200 and doc.content[:2] == b"PK"
    edited = pm.put(f"/api/reports/{rj['id']}", json={"narrative": {**rj["narrative"], "overview": "本周完成桩基 9999 根"}, "status": "draft"}).json()
    assert {"field": "overview", "number": "9999"} in edited["number_check"]
    bad = {**rj["narrative"], "overview": "本周完成桩基 9999 根"}
    r = pm.put(f"/api/reports/{rj['id']}", json={"narrative": bad, "status": "published"})   # 数字守卫后端强制：未核实不能发布
    assert r.status_code == 400 and "9999" in r.json()["detail"]
    pub = pm.put(f"/api/reports/{rj['id']}", json={"narrative": bad, "status": "published", "verified": [{"field": "overview", "number": "9999"}]}).json()
    assert pub["status"] == "published"
    assert pm.put(f"/api/reports/{rj['id']}", json={"narrative": rj["narrative"]}).status_code == 400
    qa = pm.post("/api/ask", json={"question": "3#墩桩基施工最近有什么问题？"})
    assert qa.status_code == 200, qa.text
    assert qa.json()["facts_count"] > 0
    m = login("admin").get("/api/metrics").json()
    assert m["daily_log"]["submitted"] == 1 and m["daily_log"]["ai_minutes"]["median"] == 5.0
    assert m["daily_log"]["time_saving"] is None and m["daily_log"]["time_saving_note"]
    assert login("admin").post("/api/metrics/baseline", json={"kind": "report_week", "minutes": 90, "note": "手工汇总"}).status_code == 200
    assert login("admin").get("/api/metrics").json()["baselines"][0]["minutes"] == 90


def test_manual_baseline_log(env):
    pm = login("pm")
    _import(pm)
    tech = login("tech1")
    log = tech.post("/api/logs", json={"mode": "manual"}).json()
    assert log["mode"] == "manual"
    assert tech.post(f"/api/logs/{log['id']}/draft", json={"voice_text": "x"}).status_code == 400
    content = dict(log["content"], summary="手工填写", works=[{"part": "3#墩", "process": "桩基", "desc": "钻孔", "activity_code": "A1010", "sources": []}])
    v = tech.put(f"/api/logs/{log['id']}", json={"content": content, "version": log["version"]}).json()["version"]
    backdate(log["id"])
    s = tech.post(f"/api/logs/{log['id']}/submit", json={"version": v, "active_seconds": 1800}).json()
    assert s["status"] == "submitted" and s["edit_ratio"] is None
    m = login("admin").get("/api/metrics").json()["daily_log"]
    assert m["manual_submitted"] == 1 and m["manual_minutes"]["median"] == 30.0


def test_backtest_three_periods(env):
    adm = login("admin")
    d0 = (TODAY - timedelta(days=8)).isoformat()
    d1 = (TODAY - timedelta(days=4)).isoformat()
    d2 = TODAY.isoformat()
    s0 = make_xer(d0, start=START, progress={"M0000": 100, "A1010": 20, "A2010": 0, "A3010": 10})
    # S1：A1010 明显落后（应 60% 实 25%），A3010 正常
    s1 = make_xer(d1, start=START, progress={"M0000": 100, "A1010": 25, "A2010": 15, "A3010": 30})
    # S2：A1010 实际推迟 6 天（超出 0 浮时）→ 真实延误；A3010 推迟 2 天但在 20 天浮时内
    s2 = make_xer(d2, start=START, progress={"M0000": 100, "A1010": 70, "A2010": 40, "A3010": 50}, shift={"A1010": 6, "A3010": 2})
    files = [("files", (f"u{i}.xer", s.encode(), "application/octet-stream")) for i, s in enumerate((s2, s0, s1))]
    assert adm.post("/api/p6/backtest", files=files, data={"source": "test"}).status_code in (404, 405)  # 旧接口已下线
    assert login("pm").post("/api/backtest/run", files=files, data={"source": "test"}).status_code == 403
    r = adm.post("/api/backtest/run", files=files, data={"source": "test"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert not res["summary"]["weak"] and res["summary"]["samples"] >= 3 and res["source"] == "test"
    row = next(x for x in res["rows"] if x["code"] == "A1010")
    assert row["predicted"] and row["actual"]
    row3 = next(x for x in res["rows"] if x["code"] == "A3010")
    assert not row3["actual"]
    assert res["summary"]["accuracy_ref"]["rate"] is not None
    assert adm.get("/api/backtest/latest?source=test").json()["summary"]["samples"] == res["summary"]["samples"]
    assert adm.get("/api/metrics").json()["backtest"] is None  # 测试来源不进应用成效


def test_extract_json_and_number_guard():
    assert extract_json('说明```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('<think>xx</think>前缀 {"a": {"b": "}"}} 后缀') == {"a": {"b": "}"}}
    facts = "完成 28 方；累计 60%；L12"
    issues = report_agent.number_guard({"overview": "浇筑28方，累计60%[L12]，节省85%"}, facts)
    assert issues == [{"field": "overview", "number": "85"}]


def test_ai_host_whitelist(monkeypatch):
    from app.ai import client
    from app.config import settings
    monkeypatch.setattr(settings, "ai_provider", "jiaorong")
    monkeypatch.setattr(settings, "ai_api_key", "k")
    monkeypatch.setattr(settings, "ai_base_url", "https://api.openai.com/v1")
    import pytest
    with pytest.raises(client.AIError, match="境内白名单"):
        client._post("m", [], 10, 0)
    monkeypatch.setattr(settings, "ai_base_url", "https://c4ai.ccccltd.cn/api/compatible/v1")
    assert settings.ai_host_allowed()


def test_date_window():
    from app.services.p6 import window_activities
    acts = [{"code": "X", "name": "n", "wbs_path": "3#墩", "early_start": DATA_DATE, "early_finish": TODAY.isoformat(), "act_finish": None, "is_critical": 1},
            {"code": "Y", "name": "n", "wbs_path": "路基", "early_start": DATA_DATE, "early_finish": TODAY.isoformat(), "act_finish": None, "is_critical": 0}]
    assert [a["code"] for a in window_activities(acts, TODAY.isoformat(), "3#墩")] == ["X"]
    assert date.fromisoformat(DATA_DATE) < TODAY


def test_eng_sup_can_draft_but_not_publish_report(env):
    """report_gen 不含发布：工程主管可起草、保存，直接调接口发布返回 403（发布只归 report_publish）。"""
    from test_roles import _team
    _team(env)
    _import(login("pm"))
    tech = login("tech1")
    log = _draft_today(tech)
    backdate(log["id"])
    tech.post(f"/api/logs/{log['id']}/submit", json={"version": log["version"], "active_seconds": 300})
    eng = login("eng")
    rep = eng.post("/api/reports", json={"kind": "week", "ref_date": TODAY.isoformat()})
    assert rep.status_code == 200, rep.text
    rj = rep.json()
    assert eng.put(f"/api/reports/{rj['id']}", json={"narrative": rj["narrative"], "status": "draft"}).status_code == 200
    assert eng.put(f"/api/reports/{rj['id']}", json={"narrative": rj["narrative"], "status": "published"}).status_code == 403
    assert login("pm").put(f"/api/reports/{rj['id']}", json={"narrative": rj["narrative"], "status": "published"}).json()["status"] == "published"
