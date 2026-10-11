"""成效页“AI 产出被人采纳情况”：只用既有记录；样本不足不给比例，没有记录不补零。"""
from conftest import login

from app import db as dbmod
from app.services import findings as find_svc
from app.services import metrics


def _runs(env, kind, n, ok=1, meta=None, provider="jiaorong"):
    conn = dbmod.connect()
    try:
        for _ in range(n):
            dbmod.insert(conn, "ai_runs", {"project_id": env["project_id"], "kind": kind, "provider": provider, "model": "m", "prompt_version": "v1",
                                           "input_meta_json": dbmod.dumps(meta or {}), "ok": ok, "latency_ms": 100, "created_at": dbmod.now_iso()})
    finally:
        conn.close()


def _decide_findings(env, resolved=0, dismissed=0, notes=()):
    conn = dbmod.connect()
    try:
        i = 0
        for status, count in (("resolved", resolved), ("dismissed", dismissed)):
            for _ in range(count):
                fid = find_svc.create(conn, env["project_id"], "safety", "hazard", "high", f"隐患{i}", {"desc": "x"})
                note = notes[i % len(notes)] if (status == "dismissed" and notes) else ""
                conn.execute("UPDATE findings SET status=?, feedback_by=?, feedback_note=? WHERE id=?", (status, env["users"]["pm"], note, fid))
                i += 1
    finally:
        conn.close()


def _notes_classify(env, accepted=0, dismissed=0):
    conn = dbmod.connect()
    try:
        for dec, count in (("accepted", accepted), ("dismissed", dismissed)):
            for _ in range(count):
                nid = dbmod.insert(conn, "notes", {"project_id": env["project_id"], "author_id": env["users"]["tech1"], "note_date": "2026-10-01",
                                                   "text": "x", "created_at": dbmod.now_iso()})
                dbmod.insert(conn, "note_classify", {"note_id": nid, "project_id": env["project_id"], "user_id": env["users"]["tech1"],
                                                     "decision": dec, "decided_kind": "hazard", "decided_at": dbmod.now_iso(), "created_at": dbmod.now_iso()})
    finally:
        conn.close()


def _row(env, key):
    conn = dbmod.connect()
    try:
        out = metrics.summary(conn, env["project_id"])["ai_adoption"]
    finally:
        conn.close()
    return out, next((r for r in out["agents"] if r["key"] == key), None)


def test_empty_project_has_no_numbers(env):
    out, row = _row(env, "safety")
    assert out["agents"] == [] and row is None and "安全Agent" in out["idle"]


def test_call_stats_count_repair_regen_degraded_and_success(env):
    _runs(env, "log.draft", 4)
    _runs(env, "log.draft", 1, meta={"repaired": True})
    _runs(env, "log.draft", 1, ok=0, meta={"regen": True, "degraded": "按降级规则取用"})
    _runs(env, "ai.test", 3)                                   # 连通测试不计入
    out, r = _row(env, "scribe")
    assert r["calls"] == 6 and r["success"]["k"] == 5 and r["success"]["rate"] == round(5 / 6, 4)
    assert (r["repaired"], r["regen"], r["degraded"]) == (1, 1, 1)
    assert [x["key"] for x in out["agents"]] == ["scribe"]


def test_small_sample_gives_no_percentage(env):
    _runs(env, "safety.scan", 3)
    _decide_findings(env, resolved=3, dismissed=1)
    _, r = _row(env, "safety")
    assert r["success"]["n"] == 3 and r["success"]["rate"] is None            # <5：样本不足
    assert (r["adopted"], r["rejected"]) == (3, 1) and r["adoption"]["rate"] is None and r["adoption"]["n"] == 4


def test_adoption_rejection_and_reasons(env):
    _runs(env, "safety.scan", 6)
    _decide_findings(env, resolved=6, dismissed=4, notes=["已有防护，拍摄角度问题", "已有防护，拍摄角度问题", "重复上报"])
    _notes_classify(env, accepted=1, dismissed=1)                                # 随手记归类属现场记录Agent，不混入安全Agent
    _, r = _row(env, "safety")
    assert r["adoption"] == {"k": 6, "n": 10, "rate": 0.6} and r["rejection"] == {"k": 4, "n": 10, "rate": 0.4}
    assert r["reasons"][0] == {"text": "已有防护，拍摄角度问题", "n": 3}
    _, s = _row(env, "scribe")
    assert (s["adopted"], s["rejected"]) == (1, 1) and s["sources"][0]["label"].startswith("随手记归类建议") and s["adoption"]["rate"] is None


def test_alert_feedback_counts_for_progress_agent(env):
    conn = dbmod.connect()
    try:
        for i in range(5):
            dbmod.insert(conn, "alerts", {
                "project_id": env["project_id"], "import_id": 1, "fingerprint": f"fp{i}", "activity_code": "A1", "activity_name": "桩基", "level": "red",
                "rule": "r", "title": "t", "detail_json": "{}", "status": "confirmed" if i < 4 else "false_alarm", "feedback_by": env["users"]["pm"],
                "feedback_note": "" if i < 4 else "已赶回", "first_detected": dbmod.now_iso(), "last_detected": dbmod.now_iso(), "as_of": "2026-10-01"})
    finally:
        conn.close()
    _, r = _row(env, "progress")
    assert r["adoption"] == {"k": 4, "n": 5, "rate": 0.8} and r["reasons"] == [{"text": "已赶回", "n": 1}]
    assert r["calls"] == 0 and r["success"]["n"] == 0                           # 没调用记录就是没有，不补零


def test_command_cards_are_confirm_only_not_adoption_rate(env):
    conn = dbmod.connect()
    try:
        for i in range(6):
            dbmod.event(conn, "cmd.parse", project_id=env["project_id"], user_id=env["users"]["pm"], data={"action": "assign", "ready": i < 5})
        for _ in range(2):
            dbmod.event(conn, "cmd.exec", project_id=env["project_id"], user_id=env["users"]["pm"], data={"type": "assign"})
    finally:
        conn.close()
    _, r = _row(env, "dispatch")
    assert r["confirm_only"] == [{"label": "指令动作卡（确认执行/提出）", "done": 2, "offered": 5}]
    assert r["adoption"]["n"] == 0                                              # 动作卡没有“驳回”记录，不算采纳率


def test_edit_ratio_reused_for_scribe_with_min_samples(env):
    conn = dbmod.connect()
    try:
        for i in range(3):
            conn.execute("INSERT INTO logs (project_id, author_id, log_date, mode, status, content_json, started_at, submitted_at, edit_ratio, period, ai_draft_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (env["project_id"], env["users"]["tech1"], f"2026-10-0{i + 1}", "ai", "submitted", "{}",
                                                           "2026-10-01T08:00:00", "2026-10-01T09:00:00", 0.2, "day", "{}"))
    finally:
        conn.close()
    _runs(env, "log.draft", 1)
    _, r = _row(env, "scribe")
    assert r["edit_ratio"] == {"n": 3, "median": None}                          # 3<5：样本不足，不给中位数百分比


def test_simulation_and_mock_flags_and_endpoint(env):
    conn = dbmod.connect()
    try:
        conn.execute("UPDATE projects SET config_json=? WHERE id=?", (dbmod.dumps({"cadence": "day", "simulation": True}), env["project_id"]))
    finally:
        conn.close()
    _runs(env, "report.week", 1, provider="mock")
    pm = login("pm")
    a = login("admin").get("/api/metrics").json()["ai_adoption"]
    assert a["simulation"] is True and a["has_mock"] is True and a["agents"][0]["mock"] == 1
    assert login("tech1").get("/api/metrics").status_code == 403


def test_onspot_counts_as_adopted_and_dismiss_reason_labels_are_counted(env):
    """D6 口径变化：候选“当场已纠正”直接闭环入台账，计为 AI 采纳；不采纳按选项原因计入驳回原因。确认后仍在整改中的不计。"""
    conn = dbmod.connect()
    try:
        ss = conn.execute("SELECT * FROM users WHERE username='pm'").fetchone()
        ids = [find_svc.create(conn, env["project_id"], "safety", "hazard", "medium", f"候选{i}", {"desc": "x"}, ref_type="media", ref_id=1,
                               assignee_id=env["users"]["tech1"], candidate=True) for i in range(6)]
        find_svc.decide_batch(conn, ss, [{"id": i, "action": "onspot"} for i in ids[:3]]
                              + [{"id": ids[3], "action": "confirm"}]
                              + [{"id": i, "action": "dismiss", "reason_code": "misread"} for i in ids[4:]])
    finally:
        conn.close()
    _, r = _row(env, "safety")
    assert (r["adopted"], r["rejected"]) == (3, 2)
    assert r["reasons"] == [{"text": find_svc.DISMISS_REASONS["misread"], "n": 2}]
