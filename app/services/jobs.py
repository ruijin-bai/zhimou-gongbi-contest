"""后台调度的单实例锁与每日任务认领。

- 调度锁：数据目录下的 scheduler.lock 加操作系统文件锁（Windows 用 msvcrt.locking，其他系统用 fcntl.flock）。
  同一业务库只有持锁进程运行定时判定、AI 补跑与自动催办；进程活着就一直持有，退出或崩溃时由操作系统自动释放，
  其他进程下一轮再试即可接手。不用数据库租约：租约依赖时钟，与演示时钟冲突。
- 每日任务认领：daily_jobs 主键（项目, 任务, 日期）+ INSERT OR IGNORE，认领到才运行；代替“先查当天事件再插入”
  （两步非原子，多个调度同一秒醒来会各跑一次）。AI 补跑按“当天第 N 次”认领，没真正补跑就退回。
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from ..config import settings
from ..db import event, now_iso, one, tx

log = logging.getLogger("zhimou.jobs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_jobs (
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    pid INTEGER,
    PRIMARY KEY (project_id, kind, day)
);
"""
LOCK_NAME = "scheduler.lock"
RETRY_SECONDS = 60   # 没拿到调度锁的进程每隔多久再试一次（持有者退出后接手）

_mutex = threading.Lock()
_held: int | None = None      # 本进程持有的锁文件描述符
_miss_logged: str | None = None   # 上次没拿到锁的原因（只在原因变化时记日志）


def _lock_path() -> Path:
    return settings.data_dir / LOCK_NAME


def _try_lock(path: Path) -> int | None:
    """非阻塞加锁：拿到返回文件描述符，被别的进程持有返回 None。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def hold_scheduler() -> bool:
    """本进程是否持有调度锁；还没持有就试着拿一次。同一进程内的各调度线程共用一把锁。
    锁文件打不开（被占用、无权限等）不抛异常：记日志后返回 False，调用方按 RETRY_SECONDS 下一轮再试（D-2）。"""
    global _held, _miss_logged
    with _mutex:
        if _held is not None:
            return True
        path = _lock_path()
        try:
            _held = _try_lock(path)
            miss = "busy"
        except OSError as exc:
            _held, miss = None, f"error:{exc}"
        if _held is not None:
            log.info("本进程（pid %s）持有后台调度：%s", os.getpid(), path)
            _miss_logged = None
            return True
        if _miss_logged != miss:  # 同一原因只记一次，避免每轮重试刷日志；原因变了再记
            if miss == "busy":
                log.warning("调度由其他进程持有（%s）：本进程（pid %s）不运行定时判定与自动催办，%s 秒后再试", path, os.getpid(), RETRY_SECONDS)
            else:
                log.warning("调度锁文件打不开（%s：%s）：本进程（pid %s）暂不运行定时判定与自动催办，%s 秒后再试",
                            path, miss[6:], os.getpid(), RETRY_SECONDS)
            _miss_logged = miss
        return False


def release_scheduler() -> None:
    """主动释放调度锁（测试与退出用；进程退出时操作系统也会自动释放）。"""
    global _held
    with _mutex:
        if _held is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(_held, 0, os.SEEK_SET)
                msvcrt.locking(_held, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(_held, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(_held)
        _held = None


def _insert(conn, project_id: int, kind: str, day: str) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO daily_jobs (project_id, kind, day, claimed_at, pid) VALUES (?,?,?,?,?)",
                       (project_id, kind, day, now_iso(), os.getpid()))
    return cur.rowcount == 1


def claim(conn, project_id: int, kind: str, day: str) -> bool:
    """原子认领（不记事件）：同一（项目, 任务, 日期）只有一个调用方拿到。"""
    conn.execute(SCHEMA)  # 幂等；不依赖扩展模块建表成功
    return _insert(conn, project_id, kind, day)


def release(conn, project_id: int, kind: str, day: str) -> None:
    """退回认领：任务这次没有真正执行，留给下一轮。"""
    conn.execute("DELETE FROM daily_jobs WHERE project_id=? AND kind=? AND day=?", (project_id, kind, day))


def claim_daily(conn, project_id: int, kind: str, day: str) -> bool:
    """原子认领某项目某天的一项每日任务。认领到返回 True，并记同名事件（系统概况据此显示最近一次）；已被认领返回 False。
    升级当天兼容：旧版本已按事件记过当天这一项的，也视为已跑。"""
    conn.execute(SCHEMA)
    with tx(conn):
        legacy = one(conn, "SELECT 1 x FROM events WHERE project_id=? AND kind=? AND substr(created_at,1,10)=?", (project_id, kind, day))
        if not _insert(conn, project_id, kind, day) or legacy:
            return False
        event(conn, kind, project_id=project_id)
    return True
