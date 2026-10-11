"""现场圈 Agent 化：Agent 以“同事”身份点评动态、@工笔 就地问答、一张照片经 Agent 接力的时间线。

原则（与产品定位一致）：
- 点评只来自该动态自身的真实记录（安全识别结果、关联作业与质控提醒、P6 作业与日志进度）；没有依据就不评，不编造；
- AI 只负责把“依据”写成同事口吻的一句话，数字被守卫校验，出现依据之外的数字即丢弃改用模板；
- 点评按（动态，视角）只生成一次并缓存入表，评论里附“依据”，可点开核对；
- AI 起草/提醒，人确认：这里不改进度、不闭环、不派单。
"""
from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from datetime import date

from .. import roles as R
from ..agents import AGENT_BY_KEY
from ..ai.client import AIError, call_json
from ..db import all_, dumps, insert, loads, now_iso, one
from . import feed as feed_svc
from . import facts as F
from . import findings, p6, site_agents
from .logs import progress_state
from . import act_names as _an

log = logging.getLogger(__name__)

PROMPT_VERSION = "feed-comment-v3.1-20261007"
VIEWS = {
    "safety": {"agent": "safety", "label": "安全"},
    "quality": {"agent": "quality", "label": "质量"},
    "progress": {"agent": "progress", "label": "进度"},
}
ACTIVE = ("open", "rectifying", "rejected")
ASK_PER_DAY = 30
REVIEW_BATCH = 6
LEVEL_NAME = findings.LEVEL_NAME

_locks: dict[tuple, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(pid: int, key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((pid, key), threading.Lock())


def agent_meta(view: str) -> dict:
    a = AGENT_BY_KEY[VIEWS[view]["agent"]] if view in VIEWS else AGENT_BY_KEY["analyst"]
    return {"view": view, "key": a["key"], "name": a["name"], "icon": a["icon"]}


# ============================================================================ 取动态原始记录

def _in(ids) -> str:
    return ",".join("?" for _ in ids) or "NULL"


def load_post(conn, key: str) -> dict | None:
    """按动态键读回原始记录（可见性已由调用方校验）。形状：kind/author_id/time/day/text/media/log/finding。"""
    kind, _, raw = key.partition(":")
    if kind not in ("note", "snap", "rect", "log", "notice") or not raw.isdigit():
        return None
    rid = int(raw)
    if kind == "notice":  # 公告可以向工笔提问，正文即“可见内容”
        n = one(conn, "SELECT * FROM circle_notices WHERE id=?", (rid,))
        if not n:
            return None
        return {"key": key, "kind": kind, "project_id": n["project_id"], "author_id": n["author_id"], "time": n["created_at"], "day": n["created_at"][:10],
                "text": f"{n['title']}：{n['body']}", "media": [], "voice": False, "place": "", "log": None, "finding": None}
    if kind == "note":
        n = one(conn, "SELECT * FROM notes WHERE id=?", (rid,))
        if not n:
            return None
        return {"key": key, "kind": kind, "project_id": n["project_id"], "author_id": n["author_id"], "time": n["created_at"], "day": n["note_date"],
                "text": n["text"], "media": [int(i) for i in loads(n["media_json"], [])], "voice": n["source"] == "voice",
                "place": n["geo_place"] or "", "log": None, "finding": None}
    if kind in ("snap", "rect"):
        m = one(conn, "SELECT * FROM media WHERE id=? AND purpose=?", (rid, "safety" if kind == "snap" else "rectify"))
        if not m:
            return None
        base = {"key": key, "kind": kind, "project_id": m["project_id"], "author_id": m["uploader_id"], "time": m["uploaded_at"],
                "day": m["uploaded_at"][:10], "text": "", "place": m["geo_place"] or "", "log": None, "voice": False}
        if kind == "snap":
            ms = all_(conn, "SELECT id FROM media WHERE project_id=? AND purpose='safety' AND uploader_id=? AND substr(uploaded_at,1,16)=substr(?,1,16) ORDER BY id",
                      (m["project_id"], m["uploader_id"], m["uploaded_at"]))
            return {**base, "media": [x["id"] for x in ms] or [rid], "finding": None}
        f = one(conn, "SELECT * FROM findings WHERE rectify_media_id=? AND project_id=?", (rid, m["project_id"]))
        return {**base, "media": [rid], "finding": f}
    lg = one(conn, "SELECT * FROM logs WHERE id=?", (rid,))
    if not lg:
        return None
    return {"key": key, "kind": "log", "project_id": lg["project_id"], "author_id": lg["author_id"], "time": lg["submitted_at"] or lg["started_at"],
            "day": lg["log_date"], "text": "", "media": _log_media_ids(conn, lg), "voice": False, "place": "", "log": lg, "finding": None}


def _log_media_ids(conn, lg: dict) -> list[int]:
    if lg["period"] == "week":
        rows = all_(conn, """SELECT m.id FROM media m JOIN logs d ON d.id=m.log_id WHERE d.project_id=? AND d.author_id=? AND d.period='day'
                             AND d.log_date BETWEEN ? AND ? ORDER BY m.id""", (lg["project_id"], lg["author_id"], lg["log_date"], lg["period_end"] or lg["log_date"]))
    else:
        rows = all_(conn, "SELECT id FROM media WHERE log_id=? ORDER BY id", (lg["id"],))
    return [r["id"] for r in rows]


def _hazards(conn, post: dict) -> list[dict]:
    if not post["media"]:
        return []
    return all_(conn, f"SELECT * FROM findings WHERE project_id=? AND agent='safety' AND ref_type='media' AND ref_id IN ({_in(post['media'])}) ORDER BY id",
                [post["project_id"], *post["media"]])


# ============================================================================ 关联 P6 作业

_STOP = re.compile(r"施工|作业|工程|完成|进行|继续")


def _norm(text: str) -> str:
    return re.sub(r"(\d+)\s*[号]", r"\1#", text or "")


def _bigrams(name: str) -> set[str]:
    core = re.sub(r"[^一-龥]", "", _STOP.sub("", re.sub(r"\d+\s*#\s*墩?", "", _norm(name))))
    return {core[i:i + 2] for i in range(len(core) - 1)}


def match_activities(acts: list[dict], text: str, day: str, scope: str = "") -> list[dict]:
    """按文字把动态对到 P6 作业：必须有工序词命中，部位冲突（3#墩≠4#墩）排除，并列超过 2 个或部位不明的并列视为歧义，不关联。"""
    text = _norm(text)
    if len(text) < 2 or not acts:
        return []
    t_locs = set(re.findall(r"\d+#", text))
    scored = []
    for a in p6.window_activities(acts, day, scope, limit=80):
        if a.get("is_milestone"):
            continue
        n_locs = set(re.findall(r"\d+#", _norm(a["name"])))
        if n_locs and t_locs and not (n_locs & t_locs):
            continue
        hits = sum(1 for b in _bigrams(a["name"]) if b in text)
        if hits < 1:
            continue
        scored.append((2 * hits + (1 if n_locs & t_locs else 0), frozenset(n_locs), a))
    if not scored:
        return []
    top = max(s for s, _, _ in scored)
    best = [(l, a) for s, l, a in scored if s == top]
    # 并列 ≤2 个且部位一致（如同一承台的钢筋绑扎与混凝土浇筑）才都关联；不同部位并列（3#墩/4#墩桩基）视为歧义
    if len(best) > 2 or (len(best) == 2 and best[0][0] != best[1][0]):
        return []
    return [a for _, a in best]


def _log_codes(lg: dict) -> list[str]:
    c = loads(lg["content_json"], {})
    codes = [p.get("activity_code") for p in c.get("progress", [])] + [w.get("activity_code") for w in c.get("works", [])]
    return list(dict.fromkeys(x for x in codes if x))[:4]


def _post_activities(conn, post: dict, author: dict | None) -> list[dict]:
    acts = p6.current_activities(conn, post["project_id"])
    if not acts:
        return []
    if post["kind"] == "log":
        by = {a["code"]: a for a in acts}
        return [by[c] for c in _log_codes(post["log"]) if c in by]
    parts = [post["text"], post["place"]]
    hz = _hazards(conn, post)
    parts += [f"{h['title']} {loads(h['body_json'], {}).get('scene') or ''}" for h in hz]
    if post["finding"]:
        parts.append(post["finding"]["title"])
    return match_activities(acts, " ".join(x for x in parts if x), post["day"], (author or {}).get("wbs_scope") or "")


# ============================================================================ 依据（事实）

def _f(view: str, text: str, ref: dict | None = None) -> dict:
    return {"view": view, "text": text, "ref": ref}


# ---------------------------------------------------------------------------- 整改复核（N-2，4.4 verify 契约）
# 复核结论只是 AI 建议：隐患由人确认闭环之前，不出现“已消除/已闭环”这类肯定说法；带“建议/待”的描述句不算
CLOSURE_RE = re.compile(r"已(?:确认)?消除|已闭环|整改到位|已整改完毕|已排除")


def verify_fact(conn, fd: dict) -> str:
    """整改复核的一句事实：人确认闭环后写“已闭环（谁确认）”；被退回写“谁已退回重改”；其余按 site_agents.verify_label 写 AI 建议或复核状态。"""
    who = one(conn, "SELECT name FROM users WHERE id=?", (fd["feedback_by"],)) if fd["feedback_by"] else None
    if fd["status"] == "resolved":
        return f"已闭环（{who['name']}确认）" if who else "已闭环"
    if fd["status"] in ("open", "rejected") and who:
        return f"{who['name']}已退回重改"
    v = loads(fd["verify_json"], {}) or {}
    label = site_agents.verify_label(v, fd["status"])
    reason = re.sub(r"[。；;！!？?\n]+", "，", str(v.get("reason") or "")).strip("，")  # 理由与建议留在同一句里
    return label + (site_agents.paren(reason) if label.startswith("AI 建议") else "")


def closure_problems(text: str) -> list[str]:
    """隐患还没由人确认闭环时用：点评里出现“已消除/已闭环”等肯定说法、且同一句没有“建议/待”，判为不合格。"""
    for sent in re.split(r"[。！？!?\n；;]", text or ""):
        hit = CLOSURE_RE.search(sent)
        if hit and not re.search(r"建议|待", sent):
            return [f"出现“{hit.group()}”：隐患还没有由安全主管确认闭环，整改复核只能写成 AI 建议"]
    return []


# 点评只报事实（U-20）：不替人下“落实到位”一类评价，不用撒娇语气词
TONE_RE = re.compile(r"(?:落实|管控|执行|整改)到位|[哦呢呀啦][~～]")


def tone_problems(text: str) -> list[str]:
    hit = TONE_RE.search(text or "")
    return [f"出现“{hit.group()}”：点评只报告事实，不下评价、不用语气词"] if hit else []


def _open_rect(post: dict | None) -> bool:
    return bool(post and post["kind"] == "rect" and post["finding"] and post["finding"]["status"] != "resolved")


def _safety_facts(conn, post: dict) -> list[dict]:
    out: list[dict] = []
    today = date.today().isoformat()
    hz = _hazards(conn, post)
    if hz:
        cand = [f for f in hz if findings.is_candidate(f)]  # AI 候选：待安全员确认，不算“未闭环/超期”
        open_ = [f for f in hz if f["status"] in ACTIVE and not findings.is_candidate(f)]
        late = [f for f in hz if f["status"] in ("open", "rejected") and f["due_date"] and f["due_date"] < today and not findings.is_candidate(f)]
        top = min(findings.LEVEL_RANK[f["level"]] for f in hz)
        top_name = {v: k for k, v in findings.LEVEL_RANK.items()}[top]
        uniq = list(dict.fromkeys(f["title"] for f in hz))
        names = "；".join(uniq[:2]) + (" 等" if len(uniq) > 2 else "")
        text = f"这组照片 AI 识别出 {len(hz)} 处隐患，最高等级{LEVEL_NAME[top_name]}：{names}"
        if cand:
            text += f"；{len(cand)} 处为候选，待安全员确认"
        if len(cand) < len(hz):
            text += f"；其中 {len(open_)} 处尚未闭环" if open_ else "；均已闭环"
        if late:
            text += f"，{len(late)} 处已超过整改期限"
        out.append(_f("safety", text, {"type": "finding", "id": hz[0]["id"], "label": f"隐患 #{hz[0]['id']}"}))
    fd = post["finding"]
    if fd and (loads(fd["verify_json"], {}) or fd["status"] == "resolved"):
        out.append(_f("safety", "整改照片已提交，" + verify_fact(conn, fd), {"type": "finding", "id": fd["id"], "label": f"隐患 #{fd['id']}"}))
    if post["kind"] == "log":
        rep = all_(conn, "SELECT * FROM findings WHERE project_id=? AND agent='safety' AND kind='reported' AND ref_type='log' AND ref_id=? ORDER BY id",
                   (post["project_id"], post["log"]["id"]))
        if rep:
            still = sum(1 for f in rep if f["status"] in ACTIVE)
            out.append(_f("safety", f"日志里记录了 {len(rep)} 项安全问题，已转为跟踪待办，其中 {still} 项尚未闭环" if still else f"日志里记录了 {len(rep)} 项安全问题，均已闭环",
                          {"type": "finding", "id": rep[0]["id"], "label": f"问题 #{rep[0]['id']}"}))
    return out


def _quality_facts(conn, post: dict, acts: list[dict]) -> list[dict]:
    out: list[dict] = []
    for a in acts[:2]:
        r = one(conn, """SELECT * FROM findings WHERE project_id=? AND agent='quality' AND kind='qc_reminder' AND activity_code=?
                         AND status<>'dismissed' ORDER BY id DESC LIMIT 1""", (post["project_id"], a["code"]))
        if not r:
            continue
        pts = [p.get("item") for p in (loads(r["body_json"], {}).get("points") or []) if p.get("item")][:2]
        state = {"open": "还没有处理，尚未见完成记录", "rejected": "还没有处理，尚未见完成记录", "rectifying": "已提交，待复核", "resolved": "已完成"}[r["status"]]
        out.append(_f("quality", f"这是{a['name']}（{a['code']}），已按工序派发质控提醒"
                      + (f"（{'、'.join(pts)}）" if pts else "") + f"，{state}" + (f"，期限 {r['due_date']}" if r["due_date"] and r["status"] in ("open", "rejected") else ""),
                      {"type": "finding", "id": r["id"], "label": f"质控提醒 #{r['id']}"}))
    if post["kind"] == "log":
        rep = all_(conn, "SELECT * FROM findings WHERE project_id=? AND agent='quality' AND kind='reported' AND ref_type='log' AND ref_id=? ORDER BY id",
                   (post["project_id"], post["log"]["id"]))
        if rep:
            still = sum(1 for f in rep if f["status"] in ACTIVE)
            out.append(_f("quality", f"日志里记录了 {len(rep)} 项质量问题，其中 {still} 项尚未闭环" if still else f"日志里记录了 {len(rep)} 项质量问题，均已闭环",
                          {"type": "finding", "id": rep[0]["id"], "label": f"问题 #{rep[0]['id']}"}))
    return out


def _progress_facts(conn, post: dict, acts: list[dict]) -> list[dict]:
    if not acts:
        return []
    imp = p6.current_import(conn, post["project_id"])
    state = progress_state(conn, post["project_id"], imp) if imp else {}
    reported = {}
    if post["kind"] == "log":
        reported = {p.get("activity_code"): p for p in loads(post["log"]["content_json"], {}).get("progress", [])}
    today = date.today().isoformat()
    out = []
    for a in acts[:3]:
        t = f"{_an.fmt(a['code'], a['name'])}"
        r = reported.get(a["code"])
        if r and (r.get("pct") is not None or r.get("qty") is not None):
            bits = []
            if r.get("qty") is not None:
                bits.append(f"{r['qty']:g}{r.get('unit') or ''}")
            if r.get("pct") is not None:
                bits.append(f"累计 {r['pct']:g}%")
            t += "，本次确认 " + " ".join(bits)
        if a.get("early_start") and a.get("early_finish"):
            t += f"，P6 计划 {a['early_start']}～{a['early_finish']}"
        if a.get("phys_pct") is not None:
            t += f"，P6 记录完成 {a['phys_pct']:g}%"
        st = state.get(a["code"])
        if st and st["pct"] is not None and not r:
            t += f"，日志最新累计 {st['pct']:g}%（{st['pct_date']}）"
        if a.get("early_finish") and a["early_finish"] < today and not a.get("act_finish"):
            t += "，已过计划完成日仍未完成"
        out.append(_f("progress", t, {"type": "act", "code": a["code"], "label": a["code"]}))
    return out


def collect_facts(conn, post: dict) -> list[dict]:
    author = one(conn, "SELECT * FROM users WHERE id=?", (post["author_id"],))
    acts = _post_activities(conn, post, author)
    facts = _safety_facts(conn, post) + _quality_facts(conn, post, acts) + _progress_facts(conn, post, acts)
    for i, f in enumerate(facts, 1):
        f["id"] = f"F{i}"
    return facts


# ============================================================================ 生成点评

def _template(facts: list[dict]) -> str:
    return "；".join(f["text"] for f in facts)[:300] + "。"


def _digits(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _norm_comments(data, by_view: dict[str, list[dict]], open_rect: bool = False) -> dict[str, str]:
    if not isinstance(data, dict) or not isinstance(data.get("comments"), list):
        raise ValueError("缺少comments数组")
    out = {}
    for c in data["comments"]:
        if not isinstance(c, dict) or c.get("view") not in by_view:
            continue
        text = str(c.get("text") or "").strip()
        if not text or len(text) > 160:
            raise ValueError("点评为空或超过160字")
        allowed = _digits(" ".join(f["text"] for f in by_view[c["view"]]))
        extra = _digits(text) - allowed
        if extra:
            raise ValueError(f"点评出现依据之外的数字：{sorted(extra)[:3]}")
        if open_rect and c["view"] == "safety" and closure_problems(text):
            raise ValueError(closure_problems(text)[0])
        if tone_problems(text):
            raise ValueError(tone_problems(text)[0])
        out[c["view"]] = text
    return out


COMMENT_SYSTEM = """你是施工项目“现场圈”里的 Agent 同事，要在同事发的动态下留言。每个视角（safety安全/quality质量/progress进度）写一句点评。
规则：
1. 只能依据【依据】里的事实，不得出现依据以外的数字、人名、作业代码、规范条号；事实里没有的结论一律不写。
2. 像同事一样口语化，80字以内；提醒而不下命令，不替人确认、不宣布进度或闭环。只报告事实（提醒了什么、现在什么状态），
   不下“落实到位/管控到位”这类评价，不用“哦~”“呢~”之类的语气词；整改复核照依据写成“AI 建议……”，依据没写“已闭环”就不说已消除、已闭环。
3. 只为【依据】里出现的视角写点评。
4. 【依据】里写有“未关闭的进度预警”的作业，必须如实点明滞后，不得写“按计划推进/进度挺稳/正常”等相反结论；天数、百分比只能照抄依据。
5. 现场圈的读者多是分包技术员、安全员，P6 由港湾计划工程师维护：不得叫任何人（尤其分包岗位）“同步/更新 P6”。
   只有依据里明确带有【P6待同步】标记时，才可如实说“日志累计高于 P6 记录，待计划工程师更新”；没有该标记就不提 P6 是否更新。
只输出JSON：{"comments":[{"view":"safety|quality|progress","text":"点评"}]}"""


# 叫人去“同步/更新 P6（进度计划）”的命令式说法：分包岗位没有 P6 权限，一律不说。
# 宾语必须是 P6/进度计划（“请及时更新整改情况”不算）；“待/由计划工程师更新P6”“P6 未更新”是描述句，不算（后者由事实底稿校验管）。
_P6_OBJ = r"(?:P6|进度计划)"
_CLAUSE_END = r"，。；！？,;!?\n"
SYNC_ASK_RE = re.compile(
    rf"(?:记得|请|麻烦|尽快|及时|赶紧|需要|要|帮忙)[^{_CLAUSE_END}]{{0,12}}?{_P6_OBJ}[^{_CLAUSE_END}未没已不]{{0,6}}?(?:同步|更新)"  # 记得把P6更新一下
    rf"|(?:同步|更新)(?:一下|下)?\s*(?:到\s*)?{_P6_OBJ}")                                                                       # 更新P6 / 同步一下 P6
_DESC_RE = re.compile(r"[待由]|[未没已不]$")  # 命中前同一分句里有“待/由”，或紧挨着“未/没/已/不”：描述句


def sync_ask_problems(text: str) -> list[str]:
    """只用于进度视角的点评。"""
    text = text or ""
    for hit in SYNC_ASK_RE.finditer(text):
        head = re.split(f"[{_CLAUSE_END}]", text[:hit.start()])[-1]  # 同一分句里命中之前的部分
        if _DESC_RE.search(head):
            continue
        return [f"出现“{hit.group()}”：不得叫现场人员同步/更新 P6（P6 由计划工程师维护）"]
    return []


def _write(post: dict, by_view: dict[str, list[dict]], user_id: int | None, afacts: list[dict]) -> tuple[dict[str, str], int | None, str]:
    summary = f"动态类型：{post['kind']}；时间：{post['time']}" + (f"；正文：{post['text'][:100]}" if post["text"] else "")
    lines = "\n".join(f"[{v}] " + " / ".join(f['text'] for f in fs) for v, fs in by_view.items())
    templ = {v: _template(fs) for v, fs in by_view.items()}

    def call(feedback: str):
        return call_json("feed.comment", system=COMMENT_SYSTEM, user=f"{summary}\n【依据】\n{lines}{feedback}", prompt_version=PROMPT_VERSION,
                         mock=lambda: {"comments": [{"view": v, "text": t} for v, t in templ.items()]},
                         validate=lambda d: _norm_comments(d, by_view, _open_rect(post)), project_id=post["project_id"], user_id=user_id,
                         max_tokens=800, temperature=0.4, input_meta={"post": post["key"], "views": list(by_view)})
    try:
        # 进度点评须与事实底稿一致（预警/天数/百分比/P6 同步提醒）；不一致重试一次，仍不过用依据模板
        g = F.guarded(call, lambda d: d.get("progress", ""), afacts, lambda: templ, extra=lines,
                      check=lambda d: sync_ask_problems(d.get("progress", "")))  # 只校验进度视角
        return {**templ, **g.data}, g.run_id, "ai" if g.source == "ai" else "rule"
    except AIError as exc:
        log.warning("现场圈点评改写失败，使用模板：%s", exc)
        return templ, None, "rule"


# ---------------------------------------------------------------------------- 进度点评：事实底稿与“无新信息不评”

def _prev_marks(conn, pid: int) -> tuple[dict, dict]:
    """各作业上一次进度点评时的状况签名，以及已提醒过“P6 待同步”的 P6 数据日期（取最近一次）。"""
    sigs, syncs = {}, {}
    for r in all_(conn, "SELECT evidence_json FROM feed_agent_notes WHERE project_id=? AND perspective='progress' ORDER BY id DESC LIMIT 300", (pid,)):
        for e in loads(r["evidence_json"], []):
            m = e.get("meta") if isinstance(e, dict) else None
            if m:
                for c, v in (m.get("sig") or {}).items():
                    sigs.setdefault(c, v)
                for c, v in (m.get("p6_sync") or {}).items():
                    syncs.setdefault(c, v)
    return sigs, syncs


def _progress_for_comment(conn, pid: int, prog: list[dict]) -> tuple[list[dict], dict, list[dict]]:
    """把进度依据换成点评用的版本：同一作业与上次点评相比状况未变（无新预警/新偏差/里程碑变化）不再评；
    有未关闭预警的作业附上预警事实；“P6 待同步”在一个 P6 数据日期内同一作业只提醒一次。返回 (依据, 记入点评的标记, 作业底稿)。"""
    ctx = F.context(conn, pid)
    sigs, syncs = _prev_marks(conn, pid)
    kept, afacts, meta = [], [], {"sig": {}, "p6_sync": {}}
    for pf in prog:
        code = pf["ref"]["code"]
        af = F.activity_facts(conn, pid, code, ctx)
        sig = F.signature(af)
        if sigs.get(code) == sig:
            continue
        kept.append(pf)
        meta["sig"][code] = sig
        if af["has_open_alert"]:
            lag = f"滞后 {af['slip_days']:g} 天" if af["slip_days"] else af["rule_name"]
            kept.append(_f("progress", f"该作业有未关闭的进度预警（{F.LEVEL_NAME.get(af['level'], '')}）：{lag}",
                           {"type": "alert", "id": af["alert_id"], "label": f"预警 #{af['alert_id']}"}))
        dd = af["p6_data_date"]
        if af["pct_log"] is not None and af["pct_p6"] is not None and af["pct_log"] > af["pct_p6"] and dd and syncs.get(code) != dd:
            af["p6_sync_due"] = True
            meta["p6_sync"][code] = dd
            kept.append(_f("progress", f"【P6待同步】日志最新累计 {af['pct_log']:g}% 高于 P6 记录的 {af['pct_p6']:g}%（P6 数据日期 {dd}）", pf["ref"]))
        afacts.append(af)
    return kept, meta, afacts


def ensure_comments(conn, user: dict, key: str) -> list[str]:
    """为一条动态生成尚缺的点评（幂等）。返回本次新生成的视角。无依据的视角不生成。"""
    feed_svc._author_of(conn, user, key)  # 看不到的动态不处理
    pid = user["project_id"]
    with _lock_for(pid, key):
        post = load_post(conn, key)
        if not post or post["project_id"] != pid:
            return []
        have = {r["perspective"] for r in all_(conn, "SELECT perspective FROM feed_agent_notes WHERE project_id=? AND post_key=? AND perspective<>'qa'", (pid, key))}
        facts = collect_facts(conn, post)
        meta, afacts = None, []
        if "progress" not in have and any(f["view"] == "progress" for f in facts):
            kept, meta, afacts = _progress_for_comment(conn, pid, [f for f in facts if f["view"] == "progress"])
            facts = [f for f in facts if f["view"] != "progress"] + kept
            for i, f in enumerate(facts, 1):
                f["id"] = f"F{i}"
        by_view: dict[str, list[dict]] = defaultdict(list)
        for f in facts:
            if f["view"] not in have:
                by_view[f["view"]].append(f)
        if not by_view:
            return []
        texts, run_id, source = _write(post, dict(by_view), user["id"], afacts)
        made = []
        for v, fs in by_view.items():
            ev = [{"text": f["text"], "ref": f["ref"]} for f in fs]
            if v == "progress" and meta:
                ev.append({"meta": meta})  # 供下次判断“有无新信息”，不在评论依据里显示
            cur = conn.execute("INSERT OR IGNORE INTO feed_agent_notes (project_id, post_key, perspective, text, evidence_json, source, ai_run_id, audience, created_at) "
                               "VALUES (?,?,?,?,?,?,?,?,?)",
                               (pid, key, v, texts[v], dumps(ev), source, run_id, "all", now_iso()))
            if cur.rowcount:
                made.append(v)
        return made


def review(conn, user: dict, keys: list[str]) -> tuple[dict[str, list[dict]], list[str]]:
    """前端打开现场圈后异步调用：为这些动态补点评，返回 (新增点评, 本次没轮到的动态键)。
    一次最多处理 REVIEW_BATCH 条有依据的动态，剩下的由前端接着请求。"""
    done: dict[str, list[dict]] = {}
    budget = REVIEW_BATCH
    keys = list(dict.fromkeys(keys))
    for i, key in enumerate(keys):
        if budget <= 0:
            return done, keys[i:]
        try:
            made = ensure_comments(conn, user, key)
        except ValueError:
            continue
        if made:
            budget -= 1
            done[key] = [c for c in attach(conn, user, [key]).get(key, []) if c["agent"]["view"] in made]
    return done, []


# ============================================================================ 读取并附到动态

def _link(user: dict, ref: dict | None) -> dict | None:
    if not ref:
        return None
    t = ref.get("type")
    if t == "finding":
        return {"route": "finding", "params": {"id": ref["id"]}}
    if t == "log":
        ok = user["role"] != "sub_safety" and (not R.is_sub(user["role"]) or ref.get("author_id") == user["id"])
        return {"route": "log", "params": {"id": ref["id"]}} if ok else None
    if t == "alert" and not R.is_sub(user["role"]):
        return {"route": "alert", "params": {"id": ref["id"]}}
    return None


def _evidence(user: dict, items: list[dict], fix: str = "") -> list[dict]:
    """fix：整改尚未闭环时，旧依据里的肯定说法（“已确认消除”）换成当前的复核事实。"""
    return [{"text": fix if fix and closure_problems(e["text"]) else e["text"], "label": (e.get("ref") or {}).get("label") or "",
             "link": _link(user, e.get("ref"))} for e in items if "text" in e]


def _open_rect_facts(conn, rows: list[dict]) -> dict[str, str]:
    """有安全点评的整改动态中，隐患还没由人确认闭环的：动态键 → 当前的复核事实。"""
    out = {}
    for key in dict.fromkeys(r["post_key"] for r in rows if r["perspective"] == "safety" and r["post_key"].startswith("rect:")):
        post = load_post(conn, key)
        if _open_rect(post):
            out[key] = "整改照片已提交，" + verify_fact(conn, post["finding"])
    return out


def _stale_reasons(conn, pid: int, rows: list[dict], rect: dict[str, str]) -> dict[int, list[str]]:
    """进度点评按当前事实复核（点评只生成一次，旧点评可能早于矛盾校验或情况已变）：
    与未关闭预警矛盾、滞后天数/浮时对不上、无【P6待同步】依据却提 P6、叫人同步 P6 —— 判为失效，原文保留。
    安全点评：整改还没由人确认闭环，点评里却写着“已消除”这类肯定说法（T-13 之前生成的）—— 同样判为失效。"""
    out = {r["id"]: p for r in rows if r["post_key"] in rect and r["perspective"] == "safety" and (p := closure_problems(r["text"]))}
    prog = [r for r in rows if r["perspective"] == "progress"]
    if not prog:
        return out
    ctx, cache = F.context(conn, pid), {}
    for r in prog:
        ev = [e for e in loads(r["evidence_json"], []) if isinstance(e, dict) and "text" in e]
        afacts = []
        for code in dict.fromkeys((e.get("ref") or {}).get("code") for e in ev if (e.get("ref") or {}).get("type") == "act"):
            if not code:
                continue
            if code not in cache:
                cache[code] = F.activity_facts(conn, pid, code, ctx)
            af = dict(cache[code])
            dd = af.get("p6_data_date")
            # 当时依据里标了【P6待同步】且 P6 数据日期仍是那一期，才允许提 P6 待更新
            af["p6_sync_due"] = bool(dd) and any(e["text"].startswith("【P6待同步】") and f"P6 数据日期 {dd}" in e["text"]
                                                 and (e.get("ref") or {}).get("code") == code for e in ev)
            afacts.append(af)
        probs = F.check_text(r["text"], afacts, extra=" ".join(e["text"] for e in ev), strict_pct=False) if afacts else []
        out[r["id"]] = list(dict.fromkeys(probs + sync_ask_problems(r["text"])))
    return out


def attach(conn, user: dict, keys: list[str]) -> dict[str, list[dict]]:
    """读出这些动态下的 Agent 评论（分包单位看不到仅港湾可见的问答）。进度点评附当前事实复核结果（stale）。"""
    if not keys:
        return {}
    q = f"SELECT * FROM feed_agent_notes WHERE project_id=? AND post_key IN ({_in(keys)})"
    if R.is_sub(user["role"]):
        q += " AND audience='all'"
    out: dict[str, list[dict]] = defaultdict(list)
    rows = all_(conn, q + " ORDER BY id", [user["project_id"], *keys])
    rect = _open_rect_facts(conn, rows)
    stale = _stale_reasons(conn, user["project_id"], rows, rect)
    for r in rows:
        meta = agent_meta(r["perspective"])
        fix = rect.get(r["post_key"], "") if r["perspective"] == "safety" else ""
        out[r["post_key"]].append({
            "id": f"a{r['id']}", "name": meta["name"], "role_name": "Agent", "text": r["text"], "time": r["created_at"], "mine": False,
            "agent": {**meta, "source": r["source"], "evidence": _evidence(user, loads(r["evidence_json"], []), fix),
                      "qa": r["perspective"] == "qa", "question": r["question"] or "", "scope": r["scope"] or "",
                      "audience": r["audience"], "mine": bool(r["asked_by"] and r["asked_by"] == user["id"]), "raw_id": r["id"],
                      "stale": bool(stale.get(r["id"])), "stale_reasons": stale.get(r["id"], [])}})
    return out


def delete_answer(conn, user: dict, raw_id: int) -> None:
    r = one(conn, "SELECT * FROM feed_agent_notes WHERE id=? AND project_id=? AND perspective='qa'", (raw_id, user["project_id"]))
    if not r:
        raise ValueError("回答不存在")
    if r["asked_by"] != user["id"]:
        raise PermissionError("只能删除自己提问得到的回答")
    conn.execute("DELETE FROM feed_agent_notes WHERE id=?", (raw_id,))


# ============================================================================ 接力时间线

def _run_note(conn, run_id: int | None) -> tuple[str, str | None]:
    r = one(conn, "SELECT * FROM ai_runs WHERE id=?", (run_id,)) if run_id else None
    return (f"（{r['model']}，{r['latency_ms'] or 0} 毫秒）", r["created_at"]) if r else ("", None)


def relay(conn, user: dict, key: str) -> list[dict]:
    """一条动态经过各 Agent 的处理轨迹：现场记录 → 安全 → 质量 → 数据审核 → 进度。每步都来自真实记录，没有就不出现。"""
    feed_svc._author_of(conn, user, key)
    post = load_post(conn, key)
    if not post:
        return []
    pid = post["project_id"]
    author = one(conn, "SELECT * FROM users WHERE id=?", (post["author_id"],))
    steps: list[dict] = []

    def add(agent: str, text: str, time: str | None = None, ref: dict | None = None) -> None:
        a = AGENT_BY_KEY[agent]
        steps.append({"agent": agent, "name": a["name"], "icon": a["icon"], "text": text, "time": time, "link": _link(user, ref), "label": (ref or {}).get("label", "")})

    # 1 现场记录Agent
    lg = post["log"]
    n_media = len(post["media"])
    if post["kind"] == "note":
        add("scribe", f"收到{'口述' if post['voice'] else '文字'}随手记" + (f"，附 {n_media} 张照片" if n_media else ""), post["time"])
        day_log = one(conn, """SELECT * FROM logs WHERE project_id=? AND author_id=? AND log_date=? AND mode='ai' AND period='day' AND ai_run_id IS NOT NULL""",
                      (pid, post["author_id"], post["day"]))
        if day_log:
            add("scribe", f"当天的施工日志（L{day_log['id']}）由 AI 整理成草稿", day_log["drafted_at"],
                {"type": "log", "id": day_log["id"], "author_id": day_log["author_id"], "label": f"日志 L{day_log['id']}"})
            lg = lg or day_log
    elif post["kind"] == "snap":
        add("scribe", f"收到 {n_media} 张随手拍照片" + (f"，地点：{post['place']}" if post["place"] else ""), post["time"])
    elif post["kind"] == "rect":
        add("scribe", "收到整改照片", post["time"])
    elif lg and lg["drafted_at"]:
        ratio = lg["edit_ratio"]
        add("scribe", f"把本期记录整理成{'周进度确认' if lg['period'] == 'week' else '施工日志'}草稿"
            + (f"，草稿经人工修改约 {round(ratio * 100)}%" if ratio is not None and user["role"] not in R.PM_SCOPE else ""), lg["drafted_at"],  # 修改率属应用成效，不给项目经理
            {"type": "log", "id": lg["id"], "author_id": lg["author_id"], "label": f"日志 L{lg['id']}"})

    # 2 安全Agent
    hz = _hazards(conn, post)
    if hz:
        note, t = _run_note(conn, hz[0]["ai_run_id"])
        add("safety", f"识别出 {len(hz)} 处隐患：" + "；".join(f"{f['title']}（{LEVEL_NAME[f['level']]}，{findings.status_label(f)}）" for f in hz[:3]) + note,
            t or hz[0]["created_at"], {"type": "finding", "id": hz[0]["id"], "label": f"隐患 #{hz[0]['id']}"})
    elif post["media"] and post["kind"] != "rect":  # 整改照片不做隐患识别（状态一直是 pending），这一步看下面的整改复核
        ms = all_(conn, f"SELECT safety_status FROM media WHERE id IN ({_in(post['media'])})", post["media"])
        st = {m["safety_status"] for m in ms}
        if st == {"clear"}:
            add("safety", "逐张识别完毕，没有发现明显隐患")
        elif "failed" in st:
            add("safety", "识别未成功，等待重新识别")
        elif "pending" in st:
            add("safety", "识别中")
    fd = post["finding"]
    if fd and (fd["verify_json"] or fd["status"] == "resolved"):
        v = loads(fd["verify_json"], {}) or {}
        add("safety", "对照整改前后照片复核 · " + verify_fact(conn, fd), v.get("at") or (fd["feedback_at"] if fd["status"] == "resolved" else None),
            {"type": "finding", "id": fd["id"], "label": f"隐患 #{fd['id']}"})

    # 3 质量Agent
    acts = _post_activities(conn, post, author)
    for f in _quality_facts(conn, post, acts):
        add("quality", f["text"], None, f["ref"])

    # 4 数据审核Agent：动态本身是日志，或照片/随手记已进入某份日志。项目经理不审核日志，这一步对项目经理不出现（AGENTS.md）
    log_ids = []
    if lg:
        log_ids.append(lg["id"])
    if post["media"]:
        log_ids += [r["log_id"] for r in all_(conn, f"SELECT DISTINCT log_id FROM media WHERE id IN ({_in(post['media'])}) AND log_id IS NOT NULL", post["media"])]
    for lid in dict.fromkeys(log_ids if user["role"] not in R.PM_SCOPE else []):
        row = one(conn, "SELECT * FROM logs WHERE id=?", (lid,))
        dq = loads(row["dq_json"], None) if row else None
        # 预审细节只给港湾岗位和日志作者本人，分包单位其他同事看不到
        if dq and (not R.is_sub(user["role"]) or row["author_id"] == user["id"]):
            add("dq", f"数据审核评分 {dq.get('score')}，风险{ {'low': '低', 'medium': '中', 'high': '高'}.get(dq.get('risk'), '未知') }"
                + (f"：{dq['summary']}" if dq.get("summary") else ""), dq.get("at"),
                {"type": "log", "id": row["id"], "author_id": row["author_id"], "label": f"日志 L{row['id']}"})
            break

    # 5 进度Agent
    if post["kind"] == "log" and post["log"]:
        rows = all_(conn, "SELECT activity_code, qty, unit, pct FROM log_progress WHERE log_id=? AND voided_at IS NULL ORDER BY id", (post["log"]["id"],))
        if rows and post["log"]["status"] in ("submitted", "approved"):
            add("progress", f"本期确认的 {len(rows)} 项作业进度已计入进度计划：" + "、".join(
                f"{r['activity_code']}" + (f" 累计{r['pct']:g}%" if r["pct"] is not None else "") for r in rows[:4]), post["log"]["submitted_at"])
    for f in _progress_facts(conn, post, acts):
        add("progress", f["text"], None, f["ref"])
    if acts and not R.is_sub(user["role"]):
        for al in all_(conn, f"SELECT * FROM alerts WHERE project_id=? AND activity_code IN ({_in([a['code'] for a in acts])}) AND status='open' AND rule<>'stale' ORDER BY id DESC LIMIT 2",
                       [pid, *[a["code"] for a in acts]]):
            add("progress", f"该作业有进行中的进度预警：{al['title']}", al["last_detected"], {"type": "alert", "id": al["id"], "label": f"预警 #{al['id']}"})
    return steps


# ============================================================================ @工笔

ASK_SYSTEM = """你是施工项目助理“工笔”，在现场圈一条动态下回答同事的提问。
只依据【本动态可见内容】作答，每个事实后用方括号标出处（如[F1]）；内容里没有的就说“这条动态里没有找到……”，不要猜测。
不得出现内容以外的数字；回答 120 字以内。只输出JSON：{"answer":"","refs":["F1"]}"""


def _terms(q: str) -> set[str]:
    return {q[i:i + 2] for i in range(len(q) - 1) if re.fullmatch(r"[一-龥]{2}", q[i:i + 2])}


def _scoped_answer(conn, user: dict, post: dict, facts: list[dict], q: str) -> dict:
    author = one(conn, "SELECT name FROM users WHERE id=?", (post["author_id"],))
    base = [{"id": "P0", "view": "post", "ref": None,
             "text": f"{(author or {}).get('name', '同事')}在 {post['time'][:16].replace('T', ' ')} 发布的动态" + (f"：{post['text'][:120]}" if post["text"] else "")
             + (f"，地点 {post['place']}" if post["place"] else "") + (f"，附 {len(post['media'])} 张照片" if post["media"] else "")}]
    pool = base + facts
    allowed = _digits(" ".join(f["text"] for f in pool) + q)

    def mock():
        terms = _terms(q)
        hit = [f for f in facts if terms & _terms(f["text"])][:3]
        if not hit and not (terms & _terms(base[0]["text"])):
            return {"answer": "这条动态里没有找到能回答这个问题的记录（分包单位账号只能依据本动态可见内容回答）。", "refs": []}
        use = hit or base
        return {"answer": "依据本动态相关记录：" + "；".join(f"{f['text']}[{f['id']}]" for f in use), "refs": [f["id"] for f in use]}

    def norm(d):
        if not isinstance(d, dict) or not str(d.get("answer") or "").strip():
            raise ValueError("缺少answer")
        ans = str(d["answer"]).strip()
        if len(ans) > 400:
            raise ValueError("回答过长")
        extra = _digits(re.sub(r"\[[A-Z]\d+\]", "", ans)) - allowed
        if extra:
            raise ValueError(f"回答出现依据之外的数字：{sorted(extra)[:3]}")
        ids = {f["id"] for f in pool}
        return {"answer": ans, "refs": [r for r in (str(x) for x in d.get("refs") or []) if r in ids]}
    res = call_json("feed.comment", system=ASK_SYSTEM, user=f"问题：{q}\n【本动态可见内容】\n" + "\n".join(f"[{f['id']}] {f['text']}" for f in pool),
                    prompt_version=PROMPT_VERSION, mock=mock, validate=norm, project_id=post["project_id"], user_id=user["id"],
                    max_tokens=600, temperature=0.2, input_meta={"post": post["key"], "scoped": True})
    by_id = {f["id"]: f for f in pool}
    return {"answer": res.data["answer"], "evidence": [{"text": by_id[r]["text"], "ref": by_id[r]["ref"]} for r in res.data["refs"]], "run_id": res.run_id}


def _qa_evidence(conn, user: dict, refs: list[str]) -> list[dict]:
    """问工笔的出处：日志[L12]、预警[W3]、P6作业[A1010]。"""
    out = []
    for r in refs[:8]:
        if re.fullmatch(r"L\d+", r):
            lg = one(conn, "SELECT id, author_id, log_date FROM logs WHERE id=? AND project_id=?", (int(r[1:]), user["project_id"]))
            if lg:
                out.append({"text": f"日志 {r}（{lg['log_date']}）", "ref": {"type": "log", "id": lg["id"], "author_id": lg["author_id"], "label": f"日志 {r}"}})
        elif re.fullmatch(r"W\d+", r):
            out.append({"text": f"进度预警 {r}", "ref": {"type": "alert", "id": int(r[1:]), "label": f"预警 {r}"}})
        else:
            out.append({"text": f"P6 作业 {r}", "ref": None})
    return out


def ask(conn, user: dict, key: str, text: str) -> dict:
    """评论里以 @工笔 开头的提问：港湾岗位走问工笔（全项目记录），分包单位只基于该动态可见内容回答。"""
    feed_svc._author_of(conn, user, key)
    text = (text or "").strip()
    if not text.startswith("@工笔"):
        raise ValueError("请以 @工笔 开头提问")
    q = text[len("@工笔"):].strip(" ：:，,")
    if not q:
        raise ValueError("请在 @工笔 后面写下你的问题")
    if len(text) > feed_svc.MAX_COMMENT:
        raise ValueError(f"提问最多 {feed_svc.MAX_COMMENT} 字")
    used = one(conn, "SELECT COUNT(*) n FROM feed_agent_notes WHERE asked_by=? AND substr(created_at,1,10)=?", (user["id"], date.today().isoformat()))["n"]
    if used >= ASK_PER_DAY:
        raise ValueError(f"今天已经问了 {ASK_PER_DAY} 次，明天再来")
    post = load_post(conn, key)
    if not post:
        raise ValueError("动态不存在")
    if R.is_sub(user["role"]):
        res = _scoped_answer(conn, user, post, collect_facts(conn, post), q)
        answer, evidence, run_id, scope, audience = res["answer"], res["evidence"], res["run_id"], "post", "all"
    else:
        from . import qa
        author = one(conn, "SELECT * FROM users WHERE id=?", (post["author_id"],))
        acts = _post_activities(conn, post, author)
        ctx = f"（涉及作业：{'、'.join(_an.fmt(a['code'], a['name']) for a in acts[:2])}）" if acts else (f"（针对动态：{post['text'][:30]}）" if post["text"] else "")
        r = qa.ask(conn, user, (q + ctx)[:300])
        answer, run_id, scope, audience = r["answer"], None, "project", "harbour"
        evidence = [{"text": e["text"], "ref": e["ref"]} for e in _qa_evidence(conn, user, r["refs"])]
    cm = feed_svc.add_comment(conn, user, key, text)
    nid = insert(conn, "feed_agent_notes", {
        "project_id": user["project_id"], "post_key": key, "perspective": "qa", "text": answer[:1200], "evidence_json": dumps(evidence),
        "source": "ai", "ai_run_id": run_id, "audience": audience, "asked_by": user["id"], "question": q, "reply_to": cm["id"],
        "scope": scope, "created_at": now_iso()})
    row = [c for c in attach(conn, user, [key]).get(key, []) if c["agent"]["raw_id"] == nid][0]
    return {"comment": cm, "answer": row}
