"""成效统计：全部取自系统自动记录，口径随数据一起返回；样本不足时不出结论。"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, timedelta
from statistics import median

from ..agents import AGENTS
from ..db import all_, loads, one
from ..roles import hides_fill_rate
from . import progress as progress_svc
from . import ai_trace
from . import backtest as backtest_svc

MIN_SAMPLES = 3
MIN_ADOPT = 5   # AI 产出被人处置/调用的样本少于此数，不显示比例（界面写“样本不足”）
# 调用类型 → 智能体的映射与调用统计只在 ai_trace 一处（“智能应用”页同用），这里不另立口径
EXTRA_KIND_AGENT = ai_trace.EXTRA_KIND_AGENT


def _stats(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "median": None, "mean": None}
    return {"n": len(vals), "median": round(median(vals), 2), "mean": round(sum(vals) / len(vals), 2)}


def summary(conn, project_id: int, role: str = "") -> dict:
    proj = one(conn, "SELECT config_json FROM projects WHERE id=?", (project_id,))
    cfg = loads(proj["config_json"], {}) if proj else {}
    wpw = float(cfg.get("workdays_per_week") or 6)
    weekly = cfg.get("cadence", "week") == "week"
    manual = all_(conn, "SELECT * FROM logs WHERE project_id=? AND mode='manual' AND submitted_at IS NOT NULL", (project_id,))
    man_min = _stats([l["edit_seconds"] / 60 for l in manual if l["edit_seconds"]])
    per = "week" if weekly else "day"
    weeks = all_(conn, "SELECT * FROM logs WHERE project_id=? AND mode='ai' AND period=? AND submitted_at IS NOT NULL" +
                 ("" if weekly else " AND ai_draft_json IS NOT NULL"), (project_id, per))
    days = all_(conn, "SELECT * FROM logs WHERE project_id=? AND mode='ai' AND period='day' AND submitted_at IS NOT NULL AND ai_draft_json IS NOT NULL", (project_id,))
    # 每人每周记录负担 = 本周随手记录入时长 + 周确认编写时长
    burden = []
    for w in weeks:
        n = one(conn, "SELECT SUM(seconds) s, COUNT(*) c FROM notes WHERE author_id=? AND note_date BETWEEN ? AND ?",
                (w["author_id"], w["log_date"], w["period_end"] or w["log_date"]))
        burden.append(((n["s"] or 0) + (w["edit_seconds"] or 0)) / 60)
    sys_week = _stats(burden)
    confirm_min = _stats([w["edit_seconds"] / 60 for w in weeks if w["edit_seconds"]])
    note_sec = _stats([n["seconds"] for n in all_(conn, "SELECT seconds FROM notes WHERE project_id=? AND seconds IS NOT NULL", (project_id,))])
    rep_base = [b["value"] for b in all_(conn, "SELECT value FROM events WHERE project_id=? AND kind='baseline.report_week'", (project_id,))]
    if weekly:
        base_week = round(man_min["median"] * wpw + (median(rep_base) if rep_base else 0), 1) if man_min["median"] else None
    else:  # 按日模式：同口径比较单份日志
        base_week = man_min["median"]
    saving = None
    if base_week and sys_week["n"] >= MIN_SAMPLES and man_min["n"] >= MIN_SAMPLES and sys_week["median"] is not None:
        saving = round(1 - sys_week["median"] / base_week, 4)
    on_time = [w for w in weeks if w["submitted_at"][:10] <= (date.fromisoformat(w["period_end"] or w["log_date"]) + timedelta(days=0 if not weekly else 1)).isoformat()]
    ratios = _stats([w["edit_ratio"] for w in weeks if w["edit_ratio"] is not None])
    adopted = [w for w in weeks if w["edit_ratio"] is not None and w["edit_ratio"] <= 0.3]
    notes_n = one(conn, "SELECT COUNT(*) n, COUNT(DISTINCT author_id || note_date) d FROM notes WHERE project_id=?", (project_id,))
    daily = {
        "cadence": "week" if weekly else "day",
        "week_minutes": sys_week, "confirm_minutes": confirm_min, "note_seconds": note_sec,
        "manual_minutes": man_min, "baseline_week_minutes": base_week, "workdays_per_week": wpw,
        "baseline_report_minutes": round(median(rep_base), 1) if rep_base else None,
        "time_saving": saving,
        "time_saving_note": None if saving is not None else f"周确认与手工对照日志各需至少{MIN_SAMPLES}份才计算节省比例",
        "on_time_rate": round(len(on_time) / len(weeks), 4) if weeks else None,
        "edit_ratio": ratios, "adopted_rate": round(len(adopted) / ratios["n"], 4) if ratios["n"] else None,
        "submitted": len(weeks), "manual_submitted": len(manual), "auto_days": len(days),
        "notes": notes_n["n"], "note_days": notes_n["d"],
        # 兼容旧字段
        "ai_minutes": confirm_min,
    }
    runs = all_(conn, """SELECT kind, provider, COUNT(*) n, SUM(ok) ok, AVG(latency_ms) lat FROM ai_runs WHERE project_id=?
                         GROUP BY kind, provider""", (project_id,))
    reports = all_(conn, "SELECT kind, gen_seconds FROM reports WHERE project_id=?", (project_id,))
    baselines = all_(conn, "SELECT * FROM events WHERE project_id=? AND kind LIKE 'baseline.%' ORDER BY id DESC", (project_id,))
    users = one(conn, """SELECT COUNT(DISTINCT user_id) n FROM events WHERE project_id=? AND kind IN ('log.submit','report.generate','qa.ask')""",
                (project_id,))["n"]
    photos = one(conn, "SELECT COUNT(*) n FROM media WHERE project_id=?", (project_id,))["n"]
    # 预警准确率只统计真实滞后/风险类规则；“数据缺口”（含历史 stale 旧预警）单列，不计入
    from . import alert_tags
    alerts = one(conn, "SELECT COUNT(*) n, SUM(status='confirmed') c, SUM(status='false_alarm') f FROM alerts WHERE project_id=? AND rule<>?"
                 + alert_tags.NOT_REPLAY, (project_id, progress_svc.GAP_RULE))   # 回放产生的预警不计入应用成效
    gap_hist = one(conn, "SELECT COUNT(*) n FROM alerts WHERE project_id=? AND rule=?", (project_id, progress_svc.GAP_RULE))["n"]
    gap_now = len(progress_svc.data_gaps(conn, project_id)) if one(conn, "SELECT 1 FROM p6_imports WHERE project_id=? AND is_current=1", (project_id,)) else None
    out = {
        "daily_log": daily,
        "reports": {"count": len(reports), "gen_seconds": _stats([r["gen_seconds"] for r in reports])},
        "baselines": [{"kind": b["kind"], "minutes": b["value"], **loads(b["data_json"], {}), "at": b["created_at"]} for b in baselines[:30]],
        "ai_runs": [{"kind": r["kind"], "label": ai_trace.kind_name(r["kind"]), "provider": r["provider"], "n": r["n"], "ok": r["ok"],
                     "avg_latency_s": round((r["lat"] or 0) / 1000, 1)} for r in runs],
        "usage": {"active_users": users, "photos": photos},
        "alerts": {"total": alerts["n"], "confirmed": alerts["c"] or 0, "false_alarm": alerts["f"] or 0,
                   "feedback_precision": round(alerts["c"] / (alerts["c"] + alerts["f"]), 4) if (alerts["c"] or 0) + (alerts["f"] or 0) else None,
                   "data_gaps_now": gap_now, "data_gaps_history": gap_hist, "gap_note": progress_svc.GAP_NOTE,
                   "replay_excluded": alert_tags.replay_count(conn, project_id)},
        # 只引用来源为“真实项目数据”的最新一次回测；仿真/测试数据的回测不计入应用成效
        "backtest": _safe(lambda: backtest_svc.metrics_brief(conn, project_id)),
        "ai_adoption": ai_adoption(conn, project_id, cfg, ratios),
        "definitions": {
            "日报编制耗时": "从开始编写到提交的页面活跃时长（页面在后台时不计），单位分钟，取中位数",
            "手工对照": "同一技术员不使用AI、按同一模板手工填写的日志，用于同口径对比",
            "节省比例": "1 − AI日志耗时中位数 / 手工日志耗时中位数",
            "当日提交率": "日志日期当天提交的比例",
            "修改率": "提交内容相对AI草稿的文本差异（0=原样采用，1=全部重写）",
            "草稿采纳率": "修改率≤0.3的日志占比",
            "预警反馈命中率": "确认属实/(确认属实+误报)，只统计进度滞后、应开未开、日报风险信号类预警",
            "数据缺口": "已开工关键作业连续多日没有日报进度，说明“没报/证据不足”而非“实际落后”；" + progress_svc.GAP_NOTE + "，回测混淆矩阵也不含缺口，仅单列数量",
            "历史回测": "只引用数据来源为“真实项目数据”的最新一次回测，主指标为命中率 TP/(TP+FP)；仿真/测试数据的回测不计入应用成效",
        },
    }
    if hides_fill_rate(role):  # 项目经理口径：不出现按时确认率（统计口径不变，只是不返回）
        out["daily_log"].pop("on_time_rate", None)
        out["definitions"].pop("当日提交率", None)
    return out


# ----------------------------------------------------------------------------- AI 产出被人采纳情况（按智能体）

def _rate(k: int, n: int) -> dict:
    """k/n；n 不足 MIN_ADOPT 时不给比例（n=0 界面显示“—”，0<n<5 显示“样本不足”），绝不补零、不估算。"""
    return {"k": k, "n": n, "rate": round(k / n, 4) if n >= MIN_ADOPT else None}


def _safe(fn):
    """个别扩展表缺失（旧库）时，该来源如实视为无记录，不影响其他来源。"""
    try:
        return fn()
    except sqlite3.OperationalError:
        return None


def _call_stats(conn, project_id: int) -> dict[str, dict]:
    return ai_trace.call_stats(conn, project_id)


def _decided_sources(conn, project_id: int) -> dict[str, list[dict]]:
    """有“采纳/驳回”两种明确去向的来源：agent → [{label, adopted, rejected, reasons}]。"""
    out: dict[str, list[dict]] = {}

    def add(agent, label, adopted, rejected, reasons=()):
        if adopted or rejected:
            c = Counter(" ".join(str(x).split())[:ai_trace.REASON_CHARS] for x in reasons if str(x or "").strip())
            out.setdefault(agent, []).append({"label": label, "adopted": adopted, "rejected": rejected, "reasons": dict(c)})

    # 事项与预警：闭环/属实=采纳，忽略/误报=驳回；口径与 ai_trace.feedback_summary 一致（直接复用）
    for key, f in ai_trace.feedback_summary(conn, project_id).items():
        label = "预警反馈（属实/误报）" if key == "progress" else "事项处置（闭环/忽略）"
        add(key, label, f["adopted"], f["rejected"], [x["text"] for x in f["reasons"] for _ in range(x["n"])])
    # 随手记归类建议：人点“是”=采纳，点“不是”=忽略；含规则线索与视觉分类两种建议来源，没点的不计
    r = _safe(lambda: all_(conn, "SELECT decision, COUNT(*) n FROM note_classify WHERE project_id=? AND decision IS NOT NULL GROUP BY decision", (project_id,)))
    if r:
        c = {x["decision"]: x["n"] for x in r}
        add("scribe", "随手记归类建议（采纳/忽略）", c.get("accepted", 0), c.get("dismissed", 0))
    # 票据识别草稿：确认入账=采纳；从未确认就作废=驳回（确认后又作废的两边都不计）
    t = _safe(lambda: one(conn, "SELECT SUM(status='confirmed') a, SUM(status='void' AND confirmed_at IS NULL) r FROM tickets WHERE project_id=? AND ai_status='done'",
                          (project_id,)))
    if t:
        why = _safe(lambda: [x["void_reason"] for x in all_(
            conn, "SELECT void_reason FROM tickets WHERE project_id=? AND ai_status='done' AND status='void' AND confirmed_at IS NULL", (project_id,))]) or []
        add("material", "票据草稿（确认入账/作废）", t["a"] or 0, t["r"] or 0, why)
    # 产值映射建议：商务确认=采纳，驳回=驳回
    m = one(conn, "SELECT SUM(status='confirmed') a, SUM(status='rejected') r FROM boq_map WHERE project_id=? AND ai_run_id IS NOT NULL", (project_id,))
    add("output", "清单映射建议（确认/驳回）", m["a"] or 0, m["r"] or 0)
    return out


def _confirmed_only_sources(conn, project_id: int) -> dict[str, list[dict]]:
    """只有“已确认”、没有明确驳回记录的来源：只给 确认数/产出数，不算采纳率（未处理不等于驳回）。"""
    out: dict[str, list[dict]] = {}

    def add(agent, label, done, offered):
        if offered:
            out.setdefault(agent, []).append({"label": label, "done": done, "offered": offered})

    offered_cards = done_cards = offered_items = done_items = 0
    for e in all_(conn, "SELECT kind, data_json FROM events WHERE project_id=? AND kind IN ('cmd.parse','cmd.minutes','cmd.exec','meeting.item')", (project_id,)):
        d = loads(e["data_json"], {}) or {}
        if e["kind"] == "cmd.parse" and d.get("ready"):
            offered_cards += 1
        elif e["kind"] == "cmd.exec":
            done_cards += 1
        elif e["kind"] == "cmd.minutes":
            offered_items += int(d.get("actions") or 0)
        elif e["kind"] == "meeting.item":
            done_items += 1
    add("dispatch", "指令动作卡（确认执行/提出）", done_cards, offered_cards)
    add("dispatch", "纪要行动项（确认/提出）", done_items, offered_items)
    c = _safe(lambda: one(conn, "SELECT COUNT(*) n, SUM(ready_at IS NOT NULL) d FROM conceal_records "
                                "WHERE project_id=? AND ai_json IS NOT NULL AND ai_json NOT IN ('', '{}')", (project_id,)))
    if c:
        add("records", "隐蔽验收草稿（确认齐全/AI 预填）", c["d"] or 0, c["n"] or 0)
    rp = one(conn, "SELECT COUNT(*) n, SUM(status='published') d FROM reports WHERE project_id=? AND ai_run_id IS NOT NULL", (project_id,))
    add("report", "周/月报（审核发布/AI 起草）", rp["d"] or 0, rp["n"] or 0)
    return out


def ai_adoption(conn, project_id: int, cfg: dict | None = None, edit_ratio: dict | None = None) -> dict:
    """按智能体：调用/成功/降级修复补跑、被人采纳与驳回、修改率、驳回原因。全部来自既有记录；
    没有记录显示“—”，样本不足 MIN_ADOPT 不给比例；不估算、不补零。edit_ratio 为已有的日志修改率口径（挂在现场记录Agent 名下）。"""
    calls = _call_stats(conn, project_id)
    decided = _decided_sources(conn, project_id)
    confirm_only = _confirmed_only_sources(conn, project_id)
    rows, idle = [], []
    for ag in AGENTS:
        k = ag["key"]
        cs, ds, co = calls.get(k), decided.get(k, []), confirm_only.get(k, [])
        if not cs and not ds and not co:
            idle.append(ag["name"])
            continue
        adopted = sum(s["adopted"] for s in ds)
        rejected = sum(s["rejected"] for s in ds)
        reasons: Counter = Counter()
        for s in ds:
            reasons.update(s["reasons"])
        er = edit_ratio if (k == "scribe" and edit_ratio and edit_ratio.get("n")) else None
        rows.append({
            "key": k, "name": ag["name"], "icon": ag["icon"],
            "calls": cs["calls"] if cs else 0,
            "success": _rate(cs["ok"], cs["calls"]) if cs else _rate(0, 0),
            "repaired": cs["repaired"] if cs else None, "regen": cs["regen"] if cs else None,
            "degraded": cs["degraded"] if cs else None, "mock": cs["mock"] if cs else 0,
            "adopted": adopted, "rejected": rejected,
            "adoption": _rate(adopted, adopted + rejected), "rejection": _rate(rejected, adopted + rejected),
            "sources": [{k2: v for k2, v in s.items() if k2 != "reasons"} for s in ds],
            "confirm_only": co,
            "edit_ratio": {"n": er["n"], "median": er["median"] if er["n"] >= MIN_ADOPT else None} if er else None,
            "reasons": [{"text": t, "n": n} for t, n in reasons.most_common(ai_trace.MAX_REASONS)],
        })
    return {"agents": rows, "idle": idle, "min_samples": MIN_ADOPT,
            "simulation": bool((cfg or {}).get("simulation")),
            "has_mock": any(r["mock"] for r in rows),
            "definitions": {
                "调用": "系统自动留下的 AI 调用记录里，该智能体的调用次数（失败的也算一次）与成功率，连通测试不计；"
                        "降级＝模型输出不合格、按备用规则给出结果，修复＝请模型改正一次格式，补跑＝重新生成一次，均取自每次调用的附加记录",
                "采纳率/驳回率": "只数人已明确处置的 AI 产出，分母为已处置数、待处理的不计。采纳＝事项闭环（含当场纠正（D6）：候选“当场已纠正”一键闭环、无整改照片，计为采纳）、预警属实、随手记归类点“是”、票据确认入账、清单映射确认；"
                               "驳回＝事项忽略、预警误报、归类点“不是”、票据未确认即作废、清单映射驳回",
                "确认/提出": "指令动作卡、纪要行动项、隐蔽验收草稿、周/月报没有“驳回”记录，只给确认数/提出数，不当作采纳率",
                "修改率": "沿用日志口径：提交内容相对 AI 草稿的文本差异中位数，挂在现场记录Agent 名下",
                "样本不足": f"样本少于 {MIN_ADOPT} 不显示百分比；没有任何记录显示“—”",
            }}
