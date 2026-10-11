"""简报Agent + 项目首页数据：把各 Agent 的结果压缩成领导/项目经理“一屏可读”的少量事项。

口径（项目铁律）：项目经理只关注进度、质量、安全，不审核日志。因此简报只写这三个方面，
不写日志/周确认填报率、谁没记录、数据审核，也不写产值；这类内容由技术员、工程主管和催办链处理。
模型输出与确定性模板都经 `scope_ok` 过滤，违规条目直接丢弃，不靠提示词自觉。

结论随事实失效：生成时在 content_json.refs 记下依据（引用的预警、当前 P6 批次、计数签名、提到的作业）；
显示时（latest）按同一口径轻量复核，依据已变就不用 AI 结论，改由系统按当前事实拼出并标注，不调用 AI、不写库。
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta

from ..ai.client import AIError, call_json
from ..db import all_, dumps, loads, now_iso, one
from . import act_names as _an
from . import facts as F
from . import findings, output, p6, schedule

BRIEF_AREAS = ("进度", "质量", "安全")
# 日志填报/记录率/数据审核、产值：不属于项目经理关注的范围
_OUT_OF_SCOPE = re.compile(r"填报|记录率|提交率|确认率|缺报|未填|未提交|无记录|没有记录|日志审核|审核日志|数据审核|产值")
# 零样本不下结论：近 SAMPLE_DAYS 天某方面一条记录都没有时，不得写“可控/正常/良好”（未知不等于没问题）
SAMPLE_DAYS = 14
_CONCL = re.compile(r"可控|受控|正常|良好|平稳|稳定|无虞|态势(?:好|佳)|情况(?:好|佳)")
_AREA_KW = {"安全": re.compile(r"安全|隐患"), "质量": re.compile(r"质量|质控")}
_GENERAL = re.compile(r"项目|整体|总体|一切|各方面|全面|全场")
# 说的是进度（有进度预警/预测作样本），不是不点名方面的总体结论：“关键路径整体可控”“项目进度总体平稳”
_PROGRESS_KW = re.compile(r"进度|工期|关键路径|里程碑")

SYSTEM = """你是项目经理的参谋。根据各智能体汇总的项目今日态势，写一份给项目领导的简报：
1. headline：一句话总体判断（进度、质量、安全三个方面中最需要关注的）。项目经理只关注进度、质量、安全，不审核日志：不要写日志/周确认的填报率、谁没记录、数据审核，也不要写产值。
2. items：最多5条需要领导关注或决策的事项，只能是进度、质量、安全三类，按重要性排序；每条写清楚现状、影响和建议动作，必须来自给定事实，不要编造数字。
3. 如果一切正常，就如实说正常，不要为了凑数制造问题；但【事实底稿】标明“样本为零”的方面（近期一条记录都没有），不得写“可控/正常/良好”，要么写“近 N 天无隐患记录”，要么不写该方面。
4. 【事实底稿】是各 Agent 共用的口径：天数、百分比只能照抄，不得与之矛盾；有未关闭进度预警就不能写“进度正常/按计划”；里程碑按期概率是模拟估计值，须带“估计”字样，缺值写“未知”，不要写成确定结论。
5. 数量（N 项/条/处）由系统统计后在页面上显示，headline 和 items 里不要写数量，只写原因、影响和建议；确需提数量时只能照抄事实底稿里同一口径的数：高风险数、未闭环数、超期数是三个不同的数，不得互相顶替。
只输出JSON：{"headline":"","items":[{"level":"high|medium|info","area":"进度|质量|安全","title":"不超过20字","detail":"不超过60字","action":"建议动作"}]}"""


def counts(conn, project_id: int, day: date) -> dict:
    """进度预警、安全隐患、质量问题的计数与近期样本数（KPI 与简报失效复核共用同一口径，均为轻量计数查询）。"""
    d = day.isoformat()
    alerts = {r["level"]: r["n"] for r in all_(conn, "SELECT level, COUNT(*) n FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' GROUP BY level", (project_id,))}
    safety = one(conn, """SELECT SUM(status IN ('open','rectifying','rejected')) open, SUM(status IN ('open','rectifying','rejected') AND level='high') high,
                          SUM(status IN ('open','rejected') AND due_date < ?) overdue, SUM(status='resolved') resolved, COUNT(*) total
                          FROM findings WHERE project_id=? AND agent='safety' AND confirmed_at IS NOT NULL""", (d, project_id))
    quality = one(conn, """SELECT SUM(kind<>'qc_reminder') open, SUM(kind='qc_reminder') reminders, SUM(kind<>'qc_reminder' AND due_date < ?) overdue
                           FROM findings WHERE project_id=? AND agent='quality' AND confirmed_at IS NOT NULL AND status IN ('open','rectifying','rejected')""", (d, project_id))
    since = (day - timedelta(days=SAMPLE_DAYS)).isoformat()
    samples = {area: one(conn, f"""SELECT COUNT(*) n FROM findings WHERE project_id=? AND agent=? AND confirmed_at IS NOT NULL AND kind<>'qc_reminder'
                                  AND (substr(created_at,1,10)>? OR status IN ('open','rectifying','rejected'))""", (project_id, agent, since))["n"]
               for area, agent in (("安全", "safety"), ("质量", "quality"))}
    return {
        "alerts": {"red": alerts.get("red", 0), "orange": alerts.get("orange", 0), "yellow": alerts.get("yellow", 0)},
        "quality": {"open": quality["open"] or 0, "reminders": quality["reminders"] or 0, "overdue": quality["overdue"] or 0},
        "safety": {"open": safety["open"] or 0, "high": safety["high"] or 0, "overdue": safety["overdue"] or 0,
                   "resolved": safety["resolved"] or 0, "total": safety["total"] or 0},
        "samples": samples,  # 近 SAMPLE_DAYS 天的安全/质量记录数（含仍未闭环的），0 表示样本为零
    }


def kpis(conn, project_id: int, day: date) -> dict:
    d = day.isoformat()
    techs = one(conn, "SELECT COUNT(*) n FROM users WHERE project_id=? AND role='tech' AND active=1", (project_id,))["n"]
    # 今日有现场记录（随手记/日志）的技术员数；本周已确认的技术员数
    done = one(conn, """SELECT COUNT(DISTINCT u.id) n FROM users u WHERE u.project_id=? AND u.role='tech' AND u.active=1 AND (
                        EXISTS (SELECT 1 FROM notes n WHERE n.author_id=u.id AND n.note_date=?) OR
                        EXISTS (SELECT 1 FROM logs l WHERE l.author_id=u.id AND l.log_date=? AND l.period='day' AND l.mode='ai'))""",
               (project_id, d, d))["n"]
    ws = (day - timedelta(days=day.weekday())).isoformat()
    lws = (day - timedelta(days=day.weekday() + 7)).isoformat()
    wk_done = one(conn, "SELECT COUNT(*) n FROM logs WHERE project_id=? AND period='week' AND log_date=? AND status IN ('submitted','approved')", (project_id, ws))["n"]
    lwk_done = one(conn, "SELECT COUNT(*) n FROM logs WHERE project_id=? AND period='week' AND log_date=? AND status IN ('submitted','approved')", (project_id, lws))["n"]
    notes_today = one(conn, "SELECT COUNT(*) n FROM notes WHERE project_id=? AND note_date=?", (project_id, d))["n"]
    c = counts(conn, project_id, day)
    fc = schedule.latest_forecast(conn, project_id)
    ms = (fc or {}).get("milestones", [])
    next_ms = next((m for m in ms if m["p6_date"] and m["p6_date"] >= d), ms[0] if ms else None)
    ov = output.overview(conn, project_id)
    # 本周未过完时，主数取上一个完整周
    wk = ov["series"][-2] if len(ov["series"]) > 1 else {}
    prev = ov["series"][-3] if len(ov["series"]) > 2 else {}
    imp = p6.current_import(conn, project_id)
    return {
        "day": d, "logs": {"done": done, "expected": techs, "notes_today": notes_today},
        "weeks": {"this": wk_done, "last": lwk_done, "expected": techs, "week_start": ws, "last_week_start": lws},
        "alerts": c["alerts"], "quality": c["quality"], "safety": c["safety"],
        "milestone": next_ms, "project_finish": (fc or {}).get("project_finish"), "forecast_at": (fc or {}).get("created_at"),
        "output": {"week": wk.get("record_actual") if wk.get("record_actual") is not None else wk.get("system"),
                   "week_source": "record" if wk.get("record_actual") is not None else ("system" if wk.get("system") is not None else None),
                   "week_self": wk.get("system_self"), "week_approved": wk.get("system_approved"),
                   "week_planned": wk.get("planned"), "prev_week": prev.get("record_actual") if prev.get("record_actual") is not None else prev.get("system"),
                   "currency": ov["currency"], "month_forecast": ov["month_forecast"]},
        "p6": {"data_date": imp["data_date"], "file": imp["file_name"]} if imp else None,
        "samples": c["samples"],
    }


def zero_areas(k: dict) -> set[str]:
    return {a for a, n in (k.get("samples") or {}).items() if not n}


def _zero_note(k: dict, area: str) -> str:
    what = "隐患" if area == "安全" else "问题"
    return f"；近 {SAMPLE_DAYS} 天无{what}记录（样本为零，不得据此写“可控/正常/良好”）" if area in zero_areas(k) else ""


def zero_sample_problems(text: str, zero: set[str]) -> list[str]:
    """样本为零的方面出现“可控/正常/良好”类结论即不通过；不点名方面的总体结论（项目整体正常）同样不行。"""
    out = []
    if not zero:
        return out
    for clause in re.split(r"[，。；;,\n]", text or ""):
        m = _CONCL.search(clause)
        if not m:
            continue
        hit = [a for a in zero if _AREA_KW[a].search(clause)]
        named = [a for a, kw in _AREA_KW.items() if kw.search(clause)]
        if hit or (not named and _GENERAL.search(clause) and not _PROGRESS_KW.search(clause)):
            out.append(f"“{clause.strip()}”：{'、'.join(hit or sorted(zero))}近 {SAMPLE_DAYS} 天没有记录，样本为零，不能下“{m.group()}”的结论")
    return out


def facts_text(conn, project_id: int, k: dict, pf: dict | None = None) -> str:
    # 不放入日志填报/周确认/产值数据：项目经理只关注进度、质量、安全（见模块文档）
    pf = pf or F.project_facts(conn, project_id)
    L = [f"日期 {k['day']}"]
    # 依据第一行就写明所用 P6 批次；每条预警写明判定日与它判定时的 P6 数据日期
    L.append(f"进度预警：红{k['alerts']['red']} 橙{k['alerts']['orange']} 黄{k['alerts']['yellow']}"
             + (f"（P6 数据日期 {pf['p6_data_date']}）" if pf.get("p6_data_date") else ""))
    for a in all_(conn, "SELECT * FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' AND level IN ('red','orange') ORDER BY level DESC, id LIMIT 5", (project_id,)):
        L.append(f"[W{a['id']}] {a['title']}（{F.judged_on(a['as_of'], F.import_date(conn, a['import_id']))}）；分析：{F.alert_judgement(conn, a)}")
    L.append("【事实底稿】\n" + F.render_project(pf))  # 里程碑按期概率缺值写“未知”，不当作 0%
    s = k["safety"]
    L.append(f"安全隐患：未闭环 {s['open']}（高风险 {s['high']}，超期 {s['overdue']}），累计闭环 {s['resolved']}" + _zero_note(k, "安全"))
    for f in findings.list_for(conn, project_id, agent="safety", status="active", limit=4):
        L.append(f"[S{f['id']}] {f['level_name']} {f['title']}（{f['status_name']}，期限 {f['due_date']}）")
    qc = k["quality"]
    L.append(f"质量问题：未闭环 {qc['open']}（超期 {qc['overdue']}），待执行质控提醒 {qc['reminders']}" + _zero_note(k, "质量"))
    for f in findings.list_for(conn, project_id, agent="quality", status="active", limit=4):
        if f["kind"] != "qc_reminder":
            L.append(f"[Q{f['id']}] {f['level_name']} {f['title']}（{f['status_name']}，期限 {f['due_date']}）")
    return "\n".join(L)


def count_facts(k: dict) -> dict:
    """“N 项/条/处”可取的数（facts.count_problems 用）：按方面列出事实底稿里同一含义的计数。"""
    a, s, q = k["alerts"], k["safety"], k["quality"]
    ro = a["red"] + a["orange"]
    return {"进度": {"red": a["red"], "orange": a["orange"], "yellow": a["yellow"], "red_orange": ro, "alerts": (ro, ro + a["yellow"])},
            "安全": {"open": s["open"], "high": s["high"], "overdue": s["overdue"], "resolved": s["resolved"]},
            "质量": {"open": q["open"], "overdue": q["overdue"], "reminders": q["reminders"]}}


def count_sig(k: dict) -> dict:
    """计数签名：生成简报时记下，显示时按同一口径重算，任一项不同即判 AI 结论过期（键名即标注里的说法）。"""
    a, s, q = k["alerts"], k["safety"], k["quality"]
    return {"红色预警": a["red"], "橙色预警": a["orange"], "黄色预警": a["yellow"],
            "安全隐患未闭环": s["open"], "高风险隐患": s["high"], "超期隐患": s["overdue"],
            "质量问题未闭环": q["open"], "超期质量问题": q["overdue"], "待执行质控提醒": q["reminders"],
            "样本为零的方面": "、".join(sorted(zero_areas(k)))}


PROMPT_VERSION = "brief-v4-20261007"


def _texts(d: dict) -> str:
    return "\n".join([d.get("headline") or ""] + [f"{i.get('title')} {i.get('detail')} {i.get('action')}" for i in d.get("items") or []])


def scope_ok(text: str) -> bool:
    return not _OUT_OF_SCOPE.search(text or "")


# 项目经理主动提问的范围（RJ 10-10）：产值可以问；日志填报、记录率、数据审核仍不答。简报/待办/催办仍用 scope_ok，不主动推产值
_ASK_OUT_OF_SCOPE = re.compile(_OUT_OF_SCOPE.pattern.replace("|产值", ""))


def ask_scope_ok(text: str) -> bool:
    return not _ASK_OUT_OF_SCOPE.search(text or "")


def clean(d: dict) -> dict:
    """丢弃不在“进度/质量/安全”范围内的条目；headline 越界则留空，由调用方换成模板标题。"""
    items = [i for i in d["items"] if i["area"] in BRIEF_AREAS and scope_ok(f"{i['title']} {i['detail']} {i['action']}")]
    return {"headline": d["headline"] if scope_ok(d["headline"]) else "", "items": items}


def template(k: dict, pf: dict) -> dict:
    """确定性简报：只复述预警/隐患/质量问题的数量与头部预警作业，数字全部取自底稿与 KPI。"""
    items = []
    for f in pf["lag_alerts"][:3]:
        items.append({"level": "high" if f["level"] == "red" else "medium", "area": "进度",
                      "title": f"{f['code']} 进度预警"[:40], "detail": F.alert_line(f)[:120], "action": "核实现场资源并组织赶工评审"})
    if k["safety"]["high"]:
        items.append({"level": "high", "area": "安全", "title": f"{k['safety']['high']}项高风险隐患未闭环", "detail": f"未闭环隐患共 {k['safety']['open']} 项，超期 {k['safety']['overdue']} 项", "action": "督促当日整改"})
    if k["quality"]["open"]:
        items.append({"level": "medium", "area": "质量", "title": f"{k['quality']['open']}项质量问题未闭环", "detail": f"其中超期 {k['quality']['overdue']} 项", "action": "督促整改"})
    a = pf["alerts"]
    zero = zero_areas(k)
    sa = f"安全：近 {SAMPLE_DAYS} 天无隐患记录" if "安全" in zero else f"安全隐患未闭环 {k['safety']['open']} 项"
    qa = f"质量：近 {SAMPLE_DAYS} 天无问题记录" if "质量" in zero else f"质量问题未闭环 {k['quality']['open']} 项"
    if items or sum(a.values()):
        head = f"待确认进度预警 红{a['red']} 橙{a['orange']} 黄{a['yellow']}；{sa}；{qa}"   # 只数待工程主管确认的，与“已确认红色预警”区分（D-3）
    else:
        head = f"暂无待确认的进度预警；{sa}；{qa}" if zero else "暂无待确认的进度预警与未闭环问题"
    return {"headline": head, "items": items[:5]}


def generate(conn, project_id: int, user_id: int | None, day: str | None = None) -> dict:
    d = date.fromisoformat(day) if day else date.today()
    k = kpis(conn, project_id, d)
    pf = F.project_facts(conn, project_id)
    text = facts_text(conn, project_id, k, pf)
    snap = _snapshot(conn, project_id, k, pf)  # 依据快照与事实底稿同一时刻取，AI 调用期间事实再变也能在显示时识别

    def mock():
        items = []
        if k["alerts"]["red"]:
            items.append({"level": "high", "area": "进度", "title": f"{k['alerts']['red']}项红色进度预警", "detail": "（离线模拟）关键作业滞后超过浮时", "action": "组织赶工评审"})
        elif k["alerts"]["orange"]:
            items.append({"level": "medium", "area": "进度", "title": f"{k['alerts']['orange']}项橙色进度预警", "detail": "（离线模拟）滞后消耗了较多浮时", "action": "组织赶工评审"})
        if k["safety"]["high"]:
            items.append({"level": "high", "area": "安全", "title": f"{k['safety']['high']}项高风险隐患未闭环", "detail": "（离线模拟）", "action": "督促当日整改"})
        if k["quality"]["open"]:
            items.append({"level": "medium", "area": "质量", "title": f"{k['quality']['open']}项质量问题未闭环", "detail": "（离线模拟）", "action": "督促整改"})
        return {"headline": "（离线模拟）" + ("存在需关注事项" if items else template(k, pf)["headline"]), "items": items}
    def norm(x: dict) -> dict:
        raw = {"headline": str(x.get("headline") or ""), "items": [
            {"level": i.get("level") if i.get("level") in ("high", "medium", "info") else "info", "area": str(i.get("area") or ""),
             "title": str(i.get("title") or "")[:40], "detail": str(i.get("detail") or "")[:120], "action": str(i.get("action") or "")[:80]}
            for i in (x.get("items") or []) if isinstance(i, dict)][:5]}
        out = clean(raw)  # 越界（日志填报/产值等）的条目直接丢弃
        out["headline"] = out["headline"] or template(k, pf)["headline"]
        return out

    def call(feedback: str):
        return call_json("brief.daily", system=SYSTEM, user=text + feedback, prompt_version=PROMPT_VERSION, mock=mock,
                         validate=norm, project_id=project_id, user_id=user_id, max_tokens=900)
    try:
        # 与事实底稿矛盾（按计划/天数/百分比/数量对不上）则重试一次，仍不过用确定性模板并标注
        g = F.guarded(call, _texts, [pf], lambda: template(k, pf), extra=text, counts=count_facts(k),
                      check=lambda d: zero_sample_problems(_texts(d), zero_areas(k)))
        content, run_id = {**g.data, "source": g.source}, g.run_id
        if g.source == "template":
            content["note"] = "AI 简报与事实底稿不一致，已改用系统模板文字：" + "；".join(g.problems[:2])
    except AIError as exc:
        content, run_id = {"headline": f"简报Agent暂不可用：{exc}", "items": []}, None
    content["facts"] = text
    content["refs"] = _refs(snap, content)
    conn.execute("INSERT INTO briefs (project_id, day, content_json, ai_run_id, created_at) VALUES (?,?,?,?,?)",
                 (project_id, d.isoformat(), dumps(content), run_id, now_iso()))
    return {**content, "created_at": now_iso()}


# ============================================================================ 结论随事实失效

def _mentions(text: str, code: str) -> bool:
    """文字里是否提到某作业：代码（前后不接字母数字，避免 A10 命中 A1020；中文紧挨着也认），或它的中文简称（AI 叙述按简称写）。"""
    if not code:
        return False
    if re.search(r"(?<![A-Za-z0-9])" + re.escape(code) + r"(?!\d)", text or "") is not None:
        return True
    lb = _an.label(code)
    return bool(lb) and lb != code and not lb.endswith(_an.MISSING_TAG) and lb in (text or "")


def _forecast_ref(conn, project_id: int) -> dict | None:
    """最新一次里程碑预测：编号、时间与结果签名（按期概率、预测日期、P50/P80、全线完工）。
    日志提交后会按同一随机种子重算预测，输入没变时结果相同，只换编号不算变化。"""
    row = one(conn, "SELECT id, result_json, created_at FROM forecasts WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,))
    if not row:
        return None
    r = loads(row["result_json"], {}) or {}
    ms = [[m.get(x) for x in ("code", "p6_date", "forecast", "p50", "p80", "on_time_prob")] for m in (r.get("milestones") or [])]
    sig = hashlib.sha1(dumps([ms, r.get("project_finish"), r.get("iterations")]).encode("utf-8")).hexdigest()[:16]
    return {"id": row["id"], "at": row["created_at"], "sig": sig}


def _snapshot(conn, project_id: int, k: dict, pf: dict) -> dict:
    """生成简报时的依据快照：当前 P6 批次、最新里程碑预测、未关闭预警、事实底稿列出的预警、计数签名。"""
    _an.names(conn, project_id)   # 载入作业简称表，_mentions 按简称也能认出提到的作业
    return {"imp": p6.current_import(conn, project_id), "forecast": _forecast_ref(conn, project_id),
            "rows": all_(conn, "SELECT * FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' ORDER BY id", (project_id,)),
            "listed": {i for f in pf["lag_alerts"] for i in f["alert_ids"]}, "counts": count_sig(k)}


def _refs(snap: dict, content: dict) -> dict:
    """content_json.refs：引用的预警（底稿列出的 + 文字提到的作业的）、提到的作业、P6 批次、里程碑预测、计数签名。"""
    texts = _texts(content)
    codes = sorted({r["activity_code"] for r in snap["rows"] if _mentions(texts, r["activity_code"])})
    imp = snap["imp"]
    return {"p6_import": imp["id"] if imp else None, "p6_data_date": imp["data_date"] if imp else None, "forecast": snap["forecast"],
            "alerts": [F.alert_ref(r) for r in snap["rows"] if r["id"] in snap["listed"] or r["activity_code"] in codes],
            "codes": codes, "counts": snap["counts"]}


def stale_reasons(conn, project_id: int, c: dict, k: dict) -> tuple[list[str], set[str]]:
    """已存简报的依据现在是否仍成立：返回（原因, 已失效的作业代码）。只做轻量查询，不调用 AI。
    有 refs：P6 批次、最新里程碑预测、引用的预警（状态/等级/判定数字）、提到的作业是否仍有未关闭预警、计数签名（含质控提醒数）；
    旧简报没有 refs：看文字提到的作业（预警表里有过的）是否仍有未关闭预警。新旧简报都按当前计数核对文中“N 项/条/处”。"""
    reasons, bad = [], set()
    _an.names(conn, project_id)
    refs = c.get("refs")
    texts = _texts(c)
    open_codes = {r["activity_code"] for r in all_(conn, "SELECT DISTINCT activity_code FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale'", (project_id,))}
    if refs is not None:
        imp = p6.current_import(conn, project_id)
        if (imp["id"] if imp else None) != refs.get("p6_import"):
            reasons.append(f"P6 数据日期 {refs.get('p6_data_date') or '无'} → {imp['data_date'] if imp else '无'}")
        fc, old_fc = _forecast_ref(conn, project_id), refs.get("forecast")
        if "forecast" in refs and (fc or {}).get("sig") != (old_fc or {}).get("sig"):
            reasons.append(f"里程碑预测已重算（{(fc or {}).get('at', '')[5:16].replace('T', ' ') or '无'}）")
        ids = [r["id"] for r in refs.get("alerts") or []]
        rows = {r["id"]: r for r in all_(conn, f"SELECT * FROM alerts WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
        for ref in refs.get("alerts") or []:
            why = F.alert_ref_problem(ref, rows.get(ref["id"]))
            if why:
                reasons.append(why)
                bad.add(ref["code"])
        codes = refs.get("codes") or []
    else:
        codes = sorted({r["activity_code"] for r in all_(conn, "SELECT DISTINCT activity_code FROM alerts WHERE project_id=? AND rule<>'stale'", (project_id,))
                        if _mentions(texts, r["activity_code"])})
    for code in codes:
        if code not in open_codes and code not in bad:
            reasons.append(f"作业 {code} 已没有未关闭的进度预警")
            bad.add(code)
    now = count_sig(k)
    for key, old in ((refs or {}).get("counts") or {}).items():
        if now.get(key, old) != old:
            reasons.append(f"{key} {old if old != '' else '无'} → {now[key] if now[key] != '' else '无'}")
    if not reasons and F.count_problems(texts, count_facts(k), extra=c.get("facts") or ""):
        reasons.append("文中数量与当前统计不一致")
    return reasons, bad


_LEVEL_RANK = {"high": 0, "medium": 1, "info": 2}


def current_brief(conn, project_id: int, c: dict, k: dict, reasons: list[str], bad: set[str]) -> dict:
    """依据已变：结论改由系统按当前事实拼出（template），并标注原因；不调用 AI。
    要点：引用已失效预警/作业、或与当前事实（天数、百分比、数量）不符的 AI 条目去掉，其余照留；去掉后空出的方面用系统条目补上。"""
    pf = F.project_facts(conn, project_id)
    text = facts_text(conn, project_id, k, pf)
    t = template(k, pf)
    cf, zero = count_facts(k), zero_areas(k)
    keep = []
    for i in c.get("items") or []:
        s = f"{i.get('title')} {i.get('detail')} {i.get('action')}"
        if any(_mentions(s, code) for code in bad) or F.check_text(s, [pf], extra=text, counts=cf) or zero_sample_problems(s, zero):
            continue
        keep.append(i)
    have = {i.get("area") for i in keep}
    items = sorted(keep + [i for i in t["items"] if i["area"] not in have], key=lambda i: _LEVEL_RANK.get(i.get("level"), 3))[:5]
    at = (c.get("created_at") or "")[11:16]
    note = (f"AI 结论（{at} 生成）之后情况已变化：" if at else "AI 结论生成后情况已变化：") + "；".join(reasons[:3]) + \
           (f" 等 {len(reasons)} 处" if len(reasons) > 3 else "") + "。结论改由系统按当前事实生成，点“刷新”可重新生成 AI 简报。"
    return {**c, "headline": t["headline"], "items": items, "facts": text, "source": "system", "note": note,
            "stale": {"reasons": reasons, "ai_created_at": c.get("created_at")}}


_ZERO_HINT = re.compile(r"（样本为零[^）]*）")
_SOURCE_NAME = {"ai": "AI 生成，已按事实底稿核对", "template": "AI 文字与事实底稿不一致，改用系统模板",
                "system": "系统按当前事实生成（AI 结论生成后情况已变化）"}


def _zero_from_facts(facts: str) -> set[str]:
    """从已存简报的事实底稿里读出当时样本为零的方面（_zero_note 写入的“样本为零”标注）。"""
    out = set()
    for line in (facts or "").splitlines():
        if "样本为零" in line:
            out |= {a for a in ("安全", "质量") if line.startswith(a)}
    return out


def basis(content: dict) -> dict:
    """简报依据：生成方式 + 事实底稿各行（只留进度、质量、安全；去掉日期行、分节标题和写给模型的约束语）。"""
    lines = []
    for raw in (content.get("facts") or "").splitlines():
        t = _ZERO_HINT.sub("", raw).strip()
        if t and not t.startswith("日期 ") and t != "【事实底稿】" and scope_ok(t):
            lines.append(t)
    return {"source": _SOURCE_NAME.get(content.get("source"), ""), "note": content.get("note") or "", "lines": lines[:15]}


def latest(conn, project_id: int, day: str | None = None) -> dict | None:
    """今日最新简报。先按当前事实复核依据（stale_reasons）：已变化就改用系统按当前事实拼出的结论并标注（不调用 AI、不写库，
    AI 简报原样留在库里，第二天调度或手动“刷新”再生成）；样本为零的方面若被写成“可控/正常”，headline 置空，不在首页当结论展示。"""
    d = day or date.today().isoformat()
    row = one(conn, "SELECT content_json, created_at FROM briefs WHERE project_id=? AND day=? ORDER BY id DESC LIMIT 1", (project_id, d))
    if not row:
        return None
    c = {**loads(row["content_json"]), "created_at": row["created_at"]}
    k = {"day": d, **counts(conn, project_id, date.fromisoformat(d))}
    reasons, bad = stale_reasons(conn, project_id, c, k)
    if reasons:
        c = current_brief(conn, project_id, c, k, reasons, bad)
    elif c.get("headline") and zero_sample_problems(c["headline"], _zero_from_facts(c.get("facts"))):
        c["headline"] = ""
    c["basis"] = basis(c)
    return c


def decision_queue(conn, project_id: int, limit: int = 30, role: str | None = None) -> list[dict]:
    """项目经理“待我决策”：各 Agent 需要人拍板的事项，统一排序。
    AI 候选隐患也在这里（type=candidate，“确认并派单/不采纳”）：安全主管看全部，其他岗位（项目经理）只看高风险候选。"""
    q = []
    for a in all_(conn, "SELECT * FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' AND level IN ('red','orange') ORDER BY level DESC, id DESC LIMIT 15", (project_id,)):
        exp = loads(a["explanation_json"], {}) or {}
        q.append({"type": "alert", "id": a["id"], "agent": "进度Agent", "agent_key": "progress", "status": a["status"], "level": "high" if a["level"] == "red" else "medium", "title": a["title"],
                  "detail": F.alert_judgement(conn, a), "suggest": (exp.get("measures") or [{}])[0].get("action", ""), "link": f"alert?id={a['id']}"})
    for f in findings.list_for(conn, project_id, status="active", limit=40):
        if f["agent"] == "dq" or (f["agent"] == "quality" and f["kind"] == "qc_reminder"):
            continue  # 日志数据审核不是项目经理的事；质控提醒是给技术员的
        if f["agent"] == "safety" and f["level"] not in ("high",) and not f["overdue"] and f["status"] != "rectifying":
            continue
        if f["level"] in ("low", "info") and not f["overdue"]:
            continue
        q.append({"type": "finding", "id": f["id"], "agent": f["agent_name"], "agent_key": f["agent"], "kind": f["kind"], "status": f["status"], "level": f["level"] if f["level"] != "info" else "low",
                  "title": f["title"], "detail": ("已超期 · " if f["overdue"] else "") + f["status_name"] + (f" · {f['assignee']}" if f["assignee"] else ""),
                  "suggest": (f["body"] or {}).get("measure", ""), "link": f"finding?id={f['id']}"})
    for f in findings.list_for(conn, project_id, agent="safety", status="active", limit=40, candidates="only"):
        if role != "safety_sup" and f["level"] != "high":
            continue
        q.append({"type": "candidate", "id": f["id"], "agent": f["agent_name"], "agent_key": "safety", "status": f["status"], "level": f["level"],
                  "candidate": True, "level_name": f["level_name"], "ref_type": f["ref_type"], "ref_id": f["ref_id"],
                  "body": {k: (f["body"] or {}).get(k) for k in ("category", "desc", "measure", "scene")}, "title": f["title"], "detail": findings.CANDIDATE_NAME + "：AI 在照片中识别，需人确认后才派单、计入台账",
                  "suggest": (f["body"] or {}).get("measure", ""), "link": f"finding?id={f['id']}",
                  "assignee_id": f["assignee_id"], "due_date": f["due_date"]})
    rk = {"high": 0, "medium": 1, "low": 2}
    q.sort(key=lambda x: rk.get(x["level"], 3))
    return q[:limit]


# 各 Agent 今日处理的一句话说明（待办页“智能体今日处理”用，见 services/todo.py 的 _did）
_DID = {"scribe": "整理日志、转写语音", "safety": "识别现场照片、复核整改", "quality": "生成质控点", "dq": "预审提交的日志",
        "progress": "分析进度预警、给出赶工方案", "output": "建立产值映射、分析偏差", "report": "起草周月报", "analyst": "回答提问",
        "brief": "汇总今日简报"}


def cockpit(conn, project_id: int, day: str | None = None) -> dict:
    d = date.fromisoformat(day) if day else date.today()
    k = kpis(conn, project_id, d)
    fc = schedule.latest_forecast(conn, project_id)
    ov = output.overview(conn, project_id)
    trend = all_(conn, """SELECT note_date log_date, COUNT(*) n FROM notes WHERE project_id=? AND note_date>? AND note_date<=?
                          GROUP BY note_date ORDER BY note_date""", (project_id, (d - timedelta(days=14)).isoformat(), d.isoformat()))
    safety_trend = all_(conn, """SELECT substr(created_at,1,10) day, COUNT(*) n, SUM(status='resolved') resolved FROM findings WHERE project_id=? AND agent='safety'
                                 AND confirmed_at IS NOT NULL AND substr(created_at,1,10)>? GROUP BY day ORDER BY day""", (project_id, (d - timedelta(days=14)).isoformat()))
    return {"kpis": k, "brief": latest(conn, project_id, d.isoformat()), "milestones": (fc or {}).get("milestones", [])[:8],
            "output_series": ov["series"], "currency": ov["currency"], "log_trend": trend, "safety_trend": safety_trend,
            "project": one(conn, "SELECT name, config_json FROM projects WHERE id=?", (project_id,))}
