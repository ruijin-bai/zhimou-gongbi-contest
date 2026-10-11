"""技术员随手记归类建议：规则、AI 只建议不入账、人点“是”才转草稿、“不是”维持随手记。离线模拟模型，只用合成数据。"""
import pytest
from conftest import jpeg, login
from test_roles import _team

from app import db as dbmod
from app.services import classify as svc

_seed = [0]


def _photo(c, purpose="log"):
    _seed[0] += 1
    s = _seed[0]
    r = c.post("/api/media", data={"purpose": purpose}, files=[("files", ("p.jpg", jpeg(((s * 37) % 256, (s * 11 + 40) % 256, (s * 53) % 256)), "image/jpeg"))])
    assert r.status_code == 200, r.text
    return r.json()[0]["id"]


def _note(c, text="", photos=0, purpose="log"):
    ids = [_photo(c, purpose) for _ in range(photos)]
    r = c.post("/api/notes", json={"text": text, "media_ids": ids, "source": "photo" if ids else "text"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _sug(c, nid):
    r = c.get(f"/api/notes/classify?ids={nid}")
    assert r.status_code == 200, r.text
    return r.json()["items"][0]


def _count(table):
    conn = dbmod.connect()
    try:
        return conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
    finally:
        conn.close()


def test_text_rules():
    top = lambda t: (svc.text_signals(t) or [{"kind": None}])[0]["kind"]
    assert top("3号墩承台浇筑完成") is None                       # 普通进度：不出建议
    assert top("钢筋绑扎完成六成，钢筋工12人") is None
    assert top("今日C30砼小票 12方 进场") == "ticket"
    assert top("水泥进场 一车") == "ticket"
    assert top("钢筋出厂合格证") == "ticket"
    assert top("3#墩承台钢筋隐蔽验收") == "conceal"
    assert top("基槽验槽完成") == "conceal"
    assert top("3#墩临边没防护") == "hazard"
    assert top("两名工人没戴安全帽") == "hazard"
    assert top("本周无隐患，已整改完毕") is None                   # 明确说没有隐患的不提示
    assert top("") is None


def test_progress_note_has_no_suggestion_and_is_unchanged(env):
    _team(env)
    c = login("tech1")
    nid = _note(c, "3号墩承台浇筑完成", photos=1)
    r = _sug(c, nid)
    assert r["state"] == "none" and "suggestion" not in r


def test_ticket_suggestion_then_yes_makes_draft_only(env):
    _team(env)
    c = login("tech1")
    nid = _note(c, "混凝土小票 C30 12方 进场", photos=1)
    r = _sug(c, nid)
    assert r["state"] == "suggested" and r["suggestion"]["kind"] == "ticket" and "混凝土小票" in r["suggestion"]["question"]
    before = _count("tickets")
    a = c.post(f"/api/notes/{nid}/classify/accept", json={})
    assert a.status_code == 200, a.text
    assert a.json()["state"] == "accepted" and a.json()["result"]["route"]["name"] == "tickets"
    assert _count("tickets") == before + 1
    tid = a.json()["result"]["target_id"]
    t = c.get(f"/api/tickets/{tid}").json()
    assert t["status"] == "draft" and len(t["media"]) == 1            # 只是草稿，未入账
    assert c.get("/api/pours").json()["items"] == []
    # 随手记原照片/文字仍在；重复点“是”不会再建一张
    home = c.get("/api/tech/home").json()["notes"]
    n = next(x for x in home if x["id"] == nid)
    assert n["text"].startswith("混凝土小票") and len(n["media"]) == 1
    assert c.post(f"/api/notes/{nid}/classify/accept", json={}).json()["result"]["target_id"] == tid
    assert _count("tickets") == before + 1
    assert _sug(c, nid)["state"] == "accepted"


def test_no_suggestion_without_photo_for_ticket_or_conceal(env):
    _team(env)
    c = login("tech1")
    assert _sug(c, _note(c, "混凝土小票 12方 进场"))["state"] == "none"
    assert _sug(c, _note(c, "钢筋隐蔽验收"))["state"] == "none"


def test_dismiss_keeps_plain_note_and_stops_prompting(env):
    _team(env)
    c = login("tech1")
    nid = _note(c, "混凝土小票 C30 进场", photos=1)
    assert _sug(c, nid)["state"] == "suggested"
    before = _count("tickets")
    assert c.post(f"/api/notes/{nid}/classify/dismiss").status_code == 200
    assert _sug(c, nid)["state"] == "dismissed"
    assert _count("tickets") == before
    items = c.get("/api/notes/classify").json()["items"]
    assert [i["state"] for i in items if i["note_id"] == nid] == ["dismissed"]


def test_conceal_needs_part_then_creates_draft_with_photo(env):
    _team(env)
    c = login("tech1")
    nid = _note(c, "钢筋隐蔽验收", photos=1)
    r = _sug(c, nid)
    assert r["suggestion"]["kind"] == "conceal"
    a = c.post(f"/api/notes/{nid}/classify/accept", json={}).json()
    # 文字里没有部位，技术员的负责范围里有“3#墩” → 直接沿用，不要求再填
    assert a["state"] == "accepted" and a["result"]["route"]["name"] == "conceal"
    rec = c.get(f"/api/conceal/{a['result']['target_id']}").json()
    assert rec["status"] == "draft" and rec["part"] == "3#墩" and len(rec["photos"]) == 1


def test_conceal_asks_for_part_when_unknown(env):
    _team(env)
    c = login("tech2")                                               # 负责范围“路基”，文字里也没有部位
    nid = _note(c, "钢筋隐蔽验收", photos=1)
    a = c.post(f"/api/notes/{nid}/classify/accept", json={}).json()
    assert a["state"] == "suggested" and a["need"]["part"] is True and a["templates"]
    assert _count("conceal_records") == 0
    b = c.post(f"/api/notes/{nid}/classify/accept", json={"part": "路基K1", "template_key": a["need"]["template_key"] or a["templates"][0]["key"]})
    assert b.status_code == 200, b.text
    assert b.json()["state"] == "accepted"


def test_hazard_from_safety_scan_and_text_only_flow(env):
    _team(env)
    c = login("tech1")
    # 照片已由安全Agent识别出隐患（离线模拟：purpose=safety 的照片会出隐患）：建议“查看隐患单”
    nid = _note(c, "", photos=1, purpose="safety")
    r = _sug(c, nid)
    assert r["suggestion"]["kind"] == "hazard" and r["suggestion"]["source"] == "safety" and r["suggestion"]["findings"]
    a = c.post(f"/api/notes/{nid}/classify/accept", json={}).json()
    assert a["result"]["route"]["name"] == "finding"
    # 纯文字隐患：建议拍照；没拍照直接确认会被拒，拍照后带 media_ids 才完成
    n2 = _note(c, "3#墩临边没防护")
    s = _sug(c, n2)["suggestion"]
    assert s["kind"] == "hazard" and s["needs_photo"] is True
    assert c.post(f"/api/notes/{n2}/classify/accept", json={}).status_code == 400
    mid = _photo(c, "safety")
    ok = c.post(f"/api/notes/{n2}/classify/accept", json={"media_ids": [mid]})
    assert ok.status_code == 200 and ok.json()["state"] == "accepted"
    assert c.post(f"/api/notes/{n2}/classify/accept", json={"media_ids": [99999]}).json()["state"] == "accepted"  # 已处理，幂等


def test_hazard_text_not_suggested_when_scan_found_nothing(env):
    _team(env)
    c = login("tech1")
    nid = _note(c, "临边没防护", photos=1)                          # purpose=log：离线模拟识别为“未见隐患”
    assert _sug(c, nid)["state"] == "none"


def test_other_users_cannot_touch_note_and_roles(env):
    _team(env)
    c1, c2 = login("tech1"), login("tech2")
    nid = _note(c1, "混凝土小票 进场", photos=1)
    assert c2.post(f"/api/notes/{nid}/classify/accept", json={}).status_code == 404
    assert c2.post(f"/api/notes/{nid}/classify/dismiss").status_code == 404
    assert c2.post(f"/api/notes/{nid}/classify", json={"kind": "ticket"}).status_code == 404
    assert login("pm").get("/api/notes/classify").status_code == 403


def test_vision_only_with_real_provider_and_photo_only(env, monkeypatch):
    _team(env)
    c = login("tech1")
    nid = _note(c, "", photos=1)
    assert _sug(c, nid)["state"] == "none"                           # 离线模拟模型：只用规则，不调视觉
    monkeypatch.setattr(svc, "_vision_usable", lambda: True)

    class R:
        data = {"kind": "ticket", "confidence": "high", "reason": "纸质发货单"}
        run_id = None
    monkeypatch.setattr(svc, "call_json", lambda *a, **k: R())
    first = c.get("/api/notes/classify").json()["items"]            # 触发后台分类（TestClient 同步跑完）
    assert any(i["note_id"] == nid for i in first)
    r = _sug(c, nid)
    assert r["state"] == "suggested" and r["suggestion"]["source"] == "vision" and r["suggestion"]["kind"] == "ticket"
    # 低置信度不给建议
    n2 = _note(c, "", photos=1)

    class L:
        data = {"kind": "conceal", "confidence": "medium", "reason": "像钢筋"}
        run_id = None
    monkeypatch.setattr(svc, "call_json", lambda *a, **k: L())
    c.get("/api/notes/classify")
    assert _sug(c, n2)["state"] == "none"


def test_hazard_pending_result_has_no_passthrough_link():
    """交给安全Agent 识别中：结果就在首页待办里，不再给跳到“我的待办”重复页的“查看”链接。"""
    from app.services import classify as cls
    r = cls.result_of(None, {"target_type": "hazard_pending", "target_id": None, "decided_kind": "hazard"})
    assert r["route"] is None and "待办" in r["label"]


def test_manual_reclassify_after_dismiss(env):
    """人工重新归类：点过“不是”的随手记也能自己选类别，仍要走“是”才生成草稿；没照片的小票/验收不能选。"""
    _team(env)
    c = login("tech1")
    nid = _note(c, "今天拍了一张单据", photos=1)
    c.post(f"/api/notes/{nid}/classify/dismiss")
    r = c.post(f"/api/notes/{nid}/classify", json={"kind": "ticket"}).json()
    assert r["state"] == "suggested" and r["suggestion"]["kind"] == "ticket" and r["suggestion"]["source"] == "manual"
    assert _sug(c, nid)["state"] != "dismissed"                       # 不再是“不是”
    before = _count("tickets")
    a = c.post(f"/api/notes/{nid}/classify/accept", json={"kind": "ticket"}).json()
    assert a["state"] == "accepted" and _count("tickets") == before + 1
    plain = _note(c, "K1+200 路基填筑")
    assert c.post(f"/api/notes/{plain}/classify", json={"kind": "conceal"}).status_code == 400
    assert c.post(f"/api/notes/{plain}/classify", json={"kind": "nope"}).status_code == 400
    h = c.post(f"/api/notes/{plain}/classify", json={"kind": "hazard"}).json()
    assert h["suggestion"]["needs_photo"] is True                     # 纯文字隐患：要拍一张照片
