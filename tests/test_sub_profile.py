"""分包综合履约画像：数字由系统统计；项目经理那一份不含日志/周确认；不排名、不写罚款。合成数据、离线模拟模型。"""
from conftest import login
from test_site import _world

from app.services import sub_profile as svc


def test_profiles_by_company(env):
    _world(env)
    d0 = login("eng").get("/api/sub-profiles").json()   # 打开页面不调用模型，评语先是系统统计句（验收 I9）
    assert all(p["eval"]["source"] == "rule" and p["eval"].get("can_ai") for p in d0["items"])
    d = login("eng").get("/api/sub-profiles?run=1").json()
    by = {p["company"]: p for p in d["items"]}
    assert set(by) == {"甲", "乙"} and d["with_logs"]
    a = by["甲"]
    assert a["safety"]["n"] == 1 and a["safety"]["high"] == 1 and a["safety"]["open"] == 1     # world：3#墩临边防护缺失，派给甲的技术员
    assert "logs" in a and "周确认" in a["facts"]
    assert a["eval"]["source"] == "mock" and "罚" not in a["eval"]["text"]


def test_pm_has_no_log_rate(env):
    _world(env)
    d = login("pm").get("/api/sub-profiles").json()
    assert not d["with_logs"]
    for p in d["items"]:
        assert "logs" not in p and "周确认" not in p["facts"] and "周确认" not in p["eval"]["text"] and "日志" not in p["eval"]["text"]


def test_roles(env):
    _world(env)
    for u in ("tech1", "sa_a", "admin"):
        assert login(u).get("/api/sub-profiles").status_code == 403, u
    assert login("ss").get("/api/sub-profiles").status_code == 200


def test_guard():
    f = "安全隐患 3 项，已闭环 2 项"
    assert svc.guard("闭环 2 项，较好", f) == []
    assert svc.guard("闭环 5 项，排名第一", f) == ["数字5无出处", "出现罚款/排名字样"]
