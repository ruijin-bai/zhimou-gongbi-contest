"""演示副本（T-07，D8）：把业务库只读复制成演示副本，清理晚于演示日的记录。

用法（在仓库根目录）：
  python -m tools.demo_copy                 # data/ → data_demo/，演示日 2026-09-22
  python -m tools.demo_copy --force         # 覆盖已有的演示副本（只覆盖带 DEMO_COPY.json 的目录）
  python -m tools.demo_copy --src-dir X --dst Y --day 2026-09-22
然后用 python -m tools.demo_serve 启动演示实例。

- 业务库只读：只读 URI 打开（库空闲、旁边没有 -wal/-shm 时加 immutable=1，不留下任何文件），用 sqlite backup API 复制；
  所有清理只发生在副本上。不导入 app，不碰业务数据目录里的其他文件。
- 启动检查（AC-7.4）：源库必须有项目且全部是仿真项目（projects.config_json.simulation=true），否则退出码 2，不复制。
- 副本含原始照片（media/），只在本机使用、不外传，录完即可删除；不复制 ai_keys.json、.secret、tls/（AC-7.5）。
  副本里的 Key 池登记（只有指纹）一并清空；真实 AI 模式由 demo_serve 从仓库 .env 导入（D16）。
- 清理按表结构驱动（G-07③）：表按 sqlite_master 枚举，时间列按列名识别，没登记过的新表走默认规则：
  1. 默认：行的最大时间晚于演示日 → 删除；
  2. “只是被更新过”的列（UPDATE_COLS）：行在演示日前已存在，之后只是被改过、读过、再次检出 → 行保留，
     可空列置空，不可空列退回到该行不晚于演示日的最晚时间；
  3. 演示日前就有、但 AI 产出是之后重新生成的（AI_OUTPUT_COLS，如预警的 AI 分析）→ 清空该列；
  4. 豁免清单 EXEMPT（AC-7.1 与 AC-7.3 共用，N-10①）里的行和列不动。
  然后按外键和 project_id 清掉悬空引用，合并同一小时的重复消息（F-19），去掉只被已清理记录引用的照片文件，最后自检（AC-7.3）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DAY = "2026-09-22"
DB_NAME = "zhimou.db"
MARKER = "DEMO_COPY.json"
NOTE = "副本含原始照片（media/），只在本机使用、不外传，录完即可删除；未复制 ai_keys.json、.secret、tls/。"

# AC-7.3 的时间列：按列名识别
TIME_COL = re.compile(r"(?:_at|_date)$|^(?:day|first_detected|last_detected|sent_at)$")
# 清理时也按它判断“哪天的记录”，但不在 AC-7.3 的列名规则里。as_of 是预警/预测的判定日：晚于演示日说明标题、等级、
# 天数已是之后的判定（如“超期 24 天”），整行不进副本
EXTRA_TIME_COLS = frozenset({"as_of"})
# “只是被更新过”的列：行早已存在，这一列只记录之后的改动、阅读或再次检出
UPDATE_COLS = frozenset({"updated_at", "read_at", "last_detected", "last_at", "checked_at", "started_at"})
# AI 产出列：行在演示日前就有，但这一列是演示日之后 AI 重新生成的（关联的 AI 调用晚于演示日，或内容里的判定日晚于演示日）
# → 清空，界面按“还没有 AI 分析”显示，可以现场重新分析
AI_OUTPUT_COLS = {"alerts": ("explanation_json",)}
# 登记照片/附件文件的列（路径相对 media/）：记录被清理后，只被它引用的文件也不留在副本里
FILE_REFS = (("media", ("file", "ai_file", "thumb")), ("dm_files", ("file", "thumb")))

# 豁免清单（N-10①）：AC-7.1（接口返回）与 AC-7.3（副本各表）共用，清理时不动。
# (表, 列, 行条件)：列为 None 表示满足行条件的整行豁免；行条件为 None 表示整列豁免。
EXEMPT = (
    ("backtests", None, "source = 'real'"),        # 真实回测：用真实 XER 做的，不在仿真时间线内（T-04a）
    ("audit", None, "action LIKE 'backtest.%'"),    # 回测的审计记录
    ("findings", "due_date", None),                 # 整改期限：按定义可以晚于演示日
    ("meeting_items", "due_date", None),            # 例会行动项期限
    ("gate_items", "due_date", None),               # 放行清单缺项的到期日
    ("quick_tokens", "expires_at", None),           # 扫码上报二维码的有效期
)


class CheckError(Exception):
    """启动检查不通过：进程入口以退出码 2 结束。"""


# ----------------------------------------------------------------------------- 只读打开与启动检查

def open_readonly(db: Path) -> sqlite3.Connection:
    db = Path(db).resolve()
    if not db.is_file():
        raise CheckError(f"找不到数据库 {db}")
    idle = not any(Path(str(db) + s).exists() for s in ("-wal", "-shm", "-journal"))
    # 库空闲时用 immutable：只读连接打开 WAL 库会在旁边建 -wal/-shm 且关不掉；库正被服务使用时走正常只读（读得到 WAL 里的新数据）
    # immutable 下若恰好有进程写库，读到的可能不一致：复制后的完整性检查不通过就整份作废（见 make_copy）
    conn = sqlite3.connect(db.as_uri() + ("?mode=ro&immutable=1" if idle else "?mode=ro"), uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def sim_check(conn: sqlite3.Connection) -> list[tuple]:
    """库里必须有项目，且全部是仿真项目（进程级改时钟只能用于纯仿真库）。"""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone():
        raise CheckError("库里没有 projects 表")
    rows = conn.execute("SELECT id, code, config_json FROM projects ORDER BY id").fetchall()
    if not rows:
        raise CheckError("库里没有项目")
    real = []
    for pid, code, cfg in rows:
        try:
            sim = bool((json.loads(cfg or "{}") or {}).get("simulation"))
        except ValueError:
            sim = False
        if not sim:
            real.append(f"{code}(id={pid})")
    if real:
        raise CheckError("库里有非仿真项目：" + "、".join(real) + "；演示日只能用于纯仿真库")
    return rows


def startup_check(data_dir: Path, day: str) -> dict:
    """demo_serve 的启动检查：必须是 demo_copy 做出的演示副本、演示日一致、只含仿真项目。"""
    data_dir = Path(data_dir)
    mk = data_dir / MARKER
    if not mk.is_file():
        raise CheckError(f"{data_dir} 不是演示副本（没有 {MARKER}）；先运行 python -m tools.demo_copy")
    try:
        marker = json.loads(mk.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CheckError(f"{MARKER} 无法读取") from exc
    if marker.get("day") != day:
        raise CheckError(f"副本的演示日是 {marker.get('day')}，与要求的 {day} 不一致；请重新制作副本")
    conn = open_readonly(data_dir / DB_NAME)
    try:
        sim_check(conn)
    finally:
        conn.close()
    return marker


# ----------------------------------------------------------------------------- 表结构

def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _colinfo(conn, table: str) -> dict[str, bool]:
    """列名 → 是否 NOT NULL。"""
    return {r[1]: bool(r[3]) for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _row_exempt(table: str) -> str | None:
    conds = [w for t, c, w in EXEMPT if t == table and c is None]
    return " OR ".join(f"({w})" for w in conds) or None


def _col_exempt(table: str, col: str) -> bool:
    return any(t == table and c == col and w is None for t, c, w in EXEMPT)


def _guard(table: str) -> str:
    cond = _row_exempt(table)
    return f" AND NOT ({cond})" if cond else ""


def _late(col: str) -> str:
    return f'substr("{col}",1,10) > :day'


def _add(stats: dict, table: str, n: int) -> None:
    if n and n > 0:
        stats[table] = stats.get(table, 0) + n


# ----------------------------------------------------------------------------- 清理（只在副本上）

def _reset_update_col(conn, table: str, col: str, notnull: bool, anchors: list[str], day: str, stats: dict) -> None:
    guard = _guard(table)
    if not notnull:
        _add(stats, f"~{table}.{col}", conn.execute(f'UPDATE "{table}" SET "{col}"=NULL WHERE {_late(col)}{guard}', {"day": day}).rowcount)
        return
    sel = ", ".join(f'"{c}"' for c in anchors) or "NULL"
    try:
        rows = conn.execute(f'SELECT rowid, "{col}", {sel} FROM "{table}" WHERE {_late(col)}{guard}', {"day": day}).fetchall()
    except sqlite3.OperationalError:  # 没有 rowid 的表：退回默认规则（删行）
        _add(stats, table, conn.execute(f'DELETE FROM "{table}" WHERE {_late(col)}{guard}', {"day": day}).rowcount)
        return
    for rid, old, *vals in rows:
        old = str(old)
        suffix = old[19:] if len(old) > 19 and old[10:11] in ("T", " ") else ""
        ok = [str(v) for v in vals if v and str(v)[:10] <= day]
        best = max(ok) if ok else day
        if len(best) <= 10:
            best = f"{best[:10]}T00:00:00{suffix}"
        conn.execute(f'UPDATE "{table}" SET "{col}"=? WHERE rowid=?', (best, rid))
    _add(stats, f"~{table}.{col}", len(rows))


def _clear_late_ai_output(conn, day: str, stats: dict) -> None:
    tables = set(_tables(conn))
    for t, cols in AI_OUTPUT_COLS.items():
        if t not in tables:
            continue
        info = _colinfo(conn, t)
        for c in (c for c in cols if c in info):
            cond = [f"""substr(CASE WHEN json_valid("{c}") THEN json_extract("{c}", '$.as_of') END,1,10) > :day"""]
            sets = f'"{c}"=NULL'
            if "ai_run_id" in info and "ai_runs" in tables:
                cond.append(f"ai_run_id IN (SELECT id FROM ai_runs WHERE {_late('created_at')})")
                sets += ", ai_run_id=NULL"
            n = conn.execute(f'UPDATE "{t}" SET {sets} WHERE "{c}" IS NOT NULL AND ({" OR ".join(cond)}){_guard(t)}', {"day": day}).rowcount
            _add(stats, f"~{t}.{c}", n)


def _clean_times(conn, day: str, stats: dict) -> None:
    for t in _tables(conn):
        info = _colinfo(conn, t)
        tcols = [c for c in info if (TIME_COL.search(c) or c in EXTRA_TIME_COLS) and not _col_exempt(t, c)]
        if not tcols:
            continue
        upd = [c for c in tcols if c in UPDATE_COLS]
        anchors = [c for c in tcols if c not in UPDATE_COLS]
        guard, p = _guard(t), {"day": day}
        any_upd = " OR ".join(_late(c) for c in upd)
        if anchors:
            n = conn.execute(f'DELETE FROM "{t}" WHERE ({" OR ".join(_late(c) for c in anchors)}){guard}', p).rowcount
            if upd:  # 这一行没有任何“哪天发生”的时间，只有更新时间晚于演示日：按默认规则删
                no_anchor = " AND ".join(f'"{c}" IS NULL' for c in anchors)
                n += conn.execute(f'DELETE FROM "{t}" WHERE ({no_anchor}) AND ({any_upd}){guard}', p).rowcount
                for c in upd:
                    _reset_update_col(conn, t, c, info[c], anchors, day, stats)
        else:
            n = conn.execute(f'DELETE FROM "{t}" WHERE ({any_upd}){guard}', p).rowcount
        _add(stats, t, n)


def _clean_orphans(conn, stats: dict) -> None:
    """删掉父记录后，按外键和 project_id 清掉悬空引用：级联或不可空的删行，可空的置空。反复做到不再变化。"""
    tables = set(_tables(conn))
    for _ in range(20):
        changed = 0
        for t in sorted(tables):
            info, guard = _colinfo(conn, t), _guard(t)
            drop, null = [], []          # 先删行、再置空，免得把随后要删的行也算成“置空”
            fks = conn.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()
            declared = {fk[3] for fk in fks}
            for fk in fks:
                parent, col, pcol, on_delete = fk[2], fk[3], fk[4], (fk[6] or "").upper()
                if parent not in tables or col not in info:
                    continue
                ref = f'"{pcol}"' if pcol else "rowid"
                miss = f'"{col}" IS NOT NULL AND "{col}" NOT IN (SELECT {ref} FROM "{parent}" WHERE {ref} IS NOT NULL)'
                (drop if on_delete == "CASCADE" or info[col] or (col == "project_id" and t != "users") else null).append((col, miss))
            if "project_id" in info and "project_id" not in declared and t != "projects" and "projects" in tables:
                drop.append(("project_id", "project_id IS NOT NULL AND project_id NOT IN (SELECT id FROM projects)"))
            if "ai_run_id" in info and "ai_run_id" not in declared and t != "ai_runs" and "ai_runs" in tables:
                null.append(("ai_run_id", "ai_run_id IS NOT NULL AND ai_run_id NOT IN (SELECT id FROM ai_runs)"))
            for _col, miss in drop:
                n = max(conn.execute(f'DELETE FROM "{t}" WHERE {miss}{guard}').rowcount, 0)
                _add(stats, t, n)
                changed += n
            for col, miss in null:
                n = max(conn.execute(f'UPDATE "{t}" SET "{col}"=NULL WHERE {miss}{guard}').rowcount, 0)
                _add(stats, f"~{t}.{col}", n)
                changed += n
        if not changed:
            return


def _dedupe_notifications(conn, stats: dict) -> None:
    """F-19：同一收件人、同标题、同跳转、同一小时内的重复消息只留最后一条；催办记录改指向留下的那条。"""
    if "notifications" not in _tables(conn):
        return
    groups: dict[tuple, list[int]] = {}
    for r in conn.execute("SELECT id, project_id, user_id, title, IFNULL(link_json,''), IFNULL(alert_id,0), substr(created_at,1,13) "
                          "FROM notifications ORDER BY id").fetchall():
        groups.setdefault(tuple(r[1:]), []).append(r[0])
    nudge = "notif_id" in _colinfo(conn, "nudge_log") if "nudge_log" in _tables(conn) else False
    for ids in groups.values():
        if len(ids) < 2:
            continue
        keep, drop = ids[-1], ids[:-1]
        marks = ",".join("?" * len(drop))
        conn.execute(f"DELETE FROM notifications WHERE id IN ({marks})", drop)
        if nudge:
            conn.execute(f"UPDATE nudge_log SET notif_id=? WHERE notif_id IN ({marks})", (keep, *drop))
        _add(stats, "notifications", len(drop))


def reset_key_pool(conn) -> int:
    """清空 Key 池登记（ai_keys 只有指纹与尾号，ai_usage 是各 Key 用量，seeded_fps 记着哪些 .env Key 已导入过）。"""
    tables, n = set(_tables(conn)), 0
    for t, where in (("ai_keys", ""), ("ai_usage", ""), ("ai_settings", " WHERE k='seeded_fps'")):
        if t in tables:
            n += max(conn.execute(f'DELETE FROM "{t}"{where}').rowcount, 0)
    return n


def clean(conn, day: str) -> dict:
    stats: dict[str, int] = {}
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        stats["key_pool"] = reset_key_pool(conn)
        _clear_late_ai_output(conn, day, stats)
        _clean_times(conn, day, stats)
        _clean_orphans(conn, stats)
        _dedupe_notifications(conn, stats)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("PRAGMA foreign_keys=ON")
    return stats


def verify(conn, day: str) -> list[str]:
    """AC-7.3：每张表的每个时间列（按列名识别），除豁免清单外，最大时间不晚于演示日。返回不满足的说明。"""
    bad = []
    for t in _tables(conn):
        for c in _colinfo(conn, t):
            if not TIME_COL.search(c) or _col_exempt(t, c):
                continue
            n = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE {_late(c)}{_guard(t)}', {"day": day}).fetchone()[0]
            if n:
                bad.append(f"{t}.{c}：{n} 行晚于 {day}")
    return bad


# ----------------------------------------------------------------------------- 制作副本

def _inside(a: Path, b: Path) -> bool:
    return a == b or b in a.parents


def _file_refs(conn) -> set[str]:
    """库里登记的照片/附件文件（相对 media/ 的路径）。"""
    out, tables = set(), set(_tables(conn))
    for t, cols in FILE_REFS:
        if t in tables:
            have = [c for c in cols if c in _colinfo(conn, t)]
            for row in conn.execute(f'SELECT {", ".join(have)} FROM "{t}"') if have else ():
                out.update(str(v) for v in row if v)
    return out


def _drop_files(media: Path, rels: set[str]) -> int:
    """副本里删掉只被已清理记录引用的文件（演示日之后上传的照片），再删空目录。"""
    root, n = media.resolve(), 0
    for rel in rels:
        p = (root / rel).resolve()
        if root in p.parents and p.is_file():
            p.unlink()
            n += 1
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    return n


def make_copy(src_dir, dst_dir, day: str = DEFAULT_DAY, force: bool = False) -> dict:
    src_dir, dst = Path(src_dir).resolve(), Path(dst_dir).resolve()
    day = date.fromisoformat(str(day)).isoformat()
    if _inside(dst, src_dir) or _inside(src_dir, dst):
        raise CheckError("演示副本目录不能与业务数据目录重叠")
    src = open_readonly(src_dir / DB_NAME)
    created = False
    try:
        sim_check(src)
        if dst.exists() and any(dst.iterdir()):
            if not force:
                raise CheckError(f"{dst} 已有内容；要重新制作请加 --force")
            if not (dst / MARKER).is_file():
                raise CheckError(f"{dst} 不是演示副本（没有 {MARKER}），不覆盖")
            shutil.rmtree(dst)
        dst.mkdir(parents=True, exist_ok=True)
        created = True
        (dst / ".gitignore").write_text("*\n", encoding="utf-8")  # 副本含照片：整个目录不进 Git
        out = sqlite3.connect(dst / DB_NAME)
        try:
            src.backup(out)
        finally:
            out.close()
    except BaseException:
        if created:
            shutil.rmtree(dst, ignore_errors=True)
        raise
    finally:
        src.close()
    try:
        out = sqlite3.connect(dst / DB_NAME, isolation_level=None)
        try:
            refs = _file_refs(out)
            removed = clean(out, day)
            dropped_refs = refs - _file_refs(out)
            if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("副本完整性检查未通过")
            bad = verify(out, day)
            if bad:
                raise RuntimeError("清理后仍有晚于演示日的记录：" + "；".join(bad))
            fk = len(out.execute("PRAGMA foreign_key_check").fetchall())
            projects = sim_check(out)
        finally:
            out.close()
        media_files, media_dropped = 0, 0
        if (src_dir / "media").is_dir():
            shutil.copytree(src_dir / "media", dst / "media")
            media_dropped = _drop_files(dst / "media", dropped_refs)
            media_files = sum(1 for p in (dst / "media").rglob("*") if p.is_file())
        summary = {"day": day, "source": str(src_dir / DB_NAME), "made_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "projects": [code for _, code, _ in projects], "media_files": media_files, "media_dropped": media_dropped, "fk_violations": fk,
                   "key_pool_cleared": removed.pop("key_pool", 0), "removed": {k: v for k, v in removed.items() if not k.startswith("~")},
                   "reset": {k[1:]: v for k, v in removed.items() if k.startswith("~")}, "note": NOTE}
        (dst / MARKER).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        return summary
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.demo_copy", description="把业务库只读复制成演示副本，清理晚于演示日的记录")
    ap.add_argument("--src-dir", default=str(ROOT / "data"), help="业务数据目录（只读），默认 data/")
    ap.add_argument("--dst", default=str(ROOT / "data_demo"), help="演示副本目录，默认 data_demo/")
    ap.add_argument("--day", default=DEFAULT_DAY, help=f"演示日，默认 {DEFAULT_DAY}")
    ap.add_argument("--force", action="store_true", help="覆盖已有的演示副本")
    a = ap.parse_args(argv)
    try:
        s = make_copy(a.src_dir, a.dst, a.day, a.force)
    except CheckError as exc:
        print(f"未制作演示副本：{exc}", file=sys.stderr)
        return 2
    print(f"演示副本已制作：{Path(a.dst).resolve()}（演示日 {s['day']}，项目 {'、'.join(s['projects'])}）")
    print(f"源库只读复制，未改动：{s['source']}")
    print(f"{NOTE} 照片 {s['media_files']} 个文件（演示日之后上传、已随记录清理的 {s['media_dropped']} 个不带）。")
    print("已清理晚于演示日的记录：" + ("，".join(f"{k} {v}" for k, v in sorted(s["removed"].items())) or "无"))
    if s["reset"]:
        print("只是之后被改过的记录已退回：" + "，".join(f"{k} {v}" for k, v in sorted(s["reset"].items())))
    print(f"Key 池登记已清空 {s['key_pool_cleared']} 条（真实 AI 模式启动时从仓库 .env 导入）。自检：各表时间不晚于 {s['day']}。")
    print("启动：python -m tools.demo_serve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
