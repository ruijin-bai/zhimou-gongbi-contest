"""计划体检智能体：读 plan_insight.health 的检查结果，写一句诊断和 2-4 条整改建议。

检查项、数量、作业清单由系统计算；模型只写文字。出口校验（不过则带原因重试一次，仍不过用系统模板）：
- 建议只能针对未通过的检查项（key 必须在清单里）；
- 文字中的数字必须出现在给模型的检查结果里（作业代码中的数字除外），不得自造数字；
- 不得出现金额、产值。
"""
from __future__ import annotations

import re

from ..ai.client import call_json
from ..db import dumps, loads, now_iso, one
from . import facts as F
from . import plan_insight
from . import act_names as _an

PROMPT_VERSION = "plan-health-v1-20261008"

SYSTEM = """你是中国港湾海外项目的计划工程师，负责审核 P6 进度计划的编制质量。系统已按 DCMA 14 点检查算出各项结果。
请据此写：一句话总体诊断；针对未通过的检查项给出 2-4 条整改建议（先改最影响关键路径与节点判断的）。
要求：
1. 只针对清单里“未通过”的检查项，每条建议写明改什么、为什么（对预测与预警的影响）；可点名清单中给出的作业代码；
2. 文中出现的数字只能来自检查结果，不要自己计算新数字；不要写日期推算；
3. 不涉及产值、费用、金额；
只输出JSON：{"diagnosis":"一句话诊断","advice":[{"key":"检查项key","text":"建议"}]}"""

_CODE = re.compile(r"[A-Z]{2,5}-?\d{2,6}")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_MONEY = re.compile(r"产值|费用|金额|成本|[元$￥¥€]|美元|USD|NGN|奈拉")


def _prompt(h: dict) -> str:
    lines = [f"数据日期 {h['data_date']}，作业 {h['activities']} 项，逻辑关系 {h['relations']} 条。"]
    for c in h["checks"]:
        state = {"bad": "未通过", "warn": "需关注", "ok": "通过"}[c["level"]]
        eg = "；".join(f"{_an.fmt(s['code'], s['name'])}" + (f"（{s['note']}）" if s.get("note") else "") for s in c["sample"][:5])
        lines.append(f"[{c['key']}] {c['title']}：{state}，{c['count']} 项（占 {c['share']:g}%）；规则：{c['rule']}；影响：{c['why']}" + (f"；例：{eg}" if eg else ""))
    return "\n".join(lines)


def _norm(data, keys: set[str]) -> dict:
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    adv = [{"key": a["key"], "text": str(a.get("text") or "")[:220]} for a in data.get("advice") or []
           if isinstance(a, dict) and a.get("key") in keys and str(a.get("text") or "").strip()]
    if not adv and keys:
        raise ValueError("没有可用建议")
    return {"diagnosis": str(data.get("diagnosis") or "")[:200], "advice": adv[:4]}


def _texts(d: dict) -> str:
    return "\n".join([d.get("diagnosis", "")] + [a["text"] for a in d.get("advice", [])])


def problems(text: str, prompt: str) -> list[str]:
    out = []
    allowed = {float(x) for x in _NUM.findall(_CODE.sub(" ", prompt))}
    for x in _NUM.findall(_CODE.sub(" ", text or "")):
        if float(x) not in allowed:
            out.append(f"数字 {x} 不在检查结果里")
    hit = _MONEY.search(text or "")
    if hit:
        out.append(f"出现“{hit.group()}”：计划体检不涉及产值与费用")
    return list(dict.fromkeys(out))


def template(h: dict) -> dict:
    bad = [c for c in h["checks"] if c["level"] != "ok"]
    if not bad:
        return {"diagnosis": "各项检查均通过，计划逻辑完整，可直接用于预测与预警。", "advice": []}
    top = bad[0]
    diag = f"共 {h['summary']['bad']} 项未通过、{h['summary']['warn']} 项需关注，最突出的是“{top['title']}”（{top['count']} 项）。"
    adv = []
    for c in bad[:4]:
        eg = "、".join(s["code"] for s in c["sample"][:3])
        adv.append({"key": c["key"], "text": f"{c['title']} {c['count']} 项：{c['why']}。" + (f"可先核对 {eg}。" if eg else "")})
    return {"diagnosis": diag, "advice": adv}


def run(conn, project_id: int, user_id: int | None) -> dict:
    h = plan_insight.health(conn, project_id)
    if h.get("none"):
        raise ValueError("尚未导入 P6 计划")
    keys = {c["key"] for c in h["checks"] if c["level"] != "ok"}
    prompt = _prompt(h)
    tpl = lambda: template(h)  # noqa: E731

    def call(feedback: str):
        return call_json("progress.health", system=SYSTEM, user=prompt + feedback, prompt_version=PROMPT_VERSION, mock=tpl,
                         validate=lambda d: _norm(d, keys), project_id=project_id, user_id=user_id, max_tokens=1200,
                         input_meta={"import_id": h["import_id"], "failed": len(keys)})
    if keys:
        g = F.guarded(call, _texts, None, tpl, check=lambda d: problems(_texts(d), prompt))
        data, run_id, source = g.data, g.run_id, g.source
    else:
        data, run_id, source = tpl(), None, "template"
    titles = {c["key"]: c["title"] for c in h["checks"]}
    for a in data["advice"]:
        a["title"] = titles.get(a["key"], a["key"])
    out = {**data, "import_id": h["import_id"], "data_date": h["data_date"], "ai_run_id": run_id, "source": source, "created_at": now_iso(),
           "note": "检查项与数量由系统计算，诊断与建议由交融大模型撰写" + ("（AI 文字未通过校验，已改用系统模板）" if source == "template" and keys else "")}
    conn.execute("INSERT OR REPLACE INTO knowledge (project_id, kind, key, value_json, ai_run_id, updated_at) VALUES (?,?,?,?,?,?)",
                 (project_id, "plan_health", "latest", dumps(out), run_id, now_iso()))
    return out


def latest(conn, project_id: int) -> dict | None:
    row = one(conn, "SELECT value_json FROM knowledge WHERE project_id=? AND kind='plan_health' AND key='latest'", (project_id,))
    return loads(row["value_json"], None) if row else None
