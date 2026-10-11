"""合成 P6 XER 样例（仅供自动测试与离线开发；不是任何真实项目数据）。

python tools/sample_xer.py 2026-10-01 out.xer
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

# code, name, wbs, start_offset(天), duration(天), preds, milestone
PLAN = [
    ("M0000", "开工", "W1", 0, 0, [], True),
    ("A1010", "3#墩桩基施工", "W11", 0, 10, ["M0000"], False),
    ("A1020", "3#墩承台钢筋绑扎", "W11", 10, 4, ["A1010"], False),
    ("A1030", "3#墩承台混凝土浇筑", "W11", 14, 2, ["A1020"], False),
    ("A1040", "3#墩墩身施工", "W11", 16, 12, ["A1030"], False),
    ("A2010", "4#墩桩基施工", "W12", 2, 12, ["M0000"], False),
    ("A2020", "4#墩承台施工", "W12", 14, 6, ["A2010"], False),
    ("A3010", "路基土方开挖", "W21", 0, 20, ["M0000"], False),
    ("A3020", "路基填筑", "W21", 12, 25, ["A3010"], False),
    ("A4010", "梁板预制", "W31", 5, 30, ["M0000"], False),
    ("A4020", "梁板架设", "W31", 35, 8, ["A4010", "A1040"], False),
    ("M9000", "主桥合龙节点", "W1", 43, 0, ["A4020", "A2020"], True),
]
WBS = [("W1", None, "示例大桥"), ("W11", "W1", "3#墩"), ("W12", "W1", "4#墩"), ("W21", "W1", "路基工程"), ("W31", "W1", "梁板工程")]


def make_xer(data_date: str, start: str = "2026-09-21", progress: dict[str, float] | None = None, shift: dict[str, int] | None = None) -> str:
    """progress: 各作业在数据日期时的完成%；shift: 作业预测完成推迟天数（模拟后续期次的延误）。"""
    progress = progress or {}
    shift = shift or {}
    s0 = date.fromisoformat(start)
    dd = date.fromisoformat(data_date)
    rows = []
    proj_end = s0 + timedelta(days=43)
    for code, name, wbs, off, dur, preds, ms in PLAN:
        es = s0 + timedelta(days=off)
        ef = es + timedelta(days=dur)
        delay = shift.get(code, 0)
        pct = progress.get(code, 0.0)
        act_start = es if pct > 0 else None
        act_end = ef + timedelta(days=delay) if pct >= 100 else None
        remain = round(dur * (100 - pct) / 100)
        if 0 < pct < 100:
            early_s, early_f = dd, dd + timedelta(days=remain + delay)
        else:
            early_s, early_f = es + timedelta(days=delay if pct == 0 else 0), ef + timedelta(days=delay)
        tf = (proj_end - ef).days if code not in ("A1010", "A1020", "A1030", "A1040", "A4010", "A4020", "M9000", "M0000") else 0
        if code in ("A2010", "A2020"):
            tf = 9
        if code in ("A3010", "A3020"):
            tf = 20
        rows.append({
            "task_id": str(100 + len(rows)), "proj_id": "1", "wbs_id": wbs, "clndr_id": "1",
            "phys_complete_pct": f"{pct:g}", "task_code": code, "task_name": name,
            "task_type": "TT_Mile" if ms else "TT_Task",
            "status_code": "TK_Complete" if pct >= 100 else ("TK_Active" if pct > 0 else "TK_NotStart"),
            "total_float_hr_cnt": str((tf - delay) * 8), "remain_drtn_hr_cnt": str(remain * 8),
            "target_drtn_hr_cnt": str(dur * 8),
            "act_start_date": f"{act_start} 08:00" if act_start else "", "act_end_date": f"{act_end} 17:00" if act_end else "",
            "early_start_date": f"{early_s} 08:00", "early_end_date": f"{early_f} 17:00",
            "late_start_date": "", "late_end_date": "",
            "target_start_date": f"{es} 08:00", "target_end_date": f"{ef} 17:00",
            "driving_path_flag": "Y" if tf - delay <= 0 else "N",
        })
    code_to_id = {r["task_code"]: r["task_id"] for r in rows}
    preds = [(code_to_id[x[0]], code_to_id[p]) for x in PLAN for p in x[5]]
    out = ["ERMHDR\t19.12\t2026-10-01\tProject\tadmin\tadmin\tdbx\tProject Management\tUSD"]

    def table(name, fields, records):
        out.append(f"%T\t{name}")
        out.append("%F\t" + "\t".join(fields))
        for r in records:
            out.append("%R\t" + "\t".join(str(r.get(f, "")) for f in fields))

    table("PROJECT", ["proj_id", "proj_short_name", "last_recalc_date"], [{"proj_id": "1", "proj_short_name": "SAMPLE-BRIDGE", "last_recalc_date": f"{dd} 08:00"}])
    table("CALENDAR", ["clndr_id", "clndr_name", "day_hr_cnt"], [{"clndr_id": "1", "clndr_name": "7天工作制", "day_hr_cnt": "8"}])
    table("PROJWBS", ["wbs_id", "proj_id", "parent_wbs_id", "wbs_short_name", "wbs_name", "proj_node_flag"],
          [{"wbs_id": "W0", "proj_id": "1", "parent_wbs_id": "", "wbs_short_name": "SAMPLE", "wbs_name": "SAMPLE", "proj_node_flag": "Y"}] +
          [{"wbs_id": w, "proj_id": "1", "parent_wbs_id": p or "W0", "wbs_short_name": w, "wbs_name": n, "proj_node_flag": "N"} for w, p, n in WBS])
    table("TASK", list(rows[0].keys()), rows)
    table("TASKPRED", ["task_pred_id", "task_id", "pred_task_id", "pred_type", "lag_hr_cnt"],
          [{"task_pred_id": str(i), "task_id": t, "pred_task_id": p, "pred_type": "PR_FS", "lag_hr_cnt": "0"} for i, (t, p) in enumerate(preds)])
    out.append("%E")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    dd = sys.argv[1] if len(sys.argv) > 1 else "2026-10-01"
    path = sys.argv[2] if len(sys.argv) > 2 else "sample.xer"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(make_xer(dd, progress={"M0000": 100, "A1010": 80, "A2010": 60, "A3010": 50}))
    print(path)
