"""T-03 AI 结论随事实失效（P0-3 AC-1、AC-2、AC-3.3）。

- 生成时在 content_json.refs 记下依据：引用的预警（编号/状态/等级/判定快照）、当前 P6 批次、计数签名、提到的作业；
- 显示时（GET，不调用 AI、不写库）按当前事实复核：引用的预警关闭或改级、P6 批次变化、计数变化、提到的作业已无未关闭预警，
  任一成立即改由系统按当前事实拼出结论并标注，要点里引用已失效预警的条目去掉；旧简报没有 refs，按提到的作业与文中数量判断；
- 出口校验：“N 项/条/处”须等于底稿同一含义的数（高风险、未闭环、超期不得互相顶替），不一致重试，仍不过用模板；
- 预警推送正文、事实底稿写明“判定日 X · P6 数据日期 Y”。
"""
import json
import re
from contextlib import closing
from datetime import date, timedelta

from conftest import login, sample_xer
from test_brief_headline import FACTS, _put_brief
from test_circle_agents import _setup_site
from test_facts import _add_alert, _bad_call, _bump, _gen, _lag_fact

from app import db as dbmod
from app.services import alerts as alert_svc
from app.services import brief
from app.services import facts as F
from app.services import findings

GOOD_ITEM = {"level": "high", "area": "进度", "title": "A1020 严重滞后", "detail": "滞后 8 天，超出总浮时 8 天", "action": "组织赶工评审"}
GOOD = {"headline": "A1020 3#墩承台钢筋绑扎严重滞后 8 天，需组织赶工", "items": [GOOD_ITEM]}
# 页面上看到的：作业代码在接口出口换成中文简称（app/services/act_names.py）
SHOWN = {"headline": "3#墩承台钢筋绑扎严重滞后 8 天，需组织赶工", "title": "3#墩承台钢筋绑扎 严重滞后"}


def _stored() -> dict:
    with closing(dbmod.connect()) as conn:
        return json.loads(conn.execute("SELECT content_json FROM briefs ORDER BY id DESC LIMIT 1").fetchone()["content_json"])


def _count(table: str) -> int:
    with closing(dbmod.connect()) as conn:
        return conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


def _set_alert(aid: int, **kw) -> None:
    with closing(dbmod.connect()) as conn:
        conn.execute(f"UPDATE alerts SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", (*kw.values(), aid))


def _hz(env, level="high", due_days=3) -> int:
    with closing(dbmod.connect()) as conn:
        return findings.create(conn, env["project_id"], "safety", "hazard", level, f"临边防护缺失（{level}）", {"measure": "按方案整改"},
                               assignee_id=env["users"]["tech1"], due_days=due_days, confirmed_by=env["users"]["ss"])


def _texts(att: dict) -> str:
    return "\n".join([att.get("headline") or ""] + [f"{i['title']} {i['detail']} {i['action']}" for i in att["items"]])


def _current_import(pid: int) -> dict:
    with closing(dbmod.connect()) as conn:
        return conn.execute("SELECT * FROM p6_imports WHERE project_id=? AND is_current=1", (pid,)).fetchone()


# ----------------------------------------------------------------------------- 1 生成时记下依据；事实没变照常显示 AI 结论

def test_refs_recorded_and_fresh_brief_kept(env, monkeypatch):
    _setup_site(env)
    aid = _add_alert(env["project_id"])
    _bad_call(monkeypatch, "brief", "brief.daily", [GOOD])
    assert _gen(env)["source"] == "ai"
    refs = _stored()["refs"]
    imp = _current_import(env["project_id"])
    assert refs["p6_import"] == imp["id"] and refs["p6_data_date"] == imp["data_date"]
    ref = next(r for r in refs["alerts"] if r["id"] == aid)
    assert ref["code"] == "A1020" and ref["status"] == "open" and ref["level"] == "red" and ref["as_of"] == "2026-10-05"
    assert "A1020" in refs["codes"]
    assert refs["counts"]["红色预警"] >= 1 and "高风险隐患" in refs["counts"] and "质量问题未闭环" in refs["counts"]
    # 刚生成、事实未变：照常显示 AI 结论，不误判过期
    att = login("pm").get("/api/todo").json()["attention"]
    assert att["headline"] == SHOWN["headline"]
    assert att["basis"]["source"].startswith("AI 生成")
    assert login("pm").get("/api/cockpit").json()["brief"]["headline"] == SHOWN["headline"]


# ----------------------------------------------------------------------------- 2 AC-1 引用的预警关闭后，结论更新，不再写“严重滞后”

def test_closed_alert_conclusion_replaced_without_ai_call(env, monkeypatch):
    _setup_site(env)
    aid = _add_alert(env["project_id"])
    _bad_call(monkeypatch, "brief", "brief.daily", [GOOD])
    _gen(env)
    runs, briefs = _count("ai_runs"), _count("briefs")
    _set_alert(aid, status="closed")
    pm = login("pm")
    att = pm.get("/api/todo").json()["attention"]
    text = _texts(att)
    assert att["headline"] and "严重滞后" not in text and "A1020" not in text
    b = att["basis"]
    assert "当前事实" in b["source"] and f"#{aid}" in b["note"] and "关闭" in b["note"]
    # 依据也换成当前事实：底稿里不再有已关闭的预警
    assert not any(f"W{aid}]" in ln or f"#{aid}，" in ln for ln in b["lines"])
    cp = pm.get("/api/cockpit").json()["brief"]
    assert cp["headline"] == att["headline"] and cp["source"] == "system" and cp["stale"]["reasons"]
    # 项目经理口径：系统结论、标注同样只含进度、质量、安全
    assert brief.scope_ok(text) and brief.scope_ok(b["note"])
    # 浏览页面不调用 AI、不写库；库里的 AI 简报原样保留（第二天调度或手动“刷新”再生成）
    assert _count("ai_runs") == runs and _count("briefs") == briefs
    assert _stored()["headline"] == GOOD["headline"]


def test_level_change_marks_stale(env, monkeypatch):
    _setup_site(env)
    aid = _add_alert(env["project_id"])
    _bad_call(monkeypatch, "brief", "brief.daily", [GOOD])
    _gen(env)
    _bump(aid, level="orange", slip=14.0)  # 改级 + 判定数字更新
    att = login("pm").get("/api/todo").json()["attention"]
    assert att["headline"] != GOOD["headline"] and f"#{aid}" in att["basis"]["note"]
    # 改级后的预警仍未关闭：系统结论按当前判定写（滞后 14 天），引用旧判定（8 天）的要点去掉
    assert not any("8 天" in f"{i['title']} {i['detail']}" for i in att["items"] if "A1020" in i["title"])


def test_p6_batch_change_marks_stale(env, monkeypatch):
    _setup_site(env)
    _add_alert(env["project_id"])
    _bad_call(monkeypatch, "brief", "brief.daily", [GOOD])
    _gen(env)
    old = _current_import(env["project_id"])["data_date"]
    new = (date.fromisoformat(old) + timedelta(days=1)).isoformat()
    assert login("eng").post("/api/p6/import", files={"file": ("plan2.xer", sample_xer(new), "application/octet-stream")}).status_code == 200
    b = login("pm").get("/api/cockpit").json()["brief"]
    assert b["source"] == "system" and b["headline"] != GOOD["headline"]
    assert f"P6 数据日期 {old} → {new}" in b["basis"]["note"]
    assert any(f"P6 数据日期 {new}" in ln for ln in b["basis"]["lines"])


# ----------------------------------------------------------------------------- 3 AC-2 计数变化：同屏同一指标只出现一个数

def test_count_change_keeps_one_number_per_metric(env, monkeypatch):
    _setup_site(env)
    _add_alert(env["project_id"])
    for lv in ("high", "high", "medium"):  # 高风险 2，未闭环 3
        _hz(env, lv)
    payload = {"headline": "A1020 滞后 8 天需赶工", "items": [
        GOOD_ITEM, {"level": "high", "area": "安全", "title": "2项高风险隐患未闭环", "detail": "未闭环隐患共 3 项", "action": "督促当日整改"}]}
    _bad_call(monkeypatch, "brief", "brief.daily", [payload])
    assert _gen(env)["source"] == "ai"
    _hz(env, "high")  # 之后又确认 1 项高风险：高风险 3，未闭环 4
    pm = login("pm")
    d = pm.get("/api/todo").json()
    att = d["attention"]
    k = pm.get("/api/cockpit").json()["kpis"]
    assert k["safety"]["high"] == 3 and k["safety"]["open"] == 4
    text = _texts(att)
    assert "2项高风险" not in text and "共 3 项" not in text
    assert "3项高风险隐患未闭环" in text and "未闭环隐患共 4 项" in text
    assert "高风险隐患 2 → 3" in att["basis"]["note"]
    # 仍成立的 AI 要点保留（A1020 预警没变）
    assert any(i["title"] == SHOWN["title"] for i in att["items"])
    # 红橙预警数：结论里的“红 x 橙 y”与概览“待确认红橙预警”是同一个数
    m = re.search(r"红(\d+) 橙(\d+)", att["headline"])
    ov = {o["label"]: o["value"] for o in d["overview"]}
    assert m and int(m[1]) + int(m[2]) == ov["待确认红橙预警"]


# ----------------------------------------------------------------------------- 4 旧简报（没有 refs）的降级规则

def test_legacy_brief_without_refs(env):
    _setup_site(env)
    aid = _add_alert(env["project_id"])
    head = "A1020 3#墩承台钢筋绑扎严重滞后，影响通车"
    _put_brief(env["project_id"], head, FACTS, items=[GOOD_ITEM])
    assert login("pm").get("/api/todo").json()["attention"]["headline"] == head.replace("A1020 ", "")  # 提到的作业仍有未关闭预警：照常显示
    _set_alert(aid, status="closed")
    att = login("pm").get("/api/todo").json()["attention"]
    assert att["headline"] and "严重滞后" not in _texts(att) and "3#墩承台钢筋绑扎严重" not in _texts(att)
    assert "3#墩承台钢筋绑扎" in att["basis"]["note"]
    # 文中数量与当前统计不一致（把未闭环数写成高风险数）同样不再显示
    _set_alert(aid, status="open")
    _put_brief(env["project_id"], "A1020 滞后 8 天需赶工", FACTS, items=[
        {"level": "high", "area": "安全", "title": "9项高风险隐患未闭环", "detail": "存在4项高风险隐患未闭环", "action": "督促整改"}])
    att = login("pm").get("/api/todo").json()["attention"]
    assert "9项高风险" not in _texts(att) and "当前事实" in att["basis"]["source"]


def test_legacy_brief_u02_numbers_follow_current_counts(env):
    """U-02 原样：旧简报（无 refs、不提作业代码）标题写 9 项高风险、正文写 4 项，当前高风险 5：改用系统结论，同屏只剩 5。"""
    _setup_site(env)
    for lv in ("high",) * 5 + ("medium",) * 5:  # 高风险 5，未闭环 10
        _hz(env, lv)
    item = {"level": "high", "area": "安全", "title": "9项高风险隐患未闭环", "detail": "存在4项高风险隐患未闭环，其中3项超期", "action": "督促整改"}
    _put_brief(env["project_id"], "安全隐患整改需加快", FACTS, items=[item])
    pm = login("pm")
    att = pm.get("/api/todo").json()["attention"]
    text = _texts(att)
    assert "9项" not in text and "4项高风险" not in text and "3项超期" not in text
    k = pm.get("/api/cockpit").json()["kpis"]["safety"]
    assert k["high"] == 5 and f"{k['high']}项高风险隐患未闭环" in text and f"未闭环隐患共 {k['open']} 项" in text
    assert "数量" in att["basis"]["note"]
    # 数量与当前统计一致的旧简报照常显示（不误判）
    ok = {**item, "title": "5项高风险隐患未闭环", "detail": "未闭环隐患共 10 项"}
    _put_brief(env["project_id"], "安全隐患整改需加快", FACTS, items=[ok])
    att = pm.get("/api/todo").json()["attention"]
    assert att["headline"] == "安全隐患整改需加快" and att["items"][0]["title"] == ok["title"]


# ----------------------------------------------------------------------------- 3b 计数签名含质控提醒；refs 含最新里程碑预测

def test_qc_reminder_count_in_signature(env, monkeypatch):
    _setup_site(env)
    _bad_call(monkeypatch, "brief", "brief.daily", [{"headline": "暂无需要拍板的进度事项", "items": []}])
    _gen(env)
    assert _stored()["refs"]["counts"]["待执行质控提醒"] >= 1
    assert login("pm").get("/api/todo").json()["attention"]["headline"] == "暂无需要拍板的进度事项"
    with closing(dbmod.connect()) as conn:  # 技术员执行了一条质控提醒
        conn.execute("UPDATE findings SET status='resolved' WHERE id=(SELECT MIN(id) FROM findings WHERE kind='qc_reminder' AND status='open')")
    att = login("pm").get("/api/todo").json()["attention"]
    assert att["headline"] != "暂无需要拍板的进度事项" and "待执行质控提醒" in att["basis"]["note"]


def test_forecast_rerun_same_result_kept_changed_result_stale(env, monkeypatch):
    from app.services import schedule
    _setup_site(env)
    _add_alert(env["project_id"])
    _bad_call(monkeypatch, "brief", "brief.daily", [GOOD])
    _gen(env)
    fc = _stored()["refs"]["forecast"]
    assert fc and fc["id"] and fc["sig"]
    # 日志提交后按同一随机种子重算：输入没变、结果相同，只换编号不算变化
    with closing(dbmod.connect()) as conn:
        schedule.forecast(conn, env["project_id"])
        assert conn.execute("SELECT MAX(id) n FROM forecasts").fetchone()["n"] != fc["id"]
    assert login("pm").get("/api/todo").json()["attention"]["headline"] == SHOWN["headline"]
    # 预测结果变了（U-01：依据里“推迟 39 天”与预测页“18 天”来自两次预测）：AI 结论过期
    with closing(dbmod.connect()) as conn:
        row = conn.execute("SELECT * FROM forecasts ORDER BY id DESC LIMIT 1").fetchone()
        r = json.loads(row["result_json"])
        r["milestones"][0]["p50"] = "2099-01-01"
        conn.execute("INSERT INTO forecasts (project_id, import_id, as_of, result_json, created_at) VALUES (?,?,?,?,?)",
                     (row["project_id"], row["import_id"], row["as_of"], json.dumps(r), dbmod.now_iso()))
    b = login("pm").get("/api/todo").json()["attention"]["basis"]
    assert "当前事实" in b["source"] and "里程碑预测已重算" in b["note"]


# ----------------------------------------------------------------------------- 5 出口校验：计数

COUNTS = {"进度": {"red": 1, "orange": 0, "yellow": 3, "red_orange": 1, "alerts": (1, 4)},
          "安全": {"open": 9, "high": 4, "overdue": 9, "resolved": 12},
          "质量": {"open": 1, "overdue": 0, "reminders": 2}}


def test_count_problems_matches_meaning_not_just_number():
    ok = lambda t: F.count_problems(t, COUNTS) == []  # noqa: E731
    # 9/4/5 的来历：AI 把未闭环（或超期）的 9 写成高风险
    assert not ok("9项高风险隐患未闭环")
    assert not ok("存在4项高风险隐患未闭环，其中3项超期")
    assert ok("4项高风险隐患未闭环") and ok("存在4项高风险隐患未闭环，其中9项超期")
    assert ok("未闭环隐患共 9 项，超期 9 项") and ok("安全隐患未闭环 9 项")
    assert ok("1项质量问题未闭环 其中超期 0 项") and not ok("质量问题未闭环 2 项")
    assert ok("待执行质控提醒 2 项") and not ok("待执行质控提醒1项")
    assert ok("红色进度预警 1 项") and not ok("黄色预警 1 条") and ok("3 条黄色预警")
    assert ok("进度预警 4 条") and not ok("进度预警 2 条")
    assert ok("3处临边防护缺失需整改")  # 找不到含义关键词的数量不核对（如照抄隐患标题）
    # 与底稿原文逐字相同的说法放行（作业层“有 N 条未关闭进度预警”）
    assert F.count_problems("A1020 有 2 条未关闭进度预警", COUNTS, extra="作业 A1020：有 2 条未关闭进度预警（#1，红色）") == []
    # 系统模板自己的数量与同一口径一致
    k = {"day": "2026-10-07", "alerts": {"red": 1, "orange": 0, "yellow": 3}, "safety": {"open": 9, "high": 4, "overdue": 9, "resolved": 12, "total": 21},
         "quality": {"open": 1, "overdue": 0, "reminders": 2}, "samples": {"安全": 3, "质量": 1}}
    t = brief.template(k, {"alerts": k["alerts"], "lag_alerts": []})
    assert F.count_problems(brief._texts(t), brief.count_facts(k)) == []


def test_count_rule_is_caller_switch_only_brief_enables():
    """check_text 还被预警分析、现场圈点评等共用：不传 counts 时不核对“N 项”，行为不变；简报传入才开启。"""
    f = _lag_fact()
    assert F.check_text("9项高风险隐患未闭环，A1020 滞后 8 天", [f]) == []
    assert any("高风险" in p for p in F.check_text("9项高风险隐患未闭环，A1020 滞后 8 天", [f], counts=COUNTS))


def test_brief_wrong_count_retries_then_template(env, monkeypatch):
    _setup_site(env)
    for lv in ("high", "high", "medium"):  # 高风险 2，未闭环 3
        _hz(env, lv)
    bad = {"headline": "安全需关注", "items": [{"level": "high", "area": "安全", "title": "3项高风险隐患未闭环", "detail": "高处作业防护不足", "action": "督促整改"}]}
    calls = _bad_call(monkeypatch, "brief", "brief.daily", [bad])
    out = _gen(env)
    assert len(calls) == 2 and out["source"] == "template" and "高风险" in out["note"]
    assert any(i["title"] == "2项高风险隐患未闭环" for i in out["items"])
    good = {**bad, "items": [{**bad["items"][0], "title": "2项高风险隐患未闭环"}]}
    calls = _bad_call(monkeypatch, "brief", "brief.daily", [good])
    out = _gen(env)
    assert len(calls) == 1 and out["source"] == "ai"
    # 提示词要求 AI 不写数量（由系统统计显示）；项目经理口径不变
    assert "不要写数量" in brief.SYSTEM and "产值" not in brief.SYSTEM.split("只输出JSON")[1]


# ----------------------------------------------------------------------------- 6 AC-3.3 预警推送与事实底稿写明判定日与 P6 数据日期

def test_alert_push_and_facts_carry_judgement_day_and_p6_date(env):
    _setup_site(env)
    pid = env["project_id"]
    aid = _add_alert(pid)
    dd = _current_import(pid)["data_date"]
    label = f"判定日 2026-10-05 · P6 数据日期 {dd}"
    with closing(dbmod.connect()) as conn:
        alert_svc.process_new(conn, [aid], None)
        rows = conn.execute("""SELECT n.title, n.body, u.role FROM notifications n JOIN users u ON u.id=n.user_id WHERE n.alert_id=?""", (aid,)).fetchall()
        fact_text = F.render(F.activity_facts(conn, pid, "A1020"))
        brief_facts = brief.facts_text(conn, pid, brief.kpis(conn, pid, date.today()))
        exp = json.loads(conn.execute("SELECT explanation_json FROM alerts WHERE id=?", (aid,)).fetchone()["explanation_json"])
    mgr = [r for r in rows if r["role"] != "tech"]
    assert mgr and all(label in r["body"] for r in mgr)
    for r in rows:
        if r["role"] == "tech":  # 技术员大白话不变：不出现判定日、P6
            assert "判定日" not in r["body"] and "P6" not in r["body"]
    assert label in fact_text and label in brief_facts
    # 简报依据：第一行写明 P6 批次，[W] 行写明该预警的判定日与 P6 数据日期
    lines = brief.basis({"facts": brief_facts, "source": "ai"})["lines"]
    assert f"P6 数据日期 {dd}" in lines[0] and any(f"[W{aid}]" in ln and label in ln for ln in lines)
    assert exp["p6_data_date"] == dd  # 预警详情的分析依据留存 P6 数据日期


# ----------------------------------------------------------------------------- 7 AC-3.4 分析完成后、推送之前再读一次状态，已不是 open 就不推送

def test_alert_closed_during_analysis_is_not_pushed(env, monkeypatch):
    """U-14：10-07 07:09 推送的 3 条黄色预警，点进去都已“关闭·触发条件已解除”。红/橙先做 AI 分析（可能数十秒），
    其间别的判定把排在后面的预警关了：推送前再读一次，已不是 open 的不推送；仍 open 的照常推送。"""
    _setup_site(env)
    pid = env["project_id"]
    red = _add_alert(pid)
    yel = _add_alert(pid, code="A2010", level="yellow", slip=3.0, tf=10.0)
    orig = alert_svc.explain

    def explain_meanwhile_closed(conn, alert_id, user_id):
        out = orig(conn, alert_id, user_id)
        conn.execute("UPDATE alerts SET status='closed' WHERE id=?", (yel,))  # 分析期间重新判定：触发条件已解除
        return out
    monkeypatch.setattr(alert_svc, "explain", explain_meanwhile_closed)
    with closing(dbmod.connect()) as conn:
        res = {r["alert_id"]: r for r in alert_svc.process_new(conn, [yel, red], None)}
        sent = {r["alert_id"]: r["n"] for r in conn.execute("SELECT alert_id, COUNT(*) n FROM notifications GROUP BY alert_id").fetchall()}
    assert sent.get(red) and not sent.get(yel)
    assert res[yel]["push"]["skipped"] and "关闭" in res[yel]["push"]["skipped"] and res[red]["push"]["recipients"]
