"""AI 来历、反馈汇总、示例问题：只读汇总，数据缺失如实留空，不造比例。"""
from datetime import date

from conftest import login

from app import db as dbmod
from app.services import ai_trace
from app.services import findings as find_svc


def _conn():
    return dbmod.connect()


def _finding(env, agent="safety", level="high", title="3#墩临边无防护", **kw):
    conn = _conn()
    try:
        run = dbmod.insert(conn, "ai_runs", {"project_id": env["project_id"], "kind": "safety.scan", "provider": "mock", "model": "mock",
                                              "prompt_version": "safety-v1", "ok": 1, "latency_ms": 120, "created_at": dbmod.now_iso()})
        return find_svc.create(conn, env["project_id"], agent, "hazard", level, title, {"desc": "临边未设防护栏杆", "measure": "补设栏杆"},
                               ref_type="media", ref_id=7, activity_code="A1010", ai_run_id=run, **kw)
    finally:
        conn.close()


def _alert(env, status="open"):
    conn = _conn()
    try:
        return dbmod.insert(conn, "alerts", {
            "project_id": env["project_id"], "import_id": 1, "fingerprint": "fp-" + status, "activity_code": "A1010", "activity_name": "桩基",
            "level": "red", "rule": "critical_slip", "title": "桩基滞后", "detail_json": dbmod.dumps({"slip_days": 5, "total_float_d": 0}),
            "explanation_json": dbmod.dumps({"judgement": "关键路径滞后"}), "status": status, "first_detected": dbmod.now_iso(),
            "last_detected": dbmod.now_iso(), "as_of": date.today().isoformat()})
    finally:
        conn.close()


def test_trace_finding_shows_basis_run_and_human_step(env):
    fid = _finding(env)
    pm = login("pm")
    assert pm.post(f"/api/findings/{fid}/status", json={"status": "dismissed", "note": "已有防护，拍摄角度问题"}).status_code == 200
    t = pm.get(f"/api/ai/trace?type=finding&id={fid}").json()
    assert t["detector"] == "AI 识别" and t["run"]["prompt_version"] == "safety-v1" and t["run"]["mock"] is True
    assert any("临边未设防护栏杆" in b for b in t["basis"]) and any("A1010" in b for b in t["basis"])
    assert t["human"]["by"] == "项目经理" and "拍摄角度" in t["human"]["note"]
    whats = [s["what"] for s in t["steps"]]
    assert whats[0] == "发现并生成事项" and "驳回/忽略" in whats


def test_trace_without_ai_run_says_so(env):
    conn = _conn()
    fid = find_svc.create(conn, env["project_id"], "quality", "reported", "medium", "日志提到养护不足", {"desc": "x"})
    conn.close()
    t = login("pm").get(f"/api/ai/trace?type=finding&id={fid}").json()
    assert t["run"] is None and "未调用 AI" in t["detector"] and t["human"] is None


def test_trace_alert_marks_rule_not_ai(env):
    aid = _alert(env)
    t = login("pm").get(f"/api/ai/trace?type=alert&id={aid}").json()
    assert "非 AI 判断" in t["basis"][0] and any("滞后 5 天" in b for b in t["basis"])
    assert t["steps"][0]["who"] == "规则引擎"


def test_trace_guards(env):
    pm = login("pm")
    assert pm.get("/api/ai/trace?type=finding&id=9999").status_code == 404
    assert pm.get("/api/ai/trace?type=bogus&id=1").status_code == 400
    assert login("tech1").get("/api/ai/trace?type=finding&id=1").status_code == 403    # 分包/技术员不走项目级汇总


def test_feedback_summary_counts_and_hides_rate_when_few(env):
    conn = _conn()
    a, b = _finding(env, title="a"), _finding(env, title="b")
    conn.execute("UPDATE findings SET status='resolved' WHERE id=?", (a,))
    conn.execute("UPDATE findings SET status='dismissed', feedback_note='已有防护' WHERE id=?", (b,))
    conn.commit()
    fb = ai_trace.feedback_summary(conn, env["project_id"])["safety"]
    conn.close()
    assert (fb["decided"], fb["adopted"], fb["rejected"]) == (2, 1, 1)
    assert fb["rate"] is None                       # 样本不足 3，不给比例
    assert fb["reasons"] == [{"text": "已有防护", "n": 1}]


def test_feedback_rate_and_alert_reasons(env):
    conn = _conn()
    for i in range(3):
        fid = _finding(env, title=f"f{i}")
        conn.execute("UPDATE findings SET status='resolved' WHERE id=?", (fid,))
    aid = _alert(env, "false_alarm")
    conn.execute("UPDATE alerts SET feedback_by=?, feedback_note='浮时算错' WHERE id=?", (env["users"]["pm"], aid))
    conn.commit()
    fb = ai_trace.feedback_summary(conn, env["project_id"])
    conn.close()
    assert fb["safety"]["rate"] == 1.0
    assert fb["progress"]["rejected"] == 1 and fb["progress"]["reasons"][0]["text"] == "浮时算错"
    ag = {a["key"]: a for a in login("pm").get("/api/agents").json()["agents"]}
    assert ag["safety"]["feedback"]["adopted"] == 3 and ag["quality"]["feedback"] is None


def test_suggestions_follow_real_data(env):
    pm = login("pm")
    assert pm.get("/api/ask/suggest").json()["items"] == []        # 空项目不出题
    _finding(env)
    _alert(env)
    qs = [i["q"] for i in pm.get("/api/ask/suggest").json()["items"]]
    assert any("高风险隐患" in q for q in qs) and any("红橙预警" in q for q in qs)
    assert len(qs) <= 5 and all(i["why"] for i in pm.get("/api/ask/suggest").json()["items"])
    assert login("tech1").get("/api/ask/suggest").status_code == 403


def test_trace_onspot_shows_human_close_without_rectify_photo(env):
    """D6：“当场已纠正”在来历里写明由谁闭环、“当场纠正、无整改照片”，状态名带“当场纠正”。"""
    fid = _finding(env, candidate=True)
    pm = login("pm")
    assert pm.post("/api/candidates/batch", json={"items": [{"id": fid, "action": "onspot"}]}).status_code == 200
    t = pm.get(f"/api/ai/trace?type=finding&id={fid}").json()
    assert t["status"] == "resolved" and "当场纠正" in t["status_name"]
    assert t["human"]["by"] == "项目经理" and t["human"]["note"] == "当场纠正、无整改照片"
    assert [s["what"] for s in t["steps"]][-1] == "确认闭环"
    conn = _conn()
    fb = ai_trace.feedback_summary(conn, env["project_id"])["safety"]
    conn.close()
    assert (fb["adopted"], fb["rejected"]) == (1, 0)
