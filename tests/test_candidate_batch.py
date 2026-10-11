"""T-05 候选收敛（P0-5，AC-5.1～5.8）：按照片批量处理候选、"当场已纠正"一键闭环（D6）、不采纳选原因、
同类合并提示、日志安全问题去重（D11）、默认责任人（F-16）、待办以外只读（N-10⑩）。"""
import re
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from conftest import jpeg, login
from test_roles import _add, _team

from app import db as dbmod
from app.services import ai_trace, site_agents
from app.services import findings as F

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _conn():
    return closing(dbmod.connect())


def _media(env, who="tech1", place=""):
    """直接造一张照片记录（不触发识别），返回 media id。"""
    with _conn() as conn:
        return dbmod.insert(conn, "media", {"project_id": env["project_id"], "uploader_id": env["users"][who], "sha256": f"x{who}{place}{dbmod.now_iso()}",
                                            "file": "x.jpg", "ai_file": "x.jpg", "thumb": "x.jpg", "purpose": "safety",
                                            "geo_place": place, "uploaded_at": dbmod.now_iso()})


def _cands(env, mid, specs, assignee="tech1"):
    """在同一张照片上造若干候选：specs = [(level, category, desc)]。"""
    out = []
    with _conn() as conn:
        for k, (lvl, cat, desc) in enumerate(specs):
            out.append(F.create(conn, env["project_id"], "safety", "hazard", lvl, f"{cat}：{desc}", {"category": cat, "desc": desc, "measure": "整改"},
                                ref_type="media", ref_id=mid, fingerprint=f"hz:{mid}:{k}", assignee_id=env["users"][assignee] if assignee else None,
                                due_days=2, candidate=True))
    return out


def _row(fid):
    with _conn() as conn:
        return conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()


def _audits(fid):
    with _conn() as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit WHERE target=? ORDER BY id", (f"finding:{fid}",)).fetchall()]


def _batch(c, items):
    return c.post("/api/candidates/batch", json={"items": items})


THREE = [("high", "临边洞口", "3#墩临边无防护"), ("medium", "个人防护", "未戴安全帽"), ("low", "文明施工", "材料堆放杂乱")]


# ----------------------------------------------------------------------------- AC-5.1 一张照片的全部候选，点 1 次处理完

def test_one_click_confirms_whole_photo_with_defaults(env):
    _team(env)
    mid = _media(env)
    ids = _cands(env, mid, THREE)
    sa = login("sa_a")
    r = _batch(sa, [{"id": i, "action": "confirm"} for i in ids])
    assert r.status_code == 200, r.text
    assert r.json()["decided"] == 3
    today = date.today()
    due = {"high": today, "medium": today + timedelta(days=2), "low": today + timedelta(days=6)}
    for i in ids:
        row = _row(i)
        assert row["confirmed_at"] and row["confirmed_by"] == env["users"]["sa_a"] and row["status"] == "open"
        assert row["assignee_id"] == env["users"]["tech1"]                       # 默认责任人（拍照技术员）
        assert row["due_date"] == due[row["level"]].isoformat()                  # 默认期限：按风险等级、从确认当天算
        assert _audits(i) == ["finding.confirm"]                                 # 每条决定单独写审计
    # 一次批量派单，同一责任人只收到一条消息
    notes = [n for n in login("tech1").get("/api/notifications").json() if "派单" in n["title"]]
    assert len(notes) == 1 and "3 项" in notes[0]["title"]


def test_batch_overrides_assignee_and_due_in_place(env):
    _team(env)
    ids = _cands(env, _media(env), THREE[:2])
    due = (date.today() + timedelta(days=4)).isoformat()
    r = _batch(login("ss"), [{"id": i, "action": "confirm", "assignee_id": env["users"]["sa_a"], "due_date": due} for i in ids])
    assert r.status_code == 200, r.text
    assert {(_row(i)["assignee_id"], _row(i)["due_date"]) for i in ids} == {(env["users"]["sa_a"], due)}


def test_batch_confirm_with_edits(env):
    _team(env)
    [fid] = _cands(env, _media(env), THREE[1:2])
    r = _batch(login("ss"), [{"id": fid, "action": "confirm", "level": "high", "desc": "多人未戴安全帽"}])
    assert r.status_code == 200, r.text
    row = _row(fid)
    assert row["level"] == "high" and row["title"] == "个人防护：多人未戴安全帽" and _audits(fid) == ["finding.confirm_edit"]


# ----------------------------------------------------------------------------- AC-5.2 当场已纠正（D6）

def test_onspot_closes_into_ledger_and_counts_as_adopted(env):
    _team(env)
    ids = _cands(env, _media(env), THREE[:2])
    sa = login("sa_a")
    r = _batch(sa, [{"id": i, "action": "onspot"} for i in ids])
    assert r.status_code == 200, r.text
    for i in ids:
        row = _row(i)
        assert row["status"] == "resolved" and row["confirmed_at"] and row["resolved_at"] and row["confirmed_by"] == env["users"]["sa_a"]
        assert row["rectify_media_id"] is None and row["feedback_note"] == F.ONSPOT_NOTE == "当场纠正、无整改照片"
        assert dbmod.loads(row["body_json"])["onspot"] is True
        assert _audits(i) == ["finding.resolved"]
    v = sa.get(f"/api/findings/{ids[0]}").json()
    assert v["candidate"] is False and v["onspot"] is True and "当场纠正" in v["status_name"]
    # 台账口径：计入已确认、已闭环；不通知整改人
    with _conn() as conn:
        st = F.stats(conn, env["project_id"])["safety"]
        assert (st["total"], st["resolved"], st["candidate"], st["onspot"]) == (2, 2, 0, 2)
        fb = ai_trace.feedback_summary(conn, env["project_id"])["safety"]
        assert (fb["adopted"], fb["rejected"]) == (2, 0)                         # D6：计为 AI 采纳（口径变化）
    assert not [n for n in login("tech1").get("/api/notifications").json() if "派单" in n["title"]]
    # 来历里能看到“当场纠正、无整改照片”
    t = login("ss").get(f"/api/ai/trace?type=finding&id={ids[0]}").json()
    assert t["human"]["note"] == "当场纠正、无整改照片" and "确认闭环" in [s["what"] for s in t["steps"]]


# ----------------------------------------------------------------------------- AC-5.3 不采纳选原因

def test_dismiss_by_reason_code_with_optional_note(env):
    _team(env)
    a, b, c = _cands(env, _media(env), THREE)
    ss = login("ss")
    assert _batch(ss, [{"id": a, "action": "dismiss"}]).status_code == 400                       # 必须选原因
    assert _batch(ss, [{"id": a, "action": "dismiss", "reason_code": "bogus"}]).status_code == 400
    assert _batch(ss, [{"id": a, "action": "dismiss", "reason_code": "other"}]).status_code == 400  # 选“其他”须补充说明
    assert _row(a)["status"] == "open"
    r = _batch(ss, [{"id": a, "action": "dismiss", "reason_code": "misread"},
                    {"id": b, "action": "dismiss", "reason_code": "not_hazard", "reason_note": "是警示服反光"},
                    {"id": c, "action": "dismiss", "reason_code": "other", "reason_note": "照片是别的项目的"}])
    assert r.status_code == 200, r.text
    assert _row(a)["feedback_note"] == F.DISMISS_REASONS["misread"]
    assert _row(b)["feedback_note"] == F.DISMISS_REASONS["not_hazard"] + "：是警示服反光"
    assert _row(c)["feedback_note"].endswith("照片是别的项目的")
    assert all(_row(i)["status"] == "dismissed" and _row(i)["confirmed_at"] is None for i in (a, b, c))
    assert all(_audits(i) == ["finding.dismissed"] for i in (a, b, c))
    assert login("ss").get("/api/candidates/reasons").json()[0]["code"] in F.DISMISS_REASONS


def test_mixed_actions_in_one_batch(env):
    _team(env)
    a, b, c = _cands(env, _media(env), THREE)
    r = _batch(login("sa_a"), [{"id": a, "action": "confirm"}, {"id": b, "action": "onspot"}, {"id": c, "action": "dismiss", "reason_code": "misread"}])
    assert r.status_code == 200, r.text
    assert [_row(i)["status"] for i in (a, b, c)] == ["open", "resolved", "dismissed"]


# ----------------------------------------------------------------------------- AC-5.6 整批校验、不部分生效

def test_batch_rejects_whole_batch_with_bad_ids(env):
    _team(env)
    a, b = _cands(env, _media(env), THREE[:2])
    sa = login("sa_a")
    assert _batch(sa, [{"id": a, "action": "confirm"}]).status_code == 200
    r = _batch(sa, [{"id": b, "action": "confirm"}, {"id": a, "action": "confirm"}, {"id": 99999, "action": "confirm"}])
    assert r.status_code == 400
    assert sorted(x["id"] for x in r.json()["bad"]) == [a, 99999]                 # 列出有问题的条目
    assert _row(b)["confirmed_at"] is None and _audits(b) == []                   # 不部分生效
    assert _batch(sa, [{"id": b, "action": "confirm"}, {"id": b, "action": "onspot"}]).status_code == 400   # 同一条重复
    assert _batch(sa, [{"id": b, "action": "approve"}]).status_code == 400
    assert _batch(sa, []).status_code in (400, 422)
    assert _row(b)["confirmed_at"] is None


def test_batch_cross_unit_is_403_and_nothing_applies(env):
    _team(env)
    a = _cands(env, _media(env), THREE[:1])[0]                                   # 甲单位
    b = _cands(env, _media(env, "tech2"), THREE[1:2], assignee="tech2")[0]       # 乙单位
    r = _batch(login("sa_a"), [{"id": a, "action": "onspot"}, {"id": b, "action": "onspot"}])
    assert r.status_code == 403 and [x["id"] for x in r.json()["bad"]] == [b]
    assert _row(a)["status"] == "open" and _row(a)["confirmed_at"] is None
    # 技术员、工程主管、领导无权
    _add(env, "boss", "leader", "领导")
    for who in ("tech1", "eng", "boss"):
        assert _batch(login(who), [{"id": a, "action": "confirm"}]).status_code == 403, who
    assert _row(a)["confirmed_at"] is None


def test_confirmed_or_non_candidate_finding_rejected(env):
    _team(env)
    with _conn() as conn:
        q = F.create(conn, env["project_id"], "quality", "reported", "medium", "日志上报", {}, assignee_id=env["users"]["tech1"])
    r = _batch(login("ss"), [{"id": q, "action": "onspot"}])
    assert r.status_code == 400 and r.json()["bad"][0]["id"] == q
    assert _row(q)["status"] == "open"


# ----------------------------------------------------------------------------- AC-5.7 默认责任人（F-16）

def test_default_assignee_rules(env):
    _team(env)
    with _conn() as conn:
        pid = env["project_id"]
        f_tech = _row(_cands(env, _media(env), THREE[:1])[0])
        assert F.default_assignee(conn, f_tech) == env["users"]["tech1"]           # 技术员拍的：就是他本人
        f_sa = _row(_cands(env, _media(env, "sa_a"), THREE[1:2], assignee="sa_a")[0])
        assert F.default_assignee(conn, f_sa) == env["users"]["tech1"]             # 分包安全员上报：同单位唯一技术员，不是本人
        conn.execute("INSERT INTO users (username, name, role, password_hash, project_id, wbs_scope, email, active, created_at, company) "
                     "VALUES ('tech1b', '技术员甲二', 'tech', 'x', ?, '', '', 1, ?, '甲')", (pid, dbmod.now_iso()))
        assert F.default_assignee(conn, f_sa) is None                               # 同单位有两位技术员：找不到合适人选，留空由人选
        assert F.default_assignee(conn, {**f_sa, "assignee_id": None}) is None
    # 默认为空时，批量确认必须由人选责任人；不会默认派给上报人本人
    sa = login("sa_a")
    r = _batch(sa, [{"id": f_sa["id"], "action": "confirm"}])
    assert r.status_code == 400 and "责任人" in r.json()["detail"]
    assert _batch(sa, [{"id": f_sa["id"], "action": "confirm", "assignee_id": env["users"]["tech1"]}]).status_code == 200
    # 当场已纠正不需要责任人
    f2 = _cands(env, _media(env, "sa_a"), THREE[2:], assignee="sa_a")[0]
    assert _batch(sa, [{"id": f2, "action": "onspot"}]).status_code == 200


def test_hints_give_default_assignee_and_scope(env):
    _team(env)
    a = _cands(env, _media(env), THREE[:1])[0]
    b = _cands(env, _media(env, "tech2"), THREE[1:2], assignee="tech2")[0]
    h = login("sa_a").get(f"/api/candidates/hints?ids={a},{b}").json()["items"]
    assert h[str(a)]["default_assignee_id"] == env["users"]["tech1"] and str(b) not in h    # 乙单位的候选不给甲单位
    assert login("tech1").get(f"/api/candidates/hints?ids={a}").status_code == 403


# ----------------------------------------------------------------------------- AC-5.4 同类合并提示（C 级）

def _confirmed(env, title, cat, days_ago=0, status="open"):
    with _conn() as conn:
        fid = F.create(conn, env["project_id"], "safety", "hazard", "medium", title, {"category": cat, "desc": title}, assignee_id=env["users"]["tech1"],
                       confirmed_by=env["users"]["ss"])
        t = (date.today() - timedelta(days=days_ago)).isoformat() + "T08:00:00+08:00"
        conn.execute("UPDATE findings SET created_at=?, status=? WHERE id=?", (t, status, fid))
    return fid


def test_similar_hint_same_part_same_category_within_14_days(env):
    _team(env)
    old = _confirmed(env, "3#墩临边无防护栏杆", "临边洞口", days_ago=3)
    _confirmed(env, "5#墩临边无防护", "临边洞口", days_ago=1)                    # 不同部位
    _confirmed(env, "3#墩未戴安全帽", "个人防护", days_ago=1)                    # 不同类别
    a, b = _cands(env, _media(env), [("high", "临边洞口", "3#墩临边无防护"), ("medium", "个人防护", "8#墩未戴安全帽")])
    ss = login("ss")
    h = ss.get(f"/api/candidates/hints?ids={a},{b}").json()["items"]
    assert h[str(a)]["similar"]["id"] == old and h[str(b)]["similar"] is None
    # 超过 14 天、或已闭环的不提示
    with _conn() as conn:
        conn.execute("UPDATE findings SET created_at=? WHERE id=?", ((date.today() - timedelta(days=15)).isoformat() + "T08:00:00+08:00", old))
    assert ss.get(f"/api/candidates/hints?ids={a}").json()["items"][str(a)]["similar"] is None
    with _conn() as conn:
        conn.execute("UPDATE findings SET created_at=?, status='resolved' WHERE id=?", (dbmod.now_iso(), old))
    assert ss.get(f"/api/candidates/hints?ids={a}").json()["items"][str(a)]["similar"] is None


def test_merge_into_existing_records_on_target(env):
    _team(env)
    old = _confirmed(env, "3#墩临边无防护栏杆", "临边洞口", days_ago=2)
    a = _cands(env, _media(env), [("high", "临边洞口", "3#墩临边无防护")])[0]
    r = _batch(login("ss"), [{"id": a, "action": "dismiss", "reason_code": "duplicate", "merge_into": old}])
    assert r.status_code == 200, r.text
    assert _row(a)["status"] == "dismissed" and f"#{old}" in _row(a)["feedback_note"]
    merged = dbmod.loads(_row(old)["body_json"])["merged"]
    assert merged[0]["finding_id"] == a
    # 并入的目标必须是进行中的已确认隐患
    b = _cands(env, _media(env), [("high", "临边洞口", "3#墩临边无防护2")])[0]
    assert _batch(login("ss"), [{"id": b, "action": "dismiss", "reason_code": "duplicate", "merge_into": a}]).status_code == 400


# ----------------------------------------------------------------------------- AC-5.5 日志里的安全问题去重（D11，C 级）

def _day(n):
    return (date.today() - timedelta(days=n)).isoformat()


def _log(env, day, issues, author="tech1"):
    with _conn() as conn:
        lid = dbmod.insert(conn, "logs", {"project_id": env["project_id"], "author_id": env["users"][author], "log_date": day, "mode": "ai",
                                          "status": "submitted", "content_json": dbmod.dumps({"issues": issues}), "started_at": dbmod.now_iso()})
        return conn.execute("SELECT * FROM logs WHERE id=?", (lid,)).fetchone()


def _safety_rows(env):
    with _conn() as conn:
        return conn.execute("SELECT * FROM findings WHERE project_id=? AND agent='safety' ORDER BY id", (env["project_id"],)).fetchall()


def test_log_safety_issue_merges_into_open_item_within_14_days(env):
    _team(env)
    mud = {"type": "安全", "desc": "现场便道泥泞，车辆打滑", "severity": "低", "measure": "铺碎石"}
    with _conn() as conn:
        assert site_agents.quality_from_log(conn, _log(env, _day(2), [mud])) == 1
        lg2 = _log(env, _day(1), [{**mud, "desc": "便道泥泞积水"}, {"type": "质量", "desc": "钢筋保护层不足", "severity": "中"}])
        assert site_agents.quality_from_log(conn, lg2) == 1                     # 只新建了质量问题，安全问题并入
        assert site_agents.quality_from_log(conn, lg2) == 0                     # 重复提交不重复并入
    rows = _safety_rows(env)
    assert len(rows) == 1 and rows[0]["confirmed_at"]                           # 不新建，不生成候选
    body = dbmod.loads(rows[0]["body_json"])
    assert body["category"] == "文明施工" and [m["log_id"] for m in body["merged"]] == [lg2["id"]]
    # 已开条目超过 14 天：另开新条
    with _conn() as conn:
        conn.execute("UPDATE findings SET created_at=? WHERE id=?", ((date.today() - timedelta(days=15)).isoformat() + "T08:00:00+08:00", rows[0]["id"]))
        assert site_agents.quality_from_log(conn, _log(env, _day(0), [mud])) == 1
    assert len(_safety_rows(env)) == 2


def test_log_safety_issue_different_part_or_category_not_merged(env):
    _team(env)
    with _conn() as conn:
        site_agents.quality_from_log(conn, _log(env, _day(2), [{"type": "安全", "desc": "3#墩临边无防护", "severity": "高"}]))
        site_agents.quality_from_log(conn, _log(env, _day(1), [{"type": "安全", "desc": "5#墩临边无防护", "severity": "高"},
                                                                {"type": "安全", "desc": "3#墩配电箱未上锁", "severity": "中"}]))
        site_agents.quality_from_log(conn, _log(env, _day(0), [{"type": "安全", "desc": "3#墩临边护栏缺失"}]))
    titles = [r["title"] for r in _safety_rows(env)]
    assert titles == ["3#墩临边无防护", "5#墩临边无防护", "3#墩配电箱未上锁"]


# ----------------------------------------------------------------------------- 前端：组件契约、F-17、AC-5.3、AC-5.8（静态检查）

def _js():
    return (STATIC / "aifeed.js").read_text(encoding="utf-8")


def _block(js, name):
    m = re.search(r"const " + name + r" = \{(.*?)\n\};", js, re.S)
    assert m, name
    return m.group(1)


def test_candidate_group_contract_and_single_button_names():
    js = _js()
    assert "window.CandidateGroup = CandidateGroup" in js
    g = _block(js, "CandidateGroup")
    for prop in ("groupKey", "items", "defaultAssigneeId", "defaultDue"):
        assert prop in g, prop
    assert '"done"' in g and '"changed"' in g and "/api/candidates/batch" in g and "/api/candidates/hints" in g
    assert "prompt(" not in js                                                     # AC-5.3：不弹 prompt
    # F-17：每个按钮文字只出现一次（不再有两个同名“确认并派单”）
    labels = re.findall(r"<button[^>]*>([^<{]+)", g)
    labels = [x.strip() for x in labels if x.strip()]
    assert len(labels) == len(set(labels)), labels
    assert "当场已纠正" in g and "可能是同一问题" in g


def test_candidate_box_is_readonly_outside_detail_page():
    box = _block(_js(), "CandidateBox")
    assert "去处理" in box and "/api/findings/${this.f.id}/confirm" not in box and "prompt(" not in box
    assert "candidate-group" in box                                                # 详情页（单条深链）也走同一个决策组件


def test_no_new_css_classes():
    css = "".join(p.read_text(encoding="utf-8") for p in STATIC.glob("*.css"))
    js = _js()
    used = set()
    for name in ("CandidateGroup", "CandidateBox"):
        for cls in re.findall(r'(?<![:\w-])class="([^"]+)"', _block(js, name)):
            used.update(cls.split())
    missing = [c for c in used if not re.search(r"\." + re.escape(c) + r"(?![\w-])", css)]
    assert missing == [], missing
