"""工笔文书增补类型（2026-10-08 增补能力 2、5、6）：周生产例会底稿、三周滚动计划、月度智能复盘。

与已有类型同一套规则（见 ai/docs_registry.py 文件头）：事实底稿由程序从系统数据算好并冻结，表格数字不经模型；
交融只组织文字，逐栏数字守卫，不过的栏退回规则版；AI 写的是草稿，人确认后冻结；不写人名、金额、产值，不写日志填报率。

- meeting_pack 例会底稿：上次例会行动项完成情况（来自 meeting_items）放第一节；近 7 天新的红橙预警；未闭环高风险隐患与质量问题；
  超期工作联系单；未来两周开工作业的放行缺项（放行Agent，不调用模型）。会上用统一输入框录纪要 → 行动项派单（已有），
  下一份底稿第一节就是这些行动项的完成情况。
- three_week_plan 三周滚动计划：当前 P6 计划里未来 21 天进行或开工的作业，附按实测工效重排的预计开始 / 完成（schedule 前推，参考推演）、
  放行缺项和 P6 日历不计工天数；中文说明 + 英文摘要（报监理用）。
- monthly_review 月度复盘：当月已闭环的隐患、质量问题、预警和联系单，由程序统计类别、次数、平均闭环天数、重复出现的类别；
  AI 写经验条目（每条须引用统计数字）。确认后的经验条目进入问工笔的周月报来源，提问时可以引用。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta

from ..ai import docs_registry as reg
from ..db import all_, loads, one
from . import p6, reports

PACK_ROLES = frozenset({"manager", "eng_sup"})
PLAN_ROLES = frozenset({"manager", "eng_sup"})
REVIEW_ROLES = frozenset({"manager", "eng_sup", "safety_sup"})
ACTIVE = ("open", "rectifying", "rejected")
_g = reg._g


def _project(conn, pid: int) -> str:
    return one(conn, "SELECT name FROM projects WHERE id=?", (pid,))["name"]


def _days(a: str | None, b: str | None) -> float | None:
    try:
        return max(0.0, (datetime.fromisoformat(b[:19]) - datetime.fromisoformat(a[:19])).total_seconds() / 86400)
    except (TypeError, ValueError):
        return None


def _avg(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def _cat(f: dict) -> str:
    from .findings import _cat_of
    return _cat_of(f) or "未分类"


def _has_table(conn, name: str) -> bool:
    return bool(one(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _gate_gaps(conn, user: dict, days: int, today: date) -> list[dict]:
    """放行清单里有缺项 / 受阻 / 临近缺项的作业（不调用模型；放行功能关闭或没有 P6 时为空）。"""
    from . import gate
    try:
        data = gate.build_list(conn, {**user, "role": "eng_sup" if user["role"] not in gate.VIEWERS else user["role"]}, days, today, allow_ai=False)
    except (PermissionError, ValueError):
        return []
    return [{"code": r["code"], "name": r["name"], "start": r.get("plan_start"), "days_to_start": r["days_to_start"], "critical": r["is_critical"],
             "verdict": r["verdict_name"], "short": r.get("short_labels") or [], "unknown_n": r.get("unknown_n", 0)}
            for r in data["items"] if r["verdict"] != "ready"]


# ============================================================================ 例会底稿

def meeting_facts(conn, user: dict, kind: str, ref: str | None) -> dict:
    pid = user["project_id"]
    d = date.fromisoformat(ref) if ref else date.today()
    start, end = reports.period_for("week", d.isoformat())
    since = (d - timedelta(days=7)).isoformat()
    last = one(conn, "SELECT * FROM meetings WHERE project_id=? AND confirmed_at IS NOT NULL AND meeting_date<=? ORDER BY meeting_date DESC, id DESC LIMIT 1",
               (pid, d.isoformat())) if _has_table(conn, "meetings") else None
    items = []
    if last:
        for it in all_(conn, "SELECT * FROM meeting_items WHERE meeting_id=? ORDER BY id", (last["id"],)):
            st = "done" if it["status"] == "done" else ("overdue" if it["due_date"] and it["due_date"] < d.isoformat() else "open")
            owner = one(conn, "SELECT role, company FROM users WHERE id=?", (it["owner_id"],)) if it["owner_id"] else None
            unit = (owner or {}).get("company") or it["owner_company"] or ""
            from .. import roles as R
            who = (unit + " " if unit else "") + (R.role_name(owner["role"]) if owner else "")
            items.append({"id": it["id"], "text": it["text"], "who": who.strip() or "未指定", "due": it["due_date"] or "", "status": st})
    acts = {"meeting_date": last["meeting_date"] if last else None, "title": last["title"] if last else None, "total": len(items),
            "done": sum(i["status"] == "done" for i in items), "overdue": sum(i["status"] == "overdue" for i in items),
            "open": sum(i["status"] == "open" for i in items), "items": items}
    alerts = [{"id": a["id"], "level": {"red": "红", "orange": "橙"}[a["level"]], "code": a["activity_code"], "name": a["activity_name"], "title": a["title"],
               "status": {"open": "待确认", "confirmed": "属实"}.get(a["status"], a["status"]), "first": a["first_detected"][:10]}
              for a in all_(conn, """SELECT * FROM alerts WHERE project_id=? AND level IN ('red','orange') AND status IN ('open','confirmed')
                                     AND first_detected>=? AND rule<>'stale' ORDER BY CASE level WHEN 'red' THEN 0 ELSE 1 END, first_detected DESC LIMIT 15""",
                                 (pid, since))]
    hz = [{"id": f["id"], "agent": "安全" if f["agent"] == "safety" else "质量", "title": f["title"][:60], "due": f["due_date"] or "",
           "overdue": bool(f["due_date"] and f["due_date"] < d.isoformat()), "cat": _cat(f)}
          for f in all_(conn, f"""SELECT * FROM findings WHERE project_id=? AND agent IN ('safety','quality') AND level='high' AND confirmed_at IS NOT NULL
                                  AND status IN ({','.join('?' * len(ACTIVE))}) ORDER BY due_date IS NULL, due_date LIMIT 15""", (pid, *ACTIVE))]
    letters = []
    if _has_table(conn, "sub_letters"):
        from . import sub_letters
        for r in all_(conn, "SELECT * FROM sub_letters WHERE project_id=? AND status<>'closed' ORDER BY due_date", (pid,)):
            s = sub_letters.state(conn, r)
            if s["overdue"] or s["ack_late"]:
                doc = one(conn, "SELECT title FROM gongbi_docs WHERE id=?", (r["doc_id"],))
                letters.append({"id": r["id"], "company": r["company"], "area": sub_letters.AREA_NAME[r["area"]], "title": (doc or {}).get("title", "")[:60],
                                "due": r["due_date"] or "", "what": "超期未回复" if s["overdue"] else "超 24 小时未签收"})
    imp = p6.current_import(conn, pid)
    return {"project": _project(conn, pid), "period_start": start, "period_end": end, "meeting_date": d.isoformat(), "since": since,
            "data_date": imp["data_date"] if imp else None, "actions": acts, "alerts": alerts, "hazards": hz, "letters": letters[:15],
            "lookahead": _gate_gaps(conn, user, 14, d)[:15]}


def meeting_text(f: dict) -> str:
    a = f["actions"]
    L = [f"【统计事实】项目：{f['project']}；本次例会日期：{f['meeting_date']}；统计近 7 天：{f['since']} 起" + (f"；P6 数据日期：{f['data_date']}" if f["data_date"] else "")]
    if a["meeting_date"]:
        L.append(f"上次例会：{a['meeting_date']}《{a['title']}》，行动项 {a['total']} 项，已完成 {a['done']} 项，未完成 {a['open']} 项，超期 {a['overdue']} 项")
        for i in a["items"]:
            L.append(f"行动项#{i['id']}（{i['who']}，期限 {i['due'] or '未定'}，{ {'done': '已完成', 'open': '未完成', 'overdue': '超期'}[i['status']] }）：{i['text']}")
    else:
        L.append("上次例会：系统里没有已确认的例会纪要")
    L.append(f"近 7 天新的红橙预警 {len(f['alerts'])} 条")
    for w in f["alerts"]:
        L.append(f"[W{w['id']}] {w['level']}色 {_an.fmt(w['code'], w['name'])}：{w['title']}（{w['status']}）")
    L.append(f"未闭环的高风险问题 {len(f['hazards'])} 项，其中超期 {sum(h['overdue'] for h in f['hazards'])} 项")
    for h in f["hazards"]:
        L.append(f"[S{h['id']}] {h['agent']}·{h['cat']}：{h['title']}（期限 {h['due'] or '未定'}{'，已超期' if h['overdue'] else ''}）")
    L.append(f"超期或未签收的工作联系单 {len(f['letters'])} 份")
    for x in f["letters"]:
        L.append(f"联系单#{x['id']} 致{x['company']}（{x['area']}）{x['title']}：{x['what']}，期限 {x['due'] or '未定'}")
    L.append(f"未来 14 天开工作业中放行有缺项的 {len(f['lookahead'])} 项")
    for g in f["lookahead"]:
        L.append(f"{_an.fmt(g['code'], g['name'])}：计划 {g['start'] or '未知'} 开工，{g['verdict']}" + ("，关键作业" if g["critical"] else "")
                 + (f"，缺：{'、'.join(g['short'][:3])}" if g["short"] else "") + (f"，另 {g['unknown_n']} 项待现场确认" if g["unknown_n"] else ""))
    return "\n".join(L)


def meeting_rule(kind: str, f: dict) -> dict:
    a = f["actions"]
    act = (f"上次例会（{a['meeting_date']}）行动项 {a['total']} 项，已完成 {a['done']} 项，未完成 {a['open']} 项，超期 {a['overdue']} 项。"
           if a["meeting_date"] else "系统里还没有已确认的例会纪要；本次会上用顶部输入框整理纪要后，行动项会进入下次底稿。")
    n_over = sum(h["overdue"] for h in f["hazards"])
    reds = sum(1 for w in f["alerts"] if w["level"] == "红")
    topics = []
    if a["overdue"]:
        topics.append(f"上次例会超期的 {a['overdue']} 项行动项逐项说明原因并定新期限")
    if reds:
        topics.append(f"近 7 天新增红色预警 {reds} 条，责任单位说明赶工安排")
    if n_over:
        topics.append(f"超期未闭环的高风险问题 {n_over} 项，明确整改完成时间")
    if f["letters"]:
        topics.append(f"{len(f['letters'])} 份工作联系单超期未回复或未签收，相关单位当场答复")
    if f["lookahead"]:
        topics.append(f"未来两周 {len(f['lookahead'])} 项作业放行有缺项，落实责任人与时间")
    return {
        "opening": "本周例会底稿由系统按已确认数据汇总：" + act + f"近 7 天新增红橙预警 {len(f['alerts'])} 条，未闭环高风险问题 {len(f['hazards'])} 项，"
                   f"超期或未签收联系单 {len(f['letters'])} 份，未来两周放行有缺项的作业 {len(f['lookahead'])} 项。",
        "actions": act,
        "alerts": f"近 7 天新的红橙预警 {len(f['alerts'])} 条" + (f"，其中红色 {reds} 条。" if f["alerts"] else "。"),
        "hazards": f"未闭环的高风险安全、质量问题 {len(f['hazards'])} 项" + (f"，其中超期 {n_over} 项。" if f["hazards"] else "。"),
        "letters": f"超期未回复或逾期未签收的工作联系单 {len(f['letters'])} 份。",
        "lookahead": f"未来 14 天计划开工、放行仍有缺项的作业 {len(f['lookahead'])} 项" + ("，详见下表。" if f["lookahead"] else "。"),
        "topics": topics[:5],
    }


def meeting_tables(f: dict) -> dict:
    st = {"done": "已完成", "open": "未完成", "overdue": "超期"}
    return {
        "m_actions": {"headers": ["#", "行动项", "责任单位/岗位", "期限", "状态"], "rows": [[i["id"], i["text"], i["who"], i["due"] or "—", st[i["status"]]] for i in f["actions"]["items"]]},
        "m_alerts": {"headers": ["编号", "等级", "作业", "预警", "状态"], "rows": [[f"W{w['id']}", w["level"], f"{w['code']} {w['name']}", w["title"], w["status"]] for w in f["alerts"]]},
        "m_hazards": {"headers": ["编号", "类别", "问题", "期限"], "rows": [[f"S{h['id']}", f"{h['agent']}·{h['cat']}", h["title"], (h["due"] or "—") + ("（超期）" if h["overdue"] else "")] for h in f["hazards"]]},
        "m_letters": {"headers": ["#", "单位", "类别", "联系单", "情况", "期限"], "rows": [[x["id"], x["company"], x["area"], x["title"], x["what"], x["due"] or "—"] for x in f["letters"]]},
        "m_look": {"headers": ["作业", "计划开工", "放行结论", "缺项", "关键"],
                   "rows": [[f"{g['code']} {g['name']}", g["start"] or "—", g["verdict"], "、".join(g["short"][:3]) or (f"{g['unknown_n']} 项待现场确认" if g["unknown_n"] else "—"), "是" if g["critical"] else ""] for g in f["lookahead"]]},
    }


# ============================================================================ 三周滚动计划

def plan_facts(conn, user: dict, kind: str, ref: str | None) -> dict:
    from . import schedule
    pid = user["project_id"]
    d = date.fromisoformat(ref) if ref else date.today()
    start = d - timedelta(days=d.weekday())
    end = start + timedelta(days=20)
    imp = p6.current_import(conn, pid)
    if not imp:
        raise ValueError("还没有导入 P6 计划，无法编制三周滚动计划")
    acts = p6.range_activities(p6.current_activities(conn, pid), start.isoformat(), end.isoformat(), horizon_days=0, limit=80)
    net = schedule.build_network(conn, pid, d)
    fw = schedule.forward(net) if net else {}
    rows = []
    for a in acts:
        n = (net or {}).get("nodes", {}).get(a["code"]) or {}
        if n.get("done"):
            continue   # 周确认已到 100% 的作业不再列入滚动计划
        fs = schedule._date(d, fw[a["code"]][0]) if a["code"] in fw else None
        ff = schedule._date(d, fw[a["code"]][1]) if a["code"] in fw else None
        slip = (date.fromisoformat(ff) - date.fromisoformat(a["early_finish"][:10])).days if ff and a.get("early_finish") else None
        rows.append({"code": a["code"], "name": a["name"], "p6_start": (a.get("early_start") or "")[:10], "p6_finish": (a.get("early_finish") or "")[:10],
                     "fc_start": fs, "fc_finish": ff, "slip": slip, "pct": round(n.get("pct") or a.get("phys_pct") or 0, 1), "started": bool(n.get("started")),
                     "critical": bool(a.get("is_critical")), "float": a.get("total_float_d"), "basis": n.get("basis") or "P6原定工期",
                     "factor": n.get("productivity_factor")})
    rows.sort(key=lambda r: (r["fc_start"] or r["p6_start"] or "9999", r["code"]))
    nonwork = 0
    if net and net.get("cals"):
        cal = Counter(id(c) for c in net["cals"].values()).most_common(1)
        c0 = next(c for c in net["cals"].values() if id(c) == cal[0][0])
        nonwork = sum(1 for i in range(21) if c0.day_share(start + timedelta(days=i)) <= 0 and (start + timedelta(days=i)).weekday() < 6)
    gaps = _gate_gaps(conn, user, 21, d)
    slow = [r for r in rows if r["factor"] and r["factor"] > schedule.SLOW_FACTOR]
    return {"project": _project(conn, pid), "period_start": start.isoformat(), "period_end": end.isoformat(), "data_date": imp["data_date"],
            "rows": rows, "gaps": gaps[:20], "nonwork": nonwork, "calendar": bool(net and net.get("cals")),
            "counts": {"total": len(rows), "critical": sum(r["critical"] for r in rows), "started": sum(r["started"] for r in rows),
                       "late": sum(1 for r in rows if r["slip"] and r["slip"] > 0), "slow": len(slow), "gaps": len(gaps)}}


def plan_text(f: dict) -> str:
    c = f["counts"]
    L = [f"【统计事实】项目：{f['project']}；三周滚动计划：{f['period_start']} 至 {f['period_end']}；P6 数据日期：{f['data_date']}",
         f"窗口内作业 {c['total']} 项，其中关键作业 {c['critical']} 项，已开工 {c['started']} 项；按实测工效重排后预计晚于 P6 完成的 {c['late']} 项；工效偏低 {c['slow']} 项；放行有缺项 {c['gaps']} 项",
         (f"P6 日历显示窗口内另有 {f['nonwork']} 个工作日不计工（节假日、雨季等例外日）" if f["calendar"] else "P6 未提供可用日历，按工作日近似")]
    L.append("作业（代码 名称；P6 开始~完成；预计开始~完成（参考推演）；较 P6 晚天数；当前完成%；依据）：")
    for r in f["rows"]:
        L.append(f"{_an.fmt(r['code'], r['name'])}；{r['p6_start'] or '未知'}~{r['p6_finish'] or '未知'}；{r['fc_start'] or '未知'}~{r['fc_finish'] or '未知'}；"
                 f"{_g(r['slip'])}；{_g(r['pct'])}%；{r['basis']}" + ("；关键" if r["critical"] else ""))
    L.append("放行缺项：")
    for g in f["gaps"]:
        L.append(f"{_an.fmt(g['code'], g['name'])}：{g['verdict']}" + (f"，缺：{'、'.join(g['short'][:3])}" if g["short"] else "") + (f"，另 {g['unknown_n']} 项待现场确认" if g["unknown_n"] else ""))
    return "\n".join(L)


def plan_rule(kind: str, f: dict) -> dict:
    c = f["counts"]
    late = [r for r in f["rows"] if r["slip"] and r["slip"] > 0]
    crit = [r for r in f["rows"] if r["critical"]]
    return {
        "summary": f"本期滚动计划覆盖 {f['period_start']} 至 {f['period_end']}，依据 P6 数据日期 {f['data_date']} 的计划，共 {c['total']} 项作业，其中关键作业 {c['critical']} 项、已开工 {c['started']} 项。"
                   + (f"按日志实测工效重排（参考推演），{c['late']} 项预计晚于 P6 完成。" if c["late"] else "按日志实测工效重排（参考推演），暂无作业预计晚于 P6 完成。"),
        "critical": "；".join(f"{r['code']} {r['name']}（预计 {r['fc_start'] or r['p6_start']} ~ {r['fc_finish'] or r['p6_finish']}）" for r in crit[:8]) + "。" if crit else "窗口内没有关键作业。",
        "risks": ("；".join(f"{r['code']} {r['name']} 预计较 P6 晚 {r['slip']} 天完成" for r in late[:6]) + "。" if late else "暂无预计晚完的作业。")
                 + (f"放行有缺项的作业 {c['gaps']} 项，见下表。" if c["gaps"] else ""),
        "weather": (f"P6 日历显示窗口内有 {f['nonwork']} 个工作日不计工（节假日、雨季等例外日），已在预计日期中考虑。" if f["calendar"] else "P6 未提供可用日历，预计日期按工作日近似。"),
        "en_summary": f"Three-week look-ahead from {f['period_start']} to {f['period_end']} based on the programme with data date {f['data_date']}: {c['total']} activities, "
                      f"of which {c['critical']} are critical and {c['started']} have started. {c['late']} activities are forecast to finish later than programme "
                      f"based on recorded productivity (indicative only); {c['gaps']} activities have outstanding pre-start conditions.",
    }


def plan_tables(f: dict) -> dict:
    return {
        "p_rows": {"headers": ["作业", "P6 开始", "P6 完成", "预计开始", "预计完成", "较 P6 晚（天）", "完成%", "关键"],
                   "rows": [[f"{r['code']} {r['name']}", r["p6_start"] or "—", r["p6_finish"] or "—", r["fc_start"] or "—", r["fc_finish"] or "—",
                             _g(r["slip"]) if r["slip"] is not None else "—", _g(r["pct"]), "是" if r["critical"] else ""] for r in f["rows"]]},
        "p_gaps": {"headers": ["作业", "计划开工", "放行结论", "缺项"],
                   "rows": [[f"{g['code']} {g['name']}", g["start"] or "—", g["verdict"], "、".join(g["short"][:3]) or (f"{g['unknown_n']} 项待现场确认" if g["unknown_n"] else "—")] for g in f["gaps"]]},
    }


# ============================================================================ 月度复盘

def review_facts(conn, user: dict, kind: str, ref: str | None) -> dict:
    pid = user["project_id"]
    start, end = reports.period_for("month", ref)
    e1 = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    fs = all_(conn, """SELECT * FROM findings WHERE project_id=? AND agent IN ('safety','quality') AND status='resolved' AND resolved_at>=? AND resolved_at<?""",
              (pid, start, e1))
    groups: dict[tuple, list] = {}
    for f in fs:
        groups.setdefault(("安全" if f["agent"] == "safety" else "质量", _cat(f)), []).append(f)
    cats = sorted(({"area": k[0], "cat": k[1], "n": len(v), "avg_days": _avg([_days(x["confirmed_at"] or x["created_at"], x["resolved_at"]) for x in v]),
                    "high": sum(1 for x in v if x["level"] == "high"), "ids": [x["id"] for x in v][:6],
                    "measures": [m for m in dict.fromkeys((loads(x["body_json"], {}) or {}).get("measure") or "" for x in v) if m][:3]}
                   for k, v in groups.items()), key=lambda c: (-c["n"], c["cat"]))
    new_open = all_(conn, f"""SELECT * FROM findings WHERE project_id=? AND agent IN ('safety','quality') AND confirmed_at IS NOT NULL AND created_at>=? AND created_at<?
                              AND status IN ({','.join('?' * len(ACTIVE))})""", (pid, start, e1, *ACTIVE))
    al = all_(conn, "SELECT * FROM alerts WHERE project_id=? AND rule<>'stale' AND feedback_at>=? AND feedback_at<? AND status IN ('confirmed','false_alarm','closed')", (pid, start, e1))
    rules = Counter(a["rule"] for a in al if a["status"] != "false_alarm")
    alerts = {"handled": len(al), "confirmed": sum(a["status"] == "confirmed" for a in al), "false_alarm": sum(a["status"] == "false_alarm" for a in al),
              "closed": sum(a["status"] == "closed" for a in al), "avg_days": _avg([_days(a["first_detected"], a["feedback_at"]) for a in al]),
              "by_rule": dict(rules.most_common(5))}
    letters = {"closed": 0, "avg_days": None}
    if _has_table(conn, "sub_letters"):
        ls = all_(conn, "SELECT * FROM sub_letters WHERE project_id=? AND closed_at>=? AND closed_at<?", (pid, start, e1))
        letters = {"closed": len(ls), "avg_days": _avg([_days(x["issued_at"], x["closed_at"]) for x in ls])}
    repeat = [c for c in cats if c["n"] >= 2]
    return {"project": _project(conn, pid), "period_start": start, "period_end": end, "closed": len(fs), "cats": cats[:12], "repeat": repeat[:8],
            "still_open": {"n": len(new_open), "cats": dict(Counter(_cat(f) for f in new_open).most_common(5))}, "alerts": alerts, "letters": letters}


def review_text(f: dict) -> str:
    a, lt = f["alerts"], f["letters"]
    L = [f"【统计事实】项目：{f['project']}；复盘月份：{f['period_start']} 至 {f['period_end']}",
         f"当月闭环的安全、质量问题 {f['closed']} 项；当月新发现仍未闭环 {f['still_open']['n']} 项"]
    L.append("已闭环问题按类别（领域·类别：次数；其中高风险；平均闭环天数；编号；记录的整改措施）：")
    for c in f["cats"]:
        L.append(f"{c['area']}·{c['cat']}：{c['n']} 次；高风险 {c['high']}；平均 {_g(c['avg_days'])} 天；" + "、".join(f"[S{i}]" for i in c["ids"])
                 + (f"；措施：{'／'.join(c['measures'])}" if c["measures"] else ""))
    L.append("重复出现（2 次及以上）的类别：" + ("；".join(f"{c['area']}·{c['cat']} {c['n']} 次" for c in f["repeat"]) or "无"))
    L.append("当月新发现仍未闭环的类别：" + ("；".join(f"{k} {v} 项" for k, v in f["still_open"]["cats"].items()) or "无"))
    L.append(f"进度预警：当月处理 {a['handled']} 条，属实 {a['confirmed']} 条，误报 {a['false_alarm']} 条，已关闭 {a['closed']} 条，平均 {_g(a['avg_days'])} 天给出结论；"
             + "按规则：" + ("；".join(f"{k} {v} 条" for k, v in a["by_rule"].items()) or "无"))
    L.append(f"工作联系单：当月销项 {lt['closed']} 份，平均 {_g(lt['avg_days'])} 天销项")
    return "\n".join(L)


def review_rule(kind: str, f: dict) -> dict:
    a = f["alerts"]
    top = f["cats"][:3]
    lessons = [f"{c['area']}·{c['cat']}本月闭环 {c['n']} 次，平均 {_g(c['avg_days'])} 天闭环" + (f"，记录的措施：{c['measures'][0]}" if c["measures"] else "") + "。"
               for c in f["repeat"][:5]]
    return {
        "summary": f"{f['period_start']} 至 {f['period_end']} 共闭环安全、质量问题 {f['closed']} 项，当月新发现仍未闭环 {f['still_open']['n']} 项；"
                   f"进度预警处理 {a['handled']} 条（属实 {a['confirmed']} 条、误报 {a['false_alarm']} 条）。",
        "top": "；".join(f"{c['area']}·{c['cat']} {c['n']} 次（平均 {_g(c['avg_days'])} 天闭环）" for c in top) + "。" if top else "本月没有闭环的安全、质量问题。",
        "repeat": ("重复出现的类别：" + "；".join(f"{c['area']}·{c['cat']} {c['n']} 次" for c in f["repeat"]) + "。") if f["repeat"] else "本月没有重复出现 2 次及以上的问题类别。",
        "lessons": lessons,
        "next": [f"{k}类问题当月仍有 {v} 项未闭环，下月跟踪" for k, v in list(f["still_open"]["cats"].items())[:4]],
    }


def review_tables(f: dict) -> dict:
    return {"r_cats": {"headers": ["领域·类别", "闭环次数", "高风险", "平均闭环天数", "编号"],
                       "rows": [[f"{c['area']}·{c['cat']}", c["n"], c["high"], _g(c["avg_days"]), "、".join(f"S{i}" for i in c["ids"])] for c in f["cats"]]}}


# ============================================================================ 注册

_s = reg._sec
TYPES = {
    "meeting_pack": {
        "name": "周生产例会底稿", "zh": "例会底稿", "lang": "zh", "ref": "week", "group": "例会",
        "roles": PACK_ROLES, "confirm": PACK_ROLES, "intro": "会前一键汇总：上次行动项完成情况、新红橙预警、未闭环高风险问题、超期联系单、未来两周放行缺项。会上用顶部输入框录纪要，行动项进待办。",
        "title": "{project} 周生产例会底稿",
        "sections": [
            _s("actions", "一、上次例会行动项完成情况", "说明上次例会行动项总数、已完成、未完成、超期数，点出超期的事项与责任单位。", table="m_actions", max_chars=260),
            _s("opening", "二、开场小结", "用 2～3 句概括本周需要会上拍板的重点，按轻重排序。", max_chars=220),
            _s("alerts", "三、近 7 天新的红橙预警", "点出红色预警对应的作业，引用[W编号]。", table="m_alerts", max_chars=220),
            _s("hazards", "四、未闭环的高风险问题", "点出超期的问题，引用[S编号]。", table="m_hazards", max_chars=220),
            _s("letters", "五、超期工作联系单", "点出超期未回复或未签收的单位。", table="m_letters", max_chars=160),
            _s("lookahead", "六、未来两周开工作业的放行缺项", "点出关键作业和临近开工的缺项。", table="m_look", max_chars=220),
            _s("topics", "七、建议本次议题", "每条一句，按轻重排序，只来自上面的事实。", typ="list", max_items=5),
        ],
        "facts": meeting_facts, "text": meeting_text, "rule": meeting_rule, "tables": meeting_tables,
    },
    "three_week_plan": {
        "name": "三周滚动计划", "zh": "三周滚动计划", "lang": "zh", "ref": "week", "group": "例会",
        "roles": PLAN_ROLES, "confirm": PLAN_ROLES, "intro": "从当前 P6 计划取未来 3 周的作业，附按实测工效重排的预计日期（参考推演）、放行缺项与日历不计工天数；中文说明 + 英文摘要。",
        "title": "{project} 三周滚动计划",
        "sections": [
            _s("summary", "一、编制说明", "说明覆盖日期、依据的 P6 数据日期、作业数量和关键作业数量，注明预计日期为“参考推演”。", table="p_rows", max_chars=300),
            _s("critical", "二、关键作业安排", "逐项写关键作业的预计开始与完成日期。", max_chars=320),
            _s("risks", "三、风险与前置条件", "写预计晚于 P6 完成的作业和放行缺项。", table="p_gaps", max_chars=320),
            _s("weather", "四、日历与天气", "写 P6 日历里不计工的天数；没有日历就照实说。", max_chars=120),
            _s("en_summary", "Look-ahead Summary (English)", "Write a concise English summary for the Engineer using only numbers and dates from the facts; no names, no money.", max_chars=500),
        ],
        "facts": plan_facts, "text": plan_text, "rule": plan_rule, "tables": plan_tables,
    },
    "monthly_review": {
        "name": "月度智能复盘", "zh": "月度复盘", "lang": "zh", "ref": "month", "group": "分析",
        "roles": REVIEW_ROLES, "confirm": frozenset({"manager", "eng_sup"}), "intro": "统计当月闭环的隐患、质量问题、预警和联系单：哪类最多、平均几天闭环、哪些反复出现；AI 写经验条目，确认后可在提问时引用。",
        "title": "{project} 月度复盘",
        "sections": [
            _s("summary", "一、本月概况", "概括闭环数量、未闭环数量与预警处理情况。", max_chars=220),
            _s("top", "二、出现最多的问题", "写出现次数最多的类别和平均闭环天数，引用[S编号]。", table="r_cats", max_chars=260),
            _s("repeat", "三、反复出现的问题", "写重复出现的类别；没有就照实说。", max_chars=200),
            _s("lessons", "四、经验条目", "每条一句，写“什么问题、怎么处理有效、下次怎么做”，措施只能取自事实里记录的整改措施，每条带一个出自事实的数字。", typ="list", max_items=6),
            _s("next", "五、下月关注", "每条一句，只来自事实。", typ="list", max_items=4),
        ],
        "facts": review_facts, "text": review_text, "rule": review_rule, "tables": review_tables,
    },
}
reg.TYPES.update(TYPES)
reg.NEW_KINDS = tuple(k for k, t in reg.TYPES.items() if not t.get("legacy"))


def lessons(conn, pid: int, limit: int = 3) -> list[str]:
    """问工笔用：已确认的月度复盘里的经验条目（最近几期）。"""
    out = []
    for r in all_(conn, "SELECT period_start, narrative_json FROM gongbi_docs WHERE project_id=? AND kind='monthly_review' AND status='confirmed' ORDER BY period_start DESC, version DESC LIMIT ?",
                  (pid, limit)):
        for x in (loads(r["narrative_json"], {}) or {}).get("lessons") or []:
            out.append(f"[月度复盘 {r['period_start'][:7]}] {x}")
    return out


from . import precontrol as _precontrol  # noqa: E402  高风险作业预控提醒（致分包文书的一种）
_precontrol.register_type()

from . import doc_debt as _doc_debt  # noqa: E402  资料补齐通知（致分包文书的一种）
from . import act_names as _an
_doc_debt.register_type()
