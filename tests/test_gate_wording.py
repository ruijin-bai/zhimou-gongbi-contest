"""放行清单在待办/消息里的口径（体检 D5）：
- 只把真缺项（受阻 + 系统核到确实缺）算作“缺”，待落实另计为“待现场确认”，与放行清单页“缺项 1 · 待落实 10”一致；
- 非关键线路且总浮时充足（≥7 天）的作业，不因“应开未开 1 天”进入“超期/高风险”；状态写“应开未开”；
- 路面结构层（水稳、沥青、底基层、基层、面层）用“路面结构层”模板，不再套“土方/基坑”。
"""
from contextlib import closing
from datetime import date

import pytest
from conftest import login, sample_xer
from test_roles import _add

from app import db as dbmod
from app.services import gate as gate_svc
from app.services import todo as todo_svc


# ------------------------------------------------------------------ 1) 缺项文字

def test_gap_text_counts_only_real_gaps():
    gt = gate_svc.gap_text
    assert gt(["前道工序已完成并验收"], 1, 10) == "缺 1 项：前道工序已完成并验收；另 10 项待现场确认"
    assert gt([], 0, 10) == "10 项待现场确认"
    assert gt(["图纸为最新版并已会审"], 1, 0) == "缺 1 项：图纸为最新版并已会审"
    assert gt(["甲", "乙", "丙"], 3, 2) == "缺 3 项：甲、乙等；另 2 项待现场确认"
    assert gt([], 0, 0) == ""


def _fake_digest(items):
    base = {"code": "PV120", "name": "K0~K2 水泥稳定碎石基层", "days_to_start": -1, "verdict": "near", "verdict_name": "临近缺项",
            "gap_n": 11, "short_n": 1, "unknown_n": 10, "blocked_n": 0, "short_labels": ["前道工序已完成并验收"],
            "first_gap": "前道工序已完成并验收", "is_critical": False, "total_float_d": 56.0}
    rows = [{**base, **i} for i in items]
    return lambda conn, user, today=None: {"count": len(rows), "blocked": 0, "has_plan": True, "items": rows}


def _ctx(env, username="eng"):
    _add(env, "eng", "eng_sup", "工程主管")
    conn = dbmod.connect()
    user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return conn, todo_svc.Ctx(conn, user, date(2026, 10, 7))


def test_todo_gate_reason_and_status(env, monkeypatch):
    monkeypatch.setattr(gate_svc, "digest", _fake_digest([
        {},                                                                              # 1 真缺 + 10 待落实
        {"code": "PV220", "short_n": 0, "short_labels": [], "unknown_n": 10, "gap_n": 10, "first_gap": "图纸为最新版并已会审"},
    ]))
    conn, c = _ctx(env)
    with closing(conn):
        out = todo_svc.src_gate(c)
    a, b = out
    assert "缺 1 项：前道工序已完成并验收；另 10 项待现场确认" in a["reason"]
    assert b["reason"].endswith("10 项待现场确认") and "缺" not in b["reason"].split("；", 1)[1]
    for i in out:
        assert "还缺" not in i["reason"] and "11 项" not in i["reason"]


def test_late_non_critical_with_float_not_urgent(env, monkeypatch):
    monkeypatch.setattr(gate_svc, "digest", _fake_digest([
        {"code": "PV120"},                                                               # 非关键、浮时 56 天、应开未开 1 天
        {"code": "C1", "is_critical": True, "total_float_d": 0.0},                       # 关键线路
        {"code": "F3", "total_float_d": 3.0},                                            # 浮时不足 7 天
        {"code": "FN", "total_float_d": None},                                           # 浮时未知：按不充足处理
        {"code": "L60", "days_to_start": -60},                                           # 晚开已超过浮时
        {"code": "BK", "verdict": "blocked", "verdict_name": "受阻", "blocked_n": 1, "days_to_start": 2},
        {"code": "N2", "days_to_start": 2},
    ]))
    conn, c = _ctx(env)
    with closing(conn):
        by = {i["id"].split(":", 1)[1]: i for i in todo_svc.src_gate(c) if i["source"] == "gate"}
    pv = by["PV120"]
    assert pv["overdue"] is False and pv["tone"] != "red" and pv["status"] == "应开未开"
    assert "应开未开 1 天" in pv["reason"]
    for code in ("C1", "F3", "FN", "L60"):
        assert by[code]["overdue"] is True and by[code]["status"] == "应开未开", code
    assert by["BK"]["status"] == "受阻" and by["BK"]["tone"] == "red"
    assert by["N2"]["status"] != "应开未开" and by["N2"]["overdue"] is False


def test_late_is_risky_helper():
    r = gate_svc.late_is_risky
    assert r(-1, False, 56) is False
    assert r(-1, False, 7) is False
    assert r(-1, False, 6.9) is True
    assert r(-1, True, 56) is True
    assert r(-1, False, None) is True
    assert r(-60, False, 56) is True
    assert r(0, True, 0) is False and r(3, True, 0) is False and r(None, True, 0) is False


@pytest.fixture()
def world(env):
    _add(env, "eng", "eng_sup", "工程主管")
    assert login("pm").post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    return env


def test_digest_carries_counts_for_wording(world):
    with closing(dbmod.connect()) as conn:
        d = gate_svc.digest(conn, conn.execute("SELECT * FROM users WHERE username='eng'").fetchone())
    assert d["items"]
    for i in d["items"]:
        assert {"short_n", "unknown_n", "blocked_n", "short_labels", "total_float_d", "is_critical"} <= set(i)
        assert i["short_n"] + i["unknown_n"] == i["gap_n"]
        assert len(i["short_labels"]) == min(i["short_n"], 3)


def test_real_todo_matches_gate_page_counts(world):
    eng = login("eng")
    page = {r["code"]: r for r in eng.get("/api/gate?days=7").json()["items"]}
    items = [i for i in eng.get("/api/todo").json()["items"] if i["source"] == "gate"]
    assert items
    for i in items:
        code = i["action_route"].split("open=")[1]
        r = page[code]
        short = r["missing_n"] + r["blocked_n"]
        assert "还缺" not in i["reason"]
        if short:
            assert f"缺 {short} 项：" in i["reason"]
        if r["unknown_n"]:
            assert f"{r['unknown_n']} 项待现场确认" in i["reason"]


def test_notify_wording_matches(world):
    with closing(dbmod.connect()) as conn:
        assert gate_svc.notify_due(conn, world["project_id"])["sent"] >= 1
        rows = conn.execute("SELECT title, body FROM notifications WHERE title LIKE '放行预警%'").fetchall()
    assert rows
    for r in rows:
        assert "未具备" not in r["title"] and "还有" not in r["title"]
        assert "项待现场确认" in r["title"] or "缺 " in r["title"]


# ------------------------------------------------------------------ 3) 路面结构层模板

def test_pavement_layers_use_pavement_template():
    rc = gate_svc.rule_classify
    for name in ("K0~K2 水泥稳定碎石底基层", "K0~K2 水泥稳定碎石基层", "K2~K4.5 水稳基层", "AC-20 沥青混凝土中面层",
                 "级配碎石底基层", "沥青下面层摊铺", "路面基层"):
        assert rc(name, "路面工程/K0-K2") == "pave", name
    assert rc("K0~K2 水泥稳定碎石基层", "路基路面 / 路基工程") == "pave"         # 名称优先于 WBS 里的“路基”
    assert rc("路基土方开挖") == "earth" and rc("路基填筑") == "earth"
    assert rc("3#墩承台混凝土浇筑") == "concrete"
    assert gate_svc.CAT_NAME["pave"] == "路面结构层" and "pave" in gate_svc.CLASSIFY_CATS
    assert "pave" in gate_svc.CLASSIFY_SYSTEM


def test_pavement_template_conditions():
    defs = gate_svc.defs_for("pave")
    keys = [d["key"] for d in defs]
    assert len(keys) == len(set(keys))
    spec = [d for d in defs if d["category"] == "pave"]
    text = " ".join(d["label"] for d in spec)
    for kw in ("下承层", "配合比", "原材料", "试验段", "摊铺", "碾压", "天气"):
        assert kw in text, kw
    assert not any(k in text for k in ("基坑", "支护", "降排水"))
    # 系统能核到的自动核（前道工序、材料、质控点沿用通用项），核不到的一律待落实
    auto = {d["key"]: d["auto"] for d in defs}
    assert auto["prev_done"] == "prev" and auto["material"] == "material"
    assert all(d["auto"] is None for d in spec)


def test_pavement_rule_overrides_cached_ai_earth(world):
    with closing(dbmod.connect()) as conn:
        pid = world["project_id"]
        conn.execute("INSERT INTO gate_class (project_id, activity_code, act_name, category, source, updated_at) VALUES (?,?,?,?, 'ai', '2026-10-01')",
                     (pid, "PV120", "K0~K2 水泥稳定碎石基层", "earth"))
        got = gate_svc.classify_all(conn, pid, [{"code": "PV120", "name": "K0~K2 水泥稳定碎石基层", "wbs_path": "路面工程 / K0-K2"}])
    assert got["PV120"] == {"category": "pave", "source": "rule"}


def test_pavement_material_keywords():
    kws = gate_svc._MAT_KW["pave"]
    assert "水泥" in kws and "沥青" in kws and "碎石" in kws


def test_pave_template_does_not_swallow_concrete_or_wbs_only_matches():
    """“面层/基层/路面”是宽词：混凝土面层仍归混凝土；只靠 WBS 路径“路面工程”不归路面模板。"""
    from app.services import gate
    assert gate.rule_classify("码头面层混凝土浇筑") == "concrete"
    assert gate.rule_classify("堆场混凝土面层") == "concrete"
    assert gate.rule_classify("水泥混凝土路面浇筑") == "concrete"
    assert gate.rule_classify("K0~K2 沥青混凝土下面层") == "pave"            # 沥青混凝土仍是路面结构层
    assert gate.rule_classify("K0~K2 水泥稳定碎石基层") == "pave"
    assert gate.rule_classify("路缘石安装", "路面工程/K0-K2") != "pave"
    assert gate.rule_classify("人行道铺装", "路面工程/K0-K2") != "pave"
