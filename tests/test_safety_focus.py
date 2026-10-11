"""安全员首页“今日重点作业巡查”（体检 C2）：
- 计划完成日已过、P6 没有实际完成的作业：日志累计完成率已 ≥100% 或近 14 天没有任何日报进度的，剔除；
- 只列 3–5 项真正在干的作业；不显示作业编码，只显示作业名称与部位；
- p6.window_activities 对其它调用方的默认行为不变。
"""
from contextlib import closing
from datetime import date, timedelta

from conftest import login, sample_xer
from test_roles import _team

from app import db as dbmod
from app.services import p6
from app.services import role_views as rv

T = date(2026, 10, 7)


def _a(code, name, es, ef, wbs="路面工程 / K0-K2", **kw):
    return {"code": code, "name": name, "wbs_path": wbs, "early_start": (T + timedelta(days=es)).isoformat(),
            "early_finish": (T + timedelta(days=ef)).isoformat(), "act_start": kw.pop("act_start", None), "act_finish": None,
            "is_critical": kw.pop("crit", 0), "is_milestone": 0, **kw}


ACTS = [
    _a("RD110", "K0~K2 路基土方开挖", -90, -60, wbs="路基工程 / K0-K2", act_start="2026-07-01"),   # 早已过完成日，日志 100%
    _a("RD120", "K0~K2 路基填筑", -80, -40, wbs="路基工程 / K0-K2", act_start="2026-07-10"),       # 过完成日，近 14 天无日报
    _a("RD130", "K2~K4 路基填筑", -50, -5, wbs="路基工程 / K2-K4", act_start="2026-08-20"),        # 过完成日，但近几天还在报进度
    _a("PV110", "K0~K2 水泥稳定碎石底基层", -10, 18),                                             # 计划窗口覆盖今天、有日报
    _a("PV120", "K0~K2 水泥稳定碎石基层", -3, 25, crit=1),
    _a("BR210", "3#墩墩身施工", -2, 10, wbs="桥梁工程 / 3#墩"),
    _a("BR220", "4#墩墩身施工", -1, 12, wbs="桥梁工程 / 4#墩"),
    _a("BR230", "5#墩墩身施工", 0, 12, wbs="桥梁工程 / 5#墩"),
    _a("BR240", "6#墩墩身施工", 2, 14, wbs="桥梁工程 / 6#墩"),
]
LOGMAX = {"RD110": 100.0, "RD120": 70.0, "RD130": 85.0, "PV110": 20.0}
RECENT = {"RD130", "PV110"}


def test_window_activities_default_unchanged():
    got = [a["code"] for a in p6.window_activities(ACTS, T.isoformat())]
    assert {"RD110", "RD120", "RD130"} <= set(got)          # 其它调用方（日志、动态 Agent）仍拿到“应完未完”的作业


def test_patrol_drops_stale_and_finished():
    got = rv.patrol_focus(ACTS, T.isoformat(), "", LOGMAX, RECENT)
    names = [g["name"] for g in got]
    assert "K0~K2 路基土方开挖" not in names                 # 日志累计 100%
    assert "K0~K2 路基填筑" not in names                     # P6 已开工但久无日报：排在最后，5 项之内轮不到
    assert "K2~K4 路基填筑" in names                         # 仍在报进度
    assert 3 <= len(got) <= 5
    # 有日报进度的排在前面
    assert names[:2] == ["K2~K4 路基填筑", "K0~K2 水泥稳定碎石底基层"] or set(names[:2]) == {"K2~K4 路基填筑", "K0~K2 水泥稳定碎石底基层"}


def test_patrol_hides_activity_code():
    """巡查列表显示作业名与部位，不显示作业编码（接口保留 code 作标识，前端模板只渲染 name/part）。"""
    from pathlib import Path
    got = rv.patrol_focus(ACTS, T.isoformat(), "", LOGMAX, RECENT)
    codes = {a["code"] for a in ACTS}
    for g in got:
        shown = f"{g['name']} {g['part']}"
        assert not any(c in shown for c in codes), shown
        assert g["part"]
    tpl = Path(__file__).resolve().parents[1].joinpath("app/static/roles.js").read_text("utf-8")
    seg = tpl[tpl.index("今日重点作业巡查"):tpl.index("本单位人员")]
    assert "{{ a.code }}" not in seg and "{{ a.part }}" in seg
    pv = next(g for g in got if g["name"] == "K0~K2 水泥稳定碎石底基层")
    assert pv["part"] == "K0-K2" and pv["active"] is True


def test_patrol_scope_filter():
    got = rv.patrol_focus(ACTS, T.isoformat(), "桥梁工程", {}, set())
    assert got and all("墩" in g["name"] for g in got)


def test_safety_home_focus_end_to_end(env):
    _team(env)
    assert login("eng").post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    r = login("sa_a").get("/api/safety/home")
    assert r.status_code == 200, r.text
    focus = r.json()["focus"]
    assert focus and len(focus) <= 5
    with closing(dbmod.connect()) as conn:
        codes = {a["code"] for a in p6.current_activities(conn, env["project_id"])}
    for f in focus:
        assert not any(c in f"{f['name']} {f['part']}" for c in codes)        # 显示的名称与部位不含编码
        assert f["part"] == "3#墩"                                   # 甲单位技术员负责 3#墩


def test_patrol_keeps_started_but_unreported_activity_last():
    """P6 已开工、计划完成日已过、近 14 天无日报（可能正在延误、只是没做周确认）：不剔除，排在最后。"""
    acts = [a for a in ACTS if a["code"] in ("RD120", "RD130")]
    names = [g["name"] for g in rv.patrol_focus(acts, T.isoformat(), "", LOGMAX, RECENT)]
    assert names == ["K2~K4 路基填筑", "K0~K2 路基填筑"]
