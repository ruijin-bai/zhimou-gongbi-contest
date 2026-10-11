"""索赔通知时效（工笔文书方案 2026-10-08 一.1）：给 claims.all_events() 的每个事件算“通知期限”，记录 已发出 / 不发 的人工决定。

- 只加表 claim_notices，不改旧表；没有记录的事件按默认值算：知悉日 = 事件起始日，状态 = 待处理；
- 期限 = 知悉日 + 通知天数；天数是项目规则（knowledge kind='claim_rule' key='notice_days'），默认 28（FIDIC 2017 第 20.2.1 款），只有系统管理员可改；
- 知悉日可人工改，改动写 audit 留痕；“不发”必须填原因；已过期仍待处理的照常列出（标红），不自动关闭；
- 系统只提醒时效、汇集证据，不判断事件是否构成索赔，不写金额。
"""
from __future__ import annotations

from datetime import date, timedelta

from ..db import all_, audit, dumps, loads, now_iso, one
from . import claims

SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_notices (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    aware_date TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','waived')),
    decided_by INTEGER,
    decided_at TEXT,
    reason TEXT,
    letter_id INTEGER,
    aware_changed_by INTEGER,
    aware_changed_at TEXT,
    UNIQUE (project_id, event_key)
);
"""

DEFAULT_DAYS = 28
CLAUSE = "FIDIC 2017 第 20.2.1 款"
CLAUSE_EN = "Sub-Clause 20.2.1 of the Conditions of Contract (FIDIC 2017)"
STATUS_NAMES = {"pending": "待处理", "sent": "已发出", "waived": "不发"}
SCOPE_NOTE = "系统只提醒通知时效、汇集证据；是否构成索赔事件以商务和合同部门判断为准，系统不写金额。"
ENG_DAYS, PM_DAYS = 7, 3      # 剩 7 天进工程主管待办，剩 3 天升级到项目经理（用户 10-08 已定）


def notice_days(conn, project_id: int) -> int:
    row = one(conn, "SELECT value_json FROM knowledge WHERE project_id=? AND kind='claim_rule' AND key='notice_days'", (project_id,))
    v = (loads(row["value_json"], {}) or {}).get("days") if row else None
    return int(v) if isinstance(v, (int, float)) and 1 <= v <= 365 else DEFAULT_DAYS


def set_notice_days(conn, user: dict, days: int, basis: str = "") -> int:
    if user["role"] != "admin":
        raise PermissionError("通知天数属于项目规则，由系统管理员修改")
    if not 1 <= int(days) <= 365:
        raise ValueError("通知天数应在 1～365 之间")
    old = notice_days(conn, user["project_id"])
    conn.execute("INSERT OR REPLACE INTO knowledge (project_id, kind, key, value_json, ai_run_id, updated_at) VALUES (?,?,?,?,?,?)",
                 (user["project_id"], "claim_rule", "notice_days", dumps({"days": int(days), "basis": basis[:120]}), None, now_iso()))
    audit(conn, user["id"], "claim.notice_days", f"project:{user['project_id']}", f"{old}→{int(days)} {basis[:120]}")
    return int(days)


def _row(conn, pid: int, key: str) -> dict | None:
    return one(conn, "SELECT * FROM claim_notices WHERE project_id=? AND event_key=?", (pid, key))


def state(ev: dict, row: dict | None, days: int, today: date) -> dict:
    """单个事件的时效状态。remain：距期限的天数（负数 = 已过期）；已发出 / 不发 的不再计剩余天数。"""
    aware = (row or {}).get("aware_date") or ev["date_from"]
    deadline = (date.fromisoformat(aware) + timedelta(days=days)).isoformat()
    status = (row or {}).get("status") or "pending"
    remain = (date.fromisoformat(deadline) - today).days
    if status == "sent":
        label, tone = "已发出", "green"
    elif status == "waived":
        label, tone = "不发", "gray"
    elif remain < 0:
        label, tone = f"已过期 {-remain} 天", "red"
    else:
        label, tone = f"剩 {remain} 天", "red" if remain <= PM_DAYS else "orange" if remain <= ENG_DAYS else "blue"
    return {"aware_date": aware, "aware_default": not (row or {}).get("aware_changed_at"), "deadline": deadline, "days": days,
            "status": status, "status_name": STATUS_NAMES[status], "remain": remain if status == "pending" else None, "label": label, "tone": tone,
            "reason": (row or {}).get("reason") or "", "decided_at": (row or {}).get("decided_at"), "letter_id": (row or {}).get("letter_id")}


def states(conn, project_id: int, today: date | None = None) -> dict[str, dict]:
    today = today or date.today()
    days = notice_days(conn, project_id)
    rows = {r["event_key"]: r for r in all_(conn, "SELECT * FROM claim_notices WHERE project_id=?", (project_id,))}
    return {ev["key"]: {**state(ev, rows.get(ev["key"]), days, today), "event": ev} for ev in claims.all_events(conn, project_id)}


def sort_key(n: dict):
    """待处理在前、按剩余天数从少到多（已过期最前）；已发出 / 不发 排后。"""
    return (0 if n["status"] == "pending" else 1, n["remain"] if n["remain"] is not None else 10 ** 6)


def _event(conn, pid: int, key: str) -> dict:
    ev = next((e for e in claims.all_events(conn, pid) if e["key"] == key), None)
    if not ev:
        raise LookupError("事件不存在")
    return ev


def _upsert(conn, pid: int, ev: dict, fields: dict) -> None:
    days = notice_days(conn, pid)
    row = _row(conn, pid, ev["key"])
    aware = fields.get("aware_date") or (row or {}).get("aware_date") or ev["date_from"]
    deadline = (date.fromisoformat(aware) + timedelta(days=days)).isoformat()
    if row:
        sets = {**fields, "aware_date": aware, "deadline": deadline}
        conn.execute(f"UPDATE claim_notices SET {', '.join(k + '=?' for k in sets)} WHERE id=?", [*sets.values(), row["id"]])
    else:
        rec = {"project_id": pid, "event_key": ev["key"], "status": "pending", **fields, "aware_date": aware, "deadline": deadline}
        conn.execute(f"INSERT INTO claim_notices ({', '.join(rec)}) VALUES ({', '.join('?' for _ in rec)})", list(rec.values()))


def set_aware(conn, user: dict, key: str, aware_date: str, note: str = "") -> dict:
    pid = user["project_id"]
    ev = _event(conn, pid, key)
    d = date.fromisoformat(aware_date)
    if d.isoformat() < ev["date_from"] or d > date.today():
        raise ValueError("知悉日应不早于事件起始日、不晚于今天")
    old = (_row(conn, pid, key) or {}).get("aware_date") or ev["date_from"]
    _upsert(conn, pid, ev, {"aware_date": d.isoformat(), "aware_changed_by": user["id"], "aware_changed_at": now_iso()})
    audit(conn, user["id"], "claim.notice.aware", f"claim:{key}", f"{old}→{d.isoformat()} {note[:120]}".strip())
    return view_one(conn, pid, key)


def decide(conn, user: dict, key: str, status: str, reason: str = "", letter_id: int | None = None) -> dict:
    """人工决定：sent（已发出，由人对外发送，系统只记录）/ waived（不发，必须填原因）/ pending（撤回决定）。"""
    pid = user["project_id"]
    ev = _event(conn, pid, key)
    if status not in STATUS_NAMES:
        raise ValueError("状态只能是 待处理 / 已发出 / 不发")
    reason = (reason or "").strip()
    if status == "waived" and not reason:
        raise ValueError("选择“不发”须填写原因")
    if letter_id is not None and not one(conn, "SELECT 1 FROM gongbi_docs WHERE id=? AND project_id=? AND kind='notice_of_claim'", (letter_id, pid)):
        raise ValueError("关联的通知函不存在")
    _upsert(conn, pid, ev, {"status": status, "reason": reason[:300] or None, "decided_by": user["id"] if status != "pending" else None,
                            "decided_at": now_iso() if status != "pending" else None, "letter_id": letter_id if status == "sent" else None})
    audit(conn, user["id"], f"claim.notice.{status}", f"claim:{key}", reason[:200])
    return view_one(conn, pid, key)


def view_one(conn, pid: int, key: str) -> dict:
    ev = _event(conn, pid, key)
    return state(ev, _row(conn, pid, key), notice_days(conn, pid), date.today())


def due_items(conn, project_id: int, role: str, today: date | None = None) -> list[dict]:
    """待办用：待处理且剩余天数 ≤ 7（工程主管）/ ≤ 3（项目经理），含已过期。分包、技术员等其他岗位一律没有。"""
    limit = {"eng_sup": ENG_DAYS, "manager": PM_DAYS}.get(role)
    if limit is None:
        return []
    out = [n for n in states(conn, project_id, today).values() if n["status"] == "pending" and n["remain"] is not None and n["remain"] <= limit]
    return sorted(out, key=sort_key)
