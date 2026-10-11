"""材料Agent 服务：票据草稿 → 人确认 → 自动累计浇筑方量 / 试块 / 材料进场批次与复试台账。

套路与指挥Agent、档案Agent一致：确定性数据 + Agent 只做提取 + 规则层校验 + 人确认 + audit。
- ai_json 是模型原始提取（只读）；confirmed_json 与表列是人核对后的结果，只有 status=confirmed 的票据才计入任何累计；
- 理论方量、强度评定、复试结论都由人填写，系统不估算、不补零、不判定合格；
- 超耗/少浇、少留试块、已用未复试一律表述为“疑点/请核对”，不下结论；
- 不做库存、采购、结算：材料批次只是从拍照自动长出来的台账底稿。
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta
from typing import Any

from .. import roles as R
from ..ai import ticket_agent as TA
from ..ai.client import AIError
from ..config import settings
from ..db import all_, audit, connect, dumps, event, insert, loads, now_iso, one, tx
from . import media as media_svc
from . import review as review_svc
from . import role_views

log = logging.getLogger("zhimou.tickets")

DOC_KINDS = TA.DOC_KINDS
CATEGORIES = TA.CATEGORIES
RETEST = {"not_required": "无需复试", "pending": "待取样", "sampled": "已送检", "pass": "复试合格", "fail": "复试不合格"}
POUR_STATUS = {"planned": "计划", "pouring": "浇筑中", "done": "已完成"}
CURING = ("标准养护", "同条件养护")
MAX_PHOTOS = 4
# 各类单据在核对界面里出现的字段（与前端 x_tickets.js 的字段表一致；混凝土小票的单位固定 m³，不逐项核对）
KIND_FIELDS = {
    "concrete_ticket": ("supplier", "ticket_no", "vehicle", "issued_at", "grade", "qty", "slump", "temp", "part", "receiver", "remark"),
    "material_receipt": ("supplier", "ticket_no", "vehicle", "issued_at", "material", "spec", "qty", "unit", "batch_no", "receiver", "remark"),
    "cert": ("supplier", "ticket_no", "issued_at", "material", "spec", "batch_no", "remark"),
}

NOTE_CONCRETE = "试块组数按通用常规要求测算，以项目试验计划和监理要求为准；强度评定以试验室/监理为准，本系统不做合格判定。"
NOTE_MATERIAL = "复试规则可在项目内调整，以项目检验试验计划和监理要求为准。"

DEFAULT_CONCRETE = {"specimen_per_m3": 100.0, "specimens_per_day": 1, "deviation_pct": 3.0, "warn_days": 3}
DEFAULT_MATERIAL = {
    "rebar": {"required": True, "batch_t": 60.0},
    "cement": {"required": True, "bag_t": 200.0, "bulk_t": 500.0},
    "sand": {"required": False}, "stone": {"required": False}, "admixture": {"required": False}, "other": {"required": False},
}


class TicketError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _today() -> str:
    return date.today().isoformat()


def _in(ids) -> str:
    return ",".join("?" for _ in ids) or "NULL"


def _iso(s: Any, field: str, *, future_ok: bool = False) -> str:
    try:
        d = date.fromisoformat(str(s)[:10])
    except ValueError:
        raise TicketError(400, f"{field}日期格式应为 YYYY-MM-DD") from None
    if not future_ok and d > date.today() + timedelta(days=1):
        raise TicketError(400, f"{field}不能在未来")
    return d.isoformat()


# ----------------------------------------------------------------------------- 权限与范围

READ_ROLES = frozenset({"tech", "eng_sup", "manager", "safety_sup", "leader"})
WRITE_ROLES = frozenset({"tech", "eng_sup", "manager"})
RULE_ROLES = frozenset({"manager", "eng_sup"})


def is_quality(user: dict) -> bool:
    """录试块结果、改复试结果：site_quality 能力，系统管理员不参与业务。"""
    return user["role"] != "admin" and R.can(user["role"], "site_quality")


def scope_of(conn, user: dict) -> list[int] | None:
    """None=全项目；否则只看这些人上传的票据（分包单位）。"""
    return role_views.scope_ids(conn, user)


def _names(conn, ids) -> dict[int, str]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {r["id"]: r["name"] for r in all_(conn, f"SELECT id, name FROM users WHERE id IN ({_in(ids)})", ids)}


# ----------------------------------------------------------------------------- 项目规则

def rules(conn, project_id: int) -> dict:
    row = one(conn, "SELECT config_json FROM projects WHERE id=?", (project_id,))
    cfg = loads(row["config_json"], {}) if row else {}
    conc = {**DEFAULT_CONCRETE, **(cfg.get("concrete_rules") or {})}
    mat = {k: {**v, **((cfg.get("material_rules") or {}).get(k) or {})} for k, v in DEFAULT_MATERIAL.items()}
    return {"concrete_rules": conc, "material_rules": mat}


def _num_in(v: Any, lo: float, hi: float, label: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TicketError(400, f"{label}应为数字") from None
    if not lo <= x <= hi:
        raise TicketError(400, f"{label}应在 {lo:g}~{hi:g} 之间")
    return x


def save_rules(conn, user: dict, patch: dict) -> dict:
    if user["role"] not in RULE_ROLES:
        raise TicketError(403, "仅项目经理、工程主管可修改规则")
    row = one(conn, "SELECT config_json FROM projects WHERE id=?", (user["project_id"],))
    cfg = loads(row["config_json"], {}) if row else {}
    conc = dict(cfg.get("concrete_rules") or {})
    for k, (lo, hi) in {"specimen_per_m3": (10, 1000), "specimens_per_day": (1, 5), "deviation_pct": (0.5, 30), "warn_days": (0, 14)}.items():
        if (patch.get("concrete_rules") or {}).get(k) is not None:
            v = _num_in(patch["concrete_rules"][k], lo, hi, {"specimen_per_m3": "每多少方一组", "specimens_per_day": "每浇筑日最少组数",
                                                           "deviation_pct": "方量差异阈值%", "warn_days": "临期天数"}[k])
            conc[k] = int(v) if k in ("specimens_per_day", "warn_days") else v
    mat = {k: dict(v) for k, v in (cfg.get("material_rules") or {}).items()}
    for cat, p in (patch.get("material_rules") or {}).items():
        if cat not in DEFAULT_MATERIAL:
            raise TicketError(400, f"未知材料类别：{cat}")
        cur = mat.setdefault(cat, {})
        if p.get("required") is not None:
            cur["required"] = bool(p["required"])
        for k in ("batch_t", "bag_t", "bulk_t"):
            if p.get(k) is not None:
                cur[k] = _num_in(p[k], 1, 5000, f"{CATEGORIES[cat]}取样批吨数")
    cfg["concrete_rules"], cfg["material_rules"] = conc, mat
    with tx(conn):
        conn.execute("UPDATE projects SET config_json=? WHERE id=?", (dumps(cfg), user["project_id"]))
        audit(conn, user["id"], "tickets.rules", f"project:{user['project_id']}", dumps({"concrete_rules": conc, "material_rules": mat})[:300])
    return rules_view(conn, user)


def rules_view(conn, user: dict) -> dict:
    return {**rules(conn, user["project_id"]), "can_edit": user["role"] in RULE_ROLES, "note_concrete": NOTE_CONCRETE, "note_material": NOTE_MATERIAL,
            "categories": CATEGORIES}


# ----------------------------------------------------------------------------- 票据：上传与识别

def _row(conn, tid: int, user: dict) -> dict:
    t = one(conn, "SELECT * FROM tickets WHERE id=? AND project_id=?", (tid, user["project_id"]))
    if not t:
        raise TicketError(404, "票据不存在")
    sc = scope_of(conn, user)
    if sc is not None and t["uploaded_by"] not in sc:
        raise TicketError(404, "票据不存在")
    return t


def _fields_of(t: dict) -> dict:
    cj = loads(t.get("confirmed_json"), None)
    if cj and cj.get("fields"):
        return cj["fields"]
    ai = loads(t.get("ai_json"), None)
    return ai["fields"] if ai and ai.get("fields") else {k: None for k in TA.FIELDS}


def create_draft(conn, user: dict, files: list[tuple[str, bytes, str | None, dict | None]], doc_kind: str) -> dict:
    if not 1 <= len(files) <= MAX_PHOTOS:
        raise TicketError(400, f"一张票据请拍 1~{MAX_PHOTOS} 张照片（同一张票的不同角度）")
    if doc_kind not in DOC_KINDS and doc_kind != "auto":
        raise TicketError(400, "单据类型应为 concrete_ticket / material_receipt / cert / auto")
    media, shas = [], []
    for name, raw, taken, geo in files:
        try:
            m = media_svc.save_photo(conn, user["project_id"], user["id"], name, raw, "ticket", taken_hint=taken, geo=geo)
        except media_svc.MediaError as exc:
            raise TicketError(400, f"{name}: {exc}") from None
        media.append(m)
        shas.append(m["sha256"])
    kind = doc_kind if doc_kind in DOC_KINDS else "concrete_ticket"
    tid = insert(conn, "tickets", {
        "project_id": user["project_id"], "doc_kind": kind, "status": "draft", "ai_status": "pending",
        "media_ids_json": dumps([m["id"] for m in media]), "sha_json": dumps(shas), "uploaded_by": user["id"],
        "kind_hint": doc_kind, "created_at": now_iso(), "updated_at": now_iso()})
    audit(conn, user["id"], "ticket.upload", f"ticket:{tid}", f"{len(media)}张")
    return one(conn, "SELECT * FROM tickets WHERE id=?", (tid,))


def run_ocr(ticket_id: int) -> None:
    """后台识别：自己开连接。失败只记录在票据上（可手工填写或重试），照片不受影响。"""
    conn = connect()
    try:
        t = one(conn, "SELECT * FROM tickets WHERE id=?", (ticket_id,))
        if not t or t["status"] != "draft":
            return
        conn.execute("UPDATE tickets SET ai_status='running', ai_error=NULL, updated_at=? WHERE id=?", (now_iso(), ticket_id))
        mids = loads(t["media_ids_json"], [])
        rows = all_(conn, f"SELECT * FROM media WHERE id IN ({_in(mids)})", mids)
        by_id = {m["id"]: m for m in rows}
        ordered = [by_id[i] for i in mids if i in by_id]
        paths = [str(settings.media_dir / m["ai_file"]) for m in ordered]
        try:
            res = TA.extract(paths, t["kind_hint"] or t["doc_kind"], project_id=t["project_id"], user_id=t["uploaded_by"],
                             seed=ordered[0]["sha256"] if ordered else "")
        except AIError as exc:
            conn.execute("UPDATE tickets SET ai_status='failed', ai_error=?, updated_at=? WHERE id=?", (str(exc)[:300], now_iso(), ticket_id))
            return
        except Exception as exc:  # noqa: BLE001 — 任何异常都不能让票据永远停在“识别中”
            log.exception("票据识别异常 %s", ticket_id)
            conn.execute("UPDATE tickets SET ai_status='failed', ai_error=?, updated_at=? WHERE id=?",
                         (f"识别出错：{type(exc).__name__}", now_iso(), ticket_id))
            return
        data = {**res.data, "provider": res.provider, "model": res.model}
        conn.execute("UPDATE tickets SET ai_status='done', ai_json=?, ai_run_id=?, provider=?, doc_kind=?, updated_at=? WHERE id=? AND status='draft'",
                     (dumps(data), res.run_id, res.provider, res.data["doc_kind"], now_iso(), ticket_id))
    finally:
        conn.close()


def reocr_allowed(t: dict) -> bool:
    if t["status"] != "draft":
        return False
    if t["ai_status"] in ("failed", "done"):
        return True
    # 卡住（服务重启打断了后台识别等）允许重试：running 超过 2 分钟；pending 超过 10 分钟（与页面“识别超时”同一时长，AC-1.11）
    stuck = {"running": 2, "pending": 10}.get(t["ai_status"])
    if stuck is not None:
        try:
            return datetime.fromisoformat(t["updated_at"]) < datetime.now().astimezone() - timedelta(minutes=stuck)
        except ValueError:
            return True
    return False


# ----------------------------------------------------------------------------- 票据：重复检测

def _norm_key(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "")).casefold()


def find_dups(conn, t: dict, fields: dict, scope: list[int] | None = None, confirmed_only: bool = False) -> list[dict]:
    """相同照片（sha256）或 相同票号+供应商+日期 的其它票据：已确认的，或更早拍的草稿（后拍的那张才算“疑似重复”）。
    confirmed_only=True 用于确认时拦截：只有已入账的票据才会挡住。"""
    shas = set(loads(t.get("sha_json"), []))
    key_no, key_sup, key_day = _norm_key(fields.get("ticket_no")), _norm_key(fields.get("supplier")), (fields.get("issued_at") or "")[:10]
    rows = all_(conn, "SELECT id, status, sha_json, ai_json, confirmed_json, uploaded_by, doc_kind FROM tickets WHERE project_id=? AND id<>? AND status<>'void' "
                      "ORDER BY id DESC LIMIT 3000", (t["project_id"], t["id"]))
    out = []
    for r in rows:
        if r["status"] != "confirmed" and (confirmed_only or r["id"] > t["id"]):
            continue
        reasons = []
        if shas & set(loads(r["sha_json"], [])):
            reasons.append("同一张照片")
        if key_no:
            rf = _fields_of(r)
            if _norm_key(rf.get("ticket_no")) == key_no and _norm_key(rf.get("supplier")) == key_sup and (rf.get("issued_at") or "")[:10] == key_day and key_day:
                reasons.append("票号、供应商、日期相同")
        if not reasons:
            continue
        hidden = scope is not None and r["uploaded_by"] not in scope
        out.append({"ticket_id": None if hidden else r["id"], "status": r["status"], "reason": "、".join(reasons),
                    "text": ("项目内已有" if hidden else f"与票据 #{r['id']}（{'已确认' if r['status'] == 'confirmed' else '草稿'}）") + "：" + "、".join(reasons)})
    return out


# ----------------------------------------------------------------------------- 票据：视图

def ticket_view(conn, t: dict, user: dict, scope: list[int] | None = None, names: dict | None = None) -> dict:
    fields = _fields_of(t)
    ai = loads(t["ai_json"], None) or {}
    mids = loads(t["media_ids_json"], [])
    media = [media_svc.public(m) for m in all_(conn, f"SELECT * FROM media WHERE id IN ({_in(mids)}) ORDER BY id", mids)] if mids else []
    names = names or _names(conn, [t["uploaded_by"], t["confirmed_by"]])
    pour = one(conn, "SELECT id, part, grade, pour_date FROM pours WHERE id=?", (t["pour_id"],)) if t["pour_id"] else None
    role = user["role"]
    own = t["uploaded_by"] == user["id"]
    can_confirm = t["status"] == "draft" and (role in ("eng_sup", "manager") or (role == "tech" and own))
    recheck = t["status"] == "confirmed" and is_quality(user)
    return {
        "id": t["id"], "doc_kind": t["doc_kind"], "kind_name": DOC_KINDS.get(t["doc_kind"], t["doc_kind"]), "status": t["status"],
        "status_name": {"draft": "待确认", "confirmed": "已确认", "void": "已作废"}[t["status"]],
        "ai_status": t["ai_status"], "ai_error": t["ai_error"], "provider": t["provider"], "mock": t["provider"] == "mock",
        "fields": fields, "ai_fields": ai.get("fields"), "doubts": ai.get("doubts") or {} if t["status"] == "draft" else {},
        "unclear": ai.get("unclear") or [], "edited": (loads(t["confirmed_json"], {}) or {}).get("edited", []),
        "category": t["category"] or (TA.infer_category(fields.get("material"), fields.get("spec")) if t["doc_kind"] != "concrete_ticket" else None),
        "dups": find_dups(conn, t, fields, scope) if t["status"] == "draft" else [],
        "media": media, "pour_id": t["pour_id"], "pour": ({**pour, "label": pour_label(pour)} if pour else None), "batch_id": t["batch_id"],
        "uploaded_by": t["uploaded_by"], "uploader": names.get(t["uploaded_by"], ""), "confirmed_by": t["confirmed_by"], "confirmer": names.get(t["confirmed_by"], ""),
        "created_at": t["created_at"], "confirmed_at": t["confirmed_at"], "void_reason": t["void_reason"],
        "can_confirm": can_confirm or recheck, "can_void": (t["status"] == "draft" and (own or is_quality(user))) or recheck,
        "recheck": recheck, "labels": TA.LABEL,
    }


def list_tickets(conn, user: dict, kind: str = "", status: str = "", pour_id: int | None = None, limit: int = 100) -> dict:
    sc = scope_of(conn, user)
    sql, args = "SELECT * FROM tickets WHERE project_id=?", [user["project_id"]]
    if sc is not None:
        sql += f" AND uploaded_by IN ({_in(sc)})"
        args += sc
    if kind in DOC_KINDS:
        sql += " AND doc_kind=?"
        args.append(kind)
    if status in ("draft", "confirmed", "void"):
        sql += " AND status=?"
        args.append(status)
    if pour_id:
        sql += " AND pour_id=?"
        args.append(pour_id)
    rows = all_(conn, sql + " ORDER BY id DESC LIMIT ?", args + [max(1, min(limit, 300))])
    names = _names(conn, [r["uploaded_by"] for r in rows] + [r["confirmed_by"] for r in rows])
    counts = {r["status"]: r["n"] for r in all_(conn, "SELECT status, COUNT(*) n FROM tickets WHERE project_id=?" + (f" AND uploaded_by IN ({_in(sc)})" if sc is not None else "")
                                                 + " GROUP BY status", [user["project_id"]] + (sc or []))}
    return {"items": [ticket_view(conn, r, user, sc, names) for r in rows], "counts": counts, "kinds": DOC_KINDS}


def get_ticket(conn, user: dict, tid: int) -> dict:
    return ticket_view(conn, _row(conn, tid, user), user, scope_of(conn, user))


# ----------------------------------------------------------------------------- 票据：确认 / 作废

def confirm(conn, user: dict, tid: int, payload: dict) -> dict:
    t = _row(conn, tid, user)
    role = user["role"]
    if role not in WRITE_ROLES:
        raise TicketError(403, "当前岗位只能查看票据")
    if t["status"] == "void":
        raise TicketError(409, "票据已作废")
    if t["status"] == "confirmed" and not is_quality(user):
        raise TicketError(409, "票据已确认；如需修改请联系工程主管")
    if role == "tech" and t["uploaded_by"] != user["id"]:
        raise TicketError(403, "技术员只能确认本人上传的票据")
    kind = payload.get("doc_kind") or t["doc_kind"]
    if kind not in DOC_KINDS:
        raise TicketError(400, "单据类型不正确")
    raw = {**_fields_of(t), **(payload.get("fields") or {})}
    try:
        fields, _ = TA.normalize_fields(raw, kind, strict=True)
    except ValueError as exc:
        raise TicketError(400, str(exc)) from None
    when = fields.get("issued_at") or ""
    if kind == "concrete_ticket":
        if not fields["qty"]:
            raise TicketError(400, "请填写本车方量（m³）")
        if len(when) < 10:
            raise TicketError(400, "请填写发货日期")
        if fields["grade"] is None:
            raise TicketError(400, "请填写强度等级（如 C30）")
        fields["unit"] = "m³"
    elif kind == "material_receipt":
        if not fields["material"]:
            raise TicketError(400, "请填写材料名称")
        if not fields["qty"] or not fields["unit"]:
            raise TicketError(400, "请填写数量和单位")
        if len(when) < 10:
            raise TicketError(400, "请填写到场日期")
    elif not any(fields.get(k) for k in ("material", "batch_no", "ticket_no")):
        raise TicketError(400, "合格证请至少填写材料名称、批号或编号之一")
    category = None
    if kind != "concrete_ticket":
        category = payload.get("category") or TA.infer_category(fields["material"], fields["spec"])
        if category not in CATEGORIES:
            raise TicketError(400, "材料类别不正确")
    sc = scope_of(conn, user)
    dups = find_dups(conn, t, fields, sc, confirmed_only=True)
    if dups and not payload.get("force_dup"):
        raise TicketError(409, "疑似重复票据：" + "；".join(d["text"] for d in dups[:3]) + "。确认不是同一张再选“仍要入账”")
    warnings: list[str] = []
    ai = loads(t["ai_json"], None) or {}
    ai_f = ai.get("fields") or {}
    edited = [k for k in TA.FIELDS if ai_f and ai_f.get(k) != fields.get(k)]
    # 逐字段核对记录（AI 原值｜人工最终值｜决定）：决定由 AI 原值与最终值推导，服务端为准。
    # 客户端（ReviewTable）给了 decisions 时，AI 识别出值的每个字段都必须有决定，否则不予入账并提示剩余数
    hints: dict[str, str] = {}
    if payload.get("decisions") is not None:
        decs = payload["decisions"]
        if not isinstance(decs, list):
            raise TicketError(400, "决定格式不正确")
        hints = {str(d.get("key")): d.get("decision") for d in decs if isinstance(d, dict)}
        need_keys = [k for k in KIND_FIELDS[kind] if ai_f.get(k) is not None]
        try:
            review_svc.check_decisions(need_keys, [d for d in decs if isinstance(d, dict) and str(d.get("key")) in need_keys], "adopt",
                                       {k: TA.LABEL.get(k, k) for k in need_keys})
        except review_svc.ReviewError as exc:
            raise TicketError(400, str(exc)) from None
    review_rows = []
    for k in KIND_FIELDS[kind]:
        dec = review_svc.derive_ticket_decision(ai_f.get(k), fields.get(k), hints.get(k))
        if dec:
            review_rows.append({"key": k, "label": TA.LABEL.get(k, k), "ai": ai_f.get(k), "final": fields.get(k), "decision": dec, "reason": ""})
    old_batch = t["batch_id"]
    with tx(conn):
        cur = one(conn, "SELECT * FROM tickets WHERE id=?", (tid,))
        if cur["status"] == "void" or (cur["status"] == "confirmed" and t["status"] == "draft"):
            raise TicketError(409, "票据状态已变化，请刷新")
        pour_id = cur["pour_id"]
        if kind != "concrete_ticket":
            pour_id = None
        elif "pour_id" in payload or payload.get("new_pour"):
            pour_id = _pick_pour(conn, user, payload, fields, warnings)
        conn.execute(
            "UPDATE tickets SET doc_kind=?, status='confirmed', confirmed_json=?, ticket_no=?, supplier=?, vehicle=?, issued_at=?, ticket_date=?, part=?, material=?, "
            "category=?, grade=?, spec=?, qty=?, unit=?, batch_no=?, slump=?, temp=?, receiver=?, remark=?, pour_id=?, confirmed_by=?, confirmed_at=?, updated_at=? WHERE id=?",
            (kind, dumps({"fields": fields, "edited": edited, "category": category, "force_dup": bool(dups)}), fields["ticket_no"], fields["supplier"], fields["vehicle"],
             fields["issued_at"], when[:10] or None, fields["part"], fields["material"], category, fields["grade"], fields["spec"], fields["qty"], fields["unit"],
             fields["batch_no"], fields["slump"], fields["temp"], fields["receiver"], fields["remark"], pour_id, user["id"], now_iso(), now_iso(), tid))
        t2 = one(conn, "SELECT * FROM tickets WHERE id=?", (tid,))
        batch_id = None
        if kind == "material_receipt":
            batch_id = _attach_batch(conn, user, t2)
        elif kind == "cert":
            batch_id = _attach_cert(conn, t2)
        conn.execute("UPDATE tickets SET batch_id=? WHERE id=?", (batch_id, tid))
        review_svc.save_rows(conn, t["project_id"], "ticket", "ticket", tid, review_rows, user["id"])
        for b in {old_batch, batch_id} - {None}:
            recalc_batch(conn, b)
        audit(conn, user["id"], "ticket.confirm", f"ticket:{tid}",
              f"{DOC_KINDS[kind]} 票号{fields['ticket_no'] or '-'} {fields['qty'] if fields['qty'] is not None else ''}{fields['unit'] or ''}"
              f"{' 改动:' + ','.join(edited) if edited else ''}{' 重复仍入账' if dups else ''}"[:200])
        event(conn, "ticket.confirm", project_id=t["project_id"], user_id=user["id"], ref_id=tid, value=fields["qty"], data={"kind": kind})
    out = get_ticket(conn, user, tid)
    out["warnings"] = warnings
    return out


def _pick_pour(conn, user: dict, payload: dict, fields: dict, warnings: list[str]) -> int | None:
    new = payload.get("new_pour")
    if new:
        body = {"part": new.get("part") or fields.get("part"), "grade": new.get("grade") or fields.get("grade"),
                "pour_date": new.get("pour_date") or (fields.get("issued_at") or "")[:10], "design_qty": new.get("design_qty"),
                "activity_code": new.get("activity_code") or ""}
        return int(create_pour(conn, user, body, _inner=True)["id"])
    pid = payload.get("pour_id")
    if pid is None:
        return None
    p = _pour_row(conn, user, int(pid))
    if p["grade"] and fields.get("grade") and p["grade"] != fields["grade"]:
        warnings.append(f"小票强度等级 {fields['grade']} 与浇筑单元的 {p['grade']} 不一致，请确认归属")
    if p["status"] == "done":
        warnings.append("该浇筑单元已标记完成，请确认这张小票属于它")
    return p["id"]


def assign_pour(conn, user: dict, tid: int, payload: dict) -> dict:
    t = _row(conn, tid, user)
    if user["role"] not in WRITE_ROLES:
        raise TicketError(403, "当前岗位只能查看")
    if t["doc_kind"] != "concrete_ticket" or t["status"] != "confirmed":
        raise TicketError(409, "只有已确认的混凝土小票可以归入浇筑单元")
    if user["role"] == "tech" and t["uploaded_by"] != user["id"]:
        raise TicketError(403, "技术员只能调整本人上传的票据")
    fields = _fields_of(t)
    warnings: list[str] = []
    with tx(conn):
        pid = _pick_pour(conn, user, {"pour_id": payload.get("pour_id"), "new_pour": payload.get("new_pour")}, fields, warnings)
        conn.execute("UPDATE tickets SET pour_id=?, updated_at=? WHERE id=?", (pid, now_iso(), tid))
        audit(conn, user["id"], "ticket.assign", f"ticket:{tid}", f"pour:{pid}")
    out = get_ticket(conn, user, tid)
    out["warnings"] = warnings
    return out


def void(conn, user: dict, tid: int, reason: str = "") -> dict:
    t = _row(conn, tid, user)
    if user["role"] not in WRITE_ROLES:
        raise TicketError(403, "当前岗位只能查看票据")
    if t["status"] == "void":
        raise TicketError(409, "票据已作废")
    if t["status"] == "confirmed" and not is_quality(user):
        raise TicketError(403, "已确认的票据需工程主管/项目经理作废")
    if t["status"] == "draft" and t["uploaded_by"] != user["id"] and not is_quality(user):
        raise TicketError(403, "只能作废本人上传的票据")
    with tx(conn):
        conn.execute("UPDATE tickets SET status='void', void_by=?, void_at=?, void_reason=?, updated_at=? WHERE id=?",
                     (user["id"], now_iso(), (reason or "")[:100], now_iso(), tid))
        if t["batch_id"]:
            recalc_batch(conn, t["batch_id"])
        audit(conn, user["id"], "ticket.void", f"ticket:{tid}", (reason or "")[:100])
    return get_ticket(conn, user, tid)


# ----------------------------------------------------------------------------- 浇筑单元

def pour_label(p: dict) -> str:
    return " ".join(x for x in (p.get("part"), p.get("grade"), (p.get("pour_date") or "")[5:]) if x)


def _pour_row(conn, user: dict, pid: int) -> dict:
    p = one(conn, "SELECT * FROM pours WHERE id=? AND project_id=?", (pid, user["project_id"]))
    if not p:
        raise TicketError(404, "浇筑单元不存在")
    sc = scope_of(conn, user)
    if sc is not None and not _pour_visible(conn, p, sc):
        raise TicketError(404, "浇筑单元不存在")
    return p


def _pour_visible(conn, p: dict, sc: list[int]) -> bool:
    if p["created_by"] in sc:
        return True
    return bool(one(conn, f"SELECT 1 x FROM tickets WHERE pour_id=? AND uploaded_by IN ({_in(sc)}) LIMIT 1", [p["id"]] + sc))


def _check_activity(conn, project_id: int, code: str) -> str:
    code = (code or "").strip()[:40]
    if not code:
        return ""
    from . import p6 as p6_svc
    imp = p6_svc.current_import(conn, project_id)
    if imp and not one(conn, "SELECT 1 x FROM activities WHERE import_id=? AND code=?", (imp["id"], code)):
        raise TicketError(400, f"当前 P6 计划里没有作业 {code}")
    return code


def _design_qty(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TicketError(400, "理论方量应为数字") from None
    if not 0 < x <= 100000:
        raise TicketError(400, "理论方量应大于 0 且不超过 100000 m³")
    return round(x, 3)


def create_pour(conn, user: dict, body: dict, _inner: bool = False) -> dict:
    if user["role"] not in WRITE_ROLES:
        raise TicketError(403, "当前岗位只能查看")
    part = (body.get("part") or "").strip()[:60]
    if not part:
        raise TicketError(400, "请填写浇筑部位")
    grade = ""
    if body.get("grade"):
        grade = TA.norm_grade(body["grade"]) or ""
        if not grade:
            raise TicketError(400, "强度等级格式不正确，如 C30、C35P8")
    day = _iso(body["pour_date"], "浇筑", future_ok=True) if body.get("pour_date") else None
    status = body.get("status") or "planned"
    if status not in POUR_STATUS:
        raise TicketError(400, "状态应为 planned/pouring/done")
    code = _check_activity(conn, user["project_id"], body.get("activity_code") or "")
    dup = one(conn, "SELECT id FROM pours WHERE project_id=? AND part=? AND grade=? AND IFNULL(pour_date,'')=?", (user["project_id"], part, grade, day or ""))
    if dup:
        if _inner:  # 确认票据时“新建”撞上同名单元：直接复用，避免重复单元
            return one(conn, "SELECT * FROM pours WHERE id=?", (dup["id"],))
        raise TicketError(409, f"已有相同部位、等级、日期的浇筑单元 #{dup['id']}")
    pid = insert(conn, "pours", {"project_id": user["project_id"], "part": part, "activity_code": code, "grade": grade,
                                 "design_qty": _design_qty(body.get("design_qty")), "pour_date": day, "status": status,
                                 "note": (body.get("note") or "")[:200], "created_by": user["id"], "created_at": now_iso(), "updated_at": now_iso()})
    audit(conn, user["id"], "pour.create", f"pour:{pid}", f"{part} {grade} {day or ''}"[:120])
    return one(conn, "SELECT * FROM pours WHERE id=?", (pid,))


def update_pour(conn, user: dict, pid: int, body: dict) -> dict:
    p = _pour_row(conn, user, pid)
    if user["role"] not in WRITE_ROLES:
        raise TicketError(403, "当前岗位只能查看")
    if user["role"] == "tech" and p["created_by"] != user["id"]:
        raise TicketError(403, "技术员只能修改本人建立的浇筑单元")
    sets: dict[str, Any] = {}
    if "part" in body:
        part = (body["part"] or "").strip()[:60]
        if not part:
            raise TicketError(400, "浇筑部位不能为空")
        sets["part"] = part
    if "grade" in body:
        g = TA.norm_grade(body["grade"]) if body["grade"] else ""
        if body["grade"] and not g:
            raise TicketError(400, "强度等级格式不正确，如 C30、C35P8")
        sets["grade"] = g or ""
    if "design_qty" in body:
        sets["design_qty"] = _design_qty(body["design_qty"])
    if "pour_date" in body:
        sets["pour_date"] = _iso(body["pour_date"], "浇筑", future_ok=True) if body["pour_date"] else None
    if "status" in body:
        if body["status"] not in POUR_STATUS:
            raise TicketError(400, "状态应为 planned/pouring/done")
        sets["status"] = body["status"]
    if "activity_code" in body:
        sets["activity_code"] = _check_activity(conn, user["project_id"], body["activity_code"] or "")
    if "note" in body:
        sets["note"] = (body["note"] or "")[:200]
    if not sets:
        return pour_view(conn, p, scope_of(conn, user), rules(conn, user["project_id"]), True)
    sets["updated_at"] = now_iso()
    with tx(conn):
        conn.execute(f"UPDATE pours SET {', '.join(k + '=?' for k in sets)} WHERE id=?", list(sets.values()) + [pid])
        audit(conn, user["id"], "pour.update", f"pour:{pid}", dumps({k: v for k, v in sets.items() if k != "updated_at"})[:200])
    return pour_view(conn, one(conn, "SELECT * FROM pours WHERE id=?", (pid,)), scope_of(conn, user), rules(conn, user["project_id"]), True)


def delete_pour(conn, user: dict, pid: int) -> dict:
    p = _pour_row(conn, user, pid)
    if not is_quality(user):
        raise TicketError(403, "仅工程主管/项目经理可删除浇筑单元")
    nt = one(conn, "SELECT COUNT(*) n FROM tickets WHERE pour_id=? AND status<>'void'", (pid,))["n"]
    ns = one(conn, "SELECT COUNT(*) n FROM specimens WHERE pour_id=?", (pid,))["n"]
    if nt or ns:
        raise TicketError(409, f"已关联 {nt} 张小票、{ns} 组试块，不能删除；浇完请标记为“已完成”")
    with tx(conn):
        conn.execute("DELETE FROM pours WHERE id=?", (pid,))
        audit(conn, user["id"], "pour.delete", f"pour:{pid}", pour_label(p))
    return {"ok": True}


def specimen_need(qty: float, days: int, conc: dict) -> dict:
    per, per_day = float(conc["specimen_per_m3"]), int(conc["specimens_per_day"])
    by_vol = math.ceil(round(qty / per, 6)) if qty > 0 else 0
    by_day = days * per_day
    return {"by_volume": by_vol, "by_days": by_day, "required": max(by_vol, by_day),
            "basis": f"已确认 {qty:g} m³，按每 {per:g} m³ 不少于 1 组应留 {by_vol} 组；{days} 个浇筑日按每日不少于 {per_day} 组应留 {by_day} 组；取较大值。"
                     "按通用常规要求测算，以项目试验计划和监理要求为准。"}


def _spec_state(sp: dict, today: str, warn_days: int) -> dict:
    """某组试块 7/28 天龄期的到期状态。"""
    out = {}
    for age, due, mpa in ((7, sp["due_7"], sp["r7_mpa"]), (28, sp["due_28"], sp["r28_mpa"])):
        left = (date.fromisoformat(due) - date.fromisoformat(today)).days
        if mpa is not None:
            st = "done"
        elif left < 0:
            st = "overdue"
        elif left <= warn_days:
            st = "soon"
        else:
            st = "ok"
        out[age] = {"due": due, "days_left": left, "state": st}
    return out


def specimen_view(sp: dict, pour: dict | None, today: str, warn_days: int) -> dict:
    st = _spec_state(sp, today, warn_days)
    design = TA.grade_strength(sp.get("grade") or (pour or {}).get("grade"))
    ref28 = None
    if sp["r28_mpa"] is not None:
        if design is None:
            ref28 = "未设置设计强度等级，无法对照（强度评定以试验室/监理为准）"
        elif sp["r28_mpa"] >= design:
            ref28 = f"报告值 {sp['r28_mpa']:g} MPa 不低于设计等级值 {design} MPa（仅参考，强度评定以试验室/监理为准）"
        else:
            ref28 = f"报告值 {sp['r28_mpa']:g} MPa 低于设计等级值 {design} MPa（仅参考，请向试验室/监理核实，强度评定以其为准）"
    return {"id": sp["id"], "pour_id": sp["pour_id"], "group_no": sp["group_no"], "grade": sp["grade"] or (pour or {}).get("grade") or "", "curing": sp["curing"],
            "molded_date": sp["molded_date"], "note": sp["note"], "age7": st[7], "age28": st[28],
            "r7": {"mpa": sp["r7_mpa"], "report_no": sp["r7_report_no"], "tested_at": sp["r7_tested_at"]} if sp["r7_mpa"] is not None else None,
            "r28": {"mpa": sp["r28_mpa"], "report_no": sp["r28_report_no"], "tested_at": sp["r28_tested_at"], "ref": ref28} if sp["r28_mpa"] is not None else None,
            "part": (pour or {}).get("part")}


def pour_view(conn, p: dict, scope: list[int] | None, conc_rules: dict, detail: bool = False) -> dict:
    conc = conc_rules["concrete_rules"]
    today = _today()
    sql, args = "SELECT * FROM tickets WHERE pour_id=? AND status='confirmed' AND doc_kind='concrete_ticket'", [p["id"]]
    if scope is not None:
        sql += f" AND uploaded_by IN ({_in(scope)})"
        args += scope
    ts = all_(conn, sql + " ORDER BY issued_at, id", args)
    partial = scope is not None and one(conn, "SELECT 1 x FROM tickets WHERE pour_id=? AND status='confirmed' AND doc_kind='concrete_ticket'"
                                                f" AND uploaded_by NOT IN ({_in(scope)}) LIMIT 1", [p["id"]] + scope) is not None
    actual = round(sum(t["qty"] or 0 for t in ts), 3)
    days = sorted({t["ticket_date"] for t in ts if t["ticket_date"]})
    sps = all_(conn, "SELECT * FROM specimens WHERE pour_id=? ORDER BY CAST(group_no AS INTEGER), group_no, id", (p["id"],))
    if scope is not None:
        sps = [s for s in sps if s["created_by"] in scope]
    design, thr = p["design_qty"], float(conc["deviation_pct"])
    dev, dev_flag, dev_text = None, None, None
    if design is None:
        dev_text = "理论方量待补"
    elif actual > 0 and not partial:
        dev = round((actual - design) / design * 100, 1)
        if dev > thr:
            dev_flag = "over"
            dev_text = (f"已确认累计 {actual:g} m³，比理论方量 {design:g} m³ 多 {abs(dev):g}%，超过 ±{thr:g}% 阈值。疑点，请核实：是否超灌、损耗偏大，或小票重复/录错。")
        elif dev < -thr and p["status"] == "done":
            dev_flag = "under"
            dev_text = (f"已确认累计 {actual:g} m³，比理论方量 {design:g} m³ 少 {abs(dev):g}%，超过 ±{thr:g}% 阈值。疑点，请核实：是否漏记小票、少浇或理论方量有误。")
        else:
            dev_text = f"偏差 {dev:+g}%（阈值 ±{thr:g}%）" + ("，浇筑尚未完成，少浇暂不提示" if dev < -thr else "")
    need = specimen_need(actual, len(days), conc)
    registered = len(sps)
    missing = 0 if (partial or actual <= 0) else max(0, need["required"] - registered)
    due_pending = [s["due_28"] for s in sps if s["r28_mpa"] is None]
    spec_views = [specimen_view(s, p, today, int(conc["warn_days"])) for s in sps]
    out = {
        "id": p["id"], "part": p["part"], "activity_code": p["activity_code"], "grade": p["grade"], "pour_date": p["pour_date"], "status": p["status"],
        "status_name": POUR_STATUS[p["status"]], "note": p["note"], "design_qty": design, "design_qty_text": "理论方量待补" if design is None else f"{design:g} m³",
        "actual_qty": actual, "trucks": len(ts), "days": len(days), "partial": partial,
        "deviation_pct": dev, "deviation_flag": dev_flag, "deviation_text": dev_text, "threshold_pct": thr,
        "slump_missing": sum(1 for t in ts if t["slump"] is None), "temp_missing": sum(1 for t in ts if t["temp"] is None),
        "specimens": {"required": need["required"] if not partial else None, "registered": registered, "missing": missing, "basis": need["basis"] if not partial else
                      "你只能看到本人/本单位上传的小票，完整测算由港湾岗位查看", "next_due_28": min(due_pending) if due_pending else None,
                      "alerts": sum(1 for s in spec_views if s["age28"]["state"] in ("overdue", "soon"))},
        "label": pour_label(p), "grade_mismatch": sum(1 for t in ts if p["grade"] and t["grade"] and t["grade"] != p["grade"]),
        "created_by": p["created_by"],
    }
    if detail:
        out["records"] = [{"ticket_id": t["id"], "time": (t["issued_at"] or "")[:16], "vehicle": t["vehicle"], "supplier": t["supplier"], "ticket_no": t["ticket_no"],
                           "qty": t["qty"], "grade": t["grade"], "slump": t["slump"], "temp": t["temp"]} for t in ts]
        out["specimen_list"] = spec_views
    else:
        out["specimen_list"] = spec_views
    return out


def list_pours(conn, user: dict, status: str = "") -> dict:
    sc = scope_of(conn, user)
    rl = rules(conn, user["project_id"])
    sql, args = "SELECT * FROM pours WHERE project_id=?", [user["project_id"]]
    if status in POUR_STATUS:
        sql += " AND status=?"
        args.append(status)
    rows = all_(conn, sql + " ORDER BY COALESCE(pour_date,'9999') DESC, id DESC LIMIT 300", args)
    if sc is not None:
        rows = [p for p in rows if _pour_visible(conn, p, sc)]
    return {"items": [pour_view(conn, p, sc, rl) for p in rows], "note": NOTE_CONCRETE, "rules": rl["concrete_rules"], "can_write": user["role"] in WRITE_ROLES,
            "can_quality": is_quality(user)}


def get_pour(conn, user: dict, pid: int) -> dict:
    p = _pour_row(conn, user, pid)
    return pour_view(conn, p, scope_of(conn, user), rules(conn, user["project_id"]), True)


def _simple(s: str | None) -> str:
    return re.sub(r"[\s#号]+", "", TA._nk(s or "")).upper()


def candidates(conn, user: dict, ticket_id: int | None = None, part: str = "", grade: str = "", day: str = "") -> dict:
    """给混凝土小票推荐候选浇筑单元：只推荐，不自动归入。"""
    if ticket_id:
        f = _fields_of(_row(conn, ticket_id, user))
        part, grade, day = part or f.get("part") or "", grade or f.get("grade") or "", day or (f.get("issued_at") or "")[:10]
    grade = TA.norm_grade(grade) or ""
    sc = scope_of(conn, user)
    rows = all_(conn, "SELECT * FROM pours WHERE project_id=? ORDER BY id DESC LIMIT 300", (user["project_id"],))
    if sc is not None:
        rows = [p for p in rows if _pour_visible(conn, p, sc)]
    sp = _simple(part)
    out = []
    for p in rows:
        score, why = 0, []
        pp = _simple(p["part"])
        if sp and pp:
            if sp == pp:
                score += 4
                why.append("部位相同")
            elif len(sp) >= 2 and (sp in pp or pp in sp):
                score += 2
                why.append("部位相近")
        if grade and p["grade"]:
            if grade == p["grade"]:
                score += 2
                why.append("强度等级相同")
        if day and p["pour_date"]:
            gap = abs((date.fromisoformat(day) - date.fromisoformat(p["pour_date"])).days)
            if gap == 0:
                score += 3
                why.append("浇筑日期相同")
            elif gap <= 1:
                score += 1
                why.append("浇筑日期相差 1 天")
        if p["status"] == "done":
            score -= 1
        if score > 0 and why:
            out.append((score, p, why))
    out.sort(key=lambda x: (-x[0], -x[1]["id"]))
    return {"items": [{"id": p["id"], "label": pour_label(p), "part": p["part"], "grade": p["grade"], "pour_date": p["pour_date"], "status": p["status"],
                       "status_name": POUR_STATUS[p["status"]], "reasons": why, "design_qty_text": "理论方量待补" if p["design_qty"] is None else f"{p['design_qty']:g} m³"}
                      for _, p, why in out[:6]],
            "suggest_new": {"part": part, "grade": grade, "pour_date": day or None},
            "note": "系统只做推荐，请你点选归属的浇筑单元，或新建一个；不会自动入账。"}


# ----------------------------------------------------------------------------- 试块

def add_specimen(conn, user: dict, pid: int, body: dict) -> dict:
    p = _pour_row(conn, user, pid)
    if user["role"] not in WRITE_ROLES:
        raise TicketError(403, "当前岗位只能查看")
    molded = _iso(body.get("molded_date"), "成型")
    if date.fromisoformat(molded) > date.today():
        raise TicketError(400, "成型日期不能在未来")
    curing = body.get("curing") or "标准养护"
    if curing not in CURING:
        raise TicketError(400, "养护方式应为：" + "、".join(CURING))
    n = one(conn, "SELECT COUNT(*) n FROM specimens WHERE pour_id=?", (pid,))["n"]
    group_no = (str(body.get("group_no") or "").strip() or str(n + 1))[:20]
    if one(conn, "SELECT 1 x FROM specimens WHERE pour_id=? AND group_no=?", (pid, group_no)):
        raise TicketError(409, f"第 {group_no} 组已登记")
    d = date.fromisoformat(molded)
    sid = insert(conn, "specimens", {
        "project_id": user["project_id"], "pour_id": pid, "group_no": group_no, "grade": p["grade"], "curing": curing, "molded_date": molded,
        "due_7": (d + timedelta(days=7)).isoformat(), "due_28": (d + timedelta(days=28)).isoformat(), "note": (body.get("note") or "")[:100],
        "created_by": user["id"], "created_at": now_iso()})
    audit(conn, user["id"], "specimen.add", f"specimen:{sid}", f"{pour_label(p)} 第{group_no}组 {molded}"[:150])
    return _spec_one(conn, user, sid)


def _spec_row(conn, user: dict, sid: int) -> dict:
    s = one(conn, "SELECT * FROM specimens WHERE id=? AND project_id=?", (sid, user["project_id"]))
    if not s:
        raise TicketError(404, "试块不存在")
    sc = scope_of(conn, user)
    if sc is not None and s["created_by"] not in sc:
        raise TicketError(404, "试块不存在")
    return s


def _spec_one(conn, user: dict, sid: int) -> dict:
    s = _spec_row(conn, user, sid)
    p = one(conn, "SELECT * FROM pours WHERE id=?", (s["pour_id"],))
    return specimen_view(s, p, _today(), int(rules(conn, user["project_id"])["concrete_rules"]["warn_days"]))


def record_result(conn, user: dict, sid: int, body: dict) -> dict:
    if not is_quality(user):
        raise TicketError(403, "录入试验结果需工程主管/项目经理")
    s = _spec_row(conn, user, sid)
    age = int(body.get("age") or 0)
    if age not in (7, 28):
        raise TicketError(400, "龄期应为 7 或 28")
    try:
        mpa = float(body.get("mpa"))
    except (TypeError, ValueError):
        raise TicketError(400, "请填写试验室报告的强度值（MPa）") from None
    if not 0 < mpa <= 200:
        raise TicketError(400, "强度值应大于 0 且不超过 200 MPa")
    tested = _iso(body["tested_at"], "试验") if body.get("tested_at") else None
    if tested and tested < s["molded_date"]:
        raise TicketError(400, "试验日期不能早于成型日期")
    report = (body.get("report_no") or "").strip()[:60] or None
    with tx(conn):
        conn.execute(f"UPDATE specimens SET r{age}_mpa=?, r{age}_report_no=?, r{age}_tested_at=?, r{age}_by=?, r{age}_at=? WHERE id=?",
                     (round(mpa, 2), report, tested, user["id"], now_iso(), sid))
        audit(conn, user["id"], "specimen.result", f"specimen:{sid}", f"{age}天 {mpa:g}MPa 报告{report or '-'}"[:150])
    return _spec_one(conn, user, sid)


def delete_specimen(conn, user: dict, sid: int) -> dict:
    s = _spec_row(conn, user, sid)
    if not (is_quality(user) or (user["role"] == "tech" and s["created_by"] == user["id"])):
        raise TicketError(403, "无权删除")
    if s["r7_mpa"] is not None or s["r28_mpa"] is not None:
        raise TicketError(409, "已录入试验结果的试块不能删除")
    with tx(conn):
        conn.execute("DELETE FROM specimens WHERE id=?", (sid,))
        audit(conn, user["id"], "specimen.delete", f"specimen:{sid}", f"第{s['group_no']}组")
    return {"ok": True}


def due_list(conn, user: dict, within: int = 14) -> dict:
    sc = scope_of(conn, user)
    conc = rules(conn, user["project_id"])["concrete_rules"]
    warn = int(conc["warn_days"])
    rows = all_(conn, "SELECT s.*, p.part pour_part, p.grade pour_grade FROM specimens s JOIN pours p ON p.id=s.pour_id WHERE s.project_id=?", (user["project_id"],))
    if sc is not None:
        rows = [r for r in rows if r["created_by"] in sc]
    today, items = _today(), []
    for r in rows:
        st = _spec_state(r, today, warn)
        for age in (28, 7):
            if st[age]["state"] != "done" and st[age]["days_left"] <= within:
                items.append({"specimen_id": r["id"], "pour_id": r["pour_id"], "part": r["pour_part"], "grade": r["grade"] or r["pour_grade"], "group_no": r["group_no"],
                              "age": age, "due": st[age]["due"], "days_left": st[age]["days_left"], "state": st[age]["state"], "molded_date": r["molded_date"]})
    items.sort(key=lambda x: (x["due"], x["age"] != 28))
    return {"items": items, "warn_days": warn, "note": NOTE_CONCRETE}


# ----------------------------------------------------------------------------- 材料批次与复试

def _attach_batch(conn, user: dict, t: dict) -> int:
    cat = t["category"] or "other"
    spec, bno, unit = (t["spec"] or ""), (t["batch_no"] or ""), (t["unit"] or "")
    sql = "SELECT id FROM material_batches WHERE project_id=? AND category=? AND spec=? AND batch_no=? AND unit=?"
    args: list = [t["project_id"], cat, spec, bno, unit]
    if not bno:  # 没有批号的进场单不能凭规格合并，按供应商+到场日区分
        sql += " AND supplier=? AND arrived_at=?"
        args += [t["supplier"] or "", t["ticket_date"]]
    hit = one(conn, sql, args)
    if hit:
        return hit["id"]
    required = rules(conn, t["project_id"])["material_rules"][cat].get("required")
    return insert(conn, "material_batches", {
        "project_id": t["project_id"], "category": cat, "name": t["material"] or CATEGORIES[cat], "spec": spec, "supplier": t["supplier"] or "", "batch_no": bno,
        "qty": 0, "unit": unit, "arrived_at": t["ticket_date"], "retest_status": "pending" if required else "not_required",
        "created_by": user["id"], "created_at": now_iso(), "updated_at": now_iso()})


def _attach_cert(conn, t: dict) -> int | None:
    """合格证只挂到已存在的同批号批次上（不新建批次、不计数量）。"""
    if not t["batch_no"]:
        return None
    hit = one(conn, "SELECT id FROM material_batches WHERE project_id=? AND batch_no=? ORDER BY id DESC", (t["project_id"], t["batch_no"]))
    return hit["id"] if hit else None


def recalc_batch(conn, bid: int) -> None:
    b = one(conn, "SELECT * FROM material_batches WHERE id=?", (bid,))
    if not b:
        return
    ts = all_(conn, "SELECT * FROM tickets WHERE batch_id=? AND status='confirmed' AND doc_kind='material_receipt' ORDER BY ticket_date, id", (bid,))
    if not ts:
        if b["retest_status"] in ("pending", "not_required") and not b["report_no"] and not one(
                conn, "SELECT 1 x FROM tickets WHERE batch_id=? AND status='confirmed'", (bid,)):
            conn.execute("DELETE FROM material_batches WHERE id=?", (bid,))
        else:
            conn.execute("UPDATE material_batches SET qty=0, updated_at=? WHERE id=?", (now_iso(), bid))
        return
    qty = round(sum(t["qty"] or 0 for t in ts), 3)
    sup = next((t["supplier"] for t in reversed(ts) if t["supplier"]), b["supplier"])
    conn.execute("UPDATE material_batches SET qty=?, arrived_at=?, supplier=?, updated_at=? WHERE id=?", (qty, ts[0]["ticket_date"], sup or "", now_iso(), bid))


def batch_view(conn, b: dict, mat_rules: dict, with_tickets: bool = True) -> dict:
    rule = mat_rules.get(b["category"]) or {}
    hint = None
    if rule.get("required"):
        if b["unit"] == "吨":
            lim = rule.get("batch_t") or (rule.get("bulk_t") if "散" in (b["spec"] or "") + (b["name"] or "") else rule.get("bag_t"))
            if lim:
                hint = f"累计 {b['qty']:g} 吨，按每批不超过 {lim:g} 吨取样，约需 {max(1, math.ceil(round(b['qty'] / lim, 6)))} 个取样批（以项目检验试验计划和监理要求为准）"
        else:
            hint = "数量单位不是吨，请自行按检验试验计划确定取样批数"
    out = {k: b[k] for k in ("id", "category", "name", "spec", "supplier", "batch_no", "qty", "unit", "arrived_at", "retest_status", "report_no", "reported_at")}
    out.update({"category_name": CATEGORIES.get(b["category"], b["category"]), "retest_name": RETEST[b["retest_status"]], "retest_note": b["retest_note"],
                "sampled_at": b["sampled_at"], "sample_hint": hint, "required": bool(rule.get("required"))})
    if with_tickets:
        out["tickets"] = [{"id": t["id"], "kind": t["doc_kind"], "kind_name": DOC_KINDS[t["doc_kind"]], "qty": t["qty"], "unit": t["unit"], "date": t["ticket_date"],
                           "ticket_no": t["ticket_no"]} for t in all_(conn, "SELECT * FROM tickets WHERE batch_id=? AND status='confirmed' ORDER BY ticket_date, id", (b["id"],))]
    return out


def list_batches(conn, user: dict, category: str = "", retest: str = "") -> dict:
    sc = scope_of(conn, user)
    sql, args = "SELECT * FROM material_batches WHERE project_id=?", [user["project_id"]]
    if category in CATEGORIES:
        sql += " AND category=?"
        args.append(category)
    if retest in RETEST:
        sql += " AND retest_status=?"
        args.append(retest)
    if sc is not None:
        sql += f" AND id IN (SELECT batch_id FROM tickets WHERE batch_id IS NOT NULL AND uploaded_by IN ({_in(sc)}))"
        args += sc
    rows = all_(conn, sql + " ORDER BY arrived_at DESC, id DESC LIMIT 300", args)
    mr = rules(conn, user["project_id"])["material_rules"]
    return {"items": [batch_view(conn, b, mr) for b in rows], "note": NOTE_MATERIAL, "categories": CATEGORIES, "retests": RETEST,
            "can_write": user["role"] in WRITE_ROLES, "can_quality": is_quality(user)}


def set_retest(conn, user: dict, bid: int, body: dict) -> dict:
    b = one(conn, "SELECT * FROM material_batches WHERE id=? AND project_id=?", (bid, user["project_id"]))
    sc = scope_of(conn, user)
    if not b or (sc is not None and not one(conn, f"SELECT 1 x FROM tickets WHERE batch_id=? AND uploaded_by IN ({_in(sc)}) LIMIT 1", [bid] + sc)):
        raise TicketError(404, "批次不存在")
    status = body.get("status")
    if status not in RETEST:
        raise TicketError(400, "复试状态应为 not_required/pending/sampled/pass/fail")
    if user["role"] not in WRITE_ROLES:
        raise TicketError(403, "当前岗位只能查看")
    if status in ("pass", "fail", "not_required") or b["retest_status"] in ("pass", "fail", "not_required"):
        if not is_quality(user):
            raise TicketError(403, "录入复试结果、调整复试要求需工程主管/项目经理")
    report = (body.get("report_no") or "").strip()[:60] or None
    reported = _iso(body["reported_at"], "报告") if body.get("reported_at") else None
    if status in ("pass", "fail"):
        report = report or b["report_no"]
        if not report:
            raise TicketError(400, "请填写复试报告号")
        reported = reported or b["reported_at"] or _today()
    else:
        report, reported = (report or b["report_no"]) if status == "sampled" else None, None
    with tx(conn):
        conn.execute("UPDATE material_batches SET retest_status=?, report_no=?, reported_at=?, retest_note=?, sampled_at=COALESCE(?, sampled_at), updated_by=?, updated_at=? WHERE id=?",
                     (status, report, reported, (body.get("note") or "")[:120], _today() if status == "sampled" else None, user["id"], now_iso(), bid))
        audit(conn, user["id"], "batch.retest", f"batch:{bid}", f"{b['name']} {b['spec']} {b['batch_no'] or '-'} → {status} 报告{report or '-'}"[:200])
    return batch_view(conn, one(conn, "SELECT * FROM material_batches WHERE id=?", (bid,)), rules(conn, user["project_id"])["material_rules"])


def used_before_retest(conn, project_id: int, authors: list[int] | None = None) -> list[dict]:
    """确定性规则：某类材料有 pending/sampled/fail 批次，而到场日之后的已确认日志记录了该类材料“消耗/使用”——提示核对。"""
    bs = all_(conn, "SELECT * FROM material_batches WHERE project_id=? AND retest_status IN ('pending','sampled','fail') AND arrived_at IS NOT NULL ORDER BY arrived_at", (project_id,))
    if not bs:
        return []
    lsql, largs = "SELECT id, log_date, author_id, content_json FROM logs WHERE project_id=? AND status IN ('submitted','approved') AND period='day'", [project_id]
    if authors is not None:
        lsql += f" AND author_id IN ({_in(authors)})"
        largs += authors
    logs = all_(conn, lsql + " ORDER BY log_date", largs)
    out = []
    for cat in ("rebar", "cement", "sand", "stone", "admixture"):
        cb = [b for b in bs if b["category"] == cat]
        if not cb:
            continue
        first = min(b["arrived_at"] for b in cb)
        hits = []
        for lg in logs:
            if lg["log_date"] <= first:
                continue
            for m in (loads(lg["content_json"], {}) or {}).get("materials") or []:
                act = str(m.get("action") or "")
                if ("消耗" in act or "使用" in act) and TA.match_category(str(m.get("name") or ""), cat):
                    hits.append({"date": lg["log_date"], "log_id": lg["id"], "name": m.get("name"), "qty": m.get("qty"), "unit": m.get("unit")})
        if hits:
            out.append({"category": cat, "batches": cb, "logs": hits})
    return out


# ----------------------------------------------------------------------------- 风险汇总

def _md(d: str) -> str:
    return f"{int(d[5:7])}月{int(d[8:10])}日"


def risk_items(conn, project_id: int, scope: list[int] | None = None) -> list[dict]:
    """全部预警项（含通知对象 _to）。scope 非 None 时只含分包单位自己范围的数据。"""
    items: list[dict] = []
    rl = rules(conn, project_id)
    conc, mat = rl["concrete_rules"], rl["material_rules"]
    sc_sql = f" AND uploaded_by IN ({_in(scope)})" if scope is not None else ""
    sc_args = list(scope or [])
    managers = [u["id"] for u in all_(conn, "SELECT id FROM users WHERE project_id=? AND active=1 AND role='eng_sup'", (project_id,))] or \
               [u["id"] for u in all_(conn, "SELECT id FROM users WHERE project_id=? AND active=1 AND role='manager'", (project_id,))]

    def add(kind, level, title, detail, basis, tab, ref=None, to=(), **extra):
        items.append({"key": f"{kind}:{ref if ref is not None else extra.get('category', 'all')}", "kind": kind, "level": level, "title": title, "detail": detail,
                      "basis": basis, "link": {"route": "tickets", "tab": tab, "id": ref, **extra}, "_to": list(dict.fromkeys(to))})

    # 待确认与疑似重复
    drafts = all_(conn, "SELECT * FROM tickets WHERE project_id=? AND status='draft'" + sc_sql + " ORDER BY id", [project_id] + sc_args)
    if drafts:
        add("ticket_pending", "low", f"{len(drafts)} 张票据待确认", "识别出的草稿在人确认之前不计入任何累计，请核对后确认或作废。",
            [f"票据 #{d['id']}（{DOC_KINDS[d['doc_kind']]}）" for d in drafts[:8]], "pending", None, [d["uploaded_by"] for d in drafts])
    for d in drafts:
        dups = find_dups(conn, d, _fields_of(d), scope)
        if dups:
            add("ticket_dup", "medium", f"票据 #{d['id']} 疑似重复", "；".join(x["text"] for x in dups[:2]) + "。确认前请核对，避免方量/数量重复入账。",
                [x["text"] for x in dups], "pending", d["id"], [d["uploaded_by"]])
    un = all_(conn, "SELECT id, uploaded_by, qty FROM tickets WHERE project_id=? AND status='confirmed' AND doc_kind='concrete_ticket' AND pour_id IS NULL" + sc_sql, [project_id] + sc_args)
    if un:
        add("ticket_unassigned", "low", f"{len(un)} 张已确认砼小票未归入浇筑单元",
            f"合计 {sum(u['qty'] or 0 for u in un):g} m³ 尚未计入任何浇筑单元的累计。", [f"票据 #{u['id']}" for u in un[:8]], "pending", None, [u["uploaded_by"] for u in un])

    # 浇筑单元：方量差异、少留试块、试块到期
    pours = all_(conn, "SELECT * FROM pours WHERE project_id=? ORDER BY id", (project_id,))
    if scope is not None:
        pours = [p for p in pours if _pour_visible(conn, p, scope)]
    today = _today()
    for p in pours:
        v = pour_view(conn, p, scope, rl, True)
        uploaders = [r["uploaded_by"] for r in all_(conn, "SELECT DISTINCT uploaded_by FROM tickets WHERE pour_id=? AND status='confirmed'", (p["id"],))] + [p["created_by"]]
        to = uploaders + managers
        if v["deviation_flag"]:
            add("qty_deviation", "medium", f"{v['label']}：方量差异疑点", v["deviation_text"],
                [f"理论方量 {v['design_qty']:g} m³（人工填写）", f"已确认累计 {v['actual_qty']:g} m³，{v['trucks']} 车", f"阈值 ±{v['threshold_pct']:g}%"], "pours", p["id"], to,
                pour_id=p["id"])
        sp = v["specimens"]
        if sp["missing"] > 0:
            add("specimen_missing", "medium", f"{v['label']}：疑似少留置试块",
                f"按通用常规要求测算应留 {sp['required']} 组，已登记 {sp['registered']} 组，差 {sp['missing']} 组。请核对是否漏登记或漏留置（以试验计划和监理要求为准）。",
                [sp["basis"]], "pours", p["id"], to, pour_id=p["id"])
        for s in v["specimen_list"]:
            a28 = s["age28"]
            if a28["state"] == "overdue":
                add("specimen_overdue", "high", f"{v['label']} 第{s['group_no']}组：28天结果超期未录",
                    f"28天龄期 {a28['due']} 已过 {-a28['days_left']} 天，仍未录入试验室报告强度。", [f"成型 {s['molded_date']}，{s['curing']}"], "pours", s["id"], to, pour_id=p["id"])
            elif a28["state"] == "soon":
                add("specimen_due", "medium", f"{v['label']} 第{s['group_no']}组：28天龄期临近",
                    f"28天龄期 {a28['due']}，还剩 {a28['days_left']} 天，请安排送检/催要试验室报告。", [f"成型 {s['molded_date']}，{s['curing']}"], "pours", s["id"], to, pour_id=p["id"])

    # 材料：已用未复试、复试不合格
    for h in used_before_retest(conn, project_id, scope):
        cat, name = h["category"], CATEGORIES[h["category"]]
        first = h["logs"][0]
        bt = [f"{b['name']} {b['spec'] or ''} 批号{b['batch_no'] or '未记录'}，{_md(b['arrived_at'])}到场，{RETEST[b['retest_status']]}" for b in h["batches"]]
        lg = [f"{_md(x['date'])} 日志：{x['name']} 消耗{'' if x['qty'] is None else ' ' + format(x['qty'], 'g')}{x['unit'] or ''}" for x in h["logs"][:6]]
        tos = [r["uploaded_by"] for b in h["batches"] for r in all_(conn, "SELECT DISTINCT uploaded_by FROM tickets WHERE batch_id=?", (b["id"],))]
        add("used_before_retest", "high", f"{name}：存在未出复试结果的批次",
            f"{name}存在未出复试结果的批次（{len(h['batches'])} 个），{_md(first['date'])}日志记录了{first['name']}消耗，请核对是否已使用未复试批次。日志无法精确到批次，仅作提示。",
            bt + lg, "batches", None, tos + managers, category=cat)
    for b in all_(conn, "SELECT * FROM material_batches WHERE project_id=? AND retest_status='fail'", (project_id,)):
        if scope is not None and not one(conn, f"SELECT 1 x FROM tickets WHERE batch_id=? AND uploaded_by IN ({_in(scope)}) LIMIT 1", [b["id"]] + scope):
            continue
        tos = [r["uploaded_by"] for r in all_(conn, "SELECT DISTINCT uploaded_by FROM tickets WHERE batch_id=?", (b["id"],))]
        add("retest_fail", "high", f"{b['name']} {b['spec'] or ''}（批号{b['batch_no'] or '未记录'}）复试不合格",
            f"{_md(b['arrived_at'])}到场的这批材料复试结果为不合格（报告号 {b['report_no'] or '未填'}），请按项目程序处理，并核对是否已用于工程。",
            [f"累计 {b['qty']:g} {b['unit']}"], "batches", b["id"], tos + managers)
    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda i: order[i["level"]])
    return items


def risks(conn, user: dict) -> dict:
    sc = scope_of(conn, user)
    items = risk_items(conn, user["project_id"], sc)
    counts: dict[str, int] = {}
    for i in items:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    pend = one(conn, "SELECT COUNT(*) n FROM tickets WHERE project_id=? AND status='draft'" + (f" AND uploaded_by IN ({_in(sc)})" if sc is not None else ""),
               [user["project_id"]] + (sc or []))["n"]
    return {"items": [{k: v for k, v in i.items() if k != "_to"} for i in items], "counts": counts, "total": len(items), "pending_tickets": pend,
            "alerts": sum(1 for i in items if i["level"] in ("high", "medium")),
            "note": "以上均为按票据、台账和日志核对出的疑点，请人工核实后处理；系统不下合格/不合格结论。"}


KIND_NAME = {"ticket_pending": "票据待确认", "ticket_dup": "票据疑似重复", "ticket_unassigned": "砼小票未归单元", "qty_deviation": "方量差异疑点",
             "specimen_missing": "疑似少留试块", "specimen_overdue": "试块结果超期未录", "specimen_due": "试块28天临期",
             "used_before_retest": "已用未复试请核对", "retest_fail": "复试不合格"}


DOUBT_REPUSH_DAYS = 7                                    # 同一疑点仍未核对，隔多少天可以再推一次（AC-4.7）
_DAY_COUNTER = re.compile(r"(已过|还剩)\s*-?\d+\s*天")      # 逐日变化的天数不算“疑点内容变了”


def _doubt_sig(it: dict) -> str:
    """疑点内容指纹：标题 + 说明（去掉逐日变化的“已过/还剩 N 天”）。方量、组数、批次等实质内容变了指纹才变。"""
    import hashlib
    text = it["title"] + "|" + _DAY_COUNTER.sub(r"\1N天", it["detail"])
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _doubt_pushed(conn, uid: int, key: str, sig: str, title: str, body: str, cutoff: str) -> bool:
    """同一疑点已推给此人、内容没变、且上次推送在 cutoff 之后（不满 7 天）：不再推。
    疑点键存在通知的 link_json（params.doubt / params.sig）；升级前的旧消息没有疑点键，同标题同内容即视为同一疑点、内容未变。"""
    last = one(conn, "SELECT link_json, created_at FROM notifications WHERE user_id=? AND title LIKE '材料与砼提醒：%' "
                     "AND CASE WHEN json_valid(link_json) THEN json_extract(link_json, '$.params.doubt') END=? ORDER BY id DESC LIMIT 1", (uid, key))
    if last:
        same = ((loads(last["link_json"], {}) or {}).get("params") or {}).get("sig") == sig
    else:
        last = one(conn, "SELECT created_at FROM notifications WHERE user_id=? AND title=? AND body=? ORDER BY id DESC LIMIT 1", (uid, title, body))
        same = True
    return bool(last) and same and last["created_at"][:10] > cutoff


def notify_due(conn, project_id: int) -> dict:
    """向责任人发站内消息（票据上传人 + 工程主管；无工程主管时给项目经理）。可反复调用。
    材料疑点（方量差异、少留试块、试块到期、复试等）同一疑点对同一人只推一次（C14 / AC-4.7）：疑点内容变了、或 7 天后仍未核对，才再推一次；
    核对后疑点从清单消失即不再推。“票据待确认”是待办提醒，仍按同用户同内容每天一次。检查与插入在同一事务内，并发调用也不重复。"""
    day = _today()
    cutoff = (date.fromisoformat(day) - timedelta(days=DOUBT_REPUSH_DAYS)).isoformat()
    sent = skipped = 0
    active = {u["id"] for u in all_(conn, "SELECT id FROM users WHERE project_id=? AND active=1 AND role NOT IN ('admin','leader')", (project_id,))}
    pend_by_user: dict[int, int] = {}
    for it in risk_items(conn, project_id, None):
        if it["kind"] in ("ticket_unassigned", "ticket_dup"):
            continue
        if it["kind"] == "ticket_pending":
            for d in all_(conn, "SELECT uploaded_by FROM tickets WHERE project_id=? AND status='draft' AND created_at<?", (project_id, day)):
                pend_by_user[d["uploaded_by"]] = pend_by_user.get(d["uploaded_by"], 0) + 1
            continue
        title = f"材料与砼提醒：{KIND_NAME.get(it['kind'], it['kind'])}"
        body = f"{it['title']}。{it['detail']}"
        sig = _doubt_sig(it)
        lk = it["link"]
        params = {"tab": lk.get("tab") or "pending", "doubt": it["key"], "sig": sig}   # 点消息直达对应页签（F-06）
        for k in ("pour_id", "category"):
            if lk.get(k) is not None:
                params[k] = str(lk[k])
        link = dumps({"route": "tickets", "params": params})
        for uid in it["_to"]:
            if uid not in active:
                continue
            with tx(conn):
                if _doubt_pushed(conn, uid, it["key"], sig, title, body, cutoff):
                    skipped += 1
                    continue
                insert(conn, "notifications", {"project_id": project_id, "user_id": uid, "alert_id": None, "title": title, "body": body,
                                               "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(), "link_json": link})
            sent += 1
    for uid, n in pend_by_user.items():
        if uid not in active:
            continue
        title, body = "材料与砼提醒：票据待确认", f"你有 {n} 张前几天拍的票据还没确认，确认后才会计入累计。"
        with tx(conn):
            if one(conn, "SELECT 1 x FROM notifications WHERE user_id=? AND title=? AND body=? AND substr(created_at,1,10)=?", (uid, title, body, day)):
                skipped += 1
                continue
            insert(conn, "notifications", {"project_id": project_id, "user_id": uid, "alert_id": None, "title": title, "body": body,
                                           "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(),
                                           "link_json": dumps({"route": "tickets", "params": {"tab": "pending"}})})
        sent += 1
    return {"sent": sent, "skipped": skipped}
