"""现场圈的港湾公告（含已读回执）。

公告由港湾管理岗（项目经理、工程主管、安全主管）发布，可置顶，可限定全项目或某分包单位；
成员看到即记已读，发布者能看已读/未读名单。分包单位与领导只读。
（Agent 从真实动态中挑选值得看的内容见 circle_picks.py。）
"""
from __future__ import annotations

from .. import roles as R
from ..db import all_, audit, dumps, insert, now_iso, one
from .role_views import NO_COMPANY

NOTICE_ROLES = ("manager", "eng_sup", "safety_sup")
MAX_TITLE, MAX_BODY = 40, 500


def can_publish(user: dict) -> bool:
    return user["role"] in NOTICE_ROLES


# ============================================================================ 公告

def companies(conn, project_id: int) -> list[str]:
    rows = all_(conn, "SELECT DISTINCT company FROM users WHERE project_id=? AND active=1 AND company<>'' AND role IN ('tech','sub_safety') ORDER BY company",
                (project_id,))
    return [r["company"] for r in rows]


def recipients(conn, n: dict) -> list[dict]:
    """应读人员：限定单位 → 该单位的分包人员；全项目 → 除系统管理员外的全部成员（含领导，只读）。"""
    if n["audience"] == "company":
        return all_(conn, "SELECT id, name, role, company FROM users WHERE project_id=? AND active=1 AND company=? AND role IN ('tech','sub_safety') ORDER BY id",
                    (n["project_id"], n["company"]))
    return all_(conn, "SELECT id, name, role, company FROM users WHERE project_id=? AND active=1 AND role<>'admin' ORDER BY id", (n["project_id"],))


def can_see_notice(user: dict, n: dict) -> bool:
    if n["project_id"] != user["project_id"] or n["status"] != "active":
        return False
    if n["audience"] == "all" or not R.is_sub(user["role"]):
        return True
    return (user.get("company") or "") == n["company"]


def publish(conn, user: dict, title: str, body: str, pinned: bool = False, audience: str = "all", company: str = "") -> dict:
    if not can_publish(user):
        raise PermissionError("只有港湾管理岗（项目经理、工程主管、安全主管）可以发布公告")
    title, body, company = (title or "").strip(), (body or "").strip(), (company or "").strip()
    if not title:
        raise ValueError("请写公告标题")
    if len(title) > MAX_TITLE:
        raise ValueError(f"标题最多 {MAX_TITLE} 字")
    if not body or len(body) > MAX_BODY:
        raise ValueError(f"公告内容不能为空且不超过 {MAX_BODY} 字")
    if audience not in ("all", "company"):
        raise ValueError("发布范围无效")
    if audience == "company":
        if company not in companies(conn, user["project_id"]):
            raise ValueError("请选择已有的分包单位")
    else:
        company = ""
    nid = insert(conn, "circle_notices", {"project_id": user["project_id"], "author_id": user["id"], "title": title, "body": body,
                                          "pinned": 1 if pinned else 0, "audience": audience, "company": company,
                                          "status": "active", "created_at": now_iso()})
    n = one(conn, "SELECT * FROM circle_notices WHERE id=?", (nid,))
    audit(conn, user["id"], "circle.notice.publish", f"notice:{nid}", f"{audience}:{company} {title}"[:100])
    for u in recipients(conn, n):
        if u["id"] != user["id"]:
            insert(conn, "notifications", {"project_id": user["project_id"], "user_id": u["id"], "alert_id": None, "title": f"公告：{title}",
                                           "body": body[:80], "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso()})
    return n


def _notice(conn, user: dict, nid: int) -> dict:
    n = one(conn, "SELECT * FROM circle_notices WHERE id=? AND project_id=?", (nid, user["project_id"]))
    if not n or not can_see_notice(user, n):
        raise ValueError("公告不存在")
    return n


def mark_read(conn, user: dict, nid: int) -> dict:
    n = _notice(conn, user, nid)
    conn.execute("INSERT OR IGNORE INTO circle_notice_reads (notice_id, user_id, read_at) VALUES (?,?,?)", (nid, user["id"], now_iso()))
    return {"id": n["id"], "read": True}


def _can_manage(user: dict, n: dict) -> bool:
    return can_publish(user) and (n["author_id"] == user["id"] or user["role"] == "manager")


def reads(conn, user: dict, nid: int) -> dict:
    n = _notice(conn, user, nid)
    if not _can_manage(user, n):
        raise PermissionError("只有发布人或项目经理能看已读名单")
    got = {r["user_id"]: r["read_at"] for r in all_(conn, "SELECT user_id, read_at FROM circle_notice_reads WHERE notice_id=?", (nid,))}
    rec = [u for u in recipients(conn, n) if u["id"] != n["author_id"]]

    def row(u):
        return {"id": u["id"], "name": u["name"], "role_name": R.role_name(u["role"]), "company": u["company"] or "", "read_at": got.get(u["id"])}
    return {"id": nid, "read": [row(u) for u in rec if u["id"] in got], "unread": [row(u) for u in rec if u["id"] not in got], "total": len(rec)}


def set_pinned(conn, user: dict, nid: int, pinned: bool) -> dict:
    n = _notice(conn, user, nid)
    if not _can_manage(user, n):
        raise PermissionError("只有发布人或项目经理能置顶")
    conn.execute("UPDATE circle_notices SET pinned=? WHERE id=?", (1 if pinned else 0, nid))
    audit(conn, user["id"], "circle.notice.pin", f"notice:{nid}", "置顶" if pinned else "取消置顶")
    return {"id": nid, "pinned": bool(pinned)}


def withdraw(conn, user: dict, nid: int) -> dict:
    n = _notice(conn, user, nid)
    if not _can_manage(user, n):
        raise PermissionError("只有发布人或项目经理能撤回")
    conn.execute("UPDATE circle_notices SET status='withdrawn', pinned=0 WHERE id=?", (nid,))
    audit(conn, user["id"], "circle.notice.withdraw", f"notice:{nid}", n["title"][:60])
    return {"id": nid, "withdrawn": True}


def notice_view(conn, user: dict, n: dict) -> dict:
    me = one(conn, "SELECT read_at FROM circle_notice_reads WHERE notice_id=? AND user_id=?", (n["id"], user["id"]))
    out = {"id": n["id"], "title": n["title"], "body": n["body"], "pinned": bool(n["pinned"]), "audience": n["audience"],
           "company": n["company"], "scope_label": "全项目" if n["audience"] == "all" else n["company"] or NO_COMPANY,
           "read_me": bool(me), "can_manage": _can_manage(user, n), "stats": None}
    if out["can_manage"]:
        rec = [u["id"] for u in recipients(conn, n) if u["id"] != n["author_id"]]
        got = {r["user_id"] for r in all_(conn, "SELECT user_id FROM circle_notice_reads WHERE notice_id=?", (n["id"],))}
        out["stats"] = {"read": sum(1 for i in rec if i in got), "total": len(rec)}
    return out


# ============================================================================ 动态流接入（feed.py 调用）

def notice_raws(conn, user: dict, before: str | None, limit: int, mine: bool) -> tuple[list[dict], list[dict]]:
    """返回 (按时间的公告动态, 置顶公告)。置顶公告只在第一页顶部出现，不在翻页流中重复。"""
    q = "SELECT * FROM circle_notices WHERE project_id=? AND status='active'"
    p: list = [user["project_id"]]
    if R.is_sub(user["role"]):
        q += " AND (audience='all' OR company=?)"
        p.append(user.get("company") or "")
    if mine:
        q += " AND author_id=?"
        p.append(user["id"])
    else:
        q += " AND pinned=0"
    if before:
        q += " AND created_at<?"
        p.append(before)
    rows = all_(conn, q + " ORDER BY created_at DESC LIMIT ?", p + [limit])
    pinned: list[dict] = []
    if not before and not mine:
        pq = "SELECT * FROM circle_notices WHERE project_id=? AND status='active' AND pinned=1"
        pp: list = [user["project_id"]]
        if R.is_sub(user["role"]):
            pq += " AND (audience='all' OR company=?)"
            pp.append(user.get("company") or "")
        pinned = all_(conn, pq + " ORDER BY created_at DESC LIMIT 5", pp)

    def raw(n, pin):
        return {"key": f"notice:{n['id']}", "kind": "notice", "author_id": n["author_id"], "time": n["created_at"], "notice": n, "pinned": pin}
    return [raw(n, False) for n in rows], [raw(n, True) for n in pinned]


def can_see_notice_id(conn, user: dict, nid: int) -> int | None:
    """动态键校验用：公告可见则返回发布人 id，否则抛出“动态不存在”。"""
    n = one(conn, "SELECT * FROM circle_notices WHERE id=? AND project_id=?", (nid, user["project_id"]))
    if not n or not can_see_notice(user, n):
        raise ValueError("动态不存在")
    return n["author_id"]
