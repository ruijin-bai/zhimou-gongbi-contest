"""体检 A 组：AI 输出自相矛盾（离线 mock 模型）。
A1 现场圈旧点评按当前事实复核、失效折叠；不叫分包“同步 P6”。
A2 产值分析：高于/低于、差额、百分比由系统先算，模型说反即拦；金额按万显示；周期与卡片一致。
A3 预警分析：记录判定快照，判定变化即标过期；措施期限不早于判定日；责任人只用系统岗位。
A4 赶工方案：不写金额只写资源；“工效偏低”与诊断表同一阈值。
A5 简报：零样本不下“可控/正常/良好”结论。
A6 随手记删除后重整/清空当日自动草稿；“没记录”不得写成“没施工”。
"""
from contextlib import closing
from datetime import date, timedelta

from conftest import login
from test_agents import _setup, _submit_today
from test_circle_agents import _agent_comments, _key, _setup_site
from test_facts import _add_alert, _bad_call, _bump, _explain_payload, _gen, _note

from app import db as dbmod
from app.services import alerts as alert_svc
from app.services import brief, feed_agents, output, schedule
from app.services import facts as F
from app.services import progress_agent
from app.services.progress import serialize_alert
from app.ai import alert_agent, log_agent


# ============================================================================ A1 现场圈旧点评复核

def _old_progress_comment(pid: int, key: str, text: str, code: str = "A1020", extra_ev: list | None = None) -> None:
    """模拟矛盾校验上线前已生成并缓存的进度点评。"""
    ev = [{"text": f"{code} 3#墩承台钢筋绑扎，P6 计划 2026-09-01～2026-09-30", "ref": {"type": "act", "code": code, "label": code}}] + (extra_ev or [])
    with closing(dbmod.connect()) as conn:
        dbmod.insert(conn, "feed_agent_notes", {"project_id": pid, "post_key": key, "perspective": "progress", "text": text,
                                                "evidence_json": dbmod.dumps(ev), "source": "ai", "audience": "all", "created_at": dbmod.now_iso()})


def _prog(c, key):
    return [x for x in _agent_comments(c, key) if x["agent"]["view"] == "progress"]


def test_old_feed_comment_contradicting_current_facts_is_marked_stale(env):
    _setup_site(env)
    pid = env["project_id"]
    _add_alert(pid)  # A1020 红色，滞后 8 天
    t1 = login("tech1")
    k1, k2, k3 = _note(t1), _note(t1, "3号墩承台钢筋绑扎第二天"), _note(t1, "3号墩承台钢筋绑扎第三天")
    _old_progress_comment(pid, k1, "A1020 3#墩承台钢筋绑扎按计划推进，完成77.8%")
    _old_progress_comment(pid, k2, "A1020 的 P6 没更新，记得同步哈")
    _old_progress_comment(pid, k3, "A1020 滞后 8 天，预警还没关")
    c1, c2, c3 = _prog(t1, k1)[0], _prog(t1, k2)[0], _prog(t1, k3)[0]
    # 与当前事实矛盾：标记失效，但原文保留（前端折叠为“情况已变化，此点评已过时”）
    assert c1["agent"]["stale"] is True and c1["text"] == "3#墩承台钢筋绑扎按计划推进，完成77.8%"  # 作业代码在接口出口换成中文简称（app/services/act_names.py）
    assert any("矛盾" in r for r in c1["agent"]["stale_reasons"])
    assert c2["agent"]["stale"] is True and any("P6" in r for r in c2["agent"]["stale_reasons"])
    # 与当前事实一致的点评照常显示
    assert c3["agent"]["stale"] is False and c3["agent"]["stale_reasons"] == []
    with closing(dbmod.connect()) as conn:  # 原文没有被删改
        assert conn.execute("SELECT COUNT(*) n FROM feed_agent_notes WHERE project_id=?", (pid,)).fetchone()["n"] == 3


def test_old_feed_comment_becomes_stale_when_lag_changes(env):
    _setup_site(env)
    pid = env["project_id"]
    aid = _add_alert(pid)
    t1 = login("tech1")
    k = _note(t1)
    _old_progress_comment(pid, k, "A1020 滞后 8 天，预警还没关")
    assert _prog(t1, k)[0]["agent"]["stale"] is False
    _bump(aid, "red", 12.0)  # 判定加重到 12 天：旧点评里的 8 天已不是当前事实
    cm = _prog(t1, k)[0]
    assert cm["agent"]["stale"] is True and any("滞后天数" in r for r in cm["agent"]["stale_reasons"])


def test_feed_comment_never_asks_subcontractor_to_sync_p6(env, monkeypatch):
    pm, tech = _setup()
    _submit_today(tech)  # 日志累计 A1010 82% > P6 80%：依据带【P6待同步】
    lkey = _key(pm, "log")
    calls = _bad_call(monkeypatch, "feed_agents", "feed.comment", [{"comments": [{"view": "progress", "text": "A1010 日志比 P6 快，记得同步一下 P6"}]}])
    pm.post("/api/circle/review", json={"keys": [lkey]})
    cm = _prog(pm, lkey)[0]
    assert len(calls) == 2 and cm["agent"]["source"] == "rule"  # 叫人“同步 P6”被拦：重试一次仍不过 → 依据模板
    assert "记得同步" not in cm["text"] and "同步一下" not in cm["text"]
    assert "分包" in feed_agents.COMMENT_SYSTEM and "计划工程师" in feed_agents.COMMENT_SYSTEM
    # 对分包岗位的“同步/更新 P6”命令式说法，读取时也判为失效
    assert feed_agents.sync_ask_problems("记得同步一下P6") and feed_agents.sync_ask_problems("请更新P6")
    assert feed_agents.sync_ask_problems("日志累计高于 P6 记录，待计划工程师更新") == []


# ============================================================================ A2 产值分析

def test_output_compare_is_computed_by_system():
    c = output.compare(403465.6, 379778.97)
    assert c["word"] == "高于" and c["diff"] == 23686.63 and c["pct"] == 6.2
    assert output.compare(300000, 379778.97)["word"] == "低于"
    assert output.compare(100, None) is None and output.compare(None, 100) is None and output.compare(100, 0) is None
    assert output.fmt_money(403465.6, "USD") == "40.3万 USD" and output.fmt_money(5200, "") == "5200" and output.fmt_money(None, "USD") == "—"


def test_output_check_blocks_reversed_comparison_and_raw_numbers():
    comps = {"计划": output.compare(403465.6, 379778.97)}
    allowed = "上周（09-28～10-04）产值 40.3万 USD，高于计划 38.0万 USD，差额 +2.4万 USD（+6.2%）"
    bad = "本周产值403465.6，低于计划值379778.97"
    probs = output.check_summary(bad, comps, allowed)
    assert any("低于" in p for p in probs) and any("403465.6" in p for p in probs)
    assert output.check_summary("上周产值 40.3万 USD，高于计划 2.4万 USD（+6.2%），主要来自桩基", comps, allowed) == []
    assert output.check_summary("上周产值比计划少 2.4万 USD", comps, allowed)
    assert output.check_summary("差额 -2.4万 USD", comps, allowed)  # 正负号与系统差额相反


def _records(pid: int, rows: list[tuple[str, float | None, float | None]]) -> None:
    with closing(dbmod.connect()) as conn:
        for start, planned, actual in rows:
            end = (date.fromisoformat(start) + timedelta(days=6)).isoformat()
            conn.execute("INSERT INTO output_records (project_id, period_start, period_end, planned, actual, currency, source) VALUES (?,?,?,?,?,?,?)",
                         (pid, start, end, planned, actual, "USD", "record"))


def test_output_analysis_reversed_word_falls_back_to_template(env, monkeypatch):
    _setup()
    pid = env["project_id"]
    monday = date.today() - timedelta(days=date.today().weekday())
    last, prev = (monday - timedelta(days=7)).isoformat(), (monday - timedelta(days=14)).isoformat()
    _records(pid, [(prev, 350000.0, 360000.0), (last, 379778.97, 403465.6)])
    bad = {"summary": "本周产值403465.6，低于计划值379778.97", "drivers": [], "actions": []}
    calls = _bad_call(monkeypatch, "output", "output.analyze", [bad])
    with closing(dbmod.connect()) as conn:
        out = output.analyze(conn, pid, None)
    assert len(calls) == 2 and out["source"] == "template"
    s = out["summary"]
    assert "高于计划" in s and "低于" not in s and "403465.6" not in s and "40.3万 USD" in s
    assert out["period_label"].startswith("上周") and s.startswith("上周")  # 与卡片“上周产值”同一周期
    assert out["week"]["start"] == last
    # 说对了就采用模型文字（模型只负责解释原因）
    good = {"summary": "上周产值 40.3万 USD，高于计划 2.4万 USD，主要来自桩基", "drivers": [], "actions": []}
    calls = _bad_call(monkeypatch, "output", "output.analyze", [good])
    with closing(dbmod.connect()) as conn:
        out = output.analyze(conn, pid, None)
    assert len(calls) == 1 and out["source"] == "ai" and out["summary"] == good["summary"]


# ============================================================================ A3 预警原因分析

def test_alert_explanation_snapshot_marks_stale_when_judgement_changes(env, monkeypatch):
    _setup_site(env)
    aid = _add_alert(env["project_id"])
    _bad_call(monkeypatch, "alerts", "alert.explain", [_explain_payload("A1020 滞后 8 天，已超出总浮时 8 天")])
    with closing(dbmod.connect()) as conn:
        a = alert_svc.explain(conn, aid, None)
    sig = a["explanation"]["facts_sig"]
    assert sig["as_of"] == "2026-10-05" and sig["slip_days"] == 8.0 and sig["forecast_finish"] == "2026-10-08"
    assert a["explanation_stale"] is False and a["explanation_as_of"] == "2026-10-05"
    _bump(aid, "red", 9.0)  # 判定后来变成滞后 9 天
    with closing(dbmod.connect()) as conn:
        a = serialize_alert(conn.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone())
    assert a["explanation_stale"] is True and a["explanation_as_of"] == "2026-10-05"
    # 接口同样带出过期标记，前端据此显示“基于 X 日判定·已过期”并提供重新分析
    got = login("eng").get(f"/api/alerts/{aid}").json()
    assert got["explanation_stale"] is True


def test_old_explanation_without_snapshot_checked_against_current(env):
    _setup_site(env)
    aid = _add_alert(env["project_id"], explanation={"judgement": "A1020 滞后 7 天", "causes": [], "impact": "", "measures": [], "data_gaps": []})
    with closing(dbmod.connect()) as conn:
        a = serialize_alert(conn.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone())
    assert a["explanation_stale"] is True and a["explanation_as_of"] is None


def test_alert_measures_due_and_owner_are_checked(env, monkeypatch):
    _setup_site(env)
    aid = _add_alert(env["project_id"])  # 判定日 2026-10-05
    early = {"judgement": "A1020 滞后 8 天", "causes": [], "impact": "", "data_gaps": [],
             "measures": [{"action": "增加班组", "owner": "工程主管", "due": "10-04前"}]}
    ghost = {**early, "measures": [{"action": "增加设备", "owner": "设备主管", "due": "3天内"}]}
    for bad in (early, ghost):
        calls = _bad_call(monkeypatch, "alerts", "alert.explain", [bad])
        with closing(dbmod.connect()) as conn:
            exp = alert_svc.explain(conn, aid, None)["explanation"]
        assert len(calls) == 2 and exp["source"] == "template"
        assert all(m["owner"] in alert_agent.OWNER_ROLES for m in exp["measures"])
    assert alert_agent.measure_problems({"measures": [{"action": "x", "owner": "工程主管", "due": "2026-10-04"}]}, "2026-10-05")
    assert alert_agent.measure_problems({"measures": [{"action": "x", "owner": "分包技术员", "due": "10月5日前"}]}, "2026-10-05") == []
    assert alert_agent.measure_problems({"measures": [{"action": "x", "owner": "生产经理", "due": "3天内"}]}, "2026-10-05")
    assert "工程主管" in alert_agent.SYSTEM and "判定日" in alert_agent.SYSTEM


# ============================================================================ A4 赶工方案

def _recovery_ready():
    pm, tech = _setup()
    _submit_today(tech)
    f = pm.post("/api/schedule/forecast", json={}).json()
    return pm, f


def test_recovery_blocks_money_and_uses_resources(env, monkeypatch):
    pm, f = _recovery_ready()
    code = f["critical_path"][0]["code"]
    bad = {"diagnosis": "关键路径需赶工", "measures": [{"activity_code": code, "action": "增加一个班组", "duration_factor": 0.8, "cost": "约12万元/周", "risk": "工作面受限"}]}
    calls = _bad_call(monkeypatch, "progress_agent", "progress.recovery", [bad])
    with closing(dbmod.connect()) as conn:
        out = progress_agent.recovery(conn, env["project_id"], None)
    assert len(calls) == 2 and out["source"] == "template"
    text = out["diagnosis"] + " ".join(m["cost"] + m["action"] for m in out["verified"]["measures"])
    assert "元" not in text and "USD" not in text
    assert progress_agent.money_problems("约15万元/周") and progress_agent.money_problems("$20,000") and progress_agent.money_problems("增加 2 台挖机、1 个班组") == []
    assert "人/台/班" in progress_agent.SYSTEM and "金额" in progress_agent.SYSTEM


def test_recovery_slow_claim_uses_same_threshold_as_table(env, monkeypatch):
    pm, f = _recovery_ready()
    assert f["slow_activities"] == []  # 诊断表：没有工效明显偏低的作业
    code = f["critical_path"][0]["code"]
    bad = {"diagnosis": "关键路径作业工效偏低", "measures": [{"activity_code": code, "action": "增加一个班组", "duration_factor": 0.8, "cost": "增加 1 个班组", "risk": "工作面受限"}]}
    calls = _bad_call(monkeypatch, "progress_agent", "progress.recovery", [bad])
    with closing(dbmod.connect()) as conn:
        out = progress_agent.recovery(conn, env["project_id"], None)
    assert len(calls) == 2 and out["source"] == "template" and "工效偏低" not in out["diagnosis"]
    assert progress_agent.slow_problems("A1010 工效偏低", [{"code": "A1010", "name": "3#墩桩基施工"}]) == []
    assert progress_agent.slow_problems("A2010 工效偏低", [{"code": "A1010", "name": "3#墩桩基施工"}])
    assert schedule.SLOW_FACTOR == 1.15 and f"{schedule.SLOW_FACTOR:g}" in progress_agent.build_prompt_rules()


# ============================================================================ A5 零样本不下结论

def test_brief_zero_sample_no_controlled_conclusion(env, monkeypatch):
    _setup_site(env)  # 没有任何隐患、质量问题记录
    bad = {"headline": "无未闭环安全隐患，安全状态可控", "items": [
        {"level": "info", "area": "安全", "title": "安全状态可控", "detail": "无未闭环隐患", "action": "保持"},
        {"level": "info", "area": "质量", "title": "质量总体良好", "detail": "无质量问题", "action": "保持"}]}
    calls = _bad_call(monkeypatch, "brief", "brief.daily", [bad])
    out = _gen(env)
    assert len(calls) == 2 and out["source"] == "template"
    text = brief._texts(out)
    for w in ("可控", "正常", "良好"):
        assert w not in text
    assert f"近 {brief.SAMPLE_DAYS} 天无隐患记录" in out["headline"] and brief.scope_ok(out["headline"])
    with closing(dbmod.connect()) as conn:
        k = brief.kpis(conn, env["project_id"], date.today())
        facts = brief.facts_text(conn, env["project_id"], k)
    assert "样本" in facts and "安全隐患" in facts and "质量问题" in facts


def test_brief_zero_sample_check_only_hits_empty_areas():
    zero = {"安全"}
    assert brief.zero_sample_problems("安全状态可控", zero)
    assert brief.zero_sample_problems("项目整体正常", zero)
    assert brief.zero_sample_problems("质量总体良好", zero) == []  # 质量有样本，不拦
    assert brief.zero_sample_problems("A1020 滞后 8 天", zero) == []


# ============================================================================ A6 随手记删除与“没记录≠没施工”

def _day_log(pid: int, uid: int, day: str):
    with closing(dbmod.connect()) as conn:
        return conn.execute("SELECT * FROM logs WHERE project_id=? AND author_id=? AND log_date=? AND period='day' AND mode='ai'", (pid, uid, day)).fetchone()


def test_delete_last_note_clears_auto_draft(env):
    _setup()
    tech = login("tech1")
    n = tech.post("/api/notes", json={"text": "3#墩桩基施工完成2根"}).json()
    today = date.today().isoformat()
    lg = _day_log(env["project_id"], env["users"]["tech1"], today)
    assert lg and lg["drafted_at"] and "桩基" in lg["content_json"]
    assert tech.delete(f"/api/notes/{n['id']}").status_code == 200
    lg = _day_log(env["project_id"], env["users"]["tech1"], today)
    c = dbmod.loads(lg["content_json"], {})
    assert c["summary"] == "" and c["works"] == [] and c["progress"] == []
    assert lg["drafted_at"] is None and not (lg["voice_text"] or "")


def test_delete_one_note_reorganizes_from_remaining(env):
    _setup()
    tech = login("tech1")
    a = tech.post("/api/notes", json={"text": "3#墩桩基施工完成2根"}).json()
    tech.post("/api/notes", json={"text": "路基土方开挖继续"}).json()
    assert tech.delete(f"/api/notes/{a['id']}").status_code == 200
    lg = _day_log(env["project_id"], env["users"]["tech1"], date.today().isoformat())
    assert "桩基" not in (lg["voice_text"] or "") and "路基" in lg["voice_text"]
    c = dbmod.loads(lg["content_json"], {})
    assert lg["drafted_at"] and any("路基" in w["desc"] for w in c["works"])
    assert all("桩基" not in w["desc"] for w in c["works"]) and "桩基" not in c["summary"]  # 已删随手记的内容不再出现（次日计划取自 PLAN 不算）


def test_log_draft_never_infers_no_work_from_missing_records():
    c = log_agent.empty_content()
    c["summary"] = "今日无有效施工记录，未进行实际作业"
    c["notes"] = "现场未施工"
    out = log_agent.scrub_inferred(c, "")
    assert out["summary"] == "今日无有效施工记录" and out["notes"] == ""
    c3 = {**log_agent.empty_content(), "summary": "现场未施工"}
    assert log_agent.scrub_inferred(c3, "")["summary"] == "当日无记录"
    # 口述明确说了停工/未施工，就照写
    c2 = {**log_agent.empty_content(), "summary": "下雨停工，未进行实际作业"}
    assert log_agent.scrub_inferred(c2, "今天下雨停工")["summary"] == "下雨停工，未进行实际作业"
    assert "无记录" in log_agent.SYSTEM and "未施工" in log_agent.SYSTEM and "无记录" in log_agent.WEEK_SYSTEM
    # 中间夹作业编码/名称的推断写法也要删（验收副本里周汇总出现过“未进行RD110等计划作业”）
    c4 = {**log_agent.empty_content(), "summary": "本周无有效施工记录，现场状态正常，未进行RD110等计划作业。"}
    assert log_agent.scrub_inferred(c4, "现场一切正常，无具体施工进展记录。")["summary"] == "本周无有效施工记录，现场状态正常"
    c5 = {**log_agent.empty_content(), "summary": "K1+300未开展路基填筑作业"}
    assert log_agent.scrub_inferred(c5, "")["summary"] == "当日无记录"
    c6 = {**log_agent.empty_content(), "summary": "完成桩基施工，未进行隐蔽验收"}  # 不是“没施工”，不删
    assert log_agent.scrub_inferred(c6, "")["summary"] == "完成桩基施工，未进行隐蔽验收"


# ============================================================================ 复核问题回归（2/3/4/5/6/7/8）

def test_sync_ask_only_progress_view_and_needs_p6_object():
    """只拦“叫人同步/更新 P6（进度计划）”的命令式说法；整改、描述句（待计划工程师更新P6）不拦。"""
    assert feed_agents.sync_ask_problems("请及时更新整改情况") == []
    assert feed_agents.sync_ask_problems("需要更新安全台账") == []
    assert feed_agents.sync_ask_problems("待计划工程师更新P6") == []
    assert feed_agents.sync_ask_problems("日志累计高于 P6 记录，待计划工程师更新P6") == []
    assert feed_agents.sync_ask_problems("主要是P6未更新") == []
    assert feed_agents.sync_ask_problems("请及时更新P6")
    assert feed_agents.sync_ask_problems("记得把进度计划更新一下")
    assert feed_agents.sync_ask_problems("同步到 P6")


def test_feed_safety_comment_asking_rectification_update_is_kept(env, monkeypatch):
    """“请及时更新整改情况”是安全点评的正常说法：同步 P6 校验只看进度视角，不因此重试/换模板。"""
    _setup_site(env)
    calls = _bad_call(monkeypatch, "feed_agents", "feed.comment",
                      [{"comments": [{"view": "safety", "text": "请及时更新整改情况"}, {"view": "progress", "text": "A1020 钢筋绑扎继续"}]}])
    post = {"kind": "note", "time": "2026-10-06 09:00", "text": "临边防护缺失", "project_id": env["project_id"], "key": "note:1"}
    by_view = {"safety": [{"text": "临边防护缺失，整改中"}], "progress": [{"text": "A1020 3#墩承台钢筋绑扎"}]}
    out, _run, source = feed_agents._write(post, by_view, None, [])
    assert len(calls) == 1 and source == "ai" and out["safety"] == "请及时更新整改情况"


def test_secondary_alert_explanation_not_stale_right_after_generation(env, monkeypatch):
    """同一作业多条预警：次要预警的分析按它自己的判定做快照与校验，刚生成不能就“已过期”。"""
    _setup_site(env)
    pid = env["project_id"]
    _add_alert(pid)                                  # 主预警：behind 滞后 8 天
    aid2 = _add_alert(pid, rule="late_start", slip=3.0, level="orange")  # 次要预警：应开未开 3 天
    calls = _bad_call(monkeypatch, "alerts", "alert.explain", [_explain_payload("A1020 应开未开，滞后 3 天")])
    with closing(dbmod.connect()) as conn:
        a = alert_svc.explain(conn, aid2, None)
    assert len(calls) == 1 and a["explanation"]["source"] == "ai"
    assert a["explanation"]["facts_sig"]["slip_days"] == 3.0
    assert a["explanation_stale"] is False


def _tech_notes(env, *texts):
    _setup()
    tech = login("tech1")
    ids = [tech.post("/api/notes", json={"text": t}).json()["id"] for t in texts]
    return tech, ids, _day_log(env["project_id"], env["users"]["tech1"], date.today().isoformat())


def test_delete_note_keeps_human_edited_draft(env):
    tech, (a, _b), lg = _tech_notes(env, "3#墩桩基施工完成2根", "路基土方开挖继续")
    c = dbmod.loads(lg["content_json"], {})
    c["summary"] = "技术员手改：桩基完成2根，路基开挖继续"
    with closing(dbmod.connect()) as conn:  # 技术员在草稿上做过修改（与 AI 草稿不同）
        conn.execute("UPDATE logs SET content_json=? WHERE id=?", (dbmod.dumps(c), lg["id"]))
    r = tech.delete(f"/api/notes/{a}")
    assert r.status_code == 200 and "修改" in (r.json().get("hint") or "")
    after = _day_log(env["project_id"], env["users"]["tech1"], date.today().isoformat())
    assert dbmod.loads(after["content_json"], {})["summary"] == c["summary"]  # 人工修改不被抹掉
    assert after["drafted_at"] and "桩基" not in (after["voice_text"] or "")  # 依据里只去掉已删的这条随手记


def test_delete_note_substring_of_dictation_does_not_clear(env):
    """技术员自己口述起草的日志（非随手记整理），口述里恰好含随手记文字的子串，也不能清空。"""
    tech, (a,), lg = _tech_notes(env, "桩基")
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE logs SET auto=0, voice_text=? WHERE id=?", ("3#墩桩基施工完成2根，路基开挖继续", lg["id"]))
    assert tech.delete(f"/api/notes/{a}").status_code == 200
    after = _day_log(env["project_id"], env["users"]["tech1"], date.today().isoformat())
    assert after["content_json"] == lg["content_json"] and after["voice_text"] == "3#墩桩基施工完成2根，路基开挖继续"


def test_measure_due_and_owner_edge_cases():
    ok = lambda owner, due, as_of="2026-10-05": alert_agent.measure_problems({"measures": [{"action": "x", "owner": owner, "due": due}]}, as_of)  # noqa: E731
    assert ok("工程主管", "2-3天内") == []                       # 不是 2月3日
    assert ok("工程主管", "1月5日前", "2026-12-28") == []         # 跨年：次年 1 月 5 日
    assert ok("工程主管", "2026-12-20", "2026-12-28")            # 明确早于判定日仍拦
    assert ok("工程主管", "10-04前")                             # 原有：早于判定日
    assert ok("工程主管", "9月30日前")                           # 不是跨年（次年 9 月远超半年），仍是早于判定日
    assert ok("工程主管牵头，分包技术员配合", "3天内") == []
    assert ok("工程主管（牵头）", "3天内") == []
    assert ok("设备主管", "3天内") and ok("生产经理", "3天内")


def test_brief_zero_sample_progress_clause_not_general():
    zero = {"安全"}
    assert brief.zero_sample_problems("关键路径整体可控", zero) == []
    assert brief.zero_sample_problems("项目进度总体平稳", zero) == []
    assert brief.zero_sample_problems("里程碑总体可控", zero) == []
    assert brief.zero_sample_problems("项目整体正常", zero)
    assert brief.zero_sample_problems("进度、安全总体可控", zero)


def test_output_actions_not_checked_for_comparison_words(env, monkeypatch):
    comps = {"计划": output.compare(403465.6, 379778.97), "前一周": output.compare(403465.6, 360000.0)}
    allowed = "上周产值 40.3万 USD，高于计划 38.0万 USD"
    assert output.check_summary("上周产值 40.3万 USD，高于计划，个别作业未完成计划，钢筋不足", comps, allowed) == []
    assert output.check_summary("上周产值未达计划", comps, allowed)
    assert output.check_summary("上周产值比前一周少", comps, allowed)
    _setup()
    pid = env["project_id"]
    monday = date.today() - timedelta(days=date.today().weekday())
    _records(pid, [((monday - timedelta(days=14)).isoformat(), 350000.0, 360000.0), ((monday - timedelta(days=7)).isoformat(), 379778.97, 403465.6)])
    good = {"summary": "上周产值 40.3万 USD，高于计划 2.4万 USD，主要来自桩基", "drivers": [],
            "actions": ["督促未完成计划的作业加快进度", "针对钢筋不足提前采购"]}
    calls = _bad_call(monkeypatch, "output", "output.analyze", [good])
    with closing(dbmod.connect()) as conn:
        out = output.analyze(conn, pid, None)
    assert len(calls) == 1 and out["source"] == "ai" and out["actions"] == good["actions"]


def test_slow_claim_negation_and_code_like_words():
    slow = [{"code": "A1010", "name": "3#墩桩基施工"}]
    acts = [{"code": "A1010", "name": "3#墩桩基施工"}, {"code": "A1020", "name": "3#墩承台钢筋绑扎"}]
    # “因无材料导致……工效偏低”是在说偏低，不是“没有偏低”
    assert progress_agent.slow_problems("因无材料导致A1010工效偏低", slow, acts) == []
    assert progress_agent.slow_problems("因无材料导致A1010工效偏低", [], acts)
    # 真正的否定说法紧贴“工效”
    assert progress_agent.slow_problems("无明显工效偏低", [], acts) == []
    assert progress_agent.slow_problems("没有工效偏低的作业", slow, acts)
    # 设备型号 PC200 不是作业代码
    assert progress_agent.slow_problems("A1010 PC200挖机工效偏低", slow, acts) == []
    assert progress_agent.slow_problems("A1020 工效偏低", slow, acts)


def test_new_note_does_not_overwrite_human_edited_draft(env):
    """技术员手改过当日自动草稿后，再记一条随手记：后台整理不覆盖人工修改，只提示“有 N 条新随手记未并入”。"""
    tech, (_a,), lg = _tech_notes(env, "3#墩桩基施工完成2根")
    c = dbmod.loads(lg["content_json"], {})
    c["summary"] = "技术员手改：桩基完成2根"
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE logs SET content_json=? WHERE id=?", (dbmod.dumps(c), lg["id"]))
    tech.post("/api/notes", json={"text": "下午路基土方开挖800方"})
    after = _day_log(env["project_id"], env["users"]["tech1"], date.today().isoformat())
    assert dbmod.loads(after["content_json"], {})["summary"] == c["summary"]      # 人工修改保留
    assert "路基" not in (after["voice_text"] or "")                              # 依据仍是草稿当时的随手记
    v = tech.get(f"/api/logs/{after['id']}").json()
    assert v["pending_notes"] == 1                                                # 界面据此提示“1 条新随手记未并入”


def test_reorganize_on_request_merges_pending_notes(env):
    """技术员点“重新整理（会覆盖你的修改）”= 显式确认：强制按全部随手记重新起草，提示清零。"""
    tech, (_a,), lg = _tech_notes(env, "3#墩桩基施工完成2根")
    c = dbmod.loads(lg["content_json"], {})
    c["summary"] = "技术员手改"
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE logs SET content_json=? WHERE id=?", (dbmod.dumps(c), lg["id"]))
    tech.post("/api/notes", json={"text": "下午路基土方开挖800方"})
    v = tech.post("/api/logs/organize", json={"day": date.today().isoformat()}).json()
    assert "路基" in (v["voice_text"] or "") and v["pending_notes"] == 0


def test_unedited_draft_still_reorganizes_on_new_note(env):
    """没有人工修改的草稿照常随新随手记重新整理（原行为不变）。"""
    tech, (_a,), _lg = _tech_notes(env, "3#墩桩基施工完成2根")
    tech.post("/api/notes", json={"text": "下午路基土方开挖800方"})
    after = _day_log(env["project_id"], env["users"]["tech1"], date.today().isoformat())
    assert "路基" in (after["voice_text"] or "")
    assert tech.get(f"/api/logs/{after['id']}").json()["pending_notes"] == 0
