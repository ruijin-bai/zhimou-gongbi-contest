"""各 Agent 的统一输出：发现（隐患、质控提醒、数据问题、产值偏差……）及其处置闭环。"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .. import roles as R
from ..agents import AGENT_BY_KEY
from ..db import all_, audit, dumps, insert, loads, now_iso, one, tx

LEVEL_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}
LEVEL_NAME = {"high": "高", "medium": "中", "low": "低", "info": "提示"}
STATUS_NAME = {"open": "待处理", "rectifying": "待复核", "resolved": "已闭环", "rejected": "复核未通过", "dismissed": "已忽略"}
CANDIDATE_NAME = "候选·待确认"

# D6“当场已纠正”：候选一键闭环入台账（confirmed_at 与 resolved_at 同时写入），标注“当场纠正、无整改照片”，计为 AI 采纳。
# 口径变化：采纳＝闭环，所以当场纠正的候选进入采纳数（采纳率上升）；闭环天数＝发现到当场确认，会拉低平均闭环天数。
ONSPOT_NOTE = "当场纠正、无整改照片"
ONSPOT_STATUS = "已闭环（当场纠正）"
# AC-5.3 不采纳原因：选项 + 可选补充说明（选“其他”时必填说明）；记入 feedback_note 的是“选项文字：补充说明”
DISMISS_REASONS = {"misread": "误识别：照片里没有这个问题", "not_hazard": "不构成隐患", "duplicate": "重复：已有同一问题", "other": "其他"}
SIMILAR_DAYS = 14         # AC-5.4 / AC-5.5：同部位同类别、14 天内未闭环的视为可能同一问题
BATCH_MAX = 50

# “候选”= AI 识别出、尚无人确认的隐患（confirmed_at 为空且仍是 open）。候选不进台账统计、超期督办、闭环率、分包对比，也不通知整改人；
# 不采纳（dismissed）的候选 confirmed_at 也为空，同样不算已确认问题。凡是“台账口径”的查询一律加 CONFIRMED。
CONFIRMED = "confirmed_at IS NOT NULL"


def is_candidate(f: dict) -> bool:
    return f["confirmed_at"] is None and f["status"] == "open"


def create(conn, project_id: int, agent: str, kind: str, level: str, title: str, body: dict | None = None, *,
           ref_type: str | None = None, ref_id: int | None = None, activity_code: str | None = None,
           fingerprint: str | None = None, assignee_id: int | None = None, due_days: int | None = None,
           ai_run_id: int | None = None, candidate: bool = False, confirmed_by: int | None = None) -> int | None:
    """新建发现；同一指纹已存在则只更新内容（不重复派单）。返回新建 id，已存在返回 None。
    candidate=True：AI 识别的隐患先作“候选”，须人确认后才进入台账与整改流程；其余（人写的日志上报、质控提醒、数据审核）创建即视为已确认。"""
    now = now_iso()
    if fingerprint:
        ex = one(conn, "SELECT id, status FROM findings WHERE project_id=? AND fingerprint=?", (project_id, fingerprint))
        if ex:
            conn.execute("UPDATE findings SET title=?, body_json=?, level=?, updated_at=? WHERE id=?",
                         (title, dumps(body or {}), level, now, ex["id"]))
            return None
    due = (date.today() + timedelta(days=due_days)).isoformat() if due_days is not None else None
    return insert(conn, "findings", {
        "project_id": project_id, "agent": agent, "kind": kind, "level": level, "title": title[:200],
        "body_json": dumps(body or {}), "ref_type": ref_type, "ref_id": ref_id, "activity_code": activity_code,
        "fingerprint": fingerprint, "assignee_id": assignee_id, "due_date": due, "ai_run_id": ai_run_id,
        "created_at": now, "updated_at": now, "confirmed_at": None if candidate else now, "confirmed_by": None if candidate else confirmed_by,
    })


def view(f: dict, users: dict[int, str] | None = None) -> dict:
    users = users or {}
    cand = is_candidate(f)
    overdue = bool(not cand and f["due_date"] and f["status"] in ("open", "rejected") and f["due_date"] < date.today().isoformat())
    body = loads(f["body_json"], {})
    return {**{k: f[k] for k in ("id", "agent", "kind", "level", "title", "ref_type", "ref_id", "activity_code", "status",
                                 "assignee_id", "due_date", "created_at", "updated_at", "resolved_at", "feedback_note", "rectify_media_id",
                                 "confirmed_by", "confirmed_at")},
            "agent_name": AGENT_BY_KEY.get(f["agent"], {}).get("name", f["agent"]), "body": body,
            "verify": loads(f["verify_json"]), "level_name": LEVEL_NAME[f["level"]], "status_name": status_label(f),
            "assignee": users.get(f["assignee_id"]), "overdue": overdue, "candidate": cand,
            "onspot": _is_onspot(f, body), "confirmed_by_name": users.get(f["confirmed_by"]), "returned": is_returned(f)}


def is_returned(f: dict) -> bool:
    """整改被人退回重改（或闭环后重新打开）：提交整改照片会把状态置为待复核并写入 verify_json，
    所以“待处理”又带着复核记录，只能是人把它打回来了。退回原因在 feedback_note（D-2、D-4）。"""
    return f["status"] == "open" and bool(loads(f["verify_json"]))


def _is_onspot(f: dict, body: dict | None = None) -> bool:
    if f["status"] != "resolved":
        return False
    body = body if body is not None else (loads(f.get("body_json"), {}) or {})
    return bool((body or {}).get("onspot"))


def status_label(f: dict) -> str:
    """状态中文名：候选显示“候选·待确认”；未经确认就被不采纳的显示“不采纳”；候选“当场已纠正”闭环的显示“已闭环（当场纠正）”。"""
    if is_candidate(f):
        return CANDIDATE_NAME
    if f["status"] == "dismissed" and f["confirmed_at"] is None:
        return "不采纳"
    if _is_onspot(f):
        return ONSPOT_STATUS
    return STATUS_NAME[f["status"]]


def list_for(conn, project_id: int, *, agent: str | None = None, status: str | None = None, assignee_id: int | None = None,
             assignee_ids: list[int] | None = None, kinds: list[str] | None = None, limit: int = 300,
             candidates: str = "exclude") -> list[dict]:
    """candidates：exclude（默认，只含已确认，台账口径）/ only（只含待确认候选）/ all。"""
    q, p = "SELECT * FROM findings WHERE project_id=?", [project_id]
    if candidates == "only":
        q += " AND confirmed_at IS NULL AND status='open'"
    elif candidates != "all":
        q += f" AND {CONFIRMED}"
    if agent:
        q += " AND agent=?"
        p.append(agent)
    if status == "active":
        q += " AND status IN ('open','rectifying','rejected')"
    elif status:
        q += " AND status=?"
        p.append(status)
    if assignee_id:
        q += " AND assignee_id=?"
        p.append(assignee_id)
    if assignee_ids is not None:
        q += f" AND assignee_id IN ({','.join('?' for _ in assignee_ids) or 'NULL'})"
        p.extend(assignee_ids)
    if kinds:
        q += f" AND kind IN ({','.join('?' for _ in kinds)})"
        p.extend(kinds)
    q += " ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'rejected' THEN 1 WHEN 'rectifying' THEN 2 ELSE 3 END," \
         " CASE level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, id DESC LIMIT ?"
    p.append(limit)
    users = {u["id"]: u["name"] for u in all_(conn, "SELECT id, name FROM users WHERE project_id=?", (project_id,))}
    return [view(f, users) for f in all_(conn, q, p)]


def set_status(conn, finding_id: int, user: dict, status: str, note: str = "") -> dict:
    if status not in ("open", "resolved", "dismissed"):
        raise ValueError("状态无效")
    f = one(conn, "SELECT * FROM findings WHERE id=? AND project_id=?", (finding_id, user["project_id"]))
    if not f:
        raise ValueError("记录不存在")
    if is_candidate(f):  # 候选只能“确认”或“不采纳”，不能直接闭环/重开
        if status != "dismissed":
            raise ValueError("这是 AI 候选隐患，请先确认并派单，或写明原因不采纳")
        try:
            return dismiss_candidate(conn, finding_id, user, note)
        except PermissionError as exc:  # 本接口一贯以 400 返回权限不足
            raise ValueError(str(exc)) from None
    if status == "resolved" and f["agent"] == "safety" and f["rectify_media_id"]:
        r = one(conn, "SELECT uploader_id FROM media WHERE id=?", (f["rectify_media_id"],))
        if r and r["uploader_id"] == user["id"]:
            raise ValueError("整改提交人不能闭环自己提交的整改，请由安全主管或其他人员确认")
    if R.is_sub(user["role"]):
        from .role_views import in_scope
        if not in_scope(conn, user, f["assignee_id"]):
            raise ValueError("只能处理派给本单位的事项")
        if status != "resolved" or f["agent"] == "safety":
            raise ValueError("分包单位只能提交整改，闭环、忽略或重新打开由港湾对应岗位确认")
    else:
        # 港湾侧：安全事项由安全主管（或项目经理）确认，质量/数据事项由工程主管（或项目经理）确认
        cap = "site_safety" if f["agent"] == "safety" else "site_quality"
        if not R.can(user["role"], cap) and not (status == "resolved" and f["assignee_id"] == user["id"] and f["agent"] != "safety"):
            raise ValueError("该事项应由" + ("安全主管" if cap == "site_safety" else "工程主管") + "确认")
    if status == "dismissed" and not note.strip():
        raise ValueError("忽略/误报需写明原因，用于改进 Agent")
    now = now_iso()
    with tx(conn):
        conn.execute("""UPDATE findings SET status=?, feedback_by=?, feedback_note=?, feedback_at=?, updated_at=?,
                        resolved_at=CASE WHEN ?='resolved' THEN ? ELSE resolved_at END WHERE id=?""",
                     (status, user["id"], note.strip()[:500], now, now, status, now, finding_id))
        audit(conn, user["id"], f"finding.{status}", f"finding:{finding_id}", note[:200])
        _notify_rectifier(conn, f, user, status, note)
    return view(one(conn, "SELECT * FROM findings WHERE id=?", (finding_id,)))


def _notify_rectifier(conn, f: dict, user: dict, status: str, note: str) -> None:
    """D-2：安全隐患被人确认闭环、退回重改或重新打开后，告知整改责任人和提交整改照片的人（操作人自己除外），消息带隐患跳转；
    退回写明原因。与派单、催办消息同一张 notifications 表。"""
    if f["agent"] != "safety" or status == f["status"] or status not in ("resolved", "open") or f["status"] == "dismissed":
        return
    to = {f["assignee_id"]}
    if f["rectify_media_id"]:
        m = one(conn, "SELECT uploader_id FROM media WHERE id=?", (f["rectify_media_id"],))
        to.add(m["uploader_id"] if m else None)
    to -= {None, user["id"]}
    if not to:
        return
    who = f"{user['name']}（{R.role_name(user['role'])}）"
    why = note.strip()[:200] or "未写明"
    if status == "resolved":
        title, body = f"整改已闭环：{f['title'][:40]}", f"{who}已确认闭环。"
    elif f["status"] == "resolved":
        title, body = f"隐患重新打开：{f['title'][:40]}", f"{who}重新打开了这条已闭环的隐患，原因：{why}。请重新整改并拍整改照片。"
    else:
        title, body = f"整改被退回：{f['title'][:40]}", f"{who}退回重改，原因：{why}。请按要求重新整改并拍整改照片。"
    marks = ",".join("?" for _ in to)
    for r in all_(conn, f"SELECT id FROM users WHERE project_id=? AND active=1 AND id IN ({marks}) ORDER BY id", (f["project_id"], *sorted(to))):
        insert(conn, "notifications", {"project_id": f["project_id"], "user_id": r["id"], "alert_id": None, "title": title, "body": body,
                                       "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(),
                                       "link_json": dumps({"route": "finding", "params": {"id": str(f["id"])}})})


def stats(conn, project_id: int) -> dict:
    """各 Agent 的事项统计。待确认候选单列 candidate，不进 total/open/high_open；被不采纳的候选计入 dismissed（作为 AI 误报反馈）。"""
    rows = all_(conn, """SELECT agent, kind, status, level, (confirmed_at IS NULL AND status='open') cand, COUNT(*) n FROM findings
                         WHERE project_id=? GROUP BY agent, kind, status, level, cand""", (project_id,))
    out: dict = {}
    for r in rows:
        a = out.setdefault(r["agent"], {"total": 0, "open": 0, "resolved": 0, "dismissed": 0, "high_open": 0, "candidate": 0})
        if r["cand"]:
            a["candidate"] += r["n"]
            continue
        a["total"] += r["n"]
        if r["status"] in ("open", "rectifying", "rejected"):
            a["open"] += r["n"]
            if r["level"] == "high":
                a["high_open"] += r["n"]
        elif r["status"] == "resolved":
            a["resolved"] += r["n"]
        elif r["status"] == "dismissed":
            a["dismissed"] += r["n"]
    # D6：已闭环里有多少是候选“当场已纠正”（无整改照片）——单列出来，口径变化看得见
    for r in all_(conn, f"""SELECT agent, COUNT(*) n FROM findings WHERE project_id=? AND status='resolved' AND {CONFIRMED}
                           AND json_extract(body_json, '$.onspot') IS NOT NULL GROUP BY agent""", (project_id,)):
        if r["agent"] in out:
            out[r["agent"]]["onspot"] = r["n"]
    for a in out.values():
        a.setdefault("onspot", 0)
    return out


# ----------------------------------------------------------------------------- 候选隐患：确认 / 不采纳

def can_decide(conn, user: dict, f: dict) -> bool:
    """能对候选做决定的人：港湾安全主管（及项目经理、管理员，沿用 site_safety）；分包安全员只限本单位范围内的候选。技术员、工程主管、领导不能。"""
    if R.can(user["role"], "site_safety"):
        return True
    if user["role"] == "sub_safety":
        from .role_views import in_scope
        return in_scope(conn, user, f["assignee_id"])
    return False


def _candidate_of(conn, finding_id: int, user: dict) -> dict:
    f = one(conn, "SELECT * FROM findings WHERE id=? AND project_id=?", (finding_id, user["project_id"]))
    if not f:
        raise ValueError("记录不存在")
    if not is_candidate(f):
        raise ValueError("这条隐患已处理过，不是待确认的候选")
    if not can_decide(conn, user, f):
        raise PermissionError("只有本单位安全员或安全主管可以确认候选隐患")
    return f


EDIT_FIELDS = ("level", "category", "desc", "measure")


def _plan_assignee(conn, user: dict, assignee_id: int | None) -> int | None:
    """校验指定的责任人（本项目在岗、非管理员/领导；分包安全员只能派给本单位人员）。不指定返回 None。"""
    from . import role_views
    if not assignee_id:
        return None
    t = one(conn, "SELECT * FROM users WHERE id=? AND project_id=? AND active=1", (assignee_id, user["project_id"]))
    if not t or t["role"] in ("admin", "leader"):
        raise ValueError("责任人无效")
    if R.is_sub(user["role"]) and not role_views.in_scope(conn, user, t["id"]):
        raise ValueError("只能派给本单位人员")
    return t["id"]


def _plan_confirm(conn, user: dict, f: dict, *, assignee_id: int | None, due_date: str | None, edits: dict | None) -> dict:
    """确认并派单的校验与取值（不写库）。"""
    from ..ai.safety_agent import DUE
    edits = {k: v for k, v in (edits or {}).items() if k in EDIT_FIELDS and v not in (None, "")}
    body = loads(f["body_json"], {}) or {}
    level, title = f["level"], f["title"]
    if edits:
        if "level" in edits and edits["level"] not in ("high", "medium", "low"):
            raise ValueError("风险等级无效")
        body.setdefault("ai_original", {"level": f["level"], "title": f["title"], **{k: body.get(k) for k in ("category", "desc", "measure")}})
        for k in ("category", "desc", "measure"):
            if k in edits:
                body[k] = str(edits[k]).strip()[:200]
        level = edits.get("level", level)
        if "category" in edits or "desc" in edits:
            title = f"{body.get('category') or '隐患'}：{body.get('desc') or ''}"[:200]
    target_id = _plan_assignee(conn, user, assignee_id) or f["assignee_id"]
    if due_date:
        try:
            due = date.fromisoformat(due_date[:10]).isoformat()
        except ValueError:
            raise ValueError("期限格式不对") from None
        if due < date.today().isoformat():
            raise ValueError("期限不能早于今天")
    else:  # 默认从确认当天起算，不因候选放置的时间被缩短
        due = (date.today() + timedelta(days=DUE.get(level, 2))).isoformat()
    return {"action": "confirm", "f": f, "target_id": target_id, "due": due, "level": level, "title": title, "body": body, "edits": edits}


def _apply_confirm(conn, user: dict, p: dict, now: str) -> None:
    """写入确认（调用方负责事务）；派单通知由调用方按责任人合并发送。"""
    fid = p["f"]["id"]
    conn.execute("""UPDATE findings SET confirmed_by=?, confirmed_at=?, assignee_id=?, due_date=?, level=?, title=?, body_json=?, updated_at=? WHERE id=?""",
                 (user["id"], now, p["target_id"], p["due"], p["level"], p["title"], dumps(p["body"]), now, fid))
    audit(conn, user["id"], "finding.confirm_edit" if p["edits"] else "finding.confirm", f"finding:{fid}",
          f"-> user:{p['target_id']} due:{p['due']}" + (f" 修改:{','.join(p['edits'])}" if p["edits"] else ""))


def _notify_dispatch(conn, user: dict, plans: list[dict]) -> None:
    """派单通知：同一责任人只发一条（批量确认 3 项也只收到 1 条消息）；派给确认人自己的不发。"""
    from . import role_views
    by: dict[int, list[dict]] = {}
    for p in plans:
        if p["action"] == "confirm" and p["target_id"] and p["target_id"] != user["id"]:
            by.setdefault(p["target_id"], []).append(p)
    for tid, ps in by.items():
        who = f"{user['name']}（{R.role_name(user['role'])}）"
        if len(ps) == 1:
            title, body = f"新派单：{ps[0]['title'][:40]}", f"{who}确认并派给了你，期限 {ps[0]['due']}。"
        else:
            title = f"新派单 {len(ps)} 项：{ps[0]['title'][:24]} 等"
            body = f"{who}确认并派给了你 {len(ps)} 项隐患，最早期限 {min(p['due'] for p in ps)}。"
        role_views._notify(conn, user["project_id"], tid, title, body, role_views.finding_link(ps[0]["f"]["id"]))


def confirm_candidate(conn, finding_id: int, user: dict, *, assignee_id: int | None = None, due_date: str | None = None,
                      edits: dict | None = None) -> dict:
    """确认并派单：一次操作完成确认 + 指派 + 期限；edits 不为空即“修改后确认”（可改等级/类别/描述/措施，AI 原值留在 body.ai_original）。
    确认后与原来 AI 直接派单的隐患完全一样进入台账与整改流程；责任人不是确认人时才发派单通知。"""
    f = _candidate_of(conn, finding_id, user)
    p = _plan_confirm(conn, user, f, assignee_id=assignee_id, due_date=due_date, edits=edits)
    with tx(conn):
        _apply_confirm(conn, user, p, now_iso())
        _notify_dispatch(conn, user, [p])
    users = {u["id"]: u["name"] for u in all_(conn, "SELECT id, name FROM users WHERE project_id=?", (user["project_id"],))}
    return view(one(conn, "SELECT * FROM findings WHERE id=?", (finding_id,)), users)


def _apply_dismiss(conn, user: dict, fid: int, note: str, now: str) -> None:
    conn.execute("UPDATE findings SET status='dismissed', feedback_by=?, feedback_note=?, feedback_at=?, updated_at=? WHERE id=?",
                 (user["id"], note[:500], now, now, fid))
    audit(conn, user["id"], "finding.dismissed", f"finding:{fid}", note[:200])


def dismiss_candidate(conn, finding_id: int, user: dict, reason: str) -> dict:
    """不采纳：必须写原因（记入 feedback_note，用于改进 Agent）。状态记为 dismissed，confirmed_at 仍为空，不算已确认问题。"""
    _candidate_of(conn, finding_id, user)
    if not (reason or "").strip():
        raise ValueError("不采纳需写明原因，用于改进 Agent")
    with tx(conn):
        _apply_dismiss(conn, user, finding_id, reason.strip(), now_iso())
    return view(one(conn, "SELECT * FROM findings WHERE id=?", (finding_id,)))


# ----------------------------------------------------------------------------- T-05 候选收敛：默认责任人、当场已纠正、批量处理、同类合并

def default_assignee(conn, f: dict) -> int | None:
    """候选的默认责任人（F-16，AC-5.7）。f 为 findings 原始行。
    - 现责任人是在岗的技术员/港湾人员（如技术员自己拍的照片）→ 就是他；
    - 上报人（照片上传人）或现责任人是分包安全员 → 不能默认给他本人，改取同单位技术员：作业负责范围能唯一对上的优先，
      同单位只有一位技术员时取这位；仍定不下来返回 None，由人在组件里选。"""
    users = {u["id"]: u for u in all_(conn, "SELECT id, role, company, wbs_scope, active FROM users WHERE project_id=?", (f["project_id"],))}
    cur = users.get(f["assignee_id"]) if f.get("assignee_id") else None
    cur = cur if cur and cur["active"] else None
    reporter = None
    if f.get("ref_type") == "media" and f.get("ref_id"):
        m = one(conn, "SELECT uploader_id FROM media WHERE id=?", (f["ref_id"],))
        reporter = users.get(m["uploader_id"]) if m else None
    sub_rep = reporter if reporter and reporter["role"] == "sub_safety" else None
    if cur and cur["role"] not in ("admin", "leader", "sub_safety") and not (sub_rep and cur["id"] == sub_rep["id"]):
        return cur["id"]
    base = sub_rep or (cur if cur and cur["role"] == "sub_safety" else None)
    if not base or not (base["company"] or "").strip():
        return None
    techs = [u for u in users.values() if u["role"] == "tech" and u["active"] and u["company"] == base["company"]]
    if f.get("activity_code") and len(techs) > 1:
        from . import p6
        act = next((a for a in p6.current_activities(conn, f["project_id"]) if a["code"] == f["activity_code"]), None)
        hit = [u for u in techs if act and (u["wbs_scope"] or "").strip() and p6.in_scope(act, u["wbs_scope"])]
        if len(hit) == 1:
            return hit[0]["id"]
    return techs[0]["id"] if len(techs) == 1 else None


# 日志文字 → 安全隐患类别（与安全Agent 的类别表一致）；认不出返回 None，不猜
_CAT_WORDS = (
    ("临边洞口", ("临边", "洞口", "护栏", "栏杆")),
    ("高处作业", ("高处", "安全带", "登高", "攀爬")),
    ("起重吊装", ("吊装", "起重", "吊车", "吊物", "吊索", "吊钩")),
    ("基坑边坡", ("基坑", "边坡", "坑边", "塌方", "放坡")),
    ("临时用电", ("用电", "电缆", "配电", "漏电", "电箱", "电线")),
    ("机械车辆", ("挖机", "挖掘机", "装载机", "车辆", "倒车", "机械")),
    ("脚手架模板", ("脚手架", "模板", "支架")),
    ("消防动火", ("消防", "动火", "灭火", "焊接", "气瓶")),
    ("个人防护", ("安全帽", "反光衣", "防护鞋", "劳保", "未佩戴")),
    ("文明施工", ("泥泞", "积水", "堆放", "杂乱", "文明施工", "扬尘", "便道", "围挡")),
)


def category_of_text(text: str | None) -> str | None:
    """命中关键词最多的类别（同数取表中靠前的）；一个都没命中返回 None。"""
    text = text or ""
    best, hits = None, 0
    for cat, words in _CAT_WORDS:
        n = sum(1 for w in words if w in text)
        if n > hits:
            best, hits = cat, n
    return best


def _norm(text: str | None) -> str:
    return "".join(str(text or "").split())


def _cat_of(f: dict, body: dict | None = None) -> str | None:
    body = body if body is not None else (loads(f.get("body_json"), {}) or {})
    c = body.get("category")
    return c if c and c != "其他" else category_of_text(f"{f['title']} {body.get('desc') or ''}")


def _part_of(conn, rs, f: dict, body: dict | None = None) -> str | None:
    """部位（沿用部位档案的识别规则）：作业代码 → 照片地点 → 标题/描述里的墩台编号或已知部位名；识别不出返回 None。"""
    body = body if body is not None else (loads(f.get("body_json"), {}) or {})
    geo = None
    if f.get("ref_type") == "media" and f.get("ref_id"):
        m = one(conn, "SELECT geo_place FROM media WHERE id=?", (f["ref_id"],))
        geo = m["geo_place"] if m else None
    return rs.of_code(f.get("activity_code")) or rs.of_label(geo) or rs.of_text(f"{f['title']} {body.get('desc') or ''}")


def _resolver(conn, project_id: int):
    from .parts import Resolver
    return Resolver(conn, project_id)


def _open_safety(conn, project_id: int, exclude: int | None = None) -> list[dict]:
    """14 天内创建、仍未闭环的已确认安全事项（候选、已忽略、已闭环不算）。"""
    since = (date.today() - timedelta(days=SIMILAR_DAYS)).isoformat()
    return all_(conn, f"""SELECT * FROM findings WHERE project_id=? AND agent='safety' AND {CONFIRMED} AND status IN ('open','rectifying','rejected')
                         AND substr(created_at,1,10)>=? AND id<>? ORDER BY id DESC""", (project_id, since, exclude or 0))


def similar_open(conn, f: dict, user: dict | None = None, rs=None) -> dict | None:
    """AC-5.4：同一部位、同一类别、14 天内尚未闭环的隐患 → 提示“可能是同一问题，并入 #N”。部位或类别认不出时不提示。
    分包账号只在本单位范围内找（不向其他单位透露事项）。"""
    body = loads(f.get("body_json"), {}) or {}
    cat = _cat_of(f, body)
    if not cat:
        return None
    rs = rs or _resolver(conn, f["project_id"])
    part = _part_of(conn, rs, f, body)
    if not part:
        return None
    ids = None
    if user is not None and R.is_sub(user["role"]):
        from .role_views import scope_ids
        ids = scope_ids(conn, user)
    for r in _open_safety(conn, f["project_id"], f["id"]):
        if ids is not None and r["assignee_id"] not in ids:
            continue
        rb = loads(r["body_json"], {}) or {}
        if _cat_of(r, rb) == cat and _part_of(conn, rs, r, rb) == part:
            return {"id": r["id"], "title": r["title"], "status_name": status_label(r), "part": part, "category": cat}
    return None


def hints(conn, user: dict, ids: list[int]) -> dict[str, dict]:
    """组件用的提示：每条候选的默认责任人（F-16）与同类合并提示（AC-5.4）。只返回该用户有权决定的候选。"""
    if not ids:
        return {}
    rows = all_(conn, f"SELECT * FROM findings WHERE project_id=? AND id IN ({','.join('?' for _ in ids)})", (user["project_id"], *ids))
    rs = None
    out = {}
    for f in rows:
        if not is_candidate(f) or not can_decide(conn, user, f):
            continue
        rs = rs or _resolver(conn, user["project_id"])
        out[str(f["id"])] = {"default_assignee_id": default_assignee(conn, f), "similar": similar_open(conn, f, user, rs)}
    return out


def _merge_mark(conn, target_id: int, entry: dict, key: str, now: str) -> bool:
    """在目标事项的 body.merged 里记一笔并入来源（按 key 去重）；已记过返回 False。"""
    t = one(conn, "SELECT body_json FROM findings WHERE id=?", (target_id,))
    body = loads(t["body_json"], {}) or {}
    merged = body.setdefault("merged", [])
    if any(m.get("key") == key for m in merged):
        return False
    merged.append({**entry, "key": key, "at": now})
    conn.execute("UPDATE findings SET body_json=?, updated_at=? WHERE id=?", (dumps(body), now, target_id))
    return True


def merge_log_issue(conn, lg: dict, k: int, issue: dict, rs=None) -> int | None:
    """D11 / AC-5.5：日志里提到的安全问题，14 天内已有同一问题未闭环 → 只并入该条（body.merged 记一笔），不新建、不生成候选。
    同一问题：描述相同；或类别相同且部位相同（两边部位都认不出时，须是同一技术员负责的条目）。返回并入的事项 id，没有可并入的返回 None。"""
    desc = re.sub(r"\s+", " ", str(issue.get("desc") or "")).strip()
    if not desc:
        return None
    rs = rs or _resolver(conn, lg["project_id"])
    cat = category_of_text(desc)
    probe = {"title": desc, "body_json": "{}", "activity_code": issue.get("activity_code") or None, "ref_type": "log", "ref_id": lg["id"]}
    part = _part_of(conn, rs, probe, {})
    for r in _open_safety(conn, lg["project_id"]):
        rb = loads(r["body_json"], {}) or {}
        same = _norm(desc) in (_norm(r["title"]), _norm(rb.get("desc")))
        if not same and cat and _cat_of(r, rb) == cat:
            rp = _part_of(conn, rs, r, rb)
            same = (rp == part) if (rp or part) else r["assignee_id"] == lg["author_id"]
        if same:
            _merge_mark(conn, r["id"], {"log_id": lg["id"], "log_date": lg["log_date"], "desc": desc[:120], "by": lg["author_id"]},
                        f"log:{lg['id']}:{k}", now_iso())
            return r["id"]
    return None


def _plan(conn, user: dict, f: dict, it: dict) -> dict:
    action = it.get("action")
    if action == "confirm":
        aid = it.get("assignee_id") or default_assignee(conn, f)
        if not aid:
            raise ValueError("请选择责任人")
        return _plan_confirm(conn, user, f, assignee_id=aid, due_date=it.get("due_date"), edits={k: it.get(k) for k in EDIT_FIELDS})
    if action == "onspot":
        return {"action": "onspot", "f": f, "target_id": _plan_assignee(conn, user, it.get("assignee_id")) or f["assignee_id"]}
    if action == "dismiss":
        code, note = it.get("reason_code") or "", str(it.get("reason_note") or "").strip()
        if code not in DISMISS_REASONS:
            raise ValueError("请选择不采纳原因")
        if code == "other" and not note:
            raise ValueError("选“其他”时请补充说明")
        merge_into = None
        if it.get("merge_into"):
            t = one(conn, f"""SELECT * FROM findings WHERE id=? AND project_id=? AND agent='safety' AND {CONFIRMED}
                              AND status IN ('open','rectifying','rejected')""", (it["merge_into"], user["project_id"]))
            if not t or (R.is_sub(user["role"]) and not can_decide(conn, user, t)):
                raise ValueError("并入的目标须是进行中的已确认隐患")
            merge_into = t["id"]
            note = f"并入 #{merge_into}" + (f"；{note}" if note else "")
        text = DISMISS_REASONS[code] + (f"：{note}" if note else "")
        return {"action": "dismiss", "f": f, "note": text, "merge_into": merge_into}
    raise ValueError("处理方式只能是确认派单、当场已纠正或不采纳")


def _apply_onspot(conn, user: dict, p: dict, now: str) -> None:
    """D6：候选确认即闭环——confirmed_at、resolved_at 同时写入，计入台账（已闭环），标注“当场纠正、无整改照片”，不派单不通知。"""
    f = p["f"]
    body = loads(f["body_json"], {}) or {}
    body["onspot"] = True
    conn.execute("""UPDATE findings SET confirmed_by=?, confirmed_at=?, status='resolved', resolved_at=?, assignee_id=?, body_json=?,
                    feedback_by=?, feedback_note=?, feedback_at=?, updated_at=? WHERE id=?""",
                 (user["id"], now, now, p["target_id"], dumps(body), user["id"], ONSPOT_NOTE, now, now, f["id"]))
    audit(conn, user["id"], "finding.resolved", f"finding:{f['id']}", f"{ONSPOT_NOTE}（AI 候选确认即闭环）")


class BatchRejected(Exception):
    """批量处理整批拒绝：status 400/403，bad 列出有问题的条目 [{id, reason}]。"""

    def __init__(self, status: int, message: str, bad: list[dict]):
        super().__init__(message)
        self.status, self.bad = status, bad


def decide_batch(conn, user: dict, items: list[dict]) -> dict:
    """AC-5.6：批量处理候选（确认派单 / 当场已纠正 / 不采纳）。逐条校验 can_decide 与单位范围；只要混入非候选、已处理、
    跨单位或填写有误的条目，整批拒绝（含跨单位的为 403，其余 400）并列出有问题的条目编号，不部分生效；每条决定单独写审计。
    这是人点按钮后的提交，后端不替人做任何决定。"""
    if not items:
        raise BatchRejected(400, "没有要处理的条目", [])
    if len(items) > BATCH_MAX:
        raise BatchRejected(400, f"一次最多处理 {BATCH_MAX} 条", [])
    bad, plans, seen, forbidden = [], [], set(), False
    for it in items:
        fid = it.get("id")
        if fid in seen:
            bad.append({"id": fid, "reason": "同一条重复提交"})
            continue
        seen.add(fid)
        f = one(conn, "SELECT * FROM findings WHERE id=? AND project_id=?", (fid, user["project_id"])) if isinstance(fid, int) else None
        if not f:
            bad.append({"id": fid, "reason": "记录不存在"})
        elif not is_candidate(f):
            bad.append({"id": fid, "reason": "已处理过，不是待确认的候选"})
        elif not can_decide(conn, user, f):
            bad.append({"id": fid, "reason": "不在本单位范围，无权处理"})
            forbidden = True
        else:
            try:
                plans.append(_plan(conn, user, f, it))
            except ValueError as exc:
                bad.append({"id": fid, "reason": str(exc)})
    if bad:
        msg = "；".join(f"#{b['id']} {b['reason']}" for b in bad[:5]) + ("…" if len(bad) > 5 else "")
        raise BatchRejected(403 if forbidden else 400, f"未处理，请修正后重试：{msg}", bad)
    now = now_iso()
    with tx(conn):
        for p in plans:
            if p["action"] == "confirm":
                _apply_confirm(conn, user, p, now)
            elif p["action"] == "onspot":
                _apply_onspot(conn, user, p, now)
            else:
                _apply_dismiss(conn, user, p["f"]["id"], p["note"], now)
                if p["merge_into"]:
                    _merge_mark(conn, p["merge_into"], {"finding_id": p["f"]["id"], "title": p["f"]["title"][:80], "by": user["id"]},
                                f"finding:{p['f']['id']}", now)
        _notify_dispatch(conn, user, plans)
    return {"decided": len(plans), "items": [{"id": p["f"]["id"], "action": p["action"]} for p in plans]}


def candidate_count(conn, project_id: int, user: dict | None = None) -> int:
    """待确认候选数。分包安全员只数本单位范围；港湾有 site_safety 权限的岗位数全项目；其他岗位为 0。"""
    if user is not None and not R.can(user["role"], "site_safety"):
        if user["role"] != "sub_safety":
            return 0
        from .role_views import scope_ids
        ids = scope_ids(conn, user) or [user["id"]]
        return one(conn, f"SELECT COUNT(*) n FROM findings WHERE project_id=? AND confirmed_at IS NULL AND status='open' AND assignee_id IN ({','.join('?' for _ in ids)})",
                   (project_id, *ids))["n"]
    return one(conn, "SELECT COUNT(*) n FROM findings WHERE project_id=? AND confirmed_at IS NULL AND status='open'", (project_id,))["n"]
