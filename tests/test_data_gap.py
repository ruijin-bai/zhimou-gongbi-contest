"""T03：数据缺口（没报/证据不足）与进度滞后（实际落后）分开；回测与命中率口径隔离；缺口不推送项目经理。"""
from contextlib import closing
from datetime import date, datetime, timedelta

from conftest import START, TODAY, login, sample_xer
from sample_xer import make_xer
from test_facts import _add_alert
from test_roles import _team

from app import db as dbmod
from app.services import brief
from app.services import nudge as nudge_svc
from app.services import progress as prog

TH = dict(prog.DEFAULT_THRESHOLDS)
DAY = date(2026, 10, 10)
DD = DAY - timedelta(days=10)


def _act(**kw) -> dict:
    a = {"code": "A1010", "name": "桩基", "early_start": DD.isoformat(), "early_finish": (DD + timedelta(days=20)).isoformat(), "phys_pct": 30.0,
         "act_start": DD.isoformat(), "act_finish": None, "is_milestone": 0, "is_critical": 1, "total_float_d": 0.0}
    a.update(kw)
    return a


# ----------------------------------------------------------------------------- 判定

def test_gap_is_not_a_progress_alert():
    """关键作业已开工、计划推进、N 天没有日报：只产生数据缺口，不产生任何进度预警。"""
    res, gap = prog.assess_full(_act(), None, DAY, DD, TH)
    assert res is None and gap is not None
    assert gap["idle_days"] == 10 and gap["last_report"] is None
    assert prog.assess(_act(), None, DAY, DD, TH) is None  # 对外 assess 不再返回 stale 预警
    # 有过日志但没有进度数字、已 4 天没更新：空缺天数从最后上报日算起
    st = {"pct": None, "pct_date": None, "last_date": (DAY - timedelta(days=4)).isoformat(), "reports": 1}
    res, gap = prog.assess_full(_act(), st, DAY, DD, TH)
    assert res is None and gap["idle_days"] == 4 and gap["last_report"] == (DAY - timedelta(days=4)).isoformat()
    # 阈值仍走 thresholds：stale_days 调大后不再算缺口
    assert prog.assess_full(_act(), None, DAY, DD, {**TH, "stale_days": 11})[1] is None


def test_gap_threshold_defaults_unchanged():
    assert prog.DEFAULT_THRESHOLDS["stale_days"] == 3
    for k in ("red_slip_days", "orange_float_ratio", "orange_min_days", "yellow_slip_days", "start_grace_days", "risk_lookback_days"):
        assert k in prog.DEFAULT_THRESHOLDS


def test_behind_alert_unchanged_and_not_a_gap():
    """日报有进度且落后：仍产生 behind，关键作业滞后≥1天为红色，与原逻辑一致；最近有上报则无缺口。"""
    st = {"pct": 40.0, "pct_date": DAY.isoformat(), "last_date": DAY.isoformat(), "reports": 3}
    res, gap = prog.assess_full(_act(), st, DAY, DD, TH)
    assert gap is None and res["rule"] == "behind" and res["level"] == "red" and res["facts"]["slip_days"] > 1
    # 非关键作业、滞后只够黄色时等级不变
    res2 = prog.assess(_act(is_critical=0, total_float_d=40.0, early_finish=(DD + timedelta(days=40)).isoformat()),
                       {"pct": 12.0, "pct_date": DAY.isoformat(), "last_date": DAY.isoformat(), "reports": 3}, DAY, DD, TH)
    assert res2 and res2["rule"] == "behind"


def test_behind_with_old_report_stays_behind_not_gap():
    st = {"pct": 40.0, "pct_date": (DAY - timedelta(days=5)).isoformat(), "last_date": (DAY - timedelta(days=5)).isoformat(), "reports": 3}
    res, gap = prog.assess_full(_act(), st, DAY, DD, TH)
    assert res and res["rule"] == "behind" and res["level"] == "red" and gap is None  # 已判滞后的作业不再另记缺口


def test_late_start_still_alert():
    a = _act(act_start=None, phys_pct=0.0, early_start=(DAY - timedelta(days=5)).isoformat(), early_finish=(DAY + timedelta(days=5)).isoformat())
    res, gap = prog.assess_full(a, None, DAY, DAY - timedelta(days=6), TH)
    assert res and res["rule"] == "late_start" and gap is None


# ----------------------------------------------------------------------------- 回测

def test_backtest_gap_excluded_from_confusion_matrix():
    d0, d1, d2 = (TODAY - timedelta(days=8)).isoformat(), (TODAY - timedelta(days=4)).isoformat(), TODAY.isoformat()
    from app.services import p6
    s0 = make_xer(d0, start=START, progress={"M0000": 100, "A1010": 20, "A2010": 0, "A3010": 10})
    # S1：A1010 在 S1 无任何上报（0%，无实际开始），而 S0 时已开工 → 4 天无进度 → 数据缺口；A3010 正常
    s1 = make_xer(d1, start=START, progress={"M0000": 100, "A1010": 0, "A2010": 15, "A3010": 30})
    s2 = make_xer(d2, start=START, progress={"M0000": 100, "A1010": 70, "A2010": 40, "A3010": 50}, shift={"A1010": 6, "A3010": 2})
    snaps = []
    for s in (s0, s1, s2):
        meta, acts, rels = p6.activities_from_xer(s)
        snaps.append((meta, acts, rels))
    res = prog.backtest(snaps)
    assert res["mode"] == "three-period"
    assert res["data_gaps"] >= 1 and any(g["code"] == "A1010" for g in res["gap_rows"])
    assert all(r["code"] != "A1010" for r in res["rows"])  # 缺口作业不在样本里
    assert res["tp"] + res["fp"] + res["fn"] + res["tn"] == res["samples"] == len(res["rows"])  # 混淆矩阵分母不含缺口
    assert "不计入预警准确率" in res["definitions"]["数据缺口"] and "不含数据缺口" in res["definitions"]["样本"]
    # 没有缺口的同一组快照：样本数 = 缺口场景样本数 + 缺口数（缺口只是被单列，不是丢失）
    s1_ok = make_xer(d1, start=START, progress={"M0000": 100, "A1010": 25, "A2010": 15, "A3010": 30})
    snaps2 = [p6.activities_from_xer(s) for s in (s0, s1_ok, s2)]
    res2 = prog.backtest(snaps2)
    assert res2["data_gaps"] == 0 and res2["samples"] == res["samples"] + res["data_gaps"]


def test_backtest_api_and_metrics_report_gap_count(env):
    adm = login("admin")
    d0, d1, d2 = (TODAY - timedelta(days=8)).isoformat(), (TODAY - timedelta(days=4)).isoformat(), TODAY.isoformat()
    s0 = make_xer(d0, start=START, progress={"M0000": 100, "A1010": 20, "A2010": 0, "A3010": 10})
    s1 = make_xer(d1, start=START, progress={"M0000": 100, "A1010": 0, "A2010": 15, "A3010": 30})
    s2 = make_xer(d2, start=START, progress={"M0000": 100, "A1010": 70, "A2010": 40, "A3010": 50}, shift={"A1010": 6, "A3010": 2})
    files = [("files", (f"u{i}.xer", s.encode(), "application/octet-stream")) for i, s in enumerate((s0, s1, s2))]
    assert adm.post("/api/p6/backtest", files=files, data={"source": "test"}).status_code in (404, 405)  # 旧接口已下线
    assert login("pm").post("/api/backtest/run", files=files, data={"source": "test"}).status_code == 403  # 项目经理不看回测与数据缺口
    r = adm.post("/api/backtest/run", files=files, data={"source": "test"})
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["data_gaps"] >= 1 and any(g["code"] == "A1010" for g in r.json()["gap_rows"])
    latest = adm.get("/api/backtest/latest?source=test").json()
    assert latest["id"] == r.json()["id"] and latest["summary"]["data_gaps"] == r.json()["summary"]["data_gaps"]
    m = adm.get("/api/metrics").json()
    assert m["backtest"] is None  # 仿真/测试数据的回测不计入应用成效
    assert "数据缺口" in m["definitions"] and "不计入预警准确率" in m["definitions"]["数据缺口"]
    assert m["alerts"]["gap_note"] == "数据缺口不计入预警准确率"


# ----------------------------------------------------------------------------- 命中率口径与历史 stale

def test_feedback_stats_and_metrics_exclude_stale_history(env):
    pm = login("pm")
    pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    pid = env["project_id"]
    a1 = _add_alert(pid, code="A1020", rule="behind", status="confirmed")
    a2 = _add_alert(pid, code="A1030", rule="stale", level="orange", status="false_alarm")
    _add_alert(pid, code="A1040", rule="stale", level="orange", status="open")
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE alerts SET feedback_by=?, feedback_at=? WHERE id IN (?,?)", (env["users"]["pm"], dbmod.now_iso(), a1, a2))
        st = prog.feedback_stats(conn, pid)
    assert st["confirmed"] == 1 and st["false_alarm"] == 0 and st["precision"] == 1.0  # 误报的旧 stale 不拉低命中率
    assert "stale" not in st["by_rule"] and st["data_gap_history"] == 2
    assert "数据缺口不计入预警准确率" in st["definition"]
    m = login("admin").get("/api/metrics").json()["alerts"]
    assert m["total"] == 1 and m["confirmed"] == 1 and m["false_alarm"] == 0 and m["data_gaps_history"] == 2
    assert pm.get("/api/alerts/stats").json()["data_gap_history"] == 2


def test_historical_stale_alerts_hidden_from_pm_and_brief_shown_as_history_to_eng(env):
    _team(env)
    pm, eng = login("pm"), login("eng")
    pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    pid = env["project_id"]
    aid = _add_alert(pid, code="A1030", rule="stale", level="orange", status="open")
    _add_alert(pid, code="A1020", rule="behind", level="red", status="open")
    # 项目经理：预警列表/详情看不到历史缺口；简报与总览统计也不含
    assert [a["rule"] for a in pm.get("/api/alerts?status=").json()] == ["behind"]
    assert pm.get(f"/api/alerts/{aid}").status_code == 404
    with closing(dbmod.connect()) as conn:
        k = brief.kpis(conn, pid, brief.date.today())
    assert k["alerts"] == {"red": 1, "orange": 0, "yellow": 0}
    assert pm.get("/api/dashboard").status_code == 404   # 旧首页接口已下线；项目经理总览走 /api/cockpit
    assert pm.get("/api/cockpit").json()["kpis"]["alerts"] == {"red": 1, "orange": 0, "yellow": 0}
    # 工程主管：列表里显示为“数据缺口（历史）”
    rows = {a["rule"]: a for a in eng.get("/api/alerts?status=").json()}
    assert rows["stale"]["rule_name"] == "数据缺口（历史）" and rows["behind"]["rule_name"] == "进度滞后"
    # 重新判定不改动历史缺口预警（保持未关闭，不被系统自动关闭）
    pm.post("/api/alerts/detect", json={"as_of": TODAY.isoformat()})
    with closing(dbmod.connect()) as conn:
        assert conn.execute("SELECT status FROM alerts WHERE id=?", (aid,)).fetchone()["status"] == "open"


# ----------------------------------------------------------------------------- 端到端：缺口不推项目经理，提醒技术员，工程主管单列

def _gap_env(env):
    """数据日期 5 天前：A1010 在数据日期时已 80%，之后没有任何日报 → 数据缺口（责任人 tech1=张工，甲单位）。"""
    _team(env)
    pm = login("pm")
    r = pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(data_date=(TODAY - timedelta(days=5)).isoformat()), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return pm


def test_gap_produces_no_alert_and_no_push_to_pm(env):
    pm = _gap_env(env)
    r = pm.post("/api/alerts/detect", json={"as_of": TODAY.isoformat()})
    assert r.status_code == 200, r.text
    alerts = pm.get("/api/alerts?status=").json()
    assert all(a["rule"] != "stale" for a in alerts)
    assert not [a for a in alerts if a["activity_code"] == "A1010" and a["rule"] in ("behind", "late_start", "stale")]
    with closing(dbmod.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM alerts WHERE rule='stale'").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) n FROM notifications WHERE title LIKE '%A1010%' AND alert_id IS NOT NULL").fetchone()["n"] == 0
        ev = prog.evaluate(conn, env["project_id"], TODAY.isoformat())
        assert any(g["activity"]["code"] == "A1010" for g in ev["gaps"])
        assert all(x["rule"] != "stale" for x in ev["results"])
    assert pm.get("/api/notifications").json() == []


def test_gap_listed_on_eng_workbench_with_owner_and_remind(env):
    _gap_env(env)
    eng = login("eng")
    ov = eng.get("/api/eng/overview").json()
    g = next(x for x in ov["gaps"] if x["code"] == "A1010")
    assert g["idle_days"] >= 3 and g["owners"][0]["name"] == "张工" and g["companies"] == ["甲"]
    assert g["last_report"] is None and ov["gaps_total"] >= 1
    assert {"code", "name", "last_report", "idle_days", "owners"} <= set(g)
    # “一键提醒”复用现有催办接口，提醒的是责任技术员
    r = eng.post(f"/api/users/{g['owners'][0]['id']}/nudge", json={"text": "A1010 已连续多天没有日报进度，请补报"})
    assert r.status_code == 200
    n = login("tech1").get("/api/notifications").json()
    assert any("3#墩桩基施工" in x["body"] for x in n)   # 作业代码显示为中文简称
    assert login("pm").get("/api/notifications").json() == []


def _run(env, now):
    with closing(dbmod.connect()) as conn:
        return nudge_svc.run_project(conn, env["project_id"], now=now)


def _bodies(env, username):
    with closing(dbmod.connect()) as conn:
        return [r["body"] for r in conn.execute("SELECT body FROM notifications WHERE user_id=? AND title LIKE '%' ", (env["users"][username],)).fetchall()]


def test_gap_nudge_chain_tech_then_eng_never_safety_or_pm(env):
    _gap_env(env)
    t0 = datetime.combine(TODAY, datetime.min.time()).replace(hour=10)
    r1 = _run(env, t0)
    assert any(s["to"]["name"] == "张工" and any("A1010" in i["subject"] for i in s["items"]) for s in r1["sent"])
    assert any("A1010" in b and "没有日报进度" in b for b in _bodies(env, "tech1"))
    # 幂等：同一时刻重跑不重复
    assert not any(any("A1010" in i["subject"] for i in s["items"]) for s in _run(env, t0)["sent"])
    # 逐级（D9）：本人 → 工程主管；进度类催办不发给分包安全员，全程项目经理收不到
    _run(env, t0 + timedelta(hours=25))
    assert any("A1010" in b for b in _bodies(env, "eng"))
    _run(env, t0 + timedelta(hours=50))
    _run(env, t0 + timedelta(hours=75))
    _run(env, t0 + timedelta(hours=100))
    assert not any("A1010" in b for b in _bodies(env, "pm"))
    assert _bodies(env, "pm") == []
    assert not any("A1010" in b for b in _bodies(env, "sa_a"))  # 本单位安全员也不收进度类催办（D9）
    assert not any("A1010" in b for b in _bodies(env, "sa_b"))  # 乙单位无关


def test_gap_nudge_recipients_stop_at_eng_sup():
    users = {1: {"id": 1, "role": "tech", "company": "甲", "name": "张工"}, 2: {"id": 2, "role": "eng_sup", "company": "", "name": "郑工"},
             3: {"id": 3, "role": "manager", "company": "", "name": "项目经理"}, 4: {"id": 4, "role": "sub_safety", "company": "甲", "name": "安全员"}}
    t = {"area": "gap", "owner_id": 1}
    assert [u["id"] for u in nudge_svc._recipients(1, t, users)[0]] == [1]
    r2, why2 = nudge_svc._recipients(2, t, users)
    assert r2 == [] and "安全员" in why2                     # D9：进度类催办不发给分包安全员
    assert [u["id"] for u in nudge_svc._recipients(3, t, users)[0]] == [2]
    r, why = nudge_svc._recipients(4, t, users)
    assert r == [] and "项目经理" in why


def test_gap_status_text_states_judgment_day_and_no_report_wording(env):
    """U-26：同一口径写明判定日；从没上报过时不写“最后上报无”。"""
    _gap_env(env)
    t0 = datetime.combine(TODAY, datetime.min.time()).replace(hour=10)
    _run(env, t0)
    body = next(b for b in _bodies(env, "tech1") if "A1010" in b)
    assert f"截至{TODAY.strftime('%m-%d')}" in body and "最后上报无" not in body
    assert "之后没有上报过" in body


# ----------------------------------------------------------------------------- C13 / AC-4.5：本周日志里报过该作业就不算缺口

def _log(env, user, day, content, status="draft", period="day"):
    with closing(dbmod.connect()) as conn:
        return dbmod.insert(conn, "logs", {"project_id": env["project_id"], "author_id": env["users"][user], "log_date": day, "mode": "ai", "status": status,
                                           "weather": "晴", "voice_text": "", "content_json": dbmod.dumps(content), "started_at": day + "T07:00:00",
                                           "period": period, "auto": 1})


def _gap_codes(env):
    with closing(dbmod.connect()) as conn:
        return {g["code"] for g in prog.data_gaps(conn, env["project_id"], TODAY.isoformat())}


def test_gap_waived_when_this_week_log_has_progress_row(env):
    _gap_env(env)
    assert "A1010" in _gap_codes(env)
    week_start = TODAY - timedelta(days=TODAY.weekday())
    # 只在施工内容里顺带提到：不算
    _log(env, "tech1", TODAY.isoformat(), {"works": [{"activity_code": "A1010", "desc": "桩基施工"}], "progress": []})
    assert "A1010" in _gap_codes(env)
    # 上周的日志草稿有完成量：不算本周
    _log(env, "tech1", (week_start - timedelta(days=1)).isoformat(), {"progress": [{"activity_code": "A1010", "qty": 5, "unit": "m", "pct": None}]})
    assert "A1010" in _gap_codes(env)
    # 进度行既没有完成量也没有完成%：不算
    _log(env, "tech2", TODAY.isoformat(), {"progress": [{"activity_code": "A1010", "qty": None, "pct": None}]})
    assert "A1010" in _gap_codes(env)
    # 本周任一天的日志草稿里有该作业的完成量行：算“已提到”，不是缺口
    _log(env, "tech1", week_start.isoformat(), {"progress": [{"activity_code": "A1010", "qty": 12, "unit": "m", "pct": None}]}, period="week")
    assert "A1010" not in _gap_codes(env)


def test_gap_waived_by_pct_row_and_nudge_follows(env):
    _gap_env(env)
    t0 = datetime.combine(TODAY, datetime.min.time()).replace(hour=10)
    _log(env, "tech1", TODAY.isoformat(), {"progress": [{"activity_code": "A1010", "qty": None, "pct": 85}]})
    assert "A1010" not in _gap_codes(env)
    r = _run(env, t0)
    assert not any("A1010" in i["subject"] for s in r["sent"] for i in s["items"])  # 已报的作业不催补报
    # 判定口径（evaluate，回测与预警共用）不受影响：豁免只在 data_gaps
    with closing(dbmod.connect()) as conn:
        assert any(g["activity"]["code"] == "A1010" for g in prog.evaluate(conn, env["project_id"], TODAY.isoformat())["gaps"])


# ----------------------------------------------------------------------------- F-13 / AC-4.9：预警重开清掉系统关闭说明；AC-3.6：P6 数据日期

def test_reopened_alert_clears_system_close_note(env, monkeypatch):
    pm = login("pm")
    pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    pid = env["project_id"]
    with closing(dbmod.connect()) as conn:
        imp = conn.execute("SELECT * FROM p6_imports WHERE project_id=? AND is_current=1", (pid,)).fetchone()
    hit = {"activity": {"code": "A1020", "name": "墩身"}, "rule": "behind", "level": "red", "facts": {"slip_days": 3.0, "total_float_d": 0.0, "float_left_d": -3.0}}
    state = {"results": [hit]}
    monkeypatch.setattr(prog, "evaluate", lambda conn, project_id, as_of=None: {"import": imp, "results": state["results"], "gaps": [], "as_of": TODAY.isoformat()})
    with closing(dbmod.connect()) as conn:
        assert len(prog.detect(conn, pid)["new"]) == 1
        aid = conn.execute("SELECT id FROM alerts WHERE project_id=? AND fingerprint='behind:A1020'", (pid,)).fetchone()["id"]
        state["results"] = []                         # 条件解除：系统关闭并写说明
        assert prog.detect(conn, pid)["cleared"] == 1
        row = conn.execute("SELECT status, feedback_note FROM alerts WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "closed" and row["feedback_note"] == "系统：触发条件已解除"
        state["results"] = [hit]                      # 条件再现：重开，说明清掉
        assert prog.detect(conn, pid)["escalated"] == [aid]
        row = conn.execute("SELECT status, feedback_note FROM alerts WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "open" and row["feedback_note"] == ""
    assert pm.get(f"/api/alerts/{aid}").json()["feedback_note"] == ""


def test_serialize_alert_p6_data_date(env):
    pm = login("pm")
    pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    pid = env["project_id"]
    a_new = _add_alert(pid, code="A1020", explanation={"p6_data_date": "2026-09-21"})
    a_old = _add_alert(pid, code="A1030")                               # 老预警：explanation 里没有，按 import_id 查
    a_none = _add_alert(pid, code="A2010")
    with closing(dbmod.connect()) as conn:
        dd = conn.execute("SELECT data_date FROM p6_imports WHERE project_id=? AND is_current=1", (pid,)).fetchone()["data_date"]
        conn.execute("UPDATE alerts SET import_id=99999 WHERE id=?", (a_none,))   # 批次已不存在
    assert pm.get(f"/api/alerts/{a_new}").json()["p6_data_date"] == "2026-09-21"
    assert pm.get(f"/api/alerts/{a_old}").json()["p6_data_date"] == dd
    assert pm.get(f"/api/alerts/{a_none}").json()["p6_data_date"] is None   # 查不到不补默认值
    rows = {a["id"]: a for a in pm.get("/api/alerts?status=").json()}
    assert rows[a_new]["p6_data_date"] == "2026-09-21" and rows[a_old]["p6_data_date"] == dd


def test_gap_not_in_pm_brief_decision_queue_or_facts(env):
    _gap_env(env)
    pm = login("pm")
    pm.post("/api/alerts/detect", json={"as_of": TODAY.isoformat()})
    with closing(dbmod.connect()) as conn:
        k = brief.kpis(conn, env["project_id"], brief.date.today())
        text = brief.facts_text(conn, env["project_id"], k)
        queue = brief.decision_queue(conn, env["project_id"], 50)
    assert k["alerts"] == {"red": 0, "orange": 0, "yellow": 0}
    for word in ("数据缺口", "无进度", "没有日报进度", "补报"):
        assert word not in text
        assert not any(word in str(q) for q in queue)


def test_gap_hidden_from_pm_on_eng_overview_api(env):
    _gap_env(env)
    eng = login("eng").get("/api/eng/overview").json()
    pm = login("pm").get("/api/eng/overview").json()
    assert eng["gaps_total"] >= 1
    assert pm["gaps"] == [] and pm["gaps_total"] == 0


def test_page_text_states_gap_exclusion():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    views = (root / "views.js").read_text(encoding="utf-8")
    roles = (root / "roles.js").read_text(encoding="utf-8")
    assert "不计入预警准确率" in views and "数据缺口（历史）" in views and "没报，不是落后" in views
    assert "数据缺口" not in roles  # T05：数据缺口并入首页统一待办（只给工程主管），页面文字由待办条目给出
    todo = (root.parent / "services" / "todo.py").read_text(encoding="utf-8")
    assert "数据缺口" in todo and "不是“落后”" in todo
