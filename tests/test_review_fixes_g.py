"""独立审查（G/F3–F5 收尾）发现问题的回归测试：催办周确认链到被催的那一周、技术员报的问题不标“AI 发现”、
空草稿由后端按内容判定、局域网地址只认 RFC1918、退回原因用“项目部”。"""
import ipaddress
from pathlib import Path

from app.services import nudge as nudge_svc
from app.services import quick as quick_svc
from app.services import todo as todo_svc

ROOT = Path(__file__).resolve().parents[1]


def test_week_nudge_link_points_to_the_nudged_week():
    link = nudge_svc._notif_link([{"link": "week?date=2026-09-14", "overdue": 3}], {"role": "tech"})
    assert link == {"route": "week", "params": {"date": "2026-09-14"}}
    src = (ROOT / "app/services/nudge.py").read_text("utf-8")
    assert 'f"week?date={ws.isoformat()}"' in src


def test_alert_and_reported_findings_not_tagged_ai_found():
    assert todo_svc.AI_TAG["alert"] != "AI 发现"               # 预警是规则判定，AI 只做原因分析
    src = (ROOT / "app/services/todo.py").read_text("utf-8")
    assert 'q.get("kind") != "reported"' in src                   # 技术员日志里报的问题不是 AI 发现
    assert '"kind": f["kind"]' in (ROOT / "app/services/brief.py").read_text("utf-8")


def test_lan_ip_only_rfc1918(monkeypatch):
    quick_svc._LAN_CACHE.clear()
    monkeypatch.setattr(quick_svc, "_ip_candidates", lambda: ["198.18.0.1", "100.64.3.2"])
    assert quick_svc.lan_ip() is None                             # VPN/代理虚拟网卡、CGNAT 都不给
    quick_svc._LAN_CACHE.clear()
    monkeypatch.setattr(quick_svc, "_ip_candidates", lambda: ["198.18.0.1", "192.168.1.23"])
    assert quick_svc.lan_ip() == "192.168.1.23"
    quick_svc._LAN_CACHE.clear()


def test_void_reason_uses_project_department_wording():
    src = (ROOT / "app/services/logs.py").read_text("utf-8")
    assert "港湾退回：" not in src and "项目部退回：" in src


def test_empty_draft_judged_by_backend_and_hidden_count_shown(env):
    from conftest import login
    from test_trust import _setup_daily
    _setup_daily(env) if _setup_daily.__code__.co_argcount else _setup_daily()
    tech = login("tech1")
    rows = tech.get("/api/logs").json()
    assert all("empty" in r for r in rows)
    js = (ROOT / "app/static/views.js").read_text("utf-8")
    assert "l.empty" in js and "已隐藏" in js
