"""B2（验收 2026-10-11）：进度红色预警统一为“已确认未关闭”。简报依据不再把待确认计数写成“未关闭”，并写出已确认未关闭的真实数；
预警页红色数不含已关闭，首页卡片带筛选进入。"""
import re
from contextlib import closing
from pathlib import Path

from test_facts import _add_alert, _setup_site

from app import db as dbmod
from app.services import brief
from app.services import facts as F

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_basis_line_counts_confirmed_alerts(env):
    _setup_site(env)
    pid = env["project_id"]
    _add_alert(pid, code="A1020", level="red", status="confirmed")
    _add_alert(pid, code="A1010", level="red", status="closed", rule="idle")
    with closing(dbmod.connect()) as conn:
        pf = F.project_facts(conn, pid)
    assert pf["alerts"]["red"] == 0 and pf["confirmed"]["red"] == 1   # 已关闭的不计
    text = F.render_project(pf)
    assert "待确认进度预警：红 0 橙 0 黄 0；已确认未关闭：红 1 橙 0 黄 0" in text
    assert "未关闭进度预警：红 0" not in text


def test_legacy_basis_label_relabelled():
    b = brief.basis({"facts": "P6 数据日期 2026-09-28；未关闭进度预警：红 0 橙 0 黄 0", "source": "ai"})
    assert b["lines"] == ["P6 数据日期 2026-09-28；待确认进度预警：红 0 橙 0 黄 0"]


def test_front_red_alert_scope_and_filtered_links():
    views = (STATIC / "views.js").read_text(encoding="utf8")
    assert 'redN() { return (this.all || []).filter((a) => a.level === "red" && a.status === "confirmed").length; }' in views
    assert "fromRoute()" in views and '"status" in p' in views
    pm = (STATIC / "x_pmhome.js").read_text(encoding="utf8")
    assert 'label: "进度风险", icon: "alert", value: a.red, tone: a.red ? "red" : "", route: "progress?tab=alerts&status=confirmed&level=red"' in pm
    ck = (STATIC / "cockpit.js").read_text(encoding="utf8")
    assert re.search(r'label: "进度红色预警".*params: \{ tab: "alerts", status: "confirmed", level: "red" \}', ck)
