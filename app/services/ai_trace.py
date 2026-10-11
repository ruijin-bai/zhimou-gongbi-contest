"""AI 产出的来历、人工反馈汇总与问工笔示例问题。只读汇总既有记录，不新增表，不改任何状态与审批语义。

- 来历（trace）：一条 AI 产出的“依据 → 哪次 AI 运行 → 谁在何时确认/驳回”，字段全部来自 findings / alerts / ai_runs / audit，
  没有记录的环节如实写“无记录”，不补；
- 反馈汇总（feedback_summary）：各 Agent 的产出被人采纳/驳回的次数与驳回原因。只数人已处置的事项，样本不足不给比例；
  这是“采纳率”，不是准确率；
- 示例问题（suggest_questions）：只在有对应真实数据时才出题，问题限定在问工笔能检索到的数据范围内。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from .. import roles as R
from ..agents import AGENT_BY_KEY, KIND_TO_AGENT
from ..db import all_, loads, one
from . import findings as findings_svc
from . import site_agents

MIN_DECIDED = 3          # 已处置样本少于此数不给比例
MAX_REASONS = 3
REASON_CHARS = 40
TRACE_TYPES = ("finding", "alert")

ACTION_NAME = {
    "finding.confirm": "确认并派单", "finding.confirm_edit": "修改后确认并派单", "finding.assign": "派单", "finding.nudge": "催办", "finding.resolved": "确认闭环", "finding.open": "退回重改",
    "finding.dismissed": "驳回/忽略", "finding.rejected": "复核未通过", "finding.rectifying": "提交整改",
    "finding.reverify": "重新让 AI 复核",
    "cmd.assign": "指令派单", "cmd.nudge": "指令催办", "alert.feedback": "预警反馈",
}
ALERT_STATUS_NAME = {"open": "待确认", "confirmed": "已采纳", "false_alarm": "误报", "closed": "已关闭"}
FINDING_STATUS_NAME = {"open": "待处理", "rectifying": "待复核", "resolved": "已闭环", "rejected": "复核未通过", "dismissed": "已忽略"}


class NotFound(LookupError):
    pass


# ----------------------------------------------------------------------------- 调用类型 → 智能体 / 中文用途名；调用统计（全站唯一口径）

# agents.py 登记之外、但确实发生的调用类型：归到对应的智能体；ai.* 是连通测试/自检，不计入任何智能体
EXTRA_KIND_AGENT = {"cmd.intent": "dispatch", "note.classify": "scribe", "feed.summary": "circle", "log.week": "scribe",
                    # 增补能力：随手记追问、工效诊断、预控提醒卡、分包履约画像
                    "note.followup": "scribe", "progress.slowdiag": "progress", "safety.precontrol": "safety", "sub.profile": "archivist"}
# AI 小结 digest.<列表> 按列表归属智能体；工笔文书 report.<类型> 都归报表Agent
DIGEST_AGENT = {"alerts": "progress", "safety": "safety", "quality": "quality", "logs": "dq", "tickets": "material"}

KIND_NAME = {
    "log.draft": "日志草稿生成", "log.week": "周确认草稿", "asr": "语音转写", "note.classify": "随手记归类",
    "safety.scan": "照片安全识别", "safety.verify": "整改照片复核", "quality.checklist": "质控点清单", "dq.review": "日志数据预审",
    "alert.explain": "预警原因分析", "progress.recovery": "赶工方案", "progress.health": "计划体检", "output.map": "清单映射建议", "output.analyze": "产值分析",
    "report.week": "周报撰写", "report.month": "月报撰写", "qa.plan": "问答检索规划", "qa.answer": "问答作答",
    "brief.daily": "每日简报", "cmd.parse": "指令转派单", "cmd.minutes": "例会纪要整理", "cmd.intent": "指令意图识别",
    "nudge.plan": "催办安排", "site.archive": "部位档案整理", "claim.pack": "索赔证据包", "feed.comment": "现场圈点评",
    "feed.pick": "现场圈精选", "feed.summary": "现场圈摘要", "gate.classify": "开工条件梳理", "ticket.ocr": "票据识别",
    "conceal.pack": "隐蔽验收资料归集", "note.followup": "随手记追问", "progress.slowdiag": "工效诊断", "safety.precontrol": "高风险作业预控提醒",
    "sub.profile": "分包履约画像", "ai.test": "连通测试", "ai.check.text": "文本模型自检", "ai.check.vision": "视觉模型自检",
}


def agent_of_kind(kind: str | None) -> str | None:
    """调用类型 → 智能体 key；连通测试与未登记类型返回 None（不计入任何智能体）。"""
    if not kind or kind.startswith("ai."):
        return None
    found = KIND_TO_AGENT.get(kind) or EXTRA_KIND_AGENT.get(kind)
    if found:
        return found
    head, _, tail = kind.partition(".")
    if head == "digest":
        return DIGEST_AGENT.get(tail)
    return "report" if head == "report" and tail else None


def kind_name(kind: str | None) -> str | None:
    """调用类型的中文用途名；未登记的返回 None，界面原样显示 key 并提示。"""
    k = kind or ""
    if k in KIND_NAME:
        return KIND_NAME[k]
    if k.startswith("digest.") and k[7:] in DIGEST_AGENT:
        return "AI 小结"
    return "工笔文书起草" if k.startswith("report.") and k[7:] else None


def since_days(days: int, today: date | None = None) -> str:
    """“近 N 天”的起点（含今天共 N 天）。ai_runs.created_at 为 ISO 字符串，可直接按字符串比较。"""
    return ((today or date.today()) - timedelta(days=days - 1)).isoformat()


def call_stats(conn, project_id: int, since: str | None = None) -> dict[str, dict]:
    """按智能体汇总 ai_runs：调用次数（每条留痕记一次，失败也计）、成功数、修复/补跑/降级、模拟、最近运行。
    “智能应用”页与“应用成效”页都只用这一个函数，口径一致。"""
    sql = "SELECT kind, provider, ok, input_meta_json, created_at FROM ai_runs WHERE project_id=?"
    args: tuple = (project_id,)
    if since:
        sql += " AND created_at >= ?"
        args += (since,)
    out: dict[str, dict] = {}
    for r in all_(conn, sql, args):
        key = agent_of_kind(r["kind"])
        if not key:
            continue
        a = out.setdefault(key, {"calls": 0, "ok": 0, "repaired": 0, "regen": 0, "degraded": 0, "mock": 0, "last": None})
        meta = loads(r["input_meta_json"], {}) or {}
        a["calls"] += 1
        a["ok"] += 1 if r["ok"] else 0
        a["repaired"] += 1 if meta.get("repaired") else 0
        a["regen"] += 1 if meta.get("regen") else 0
        a["degraded"] += 1 if meta.get("degraded") else 0
        a["mock"] += 1 if r["provider"] == "mock" else 0
        a["last"] = max(a["last"] or "", r["created_at"] or "") or None
    return out


# ----------------------------------------------------------------------------- 来历

def _run_info(conn, run_id: int | None) -> dict | None:
    if not run_id:
        return None
    r = one(conn, "SELECT kind, provider, model, prompt_version, latency_ms, ok, created_at FROM ai_runs WHERE id=?", (run_id,))
    if not r:
        return None
    ag = AGENT_BY_KEY.get(agent_of_kind(r["kind"]) or "", {})
    return {"id": run_id, "kind": r["kind"], "agent": ag.get("name", ""), "provider": r["provider"], "model": r["model"],
            "prompt_version": r["prompt_version"], "latency_ms": r["latency_ms"], "ok": bool(r["ok"]), "at": r["created_at"],
            "mock": r["provider"] == "mock"}


def _audit_steps(conn, target: str) -> list[dict]:
    rows = all_(conn, """SELECT a.action, a.detail, a.created_at, u.name, u.role FROM audit a LEFT JOIN users u ON u.id=a.user_id
                         WHERE a.target=? ORDER BY a.id""", (target,))
    return [{"at": r["created_at"], "who": r["name"] or "系统", "role_name": R.role_name(r["role"]) if r["role"] else "",
             "what": ACTION_NAME.get(r["action"], r["action"]), "note": (r["detail"] or "")[:120] if r["action"].endswith(("dismissed", "feedback")) else ""}
            for r in rows]


def _finding_trace(conn, user: dict, fid: int) -> dict:
    f = one(conn, "SELECT * FROM findings WHERE id=? AND project_id=?", (fid, user["project_id"]))
    if not f:
        raise NotFound("事项不存在")
    body, verify = loads(f["body_json"], {}) or {}, loads(f["verify_json"]) or {}
    basis = []
    if body.get("desc"):
        basis.append(str(body["desc"])[:200])
    for k, label in (("basis", "依据"), ("standard", "规范"), ("evidence", "证据")):
        if body.get(k):
            basis.append(f"{label}：{str(body[k])[:200]}")
    src = {"log": "施工日志", "media": "现场照片", "note": "随手记"}.get(f["ref_type"] or "", "")
    if src:
        basis.append(f"来源：{src} #{f['ref_id']}" if f["ref_id"] else f"来源：{src}")
    if f["activity_code"]:
        basis.append(f"关联作业：{f['activity_code']}")
    steps = [{"at": f["created_at"], "who": AGENT_BY_KEY.get(f["agent"], {}).get("name", f["agent"]), "role_name": "智能体", "what": "发现并生成事项", "note": ""}]
    steps += _audit_steps(conn, f"finding:{fid}")
    if verify.get("state") or verify.get("reason"):  # AI 复核只是建议：闭环前一律写“AI 建议：……”，不写成已确认
        label = site_agents.verify_label(verify, f["status"])
        reason = verify.get("reason") if label.startswith("AI 建议") else ""
        steps.append({"at": verify.get("at") or verify.get("started_at") or f["updated_at"], "who": "智能体", "role_name": "智能体", "what": "整改照片复核",
                      "note": (label + (f"；{reason}" if reason else ""))[:120]})
    steps.sort(key=lambda s: s["at"] or "")
    decider = one(conn, "SELECT name FROM users WHERE id=?", (f["feedback_by"],)) if f["feedback_by"] else None
    return {"type": "finding", "id": fid, "title": f["title"], "agent": AGENT_BY_KEY.get(f["agent"], {}).get("name", f["agent"]),
            "status": f["status"], "status_name": findings_svc.status_label(f),
            "detector": "AI 识别" if f["ai_run_id"] else "规则/日志提取（未调用 AI）", "basis": basis,
            "run": _run_info(conn, f["ai_run_id"]),
            "human": {"by": decider["name"] if decider else None, "at": f["feedback_at"], "note": f["feedback_note"] or ""} if f["feedback_by"] else None,
            "steps": steps}


def _alert_trace(conn, user: dict, aid: int) -> dict:
    a = one(conn, "SELECT * FROM alerts WHERE id=? AND project_id=?", (aid, user["project_id"]))
    if not a:
        raise NotFound("预警不存在")
    d, exp = loads(a["detail_json"], {}) or {}, loads(a["explanation_json"], {}) or {}
    basis = [f"触发规则：{a['rule']}（由 P6 与日志进度确定性计算，非 AI 判断）"]
    if d.get("slip_days") is not None:
        basis.append(f"滞后 {d['slip_days']} 天，总浮时 {d.get('total_float_d')} 天")
    basis.append(f"关联作业：{a['activity_code']} {a['activity_name']}；数据日期 {a['as_of']}")
    if exp.get("judgement"):
        basis.append(f"AI 解读：{str(exp['judgement'])[:200]}")
    steps = [{"at": a["first_detected"], "who": "规则引擎", "role_name": "系统", "what": "首次检出", "note": ""}]
    steps += _audit_steps(conn, f"alert:{aid}")
    steps.sort(key=lambda s: s["at"] or "")
    decider = one(conn, "SELECT name FROM users WHERE id=?", (a["feedback_by"],)) if a["feedback_by"] else None
    return {"type": "alert", "id": aid, "title": a["title"], "agent": "进度Agent", "status": a["status"],
            "status_name": ALERT_STATUS_NAME.get(a["status"], a["status"]),
            "detector": "规则检出，AI 只做解读" if a["ai_run_id"] else "规则检出（AI 解读未生成）", "basis": basis,
            "run": _run_info(conn, a["ai_run_id"]),
            "human": {"by": decider["name"] if decider else None, "at": a["feedback_at"], "note": a["feedback_note"] or ""} if a["feedback_by"] else None,
            "steps": steps}


def trace(conn, user: dict, ttype: str, tid: int) -> dict:
    if ttype not in TRACE_TYPES:
        raise ValueError("类型只能是 finding 或 alert")
    return _finding_trace(conn, user, tid) if ttype == "finding" else _alert_trace(conn, user, tid)


# ----------------------------------------------------------------------------- 反馈汇总

def _reasons(rows: list[dict]) -> list[dict]:
    c: Counter = Counter()
    for r in rows:
        t = " ".join(str(r["note"] or "").split())[:REASON_CHARS]
        if t:
            c[t] += 1
    return [{"text": t, "n": n} for t, n in c.most_common(MAX_REASONS)]


def feedback_summary(conn, project_id: int) -> dict[str, dict]:
    """agent key → {decided, adopted, rejected, rate(采纳率，样本足够才有), reasons[{text,n}]}。
    事项：已闭环=采纳，已忽略=驳回；预警：确认属实=采纳，误报=驳回（归进度Agent）。待处理和系统自动关闭的不计。"""
    out: dict[str, dict] = {}

    def slot(key):
        return out.setdefault(key, {"adopted": 0, "rejected": 0, "notes": []})

    for r in all_(conn, "SELECT agent, status, feedback_note note FROM findings WHERE project_id=? AND status IN ('resolved','dismissed')", (project_id,)):
        s = slot(r["agent"])
        if r["status"] == "resolved":
            s["adopted"] += 1
        else:
            s["rejected"] += 1
            s["notes"].append(r)
    from .alert_tags import NOT_REPLAY   # 回放产生的预警不计入采纳统计
    for r in all_(conn, "SELECT status, feedback_note note FROM alerts WHERE project_id=? AND rule<>'stale' AND status IN ('confirmed','false_alarm') AND feedback_by IS NOT NULL"
                  + NOT_REPLAY, (project_id,)):
        s = slot("progress")
        if r["status"] == "confirmed":
            s["adopted"] += 1
        else:
            s["rejected"] += 1
            s["notes"].append(r)
    res = {}
    for key, s in out.items():
        decided = s["adopted"] + s["rejected"]
        res[key] = {"decided": decided, "adopted": s["adopted"], "rejected": s["rejected"],
                    "rate": round(s["adopted"] / decided, 3) if decided >= MIN_DECIDED else None, "reasons": _reasons(s["notes"])}
    return res


# ----------------------------------------------------------------------------- 示例问题

_ROLE_TOPICS = {"safety_sup": {"safety"}, "eng_sup": {"quality", "progress", "logs"}}


def suggest_questions(conn, user: dict, today: date | None = None, limit: int = 5) -> list[dict]:
    """按岗位和项目里真实存在的数据出示例问题；没有对应数据就不出，不引导到查不到的问题上。"""
    today = today or date.today()
    pid = user["project_id"]
    topics = _ROLE_TOPICS.get(user["role"])
    want = (lambda t: True) if topics is None else (lambda t: t in topics)
    d = today.isoformat()
    out: list[dict] = []

    def add(topic: str, q: str, why: str):
        if want(topic):
            out.append({"q": q, "why": why})

    sf = one(conn, """SELECT SUM(status IN ('open','rectifying','rejected') AND level='high') high,
                      SUM(status IN ('open','rejected') AND due_date < ?) overdue FROM findings WHERE project_id=? AND agent='safety' AND confirmed_at IS NOT NULL""", (d, pid))
    if (sf["high"] or 0) > 0:
        add("safety", "当前未闭环的高风险隐患有哪些？责任人和期限是什么？", f"有 {sf['high']} 项高风险隐患未闭环")
    if (sf["overdue"] or 0) > 0:
        add("safety", "哪些安全隐患已经超期没整改？", f"有 {sf['overdue']} 项隐患已超期")
    ql = one(conn, "SELECT COUNT(*) n FROM findings WHERE project_id=? AND agent='quality' AND kind<>'qc_reminder' AND status IN ('open','rectifying','rejected')", (pid,))
    if ql["n"]:
        add("quality", "质量问题还有哪些没闭环？集中在哪些工序？", f"有 {ql['n']} 项质量问题未闭环")
    al = one(conn, "SELECT COUNT(*) n FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' AND level IN ('red','orange')", (pid,))
    if al["n"]:
        add("progress", "红橙预警涉及哪些关键作业？滞后原因是什么？", f"有 {al['n']} 条红橙预警待确认")
    lg = one(conn, "SELECT COUNT(*) n FROM logs WHERE project_id=? AND mode='ai' AND status IN ('submitted','approved')", (pid,))
    if lg["n"]:
        add("logs", "上周日志里反映了哪些问题？", f"已有 {lg['n']} 份已提交日志可检索")
        add("logs", "最近一周各作业的进度推进情况怎么样？", "依据已提交日志的进度记录")
    return out[:limit]
