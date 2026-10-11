"""随手记追问一句（2026-10-08 增补能力 1）：随手记缺关键信息时，就地追问一句，给 2～3 个可点的候选。

- 只追问“必要信息”：像是进度记录（提到完成、浇筑、绑扎、开挖……）却①对不上是哪个作业，或②没有数量。
  其余记录（问题、天气、安全、纯照片）不追问；已有归类建议（隐患/小票/验收）的同一条不再叠一个追问。
- 缺什么、候选作业由程序判断：候选只来自本人负责范围内当天应关注的 P6 作业（与现场记录Agent 起草日志用的同一个窗口），
  按部位编号和工序词打分。程序没把握的不问，不猜。
- 交融模型只负责把问题说顺（≤30 字）并在程序给的候选里排序；出口校验：候选必须出自程序给的清单，问题里的数字必须出自原话或候选，
  不过或模型不可用就用规则问句（不依赖模型的兜底）。离线模拟模型下直接用规则问句。
- 每条随手记最多追问 1 次，每人每天最多 5 次；技术员可点选、打字（手机输入法可语音），也可以忽略，忽略不扣分，周确认时再处理。
- 回答以“（补充：…）”写回同一条随手记的文字，晚间整理日志、周确认直接用上；工程量只是技术员自报，仍经周确认核对。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from ..ai.client import AIError, call_json, provider_status
from ..config import settings
from ..db import all_, audit, connect, dumps, insert, loads, now_iso, one
from . import p6
from .classify import ROLES, text_signals
from .parts import activity_part, part_from_text

log = logging.getLogger("zhimou.followup")

PROMPT_VERSION = "followup-v1-20261008"
DAILY_CAP = 5
PENDING_SECONDS = 15        # 模型润色超过这个时间还没回来，先给规则问句

# 像进度记录的词（“开始”“准备”不算完成量，不追问数量）
DONE_WORDS = ("完成", "浇筑", "浇注", "绑扎", "开挖", "填筑", "安装", "架设", "铺设", "摊铺", "做了", "打了", "成孔", "钻孔", "灌注",
              "砌筑", "碾压", "支模", "拆模", "焊接", "吊装", "压实", "施工了")
NOT_PROGRESS = ("停工", "下雨", "暴雨", "缺料", "故障", "没来", "未到", "等待", "待料", "开会", "交底")
# 数量：阿拉伯数字或中文数字 + 单位；“六成”“八成”“一半”“百分之”也算给了量
_QTY = re.compile(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十百千半几]+)\s*(?:方|立方|m3|m³|㎥|米|延米|m|根|吨|t|榀|片|个|孔|节|%|％|成|㎡|平方|平米|块|段|跨|车|榀|层|模)")
_PCT = re.compile(r"百分之|一半|过半|全部完成|已完成全部|完工")
# 工序词：用于把随手记对到作业上
PROC_WORDS = ("桩基", "成孔", "灌注", "钻孔", "承台", "钢筋", "模板", "混凝土", "浇筑", "墩身", "盖梁", "系梁", "垫石", "支座", "开挖", "填筑", "土方",
              "路基", "梁板", "预制", "架设", "涵洞", "基础", "路面", "基层", "底基层", "水稳", "沥青", "摊铺", "垫层", "挡墙", "护坡", "排水", "边沟",
              "管道", "台背", "回填", "防撞", "护栏", "桥面", "铺装", "湿接缝", "伸缩缝", "沉箱", "码头", "胸墙", "抛石", "围堰")

UNIT_HINTS = (
    (("钢筋",), ["吨", "%"]),
    (("桩", "成孔", "灌注", "钻孔"), ["根", "%"]),
    (("混凝土", "浇筑", "承台", "垫层", "墩身", "盖梁", "铺装"), ["方", "%"]),
    (("开挖", "填筑", "土方", "回填"), ["方", "%"]),
    (("预制", "架设", "梁板"), ["片", "%"]),
    (("路面", "摊铺", "基层", "水稳", "沥青", "边沟", "排水", "管道", "护栏"), ["米", "%"]),
)
DEFAULT_UNITS = ["方", "米", "%"]


class FollowupError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


# ----------------------------------------------------------------------------- 规则：缺什么、候选是谁

def _proc_hits(text: str) -> set[str]:
    return {w for w in PROC_WORDS if w in (text or "")}


def is_progress_like(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 4 or not any(w in t for w in DONE_WORDS):
        return False
    return not any(w in t for w in NOT_PROGRESS)


def has_qty(text: str) -> bool:
    return bool(_QTY.search(text or "") or _PCT.search(text or ""))


def score(a: dict, text: str) -> int:
    """随手记与作业的匹配分。写了部位但不是这个部位的直接排除（-99）。"""
    part, apart = part_from_text(text), activity_part(a)
    s = 0
    if part and apart and part_from_text(apart):
        if part != apart:
            return -99
        s += 3
    name = a.get("name") or ""
    s += 2 * len(_proc_hits(text) & _proc_hits(name))
    if a.get("act_start") and not a.get("act_finish"):
        s += 1   # 已开工未完成的更可能是今天在做的
    return s


def units_for(name: str) -> list[str]:
    for words, units in UNIT_HINTS:
        if any(w in (name or "") for w in words):
            return units
    return DEFAULT_UNITS


def gaps(conn, user: dict, note: dict) -> dict | None:
    """返回 {need_activity, need_qty, candidates:[{code,name}], units, matched?}；不需要追问返回 None。"""
    text = (note.get("text") or "").strip()
    if not is_progress_like(text):
        return None
    acts = [a for a in p6.window_activities(p6.current_activities(conn, user["project_id"]), note["note_date"], user.get("wbs_scope", ""))
            if not a.get("is_milestone")]
    ranked = sorted(((score(a, text), a) for a in acts), key=lambda x: -x[0])
    ranked = [(s, a) for s, a in ranked if s > 0]
    matched = None
    if ranked and ranked[0][0] >= 4 and (len(ranked) == 1 or ranked[1][0] < ranked[0][0]):
        matched = ranked[0][1]
    need_qty = not has_qty(text)
    need_act = matched is None
    cands: list[dict] = []
    if need_act:
        pool = [a for _, a in ranked[:3]]
        if not pool and not part_from_text(text) and not _proc_hits(text):
            pool = [a for a in acts if a.get("act_start")][:3]   # 什么都没写：列出正在做的作业
        cands = [{"code": a["code"], "name": a["name"]} for a in pool]
        if not cands:
            need_act = False   # 没有可点的候选：不凭空问“是哪个作业”
    if not need_act and not need_qty:
        return None
    if need_qty and not need_act and matched is None:
        return None            # 既对不上作业又没有候选，只问数量也写不回有用的东西
    name = matched["name"] if matched else (cands[0]["name"] if cands else "")
    return {"need_activity": need_act, "need_qty": need_qty, "candidates": cands,
            "matched": {"code": matched["code"], "name": matched["name"]} if matched else None, "units": units_for(name)}


def rule_question(g: dict) -> str:
    if g["need_activity"] and g["need_qty"]:
        return f"是{g['candidates'][0]['name']}吗？大约完成多少？" if len(g["candidates"]) == 1 else "是哪个作业？大约完成多少？"
    if g["need_activity"]:
        return f"是{g['candidates'][0]['name']}吗？" if len(g["candidates"]) == 1 else "说的是哪个作业？"
    return f"{g['matched']['name']}大约完成多少{g['units'][0] if g['units'][0] != '%' else ''}？"


# ----------------------------------------------------------------------------- 模型润色（交融通道）+ 出口校验

SYSTEM = """你是施工现场记录助手。技术员刚记了一句随手记，系统发现缺少关键信息（缺什么见 JSON 的 need）。
请写一句追问，不超过 30 字，口语、客气、直接，像同事随口一问；需要选作业时，从 candidates 里按可能性排序给出最多 3 个作业代码。
硬性要求：
1. 只能问 need 里缺的信息，不要问别的；
2. 作业只能从 candidates 里选，不得编造作业；
3. 不得写原话和候选名称里没有的数字；
4. 不要使用 markdown。
只输出 JSON：{"question": "……", "codes": ["作业代码", ...]}"""

_NUM = re.compile(r"\d+(?:\.\d+)?")


def _allowed_nums(note_text: str, g: dict) -> set[str]:
    src = note_text + " " + " ".join(c["name"] for c in g["candidates"]) + " " + ((g.get("matched") or {}).get("name") or "")
    return {f"{float(x):g}" for x in _NUM.findall(src)}


def guard(q: str, codes: list, note_text: str, g: dict) -> list[str]:
    """返回不合格原因（空表示通过）。"""
    bad = []
    ok_codes = {c["code"] for c in g["candidates"]}
    if any(c not in ok_codes for c in codes):
        bad.append("候选不在清单内")
    allowed = _allowed_nums(note_text, g)
    for x in _NUM.findall(q):
        if f"{float(x):g}" not in allowed:
            bad.append(f"数字{x}无出处")
    if len(q) > 40:
        bad.append("问句过长")
    return bad


def _model_ready() -> bool:
    return settings.ai_provider != "mock" and bool(provider_status().get("ready"))


def _norm(d) -> dict:
    if not isinstance(d, dict) or not isinstance(d.get("question"), str) or not d["question"].strip():
        raise ValueError("缺少 question")
    codes = d.get("codes") if isinstance(d.get("codes"), list) else []
    return {"question": re.sub(r"\s+", "", d["question"].strip())[:60], "codes": [str(c) for c in codes][:3]}


def refine(note_id: int) -> None:
    """后台：用交融模型把追问说顺。失败或不过校验保留规则问句，只记原因。"""
    conn = connect()
    try:
        fu = one(conn, "SELECT * FROM note_followup WHERE note_id=?", (note_id,))
        note = one(conn, "SELECT * FROM notes WHERE id=?", (note_id,))
        if not fu or not note or fu["status"] != "pending":
            return
        g = loads(fu["gaps_json"], {})
        payload = {"note": note["text"], "need": [k for k, v in (("作业", g.get("need_activity")), ("数量", g.get("need_qty"))) if v],
                   "candidates": g.get("candidates", []), "matched": g.get("matched"), "units": g.get("units")}
        note_txt = ""
        try:
            import json
            res = call_json("note.followup", system=SYSTEM, user=json.dumps(payload, ensure_ascii=False), prompt_version=PROMPT_VERSION,
                            mock=lambda: {"question": fu["question"], "codes": [c["code"] for c in g.get("candidates", [])]},
                            validate=_norm, project_id=note["project_id"], user_id=note["author_id"], max_tokens=200, temperature=0.2,
                            input_meta={"note": note_id})
            bad = guard(res.data["question"], res.data["codes"], note["text"], g)
            if bad or res.degraded:
                note_txt = "AI 追问未通过校验（" + "、".join(bad[:2] or ["格式"]) + "），用系统问句"
                conn.execute("UPDATE note_followup SET status='asked', source='rule', note=?, ai_run_id=? WHERE note_id=?", (note_txt, res.run_id, note_id))
                return
            order = {c: i for i, c in enumerate(res.data["codes"])}
            cands = sorted(g.get("candidates", []), key=lambda c: order.get(c["code"], 99))
            conn.execute("UPDATE note_followup SET status='asked', source='ai', question=?, options_json=?, ai_run_id=? WHERE note_id=?",
                         (res.data["question"], dumps(cands), res.run_id, note_id))
        except AIError as exc:
            conn.execute("UPDATE note_followup SET status='asked', source='rule', note=? WHERE note_id=?", ("AI 暂不可用，用系统问句（" + str(exc)[:30] + "）", note_id))
    except Exception:  # noqa: BLE001 — 追问只是辅助，异常不影响随手记
        log.exception("随手记追问润色异常")
        try:
            conn.execute("UPDATE note_followup SET status='asked', source='rule' WHERE note_id=? AND status='pending'", (note_id,))
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


# ----------------------------------------------------------------------------- 一条随手记的追问状态

def _note(conn, user: dict, nid: int) -> dict | None:
    return one(conn, "SELECT * FROM notes WHERE id=? AND author_id=? AND project_id=?", (nid, user["id"], user["project_id"]))


def _day_locked(conn, note: dict) -> bool:
    d = one(conn, "SELECT status FROM logs WHERE project_id=? AND author_id=? AND log_date=? AND period='day' AND mode='ai'",
            (note["project_id"], note["author_id"], note["note_date"]))
    return bool(d and d["status"] in ("submitted", "approved"))


def _asked_today(conn, user: dict, day: str) -> int:
    return one(conn, "SELECT COUNT(*) n FROM note_followup WHERE user_id=? AND day=?", (user["id"], day))["n"]


def _view(fu: dict) -> dict:
    g = loads(fu["gaps_json"], {})
    st = fu["status"]
    if st == "pending":
        started = fu["created_at"] or ""
        if started < (datetime.now().astimezone() - timedelta(seconds=PENDING_SECONDS)).isoformat():
            st = "asked"   # 模型迟迟没回：先给规则问句
    out = {"note_id": fu["note_id"], "state": st, "source": fu["source"], "note": fu["note"] or ""}
    if st in ("asked",):
        out["ask"] = {"question": fu["question"], "candidates": loads(fu["options_json"], []), "need_activity": bool(g.get("need_activity")),
                      "need_qty": bool(g.get("need_qty")), "units": g.get("units") or DEFAULT_UNITS, "matched": g.get("matched")}
    if st == "answered":
        out["answer"] = loads(fu["answer_json"], {})
    return out


def state(conn, user: dict, note: dict) -> dict:
    """返回 {note_id, state: none|pending|asked|answered|dismissed, ask?, need_refine?}。首次判断时建行（每条最多问一次）。"""
    fu = one(conn, "SELECT * FROM note_followup WHERE note_id=?", (note["id"],))
    if fu:
        return _view(fu)
    base = {"note_id": note["id"], "state": "none"}
    if note["note_date"] != date.today().isoformat() or _day_locked(conn, note):
        return base
    if any(s["kind"] in ("hazard", "ticket", "conceal") for s in text_signals(note["text"] or "")):
        return base   # 已有归类建议的，不再叠一个追问
    cls = one(conn, "SELECT decision FROM note_classify WHERE note_id=?", (note["id"],))
    if cls and cls["decision"] == "accepted":
        return base
    if _asked_today(conn, user, note["note_date"]) >= DAILY_CAP:
        return base
    g = gaps(conn, user, note)
    if not g:
        return base
    refine_it = _model_ready()
    insert(conn, "note_followup", {"note_id": note["id"], "project_id": note["project_id"], "user_id": note["author_id"], "day": note["note_date"],
                                   "gaps_json": dumps(g), "question": rule_question(g), "options_json": dumps(g["candidates"]),
                                   "status": "pending" if refine_it else "asked", "source": "rule", "created_at": now_iso()})
    out = _view(one(conn, "SELECT * FROM note_followup WHERE note_id=?", (note["id"],)))
    if refine_it:
        out["need_refine"] = True
    return out


def batch(conn, user: dict, ids: list[int]) -> list[dict]:
    rows = [r for r in (_note(conn, user, int(i)) for i in ids[:60]) if r]
    return [state(conn, user, n) for n in rows]


# ----------------------------------------------------------------------------- 回答 / 忽略

def answer(conn, user: dict, nid: int, body: dict) -> dict:
    note = _note(conn, user, nid)
    if not note:
        raise FollowupError(404, "记录不存在")
    fu = one(conn, "SELECT * FROM note_followup WHERE note_id=?", (nid,))
    if not fu or fu["status"] not in ("pending", "asked"):
        raise FollowupError(409, "这条已经回答或忽略了")
    if _day_locked(conn, note):
        raise FollowupError(409, "当日日志已确认，补充内容会列入下次周确认的迟到记录，请重新记一条")
    g = loads(fu["gaps_json"], {})
    cands = {c["code"]: c for c in loads(fu["options_json"], [])}
    code = (body.get("code") or "").strip()
    if code and code not in cands:
        raise FollowupError(400, "只能从给出的作业里选")
    qty = body.get("qty")
    if qty not in (None, ""):
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            raise FollowupError(400, "数量要写数字") from None
        if qty < 0 or qty > 1e6:
            raise FollowupError(400, "数量不合理")
    else:
        qty = None
    unit = (body.get("unit") or "").strip()[:6]
    free = (body.get("text") or "").strip()[:200]
    if not code and qty is None and not free:
        raise FollowupError(400, "点一个作业、填个数量，或打一句话")
    act = cands.get(code) or (g.get("matched") if not g.get("need_activity") else None)
    bits = []
    if act and (code or g.get("matched")):
        bits.append(act["name"])
    if qty is not None:
        q = f"{qty:g}"
        bits.append(f"约完成{q}%" if unit in ("%", "％") else f"约{q}{unit or ''}")
    if free:
        bits.append(free)
    supplement = "（补充：" + "，".join(bits) + "）"
    new_text = ((note["text"] or "").rstrip() + supplement)[:2000]
    conn.execute("UPDATE notes SET text=? WHERE id=?", (new_text, nid))
    ans = {"code": code or ((g.get("matched") or {}).get("code") if not g.get("need_activity") else None), "name": act["name"] if act else None,
           "qty": qty, "unit": unit, "text": free, "supplement": supplement}
    conn.execute("UPDATE note_followup SET status='answered', answer_json=?, answered_at=? WHERE note_id=?", (dumps(ans), now_iso(), nid))
    audit(conn, user["id"], "note.followup", f"note:{nid}", supplement[:120])
    return {"state": "answered", "answer": ans, "text": new_text}


def dismiss(conn, user: dict, nid: int) -> dict:
    note = _note(conn, user, nid)
    if not note:
        raise FollowupError(404, "记录不存在")
    conn.execute("UPDATE note_followup SET status='dismissed', answered_at=? WHERE note_id=? AND status IN ('pending','asked')", (now_iso(), nid))
    audit(conn, user["id"], "note.followup", f"note:{nid}", "dismiss")
    return {"state": "dismissed"}


def stats(conn, project_id: int, days: int = 30) -> dict:
    """追问的回答率（只给数量；样本不足不下结论）。"""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = all_(conn, "SELECT status, COUNT(*) n FROM note_followup WHERE project_id=? AND day>=? GROUP BY status", (project_id, since))
    by = {r["status"]: r["n"] for r in rows}
    return {"asked": sum(by.values()), "answered": by.get("answered", 0), "dismissed": by.get("dismissed", 0)}


__all__ = ["ROLES", "batch", "answer", "dismiss", "refine", "gaps", "FollowupError"]
