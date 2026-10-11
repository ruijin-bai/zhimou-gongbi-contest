"""“问工笔”：规划 → 检索 → 有据回答。"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .. import roles as R
from ..ai import qa_agent
from ..ai.client import call_json
from ..agents import AGENT_BY_KEY
from ..db import all_, loads
from . import facts as facts_svc
from . import output as output_svc
from . import p6
from . import reports as report_svc
from . import contract_kb
from .logs import progress_state
from . import act_names as _an

# 各 Agent 的问答范围：只查本职相关的数据源，回答时只以本职身份作答。key 与 agents.py 一致；analyst/空 = 全部数据（问工笔）。
# sources：logs 日志/进度，alerts 进度预警，plan P6计划，findings:<agent> 该Agent发现的事项台账，dq 日志预审结果，output 周产值，reports 周月报，brief 今日简报
SCOPES: dict[str, dict] = {
    "scribe": {"sources": ["logs"], "scope": "施工日志的内容与整理情况（做了什么、投入多少人和机械、遇到什么问题）"},
    "safety": {"sources": ["findings:safety"], "scope": "安全隐患台账（隐患、等级、责任人、期限、整改与复核结论）"},
    "quality": {"sources": ["findings:quality", "logs"], "scope": "质量问题与质控提醒台账，以及日志中记录的质量类问题"},
    "dq": {"sources": ["dq"], "scope": "日志数据审核结果（预审评分与发现的完整性、一致性、重复照片等问题）"},
    "progress": {"sources": ["alerts", "plan", "logs"], "scope": "P6 计划、进度预警、关键作业滞后及原因"},
    "output": {"sources": ["output"], "scope": "周产值（实际与计划）、月末预测和产值分析"},
    "report": {"sources": ["reports"], "scope": "已生成的周报、月报内容，以及已确认的月度复盘经验条目"},
    "brief": {"sources": ["brief", "alerts"], "scope": "今日简报与进度预警中需要关注或决策的事项"},
    # 以下 7 个“办事型”智能体只查自己的产出；roles = 能问的岗位，与各自页面/接口的查看权限一致（指挥另按派单权限判定，见 can_ask）
    "chaser": {"sources": ["nudge"], "scope": "催办记录：提醒了谁、到第几级、因静默或上限延后的情况，以及事项是否已处理", "roles": ("eng_sup", "safety_sup")},
    "dispatch": {"sources": ["cmd"], "scope": "已确认的例会纪要与行动项（责任人、期限、是否完成）和一句话指令派单的记录"},
    "archivist": {"sources": ["claims"], "scope": "从日志归并出的停工、缺料、故障等索赔事件和已生成的部位档案概况",
                  "roles": ("manager", "eng_sup", "safety_sup", "leader")},
    "circle": {"sources": ["circle"], "scope": "现场圈里智能体的点评、对 @工笔 的回答和 AI 精选理由", "roles": ("tech", "sub_safety", "manager", "eng_sup", "safety_sup")},
    "gate": {"sources": ["gate"], "scope": "未来 14 天要开工作业的放行清单：开工结论、缺项和受阻条件", "roles": ("manager", "eng_sup", "safety_sup", "leader", "tech")},
    "material": {"sources": ["tickets"], "scope": "材料票据与复试台账：待确认票据、疑点、试块到期和复试状态", "roles": ("tech", "eng_sup", "manager", "safety_sup", "leader")},
    "records": {"sources": ["conceal"], "scope": "隐蔽验收记录（状态、缺拍、待填项）和疑似缺验收资料的作业", "roles": ("manager", "eng_sup", "tech", "safety_sup", "leader")},
}
# 问工笔（不指定 Agent / analyst）只检索日志、进度预警与 P6 计划；产值、日志预审只经对应 Agent 问，且项目经理不开放（见 ask 的岗位检查）
DEFAULT_SOURCES = ("logs", "alerts", "plan")


def _findings_facts(conn, pid: int, agent: str, words: list[str]) -> list[str]:
    rows = all_(conn, "SELECT * FROM findings WHERE project_id=? AND agent=? AND confirmed_at IS NOT NULL ORDER BY id DESC LIMIT 80", (pid, agent))
    out = []
    for r in rows:
        body, ver = loads(r["body_json"], {}), loads(r["verify_json"]) or {}
        text = f"{r['title']} {body.get('desc', '')} {body.get('measure', '')}"
        if words and not any(w in text for w in words) and len(rows) > 30:
            continue
        out.append(f"[F{r['id']}] {r['created_at'][:10]} {r['kind']} 等级{r['level']} 状态{r['status']}：{r['title']}"
                   + (f" 措施：{body.get('measure')}" if body.get("measure") else "") + (f" 期限{r['due_date']}" if r["due_date"] else "")
                   + _verify_fact(ver, r["status"]))
    return out


def _verify_fact(ver: dict, status: str) -> str:
    """整改复核写成“AI 建议”（4.4 契约）：闭环前不写成已确认，模型给的理由跟在建议后面。"""
    from .site_agents import paren, verify_label
    label = verify_label(ver, status)
    if not label:
        return ""
    reason = (ver.get("reason") or "").strip() if label.startswith("AI 建议") else ""
    return f" 整改复核：{label}" + paren(reason)


def can_ask(user: dict, agent: str | None) -> bool:
    """这个岗位能不能向该智能体提问（智能应用页据此显示“提问”，接口同样把关）。问工笔/不指定 = 都能问。"""
    if not agent or agent == "analyst":
        return True
    sc = SCOPES.get(agent)
    if not sc:
        return False
    if user["role"] in R.PM_SCOPE and agent not in R.PM_ASK_AGENTS:
        return False
    if agent == "dispatch":
        from . import cmd as cmd_svc
        return cmd_svc.is_commander(user)
    return "roles" not in sc or user["role"] in sc["roles"]


def _nudge_facts(conn, user: dict, words: list[str]) -> list[str]:
    from . import nudge as nudge_svc
    s = nudge_svc.today_summary(conn, user["project_id"], role=user["role"])
    out = [f"[催办] 今天（{s['day']}）自动提醒 {s['persons']} 人、{s['objects']} 件，升级到主管及以上 {s['escalated_n']} 件，"
           f"因静默时段延后 {s['deferred_quiet']} 件、因每日上限延后 {s['deferred_cap']} 件；催办{'已开启' if s['enabled'] else '已关闭'}"]
    for r in nudge_svc.recent_log(conn, user["project_id"], 60, user["role"]):
        text = f"{r['subject']} {r['status_text']} {r['to']} {r['owner']}"
        if words and not any(w in text for w in words) and len(out) > 30:
            continue
        sent = "提醒" if r["status"] == "sent" else "未发（" + (r["reason"] or r["status"]) + "）"
        out.append(f"[催办] {r['at'][:16]} 第{r['stage']}级（{r['stage_name']}）{sent}{r['to']}" + (f"（{r['to_role']}）" if r["to_role"] else "")
                   + f"：{r['subject']} {r['status_text']}" + (f" 责任人{r['owner']}" if r["owner"] else "")
                   + ("，事项已处理" if r["handled"] else "，事项未处理" if r["handled"] is False else ""))
    return out


def _cmd_facts(conn, user: dict, words: list[str]) -> list[str]:
    from . import cmd as cmd_svc
    pid, out = user["project_id"], []
    for m in cmd_svc.meeting_list(conn, pid, 8):
        out.append(f"[例会] {m['date']} {m['title']}")
        for i in all_(conn, "SELECT * FROM meeting_items WHERE meeting_id=? ORDER BY id", (m["id"],)):
            it = cmd_svc.item_view(conn, i)
            state = {"done": "已完成", "overdue": f"超期{it['days_over']}天"}.get(it["status"], "进行中")
            out.append(f"[例会] {m['date']} 行动项：{it['text']}（责任人{it['owner'] or '未定'}，期限{it['due_date'] or '未定'}，{state}）"
                       + (f" 关联[F{it['finding']['id']}]{it['finding']['status_name']}" if it["finding"] else ""))
    names = {u["id"]: u["name"] for u in all_(conn, "SELECT id, name FROM users WHERE project_id=?", (pid,))}
    rows = all_(conn, "SELECT * FROM audit WHERE action IN ('cmd.assign','cmd.nudge') AND user_id IN (SELECT id FROM users WHERE project_id=?) ORDER BY id DESC LIMIT 40", (pid,))
    for a in rows:
        d = loads(a["detail"], {}) or {}
        fid = str(a["target"] or "").replace("finding:", "")
        if a["action"] == "cmd.assign":
            who = names.get(int(d["assignee_id"]), "某人") if d.get("assignee_id") else "某人"
            what = f"派给{who}" + (f"，期限{d['due']}" if d.get("due") else "")
        else:
            what = f"催办：{d.get('note') or ''}"
        out.append(f"[指令] {a['created_at'][:16]} {names.get(a['user_id'], '')} 用一句话指令对[F{fid}]{what}")
    return out


def _claims_facts(conn, user: dict, words: list[str]) -> list[str]:
    from . import claims as claim_svc
    out = []
    for e in claim_svc.list_events(conn, user):
        acts = "、".join(f"{_an.fmt(a['code'], a['name'])}" for a in e["activities"][:3])
        out.append(f"[索赔事件] {e['date_from']}~{e['date_to']} {e['cat_name']}（{e['company']}）：涉及{e['log_days']}天日志、{e['n_issues']}条问题"
                   + ("，有停工" if e["halt"] else "") + ("，含未审核日志" if e["unconfirmed"] else "") + (f"；作业 {acts}" if acts else "")
                   + (f"；例：{'；'.join(e['descs'])}" if e["descs"] else ""))
    for r in all_(conn, "SELECT part, summary, created_at FROM site_summaries WHERE project_id=? ORDER BY created_at DESC LIMIT 20", (user["project_id"],)):
        out.append(f"[部位档案] {r['part']}（{r['created_at'][:10]}）：{r['summary'][:200]}")
    return out


def _circle_facts(conn, user: dict, words: list[str]) -> list[str]:
    pid, out = user["project_id"], []
    q = "SELECT * FROM feed_agent_notes WHERE project_id=?" + (" AND audience='all'" if R.is_sub(user["role"]) else "") + " ORDER BY id DESC LIMIT 60"
    for r in all_(conn, q, (pid,)):
        if words and not any(w in r["text"] + (r["question"] or "") for w in words) and len(out) > 30:
            continue
        out.append(f"[现场圈] {r['created_at'][:16]} {r['perspective']}视角" + (f"回答提问“{r['question']}”" if r["question"] else "点评") + f"：{r['text'][:200]}")
    for r in all_(conn, "SELECT * FROM circle_picks WHERE project_id=? ORDER BY id DESC LIMIT 20", (pid,)):
        out.append(f"[AI精选] {r['created_at'][:10]}：{r['reason']}")
    return out


def _gate_facts(conn, user: dict, words: list[str]) -> list[str]:
    from . import gate as gate_svc
    d = gate_svc.build_list(conn, user, 14, allow_ai=False)
    if not d["has_plan"]:
        return ["[放行] 还没有导入 P6 计划，没有放行清单"]
    k = d["kpi"]
    out = [f"[放行] 未来 14 天要开工 {k['total']} 项：可开工{k['ready']}、临近有缺项{k['near']}、有缺项{k['gap']}、受阻{k['blocked']}（P6 数据日期 {d['data_date']}）"]
    for r in d["items"]:
        gaps = "、".join(g["label"] for g in r.get("top_gaps") or [])
        when = f"还有{r['days_to_start']}天开工" if r["days_to_start"] is not None else "开工日期不明"
        out.append(f"[{r['code']}] {_an.label(r['code'], r['name'])}：{when}，结论{r['verdict_name']}" + (f"，缺项：{gaps}" if gaps else "") + ("，关键线路" if r.get("is_critical") else ""))
    return out


def _tickets_facts(conn, user: dict, words: list[str]) -> list[str]:
    from . import tickets as tk
    rk = tk.risks(conn, user)
    out = [f"[材料] 待确认票据 {rk['pending_tickets']} 张，核对出的疑点 {rk['total']} 条（{rk['note']}）"]
    out += [f"[材料疑点] {tk.KIND_NAME.get(i['kind'], i['kind'])}：{i['title']} {i['detail']}" for i in rk["items"][:40]]
    for i in tk.due_list(conn, user, 14)["items"][:30]:
        left = f"已过{-i['days_left']}天" if i["days_left"] < 0 else f"还剩{i['days_left']}天"
        out.append(f"[试块] {i['part']} {i['grade']} 第{i['group_no']}组 {i['age']}天试块 {i['due']} 到期（{left}）")
    for b in tk.list_batches(conn, user)["items"][:40]:
        out.append(f"[材料批次] {b['arrived_at'] or ''} {b['category_name']} {b['name']} {b['spec'] or ''} {b['qty']:g}{b['unit']}"
                   f"（{b['supplier'] or '供应商未填'}），复试{b['retest_name']}" + (f"，报告{b['report_no']}" if b["report_no"] else ""))
    return out


def _conceal_facts(conn, user: dict, words: list[str]) -> list[str]:
    from . import conceal as cc
    out = [f"[验收#{r['id']}] {r['part']} {r['template_name']}（{r['status_name']}）验收日{r['inspection_date'] or '未定'}，照片{r['photos']}张"
           + (f"，缺拍{r['missing_shots']}项" if r["missing_shots"] else "") + (f"，待填{r['pending_items']}项" if r["pending_items"] else "")
           + (f"，监理意见：{r['result_note']}" if r["result_note"] else "") for r in cc.list_records(conn, user)["records"][:60]]
    g = cc.gaps(conn, user)
    if g["has_plan"]:
        s = g["summary"]
        out.append(f"[验收缺口] 已开工作业应有验收 {s['total']} 项：已齐{s['done']}、未齐{s['pending']}、疑似缺记录{s['missing']}（{g['note']}）")
        out += [f"[{x['activity_code']}] {_an.label(x['activity_code'], x['activity_name'])}（{x['part']}）{x['template_name']}：{x['state_name']}" for x in g["gaps"][:30]]
    return out


EXTRA_FACTS = {"nudge": _nudge_facts, "cmd": _cmd_facts, "claims": _claims_facts, "circle": _circle_facts,
               "gate": _gate_facts, "tickets": _tickets_facts, "conceal": _conceal_facts}


# 项目经理问到范围外的事（填报、记录率、日志/数据审核等，关键词表见 brief.ask_scope_ok；产值可以问，RJ 10-10）：确定性拒答，不调用 AI
PM_REFUSAL = "不在项目经理的查看范围。可以问进度、质量、安全方面的问题，例如关键作业进度、预警、隐患整改。"


def _refuse(question: str, agent: str | None) -> dict:
    plan = {"need": [], "activity_codes": [], "keywords": [], "date_from": None, "date_to": None}
    return {"question": question, "plan": plan, "answer": PM_REFUSAL, "refs": [], "facts_count": 0,
            "agent": agent if agent in SCOPES else None, "degraded": False, "out_of_scope": True}


def _dq_facts(conn, pid: int) -> list[str]:
    out = []
    for r in all_(conn, "SELECT id, log_date, dq_json FROM logs WHERE project_id=? AND dq_json IS NOT NULL ORDER BY id DESC LIMIT 40", (pid,)):
        dq = loads(r["dq_json"], {})
        out.append(f"[L{r['id']}] {r['log_date']} 预审：{str(dq)[:260]}")
    return out


def _output_facts(conn, pid: int) -> list[str]:
    ov = output_svc.overview(conn, pid)
    out = [f"[产值] 周产值 {w['week']}：实际{w['record_actual'] if w['record_actual'] is not None else w['system']}，计划{w['planned']}（{ov['currency']}）" for w in ov["series"][-12:]]
    if ov.get("analysis"):
        out.append(f"[产值] 产值分析：{str(ov['analysis'].get('summary') or ov['analysis'])[:400]}")
    out.append(f"[产值] 清单映射：已确认{ov['maps_confirmed']}条，待确认{ov['maps_suggested']}条")
    return out


def _report_facts(conn, pid: int, role: str = "", question: str = "") -> list[str]:
    from ..ai import report_agent
    out = []
    for r in all_(conn, "SELECT id, kind, period_start, period_end, facts_json, narrative_json FROM reports WHERE project_id=? ORDER BY id DESC LIMIT 6", (pid,)):
        nar = loads(r["narrative_json"], {})
        if role in R.PM_SCOPE and "产值" not in question:  # 项目经理没问产值时不主动带产值（主动问产值则照常引用，RJ 10-10）
            facts = loads(r["facts_json"], {}) or {}
            _f, _t, nar, _c = report_svc.pm_scope(facts, facts.get("template") or report_agent.default_template(r["kind"]), nar, [], drop_output=True)
        out.append(f"[R{r['id']}] {'周报' if r['kind'] == 'week' else '月报'} {r['period_start']}~{r['period_end']}：{str(nar)[:500]}")
    from .gongbi_more import lessons   # 已确认的月度复盘经验条目，提问时可引用
    return out + lessons(conn, pid)


def _brief_facts(conn, pid: int) -> list[str]:
    from .brief import latest
    b = latest(conn, pid)
    if not b:
        return []
    return [f"[简报] {b.get('headline')}"] + [f"[简报] [{i.get('area')}] {i.get('title')}：{i.get('detail')} → {i.get('action')}" for i in b.get("items", [])]


def ask(conn, user: dict, question: str, agent: str | None = None) -> dict:
    question = question.strip()
    if not question or len(question) > 300:
        raise ValueError("问题不能为空且不超过300字")
    if agent and user["role"] in R.PM_SCOPE and agent not in R.PM_ASK_AGENTS:
        # 项目经理不审核日志：日志整理、日志预审 Agent 的问答不开放（前端不列，接口同样拒绝）；产值 Agent 可以主动问（RJ 10-10）
        raise PermissionError("该智能体不在项目经理的查看范围")
    if agent in SCOPES and not can_ask(user, agent):
        raise PermissionError("你的岗位看不到这个智能体的数据，不能向它提问")
    if user["role"] in R.PM_SCOPE:
        from .brief import ask_scope_ok
        if not ask_scope_ok(question):
            return _refuse(question, agent)
    pid = user["project_id"]
    acts = p6.current_activities(conn, pid)
    by_code = {a["code"]: a for a in acts}
    hint = [f"{_an.fmt(a['code'], a['name'])}" for a in acts if not a.get("act_finish")]
    plan_res = call_json(
        "qa.plan", system=qa_agent.PLAN_SYSTEM.replace("{today}", date.today().isoformat()),
        user=qa_agent.plan_prompt(question, hint), prompt_version=qa_agent.PROMPT_VERSION,
        mock=lambda: qa_agent.mock_plan(question, acts), validate=lambda d: qa_agent.normalize_plan(d, set(by_code)),
        # 规划输出不合格时降级为按关键词/日期规则检索（检索范围更宽，但仍只依据真实记录作答）
        fallback=lambda _t: qa_agent.normalize_plan(qa_agent.mock_plan(question, acts), set(by_code)),
        project_id=pid, user_id=user["id"], max_tokens=500, temperature=0.0, input_meta={"q_chars": len(question)},
    )
    plan = plan_res.data
    today = date.today()
    df = plan["date_from"] or (today - timedelta(days=30)).isoformat()
    dt = plan["date_to"] or today.isoformat()
    codes, words = set(plan["activity_codes"]), plan["keywords"]
    scope = SCOPES.get(agent or "")
    srcs = scope["sources"] if scope else None  # None = 问工笔：DEFAULT_SOURCES（日志、预警、P6），不含产值、日志预审
    if not scope:
        plan["need"] = [n for n in plan["need"] if n in DEFAULT_SOURCES or n == "progress"]
    else:
        plan["need"] =[n for n in ("logs", "alerts", "plan") if n in srcs] + (["progress"] if "plan" in srcs and "logs" in srcs else [])
    facts: list[str] = []
    for s_ in srcs or []:
        if s_.startswith("findings:"):
            facts += _findings_facts(conn, pid, s_.split(":")[1], words)
    if not scope and "产值" in question:  # 问工笔里明确问产值：带上产值事实（各岗位都能查产值，经理只读）
        srcs = [*DEFAULT_SOURCES, "output"]
    scope_text = SCOPES[agent]["scope"] if scope else "项目日志、P6 进度与预警"
    if not scope:   # 问工笔里问到安全隐患、质量问题：检索范围带上安全/质量台账，不只查日志与进度（验收 I11）
        for kw, ag, name in ((r"安全|隐患", "safety", "安全隐患台账"), (r"质量|质控", "quality", "质量问题台账")):
            if re.search(kw, question):
                facts += _findings_facts(conn, pid, ag, words)
                scope_text += "、" + name
    if srcs:
        facts += (_dq_facts(conn, pid) if "dq" in srcs else []) + (_output_facts(conn, pid) if "output" in srcs else [])             + (_report_facts(conn, pid, user["role"], question) if "reports" in srcs else []) + (_brief_facts(conn, pid) if "brief" in srcs else [])
        for s_ in srcs:
            if s_ in EXTRA_FACTS:
                facts += EXTRA_FACTS[s_](conn, user, words)
    if "logs" in plan["need"] or "progress" in plan["need"]:
        rows = all_(conn, """SELECT l.id, l.log_date, l.content_json, u.name author FROM logs l JOIN users u ON u.id=l.author_id
                             WHERE l.project_id=? AND l.mode='ai' AND l.status IN ('submitted','approved') AND l.log_date BETWEEN ? AND ?
                             ORDER BY l.log_date DESC LIMIT 120""", (pid, df, dt))
        for r in rows:
            c = loads(r["content_json"], {})
            items = [("施工", w.get("activity_code"), f"{w.get('part', '')}{w.get('process', '')}：{w.get('desc', '')}") for w in c.get("works", [])]
            items += [("进度", p.get("activity_code"), f"{p.get('qty') or ''}{p.get('unit') or ''} 累计{p.get('pct')}%") for p in c.get("progress", [])]
            items += [("问题", i.get("activity_code"), f"{i.get('type')}/{i.get('severity')}：{i.get('desc')} 措施：{i.get('measure')}") for i in c.get("issues", [])]
            items += [("人员", None, f"{x['trade']}{x.get('count') or ''}人") for x in c.get("labor", [])]
            items += [("机械", None, f"{x['name']}{x.get('count') or ''}台 {x.get('status', '')}") for x in c.get("machinery", [])]
            for kind, code, text in items:
                hit = (not codes and not words) or (code and code in codes) or any(w in text for w in words)
                if hit:
                    facts.append(f"[L{r['id']}] {r['log_date']} {r['author']} {kind}{'(' + _an.fmt(code) + ')' if code else ''}：{text}")
            if len(facts) > 120:
                break
    if "alerts" in plan["need"]:
        q = "SELECT * FROM alerts WHERE project_id=? AND rule<>'stale' ORDER BY id DESC LIMIT 40"
        for a in all_(conn, q, (pid,)):
            if not codes or a["activity_code"] in codes or any(w in a["title"] for w in words):
                d = loads(a["detail_json"], {})
                judge = facts_svc.alert_judgement(conn, a) if a["explanation_json"] else ""  # 与最新判定天数不一致的旧分析不再引用
                facts.append(f"[W{a['id']}] {a['as_of']} {a['title']}（状态{a['status']}，滞后{d.get('slip_days')}天，浮时{d.get('total_float_d')}天）"
                             + (f" 分析：{judge}" if judge else ""))
    if "plan" in plan["need"] or "progress" in plan["need"]:
        imp = p6.current_import(conn, pid)
        state = progress_state(conn, pid, imp) if imp else {}
        targets = [by_code[c] for c in codes] if codes else [a for a in acts if any(w in a["name"] for w in words)][:15]
        for a in targets:
            st = state.get(a["code"]) or {}
            facts.append(f"[{a['code']}] {_an.label(a['code'], a['name'])}：P6计划 {a.get('early_start')}~{a.get('early_finish')}，总浮时{a.get('total_float_d')}天，"
                         f"P6完成{a.get('phys_pct')}%（数据日期{imp['data_date'] if imp else '无'}），日报最新累计{st.get('pct')}%（{st.get('pct_date')}）")
    if not scope and user["role"] in contract_kb.READERS and contract_kb.CONTRACT_RE.search(question) and contract_kb.has_library(conn, pid):
        facts = contract_kb.qa_facts(conn, user, words, question) + facts   # 问到合同/条款/规范：条款库原文排在前面，回答标 [C编号]
    facts_text = "\n".join(facts[:160]) or "（没有检索到记录）"
    res = call_json(
        "qa.answer", system=qa_agent.ANSWER_SYSTEM, user=f"用户问题：{question}\n\n【检索结果】\n{facts_text}",
        prompt_version=qa_agent.PROMPT_VERSION, mock=lambda: qa_agent.mock_answer(question, facts_text),
        validate=qa_agent.normalize_answer, fallback=qa_agent.fallback_answer, project_id=pid, user_id=user["id"],
        max_tokens=1200, input_meta={"facts": len(facts)},
    )
    ans, problems = qa_agent.verify_answer(res.data, facts_text, question, len(facts))   # 出处只认检索结果里的编号，数字必须有出处
    if problems:
        ans = qa_agent.rule_answer(facts_text, "回答里有记录中查不到的数字")
    return {"question": question, "plan": plan, "answer": ans["answer"], "refs": ans["refs"], "facts_count": len(facts),
            "agent": agent if scope else None, "scope": scope_text, "degraded": bool(res.degraded or plan_res.degraded or problems)}
