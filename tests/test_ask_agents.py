"""智能应用页：每个智能体都能点进去提问（离线 mock 模型）。
- 7 个“办事型”智能体（催办、指挥、档案、现场圈、放行、材料、资料）各有问答范围，只查自己的产出；
- 能不能问按岗位把关，与各自页面/接口的查看权限一致：/api/agents 的 askable 与 /api/ask 的 403 同一函数（qa.can_ask）；
- 项目经理口径不变：只列 PM_AGENTS，全部可问。"""
from conftest import login
from test_pm_scope import _eng_sup

from app import roles as R
from app.agents import AGENTS
from app.services import qa

DOERS = ("chaser", "dispatch", "archivist", "circle", "gate", "material", "records")


def test_every_agent_has_ask_scope():
    assert {a["key"] for a in AGENTS} - set(qa.SCOPES) == {"analyst"}   # 问工笔 = 不指定范围
    assert set(qa.EXTRA_FACTS) == {s for k in DOERS for s in qa.SCOPES[k]["sources"]}


def test_engineering_supervisor_can_ask_all_doers(env):
    _eng_sup(env)
    eng = login("eng")
    cards = {a["key"]: a["askable"] for a in eng.get("/api/agents").json()["agents"]}
    assert all(cards.values()), [k for k, v in cards.items() if not v]
    for ag in DOERS:
        r = eng.post("/api/ask", json={"question": "最近有什么情况？", "agent": ag})
        assert r.status_code == 200, (ag, r.text)
        assert r.json()["agent"] == ag
    for ag in ("chaser", "gate", "material"):        # 这几个即使没有业务数据也有一行汇总事实
        assert eng.post("/api/ask", json={"question": "怎么样", "agent": ag}).json()["facts_count"] >= 1, ag


def test_ask_follows_each_page_permission(env):
    admin = login("admin")
    cards = {a["key"]: a["askable"] for a in admin.get("/api/agents").json()["agents"]}
    assert cards["safety"] and cards["analyst"]
    assert not cards["chaser"] and not cards["material"] and not cards["gate"]   # 系统管理员不参与这些业务页
    for ag in ("chaser", "material", "gate", "records", "archivist"):              # 卡片不给“提问”，接口同样拒绝
        assert admin.post("/api/ask", json={"question": "最近怎么样", "agent": ag}).status_code == 403, ag
    assert admin.post("/api/ask", json={"question": "最近怎么样", "agent": "safety"}).status_code == 200


def test_project_manager_cards_unchanged_and_all_askable(env):
    pm = login("pm")
    cards = {a["key"]: a["askable"] for a in pm.get("/api/agents").json()["agents"]}
    assert set(cards) <= R.PM_AGENTS and all(cards.values())
    for ag in DOERS:
        assert pm.post("/api/ask", json={"question": "进度怎么样", "agent": ag}).status_code == 403, ag


def test_verify_answer_keeps_grounded_numbers_and_counts():
    """有出处的数字、按检索条数计数都保留；不存在的出处只删编号，不整段降级。"""
    from app.ai import qa_agent
    facts = "[W1] 2026-09-20 A1010 桩基滞后（状态open，滞后3天，浮时-2天）\n[W2] 2026-09-21 A1020 承台未开工（滞后1天）"
    ans, problems = qa_agent.verify_answer({"answer": "共 2 条预警：A1010 滞后 3 天 [W1]，另见 [W2][L9]", "refs": ["W1", "W2", "L9"]}, facts, "有哪些预警？", 2)
    assert problems == [] and ans["refs"] == ["W1", "W2"] and "[L9]" not in ans["answer"] and "3 天" in ans["answer"]
