"""现场圈讨论组：话题 / 问题 / 专题三类小组，组内发帖（文字 + 照片）、赞与评论，可关联工点或 P6 作业，可关闭、置顶。

口径与现场圈一致：
- 全项目可见（10-09 RJ 定，分包单位也看到全部小组与组内照片）；领导只读；系统管理员不参与；
- 能互动的岗位（技术员、分包安全员、港湾管理岗）都能建组、发帖、评论、点赞；
- 关闭：建组人或港湾管理岗（问题组关闭即“已解决”，可写一句说明）；关闭后只读，可重新打开；
- 置顶：只有港湾管理岗（与公告置顶同一批人）；
- 帖子、评论只能本人删除；小组不删除，只关闭。
讨论组只是沟通层：不生成待办、不改进度/审批/计量，不走 AI 识别（照片 purpose='talk'，不做隐患识别）。
"""
from __future__ import annotations

from .. import roles as R
from ..db import all_, audit, dumps, insert, loads, now_iso, one
from . import act_names as _an
from . import p6
from .circle_board import can_publish
from .parts import activity_part

KINDS = {"topic": "话题", "issue": "问题", "special": "专题"}
MAX_TITLE, MAX_INTRO, MAX_POST, MAX_COMMENT, MAX_PHOTOS = 40, 300, 1000, 300, 9


def can_view(user: dict) -> bool:
    return user["role"] != "admin"


def can_write(user: dict) -> bool:
    return user["role"] not in ("admin", "leader")


def _need_write(user: dict) -> None:
    if user["role"] == "admin":
        raise PermissionError("系统管理员不参与现场动态")
    if user["role"] == "leader":
        raise PermissionError("当前角色只能查看讨论")


def _group(conn, user: dict, gid: int) -> dict:
    g = one(conn, "SELECT * FROM talk_groups WHERE id=? AND project_id=?", (gid, user["project_id"]))
    if not g:
        raise ValueError("讨论组不存在")
    return g


def _open_group(conn, user: dict, gid: int) -> dict:
    g = _group(conn, user, gid)
    if g["status"] != "open":
        raise ValueError("讨论组已关闭，不能再发言")
    return g


def can_manage(user: dict, g: dict) -> bool:
    """关闭/重新打开：建组人或港湾管理岗。"""
    return g["author_id"] == user["id"] or can_publish(user)


def _notify(conn, pid: int, uid: int | None, title: str, body: str, gid: int, skip: set[int]) -> None:
    if uid and uid not in skip:
        skip.add(uid)
        insert(conn, "notifications", {"project_id": pid, "user_id": uid, "alert_id": None, "title": title, "body": body[:80],
                                       "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(),
                                       "link_json": dumps({"route": "circle", "params": {"view": "talk", "g": gid}})})


def _touch(conn, gid: int) -> None:
    conn.execute("UPDATE talk_groups SET last_at=? WHERE id=?", (now_iso(), gid))


# ============================================================================ 工点与作业

def options(conn, user: dict) -> dict:
    """建组时可选的工点（P6 作业归出的部位）与作业（页面一律显示中文简称，代码只作值）。"""
    if not can_view(user):
        raise PermissionError("系统管理员不参与现场动态")
    acts = p6.current_activities(conn, user["project_id"])
    mp = _an.names(conn, user["project_id"])
    parts = sorted({p for p in (activity_part(a) for a in acts) if p})
    items = [{"code": a["code"], "label": _an.label(a["code"], a["name"], mp), "part": activity_part(a) or ""}
             for a in acts if not a["is_milestone"] and not a["act_finish"]]
    return {"kinds": [{"k": k, "name": v} for k, v in KINDS.items()], "parts": parts, "activities": items}


def _act_label(conn, user: dict, code: str) -> str:
    if not code:
        return ""
    a = one(conn, """SELECT a.code, a.name FROM activities a JOIN p6_imports i ON i.id=a.import_id
                     WHERE i.project_id=? AND a.code=? ORDER BY i.is_current DESC, i.id DESC LIMIT 1""", (user["project_id"], code))
    return _an.label(code, a["name"] if a else None, _an.names(conn, user["project_id"]))


# ============================================================================ 小组

def create_group(conn, user: dict, kind: str, title: str, intro: str = "", part: str = "", activity_code: str = "") -> dict:
    _need_write(user)
    title, intro, part, code = (title or "").strip(), (intro or "").strip(), (part or "").strip()[:30], (activity_code or "").strip()
    if kind not in KINDS:
        raise ValueError("讨论组类型无效")
    if not title:
        raise ValueError("请写讨论组名称")
    if len(title) > MAX_TITLE:
        raise ValueError(f"名称最多 {MAX_TITLE} 字")
    if len(intro) > MAX_INTRO:
        raise ValueError(f"说明最多 {MAX_INTRO} 字")
    if code:
        a = next((x for x in p6.current_activities(conn, user["project_id"]) if x["code"] == code), None)
        if not a:
            raise ValueError("所选作业不在当前 P6 计划里")
        part = part or (activity_part(a) or "")
    now = now_iso()
    gid = insert(conn, "talk_groups", {"project_id": user["project_id"], "kind": kind, "title": title, "intro": intro, "part": part,
                                       "activity_code": code, "author_id": user["id"], "status": "open", "pinned": 0,
                                       "created_at": now, "last_at": now})
    audit(conn, user["id"], "talk.group.create", f"talk:{gid}", f"{kind} {title}"[:100])
    return group_view(conn, user, _group(conn, user, gid))


def set_status(conn, user: dict, gid: int, closed: bool, note: str = "") -> dict:
    _need_write(user)
    g = _group(conn, user, gid)
    if not can_manage(user, g):
        raise PermissionError("只有建组人或港湾管理岗可以关闭讨论组")
    note = (note or "").strip()[:200]
    if closed:
        conn.execute("UPDATE talk_groups SET status='closed', pinned=0, closed_by=?, closed_at=?, close_note=? WHERE id=?",
                     (user["id"], now_iso(), note, gid))
    else:
        conn.execute("UPDATE talk_groups SET status='open', closed_by=NULL, closed_at=NULL, close_note='' WHERE id=?", (gid,))
    audit(conn, user["id"], "talk.group." + ("close" if closed else "reopen"), f"talk:{gid}", note[:100])
    if closed and g["author_id"] != user["id"]:
        word = "已解决" if g["kind"] == "issue" else "已关闭"
        _notify(conn, user["project_id"], g["author_id"], f"{user['name']} 把讨论组“{g['title']}”标为{word}", note or g["title"], gid, set())
    return group_view(conn, user, _group(conn, user, gid))


def set_pinned(conn, user: dict, gid: int, pinned: bool) -> dict:
    _need_write(user)
    if not can_publish(user):
        raise PermissionError("只有港湾管理岗（项目经理、工程主管、安全主管）可以置顶")
    g = _group(conn, user, gid)
    if pinned and g["status"] != "open":
        raise ValueError("已关闭的讨论组不能置顶")
    conn.execute("UPDATE talk_groups SET pinned=? WHERE id=?", (1 if pinned else 0, gid))
    audit(conn, user["id"], "talk.group.pin", f"talk:{gid}", "1" if pinned else "0")
    return group_view(conn, user, _group(conn, user, gid))


def _author(conn, uid: int) -> dict:
    u = one(conn, "SELECT id, name, role, company FROM users WHERE id=?", (uid,)) or {"id": uid, "name": "已停用", "role": "", "company": ""}
    return {"id": u["id"], "name": u["name"], "role_name": R.role_name(u["role"]), "company": u["company"] or ""}


def group_view(conn, user: dict, g: dict) -> dict:
    n = one(conn, "SELECT COUNT(*) n, COUNT(DISTINCT user_id) m FROM talk_posts WHERE group_id=?", (g["id"],))
    closer = one(conn, "SELECT name FROM users WHERE id=?", (g["closed_by"],)) if g["closed_by"] else None
    return {"id": g["id"], "kind": g["kind"], "kind_name": KINDS.get(g["kind"], g["kind"]), "title": g["title"], "intro": g["intro"],
            "part": g["part"], "activity": {"code": g["activity_code"], "label": _act_label(conn, user, g["activity_code"])} if g["activity_code"] else None,
            "author": _author(conn, g["author_id"]), "status": g["status"], "pinned": bool(g["pinned"]),
            "closed": {"by": closer["name"] if closer else "", "at": g["closed_at"], "note": g["close_note"]} if g["status"] != "open" else None,
            "posts": n["n"], "people": n["m"], "created_at": g["created_at"], "last_at": g["last_at"],
            "can_manage": can_write(user) and can_manage(user, g), "can_pin": can_write(user) and can_publish(user)}


def list_groups(conn, user: dict, kind: str = "", status: str = "open", q: str = "") -> dict:
    """置顶在前，其余按最近活跃倒序；status：open 进行中 / closed 已关闭 / all。"""
    if not can_view(user):
        raise PermissionError("系统管理员不参与现场动态")
    sql, a = "SELECT * FROM talk_groups WHERE project_id=?", [user["project_id"]]
    if kind in KINDS:
        sql, a = sql + " AND kind=?", a + [kind]
    if status in ("open", "closed"):
        sql, a = sql + " AND status=?", a + [status]
    if q.strip():
        like = f"%{q.strip()}%"
        sql, a = sql + " AND (title LIKE ? OR intro LIKE ? OR part LIKE ?)", a + [like, like, like]
    rows = all_(conn, sql + " ORDER BY pinned DESC, last_at DESC, id DESC LIMIT 200", a)
    counts = {r["status"]: r["n"] for r in all_(conn, "SELECT status, COUNT(*) n FROM talk_groups WHERE project_id=? GROUP BY status", (user["project_id"],))}
    return {"items": [group_view(conn, user, g) for g in rows], "counts": {"open": counts.get("open", 0), "closed": counts.get("closed", 0)},
            "can_write": can_write(user)}


# ============================================================================ 帖子

def _photos(conn, user: dict, ids: list[int]) -> list[int]:
    ids = [int(i) for i in ids or []][:MAX_PHOTOS]
    for mid in ids:
        m = one(conn, "SELECT uploader_id FROM media WHERE id=? AND project_id=?", (mid, user["project_id"]))
        if not m or m["uploader_id"] != user["id"]:
            raise ValueError("只能附自己上传的照片")
    return ids


def add_post(conn, user: dict, gid: int, text: str, media_ids: list[int] | None = None) -> dict:
    _need_write(user)
    g = _open_group(conn, user, gid)
    text = (text or "").strip()
    ids = _photos(conn, user, media_ids or [])
    if not text and not ids:
        raise ValueError("写点内容或附张照片")
    if len(text) > MAX_POST:
        raise ValueError(f"帖子最多 {MAX_POST} 字")
    pid = insert(conn, "talk_posts", {"project_id": user["project_id"], "group_id": gid, "user_id": user["id"], "text": text,
                                      "media_json": dumps(ids), "created_at": now_iso()})
    _touch(conn, gid)
    skip = {user["id"]}
    _notify(conn, user["project_id"], g["author_id"], f"{user['name']} 在“{g['title']}”里发了帖", text or "[照片]", gid, skip)
    from . import social
    for m in social.mentioned(conn, user, text):
        _notify(conn, user["project_id"], m["id"], f"{user['name']} 在讨论组“{g['title']}”提到了你", text, gid, skip)
    return post_view(conn, user, one(conn, "SELECT * FROM talk_posts WHERE id=?", (pid,)))


def delete_post(conn, user: dict, post_id: int) -> None:
    p = one(conn, "SELECT * FROM talk_posts WHERE id=? AND project_id=?", (post_id, user["project_id"]))
    if not p:
        raise ValueError("帖子不存在")
    if p["user_id"] != user["id"]:
        raise PermissionError("只能删除自己的帖子")
    conn.execute("DELETE FROM talk_comments WHERE post_id=?", (post_id,))
    conn.execute("DELETE FROM talk_likes WHERE post_id=?", (post_id,))
    conn.execute("DELETE FROM talk_posts WHERE id=?", (post_id,))


def _likes(conn, user: dict, post_id: int) -> dict:
    rows = all_(conn, "SELECT l.user_id, u.name FROM talk_likes l JOIN users u ON u.id=l.user_id WHERE l.post_id=? ORDER BY l.created_at", (post_id,))
    return {"n": len(rows), "me": any(r["user_id"] == user["id"] for r in rows), "names": [r["name"] for r in rows[:20]]}


def post_view(conn, user: dict, p: dict) -> dict:
    cms = all_(conn, """SELECT c.*, u.name, u.role, r.name reply_name FROM talk_comments c JOIN users u ON u.id=c.user_id
                        LEFT JOIN talk_comments rc ON rc.id=c.reply_to LEFT JOIN users r ON r.id=rc.user_id
                        WHERE c.post_id=? ORDER BY c.id""", (p["id"],))
    a = _author(conn, p["user_id"])
    return {"id": p["id"], "author": {**a, "mine": p["user_id"] == user["id"]}, "text": p["text"], "time": p["created_at"],
            "photos": [{"id": m, "thumb": f"/api/media/{m}/thumb", "url": f"/api/media/{m}/file"} for m in loads(p["media_json"], [])],
            "likes": _likes(conn, user, p["id"]),
            "comments": [{"id": c["id"], "name": c["name"], "role_name": R.role_name(c["role"]), "text": c["text"], "time": c["created_at"],
                          "mine": c["user_id"] == user["id"], "reply_to": {"id": c["reply_to"], "name": c["reply_name"]} if c["reply_to"] else None}
                         for c in cms]}


def detail(conn, user: dict, gid: int, before: int | None = None, limit: int = 20) -> dict:
    if not can_view(user):
        raise PermissionError("系统管理员不参与现场动态")
    g = _group(conn, user, gid)
    sql, a = "SELECT * FROM talk_posts WHERE group_id=?", [gid]
    if before:
        sql, a = sql + " AND id<?", a + [before]
    rows = all_(conn, sql + " ORDER BY id DESC LIMIT ?", a + [limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    return {"group": group_view(conn, user, g), "posts": [post_view(conn, user, p) for p in rows],
            "next": rows[-1]["id"] if more and rows else None, "can_write": can_write(user) and g["status"] == "open"}


def _post(conn, user: dict, post_id: int) -> tuple[dict, dict]:
    p = one(conn, "SELECT * FROM talk_posts WHERE id=? AND project_id=?", (post_id, user["project_id"]))
    if not p:
        raise ValueError("帖子不存在")
    return p, _group(conn, user, p["group_id"])


def toggle_like(conn, user: dict, post_id: int) -> dict:
    _need_write(user)
    p, _g = _post(conn, user, post_id)
    if one(conn, "SELECT 1 FROM talk_likes WHERE post_id=? AND user_id=?", (post_id, user["id"])):
        conn.execute("DELETE FROM talk_likes WHERE post_id=? AND user_id=?", (post_id, user["id"]))
    else:
        insert(conn, "talk_likes", {"post_id": post_id, "user_id": user["id"], "created_at": now_iso()})
    return _likes(conn, user, post_id)


def add_comment(conn, user: dict, post_id: int, text: str, reply_to: int | None = None) -> dict:
    _need_write(user)
    p, g = _post(conn, user, post_id)
    if g["status"] != "open":
        raise ValueError("讨论组已关闭，不能再发言")
    text = (text or "").strip()
    if not text:
        raise ValueError("评论不能为空")
    if len(text) > MAX_COMMENT:
        raise ValueError(f"评论最多 {MAX_COMMENT} 字")
    parent = None
    if reply_to:
        parent = one(conn, "SELECT id, user_id FROM talk_comments WHERE id=? AND post_id=?", (reply_to, post_id))
        if not parent:
            raise ValueError("要回复的评论不存在")
    insert(conn, "talk_comments", {"project_id": user["project_id"], "post_id": post_id, "user_id": user["id"], "text": text,
                                   "reply_to": parent["id"] if parent else None, "created_at": now_iso()})
    _touch(conn, g["id"])
    skip = {user["id"]}
    _notify(conn, user["project_id"], parent["user_id"] if parent else None, f"{user['name']} 回复了你的讨论", text, g["id"], skip)
    from . import social
    for m in social.mentioned(conn, user, text):
        _notify(conn, user["project_id"], m["id"], f"{user['name']} 在讨论组“{g['title']}”提到了你", text, g["id"], skip)
    _notify(conn, user["project_id"], p["user_id"], f"{user['name']} 评论了你在“{g['title']}”的帖子", text, g["id"], skip)
    return post_view(conn, user, p)


def delete_comment(conn, user: dict, cid: int) -> dict:
    c = one(conn, "SELECT * FROM talk_comments WHERE id=? AND project_id=?", (cid, user["project_id"]))
    if not c:
        raise ValueError("评论不存在")
    if c["user_id"] != user["id"]:
        raise PermissionError("只能删除自己的评论")
    conn.execute("UPDATE talk_comments SET reply_to=NULL WHERE reply_to=?", (cid,))
    conn.execute("DELETE FROM talk_comments WHERE id=?", (cid,))
    p, _g = _post(conn, user, c["post_id"])
    return post_view(conn, user, p)


def media_in_talk(conn, project_id: int, media_id: int) -> bool:
    for r in all_(conn, "SELECT media_json FROM talk_posts WHERE project_id=? AND media_json LIKE ?", (project_id, f"%{media_id}%")):
        if media_id in loads(r["media_json"], []):
            return True
    return False
