"""P0-1 离线暂存补传·服务端（T-08）。

- 语音：/api/quick/sync/voice 收下录音就返回，不等转写；录音先放 voice_pending，后台转写成功才写成随手记（时间＝录音时间）；
  录音一律保留供回放（RJ 2026-10-09，取代 D13 的删除规则），失败可重新转写或放弃；拍照时同时录的补进那条照片随手记；只能操作本人的记录，录音 25MB 以内、格式在白名单内（AC-1.8）。
- 随手记补传按记录时间入账，不再有“早于 3 天改记到收到当天”的下界（AC-3）；所属周（按日确认的项目为当天）已确认的数量不动，
  这类记录由 /api/quick/late-notes 列出，交技术员在下一次周确认里决定是否补入（D5、AC-3.2、F-12）。
- 同一条补传 2 次服务器上只有 1 条：随手记、照片、语音、整改提交各一组，含并发时的“处理中 409”（AC-6）。
- 整改提交 /api/quick/sync/rectify 按 4.4 契约调用 T-13 的 safety_rectify(..., bg=)；拍摄时间早于派单超过 24 小时返回 422
  {detail, code=photo_before_dispatch, media_id}，同一 client_id 重发得到同样的 422（N-8）。幂等与出错处理用契约形状的替身测，另有与 T-13 的真实联调。
"""
import io
import threading
import wave
from datetime import date, datetime, time, timedelta

import pytest

from app import db as dbmod
from app.ai.client import AIError
from app.config import settings
from app.services import site_agents
from app.services import voice
from conftest import TODAY, confirm_hazards, jpeg, login, sample_xer
from test_roles import _team

LAST_MON = TODAY - timedelta(days=TODAY.weekday() + 7)
MOCK_TEXT = "（离线模拟转写）3#墩桩基施工今天完成2根桩"


def _at(day: date, hh=9, mm=30) -> str:
    return datetime.combine(day, time(hh, mm)).astimezone().isoformat(timespec="seconds")


def _webm(n=64) -> bytes:
    return b"\x1aE\xdf\xa3" + bytes(range(n % 256)) * 2


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return buf.getvalue()


def _voice(c, cid, recorded_at=None, raw=None, name="voice.webm", **extra):
    data = {"client_id": cid, "recorded_at": recorded_at or "", "seconds": "6.5", **extra}
    return c.post("/api/quick/sync/voice", files={"file": (name, raw if raw is not None else _webm(), "audio/webm")}, data=data)


def _q(sql, params=()):
    conn = dbmod.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _row(pid):
    return _q("SELECT * FROM voice_pending WHERE id=?", (pid,))[0]


def _audio(pid):
    rel = _row(pid)["audio_file"]
    return settings.data_dir / rel if rel else None


def _fail_asr(monkeypatch, msg="语音识别失败：ConnectError"):
    def boom(*a, **k):
        raise AIError(msg)
    monkeypatch.setattr(voice, "transcribe", boom)


def _hold(monkeypatch, target, attr):
    """让 target.attr 的第一次调用停在中途，直到放行；用事件同步，不靠 sleep。"""
    entered, release, calls = threading.Event(), threading.Event(), []
    real = getattr(target, attr)

    def wrapper(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(60)
        return real(*a, **k)

    monkeypatch.setattr(target, attr, wrapper)
    return entered, release, calls


def _race(first, second, entered, release):
    """first 在另一线程里发出并停在中途时发 second；随后放行 first。返回 (first 的响应, second 的响应)。"""
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("first", first()))
    th.start()
    try:
        assert entered.wait(60)
        out["second"] = second()
    finally:
        release.set()
        th.join(60)
    return out["first"], out["second"]


# ============================================================================ 语音补传

def test_voice_sync_returns_at_once_and_note_takes_record_time(env):
    """AC-1：联网后录音自动上传并转写，这条随手记的时间是录音时间；收到就返回 pending，转写在后台。"""
    t1 = login("tech1")
    rec = _at(TODAY - timedelta(days=2), 8, 15)
    files_before = set((settings.data_dir / "voice").glob("*"))
    r = _voice(t1, "voice-cid-0001", rec, geo_lat="22.5", geo_lon="113.9", geo_acc="12", place="3号墩")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"pending_id", "status", "recorded_at", "late_days", "client_id"}
    assert body["status"] == "pending" and body["client_id"] == "voice-cid-0001" and body["late_days"] == 2
    assert body["recorded_at"] == rec
    # 后台转写已完成（TestClient 等后台任务跑完）：写成随手记，时间取录音时间，音频保留供回放
    row = _row(body["pending_id"])
    assert row["status"] == "done" and row["note_id"] and row["audio_file"] and _audio(body["pending_id"]).is_file() and row["tries"] == 1
    n = _q("SELECT * FROM notes WHERE id=?", (row["note_id"],))[0]
    assert n["source"] == "voice" and n["text"].startswith("（离线模拟转写）") and n["seconds"] == 6.5
    assert n["note_date"] == (TODAY - timedelta(days=2)).isoformat() and n["created_at"] == rec
    assert (n["geo_lat"], n["geo_lon"], n["geo_place"]) == (22.5, 113.9, "3号墩")
    assert len(set((settings.data_dir / "voice").glob("*")) - files_before) == 1  # 录音转写后仍保留
    # 录音那天的日志由随手记整理（与随手记同一条后台流程）
    lg = _q("SELECT * FROM logs WHERE log_date=? AND period='day' AND auto=1", ((TODAY - timedelta(days=2)).isoformat(),))
    assert lg and n["text"] in lg[0]["voice_text"]
    assert t1.get("/api/quick/voice-pending").json() == []
    # 没带录音时间（或时间不可信）按服务器收到的时间
    r2 = _voice(t1, "voice-cid-0002", "")
    assert r2.status_code == 200 and r2.json()["late_days"] == 0 and r2.json()["recorded_at"][:10] == TODAY.isoformat()
    fut = (datetime.now().astimezone() + timedelta(hours=3)).isoformat()
    r3 = _voice(t1, "voice-cid-0003", fut)
    assert r3.status_code == 200 and r3.json()["recorded_at"] <= dbmod.now_iso()


def test_voice_pending_never_enters_notes(env, monkeypatch):
    """转写完成前不进 notes：整理日志、归类、现场圈都读不到（独立表 voice_pending，G-02）。"""
    _fail_asr(monkeypatch)
    t1 = login("tech1")
    r = _voice(t1, "voice-cid-0101", _at(TODAY, 7, 0))
    assert r.status_code == 200
    assert _q("SELECT COUNT(*) n FROM notes")[0]["n"] == 0
    assert t1.get("/api/tech/home").json()["notes"] == []
    items = t1.get("/api/quick/voice-pending").json()
    assert [set(i) for i in items] == [{"pending_id", "status", "recorded_at", "seconds", "last_error"}]
    assert items[0]["status"] == "failed" and "语音识别失败" in items[0]["last_error"] and items[0]["seconds"] == 6.5


def test_voice_twice_is_one_record_and_busy_409(env, monkeypatch):
    """AC-6：同一条录音补传 2 次服务器上只有 1 条；第一次还在处理时第二次得到 409（前端按退避重试）。"""
    a, b = login("tech1"), login("tech1")
    entered, release, calls = _hold(monkeypatch, voice, "receive")
    rec = _at(TODAY - timedelta(days=1))
    first, second = _race(lambda: _voice(a, "voice-cid-0201", rec), lambda: _voice(b, "voice-cid-0201", rec), entered, release)
    assert first.status_code == 200, first.text
    assert second.status_code == 409 and "正在处理" in second.json()["detail"]
    again = _voice(b, "voice-cid-0201", rec)
    assert again.status_code == 200 and again.json() == first.json()  # 已完成：返回原结果
    assert len(calls) == 1
    assert _q("SELECT COUNT(*) n FROM voice_pending")[0]["n"] == 1 and _q("SELECT COUNT(*) n FROM notes")[0]["n"] == 1


def test_voice_validation_role_size_format(env, monkeypatch):
    """AC-1.8：仅技术员；不超过 25MB；按文件头认格式（白名单），不认文件名；失败不占幂等位。"""
    _team(env)
    t1 = login("tech1")
    assert voice.MAX_AUDIO == 25 * 1024 * 1024
    assert _voice(login("sa_a"), "voice-cid-0301").status_code == 403
    assert _voice(t1, "short").status_code == 422  # client_id 太短
    assert _voice(t1, "voice-cid-0302", raw=jpeg(), name="fake.webm").status_code == 400  # 照片改名成录音
    assert _voice(t1, "voice-cid-0302", raw=b"").status_code == 400
    assert _voice(t1, "voice-cid-0302", raw=b"#!AMR\n" + b"\x00" * 50, name="a.amr").status_code == 400  # 转写接口不认的格式
    for brand in (b"3gp4", b"heic", b"mif1", b"avif"):  # 同为 ftyp 容器的 3GP 录音、iPhone HEIC/AVIF 照片
        assert _voice(t1, "voice-cid-0302", raw=b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 40, name="a.m4a").status_code == 400, brand
    monkeypatch.setattr(voice, "MAX_AUDIO", 1000)
    big = _voice(t1, "voice-cid-0302", raw=_webm(2000) + b"\x00" * 2000)
    assert big.status_code == 413 and "25MB" in big.json()["detail"]
    monkeypatch.undo()
    assert _q("SELECT COUNT(*) n FROM voice_pending")[0]["n"] == 0
    # 白名单内的格式都收；失败的那几次不占幂等位，同一 client_id 改正后可以再交
    for i, (raw, name) in enumerate(((_wav(), "a.wav"), (b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00" + b"\x00" * 40, "a.m4a"),
                                     (b"OggS" + b"\x00" * 60, "a.ogg"), (b"ID3\x04" + b"\x00" * 60, "a.mp3"), (_webm(), "voice.webm"))):
        cid = "voice-cid-0302" if i == 0 else f"voice-cid-03{10 + i}"
        r = _voice(t1, cid, raw=raw, name=name)
        assert r.status_code == 200, (name, r.text)
    assert {r["mime"] for r in _q("SELECT mime FROM voice_pending")} == {"audio/wav", "audio/mp4", "audio/ogg", "audio/mpeg", "audio/webm"}


def test_voice_failure_keeps_audio_then_retry_or_discard(env, monkeypatch):
    """转写失败保留音频等重试；重新转写成功后写成随手记，音频仍保留；放弃则连记录带音频一起删。"""
    _fail_asr(monkeypatch)
    t1 = login("tech1")
    rec = _at(TODAY - timedelta(days=1), 10, 0)
    p1 = _voice(t1, "voice-cid-0401", rec).json()["pending_id"]
    p2 = _voice(t1, "voice-cid-0402", rec, raw=_webm(99)).json()["pending_id"]
    for p in (p1, p2):
        assert _row(p)["status"] == "failed" and _audio(p).is_file()
    monkeypatch.undo()  # 网络恢复
    r = t1.post(f"/api/quick/voice/{p1}/retry")
    assert r.status_code == 200 and r.json()["pending_id"] == p1 and r.json()["status"] == "pending"
    row = _row(p1)
    assert row["status"] == "done" and _audio(p1).is_file() and row["tries"] == 2 and row["last_error"] == ""
    n = _q("SELECT * FROM notes WHERE id=?", (row["note_id"],))[0]
    assert n["created_at"] == rec and n["note_date"] == (TODAY - timedelta(days=1)).isoformat()
    assert t1.post(f"/api/quick/voice/{p1}/retry").status_code == 409  # 已完成
    path = _audio(p2)
    d = t1.post(f"/api/quick/voice/{p2}/discard")
    assert d.status_code == 200 and d.json() == {"ok": True}
    assert not path.exists() and not _q("SELECT 1 FROM voice_pending WHERE id=?", (p2,))
    assert t1.get("/api/quick/voice-pending").json() == []
    assert _q("SELECT COUNT(*) n FROM notes")[0]["n"] == 1
    assert _q("SELECT COUNT(*) n FROM audit WHERE action='quick.voice.discard'")[0]["n"] == 1


def test_voice_ops_only_on_own_records(env, monkeypatch):
    """AC-1.8：voice-pending、retry、discard 只能操作本人的记录。"""
    _fail_asr(monkeypatch)
    t1, t2 = login("tech1"), login("tech2")
    pid = _voice(t1, "voice-cid-0501").json()["pending_id"]
    assert t2.get("/api/quick/voice-pending").json() == []
    assert t2.post(f"/api/quick/voice/{pid}/retry").status_code == 404
    assert t2.post(f"/api/quick/voice/{pid}/discard").status_code == 404
    assert login("pm").get("/api/quick/voice-pending").json() == []
    assert login("pm").post(f"/api/quick/voice/{pid}/discard").status_code == 404
    assert _row(pid)["status"] == "failed" and _audio(pid).is_file()
    assert len(t1.get("/api/quick/voice-pending").json()) == 1


def test_voice_claim_is_atomic_and_stale_running_reclaimed(env, monkeypatch):
    """后台转写先原子认领：并发只转写一次；停在 running 超过 10 分钟可重新认领，旧的那次回来也落不了账。"""
    _fail_asr(monkeypatch)
    t1 = login("tech1")
    pid = _voice(t1, "voice-cid-0601").json()["pending_id"]
    conn = dbmod.connect()
    conn.execute("UPDATE voice_pending SET status='pending' WHERE id=?", (pid,))
    conn.close()
    seen, lock = [], threading.Lock()

    def asr(*a, **k):
        with lock:
            seen.append(1)
        return type("R", (), {"data": "并发只转写一次"})()

    monkeypatch.setattr(voice, "transcribe", asr)
    barrier = threading.Barrier(4)

    def run():
        barrier.wait(30)
        voice.process(pid)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    assert len(seen) == 1 and _row(pid)["status"] == "done"
    assert _q("SELECT COUNT(*) n FROM notes WHERE text='并发只转写一次'")[0]["n"] == 1

    # 停在 running：10 分钟内不让别人认领；超过 10 分钟可以，旧认领回来写不进去
    _fail_asr(monkeypatch)
    p2 = _voice(t1, "voice-cid-0602", raw=_webm(77)).json()["pending_id"]
    monkeypatch.setattr(voice, "transcribe", asr)
    conn = dbmod.connect()
    try:
        conn.execute("UPDATE voice_pending SET status='pending' WHERE id=?", (p2,))
        old = voice.claim(conn, p2)
        assert old and old["status"] == "running" and voice.claim(conn, p2) is None
        stale = (datetime.now().astimezone() - timedelta(minutes=11)).isoformat(timespec="seconds")
        conn.execute("UPDATE voice_pending SET updated_at=? WHERE id=?", (stale, p2))
        new = voice.claim(conn, p2)
        assert new and new["tries"] == old["tries"] + 1
        voice.transcribe_claimed(conn, old)  # 中断后又回来的旧任务
        assert _row(p2)["status"] == "running" and not _row(p2)["note_id"] and _audio(p2).is_file()
        voice.transcribe_claimed(conn, new)
        assert _row(p2)["status"] == "done" and _row(p2)["note_id"] and _audio(p2).is_file()
    finally:
        conn.close()
    assert _q("SELECT COUNT(*) n FROM notes WHERE text='并发只转写一次'")[0]["n"] == 2
    assert len(seen) == 3


def test_voice_pending_requeued_after_restart(env, monkeypatch):
    """服务重启会丢掉排队中的后台任务：本人打开待转写列表时，搁置的 pending、超时的 running 重新排队。"""
    _fail_asr(monkeypatch)
    t1 = login("tech1")
    pid = _voice(t1, "voice-cid-0701").json()["pending_id"]
    monkeypatch.undo()
    old = (datetime.now().astimezone() - timedelta(minutes=3)).isoformat(timespec="seconds")
    conn = dbmod.connect()
    conn.execute("UPDATE voice_pending SET status='pending', updated_at=? WHERE id=?", (old, pid))
    conn.close()
    items = t1.get("/api/quick/voice-pending").json()
    assert [i["pending_id"] for i in items] == [pid]  # 列表先返回，后台随后转写
    assert _row(pid)["status"] == "done" and t1.get("/api/quick/voice-pending").json() == []


def test_failed_audio_kept_beyond_7_days(env, monkeypatch):
    """录音一律保留（RJ 2026-10-09）：失败的录音过了 7 天也不删，仍可重新转写；只清录音目录里没有记录引用的孤儿文件。"""
    time_machine = pytest.importorskip("time_machine")
    _fail_asr(monkeypatch)
    t1 = login("tech1")
    pid = _voice(t1, "voice-cid-0801").json()["pending_id"]
    path = _audio(pid)
    orphan = settings.data_dir / "voice" / "orphan-old.webm"
    orphan.write_bytes(_webm())
    base = datetime.now().astimezone()
    import os
    os.utime(orphan, (base.timestamp() - 8 * 86400,) * 2)
    with time_machine.travel(base + timedelta(days=30), tick=True):
        t1 = login("tech1")  # 登录会过期，重新登录
        items = t1.get("/api/quick/voice-pending").json()
        assert path.is_file() and _row(pid)["audio_file"] and items[0]["status"] == "failed"
        assert not orphan.exists()
        monkeypatch.undo()
        assert t1.post(f"/api/quick/voice/{pid}/retry").status_code == 200
    assert _row(pid)["status"] == "done" and path.is_file()


def test_photo_voice_goes_into_photo_note_and_audio_replays(env):
    """拍照时同时录的语音（note_cid）：转写后补进那条照片随手记，不另起一条；本人与工程主管可回放，别人不行；删随手记连录音一起删。"""
    t1 = login("tech1")
    m = t1.post("/api/media", files={"files": ("a.jpg", jpeg(), "image/jpeg")}, data={"purpose": "log", "scan": "0"}).json()[0]
    r = t1.post("/api/quick/sync/note", json={"client_id": "note-cid-photo01", "text": "", "media_ids": [m["id"]], "source": "photo"})
    assert r.status_code == 200, r.text
    nid = r.json()["id"]
    v = _voice(t1, "voice-cid-0901", note_cid="note-cid-photo01").json()
    row = _row(v["pending_id"])
    assert row["status"] == "done" and row["note_id"] == nid and row["note_cid"] == "note-cid-photo01"
    n = _q("SELECT * FROM notes WHERE id=?", (nid,))[0]
    assert n["source"] == "photo" and n["text"].startswith("（离线模拟转写）")
    assert _q("SELECT COUNT(*) n FROM notes")[0]["n"] == 1
    home = t1.get("/api/tech/home").json()
    assert [x["audio"] for x in home["notes"] if x["id"] == nid] == [[v["pending_id"]]]
    a = t1.get(f"/api/quick/voice/{v['pending_id']}/audio")
    assert a.status_code == 200 and a.content[:4] == _webm()[:4]
    assert login("tech2").get(f"/api/quick/voice/{v['pending_id']}/audio").status_code == 404
    # 照片随手记不存在（照片被放弃）：照常单独成一条语音随手记
    v2 = _voice(t1, "voice-cid-0902", note_cid="note-cid-missing").json()
    n2 = _q("SELECT * FROM notes WHERE id=?", (_row(v2["pending_id"])["note_id"],))[0]
    assert n2["source"] == "voice" and n2["id"] != nid
    # 删照片随手记：它的录音一并删掉
    path = _audio(v["pending_id"])
    assert t1.delete(f"/api/notes/{nid}").status_code == 200
    assert not path.exists() and not _q("SELECT 1 FROM voice_pending WHERE id=?", (v["pending_id"],))


# ============================================================================ 迟到补传与已确认周（AC-3、D5、AC-3.2）

def _weekly(env):
    conn = dbmod.connect()
    conn.execute("UPDATE projects SET config_json='{\"cadence\":\"week\",\"daily_log\":true}' WHERE id=?", (env["project_id"],))
    conn.close()


def _note(c, cid, when, text):
    r = c.post("/api/quick/sync/note", json={"client_id": cid, "text": text, "source": "text", "client_time": when})
    assert r.status_code == 200, r.text
    return r.json()


def _late(c, days=21):
    r = c.get(f"/api/quick/late-notes?days={days}")
    assert r.status_code == 200, r.text
    return {i["note_id"]: i for i in r.json()}


def _snapshot():
    prog = [tuple(r.values()) for r in _q("SELECT * FROM log_progress ORDER BY id")]
    logs = [(r["id"], r["status"], r["version"], r["content_json"]) for r in _q("SELECT * FROM logs WHERE status IN ('submitted','approved') ORDER BY id")]
    return prog, logs


def test_note_5_days_late_keeps_record_day(env):
    """AC-3：5 天后才补传，仍归记录当天，迟到 5 天，不被拒收；更早的也一样（不再有 3 天下界）。"""
    t1 = login("tech1")
    when = _at(TODAY - timedelta(days=5), 16, 40)
    n = _note(t1, "note-cid-late01", when, "5天前离线记的：3号墩承台模板安装")
    assert n["late_days"] == 5
    row = _q("SELECT * FROM notes WHERE id=?", (n["id"],))[0]
    assert row["note_date"] == (TODAY - timedelta(days=5)).isoformat() and row["created_at"] == when
    old = _note(t1, "note-cid-late02", _at(TODAY - timedelta(days=40)), "40天前记的")
    assert old["late_days"] == 40
    assert _q("SELECT note_date FROM notes WHERE id=?", (old["id"],))[0]["note_date"] == (TODAY - timedelta(days=40)).isoformat()
    on_time = _note(t1, "note-cid-late03", dbmod.now_iso(), "当场记的")
    assert on_time["late_days"] == 0
    late = _late(t1)
    assert set(late) == {n["id"], old["id"]}
    assert late[n["id"]] == {"note_id": n["id"], "note_date": (TODAY - timedelta(days=5)).isoformat(), "received_date": TODAY.isoformat(),
                             "late_days": 5, "week_confirmed": False, "text": "5天前离线记的：3号墩承台模板安装", "time": "16:40"}


def test_late_note_leaves_confirmed_week_untouched(env):
    """D5/AC-3.2：所属周已确认后才到的随手记（迟到补传、确认后补记）不改已确认的数量，列入迟到记录（week_confirmed）。"""
    _weekly(env)
    _team(env)
    pm = login("pm")
    assert pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(data_date=(LAST_MON - timedelta(days=1)).isoformat()),
                                                     "application/octet-stream")}).status_code == 200
    t1 = login("tech1")
    n1 = _note(t1, "note-cid-wk0001", _at(LAST_MON), "3#墩桩基施工今天完成2根，累计完成30%")
    n2 = _note(t1, "note-cid-wk0002", _at(LAST_MON + timedelta(days=1)), "3#墩桩基施工完成3根，累计完成45%")
    wk = t1.post("/api/logs", json={"period": "week", "log_date": (LAST_MON + timedelta(days=2)).isoformat()}).json()
    dr = t1.post(f"/api/logs/{wk['id']}/draft", json={"voice_text": ""})
    assert dr.status_code == 200, dr.text
    s = t1.post(f"/api/logs/{wk['id']}/submit", json={"version": dr.json()["version"], "active_seconds": 120})
    assert s.status_code == 200, s.text
    before = _snapshot()
    assert before[0], "周确认应已写入工程量"
    # 确认之后：补传已有日志的那天、补传原本没有记录的那天、以及本周之内的确认后补记
    n3 = _note(t1, "note-cid-wk0003", _at(LAST_MON + timedelta(days=1), 17, 5), "补传：3#墩桩基又完成1根，累计完成50%")
    n4 = _note(t1, "note-cid-wk0004", _at(LAST_MON + timedelta(days=4)), "补传：周五钢筋进场")
    assert _snapshot() == before  # 已确认的数量、周确认与当日日志都没动
    assert t1.get(f"/api/logs/{wk['id']}").json()["status"] == "submitted"
    late = _late(t1)
    assert late[n1["id"]]["week_confirmed"] is False and late[n2["id"]]["week_confirmed"] is False  # 确认之前到的，已在那次确认里
    assert late[n3["id"]]["week_confirmed"] is True and late[n3["id"]]["late_days"] == (TODAY - LAST_MON - timedelta(days=1)).days
    assert late[n4["id"]]["week_confirmed"] is True and late[n4["id"]]["received_date"] == TODAY.isoformat()
    # 退回后重新确认：确认之前到的都不再算“没赶上”
    assert login("eng").post(f"/api/logs/{wk['id']}/review", json={"action": "return", "note": "补上周五钢筋进场"}).status_code == 200
    late = _late(t1)
    assert not any(i["week_confirmed"] for i in late.values())


def test_late_note_leaves_confirmed_day_untouched(env):
    """按日确认的项目：当天日志已确认后补传的随手记不改数量，同样列入迟到记录。"""
    pm = login("pm")
    assert pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    t1 = login("tech1")
    day = TODAY - timedelta(days=3)
    n1 = _note(t1, "note-cid-dy0001", _at(day), "3#墩桩基施工今天完成2根桩，累计完成82%。钢筋工12人")
    lg = _q("SELECT * FROM logs WHERE log_date=? AND period='day'", (day.isoformat(),))[0]
    s = t1.post(f"/api/logs/{lg['id']}/submit", json={"version": lg["version"], "active_seconds": 60})
    assert s.status_code == 200, s.text
    before = _snapshot()
    assert before[0]
    n2 = _note(t1, "note-cid-dy0002", _at(day, 18, 0), "补传：3#墩又完成1根")
    assert n2["late_days"] == 3 and _snapshot() == before
    late = _late(t1)
    assert late[n1["id"]]["week_confirmed"] is False and late[n2["id"]]["week_confirmed"] is True


def test_late_notes_window_and_roles(env):
    _team(env)
    t1 = login("tech1")
    n = _note(t1, "note-cid-win001", _at(TODAY - timedelta(days=35)), "35 天前记的，30 天前补传")
    conn = dbmod.connect()
    conn.execute("UPDATE events SET created_at=? WHERE kind='note.add' AND ref_id=?", (_at(TODAY - timedelta(days=30)), n["id"]))
    conn.close()
    assert _late(t1) == {}  # 默认只看最近 21 天到达的
    assert _late(t1, 40)[n["id"]]["late_days"] == 5
    assert login("tech2").get("/api/quick/late-notes").json() == []
    assert login("sa_a").get("/api/quick/late-notes").json() == []
    # 语音：迟到天数按服务器收到录音的那天算，不按转写完成的那天
    v = _voice(t1, "voice-cid-win01", _at(TODAY - timedelta(days=4))).json()
    nid = _row(v["pending_id"])["note_id"]
    conn = dbmod.connect()
    conn.execute("UPDATE voice_pending SET received_at=? WHERE id=?", (_at(TODAY - timedelta(days=1)), v["pending_id"]))
    conn.close()
    item = _late(t1)[nid]
    assert item["late_days"] == 3 and item["received_date"] == (TODAY - timedelta(days=1)).isoformat()


# ============================================================================ 照片与整改（AC-2、AC-6、N-8）

def test_sync_media_accepts_rectify_and_keeps_shot_time(env):
    """AC-2 服务端：整改照片可暂存补传；整改单的照片带拍摄时间和上传时间，前端据此标“补传”。"""
    t1 = login("tech1")
    up = t1.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))]).json()[0]
    assert "uploaded_at" in up and up["uploaded_at"][:10] == TODAY.isoformat()
    confirm_hazards()
    fid = t1.get("/api/findings?agent=safety&status=active&mine=1").json()[0]["id"]
    shot = (datetime.now() - timedelta(hours=5)).isoformat(timespec="minutes")
    data = {"client_id": "media-cid-rect01", "purpose": "rectify", "taken_at": shot}
    m1 = t1.post("/api/quick/sync/media", files={"file": ("r.jpg", jpeg((20, 210, 20)), "image/jpeg")}, data=data)
    m2 = t1.post("/api/quick/sync/media", files={"file": ("r.jpg", jpeg((20, 210, 20)), "image/jpeg")}, data=data)
    assert m1.status_code == 200, m1.text
    assert m1.json() == m2.json() and m1.json()["purpose"] == "rectify" and m1.json()["taken_at"] == shot
    assert _q("SELECT COUNT(*) n FROM media WHERE purpose='rectify'")[0]["n"] == 1
    assert not _q("SELECT 1 FROM findings WHERE ref_id=?", (m1.json()["id"],))  # 整改照片不当隐患照片识别
    assert t1.post(f"/api/findings/{fid}/rectify", json={"media_id": m1.json()["id"]}).status_code == 200
    f = t1.get(f"/api/findings/{fid}").json()
    assert f["rectify_photo"]["taken_at"] == shot and f["rectify_photo"]["uploaded_at"] > shot


def test_rejected_rectify_photo_can_become_snap(env):
    """N-8“改作随手拍”：没挂到整改上的整改照片，同一张图改用 safety 重新补传，转为随手拍并进识别；已挂到整改上的不动。"""
    t1 = login("tech1")
    raw = jpeg((30, 60, 90))
    r = t1.post("/api/quick/sync/media", files={"file": ("r.jpg", raw, "image/jpeg")}, data={"client_id": "media-cid-rj001", "purpose": "rectify"}).json()
    s = t1.post("/api/quick/sync/media", files={"file": ("r.jpg", raw, "image/jpeg")}, data={"client_id": "media-cid-rj002", "purpose": "safety"})
    assert s.status_code == 200 and s.json()["id"] == r["id"] and s.json()["purpose"] == "safety"
    row = _q("SELECT purpose, safety_status FROM media WHERE id=?", (r["id"],))[0]
    assert row["purpose"] == "safety" and row["safety_status"] in ("hazard", "clear")  # 已在后台识别
    # 已作为整改证据挂上的照片不改用途
    up = t1.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))]).json()[0]
    confirm_hazards()
    fid = _q("SELECT id FROM findings WHERE ref_id=?", (up["id"],))[0]["id"]
    raw2 = jpeg((90, 60, 30))
    e = t1.post("/api/quick/sync/media", files={"file": ("e.jpg", raw2, "image/jpeg")}, data={"client_id": "media-cid-rj003", "purpose": "rectify"}).json()
    assert t1.post(f"/api/findings/{fid}/rectify", json={"media_id": e["id"]}).status_code == 200
    again = t1.post("/api/quick/sync/media", files={"file": ("e.jpg", raw2, "image/jpeg")}, data={"client_id": "media-cid-rj004", "purpose": "safety"})
    assert again.status_code == 200 and again.json()["purpose"] == "rectify"


def test_note_and_media_busy_409(env, monkeypatch):
    """AC-6 并发：同一条随手记/照片第一次还在处理时，第二次得到 409；最终各只有 1 条。"""
    from app import main as M
    from app.services import logs as log_svc
    a, b = login("tech1"), login("tech1")
    entered, release, _ = _hold(monkeypatch, log_svc, "add_note")
    body = {"client_id": "note-cid-busy01", "text": "并发补传", "client_time": _at(TODAY - timedelta(days=1))}
    first, second = _race(lambda: a.post("/api/quick/sync/note", json=body), lambda: b.post("/api/quick/sync/note", json=body), entered, release)
    assert first.status_code == 200 and second.status_code == 409
    assert b.post("/api/quick/sync/note", json=body).json() == first.json()
    assert _q("SELECT COUNT(*) n FROM notes")[0]["n"] == 1
    monkeypatch.undo()
    entered, release, _ = _hold(monkeypatch, M, "upload_media")
    data = {"client_id": "media-cid-busy01", "purpose": "log"}
    send = lambda c: c.post("/api/quick/sync/media", files={"file": ("a.jpg", jpeg((1, 2, 3)), "image/jpeg")}, data=data)  # noqa: E731
    first, second = _race(lambda: send(a), lambda: send(b), entered, release)
    assert first.status_code == 200 and second.status_code == 409
    assert send(b).json() == first.json() and _q("SELECT COUNT(*) n FROM media")[0]["n"] == 1


def test_client_id_kind_must_match(env):
    """幂等位按 client_id 记：同一个编号先用于照片、又拿来提交别的，明确拒绝，不把照片结果当成别的结果返回。"""
    t1 = login("tech1")
    assert t1.post("/api/quick/sync/media", files={"file": ("a.jpg", jpeg((5, 6, 7)), "image/jpeg")}, data={"client_id": "shared-cid-001"}).status_code == 200
    r = t1.post("/api/quick/sync/note", json={"client_id": "shared-cid-001", "text": "x"})
    assert r.status_code == 400 and "client_id" in r.json()["detail"]  # 400：前端队列标失败留人处理，不无限重试
    assert _voice(t1, "shared-cid-001").status_code == 400
    assert _q("SELECT COUNT(*) n FROM notes")[0]["n"] == 0 and _q("SELECT COUNT(*) n FROM voice_pending")[0]["n"] == 0


# ---------------------------------------------------------------------------- 整改提交（契约：safety_rectify(conn, fid, user, media_id, bg=None)）

def _fake_rectify(monkeypatch, behave=None):
    """契约形状的替身：只看本接口自己的幂等与出错处理（调用几次、bg 是否交出），不跑 T-13 的复核。真实联调见 test_sync_rectify_with_t13。"""
    calls = []

    def fake(conn, fid, user, media_id, bg=None):
        calls.append({"fid": fid, "media_id": media_id, "bg": bg is not None})
        if behave:
            behave(len(calls))
        conn.execute("UPDATE findings SET status='rectifying', rectify_media_id=? WHERE id=?", (media_id, fid))
        from app.services import findings
        return findings.view(conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone())

    monkeypatch.setattr(site_agents, "safety_rectify", fake)
    return calls


def _finding_and_photo(t1):
    up = t1.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))]).json()[0]
    confirm_hazards()
    fid = _q("SELECT id FROM findings WHERE ref_id=?", (up["id"],))[0]["id"]
    m = t1.post("/api/quick/sync/media", files={"file": ("r.jpg", jpeg((20, 210, 20)), "image/jpeg")},
                data={"client_id": "media-cid-fx" + str(fid).zfill(4), "purpose": "rectify"}).json()
    return fid, m["id"]


def test_sync_rectify_is_idempotent(env, monkeypatch):
    """AC-6：同一次整改提交补传 2 次只提交 1 次；按契约把 BackgroundTasks 交给 safety_rectify，立即返回整改单视图。"""
    t1 = login("tech1")
    fid, mid = _finding_and_photo(t1)
    calls = _fake_rectify(monkeypatch)
    body = {"client_id": "act-cid-000001", "finding_id": fid, "media_id": mid}
    r1 = t1.post("/api/quick/sync/rectify", json=body)
    r2 = t1.post("/api/quick/sync/rectify", json=body)
    assert r1.status_code == 200, r1.text
    assert r1.json() == r2.json() and r1.json()["id"] == fid and r1.json()["status"] == "rectifying"
    assert calls == [{"fid": fid, "media_id": mid, "bg": True}]
    assert t1.post("/api/quick/sync/rectify", json={**body, "client_id": "x"}).status_code == 422


def test_sync_rectify_photo_before_dispatch_422_replayed(env, monkeypatch):
    """N-8：T-13 抛 RectifyPhotoError 时返回 422 {detail, code=photo_before_dispatch, media_id}（与原整改接口同形）；
    同一 client_id 再交返回同样的 422，不再处理。"""
    t1 = login("tech1")
    fid, mid = _finding_and_photo(t1)

    def reject(n):
        raise site_agents.RectifyPhotoError("拍摄时间早于派单超过 24 小时，不能作为这条整改的照片", mid)

    calls = _fake_rectify(monkeypatch, reject)
    body = {"client_id": "act-cid-000002", "finding_id": fid, "media_id": mid}
    r1 = t1.post("/api/quick/sync/rectify", json=body)
    r2 = t1.post("/api/quick/sync/rectify", json=body)
    for r in (r1, r2):
        assert r.status_code == 422 and r.json() == {"detail": "拍摄时间早于派单超过 24 小时，不能作为这条整改的照片",
                                                     "code": "photo_before_dispatch", "media_id": mid}
    assert len(calls) == 1


def test_sync_rectify_busy_409_and_plain_error_frees_slot(env, monkeypatch):
    t1, t1b = login("tech1"), login("tech1")
    fid, mid = _finding_and_photo(t1)

    def first_refused(n):
        if n == 1:
            raise ValueError("该隐患当前不需要整改")

    calls = _fake_rectify(monkeypatch, first_refused)
    body = {"client_id": "act-cid-000003", "finding_id": fid, "media_id": mid}
    r = t1.post("/api/quick/sync/rectify", json=body)
    assert r.status_code == 400 and r.json()["detail"] == "该隐患当前不需要整改"
    # 普通拒绝不占幂等位：修正后同一 client_id 可以再交；并发时第二个得到 409
    entered, release, _ = _hold(monkeypatch, site_agents, "safety_rectify")
    first, second = _race(lambda: t1.post("/api/quick/sync/rectify", json=body), lambda: t1b.post("/api/quick/sync/rectify", json=body), entered, release)
    assert first.status_code == 200 and first.json()["status"] == "rectifying"
    assert second.status_code == 409
    assert len(calls) == 2


def test_sync_rectify_with_t13(env):
    """与 T-13 联调（AC-2、AC-6、N-8）：迟到补传的整改照片 + 幂等提交 → 待复核、AI 只复核一次、整改单带拍摄与上传时间；
    拍摄早于派单超过 24 小时 → 422（重发同样 422），照片留在服务器不挂整改，同一张图可“改作随手拍”。"""
    _team(env)
    t1 = login("tech1")
    up = t1.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))]).json()[0]
    conn = dbmod.connect()  # 派单在两天前；整改照片昨天离线拍的、今天才补传
    conn.execute("UPDATE findings SET created_at=?, confirmed_at=? WHERE ref_id=?", (_at(TODAY - timedelta(days=2)), _at(TODAY - timedelta(days=2)), up["id"]))
    conn.close()
    fid = _q("SELECT id FROM findings WHERE ref_id=?", (up["id"],))[0]["id"]
    shot = datetime.combine(TODAY - timedelta(days=1), time(15, 20)).isoformat(timespec="minutes")
    m = t1.post("/api/quick/sync/media", files={"file": ("r.jpg", jpeg((20, 210, 20)), "image/jpeg")},
                data={"client_id": "media-cid-t13001", "purpose": "rectify", "taken_at": shot}).json()
    body = {"client_id": "act-cid-t13001", "finding_id": fid, "media_id": m["id"]}
    runs = _q("SELECT COUNT(*) n FROM ai_runs WHERE kind='safety.verify'")[0]["n"]
    r1 = t1.post("/api/quick/sync/rectify", json=body)
    r2 = t1.post("/api/quick/sync/rectify", json=body)
    assert r1.status_code == 200, r1.text
    assert r1.json() == r2.json() and r1.json()["status"] == "rectifying" and r1.json()["verify"]["photo_check"] == "ok"
    assert _q("SELECT COUNT(*) n FROM ai_runs WHERE kind='safety.verify'")[0]["n"] == runs + 1  # 重发不再复核
    f = t1.get(f"/api/findings/{fid}").json()
    assert f["status"] == "rectifying" and f["verify"]["state"] == "done"  # AI 只给建议，仍待安全主管确认
    assert f["rectify_photo"]["taken_at"] == shot and f["rectify_photo"]["uploaded_at"][:10] == TODAY.isoformat()
    assert login("ss").get("/api/notifications").json()[0]["link"] == {"route": "finding", "params": {"id": str(fid)}}

    up2 = t1.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz2.jpg", jpeg((210, 40, 40)), "image/jpeg"))]).json()[0]
    confirm_hazards()
    fid2 = _q("SELECT id FROM findings WHERE ref_id=?", (up2["id"],))[0]["id"]
    raw = jpeg((7, 7, 70))
    early = (datetime.now() - timedelta(days=3)).isoformat(timespec="minutes")
    m2 = t1.post("/api/quick/sync/media", files={"file": ("old.jpg", raw, "image/jpeg")},
                 data={"client_id": "media-cid-t13002", "purpose": "rectify", "taken_at": early}).json()
    body2 = {"client_id": "act-cid-t13002", "finding_id": fid2, "media_id": m2["id"]}
    for _ in range(2):
        r = t1.post("/api/quick/sync/rectify", json=body2)
        assert r.status_code == 422 and r.json()["code"] == "photo_before_dispatch" and r.json()["media_id"] == m2["id"]
    row = _q("SELECT status, rectify_media_id FROM findings WHERE id=?", (fid2,))[0]
    assert row["status"] == "open" and row["rectify_media_id"] is None
    snap = t1.post("/api/quick/sync/media", files={"file": ("old.jpg", raw, "image/jpeg")},
                   data={"client_id": "media-cid-t13003", "purpose": "safety", "taken_at": early})
    assert snap.status_code == 200 and snap.json()["id"] == m2["id"] and snap.json()["purpose"] == "safety"
