"""各岗位首页统一待办队列：把分散在各处的“要我办的事”汇成一张表，只汇总现有数据，不新增业务流程。

口径与边界（AGENTS.md）：
- 每个来源只读已有记录；角色权限沿用 app/roles.py 的 can() 与既有数据范围（分包单位只见本单位）；
- 项目经理只关注进度、质量、安全，且只处理要自己拍板的事（D7、D19）：待办只留已确认红色预警的赶工决定、升级到经理的高风险隐患、
  周报发布、指派给本人的例会行动项；其余合成一行“需要知道：N 项”（只给数字和链接）。来源按角色挂接，
  最后再按白名单 PM_ALLOWED、反向白名单 PM_FORBIDDEN 和简报同一套 scope_ok 做兜底过滤；
- AI 候选隐患是待办来源之一，确认/修改后确认/不采纳仍就地处理（前端沿用 CandidateBox，T-05 的 CandidateGroup 合入后按照片成组），
  首页不再另放“待确认隐患”区块；项目经理退出候选确认与预警“属实/误报”反馈（权限保留，只是不进待办）；
- 未知数据不补零：没有数据的来源不出条目，空库给出带按钮的引导。
每个条目：id, module, title, reason, status, due, action_label, action_route（hash 路由，如 finding?id=3）, overdue，
以及 tone（状态色）、source、可选的就地操作 quick / inline、附加链接 links 与展示用附加字段。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from .. import roles as R
from ..db import all_, audit, insert, loads, now_iso, one, tx
from . import brief as brief_svc
from . import facts as facts_svc
from . import features as feat_svc
from . import findings as find_svc
from . import logs as log_svc
from . import site_agents
from . import p6, role_views, site_agents

log = logging.getLogger("zhimou.todo")

MODULES = [("site", "现场"), ("progress", "进度"), ("safety", "安全"), ("quality", "质量"), ("report", "报表")]
MODULE_NAME = dict(MODULES)
TONE_RANK = {"red": 0, "orange": 1, "blue": 2, "gray": 3, "green": 4}
MAX_ITEMS = 200
PER_SOURCE = 8          # 同一来源最多逐条列出几项，其余折成“另有 N 项”
BOUNDARY = "AI 只整理线索与依据，办理结论由人员确认。"
# 项目经理的待办里不得出现的来源（反向白名单，只增不减）：日志/周确认/数据缺口/产值类，以及 D7 退出的执行层事项——
# AI 候选确认、放行清单、扫码认领、材料疑点与票据、隐蔽验收资料、预警“属实/误报”反馈（alert）
PM_FORBIDDEN = frozenset({"log_review", "log_low", "log_missing", "week_missing", "data_gap", "mapping",
                          "candidate", "gate", "gate_more", "quick", "ticket_risk", "ticket_pending", "conceal_gap", "conceal_todo", "conceal_result", "alert",
                          "letter", "scan_failed"})   # letter：致分包文书的签收/回复/接受，是执行层事项；scan_failed：照片识别失败重试
# 项目经理待办的白名单（D7、D19；claim_notice 为索赔通知时效剩 3 天升级，用户 10-08 已定）：赶工决定（crash，不与预警反馈 alert 重名）、升级到经理的高风险隐患、周报发布、本人的例会行动项
PM_ALLOWED = frozenset({"crash", "escalated", "report", "meeting", "claim_notice"})
READONLY_ROLES = frozenset({"leader", "admin"})
# 项目经理“智能体今日处理”只列进度、质量、安全相关的 Agent（与智能应用页同一份，定义在 roles.py）
PM_AGENTS = R.PM_AGENTS
# “事项”列的小标签：按来源选词，只给 ai=True 的条目；人工来源（派单整改、扫码上报、例会、缺报提醒、放行清单、数据缺口）不加
AI_TAG = {
    "candidate": "AI 发现",      # 安全 Agent 照片识别的候选隐患
    "alert": "AI 分析",          # 进度预警由规则判定，AI 只做原因分析
    "crash": "AI 分析",          # 赶工决定：预警由规则判定、工程主管确认属实，AI 只分析原因、提出赶工方案
    "finding": "AI 发现",        # 项目经理待决策里安全/质量 Agent 发现的事项
    "escalated": "AI 发现",      # 升级到项目经理的高风险隐患（安全/质量 Agent 发现的）
    "log_review": "AI 发现",     # 数据审核 Agent 预审出的疑点
    "log_low": "AI 发现",        # 数据审核 Agent 逐份预审的低风险日志
    "ticket_risk": "AI 发现",    # 材料与砼疑点
    "conceal_gap": "AI 发现",    # 疑似缺验收资料
    "report": "AI 起草",         # 报表 Agent 起草的周/月报
    "mapping": "AI 起草",        # AI 建议的清单映射
    "ticket_pending": "AI 起草",  # 小票识别成的草稿
}


class Ctx:
    def __init__(self, conn, user: dict, today: date | None = None):
        self.conn, self.user = conn, user
        self.role, self.pid, self.uid = user["role"], user["project_id"], user["id"]
        self.today = today or date.today()
        self.iso = self.today.isoformat()
        self._users: dict[int, dict] | None = None
        self._cand_ids: set[int] = set()
        self._esc: tuple[list[dict], list[dict]] | None = None
        self.features = feat_svc.flags(conn, self.pid)      # 项目设置里隐藏的增强能力：其待办来源不出条目

    def feat(self, key: str) -> bool:
        return self.features.get(key, True)

    @property
    def users(self) -> dict[int, dict]:
        if self._users is None:
            self._users = {u["id"]: u for u in all_(self.conn, "SELECT id, name, role, company, active FROM users WHERE project_id=?", (self.pid,))}
        return self._users

    def name(self, uid: int | None) -> str:
        return (self.users.get(uid) or {}).get("name", "") if uid else ""


def item(source: str, key, module: str, title: str, reason: str, status: str, *, tone: str = "blue", due: str | None = None,
         overdue: bool = False, label: str = "去处理", route: str = "", quick: list | None = None, inline: str | None = None,
         finding: dict | None = None, thumb: int | None = None, trace: dict | None = None, detail: dict | None = None, ai: bool = False,
         links: list | None = None) -> dict:
    return {"id": f"{source}:{key}", "source": source, "module": module, "module_name": MODULE_NAME[module], "title": title[:160], "reason": reason[:240],
            "status": status, "tone": tone, "due": due, "overdue": bool(overdue), "action_label": label, "action_route": route,
            "quick": quick or [], "inline": inline, "finding": finding, "thumb": thumb, "trace": trace, "detail": detail, "ai": ai,
            "ai_tag": AI_TAG.get(source) if ai else None, "links": links or []}


def _base(source: str) -> str:
    """“另有 N 项”行的来源名带 _more 后缀，口径判断按原来源算。"""
    return source[:-5] if source.endswith("_more") else source


def _cap(rows: list[dict], source: str, total: int | None, noun: str, route: str, module: str) -> list[dict]:
    """同一来源超过上限时，只留前几项并追加一条“另有 N 项”（指向该来源的完整页面，不吞掉数据）。"""
    total = len(rows) if total is None else total
    out = rows[:PER_SOURCE]
    if total > len(out):
        out.append(item(source + "_more", "more", module, f"另有 {total - len(out)} 项{noun}", "以上按紧急程度列出前几项，其余请到对应页面查看", "更多", tone="gray",
                        label="查看全部", route=route))
        out[-1]["more_n"] = total - len(out) + 1   # 折叠掉的条数：首页指标按“列出的 + 折叠的”计，与台账同源（验收 I1）
    return out


def _fmin(f: dict) -> dict:
    """候选隐患给 CandidateBox 用的字段（只带处理所需，不带台账内部字段）。"""
    b = f.get("body") or {}
    return {"id": f["id"], "title": f["title"], "level": f["level"], "level_name": f["level_name"], "assignee_id": f["assignee_id"],
            "ref_type": f["ref_type"], "ref_id": f["ref_id"], "body": {k: b.get(k) for k in ("category", "desc", "measure", "scene")}}


def _thumb(f: dict) -> int | None:
    return f["ref_id"] if f.get("ref_type") == "media" else None


def _module_of(agent: str) -> str:
    return {"safety": "safety", "quality": "quality"}.get(agent, "site")


# ============================================================================ 来源：候选隐患

def _default_assignees(c: Ctx, ids: list[int]) -> dict[int, int | None]:
    """候选的默认责任人：调用 T-05 提供的 findings.default_assignee(conn, f)（F-16 规则）；该函数还不存在或出错时为空，由人选择。"""
    fn = getattr(find_svc, "default_assignee", None)
    if not fn or not ids:
        return {}
    out: dict[int, int | None] = {}
    for f in all_(c.conn, f"SELECT * FROM findings WHERE id IN ({','.join('?' for _ in ids)})", ids):
        try:
            v = fn(c.conn, f)
            v = v.get("id") if isinstance(v, dict) else v
            out[f["id"]] = int(v) if v else None
        except Exception:  # noqa: BLE001 — 默认值算不出来不影响待办，由人选择
            log.exception("候选默认责任人计算失败：%s", f["id"])
    return out


def src_scan_failed(c: Ctx) -> list[dict]:
    """随手拍识别失败或超时的照片（验收 I10）：合成一条，写明原因与下一步，就地“重新识别”。"""
    from . import quick as quick_svc
    rows = quick_svc.scan_problems(c.conn, c.user)
    if not rows:
        return []
    failed = sum(1 for r in rows if r["safety_status"] == "failed")
    why = "；".join(x for x in (f"{failed} 张识别失败" if failed else "", f"{len(rows) - failed} 张识别超过 {quick_svc.SCAN_STUCK_MIN} 分钟仍无结果" if len(rows) > failed else "") if x)
    return [item("scan_failed", "all", "safety", f"{len(rows)} 张现场照片没有识别出结果", why + "。照片已保存，可重新识别；仍失败请稍后再试或联系系统管理员",
                 "识别失败", tone="orange", label="重新识别",
                 quick=[{"label": "重新识别", "primary": True, "api": {"method": "POST", "url": "/api/quick/media-rescan", "body": {"ids": [r["id"] for r in rows]}},
                         "done": "已重新开始识别，结果出来后在这里和消息里查看"}])]


def src_candidates(c: Ctx) -> list[dict]:
    """T04 的 AI 候选隐患：本单位安全员看本单位的、安全主管看全部；技术员、工程主管不能决定，项目经理退出候选确认（D7，权限保留），都不出条目。
    同一张照片的候选带同一个 group_key（media:<id>），前端有 CandidateGroup 时按照片成组一次处理（T-05 契约）。"""
    if c.role == "sub_safety":
        ids = role_views.scope_ids(c.conn, c.user) or [c.uid]
        rows = find_svc.list_for(c.conn, c.pid, agent="safety", candidates="only", assignee_ids=ids, limit=60)
    elif c.role == "safety_sup":
        rows = find_svc.list_for(c.conn, c.pid, agent="safety", candidates="only", limit=60)
    else:
        return []
    defaults = _default_assignees(c, [f["id"] for f in rows[:PER_SOURCE]])
    out = []
    for f in rows:
        c._cand_ids.add(f["id"])
        src = "（扫码上报）" if (f["body"] or {}).get("source") == "扫码" else ""
        it = item("candidate", f["id"], "safety", f["title"], f"AI 在照片中识别出隐患{src}；确认并派单后才计入台账、通知整改人",
                  find_svc.CANDIDATE_NAME, tone="orange" if f["level"] != "high" else "red", label="确认处理", route=f"finding?id={f['id']}",
                  inline="candidate", finding=_fmin(f), thumb=_thumb(f), trace={"type": "finding", "id": f["id"]}, ai=True)
        it["group_key"] = f"{f['ref_type']}:{f['ref_id']}" if f.get("ref_type") and f.get("ref_id") else f"finding:{f['id']}"
        it["default_assignee_id"] = defaults.get(f["id"])
        out.append(it)
    return _cap(out, "candidate", len(rows), "待确认隐患", "safety", "safety")


# ============================================================================ 来源：隐患/质控/质量问题

def _verify_text(f: dict) -> str:
    v = f.get("verify") or {}
    return v.get("reason") or ""


PENDING_STALE_TEXT = "AI 复核未开始，可人工闭环或退回"


def pending_stale(f: dict, now: datetime | None = None) -> bool:
    """整改已提交、AI 复核还停在 pending（后台任务没跑起来，如进程重启）超过 10 分钟（T-16）。
    pending 没有开始时间：按整改提交时写入的 updated_at 算（其后别的改动会把它推后，只会更晚判为“未开始”，不会提前）；时间读不出来时不判。"""
    v = f.get("verify") or {}
    if f.get("status") != "rectifying" or v.get("state") != "pending":
        return False
    try:
        t = datetime.fromisoformat(str(f.get("updated_at") or ""))
    except ValueError:
        return False
    t = t if t.tzinfo else t.astimezone()
    return (now or datetime.now().astimezone()) - t >= timedelta(minutes=site_agents.VERIFY_STALE_MIN)


def verify_text(f: dict) -> tuple[str, bool]:
    """整改复核文案（N-2）：沿用 T-13 的 site_agents.verify_label 口径——AI 判断只写成建议，隐患闭环前不出现“已消除”这类肯定说法。
    返回（文案, 是否可“重新让 AI 复核”）：与 /api/findings/{id}/reverify 同一条件，只在 AI 复核失败或卡在复核中超过 10 分钟时。
    pending 超过 10 分钟不再写“AI 复核中”，改写“AI 复核未开始，可人工闭环或退回”；后端“重新让 AI 复核”规则不变，仍不开放（T-16）。"""
    v = f.get("verify") or {}
    text = PENDING_STALE_TEXT if pending_stale(f) else site_agents.verify_label(v, f.get("status")) or "待人工复核"
    return text, f.get("status") == "rectifying" and (v.get("state") == "failed" or site_agents.verify_stale(v))


def closed_by_name(c: Ctx, f: dict) -> str | None:
    """闭环人：隐患已闭环（resolved）时取确认闭环的人（findings.feedback_by，由 set_status 写入），拿不到就为空，前端只写“已闭环”。"""
    if f.get("status") != "resolved":
        return None
    uid = f.get("feedback_by")
    if uid is None:
        row = one(c.conn, "SELECT feedback_by FROM findings WHERE id=?", (f["id"],))
        uid = row["feedback_by"] if row else None
    return c.name(uid) or None


def _rectify_item(c: Ctx, f: dict) -> dict:
    """派给本人/本单位的整改：待整改 → 就地拍整改照片；复核未通过 → 带上复核说明；被安全主管退回 → 写明“被退回”和退回原因（D-2）。
    分包安全员看的是本单位的整改，写明责任人（U-03）。"""
    rejected = f["status"] == "rejected"
    returned = bool(f.get("returned"))
    if returned:
        reason = f"被退回重改，原因：{f.get('feedback_note') or '未写明'}；请重新整改并拍整改照片"
    else:
        reason = (f"整改复核未通过：{_verify_text(f)}" if rejected and _verify_text(f) else f"{f['level_name']}风险隐患，请整改并拍整改照片")
    meas = (f["body"] or {}).get("measure")
    if meas and not rejected:
        reason += f"；措施：{meas}"
    if c.role != "tech":
        reason = f"责任人 {f['assignee'] or '未指派'}；{reason}"
    return item("rectify", f["id"], "safety", f["title"], reason, "被退回" if returned else f["status_name"], tone="red" if (f["overdue"] or f["level"] == "high") else "orange",
                due=f["due_date"], overdue=f["overdue"], label="拍整改照片", route=f"finding?id={f['id']}", inline="rectify", thumb=_thumb(f),
                finding={"id": f["id"]}, trace={"type": "finding", "id": f["id"]})


def src_my_findings(c: Ctx) -> list[dict]:
    """技术员：派给自己的整改与质控；分包安全员：本单位待整改（整改照片已提交、待港湾复核的不算我要办的）。"""
    if c.role == "tech":
        rows = find_svc.list_for(c.conn, c.pid, status="active", assignee_id=c.uid, limit=60)
    elif c.role == "sub_safety":
        ids = role_views.scope_ids(c.conn, c.user) or [c.uid]
        rows = find_svc.list_for(c.conn, c.pid, agent="safety", status="active", assignee_ids=ids, limit=60)
        rows = [f for f in rows if f["status"] in ("open", "rejected")]
    else:
        return []
    rows.sort(key=lambda f: (0 if f["overdue"] else 1, f["due_date"] or "9999", {"high": 0, "medium": 1, "low": 2}.get(f["level"], 3)))
    out = []
    for f in rows:
        if f["agent"] == "safety" and f["status"] in ("open", "rejected"):
            out.append(_rectify_item(c, f))
        elif f["kind"] == "qc_reminder":
            out.append(item("qc", f["id"], "quality", f["title"], "开工前质控点：请到场执行并勾选", f["status_name"], tone="red" if f["overdue"] else "blue",
                            due=f["due_date"], overdue=f["overdue"], label="去执行", route=f"finding?id={f['id']}", trace=None))
        else:
            out.append(item("my_finding", f["id"], _module_of(f["agent"]), f["title"], f"派给你的{f['agent_name']}事项（{f['level_name']}）", f["status_name"],
                            tone="red" if f["overdue"] else "orange" if f["level"] == "high" else "blue", due=f["due_date"], overdue=f["overdue"],
                            route=f"finding?id={f['id']}", thumb=_thumb(f), trace={"type": "finding", "id": f["id"]}))
    return _cap(out, "my_finding", len(out), "待办事项", "tasks" if c.role == "tech" else "ledger", "safety")


def _closing_quick(f_id: int) -> list[dict]:
    return [{"label": "确认闭环", "primary": True, "api": {"method": "POST", "url": f"/api/findings/{f_id}/status", "body": {"status": "resolved", "note": ""}}, "done": "已确认闭环"},
            {"label": "退回重改", "api": {"method": "POST", "url": f"/api/findings/{f_id}/status", "body": {"status": "open"}},
             "prompt": {"text": "退回原因（将通知责任人）：", "field": "note", "required": True}, "done": "已退回重改"}]


def _review_item(c: Ctx, f: dict) -> dict:
    """整改待复核：AI 结论只作建议（N-2），闭环和退回始终由人点；复核失败或卡住时可重新让 AI 复核（只限安全主管、项目经理）。"""
    v = f.get("verify") or {}
    text, stuck = verify_text(f)
    why = site_agents.paren(v.get("reason")) if text.startswith("AI 建议") else ""           # mock 理由自带括号时不再套一层（D-5）
    photo = site_agents.PHOTO_CHECK_NAME.get(v.get("photo_check") or "")
    reason = f"整改照片已提交；{text}{why}" + ("" if "待安全主管确认" in text or text == PENDING_STALE_TEXT else "；需安全主管确认闭环") + (f"；注意：{photo}" if photo else "")
    return item("review", f["id"], "safety", f["title"], reason,
                "整改待复核", tone="orange", label="看前后照片", route=f"finding?id={f['id']}", quick=_closing_quick(f["id"]) if R.can(c.role, "site_safety") else [],
                inline="review", finding={"id": f["id"]}, thumb=_thumb(f), trace={"type": "finding", "id": f["id"]},
                detail={"before": _thumb(f), "after": f.get("rectify_media_id"), "verify": v or None, "verify_text": text,
                        "reverify": stuck and c.role in site_agents.REVERIFY_ROLES, "photo_check": photo, "assignee": f.get("assignee"),
                        "status": f.get("status"), "closed_by_name": closed_by_name(c, f)}, ai=True)


def src_safety_board(c: Ctx) -> list[dict]:
    """安全主管：整改待复核 + 超期督办 + 未指派的高风险隐患。"""
    if c.role != "safety_sup":
        return []
    rows = find_svc.list_for(c.conn, c.pid, agent="safety", status="active", limit=200)
    out, ids = [], set()
    for f in rows:
        if f["status"] == "rectifying":
            out.append(_review_item(c, f)); ids.add(f["id"])
    for f in sorted((x for x in rows if x["overdue"]), key=lambda x: x["due_date"] or ""):
        days = (c.today - date.fromisoformat(f["due_date"])).days
        quick = [{"label": "催办", "api": {"method": "POST", "url": f"/api/findings/{f['id']}/nudge", "body": {"text": ""}}, "done": "已催办，已通知对方"}] if f["assignee_id"] and R.can(c.role, "nudge") else []
        out.append(item("overdue", f["id"], "safety", f["title"], f"{f['assignee'] or '未指派'}已超期 {days} 天未整改（期限 {f['due_date']}）；可催办，或到详情改派",
                        f"超期 {days} 天", tone="red", due=f["due_date"], overdue=True, label="详情·改派", route=f"finding?id={f['id']}", quick=quick, thumb=_thumb(f),
                        trace={"type": "finding", "id": f["id"]})); ids.add(f["id"])
    for f in rows:
        if f["id"] not in ids and f["level"] == "high" and not f["assignee_id"] and f["status"] in ("open", "rejected"):
            out.append(item("unassigned", f["id"], "safety", f["title"], "高风险隐患还没有责任人，请派单", "未指派", tone="red", due=f["due_date"], label="去派单",
                            route=f"finding?id={f['id']}", thumb=_thumb(f), trace={"type": "finding", "id": f["id"]}))
    return _cap(out, "safety_board", len(out), "安全待办", "safety", "safety")


# ============================================================================ 来源：工程主管的预警确认；项目经理的赶工决定与升级隐患；质量问题

def _alert_quick(c: Ctx, a_id: int) -> list[dict]:
    if not R.can(c.role, "progress_ops"):
        return []
    return [{"label": "确认属实", "primary": True, "api": {"method": "POST", "url": f"/api/alerts/{a_id}/feedback", "body": {"status": "confirmed", "note": ""}}, "done": "已确认属实"},
            {"label": "误报", "api": {"method": "POST", "url": f"/api/alerts/{a_id}/feedback", "body": {"status": "false_alarm"}},
             "prompt": {"text": "误报原因：", "field": "note", "required": True}, "done": "已标记误报"}]


def src_decisions(c: Ctx) -> list[dict]:
    """工程主管：红/橙进度预警判“确认属实 / 误报”。项目经理不再做这项反馈（D7），改为对已确认的红色预警做赶工决定（src_crash）。"""
    if c.role != "eng_sup":
        return []
    out = []
    for q in brief_svc.decision_queue(c.conn, c.pid, role=c.role):
        if q["type"] == "alert":
            out.append(item("alert", q["id"], "progress", q["title"], q["detail"] or "进度预警，请核实并决定是否组织赶工", "红色预警" if q["level"] == "high" else "橙色预警",
                            tone="red" if q["level"] == "high" else "orange", label="查看处置", route=q["link"], quick=_alert_quick(c, q["id"]), inline="alert",
                            trace={"type": "alert", "id": q["id"]}, ai=True))
    return _cap(out, "decision", len(out), "待决策事项", "progress?tab=alerts", "progress")


# ---------------------------------------------------------------------------- 项目经理：赶工决定（D7 ①，AC-6.6）

ALERT_LEVEL_RANK = {"yellow": 0, "orange": 1, "red": 2}
ALERT_LEVEL_NAME = {"yellow": "黄色", "orange": "橙色", "red": "红色"}
CRASH_NAME = {"adopt": "采纳赶工方案", "defer": "暂不处理"}
DEFER_SLIP_DAYS = 3.0    # “暂不处理”之后滞后天数比决定时多 3 天及以上，重新请项目经理决定（N-7）
DEFER_DAYS = 7           # “暂不处理”满 7 天，重新请项目经理决定（N-7）


class TodoError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _slip(a: dict) -> float | None:
    v = (loads(a["detail_json"], {}) or {}).get("slip_days")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def crash_plan(conn, project_id: int) -> dict | None:
    """当前赶工方案：进度Agent 生成、系统按 CPM 复算的项目级方案（knowledge: recovery/latest，完整内容在预测页）。没有就为空。"""
    row = one(conn, "SELECT value_json, updated_at FROM knowledge WHERE project_id=? AND kind='recovery' AND key='latest'", (project_id,))
    if not row:
        return None
    v = loads(row["value_json"], {}) or {}
    return {"at": row["updated_at"], "diagnosis": str(v.get("diagnosis") or ""), "measures": len((v.get("verified") or {}).get("measures") or [])}


def crash_reopen(a: dict, dec: dict | None, today: date) -> str | None:
    """上次赶工决定是否已失效（N-7）。返回：没有决定 → ""；决定仍有效 → None；已失效 → 重新提请的原因。
    两种决定：预警等级升高、预警被关闭后重开并重新确认属实，都要重新决定；“暂不处理”另有滞后再多 3 天及以上、满 7 天两条。"""
    if not dec:
        return ""
    if ALERT_LEVEL_RANK.get(a["level"], 0) > ALERT_LEVEL_RANK.get(dec["level_at"], 0):
        return f"预警由{ALERT_LEVEL_NAME.get(dec['level_at'], dec['level_at'])}升为{ALERT_LEVEL_NAME.get(a['level'], a['level'])}"
    if (a["feedback_at"] or "") != (dec["feedback_at"] or ""):
        return f"预警已重新确认属实（{(a['feedback_at'] or '')[5:10]}）"
    if dec["decision"] != "defer":
        return None
    slip = _slip(a)
    if slip is not None and dec["slip_at"] is not None and slip - dec["slip_at"] >= DEFER_SLIP_DAYS:
        return f"滞后由 {dec['slip_at']:g} 天增至 {slip:g} 天（多 {slip - dec['slip_at']:g} 天）"
    days = (today - date.fromisoformat(dec["decided_at"][:10])).days
    if days >= DEFER_DAYS:
        return f"距上次决定已满 {days} 天"
    return None


def src_crash(c: Ctx) -> list[dict]:
    """项目经理：工程主管已确认属实的红色预警，要不要赶工（直接查 confirmed 且 red 且非历史数据缺口、尚无有效决定的预警，不动 brief.py，G-04）。
    就地两个选项“采纳赶工方案 / 暂不处理”，只留痕、记审计，不派单；还没有赶工方案时先就地生成（既有接口 POST /api/schedule/recovery）。"""
    if c.role != "manager":
        return []
    plan = crash_plan(c.conn, c.pid)
    out = []
    for a in all_(c.conn, "SELECT * FROM alerts WHERE project_id=? AND status='confirmed' AND level='red' AND rule<>'stale' ORDER BY id DESC LIMIT 30", (c.pid,)):
        dec = one(c.conn, "SELECT * FROM alert_decisions WHERE project_id=? AND alert_id=? ORDER BY id DESC LIMIT 1", (c.pid, a["id"]))
        why = crash_reopen(a, dec, c.today)
        if why is None:
            continue
        api = lambda d: {"method": "POST", "url": f"/api/todo/alerts/{a['id']}/crash-decision", "body": {"decision": d}}  # noqa: E731
        defer = {"label": CRASH_NAME["defer"], "api": api("defer"), "confirm": f"暂不处理？滞后再多 {DEFER_SLIP_DAYS:g} 天、预警升级或满 {DEFER_DAYS} 天会再次提请。",
                 "done": "已记录：暂不处理"}
        if plan:
            quick = [{"label": CRASH_NAME["adopt"], "primary": True, "api": api("adopt"), "confirm": "采纳当前赶工方案？只记录决定，不自动派单。", "done": "已记录：采纳赶工方案"}, defer]
            plan_text = f"赶工方案（{plan['at'][5:10]} 生成）：" + (plan["diagnosis"][:60] or f"{plan['measures']} 项措施")
        else:
            quick = [{"label": "生成赶工方案", "primary": True, "api": {"method": "POST", "url": "/api/schedule/recovery"}, "done": "赶工方案已生成，请查看后决定"}, defer]
            plan_text = "尚无赶工方案，可先生成（进度Agent 提出措施，系统按 CPM 复算工期效果）"
        reason = facts_svc.alert_judgement(c.conn, a) + (f"；重新提请：{why}" if why else "") + f"；{plan_text}"
        status = f"上次{'暂不处理' if dec['decision'] == 'defer' else '已采纳赶工方案'}（{dec['decided_at'][5:10]}）" if dec else "已确认红色预警"
        out.append(item("crash", a["id"], "progress", a["title"], reason, status, tone="red", label="看预警", route=f"alert?id={a['id']}", quick=quick,
                        trace={"type": "alert", "id": a["id"]}, ai=True, links=[{"label": "查看赶工方案", "route": "progress?tab=forecast"}] if plan else None))
    return _cap(out, "crash", len(out), "条已确认红色预警", "progress?tab=alerts", "progress")


def crash_decide(conn, user: dict, alert_id: int, decision: str, note: str = "") -> dict:
    """记录项目经理的赶工决定：只留痕、记审计，不派单、不发通知、不改预警状态。失效后由 src_crash 重新提请（N-7）。"""
    if user["role"] != "manager":
        raise TodoError(403, "赶工决定由项目经理做")
    if decision not in CRASH_NAME:
        raise TodoError(400, "决定只能是“采纳赶工方案”或“暂不处理”")
    a = one(conn, "SELECT * FROM alerts WHERE id=? AND project_id=?", (alert_id, user["project_id"]))
    if not a or a["rule"] == "stale":
        raise TodoError(404, "预警不存在")
    if a["status"] != "confirmed" or a["level"] != "red":
        raise TodoError(409, "只有工程主管已确认属实的红色预警需要做赶工决定")
    plan = crash_plan(conn, user["project_id"])
    if decision == "adopt" and not plan:
        raise TodoError(409, "尚无赶工方案，请先生成赶工方案")
    at = now_iso()
    with tx(conn):
        insert(conn, "alert_decisions", {"project_id": user["project_id"], "alert_id": alert_id, "decision": decision, "level_at": a["level"], "slip_at": _slip(a),
                                         "feedback_at": a["feedback_at"], "plan_at": plan["at"] if plan else None, "note": (note or "").strip()[:300],
                                         "decided_by": user["id"], "decided_at": at})
        audit(conn, user["id"], "alert.crash", f"alert:{alert_id}", (CRASH_NAME[decision] + (f"：{note.strip()}" if (note or "").strip() else ""))[:200])
    return {"ok": True, "alert_id": alert_id, "decision": decision, "decision_name": CRASH_NAME[decision], "decided_at": at}


# ---------------------------------------------------------------------------- 项目经理：升级到经理的高风险隐患（D7 ②）

def _escalation(c: Ctx) -> tuple[list[dict], list[dict]]:
    """未闭环的高风险安全隐患与质量问题（台账口径），分成（已升级到项目经理的, 其余的）。口径写死在这里（G-04）：
    催办链阶段 4 已发给项目经理（nudge_log status='sent'；只认领未发出的 claimed 不算，T-01）且尚未整改、或已超期的高风险，算“已升级”；
    整改照片已提交、待安全主管复核的不是项目经理要拍板的事，两边都不列（升级过的）或只计入“需要知道”（没升级的）。"""
    if c._esc is None:
        sent = {str(r["obj_key"]) for r in all_(c.conn, "SELECT DISTINCT obj_key FROM nudge_log WHERE project_id=? AND obj_type='finding' AND stage=4 AND status='sent'", (c.pid,))}
        high = [f for f in find_svc.list_for(c.conn, c.pid, status="active", limit=300)
                if f["agent"] in ("safety", "quality") and f["kind"] != "qc_reminder" and f["level"] == "high"]
        up = [f for f in high if f["status"] in ("open", "rejected") and (str(f["id"]) in sent or f["overdue"])]
        ids = {f["id"] for f in up}
        c._esc = (up, [f for f in high if f["id"] not in ids and str(f["id"]) not in sent])
    return c._esc


def src_escalated(c: Ctx) -> list[dict]:
    """项目经理：升级到经理的高风险隐患（含高风险质量问题），带期限与责任人；办理在详情页（催办、改派等既有操作）。"""
    if c.role != "manager":
        return []
    up, _ = _escalation(c)
    out = []
    # 责任人所属分包单位：项目经理首页按单位归成“分包领导层面的协调事项”（只是归类展示，不生成新任务、不派单）
    units = {r["id"]: (r["company"] or "").strip() for r in all_(c.conn, "SELECT id, company FROM users WHERE project_id=?", (c.pid,))}
    # 催办链阶段 4 已实际发给项目经理的（确有提请）；只是超期、没走到提请的，经理首页列为“需关注”
    asked = {str(r["obj_key"]) for r in all_(c.conn, "SELECT DISTINCT obj_key FROM nudge_log WHERE project_id=? AND obj_type='finding' AND stage=4 AND status='sent'", (c.pid,))}
    for q in sorted(up, key=lambda f: (0 if f["overdue"] else 1, f["due_date"] or "9999")):
        days = (c.today - date.fromisoformat(q["due_date"][:10])).days if q["overdue"] and q["due_date"] else 0
        why = f"高风险且已超期 {days} 天" if q["overdue"] else "逐级催办后仍未处理，已升级到项目经理"
        meas = re.sub(r"^建议[：:，,\s]*", "", str((q["body"] or {}).get("measure") or "").strip())       # 措施本身以“建议”开头时不再叠一层（U-13②）
        out.append(item("escalated", q["id"], _module_of(q["agent"]), q["title"], f"{why}；责任人 {q['assignee'] or '未指派'}" + (f"；措施：{meas}" if meas else ""),
                        f"超期 {days} 天" if q["overdue"] else "已升级", tone="red", due=q["due_date"], overdue=q["overdue"], label="查看处置",
                        route=f"finding?id={q['id']}", finding={"id": q["id"]}, thumb=_thumb(q), trace={"type": "finding", "id": q["id"]},
                        ai=q["agent"] in ("safety", "quality") and q.get("kind") != "reported"))
        out[-1]["company"] = units.get(q.get("assignee_id"), "")
        out[-1]["requested"] = str(q["id"]) in asked
    return _cap(out, "escalated", len(out), "项升级到项目经理的高风险问题", "safety", "safety")


def _know(c: Ctx) -> dict | None:
    """项目经理“需要知道：N 项”（AC-6.2）：不属于三类拍板项、但该知道的事，只给数字和链接，完整列表在预警页、安全页、质量页。
    口径：待工程主管确认的红、橙预警；未升级到经理的高风险安全隐患与质量问题。计数前先按 PM_FORBIDDEN 过滤，文字过 scope_ok，不出现人名。"""
    if c.role != "manager":
        return None
    _, rest = _escalation(c)
    a = brief_svc.counts(c.conn, c.pid, c.today)["alerts"]       # 与简报“红 x 橙 y”、概览“待确认红橙预警”同一来源（D-1）
    alerts = a["red"] + a["orange"]
    # 各项带筛选进入（验收 I4）：预警看“待确认”，隐患/质量看“高风险”
    parts = [{"source": "know_alert", "label": "待工程主管确认的红橙预警", "n": alerts, "route": "progress?tab=alerts&status=open"},
             {"source": "know_safety", "label": "未升级的高风险隐患", "n": sum(1 for f in rest if f["agent"] == "safety"), "route": "safety?only=high"},
             {"source": "know_quality", "label": "未升级的高风险质量问题", "n": sum(1 for f in rest if f["agent"] == "quality"), "route": "quality?only=high"}]
    parts = [p for p in parts if p["n"] and p["source"] not in PM_FORBIDDEN and brief_svc.scope_ok(p["label"])]
    return {"n": sum(p["n"] for p in parts), "parts": parts}


def src_quality(c: Ctx) -> list[dict]:
    """工程主管：未闭环的质量问题（质控提醒是给技术员的，不在此列）。"""
    if c.role != "eng_sup":
        return []
    rows = [f for f in find_svc.list_for(c.conn, c.pid, agent="quality", status="active", limit=100) if f["kind"] != "qc_reminder"]
    rows.sort(key=lambda f: (0 if f["overdue"] else 1, f["due_date"] or "9999"))
    out = [item("quality", f["id"], "quality", f["title"], f"{f['assignee'] or '未指派'}负责的质量问题（{f['level_name']}）", f["status_name"],
                tone="red" if f["overdue"] else "orange" if f["level"] == "high" else "blue", due=f["due_date"], overdue=f["overdue"],
                route=f"finding?id={f['id']}", trace={"type": "finding", "id": f["id"]}) for f in rows]
    return _cap(out, "quality", len(out), "质量问题", "quality", "quality")


# ============================================================================ 来源：日志预审 / 缺报 / 周确认

def _week_start(day: date) -> str:
    return (day - timedelta(days=day.weekday())).isoformat()


def src_log_review(c: Ctx) -> list[dict]:
    """工程主管：分包日志预审（只列红/橙与未预审，低风险合成一条“一键通过”）。
    周确认模式下预审的是周日志（数据审核与审核操作都在周日志上），日日志不再列出，避免“去预审”落到没有审核操作的日日志（F-02）。"""
    if c.role != "eng_sup" or not R.can(c.role, "log_review"):
        return []
    period = "week" if log_svc.weekly_mode(c.conn, c.pid) else "day"
    rows = all_(c.conn, """SELECT l.id, l.log_date, l.author_id, l.dq_json, l.checks_json, u.name author, u.company FROM logs l JOIN users u ON u.id=l.author_id
                           WHERE l.project_id=? AND l.status='submitted' AND l.mode='ai' AND l.period=? ORDER BY l.log_date DESC, l.id DESC LIMIT 80""", (c.pid, period))
    what = "起一周进度确认" if period == "week" else "日志"
    rk = {"high": 0, "medium": 1, "none": 2}
    show, low_ids = [], []
    for l in rows:
        dq = loads(l["dq_json"])
        risk = dq["risk"] if dq else "none"
        if risk == "low":
            low_ids.append(l["id"]); continue
        flags = sum(1 for x in loads(l["checks_json"], []) if x["level"] in ("error", "warn"))
        show.append((rk.get(risk, 3), l, dq, risk, flags))
    show.sort(key=lambda x: x[0])
    out = []
    for _, l, dq, risk, flags in show:
        name = {"high": "高风险", "medium": "待核实", "none": "未预审"}[risk]
        summary, by_ai = site_agents.dq_summary(dq)
        out.append(item("log_review", l["id"], "site", f"{l['author']}（{l['company'] or '未标注单位'}）{l['log_date'][5:]} {what}", summary + (f"；{flags} 处提示" if flags else ""),
                        name, tone="red" if risk == "high" else "orange" if risk == "medium" else "gray", label="去预审", route=f"log?id={l['id']}", ai=by_ai))
    out = _cap(out, "log_review", len(show), "份日志待预审", "logs", "site")
    if low_ids:
        out.append(item("log_low", "all", "site", f"低风险日志 {len(low_ids)} 份可一键通过", "数据审核 Agent 已逐份预审，这些日志没有发现问题", "低风险", tone="green",
                        label="一键通过", quick=[{"label": f"通过 {len(low_ids)} 份", "primary": True, "api": {"method": "POST", "url": "/api/logs/batch-approve", "body": {"ids": low_ids[:100]}},
                                                "confirm": f"通过 {len(low_ids)} 份低风险日志？", "done": "已通过低风险日志"}], route="logs", ai=True))
    return out


def src_week_missing(c: Ctx) -> list[dict]:
    """工程主管：上周周确认未提交的技术员，一键提醒直达那一周的周确认（F-08；止于工程主管，不进项目经理）。
    “今日无记录”已删除（C12）：每天都出、又不需要工程主管拍板，日志缺报交给催办链。"""
    if c.role != "eng_sup" or not R.can(c.role, "nudge"):
        return []
    cmap = {u["id"]: (u["company"] or "未标注单位") for u in c.users.values()}
    lws = (c.today - timedelta(days=c.today.weekday() + 7)).isoformat()
    unconf = all_(c.conn, """SELECT u.id, u.name FROM users u WHERE u.project_id=? AND u.role='tech' AND u.active=1 AND NOT EXISTS
                             (SELECT 1 FROM logs l WHERE l.author_id=u.id AND l.period='week' AND l.log_date=? AND l.status IN ('submitted','approved'))""", (c.pid, lws))

    def remind(uid: int) -> list[dict]:
        return [{"label": "提醒", "primary": True, "api": {"method": "POST", "url": "/api/todo/remind-week", "body": {"user_id": uid, "week": lws}}, "done": "已发提醒"}]
    b = [item("week_missing", u["id"], "site", f"{u['name']}（{cmap.get(u['id'], '未标注单位')}）上周进度未确认", "上周进度确认还没有提交，进度回填 P6 与预警会缺这部分",
              "上周未确认", tone="orange", due=(date.fromisoformat(lws) + timedelta(days=6)).isoformat(), overdue=True, label="提醒",
              quick=remind(u["id"]), route="logs") for u in unconf]
    return _cap(b, "week_missing", len(b), "人上周进度未确认", "logs", "site")


def remind_week(conn, user: dict, target_id: int, week: str) -> dict:
    """工程主管提醒技术员补交某一周的进度确认：消息直达那一周的周确认（不再落到首页随手记，F-08），写审计。"""
    if user["role"] != "eng_sup":
        raise TodoError(403, "周进度确认提醒由工程主管发")
    try:
        d = date.fromisoformat(str(week or "")[:10])
    except ValueError:
        raise TodoError(400, "周的日期格式应为 YYYY-MM-DD") from None
    ws = d - timedelta(days=d.weekday())
    t = one(conn, "SELECT * FROM users WHERE id=? AND project_id=? AND active=1 AND role='tech'", (target_id, user["project_id"]))
    if not t:
        raise TodoError(400, "只能提醒本项目在岗的技术员")
    if one(conn, "SELECT 1 x FROM logs WHERE project_id=? AND author_id=? AND period='week' AND log_date=? AND status IN ('submitted','approved')",
           (user["project_id"], t["id"], ws.isoformat())):
        raise TodoError(409, "这一周的进度确认已经提交，不用再提醒")
    role_views._notify(conn, user["project_id"], t["id"], "周进度确认提醒",
                       f"{user['name']}（{R.role_name(user['role'])}）提醒：{ws:%m-%d} 起一周的进度确认还没有提交，请打开确认；提交后更新进度计划。",
                       {"route": "week", "params": {"date": ws.isoformat()}})
    audit(conn, user["id"], "user.nudge", f"user:{t['id']}", f"周进度确认 {ws.isoformat()}")
    return {"ok": True, "user_id": t["id"], "week": ws.isoformat()}


def src_tech_week(c: Ctx) -> list[dict]:
    """技术员：往期周确认被项目部退回、或还没提交的，每一周一条，直达那一周（F-08，不只看上一周）；本周被退回的也列出。
    周确认模式下上周连周日志都没有也算未提交（账号是那一周之后才开的不追）。本周的进度确认就在首页“本周进度确认”卡上，不在此重复。"""
    if c.role != "tech":
        return []
    ws = _week_start(c.today)
    pws = (date.fromisoformat(ws) - timedelta(days=7)).isoformat()
    rows = all_(c.conn, """SELECT log_date, status, review_note FROM logs WHERE project_id=? AND author_id=? AND period='week' AND mode='ai' AND log_date<=?
                           AND status IN ('draft','returned') ORDER BY log_date DESC LIMIT 30""", (c.pid, c.uid, ws))
    out = []
    for r in rows:
        cur = r["log_date"] == ws
        if cur and r["status"] != "returned":
            continue
        when = "本周" if cur else "上周" if r["log_date"] == pws else f"{r['log_date'][5:]} 起一周"
        due = (date.fromisoformat(r["log_date"]) + timedelta(days=6)).isoformat()
        if r["status"] == "returned":
            out.append(item("week", r["log_date"], "site", f"{when}进度确认被退回", f"项目部退回意见：{r['review_note'] or '未写明'}；修改后重新提交", "被退回", tone="red",
                            due=due, overdue=not cur, label="去修改", route=f"week?date={r['log_date']}"))
        else:
            out.append(item("week", r["log_date"], "site", f"{when}进度确认", f"{when}进度还没有提交确认；提交后更新进度计划", "上周未确认" if when == "上周" else "未确认",
                            tone="red", due=due, overdue=True, label="去确认", route=f"week?date={r['log_date']}"))
    if log_svc.weekly_mode(c.conn, c.pid) and not one(c.conn, "SELECT 1 x FROM logs WHERE project_id=? AND author_id=? AND period='week' AND log_date=?", (c.pid, c.uid, pws)):
        me = one(c.conn, "SELECT created_at FROM users WHERE id=?", (c.uid,))
        end = (date.fromisoformat(pws) + timedelta(days=6)).isoformat()
        if (me["created_at"] or "")[:10] <= end:
            out.append(item("week", pws, "site", "上周进度确认", "上周进度还没有提交确认；提交后更新进度计划", "上周未确认", tone="red", due=end, overdue=True,
                            label="去确认", route=f"week?date={pws}"))
    return _cap(sorted(out, key=lambda i: i["due"] or ""), "week", len(out), "周进度确认待处理", "week", "site")


# ============================================================================ 来源：进度侧（放行清单缺项、数据缺口、清单映射）

def src_gate(c: Ctx) -> list[dict]:
    """放行清单：未来 7 天将开工、条件未齐的作业（技术员只看自己负责范围；原先挂在 top:todo 的小卡从未渲染，现并入这里）。
    项目经理不再接收（D7：放行是执行层事项）。"""
    if c.role not in ("tech", "eng_sup"):
        return []
    from . import gate as gate_svc
    try:
        d = gate_svc.digest(c.conn, c.user, c.today)
    except PermissionError:
        return []
    out = []
    for i in d["items"]:
        dd = i["days_to_start"]
        when = "未排期" if dd is None else "今日开工" if dd == 0 else f"{dd} 天后开工" if dd > 0 else f"应开未开 {-dd} 天"
        due = (c.today + timedelta(days=dd)).isoformat() if dd is not None else None
        # 与放行清单页同一口径：只有受阻/系统核到确实缺才算“缺”，待落实写“待现场确认”（待落实不等于未做）
        gap = gate_svc.gap_text(i.get("short_labels") or [], i.get("short_n", 0), i.get("unknown_n", 0)) or "开工条件未齐"
        # 非关键线路且总浮时充足的作业，晚开一两天不算超期、不进“紧急待办”
        risky = gate_svc.late_is_risky(dd, bool(i["is_critical"]), i.get("total_float_d"))
        blocked = i["verdict"] == "blocked"
        status = "受阻" if blocked else "应开未开" if dd is not None and dd < 0 else "临近缺项" if i.get("short_n") else "待现场确认"
        out.append(item("gate", i["code"], "progress", f"{i['code']} {i['name']}" + ("（关键线路）" if i["is_critical"] else ""), f"{when}；{gap}", status,
                        tone="red" if blocked else "orange" if (risky or i.get("short_n")) else "blue", due=due, overdue=risky,
                        label="看放行条件", route=f"gate?open={i['code']}"))
    if d["count"] > len(out):
        out.append(item("gate_more", "more", "progress", f"另有 {d['count'] - len(out)} 项作业开工条件未齐", "按紧迫度列出前几项", "更多", tone="gray", label="查看全部", route="gate"))
    return out


def src_data_gap(c: Ctx) -> list[dict]:
    """T03 数据缺口：只给工程主管，“没报”不是“落后”；一键提醒责任技术员补报。"""
    if c.role != "eng_sup" or not p6.current_import(c.conn, c.pid):
        return []
    from . import progress as prog_svc
    rows = prog_svc.data_gaps(c.conn, c.pid, as_of=c.iso)
    out = []
    for g in rows:
        text = f"{g['code']} {g['name']} 已连续 {g['idle_days']} 天没有日报进度，请补记随手记或在本周确认中补上进度"
        quick = [{"label": "提醒补报", "primary": True, "api": [{"method": "POST", "url": f"/api/users/{o['id']}/nudge", "body": {"text": text}} for o in g["owners"]],
                  "done": f"已提醒 {len(g['owners'])} 人"}] if g["owners"] and R.can(c.role, "nudge") else []
        who = "、".join(o["name"] + (f"（{o['company']}）" if o["company"] else "") for o in g["owners"]) or "未按负责范围匹配到技术员，请人工联系"
        out.append(item("data_gap", g["code"], "progress", f"{g['code']} {g['name']}", f"已 {g['idle_days']} 天无进度；最后上报：{g['last_report'] or '数据日期后无上报'}；责任人：{who}。这是“没报”，不是“落后”",
                        "数据缺口", tone="orange", label="提醒补报", quick=quick, route="progress?tab=plan"))
    return _cap(out, "data_gap", len(rows), "处数据缺口", "progress?tab=plan", "progress")


def src_mapping(c: Ctx) -> list[dict]:
    """工程主管：AI 建议的清单映射待确认（涉及产值，项目经理不看）。"""
    if c.role != "eng_sup" or not R.can(c.role, "output_ops"):
        return []
    sug = one(c.conn, "SELECT COUNT(*) n FROM boq_map WHERE project_id=? AND status='suggested'", (c.pid,))["n"]
    if not sug:
        return []
    un = one(c.conn, """SELECT COUNT(DISTINCT lp.activity_code) n FROM log_progress lp WHERE lp.project_id=? AND lp.voided_at IS NULL AND NOT EXISTS
                        (SELECT 1 FROM boq_map m WHERE m.project_id=lp.project_id AND m.activity_code=lp.activity_code AND m.status='confirmed')""", (c.pid,))["n"]
    return [item("mapping", "all", "progress", f"清单映射待确认 {sug} 条", "AI 建议的 P6 作业与清单子目对应关系，确认后才计入内部产值统计（非计量认证）" + (f"；另有 {un} 个作业有进度但未映射" if un else ""),
                 "待确认", tone="blue", label="去确认", route="output", ai=True)]


# ============================================================================ 来源：质量侧（材料与砼、隐蔽验收）

def src_site_quality(c: Ctx) -> list[dict]:
    """材料与砼疑点、疑似缺验收资料、已报验待录入监理结果：工程主管（项目经理不再接收，D7）；
    技术员只提示自己范围内待确认的票据、被监理退回的验收记录（逐条带退回原因，F-04）与待补的草稿；
    工程主管本人编制、被监理退回的记录同样逐条带退回原因（T-16）。"""
    out = []
    try:
        from . import conceal as conceal_svc
        from . import tickets as tickets_svc
        if c.role == "eng_sup":
            r = tickets_svc.risks(c.conn, c.user) if c.feat("tickets") else {"items": []}
            pri = [i for i in r["items"] if i["level"] != "low"][:3] or [i for i in r["items"]][:1]
            for i in pri:
                out.append(item("ticket_risk", f"{i['kind']}:{i.get('ref_id', '')}:{len(out)}", "quality", i["title"], (i["detail"] or "系统记录疑点，须人工核实")[:140],
                                tickets_svc.KIND_NAME.get(i["kind"], "疑点"), tone="red" if i["level"] == "high" else "orange", label="查看", route="tickets?tab=alerts", ai=True))
            g = conceal_svc.gaps(c.conn, c.user) if c.feat("conceal") else {"gaps": []}
            if g["gaps"]:
                out.append(item("conceal_gap", "all", "quality", f"{len(g['gaps'])} 项已开工作业疑似缺验收资料",
                                "P6 状态与系统记录比对：" + "；".join(f"{x['part']} {x['template_name']}" for x in g["gaps"][:3]) + (" 等" if len(g["gaps"]) > 3 else ""),
                                "疑似缺资料", tone="orange", label="查看缺项", route="conceal?tab=gaps", ai=True))
            sub = conceal_svc.list_records(c.conn, c.user, status="submitted")["records"] if c.feat("conceal") else []
            res = [item("conceal_result", x["id"], "quality", f"{x['part']} {x['template_name']}",
                        f"已报验（{(x['submitted_at'] or '')[:10] or '日期未记'}），待录入监理结果：监理验收后录入通过或退回（退回须写原因）；记录人 {x['created_by']}",
                        "待录入监理结果", tone="blue", due=x["inspection_date"], label="录入结果", route=f"conceal?id={x['id']}") for x in sub]
            out += _cap(res, "conceal_result", len(res), "份隐蔽验收待录入监理结果", "conceal", "quality")
            if c.feat("conceal"):
                # 工程主管本人编制、被监理退回的记录：同样逐条带退回原因（编制人负责补正；别人编制的由编制人自己的待办提醒）
                mine = {r["id"] for r in all_(c.conn, "SELECT id FROM conceal_records WHERE project_id=? AND status='rejected' AND created_by=?", (c.pid, c.uid))}
                out += _conceal_back([x for x in conceal_svc.list_records(c.conn, c.user, status="rejected")["records"] if x["id"] in mine])
        elif c.role == "tech":
            r = tickets_svc.risks(c.conn, c.user) if c.feat("tickets") else {"pending_tickets": 0}
            if r["pending_tickets"]:
                out.append(item("ticket_pending", "all", "quality", f"{r['pending_tickets']} 张票据待确认", "拍下的混凝土小票/进场单识别成草稿，确认后才计入累计", "待确认", tone="blue", label="去确认",
                                route="tickets", ai=True))
            l = conceal_svc.list_records(c.conn, c.user) if c.feat("conceal") else {"counts": {}, "records": []}
            out += _conceal_back([x for x in l["records"] if x["status"] == "rejected"])
            n = l["counts"].get("draft", 0)
            if n:
                out.append(item("conceal_todo", "all", "quality", f"{n} 份隐蔽验收记录待补", "验收记录草稿还缺照片或数据项，补齐后报验", "待补", tone="orange", label="去补齐", route="conceal"))
    except Exception:  # noqa: BLE001 — 材料/资料模块出问题不拖垮整张待办
        log.exception("待办来源失败：材料与资料")
    return out


def _conceal_back(back: list[dict]) -> list[dict]:
    """被监理退回的隐蔽验收记录（F-04）：逐条带退回原因、直达记录；超过上限的另起一行“另有 N 份被监理退回”，不并进草稿的“N 份待补”（T-16）。"""
    out = [item("conceal_todo", x["id"], "quality", f"{x['part']} {x['template_name']}", f"监理退回：{x['result_note'] or '未写明原因'}；按退回意见补正后重新报验",
                "监理退回", tone="red", label="去补正", route=f"conceal?id={x['id']}") for x in back[:PER_SOURCE]]
    if len(back) > PER_SOURCE:
        out.append(item("conceal_todo", "back_more", "quality", f"另有 {len(back) - PER_SOURCE} 份被监理退回", "以上逐条列出前几份及退回原因，其余请到隐蔽验收页查看（状态为“监理退回”）",
                        "监理退回", tone="red", label="查看全部", route="conceal"))
    return out


# ============================================================================ 来源：报表、例会行动项、扫码待认领

def src_reports(c: Ctx) -> list[dict]:
    """周/月报草稿：项目经理审核发布；工程主管审阅修改（草稿已存在才出条目）。"""
    if not (R.can(c.role, "report_publish") or R.can(c.role, "report_gen")) or c.role not in ("manager", "eng_sup"):
        return []
    rows = all_(c.conn, "SELECT id, kind, period_start, period_end FROM reports WHERE project_id=? AND status='draft' AND narrative_json IS NOT NULL ORDER BY id DESC LIMIT 5", (c.pid,))
    return [item("report", r["id"], "report", f"{'周报' if r['kind'] == 'week' else '月报'} {r['period_start']}～{r['period_end']}",
                 "草稿已由报表 Agent 起草，数字经系统校验；请审核后发布" if c.role == "manager" else "草稿已起草，请审阅修改后交项目经理审核发布", "待审核", tone="blue",
                 label="去审核" if c.role == "manager" else "去审阅", route=f"report?id={r['id']}", ai=True) for r in rows]


def src_claim_notice(c: Ctx) -> list[dict]:
    """索赔通知时效（工笔文书方案 2026-10-08）：待处理事件剩 7 天进工程主管待办，剩 3 天升级到项目经理；已过期的照常列出（标红），不自动关闭。
    只写事件、日期、受影响作业与剩余天数，不写金额；分包与其他岗位没有这条来源。"""
    from . import claim_notice as cn
    out = []
    for n in cn.due_items(c.conn, c.pid, c.role, c.today):
        ev = n["event"]
        acts = "、".join(ev["codes"][:4]) or "受影响作业待补"
        over = n["remain"] < 0
        when = f"已过期 {-n['remain']} 天" if over else f"剩 {n['remain']} 天"
        out.append(item("claim_notice", ev["key"], "progress", f"索赔通知时效{when}：{ev['cat_name']}（{ev['date_from']} 起）",
                        f"受影响作业：{acts}；通知期限 {n['deadline']}（{cn.CLAUSE}）。请决定发出通知函或登记不发原因；是否构成索赔以商务和合同部门判断为准",
                        "已过期" if over else "待决定", tone="red" if over or n["remain"] <= cn.PM_DAYS else "orange", due=n["deadline"], overdue=over,
                        label="去处理", route="claims"))
    return _cap(out, "claim_notice", len(out), "个事件待决定是否发通知函", "claims", "progress")


def src_letters(c: Ctx) -> list[dict]:
    """致分包文书（工笔文书方案 2026-10-08）：分包看“待签收 / 待回复”，工程主管看“进度类回复待接受”，签发人看“超 24 小时未签收”。"""
    from . import sub_letters as sl
    out = []
    for r in sl.pending_for(c.conn, c.user):
        d = one(c.conn, "SELECT title FROM gongbi_docs WHERE id=?", (r["doc_id"],)) or {}
        mod = {"safety": "safety", "quality": "quality"}.get(r["area"], "progress")
        name = "工作联系单" if r["kind"] == "work_contact" else "项目部函件"
        route = f"letters?id={r['id']}"
        if r["todo"] == "ack":
            out.append(item("letter", r["id"], mod, f"{name}待签收：{d.get('title', '')}", "项目部已下发，请打开查看并签收" + (f"；期限 {r['due_date']}" if r["due_date"] else ""),
                            "待签收", tone="red" if r["ack_late"] else "orange", due=r["due_date"], overdue=r["overdue"], label="去签收", route=route,
                            quick=[{"label": "签收", "primary": True, "api": {"method": "POST", "url": f"/api/gongbi/letters/{r['id']}/ack"}, "done": "已签收"}]))
        elif r["todo"] == "reply":
            out.append(item("letter", r["id"], mod, f"{name}待回复：{d.get('title', '')}", "请书面回复赶工措施及预计完成日期" + ("（上次回复被退回）" if r["status"] == "returned" else ""),
                            "待回复", tone="red" if r["overdue"] else "orange", due=r["due_date"], overdue=r["overdue"], label="去回复", route=route))
        elif r["todo"] == "review":
            out.append(item("letter", r["id"], mod, f"分包已回复进度联系单：{d.get('title', '')}", f"{r['company']}已回复措施，请接受或退回（接受不改任何进度数据）",
                            "待接受", tone="blue", label="去查看", route=route))
        else:
            out.append(item("letter", r["id"], mod, f"{name}超过 24 小时未签收：{d.get('title', '')}", f"{r['company']}尚未签收，已进入催办", "未签收", tone="orange", label="去查看", route=route))
    return _cap(out, "letter", len(out), "份致分包文书", "letters", "site")


def src_meetings(c: Ctx) -> list[dict]:
    """例会行动项：派给我、尚未完成的（就地标记完成）。"""
    if c.role in READONLY_ROLES:
        return []
    from . import cmd as cmd_svc
    try:
        rows = [i for i in cmd_svc.my_items(c.conn, c.user) if i["status"] != "done"]
    except PermissionError:
        return []
    out = []
    for i in rows:
        over = i["status"] == "overdue"
        out.append(item("meeting", i["id"], "site", i["text"], "例会行动项" + (f"；已逾期 {i['days_over']} 天" if over else ""), "已逾期" if over else "待办", tone="red" if over else "blue",
                        due=i["due_date"], overdue=over, label="标记完成", quick=[{"label": "标记完成", "primary": True, "api": {"method": "POST", "url": f"/api/meetings/items/{i['id']}/done", "body": {"done": True}},
                                                                             "done": "已标记完成"}]))
    return _cap(out, "meeting", len(out), "例会行动项", "meetings" if cmd_ok(c) else "", "site")


def cmd_ok(c: Ctx) -> bool:
    from . import cmd as cmd_svc
    return cmd_svc.is_commander(c.user)


def src_quick(c: Ctx) -> list[dict]:
    """扫码上报待认领（只留下没有对应候选隐患的上报——有候选的以候选条目为准，同一件事只一个入口）。"""
    from . import quick as quick_svc
    if c.role not in quick_svc.QR_ROLES:
        return []
    rows = quick_svc.list_submissions(c.conn, c.user, "new", limit=30)
    scan = {"pending": "智能体识别中", "hazard": "已识别出隐患", "clear": "未发现明显隐患", "failed": "识别暂不可用", "skipped": "未做识别"}
    out = []
    for s in rows:
        if any(f["candidate"] and f["id"] in c._cand_ids for f in s["findings"]):
            continue
        who = " · ".join(x for x in (s["team"], s["name"]) if x)
        out.append(item("quick", s["id"], "safety", f"扫码上报：{s['label']}（回执 {s['receipt']}）",
                        f"{scan.get(s['scan'], '')}" + (f"；说明：{s['note']}" if s["note"] else "") + (f"；上报人：{who}（自填）" if who else ""), "待认领", tone="blue", label="认领",
                        quick=[{"label": "认领", "primary": True, "api": {"method": "POST", "url": f"/api/quick/submissions/{s['id']}/claim"}, "done": "已认领"},
                               {"label": "标无效", "api": {"method": "POST", "url": f"/api/quick/submissions/{s['id']}/invalid"}, "confirm": "标为无效？", "done": "已标为无效"}],
                        route="qrcodes", thumb=(s["photos"][0]["id"] if s["photos"] else None)))
    return _cap(out, "quick", len(out), "条扫码上报", "qrcodes", "safety")


# ============================================================================ 汇总

SOURCES: dict[str, list] = {
    "tech": [src_my_findings, src_letters, src_tech_week, src_gate, src_site_quality, src_meetings, src_scan_failed],
    "sub_safety": [src_candidates, src_scan_failed, src_my_findings, src_letters, src_quick, src_meetings],
    "eng_sup": [src_decisions, src_quality, src_log_review, src_week_missing, src_data_gap, src_gate, src_mapping, src_site_quality, src_reports, src_claim_notice, src_letters, src_meetings],
    "safety_sup": [src_candidates, src_scan_failed, src_safety_board, src_letters, src_quick, src_meetings],
    # 项目经理只留拍板项（D7）与本人的例会行动项（D19）
    "manager": [src_crash, src_escalated, src_claim_notice, src_reports, src_meetings],
}


# 来源函数 → 所属的增强能力开关；关闭后该来源不出条目（src_site_quality 同时含材料与资料，在函数内分别判断）
SOURCE_FEATURE = {"src_gate": "gate", "src_meetings": "cmd", "src_quick": "qr"}


def _sort_key(c: Ctx, order: dict[str, int]):
    # 分包安全员：只有本人能拍板的 AI 候选置顶，不被派给技术员的整改挤到折叠区（U-03）
    pin = (lambda i: 0 if _base(i["source"]) == "candidate" else 1) if c.role == "sub_safety" else (lambda i: 0)

    def key(i: dict):
        return (pin(i), 0 if i["overdue"] else 1, i["due"] or c.iso, TONE_RANK.get(i["tone"], 5), order.get(_base(i["source"]), 99), str(i["id"]))
    return key


def _scope_guard(c: Ctx, items: list[dict]) -> list[dict]:
    """项目经理待办的兜底守卫：只留白名单来源（PM_ALLOWED，“另有 N 项”按原来源算），反向白名单 PM_FORBIDDEN 一律丢弃，
    再按简报同一套规则丢弃越界文字（日志/缺报/数据审核/产值）。来源挂错或新增来源忘了口径，也到不了项目经理。"""
    if c.role != "manager":
        return items
    return [i for i in items if _base(i["source"]) in PM_ALLOWED and _base(i["source"]) not in PM_FORBIDDEN and i["source"] not in PM_FORBIDDEN
            and brief_svc.scope_ok(f"{i['title']} {i['reason']} {i['status']}")]


def _count(conn, sql: str, params=()) -> int:
    return int(one(conn, sql, params)["n"] or 0)


URGENT_NAME = "紧急待办"   # 待办里超期或红色的条数：是“待办条数”，与简报里的“超期隐患/高风险隐患”等台账数区分开（D-1）


def _overview(c: Ctx, items: list[dict], urgent: int) -> list[dict]:
    """概览条 4 个数。管理岗（有简报的岗位）的台账类数字取自 brief.counts——与简报计数签名（refs.counts）同一来源、同一口径，
    同屏同一指标只有一个数（AC-2、D-1）；“待处理/待我拍板/紧急待办”数的是待办条目，名称上与台账数区分开。"""
    q = lambda label, value, **kw: {"label": label, "value": value, **kw}  # noqa: E731
    pending = q("待处理", len(items), filter="all", tone="orange" if items else "")
    attn = q(URGENT_NAME, urgent, filter="urgent", tone="red" if urgent else "")
    conn, pid, iso = c.conn, c.pid, c.iso
    ws = _week_start(c.today)
    if c.role == "tech":
        n = _count(conn, "SELECT COUNT(*) n FROM notes WHERE project_id=? AND author_id=? AND note_date=?", (pid, c.uid, iso))
        wk = one(conn, "SELECT status FROM logs WHERE project_id=? AND author_id=? AND period='week' AND mode='ai' AND log_date=?", (pid, c.uid, ws))
        days = _count(conn, "SELECT COUNT(DISTINCT note_date) n FROM notes WHERE project_id=? AND author_id=? AND note_date BETWEEN ? AND ?", (pid, c.uid, ws, iso))
        done = bool(wk and wk["status"] in ("submitted", "approved"))
        return [q("今日随手记", n), pending, attn, q("本周确认", "已确认" if done else "未确认", sub=f"本周 {days} 天有记录", route="week", tone="green" if done else "")]
    if c.role == "sub_safety":
        ids = role_views.scope_ids(conn, c.user) or [c.uid]
        mk = ",".join("?" for _ in ids)
        photos = _count(conn, f"SELECT COUNT(*) n FROM media WHERE project_id=? AND purpose='safety' AND substr(uploaded_at,1,10)=? AND uploader_id IN ({mk})", (pid, iso, *ids))
        res = _count(conn, f"SELECT COUNT(*) n FROM findings WHERE project_id=? AND agent='safety' AND confirmed_at IS NOT NULL AND status='resolved' AND substr(resolved_at,1,10)>=? AND assignee_id IN ({mk})", (pid, ws, *ids))
        return [q("今日随手拍", photos, sub="张"), {**pending, "label": "待整改与确认"}, attn, q("本周已闭环", res, tone="green")]
    k = brief_svc.counts(conn, pid, c.today)
    s, qa = k["safety"], k["quality"]
    hazard = q("超期隐患", s["overdue"], sub=f"高风险隐患 {s['high']}", route="safety", tone="red" if s["overdue"] else "")
    if c.role == "eng_sup":
        n = _count(conn, "SELECT COUNT(*) n FROM notes WHERE project_id=? AND note_date=?", (pid, iso))
        techs = _count(conn, "SELECT COUNT(*) n FROM users WHERE project_id=? AND role='tech' AND active=1", (pid,))
        conf = _count(conn, "SELECT COUNT(*) n FROM logs WHERE project_id=? AND period='week' AND log_date=? AND status IN ('submitted','approved')", (pid, ws))
        # 数字与名称连读：“2 / 今日随手记 / 全项目”，不再拼成“2 今日新增记录 条随手记”（F-18③、U-31）
        return [q("今日随手记", n, sub="全项目", route="logs"), pending,
                q("超期质量问题", qa["overdue"], sub=f"质量问题未闭环 {qa['open']}", route="quality", tone="red" if qa["overdue"] else ""),
                q("本周确认进度", f"{conf}/{techs}" if techs else "—", sub="技术员已确认" if techs else "暂无技术员账号", route="logs")]
    new_hz = _count(conn, "SELECT COUNT(*) n FROM findings WHERE project_id=? AND agent='safety' AND confirmed_at IS NOT NULL AND substr(created_at,1,10)=?", (pid, iso))
    if c.role == "safety_sup":
        res = _count(conn, "SELECT COUNT(*) n FROM findings WHERE project_id=? AND agent='safety' AND confirmed_at IS NOT NULL AND status='resolved' AND substr(resolved_at,1,10)>=?", (pid, ws))
        return [q("今日新增隐患", new_hz), pending, hazard, q("本周已闭环", res, tone="green")]
    # 项目经理：只有进度、质量、安全。“待我拍板”只数待办里真正要他办的件数（U-13③）；红橙预警数与简报“红 x 橙 y”同源。
    # 只数待工程主管确认（status=open）的红橙预警，名称写明“待确认”，与待办里“已确认红色预警”（赶工决定）区分开（D-3）
    new_q = _count(conn, "SELECT COUNT(*) n FROM findings WHERE project_id=? AND agent='quality' AND kind<>'qc_reminder' AND confirmed_at IS NOT NULL AND substr(created_at,1,10)=?", (pid, iso))
    alerts = k["alerts"]["red"] + k["alerts"]["orange"]
    return [q("今日新增问题", new_hz + new_q, sub="隐患与质量问题"), {**pending, "label": "待我拍板"}, hazard,
            q("待确认红橙预警", alerts, sub="待工程主管确认", route="progress?tab=alerts", tone="red" if alerts else "")]


_ALERT_CODE = re.compile(r"\b[A-Z]{1,3}\d{3,5}\b")
AREA_ROUTE = {"进度": "progress?tab=alerts", "质量": "quality", "安全": "safety"}


def _same_sentence(a: str | None, b: str | None) -> bool:
    norm = lambda t: re.sub(r"[\s，。；、：:,.;！!]", "", t or "")  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def _attention(c: Ctx) -> dict:
    """需要关注 ≤3 条：取今日简报（只含进度、质量、安全，已按 scope_ok 过滤）；还没生成简报时给出生成入口。
    结论行：安全主管取简报里第一条安全条目（U-11，按岗位取最相关的一条），其余岗位取 headline；要点区不再重复结论那一句（S-9①）。
    source/note：简报结论已失效、改由系统按当前事实生成时为 system，前端据此把标签换成“系统按当前事实生成”并直接显示原因（AC-3.5）。"""
    if c.role not in ("manager", "eng_sup", "safety_sup"):
        return {"enabled": False, "items": [], "missing": False, "headline": None, "at": None}
    b = brief_svc.latest(c.conn, c.pid, c.iso)
    if not b:
        return {"enabled": True, "items": [], "missing": True, "headline": None, "at": None}
    rows = [i for i in (b.get("items") or []) if i.get("area") in brief_svc.BRIEF_AREAS and brief_svc.scope_ok(f"{i.get('title')} {i.get('detail')} {i.get('action')}")]
    headline = b.get("headline") if brief_svc.scope_ok(b.get("headline") or "") else None
    n0 = len(rows)
    if c.role == "safety_sup":
        rows.sort(key=lambda i: 0 if i["area"] == "安全" else 1)
        pick = next((i for i in rows if i["area"] == "安全" and i.get("title")), None)
        text = f"{pick['title']}：{pick['detail']}" if pick and pick.get("detail") else (pick or {}).get("title")
        # 样本为零的方面写成“可控/正常”的不当结论（与 headline 同一道校验）
        if text and not brief_svc.zero_sample_problems(text, brief_svc._zero_from_facts(b.get("facts"))):
            headline = text
            rows = [i for i in rows if i is not pick]
    if headline:
        rows = [i for i in rows if not (_same_sentence(i.get("title"), headline) or _same_sentence(f"{i.get('title')}{i.get('detail')}", headline))]
    out = []
    for i in rows[:3]:
        route = AREA_ROUTE.get(i["area"], "")
        m = _ALERT_CODE.search(f"{i.get('title', '')} {i.get('detail', '')}")
        if m and i["area"] == "进度":
            a = one(c.conn, "SELECT id FROM alerts WHERE project_id=? AND activity_code=? AND status='open' AND rule<>'stale' ORDER BY id DESC LIMIT 1", (c.pid, m.group(0)))
            if a:
                route = f"alert?id={a['id']}"
        out.append({"level": i.get("level"), "area": i["area"], "title": i.get("title"), "detail": i.get("detail"), "action": i.get("action"), "locate": route})
    return {"enabled": True, "items": out, "missing": False, "headline": headline, "at": b.get("created_at"), "basis": b.get("basis"),
            "source": b.get("source") or "", "note": (b.get("basis") or {}).get("note") or "", "merged": len(rows) < n0}


def _did(c: Ctx) -> list[dict]:
    """智能体今日处理（只数真实调用）：并入需要关注区的一行，不再单独成块。"""
    if c.role not in ("manager", "eng_sup", "safety_sup"):
        return []
    from ..agents import AGENT_BY_KEY, KIND_TO_AGENT
    mine = {"safety_sup": {"safety"}, "eng_sup": {"scribe", "quality", "dq", "progress", "output", "report"}, "manager": PM_AGENTS}[c.role]
    did: dict[str, dict] = {}
    for r in all_(c.conn, "SELECT kind, COUNT(*) n, SUM(ok) ok FROM ai_runs WHERE project_id=? AND substr(created_at,1,10)=? GROUP BY kind", (c.pid, c.iso)):
        key = KIND_TO_AGENT.get(r["kind"])
        if not key or key not in mine:
            continue
        a = did.setdefault(key, {"key": key, "name": AGENT_BY_KEY[key]["name"].replace("Agent", ""), "did": brief_svc._DID.get(key, ""), "runs": 0, "ok": 0})
        a["runs"] += r["n"]
        a["ok"] += r["ok"] or 0
    return sorted(did.values(), key=lambda x: -x["ok"])


def _empty(c: Ctx) -> dict:
    """没有任何待办时的引导：空库给出下一步（带按钮），不是一片“暂无”。"""
    conn = c.conn
    techs = _count(conn, "SELECT COUNT(*) n FROM users WHERE project_id=? AND role='tech' AND active=1", (c.pid,))
    has_plan = bool(p6.current_import(conn, c.pid))
    if c.role == "tech":
        return {"title": "暂无待办", "hint": "可以先说一句、拍一张，记下今天的现场情况；每周确认一次进度即可。", "actions": [{"label": "记一条随手记", "focus": "note"}]}
    if c.role == "sub_safety":
        return {"title": "暂无待办", "hint": "现场巡查发现隐患，随手拍一张，AI 识别后在这里等你确认。", "actions": [{"label": "随手拍隐患", "focus": "snap"}] + ([{"label": "生成部位二维码", "route": "qrcodes"}] if c.feat("qr") else [])}
    if not has_plan and R.can(c.role, "progress_ops"):
        return {"title": "暂无待办：还没有 P6 计划", "hint": "导入 P6 导出的 .xer 后，才会有进度预警、放行清单和数据缺口。"
                + ("" if techs else "现场记录需要先有技术员账号，请系统管理员在“账号与权限”里新增。"), "actions": [{"label": "去导入 P6 计划", "route": "progress?tab=plan"}]}
    if not has_plan:
        return {"title": "暂无待办：项目还没有 P6 计划", "hint": "等项目部导入 P6 计划后，进度与安全质量的待办会出现在这里。", "actions": []}
    if not techs:
        return {"title": "暂无待办：还没有技术员账号", "hint": "现场记录来自分包技术员，请系统管理员在“账号与权限”里新增技术员账号。", "actions": []}
    return {"title": "暂无待办", "hint": "当前没有需要你办理的事项。", "actions": []}


def build(conn, user: dict, today: date | None = None) -> dict:
    c = Ctx(conn, user, today)
    base = {"role": c.role, "day": c.iso, "boundary": BOUNDARY, "modules": [{"key": "all", "name": "全部"}] + [{"key": k, "name": n} for k, n in MODULES]}
    if c.role in READONLY_ROLES:
        # 领导只读、系统管理员不参与业务决策：没有“要我办”的待办，首页保留各自的总览（见前端）
        return {**base, "readonly": True, "items": [], "total": 0, "tabs": {"all": 0, **{k: 0 for k, _ in MODULES}}, "today_due": 0, "overdue": 0, "overview": [],
                "attention": {"enabled": False, "items": [], "missing": False, "headline": None, "at": None}, "did": [], "empty": None, "know": None}
    items: list[dict] = []
    order: dict[str, int] = {}
    for n, fn in enumerate(SOURCES.get(c.role, [])):
        feat = SOURCE_FEATURE.get(fn.__name__)
        if feat and not c.feat(feat):
            continue
        try:
            got = fn(c)
        except Exception:  # noqa: BLE001 — 一个来源出错只记录，不拖垮整张待办
            log.exception("待办来源失败：%s", fn.__name__)
            continue
        for g in got:
            order.setdefault(g["source"].replace("_more", ""), n)
        items += got
    items = _scope_guard(c, items)
    items.sort(key=_sort_key(c, order))
    total = len(items)
    items = items[:MAX_ITEMS]
    for n, i in enumerate(items, 1):
        i["no"] = n
    urgent = sum(1 for i in items if i["overdue"] or i["tone"] == "red")
    tabs = {"all": total, **{k: sum(1 for i in items if i["module"] == k) for k, _ in MODULES}}
    imp = p6.current_import(conn, c.pid) if c.role in ("manager", "eng_sup", "safety_sup") else None
    p6info = {"file": imp["file_name"], "data_date": imp["data_date"], "stale_days": (c.today - date.fromisoformat(imp["data_date"])).days} if imp else None
    return {**base, "readonly": False, "p6": p6info, "items": items, "total": total, "tabs": tabs, "today_due": sum(1 for i in items if i["due"] == c.iso), "overdue": sum(1 for i in items if i["overdue"]),
            "overview": _overview(c, items, urgent), "attention": _attention(c), "did": _did(c), "empty": None if items else _empty(c), "know": _know(c)}
