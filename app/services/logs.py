"""施工日志：起草（AI/手工对照）→ 规则交叉校验 → 技术员确认提交 → 项目部审核。

计时与修改率在提交时自动记录，作为成效数据的唯一来源（口径见 metrics.py）。
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import date, datetime, timedelta

from .. import roles as R
from ..ai import log_agent
from ..ai import pool as ai_pool
from ..ai.client import AIError, call_json
from ..config import settings
from ..db import all_, audit, dumps, event, insert, loads, now_iso, one, tx
from . import p6
from . import review as review_svc


class LogError(ValueError):
    pass


class Conflict(LogError):
    pass


def _today() -> str:
    return date.today().isoformat()


def get_log(conn, log_id: int) -> dict | None:
    return one(conn, "SELECT * FROM logs WHERE id=?", (log_id,))


def week_of(day: str) -> tuple[str, str]:
    d = date.fromisoformat(day)
    s = d - timedelta(days=d.weekday())
    return s.isoformat(), (s + timedelta(days=6)).isoformat()


def ensure_log(conn, user: dict, log_date: str | None, mode: str = "ai", period: str = "day", auto: bool = False) -> dict:
    log_date = log_date or _today()
    date.fromisoformat(log_date)
    if mode not in ("ai", "manual"):
        raise LogError("mode 只能是 ai 或 manual")
    if period not in ("day", "week"):
        raise LogError("period 只能是 day 或 week")
    period_end = None
    if period == "week":
        log_date, period_end = week_of(log_date)
    row = one(conn, "SELECT * FROM logs WHERE project_id=? AND author_id=? AND log_date=? AND mode=? AND period=?",
              (user["project_id"], user["id"], log_date, mode, period))
    if row:
        return row
    with tx(conn):
        log_id = insert(conn, "logs", {
            "project_id": user["project_id"], "author_id": user["id"], "log_date": log_date, "mode": mode,
            "content_json": dumps(log_agent.empty_content()), "started_at": now_iso(), "period": period,
            "period_end": period_end, "auto": 1 if auto else 0,
        })
        event(conn, "log.start", project_id=user["project_id"], user_id=user["id"], ref_id=log_id, data={"mode": mode, "period": period})
    return get_log(conn, log_id)


def photos_of(conn, log_id: int) -> list[dict]:
    return all_(conn, "SELECT * FROM media WHERE log_id=? ORDER BY COALESCE(taken_at, uploaded_at), id", (log_id,))


def attach_photo(conn, log: dict, media_id: int, user: dict) -> None:
    _editable(log, user)
    m = one(conn, "SELECT * FROM media WHERE id=? AND project_id=?", (media_id, log["project_id"]))
    if not m or m["uploader_id"] != user["id"]:
        raise LogError("照片不存在或不属于你")
    if m["log_id"] and m["log_id"] != log["id"]:
        raise LogError("照片已用于其他日志")
    conn.execute("UPDATE media SET log_id=? WHERE id=?", (log["id"], media_id))


def detach_photo(conn, log: dict, media_id: int, user: dict) -> None:
    _editable(log, user)
    conn.execute("UPDATE media SET log_id=NULL WHERE id=? AND log_id=?", (media_id, log["id"]))


def _editable(log: dict, user: dict) -> None:
    if log["author_id"] != user["id"]:
        raise LogError("只能编辑自己的日志")
    if log["status"] not in ("draft", "returned"):
        raise LogError("日志已提交，不能修改；如需更正请联系审核人退回")


# ----------------------------------------------------------------------------- 进度现状

def progress_state(conn, project_id: int, import_row: dict | None, before: str | None = None) -> dict[str, dict]:
    """各作业的最新累计完成%与数据日期后累计完成量（只计已提交/已审核日志，失效行不计）。

    qty 为合计（自报+核定，口径不变）；qty_self=分包自报（日志待审核），qty_approved=港湾核定（日志已审核），
    log_ids_self / log_ids_approved 为来源日志编号。
    """
    if not import_row:
        return {}
    params: list = [project_id, import_row["data_date"]]
    cond = ""
    if before:
        cond = " AND lp.log_date < ?"
        params.append(before)
    rows = all_(conn, f"""
        SELECT lp.activity_code, lp.log_date, lp.qty, lp.unit, lp.pct, lp.log_id, l.status
        FROM log_progress lp JOIN logs l ON l.id = lp.log_id
        WHERE lp.project_id=? AND lp.log_date>=? AND l.status IN ('submitted','approved') AND lp.voided_at IS NULL{cond}
        ORDER BY lp.log_date, lp.id""", params)
    state: dict[str, dict] = {}
    for r in rows:
        s = state.setdefault(r["activity_code"], {"pct": None, "pct_date": None, "qty": 0.0, "unit": r["unit"],
                                                  "first_date": r["log_date"], "last_date": r["log_date"], "reports": 0,
                                                  "qty_self": 0.0, "qty_approved": 0.0, "log_ids_self": [], "log_ids_approved": []})
        s["reports"] += 1
        s["last_date"] = r["log_date"]
        key = "approved" if r["status"] == "approved" else "self"
        if r["log_id"] not in s[f"log_ids_{key}"]:
            s[f"log_ids_{key}"].append(r["log_id"])
        if r["qty"] is not None:
            s["qty"] += r["qty"]
            s[f"qty_{key}"] += r["qty"]
            s["unit"] = s["unit"] or r["unit"]
        if r["pct"] is not None:
            s["pct"], s["pct_date"] = r["pct"], r["log_date"]
    return state


def trust_label(st: dict) -> str:
    """某作业进度的可信分级文字（依据 progress_state 的来源日志）：全部核定 / 全部自报 / 部分核定。"""
    has_s, has_a = bool(st.get("log_ids_self")), bool(st.get("log_ids_approved"))
    if has_a and not has_s:
        return "港湾核定"
    if has_s and not has_a:
        return "分包自报"
    if has_s and has_a:
        return "部分核定"
    return ""


def progress_rows_of(conn, log_id: int) -> dict:
    """某份日志的工程量记录：当前有效行 + 已失效（退回）的行及原因，供详情页显示。"""
    rows = all_(conn, "SELECT activity_code, qty, unit, pct, voided_at, void_reason FROM log_progress WHERE log_id=? ORDER BY id", (log_id,))
    return {"active": [r for r in rows if not r["voided_at"]], "voided": [r for r in rows if r["voided_at"]]}


def void_progress(conn, log_id: int, reason: str) -> int:
    """把某份日志的有效工程量标记失效（保留原数量与原因，统计一律排除）。"""
    return conn.execute("UPDATE log_progress SET voided_at=?, void_reason=? WHERE log_id=? AND voided_at IS NULL",
                        (now_iso(), (reason or "")[:300], log_id)).rowcount


def _context(conn, log: dict, author: dict) -> dict:
    project = one(conn, "SELECT * FROM projects WHERE id=?", (log["project_id"],))
    imp = p6.current_import(conn, log["project_id"])
    acts = p6.current_activities(conn, log["project_id"])
    window = p6.window_activities(acts, log["log_date"], author.get("wbs_scope", ""))
    state = progress_state(conn, log["project_id"], imp, before=log["log_date"])
    for a in window:
        st = state.get(a["code"])
        a["cur_pct"] = st["pct"] if st and st["pct"] is not None else a.get("phys_pct")
    prev = one(conn, """SELECT * FROM logs WHERE project_id=? AND author_id=? AND log_date<? AND period=? AND mode='ai'
                        AND (status IN ('submitted','approved') OR auto=1) ORDER BY log_date DESC LIMIT 1""",
               (log["project_id"], log["author_id"], log["log_date"], log.get("period") or "day"))
    photos = photos_of(conn, log["id"])
    return {
        "log_date": log["log_date"], "project_name": project["name"] if project else "", "author": author["name"],
        "scope": author.get("wbs_scope", ""), "weather": log["weather"], "voice_text": log["voice_text"],
        "photos": [{"ref": f"P{i + 1}", "id": m["id"], "taken_at": m["taken_at"], "ai_file": m["ai_file"]}
                   for i, m in enumerate(photos[: ai_pool.max_images()])],
        "activities": window, "all_codes": {a["code"]: a for a in acts},
        "yesterday": summarize(loads(prev["content_json"], {})) if prev else "",
        "yesterday_content": loads(prev["content_json"], {}) if prev else {},
        "qa": loads(log.get("qa_json"), []),
        "yesterday_date": prev["log_date"] if prev else None,
        "data_date": imp["data_date"] if imp else None, "period": log.get("period") or "day",
    }


def summarize(content: dict, limit: int = 900) -> str:
    parts = []
    if content.get("summary"):
        parts.append(content["summary"])
    for w in content.get("works", [])[:8]:
        parts.append(f"- {w.get('part', '')}{w.get('process', '')}：{w.get('desc', '')}[{w.get('activity_code', '')}]")
    for p in content.get("progress", [])[:10]:
        parts.append(f"- 进度 {p.get('activity_code')} 量{p.get('qty')}{p.get('unit', '')} 累计{p.get('pct')}%")
    for i in content.get("issues", [])[:5]:
        parts.append(f"- 问题({i.get('type')}): {i.get('desc')}")
    for t in content.get("tomorrow", [])[:5]:
        parts.append(f"- 计划: {t.get('desc')}")
    return "\n".join(parts)[:limit]


# ----------------------------------------------------------------------------- AI 起草

def draft(conn, log: dict, user: dict, voice_text: str | None = None, weather: str | None = None) -> dict:
    _editable(log, user)
    if log["mode"] != "ai":
        raise LogError("手工对照日志不使用 AI")
    if log.get("period") == "week":
        return draft_week(conn, log, user, voice_text)
    if voice_text is not None or weather is not None:
        conn.execute("UPDATE logs SET voice_text=COALESCE(?, voice_text), weather=COALESCE(?, weather) WHERE id=?",
                     (voice_text, weather, log["id"]))
        log = get_log(conn, log["id"])
    ctx = _context(conn, log, user)
    if not ctx["photos"] and not (ctx["voice_text"] or "").strip():
        raise LogError("请至少上传一张照片或输入口述内容")
    refs = [p["ref"] for p in ctx["photos"]]
    result = call_json(
        "log.draft", system=log_agent.SYSTEM, user=log_agent.build_user_prompt(ctx),
        prompt_version=log_agent.PROMPT_VERSION, images=[str(settings.media_dir / p["ai_file"]) for p in ctx["photos"]],
        mock=lambda: log_agent.mock_draft(ctx), validate=lambda d: log_agent.normalize(d, refs),
        project_id=log["project_id"], user_id=user["id"], max_tokens=4000,
        input_meta={"log_id": log["id"], "activities": len(ctx["activities"]), "voice_chars": len(ctx["voice_text"] or ""),
                    "has_yesterday": bool(ctx["yesterday"])},
    )
    content = log_agent.scrub_inferred(result.data, ctx["voice_text"] or "")  # “没记录”不得写成“没施工”
    checks = run_checks(content, ctx)
    # 已回答的问题保留；新问题追加（技术员只需点选）
    qa = [q for q in ctx["qa"] if q.get("answer")]
    asked = {q["question"] for q in qa}
    qa += [{"field": u["field"], "question": u["question"], "options": u.get("options", []), "answer": None}
           for u in content.get("uncertain", []) if u["question"] not in asked]
    with tx(conn):
        conn.execute("""UPDATE logs SET content_json=?, ai_draft_json=?, checks_json=?, ai_run_id=?, drafted_at=?, qa_json=?, version=version+1
                        WHERE id=?""", (dumps(content), dumps(content), dumps(checks), result.run_id, now_iso(), dumps(qa), log["id"]))
        event(conn, "log.draft", project_id=log["project_id"], user_id=user["id"], ref_id=log["id"], value=result.latency_ms,
              data={"provider": result.provider, "model": result.model, "photos": len(refs)})
    return get_log(conn, log["id"])


# ----------------------------------------------------------------------------- 规则交叉校验

def run_checks(content: dict, ctx: dict) -> list[dict]:
    """确定性校验：把需要人看的地方挑出来。level: error 必须处理 / warn 建议核实 / info 提醒。"""
    checks: list[dict] = []
    codes = ctx.get("all_codes") or {}
    window = {a["code"]: a for a in ctx.get("activities") or []}
    week = ctx.get("period") == "week"
    log_day = date.fromisoformat(ctx.get("week_end") or ctx["log_date"])
    when = "本周" if week else "今天"

    def add(level, section, idx, msg):
        checks.append({"level": level, "section": section, "index": idx, "message": msg})

    for sec in ("works", "progress", "labor", "machinery", "materials", "issues"):
        for i, row in enumerate(content.get(sec, [])):
            if not row.get("sources"):
                add("warn", sec, i, "没有标注依据（照片/口述/计划），请确认是否属实")
    for i, row in enumerate(content.get("progress", [])):
        code = row.get("activity_code")
        act = codes.get(code)
        if codes and not act:
            add("error", "progress", i, f"作业代码 {code} 不在当前 P6 计划中")
            continue
        if act and act.get("act_finish"):
            add("warn", "progress", i, f"{code} 在 P6 中已完成，仍有进度填报")
        if act and code not in window and act.get("early_start") and date.fromisoformat(act["early_start"]) > log_day + timedelta(days=7):
            add("warn", "progress", i, f"{code} 计划 {act['early_start']} 才开始，请确认是否提前施工")
        pct = row.get("pct")
        if pct is not None:
            if not 0 <= pct <= 100:
                add("error", "progress", i, "累计完成百分比应在 0~100 之间")
            prev = window.get(code, {}).get("cur_pct") if code in window else (act or {}).get("phys_pct")
            if prev is not None and pct + 0.01 < prev:
                add("error", "progress", i, f"累计完成 {pct}% 低于此前记录 {prev}%")
        if week:
            sm = (ctx.get("sums") or {}).get(code)
            if row.get("qty") is not None and sm and abs(row["qty"] - sm["qty"]) > 1e-6 and "V" not in row.get("sources", []):
                add("warn", "progress", i, f"本周工程量 {row['qty']:g} 与每日记录合计 {sm['qty']:g}{sm['unit']} 不一致")
            if row.get("qty") is not None and not sm and "V" not in row.get("sources", []):
                add("warn", "progress", i, "每日记录中没有该作业的数量，请核实")
        elif row.get("qty") is not None and not ({"V", "X"} & set(row.get("sources", []))):  # X：技术员确认过的 Excel 日报数量
            add("warn", "progress", i, "工程量不是来自口述，可能是照片估计，请核实数量")
        if row.get("qty") is None and pct is None:
            add("warn", "progress", i, "没有填写完成量或累计百分比")
    for i, row in enumerate(content.get("works", [])):
        if codes and row.get("activity_code") and row["activity_code"] not in codes:
            add("error", "works", i, f"作业代码 {row['activity_code']} 不在 P6 计划中")
        elif codes and not row.get("activity_code"):
            add("info", "works", i, "未挂接 P6 作业，进度不会计入预警模型")
    reported = {r.get("activity_code") for r in content.get("progress", [])} | {r.get("activity_code") for r in content.get("works", [])}
    for code, a in window.items():
        started = a.get("act_start") or (a.get("cur_pct") or 0) > 0
        es = a.get("early_start")
        due = es and date.fromisoformat(es) <= log_day
        if a.get("is_critical") and (started or due) and code not in reported:
            checks.append({"level": "info", "section": "progress", "index": None,
                           "message": f"关键作业 {code}「{a['name']}」{when}没有进度记录，是否停工？如停工请写明原因"})
    for p in ([] if week else ctx.get("photos") or []):
        if p.get("taken_at") and p["taken_at"][:10] != ctx["log_date"]:
            checks.append({"level": "warn", "section": "photos", "index": p["ref"],
                           "message": f"照片 {p['ref']} 拍摄于 {p['taken_at'][:10]}，不是日志当天"})
    return checks


def answer(conn, log: dict, user: dict, index: int, value: str) -> dict:
    """技术员回答 AI 的提问（点选或一句话）。全部回答后由前端触发重新起草。"""
    _editable(log, user)
    qa = loads(log["qa_json"], [])
    if not 0 <= index < len(qa):
        raise LogError("问题不存在")
    value = (value or "").strip()[:200]
    if not value:
        raise LogError("回答不能为空")
    qa[index]["answer"] = value
    conn.execute("UPDATE logs SET qa_json=? WHERE id=?", (dumps(qa), log["id"]))
    return get_log(conn, log["id"])


def carry_over(conn, log: dict, user: dict) -> dict:
    """“同昨日”：一键沿用昨日人员与机械配置（来源标注 Y）。"""
    _editable(log, user)
    prev = one(conn, """SELECT content_json FROM logs WHERE project_id=? AND author_id=? AND log_date<? AND status IN ('submitted','approved')
                        ORDER BY log_date DESC LIMIT 1""", (log["project_id"], log["author_id"], log["log_date"]))
    if not prev:
        raise LogError("没有可沿用的昨日日志")
    y = loads(prev["content_json"], {})
    c = loads(log["content_json"], {})
    c["labor"] = [dict(x, sources=["Y"]) for x in y.get("labor", [])]
    c["machinery"] = [dict(x, sources=["Y"]) for x in y.get("machinery", [])]
    conn.execute("UPDATE logs SET content_json=?, version=version+1 WHERE id=?", (dumps(c), log["id"]))
    return get_log(conn, log["id"])


# ----------------------------------------------------------------------------- 保存 / 提交 / 审核

def save(conn, log: dict, user: dict, content: dict, version: int, weather: str | None = None, voice_text: str | None = None) -> dict:
    _editable(log, user)
    if version != log["version"]:
        raise Conflict("日志已在其他页面更新，请刷新后再改")
    clean = _clean_content(content)
    with tx(conn):
        cur = conn.execute("""UPDATE logs SET content_json=?, weather=COALESCE(?, weather), voice_text=COALESCE(?, voice_text),
                              version=version+1 WHERE id=? AND version=?""",
                           (dumps(clean), weather, voice_text, log["id"], version))
        if cur.rowcount != 1:
            raise Conflict("日志已在其他页面更新，请刷新后再改")
    return get_log(conn, log["id"])


def _clean_content(content: dict) -> dict:
    base = log_agent.empty_content()
    if not isinstance(content, dict):
        raise LogError("内容格式错误")
    for k in base:
        if k in content:
            base[k] = content[k]
    for p in base["progress"]:
        for key in ("qty", "pct"):
            v = p.get(key)
            p[key] = None if v in (None, "") else float(v)
    return base


def _flatten(content: dict | None) -> str:
    if not content:
        return ""
    keep = {k: content.get(k) for k in ("summary", "works", "progress", "labor", "machinery", "materials", "issues", "tomorrow", "notes")}

    def strip(obj):
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items() if k != "sources"}
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj
    return json.dumps(strip(keep), ensure_ascii=False, sort_keys=True)


def edit_ratio(ai_draft: dict | None, final: dict) -> float | None:
    """1 - 相似度：0 表示 AI 草稿原样采用，1 表示全部重写。"""
    if not ai_draft:
        return None
    a, b = _flatten(ai_draft), _flatten(final)
    return round(1 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


def submit(conn, log: dict, user: dict, version: int, active_seconds: float | None = None, decisions: list | None = None) -> dict:
    """提交日志。周进度确认可带逐行 decisions（界面 ReviewTable）：给出就必须覆盖本周每个作业（否则不予提交并提示剩余数），
    “不采纳/无法判断”的作业不入 log_progress（不补零）；不给 decisions 时沿用原口径（API 兼容）。"""
    _editable(log, user)
    if version != log["version"]:
        raise Conflict("日志已在其他页面更新，请刷新后再提交")
    content = loads(log["content_json"], {})
    review_rows = None
    if decisions is not None:
        if log["mode"] != "ai" or log.get("period") != "week":
            raise LogError("只有周进度确认需要逐项决定")
        wrows = week_review(conn, log, user)["rows"]
        try:
            chosen = review_svc.check_decisions([r["key"] for r in wrows], decisions, "adopt", {r["key"]: r["label"] for r in wrows}, numeric=True)
        except review_svc.ReviewError as exc:
            raise LogError(str(exc)) from None
        content, review_rows = _apply_week_decisions(content, wrows, chosen)
    if not (content.get("works") or content.get("summary")):
        raise LogError("施工内容为空，不能提交")
    imp = p6.current_import(conn, log["project_id"])
    codes = {a["code"] for a in p6.current_activities(conn, log["project_id"])} if imp else set()
    bad = [p["activity_code"] for p in content.get("progress", []) if codes and p.get("activity_code") not in codes]
    if bad:
        raise LogError(f"以下作业代码不在 P6 计划中：{', '.join(bad)}")
    for p in content.get("progress", []):
        if p.get("pct") is not None and not 0 <= p["pct"] <= 100:
            raise LogError("累计完成百分比应在 0~100 之间")
    now = now_iso()
    wall = (datetime.fromisoformat(now) - datetime.fromisoformat(log["started_at"])).total_seconds()
    seconds = float(active_seconds) if active_seconds and 0 < float(active_seconds) <= wall + 5 else wall
    ratio = edit_ratio(loads(log["ai_draft_json"]), content) if log["mode"] == "ai" else None
    with tx(conn):
        cur = conn.execute("""UPDATE logs SET status='submitted', submitted_at=?, edit_seconds=?, edit_ratio=?, version=version+1
                              WHERE id=? AND version=? AND status IN ('draft','returned')""",
                           (now, round(seconds, 1), ratio, log["id"], version))
        if cur.rowcount != 1:
            raise Conflict("日志状态已变化，请刷新")
        void_progress(conn, log["id"], "重新提交，旧数量作废")  # 理论上草稿/退回态无有效行；防重复计数
        if review_rows is not None:
            conn.execute("UPDATE logs SET content_json=? WHERE id=?", (dumps(content), log["id"]))
            review_svc.save_rows(conn, log["project_id"], "week_progress", "log", log["id"], review_rows, user["id"])
        # 正式进度只来自“周确认”（避免日/周重复计量）；未启用周确认的项目由日志直接计入
        counts = log["mode"] == "ai" and (log.get("period") == "week" or not weekly_mode(conn, log["project_id"]))
        if counts:
            pdate = min(log.get("period_end") or log["log_date"], now[:10]) if log.get("period") == "week" else log["log_date"]
            conn.executemany(
                "INSERT INTO log_progress (log_id, project_id, log_date, activity_code, qty, unit, pct, note) VALUES (?,?,?,?,?,?,?,?)",
                [(log["id"], log["project_id"], pdate, p["activity_code"], p.get("qty"), p.get("unit") or "",
                  p.get("pct"), p.get("note") or "") for p in content.get("progress", []) if p.get("activity_code")])
        if log.get("period") == "week":
            # 周确认即对本周每日日志的确认（技术员不必逐日提交）
            conn.execute("""UPDATE logs SET status='submitted', submitted_at=?, version=version+1 WHERE project_id=? AND author_id=?
                            AND period='day' AND mode='ai' AND log_date BETWEEN ? AND ? AND status IN ('draft','returned')""",
                         (now, log["project_id"], log["author_id"], log["log_date"], log["period_end"]))
        event(conn, "log.submit", project_id=log["project_id"], user_id=user["id"], ref_id=log["id"], value=round(seconds, 1),
              data={"mode": log["mode"], "edit_ratio": ratio, "wall_seconds": round(wall, 1),
                    **({"decisions": {k: sum(1 for r in review_rows if r["decision"] == k) for k in review_svc.ADOPT}} if review_rows is not None else {})})
        audit(conn, user["id"], "log.submit", f"log:{log['id']}")
    return get_log(conn, log["id"])


def review(conn, log: dict, user: dict, action: str, note: str = "", decisions: list | None = None) -> dict:
    if not R.can(user["role"], "log_review"):
        raise LogError("日志由工程主管审核，当前岗位没有审核权限")
    if log["author_id"] == user["id"]:
        raise LogError("不能审核自己编写的日志")
    if log["status"] != "submitted":
        raise LogError("只有已提交的日志可以审核")
    if action not in ("approve", "return"):
        raise LogError("未知操作")
    if action == "return" and not note.strip():
        raise LogError("退回需填写原因")
    dq_rows = None
    if decisions is not None:  # 日志预审：数据审核Agent 的每条检查结论逐条核对（属实/误报，误报必填原因），全部决定后才能通过或退回
        rows = dq_review_rows(conn, log)
        try:
            chosen = review_svc.check_decisions([r["key"] for r in rows], decisions, "verify", {r["key"]: r["ai_value"] for r in rows})
        except review_svc.ReviewError as exc:
            raise LogError(str(exc)) from None
        dq_rows = [{"key": r["key"], "label": r["label"], "ai": r["ai_value"], "final": r["ai_value"] if chosen[r["key"]]["decision"] == "true" else None,
                    "decision": chosen[r["key"]]["decision"], "reason": chosen[r["key"]]["reason"]} for r in rows]
    with tx(conn):
        cur = conn.execute("""UPDATE logs SET status=?, reviewed_by=?, reviewed_at=?, review_note=?, version=version+1
                              WHERE id=? AND status='submitted'""",
                           ("approved" if action == "approve" else "returned", user["id"], now_iso(), note.strip(), log["id"]))
        if cur.rowcount != 1:
            raise Conflict("日志状态已变化，请刷新")
        if dq_rows is not None:
            review_svc.save_rows(conn, log["project_id"], "dq_check", "log", log["id"], dq_rows, user["id"])
        if action == "return":
            void_progress(conn, log["id"], f"项目部退回：{note.strip()}")  # 留痕不删除；统计排除
        if log.get("period") == "week":
            conn.execute("""UPDATE logs SET status=?, reviewed_by=?, reviewed_at=?, version=version+1 WHERE project_id=? AND author_id=?
                            AND period='day' AND mode='ai' AND log_date BETWEEN ? AND ? AND status='submitted'""",
                         ("approved" if action == "approve" else "draft", user["id"], now_iso(), log["project_id"], log["author_id"],
                          log["log_date"], log["period_end"]))
        event(conn, f"log.{action}", project_id=log["project_id"], user_id=user["id"], ref_id=log["id"])
        audit(conn, user["id"], f"log.{action}", f"log:{log['id']}", note[:200])
    return get_log(conn, log["id"])


def recheck(conn, log: dict, user: dict) -> list[dict]:
    """技术员修改后重新跑规则校验（不调用模型）。"""
    author = user if user["id"] == log["author_id"] else one(conn, "SELECT * FROM users WHERE id=?", (log["author_id"],))
    ctx = _week_context(conn, log, author) if log.get("period") == "week" else _context(conn, log, author)
    checks = run_checks(loads(log["content_json"], {}), ctx)
    conn.execute("UPDATE logs SET checks_json=? WHERE id=?", (dumps(checks), log["id"]))
    return checks


def weekly_mode(conn, project_id: int) -> bool:
    """项目是否采用“随手记 + 按周确认”（默认是）。"""
    p = one(conn, "SELECT config_json FROM projects WHERE id=?", (project_id,))
    return (loads(p["config_json"], {}) if p else {}).get("cadence", "week") == "week"


def daily_log_required(conn, project_id: int) -> bool:
    p = one(conn, "SELECT config_json FROM projects WHERE id=?", (project_id,))
    return (loads(p["config_json"], {}) if p else {}).get("daily_log", True)


# ----------------------------------------------------------------------------- 随手记

def add_note(conn, user: dict, text: str, media_ids: list[int], seconds: float | None, source: str, note_date: str | None = None,
             geo: dict | None = None) -> dict:
    text = (text or "").strip()[:2000]
    media_ids = [int(m) for m in media_ids or []][:9]
    if not text and not media_ids:
        raise LogError("请说一句或拍一张")
    photo_geo = None
    for mid in media_ids:
        m = one(conn, "SELECT uploader_id, geo_lat, geo_lon, geo_acc, geo_place FROM media WHERE id=? AND project_id=?", (mid, user["project_id"]))
        if not m or m["uploader_id"] != user["id"]:
            raise LogError("照片不存在或不属于你")
        if photo_geo is None and m["geo_lat"] is not None:
            photo_geo = {"lat": m["geo_lat"], "lon": m["geo_lon"], "acc": m["geo_acc"], "place": m["geo_place"]}
    # 随手记自带定位优先；只有照片带定位时沿用照片的
    if not geo or geo["lat"] is None:
        geo = photo_geo or geo
    d = note_date or _today()
    date.fromisoformat(d)
    nid = insert(conn, "notes", {"project_id": user["project_id"], "author_id": user["id"], "note_date": d, "text": text,
                                 "media_json": dumps(media_ids), "source": source if source in ("voice", "text", "photo") else "text",
                                 "seconds": round(float(seconds), 1) if seconds and 0 < float(seconds) < 1800 else None, "created_at": now_iso(),
                                 "geo_lat": geo["lat"] if geo else None, "geo_lon": geo["lon"] if geo else None,
                                 "geo_acc": geo["acc"] if geo else None, "geo_place": geo["place"] if geo else ""})
    event(conn, "note.add", project_id=user["project_id"], user_id=user["id"], ref_id=nid, value=seconds, data={"source": source})
    return one(conn, "SELECT * FROM notes WHERE id=?", (nid,))


def notes_of(conn, project_id: int, author_id: int, start: str, end: str | None = None) -> list[dict]:
    return all_(conn, "SELECT * FROM notes WHERE project_id=? AND author_id=? AND note_date BETWEEN ? AND ? ORDER BY created_at",
                (project_id, author_id, start, end or start))


def delete_note(conn, user: dict, note_id: int) -> dict:
    n = one(conn, "SELECT * FROM notes WHERE id=? AND author_id=?", (note_id, user["id"]))
    if not n:
        raise LogError("记录不存在")
    day = one(conn, "SELECT status FROM logs WHERE project_id=? AND author_id=? AND log_date=? AND period='day' AND mode='ai'",
              (n["project_id"], n["author_id"], n["note_date"]))
    if day and day["status"] in ("submitted", "approved"):
        raise LogError("当日日志已确认，不能删除")
    conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
    return _reset_day_after_delete(conn, n)


def _note_line(n: dict) -> str:
    """随手记在整理依据（voice_text）里的那一行，与 organize_day 的拼法一致。"""
    return f"[{n['created_at'][11:16]}] {n['text']}"


def _has_note(voice_text: str, n: dict) -> bool:
    """整行匹配：该条随手记是否是这份日志的整理依据之一（不用子串，避免“桩基”误中“3#墩桩基施工”）。"""
    if not n["text"]:
        return False
    lines = (voice_text or "").split("\n")
    entry = _note_line(n).split("\n")
    return any(lines[i:i + len(entry)] == entry for i in range(len(lines) - len(entry) + 1))


def pending_notes(conn, lg: dict) -> int:
    """当日自动草稿被人工改过后，还没并入草稿（不在整理依据里）的随手记条数；其余情况为 0。"""
    if lg["period"] != "day" or not lg["auto"] or lg["status"] not in ("draft", "returned") or not (_human_edited(lg) or _imported(conn, lg)):
        return 0
    return sum(1 for n in notes_of(conn, lg["project_id"], lg["author_id"], lg["log_date"]) if n["text"] and not _has_note(lg["voice_text"], n))


def _human_edited(lg: dict) -> bool:
    """草稿有人工修改痕迹：当前内容与 AI 起草的原稿不同（AI 起草时两者写入同一份内容）。"""
    if not lg["ai_draft_json"]:
        return False
    return loads(lg["content_json"], {}) != loads(lg["ai_draft_json"], {})


def _imported(conn, lg: dict) -> bool:
    """当天日志的内容已由 Excel 日报导入（技术员逐行确认过）：后台整理不覆盖，与人工修改同样对待。"""
    return bool(one(conn, "SELECT 1 FROM xlsx_imports WHERE log_id=? AND confirmed_at IS NOT NULL LIMIT 1", (lg["id"],)))


def _reset_day_after_delete(conn, n: dict) -> dict:
    """删掉随手记后，当日由随手记整理的日志草稿不能还留着已删内容（否则会写出“未进行实际作业”并带进周确认）：
    先清空草稿并解除该条随手记的照片；当天还有其他随手记时返回 reorganize=True，由调用方（后台）按剩余记录重新整理。
    技术员已在草稿上做过修改时不清空、不重整（否则抹掉人工修改），只去掉依据里的这条随手记并返回提示。"""
    out = {"day": n["note_date"], "reorganize": False}
    lg = one(conn, "SELECT * FROM logs WHERE project_id=? AND author_id=? AND log_date=? AND period='day' AND mode='ai' AND status IN ('draft','returned')",
             (n["project_id"], n["author_id"], n["note_date"]))
    if not lg or not (lg["auto"] or _has_note(lg["voice_text"], n)):
        return out  # 不是由随手记整理的日志（技术员自己口述起草的），不动
    mids = [int(m) for m in loads(n["media_json"], [])]
    with tx(conn):
        if mids:
            conn.execute(f"UPDATE media SET log_id=NULL WHERE log_id=? AND id IN ({','.join('?' for _ in mids)})", (lg["id"], *mids))
        if _human_edited(lg) or _imported(conn, lg):
            lines, entry = (lg["voice_text"] or "").split("\n"), _note_line(n).split("\n")
            for i in range(len(lines) - len(entry) + 1):
                if lines[i:i + len(entry)] == entry:
                    del lines[i:i + len(entry)]
                    break
            conn.execute("UPDATE logs SET voice_text=? WHERE id=?", ("\n".join(lines), lg["id"]))
            out["hint"] = "当日日志草稿有你的修改，未自动清空；请在日志里核对并删去这条随手记对应的内容"
            return out
        conn.execute("""UPDATE logs SET content_json=?, ai_draft_json=NULL, checks_json='[]', qa_json='[]', voice_text='', drafted_at=NULL,
                        ai_run_id=NULL, version=version+1 WHERE id=?""", (dumps(log_agent.empty_content()), lg["id"]))
    out["reorganize"] = bool(notes_of(conn, n["project_id"], n["author_id"], n["note_date"]))
    return out


def organize_day(conn, author: dict, day: str, force: bool = False) -> dict | None:
    """现场记录Agent：把当天随手记整理成每日施工日志（技术员不必单独写日志）。"""
    notes = notes_of(conn, author["project_id"], author["id"], day)
    if not notes:
        return None
    lg = ensure_log(conn, author, day, "ai", "day", auto=True)
    if lg["status"] not in ("draft", "returned"):
        return lg
    # 已整理过且每条随手记都在整理依据里就不重复起草（逐条核对，不靠秒级时间戳：同一秒新记的随手记也不会漏）；
    # 纯照片随手记没有文字，仍按时间判断
    photo_last = max((n["created_at"] for n in notes if not n["text"]), default="")
    if not force and lg["drafted_at"] and all(_has_note(lg["voice_text"], n) for n in notes if n["text"]) and lg["drafted_at"] >= photo_last:
        return lg
    if not force and (_human_edited(lg) or _imported(conn, lg)):
        # 技术员已在草稿上改过（或已从 Excel 日报导入）：后台不覆盖人工修改，新随手记先作“未并入”（日志页提示，点“重新整理”= force 才覆盖）
        return lg
    text = "\n".join(f"[{n['created_at'][11:16]}] {n['text']}" for n in notes if n["text"])
    for n in notes:
        for mid in loads(n["media_json"], []):
            conn.execute("UPDATE media SET log_id=? WHERE id=? AND (log_id IS NULL OR log_id=?)", (lg["id"], mid, lg["id"]))
    conn.execute("UPDATE logs SET voice_text=? WHERE id=?", (text, lg["id"]))
    lg = get_log(conn, lg["id"])
    if not text and not photos_of(conn, lg["id"]):
        return lg
    return draft(conn, lg, author)


# ----------------------------------------------------------------------------- 周确认

def _week_context(conn, log: dict, author: dict) -> dict:
    ctx = _context(conn, log, author)
    ws, we = log["log_date"], log["period_end"]
    acts = list(ctx["all_codes"].values())
    window = p6.range_activities(acts, ws, we, author.get("wbs_scope", ""))
    imp = p6.current_import(conn, log["project_id"])
    state = progress_state(conn, log["project_id"], imp, before=ws)
    for a in window:
        st = state.get(a["code"])
        a["cur_pct"] = st["pct"] if st and st["pct"] is not None else a.get("phys_pct")
    days = all_(conn, """SELECT * FROM logs WHERE project_id=? AND author_id=? AND period='day' AND mode='ai' AND log_date BETWEEN ? AND ?
                         ORDER BY log_date""", (log["project_id"], log["author_id"], ws, we))
    dlist, sums, labor, mach = [], {}, {}, {}
    for i, d in enumerate(days):
        c = loads(d["content_json"], {})
        ref = f"D{i + 1}"
        dlist.append({"ref": ref, "id": d["id"], "date": d["log_date"], "status": d["status"], "summary": summarize(c, 600), "content": c})
        for pr in c.get("progress", []):
            code = pr.get("activity_code")
            if not code:
                continue
            sm = sums.setdefault(code, {"qty": 0.0, "unit": pr.get("unit") or "", "last_pct": None, "days": 0, "refs": []})
            if pr.get("qty") is not None:
                sm["qty"] += pr["qty"]
            if pr.get("pct") is not None:
                sm["last_pct"] = pr["pct"]
            sm["days"] += 1
            sm["refs"].append(ref)
        for x in c.get("labor", []):
            if x.get("count") is not None:
                labor.setdefault(x["trade"], []).append(x["count"])
        for x in c.get("machinery", []):
            if x.get("count") is not None:
                mach.setdefault(x["name"], []).append(x["count"])
    for v in sums.values():
        v["qty"] = round(v["qty"], 3)
    notes = [{"date": n["note_date"], "time": n["created_at"][11:16], "text": n["text"]} for n in notes_of(conn, log["project_id"], log["author_id"], ws, we) if n["text"]]
    ctx.update({"week_start": ws, "week_end": we, "activities": window, "days": dlist, "sums": sums, "notes": notes,
                "labor_avg": {k: round(sum(v) / len(v), 1) for k, v in labor.items()},
                "mach_avg": {k: round(sum(v) / len(v), 1) for k, v in mach.items()}, "period": "week", "photos": []})
    return ctx


def draft_week(conn, log: dict, user: dict, voice_text: str | None = None) -> dict:
    if voice_text is not None:
        conn.execute("UPDATE logs SET voice_text=? WHERE id=?", (voice_text, log["id"]))
        log = get_log(conn, log["id"])
    ctx = _week_context(conn, log, user)
    if not ctx["days"] and not (ctx["voice_text"] or "").strip():
        raise LogError("本周还没有随手记或每日日志；请先随手记，或在补充说明里口述本周情况")
    refs = [d["ref"] for d in ctx["days"]] + ["N"]
    result = call_json(
        "log.week", system=log_agent.WEEK_SYSTEM, user=log_agent.build_week_prompt(ctx), prompt_version=log_agent.WEEK_PROMPT_VERSION,
        mock=lambda: log_agent.mock_week(ctx), validate=lambda d: log_agent.normalize(d, refs), project_id=log["project_id"],
        user_id=user["id"], max_tokens=4000, input_meta={"log_id": log["id"], "days": len(ctx["days"]), "notes": len(ctx["notes"])})
    content = log_agent.scrub_inferred(result.data, " ".join([ctx["voice_text"] or ""] + [n["text"] for n in ctx["notes"]] + [d["summary"] for d in ctx["days"]]))
    checks = run_checks(content, ctx)
    qa = [q for q in ctx["qa"] if q.get("answer")]
    asked = {q["question"] for q in qa}
    qa += [{"field": u["field"], "question": u["question"], "options": u.get("options", []), "answer": None}
           for u in content.get("uncertain", []) if u["question"] not in asked]
    with tx(conn):
        conn.execute("""UPDATE logs SET content_json=?, ai_draft_json=?, checks_json=?, ai_run_id=?, drafted_at=?, qa_json=?, version=version+1
                        WHERE id=?""", (dumps(content), dumps(content), dumps(checks), result.run_id, now_iso(), dumps(qa), log["id"]))
        event(conn, "log.draft", project_id=log["project_id"], user_id=user["id"], ref_id=log["id"], value=result.latency_ms,
              data={"provider": result.provider, "period": "week", "days": len(ctx["days"])})
    return get_log(conn, log["id"])


def week_days(conn, log: dict) -> list[dict]:
    rows = all_(conn, """SELECT id, log_date, status, auto, content_json, (SELECT COUNT(*) FROM media m WHERE m.log_id=logs.id) photos FROM logs
                         WHERE project_id=? AND author_id=? AND period='day' AND mode='ai' AND log_date BETWEEN ? AND ? ORDER BY log_date""",
                (log["project_id"], log["author_id"], log["log_date"], log["period_end"]))
    notes = all_(conn, """SELECT note_date, COUNT(*) n FROM notes WHERE project_id=? AND author_id=? AND note_date BETWEEN ? AND ? GROUP BY note_date""",
                 (log["project_id"], log["author_id"], log["log_date"], log["period_end"]))
    nmap = {n["note_date"]: n["n"] for n in notes}
    by_day = {r["log_date"]: r for r in rows}
    out = []
    d = date.fromisoformat(log["log_date"])
    for i in range(7):
        k = (d + timedelta(days=i)).isoformat()
        r = by_day.get(k)
        out.append({"date": k, "log_id": r["id"] if r else None, "status": r["status"] if r else None, "notes": nmap.get(k, 0),
                    "photos": r["photos"] if r else 0, "summary": loads(r["content_json"], {}).get("summary", "") if r else ""})
    return out


# ----------------------------------------------------------------------------- “AI建议｜依据｜人工决定”核对行

_WEEKDAY = "一二三四五六日"
_SEC_NAME = {"works": "施工内容", "progress": "工程量", "labor": "人员", "machinery": "机械", "materials": "材料", "issues": "问题", "photos": "照片"}
_LV_NAME = {"error": "错误", "warn": "待核实", "info": "提示"}


def week_review(conn, log: dict, author: dict) -> dict:
    """周进度确认的核对行：每个 P6 作业一行 = AI 汇总的本周工程量｜依据（每日日志哪几天哪条）｜人工决定。
    AI 原值取 AI 草稿（ai_draft_json）；每日记录合计单列为“依据”，两者不一致时给出提示（保留，不自动改）。没有数量就是 None，不补零。"""
    ai_draft = loads(log["ai_draft_json"], None)
    if log.get("period") != "week" or not ai_draft:
        return {"rows": [], "decided": False}
    ctx = _week_context(conn, log, author)
    content = loads(log["content_json"], {})
    ai_rows: dict[str, dict] = {}
    for r in ai_draft.get("progress", []):
        if r.get("activity_code") and r["activity_code"] not in ai_rows:
            ai_rows[r["activity_code"]] = r
    cur_rows: dict[str, dict] = {}
    for r in content.get("progress", []):
        if r.get("activity_code") and r["activity_code"] not in cur_rows:
            cur_rows[r["activity_code"]] = r
    daily: dict[str, list[dict]] = {}
    for d in ctx["days"]:
        wd = _WEEKDAY[date.fromisoformat(d["date"]).weekday()]
        for pr in d["content"].get("progress", []):
            code = pr.get("activity_code")
            if not code:
                continue
            bits = []
            if pr.get("qty") is not None:
                bits.append(f"完成 {pr['qty']:g}{pr.get('unit') or ''}")
            if pr.get("pct") is not None:
                bits.append(f"累计 {pr['pct']:g}%")
            daily.setdefault(code, []).append({"type": "log", "ref": d["ref"], "log_id": d["id"], "date": d["date"], "qty": pr.get("qty"), "unit": pr.get("unit") or "",
                                               "text": f"周{wd} {d['date'][5:]}（{d['ref']}）" + ("，".join(bits) or "有记录，无数量")})
    codes = list(dict.fromkeys([*ai_rows, *cur_rows, *daily]))
    saved = review_svc.saved_map(conn, "log", log["id"], "week_progress")
    acts = ctx["all_codes"]
    rows = []
    for code in codes:
        ai = ai_rows.get(code)
        ai_qty = ai.get("qty") if ai else None
        ev = [{k: v for k, v in e.items() if k not in ("qty", "unit")} for e in daily.get(code, [])]
        qtys = [e["qty"] for e in daily.get(code, []) if e["qty"] is not None]
        dsum = round(sum(qtys), 3) if qtys else None
        unit = (ai or {}).get("unit") or next((e["unit"] for e in daily.get(code, []) if e["unit"]), "") or (cur_rows.get(code) or {}).get("unit") or ""
        flag = ""
        verbal = bool(ai and "V" in (ai.get("sources") or []))
        if ai is None and dsum is not None:
            flag = f"AI 草稿没有汇总该作业；每日记录合计 {dsum:g}{unit}"
        elif ai_qty is not None and dsum is not None and abs(ai_qty - dsum) > 1e-6 and not verbal:
            flag = f"AI 汇总 {ai_qty:g}{unit} 与每日记录合计 {dsum:g}{unit} 不一致，请核对"
        elif ai_qty is not None and dsum is None and not verbal:
            flag = "每日记录里没有该作业的数量（来自口述或补充），请核实"
        if not ev:
            ev = [{"type": "text", "ref": "", "text": "每日日志里没有该作业的记录"}]
        name = (acts.get(code) or {}).get("name", "")
        sv = saved.get(code)
        rows.append({"key": code, "code": code, "name": name, "label": f"{code} {name}".strip(), "unit": unit, "ai_value": ai_qty,
                     "pct": (ai or cur_rows.get(code) or {}).get("pct"), "daily_sum": dsum, "cur_value": (cur_rows.get(code) or {}).get("qty"),
                     "evidence": ev, "flag": flag,
                     "saved": {"decision": sv["decision"], "final": sv["final_value"], "reason": sv["reason"]} if sv else None})
    return {"rows": rows, "decided": bool(saved)}


def _apply_week_decisions(content: dict, rows: list[dict], chosen: dict[str, dict]) -> tuple[dict, list[dict]]:
    """把逐行决定落到周确认内容：确认=采用 AI 数量，修改=人工数量，不采纳/无法判断=该作业不提交数量（行直接去掉，不补零）。
    返回（新内容，逐字段记录：AI 原值与最终值）。"""
    progress = content.get("progress", [])
    first: dict[str, dict] = {}
    for p in progress:
        if p.get("activity_code") and p["activity_code"] not in first:
            first[p["activity_code"]] = p
    new, recs = [], []
    for r in rows:
        d = chosen[r["key"]]
        dec = d["decision"]
        base = first.get(r["code"])
        if dec == "confirm":
            qty = r["ai_value"] if r["ai_value"] is not None else (base.get("qty") if base else None)
        elif dec == "modify":
            qty = d["final"]
        else:
            qty = None
        recs.append({"key": r["key"], "label": r["label"], "ai": r["ai_value"], "final": qty, "decision": dec, "reason": d["reason"]})
        if dec in ("reject", "unknown"):
            continue
        row = dict(base) if base else {"activity_code": r["code"], "unit": r["unit"], "pct": r["pct"], "note": "",
                                       "sources": [e["ref"] for e in r["evidence"] if e.get("ref")]}
        row["qty"] = qty
        if not row.get("unit") and r["unit"]:
            row["unit"] = r["unit"]
        if row["qty"] is None and row.get("pct") is None:
            continue
        new.append(row)
    out = dict(content)
    out["progress"] = new + [p for p in progress if not p.get("activity_code")]
    return out, recs


def dq_review_rows(conn, log: dict) -> list[dict]:
    """日志预审的核对行：数据审核Agent 的每条检查结论一行，人工决定 属实/误报（误报必填原因）。"""
    dq = loads(log.get("dq_json"), None) or {}
    saved = review_svc.saved_map(conn, "log", log["id"], "dq_check")
    rows = []
    for i, c in enumerate(dq.get("checks") or []):
        msg = str(c.get("message") or "")
        src = c.get("source") or ""
        lv = _LV_NAME.get(c.get("level"), "提示")
        where = _SEC_NAME.get(c.get("section") or "", "")
        nth = f" 第{c['index'] + 1}条" if isinstance(c.get("index"), int) else ""
        ev = [{"type": "text", "ref": src, "text": f"{src or '检查'}：{lv}" + (f"（{where}{nth}）" if where else "")}]
        for lid in dict.fromkeys(re.findall(r"L(\d+)", msg)):
            ev.append({"type": "log", "ref": f"L{lid}", "log_id": int(lid), "text": f"相关日志 L{lid}"})
        key = f"c{i}"
        sv = saved.get(key)
        rows.append({"key": key, "label": f"{lv}·{src}" if src else lv, "ai_value": msg, "level": c.get("level"), "evidence": ev,
                     "flag": "error" if c.get("level") == "error" else "",
                     "saved": {"decision": sv["decision"], "final": sv["final_value"], "reason": sv["reason"]} if sv else None})
    return rows


__all__ = ["AIError", "LogError", "Conflict"]
