"""进度异常识别与关键路径预警（规定动作：P6 接入→进度模型→阈值→触发→推送→反馈）。

进度模型（可解释、可回测）：
- 计划曲线：P6 数据日期时的完成% 到最早完成日之间按日线性推进（P6 最早日期已含逻辑与日历）；
- 实际进度：日报中技术员确认的累计完成%（数据日期之后）；
- 滞后天数（Earned Schedule 挣值进度法）：实际完成%对应计划曲线上的日期与当前日期之差；
- 关键路径影响：滞后天数 − P6 总浮时 > 0 即会推迟后续关键节点/完工；
- 下游节点：沿 P6 逻辑关系查找受影响的里程碑。
AI（交融）只负责解释原因、关联日报证据、提出措施，不参与判定，保证判定可回测。

“数据缺口”与“进度滞后”分开：已开工关键作业连续 stale_days 天没有日报进度，说明的是“没报/证据不足”，不是“实际落后”。
它不产生红/橙/黄进度预警、不推送项目经理、不进入预警反馈命中率与回测的混淆矩阵，只单列为数据缺口，
提醒对应技术员补报（见 nudge 的 datagap 来源），并在工程主管工作台单列。
"""
from __future__ import annotations

from collections import deque
from datetime import date, timedelta

from ..db import all_, dumps, loads, now_iso, one, tx, insert
from . import p6
from .logs import progress_state

DEFAULT_THRESHOLDS = {
    "critical_float_days": 0.0,   # 总浮时≤此值视为关键作业
    "red_slip_days": 1.0,          # 关键作业滞后≥此值，或滞后超过浮时 → 红色
    "orange_float_ratio": 0.5,     # 滞后消耗浮时比例≥此值 → 橙色
    "orange_min_days": 2.0,
    "yellow_slip_days": 3.0,       # 非关键作业滞后≥此值 → 黄色
    "start_grace_days": 1.0,       # 应开未开宽限
    "stale_days": 3,               # 进行中关键作业连续 N 天无进度 → 数据缺口（请补报，不是进度预警）
    "risk_lookback_days": 7,       # 日报风险信号回看天数
}

LEVEL_RANK = {"yellow": 1, "orange": 2, "red": 3}

GAP_RULE = "stale"                                   # 历史库里作为预警写入的旧规则名；现在只当“数据缺口（历史）”读
REAL_RULES = ("behind", "late_start", "risk_signal")  # 真实滞后/风险类规则：反馈命中率与回测只统计这些
GAP_NOTE = "数据缺口不计入预警准确率"
GAP_VIEW_ROLES = ("eng_sup",)                          # 能看到“数据缺口（历史）”旧预警条目的岗位；项目经理等不看缺报类信息


def thresholds(conn, project_id: int) -> dict:
    proj = one(conn, "SELECT config_json FROM projects WHERE id=?", (project_id,))
    cfg = loads(proj["config_json"], {}) if proj else {}
    out = dict(DEFAULT_THRESHOLDS)
    out.update({k: v for k, v in (cfg.get("thresholds") or {}).items() if k in DEFAULT_THRESHOLDS})
    return out


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def planned_pct(act: dict, day: date, data_date: date) -> float | None:
    es, ef = _d(act.get("early_start")), _d(act.get("early_finish"))
    if not es or not ef:
        return None
    pct0 = act.get("phys_pct") or 0.0
    start = max(es, data_date)
    if day <= start:
        return pct0
    if day >= ef:
        return 100.0
    span = (ef - start).days
    return pct0 + (100.0 - pct0) * (day - start).days / span if span > 0 else 100.0


def earned_schedule_slip(act: dict, actual_pct: float, day: date, data_date: date) -> float | None:
    """实际完成%在计划曲线上对应的日期 ES；滞后 = 当前日期 − ES（天，正数为滞后）。"""
    es, ef = _d(act.get("early_start")), _d(act.get("early_finish"))
    if not es or not ef:
        return None
    pct0 = act.get("phys_pct") or 0.0
    start = max(es, data_date)
    span = max((ef - start).days, 0)
    if actual_pct >= 100:
        return 0.0
    if span == 0 or 100 - pct0 <= 0:
        es_day = start
    else:
        frac = max(0.0, (actual_pct - pct0) / (100.0 - pct0))
        es_day = start + timedelta(days=frac * span)
    if day <= start and actual_pct <= pct0:
        return 0.0
    return round((day - es_day).days + ((day - es_day).seconds / 86400), 1)


def downstream_milestones(code: str, rels: list[dict], acts: dict[str, dict], limit: int = 5) -> list[dict]:
    succ: dict[str, list[str]] = {}
    for r in rels:
        succ.setdefault(r["pred_code"], []).append(r["succ_code"])
    seen, out, q = {code}, [], deque([code])
    while q and len(out) < limit:
        cur = q.popleft()
        for nxt in succ.get(cur, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            a = acts.get(nxt)
            if a and a.get("is_milestone"):
                out.append({"code": nxt, "name": a["name"], "planned": a.get("early_finish") or a.get("early_start"),
                            "float_d": a.get("total_float_d")})
            q.append(nxt)
    return out


def assess(act: dict, st: dict | None, day: date, data_date: date, th: dict, risk: list[dict] | None = None) -> dict | None:
    """单个作业的进度预警判定；未触发返回 None。数据缺口不在此返回，见 assess_full。"""
    return assess_full(act, st, day, data_date, th, risk)[0]


def assess_full(act: dict, st: dict | None, day: date, data_date: date, th: dict, risk: list[dict] | None = None) -> tuple[dict | None, dict | None]:
    """返回 (进度预警, 数据缺口)。已判出滞后等级的作业不再另记缺口；风险信号预警与缺口可并存。"""
    if act.get("act_finish") or act.get("is_milestone"):
        return None, None
    tf = act.get("total_float_d")
    critical = bool(act.get("is_critical")) or (tf is not None and tf <= th["critical_float_days"])
    pct0 = act.get("phys_pct") or 0.0
    plan = planned_pct(act, day, data_date)
    if plan is None:
        return None, None
    es = _d(act.get("early_start"))
    reported = st is not None and st.get("pct") is not None
    actual = st["pct"] if reported else pct0
    started = bool(act.get("act_start")) or actual > 0 or (st is not None and st.get("reports", 0) > 0)
    rule, slip = None, 0.0
    if reported:
        rule, slip = "behind", earned_schedule_slip(act, actual, day, data_date) or 0.0
    elif not started and es and day > es + timedelta(days=th["start_grace_days"]):
        rule, slip = "late_start", float((day - es).days)
    float_left = (tf - slip) if tf is not None else None
    facts = {
        "planned_pct": round(plan, 1), "actual_pct": round(actual, 1),
        "actual_source": "日报确认" if reported else ("P6数据日期" if started else "无开工记录"),
        "pct_date": st.get("pct_date") if st else None, "slip_days": slip, "total_float_d": tf,
        "float_left_d": round(float_left, 1) if float_left is not None else None, "critical": critical,
        "early_start": act.get("early_start"), "early_finish": act.get("early_finish"),
        "forecast_finish": (_d(act["early_finish"]) + timedelta(days=max(slip, 0))).isoformat() if act.get("early_finish") else None,
        "last_report": st.get("last_date") if st else None,
    }
    level = None
    if slip > 0:
        if (critical and slip >= th["red_slip_days"]) or (tf is not None and slip > max(tf, 0) and slip >= th["red_slip_days"]):
            level = "red"
        elif tf is not None and tf > 0 and slip / tf >= th["orange_float_ratio"] and slip >= th["orange_min_days"]:
            level = "orange"
        elif slip >= th["yellow_slip_days"]:
            level = "yellow"
    # 数据缺口：已开工的关键作业在计划推进期内连续 N 天没有日报进度。
    # 未知不当作零进度，也不当作落后：只提示补报，不产生进度预警、不推送项目经理、不计入预警准确率。
    gap = None
    if not level and critical and started and plan > actual:
        last = _d(st.get("last_date")) if st else None
        idle = (day - (last or data_date)).days
        if idle >= th["stale_days"]:
            gap = {"idle_days": idle, "last_report": last.isoformat() if last else None, "since": (last or data_date).isoformat(),
                   "planned_pct": round(plan, 1), "actual_pct": round(actual, 1), "total_float_d": tf}
    # 日报风险信号：关键/近浮时耗尽作业近期出现中高风险问题（缺料、设备、图纸、外部等）
    if risk:
        near = critical or (float_left is not None and float_left <= 5)
        if near:
            sig = "red" if (critical and any(r.get("severity") == "高" for r in risk)) else "orange"
            if not level:
                level, rule = sig, "risk_signal"
            elif LEVEL_RANK[sig] > LEVEL_RANK[level]:
                level = sig
            facts["risk_signals"] = risk[:5]
    if not level:
        return None, gap
    return {"level": level, "rule": rule, "facts": facts}, gap


RULE_TITLES = {
    "behind": "进度滞后", "late_start": "应开未开", "stale": "数据缺口（历史）", "risk_signal": "日报风险信号",
}


def _title(act: dict, res: dict) -> str:
    f = res["facts"]
    base = f"{act['code']} {act['name']}：{RULE_TITLES.get(res['rule'], res['rule'])}"
    if f.get("slip_days"):
        base += f" {f['slip_days']:g} 天"
    if f.get("float_left_d") is not None and f["float_left_d"] < 0:
        base += f"，超出总浮时 {-f['float_left_d']:g} 天"
    return base


def risk_signals(conn, project_id: int, day: date, lookback: int) -> dict[str, list[dict]]:
    since = (day - timedelta(days=lookback)).isoformat()
    # 每日日志（含AI自动整理、尚未周确认的）即可发出风险信号，不必等周确认
    rows = all_(conn, """SELECT id, log_date, content_json FROM logs WHERE project_id=? AND period='day'
                         AND (status IN ('submitted','approved') OR auto=1) AND log_date>=? AND log_date<=? AND mode='ai'""", (project_id, since, day.isoformat()))
    out: dict[str, list[dict]] = {}
    for r in rows:
        for issue in loads(r["content_json"], {}).get("issues", []):
            code = issue.get("activity_code")
            if code and issue.get("severity") in ("高", "中") and issue.get("type") != "安全":
                out.setdefault(code, []).append({"log_id": r["id"], "date": r["log_date"], "type": issue.get("type"),
                                                 "severity": issue.get("severity"), "desc": issue.get("desc", "")[:120]})
    return out


def evaluate(conn, project_id: int, as_of: str | None = None) -> dict:
    """对当前 P6 批次全部未完成作业做判定，返回结果（不写库）。"""
    imp = p6.current_import(conn, project_id)
    if not imp:
        return {"import": None, "results": [], "gaps": [], "as_of": as_of}
    day = date.fromisoformat(as_of) if as_of else date.today()
    dd = date.fromisoformat(imp["data_date"])
    th = thresholds(conn, project_id)
    acts = p6.current_activities(conn, project_id)
    by_code = {a["code"]: a for a in acts}
    rels = all_(conn, "SELECT * FROM relations WHERE import_id=?", (imp["id"],))
    state = progress_state(conn, project_id, imp, before=(day + timedelta(days=1)).isoformat())
    risks = risk_signals(conn, project_id, day, int(th["risk_lookback_days"]))
    results, gaps = [], []
    for a in acts:
        res, gap = assess_full(a, state.get(a["code"]), day, dd, th, risks.get(a["code"]))
        if res:
            if res["level"] == "red":
                res["facts"]["milestones"] = downstream_milestones(a["code"], rels, by_code)
            results.append({"activity": a, **res})
        if gap:
            gaps.append({"activity": a, **gap})
    results.sort(key=lambda r: (-LEVEL_RANK[r["level"]], -(r["facts"].get("slip_days") or 0)))
    gaps.sort(key=lambda g: (-g["idle_days"], g["activity"]["code"]))
    return {"import": imp, "results": results, "gaps": gaps, "as_of": day.isoformat(), "thresholds": th,
            "monitored": sum(1 for a in acts if not a.get("act_finish") and not a.get("is_milestone"))}


def _mentioned_this_week(conn, project_id: int, as_of: str) -> set[str]:
    """本周（周一至判定日）任意一天的日志里有完成量或完成%行的作业代码（C13 / AC-4.5“已提到”）。
    含草稿、自动整理和周确认草稿，不看审核状态；只在施工内容（works）里顺带提到、或进度行既无完成量也无完成%的不算。"""
    day = date.fromisoformat(as_of)
    week_start = day - timedelta(days=day.weekday())
    codes: set[str] = set()
    for r in all_(conn, "SELECT content_json FROM logs WHERE project_id=? AND log_date>=? AND log_date<=?", (project_id, week_start.isoformat(), as_of)):
        for p in loads(r["content_json"], {}).get("progress") or []:
            if isinstance(p, dict) and p.get("activity_code") and (p.get("qty") is not None or p.get("pct") is not None):
                codes.add(p["activity_code"])
    return codes


def data_gaps(conn, project_id: int, as_of: str | None = None, ev: dict | None = None) -> list[dict]:
    """当前数据缺口清单（实时判定，不落库）：作业、最后上报日期、已空缺天数、责任人。

    责任人 = 负责范围（WBS/作业前缀）覆盖该作业的技术员，与预警推送的匹配方式一致；匹配不到则 owners 为空，由工程主管人工指派。
    本周日志（含草稿）里已有该作业完成量或完成%行的，算“已提到”，不列为缺口（C13）。豁免只在这里，判定（evaluate/回测）口径不变。
    """
    ev = ev or evaluate(conn, project_id, as_of)
    if not ev.get("gaps"):
        return []
    mentioned = _mentioned_this_week(conn, project_id, ev["as_of"])
    techs = all_(conn, "SELECT id, name, company, wbs_scope FROM users WHERE project_id=? AND role='tech' AND active=1 ORDER BY id", (project_id,))
    out = []
    for g in ev["gaps"]:
        a = g["activity"]
        if a["code"] in mentioned:
            continue
        owners = [{"id": u["id"], "name": u["name"], "company": u["company"] or ""} for u in techs if u["wbs_scope"] and p6.in_scope(a, u["wbs_scope"])]
        out.append({"code": a["code"], "name": a["name"], "wbs_path": a.get("wbs_path") or "", "last_report": g["last_report"], "since": g["since"],
                    "idle_days": g["idle_days"], "planned_pct": g["planned_pct"], "actual_pct": g["actual_pct"], "total_float_d": g["total_float_d"],
                    "owners": owners, "companies": sorted({o["company"] or "未登记单位" for o in owners}), "as_of": ev["as_of"]})
    return out


def detect(conn, project_id: int, as_of: str | None = None) -> dict:
    """运行判定并落库：新增/升级的预警返回给调用方去推送与 AI 解释。"""
    ev = evaluate(conn, project_id, as_of)
    if not ev["import"]:
        return {**ev, "new": [], "escalated": [], "cleared": 0}
    now = now_iso()
    new, escalated, seen = [], [], set()
    with tx(conn):
        for r in ev["results"]:
            a = r["activity"]
            fp = f"{r['rule'] if r['rule'] != 'risk_signal' else 'risk'}:{a['code']}"
            seen.add(fp)
            existing = one(conn, "SELECT * FROM alerts WHERE project_id=? AND fingerprint=?", (project_id, fp))
            detail = dumps(r["facts"])
            title = _title(a, r)
            if not existing:
                aid = insert(conn, "alerts", {
                    "project_id": project_id, "import_id": ev["import"]["id"], "fingerprint": fp, "activity_code": a["code"],
                    "activity_name": a["name"], "level": r["level"], "rule": r["rule"], "title": title, "detail_json": detail,
                    "first_detected": now, "last_detected": now, "as_of": ev["as_of"],
                })
                new.append(aid)
            else:
                up = LEVEL_RANK[r["level"]] > LEVEL_RANK[existing["level"]]
                reopen = existing["status"] == "closed" and not existing["feedback_by"]
                status = "open" if (reopen or (up and existing["status"] != "false_alarm")) else existing["status"]
                # 系统关闭后条件再现而重开：清掉“系统：触发条件已解除”（F-13）；人工反馈过的不会走到重开
                note = "" if reopen else existing["feedback_note"]
                conn.execute("""UPDATE alerts SET level=?, title=?, detail_json=?, last_detected=?, as_of=?, status=?, import_id=?, feedback_note=?
                                WHERE id=?""", (r["level"], title, detail, now, ev["as_of"], status, ev["import"]["id"], note, existing["id"]))
                if up or reopen:
                    escalated.append(existing["id"])
        # 条件已解除：系统关闭（不计入人工反馈准确率）
        cleared = 0
        for row in all_(conn, "SELECT id, fingerprint FROM alerts WHERE project_id=? AND status='open'", (project_id,)):
            if row["fingerprint"] not in seen and not row["fingerprint"].startswith(GAP_RULE + ":"):  # 历史“数据缺口”旧预警不动
                conn.execute("UPDATE alerts SET status='closed', feedback_note='系统：触发条件已解除' WHERE id=?", (row["id"],))
                cleared += 1
    return {**ev, "new": new, "escalated": escalated, "cleared": cleared}


# ----------------------------------------------------------------------------- 历史回测

def backtest(snapshots: list[tuple[dict, list[dict], list[dict]]], th: dict | None = None, tolerance_days: float = 0.0) -> dict:
    """用 P6 历史更新批次回测关键路径预警。

    snapshots 按数据日期升序，每项为 (meta, activities, relations)。
    对每组相邻三期 (S0, S1, S2)：以 S0 的计划与浮时 + S1 的实际完成% 在 S1 数据日期做判定（预测）；
    以 S2 中该作业的实际完成日/最新预测完成日判断“是否真的超出浮时延误”（事实）。
    只有两期时退化为：以 S1 的 P6 重算完成日作为事实（口径更弱，会在结果中注明）。
    """
    th = {**DEFAULT_THRESHOLDS, **(th or {})}
    if len(snapshots) < 2:
        raise ValueError("回测至少需要两期 P6 更新（建议三期以上）")
    snaps = sorted(snapshots, key=lambda s: s[0]["data_date"])
    rows, gap_rows, mode = [], [], "three-period" if len(snaps) >= 3 else "two-period"
    groups = [(snaps[i], snaps[i + 1], snaps[i + 2]) for i in range(len(snaps) - 2)] if mode == "three-period" else [(snaps[0], snaps[1], snaps[1])]
    for s0, s1, s2 in groups:
        d0, d1 = date.fromisoformat(s0[0]["data_date"]), date.fromisoformat(s1[0]["data_date"])
        a1 = {a["code"]: a for a in s1[1]}
        a2 = {a["code"]: a for a in s2[1]}
        for a in s0[1]:
            if a.get("act_finish") or a.get("is_milestone") or not a.get("early_finish") or not a.get("early_start"):
                continue
            if date.fromisoformat(a["early_start"]) > d1:  # 判定时点前尚未计划开工的不评
                continue
            later, outcome = a1.get(a["code"]), a2.get(a["code"])
            if not later or not outcome:
                continue
            st = {"pct": later.get("phys_pct") if later.get("phys_pct") is not None else 0.0, "pct_date": d1.isoformat(),
                  "last_date": d1.isoformat(), "reports": 1 if (later.get("act_start") or (later.get("phys_pct") or 0) > 0) else 0}
            if st["reports"] == 0:
                st = None
            res, gap = assess_full(a, st, d1, d0, th)
            if gap:  # 没报/证据不足：既不是预测也不是事实，不进混淆矩阵，单列数量
                gap_rows.append({"period": f"{s0[0]['data_date']}→{s1[0]['data_date']}→{s2[0]['data_date']}", "code": a["code"], "name": a["name"],
                                 "idle_days": gap["idle_days"], "last_report": gap["last_report"]})
                continue
            predicted = bool(res and res["level"] == "red")
            fin = outcome.get("act_finish") or outcome.get("early_finish")
            if not fin:
                continue
            true_delay = (date.fromisoformat(fin) - date.fromisoformat(a["early_finish"])).days
            tf = a.get("total_float_d") or 0.0
            actual = true_delay > max(tf, 0) + tolerance_days
            rows.append({"period": f"{s0[0]['data_date']}→{s1[0]['data_date']}→{s2[0]['data_date']}", "code": a["code"],
                         "name": a["name"], "predicted": predicted, "level": res["level"] if res else None,
                         "pred_slip": res["facts"]["slip_days"] if res else 0, "true_delay": true_delay, "float": tf, "actual": actual})
    tp = sum(1 for r in rows if r["predicted"] and r["actual"])
    fp = sum(1 for r in rows if r["predicted"] and not r["actual"])
    fn = sum(1 for r in rows if not r["predicted"] and r["actual"])
    tn = sum(1 for r in rows if not r["predicted"] and not r["actual"])
    n = len(rows)

    def ratio(a, b):
        return round(a / b, 4) if b else None
    return {
        "mode": mode, "samples": n, "data_gaps": len(gap_rows), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "accuracy": ratio(tp + tn, n), "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
        "definitions": {
            "样本": "判定时点前已计划开工、未完成的非里程碑作业（不含数据缺口作业）",
            "数据缺口": GAP_NOTE + "：已开工关键作业连续无日报进度的“没报/证据不足”作业不进入混淆矩阵，只单列数量",
            "预测为预警": "判定为红色（关键路径受影响）",
            "事实为延误": "后一期实际/预测完成日较原计划推迟超过原总浮时" + (f"+{tolerance_days}天容差" if tolerance_days else ""),
            "准确率": "(TP+TN)/样本数", "预警命中率(精确率)": "TP/(TP+FP)", "召回率": "TP/(TP+FN)",
        },
        "note": "两期模式以 P6 重算的预测完成日为事实，口径弱于三期模式" if mode == "two-period" else "",
        "rows": rows, "gap_rows": gap_rows,
    }


def feedback_stats(conn, project_id: int) -> dict:
    """人工反馈命中率：只统计真实滞后/风险类规则；历史“数据缺口”旧预警（rule=stale）排除并单列数量；回放产生的预警（alert_tags）排除并单列。"""
    from . import alert_tags
    rows = all_(conn, "SELECT rule, level, status, COUNT(*) n FROM alerts WHERE project_id=?" + alert_tags.NOT_REPLAY + " GROUP BY rule, level, status", (project_id,))
    by_rule: dict[str, dict[str, int]] = {}
    gap_hist = 0
    for r in rows:
        if r["rule"] == GAP_RULE:
            gap_hist += r["n"]
            continue
        d = by_rule.setdefault(r["rule"], {"confirmed": 0, "false_alarm": 0, "open": 0, "closed": 0})
        d[r["status"]] = d.get(r["status"], 0) + r["n"]
    total_c = sum(d["confirmed"] for d in by_rule.values())
    total_f = sum(d["false_alarm"] for d in by_rule.values())
    return {"by_rule": by_rule, "confirmed": total_c, "false_alarm": total_f,
            "precision": round(total_c / (total_c + total_f), 4) if total_c + total_f else None,
            "data_gap_history": gap_hist, "replay_excluded": alert_tags.replay_count(conn, project_id),
            "definition": "人工反馈口径：确认属实/(确认属实+误报)，未反馈与系统关闭的不计入；" + GAP_NOTE + "；回放产生的预警不计入"}


def related_logs(conn, project_id: int, code: str, day: str, days: int = 14) -> list[dict]:
    since = (date.fromisoformat(day) - timedelta(days=days)).isoformat()
    rows = all_(conn, """SELECT l.id, l.log_date, l.content_json, u.name author FROM logs l JOIN users u ON u.id=l.author_id
                         WHERE l.project_id=? AND (l.status IN ('submitted','approved') OR l.auto=1) AND l.log_date>=? AND l.mode='ai'
                         AND l.period='day' ORDER BY l.log_date DESC""", (project_id, since))
    out = []
    for r in rows:
        c = loads(r["content_json"], {})
        hits = [w["desc"] for w in c.get("works", []) if w.get("activity_code") == code]
        hits += [f"进度：{p.get('qty') or ''}{p.get('unit') or ''} 累计{p.get('pct')}%" for p in c.get("progress", []) if p.get("activity_code") == code]
        hits += [f"问题({i.get('type')})：{i.get('desc')}" for i in c.get("issues", []) if i.get("activity_code") == code]
        general = [f"问题({i.get('type')})：{i.get('desc')}" for i in c.get("issues", []) if not i.get("activity_code")]
        if hits or general:
            out.append({"log_id": r["id"], "date": r["log_date"], "author": r["author"], "items": hits[:6], "general": general[:3]})
    return out[:10]


def _p6_data_date(a: dict, explanation: dict | None, conn=None) -> str | None:
    """预警所依据的 P6 数据日期（AC-3.6）：先取分析留存的 explanation.p6_data_date；老预警没有时按 import_id 查 p6_imports；
    都查不到返回 None（界面写“P6 数据日期未知”，不补默认值）。"""
    if isinstance(explanation, dict) and explanation.get("p6_data_date"):
        return explanation["p6_data_date"]
    if not a.get("import_id"):
        return None
    sql, args = "SELECT data_date FROM p6_imports WHERE id=?", (a["import_id"],)
    if conn is not None:
        row = one(conn, sql, args)
    else:  # 调用方没给连接（接口层直接序列化）：临时开一个只读查询
        from contextlib import closing
        from ..db import connect
        with closing(connect()) as c:
            row = one(c, sql, args)
    return (row or {}).get("data_date") or None


def _is_replay(conn, alert_id: int) -> bool:
    from . import alert_tags
    return alert_id in alert_tags.replay_ids(conn, [alert_id])


def serialize_alert(a: dict, conn=None) -> dict:
    from .facts import explanation_state  # facts 依赖本模块，延迟导入
    st = explanation_state(a)
    explanation = loads(a["explanation_json"])
    return {**{k: a[k] for k in ("id", "activity_code", "activity_name", "level", "rule", "title", "status", "feedback_note",
                                 "first_detected", "last_detected", "as_of", "feedback_at")},
            "rule_name": RULE_TITLES.get(a["rule"], a["rule"]), "detail": loads(a["detail_json"], {}),
            "explanation": explanation, "ai_run_id": a["ai_run_id"],
            # 分析所依据的判定日；判定数字（滞后/浮时/预测完成）后来变了即过期，界面提示重新分析
            "explanation_as_of": st["as_of"], "explanation_stale": st["stale"],
            "replay": bool(conn is not None and _is_replay(conn, a["id"])),
            "p6_data_date": _p6_data_date(a, explanation, conn)}
