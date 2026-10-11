"""致分包文书（工笔文书方案 2026-10-08 一.3）：工作联系单、致分包函的下发、签收、回复、销项与催办口径。"""
from datetime import date, timedelta

from conftest import login
from test_site import _world

from app import db as dbmod
from app.services import findings as find_svc
from app.services import nudge as nudge_svc
from app.services import sub_letters as sl
from app.services import todo as todo_svc


def _user(env, name):
    conn = dbmod.connect()
    u = dbmod.one(conn, "SELECT * FROM users WHERE id=?", (env["users"][name],))
    conn.close()
    return u


def _hazard_to_sub(env, ids):
    """把 _world 里的高风险隐患改派给甲单位安全员（分包安全员整改）。"""
    conn = dbmod.connect()
    conn.execute("UPDATE findings SET assignee_id=? WHERE id=?", (env["users"]["sa_a"], ids["hz"]))
    conn.close()
    return ids["hz"]


def _alert(env, status="confirmed"):
    conn = dbmod.connect()
    imp = dbmod.one(conn, "SELECT id FROM p6_imports WHERE project_id=? AND is_current=1", (env["project_id"],))
    aid = dbmod.insert(conn, "alerts", {"project_id": env["project_id"], "import_id": imp["id"], "fingerprint": "t:al1", "activity_code": "A1010", "activity_name": "桩基",
                                        "level": "orange", "rule": "behind", "title": "A1010 桩基 进度滞后", "status": status,
                                        "detail_json": dbmod.dumps({"planned_pct": 60, "actual_pct": 30, "slip_days": 5, "early_finish": "2026-11-01", "forecast_finish": "2026-11-06"}),
                                        "first_detected": dbmod.now_iso(), "last_detected": dbmod.now_iso(), "as_of": dbmod.now_iso()[:10]})
    conn.close()
    return aid


def _todo(env, name, src="letter"):
    conn = dbmod.connect()
    out = [i for i in todo_svc.build(conn, _user(env, name))["items"] if i["source"] == src]
    conn.close()
    return out


def test_candidate_cannot_issue_and_issuer_by_area(env):
    ids = _world(env)
    conn = dbmod.connect()
    cand = find_svc.create(conn, env["project_id"], "safety", "hazard", "high", "候选：未系安全带", {"category": "高处"}, fingerprint="t:c1",
                           assignee_id=env["users"]["sa_a"], due_days=2, candidate=True)
    conn.close()
    ss, eng = login("ss"), login("eng")
    r = ss.post("/api/gongbi/docs", json={"kind": "work_contact", "ref": f"finding:{cand}"})
    assert r.status_code == 400 and "候选" in r.json()["detail"]
    hz = _hazard_to_sub(env, ids)
    assert eng.post("/api/gongbi/docs", json={"kind": "work_contact", "ref": f"finding:{hz}"}).status_code == 403   # 安全类由安全主管签发
    assert login("pm").post("/api/gongbi/docs", json={"kind": "work_contact", "ref": f"finding:{hz}"}).status_code == 403
    assert login("sa_a").post("/api/gongbi/docs", json={"kind": "work_contact", "ref": f"finding:{hz}"}).status_code == 403


def test_safety_contact_issue_ack_and_close_via_rectify(env):
    ids = _world(env)
    hz = _hazard_to_sub(env, ids)
    ss = login("ss")
    d = ss.post("/api/gongbi/docs", json={"kind": "work_contact", "ref": f"finding:{hz}"}).json()
    assert d["company"] == "甲" and d["status"] == "draft" and d["letter"] is None
    assert f"S{hz}" in str(d["narrative"]) and "罚" not in str(d["narrative"]) and "元" not in str(d["narrative"])
    # 人工写进罚款 → 核对提示
    e = ss.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": {"requirement": "逾期罚款 500 元"}}).json()
    assert any(x["issue"] == "出现罚款/处罚字样" for x in e["check"]) and any(x["issue"] == "出现金额字样" for x in e["check"])
    c = ss.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": {"requirement": d["narrative"]["requirement"]}, "confirm": True}).json()
    assert c["status"] == "confirmed" and c["letter"]["status"] == "issued"
    lid = c["letter"]["id"]
    sa_a, sa_b, t1 = login("sa_a"), login("sa_b"), login("tech1")
    assert [x["id"] for x in sa_a.get("/api/gongbi/letters").json()] == [lid]
    assert sa_b.get("/api/gongbi/letters").json() == [] and sa_b.get(f"/api/gongbi/letters/{lid}").status_code == 404
    assert t1.get("/api/gongbi/letters").json() == []                    # 安全类发给分包安全员
    assert _todo(env, "sa_a") and _todo(env, "sa_a")[0]["status"] == "待签收"
    conn = dbmod.connect()
    assert dbmod.one(conn, "SELECT COUNT(*) n FROM notifications WHERE user_id=? AND title LIKE '收到工作联系单%'", (env["users"]["sa_a"],))["n"] == 1
    conn.close()
    a = sa_a.post(f"/api/gongbi/letters/{lid}/ack").json()
    assert a["acked_at"] and a["status"] == "acked" and not _todo(env, "sa_a")
    assert sa_a.post(f"/api/gongbi/letters/{lid}/reply", json={"text": "已整改完毕", "finish_date": "2026-10-10"}).status_code == 400   # 安全类走整改单
    conn = dbmod.connect()
    conn.execute("UPDATE findings SET status='resolved', resolved_at=? WHERE id=?", (dbmod.now_iso(), hz))
    conn.close()
    assert sa_a.get(f"/api/gongbi/letters/{lid}").json()["status"] == "closed"
    assert ss.get(f"/api/gongbi/docs/{d['id']}/docx").status_code == 200


def test_progress_contact_reply_accept_does_not_touch_progress(env):
    _world(env)
    eng = login("eng")
    unconfirmed = _alert(env, status="open")
    assert eng.post("/api/gongbi/docs", json={"kind": "work_contact", "ref": f"alert:{unconfirmed}|甲"}).status_code == 400
    conn = dbmod.connect()
    conn.execute("UPDATE alerts SET status='confirmed' WHERE id=?", (unconfirmed,))
    before = (dbmod.all_(conn, "SELECT * FROM log_progress ORDER BY id"), dbmod.one(conn, "SELECT * FROM alerts WHERE id=?", (unconfirmed,)))
    conn.close()
    d = eng.post("/api/gongbi/docs", json={"kind": "work_contact", "ref": f"alert:{unconfirmed}|甲"}).json()
    assert d["area"] == "progress" and "赶工措施" in d["narrative"]["requirement"] and f"W{unconfirmed}" in str(d["narrative"])
    lid = eng.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": {}, "confirm": True}).json()["letter"]["id"]
    sa_a, t1 = login("sa_a"), login("tech1")
    assert sa_a.get("/api/gongbi/letters").json() == []                 # D9：进度类不发分包安全员
    assert [x["id"] for x in t1.get("/api/gongbi/letters").json()] == [lid]
    assert t1.post(f"/api/gongbi/letters/{lid}/reply", json={"text": "增加一台旋挖钻", "finish_date": "bad"}).status_code == 400
    r = t1.post(f"/api/gongbi/letters/{lid}/reply", json={"text": "增加一台旋挖钻，两班作业", "finish_date": "2026-11-04"}).json()
    assert r["status"] == "replied" and r["acked_at"]
    assert _todo(env, "eng") and any("已回复" in i["title"] for i in _todo(env, "eng"))
    assert eng.post(f"/api/gongbi/letters/{lid}/review", json={"accept": False}).status_code == 400   # 退回须填原因
    rt = eng.post(f"/api/gongbi/letters/{lid}/review", json={"accept": False, "reason": "措施不具体"}).json()
    assert rt["status"] == "returned" and rt["can_reply"] is False
    t1.post(f"/api/gongbi/letters/{lid}/reply", json={"text": "增加一台旋挖钻，夜间加班，11月4日完成", "finish_date": "2026-11-04"})
    ok = eng.post(f"/api/gongbi/letters/{lid}/review", json={"accept": True}).json()
    assert ok["status"] == "closed"
    conn = dbmod.connect()
    after = (dbmod.all_(conn, "SELECT * FROM log_progress ORDER BY id"), dbmod.one(conn, "SELECT * FROM alerts WHERE id=?", (unconfirmed,)))
    conn.close()
    assert after == before                                                  # 接受回复不改任何进度数据
    assert login("pm").post(f"/api/gongbi/letters/{lid}/review", json={"accept": True}).status_code in (400, 403)


def test_unacked_24h_enters_nudge_chain(env):
    _world(env)
    eng = login("eng")
    aid = _alert(env)
    d = eng.post("/api/gongbi/docs", json={"kind": "work_contact", "ref": f"alert:{aid}|甲"}).json()
    lid = eng.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": {}, "confirm": True}).json()["letter"]["id"]
    conn = dbmod.connect()
    today = date.today()
    assert sl.nudge_targets(conn, env["project_id"], today) == []
    old = (today - timedelta(days=2)).isoformat() + "T08:00:00"
    conn.execute("UPDATE sub_letters SET issued_at=? WHERE id=?", (old, lid))
    t = sl.nudge_targets(conn, env["project_id"], today)
    assert len(t) == 1 and t[0]["area"] == "letter_progress" and "未签收" in t[0]["subject"]
    users = {u["id"]: u for u in dbmod.all_(conn, "SELECT * FROM users WHERE project_id=?", (env["project_id"],))}
    tgt = nudge_svc._target("letter", t[0]["key"], t[0]["area"], t[0]["subject"], t[0]["due"], today, t[0]["owner_id"], t[0]["link"], t[0]["ref_id"])
    assert nudge_svc._recipients(1, tgt, users)[0][0]["id"] == env["users"]["tech1"]
    assert nudge_svc._recipients(2, tgt, users)[0] == []                    # 进度类不发分包安全员
    assert {u["role"] for u in nudge_svc._recipients(3, tgt, users)[0]} == {"eng_sup"}
    assert nudge_svc._recipients(4, tgt, users)[0] == []                    # 止于工程主管
    targets, _ = nudge_svc.collect_targets(conn, env["project_id"], nudge_svc.load_rules(conn, env["project_id"]), today, users)
    assert any(x["type"] == "letter" for x in targets)
    conn.close()
    assert any("超过 24 小时未签收" in i["title"] for i in _todo(env, "eng"))


def test_letter_to_sub_needs_pm_confirm(env):
    ids = _world(env)
    _hazard_to_sub(env, ids)
    ss, pm = login("ss"), login("pm")
    refs = ss.get("/api/gongbi/refs", params={"kind": "letter_to_sub"}).json()
    assert any(r["key"] == "甲|safety" for r in refs)
    d = ss.post("/api/gongbi/docs", json={"kind": "letter_to_sub", "ref": "甲|safety"}).json()
    assert d["kind"] == "letter_to_sub" and d["tables"]["fact"]["rows"] and d["can_confirm"] is False
    assert ss.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": {}, "confirm": True}).status_code == 403
    c = pm.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": {}, "confirm": True}).json()
    assert c["status"] == "confirmed" and c["letter"]["status"] == "issued"
    v = login("sa_a").get(f"/api/gongbi/letters/{c['letter']['id']}").json()
    assert v["cc"] >= 1 and v["company"] == "甲"                            # 抄送该单位其他账号
    assert login("tech1").get(f"/api/gongbi/letters/{c['letter']['id']}").status_code == 200
    assert ss.post("/api/gongbi/docs", json={"kind": "letter_to_sub", "ref": "丙|safety"}).status_code == 400


def test_tech_has_no_gongbi_docs(env):
    """RJ 10-09：技术员是分包岗位，没有工笔文书、不能给分包发文；只能签收、回复发给自己的联系单。"""
    ids = _world(env)
    hz = _hazard_to_sub(env, ids)
    t1 = login("tech1")
    for path in ("/api/gongbi/types", "/api/gongbi/docs", "/api/gongbi/refs?kind=work_contact", "/api/reports"):
        assert t1.get(path).status_code == 403, path
    for kind, ref in (("work_contact", f"finding:{hz}"), ("letter_to_sub", "甲|safety"), ("week", date.today().isoformat())):
        assert t1.post("/api/gongbi/docs", json={"kind": kind, "ref": ref}).status_code == 403, kind
    assert t1.post("/api/reports", json={"kind": "week"}).status_code == 403
    assert t1.get("/api/gongbi/letters").status_code == 200          # 接收发给自己的联系单照旧


def test_admin_cannot_generate_reports(env):
    """RJ 10-09：系统管理员不生成报表（周月报、英文周月报等工笔文书），也不审核发布；查看不受影响。"""
    from app import roles as R
    assert not R.can("admin", "report_gen") and not R.can("admin", "report_publish")
    adm = login("admin")
    assert adm.post("/api/reports", json={"kind": "week"}).status_code == 403
    for kind in ("weekly_en", "monthly_en"):
        assert adm.post("/api/gongbi/docs", json={"kind": kind, "ref": date.today().isoformat()}).status_code == 403, kind
    assert adm.get("/api/reports").status_code == 200
    assert not any(t["can_draft"] or t["can_confirm"] for t in adm.get("/api/gongbi/types").json()["types"])
