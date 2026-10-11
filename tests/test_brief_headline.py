"""管理岗工作台“AI 今日结论”、P6 数据日期提示、命名统一、按期概率统一口径（体检 D1/D4、C2②）。

- 接口：/api/todo 给项目经理/工程主管/安全主管返回今日简报 headline；没有简报不给；样本为零的“可控”结论不给；
  简报依据（/api/cockpit 的 brief.basis）只含进度、质量、安全；
- 按期概率：schedule.prob_text 统一格式化，0 次按期写“<1%（N 次模拟无一次按期）”并接 P50 推迟天数，全部按期写“>99%”；
  简报事实底稿用同一文字，出口校验仍通过，写“按期概率 0%”被拦；
- 前端：todo.js / views.js / cockpit.js 的文案静态核对。
"""
import json
import re
from contextlib import closing
from datetime import date
from pathlib import Path

from conftest import login
from test_facts import _add_alert, _bad_call, _gen, _lag_fact
from test_circle_agents import _setup_site

from app import db as dbmod
from app.services import brief
from app.services import facts as F
from app.services import schedule

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def read(n):
    return (STATIC / n).read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- 4 按期概率格式化

def test_prob_text_extremes_and_middle():
    assert schedule.prob_text(None, 400) == "未知"
    assert schedule.prob_text(0, 400) == "<1%（400 次模拟无一次按期）"
    assert schedule.prob_text(0.0, 400, 12) == "<1%（400 次模拟无一次按期），预计推迟 12 天（P50）"
    assert schedule.prob_text(0.003, 400) == "<1%"                     # 有按期样本但四舍五入为 0：不写“无一次”
    assert schedule.prob_text(1, 400) == ">99%（400 次模拟全部按期）"
    assert schedule.prob_text(0.997, 400) == ">99%"
    assert schedule.prob_text(0.35, 400) == "35%"
    assert schedule.prob_text(0.35, 400, 5) == "35%"                   # 推迟天数只跟在 <1% 后面
    for p in (0, 0.002, 1, 0.999):
        assert not re.search(r"(?<![<>\d])(0|100)%", schedule.prob_text(p, 400))


def test_milestone_prob_fields_use_p50_delay():
    m = {"code": "M1", "on_time_prob": 0.0, "p6_date": "2026-10-01", "p50": "2026-10-13"}
    assert schedule.p50_delay(m) == 12
    out = schedule.with_prob_text({"iterations": 400, "milestones": [m, {"code": "M2", "on_time_prob": None, "p6_date": None, "p50": "2026-10-13"}]})
    a, b = out["milestones"]
    assert a["prob_label"] == "<1%" and a["prob_text"] == "<1%（400 次模拟无一次按期），预计推迟 12 天（P50）"
    assert a["prob_detail"] == "400 次模拟无一次按期，预计推迟 12 天（P50）"
    assert b["prob_label"] == "未知" and b["prob_detail"] == ""


def test_facts_render_uses_prob_text_and_check_blocks_zero_percent():
    ms = [{"code": "M1", "name": "主桥合龙", "p6_date": "2026-10-01", "forecast": "2026-10-12", "p50": "2026-10-13", "p80": "2026-10-16", "on_time_prob": 0.0}]
    p = {"kind": "project", "p6_data_date": "2026-10-01", "alerts": {"red": 1, "orange": 0, "yellow": 0}, "lag_alerts": [], "milestones": ms,
         "iterations": 400, "project_finish": None, "forecast_at": None}
    text = F.render_project(p)
    assert "按期概率 <1%（400 次模拟无一次按期），预计推迟 12 天（P50）" in text
    assert not re.search(r"按期概率\s*0%", text)
    # 照抄底稿文字通过；写成“0%”/“100%”被拦（即使 0 这个数在底稿别处出现）
    assert F.check_text("主桥合龙按期概率 <1%，预计推迟 12 天（P50）", [p], extra=text) == []
    assert F.check_text("主桥合龙按期概率 0%", [p], extra=text + "\n累计完成 0%")
    assert F.check_text("主桥合龙按期概率为100%", [p], extra=text)
    assert F.check_text("按期概率 35% 左右", [{**p, "milestones": [{**ms[0], "on_time_prob": 0.35}]}]) == []
    # 作业底稿里的下游里程碑同样用统一文字
    f = _lag_fact(milestones=[{"code": "M1", "name": "主桥合龙", "p6_date": "2026-10-01", "forecast": "2026-10-12", "p50": "2026-10-13", "on_time_prob": 0.0}])
    assert "<1%（400 次模拟无一次按期），预计推迟 12 天（P50）" in F.render(f)


def test_brief_facts_and_cockpit_carry_prob_text(env):
    _setup_site(env)
    with closing(dbmod.connect()) as conn:
        fc = schedule.forecast(conn, env["project_id"])
        assert fc and all("prob_text" in m and "prob_label" in m for m in fc["milestones"])
        k = brief.kpis(conn, env["project_id"], date.today())
        text = brief.facts_text(conn, env["project_id"], k)
    for m in fc["milestones"]:
        if m["on_time_prob"] is not None:
            assert m["prob_text"] in text
    assert not re.search(r"按期概率\s*(0|100)%", text)
    cp = login("pm").get("/api/cockpit").json()
    assert all("prob_label" in m and "prob_detail" in m for m in cp["milestones"])
    assert login("eng").get("/api/schedule/forecast").json()["milestones"][0]["prob_text"]


def test_brief_with_extreme_prob_still_passes_exit_check(env, monkeypatch):
    """底稿里给的是格式化后的文字：AI 照抄“<1%…”能通过出口校验，写“0%”退回模板。"""
    _setup_site(env)
    _add_alert(env["project_id"])
    with closing(dbmod.connect()) as conn:
        conn.execute("""INSERT INTO forecasts (project_id, import_id, as_of, result_json, created_at)
                        SELECT project_id, id, ?, ?, ? FROM p6_imports WHERE project_id=? AND is_current=1""",
                     (date.today().isoformat(), json.dumps({"iterations": 400, "milestones": [
                         {"code": "M9", "name": "主桥合龙", "p6_date": "2026-10-01", "forecast": "2026-10-12", "p50": "2026-10-13", "p80": "2026-10-16",
                          "slip_days": 11, "on_time_prob": 0.0}], "project_finish": {"forecast": "2026-12-01", "p50": "2026-12-03", "p80": "2026-12-09"}}),
                      dbmod.now_iso(), env["project_id"]))
    good = {"headline": "A1020 滞后 8 天，主桥合龙按期概率 <1%，预计推迟 12 天（P50）",
            "items": [{"level": "high", "area": "进度", "title": "A1020 滞后", "detail": "滞后 8 天，超出总浮时 8 天", "action": "赶工"}]}
    _bad_call(monkeypatch, "brief", "brief.daily", [good])
    out = _gen(env)
    assert out["source"] == "ai" and out["headline"] == good["headline"]
    bad = {**good, "headline": "A1020 滞后 8 天，主桥合龙按期概率 0%"}
    _bad_call(monkeypatch, "brief", "brief.daily", [bad])
    out = _gen(env)
    assert out["source"] == "template" and "0%" not in out["headline"]


# ----------------------------------------------------------------------------- 1 AI 今日结论（接口层）

def _put_brief(pid, headline, facts="", items=None, source="ai"):
    with closing(dbmod.connect()) as conn:
        conn.execute("DELETE FROM briefs")
        conn.execute("INSERT INTO briefs (project_id, day, content_json, created_at) VALUES (?,?,?,?)",
                     (pid, date.today().isoformat(), json.dumps({"headline": headline, "items": items or [], "facts": facts, "source": source}), dbmod.now_iso()))


FACTS = ("日期 2026-10-07\n进度预警：红1 橙0 黄0\n[W1] A1020 进度滞后 8 天；分析：滞后 8 天\n【事实底稿】\nP6 数据日期 2026-10-01；未关闭进度预警：红 1 橙 0 黄 0\n"
         "安全隐患：未闭环 0（高风险 0，超期 0），累计闭环 0；近 14 天无隐患记录（样本为零，不得据此写“可控/正常/良好”）\n"
         "质量问题：未闭环 1（超期 0），待执行质控提醒 2")


def test_desk_returns_today_headline_for_manager_roles(env):
    _setup_site(env)
    _put_brief(env["project_id"], "A1020 滞后 8 天需赶工；安全：近 14 天无隐患记录", FACTS)
    for u in ("pm", "eng", "ss"):
        att = login(u).get("/api/todo").json()["attention"]
        assert att["headline"] == "3#墩承台钢筋绑扎 滞后 8 天需赶工；安全：近 14 天无隐患记录", u  # 作业代码在接口出口换成中文简称（app/services/act_names.py）
    # 依据：事实底稿各行，只含进度、质量、安全；提示模型的话不外露
    b = login("pm").get("/api/cockpit").json()["brief"]["basis"]
    assert b["source"] and b["lines"]
    joined = "\n".join(b["lines"])
    assert "3#墩承台钢筋绑扎" in joined and "质量问题" in joined and "近 14 天无隐患记录" in joined
    assert "不得据此" not in joined and "【事实底稿】" not in joined and not joined.startswith("日期")
    assert brief.scope_ok(joined)


def test_no_brief_no_headline(env):
    _setup_site(env)
    with closing(dbmod.connect()) as conn:
        conn.execute("DELETE FROM briefs")
    att = login("pm").get("/api/todo").json()["attention"]
    assert att["missing"] and not att["headline"]


def test_zero_sample_conclusion_is_withheld(env):
    """安全近 14 天零样本，旧简报 headline 却写“安全总体可控”：工作台不给这句结论（沿用 zero_sample_problems）。"""
    _setup_site(env)
    _put_brief(env["project_id"], "进度预警红1，安全总体可控", FACTS)
    assert not login("pm").get("/api/todo").json()["attention"]["headline"]
    # 质量有样本：质量方面的结论照常给
    _put_brief(env["project_id"], "质量问题已在整改，质量总体可控", FACTS)
    assert login("pm").get("/api/todo").json()["attention"]["headline"] == "质量问题已在整改，质量总体可控"


def test_out_of_scope_basis_lines_dropped():
    b = brief.basis({"facts": "日期 x\n进度预警：红1\n今日日志填报 2/4\n本周产值 30 万", "source": "template", "note": "AI 简报与事实底稿不一致"})
    assert b["lines"] == ["待确认进度预警：红1"] and "模板" in b["source"] and b["note"]


# ----------------------------------------------------------------------------- 1/2/3 前端文案

def test_todo_home_conclusion_line_and_p6_hint():
    js = read("todo.js")
    assert "AI 今日结论" in js and "依据" in js
    assert "暂无" not in re.search(r"const TodoConclusion = \{.*?\n\};", js, re.S).group(0)  # 没有简报不显示“暂无”
    assert "请更新后重新导入" not in js and "需更新" in js and "stale_days > 14" in js
    head = re.search(r"<page-head :title=\"title\" page=\"desk\">.*?</page-head>", js, re.S).group(0)
    assert "tc-over" not in head and "距今" in head
    # D 版（2026-10-08 用户定稿）：结论不再压在头卡里，放进右栏“AI 今日简报”，在 AI 今日要点上方
    assert "<todo-conclusion" in js.split('<aside class="home-rail')[1].split("<todo-attention")[0]


def test_unified_names():
    js = read("todo.js")
    assert "<h2>需要关注</h2>" not in js and "AI 今日要点" in js
    assert "紧急待办" in js and "超期/高风险" not in js          # 待办条数叫“紧急待办”，与台账里的“超期隐患/高风险隐患”区分开（D-1）
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("app/services/todo.py").read_text("utf-8")
    assert '"label": "确认属实"' in src and "采纳预警" not in src and '"紧急待办"' in src and '"需关注"' not in src   # 名称从源头统一
    card = re.search(r"const AlertCard = \{.*?\n\};", read("views.js"), re.S).group(0)
    assert ">确认属实</button>" in card and ">误报</button>" in card and ">确认</button>" not in card


def test_cockpit_uses_backend_prob_text():
    cp = read("cockpit.js")
    assert "prob_label" in cp and "prob_detail" in cp


def test_milestone_p50_delay_accepted_as_lag_days():
    """里程碑 P50 推迟天数写成“滞后/落后 N 天”是自然说法，守卫应认可（仍拦与底稿不符的天数）。"""
    ms = [{"code": "M1", "name": "主桥合龙", "p6_date": "2026-10-01", "forecast": "2026-10-12", "p50": "2026-10-13", "p80": "2026-10-16", "on_time_prob": 0.0}]
    p = {"kind": "project", "p6_data_date": "2026-10-01", "alerts": {"red": 1, "orange": 0, "yellow": 0}, "lag_alerts": [], "milestones": ms,
         "iterations": 400, "project_finish": None, "forecast_at": None}
    text = F.render_project(p)
    assert F.check_text("主桥合龙预计滞后 12 天", [p], extra=text) == []
    assert F.check_text("主桥合龙预计滞后 25 天", [p], extra=text)


def test_forecast_flags_stale_p6_batch(env):
    """验收 I14：预测依据的 P6 数据日期与当前批次不一致时，接口标 stale_p6，页面先说明预测已过期。"""
    _setup_site(env)
    with closing(dbmod.connect()) as conn:
        schedule.forecast(conn, env["project_id"])
    d = login("eng").get("/api/schedule/forecast").json()
    assert d["stale_p6"] is False and d["current_data_date"] == d["data_date"]
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE p6_imports SET data_date='2000-01-01' WHERE project_id=? AND is_current=1", (env["project_id"],))
    d = login("eng").get("/api/schedule/forecast").json()
    assert d["stale_p6"] is True and d["current_data_date"] == "2000-01-01"
