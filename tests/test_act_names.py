"""P6 作业代码 → 简短中文名（app/services/act_names.py）：命名规则、文字替换、接口出口、AI 校验不受影响。"""
from conftest import login, sample_xer

from app.ai.report_agent import number_guard
from app.services import act_names as A

ROWS = [
    {"code": "CON-1000", "name": "临时便道施工：Temporary Access Road Construction CH0+000 – CH3+500", "wbs_path": "施工 / 道路工程 / 临时工程"},
    {"code": "CON-2220", "name": "连续配筋混凝土路面（CRCP）：CH0+950～CH2+140 左幅", "wbs_path": "施工 / 道路工程 / 路面工程"},
    {"code": "CON-2450", "name": "排水工程：CH0+950-CH4+900 右幅 边沟 (Tpye:1)", "wbs_path": "施工 / 道路工程 / 附属工程"},
    {"code": "CON-1450", "name": "涵洞工程：CH4+900 圆管涵 (1-φ1.2m)", "wbs_path": "施工 / 道路工程 / 涵洞工程"},
    {"code": "CON-4230", "name": "墩柱施工", "wbs_path": "施工 / 桥梁工程 / 一号泻湖桥（2600m） / 钢栈桥平台段 / 43号墩位"},
    {"code": "CON-6820", "name": "墩柱施工", "wbs_path": "施工 / 桥梁工程 / 一号泻湖桥（2600m） / 陆域回填平台段 / 2号墩位"},
    {"code": "CON-4980", "name": "T梁预制（50片）", "wbs_path": "施工 / 桥梁工程 / 梁板预制"},
    {"code": "CON-4990", "name": "T梁预制（50片）", "wbs_path": "施工 / 桥梁工程 / 梁板预制"},
    {"code": "ENG-0001", "name": "Survey and Setting Out", "wbs_path": "Engineering"},
]


def test_short_names_follow_p6_names_without_inventing():
    mp = A.build(ROWS)
    assert mp["CON-1000"] == "临时便道施工 CH0+000–CH3+500"           # 去掉夹在中文名里的英文整句
    assert mp["CON-2220"] == "连续配筋混凝土路面 CH0+950～CH2+140 左幅"  # 去掉（CRCP）
    assert mp["CON-2450"] == "排水工程 CH0+950-CH4+900 右幅 边沟"        # 去掉 (Tpye:1)
    assert mp["CON-1450"] == "涵洞工程 CH4+900 圆管涵 (1-φ1.2m)"         # 尺寸照留
    # 重名作业按 WBS 末级区分；WBS 也完全相同的保持原名（代码在悬停里看），不编号、不造名
    assert mp["CON-4230"] == "43号墩位 墩柱施工" and mp["CON-6820"] == "2号墩位 墩柱施工"
    assert mp["CON-4980"] == mp["CON-4990"] == "T梁预制（50片）"
    # 没有中文名：照用原名并标“中文名待补”
    assert mp["ENG-0001"] == "Survey and Setting Out" + A.MISSING_TAG
    assert mp.raw["CON-2220"] == ROWS[1]["name"]


def test_humanize_replaces_codes_in_text():
    mp = A.build(ROWS)
    t = ("CON-4230 墩柱施工：比计划慢 3 天；[CON-2220] 连续配筋混凝土路面（CRCP）：CH0+950～CH2+140 左幅；"
         "关键作业 CON-6820「墩柱施工」今天有进展吗？涉及 43号墩位 墩柱施工（CON-4230）。单独写 CON-1000。")
    out = A.humanize(t, mp)
    assert "CON-" not in out
    assert out.startswith("43号墩位 墩柱施工：比计划慢 3 天；连续配筋混凝土路面 CH0+950～CH2+140 左幅；")
    assert "关键作业 2号墩位 墩柱施工今天有进展吗" in out
    assert "涉及 43号墩位 墩柱施工。" in out and "单独写 临时便道施工 CH0+000–CH3+500。" in out
    # 不在计划里的代码、像代码的别的词不动
    assert A.humanize("C30 混凝土，PC200 挖掘机，CON-9999", mp) == "C30 混凝土，PC200 挖掘机，CON-9999"


def test_enrich_keeps_code_fields_and_adds_short_name():
    mp = A.build(ROWS)
    data = {"items": [{"code": "CON-4230", "name": "墩柱施工", "title": "CON-4230 墩柱施工：滞后", "route": "gate?open=CON-4230"}],
            "alerts": [{"activity_code": "CON-1000", "activity_name": ROWS[0]["name"], "refs": ["CON-1000"]}],
            "lines": ["作业 CON-6820 墩柱施工"]}
    A.enrich(data, mp)
    it = data["items"][0]
    assert it["code"] == "CON-4230" and it["short_name"] == "43号墩位 墩柱施工"
    assert it["title"] == "43号墩位 墩柱施工：滞后" and it["route"] == "gate?open=CON-4230"
    al = data["alerts"][0]
    assert al["activity_code"] == "CON-1000" and al["activity_short"] == "临时便道施工 CH0+000–CH3+500" and al["refs"] == ["CON-1000"]
    assert data["lines"] == ["作业 2号墩位 墩柱施工"]


def test_ai_facts_carry_short_name_and_number_guard_still_holds():
    mp = A.build(ROWS)
    facts = "\n".join(f"{A.fmt(r['code'], r['name'], mp)}：累计 40%" for r in ROWS[:5])
    assert "43号墩位 墩柱施工（CON-4230）：累计 40%" in facts
    # 叙述按简称写（简称里的“43号”“CH0+950”出自事实），数字守卫照常放行；自造数字照常拦截
    assert number_guard({"progress": "43号墩位 墩柱施工累计 40%，临时便道施工 CH0+000–CH3+500 累计 40%。"}, facts) == []
    assert number_guard({"progress": "43号墩位 墩柱施工累计 55%。"}, facts) == [{"field": "progress", "number": "55"}]
    from app.ai.client import ACT_NAME_RULE, _ACT_REF
    assert _ACT_REF.search(facts) and "不写作业代码" in ACT_NAME_RULE


def test_slow_problems_recognizes_short_names():
    from app.services import progress_agent
    mp = A.build(ROWS)
    tok = A.CURRENT.set(mp)
    try:
        acts = [{"code": r["code"], "name": r["name"]} for r in ROWS[4:6]]
        slow = [{"code": "CON-4230", "name": "墩柱施工"}]
        assert progress_agent.slow_problems("43号墩位 墩柱施工工效偏低", slow, acts) == []
        bad = progress_agent.slow_problems("2号墩位 墩柱施工工效偏低", slow, acts)
        assert bad and "CON-6820" in bad[0]
    finally:
        A.CURRENT.reset(tok)


def test_api_responses_show_short_names(env):
    eng = login("admin")
    assert eng.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    items = eng.get("/api/p6/activities").json()["items"]
    a = next(x for x in items if x["code"] == "A1020")
    assert a["short_name"] == "3#墩承台钢筋绑扎" and a["name"] == "3#墩承台钢筋绑扎"
    assert all("short_name" in x for x in items)
    # 未登录的接口不受影响
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/api/p6/activities").status_code == 401
