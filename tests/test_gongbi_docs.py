"""工笔文书（方案 2026-10-08）：索赔通知时效、提醒、文书注册表（weekly_en / monthly_en / notice_of_claim / delay_brief）、版本与冻结、项目经理口径。"""
from datetime import date, timedelta

import pytest
from conftest import login
from test_site import D, _world

from app import db as dbmod
from app.ai import docs_registry as reg
from app.ai import report_agent
from app.services import claim_notice as cn
from app.services import gongbi_docs as gd
from app.services import todo as todo_svc


def _eq_key(c):
    return next(e["key"] for e in c.get("/api/claims/events").json()["events"] if e["cat"] == "equipment")


def _user(env, name):
    conn = dbmod.connect()
    u = dbmod.one(conn, "SELECT * FROM users WHERE id=?", (env["users"][name],))
    conn.close()
    return u


# ----------------------------------------------------------------------------- 通知时效

def test_deadline_and_state_flow(env):
    _world(env)
    pm = login("pm")
    key = _eq_key(pm)
    r = pm.get("/api/claims/notices").json()
    n = r["notices"][key]
    assert r["days"] == 28 and n["aware_date"] == D(4) and n["deadline"] == (date.fromisoformat(D(4)) + timedelta(days=28)).isoformat()
    assert n["status"] == "pending" and n["remain"] == 24 and n["label"] == "剩 24 天"
    # 不发必须填原因
    assert pm.put("/api/claims/notices", json={"key": key, "status": "waived"}).status_code == 400
    w = pm.put("/api/claims/notices", json={"key": key, "status": "waived", "reason": "商务判断属承包商自身原因"}).json()
    assert w["status"] == "waived" and w["remain"] is None and w["label"] == "不发"
    # 知悉日可改（留痕），期限随之重算；不能早于事件起始日
    assert pm.put("/api/claims/notices", json={"key": key, "aware_date": D(10)}).status_code == 400
    a = pm.put("/api/claims/notices", json={"key": key, "aware_date": D(3), "note": "监理书面确认日"}).json()
    assert a["aware_date"] == D(3) and a["deadline"] == (date.fromisoformat(D(3)) + timedelta(days=28)).isoformat() and not a["aware_default"]
    back = pm.put("/api/claims/notices", json={"key": key, "status": "pending"}).json()
    assert back["status"] == "pending" and back["remain"] == 25
    conn = dbmod.connect()
    acts = [x["action"] for x in dbmod.all_(conn, "SELECT action FROM audit WHERE target=? ORDER BY id", (f"claim:{key}",))]
    conn.close()
    assert acts == ["claim.notice.waived", "claim.notice.aware", "claim.notice.pending"]
    # 通知天数是项目规则：只有管理员能改
    assert pm.put("/api/claims/notice-rule", json={"days": 14}).status_code == 403
    assert login("admin").put("/api/claims/notice-rule", json={"days": 14, "basis": "合同专用条件"}).json()["days"] == 14
    assert pm.get("/api/claims/notices").json()["notices"][key]["remain"] == 11


def test_overdue_stays_pending(env):
    _world(env)
    login("admin").put("/api/claims/notice-rule", json={"days": 2})
    pm = login("pm")
    n = pm.get("/api/claims/notices").json()["notices"][_eq_key(pm)]
    assert n["status"] == "pending" and n["remain"] == -2 and n["label"] == "已过期 2 天" and n["tone"] == "red"


def test_reminders_day_21_and_25_not_to_subs(env):
    _world(env)
    pm = login("pm")
    key = _eq_key(pm)
    aware = date.fromisoformat(D(4))
    conn = dbmod.connect()
    users = {k: _user(env, k) for k in ("eng", "pm", "tech1", "sa_a", "ss")}

    def got(name, day):
        return [i for i in todo_svc.build(conn, users[name], aware + timedelta(days=day))["items"] if i["source"] == "claim_notice" and key in i["id"]]
    assert not got("eng", 20) and not got("pm", 20)
    e21 = got("eng", 21)
    assert e21 and "剩 7 天" in e21[0]["title"] and e21[0]["action_route"] == "claims"
    assert not got("pm", 21) and not got("pm", 24)
    p25 = got("pm", 25)
    assert p25 and "剩 3 天" in p25[0]["title"] and got("eng", 25)
    assert "金额" not in p25[0]["reason"] and "产值" not in p25[0]["title"] + p25[0]["reason"]
    for who in ("tech1", "sa_a", "ss"):
        assert not got(who, 25) and not got(who, 40)
    # 过期仍待处理：继续提醒（标红）；登记“已发出”后不再提醒
    assert got("pm", 30)[0]["overdue"] is True
    conn.close()
    pm.put("/api/claims/notices", json={"key": key, "status": "sent"})
    conn = dbmod.connect()
    assert not got("pm", 30) and not got("eng", 30)
    conn.close()


def test_subs_cannot_see_notices(env):
    _world(env)
    for who in ("tech1", "sa_a"):
        c = login(who)
        assert c.get("/api/claims/notices").status_code == 403
        assert c.post("/api/gongbi/docs", json={"kind": "notice_of_claim", "ref": "x"}).status_code == 403


# ----------------------------------------------------------------------------- 文书

def test_number_guard_thousands_and_en_rules():
    facts = "Concrete 1234.5 m3; 12 crew; 2026-09-01"
    assert reg.check("weekly_en", {"summary": "Placed 1,234.5 m3 of concrete with 12 crew on 1 September 2026."}, facts) == []
    bad = reg.check("weekly_en", {"summary": "Placed 1,300 m3 with three crews, USD 5,000."}, facts)
    kinds = {x["issue"] for x in bad}
    assert "数字未在事实中找到" in kinds and "英文数字词（应写阿拉伯数字）" in kinds and "出现金额字样" in kinds
    assert report_agent.number_guard({"x": "1234.5"}, facts) == []


def test_catalogue_lists_all_types(env):
    _world(env)
    t = {x["kind"]: x for x in login("pm").get("/api/gongbi/types").json()["types"]}
    assert {"week", "month", "weekly_en", "monthly_en", "notice_of_claim", "delay_brief"} <= t.keys()
    assert t["week"]["legacy"] and t["notice_of_claim"]["outward"] and t["notice_of_claim"]["can_confirm"]
    te = {x["kind"]: x for x in login("eng").get("/api/gongbi/types").json()["types"]}
    assert te["notice_of_claim"]["can_draft"] and te["notice_of_claim"]["can_confirm"] is False


def test_weekly_en_no_output_and_pm_scope(env):
    _world(env)
    for who in ("eng", "pm"):
        c = login(who)
        d = c.post("/api/gongbi/docs", json={"kind": "weekly_en", "ref": D(3)}).json()
        assert d["kind"] == "weekly_en" and d["lang"] == "en" and d["status"] == "draft" and d["version"] >= 1
        blob = str(d)
        assert "产值" not in blob and "output value" not in blob.lower() and "value_self" not in blob and "approved_total" not in blob
        assert "张工" not in blob and "李工" not in blob      # 不出现人名
        keys = [s["key"] for s in d["template"]["sections"]]
        assert "output" not in keys and "progress" in d["tables"]
        assert c.get(f"/api/gongbi/docs/{d['id']}/docx").status_code == 200


def test_pm_never_sees_output_in_any_doc(env):
    _world(env)
    pm = login("pm")
    key = _eq_key(pm)
    for kind, ref in (("weekly_en", D(3)), ("monthly_en", D(3)), ("notice_of_claim", key), ("delay_brief", key)):
        d = pm.post("/api/gongbi/docs", json={"kind": kind, "ref": ref}).json()
        assert "id" in d, d
        assert "产值" not in str(d) and "value_approved" not in str(d)
    # 叙述里被人写进的产值句子：核对标出（所有岗位一致）；项目经理只读产值（RJ 10-10），看到与工程主管同一份文字，不再单独裁剪
    conn = dbmod.connect()
    did = dbmod.one(conn, "SELECT id FROM gongbi_docs WHERE kind='delay_brief'")["id"]
    conn.close()
    e = login("eng").put(f"/api/gongbi/docs/{did}", json={"narrative": {"event": "设备故障。本周产值下降。"}}).json()
    assert any(x["issue"] == "出现产值字样" for x in e["check"])
    assert pm.get(f"/api/gongbi/docs/{did}").json()["narrative"]["event"] == e["narrative"]["event"]


def test_notice_of_claim_no_amounts_and_frozen_versions(env):
    _world(env)
    eng, pm = login("eng"), login("pm")
    key = _eq_key(pm)
    d = eng.post("/api/gongbi/docs", json={"kind": "notice_of_claim", "ref": key}).json()
    text = str(d["narrative"])
    assert "20.2.1" in text and "3#墩桩基施工" in text and not reg._MONEY.search(text)
    assert d["notice"]["deadline"] and d["outward"] and d["can_confirm"] is False     # 工程主管起草，项目经理确认
    # 人工改写里写了金额 → 核对提示；工程主管不能确认
    e = eng.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": {"basis": "We claim USD 20,000."}}).json()
    assert any(x["issue"] == "出现金额字样" for x in e["check"])
    assert eng.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": {}, "confirm": True}).status_code == 403
    c = pm.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": {"basis": d["narrative"]["basis"]}, "confirm": True}).json()
    assert c["status"] == "confirmed" and c["can_edit"] is False
    assert pm.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": {"basis": "x"}}).status_code == 400   # 冻结
    d2 = eng.post("/api/gongbi/docs", json={"kind": "notice_of_claim", "ref": key}).json()
    assert d2["version"] == 2 and d2["revision_of"] == d["id"]
    # 系统不发送：登记已发出由人完成，可关联通知函
    s = pm.put("/api/claims/notices", json={"key": key, "status": "sent", "letter_id": d["id"]}).json()
    assert s["status"] == "sent" and s["letter_id"] == d["id"]
    assert pm.get("/api/claims/notices").json()["notices"][key]["letter"]["id"] == d2["id"]
    assert pm.get(f"/api/gongbi/docs/{d['id']}/docx").status_code == 200


def test_delay_brief_reference_only(env):
    _world(env)
    eng = login("eng")
    d = eng.post("/api/gongbi/docs", json={"kind": "delay_brief", "ref": _eq_key(eng)}).json()
    assert d["lang"] == "zh" and "参考推演" in d["narrative"]["milestones"]
    assert "affected" in d["tables"] and "est_loss_days" not in str(d) and "loss_days" not in str(d)


def test_guard_failure_falls_back_to_rule(env, monkeypatch):
    _world(env)
    eng = login("eng")
    from app.ai.client import AIResult

    def fake(kind, **kw):
        data = kw["validate"]({**reg.rule("delay_brief", {"event": {"cat_name": "x", "date_from": "", "date_to": "", "log_days": 1, "issues": []}, "notice": {
            "aware_date": "", "deadline": "", "clause": "", "days": 1, "status_name": ""}, "affected": []}), "event": "设备故障导致停工 99 天。"})
        return AIResult(data=data, run_id=None, provider="openai", model="m", latency_ms=1, repaired=False, degraded="")
    monkeypatch.setattr(gd, "call_json", fake)
    d = eng.post("/api/gongbi/docs", json={"kind": "delay_brief", "ref": _eq_key(eng)}).json()
    assert "99" not in d["narrative"]["event"] and "event" in d["fallback"] and "换成规则版" in d["note"] and "event" not in d["note"]   # 提示写栏目标题，不露字段名（验收 I17）


def test_leader_sees_confirmed_only_and_subs_blocked(env):
    _world(env)
    eng, boss = login("eng"), login("boss")
    d = eng.post("/api/gongbi/docs", json={"kind": "weekly_en", "ref": D(3)}).json()
    assert boss.get("/api/gongbi/docs").json() == [] and boss.get(f"/api/gongbi/docs/{d['id']}").status_code == 404
    assert login("tech1").get("/api/gongbi/docs").status_code == 403
    with pytest.raises(ValueError):
        gd._type("week")


def test_notice_rule_has_admin_ui():
    """索赔通知天数在“项目与规则”里有输入项（原先只有接口、界面改不了）。"""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "app" / "static" / "views.js").read_text(encoding="utf-8")
    assert '"/api/claims/notice-rule"' in js and "saveClaimRule" in js


def test_fallback_note_uses_section_titles_and_rewrites_old_notes():
    from app.services import gongbi_docs as gd
    n = gd.fallback_note("work_contact", ["fact"])
    assert "fact" not in n and "“" in n
    old = {"note": "以下栏目数字核对未通过，已退回规则版：fact", "fallback": ["fact"]}
    assert gd._note("work_contact", old) == n
