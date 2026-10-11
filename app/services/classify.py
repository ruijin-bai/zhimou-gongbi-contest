"""随手记归类建议：一个入口（说/打/拍），AI 判断这条内容像什么、该去哪。

- 只建议，不自动入账。默认就是普通进度记录（不出建议条）；只有线索足够明确时才给出一条建议：
  安全隐患 / 材料·混凝土小票 / 隐蔽验收照片。
- 线索来源（由强到弱）：安全Agent 对照片已有的识别结果 → 文字关键词规则 → （仅真实模型、仅纯照片无文字时）一次轻量视觉分类。
  离线模拟模型下只用规则；拿不准就不给建议。
- 人点“是”才转入对应既有流程，且只生成草稿：票据进材料Agent 草稿（人确认后才计入）、隐蔽验收进资料Agent 草稿、
  隐患走安全Agent 识别派单；原随手记文字与照片不动。点“不是”只记一笔，之后不再提示。
- 决定（接受/忽略）记在 note_classify；视觉分类结果也缓存在此，同一条随手记最多调用一次模型。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from ..ai.client import AIError, call_json, provider_status
from ..config import settings
from ..db import all_, audit, connect, dumps, insert, loads, now_iso, one
from . import conceal as conceal_svc
from . import media as media_svc
from . import parts as part_svc

log = logging.getLogger("zhimou.classify")

PROMPT_VERSION = "classify-v1-20261006"
KIND_NAME = {"hazard": "安全隐患", "ticket": "材料/混凝土小票", "conceal": "隐蔽验收照片"}
ROLES = frozenset({"tech"})

# ----------------------------------------------------------------------------- 文字规则

_GRADE = re.compile(r"[Cc]\s?\d{2}")
_VOLUME = re.compile(r"\d+(?:\.\d+)?\s*(?:方|立方|m3|m³|㎥)")
_NEG_HAZARD = re.compile(r"(无|没有|未发现|不存在|已消除|已整改|整改完成|整改完毕)\s*(安全)?隐患|隐患\s*(已|均已)?(整改|消除|闭环)")

TICKET_STRONG = ("小票", "送货单", "发货单", "进场单", "出厂合格证", "合格证", "质保书", "质量证明", "检测报告", "坍落度", "到货单", "过磅单")
TICKET_MATERIAL = ("钢筋", "水泥", "砂", "碎石", "石子", "外加剂", "钢材", "砼", "混凝土", "商砼", "商品混凝土", "预制", "管材", "钢绞线")
TICKET_ARRIVE = ("进场", "到场", "到货", "入场", "卸车", "送到")
CONCRETE_WORDS = ("砼", "混凝土", "坍落度", "方量", "商砼")
CONCEAL_STRONG = ("隐蔽", "验槽", "报验", "验收")
CONCEAL_SUBJECT = ("钢筋", "模板", "基槽", "基坑", "桩基", "预埋", "防水", "钢筋笼", "垫块")
CONCEAL_SHOT = ("保护层", "间距", "搭接", "锚固", "垫块", "垂直度", "拼缝", "标高", "持力层")
HAZARD_WORDS = ("隐患", "没戴", "未戴", "不戴", "没系", "未系", "不系", "没有防护", "无防护", "没防护", "未防护", "临边", "洞口", "违章", "违规",
                "漏电", "裸露", "无护栏", "没护栏", "缺护栏", "坍塌", "未佩戴", "没佩戴", "乱接", "吊物下", "危险")
HAZARD_PPE = ("安全帽", "安全带", "反光衣", "防护")


def _hits(text: str, words) -> list[str]:
    return [w for w in words if w in text]


def text_signals(text: str) -> list[dict]:
    """文字线索 → [{kind, score, reason, doc_kind}]，按得分降序；没有线索返回空列表（即普通进度记录）。"""
    t = (text or "").strip()
    if not t:
        return []
    out: list[dict] = []
    # 安全隐患：隐患/违章类词，或“没戴/未系 + 安全帽/安全带”；明确写了“无隐患/已整改”的不算
    hz = _hits(t, HAZARD_WORDS)
    if hz and not _NEG_HAZARD.search(t):
        ppe = _hits(t, HAZARD_PPE)
        score = 2 + (1 if ppe else 0) + (1 if "隐患" in hz else 0)
        out.append({"kind": "hazard", "score": score, "reason": "文字提到：" + "、".join((hz + ppe)[:4])})
    # 材料/混凝土小票：强线索（小票/合格证…）；或 材料词 + 进场词；或 强度等级 + 方量 + 进场/到场
    st = _hits(t, TICKET_STRONG)
    arrive = _hits(t, TICKET_ARRIVE)
    mats = _hits(t, TICKET_MATERIAL)
    conc = bool(_GRADE.search(t) and _VOLUME.search(t))
    if st or (arrive and mats) or (conc and arrive):
        kind = "concrete_ticket" if (_hits(t, CONCRETE_WORDS) or conc) and "合格证" not in t else "cert" if ("合格证" in t or "质保书" in t or "质量证明" in t) else "material_receipt"
        out.append({"kind": "ticket", "score": 3 if st else 2, "doc_kind": kind,
                    "reason": "文字提到：" + "、".join((st + arrive + mats)[:4])})
    # 隐蔽验收：隐蔽/验槽/报验/验收 + 隐蔽工程对象；或 隐蔽对象 + 验收照片常拍的细节
    cs, sub, shot = _hits(t, CONCEAL_STRONG), _hits(t, CONCEAL_SUBJECT), _hits(t, CONCEAL_SHOT)
    if ("隐蔽" in t or "验槽" in t) or (cs and sub) or (sub and shot):
        out.append({"kind": "conceal", "score": 3 if ("隐蔽" in t or "验槽" in t) else 2, "reason": "文字提到：" + "、".join((cs + sub + shot)[:4])})
    order = {"hazard": 0, "ticket": 1, "conceal": 2}
    out.sort(key=lambda x: (-x["score"], order[x["kind"]]))
    return out


# ----------------------------------------------------------------------------- 一条随手记的上下文

def _note(conn, user: dict, nid: int) -> dict | None:
    return one(conn, "SELECT * FROM notes WHERE id=? AND author_id=? AND project_id=?", (nid, user["id"], user["project_id"]))


def _media(conn, note: dict) -> list[dict]:
    ids = [int(i) for i in loads(note["media_json"], []) or []]
    if not ids:
        return []
    rows = {m["id"]: m for m in all_(conn, f"SELECT * FROM media WHERE id IN ({','.join('?' * len(ids))})", ids)}
    return [rows[i] for i in ids if i in rows]


def _state(conn, nid: int) -> dict | None:
    return one(conn, "SELECT * FROM note_classify WHERE note_id=?", (nid,))


def _upsert(conn, note: dict, **cols) -> None:
    if _state(conn, note["id"]):
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE note_classify SET {sets} WHERE note_id=?", (*cols.values(), note["id"]))
    else:
        insert(conn, "note_classify", {"note_id": note["id"], "project_id": note["project_id"], "user_id": note["author_id"],
                                       "created_at": now_iso(), **cols})


def _hazard_findings(conn, media: list[dict]) -> list[dict]:
    ids = [m["id"] for m in media]
    if not ids:
        return []
    rows = all_(conn, f"SELECT id, title, level, status FROM findings WHERE agent='safety' AND ref_type='media' AND ref_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids)
    return [{"id": r["id"], "title": r["title"], "level": r["level"], "status": r["status"]} for r in rows]


def _sug(kind: str, source: str, reason: str, *, needs_photo: bool = False, doc_kind: str | None = None, found: list[dict] | None = None) -> dict:
    name = KIND_NAME[kind]
    if kind == "ticket":
        label = {"concrete_ticket": "混凝土小票", "material_receipt": "材料进场单", "cert": "合格证"}.get(doc_kind or "", "材料/混凝土小票")
        q = f"像是{label} → 识别为材料草稿？"
    elif kind == "conceal":
        q = "像是隐蔽验收照片 → 归入隐蔽验收草稿？"
    elif found:
        q = f"安全Agent在照片里发现 {len(found)} 项隐患 → 查看隐患单？"
    elif needs_photo:
        q = "像是安全隐患 → 拍一张照片，交给安全Agent识别并派单？"
    else:
        q = "像是安全隐患 → 交给安全Agent识别并派单？"
    return {"kind": kind, "kind_name": name, "question": q, "reason": reason, "source": source, "needs_photo": needs_photo,
            "doc_kind": doc_kind, "findings": found or []}


def _vision_usable() -> bool:
    return settings.ai_provider != "mock" and bool(provider_status().get("ready"))


def suggest(conn, note: dict) -> dict:
    """返回 {note_id, state, suggestion?, result?}。state：none / suggested / pending（视觉分类中）/ accepted / dismissed。"""
    st = _state(conn, note["id"])
    base = {"note_id": note["id"], "state": "none"}
    if st and st["decision"] == "dismissed":
        return {**base, "state": "dismissed"}
    if st and st["decision"] == "accepted":
        return {**base, "state": "accepted", "result": result_of(conn, st)}
    media = _media(conn, note)
    text = note["text"] or ""
    sigs = text_signals(text)
    statuses = [m["safety_status"] for m in media]
    # 1) 照片已被安全Agent识别出隐患：最强线索
    if "hazard" in statuses:
        return {**base, "state": "suggested", "suggestion": _sug("hazard", "safety", "安全Agent 已在照片中识别到隐患", found=_hazard_findings(conn, media))}
    # 2) 文字规则。照片都被识别为“未见隐患”时，不凭文字再建议隐患（拿不准就不提示）
    scanned_clear = bool(media) and all(s == "clear" for s in statuses)
    for s in sigs:
        if s["kind"] == "hazard" and scanned_clear:
            continue
        if s["kind"] in ("ticket", "conceal") and not media:
            continue  # 小票、验收照片需要有照片才能转草稿；纯文字不建议
        return {**base, "state": "suggested", "suggestion": _sug(s["kind"], "rule", s["reason"], needs_photo=(s["kind"] == "hazard" and not media), doc_kind=s.get("doc_kind"))}
    # 3) 纯照片（无文字线索）：真实模型下做一次轻量视觉分类（缓存），其余情况不给建议
    if media and not sigs and len(text.strip()) <= 8:
        if st and st["vision_status"] == "done" and st["vision_kind"] in ("ticket", "conceal"):
            return {**base, "state": "suggested", "suggestion": _sug(st["vision_kind"], "vision", "AI 看图判断：" + (st["vision_reason"] or "像是该类单据/资料"))}
        if st and st["vision_status"] == "pending" and (st["vision_at"] or "") > (datetime.now().astimezone() - timedelta(minutes=5)).isoformat():
            return {**base, "state": "pending"}
        if not st or not st["vision_status"]:
            if _vision_usable() and media[0].get("purpose") == "log":
                return {**base, "state": "pending", "need_vision": True}
    return base


def result_of(conn, st: dict) -> dict:
    """接受后的去向，供前端显示“已转入…”与可点链接。"""
    t, i = st["target_type"], st["target_id"]
    if t == "ticket":
        return {"kind": "ticket", "label": "已转入材料草稿，待你确认", "route": {"name": "tickets", "params": {"tab": "pending"}}, "target_id": i}
    if t == "conceal":
        return {"kind": "conceal", "label": "已转入隐蔽验收草稿，待补资料", "route": {"name": "conceal", "params": {"id": i}}, "target_id": i}
    if t == "finding":
        return {"kind": "hazard", "label": "安全Agent 已生成隐患单", "route": {"name": "finding", "params": {"id": i}}, "target_id": i}
    if t == "hazard_pending":
        return {"kind": "hazard", "label": "已交给安全Agent 识别，结果会出现在下方待办里", "route": None, "target_id": None}  # 首页待办就在本页，不跳重复的“我的待办”页
    return {"kind": st["decided_kind"] or "", "label": "已转入", "route": None, "target_id": i}


def batch(conn, user: dict, ids: list[int] | None = None) -> list[dict]:
    """今天（或指定）的随手记的归类建议；纯照片需要视觉分类的返回 state=pending 并带 need_vision，由路由丢到后台。"""
    if ids is None:
        rows = all_(conn, "SELECT * FROM notes WHERE author_id=? AND project_id=? AND note_date=? ORDER BY id", (user["id"], user["project_id"], datetime.now().date().isoformat()))
    else:
        ids = [int(i) for i in ids][:60]
        rows = [r for r in (_note(conn, user, i) for i in ids) if r]
    return [suggest(conn, n) for n in rows]


# ----------------------------------------------------------------------------- 视觉分类（真实模型、纯照片、仅一次）

VISION_SYSTEM = """你是施工现场资料分类员。看这张现场照片，判断它最像哪一类：
ticket：混凝土发货小票、材料进场/送货单、出厂合格证、检测报告等纸质单据（画面以单据文字为主）
conceal：钢筋、模板、基槽、桩基、预埋、防水等隐蔽工程被覆盖前的验收拍照（可见钢筋绑扎、模板、基坑等，常带标尺、标识牌）
progress：其他普通施工进度/现场照片
只依据画面本身；拿不准一律选 progress，不要猜。
只输出JSON：{"kind":"ticket|conceal|progress","confidence":"high|medium|low","reason":"一句话依据(20字内)"}"""


def _vision_norm(d) -> dict:
    if not isinstance(d, dict) or d.get("kind") not in ("ticket", "conceal", "progress"):
        raise ValueError("kind 不合法")
    return {"kind": d["kind"], "confidence": d.get("confidence") if d.get("confidence") in ("high", "medium", "low") else "low", "reason": str(d.get("reason") or "")[:40]}


def vision_classify(note_id: int) -> None:
    """后台：对一条纯照片随手记做一次视觉分类，结果写缓存。失败只记失败，不给建议、不重试。"""
    conn = connect()
    try:
        note = one(conn, "SELECT * FROM notes WHERE id=?", (note_id,))
        if not note:
            return
        st = _state(conn, note_id)
        if st and st["vision_status"]:
            return
        _upsert(conn, note, vision_status="pending", vision_at=now_iso())
        media = _media(conn, note)
        if not media:
            _upsert(conn, note, vision_status="failed")
            return
        try:
            res = call_json("note.classify", system=VISION_SYSTEM, user="请判断这张照片的类别。", prompt_version=PROMPT_VERSION,
                            images=[str(settings.media_dir / media[0]["ai_file"])], mock=lambda: {"kind": "progress", "confidence": "low", "reason": "（离线模拟）"},
                            validate=_vision_norm, project_id=note["project_id"], user_id=note["author_id"], max_tokens=200, temperature=0.0,
                            input_meta={"note": note_id})
        except AIError as exc:
            log.warning("随手记视觉分类失败: %s", exc)
            _upsert(conn, note, vision_status="failed")
            return
        d = res.data
        keep = d["kind"] in ("ticket", "conceal") and d["confidence"] == "high"
        _upsert(conn, note, vision_status="done", vision_kind=d["kind"] if keep else "progress", vision_reason=d["reason"], vision_run_id=res.run_id)
    except Exception:  # noqa: BLE001 — 归类只是建议，任何异常都不能影响随手记
        log.exception("随手记视觉分类异常")
    finally:
        conn.close()


# ----------------------------------------------------------------------------- 人点“是”：转入既有流程（只生成草稿）

class ClassifyError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def manual(conn, user: dict, nid: int, kind: str) -> dict:
    """人工重新归类：技术员自己选类别（含之前点过“不是”的记录），返回一条建议，仍由人点“是”才转入既有流程（只生成草稿）。"""
    note = _note(conn, user, nid)
    if not note:
        raise ClassifyError(404, "记录不存在")
    st = _state(conn, nid)
    if st and st["decision"] == "accepted":
        return {"note_id": nid, "state": "accepted", "result": result_of(conn, st)}
    if kind not in KIND_NAME:
        raise ClassifyError(400, "类别只能是安全隐患、材料小票或隐蔽验收")
    media = _media(conn, note)
    if kind in ("ticket", "conceal") and not media:
        raise ClassifyError(400, f"{KIND_NAME[kind]}需要照片：这条随手记没有照片")
    if st and st["decision"] == "dismissed":
        _upsert(conn, note, decision=None, decided_at=None)
    audit(conn, user["id"], "note.classify", f"note:{nid}", f"manual:{kind}")
    # 原照片已被安全Agent判为“未见隐患”的，再交给它同一张照片没有意义：请拍一张能看清隐患的照片
    clear = bool(media) and all(m["safety_status"] == "clear" for m in media)
    return {"note_id": nid, "state": "suggested", "suggestion": _sug(kind, "manual", "你选的类别", needs_photo=(kind == "hazard" and (not media or clear)))}


def dismiss(conn, user: dict, nid: int) -> dict:
    note = _note(conn, user, nid)
    if not note:
        raise ClassifyError(404, "记录不存在")
    _upsert(conn, note, decision="dismissed", decided_at=now_iso())
    audit(conn, user["id"], "note.classify", f"note:{nid}", "dismiss")
    return {"ok": True, "state": "dismissed"}


def _ticket_from_media(conn, user: dict, media: list[dict], doc_kind: str) -> dict:
    """用随手记里已有的照片建一张票据草稿（识别由路由放后台）；照片原样保留在随手记里。"""
    from . import tickets as ticket_svc
    pick = media[:ticket_svc.MAX_PHOTOS]
    kind = doc_kind if doc_kind in ticket_svc.DOC_KINDS else "concrete_ticket"
    tid = insert(conn, "tickets", {
        "project_id": user["project_id"], "doc_kind": kind, "status": "draft", "ai_status": "pending",
        "media_ids_json": dumps([m["id"] for m in pick]), "sha_json": dumps([m["sha256"] for m in pick]), "uploaded_by": user["id"],
        "kind_hint": doc_kind if doc_kind in ticket_svc.DOC_KINDS else "auto", "created_at": now_iso(), "updated_at": now_iso()})
    audit(conn, user["id"], "ticket.upload", f"ticket:{tid}", f"来自随手记 {len(pick)}张")
    return {"id": tid}


def accept(conn, user: dict, nid: int, body: dict) -> dict:
    """返回 {state:'accepted', result, ...}；缺信息时返回 {need:{...}} 由前端就地补填；后台任务名单放在 'bg'。"""
    note = _note(conn, user, nid)
    if not note:
        raise ClassifyError(404, "记录不存在")
    st = _state(conn, nid)
    if st and st["decision"] == "accepted":
        return {"state": "accepted", "result": result_of(conn, st), "bg": {}}
    cur = suggest(conn, note)
    sug = cur.get("suggestion")
    kind = body.get("kind") or (sug or {}).get("kind")
    if kind not in KIND_NAME:
        raise ClassifyError(400, "没有可转入的类别")
    media = _media(conn, note)
    bg: dict = {}
    if kind == "ticket":
        if not media:
            raise ClassifyError(400, "小票需要照片：请先拍一张")
        doc_kind = (sug or {}).get("doc_kind") or "auto"
        t = _ticket_from_media(conn, user, media, doc_kind)
        bg["ocr"] = t["id"]
        target = ("ticket", t["id"])
    elif kind == "conceal":
        if not media:
            raise ClassifyError(400, "验收资料需要照片：请先拍一张")
        tpls = conceal_svc.templates(conn, user["project_id"])
        key = (body.get("template_key") or "").strip()
        if not key:
            m = conceal_svc.match_templates(tpls, note["text"] or "")
            key = m[0]["key"] if m else ""
        part = (body.get("part") or "").strip() or part_svc.part_from_text(note["text"]) or part_svc.part_from_text(note["geo_place"]) \
            or part_svc.part_from_text(user.get("wbs_scope"))
        if not key or not part:
            return {"state": "suggested", "need": {"part": not part, "template": not key, "part_value": part or "", "template_key": key},
                    "templates": [{"key": t["key"], "name": t["name"]} for t in tpls], "bg": {}}
        rec = None
        try:
            rec = conceal_svc.create(conn, user, part, key, None, note["note_date"])
            conceal_svc.attach_existing(conn, user, conceal_svc.get_row(conn, user, rec["id"]), [m["id"] for m in media], None)
        except conceal_svc.ConcealError as exc:
            if rec:  # 照片挂不上就不留空草稿
                conceal_svc.delete(conn, user, conceal_svc.get_row(conn, user, rec["id"]))
            raise ClassifyError(exc.status, exc.msg) from None
        target = ("conceal", rec["id"])
    else:  # hazard（人工归类时新拍的隐患照片放在 media_ids，优先用新照片）
        if media and not body.get("media_ids"):
            found = _hazard_findings(conn, media)
            todo = [m["id"] for m in media if m["safety_status"] in ("skipped", "failed")]
            if todo:
                conn.executemany("UPDATE media SET safety_status='pending' WHERE id=?", [(i,) for i in todo])
                bg["scan"] = todo
            target = ("finding", found[0]["id"]) if found else ("hazard_pending", None)
        else:
            ids = [int(i) for i in body.get("media_ids") or []][:9]
            rows = [one(conn, "SELECT * FROM media WHERE id=? AND uploader_id=? AND project_id=?", (i, user["id"], user["project_id"])) for i in ids]
            if not ids or not all(rows):
                raise ClassifyError(400, "没有可识别的隐患照片：请先拍一张")
            found = _hazard_findings(conn, rows)
            target = ("finding", found[0]["id"]) if found else ("hazard_pending", None)
    _upsert(conn, note, decision="accepted", decided_kind=kind, target_type=target[0], target_id=target[1], decided_at=now_iso())
    audit(conn, user["id"], "note.classify", f"note:{nid}", f"accept {kind} -> {target[0]}:{target[1]}")
    return {"state": "accepted", "result": result_of(conn, _state(conn, nid)), "bg": bg}
