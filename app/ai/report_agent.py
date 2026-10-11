"""报表Agent：按项目定义的模板逐栏撰写。模板规定每一栏写什么、多长、必须包含什么；数字由系统统计。"""
from __future__ import annotations

import copy
import re
from ..services import act_names as _an

PROMPT_VERSION = "report-v3-20261006"

_SECTIONS = [
    {"key": "overview", "title": "一、{p}施工概况", "type": "narrative", "max_chars": 180,
     "instruction": "概括{p}完成的主要施工内容和关键节点进展，提及依据的日志份数与主要作业"},
    {"key": "progress", "title": "二、形象进度与计划对比", "type": "narrative", "table": "progress", "max_chars": 220,
     "instruction": "逐项说明滞后作业的偏差百分点及原因，原因须引用日志编号[L]；无滞后写“各作业基本按计划推进”"},
    {"key": "milestones", "title": "三、关键节点预测", "type": "narrative", "table": "milestones", "max_chars": 150,
     "instruction": "说明按期概率低于60%的节点、预测日期与主要驱动作业；无风险写“关键节点预计按期”"},
    {"key": "output", "title": "四、产值完成情况", "type": "narrative", "table": "output", "max_chars": 150,
     "instruction": "说明{p}系统统计产值、与计划对比及主要贡献作业；无清单映射时说明“产值未统计”"},
    {"key": "safety", "title": "五、安全管理", "type": "narrative", "table": "safety", "max_chars": 160,
     "instruction": "说明{p}发现隐患数量、主要类别、整改闭环情况，单独点出未闭环的高风险隐患"},
    {"key": "quality", "title": "六、质量管理", "type": "narrative", "max_chars": 120,
     "instruction": "说明质量问题和质控点执行情况；无记录写“{p}未记录质量问题”"},
    {"key": "issues", "title": "七、存在问题及解决措施", "type": "narrative", "table": "issues", "max_chars": 260,
     "instruction": "列出主要问题及已采取或建议的措施，每条一行，以“1.”“2.”编号"},
    {"key": "next_plan", "title": "八、{n}工作计划", "type": "narrative", "table": "next", "max_chars": 160,
     "instruction": "写{n}重点作业、目标和需提前准备的资源"},
    {"key": "coordination", "title": "九、需协调事项", "type": "list", "max_items": 5,
     "instruction": "需业主、监理或公司协调的事项，没有则为空"},
]


def default_template(kind: str) -> dict:
    p, n = ("本周", "下周") if kind == "week" else ("本月", "下月")
    secs = copy.deepcopy(_SECTIONS)
    for s in secs:
        s["title"] = s["title"].format(p=p, n=n)
        s["instruction"] = s["instruction"].format(p=p, n=n)
    return {"kind": kind, "title": "{project} 施工" + ("周报" if kind == "week" else "月报"), "sections": secs}


def validate_template(t: dict, kind: str) -> dict:
    if not isinstance(t, dict) or not isinstance(t.get("sections"), list) or not t["sections"]:
        raise ValueError("模板需包含 sections 列表")
    keys = set()
    out = []
    for s in t["sections"]:
        key = str(s.get("key") or "").strip()
        if not re.fullmatch(r"[a-z_]{2,30}", key) or key in keys:
            raise ValueError(f"栏目键名无效或重复：{key}")
        keys.add(key)
        typ = s.get("type") if s.get("type") in ("narrative", "list") else "narrative"
        out.append({"key": key, "title": str(s.get("title") or key)[:60], "type": typ,
                    "instruction": str(s.get("instruction") or "")[:300], "table": s.get("table") if s.get("table") in TABLES else None,
                    "max_chars": int(s.get("max_chars") or 200), "max_items": int(s.get("max_items") or 5)})
    return {"kind": kind, "title": str(t.get("title") or "{project} 施工报告")[:60], "sections": out}


TABLES = ("progress", "milestones", "output", "safety", "issues", "next")


def system_prompt(tpl: dict) -> str:
    slots = []
    for s in tpl["sections"]:
        if s["type"] == "list":
            slots.append(f'"{s["key"]}": 字符串数组，最多{s["max_items"]}条。要求：{s["instruction"]}')
        else:
            slots.append(f'"{s["key"]}": 不超过{s["max_chars"]}字。要求：{s["instruction"]}')
    return ("你是中国港湾海外项目的资料工程师，按公司报告模板逐栏撰写报告文字。\n"
            "硬性规定：\n1. 只能使用【统计事实】中出现的数字，不得自行计算、推算或编造任何数字。\n"
            "2. 引用日志用[L编号]，引用预警用[W编号]，引用隐患用[S编号]。\n"
            "3. 每栏严格遵守字数与要求，语言正式简洁，可直接报送。\n"
            "4. 产值只写在产值栏（键 output 或挂产值表的栏目），其他栏目一律不提产值、产值金额或产值对比。\n"
            "只输出JSON，键名固定如下：\n{" + ",\n".join(slots) + "}")


def build_prompt(facts: dict) -> str:
    g = lambda v: "未知" if v is None else f"{v:g}"  # noqa: E731
    L = [f"项目：{facts['project']}；周期：{facts['period_start']} ~ {facts['period_end']}",
         f"依据：已确认每日日志{facts['logs']['count']}份；周进度确认{facts['logs'].get('weeks', 0)}份；技术员{facts['logs']['authors']}人，照片{facts['photos']}张。",
         "\n【统计事实】作业进度（代码 名称：本期完成量；期末累计%；期末计划%；偏差百分点）"]
    for p in facts["progress"]:
        qty = "、".join(f"{q['qty']:g}{q['unit']}" for q in p["qty"]) or "未填量"
        split = "".join(f"（其中分包自报{q['self']:g}、港湾核定{q['approved']:g}{q['unit']}）" for q in p["qty"] if "self" in q)
        L.append(f"{_an.fmt(p['code'], p['name'])}：{qty}{split}；累计{g(p['end_pct'])}%；计划{g(p['plan_pct'])}%；偏差{g(p['gap'])}")
    if facts.get("milestones"):
        L.append("关键节点预测（代码 名称 P6日期 预测日期 P80日期 按期概率%）：")
        for m in facts["milestones"]:
            L.append(f"{_an.fmt(m['code'], m['name'])} {m['p6_date']} {m['forecast']} {m['p80']} {m.get('prob_text') or g(m['prob_pct'])}")
    o = facts.get("output") or {}
    if o.get("total") is not None:
        L.append(f"产值（{o.get('currency') or '本币'}）：系统统计{g(o.get('total'))}（内部产值统计，非计量认证；其中分包自报{g(o.get('self_total'))}、港湾核定{g(o.get('approved_total'))}）；记录实际{g(o.get('record_actual'))}；更新产值计划{g(o.get('planned'))}；基准产值计划{g(o.get('baseline'))}；"
                 + "主要作业：" + "；".join(f"{_an.fmt(a['activity_code'], a['name'])} {a['value']:g}" for a in o.get("by_activity", [])[:6]))
    s = facts.get("safety") or {}
    L.append(f"安全：发现隐患{s.get('found', 0)}项（高{s.get('high', 0)}），已闭环{s.get('resolved', 0)}项，未闭环{s.get('open', 0)}项。类别："
             + "；".join(f"{k}{v}项" for k, v in (s.get("by_category") or {}).items()))
    for h in s.get("open_high", [])[:5]:
        L.append(f"[S{h['id']}] 未闭环高风险：{h['title']}（期限{h['due_date']}）")
    q = facts.get("quality") or {}
    L.append(f"质量：质量问题{q.get('issues', 0)}项，质控提醒{q.get('reminders', 0)}项。")
    if facts["labor"]:
        L.append("人员（工种 日均人数）：" + "；".join(f"{x['name']} {x['avg']:g}" for x in facts["labor"]))
    if facts["machinery"]:
        L.append("机械（名称 日均台数）：" + "；".join(f"{x['name']} {x['avg']:g}" for x in facts["machinery"]))
    L.append("\n问题记录：")
    for i in facts["issues"][:25]:
        L.append(f"[L{i['log_id']}] {i['date']} {i['type']}/{i['severity']}：{i['desc']}（措施：{i['measure'] or '未写'}）")
    L.append("\n进度预警：")
    for w in facts["alerts"][:15]:
        L.append(f"[W{w['id']}] {w['level_name']} {w['title']}（{w['status_name']}）")
    L.append("\n施工内容摘录：")
    for w in facts["works"][:40]:
        L.append(f"[L{w['log_id']}] {w['date']} {w['text']}")
    L.append("\n下期P6计划作业：")
    for n in facts["next"][:20]:
        L.append(f"{_an.fmt(n['code'], n['name'])}（{n['start']}~{n['finish']}{'，关键' if n['critical'] else ''}）")
    return "\n".join(L)


_NUM = re.compile(r"(?<![A-Za-z\d])(\d+(?:\.\d+)?)")


def _canon(n: str) -> str:
    try:
        return f"{float(n):g}"
    except ValueError:
        return n


def number_guard(narrative: dict, facts_text: str) -> list[dict]:
    """叙述中出现但统计事实里没有的数字 → 疑似模型自造。引用编号、日期、作业代码、列表序号不算。"""
    allowed = {_canon(n) for n in _NUM.findall(facts_text)}
    issues = []
    for key, text in narrative.items():
        if not isinstance(text, str):
            text = "；".join(text) if isinstance(text, list) else str(text)
        scrub = re.sub(r"\[(?:L|W|S)\d+\]", "", text)
        scrub = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?", "", scrub)
        scrub = re.sub(r"\d{1,2}月\d{1,2}日", "", scrub)
        scrub = re.sub(r"[A-Za-z]+\d+", "", scrub)
        scrub = re.sub(r"(^|\n)\s*\d{1,2}[.、]", r"\1", scrub)
        for n in _NUM.findall(scrub):
            if _canon(n) not in allowed:
                issues.append({"field": key, "number": n})
    return issues


def _as_text(v) -> str:
    """叙述栏的值：字符串原样；模型把它写成数组（如编号条目）时按行拼接，写成对象时拼成“键：值”。"""
    if isinstance(v, list):
        return "\n".join(_as_text(x) for x in v if x not in (None, "")).strip()
    if isinstance(v, dict):
        return "；".join(f"{k}：{_as_text(x)}" for k, x in v.items() if x not in (None, "")).strip()
    return "" if v is None else str(v).strip()


def normalize(data, tpl: dict) -> dict:
    """模型输出 → 按模板各栏的字符串/列表。缺栏留空（template_check 会如实标“栏目为空”），首个叙述栏都没有才算整体失败。
    容忍整体被多包了一层（{"report": {...}}）、叙述栏写成数组或对象。"""
    if isinstance(data, dict):
        keys = {s["key"] for s in tpl["sections"]}
        if not keys & data.keys():
            data = next((v for v in data.values() if isinstance(v, dict) and keys & v.keys()), data)
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    out = {}
    for s in tpl["sections"]:
        v = data.get(s["key"])
        if s["type"] == "list":
            if isinstance(v, str):
                v = [x for x in re.split(r"\n+", v) if x.strip()]
            out[s["key"]] = [str(x).strip() for x in (v if isinstance(v, list) else ([v] if v else [])) if str(x).strip()][: s["max_items"]]
        else:
            out[s["key"]] = _as_text(v)
    first = next((s for s in tpl["sections"] if s["type"] == "narrative"), None)
    if first and not out.get(first["key"]):
        raise ValueError(f"缺少栏目 {first['key']}")
    return out


def template_check(narrative: dict, tpl: dict) -> list[dict]:
    """模板符合性：超字数、缺栏目。"""
    out = []
    for s in tpl["sections"]:
        v = narrative.get(s["key"])
        if s["type"] == "narrative":
            if not v:
                out.append({"field": s["key"], "issue": "栏目为空"})
            elif len(v) > s["max_chars"] * 1.15:
                out.append({"field": s["key"], "issue": f"超出字数要求（{len(v)}/{s['max_chars']}）"})
    return out


def mock(facts: dict, tpl: dict) -> dict:
    behind = [p for p in facts["progress"] if p["gap"] is not None and p["gap"] < 0]
    base = {
        "overview": f"（离线模拟）本期依据日志{facts['logs']['count']}份。",
        "progress": "（离线模拟）" + ("；".join(f"{p['code']}{p['name']}偏差{p['gap']:g}" for p in behind[:3]) or "各作业基本按计划推进。"),
        "milestones": "（离线模拟）关键节点见下表。", "output": "（离线模拟）产值见下表。",
        "safety": f"（离线模拟）发现隐患{(facts.get('safety') or {}).get('found', 0)}项。", "quality": "（离线模拟）未记录质量问题。",
        "issues": "（离线模拟）" + ("\n".join(f"{k + 1}.{i['desc']}[L{i['log_id']}]" for k, i in enumerate(facts["issues"][:3])) or "无"),
        "next_plan": "（离线模拟）" + "；".join(n["name"] for n in facts["next"][:5]), "coordination": [],
    }
    return {s["key"]: base.get(s["key"], "（离线模拟）" if s["type"] == "narrative" else []) for s in tpl["sections"]}
