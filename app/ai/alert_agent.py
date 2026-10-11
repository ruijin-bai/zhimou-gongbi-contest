"""预警分析智能体：对规则判定的预警，结合日报证据解释原因、评估影响、给出措施。

判定由规则引擎完成（可回测）；本智能体只做“为什么、怎么办”，且原因必须引用日报编号 L{id}
或 P6 事实，否则标为“证据不足”，不臆测。
"""
from __future__ import annotations

import re
from datetime import date

from .. import roles as R
from ..services import act_names as _an

PROMPT_VERSION = "alert-v3-20261006"
# 措施责任人只能用系统里已有的业务岗位（不含系统管理员与只读领导）
OWNER_ROLES = tuple(R.ROLES[k]["name"] for k in ("tech", "sub_safety", "eng_sup", "safety_sup", "manager"))

SYSTEM = """你是中国港湾海外项目的计划工程师。系统已按P6计划和现场日报判定出一条进度预警，请你：
1. 根据给定的日报摘录找出最可能的滞后原因。每条原因必须在evidence中引用日报编号(如L12)或"P6"；没有证据支持的原因不要写，改写入data_gaps。
2. 说明对后续作业/关键节点的影响，只使用给定的数字，不要自行计算新的工期数字。
3. 给出2-4条可执行的赶工或纠偏措施，写明建议责任岗位和完成时限。责任岗位只能从系统岗位中选：技术员、安全员、工程主管、安全主管、项目经理（可加“分包”前缀，如“分包技术员”），不要写系统里没有的岗位（如设备主管、生产经理）；完成时限写“N天内”或不早于判定日的日期，不得早于判定日。
4. 文字里的天数、百分比必须与【事实底稿】一致，不得出现与底稿相反的结论（如“按计划推进”）；不要重复提醒“同步P6”。
5. 用简洁中文。只输出JSON：
{"judgement":"一句话结论","causes":[{"type":"材料|设备|人员|天气|图纸|外部|工序衔接|其他","cause":"原因","evidence":["L12"]}],
 "impact":"影响说明","measures":[{"action":"措施","owner":"责任岗位","due":"时限"}],"data_gaps":["缺少哪些信息"]}"""


def build_prompt(alert: dict, act: dict, logs: list[dict]) -> str:
    f = alert["detail"]
    lines = [
        f"预警：{alert['title']}（等级：{ {'red': '红色', 'orange': '橙色', 'yellow': '黄色'}[alert['level']] }，规则：{alert['rule_name']}）",
        f"P6作业：{_an.fmt(act['code'], act['name'])}；WBS：{act.get('wbs_path') or ''}",
        f"计划：{f.get('early_start')} ~ {f.get('early_finish')}；总浮时 {f.get('total_float_d')} 天；关键作业：{'是' if f.get('critical') else '否'}",
        f"判定日应完成 {f.get('planned_pct')}%，实际 {f.get('actual_pct')}%（来源：{f.get('actual_source')}），滞后 {f.get('slip_days')} 天，剩余浮时 {f.get('float_left_d')} 天，预测完成 {f.get('forecast_finish')}",
    ]
    if f.get("milestones"):
        lines.append("下游里程碑：" + "；".join(f"{_an.fmt(m['code'], m['name'])}(计划{m.get('planned')})" for m in f["milestones"]))
    if f.get("risk_signals"):
        lines.append("日报风险信号：" + "；".join(f"L{r['log_id']} {r['date']} {r['type']}：{r['desc']}" for r in f["risk_signals"]))
    lines.append("\n近期相关日报摘录：")
    if not logs:
        lines.append("（无相关日报）")
    for lg in logs:
        items = "；".join(lg["items"] + lg["general"])
        lines.append(f"L{lg['log_id']} {lg['date']} {lg['author']}：{items}")
    return "\n".join(lines)


def normalize(data, valid_logs: set[str]) -> dict:
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")

    def ev(v):
        refs = re.findall(r"L\d+|P6", " ".join(v) if isinstance(v, list) else str(v or ""))
        return [r for r in dict.fromkeys(refs) if r == "P6" or r in valid_logs]
    causes = []
    gaps = [str(g) for g in data.get("data_gaps") or [] if str(g).strip()]
    for c in data.get("causes") or []:
        if not isinstance(c, dict) or not str(c.get("cause") or "").strip():
            continue
        refs = ev(c.get("evidence"))
        if not refs:
            gaps.append(f"未证实的推测原因：{c.get('cause')}")
            continue
        causes.append({"type": str(c.get("type") or "其他"), "cause": str(c["cause"]).strip(), "evidence": refs})
    measures = [{"action": str(m.get("action") or "").strip(), "owner": str(m.get("owner") or "").strip(), "due": str(m.get("due") or "").strip()}
                for m in data.get("measures") or [] if isinstance(m, dict) and str(m.get("action") or "").strip()]
    return {"judgement": str(data.get("judgement") or "").strip(), "causes": causes, "impact": str(data.get("impact") or "").strip(),
            "measures": measures[:5], "data_gaps": gaps[:6]}


def mock(alert: dict, logs: list[dict]) -> dict:
    f = alert["detail"]
    causes = []
    for lg in logs:
        for item in lg["items"] + lg["general"]:
            if item.startswith("问题"):
                causes.append({"type": "其他", "cause": item, "evidence": [f"L{lg['log_id']}"]})
    lag = f"滞后 {f['slip_days']:g} 天，" if alert["rule"] in ("behind", "late_start") and f.get("slip_days") is not None else ""
    left = f"剩余浮时 {f['float_left_d']:g} 天" if f.get("float_left_d") is not None else alert["rule_name"]
    return {
        "judgement": f"（离线模拟）{alert['activity_code']} {lag}{left}。",
        "causes": causes[:3], "impact": "（离线模拟）若不纠偏，将影响后续作业。",
        "measures": [{"action": "核实现场资源投入并制定赶工计划", "owner": "工程主管", "due": "3天内"}],
        "data_gaps": [] if causes else ["近期日报未记录与该作业相关的问题"],
    }


def template(alert: dict, logs: list[dict], line: str) -> dict:
    """AI 文字与事实底稿不一致时的确定性替代：判定数字取自预警事实，原因只列日报里记录过的“问题”，措施为通用核查项。"""
    f = alert["detail"]
    causes = [{"type": "其他", "cause": item, "evidence": [f"L{lg['log_id']}"]} for lg in logs for item in lg["items"] + lg["general"] if item.startswith("问题")]
    return {
        "judgement": f"{alert['activity_code']} {alert['rule_name']}：{line}。" if line else f"{alert['activity_code']} {alert['rule_name']}。",
        "causes": causes[:3],
        "impact": f"计划完成 {f.get('early_finish')}，按当前进度预测完成 {f.get('forecast_finish')}。" if f.get("forecast_finish") else "",
        "measures": [{"action": "核实现场资源投入并制定赶工计划", "owner": "工程主管", "due": "3天内"}],
        "data_gaps": ([] if causes else ["近期日报未记录与该作业相关的问题"]) + ["AI 文字与事实底稿不一致，已改用系统模板文字"],
    }


def texts(d: dict) -> str:
    """需要与事实底稿核对的全部文字。"""
    parts = [d.get("judgement", ""), d.get("impact", "")]
    parts += [c["cause"] for c in d.get("causes", [])]
    parts += [f"{m['action']} {m['due']}" for m in d.get("measures", [])]
    return "\n".join(str(x) for x in parts)


# 期限日期只认三种写法：带年份的 ISO（2026-10-08 / 2026/10/08 / 2026年10月8日）、带“月”字（10月8日）、
# 以及后面不跟数量单位的“10-08”。“2-3天内”“1-2周”是天数区间，不是日期。
_DUE_DATE = re.compile(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?!\d)"
                       r"|(?<!\d)(\d{1,2})月(\d{1,2})[日号]?(?!\d)"
                       r"|(?<![\d-])(\d{1,2})-(\d{1,2})(?![\d天日周个月小时])")


def _due_date(text: str, as_of: str) -> date | None:
    m = _DUE_DATE.search(text or "")
    if not m:
        return None
    y, mo, d = (m.group(1), m.group(2), m.group(3)) if m.group(1) else ((None, m.group(4), m.group(5)) if m.group(4) else (None, m.group(6), m.group(7)))
    try:
        ref = date.fromisoformat(as_of)
        mo, d = int(mo), int(d)
        if y:
            return date(int(y), mo, d)
        # 没写年份：月份小于判定月按次年（判定 12-28，期限“1月5日前”是次年 1 月 5 日）；
        # 次年的日期离判定日超过半年就不是跨年，仍按当年算（判定 10-05 写“9月30日前”是早于判定日）
        due = date(ref.year, mo, d)
        if mo < ref.month:
            nxt = date(ref.year + 1, mo, d)
            if (nxt - ref).days <= 183:
                due = nxt
        return due
    except ValueError:
        return None


def _owner_ok(owner: str) -> bool:
    """责任人里含任一系统岗位名即可（“工程主管牵头，分包技术员配合”“工程主管（牵头）”都算）。"""
    return any(r in (owner or "") for r in OWNER_ROLES)


def measure_problems(d: dict, as_of: str | None) -> list[str]:
    """措施的出口校验：完成时限不得早于判定日；责任人只能是系统已有岗位。"""
    out = []
    for m in d.get("measures") or []:
        due = _due_date(m.get("due", ""), as_of) if as_of else None
        if due and due.isoformat() < as_of:
            out.append(f"措施“{m.get('action')}”期限 {m.get('due')} 早于判定日 {as_of}")
        if not _owner_ok(m.get("owner", "")):
            out.append(f"责任人“{m.get('owner')}”不是系统岗位（只能用：{'、'.join(OWNER_ROLES)}）")
    return out
