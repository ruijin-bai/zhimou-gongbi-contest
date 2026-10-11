"""统一 AI 输入框：意图判断（提问 / 指令 / 纪要）。规则优先，拿不准再问模型，仍不确定交给人选。"""
from types import SimpleNamespace

import pytest

from conftest import login
from test_roles import _add, _team

from app.services import cmd as cmd_svc


@pytest.mark.parametrize("text,intent", [
    ("上周关键作业滞后原因", "ask"),
    ("超期整改有多少", "ask"),
    ("今天安排了什么？", "ask"),
    ("本周质量问题情况", "ask"),
    ("3#墩钢筋整改派给乙分包李工，明天下班前", "cmd"),
    ("催办5#墩临边防护", "cmd"),
    ("请李工去处理5#墩", "cmd"),
    ("第一项\n第二项\n第三项\n第四项", "minutes"),
    ("议题一：进度。结论：加快。行动项：乙分包下周五前完成3#墩钢筋绑扎，并复查整改，安全员乙本周内跟进临边防护", "minutes"),
    ("会议" + "说了很多事情，" * 30, "minutes"),
])
def test_rules_confident(text, intent):
    r = cmd_svc.intent_rules(text)
    assert r["confident"] and r["intent"] == intent and r["options"] == [intent]


@pytest.mark.parametrize("text,options", [
    ("为什么3#墩整改没催办", ["ask", "cmd"]),           # 既像提问又像指令
    ("3#墩钢筋整改明天完成", ["ask", "cmd"]),            # 像事项描述，没说派给谁
    ("一二三。四五六。七八九。" + "十" * 60, ["ask", "minutes"]),   # 多句陈述
])
def test_rules_ambiguous_offers_choices(text, options):
    r = cmd_svc.intent_rules(text)
    assert not r["confident"] and r["options"] == options


def test_endpoint_commander_vs_others(env):
    _team(env)
    _add(env, "boss", "leader", "领导")
    pm = login("pm")
    r = pm.post("/api/ai/intent", json={"text": "3#墩钢筋整改派给乙分包李工，明天下班前"}).json()
    assert r["intent"] == "cmd" and r["confident"] and r["source"] == "rule"
    r = pm.post("/api/ai/intent", json={"text": "上周关键作业滞后原因"}).json()
    assert r["intent"] == "ask" and r["confident"]
    r = pm.post("/api/ai/intent", json={"text": "为什么3#墩整改没催办"}).json()      # 离线模型：不调模型，交给人选
    assert not r["confident"] and [o["intent"] for o in r["options"]] == ["ask", "cmd"] and r["source"] == "rule"
    assert [o["label"] for o in r["options"]] == ["问工笔", "下指令"]
    # 判断只是判断：不解析指令、不产生纪要草稿、不派单
    from app import db as dbmod
    conn = dbmod.connect()
    assert dbmod.one(conn, "SELECT COUNT(*) n FROM meetings")["n"] == 0
    assert dbmod.one(conn, "SELECT COUNT(*) n FROM events WHERE kind='cmd.parse'")["n"] == 0
    conn.close()
    # 没有派单权的岗位只有“提问”，哪怕话像指令也不会被当成指令
    for u in ("tech1", "boss"):
        r = login(u).post("/api/ai/intent", json={"text": "3#墩钢筋整改派给乙分包李工，明天下班前"}).json()
        assert r["intent"] == "ask" and r["confident"] and [o["intent"] for o in r["options"]] == ["ask"], u
    assert login("pm").post("/api/ai/intent", json={"text": ""}).json()["intent"] == "ask"


def _fake_model(monkeypatch, data, calls):
    monkeypatch.setattr(cmd_svc, "settings", SimpleNamespace(ai_provider="real"))

    def fake(kind, **kw):
        calls.append((kind, kw["user"]))
        if isinstance(data, Exception):
            raise data
        return SimpleNamespace(data=kw["validate"](data), run_id=1)
    monkeypatch.setattr(cmd_svc, "call_json", fake)


def test_model_breaks_tie_only_when_rules_unsure(env, monkeypatch):
    _team(env)
    calls: list = []
    _fake_model(monkeypatch, {"intent": "cmd", "confidence": 0.9, "reason": "要求催办"}, calls)
    pm = login("pm")
    # 规则已确定：不调模型
    assert pm.post("/api/ai/intent", json={"text": "上周关键作业滞后原因"}).json()["source"] == "rule" and not calls
    # 规则拿不准：问一次轻量模型，置信度够就采纳
    r = pm.post("/api/ai/intent", json={"text": "为什么3#墩整改没催办"}).json()
    assert len(calls) == 1 and calls[0][0] == "cmd.intent"
    assert r["intent"] == "cmd" and r["confident"] and r["source"] == "model"


def test_model_low_confidence_or_failure_falls_back_to_choices(env, monkeypatch):
    _team(env)
    pm = login("pm")
    calls: list = []
    _fake_model(monkeypatch, {"intent": "minutes", "confidence": 0.4, "reason": "不确定"}, calls)
    r = pm.post("/api/ai/intent", json={"text": "为什么3#墩整改没催办"}).json()
    assert not r["confident"] and r["source"] == "rule"
    assert [o["intent"] for o in r["options"]] == ["minutes", "ask", "cmd"]   # 模型倾向排前，仍由人选
    _fake_model(monkeypatch, RuntimeError("模型不可用"), calls)
    r = pm.post("/api/ai/intent", json={"text": "为什么3#墩整改没催办"}).json()
    assert not r["confident"] and [o["intent"] for o in r["options"]] == ["ask", "cmd"]
