"""T02 工程量可信分级：分包自报 / 港湾核定 分层展示；退回留痕不删除。合计口径不变（离线 mock 模型）。"""
import io
import sqlite3
from contextlib import closing
from datetime import timedelta

from conftest import TODAY, backdate, jpeg, login, sample_xer
from test_weekly import LAST_MON, _note_on, _weekly_project

from app import db as dbmod
from app.services import brief

VOICE = "3#墩桩基施工今天完成2根桩，累计完成82%。钢筋工12人，汽车吊1台。旋挖钻故障停机半天。明天继续3#墩桩基施工"


def _boq_confirmed(pm):
    from openpyxl import Workbook
    pm = login("admin")   # 产值由工程主管/管理员操作，项目经理不看产值
    wb = Workbook()
    ws = wb.active
    ws.append(["编码", "名称", "单位", "单价", "数量"])
    ws.append(["B01", "3#墩桩基施工", "根", 5000, 20])
    buf = io.BytesIO()
    wb.save(buf)
    assert pm.post("/api/output/boq", files={"file": ("boq.xlsx", buf.getvalue())}).json()["items"] == 1
    assert pm.post("/api/output/map/suggest").json()["suggested"] >= 1
    a = next(m for m in pm.get("/api/output/maps").json() if m["activity_code"] == "A1010")
    pm.post("/api/output/maps", json={"ids": [a["id"]], "status": "confirmed"})


def _setup_daily():
    pm = login("pm")
    assert pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    _boq_confirmed(pm)
    return pm, login("tech1")


def _draft_today(tech):
    log = tech.post("/api/logs", json={}).json()
    tech.post("/api/media", data={"log_id": str(log["id"])}, files=[("files", ("p.jpg", jpeg((100, 90, 80)), "image/jpeg"))])
    d = tech.post(f"/api/logs/{log['id']}/draft", json={"voice_text": VOICE}).json()
    backdate(log["id"])
    return d


def _submit(tech, log_id, version):
    s = tech.post(f"/api/logs/{log_id}/submit", json={"version": version, "active_seconds": 200})
    assert s.status_code == 200, s.text
    return s.json()


def _reviewer():
    """日志审核账号：项目经理不审核日志（log_review 只含工程主管与管理员）；默认测试项目没有工程主管，用管理员代审。"""
    return login("admin")

def _week(pm):
    return login("admin").get("/api/output/overview").json()["series"][-1]


def _a1010(pm):
    return next(x for x in pm.get("/api/p6/activities").json()["items"] if x["code"] == "A1010")


def _rows(log_id):
    with closing(dbmod.connect()) as conn:
        return conn.execute("SELECT * FROM log_progress WHERE log_id=? ORDER BY id", (log_id,)).fetchall()


def test_submitted_is_self_reported_then_approved_is_confirmed_total_unchanged(env):
    pm, tech = _setup_daily()
    d = _draft_today(tech)
    s = _submit(tech, d["id"], d["version"])
    assert s["status"] == "submitted"
    # 提交后：合计 = 原值（2 根 × 5000）；自报 = 该数量，核定 = 0
    w = _week(pm)
    assert w["system"] == 10000 and w["system_self"] == 10000 and w["system_approved"] == 0
    assert w["log_ids_self"] == [d["id"]] and w["log_ids_approved"] == []
    a = _a1010(pm)
    assert a["log_qty"] == 2 and a["log_qty_self"] == 2 and a["log_qty_approved"] is None
    assert a["log_trust"] == "分包自报" and a["log_ids_self"] == [d["id"]]
    # 港湾审核通过：自报 = 0，核定 = 该数量；合计不变
    assert _reviewer().post(f"/api/logs/{d['id']}/review", json={"action": "approve"}).json()["status"] == "approved"
    w = _week(pm)
    assert w["system"] == 10000 and w["system_self"] == 0 and w["system_approved"] == 10000
    assert w["log_ids_self"] == [] and w["log_ids_approved"] == [d["id"]]
    a = _a1010(pm)
    assert a["log_qty"] == 2 and a["log_qty_self"] is None and a["log_qty_approved"] == 2 and a["log_trust"] == "港湾核定"


def test_return_voids_instead_of_deleting_and_resubmit_does_not_double_count(env):
    pm, tech = _setup_daily()
    d = _draft_today(tech)
    s = _submit(tech, d["id"], d["version"])
    assert len(_rows(d["id"])) == 1
    r = _reviewer().post(f"/api/logs/{d['id']}/review", json={"action": "return", "note": "桩数与现场不符"})
    assert r.status_code == 200 and r.json()["status"] == "returned"
    # 数据库仍可查到，且带失效时间与原因
    rows = _rows(d["id"])
    assert len(rows) == 1 and rows[0]["qty"] == 2 and rows[0]["voided_at"] and "桩数与现场不符" in rows[0]["void_reason"]
    # 退回的数量不计入任何统计：产值、P6 回填进度、P6 导出
    w = _week(pm)
    assert w["system"] is None and w["system_self"] is None and w["system_approved"] is None
    a = _a1010(pm)
    assert a["log_pct"] is None and a["log_qty"] is None and a["log_trust"] == ""
    # 日志详情：有效行为空，退回原数量与原因可见
    v = tech.get(f"/api/logs/{d['id']}").json()
    assert v["progress_rows"]["active"] == []
    vp = v["progress_rows"]["voided"]
    assert len(vp) == 1 and vp[0]["qty"] == 2 and vp[0]["activity_code"] == "A1010" and "桩数与现场不符" in vp[0]["void_reason"]
    # 重新提交：正常计入，且不重复计数
    s2 = _submit(tech, d["id"], v["version"])
    assert s2["status"] == "submitted"
    rows = _rows(d["id"])
    assert len(rows) == 2 and sum(1 for x in rows if x["voided_at"] is None) == 1
    w = _week(pm)
    assert w["system"] == 10000 and w["system_self"] == 10000 and w["system_approved"] == 0
    assert _a1010(pm)["log_qty"] == 2
    # 再次退回 → 再次通过，仍只有一份有效数量，历史失效行都保留
    _reviewer().post(f"/api/logs/{d['id']}/review", json={"action": "return", "note": "再核对"})
    v = tech.get(f"/api/logs/{d['id']}").json()
    _submit(tech, d["id"], v["version"])
    _reviewer().post(f"/api/logs/{d['id']}/review", json={"action": "approve"})
    rows = _rows(d["id"])
    assert len(rows) == 3 and sum(1 for x in rows if x["voided_at"] is None) == 1
    assert _week(pm)["system"] == 10000 and _week(pm)["system_approved"] == 10000


def test_p6_export_has_approval_status_column(env):
    from openpyxl import load_workbook
    pm, tech = _setup_daily()
    d = _draft_today(tech)
    _submit(tech, d["id"], d["version"])

    def export():
        x = pm.get("/api/p6/export-progress")
        assert x.status_code == 200
        ws = load_workbook(io.BytesIO(x.content)).active
        rows = list(ws.iter_rows(values_only=True))
        head = list(rows[0])
        assert head[-3:] == ["qty_self", "qty_approved", "approval_status"] and rows[1][-1] == "核定状态"
        return {r[0]: dict(zip(head, r)) for r in rows[2:]}

    t = export()["A1010"]
    assert t["qty_self"] == 2 and t["qty_approved"] is None and t["approval_status"] == "分包自报" and t["phys_complete_pct"] == 82
    _reviewer().post(f"/api/logs/{d['id']}/review", json={"action": "approve"})
    t = export()["A1010"]
    assert t["qty_self"] is None and t["qty_approved"] == 2 and t["approval_status"] == "港湾核定"
    # 已核定的日志不能再退回；另提交一份被退回的日志后，导出里不再有失效数量
    assert _reviewer().post(f"/api/logs/{d['id']}/review", json={"action": "return", "note": "重做"}).status_code == 400


def test_report_and_output_split(env):
    pm, tech = _setup_daily()
    d = _draft_today(tech)
    _submit(tech, d["id"], d["version"])
    adm = login("admin")   # 完整版（含产值节、自报/核定拆分）；项目经理看到的是剔除版（见 test_pm_scope.py）
    rep = adm.get(f"/api/reports/{pm.post('/api/reports', json={'kind': 'week'}).json()['id']}").json()
    q = next(p for p in rep["facts"]["progress"] if p["code"] == "A1010")["qty"][0]
    assert q["qty"] == 2 and q["self"] == 2 and q["approved"] == 0  # 合计不变，拆分并存
    out = rep["facts"]["output"]
    assert out["total"] == 10000 and out["self_total"] == 10000 and out["approved_total"] == 0
    assert "非计量认证" in out["note"]
    hp, rp = rep["tables"]["progress"]["headers"], rep["tables"]["progress"]["rows"]
    assert hp[:5] == ["作业代码", "作业名称", "本期完成量（合计）", "其中分包自报", "其中港湾核定"]
    row = next(r for r in rp if r[0] == "A1010")
    assert row[2] == "2根" and row[3] == "2根" and row[4] == "0根"
    ho = rep["tables"]["output"]["headers"]
    assert "内部产值统计（非计量认证）" in ho and "分包自报 / 港湾核定" in ho
    # 核定后重新生成：拆分翻转，合计不变
    _reviewer().post(f"/api/logs/{d['id']}/review", json={"action": "approve"})
    rep2 = adm.get(f"/api/reports/{pm.post('/api/reports', json={'kind': 'week'}).json()['id']}").json()
    q = next(p for p in rep2["facts"]["progress"] if p["code"] == "A1010")["qty"][0]
    assert q["qty"] == 2 and q["self"] == 0 and q["approved"] == 2
    assert rep2["facts"]["output"]["total"] == 10000 and rep2["facts"]["output"]["approved_total"] == 10000
    # Word 导出可用，且说明了分层口径
    docx = adm.get(f"/api/reports/{rep2['id']}/docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"
    from docx import Document
    text = "\n".join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)
    assert "分包自报" in text and "港湾核定" in text and "非计量认证" in text


def test_weekly_return_voids_and_resubmit_keeps_daily_flow(env):
    _weekly_project(env)
    pm = login("pm")
    assert pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(data_date=(LAST_MON - timedelta(days=1)).isoformat()),
                                                     "application/octet-stream")}).status_code == 200
    _boq_confirmed(pm)
    tech = login("tech1")
    _note_on(tech, LAST_MON, "3#墩桩基施工今天完成2根，累计完成30%。钢筋工12人")
    _note_on(tech, LAST_MON + timedelta(days=1), "3#墩桩基施工完成3根，累计完成45%")
    wk = tech.post("/api/logs", json={"period": "week", "log_date": (LAST_MON + timedelta(days=3)).isoformat()}).json()
    w = tech.post(f"/api/logs/{wk['id']}/draft", json={"voice_text": ""}).json()
    backdate(wk["id"])
    s = _submit(tech, wk["id"], w["version"])
    day_ids = [d["log_id"] for d in s["days"] if d["log_id"]]
    assert day_ids and all(d["status"] == "submitted" for d in s["days"] if d["log_id"])
    with closing(dbmod.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM log_progress").fetchone()["n"] == 1  # 每日日志不重复计量
    series = login("admin").get("/api/output/overview").json()["series"]
    wk_row = next(x for x in series if x["week"] == LAST_MON.isoformat())
    assert wk_row["system"] == 25000 and wk_row["system_self"] == 25000 and wk_row["system_approved"] == 0
    with closing(dbmod.connect()) as conn:
        k = brief.kpis(conn, env["project_id"], TODAY)["output"]
    assert k["week_source"] == "system" and k["week"] == 25000 and k["week_self"] == 25000 and k["week_approved"] == 0
    # 周确认被退回：数量失效留痕，每日日志回到草稿（与原行为一致）
    r = _reviewer().post(f"/api/logs/{wk['id']}/review", json={"action": "return", "note": "第二天数量不对"}).json()
    assert r["status"] == "returned" and all(d["status"] == "draft" for d in r["days"] if d["log_id"])
    rows = _rows(wk["id"])
    assert len(rows) == 1 and rows[0]["voided_at"] and "第二天数量不对" in rows[0]["void_reason"]
    series = login("admin").get("/api/output/overview").json()["series"]
    assert next(x for x in series if x["week"] == LAST_MON.isoformat())["system"] is None
    assert _a1010(pm)["log_qty"] is None
    # 重新提交 + 通过：只计一份，核定 = 合计
    v = tech.get(f"/api/logs/{wk['id']}").json()
    assert v["progress_rows"]["voided"] and not v["progress_rows"]["active"]
    _submit(tech, wk["id"], v["version"])
    ok = _reviewer().post(f"/api/logs/{wk['id']}/review", json={"action": "approve"}).json()
    assert ok["status"] == "approved" and all(d["status"] == "approved" for d in ok["days"] if d["log_id"])
    series = login("admin").get("/api/output/overview").json()["series"]
    wk_row = next(x for x in series if x["week"] == LAST_MON.isoformat())
    assert wk_row["system"] == 25000 and wk_row["system_approved"] == 25000 and wk_row["system_self"] == 0
    assert len(_rows(wk["id"])) == 2 and sum(1 for x in _rows(wk["id"]) if x["voided_at"] is None) == 1


def test_old_database_migrates_without_error():
    """旧库（无失效列、user_version=5）启动迁移：自动加列，原有数据保留，后续建表脚本不报错。"""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = dbmod._dict_factory
    conn.execute("""CREATE TABLE log_progress (id INTEGER PRIMARY KEY, log_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
                    log_date TEXT NOT NULL, activity_code TEXT NOT NULL, qty REAL, unit TEXT NOT NULL DEFAULT '', pct REAL, note TEXT NOT NULL DEFAULT '')""")
    conn.execute("INSERT INTO log_progress (log_id, project_id, log_date, activity_code, qty, unit) VALUES (1, 1, '2026-01-01', 'A1', 3, '根')")
    conn.execute("PRAGMA user_version=5")
    dbmod._migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(log_progress)").fetchall()}
    assert {"voided_at", "void_reason"} <= cols
    row = conn.execute("SELECT * FROM log_progress").fetchone()
    assert row["qty"] == 3 and row["voided_at"] is None and row["void_reason"] == ""
    dbmod._migrate(conn)  # 重复执行幂等
    conn.executescript(dbmod.SCHEMA)
    assert dbmod.SCHEMA_VERSION >= 6
    conn.close()


def test_no_other_reader_counts_voided_rows(env):
    """不经日志状态连接的读取点（工程主管台的“有进度但未映射”作业数）也排除失效行。"""
    from app.services import role_views
    pm, tech = _setup_daily()
    d = _draft_today(tech)
    _submit(tech, d["id"], d["version"])
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE boq_map SET status='rejected'")
        assert role_views.eng_overview(conn, env["project_id"])["mapping"]["unmapped_progress"] == 1
    _reviewer().post(f"/api/logs/{d['id']}/review", json={"action": "return", "note": "退"})
    with closing(dbmod.connect()) as conn:
        assert role_views.eng_overview(conn, env["project_id"])["mapping"]["unmapped_progress"] == 0
