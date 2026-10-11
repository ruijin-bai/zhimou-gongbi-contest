"""“问工笔”项目问答智能体：两步式工具调用。

第1步：模型把自然语言问题规划为检索指令（日期范围、作业、关键词、要查的数据类型）；
第2步：系统按指令从日报/P6/预警中检索事实；
第3步：模型只依据检索到的事实作答，并用 [L12]/[W3]/[A1010] 标注出处，查不到就说查不到。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

PROMPT_VERSION = "qa-v1-20261005"

PLAN_SYSTEM = """你是施工项目数据检索规划器。把用户问题转换为检索指令，只输出JSON：
{"date_from":"YYYY-MM-DD或空","date_to":"YYYY-MM-DD或空","activity_codes":["P6作业代码"],"keywords":["关键词"],
 "need":["logs","progress","alerts","plan"]}
今天是{today}。“上周”“本月”“最近”等请换算成具体日期。关键词取问题中的部位、工序、材料、设备、问题类型等名词。"""

ANSWER_SYSTEM = """你是施工项目助理“工笔”。只依据【检索结果】回答用户问题：
1. 每个事实后用方括号标注出处：日志[L编号]、预警[W编号]、P6作业[作业代码]、整改/问题台账[F编号]、周月报[R编号]；没有编号的事实按其行首标签标注，如[产值]、[简报]。
2. 检索结果不足以回答时，明确说“现有记录中没有找到……”，并建议需要补充什么记录，不要猜测。
3. 回答简洁，先给结论再列依据。只输出JSON：{"answer":"回答正文","refs":["L12","W3","A1010"]}"""


def plan_prompt(question: str, codes_hint: list[str]) -> str:
    hint = "，".join(codes_hint[:80])
    return f"用户问题：{question}\n可用P6作业（代码 名称）：{hint}"


def normalize_plan(data, valid_codes: set[str]) -> dict:
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")

    def d(v):
        try:
            return date.fromisoformat(str(v)[:10]).isoformat()
        except ValueError:
            return None
    need = [n for n in data.get("need") or [] if n in ("logs", "progress", "alerts", "plan")] or ["logs", "alerts"]
    return {"date_from": d(data.get("date_from") or ""), "date_to": d(data.get("date_to") or ""),
            "activity_codes": [c for c in data.get("activity_codes") or [] if c in valid_codes][:10],
            "keywords": [str(k).strip() for k in data.get("keywords") or [] if str(k).strip()][:8], "need": need}


def normalize_answer(data) -> dict:
    if not isinstance(data, dict) or not str(data.get("answer") or "").strip():
        raise ValueError("缺少answer")
    refs = data.get("refs")
    refs = [str(r).strip("[] ") for r in (refs if isinstance(refs, list) else [refs] if refs else [])]
    return {"answer": str(data["answer"]).strip(), "refs": refs}


_REF = re.compile(r"\[([A-Z]+\d+|产值|简报)\]")


def fallback_answer(text: str) -> dict:
    """降级：模型直接用自然语言作答而没按 JSON 输出时，取其正文作为回答，出处从正文里的 [L12] 等标注提取。
    看起来是被截断的 JSON 时不降级（避免把半截 JSON 展示给人）。"""
    t = re.sub(r"<think>.*?(?:</think>|$)", "", text or "", flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    if not t:
        raise ValueError("空输出")
    if t.startswith(("{", "[")):
        m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', t, flags=re.S)
        if not m:
            raise ValueError("看起来是不完整的JSON")
        try:
            t = json.loads('"' + m.group(1) + '"')
        except ValueError:
            raise ValueError("看起来是不完整的JSON") from None
    return {"answer": t[:1500], "refs": list(dict.fromkeys(_REF.findall(t)))[:8]}


_TAG = re.compile(r"\[([^\[\]\s]{1,16})\]")


def verify_answer(ans: dict, facts_text: str, question: str, n_facts: int) -> tuple[dict, list[str]]:
    """出口校验：出处只能是检索结果里出现过的编号（不存在的编号从出处和正文里删去）；
    正文数字必须出自检索结果或问题本身（计数类小整数 ≤ 检索条数也认）。数字对不上返回问题清单，由调用方改用规则回答。"""
    from .report_agent import number_guard   # 与报表同一套数字守卫
    known = set(_TAG.findall(facts_text))
    bad = [r for r in dict.fromkeys(ans["refs"] + _REF.findall(ans["answer"])) if r not in known]
    text = ans["answer"]
    for r in bad:
        text = text.replace(f"[{r}]", "")
    refs = [r for r in dict.fromkeys(ans["refs"]) if r in known]
    allowed = "\n".join([facts_text, question, " ".join(str(i) for i in range(n_facts + 1))])
    problems = [f"出处 {r} 不在检索结果里" for r in bad]
    nums = [i["number"] for i in number_guard({"answer": text}, allowed)]
    problems += [f"数字 {n} 在记录里查不到" for n in dict.fromkeys(nums)]
    return {"answer": text.strip(), "refs": refs}, ([] if not nums else problems)


def rule_answer(facts_text: str, reason: str) -> dict:
    """数字核对不过时的规则回答：不复述模型的话，只列出检索到的相关记录，点出处看原文。"""
    refs = [r for r in dict.fromkeys(re.findall(r"^\[([^\]\s]+)\]", facts_text, flags=re.M))][:6]
    if not refs:
        return {"answer": f"AI 回答未通过核对（{reason}），现有记录中也没有找到相关内容。", "refs": []}
    return {"answer": f"AI 回答未通过核对（{reason}），以下是检索到的相关记录，请点出处查看原文：" + "、".join(f"[{r}]" for r in refs), "refs": refs}


def mock_plan(question: str, acts: list[dict]) -> dict:
    today = date.today()
    df = None
    if "上周" in question:
        start = today - timedelta(days=today.weekday() + 7)
        df, dt = start.isoformat(), (start + timedelta(days=6)).isoformat()
    elif "本周" in question:
        df, dt = (today - timedelta(days=today.weekday())).isoformat(), today.isoformat()
    else:
        df, dt = (today - timedelta(days=30)).isoformat(), today.isoformat()
    codes = [a["code"] for a in acts if a["code"] in question or (len(a["name"]) >= 2 and a["name"][:4] in question)]
    words = [w for w in re.findall(r"[一-龥]{2,4}", question) if w not in ("上周", "本周", "为什么", "什么", "多少", "情况", "现在")]
    return {"date_from": df, "date_to": dt, "activity_codes": codes[:5], "keywords": words[:5], "need": ["logs", "progress", "alerts", "plan"]}


def mock_answer(question: str, facts_text: str) -> dict:
    refs = list(dict.fromkeys(re.findall(r"\[(L\d+|W\d+|F\d+|R\d+|[A-Z]+\d+)\]", facts_text)))[:6]
    if not refs:
        return {"answer": "（离线模拟）现有记录中没有找到与该问题相关的内容。", "refs": []}
    return {"answer": "（离线模拟）检索到以下相关记录：" + "、".join(f"[{r}]" for r in refs), "refs": refs}
