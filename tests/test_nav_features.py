"""T06：导航收敛为业务线 + 增强能力启用/隐藏开关。

- 开关存 projects.config_json 的 features 段，默认全部启用；只有系统管理员能改；全部启用时不留痕迹；
- 关闭后：对应待办来源不出条目（放行、指挥/例会、二维码、材料、资料），重新启用恢复；
- 导航配置（app/static/nav.js）：每个岗位一级菜单 ≤6 条且各带副标题，各岗位页面与体检报告 E 组收敛后的目标逐项一致，旧路由保留，导航里没有 emoji；
- 同一件事一个入口：问工笔、指挥与例会、个人设置不进任何岗位导航（路由仍可直达），项目总览只给领导；
- 项目经理的导航不新增日志审核/填报入口。
"""
import json
import re
import shutil
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from conftest import login
from test_todo import _get, _sources, world  # noqa: F401  （world 是 test_todo 里的造数夹具）

from app import db as dbmod
from app.services import features as feat_svc

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def _set(admin, **flags):
    r = admin.put("/api/project", json={"name": "示例大桥（合成测试）", "thresholds": {}, "features": flags})
    assert r.status_code == 200, r.text
    return r.json()


def _cfg(env):
    with closing(dbmod.connect()) as conn:
        return json.loads(conn.execute("SELECT config_json FROM projects WHERE id=?", (env["project_id"],)).fetchone()["config_json"])


# ----------------------------------------------------------------------------- 开关读写

def test_features_default_all_enabled(env):
    for u in ("admin", "pm", "tech1"):
        c = login(u)
        me = c.get("/api/me").json()
        assert me["project"]["features"] == {k: True for k in feat_svc.KEYS}
        rows = c.get("/api/project/features").json()
        assert [r["key"] for r in rows] == feat_svc.KEYS and all(r["enabled"] and r["name"] and r["desc"] for r in rows)
    assert len(feat_svc.KEYS) == 8      # 现场圈Agent、指挥、催办、档案、放行、材料、资料、二维码


def test_features_toggle_roundtrip_and_clean_config(env):
    admin = login("admin")
    out = _set(admin, gate=False, qr=False)
    assert out["features"]["gate"] is False and out["features"]["qr"] is False and out["features"]["cmd"] is True
    assert _cfg(env)["features"] == {"gate": False, "qr": False}          # 只记被隐藏的项，其余默认启用
    assert login("pm").get("/api/me").json()["project"]["features"]["gate"] is False
    _set(admin, gate=True, qr=True, bogus=False)                           # 未知键忽略；全部启用后不留 features 段
    assert "features" not in _cfg(env)
    assert login("pm").get("/api/me").json()["project"]["features"] == {k: True for k in feat_svc.KEYS}


def test_features_only_admin_can_change(env):
    r = login("pm").put("/api/project", json={"name": "x", "thresholds": {}, "features": {"gate": False}})
    assert r.status_code in (401, 403)
    assert "features" not in _cfg(env)


def test_features_patch_keeps_other_flags(env):
    admin = login("admin")
    _set(admin, nudge=False)
    _set(admin, cmd=False)                                                  # 只提交一项时不影响已隐藏的其它项
    assert _cfg(env)["features"] == {"nudge": False, "cmd": False}


# ----------------------------------------------------------------------------- 待办来源随开关隐藏

def test_todo_sources_follow_switches(world):
    tech, eng, sa, ss = login("tech1"), login("eng"), login("sa_a"), login("ss")
    admin = login("admin")
    assert {"gate", "meeting"} <= _sources(_get(tech)) and "gate" in _sources(_get(eng)) and "quick" in _sources(_get(sa))
    _set(admin, gate=False)
    assert not {"gate", "gate_more"} & (_sources(_get(tech)) | _sources(_get(eng)))
    assert "meeting" in _sources(_get(tech))                                   # 其它来源不受影响
    _set(admin, cmd=False)
    assert not {"meeting", "meeting_more"} & (_sources(_get(tech)) | _sources(_get(eng)) | _sources(_get(ss)))
    _set(admin, qr=False)
    assert not {"quick", "quick_more"} & (_sources(_get(sa)) | _sources(_get(ss)))
    assert "candidate" in _sources(_get(sa))                                    # 候选隐患是安全Agent 的产出，不属于二维码开关
    _set(admin, gate=True, cmd=True, qr=True)                                   # 重新启用即恢复
    assert {"gate", "meeting"} <= _sources(_get(tech)) and "quick" in _sources(_get(sa))


def test_todo_ticket_and_conceal_sources_follow_switches(world, monkeypatch):
    from app.services import conceal as conceal_svc
    from app.services import tickets as tickets_svc
    monkeypatch.setattr(tickets_svc, "risks", lambda conn, user: {"items": [{"kind": "x", "level": "high", "title": "小票疑点", "detail": "d", "ref_id": 1}], "pending_tickets": 2})
    monkeypatch.setattr(conceal_svc, "gaps", lambda conn, user: {"gaps": [{"part": "3#墩", "template_name": "钢筋"}]})
    monkeypatch.setattr(conceal_svc, "list_records", lambda conn, user: {"counts": {"draft": 1}, "records": []})
    eng, tech, admin = login("eng"), login("tech1"), login("admin")
    assert {"ticket_risk", "conceal_gap"} <= _sources(_get(eng)) and {"ticket_pending", "conceal_todo"} <= _sources(_get(tech))
    _set(admin, tickets=False)
    assert "ticket_risk" not in _sources(_get(eng)) and "conceal_gap" in _sources(_get(eng))
    assert "ticket_pending" not in _sources(_get(tech)) and "conceal_todo" in _sources(_get(tech))
    _set(admin, conceal=False)
    assert not {"conceal_gap", "conceal_todo"} & (_sources(_get(eng)) | _sources(_get(tech)))
    _set(admin, tickets=True, conceal=True)
    assert {"ticket_risk", "conceal_gap"} <= _sources(_get(eng))


def test_circle_agent_hidden_stops_review_and_picks(env, monkeypatch):
    from app.services import circle_picks as picks
    from app.services import feed_agents as agents

    def boom(*a, **k):
        raise AssertionError("隐藏后不应再生成 Agent 点评/精选")
    monkeypatch.setattr(agents, "review", boom)
    monkeypatch.setattr(picks, "maybe_auto_pick", boom)
    _set(login("admin"), circle_agent=False)
    r = login("pm").post("/api/circle/review", json={"keys": ["note:1"]})
    assert r.status_code == 200 and r.json() == {"comments": {}, "left": [], "picked": False}


# ----------------------------------------------------------------------------- 导航配置（node 直接跑 nav.js）

NODE = shutil.which("node")
HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const ctx = { EXT: { navPages: [], aliases: {} }, store: { me: null }, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8') + '\nthis.__o = { NAV_LINES, NAV_PAGES, buildNav, navMatch, featOn };', ctx);
vm.runInContext(fs.readFileSync(process.argv[3], 'utf8') + '\nthis.__i = ICONS;', ctx);
const o = ctx.__o, roles = ['tech', 'sub_safety', 'manager', 'eng_sup', 'safety_sup', 'leader', 'admin'];
const out = { icons: Object.keys(ctx.__i), pages: o.NAV_PAGES.map(p => ({ k: p.k, route: p.route, icon: p.icon, label: p.label, line: p.line, feat: p.feat || '' })),
  lines: o.NAV_LINES.map(l => ({ k: l.k, name: l.name, icon: l.icon, bottom: !!l.bottom })), roles: {}, off: {}, match: {} };
for (const r of roles) {
  const n = o.buildNav(r, {});
  out.roles[r] = { top: n.top.map(l => ({ k: l.k, name: l.name, sub: l.sub, pages: l.pages.map(p => p.route + (p.params ? '?tab=' + p.params.tab : '')) })),
                   bottom: n.bottom.map(l => ({ k: l.k, pages: l.pages.map(p => p.route + (p.params ? '?tab=' + p.params.tab : '')) })),
                   ks: n.top.concat(n.bottom).flatMap(l => l.pages.map(p => p.k)) };
  const f = {}; ['circle_agent', 'cmd', 'nudge', 'archive', 'gate', 'tickets', 'conceal', 'qr'].forEach(k => f[k] = false);
  const m = o.buildNav(r, f); out.off[r] = m.top.concat(m.bottom).flatMap(l => l.pages.map(p => p.route));
}
const probe = (r, name, params, c) => { const p = o.navMatch(r, name, params || {}, c || {}); return p ? p.k : null; };
out.match = { techLog: probe('tech', 'log'), engLog: probe('eng_sup', 'log'), pmLog: probe('manager', 'log'), techFinding: probe('tech', 'finding'), subFinding: probe('sub_safety', 'finding'),
  pmFindingQ: probe('manager', 'finding', {}, { findingAgent: 'quality' }), pmFindingS: probe('manager', 'finding'), alert: probe('manager', 'alert'), report: probe('manager', 'report'),
  claims: probe('manager', 'claims'), ask: probe('manager', 'agents', { tab: 'ask' }), agents: probe('manager', 'agents', {}), snap: probe('tech', 'snap'), capture: probe('tech', 'capture'),
  techLogs: probe('tech', 'logs'), techWeek: probe('tech', 'week'), oldWeekly: probe('manager', 'reports', { kind: 'week' }), oldMonthly: probe('eng_sup', 'reports', { kind: 'month' }),
  docs: probe('eng_sup', 'reports', { kind: 'notice_of_claim' }), rptSeg: probe('leader', 'rpt', { seg: 'month' }), progressNoTab: probe('manager', 'progress', {}),
  hiddenKept: o.buildNav('eng_sup', { gate: false }, 'gate').top.flatMap(l => l.pages.map(p => p.k)).includes('gate') };
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def nav():
    if not NODE:
        pytest.skip("没有 node，跳过 nav.js 静态校验")
    h = Path(__file__).with_name("_nav_harness.js")
    h.write_text(HARNESS, encoding="utf-8")
    try:
        r = subprocess.run([NODE, str(h), str(STATIC / "nav.js"), str(STATIC / "icons.js")], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        h.unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# 各岗位导航（按业务线顺序的页面 k）。导航编排 10-08（用户同意，docs/导航编排方案_2026-10-08.md）：
# 工作台 → 现场 → 进度 → 工笔文书 → 资料台账 → 智能体 六组，同一组各岗位同名；“更多功能”取消，催办记录归智能体；
# 技术员的整改与质控和首页“我的待办”重复、开工条件不给一线；问工笔/个人设置不进导航；项目总览只作领导首页
# 10-09 RJ：现场圈排现场第一个；日志移出现场，与周报、月报合成“工笔文书 · 报表”（rpt）；进度按 计划 → 体检 → 预警 → 开工条件 → 预测 → 产值
# 10-10 RJ 采用 09_V8：一级顺序 智能管理台 → 进度与产值 → 智眸现场 → 工笔文书 → 资料台账 → 智能体
EXPECT = {
    "tech": ["home", "rpt", "circle", "letters", "conceal", "tickets", "notices"],   # 技术员的报表在“随手记”组；文书组对他是“收发文”，只有项目部来函（RJ 10-09）
    "sub_safety": ["shome", "circle", "ledger", "qrcodes", "letters", "notices"],
    "eng_sup": ["eng", "plan", "alerts", "gate", "progress", "output", "circle", "safety", "quality", "rpt", "letter", "meetings", "ana",
                "conceal", "tickets", "sites", "agents", "metrics", "chase", "notices"],
    # RJ 10-10：工笔文书按用途分四个同级分区 报表 | 函件（含致分包文书的签收与回复）| 例会 | 分析；原“文书”“致分包文书”页签取消
    "safety_sup": ["sdash", "circle", "safety", "qrcodes", "rpt", "letter", "meetings", "ana", "notices"],
    # 项目经理只看进度、质量、安全：产值、应用成效、P6 计划、开工条件、验收、材料、档案、催办、致分包文书列表都不给（见 test_pm_scope.py）
    "manager": ["work", "alerts", "progress", "output", "circle", "safety", "quality", "rpt", "letter", "meetings", "ana", "agents", "notices"],
    "leader": ["cockpit", "alerts", "progress", "output", "rpt", "letter", "meetings", "ana", "notices"],
    "admin": ["sysh", "aimgr", "audit", "agents", "metrics", "users", "settings", "notices"],
}


def test_nav_at_most_six_lines_each_with_subtitle(nav):
    for role, n in nav["roles"].items():
        assert 1 <= len(n["top"]) <= 6, role
        for line in n["top"]:
            assert line["sub"].strip(), (role, line["k"])
            assert line["pages"], (role, line["k"])
    assert len([x for x in nav["lines"] if not x["bottom"]]) == 6
    assert {x["k"] for x in nav["lines"] if x["bottom"]} == {"sys", "msg"}          # “更多功能”已取消


def test_nav_each_role_matches_target(nav):
    for role, want in EXPECT.items():
        assert nav["roles"][role]["ks"] == want, role
    # 一线手机底栏（业务线数）：技术员 4（随手记、智眸现场、收发文、资料台账）、分包安全员 3（今日安全、智眸现场、收发文），不超过 5
    assert [x["k"] for x in nav["roles"]["tech"]["top"]] == ["desk", "site", "rep", "arch"]
    # 分包岗位没有工笔文书：这一组叫“收发文”，只有项目部来函（RJ 10-09）
    for r in ("tech", "sub_safety"):
        rep = next(x for x in nav["roles"][r]["top"] if x["k"] == "rep")
        assert rep["name"] == "收发文" and rep["pages"] == ["letters"]   # 页签名按岗位改为“项目部来函”（nav.js 的 as）
    assert [x["k"] for x in nav["roles"]["sub_safety"]["top"]] == ["desk", "site", "rep"]
    # 同一组在所有岗位同名（首页那组、管理员的 AI 治理、分包岗位的“收发文”除外）
    names = {"site": "智眸现场", "prog": "进度与产值", "rep": "工笔文书", "arch": "资料台账"}
    for role, n in nav["roles"].items():
        for line in n["top"]:
            if line["k"] in names and not (line["k"] == "rep" and role in ("tech", "sub_safety")):
                assert line["name"] == names[line["k"]], (role, line["k"], line["name"])
            if line["k"] == "ai":
                assert line["name"] == ("AI 治理" if role == "admin" else "智能体"), role
    assert next(x for x in nav["roles"]["leader"]["top"] if x["k"] == "desk")["name"] == "项目总览"


def test_nav_single_entry_no_pass_through_pages(nav):
    for role, n in nav["roles"].items():
        pages = [p for x in n["top"] + n["bottom"] for p in x["pages"]]
        if role != "admin":
            assert not {"ask", "settings_u"} & set(n["ks"]), role       # 问工笔/个人设置不进导航（例会 10-08 起进“工笔文书”）
            assert not {"agents?tab=ask", "settings"} & set(pages), role   # 按路由再兜一次
        assert ("cockpit" in n["ks"]) == (role == "leader"), role                   # 只有领导有项目总览
    assert not {"ask", "settings_u", "tasks", "qrcodes_more"} & {p["k"] for p in nav["pages"]}


def test_nav_no_emoji_and_icons_exist(nav):
    src = (STATIC / "nav.js").read_text(encoding="utf-8")
    assert not re.search("[\U0001F000-\U0001FFFF☀-➿️]", src), "导航配置里不得出现 emoji"
    icons = set(nav["icons"])
    for p in nav["pages"]:
        assert p["icon"] in icons, p
    for x in nav["lines"]:
        assert x["icon"] in icons, x
    for k in ("x_conceal.js", "x_gate.js", "x_tickets.js", "x_quick.js"):
        assert "EXT.nav(" not in (STATIC / k).read_text(encoding="utf-8"), f"{k} 不应再各自注册一级菜单"


def test_nav_routes_exist_and_old_routes_kept(nav):
    main = (STATIC / "main.js").read_text(encoding="utf-8")
    known = set(re.findall(r"r\.name === '([a-z]+)'", main))
    for f in STATIC.glob("x_*.js"):
        known |= set(re.findall(r'EXT\.route\("([a-z]+)"', f.read_text(encoding="utf-8")))
    for p in nav["pages"]:
        assert p["route"] in known, p
    # 旧路由全部保留（书签、通知里的链接）
    old = {"home", "snap", "capture", "week", "tasks", "manual", "log", "logs", "circle", "shome", "ledger", "finding", "eng", "sdash", "sysh", "users", "audit", "work", "cockpit", "quality",
           "safety", "progress", "alert", "output", "reports", "report", "agents", "settings", "notices", "aimgr", "meetings", "chase", "conceal", "gate", "qrcodes", "sites", "claims", "tickets"}
    assert old <= known, sorted(old - known)


def test_nav_feature_switches_hide_entries_only(nav):
    gated = {p["route"] for p in nav["pages"] if p["feat"]}
    assert gated == {"gate", "chase", "qrcodes", "conceal", "tickets", "sites"}     # 例会分区有例会底稿、三周滚动计划，常显；只有其中“行动项核对”随“指挥”开关（x_docs.js）
    for role, routes in nav["off"].items():
        assert not gated & set(routes), (role, gated & set(routes))      # 全部隐藏后，这些入口都不在导航里
        assert routes, role                                              # 但每个岗位仍有导航
    assert nav["match"]["hiddenKept"]                                    # 当前页即使所属能力被隐藏也留在页签里，不让界面跳走


def test_nav_detail_pages_highlight_their_line(nav):
    m = nav["match"]
    assert (m["techLog"], m["engLog"], m["pmLog"]) == ("rpt", "rpt", "quality")          # 日志详情归“报表”；项目经理不看日志，归质量
    assert (m["techFinding"], m["subFinding"], m["pmFindingQ"], m["pmFindingS"]) == ("home", "ledger", "quality", "safety")   # 技术员的隐患详情归首页待办
    assert (m["alert"], m["report"], m["agents"], m["snap"], m["capture"]) == ("alerts", "rpt", "agents", "home", "rpt")   # 预警详情高亮“预警”页签；进度的 预测/预警/P6 计划 是同一路由下的页签页
    # 报表的各分页沿用原地址：#/logs、#/week 与旧的 #/reports?kind=week|month 都高亮“报表”；其余文书按类型归分区（通知函 → 函件）；进度页不带 tab 仍是预测与赶工
    assert (m["techLogs"], m["techWeek"], m["oldWeekly"], m["oldMonthly"], m["rptSeg"]) == ("rpt",) * 5
    assert m["docs"] == "letter" and m["progressNoTab"] == "progress"
    assert m["claims"] is None       # 项目经理导航里没有档案：直达时页面照常打开，只是不高亮任何页签
    assert m["ask"] == "agents"      # 问工笔不单独进导航；从智能体卡片点进的提问页归属“智能体”，高亮它


def test_nav_order_after_rj_1009(nav):
    """10-09 RJ：首页组叫“智能管理台”；现场圈是现场第一个；进度按 计划 → 预警 → 开工条件 → 预测 → 产值（计划体检由 x_plan.js 挂在计划之后）。"""
    for role in ("tech", "sub_safety", "manager", "eng_sup", "safety_sup"):
        site = next(x for x in nav["roles"][role]["top"] if x["k"] == "site")
        assert site["pages"][0] == "circle", role
    prog = next(x for x in nav["roles"]["eng_sup"]["top"] if x["k"] == "prog")
    assert prog["pages"] == ["progress?tab=plan", "progress?tab=alerts", "gate", "progress?tab=forecast", "output"]
    assert next(x for x in nav["lines"] if x["k"] == "desk")["name"] == "智能管理台"
    tech = {x["k"]: x["pages"] for x in nav["roles"]["tech"]["top"]}
    assert tech["desk"] == ["home", "rpt"] and "rpt" not in tech.get("rep", [])     # 技术员不用工笔文书：报表在“随手记”组
    plan = (STATIC / "x_plan.js").read_text(encoding="utf-8")
    assert 'k: "planck"' in plan and 'after: "plan"' in plan
    assert 'label: "分包履约画像"' in (STATIC / "x_subprofile.js").read_text(encoding="utf-8")


def test_nav_pm_has_no_log_review_or_fill_entry(nav):
    pm = nav["roles"]["manager"]
    routes = {p.split("?")[0] for x in pm["top"] + pm["bottom"] for p in x["pages"]}
    assert not {"logs", "week", "capture", "manual", "tasks"} & routes          # 项目经理导航里没有日志审核/填报入口
    assert "output" in routes                                                     # 产值只读（RJ 10-10）
    assert all("日志" not in x["sub"] and "填报" not in x["sub"] for x in pm["top"])
    eng = nav["roles"]["eng_sup"]
    assert "rpt" in {p for x in eng["top"] for p in x["pages"]}                 # 日志（日报）在工程主管的“工笔文书 · 报表”里
    assert not {"logs", "week"} & {p for x in eng["top"] if x["k"] == "site" for p in x["pages"]}   # 现场线下不再有日志（RJ 10-09）
