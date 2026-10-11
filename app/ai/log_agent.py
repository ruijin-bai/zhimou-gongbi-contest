"""日报智能体：照片 + 口述 + P6 当日作业 + 昨日日报 → 整份结构化日报草稿。

与“逐张识别字段”的区别：
1. 多源联合推理：照片看现场、口述给数量、P6 给作业清单与编码、昨日日报给连续性；
2. 每条内容必须标注依据（P1..Pn 照片 / V 口述 / PLAN 计划 / Y 昨日），没有依据就不写；
3. 工程量只能来自口述或明确可读的标识，照片目测数量被规则层标记为“需核实”；
4. 模型主动列出“需要技术员确认的问题”，技术员只处理被标记的地方。
"""
from __future__ import annotations

import re
from typing import Any

PROMPT_VERSION = "log-v3-20261006"

SECTIONS = ("works", "progress", "labor", "machinery", "materials", "issues", "tomorrow")
ISSUE_TYPES = ("安全", "质量", "材料", "设备", "人员", "天气", "图纸", "外部", "其他")

SYSTEM = """你是中国港湾海外施工项目的资深技术员助理，负责把技术员的语音口述和现场照片整理成规范的《施工日志》，目标是让技术员几乎不用打字。
严格遵守：
1. 只写有依据的内容。依据代号：P1、P2…为照片，V为技术员口述，PLAN为P6计划作业，Y为昨日日志。每条内容在sources中列出依据。
2. 工程量、人数、机械台数只能来自口述(V)；口述没提到的人员和机械，沿用昨日日志(Y)并标注来源Y；不要从照片数人数和机械台数，也不要凭照片估计方量长度。
3. 照片用于说明施工部位与形象进度、发现安全质量问题；照片与口述矛盾时写入uncertain。
4. 施工内容和工程量尽量挂接到给定的P6作业代码；确实无法确定时留空并在uncertain中提问，不要编造代码。
5. pct为该作业累计完成百分比(0-100)，只有口述明确或可由“已完成量/总量”推出时才填，否则填null。
6. 主动发现遗漏：关键作业今天没有进展、口述提到停工/缺料/设备故障、昨日问题今天未交代等，写入uncertain。
7. uncertain 中每个问题都给出2-4个简短选项(options)，让技术员点选即可；最多4个问题，只问真正影响记录准确性的事。
8. 看不清、未提及的内容不要猜；不要输出置信度数字。没有记录的内容只能写“无记录”，不得据此推断“未施工/未进行作业/停工”，除非口述明确这样说。用简洁规范的中文工程语言。只输出一个JSON对象。"""

SCHEMA_HINT = """输出JSON结构：
{
 "summary": "一句话概括当日施工",
 "works": [{"part":"施工部位","process":"工序","desc":"具体内容","activity_code":"P6作业代码或空","sources":["P1","V"]}],
 "progress": [{"activity_code":"代码","qty":数值或null,"unit":"单位","pct":累计百分比或null,"note":"","sources":["V"]}],
 "labor": [{"trade":"工种","count":人数或null,"sources":["V"]}],
 "machinery": [{"name":"机械名称","count":台数或null,"status":"正常/故障/待命","sources":["P2"]}],
 "materials": [{"name":"材料","qty":数值或null,"unit":"单位","action":"进场/消耗/检验","sources":["V"]}],
 "issues": [{"type":"安全|质量|材料|设备|人员|天气|图纸|外部|其他","desc":"问题","severity":"高|中|低","measure":"已采取或建议措施","activity_code":"受影响作业代码或空","sources":["P3"]}],
 "tomorrow": [{"desc":"次日计划","activity_code":"代码或空","sources":["PLAN","V"]}],
 "photo_notes": [{"photo":"P1","caption":"该照片显示的内容，20字内"}],
 "notes": "其他记事",
 "uncertain": [{"field":"相关栏目","question":"需要技术员确认的具体问题","options":["选项1","选项2"]}]
}"""


def build_user_prompt(ctx: dict) -> str:
    lines = [f"日志日期：{ctx['log_date']}；项目：{ctx['project_name']}；技术员：{ctx['author']}；负责范围：{ctx.get('scope') or '全部'}"]
    if ctx.get("weather"):
        lines.append(f"天气：{ctx['weather']}")
    lines.append("\n【技术员口述 V】\n" + (ctx.get("voice_text") or "（无口述）"))
    photos = ctx.get("photos") or []
    if photos:
        lines.append("\n【照片】按顺序附图，编号依次为：" + "、".join(
            f"{p['ref']}(拍摄{p.get('taken_at') or '时间未知'})" for p in photos))
    else:
        lines.append("\n【照片】无")
    acts = ctx.get("activities") or []
    if acts:
        lines.append("\n【PLAN 今日相关P6作业】代码 | 名称 | WBS | 计划开始~完成 | 当前完成% | 总浮时(天) | 关键")
        for a in acts:
            lines.append(f"{a['code']} | {a['name']} | {a.get('wbs_path') or ''} | {a.get('early_start') or '?'}~{a.get('early_finish') or '?'}"
                         f" | {a.get('cur_pct') if a.get('cur_pct') is not None else '未知'} | {a.get('total_float_d') if a.get('total_float_d') is not None else '未知'}"
                         f" | {'是' if a.get('is_critical') else '否'}")
    else:
        lines.append("\n【PLAN】未导入P6计划")
    if ctx.get("yesterday"):
        lines.append("\n【Y 昨日日志摘要】\n" + ctx["yesterday"])
    answered = [q for q in ctx.get("qa") or [] if q.get("answer")]
    if answered:
        lines.append("\n【技术员对上次提问的回答（视同口述V，据此修正日志，不要再问同样问题）】")
        lines += [f"问：{q['question']} 答：{q['answer']}" for q in answered]
    lines.append("\n" + SCHEMA_HINT)
    return "\n".join(lines)


# ----------------------------------------------------------------------------- 校验

def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        return float(m.group()) if m else None


def _sources(v: Any, valid: set[str]) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,，、\s]+", v)
    out = []
    for s in v or []:
        s = str(s).strip().upper()
        if s in valid and s not in out:
            out.append(s)
    return out


def normalize(data: Any, photo_refs: list[str]) -> dict:
    """把模型输出收敛为固定结构；非法依据代号被剔除，数字统一为数值或 None。"""
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    valid = set(photo_refs) | {"V", "PLAN", "Y"}

    def rows(key: str) -> list[dict]:
        val = data.get(key) or []
        return [r for r in val if isinstance(r, dict)] if isinstance(val, list) else []

    def s(r: dict, k: str) -> str:
        v = r.get(k)
        return "" if v is None else str(v).strip()

    out = {
        "summary": str(data.get("summary") or "").strip(),
        "works": [{"part": s(r, "part"), "process": s(r, "process"), "desc": s(r, "desc"),
                   "activity_code": s(r, "activity_code"), "sources": _sources(r.get("sources"), valid)} for r in rows("works")],
        "progress": [{"activity_code": s(r, "activity_code"), "qty": _num(r.get("qty")), "unit": s(r, "unit"),
                      "pct": _num(r.get("pct")), "note": s(r, "note"), "sources": _sources(r.get("sources"), valid)}
                     for r in rows("progress") if s(r, "activity_code")],
        "labor": [{"trade": s(r, "trade"), "count": _num(r.get("count")), "sources": _sources(r.get("sources"), valid)}
                  for r in rows("labor") if s(r, "trade")],
        "machinery": [{"name": s(r, "name"), "count": _num(r.get("count")), "status": s(r, "status") or "正常",
                       "sources": _sources(r.get("sources"), valid)} for r in rows("machinery") if s(r, "name")],
        "materials": [{"name": s(r, "name"), "qty": _num(r.get("qty")), "unit": s(r, "unit"), "action": s(r, "action"),
                       "sources": _sources(r.get("sources"), valid)} for r in rows("materials") if s(r, "name")],
        "issues": [{"type": s(r, "type") if s(r, "type") in ISSUE_TYPES else "其他", "desc": s(r, "desc"),
                    "severity": s(r, "severity") if s(r, "severity") in ("高", "中", "低") else "中",
                    "measure": s(r, "measure"), "activity_code": s(r, "activity_code"),
                    "sources": _sources(r.get("sources"), valid)} for r in rows("issues") if s(r, "desc")],
        "tomorrow": [{"desc": s(r, "desc"), "activity_code": s(r, "activity_code"), "sources": _sources(r.get("sources"), valid)}
                     for r in rows("tomorrow") if s(r, "desc")],
        "photo_notes": [{"photo": s(r, "photo").upper(), "caption": s(r, "caption")} for r in rows("photo_notes")
                        if s(r, "photo").upper() in photo_refs],
        "notes": str(data.get("notes") or "").strip(),
        "uncertain": [{"field": s(r, "field"), "question": s(r, "question"),
                       "options": [str(o).strip()[:30] for o in (r.get("options") or []) if str(o).strip()][:4]}
                      for r in rows("uncertain") if s(r, "question")][:4],
    }
    if not (out["works"] or out["progress"] or out["summary"]):
        raise ValueError("草稿没有任何施工内容")
    return out


def empty_content() -> dict:
    return {"summary": "", "works": [], "progress": [], "labor": [], "machinery": [], "materials": [], "issues": [],
            "tomorrow": [], "photo_notes": [], "notes": "", "uncertain": []}


# ----------------------------------------------------------------------------- 离线模拟（仅开发测试）

_QTY = re.compile(r"(\d+(?:\.\d+)?)\s*(方|m3|立方米|米|m|吨|t|根|片|个|榀|延米|平方米|m2|平方)")
_PEOPLE = re.compile(r"([一-龥]{1,4}工)\s*(\d+)\s*(?:人|名)")
_MACH = re.compile(r"(泵车|挖机|挖掘机|吊车|汽车吊|履带吊|装载机|压路机|罐车|搅拌车|旋挖钻|振动锤|发电机)\s*(\d+)?\s*台?")
_PCT = re.compile(r"(?:完成|累计)\s*(\d{1,3}(?:\.\d+)?)\s*%")


def mock_draft(ctx: dict) -> dict:
    voice = ctx.get("voice_text") or ""
    acts = ctx.get("activities") or []
    photos = [p["ref"] for p in ctx.get("photos") or []]

    def match_act(text: str) -> dict | None:
        best, score = None, 0
        for a in acts:
            name = a["name"]
            common = sum(1 for ch in set(name) if ch in text and "一" <= ch <= "龥")
            if a["code"] in text:
                common += 10
            if common > score:
                best, score = a, common
        return best if score >= 2 else None

    voice = re.sub(r"\[\d{1,2}:\d{2}\]\s*", "", voice)
    sentences = [s.strip() for s in re.split(r"[。；;\n]", voice) if s.strip()]
    works, progress, materials, issues, tomorrow = [], [], [], [], []
    for sent in sentences:
        act = match_act(sent)
        code = act["code"] if act else ""
        if any(k in sent for k in ("明天", "明日", "次日")):
            tomorrow.append({"desc": sent, "activity_code": code, "sources": ["V"]})
            continue
        if any(k in sent for k in ("停工", "故障", "缺料", "未到", "下雨", "暴雨", "隐患", "没到")):
            issues.append({"type": "设备" if "故障" in sent else ("天气" if "雨" in sent else "材料"), "desc": sent,
                           "severity": "中", "measure": "", "activity_code": code, "sources": ["V"]})
            continue
        works.append({"part": act["wbs_path"].split(" / ")[-1] if act and act.get("wbs_path") else "",
                      "process": act["name"] if act else "", "desc": sent, "activity_code": code,
                      "sources": ["V"] + photos[:1]})
        q = _QTY.search(sent)
        p = _PCT.search(sent)
        if code and (q or p):
            progress.append({"activity_code": code, "qty": float(q.group(1)) if q else None, "unit": q.group(2) if q else "",
                             "pct": float(p.group(1)) if p else None, "note": "", "sources": ["V"]})
        if q and any(k in sent for k in ("混凝土", "钢筋", "砂", "碎石", "水泥")):
            materials.append({"name": sent[:8], "qty": float(q.group(1)), "unit": q.group(2), "action": "消耗", "sources": ["V"]})
    labor = [{"trade": t, "count": float(n), "sources": ["V"]} for t, n in _PEOPLE.findall(voice)]
    machinery = [{"name": m, "count": float(n) if n else None, "status": "正常", "sources": ["V"]} for m, n in _MACH.findall(voice)]
    y = ctx.get("yesterday_content") or {}
    if not labor:
        labor = [dict(x, sources=["Y"]) for x in y.get("labor", [])]
    if not machinery:
        machinery = [dict(x, sources=["Y"]) for x in y.get("machinery", [])]
    if not works and photos:
        works.append({"part": "", "process": "", "desc": "现场施工（离线模拟：未调用真实模型，照片内容未识别）",
                      "activity_code": "", "sources": photos})
    for a in acts:
        if a.get("is_critical") and not any(p["activity_code"] == a["code"] for p in progress):
            tomorrow.append({"desc": f"推进{a['name']}", "activity_code": a["code"], "sources": ["PLAN"]})
            break
    return {
        "summary": (sentences[0] if sentences else "现场施工") + "（离线模拟草稿）",
        "works": works, "progress": progress, "labor": labor, "machinery": machinery, "materials": materials,
        "issues": issues, "tomorrow": tomorrow,
        "photo_notes": [{"photo": r, "caption": "离线模拟：未识别"} for r in photos],
        "notes": "", "uncertain": ([] if works else [{"field": "works", "question": "未识别到施工内容，请补充", "options": []}]) + [
            {"field": "progress", "question": f"关键作业 {a['code']}「{a['name']}」今天有进展吗？", "options": ["有进展，按计划", "停工", "未开始"]}
            for a in acts if a.get("is_critical") and a.get("cur_pct") and not any(p["activity_code"] == a["code"] for p in progress)
            and not any(q.get("answer") and a["code"] in q.get("question", "") for q in ctx.get("qa") or [])][:2],
    }


# ============================================================================ 周确认

WEEK_PROMPT_VERSION = "week-v2-20261006"

WEEK_SYSTEM = """你是中国港湾海外施工项目的技术员助理。技术员本周每天随手记了语音和照片，系统已整理成每日施工日志。请把本周汇总成《周进度确认单》，供技术员一次性核对提交，并回填P6进度。
严格遵守：
1. 依据代号：D1~D7为本周每日日志（按日期顺序），N为随手记原文，V为技术员本次补充说明，PLAN为P6计划作业，Y为上周确认单。每条内容在sources中列出依据。
2. 本周工程量(progress.qty)必须等于每日日志中该作业数量之和；系统已给出【每日记录合计】，直接采用，不得自行增减；只有技术员补充说明(V)明确更正时才可改动，并在note中说明。
3. pct为截至本周末的累计完成百分比：优先取每日日志中最后一次明确的累计%；无法确定时填null并在uncertain中提问。
4. labor/machinery写本周日均投入（取每日记录平均，系统已给出），machinery状态有故障要写明。
5. issues汇总本周问题，同一问题合并，写明是否已解决；tomorrow写下周计划（结合PLAN中下周作业和技术员口述）。
6. 主动发现：PLAN中本周应进行的关键作业本周没有任何记录、累计%与工程量明显不匹配、问题未闭环等，写入uncertain并给2-4个选项。最多4个问题。
7. 不要编造未记录的数量和事件。某天或某作业“（无记录）”只能写“无记录”，不得推断为“未施工/未进行作业/停工”，除非原文明确这样说。只输出一个JSON对象，结构与日志相同。"""


def build_week_prompt(ctx: dict) -> str:
    L = [f"周期：{ctx['week_start']} ~ {ctx['week_end']}；项目：{ctx['project_name']}；技术员：{ctx['author']}；负责范围：{ctx.get('scope') or '全部'}"]
    L.append("\n【每日日志】")
    for d in ctx["days"]:
        L.append(f"{d['ref']} {d['date']}：" + (d["summary"] or "（无记录）"))
    if ctx.get("notes"):
        L.append("\n【N 随手记原文】")
        L += [f"{n['date']} {n['time']} {n['text']}" for n in ctx["notes"][:80]]
    L.append("\n【每日记录合计（系统计算，直接采用）】作业代码 | 本周合计 | 单位 | 最后一次累计% | 记录天数")
    for code, v in ctx["sums"].items():
        L.append(f"{code} | {v['qty']:g} | {v['unit']} | {v['last_pct'] if v['last_pct'] is not None else '未知'} | {v['days']}")
    if ctx.get("labor_avg"):
        L.append("日均人员：" + "；".join(f"{k} {v:g}" for k, v in ctx["labor_avg"].items()))
    if ctx.get("mach_avg"):
        L.append("日均机械：" + "；".join(f"{k} {v:g}" for k, v in ctx["mach_avg"].items()))
    if ctx.get("voice_text"):
        L.append("\n【V 技术员补充说明】\n" + ctx["voice_text"])
    acts = ctx.get("activities") or []
    if acts:
        L.append("\n【PLAN 本周及下周P6作业】代码 | 名称 | 计划开始~完成 | 上周末完成% | 总浮时(天) | 关键")
        for a in acts:
            L.append(f"{a['code']} | {a['name']} | {a.get('early_start') or '?'}~{a.get('early_finish') or '?'} | "
                     f"{a.get('cur_pct') if a.get('cur_pct') is not None else '未知'} | {a.get('total_float_d') if a.get('total_float_d') is not None else '未知'} | {'是' if a.get('is_critical') else '否'}")
    if ctx.get("yesterday"):
        L.append("\n【Y 上周确认单摘要】\n" + ctx["yesterday"])
    answered = [q for q in ctx.get("qa") or [] if q.get("answer")]
    if answered:
        L.append("\n【技术员对提问的回答（视同V）】")
        L += [f"问：{q['question']} 答：{q['answer']}" for q in answered]
    L.append("\n" + SCHEMA_HINT.replace("次日计划", "下周计划"))
    return "\n".join(L)


def mock_week(ctx: dict) -> dict:
    refs = {d["date"]: d["ref"] for d in ctx["days"]}
    works = []
    for d in ctx["days"]:
        for w in d["content"].get("works", [])[:3]:
            works.append({**{k: w.get(k, "") for k in ("part", "process", "desc", "activity_code")}, "sources": [refs[d["date"]]]})
    progress = [{"activity_code": c, "qty": v["qty"], "unit": v["unit"], "pct": v["last_pct"], "note": "", "sources": v["refs"]}
                for c, v in ctx["sums"].items()]
    issues = []
    for d in ctx["days"]:
        for i in d["content"].get("issues", []):
            issues.append({**{k: i.get(k, "") for k in ("type", "desc", "severity", "measure", "activity_code")}, "sources": [refs[d["date"]]]})
    acts = ctx.get("activities") or []
    unc = [{"field": "progress", "question": f"关键作业 {a['code']}「{a['name']}」本周没有记录，是否停工？", "options": ["本周停工", "有施工但漏记", "尚未开始"]}
           for a in acts if a.get("is_critical") and a["code"] not in ctx["sums"] and a.get("early_start") and a["early_start"] <= ctx["week_end"]][:2]
    return {"summary": f"（离线模拟）本周记录 {sum(1 for d in ctx['days'] if d['summary'])} 天", "works": works[:12], "progress": progress,
            "labor": [{"trade": k, "count": v, "sources": list(refs.values())[:1]} for k, v in ctx.get("labor_avg", {}).items()],
            "machinery": [{"name": k, "count": v, "status": "正常", "sources": list(refs.values())[:1]} for k, v in ctx.get("mach_avg", {}).items()],
            "materials": [], "issues": issues[:8],
            "tomorrow": [{"desc": f"推进{a['name']}", "activity_code": a["code"], "sources": ["PLAN"]} for a in acts if a.get("early_start", "") > ctx["week_end"]][:3],
            "photo_notes": [], "notes": "", "uncertain": unc}


# ============================================================================ “没有记录”不等于“没施工”

# 由“没有记录”推断“没施工/停工”的说法；只有口述/随手记原文明确这样说时才保留
_NO_WORK = re.compile(r"未(?:进行|开展)[^，。；;,]{0,20}?(?:施工|作业)|没有(?:进行)?(?:任何)?(?:施工|作业)|未(?:施工|开工|作业)|没(?:施工|开工|干活)|无施工(?!记录)|停工|未进场")
_EXPLICIT = re.compile(r"停工|未施工|没施工|没有施工|未作业|没干|休息|放假|未开工|没开工|暂停|歇工|未进场|没进场|未进行|未开展")


def _scrub(text: str) -> str:
    parts = re.split(r"(?<=[，。；;,])", text or "")
    kept = [p for p in parts if not _NO_WORK.search(p)]
    return "".join(kept).rstrip("，；;, ")


def scrub_inferred(content: dict, source_text: str) -> dict:
    """出口校验：依据原文（口述/随手记）没有明确说停工、未施工时，删去“未进行实际作业”类推断；摘要删空则写“当日无记录”。"""
    if _EXPLICIT.search(source_text or ""):
        return content
    out = dict(content)
    if _NO_WORK.search(out.get("summary") or ""):
        out["summary"] = _scrub(out["summary"]) or "当日无记录"
    if _NO_WORK.search(out.get("notes") or ""):
        out["notes"] = _scrub(out["notes"])
    return out
