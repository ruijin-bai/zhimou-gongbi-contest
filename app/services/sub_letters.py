"""致分包文书（工笔文书方案 2026-10-08 一.3）：工作联系单 work_contact、致分包函 letter_to_sub。

定位：已有派单的正式书面形式，不是新流程。
- 必须挂一条已有记录：已确认的安全隐患（候选不行）、质量问题、已确认属实的进度预警；致分包函挂“某单位 + 某类问题”；
- 签发：安全类由安全主管，进度、质量类由工程主管；致分包函由主管起草、项目经理确认后才算正式；
- 接收：安全类 → 该单位分包安全员；进度、质量类 → 该单位负责该作业的技术员（按 WBS 范围匹配，匹配不到发给该单位所有技术员）；
  进度类不发给分包安全员（D9）；致分包函抄送该单位全部账号；
- 确认下发 = 文书冻结 + 在分包账号的待办和消息里出现。系统不对外自动发送；需要纸质版的导出 Word 盖章；
- 分包签收（acked_at）→ 回复：安全、质量类沿用整改照片 + 复核闭环；进度类填“措施 + 预计完成日期”，工程主管接受或退回（退回必须填原因）。
  接受回复只代表收到措施，不改任何进度数据；
- 未签收超过 24 小时、或进度类过了回复期限，进入催办链（services/nudge.py，进度类止于工程主管）；
- 不写罚款、扣款金额和处罚结论。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .. import roles as R
from ..db import all_, audit, dumps, insert, loads, now_iso, one

SCHEMA = """
CREATE TABLE IF NOT EXISTS sub_letters (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    doc_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    area TEXT NOT NULL,
    src_type TEXT NOT NULL,
    src_id INTEGER,
    company TEXT NOT NULL DEFAULT '',
    to_user_ids TEXT NOT NULL DEFAULT '[]',
    cc_user_ids TEXT NOT NULL DEFAULT '[]',
    due_date TEXT,
    issued_by INTEGER,
    issued_at TEXT,
    acked_by INTEGER,
    acked_at TEXT,
    reply_text TEXT,
    reply_date TEXT,
    reply_by INTEGER,
    reply_at TEXT,
    closed_by INTEGER,
    closed_at TEXT,
    return_reason TEXT,
    status TEXT NOT NULL DEFAULT 'issued' CHECK (status IN ('issued','acked','replied','returned','closed'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sub_letters_doc ON sub_letters(doc_id);
CREATE INDEX IF NOT EXISTS ix_sub_letters ON sub_letters(project_id, status);
"""

AREA_NAME = {"safety": "安全", "quality": "质量", "progress": "进度"}
ISSUER = {"safety": frozenset({"safety_sup"}), "quality": frozenset({"eng_sup"}), "progress": frozenset({"eng_sup"})}
BASIS = {"safety": "依据：项目安全管理规定", "quality": "依据：项目质量管理规定", "progress": "依据：项目进度计划（P6）及分包合同进度要求"}
PROGRESS_REPLY_DAYS = 3
ACK_HOURS = 24
STATUS_NAME = {"issued": "待签收", "acked": "已签收", "replied": "已回复", "returned": "回复被退回", "closed": "已销项"}
REPEAT_DAYS = 30


def basis(conn, pid: int, area: str) -> str:
    """依据条款名：项目在知识库里配置了（kind='letter_basis' key=area）就用，否则写通用依据，不编条款号。"""
    row = one(conn, "SELECT value_json FROM knowledge WHERE project_id=? AND kind='letter_basis' AND key=?", (pid, area))
    v = (loads(row["value_json"], {}) or {}).get("text") if row else None
    return f"依据：{v}" if v else BASIS[area]


def _users(conn, pid: int) -> list[dict]:
    return all_(conn, "SELECT id, name, role, company, wbs_scope, active FROM users WHERE project_id=? AND active=1", (pid,))


def recipients(conn, pid: int, area: str, company: str, activity_code: str | None) -> list[dict]:
    us = [u for u in _users(conn, pid) if (u["company"] or "") == company]
    if area == "safety":
        return [u for u in us if u["role"] == "sub_safety"]
    techs = [u for u in us if u["role"] == "tech"]
    if activity_code:
        from . import p6
        imp = p6.current_import(conn, pid)
        act = one(conn, "SELECT * FROM activities WHERE import_id=? AND code=?", (imp["id"], activity_code)) if imp else None
        hit = [u for u in techs if act and (u["wbs_scope"] or "").strip() and p6.in_scope(act, u["wbs_scope"])]
        if hit:
            return hit
    return techs


def _photos(f: dict, body: dict) -> list[int]:
    ids = [body.get("media_id")] if body.get("media_id") else []
    if f.get("ref_type") == "media" and f.get("ref_id"):
        ids.append(f["ref_id"])
    return sorted({int(i) for i in ids if i})


def _company_of(conn, uid: int | None) -> str:
    u = one(conn, "SELECT company FROM users WHERE id=?", (uid,)) if uid else None
    return (u or {}).get("company") or ""


def source_facts(conn, pid: int, ref: str) -> dict:
    """工作联系单的事实：ref = 'finding:<id>' 或 'alert:<id>'（可附 '|单位'，进度预警匹配不到单位时指定）。"""
    typ, _, rest = ref.partition(":")
    sid, _, company_hint = rest.partition("|")
    if not sid.isdigit():
        raise ValueError("请选择一条隐患、质量问题或进度预警")
    project = one(conn, "SELECT name FROM projects WHERE id=?", (pid,))["name"]
    if typ == "finding":
        f = one(conn, "SELECT * FROM findings WHERE id=? AND project_id=?", (int(sid), pid))
        if not f or f["agent"] not in ("safety", "quality") or f["kind"] == "qc_reminder":
            raise LookupError("隐患或质量问题不存在")
        if not f["confirmed_at"]:
            raise ValueError("AI 候选隐患尚未确认，不能下发工作联系单")
        if f["status"] in ("resolved", "dismissed"):
            raise ValueError("该问题已闭环，无需下发")
        body = loads(f["body_json"], {}) or {}
        area = f["agent"]
        company = _company_of(conn, f["assignee_id"]) or company_hint
        if not company:
            raise ValueError("该问题还没有派给分包单位，请先派单")
        req = body.get("measure") or body.get("suggestion") or "请按整改要求落实整改，上传整改照片"
        return {"project": project, "src_type": "finding", "src_id": f["id"], "area": area, "area_name": AREA_NAME[area], "company": company,
                "title": f["title"], "level": f["level"], "category": body.get("category") or "", "desc": body.get("desc") or f["title"], "scene": body.get("scene") or "",
                "date": f["created_at"][:10], "activity_code": f["activity_code"] or "", "photos": _photos(f, body), "log_id": body.get("log_id"),
                "requirement": req, "due_date": f["due_date"][:10] if f["due_date"] else None, "ref_tag": f"S{f['id']}" if area == "safety" else f"Q{f['id']}",
                "basis": basis(conn, pid, area)}
    if typ == "alert":
        a = one(conn, "SELECT * FROM alerts WHERE id=? AND project_id=?", (int(sid), pid))
        if not a:
            raise LookupError("进度预警不存在")
        if a["status"] != "confirmed":
            raise ValueError("进度预警须先由工程主管确认属实，才能下发工作联系单")
        d = loads(a["detail_json"], {}) or {}
        techs = [u for u in _users(conn, pid) if u["role"] == "tech"]
        from . import p6
        imp = p6.current_import(conn, pid)
        act = one(conn, "SELECT * FROM activities WHERE import_id=? AND code=?", (imp["id"], a["activity_code"])) if imp else None
        cos = sorted({u["company"] for u in techs if u["company"] and act and (u["wbs_scope"] or "").strip() and p6.in_scope(act, u["wbs_scope"])})
        company = company_hint or (cos[0] if len(cos) == 1 else "")
        if not company:
            raise ValueError("未能按 WBS 范围确定负责该作业的分包单位，请指定单位" if not cos else "该作业涉及多个分包单位，请指定单位：" + "、".join(cos))
        due = (date.today() + timedelta(days=PROGRESS_REPLY_DAYS)).isoformat()
        return {"project": project, "src_type": "alert", "src_id": a["id"], "area": "progress", "area_name": "进度", "company": company,
                "title": a["title"], "level": a["level"], "activity_code": a["activity_code"], "activity_name": a["activity_name"],
                "planned_pct": d.get("planned_pct"), "actual_pct": d.get("actual_pct"), "slip_days": d.get("slip_days"), "early_finish": d.get("early_finish"),
                "forecast_finish": d.get("forecast_finish"), "date": a["first_detected"][:10], "photos": [],
                "requirement": f"请于 {due} 前书面回复赶工措施及预计完成日期", "due_date": due, "ref_tag": f"W{a['id']}", "basis": basis(conn, pid, "progress")}
    raise ValueError("请选择一条隐患、质量问题或进度预警")


def company_facts(conn, pid: int, ref: str) -> dict:
    """致分包函的事实：ref = '<单位>|<safety|quality|progress>'。近 30 天该单位同类问题 + 超期未回复的联系单。"""
    company, _, area = ref.partition("|")
    if area not in AREA_NAME or not company:
        raise ValueError("请选择分包单位与问题类别")
    project = one(conn, "SELECT name FROM projects WHERE id=?", (pid,))["name"]
    since = (date.today() - timedelta(days=REPEAT_DAYS)).isoformat()
    probs = []
    if area in ("safety", "quality"):
        for f in all_(conn, """SELECT f.* FROM findings f JOIN users u ON u.id=f.assignee_id WHERE f.project_id=? AND f.agent=? AND f.kind<>'qc_reminder'
                               AND f.confirmed_at IS NOT NULL AND u.company=? AND substr(f.created_at,1,10)>=? ORDER BY f.id""", (pid, area, company, since)):
            body = loads(f["body_json"], {}) or {}
            probs.append({"ref": ("S" if area == "safety" else "Q") + str(f["id"]), "date": f["created_at"][:10], "title": f["title"], "category": body.get("category") or "",
                          "status": {"resolved": "已闭环", "dismissed": "已忽略"}.get(f["status"], "未闭环")})
    letters = [x for x in all_(conn, "SELECT * FROM sub_letters WHERE project_id=? AND company=? AND area=? AND kind='work_contact'", (pid, company, area))
               if x["status"] in ("issued", "acked", "returned") and x["due_date"] and x["due_date"] < date.today().isoformat()]
    if not probs and not letters:
        raise ValueError(f"近 {REPEAT_DAYS} 天{company}没有{AREA_NAME[area]}类问题记录，也没有超期未回复的联系单")
    cats: dict[str, int] = {}
    for p in probs:
        cats[p["category"] or p["title"][:12]] = cats.get(p["category"] or p["title"][:12], 0) + 1
    due = (date.today() + timedelta(days=PROGRESS_REPLY_DAYS)).isoformat()
    return {"project": project, "src_type": "company", "src_id": None, "area": area, "area_name": AREA_NAME[area], "company": company, "since": since, "days": REPEAT_DAYS,
            "problems": probs, "n_problems": len(probs), "repeat": {k: v for k, v in cats.items() if v >= 2}, "n_open": sum(1 for p in probs if p["status"] == "未闭环"),
            "overdue_letters": [{"doc_id": x["doc_id"], "issued": (x["issued_at"] or "")[:10], "due": x["due_date"], "status": STATUS_NAME[x["status"]]} for x in letters],
            "requirement": f"请于 {due} 前书面回复整改措施与防止再次发生的安排", "due_date": due, "basis": basis(conn, pid, area)}


# ----------------------------------------------------------------------------- 下发、签收、回复、销项

def issue(conn, user: dict, doc: dict, facts: dict) -> dict:
    """文书确认时调用：建 sub_letters 记录并给接收人发站内消息（不对外发送）。"""
    pid = user["project_id"]
    area, company = facts["area"], facts["company"]
    to = recipients(conn, pid, area, company, facts.get("activity_code"))
    if not to:
        raise ValueError(f"{company}没有可接收的{'分包安全员' if area == 'safety' else '技术员'}账号")
    cc = [u for u in _users(conn, pid) if (u["company"] or "") == company and R.is_sub(u["role"]) and u["id"] not in {x["id"] for x in to}] \
        if doc["kind"] == "letter_to_sub" else []
    if area == "progress":
        cc = [u for u in cc if u["role"] != "sub_safety"]   # D9：进度类不发给分包安全员
    lid = insert(conn, "sub_letters", {"project_id": pid, "doc_id": doc["id"], "kind": doc["kind"], "area": area, "src_type": facts["src_type"], "src_id": facts.get("src_id"),
                                       "company": company, "to_user_ids": dumps([u["id"] for u in to]), "cc_user_ids": dumps([u["id"] for u in cc]),
                                       "due_date": facts.get("due_date"), "issued_by": user["id"], "issued_at": now_iso(), "status": "issued"})
    name = "工作联系单" if doc["kind"] == "work_contact" else "项目部函件"
    for u in to + cc:
        insert(conn, "notifications", {"project_id": pid, "user_id": u["id"], "alert_id": None, "title": f"收到{name}：{doc['title'][:40]}",
                                       "body": f"请打开查看并签收。{facts.get('requirement') or ''}"[:200], "channels_json": dumps({"inapp": "sent"}),
                                       "created_at": now_iso(), "link_json": dumps({"route": "letters", "params": {"id": lid}})})
    audit(conn, user["id"], "letter.issue", f"letter:{lid}", f"{doc['kind']} {company} {area}")
    return one(conn, "SELECT * FROM sub_letters WHERE id=?", (lid,))


def _addressed(row: dict, uid: int) -> bool:
    return uid in loads(row["to_user_ids"], []) or uid in loads(row["cc_user_ids"], [])


def _letter(conn, user: dict, lid: int) -> dict:
    row = one(conn, "SELECT * FROM sub_letters WHERE id=? AND project_id=?", (lid, user["project_id"]))
    if not row:
        raise LookupError("联系单不存在")
    if R.is_sub(user["role"]) and not _addressed(row, user["id"]):
        raise LookupError("联系单不存在")
    return row


def ack(conn, user: dict, lid: int) -> dict:
    row = _letter(conn, user, lid)
    if user["id"] not in loads(row["to_user_ids"], []) + loads(row["cc_user_ids"], []):
        raise PermissionError("只有接收人可以签收")
    if not row["acked_at"]:
        conn.execute("UPDATE sub_letters SET acked_by=?, acked_at=?, status=CASE WHEN status='issued' THEN 'acked' ELSE status END WHERE id=?", (user["id"], now_iso(), lid))
        audit(conn, user["id"], "letter.ack", f"letter:{lid}")
    return view(conn, user, lid)


def reply(conn, user: dict, lid: int, text: str, finish_date: str) -> dict:
    row = _letter(conn, user, lid)
    if user["id"] not in loads(row["to_user_ids"], []):
        raise PermissionError("只有接收人可以回复")
    if row["area"] != "progress":
        raise ValueError("安全、质量类请在整改单上传整改照片并说明，按整改复核闭环")
    if row["status"] == "closed":
        raise ValueError("联系单已销项")
    text = (text or "").strip()
    if len(text) < 4:
        raise ValueError("请写明赶工措施")
    date.fromisoformat(finish_date)
    conn.execute("UPDATE sub_letters SET reply_text=?, reply_date=?, reply_by=?, reply_at=?, acked_by=COALESCE(acked_by, ?), acked_at=COALESCE(acked_at, ?), "
                 "status='replied', return_reason=NULL WHERE id=?", (text[:600], finish_date, user["id"], now_iso(), user["id"], now_iso(), lid))
    audit(conn, user["id"], "letter.reply", f"letter:{lid}", text[:120])
    return view(conn, user, lid)


def review(conn, user: dict, lid: int, accept: bool, reason: str = "") -> dict:
    """进度类回复：工程主管接受（销项，只代表收到措施，不改任何进度数据）或退回（必须填原因）。"""
    row = _letter(conn, user, lid)
    if user["role"] != "eng_sup":
        raise PermissionError("进度类回复由工程主管接受或退回")
    if row["area"] != "progress" or row["status"] != "replied":
        raise ValueError("当前没有待处理的回复")
    if accept:
        conn.execute("UPDATE sub_letters SET status='closed', closed_by=?, closed_at=? WHERE id=?", (user["id"], now_iso(), lid))
    else:
        if not (reason or "").strip():
            raise ValueError("退回须填写原因")
        conn.execute("UPDATE sub_letters SET status='returned', return_reason=? WHERE id=?", (reason.strip()[:300], lid))
        for uid in loads(row["to_user_ids"], []):
            insert(conn, "notifications", {"project_id": row["project_id"], "user_id": uid, "alert_id": None, "title": "联系单回复被退回",
                                           "body": f"退回原因：{reason.strip()[:120]}", "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(),
                                           "link_json": dumps({"route": "letters", "params": {"id": lid}})})
    audit(conn, user["id"], "letter.accept" if accept else "letter.return", f"letter:{lid}", reason[:120])
    return view(conn, user, lid)


def state(conn, row: dict) -> dict:
    """安全、质量类的销项看整改单：整改单闭环即销项（沿用复核闭环）。"""
    st = row["status"]
    fin = None
    if row["src_type"] == "finding" and row["src_id"]:
        fin = one(conn, "SELECT id, status, rectify_media_id, resolved_at FROM findings WHERE id=?", (row["src_id"],))
        if fin and fin["status"] == "resolved":
            st = "closed"
        elif fin and fin["status"] == "rectifying" and st in ("issued", "acked"):
            st = "replied"
    if row["src_type"] in ("precontrol", "docdebt") and row["acked_at"] and st != "closed":
        st = "closed"   # 预控提醒、资料补齐通知：签收即销项（落实情况由放行清单、资料欠账清单本身跟进）
    overdue = bool(row["due_date"] and row["due_date"] < date.today().isoformat() and st in ("issued", "acked", "returned"))
    ack_late = not row["acked_at"] and row["issued_at"] and datetime.fromisoformat(row["issued_at"][:19]) < datetime.now() - timedelta(hours=ACK_HOURS)
    return {"status": st, "status_name": STATUS_NAME[st], "overdue": overdue, "ack_late": bool(ack_late), "finding": fin}


def view(conn, user: dict, lid: int) -> dict:
    row = _letter(conn, user, lid)
    from . import gongbi_docs
    d = gongbi_docs.view(conn, one(conn, "SELECT * FROM gongbi_docs WHERE id=?", (row["doc_id"],)), user["role"])
    names = {u["id"]: u for u in all_(conn, "SELECT id, name, role FROM users WHERE project_id=?", (user["project_id"],))}
    st = state(conn, row)
    mine = user["id"] in loads(row["to_user_ids"], [])
    return {"id": row["id"], "doc": d, "kind": row["kind"], "area": row["area"], "area_name": AREA_NAME[row["area"]], "company": row["company"], "due_date": row["due_date"],
            "issued_at": row["issued_at"], "acked_at": row["acked_at"], "reply_text": row["reply_text"], "reply_date": row["reply_date"], "reply_at": row["reply_at"],
            "return_reason": row["return_reason"], "closed_at": row["closed_at"], **st,
            "to": [R.role_name(names[i]["role"]) + " " + names[i]["name"] for i in loads(row["to_user_ids"], []) if i in names],
            "cc": len(loads(row["cc_user_ids"], [])), "src_type": row["src_type"], "src_id": row["src_id"],
            "can_ack": _addressed(row, user["id"]) and not row["acked_at"], "can_reply": mine and row["area"] == "progress" and st["status"] in ("issued", "acked", "returned"),
            "can_review": user["role"] == "eng_sup" and row["area"] == "progress" and st["status"] == "replied"}


def listing(conn, user: dict) -> list[dict]:
    rows = all_(conn, "SELECT * FROM sub_letters WHERE project_id=? ORDER BY id DESC LIMIT 200", (user["project_id"],))
    if R.is_sub(user["role"]):
        rows = [r for r in rows if _addressed(r, user["id"])]
        if user["role"] == "sub_safety":
            rows = [r for r in rows if r["area"] != "progress"]
    elif user["role"] not in ("manager", "eng_sup", "safety_sup", "leader"):
        return []
    docs = {d["id"]: d for d in all_(conn, f"SELECT id, title, version FROM gongbi_docs WHERE id IN ({','.join(str(r['doc_id']) for r in rows) or 'NULL'})")}
    out = []
    for r in rows:
        st = state(conn, r)
        out.append({"id": r["id"], "doc_id": r["doc_id"], "title": (docs.get(r["doc_id"]) or {}).get("title", ""), "kind": r["kind"], "area": r["area"],
                    "area_name": AREA_NAME[r["area"]], "company": r["company"], "issued_at": r["issued_at"], "due_date": r["due_date"], "acked_at": r["acked_at"], **{k: st[k] for k in ("status", "status_name", "overdue", "ack_late")}})
    return out


def pending_for(conn, user: dict) -> list[dict]:
    """待办用：分包看“待签收 / 待回复”；工程主管看“进度类回复待接受”，签发人看“超 24 小时未签收”。"""
    out = []
    for r in all_(conn, "SELECT * FROM sub_letters WHERE project_id=? AND status<>'closed' ORDER BY id", (user["project_id"],)):
        st = state(conn, r)
        if st["status"] == "closed":
            continue
        if R.is_sub(user["role"]) and user["id"] in loads(r["to_user_ids"], []):
            if not r["acked_at"]:
                out.append({**r, **st, "todo": "ack"})
            elif r["area"] == "progress" and st["status"] in ("acked", "returned"):
                out.append({**r, **st, "todo": "reply"})
        elif user["role"] == "eng_sup" and r["area"] == "progress" and st["status"] == "replied":
            out.append({**r, **st, "todo": "review"})
        elif r["issued_by"] == user["id"] and st["ack_late"]:
            out.append({**r, **st, "todo": "ack_late"})
    return out


def nudge_targets(conn, pid: int, today: date) -> list[dict]:
    """催办链用：未签收超过 24 小时，或进度类过了回复期限仍未回复的联系单。责任人取第一位接收人。"""
    out = []
    for r in all_(conn, "SELECT * FROM sub_letters WHERE project_id=? AND status IN ('issued','acked','returned')", (pid,)):
        st = state(conn, r)
        to = loads(r["to_user_ids"], [])
        if st["status"] == "closed" or not to:
            continue
        if not r["acked_at"]:
            due = date.fromisoformat(r["issued_at"][:10]) + timedelta(days=1)
            what = "未签收"
        elif r["area"] == "progress" and r["due_date"]:
            due = date.fromisoformat(r["due_date"])
            what = "未回复"
        else:
            continue   # 安全、质量类签收后由整改单本身催办
        if today < due:
            continue
        lvl = (one(conn, "SELECT level FROM findings WHERE id=?", (r["src_id"],)) or {}).get("level") if r["src_type"] == "finding" else None
        out.append({"key": str(r["id"]), "area": "letter_progress" if r["area"] == "progress" else r["area"], "level": lvl, "subject": f"工作联系单{what}（{r['company']}）",
                    "due": due, "owner_id": to[0], "link": f"letters?id={r['id']}", "ref_id": r["id"]})
    return out
