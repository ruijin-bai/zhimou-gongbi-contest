"""P6 计划的扩展字段：日历、约束、自由浮时、作业 UDF（数量 / 计划工效 / 单位）。

与 activities 同一导入批次（import_id），只新增表、不改原表；没有扩展数据的批次（旧导入、Excel）
各功能按“无数据”如实显示，排程退回旧口径。XER 原文件不落盘，只存解析结果。
"""
from __future__ import annotations

import re

from ..db import all_, dumps, loads, one
from . import p6
from .p6cal import parse_calendar

SCHEMA = """
CREATE TABLE IF NOT EXISTS p6_calendars (
    import_id INTEGER NOT NULL REFERENCES p6_imports(id) ON DELETE CASCADE,
    clndr_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    day_hr REAL,
    week_hr REAL,
    is_default INTEGER NOT NULL DEFAULT 0,
    spec_json TEXT,
    PRIMARY KEY (import_id, clndr_id)
);
CREATE TABLE IF NOT EXISTS p6_act_ext (
    import_id INTEGER NOT NULL REFERENCES p6_imports(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    clndr_id TEXT,
    cstr_type TEXT, cstr_date TEXT,
    cstr_type2 TEXT, cstr_date2 TEXT,
    free_float_d REAL,
    qty_text TEXT, unit TEXT, prod_text TEXT,
    PRIMARY KEY (import_id, code)
);
"""

# P6 约束类型 → 中文；hard=硬约束（会覆盖逻辑关系）
CSTR = {
    "CS_MSO": ("开始于", True), "CS_MEO": ("完成于", True), "CS_MANDSTART": ("强制开始", True), "CS_MANDFIN": ("强制完成", True),
    "CS_MSOA": ("开始不早于", False), "CS_MSOB": ("开始不晚于", False), "CS_MEOA": ("完成不早于", False), "CS_MEOB": ("完成不晚于", False),
    "CS_ALAP": ("尽可能晚", False),
}
# “不晚于”类约束日期就是节点的控制日期
DEADLINE_TYPES = ("CS_MEOB", "CS_MEO", "CS_MANDFIN", "CS_MSOB", "CS_MSO", "CS_MANDSTART")

_UDF_LABELS = {"quantity": ("quantity", "工程量", "数量"), "productivity": ("productivity", "工效", "生产率"), "unit": ("unit", "单位")}


def _udf_kind(label: str) -> str | None:
    low = (label or "").strip().lower()
    for kind, keys in _UDF_LABELS.items():
        if any(k in low for k in keys):
            return kind
    return None


def extract(tables: dict, codes: set[str]) -> tuple[list[dict], list[dict]]:
    """XER 表 → (日历行, 作业扩展行)。只取导入的那个项目（作业最多的 proj_id），与 p6.activities_from_xer 一致。"""
    tasks = tables.get("TASK") or []
    counts: dict[str, int] = {}
    for r in tasks:
        counts[r.get("proj_id", "")] = counts.get(r.get("proj_id", ""), 0) + 1
    if not counts:
        return [], []
    proj_id = max(counts, key=counts.get)
    project = next((p for p in tables.get("PROJECT", []) if p.get("proj_id") == proj_id), {})
    cal_rows = {c.get("clndr_id"): c for c in tables.get("CALENDAR", [])}
    hpd = {cid: p6._f(c.get("day_hr_cnt")) or 8.0 for cid, c in cal_rows.items()}
    udf_kind = {u.get("udf_type_id"): _udf_kind(u.get("udf_type_label") or "") for u in tables.get("UDFTYPE", [])
                if (u.get("table_name") or "") == "TASK"}
    udf: dict[str, dict] = {}
    for v in tables.get("UDFVALUE", []):
        kind = udf_kind.get(v.get("udf_type_id"))
        text = (v.get("udf_text") or v.get("udf_number") or "").strip()
        if kind and text:
            udf.setdefault(v.get("fk_id"), {}).setdefault(kind, text)
    acts, used = [], set()
    for r in tasks:
        code = (r.get("task_code") or "").strip()
        if r.get("proj_id") != proj_id or code not in codes:
            continue
        cid = r.get("clndr_id") or ""
        used.add(cid)
        u = udf.get(r.get("task_id"), {})
        acts.append({"code": code, "clndr_id": cid,
                     "cstr_type": r.get("cstr_type") or None, "cstr_date": p6._d(r.get("cstr_date")),
                     "cstr_type2": r.get("cstr_type2") or None, "cstr_date2": p6._d(r.get("cstr_date2")),
                     "free_float_d": p6._hours_to_days(p6._f(r.get("free_float_hr_cnt")), hpd.get(cid, 8.0)),
                     "qty_text": u.get("quantity"), "unit": u.get("unit"), "prod_text": u.get("productivity")})
    default = project.get("clndr_id")
    used.add(default)
    cals = []
    for cid in sorted(c for c in used if c in cal_rows):
        c = cal_rows[cid]
        spec = parse_calendar(c.get("clndr_data") or "")
        cals.append({"clndr_id": cid, "name": (c.get("clndr_name") or "").strip(), "day_hr": hpd.get(cid),
                     "week_hr": p6._f(c.get("week_hr_cnt")), "is_default": 1 if cid == default else 0,
                     "spec_json": dumps(spec) if spec else None})
    return cals, acts


def save(conn, import_id: int, tables: dict, codes: set[str]) -> dict:
    cals, acts = extract(tables, codes)
    conn.executemany("INSERT OR REPLACE INTO p6_calendars (import_id, clndr_id, name, day_hr, week_hr, is_default, spec_json) VALUES (?,?,?,?,?,?,?)",
                     [(import_id, c["clndr_id"], c["name"], c["day_hr"], c["week_hr"], c["is_default"], c["spec_json"]) for c in cals])
    conn.executemany("INSERT OR REPLACE INTO p6_act_ext (import_id, code, clndr_id, cstr_type, cstr_date, cstr_type2, cstr_date2, free_float_d,"
                     " qty_text, unit, prod_text) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     [(import_id, a["code"], a["clndr_id"], a["cstr_type"], a["cstr_date"], a["cstr_type2"], a["cstr_date2"],
                       a["free_float_d"], a["qty_text"], a["unit"], a["prod_text"]) for a in acts])
    return {"calendars": len(cals), "parsed": sum(1 for c in cals if c["spec_json"]), "ext": len(acts)}


def has_ext(conn, import_id: int) -> bool:
    return bool(one(conn, "SELECT 1 FROM p6_act_ext WHERE import_id=? LIMIT 1", (import_id,)))


def calendars(conn, import_id: int) -> list[dict]:
    rows = all_(conn, "SELECT * FROM p6_calendars WHERE import_id=? ORDER BY clndr_id", (import_id,))
    for r in rows:
        r["spec"] = loads(r.pop("spec_json"), None) if r.get("spec_json") else None
    return rows


def ext_map(conn, import_id: int) -> dict[str, dict]:
    return {r["code"]: r for r in all_(conn, "SELECT * FROM p6_act_ext WHERE import_id=?", (import_id,))}


# --------------------------------------------------------------------------- UDF 文本 → 数值

_NUM = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*([A-Za-z一-鿿0-9³²]*)")
_UNIT_ALIAS = {"m³": "m3", "m²": "m2", "米": "m", "立方米": "m3", "方": "m3", "平方米": "m2", "吨": "t", "个": "no", "根": "no", "片": "no",
               "nos": "no", "nr": "no", "number": "no", "km": "km"}


def norm_unit(u: str | None) -> str:
    u = (u or "").strip().lower().replace(" ", "")
    return _UNIT_ALIAS.get(u, u)


def parse_qty(text: str | None) -> tuple[float | None, str]:
    """“92,124 m3”→(92124, "m3")；“1000”→(1000, "")；“6km”→(6, "km")；读不出数字为 (None, "")。"""
    m = _NUM.search(text or "")
    if not m:
        return None, ""
    try:
        return float(m.group(1).replace(",", "")), norm_unit(m.group(2))
    except ValueError:
        return None, ""


def parse_prod(text: str | None) -> tuple[float | None, str]:
    """“50m/d”“2000m3/d”“1,500 m3/天”→(数值, 单位)；没有“/d、/天”也按每天读；读不出为 (None, "")。"""
    t = (text or "").strip()
    m = re.match(r"\s*(-?\d[\d,]*(?:\.\d+)?)\s*([^/\s]*)\s*(?:/\s*(d|day|天|日))?", t, re.IGNORECASE)
    if not m:
        return None, ""
    try:
        return float(m.group(1).replace(",", "")), norm_unit(m.group(2))
    except ValueError:
        return None, ""


def quantity_of(ext: dict) -> tuple[float | None, str]:
    """作业数量与单位：数量文字自带单位的用自带的，否则用“单位”UDF。"""
    q, u = parse_qty(ext.get("qty_text"))
    return q, (u or norm_unit(ext.get("unit")))
