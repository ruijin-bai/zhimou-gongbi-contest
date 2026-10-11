"""现场三个 Agent：安全（照片隐患+整改复核）、质量（工序质控点提醒）、数据审核（日志预审）。"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from statistics import median

from .. import roles as R
from ..ai import safety_agent
from ..ai.client import AIError, call_json
from ..config import settings
from ..db import all_, audit, dumps, event, insert, loads, now_iso, one, tx
from . import findings, p6
from .media import hamming

log = logging.getLogger(__name__)


# ============================================================================ 安全 Agent

def _scan_group(conn, batch: list[dict], user_id: int | None, created: list[int]) -> int:
    """识别一组（≤4张）照片，返回失败的张数。整组因“输出无法解析/请求被拒（多为图片过大或损坏）”失败时，拆成单张再试一次，
    让一张坏图不拖累同组其他照片；网络、限流、额度、暂停类失败不拆（拆了只会更多次失败）。"""
    refs = [f"P{j + 1}" for j in range(len(batch))]
    photos = [{"ref": refs[j], "purpose": m["purpose"]} for j, m in enumerate(batch)]
    try:
        res = call_json(
            "safety.scan", system=safety_agent.SCAN_SYSTEM, user="请检查以下照片：" + "、".join(refs),
            prompt_version=safety_agent.PROMPT_VERSION, images=[str(settings.media_dir / m["ai_file"]) for m in batch],
            mock=lambda: safety_agent.mock_scan(photos), validate=lambda d: safety_agent.normalize_scan(d, refs),
            project_id=batch[0]["project_id"], user_id=user_id, max_tokens=2200, input_meta={"media": [m["id"] for m in batch]},
        )
    except AIError as exc:
        log.warning("安全识别失败（%d张，%s）: %s", len(batch), exc.kind, exc)
        if len(batch) > 1 and exc.kind in ("format", "bad_request"):
            return sum(_scan_group(conn, [m], user_id, created) for m in batch)
        for m in batch:
            conn.execute("UPDATE media SET safety_status='failed' WHERE id=?", (m["id"],))
        return len(batch)
    by_ref = {p["photo"]: p for p in res.data}
    failed = 0
    try:
        with tx(conn):
            for j, m in enumerate(batch):
                p = by_ref.get(refs[j])
                if p is None:  # 模型没给这张照片的结果：不能当作“无隐患”，保留失败状态以便补跑
                    conn.execute("UPDATE media SET safety_status='failed' WHERE id=?", (m["id"],))
                    failed += 1
                    continue
                conn.execute("UPDATE media SET safety_status=? WHERE id=?", ("hazard" if p["hazards"] else "clear", m["id"]))
                for k, h in enumerate(p["hazards"]):
                    fid = findings.create(
                        conn, m["project_id"], "safety", "hazard", h["severity"], f"{h['category']}：{h['desc']}",
                        {**h, "scene": p.get("scene"), "media_id": m["id"], "log_id": m["log_id"]},
                        ref_type="media", ref_id=m["id"], fingerprint=f"hz:{m['id']}:{k}",
                        assignee_id=m["uploader_id"], due_days=safety_agent.DUE[h["severity"]], ai_run_id=res.run_id,
                        candidate=True)  # AI 识别的隐患先作“候选”，由安全员/安全主管确认后才进台账、派单、通知
                    if fid:
                        created.append(fid)
    except Exception:  # noqa: BLE001  入库异常：照片标记失败待补跑，不影响同次其他批次
        log.exception("安全识别结果入库失败")
        for m in batch:
            conn.execute("UPDATE media SET safety_status='failed' WHERE id=?", (m["id"],))
        return len(batch)
    return failed


def safety_scan(conn, media_ids: list[int], user_id: int | None) -> dict:
    """识别照片隐患并派发整改。每次最多 4 张一组调用交融，失败的照片标记 failed，
    由安全员点“重新识别”人工重试（不自动补跑）。重复识别同一张照片不会重复派单（按指纹去重）。"""
    rows = [one(conn, "SELECT * FROM media WHERE id=?", (i,)) for i in media_ids]
    rows = [r for r in rows if r and r["purpose"] != "rectify"]
    created: list[int] = []
    failed = 0
    for i in range(0, len(rows), 4):
        failed += _scan_group(conn, rows[i:i + 4], user_id, created)
    return {"created": created, "failed": failed, "ok": len(rows) - failed}


def _ai_ready() -> bool:
    from ..ai.client import provider_status
    try:
        return bool(provider_status().get("ready"))
    except Exception:  # noqa: BLE001
        return False


def _older_than(ts: str | None, minutes: int, now: datetime) -> bool:
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return True
    if t.tzinfo is None:
        t = t.astimezone()
    return now - t >= timedelta(minutes=minutes)


def after_rescan(conn, media_ids: list[int]) -> None:
    """人工“重新识别”成功后补齐首次识别流程里“识别完成后”才做的两件事：扫码照片的来源标注与归类。失败只记日志。"""
    from . import quick
    try:
        mark = ",".join("?" for _ in media_ids)
        done = {r["id"] for r in all_(conn, f"SELECT id FROM media WHERE id IN ({mark}) AND safety_status IN ('hazard','clear')", media_ids)}
        if not done:
            return
        for sub in all_(conn, "SELECT id, media_json FROM quick_submissions WHERE media_json<>'[]'"):
            if done & set(loads(sub["media_json"], [])):
                quick.tag_findings(conn, sub["id"])
        dmark = ",".join("?" for _ in done)
        conn.execute(f"UPDATE media SET purpose='log' WHERE id IN ({dmark}) AND purpose='safety' AND substr(original_name,1,3)='扫码_'", list(done))
    except Exception:  # noqa: BLE001
        log.exception("重新识别后处理失败")


# ---------------------------------------------------------------------------- 整改复核：AI 只给建议，闭环 / 退回由人确认
# verify_json（契约见升级可行性评估 4.4）：
#   state        pending 待复核 / running 复核中 / done 已给建议 / failed 调用失败
#   suggest      resolved / not_resolved / None（AI 建议，不是确认）
#   started_at   本次 AI 复核开始时间（认领时写入，也是写回结果时的核对凭据）
#   photo_check  ok / unknown 拍摄时间未知 / early 早于派单（24 小时内）/ clock_skew 手机时间异常
#   resolved、reason、remaining、at、by 旧键保留，含义同为 AI 建议；photo_taken_at 仅 clock_skew 时记手机原拍摄时间

VERIFY_STALE_MIN = 10        # running 超过这么久视为未完成（进程重启或模型长时间无响应）
PHOTO_EARLY_MAX = timedelta(hours=24)
PHOTO_SKEW_MAX = timedelta(minutes=10)
REVERIFY_ROLES = ("safety_sup", "manager")
PHOTO_CHECK_NAME = {"unknown": "拍摄时间未知", "early": "拍摄时间早于派单，请核实", "clock_skew": "手机时间异常"}


class RectifyPhotoError(ValueError):
    """整改照片拍摄时间早于派单时间超过 24 小时：照片已保存，但不挂到这条整改。接口返回 422 code=photo_before_dispatch。"""
    code = "photo_before_dispatch"

    def __init__(self, msg: str, media_id: int | None):
        super().__init__(msg)
        self.media_id = media_id


class VerifyConflict(ValueError):
    """当前复核状态不允许“重新让 AI 复核”（接口返回 409）。"""


def _ts(s) -> datetime | None:
    """ISO 时间 → 带时区的 datetime；照片的拍摄时间不带时区，按服务器本地时间理解。无法解析返回 None。"""
    try:
        t = datetime.fromisoformat(str(s).strip())
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def photo_check(f: dict, m: dict, now: datetime | None = None) -> tuple[str, str | None]:
    """整改照片拍摄时间校验（N-8，给手机时钟留容差）。返回 (photo_check, 改记的拍摄时间或 None)。
    派单时间取 confirmed_at，没有时取 created_at；早于派单超过 24 小时抛 RectifyPhotoError，其余只标记、不拒收。"""
    now = now or datetime.now().astimezone()
    taken = _ts(m.get("taken_at")) if m.get("taken_at") else None
    if taken is None:
        return "unknown", None
    if taken - now > PHOTO_SKEW_MAX:  # 手机时钟快了：拍摄时间改记为服务器收到照片的时间
        got = _ts(m.get("uploaded_at")) or now
        return "clock_skew", got.astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
    dispatched = _ts(f["confirmed_at"] or f["created_at"])
    if dispatched and dispatched - taken > PHOTO_EARLY_MAX:
        raise RectifyPhotoError(f"整改照片拍摄于 {taken:%m-%d %H:%M}，早于派单时间 {dispatched.astimezone():%m-%d %H:%M} 超过 24 小时，"
                                "不能作为本次整改照片，请现场重拍（这张照片已保存）", m.get("id"))
    if dispatched and taken < dispatched:
        return "early", None
    return "ok", None


def verify_stale(v: dict | None, now: datetime | None = None) -> bool:
    """AI 复核卡在 running 超过 VERIFY_STALE_MIN 分钟：视为未完成，可人工复核或“重新让 AI 复核”。"""
    v = v or {}
    return v.get("state") == "running" and _older_than(v.get("started_at"), VERIFY_STALE_MIN, now or datetime.now().astimezone())


def verify_label(v: dict | None, status: str | None = None, now: datetime | None = None) -> str:
    """整改复核的统一文案（4.4 契约）：AI 结论只是建议，隐患闭环（status=resolved）之前不出现肯定说法。
    兼容 T-13 之前的旧记录（没有 state，只有 resolved/reason）。"""
    if not v:
        return ""
    state, suggest = v.get("state"), v.get("suggest")
    if state is None:
        suggest = "resolved" if v.get("resolved") is True else "not_resolved" if v.get("resolved") is False else None
        state = "done" if suggest else "failed"
    if status == "open":  # D-4：整改已被人退回重改（或闭环后重新打开）：这是上一次提交的 AI 建议，不再写“待安全主管确认”
        if state == "done" and suggest == "resolved":
            return "已退回重改；AI 原建议（隐患消除）已被人工否定"
        if state == "done" and suggest == "not_resolved":
            return f"已退回重改；AI 原建议：仍存在{v.get('remaining') or '原隐患'}"
        return "已退回重改"
    if state in ("pending", "running"):
        return "AI 复核未完成，可人工复核" if verify_stale(v, now) else "AI 复核中"
    if state == "failed" or not suggest:
        return "AI 复核失败，待人工复核"
    if suggest == "resolved":
        return "AI 建议：已消除" + ("" if status == "resolved" else "，待安全主管确认")
    return f"AI 建议：仍存在{v.get('remaining') or '原隐患'}"


def paren(text) -> str:
    """把一句说明放进全角括号；本身已带括号的（如离线模拟模型的理由“（离线模拟）”）不再套一层（D-5）。空的返回空串。"""
    t = str(text or "").strip()
    if not t:
        return ""
    return t if t.startswith("（") and t.endswith("）") else f"（{t}）"


def _notify_reviewers(conn, f: dict, user: dict, check: str) -> None:
    """整改待复核：通知本项目安全主管（提交人本人除外），消息带隐患跳转。直接写 notifications，不经过 role_views。"""
    note = PHOTO_CHECK_NAME.get(check)
    body = (f"{user['name']}（{R.role_name(user['role'])}）提交了整改照片。AI 复核结论仅供参考，请看前后照片确认闭环或退回。"
            + (f"注意：{note}。" if note else ""))
    for r in all_(conn, "SELECT id FROM users WHERE project_id=? AND role='safety_sup' AND active=1 AND id<>?", (f["project_id"], user["id"])):
        insert(conn, "notifications", {"project_id": f["project_id"], "user_id": r["id"], "alert_id": None, "title": f"整改待复核：{f['title'][:40]}",
                                       "body": body, "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(),
                                       "link_json": dumps({"route": "finding", "params": {"id": str(f["id"])}})})


def rectify_submit(conn, finding_id: int, user: dict, media_id: int) -> dict:
    """提交整改照片（同步，不调用 AI）：校验拍摄时间 → 状态置“待复核”、verify_json 置 pending → 写审计、通知安全主管。
    返回整改单视图。拍摄时间早于派单超过 24 小时抛 RectifyPhotoError，照片留在服务器上但不挂到这条整改。"""
    f = one(conn, "SELECT * FROM findings WHERE id=? AND project_id=? AND agent='safety'", (finding_id, user["project_id"]))
    if not f:
        raise ValueError("隐患不存在")
    if R.is_sub(user["role"]):
        from .role_views import in_scope
        if not in_scope(conn, user, f["assignee_id"]):
            raise ValueError("该隐患未派给你或你的单位")
    if findings.is_candidate(f):
        raise ValueError("这是 AI 候选隐患，须先由安全员或安全主管确认后再整改")
    if f["status"] not in ("open", "rejected"):
        raise ValueError("该隐患当前不需要整改")
    after = one(conn, "SELECT * FROM media WHERE id=? AND project_id=?", (media_id, user["project_id"]))
    if not after:
        raise ValueError("整改照片不存在")
    check, taken_fix = photo_check(f, after)
    body = loads(f["body_json"], {}) or {}
    v = {"state": "pending", "suggest": None, "started_at": None, "photo_check": check,
         "resolved": None, "reason": "", "remaining": "", "at": None, "by": "safety_agent", "body": body.get("desc")}
    if taken_fix:
        v["photo_taken_at"] = after["taken_at"]
    now = now_iso()
    with tx(conn):
        cur = conn.execute("""UPDATE findings SET status='rectifying', rectify_media_id=?, verify_json=?, updated_at=?, resolved_at=NULL
                              WHERE id=? AND status IN ('open','rejected')""", (media_id, dumps(v), now, finding_id))
        if cur.rowcount != 1:  # 并发重复提交：只有第一次生效
            raise ValueError("该隐患当前不需要整改")
        conn.execute("UPDATE media SET purpose='rectify', taken_at=COALESCE(?, taken_at) WHERE id=?", (taken_fix, media_id))
        audit(conn, user["id"], "finding.rectifying", f"finding:{finding_id}", f"media:{media_id} photo_check:{check}")
        _notify_reviewers(conn, f, user, check)
    return findings.view(one(conn, "SELECT * FROM findings WHERE id=?", (finding_id,)))


def rectify_verify(conn, finding_id: int, user_id: int | None = None) -> dict | None:
    """AI 复核：按 verify_json.state 原子认领 pending → running，调用一次 AI，结论只写进 verify_json 作建议，不改隐患状态。
    认领不到（已认领、已完成、隐患已不在待复核）返回 None，不调用 AI；写回前核对 started_at，被重新提交或“重新让 AI 复核”接管的旧结果作废。"""
    started = now_iso()
    with tx(conn):
        f = one(conn, "SELECT * FROM findings WHERE id=?", (finding_id,))
        v = (loads(f["verify_json"], {}) or {}) if f else {}
        if not f or f["status"] != "rectifying" or v.get("state") != "pending" or not f["rectify_media_id"]:
            return None
        v.update(state="running", started_at=started)
        conn.execute("UPDATE findings SET verify_json=? WHERE id=?", (dumps(v), finding_id))
    after = one(conn, "SELECT * FROM media WHERE id=?", (f["rectify_media_id"],))
    before = one(conn, "SELECT * FROM media WHERE id=?", (f["ref_id"],)) if f["ref_type"] == "media" else None
    images = [str(settings.media_dir / m["ai_file"]) for m in (before, after) if m]
    try:
        res = call_json(
            "safety.verify", system=safety_agent.VERIFY_SYSTEM.replace("{hazard}", f["title"]),
            user="第一张：整改前；第二张：整改后。" if before else "仅有整改后照片，请判断是否仍存在原隐患。",
            prompt_version=safety_agent.PROMPT_VERSION, images=images, mock=lambda: {"resolved": True, "same_scene": True, "reason": "（离线模拟）"},
            validate=safety_agent.normalize_verify, project_id=f["project_id"], user_id=user_id, max_tokens=400,
            input_meta={"finding": finding_id})
        out = {**res.data, "state": "done", "suggest": "resolved" if res.data["resolved"] else "not_resolved"}
    except Exception as exc:  # noqa: BLE001  任何失败都落到 failed，交人工复核，不让状态卡在 running
        if not isinstance(exc, AIError):
            log.exception("整改复核失败")
        out = {"state": "failed", "suggest": None, "resolved": None, "reason": f"AI 复核失败：{exc}"[:300], "remaining": ""}
    now = now_iso()
    with tx(conn):
        cur = one(conn, "SELECT verify_json FROM findings WHERE id=?", (finding_id,))
        cv = (loads(cur["verify_json"], {}) or {}) if cur else {}
        if cv.get("state") != "running" or cv.get("started_at") != started:
            return None
        cv.update(out, at=now, by="safety_agent")
        conn.execute("UPDATE findings SET verify_json=?, updated_at=? WHERE id=?", (dumps(cv), now, finding_id))
    return cv


def _verify_bg(finding_id: int, user_id: int | None) -> None:
    """BackgroundTasks 里执行：请求用的连接此时已关闭，另开连接。"""
    from ..db import connect
    conn = connect()
    try:
        rectify_verify(conn, finding_id, user_id)
    except Exception:  # noqa: BLE001
        log.exception("整改复核后台执行失败")
    finally:
        conn.close()


def _queue_verify(conn, finding_id: int, user_id: int, bg) -> None:
    if bg is not None:
        bg.add_task(_verify_bg, finding_id, user_id)
    else:  # 没有 bg 的调用方（脚本等）：同步复核
        rectify_verify(conn, finding_id, user_id)


def safety_rectify(conn, finding_id: int, user: dict, media_id: int, bg=None) -> dict:
    """提交整改照片并安排 AI 复核。传入 FastAPI 的 BackgroundTasks 时，复核在响应发出之后执行，返回时状态为“待复核”、
    verify.state=pending；不传时同步复核。无论 AI 结论如何，状态都停在“待复核”，由安全主管确认闭环或退回。"""
    rectify_submit(conn, finding_id, user, media_id)
    _queue_verify(conn, finding_id, user["id"], bg)
    return findings.view(one(conn, "SELECT * FROM findings WHERE id=?", (finding_id,)))


def rectify_reverify(conn, finding_id: int, user: dict, bg=None) -> dict:
    """重新让 AI 复核（N-9）：只限安全主管、项目经理；只在 AI 复核失败或 running 超过 10 分钟时可用，写审计。
    这是除首次复核外唯一允许再次调用 AI 的途径。"""
    if user["role"] not in REVERIFY_ROLES:
        raise PermissionError("只有安全主管或项目经理可以重新让 AI 复核")
    with tx(conn):
        f = one(conn, "SELECT * FROM findings WHERE id=? AND project_id=? AND agent='safety'", (finding_id, user["project_id"]))
        if not f:
            raise LookupError("隐患不存在")
        v = loads(f["verify_json"], {}) or {}
        if f["status"] != "rectifying" or not (v.get("state") == "failed" or verify_stale(v)):
            raise VerifyConflict("只有待复核且 AI 复核失败、或复核超过 10 分钟未完成时，才能重新让 AI 复核")
        prev = v.get("state")
        v.update(state="pending", suggest=None, started_at=None, resolved=None, reason="", remaining="")
        conn.execute("UPDATE findings SET verify_json=?, updated_at=? WHERE id=?", (dumps(v), now_iso(), finding_id))
        audit(conn, user["id"], "finding.reverify", f"finding:{finding_id}", f"上次状态：{prev}")
    _queue_verify(conn, finding_id, user["id"], bg)
    return findings.view(one(conn, "SELECT * FROM findings WHERE id=?", (finding_id,)))


# ============================================================================ 质量 Agent

QC_VERSION = "qc-v2-20261006"
QC_BATCH = 5  # 每次问几个作业：批太大时输出（每作业3-5条×3个字段）会撞上 max_tokens 被截断，整批报废
QC_STAGES = ("开工前", "施工中", "完成后")

QC_SYSTEM = """你是中国港湾海外道路/港口/桥梁工程的质量工程师。为下列施工作业分别列出关键质量控制点（每个作业3-5条），覆盖开工前、施工中、完成后。
只写通用、可执行的检查项和需留存的记录（如“钢筋隐蔽验收记录”“混凝土试块留置”），不要编造规范条号和具体数值。
每条 item 不超过20字，record 不超过15字；name 必须与给定的作业名称逐字一致。
只输出JSON：{"items":[{"name":"作业名称","points":[{"stage":"开工前|施工中|完成后","item":"检查项","record":"需留存记录"}]}]}"""


def _qc_mock(names: list[str]) -> dict:
    return {"items": [{"name": n, "points": [{"stage": "开工前", "item": "（离线模拟）技术交底与材料报验", "record": "交底记录"},
                                               {"stage": "施工中", "item": "（离线模拟）工序自检", "record": "自检记录"}]} for n in names]}


def _qc_norm(data, names: list[str]) -> dict[str, list[dict]]:
    """模型输出 → {作业名: 质控点}。容忍顶层数组、{作业名: [点…]} 写法、点写成字符串、阶段写法不规范；
    作业名对不上的丢弃（不张冠李戴），一个都对不上则报错，由调用方重答/记为失败，而不是“成功但什么都没有”。"""
    if isinstance(data, list):
        data = {"items": data}
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    items = data.get("items")
    if not isinstance(items, list):
        if data and all(isinstance(v, list) for v in data.values()):
            items = [{"name": k, "points": v} for k, v in data.items()]
        else:
            raise ValueError("缺少items")
    out: dict[str, list[dict]] = {}
    for pos, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        n = str(it.get("name") or "").strip()
        raw = it.get("points")
        pts = []
        for p in raw if isinstance(raw, list) else []:
            if isinstance(p, str) and p.strip():
                p = {"item": p}
            if not isinstance(p, dict) or not str(p.get("item") or "").strip():
                continue
            stage = str(p.get("stage") or "").strip()
            stage = next((x for x in QC_STAGES if x[:2] in stage), "施工中")
            pts.append({"stage": stage, "item": str(p["item"]).strip(), "record": str(p.get("record") or "").strip()})
        match = n if n in names else next((x for x in names if n and (x in n or n in x)), None)
        if not n and len(items) == len(names):
            match = names[pos]  # 没写名称但条数一致：按顺序对应
        if match and pts and match not in out:
            out[match] = pts[:6]
    if not out:
        raise ValueError("没有可对应到作业名称的质控点")
    return out


def qc_points(conn, project_id: int, names: list[str], user_id: int | None) -> dict[str, list[dict]]:
    """按作业名称取质控点（按项目缓存，同名作业只问一次模型）。失败的批次记 qc.fail 事件，定时任务会限次补跑。"""
    names = list(dict.fromkeys(n for n in names if n))
    cached = {r["key"]: loads(r["value_json"]) for r in all_(conn, "SELECT key, value_json FROM knowledge WHERE project_id=? AND kind='qc'", (project_id,))}
    missing = [n for n in names if n not in cached]
    for i in range(0, len(missing), QC_BATCH):
        batch = missing[i:i + QC_BATCH]
        try:
            res = call_json("quality.checklist", system=QC_SYSTEM, user="作业：\n" + "\n".join(batch), prompt_version=QC_VERSION,
                            mock=lambda: _qc_mock(batch), validate=lambda d: _qc_norm(d, batch), project_id=project_id,
                            user_id=user_id, max_tokens=500 + 650 * len(batch), input_meta={"names": len(batch)})
        except AIError as exc:
            log.warning("质控点生成失败: %s", exc)
            event(conn, "qc.fail", project_id=project_id, value=len(batch), data={"error": str(exc)[:120]})
            continue
        for n, pts in res.data.items():
            conn.execute("INSERT OR REPLACE INTO knowledge (project_id, kind, key, value_json, ai_run_id, updated_at) VALUES (?,?,?,?,?,?)",
                         (project_id, "qc", n, dumps(pts), res.run_id, now_iso()))
            cached[n] = pts
        if len(res.data) < len(batch):  # 部分作业模型没给出：不缓存、不造数，下次补跑时再问
            event(conn, "qc.fail", project_id=project_id, value=len(batch) - len(res.data), data={"error": "部分作业未返回质控点"})
    return {n: cached[n] for n in names if n in cached}


QC_RETRY_MAX_PER_DAY = 3
QC_RETRY_GAP_MIN = 30


def retry_quality(conn, project_id: int, now: datetime | None = None) -> dict:
    """当天质控点生成失败（qc.fail）后补跑：每天最多 QC_RETRY_MAX_PER_DAY 次、间隔不少于 QC_RETRY_GAP_MIN 分钟；
    已缓存的作业不会再问模型，提醒按指纹去重，所以重复执行无副作用。"""
    now = now or datetime.now().astimezone()
    today = now.date().isoformat()
    fails = one(conn, "SELECT COUNT(*) n, MAX(created_at) last FROM events WHERE project_id=? AND kind='qc.fail' AND substr(created_at,1,10)=?", (project_id, today))
    if not fails["n"]:
        return {"tried": 0, "created": 0, "skipped": ""}
    retries = one(conn, "SELECT COUNT(*) n, MAX(created_at) last FROM events WHERE project_id=? AND kind='qc.retry' AND substr(created_at,1,10)=?", (project_id, today))
    if retries["n"] >= QC_RETRY_MAX_PER_DAY:
        return {"tried": 0, "created": 0, "skipped": "已达当日补跑上限"}
    if not _older_than(max(fails["last"], retries["last"] or ""), QC_RETRY_GAP_MIN, now):
        return {"tried": 0, "created": 0, "skipped": "间隔未到"}
    if not _ai_ready():
        return {"tried": 0, "created": 0, "skipped": "AI 暂不可用"}
    event(conn, "qc.retry", project_id=project_id, value=1)
    return {"tried": 1, "created": quality_reminders(conn, project_id)}


def quality_reminders(conn, project_id: int, day: str | None = None, user_id: int | None = None, horizon: int = 2) -> int:
    """对即将开工/正在施工的作业派发质控提醒给负责技术员。"""
    d = date.fromisoformat(day) if day else date.today()
    acts = p6.current_activities(conn, project_id)
    targets = []
    for a in acts:
        if a["act_finish"] or a["is_milestone"] or not a["early_start"]:
            continue
        es = date.fromisoformat(a["early_start"])
        if 0 <= (es - d).days <= horizon and not a["act_start"]:
            targets.append((a, "开工前"))
    targets = targets[:20]
    if not targets:
        return 0
    pts = qc_points(conn, project_id, [a["name"] for a, _ in targets], user_id)
    techs = all_(conn, "SELECT * FROM users WHERE project_id=? AND role='tech' AND active=1", (project_id,))
    n = 0
    for a, stage in targets:
        items = [p for p in pts.get(a["name"], []) if p["stage"] == stage] or pts.get(a["name"], [])[:2]
        if not items:
            continue
        owner = next((t for t in techs if t["wbs_scope"] and p6.in_scope(a, t["wbs_scope"])), None)
        fid = findings.create(conn, project_id, "quality", "qc_reminder", "info",
                              f"{a['code']} {a['name']} 计划 {a['early_start']} 开工：请完成开工前质控",
                              {"points": items, "early_start": a["early_start"]}, activity_code=a["code"],
                              fingerprint=f"qc:{a['code']}:{stage}", assignee_id=owner["id"] if owner else None, due_days=horizon)
        n += 1 if fid else 0
    return n


# ============================================================================ 数据审核 Agent

DQ_SYSTEM = """你是项目部的日志审核员。系统已对一份施工日志做了规则检查，请给出预审结论：
risk=low 表示内容完整一致、可直接通过；medium 表示有需要核实的地方；high 表示存在明显矛盾、疑似重复或虚假记录。
summary 一句话说明日志质量；focus 列出审核人最该核实的1-3点（没有则空）。只输出JSON：{"risk":"low|medium|high","summary":"","focus":[""]}"""


def _dq_checks(conn, lg: dict) -> list[dict]:
    c = loads(lg["content_json"], {})
    out = [dict(x, source="规则") for x in loads(lg["checks_json"], []) if x["level"] in ("error", "warn")]

    def add(level, msg):
        out.append({"level": level, "message": msg, "source": "审核Agent"})
    if lg["period"] == "week":
        photos = all_(conn, """SELECT m.* FROM media m JOIN logs d ON d.id=m.log_id WHERE d.project_id=? AND d.author_id=? AND d.period='day'
                               AND d.log_date BETWEEN ? AND ?""", (lg["project_id"], lg["author_id"], lg["log_date"], lg["period_end"]))
    else:
        photos = all_(conn, "SELECT * FROM media WHERE log_id=?", (lg["id"],))
    own_ids = {p["log_id"] for p in photos}
    if photos:
        others = all_(conn, """SELECT m.id, m.dhash, m.log_id, l.log_date FROM media m JOIN logs l ON l.id=m.log_id
                               WHERE m.project_id=? AND m.log_id IS NOT NULL AND m.log_id<>? AND m.dhash IS NOT NULL""",
                      (lg["project_id"], lg["id"]))
        others = [o for o in others if o["log_id"] not in own_ids]
        for p in photos:
            dup = next((o for o in others if hamming(p["dhash"], o["dhash"]) <= 4), None)
            if dup:
                add("warn", f"照片与 L{dup['log_id']}（{dup['log_date']}）中的照片高度相似，疑似重复使用")
    for pr in c.get("progress", []):
        if pr.get("qty") is None:
            continue
        hist = [r["qty"] for r in all_(conn, """SELECT lp.qty FROM log_progress lp JOIN logs l ON l.id=lp.log_id WHERE lp.project_id=?
                    AND lp.activity_code=? AND lp.unit=? AND lp.log_id<>? AND lp.qty IS NOT NULL AND lp.voided_at IS NULL AND l.status IN ('submitted','approved')""",
                    (lg["project_id"], pr["activity_code"], pr.get("unit") or "", lg["id"])) if r["qty"] > 0]
        if len(hist) >= 3 and pr["qty"] > 3 * median(hist):
            add("warn", f"{pr['activity_code']} 今日完成 {pr['qty']:g}{pr.get('unit') or ''}，超过历史日均中位数 {median(hist):g} 的3倍")
    if c.get("works") and not c.get("labor"):
        add("info", "记录了施工内容但没有人员投入")
    unanswered = [q for q in loads(lg["qa_json"], []) if not q.get("answer")]
    if unanswered:
        add("warn", f"AI 提出的 {len(unanswered)} 个问题未回答")
    hz = sum(one(conn, """SELECT COUNT(*) n FROM findings f WHERE f.ref_type='media' AND f.ref_id=? AND f.agent='safety'""", (p["id"],))["n"]
             for p in photos)
    if hz and not any(i.get("type") == "安全" for i in c.get("issues", [])):
        add("warn", f"安全Agent在本日志照片中发现 {hz} 项隐患，日志未记录安全问题")
    due = date.fromisoformat(lg["period_end"] or lg["log_date"]) + timedelta(days=1)
    if lg["submitted_at"] and lg["submitted_at"][:10] > due.isoformat():
        add("info", "周确认晚于下周一提交" if lg["period"] == "week" else "日志晚于次日提交")
    if lg["period"] == "week":
        recorded = one(conn, """SELECT COUNT(DISTINCT note_date) n FROM notes WHERE author_id=? AND note_date BETWEEN ? AND ?""",
                       (lg["author_id"], lg["log_date"], lg["period_end"]))["n"]
        if recorded < 3:
            add("info", f"本周只有 {recorded} 天有随手记，周确认主要依赖回忆")
    return out


def dq_review(conn, log_id: int, user_id: int | None) -> dict:
    lg = one(conn, "SELECT * FROM logs WHERE id=?", (log_id,))
    checks = _dq_checks(conn, lg)
    score = max(0, 100 - sum({"error": 30, "warn": 12, "info": 3}.get(x["level"], 0) for x in checks))
    c = loads(lg["content_json"], {})
    from .logs import summarize
    prompt = f"日志 L{lg['id']} {lg['log_date']}\n{summarize(c, 1500)}\n\n规则检查结果（{len(checks)}条）：\n" + \
             ("\n".join(f"- [{x['level']}] {x['message']}" for x in checks) or "无问题")
    mock_risk = "high" if any(x["level"] == "error" for x in checks) else ("medium" if any(x["level"] == "warn" for x in checks) else "low")
    try:
        res = call_json("dq.review", system=DQ_SYSTEM, user=prompt, prompt_version="dq-v1-20261006",
                        mock=lambda: {"risk": mock_risk, "summary": "（离线模拟）规则检查" + ("通过" if mock_risk == "low" else "发现待核实项"), "focus": [x["message"] for x in checks[:2]]},
                        validate=lambda d: {"risk": d.get("risk") if d.get("risk") in ("low", "medium", "high") else "medium",
                                            "summary": str(d.get("summary") or "")[:200], "focus": [str(x) for x in (d.get("focus") or [])][:3]},
                        project_id=lg["project_id"], user_id=user_id, max_tokens=400, input_meta={"log_id": log_id})
        ai = res.data
    except AIError as exc:
        ai = {"risk": mock_risk, "summary": f"AI预审不可用（{exc}），按规则结论", "focus": []}
    # 规则发现错误时 AI 不能降级为低风险
    if any(x["level"] == "error" for x in checks):
        ai["risk"] = "high"
    elif ai["risk"] == "low" and any(x["level"] == "warn" for x in checks):
        ai["risk"] = "medium"
    dq = {"score": score, **ai, "checks": checks, "at": now_iso()}
    conn.execute("UPDATE logs SET dq_json=? WHERE id=?", (dumps(dq), log_id))
    if ai["risk"] == "high":
        findings.create(conn, lg["project_id"], "dq", "log_risk", "medium", f"日志 L{log_id}（{lg['log_date']}）数据审核未通过：{ai['summary']}",
                        {"checks": checks[:6]}, ref_type="log", ref_id=log_id, fingerprint=f"dq:{log_id}")
    return dq


def missing_logs(conn, project_id: int, day: str) -> list[dict]:
    """当天没有任何现场记录（随手记或日志）的活跃技术员。"""
    return all_(conn, """SELECT u.id, u.name FROM users u WHERE u.project_id=? AND u.role='tech' AND u.active=1
                         AND NOT EXISTS (SELECT 1 FROM notes n WHERE n.author_id=u.id AND n.note_date=?)
                         AND NOT EXISTS (SELECT 1 FROM logs l WHERE l.author_id=u.id AND l.log_date=? AND l.mode='ai' AND l.period='day'
                                         AND (l.status IN ('submitted','approved') OR l.auto=1))""", (project_id, day, day))


def quality_from_log(conn, lg: dict) -> int:
    """日志中的质量/安全问题同步为对应 Agent 的待办（人报的问题与 AI 发现的并轨管理）。
    安全问题（D11）：14 天内已有同一问题未闭环的只并入该条（findings.merge_log_issue），不新建、不生成候选、不需要确认。"""
    c = loads(lg["content_json"], {})
    n = 0
    rs = None
    for k, i in enumerate(c.get("issues", [])):
        agent = {"质量": "quality", "安全": "safety"}.get(i.get("type"))
        if not agent:
            continue
        body = {"measure": i.get("measure"), "log_id": lg["id"]}
        if agent == "safety":
            if not one(conn, "SELECT 1 FROM findings WHERE project_id=? AND fingerprint=?", (lg["project_id"], f"rep:{lg['id']}:{k}")):
                rs = rs or findings._resolver(conn, lg["project_id"])
                if findings.merge_log_issue(conn, lg, k, i, rs):
                    continue
            cat = findings.category_of_text(i.get("desc"))
            if cat:
                body["category"] = cat
        lvl = {"高": "high", "中": "medium", "低": "low"}.get(i.get("severity"), "medium")
        fid = findings.create(conn, lg["project_id"], agent, "reported", lvl, re.sub(r"\s+", " ", i.get("desc", ""))[:120],
                              body, ref_type="log", ref_id=lg["id"],
                              activity_code=i.get("activity_code") or None, fingerprint=f"rep:{lg['id']}:{k}",
                              assignee_id=lg["author_id"], due_days=2, confirmed_by=lg["author_id"])  # 人写的日志上报：创建即视为已确认
        n += 1 if fid else 0
    return n
