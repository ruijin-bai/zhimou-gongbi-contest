"""SQLite 存储：单文件、零运维。每次请求一个连接，WAL 支持并发读。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from .config import settings
from .roles import ROLES

SCHEMA_VERSION = 10
_ROLE_CHECK = ",".join(f"'{r}'" for r in ROLES)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN (__ROLES__)),
    password_hash TEXT NOT NULL,
    project_id INTEGER REFERENCES projects(id),
    wbs_scope TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS p6_imports (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    proj_name TEXT,
    data_date TEXT,
    activity_count INTEGER NOT NULL,
    relation_count INTEGER NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    imported_by INTEGER REFERENCES users(id),
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES p6_imports(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    wbs_path TEXT NOT NULL DEFAULT '',
    task_type TEXT,
    status TEXT,
    target_start TEXT, target_finish TEXT,
    early_start TEXT, early_finish TEXT,
    late_start TEXT, late_finish TEXT,
    act_start TEXT, act_finish TEXT,
    orig_dur_d REAL, remain_dur_d REAL,
    total_float_d REAL,
    phys_pct REAL,
    is_critical INTEGER NOT NULL DEFAULT 0,
    is_milestone INTEGER NOT NULL DEFAULT 0,
    UNIQUE (import_id, code)
);
CREATE INDEX IF NOT EXISTS ix_act_import ON activities(import_id);
CREATE TABLE IF NOT EXISTS relations (
    import_id INTEGER NOT NULL REFERENCES p6_imports(id) ON DELETE CASCADE,
    pred_code TEXT NOT NULL,
    succ_code TEXT NOT NULL,
    kind TEXT NOT NULL,
    lag_d REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_rel_import ON relations(import_id);
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    uploader_id INTEGER NOT NULL REFERENCES users(id),
    log_id INTEGER REFERENCES logs(id),
    sha256 TEXT NOT NULL,
    file TEXT NOT NULL,
    ai_file TEXT NOT NULL,
    thumb TEXT NOT NULL,
    original_name TEXT,
    width INTEGER, height INTEGER,
    taken_at TEXT,
    has_gps INTEGER NOT NULL DEFAULT 0,
    dhash TEXT,
    purpose TEXT NOT NULL DEFAULT 'log',
    safety_status TEXT NOT NULL DEFAULT 'pending',
    uploaded_at TEXT NOT NULL,
    geo_lat REAL, geo_lon REAL, geo_acc REAL, geo_place TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_media_log ON media(log_id);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    author_id INTEGER NOT NULL REFERENCES users(id),
    log_date TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'ai' CHECK (mode IN ('ai','manual')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted','approved','returned')),
    weather TEXT NOT NULL DEFAULT '',
    voice_text TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL DEFAULT '{}',
    ai_draft_json TEXT,
    checks_json TEXT NOT NULL DEFAULT '[]',
    ai_run_id INTEGER REFERENCES ai_runs(id),
    started_at TEXT NOT NULL,
    drafted_at TEXT,
    submitted_at TEXT,
    edit_seconds REAL,
    edit_ratio REAL,
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    review_note TEXT NOT NULL DEFAULT '',
    qa_json TEXT NOT NULL DEFAULT '[]',
    dq_json TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    period TEXT NOT NULL DEFAULT 'day' CHECK (period IN ('day','week')),
    period_end TEXT,
    auto INTEGER NOT NULL DEFAULT 0,
    UNIQUE (project_id, author_id, log_date, mode, period)
);
CREATE TABLE IF NOT EXISTS log_progress (
    id INTEGER PRIMARY KEY,
    log_id INTEGER NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    activity_code TEXT NOT NULL,
    qty REAL,
    unit TEXT NOT NULL DEFAULT '',
    pct REAL,
    note TEXT NOT NULL DEFAULT '',
    voided_at TEXT,
    void_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_prog_code ON log_progress(project_id, activity_code, log_date);
CREATE TABLE IF NOT EXISTS review_fields (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    field_key TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    ai_value TEXT,
    final_value TEXT,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    decided_by INTEGER,
    decided_at TEXT NOT NULL,
    UNIQUE (kind, ref_type, ref_id, field_key)
);
CREATE INDEX IF NOT EXISTS ix_review_proj ON review_fields(project_id, kind);
CREATE TABLE IF NOT EXISTS ai_runs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_meta_json TEXT NOT NULL DEFAULT '{}',
    output_text TEXT,
    ok INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    tokens_json TEXT,
    error TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    import_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    activity_code TEXT NOT NULL,
    activity_name TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('red','orange','yellow')),
    rule TEXT NOT NULL,
    title TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    explanation_json TEXT,
    ai_run_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','confirmed','false_alarm','closed')),
    feedback_by INTEGER, feedback_note TEXT NOT NULL DEFAULT '', feedback_at TEXT,
    first_detected TEXT NOT NULL,
    last_detected TEXT NOT NULL,
    as_of TEXT NOT NULL,
    UNIQUE (project_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    alert_id INTEGER REFERENCES alerts(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    channels_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    read_at TEXT,
    link_json TEXT  -- 跳转目标 {"route", "params"}；为空则消息页不放按钮（预警看 alert_id）
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL CHECK (kind IN ('week','month')),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    narrative_json TEXT,
    number_check_json TEXT,
    ai_run_id INTEGER,
    status TEXT NOT NULL DEFAULT 'draft',
    created_by INTEGER,
    created_at TEXT NOT NULL,
    gen_seconds REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    project_id INTEGER,
    user_id INTEGER,
    kind TEXT NOT NULL,
    ref_id INTEGER,
    value REAL,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    author_id INTEGER NOT NULL REFERENCES users(id),
    note_date TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    media_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'text',
    seconds REAL,
    created_at TEXT NOT NULL,
    geo_lat REAL, geo_lon REAL, geo_acc REAL, geo_place TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_notes_day ON notes(project_id, author_id, note_date);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('high','medium','low','info')),
    title TEXT NOT NULL,
    body_json TEXT NOT NULL DEFAULT '{}',
    ref_type TEXT, ref_id INTEGER,
    activity_code TEXT,
    fingerprint TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','rectifying','resolved','rejected','dismissed')),
    assignee_id INTEGER REFERENCES users(id),
    due_date TEXT,
    ai_run_id INTEGER,
    rectify_media_id INTEGER REFERENCES media(id),
    verify_json TEXT,
    feedback_by INTEGER, feedback_note TEXT NOT NULL DEFAULT '', feedback_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    confirmed_by INTEGER,  -- 人工确认人；confirmed_at 为空且仍为 open 即“AI 候选·待确认”（不计入台账/督办/闭环率）
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_find_proj ON findings(project_id, agent, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_find_fp ON findings(project_id, fingerprint) WHERE fingerprint IS NOT NULL;
CREATE TABLE IF NOT EXISTS boq_items (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    unit_price REAL,
    currency TEXT NOT NULL DEFAULT '',
    contract_qty REAL,
    UNIQUE (project_id, code)
);
CREATE TABLE IF NOT EXISTS boq_map (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    activity_code TEXT NOT NULL,
    boq_id INTEGER NOT NULL REFERENCES boq_items(id) ON DELETE CASCADE,
    factor REAL NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'suggested' CHECK (status IN ('suggested','confirmed','rejected')),
    reason TEXT NOT NULL DEFAULT '',
    ai_run_id INTEGER,
    confirmed_by INTEGER,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, activity_code)
);
CREATE TABLE IF NOT EXISTS output_records (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    planned REAL,
    actual REAL,
    baseline REAL,
    currency TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'record',
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (project_id, period_start, source)
);
CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    day TEXT NOT NULL,
    content_json TEXT NOT NULL,
    ai_run_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge (
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    ai_run_id INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, kind, key)
);
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    import_id INTEGER NOT NULL,
    as_of TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_likes (
    project_id INTEGER NOT NULL,
    post_key TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, post_key, user_id)
);
CREATE TABLE IF NOT EXISTS feed_comments (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    post_key TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feed_comments ON feed_comments(project_id, post_key);"""

SCHEMA = SCHEMA.replace("__ROLES__", _ROLE_CHECK)

_init_lock = threading.Lock()
_initialized = False


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    return json.loads(value)


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        settings.export_dir.mkdir(parents=True, exist_ok=True)
        conn = connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            _migrate(conn)
            conn.executescript(SCHEMA)
            from . import routers  # 扩展模块自带的表（延迟导入，避免循环）
            routers.apply_schema(conn)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            conn.close()
        _initialized = True


def _migrate(conn: sqlite3.Connection) -> None:
    ver = conn.execute("PRAGMA user_version").fetchone()["user_version"]
    if ver == 0 or ver >= SCHEMA_VERSION:
        return
    if ver < 2:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
        for name, ddl in (("dhash", "TEXT"), ("purpose", "TEXT NOT NULL DEFAULT 'log'"), ("safety_status", "TEXT NOT NULL DEFAULT 'pending'")):
            if name not in cols:
                conn.execute(f"ALTER TABLE media ADD COLUMN {name} {ddl}")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
        if "qa_json" not in cols:
            conn.execute("ALTER TABLE logs ADD COLUMN qa_json TEXT NOT NULL DEFAULT '[]'")
        if "dq_json" not in cols:
            conn.execute("ALTER TABLE logs ADD COLUMN dq_json TEXT")
        # users 角色约束增加 leader：SQLite 需重建表（关闭外键与改名引用改写）
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.executescript("""
            BEGIN;
            CREATE TABLE users_v2 (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('tech','manager','leader','admin')), password_hash TEXT NOT NULL,
                project_id INTEGER REFERENCES projects(id), wbs_scope TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            INSERT INTO users_v2 SELECT id, username, name, role, password_hash, project_id, wbs_scope, email, active, created_at FROM users;
            DROP TABLE users;
            ALTER TABLE users_v2 RENAME TO users;
            COMMIT;""")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        ver = 2
    if ver < 3:
        # logs 增加周期（日/周）：唯一约束变化需重建表
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()]
        conn.executescript(f"""
            BEGIN;
            CREATE TABLE logs_v3 (
                id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), author_id INTEGER NOT NULL REFERENCES users(id),
                log_date TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'ai' CHECK (mode IN ('ai','manual')),
                status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted','approved','returned')),
                weather TEXT NOT NULL DEFAULT '', voice_text TEXT NOT NULL DEFAULT '', content_json TEXT NOT NULL DEFAULT '{{}}',
                ai_draft_json TEXT, checks_json TEXT NOT NULL DEFAULT '[]', ai_run_id INTEGER REFERENCES ai_runs(id),
                started_at TEXT NOT NULL, drafted_at TEXT, submitted_at TEXT, edit_seconds REAL, edit_ratio REAL,
                reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT, review_note TEXT NOT NULL DEFAULT '',
                qa_json TEXT NOT NULL DEFAULT '[]', dq_json TEXT, version INTEGER NOT NULL DEFAULT 1,
                period TEXT NOT NULL DEFAULT 'day' CHECK (period IN ('day','week')), period_end TEXT, auto INTEGER NOT NULL DEFAULT 0,
                UNIQUE (project_id, author_id, log_date, mode, period));
            INSERT INTO logs_v3 ({", ".join(cols)}) SELECT {", ".join(cols)} FROM logs;
            DROP TABLE logs;
            ALTER TABLE logs_v3 RENAME TO logs;
            COMMIT;""")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        ver = 3
    if ver < 4:
        # 8 个业务角色 + 分包单位名称：放宽角色约束并增加 company，需重建 users 表
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.executescript(f"""
            BEGIN;
            CREATE TABLE users_v4 (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ({_ROLE_CHECK})), password_hash TEXT NOT NULL,
                project_id INTEGER REFERENCES projects(id), wbs_scope TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, company TEXT NOT NULL DEFAULT '');
            INSERT INTO users_v4 (id, username, name, role, password_hash, project_id, wbs_scope, email, active, created_at)
                SELECT id, username, name, role, password_hash, project_id, wbs_scope, email, active, created_at FROM users;
            DROP TABLE users;
            ALTER TABLE users_v4 RENAME TO users;
            COMMIT;""")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
    if ver < 5:
        # 照片与随手记增加手机定位（经纬度、精度、地点/部位）
        for table in ("media", "notes"):
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not cols:  # 表还不存在，随后由 SCHEMA 按新结构创建
                continue
            for name, ddl in (("geo_lat", "REAL"), ("geo_lon", "REAL"), ("geo_acc", "REAL"), ("geo_place", "TEXT NOT NULL DEFAULT ''")):
                if name not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    if ver < 6:
        # 工程量留痕：退回的数量不再物理删除，标记失效（统计一律排除 voided_at 非空的行）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(log_progress)").fetchall()}
        if cols:  # 表还不存在则随后由 SCHEMA 按新结构创建
            for name, ddl in (("voided_at", "TEXT"), ("void_reason", "TEXT NOT NULL DEFAULT ''")):
                if name not in cols:
                    conn.execute(f"ALTER TABLE log_progress ADD COLUMN {name} {ddl}")
    if ver < 7:
        # AI 隐患先“候选”：findings 加人工确认字段。历史隐患一律视为已确认（confirmed_at 回填为 created_at），既有台账数字不变
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(findings)").fetchall()}
        if cols:  # 表还不存在则随后由 SCHEMA 按新结构创建
            fresh = "confirmed_at" not in cols
            for name, ddl in (("confirmed_by", "INTEGER"), ("confirmed_at", "TEXT")):
                if name not in cols:
                    conn.execute(f"ALTER TABLE findings ADD COLUMN {name} {ddl}")
            if fresh:  # 仅首次加列时回填，保证重复执行不会把之后新增的候选误当作已确认
                conn.execute("UPDATE findings SET confirmed_at=created_at WHERE confirmed_at IS NULL")
    # ver < 8：新增 review_fields（逐字段 AI 原值/人工最终值）。只是新表，由 SCHEMA 的 CREATE TABLE IF NOT EXISTS 幂等创建，无需改既有表
    if ver < 9:
        # 消息跳转目标：notifications 加可空列 link_json，旧记录为空（消息页不放按钮）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(notifications)").fetchall()}
        if cols and "link_json" not in cols:  # 表还不存在则随后由 SCHEMA 按新结构创建
            conn.execute("ALTER TABLE notifications ADD COLUMN link_json TEXT")
    if ver < 10:
        # 产值按周报口径：planned 为“更新产值计划”，新增可空列 baseline 为“基准产值计划”，旧记录为空（显示“未录入”，不补零）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(output_records)").fetchall()}
        if cols and "baseline" not in cols:  # 表还不存在则随后由 SCHEMA 按新结构创建
            conn.execute("ALTER TABLE output_records ADD COLUMN baseline REAL")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务；IMMEDIATE 防止并发提交/审批的写写竞争。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def one(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> dict | None:
    return conn.execute(sql, tuple(params)).fetchone()


def all_(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> list[dict]:
    return conn.execute(sql, tuple(params)).fetchall()


def insert(conn: sqlite3.Connection, table: str, values: dict) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    return int(cur.lastrowid)


def audit(conn: sqlite3.Connection, user_id: int | None, action: str, target: str = "", detail: str = "") -> None:
    insert(conn, "audit", {"user_id": user_id, "action": action, "target": target, "detail": detail, "created_at": now_iso()})


def event(conn: sqlite3.Connection, kind: str, *, project_id=None, user_id=None, ref_id=None, value=None, data=None) -> None:
    insert(conn, "events", {
        "project_id": project_id, "user_id": user_id, "kind": kind, "ref_id": ref_id,
        "value": value, "data_json": dumps(data or {}), "created_at": now_iso(),
    })
