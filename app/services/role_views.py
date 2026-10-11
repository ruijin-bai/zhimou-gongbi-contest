"""按角色组织的数据：数据范围判定，以及安全员、工程主管、安全主管、系统管理员各自首页所需的汇总。

只做读取与汇总，不改变工程量、进度、审批语义；没有数据的项返回空/None，不补零造数。
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import date, datetime, timedelta

from .. import roles as R
from ..ai.client import provider_status
from ..config import settings
from ..db import all_, audit, dumps, insert, loads, now_iso, one
from . import findings, p6
from . import progress as prog_svc
from .site_agents import missing_logs

NO_COMPANY = "未标注单位"
ACTIVE = ("open", "rectifying", "rejected")


def company_label(c: str | None) -> str:
    return c or NO_COMPANY


# ----------------------------------------------------------------------------- 数据范围

def scope_ids(conn, user: dict) -> list[int] | None:
    """分包单位账号能看到的人员 id：技术员只看自己；安全员看本单位全部分包人员。港湾/管理员返回 None（不限）。"""
    role = user["role"]
    if role == "tech":
        return [user["id"]]
    if role == "sub_safety":
        rows = all_(conn, "SELECT id FROM users WHERE project_id=? AND company=? AND role IN ('tech','sub_safety') AND active=1",
                    (user["project_id"], user.get("company") or ""))
        return [r["id"] for r in rows] or [user["id"]]
    return None


def in_scope(conn, user: dict, uid: int | None) -> bool:
    ids = scope_ids(conn, user)
    return ids is None or (uid is not None and uid in ids)


def _members(conn, project_id: int) -> list[dict]:
    return all_(conn, "SELECT id, name, role, company, wbs_scope FROM users WHERE project_id=? AND active=1 AND role IN ('tech','sub_safety') ORDER BY id", (project_id,))


def _company_map(conn, project_id: int) -> dict[int, str]:
    return {u["id"]: company_label(u["company"]) for u in _members(conn, project_id)}


def _responsible(acts_by_code: dict, members: list[dict], code: str) -> list[str]:
    """按负责范围（WBS/作业前缀）推断作业所属分包单位；范围为空的人员不参与推断。"""
    act = acts_by_code.get(code)
    if not act:
        return []
    out = []
    for m in members:
        if m["wbs_scope"] and p6.in_scope(act, m["wbs_scope"]):
            c = company_label(m["company"])
            if c not in out:
                out.append(c)
    return out


# ----------------------------------------------------------------------------- 派单与催办

def finding_link(finding_id: int) -> dict:
    """消息跳转目标：隐患/事项详情。"""
    return {"route": "finding", "params": {"id": str(finding_id)}}


def _notify(conn, project_id: int, user_id: int, title: str, body: str, link: dict | None = None) -> None:
    insert(conn, "notifications", {"project_id": project_id, "user_id": user_id, "alert_id": None, "title": title, "body": body,
                                   "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(), "link_json": dumps(link) if link else None})


def assign_finding(conn, finding_id: int, user: dict, assignee_id: int) -> dict:
    f = one(conn, "SELECT * FROM findings WHERE id=? AND project_id=?", (finding_id, user["project_id"]))
    if not f:
        raise ValueError("记录不存在")
    if not R.can(user["role"], "dispatch"):
        raise PermissionError("当前角色不能派单")
    if findings.is_candidate(f):
        raise ValueError("这是 AI 候选隐患，请用“确认并派单”，确认前不能派单或通知")
    if f["status"] in ("resolved", "dismissed"):
        raise ValueError("已闭环或已忽略的事项不能再派单")
    target = one(conn, "SELECT * FROM users WHERE id=? AND project_id=? AND active=1", (assignee_id, user["project_id"]))
    if not target or target["role"] in ("admin", "leader"):
        raise ValueError("责任人无效")
    if R.is_sub(user["role"]):
        if not in_scope(conn, user, f["assignee_id"]):
            raise PermissionError("只能处理本单位的事项")
        if not in_scope(conn, user, target["id"]):
            raise ValueError("只能派给本单位人员")
    conn.execute("UPDATE findings SET assignee_id=?, updated_at=? WHERE id=?", (assignee_id, now_iso(), finding_id))
    audit(conn, user["id"], "finding.assign", f"finding:{finding_id}", f"-> user:{assignee_id}")
    if assignee_id != user["id"]:
        _notify(conn, user["project_id"], assignee_id, f"新派单：{f['title'][:40]}", f"{user['name']}（{R.role_name(user['role'])}）把这项事项派给了你，期限 {f['due_date'] or '未设'}。",
                finding_link(finding_id))
    return findings.view(one(conn, "SELECT * FROM findings WHERE id=?", (finding_id,)),
                         {u["id"]: u["name"] for u in all_(conn, "SELECT id, name FROM users WHERE project_id=?", (user["project_id"],))})


def nudge_finding(conn, finding_id: int, user: dict, text: str = "") -> dict:
    f = one(conn, "SELECT * FROM findings WHERE id=? AND project_id=?", (finding_id, user["project_id"]))
    if not f:
        raise ValueError("记录不存在")
    if not R.can(user["role"], "nudge"):
        raise PermissionError("当前角色不能催办")
    if findings.is_candidate(f):
        raise ValueError("这是 AI 候选隐患，确认前不催办")
    if not f["assignee_id"]:
        raise ValueError("这项还没有责任人，请先派单")
    if f["status"] not in ACTIVE:
        raise ValueError("该事项已处理完毕")
    _notify(conn, user["project_id"], f["assignee_id"], f"催办：{f['title'][:40]}",
            f"{user['name']}（{R.role_name(user['role'])}）催办：{text.strip() or '请尽快处理'}。期限 {f['due_date'] or '未设'}。", finding_link(finding_id))
    audit(conn, user["id"], "finding.nudge", f"finding:{finding_id}", text[:100])
    return {"ok": True}


def nudge_user(conn, user: dict, target_id: int, text: str) -> dict:
    if not R.can(user["role"], "nudge"):
        raise PermissionError("当前角色不能催办")
    t = one(conn, "SELECT * FROM users WHERE id=? AND project_id=? AND active=1 AND role IN ('tech','sub_safety')", (target_id, user["project_id"]))
    if not t:
        raise ValueError("人员无效")
    _notify(conn, user["project_id"], t["id"], "进度记录提醒", f"{user['name']}（{R.role_name(user['role'])}）提醒：{text.strip() or '请尽快补充今日现场记录/本周确认'}。",
            {"route": "home", "params": {}, "focus": "note"} if t["role"] == "tech" else None)  # 技术员直达首页随手记
    audit(conn, user["id"], "user.nudge", f"user:{t['id']}", text[:100])
    return {"ok": True}


# ----------------------------------------------------------------------------- 公共统计

def _day(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def _labor_machinery(conn, project_id: int, start: str, end: str) -> list[dict]:
    """每日人、机投入合计（只取日志中填写的数量；没有日志的日期不出现）。"""
    rows = all_(conn, "SELECT l.log_date, l.author_id, l.content_json FROM logs l WHERE l.project_id=? AND l.period='day' AND l.mode='ai' "
                      "AND l.log_date>=? AND l.log_date<=?", (project_id, start, end))
    by_day: dict[str, dict] = {}
    for r in rows:
        c = loads(r["content_json"], {})
        d = by_day.setdefault(r["log_date"], {"date": r["log_date"], "labor": 0, "machinery": 0, "down": 0, "logs": 0})
        d["logs"] += 1
        d["labor"] += sum(int(x.get("count") or 0) for x in c.get("labor", []))
        for m in c.get("machinery", []):
            n = int(m.get("count") or 0)
            d["machinery"] += n
            if any(k in str(m.get("status") or "") for k in ("故障", "停", "维修")):
                d["down"] += n
    return [by_day[k] for k in sorted(by_day)]


def company_rollup(conn, project_id: int, day: date | None = None) -> list[dict]:
    """各分包单位一览：今日记录、本周确认、安全/质量待办、受影响预警、今日投入人数。"""
    day = day or date.today()
    iso = day.isoformat()
    members = _members(conn, project_id)
    groups: dict[str, list[dict]] = {}
    for m in members:
        groups.setdefault(company_label(m["company"]), []).append(m)
    ws = (day - timedelta(days=day.weekday())).isoformat()
    acts = {a["code"]: a for a in p6.current_activities(conn, project_id)}
    alerts = all_(conn, "SELECT activity_code, level FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' AND level IN ('red','orange')", (project_id,))
    finds = all_(conn, "SELECT f.agent, f.status, f.level, f.due_date, f.assignee_id FROM findings f WHERE f.project_id=? AND f.status IN ('open','rectifying','rejected') "
                       "AND f.kind<>'qc_reminder' AND f.confirmed_at IS NOT NULL", (project_id,))
    out = []
    for name, ms in groups.items():
        ids = {m["id"] for m in ms}
        techs = [m for m in ms if m["role"] == "tech"]
        done = 0
        wk = 0
        people = 0
        for t in techs:
            has = one(conn, "SELECT 1 FROM notes WHERE author_id=? AND note_date=? UNION SELECT 1 FROM logs WHERE author_id=? AND log_date=? AND period='day' AND mode='ai' "
                      "AND (status IN ('submitted','approved') OR auto=1)",
                      (t["id"], iso, t["id"], iso))
            done += 1 if has else 0
            if one(conn, "SELECT 1 FROM logs WHERE author_id=? AND period='week' AND log_date=? AND status IN ('submitted','approved')", (t["id"], ws)):
                wk += 1
            lg = one(conn, "SELECT content_json FROM logs WHERE author_id=? AND log_date=? AND period='day' AND mode='ai'", (t["id"], iso))
            if lg:
                people += sum(int(x.get("count") or 0) for x in loads(lg["content_json"], {}).get("labor", []))
        mine = [f for f in finds if f["assignee_id"] in ids]
        saf = [f for f in mine if f["agent"] == "safety"]
        qua = [f for f in mine if f["agent"] == "quality"]
        red = [a for a in alerts if name in _responsible(acts, ms, a["activity_code"])]
        out.append({"company": name, "members": len(ms), "techs": len(techs), "record_today": done, "week_confirmed": wk, "people_today": people,
                    "safety_open": len(saf), "safety_high": sum(1 for f in saf if f["level"] == "high"),
                    "safety_overdue": sum(1 for f in saf if f["status"] != "rectifying" and f["due_date"] and f["due_date"] < iso),
                    "quality_open": len(qua), "alerts": len(red), "alerts_red": sum(1 for a in red if a["level"] == "red")})
    out.sort(key=lambda x: (-x["safety_overdue"], -x["alerts_red"], x["company"]))
    return out


# ----------------------------------------------------------------------------- 安全员首页（分包）

RISK_HINTS = (("吊装", "起重吊装"), ("起重", "起重吊装"), ("吊", "起重吊装"), ("基坑", "基坑边坡"), ("开挖", "基坑边坡"), ("边坡", "基坑边坡"),
              ("深基", "基坑边坡"), ("模板", "脚手架模板"), ("脚手架", "脚手架模板"), ("支架", "脚手架模板"), ("高处", "高处作业"), ("桥墩", "高处作业"),
              ("盖梁", "高处作业"), ("桩", "机械车辆"), ("焊", "消防动火"), ("动火", "消防动火"), ("用电", "临时用电"), ("爆破", "爆破作业"),
              ("临边", "临边洞口"), ("洞口", "临边洞口"), ("运输", "机械车辆"), ("填筑", "机械车辆"), ("碾压", "机械车辆"))


def risk_tags(name: str) -> list[str]:
    seen: list[str] = []
    for kw, tag in RISK_HINTS:
        if kw in name and tag not in seen:
            seen.append(tag)
    return seen[:3]


PATROL_MAX = 5          # 今日重点作业巡查最多列几项
PATROL_QUIET_DAYS = 14  # 计划完成日已过、P6 无实际完成：近这么多天没有任何日报进度，视为已停或已完，不列


def _part(act: dict) -> str:
    """部位：取 WBS 路径最末一级（如“路面工程 / K0-K2” → K0-K2）。"""
    segs = [x.strip() for x in (act.get("wbs_path") or "").replace("／", "/").split("/") if x.strip()]
    return segs[-1] if segs else ""


def _log_stats(conn, project_id: int, since: str) -> tuple[dict[str, float], set[str]]:
    """日志里的进度：各作业累计完成率最大值，及 since 之后有日报进度的作业（只计已提交/已审核、未作废的记录）。"""
    rows = all_(conn, """SELECT lp.activity_code code, MAX(lp.pct) m, MAX(lp.log_date) last FROM log_progress lp JOIN logs l ON l.id=lp.log_id
                         WHERE lp.project_id=? AND l.status IN ('submitted','approved') AND lp.voided_at IS NULL GROUP BY lp.activity_code""",
                (project_id,))
    return ({r["code"]: r["m"] for r in rows if r["m"] is not None}, {r["code"] for r in rows if (r["last"] or "") >= since})


def patrol_focus(acts: list[dict], day: str, scope: str, logmax: dict[str, float], recent: set[str], limit: int = PATROL_MAX) -> list[dict]:
    """安全员今日重点作业巡查：从 P6 当天窗口里挑真正在干的作业。
    剔除：日志累计完成率已 ≥100%（P6 未更新实际完成）；计划完成日已过、P6 无实际完成且近 PATROL_QUIET_DAYS 天没有日报进度。
    排序：近期有日报进度 → P6 已开工 → 计划覆盖今天 → 即将开工，同档关键线路优先。只给名称与部位，不显示作业编码。"""
    target = date.fromisoformat(day)
    rows = []
    for a in p6.window_activities(acts, day, scope, limit=max(len(acts), 1)):
        code = a["code"]
        if a.get("is_milestone") or (logmax.get(code) or 0) >= 100:
            continue
        ef = date.fromisoformat(a["early_finish"][:10]) if a.get("early_finish") else None
        es = date.fromisoformat(a["early_start"][:10]) if a.get("early_start") else None
        stale = bool(ef and ef < target and code not in recent)
        if stale and not a.get("act_start"):
            continue   # 计划早该完成、P6 没开工记录、近期也无日报：不是在干的作业
        # P6 已开工但久无日报（可能正延误、只是没做周确认）：不剔除，排在最后
        rank = 4 if stale else 0 if code in recent else 1 if a.get("act_start") else 2 if es and es <= target else 3
        rows.append((rank, 0 if a.get("is_critical") else 1, a.get("early_start") or "9999", a))
    rows.sort(key=lambda r: r[:3])
    out, seen = [], set()
    for _, _, _, a in rows[:limit]:
        part = _part(a)
        out.append({"code": a["code"], "part": part or "—", "name": a["name"], "critical": bool(a.get("is_critical")), "tags": risk_tags(a["name"]),
                    "start": a.get("early_start"), "finish": a.get("early_finish"), "active": bool(a.get("act_start")) or a["code"] in recent})
    return out


def safety_home(conn, user: dict) -> dict:
    pid, today = user["project_id"], date.today()
    iso = today.isoformat()
    ids = scope_ids(conn, user) or [user["id"]]
    mark = ",".join("?" for _ in ids)
    rows = all_(conn, f"SELECT * FROM findings WHERE project_id=? AND agent='safety' AND confirmed_at IS NOT NULL AND assignee_id IN ({mark})", (pid, *ids))
    cands = all_(conn, f"SELECT * FROM findings WHERE project_id=? AND agent='safety' AND confirmed_at IS NULL AND status='open' AND assignee_id IN ({mark}) "
                       "ORDER BY CASE level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id DESC", (pid, *ids))
    users = {u["id"]: u["name"] for u in all_(conn, "SELECT id, name FROM users WHERE project_id=?", (pid,))}
    active = [f for f in rows if f["status"] in ACTIVE]
    wk0 = (today - timedelta(days=today.weekday())).isoformat()
    due = [findings.view(f, users) for f in active]
    due.sort(key=lambda f: (0 if f["overdue"] else 1, f["due_date"] or "9999", {"high": 0, "medium": 1, "low": 2}.get(f["level"], 3)))
    snaps = all_(conn, f"SELECT safety_status FROM media WHERE project_id=? AND purpose='safety' AND substr(uploaded_at,1,10)=? AND uploader_id IN ({mark})", (pid, iso, *ids))
    imp = p6.current_import(conn, pid)
    focus = []
    if imp:
        members = [m for m in _members(conn, pid) if m["id"] in ids]
        scopes = ",".join(m["wbs_scope"] for m in members if m["wbs_scope"])
        logmax, recent = _log_stats(conn, pid, (today - timedelta(days=PATROL_QUIET_DAYS)).isoformat())
        focus = patrol_focus(p6.current_activities(conn, pid), iso, scopes, logmax, recent)
    return {"company": company_label(user.get("company")), "today": iso,
            "counts": {"pending": sum(1 for f in active if f["status"] in ("open", "rejected")), "rectifying": sum(1 for f in active if f["status"] == "rectifying"),
                       "overdue": sum(1 for f in due if f["overdue"]), "high": sum(1 for f in active if f["level"] == "high"),
                       "resolved_week": sum(1 for f in rows if f["status"] == "resolved" and (f["resolved_at"] or "")[:10] >= wk0),
                       "new_week": sum(1 for f in rows if f["created_at"][:10] >= wk0)},
            "snaps_today": {"photos": len(snaps), "hazards": sum(1 for s in snaps if s["safety_status"] == "hazard"),
                            "pending": sum(1 for s in snaps if s["safety_status"] == "pending")},
            "due": due[:20], "focus": focus,
            # AI 候选隐患：待本单位安全员确认，不计入上面的整改数、超期数
            "candidates": [findings.view(f, users) for f in cands[:20]], "candidate_count": len(cands),
            "team": [{"id": m["id"], "name": m["name"], "role": m["role"], "role_name": R.role_name(m["role"])} for m in _members(conn, pid) if m["id"] in ids]}


# ----------------------------------------------------------------------------- 安全主管

def safety_overview(conn, project_id: int) -> dict:
    today = date.today()
    iso = today.isoformat()
    users = {u["id"]: u for u in all_(conn, "SELECT id, name, company, role FROM users WHERE project_id=?", (project_id,))}
    rows = all_(conn, "SELECT * FROM findings WHERE project_id=? AND agent='safety' AND confirmed_at IS NOT NULL", (project_id,))
    cand_rows = all_(conn, "SELECT * FROM findings WHERE project_id=? AND agent='safety' AND confirmed_at IS NULL AND status='open' "
                           "ORDER BY CASE level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id DESC", (project_id,))
    d30 = (today - timedelta(days=30)).isoformat()
    d6 = (today - timedelta(days=6)).isoformat()

    def comp(f):
        u = users.get(f["assignee_id"])
        return company_label(u["company"]) if u and R.is_sub(u["role"]) else ("港湾" if u else "未指派")

    def days_open(f):
        a, b = _day(f["created_at"]), _day(f["resolved_at"])
        return (b - a).days if a and b else None

    active = [f for f in rows if f["status"] in ACTIVE]
    overdue = [f for f in active if f["status"] != "rectifying" and f["due_date"] and f["due_date"] < iso]
    last30 = [f for f in rows if f["created_at"][:10] >= d30]
    closed30 = [f for f in rows if f["status"] == "resolved" and (f["resolved_at"] or "")[:10] >= d30]
    times = [x for x in (days_open(f) for f in closed30) if x is not None]
    trend = []
    for i in range(13, -1, -1):
        dd = (today - timedelta(days=i)).isoformat()
        trend.append({"date": dd, "new": sum(1 for f in rows if f["created_at"][:10] == dd), "resolved": sum(1 for f in rows if (f["resolved_at"] or "")[:10] == dd)})
    groups: dict[str, dict] = {}
    for f in rows:
        g = groups.setdefault(comp(f), {"company": comp(f), "open": 0, "overdue": 0, "high": 0, "rectifying": 0, "new_30d": 0, "closed_30d": 0, "_t": []})
        if f in active:
            g["open"] += 1
            g["high"] += 1 if f["level"] == "high" else 0
            g["rectifying"] += 1 if f["status"] == "rectifying" else 0
        if f in overdue:
            g["overdue"] += 1
        if f["created_at"][:10] >= d30:
            g["new_30d"] += 1
        if f in closed30:
            g["closed_30d"] += 1
            t = days_open(f)
            if t is not None:
                g["_t"].append(t)
    by_company = []
    for g in groups.values():
        t = g.pop("_t")
        g["avg_days"] = round(sum(t) / len(t), 1) if t else None
        g["close_rate"] = round(g["closed_30d"] / g["new_30d"], 3) if g["new_30d"] else None
        by_company.append(g)
    by_company.sort(key=lambda g: (-g["overdue"], -g["open"], g["company"]))
    cats = Counter(loads(f["body_json"], {}).get("category") or "其他" for f in active)
    fv = lambda f: findings.view(f, {k: v["name"] for k, v in users.items()}) | {"company": comp(f)}  # noqa: E731
    review = [fv(f) for f in active if f["status"] in ("rectifying", "rejected")]
    od = []
    for f in sorted(overdue, key=lambda f: f["due_date"]):
        v = fv(f)
        v["days_over"] = (today - date.fromisoformat(f["due_date"])).days
        od.append(v)
    return {"kpis": {"open": len(active), "high": sum(1 for f in active if f["level"] == "high"), "overdue": len(overdue),
                     "review": sum(1 for f in active if f["status"] == "rectifying"),
                     "new_7d": sum(1 for f in rows if f["created_at"][:10] >= d6), "resolved_7d": sum(1 for f in rows if (f["resolved_at"] or "")[:10] >= d6),
                     "close_rate_30d": round(len(closed30) / len(last30), 3) if last30 else None,
                     "avg_close_days": round(sum(times) / len(times), 1) if times else None,
                     "unassigned": sum(1 for f in active if not f["assignee_id"]),
                     "ai_found": sum(1 for f in rows if f["kind"] == "hazard"), "total": len(rows)},
            "trend": trend, "by_company": by_company,
            "by_category": [{"name": k, "n": v} for k, v in cats.most_common()],
            "review": review[:20], "overdue": od[:20],
            "candidates": [fv(f) for f in cand_rows[:20]], "candidate_count": len(cand_rows),  # 待确认候选：不计入上面任何台账数字
            "assignees": [{"id": u["id"], "name": u["name"], "role_name": R.role_name(u["role"]), "company": company_label(u["company"]) if R.is_sub(u["role"]) else "港湾"}
                          for u in users.values() if u["role"] not in ("admin", "leader")]}


# ----------------------------------------------------------------------------- 工程主管

def eng_overview(conn, project_id: int) -> dict:
    today = date.today()
    iso = today.isoformat()
    imp = p6.current_import(conn, project_id)
    cmap = _company_map(conn, project_id)
    subs = all_(conn, """SELECT l.id, l.log_date, l.author_id, l.submitted_at, l.dq_json, l.checks_json, u.name author FROM logs l JOIN users u ON u.id=l.author_id
                         WHERE l.project_id=? AND l.status='submitted' AND l.mode='ai' AND l.period='day' ORDER BY l.log_date DESC, l.id DESC LIMIT 60""", (project_id,))
    risk = Counter()
    review, low_ids = [], []
    for l in subs:
        dq = loads(l["dq_json"])
        r = dq["risk"] if dq else "none"
        risk[r] += 1
        if r == "low":
            low_ids.append(l["id"])
        review.append({"id": l["id"], "date": l["log_date"], "author": l["author"], "company": cmap.get(l["author_id"], NO_COMPANY), "risk": r,
                       "score": dq["score"] if dq else None, "summary": dq["summary"] if dq else "", "flags": sum(1 for x in loads(l["checks_json"], []) if x["level"] in ("error", "warn"))})
    review.sort(key=lambda x: {"high": 0, "medium": 1, "none": 2, "low": 3}.get(x["risk"], 4))
    miss = [{"id": m["id"], "name": m["name"], "company": cmap.get(m["id"], NO_COMPANY)} for m in missing_logs(conn, project_id, iso)]
    lws = (today - timedelta(days=today.weekday() + 7)).isoformat()
    unconfirmed = all_(conn, """SELECT u.id, u.name FROM users u WHERE u.project_id=? AND u.role='tech' AND u.active=1 AND NOT EXISTS
                                (SELECT 1 FROM logs l WHERE l.author_id=u.id AND l.period='week' AND l.log_date=? AND l.status IN ('submitted','approved'))""", (project_id, lws))
    members = _members(conn, project_id)
    acts = {a["code"]: a for a in p6.current_activities(conn, project_id)} if imp else {}
    slip, gaps = [], []
    if imp:
        ev = prog_svc.evaluate(conn, project_id)
        gaps = prog_svc.data_gaps(conn, project_id, ev=ev)
        for r in ev["results"][:8]:
            a = r["activity"]
            slip.append({"code": a["code"], "name": a["name"], "level": r["level"], "rule": prog_svc.RULE_TITLES.get(r["rule"], r["rule"]), "critical": bool(a["is_critical"]),
                         "slip": r["facts"].get("slip_days"), "float": r["facts"].get("total_float_d"), "companies": _responsible(acts, members, a["code"])})
    qrows = findings.list_for(conn, project_id, agent="quality", status="active", limit=100)
    qissues = [f for f in qrows if f["kind"] != "qc_reminder"]
    unmapped = one(conn, """SELECT COUNT(DISTINCT lp.activity_code) n FROM log_progress lp WHERE lp.project_id=? AND lp.voided_at IS NULL AND NOT EXISTS
                            (SELECT 1 FROM boq_map m WHERE m.project_id=lp.project_id AND m.activity_code=lp.activity_code AND m.status='confirmed')""", (project_id,))["n"]
    maps = {r["status"]: r["n"] for r in all_(conn, "SELECT status, COUNT(*) n FROM boq_map WHERE project_id=? GROUP BY status", (project_id,))}
    return {"p6": {"file": imp["file_name"], "data_date": imp["data_date"], "activities": imp["activity_count"], "stale_days": (today - date.fromisoformat(imp["data_date"])).days} if imp else None,
            "review": {"pending": len(subs), "risk": {"high": risk["high"], "medium": risk["medium"], "low": risk["low"], "none": risk["none"]}, "low_ids": low_ids, "items": review[:10]},
            "missing_today": miss, "week_unconfirmed": [{"id": u["id"], "name": u["name"], "company": cmap.get(u["id"], NO_COMPANY)} for u in unconfirmed],
            "slip": slip,
            # 数据缺口：关键作业连续多日没有日报进度。是“没报”不是“落后”，不计入进度预警；只在工程主管工作台单列
            "gaps": gaps[:30], "gaps_total": len(gaps),
            "quality": {"open": len(qissues), "overdue": sum(1 for f in qissues if f["overdue"]), "reminders": len(qrows) - len(qissues),
                        "items": [{"id": f["id"], "title": f["title"], "level": f["level"], "level_name": f["level_name"], "status_name": f["status_name"], "due_date": f["due_date"],
                                   "overdue": f["overdue"], "assignee": f["assignee"]} for f in qissues[:8]]},
            "mapping": {"confirmed": maps.get("confirmed", 0), "suggested": maps.get("suggested", 0), "unmapped_progress": unmapped}}


# ----------------------------------------------------------------------------- 生产经理

def production_overview(conn, project_id: int) -> dict:
    from . import brief as brief_svc  # 延迟导入，避免循环
    today = date.today()
    iso = today.isoformat()
    k = brief_svc.kpis(conn, project_id, today)
    trend = _labor_machinery(conn, project_id, (today - timedelta(days=13)).isoformat(), iso)
    cmap = _company_map(conn, project_id)
    since = (today - timedelta(days=6)).isoformat()
    kinds = ("材料", "设备", "图纸", "外部", "天气", "人员")
    blockers, cnt = [], Counter()
    for r in all_(conn, """SELECT l.id, l.log_date, l.author_id, l.content_json, u.name author FROM logs l JOIN users u ON u.id=l.author_id
                           WHERE l.project_id=? AND l.period='day' AND l.mode='ai' AND l.log_date>=? ORDER BY l.log_date DESC""", (project_id, since)):
        for i in loads(r["content_json"], {}).get("issues", []):
            if i.get("type") in kinds:
                cnt[i["type"]] += 1
                blockers.append({"log_id": r["id"], "date": r["log_date"], "author": r["author"], "company": cmap.get(r["author_id"], NO_COMPANY), "type": i["type"],
                                 "severity": i.get("severity"), "desc": i.get("desc"), "measure": i.get("measure"), "activity_code": i.get("activity_code")})
    blockers.sort(key=lambda b: b["date"], reverse=True)
    blockers.sort(key=lambda b: {"高": 0, "中": 1, "低": 2}.get(b["severity"], 3))
    members = _members(conn, project_id)
    acts = {a["code"]: a for a in p6.current_activities(conn, project_id)}
    critical = []
    for a in all_(conn, "SELECT * FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' AND level IN ('red','orange') ORDER BY level DESC, id DESC LIMIT 8", (project_id,)):
        d = loads(a["detail_json"], {})
        critical.append({"id": a["id"], "level": a["level"], "title": a["title"], "code": a["activity_code"], "slip": d.get("slip_days"), "float": d.get("total_float_d"),
                         "forecast": d.get("forecast_finish"), "companies": _responsible(acts, members, a["activity_code"])})
    return {"kpis": k, "trend": trend, "companies": company_rollup(conn, project_id, today), "critical": critical,
            "blockers": {"counts": [{"type": t, "n": cnt[t]} for t in kinds if cnt[t]], "items": blockers[:12], "days": 7}}


# ----------------------------------------------------------------------------- 系统管理员

def _dir_size(path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def scheduler_status() -> dict:
    """后台调度（每日判定、晚间整理、自动催办）是否开启。技术细节（环境变量）只在系统管理员的系统概况里出现。"""
    on = os.environ.get("ZB_SCHEDULER", "1") != "0"
    return {"on": on, "detail": "已开启" if on else "未开启：服务以环境变量 ZB_SCHEDULER=0 启动，每日判定、晚间整理与自动催办只能手动运行"}


def admin_overview(conn, user: dict) -> dict:
    pid = user["project_id"]
    today = date.today()
    users = all_(conn, "SELECT id, name, role, active, company FROM users WHERE project_id=?", (pid,))
    last = {r["user_id"]: r["last"] for r in all_(conn, "SELECT user_id, MAX(created_at) last FROM audit WHERE action IN ('login','dev_login') GROUP BY user_id")}
    by_role = []
    for role in R.ROLE_ORDER:
        us = [u for u in users if u["role"] == role]
        if us or role != "leader":
            by_role.append({"role": role, "name": R.role_name(role), "org": R.ROLES[role]["org"], "active": sum(1 for u in us if u["active"]), "inactive": sum(1 for u in us if not u["active"])})
    never = [u["name"] for u in users if u["active"] and u["id"] not in last]
    since7 = (datetime.now().astimezone() - timedelta(days=7)).isoformat(timespec="seconds")
    runs = one(conn, "SELECT COUNT(*) n, SUM(ok) ok, AVG(latency_ms) lat FROM ai_runs WHERE project_id=? AND created_at>=?", (pid, since7))
    fails = all_(conn, "SELECT kind, error, created_at FROM ai_runs WHERE project_id=? AND ok=0 ORDER BY id DESC LIMIT 5", (pid,))
    ev = {r["kind"]: r["last"] for r in all_(conn, "SELECT kind, MAX(created_at) last FROM events WHERE project_id=? AND kind IN ('detect.daily','evening') GROUP BY kind", (pid,))}
    db_size = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    imp = p6.current_import(conn, pid)
    logs = {r["status"]: r["n"] for r in all_(conn, "SELECT status, COUNT(*) n FROM logs WHERE project_id=? GROUP BY status", (pid,))}
    projects = all_(conn, "SELECT p.id, p.name, p.code, p.config_json, (SELECT COUNT(*) FROM users u WHERE u.project_id=p.id AND u.active=1) users FROM projects p ORDER BY p.id")
    audits = audit_names(conn, all_(conn, "SELECT a.id, a.action, a.target, a.detail, a.created_at, u.name FROM audit a LEFT JOIN users u ON u.id=a.user_id "
                                          f"WHERE a.action NOT IN ({','.join('?' for _ in DEV_AUDIT)}) ORDER BY a.created_at DESC, a.id DESC LIMIT 12", DEV_AUDIT))
    return {"users": {"total": len(users), "active": sum(1 for u in users if u["active"]), "by_role": by_role, "never_logged": never,
                      "companies": sorted({company_label(u["company"]) for u in users if R.is_sub(u["role"])})},
            "ai": {**provider_status(), "runs_7d": runs["n"] or 0, "failed_7d": (runs["n"] or 0) - int(runs["ok"] or 0), "avg_latency_s": round((runs["lat"] or 0) / 1000, 1) if runs["n"] else None,
                   "recent_failures": fails},
            "system": {"db_mb": round(db_size / 1048576, 1), "media_count": one(conn, "SELECT COUNT(*) n FROM media")["n"], "media_mb": round(_dir_size(settings.media_dir) / 1048576, 1),
                       "last_detect": ev.get("detect.daily"), "last_evening": ev.get("evening"),
                       "scheduler": scheduler_status(),
                       "p6": {"file": imp["file_name"], "data_date": imp["data_date"], "stale_days": (today - date.fromisoformat(imp["data_date"])).days} if imp else None,
                       "logs": logs, "unread_notifications": one(conn, "SELECT COUNT(*) n FROM notifications WHERE read_at IS NULL")["n"]},
            "projects": [{"id": p["id"], "name": p["name"], "code": p["code"], "users": p["users"], "simulation": bool(loads(p["config_json"], {}).get("simulation")), "current": p["id"] == pid} for p in projects],
            "audit": audits, "last_login": {str(k): v for k, v in last.items()}}


DEV_AUDIT = ("dev_login",)  # 开发态记录：审计列表与系统概况默认不显示（仍计入“最近登录”）

# 审计里的操作与对象一律写中文（U-27）：操作按全名查，查不到按前缀归类；对象 user:16、finding:106 换成人名、事项标题
AUDIT_NAME = {
    "login": "登录", "login.fail": "登录失败", "dev_login": "开发态登录", "user.create": "新增账号", "user.update": "修改账号", "user.nudge": "提醒人员",
    "project.update": "修改项目设置", "qr.base": "修改扫码网址",
    "finding.confirm": "确认并派单", "finding.confirm_edit": "修改后确认并派单", "finding.assign": "派单", "finding.nudge": "催办",
    "finding.resolved": "确认闭环", "finding.open": "退回重改", "finding.dismissed": "忽略/不采纳", "finding.rectifying": "提交整改",
    "finding.rejected": "复核未通过", "finding.reverify": "重新让 AI 复核",
    "log.submit": "提交日志", "log.approve": "审核通过日志", "log.return": "退回日志", "report.draft": "保存报表", "report.published": "发布报表",
    "alert.feedback": "预警反馈", "ai.config": "调整 AI 模型与限额", "ai.key.add": "新增 AI Key", "ai.key.update": "修改 AI Key",
    "ai.key.delete": "删除 AI Key", "ai.key.test": "测试 AI Key", "ai.rebalance": "重新分配 AI Key",
    "nudge.auto": "自动催办", "nudge.run": "手动运行催办", "nudge.rules": "修改催办规则", "cmd.assign": "指令派单", "cmd.nudge": "指令催办",
    "meeting.save": "保存例会纪要", "meeting.item": "确认例会行动项", "meeting.done": "完成例会行动项", "meeting.reopen": "重开例会行动项",
    "gate.state": "更新放行条件", "gate.notify": "发放行预警", "gate.assign": "指定放行条件责任人", "gate.custom_add": "新增放行条件", "gate.custom_del": "删除放行条件",
    "ticket.upload": "上传单据", "ticket.confirm": "确认单据入账", "ticket.void": "作废单据", "ticket.assign": "指派单据", "tickets.rules": "修改单据规则",
    "specimen.add": "新增试块", "specimen.result": "录入试块结果", "specimen.delete": "删除试块", "batch.retest": "复检",
    "pour.create": "新建浇筑记录", "pour.update": "修改浇筑记录", "pour.delete": "删除浇筑记录",
    "conceal.create": "新建隐蔽验收", "conceal.edit": "修改隐蔽验收", "conceal.fields": "补填验收数据", "conceal.photo": "隐蔽验收拍照",
    "conceal.analyze": "AI 核对验收资料", "conceal.ready": "验收资料齐备", "conceal.submit": "报验", "conceal.result": "录入监理结果",
    "conceal.reopen": "重开隐蔽验收", "conceal.delete": "删除隐蔽验收", "conceal.export": "导出验收记录", "conceal.template": "修改验收模板",
    "quick.qr.create": "生成扫码二维码", "quick.qr.revoke": "作废二维码", "quick.claim": "认领扫码上报", "quick.invalid": "扫码上报判为无效",
    "note.classify": "随手记归类", "site.summary": "生成部位小结", "site.organize": "归并部位", "site.export": "导出部位档案",
    "claim.generate": "生成索赔证据包", "claim.export": "导出索赔证据包", "circle.pick": "现场圈精选", "circle.notice.publish": "发布公告",
    "circle.notice.withdraw": "撤回公告", "circle.notice.pin": "置顶公告", "feed.comment.delete": "删除评论",
    "output.records": "录入产值", "boq.import": "导入工程量清单", "ask.pin": "固定提问", "ask.unpin": "取消固定",
}
AUDIT_GROUP = {"user": "账号", "project": "项目", "finding": "事项", "log": "日志", "report": "报表", "alert": "预警", "ai": "AI 设置", "nudge": "催办",
               "cmd": "指令", "meeting": "例会", "gate": "放行", "ticket": "单据", "tickets": "单据", "specimen": "试块", "batch": "试块", "pour": "浇筑",
               "conceal": "隐蔽验收", "quick": "现场扫码", "qr": "现场扫码", "note": "随手记", "site": "部位档案", "claim": "索赔", "circle": "现场圈",
               "feed": "现场圈", "output": "产值", "boq": "工程量清单", "boq_map": "清单映射", "ask": "提问"}


def audit_names(conn, rows: list[dict]) -> list[dict]:
    """给审计记录补上 action_name、target_name（中文）。"""
    users, finds = {}, {}
    for r in rows:
        a = r["action"] or ""
        group = AUDIT_GROUP.get(a.split(".")[0])
        r["action_name"] = AUDIT_NAME.get(a) or (f"{group}操作" if group else "其他操作")
        kind, _, rest = (r["target"] or "").partition(":")
        rid = rest.split(":")[0]
        name = ""
        if kind == "user" and rid.isdigit():
            if rid not in users:
                u = one(conn, "SELECT name FROM users WHERE id=?", (int(rid),))
                users[rid] = u["name"] if u else ""
            name = f"账号 {users[rid]}" if users[rid] else "账号（已删除）"
        elif kind == "finding" and rid.isdigit():
            if rid not in finds:
                f = one(conn, "SELECT title FROM findings WHERE id=?", (int(rid),))
                finds[rid] = f["title"][:20] if f else ""
            name = f"事项 #{rid} {finds[rid]}".strip()
        elif kind == "gate" and rid:
            name = f"放行 {rid}"
        elif kind and rid:
            name = f"{AUDIT_GROUP.get(kind, '对象')} #{rid}" if rid.isdigit() else f"{AUDIT_GROUP.get(kind, '对象')} {rid}"
        r["target_name"] = name or (r["target"] if r["target"] and ":" not in r["target"] else "")
    return rows


def audit_list(conn, limit: int = 100, action: str = "", dev: bool = False) -> list[dict]:
    q = "SELECT a.id, a.action, a.target, a.detail, a.created_at, u.name FROM audit a LEFT JOIN users u ON u.id=a.user_id"
    where: list[str] = []
    p: list = []
    if action:
        where.append("a.action LIKE ?")
        p.append(action + "%")
    if not dev:
        where.append(f"a.action NOT IN ({','.join('?' for _ in DEV_AUDIT)})")
        p += DEV_AUDIT
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY a.created_at DESC, a.id DESC LIMIT ?"
    p.append(min(max(limit, 1), 500))
    return audit_names(conn, all_(conn, q, p))
