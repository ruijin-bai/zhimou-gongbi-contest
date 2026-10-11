from datetime import timedelta

from conftest import TODAY, backdate, jpeg, login, sample_xer

from app import db as dbmod

LAST_MON = TODAY - timedelta(days=TODAY.weekday() + 7)


def _weekly_project(env):
    conn = dbmod.connect()
    conn.execute("UPDATE projects SET config_json='{\"cadence\":\"week\",\"daily_log\":true}' WHERE id=?", (env["project_id"],))
    conn.close()


def _note_on(tech, day, text, photo=False):
    media = []
    if photo:
        media = [m["id"] for m in tech.post("/api/media", files=[("files", ("n.jpg", jpeg((80, 120, 160)), "image/jpeg"))]).json()]
    n = tech.post("/api/notes", json={"text": text, "media_ids": media, "seconds": 12, "source": "voice"})
    assert n.status_code == 200, n.text
    conn = dbmod.connect()
    conn.execute("UPDATE notes SET note_date=? WHERE id=?", (day.isoformat(), n.json()["id"]))
    # 测试中把“今天”的随手记改到过去日期：照片与后台整理出的今日日志一并解除
    conn.execute("UPDATE media SET log_id=NULL")
    conn.execute("DELETE FROM logs WHERE log_date=? AND period='day' AND auto=1", (TODAY.isoformat(),))
    conn.close()
    r = tech.post("/api/logs/organize", json={"day": day.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def test_notes_daily_auto_and_weekly_confirm(env):
    _weekly_project(env)
    pm = login("pm")
    assert pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(data_date=(LAST_MON - timedelta(days=1)).isoformat()),
                                                     "application/octet-stream")}).status_code == 200
    tech = login("tech1")
    d1 = _note_on(tech, LAST_MON, "3#墩桩基施工今天完成2根，累计完成30%。钢筋工12人", photo=True)
    assert d1["auto"] and d1["period"] == "day" and d1["status"] == "draft" and d1["photos"]
    _note_on(tech, LAST_MON + timedelta(days=1), "3#墩桩基施工完成3根，累计完成45%。旋挖钻故障停机半天")
    _note_on(tech, LAST_MON + timedelta(days=2), "3#墩桩基施工完成1根，累计完成50%")
    # 每日日志不计入正式进度
    conn = dbmod.connect()
    assert conn.execute("SELECT COUNT(*) n FROM log_progress").fetchone()["n"] == 0
    conn.close()
    wk = tech.post("/api/logs", json={"period": "week", "log_date": (LAST_MON + timedelta(days=3)).isoformat()}).json()
    assert wk["log_date"] == LAST_MON.isoformat() and wk["period_end"] == (LAST_MON + timedelta(days=6)).isoformat()
    assert sum(1 for d in wk["days"] if d["log_id"]) == 3
    dr = tech.post(f"/api/logs/{wk['id']}/draft", json={"voice_text": ""})
    assert dr.status_code == 200, dr.text
    w = dr.json()
    pr = next(p for p in w["content"]["progress"] if p["activity_code"] == "A1010")
    assert pr["qty"] == 6 and pr["pct"] == 50  # 每日合计 2+3+1，取最后累计%
    assert w["content"]["issues"]
    # 改成与每日合计不一致 → 规则提示
    c = w["content"]
    c["progress"][0]["qty"] = 9
    v = tech.put(f"/api/logs/{wk['id']}", json={"content": c, "version": w["version"]}).json()
    assert any("不一致" in x["message"] for x in v["checks"])
    c["progress"][0]["qty"] = 6
    v = tech.put(f"/api/logs/{wk['id']}", json={"content": c, "version": v["version"]}).json()
    backdate(wk["id"])
    s = tech.post(f"/api/logs/{wk['id']}/submit", json={"version": v["version"], "active_seconds": 180})
    assert s.status_code == 200, s.text
    assert all(d["status"] == "submitted" for d in s.json()["days"] if d["log_id"])
    conn = dbmod.connect()
    rows = conn.execute("SELECT * FROM log_progress").fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0]["qty"] == 6 and rows[0]["log_date"] == (LAST_MON + timedelta(days=6)).isoformat()
    # 项目部审核周确认 → 每日日志一并通过
    ok = login("admin").post(f"/api/logs/{wk['id']}/review", json={"action": "approve"}).json()  # 项目经理不审核日志
    assert ok["status"] == "approved" and all(d["status"] == "approved" for d in ok["days"] if d["log_id"])
    a = next(x for x in pm.get("/api/p6/activities").json()["items"] if x["code"] == "A1010")
    assert a["log_pct"] == 50
    rep = pm.post("/api/reports", json={"kind": "week", "ref_date": LAST_MON.isoformat()}).json()
    assert rep["facts"]["logs"]["count"] == 3 and rep["facts"]["logs"]["weeks"] == 1
    assert next(p for p in rep["facts"]["progress"] if p["code"] == "A1010")["qty"][0]["qty"] == 6
    m = login("admin").get("/api/metrics").json()["daily_log"]
    assert m["cadence"] == "week" and m["submitted"] == 1 and m["week_minutes"]["median"] == round((36 + 180) / 60, 2)
    # 已确认的随手记不能删除
    conn = dbmod.connect()
    nid = conn.execute("SELECT id FROM notes ORDER BY id LIMIT 1").fetchone()["id"]
    conn.close()
    assert tech.delete(f"/api/notes/{nid}").status_code == 400


def test_tech_home_and_risk_from_auto_log(env):
    _weekly_project(env)
    pm = login("pm")
    pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    tech = login("tech1")
    tech.post("/api/notes", json={"text": "3#墩桩基施工旋挖钻故障停工，等配件", "seconds": 8, "source": "voice"})
    h = tech.get("/api/tech/home").json()
    assert h["notes"] and h["week"]["id"] and len(h["week"]["days"]) == 7 and h["daily_log"] is True
    # 后台已把随手记整理成今日日志（未确认），其中的问题即可触发风险信号
    lg = h["day_log"]
    assert lg and lg["status"] == "draft"
    d = tech.get(f"/api/logs/{lg['id']}").json()
    d["content"]["issues"][0]["activity_code"] = "A1010"
    d["content"]["issues"][0]["severity"] = "高"
    tech.put(f"/api/logs/{lg['id']}", json={"content": d["content"], "version": d["version"]})
    pm.post("/api/alerts/detect", json={"as_of": TODAY.isoformat()})
    alerts = pm.get("/api/alerts?status=").json()
    assert any(a["activity_code"] == "A1010" and a["detail"].get("risk_signals") for a in alerts)
    assert pm.get("/api/queue").status_code == 404   # 旧待办队列接口已下线
    assert not {"weeks", "logs"} & set(pm.get("/api/cockpit").json()["kpis"])  # 项目经理口径：KPI 不含日志/周确认填报（产值只读可看）
    from app.services import brief
    conn = dbmod.connect()
    try:
        k = brief.kpis(conn, env["project_id"], TODAY)
    finally:
        conn.close()
    assert "weeks" in k and k["logs"]["notes_today"] == 1   # 服务层口径不变（工程主管等仍可见）
