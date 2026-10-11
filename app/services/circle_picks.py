"""现场圈 AI精选：现场圈Agent 从近 7 天真实动态里挑出“值得大家看”的几条，并写一句理由。

只挑动态，不排名人、不评奖、不造数：候选必须来自库里的真实记录，规则优先级（有数据才入选）：
  ① 整改照片，且对应隐患已复核闭环（前后对比）
  ② 随手拍识别出高/中风险隐患，并已派单
  ③ 周进度确认已提交，且数据审核为低风险（评分不低于 90）；理由文案不出现“数据审核”与评分（项目经理也看精选）
每次最多新增 3 条；AI 在候选里挑选并写 ≤40 字理由（只说明价值、不点名批评、不比较名次），
AI 失败或理由里出现候选事实之外的数字时，回退为规则模板理由（source='rule'）。没有候选就不生成。

精选是项目级记录，全项目可见（10-09 起现场圈不再按单位过滤）；读取时项目经理另过简报范围守卫。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from ..ai.client import AIError, call_json
from ..db import all_, audit, dumps, insert, loads, now_iso, one

log = logging.getLogger(__name__)

PICK_VERSION = "feed-pick-v1-20261006"
WINDOW_DAYS = 7
MAX_PER_ROUND = 3
MAX_CANDIDATES = 12
MAX_REASON = 40
MIN_DQ_SCORE = 90
LEVEL_ZH = {"high": "高", "medium": "中"}


def _since() -> str:
    return (datetime.now().astimezone() - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")


def _names(conn, project_id: int) -> dict[int, str]:
    return {u["id"]: u["name"] for u in all_(conn, "SELECT id, name FROM users WHERE project_id=?", (project_id,))}


# ----------------------------------------------------------------------------- 候选

def candidates(conn, project_id: int) -> list[dict]:
    """近 7 天、尚未被精选的真实动态候选，按规则优先级再按时间新到旧排序。每条带只含库内真实字段的事实摘要。"""
    since = _since()
    done = {r["post_key"] for r in all_(conn, "SELECT post_key FROM circle_picks WHERE project_id=?", (project_id,))}
    out: list[dict] = []
    covered: set[int] = set()  # 已作为“整改闭环”入选候选的隐患，不再作为随手拍候选重复出现

    # ① 整改闭环：整改照片动态，对应隐患已复核闭环
    for r in all_(conn, """SELECT m.id mid, m.uploader_id, m.uploaded_at, f.id fid, f.title, f.level, f.ref_type FROM media m
                           JOIN findings f ON f.rectify_media_id=m.id AND f.project_id=m.project_id
                           WHERE m.project_id=? AND m.purpose='rectify' AND m.uploaded_at>=? AND f.status='resolved'
                           ORDER BY m.uploaded_at DESC""", (project_id, since)):
        covered.add(r["fid"])
        key = f"rect:{r['mid']}"
        if key in done:
            continue
        done.add(key)  # 一张整改照片可能对应多条隐患，同一条动态只出现一次
        facts = f"整改闭环：隐患“{r['title']}”经复核已闭环" + ("，有整改前后对比照片" if r["ref_type"] == "media" else "")
        out.append({"key": key, "rule": 1, "time": r["uploaded_at"], "author_id": r["uploader_id"], "facts": facts,
                    "tpl": f"整改已复核闭环：{r['title']}" + ("，前后对比一目了然" if r["ref_type"] == "media" else "") })

    # ② 随手拍：识别出高/中风险隐患并已派单
    snaps: dict[str, dict] = {}
    for f in all_(conn, """SELECT f.id, f.level, f.title, m.uploader_id, m.uploaded_at, m.id mid FROM findings f
                           JOIN media m ON m.id=f.ref_id AND m.project_id=f.project_id
                           WHERE f.project_id=? AND f.agent='safety' AND f.kind='hazard' AND f.ref_type='media' AND f.level IN ('high','medium')
                           AND f.assignee_id IS NOT NULL AND f.confirmed_at IS NOT NULL AND f.status<>'dismissed' AND m.purpose='safety' AND m.uploaded_at>=?
                           ORDER BY f.id""", (project_id, since)):
        if f["id"] in covered:
            continue
        first = one(conn, "SELECT MIN(id) k FROM media WHERE project_id=? AND purpose='safety' AND uploader_id=? AND substr(uploaded_at,1,16)=substr(?,1,16)",
                    (project_id, f["uploader_id"], f["uploaded_at"]))["k"]
        key = f"snap:{first}"
        if key in done:
            continue
        g = snaps.setdefault(key, {"hz": [], "author_id": f["uploader_id"], "time": f["uploaded_at"]})
        g["hz"].append(f)
        g["time"] = max(g["time"], f["uploaded_at"])
    for key, g in snaps.items():
        hz = g["hz"]
        by = {lv: sum(1 for h in hz if h["level"] == lv) for lv in ("high", "medium")}
        parts = "、".join(f"{LEVEL_ZH[lv]}风险 {n} 处" for lv, n in by.items() if n)
        out.append({"key": key, "rule": 2, "time": g["time"], "author_id": g["author_id"],
                    "facts": f"随手拍识别出隐患：{parts}，均已派单整改；首项“{hz[0]['title']}”",
                    "tpl": f"随手拍识别出{parts.replace(' ', '')}隐患，已派单跟进"})

    # ③ 周进度确认已提交，数据审核低风险。精选对全项目可见（含项目经理）：事实与理由只写“AI 核对未见明显问题”，不写“数据审核”与评分
    for lg in all_(conn, """SELECT id, author_id, submitted_at, dq_json FROM logs WHERE project_id=? AND mode='ai' AND period='week'
                            AND status IN ('submitted','approved') AND submitted_at IS NOT NULL AND submitted_at>=? AND dq_json IS NOT NULL
                            ORDER BY submitted_at DESC""", (project_id, since)):
        key = f"log:{lg['id']}"
        dq = loads(lg["dq_json"], {}) or {}
        score = dq.get("score")
        if key in done or dq.get("risk") != "low" or not isinstance(score, (int, float)) or score < MIN_DQ_SCORE:
            continue
        out.append({"key": key, "rule": 3, "time": lg["submitted_at"], "author_id": lg["author_id"],
                    "facts": "周进度确认已提交，AI 核对未见明显问题",
                    "tpl": "周进度确认已提交，AI 核对未见明显问题"})

    out.sort(key=lambda c: c["time"], reverse=True)
    out.sort(key=lambda c: c["rule"])
    return out[:MAX_CANDIDATES]


# ----------------------------------------------------------------------------- AI 挑选与理由

def _norm(data, cands: list[dict], names: dict[int, str]) -> list[dict]:
    """校验模型输出：key 必须是候选、不重复、理由非空且 ≤40 字、不出现候选事实之外的数字、不点名。不合格整体作废，回退规则。"""
    rows = data.get("picks") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("缺少 picks")
    by_key = {c["key"]: c for c in cands}
    out, seen = [], set()
    for r in rows[:MAX_PER_ROUND]:
        key = str((r or {}).get("key") or "")
        reason = str((r or {}).get("reason") or "").strip()
        c = by_key.get(key)
        if not c or key in seen:
            raise ValueError(f"key 不在候选内或重复：{key}")
        if not reason or len(reason) > MAX_REASON:
            raise ValueError(f"理由为空或超过 {MAX_REASON} 字")
        bad = [d for d in re.findall(r"\d+", reason) if d not in set(re.findall(r"\d+", c["facts"]))]
        if bad:
            raise ValueError(f"出现候选事实之外的数字：{bad[:3]}")
        who = names.get(c["author_id"]) or ""
        if len(who) >= 2 and who in reason:
            raise ValueError("理由不得点名")
        seen.add(key)
        out.append({"key": key, "reason": reason})
    return out


PICK_SYSTEM = ("你是现场圈Agent，从【候选动态】里挑出最值得全项目同事看的动态（最多 3 条，宁缺毋滥），并为每条写一句理由（40 字内）。"
               "只表扬、说明这条动态的价值；不点名批评、不写人名、不比较名次、不评奖；理由里不得出现该候选【事实】之外的数字。"
               "只输出JSON：{\"picks\":[{\"key\":\"候选的key\",\"reason\":\"\"}]}")


def _choose(conn, project_id: int, cands: list[dict]) -> tuple[list[dict], int | None, str]:
    """返回 (入选[{key, reason}], ai_run_id, source)。AI 失败或不合格时取规则优先级前 3 条并用模板理由。"""
    rule = [{"key": c["key"], "reason": c["tpl"][:MAX_REASON]} for c in cands[:MAX_PER_ROUND]]
    names = _names(conn, project_id)
    brief = [{"key": c["key"], "类型": {1: "整改闭环", 2: "已派单的高中风险隐患", 3: "低风险周确认"}[c["rule"]], "事实": c["facts"]} for c in cands]
    try:
        r = call_json("feed.pick", system=PICK_SYSTEM, user=f"【候选动态】{dumps(brief)}", prompt_version=PICK_VERSION,
                      mock=lambda: {"picks": rule}, validate=lambda d: _norm(d, cands, names), project_id=project_id,
                      max_tokens=600, temperature=0.3, input_meta={"candidates": len(cands)})
        return r.data, r.run_id, "ai"
    except AIError as exc:
        log.warning("AI精选生成失败，改用规则模板理由：%s", exc)
    return rule, None, "rule"


def generate_picks(conn, project_id: int) -> list[dict]:
    """挑一轮 AI精选，返回新增的精选记录；没有候选就不生成。"""
    cands = candidates(conn, project_id)
    if not cands:
        return []
    chosen, run_id, source = _choose(conn, project_id, cands)
    out = []
    for c in chosen[:MAX_PER_ROUND]:
        pid = insert(conn, "circle_picks", {"project_id": project_id, "post_key": c["key"], "reason": c["reason"], "source": source,
                                            "ai_run_id": run_id, "created_at": now_iso()})
        out.append({"id": pid, "post_key": c["key"], "reason": c["reason"], "source": source})
    if out:
        audit(conn, None, "circle.pick", f"project:{project_id}", f"{source} {len(out)}条")
    return out


_auto_tried: dict[int, float] = {}


def maybe_auto_pick(conn, project_id: int) -> list[dict]:
    """打开现场圈时自动挑选：同项目内存里 10 分钟内最多试一次；当天已自动挑过一轮（有精选记录）就不再挑。返回新增记录（可能为空）。"""
    if one(conn, "SELECT 1 x FROM circle_picks WHERE project_id=? AND substr(created_at,1,10)=?", (project_id, now_iso()[:10])):
        return []
    if time.monotonic() - _auto_tried.get(project_id, -1e9) < 600:
        return []
    _auto_tried[project_id] = time.monotonic()
    return generate_picks(conn, project_id)
