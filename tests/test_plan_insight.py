"""计划深挖（P6 日历排程、体检、期间对比、工效对比、节点倒计时、雨季提示）的自动测试。

XER 为手写合成数据（不是任何真实项目）：一套 7 天工作制日历，6 月 10-14 日设为雨季例外不工作。
"""
from datetime import date, timedelta

import pytest

from conftest import PW, login  # noqa: F401
from app import db as dbmod
from app.services import p6, p6cal, p6ext, plan_health_agent, plan_insight, schedule

EPOCH = date(1899, 12, 30)
DD1, DD2 = "2026-06-01", "2026-06-08"
RAIN = [date(2026, 6, d) for d in range(10, 15)]


def cal_data(off_days=(), half_days=()) -> str:
    day = "(0||{n}()((0||0(s|08:00|f|12:00)())(0||1(s|13:00|f|17:00)())))"
    exc = [f"(0||{i}(d|{(d - EPOCH).days})())" for i, d in enumerate(off_days)]
    exc += [f"(0||{i + 100}(d|{(d - EPOCH).days})((0||0(s|08:00|f|12:00)())))" for i, d in enumerate(half_days)]
    return ("(0||CalendarData()((0||DaysOfWeek()(" + "".join(day.format(n=n) for n in range(1, 8)) + "))"
            "(0||VIEW(ShowTotal|N)())(0||Exceptions()(" + "".join(exc) + "))))")


# code, name, start, dur(工作日), preds, type, pct@DD1, pct@DD2, cstr
ACTS = [
    ("M100", "开工", "2026-05-25", 0, [], "TT_Mile", 100, 100, None),
    ("A100", "清表", "2026-05-25", 10, ["M100"], "TT_Task", 50, 100, None),
    ("A200", "路基填筑", "2026-06-05", 20, ["A100"], "TT_Task", 0, 10, None),
    ("A300", "涵洞施工", "2026-06-05", 60, [], "TT_Task", 0, 0, None),          # 缺紧前、工期超 44 天
    ("M900", "路基完成", "2026-07-01", 0, ["A200"], "TT_FinMile", 0, 0, ("CS_MEOB", "2026-07-20")),
]
UDF = {"A100": ("m", "1000", "100m/d"), "A200": ("m3", "20000 m3", "1000m3/d"), "A300": ("m", "500 m3", "10m/d")}


def make(dd: str, slip: int = 0) -> bytes:
    rows, preds = [], []
    ids = {a[0]: str(500 + i) for i, a in enumerate(ACTS)}
    for code, name, start, dur, ps, tt, p1, p2, cst in ACTS:
        pct = p1 if dd == DD1 else p2
        s = date.fromisoformat(start)
        f = s + timedelta(days=max(0, dur - 1)) + timedelta(days=slip if code in ("A200", "M900") and dd == DD2 else 0)
        rows.append({
            "task_id": ids[code], "proj_id": "1", "wbs_id": "W1", "clndr_id": "9", "task_code": code, "task_name": name, "task_type": tt,
            "status_code": "TK_Complete" if pct >= 100 else ("TK_Active" if pct > 0 else "TK_NotStart"),
            "phys_complete_pct": str(pct), "complete_pct_type": "CP_Phys",
            "target_drtn_hr_cnt": str(dur * 8), "remain_drtn_hr_cnt": str(round(dur * (100 - pct) / 100) * 8),
            "total_float_hr_cnt": "-16" if code == "A200" and dd == DD2 else "400", "free_float_hr_cnt": "0",
            "act_start_date": f"{start} 08:00" if pct > 0 else "", "act_end_date": f"{f} 17:00" if pct >= 100 else "",
            "early_start_date": f"{start} 08:00", "early_end_date": f"{f} 17:00",
            "target_start_date": f"{start} 08:00", "target_end_date": f"{f} 17:00",
            "cstr_type": cst[0] if cst else "", "cstr_date": f"{cst[1]} 17:00" if cst else "",
        })
        preds += [(ids[code], ids[p]) for p in ps]
    out = ["ERMHDR\t19.12"]

    def table(name, records):
        fields = list(records[0].keys())
        out.append(f"%T\t{name}")
        out.append("%F\t" + "\t".join(fields))
        out.extend("%R\t" + "\t".join(str(r.get(k, "")) for k in fields) for r in records)
    table("PROJECT", [{"proj_id": "1", "proj_short_name": "PLAN-T", "last_recalc_date": f"{dd} 17:00", "clndr_id": "9"}])
    table("CALENDAR", [{"clndr_id": "9", "clndr_name": "7天 雨季", "day_hr_cnt": "8", "week_hr_cnt": "56", "clndr_data": cal_data(RAIN)}])
    table("PROJWBS", [{"wbs_id": "W0", "proj_id": "1", "parent_wbs_id": "", "wbs_name": "T", "proj_node_flag": "Y"},
                      {"wbs_id": "W1", "proj_id": "1", "parent_wbs_id": "W0", "wbs_name": "道路", "proj_node_flag": "N"}])
    table("TASK", rows)
    table("TASKPRED", [{"task_pred_id": str(i), "task_id": t, "pred_task_id": p, "pred_type": "PR_FS", "lag_hr_cnt": "0"}
                       for i, (t, p) in enumerate(preds)])
    table("UDFTYPE", [{"udf_type_id": "1", "table_name": "TASK", "udf_type_label": "Unit"},
                      {"udf_type_id": "2", "table_name": "TASK", "udf_type_label": "Road Quantity"},
                      {"udf_type_id": "3", "table_name": "TASK", "udf_type_label": "Road Productivity"}])
    table("UDFVALUE", [{"udf_type_id": str(k + 1), "fk_id": ids[c], "udf_text": v} for c, vals in UDF.items() for k, v in enumerate(vals)])
    out.append("%E")
    return ("\n".join(out) + "\n").encode("utf-8")


_OPEN = []


@pytest.fixture(autouse=True)
def _close():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _setup(env, both=True):
    conn = dbmod.connect()
    _OPEN.append(conn)
    pid, uid = env["project_id"], env["users"]["admin"]
    p6.import_schedule(conn, pid, uid, "p1.xer", make(DD1))
    if both:
        p6.import_schedule(conn, pid, uid, "p2.xer", make(DD2, slip=5))
    return conn, pid


def test_calendar_parse_and_add():
    spec = p6cal.parse_calendar(cal_data(RAIN, half_days=[date(2026, 6, 20)]))
    assert spec["week"] == [8.0] * 7
    assert spec["exceptions"]["2026-06-10"] == 0 and spec["exceptions"]["2026-06-20"] == 4.0
    cal = p6cal.Calendar(spec, 8.0, date(2026, 6, 1))
    # 6-08 开工 5 个工作日：6-08、6-09 干两天，6-10~14 雨季不工作，6-15~17 再干三天 → 6-17 收工（第 17 天结束＝偏移 17）
    assert cal.add(7, 5) == 17
    assert cal.work_between(9, 14) == 0
    assert cal.day_share(date(2026, 6, 20)) == 0.5
    assert p6cal.parse_calendar("坏数据") is None


def test_import_saves_ext_and_forecast_uses_calendar(env):
    conn, pid = _setup(env, both=False)
    imp = p6.current_import(conn, pid)
    ext = p6ext.ext_map(conn, imp["id"])
    assert ext["M900"]["cstr_type"] == "CS_MEOB" and ext["M900"]["cstr_date"] == "2026-07-20"
    assert ext["A200"]["prod_text"] == "1000m3/d" and p6ext.quantity_of(ext["A200"]) == (20000.0, "m3")
    res = schedule.forecast(conn, pid, as_of=DD1)
    assert res["calendar_mode"] == "p6" and "雨季" in res["calendar_note"]
    assert res["legacy"] and res["legacy"]["project_finish"]
    # 雨季 5 天不计工：按 P6 日历的完工比旧口径（日历天近似）晚
    assert res["project_finish"]["forecast"] > res["legacy"]["project_finish"]


def test_health_checks(env):
    conn, pid = _setup(env)
    h = plan_insight.health(conn, pid)
    by = {c["key"]: c for c in h["checks"]}
    assert "A300" in [s["code"] for s in by["open_start"]["sample"]]
    assert "A300" in [s["code"] for s in by["open_finish"]["sample"]]
    assert by["long_dur"]["count"] == 1
    assert by["neg_float"]["count"] == 1 and by["neg_float"]["level"] == "bad"
    assert [s["code"] for s in by["udf_unit"]["sample"]] == ["A300"]          # 数量 m3、工效 m/d
    assert h["summary"]["bad"] >= 1


def test_health_agent_guard(env):
    conn, pid = _setup(env)
    out = plan_health_agent.run(conn, pid, env["users"]["admin"])
    assert out["advice"] and all(a["key"] for a in out["advice"])
    assert plan_health_agent.latest(conn, pid)["import_id"] == out["import_id"]
    prompt = plan_health_agent._prompt(plan_insight.health(conn, pid))
    assert plan_health_agent.problems("建议压缩 37 项作业，节省产值", prompt)
    assert not plan_health_agent.problems("先核对 A300 的紧前逻辑", prompt)


def test_compare_and_productivity(env):
    conn, pid = _setup(env)
    cm = plan_insight.compare(conn, pid)
    assert cm["previous"]["data_date"] == DD1 and cm["current"]["data_date"] == DD2
    assert {r["code"]: r["days"] for r in cm["slipped"]}["A200"] == 5
    assert "A100" in [r["code"] for r in cm["finished"]] and "A200" in [r["code"] for r in cm["started"]]
    pr = plan_insight.productivity(conn, pid)
    rows = {r["code"]: r for r in pr["rows"]}
    # A100：50%→100%，1000m × 50% = 500m；期间 6-02 ~ 完工 6-03（2 个工作日）→ 250 m/d，计划 100m/d
    assert rows["A100"]["actual_rate"] == 250.0 and rows["A100"]["level"] == "fast"
    # A200：0→10%，20000m3 × 10% = 2000m3；6-05 开工 ~ 6-08 共 4 个工作日 → 500 m3/d，计划 1000 → 系数 2
    assert rows["A200"]["actual_rate"] == 500.0 and rows["A200"]["factor"] == 2.0 and rows["A200"]["level"] == "slow"


def test_countdown_and_rain(env):
    conn, pid = _setup(env)
    fc = schedule.forecast(conn, pid, as_of=DD2)
    cd = plan_insight.countdown(conn, pid, today=date(2026, 6, 10), forecast=fc)
    m = cd["rows"][0]
    assert m["code"] == "M900" and m["deadline"] == "2026-07-20" and m["days_left"] == 40
    assert m["forecast"] and m["margin_days"] == (date(2026, 7, 20) - date.fromisoformat(m["forecast"])).days
    rn = plan_insight.rain(conn, pid, today=date(2026, 6, 1))
    assert rn["calendars"][0]["rainy_months"] == []          # 一个月只损失 5 天，未到雨季月阈值
    hit = {a["code"]: a for a in rn["activities"]}
    assert hit["A200"]["lost_days"] == 5.0 and "M900" not in hit


def test_api_permissions_and_pm_scope(env):
    _setup(env)
    pm = login("pm")
    for path in ("/api/plan/health", "/api/plan/compare", "/api/plan/productivity", "/api/plan/countdown", "/api/plan/rain"):
        r = pm.get(path)
        assert r.status_code == 200, path
        assert "产值" not in r.text
    assert login("tech1").get("/api/plan/health").status_code == 403
    assert login("tech1").post("/api/plan/health/agent").status_code == 403
    r = login("admin").post("/api/plan/health/agent")
    assert r.status_code in (200, 403)


def test_old_imports_without_ext(env):
    from conftest import sample_xer
    conn = dbmod.connect()
    _OPEN.append(conn)
    p6.import_schedule(conn, env["project_id"], env["users"]["admin"], "s.xer", sample_xer())
    pid = env["project_id"]
    assert plan_insight.countdown(conn, pid)["rows"] == []
    assert plan_insight.rain(conn, pid)["calendars"] == []
    assert schedule.forecast(conn, pid)["calendar_mode"] == "approx"


def test_gantt_baseline(env):
    _setup(env)
    resp = login("pm").get("/api/plan/gantt")
    r = resp.json()
    assert r["data_date"] == DD2 and r["baseline"]["A100"][0] == "2026-05-25"


def test_replay_alerts_excluded_from_stats(env):
    """回放产生的预警：页面可见并带标识，但不计入人工反馈命中率、应用成效与采纳统计。"""
    from app.services import alert_tags, metrics, progress
    conn = dbmod.connect()
    _OPEN.append(conn)
    pid = env["project_id"]
    ids = []
    for i, st in enumerate(("confirmed", "false_alarm", "confirmed")):
        ids.append(dbmod.insert(conn, "alerts", {
            "project_id": pid, "import_id": 0, "fingerprint": f"t{i}", "activity_code": f"A{i}", "activity_name": "作业", "level": "red", "rule": "late_start", "title": "应开未开",
            "status": st, "feedback_by": env["users"]["admin"], "first_detected": "2026-10-01", "last_detected": "2026-10-01", "as_of": "2026-10-01",
            "detail_json": "{}"}))
    assert progress.feedback_stats(conn, pid)["precision"] == round(2 / 3, 4)
    assert alert_tags.tag(conn, ids[1:], alert_tags.REPLAY, "测试回放") == 2
    s = progress.feedback_stats(conn, pid)
    assert s["confirmed"] == 1 and s["false_alarm"] == 0 and s["precision"] == 1.0 and s["replay_excluded"] == 2
    m = metrics.summary(conn, pid)
    assert m["alerts"]["total"] == 1 and m["alerts"]["replay_excluded"] == 2 and m["alerts"]["feedback_precision"] == 1.0
    rows = {a["id"]: a for a in login("pm").get("/api/alerts").json()}
    assert rows[ids[1]]["replay"] is True and rows[ids[0]]["replay"] is False
