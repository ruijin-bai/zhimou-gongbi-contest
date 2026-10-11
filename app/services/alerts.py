"""预警处置：AI 分析、推送（站内/邮件/Webhook）、人工反馈。"""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

import httpx

from .. import roles as R
from ..ai import alert_agent
from ..ai.client import AIError, call_json
from ..config import settings
from ..db import all_, audit, dumps, insert, loads, now_iso, one, tx
from . import act_names as _an
from . import facts as F
from . import p6
from .progress import related_logs, serialize_alert

log = logging.getLogger(__name__)


def explain(conn, alert_id: int, user_id: int | None) -> dict:
    row = one(conn, "SELECT * FROM alerts WHERE id=?", (alert_id,))
    if not row:
        raise ValueError("预警不存在")
    alert = serialize_alert(row)
    act = one(conn, "SELECT * FROM activities WHERE import_id=? AND code=?", (row["import_id"], row["activity_code"])) or {
        "code": row["activity_code"], "name": row["activity_name"]}
    logs = related_logs(conn, row["project_id"], row["activity_code"], row["as_of"])
    valid = {f"L{lg['log_id']}" for lg in logs} | {f"L{r['log_id']}" for r in alert["detail"].get("risk_signals", [])}
    fact = F.alert_fact(conn, row)  # 本条预警自己的判定（同一作业多条预警时不取主预警的数字）
    prompt = (alert_agent.build_prompt(alert, act, logs) + "\n\n【事实底稿】\n" + F.render(fact) + "\n" + F.RULES_TEXT
              + f"\n【措施约束】判定日 {row['as_of']}：措施期限不得早于判定日；责任岗位只能从 {'、'.join(alert_agent.OWNER_ROLES)} 中选。")

    def call(feedback: str):
        return call_json(
            "alert.explain", system=alert_agent.SYSTEM, user=prompt + feedback,
            prompt_version=alert_agent.PROMPT_VERSION, mock=lambda: alert_agent.mock(alert, logs),
            validate=lambda d: alert_agent.normalize(d, valid), project_id=row["project_id"], user_id=user_id, max_tokens=1500,
            input_meta={"alert_id": alert_id, "logs": len(logs)},
        )
    # 判定数字（滞后/浮时）与预警标题同源；AI 文字与之矛盾时重试一次，仍不过则改用模板并标注
    # 措施期限不得早于判定日、责任人只能是系统岗位，同样不过即重试/模板
    g = F.guarded(call, alert_agent.texts, [fact], lambda: alert_agent.template(alert, logs, F.alert_line(fact)), extra=prompt,
                  check=lambda d: alert_agent.measure_problems(d, row["as_of"]))
    # 分析所依据的 P6 批次数据日期一并留存（预警详情“基于 X 判定”可同时写明 P6 数据日期）
    data = {**g.data, "source": g.source, "facts_sig": F.facts_sig(fact), "p6_data_date": fact.get("alert_p6_date")}
    if g.problems and g.source == "template":
        data["rejected"] = g.problems[:3]
    conn.execute("UPDATE alerts SET explanation_json=?, ai_run_id=? WHERE id=?", (dumps(data), g.run_id, alert_id))
    return serialize_alert(one(conn, "SELECT * FROM alerts WHERE id=?", (alert_id,)))


def recipients(conn, project_id: int, code: str) -> list[dict]:
    imp = p6.current_import(conn, project_id)
    act = one(conn, "SELECT * FROM activities WHERE import_id=? AND code=?", (imp["id"], code)) if imp else None
    users = all_(conn, "SELECT * FROM users WHERE project_id=? AND active=1", (project_id,))
    out = []
    for u in users:
        if u["role"] in ("manager", "admin", "eng_sup"):
            out.append(u)
        elif u["role"] == "tech" and act and u["wbs_scope"] and p6.in_scope(act, u["wbs_scope"]):
            out.append(u)
    return out


def _send_email(to: str, subject: str, body: str) -> str:
    if not settings.smtp_host or not to:
        return "skipped"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("智眸工笔预警", settings.smtp_from))
    msg["To"] = to
    try:
        cls = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
        with cls(settings.smtp_host, settings.smtp_port, timeout=20) as s:
            if not settings.smtp_ssl:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.sendmail(settings.smtp_from, [to], msg.as_string())
        return "sent"
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("邮件推送失败: %s", type(exc).__name__)
        return f"failed:{type(exc).__name__}"


def _send_webhook(title: str, body: str) -> str:
    if not settings.webhook_url:
        return "skipped"
    try:
        # 兼容企业微信/钉钉群机器人 text 消息格式；其他系统可按需适配
        r = httpx.post(settings.webhook_url, json={"msgtype": "text", "text": {"content": f"{title}\n{body}"}}, timeout=15)
        return "sent" if r.status_code < 300 else f"failed:HTTP{r.status_code}"
    except httpx.HTTPError as exc:
        return f"failed:{type(exc).__name__}"


def tech_message(row: dict, f: dict) -> tuple[str, str]:
    """给技术员的预警通知：大白话，只说哪项作业慢了几天、请说原因、去哪里说；不出现浮时、判定日、里程碑、作业编码。"""
    name = f"《{row['activity_name'] or '这项作业'}》"
    slip = f.get("slip_days") or 0
    days = max(1, round(slip)) if slip > 0 else 0
    head = f"{name}比计划慢约 {days} 天" if days else f"{name}近几天的记录里有可能拖慢进度的问题"
    ask = "请说一下原因（缺料/天气/设备/人手…）" if days else "请说一下现在的情况（缺料/天气/设备/人手…）"
    body = f"{head}，{ask}。\n回首页“随手记”说一句或打一行字就行，不用填表。"
    return f"进度提醒：{head}", body


def notify(conn, alert_id: int) -> dict:
    row = one(conn, "SELECT * FROM alerts WHERE id=?", (alert_id,))
    alert = serialize_alert(row)
    f = alert["detail"]
    lvl = {"red": "【红色预警】", "orange": "【橙色预警】", "yellow": "【黄色预警】"}[row["level"]]
    title = lvl + row["title"]
    # 写明判定日与所用 P6 批次的数据日期：按真实日期判定、批次后来又变时，收件人能看出这条推送依据的是哪一期计划
    imp = one(conn, "SELECT data_date FROM p6_imports WHERE id=?", (row["import_id"],)) if row.get("import_id") else None
    lines = [f"{F.judged_on(row['as_of'], (imp or {}).get('data_date'))}：计划应完成 {f.get('planned_pct')}%，实际 {f.get('actual_pct')}%（{f.get('actual_source')}）",
             f"滞后 {f.get('slip_days')} 天，总浮时 {f.get('total_float_d')} 天，预测完成 {f.get('forecast_finish')}"]
    if f.get("milestones"):
        lines.append("可能影响里程碑：" + "、".join(m["name"] for m in f["milestones"]))
    exp = alert.get("explanation") or {}
    if exp.get("judgement"):
        lines.append("AI分析：" + exp["judgement"])
    for m in exp.get("measures", [])[:3]:
        lines.append(f"建议：{m['action']}（{m['owner']}，{m['due']}）")
    body = "\n".join(lines)
    mp = _an.names(conn, row["project_id"])   # 推送、邮件里的作业代码换成中文简称
    title, body = _an.humanize(title, mp), _an.humanize(body, mp)
    channels = {"webhook": _send_webhook(title, body)} if row["level"] in ("red", "orange") else {"webhook": "skipped"}
    sent = []
    tech_title, tech_body = (_an.humanize(x, mp) for x in tech_message(row, f))
    for u in recipients(conn, row["project_id"], row["activity_code"]):
        t, b = (tech_title, tech_body) if u["role"] == "tech" else (title, body)  # 技术员看大白话；管理岗原文不变
        ch = {"inapp": "sent", "email": _send_email(u["email"], t, b) if row["level"] != "yellow" else "skipped"}
        insert(conn, "notifications", {"project_id": row["project_id"], "user_id": u["id"], "alert_id": alert_id, "title": t,
                                       "body": b, "channels_json": dumps({**ch, **channels}), "created_at": now_iso()})
        sent.append({"user": u["name"], **ch})
    return {"recipients": sent, **channels}


def process_new(conn, alert_ids: list[int], user_id: int | None, explain_limit: int = 6) -> list[dict]:
    """新增/升级预警：先 AI 分析（红/橙优先、数量受限），再推送。AI 失败不阻断推送。
    分析要等 AI（每条可能数十秒），其间预警可能已被重新判定关闭或人工处理：推送前再读一次状态，已不是 open 的不推送。"""
    out = []
    rows = [one(conn, "SELECT * FROM alerts WHERE id=?", (i,)) for i in alert_ids]
    rows.sort(key=lambda r: {"red": 0, "orange": 1, "yellow": 2}[r["level"]])
    for idx, r in enumerate(rows):
        err = None
        if idx < explain_limit and r["level"] in ("red", "orange"):
            try:
                explain(conn, r["id"], user_id)
            except AIError as exc:
                err = str(exc)
        cur = one(conn, "SELECT status FROM alerts WHERE id=?", (r["id"],))
        if not cur or cur["status"] != "open":
            state = F.ALERT_STATUS_NAME.get(cur["status"], "已关闭") if cur else "已不存在"
            out.append({"alert_id": r["id"], "ai_error": err, "push": {"skipped": f"预警{state}，不再推送"}})
            continue
        out.append({"alert_id": r["id"], "ai_error": err, "push": notify(conn, r["id"])})
    return out


def feedback(conn, alert_id: int, user: dict, status: str, note: str) -> dict:
    if not R.can(user["role"], "progress_ops"):
        raise ValueError("只有港湾项目部相关岗位可以反馈预警")
    if status not in ("confirmed", "false_alarm", "closed"):
        raise ValueError("反馈状态无效")
    if status == "false_alarm" and not note.strip():
        raise ValueError("标记误报请写明原因，用于优化阈值")
    with tx(conn):
        cur = conn.execute("UPDATE alerts SET status=?, feedback_by=?, feedback_note=?, feedback_at=? WHERE id=? AND project_id=?",
                           (status, user["id"], note.strip()[:500], now_iso(), alert_id, user["project_id"]))
        if cur.rowcount != 1:
            raise ValueError("预警不存在")
        audit(conn, user["id"], "alert.feedback", f"alert:{alert_id}", status)
    return serialize_alert(one(conn, "SELECT * FROM alerts WHERE id=?", (alert_id,)))


__all__ = ["explain", "notify", "process_new", "feedback", "loads"]
