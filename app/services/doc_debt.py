"""资料欠账：完工未验收、已用未复试（2026-10-08 增补能力 8）。

- 哪些作业算“完工”：P6 实际完成、P6 完成% ≥ 100，或已确认日志（周确认）里的累计完成% ≥ 100；
- 档案Agent 按作业核对（全部确定性规则，不调用模型）：
  ① 隐蔽验收：复用资料Agent 的“疑似缺验收资料”扫描（按作业名与 WBS 匹配验收模板），没有已齐 / 已报验 / 已通过记录的列为欠账，
     有草稿但缺拍或被退回的写“有记录未齐”；
  ② 材料复试：作业所属工序用到的材料类别（混凝土类 → 水泥、砂、石子、外加剂；钢筋/模板、桩基、混凝土 → 钢筋）
     在作业完工前到场、复试仍是待取样 / 已送检 / 不合格的批次，列为“已用未复试请核对”；
- “疑似”口径与资料Agent 一致：是否确需验收、复试，以设计、规范和监理要求为准；
- 可一键起草“资料补齐通知”（致分包文书的一种），工程主管确认后才下发到负责该作业的分包技术员，签收即销项；资料补齐后欠账自动消失。
"""
from __future__ import annotations

from datetime import date, timedelta

from ..db import all_, one
from . import conceal, p6
from .logs import progress_state
from .parts import activity_part
from . import act_names as _an

ROLES = frozenset({"eng_sup"})
MAT_BY_CAT = {"concrete": ("cement", "sand", "stone", "admixture", "rebar"), "rebar_form": ("rebar",), "pile": ("rebar", "cement", "sand", "stone"),
              "pave": ("cement", "stone", "sand")}
RETEST_NAME = {"pending": "待取样", "sampled": "已送检", "fail": "复试不合格"}


def _done_acts(conn, pid: int) -> list[dict]:
    imp = p6.current_import(conn, pid)
    if not imp:
        return []
    st = progress_state(conn, pid, imp)
    out = []
    for a in p6.current_activities(conn, pid):
        if a.get("is_milestone"):
            continue
        log_pct = (st.get(a["code"]) or {}).get("pct")
        if a.get("act_finish"):
            basis, fin = f"P6 实际完成 {a['act_finish'][:10]}", a["act_finish"][:10]
        elif (a.get("phys_pct") or 0) >= 100:
            basis, fin = "P6 完成 100%", (a.get("early_finish") or "")[:10] or None
        elif log_pct is not None and log_pct >= 100:
            basis, fin = f"周确认累计完成 100%（{(st[a['code']].get('pct_date') or '')}）", st[a["code"]].get("pct_date")
        else:
            continue
        out.append({**a, "_basis": basis, "_finish": fin})
    return out


def _units(conn, pid: int, act: dict) -> list[str]:
    return sorted({u["company"] for u in all_(conn, "SELECT company, wbs_scope FROM users WHERE project_id=? AND active=1 AND role='tech'", (pid,))
                   if u["company"] and (u["wbs_scope"] or "").strip() and p6.in_scope(act, u["wbs_scope"])})


def build(conn, user: dict) -> dict:
    pid = user["project_id"]
    acts = _done_acts(conn, pid)
    if not acts:
        return {"items": [], "summary": {"acts": 0, "conceal": 0, "material": 0}, "note": conceal_note(), "has_plan": bool(p6.current_import(conn, pid))}
    done = {a["code"]: a for a in acts}
    # ① 隐蔽验收：资料Agent 的扫描结果里属于完工作业的
    cg = conceal.gaps(conn, {**user, "role": "eng_sup"})
    debts: dict[str, list[dict]] = {}
    for g in cg["gaps"]:
        if g["activity_code"] in done:
            debts.setdefault(g["activity_code"], []).append({"type": "conceal", "text": f"{g['template_name']}：{g['state_name']}",
                                                             "record_ids": g["record_ids"], "template_key": g["template_key"]})
    # ② 材料复试
    try:
        from . import gate
        cats = gate.classify_all(conn, pid, acts, allow_ai=False)
    except Exception:  # noqa: BLE001  放行归类不可用时只是少一类核对
        cats = {}
    batches = all_(conn, "SELECT * FROM material_batches WHERE project_id=? AND retest_status IN ('pending','sampled','fail') AND arrived_at IS NOT NULL", (pid,)) \
        if one(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='material_batches'") else []
    from ..ai.ticket_agent import CATEGORIES
    for a in acts:
        need = MAT_BY_CAT.get((cats.get(a["code"]) or {}).get("category"), ())
        fin = a["_finish"] or date.today().isoformat()
        for b in batches:
            if b["category"] in need and b["arrived_at"][:10] <= fin:
                debts.setdefault(a["code"], []).append({"type": "material", "batch_id": b["id"],
                                                        "text": f"{CATEGORIES.get(b['category'], b['category'])} {b['name'] or ''} 批号{b['batch_no'] or '未记录'}（{b['arrived_at'][:10]} 到场）：{RETEST_NAME[b['retest_status']]}，请核对是否已用于本作业"})
    items = []
    for code, ds in debts.items():
        a = done[code]
        items.append({"code": code, "name": a["name"], "part": activity_part(a) or "未归部位", "basis": a["_basis"], "finish": a["_finish"], "units": _units(conn, pid, a),
                      "debts": ds, "notice": _notice(conn, pid, code)})
    items.sort(key=lambda x: (x["finish"] or "9999", x["code"]))
    return {"items": items, "summary": {"acts": len(items), "conceal": sum(1 for i in items for d in i["debts"] if d["type"] == "conceal"),
                                        "material": sum(1 for i in items for d in i["debts"] if d["type"] == "material")},
            "note": conceal_note(), "has_plan": True}


def conceal_note() -> str:
    return "“疑似”：依据 = 作业已完工 + 系统内没找到对应的已齐/已报验/已通过验收记录或复试结果；是否确需验收、复试，以设计、规范和监理要求为准。"


def _notice(conn, pid: int, code: str) -> dict | None:
    if not one(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='gongbi_docs'"):
        return None
    d = one(conn, "SELECT id, status, ref FROM gongbi_docs WHERE project_id=? AND kind='docdebt_notice' AND ref LIKE ? ORDER BY id DESC LIMIT 1", (pid, code + "|%"))
    if not d:
        return None
    out = {"doc_id": d["id"], "status": d["status"]}
    lt = one(conn, "SELECT * FROM sub_letters WHERE doc_id=?", (d["id"],)) if d["status"] == "confirmed" else None
    if lt:
        from . import sub_letters
        out.update(letter_id=lt["id"], letter_status=sub_letters.state(conn, lt)["status_name"])
    return out


# ============================================================================ 致分包文书：资料补齐通知（docdebt_notice）

def notice_facts(conn, user: dict, kind: str, ref: str | None) -> dict:
    code, _, company = (ref or "").partition("|")
    if not code or not company:
        raise ValueError("请选择作业与负责单位")
    hit = next((i for i in build(conn, user)["items"] if i["code"] == code), None)
    if not hit:
        raise LookupError("该作业没有资料欠账")
    if company not in hit["units"]:
        raise ValueError("该单位不负责这个作业")
    project = one(conn, "SELECT name FROM projects WHERE id=?", (user["project_id"],))["name"]
    due = (date.today() + timedelta(days=3)).isoformat()
    from . import sub_letters
    return {"project": project, "ref": f"{code}|{company}", "key": f"{code}|{company}", "src_type": "docdebt", "src_id": None, "area": "quality", "area_name": "质量",
            "company": company, "title": f"{code} {hit['name']}", "activity_code": code, "activity_name": hit["name"], "part": hit["part"], "basis_done": hit["basis"],
            "debts": [d["text"] for d in hit["debts"]], "date": date.today().isoformat(), "due_date": due,
            "requirement": "请补齐下列验收资料或复试报告，在系统中完成验收记录或录入复试结果；确无需验收或复试的请书面说明", "basis": sub_letters.basis(conn, user["project_id"], "quality")}


def notice_text(f: dict) -> str:
    return "\n".join([f"【统计事实】项目：{f['project']}；致：{f['company']}；类别：质量（资料补齐）",
                      f"作业：{_an.fmt(f['activity_code'], f['activity_name'])}（部位：{f['part']}）；完工依据：{f['basis_done']}",
                      "资料欠账：" + "；".join(f["debts"]), f"要求：{f['requirement']}", f"期限：{f['due_date']}", f["basis"]])


def notice_rule(kind: str, f: dict) -> dict:
    return {"subject": f"关于{f['activity_code']} {f['activity_name']}完工资料补齐",
            "fact": f"{f['activity_code']} {f['activity_name']}（{f['part']}）已完工（{f['basis_done']}），系统核对发现以下资料尚未齐全：" + "；".join(f["debts"]) + "。",
            "requirement": f["requirement"] + "。", "deadline": f"请于 {f['due_date']} 前补齐并签收回复。", "basis": f["basis"] + "。"}


def notice_refs(conn, user: dict) -> list[dict]:
    """工笔文书页的可选对象：有资料欠账的完工作业 × 负责单位（只给工程主管）。"""
    if user["role"] not in ROLES:
        return []
    return [{"key": f"{i['code']}|{u}", "label": f"{i['code']} {i['name']}（{u}）", "notice": f"欠账 {len(i['debts'])} 项"} for i in build(conn, user)["items"] for u in i["units"]]


def register_type() -> None:
    from ..ai import docs_registry as reg
    _s = reg._sec
    reg.TYPES["docdebt_notice"] = {
        "name": "资料补齐通知（致分包）", "zh": "资料补齐通知", "lang": "zh", "ref": "docdebt", "group": "函件", "outward": True,
        "roles": ROLES, "confirm": ROLES, "intro": "完工作业缺隐蔽验收记录或材料复试结果时，通知负责单位补齐。工程主管确认后下发到该单位技术员，签收即销项；资料补齐后欠账自动消失。不写罚款。",
        "title": "{company} 资料补齐通知 — {title}",
        "sections": [
            _s("subject", "事由", "一句话写明作业与资料补齐。", max_chars=60),
            _s("fact", "一、欠账情况", "写明作业完工依据与缺的资料，只写事实。", max_chars=300),
            _s("requirement", "二、要求", "写明需要补齐的资料。不写罚款、扣款或处罚结论。", max_chars=200),
            _s("deadline", "三、期限", "写明补齐期限。", max_chars=60),
            _s("basis", "四、依据", "写事实里给出的依据，不编条款号。", max_chars=80),
        ],
        "facts": notice_facts, "text": notice_text, "rule": notice_rule, "tables": lambda f: {}, "refs": notice_refs,
    }
    if "docdebt_notice" not in reg.SUB_KINDS:
        reg.SUB_KINDS = reg.SUB_KINDS + ("docdebt_notice",)
    reg.NEW_KINDS = tuple(k for k, t in reg.TYPES.items() if not t.get("legacy"))
