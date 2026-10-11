"""一数一源纠正（演示副本专用，在 fill 之前跑）：同一指标只留一个源头，各页取数一致。

1. 被退回、漏交的周进度确认按“剧本真值”补交并审核（工程量只来自周确认这一处）；
2. 周确认工程量单位统一成工程量清单单位（m3/m2/m），否则产值统计会把同一作业当成“单位不符”漏算；
3. 周确认累计% = 开累工程量 ÷ 清单数量（与 P6 各期完成% 同一口径，原来 AI 摘录的 100%、10.4% 等错值纠正）；
4. 周产值记录 = 系统按周确认工程量×清单单价的统计（一个数）；更新/基准产值计划 = P6 当期/基准计划 × 清单单价；
   币种统一 USD；清单总产值 = 工程量清单合计（项目整体完成百分比的唯一分母）；合同额不录（合同留空）；
5. 语音转写错字（半河站、母绑）更正。
每一步都打印改动，汇总写进 <data-dir>/fix_report.json。可重复运行（已改过的不再改）。

用法：python -m tools.showcase.fix --data-dir data_showcase
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT: list[dict] = []
UNIT_MAP = {"立方米": "m3", "m³": "m3", "方": "m3", "平方米": "m2", "m²": "m2", "㎡": "m2", "米": "m", "延米": "m"}
TYPO = [("半河站", "拌合站"), ("母绑", "模板")]
TRADE = {"焊工": "电焊工", "焊接工": "电焊工", "击手": "机手", "机械操作手": "机手"}
MACH = {"挖机": "挖掘机", "旋挖钻": "旋挖钻机", "商混罐车": "混凝土罐车", "罐车": "混凝土罐车", "混凝土搅拌车": "混凝土罐车", "铲车": "装载机", "运输车": "自卸车"}
CUR = "USD"


def note(item, before, after, why):
    REPORT.append({"item": item, "before": before, "after": after, "why": why})
    print("·", item, "|", before, "→", after)


def at(dt: str):
    import time_machine
    return time_machine.travel(datetime.fromisoformat(dt).astimezone(), tick=False)


# ----------------------------------------------------------------------------- 1 周确认补交与审核

WEEK_FIX = [  # (log_id, 作者, 技术员重新提交时间, 工程主管审核时间)
    (353, "zhao", "2026-09-22T10:20:00", "2026-09-22T16:40:00"),
    (355, "li", "2026-09-22T11:05:00", "2026-09-22T16:45:00"),
    (382, "sun", "2026-09-28T15:10:00", "2026-09-29T09:30:00"),
    (383, "li", None, "2026-09-29T09:40:00"),
]


def weeks(c):
    from tools.sim_replay import scenario as S
    truth = S.daily_truth()
    for lid, tech, t_sub, t_ok in WEEK_FIX:
        row = c.q("SELECT status, log_date FROM logs WHERE id=?", lid)
        if not row:
            continue
        st, monday = row[0]["status"], row[0]["log_date"]
        if st in ("draft", "returned") and t_sub:
            with at(t_sub):
                lg = c.call(tech, "GET", f"/api/logs/{lid}")
                rows = ((lg.get("week_review") or {}).get("rows")) or []
                if not rows:
                    c.call(tech, "POST", f"/api/logs/{lid}/draft", json={})
                    lg = c.call(tech, "GET", f"/api/logs/{lid}")
                    rows = ((lg.get("week_review") or {}).get("rows")) or []
                wk = {}
                d0 = date.fromisoformat(monday)
                for i in range(7):
                    for w in truth.get((d0 + timedelta(days=i)).isoformat(), {}).get(tech, []):
                        wk[w.code] = wk.get(w.code, 0) + w.qty
                dec = []
                for r in rows:
                    true = wk.get(r.get("key"))
                    if not true:
                        dec.append({"key": r["key"], "decision": "reject", "reason": "本周该作业没有实际完成量"})
                    else:
                        dec.append({"key": r["key"], "decision": "modify", "final": round(true, 2), "reason": "按每日记录与照片核对后修正"})
                ver = lg.get("version") or (lg.get("log") or {}).get("version")
                c.call(tech, "POST", f"/api/logs/{lid}/submit", json={"version": ver, "active_seconds": 380, "decisions": dec})
            note(f"周进度确认 L{lid}（{monday} 周）", st, "已重新提交", "退回/漏交的周确认按每日记录补交，工程量只来自周确认")
            st = "submitted"
        if st == "submitted":
            with at(t_ok):
                lg = c.call("eng", "GET", f"/api/logs/{lid}")
                rows = ((lg.get("dq_review") or {}).get("rows")) or []
                dec = [{"key": r.get("key"), "decision": "false", "reason": "核对每日记录与照片，数量一致"} for r in rows]
                c.call("eng", "POST", f"/api/logs/{lid}/review", json={"action": "approve", **({"decisions": dec} if dec else {})})
            note(f"周进度确认 L{lid}", "已提交待审核", "工程主管审核通过", "补交后审核，计入核定工程量")


# ----------------------------------------------------------------------------- 2/3 单位与累计%

def units_pct(conn):
    boq = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT m.activity_code, b.unit, b.contract_qty FROM boq_map m JOIN boq_items b ON b.id=m.boq_id WHERE m.status='confirmed'")}
    n = 0
    for rid, code, unit in conn.execute("SELECT id, activity_code, unit FROM log_progress").fetchall():
        target = boq.get(code, (None,))[0]
        u = UNIT_MAP.get(unit, unit)
        if target and u.lower() == target.lower() and unit != target:
            conn.execute("UPDATE log_progress SET unit=? WHERE id=?", (target, rid))
            n += 1
    if n:
        note("周确认工程量单位", "立方米/m³/m²/米 混写", "统一为清单单位 m3/m2/m", f"{n} 行；单位不一致时产值统计会漏算")
    cum: dict[str, float] = {}
    changed = []
    for rid, code, qty, pct in conn.execute("""SELECT lp.id, lp.activity_code, lp.qty, lp.pct FROM log_progress lp JOIN logs l ON l.id=lp.log_id
                                               WHERE lp.voided_at IS NULL AND l.status IN ('submitted','approved') ORDER BY lp.log_date, lp.id""").fetchall():
        if code not in boq or qty is None or not boq[code][1]:
            continue
        cum[code] = cum.get(code, 0) + qty
        new = round(cum[code] / boq[code][1] * 100, 1)
        if pct is None or abs(pct - new) > 0.05:
            changed.append((code, pct, new))
            conn.execute("UPDATE log_progress SET pct=?, note=? WHERE id=?", (new, f"累计% = 开累工程量 {cum[code]:g} ÷ 清单数量 {boq[code][1]:g}", rid))
    if changed:
        note("周确认累计完成%", "；".join(f"{c} {'空' if p is None else p}" for c, p, _ in changed),
             "；".join(f"{c} {n}" for c, _, n in changed), "统一为“开累工程量 ÷ 清单数量”，与 P6 各期完成% 同一口径")


# ----------------------------------------------------------------------------- 4 产值

def output(conn, appconn):
    from app.services import output as O
    from app.services.progress import planned_pct
    pid = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()[0]
    old_cur = conn.execute("SELECT COUNT(*) FROM boq_items WHERE project_id=? AND IFNULL(currency,'')<>?", (pid, CUR)).fetchone()[0]
    if old_cur:
        conn.execute("UPDATE boq_items SET currency=? WHERE project_id=?", (CUR, pid))
        note("工程量清单币种", "未注明", CUR, "产值金额与清单总产值同一币种，完成百分比才可计算")
    imps = [dict(zip(("id", "data_date"), r)) for r in conn.execute("SELECT id, data_date FROM p6_imports WHERE project_id=? ORDER BY data_date", (pid,))]
    price = {r[0]: r[1] * 1.0 for r in conn.execute(
        "SELECT m.activity_code, b.unit_price*b.contract_qty FROM boq_map m JOIN boq_items b ON b.id=m.boq_id WHERE m.status='confirmed'")}

    def acts(imp_id):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)")]
        return [dict(zip(cols, r)) for r in conn.execute("SELECT * FROM activities WHERE import_id=?", (imp_id,))]

    def plan_value(imp, d0: date, d1: date) -> float:
        dd = date.fromisoformat(imp["data_date"])
        v = 0.0
        for a in acts(imp["id"]):
            if a["code"] not in price:
                continue
            p0, p1 = planned_pct(a, d0, dd), planned_pct(a, d1, dd)
            if p0 is not None and p1 is not None and p1 > p0:
                v += (p1 - p0) / 100 * price[a["code"]]
        return round(v, 2)

    base = imps[0]
    mondays = [date(2026, 9, 7) + timedelta(days=7 * i) for i in range(4)]   # 09-07 … 09-28（演示日 10-04：9-28 周周一才上报，只有计划）
    for m in mondays:
        e = m + timedelta(days=6)
        upd = [i for i in imps if i["data_date"] <= m.isoformat()][-1]
        planned, baseline = plan_value(upd, m, e + timedelta(days=1)), plan_value(base, m, e + timedelta(days=1))
        actual = O.compute(appconn, pid, m.isoformat(), e.isoformat())["total"] if e < date(2026, 10, 4) else None
        actual = round(actual, 2) if actual else None
        old = conn.execute("SELECT id, planned, actual, baseline, currency FROM output_records WHERE project_id=? AND period_start=? AND source='record'",
                           (pid, m.isoformat())).fetchone()
        if old:
            conn.execute("UPDATE output_records SET planned=?, actual=?, baseline=?, currency=?, note=? WHERE id=?",
                         (planned, actual, baseline, CUR, "仿真演示数据：完成=周确认工程量×清单单价；计划=P6×清单单价", old[0]))
            if old[2] != actual or old[1] != planned:
                note(f"周产值记录 {m}", f"完成 {old[2]}，计划 {old[1]}，基准 {old[3]}，币种 {old[4] or '空'}",
                     f"完成 {actual}，计划 {planned}，基准 {baseline}，{CUR}", "完成产值只认一个数：周确认工程量×清单单价（与产值页系统统计一致）")
        else:
            conn.execute("""INSERT INTO output_records (project_id, period_start, period_end, planned, actual, currency, source, note, baseline)
                            VALUES (?,?,?,?,?,?,?,?,?)""", (pid, m.isoformat(), e.isoformat(), planned, actual, CUR, "record",
                                                         "仿真演示数据：计划=P6×清单单价", baseline))
            note(f"周产值记录 {m}", "无", f"计划 {planned}，基准 {baseline}，完成 {actual}", "补本周计划，十项指标“本周计划”才有数")
    boq_total = conn.execute("SELECT SUM(unit_price*contract_qty) FROM boq_items WHERE project_id=?", (pid,)).fetchone()[0]
    year_plan = plan_value(base, date(2026, 9, 7), date(2027, 1, 1))
    cfg = json.loads(conn.execute("SELECT config_json FROM projects WHERE id=?", (pid,)).fetchone()[0] or "{}")
    plan = cfg.get("output_plan") or {}
    want = {"contract_amount": None, "boq_total": round(boq_total, 2), "year_plan": {"2026": year_plan}, "report_lag_days": 0,
            "source": "仿真演示数据：清单总产值=工程量清单合计；2026 年度计划=P6 基准计划×清单单价"}
    if plan != want:
        cfg["output_plan"] = want
        conn.execute("UPDATE projects SET config_json=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), pid))
        note("项目产值参数", json.dumps(plan, ensure_ascii=False) or "未录入", f"清单总产值 {boq_total:g}，2026 年度计划 {year_plan:g}，合同额不录",
             "项目整体完成百分比的分母只认清单总产值；合同留空")


# ----------------------------------------------------------------------------- 6 公司一数一源标准（工程管理数据，附件1.2）

PLATFORM = {  # 标准 No. → 取值（仿真项目的虚构值；合同类字段按 RJ 要求留空）
    "source": "仿真演示数据（按《中国港湾“一数一源”数据标准 第2部分 工程管理数据标准》2025征求意见稿字段填写，取值虚构）",
    "1_项目阶段": {"code": "02", "name": "活跃在建"},
    "2_中交项目主数据编码": "PJSIM0000001（仿真）",
    "3_项目名称（中文）": "仿真公路项目",
    "4_项目名称（外文）": "SIMULATED ROAD PROJECT (DEMO)",
    "5_项目分类": {"code": "02", "name": "B类"},
    "7_项目实施模式": {"code": "03", "name": "总承包"},
    "8_公司项目编码": "SIM-ROAD",
    "12_项目总/分属性": "总项目",
    "14_项目主要工作内容": "K0+000~K4+500 路基、软基换填、立交桥（桩基、承台、墩身、T梁预制与架设）、盖板涵与圆管涵、排水、路面及交通工程（仿真）",
    "19_项目类别（集团）": {"code": "09", "name": "路桥工程"},
    "20_建设性质": {"code": "01", "name": "新建项目"},
    "25_项目承接性质": {"code": "01", "name": "自行承接"},
    "26_项目承接角色": {"code": "01", "name": "总承包商"},
    "27_参与方式": {"code": "01", "name": "唯一主体"},
    "28_项目经理部名称": "仿真公路项目经理部",
    "29_项目经理": "陈立军",
    "30_预计开工日期": "2026-09-07",
    "31_预计交工日期": None,
    "32_实际开工日期": "2026-09-07",
    "33_实际交工日期": None,
    "34_竣工日期": None,
    "164_图文类型": {"code": "02", "name": "现场照片"},
    "212_会议层级": "项目经理部专题会议",
}


def standard(conn):
    pid = conn.execute("SELECT id FROM projects WHERE code='SIM-ROAD'").fetchone()[0]
    cfg = json.loads(conn.execute("SELECT config_json FROM projects WHERE id=?", (pid,)).fetchone()[0] or "{}")
    if cfg.get("platform") != PLATFORM:
        cfg["platform"] = PLATFORM
        conn.execute("UPDATE projects SET config_json=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), pid))
        note("项目主数据（标准 No.1-34）", "只有名称与自编编码", "按标准字段补项目阶段、分类、实施模式、项目类别、项目经理等（虚构值，合同/交工日期留空）",
             "工程管理数据标准 No.1-34；项目经理与系统项目经理账号同名，预计开工与 P6 开工日一致")
    for mid, title, d in conn.execute("SELECT id, title, meeting_date FROM meetings WHERE project_id=?", (pid,)).fetchall():
        new = f"安全生产会（周）{d}"
        if title != new:
            conn.execute("UPDATE meetings SET title=? WHERE id=?", (new, mid))
            note(f"例会名称 #{mid}", title, new, "工程管理数据标准 No.213 会议名称取标准清单“安全生产会（周）”，层级为项目经理部专题会议（No.212）")


# ----------------------------------------------------------------------------- 5 错字

def names(conn):
    """同一工种、同一机械只用一个名称（周报/月报的人机统计按名称汇总，名称不统一会拆成几行）；一条记录挂多个作业代码的取第一个。"""
    n_t = n_m = n_c = 0
    for lid, cj in conn.execute("SELECT id, content_json FROM logs WHERE content_json LIKE '%labor%' OR content_json LIKE '%activity_code%'").fetchall():
        d = json.loads(cj or "{}")
        ch = False
        for x in d.get("labor", []):
            if x.get("trade") in TRADE:
                x["trade"] = TRADE[x["trade"]]; n_t += 1; ch = True
        merged = {}
        for x in d.get("labor", []):
            k = x.get("trade")
            if k in merged and x.get("count") is not None and merged[k].get("count") is not None:
                merged[k]["count"] += x["count"]; ch = True
            else:
                merged[k] = x
        d["labor"] = list(merged.values()) if "labor" in d else d.get("labor")
        for x in d.get("machinery", []):
            if x.get("name") in MACH:
                x["name"] = MACH[x["name"]]; n_m += 1; ch = True
        merged = {}
        for x in d.get("machinery", []):
            k = x.get("name")
            if k in merged and x.get("count") is not None and merged[k].get("count") is not None:
                merged[k]["count"] += x["count"]; ch = True
            else:
                merged[k] = x
        if "machinery" in d:
            d["machinery"] = list(merged.values())
        for x in d.get("issues", []) + d.get("works", []):
            a = x.get("activity_code") or ""
            if any(sep in a for sep in (",", "，", "、")):
                x["activity_code"] = a.replace("，", ",").replace("、", ",").split(",")[0].strip(); n_c += 1; ch = True
        if ch:
            conn.execute("UPDATE logs SET content_json=? WHERE id=?", (json.dumps(d, ensure_ascii=False), lid))
    if n_t or n_m:
        note("日志人员工种、机械名称", "焊工/焊接工/电焊工、击手/机械操作手/机手；挖机/挖掘机、旋挖钻/旋挖钻机、罐车/商混罐车/混凝土搅拌车等混用",
             "电焊工、机手；挖掘机、旋挖钻机、混凝土罐车、装载机、自卸车", f"工种 {n_t} 处、机械 {n_m} 处；同一工种/机械一个名称，周月报人机统计不再拆行")
    if n_c:
        note("日志问题/工作的作业代码", "一条写多个代码（如 BR110,PR100）", "取第一个代码", f"{n_c} 处；否则文书里会出现“（不在当前计划）”的假作业")


def typos(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    tot = 0
    for t in tables:
        for _cid, col, typ, *_ in conn.execute(f'PRAGMA table_info("{t}")').fetchall():
            if typ and typ.upper() not in ("TEXT", ""):
                continue
            for a, b in TYPO:
                cur = conn.execute(f'UPDATE "{t}" SET "{col}"=replace("{col}", ?, ?) WHERE instr("{col}", ?) > 0', (a, b, a))
                tot += max(cur.rowcount, 0)
    if tot:
        note("语音转写错字", "半河站、母绑", "拌合站、模板", f"{tot} 处")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data_showcase"))
    a = ap.parse_args()
    data_dir = Path(a.data_dir).resolve()
    if data_dir == (ROOT / "data").resolve() or not (data_dir / "DEMO_COPY.json").exists():
        sys.exit("只能对演示副本运行")
    os.environ.update(ZB_DATA_DIR=str(data_dir), ZB_SCHEDULER="0", ZB_DEV_LOGIN="1", ZB_AI_PROVIDER="mock", ZB_ENV_FILE="NUL")
    sys.path.insert(0, str(ROOT))
    import time_machine
    trav = time_machine.travel(datetime.fromisoformat("2026-10-04T21:00:00").astimezone(), tick=True)
    trav.start()
    from app.db import connect, init_db
    init_db()
    from tools.showcase import author
    author.install()
    from tools.showcase.fill import Ctx
    c = Ctx("2026-10-04")
    weeks(c)
    import sqlite3
    from app.config import settings
    conn = sqlite3.connect(settings.db_path, isolation_level=None)
    appconn = connect()
    try:
        conn.execute("BEGIN")
        units_pct(conn)
        typos(conn)
        names(conn)
        conn.execute("COMMIT")
        conn.execute("BEGIN")
        output(conn, appconn)
        standard(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
        appconn.close()
    trav.stop()
    old = json.loads((data_dir / "fix_report.json").read_text("utf-8")) if (data_dir / "fix_report.json").exists() else []
    (data_dir / "fix_report.json").write_text(json.dumps(old + REPORT, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
