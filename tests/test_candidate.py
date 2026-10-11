"""T04：AI 识别的隐患先作“候选·待确认”，人确认后才派单、计入台账。

- 候选不进台账统计、超期督办、闭环率、分包对比，不通知整改人；
- 本单位安全员 / 安全主管可【确认并派单】【修改后确认】【不采纳（必填原因）】，分包技术员、工程主管无权；
- 整改提交人不能闭环自己的整改；
- 旧库迁移：历史隐患视为已确认（confirmed_at 回填 created_at），台账数字不变。
"""
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, timedelta

from conftest import jpeg, login, sample_xer
from test_roles import _add, _team

from app import db as dbmod
from app.services import brief, nudge, qa, reports, role_views
from app.services import findings as F


def _snap(c, color=(200, 50, 50)):
    c.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg(color), "image/jpeg"))])


def _cand(env, who="tech1"):
    """某人随手拍一张照片 → 安全 Agent（离线模拟）识别出 1 处隐患 → 候选。返回 (拍照人客户端, finding id)。"""
    _team(env)
    c = login(who)
    _snap(c)
    ids = [f["id"] for f in c.get("/api/findings?candidates=only").json()]
    assert len(ids) == 1
    return c, ids[0]


def _row(fid):
    conn = dbmod.connect()
    try:
        return conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    finally:
        conn.close()


def _notes(c, kw):
    return [n for n in c.get("/api/notifications").json() if kw in n["title"]]


# ----------------------------------------------------------------------------- 1 默认候选，不进任何台账口径

def test_ai_hazard_is_candidate_and_stays_out_of_ledger(env):
    tech, fid = _cand(env)
    row = _row(fid)
    assert row["kind"] == "hazard" and row["confirmed_at"] is None and row["confirmed_by"] is None and row["status"] == "open"
    v = tech.get(f"/api/findings/{fid}").json()
    assert v["candidate"] is True and v["status_name"] == "候选·待确认" and v["overdue"] is False
    # 默认台账列表（含技术员“我的待办”）不含候选；candidates=only 才能看到
    assert tech.get("/api/findings?agent=safety&status=active&mine=1").json() == []
    assert tech.get("/api/tech/home").json()["tasks"] == []
    assert [f["id"] for f in tech.get("/api/findings?candidates=all").json()] == [fid]
    ss, sa_a, sa_b = login("ss"), login("sa_a"), login("sa_b")
    assert ss.get("/api/findings?agent=safety&status=active").json() == []
    # 安全主管驾驶舱：台账口径全为 0，候选单列
    ov = ss.get("/api/safety/overview").json()
    k = ov["kpis"]
    assert (k["open"], k["overdue"], k["total"], k["ai_found"], k["unassigned"]) == (0, 0, 0, 0, 0) and k["close_rate_30d"] is None
    assert ov["by_company"] == [] and ov["by_category"] == [] and ov["review"] == [] and ov["overdue"] == []
    assert ov["candidate_count"] == 1 and ov["candidates"][0]["id"] == fid
    assert sum(t["new"] for t in ov["trend"]) == 0
    # 分包安全员首页：整改数不含候选，候选单列（只有本单位）
    h = sa_a.get("/api/safety/home").json()
    assert h["counts"]["pending"] == 0 and h["counts"]["new_week"] == 0 and h["due"] == []
    assert h["candidate_count"] == 1 and h["candidates"][0]["id"] == fid
    assert sa_b.get("/api/safety/home").json()["candidate_count"] == 0
    # 简报 KPI、项目总览、周报事实、提问事实、分包一览都不含候选
    conn = dbmod.connect()
    try:
        pid = env["project_id"]
        assert brief.kpis(conn, pid, date.today())["safety"] == {"open": 0, "high": 0, "overdue": 0, "resolved": 0, "total": 0}
        assert all(c["safety_open"] == 0 for c in role_views.company_rollup(conn, pid))
        day = date.today().isoformat()
        assert reports.build_facts(conn, pid, day, day)["safety"]["found"] == 0
        assert qa._findings_facts(conn, pid, "safety", []) == []
        assert F.stats(conn, pid)["safety"]["total"] == 0 and F.stats(conn, pid)["safety"]["candidate"] == 1
        assert F.list_for(conn, pid, agent="safety", status="active") == []
        assert F.candidate_count(conn, pid) == 1
    finally:
        conn.close()


def test_candidate_never_overdue_and_never_nudged(env):
    tech, fid = _cand(env)
    conn = dbmod.connect()
    conn.execute("UPDATE findings SET due_date=? WHERE id=?", ((date.today() - timedelta(days=5)).isoformat(), fid))  # 放置多日、期限已过
    conn.close()
    ss = login("ss")
    assert ss.get("/api/safety/overview").json()["kpis"]["overdue"] == 0 and ss.get("/api/safety/overview").json()["overdue"] == []
    assert tech.get(f"/api/findings/{fid}").json()["overdue"] is False
    conn = dbmod.connect()
    try:
        assert nudge._collect_findings(conn, env["project_id"], nudge.DEFAULT_RULES, date.today()) == ([], [])
        assert brief.kpis(conn, env["project_id"], date.today())["safety"]["overdue"] == 0
    finally:
        conn.close()
    assert [n for n in tech.get("/api/notifications").json() if "催办" in n["title"] or "派单" in n["title"]] == []


def test_candidate_cannot_be_assigned_nudged_rectified_or_closed(env):
    tech, fid = _cand(env)
    ss, sa_a = login("ss"), login("sa_a")
    assert ss.put(f"/api/findings/{fid}/assignee", json={"assignee_id": env["users"]["tech1"]}).status_code == 400
    assert ss.post(f"/api/findings/{fid}/nudge", json={"text": "x"}).status_code == 400
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 400
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "open"}).status_code == 400
    rect = tech.post("/api/media", data={"purpose": "rectify"}, files=[("files", ("r.jpg", jpeg((20, 210, 20)), "image/jpeg"))]).json()[0]
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": rect["id"]}).status_code == 400
    assert _row(fid)["confirmed_at"] is None and _row(fid)["status"] == "open"
    assert tech.get("/api/notifications").json() == [] or not _notes(tech, "派单")


# ----------------------------------------------------------------------------- 2 确认并派单 → 回到原流程

def test_confirm_and_dispatch_enters_original_flow(env):
    tech, fid = _cand(env)
    sa_a, ss = login("sa_a"), login("ss")
    r = sa_a.post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"]["tech1"]})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["candidate"] is False and v["status"] == "open" and v["status_name"] == "待处理"
    assert v["confirmed_by"] == env["users"]["sa_a"] and v["confirmed_at"] and v["assignee_id"] == env["users"]["tech1"]
    assert v["due_date"] == (date.today() + timedelta(days=2)).isoformat()  # 中风险：从确认当天起 2 天
    # 原流程：台账、驾驶舱、技术员待办、派单通知
    assert [f["id"] for f in tech.get("/api/findings?agent=safety&status=active&mine=1").json()] == [fid]
    k = ss.get("/api/safety/overview").json()["kpis"]
    assert k["open"] == 1 and k["total"] == 1 and k["ai_found"] == 1 and k["unassigned"] == 0
    assert ss.get("/api/safety/overview").json()["candidate_count"] == 0
    assert sa_a.get("/api/safety/home").json()["counts"]["pending"] == 1
    assert len(_notes(tech, "新派单")) == 1
    # 已确认的不能再确认/不采纳
    assert sa_a.post(f"/api/findings/{fid}/confirm", json={}).status_code == 400
    assert sa_a.post(f"/api/findings/{fid}/dismiss", json={"reason": "x"}).status_code == 400
    # 原整改流程照常：技术员拍整改照片 → AI 复核
    rect = tech.post("/api/media", data={"purpose": "rectify"}, files=[("files", ("r.jpg", jpeg((20, 210, 20)), "image/jpeg"))]).json()[0]
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": rect["id"]}).status_code == 200


def test_confirm_by_harbour_safety_sup_with_due_and_scope(env):
    tech, fid = _cand(env)
    ss = login("ss")
    assert ss.post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"]["tech1"], "due_date": "1999-01-01"}).status_code == 400  # 期限不能早于今天
    assert ss.post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"]["admin"]}).status_code == 400  # 责任人无效
    due = (date.today() + timedelta(days=5)).isoformat()
    r = ss.post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"]["sa_a"], "due_date": due})
    assert r.status_code == 200 and r.json()["due_date"] == due and r.json()["assignee"] == "安全员甲"
    assert len(_notes(login("sa_a"), "新派单")) == 1


def test_confirm_and_edit_keeps_ai_original(env):
    tech, fid = _cand(env)
    sa_a = login("sa_a")
    r = sa_a.post(f"/api/findings/{fid}/confirm", json={"level": "high", "category": "临边防护", "desc": "基坑边缘无防护栏杆", "measure": "立即设置防护栏杆"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["level"] == "high" and v["title"] == "临边防护：基坑边缘无防护栏杆" and v["body"]["measure"] == "立即设置防护栏杆"
    assert v["due_date"] == date.today().isoformat()  # 高风险当天
    assert v["body"]["ai_original"]["level"] == "medium" and "未佩戴安全帽" in v["body"]["ai_original"]["desc"]
    assert sa_a.post(f"/api/findings/{fid}/confirm", json={}).status_code == 400
    conn = dbmod.connect()
    try:
        a = conn.execute("SELECT action FROM audit WHERE target=?", (f"finding:{fid}",)).fetchall()
    finally:
        conn.close()
    assert [x["action"] for x in a] == ["finding.confirm_edit"]


def test_edit_validation(env):
    tech, fid = _cand(env)
    assert login("ss").post(f"/api/findings/{fid}/confirm", json={"level": "info"}).status_code == 400
    assert _row(fid)["confirmed_at"] is None


# ----------------------------------------------------------------------------- 3 不采纳必须填原因

def test_dismiss_requires_reason_and_stays_out_of_ledger(env):
    tech, fid = _cand(env)
    sa_a, ss = login("sa_a"), login("ss")
    assert sa_a.post(f"/api/findings/{fid}/dismiss", json={"reason": "  "}).status_code == 400
    assert sa_a.post(f"/api/findings/{fid}/dismiss", json={}).status_code == 400
    assert _row(fid)["status"] == "open"
    r = sa_a.post(f"/api/findings/{fid}/dismiss", json={"reason": "是反光的警示服，不是未戴安全帽"})
    assert r.status_code == 200
    row = _row(fid)
    assert row["status"] == "dismissed" and row["feedback_note"].startswith("是反光") and row["feedback_by"] == env["users"]["sa_a"]
    assert row["confirmed_at"] is None  # 不算已确认问题
    v = tech.get(f"/api/findings/{fid}").json()
    assert v["candidate"] is False and v["status_name"] == "不采纳"
    assert ss.get("/api/safety/overview").json()["kpis"]["total"] == 0 and ss.get("/api/safety/overview").json()["candidate_count"] == 0
    assert tech.get("/api/findings?agent=safety&status=&candidates=all").json()[0]["status"] == "dismissed"
    # 同一张照片重新识别不会让它重新变成候选
    conn = dbmod.connect()
    try:
        assert F.stats(conn, env["project_id"])["safety"]["dismissed"] == 1  # 作为 AI 误报反馈保留
        assert F.stats(conn, env["project_id"])["safety"]["candidate"] == 0
    finally:
        conn.close()


def test_old_dismiss_entry_applies_to_candidates(env):
    tech, fid = _cand(env)
    ss = login("ss")
    assert tech.post(f"/api/findings/{fid}/status", json={"status": "dismissed", "note": "误报"}).status_code == 400
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "dismissed", "note": ""}).status_code == 400
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "dismissed", "note": "拍摄者反馈：误报"}).status_code == 200
    assert _row(fid)["confirmed_at"] is None and _row(fid)["status"] == "dismissed"


# ----------------------------------------------------------------------------- 4 权限

def test_only_own_company_safety_or_safety_sup_can_decide(env):
    tech, fid = _cand(env)
    for who in ("tech1", "tech2", "eng", "sa_b"):
        c = login(who)
        assert c.post(f"/api/findings/{fid}/confirm", json={}).status_code == 403, who
        assert c.post(f"/api/findings/{fid}/dismiss", json={"reason": "x"}).status_code == 403, who
    assert _row(fid)["confirmed_at"] is None
    # 乙单位安全员看不到甲单位的候选
    assert login("sa_b").get("/api/findings?candidates=only").json() == []
    assert login("sa_b").get("/api/safety/home").json()["candidate_count"] == 0
    # 拍照的技术员自己看得到，但无权决定
    assert [f["id"] for f in tech.get("/api/findings?candidates=only").json()] == [fid]
    # 领导只读
    _add(env, "boss", "leader", "领导")
    assert login("boss").post(f"/api/findings/{fid}/confirm", json={}).status_code == 403
    # 本单位安全员可以
    assert login("sa_a").post(f"/api/findings/{fid}/confirm", json={}).status_code == 200


def test_sub_safety_can_only_assign_within_company(env):
    tech, fid = _cand(env)
    sa_a = login("sa_a")
    assert sa_a.post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"]["tech2"]}).status_code == 400  # 乙单位技术员
    assert _row(fid)["confirmed_at"] is None
    assert sa_a.post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"]["sa_a"]}).status_code == 200


# ----------------------------------------------------------------------------- 5 整改提交人 ≠ 闭环人

def test_rectifier_cannot_close_own_rectification(env):
    tech, fid = _cand(env)
    ss, pm = login("ss"), login("pm")
    assert ss.post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"]["tech1"]}).status_code == 200
    # 安全主管自己拍了整改照片（模拟 AI 复核未能判定，状态停在待复核）
    rect = ss.post("/api/media", data={"purpose": "rectify"}, files=[("files", ("r.jpg", jpeg((20, 210, 20)), "image/jpeg"))]).json()[0]
    conn = dbmod.connect()
    conn.execute("UPDATE findings SET status='rectifying', rectify_media_id=? WHERE id=?", (rect["id"], fid))
    conn.close()
    r = ss.post(f"/api/findings/{fid}/status", json={"status": "resolved"})
    assert r.status_code == 400 and "整改" in r.json()["detail"]
    assert _row(fid)["status"] == "rectifying"
    # 别的有权人员可以闭环
    assert pm.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 200
    assert _row(fid)["status"] == "resolved"


def test_non_rectifier_safety_sup_can_close_normal_rectification(env):
    tech, fid = _cand(env)
    ss = login("ss")
    ss.post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"]["tech1"]})
    rect = tech.post("/api/media", data={"purpose": "rectify"}, files=[("files", ("r.jpg", jpeg((20, 210, 20)), "image/jpeg"))]).json()[0]
    conn = dbmod.connect()
    conn.execute("UPDATE findings SET status='rectifying', rectify_media_id=? WHERE id=?", (rect["id"], fid))
    conn.close()
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 200


# ----------------------------------------------------------------------------- 6 首页“待确认事项”计入候选

def _cands(client):
    return [i for i in client.get("/api/todo").json()["items"] if i["source"] == "candidate"]


def _queue_cands(env, role):
    with closing(dbmod.connect()) as conn:
        return [i for i in brief.decision_queue(conn, env["project_id"], role=role) if i["type"] == "candidate"]


def test_home_todo_counts_candidates(env):
    """原 /api/ai/feed 的 todo（已下线）：安全主管在 /api/todo 看到候选；服务层 decision_queue 的候选口径不变。"""
    tech, fid = _cand(env)
    login("pm").post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    ss, pm, eng = login("ss"), login("pm"), login("eng")
    assert ss.get("/api/ai/feed").status_code == 404
    c = next(i for i in _cands(ss) if i["finding"]["id"] == fid)
    assert c["id"] == f"candidate:{fid}" and c["finding"]["body"]["category"] == "个人防护" and c["finding"]["ref_type"] == "media"
    q = next(i for i in _queue_cands(env, "safety_sup") if i["id"] == fid)
    assert q["candidate"] is True and q["body"]["category"] == "个人防护" and q["ref_type"] == "media"
    # 工程主管不看安全候选；项目经理退出候选确认（D7），待办里不出候选
    assert not _cands(pm) and not _cands(eng)
    # 服务层口径：除安全主管外只列高风险候选
    assert not _queue_cands(env, "manager") and not _queue_cands(env, "eng_sup")
    conn = dbmod.connect()
    hid = F.create(conn, env["project_id"], "safety", "hazard", "high", "高处作业：临边无防护", {"category": "高处作业", "desc": "临边无防护", "measure": "加护栏"},
                   fingerprint="t:high", assignee_id=env["users"]["tech1"], due_days=0, candidate=True)
    conn.close()
    assert any(i["id"] == hid for i in _queue_cands(env, "manager"))
    assert not _cands(pm)
    # 确认后从“待确认事项”里消失
    assert ss.post(f"/api/findings/{fid}/confirm", json={}).status_code == 200
    assert not [i for i in _cands(ss) if i["finding"]["id"] == fid]
    assert not [i for i in _queue_cands(env, "safety_sup") if i["id"] == fid]


# ----------------------------------------------------------------------------- 7 创建点：人写的不是候选

def test_only_ai_hazards_are_candidates(env):
    conn = dbmod.connect()
    pid = env["project_id"]
    try:
        a = F.create(conn, pid, "safety", "hazard", "low", "AI", {}, fingerprint="a", candidate=True)
        b = F.create(conn, pid, "quality", "reported", "medium", "日志上报", {}, fingerprint="b", confirmed_by=env["users"]["tech1"])
        c = F.create(conn, pid, "quality", "qc_reminder", "info", "质控提醒", {}, fingerprint="c")
        d = F.create(conn, pid, "dq", "log_risk", "medium", "数据审核", {}, fingerprint="d")
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM findings").fetchall()}
    finally:
        conn.close()
    assert rows[a]["confirmed_at"] is None
    assert all(rows[i]["confirmed_at"] for i in (b, c, d)) and rows[b]["confirmed_by"] == env["users"]["tech1"]


def test_qr_report_is_candidate_and_claim_does_not_reassign(env):
    from test_quick import _qr, _submit, anon
    _team(env)
    _add(env, "sa_a2", "sub_safety", "安全员甲二", "甲")
    sa, sa2 = login("sa_a"), login("sa_a2")
    _submit(anon(), _qr(sa))
    sub = sa.get("/api/quick/submissions").json()[0]
    f = sub["findings"][0]
    assert f["candidate"] is True and f["status_name"] == "候选·待确认" and f["category"] == "个人防护"
    assert sa2.post(f"/api/quick/submissions/{sub['id']}/claim").status_code == 200
    assert _row(f["id"])["assignee_id"] == env["users"]["sa_a"]  # 候选认领不改派
    assert sa.get("/api/findings?agent=safety&status=active").json() == []
    # 认领后由本单位安全员在原处确认，再按原规则进入台账
    assert sa2.post(f"/api/findings/{f['id']}/confirm", json={"assignee_id": env["users"]["sa_a2"]}).status_code == 200
    assert [x["id"] for x in sa2.get("/api/findings?agent=safety&status=active").json()] == [f["id"]]


def test_candidate_excluded_from_evidence_and_commands(env):
    tech, fid = _cand(env)
    conn = dbmod.connect()
    try:
        from app.services import cmd as cmd_svc
        assert cmd_svc.search_findings(conn, env["project_id"], "安全帽") == []
        assert cmd_svc.search_findings(conn, env["project_id"], f"S{fid}") == []
    finally:
        conn.close()
    # 指令派单直接指向候选也被拒绝（assign_finding 保护）
    ss = login("ss")
    r = ss.post("/api/cmd/execute", json={"type": "assign", "finding_id": fid, "assignee_id": env["users"]["tech1"]})
    assert r.status_code in (400, 403)
    assert _row(fid)["assignee_id"] == env["users"]["tech1"] and not _notes(tech, "新派单")


# ----------------------------------------------------------------------------- 8 迁移：历史隐患视为已确认

def test_migration_v7_backfills_history_and_is_idempotent():
    conn = sqlite3.connect(os.path.join(tempfile.mkdtemp(prefix="zb_mig7_"), "old.db"), isolation_level=None)
    conn.row_factory = dbmod._dict_factory
    conn.executescript("""CREATE TABLE findings (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL, level TEXT NOT NULL,
            title TEXT NOT NULL, body_json TEXT NOT NULL DEFAULT '{}', ref_type TEXT, ref_id INTEGER, activity_code TEXT, fingerprint TEXT,
            status TEXT NOT NULL DEFAULT 'open', assignee_id INTEGER, due_date TEXT, ai_run_id INTEGER, rectify_media_id INTEGER, verify_json TEXT,
            feedback_by INTEGER, feedback_note TEXT NOT NULL DEFAULT '', feedback_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT);
        INSERT INTO findings (project_id, agent, kind, level, title, status, created_at, updated_at) VALUES
            (1, 'safety', 'hazard', 'high', 'a', 'open', '2026-09-01T08:00:00+08:00', 'x'),
            (1, 'safety', 'hazard', 'low', 'b', 'resolved', '2026-09-02T08:00:00+08:00', 'x'),
            (1, 'safety', 'hazard', 'low', 'c', 'dismissed', '2026-09-03T08:00:00+08:00', 'x');
        PRAGMA user_version=6;""")
    dbmod._migrate(conn)
    rows = conn.execute("SELECT title, status, created_at, confirmed_at, confirmed_by FROM findings ORDER BY id").fetchall()
    assert [r["confirmed_at"] for r in rows] == [r["created_at"] for r in rows] and all(r["confirmed_by"] is None for r in rows)
    # 台账口径下的数量与迁移前一致（含已闭环、已忽略）
    assert conn.execute("SELECT COUNT(*) n FROM findings WHERE confirmed_at IS NOT NULL").fetchone()["n"] == 3
    # 重复迁移：不报错，且不会把迁移后新增的候选误当成已确认
    conn.execute("INSERT INTO findings (project_id, agent, kind, level, title, created_at, updated_at) VALUES (1, 'safety', 'hazard', 'low', 'new', 'n', 'n')")
    conn.execute("PRAGMA user_version=6")
    dbmod._migrate(conn)
    assert conn.execute("SELECT confirmed_at FROM findings WHERE title='new'").fetchone()["confirmed_at"] is None
    assert conn.execute("SELECT COUNT(*) n FROM findings").fetchone()["n"] == 4
    conn.close()


def test_new_db_has_confirm_columns(env):
    conn = dbmod.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(findings)").fetchall()}
    finally:
        conn.close()
    assert {"confirmed_by", "confirmed_at"} <= cols and dbmod.SCHEMA_VERSION >= 7
