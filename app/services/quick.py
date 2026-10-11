"""一线减压三件套的后端逻辑：扫码免账号随手拍、弱网补传的幂等、拍完即时反馈的识别状态。

扫码上报只"收"不"给"：公开页面和匿名接口只能提交照片，成功后仅回"已收到"与回执号，读不到任何项目数据。
提交的照片归到生成二维码的安全员名下，沿用安全Agent识别并生成隐患（来源标注"扫码"），
由本单位安全员 / 港湾安全主管在原有隐患流程里认领、处置；AI 只发现，不替人闭环。
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import logging
import re
import secrets
import socket
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from html import escape

from PIL import Image, UnidentifiedImageError

from .. import roles as R
from ..config import settings
from ..db import all_, audit, dumps, event, insert, loads, now_iso, one, tx
from . import findings as find_svc
from . import media as media_svc

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
QR_ROLES = ("sub_safety", "safety_sup", "manager")  # 可生成/管理二维码、认领扫码上报的岗位
MAX_PHOTOS = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_BODY = MAX_PHOTOS * MAX_IMAGE_BYTES + 256 * 1024
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")
DEFAULT_DAYS, MAX_DAYS = 30, 90
PER_TOKEN_HOUR = 12      # 每个二维码每小时最多收几次提交
PER_TOKEN_DAY = 60
PER_IP_HOUR = 30         # 同一来源地址每小时最多提交几次（跨二维码）
BAD_TOKEN_10MIN = 40     # 同一来源地址 10 分钟内错误令牌次数上限，防猜测


class QuickError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


# ----------------------------------------------------------------------------- 限流（进程内滑动窗口，重启清零；每码上限另以数据库计数为准）

class _Limiter:
    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def count(self, key: str, seconds: float) -> int:
        now = time.monotonic()
        with self.lock:
            q = self.hits.get(key)
            if not q:
                return 0
            while q and now - q[0] > seconds:
                q.popleft()
            if not q:
                self.hits.pop(key, None)
            return len(q)

    def add(self, key: str) -> None:
        with self.lock:
            self.hits[key].append(time.monotonic())
            if len(self.hits) > 20000:  # 防止被海量来源撑爆内存：整体丢弃最旧的一半
                for k in list(self.hits)[:10000]:
                    self.hits.pop(k, None)

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()


LIMITER = _Limiter()


def ip_key(ip: str) -> str:
    return hashlib.sha256((settings.secret + "|" + (ip or "")).encode()).hexdigest()[:16]


def bad_token_blocked(ip: str) -> bool:
    return LIMITER.count("bad:" + ip_key(ip), 600) >= BAD_TOKEN_10MIN


def note_bad_token(ip: str) -> None:
    LIMITER.add("bad:" + ip_key(ip))


# ----------------------------------------------------------------------------- 二维码令牌

def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def token_status(row: dict) -> str:
    if row.get("revoked_at"):
        return "revoked"
    exp = _parse(row["expires_at"])
    return "expired" if not exp or exp <= _now() else "active"


def scope_clause(user: dict) -> tuple[str, list]:
    """令牌可见范围：分包安全员只看本单位生成的；港湾岗位看全项目。"""
    if R.is_sub(user["role"]):
        return " AND t.org='sub' AND t.company=?", [user.get("company") or ""]
    return "", []


def create_token(conn, user: dict, label: str, days: int | None) -> dict:
    label = re.sub(r"\s+", " ", (label or "").strip())[:40]
    if not label:
        raise QuickError(400, "请写明二维码对应的部位或说明")
    days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    now = _now()
    tid = insert(conn, "quick_tokens", {
        "project_id": user["project_id"], "token": secrets.token_urlsafe(18), "label": label, "created_by": user["id"],
        "org": "sub" if R.is_sub(user["role"]) else "harbour", "company": (user.get("company") or "") if R.is_sub(user["role"]) else "",
        "created_at": _iso(now), "expires_at": _iso(now + timedelta(days=days))})
    audit(conn, user["id"], "quick.qr.create", f"qr:{tid}", label)
    return token_view(conn, one(conn, "SELECT * FROM quick_tokens WHERE id=?", (tid,)))


def token_view(conn, t: dict) -> dict:
    st = one(conn, "SELECT COUNT(*) n, SUM(status='new') new, MAX(created_at) last FROM quick_submissions WHERE token_id=?", (t["id"],))
    by = one(conn, "SELECT name FROM users WHERE id=?", (t["created_by"],))
    return {"id": t["id"], "label": t["label"], "path": f"/q/{t['token']}", "status": token_status(t), "created_at": t["created_at"],
            "expires_at": t["expires_at"], "revoked_at": t["revoked_at"], "created_by": by["name"] if by else "",
            "company": t["company"], "submissions": st["n"] or 0, "new": int(st["new"] or 0), "last_at": st["last"]}


def list_tokens(conn, user: dict) -> list[dict]:
    clause, params = scope_clause(user)
    rows = all_(conn, f"SELECT t.* FROM quick_tokens t WHERE t.project_id=?{clause} ORDER BY t.id DESC LIMIT 100", [user["project_id"], *params])
    return [token_view(conn, t) for t in rows]


def _token_in_scope(conn, user: dict, tid: int) -> dict:
    clause, params = scope_clause(user)
    t = one(conn, f"SELECT t.* FROM quick_tokens t WHERE t.id=? AND t.project_id=?{clause}", [tid, user["project_id"], *params])
    if not t:
        raise QuickError(404, "二维码不存在")
    return t


def revoke_token(conn, user: dict, tid: int) -> dict:
    t = _token_in_scope(conn, user, tid)
    if not t["revoked_at"]:
        conn.execute("UPDATE quick_tokens SET revoked_at=?, revoked_by=? WHERE id=?", (now_iso(), user["id"], tid))
        audit(conn, user["id"], "quick.qr.revoke", f"qr:{tid}", t["label"])
    return token_view(conn, one(conn, "SELECT * FROM quick_tokens WHERE id=?", (tid,)))


def find_token(conn, token: str) -> dict | None:
    """只返回当前可用的令牌（格式正确、未过期、未作废、生成人仍在岗）；其余情形调用方一律当"无效"处理，不区分原因。"""
    if not TOKEN_RE.match(token or ""):
        return None
    t = one(conn, "SELECT t.* FROM quick_tokens t JOIN users u ON u.id=t.created_by AND u.active=1 WHERE t.token=?", (token,))
    return t if t and token_status(t) == "active" else None


# ----------------------------------------------------------------------------- 二维码网址：本机局域网地址建议 + 管理员设置的部署地址

SETTINGS_SQL = "CREATE TABLE IF NOT EXISTS quick_settings (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
BASE_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+$")   # 只要“协议://主机[:端口]”，不带路径
_LAN_CACHE: dict[str, tuple[float, str | None]] = {}
LAN_TTL = 60.0


def _ip_candidates() -> list[str]:
    """本机 IPv4 候选：先用 UDP connect 取默认出口网卡地址（不发包），再补主机名解析到的地址。"""
    out: list[str] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        out.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        out += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return out


def lan_ip() -> str | None:
    """本机非回环 IPv4（优先私网地址）；取不到返回 None（不猜）。结果缓存 60 秒。"""
    hit = _LAN_CACHE.get("ip")
    if hit and time.monotonic() - hit[0] < LAN_TTL:
        return hit[1]
    good: list[ipaddress.IPv4Address] = []
    for raw in _ip_candidates():
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        # 只认 RFC1918 局域网段：VPN/代理虚拟网卡（198.18/15）、CGNAT（100.64/10）、公网地址手机多半连不上，宁可不给
        if ip.version == 4 and any(ip in ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
            good.append(ip)
    best = good[0] if good else None
    val = str(best) if best else None
    _LAN_CACHE["ip"] = (time.monotonic(), val)
    return val


def lan_base(scheme: str, port: int | None) -> str:
    ip = lan_ip()
    if not ip:
        return ""
    return f"{scheme}://{ip}" + (f":{port}" if port else "")


def get_base(conn) -> str:
    """系统管理员设置的二维码网址（部署域名或固定局域网地址）；未设置返回空串。"""
    try:
        row = one(conn, "SELECT v FROM quick_settings WHERE k='qr_base'")
    except sqlite3.OperationalError:  # 表尚未建（旧库未重启迁移）
        return ""
    return row["v"] if row else ""


def set_base(conn, user: dict, base: str) -> str:
    base = (base or "").strip().rstrip("/")
    if base and not BASE_RE.match(base):
        raise QuickError(400, "请填写“http(s)://地址:端口”，不带路径")
    conn.execute(SETTINGS_SQL)
    if base:
        conn.execute("INSERT INTO quick_settings(k, v) VALUES('qr_base', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (base,))
    else:
        conn.execute("DELETE FROM quick_settings WHERE k='qr_base'")
    audit(conn, user["id"], "qr.base", "quick", base or "（清空，按本机局域网地址自动生成）")
    return base


# ----------------------------------------------------------------------------- 匿名提交

def _clean(s: str | None, n: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", (s or "")).strip()[:n]


def check_image(raw: bytes) -> None:
    if not raw:
        raise QuickError(400, "有一张照片是空的，请重拍")
    if len(raw) > MAX_IMAGE_BYTES:
        raise QuickError(413, "单张照片不超过 10MB")
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = img.format
    except (UnidentifiedImageError, OSError):
        raise QuickError(400, "只能上传照片（JPG/PNG/WEBP）") from None
    if fmt not in ALLOWED_FORMATS:
        raise QuickError(400, "只能上传照片（JPG/PNG/WEBP）")


def _receipt(conn) -> str:
    for _ in range(5):
        r = f"Q{datetime.now():%y%m%d}-{secrets.token_hex(3).upper()}"
        if not one(conn, "SELECT 1 FROM quick_submissions WHERE receipt=?", (r,)):
            return r
    raise QuickError(500, "回执号生成失败，请重试")


def _rate_check(conn, t: dict) -> None:
    now = _now()
    h = one(conn, "SELECT COUNT(*) n FROM quick_submissions WHERE token_id=? AND created_at>=?", (t["id"], _iso(now - timedelta(hours=1))))["n"]
    d = one(conn, "SELECT COUNT(*) n FROM quick_submissions WHERE token_id=? AND created_at>=?", (t["id"], _iso(now - timedelta(days=1))))["n"]
    if h >= PER_TOKEN_HOUR or d >= PER_TOKEN_DAY:
        raise QuickError(429, "这个二维码提交得太频繁了，请稍后再试，或直接告诉安全员")


def submit(conn, t: dict, uploads: list[bytes], name: str, team: str, note: str, cid: str, ip: str) -> dict:
    """匿名提交 1~4 张照片。幂等：同一令牌下相同 cid 重复提交返回原回执。返回 {receipt, media_ids, submission_id, dup}。"""
    cid = _clean(cid, 64)
    if cid:
        ex = one(conn, "SELECT id, receipt, media_json FROM quick_submissions WHERE token_id=? AND client_id=?", (t["id"], cid))
        if ex:
            return {"receipt": ex["receipt"], "media_ids": [], "submission_id": ex["id"], "dup": True}
    if LIMITER.count("sub:" + ip_key(ip), 3600) >= PER_IP_HOUR:
        raise QuickError(429, "提交得太频繁了，请稍后再试")
    LIMITER.add("sub:" + ip_key(ip))  # 失败的尝试也计数，防止拿垃圾文件反复试
    if not 1 <= len(uploads) <= MAX_PHOTOS:
        raise QuickError(400, f"请拍 1~{MAX_PHOTOS} 张照片")
    for raw in uploads:
        check_image(raw)
    _rate_check(conn, t)
    receipt = _receipt(conn)
    media_ids = []
    for i, raw in enumerate(uploads, 1):
        try:
            m = media_svc.save_photo(conn, t["project_id"], t["created_by"], f"扫码_{receipt}_{i}.jpg", raw, "safety",
                                     geo=media_svc.parse_geo(None, None, None, t["label"]))
        except media_svc.MediaError as exc:
            raise QuickError(400, str(exc)) from None
        media_ids.append(m["id"])
    with tx(conn):
        _rate_check(conn, t)  # 事务内复核，防并发突破上限
        sid = insert(conn, "quick_submissions", {
            "project_id": t["project_id"], "token_id": t["id"], "receipt": receipt, "client_id": cid or None,
            "reporter_name": _clean(name, 20), "reporter_team": _clean(team, 20), "note": _clean(note, 200),
            "media_json": dumps(media_ids), "status": "new", "ip_hash": ip_key(ip), "created_at": now_iso()})
        insert(conn, "notifications", {
            "project_id": t["project_id"], "user_id": t["created_by"], "alert_id": None, "title": f"扫码上报：{t['label']}",
            "body": f"现场人员通过二维码提交了 {len(media_ids)} 张照片（回执 {receipt}），安全Agent 正在识别，请到「现场扫码」认领。",
            "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(),
            "link_json": dumps({"route": "qrcodes", "params": {}})})  # 点消息直达「现场扫码」认领
        event(conn, "quick.submit", project_id=t["project_id"], user_id=t["created_by"], ref_id=sid, value=len(media_ids))
    return {"receipt": receipt, "media_ids": media_ids, "submission_id": sid, "dup": False}


def scan_in_background(media_ids: list[int], creator_id: int, submission_id: int) -> None:
    """后台识别（沿用安全Agent），随后给生成的隐患标注来源"扫码"与提交人信息。失败只记日志，照片与提交记录不受影响。"""
    from ..db import connect
    from . import site_agents
    conn = connect()
    try:
        site_agents.safety_scan(conn, media_ids, creator_id)
        tag_findings(conn, submission_id)
    except Exception:  # noqa: BLE001
        log.exception("扫码上报识别失败")
    finally:
        # 识别完成后把本次新存的扫码照片从"随手拍"归回普通照片：它们不是安全员本人所拍，不应以其名义出现在现场圈动态里
        try:
            mark = ",".join("?" for _ in media_ids)
            conn.execute(f"UPDATE media SET purpose='log' WHERE id IN ({mark}) AND purpose='safety' AND substr(original_name,1,3)='扫码_'", media_ids)
        except Exception:  # noqa: BLE001
            log.exception("扫码照片归类失败")
        conn.close()


def tag_findings(conn, submission_id: int) -> None:
    s = one(conn, "SELECT * FROM quick_submissions WHERE id=?", (submission_id,))
    ids = loads(s["media_json"], []) if s else []
    if not ids:
        return
    mark = ",".join("?" for _ in ids)
    who = " · ".join(x for x in (s["reporter_team"], s["reporter_name"]) if x)
    for f in all_(conn, f"SELECT id, body_json FROM findings WHERE project_id=? AND agent='safety' AND ref_type='media' AND ref_id IN ({mark})", (s["project_id"], *ids)):
        body = loads(f["body_json"], {})
        body.update({"source": "扫码", "receipt": s["receipt"], "reporter": who, "report_note": s["note"]})
        conn.execute("UPDATE findings SET body_json=? WHERE id=?", (dumps(body), f["id"]))


# ----------------------------------------------------------------------------- 待认领

def _scan_state(statuses: list[str], n_find: int) -> str:
    if any(s == "pending" for s in statuses):
        return "pending"
    if any(s == "failed" for s in statuses):
        return "failed"
    if statuses and all(s == "skipped" for s in statuses):
        return "skipped"
    return "hazard" if n_find else "clear"


def _findings_of_media(conn, project_id: int, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    mark = ",".join("?" for _ in ids)
    return all_(conn, f"SELECT * FROM findings WHERE project_id=? AND agent='safety' AND ref_type='media' AND ref_id IN ({mark}) ORDER BY id", (project_id, *ids))


def submission_view(conn, s: dict, label: str, names: dict[int, str]) -> dict:
    ids = loads(s["media_json"], [])
    ms = [one(conn, "SELECT id, safety_status FROM media WHERE id=?", (i,)) for i in ids]
    ms = [m for m in ms if m]
    fs = _findings_of_media(conn, s["project_id"], ids)
    return {"id": s["id"], "receipt": s["receipt"], "label": label, "name": s["reporter_name"], "team": s["reporter_team"], "note": s["note"],
            "status": s["status"], "created_at": s["created_at"], "claimed_by": names.get(s["claimed_by"]), "claimed_at": s["claimed_at"],
            "photos": [{"id": m["id"], "thumb": f"/api/media/{m['id']}/thumb", "url": f"/api/media/{m['id']}/file"} for m in ms],
            "scan": _scan_state([m["safety_status"] for m in ms], len(fs)),
            "findings": [{"id": f["id"], "title": f["title"], "level": f["level"], "level_name": find_svc.LEVEL_NAME[f["level"]],
                          "status": f["status"], "status_name": find_svc.status_label(f), "candidate": find_svc.is_candidate(f),
                          "assignee_id": f["assignee_id"], "due_date": f["due_date"], "category": loads(f["body_json"], {}).get("category", ""),
                          "desc": loads(f["body_json"], {}).get("desc", ""), "measure": loads(f["body_json"], {}).get("measure", "")} for f in fs]}


def list_submissions(conn, user: dict, status: str = "new", limit: int = 60) -> list[dict]:
    clause, params = scope_clause(user)
    q = f"SELECT s.*, t.label FROM quick_submissions s JOIN quick_tokens t ON t.id=s.token_id WHERE s.project_id=?{clause}"
    p = [user["project_id"], *params]
    if status in ("new", "claimed", "invalid"):
        q += " AND s.status=?"
        p.append(status)
    rows = all_(conn, q + " ORDER BY s.id DESC LIMIT ?", [*p, limit])
    names = {u["id"]: u["name"] for u in all_(conn, "SELECT id, name FROM users WHERE project_id=?", (user["project_id"],))}
    return [submission_view(conn, s, s["label"], names) for s in rows]


def _submission_in_scope(conn, user: dict, sid: int) -> dict:
    clause, params = scope_clause(user)
    s = one(conn, f"SELECT s.* FROM quick_submissions s JOIN quick_tokens t ON t.id=s.token_id WHERE s.id=? AND s.project_id=?{clause}", [sid, user["project_id"], *params])
    if not s:
        raise QuickError(404, "上报记录不存在")
    return s


def _one_view(conn, user: dict, sid: int) -> dict:
    s = one(conn, "SELECT s.*, t.label FROM quick_submissions s JOIN quick_tokens t ON t.id=s.token_id WHERE s.id=?", (sid,))
    names = {u["id"]: u["name"] for u in all_(conn, "SELECT id, name FROM users WHERE project_id=?", (user["project_id"],))}
    return submission_view(conn, s, s["label"], names)


def claim(conn, user: dict, sid: int) -> dict:
    """认领：记录由谁接手。分包安全员认领时，名下尚未处理的隐患改派给本人（仍在本单位内）；港湾岗位认领只做标记，不改责任人。"""
    s = _submission_in_scope(conn, user, sid)
    if s["status"] != "new":
        raise QuickError(400, "这条上报已处理过")
    now = now_iso()
    with tx(conn):
        conn.execute("UPDATE quick_submissions SET status='claimed', claimed_by=?, claimed_at=? WHERE id=?", (user["id"], now, sid))
        audit(conn, user["id"], "quick.claim", f"quick:{sid}", s["receipt"])
    if R.is_sub(user["role"]):
        from . import role_views
        for f in _findings_of_media(conn, s["project_id"], loads(s["media_json"], [])):
            if f["status"] in ("open", "rejected") and f["assignee_id"] != user["id"] and not find_svc.is_candidate(f):  # 候选须先确认，认领不改派
                try:
                    role_views.assign_finding(conn, f["id"], user, user["id"])
                except (ValueError, PermissionError):
                    pass  # 改派不成功不影响认领，仍可在隐患台账里手动派单
    return _one_view(conn, user, sid)


def mark_invalid(conn, user: dict, sid: int) -> dict:
    s = _submission_in_scope(conn, user, sid)
    if s["status"] != "new":
        raise QuickError(400, "这条上报已处理过")
    conn.execute("UPDATE quick_submissions SET status='invalid', claimed_by=?, claimed_at=? WHERE id=?", (user["id"], now_iso(), sid))
    audit(conn, user["id"], "quick.invalid", f"quick:{sid}", s["receipt"])
    return _one_view(conn, user, sid)


def pending_count(conn, user: dict) -> int:
    clause, params = scope_clause(user)
    return one(conn, f"SELECT COUNT(*) n FROM quick_submissions s JOIN quick_tokens t ON t.id=s.token_id WHERE s.project_id=? AND s.status='new'{clause}",
               [user["project_id"], *params])["n"]


# ----------------------------------------------------------------------------- 拍完即时反馈：识别状态

def media_status(conn, user: dict, ids: list[int]) -> dict:
    """本人（分包安全员含本单位同事）照片的安全识别进展与结果摘要，供拍照后的反馈气泡轮询。只读。"""
    ids = [int(i) for i in ids][:20]
    out, kept = [], []
    for mid in ids:
        m = one(conn, "SELECT id, uploader_id, safety_status, purpose FROM media WHERE id=? AND project_id=?", (mid, user["project_id"]))
        if not m:
            continue
        if m["uploader_id"] != user["id"]:
            from . import role_views
            if not (R.is_sub(user["role"]) and role_views.in_scope(conn, user, m["uploader_id"])):
                continue
        kept.append(m["id"])
        out.append({"id": m["id"], "status": m["safety_status"]})
    fs = _findings_of_media(conn, user["project_id"], kept)
    scenes = []
    for f in fs:
        sc = (loads(f["body_json"], {}).get("scene") or "").strip()
        if sc and sc not in scenes:
            scenes.append(sc)
    return {"items": out, "pending": sum(1 for o in out if o["status"] == "pending"), "failed": sum(1 for o in out if o["status"] == "failed"),
            "scenes": scenes[:3],
            "findings": [{"id": f["id"], "title": f["title"], "level": f["level"], "level_name": find_svc.LEVEL_NAME[f["level"]], "status": f["status"],
                          "candidate": find_svc.is_candidate(f), "category": loads(f["body_json"], {}).get("category", ""), "measure": loads(f["body_json"], {}).get("measure", "")} for f in fs]}


# ----------------------------------------------------------------------------- 弱网补传：幂等

def clean_cid(cid: str | None) -> str:
    return _clean(cid, 64)


def idem_begin(conn, user_id: int, cid: str, kind: str) -> tuple[str, dict | None]:
    """返回 ("new", None) 可继续处理；("done", 结果) 已处理过；("busy", None) 同一条正在处理。
    同一个 client_id 已用于另一类记录（如先传照片、又拿来提交整改）时拒绝，不把别的结果当成这次的结果返回。"""
    cid = clean_cid(cid)
    if not cid:
        raise QuickError(400, "缺少 client_id")
    with tx(conn):
        ex = one(conn, "SELECT * FROM quick_idem WHERE user_id=? AND client_id=?", (user_id, cid))
        if ex:
            if ex["kind"] != kind:
                raise QuickError(400, "client_id 已用于另一条记录，请换一个编号重新提交")
            if ex["result_json"]:
                return "done", loads(ex["result_json"])
            if (_parse(ex["created_at"]) or _now()) > _now() - timedelta(minutes=2):
                return "busy", None
            conn.execute("DELETE FROM quick_idem WHERE user_id=? AND client_id=?", (user_id, cid))  # 上次处理中途中断，接管重做
        conn.execute("INSERT INTO quick_idem (user_id, client_id, kind, result_json, created_at) VALUES (?,?,?,?,?)", (user_id, cid, kind, "", now_iso()))
    return "new", None


def idem_done(conn, user_id: int, cid: str, result: dict) -> None:
    conn.execute("UPDATE quick_idem SET result_json=? WHERE user_id=? AND client_id=?", (dumps(result), user_id, clean_cid(cid)))


def idem_abort(conn, user_id: int, cid: str) -> None:
    conn.execute("DELETE FROM quick_idem WHERE user_id=? AND client_id=? AND result_json=''", (user_id, clean_cid(cid)))


def note_stamp(client_time: str | None) -> tuple[str, str] | None:
    """离线记录（随手记、录音）的真实时间，返回(日期, 本地 ISO 时间)：补传一律按记录时间入账，不论晚到多少天（P0-1，已确认）；
    只有晚于服务器时间 5 分钟以上的“未来时间”不可信，返回 None，由调用方按服务器收到的时间。"""
    if not client_time:
        return None
    try:
        t = datetime.fromisoformat(client_time.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    t = t.astimezone()
    now = _now()
    if t > now + timedelta(minutes=5):
        return None
    t = min(t, now)
    return t.date().isoformat(), _iso(t)


def _day(s: str | None) -> date | None:
    try:
        t = datetime.fromisoformat(s) if s else None
    except ValueError:
        return None
    if t is None:
        return None
    return (t.astimezone() if t.tzinfo else t).date()


def late_days(recorded: str | None, received: str | None) -> int:
    """迟到天数＝服务器收到那天减记录那天（按日历日）；任一缺失按 0。≥1 时前端标“迟到补传 N 天”。"""
    a, b = _day(recorded), _day(received)
    return max(0, (b - a).days) if a and b else 0


def reuse_rectify_photo(conn, media_id: int, purpose: str, scan: bool) -> bool:
    """整改照片被拒（N-8：拍摄早于派单）后“改作随手拍”：同一张照片按内容去重会拿回那条整改照片记录，
    它没挂到任何整改上时改成这次的用途（进识别队列）；已挂到整改上的是整改证据，不动。返回是否改了。"""
    if one(conn, "SELECT 1 FROM findings WHERE rectify_media_id=?", (media_id,)):
        return False
    cur = conn.execute("UPDATE media SET purpose=?, safety_status=? WHERE id=? AND purpose='rectify'",
                       (purpose, "pending" if scan else "skipped", media_id))
    return cur.rowcount == 1


def late_notes(conn, user: dict, days: int = 21) -> list[dict]:
    """D5/AC-3.2：最近 days 天里到达的、需要技术员留意的随手记。两类：
    ① 迟到补传：收到那天晚于记录那天（late_days≥1）；
    ② 没赶上确认：所属周（按日确认的项目为当天）在它进入随手记之前就已确认（week_confirmed，含确认后补记，F-12）。
    已确认的数量不动（每日日志已提交时整理直接跳过；正式工程量只来自确认那一刻）；②由技术员在下一次周确认的“迟到记录”区决定是否补入。
    收到时间取 events(note.add)；语音取服务器收到录音的时间（voice_pending.received_at），不取转写完成的时间。
    “确认之前/之后”按事件先后（events 自增编号：note.add 与最近一次 log.submit）判断，不受同一秒的影响；没有提交事件的旧数据按时间比。"""
    from . import logs as log_svc
    days = max(1, min(int(days or 21), 90))
    rows = all_(conn, """SELECT n.id, n.note_date, n.text, n.created_at, MIN(e.id) seq, MIN(e.created_at) added_at, v.received_at
                         FROM events e JOIN notes n ON n.id=e.ref_id
                         LEFT JOIN voice_pending v ON v.note_id=n.id
                         WHERE e.kind='note.add' AND e.user_id=? AND e.created_at>=? AND n.author_id=? AND n.project_id=?
                         GROUP BY n.id""", (user["id"], _iso(_now() - timedelta(days=days)), user["id"], user["project_id"]))
    weekly = log_svc.weekly_mode(conn, user["project_id"])
    confirmed: dict[str, dict | None] = {}

    def confirmation(day: str) -> dict | None:
        """所属周（或当天）现在的确认：{seq: 最近一次提交事件编号, at: 提交时间}；未确认（草稿、退回）为 None。"""
        key = log_svc.week_of(day)[0] if weekly else day
        if key not in confirmed:
            lg = one(conn, """SELECT id, submitted_at FROM logs WHERE project_id=? AND author_id=? AND period=? AND log_date=?
                              AND status IN ('submitted','approved') ORDER BY submitted_at DESC LIMIT 1""",
                     (user["project_id"], user["id"], "week" if weekly else "day", key))
            ev = one(conn, "SELECT MAX(id) seq FROM events WHERE kind='log.submit' AND ref_id=?", (lg["id"],)) if lg else None
            confirmed[key] = {"seq": ev["seq"] if ev else None, "at": lg["submitted_at"]} if lg else None
        return confirmed[key]

    out = []
    for r in rows:
        received = r["received_at"] or r["added_at"]
        late = late_days(r["note_date"], received)
        c = confirmation(r["note_date"])
        if not c:
            missed = False
        elif c["seq"]:
            missed = r["seq"] > c["seq"]
        else:
            missed = bool(_parse(c["at"]) and _parse(r["added_at"]) and _parse(c["at"]) < _parse(r["added_at"]))
        if late < 1 and not missed:
            continue
        out.append({"note_id": r["id"], "note_date": r["note_date"], "received_date": _day(received).isoformat(), "late_days": late,
                    "week_confirmed": missed, "text": r["text"], "time": r["created_at"][11:16]})
    return sorted(out, key=lambda x: (x["note_date"], x["note_id"]))


# ----------------------------------------------------------------------------- 公开页面

# 工人扫码页（设计 19，RJ 10-10）：中港蓝顶栏 + 短装饰横幅（项目名取真实配置，横幅图只是装饰、不是现场证据）+ 一张上报卡。
# 免登录、不算岗位、只写不读：不放登录、语言切换或任何内部列表；没有照片时提交按钮禁用；触控目标不小于 44px，360–430 宽单列无横向溢出。
_PUB_CSS = """
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:#eef3fa;color:#13254d;font:16px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.top{background:linear-gradient(180deg,#183a86,#14316f);color:#fff;padding:calc(10px + env(safe-area-inset-top)) 16px 10px}
.top-in{max-width:480px;margin:0 auto;display:flex;align-items:center;gap:10px}
.top img{width:36px;height:36px;flex:none}.top b{display:block;font-size:18px;letter-spacing:.08em;line-height:1.2}
.top small{display:block;font-size:12px;color:#c3d4f3;letter-spacing:.04em}
.w{max-width:480px;margin:0 auto;padding:12px 16px calc(28px + env(safe-area-inset-bottom))}
.hero{position:relative;height:84px;border-radius:12px;overflow:hidden;color:#fff;
 background:linear-gradient(90deg,rgba(14,36,92,.94) 0%,rgba(24,58,134,.78) 52%,rgba(24,58,134,.15) 100%),url(/static/brand/hero-bridge.jpg) center/cover no-repeat}
.hero-tx{position:absolute;left:16px;right:16px;top:50%;transform:translateY(-50%)}
.hero h1{margin:0;font-size:19px;line-height:1.3;color:#fff;overflow-wrap:anywhere}
.hero div{font-size:13px;color:#dbe7ff;margin-top:2px}
.card{background:#fff;border:1px solid #dce5f2;border-radius:12px;padding:14px;margin-top:12px}
.ch{display:flex;align-items:center;gap:10px}.ch i{flex:none;width:40px;height:40px;border-radius:10px;background:#eaf1fc;display:flex;align-items:center;justify-content:center}
.ch i svg{width:24px;height:24px;stroke:#2f6fe0}.ch h2{margin:0;font-size:18px;color:#1a398c}.sub{color:#64789b;font-size:14px}
.lab{display:inline-block;background:#eaf1fc;color:#1a398c;border-radius:6px;padding:2px 8px;font-size:14px;overflow-wrap:anywhere}
.where{margin-top:10px;font-size:14px;color:#3b5179}
.add{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;width:100%;min-height:128px;margin-top:12px;
 border:1.5px dashed #9fbbe6;border-radius:12px;background:#f5f9ff;color:#1a398c;font:inherit;cursor:pointer}
.add svg{width:40px;height:40px;stroke:#2f6fe0}.add b{font-size:17px}.add span{font-size:13px;color:#64789b}
.th{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:12px}.th:empty{display:none}
.th div{position:relative;aspect-ratio:4/3;border-radius:8px;overflow:hidden;background:#dfe7f3}.th img{width:100%;height:100%;object-fit:contain;display:block}
.th b{position:absolute;right:0;top:0;width:44px;height:44px;display:flex;align-items:center;justify-content:center;cursor:pointer}
.th b::before{content:"×";width:26px;height:26px;border-radius:50%;background:rgba(0,0,0,.6);color:#fff;text-align:center;line-height:26px;font-size:16px}
.add.more{min-height:48px;flex-direction:row;gap:8px}.add.more svg{width:22px;height:22px}.add.more span{display:none}
label.f{display:block;margin-top:12px;font-size:15px;font-weight:600;color:#13254d}label.f small{font-weight:400;color:#64789b}
input,textarea{width:100%;border:1px solid #d3dbe5;border-radius:10px;padding:11px 12px;font-size:16px;font-family:inherit;color:#13254d;margin-top:6px;background:#fff}
input{min-height:48px}textarea{min-height:88px;resize:vertical}input:focus,textarea:focus{outline:2px solid #9fbbe6;border-color:#2f6fe0}
.cnt{text-align:right;font-size:12px;color:#64789b;margin-top:2px}
.row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.tip{display:flex;gap:10px;align-items:flex-start;margin-top:14px;padding:10px 12px;border-radius:10px;background:#eaf1fc;font-size:14px;color:#3b5179}
.tip b{color:#1a398c}.tip i{flex:none;font-style:normal;width:22px;height:22px;border-radius:50%;background:#2f6fe0;color:#fff;text-align:center;line-height:22px;font-size:14px;font-weight:700}
.btn{display:block;width:100%;min-height:52px;border:0;border-radius:10px;padding:12px;font:inherit;font-size:17px;font-weight:700;background:#1a398c;color:#fff;margin-top:14px;cursor:pointer}
.btn[disabled]{background:#b8c6dd;cursor:default}.btn.alt{background:#eaf1fc;color:#1a398c}
.err{color:#c8352b;font-size:14px;margin-top:8px}.err:empty{display:none}
.ok{text-align:center;padding:24px 12px}.ok i{display:block;font-style:normal;font-size:48px;line-height:1.2;color:#23855a}.ok h1{margin:4px 0;font-size:20px}
.rc{font:600 22px/1.4 ui-monospace,Menlo,Consolas,monospace;letter-spacing:1px;color:#1a398c;overflow-wrap:anywhere}
.tiny{font-size:12.5px;color:#64789b;margin-top:12px;text-align:center}
.foot{margin-top:18px;text-align:center;font-size:12.5px;color:#64789b;letter-spacing:.3em}
"""
_CAM_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>')


def _page(title: str, body: str, script: str = "") -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><meta name="robots" content="noindex,nofollow">
<meta name="theme-color" content="#183a86"><title>{escape(title)}</title><style>{_PUB_CSS}</style></head><body>
<header class="top"><div class="top-in"><img src="/static/brand/logo-eye.png" alt=""><div><b>智眸·工笔</b><small>施工现场 AI 助手 · 中国港湾 CHEC</small></div></div></header>
<div class="w">{body}<div class="foot">包容 诚信 创新 奉献</div></div>{script}</body></html>"""


def bad_page() -> str:
    return _page("二维码无效", """<div class="card ok"><i>⚠</i><h1>二维码无效</h1>
<div class="sub">这个二维码已过期、被作废或不存在。<br>请联系现场安全员获取新的二维码。</div></div>""")


def public_page(t: dict) -> str:
    # 不显示项目名称（tests/test_quick.py 守卫：二维码外传也不暴露项目信息），横幅只写用途
    hero = """<div class="hero"><div class="hero-tx"><h1>随手拍 · 报隐患</h1><div>无需账号，只上报不查看</div></div></div>"""
    body = f"""{hero}<div id="form"><div class="card"><div class="ch"><i>{_CAM_SVG}</i><div><h2>现场问题上报</h2><div class="sub">拍照即可，安全员会收到</div></div></div>
<div class="where">上报部位：<span class="lab">{escape(t['label'])}</span></div>
<div class="th" id="th"></div>
<button class="add" id="add" type="button">{_CAM_SVG}<b>添加现场照片</b><span>支持 1–4 张</span></button>
<input id="file" type="file" accept="image/*" capture="environment" multiple hidden>
<label class="f" for="note">发生了什么？<small>（可选）</small></label>
<textarea id="note" rows="3" maxlength="200" placeholder="例：脚手架外侧没有挡脚板"></textarea><div class="cnt"><span id="nc">0</span>/200</div>
<div class="row"><div><label class="f" for="name">姓名<small>（可选）</small></label><input id="name" maxlength="20" placeholder="姓名" autocomplete="off"></div>
<div><label class="f" for="team">班组<small>（可选）</small></label><input id="team" maxlength="20" placeholder="班组" autocomplete="off"></div></div>
<div class="tip"><i>!</i><div><b>先确保自身安全，再拍照上报。</b></div></div>
<div class="err" id="err"></div>
<button class="btn" id="go" type="button" disabled>提交上报</button>
<div class="tiny">提交后由项目安全员核实处理；本页不显示项目内部信息。</div></div></div>
<div id="done" class="card ok" hidden><i>✓</i><h1>已收到</h1><div class="sub">谢谢！安全员会核实处理。回执号</div><div class="rc" id="rc"></div>
<button class="btn alt" id="again" type="button">再报一处</button></div>"""
    script = """<script>
(function(){
var MAXN=4,files=[],busy=false,cid=gid(),$=function(i){return document.getElementById(i)};
function gid(){var a=new Uint8Array(12);(window.crypto||window.msCrypto).getRandomValues(a);return Array.prototype.map.call(a,function(x){return('0'+x.toString(16)).slice(-2)}).join('')}
function draw(){var th=$('th');th.innerHTML='';files.forEach(function(f,i){var d=document.createElement('div'),im=new Image(),b=document.createElement('b');
im.src=f.url;b.textContent='×';b.onclick=function(){URL.revokeObjectURL(f.url);files.splice(i,1);draw()};d.appendChild(im);d.appendChild(b);th.appendChild(d)});
$('go').disabled=!files.length||busy;$('add').style.display=files.length>=MAXN?'none':'';$('add').className=files.length?'add more':'add'}
function shrink(f){return new Promise(function(res){
try{var im=new Image(),u=URL.createObjectURL(f);im.onload=function(){var k=Math.min(1,1600/Math.max(im.naturalWidth,im.naturalHeight)),c=document.createElement('canvas');
c.width=Math.round(im.naturalWidth*k);c.height=Math.round(im.naturalHeight*k);c.getContext('2d').drawImage(im,0,0,c.width,c.height);URL.revokeObjectURL(u);
c.toBlob(function(b){res(b&&b.size<f.size?b:f)},'image/jpeg',.82)};im.onerror=function(){URL.revokeObjectURL(u);res(f)};im.src=u}catch(e){res(f)}})}
$('add').onclick=function(){$('file').click()};
$('note').oninput=function(){$('nc').textContent=this.value.length};
$('file').onchange=function(){var l=[].slice.call(this.files);this.value='';$('err').textContent='';
l.slice(0,MAXN-files.length).forEach(function(f){files.push({blob:f,url:URL.createObjectURL(f)})});draw()};
$('go').onclick=function(){if(busy||!files.length)return;busy=true;$('go').disabled=true;$('go').textContent='提交中…';$('err').textContent='';
Promise.all(files.map(function(f){return shrink(f.blob)})).then(function(bl){var fd=new FormData();
bl.forEach(function(b,i){fd.append('files',b,'p'+(i+1)+'.jpg')});
fd.append('note',$('note').value);fd.append('team',$('team').value);fd.append('name',$('name').value);fd.append('cid',cid);
return fetch(location.pathname.replace(/\\/$/,'')+'/submit',{method:'POST',body:fd,headers:{'x-zb':'1'}})})
.then(function(r){return r.json().catch(function(){return{}}).then(function(j){if(!r.ok)throw new Error(j.detail||'提交失败（'+r.status+'）');return j})})
.then(function(j){$('form').hidden=true;$('done').hidden=false;$('rc').textContent=j.receipt||''})
.catch(function(e){$('err').textContent=(e&&e.message&&e.message!=='Failed to fetch'?e.message:'网络不好，请稍后再点一次提交（照片不会重复）')})
.then(function(){busy=false;$('go').textContent='提交上报';draw()})};
$('again').onclick=function(){files.forEach(function(f){URL.revokeObjectURL(f.url)});files=[];cid=gid();$('note').value='';$('nc').textContent='0';$('done').hidden=true;$('form').hidden=false;draw()};
draw()})();
</script>"""
    return _page("现场问题上报", body, script)
