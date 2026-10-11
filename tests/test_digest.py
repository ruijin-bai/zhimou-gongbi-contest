"""列表页 AI 小结（services/digest.py、routers/digest.py）：数字校验、退回规则版、缓存、项目经理口径。"""
from __future__ import annotations

from conftest import login

from app.services import digest as d

FACTS = {"lead": "共 3 项待处理预警（黄 3）", "key": [],
         "groups": ["3 项“应开未开”（黄色）：CV110、CV120、RD120，滞后 9–17 天，总浮时最少还有 46 天"]}


def test_check_allows_numbers_from_facts_and_codes():
    assert d.check("3 项应开未开（CV110、CV120、RD120）滞后 9–17 天，浮时还有 46 天，可以一次确认。", FACTS) == []


def test_check_rejects_invented_numbers():
    assert d.check("建议 2 天内处理 CV110，预计节省 30%", FACTS) == ["2", "30"]


def test_mock_text_only_reuses_facts():
    t = d.mock_text({"key": ["BR110 立交桥桩基施工：应开未开 6 天：影响 立交桥主体完成"], "groups": FACTS["groups"]}, "x")
    assert t.startswith("先处理BR110 立交桥桩基施工") and "可以合并一次处理" in t
    assert d.mock_text({}, "规则版") == "规则版"


def test_endpoint_returns_and_caches(env):
    c = login("pm")
    body = {"kind": "alerts", "facts": FACTS, "fallback": FACTS["lead"]}
    r1 = c.post("/api/digest", json=body).json()
    assert r1["source"] == "mock" and r1["text"]           # 离线模拟：照样核对数字，标 mock
    r2 = c.post("/api/digest", json=body).json()
    assert not r2.get("cached")                              # 只缓存真实模型成功的小结（mock、失败都不缓存）
    assert c.post("/api/digest", json={**body, "kind": "nope"}).status_code == 400


def test_pm_cannot_digest_logs(env):
    assert login("pm").post("/api/digest", json={"kind": "logs", "facts": {}, "fallback": "x"}).status_code == 403


def test_digest_only_for_harbour_roles(env):
    """AI 小结只给港湾岗位：技术员、分包安全员、系统管理员 403，不发出模型调用。"""
    from conftest import login
    from test_roles import _team
    _team(env)
    body = {"kind": "safety", "facts": {"lead": "未闭环安全隐患共 1 项"}, "fallback": "规则归并文字"}
    for who in ("tech1", "sa_a", "admin"):
        assert login(who).post("/api/digest", json=body).status_code == 403, who
    assert login("ss").post("/api/digest", json=body).status_code == 200
