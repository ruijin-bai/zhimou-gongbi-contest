"""I11（验收 2026-10-11）：周报定性与表格偏差、预警矛盾时拦下；问工笔问安全隐患时检索范围含安全台账。离线模拟模型。"""
from app.ai import report_agent

from conftest import confirm_hazards, login
from test_site import _world

FACTS = {"progress": [{"code": "CV110", "name": "涵洞", "gap": -2.8}, {"code": "BR110", "name": "桩基", "gap": -0.3}, {"code": "RD100", "name": "路基", "gap": 1.0}],
         "alerts": [{"id": 12, "level": "red", "status": "confirmed"}]}


def test_stance_fix_removes_on_plan_claim():
    nar = {"progress": "本周各作业基本按计划推进。BR110 桩基继续施工。", "overview": "本周完成路基填筑，整体进展顺利。", "safety": "无"}
    issues = report_agent.stance_fix(nar, FACTS)
    assert {i["field"] for i in issues} == {"progress", "overview"}
    assert "按计划" not in nar["progress"] and "CV110 涵洞 偏差 -2.8 个百分点" in nar["progress"] and "[W12]" in nar["progress"]
    assert "BR110 桩基继续施工" in nar["progress"] and "进展顺利" not in nar["overview"] and "路基填筑" in nar["overview"]


def test_stance_fix_quiet_when_on_plan():
    nar = {"progress": "各作业基本按计划推进。"}
    assert report_agent.stance_fix(nar, {"progress": [{"code": "A", "name": "a", "gap": 0.5}], "alerts": []}) == []
    assert nar["progress"] == "各作业基本按计划推进。"


def test_free_question_about_hazards_searches_safety_ledger(env):
    _world(env)
    confirm_hazards()
    r = login("pm").post("/api/ask", json={"question": "本周有哪些安全隐患需要我拍板？"}).json()
    assert "安全隐患台账" in r["scope"]
    assert any(x.startswith("F") for x in r["refs"])   # 引用了安全台账里的隐患
