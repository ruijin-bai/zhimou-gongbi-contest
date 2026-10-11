"""进度Agent的 AI 层：读预测结果与现场日志，提出赶工/纠偏方案；方案效果由 schedule.what_if 复算，不采信模型自报的天数。

出口校验（不过则带原因重试一次，仍不过用系统模板）：
- 代价只写资源（人/台/班），不得出现金额或币种——系统没有成本数据，模型写的金额都是自造的；
- “工效偏低”与预测页“工效偏低作业”表同一阈值（schedule.SLOW_FACTOR），表里没有的作业不得说工效偏低。
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from ..ai.client import call_json
from ..db import all_, dumps, loads, now_iso
from . import facts as F
from . import schedule, slow_diag
from . import act_names as _an

PROMPT_VERSION = "recovery-v2-20261006"

SYSTEM = """你是中国港湾海外项目的计划经理。系统已基于现场实绩用CPM与蒙特卡洛预测了里程碑，部分里程碑可能延误。
请针对预测关键路径上的作业提出2-4条赶工或纠偏措施，每条只针对一个作业，给出“工期系数”duration_factor（0.5-1.0，例如增加一个作业面约0.7）。
要求：
1. 措施须结合给定的日志资源与问题（如增加班组、增加设备、调整工序搭接、解决缺料），写明代价与风险；不要计算日期，日期由系统复算。
2. “代价”只写需要增加的资源，用人/台/班表示（如“增加 1 个班组、2 台挖机”）；系统没有成本数据，不得写任何金额或币种（元、万元、美元、USD 等）。
3. “工效偏低”只能用于【工效判定口径】里列出的作业；清单为空时，不得说任何作业工效偏低。
只输出JSON：{"diagnosis":"一句话诊断","measures":[{"activity_code":"代码","action":"措施","duration_factor":0.7,"cost":"资源代价（人/台/班）","risk":"风险"}]}"""

MONEY_RE = re.compile(r"\d[\d,，.]*\s*(?:万|千|百|亿)?\s*(?:元|美元|美金|欧元|奈拉|人民币|USD|RMB|NGN|EUR)|[$￥¥€]\s*\d|(?:万元|美元|元)\s*/\s*(?:周|天|日|月)",
                      re.IGNORECASE)
SLOW_RE = re.compile(r"工效(?:明显|严重)?(?:偏低|较低|低下|不足|偏慢|较慢|低|慢)|效率(?:偏低|较低|低下|不足)")
_CODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,4}\d{2,6}(?![0-9])")
# “没有偏低”的否定说法须紧贴“工效/效率”（“无明显工效偏低”“没有工效偏低”）；“因无材料导致A1010工效偏低”里的“无”不算
_NEG_RE = re.compile(r"(?:没有|并无|未见|未发现|未出现|不存在|均未|无)(?:明显|严重)?(?:的)?\s*$")


def money_problems(text: str) -> list[str]:
    hit = MONEY_RE.search(text or "")
    return [f"出现金额“{hit.group()}”：系统没有成本数据，代价只能写人/台/班"] if hit else []


def slow_problems(text: str, slow: list[dict], acts: list[dict] | None = None) -> list[str]:
    """“工效偏低”的说法必须与诊断表（同一阈值）一致：表为空不得说偏低；点名的作业必须在表里；表不空不得说“没有偏低”。"""
    codes = {s["code"] for s in slow}
    # 点名作业只认诊断表与关键路径里有的代码（设备型号 PC200 之类的代码样式词忽略）；没给作业清单时按代码样式识别
    known = codes | {a["code"] for a in acts} if acts is not None else None
    out = []
    for clause in re.split(r"[，。；;,\n]", text or ""):
        m = SLOW_RE.search(clause)
        if not m:
            continue
        if _NEG_RE.search(clause[:m.start()]):
            if slow:
                out.append(f"写了“{clause.strip()}”，但诊断表列出了工效偏低作业：{'、'.join(sorted(codes))}")
            continue
        if not slow:
            out.append(f"写了“{m.group()}”，但按系统阈值（工效系数>{schedule.SLOW_FACTOR:g}）没有工效偏低的作业")
            continue
        named = {c for c in _CODE_RE.findall(clause) if known is None or c in known}
        # 先按中文简称认（“43号墩位 墩柱施工”只指一个作业），认过的字去掉，再按 P6 原名认
        rest = clause
        for a in sorted(acts or [], key=lambda x: -len(_an.label(x["code"], x.get("name")))):
            lb = _an.label(a["code"], a.get("name"))
            if lb and lb != a.get("name") and lb in rest:
                named.add(a["code"])
                rest = rest.replace(lb, " ")
        named |= {a["code"] for a in (acts or []) if a.get("name") and a["name"] in rest}
        bad = sorted(c for c in named if c not in codes)
        if bad:
            out.append(f"说 {'、'.join(bad)} 工效偏低，但它不在诊断表（工效系数>{schedule.SLOW_FACTOR:g}）里")
    return out


def build_prompt_rules(slow: list[dict] | None = None) -> str:
    lst = "；".join(f"{_an.fmt(s['code'], s['name'])}（工效系数 {s['productivity_factor']:g}）" for s in slow or []) or "无"
    return (f"【工效判定口径】工效系数（计划工效/实测工效）>{schedule.SLOW_FACTOR:g} 才算工效偏低，与预测页“工效偏低作业”表同一阈值。"
            f"系统判定的工效偏低作业：{lst}")


def _norm(data, valid: set[str]) -> dict:
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    ms = []
    for m in data.get("measures") or []:
        if not isinstance(m, dict) or m.get("activity_code") not in valid:
            continue
        try:
            f = float(m.get("duration_factor"))
        except (TypeError, ValueError):
            continue
        ms.append({"activity_code": m["activity_code"], "action": str(m.get("action") or "")[:200], "duration_factor": min(1.0, max(0.5, f)),
                   "cost": str(m.get("cost") or "")[:120], "risk": str(m.get("risk") or "")[:120]})
    if not ms:
        raise ValueError("没有可用措施")
    return {"diagnosis": str(data.get("diagnosis") or "")[:200], "measures": ms[:4]}


def _texts(d: dict) -> str:
    return "\n".join([d.get("diagnosis", "")] + [f"{m['action']} {m['cost']} {m['risk']}" for m in d.get("measures", [])])


def template(crit: list[dict], slow: list[dict]) -> dict:
    """系统模板：诊断取自诊断表（同一阈值），措施为通用资源投入，代价只写人/台/班。"""
    codes = {s["code"] for s in slow}
    pick = [c for c in crit if c["code"] in codes][:2] or crit[:2]
    if slow:
        diag = "工效偏低作业（系统判定）：" + "、".join(f"{s['code']} {s['name']}" for s in slow[:3]) + "；赶工优先从这些作业和关键路径前段入手"
    else:
        diag = f"没有工效明显偏低的作业（工效系数阈值 {schedule.SLOW_FACTOR:g}），赶工从预测关键路径前段作业入手"
    return {"diagnosis": diag, "measures": [
        {"activity_code": c["code"], "action": "增加一个作业班组或增开一个作业面", "duration_factor": 0.75,
         "cost": "增加 1 个班组的人员及配套机械（人/台/班）", "risk": "工作面受限，需协调交叉作业"} for c in pick]}


def recovery(conn, project_id: int, user_id: int | None) -> dict:
    fc = schedule.latest_forecast(conn, project_id) or schedule.forecast(conn, project_id)
    if not fc:
        raise ValueError("尚未导入 P6 计划")
    late = [m for m in fc["milestones"] if (m["slip_days"] or 0) > 0 or (m["on_time_prob"] is not None and m["on_time_prob"] < 0.6)]
    crit = fc["critical_path"][:15]
    slow = fc.get("slow_activities") or []
    valid = {c["code"] for c in crit}
    since = (date.today() - timedelta(days=7)).isoformat()
    res_lines = []
    for r in all_(conn, "SELECT id, log_date, content_json FROM logs WHERE project_id=? AND log_date>=? AND status IN ('submitted','approved') ORDER BY log_date DESC LIMIT 30", (project_id, since)):
        c = loads(r["content_json"], {})
        lab = "、".join(f"{x['trade']}{x.get('count') or ''}" for x in c.get("labor", [])[:4])
        mac = "、".join(f"{x['name']}{x.get('count') or ''}" for x in c.get("machinery", [])[:4])
        iss = "；".join(i.get("desc", "") for i in c.get("issues", [])[:3])
        res_lines.append(f"L{r['id']} {r['log_date']} 人员:{lab or '未记'} 机械:{mac or '未记'} 问题:{iss or '无'}")
    prompt = ("可能延误的里程碑：\n" + ("\n".join(f"{_an.fmt(m['code'], m['name'])} P6 {m['p6_date']} 预测 {m['forecast']} P80 {m['p80']} 按期概率 {schedule.prob_text(m['on_time_prob'], None, schedule.p50_delay(m))}" for m in late) or "无") +
              "\n\n预测关键路径作业（代码 名称 剩余工期天 工效依据 工效系数>1表示慢于计划）：\n" +
              "\n".join(f"{_an.fmt(c['code'], c['name'])} {c['remaining_d']} {c['basis']} {c['productivity_factor']}" for c in crit) +
              "\n\n" + build_prompt_rules(slow) + "\n\n" + slow_diag.prompt_lines(conn, project_id, slow) +
              "\n\n近7天日志资源与问题：\n" + ("\n".join(res_lines) or "无"))
    tpl = lambda: template(crit, slow)  # noqa: E731

    def call(feedback: str):
        return call_json("progress.recovery", system=SYSTEM, user=prompt + feedback, prompt_version=PROMPT_VERSION, mock=tpl,
                         validate=lambda d: _norm(d, valid), project_id=project_id, user_id=user_id, max_tokens=1500,
                         input_meta={"late": len(late), "critical": len(crit)})
    # 金额（自造）与“工效偏低”口径不一致：带原因重试一次，仍不过用系统模板
    g = F.guarded(call, _texts, None, tpl, check=lambda d: money_problems(_texts(d)) + slow_problems(_texts(d), slow, crit))
    verified = schedule.what_if(conn, project_id, g.data["measures"], fc["as_of"])
    out = {"diagnosis": g.data["diagnosis"], "verified": verified, "forecast_at": fc.get("created_at"), "ai_run_id": g.run_id, "source": g.source,
           "note": "措施由交融大模型提出，工期效果由系统按CPM逻辑复算，模型不直接给出日期；代价只列资源（人/台/班），不估算金额"
           + ("。AI 文字未通过校验，已改用系统模板" if g.source == "template" else "")}
    conn.execute("INSERT OR REPLACE INTO knowledge (project_id, kind, key, value_json, ai_run_id, updated_at) VALUES (?,?,?,?,?,?)",
                 (project_id, "recovery", "latest", dumps(out), g.run_id, now_iso()))
    return out
