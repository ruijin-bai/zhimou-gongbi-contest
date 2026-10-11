"""两周高风险作业安全预控：放行清单里的吊装/水上/深基坑/交通导改作业 + 负责单位历史同类隐患；预控提醒安全主管确认后才下发，签收即销项。合成数据、离线模拟模型。"""
from conftest import confirm_hazards, login, sample_xer
from test_roles import _team

from app import db as dbmod
from app.services import findings as find_svc
from app.services import precontrol as svc


def _world(env):
    _team(env)
    assert login("pm").post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    conn = dbmod.connect()
    conn.execute("UPDATE activities SET name='3#墩预制梁吊装' WHERE code='A1030'")
    conn.execute("UPDATE activities SET name='路基段夜间交通导改' WHERE code='A3020'")
    find_svc.create(conn, env["project_id"], "safety", "hazard", "high", "吊装作业警戒缺失", {"category": "吊装"}, fingerprint="t:pc1",
                    assignee_id=env["users"]["tech1"], due_days=2)
    find_svc.create(conn, env["project_id"], "safety", "hazard", "medium", "临边护栏缺失", {"category": "临边"}, fingerprint="t:pc2",
                    assignee_id=env["users"]["tech1"], due_days=2)
    conn.close()
    confirm_hazards()


def test_risk_kind_rules():
    assert svc.risk_kind({"name": "预制梁吊装", "category": "lifting"}) == "lifting"
    assert svc.risk_kind({"name": "沉箱出运", "category": "marine"}) == "marine"
    assert svc.risk_kind({"name": "基坑开挖", "category": "earth"}) == "deep"
    assert svc.risk_kind({"name": "路基填筑", "category": "earth"}) is None
    assert svc.risk_kind({"name": "夜间交通导改", "category": "general"}) == "traffic"
    assert svc.risk_kind({"name": "承台混凝土浇筑", "category": "concrete"}) is None


def test_card_lists_high_risk_with_history(env):
    _world(env)
    d = login("ss").get("/api/precontrol").json()
    by = {(r["code"], r["company"]): r for r in d["items"]}
    lift = by[("A1030", "甲")]
    assert lift["kind"] == "lifting" and lift["hist"]["n"] == 1 and lift["hist"]["cats"] == {"吊装": 1}   # 临边那条不算吊装同类
    assert "近 30 天同类隐患 1 次" in lift["focus"]
    assert by[("A3020", "乙")]["kind"] == "traffic"
    assert all(r["code"] not in ("A1040", "A2020") for r in d["items"])      # 普通作业不进
    assert lift["notice"] is None


def test_only_safety_sup(env):
    _world(env)
    for u in ("pm", "eng", "tech1", "sa_a"):
        assert login(u).get("/api/precontrol").status_code == 403, u


def test_notice_confirm_issues_and_ack_closes(env):
    _world(env)
    ss = login("ss")
    d = ss.post("/api/gongbi/docs", json={"kind": "precontrol_notice", "ref": "A1030|甲"})
    assert d.status_code == 200, d.text
    doc = d.json()
    assert doc["check"] == [] and "近 30 天同类安全隐患 1 次" in doc["narrative"]["fact"] and "吊装方案已批" in doc["narrative"]["requirement"]
    assert "罚" not in str(doc["narrative"])
    sa = login("sa_a")
    assert sa.get("/api/gongbi/letters").json() == []                       # 草稿不下发
    assert login("eng").put(f"/api/gongbi/docs/{doc['id']}", json={"narrative": doc["narrative"], "confirm": True}).status_code == 403
    assert ss.put(f"/api/gongbi/docs/{doc['id']}", json={"narrative": doc["narrative"], "confirm": True}).status_code == 200
    got = sa.get("/api/gongbi/letters").json()
    assert len(got) == 1 and got[0]["status"] == "issued"
    assert login("sa_b").get("/api/gongbi/letters").json() == []            # 乙单位看不到
    assert sa.post(f"/api/gongbi/letters/{got[0]['id']}/ack").status_code == 200
    assert sa.get("/api/gongbi/letters").json()[0]["status"] == "closed"     # 签收即销项
    row = next(r for r in ss.get("/api/precontrol").json()["items"] if r["code"] == "A1030")
    assert row["notice"]["letter_status"] == "已销项"


def test_notice_rejects_unknown_activity(env):
    _world(env)
    r = login("ss").post("/api/gongbi/docs", json={"kind": "precontrol_notice", "ref": "A1040|甲"})
    assert r.status_code in (400, 404)


def test_refs_for_docs_page(env):
    _world(env)
    refs = login("ss").get("/api/gongbi/refs?kind=precontrol_notice").json()
    assert any(r["key"] == "A1030|甲" and "近 30 天同类隐患 1 次" in r["notice"] for r in refs)
    assert login("eng").get("/api/gongbi/refs?kind=precontrol_notice").json() == []
