"""事实底稿：各 Agent 说同一件事时共用的确定性事实，以及 AI 文字与底稿一致性的出口校验。

原则：
- 底稿只汇总已有计算结果（进度预警判定、P6 数据日期、日志累计完成率、里程碑模拟），不新造口径，不改统计；
- 底稿以文字注入提示词，要求 AI 不得与之矛盾；
- 输出后用 check_text 复核：天数、百分比必须出自底稿；有未关闭进度预警的作业不得出现“按计划/正常推进”等措辞；
  “P6 没更新”类提醒只在底稿明确标注待同步时才允许；数量（N 项/条/处）用 count_problems 按含义核对；
- 预警依据统一写“判定日 X · P6 数据日期 Y”（judged_on）；AI 文字引用的预警用 alert_ref 留存、alert_ref_problem 复核；
- 校验不过则带着原因重试一次，仍不过用确定性模板替代并标注（guarded）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from ..ai.client import AIError
from ..db import all_, loads
from . import p6, schedule
from .logs import progress_state
from .progress import RULE_TITLES
from . import act_names as _an

log = logging.getLogger(__name__)

LEVEL_NAME = {"red": "红色", "orange": "橙色", "yellow": "黄色"}
LAG_RULES = ("behind", "late_start")  # 这两类规则的 slip_days 是“滞后天数”


def _n(v) -> str:
    return f"{v:g}"


# ============================================================================ 取底稿

def context(conn, project_id: int) -> dict:
    """一次取齐：当前 P6 批次、作业、日志累计进度、最近一次里程碑预测。循环取多个作业时复用。"""
    _an.bind(conn, project_id)   # 底稿里作业写“简称（代码）”
    imp = p6.current_import(conn, project_id)
    fc = schedule.latest_forecast(conn, project_id)
    return {"project_id": project_id, "imp": imp,
            "acts": {a["code"]: a for a in p6.current_activities(conn, project_id)} if imp else {},
            "state": progress_state(conn, project_id, imp) if imp else {},
            "forecast": fc, "ms": {m["code"]: m for m in (fc or {}).get("milestones", [])}}


def _alert_part(row: dict) -> dict:
    """一条预警行 → 事实（只读行内已存的判定结果，与预警标题同源）。"""
    d = loads(row["detail_json"], {}) or {}
    slip = d.get("slip_days") if row["rule"] in LAG_RULES else None
    left = d.get("float_left_d")
    return {"id": row["id"], "level": row["level"], "rule": row["rule"], "rule_name": RULE_TITLES.get(row["rule"], row["rule"]),
            "as_of": row["as_of"], "import_id": row.get("import_id"), "slip_days": slip, "float_total": d.get("total_float_d"), "float_left": left,
            "over_float": round(-left, 1) if left is not None and left < 0 else None,
            "planned_pct": d.get("planned_pct"), "actual_pct": d.get("actual_pct"), "actual_source": d.get("actual_source"),
            "early_finish": d.get("early_finish"), "forecast_finish": d.get("forecast_finish"), "idle_days": d.get("idle_days"),
            "milestones": d.get("milestones") or []}


def import_date(conn, import_id, ctx: dict | None = None) -> str | None:
    """预警判定所用 P6 批次的数据日期（预警行的 import_id 随每次判定更新；与当前批次相同时不再查库）。"""
    if not import_id:
        return None
    imp = (ctx or {}).get("imp")
    if imp and imp["id"] == import_id:
        return imp["data_date"]
    row = all_(conn, "SELECT data_date FROM p6_imports WHERE id=?", (import_id,))
    return row[0]["data_date"] if row else None


def judged_on(as_of: str | None, p6_date: str | None) -> str:
    """预警依据的统一写法：判定日 X · P6 数据日期 Y（推送正文、事实底稿共用）。"""
    return f"判定日 {as_of}" + (f" · P6 数据日期 {p6_date}" if p6_date else "")


def activity_facts(conn, project_id: int, code: str, ctx: dict | None = None) -> dict:
    """某个作业的确定性事实。has_open_alert 为真时，任何“按计划/正常推进”的说法都与底稿矛盾。"""
    ctx = ctx or context(conn, project_id)
    act = ctx["acts"].get(code) or {}
    rows = all_(conn, "SELECT * FROM alerts WHERE project_id=? AND activity_code=? AND status='open' AND rule<>'stale' ORDER BY id", (project_id, code))
    parts = [_alert_part(r) for r in rows]
    primary = next((p for p in parts if p["rule"] in LAG_RULES), parts[0] if parts else None)
    st = ctx["state"].get(code) or {}
    imp = ctx["imp"]
    ms = []
    for m in (primary or {}).get("milestones", []):
        fm = ctx["ms"].get(m["code"])
        ms.append({"code": m["code"], "name": m.get("name"), "p6_date": (fm or {}).get("p6_date") or m.get("planned"),
                   "forecast": (fm or {}).get("forecast"), "p50": (fm or {}).get("p50"), "on_time_prob": (fm or {}).get("on_time_prob") if fm else None})
    today = date.today().isoformat()
    pct_log, pct_p6 = st.get("pct"), act.get("phys_pct")
    return {
        "kind": "activity", "code": code, "name": act.get("name") or (rows[0]["activity_name"] if rows else ""),
        "has_open_alert": bool(parts), "alert_ids": [p["id"] for p in parts], "alert_id": primary["id"] if primary else None,
        "level": primary["level"] if primary else None, "rule": primary["rule"] if primary else None,
        "rule_name": primary["rule_name"] if primary else None, "as_of": primary["as_of"] if primary else None,
        "alert_p6_date": import_date(conn, primary["import_id"], ctx) if primary else None,
        "slip_days": primary["slip_days"] if primary else None, "float_total": primary["float_total"] if primary else act.get("total_float_d"),
        "float_left": primary["float_left"] if primary else None, "over_float": primary["over_float"] if primary else None,
        "idle_days": primary["idle_days"] if primary else None,
        "planned_pct": primary["planned_pct"] if primary else None,
        "completion_pct": primary["actual_pct"] if primary and primary["actual_pct"] is not None else (pct_log if pct_log is not None else pct_p6),
        "pct_log": pct_log, "pct_log_date": st.get("pct_date"), "pct_p6": pct_p6, "p6_data_date": imp["data_date"] if imp else None,
        "early_finish": act.get("early_finish"), "forecast_finish": primary["forecast_finish"] if primary else None,
        "past_finish": bool(act.get("early_finish") and act["early_finish"] < today and not act.get("act_finish")),
        "milestones": ms, "iterations": (ctx["forecast"] or {}).get("iterations"), "p6_sync_due": False,
    }


def project_facts(conn, project_id: int, ctx: dict | None = None, limit: int = 5) -> dict:
    """项目层事实：未关闭预警数量与头部作业、里程碑按期概率（缺值写未知，不当作 0）、P6 数据日期。"""
    ctx = ctx or context(conn, project_id)
    cnt = {r["level"]: r["n"] for r in all_(conn, "SELECT level, COUNT(*) n FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' GROUP BY level", (project_id,))}
    lag = [activity_facts(conn, project_id, r["activity_code"], ctx) for r in all_(
        conn, "SELECT activity_code FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' AND level IN ('red','orange') ORDER BY level DESC, id LIMIT ?", (project_id, limit))]
    fc = ctx["forecast"] or {}
    return {"kind": "project", "p6_data_date": ctx["imp"]["data_date"] if ctx["imp"] else None,
            "alerts": {k: cnt.get(k, 0) for k in ("red", "orange", "yellow")}, "lag_alerts": lag,
            "milestones": [{"code": m["code"], "name": m["name"], "p6_date": m["p6_date"], "forecast": m["forecast"], "p50": m.get("p50"), "p80": m.get("p80"),
                            "on_time_prob": m["on_time_prob"]} for m in fc.get("milestones", [])[:6]],
            "iterations": fc.get("iterations"), "project_finish": fc.get("project_finish"), "forecast_at": fc.get("created_at")}


# ============================================================================ 渲染成提示词

def _prob(m: dict, iterations) -> str:
    """按期概率用 schedule.prob_text 的统一文字：0 次按期写“<1%（N 次模拟无一次按期），预计推迟 N 天（P50）”，不写“0%”。"""
    p = m.get("on_time_prob")
    if p is None:
        return "按期概率未知（缺少 P6 计划日期）"
    s = "按期概率 " + schedule.prob_text(p, iterations, schedule.p50_delay(m))
    return s + (f"（基于 {iterations} 次模拟的估计）" if iterations and 1 <= round(p * 100) <= 99 else "")


def alert_line(f: dict) -> str:
    """一条预警的确定性一句话（预警原因/依据在 AI 文字不可用或已过期时的替代）。"""
    bits = []
    if f.get("slip_days"):
        bits.append(f"滞后 {_n(f['slip_days'])} 天")
    if f.get("float_total") is not None:
        bits.append(f"总浮时 {_n(f['float_total'])} 天")
    if f.get("over_float"):
        bits.append(f"超出总浮时 {_n(f['over_float'])} 天")
    if f.get("idle_days"):
        bits.append(f"{_n(f['idle_days'])} 天无日志进度")
    return "，".join(bits) or (f.get("rule_name") or "")


def render(f: dict) -> str:
    """作业底稿 → 文字。"""
    head = f"作业 {_an.fmt(f['code'], f['name'])}"
    if not f["has_open_alert"]:
        lines = ["无未关闭的进度预警"]
    else:
        lines = [f"有 {len(f['alert_ids'])} 条未关闭进度预警（#{f['alert_id']}，{LEVEL_NAME.get(f['level'], f['level'])}，{f['rule_name']}，"
                 f"{judged_on(f['as_of'], f.get('alert_p6_date'))}）"
                 + ("：" + alert_line(f) if alert_line(f) else "")]
    if f.get("completion_pct") is not None:
        lines.append(f"累计完成 {_n(f['completion_pct'])}%" + (f"（判定日计划应完成 {_n(f['planned_pct'])}%）" if f.get("planned_pct") is not None else ""))
    if f.get("pct_log") is not None:
        lines.append(f"日志最新累计 {_n(f['pct_log'])}%（{f['pct_log_date']}）")
    if f.get("pct_p6") is not None:
        lines.append(f"P6 记录完成 {_n(f['pct_p6'])}%（P6 数据日期 {f['p6_data_date']}）")
    if f.get("forecast_finish"):
        lines.append(f"计划完成 {f['early_finish']}，预测完成 {f['forecast_finish']}")
    for m in f.get("milestones", []):
        lines.append(f"下游里程碑 {m['name']}：P6 {m['p6_date']}，{_prob(m, f.get('iterations'))}")
    return head + "：" + "；".join(lines)


def render_project(p: dict) -> str:
    a = p["alerts"]
    L = [f"P6 数据日期 {p['p6_data_date'] or '无'}；未关闭进度预警：红 {a['red']} 橙 {a['orange']} 黄 {a['yellow']}"]
    for f in p["lag_alerts"]:
        L.append(render(f))
    if p["milestones"]:
        for m in p["milestones"]:
            L.append(f"里程碑 {m['name']}：P6 {m['p6_date']}，预测 {m['forecast']}，P80 {m['p80']}，{_prob(m, p['iterations'])}")
    else:
        L.append("里程碑：暂无预测")
    return "\n".join(L)


RULES_TEXT = ("【事实底稿硬约束】文字中的天数、百分比只能取自事实底稿，不得自行推算或四舍五入成别的数；"
              "底稿写有未关闭进度预警的作业，不得写“按计划/正常推进/进度挺稳”等相反结论；"
              "底稿没有标注【P6待同步】时，不得提醒“同步 P6/P6 没更新”；按期概率是模拟估计值，不得写成确定结论。")


# ============================================================================ 出口校验

CONTRA = re.compile(r"(?<![未没不])(?<!没有)(?<!并未)按计划|按期推进|正常推进|推进(?:很|挺|比较)?(?:稳|顺)|进度(?:很|挺|比较)?(?:稳|正常)|符合计划|如期(?:推进|完成)|"
                    r"(?:没有|无|未见)滞后|(?:没有|无)偏差|进展顺利|一切正常|(?:项目|整体|总体)(?:运行)?正常|顺利推进|平稳推进")
_NUM = r"(\d+(?:\.\d+)?)"
LAG_RE = re.compile(r"(?:滞后|落后|拖后)(?:了|约|大约|近|达|已|累计|超过|超出)*\s*" + _NUM + r"\s*(?:个)?(?:天|日)")
OVER_RE = re.compile(r"超出(?:总)?浮时\s*" + _NUM + r"\s*(?:个)?(?:天|日)")
FLOAT_RE = re.compile(r"(?:总浮时|浮时)(?:仅|只有|为|约|是|还剩|剩|剩余)?\s*[-−]?" + _NUM + r"\s*(?:个)?(?:天|日)")
PCT_RE = re.compile(_NUM + r"\s*%")
# 按期概率写成“0%”“100%”：模拟估计不得写成确定结论，应写“<1%”“>99%”（见 schedule.prob_text）
PROB_EXTREME_RE = re.compile(r"按期(?:概率|可能性|把握)?[^0-9<>＜＞，。；,;\n]{0,8}(?<![\d.])(0|100)(?:\.0+)?\s*%")
SYNC_RE = re.compile(r"P6.{0,12}(?:没更新|未更新|还没更新|尚未更新|待同步|没同步|未同步)|记得同步|同步一下|同步(?:到)?\s*P6|更新(?:一下)?\s*P6")


def _flat(facts: list[dict]) -> list[dict]:
    out: list[dict] = []
    for f in facts:
        if f.get("kind") == "project":
            out.append(f)
            out += f["lag_alerts"]
        else:
            out.append(f)
    return out


def _close(v: float, pool: set[float]) -> bool:
    return any(abs(v - x) < 0.051 for x in pool)


def check_text(text: str, facts: list[dict], extra: str = "", strict_pct: bool = True, counts: dict | None = None) -> list[str]:
    """校验 AI 文字与底稿一致，返回问题清单（空表示通过）。extra 为同时给了模型的其他依据文字，其中的百分比视为已核实。
    counts 为调用方开关：传入计数（见 count_problems）才核对“N 项/条/处”，目前只有简报传；预警分析、现场圈点评等不传，规则不变。"""
    problems: list[str] = []
    fl = _flat(facts)
    acts = [f for f in fl if f.get("kind") == "activity"]
    lag = {abs(f["slip_days"]) for f in acts if f.get("slip_days") is not None}
    flt = {abs(x) for f in acts for x in (f.get("float_total"), f.get("float_left"), f.get("over_float")) if x is not None}
    over = {f["over_float"] for f in acts if f.get("over_float") is not None}
    pcts = {f[k] for f in acts for k in ("planned_pct", "completion_pct", "pct_log", "pct_p6") if f.get(k) is not None}
    for f in fl:
        for m in (f.get("milestones") or []):
            if m.get("on_time_prob") is not None and 1 <= round(m["on_time_prob"] * 100) <= 99:
                pcts.add(round(m["on_time_prob"] * 100))
            d = schedule.p50_delay(m)   # 里程碑 P50 推迟天数：写成“滞后/落后 N 天”也认
            if d is not None and d > 0:
                lag.add(float(d))
    pcts |= {float(x) for x in PCT_RE.findall(extra)}
    pcts |= {float(x) for f in fl for x in PCT_RE.findall(render_project(f) if f.get("kind") == "project" else render(f))}
    # 1 有未关闭预警/滞后事实时，不得出现“按计划”类结论
    if any(f.get("has_open_alert") for f in acts) or any(sum(f["alerts"].values()) for f in fl if f.get("kind") == "project"):
        hit = CONTRA.search(text)
        if hit:
            problems.append(f"文字出现“{hit.group()}”，与底稿中未关闭的进度预警矛盾")
    # 2 天数
    for m in LAG_RE.finditer(text):
        if not _close(float(m.group(1)), lag):
            problems.append(f"滞后天数写作 {m.group(1)} 天，底稿为 " + ("、".join(_n(x) for x in sorted(lag)) + " 天" if lag else "无滞后事实"))
    masked = OVER_RE.sub(lambda m: " " * len(m.group()), text)
    for m in OVER_RE.finditer(text):
        if not _close(float(m.group(1)), over | flt):
            problems.append(f"超出总浮时写作 {m.group(1)} 天，与底稿不符")
    for m in FLOAT_RE.finditer(masked):
        if not _close(float(m.group(1)), flt):
            problems.append(f"总浮时写作 {m.group(1)} 天，与底稿不符")
    # 3 百分比
    for m in (PCT_RE.finditer(text) if strict_pct else ()):
        v = float(m.group(1))
        if not _close(v, pcts) and not _close(round(v), pcts):
            problems.append(f"百分比 {m.group(1)}% 不在底稿内")
    for m in PROB_EXTREME_RE.finditer(text):
        problems.append(f"按期概率写作 {m.group(1)}%，模拟估计应写“<1%”或“>99%”（照抄事实底稿）")
    # 4 P6 同步提醒必须有底稿标注
    if SYNC_RE.search(text) and not any(f.get("p6_sync_due") for f in acts):
        problems.append("出现“P6 未更新/同步”提醒，但底稿没有待同步标注（同一作业在一个 P6 数据日期内只提醒一次）")
    # 5 数量（仅调用方传入 counts 时）
    if counts is not None:
        problems += count_problems(text, counts, extra)
    return list(dict.fromkeys(problems))


# 计数（N 项/条/处）：数量由系统统计，AI 文字里出现时必须等于底稿同一含义的数（“9 项高风险”不能拿未闭环数 9 顶替高风险数 4）
COUNT_RE = re.compile(r"(?<![A-Za-z\d.])(\d+)\s*(?:项|条|处)")
# 含义关键词 → 计数字段：取同一分句里离数字最近（_COUNT_GAP 字以内）的一个；“预警”只在没有更具体的词时才用
COUNT_KW = (("高风险", "high"), ("未闭环", "open"), ("未整改", "open"), ("超期", "overdue"), ("逾期", "overdue"),
            ("累计闭环", "resolved"), ("已闭环", "resolved"), ("质控提醒", "reminders"),
            ("红橙", "red_orange"), ("红色", "red"), ("橙色", "orange"), ("黄色", "yellow"))
COUNT_KW_LOOSE = (("预警", "alerts"),)
COUNT_AREA = (("安全", re.compile(r"安全|隐患")), ("质量", re.compile(r"质量|质控")))
_COUNT_GAP = 8


def _count_kw(clause: str, m: re.Match) -> tuple | None:
    """数字 m 的含义关键词 (词, 字段, 起, 止)；距离相同时取数字后面的（“3 项高风险”）。"""
    for table in (COUNT_KW, COUNT_KW_LOOSE):
        best = None
        for word, field in table:
            for k in re.finditer(re.escape(word), clause):
                after = k.start() >= m.end()
                gap = k.start() - m.end() if after else m.start() - k.end()
                if 0 <= gap <= _COUNT_GAP and (best is None or gap < best[0] or (gap == best[0] and after)):
                    best = (gap, word, field, k.start(), k.end())
        if best:
            return best[1:]
    return None


def count_problems(text: str, counts: dict[str, dict], extra: str = "") -> list[str]:
    """“N 项/条/处”与底稿计数核对。counts 按方面给出 {字段: 数或可取的数元组}，如 {"安全": {"open": 9, "high": 4, "overdue": 9}}。
    方面看所在分句（安全/隐患、质量/质控），分句没点名看整行，都没有则各方面同一字段的数都算；
    找不到含义关键词的数量不核对（如照抄隐患标题“3处临边防护缺失”）；与 extra（给模型的底稿原文）逐字相同的说法放行。"""
    problems: list[str] = []
    flat = re.sub(r"\s+", "", extra or "")
    for line in (text or "").splitlines():
        for clause in re.split(r"[，。；;,]", line):
            for m in COUNT_RE.finditer(clause):
                kw = _count_kw(clause, m)
                if not kw:
                    continue
                word, field, ks, ke = kw
                if flat and re.sub(r"\s+", "", clause[min(ks, m.start()):max(ke, m.end())]) in flat:
                    continue
                areas = [a for a, rx in COUNT_AREA if rx.search(clause)] or [a for a, rx in COUNT_AREA if rx.search(line)]
                pool = [counts[a][field] for a in areas if field in counts.get(a, {})] or [v[field] for v in counts.values() if field in v]
                allowed = {int(x) for v in pool for x in (v if isinstance(v, (list, tuple, set)) else (v,)) if x is not None}
                if allowed and int(m.group(1)) not in allowed:
                    problems.append(f"“{clause.strip()}”：{word}的数量写作 {m.group(1)}，底稿为 {'、'.join(str(x) for x in sorted(allowed))}"
                                    "（数量照抄同一口径，高风险、未闭环、超期不得互相顶替）")
    return list(dict.fromkeys(problems))


# ============================================================================ 预警文字的新旧判断

def _row_fact(row: dict, cur: dict, is_open: bool = True) -> dict:
    """一条预警行的当前判定 → 作业底稿形状（只含该预警本身的事实）。"""
    return {"kind": "activity", "code": row["activity_code"], "name": row["activity_name"], "has_open_alert": is_open, "alert_ids": [row["id"]],
            "alert_id": row["id"], "level": cur["level"], "rule": cur["rule"], "rule_name": cur["rule_name"], "as_of": cur["as_of"],
            "slip_days": cur["slip_days"], "float_total": cur["float_total"], "float_left": cur["float_left"], "over_float": cur["over_float"],
            "idle_days": cur["idle_days"], "planned_pct": cur["planned_pct"], "completion_pct": cur["actual_pct"],
            "early_finish": cur["early_finish"], "forecast_finish": cur["forecast_finish"], "milestones": []}


def alert_fact(conn, row: dict) -> dict:
    """某一条预警的事实底稿：作业层事实（日志/P6 进度、里程碑）＋该预警自己的判定数字。
    同一作业有多条预警时，activity_facts 只取主预警；分析/复核次要预警必须用它自己的判定，否则刚生成就“已过期”。"""
    own = _row_fact(row, _alert_part(row), row.get("status", "open") == "open")
    base = activity_facts(conn, row["project_id"], row["activity_code"])
    keep = ("alert_id", "level", "rule", "rule_name", "as_of", "slip_days", "float_total", "float_left", "over_float", "idle_days", "planned_pct", "forecast_finish")
    out = {**base, **{k: own[k] for k in keep}}
    out["alert_p6_date"] = import_date(conn, row.get("import_id"))
    if own["completion_pct"] is not None:
        out["completion_pct"] = own["completion_pct"]
    out["has_open_alert"] = base["has_open_alert"] or own["has_open_alert"]
    return out


def alert_judgement(conn, row: dict) -> str:
    """预警“原因/依据”一句话：AI 分析与当前判定一致就用它，否则（判定后来更新了天数、或文字自相矛盾）用最新判定的确定性文字。"""
    exp = loads(row["explanation_json"], {}) or {}
    text = str(exp.get("judgement") or "").strip()
    cur = _alert_part(row)
    fact = _row_fact(row, cur)
    if text and not _sig_changed(exp.get("facts_sig"), cur) and not check_text(text, [fact], strict_pct=False):
        return text
    return alert_line(fact)


SIG_KEYS = ("slip_days", "float_left", "forecast_finish")  # 这几项一变，旧分析就过期（判定日每天滚动，不算变化）


def facts_sig(f: dict) -> dict:
    """解释生成时所依据的判定快照（判定日 + 滞后/剩余浮时/预测完成），用来识别“预警后来又更新了”。"""
    return {"as_of": f.get("as_of"), **{k: f.get(k) for k in SIG_KEYS}}


def _sig_changed(sig: dict | None, cur: dict) -> bool:
    return bool(sig) and any(k in sig and sig[k] != cur.get(k) for k in SIG_KEYS)


def explanation_state(row: dict) -> dict:
    """预警原因分析相对当前判定是否过期：有快照则比对快照；旧分析没有快照时，用当前事实复核其结论句。
    返回 {stale, as_of}（as_of 为分析所依据的判定日，旧分析未知为 None）。"""
    exp = loads(row.get("explanation_json"), {}) or {}
    if not exp:
        return {"stale": False, "as_of": None}
    cur = _alert_part(row)
    sig = exp.get("facts_sig")
    if sig:
        return {"stale": _sig_changed(sig, cur), "as_of": sig.get("as_of")}
    text = str(exp.get("judgement") or "").strip()
    fact = _row_fact(row, cur, row.get("status") == "open")
    return {"stale": bool(text and check_text(text, [fact], strict_pct=False)), "as_of": None}


# ============================================================================ AI 文字引用的预警：生成时留存，显示时复核

ALERT_STATUS_NAME = {"confirmed": "已确认属实", "false_alarm": "已标为误报", "closed": "已关闭"}


def alert_ref(row: dict) -> dict:
    """简报等 AI 文字引用某条预警时留存的依据：编号、作业、状态、等级、判定日与判定数字快照。"""
    return {"id": row["id"], "code": row["activity_code"], "status": row["status"], "level": row["level"], "as_of": row["as_of"],
            "sig": facts_sig(_alert_part(row))}


def alert_ref_problem(ref: dict, row: dict | None) -> str:
    """引用的预警现在是否仍成立：不成立返回原因（如“预警 #11（PR100）已关闭”），仍成立返回空串。
    已不是 open（关闭/确认/误报后不再计入未关闭预警）、等级变了、滞后/剩余浮时/预测完成变了，都算不成立；判定日每天滚动，不算。"""
    name = f"预警 #{ref['id']}（{ref.get('code')}）"
    if not row:
        return name + "已不存在"
    if row["status"] != "open":
        return name + ALERT_STATUS_NAME.get(row["status"], "已关闭")
    if row["level"] != ref.get("level"):
        return name + f"由{LEVEL_NAME.get(ref.get('level'), ref.get('level'))}变为{LEVEL_NAME.get(row['level'], row['level'])}"
    if _sig_changed(ref.get("sig"), _alert_part(row)):
        return name + "判定数字已更新"
    return ""


# ============================================================================ 现场圈“无新信息不评”的签名

def signature(f: dict) -> str:
    """作业状况签名：预警编号/级别、是否超出浮时、滞后天数（3 天一档，避免每天+1 就重评）、里程碑按期概率（10% 一档）、是否已过计划完成日。"""
    slip = f.get("slip_days") or 0
    probs = ",".join(f"{m['code']}:{round(m['on_time_prob'] * 10) if m.get('on_time_prob') is not None else 'x'}" for m in f.get("milestones", []))
    return f"{f.get('alert_id')}|{f.get('level')}|{int(bool(f.get('over_float')))}|{int(slip // 3)}|{probs}|{int(bool(f.get('past_finish')))}"


# ============================================================================ 带校验的调用

@dataclass
class Guarded:
    data: Any
    run_id: int | None
    source: str  # ai | template
    problems: list[str]


def guarded(call: Callable[[str], Any], texts: Callable[[Any], str], facts: list[dict] | None, fallback: Callable[[], Any], extra: str = "",
            check: Callable[[Any], list[str]] | None = None, counts: dict | None = None) -> Guarded:
    """call(feedback)->AIResult；texts(data)->要校验的全部文字。第一次与底稿矛盾则带着原因重试一次，仍不过用 fallback() 的确定性文字。
    facts 为 None 时不做底稿核对；check(data) 为调用方的附加校验（如比较词、期限、金额），问题一并计入；
    counts 传给 check_text，开启“N 项/条/处”核对（只有简报开启）。
    第一次调用失败（AIError）照常上抛，由调用方沿用原有的降级；重试时失败则直接用模板。"""
    feedback, problems, run_id = "", [], None
    for attempt in range(2):
        try:
            res = call(feedback)
        except AIError:
            if attempt == 0:
                raise
            break
        run_id = res.run_id
        problems = (check_text(texts(res.data), facts, extra, counts=counts) if facts is not None else []) + (check(res.data) if check else [])
        if not problems:
            return Guarded(res.data, run_id, "ai", [])
        log.info("AI 文字与事实底稿矛盾（第%d次）：%s", attempt + 1, "；".join(problems))
        feedback = "\n\n【上次输出被拒绝】与事实底稿不一致：" + "；".join(problems) + "。请严格按事实底稿重写。"
    return Guarded(fallback(), run_id, "template", problems)
