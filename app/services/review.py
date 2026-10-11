"""“AI 建议｜依据｜人工决定”核对记录：逐字段保存 AI 原值与人工最终值，并据此算字段级采纳率/修改率。

三处使用（界面见 app/static/x_ui.js 的 ReviewTable）：
- week_progress  周进度确认：每个 P6 作业一行，决定 确认 / 修改 / 不采纳 / 无法判断（后两者不提交数量，不补零）；
- dq_check       日志预审：数据审核Agent 的每条检查结论一行，决定 属实 / 误报（误报必填原因）；
- ticket         材料小票识别：每个字段一行，确认后才计入（既有逻辑不变），决定由 AI 原值与最终值推导。

口径（如实）：
- 采纳率 = 确认 /（确认+修改+不采纳）；修改率、不采纳率同分母；“无法判断”与“人工补填（AI 没识别出值）”单列，不进分母；
- 属实率 = 属实 /（属实+误报）；
- 已判定字段不足 MIN_SAMPLES 个时只给数量，不给比例，提示“样本不足，尚无结论”。
AI 原值/最终值以 JSON 文本存放：未知存 NULL，不补零。
"""
from __future__ import annotations

import math
from typing import Any

from ..db import all_, dumps, loads, now_iso

MIN_SAMPLES = 10
ADOPT = ("confirm", "modify", "reject", "unknown")
VERIFY = ("true", "false")
DECISION_NAME = {"confirm": "确认", "modify": "修改", "reject": "不采纳", "unknown": "无法判断", "true": "属实", "false": "误报"}
KINDS = {"week_progress": ("周进度确认", "adopt"), "dq_check": ("日志预审", "verify"), "ticket": ("小票字段", "adopt")}
INSUFFICIENT = "样本不足，尚无结论"


class ReviewError(ValueError):
    pass


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool) or (isinstance(v, str) and not v.strip()):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def check_decisions(expected: list[str], decisions: Any, mode: str, labels: dict[str, str] | None = None,
                    numeric: bool = False) -> dict[str, dict]:
    """校验一次提交的全部决定。返回 {key: {decision, final, reason}}；任何一项不合格即抛 ReviewError（不落任何数据）。

    规则：每一项都必须有决定（未决定的给出剩余数）；modify 必须给最终值（numeric 时须为 ≥0 的数）；误报（false）必填原因。
    """
    labels = labels or {}
    allowed = ADOPT if mode == "adopt" else VERIFY
    if not isinstance(decisions, list):
        raise ReviewError("决定格式不正确")
    exp = set(expected)
    got: dict[str, dict] = {}
    for d in decisions:
        if not isinstance(d, dict) or "key" not in d:
            raise ReviewError("决定格式不正确")
        key = str(d["key"])
        if key not in exp:
            raise ReviewError(f"决定里有不属于本次核对的项：{key}")
        dec = d.get("decision")
        if dec not in allowed:
            raise ReviewError(f"「{labels.get(key, key)}」的决定不正确，可选：{'、'.join(DECISION_NAME[a] for a in allowed)}")
        reason = str(d.get("reason") or "").strip()[:200]
        final = d.get("final")
        if dec == "false" and not reason:
            raise ReviewError(f"「{labels.get(key, key)}」判为误报，请填写原因")
        if dec == "modify":
            if numeric:
                final = _num(final)
                if final is None or final < 0:
                    raise ReviewError(f"「{labels.get(key, key)}」选了修改，请填写不小于 0 的数字")
            elif final is None or (isinstance(final, str) and not final.strip()):
                raise ReviewError(f"「{labels.get(key, key)}」选了修改，请填写修改后的值")
        else:
            final = None
        got[key] = {"decision": dec, "final": final, "reason": reason}
    missing = [k for k in expected if k not in got]
    if missing:
        raise ReviewError(f"还有 {len(missing)} 项未做决定，全部决定后才能提交")
    return got


def save_rows(conn, project_id: int, kind: str, ref_type: str, ref_id: int, rows: list[dict], user_id: int | None) -> None:
    """保存/覆盖某对象的逐字段记录（重新提交以最新一次为准）。rows: [{key,label,ai,final,decision,reason}]。须在调用方事务内。"""
    keys = [r["key"] for r in rows]
    if keys:
        conn.execute(f"DELETE FROM review_fields WHERE kind=? AND ref_type=? AND ref_id=? AND field_key NOT IN ({','.join('?' for _ in keys)})",
                     (kind, ref_type, ref_id, *keys))
    else:
        conn.execute("DELETE FROM review_fields WHERE kind=? AND ref_type=? AND ref_id=?", (kind, ref_type, ref_id))
    for r in rows:
        conn.execute("""INSERT INTO review_fields (project_id, kind, ref_type, ref_id, field_key, label, ai_value, final_value, decision, reason, decided_by, decided_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(kind, ref_type, ref_id, field_key) DO UPDATE SET label=excluded.label, ai_value=excluded.ai_value,
                          final_value=excluded.final_value, decision=excluded.decision, reason=excluded.reason,
                          decided_by=excluded.decided_by, decided_at=excluded.decided_at""",
                     (project_id, kind, ref_type, ref_id, r["key"], r.get("label") or "", None if r.get("ai") is None else dumps(r["ai"]),
                      None if r.get("final") is None else dumps(r["final"]), r["decision"], r.get("reason") or "", user_id, now_iso()))


def fields_of(conn, ref_type: str, ref_id: int, kind: str | None = None) -> list[dict]:
    """某对象的逐字段记录：AI 原值、人工最终值、决定与原因。"""
    sql, args = "SELECT * FROM review_fields WHERE ref_type=? AND ref_id=?", [ref_type, ref_id]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    out = []
    for r in all_(conn, sql + " ORDER BY id", args):
        out.append({"kind": r["kind"], "key": r["field_key"], "label": r["label"], "ai_value": loads(r["ai_value"]), "final_value": loads(r["final_value"]),
                    "decision": r["decision"], "decision_name": DECISION_NAME.get(r["decision"], r["decision"]), "reason": r["reason"],
                    "decided_by": r["decided_by"], "decided_at": r["decided_at"]})
    return out


def saved_map(conn, ref_type: str, ref_id: int, kind: str) -> dict[str, dict]:
    return {f["key"]: f for f in fields_of(conn, ref_type, ref_id, kind)}


def derive_ticket_decision(ai: Any, final: Any, hint: str | None = None) -> str | None:
    """小票字段的决定由 AI 原值与最终值推导（服务端为准，客户端不能把改过的值报成“确认”）。
    AI 与最终值都没有值 → None（无需记录，除非人明确标了“无法判断”）。"""
    hint = hint if hint in ADOPT else None
    if ai is None and final is None:
        return "unknown" if hint == "unknown" else None
    if ai is not None and final is not None and ai == final:
        return "confirm"
    if final is None:  # AI 有值而最终为空：人没采纳，或判断不了
        return "unknown" if hint == "unknown" else "reject"
    return "modify"  # 改值，或 AI 没识别、人工补填（ai 为空，统计里单列为“人工补填”）


def _rates(rows: list[dict], mode: str) -> dict:
    c = {k: 0 for k in (ADOPT if mode == "adopt" else VERIFY)}
    manual = 0
    for r in rows:
        if mode == "adopt" and r["decision"] == "modify" and r["ai_value"] is None:
            manual += 1  # AI 没给值、人工补填：不是对 AI 的修改
            continue
        if r["decision"] in c:
            c[r["decision"]] += 1
    judged = (c["confirm"] + c["modify"] + c["reject"]) if mode == "adopt" else (c["true"] + c["false"])
    out: dict = {"fields_total": len(rows), "judged": judged, **c}
    if mode == "adopt":
        out["manual_fill"] = manual
    enough = judged >= MIN_SAMPLES
    out["enough"] = enough
    keys = ("adopt_rate", "modify_rate", "reject_rate") if mode == "adopt" else ("true_rate", "false_rate")
    nums = ("confirm", "modify", "reject") if mode == "adopt" else ("true", "false")
    for k, n in zip(keys, nums):
        out[k] = round(c[n] / judged, 4) if enough else None
    out["note"] = "" if enough else f"{INSUFFICIENT}（已判定 {judged} 个字段，至少 {MIN_SAMPLES} 个才给比例）"
    return out


def stats(conn, project_id: int, kind: str | None = None) -> dict:
    """字段级采纳率/修改率（及日志预审的属实率/误报率）。样本不足时比例为 None 并给出说明，不造数字。"""
    out = {}
    for k, (name, mode) in KINDS.items():
        if kind and k != kind:
            continue
        rows = [{"decision": r["decision"], "ai_value": r["ai_value"], "label": r["label"], "key": r["field_key"]}
                for r in all_(conn, "SELECT decision, ai_value, label, field_key FROM review_fields WHERE project_id=? AND kind=?", (project_id, k))]
        item = {"kind": k, "name": name, "mode": mode, **_rates(rows, mode)}
        if k == "ticket":  # 小票按字段拆开（同一字段名下比较采纳情况）
            by: dict[str, list[dict]] = {}
            for r in rows:
                by.setdefault(r["key"], []).append(r)
            item["by_field"] = [{"key": f, "label": rs[0]["label"] or f, **_rates(rs, mode)} for f, rs in sorted(by.items())]
        out[k] = item
    return {"min_samples": MIN_SAMPLES, "kinds": out,
            "definition": "采纳率=确认/(确认+修改+不采纳)；无法判断与人工补填单列、不进分母；日志预审为属实率=属实/(属实+误报)。样本不足时不给比例。"}
