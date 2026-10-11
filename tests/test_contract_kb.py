"""合同 / 规范条款库：导入切分、检索、问工笔引用 [C编号]、权限。条款文字为测试自拟的合成文本，不是任何真实合同。"""
from conftest import login
from test_roles import _team

from app.services import contract_kb as svc

TEXT = """Sub-Clause 20.2 承包商索赔
承包商认为有权获得延期或额外付款时，应在知悉事件后 28 天内向工程师发出通知。
逾期未通知的，承包商丧失相应权利。
Sub-Clause 8.5 竣工时间的延长
因异常不利的气候条件导致延误的，承包商可按第 20.2 款提出延期。
第 12 条 计量
工程量按实际完成并经检验合格的数量计量。
"""


def test_split_clauses():
    cs = svc.split_clauses(TEXT)
    assert [c["clause_no"] for c in cs] == ["20.2", "8.5", "12"]
    assert cs[0]["title"] == "承包商索赔" and "28 天内" in cs[0]["text"]
    plain = svc.split_clauses("第一段没有条款号。\n\n第二段也没有。")
    assert plain[0]["clause_no"] == "段1"


def test_import_search_and_roles(env):
    _team(env)
    eng = login("eng")
    r = eng.post("/api/contract/docs", data={"title": "合同条件（合成样例）", "kind": "contract", "text": TEXT})
    assert r.status_code == 200 and r.json()["clauses"] == 3
    hits = eng.get("/api/contract/search?q=20.2").json()["items"]
    assert hits[0]["clause_no"] == "20.2"
    hits = eng.get("/api/contract/search?q=气候延误").json()["items"]
    assert hits and hits[0]["clause_no"] == "8.5"
    assert eng.get(f"/api/contract/clause/{hits[0]['id']}").json()["clause_no"] == "8.5"
    assert login("ss").post("/api/contract/docs", data={"title": "x", "text": TEXT}).status_code == 403    # 安全主管只读
    pm = login("pm")   # 项目经理只关注进度、质量、安全：条款库只读，不导入、不删除
    assert pm.post("/api/contract/docs", data={"title": "x", "text": TEXT}).status_code == 403
    assert pm.delete(f"/api/contract/docs/{r.json()['id']}").status_code == 403
    assert pm.get("/api/contract/docs").json()["can_import"] is False and pm.get("/api/contract/search?q=20.2").json()["items"]
    assert login("ss").get("/api/contract/docs").status_code == 200
    assert login("tech1").get("/api/contract/docs").status_code == 403


def test_upload_docx(env):
    _team(env)
    import io
    from docx import Document
    d = Document()
    for line in TEXT.splitlines():
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    r = login("eng").post("/api/contract/docs", data={"kind": "particular"}, files={"file": ("特殊条款.docx", buf.getvalue(), "application/octet-stream")})
    assert r.status_code == 200 and r.json()["title"] == "特殊条款" and r.json()["clauses"] == 3


def test_ask_cites_clause(env):
    _team(env)
    pm = login("pm")
    login("eng").post("/api/contract/docs", data={"title": "合同条件（合成样例）", "kind": "contract", "text": TEXT})
    a = pm.post("/api/ask", json={"question": "合同里索赔通知要在多少天内发出？"}).json()
    assert any(x.startswith("C") for x in a["refs"]), a
    b = pm.post("/api/ask", json={"question": "3#墩桩基进度怎么样"}).json()     # 不问合同时不引用条款库
    assert not any(x.startswith("C") for x in b["refs"])


def test_ask_no_clause_for_non_readers(env):
    """领导、技术员等不在条款库读者范围：问合同也不引用 [C编号]（否则回答里的链接点开 403）。"""
    from test_roles import _add
    _team(env)
    _add(env, "boss", "leader", "领导")
    login("eng").post("/api/contract/docs", data={"title": "合同条件（合成样例）", "kind": "contract", "text": TEXT})
    for who in ("boss", "tech1"):
        a = login(who).post("/api/ask", json={"question": "合同里索赔通知要在多少天内发出？"}).json()
        assert not any(x.startswith("C") for x in a.get("refs", [])), (who, a)
