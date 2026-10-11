"""产值页“周报口径”指标（2026-10-09）：基准/更新产值计划、完成、差值、开累与完成百分比、本年度；缺数据为 None，不补零。
RJ 10-10 决定 2：项目整体完成百分比 = 开累产值 ÷ 清单总产值（按产值口径），分母缺失或为零为 None；
总体计划完成百分比来源未核实，不套用、恒为 None。周报发布时冻结十项指标快照，历史不回填。"""
import io
from datetime import date, timedelta

from openpyxl import Workbook

from app import db as dbmod
from app.security import hash_password
from app.services import output
from conftest import PW, login


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _eng(env):
    conn = dbmod.connect()
    uid = dbmod.insert(conn, "users", {"username": "eng", "name": "主管", "role": "eng_sup", "password_hash": hash_password(PW),
                                       "project_id": env["project_id"], "wbs_scope": "", "email": "", "active": 1, "created_at": dbmod.now_iso()})
    conn.close()
    return uid


def _weeks():
    mon = date.today() - timedelta(days=date.today().weekday())
    return [(mon - timedelta(days=7 * i)).isoformat() for i in (3, 2, 1, 0)]   # 前三周 + 本周


def test_import_two_plan_columns_and_kpis(env):
    _eng(env)
    w = _weeks()
    c = login("eng")
    head = ["开始日期", "结束日期", "实际产值", "基准产值计划", "更新产值计划", "币种"]
    rows = [head] + [[w[i], None, a, b, p, "USD"] for i, (a, b, p) in enumerate([(100, 120, 110), (200, 220, 210), (300, 330, 320)])]
    rows.append([w[3], None, None, 400, 350, "USD"])
    r = c.post("/api/output/records", files={"file": ("r.xlsx", _xlsx(rows))})
    assert r.status_code == 200, r.text
    rep = c.get("/api/output/overview").json()["report"]
    lw = rep["last_week"]
    assert (lw["week"], lw["baseline"], lw["planned"], lw["actual"], lw["diff"]) == (w[2], 330, 320, 300, -20)
    assert rep["this_week"] == {"week": w[3], "baseline": 400, "planned": 350}
    cum = rep["cumulative"]
    assert cum["actual"] == 600 and cum["baseline_cum"] == 670
    assert cum["boq_total"] is None and cum["pct"] is None and cum["gap"] is None   # 没有清单总产值：不算百分比
    assert "清单总产值" in rep["missing"] and cum["pct_why"] == "清单总产值未录入"
    # 只录合同额：不拿合同额顶替分母
    yr = rep["year"]["year"]
    assert c.put("/api/output/plan", json={"contract_amount": 10000, "year": yr, "year_plan": 2000}).status_code == 200
    cum = c.get("/api/output/overview").json()["report"]["cumulative"]
    assert cum["contract"] == 10000 and cum["pct"] is None
    # 录入清单总产值后：整体完成 = 600/10000；总体计划完成百分比来源未核实，不算，进度差随之为空
    assert c.put("/api/output/plan", json={"contract_amount": 10000, "boq_total": 10000, "year": yr, "year_plan": 2000}).status_code == 200
    rep = c.get("/api/output/overview").json()["report"]
    assert rep["cumulative"]["pct"] == 6.0 and rep["cumulative"]["plan_pct"] is None and rep["cumulative"]["gap"] is None
    assert rep["year"]["plan"] == 2000 and rep["missing"] == []


def test_plan_only_file_keeps_actual_and_missing_baseline_not_zero(env):
    _eng(env)
    w = _weeks()
    c = login("eng")
    c.post("/api/output/records", files={"file": ("a.xlsx", _xlsx([["开始日期", "实际产值"], [w[1], 50], [w[2], 70]]))})
    c.post("/api/output/records", files={"file": ("p.xlsx", _xlsx([["开始日期", "更新产值计划"], [w[2], 90]]))})   # 只补计划，不清空实际
    rep = c.get("/api/output/overview").json()["report"]
    assert rep["last_week"]["actual"] == 70 and rep["last_week"]["planned"] == 90 and rep["last_week"]["baseline"] is None
    assert rep["cumulative"]["actual"] == 120 and rep["cumulative"]["baseline_cum"] is None and rep["cumulative"]["plan_pct"] is None


def test_plan_api_validation_and_roles(env):
    _eng(env)
    c = login("eng")
    assert c.put("/api/output/plan", json={"contract_amount": -1}).status_code == 400
    assert c.put("/api/output/plan", json={"year": "26", "year_plan": 1}).status_code == 400
    assert login("tech1").put("/api/output/plan", json={"contract_amount": 1}).status_code == 403
    assert login("pm").put("/api/output/plan", json={"contract_amount": 1}).status_code == 403   # 项目经理只读产值，不维护计划
    assert c.put("/api/output/plan", json={"boq_total": 0}).status_code == 400


def test_report_kpis_no_data(env):
    conn = dbmod.connect()
    rep = output.report_kpis(conn, env["project_id"])
    conn.close()
    assert rep["last_week"]["actual"] is None and rep["cumulative"]["actual"] is None and rep["year"]["actual"] is None


def test_boq_total_denominator_and_report_lag_year(env):
    """与产值台账一致：百分比分母用清单总产值；周产值按“上报日 = 周末 + N 天”归年度（跨年那一周算新年）。"""
    uid = _eng(env)
    conn = dbmod.connect()
    pid = env["project_id"]
    for s, e, a, b in (("2025-12-15", "2025-12-21", 100, 100), ("2025-12-22", "2025-12-28", 50, 60), ("2025-12-29", "2026-01-04", 30, 40)):
        dbmod.insert(conn, "output_records", {"project_id": pid, "period_start": s, "period_end": e, "actual": a, "baseline": b, "planned": b,
                                              "currency": "USD", "source": "record"})
    output.save_plan_config(conn, pid, uid, 5000, "2026", 1000, boq_total=1000, report_lag_days=4)
    series = output.weekly_series(conn, pid, 4, date(2026, 1, 7))
    rep = output.report_kpis(conn, pid, series, date(2026, 1, 7))
    conn.close()
    cum = rep["cumulative"]
    assert cum["denom_name"] == "清单总产值" and cum["pct"] == 18.0 and cum["plan_pct"] is None and cum["gap"] is None
    assert rep["year"]["year"] == "2026" and rep["year"]["actual"] == 80   # 12-22 那周上报日 12-28+4=01-01，算 2026


def test_pct_none_when_currencies_differ(env):
    """开累产值与清单总产值币种不一致：不换算，整体完成百分比为空并说明原因。"""
    uid = _eng(env)
    conn = dbmod.connect()
    pid = env["project_id"]
    dbmod.insert(conn, "output_records", {"project_id": pid, "period_start": "2026-01-05", "period_end": "2026-01-11", "actual": 100,
                                          "currency": "USD", "source": "record"})
    dbmod.insert(conn, "boq_items", {"project_id": pid, "code": "1-1", "name": "土方", "unit": "m3", "unit_price": 1, "currency": "CNY", "contract_qty": 1})
    output.save_plan_config(conn, pid, uid, None, boq_total=1000)
    cum = output.report_kpis(conn, pid, today=date(2026, 1, 14))["cumulative"]
    conn.close()
    assert cum["actual"] == 100 and cum["pct"] is None and "币种不一致" in cum["pct_why"]


def test_kpi10_items_units_and_diff(env):
    """十项指标：名称照用户原名，金额按万美元，差值 = 完成产值 − 更新计划；未报送为空不补零。"""
    uid = _eng(env)
    conn = dbmod.connect()
    pid = env["project_id"]
    dbmod.insert(conn, "output_records", {"project_id": pid, "period_start": "2026-01-05", "period_end": "2026-01-11", "actual": 30000,
                                          "planned": 50000, "baseline": 60000, "currency": "USD", "source": "record"})
    output.save_plan_config(conn, pid, uid, None, "2026", 1000000, boq_total=200000)
    k = output.kpi10(conn, pid, week="2026-01-05")
    conn.close()
    items = {x["key"]: x for x in k["items"]}
    assert [x["label"] for x in k["items"]] == [x[1] for x in output.KPI10] and len(items) == 10
    assert k["period"] == {"week": "2026-01-05", "start": "2026-01-05", "end": "2026-01-11"} and k["unit"] == "万美元"
    assert (items["last_baseline"]["value"], items["last_planned"]["value"], items["last_actual"]["value"]) == (6.0, 5.0, 3.0)
    assert items["last_diff"]["value"] == -2.0 and items["last_diff"]["unit"] == "万美元"
    assert items["pct"]["value"] == 15.0 and items["plan_pct"]["value"] is None and items["plan_pct"]["why"] == "来源未核实，暂不计算"
    assert items["gap"]["value"] is None and items["gap"]["unit"] == "个百分点"
    assert items["year_plan"]["value"] == 100.0 and items["year_actual"]["value"] == 3.0 and items["cum"]["value"] == 3.0
    assert k["source"]["version"] and k["source"]["data_date"] == "2026-01-11"


def test_report_publish_freezes_kpi10_snapshot(env):
    """周报发布时冻结十项指标；之后数据再变，已发布周报仍读快照；快照上线前已发布的周报不回填。"""
    from test_pm_scope import _eng_sup, _submitted_report
    _eng_sup(env)
    pm, rep = _submitted_report(env)
    assert rep["kpi10"]["kind"] == "live" and len(rep["kpi10"]["items"]) == 10
    verified = [{"field": x["field"], "number": x["number"]} for x in rep["number_check"]]
    pub = pm.put(f"/api/reports/{rep['id']}", json={"narrative": rep["narrative"], "status": "published", "verified": verified}).json()
    snap = pub["kpi10"]
    assert snap["kind"] == "snapshot" and snap["period"]["start"] == rep["period_start"]
    conn = dbmod.connect()
    dbmod.insert(conn, "output_records", {"project_id": env["project_id"], "period_start": rep["period_start"], "period_end": rep["period_end"],
                                          "actual": 99999, "currency": "USD", "source": "record"})
    conn.close()
    again = pm.get(f"/api/reports/{rep['id']}").json()["kpi10"]
    assert again == snap   # 已发布读快照，不随后来的数据变化
    # 快照上线前已发布的周报：没有快照，如实说明，不回填
    conn = dbmod.connect()
    conn.execute("DELETE FROM knowledge WHERE kind='report_kpi10'")
    conn.close()
    old = pm.get(f"/api/reports/{rep['id']}").json()["kpi10"]
    assert old["kind"] == "none" and "不回填" in old["note"]
    conn = dbmod.connect()
    assert conn.execute("SELECT COUNT(*) n FROM knowledge WHERE kind='report_kpi10'").fetchone()["n"] == 0
    conn.close()
