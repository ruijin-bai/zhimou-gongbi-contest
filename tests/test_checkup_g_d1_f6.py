"""体检 G（开发痕迹）、D1（待办标注 AI 起草/发现）、F6（一线措辞）：

- G 扫码页：后端给出本机局域网地址建议（取不到不给），二维码默认用“局域网 IP:端口”；
  “二维码网址前缀”这一部署配置只给系统管理员（服务端保存），安全员页面只给一句大白话；
- G 审计：开发态登录默认不进审计列表与系统概况，可选参数 dev=1 再显示；
- D1 待办：AI 起草/发现的来源带 ai_tag，人工来源不带；前端用中港蓝 chip 展示；
- F6 一线（技术员、分包安全员）看到的是“项目部”，不是“港湾核定/港湾退回”。
"""
from contextlib import closing
from pathlib import Path

import pytest

from app import db as dbmod
from app.services import quick
from app.services import todo as todo_svc
from conftest import login
from test_roles import _team
from test_todo import _get, _src, world  # noqa: F401 — world 是复用的 fixture

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def read(n):
    return (STATIC / n).read_text(encoding="utf-8")


# ============================================================================ G：扫码页局域网地址

def test_lan_ip_skips_loopback_and_link_local(monkeypatch):
    quick._LAN_CACHE.clear()
    monkeypatch.setattr(quick, "_ip_candidates", lambda: ["127.0.0.1", "169.254.3.4", "192.168.1.23"])
    assert quick.lan_ip() == "192.168.1.23"
    quick._LAN_CACHE.clear()
    monkeypatch.setattr(quick, "_ip_candidates", lambda: ["127.0.0.1", "::1", "not-an-ip"])
    assert quick.lan_ip() is None                                   # 取不到就不给
    quick._LAN_CACHE.clear()


def test_lan_base_uses_scheme_and_port(monkeypatch):
    quick._LAN_CACHE.clear()
    monkeypatch.setattr(quick, "_ip_candidates", lambda: ["10.0.0.8"])
    assert quick.lan_base("http", 8600) == "http://10.0.0.8:8600"
    assert quick.lan_base("https", None) == "https://10.0.0.8"
    quick._LAN_CACHE.clear()
    monkeypatch.setattr(quick, "_ip_candidates", lambda: [])
    assert quick.lan_base("http", 8600) == ""
    quick._LAN_CACHE.clear()


def test_qr_base_endpoint_and_admin_only_setting(env, monkeypatch):
    _team(env)
    quick._LAN_CACHE.clear()
    monkeypatch.setattr(quick, "_ip_candidates", lambda: ["192.168.5.6"])
    sa, adm, tech = login("sa_a"), login("admin"), login("tech1")
    r = sa.get("/api/quick/base")
    assert r.status_code == 200
    body = r.json()
    assert body["base"] == "" and body["lan"].startswith("http://192.168.5.6") and body["admin"] is False
    assert tech.get("/api/quick/base").status_code == 403            # 与扫码管理同一批岗位
    # 只有系统管理员能改部署配置
    assert sa.put("/api/quick/base", json={"base": "http://x.cn"}).status_code == 403
    assert adm.put("/api/quick/base", json={"base": "ftp://x"}).status_code == 400
    assert adm.put("/api/quick/base", json={"base": "http://x.cn/a"}).status_code == 400
    assert adm.put("/api/quick/base", json={"base": "https://zb.example.cn:8443/"}).status_code == 200
    assert adm.get("/api/quick/base").json()["admin"] is True
    assert sa.get("/api/quick/base").json()["base"] == "https://zb.example.cn:8443"
    with closing(dbmod.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM audit WHERE action='qr.base'").fetchone()["n"] == 1
    assert adm.put("/api/quick/base", json={"base": ""}).status_code == 200     # 清空后回到自动建议
    assert sa.get("/api/quick/base").json()["base"] == ""
    quick._LAN_CACHE.clear()


def test_qr_page_has_plain_hint_and_no_deploy_setting():
    js = read("x_quick.js")
    qr = js[js.index("const QuickQr"):]
    assert "localhost 地址" not in qr and "二维码网址前缀" not in qr and "lsSet(QR_BASE_KEY" not in qr
    assert "手机需与电脑在同一网络" in qr
    assert "/api/quick/base" in qr
    # 部署配置挪到系统管理员的系统概况
    roles = read("roles.js")
    sys_home = roles[roles.index("const SysHome"):roles.index("const CAP_NAME")]
    assert "二维码网址前缀" in sys_home and 'PUT("/api/quick/base"' in sys_home


# ============================================================================ G：审计默认隐藏开发态登录

def test_audit_hides_dev_login_by_default(env):
    with closing(dbmod.connect()) as conn:
        for _ in range(5):
            dbmod.audit(conn, env["users"]["admin"], "dev_login")
        dbmod.audit(conn, env["users"]["admin"], "user.update", "user:2")
    adm = login("admin")                                             # 正常登录会记一条 login
    acts = [a["action"] for a in adm.get("/api/admin/audit?limit=200").json()]
    assert "dev_login" not in acts and "login" in acts and "user.update" in acts
    acts = [a["action"] for a in adm.get("/api/admin/audit?limit=200&dev=1").json()]
    assert acts.count("dev_login") == 5
    assert "dev_login" not in [a["action"] for a in adm.get("/api/admin/overview").json()["audit"]]
    # 统计“最近登录/从未登录”仍把开发态登录算作登录过
    assert str(env["users"]["admin"]) in adm.get("/api/admin/overview").json()["last_login"]


def test_audit_page_has_dev_toggle():
    roles = read("roles.js")
    page = roles[roles.index("const AuditPage"):]
    assert "显示开发态登录" in page and "&dev=" in page


# ============================================================================ D1：待办里 AI 起草 / AI 发现

def test_ai_tag_words_and_sources():
    assert set(todo_svc.AI_TAG.values()) == {"AI 起草", "AI 发现", "AI 分析"}
    assert todo_svc.AI_TAG["alert"] == "AI 分析"          # 预警由规则判定，AI 只分析原因
    for s in ("candidate", "log_review", "ticket_risk"):
        assert todo_svc.AI_TAG[s] == "AI 发现", s
    for s in ("report", "mapping", "ticket_pending"):
        assert todo_svc.AI_TAG[s] == "AI 起草", s
    for s in ("gate", "data_gap", "quick", "rectify", "meeting", "week_missing", "log_missing", "week"):
        assert s not in todo_svc.AI_TAG, s
    # 只有 ai=True 的条目带标签（例如项目经理待决策里非安全/质量 Agent 的事项不带）
    assert todo_svc.item("finding", 1, "site", "t", "r", "s", ai=False)["ai_tag"] is None
    assert todo_svc.item("finding", 1, "safety", "t", "r", "s", ai=True)["ai_tag"] == "AI 发现"
    assert todo_svc.item("gate", 1, "progress", "t", "r", "s", ai=True)["ai_tag"] is None


def test_ai_tag_on_manager_items(world):
    pm, eng = _get(login("pm")), _get(login("eng"))
    # D7：项目经理不再做候选确认与预警反馈；赶工决定是“AI 分析”（预警由规则判定、经人确认），升级隐患是“AI 发现”
    assert {i["ai_tag"] for i in _src(pm, "crash")} == {"AI 分析"}
    assert {i["ai_tag"] for i in _src(pm, "escalated")} == {"AI 发现"}
    assert {i["ai_tag"] for i in _src(eng, "alert")} == {"AI 分析"}
    assert {i["ai_tag"] for i in _src(pm, "report")} == {"AI 起草"}
    assert {i["ai_tag"] for i in _src(eng, "mapping")} == {"AI 起草"}
    assert {i["ai_tag"] for i in _src(eng, "log_review")} == {"AI 发现"}
    for s in ("gate", "data_gap", "week_missing", "log_missing", "meeting"):
        assert all(i["ai_tag"] is None for i in _src(eng, s) + _src(pm, s)), s
    assert all(i["ai_tag"] is None for i in _src(_get(login("sa_a")), "quick"))      # 扫码上报是人报的


def test_ai_tag_rendered_as_brand_chip():
    js, css = read("todo.js"), read("todo.css")
    item = js[js.index("const TodoItem"):js.index("const GROUP_SOURCES")]
    assert item.count("it.ai_tag") >= 2                                # 表格行与手机卡片都显示
    assert "chip blue ai-tag" in item
    rule = css[css.index(".ai-tag"):]
    rule = rule[:rule.index("}")]
    assert "border-left" not in rule and "violet" not in rule


# ============================================================================ F6：一线措辞

def test_frontline_wording_project_dept(world):
    d = _get(login("tech1"))
    week = _src(d, "week")
    assert week, "上周周确认草稿应出待办"
    for i in d["items"]:
        text = f"{i['title']} {i['reason']} {i['status']}"
        assert "港湾" not in text and "回填 P6" not in text and "产值" not in text, text
    assert any("提交后更新进度计划" in i["reason"] for i in week)
    src = (Path(todo_svc.__file__)).read_text(encoding="utf-8")
    tech_week = src[src.index("def src_tech_week"):src.index("def src_gate")]
    assert "港湾" not in tech_week.split('"""', 2)[2] and "项目部退回" in tech_week
    roles = read("roles.js")
    safe_home = roles[roles.index("const SafeHome"):roles.index("const SysHome")]
    safe_home = safe_home[:safe_home.index("// ----")]
    assert "港湾" not in safe_home and "项目部安全主管" in safe_home
