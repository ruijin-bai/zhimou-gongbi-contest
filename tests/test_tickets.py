"""材料Agent：票据识别草稿 → 人确认 → 浇筑方量 / 试块 / 材料批次复试 / 预警。数据直接写表构造，避免依赖其他 Agent。"""
from contextlib import closing
from datetime import timedelta

import pytest
from conftest import TODAY, jpeg, login
from test_roles import _add, _team

from app import db as dbmod
from app.ai import ticket_agent as TA
from app.ai.client import AIError
from app.services import tickets as svc


def D(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


_seed = [0]


def _up(c, kind="concrete_ticket", n=1, color=None):
    _seed[0] += 1
    s = _seed[0]
    files = [("files", (f"t{i}.jpg", jpeg(color or ((s * 7 + i * 31) % 256, (s * 13 + 5) % 256, (s * 29 + i * 17) % 256)), "image/jpeg")) for i in range(n)]
    return c.post("/api/tickets/upload", files=files, data={"doc_kind": kind})


def _draft(c, kind="concrete_ticket", **kw):
    r = _up(c, kind, **kw)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"
    got = c.get(f"/api/tickets/{r.json()['id']}")      # 后台识别在响应后完成，前端同样轮询此接口
    assert got.status_code == 200, got.text
    return got.json()


def _confirm(c, tid, expect=200, **body):
    r = c.put(f"/api/tickets/{tid}", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def _world(env):
    _team(env)
    _add(env, "lead", "leader", "领导")
    return {u: login(u) for u in ("pm", "tech1", "tech2", "eng", "sa_a", "ss", "lead", "admin")}


def _pour(c, part="3#墩承台", grade="C30", day=None, **kw):
    r = c.post("/api/pours", json={"part": part, "grade": grade, "pour_date": day or TODAY.isoformat(), **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _truck(c, pour_id, qty, day=None, **fields):
    """拍一张砼小票并确认归入浇筑单元（数值用人工确认的字段覆盖）。"""
    t = _draft(c)
    f = {"qty": qty, **fields}
    if day:
        f["issued_at"] = day
    return _confirm(c, t["id"], fields=f, pour_id=pour_id)


def _pour_view(c, pid):
    r = c.get(f"/api/pours/{pid}")
    assert r.status_code == 200, r.text
    return r.json()


# ----------------------------------------------------------------------------- 识别草稿与确认

def test_ocr_mock_draft_not_counted_until_confirmed(env):
    c = _world(env)
    tech = c["tech1"]
    pour = _pour(c["eng"])
    d = _draft(tech)
    assert d["status"] == "draft" and d["ai_status"] == "done" and d["mock"] is True and d["provider"] == "mock"
    f = d["fields"]
    assert f["supplier"].startswith("（离线模拟）") and 6 <= f["qty"] <= 10 and f["unit"] == "m³" and f["grade"] == "C30"
    assert f["slump"] is not None and f["temp"] is None and "temp" in d["unclear"]   # 看不清的不补
    assert len(d["media"]) == 1 and d["ai_fields"]["qty"] == f["qty"]
    # 草稿不计入累计
    assert _pour_view(c["eng"], pour["id"])["actual_qty"] == 0 and _pour_view(c["eng"], pour["id"])["trucks"] == 0
    cands = tech.get(f"/api/pours/candidates?ticket_id={d['id']}").json()
    assert cands["suggest_new"]["grade"] == "C30"
    # 确认前 pour_id 不变；技术员看不到工程主管建的单元，但新建/复用由人选
    assert tech.get(f"/api/tickets/{d['id']}").json()["pour_id"] is None
    ok = _confirm(c["eng"], d["id"], fields={"temp": 21.5}, pour_id=pour["id"])
    assert ok["status"] == "confirmed" and ok["confirmer"] == "工程主管" and ok["edited"] == ["temp"] and ok["fields"]["temp"] == 21.5
    v = _pour_view(c["eng"], pour["id"])
    assert v["actual_qty"] == f["qty"] and v["trucks"] == 1 and v["slump_missing"] == 0 and v["temp_missing"] == 0
    # AI 原始提取只读保留
    assert c["eng"].get(f"/api/tickets/{d['id']}").json()["ai_fields"]["temp"] is None
    # 已确认的票据技术员不能再改
    assert tech.put(f"/api/tickets/{d['id']}", json={"fields": {"qty": 9}}).status_code == 409


def test_normalize_range_and_units():
    f, d = TA.normalize_fields({"qty": 25, "unit": "方", "slump": 350, "temp": 80, "grade": "c35p8"}, "concrete_ticket")
    assert f["qty"] is None and f["slump"] is None and f["temp"] is None and f["grade"] == "C35P8"
    assert set(d) >= {"qty", "slump", "temp"} and "20" in d["qty"]
    f, d = TA.normalize_fields({"qty": "8.5m³", "slump": "180mm", "temp": "-5℃", "grade": "C30 "}, "concrete_ticket")
    assert (f["qty"], f["slump"], f["temp"], f["grade"], f["unit"]) == (8.5, 180, -5, "C30", "m³") and not d
    f, d = TA.normalize_fields({"qty": 0, "unit": "m3"}, "concrete_ticket")
    assert f["qty"] is None and "qty" in d
    f, d = TA.normalize_fields({"qty": 5, "unit": "吨"}, "concrete_ticket")
    assert f["qty"] is None and "m³" in d["qty"]
    f, d = TA.normalize_fields({"qty": 24500, "unit": "公斤", "material": "钢筋", "spec": "hrb400e φ25"}, "material_receipt")
    assert f["qty"] == 24.5 and f["unit"] == "吨" and f["spec"] == "HRB400E Φ25"
    f, d = TA.normalize_fields({"qty": 9000, "unit": "吨", "material": "钢筋"}, "material_receipt")
    assert f["qty"] is None and "qty" in d
    f, d = TA.normalize_fields({"issued_at": "2099-01-01 08:00", "grade": "优质混凝土"}, "concrete_ticket")
    assert f["issued_at"] is None and "issued_at" in d and f["grade"] is None and "grade" in d
    f, d = TA.normalize_fields({"issued_at": "2026年10月6日 8时5分"}, "concrete_ticket", today=TODAY.replace(year=2026, month=10, day=6))
    assert f["issued_at"] == "2026-10-06 08:05"
    f, _ = TA.normalize_fields({"slump": 180, "temp": 20}, "material_receipt")
    assert f["slump"] is None and f["temp"] is None
    with pytest.raises(ValueError):
        TA.normalize_fields({"qty": 30}, "concrete_ticket", strict=True)
    assert TA.norm_cement("p.o 42.5") == "P.O 42.5" and TA.infer_category("商品混凝土") == "other"
    assert TA.infer_category("HRB400E钢筋") == "rebar" and TA.infer_category("机制砂") == "sand" and TA.infer_category("碎石") == "stone"
    assert TA.infer_category("聚羧酸减水剂") == "admixture" and TA.infer_category("P.O42.5水泥") == "cement"


def test_out_of_range_from_model_becomes_doubt(env, monkeypatch):
    c = _world(env)
    bad = TA.mock_payload("concrete_ticket", "abcdef")
    bad.update({"qty": 30, "unit": "方", "slump": 400, "temp": 99})
    monkeypatch.setattr(TA, "mock_payload", lambda kind, seed: bad)
    d = _draft(c["tech1"])
    assert d["fields"]["qty"] is None and d["fields"]["slump"] is None and d["fields"]["temp"] is None
    assert set(d["doubts"]) >= {"qty", "slump", "temp"}
    # 人必须补上合理方量才能确认；人工输入越界同样被拒
    assert c["tech1"].put(f"/api/tickets/{d['id']}", json={"fields": {}}).status_code == 400
    assert c["tech1"].put(f"/api/tickets/{d['id']}", json={"fields": {"qty": 25}}).status_code == 400
    ok = _confirm(c["tech1"], d["id"], fields={"qty": 9})
    assert ok["fields"]["qty"] == 9 and "qty" in ok["edited"] and ok["pour_id"] is None


def test_duplicate_detection(env):
    c = _world(env)
    tech, eng = c["tech1"], c["eng"]
    a = _draft(tech, color=(10, 20, 30))
    b = _draft(tech, color=(10, 20, 30))                   # 同一张照片
    assert b["dups"] and "同一张照片" in b["dups"][0]["reason"] and b["dups"][0]["ticket_id"] == a["id"]
    _confirm(tech, a["id"])
    r = tech.put(f"/api/tickets/{b['id']}", json={})
    assert r.status_code == 409 and "疑似重复" in r.json()["detail"]
    assert tech.put(f"/api/tickets/{b['id']}", json={"force_dup": True}).status_code == 200
    # 不同照片但票号+供应商+日期相同
    x = _draft(tech)
    _confirm(tech, x["id"], fields={"ticket_no": "A-001", "supplier": "某商砼站", "issued_at": D(1)})
    y = _draft(tech)
    y2 = tech.put(f"/api/tickets/{y['id']}", json={"fields": {"ticket_no": " a-001", "supplier": "某商砼站", "issued_at": D(1)}})
    assert y2.status_code == 409
    assert tech.put(f"/api/tickets/{y['id']}", json={"fields": {"ticket_no": "A-002", "supplier": "某商砼站", "issued_at": D(1)}}).status_code == 200
    # 重复提示进预警汇总
    z = _draft(tech, color=(10, 20, 30))
    assert any(i["kind"] == "ticket_dup" and i["link"]["id"] == z["id"] for i in eng.get("/api/tickets/risks").json()["items"])
    assert tech.post(f"/api/tickets/{z['id']}/void", json={"reason": "重复"}).json()["status"] == "void"
    assert not any(i["kind"] == "ticket_dup" for i in eng.get("/api/tickets/risks").json()["items"])


def test_upload_validation_and_no_safety_scan(env):
    c = _world(env)
    tech = c["tech1"]
    assert _up(tech, n=5).status_code == 400
    assert tech.post("/api/tickets/upload", files=[("files", ("x.jpg", b"notimage", "image/jpeg"))], data={"doc_kind": "auto"}).status_code == 400
    assert _up(tech, kind="weird").status_code == 400
    d = _draft(tech, n=3)
    assert len(d["media"]) == 3 and all(m["purpose"] == "ticket" and m["safety_status"] == "na" for m in d["media"])
    conn = dbmod.connect()
    assert conn.execute("SELECT COUNT(*) n FROM findings").fetchone()["n"] == 0
    run = conn.execute("SELECT * FROM ai_runs WHERE kind='ticket.ocr' ORDER BY id DESC").fetchone()
    assert run["provider"] == "mock" and run["ok"] == 1
    conn.close()
    auto = _draft(tech, kind="auto")
    assert auto["doc_kind"] == "concrete_ticket"


def test_ocr_failure_manual_fill_and_retry(env, monkeypatch):
    c = _world(env)
    tech = c["tech1"]
    orig = TA.extract

    def boom(*a, **k):
        raise AIError("交融模型响应超时")
    monkeypatch.setattr(TA, "extract", boom)
    d = _draft(tech)
    assert d["ai_status"] == "failed" and "超时" in d["ai_error"] and d["fields"]["qty"] is None
    monkeypatch.setattr(TA, "extract", orig)
    r = tech.post(f"/api/tickets/{d['id']}/reocr")
    assert r.status_code == 200
    assert tech.get(f"/api/tickets/{d['id']}").json()["ai_status"] == "done"
    assert tech.post(f"/api/tickets/{d['id']}/reocr").status_code == 200   # done 也可再识别
    _confirm(tech, d["id"])
    assert tech.post(f"/api/tickets/{d['id']}/reocr").status_code == 409


# ----------------------------------------------------------------------------- 浇筑

def test_pour_deviation_and_pending_design_qty(env):
    c = _world(env)
    eng = c["eng"]
    p = _pour(eng, grade="c30")
    assert p["grade"] == "C30" and p["design_qty"] is None and p["design_qty_text"] == "理论方量待补" and p["deviation_pct"] is None
    _truck(eng, p["id"], 20)
    v = _pour_view(eng, p["id"])
    assert v["actual_qty"] == 20 and v["deviation_pct"] is None and v["deviation_flag"] is None and v["deviation_text"] == "理论方量待补"
    assert eng.put(f"/api/pours/{p['id']}", json={"design_qty": 0}).status_code == 400
    assert eng.put(f"/api/pours/{p['id']}", json={"design_qty": 100}).status_code == 200
    for q in (20, 20, 20, 20, 10):
        _truck(eng, p["id"], q)
    v = _pour_view(eng, p["id"])
    assert v["actual_qty"] == 110 and v["trucks"] == 6 and v["deviation_pct"] == 10 and v["deviation_flag"] == "over"
    assert "疑点" in v["deviation_text"] and "请核实" in v["deviation_text"]
    assert any(i["kind"] == "qty_deviation" for i in eng.get("/api/tickets/risks").json()["items"])
    # 阈值可配置
    assert eng.put("/api/tickets/rules", json={"concrete_rules": {"deviation_pct": 15}}).status_code == 200
    assert _pour_view(eng, p["id"])["deviation_flag"] is None
    assert eng.put("/api/tickets/rules", json={"concrete_rules": {"deviation_pct": 3}}).status_code == 200
    # 未浇完不提示少浇，标记完成后才是疑点
    q = _pour(eng, part="4#墩承台", design_qty=100)
    _truck(eng, q["id"], 20)
    _truck(eng, q["id"], 20)
    v = _pour_view(eng, q["id"])
    assert v["deviation_pct"] == -60 and v["deviation_flag"] is None
    eng.put(f"/api/pours/{q['id']}", json={"status": "done"})
    v = _pour_view(eng, q["id"])
    assert v["deviation_flag"] == "under" and "疑点" in v["deviation_text"]
    # 作废一张后累计下降
    first = eng.get(f"/api/tickets?pour_id={q['id']}&status=confirmed").json()["items"][0]
    assert eng.post(f"/api/tickets/{first['id']}/void", json={}).status_code == 200
    assert _pour_view(eng, q["id"])["actual_qty"] == 20


def test_candidates_recommend_but_never_auto_assign(env):
    c = _world(env)
    eng = c["eng"]
    a = _pour(eng, part="3#墩承台", grade="C30", day=D(0))
    b = _pour(eng, part="5#墩", grade="C40", day=D(9))
    t = _draft(eng)
    assert t["pour_id"] is None
    cands = eng.get(f"/api/pours/candidates?ticket_id={t['id']}&part=3%23%E5%A2%A9%E6%89%BF%E5%8F%B0").json()
    assert [x["id"] for x in cands["items"]] == [a["id"]] and "部位相同" in cands["items"][0]["reasons"] and "强度等级相同" in cands["items"][0]["reasons"]
    assert b["id"] not in [x["id"] for x in cands["items"]]
    assert eng.get(f"/api/tickets/{t['id']}").json()["pour_id"] is None
    # 新建单元并入账；新建撞上同名单元时复用，不产生重复
    ok = _confirm(eng, t["id"], new_pour={"part": "6#墩", "grade": "C30", "pour_date": D(0)})
    assert ok["pour"]["part"] == "6#墩"
    t2 = _draft(eng)
    ok2 = _confirm(eng, t2["id"], new_pour={"part": "6#墩", "grade": "C30", "pour_date": D(0)})
    assert ok2["pour_id"] == ok["pour_id"]
    assert eng.post("/api/pours", json={"part": "6#墩", "grade": "C30", "pour_date": D(0)}).status_code == 409
    # 强度等级不一致只提示，不拦也不改
    t3 = _draft(eng)
    ok3 = _confirm(eng, t3["id"], pour_id=b["id"])
    assert any("不一致" in w for w in ok3["warnings"])
    # 重新归属
    r = eng.post(f"/api/tickets/{t3['id']}/pour", json={"pour_id": a["id"]})
    assert r.status_code == 200 and r.json()["pour_id"] == a["id"]
    assert eng.delete(f"/api/pours/{a['id']}").status_code == 409


# ----------------------------------------------------------------------------- 试块

def test_specimen_required_missing_and_due(env):
    c = _world(env)
    eng, tech = c["eng"], c["tech1"]
    p = _pour(eng, day=D(2))
    # 3 个浇筑日各 8m³：按方量 1 组，按日 3 组 → 应留 3 组
    for day in (D(2), D(1), D(0)):
        _truck(eng, p["id"], 8, day=day)
    v = _pour_view(eng, p["id"])
    sp = v["specimens"]
    assert v["days"] == 3 and sp["required"] == 3 and sp["registered"] == 0 and sp["missing"] == 3 and "通用常规" in sp["basis"]
    miss = [i for i in eng.get("/api/tickets/risks").json()["items"] if i["kind"] == "specimen_missing"]
    assert len(miss) == 1 and "差 3 组" in miss[0]["detail"] and "疑似" in miss[0]["title"]
    # 技术员登记试块；7/28 天到期日自动算出
    r = tech.post(f"/api/pours/{p['id']}/specimens", json={"molded_date": D(0)})
    assert r.status_code == 404  # 技术员看不到工程主管建的浇筑单元
    r = eng.post(f"/api/pours/{p['id']}/specimens", json={"molded_date": D(26), "curing": "标准养护"})
    assert r.status_code == 200
    s1 = r.json()
    assert s1["group_no"] == "1" and s1["age7"]["due"] == D(19) and s1["age28"]["due"] == D(-2) and s1["age28"]["state"] == "soon" and s1["age28"]["days_left"] == 2
    assert eng.post(f"/api/pours/{p['id']}/specimens", json={"molded_date": D(26), "group_no": "1"}).status_code == 409
    assert eng.post(f"/api/pours/{p['id']}/specimens", json={"molded_date": D(-3)}).status_code == 400
    assert eng.post(f"/api/pours/{p['id']}/specimens", json={"molded_date": D(1), "curing": "蒸养"}).status_code == 400
    s2 = eng.post(f"/api/pours/{p['id']}/specimens", json={"molded_date": D(30), "group_no": "A2"}).json()
    s3 = eng.post(f"/api/pours/{p['id']}/specimens", json={"molded_date": D(1), "curing": "同条件养护"}).json()
    assert s2["age28"]["state"] == "overdue" and s3["age28"]["state"] == "ok"
    v = _pour_view(eng, p["id"])
    assert v["specimens"]["registered"] == 3 and v["specimens"]["missing"] == 0 and v["specimens"]["next_due_28"] == D(2)
    items = eng.get("/api/tickets/risks").json()["items"]
    assert not any(i["kind"] == "specimen_missing" for i in items)
    soon = [i for i in items if i["kind"] == "specimen_due"]
    over = [i for i in items if i["kind"] == "specimen_overdue"]
    assert len(soon) == 1 and "还剩 2 天" in soon[0]["detail"] and len(over) == 1 and over[0]["level"] == "high" and "已过 2 天" in over[0]["detail"]
    due = eng.get("/api/specimens/due").json()
    assert [x["state"] for x in due["items"] if x["age"] == 28][:2] == ["overdue", "soon"]
    # 录结果：需 site_quality
    body = {"age": 28, "mpa": 33.5, "report_no": "SY-001", "tested_at": D(0)}
    assert tech.post(f"/api/specimens/{s1['id']}/result", json=body).status_code == 403
    assert c["admin"].post(f"/api/specimens/{s1['id']}/result", json=body).status_code == 403
    assert eng.post(f"/api/specimens/{s1['id']}/result", json={**body, "mpa": 0}).status_code == 400
    assert eng.post(f"/api/specimens/{s1['id']}/result", json={**body, "age": 14}).status_code == 400
    assert eng.post(f"/api/specimens/{s1['id']}/result", json={**body, "tested_at": D(40)}).status_code == 400
    ok = c["pm"].post(f"/api/specimens/{s1['id']}/result", json=body).json()
    assert ok["age28"]["state"] == "done" and ok["r28"]["mpa"] == 33.5 and "不低于设计等级值 30" in ok["r28"]["ref"] and "仅参考" in ok["r28"]["ref"]
    low = eng.post(f"/api/specimens/{s2['id']}/result", json={**body, "mpa": 28.0, "report_no": "SY-002"}).json()
    assert "低于设计等级值 30" in low["r28"]["ref"] and "合格" not in low["r28"]["ref"]
    assert not any(i["kind"] in ("specimen_due", "specimen_overdue") for i in eng.get("/api/tickets/risks").json()["items"])
    assert eng.delete(f"/api/specimens/{s1['id']}").status_code == 409
    assert eng.delete(f"/api/specimens/{s3['id']}").status_code == 200


def test_specimen_required_by_volume(env):
    c = _world(env)
    eng = c["eng"]
    p = _pour(eng)
    for _ in range(5):
        _truck(eng, p["id"], 20)                                # 恰好 100m³ → 1 组
    assert _pour_view(eng, p["id"])["specimens"]["required"] == 1
    _truck(eng, p["id"], 1)                                     # 101m³ → 2 组
    assert _pour_view(eng, p["id"])["specimens"]["required"] == 2
    assert eng.put("/api/tickets/rules", json={"concrete_rules": {"specimen_per_m3": 50}}).status_code == 200
    assert _pour_view(eng, p["id"])["specimens"]["required"] == 3


# ----------------------------------------------------------------------------- 材料批次与复试

def _receipt(c, **fields):
    d = _draft(c, "material_receipt")
    return _confirm(c, d["id"], fields=fields)


def test_batch_merge_and_retest_flow(env):
    c = _world(env)
    eng, tech = c["eng"], c["tech1"]
    d = _draft(tech, "material_receipt")
    assert d["category"] == "rebar" and d["fields"]["spec"] == "HRB400E Φ25" and d["fields"]["qty"] >= 10
    a = _confirm(tech, d["id"], fields={"qty": 30, "batch_no": "l-123", "issued_at": D(3)})
    b = _receipt(tech, qty=45, batch_no="L-123", issued_at=D(2))           # 同类别+规格+批号 → 并入
    assert a["batch_id"] == b["batch_id"]
    n = _receipt(tech, qty=20, batch_no="L-999", issued_at=D(2))
    assert n["batch_id"] != a["batch_id"]
    batches = eng.get("/api/batches").json()
    assert len(batches["items"]) == 2 and "监理" in batches["note"]
    bt = next(x for x in batches["items"] if x["id"] == a["batch_id"])
    assert bt["qty"] == 75 and bt["unit"] == "吨" and bt["batch_no"] == "L-123" and bt["arrived_at"] == D(3) and bt["retest_status"] == "pending"
    assert "约需 2 个取样批" in bt["sample_hint"] and len(bt["tickets"]) == 2
    # 砂石默认不强制复试；水泥袋装/散装吨数不同
    sand = _receipt(tech, material="机制砂", spec="中砂", qty=60, unit="吨", batch_no="S1", issued_at=D(1))
    assert eng.get(f"/api/batches?category=sand").json()["items"][0]["retest_status"] == "not_required" and sand["category"] == "sand"
    cem = _receipt(tech, material="P.O42.5水泥", spec="p.o 42.5", qty=350, unit="吨", batch_no="C1", issued_at=D(1))
    cb = next(x for x in eng.get("/api/batches?category=cement").json()["items"] if x["id"] == cem["batch_id"])
    assert cb["spec"] == "P.O 42.5" and "约需 2 个取样批" in cb["sample_hint"] and "200" in cb["sample_hint"]
    # 复试流转：技术员可标已送检，结果需 site_quality 且要报告号
    bid = a["batch_id"]
    assert tech.put(f"/api/batches/{bid}/retest", json={"status": "sampled"}).json()["retest_status"] == "sampled"
    assert tech.put(f"/api/batches/{bid}/retest", json={"status": "pass", "report_no": "FS-1"}).status_code == 403
    assert c["admin"].put(f"/api/batches/{bid}/retest", json={"status": "pass", "report_no": "FS-1"}).status_code == 403
    assert eng.put(f"/api/batches/{bid}/retest", json={"status": "pass"}).status_code == 400
    assert eng.put(f"/api/batches/{bid}/retest", json={"status": "bad"}).status_code == 400
    ok = eng.put(f"/api/batches/{bid}/retest", json={"status": "pass", "report_no": "FS-1", "reported_at": D(0)}).json()
    assert ok["retest_status"] == "pass" and ok["report_no"] == "FS-1" and ok["reported_at"] == D(0)
    assert tech.put(f"/api/batches/{bid}/retest", json={"status": "pending"}).status_code == 403    # 已出结果的批次技术员不能改回
    # fail 进预警
    nb = n["batch_id"]
    eng.put(f"/api/batches/{nb}/retest", json={"status": "fail", "report_no": "FS-2"})
    fails = [i for i in eng.get("/api/tickets/risks").json()["items"] if i["kind"] == "retest_fail"]
    assert len(fails) == 1 and "不合格" in fails[0]["title"] and fails[0]["link"] == {"route": "tickets", "tab": "batches", "id": nb}
    # 合格证挂到同批号批次，不改数量
    cert = _draft(tech, "cert")
    cc = _confirm(tech, cert["id"], fields={"batch_no": "L-123"})
    assert cc["batch_id"] == bid
    after = next(x for x in eng.get("/api/batches?category=rebar").json()["items"] if x["id"] == bid)
    assert after["qty"] == 75 and sorted(t["kind"] for t in after["tickets"]) == ["cert", "material_receipt", "material_receipt"]


def test_void_receipt_recalculates_or_drops_batch(env):
    c = _world(env)
    eng, tech = c["eng"], c["tech1"]
    a = _receipt(tech, qty=30, batch_no="V1", issued_at=D(2))
    b = _receipt(tech, qty=10, batch_no="V1", issued_at=D(1))
    bid = a["batch_id"]
    assert eng.get("/api/batches").json()["items"][0]["qty"] == 40
    assert tech.post(f"/api/tickets/{b['id']}/void", json={}).status_code == 403     # 已确认票据需主管作废
    assert eng.post(f"/api/tickets/{b['id']}/void", json={"reason": "录错"}).status_code == 200
    assert eng.get("/api/batches").json()["items"][0]["qty"] == 30
    assert eng.post(f"/api/tickets/{a['id']}/void", json={}).status_code == 200
    assert eng.get("/api/batches").json()["items"] == []                              # 没有票据支撑的待取样批次自动消失
    assert eng.post(f"/api/tickets/{a['id']}/void", json={}).status_code == 409
    # 已有复试结果的批次不会被删除
    x = _receipt(tech, qty=30, batch_no="V2", issued_at=D(2))
    eng.put(f"/api/batches/{x['batch_id']}/retest", json={"status": "pass", "report_no": "R1"})
    eng.post(f"/api/tickets/{x['id']}/void", json={})
    kept = eng.get("/api/batches").json()["items"]
    assert len(kept) == 1 and kept[0]["qty"] == 0 and kept[0]["retest_status"] == "pass"
    assert bid


def _mklog(env, user, day, materials, status="approved", period="day"):
    conn = dbmod.connect()
    lid = dbmod.insert(conn, "logs", {"project_id": env["project_id"], "author_id": env["users"][user], "log_date": day, "mode": "ai", "status": status, "weather": "晴",
                                      "voice_text": "", "content_json": dbmod.dumps({"materials": materials}), "started_at": day + "T07:00:00", "period": period, "auto": 1,
                                      "submitted_at": (day + "T20:00:00") if status != "draft" else None})
    conn.close()
    return lid


def _used(c):
    return [i for i in c.get("/api/tickets/risks").json()["items"] if i["kind"] == "used_before_retest"]


def test_used_before_retest_warning(env):
    c = _world(env)
    eng, tech = c["eng"], c["tech1"]
    r = _receipt(tech, qty=30, batch_no="U1", issued_at=D(5))
    assert _used(eng) == []                                                      # 没有日志消耗：不预警
    _mklog(env, "tech1", D(3), [{"name": "钢筋", "qty": 5, "unit": "吨", "action": "进场"}])
    _mklog(env, "tech1", D(4), [{"name": "钢筋", "qty": 5, "unit": "吨", "action": "检验"}])
    assert _used(eng) == []                                                      # 进场/检验不算消耗
    _mklog(env, "tech1", D(6), [{"name": "钢筋", "qty": 5, "unit": "吨", "action": "消耗"}])
    assert _used(eng) == []                                                      # 到场日之前的消耗不算
    _mklog(env, "tech1", D(2), [{"name": "水泥", "qty": 5, "unit": "吨", "action": "消耗"}])
    assert _used(eng) == []                                                      # 别的类别
    _mklog(env, "tech2", D(1), [{"name": "φ25钢筋", "qty": 5, "unit": "吨", "action": "使用"}], status="draft")
    assert _used(eng) == []                                                      # 未提交的日志不算
    _mklog(env, "tech2", D(1), [{"name": "φ25钢筋", "qty": 5, "unit": "吨", "action": "使用"}], status="submitted", period="week")
    assert _used(eng) == []                                                      # 周报汇总不精确到日，不参与
    lid = _mklog(env, "pm", D(1), [{"name": "φ25钢筋", "qty": 5, "unit": "吨", "action": "使用"}], status="submitted")
    w = _used(eng)
    assert len(w) == 1 and w[0]["level"] == "high" and "请核对" in w[0]["detail"] and "未出复试结果" in w[0]["title"]
    assert f"{int(D(1)[5:7])}月{int(D(1)[8:10])}日" in w[0]["detail"]
    assert any("U1" in b for b in w[0]["basis"]) and any("消耗" in b for b in w[0]["basis"]) and lid
    # 已送检 / 不合格同样预警；合格后消失；无需复试不预警
    eng.put(f"/api/batches/{r['batch_id']}/retest", json={"status": "sampled"})
    assert len(_used(eng)) == 1
    eng.put(f"/api/batches/{r['batch_id']}/retest", json={"status": "fail", "report_no": "F1"})
    assert len(_used(eng)) == 1
    eng.put(f"/api/batches/{r['batch_id']}/retest", json={"status": "pass", "report_no": "P1"})
    assert _used(eng) == []
    # 分包单位只会被自己范围内的日志触发
    r2 = _receipt(tech, qty=10, batch_no="U2", issued_at=D(5))
    assert len(_used(eng)) == 1 and _used(tech) == []                          # tech1 自己的日志里没有钢筋消耗
    _mklog(env, "tech1", D(0), [{"name": "钢筋", "qty": 1, "unit": "吨", "action": "消耗"}])
    assert len(_used(tech)) == 1 and r2["batch_id"]


# ----------------------------------------------------------------------------- 权限与范围

def test_permissions_matrix(env):
    c = _world(env)
    eng, tech, tech2 = c["eng"], c["tech1"], c["tech2"]
    t = _draft(tech)
    for path in ("/api/tickets", "/api/tickets/risks", "/api/tickets/rules", "/api/pours", "/api/batches", "/api/specimens/due", f"/api/tickets/{t['id']}"):
        assert c["admin"].get(path).status_code == 403, path
        assert c["sa_a"].get(path).status_code == 403, path
        assert c["lead"].get(path).status_code == 200, path
        assert c["ss"].get(path).status_code == 200, path
    # 领导/安全主管只读
    assert c["lead"].post("/api/tickets/upload", files=[("files", ("a.jpg", jpeg((1, 2, 3)), "image/jpeg"))]).status_code == 403
    assert c["ss"].post("/api/tickets/upload", files=[("files", ("a.jpg", jpeg((1, 2, 3)), "image/jpeg"))]).status_code == 403
    assert c["lead"].put(f"/api/tickets/{t['id']}", json={}).status_code == 403
    assert c["lead"].post("/api/pours", json={"part": "x"}).status_code == 403
    assert c["lead"].post(f"/api/tickets/{t['id']}/void", json={}).status_code == 403
    assert c["sa_a"].post("/api/tickets/upload", files=[("files", ("a.jpg", jpeg((1, 2, 3)), "image/jpeg"))]).status_code == 403
    assert c["admin"].post("/api/tickets/upload", files=[("files", ("a.jpg", jpeg((1, 2, 3)), "image/jpeg"))]).status_code == 403
    # 技术员：别人的票据看不到、动不了；本人的可确认
    assert tech2.get(f"/api/tickets/{t['id']}").status_code == 404
    assert tech2.put(f"/api/tickets/{t['id']}", json={}).status_code == 404
    assert tech2.post(f"/api/tickets/{t['id']}/void", json={}).status_code == 404
    assert tech2.get("/api/tickets").json()["items"] == []
    assert [x["id"] for x in tech.get("/api/tickets").json()["items"]] == [t["id"]] and len(eng.get("/api/tickets").json()["items"]) == 1
    assert tech2.get("/api/tickets/risks").json()["total"] == 0 and eng.get("/api/tickets/risks").json()["pending_tickets"] == 1
    # 工程主管可确认他人票据，且上传人保持不变
    done = _confirm(eng, t["id"])
    assert done["uploader"] == "张工" and done["confirmer"] == "工程主管"
    # 规则：只有项目经理/工程主管能改，值域校验
    assert tech.put("/api/tickets/rules", json={"concrete_rules": {"deviation_pct": 5}}).status_code == 403
    assert c["lead"].put("/api/tickets/rules", json={"concrete_rules": {"deviation_pct": 5}}).status_code == 403
    assert eng.put("/api/tickets/rules", json={"concrete_rules": {"deviation_pct": 99}}).status_code == 400
    assert eng.put("/api/tickets/rules", json={"material_rules": {"foo": {"required": True}}}).status_code == 400
    ok = c["pm"].put("/api/tickets/rules", json={"material_rules": {"sand": {"required": True, "batch_t": 400}}}).json()
    assert ok["material_rules"]["sand"]["required"] is True and ok["concrete_rules"]["deviation_pct"] == 3 and tech.get("/api/tickets/rules").json()["can_edit"] is False
    assert "监理" in ok["note_material"] and "监理" in ok["note_concrete"]
    # 规则变化只影响之后新建的批次
    s = _receipt(tech, material="机制砂", spec="中砂", qty=100, unit="吨", batch_no="S9", issued_at=D(1))
    assert next(b for b in eng.get("/api/batches?category=sand").json()["items"] if b["id"] == s["batch_id"])["retest_status"] == "pending"
    # 技术员不能删浇筑单元 / 录试验结果
    p = _pour(tech)
    assert tech.delete(f"/api/pours/{p['id']}").status_code == 403
    assert tech.put(f"/api/pours/{p['id']}", json={"design_qty": 50}).status_code == 200
    assert eng.delete(f"/api/pours/{p['id']}").status_code == 200


def test_company_scope_partial_view(env):
    c = _world(env)
    eng, tech, tech2 = c["eng"], c["tech1"], c["tech2"]
    p = _pour(tech, design_qty=100, day=D(0))
    assert tech2.get("/api/pours").json()["items"] == [] and tech2.get(f"/api/pours/{p['id']}").status_code == 404
    t1 = _draft(tech)
    _confirm(tech, t1["id"], fields={"qty": 10}, pour_id=p["id"])
    t2 = _draft(tech2)
    assert tech2.put(f"/api/tickets/{t2['id']}", json={"fields": {"qty": 15}, "pour_id": p["id"]}).status_code == 404   # 看不到别单位的单元
    _confirm(eng, t2["id"], fields={"qty": 15}, pour_id=p["id"])
    full, mine = _pour_view(eng, p["id"]), _pour_view(tech, p["id"])
    assert full["actual_qty"] == 25 and full["partial"] is False and full["deviation_pct"] == -75
    assert mine["actual_qty"] == 10 and mine["partial"] is True and mine["deviation_pct"] is None and mine["specimens"]["required"] is None
    # 技术员登记试块只看到自己登记的
    assert tech.post(f"/api/pours/{p['id']}/specimens", json={"molded_date": D(1)}).status_code == 200
    assert len(_pour_view(tech, p["id"])["specimen_list"]) == 1 and _pour_view(eng, p["id"])["specimens"]["registered"] == 1


def test_rules_default_and_audit(env):
    c = _world(env)
    r = c["eng"].get("/api/tickets/rules").json()
    assert r["concrete_rules"] == {"specimen_per_m3": 100.0, "specimens_per_day": 1, "deviation_pct": 3.0, "warn_days": 3}
    assert r["material_rules"]["rebar"] == {"required": True, "batch_t": 60.0} and r["material_rules"]["sand"]["required"] is False and r["can_edit"] is True
    c["eng"].put("/api/tickets/rules", json={"concrete_rules": {"warn_days": 5}})
    conn = dbmod.connect()
    acts = [a["action"] for a in conn.execute("SELECT action FROM audit").fetchall()]
    assert "tickets.rules" in acts
    # 其它项目配置不被覆盖
    cfg = dbmod.loads(conn.execute("SELECT config_json FROM projects").fetchone()["config_json"])
    assert cfg["cadence"] == "day" and cfg["concrete_rules"]["warn_days"] == 5
    conn.close()


# ----------------------------------------------------------------------------- 提醒

def test_notify_due_is_idempotent(env):
    c = _world(env)
    eng, tech = c["eng"], c["tech1"]
    p = _pour(eng, design_qty=100)
    t = _draft(tech)
    _confirm(eng, t["id"], fields={"qty": 20}, pour_id=p["id"])
    for _ in range(5):
        _truck(eng, p["id"], 20)
    eng.post(f"/api/pours/{p['id']}/specimens", json={"molded_date": D(30)})
    _receipt(tech, qty=30, batch_no="N1", issued_at=D(5))
    _mklog(env, "tech1", D(1), [{"name": "钢筋", "qty": 2, "unit": "吨", "action": "消耗"}])
    stale = _draft(tech)
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE tickets SET created_at=? WHERE id=?", (D(2) + "T09:00:00+08:00", stale["id"]))
        r1 = svc.notify_due(conn, env["project_id"])
        r2 = svc.notify_due(conn, env["project_id"])
        assert r1["sent"] >= 5 and r2["sent"] == 0 and r2["skipped"] >= r1["sent"]
        tech_notes = conn.execute("SELECT title, body FROM notifications WHERE user_id=?", (env["users"]["tech1"],)).fetchall()
        eng_notes = conn.execute("SELECT title, body FROM notifications WHERE user_id=?", (env["users"]["eng"],)).fetchall()
        titles = {n["title"] for n in eng_notes}
        assert {"材料与砼提醒：方量差异疑点", "材料与砼提醒：试块结果超期未录", "材料与砼提醒：已用未复试请核对"} <= titles
        assert any("票据待确认" in n["title"] for n in tech_notes) and any("方量差异" in n["title"] for n in tech_notes)
        assert not conn.execute("SELECT 1 FROM notifications WHERE user_id=?", (env["users"]["admin"],)).fetchone()
        assert all("合格" not in n["body"] or "不合格" in n["body"] for n in eng_notes)
        # 疑点键与跳转写在 link_json（F-06）
        for n in conn.execute("SELECT title, link_json FROM notifications WHERE title LIKE '材料与砼提醒：%'").fetchall():
            lk = dbmod.loads(n["link_json"])
            assert lk and lk["route"] == "tickets" and lk["params"].get("tab"), n
            assert n["title"] == "材料与砼提醒：票据待确认" or lk["params"].get("doubt"), n
        # AC-4.7：次日再调用，同一疑点不再推（只剩每天的“票据待确认”提醒）
        conn.execute("UPDATE notifications SET created_at=?", (D(1) + "T09:00:00+08:00",))
        r3 = svc.notify_due(conn, env["project_id"])
        assert r3["sent"] == 1 and conn.execute("SELECT COUNT(*) n FROM notifications WHERE created_at>=? AND title<>'材料与砼提醒：票据待确认'",
                                                (TODAY.isoformat(),)).fetchone()["n"] == 0
        # 7 天后仍未核对：再推一次
        conn.execute("UPDATE notifications SET created_at=?", (D(7) + "T09:00:00+08:00",))
        assert svc.notify_due(conn, env["project_id"])["sent"] == r1["sent"]


def test_notify_due_repushes_when_doubt_content_changes(env):
    c = _world(env)
    eng = c["eng"]
    p = _pour(eng, design_qty=10)
    _truck(eng, p["id"], 20)
    conn = dbmod.connect()
    try:
        r1 = svc.notify_due(conn, env["project_id"])
        dev = "SELECT COUNT(*) n FROM notifications WHERE user_id=? AND title='材料与砼提醒：方量差异疑点'"
        assert r1["sent"] >= 1 and conn.execute(dev, (env["users"]["eng"],)).fetchone()["n"] == 1
        assert svc.notify_due(conn, env["project_id"])["sent"] == 0
        _truck(eng, p["id"], 10)    # 方量变了：同一疑点内容变化，可以再推一次
        assert svc.notify_due(conn, env["project_id"])["sent"] >= 1
        assert conn.execute(dev, (env["users"]["eng"],)).fetchone()["n"] == 2
        assert svc.notify_due(conn, env["project_id"])["sent"] == 0
    finally:
        conn.close()


def test_notify_due_legacy_same_text_counts_as_sent(env):
    """升级前已按“同标题同内容”发过的疑点（没有疑点键），7 天内不因换了去重口径而再推一次。"""
    c = _world(env)
    eng = c["eng"]
    p = _pour(eng, design_qty=10)
    _truck(eng, p["id"], 20)
    conn = dbmod.connect()
    try:
        assert svc.notify_due(conn, env["project_id"])["sent"] >= 1
        conn.execute("UPDATE notifications SET link_json=NULL, created_at=?", (D(2) + "T09:00:00+08:00",))
        assert svc.notify_due(conn, env["project_id"])["sent"] == 0
    finally:
        conn.close()


def test_reocr_allowed_for_stale_pending(env):
    """T-09b 遗留：pending 超过 10 分钟（后台识别被重启打断）也允许重新识别；刚上传的 pending 仍拒绝。"""
    from datetime import datetime
    now = datetime.now().astimezone()
    base = {"status": "draft", "ai_status": "pending"}
    assert svc.reocr_allowed({**base, "updated_at": (now - timedelta(minutes=11)).isoformat(timespec="seconds")}) is True
    assert svc.reocr_allowed({**base, "updated_at": (now - timedelta(minutes=3)).isoformat(timespec="seconds")}) is False
    assert svc.reocr_allowed({**base, "status": "confirmed", "updated_at": (now - timedelta(hours=1)).isoformat(timespec="seconds")}) is False
    c = _world(env)
    tech = c["tech1"]
    d = _draft(tech)
    conn = dbmod.connect()
    conn.execute("UPDATE tickets SET ai_status='pending', updated_at=? WHERE id=?", (dbmod.now_iso(), d["id"]))
    assert tech.post(f"/api/tickets/{d['id']}/reocr").status_code == 409
    conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", ((now - timedelta(minutes=15)).isoformat(timespec="seconds"), d["id"]))
    conn.close()
    assert tech.post(f"/api/tickets/{d['id']}/reocr").status_code == 200
    assert tech.get(f"/api/tickets/{d['id']}").json()["ai_status"] == "done"


def test_risks_shape(env):
    c = _world(env)
    eng = c["eng"]
    empty = eng.get("/api/tickets/risks").json()
    assert empty["items"] == [] and empty["total"] == 0 and empty["counts"] == {} and empty["pending_tickets"] == 0
    d = _draft(c["tech1"])
    r = eng.get("/api/tickets/risks").json()
    assert r["counts"] == {"ticket_pending": 1} and r["items"][0]["link"]["tab"] == "pending" and r["alerts"] == 0
    _confirm(eng, d["id"])
    r = eng.get("/api/tickets/risks").json()
    assert r["counts"] == {"ticket_unassigned": 1}                     # 砼小票未归单元也要提示
    assert all("_to" not in i for i in r["items"])
