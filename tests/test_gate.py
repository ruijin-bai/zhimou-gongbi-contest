"""放行Agent：开工/浇筑放行清单。P6 数据用 conftest 的合成 XER（开工日 = 今天 - 10 天），其余表直接写入，不依赖其他功能的行为。

合成计划相对今天：A1020 3#墩承台钢筋绑扎（今天开始，前置 A1010 完成 80%）、A3020 路基填筑（+2 天，前置 A3010 50%）、
A1030 3#墩承台混凝土浇筑（+4 天）、A2020 4#墩承台施工（+4 天）、A1040 3#墩墩身施工（+6 天）；A1010/A2010/A3010/A4010 已开工。
"""
from contextlib import closing
from datetime import date, timedelta

import pytest
from conftest import login, sample_xer
from test_roles import _add

from app import db as dbmod
from app.ai.client import AIError, AIResult
from app.services import findings as find_svc
from app.services import gate as gate_svc


@pytest.fixture()
def world(env):
    _add(env, "eng", "eng_sup", "工程主管")
    _add(env, "ss", "safety_sup", "安全主管")
    _add(env, "sa", "sub_safety", "安全员甲", "甲")
    _add(env, "lead", "leader", "公司领导")
    conn = dbmod.connect()
    conn.execute("UPDATE users SET company='甲' WHERE username='tech1'")
    conn.execute("UPDATE users SET company='乙' WHERE username='tech2'")
    conn.close()
    assert login("pm").post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    return env


def _digest(username):
    """首页摘要已并入待办 src_gate（GET /api/gate/digest 已下线），直接调服务层同一函数。"""
    with closing(dbmod.connect()) as conn:
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return gate_svc.digest(conn, user)


def _cond(d, key):
    return next(c for c in d["conditions"] if c["key"] == key)


def _optional_tables(conn):
    """重建其他功能负责的表（测试库临时，仅含本功能读取的列）。"""
    for t in ("conceal_records", "material_batches", "pours"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("CREATE TABLE conceal_records (id INTEGER PRIMARY KEY, project_id INTEGER, activity_code TEXT, part TEXT, template_key TEXT, status TEXT, inspection_date TEXT)")
    conn.execute("CREATE TABLE material_batches (id INTEGER PRIMARY KEY, project_id INTEGER, category TEXT, name TEXT, retest_status TEXT, arrived_at TEXT)")
    conn.execute("CREATE TABLE pours (id INTEGER PRIMARY KEY, project_id INTEGER, part TEXT, activity_code TEXT, grade TEXT, pour_date TEXT, status TEXT)")


def _drop_optional(conn):
    for t in ("conceal_records", "material_batches", "pours"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")


# ------------------------------------------------------------------ 归类

def test_rule_classification():
    rc = gate_svc.rule_classify
    assert rc("3#墩承台混凝土浇筑") == "concrete"
    assert rc("3#墩承台钢筋绑扎") == "rebar_form"
    assert rc("4#墩桩基施工") == "pile"
    assert rc("钻孔灌注桩施工") == "pile"
    assert rc("路基土方开挖") == "earth"
    assert rc("梁板架设") == "lifting"
    assert rc("沉箱出运安装") == "marine"
    assert rc("3#墩墩身施工") == "concrete"
    assert rc("现场清理") is None
    assert rc("清理", "桥面系 / 路基工程") == "earth"       # 名称没命中再看 WBS


def test_templates_cover_required_conditions():
    keys = {c["key"] for c in gate_svc.defs_for("concrete")}
    assert {"drawing", "scheme", "disclosure", "prev_done", "resources", "material",
            "rebar_conceal", "formwork", "mix_design", "sampling", "trucks", "weather"} <= keys
    assert {"pile_layout", "hole_accept", "cage_accept"} <= {c["key"] for c in gate_svc.defs_for("pile")}
    assert {"excav_scheme", "monitor", "dewater"} <= {c["key"] for c in gate_svc.defs_for("earth")}
    assert {"lift_scheme", "crane_cert", "wind"} <= {c["key"] for c in gate_svc.defs_for("lifting")}
    assert {"tide", "vessel_report"} <= {c["key"] for c in gate_svc.defs_for("marine")}
    assert [c["key"] for c in gate_svc.defs_for("general")] == [c["key"] for c in gate_svc.GENERAL]


def test_ai_classify_cached_and_falls_back(world, monkeypatch):
    conn = dbmod.connect()
    pid = world["project_id"]
    acts = [{"code": "Z1", "name": "现场清理", "wbs_path": ""}]

    def boom(*a, **k):
        raise AIError("模型不可用")

    # 失败回退通用，且不抛错、不写缓存
    monkeypatch.setattr(gate_svc, "call_json", boom)
    gate_svc._AI_FAIL.clear()
    out = gate_svc.classify_all(conn, pid, acts, None, allow_ai=True)
    assert out["Z1"] == {"category": "general", "source": "default"}
    assert conn.execute("SELECT COUNT(*) n FROM gate_class").fetchone()["n"] == 0
    # 成功则缓存；之后不再调用模型
    gate_svc._AI_FAIL.clear()
    calls = []

    def fake(kind, **k):
        calls.append(kind)
        return AIResult(data={"Z1": "lifting"}, run_id=0, provider="mock", model="mock", latency_ms=1)

    monkeypatch.setattr(gate_svc, "call_json", fake)
    assert gate_svc.classify_all(conn, pid, acts, None, allow_ai=True)["Z1"] == {"category": "lifting", "source": "ai"}
    assert calls == ["gate.classify"]
    assert gate_svc.classify_all(conn, pid, acts, None, allow_ai=False)["Z1"] == {"category": "lifting", "source": "ai"}
    assert calls == ["gate.classify"]
    # 模型给出的类别必须在固定集合内
    assert gate_svc._norm_classify({"items": [{"code": "Z1", "category": "lifting"}, {"code": "Z1", "category": "瞎编"}]}, ["Z1"]) == {"Z1": "lifting"}
    with pytest.raises(ValueError):
        gate_svc._norm_classify({"items": [{"code": "Z1", "category": "瞎编"}]}, ["Z1"])
    conn.close()


def test_ai_classify_with_mock_provider_real_path(world):
    """走真实的 call_json（离线模拟）：只会归到通用并缓存，不抛错。"""
    conn = dbmod.connect()
    gate_svc._AI_FAIL.clear()
    out = gate_svc.classify_all(conn, world["project_id"], [{"code": "Z9", "name": "现场清理", "wbs_path": ""}], None, allow_ai=True)
    assert out["Z9"]["category"] == "general"
    conn.close()


# ------------------------------------------------------------------ 自动核验

def test_prev_done_missing_then_ok(world):
    eng = login("eng")
    d = eng.get("/api/gate/A1020").json()
    c = _cond(d, "prev_done")
    assert c["auto_state"] == "missing" and c["effective"] == "missing" and "3#墩桩基施工" in c["auto_text"] and "80%" in c["auto_text"]
    # 前道工序完成 100% → 自动具备
    assert login("pm").post("/api/p6/import", files={"file": ("plan2.xer", sample_xer(progress={"M0000": 100, "A1010": 100, "A2010": 60, "A3010": 50, "A4010": 10}), "application/octet-stream")}).status_code == 200
    c = _cond(eng.get("/api/gate/A1020").json(), "prev_done")
    assert c["auto_state"] == "ok" and c["effective"] == "ready" and c["source"] == "auto" and c["manual"] is None


def test_prev_done_from_confirmed_logs(world):
    conn = dbmod.connect()
    pid, uid = world["project_id"], world["users"]["tech1"]
    today = date.today().isoformat()
    lid = dbmod.insert(conn, "logs", {"project_id": pid, "author_id": uid, "log_date": today, "mode": "ai", "status": "approved",
                                      "content_json": "{}", "started_at": "x", "period": "day", "auto": 1})
    dbmod.insert(conn, "log_progress", {"log_id": lid, "project_id": pid, "log_date": today, "activity_code": "A1010", "qty": 1, "unit": "根", "pct": 100, "note": ""})
    conn.close()
    eng = login("eng")
    assert _cond(eng.get("/api/gate/A1020").json(), "prev_done")["auto_state"] == "ok"      # 日志累计 100% 也算完成
    conn = dbmod.connect()
    conn.execute("UPDATE logs SET status='draft'")                                          # 草稿日志不算
    conn.close()
    assert _cond(eng.get("/api/gate/A1020").json(), "prev_done")["auto_state"] == "missing"


def test_prev_done_unknown_without_relation(world):
    conn = dbmod.connect()
    conn.execute("DELETE FROM relations")
    conn.close()
    c = _cond(login("eng").get("/api/gate/A1020").json(), "prev_done")
    assert c["auto_state"] == "unknown" and c["effective"] == "unknown"


def test_qc_points_auto(world):
    conn = dbmod.connect()
    pid = world["project_id"]
    assert _cond(login("eng").get("/api/gate/A1030").json(), "qc_points")["auto_state"] == "unknown"      # 没有记录 → 待落实，不默认通过
    fid = find_svc.create(conn, pid, "quality", "qc_reminder", "info", "A1030 开工前质控", {}, activity_code="A1030", fingerprint="t:qc", due_days=1)
    c = _cond(login("eng").get("/api/gate/A1030").json(), "qc_points")
    assert c["auto_state"] == "missing" and "未闭环" in c["auto_text"]
    conn.execute("UPDATE findings SET status='resolved' WHERE id=?", (fid,))
    conn.close()
    assert _cond(login("eng").get("/api/gate/A1030").json(), "qc_points")["auto_state"] == "ok"


def test_missing_optional_tables_degrade_to_unknown(world):
    conn = dbmod.connect()
    _drop_optional(conn)
    conn.close()
    eng = login("eng")
    r = eng.get("/api/gate/A1030")
    assert r.status_code == 200
    d = r.json()
    for k in ("rebar_conceal", "formwork", "material", "sampling"):
        c = _cond(d, k)
        assert c["auto_state"] == "unknown" and c["effective"] == "unknown", k
    assert eng.get("/api/gate?days=14").status_code == 200
    assert _digest("eng")["has_plan"]


def test_optional_tables_with_wrong_columns_degrade(world):
    conn = dbmod.connect()
    _drop_optional(conn)
    conn.execute("CREATE TABLE conceal_records (id INTEGER PRIMARY KEY, project_id INTEGER)")      # 缺列
    conn.close()
    r = login("eng").get("/api/gate/A1030")
    assert r.status_code == 200 and _cond(r.json(), "rebar_conceal")["auto_state"] == "unknown"


def test_conceal_material_pours_evidence(world):
    conn = dbmod.connect()
    pid = world["project_id"]
    _optional_tables(conn)

    def ins(t, **v):
        dbmod.insert(conn, t, {"project_id": pid, **v})

    ins("conceal_records", activity_code="A1030", part="3#墩承台", template_key="rebar", status="passed", inspection_date="2026-10-01")
    ins("conceal_records", activity_code="A1030", part="3#墩承台", template_key="formwork", status="submitted", inspection_date=None)
    ins("material_batches", category="钢筋", name="HRB400 φ25", retest_status="pending", arrived_at="2026-10-01")
    ins("pours", part="3#墩承台", activity_code="A1030", grade="C35", pour_date="2026-09-20", status="done")
    conn.close()
    eng = login("eng")
    d = eng.get("/api/gate/A1030").json()
    assert _cond(d, "rebar_conceal")["auto_state"] == "ok" and "已通过" in _cond(d, "rebar_conceal")["auto_text"]
    f = _cond(d, "formwork")                     # 模板只有已提交记录，不算通过
    assert f["auto_state"] == "unknown" and f["effective"] == "unknown"
    m = _cond(d, "material")
    assert m["auto_state"] == "missing" and "未出结果" in m["auto_text"]
    s = _cond(d, "sampling")                     # 浇筑记录只做提示，不会变成已具备
    assert s["auto_state"] == "unknown" and "提示" in s["auto_text"]
    # 复试合格且没有未结批次 → 材料自动具备；出现不合格 → 缺项
    conn = dbmod.connect()
    conn.execute("UPDATE material_batches SET retest_status='pass'")
    conn.close()
    assert _cond(eng.get("/api/gate/A1030").json(), "material")["auto_state"] == "ok"
    conn = dbmod.connect()
    dbmod.insert(conn, "material_batches", {"project_id": pid, "category": "钢筋", "name": "HRB400 φ16", "retest_status": "fail", "arrived_at": "2026-10-02"})
    conn.close()
    assert _cond(eng.get("/api/gate/A1030").json(), "material")["auto_state"] == "missing"
    # 与本作业无关的材料不影响（路基填筑不涉及钢筋）
    assert _cond(eng.get("/api/gate/A3020").json(), "material")["auto_state"] == "unknown"
    # 隐蔽验收被退回 → 缺项
    conn = dbmod.connect()
    conn.execute("UPDATE conceal_records SET status='rejected' WHERE template_key='rebar'")
    conn.close()
    assert _cond(eng.get("/api/gate/A1030").json(), "rebar_conceal")["auto_state"] == "missing"


def test_never_defaults_to_ready(world):
    d = login("eng").get("/api/gate/A1030").json()
    assert d["verdict"] == "gap"
    non_auto = [c for c in d["conditions"] if not c["auto"]]
    assert non_auto and all(c["effective"] == "unknown" and c["manual"] is None for c in non_auto)
    assert all(c["source"] == "auto" for c in d["conditions"] if c["effective"] == "ready")


# ------------------------------------------------------------------ 人工状态与结论

def test_manual_priority_and_na_needs_reason(world):
    eng = login("eng")
    url = "/api/gate/A1020/items/prev_done"
    assert eng.put(url, json={"state": "na", "note": ""}).status_code == 400
    assert eng.put(url, json={"state": "blocked", "note": " "}).status_code == 400
    assert eng.put(url, json={"state": "瞎写"}).status_code == 400
    d = eng.put(url, json={"state": "ready", "note": "监理已口头确认，凭证见群"}).json()
    c = _cond(d, "prev_done")
    assert c["effective"] == "ready" and c["source"] == "manual" and c["conflict"] is True      # 与系统核验不一致要标出来
    assert c["auto_state"] == "missing"                                                         # 系统结果与人工分开保留
    assert c["manual"]["by_name"] == "工程主管" and c["manual"]["note"].startswith("监理") and c["manual"]["at"]
    d = eng.put(url, json={"state": "na", "note": "无前道工序"}).json()
    assert _cond(d, "prev_done")["effective"] == "na"
    d = eng.put(url, json={"state": "pending"}).json()                                          # 撤销人工，回到系统核验
    c = _cond(d, "prev_done")
    assert c["effective"] == "missing" and c["manual"] is None
    conn = dbmod.connect()
    acts = [r["action"] for r in conn.execute("SELECT action FROM audit").fetchall()]
    conn.close()
    assert acts.count("gate.state") == 3
    assert eng.put("/api/gate/A1020/items/不存在", json={"state": "ready"}).status_code == 404
    assert eng.put("/api/gate/NOPE/items/prev_done", json={"state": "ready"}).status_code == 404


def test_verdicts(world):
    eng = login("eng")
    lst = eng.get("/api/gate?days=14").json()
    by = {r["code"]: r for r in lst["items"]}
    assert by["A1020"]["verdict"] == "near" and by["A1020"]["days_to_start"] == 0          # 今天开工仍有缺项
    assert by["A3020"]["verdict"] == "near"
    assert by["A1030"]["verdict"] == "gap" and by["A1030"]["days_to_start"] == 4
    assert "A4020" not in by and "A1010" not in by                                          # 超出窗口 / 已开工不列
    assert lst["kpi"]["near"] >= 2 and lst["kpi"]["total"] == len(lst["items"])
    assert lst["items"][0]["verdict"] != "ready"
    assert "3#墩" in by["A1020"]["wbs_path"] and by["A1020"]["units"] == ["甲"]
    # 全部落实 → 可开工；有受阻 → 受阻
    for c in eng.get("/api/gate/A1030").json()["conditions"]:
        st = "na" if c["key"] == "weather" else "ready"
        eng.put(f"/api/gate/A1030/items/{c['key']}", json={"state": st, "note": "季节无需考虑" if st == "na" else ""})
    d = eng.get("/api/gate/A1030").json()
    assert d["verdict"] == "ready" and d["verdict_name"] == "可开工" and d["gap_n"] == 0 and d["na_n"] == 1
    d = eng.put("/api/gate/A1030/items/trucks", json={"state": "blocked", "note": "泵车检修"}).json()
    assert d["verdict"] == "blocked" and d["blocked_n"] == 1
    assert eng.get("/api/gate?days=14").json()["kpi"]["blocked"] == 1
    assert eng.get("/api/gate?days=9").status_code == 400
    assert all(r["days_to_start"] <= 7 for r in eng.get("/api/gate?days=7").json()["items"])


def test_verdict_function_boundaries():
    mk = lambda *effs: [{"effective": e} for e in effs]  # noqa: E731
    v = gate_svc.verdict_of
    assert v(mk("ready", "na"), 0) == "ready"
    assert v(mk("ready", "unknown"), 3) == "near"        # 距开工 3 天仍有未具备项
    assert v(mk("ready", "missing"), 4) == "gap"
    assert v(mk("ready", "missing"), -2) == "near"       # 应开未开
    assert v(mk("blocked", "ready"), 10) == "blocked"
    assert v(mk("unknown"), None) == "gap"               # 没有计划日期不会被当成可开工


def test_no_plan_returns_empty(env):
    _add(env, "eng", "eng_sup", "工程主管")
    d = login("eng").get("/api/gate").json()
    assert d["has_plan"] is False and d["items"] == [] and d["kpi"]["total"] == 0
    assert login("eng").get("/api/gate/A1020").status_code == 404


# ------------------------------------------------------------------ 自定义条件与责任人

def test_custom_items_and_assign(world):
    eng, pm = login("eng"), login("pm")
    d = eng.post("/api/gate/A1030/items", json={"label": "业主代表到场见证"}).json()
    c = next(x for x in d["conditions"] if x["custom"])
    assert c["label"] == "业主代表到场见证" and c["effective"] == "unknown" and c["auto"] is False
    assert eng.post("/api/gate/A1030/items", json={"label": "业主代表到场见证"}).status_code == 400      # 同名
    assert eng.post("/api/gate/A1030/items", json={"label": "x"}).status_code == 400
    assert eng.delete("/api/gate/A1030/items/prev_done").status_code == 400                           # 模板条件不能删
    # 指定责任人与期限
    tid = world["users"]["tech1"]
    d = eng.put(f"/api/gate/A1030/items/{c['key']}/owner", json={"owner_id": tid, "due_date": "2026-12-31"}).json()
    c2 = next(x for x in d["conditions"] if x["key"] == c["key"])
    assert c2["owner"]["id"] == tid and c2["due_date"] == "2026-12-31"
    assert eng.put(f"/api/gate/A1030/items/{c['key']}/owner", json={"owner_id": 99999}).status_code == 400
    assert eng.put(f"/api/gate/A1030/items/{c['key']}/owner", json={"owner_id": tid, "due_date": "明天"}).status_code == 400
    # 给模板条件指定责任人
    d = pm.put("/api/gate/A1030/items/mix_design/owner", json={"owner_id": tid}).json()
    assert _cond(d, "mix_design")["owner"]["name"] == "张工"
    conn = dbmod.connect()
    n = conn.execute("SELECT COUNT(*) n FROM notifications WHERE user_id=?", (tid,)).fetchone()["n"]
    conn.close()
    assert n == 2
    assert [p["name"] for p in d["people"] if p["id"] == tid] == ["张工"]
    d = eng.delete(f"/api/gate/A1030/items/{c['key']}").json()
    assert not any(x["custom"] for x in d["conditions"])
    # 自定义条件未具备时会影响结论
    d = eng.post("/api/gate/A1030/items", json={"label": "临时用电报验"}).json()
    assert any(x["custom"] and x["effective"] == "unknown" for x in d["conditions"])


# ------------------------------------------------------------------ 权限矩阵

def test_permission_matrix(world):
    eng, tech1, tech2 = login("eng"), login("tech1"), login("tech2")
    for u in ("admin", "sa"):
        c = login(u)
        with pytest.raises(PermissionError):
            _digest(u)
        for r in (c.get("/api/gate"), c.get("/api/gate/A1020"),
                  c.put("/api/gate/A1020/items/drawing", json={"state": "ready"}), c.post("/api/gate/notify")):
            assert r.status_code == 403, (u, r.url)
    # 只读岗位：能看不能改
    for u in ("lead", "ss"):
        c = login(u)
        assert c.get("/api/gate").status_code == 200 and c.get("/api/gate/A1020").status_code == 200 and "items" in _digest(u)
        assert c.get("/api/gate/A1020").json()["perms"]["edit"] is False
        assert c.put("/api/gate/A1020/items/drawing", json={"state": "ready"}).status_code == 403
        assert c.post("/api/gate/A1020/items", json={"label": "自定义条件一"}).status_code == 403
        assert c.post("/api/gate/notify").status_code == 403
    # 技术员：只看自己范围
    assert {r["code"] for r in tech1.get("/api/gate").json()["items"]} == {"A1020", "A1030", "A1040"}
    assert {r["code"] for r in tech2.get("/api/gate").json()["items"]} == {"A3020"}
    assert tech1.get("/api/gate/A3020").status_code == 403                                           # 范围外
    assert tech1.put("/api/gate/A3020/items/drawing", json={"state": "ready"}).status_code == 403
    assert tech2.get("/api/gate/A1030").status_code == 403
    d = tech1.get("/api/gate/A1020").json()
    assert d["perms"]["states"] == ["ready", "blocked", "pending"] and d["people"] == []
    r = tech1.put("/api/gate/A1020/items/drawing", json={"state": "ready", "note": "已领蓝图"})
    assert r.status_code == 200 and _cond(r.json(), "drawing")["manual"]["by_name"] == "张工"
    assert tech1.put("/api/gate/A1020/items/scheme", json={"state": "blocked", "note": "方案未批"}).status_code == 200
    assert tech1.put("/api/gate/A1020/items/scheme", json={"state": "na", "note": "不需要方案"}).status_code == 403
    assert tech1.post("/api/gate/A1020/items", json={"label": "自定义条件一"}).status_code == 403
    assert tech1.put("/api/gate/A1020/items/disclosure/owner", json={"owner_id": world["users"]["tech1"]}).status_code == 403
    assert tech1.post("/api/gate/notify").status_code == 403
    # 工程主管、项目经理可改全部
    assert eng.put("/api/gate/A3020/items/drawing", json={"state": "ready"}).status_code == 200
    assert login("pm").put("/api/gate/A3020/items/scheme", json={"state": "na", "note": "无需方案"}).status_code == 200
    # 未登录
    from fastapi.testclient import TestClient

    from app.main import app
    assert TestClient(app).get("/api/gate").status_code == 401


# ------------------------------------------------------------------ 首页摘要与提醒

def test_digest(world):
    eng = login("eng")
    assert eng.get("/api/gate/digest").status_code == 404   # 旧接口已下线，摘要并入待办 src_gate
    d = _digest("eng")
    assert d["has_plan"] and d["count"] >= 2 and len(d["items"]) <= 5
    assert {i["code"] for i in d["items"]} >= {"A1020", "A3020"}
    assert d["items"][0]["days_to_start"] <= d["items"][-1]["days_to_start"] and d["items"][0]["first_gap"]
    assert {i["code"] for i in _digest("tech1")["items"]} == {"A1020"}
    # 全部落实后不再出现
    for code in ("A1020", "A3020"):
        for c in eng.get(f"/api/gate/{code}").json()["conditions"]:
            eng.put(f"/api/gate/{code}/items/{c['key']}", json={"state": "ready"})
    assert {i["code"] for i in _digest("eng")["items"]}.isdisjoint({"A1020", "A3020"})


def _notifs(uid):
    conn = dbmod.connect()
    rows = conn.execute("SELECT title, body FROM notifications WHERE user_id=? AND title LIKE '放行预警%'", (uid,)).fetchall()
    conn.close()
    return rows


def test_notify_due_idempotent_and_recipients(world):
    pid, eng_id, t1 = world["project_id"], world["users"]["eng"], world["users"]["tech1"]
    pm = login("pm")
    # 给 A1020 的“图纸”条件指定责任人 tech1，其它缺项无人负责 → 发给工程主管
    pm.put("/api/gate/A1020/items/drawing/owner", json={"owner_id": t1})
    conn = dbmod.connect()
    r1 = gate_svc.notify_due(conn, pid)
    assert r1["sent"] >= 3 and r1["duplicates"] == 0 and r1["activities"] >= 2         # A1020(0天)、A3020(2天) 各自有缺项
    t1_msgs, eng_msgs = _notifs(t1), _notifs(eng_id)
    assert len(t1_msgs) == 1 and "图纸" in t1_msgs[0]["body"] and "A1020" in t1_msgs[0]["title"]
    assert len(eng_msgs) >= 2 and not any("图纸" in m["body"] for m in eng_msgs if "A1020" in m["title"])
    # 同一天再跑不重复；A1030（+4 天）不在 3 天提醒范围
    r2 = gate_svc.notify_due(conn, pid)
    assert r2["sent"] == 0 and r2["duplicates"] == r1["sent"]
    assert not any("A1030" in m["title"] for m in eng_msgs)
    assert len(_notifs(t1)) == 1 and len(_notifs(eng_id)) == len(eng_msgs)
    # 次日会再提醒（用 today 参数模拟）
    assert gate_svc.notify_due(conn, pid, date.today() + timedelta(days=1))["sent"] >= 1
    conn.close()
    # 手动触发接口同样幂等
    r = login("eng").post("/api/gate/notify")
    assert r.status_code == 200 and r.json()["sent"] == 0 and r.json()["duplicates"] >= 1


def test_notify_skips_when_nothing_missing(world):
    eng = login("eng")
    for code in ("A1020", "A3020"):
        for c in eng.get(f"/api/gate/{code}").json()["conditions"]:
            eng.put(f"/api/gate/{code}/items/{c['key']}", json={"state": "ready"})
    conn = dbmod.connect()
    assert gate_svc.notify_due(conn, world["project_id"])["sent"] == 0
    conn.close()
