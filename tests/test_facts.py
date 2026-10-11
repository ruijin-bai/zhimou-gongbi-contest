"""各 Agent 口径统一：事实底稿、矛盾措辞/数字拦截、预警原因随判定更新、现场圈点评去重（离线 mock 模型）。"""
from contextlib import closing

from conftest import login
from test_agents import _setup, _submit_today
from test_circle_agents import _agent_comments, _key, _setup_site

from app import db as dbmod
from app.services import alerts as alert_svc
from app.services import brief, feed_agents
from app.services import facts as F

BAD_PLAN = "进度挺稳，按计划推进"


def _add_alert(pid: int, code="A1020", level="red", slip=8.0, tf=0.0, status="open", explanation=None, rule="behind") -> int:
    """直接写一条预警（判定天数与标题同源：滞后 slip，剩余浮时 tf-slip）。"""
    conn = dbmod.connect()
    try:
        imp = conn.execute("SELECT id FROM p6_imports WHERE project_id=? AND is_current=1", (pid,)).fetchone()["id"]
        name = conn.execute("SELECT name FROM activities WHERE import_id=? AND code=?", (imp, code)).fetchone()["name"]
        left = tf - slip
        detail = {"planned_pct": 70.0, "actual_pct": 30.0, "actual_source": "日报确认", "slip_days": slip, "total_float_d": tf, "float_left_d": left,
                  "critical": True, "early_start": "2026-09-01", "early_finish": "2026-09-30", "forecast_finish": "2026-10-08"}
        title = f"{code} {name}：进度滞后 {slip:g} 天" + (f"，超出总浮时 {-left:g} 天" if left < 0 else "")
        return dbmod.insert(conn, "alerts", {
            "project_id": pid, "import_id": imp, "fingerprint": f"{rule}:{code}", "activity_code": code, "activity_name": name, "level": level,
            "rule": rule, "title": title, "detail_json": dbmod.dumps(detail), "explanation_json": dbmod.dumps(explanation) if explanation else None,
            "status": status, "first_detected": dbmod.now_iso(), "last_detected": dbmod.now_iso(), "as_of": "2026-10-05"})
    finally:
        conn.close()


def _bump(aid: int, level="orange", slip=14.0) -> None:
    """预警后来升级/加重：等同于“有新偏差”。"""
    with closing(dbmod.connect()) as conn:
        d = dbmod.loads(conn.execute("SELECT detail_json FROM alerts WHERE id=?", (aid,)).fetchone()["detail_json"], {})
        d.update({"slip_days": slip, "float_left_d": d["total_float_d"] - slip})
        conn.execute("UPDATE alerts SET level=?, detail_json=? WHERE id=?", (level, dbmod.dumps(d), aid))


def _lag_fact(**kw) -> dict:
    f = {"kind": "activity", "code": "A1020", "name": "3#墩承台钢筋绑扎", "has_open_alert": True, "alert_ids": [1], "alert_id": 1, "level": "red",
         "rule": "behind", "rule_name": "进度滞后", "as_of": "2026-10-05", "slip_days": 8.0, "float_total": 0.0, "float_left": -8.0, "over_float": 8.0,
         "idle_days": None, "planned_pct": 70.0, "completion_pct": 30.0, "pct_log": 30.0, "pct_log_date": "2026-10-04", "pct_p6": 20.0,
         "p6_data_date": "2026-10-01", "early_finish": "2026-09-30", "forecast_finish": "2026-10-08", "past_finish": True, "milestones": [],
         "iterations": 400, "p6_sync_due": False}
    f.update(kw)
    return f


# ----------------------------------------------------------------------------- 出口校验

def test_check_text_blocks_contradiction_and_mismatched_numbers():
    f = _lag_fact()
    assert F.check_text("A1020 滞后 8 天，已超出总浮时 8 天，累计完成 30%", [f]) == []
    # 矛盾措辞：有未关闭预警却说按计划/挺稳
    for bad in ("A1020 按计划推进", "进度挺稳", "目前正常推进", "项目运行正常"):
        assert any("矛盾" in p for p in F.check_text(bad, [f])), bad
    assert F.check_text("没有按计划完成，进度滞后 8 天", [f]) == []  # “没按计划”不是矛盾
    assert F.check_text("今天按计划推进", [_lag_fact(has_open_alert=False, alert_ids=[], alert_id=None)]) == []  # 无预警时不拦
    # 数字不一致：滞后 7 天（标题 8 天）、浮时、百分比
    assert any("滞后天数" in p for p in F.check_text("进度滞后 7 天", [f]))
    assert any("超出总浮时" in p for p in F.check_text("滞后 8 天，超出总浮时 7 天", [f]))
    assert any("百分比" in p for p in F.check_text("累计完成 55%", [f]))
    assert F.check_text("累计完成 55%", [f], extra="日志里写累计 55%") == []
    # P6 同步提醒只在底稿标注待同步时放行
    assert any("P6" in p for p in F.check_text("P6记录还没更新，记得同步", [f]))
    assert F.check_text("P6记录还没更新，记得同步", [_lag_fact(p6_sync_due=True)]) == []


def test_milestone_probability_unknown_is_not_zero(env):
    _setup_site(env)  # 导入 P6，尚无预测
    with closing(dbmod.connect()) as conn:
        p = F.project_facts(conn, env["project_id"])
    p["milestones"] = [{"code": "M9000", "name": "合龙", "p6_date": "2026-11-01", "forecast": "2026-11-03", "p80": "2026-11-05", "on_time_prob": None}]
    text = F.render_project(p)
    assert "按期概率未知" in text and "0%" not in text
    p["milestones"][0]["on_time_prob"] = 0.0
    p["iterations"] = 400
    assert "按期概率 <1%（400 次模拟无一次按期）" in F.render_project(p) and "0%" not in F.render_project(p)  # 统一口径见 schedule.prob_text


# ----------------------------------------------------------------------------- 预警原因 / 简报

def _bad_call(monkeypatch, module, kind, payloads):
    """让指定 kind 的 mock 依次返回 payloads（用完后重复最后一个），并记录调用次数。"""
    from app.ai import client
    orig, calls = client.call_json, []

    def wrapper(k, **kw):
        if k == kind:
            payload = payloads[min(len(calls), len(payloads) - 1)]
            calls.append(payload)
            kw["mock"] = lambda: payload
        return orig(k, **kw)
    monkeypatch.setattr(f"app.services.{module}.call_json", wrapper)
    return calls


def _explain_payload(judgement: str) -> dict:
    return {"judgement": judgement, "causes": [], "impact": "", "measures": [{"action": "增加班组", "owner": "工程主管", "due": "3天内"}], "data_gaps": []}


def test_alert_explain_retries_once_then_uses_template(env, monkeypatch):
    _setup_site(env)
    aid = _add_alert(env["project_id"])
    calls = _bad_call(monkeypatch, "alerts", "alert.explain", [_explain_payload("A1020 滞后 7 天，按计划推进")])
    with closing(dbmod.connect()) as conn:
        out = alert_svc.explain(conn, aid, None)
    assert len(calls) == 2  # 矛盾 → 重试一次 → 仍矛盾
    exp = out["explanation"]
    assert exp["source"] == "template" and exp["rejected"]
    assert "滞后 8 天" in exp["judgement"] and "7" not in exp["judgement"] and "按计划" not in exp["judgement"]
    assert any("事实底稿不一致" in g for g in exp["data_gaps"])

    # 第二次改对了：采用 AI 文字
    calls = _bad_call(monkeypatch, "alerts", "alert.explain", [_explain_payload("滞后 7 天"), _explain_payload("A1020 滞后 8 天，已超出总浮时 8 天")])
    with closing(dbmod.connect()) as conn:
        exp = alert_svc.explain(conn, aid, None)["explanation"]
    assert len(calls) == 2 and exp["source"] == "ai" and "滞后 8 天" in exp["judgement"]
    assert exp["facts_sig"] == {"as_of": "2026-10-05", "slip_days": 8.0, "float_left": -8.0, "forecast_finish": "2026-10-08"}


def test_stale_explanation_is_replaced_in_decision_queue(env):
    """标题已更新到滞后 8 天，旧的 AI 分析还写滞后 7 天：首页依据不得沿用旧数字。"""
    _setup_site(env)
    aid = _add_alert(env["project_id"], explanation={"judgement": "A1020 滞后 7 天，剩余浮时 -7 天", "causes": [], "impact": "", "measures": [], "data_gaps": []})
    with closing(dbmod.connect()) as conn:
        item = next(i for i in brief.decision_queue(conn, env["project_id"]) if i["type"] == "alert" and i["id"] == aid)
        assert "滞后 8 天" in item["title"] and "7" not in item["detail"] and "8" in item["detail"]
        # 记录了生成时判定数字的新解释，数字一致则原样使用
        conn.execute("UPDATE alerts SET explanation_json=? WHERE id=?",
                     (dbmod.dumps({"judgement": "关键作业，需赶工", "facts_sig": {"slip_days": 8.0, "float_left": -8.0}}), aid))
        item = next(i for i in brief.decision_queue(conn, env["project_id"]) if i["id"] == aid)
    assert item["detail"] == "关键作业，需赶工"


def _gen(env):
    with closing(dbmod.connect()) as conn:
        return brief.generate(conn, env["project_id"], None)


def test_brief_contradicting_facts_falls_back_to_template(env, monkeypatch):
    _setup_site(env)
    _add_alert(env["project_id"])
    for bad in ({"headline": "项目运行正常，进度按计划推进", "items": []},
                {"headline": "进度需关注", "items": [{"level": "high", "area": "进度", "title": "A1020 滞后", "detail": "滞后 7 天", "action": "赶工"}]},
                {"headline": "进度需关注", "items": [{"level": "high", "area": "进度", "title": "合龙节点", "detail": "关键里程碑按期概率为35%", "action": "评审"}]}):
        calls = _bad_call(monkeypatch, "brief", "brief.daily", [bad])
        out = _gen(env)
        assert len(calls) == 2 and out["source"] == "template" and "事实底稿" in out["note"]
        assert "按计划" not in out["headline"] and any("8 天" in i["detail"] for i in out["items"])
    # 与底稿一致的简报照常采用
    good = {"headline": "A1020 滞后 8 天需赶工", "items": [{"level": "high", "area": "进度", "title": "A1020 滞后", "detail": "滞后 8 天，超出总浮时 8 天", "action": "赶工"}]}
    _bad_call(monkeypatch, "brief", "brief.daily", [good])
    out = _gen(env)
    assert out["source"] == "ai" and out["headline"] == good["headline"]


# ----------------------------------------------------------------------------- 现场圈点评

def _note(c, text="3号墩承台钢筋绑扎继续"):
    return f"note:{c.post('/api/notes', json={'text': text}).json()['id']}"


def _progress(c, key):
    return [x for x in _agent_comments(c, key) if x["agent"]["view"] == "progress"]


def test_feed_progress_comment_contradiction_blocked(env, monkeypatch):
    _setup_site(env)
    _add_alert(env["project_id"])
    t1 = login("tech1")
    key = _note(t1)
    # 1 “按计划/挺稳”与未关闭预警矛盾；数字没越过现有守卫（A1020 里的数字），仍应被拦下
    calls = _bad_call(monkeypatch, "feed_agents", "feed.comment", [{"comments": [{"view": "progress", "text": f"A1020 {BAD_PLAN}"}]}])
    t1.post("/api/circle/review", json={"keys": [key]})
    cm = _progress(t1, key)[0]
    assert len(calls) == 2 and cm["agent"]["source"] == "rule"
    assert "按计划推进" not in cm["text"] and "挺稳" not in cm["text"] and "滞后 8 天" in cm["text"]
    assert any("滞后 8 天" in e["text"] for e in cm["agent"]["evidence"])
    assert all("meta" not in e["text"] for e in cm["agent"]["evidence"])  # 内部标记不外露


def test_feed_progress_comment_number_mismatch_blocked(env, monkeypatch):
    _setup_site(env)
    aid = _add_alert(env["project_id"])
    t1 = login("tech1")
    key = _note(t1)
    # 数字 3 出现在依据里（3#墩），过得了数字守卫，但“滞后 3 天”与底稿的 8 天不符
    calls = _bad_call(monkeypatch, "feed_agents", "feed.comment", [{"comments": [{"view": "progress", "text": "3#墩承台钢筋绑扎滞后 3 天"}]}])
    t1.post("/api/circle/review", json={"keys": [key]})
    assert len(calls) == 2 and _progress(t1, key)[0]["agent"]["source"] == "rule"
    # 第二次改对：采用 AI 文字
    key2 = _note(t1, "3号墩承台钢筋绑扎又干了一天")
    _bump(aid, "orange", 11.0)  # 预警加重 → 有新信息
    calls = _bad_call(monkeypatch, "feed_agents", "feed.comment", [
        {"comments": [{"view": "progress", "text": "3#墩承台钢筋绑扎滞后 3 天"}]}, {"comments": [{"view": "progress", "text": "3#墩承台钢筋绑扎滞后 11 天，预警还没关"}]}])
    t1.post("/api/circle/review", json={"keys": [key2]})
    cm = _progress(t1, key2)[0]
    assert len(calls) == 2 and cm["agent"]["source"] == "ai" and "滞后 11 天" in cm["text"]


def test_feed_no_new_info_no_comment_and_p6_reminder_once(env):
    _setup_site(env)
    t1 = login("tech1")
    k1, k2 = _note(t1), _note(t1, "3号墩承台钢筋绑扎第二天")
    t1.post("/api/circle/review", json={"keys": [k1]})
    assert len(_progress(t1, k1)) == 1
    # 同一作业、状况与上次点评相比没有变化：不再点评
    t1.post("/api/circle/review", json={"keys": [k2]})
    assert _progress(t1, k2) == []
    # 出现新预警 → 有新信息，再评一次，并带上预警事实
    aid = _add_alert(env["project_id"])
    t1.post("/api/circle/review", json={"keys": [k2]})
    cm = _progress(t1, k2)
    assert len(cm) == 1 and any("滞后 8 天" in e["text"] for e in cm[0]["agent"]["evidence"])
    # 又一条：预警状况没变 → 不评；滞后在同一档内小幅变化也不评
    k3 = _note(t1, "3号墩承台钢筋绑扎第三天")
    t1.post("/api/circle/review", json={"keys": [k3]})
    assert _progress(t1, k3) == []
    _bump(aid, "red", 8.5)
    k4 = _note(t1, "3号墩承台钢筋绑扎第四天")
    t1.post("/api/circle/review", json={"keys": [k4]})
    assert _progress(t1, k4) == []
    _bump(aid, "red", 12.0)  # 滞后跨档（新偏差）→ 再评
    k5 = _note(t1, "3号墩承台钢筋绑扎第五天")
    t1.post("/api/circle/review", json={"keys": [k5]})
    assert len(_progress(t1, k5)) == 1


def test_p6_sync_reminder_at_most_once_per_data_date(env):
    pm, tech = _setup()
    _submit_today(tech)  # 日志确认 A1010 累计 82%，P6 记录 80%（数据日期后日志更新）
    lkey = _key(pm, "log")
    pm.post("/api/circle/review", json={"keys": [lkey]})
    first = [e["text"] for c in _progress(pm, lkey) for e in c["agent"]["evidence"]]
    assert sum("P6待同步" in t for t in first) == 1
    # 同一数据日期内，该作业再有新情况（新预警）点评时，不再带“P6 待同步”
    with closing(dbmod.connect()) as conn:  # 提交日志后判定已产生 A1010 预警
        aid = conn.execute("SELECT id FROM alerts WHERE activity_code='A1010' AND status='open'").fetchone()["id"]
    _bump(aid, "red", 40.0)
    n = _note(tech, "3号墩桩基施工继续")
    pm.post("/api/circle/review", json={"keys": [n]})
    second = [e["text"] for c in _progress(pm, n) for e in c["agent"]["evidence"]]
    assert second and not any("P6待同步" in t for t in second)
    # 且 AI 即便自己写“P6 没更新”也会被拦（无待同步标注）
    with closing(dbmod.connect()) as conn:
        f = F.activity_facts(conn, env["project_id"], "A1010")
    assert any("P6" in p for p in F.check_text("P6记录还没更新，记得同步", [f]))
    assert feed_agents.PROMPT_VERSION.startswith("feed-comment-v3")
