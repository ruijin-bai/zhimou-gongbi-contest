"""两周高风险作业安全预控（2026-10-08 增补能力 3）：安全由事后变事前。

- 作业从哪来：放行Agent 已经把未来 14 天要开工的 P6 作业归到工序类别（不调用模型，用已缓存的归类与关键词规则）；
  这里只取高风险类：吊装/起重、水上/沉箱/预制安装、深基坑（土方类里名称含基坑、围堰、边坡、支护、深挖的），
  以及名称里写了交通导改、封道、夜间施工的作业；
- 历史隐患：按 WBS 范围找到负责该作业的分包单位，统计该单位近 30 天已确认的安全隐患里与这类作业相关的条数（按类别词归类）；
- AI 只写一句“预控重点”（交融，数字守卫：句子里的数字必须出自统计），不过或模型不可用用规则句；同一份统计当天只调用一次；
- 安全主管首页一张卡片（不给项目经理加卡片），可一键起草“高风险作业预控提醒”（致分包文书的一种），安全主管确认后才下发到
  该单位分包安全员的待办和消息，签收即销项；系统不对外发送，不写罚款。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta

from ..ai.client import AIError, call_json
from ..db import all_, now_iso, one
from . import gate, p6
from . import act_names as _an

PROMPT_VERSION = "precontrol-v1-20261008"
ROLES = frozenset({"safety_sup"})
DAYS = 14
HIST_DAYS = 30

KIND_NAME = {"lifting": "吊装/起重", "marine": "水上/沉箱/预制安装", "deep": "深基坑", "traffic": "交通导改/夜间施工"}
DEEP_WORDS = ("基坑", "围堰", "边坡", "支护", "深挖", "深基")
TRAFFIC_WORDS = ("导改", "交通", "封道", "夜间", "夜施")
HAZARD_WORDS = {
    "lifting": ("吊装", "起重", "吊车", "吊物", "吊索", "钢丝绳", "司索", "信号工", "警戒", "吊"),
    "marine": ("水上", "救生", "落水", "船", "沉箱", "潮", "码头"),
    "deep": ("基坑", "临边", "坍塌", "边坡", "支护", "降水", "护栏"),
    "traffic": ("交通", "导改", "夜间", "反光", "警示", "围挡", "车辆", "照明"),
}
CHECKS = {"lifting": ("吊装方案已批（吊点、站位、警戒）", "起重设备检定合格、司机司索持证", "风速、天气满足吊装限制"),
          "marine": ("船机报备与水上作业许可", "救生与警示设施到位", "潮位、水文窗口满足"),
          "deep": ("开挖方案（含支护、边坡）已批", "监测点布设并取得初始值", "临边防护与降排水到位"),
          "traffic": ("交通导改方案已报批", "夜间照明、反光警示与围挡到位", "专人指挥与交底")}

SCHEMA = """
CREATE TABLE IF NOT EXISTS precontrol_cache (
    key TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    data_json TEXT NOT NULL,
    source TEXT,
    note TEXT,
    run_id INTEGER,
    created_at TEXT NOT NULL
);
"""


def risk_kind(row: dict) -> str | None:
    name = row.get("name") or ""
    if any(w in name for w in TRAFFIC_WORDS):
        return "traffic"
    if row.get("category") in ("lifting", "marine"):
        return row["category"]
    if row.get("category") == "earth" and any(w in name + (row.get("wbs_path") or "") for w in DEEP_WORDS):
        return "deep"
    return None


def _units(conn, pid: int, code: str) -> list[str]:
    imp = p6.current_import(conn, pid)
    act = one(conn, "SELECT * FROM activities WHERE import_id=? AND code=?", (imp["id"], code)) if imp else None
    if not act:
        return []
    return sorted({u["company"] for u in all_(conn, "SELECT company, wbs_scope FROM users WHERE project_id=? AND active=1 AND role IN ('tech','sub_safety')", (pid,))
                   if u["company"] and (u["wbs_scope"] or "").strip() and p6.in_scope(act, u["wbs_scope"])})


def history(conn, pid: int, company: str, kind: str, today: date) -> dict:
    """该单位近 30 天已确认的安全隐患里，与这类作业相关的条数与主要类别。"""
    from .findings import _cat_of
    since = (today - timedelta(days=HIST_DAYS)).isoformat()
    words = HAZARD_WORDS[kind]
    hits = []
    for f in all_(conn, """SELECT f.* FROM findings f JOIN users u ON u.id=f.assignee_id WHERE f.project_id=? AND f.agent='safety' AND f.kind<>'qc_reminder'
                           AND f.confirmed_at IS NOT NULL AND u.company=? AND substr(f.created_at,1,10)>=?""", (pid, company, since)):
        text = f["title"] + " " + (_cat_of(f) or "")
        if any(w in text for w in words):
            hits.append({"id": f["id"], "cat": _cat_of(f) or "未分类", "open": f["status"] in ("open", "rectifying", "rejected")})
    return {"n": len(hits), "open": sum(h["open"] for h in hits), "cats": dict(Counter(h["cat"] for h in hits).most_common(3)), "ids": [h["id"] for h in hits][:5]}


def rows(conn, user: dict, today: date | None = None) -> list[dict]:
    today = today or date.today()
    data = gate.build_list(conn, {**user, "role": "safety_sup"}, DAYS, today, allow_ai=False)
    out = []
    for r in data["items"]:
        k = risk_kind(r)
        if not k or (r["days_to_start"] is not None and r["days_to_start"] < 0 and not r["is_critical"]):
            continue
        units = r.get("units") or []
        units = [u for u in units if u != "未标注单位"] or _units(conn, user["project_id"], r["code"])
        for company in units or [""]:
            h = history(conn, user["project_id"], company, k, today) if company else {"n": 0, "open": 0, "cats": {}, "ids": []}
            out.append({"code": r["code"], "name": r["name"], "start": r.get("plan_start"), "days_to_start": r["days_to_start"], "critical": r["is_critical"],
                        "kind": k, "kind_name": KIND_NAME[k], "company": company, "hist": h, "checks": list(CHECKS[k]),
                        "gate": {"verdict": r["verdict_name"], "short": r.get("short_labels") or [], "unknown_n": r.get("unknown_n", 0)}})
    out.sort(key=lambda x: (x["days_to_start"] if x["days_to_start"] is not None else 99, -x["hist"]["n"], x["code"]))
    return out[:20]


def rule_focus(r: dict) -> str:
    h = r["hist"]
    who = r["company"] or "负责单位"
    if h["n"]:
        top = "、".join(f"{k}{v}次" for k, v in h["cats"].items())
        return f"{who}近 30 天同类隐患 {h['n']} 次（{top}），开工前重点核对：{r['checks'][0]}。"
    return f"{who}近 30 天无同类隐患记录，开工前核对：{r['checks'][0]}、{r['checks'][1]}。"


SYSTEM = """你是施工项目的安全智能体，正在给安全主管列“未来两周高风险作业”的预控重点。
输入是系统统计好的作业清单：作业、开工日期、作业类型、负责单位、该单位近 30 天同类隐患次数与类别、开工前要核对的条件。
请为每个作业写一句预控重点，不超过 45 字：先点出该单位的历史问题（没有就不提），再说开工前最该盯的一条。
硬性要求：只能使用输入里的数字；不得推测原因；不写罚款；不要 markdown。
只输出 JSON：{"items": [{"code": "作业代码", "company": "单位", "focus": "……"}]}"""
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _guard(text: str, r: dict) -> bool:
    src = json.dumps({k: r[k] for k in ("code", "name", "start", "days_to_start", "hist", "checks")}, ensure_ascii=False) + " 30"
    ok = {f"{float(x):g}" for x in _NUM.findall(src)}
    clean = re.sub(r"[A-Za-z]+\d+|\d+#|\d{4}-\d{2}-\d{2}", "", text)
    return all(f"{float(x):g}" in ok for x in _NUM.findall(clean)) and not re.search("罚|扣款|处罚", text)


def card(conn, user: dict, today: date | None = None) -> dict:
    today = today or date.today()
    rs = rows(conn, user, today)
    for r in rs:
        r["focus"], r["focus_source"] = rule_focus(r), "rule"
    if not rs:
        return {"items": [], "days": DAYS, "source": "rule", "note": ""}
    payload = [{k: r[k] for k in ("code", "name", "start", "kind_name", "company", "hist", "checks")} for r in rs]
    key = hashlib.sha256(json.dumps({"p": user["project_id"], "d": today.isoformat(), "x": payload, "v": PROMPT_VERSION}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
    hit = one(conn, "SELECT * FROM precontrol_cache WHERE key=?", (key,))
    note, source, got, run_id = "", "rule", {}, None
    if hit:
        got, source, note = json.loads(hit["data_json"]), hit["source"], hit["note"] or ""
    else:
        try:
            res = call_json("safety.precontrol", system=SYSTEM, user=json.dumps(payload, ensure_ascii=False), prompt_version=PROMPT_VERSION,
                            mock=lambda: {"items": [{"code": r["code"], "company": r["company"], "focus": r["focus"]} for r in rs]},
                            validate=lambda d: {"items": [x for x in (d or {}).get("items", []) if isinstance(x, dict) and x.get("code") and isinstance(x.get("focus"), str)]},
                            project_id=user["project_id"], user_id=user["id"], max_tokens=1500, temperature=0.2, input_meta={"rows": len(rs)})
            got = {f"{x['code']}|{x.get('company') or ''}": x["focus"].strip()[:80] for x in res.data["items"]}
            source, run_id = ("mock" if res.provider == "mock" else "ai"), res.run_id
            if source == "mock":
                note = "离线模拟模型输出，仅供开发测试"
        except AIError as exc:
            note = "AI 暂不可用，显示系统规则句（" + str(exc)[:30] + "）"
        if source == "ai":   # 只缓存真实模型成功的结果，失败、离线模拟不缓存（RJ 2026-10-09）；记下 run_id 便于追溯
            conn.execute("INSERT OR REPLACE INTO precontrol_cache(key, project_id, day, data_json, source, note, run_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (key, user["project_id"], today.isoformat(), json.dumps(got, ensure_ascii=False), source, note, run_id, now_iso()))
    bad = 0
    if source in ("ai", "mock"):   # 离线模拟同样走数字核对，标 focus_source=mock
        for r in rs:
            t = got.get(f"{r['code']}|{r['company']}")
            if t and _guard(t, r):
                r["focus"], r["focus_source"] = t, source
            elif t:
                bad += 1
        if bad:
            note = f"{bad} 条 AI 预控重点里有统计中没有的数字，已改用系统规则句"
    for r in rs:
        r["notice"] = notice_state(conn, user["project_id"], f"{r['code']}|{r['company']}")
    return {"items": rs, "days": DAYS, "source": source, "note": note}


def notice_state(conn, pid: int, ref: str) -> dict | None:
    """这条作业 + 单位最近一份预控提醒：草稿 / 已下发（签收状态）。"""
    d = one(conn, "SELECT id, status FROM gongbi_docs WHERE project_id=? AND kind='precontrol_notice' AND ref=? ORDER BY version DESC LIMIT 1", (pid, ref)) \
        if one(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='gongbi_docs'") else None
    if not d:
        return None
    out = {"doc_id": d["id"], "status": d["status"]}
    lt = one(conn, "SELECT * FROM sub_letters WHERE doc_id=?", (d["id"],)) if d["status"] == "confirmed" else None
    if lt:
        from . import sub_letters
        st = sub_letters.state(conn, lt)
        out.update(letter_id=lt["id"], letter_status=st["status_name"])
    return out


# ============================================================================ 致分包文书：高风险作业预控提醒（precontrol_notice）

def notice_facts(conn, user: dict, kind: str, ref: str | None) -> dict:
    code, _, company = (ref or "").partition("|")
    if not code or not company:
        raise ValueError("请选择作业与负责单位")
    today = date.today()
    hit = next((r for r in rows(conn, user, today) if r["code"] == code and r["company"] == company), None)
    if not hit:
        raise LookupError("该作业不在未来两周高风险作业清单里")
    project = one(conn, "SELECT name FROM projects WHERE id=?", (user["project_id"],))["name"]
    due = (date.fromisoformat(hit["start"][:10]) - timedelta(days=1)).isoformat() if hit.get("start") and hit["start"][:10] > today.isoformat() else today.isoformat()
    from . import sub_letters
    return {"project": project, "ref": f"{code}|{company}", "key": f"{code}|{company}", "src_type": "precontrol", "src_id": None, "area": "safety", "area_name": "安全", "company": company,
            "title": f"{code} {hit['name']}", "activity_code": code, "activity_name": hit["name"], "start": (hit.get("start") or "")[:10], "kind_name": hit["kind_name"],
            "hist": hit["hist"], "checks": hit["checks"], "gate": hit["gate"], "date": today.isoformat(), "due_date": due,
            "requirement": "请在开工前逐项落实下列预控措施，并组织作业班组安全交底；落实情况在开工前回复", "basis": sub_letters.basis(conn, user["project_id"], "safety")}


def notice_text(f: dict) -> str:
    h = f["hist"]
    L = [f"【统计事实】项目：{f['project']}；致：{f['company']}；类别：安全（高风险作业预控）",
         f"作业：{_an.fmt(f['activity_code'], f['activity_name'])}；作业类型：{f['kind_name']}；计划开工：{f['start'] or '未知'}",
         f"该单位近 30 天同类安全隐患 {h['n']} 次，未闭环 {h['open']} 次" + ("；类别：" + "、".join(f"{k} {v} 次" for k, v in h["cats"].items()) if h["cats"] else "")
         + ("；编号：" + "、".join(f"[S{i}]" for i in h["ids"]) if h["ids"] else ""),
         "开工前核对：" + "；".join(f["checks"]),
         f"放行清单：{f['gate']['verdict']}" + (f"，缺：{'、'.join(f['gate']['short'][:3])}" if f["gate"]["short"] else "") + (f"，另 {f['gate']['unknown_n']} 项待现场确认" if f["gate"]["unknown_n"] else ""),
         f"要求：{f['requirement']}", f"期限：{f['due_date']}", f["basis"]]
    return "\n".join(L)


def notice_rule(kind: str, f: dict) -> dict:
    h = f["hist"]
    hist = (f"贵单位近 30 天同类安全隐患 {h['n']} 次（" + "、".join(f"{k} {v} 次" for k, v in h["cats"].items()) + "）" + "、".join(f"[S{i}]" for i in h["ids"]) + "。") if h["n"] else "贵单位近 30 天无同类安全隐患记录。"
    return {"subject": f"关于{f['activity_code']} {f['activity_name']}（{f['kind_name']}）开工前安全预控",
            "fact": f"根据项目进度计划，{f['activity_code']} {f['activity_name']} 计划于 {f['start'] or '近期'} 开工，属{f['kind_name']}类高风险作业。{hist}",
            "requirement": "请在开工前逐项落实：" + "；".join(f["checks"]) + "；并组织作业班组安全交底。",
            "deadline": f"请于 {f['due_date']} 前落实并签收回复。", "basis": f["basis"] + "。"}


def notice_refs(conn, user: dict) -> list[dict]:
    """工笔文书页的可选对象：未来两周高风险作业 × 负责单位（只给安全主管）。"""
    if user["role"] not in ROLES:
        return []
    return [{"key": f"{r['code']}|{r['company']}", "label": f"{(r['start'] or '')[5:10]} {r['code']} {r['name']}（{r['company']}）",
             "notice": f"{r['kind_name']} · 近 30 天同类隐患 {r['hist']['n']} 次"} for r in rows(conn, user) if r["company"]]


def register_type() -> None:
    from ..ai import docs_registry as reg
    _s = reg._sec
    reg.TYPES["precontrol_notice"] = {
        "name": "高风险作业预控提醒（致分包）", "zh": "预控提醒", "lang": "zh", "ref": "precontrol", "group": "函件", "outward": True,
        "roles": ROLES, "confirm": ROLES, "intro": "未来两周的吊装、水上、深基坑、交通导改等作业，开工前提醒负责单位落实预控措施。安全主管确认后下发到该单位分包安全员，签收即销项。不写罚款。",
        "title": "{company} 高风险作业预控提醒 — {title}",
        "sections": [
            _s("subject", "事由", "一句话写明作业与作业类型。", max_chars=60),
            _s("fact", "一、作业与历史情况", "写明计划开工日期、作业类型，以及该单位近 30 天同类隐患情况，引用[S编号]；只写事实。", max_chars=240),
            _s("requirement", "二、预控要求", "逐项写开工前要落实的措施，只来自事实里的核对项。不写罚款、扣款或处罚结论。", max_chars=220),
            _s("deadline", "三、期限", "写明落实与回复期限。", max_chars=60),
            _s("basis", "四、依据", "写事实里给出的依据，不编条款号。", max_chars=80),
        ],
        "facts": notice_facts, "text": notice_text, "rule": notice_rule, "refs": notice_refs,
        "tables": lambda f: {},
    }
    if "precontrol_notice" not in reg.SUB_KINDS:
        reg.SUB_KINDS = reg.SUB_KINDS + ("precontrol_notice",)
    reg.NEW_KINDS = tuple(k for k, t in reg.TYPES.items() if not t.get("legacy"))
