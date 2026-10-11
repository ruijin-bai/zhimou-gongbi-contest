"""T07 “AI建议｜依据｜人工决定”核对表：周进度确认、日志预审、小票识别三处的后端行为与字段级采纳率。"""
import json
from datetime import timedelta

from conftest import backdate, login, sample_xer
from test_roles import _add, _team
from test_tickets import _confirm, _draft
from test_weekly import LAST_MON, _note_on, _weekly_project

from app import db as dbmod
from app.services import review as review_svc


def _week_ready(env, who="tech1", extra_row=False):
    """每日日志三天（A1010 完成 2、3、1 → 合计 6）→ 周确认 AI 草稿。extra_row：AI 草稿里再多一个每日记录没有数量的作业 A1020。"""
    _weekly_project(env)
    pm = login("pm")
    assert pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(data_date=(LAST_MON - timedelta(days=1)).isoformat()),
                                                     "application/octet-stream")}).status_code == 200
    tech = login(who)
    _note_on(tech, LAST_MON, "3#墩桩基施工今天完成2根，累计完成30%。钢筋工12人")
    _note_on(tech, LAST_MON + timedelta(days=1), "3#墩桩基施工完成3根，累计完成45%。")
    _note_on(tech, LAST_MON + timedelta(days=2), "3#墩桩基施工完成1根，累计完成50%")
    wk = tech.post("/api/logs", json={"period": "week", "log_date": (LAST_MON + timedelta(days=3)).isoformat()}).json()
    w = tech.post(f"/api/logs/{wk['id']}/draft", json={"voice_text": ""}).json()
    if extra_row:
        conn = dbmod.connect()
        row = conn.execute("SELECT content_json, ai_draft_json FROM logs WHERE id=?", (wk["id"],)).fetchone()
        for col in ("content_json", "ai_draft_json"):
            c = json.loads(row[col])
            c["progress"].append({"activity_code": "A1020", "qty": 5.0, "unit": "m3", "pct": 20.0, "note": "", "sources": ["V"]})
            conn.execute(f"UPDATE logs SET {col}=? WHERE id=?", (json.dumps(c, ensure_ascii=False), wk["id"]))
        conn.close()
        w = tech.get(f"/api/logs/{wk['id']}").json()
    backdate(wk["id"])
    return tech, w


def _progress_rows():
    conn = dbmod.connect()
    rows = conn.execute("SELECT activity_code, qty, unit, pct FROM log_progress WHERE voided_at IS NULL ORDER BY activity_code").fetchall()
    conn.close()
    return rows


def _fields(ref_type, ref_id, kind):
    conn = dbmod.connect()
    out = review_svc.fields_of(conn, ref_type, ref_id, kind)
    conn.close()
    return {f["key"]: f for f in out}


# ----------------------------------------------------------------------------- 周进度确认

def test_week_review_rows_have_evidence_and_flags(env):
    tech, w = _week_ready(env, extra_row=True)
    rows = {r["key"]: r for r in w["week_review"]["rows"]}
    assert list(rows) == ["A1010", "A1020"]
    a = rows["A1010"]
    assert a["ai_value"] == 6 and a["daily_sum"] == 6 and a["label"] == "3#墩桩基施工" and a["flag"] == ""
    assert [e["ref"] for e in a["evidence"]] == ["D1", "D2", "D3"] and all(e["log_id"] for e in a["evidence"])
    assert "完成 2" in a["evidence"][0]["text"] and "完成 3" in a["evidence"][1]["text"] and "完成 1" in a["evidence"][2]["text"]
    # AI 草稿数量与每日合计不一致 → 提示保留；每日记录里没有数量的作业 → 不补零，daily_sum 为 None
    b = rows["A1020"]
    assert b["ai_value"] == 5 and b["daily_sum"] is None and b["evidence"][0]["type"] == "text"
    conn = dbmod.connect()
    d = json.loads(conn.execute("SELECT ai_draft_json FROM logs WHERE id=?", (w["id"],)).fetchone()["ai_draft_json"])
    d["progress"][0]["qty"] = 9
    d["progress"][0]["sources"] = []
    conn.execute("UPDATE logs SET ai_draft_json=? WHERE id=?", (json.dumps(d, ensure_ascii=False), w["id"]))
    conn.close()
    again = {r["key"]: r for r in tech.get(f"/api/logs/{w['id']}").json()["week_review"]["rows"]}
    assert again["A1010"]["ai_value"] == 9 and "不一致" in again["A1010"]["flag"]
    # 工程主管/他人看不到作者的周确认核对行
    _team(env)
    assert login("eng").get(f"/api/logs/{w['id']}").json()["week_review"] is None


def test_week_submit_blocked_until_all_decided(env):
    tech, w = _week_ready(env, extra_row=True)
    v = w["version"]
    r = tech.post(f"/api/logs/{w['id']}/submit", json={"version": v, "decisions": [{"key": "A1010", "decision": "confirm"}]})
    assert r.status_code == 400 and "还有 1 项" in r.json()["detail"]
    r = tech.post(f"/api/logs/{w['id']}/submit", json={"version": v, "decisions": []})
    assert r.status_code == 400 and "还有 2 项" in r.json()["detail"]
    # 修改必须给数字；决定名必须合法；不属于本周的作业不接受
    bad = [[{"key": "A1010", "decision": "modify"}, {"key": "A1020", "decision": "confirm"}],
           [{"key": "A1010", "decision": "modify", "final": "abc"}, {"key": "A1020", "decision": "confirm"}],
           [{"key": "A1010", "decision": "maybe"}, {"key": "A1020", "decision": "confirm"}],
           [{"key": "A1010", "decision": "confirm"}, {"key": "A1020", "decision": "confirm"}, {"key": "ZZ", "decision": "confirm"}]]
    for dec in bad:
        assert tech.post(f"/api/logs/{w['id']}/submit", json={"version": v, "decisions": dec}).status_code == 400
    assert _progress_rows() == [] and tech.get(f"/api/logs/{w['id']}").json()["status"] == "draft"
    assert _fields("log", w["id"], "week_progress") == {}  # 被拒的提交不留任何记录


def test_week_unknown_and_reject_submit_no_quantity(env):
    tech, w = _week_ready(env, extra_row=True)
    s = tech.post(f"/api/logs/{w['id']}/submit", json={"version": w["version"], "decisions": [
        {"key": "A1010", "decision": "confirm"}, {"key": "A1020", "decision": "unknown"}]})
    assert s.status_code == 200, s.text
    assert [(r["activity_code"], r["qty"]) for r in _progress_rows()] == [("A1010", 6)]  # 无法判断的作业没有任何数量行，更没有补零
    assert [p["activity_code"] for p in s.json()["content"]["progress"]] == ["A1010"]
    f = _fields("log", w["id"], "week_progress")
    assert f["A1020"]["decision"] == "unknown" and f["A1020"]["ai_value"] == 5 and f["A1020"]["final_value"] is None
    assert f["A1010"]["decision"] == "confirm" and f["A1010"]["ai_value"] == 6 and f["A1010"]["final_value"] == 6
    # 不采纳同样不提交数量（另一份周确认，重新走一遍）
    saved = tech.get(f"/api/logs/{w['id']}").json()
    assert saved["status"] == "submitted" and saved["week_review"]["rows"][1]["saved"]["decision"] == "unknown"


def test_week_reject_and_modify(env):
    tech, w = _week_ready(env, extra_row=True)
    s = tech.post(f"/api/logs/{w['id']}/submit", json={"version": w["version"], "decisions": [
        {"key": "A1010", "decision": "modify", "final": 7}, {"key": "A1020", "decision": "reject"}]})
    assert s.status_code == 200, s.text
    assert [(r["activity_code"], r["qty"]) for r in _progress_rows()] == [("A1010", 7)]
    f = _fields("log", w["id"], "week_progress")
    assert (f["A1010"]["ai_value"], f["A1010"]["final_value"], f["A1010"]["decision"]) == (6, 7, "modify")
    assert (f["A1020"]["ai_value"], f["A1020"]["final_value"], f["A1020"]["decision"]) == (5, None, "reject")
    # 修改率随之变化：AI 草稿与最终内容不同
    assert s.json()["edit_ratio"] > 0


def test_week_log_progress_same_as_legacy_for_same_input(env):
    """同一输入：沿用原口径（不带 decisions）与全部“确认”的逐行提交，log_progress 合计一致。"""
    _add(env, "tech3", "tech", "王工", scope="3#墩")
    tech1, w1 = _week_ready(env, "tech1")
    legacy = tech1.post(f"/api/logs/{w1['id']}/submit", json={"version": w1["version"], "active_seconds": 180})
    assert legacy.status_code == 200, legacy.text
    conn = dbmod.connect()
    uid1 = env["users"]["tech1"]
    base = conn.execute("SELECT activity_code, qty, unit, pct, log_date FROM log_progress WHERE log_id=? ORDER BY activity_code", (w1["id"],)).fetchall()
    conn.close()
    tech3 = login("tech3")
    for off, text in ((0, "3#墩桩基施工今天完成2根，累计完成30%。钢筋工12人"), (1, "3#墩桩基施工完成3根，累计完成45%。"), (2, "3#墩桩基施工完成1根，累计完成50%")):
        _note_on(tech3, LAST_MON + timedelta(days=off), text)
    wk3 = tech3.post("/api/logs", json={"period": "week", "log_date": (LAST_MON + timedelta(days=3)).isoformat()}).json()
    w3 = tech3.post(f"/api/logs/{wk3['id']}/draft", json={"voice_text": ""}).json()
    backdate(wk3["id"])
    rows = [r["key"] for r in w3["week_review"]["rows"]]
    new = tech3.post(f"/api/logs/{wk3['id']}/submit", json={"version": w3["version"], "active_seconds": 180,
                                                            "decisions": [{"key": k, "decision": "confirm"} for k in rows]})
    assert new.status_code == 200, new.text
    conn = dbmod.connect()
    got = conn.execute("SELECT activity_code, qty, unit, pct, log_date FROM log_progress WHERE log_id=? ORDER BY activity_code", (wk3["id"],)).fetchall()
    conn.close()
    assert uid1 and base == got and base[0]["qty"] == 6
    assert (new.json()["content"]["progress"] == legacy.json()["content"]["progress"])


def test_week_decisions_only_for_week_logs(env):
    pm = login("pm")
    pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    tech = login("tech1")
    day = tech.post("/api/logs", json={}).json()
    d = tech.post(f"/api/logs/{day['id']}/draft", json={"voice_text": "3#墩桩基施工完成2根，累计完成30%。钢筋工5人"}).json()
    r = tech.post(f"/api/logs/{day['id']}/submit", json={"version": d["version"], "decisions": []})
    assert r.status_code == 400 and "周进度确认" in r.json()["detail"]


# ----------------------------------------------------------------------------- 日志预审

def _submitted_log_with_dq(env):
    _team(env)
    pm = login("pm")
    pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    tech = login("tech1")
    log = tech.post("/api/logs", json={}).json()
    d = tech.post(f"/api/logs/{log['id']}/draft", json={"voice_text": "3#墩桩基施工完成2根，累计完成30%。钢筋工5人"}).json()
    backdate(log["id"])
    assert tech.post(f"/api/logs/{log['id']}/submit", json={"version": d["version"], "active_seconds": 200}).status_code == 200
    dq = {"score": 70, "risk": "medium", "summary": "有待核实项", "focus": ["重复照片"], "at": "2026-01-01T00:00:00+08:00",
          "checks": [{"level": "warn", "message": "照片与 L1（2026-01-01）中的照片高度相似，疑似重复使用", "source": "审核Agent"},
                     {"level": "info", "message": "记录了施工内容但没有人员投入", "source": "审核Agent"},
                     {"level": "warn", "message": "没有标注依据，请确认是否属实", "source": "规则", "section": "progress", "index": 0}]}
    conn = dbmod.connect()
    conn.execute("UPDATE logs SET dq_json=? WHERE id=?", (json.dumps(dq, ensure_ascii=False), log["id"]))
    conn.close()
    return log["id"]


def test_dq_review_false_alarm_needs_reason_and_all_decided(env):
    lid = _submitted_log_with_dq(env)
    eng = login("eng")
    v = eng.get(f"/api/logs/{lid}").json()
    assert v["dq_review"]["reviewable"] is True and [r["key"] for r in v["dq_review"]["rows"]] == ["c0", "c1", "c2"]
    assert v["dq_review"]["rows"][0]["evidence"][1]["log_id"] == 1  # 检查结论里的 L1 可点开
    ok = [{"key": "c0", "decision": "true"}, {"key": "c1", "decision": "true"}, {"key": "c2", "decision": "false", "reason": "该行来自口述"}]
    # 误报无原因（或原因只有空白）不能保存
    for no_reason in ({"key": "c2", "decision": "false"}, {"key": "c2", "decision": "false", "reason": "   "}):
        r = eng.post(f"/api/logs/{lid}/review", json={"action": "approve", "decisions": [*ok[:2], no_reason]})
        assert r.status_code == 400 and "误报" in r.json()["detail"] and "原因" in r.json()["detail"]
    # 没有全部决定：提示剩余数
    r = eng.post(f"/api/logs/{lid}/review", json={"action": "approve", "decisions": ok[:1]})
    assert r.status_code == 400 and "还有 2 项" in r.json()["detail"]
    after = eng.get(f"/api/logs/{lid}").json()
    assert after["status"] == "submitted" and _fields("log", lid, "dq_check") == {}  # 被拒后没有任何落库
    r = eng.post(f"/api/logs/{lid}/review", json={"action": "approve", "decisions": ok})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    f = _fields("log", lid, "dq_check")
    assert f["c2"]["decision"] == "false" and f["c2"]["reason"] == "该行来自口述" and f["c2"]["final_value"] is None
    assert f["c0"]["decision"] == "true" and f["c0"]["ai_value"] == f["c0"]["final_value"]
    # 逐字段查询接口已下线（RJ 2026-10-09），字段记录仍落 review_fields；项目经理看不到日志预审
    assert eng.get(f"/api/review/fields?ref_type=log&ref_id={lid}").status_code == 404
    assert login("pm").get(f"/api/logs/{lid}").json()["dq_review"] is None


def test_dq_review_without_decisions_keeps_old_behaviour(env):
    lid = _submitted_log_with_dq(env)
    r = login("eng").post(f"/api/logs/{lid}/review", json={"action": "return", "note": "补充依据"})
    assert r.status_code == 200 and r.json()["status"] == "returned"
    r = login("eng").post(f"/api/logs/{lid}/review", json={"action": "approve", "decisions": [{"key": "zz", "decision": "true"}]})
    assert r.status_code == 400  # 已不是待审核状态，且 key 不属于本日志


# ----------------------------------------------------------------------------- 小票

def test_ticket_fields_saved_with_ai_original_and_final(env):
    _team(env)
    tech = login("tech1")
    d = _draft(tech)
    ai = d["ai_fields"]
    assert ai["qty"] is not None and ai["grade"] == "C30"
    # 缺决定：AI 识别出值的字段必须都有决定
    keys = [k for k in ("supplier", "ticket_no", "vehicle", "issued_at", "grade", "qty", "slump", "temp", "part", "receiver", "remark") if ai.get(k) is not None]
    part = [{"key": k, "decision": "confirm"} for k in keys[:-1]]
    r = tech.put(f"/api/tickets/{d['id']}", json={"fields": {}, "decisions": part})
    assert r.status_code == 400 and "还有 1 项" in r.json()["detail"]
    assert tech.get(f"/api/tickets/{d['id']}").json()["status"] == "draft"  # 没确认就不计入
    # 全部决定：qty 改成 9.5（修改），其余确认
    decs = [{"key": k, "decision": "modify", "final": 9.5} if k == "qty" else {"key": k, "decision": "confirm"} for k in keys]
    ok = tech.put(f"/api/tickets/{d['id']}", json={"fields": {"qty": 9.5}, "decisions": decs})
    assert ok.status_code == 200, ok.text
    f = _fields("ticket", d["id"], "ticket")
    assert f["qty"]["decision"] == "modify" and f["qty"]["ai_value"] == ai["qty"] and f["qty"]["final_value"] == 9.5
    assert f["grade"]["decision"] == "confirm" and f["grade"]["ai_value"] == f["grade"]["final_value"] == "C30"
    # 客户端不能把改过的值报成“确认”：以 AI 原值与最终值推导为准
    d2 = _draft(tech)
    ok2 = tech.put(f"/api/tickets/{d2['id']}", json={"fields": {"qty": 8.0}, "decisions": [{"key": k, "decision": "confirm"} for k in d2["ai_fields"] if d2["ai_fields"][k] is not None and k in keys]})
    assert ok2.status_code == 200, ok2.text
    assert _fields("ticket", d2["id"], "ticket")["qty"]["decision"] == "modify"


def test_ticket_legacy_confirm_without_decisions_still_records(env):
    _team(env)
    tech = login("tech1")
    d = _draft(tech)
    # 离线模拟的识别方量为 6~10 的整数（由图片哈希决定，随用例顺序变化）：改成 AI 值 +0.5，保证确实是“修改”
    new_qty = d["ai_fields"]["qty"] + 0.5
    _confirm(tech, d["id"], fields={"qty": new_qty})
    f = _fields("ticket", d["id"], "ticket")
    assert f["qty"]["decision"] == "modify" and f["qty"]["final_value"] == new_qty and f["grade"]["decision"] == "confirm"


# ----------------------------------------------------------------------------- 字段级采纳率

def _seed(project_id, kind, ref_id, decs):
    conn = dbmod.connect()
    review_svc.save_rows(conn, project_id, kind, "log", ref_id, [
        {"key": f"k{i}", "label": f"字段{i}", "ai": 1.0 if ai else None, "final": 1.0 if dec in ("confirm", "true") else (2.0 if dec == "modify" else None),
         "decision": dec, "reason": "说明" if dec == "false" else ""} for i, (dec, ai) in enumerate(decs)], None)
    conn.close()


def _stats(pid, kind=None):
    conn = dbmod.connect()
    try:
        return review_svc.stats(conn, pid, kind)["kinds"]
    finally:
        conn.close()


def test_field_adoption_rates_and_insufficient_sample(env):
    """字段级采纳率接口已下线（RJ 2026-10-09），统计函数保留，这里直接测口径。"""
    assert login("admin").get("/api/review/stats").status_code == 404
    pid = env["project_id"]
    wp = _stats(pid)["week_progress"]
    assert wp["enough"] is False and wp["adopt_rate"] is None and wp["modify_rate"] is None and "样本不足，尚无结论" in wp["note"]
    # 样本不足（9 个已判定字段）：只给数量，不给比例
    _seed(pid, "week_progress", 1, [("confirm", True)] * 6 + [("modify", True)] * 2 + [("reject", True)])
    wp = _stats(pid, "week_progress")["week_progress"]
    assert wp["judged"] == 9 and wp["adopt_rate"] is None and "样本不足" in wp["note"]
    # 13 个已判定：8 确认 + 3 修改 + 2 不采纳；1 个无法判断、1 个人工补填（AI 没识别）单列不进分母
    _seed(pid, "week_progress", 2, [("confirm", True)] * 2 + [("modify", True)] * 1 + [("reject", True)] + [("unknown", True), ("modify", False)])
    wp = _stats(pid, "week_progress")["week_progress"]
    assert wp["judged"] == 13 and wp["confirm"] == 8 and wp["modify"] == 3 and wp["reject"] == 2 and wp["unknown"] == 1 and wp["manual_fill"] == 1
    assert wp["enough"] is True and wp["adopt_rate"] == round(8 / 13, 4) and wp["modify_rate"] == round(3 / 13, 4) and wp["reject_rate"] == round(2 / 13, 4)
    # 日志预审：属实率/误报率
    _seed(pid, "dq_check", 3, [("true", True)] * 7 + [("false", True)] * 3)
    dq = _stats(pid, "dq_check")["dq_check"]
    assert dq["judged"] == 10 and dq["true_rate"] == 0.7 and dq["false_rate"] == 0.3

def test_review_fields_migration_is_idempotent(env):
    conn = dbmod.connect()
    conn.execute("DROP TABLE review_fields")
    conn.execute("PRAGMA user_version=7")
    conn.close()
    for _ in range(2):  # 旧库（版本 7、没有该表）启动后自动补表；重复启动不报错
        dbmod._initialized = False
        dbmod.init_db()
    conn = dbmod.connect()
    assert conn.execute("PRAGMA user_version").fetchone()["user_version"] == dbmod.SCHEMA_VERSION >= 8
    assert conn.execute("SELECT COUNT(*) n FROM review_fields").fetchone()["n"] == 0
    conn.close()
