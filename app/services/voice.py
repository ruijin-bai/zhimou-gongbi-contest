"""离线语音补传（P0-1）：录音先到服务器暂存，后台转写成随手记。

- 录音单独放在 voice_pending（建表在 routers/quick.py 的 SCHEMA）：转写完成前不进 notes，读随手记的各处（整理日志、归类、现场圈）一律不用改；
- 随手记的时间取录音时间（note_date＝录音那天，created_at＝录音时刻），不是上传或转写的时间；
- 后台转写先原子认领（pending→running）；停在 running 超过 10 分钟视为中断，可重新认领。认领次数 tries 兼作令牌：
  被接管的旧任务回来时写不进结果，同一段录音只会成一条随手记；
- 录音一律保留（RJ 2026-10-09：防止没网、识别不准时还能回听；取代 D13 的“转写成功即删、失败留 7 天”）：
  转写成功后音频仍在，随手记旁可回放；失败的可重新转写或放弃。只有本人放弃、或删掉对应随手记时才删音频；
- 拍照时同时录的语音（note_cid＝那条照片随手记的 client_id）：转写后把文字补进那条随手记，不另起一条；
  那条随手记不存在（照片被放弃、未上传成功、已删除）时，照常单独成一条语音随手记。
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from .. import roles as R
from ..ai.client import AIError, transcribe
from ..config import settings
from ..db import all_, audit, connect, insert, loads, one, tx
from . import logs as log_svc
from .quick import QuickError, clean_cid, late_days, note_stamp

log = logging.getLogger(__name__)

MAX_AUDIO = 25 * 1024 * 1024
KEEP_DAYS = 7             # 录音目录里没有记录引用的孤儿文件，多少天后清理（有记录的录音一律保留）
STALE_MINUTES = 10        # running 超过这么久视为中断，可重新认领
REQUEUE_SECONDS = 60      # pending 搁置超过这么久（多为服务重启丢了后台任务），打开列表时重新排队
FORMATS = "webm、ogg、m4a、mp3、wav、aac、flac"
_NOT_AUDIO_BRANDS = (b"3g", b"hei", b"hev", b"mif1", b"msf1", b"avif", b"avis")  # 同为 ftyp 容器的 3GP 录音、HEIC/AVIF 照片


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def voice_dir() -> Path:
    return settings.data_dir / "voice"


def _abs(rel: str | None) -> Path | None:
    """库里记的相对路径 → 绝对路径；不在录音目录下的一律不认。"""
    if not rel:
        return None
    p = (settings.data_dir / rel).resolve()
    return p if voice_dir().resolve() in p.parents else None


def _unlink(rel: str | None) -> None:
    p = _abs(rel)
    if p:
        try:
            p.unlink(missing_ok=True)
        except OSError:  # 删不掉（被占用等）的留给过期清理
            log.warning("录音删除失败：%s", rel)


def sniff(raw: bytes) -> tuple[str, str] | None:
    """按文件头认格式（白名单），不信文件名和浏览器报的类型。返回 (扩展名, mime)；不认识的返回 None。
    AMR/3GP（部分手机录音机）转写接口不认，不收，前端提示改用 m4a/mp3 或改打字。"""
    h = raw[:16]
    if h.startswith(b"\x1aE\xdf\xa3"):
        return ".webm", "audio/webm"
    if h.startswith(b"OggS"):
        return ".ogg", "audio/ogg"
    if h.startswith(b"RIFF") and raw[8:12] == b"WAVE":
        return ".wav", "audio/wav"
    if raw[4:8] == b"ftyp" and not raw[8:12].startswith(_NOT_AUDIO_BRANDS):  # MP4 容器：排除 3GP 和 HEIC/AVIF 照片
        return ".m4a", "audio/mp4"
    if h.startswith(b"fLaC"):
        return ".flac", "audio/flac"
    if h.startswith(b"ID3"):
        return ".mp3", "audio/mpeg"
    if len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xF6) == 0xF0:  # ADTS 帧头
        return ".aac", "audio/aac"
    if len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0 and (h[1] & 0x06):  # MPEG 音频帧同步（层不为保留值）
        return ".mp3", "audio/mpeg"
    return None


def _seconds(v) -> float | None:
    try:
        s = float(v)
    except (TypeError, ValueError):
        return None
    return round(s, 1) if 0 < s < 1800 else None


def receipt(row: dict) -> dict:
    """收下录音时的回执（契约 4.4）：{pending_id, status, recorded_at, late_days, client_id}。"""
    return {"pending_id": row["id"], "status": row["status"], "recorded_at": row["recorded_at"],
            "late_days": late_days(row["recorded_at"], row["received_at"]), "client_id": row["client_id"]}


def view(row: dict) -> dict:
    return {"pending_id": row["id"], "status": row["status"], "recorded_at": row["recorded_at"], "seconds": row["seconds"], "last_error": row["last_error"]}


def receive(conn, user: dict, raw: bytes, client_id: str, recorded_at: str | None, seconds, geo: dict | None, note_cid: str = "") -> dict:
    """校验并存下录音，登记一条待转写（不等转写）。录音时间不可信（缺失、未来）时按服务器收到的时间。
    note_cid：拍照时同时录的，指向那条照片随手记的 client_id，转写后文字补进那条随手记。"""
    if not raw:
        raise QuickError(400, "录音是空的，请重录")
    if len(raw) > MAX_AUDIO:
        raise QuickError(413, "录音不超过 25MB")
    kind = sniff(raw)
    if not kind:
        raise QuickError(400, f"录音格式不支持（支持 {FORMATS}），请改打字")
    cid = clean_cid(client_id)
    now = _iso(_now())
    stamp = note_stamp(recorded_at)
    rel = f"voice/{secrets.token_hex(12)}{kind[0]}"
    voice_dir().mkdir(parents=True, exist_ok=True)
    (settings.data_dir / rel).write_bytes(raw)
    try:
        pid = insert(conn, "voice_pending", {
            "project_id": user["project_id"], "user_id": user["id"], "client_id": cid, "audio_file": rel, "mime": kind[1],
            "seconds": _seconds(seconds), "recorded_at": stamp[1] if stamp else now, "received_at": now,
            "geo_lat": geo["lat"] if geo else None, "geo_lon": geo["lon"] if geo else None, "geo_acc": geo["acc"] if geo else None,
            "geo_place": geo["place"] if geo else "", "note_cid": clean_cid(note_cid) if note_cid else "", "status": "pending", "tries": 0, "last_error": "", "updated_at": now})
    except sqlite3.IntegrityError:  # 同一条已登记过（幂等记录被接管重做时）：不重复存，按原来那条回
        _unlink(rel)
        return receipt(one(conn, "SELECT * FROM voice_pending WHERE user_id=? AND client_id=?", (user["id"], cid)))
    except BaseException:
        _unlink(rel)
        raise
    return receipt(one(conn, "SELECT * FROM voice_pending WHERE id=?", (pid,)))


def pending_of(conn, user: dict) -> list[dict]:
    """本人未完成的转写（转写中、失败），按录音时间排。"""
    return [view(r) for r in all_(conn, "SELECT * FROM voice_pending WHERE user_id=? AND status<>'done' ORDER BY recorded_at, id", (user["id"],))]


def requeue_ids(conn, user_id: int) -> list[int]:
    """服务重启会丢掉排队中的后台任务：本人搁置的 pending、超时的 running 重新排队（认领是原子的，重复排队无害）。"""
    now = _now()
    return [r["id"] for r in all_(conn, """SELECT id FROM voice_pending WHERE user_id=? AND audio_file<>'' AND
                                           ((status='pending' AND updated_at<?) OR (status='running' AND updated_at<?)) ORDER BY id LIMIT 20""",
                                  (user_id, _iso(now - timedelta(seconds=REQUEUE_SECONDS)), _iso(now - timedelta(minutes=STALE_MINUTES))))]


def _mine(conn, user: dict, pid: int) -> dict:
    row = one(conn, "SELECT * FROM voice_pending WHERE id=? AND user_id=?", (pid, user["id"]))
    if not row:
        raise QuickError(404, "录音记录不存在")
    return row


def _stale(row: dict) -> bool:
    try:
        t = datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        return True
    return _now() - (t if t.tzinfo else t.astimezone()) >= timedelta(minutes=STALE_MINUTES)


def retry(conn, user: dict, pid: int) -> dict:
    """重新转写：失败的、搁置的、卡在转写中超过 10 分钟的可以；转写完成的、音频已过期删除的不行。调用方随后把它排进后台。"""
    row = _mine(conn, user, pid)
    if row["status"] == "done":
        raise QuickError(409, "这段录音已转写成随手记")
    if not row["audio_file"]:
        raise QuickError(409, "录音文件已不存在，请改打字补记")
    if row["status"] == "running" and not _stale(row):
        raise QuickError(409, "正在转写，请稍候")
    conn.execute("UPDATE voice_pending SET status='pending', last_error='', updated_at=? WHERE id=? AND status=?", (_iso(_now()), pid, row["status"]))
    return view(one(conn, "SELECT * FROM voice_pending WHERE id=?", (pid,)))


def discard(conn, user: dict, pid: int) -> dict:
    """放弃（技术员改打字或不要了）：连记录带音频一起删，留审计。已转写成随手记的到随手记里删。"""
    row = _mine(conn, user, pid)
    if row["status"] == "done":
        raise QuickError(409, "这段录音已转写成随手记，请在随手记里删除")
    if row["status"] == "running" and not _stale(row):
        raise QuickError(409, "正在转写，请稍候再放弃")
    with tx(conn):
        conn.execute("DELETE FROM voice_pending WHERE id=?", (pid,))
        audit(conn, user["id"], "quick.voice.discard", f"voice:{pid}", f"录音 {row['recorded_at'][:16]}，{row['seconds'] or '?'} 秒")
    _unlink(row["audio_file"])
    return {"ok": True}


# ----------------------------------------------------------------------------- 后台转写

def claim(conn, pid: int) -> dict | None:
    """原子认领：pending，或 running 已超过 10 分钟（中断）的，改为 running 并把 tries 加一；别人已认领返回 None。"""
    now = _now()
    cur = conn.execute("""UPDATE voice_pending SET status='running', tries=tries+1, updated_at=?
                          WHERE id=? AND audio_file<>'' AND (status='pending' OR (status='running' AND updated_at<?))""",
                       (_iso(now), pid, _iso(now - timedelta(minutes=STALE_MINUTES))))
    return one(conn, "SELECT * FROM voice_pending WHERE id=?", (pid,)) if cur.rowcount == 1 else None


def _fail(conn, row: dict, msg: str) -> None:
    conn.execute("UPDATE voice_pending SET status='failed', last_error=?, updated_at=? WHERE id=? AND status='running' AND tries=?",
                 ((msg or "转写失败")[:200], _iso(_now()), row["id"], row["tries"]))


def _photo_note(conn, row: dict) -> dict | None:
    """拍照时同时录的语音所属的那条照片随手记（按随手记补传时的 client_id 找，必须是本人的、还在的）。"""
    if not row.get("note_cid"):
        return None
    idem = one(conn, "SELECT result_json FROM quick_idem WHERE user_id=? AND client_id=? AND kind='note'", (row["user_id"], row["note_cid"]))
    nid = (loads(idem["result_json"], {}) or {}).get("id") if idem and idem["result_json"] else None
    return one(conn, "SELECT * FROM notes WHERE id=? AND author_id=?", (nid, row["user_id"])) if nid else None


def transcribe_claimed(conn, row: dict, after=None) -> int | None:
    """转写一条已认领的录音。成功：写成随手记（时间＝录音时间；拍照时录的补进那条照片随手记）、标 done，音频保留供回放，
    再调 after(user_id, 日期) 整理当天日志；失败：标 failed 并保留音频。认领已被接管（tries 变了）或记录已放弃时什么也不写。
    返回随手记的 id。"""
    path = _abs(row["audio_file"])
    if not path or not path.is_file():
        _fail(conn, row, "录音文件已不存在，请改打字补记")
        return None
    try:
        res = transcribe(str(path), f"voice{path.suffix}", project_id=row["project_id"], user_id=row["user_id"])
        text = str(res.data or "").strip()
        if not text:
            raise AIError("语音识别结果为空")
    except AIError as exc:
        _fail(conn, row, str(exc))
        return None
    user = one(conn, "SELECT * FROM users WHERE id=?", (row["user_id"],))
    day = datetime.fromisoformat(row["recorded_at"]).astimezone().date().isoformat()
    geo = {"lat": row["geo_lat"], "lon": row["geo_lon"], "acc": row["geo_acc"], "place": row["geo_place"]} \
        if row["geo_lat"] is not None or row["geo_place"] else None
    nid = None
    try:
        with tx(conn):
            cur = conn.execute("UPDATE voice_pending SET status='done', last_error='', updated_at=? WHERE id=? AND status='running' AND tries=?",
                               (_iso(_now()), row["id"], row["tries"]))
            if cur.rowcount == 1:
                host = _photo_note(conn, row)
                if host:  # 拍照时录的：文字补进那条照片随手记，日期跟那条走
                    merged = f"{host['text']}；{text}" if host["text"].strip() else text
                    conn.execute("UPDATE notes SET text=? WHERE id=?", (merged[:2000], host["id"]))
                    nid, day = host["id"], host["note_date"]
                else:
                    n = log_svc.add_note(conn, user, text, [], row["seconds"], "voice", note_date=day, geo=geo)
                    conn.execute("UPDATE notes SET created_at=? WHERE id=?", (row["recorded_at"], n["id"]))
                    nid = n["id"]
                conn.execute("UPDATE voice_pending SET note_id=? WHERE id=?", (nid, row["id"]))
    except log_svc.LogError as exc:
        _fail(conn, row, str(exc))
        return None
    if nid is None:
        return None
    if after:
        after(row["user_id"], day)
    return nid


def process(pending_id: int, after=None) -> None:
    """后台任务入口（自开连接）：认领到才转写；出错只记日志，录音留着等重试。"""
    conn = connect()
    try:
        row = claim(conn, pending_id)
        if row:
            transcribe_claimed(conn, row, after)
    except Exception:  # noqa: BLE001
        log.exception("语音转写失败：voice_pending %s", pending_id)
    finally:
        conn.close()


# ----------------------------------------------------------------------------- 回放与清理

def audio_of(conn, user: dict, pid: int) -> tuple[Path, str]:
    """回放录音：本人，或同项目有日志审核权的人（工程主管）。返回 (文件, mime)。"""
    row = one(conn, "SELECT * FROM voice_pending WHERE id=?", (pid,))
    if not row or (row["user_id"] != user["id"] and not (R.can(user["role"], "log_review") and row["project_id"] == user["project_id"])):
        raise QuickError(404, "录音不存在")
    path = _abs(row["audio_file"])
    if not path or not path.is_file():
        raise QuickError(404, "录音文件已不存在")
    return path, row["mime"] or "application/octet-stream"


def audio_ids(conn, note_ids: list[int]) -> dict[int, list[int]]:
    """随手记 → 它保留着的录音编号（可回放）。"""
    if not note_ids:
        return {}
    out: dict[int, list[int]] = {}
    for r in all_(conn, f"SELECT id, note_id FROM voice_pending WHERE audio_file<>'' AND note_id IN ({','.join('?' * len(note_ids))}) ORDER BY recorded_at, id",
                  tuple(note_ids)):
        out.setdefault(r["note_id"], []).append(r["id"])
    return out


def drop_for_note(conn, note_id: int) -> int:
    """随手记被删：它转写自/补进它的录音一并删掉（人已决定不要这条记录）。返回删除的条数。"""
    rows = all_(conn, "SELECT id, audio_file FROM voice_pending WHERE note_id=?", (note_id,))
    conn.execute("DELETE FROM voice_pending WHERE note_id=?", (note_id,))
    for r in rows:
        _unlink(r["audio_file"])
    return len(rows)


def cleanup(conn) -> int:
    """录音一律保留；只删录音目录里 7 天前、已没有记录引用的孤儿文件（放弃或删除后没删成的）。
    没有调度也能清：在调用语音接口时顺带执行。返回删除的文件数。"""
    gone = 0
    d = voice_dir()
    if d.is_dir():
        used = {Path(r["audio_file"]).name for r in all_(conn, "SELECT audio_file FROM voice_pending WHERE audio_file<>''")}
        limit = time.time() - KEEP_DAYS * 86400
        for p in d.iterdir():
            try:
                if p.is_file() and p.name not in used and p.stat().st_mtime < limit:
                    p.unlink()
                    gone += 1
            except OSError:
                pass
    return gone
