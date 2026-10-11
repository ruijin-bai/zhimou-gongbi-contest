"""T-07 演示日（P0-7，D8/D16）：演示副本 + 进程级演示时钟。

- AC-7.1 演示时钟下遍历全部 GET 接口：返回里没有晚于演示日的记录时间；豁免清单与 AC-7.3 相同（demo_copy.EXEMPT，N-10①）。
- AC-7.2 演示实例不起调度：demo_serve 强制 ZB_SCHEDULER=0，进程里没有 detect-scheduler 线程。
- AC-7.3 副本中每张含时间列的表（按 sqlite_master 枚举全部表，时间列按列名识别），最大时间不晚于演示日 23:59。
- AC-7.4 进程入口 demo_copy / demo_serve 做启动检查：库里有非仿真项目（或不是演示副本）时退出码为 2。
- AC-7.5 制作副本不修改源库（大小、修改时间、user_version）；副本带 media/，不带 ai_keys.json、.secret、tls/。
另：F-19 历史重复消息合并；D16 演示实例默认 mock、真实 AI 要显式开关、Key 池文件演示结束后删除；
页头“演示日”标识全页只出现一次；runAction 对文件框直接弹出选择（U-05③）；F-18①② 文案。
没装 time-machine 的环境整个文件跳过（N-10⑥）。
"""
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pytest

time_machine = pytest.importorskip("time_machine")

from conftest import PW, ROOT  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sample_xer import make_xer  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import main as M  # noqa: E402
from app.config import settings  # noqa: E402
from app.security import hash_password  # noqa: E402
from tools import demo_copy as dc  # noqa: E402
from tools import demo_serve as ds  # noqa: E402

DAY = "2026-09-22"
TZ = datetime.now().astimezone().tzinfo
STATIC = ROOT / "app" / "static"
NODE = shutil.which("node")
# AC-7.3 的时间列规则（与任务单一致，测试里独立写一遍，不复用被测代码的正则）
AC_TIME_COL = re.compile(r"(_at|_date)$|^(day|first_detected|last_detected|sent_at)$")


def at(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _client(username: str) -> TestClient:
    c = TestClient(M.app, raise_server_exceptions=False)
    c.headers.update({"x-zb": "1"})
    r = c.post("/api/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return c


def _reset_db():
    for p in (settings.db_path, Path(str(settings.db_path) + "-wal"), Path(str(settings.db_path) + "-shm")):
        if p.exists():
            p.unlink()
    dbmod._initialized = False
    dbmod.init_db()


ROLE_USERS = (("d_admin", "admin", ""), ("d_pm", "manager", ""), ("d_eng", "eng_sup", ""), ("d_saf", "safety_sup", ""),
              ("d_boss", "leader", ""), ("d_safe", "sub_safety", "甲分包"), ("d_tech", "tech", "甲分包"))


# ---------------------------------------------------------------------------------------------- 源库（相当于业务库）
@pytest.fixture(scope="module")
def built():
    """在测试自己的数据目录里造一个“业务库”：仿真项目从 09-01 开工，回放到 09-22；10-07 又按真实日期跑了一轮调度、浏览写库，
    另有真实回测、晚于演示日才建的仿真项目、09-21 同一小时的重复消息、期限晚于演示日的隐患等。"""
    _reset_db()
    conn = dbmod.connect()
    ids = {}
    with time_machine.travel(at(2026, 9, 6, 18), tick=False):
        pid = dbmod.insert(conn, "projects", {"name": "演示仿真项目", "code": "DEMO-SIM",
                                              "config_json": json.dumps({"simulation": True, "cadence": "day", "daily_log": True}),
                                              "created_at": dbmod.now_iso()})
        for uname, role, company in ROLE_USERS:
            ids[uname] = dbmod.insert(conn, "users", {"username": uname, "name": uname, "role": role, "password_hash": hash_password(PW),
                                                      "project_id": pid, "wbs_scope": "3#墩" if role == "tech" else "", "email": "",
                                                      "active": 1, "created_at": dbmod.now_iso(), "company": company})
    ids["pid"] = pid
    conn.close()

    with time_machine.travel(at(2026, 9, 20, 9), tick=False):
        pm = _client("d_pm")
        r = pm.post("/api/p6/import", files={"file": ("plan.xer", make_xer("2026-09-19", start="2026-09-01",
                                                                            progress={"M0000": 100, "A1010": 60, "A2010": 30}).encode(), "application/octet-stream")})
        assert r.status_code == 200, r.text
        tech = _client("d_tech")
        assert tech.post("/api/notes", json={"text": "3#墩桩基今天完成2根，钢筋笼下放正常"}).status_code == 200
    with time_machine.travel(at(2026, 9, 21, 10), tick=False):
        tech = _client("d_tech")
        assert tech.post("/api/notes", json={"text": "3#墩承台钢筋绑扎开始，现场有积水"}).status_code == 200
        conn = dbmod.connect()
        # 09-21 同一小时发给项目经理的 3 条重复消息（F-19）+ 1 条不同标题
        for i in range(3):
            dbmod.insert(conn, "notifications", {"project_id": pid, "user_id": ids["d_pm"], "alert_id": None, "title": "待决策：场地泥泞积水",
                                                 "body": f"第{i + 1}轮", "channels_json": "{}", "created_at": iso(at(2026, 9, 21, 10, 5 + i)),
                                                 "link_json": json.dumps({"route": "safety"})})
        ids["notif_other"] = dbmod.insert(conn, "notifications", {"project_id": pid, "user_id": ids["d_pm"], "alert_id": None, "title": "请确认本周进度",
                                                                  "body": "x", "channels_json": "{}", "created_at": iso(at(2026, 9, 21, 10, 30))})
        # 期限晚于演示日的已确认隐患（due_date 在豁免清单里）
        ids["finding_due"] = dbmod.insert(conn, "findings", {
            "project_id": pid, "agent": "safety", "kind": "hazard", "level": "high", "title": "临边防护缺失", "body_json": "{}",
            "status": "open", "assignee_id": ids["d_safe"], "due_date": "2026-09-30", "created_at": iso(at(2026, 9, 21, 10)),
            "updated_at": iso(at(2026, 9, 21, 10)), "confirmed_at": iso(at(2026, 9, 21, 10)), "confirmed_by": ids["d_saf"]})
        conn.close()
    # 与业务库的真实经过一致：回放到 09-21 时，开着调度的服务按真实日期 10-07 跑了一轮（新建/升级预警、简报、催办……），
    # 之后回放继续跑 09-22，把已有预警重新判定回仿真日期
    for when in (at(2026, 9, 21, 21), at(2026, 10, 7, 7, 9), at(2026, 9, 22, 21)):
        with time_machine.travel(when, tick=False):
            M._scheduled_jobs(datetime.now())

    # ---- 10-07：浏览页面写库，以及其他晚于演示日的记录
    with time_machine.travel(at(2026, 10, 7, 8, 30), tick=False):
        tech = _client("d_tech")
        assert tech.post("/api/notes", json={"text": "10-07 的随手记"}).status_code == 200
        conn = dbmod.connect()
        conn.execute("UPDATE notifications SET read_at=? WHERE id=?", (dbmod.now_iso(), ids["notif_other"]))      # 只是之后才读
        lg = conn.execute("SELECT id FROM logs WHERE project_id=? AND log_date<=? ORDER BY id LIMIT 1", (pid, DAY)).fetchone()
        assert lg, "09-20/09-21 应已整理出日志"
        ids["log_reopened"] = lg["id"]
        conn.execute("UPDATE logs SET started_at=? WHERE id=?", (dbmod.now_iso(), lg["id"]))                      # 只是之后又打开
        # 真实回测（T-04a）与测试回测：真实的一行不能删（豁免），测试的一行按默认规则删
        for src in ("real", "test"):
            ids["bt_" + src] = dbmod.insert(conn, "backtests", {"project_id": pid, "source": src, "label": src, "created_by": ids["d_eng"],
                                                                "created_at": dbmod.now_iso()})
            dbmod.audit(conn, ids["d_eng"], "backtest.run", f"backtest:{ids['bt_' + src]}")
        # 晚于演示日才建的仿真项目：整个项目连同账号、日志一起不进副本
        p2 = dbmod.insert(conn, "projects", {"name": "新仿真", "code": "DEMO-SIM-NEW", "config_json": json.dumps({"simulation": True}),
                                             "created_at": iso(at(2026, 10, 5, 20))})
        u2 = dbmod.insert(conn, "users", {"username": "d_new", "name": "新", "role": "tech", "password_hash": hash_password(PW),
                                          "project_id": p2, "wbs_scope": "", "email": "", "active": 0, "created_at": iso(at(2026, 10, 5, 20))})
        dbmod.insert(conn, "logs", {"project_id": p2, "author_id": u2, "log_date": "2026-09-10", "started_at": iso(at(2026, 10, 5, 21))})
        ids["p_new"], ids["u_new"] = p2, u2
        # 演示日前就有、10-07 被重新判定后没再按仿真日期判定的预警：标题、等级已是 10-07 的判定，整行不进副本（不留“超期 N 天”）
        ids["alert_rejudged"] = dbmod.insert(conn, "alerts", {
            "project_id": pid, "import_id": 1, "fingerprint": "rejudged", "activity_code": "A4010", "activity_name": "梁板预制", "level": "red",
            "rule": "not_started", "title": "应开未开 23 天", "detail_json": "{}", "status": "open", "first_detected": iso(at(2026, 9, 21, 7)),
            "last_detected": dbmod.now_iso(), "as_of": "2026-10-07"})
        # 二维码：有效期晚于演示日（豁免）
        ids["qr"] = dbmod.insert(conn, "quick_tokens", {"project_id": pid, "token": "t" * 32, "label": "1#工点", "created_by": ids["d_safe"],
                                                        "created_at": iso(at(2026, 9, 20, 15)), "expires_at": iso(at(2026, 11, 19, 15))})
        # 业务库 Key 池登记（只有指纹）：副本里要清空，真实 AI 模式再从 .env 导入
        conn.execute("INSERT INTO ai_keys(label, fp, tail, created_at) VALUES ('k', 'fp-business', '1234', ?)", (iso(at(2026, 9, 6)),))
        conn.execute("INSERT OR REPLACE INTO ai_settings(k, v) VALUES ('seeded_fps', '[\"fp-business\"]')")
        conn.close()
    # 数据目录里的其他文件：照片要带上；Key 文件、会话密钥、证书不带
    media = settings.media_dir / str(pid)
    media.mkdir(parents=True, exist_ok=True)
    (media / "demo_photo.jpg").write_bytes(b"\xff\xd8 fake jpeg")
    # 10-07 上传的照片：记录清理后，文件也不进副本
    (media / "202610").mkdir(exist_ok=True)
    late = {k: f"{pid}/202610/late_{k}.jpg" for k in ("file", "ai_file", "thumb")}
    for rel in late.values():
        (settings.media_dir / rel).write_bytes(b"late")
    conn = dbmod.connect()
    dbmod.insert(conn, "media", {"project_id": pid, "uploader_id": ids["d_tech"], "sha256": "x" * 64, **late, "uploaded_at": iso(at(2026, 10, 7, 9))})
    conn.close()
    ids["late_files"] = list(late.values())
    (settings.data_dir / "ai_keys.json").write_text('{"fp-business": "not-a-real-key"}', encoding="utf-8")
    (settings.data_dir / "tls").mkdir(exist_ok=True)
    (settings.data_dir / "tls" / "cert.pem").write_text("dummy", encoding="utf-8")
    assert (settings.data_dir / ".secret").is_file()
    yield ids
    for p in (settings.data_dir / "ai_keys.json", settings.data_dir / "tls" / "cert.pem"):
        p.unlink(missing_ok=True)


def _mkdtemp() -> Path:
    return Path(tempfile.mkdtemp(prefix="zb_demo_"))


@pytest.fixture()
def tmp():
    d = _mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _stat(db: Path) -> tuple:
    st = db.stat()
    c = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        uv = c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()
    return st.st_size, st.st_mtime_ns, uv


@pytest.fixture(scope="module")
def copied(built):
    src = settings.db_path
    # 业务库空闲：没有 -wal/-shm（连接都已关闭）
    assert not Path(str(src) + "-wal").exists() and not Path(str(src) + "-shm").exists()
    before = _stat(src)
    dst = _mkdtemp() / "演示副本"
    summary = dc.make_copy(settings.data_dir, dst, DAY)
    after = _stat(src)
    side = [Path(str(src) + s).exists() for s in ("-wal", "-shm")]
    yield {"dst": dst, "before": before, "after": after, "side": side, "summary": summary, "ids": built}
    shutil.rmtree(dst.parent, ignore_errors=True)


@contextmanager
def _ro(db: Path):
    c = sqlite3.connect(db)
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------------------------- AC-7.5 / AC-7.3 / 清理语义
def test_ac75_copy_leaves_source_untouched_and_takes_media_only(copied):
    assert copied["before"] == copied["after"]                      # 大小、修改时间、user_version 都不变
    assert copied["side"] == [False, False]                         # 也没在业务库旁边留下 -wal/-shm
    dst = copied["dst"]
    assert (dst / "zhimou.db").is_file()
    assert (dst / "media" / str(copied["ids"]["pid"]) / "demo_photo.jpg").read_bytes() == b"\xff\xd8 fake jpeg"
    for name in ("ai_keys.json", ".secret", "tls"):
        assert not (dst / name).exists(), name
    for rel in copied["ids"]["late_files"]:                       # 演示日之后上传的照片随记录一起不带
        assert not (dst / "media" / rel).exists(), rel
    assert copied["summary"]["media_dropped"] == 3
    marker =json.loads((dst / dc.MARKER).read_text(encoding="utf-8"))
    assert marker["day"] == DAY and "media/" in marker["note"] and "原始照片" in marker["note"]
    assert (dst / ".gitignore").read_text(encoding="utf-8").strip() == "*"   # 副本含照片，整个目录不进 Git
    with _ro(dst / "zhimou.db") as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not c.execute("PRAGMA foreign_key_check").fetchall()


def test_ac75_copy_while_source_in_use_reads_wal_without_touching_it(tmp):
    """业务库正被服务使用（WAL 里有还没写回主文件的新记录）：照样只读复制，读得到 WAL 里的记录，主文件不变。"""
    src = tmp / "业务"
    src.mkdir()
    w = sqlite3.connect(src / "zhimou.db", isolation_level=None)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("CREATE TABLE projects(id INTEGER PRIMARY KEY, name TEXT, code TEXT, config_json TEXT, created_at TEXT)")
    w.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, project_id INTEGER, note_date TEXT, created_at TEXT)")
    w.execute("INSERT INTO projects VALUES (1, '仿真', 'S', '{\"simulation\": true}', '2026-09-06T18:00:00')")
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    w.execute("INSERT INTO notes VALUES (1, 1, '2026-09-21', '2026-09-21T10:00:00'), (2, 1, '2026-10-07', '2026-10-07T08:00:00')")
    try:
        assert Path(str(src / "zhimou.db") + "-wal").stat().st_size > 0
        before = (src / "zhimou.db").stat()
        dc.make_copy(src, tmp / "demo", DAY)
        after = (src / "zhimou.db").stat()
        assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    finally:
        w.close()
    with _ro(tmp / "demo" / "zhimou.db") as c:
        assert c.execute("SELECT id FROM notes").fetchall() == [(1,)]


def _exempt(table: str, col: str) -> tuple[bool, str | None]:
    """按 demo_copy.EXEMPT 判断：(整列豁免, 行豁免条件)。"""
    whole, cond = False, None
    for t, c, where in dc.EXEMPT:
        if t != table:
            continue
        if c == col and where is None:
            whole = True
        if c is None:
            cond = where
    return whole, cond


def test_ac73_every_table_max_time_not_after_demo_day(copied):
    with _ro(copied["dst"] / "zhimou.db") as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        assert len(tables) >= 50, tables                              # 枚举的是全部表（含各扩展模块的表）
        checked, bad = 0, []
        for t in tables:
            for col in [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]:
                if not AC_TIME_COL.search(col):
                    continue
                whole, cond = _exempt(t, col)
                if whole:
                    continue
                checked += 1
                where = f" AND NOT ({cond})" if cond else ""
                mx = c.execute(f'SELECT MAX("{col}") FROM "{t}" WHERE substr("{col}",1,10) > ?{where}', (DAY,)).fetchone()[0]
                if mx:
                    bad.append(f"{t}.{col}={mx}")
        assert checked >= 80 and not bad, bad
        # 被测代码自己的校验与本测试一致
        assert dc.verify(c, DAY) == []


def test_exemption_list_is_the_shared_constant():
    tables = {t for t, _, _ in dc.EXEMPT}
    assert {"backtests", "audit"} <= tables
    rows = {(t, w) for t, c, w in dc.EXEMPT if c is None}
    assert ("backtests", "source = 'real'") in rows and ("audit", "action LIKE 'backtest.%'") in rows
    for t, c, w in dc.EXEMPT:                                        # 列豁免只给“期限/有效期”这类本来就在将来的列
        if c is not None:
            assert w is None and c in ("due_date", "expires_at"), (t, c)


def test_cleaning_keeps_timeline_and_drops_later_records(copied):
    ids = copied["ids"]
    with _ro(copied["dst"] / "zhimou.db") as c:
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731
        # 演示日之前的仿真时间线完整保留
        assert one("SELECT COUNT(*) FROM projects") == 1 and one("SELECT code FROM projects") == "DEMO-SIM"
        assert one("SELECT COUNT(*) FROM users WHERE project_id=?", ids["pid"]) == len(ROLE_USERS)
        assert one("SELECT COUNT(*) FROM notes WHERE note_date<=?", DAY) == 2
        assert one("SELECT COUNT(*) FROM briefs WHERE day<=?", DAY) >= 1
        assert one("SELECT COUNT(*) FROM p6_imports") == 1 and one("SELECT COUNT(*) FROM activities") > 0
        assert one("SELECT COUNT(*) FROM alerts") >= 1 and one("SELECT MAX(as_of) FROM alerts") <= DAY
        assert one("SELECT COUNT(*) FROM alerts WHERE id=?", ids["alert_rejudged"]) == 0
        # 演示日前就有的预警，10-07 被 AI 重新分析过：分析内容清空（不带出 10-07 的判定）
        assert one("SELECT COUNT(*) FROM alerts WHERE json_extract(explanation_json, '$.as_of') > ?", DAY) == 0
        assert one("SELECT COUNT(*) FROM alerts WHERE ai_run_id IS NOT NULL AND ai_run_id NOT IN (SELECT id FROM ai_runs)") == 0
        # 晚于演示日的全部去掉（含 10-07 的调度产物、浏览写库、之后才建的项目及其账号日志）
        assert one("SELECT COUNT(*) FROM notes WHERE note_date>?", DAY) == 0
        assert one("SELECT COUNT(*) FROM briefs WHERE day>?", DAY) == 0
        assert one("SELECT COUNT(*) FROM users WHERE id=?", ids["u_new"]) == 0
        assert one("SELECT COUNT(*) FROM logs WHERE project_id=?", ids["p_new"]) == 0
        assert one("SELECT COUNT(*) FROM audit WHERE substr(created_at,1,10)>? AND action NOT LIKE 'backtest.%'", DAY) == 0
        # 豁免：真实回测与其审计保留；测试回测按默认规则删除
        assert one("SELECT COUNT(*) FROM backtests WHERE id=?", ids["bt_real"]) == 1
        assert one("SELECT COUNT(*) FROM backtests WHERE id=?", ids["bt_test"]) == 0
        assert one("SELECT COUNT(*) FROM audit WHERE action='backtest.run'") == 2
        # 豁免列：整改期限晚于演示日的隐患保留
        assert one("SELECT due_date FROM findings WHERE id=?", ids["finding_due"]) == "2026-09-30"
        # “只是被更新过”：行保留，可空列置空、不可空列退回到不晚于演示日
        assert one("SELECT read_at FROM notifications WHERE id=?", ids["notif_other"]) is None
        started = one("SELECT started_at FROM logs WHERE id=?", ids["log_reopened"])
        assert started and started[:10] <= DAY
        # F-19：同一收件人、同标题、同跳转、同一小时的重复消息只留一条
        assert one("SELECT COUNT(*) FROM notifications WHERE user_id=? AND title='待决策：场地泥泞积水'", ids["d_pm"]) == 1
        # Key 池登记清空（不复制 Key；真实 AI 模式由 demo_serve 从 .env 导入）
        assert one("SELECT COUNT(*) FROM ai_keys") == 0
        assert one("SELECT COUNT(*) FROM ai_settings WHERE k='seeded_fps'") == 0
    s = copied["summary"]
    assert s["day"] == DAY and s["removed"].get("notifications", 0) >= 1 and s["media_files"] >= 1


# ---------------------------------------------------------------------------------------------- AC-7.1
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
SKIP_SUFFIX = ("/docx", "/file", "/thumb", "/export-progress")


def _param_values(c, path) -> list[str]:
    q = {"aid": "SELECT id FROM alerts", "bid": "SELECT id FROM backtests", "log_id": "SELECT id FROM logs", "tid": "SELECT id FROM tickets",
         "nid": "SELECT id FROM circle_notices", "uid": "SELECT id FROM users", "peer": "SELECT id FROM users",
         "activity_code": "SELECT code FROM activities"}
    if path.startswith("/api/findings/"):
        sql = "SELECT id FROM findings"
    elif path.startswith("/api/reports/template"):
        return ["week", "month"]
    elif path.startswith("/api/reports/"):
        sql = "SELECT id FROM reports"
    elif path.startswith("/api/conceal/"):
        sql = "SELECT id FROM conceal_records"
    elif path.startswith("/api/pours/"):
        sql = "SELECT id FROM pours"
    elif path.startswith("/api/claims/packs/"):
        sql = "SELECT id FROM claim_packs"
    else:
        name = re.findall(r"\{(\w+)\}", path)[0]
        sql = q.get(name)
    if not sql:
        return ["1"]
    vals = [str(r[0]) for r in c.execute(sql + " ORDER BY 1 DESC LIMIT 3").fetchall()]
    return vals or ["1"]


def _scan(obj, key, rec_keys, out):
    """带时刻的时间值不论键名都查；只有日期的值，只查键名是库里时间列名（或 today/now/as_of）的——P6 计划日、预测完工日不是记录时间。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _scan(v, str(k), rec_keys, out)
    elif isinstance(obj, list):
        for v in obj:
            _scan(v, key, rec_keys, out)
    elif isinstance(obj, str):
        if STAMP.match(obj) or (key in rec_keys and DATE.match(obj)):
            out.append((key, obj))


def _exempt_values(c) -> tuple[set, set]:
    """豁免清单落到接口上：整行豁免的记录里的时间值；整列豁免的列名。"""
    values, keys = set(), set()
    for t, col, where in dc.EXEMPT:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
            continue
        if col is not None:
            keys.add(col)
            continue
        cols = [r[1] for r in c.execute(f'PRAGMA table_info("{t}")') if AC_TIME_COL.search(r[1])]
        for r in c.execute(f'SELECT {", ".join(cols)} FROM "{t}" WHERE {where}').fetchall():
            values.update(v for v in r if v)
    return values, keys


@pytest.fixture(scope="module")
def demo_app(copied):
    """让测试进程的 app 跑在演示副本上（复制到测试自己的数据目录，不碰业务库），并设置 ZB_DEMO_DAY。"""
    _reset_db()
    settings.db_path.unlink()
    shutil.copyfile(copied["dst"] / "zhimou.db", settings.db_path)
    dbmod._initialized = False
    old = os.environ.get("ZB_DEMO_DAY")
    os.environ["ZB_DEMO_DAY"] = DAY
    yield copied
    if old is None:
        os.environ.pop("ZB_DEMO_DAY", None)
    else:
        os.environ["ZB_DEMO_DAY"] = old
    _reset_db()


def _crawl() -> tuple[int, list[str]]:
    """演示时钟下，以各岗位身份遍历全部 GET 接口，返回（抓到的 JSON 返回数, 晚于演示日的时间）。"""
    with time_machine.travel(at(2026, 9, 22, 15), tick=False):
        dbmod.init_db()
        with _ro(settings.db_path) as c:
            exempt_vals, exempt_keys = _exempt_values(c)
            rec_keys = {"today", "now", "as_of"} | {col for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                                                    for col in [r[1] for r in c.execute(f'PRAGMA table_info("{t[0]}")')] if AC_TIME_COL.search(col)}
            routes = sorted({r.path for r in M.app.routes if "GET" in getattr(r, "methods", set()) and r.path.startswith("/api/")})
            assert len(routes) >= 100
            urls = []
            for p in routes:
                if p.endswith(SKIP_SUFFIX) or "/dm/files/" in p:
                    continue
                if "{" in p:
                    urls += [re.sub(r"\{\w+\}", v, p) for v in _param_values(c, p)]
                else:
                    urls.append(p)
        bad, ok = [], 0
        for uname, role, _ in ROLE_USERS:
            cl = _client(uname)
            for u in urls:
                r = cl.get(u)
                if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                    continue
                ok += 1
                found = []
                _scan(r.json(), "", rec_keys, found)
                for k, v in found:
                    if v[:10] > DAY and v not in exempt_vals and k not in exempt_keys:
                        bad.append(f"{role} {u} {k}={v}")
    return ok, bad


def test_ac71_all_get_endpoints_show_nothing_after_demo_day(demo_app):
    ok, bad = _crawl()
    assert ok >= 300, ok                                              # 确实抓到了大量接口返回
    assert not bad, bad[:20]


def test_demo_clock_endpoint_and_today(demo_app):
    with time_machine.travel(at(2026, 9, 22, 15), tick=False):
        cl = TestClient(M.app)
        r = cl.get("/api/demo/clock")                                # 不需要登录：登录页也要看得出是演示实例
        assert r.status_code == 200
        d = r.json()
        assert d["demo"] is True and d["day"] == DAY and d["today"] == DAY and d["scheduler"] is False
        assert set(d) <= {"demo", "day", "today", "now", "scheduler", "ai"}
        assert _client("d_pm").get("/api/me").json()["today"] == DAY
    os.environ.pop("ZB_DEMO_DAY")
    try:
        assert TestClient(M.app).get("/api/demo/clock").json() == {"demo": False}
    finally:
        os.environ["ZB_DEMO_DAY"] = DAY


# ---------------------------------------------------------------------------------------------- AC-7.2 / D16
def test_ac72_demo_serve_forces_scheduler_off(monkeypatch, tmp):
    monkeypatch.setenv("ZB_SCHEDULER", "1")
    for k in ("ZB_DATA_DIR", "ZB_AI_PROVIDER", "ZB_ENV_FILE", "ZB_DEMO_DAY", "ZB_DEV_LOGIN", "ZB_AI_API_KEY"):
        monkeypatch.setenv(k, os.environ.get(k, ""))
    ds.configure_env(tmp, date(2026, 9, 22), real_ai=False, env_file=tmp / "x.env")
    assert os.environ["ZB_SCHEDULER"] == "0"                         # 先断言，再进 lifespan，免得真起一个调度线程
    with TestClient(M.app) as cl:
        assert not any(t.name == "detect-scheduler" for t in threading.enumerate())
        assert cl.get("/api/demo/clock").json()["scheduler"] is False


def test_d16_mock_by_default_and_real_ai_is_explicit(monkeypatch, tmp, capsys):
    for k in ("ZB_SCHEDULER", "ZB_DATA_DIR", "ZB_AI_PROVIDER", "ZB_ENV_FILE", "ZB_DEMO_DAY", "ZB_DEV_LOGIN"):
        monkeypatch.setenv(k, os.environ.get(k, ""))
    monkeypatch.setenv("ZB_AI_API_KEY", "sk-should-be-dropped")
    monkeypatch.setenv("ZB_AI_API_KEYS", "a,b")
    assert ds.parse([]).real_ai is False                              # 默认 mock
    ds.configure_env(tmp, date(2026, 9, 22), real_ai=False, env_file=tmp / "x.env")
    assert os.environ["ZB_AI_PROVIDER"] == "mock" and os.environ["ZB_ENV_FILE"] == os.devnull
    assert "ZB_AI_API_KEY" not in os.environ and "ZB_AI_API_KEYS" not in os.environ
    env = tmp / ".env"
    ds.configure_env(tmp, date(2026, 9, 22), real_ai=True, env_file=env)
    assert ds.parse(["--real-ai"]).real_ai is True
    assert os.environ["ZB_AI_PROVIDER"] == "jiaorong" and os.environ["ZB_ENV_FILE"] == str(env)
    print(ds.real_ai_banner(tmp))
    out = capsys.readouterr().out
    assert "真实" in out and "授权" in out and "ai_keys.json" in out


def test_d16_key_pool_file_removed_and_registry_reset(tmp):
    db = tmp / "zhimou.db"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE ai_keys(id INTEGER PRIMARY KEY, fp TEXT); CREATE TABLE ai_usage(key_id INTEGER, day TEXT);"
                    "CREATE TABLE ai_settings(k TEXT PRIMARY KEY, v TEXT);"
                    "INSERT INTO ai_keys(fp) VALUES ('a'); INSERT INTO ai_usage VALUES (1, '2026-09-22');"
                    "INSERT INTO ai_settings VALUES ('seeded_fps', '[\"a\"]'), ('model_core', '\"m\"');")
    c.commit()
    c.close()
    for n in ("ai_keys.json", "ai_keys.tmp"):
        (tmp / n).write_text("{}", encoding="utf-8")
    ds.drop_key_files(tmp)
    ds.reset_key_pool(db)
    assert not (tmp / "ai_keys.json").exists() and not (tmp / "ai_keys.tmp").exists()
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM ai_keys").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 0
    assert c.execute("SELECT k FROM ai_settings").fetchall() == [("model_core",)]   # 其他 AI 设置不动
    c.close()


def test_https_takes_leaf_cert_paths_from_self_signed(tmp, capsys):
    """--https：按 T-10b 的 _self_signed 返回字典取叶子证书路径（不起服务）；证书放在副本自己的 tls/ 下。"""
    assert ds.parse([]).https is False and ds.parse(["--https"]).https is True
    assert ds.ssl_options(tmp, https=False) == {}
    ssl = ds.ssl_options(tmp, https=True, port=8650)
    assert ssl == {"ssl_keyfile": str(tmp / "tls" / "key.pem"), "ssl_certfile": str(tmp / "tls" / "cert.pem")}
    assert Path(ssl["ssl_keyfile"]).is_file() and Path(ssl["ssl_certfile"]).is_file()
    assert b"PRIVATE KEY" in Path(ssl["ssl_keyfile"]).read_bytes() and b"CERTIFICATE" in Path(ssl["ssl_certfile"]).read_bytes()
    assert (tmp / "tls" / "zhimou-ca.crt").is_file()                  # 手机安装用的根证书
    assert "HTTPS 已启用" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------- AC-7.4：进程入口
def _run(mod: str, *args, env=None, timeout=60) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONIOENCODING="utf-8", **(env or {}))
    return subprocess.run([sys.executable, "-m", mod, *args], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, env=e)


def _mini_db(folder: Path, projects: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(folder / "zhimou.db")
    c.execute("CREATE TABLE projects(id INTEGER PRIMARY KEY, name TEXT, code TEXT, config_json TEXT, created_at TEXT)")
    for i, p in enumerate(projects, 1):
        c.execute("INSERT INTO projects VALUES (?, ?, ?, ?, ?)", (i, p["name"], f"P{i}", json.dumps(p["cfg"]), "2026-09-01T00:00:00"))
    c.commit()
    c.close()
    return folder


def test_ac74_demo_copy_refuses_non_sim_db(tmp):
    src = _mini_db(tmp / "src", [{"name": "仿真", "cfg": {"simulation": True}}, {"name": "真实项目", "cfg": {}}])
    r = _run("tools.demo_copy", "--src-dir", str(src), "--dst", str(tmp / "out"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "非仿真" in r.stderr and not (tmp / "out").exists()
    empty = _mini_db(tmp / "empty", [])
    assert _run("tools.demo_copy", "--src-dir", str(empty), "--dst", str(tmp / "out2")).returncode == 2


def test_ac74_demo_serve_startup_checks_exit_2(tmp):
    # 非仿真项目
    bad = _mini_db(tmp / "bad", [{"name": "真实项目", "cfg": {}}])
    (bad / dc.MARKER).write_text(json.dumps({"day": DAY}), encoding="utf-8")
    r = _run("tools.demo_serve", "--data-dir", str(bad), "--port", "8659")
    assert r.returncode == 2 and "非仿真" in r.stderr, r.stderr
    # 不是演示副本（没有 DEMO_COPY.json）
    nomark = _mini_db(tmp / "nomark", [{"name": "仿真", "cfg": {"simulation": True}}])
    r = _run("tools.demo_serve", "--data-dir", str(nomark), "--port", "8659")
    assert r.returncode == 2 and dc.MARKER in r.stderr, r.stderr
    # 演示日与副本不一致
    (nomark / dc.MARKER).write_text(json.dumps({"day": "2026-09-21"}), encoding="utf-8")
    assert _run("tools.demo_serve", "--data-dir", str(nomark), "--port", "8659").returncode == 2
    # 业务数据目录、业务端口一律拒绝
    assert _run("tools.demo_serve", "--data-dir", str(ROOT / "data"), "--port", "8659").returncode == 2
    (nomark / dc.MARKER).write_text(json.dumps({"day": DAY}), encoding="utf-8")
    for port in ("8600", "8601"):
        assert _run("tools.demo_serve", "--data-dir", str(nomark), "--port", port).returncode == 2


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_demo_serve_end_to_end_mock(copied, tmp):
    """真正起一个演示实例（子进程，mock）：调度在环境里是 1 也被关掉；今天=演示日；响应头 Date 也是演示日；不写 Key 文件。"""
    import httpx
    data = tmp / "demo"
    shutil.copytree(copied["dst"], data)
    port = _free_port()
    e = dict(os.environ, PYTHONIOENCODING="utf-8", ZB_SCHEDULER="1", ZB_AI_API_KEY="sk-env-should-not-be-used")
    p = subprocess.Popen([sys.executable, "-m", "tools.demo_serve", "--data-dir", str(data), "--port", str(port)], cwd=str(ROOT),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=e)
    try:
        d, r = None, None
        end = time.monotonic() + 90
        while time.monotonic() < end and p.poll() is None:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/api/demo/clock", timeout=2)
                d = r.json()
                break
            except (httpx.HTTPError, ValueError):
                time.sleep(0.5)
        assert d, f"演示实例没有起来：rc={p.poll()}"
        assert d["demo"] is True and d["today"] == DAY and d["day"] == DAY
        assert d["scheduler"] is False and d["ai"] == "mock"
        # 响应头 Date 是 GMT：演示时钟按本机当地钟点走，当地刚过午夜时 GMT 还在前一天，所以换回本机时区再比日期（不依赖跑测试的钟点）
        from email.utils import parsedate_to_datetime
        assert parsedate_to_datetime(r.headers["date"]).astimezone().date().isoformat() == DAY
        assert not (data / "ai_keys.json").exists()
    finally:
        p.terminate()
        try:
            out = p.communicate(timeout=20)[0]
        except subprocess.TimeoutExpired:
            p.kill()
            out = p.communicate()[0]
    assert "演示日 2026-09-22" in out and "原始照片" in out and "离线模拟模型" in out


# ---------------------------------------------------------------------------------------------- 前端：页头标识 / runAction / 文案
UI_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
let clicked = 0, focused = 0, scrolled = 0;
const els = { f: { tagName: 'INPUT', type: 'file', click: () => clicked++, focus: () => focused++, scrollIntoView: () => scrolled++ },
              t: { tagName: 'INPUT', type: 'text', click: () => clicked++, focus: () => focused++, scrollIntoView: () => scrolled++ } };
const demo = process.argv[3] === 'demo';
const ctx = { console, isSim: () => false, isTech: () => false, go: () => {}, store: {}, setTimeout: (f) => f(),
  document: { getElementById: (id) => els[id] || null },
  Vue: { reactive: (o) => o },
  fetch: async () => ({ ok: true, json: async () => (demo ? { demo: true, day: '2026-09-22', today: '2026-09-22' } : { demo: false }) }) };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8') + '\nthis.__o = { PageHead, runAction, PAGE_HEADS };', ctx);
const o = ctx.__o, P = o.PageHead;
const mk = () => { const i = { page: 'desk', purpose: undefined, title: undefined }; if (P.data) Object.assign(i, P.data.call(i)); P.created && P.created.call(i); P.mounted && P.mounted.call(i); return i; };
const tag = (i) => P.computed.demoTag.call(i);
(async () => {
  const a = mk(), b = mk();
  await new Promise((r) => setImmediate(r)); await new Promise((r) => setImmediate(r));
  const both = [tag(a), tag(b)];
  P.unmounted.call(a);
  const after = tag(b);
  o.runAction({ id: 'f' }); const file = { clicked, focused };
  o.runAction({ id: 't' }); const text = { clicked, focused, scrolled };
  const pur = (page) => P.computed.pur.call({ page, purpose: undefined, get ph() { return P.computed.ph.call(this); } });
  process.stdout.write(JSON.stringify({ both, after, file, text, logEdit: pur('log_edit'), week: o.PAGE_HEADS.week, tmpl: P.template }));
})();
"""


def _ui(mode: str) -> dict:
    if not NODE:
        pytest.skip("没有 node，跳过 x_ui.js 校验")
    h = Path(tempfile.mkdtemp()) / "ui.js"
    h.write_text(UI_HARNESS, encoding="utf-8")
    r = subprocess.run([NODE, str(h), str(STATIC / "x_ui.js"), mode], capture_output=True, text=True, encoding="utf-8", timeout=60)
    shutil.rmtree(h.parent, ignore_errors=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_page_head_demo_badge_shows_once_per_page():
    d = _ui("demo")
    assert d["both"] == ["演示日 09-22", ""]                          # 同一页两个页头：只有最先挂载的那个显示
    assert d["after"] == "演示日 09-22"                               # 它卸载后由剩下的页头接着显示
    assert "demoTag" not in d["tmpl"]                                 # 验收 I16：开发环境信息不上页面，模板不再渲染演示日标签
    n = _ui("normal")
    assert n["both"] == ["", ""]                                      # 普通实例不显示
    src = (STATIC / "x_ui.js").read_text(encoding="utf-8")
    assert src.count("/api/demo/clock") == 1


def test_run_action_opens_file_picker_and_f18_copy():
    d = _ui("normal")
    assert d["file"] == {"clicked": 1, "focused": 0}                  # U-05③：文件框直接弹出选择，不是只聚焦
    assert d["text"]["clicked"] == 1 and d["text"]["focused"] == 1 and d["text"]["scrolled"] == 1
    assert "P6" not in d["week"][1] and "更新进度计划" in d["week"][1]   # F-18①
    assert "确认后提交" not in d["logEdit"] and len(d["logEdit"]) <= 25   # F-18②：审核人看到的副标题不是作者视角


def test_demo_clock_starts_after_data_cutoff():
    """验收 I13：演示时钟默认从演示日 22:45 起走（数据截止 22:40 之后），--time now 沿用真实钟点，格式不对报错。"""
    from datetime import date as _d
    t = ds.clock_start(_d(2026, 10, 4))
    assert (t.date().isoformat(), t.strftime("%H:%M")) == ("2026-10-04", "22:45")
    assert ds.clock_start(_d(2026, 10, 4), "23:10").strftime("%H:%M") == "23:10"
    assert ds.clock_start(_d(2026, 10, 4), "now").date().isoformat() == "2026-10-04"
    with pytest.raises(ds.dc.CheckError):
        ds.clock_start(_d(2026, 10, 4), "2300")
