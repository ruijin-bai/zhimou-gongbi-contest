import threading
from datetime import datetime, timedelta

import pytest
from conftest import login
from test_roles import _add, _team

from app import db as dbmod
from app.ai.client import AIError
from app.services import findings as find_svc
from app.services import nudge as svc

T0 = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)


def _finding(env, owner, agent="safety", days=-3, title="临边防护缺失", kind="hazard", level="high"):
    conn = dbmod.connect()
    # 默认项目为每日提交模式，会把“缺报”也算进来；这里只看隐患/质控，先关掉日志来源
    conn.execute("UPDATE projects SET config_json='{\"cadence\":\"day\",\"nudge\":{\"sources\":{\"logs\":false}}}'")
    fid = find_svc.create(conn, env["project_id"], agent, kind, level, title, {}, fingerprint=f"t:{title}:{owner}",
                          assignee_id=env["users"][owner] if owner else None, due_days=days)
    conn.close()
    return fid


def _run(env, now, dry=False):
    conn = dbmod.connect()
    try:
        return svc.run_project(conn, env["project_id"], now=now, dry=dry)
    finally:
        conn.close()


def _notes(env, username=None, like="%"):
    conn = dbmod.connect()
    try:
        if username:
            return conn.execute("SELECT * FROM notifications WHERE user_id=? AND title LIKE ?", (env["users"][username], like)).fetchall()
        return conn.execute("SELECT * FROM notifications WHERE title LIKE ?", (like,)).fetchall()
    finally:
        conn.close()


def _to(rep):
    return sorted(s["to"]["name"] for s in rep["sent"])


def test_escalation_chain_is_idempotent_and_never_hits_leader_or_admin(env):
    _team(env)
    _add(env, "boss", "leader", "领导")
    _finding(env, "tech1")
    # 阶段1：提醒责任人本人；同一时刻重跑不重复
    r1 = _run(env, T0)
    assert _to(r1) == ["张工"] and r1["sent"][0]["stage"] == 1 and r1["sent"][0]["via"] == "ai"
    assert "已超期3天" in _notes(env, "tech1", "催办提醒%")[0]["body"]
    assert _run(env, T0)["sent"] == [] and _run(env, T0)["reasons"].get("waiting") == 1
    # 未满 24 小时不升级；满了提醒甲单位安全员（不是乙单位）
    assert _run(env, T0 + timedelta(hours=23))["sent"] == []
    r2 = _run(env, T0 + timedelta(hours=25))
    assert _to(r2) == ["安全员甲"] and r2["sent"][0]["stage"] == 2
    assert _notes(env, "sa_b") == []
    # 阶段3：安全隐患 → 安全主管（不是工程主管）；阶段4：高风险隐患升级到项目经理，并进入其首页待办（T-06 “升级到项目经理的高风险问题”）
    r3 = _run(env, T0 + timedelta(hours=50))
    assert _to(r3) == ["安全主管"] and r3["sent"][0]["stage"] == 3
    r4 = _run(env, T0 + timedelta(hours=75))
    assert _to(r4) == ["项目经理"] and r4["sent"][0]["title"].startswith("升级到经理") and r4["sent"][0]["stage_name"] == "升级到项目经理"
    body = _notes(env, "pm", "升级到经理%")[0]["body"]
    assert "已列入您的首页待办" in body and "待我决策" not in body and "待决策" not in body
    assert _notes(env, "eng") == []
    # 走完升级链后不再发
    r5 = _run(env, T0 + timedelta(hours=100))
    assert r5["sent"] == [] and r5["reasons"].get("chain_done") == 1
    # 领导、管理员从未收到任何催办
    assert _notes(env, "boss") == [] and _notes(env, "admin") == []
    # 全程留痕：升级日志、审计、事件、催办Agent 调用
    conn = dbmod.connect()
    assert conn.execute("SELECT COUNT(*) n FROM nudge_log WHERE status='sent'").fetchone()["n"] == 4
    assert conn.execute("SELECT COUNT(*) n FROM audit WHERE action='nudge.auto'").fetchone()["n"] == 4
    assert conn.execute("SELECT COUNT(*) n FROM events WHERE kind='nudge.sent'").fetchone()["n"] == 4
    assert conn.execute("SELECT COUNT(*) n FROM ai_runs WHERE kind='nudge.plan'").fetchone()["n"] == 4
    assert conn.execute("SELECT channels_json FROM notifications WHERE title LIKE '催办提醒%'").fetchone()["channels_json"] == '{"inapp":"sent"}'
    conn.close()


def test_resolved_stops_and_dry_run_writes_nothing(env):
    _team(env)
    fid = _finding(env, "tech1")
    pre = _run(env, T0, dry=True)
    assert len(pre["sent"]) == 1 and pre["dry"] and _notes(env) == []
    _run(env, T0)
    conn = dbmod.connect()
    conn.execute("UPDATE findings SET status='resolved' WHERE id=?", (fid,))
    conn.close()
    later = _run(env, T0 + timedelta(hours=30))
    assert later["sent"] == [] and later["targets"] == 0
    # 临期（期限前一天）也会提醒，更远的不会
    _finding(env, "tech1", days=1, title="明天到期")
    _finding(env, "tech1", days=3, title="三天后到期")
    r = _run(env, T0 + timedelta(hours=31))
    items = [i["subject"] for s in r["sent"] for i in s["items"]]
    assert "明天到期" in items and "三天后到期" not in items


def test_quiet_hours_and_daily_cap_defer_not_drop(env):
    _team(env)
    _finding(env, "tech1")
    night = T0.replace(hour=22)
    r = _run(env, night)
    assert r["sent"] == [] and r["quiet"] and r["reasons"]["quiet"] == 1 and r["deferred"][0]["to"]["name"] == "张工"
    # 次日早晨（静默结束后）补发
    assert _to(_run(env, T0.replace(hour=8) + timedelta(days=1))) == ["张工"]

    # 每日上限：上限 1 条时，当天第二条（新事项）延后
    login("admin").put("/api/nudge/rules", json={"daily_cap": 1})
    _finding(env, "tech2", title="乙的事项")
    _finding(env, "tech2", title="乙的第二件", agent="quality", kind="reported", level="medium")
    r = _run(env, T0.replace(hour=11) + timedelta(days=1))
    assert _to(r) == ["李工"] and len(r["sent"][0]["items"]) == 2  # 同一人同一阶段合并为一条
    _finding(env, "tech2", title="乙的第三件")
    r = _run(env, T0.replace(hour=12) + timedelta(days=1))
    assert r["sent"] == [] and r["reasons"]["cap"] == 1


def test_missing_stages_collapse_and_no_owner_reported(env):
    # 没有单位、没有工程主管：阶段2、3 无人可接，自动顺延到项目经理，不卡死
    _finding(env, "tech1", agent="quality", kind="reported", level="medium", title="质量问题")
    _finding(env, None, title="没人负责")
    r1 = _run(env, T0)
    assert _to(r1) == ["张工"] and r1["reasons"]["no_owner"] == 1
    assert any("先派单" in s["reason"] for s in r1["skipped"])
    r2 = _run(env, T0 + timedelta(hours=25))
    assert _to(r2) == ["项目经理"] and r2["sent"][0]["stage"] == 4
    # 中风险质量问题不进项目经理首页待办（T-06 只列高风险），文案不能说“已列入”
    body = _notes(env, "pm")[0]["body"]
    assert "质量问题" in body and "首页待办" not in body and "待决策" not in body
    conn = dbmod.connect()
    assert conn.execute("SELECT COUNT(*) n FROM nudge_log WHERE status='skipped'").fetchone()["n"] == 2
    conn.close()


# ---------------------------------------------------------------- D9：催办链调整（AC-4.6）

def test_medium_and_low_hazards_stop_at_safety_sup(env):
    _team(env)
    _finding(env, "tech1", title="场地泥泞积水", level="medium")
    _finding(env, "tech2", title="材料堆放不整齐", level="low")
    _run(env, T0)
    _run(env, T0 + timedelta(hours=25))
    r3 = _run(env, T0 + timedelta(hours=50))
    assert _to(r3) == ["安全主管"] and r3["sent"][0]["stage"] == 3 and len(r3["sent"][0]["items"]) == 2
    r4 = _run(env, T0 + timedelta(hours=75))
    assert r4["sent"] == [] and r4["reasons"].get("chain_done") == 2
    assert _run(env, T0 + timedelta(hours=200))["sent"] == []
    assert _notes(env, "pm") == []
    conn = dbmod.connect()   # 不写“阶段4 跳过”记录：风险等级日后调高为高风险时仍可升级到项目经理
    assert conn.execute("SELECT COUNT(*) n FROM nudge_log WHERE stage=4").fetchone()["n"] == 0
    conn.execute("UPDATE findings SET level='high' WHERE title='场地泥泞积水'")
    conn.close()
    r5 = _run(env, T0 + timedelta(hours=201))
    assert _to(r5) == ["项目经理"] and [i["subject"] for i in r5["sent"][0]["items"]] == ["场地泥泞积水"]


def test_progress_nudges_skip_sub_safety():
    users = {1: {"id": 1, "role": "tech", "company": "甲", "name": "张工"}, 4: {"id": 4, "role": "sub_safety", "company": "甲", "name": "安全员"},
             5: {"id": 5, "role": "safety_sup", "company": "", "name": "安全主管"}, 3: {"id": 3, "role": "manager", "company": "", "name": "项目经理"}}
    for area in ("log", "gap"):   # 日志/周确认缺报、数据缺口（补报进度）都是进度类
        r, why = svc._recipients(2, {"area": area, "owner_id": 1}, users)
        assert r == [] and "安全员" in why, area
    # 安全隐患照旧经过单位安全员；中低风险到安全主管为止，高风险才到项目经理
    assert [u["id"] for u in svc._recipients(2, {"area": "safety", "owner_id": 1, "level": "high"}, users)[0]] == [4]
    for lv in ("medium", "low"):
        r, why = svc._recipients(4, {"area": "safety", "owner_id": 1, "level": lv}, users)
        assert r == [] and "安全主管" in why, lv
    assert [u["id"] for u in svc._recipients(4, {"area": "safety", "owner_id": 1, "level": "high"}, users)[0]] == [3]


def test_weekly_confirm_chain_skips_sub_safety(env):
    _team(env)
    conn = dbmod.connect()
    conn.execute("UPDATE projects SET config_json='{\"cadence\":\"week\"}'")
    conn.close()
    wed = T0 + timedelta(days=(2 - T0.weekday()) % 7 + 7)  # 周三：上周的周确认已过周一期限
    _run(env, wed)
    _run(env, wed + timedelta(hours=25))
    assert _notes(env, "sa_a") == [] and _notes(env, "sa_b") == []
    assert {n["user_id"] for n in _notes(env, "eng")} == {env["users"]["eng"]}


# ---------------------------------------------------------------- 文案：统一称呼（S-5⑧）

def test_salutation_is_system_template(env, monkeypatch):
    _team(env)
    _finding(env, "tech1")
    _finding(env, "tech2", title="乙的事项")
    real = svc.call_json
    intros = iter(["周明同志，您好！以下事项请抽空处理。", "您好，以下事项请抽空处理。"])
    monkeypatch.setattr(svc, "call_json", lambda *a, **k: real(*a, **{**k, "mock": lambda: {"intro": next(intros)}}))
    r = _run(env, T0)
    assert all(s["via"] == "ai" for s in r["sent"])
    for who, name in (("tech1", "张工"), ("tech2", "李工")):
        body = _notes(env, who)[0]["body"]
        assert body.startswith(f"{name}，您好：以下事项请抽空处理。"), body
        assert "同志" not in body and body.count("您好") == 1
    # 模板路径同一称呼
    monkeypatch.setattr(svc, "call_json", lambda *a, **k: (_ for _ in ()).throw(AIError("超时")))
    _finding(env, "tech1", title="又一件", days=-1)
    r = _run(env, T0 + timedelta(days=1))
    body = [n for n in _notes(env, "tech1") if "又一件" in n["body"]][0]["body"]
    assert body.startswith("张工，您好：") and body.count("您好") == 1


def test_leader_or_admin_owner_is_never_nudged(env):
    _add(env, "boss", "leader", "领导")
    _finding(env, "boss")
    _finding(env, "admin", title="管理员的")
    r = _run(env, T0)
    assert r["sent"] == [] and r["reasons"]["no_recipient"] == 2
    assert _notes(env, "boss") == [] and _notes(env, "admin") == []


def test_wording_guard_falls_back_to_template(env, monkeypatch):
    _team(env)
    _finding(env, "tech1")
    real = svc.call_json
    monkeypatch.setattr(svc, "call_json", lambda *a, **k: real(*a, **{**k, "mock": lambda: {"intro": "已超期99天，再不处理将处罚"}}))
    r = _run(env, T0)
    assert r["sent"][0]["via"] == "template"
    assert "99" not in _notes(env, "tech1")[0]["body"] and "已超期3天" in _notes(env, "tech1")[0]["body"]


def test_daily_log_and_weekly_confirm_targets(env):
    _team(env)
    now = T0 + timedelta(days=(2 - T0.weekday()) % 7 + 7)  # 下周三：前一天是工作日
    r = _run(env, now)  # 默认项目为每日提交模式
    assert {"张工", "李工"} == set(_to(r)) and "缺报" in r["sent"][0]["items"][0]["subject"]
    conn = dbmod.connect()
    conn.execute("UPDATE projects SET config_json='{\"cadence\":\"week\"}'")
    conn.close()
    # 周确认模式：周三只会追上周还没交的（已过周一期限），不追本周
    r = _run(env, now, dry=True)
    assert r["targets"] == 2 and all("进度确认" in i["subject"] and "已超期" in i["status_text"] for s in r["sent"] for i in s["items"])
    fri = T0 + timedelta(days=(4 - T0.weekday()) % 7 + 7)
    r = _run(env, fri, dry=True)
    assert r["targets"] == 4  # 上周 + 本周（周五起提醒）各两人
    # 周确认提交后不再催
    conn = dbmod.connect()
    for back in (0, 7):
        ws = (fri.date() - timedelta(days=fri.weekday() + back)).isoformat()
        conn.execute("INSERT INTO logs (project_id, author_id, log_date, mode, status, started_at, period, period_end) VALUES (?,?,?,?,?,?,?,?)",
                     (env["project_id"], env["users"]["tech1"], ws, "ai", "submitted", dbmod.now_iso(), "week", ws))
    conn.close()
    names = {s["to"]["name"] for s in _run(env, fri)["sent"]}
    assert names == {"李工"}


def test_action_items_adapter(env, monkeypatch):
    _team(env)
    conn = dbmod.connect()
    conn.execute("UPDATE projects SET config_json='{\"nudge\":{\"sources\":{\"logs\":false}}}'")
    conn.execute("CREATE TABLE zz_actions (id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, assignee_id INTEGER, due_date TEXT, status TEXT)")
    due = (T0.date() - timedelta(days=2)).isoformat()
    conn.execute("INSERT INTO zz_actions VALUES (1,?,?,?,?,?)", (env["project_id"], "例会：补报检测报告", env["users"]["tech1"], due, "open"))
    conn.execute("INSERT INTO zz_actions VALUES (2,?,?,?,?,?)", (env["project_id"], "已完成的", env["users"]["tech1"], due, "done"))
    conn.close()
    monkeypatch.setattr(svc, "_ACTION_TABLES", ("zz_actions",))
    r = _run(env, T0)
    assert _to(r) == ["张工"] and [i["subject"] for i in r["sent"][0]["items"]] == ["例会：补报检测报告"]
    assert r["sent"][0]["items"][0]["area"] == "action"
    # 阶段3 对行动项走工程主管
    _run(env, T0 + timedelta(hours=25))
    assert _to(_run(env, T0 + timedelta(hours=50))) == ["工程主管"]


def test_meeting_items_are_picked_up_unless_linked_to_a_finding(env):
    _team(env)
    conn = dbmod.connect()
    if not conn.execute("PRAGMA table_info(meeting_items)").fetchall():
        conn.close()
        pytest.skip("例会行动项表不存在")
    conn.execute("UPDATE projects SET config_json='{\"nudge\":{\"sources\":{\"logs\":false}}}'")
    pid, tech = env["project_id"], env["users"]["tech1"]
    mid = dbmod.insert(conn, "meetings", {"project_id": pid, "title": "周例会", "meeting_date": T0.date().isoformat(), "created_at": dbmod.now_iso()})
    due = (T0.date() - timedelta(days=1)).isoformat()
    fid = _finding(env, "tech1", title="已关联的整改")
    for text, finding in (("补交检测报告", None), ("已挂整改的", fid)):
        dbmod.insert(conn, "meeting_items", {"project_id": pid, "meeting_id": mid, "text": text, "owner_id": tech, "due_date": due,
                                              "finding_id": finding, "created_at": dbmod.now_iso()})
    conn.close()
    subjects = [i["subject"] for s in _run(env, T0)["sent"] for i in s["items"]]
    assert "补交检测报告" in subjects and "已挂整改的" not in subjects and "已关联的整改" in subjects


def test_rules_api_permissions_validation_and_effect(env):
    _team(env)
    _add(env, "boss", "leader", "领导")
    eng, ss = login("eng"), login("ss")
    adm = login("admin")
    for who in ("tech1", "sa_a", "boss", "admin", "pm"):     # 项目经理只拍板，不进催办记录（D7、AC-6.7、D-7）；管理员只管规则
        if who != "admin":
            assert login(who).get("/api/nudge/rules").status_code == 403, who
        assert login(who).post("/api/nudge/run", json={}).status_code == 403, who
        assert login(who).get("/api/nudge/log").status_code == 403, who
    got = ss.get("/api/nudge/rules").json()
    assert got["rules"]["quiet_start"] == "21:00" and got["rules"]["gap_hours"] == [24, 24, 24] and got["rules"]["daily_cap"] == 3
    # 导航编排 10-08：规则归系统管理员，工程主管、安全主管只读
    for c in (eng, ss):
        assert c.put("/api/nudge/rules", json={"daily_cap": 5}).status_code == 403
    assert adm.get("/api/nudge/rules").status_code == 200
    assert adm.put("/api/nudge/rules", json={"quiet_start": "25:99"}).status_code == 400
    assert adm.put("/api/nudge/rules", json={"gap_hours": [1, 2]}).status_code == 400
    assert adm.put("/api/nudge/rules", json={"daily_cap": 0}).status_code == 400
    ok = adm.put("/api/nudge/rules", json={"gap_hours": [2, 3, 4], "quiet_start": "20:30", "sources": {"logs": False}}).json()["rules"]
    assert ok["gap_hours"] == [2, 3, 4] and ok["quiet_start"] == "20:30" and ok["sources"] == {"findings": True, "logs": False, "actions": True}
    # 规则生效：间隔 2 小时即可升级；项目 config 的默认值被保存的规则覆盖
    _finding(env, "tech1")
    _run(env, T0)
    assert _to(_run(env, T0 + timedelta(hours=3))) == ["安全员甲"]
    conn = dbmod.connect()
    conn.execute("DELETE FROM knowledge WHERE kind='nudge_rules'")
    conn.execute("UPDATE projects SET config_json='{\"cadence\":\"day\",\"nudge\":{\"daily_cap\":7}}'")
    conn.close()
    assert ss.get("/api/nudge/rules").json()["rules"]["daily_cap"] == 7
    # 手动立即运行 + 日志 + 今日动态
    rep = ss.post("/api/nudge/run", json={}).json()
    assert {"sent", "deferred", "skipped", "reasons", "summary"} <= set(rep)
    rep = ss.post("/api/nudge/run", json={"dry": True}).json()
    assert rep["dry"] is True
    log = ss.get("/api/nudge/log").json()
    assert log and log[0]["subject"] and log[0]["stage_name"] and log[0]["to"]
    today = eng.get("/api/nudge/today").json()
    assert today["objects"] >= 1 and today["persons"] >= 1
    # 关闭后不再发送
    adm.put("/api/nudge/rules", json={"enabled": False})
    assert ss.post("/api/nudge/run", json={}).json()["sent"] == []


def test_scheduler_respects_env_switch():
    from app.routers import nudge as rt
    assert rt.start_scheduler() is False  # 测试环境 ZB_SCHEDULER=0
    out = svc.tick(T0)
    assert out["projects"] >= 0


# ---------------------------------------------------------------- 并发：多个进程同时跑同一项目（AC-4.1～4.3）

def _concurrent(env, monkeypatch, now, n=4):
    """n 个连接同时跑一轮。在写开头语（AI）处用屏障让各轮都停在“已决定要发、尚未写库”的时刻，重叠最大化。"""
    barrier = threading.Barrier(n)
    real = svc.call_json

    def slow(*a, **k):
        try:
            barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass
        return real(*a, **k)
    monkeypatch.setattr(svc, "call_json", slow)
    reps, errors = [None] * n, []

    def one_run(i):
        conn = dbmod.connect()
        try:
            reps[i] = svc.run_project(conn, env["project_id"], now=now)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()
    threads = [threading.Thread(target=one_run, args=(i,), name=f"run-{i}") for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    return reps


def _nudge_pairs():
    """催办通知与 nudge_log 的对应关系：(催办通知 id 集合, 日志里 sent 行引用的 notif_id 集合, sent 行数)。"""
    conn = dbmod.connect()
    try:
        notes = {r["id"] for r in conn.execute("SELECT n.id FROM events e JOIN notifications n ON n.id=e.ref_id WHERE e.kind='nudge.sent'").fetchall()}
        assert len(notes) == conn.execute("SELECT COUNT(*) n FROM events WHERE kind='nudge.sent'").fetchone()["n"]
        logged = {r["notif_id"] for r in conn.execute("SELECT notif_id FROM nudge_log WHERE status='sent'").fetchall()}
        rows = conn.execute("SELECT COUNT(*) n FROM nudge_log WHERE status='sent'").fetchone()["n"]
        return notes, logged, rows
    finally:
        conn.close()


def test_concurrent_runs_send_each_item_once_and_log_matches_notifications(env, monkeypatch):
    # 10-07 实况：4 个调度同时跑，同一人同一分钟收到 4 条相同催办，其中 3 条不在 nudge_log 里
    _team(env)
    _finding(env, "tech1")
    _finding(env, "tech2", title="乙的事项")
    reps = _concurrent(env, monkeypatch, T0)
    assert sorted(n["user_id"] for n in _notes(env, like="催办提醒%")) == sorted([env["users"]["tech1"], env["users"]["tech2"]])
    assert sum(len(r["sent"]) for r in reps) == 2
    notes, logged, rows = _nudge_pairs()
    assert len(notes) == 2 and notes == logged and rows == 2   # 每条催办通知都在 nudge_log 里，日志里的每条都对应真实通知
    conn = dbmod.connect()
    assert conn.execute("SELECT COUNT(*) n FROM audit WHERE action='nudge.auto'").fetchone()["n"] == 2
    assert conn.execute("SELECT COUNT(*) n FROM nudge_log WHERE status<>'sent' AND status<>'skipped'").fetchone()["n"] == 0  # 认领中间态不落库
    conn.close()
    # 之后再跑：未到升级时间，不再发
    assert _run(env, T0 + timedelta(hours=1))["sent"] == []


def test_concurrent_runs_never_exceed_daily_cap(env, monkeypatch):
    # 各轮看到的事项不同（如“立即运行”与后台调度同时跑），同一人当天的催办总数仍不超过每日上限
    _team(env)
    login("admin").put("/api/nudge/rules", json={"daily_cap": 2})
    tech = env["users"]["tech1"]

    def per_run_targets(conn, pid, rules, day, users):
        i = int(threading.current_thread().name.rsplit("-", 1)[1])
        return [svc._target("finding", str(900 + i), "safety", f"并发事项{i}", day - timedelta(days=1), day, tech, f"finding?id={900 + i}", 900 + i)], []
    monkeypatch.setattr(svc, "collect_targets", per_run_targets)
    reps = _concurrent(env, monkeypatch, T0)
    assert len(_notes(env, "tech1", "催办提醒%")) == 2
    assert sum(len(r["sent"]) for r in reps) == 2 and sum(r["reasons"].get("cap", 0) for r in reps) == 2
    notes, logged, rows = _nudge_pairs()
    assert notes == logged and rows == 2
    conn = dbmod.connect()
    assert svc._sent_today(conn, env["project_id"], tech, T0.date().isoformat()) == 2
    conn.close()


def test_same_item_same_person_at_most_once_a_day(env, monkeypatch):
    # 升级链的几个阶段若落到同一人（如某级缺人顺延、规则调整），同一事项当天只提醒他一次，次日再发
    _team(env)
    _finding(env, "tech1")
    eng = env["users"]["eng"]
    login("admin").put("/api/nudge/rules", json={"gap_hours": [1, 1, 1]})
    real = svc._recipients
    monkeypatch.setattr(svc, "_recipients", lambda stage, t, users: real(stage, t, users) if stage == 1 else ([users[eng]], ""))
    assert _to(_run(env, T0)) == ["张工"]
    r2 = _run(env, T0 + timedelta(hours=2))
    assert _to(r2) == ["工程主管"] and r2["sent"][0]["stage"] == 2
    r3 = _run(env, T0 + timedelta(hours=4))   # 阶段3 又轮到工程主管：当天不再就此事提醒他
    assert r3["sent"] == [] and r3["reasons"].get("same_day") == 1 and r3["deferred"][0]["code"] == "same_day"
    assert "今天已提醒过同一人" in r3["summary"]
    r4 = _run(env, T0 + timedelta(days=1))
    assert _to(r4) == ["工程主管"] and r4["sent"][0]["stage"] == 3
    assert len(_notes(env, "eng")) == 2


# ---------------------------------------------------------------- 固定 / 订阅式提问

def _ask(c, q="本周有哪些关键作业滞后？"):
    return c.post("/api/ask", json={"question": q}).json()


def test_pin_subscribe_refresh_and_failure_keeps_last_answer(env, monkeypatch):
    _team(env)
    pm = login("pm")
    a = _ask(pm)
    pin = pm.post("/api/nudge/pins", json={"question": a["question"], "schedule": "fri", "answer": a}).json()
    assert pin["schedule"] == "fri" and pin["schedule_name"] == "每周五" and pin["answer"]["answer"] == a["answer"]
    assert pin["answer"]["refs"] == a["refs"] and pin["answer"]["at"]
    # 同一问题再固定只更新订阅周期
    again = pm.post("/api/nudge/pins", json={"question": a["question"], "schedule": "daily", "answer": a}).json()
    assert again["id"] == pin["id"] and again["schedule"] == "daily" and len(pm.get("/api/nudge/pins").json()) == 1
    assert pm.put(f"/api/nudge/pins/{pin['id']}", json={"schedule": "mon"}).json()["schedule_name"] == "每周一"
    assert pm.put(f"/api/nudge/pins/{pin['id']}", json={"schedule": "yearly"}).status_code == 400
    assert pm.post("/api/nudge/pins", json={"question": " "}).status_code == 400

    # 失败不覆盖上次成功答案
    before = pm.get("/api/nudge/pins").json()[0]
    monkeypatch.setattr(svc.qa_svc, "ask", lambda *a, **k: (_ for _ in ()).throw(AIError("交融超时")))
    res = pm.post(f"/api/nudge/pins/{pin['id']}/refresh").json()
    assert res["ok"] is False and "交融超时" in res["error"] and res["answer"]["answer"] == before["answer"]["answer"]
    assert pm.get("/api/nudge/pins").json()[0]["last_run_at"] == before["last_run_at"]
    monkeypatch.undo()
    ok = pm.post(f"/api/nudge/pins/{pin['id']}/refresh").json()
    assert ok["ok"] is True and ok["last_error"] is None and ok["answer"]["refs"] is not None

    # 取消；别人动不了
    eng = login("eng")
    assert eng.delete(f"/api/nudge/pins/{pin['id']}").status_code == 400
    assert eng.post(f"/api/nudge/pins/{pin['id']}/refresh").status_code == 400
    assert pm.delete(f"/api/nudge/pins/{pin['id']}").status_code == 200 and pm.get("/api/nudge/pins").json() == []


def test_pin_roles_and_limits(env):
    _team(env)
    _add(env, "boss", "leader", "领导")
    for who in ("tech1", "sa_a", "boss", "admin"):
        c = login(who)
        assert c.get("/api/nudge/pins").status_code == 403, who
        assert c.post("/api/nudge/pins", json={"question": "x"}).status_code == 403, who
    # 不带回答（接口直调）时现问一次
    p = login("ss").post("/api/nudge/pins", json={"question": "最近记录了哪些安全隐患？"}).json()
    assert p["answer"] and p["answer"]["answer"]
    for i in range(svc.MAX_PINS_PER_USER - 1):
        assert login("ss").post("/api/nudge/pins", json={"question": f"问题{i}"}).status_code == 200
    assert login("ss").post("/api/nudge/pins", json={"question": "超出上限"}).status_code == 400


def test_due_pins_rerun_on_schedule_and_notify(env, monkeypatch):
    _team(env)
    pm = login("pm")
    a = _ask(pm)
    pin = pm.post("/api/nudge/pins", json={"question": a["question"], "schedule": "mon", "answer": a}).json()
    # 把上次运行改到很久以前
    conn = dbmod.connect()
    conn.execute("UPDATE ask_pins SET last_run_at='2020-01-01T09:00:00+08:00'")
    next_mon = T0.replace(hour=9) + timedelta(days=(0 - T0.weekday()) % 7 + 7)
    conn.execute("UPDATE ask_pins SET last_run_at=?", ((next_mon - timedelta(days=7)).isoformat(),))
    assert svc.run_due_pins(conn, next_mon.replace(hour=7)) == 0     # 当天 8 点前不跑
    assert svc.run_due_pins(conn, next_mon + timedelta(days=1)) == 1 # 周二：周一的订阅补跑一次
    conn.execute("UPDATE ask_pins SET last_run_at=?", ((next_mon - timedelta(days=7)).isoformat(),))
    assert svc.run_due_pins(conn, next_mon) == 1                     # 周一 9 点按时跑
    assert conn.execute("SELECT COUNT(*) n FROM notifications WHERE title='你订阅的提问有新回答' AND user_id=?", (env["users"]["pm"],)).fetchone()["n"] == 2
    # 周三不跑；每周五订阅在周五才跑；只固定不订阅的从不自动跑
    conn.execute("UPDATE ask_pins SET last_run_at=?", ((next_mon + timedelta(hours=1)).isoformat(),))
    assert svc.run_due_pins(conn, next_mon + timedelta(days=2)) == 0
    conn.execute("UPDATE ask_pins SET schedule='fri'")
    assert svc.run_due_pins(conn, next_mon + timedelta(days=4)) == 1
    conn.execute("UPDATE ask_pins SET schedule='', last_run_at='2020-01-01T09:00:00+08:00'")
    assert svc.run_due_pins(conn, next_mon + timedelta(days=30)) == 0
    # 失败不覆盖答案、两小时内不重试
    conn.execute("UPDATE ask_pins SET schedule='daily'")
    monkeypatch.setattr(svc.qa_svc, "ask", lambda *a, **k: (_ for _ in ()).throw(AIError("坏了")))
    now = next_mon + timedelta(days=60)
    kept = conn.execute("SELECT last_answer_json FROM ask_pins").fetchone()["last_answer_json"]
    assert svc.run_due_pins(conn, now) == 1
    assert conn.execute("SELECT last_answer_json FROM ask_pins").fetchone()["last_answer_json"] == kept
    assert svc.pin_due(conn.execute("SELECT * FROM ask_pins").fetchone(), datetime.now()) is False
    conn.close()
