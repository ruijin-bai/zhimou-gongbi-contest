"""工笔文书类型注册表（工笔文书方案 2026-10-08）。

周报 week / 月报 month 仍走 report_agent + services/reports.py，原有行为不变；这里登记它们只为“工笔文书”页列出全部类型。
新增类型每个定义：名称、语言、栏目（代码常量）、对象（周期 / 事件）、可起草 / 可确认的岗位、是否对外发出、事实文本、规则版文字。

所有类型同一套规则：
1. 数字只来自系统事实；AI 只组织文字。数字守卫逐栏核对，核对不过的栏目退回规则版文字（规则版只拼接事实里的数字）；
2. 引用编号 [L日志] [W预警] [S隐患]；
3. AI 写的是草稿，人确认后冻结；对外发送由人完成，系统只记录“已发出”；
4. 英文版与中文版同一份事实底稿；对外文书不写人名、金额，事实底稿里本来就不放人名与产值。
"""
from __future__ import annotations

import re

from . import report_agent
from ..services import act_names as _an

PROMPT_VERSION = "gongbi-docs-v1-20261008"

ENG = frozenset({"eng_sup"})
REPORT_GEN = frozenset({"manager", "eng_sup"})      # 与周月报 report_gen 一致（系统管理员不生成报表，RJ 10-09）
REPORT_PUB = frozenset({"manager"})                 # 与周月报 report_publish 一致
EVENT_ROLES = frozenset({"manager", "eng_sup"})

CAT_EN = {"stop": "Suspension / stoppage of work", "material": "Shortage or late delivery of materials", "equipment": "Equipment breakdown",
          "weather": "Adverse weather conditions", "change": "Instruction / variation / external cause"}


def _sec(key, title, instruction, *, table=None, max_chars=400, typ="narrative", max_items=6):
    return {"key": key, "title": title, "type": typ, "instruction": instruction, "table": table, "max_chars": max_chars, "max_items": max_items}


EN_RULES = ("Write in formal British English for submission to the Engineer.\n"
            "Hard rules:\n1. Use ONLY numbers that appear in [FACTS]; never calculate, estimate or invent a number. Always write numbers as Arabic numerals "
            "(e.g. 3, not three). Write dates as YYYY-MM-DD.\n2. Cite sources as [L<log id>], [W<warning id>], [S<hazard id>] exactly as given.\n"
            "3. Translate Chinese site records faithfully; do not add facts that are not in [FACTS]. Use activity codes with their names as given.\n"
            "4. Never mention personal names; refer to roles only (e.g. the site engineer, the subcontractor).\n"
            "5. Never mention money, prices, amounts, output value or costs.\n")
ZH_RULES = ("你是中国港湾海外项目的资料工程师。硬性规定：\n1. 只能使用【统计事实】中出现的数字，不得自行计算、推算或编造任何数字。\n"
            "2. 引用日志用[L编号]，引用预警用[W编号]，引用隐患用[S编号]。\n3. 不写人名，只写岗位；不写金额、产值、费用。\n"
            "4. 结论性语句须注明“参考推演”，不得给出工期索赔（EOT）天数结论。\n")

TYPES: dict[str, dict] = {
    "week": {"name": "施工周报", "lang": "zh", "ref": "week", "group": "报表", "legacy": True},
    "month": {"name": "施工月报", "lang": "zh", "ref": "month", "group": "报表", "legacy": True},
    "weekly_en": {
        "name": "Weekly Progress Report", "zh": "英文周报（致监理）", "lang": "en", "ref": "week", "group": "报表", "outward": True,
        "roles": REPORT_GEN, "confirm": REPORT_PUB, "intro": "同周报事实底稿，不含产值，报给 Engineer。",
        "title": "{project} — Weekly Progress Report",
        "sections": [
            _sec("summary", "1. Executive Summary", "Summarise the main works completed this week and the overall progress status, citing the number of daily logs relied on.", max_chars=600),
            _sec("progress", "2. Progress against Programme", "For each activity behind programme, state the variance in percentage points and the recorded cause with [L] references; if none, say activities are generally on programme.", table="progress", max_chars=700),
            _sec("milestones", "3. Key Milestones Forecast", "Name milestones with on-time probability below 60%, their forecast dates and drivers; if none, say key milestones are forecast on time.", table="milestones", max_chars=450),
            _sec("safety", "4. Health and Safety", "State hazards found, main categories, close-out status, and any open high-risk hazards with [S] references.", table="safety", max_chars=450),
            _sec("quality", "5. Quality", "State quality issues and QC reminders recorded; if none, say no quality issues were recorded this week.", max_chars=300),
            _sec("issues", "6. Issues and Mitigation Measures", "List main issues with the measures taken or proposed, one per line numbered 1., 2., with [L] references.", table="issues", max_chars=800),
            _sec("next_plan", "7. Look-ahead (Next Week)", "State the key activities planned for next week from the programme and resources to be prepared.", table="next", max_chars=450),
            _sec("coordination", "8. Matters Requiring the Engineer's Attention", "Items needing the Engineer's or Employer's action; empty if none.", typ="list", max_items=5),
        ],
    },
    "monthly_en": {
        "name": "Monthly Progress Report", "zh": "英文月报（FIDIC 4.20 栏目）", "lang": "en", "ref": "month", "group": "报表", "outward": True,
        "roles": REPORT_GEN, "confirm": REPORT_PUB, "intro": "按 FIDIC 2017 第 4.20 款月报栏目，同月报事实底稿，不含产值。",
        "title": "{project} — Monthly Progress Report",
        "sections": [
            _sec("summary", "1. Executive Summary", "Summarise progress of the month, key achievements and main concerns, citing the number of daily logs relied on.", max_chars=700),
            _sec("progress", "2. Progress of the Works (Sub-Clause 4.20(a))", "Describe progress of each main activity with quantities and cumulative percentages from [FACTS], citing [L] references.", table="progress", max_chars=900),
            _sec("resources", "3. Contractor's Personnel and Equipment (Sub-Clause 4.20(c))", "Describe average daily personnel by trade and equipment on site as given in [FACTS].", table="resources", max_chars=450),
            _sec("quality", "4. Quality Assurance (Sub-Clause 4.20(d))", "State quality issues and QC reminders recorded in the month.", max_chars=350),
            _sec("notices", "5. Notices and Claims (Sub-Clause 4.20(e))", "List delay events recorded in the month with their notice status as given; do not judge entitlement and do not mention amounts.", table="notices", max_chars=500),
            _sec("safety", "6. Health and Safety (Sub-Clause 4.20(f))", "State hazards found, categories, close-out status, and open high-risk hazards with [S] references.", table="safety", max_chars=450),
            _sec("comparison", "7. Comparison of Actual and Planned Progress (Sub-Clause 4.20(g))", "State activities behind programme with variance and causes [L], milestone forecasts, and the measures being taken.", table="milestones", max_chars=800),
            _sec("next_plan", "8. Programme for Next Month", "State the key activities planned for next month from the programme.", table="next", max_chars=450),
        ],
    },
    "notice_of_claim": {
        "name": "Notice of Claim", "zh": "索赔意向通知函（中英对照）", "lang": "en", "ref": "event", "group": "函件", "outward": True,
        "roles": EVENT_ROLES, "confirm": frozenset({"manager"}), "intro": "单个事件，按合同通知条款在期限内发给 Engineer。只写事件、日期、受影响作业与条款号，不写金额与天数结论。",
        "title": "Notice of Claim — {event}",
        "sections": [
            _sec("subject", "Subject", "One line: 'Notice of Claim under <clause> — <event category>, <date from> to <date to>'.", max_chars=180),
            _sec("event", "Description of the Event or Circumstance", "Describe what happened, on which dates, as recorded in the site daily logs, citing [L] references. Do not judge the cause or liability.", max_chars=900),
            _sec("affected", "Works Affected", "List the affected programme activities by code and name, with their planned dates and whether critical, as given.", max_chars=500),
            _sec("basis", "Contractual Basis and Reservation", "State that this notice is given under the clause in [FACTS], that the Contractor considers itself entitled to an extension of time and/or additional payment, and reserves the right to submit a fully detailed claim with particulars in due course. No amounts and no number of days.", max_chars=500),
            _sec("zh", "中文对照", "用中文完整对照上面四栏的内容，不得增加英文版没有的内容。", max_chars=900),
        ],
    },
    "delay_brief": {
        "name": "延误事件分析简报", "zh": "延误事件分析简报", "lang": "zh", "ref": "event", "group": "分析",
        "roles": EVENT_ROLES, "confirm": EVENT_ROLES, "intro": "这个事件可能影响哪些作业和关键节点。结论标“参考推演”，不出工期索赔天数。",
        "title": "延误事件分析简报 — {event}",
        "sections": [
            _sec("event", "一、事件概况", "概括事件类别、起止日期、日志记录天数、是否含停工，引用日志编号[L]。", max_chars=220),
            _sec("affected", "二、受影响作业", "逐项说明受影响作业的计划日期、总浮时与是否关键作业。", table="affected", max_chars=260),
            _sec("milestones", "三、对关键节点的可能影响（参考推演）", "按 P6 逻辑关系说明受影响作业的后续里程碑及其当前预测日期；标明“参考推演”；不给天数结论。", table="milestones", max_chars=260),
            _sec("notice", "四、通知时效", "说明知悉日、通知期限、当前状态；提醒是否构成索赔以商务和合同部门判断为准。", max_chars=160),
            _sec("follow", "五、建议跟进事项", "需补充的证据与需协调的事项，每条一句。", typ="list", max_items=5),
        ],
    },
    "work_contact": {
        "name": "工作联系单（致分包）", "zh": "工作联系单（致分包）", "lang": "zh", "ref": "source", "group": "函件", "outward": True,
        "roles": frozenset({"eng_sup", "safety_sup"}), "confirm": frozenset({"eng_sup", "safety_sup"}),
        "intro": "挂在已确认的隐患、质量问题或属实的进度预警上，是派单的书面形式。主管确认即下发到分包账号，分包签收、回复后销项。不写罚款。",
        "title": "{company} 工作联系单 — {title}",
        "sections": [
            _sec("subject", "事由", "一句话写明问题类别、部位或作业。", max_chars=50),
            _sec("fact", "一、问题情况", "写明发现日期、部位或作业、问题描述，引用编号（隐患[S]、预警[W]）与照片编号；只写事实。", max_chars=260),
            _sec("requirement", "二、整改与回复要求", "沿用派单时的整改要求；进度类写清请书面回复赶工措施及预计完成日期。不写罚款、扣款或处罚结论。", max_chars=200),
            _sec("deadline", "三、期限", "写明完成或回复期限。", max_chars=60),
            _sec("basis", "四、依据", "写事实里给出的依据，不编条款号。", max_chars=80),
        ],
    },
    "letter_to_sub": {
        "name": "致分包函（项目部名义）", "zh": "致分包函（项目部名义）", "lang": "zh", "ref": "company", "group": "函件", "outward": True,
        "roles": frozenset({"eng_sup", "safety_sup"}), "confirm": frozenset({"manager"}),
        "intro": "同一单位同类问题重复出现、或联系单超期未回复时用。主管起草，项目经理确认后才算正式，抄送该单位全部账号。不写罚款。",
        "title": "致{company}函 — {area}问题",
        "sections": [
            _sec("subject", "事由", "一句话写明单位与问题类别。", max_chars=50),
            _sec("fact", "一、近期问题情况", "按事实列出统计期内该单位此类问题的数量、重复出现的类别、未闭环情况与超期未回复的联系单，引用编号。", table="problems", max_chars=320),
            _sec("requirement", "二、要求", "要求该单位书面回复整改措施与防止再次发生的安排。不写罚款、扣款或处罚结论。", max_chars=220),
            _sec("deadline", "三、期限与回复", "写明书面回复期限。", max_chars=60),
            _sec("basis", "四、依据", "写事实里给出的依据，不编条款号。", max_chars=80),
        ],
    },
}

NEW_KINDS = tuple(k for k, t in TYPES.items() if not t.get("legacy"))


def template(kind: str) -> dict:
    t = TYPES[kind]
    return {"kind": kind, "title": t["title"], "sections": [dict(s) for s in t["sections"]]}


def catalogue(role: str) -> list[dict]:
    """“工笔文书”页的类型清单：该岗位能起草的、能看的都列出，带分组。"""
    out = []
    for k, t in TYPES.items():
        out.append({"kind": k, "name": t["name"], "zh": t.get("zh") or t["name"], "lang": t["lang"], "ref": t["ref"], "group": t["group"],
                    "outward": bool(t.get("outward")), "intro": t.get("intro", ""), "legacy": bool(t.get("legacy")),
                    "can_draft": role in t["roles"] if "roles" in t else None, "can_confirm": role in t["confirm"] if "confirm" in t else None})
    return out


# ----------------------------------------------------------------------------- 提示词

def system_prompt(kind: str) -> str:
    tpl = template(kind)
    slots = []
    for s in tpl["sections"]:
        if s["type"] == "list":
            slots.append(f'"{s["key"]}": array of strings, at most {s["max_items"]} items. {s["instruction"]}')
        else:
            slots.append(f'"{s["key"]}": at most {s["max_chars"]} characters. {s["instruction"]}')
    head = EN_RULES if TYPES[kind]["lang"] == "en" else ZH_RULES
    return head + "Output JSON only, with exactly these keys:\n{" + ",\n".join(slots) + "}"


def _g(v) -> str:
    return "unknown" if v is None else f"{v:g}" if isinstance(v, (int, float)) else str(v)


def facts_text(kind: str, f: dict) -> str:
    """交给模型、也作为数字守卫白名单的事实文本。数字全部出自事实底稿。"""
    if TYPES[kind].get("text"):   # 增补类型自带事实文本（services/gongbi_more.py）
        return TYPES[kind]["text"](f)
    if TYPES[kind]["ref"] in ("week", "month"):
        return _period_text(f)
    if TYPES[kind]["ref"] in ("source", "company"):
        return _sub_text(f)
    return _event_text(f)


def _sub_text(f: dict) -> str:
    L = [f"【统计事实】项目：{f['project']}；致：{f['company']}；问题类别：{f['area_name']}"]
    if f["src_type"] == "finding":
        L.append(f"[{'S' if f['area'] == 'safety' else 'Q'}{f['src_id']}] {f['date']} 发现：{f['title']}；类别：{f['category'] or '未分类'}；等级：{f['level']}"
                 + (f"；部位/场景：{f['scene']}" if f.get("scene") else "") + (f"；作业：{_an.fmt(f['activity_code'])}" if f.get("activity_code") else ""))
        L.append(f"问题描述：{f['desc']}")
        if f.get("log_id"):
            L.append(f"来源日志：[L{f['log_id']}]")
        if f["photos"]:
            L.append("照片编号：" + "、".join(f"M{i}" for i in f["photos"]))
    elif f["src_type"] == "alert":
        L.append(f"[W{f['src_id']}] {f['date']} 进度预警（已确认属实）：{f['title']}；作业：{_an.fmt(f['activity_code'], f['activity_name'])}")
        L.append(f"计划完成 {_g(f.get('planned_pct'))}%，实际完成 {_g(f.get('actual_pct'))}%，滞后 {_g(f.get('slip_days'))} 天；计划完工 {f.get('early_finish') or '未知'}，预测完工 {f.get('forecast_finish') or '未知'}")
    else:
        L.append(f"统计期：{f['since']} 起近 {f['days']} 天；该单位{f['area_name']}类问题 {f['n_problems']} 项，其中未闭环 {f['n_open']} 项")
        for k, v in f["repeat"].items():
            L.append(f"重复出现：{k} {v} 次")
        for p in f["problems"][:12]:
            L.append(f"[{p['ref']}] {p['date']} {p['title']}（{p['status']}）")
        for x in f["overdue_letters"]:
            L.append(f"超期未回复的工作联系单：{x['issued']} 下发，期限 {x['due']}，{x['status']}")
    L.append(f"要求：{f['requirement']}")
    L.append(f"期限：{f.get('due_date') or '按派单期限'}")
    L.append(f["basis"])
    return "\n".join(L)


def _period_text(f: dict) -> str:
    L = [f"[FACTS] Project: {f['project']}; period: {f['period_start']} to {f['period_end']}",
         f"Basis: {f['logs']['count']} confirmed daily logs; {f['photos']} site photos." + (f" Programme data date: {f['p6']['data_date']}." if f.get("p6") else "")]
    L.append("Activity progress (code name: quantity this period; cumulative %; planned %; variance in percentage points):")
    for p in f["progress"]:
        qty = ", ".join(f"{q['qty']:g} {q['unit']}" for q in p["qty"]) or "no quantity"
        L.append(f"{_an.fmt(p['code'], p['name'])}: {qty}; cumulative {_g(p['end_pct'])}%; planned {_g(p['plan_pct'])}%; variance {_g(p['gap'])}" + ("; critical" if p.get("critical") else ""))
    if f.get("milestones"):
        L.append("Milestone forecast (code name; programme date; forecast; P80; on-time probability %):")
        for m in f["milestones"]:
            L.append(f"{_an.fmt(m['code'], m['name'])}; {m['p6_date']}; {m['forecast']}; {m['p80']}; {_g(m.get('prob_pct'))}")
    s = f.get("safety") or {}
    L.append(f"Safety: {s.get('found', 0)} hazards found ({s.get('high', 0)} high risk); {s.get('resolved', 0)} closed; {s.get('open', 0)} open. Categories: "
             + "; ".join(f"{k} {v}" for k, v in (s.get("by_category") or {}).items()))
    for h in s.get("open_high", [])[:5]:
        L.append(f"[S{h['id']}] open high-risk hazard: {h['title']} (due {h['due_date']})")
    q = f.get("quality") or {}
    L.append(f"Quality: {q.get('issues', 0)} quality issues; {q.get('reminders', 0)} QC reminders.")
    if f.get("labor"):
        L.append("Average daily personnel by trade: " + "; ".join(f"{x['name']} {x['avg']:g}" for x in f["labor"]))
    if f.get("machinery"):
        L.append("Average daily equipment: " + "; ".join(f"{x['name']} {x['avg']:g}" for x in f["machinery"]))
    if f.get("notices") is not None:
        L.append("Delay events and notice status (category; from; to; notice deadline; status):")
        for n in f["notices"]:
            L.append(f"{n['cat_en']}; {n['date_from']}; {n['date_to']}; {n['deadline']}; {n['status_en']}")
    L.append("Issues recorded in daily logs (Chinese original):")
    for i in f["issues"][:25]:
        L.append(f"[L{i['log_id']}] {i['date']} {i['type']}/{i['severity']}: {i['desc']} (measure: {i['measure'] or 'not stated'})")
    L.append("Programme warnings:")
    for w in f["alerts"][:15]:
        L.append(f"[W{w['id']}] {w['level']} {w['title']} ({w['status']})")
    L.append("Works recorded (Chinese original):")
    for w in f["works"][:40]:
        L.append(f"[L{w['log_id']}] {w['date']} {w['text']}")
    L.append("Activities planned for next period:")
    for n in f["next"][:20]:
        L.append(f"{_an.fmt(n['code'], n['name'])} ({n['start']} to {n['finish']}{', critical' if n['critical'] else ''})")
    return "\n".join(L)


def _event_text(f: dict) -> str:
    e = f["event"]
    L = [f"[FACTS] Project: {f['project']}",
         f"Event category: {e['cat_en']}（{e['cat_name']}）; first recorded {e['date_from']}; last recorded {e['date_to']}; recorded on {e['log_days']} day{'s' if e['log_days'] != 1 else ''}"
         + (f"; stoppage recorded on {', '.join(e['halt_dates'])}" if e.get("halt_dates") else ""),
         f"Notice: aware date {f['notice']['aware_date']}; notice period {f['notice']['days']} days under {f['notice']['clause_en']}（{f['notice']['clause']}）; deadline {f['notice']['deadline']}; status {f['notice']['status_name']}"]
    L.append("Daily log records (Chinese original):")
    for i in e["issues"]:
        L.append(f"[L{i['log_id']}] {i['date']} {i['type']}/{i['severity']}: {i['desc']} (measure: {i['measure'] or 'not stated'})")
    for w in e.get("weather", []):
        if w["weather"]:
            L.append(f"[L{w['log_id']}] {w['date']} weather recorded: {w['weather']}")
    L.append("Affected programme activities (code name; planned start; planned finish; total float days; critical):")
    for a in f["affected"]:
        L.append(f"{_an.fmt(a['code'], a['name'])}; {a['plan_start'] or 'unknown'}; {a['plan_finish'] or 'unknown'}; {_g(a['float_d'])}; {'yes' if a['critical'] else 'no'}")
    if not f["affected"]:
        L.append("No programme activity code was recorded against this event (to be supplemented).")
    if f.get("milestones") is not None:
        L.append("Downstream milestones by programme logic (code name; programme date; current forecast; P80):")
        for m in f["milestones"]:
            L.append(f"{_an.fmt(m['code'], m['name'])}; {m['p6_date']}; {m['forecast']}; {m['p80']}")
    if f.get("gaps"):
        L.append("Evidence to be supplemented: " + "; ".join(f["gaps"]))
    return "\n".join(L)


# ----------------------------------------------------------------------------- 规则版文字（AI 不可用、或某栏数字守卫不通过时用；只拼接事实）

def rule(kind: str, f: dict) -> dict:
    if TYPES[kind].get("rule"):
        return TYPES[kind]["rule"](kind, f)
    return {"weekly_en": _rule_period, "monthly_en": _rule_period, "notice_of_claim": _rule_notice, "delay_brief": _rule_brief,
            "work_contact": _rule_sub, "letter_to_sub": _rule_sub}[kind](kind, f)


def _rule_sub(kind: str, f: dict) -> dict:
    if f["src_type"] == "finding":
        tag = f"[{'S' if f['area'] == 'safety' else 'Q'}{f['src_id']}]"
        fact = (f"{f['date']} 检查发现：{f['desc']}{tag}" + (f"，部位/场景：{f['scene']}" if f.get("scene") else "")
                + (f"，见照片 {'、'.join('M' + str(i) for i in f['photos'])}" if f["photos"] else "") + "。")
        subject = f"关于{f['area_name']}问题“{f['title'][:30]}”的整改"
    elif f["src_type"] == "alert":
        fact = (f"[W{f['src_id']}] 作业 {f['activity_code']} {f['activity_name']} 计划完成 {_g(f.get('planned_pct'))}%，实际完成 {_g(f.get('actual_pct'))}%，"
                f"滞后 {_g(f.get('slip_days'))} 天；预测完工 {f.get('forecast_finish') or '未知'}（计划 {f.get('early_finish') or '未知'}）。该预警已经项目部确认属实。")
        subject = f"关于作业 {f['activity_code']} 进度滞后的工作联系"
    else:
        rep = "；".join(f"{k} {v} 次" for k, v in f["repeat"].items())
        fact = (f"自 {f['since']} 起近 {f['days']} 天，贵单位共发生{f['area_name']}类问题 {f['n_problems']} 项，其中未闭环 {f['n_open']} 项"
                + (f"；重复出现：{rep}" if rep else "") + "；" + "、".join(f"[{p['ref']}]" for p in f["problems"][:8]) + "。"
                + ("另有工作联系单超期未回复：" + "；".join(f"{x['issued']} 下发、期限 {x['due']}" for x in f["overdue_letters"]) + "。" if f["overdue_letters"] else ""))
        subject = f"关于{f['company']}{f['area_name']}问题的函"
    return {"subject": subject, "fact": fact, "requirement": f["requirement"] + "。" if not f["requirement"].endswith("。") else f["requirement"],
            "deadline": f"请于 {f['due_date']} 前完成并书面回复。" if f.get("due_date") else "按派单期限完成。", "basis": f["basis"] + "。"}


def _behind(f):
    return [p for p in f["progress"] if p["gap"] is not None and p["gap"] < 0]


def _rule_period(kind: str, f: dict) -> dict:
    wk = kind == "weekly_en"
    per = "week" if wk else "month"
    s, q = f.get("safety") or {}, f.get("quality") or {}
    behind = _behind(f)
    prog = "; ".join(f"{p['code']} {p['name']} is {-p['gap']:g} percentage points behind programme (cumulative {_g(p['end_pct'])}% against {_g(p['plan_pct'])}% planned)" for p in behind[:5])
    risky = [m for m in f.get("milestones", []) if m.get("prob_pct") is not None and m["prob_pct"] < 60]
    ms = "; ".join(f"{m['code']} {m['name']} is forecast for {m['forecast']} against {m['p6_date']} (on-time probability {m['prob_pct']}%)" for m in risky[:4])
    out = {
        "summary": f"This report covers {f['period_start']} to {f['period_end']} and is based on {f['logs']['count']} confirmed daily logs and {f['photos']} site photos. "
                   + ("Some activities are behind programme, as set out under Progress against Programme." if behind else "Activities are generally progressing on programme."),
        "progress": (prog + ".") if prog else "Activities are generally progressing on programme.",
        "milestones": (ms + ".") if ms else "Key milestones are forecast to be achieved on time.",
        "safety": f"{s.get('found', 0)} hazards were found this {per}, of which {s.get('resolved', 0)} were closed and {s.get('open', 0)} remain open."
                  + "".join(f" Open high-risk hazard [S{h['id']}]: {h['title']}." for h in s.get("open_high", [])[:3]),
        "quality": f"{q.get('issues', 0)} quality issues and {q.get('reminders', 0)} QC reminders were recorded this {per}.",
        "issues": "\n".join(f"{k + 1}. {i['type']}: see daily log [L{i['log_id']}] of {i['date']}." for k, i in enumerate(f["issues"][:6])) or "No significant issues were recorded.",
        "next_plan": "Planned activities: " + ("; ".join(f"{n['code']} {n['name']}" for n in f["next"][:8]) or "see the programme") + ".",
        "coordination": [],
        "resources": "Average daily personnel: " + ("; ".join(f"{x['name']} {x['avg']:g}" for x in f.get("labor", [])) or "not recorded")
                     + ". Average daily equipment: " + ("; ".join(f"{x['name']} {x['avg']:g}" for x in f.get("machinery", [])) or "not recorded") + ".",
        "notices": ("; ".join(f"{n['cat_en']} from {n['date_from']} to {n['date_to']}, notice deadline {n['deadline']}, status: {n['status_en']}" for n in (f.get("notices") or [])[:6]) + ".")
                   if f.get("notices") else "No delay events were recorded in this period.",
        "comparison": ((prog + ". ") if prog else "Activities are generally progressing on programme. ") + ((ms + ".") if ms else "Key milestones are forecast to be achieved on time."),
    }
    return {sec["key"]: out.get(sec["key"], "" if sec["type"] == "narrative" else []) for sec in TYPES[kind]["sections"]}


def _rule_notice(kind: str, f: dict) -> dict:
    e, n = f["event"], f["notice"]
    acts = "; ".join(f"{a['code']} {a['name']} (planned {a['plan_start'] or 'unknown'} to {a['plan_finish'] or 'unknown'}{', critical' if a['critical'] else ''})" for a in f["affected"])
    refs = ", ".join(f"[L{i}]" for i in sorted({i["log_id"] for i in e["issues"]}))
    return {
        "subject": f"Notice of Claim under {n['clause_en']} — {e['cat_en']}, {e['date_from']} to {e['date_to']}",
        "event": f"The Contractor gives notice of the following event or circumstance. {e['cat_en']} was recorded in the site daily logs from {e['date_from']} to {e['date_to']} "
                 f"on {e['log_days']} day{'s' if e['log_days'] != 1 else ''} {refs}." + (f" Stoppage of work was recorded on {', '.join(e['halt_dates'])}." if e.get("halt_dates") else ""),
        "affected": ("The following programme activities are affected: " + acts + ".") if acts else "The affected programme activities are being identified and will be stated in the detailed claim.",
        "basis": f"This notice is given under {n['clause_en']}. The Contractor considers itself entitled to an extension of the Time for Completion and/or additional payment, "
                 "and reserves the right to submit a fully detailed claim with supporting particulars in accordance with the Contract.",
        "zh": f"承包商就以下事件发出通知：施工日志于 {e['date_from']} 至 {e['date_to']} 期间共 {e['log_days']} 天记录了{e['cat_name']} {refs}。"
              + (f"受影响作业：{'；'.join(a['code'] + ' ' + a['name'] for a in f['affected'])}。" if f["affected"] else "受影响作业待补充。")
              + f"本通知依据{n['clause']}发出，承包商认为有权获得工期延长和/或额外付款，并保留按合同提交完整详细索赔的权利。",
    }


def _rule_brief(kind: str, f: dict) -> dict:
    e, n = f["event"], f["notice"]
    ms = f.get("milestones") or []
    return {
        "event": f"{e['cat_name']}：日志于 {e['date_from']} 至 {e['date_to']} 期间记录 {e['log_days']} 天"
                 + ("，含停工记录" if e.get("halt_dates") else "") + "；依据 " + "、".join(f"[L{i}]" for i in sorted({i["log_id"] for i in e["issues"]})) + "。",
        "affected": "；".join(f"{a['code']} {a['name']}（计划 {a['plan_start'] or '未知'} ~ {a['plan_finish'] or '未知'}，总浮时 {_g(a['float_d'])} 天{'，关键作业' if a['critical'] else ''}）"
                             for a in f["affected"]) + "。" if f["affected"] else "日志未写作业代码，受影响作业待补充。",
        "milestones": ("参考推演：按 P6 逻辑关系，后续里程碑有 " + "；".join(f"{m['code']} {m['name']}（P6 {m['p6_date']}，当前预测 {m['forecast']}）" for m in ms[:5]) + "。不作为工期索赔天数结论。")
                      if ms else "参考推演：未找到受影响作业的后续里程碑，或尚无进度预测。",
        "notice": f"知悉日 {n['aware_date']}，通知期限 {n['deadline']}（{n['clause']}，{n['days']} 天），当前状态：{n['status_name']}。是否构成索赔事件以商务和合同部门判断为准。",
        "follow": list(f.get("gaps") or [])[:5],
    }


# ----------------------------------------------------------------------------- 核对

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_EN_DATE = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?,?\s+\d{4}\b"
                      r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b")
_NUM_WORDS = re.compile(r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
                        r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million)\b", re.I)
_MONEY = re.compile(r"(US\$|\$|€|£|¥|\bUSD\b|\bEUR\b|\bXAF\b|\bXOF\b|\bFCFA\b|\bCNY\b|\bRMB\b|金额|万元|\d\s*元|美元|欧元|人民币)")
_OUTPUT = re.compile(r"产值|output value|earned value|turnover|valuation", re.I)
_PENALTY = re.compile(r"罚款|罚金|扣款|扣除|处罚|违约金|penalt|\bfine[sd]?\b|deduction", re.I)
SUB_KINDS = ("work_contact", "letter_to_sub")


def _flat(v) -> str:
    return "\n".join(v) if isinstance(v, list) else str(v or "")


def check(kind: str, narrative: dict, ftext: str) -> list[dict]:
    """逐栏核对：数字守卫（认千分位逗号、英文日期）+ 英文数字词 + 金额字样 + 产值字样。返回 [{field, number, issue}]。"""
    en = TYPES[kind]["lang"] == "en"
    issues = []
    for key, v in narrative.items():
        text = _flat(v)
        scrub = _THOUSANDS.sub("", _EN_DATE.sub("", text))
        issues += [{**x, "issue": "数字未在事实中找到"} for x in report_agent.number_guard({key: scrub}, _THOUSANDS.sub("", ftext))]
        if en and key != "zh":
            issues += [{"field": key, "number": m.group(0), "issue": "英文数字词（应写阿拉伯数字）"} for m in _NUM_WORDS.finditer(text)]
        issues += [{"field": key, "number": m.group(0), "issue": "出现金额字样"} for m in _MONEY.finditer(text)]
        issues += [{"field": key, "number": m.group(0), "issue": "出现产值字样"} for m in _OUTPUT.finditer(text)]
        if kind in SUB_KINDS:
            issues += [{"field": key, "number": m.group(0), "issue": "出现罚款/处罚字样"} for m in _PENALTY.finditer(text)]
    return issues


def normalize(kind: str, data) -> dict:
    return report_agent.normalize(data, template(kind))
