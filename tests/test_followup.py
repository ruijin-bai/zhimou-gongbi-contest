"""随手记追问一句：程序判断缺什么、候选只来自本人范围内的 P6 作业；每条最多问一次、每天最多 5 次；回答写回同一条随手记。
离线模拟模型，只用合成数据。"""
from conftest import login, sample_xer

from app.services import followup as svc


def _p6():
    pm = login("pm")
    r = pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    assert r.status_code == 200, r.text


def _note(c, text):
    r = c.post("/api/notes", json={"text": text, "source": "text"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _fu(c, nid):
    r = c.get(f"/api/notes/followup?ids={nid}")
    assert r.status_code == 200, r.text
    return r.json()["items"][0]


def test_rules():
    assert svc.is_progress_like("今天桩基做了一些")
    assert svc.is_progress_like("3#墩承台浇筑完成")
    assert not svc.is_progress_like("下雨停工半天")
    assert not svc.is_progress_like("开会")
    assert svc.has_qty("浇筑完成 12方") and svc.has_qty("绑扎完成六成") and svc.has_qty("完成两根桩")
    assert not svc.has_qty("3#墩承台浇筑完成")   # 部位编号不算数量


def test_missing_qty_on_matched_activity(env):
    _p6()
    c = login("tech1")
    f = _fu(c, _note(c, "3#墩承台钢筋绑扎完成"))
    assert f["state"] == "asked" and f["source"] == "rule"
    a = f["ask"]
    assert a["need_qty"] and not a["need_activity"] and a["matched"]["code"] == "A1020"
    assert a["units"][0] == "吨" and "3#墩承台钢筋绑扎" in a["question"]


def test_vague_note_gets_activity_candidates(env):
    _p6()
    c = login("tech1")
    f = _fu(c, _note(c, "今天桩基做了一些"))
    a = f["ask"]
    assert a["need_activity"] and a["need_qty"]
    codes = [x["code"] for x in a["candidates"]]
    assert codes and codes[0] == "A1010" and len(codes) <= 3
    assert "A2010" not in codes          # 4#墩不在张工的负责范围
    # 每条最多问一次：再取仍是同一个问题，不新建
    assert _fu(c, f["note_id"])["ask"]["question"] == a["question"]


def test_no_followup_when_complete_or_not_progress(env):
    _p6()
    c = login("tech1")
    assert _fu(c, _note(c, "3#墩承台钢筋绑扎完成8吨"))["state"] == "none"
    assert _fu(c, _note(c, "下雨停工"))["state"] == "none"
    assert _fu(c, _note(c, "3#墩临边没防护，浇筑完成"))["state"] == "none"   # 已有隐患归类建议，不叠追问
    assert _fu(c, _note(c, "混凝土小票 C30 12方 进场"))["state"] == "none"


def test_no_p6_no_question(env):
    c = login("tech1")
    assert _fu(c, _note(c, "今天桩基做了一些"))["state"] == "none"   # 没有可点的候选就不问


def test_answer_writes_back_to_same_note(env):
    _p6()
    c = login("tech1")
    nid = _note(c, "今天桩基做了一些")
    _fu(c, nid)
    bad = c.post(f"/api/notes/{nid}/followup/answer", json={"code": "A2010", "qty": 2, "unit": "根"})
    assert bad.status_code == 400                      # 只能从给出的候选里选
    r = c.post(f"/api/notes/{nid}/followup/answer", json={"code": "A1010", "qty": 2, "unit": "根"})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "今天桩基做了一些（补充：3#墩桩基施工，约2根）"
    home = c.get("/api/tech/home").json()["notes"]
    assert [n["text"] for n in home if n["id"] == nid] == ["今天桩基做了一些（补充：3#墩桩基施工，约2根）"]
    assert _fu(c, nid)["state"] == "answered"
    assert c.post(f"/api/notes/{nid}/followup/answer", json={"text": "再补"}).status_code == 409


def test_answer_percent_and_dismiss(env):
    _p6()
    c = login("tech1")
    nid = _note(c, "3#墩承台钢筋绑扎完成")
    _fu(c, nid)
    r = c.post(f"/api/notes/{nid}/followup/answer", json={"qty": 60, "unit": "%"})
    assert r.json()["text"].endswith("（补充：3#墩承台钢筋绑扎，约完成60%）")
    n2 = _note(c, "今天桩基做了一些")
    _fu(c, n2)
    assert c.post(f"/api/notes/{n2}/followup/dismiss").json()["state"] == "dismissed"
    assert _fu(c, n2)["state"] == "dismissed"


def test_daily_cap(env):
    _p6()
    c = login("tech1")
    states = [_fu(c, _note(c, f"今天桩基做了一些，第{i}次"))["state"] for i in range(6)]
    assert states[:5] == ["asked"] * 5 and states[5] == "none"


def test_only_tech(env):
    assert login("pm").get("/api/notes/followup?ids=1").status_code == 403


def test_guard():
    g = {"candidates": [{"code": "A1010", "name": "3#墩桩基施工"}], "matched": None}
    assert svc.guard("是3#墩桩基吗？大约几根？", ["A1010"], "今天桩基做了一些", g) == []
    assert svc.guard("是3#墩桩基吗？大约5根？", ["A1010"], "今天桩基做了一些", g) == ["数字5无出处"]
    assert svc.guard("是哪个？", ["A9999"], "今天桩基做了一些", g) == ["候选不在清单内"]


def _refine_with(monkeypatch, c, data):
    from app import db as dbmod
    from app.ai.client import AIResult
    nid = _note(c, "今天桩基做了一些")
    _fu(c, nid)
    conn = dbmod.connect()
    conn.execute("UPDATE note_followup SET status='pending' WHERE note_id=?", (nid,))
    conn.close()
    monkeypatch.setattr(svc, "call_json", lambda *a, **kw: AIResult(data=data, run_id=None, provider="jiaorong", model="m", latency_ms=1))
    svc.refine(nid)
    return _fu(c, nid)


def test_refine_uses_model_wording_only_when_guard_passes(env, monkeypatch):
    _p6()
    c = login("tech1")
    ok = _refine_with(monkeypatch, c, {"question": "是3#墩桩基吗？今天大概打了几根？", "codes": ["A1010"]})
    assert ok["source"] == "ai" and ok["ask"]["question"] == "是3#墩桩基吗？今天大概打了几根？"
    bad = _refine_with(monkeypatch, c, {"question": "是3#墩桩基吗？打了5根吧？", "codes": ["A1010"]})
    assert bad["source"] == "rule" and "5" not in bad["ask"]["question"] and "未通过校验" in bad["note"]
