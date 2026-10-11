"""资料欠账：完工作业缺隐蔽验收、已用未复试；补齐通知工程主管确认后才下发给负责单位技术员，签收即销项。合成数据。"""
from conftest import login
from test_site import D, _mklog, _world

from app import db as dbmod


def _finish_a1010(env, batch=True):
    conn = dbmod.connect()
    _mklog(conn, env, "tech1", D(0), {"works": [{"part": "3#墩", "process": "桩基", "desc": "3#墩桩基全部完成", "activity_code": "A1010"}], "labor": [], "machinery": [], "issues": []},
           progress=[("A1010", 2, "根", 100)])
    if batch:
        dbmod.insert(conn, "material_batches", {"project_id": env["project_id"], "category": "rebar", "name": "HRB400 钢筋", "batch_no": "B-01", "arrived_at": D(8),
                                                "retest_status": "pending", "created_at": dbmod.now_iso()})
    conn.close()


def test_done_activity_lists_conceal_and_material_debts(env):
    _world(env)
    _finish_a1010(env)
    d = login("eng").get("/api/doc-debt").json()
    it = next(i for i in d["items"] if i["code"] == "A1010")
    kinds = [x["type"] for x in it["debts"]]
    assert "conceal" in kinds and "material" in kinds
    assert "周确认累计完成 100%" in it["basis"] and it["units"] == ["甲"]
    assert any("HRB400" in x["text"] and "待取样" in x["text"] for x in it["debts"])
    assert all(i["code"] != "A3010" for i in d["items"])          # 没完工的不列


def test_not_done_no_debt(env):
    _world(env)
    assert login("eng").get("/api/doc-debt").json()["items"] == []


def test_only_eng(env):
    _world(env)
    for u in ("pm", "tech1", "ss"):
        assert login(u).get("/api/doc-debt").status_code == 403, u


def test_notice_issue_and_ack(env):
    _world(env)
    _finish_a1010(env)
    eng = login("eng")
    assert eng.post("/api/gongbi/docs", json={"kind": "docdebt_notice", "ref": "A1010|乙"}).status_code == 400   # 乙不负责该作业
    doc = eng.post("/api/gongbi/docs", json={"kind": "docdebt_notice", "ref": "A1010|甲"}).json()
    assert doc["check"] == [] and "HRB400" in doc["narrative"]["fact"] and "罚" not in str(doc["narrative"])
    t1 = login("tech1")
    assert t1.get("/api/gongbi/letters").json() == []
    assert eng.put(f"/api/gongbi/docs/{doc['id']}", json={"narrative": doc["narrative"], "confirm": True}).status_code == 200
    got = t1.get("/api/gongbi/letters").json()
    assert len(got) == 1 and got[0]["area"] == "quality"
    assert t1.post(f"/api/gongbi/letters/{got[0]['id']}/ack").status_code == 200
    assert t1.get("/api/gongbi/letters").json()[0]["status"] == "closed"
    it = next(i for i in eng.get("/api/doc-debt").json()["items"] if i["code"] == "A1010")
    assert it["notice"]["letter_status"] == "已销项"


def test_refs_for_docs_page(env):
    _world(env)
    _finish_a1010(env)
    refs = login("eng").get("/api/gongbi/refs?kind=docdebt_notice").json()
    assert [r["key"] for r in refs] == ["A1010|甲"]
