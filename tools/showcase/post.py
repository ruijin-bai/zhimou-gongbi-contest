"""收尾清理（演示副本专用，可重复运行）：

- 复原：fix 在按业务时间补交周确认时，进度预警引擎按“当时”重跑，用到了截止日之后的数据（BR110 日报风险预警的说明与 3 条通知），
  从补数前的备份复原这两条预警，删掉那几条通知；
- 系统离线结果里带的“（离线模拟）”前缀去掉（这些文字已由仿真撰写接管，ai_runs 记 provider=sim）；
- 截止日之后的更新时间、预警检测时间压回 10-04；
- 虚构小项目的 P6 文件名与审计里残留的旧编码改成新编码；
- 演示副本不留 AI Key（ai_keys 表清空；文件 ai_keys.json 另行删除）。
用法：python -m tools.showcase.post --data-dir data_showcase [--backup data_showcase/zhimou.before-fill.bak]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
C = "2026-10-04T23:59:59"


def run(conn, backup: Path | None) -> dict:
    st = {}

    def x(label, sql, *a):
        n = conn.execute(sql, a).rowcount
        if n:
            st[label] = st.get(label, 0) + n

    if backup and backup.exists():
        sys.path.insert(0, str(ROOT))
        from tools.showcase.rename import rename_text
        o = sqlite3.connect(backup)
        cols = [r[1] for r in o.execute("PRAGMA table_info(alerts)")]
        for aid in (7, 15):
            r = o.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
            cur = conn.execute("SELECT explanation_json FROM alerts WHERE id=?", (aid,)).fetchone()
            if r and cur and ("2026-10-05" in (cur[0] or "") or "L416" in (cur[0] or "")):
                vals = [rename_text(v) for v in r]
                x("alerts（复原）", "UPDATE alerts SET " + ",".join(f"{k}=?" for k in cols[1:]) + " WHERE id=?", *vals[1:], aid)
        o.close()
    x("notifications（按当时重跑预警产生的通知）", "DELETE FROM notifications WHERE created_at='2026-09-28T15:10:00+01:00' AND (title LIKE '【红色预警】立交桥桩基施工%' OR title LIKE '进度提醒：《立交桥桩基施工》%')")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall():
            if r[2] and r[2].upper() not in ("TEXT", ""):
                continue
            x("去掉“（离线模拟）”前缀", f'UPDATE "{t}" SET "{r[1]}"=replace("{r[1]}", "（离线模拟）", "") WHERE instr("{r[1]}", "（离线模拟）")>0')
    x("alerts（说明里的日期）", "UPDATE alerts SET explanation_json=replace(explanation_json,'2026-10-05','2026-10-04'), detail_json=replace(detail_json,'2026-10-05','2026-10-04') WHERE id IN (7,15)")
    x("alerts.last_detected", "UPDATE alerts SET last_detected='2026-10-04T07:06:00+01:00' WHERE last_detected>?", C)
    x("alerts.as_of", "UPDATE alerts SET as_of='2026-10-04' WHERE as_of>'2026-10-04'")
    x("findings.updated_at", "UPDATE findings SET updated_at='2026-10-04T18:00:00+01:00' WHERE updated_at>?", C)
    for code in ("SIM-LINK", "SIM-BRIDGE"):
        p = conn.execute("SELECT id FROM projects WHERE code=?", (code,)).fetchone()
        if p:
            for col in ("file_name", "proj_name"):
                x("小项目 P6 文件名", f"UPDATE p6_imports SET {col}=replace({col}, 'LEKKI-SIM', ?) WHERE project_id=? AND instr({col}, 'LEKKI')>0", code, p[0])
    x("audit（旧编码）", "UPDATE audit SET detail=replace(detail, 'LEKKI-SIM', 'SIM') WHERE instr(detail, 'LEKKI')>0")
    if "ai_keys" in tables:
        x("ai_keys（演示副本不留 Key）", "DELETE FROM ai_keys")
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data_showcase"))
    ap.add_argument("--backup", default="")
    a = ap.parse_args()
    data_dir = Path(a.data_dir).resolve()
    if data_dir == (ROOT / "data").resolve() or not (data_dir / "DEMO_COPY.json").exists():
        sys.exit("只能对演示副本运行")
    conn = sqlite3.connect(data_dir / "zhimou.db", isolation_level=None)
    conn.execute("BEGIN")
    st = run(conn, Path(a.backup) if a.backup else data_dir / "zhimou.before-fill.bak")
    conn.execute("COMMIT")
    sys.path.insert(0, str(ROOT))
    from tools.showcase.rename import residue
    print(st)
    print("残留字样：", residue(conn) or "无")
    conn.close()


if __name__ == "__main__":
    main()
