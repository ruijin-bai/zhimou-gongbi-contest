"""现场圈社交层：收藏、关注/粉丝、项目内转发、私信，以及简报阅读（AI 总结、划重点、对句评论）。

边界（与产品定位一致）：
- 只是阅读与沟通层，不改变进度、审批、计量语义；AI 总结只复述该简报自身内容，数字被守卫校验，出现原文之外的数字即改用规则摘要；
- 可见范围沿用现场圈（10-09 起全项目可见）：主页、关注、收藏、转发全项目；私信与 @ 可选的人仍只到本单位同事与港湾岗位；
- 不做点赞或粉丝排名，没有任何按互动量排序的榜单；
- 私信只有会话双方能读：没有管理员查阅入口、不提供删除，也不提供撤回（现场沟通可能成为工期费用的证据）；
  私信照片单独存放（dm_files），不进入照片库，不被部位档案、证据包或安全识别读取。
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .. import roles as R
from ..ai.client import AIError, call_json
from ..config import settings
from ..db import all_, audit, dumps, insert, loads, now_iso, one
from . import circle_board as board
from . import feed as feed_svc

PROMPT_VERSION = "feed-summary-v1-20261006"
ARTICLE_KINDS = ("log", "notice", "note")
MAX_REPOST = 300
MAX_DM = 1000
MAX_QUOTE = 200
MAX_NOTE = 100
DM_EDGE, DM_THUMB = 1600, 400
DM_MAX_UPLOAD = 15 * 1024 * 1024


# ============================================================================ 开关

def enabled(conn, project_id: int) -> bool:
    """社交层（关注、收藏、转发、私信、划重点、精选评论）项目级开关，默认关闭，由系统管理员在“项目与规则”开启。
    点赞、评论（含回复与 @）、Agent 点评、@工笔问答、公告不受此开关影响。"""
    p = one(conn, "SELECT config_json FROM projects WHERE id=?", (project_id,))
    return bool((loads(p["config_json"], {}) if p else {}).get("social_enabled", False))


# ============================================================================ 人与可见范围

def can_write(user: dict) -> bool:
    return user["role"] not in ("admin", "leader")


def visible_peer(conn, user: dict, uid: int | None, dm: bool = False) -> dict | None:
    """当前用户能看到的同事（用于主页、关注、私信）。现场圈全项目可见；私信（dm=True）分包单位仍只到本单位同事与港湾岗位。"""
    if not uid:
        return None
    u = one(conn, "SELECT id, name, role, company, project_id, active FROM users WHERE id=? AND project_id=?", (uid, user["project_id"]))
    if not u or not u["active"] or u["role"] == "admin":
        return None
    if dm and R.is_sub(user["role"]) and R.is_sub(u["role"]) and (u.get("company") or "") != (user.get("company") or ""):
        return None
    return u


def _person(u: dict) -> dict:
    return {"id": u["id"], "name": u["name"], "role_name": R.role_name(u["role"]), "company": u.get("company") or ""}


def people(conn, user: dict) -> list[dict]:
    """@同事、发私信可选的人：可见的、能参与互动的同事（不含自己）。"""
    rows = all_(conn, "SELECT id, name, role, company FROM users WHERE project_id=? AND active=1 AND role NOT IN ('admin','leader') AND id<>? ORDER BY role, name",
                (user["project_id"], user["id"]))
    out = []
    for u in rows:
        if R.is_sub(user["role"]) and R.is_sub(u["role"]) and (u.get("company") or "") != (user.get("company") or ""):
            continue
        out.append(_person(u))
    return out


def _notify(conn, project_id: int, user_id: int, title: str, body: str = "", link: dict | None = None) -> None:
    insert(conn, "notifications", {"project_id": project_id, "user_id": user_id, "alert_id": None, "title": title, "body": body[:80],
                                   "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(), "link_json": dumps(link) if link else None})


def mentioned(conn, user: dict, text: str) -> list[dict]:
    """文字里 @姓名 提到的、当前用户可见的同事。"""
    hits = []
    for p in sorted(people(conn, user), key=lambda x: -len(x["name"])):
        if ("@" + p["name"]) in text and p not in hits:
            hits.append(p)
    return hits


def notify_mentions(conn, user: dict, text: str, where: str, skip: set[int] | None = None, link: dict | None = None) -> None:
    for p in mentioned(conn, user, text):
        if p["id"] != user["id"] and p["id"] not in (skip or set()):
            _notify(conn, user["project_id"], p["id"], f"{user['name']} 在{where}提到了你", text, link)


# ============================================================================ 收藏

def toggle_fav(conn, user: dict, key: str) -> dict:
    feed_svc._author_of(conn, user, key)
    pid = user["project_id"]
    ex = one(conn, "SELECT 1 x FROM feed_favs WHERE project_id=? AND post_key=? AND user_id=?", (pid, key, user["id"]))
    if ex:
        conn.execute("DELETE FROM feed_favs WHERE project_id=? AND post_key=? AND user_id=?", (pid, key, user["id"]))
    else:
        insert(conn, "feed_favs", {"project_id": pid, "post_key": key, "user_id": user["id"], "created_at": now_iso()})
    return {"key": key, "me": not ex}


def fav_keys(conn, user: dict, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    marks = ",".join("?" for _ in keys)
    return {r["post_key"] for r in all_(conn, f"SELECT post_key FROM feed_favs WHERE project_id=? AND user_id=? AND post_key IN ({marks})",
                                        [user["project_id"], user["id"], *keys])}


def repost_counts(conn, user: dict, keys: list[str]) -> dict[str, int]:
    if not keys:
        return {}
    marks = ",".join("?" for _ in keys)
    return {r["orig_key"]: r["n"] for r in all_(conn, f"SELECT orig_key, COUNT(*) n FROM feed_reposts WHERE project_id=? AND orig_key IN ({marks}) GROUP BY orig_key",
                                                  [user["project_id"], *keys])}


# ============================================================================ 关注 / 粉丝

def set_follow(conn, user: dict, uid: int, on: bool) -> dict:
    if uid == user["id"]:
        raise ValueError("不能关注自己")
    peer = visible_peer(conn, user, uid)
    if not peer or peer["role"] == "leader":
        raise ValueError("找不到这位同事")
    pid = user["project_id"]
    ex = one(conn, "SELECT 1 x FROM feed_follows WHERE project_id=? AND follower_id=? AND followee_id=?", (pid, user["id"], uid))
    if on and not ex:
        insert(conn, "feed_follows", {"project_id": pid, "follower_id": user["id"], "followee_id": uid, "created_at": now_iso()})
        _notify(conn, pid, uid, f"{user['name']} 关注了你")
    elif not on and ex:
        conn.execute("DELETE FROM feed_follows WHERE project_id=? AND follower_id=? AND followee_id=?", (pid, user["id"], uid))
    return follow_counts(conn, user, uid)


def following_ids(conn, user: dict) -> list[int]:
    return [r["followee_id"] for r in all_(conn, "SELECT followee_id FROM feed_follows WHERE project_id=? AND follower_id=?", (user["project_id"], user["id"]))]


def follow_list(conn, user: dict, uid: int, kind: str) -> list[dict]:
    """关注列表 / 粉丝列表，只含当前用户可见的同事。"""
    if not visible_peer(conn, user, uid) and uid != user["id"]:
        raise ValueError("找不到这位同事")
    a, b = ("follower_id", "followee_id") if kind == "followers" else ("followee_id", "follower_id")
    rows = all_(conn, f"SELECT {a} id FROM feed_follows WHERE project_id=? AND {b}=? ORDER BY created_at DESC", (user["project_id"], uid))
    out = []
    for r in rows:
        u = visible_peer(conn, user, r["id"])
        if u:
            out.append(_person(u))
    return out


def follow_counts(conn, user: dict, uid: int) -> dict:
    me = one(conn, "SELECT 1 x FROM feed_follows WHERE project_id=? AND follower_id=? AND followee_id=?", (user["project_id"], user["id"], uid))
    back = one(conn, "SELECT 1 x FROM feed_follows WHERE project_id=? AND follower_id=? AND followee_id=?", (user["project_id"], uid, user["id"]))
    return {"following": len(follow_list(conn, user, uid, "following")), "followers": len(follow_list(conn, user, uid, "followers")),
            "i_follow": bool(me), "follows_me": bool(back)}


def profile(conn, user: dict, uid: int) -> dict:
    """头像抽屉主页：岗位、关注/粉丝、去过的地点（取自照片与随手记的定位）、本周确认情况（仅港湾与本人可见）。"""
    u = visible_peer(conn, user, uid) or (one(conn, "SELECT id, name, role, company, project_id, active FROM users WHERE id=?", (uid,)) if uid == user["id"] else None)
    if not u:
        raise ValueError("找不到这位同事")
    pid = user["project_id"]
    out = {**_person(u), "mine": uid == user["id"], **follow_counts(conn, user, uid), "can_follow": uid != user["id"] and can_write(user) and u["role"] != "leader",
           "can_dm": uid != user["id"] and can_write(user) and u["role"] != "leader" and bool(visible_peer(conn, user, uid, dm=True)), "places": [], "week": None, "posts_visible": feed_svc.can_see_author(conn, user, uid)}
    if out["posts_visible"]:
        since = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        rows = all_(conn, "SELECT geo_place p, COUNT(*) n FROM (SELECT geo_place FROM media WHERE project_id=? AND uploader_id=? AND purpose IN ('safety','rectify','log') AND geo_place<>'' AND uploaded_at>=? "
                          "UNION ALL SELECT geo_place FROM notes WHERE project_id=? AND author_id=? AND geo_place<>'' AND created_at>=?) GROUP BY geo_place ORDER BY n DESC LIMIT 6",
                    (pid, uid, since, pid, uid, since))
        out["places"] = [{"place": r["p"], "n": r["n"]} for r in rows]
        located = one(conn, "SELECT (SELECT COUNT(*) FROM media WHERE project_id=? AND uploader_id=? AND geo_lat IS NOT NULL AND uploaded_at>=?) + "
                            "(SELECT COUNT(*) FROM notes WHERE project_id=? AND author_id=? AND geo_lat IS NOT NULL AND created_at>=?) n",
                      (pid, uid, since, pid, uid, since))
        out["located_30d"] = located["n"] if located else 0
    if u["role"] == "tech" and (uid == user["id"] or not R.is_sub(user["role"])):
        today = datetime.now().date()
        ws = (today - timedelta(days=today.weekday())).isoformat()
        wk = one(conn, "SELECT status FROM logs WHERE project_id=? AND author_id=? AND log_date=? AND mode='ai' AND period='week'", (pid, uid, ws))
        out["week"] = {"start": ws, "status": wk["status"] if wk else None}
    return out


# ============================================================================ 转发

def repost(conn, user: dict, key: str, text: str) -> dict:
    if key.split(":")[0] == "repost":
        raise ValueError("不能转发一条转发，请转发原动态")
    author = feed_svc._author_of(conn, user, key)  # 看不到的动态不能转发
    text = (text or "").strip()
    if len(text) > MAX_REPOST:
        raise ValueError(f"转发语最多 {MAX_REPOST} 字")
    pid = user["project_id"]
    rid = insert(conn, "feed_reposts", {"project_id": pid, "user_id": user["id"], "orig_key": key, "text": text, "created_at": now_iso()})
    done = set()
    link = feed_svc.circle_link(f"repost:{rid}")
    if author and author != user["id"]:
        _notify(conn, pid, author, f"{user['name']} 转发了你的现场动态", text, link)
        done.add(author)
    notify_mentions(conn, user, text, "转发", done, link)
    return {"key": f"repost:{rid}"}


# ============================================================================ 评论增强：回复、对句、精选

def feature_comment(conn, user: dict, cid: int, on: bool) -> dict:
    if not board.can_publish(user):
        raise PermissionError("只有港湾管理岗可以设置精选评论")
    c = one(conn, "SELECT * FROM feed_comments WHERE id=? AND project_id=?", (cid, user["project_id"]))
    if not c:
        raise ValueError("评论不存在")
    feed_svc._author_of(conn, user, c["post_key"])
    conn.execute("UPDATE feed_comments SET featured=? WHERE id=?", (1 if on else 0, cid))
    return {"id": cid, "featured": on}


# ============================================================================ 简报阅读

def _row_text(sec: str, r: dict) -> str:
    g = lambda k: str(r.get(k) or "").strip()  # noqa: E731
    if sec == "works":
        return " ".join(x for x in (g("part"), g("process"), g("desc")) if x)
    if sec == "progress":
        q = f"{g('qty')}{g('unit')}" if g("qty") else ""
        return " ".join(x for x in (g("activity_code"), ("完成 " + q) if q else "", ("累计 " + g("pct") + "%") if g("pct") else "", g("note")) if x)
    if sec in ("labor", "machinery"):
        return " ".join(x for x in (g("trade") or g("name"), (g("count") + ("人" if sec == "labor" else "台")) if g("count") else "", g("status")) if x)
    if sec == "materials":
        return " ".join(x for x in (g("name"), g("action"), g("qty") + g("unit")) if x)
    if sec == "issues":
        head = "·".join(x for x in (g("type"), g("severity")) if x)
        return " ".join(x for x in (head, g("desc"), ("措施：" + g("measure")) if g("measure") else "") if x)
    if sec == "tomorrow":
        return g("desc")
    return ""


LOG_SECTIONS = (("works", "施工内容"), ("progress", "完成工程量"), ("labor", "人员投入"), ("machinery", "机械投入"),
                ("materials", "材料"), ("issues", "质量安全及问题"), ("tomorrow", "次日计划"))


def _paragraphs(conn, user: dict, raw: dict, post: dict) -> tuple[str, list[dict]]:
    kind = raw["kind"]
    paras: list[dict] = []

    def add(text: str, label: str = "") -> None:
        text = (text or "").strip()
        if text:
            paras.append({"id": f"P{len(paras) + 1}", "text": text, "label": label})
    if kind == "notice":
        n = post["notice"]
        for line in n["body"].splitlines():
            add(line)
        return n["title"], paras
    if kind == "log":
        lg = raw["log"]
        c = loads(lg["content_json"], {})
        for sec, title in LOG_SECTIONS:
            for r in c.get(sec) or []:
                add(_row_text(sec, r), title)
        add(str(c.get("notes") or ""), "其他")
        return post["tag"] + "（" + (f"{lg['log_date']} ~ {lg['period_end']}" if lg["period"] == "week" and lg["period_end"] else lg["log_date"]) + "）", paras
    # note
    for line in (raw.get("text") or "").splitlines():
        add(line)
    return "随手记", paras


def _digits(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _hl_views(conn, user: dict, key: str) -> dict:
    pid = user["project_id"]
    rows = all_(conn, "SELECT h.*, u.name FROM feed_highlights h JOIN users u ON u.id=h.user_id WHERE h.project_id=? AND h.post_key=? AND (h.user_id=? OR h.public=1) ORDER BY h.id",
                (pid, key, user["id"]))
    mine = [{"id": h["id"], "para": h["para"], "quote": h["quote"], "note": h["note"], "public": bool(h["public"])} for h in rows if h["user_id"] == user["id"]]
    agg: dict[tuple, int] = {}
    for h in rows:
        if h["user_id"] != user["id"] and h["public"]:
            agg[(h["para"], h["quote"])] = agg.get((h["para"], h["quote"]), 0) + 1
    others = [{"para": p, "quote": q, "n": n, "note": ""} for (p, q), n in agg.items()]
    return {"mine": mine, "others": others}


def article(conn, user: dict, key: str) -> dict:
    kind = key.partition(":")[0]
    if kind not in ARTICLE_KINDS:
        raise ValueError("这类动态没有阅读模式")
    feed_svc._author_of(conn, user, key)
    raw = feed_svc.raw_by_key(conn, user, key)
    if not raw:
        raise ValueError("动态不存在")
    people_ = {u["id"]: u for u in all_(conn, "SELECT id, name, role, company FROM users WHERE project_id=?", (user["project_id"],))}
    post = feed_svc._post(conn, user, raw, people_, {})
    title, paras = _paragraphs(conn, user, raw, post)
    digest = hashlib.sha1("\n".join(p["text"] for p in paras).encode()).hexdigest()[:16]
    pid = user["project_id"]
    conn.execute("INSERT OR IGNORE INTO feed_reads (project_id, post_key, user_id, read_at) VALUES (?,?,?,?)", (pid, key, user["id"], now_iso()))
    reads = one(conn, "SELECT COUNT(*) n FROM feed_reads WHERE project_id=? AND post_key=?", (pid, key))["n"]
    feed_svc._attach_social(conn, user, [post])
    sm = one(conn, "SELECT * FROM feed_summaries WHERE project_id=? AND post_key=? AND digest=?", (pid, key, digest))
    return {"key": key, "kind": kind, "title": title, "author": post["author"], "time": post["time"], "tag": post["tag"], "geo": post.get("geo"),
            "geo_slot": post.get("geo_slot"), "photos": post["photos"], "paragraphs": paras, "reads": reads, "digest": digest,
            "summary": _summary_view(sm), "highlights": _hl_views(conn, user, key), "comments": post["comments"], "likes": post["likes"],
            "fav": post.get("fav", {"me": False}), "can_write": can_write(user)}


def _summary_view(sm: dict | None) -> dict | None:
    if not sm:
        return None
    d = loads(sm["body_json"], {})
    return {"one": d.get("one", ""), "points": d.get("points", []), "source": sm["source"], "created_at": sm["created_at"]}


SUMMARY_SYSTEM = """你是施工项目“现场圈”的阅读助手，为一篇简报写阅读摘要，帮同事快速抓重点。
规则：
1. 只能依据【原文】各段（P1、P2…），不得出现原文以外的数字、人名、作业代码；原文没有的结论一律不写。
2. 先写一句话概括（60字以内），再写至多3条要点（每条50字以内），每条要点标注依据的段落编号 refs。
3. 只复述、不下判断：不替人确认进度、不宣布闭环、不评价人。
只输出JSON：{"one":"一句话","points":[{"text":"要点","refs":["P2"]}]}"""


def _rule_summary(title: str, paras: list[dict]) -> dict:
    pri = ("质量安全及问题", "施工内容", "完成工程量", "次日计划")
    ranked = sorted(paras, key=lambda p: (pri.index(p["label"]) if p["label"] in pri else len(pri)))
    pts = [{"text": p["text"][:50], "refs": [p["id"]]} for p in ranked[:3]]
    return {"one": f"{title}，共 {len(paras)} 段内容；以下摘自原文。"[:60], "points": pts}


def _norm_summary(data, paras: list[dict]) -> dict:
    if not isinstance(data, dict) or not str(data.get("one") or "").strip() or not isinstance(data.get("points"), list):
        raise ValueError("缺少one或points")
    ids = {p["id"] for p in paras}
    src = _digits(" ".join(p["text"] for p in paras))
    one_ = str(data["one"]).strip()
    if len(one_) > 70:
        raise ValueError("概括过长")
    pts = []
    for p in data["points"][:3]:
        t = str((p or {}).get("text") or "").strip()
        refs = [r for r in ((p or {}).get("refs") or []) if r in ids]
        if not t or len(t) > 60 or not refs:
            raise ValueError("要点为空、过长或没有依据段落")
        pts.append({"text": t, "refs": refs})
    extra = _digits(one_ + " " + " ".join(p["text"] for p in pts)) - src
    if extra:
        raise ValueError(f"摘要出现原文之外的数字：{sorted(extra)[:3]}")
    return {"one": one_, "points": pts}


_inflight: set[tuple[int, str]] = set()  # 正在后台由智能体润色的总结


def _store_summary(conn, pid: int, key: str, digest: str, data: dict, source: str, run_id: int | None) -> None:
    conn.execute("DELETE FROM feed_summaries WHERE project_id=? AND post_key=?", (pid, key))
    insert(conn, "feed_summaries", {"project_id": pid, "post_key": key, "digest": digest, "body_json": dumps(data), "source": source,
                                    "ai_run_id": run_id, "created_at": now_iso()})


def _ai_upgrade(pid: int, user_id: int, key: str, title: str, paras: list[dict], digest: str) -> None:
    """后台：让智能体按原文改写总结；成功才替换规则摘要，失败保留规则摘要（不影响阅读）。"""
    from ..db import connect
    conn = connect()
    try:
        body = "\n".join(f"{p['id']}：{p['text']}" for p in paras)[:6000]
        try:
            res = call_json("feed.summary", system=SUMMARY_SYSTEM, user=f"标题：{title}\n【原文】\n{body}", prompt_version=PROMPT_VERSION,
                            mock=lambda: _rule_summary(title, paras), validate=lambda d: _norm_summary(d, paras), project_id=pid, user_id=user_id,
                            max_tokens=700, temperature=0.2, input_meta={"post": key})
        except AIError:
            return
        cur = one(conn, "SELECT digest FROM feed_summaries WHERE project_id=? AND post_key=?", (pid, key))
        if cur and cur["digest"] == digest:  # 原文没变才替换
            _store_summary(conn, pid, key, digest, res.data, "ai", res.run_id)
    finally:
        _inflight.discard((pid, key))
        conn.close()


def summary_state(conn, user: dict, key: str) -> dict:
    sm = one(conn, "SELECT * FROM feed_summaries WHERE project_id=? AND post_key=?", (user["project_id"], key))
    feed_svc._author_of(conn, user, key)
    return {"summary": _summary_view(sm), "pending": (user["project_id"], key) in _inflight}


def summarize(conn, user: dict, key: str, force: bool = False, bg=None) -> dict:
    """先立刻给出按原文摘录的总结，再在后台请智能体润色（bg 为 None 时同步润色，用于测试）。"""
    art = article(conn, user, key)
    pid = user["project_id"]
    if art["summary"] and not force:
        return {"summary": art["summary"], "pending": (pid, key) in _inflight}
    if not art["paragraphs"]:
        raise ValueError("这篇没有可总结的内容")
    _store_summary(conn, pid, key, art["digest"], _rule_summary(art["title"], art["paragraphs"]), "rule", None)
    if (pid, key) not in _inflight:
        _inflight.add((pid, key))
        args = (pid, user["id"], key, art["title"], art["paragraphs"], art["digest"])
        if bg is not None:
            bg.add_task(_ai_upgrade, *args)
        else:
            _ai_upgrade(*args)
    sm = one(conn, "SELECT * FROM feed_summaries WHERE project_id=? AND post_key=?", (pid, key))
    return {"summary": _summary_view(sm), "pending": (pid, key) in _inflight}


# ----------------------------------------------------------------------------- 划重点

def add_highlight(conn, user: dict, key: str, para: str, quote: str, note: str, public: bool) -> dict:
    art = article(conn, user, key)
    p = next((x for x in art["paragraphs"] if x["id"] == para), None)
    quote = (quote or "").strip()
    if not p or not quote or quote not in p["text"]:
        raise ValueError("选中的文字不在这一段里")
    if len(quote) > MAX_QUOTE:
        raise ValueError(f"一次最多划 {MAX_QUOTE} 字")
    note = (note or "").strip()[:MAX_NOTE]
    public = bool(public) and can_write(user)  # 只读角色只能自己看
    pid = user["project_id"]
    ex = one(conn, "SELECT id FROM feed_highlights WHERE project_id=? AND post_key=? AND user_id=? AND para=? AND quote=?", (pid, key, user["id"], para, quote))
    if ex:
        conn.execute("UPDATE feed_highlights SET note=?, public=? WHERE id=?", (note, 1 if public else 0, ex["id"]))
        hid = ex["id"]
    else:
        hid = insert(conn, "feed_highlights", {"project_id": pid, "post_key": key, "user_id": user["id"], "para": para, "quote": quote, "note": note,
                                              "public": 1 if public else 0, "created_at": now_iso()})
    return {"id": hid, "para": para, "quote": quote, "note": note, "public": public}


def set_highlight(conn, user: dict, hid: int, public: bool | None, note: str | None) -> dict:
    h = one(conn, "SELECT * FROM feed_highlights WHERE id=? AND project_id=? AND user_id=?", (hid, user["project_id"], user["id"]))
    if not h:
        raise ValueError("重点不存在")
    if public is not None:
        conn.execute("UPDATE feed_highlights SET public=? WHERE id=?", (1 if (public and can_write(user)) else 0, hid))
    if note is not None:
        conn.execute("UPDATE feed_highlights SET note=? WHERE id=?", (note.strip()[:MAX_NOTE], hid))
    h = one(conn, "SELECT * FROM feed_highlights WHERE id=?", (hid,))
    return {"id": hid, "para": h["para"], "quote": h["quote"], "note": h["note"], "public": bool(h["public"])}


def delete_highlight(conn, user: dict, hid: int) -> None:
    cur = conn.execute("DELETE FROM feed_highlights WHERE id=? AND project_id=? AND user_id=?", (hid, user["project_id"], user["id"]))
    if not cur.rowcount:
        raise ValueError("重点不存在")


def my_highlights(conn, user: dict, limit: int = 50) -> list[dict]:
    rows = all_(conn, "SELECT * FROM feed_highlights WHERE project_id=? AND user_id=? ORDER BY id DESC LIMIT ?", (user["project_id"], user["id"], limit))
    out = []
    for h in rows:
        try:
            feed_svc._author_of(conn, user, h["post_key"])  # 原文已不可见（如被撤回）的重点不再显示
        except ValueError:
            continue
        raw = feed_svc.raw_by_key(conn, user, h["post_key"])
        if not raw:
            continue
        out.append({"id": h["id"], "key": h["post_key"], "para": h["para"], "quote": h["quote"], "note": h["note"], "public": bool(h["public"]),
                    "time": h["created_at"], "tag": {"log": "日志/周确认", "notice": "公告", "note": "随手记"}.get(raw["kind"], "")})
    return out


# ============================================================================ 私信
# 只有会话双方可读；没有删除、没有撤回、没有管理员入口。

def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _dm_peer(conn, user: dict, peer_id: int) -> dict:
    if not can_write(user):
        raise PermissionError("当前角色不能使用私信")
    peer = visible_peer(conn, user, peer_id, dm=True)
    if not peer or peer["role"] in ("leader", "admin") or peer["id"] == user["id"]:
        raise ValueError("找不到这位同事")
    return peer


def _thread(conn, user: dict, peer_id: int, create: bool = False) -> dict | None:
    lo, hi = _pair(user["id"], peer_id)
    t = one(conn, "SELECT * FROM dm_threads WHERE project_id=? AND user_lo=? AND user_hi=?", (user["project_id"], lo, hi))
    if not t and create:
        tid = insert(conn, "dm_threads", {"project_id": user["project_id"], "user_lo": lo, "user_hi": hi, "created_at": now_iso(), "last_at": now_iso()})
        t = one(conn, "SELECT * FROM dm_threads WHERE id=?", (tid,))
    return t


def _last_read(conn, tid: int, uid: int) -> int:
    r = one(conn, "SELECT last_id FROM dm_reads WHERE thread_id=? AND user_id=?", (tid, uid))
    return r["last_id"] if r else 0


def _msg_view(m: dict, user: dict) -> dict:
    return {"id": m["id"], "mine": m["sender_id"] == user["id"], "text": m["text"], "time": m["created_at"], "ref": loads(m["ref_json"]),
            "files": [{"id": f, "thumb": f"/api/social/dm/files/{f}/thumb", "url": f"/api/social/dm/files/{f}/file"} for f in loads(m["files_json"], [])]}


def _ref_card(conn, user: dict, key: str) -> dict:
    """被引用动态的快照：发送时可见才能引用，快照只含当时能看到的摘要。"""
    feed_svc._author_of(conn, user, key)
    raw = feed_svc.raw_by_key(conn, user, key)
    if not raw:
        raise ValueError("动态不存在")
    people_ = {u["id"]: u for u in all_(conn, "SELECT id, name, role, company FROM users WHERE project_id=?", (user["project_id"],))}
    p = feed_svc._post(conn, user, raw, people_, {})
    text = p["text"] or (p.get("notice") or {}).get("title") or ""
    return {"key": key, "tag": p["tag"], "author": p["author"]["name"], "text": text[:80], "thumb": p["photos"][0]["thumb"] if p["photos"] else None}


def dm_send(conn, user: dict, to: int, text: str, file_ids: list[int], ref_key: str | None) -> dict:
    peer = _dm_peer(conn, user, to)
    text = (text or "").strip()
    if len(text) > MAX_DM:
        raise ValueError(f"一条私信最多 {MAX_DM} 字")
    file_ids = [int(f) for f in (file_ids or [])][:9]
    if not text and not file_ids:
        raise ValueError("请输入内容或选一张照片")
    for fid in file_ids:
        f = one(conn, "SELECT uploader_id FROM dm_files WHERE id=? AND project_id=?", (fid, user["project_id"]))
        if not f or f["uploader_id"] != user["id"]:
            raise ValueError("照片不存在或不属于你")
    ref = _ref_card(conn, user, ref_key) if ref_key else None
    t = _thread(conn, user, peer["id"], create=True)
    unread_from_me = one(conn, "SELECT COUNT(*) n FROM dm_messages WHERE thread_id=? AND sender_id=? AND id>?", (t["id"], user["id"], _last_read(conn, t["id"], peer["id"])))["n"]
    mid = insert(conn, "dm_messages", {"thread_id": t["id"], "project_id": user["project_id"], "sender_id": user["id"], "text": text,
                                       "files_json": dumps(file_ids), "ref_json": dumps(ref) if ref else None, "created_at": now_iso()})
    conn.execute("UPDATE dm_threads SET last_at=? WHERE id=?", (now_iso(), t["id"]))
    conn.execute("INSERT INTO dm_reads (thread_id, user_id, last_id) VALUES (?,?,?) ON CONFLICT(thread_id, user_id) DO UPDATE SET last_id=excluded.last_id", (t["id"], user["id"], mid))
    if not unread_from_me:  # 对方已读完我之前的消息才提醒，连发多条只提醒一次；通知里不写私信内容
        _notify(conn, user["project_id"], peer["id"], f"{user['name']} 给你发来私信", "", link={"route": "circle", "params": {"dm": str(user["id"])}})
    return _msg_view(one(conn, "SELECT * FROM dm_messages WHERE id=?", (mid,)), user)


def dm_messages(conn, user: dict, peer_id: int, after: int = 0, mark: bool = True) -> dict:
    peer = _dm_peer(conn, user, peer_id)
    t = _thread(conn, user, peer["id"])
    msgs: list[dict] = []
    if t:
        msgs = all_(conn, "SELECT * FROM dm_messages WHERE thread_id=? AND id>? ORDER BY id LIMIT 200", (t["id"], max(0, after)))
        if mark and msgs:
            conn.execute("INSERT INTO dm_reads (thread_id, user_id, last_id) VALUES (?,?,?) ON CONFLICT(thread_id, user_id) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)",
                         (t["id"], user["id"], msgs[-1]["id"]))
    peer_read = _last_read(conn, t["id"], peer["id"]) if t else 0
    return {"peer": _person(peer), "messages": [_msg_view(m, user) for m in msgs], "peer_read": peer_read}


def dm_threads(conn, user: dict) -> list[dict]:
    if not can_write(user):
        raise PermissionError("当前角色不能使用私信")
    out = []
    for t in all_(conn, "SELECT * FROM dm_threads WHERE project_id=? AND (user_lo=? OR user_hi=?) ORDER BY last_at DESC LIMIT 100", (user["project_id"], user["id"], user["id"])):
        pid_ = t["user_hi"] if t["user_lo"] == user["id"] else t["user_lo"]
        peer = visible_peer(conn, user, pid_, dm=True)
        if not peer:
            continue
        last = one(conn, "SELECT * FROM dm_messages WHERE thread_id=? ORDER BY id DESC LIMIT 1", (t["id"],))
        if not last:
            continue
        unread = one(conn, "SELECT COUNT(*) n FROM dm_messages WHERE thread_id=? AND sender_id<>? AND id>?", (t["id"], user["id"], _last_read(conn, t["id"], user["id"])))["n"]
        preview = last["text"] or ("[照片]" if loads(last["files_json"], []) else "[动态]")
        out.append({"peer": _person(peer), "last": {"text": preview[:40], "time": last["created_at"], "mine": last["sender_id"] == user["id"]}, "unread": unread})
    return out


def dm_unread(conn, user: dict) -> int:
    if not can_write(user):
        return 0
    row = one(conn, "SELECT COUNT(*) n FROM dm_messages m JOIN dm_threads t ON t.id=m.thread_id LEFT JOIN dm_reads r ON r.thread_id=t.id AND r.user_id=? "
                    "WHERE t.project_id=? AND (t.user_lo=? OR t.user_hi=?) AND m.sender_id<>? AND m.id>COALESCE(r.last_id,0)",
              (user["id"], user["project_id"], user["id"], user["id"], user["id"]))
    return row["n"] if row else 0


def dm_save_file(conn, user: dict, filename: str, raw: bytes) -> dict:
    if not can_write(user):
        raise PermissionError("当前角色不能使用私信")
    if not raw or len(raw) > DM_MAX_UPLOAD:
        raise ValueError("照片为空或超过 15MB")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise ValueError("无法识别的图片格式") from None
    up = ImageOps.exif_transpose(img).convert("RGB")  # 重新编码，去除 EXIF（含 GPS）
    sha = hashlib.sha256(raw).hexdigest()
    d = settings.media_dir / "dm"
    d.mkdir(parents=True, exist_ok=True)
    big, th = up.copy(), up.copy()
    big.thumbnail((DM_EDGE, DM_EDGE))
    th.thumbnail((DM_THUMB, DM_THUMB))
    big.save(d / f"{sha}.jpg", "JPEG", quality=85)
    th.save(d / f"{sha}_t.jpg", "JPEG", quality=78)
    fid = insert(conn, "dm_files", {"project_id": user["project_id"], "uploader_id": user["id"], "file": f"dm/{sha}.jpg", "thumb": f"dm/{sha}_t.jpg",
                                    "name": (filename or "")[:120], "created_at": now_iso()})
    return {"id": fid, "thumb": f"/api/social/dm/files/{fid}/thumb", "url": f"/api/social/dm/files/{fid}/file"}


def dm_file_path(conn, user: dict, fid: int, which: str) -> Path | None:
    """私信照片只有发送人与会话对方能取；其他人（含管理员）一律没有。"""
    f = one(conn, "SELECT * FROM dm_files WHERE id=? AND project_id=?", (fid, user["project_id"]))
    if not f:
        return None
    ok = f["uploader_id"] == user["id"]
    if not ok:
        for m in all_(conn, "SELECT m.files_json, t.user_lo, t.user_hi FROM dm_messages m JOIN dm_threads t ON t.id=m.thread_id "
                            "WHERE m.sender_id=? AND m.project_id=? AND (t.user_lo=? OR t.user_hi=?)", (f["uploader_id"], user["project_id"], user["id"], user["id"])):
            if fid in loads(m["files_json"], []):
                ok = True
                break
    if not ok:
        return None
    path = (settings.media_dir / f["thumb" if which == "thumb" else "file"]).resolve()
    return path if settings.media_dir.resolve() in path.parents and path.is_file() else None
