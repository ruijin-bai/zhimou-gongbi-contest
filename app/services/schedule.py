"""进度Agent的算法层：基于现场实绩的 CPM 重排预测 + 蒙特卡洛里程碑概率 + 赶工方案复算。

- 工期：已完成=0；进行中=按日志观测工效推算剩余工期（观测不足时用 P6 尚需工期）；未开始=P6 原定工期；
- 逻辑：FS/SS/FF/SF + 滞后，且不早于 P6 当前最早开始（保留 P6 中的约束与资源平衡结果）；
- 时间单位：P6 工期为工作日。导入时存了 P6 日历（p6ext）的，按每项作业自己的日历逐日排程（周休、节假日、雨季等例外日不计工）；
  没有日历数据的（旧导入、Excel）按项目每周工作天数换算为日历天（近似，见 calendar_note）；
- 不确定性：每项未完工作的工期乘以三角分布系数，进行中作业以观测工效为众数；
- 赶工方案：由 AI 提出“哪个作业、工期缩短多少、靠什么措施”，再由本模块复算里程碑，展示真实效果。
"""
from __future__ import annotations

import math
import random
from collections import defaultdict, deque
from datetime import date, timedelta

from ..db import all_, dumps, loads, now_iso, one, insert
from . import p6, p6cal, p6ext
from .logs import progress_state

SLOW_FACTOR = 1.15  # 工效系数（计划工效/实测工效）超过此值才算“工效偏低”：诊断表与赶工方案共用同一阈值
CAL_SHIFT = 2000    # 日历第 0 天＝as_of 前 2000 天；排程时间都是距 as_of 的日历天


def _d(s):
    return date.fromisoformat(s) if s else None


def _calendars(conn, imp: dict, acts: dict, as_of: date) -> tuple[dict, list[str]]:
    """{作业代码: Calendar}；没有扩展数据或日历都解析不了时返回 ({}, [])，调用方退回旧口径。"""
    rows = p6ext.calendars(conn, imp["id"])
    cals = p6cal.build_calendars(rows, as_of - timedelta(days=CAL_SHIFT))
    if not cals:
        return {}, []
    ext = p6ext.ext_map(conn, imp["id"])
    default = next((cals[str(r["clndr_id"])] for r in rows if r["is_default"] and str(r["clndr_id"]) in cals), None) or next(iter(cals.values()))
    by_code = {c: cals.get(str((ext.get(c) or {}).get("clndr_id") or ""), default) for c in acts}
    names = sorted({c.name for c in by_code.values()})
    return by_code, names


def _add(net: dict, code: str, t: float, d: float) -> float:
    c = net["cals"].get(code)
    return t + d if c is None else c.add(t + CAL_SHIFT, d) - CAL_SHIFT


def _sub(net: dict, code: str, t: float, d: float) -> float:
    c = net["cals"].get(code)
    return t - d if c is None else c.sub(t + CAL_SHIFT, d) - CAL_SHIFT


def build_network(conn, project_id: int, as_of: date, use_calendar: bool = True) -> dict | None:
    imp = p6.current_import(conn, project_id)
    if not imp:
        return None
    proj = one(conn, "SELECT config_json FROM projects WHERE id=?", (project_id,))
    wpw = float((loads(proj["config_json"], {}) or {}).get("workdays_per_week") or 7)
    acts = {a["code"]: a for a in p6.current_activities(conn, project_id)}
    cals, cal_names = _calendars(conn, imp, acts, as_of) if use_calendar else ({}, [])
    cal = 1.0 if cals else 7.0 / max(1.0, min(7.0, wpw))   # 有 P6 日历时工期保持工作日，由日历换算日期
    rels = all_(conn, "SELECT pred_code, succ_code, kind, lag_d FROM relations WHERE import_id=?", (imp["id"],))
    state = progress_state(conn, project_id, imp, before=(as_of + timedelta(days=1)).isoformat())
    dd = _d(imp["data_date"])
    nodes = {}
    for code, a in acts.items():
        st = state.get(code) or {}
        pct = st.get("pct") if st.get("pct") is not None else (a["phys_pct"] or 0.0)
        done = bool(a["act_finish"]) or pct >= 100
        started = done or bool(a["act_start"]) or pct > 0
        orig = (a["orig_dur_d"] or 0.0) * cal
        remain = (a["remain_dur_d"] if a["remain_dur_d"] is not None else (a["orig_dur_d"] or 0.0) * (1 - pct / 100)) * cal
        factor, basis = 1.0, "P6尚需工期"
        if started and not done:
            p0 = a["phys_pct"] or 0.0
            first = _d(a["act_start"]) or _d(st.get("first_date"))
            last = _d(st.get("pct_date"))
            if st.get("pct") is not None and pct > p0:
                remain = max(1.0, orig * (1 - pct / 100)) if orig else remain  # 以日志最新累计%重算尚需工期
            if st.get("pct") is not None and last and first and pct > p0 and st.get("reports", 0) >= 3:
                elapsed = max(1, (last - max(dd, first)).days + 1)
                if cals.get(code) and elapsed >= 3:   # 有日历时实测工效按“经过的工作日”算
                    a0 = (max(dd, first) - as_of).days + CAL_SHIFT
                    elapsed = max(1.0, cals[code].work_between(a0, a0 + elapsed))
                if elapsed >= 3:
                    rate = (pct - p0) / elapsed                    # 实测 %/天（自实际开工起）
                    planned_rate = 100.0 / max(1.0, orig) if orig else None
                    if rate > 0:
                        remain = min((100 - pct) / rate, max(1.0, orig * (1 - pct / 100)) * 3)
                        basis = f"日志实测工效 {rate:.1f}%/天"
                        if planned_rate:
                            factor = round(planned_rate / rate, 2)
            elif pct > 0 and remain <= 0:
                remain = max(1.0, orig * (1 - pct / 100))
        nodes[code] = {
            "code": code, "name": a["name"], "milestone": bool(a["is_milestone"]), "critical_p6": bool(a["is_critical"]),
            "fin_mile": a["task_type"] == "TT_FinMile",
            "done": done, "started": started, "act_start": _d(a["act_start"]), "act_finish": _d(a["act_finish"]),
            "p6_es": _d(a["early_start"]), "p6_ef": _d(a["early_finish"]), "dur": 0.0 if done else (remain if started else orig),
            "orig": orig, "pct": pct, "basis": basis if started and not done else ("已完成" if done else "P6原定工期"),
            "productivity_factor": factor,
        }
    return {"import": imp, "nodes": nodes, "rels": rels, "as_of": as_of, "cal_factor": cal, "wpw": wpw, "cals": cals, "cal_names": cal_names}


def _topo(nodes: dict, rels: list[dict]) -> tuple[list[str], dict, dict]:
    preds, succs, indeg = defaultdict(list), defaultdict(list), {c: 0 for c in nodes}
    for r in rels:
        if r["pred_code"] in nodes and r["succ_code"] in nodes:
            preds[r["succ_code"]].append(r)
            succs[r["pred_code"]].append(r)
            indeg[r["succ_code"]] += 1
    q = deque(sorted(c for c, n in indeg.items() if n == 0))
    order = []
    while q:
        c = q.popleft()
        order.append(c)
        for r in succs[c]:
            indeg[r["succ_code"]] -= 1
            if indeg[r["succ_code"]] == 0:
                q.append(r["succ_code"])
    if len(order) < len(nodes):  # 逻辑环：剩余作业按原顺序追加，不中断计算
        order += [c for c in nodes if c not in set(order)]
    return order, preds, succs


def forward(net: dict, durations: dict[str, float] | None = None) -> dict[str, tuple[float, float]]:
    """返回 {code: (ES, EF)}，单位为距 as_of 的日历天（可为负，表示已在过去）。

    按 P6 日历排程时内部用“完成＝收工时刻”（第 k 天收工＝k+1），与 P6 一致：后续作业从次日开工；
    返回前把完成换回“完成当天”（第 k 天），与旧口径同一含义。"""
    nodes, as_of = net["nodes"], net["as_of"]
    cal = bool(net.get("cals"))
    if "_topo" not in net:
        net["_topo"] = _topo(nodes, net["rels"])
    order, preds, _ = net["_topo"]
    res: dict[str, tuple[float, float]] = {}
    for c in order:
        n = nodes[c]
        dur = durations.get(c, n["dur"]) if durations else n["dur"]
        if n["done"]:
            f = n["act_finish"] or n["p6_ef"] or as_of
            s = n["act_start"] or f
            res[c] = ((s - as_of).days, (f - as_of).days + (1 if cal else 0))
            continue
        if n["started"]:
            s = ((n["act_start"] or as_of) - as_of).days
            res[c] = (s, _add(net, c, 0.0, max(0.0, dur)))
            continue
        es = 0.0
        if n["p6_es"]:
            es = max(es, float((n["p6_es"] - as_of).days))
        for r in preds[c]:
            ps, pf = res[r["pred_code"]]
            lag = r["lag_d"] or 0.0
            k = r["kind"]
            if k == "SS":
                es = max(es, _add(net, c, ps, lag))
            elif k == "FF":
                es = max(es, _sub(net, c, _add(net, c, pf, lag), dur))
            elif k == "SF":
                es = max(es, _sub(net, c, _add(net, c, ps, lag), dur))
            else:
                es = max(es, _add(net, c, pf, lag))
        res[c] = (es, _add(net, c, es, dur))
    if cal:
        return {c: (s, f if f <= s and not nodes[c].get("fin_mile") else math.floor(f - 1e-6)) for c, (s, f) in res.items()}
    return res


def backward_float(net: dict, fw: dict) -> dict[str, float]:
    nodes = net["nodes"]
    order, _, succs = net["_topo"]
    end = max(f for _, f in fw.values()) if fw else 0.0
    lf: dict[str, float] = {}
    for c in reversed(order):
        n = nodes[c]
        dur = fw[c][1] - fw[c][0]
        v = end
        for r in succs[c]:
            s = r["succ_code"]
            ls_s = lf[s] - (fw[s][1] - fw[s][0])
            lag = r["lag_d"] or 0.0
            k = r["kind"]
            if k == "SS":
                v = min(v, ls_s - lag + dur)
            elif k == "FF":
                v = min(v, lf[s] - lag)
            elif k == "SF":
                v = min(v, lf[s] - lag + dur)
            else:
                v = min(v, ls_s - lag)
        lf[c] = v
    return {c: round(lf[c] - fw[c][1], 1) for c in nodes if not nodes[c]["done"]}


def _date(as_of: date, offset: float) -> str:
    return (as_of + timedelta(days=round(offset))).isoformat()


def forecast(conn, project_id: int, as_of: str | None = None, iterations: int = 400, seed: int = 7) -> dict | None:
    day = date.fromisoformat(as_of) if as_of else date.today()
    net = build_network(conn, project_id, day)
    if not net:
        return None
    nodes = net["nodes"]
    fw = forward(net)
    tf = backward_float(net, fw)
    milestones = [c for c, n in nodes.items() if n["milestone"] and not n["done"]]
    end_code = max(fw, key=lambda c: fw[c][1]) if fw else None
    targets = milestones or ([end_code] if end_code else [])
    rng = random.Random(seed)
    open_codes = [c for c, n in nodes.items() if not n["done"] and n["dur"] > 0]
    samples: dict[str, list[float]] = {c: [] for c in targets}
    finish_samples = []
    crit_count: dict[str, int] = defaultdict(int)
    for _ in range(iterations):
        durs = {}
        for c in open_codes:
            n = nodes[c]
            mode = 1.0
            hi = 1.35 if n["started"] else 1.30
            durs[c] = n["dur"] * rng.triangular(0.9, hi, mode)
        r = forward(net, durs)
        for c in targets:
            samples[c].append(r[c][1])
        fin = max(v[1] for v in r.values())
        finish_samples.append(fin)
        last = max(r, key=lambda c: r[c][1])
        crit_count[last] += 1

    def pctl(vals, q):
        s = sorted(vals)
        return s[min(len(s) - 1, int(q * len(s)))]
    ms = []
    for c in targets:
        n = nodes[c]
        plan = n["p6_ef"] or n["p6_es"]
        plan_off = (plan - day).days if plan else None
        vals = samples[c]
        prob = round(sum(1 for v in vals if plan_off is not None and v <= plan_off + 0.5) / len(vals), 3) if plan_off is not None and vals else None
        ms.append({"code": c, "name": n["name"], "p6_date": plan.isoformat() if plan else None,
                   "forecast": _date(day, fw[c][1]), "slip_days": round(fw[c][1] - plan_off, 1) if plan_off is not None else None,
                   "p50": _date(day, pctl(vals, 0.5)), "p80": _date(day, pctl(vals, 0.8)), "on_time_prob": prob})
    ms.sort(key=lambda m: m["p6_date"] or "9999")
    crit = sorted((c for c in tf if tf[c] <= 0.5), key=lambda c: fw[c][0])
    drivers = [{"code": c, "name": nodes[c]["name"], "forecast_start": _date(day, fw[c][0]), "forecast_finish": _date(day, fw[c][1]),
                "remaining_d": round(nodes[c]["dur"], 1), "basis": nodes[c]["basis"], "productivity_factor": nodes[c]["productivity_factor"],
                "p6_finish": nodes[c]["p6_ef"].isoformat() if nodes[c]["p6_ef"] else None,
                "slip_days": round(fw[c][1] - (nodes[c]["p6_ef"] - day).days, 1) if nodes[c]["p6_ef"] else None}
               for c in crit][:40]
    legacy = None
    if net["cals"]:  # 旧口径（每周工作天数近似）并排留一行，便于对比口径变化
        old = build_network(conn, project_id, day, use_calendar=False)
        ofw = forward(old)
        legacy = {"project_finish": _date(day, max(v[1] for v in ofw.values())), "note": f"旧口径：P6工作日按每周{old['wpw']:g}个工作日换算"}
    result = {
        "as_of": day.isoformat(), "data_date": net["import"]["data_date"], "iterations": iterations,
        "calendar_mode": "p6" if net["cals"] else "approx", "legacy": legacy,
        "calendar_note": (f"按 P6 作业日历逐日排程（{len(net['cal_names'])} 套：{'、'.join(net['cal_names'])}；节假日与雨季等例外日不计工）" if net["cals"]
                          else f"P6工作日按每周{net['wpw']:g}个工作日换算为日历天" + ("" if net["wpw"] == 7 else "（近似）")),
        "project_finish": {"forecast": _date(day, max(v[1] for v in fw.values())),
                           "p80": _date(day, pctl(finish_samples, 0.8)), "p50": _date(day, pctl(finish_samples, 0.5))},
        "milestones": ms, "critical_path": drivers,
        "slow_activities": sorted([{"code": c, "name": n["name"], "productivity_factor": n["productivity_factor"], "basis": n["basis"]}
                                   for c, n in nodes.items() if n["started"] and not n["done"] and n["productivity_factor"] > SLOW_FACTOR],
                                  key=lambda x: -x["productivity_factor"])[:10],
        "method": "基于日志实测工效的CPM前推/后推重排 + 三角分布蒙特卡洛模拟",
    }
    insert(conn, "forecasts", {"project_id": project_id, "import_id": net["import"]["id"], "as_of": day.isoformat(),
                               "result_json": dumps(result), "created_at": now_iso()})
    return with_prob_text(result)


def _prob_parts(prob, runs=None, delay_days=None) -> tuple[str, str, str]:
    """(标签, 模拟次数说明, P50 推迟说明)。四舍五入为 0/100 的不写成“0%”“100%”：模拟是估计，不是确定结论。"""
    if prob is None:
        return "未知", "", ""
    r = round(prob * 100)
    if r < 1:
        return ("<1%", f"{runs} 次模拟无一次按期" if prob <= 0 and runs else "",
                f"预计推迟 {delay_days:g} 天（P50）" if delay_days is not None and delay_days > 0 else "")
    if r > 99:
        return ">99%", f"{runs} 次模拟全部按期" if prob >= 1 and runs else "", ""
    return f"{r}%", "", ""


def prob_text(prob, runs=None, delay_days=None) -> str:
    """里程碑按期概率的统一文字（简报事实底稿与各页面共用）：
    0 次按期 →“<1%（400 次模拟无一次按期），预计推迟 N 天（P50）”；全部按期 →“>99%（400 次模拟全部按期）”；缺值 →“未知”。"""
    label, runs_note, delay_note = _prob_parts(prob, runs, delay_days)
    return label + (f"（{runs_note}）" if runs_note else "") + (f"，{delay_note}" if delay_note else "")


def p50_delay(m: dict) -> int | None:
    """P50 完成日比 P6 计划日晚多少天（不晚则为 0 或负数；缺日期为 None）。"""
    if not m.get("p50") or not m.get("p6_date"):
        return None
    return (date.fromisoformat(m["p50"]) - date.fromisoformat(m["p6_date"])).days


def with_prob_text(result: dict) -> dict:
    """给预测结果的每个里程碑加上展示文字：prob_label（标签）、prob_detail（补充说明）、prob_text（完整一句）。只读展示，不改概率本身。"""
    runs = result.get("iterations")
    for m in result.get("milestones") or []:
        delay = p50_delay(m)
        label, runs_note, delay_note = _prob_parts(m.get("on_time_prob"), runs, delay)
        m["prob_label"], m["prob_detail"] = label, "，".join(x for x in (runs_note, delay_note) if x)
        m["prob_text"] = prob_text(m.get("on_time_prob"), runs, delay)
    return result


def latest_forecast(conn, project_id: int) -> dict | None:
    row = one(conn, "SELECT result_json, created_at FROM forecasts WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,))
    return with_prob_text({**loads(row["result_json"]), "created_at": row["created_at"]}) if row else None


def what_if(conn, project_id: int, measures: list[dict], as_of: str | None = None) -> dict:
    """复算赶工方案：measures=[{activity_code, duration_factor}]，返回各里程碑提前天数（单项与组合）。"""
    day = date.fromisoformat(as_of) if as_of else date.today()
    net = build_network(conn, project_id, day)
    if not net:
        raise ValueError("尚未导入 P6 计划")
    nodes = net["nodes"]
    base = forward(net)
    targets = [c for c, n in nodes.items() if n["milestone"] and not n["done"]] or [max(base, key=lambda c: base[c][1])]

    def run(ms):
        durs = {m["activity_code"]: nodes[m["activity_code"]]["dur"] * m["duration_factor"] for m in ms
                if m["activity_code"] in nodes and not nodes[m["activity_code"]]["done"]}
        r = forward(net, durs)
        return {c: round(base[c][1] - r[c][1], 1) for c in targets}
    singles = [{**m, "gain": run([m])} for m in measures]
    combined = run(measures)
    return {"targets": [{"code": c, "name": nodes[c]["name"], "base": _date(day, base[c][1]),
                         "after": _date(day, base[c][1] - combined[c]), "gain_days": combined[c]} for c in targets],
            "measures": singles}
