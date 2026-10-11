"""工效偏低 / 滞后原因诊断：程序统计日志信号（天数 + 日志编号），AI 只写一句归因并经核对。合成数据、离线模拟模型。"""
from conftest import login
from test_site import D, _world

from app import db as dbmod
from app.services import progress_agent
from app.services import slow_diag as svc


def _alert(env, code="A1010"):
    conn = dbmod.connect()
    imp = dbmod.one(conn, "SELECT id FROM p6_imports WHERE project_id=? ORDER BY id DESC LIMIT 1", (env["project_id"],))
    aid = dbmod.insert(conn, "alerts", {"project_id": env["project_id"], "import_id": imp["id"], "fingerprint": "t:sd", "activity_code": code, "activity_name": "3#墩桩基施工",
                                        "level": "red", "rule": "behind", "title": "滞后", "detail_json": "{}", "status": "confirmed", "first_detected": D(1),
                                        "last_detected": D(1), "as_of": D(1)})
    conn.close()
    return aid


def test_signals_equipment_is_main(env):
    ids = _world(env)
    conn = dbmod.connect()
    s = svc.signals(conn, env["project_id"], "A1010")
    conn.close()
    eq = next(x for x in s["signals"] if x["type"] == "equipment")
    assert s["main"] == "equipment" and eq["days"] == 2 and set(eq["refs"]) == {ids["e1"], ids["e2"]}
    assert all(x["type"] != "weather" for x in s["signals"])          # 乙的暴雨停工挂在 A3010，不算到 A1010
    assert s["avg_labor"] == 18                                          # 钢筋工 12 + 普工 6
    t = svc.rule_text(s)
    assert t.startswith("主因：设备（近 14 天有 2 天日志提到") and f"[L{ids['e1']}]" in t


def test_no_logs_no_guess(env):
    _world(env)
    conn = dbmod.connect()
    s = svc.signals(conn, env["project_id"], "A2010")
    conn.close()
    assert s["signals"] == [] and "无法判断" in svc.rule_text(s)


def test_guard():
    s = {"signals": [{"type": "equipment", "name": "设备", "days": 2, "refs": [7, 8], "samples": []}], "log_ids": [7, 8], "logs": 2}
    assert svc.guard("主因是设备故障，2 天日志提到[L7][L8]", s) == []
    assert svc.guard("主因是设备故障，5 天日志提到[L9]", s) == ["引用L9不在统计里", "数字5无出处"]


def test_api_and_roles(env):
    ids = _world(env)
    aid = _alert(env)
    d = login("pm").get(f"/api/alerts/{aid}/slow-diag").json()
    assert d["main"] == "equipment" and d["source"] == "mock" and f"[L{ids['e1']}]" in d["text"]
    assert login("tech1").get(f"/api/alerts/{aid}/slow-diag").status_code == 403


def test_recovery_prompt_includes_causes(env, monkeypatch):
    _world(env)
    seen = {}
    real = progress_agent.call_json

    def spy(kind, **kw):
        seen["user"] = kw["user"]
        return real(kind, **kw)
    monkeypatch.setattr(progress_agent, "call_json", spy)
    conn = dbmod.connect()
    progress_agent.recovery(conn, env["project_id"], None)
    conn.close()
    assert "【工效偏低原因（系统按日志统计）】" in seen["user"]
