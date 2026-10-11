"""现场圈：把现场的各类发布（随手记、随手拍隐患、整改照片、日志/周确认）按时间汇成一条动态流，可点赞、评论。

动态本身不另存一份：直接从既有记录汇总，所以现场记录改了（如删除随手记）动态随之消失；
只有点赞与评论是新增数据，用 `kind:id` 形式的动态键关联。只读汇总，不改变进度、审批、计量语义。
现场圈全项目可见（RJ 10-09：“现场圈大家都能看”）：分包单位也看到其他单位与港湾的动态；只有按单位发布的公告、仅港湾可见的 Agent 回答仍按原范围。
scope_ids（本单位范围）仍给部位档案等非现场圈功能使用。

现场圈 Agent 化新增港湾公告（notice，含置顶与已读回执），见 circle_board.py；Agent 从真实动态里挑的“AI精选”存于 circle_picks，见 circle_picks.py，读取时按动态键附上；
动态下的 Agent 同事点评与 @工笔 问答存于 feed_agent_notes，见 feed_agents.py，读取时按动态键附上。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from .. import roles as R
from ..db import all_, audit, dumps, insert, loads, now_iso, one
from . import circle_board as board
from .media import geo_of


KINDS = ("note", "snap", "rect", "log", "notice", "repost")
MAX_COMMENT = 300


# ----------------------------------------------------------------------------- 可见范围

def scope_ids(conn, user: dict) -> list[int] | None:
    """分包单位：同单位的技术员与安全员；其他角色返回 None（不限）。"""
    if not R.is_sub(user["role"]):
        return None
    rows = all_(conn, "SELECT id FROM users WHERE project_id=? AND company=? AND role IN ('tech','sub_safety')",
                (user["project_id"], user.get("company") or ""))
    return [r["id"] for r in rows] or [user["id"]]


def can_see_author(conn, user: dict, uid: int | None) -> bool:
    """现场圈里能否看到这个人发布的动态：全项目可见（调用方已按 project_id 取记录）。"""
    return uid is not None


def media_in_circle(conn, user: dict, m: dict) -> bool:
    """这张照片是否出现在现场圈里（随手拍、整改照片、随手记附图、讨论组帖子）：分包单位可看现场圈里的照片，其他照片仍按本单位范围。"""
    if m["purpose"] in ("safety", "rectify"):
        return True
    for n in all_(conn, "SELECT media_json FROM notes WHERE project_id=? AND media_json LIKE ?", (user["project_id"], f"%{m['id']}%")):
        if m["id"] in loads(n["media_json"], []):
            return True
    from . import talk   # 现场圈讨论组帖子里的照片（10-09）
    return talk.media_in_talk(conn, user["project_id"], m["id"])


def _in(ids: list[int]) -> str:
    return ",".join("?" for _ in ids) or "NULL"


# ----------------------------------------------------------------------------- 取各类动态

def _first_geo(rows: list[dict]) -> dict | None:
    """一组照片取第一张带坐标的；都没有坐标时取带地点文字的。"""
    geos = [g for g in (geo_of(r) for r in rows) if g]
    return next((g for g in geos if g["lat"] is not None), geos[0] if geos else None)


def _note_geo(conn, n: dict) -> dict | None:
    """随手记自带的定位；旧记录没有时，沿用其照片的定位。"""
    g = geo_of(n)
    if g and g["lat"] is not None:
        return g
    ids = [int(i) for i in loads(n["media_json"], [])]
    if ids:
        ms = all_(conn, f"SELECT geo_lat, geo_lon, geo_acc, geo_place FROM media WHERE id IN ({_in(ids)})", ids)
        return _first_geo(ms) or g
    return g


GEO_BOX = 0.002  # 约 200 米：没有地点文字、只有坐标的动态，按这个范围算“同一地点”


def _geo_sql(geo: dict | None) -> tuple[str, list]:
    """同地点筛选：地点文字相同，或坐标在同一小范围内。"""
    if not geo:
        return "", []
    conds, p = [], []
    if geo.get("place"):
        conds.append("geo_place=?")
        p.append(geo["place"])
    if geo.get("lat") is not None and geo.get("lon") is not None:
        conds.append("(geo_lat BETWEEN ? AND ? AND geo_lon BETWEEN ? AND ?)")
        p += [geo["lat"] - GEO_BOX, geo["lat"] + GEO_BOX, geo["lon"] - GEO_BOX, geo["lon"] + GEO_BOX]
    return (" AND (" + " OR ".join(conds) + ")", p) if conds else (" AND 0", [])


def _note_raw(conn, n: dict) -> dict:
    return {"key": f"note:{n['id']}", "kind": "note", "author_id": n["author_id"], "time": n["created_at"], "text": n["text"],
            "media": loads(n["media_json"], []), "voice": n["source"] == "voice", "geo": _note_geo(conn, n)}


def _snap_raw(uid: int, ms: list[dict]) -> dict:
    ms = sorted(ms, key=lambda m: m["id"])
    return {"key": f"snap:{ms[0]['id']}", "kind": "snap", "author_id": uid, "time": max(m["uploaded_at"] for m in ms), "media": [m["id"] for m in ms],
            "safety": [m["safety_status"] for m in ms], "geo": _first_geo(ms)}


def _rect_raw(conn, user, m: dict) -> dict:
    f = one(conn, "SELECT * FROM findings WHERE rectify_media_id=? AND project_id=?", (m["id"], user["project_id"]))
    return {"key": f"rect:{m['id']}", "kind": "rect", "author_id": m["uploader_id"], "time": m["uploaded_at"], "media": [m["id"]],
            "finding": f, "geo": geo_of(m)}


def _log_raw(lg: dict) -> dict:
    c = loads(lg["content_json"], {})
    works = [str(w.get("desc") or w.get("process") or "").strip() for w in c.get("works", [])]
    return {"key": f"log:{lg['id']}", "kind": "log", "author_id": lg["author_id"], "time": lg["submitted_at"],
            "log": lg, "works": [w for w in works if w][:3], "work_total": len(works), "issues": len(c.get("issues", []))}


def notes_today_feed(conn, user: dict, before: str | None, limit: int) -> dict:
    q, p = "SELECT * FROM notes WHERE project_id=? AND note_date=?", [user["project_id"], date.today().isoformat()]
    if before:
        q += " AND created_at<?"
        p.append(before)
    rows = all_(conn, q + " ORDER BY created_at DESC LIMIT ?", p + [limit + 1])
    more = len(rows) > limit
    raws = [_note_raw(conn, n) for n in rows[:limit]]
    total = one(conn, "SELECT COUNT(*) n FROM notes WHERE project_id=? AND note_date=?", (user["project_id"], date.today().isoformat()))["n"]
    out = _assemble(conn, user, raws, raws[-1]["time"] if more and raws else None)
    return {**out, "scope": f"今日随手记 {total} 条 · 全项目", "total": total}


def _notes(conn, user, ids, before, limit, geo=None) -> list[dict]:
    q, p = "SELECT * FROM notes WHERE project_id=?", [user["project_id"]]
    if ids is not None:
        q += f" AND author_id IN ({_in(ids)})"
        p += ids
    if before:
        q += " AND created_at<?"
        p.append(before)
    gq, gp = _geo_sql(geo)
    rows = all_(conn, q + gq + " ORDER BY created_at DESC LIMIT ?", p + gp + [limit])
    return [_note_raw(conn, n) for n in rows]


def _snaps(conn, user, ids, before, limit, geo=None) -> list[dict]:
    """随手拍：同一人同一分钟上传的归为一条，组内取最小照片 id 作键，后续补传不改变键。"""
    q, p = "SELECT * FROM media WHERE project_id=? AND purpose='safety'", [user["project_id"]]
    if ids is not None:
        q += f" AND uploader_id IN ({_in(ids)})"
        p += ids
    if before:
        q += " AND substr(uploaded_at,1,16)<=substr(?,1,16)"
        p.append(before)
    gq, gp = _geo_sql(geo)
    rows = all_(conn, q + gq + " ORDER BY uploaded_at DESC, id DESC LIMIT ?", p + gp + [limit * 8])
    groups: dict[tuple, list[dict]] = {}
    for m in rows:
        groups.setdefault((m["uploader_id"], m["uploaded_at"][:16]), []).append(m)
    out = []
    for (uid, _), ms in groups.items():
        t = max(m["uploaded_at"] for m in ms)
        if before and t >= before:
            continue
        out.append(_snap_raw(uid, ms))
    return sorted(out, key=lambda x: x["time"], reverse=True)[:limit]


def _rects(conn, user, ids, before, limit, geo=None) -> list[dict]:
    q, p = "SELECT * FROM media WHERE project_id=? AND purpose='rectify'", [user["project_id"]]
    if ids is not None:
        q += f" AND uploader_id IN ({_in(ids)})"
        p += ids
    if before:
        q += " AND uploaded_at<?"
        p.append(before)
    gq, gp = _geo_sql(geo)
    rows = all_(conn, q + gq + " ORDER BY uploaded_at DESC LIMIT ?", p + gp + [limit])
    return [_rect_raw(conn, user, m) for m in rows]


def _logs(conn, user, ids, before, limit) -> list[dict]:
    """周确认，以及手动提交的日报；每日日志随周确认提交，不重复上墙。"""
    q = ("SELECT * FROM logs WHERE project_id=? AND mode='ai' AND submitted_at IS NOT NULL AND status IN ('submitted','approved','returned') "
         "AND (period='week' OR auto=0)")
    p = [user["project_id"]]
    if ids is not None:
        q += f" AND author_id IN ({_in(ids)})"
        p += ids
    if before:
        q += " AND submitted_at<?"
        p.append(before)
    rows = all_(conn, q + " ORDER BY submitted_at DESC LIMIT ?", p + [limit])
    return [_log_raw(lg) for lg in rows]


def _reposts(conn, user, ids, before, limit) -> list[dict]:
    """项目内转发：一条带转发语的新动态，里面嵌原动态卡片；谁看得到原文仍由原文可见范围决定。"""
    q, p = "SELECT * FROM feed_reposts WHERE project_id=?", [user["project_id"]]
    if ids is not None:
        q += f" AND user_id IN ({_in(ids)})"
        p += ids
    if before:
        q += " AND created_at<?"
        p.append(before)
    return [_repost_raw(r) for r in all_(conn, q + " ORDER BY created_at DESC LIMIT ?", p + [limit])]


def _repost_raw(r: dict) -> dict:
    return {"key": f"repost:{r['id']}", "kind": "repost", "author_id": r["user_id"], "time": r["created_at"], "text": r["text"], "orig_key": r["orig_key"]}


def raw_by_key(conn, user: dict, key: str) -> dict | None:
    """按动态键取一条动态的原始记录（可见性由调用方先用 _author_of 校验）。收藏、阅读模式、转发卡片共用。"""
    kind, _, sid = key.partition(":")
    if kind not in KINDS or not sid.isdigit():
        return None
    rid, pid = int(sid), user["project_id"]
    if kind == "note":
        n = one(conn, "SELECT * FROM notes WHERE id=? AND project_id=?", (rid, pid))
        return _note_raw(conn, n) if n else None
    if kind == "snap":
        m = one(conn, "SELECT * FROM media WHERE id=? AND project_id=? AND purpose='safety'", (rid, pid))
        if not m:
            return None
        ms = all_(conn, "SELECT * FROM media WHERE project_id=? AND purpose='safety' AND uploader_id=? AND substr(uploaded_at,1,16)=substr(?,1,16) ORDER BY id",
                  (pid, m["uploader_id"], m["uploaded_at"]))
        raw = _snap_raw(m["uploader_id"], ms or [m])
        raw["key"] = key
        return raw
    if kind == "rect":
        m = one(conn, "SELECT * FROM media WHERE id=? AND project_id=? AND purpose='rectify'", (rid, pid))
        return _rect_raw(conn, user, m) if m else None
    if kind == "log":
        lg = one(conn, "SELECT * FROM logs WHERE id=? AND project_id=? AND submitted_at IS NOT NULL", (rid, pid))
        return _log_raw(lg) if lg else None
    if kind == "notice":
        n = one(conn, "SELECT * FROM circle_notices WHERE id=? AND project_id=? AND status='active'", (rid, pid))
        return {"key": key, "kind": "notice", "author_id": n["author_id"], "time": n["created_at"], "notice": n, "pinned": bool(n["pinned"])} if n else None
    r = one(conn, "SELECT * FROM feed_reposts WHERE id=? AND project_id=?", (rid, pid))
    return _repost_raw(r) if r else None


# ----------------------------------------------------------------------------- 组装

def _media_view(mid: int) -> dict:
    return {"id": mid, "thumb": f"/api/media/{mid}/thumb", "url": f"/api/media/{mid}/file"}


def _post(conn, user, raw: dict, people: dict[int, dict], hazards: dict[int, list[dict]]) -> dict:
    a = people.get(raw["author_id"], {})
    author = {"id": raw["author_id"], "name": a.get("name", "已离职"), "role_name": R.role_name(a.get("role", "")),
              "company": a.get("company") or "", "mine": raw["author_id"] == user["id"]}
    base = {"key": raw["key"], "kind": raw["kind"], "time": raw["time"], "author": author,
            "text": "", "photos": [_media_view(m) for m in raw.get("media", [])], "tag": None, "ai": [], "link": None,
            "geo": raw.get("geo"), "geo_slot": raw["kind"] in ("note", "snap", "rect")}
    k = raw["kind"]
    if k == "note":
        base["text"] = raw["text"]
        base["tag"] = "口述记录" if raw["voice"] else "随手记"
    elif k == "snap":
        base["tag"] = "随手拍隐患"
        hz = [h for mid in raw["media"] for h in hazards.get(mid, [])]
        st = raw["safety"]
        if hz:
            base["ai"] = [{"id": h["id"], "level": h["level"], "title": h["title"], "status": h["status"],
                           "candidate": h["confirmed_at"] is None and h["status"] == "open"} for h in hz]  # 候选：AI 识别、待安全员确认
            base["text"] = f"拍了 {len(raw['media'])} 张现场照片，安全Agent 识别出 {len(hz)} 处隐患。"
        elif any(s in ("pending",) for s in st):
            base["text"] = f"拍了 {len(raw['media'])} 张现场照片，安全Agent 识别中…"
        elif any(s == "failed" for s in st):
            base["text"] = f"拍了 {len(raw['media'])} 张现场照片，安全Agent 识别失败，待重试。"
        elif st and all(s == "skipped" for s in st):
            base["text"] = f"拍了 {len(raw['media'])} 张现场照片，拍摄者选择不做隐患识别。"
        else:
            base["text"] = f"拍了 {len(raw['media'])} 张现场照片，安全Agent 未发现明显隐患。"
    elif k == "rect":
        base["tag"] = "整改"
        f = raw["finding"]
        if f:
            before = f["ref_id"] if f["ref_type"] == "media" else None
            if before:
                base["photos"] = [_media_view(before)] + base["photos"]
                base["compare"] = True
            base["text"] = f"提交了整改照片：{f['title']}"
            base["ai"] = [{"id": f["id"], "level": f["level"], "title": f["title"], "status": f["status"], "reason": _verify_line(f)}]
        else:
            base["text"] = "提交了一张整改照片。"
    elif k == "log":
        lg = raw["log"]
        week = lg["period"] == "week"
        base["tag"] = "周进度确认" if week else "施工日志"
        span = f"{lg['log_date']} ~ {lg['period_end']}" if week and lg["period_end"] else lg["log_date"]
        head = f"提交了{'本周进度确认' if week else '施工日志'}（{span}）"
        body = "；".join(w.strip().rstrip("。；;") for w in raw["works"] if w and w.strip())   # 每条去掉句末标点再连，避免“。；”（D5）
        base["text"] = head + (f"\n{body}" + (f" 等 {raw['work_total']} 项" if raw["work_total"] > len(raw["works"]) else "") if body else "")
        if raw["issues"]:
            base["text"] += f"\n记录了 {raw['issues']} 个问题。"
        # 技术员只能打开自己的日志；安全员不看施工日志；港湾岗位可看
        if user["role"] != "sub_safety" and (not R.is_sub(user["role"]) or lg["author_id"] == user["id"]):
            base["link"] = {"route": "log", "params": {"id": lg["id"]}}
    elif k == "notice":
        base["tag"] = "置顶公告" if raw.get("pinned") else "公告"
        base["notice"] = board.notice_view(conn, user, raw["notice"])
        base["pinned"] = bool(raw.get("pinned"))
    elif k == "repost":
        base["tag"] = "转发"
        base["text"] = raw["text"]
        base["orig"] = _orig_card(conn, user, raw["orig_key"], people)
    return base


def _verify_line(f: dict) -> str:
    """整改复核一行（4.4 契约）：AI 结论只是建议，闭环前写“AI 建议：……，待安全主管确认”，模型给的理由附在后面。"""
    from .site_agents import verify_label
    v = loads(f["verify_json"], {}) or {}
    label = verify_label(v, f["status"])
    reason = (v.get("reason") or "").strip() if label.startswith("AI 建议") else ""
    return label + (f"；{reason}" if reason else "")


def pick_visible(user: dict, reason: str | None) -> bool:
    """AI精选理由对项目经理先过简报的范围守卫：库里已有的旧理由（如“数据审核低风险”）也拦住，不展示这条精选。"""
    if user["role"] not in R.PM_SCOPE:
        return True
    from .brief import scope_ok
    return scope_ok(reason or "")


def _hazard_map(conn, user, raws: list[dict]) -> dict[int, list[dict]]:
    snap_media = [m for r in raws if r["kind"] == "snap" for m in r["media"]]
    hazards: dict[int, list[dict]] = defaultdict(list)
    if snap_media:
        for f in all_(conn, f"SELECT id, level, title, status, ref_id, confirmed_at FROM findings WHERE project_id=? AND ref_type='media' AND agent='safety' "
                            f"AND ref_id IN ({_in(snap_media)}) ORDER BY id", [user["project_id"], *snap_media]):
            hazards[f["ref_id"]].append(f)
    return hazards


def _orig_card(conn, user, key: str, people: dict[int, dict]) -> dict:
    """转发里嵌的原动态：当前用户看不到（范围外或已删除）时只给占位，不泄露内容。"""
    try:
        _author_of(conn, user, key)
        raw = raw_by_key(conn, user, key)
    except ValueError:
        raw = None
    if not raw:
        return {"key": key, "hidden": True}
    p = _post(conn, user, raw, people, _hazard_map(conn, user, [raw]))
    text = p["text"] or (p.get("notice") or {}).get("title") or ""
    return {"key": key, "hidden": False, "kind": p["kind"], "tag": p["tag"], "author": p["author"], "time": p["time"], "text": text[:200],
            "photos": p["photos"][:3], "geo": p.get("geo"), "geo_slot": p.get("geo_slot")}


def _attach_social(conn, user, posts: list[dict]) -> None:
    keys = [p["key"] for p in posts]
    if not keys:
        return
    marks = ",".join("?" for _ in keys)
    likes: dict[str, list[dict]] = defaultdict(list)
    for r in all_(conn, f"SELECT l.post_key, l.user_id, u.name FROM feed_likes l JOIN users u ON u.id=l.user_id "
                        f"WHERE l.project_id=? AND l.post_key IN ({marks}) ORDER BY l.created_at", [user["project_id"], *keys]):
        likes[r["post_key"]].append(r)
    comments: dict[str, list[dict]] = defaultdict(list)
    rows = all_(conn, f"SELECT c.id, c.post_key, c.user_id, c.text, c.created_at, c.reply_to, c.quote, c.featured, u.name, u.role, u.company FROM feed_comments c "
                      f"JOIN users u ON u.id=c.user_id WHERE c.project_id=? AND c.post_key IN ({marks}) ORDER BY c.id", [user["project_id"], *keys])
    who = {r["id"]: r["name"] for r in rows}
    for r in rows:
        comments[r["post_key"]].append({"id": r["id"], "name": r["name"], "role_name": R.role_name(r["role"]), "text": r["text"],
                                        "time": r["created_at"], "mine": r["user_id"] == user["id"], "quote": r["quote"] or "",
                                        "featured": bool(r["featured"]), "reply_to": {"id": r["reply_to"], "name": who.get(r["reply_to"], "已删除的评论")} if r["reply_to"] else None})
    # Agent 同事的点评与 @工笔 回答并入同一评论区（带 agent 标记），同秒时人的评论在前
    from . import feed_agents
    for key, cs in feed_agents.attach(conn, user, keys).items():
        comments[key] = sorted(comments.get(key, []) + cs, key=lambda c: (c["time"], 1 if c.get("agent") else 0))
    from . import social
    favs, reposts = social.fav_keys(conn, user, keys), social.repost_counts(conn, user, keys)
    picks = {r["post_key"]: r for r in all_(conn, f"SELECT post_key, reason, source, created_at FROM circle_picks WHERE project_id=? AND post_key IN ({marks})",
                                            [user["project_id"], *keys])}
    for p in posts:
        pk = picks.get(p["key"])
        pk = pk if pk and pick_visible(user, pk["reason"]) else None
        p["ai_pick"] = {"reason": pk["reason"], "source": pk["source"], "time": pk["created_at"]} if pk else None
        p["fav"] = {"me": p["key"] in favs}
        p["reposts"] = reposts.get(p["key"], 0)
        ls = likes.get(p["key"], [])
        p["likes"] = {"n": len(ls), "me": any(x["user_id"] == user["id"] for x in ls), "names": [x["name"] for x in ls][:8]}
        # 精选评论排在最前，其余保持时间顺序
        p["comments"] = sorted(comments.get(p["key"], []), key=lambda c: 0 if c.get("featured") else 1)
        p["article"] = p["kind"] in ("log", "notice") or (p["kind"] == "note" and len(p.get("text") or "") > 80)


def feed(conn, user: dict, before: str | None = None, limit: int = 20, mine: bool = False, view: str = "", author: int | None = None,
         geo: dict | None = None) -> dict:
    """view：""全部 / "follow" 只看我关注的人 / "fav" 我的收藏 / "ai" AI精选；author 看某人的动态（主页）；geo 同地点动态。"""
    limit = max(1, min(50, limit))
    if view == "fav":
        return fav_feed(conn, user, before, limit)
    if view == "ai":
        return pick_feed(conn, user, before, limit)
    if view == "notes_today":   # 首页“今日随手记 N · 全项目”点进来：只列今天的随手记，条数与首页同源（验收 I5）
        return notes_today_feed(conn, user, before, limit)
    ids = None   # 现场圈全项目可见
    if mine:
        ids = [user["id"]]
    elif author:
        ids = [author] if ids is None or author in ids else []
    elif view == "follow":
        from . import social
        f = social.following_ids(conn, user)
        ids = f if ids is None else [i for i in f if i in ids]
    narrowed = mine or bool(author) or view == "follow"
    lim = limit + 1  # 每类多取一条，才能判断后面是否还有更早的动态（只有一类动态时也能翻页）
    raws = _notes(conn, user, ids, before, lim, geo) + _snaps(conn, user, ids, before, lim, geo) + _rects(conn, user, ids, before, lim, geo)
    pinned: list[dict] = []
    if not geo:  # 同地点视图只看有定位的现场发布，不混入日志、公告、转发
        raws += _logs(conn, user, ids, before, lim) + _reposts(conn, user, ids, before, lim)
        notices, pinned = board.notice_raws(conn, user, before, lim, mine)
        if narrowed and not mine:  # 关注/主页视图：公告只留被关注/被查看人发布的
            notices = [n for n in notices + pinned if ids is not None and n["author_id"] in ids]
            pinned = []
        raws += notices
    raws.sort(key=lambda x: x["time"], reverse=True)
    more = len(raws) > limit
    raws = raws[:limit]
    next_cursor = raws[-1]["time"] if more and raws else None
    raws = pinned + raws  # 置顶公告固定在第一页最上方
    return _assemble(conn, user, raws, next_cursor)


def _assemble(conn, user, raws: list[dict], next_cursor: str | None) -> dict:
    people = {u["id"]: u for u in all_(conn, "SELECT id, name, role, company FROM users WHERE project_id=?", (user["project_id"],))}
    hazards = _hazard_map(conn, user, raws)
    posts = [_post(conn, user, r, people, hazards) for r in raws]
    _attach_social(conn, user, posts)
    return {"posts": posts, "next": next_cursor,
            "scope": "全项目"}


def fav_feed(conn, user: dict, before: str | None, limit: int) -> dict:
    """我的收藏：按收藏时间倒序；原文已不可见（被删、被撤回）的不显示。"""
    q, p = "SELECT post_key, created_at FROM feed_favs WHERE project_id=? AND user_id=?", [user["project_id"], user["id"]]
    if before:
        q += " AND created_at<?"
        p.append(before)
    rows = all_(conn, q + " ORDER BY created_at DESC LIMIT ?", p + [limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    raws = []
    for r in rows:
        try:
            _author_of(conn, user, r["post_key"])
            raw = raw_by_key(conn, user, r["post_key"])
        except ValueError:
            continue
        if raw:
            raws.append({**raw, "faved_at": r["created_at"]})
    out = _assemble(conn, user, raws, rows[-1]["created_at"] if more and rows else None)
    for p_, r_ in zip(out["posts"], raws):
        p_["faved_at"] = r_["faved_at"]
    return out


def pick_feed(conn, user: dict, before: str | None, limit: int) -> dict:
    """AI精选：项目级精选记录，全项目可见（项目经理另过简报范围守卫）；按精选先后倒序，游标为精选记录 id。"""
    cursor = int(before) if before and before.isdigit() else None
    raws: list[dict] = []
    last_id: dict[str, int] = {}
    more = False
    while True:
        q, p = "SELECT id, post_key, reason FROM circle_picks WHERE project_id=?", [user["project_id"]]
        if cursor is not None:
            q += " AND id<?"
            p.append(cursor)
        rows = all_(conn, q + " ORDER BY id DESC LIMIT 100", p)
        for r in rows:
            cursor = r["id"]
            if not pick_visible(user, r["reason"]):
                continue
            try:
                _author_of(conn, user, r["post_key"])
                raw = raw_by_key(conn, user, r["post_key"])
            except ValueError:
                continue
            if not raw:
                continue
            if len(raws) == limit:
                more = True
                break
            raws.append(raw)
            last_id[raw["key"]] = r["id"]
        if more or len(rows) < 100:
            break
    return _assemble(conn, user, raws, str(last_id[raws[-1]["key"]]) if more and raws else None)


# ----------------------------------------------------------------------------- 点赞与评论

def _author_of(conn, user: dict, key: str) -> int | None:
    """校验动态存在且当前用户看得到，返回发布人 id。"""
    kind, _, raw_id = key.partition(":")
    if kind not in KINDS or not raw_id.isdigit():
        raise ValueError("动态不存在")
    rid, pid = int(raw_id), user["project_id"]
    if kind == "notice":
        return board.can_see_notice_id(conn, user, rid)
    if kind == "repost":
        row = one(conn, "SELECT user_id a FROM feed_reposts WHERE id=? AND project_id=?", (rid, pid))
        if not row or not can_see_author(conn, user, row["a"]):
            raise ValueError("动态不存在")
        return row["a"]
    if kind == "note":
        row = one(conn, "SELECT author_id a FROM notes WHERE id=? AND project_id=?", (rid, pid))
    elif kind == "snap":
        row = one(conn, "SELECT uploader_id a FROM media WHERE id=? AND project_id=? AND purpose='safety'", (rid, pid))
    elif kind == "rect":
        row = one(conn, "SELECT uploader_id a FROM media WHERE id=? AND project_id=? AND purpose='rectify'", (rid, pid))
    else:
        row = one(conn, "SELECT author_id a FROM logs WHERE id=? AND project_id=? AND submitted_at IS NOT NULL", (rid, pid))
    if not row or not can_see_author(conn, user, row["a"]):
        raise ValueError("动态不存在")
    return row["a"]


def toggle_like(conn, user: dict, key: str) -> dict:
    _author_of(conn, user, key)
    ex = one(conn, "SELECT 1 x FROM feed_likes WHERE project_id=? AND post_key=? AND user_id=?", (user["project_id"], key, user["id"]))
    if ex:
        conn.execute("DELETE FROM feed_likes WHERE project_id=? AND post_key=? AND user_id=?", (user["project_id"], key, user["id"]))
    else:
        insert(conn, "feed_likes", {"project_id": user["project_id"], "post_key": key, "user_id": user["id"], "created_at": now_iso()})
    rows = all_(conn, "SELECT l.user_id, u.name FROM feed_likes l JOIN users u ON u.id=l.user_id WHERE l.project_id=? AND l.post_key=? ORDER BY l.created_at",
                (user["project_id"], key))
    return {"n": len(rows), "me": not ex, "names": [r["name"] for r in rows][:8]}


def circle_link(key: str) -> dict:
    """消息跳转目标：现场圈的某条动态（key 供现场圈定位；现场圈暂不按 key 定位时只到现场圈）。"""
    return {"route": "circle", "params": {"key": key}}


def add_comment(conn, user: dict, key: str, text: str, reply_to: int | None = None, quote: str = "") -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("评论不能为空")
    if len(text) > MAX_COMMENT:
        raise ValueError(f"评论最多 {MAX_COMMENT} 字")
    author = _author_of(conn, user, key)
    quote = (quote or "").strip()[:120]
    parent = None
    if reply_to:
        parent = one(conn, "SELECT c.id, c.user_id, u.name FROM feed_comments c JOIN users u ON u.id=c.user_id WHERE c.id=? AND c.project_id=? AND c.post_key=?",
                     (reply_to, user["project_id"], key))
        if not parent:
            raise ValueError("要回复的评论不存在")
    cid = insert(conn, "feed_comments", {"project_id": user["project_id"], "post_key": key, "user_id": user["id"], "text": text, "created_at": now_iso(),
                                         "reply_to": parent["id"] if parent else None, "quote": quote})
    notified: set[int] = {user["id"]}
    link = dumps(circle_link(key))

    def tell(uid: int | None, title: str) -> None:
        if uid and uid not in notified:
            notified.add(uid)
            insert(conn, "notifications", {"project_id": user["project_id"], "user_id": uid, "alert_id": None, "title": title,
                                           "body": text[:80], "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(), "link_json": link})
    # 被回复的人、被 @ 的人、动态发布人各收一条（点赞不打扰）
    tell(parent["user_id"] if parent else None, f"{user['name']} 回复了你的评论")
    from . import social
    for m in social.mentioned(conn, user, text):
        tell(m["id"], f"{user['name']} 在评论中提到了你")
    tell(author, f"{user['name']} 评论了你的现场动态")
    return {"id": cid, "name": user["name"], "role_name": R.role_name(user["role"]), "text": text, "time": now_iso(), "mine": True, "quote": quote,
            "featured": False, "reply_to": {"id": parent["id"], "name": parent["name"]} if parent else None}


def delete_comment(conn, user: dict, cid: int) -> None:
    c = one(conn, "SELECT * FROM feed_comments WHERE id=? AND project_id=?", (cid, user["project_id"]))
    if not c:
        raise ValueError("评论不存在")
    # 自己的评论可删；管理员可删任何评论（治理）
    if c["user_id"] != user["id"] and user["role"] != "admin":
        raise PermissionError("只能删除自己的评论")
    conn.execute("DELETE FROM feed_comments WHERE id=?", (cid,))
    if c["user_id"] != user["id"]:
        audit(conn, user["id"], "feed.comment.delete", f"comment:{cid}", c["text"][:100])
