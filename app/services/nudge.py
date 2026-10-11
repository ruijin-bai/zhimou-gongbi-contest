"""催办Agent（升级链）+ 订阅式提问。

升级链：阶段1 提醒责任人本人 → 阶段2（再过 N 小时仍未处理）提醒其所属分包单位安全员 → 阶段3 提醒港湾对应主管
（安全隐患→安全主管；质量/日志/行动项→工程主管）→ 阶段4 升级到项目经理（只生成通知；其中高风险安全/质量问题同时进入项目经理首页待办，见 todo._escalation）。
日志/周确认缺报、关键作业数据缺口（连续多日无进度上报）的升级链止于阶段3：项目经理只关注进度、质量、安全，不处理日志填报。
催办链调整（D9）：进度类催办（日志/周确认缺报、数据缺口补报）不发给分包安全员，本人之后直接到工程主管；中低风险安全隐患最高只升级到安全主管。

原则：
- 只催“人该做的事”：超期/临期未整改的隐患与质控、缺报的日志/周确认、分配给人的行动项；已处理的事项下一轮自动停止。
- 幂等：同一对象同一阶段对同一接收人只发一次（nudge_log 唯一索引，发送前在同一事务内原子认领，多个进程同时跑也不重复）；
  同一对象对同一接收人每天最多提醒一次（不分阶段）；静默时段、每人每日上限与“今天已提醒过”只延后，不丢。
- 催办Agent 只负责提醒开头一句得体的话；事实清单（哪件事、超期几天、期限）由系统按记录拼接，AI 编不了事实。没有 AI 用模板。
- 绝不对 leader / admin 发催办。
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta

from .. import roles as R
from ..ai.client import AIError, call_json
from ..db import all_, audit, connect, dumps, event, insert, loads, now_iso, one, tx
from . import logs as log_svc
from . import progress as progress_svc
from . import qa as qa_svc
from .site_agents import missing_logs

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS nudge_log (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    obj_type TEXT NOT NULL,
    obj_key TEXT NOT NULL,
    stage INTEGER NOT NULL,
    to_user_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'sent',
    subject TEXT NOT NULL DEFAULT '',
    status_text TEXT NOT NULL DEFAULT '',
    owner_id INTEGER,
    area TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    via TEXT NOT NULL DEFAULT '',
    ai_run_id INTEGER,
    notif_id INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_nudge_log ON nudge_log(project_id, obj_type, obj_key, stage, to_user_id);
CREATE INDEX IF NOT EXISTS ix_nudge_day ON nudge_log(project_id, sent_at);
CREATE TABLE IF NOT EXISTS ask_pins (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    question TEXT NOT NULL,
    schedule TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    last_answer_json TEXT,
    last_error TEXT,
    last_error_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ask_pins_user ON ask_pins(project_id, user_id);
"""

STAGE_NAME = {1: "提醒本人", 2: "提醒单位安全员", 3: "升级到主管", 4: "升级到项目经理"}
AREA_NAME = {"safety": "安全隐患", "quality": "质量质控", "log": "日志/周确认", "gap": "数据缺口（补报）", "action": "行动项", "letter_progress": "进度联系单"}
LOG_LIKE_AREAS = ("log", "gap")   # 日志类缺报：升级链止于工程主管，不进项目经理
PROGRESS_AREAS = LOG_LIKE_AREAS + ("letter_progress",)   # 进度类催办（日志/周确认、数据缺口补报、进度联系单）：不发给分包安全员（D9）
PM_TODO_AGENTS = ("safety", "quality")   # 阶段4 中会进入项目经理首页待办的：高风险安全/质量问题（与 todo._escalation 同口径）
NO_NUDGE_ROLES = frozenset({"admin", "leader"})
# 催办台与规则（规则、立即运行、升级记录、今日动态）：工程主管、安全主管。项目经理只拍板、不进催办台（D7、AC-6.7、D-7），
# 固定的提问（pins）另走本人校验，项目经理照常可用
MGMT_ROLES = ("eng_sup", "safety_sup")
PIN_ROLES = ("manager", "eng_sup", "safety_sup")    # 固定到首页/订阅：港湾管理岗（分包单位不开放）
MAX_AI_PER_RUN = 12
MAX_PINS_PER_USER = 12
SUBSCRIBE_HOUR = 8
SCHEDULES = {"daily": "每天", "mon": "每周一", "fri": "每周五"}

DEFAULT_RULES: dict = {
    "enabled": True,
    "lead_days": 1,                  # 临期：期限前 N 天起提醒责任人（0=到期当天起）
    "gap_hours": [24, 24, 24],       # 阶段1→2、2→3、3→4 之间的等待小时数
    "quiet_start": "21:00",          # 静默时段内不发送（延后到静默结束后的下一轮）；起止相同=不设静默
    "quiet_end": "07:00",
    "daily_cap": 3,                  # 每人每天最多收到几条催办通知（同一轮内同阶段的事项合并为一条）
    "sources": {"findings": True, "logs": True, "actions": True},
}


# ----------------------------------------------------------------------------- 规则

def _hhmm(v, name: str) -> str:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(v or "").strip())
    if not m:
        raise ValueError(f"{name}格式应为 HH:MM")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def normalize_rules(data: dict, base: dict | None = None) -> dict:
    """校验并补全规则；非法值抛 ValueError（接口返回 400）。"""
    out = {**DEFAULT_RULES, "gap_hours": list(DEFAULT_RULES["gap_hours"]), "sources": dict(DEFAULT_RULES["sources"])}
    for src in (base or {}, data or {}):
        if "enabled" in src:
            out["enabled"] = bool(src["enabled"])
        if "lead_days" in src:
            try:
                v = int(src["lead_days"])
            except (TypeError, ValueError):
                raise ValueError("临期天数应为整数") from None
            if not 0 <= v <= 7:
                raise ValueError("临期天数应在 0~7 之间")
            out["lead_days"] = v
        if "gap_hours" in src:
            g = src["gap_hours"]
            if not isinstance(g, (list, tuple)) or len(g) != 3:
                raise ValueError("升级间隔需要给出 3 个小时数")
            try:
                g = [float(x) for x in g]
            except (TypeError, ValueError):
                raise ValueError("升级间隔应为数字") from None
            if any(not 1 <= x <= 720 for x in g):
                raise ValueError("升级间隔应在 1~720 小时之间")
            out["gap_hours"] = [int(x) if x == int(x) else x for x in g]
        if "quiet_start" in src:
            out["quiet_start"] = _hhmm(src["quiet_start"], "静默开始时间")
        if "quiet_end" in src:
            out["quiet_end"] = _hhmm(src["quiet_end"], "静默结束时间")
        if "daily_cap" in src:
            try:
                v = int(src["daily_cap"])
            except (TypeError, ValueError):
                raise ValueError("每日上限应为整数") from None
            if not 1 <= v <= 20:
                raise ValueError("每人每天提醒上限应在 1~20 之间")
            out["daily_cap"] = v
        if isinstance(src.get("sources"), dict):
            for k in out["sources"]:
                if k in src["sources"]:
                    out["sources"][k] = bool(src["sources"][k])
    return out


def load_rules(conn, project_id: int) -> dict:
    """默认值 < 项目 config_json 的 nudge 段 < 催办台保存的规则（knowledge: nudge_rules）。非法的历史值回退默认。"""
    p = one(conn, "SELECT config_json FROM projects WHERE id=?", (project_id,))
    cfg = (loads(p["config_json"], {}) if p else {}) or {}
    saved = one(conn, "SELECT value_json FROM knowledge WHERE project_id=? AND kind='nudge_rules' AND key='rules'", (project_id,))
    layers = [cfg.get("nudge") if isinstance(cfg.get("nudge"), dict) else {}, loads(saved["value_json"], {}) if saved else {}]
    rules = normalize_rules({})
    for layer in layers:
        try:
            rules = normalize_rules(layer, rules)
        except ValueError:
            log.warning("催办规则含非法值，已忽略该层")
    return rules


# ----------------------------------------------------------------------------- 项目经理口径：日志/周确认缺报（含数据缺口补报）不出现在项目经理的催办台

def hides_log_items(role: str) -> bool:
    return role in R.PM_SCOPE


def scope_rules(rules: dict, role: str) -> dict:
    """给项目经理看的规则：不含“日志/周确认”催办范围（由工程主管设置）。"""
    if not hides_log_items(role):
        return rules
    return {**rules, "sources": {k: v for k, v in rules["sources"].items() if k != "logs"}}


def scope_report(rep: dict, role: str) -> dict:
    """给项目经理看的运行结果：剔除日志/周确认、数据缺口类事项（谁没记录不给项目经理）。"""
    if not hides_log_items(role):
        return rep
    keep = lambda it: it.get("area") not in LOG_LIKE_AREAS  # noqa: E731
    out = dict(rep)
    for k in ("sent", "deferred"):
        out[k] = [{**e, "items": [i for i in e["items"] if keep(i)]} for e in rep.get(k, [])]
        out[k] = [e for e in out[k] if e["items"]]
    out["skipped"] = [x for x in rep.get("skipped", []) if keep(x)]
    # 计数与摘要按剔除后的事项重算（否则项目经理看到的“检查到 N 件/暂不发送”仍含日志缺报）
    if "areas" in rep:
        out["areas"] = {a: n for a, n in rep["areas"].items() if a not in LOG_LIKE_AREAS}
        out["targets"] = sum(out["areas"].values())
    if "reasons_by_area" in rep:
        out["reasons_by_area"] = {a: c for a, c in rep["reasons_by_area"].items() if a not in LOG_LIKE_AREAS}
        out["reasons"] = dict(sum((Counter(c) for c in out["reasons_by_area"].values()), Counter()))
    if "summary" in rep and "areas" in rep:
        out["summary"] = _summary(rep.get("dry", False), out["targets"], out["sent"], out.get("reasons", {}))
    return out


def save_rules(conn, project_id: int, user: dict, data: dict) -> dict:
    cur = load_rules(conn, project_id)
    if hides_log_items(user["role"]) and isinstance((data or {}).get("sources"), dict):
        data = {**data, "sources": {k: v for k, v in data["sources"].items() if k != "logs"}}  # 项目经理改不了日志/周确认催办范围
    rules = normalize_rules(data, cur)
    conn.execute("INSERT OR REPLACE INTO knowledge (project_id, kind, key, value_json, ai_run_id, updated_at) VALUES (?,?,?,?,?,?)",
                 (project_id, "nudge_rules", "rules", dumps(rules), None, now_iso()))
    audit(conn, user["id"], "nudge.rules", f"project:{project_id}", dumps(rules)[:300])
    return rules


def in_quiet(now: datetime, start: str, end: str) -> bool:
    if start == end:
        return False
    t = now.strftime("%H:%M")
    return (start <= t < end) if start < end else (t >= start or t < end)


# ----------------------------------------------------------------------------- 找出待催办的对象

def _status_text(overdue: int, due: date) -> str:
    if overdue > 0:
        return f"已超期{overdue}天（期限{due.isoformat()}）"
    if overdue == 0:
        return f"今天到期（期限{due.isoformat()}）"
    return f"{-overdue}天后到期（期限{due.isoformat()}）"


# 催办消息的跳转目标：只用接收人自己能打开的路由。“home/week”是技术员首页/周确认页，升级给其他岗位时不带
_TECH_ONLY_ROUTES = {"home", "week"}


def _notif_link(items: list[dict], user: dict) -> dict | None:
    """一条催办合并多件事时，取超期最久那件的链接（如 finding?id=7 → {"route": "finding", "params": {"id": "7"}}）。"""
    for it in sorted(items, key=lambda i: -(i.get("overdue") or 0)):
        name, _, qs = (it.get("link") or "").partition("?")
        if not name or (name in _TECH_ONLY_ROUTES and user.get("role") != "tech"):
            continue
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        return {"route": name, "params": params}
    return None


def _target(ttype: str, key: str, area: str, subject: str, due: date, today: date, owner_id: int | None, link: str, ref_id: int | None = None) -> dict:
    overdue = (today - due).days
    return {"type": ttype, "key": key, "area": area, "subject": subject, "due": due, "overdue": overdue, "owner_id": owner_id,
            "link": link, "ref_id": ref_id, "status_text": _status_text(overdue, due)}


def _collect_findings(conn, pid: int, rules: dict, today: date) -> tuple[list[dict], list[dict]]:
    out, skipped = [], []
    rows = all_(conn, "SELECT * FROM findings WHERE project_id=? AND agent IN ('safety','quality','dispatch') AND status IN ('open','rejected') "
                      "AND due_date IS NOT NULL AND confirmed_at IS NOT NULL", (pid,))  # AI 候选隐患确认前不催办
    for f in rows:
        try:
            due = date.fromisoformat(f["due_date"][:10])
        except ValueError:
            continue
        if (today - due).days < -rules["lead_days"]:
            continue
        area = {"safety": "safety", "quality": "quality"}.get(f["agent"], "action")
        t = _target("finding", str(f["id"]), area, f["title"][:60], due, today, f["assignee_id"], f"finding?id={f['id']}", f["id"])
        t.update(level=f["level"], agent=f["agent"], kind=f["kind"])
        if not f["assignee_id"]:
            skipped.append({"subject": t["subject"], "area": area, "code": "no_owner", "reason": "还没有责任人，请先派单"})
            continue
        out.append(t)
    return out, skipped


def _collect_logs(conn, pid: int, rules: dict, today: date, users: dict[int, dict]) -> list[dict]:
    out = []
    techs = [u for u in users.values() if u["role"] == "tech"]
    if log_svc.weekly_mode(conn, pid):
        # 周确认：本周五起提醒，下周一为期限；过期一周后不再追
        ws_now = today - timedelta(days=today.weekday())
        for ws in (ws_now, ws_now - timedelta(days=7)):
            due = ws + timedelta(days=7)
            if today < ws + timedelta(days=4) or today > due + timedelta(days=7):
                continue
            for u in techs:
                if (u["created_at"] or "")[:10] > (ws + timedelta(days=6)).isoformat():
                    continue  # 本周之后才开的账号不追
                done = one(conn, "SELECT 1 x FROM logs WHERE author_id=? AND period='week' AND mode='ai' AND log_date=? AND status IN ('submitted','approved')",
                           (u["id"], ws.isoformat()))
                if not done:
                    out.append(_target("weeklog", f"{u['id']}:{ws.isoformat()}", "log", f"{u['name']}的{ws.strftime('%m-%d')}起一周进度确认未提交",
                                       due, today, u["id"], f"week?date={ws.isoformat()}"))
    else:
        # 每日提交模式：只看昨天、前天（周末不要求）
        for back in (1, 2):
            day = today - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            for m in missing_logs(conn, pid, day.isoformat()):
                u = users.get(m["id"])
                if not u or (u["created_at"] or "")[:10] > day.isoformat():
                    continue
                out.append(_target("dailylog", f"{u['id']}:{day.isoformat()}", "log", f"{u['name']}{day.strftime('%m-%d')}的现场日志缺报",
                                   day + timedelta(days=1), today, u["id"], "home"))
    return out


def _collect_gaps(conn, pid: int, today: date, users: dict[int, dict]) -> list[dict]:
    """数据缺口：已开工关键作业连续多日没有日报进度。这是“没报/证据不足”，不是进度预警：
    提醒负责该作业的技术员补报（技术员→分包安全员→工程主管，止于工程主管）；找不到责任技术员的缺口只在工程主管工作台单列。"""
    out = []
    stale_days = int(progress_svc.thresholds(conn, pid)["stale_days"])
    for g in progress_svc.data_gaps(conn, pid, today.isoformat()):
        for o in g["owners"]:
            if o["id"] not in users:
                continue
            due = date.fromisoformat(g["since"]) + timedelta(days=stale_days)
            t = _target("datagap", f"{g['code']}:{g['since']}:{o['id']}", "gap", f"{g['code']} {g['name']}：关键作业无进度上报"[:60], due, today, o["id"], "home")
            # 同一口径（U-26）：写明判定日；天数按同一判定日从最后上报日（从没报过则从 P6 数据日期）算起
            since = f"最后上报{g['last_report']}" if g["last_report"] else f"P6 数据日期{g['since']}之后没有上报过"
            t["status_text"] = f"截至{today.strftime('%m-%d')}已{g['idle_days']}天没有日报进度（{since}）"
            out.append(t)
    return out


# 例会行动项（meeting_items，指令条/例会模块维护）：按表结构自动适配，表不存在则忽略；
# 已关联到某条整改(finding_id)的行动项由该整改的升级链催办，这里不重复
_ACTION_TABLES = ("meeting_items", "action_items")
_DONE_WORDS = {"done", "closed", "resolved", "cancelled", "canceled", "dismissed", "completed", "finished", "已完成", "已关闭", "已取消"}


def _collect_actions(conn, pid: int, today: date, lead: int) -> tuple[list[dict], list[dict]]:
    out, skipped = [], []
    for tbl in _ACTION_TABLES:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
        if not cols:
            continue
        pick = lambda *c: next((x for x in c if x in cols), None)  # noqa: E731
        ass, due, title, status = pick("assignee_id", "owner_id"), pick("due_date", "due"), pick("title", "text", "content", "desc"), pick("status")
        if not (ass and due and title and status and "id" in cols):
            continue
        where, args = ("project_id=?", (pid,)) if "project_id" in cols else ("1=1", ())
        link = ", finding_id fid" if "finding_id" in cols else ", NULL fid"
        for r in conn.execute(f"SELECT id, {ass} a, {due} d, {title} t, {status} s{link} FROM {tbl} WHERE {where}", args).fetchall():
            if str(r["s"] or "").strip().lower() in _DONE_WORDS or not r["d"] or r["fid"]:
                continue
            try:
                d = date.fromisoformat(str(r["d"])[:10])
            except ValueError:
                continue
            if (today - d).days < -lead:
                continue
            t = _target("action", f"{tbl}:{r['id']}", "action", str(r["t"] or "")[:60], d, today, r["a"], "", r["id"])
            if not r["a"]:
                skipped.append({"subject": t["subject"], "area": "action", "code": "no_owner", "reason": "行动项还没有责任人"})
                continue
            out.append(t)
    return out, skipped


def collect_targets(conn, pid: int, rules: dict, today: date, users: dict[int, dict]) -> tuple[list[dict], list[dict]]:
    targets, skipped = [], []
    src = rules["sources"]
    if src.get("findings"):
        t, s = _collect_findings(conn, pid, rules, today)
        targets += t
        skipped += s
    if src.get("logs"):
        targets += _collect_logs(conn, pid, rules, today, users)
        targets += _collect_gaps(conn, pid, today, users)
    if src.get("actions"):
        t, s = _collect_actions(conn, pid, today, rules["lead_days"])
        targets += t
        skipped += s
    if src.get("findings"):   # 致分包文书：未签收超 24 小时、进度类过了回复期限（工笔文书方案 2026-10-08；安全质量类签收后由整改单本身催办）
        from . import sub_letters
        targets += [_target("letter", x["key"], x["area"], x["subject"], x["due"], today, x["owner_id"], x["link"], x["ref_id"]) | {"level": x["level"]}
                    for x in sub_letters.nudge_targets(conn, pid, today)]
    targets.sort(key=lambda x: -x["overdue"])
    return targets, skipped


# ----------------------------------------------------------------------------- 升级链

def _recipients(stage: int, t: dict, users: dict[int, dict]) -> tuple[list[dict], str]:
    """阶段接收人与找不到时的原因。永不包含 leader/admin，也不重复发给责任人本人。"""
    owner = users.get(t["owner_id"])
    if stage == 1:
        if not owner:
            return [], "责任人已停用或不在本项目"
        if owner["role"] in NO_NUDGE_ROLES:
            return [], f"责任人是{R.role_name(owner['role'])}，不催办"
        return [owner], ""
    if stage == 2:
        if t["area"] in PROGRESS_AREAS:  # D9：进度类催办分包安全员没有可做的动作，本人之后直接升级到工程主管
            return [], "进度类催办不发给分包安全员"
        co = (owner or {}).get("company") or ""
        if not owner or not R.is_sub(owner["role"]) or not co:
            return [], "责任人不属于分包单位，跳过单位安全员这一级"
        r = [u for u in users.values() if u["role"] == "sub_safety" and u["company"] == co and u["id"] != owner["id"]]
        return r, "" if r else f"{co}没有安全员账号，跳过这一级"
    if stage == 3:
        role = "safety_sup" if t["area"] == "safety" else "eng_sup"
        r = [u for u in users.values() if u["role"] == role and u["id"] != t["owner_id"]]
        return r, "" if r else f"项目里没有{R.role_name(role)}账号"
    if t["area"] in LOG_LIKE_AREAS:  # 项目经理只关注进度、质量、安全，不处理日志/周确认/数据缺口；这类事项升级链止于工程主管
        return [], "日志/周确认只升级到工程主管，不升级到项目经理"
    if t["area"] == "letter_progress":  # 对分包的生产催办，与项目经理的赶工拍板是两回事，止于工程主管
        return [], "进度联系单只升级到工程主管，不升级到项目经理"
    if _stops_at_safety_sup(t):
        return [], "中低风险隐患最高只升级到安全主管"
    r = [u for u in users.values() if u["role"] == "manager" and u["id"] != t["owner_id"]]
    return r, "" if r else "项目里没有项目经理账号"


def _stops_at_safety_sup(t: dict) -> bool:
    """D9：中低风险（含 info）安全隐患的升级链止于安全主管。隐患目标都带 level；未带等级的目标（非隐患来源）不受此限。"""
    return t["area"] == "safety" and t.get("level") not in (None, "high")


def _in_pm_todo(t: dict) -> bool:
    """阶段4 发给项目经理后，此事项会出现在其首页待办“升级到项目经理的高风险问题”里（与 todo._escalation 同口径）。"""
    return t["type"] == "finding" and t.get("agent") in PM_TODO_AGENTS and t.get("level") == "high" and t.get("kind") != "qc_reminder"


def _load_state(conn, pid: int) -> dict[tuple[str, str], list[dict]]:
    st: dict[tuple[str, str], list[dict]] = {}
    for r in all_(conn, "SELECT * FROM nudge_log WHERE project_id=?", (pid,)):
        st.setdefault((r["obj_type"], r["obj_key"]), []).append(r)
    return st


def _plan(conn, pid: int, t: dict, rows: list[dict], users: dict, rules: dict, now: datetime, dry: bool) -> tuple[tuple[int, list[dict]] | None, dict | None]:
    """返回 (本轮要发的 (阶段, 接收人) 或 None, 暂不发送的原因 或 None)。无人可接的阶段写一条 skipped 并顺延到下一阶段。"""
    first = [r for r in rows if r["stage"] == 1 and r["status"] == "sent" and r["to_user_id"] == t["owner_id"]]
    if not first:
        recips, why = _recipients(1, t, users)
        return ((1, recips), None) if recips else (None, {"code": "no_recipient", "reason": why})
    prev = datetime.fromisoformat(min(r["sent_at"] for r in first))
    for stage in (2, 3, 4):
        done = [r for r in rows if r["stage"] == stage]
        if done:
            prev = max(prev, datetime.fromisoformat(max(r["sent_at"] for r in done)))
            continue
        if stage == 4 and _stops_at_safety_sup(t):
            break  # 不写“跳过”记录：风险等级日后调为高风险时仍可升级到项目经理
        gap = float(rules["gap_hours"][stage - 2])
        due_at = prev + timedelta(hours=gap)
        if now < due_at:
            return None, {"code": "waiting", "reason": f"已{STAGE_NAME[stage - 1]}，{due_at.strftime('%m-%d %H:%M')}后仍未处理将{STAGE_NAME[stage]}"}
        recips, why = _recipients(stage, t, users)
        if recips:
            return (stage, recips), None
        if not dry:
            conn.execute("INSERT OR IGNORE INTO nudge_log (project_id, obj_type, obj_key, stage, to_user_id, status, subject, status_text, owner_id, area, link, reason, sent_at) "
                         "VALUES (?,?,?,?,0,'skipped',?,?,?,?,?,?,?)",
                         (pid, t["type"], t["key"], stage, t["subject"], t["status_text"], t["owner_id"], t["area"], t["link"], why, prev.isoformat(timespec="seconds")))
        rows.append({"stage": stage, "status": "skipped", "to_user_id": 0, "sent_at": prev.isoformat(timespec="seconds")})
    return None, {"code": "chain_done", "reason": "升级链已走完，等待处理"}


# ----------------------------------------------------------------------------- 话术（催办Agent）

_SYSTEM = """你是施工项目部的催办助理，负责把提醒写得得体、温和、不过度。
只能使用给定事实（事项数量、状态、责任人），不得编造任何数字、日期、人名或后果；不要指责、威胁或提及处罚；
只写提醒开头的一句到两句话（不超过80字），不要重复列出事项清单（清单由系统另附）；
不要写称呼和问候语（“某某，您好”由系统统一加上），直接写正文。
只输出JSON：{"intro":"开头的话"}"""

_BANNED = ("罚", "处分", "问责", "追究", "通报", "扣", "严重后果", "后果自负", "否则", "必须立即")


def _intro_template(stage: int, items: list[dict], users: dict) -> str:
    """开头正文（不含称呼；称呼由 _salutation 统一加）。"""
    if stage == 1 and items and all(i["type"] == "datagap" for i in items):
        return "以下关键作业已有一段时间没收到进度记录，请补一条随手记或在本周确认里补上进度；如现场确实没有施工，也请记一句原因。"
    if stage == 1:
        return "以下事项临近或已过期限，请抽空处理；如已处理或遇到困难，直接回复对应主管即可。"
    names = "、".join(dict.fromkeys((users.get(i["owner_id"]) or {}).get("name", "") for i in items if users.get(i["owner_id"])))
    if stage == 2:
        return f"{names or '同事'}负责的以下事项已提醒本人，目前仍未处理，麻烦协助跟进。"
    if stage == 3:  # 进度类（D9）不经过单位安全员，只说“已提醒到责任人”
        return "以下事项已提醒到责任人，目前仍未处理，请您关注并协调。"
    return "以下事项经逐级提醒仍未处理，请您关注并给出处理意见。"


def _salutation(recipient: dict) -> str:
    """统一称呼（S-5⑧）：姓名 + “，您好：”，不按岗位加头衔；AI 不写称呼。"""
    return f"{recipient.get('name') or '您'}，您好："


# AI 自己写的称呼问候（如“周明同志，您好！”“安全员周明，您好”“您好，”）：去掉，称呼统一由系统加
_GREETING = re.compile(r"^\s*(?:[^，,。！!：:\n]{1,16}[，,\s]*)?(?:您好|你好)[，,。！!：:\s]*")


def _strip_greeting(text: str) -> str:
    return _GREETING.sub("", text or "", count=1).strip()


def _fact_lines(items: list[dict], stage: int, users: dict) -> list[str]:
    lines = []
    for i, it in enumerate(items[:5], 1):
        who = (users.get(it["owner_id"]) or {}).get("name")
        extra = f"责任人{who}；" if stage >= 2 and who else ""
        # 只对确实会进入项目经理首页待办的事项这样写（T-06：高风险安全/质量问题），其余不作此表述
        mark = "（已列入您的首页待办）" if stage == 4 and _in_pm_todo(it) else ""
        lines.append(f"{i}. {it['subject']}：{extra}{it['status_text']}{mark}")
    if len(items) > 5:
        lines.append(f"另有{len(items) - 5}件，详见催办台。")
    return lines


def compose(project_id: int, stage: int, recipient: dict, items: list[dict], users: dict, use_ai: bool) -> dict:
    """生成一条通知：标题 + 统一称呼 + 开头语（AI 或模板）+ 系统拼接的事实清单。"""
    tpl = _intro_template(stage, items, users)
    facts = f"共{len(items)}件；" + "；".join(f"{it['subject']}，{it['status_text']}" for it in items[:8])
    intro, via, run_id = tpl, "template", None
    if use_ai:
        def check(d):
            text = _strip_greeting(str((d or {}).get("intro") or ""))
            if not text or len(text) > 120:
                raise ValueError("开头语为空或过长")
            if any(w in text for w in _BANNED):
                raise ValueError("语气过重")
            if set(re.findall(r"\d+", text)) - set(re.findall(r"\d+", facts)):
                raise ValueError("出现了事实之外的数字")
            return {"intro": text}
        try:
            res = call_json("nudge.plan", system=_SYSTEM,
                            user=f"提醒阶段：{STAGE_NAME[stage]}；接收人：{R.role_name(recipient['role'])}{recipient['name']}\n事实：{facts}",
                            prompt_version="nudge-v2-20261007", mock=lambda: {"intro": tpl}, validate=check, project_id=project_id,
                            max_tokens=300, temperature=0.3, input_meta={"stage": stage, "items": len(items)})
            intro, via, run_id = res.data["intro"], "ai", res.run_id
        except AIError as exc:
            log.info("催办话术回退模板：%s", exc)
    n = len(items)
    head = items[0]["subject"][:22]
    title = {1: f"催办提醒：{head}" if n == 1 else f"催办提醒：{n}件事项待处理",
             2: f"协助跟进：{head}" if n == 1 else f"协助跟进：{n}件事项待处理",
             3: f"升级提醒：{head}" if n == 1 else f"升级提醒：{n}件事项仍未处理",
             4: f"升级到经理：{head}" if n == 1 else f"升级到经理：{n}件事项逐级提醒后仍未处理"}[stage]
    body = (_salutation(recipient) + intro + "\n" + "\n".join(_fact_lines(items, stage, users)))[:600]
    return {"title": title, "body": body, "via": via, "run_id": run_id}


# ----------------------------------------------------------------------------- 一轮运行

def _sent_today(conn, pid: int, uid: int, day: str) -> int:
    return one(conn, "SELECT COUNT(DISTINCT notif_id) n FROM nudge_log WHERE project_id=? AND to_user_id=? AND status='sent' AND substr(sent_at,1,10)=?",
               (pid, uid, day))["n"]


def _split_items(conn, pid: int, uid: int, stage: int, items: list[dict], day: str) -> tuple[list[dict], list[dict], list[dict]]:
    """把一条通知的事项分成：(仍可发的, 本阶段已有记录的（同时进行的另一轮已发出）, 今天已就同一事项提醒过此人的（不分阶段）)。"""
    done = {(r["obj_type"], r["obj_key"]) for r in all_(conn, "SELECT obj_type, obj_key FROM nudge_log WHERE project_id=? AND stage=? AND to_user_id=?",
                                                         (pid, stage, uid))}
    seen = {(r["obj_type"], r["obj_key"]) for r in all_(conn, "SELECT obj_type, obj_key FROM nudge_log WHERE project_id=? AND to_user_id=? AND status='sent' "
                                                              "AND substr(sent_at,1,10)=?", (pid, uid, day))}
    ok, taken, same_day = [], [], []
    for it in items:
        k = (it["type"], it["key"])
        (taken if k in done else same_day if k in seen else ok).append(it)
    return ok, taken, same_day


def _claim_and_send(conn, pid: int, stage: int, u: dict, items: list[dict], msg: dict, cap: int, now: datetime,
                    users: dict, actor: dict | None) -> dict:
    """原子认领并发送一条催办。同一个 IMMEDIATE 事务内：复核每日上限 → 逐件认领 nudge_log（先记 claimed）→ 插入通知并回填 notif_id。
    多个进程、或“立即运行”与后台调度同时跑时，同一（接收人, 阶段, 事项）只有一个能认领到；claimed 在提交前对其他连接不可见，出错整体回滚。"""
    day, at = now.date().isoformat(), now.isoformat(timespec="seconds")
    out: dict = {"sent": [], "taken": [], "same_day": [], "capped": False, "msg": msg}
    with tx(conn):
        if _sent_today(conn, pid, u["id"], day) >= cap:
            out["capped"] = True
            return out
        ok, out["taken"], out["same_day"] = _split_items(conn, pid, u["id"], stage, items, day)
        claimed: list[int] = []
        for it in ok:
            cur = conn.execute("INSERT OR IGNORE INTO nudge_log (project_id, obj_type, obj_key, stage, to_user_id, status, subject, status_text, owner_id, area, link, sent_at) "
                               "VALUES (?,?,?,?,?,'claimed',?,?,?,?,?,?)",
                               (pid, it["type"], it["key"], stage, u["id"], it["subject"], it["status_text"], it["owner_id"], it["area"], it["link"], at))
            if cur.rowcount == 1:
                claimed.append(cur.lastrowid)
                out["sent"].append(it)
            else:
                out["taken"].append(it)
        if not claimed:  # 全被别的运行认领：丢弃这条文案
            return out
        if len(out["sent"]) < len(items):  # 只认领到部分事项：按剩余事项用模板重拼（事务里不调 AI）
            msg = out["msg"] = compose(pid, stage, u, out["sent"], users, use_ai=False)
        link = _notif_link(out["sent"], u)
        nid = insert(conn, "notifications", {"project_id": pid, "user_id": u["id"], "alert_id": None, "title": msg["title"], "body": msg["body"],
                                             "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(), "link_json": dumps(link) if link else None})
        conn.execute(f"UPDATE nudge_log SET status='sent', via=?, ai_run_id=?, notif_id=? WHERE id IN ({','.join('?' * len(claimed))})",
                     (msg["via"], msg["run_id"], nid, *claimed))
        audit(conn, actor["id"] if actor else None, "nudge.auto", f"user:{u['id']}", f"阶段{stage} {len(out['sent'])}件 {msg['title']}"[:200])
        event(conn, "nudge.sent", project_id=pid, user_id=u["id"], ref_id=nid, value=stage, data={"items": len(out["sent"]), "via": msg["via"]})
    return out


def _entry(stage: int, u: dict, items: list[dict]) -> dict:
    return {"stage": stage, "stage_name": STAGE_NAME[stage], "to": {"id": u["id"], "name": u["name"], "role_name": R.role_name(u["role"])},
            "items": [{"subject": i["subject"], "status_text": i["status_text"], "area": i["area"]} for i in items]}


def run_project(conn, project_id: int, *, now: datetime | None = None, dry: bool = False, actor: dict | None = None) -> dict:
    """对一个项目跑一轮升级链。dry=True 只预览不写库。返回发送清单与“为什么不发”的原因摘要。"""
    now = (now or datetime.now()).replace(microsecond=0)
    today = now.date()
    rules = load_rules(conn, project_id)
    rep: dict = {"at": now.isoformat(timespec="seconds"), "dry": dry, "enabled": rules["enabled"], "sent": [], "deferred": [], "skipped": [],
                 "reasons": {}, "targets": 0, "quiet": in_quiet(now, rules["quiet_start"], rules["quiet_end"])}
    if not rules["enabled"]:
        rep["summary"] = "催办Agent 已在规则中关闭，本次没有发送。"
        return rep
    users = {u["id"]: u for u in all_(conn, "SELECT * FROM users WHERE project_id=? AND active=1", (project_id,))}
    targets, skipped = collect_targets(conn, project_id, rules, today, users)
    if actor and hides_log_items(actor["role"]):
        # 项目经理“立即运行”：日志/周确认缺报、数据缺口不以其名义催办（这类事项由工程主管或后台调度跑）
        targets = [t for t in targets if t["area"] not in LOG_LIKE_AREAS]
        skipped = [s for s in skipped if s.get("area") not in LOG_LIKE_AREAS]
    rep["targets"] = len(targets)
    rep["areas"] = dict(Counter(t["area"] for t in targets))
    reasons: Counter = Counter()
    by_area: dict[str, Counter] = {}

    def count(code: str, area: str, n: int = 1) -> None:
        reasons[code] += n
        by_area.setdefault(area, Counter())[code] += n
    for s in skipped:
        count(s["code"], s.get("area", ""))
        rep["skipped"].append(s)
    state = _load_state(conn, project_id)
    steps: list[tuple[dict, int, list[dict]]] = []
    for t in targets:
        step, why = _plan(conn, project_id, t, state.setdefault((t["type"], t["key"]), []), users, rules, now, dry)
        if step:
            steps.append((t, step[0], step[1]))
        else:
            count(why["code"], t["area"])
            rep["skipped"].append({"subject": t["subject"], "area": t["area"], "code": why["code"], "reason": why["reason"]})

    # 按 (接收人, 阶段) 合并：同一人同一阶段的多件事一条通知
    groups: dict[tuple[int, int], dict] = {}
    for t, stage, recips in steps:
        for u in recips:
            groups.setdefault((u["id"], stage), {"user": u, "stage": stage, "items": []})["items"].append(t)
    ai_left = MAX_AI_PER_RUN
    day = today.isoformat()

    def later(stage: int, u: dict, items: list[dict], code: str, why: str) -> None:
        for i in items:
            count(code, i["area"])
        if items:
            rep["deferred"].append({**_entry(stage, u, items), "code": code, "why": why})

    def already_sent(items: list[dict]) -> None:
        for i in items:
            count("concurrent", i["area"])
            rep["skipped"].append({"subject": i["subject"], "area": i["area"], "code": "concurrent", "reason": "同时进行的另一轮已发出"})
    for (uid, stage), g in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        u, items = g["user"], g["items"]
        if u["role"] in NO_NUDGE_ROLES:  # 双保险
            continue
        if rep["quiet"]:
            later(stage, u, items, "quiet", "静默时段，延后到静默结束后")
            continue
        cap_why, same_why = f"{u['name']}今天已收到 {rules['daily_cap']} 条催办，明天再发", f"今天已就同一事项提醒过{u['name']}，明天再发"
        # 先只读筛一遍，省掉注定发不出去的 AI 调用；真正发不发以下面事务内的认领为准
        items, lost, same_day = _split_items(conn, project_id, u["id"], stage, items, day)
        already_sent(lost)
        later(stage, u, same_day, "same_day", same_why)
        if not items:
            continue
        if _sent_today(conn, project_id, u["id"], day) >= rules["daily_cap"]:
            later(stage, u, items, "cap", cap_why)
            continue
        if dry:
            rep["sent"].append({**_entry(stage, u, items), "title": "", "via": "preview"})
            continue
        msg = compose(project_id, stage, u, items, users, use_ai=ai_left > 0)
        if msg["via"] == "ai":
            ai_left -= 1
        res = _claim_and_send(conn, project_id, stage, u, items, msg, rules["daily_cap"], now, users, actor)
        if res["capped"]:
            later(stage, u, items, "cap", cap_why)
            continue
        already_sent(res["taken"])
        later(stage, u, res["same_day"], "same_day", same_why)
        if res["sent"]:
            rep["sent"].append({**_entry(stage, u, res["sent"]), "title": res["msg"]["title"], "via": res["msg"]["via"]})

    rep["reasons"] = dict(reasons)
    rep["reasons_by_area"] = {a: dict(c) for a, c in by_area.items()}
    rep["skipped"] = rep["skipped"][:40]
    rep["summary"] = _summary(dry, len(targets), rep["sent"], reasons)
    if not dry:
        conn.execute("INSERT OR REPLACE INTO knowledge (project_id, kind, key, value_json, ai_run_id, updated_at) VALUES (?,?,?,?,?,?)",
                     (project_id, "nudge_state", "last_run", dumps({"at": rep["at"], "quiet": rep["quiet"], "reasons": rep["reasons"], "sent": len(rep["sent"]),
                                                                    "reasons_by_area": rep["reasons_by_area"], "summary": rep["summary"]}), None, now_iso()))
    return rep


def _summary(dry: bool, targets: int, sent: list[dict], reasons: dict) -> str:
    people = len({s["to"]["id"] for s in sent if s.get("to")})
    notp = [f"{n}件{lab}" for code, lab in (("quiet", "在静默时段延后"), ("cap", "已达每日上限"), ("same_day", "今天已提醒过同一人"),
                                          ("concurrent", "已由同时进行的另一轮发出"), ("waiting", "未到升级时间"),
                                          ("chain_done", "升级链已走完等待处理"), ("no_owner", "没有责任人"), ("no_recipient", "找不到可提醒的人")) if (n := reasons.get(code))]
    return (f"{'预览：' if dry else ''}检查到 {targets} 件待跟进事项，{'将' if dry else '已'}发出 {len(sent)} 条提醒（{people} 人）"
            + (f"；暂不发送：{'、'.join(notp)}" if notp else "") + "。")


# ----------------------------------------------------------------------------- 展示：日志与今日动态

def _area_filter(role: str) -> str:
    return f" AND l.area NOT IN ({','.join(repr(a) for a in LOG_LIKE_AREAS)})" if hides_log_items(role) else ""


def recent_log(conn, project_id: int, limit: int = 60, role: str = "") -> list[dict]:
    rows = all_(conn, f"""SELECT l.*, u.name to_name, u.role to_role, o.name owner_name FROM nudge_log l LEFT JOIN users u ON u.id=l.to_user_id
                         LEFT JOIN users o ON o.id=l.owner_id WHERE l.project_id=?{_area_filter(role)} ORDER BY l.sent_at DESC, l.id DESC LIMIT ?""", (project_id, limit))
    fstat = {r["id"]: r["status"] for r in all_(conn, "SELECT id, status FROM findings WHERE project_id=?", (project_id,))}
    out = []
    for r in rows:
        handled = None
        if r["obj_type"] == "finding":
            handled = fstat.get(int(r["obj_key"])) not in ("open", "rejected")
        out.append({"id": r["id"], "at": r["sent_at"], "stage": r["stage"], "stage_name": STAGE_NAME.get(r["stage"], ""), "status": r["status"],
                    "to": r["to_name"] or "", "to_role": R.role_name(r["to_role"]) if r["to_role"] else "", "owner": r["owner_name"] or "",
                    "subject": r["subject"], "status_text": r["status_text"], "area": r["area"], "area_name": AREA_NAME.get(r["area"], ""),
                    "via": r["via"], "reason": r["reason"], "link": r["link"], "handled": handled})
    return out


def today_summary(conn, project_id: int, now: datetime | None = None, role: str = "") -> dict:
    """催办Agent 今日动态：今天自动提醒了谁/几件、哪些已升级到主管、哪些因静默延后。只数真实发送记录（项目经理不含日志类缺报）。"""
    now = now or datetime.now()
    d = now.date().isoformat()
    rows = all_(conn, f"""SELECT l.*, u.name to_name FROM nudge_log l LEFT JOIN users u ON u.id=l.to_user_id
                         WHERE l.project_id=? AND l.status='sent' AND substr(l.sent_at,1,10)=?{_area_filter(role)} ORDER BY l.stage DESC, l.id DESC""", (project_id, d))
    objs = {(r["obj_type"], r["obj_key"]) for r in rows}
    persons = {r["to_user_id"] for r in rows}
    up = [{"subject": r["subject"], "to": r["to_name"], "stage": r["stage"], "stage_name": STAGE_NAME[r["stage"]], "status_text": r["status_text"]}
          for r in rows if r["stage"] >= 3]
    last = one(conn, "SELECT value_json FROM knowledge WHERE project_id=? AND kind='nudge_state' AND key='last_run'", (project_id,))
    st = loads(last["value_json"], {}) if last else {}
    today_run = bool(st.get("at") and st["at"][:10] == d)
    if hides_log_items(role) and "reasons_by_area" in st:  # 项目经理：延后件数也不含日志类缺报
        st["reasons"] = dict(sum((Counter(c) for a, c in st["reasons_by_area"].items() if a not in LOG_LIKE_AREAS), Counter()))
    rules = load_rules(conn, project_id)
    return {"day": d, "enabled": rules["enabled"], "objects": len(objs), "persons": len(persons),
            "by_stage": {str(s): sum(1 for r in rows if r["stage"] == s) for s in (1, 2, 3, 4)},
            "escalated": up[:6], "escalated_n": len({(u["subject"], u["to"]) for u in up}),
            "deferred_quiet": (st.get("reasons") or {}).get("quiet", 0) if today_run else 0,
            "deferred_cap": (st.get("reasons") or {}).get("cap", 0) if today_run else 0,
            "last_run_at": st.get("at"), "quiet": f"{rules['quiet_start']}–{rules['quiet_end']}"}


# ----------------------------------------------------------------------------- 固定与订阅式提问

def _pin_view(p: dict) -> dict:
    return {"id": p["id"], "question": p["question"], "schedule": p["schedule"], "schedule_name": SCHEDULES.get(p["schedule"], ""),
            "created_at": p["created_at"], "last_run_at": p["last_run_at"], "last_error": p["last_error"], "last_error_at": p["last_error_at"],
            "answer": loads(p["last_answer_json"], None)}


def _clean_answer(a: dict | None) -> dict | None:
    """页面带来的回答只保留问答结构与出处字段（不信任多余内容）。"""
    if not isinstance(a, dict) or not str(a.get("answer") or "").strip():
        return None
    plan = a.get("plan") if isinstance(a.get("plan"), dict) else {}
    return {"answer": str(a["answer"]).strip()[:4000], "refs": [str(r)[:30] for r in (a.get("refs") or [])][:30],
            "facts_count": int(a["facts_count"]) if isinstance(a.get("facts_count"), int) else None,
            "plan": {"date_from": plan.get("date_from"), "date_to": plan.get("date_to"), "keywords": [str(k)[:20] for k in (plan.get("keywords") or [])][:8]},
            "at": now_iso()}


def _pin_user_ok(user: dict) -> None:
    if user["role"] not in PIN_ROLES:
        raise PermissionError("固定到首页/订阅只对港湾管理岗开放（分包单位不开放）")


def list_pins(conn, user: dict) -> list[dict]:
    _pin_user_ok(user)
    return [_pin_view(p) for p in all_(conn, "SELECT * FROM ask_pins WHERE project_id=? AND user_id=? ORDER BY id DESC", (user["project_id"], user["id"]))]


def create_pin(conn, user: dict, question: str, schedule: str = "", answer: dict | None = None) -> dict:
    _pin_user_ok(user)
    question = (question or "").strip()
    if not question or len(question) > 300:
        raise ValueError("问题不能为空且不超过300字")
    if schedule not in ("", *SCHEDULES):
        raise ValueError("订阅周期只能是每天/每周一/每周五")
    ex = one(conn, "SELECT * FROM ask_pins WHERE project_id=? AND user_id=? AND question=?", (user["project_id"], user["id"], question))
    ans = None  # 前端回传的答案不可信，一律由服务端按本人权限重问（见下方 run_pin）
    if ex:  # 同一问题只保留一条：更新订阅周期与答案
        conn.execute("UPDATE ask_pins SET schedule=?, last_answer_json=COALESCE(?, last_answer_json), last_run_at=CASE WHEN ? IS NULL THEN last_run_at ELSE ? END WHERE id=?",
                     (schedule, dumps(ans) if ans else None, ans and 1, now_iso(), ex["id"]))
        pid = ex["id"]
    else:
        if one(conn, "SELECT COUNT(*) n FROM ask_pins WHERE project_id=? AND user_id=?", (user["project_id"], user["id"]))["n"] >= MAX_PINS_PER_USER:
            raise ValueError(f"最多固定 {MAX_PINS_PER_USER} 个问题，请先取消不用的")
        pid = insert(conn, "ask_pins", {"project_id": user["project_id"], "user_id": user["id"], "question": question, "schedule": schedule,
                                        "created_at": now_iso(), "last_run_at": now_iso() if ans else None,
                                        "last_answer_json": dumps(ans) if ans else None})
    audit(conn, user["id"], "ask.pin", f"pin:{pid}", f"{schedule or '仅固定'} {question[:60]}")
    pin = one(conn, "SELECT * FROM ask_pins WHERE id=?", (pid,))
    if not pin["last_answer_json"]:  # 没带回答（接口直调）就现问一次，保证看板上有内容
        run_pin(conn, pin, user)
        pin = one(conn, "SELECT * FROM ask_pins WHERE id=?", (pid,))
    return _pin_view(pin)


def _own_pin(conn, user: dict, pin_id: int) -> dict:
    _pin_user_ok(user)
    p = one(conn, "SELECT * FROM ask_pins WHERE id=? AND project_id=? AND user_id=?", (pin_id, user["project_id"], user["id"]))
    if not p:
        raise ValueError("固定的问题不存在")
    return p


def set_pin_schedule(conn, user: dict, pin_id: int, schedule: str) -> dict:
    p = _own_pin(conn, user, pin_id)
    if schedule not in ("", *SCHEDULES):
        raise ValueError("订阅周期只能是每天/每周一/每周五")
    conn.execute("UPDATE ask_pins SET schedule=? WHERE id=?", (schedule, p["id"]))
    return _pin_view(one(conn, "SELECT * FROM ask_pins WHERE id=?", (p["id"],)))


def delete_pin(conn, user: dict, pin_id: int) -> None:
    p = _own_pin(conn, user, pin_id)
    conn.execute("DELETE FROM ask_pins WHERE id=?", (p["id"],))
    audit(conn, user["id"], "ask.unpin", f"pin:{p['id']}", p["question"][:60])


def run_pin(conn, pin: dict, user: dict, *, notify: bool = False) -> dict:
    """重跑一次问答。失败只记录错误，不覆盖上次成功的答案。"""
    try:
        res = qa_svc.ask(conn, user, pin["question"])
    except Exception as exc:  # noqa: BLE001  AI 失败、权限、问题非法都按“失败不覆盖”处理
        msg = str(exc)[:200] or exc.__class__.__name__
        conn.execute("UPDATE ask_pins SET last_error=?, last_error_at=? WHERE id=?", (msg, now_iso(), pin["id"]))
        return {"ok": False, "error": msg}
    ans = {"answer": res["answer"], "refs": res["refs"], "facts_count": res["facts_count"],
           "plan": {"date_from": res["plan"].get("date_from"), "date_to": res["plan"].get("date_to"), "keywords": res["plan"].get("keywords", [])},
           "at": now_iso()}
    conn.execute("UPDATE ask_pins SET last_answer_json=?, last_run_at=?, last_error=NULL, last_error_at=NULL WHERE id=?", (dumps(ans), now_iso(), pin["id"]))
    event(conn, "qa.pin_run", project_id=pin["project_id"], user_id=user["id"], ref_id=pin["id"])
    if notify:
        insert(conn, "notifications", {"project_id": pin["project_id"], "user_id": user["id"], "alert_id": None, "title": "你订阅的提问有新回答",
                                       "body": f"{pin['question'][:40]}：{res['answer'][:80]}", "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso()})
    return {"ok": True}


def refresh_pin(conn, user: dict, pin_id: int) -> dict:
    p = _own_pin(conn, user, pin_id)
    r = run_pin(conn, p, user)
    return {**_pin_view(one(conn, "SELECT * FROM ask_pins WHERE id=?", (p["id"],))), "ok": r["ok"], "error": r.get("error")}


def pin_due(pin: dict, now: datetime) -> bool:
    sch = pin["schedule"]
    if sch not in SCHEDULES:
        return False
    today = now.date()
    due_day = today if sch == "daily" else today - timedelta(days=(today.weekday() - (0 if sch == "mon" else 4)) % 7)
    last = date.fromisoformat((pin["last_run_at"] or pin["created_at"])[:10])
    if last >= due_day:
        return False
    if due_day == today and now.hour < SUBSCRIBE_HOUR:
        return False
    if pin["last_error_at"]:  # 失败后两小时内不重试，避免刷错误
        try:
            if now - datetime.fromisoformat(pin["last_error_at"]).replace(tzinfo=None) < timedelta(hours=2):
                return False
        except ValueError:
            pass
    return True


def run_due_pins(conn, now: datetime | None = None, limit: int = 20) -> int:
    now = now or datetime.now()
    n = 0
    for p in all_(conn, "SELECT * FROM ask_pins WHERE schedule<>'' ORDER BY id"):
        if n >= limit:
            break
        if not pin_due(p, now):
            continue
        u = one(conn, "SELECT * FROM users WHERE id=? AND active=1", (p["user_id"],))
        if not u or u["role"] not in PIN_ROLES:
            continue
        run_pin(conn, p, u, notify=True)
        n += 1
    return n


# ----------------------------------------------------------------------------- 后台 tick

def tick(now: datetime | None = None) -> dict:
    """调度线程每 30 分钟调用一次：各项目跑升级链 + 到期的订阅提问。单项失败不影响其它。"""
    out = {"projects": 0, "sent": 0, "pins": 0}
    conn = connect()
    try:
        for p in all_(conn, "SELECT id FROM projects"):
            try:
                rep = run_project(conn, p["id"], now=now)
                out["projects"] += 1
                out["sent"] += len(rep["sent"])
            except Exception:  # noqa: BLE001
                log.exception("催办运行失败：项目 %s", p["id"])
        try:
            out["pins"] = run_due_pins(conn, now)
        except Exception:  # noqa: BLE001
            log.exception("订阅提问运行失败")
    finally:
        conn.close()
    return out
