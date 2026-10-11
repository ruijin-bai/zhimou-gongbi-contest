"""分包综合履约画像（2026-10-08 增补能力 9）：每家分包一张卡，近 30 天的安全、质量、进度、联系单与（只给工程主管的）日志按时情况。

- 数字全部由程序从系统已确认数据算出：已确认的安全隐患 / 质量问题（派给该单位账号的）、闭环数与平均闭环天数、超期未闭环；
  该单位技术员负责范围内作业的红橙进度预警（不含数据缺口）；致该单位的联系单与 24 小时内签收率；
- 日志按时率（周确认在周末次日前提交的比例）只给工程主管：项目经理口径不看日志填报（AGENTS.md），给项目经理的事实底稿与 AI 评价里都不含；
- 交融只写一段评价（≤100 字）：先说做得好的，再说要盯的一项；数字守卫，不过或模型不可用用规则句；同一份统计当天只调用一次；
- 不排名、不打分、不写罚款，只供管理参考。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta

from .. import roles as R
from ..ai.client import AIError, call_json
from ..db import all_, now_iso, one
from . import p6

PROMPT_VERSION = "subprofile-v1-20261008"
ROLES = frozenset({"eng_sup", "safety_sup", "manager"})
DAYS = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS subprofile_cache (
    key TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL,
    company TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
"""


def _days(a: str | None, b: str | None) -> float | None:
    try:
        return max(0.0, (datetime.fromisoformat(b[:19]) - datetime.fromisoformat(a[:19])).total_seconds() / 86400)
    except (TypeError, ValueError):
        return None


def _avg(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def _pct(n: int, d: int) -> float | None:
    return round(100 * n / d) if d else None


def companies(conn, pid: int) -> list[str]:
    return sorted({u["company"] for u in all_(conn, "SELECT company FROM users WHERE project_id=? AND active=1 AND role IN ('tech','sub_safety') AND company<>''", (pid,))})


def _find_stats(conn, pid: int, company: str, agent: str, since: str, today: str) -> dict:
    rows = all_(conn, """SELECT f.* FROM findings f JOIN users u ON u.id=f.assignee_id WHERE f.project_id=? AND f.agent=? AND f.kind<>'qc_reminder'
                         AND f.confirmed_at IS NOT NULL AND u.company=? AND substr(f.created_at,1,10)>=?""", (pid, agent, company, since))
    closed = [f for f in rows if f["status"] == "resolved"]
    open_ = [f for f in rows if f["status"] in ("open", "rectifying", "rejected")]
    return {"n": len(rows), "high": sum(1 for f in rows if f["level"] == "high"), "closed": len(closed),
            "avg_days": _avg(_days(f["confirmed_at"] or f["created_at"], f["resolved_at"]) for f in closed),
            "open": len(open_), "overdue": sum(1 for f in open_ if f["due_date"] and f["due_date"][:10] < today)}


def profile(conn, pid: int, company: str, with_logs: bool, today: date | None = None) -> dict:
    today = today or date.today()
    t, since = today.isoformat(), (today - timedelta(days=DAYS)).isoformat()
    techs = all_(conn, "SELECT id, wbs_scope FROM users WHERE project_id=? AND active=1 AND role='tech' AND company=?", (pid, company))
    acts = [a for a in p6.current_activities(conn, pid) if any((u["wbs_scope"] or "").strip() and p6.in_scope(a, u["wbs_scope"]) for u in techs)]
    codes = {a["code"] for a in acts}
    al = [a for a in all_(conn, "SELECT * FROM alerts WHERE project_id=? AND rule<>'stale' AND level IN ('red','orange') AND status IN ('open','confirmed') AND last_detected>=?",
                          (pid, since)) if a["activity_code"] in codes]
    prog = {"acts": len(acts), "alerts": len(al), "red": sum(1 for a in al if a["level"] == "red"), "confirmed": sum(1 for a in al if a["status"] == "confirmed")}
    letters = {"n": 0, "acked_24h": 0, "rate": None, "overdue": 0}
    if one(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='sub_letters'"):
        from . import sub_letters
        ls = all_(conn, "SELECT * FROM sub_letters WHERE project_id=? AND company=? AND substr(issued_at,1,10)>=?", (pid, company, since))
        ok = sum(1 for x in ls if x["acked_at"] and (_days(x["issued_at"], x["acked_at"]) or 99) <= 1)
        letters = {"n": len(ls), "acked_24h": ok, "rate": _pct(ok, len(ls)), "overdue": sum(1 for x in ls if sub_letters.state(conn, x)["overdue"])}
    out = {"company": company, "since": since, "until": t, "techs": len(techs), "safety": _find_stats(conn, pid, company, "safety", since, t),
           "quality": _find_stats(conn, pid, company, "quality", since, t), "progress": prog, "letters": letters}
    if with_logs:
        ids = [u["id"] for u in techs]
        weeks = all_(conn, f"""SELECT * FROM logs WHERE project_id=? AND mode='ai' AND period='week' AND period_end>=? AND period_end<?
                               AND author_id IN ({','.join('?' * len(ids)) or 'NULL'})""", (pid, since, t, *ids)) if ids else []
        on = sum(1 for w in weeks if w["status"] in ("submitted", "approved") and w["submitted_at"]
                 and w["submitted_at"][:10] <= (date.fromisoformat(w["period_end"]) + timedelta(days=1)).isoformat())
        out["logs"] = {"weeks": len(weeks), "on_time": on, "rate": _pct(on, len(weeks))}
    return out


def facts_text(p: dict) -> str:
    s, q, g, lt = p["safety"], p["quality"], p["progress"], p["letters"]
    L = [f"单位：{p['company']}；统计期：{p['since']} 至 {p['until']}（近 {DAYS} 天）",
         f"安全隐患 {s['n']} 项（高风险 {s['high']}），已闭环 {s['closed']} 项，平均 {s['avg_days'] if s['avg_days'] is not None else '无'} 天闭环，未闭环 {s['open']} 项，其中超期 {s['overdue']} 项",
         f"质量问题 {q['n']} 项，已闭环 {q['closed']} 项，平均 {q['avg_days'] if q['avg_days'] is not None else '无'} 天闭环，未闭环 {q['open']} 项，其中超期 {q['overdue']} 项",
         f"负责范围内作业 {g['acts']} 项，近期红橙进度预警 {g['alerts']} 条（红色 {g['red']} 条，已确认属实 {g['confirmed']} 条）",
         f"收到工作联系单 {lt['n']} 份，24 小时内签收 {lt['acked_24h']} 份" + (f"（{lt['rate']}%）" if lt["rate"] is not None else "") + f"，超期未回复 {lt['overdue']} 份"]
    if "logs" in p:
        lg = p["logs"]
        L.append(f"周确认 {lg['weeks']} 份，按时提交 {lg['on_time']} 份" + (f"（{lg['rate']}%）" if lg["rate"] is not None else ""))
    return "\n".join(L)


def rule_text(p: dict) -> str:
    s, q, g, lt = p["safety"], p["quality"], p["progress"], p["letters"]
    good, watch = [], []
    if s["n"] and s["open"] == 0:
        good.append(f"安全隐患 {s['n']} 项已全部闭环")
    if s["overdue"]:
        watch.append(f"超期未闭环的安全隐患 {s['overdue']} 项")
    if q["overdue"]:
        watch.append(f"超期未闭环的质量问题 {q['overdue']} 项")
    if g["red"]:
        watch.append(f"负责作业有红色进度预警 {g['red']} 条")
    if lt["overdue"]:
        watch.append(f"联系单超期未回复 {lt['overdue']} 份")
    if "logs" in p and p["logs"]["weeks"] and p["logs"]["on_time"] < p["logs"]["weeks"]:
        watch.append(f"周确认按时 {p['logs']['on_time']}/{p['logs']['weeks']}")
    if not s["n"] and not q["n"] and not g["alerts"] and not lt["n"]:
        return f"近 {DAYS} 天没有该单位的安全、质量、进度预警和联系单记录，暂无评价依据。"
    return ("；".join(good) + "。" if good else "") + ("需要盯：" + "、".join(watch) + "。" if watch else "近 30 天没有超期事项。")


SYSTEM = """你是施工项目部的管理助手。下面是系统统计好的某分包单位近 30 天履约数据。
请写一段不超过 100 字的中文评价：先说做得好的一项（没有就不说），再点出最需要盯的一项，最后给一句管理建议。
硬性要求：只用统计里的数字；不排名、不打分；不写罚款、扣款、处罚；不推测原因；不要 markdown。
只输出 JSON：{"text": "……"}"""
_NUM = re.compile(r"\d+(?:\.\d+)?")


def guard(text: str, ftext: str) -> list[str]:
    ok = {f"{float(x):g}" for x in _NUM.findall(ftext)}
    clean = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
    bad = [f"数字{x}无出处" for x in _NUM.findall(clean) if f"{float(x):g}" not in ok]
    if re.search("罚|扣款|处罚|排名|第一名|打分", text):
        bad.append("出现罚款/排名字样")
    return bad


def _norm(d) -> dict:
    t = str((d or {}).get("text") or "").strip() if isinstance(d, dict) else ""
    if not t:
        raise ValueError("缺少 text")
    return {"text": t[:200]}


def evaluate(conn, pid: int, p: dict, user_id: int | None) -> dict:
    ftext = facts_text(p)
    base = {"text": rule_text(p), "source": "rule", "note": ""}
    key = hashlib.sha256(json.dumps({"p": pid, "f": ftext, "d": date.today().isoformat(), "v": PROMPT_VERSION}, ensure_ascii=False).encode()).hexdigest()[:32]
    hit = one(conn, "SELECT * FROM subprofile_cache WHERE key=?", (key,))
    if hit:
        return {"text": hit["text"], "source": hit["source"], "note": hit["note"] or ""}
    try:
        res = call_json("sub.profile", system=SYSTEM, user=ftext, prompt_version=PROMPT_VERSION, mock=lambda: {"text": base["text"]}, validate=_norm,
                        project_id=pid, user_id=user_id, max_tokens=400, temperature=0.2, input_meta={"company": p["company"]})
        bad = guard(res.data["text"], ftext)
        if bad:
            base["note"] = "AI 评价未通过核对（" + "、".join(bad[:2]) + "），用系统统计句"
        elif res.provider == "mock":   # 离线模拟：标 source=mock，界面写“离线模拟模型”，不当作 AI 评价
            base.update(text=res.data["text"], source="mock", note="离线模拟模型输出，仅供开发测试")
        else:
            base.update(text=res.data["text"], source="ai")
    except AIError as exc:
        base["note"] = "AI 暂不可用，用系统统计句（" + str(exc)[:30] + "）"
    if base["source"] != "ai":   # 只缓存真实模型成功的评价，失败不缓存（RJ 2026-10-09）
        return base
    conn.execute("INSERT OR REPLACE INTO subprofile_cache(key, project_id, company, text, source, note, created_at) VALUES (?,?,?,?,?,?,?)",
                 (key, pid, p["company"], base["text"], base["source"], base["note"], now_iso()))
    return base


def listing(conn, user: dict) -> dict:
    if user["role"] not in ROLES:
        raise PermissionError("分包履约画像只给工程主管、安全主管和项目经理")
    with_logs = user["role"] == "eng_sup"     # 项目经理不看日志填报；安全主管不管日志
    pid = user["project_id"]
    items = []
    for c in companies(conn, pid):
        p = profile(conn, pid, c, with_logs)
        items.append({**p, "facts": facts_text(p), "eval": evaluate(conn, pid, p, user["id"])})
    return {"items": items, "days": DAYS, "with_logs": with_logs, "role_name": R.role_name(user["role"])}
