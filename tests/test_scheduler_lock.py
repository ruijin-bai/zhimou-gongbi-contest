"""调度单实例（AC-4.4）：同一数据目录（同一业务库）下只有一个进程运行后台调度；进程退出/崩溃后锁自动释放；
每日任务（每日判定、到期提醒、晚间整理）每个项目每天只认领一次。"""
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import ROOT

from app import db as dbmod
from app import main as M
from app.routers import nudge as nudge_rt
from app.services import jobs

# 子进程：按同一数据目录（环境变量 ZB_DATA_DIR 继承自测试进程）尝试拿调度锁，打印结果；拿到则一直持有
_CHILD = ("import sys, time\n"
          "from app.services import jobs\n"
          "ok = jobs.hold_scheduler()\n"
          "print('held' if ok else 'busy', flush=True)\n"
          "time.sleep(60 if ok else 0)\n")


class _Stop(BaseException):
    """打断调度死循环（不被 except Exception 吞掉）。"""


@pytest.fixture(autouse=True)
def _release():
    jobs.release_scheduler()
    yield
    jobs.release_scheduler()


def _child() -> tuple[subprocess.Popen, str]:
    p = subprocess.Popen([sys.executable, "-c", _CHILD], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    return p, p.stdout.readline().strip()


def _wait_hold(timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if jobs.hold_scheduler():
            return True
        time.sleep(0.2)
    return False


def test_only_one_process_holds_scheduler_and_crash_releases_it():
    assert jobs.hold_scheduler() is True and jobs.hold_scheduler() is True   # 本进程拿到后一直持有（重复调用不变）
    p, out = _child()
    p.wait(30)
    assert out == "busy"                                                     # 另一个进程连同一个库：拿不到，不跑调度
    jobs.release_scheduler()
    p, out = _child()                                                        # 现在由子进程持有
    try:
        assert out == "held"
        assert jobs.hold_scheduler() is False
        p.kill()                                                             # 模拟持有者崩溃：操作系统自动释放
        p.wait(30)
        assert _wait_hold() is True                                          # 本进程随即接手
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(30)


def test_scheduler_loops_do_nothing_without_the_lock(monkeypatch):
    calls = []
    monkeypatch.setattr(M, "_scheduled_jobs", lambda now: calls.append("jobs"))
    monkeypatch.setattr(M, "_ai_retries", lambda: calls.append("retry"))
    monkeypatch.setattr(nudge_rt.svc, "tick", lambda *a, **k: calls.append("nudge"))
    monkeypatch.setenv("ZB_SCHEDULER", "1")
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= 2 or sec != nudge_rt.FIRST_DELAY:
            raise _Stop()
    monkeypatch.setattr(M.time, "sleep", fake_sleep)

    for held in (False, True):
        monkeypatch.setattr(jobs, "hold_scheduler", lambda h=held: h)
        sleeps.clear()
        with pytest.raises(_Stop):
            M._scheduler()
        sleeps.clear()
        with pytest.raises(_Stop):
            nudge_rt._loop()
        if not held:
            assert calls == []                                               # 调度由其他进程持有：定时判定、AI 补跑、自动催办都不跑
        else:
            assert calls == ["jobs", "retry", "nudge"]


def test_unopenable_lock_file_logs_and_retries(monkeypatch, caplog):
    """验收缺陷 D-2：锁文件打不开（被占用、无权限；这里用同名目录模拟）时不抛异常、记日志，下一轮重试；可打开后照常接手。"""
    bad = Path(tempfile.mkdtemp(prefix="zb_lock_")) / jobs.LOCK_NAME
    bad.mkdir()
    monkeypatch.setattr(jobs, "_lock_path", lambda: bad)
    with caplog.at_level("WARNING", logger="zhimou.jobs"):
        assert jobs.hold_scheduler() is False
        assert jobs.hold_scheduler() is False
    assert any("打不开" in r.getMessage() for r in caplog.records)
    # 调度线程不退出：第一轮拿不到锁按重试间隔等待，第二轮锁可用后照常运行
    calls, sleeps = [], []
    monkeypatch.setattr(M, "_scheduled_jobs", lambda now: calls.append("jobs"))
    monkeypatch.setattr(M, "_ai_retries", lambda: calls.append("retry"))

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) == 1:
            bad.rmdir()                       # 占用解除
        else:
            raise _Stop()
    monkeypatch.setattr(M.time, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        M._scheduler()
    assert sleeps[0] == jobs.RETRY_SECONDS and calls == ["jobs", "retry"]
    jobs.release_scheduler()


def _project(conn, code):
    return dbmod.insert(conn, "projects", {"name": f"项目{code}", "code": code, "config_json": "{}", "created_at": dbmod.now_iso()})


def test_daily_jobs_run_once_per_project_per_day_under_concurrency(env, monkeypatch):
    # 10-07 实况：两个调度在同一秒各插入一次 detect.daily，同一项目一天预测两次
    conn = dbmod.connect()
    pid2 = _project(conn, "T2")
    conn.close()
    pids = sorted([env["project_id"], pid2])
    ran: list[tuple[str, int]] = []
    lock = threading.Lock()
    n = 4
    barrier = threading.Barrier(n)

    def rec(name):
        def f(pid, *a):
            with lock:
                ran.append((name, pid))
        return f

    def has_import(conn, pid):
        try:  # 让各进程都停在“已查过今天没跑、尚未认领”的时刻
            barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass
        return {"id": 1}
    monkeypatch.setattr(M.p6_svc, "current_import", has_import)
    for name in ("_detect_bg", "_daily_agents", "_due_reminders", "_evening"):
        monkeypatch.setattr(M, name, rec(name))
    now = datetime.now().replace(hour=21, minute=0, second=0, microsecond=0)
    errors = []

    def one_pass():
        try:
            M._scheduled_jobs(now)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=one_pass) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    for name in ("_detect_bg", "_daily_agents", "_due_reminders", "_evening"):
        assert sorted(p for k, p in ran if k == name) == pids, name   # 每个项目恰好一次
    conn = dbmod.connect()
    try:
        for kind in ("detect.daily", "due.remind", "evening"):        # 系统概况看的事件也只有一条
            assert conn.execute("SELECT COUNT(*) n FROM events WHERE kind=? AND project_id=?", (kind, pids[0])).fetchone()["n"] == 1, kind
    finally:
        conn.close()
    # 同一天再跑一轮不重复；次日照常
    ran.clear()
    monkeypatch.setattr(M.p6_svc, "current_import", lambda conn, pid: {"id": 1})
    M._scheduled_jobs(now + timedelta(minutes=30))
    assert ran == []
    M._scheduled_jobs(now + timedelta(days=1))
    assert sorted(ran) == sorted((k, p) for k in ("_detect_bg", "_daily_agents", "_due_reminders", "_evening") for p in pids)


def test_daily_jobs_respect_runs_recorded_before_upgrade(env, monkeypatch):
    # 升级当天：旧版本已按事件记过今天的每日判定、到期提醒、晚间整理，新版本不再重复跑
    pid = env["project_id"]
    conn = dbmod.connect()
    try:
        for kind in ("detect.daily", "due.remind", "evening"):
            dbmod.event(conn, kind, project_id=pid)
    finally:
        conn.close()
    ran = []
    monkeypatch.setattr(M.p6_svc, "current_import", lambda conn, p: {"id": 1})
    for name in ("_detect_bg", "_daily_agents", "_due_reminders", "_evening"):
        monkeypatch.setattr(M, name, lambda p, *a, n=name: ran.append(n))
    M._scheduled_jobs(datetime.now().replace(hour=21, minute=0))
    assert ran == []
    day = datetime.now().date().isoformat()
    conn = dbmod.connect()
    try:
        assert conn.execute("SELECT COUNT(*) n FROM events WHERE kind IN ('detect.daily','due.remind','evening')").fetchone()["n"] == 3
        # 新认领的任务记下是哪个进程跑的，便于排查
        assert jobs.claim_daily(conn, pid, "x.test", day) is True and jobs.claim_daily(conn, pid, "x.test", day) is False
        row = conn.execute("SELECT * FROM daily_jobs WHERE kind='x.test'").fetchone()
        assert row["day"] == day and row["pid"] == os.getpid()
    finally:
        conn.close()


# ---------------------------------------------------------------- AI 补跑：多进程同时补跑会重复调用 AI

def _event_ago(pid, kind, minutes):
    at = (datetime.now().astimezone() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    conn = dbmod.connect()
    try:
        dbmod.insert(conn, "events", {"project_id": pid, "kind": kind, "data_json": "{}", "created_at": at})
    finally:
        conn.close()


def _retry_rows():
    conn = dbmod.connect()
    try:
        return sorted(r["kind"] for r in conn.execute("SELECT kind FROM daily_jobs WHERE kind LIKE 'qc.retry%'").fetchall())
    finally:
        conn.close()


def test_ai_retries_call_ai_once_under_concurrency(env, monkeypatch):
    pid = env["project_id"]
    _event_ago(pid, "qc.fail", 40)
    calls = []
    lock = threading.Lock()
    n = 4
    barrier = threading.Barrier(n)

    def ready():
        try:  # 让各进程都停在“已查过次数与间隔、尚未记补跑”的时刻
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return True
    monkeypatch.setattr(M.site_agents, "_ai_ready", ready)

    def ai(conn, p, *a, **k):
        with lock:
            calls.append(p)
        return 0
    monkeypatch.setattr(M.site_agents, "quality_reminders", ai)
    threads = [threading.Thread(target=M._ai_retries) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert calls == [pid]                                                    # 只补跑一次（只调用一次 AI）
    assert _retry_rows() == ["qc.retry#1"]


def test_ai_retry_claim_is_returned_when_nothing_was_retried(env, monkeypatch):
    pid = env["project_id"]
    calls = []
    monkeypatch.setattr(M.site_agents, "_ai_ready", lambda: True)
    monkeypatch.setattr(M.site_agents, "quality_reminders", lambda conn, p, *a, **k: calls.append(p) or 0)
    _event_ago(pid, "qc.fail", 5)
    M._ai_retries()                                                          # 间隔未到：不补跑，退回认领
    assert calls == [] and _retry_rows() == []
    conn = dbmod.connect()
    conn.execute("UPDATE events SET created_at=? WHERE kind='qc.fail'", ((datetime.now().astimezone() - timedelta(minutes=40)).isoformat(timespec="seconds"),))
    conn.close()
    M._ai_retries()                                                          # 满 30 分钟：补跑第 1 次
    assert calls == [pid] and _retry_rows() == ["qc.retry#1"]
    M._ai_retries()                                                          # 刚补跑过：第 2 次间隔未到，认领退回
    assert calls == [pid] and _retry_rows() == ["qc.retry#1"]


# ---------------------------------------------------------------- AC-4.8 持锁进程退出后，另一个进程在一个调度周期内接管

def test_takeover_within_one_cycle_after_holder_exits(monkeypatch):
    p, out = _child()
    assert out == "held"
    calls = []
    monkeypatch.setattr(M, "_scheduled_jobs", lambda now: calls.append("jobs"))
    monkeypatch.setattr(M, "_ai_retries", lambda: calls.append("retry"))
    real_sleep = time.sleep
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if calls:
            raise _Stop()
        if len(sleeps) > 50:
            raise AssertionError("持锁进程退出后迟迟没有接管")
        if p.poll() is None:  # 第一轮没拿到锁、什么也没跑；随后持锁进程退出（含崩溃）
            assert calls == []
            p.kill()
            p.wait(30)
        real_sleep(0.2)       # Windows 释放文件锁可能稍有延迟
    monkeypatch.setattr(M.time, "sleep", fake_sleep)
    try:
        with pytest.raises(_Stop):
            M._scheduler()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(30)
    assert calls == ["jobs", "retry"]
    waits = sleeps[:-1]   # 接管前每次等待都是重试间隔，不超过一个调度周期（定时判定 600 秒、催办 30 分钟）
    assert waits and all(w == jobs.RETRY_SECONDS for w in waits) and jobs.RETRY_SECONDS < 600 <= nudge_rt.TICK_SECONDS
    assert sleeps[-1] == 600
