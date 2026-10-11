"""计划深挖：基于 P6 XER 的计划体检、期间对比、计划工效对实测工效、控制节点倒计时、雨季提示。

全部是确定性计算（不调用 AI）；只有体检的“诊断与建议”由计划体检智能体写（plan_health_agent），数字与作业以本模块结果为准。
口径：
- 工作日按作业自己的 P6 日历计（p6cal）；没有扩展数据（旧导入、Excel）的批次如实提示“需重新导入 XER”，不猜；
- 实测工效＝两期更新之间完成%的增量 × 作业数量（UDF）/ 期间内该作业日历的工作日；单位与计划工效对不上的不比较；
- 控制节点＝带“不晚于/完成于”类约束日期的里程碑（P6 约束日期，不等同于合同日期）；
- 不涉及产值、费用。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from ..db import all_, one
from . import p6, p6cal, p6ext

# DCMA 14 点检查里常用的阈值（工作日）
LONG_DUR_D = 44
HIGH_FLOAT_D = 44
RAIN_WINDOW_D = 60         # 雨季提示看未来多少天
RAIN_LOST_SHARE = 0.25     # 某月损失工作日占比超过此值算雨季月
PROD_SLOW = 1.15           # 与 schedule.SLOW_FACTOR 一致：计划工效/实测工效 > 1.15 算偏慢


def _imports(conn, project_id: int) -> list[dict]:
    return all_(conn, "SELECT * FROM p6_imports WHERE project_id=? ORDER BY data_date, id", (project_id,))


def current_and_previous(conn, project_id: int) -> tuple[dict | None, dict | None]:
    cur = p6.current_import(conn, project_id)
    if not cur:
        return None, None
    prev = one(conn, "SELECT * FROM p6_imports WHERE project_id=? AND id<>? AND data_date<? ORDER BY data_date DESC, id DESC LIMIT 1",
               (project_id, cur["id"], cur["data_date"]))
    return cur, prev


def _acts(conn, import_id: int) -> dict[str, dict]:
    return {a["code"]: a for a in all_(conn, "SELECT * FROM activities WHERE import_id=?", (import_id,))}


def _cal_for(conn, import_id: int, origin: date) -> tuple[dict, dict, p6cal.Calendar | None]:
    rows = p6ext.calendars(conn, import_id)
    cals = p6cal.build_calendars(rows, origin)
    default = next((cals.get(str(r["clndr_id"])) for r in rows if r["is_default"]), None) or (next(iter(cals.values())) if cals else None)
    return cals, {str(r["clndr_id"]): r for r in rows}, default


def _sample(items: list[dict], n: int = 8) -> list[dict]:
    return [{"code": a["code"], "name": a.get("name", ""), **({"note": a["note"]} if a.get("note") else {})} for a in items[:n]]


def _check(key: str, title: str, items: list[dict], total: int, *, warn: float, bad: float, ratio: bool = True,
           rule: str = "", why: str = "") -> dict:
    n = len(items)
    share = n / total if total else 0.0
    v = share if ratio else n
    level = "bad" if v > bad else ("warn" if v > warn else "ok")
    return {"key": key, "title": title, "count": n, "total": total, "share": round(share * 100, 1), "level": level,
            "rule": rule, "why": why, "sample": _sample(items)}


# --------------------------------------------------------------------------- 体检

def health(conn, project_id: int) -> dict:
    imp = p6.current_import(conn, project_id)
    if not imp:
        return {"none": True}
    acts = _acts(conn, imp["id"])
    rels = all_(conn, "SELECT pred_code, succ_code, kind, lag_d FROM relations WHERE import_id=?", (imp["id"],))
    ext = p6ext.ext_map(conn, imp["id"])
    dd = imp["data_date"]
    tasks = [a for a in acts.values() if not a["is_milestone"]]
    has_pred, has_succ = defaultdict(int), defaultdict(int)
    for r in rels:
        has_pred[r["succ_code"]] += 1
        has_succ[r["pred_code"]] += 1
    open_ = [a for a in acts.values() if not a["act_finish"]]
    starts = {c for c, a in acts.items() if a["is_milestone"] and a["task_type"] == "TT_Mile"}
    finishes = {c for c, a in acts.items() if a["task_type"] == "TT_FinMile"}
    no_pred = [a for a in open_ if not has_pred[a["code"]] and a["code"] not in starts]
    no_succ = [a for a in open_ if not has_succ[a["code"]] and a["code"] not in finishes]
    checks = [
        _check("open_start", "缺紧前作业", no_pred, len(open_), warn=0.02, bad=0.05,
               rule="未完成的作业（开始里程碑除外）应有紧前作业；阈值 5%",
               why="没有紧前作业，开工日只由数据日期或约束决定，前面工作拖期也传不过来"),
        _check("open_finish", "缺紧后作业", no_succ, len(open_), warn=0.02, bad=0.05,
               rule="未完成的作业（完成里程碑除外）应有紧后作业；阈值 5%",
               why="没有紧后作业，它拖期不会推迟任何节点，关键路径会被漏掉"),
    ]
    hard = []
    for c, e in ext.items():
        if c in acts and not acts[c]["act_finish"]:
            for t, d in ((e.get("cstr_type"), e.get("cstr_date")), (e.get("cstr_type2"), e.get("cstr_date2"))):
                if t and p6ext.CSTR.get(t, ("", False))[1]:
                    hard.append({**acts[c], "note": f"{p6ext.CSTR[t][0]} {d or ''}".strip()})
    checks.append(_check("hard_cstr", "硬约束", hard, len(open_), warn=0.0, bad=0.05,
                         rule="开始于/完成于/强制类约束会覆盖逻辑关系；阈值 5%",
                         why="硬约束让日期不随前序变化，排程显示“按期”可能是约束钉住的，不是真能按期"))
    neg = sorted([a for a in open_ if a["total_float_d"] is not None and a["total_float_d"] < 0], key=lambda a: a["total_float_d"])
    for a in neg:
        a["note"] = f"总浮时 {round(a['total_float_d'], 1):g} 天"
    checks.append(_check("neg_float", "负总浮时", neg, len(open_), warn=0.0, bad=0.0, ratio=False,
                         rule="出现负浮时即说明按当前逻辑已赶不上约束日期",
                         why="负浮时的作业按当前逻辑已经晚于约束日期，需要调整逻辑或采取措施"))
    high = sorted([a for a in open_ if not a["is_milestone"] and a["total_float_d"] is not None and a["total_float_d"] > HIGH_FLOAT_D],
                  key=lambda a: -a["total_float_d"])
    for a in high:
        a["note"] = f"总浮时 {round(a['total_float_d'], 1):g} 天"
    checks.append(_check("high_float", f"总浮时超过 {HIGH_FLOAT_D} 个工作日", high, len([a for a in open_ if not a["is_milestone"]]),
                         warn=0.05, bad=0.25, rule=f"阈值：超过 {HIGH_FLOAT_D} 个工作日的作业占比 5%",
                         why="浮时过大通常是缺紧后逻辑或逻辑挂错，作业拖了也不报警"))
    long_ = sorted([a for a in tasks if not a["act_finish"] and (a["orig_dur_d"] or 0) > LONG_DUR_D], key=lambda a: -(a["orig_dur_d"] or 0))
    for a in long_:
        a["note"] = f"原定工期 {round(a['orig_dur_d'], 1):g} 天"
    checks.append(_check("long_dur", f"原定工期超过 {LONG_DUR_D} 个工作日", long_, len([a for a in tasks if not a["act_finish"]]),
                         warn=0.05, bad=0.15, rule=f"阈值：工期超过 {LONG_DUR_D} 个工作日的作业占比 5%",
                         why="工期太长的作业难以按周跟踪，宜按部位或工序拆分"))
    lag_rels = [r for r in rels if (r["lag_d"] or 0) > 0]
    lead_rels = [r for r in rels if (r["lag_d"] or 0) < 0]
    non_fs = [r for r in rels if r["kind"] != "FS"]

    def rel_items(rs):
        out = []
        for r in rs:
            lag = f" 滞后 {r['lag_d']:g} 天" if r["lag_d"] else ""
            out.append({"code": r["succ_code"], "name": (acts.get(r["succ_code"]) or {}).get("name", ""),
                        "note": f"{r['pred_code']} → {r['succ_code']} {r['kind']}{lag}"})
        return out
    checks.append(_check("lags", "带滞后的逻辑关系", rel_items(lag_rels), len(rels), warn=0.05, bad=0.10,
                         rule="阈值：带正滞后的关系占比 5%", why="滞后是“看不见的作业”，养护、等待等宜单列作业"))
    checks.append(_check("leads", "负滞后（提前量）", rel_items(lead_rels), len(rels), warn=0.0, bad=0.0, ratio=False,
                         rule="不应使用负滞后", why="负滞后让后续作业在前序完成前开始，逻辑难以核查"))
    checks.append(_check("non_fs", "非 FS 关系", rel_items(non_fs), len(rels), warn=0.10, bad=0.20,
                         rule="阈值：SS/FF/SF 关系占比 10%", why="FS 以外的关系多了，关键路径不易看清"))
    # 进度数据：实际日期晚于数据日期、逻辑倒置（后续已开工、前序未完成且为 FS）
    bad_act = [{**a, "note": f"实际{'完成' if (a['act_finish'] or '') > dd else '开始'} 晚于数据日期 {dd}"} for a in acts.values()
               if (a["act_start"] and a["act_start"] > dd) or (a["act_finish"] and a["act_finish"] > dd)]
    checks.append(_check("bad_actual", "实际日期晚于数据日期", bad_act, len(acts), warn=0.0, bad=0.0, ratio=False,
                         rule="实际开始/完成不能晚于数据日期", why="实际日期在未来，说明更新时填错了"))
    oos = []
    for r in rels:
        p, s = acts.get(r["pred_code"]), acts.get(r["succ_code"])
        if r["kind"] == "FS" and p and s and s["act_start"] and not p["act_finish"]:
            oos.append({**s, "note": f"紧前 {p['code']} 未完成"})
    checks.append(_check("out_of_seq", "逻辑倒置（紧前未完成已开工）", oos, max(1, sum(1 for a in acts.values() if a["act_start"])),
                      warn=0.0, bad=0.10, rule="FS 紧前未完成，紧后已开工", why="现场实际顺序与计划逻辑不一致，计划需要按实际修正逻辑"))
    # UDF：计划工效与数量单位、工期是否一致
    unit_bad, prod_bad = [], []
    for c, e in ext.items():
        a = acts.get(c)
        if not a or a["is_milestone"] or not e.get("prod_text"):
            continue
        q, qu = p6ext.quantity_of(e)
        r, ru = p6ext.parse_prod(e.get("prod_text"))
        if q is None or not r:
            continue
        q2, ok = _conv(q, qu, ru)
        if not ok:
            unit_bad.append({**a, "note": f"数量 {e.get('qty_text')}（单位 {e.get('unit') or '未填'}），计划工效 {e.get('prod_text')}"})
            continue
        dur = a["orig_dur_d"] or 0
        if dur > 0:
            need = q2 / r
            if need > dur * 1.5 or need < dur / 1.5:
                prod_bad.append({**a, "note": f"数量 {e.get('qty_text')} ÷ 工效 {e.get('prod_text')} ≈ {need:.0f} 天，原定工期 {dur:g} 天"})
    with_prod = sum(1 for c, e in ext.items() if e.get("prod_text") and c in acts and not acts[c]["is_milestone"])
    checks.append(_check("udf_unit", "工效与数量单位不一致", unit_bad, with_prod, warn=0.0, bad=0.10,
                         rule="计划工效（UDF）的单位应与作业数量单位一致", why="单位不一致时无法用工效核算工期，也无法与现场实测工效对比"))
    checks.append(_check("udf_dur", "工效推算工期与原定工期相差 1.5 倍以上", prod_bad, with_prod, warn=0.10, bad=0.25,
                         rule="数量 ÷ 计划工效 应接近原定工期", why="两者对不上，说明工期或工效有一个没按实际资源编"))
    order = {"bad": 0, "warn": 1, "ok": 2}
    checks.sort(key=lambda c: order[c["level"]])
    return {"import_id": imp["id"], "data_date": dd, "file": imp["file_name"], "activities": len(acts), "relations": len(rels),
            "has_ext": bool(ext), "checks": checks,
            "summary": {k: sum(1 for c in checks if c["level"] == k) for k in ("bad", "warn", "ok")},
            "method": "参照 DCMA 14 点计划质量检查，阈值按工作日；结果只读 P6 导入数据，不改动 P6 计划"}


def _conv(q: float, qu: str, ru: str) -> tuple[float, bool]:
    """数量换算到工效单位；km→m；单位都缺时视为一致。"""
    if not qu or not ru or qu == ru:
        return q, True
    if qu == "km" and ru == "m":
        return q * 1000, True
    if qu == "m" and ru == "km":
        return q / 1000, True
    return q, False


# --------------------------------------------------------------------------- 期间对比

def compare(conn, project_id: int) -> dict:
    cur, prev = current_and_previous(conn, project_id)
    if not cur:
        return {"none": True}
    if not prev:
        return {"none": True, "reason": "只有一期 P6 计划，导入下一期更新后可对比", "current": _imp_brief(cur)}
    a, b = _acts(conn, prev["id"]), _acts(conn, cur["id"])
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    started, finished, slipped, gained, float_drop = [], [], [], [], []
    for c in sorted(set(a) & set(b)):
        x, y = a[c], b[c]
        if y["act_start"] and not x["act_start"]:
            started.append({"code": c, "name": y["name"], "date": y["act_start"]})
        if y["act_finish"] and not x["act_finish"]:
            finished.append({"code": c, "name": y["name"], "date": y["act_finish"]})
        fx, fy = x["act_finish"] or x["early_finish"], y["act_finish"] or y["early_finish"]
        if fx and fy and not y["is_milestone"]:
            d = (date.fromisoformat(fy) - date.fromisoformat(fx)).days
            row = {"code": c, "name": y["name"], "before": fx, "after": fy, "days": d, "critical": bool(y["is_critical"])}
            if d > 0:
                slipped.append(row)
            elif d < 0:
                gained.append(row)
        tx, ty = x["total_float_d"], y["total_float_d"]
        if tx is not None and ty is not None and ty < tx and not y["act_finish"]:
            float_drop.append({"code": c, "name": y["name"], "before": tx, "after": ty, "days": round(ty - tx, 1)})
    ms = []
    for c in sorted(set(a) & set(b)):
        x, y = a[c], b[c]
        if not y["is_milestone"]:
            continue
        fx = x["act_finish"] or x["early_finish"] or x["act_start"] or x["early_start"]
        fy = y["act_finish"] or y["early_finish"] or y["act_start"] or y["early_start"]
        if fx and fy:
            d = (date.fromisoformat(fy) - date.fromisoformat(fx)).days
            ms.append({"code": c, "name": y["name"], "before": fx, "after": fy, "days": d, "done": bool(y["act_finish"])})
    ms.sort(key=lambda m: (-abs(m["days"]), m["after"]))

    def finish(acts):
        ds = [x["act_finish"] or x["early_finish"] for x in acts.values() if x["act_finish"] or x["early_finish"]]
        return max(ds) if ds else None
    pf_a, pf_b = finish(a), finish(b)
    crit_a = {c for c, x in a.items() if x["is_critical"] and not x["act_finish"]}
    crit_b = {c for c, x in b.items() if x["is_critical"] and not x["act_finish"]}
    return {
        "previous": _imp_brief(prev), "current": _imp_brief(cur),
        "project_finish": {"before": pf_a, "after": pf_b,
                           "days": (date.fromisoformat(pf_b) - date.fromisoformat(pf_a)).days if pf_a and pf_b else None},
        "counts": {"started": len(started), "finished": len(finished), "slipped": len(slipped), "gained": len(gained),
                   "added": len(added), "removed": len(removed), "float_drop": len(float_drop),
                   "critical_before": len(crit_a), "critical_after": len(crit_b),
                   "critical_new": len(crit_b - crit_a), "relations_before": prev["relation_count"], "relations_after": cur["relation_count"]},
        "milestones": ms,
        "slipped": sorted(slipped, key=lambda r: (-r["days"], r["code"]))[:20],
        "gained": sorted(gained, key=lambda r: (r["days"], r["code"]))[:10],
        "started": started[:20], "finished": finished[:20],
        "float_drop": sorted(float_drop, key=lambda r: r["days"])[:15],
        "critical_new": [{"code": c, "name": b[c]["name"]} for c in sorted(crit_b - crit_a)][:15],
        "added": [{"code": c, "name": b[c]["name"]} for c in added[:15]],
        "removed": [{"code": c, "name": a[c]["name"]} for c in removed[:15]],
        "note": "比较的是 P6 两期更新各自的计划/实际日期（完成日取实际完成，否则取最早完成）；不是系统预测",
    }


def _imp_brief(imp: dict) -> dict:
    return {"id": imp["id"], "file": imp["file_name"], "data_date": imp["data_date"], "activities": imp["activity_count"]}


# --------------------------------------------------------------------------- 计划工效 vs 实测工效

def productivity(conn, project_id: int) -> dict:
    cur, prev = current_and_previous(conn, project_id)
    if not cur:
        return {"none": True}
    ext = p6ext.ext_map(conn, cur["id"])
    if not ext:
        return {"none": True, "reason": "当前批次没有作业数量与计划工效（需用 XER 重新导入）"}
    if not prev:
        return {"none": True, "reason": "只有一期 P6 计划，实测工效要两期更新之间的完成量，导入下一期后可算"}
    d0, d1 = date.fromisoformat(prev["data_date"]), date.fromisoformat(cur["data_date"])
    cals, _, default = _cal_for(conn, cur["id"], d0 - timedelta(days=30))
    a, b = _acts(conn, prev["id"]), _acts(conn, cur["id"])
    rows, skipped = [], {"no_prod": 0, "unit": 0, "no_progress": 0}
    for c, y in b.items():
        e = ext.get(c) or {}
        x = a.get(c)
        if y["is_milestone"] or not x:
            continue
        q, qu = p6ext.quantity_of(e)
        r, ru = p6ext.parse_prod(e.get("prod_text"))
        if q is None or not r:
            skipped["no_prod"] += 1
            continue
        dp = (y["phys_pct"] or 0) - (x["phys_pct"] or 0)
        if dp <= 0:
            if y["act_start"] and not x["act_finish"]:
                skipped["no_progress"] += 1
            continue
        q2, ok = _conv(q, qu, ru)
        if not ok:
            skipped["unit"] += 1
            continue
        cal = cals.get(str(e.get("clndr_id") or "")) or default
        # 数据日期按当天收工计：期间＝上期数据日期次日 ～ 本期数据日期；期间内开工/完工的从开工日算到完工日
        s = d0 + timedelta(days=1)
        if y["act_start"] and date.fromisoformat(y["act_start"]) > d0:
            s = date.fromisoformat(y["act_start"])
        f = min(d1, date.fromisoformat(y["act_finish"])) if y["act_finish"] else d1
        if cal:
            o = cal.origin
            wd = cal.work_between((s - o).days, (f - o).days + 1)
        else:
            wd = float((f - s).days + 1)
        if wd < 1:
            continue
        done = q2 * dp / 100.0
        rate = done / wd
        factor = round(r / rate, 2) if rate > 0 else None
        rows.append({"code": c, "name": y["name"], "wbs_path": y["wbs_path"], "critical": bool(y["is_critical"]),
                     "plan_rate": r, "unit": ru or qu, "actual_rate": round(rate, 1), "factor": factor,
                     "workdays": round(wd, 1), "qty_done": round(done, 1), "pct_before": x["phys_pct"], "pct_after": y["phys_pct"],
                     "plan_text": e.get("prod_text"), "qty_text": e.get("qty_text"),
                     "level": "slow" if factor and factor > PROD_SLOW else ("fast" if factor and factor < 1 / PROD_SLOW else "ok")})
    rows.sort(key=lambda r: (-(r["factor"] or 0), r["code"]))
    return {"previous": _imp_brief(prev), "current": _imp_brief(cur), "rows": rows, "skipped": skipped,
            "slow": sum(1 for r in rows if r["level"] == "slow"), "threshold": PROD_SLOW,
            "method": "实测工效＝两期之间 P6 完成% 增量 × 作业数量（UDF）÷ 期间该作业日历的工作日；"
                      "计划工效取 P6 的工效 UDF；系数＝计划/实测，>1.15 为慢于计划"}


# --------------------------------------------------------------------------- 控制节点倒计时

def countdown(conn, project_id: int, today: date | None = None, forecast: dict | None = None) -> dict:
    imp = p6.current_import(conn, project_id)
    if not imp:
        return {"none": True}
    ext = p6ext.ext_map(conn, imp["id"])
    if not ext:
        return {"none": True, "reason": "当前批次没有约束日期（需用 XER 重新导入）"}
    today = today or date.today()
    acts = _acts(conn, imp["id"])
    fc = {m["code"]: m for m in (forecast or {}).get("milestones") or []}
    rows = []
    for c, a in acts.items():
        e = ext.get(c) or {}
        if not a["is_milestone"]:
            continue
        cst = next(((t, d) for t, d in ((e.get("cstr_type2"), e.get("cstr_date2")), (e.get("cstr_type"), e.get("cstr_date")))
                    if t in p6ext.DEADLINE_TYPES and d), None)
        if not cst:
            continue
        t, d = cst
        dl = date.fromisoformat(d)
        m = fc.get(c) or {}
        f = m.get("forecast")
        rows.append({"code": c, "name": a["name"], "type": p6ext.CSTR.get(t, (t,))[0], "deadline": d,
                     "days_left": (dl - today).days, "done": bool(a["act_finish"]), "act_finish": a["act_finish"],
                     "p6_date": a["early_finish"] or a["early_start"], "total_float_d": a["total_float_d"],
                     "forecast": f, "margin_days": (dl - date.fromisoformat(f)).days if f else None,
                     "p80": m.get("p80"), "margin_p80": (dl - date.fromisoformat(m["p80"])).days if m.get("p80") else None,
                     "on_time_prob": m.get("on_time_prob"), "prob_label": m.get("prob_label")})
    rows.sort(key=lambda r: (r["done"], r["deadline"]))
    return {"today": today.isoformat(), "rows": rows, "data_date": imp["data_date"],
            "note": "节点日期取 P6 里程碑的约束日期（完成不晚于/完成于等），是计划控制日期，不等同于合同工期；余量＝约束日期 − 系统预测完成日"}


# --------------------------------------------------------------------------- 雨季提示

def rain(conn, project_id: int, today: date | None = None, window: int = RAIN_WINDOW_D) -> dict:
    """P6 日历里的雨季：按月统计各日历的损失工作日（损失占比 > RAIN_LOST_SHARE 的月份为雨季月）；
    未来 window 天内计划施工、因所挂日历的例外日（雨季、节假日）少掉至少 1 个工作日的作业。"""
    imp = p6.current_import(conn, project_id)
    if not imp:
        return {"none": True}
    rows = p6ext.calendars(conn, imp["id"])
    if not rows:
        return {"none": True, "reason": "当前批次没有 P6 日历（需用 XER 重新导入）"}
    today = today or date.today()
    origin = date(today.year - 1, 1, 1)
    cals = p6cal.build_calendars(rows, origin)
    ext = p6ext.ext_map(conn, imp["id"])
    use = defaultdict(int)
    for e in ext.values():
        use[str(e.get("clndr_id") or "")] += 1
    # 雨季月：该日历比“同一周工作制、无例外”的情况少掉的工作日占当月 > RAIN_LOST_SHARE
    profiles = []
    for r in rows:
        cid = str(r["clndr_id"])
        cal = cals.get(cid)
        if not cal or not use.get(cid):
            continue
        week = r["spec"]["week"]
        months = []
        d = date(today.year, today.month, 1)
        for _ in range(18):
            nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            base = sum(week[(d + timedelta(days=i)).weekday()] / (r["day_hr"] or 8) for i in range((nxt - d).days))
            got = cal.work_between((d - origin).days, (nxt - origin).days)
            lost = max(0.0, base - got)
            months.append({"month": d.strftime("%Y-%m"), "workdays": round(got, 1), "lost": round(lost, 1),
                           "share": round(lost / base * 100, 1) if base else 0.0})
            d = nxt
        rainy = [m for m in months if m["share"] > RAIN_LOST_SHARE * 100]
        profiles.append({"clndr_id": cid, "name": r["name"], "activities": use[cid], "months": months,
                         "rainy_months": [m["month"] for m in rainy], "is_rain": bool(rainy)})
    end = today + timedelta(days=window)
    acts = _acts(conn, imp["id"])
    hit = []
    for c, a in acts.items():
        e = ext.get(c) or {}
        cid = str(e.get("clndr_id") or "")
        if cid not in cals or a["act_finish"] or a["is_milestone"]:
            continue
        s = date.fromisoformat(a["act_start"]) if a["act_start"] else (date.fromisoformat(a["early_start"]) if a["early_start"] else None)
        f = date.fromisoformat(a["early_finish"]) if a["early_finish"] else None
        if not s or not f or f < today or s > end:
            continue
        lo, hi = max(s, today), min(f, end)
        cal = cals[cid]
        span = (hi - lo).days + 1
        got = cal.work_between((lo - origin).days, (hi - origin).days + 1)
        week = next(r["spec"]["week"] for r in rows if str(r["clndr_id"]) == cid)
        base = sum(week[(lo + timedelta(days=i)).weekday()] / (cal.day_hr or 8) for i in range(span))
        lost = base - got   # 只算例外日少掉的工作日，周休不算
        if lost >= 1:
            hit.append({"code": c, "name": a["name"], "start": s.isoformat(), "finish": f.isoformat(), "critical": bool(a["is_critical"]),
                        "total_float_d": a["total_float_d"], "lost_days": round(lost, 1), "calendar": cal.name})
    hit.sort(key=lambda h: (not h["critical"], -h["lost_days"], h["start"]))
    return {"today": today.isoformat(), "window_days": window, "calendars": profiles, "activities": hit[:30], "activity_count": len(hit),
            "note": "雨季与节假日取自 P6 日历的例外日（不工作或减时），是计划编制时的假设，不是天气预报"}
