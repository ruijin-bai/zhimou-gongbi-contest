"""演示日截止（RJ 2026-10-11：演示日为 10-04）：把演示副本的时间线截到 2026-10-04 当天结束。

- 10-05 及以后新建的行删除：P6 期次（含作业/逻辑/日历）、例会、随手记、日志、照片、单据、浇筑、隐患、预测、简报、通知、催办、AI 调用与审计记录等；
- 10-04 以前建、10-05 才发生的状态变化复原：日志审核 → 回到“已提交待审核”，隐患闭环 / 确认 / 驳回 → 回到处理前，相应审核留痕删除；
- 9-28 那周的周产值实际值清空（周一才上报），只留计划；10-05 那周的产值计划行删除；
- DEMO_COPY.json 的演示日改为 10-04。
可重复运行。用法：python -m tools.showcase.cutoff --data-dir data_showcase
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
D = "2026-10-04"
C = "2026-10-04T23:59:59"


def run(conn) -> dict:
    st: dict[str, int] = {}

    def x(label, sql, *a):
        n = conn.execute(sql, a).rowcount
        if n:
            st[label] = st.get(label, 0) + n

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # P6 期次
    imps = [r[0] for r in conn.execute("SELECT id FROM p6_imports WHERE data_date>?", (D,))]
    for t in ("activities", "relations", "p6_act_ext", "p6_calendars"):
        if t in tables and "import_id" in {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}:
            for i in imps:
                x(f"{t}（10-05 期次）", f"DELETE FROM {t} WHERE import_id=?", i)
    for i in imps:
        x("p6_imports", "DELETE FROM p6_imports WHERE id=?", i)
    last = conn.execute("SELECT id FROM p6_imports ORDER BY data_date DESC, id DESC LIMIT 1").fetchone()
    if last:
        conn.execute("UPDATE p6_imports SET is_current=CASE WHEN id=? THEN 1 ELSE 0 END", (last[0],))
    # 例会
    mids = [r[0] for r in conn.execute("SELECT id FROM meetings WHERE meeting_date>? OR created_at>?", (D, C))]
    for m in mids:
        x("meeting_items", "DELETE FROM meeting_items WHERE meeting_id=?", m)
        x("meetings", "DELETE FROM meetings WHERE id=?", m)
    x("meeting_items（10-05 完成复原）", "UPDATE meeting_items SET status='open', done_at=NULL, done_by=NULL WHERE done_at>?", C)
    # 日志
    lids = [r[0] for r in conn.execute("SELECT id FROM logs WHERE log_date>?", (D,))]
    for lid in lids:
        x("log_progress", "DELETE FROM log_progress WHERE log_id=?", lid)
        x("review_fields", "DELETE FROM review_fields WHERE ref_type='log' AND ref_id=?", lid)
        x("media（日志解绑）", "UPDATE media SET log_id=NULL WHERE log_id=?", lid)
        x("logs", "DELETE FROM logs WHERE id=?", lid)
    x("review_fields（10-05 审核留痕）", "DELETE FROM review_fields WHERE decided_at>?", C)
    x("logs（10-05 审核复原为待审核）", "UPDATE logs SET status='submitted', reviewed_by=NULL, reviewed_at=NULL, review_note='' WHERE reviewed_at>? AND status IN ('approved','returned')", C)
    x("notes", "DELETE FROM notes WHERE note_date>? OR created_at>?", D, C)
    # 单据、浇筑、照片
    for t, col in (("tickets", "created_at"), ("pours", "created_at")):
        x(t, f"DELETE FROM {t} WHERE {col}>?", C)
    x("media", "DELETE FROM media WHERE uploaded_at>? AND id NOT IN (SELECT IFNULL(rectify_media_id,0) FROM findings)", C)
    # 隐患
    x("findings（10-05 新建）", "DELETE FROM findings WHERE created_at>?", C)
    x("findings（10-05 闭环复原）", """UPDATE findings SET status=CASE WHEN rectify_media_id IS NULL THEN 'open' ELSE 'rectifying' END, resolved_at=NULL,
                                       feedback_at=NULL, feedback_by=NULL, feedback_note='' WHERE resolved_at>?""", C)
    x("findings（10-05 驳回复原为候选）", "UPDATE findings SET status='open', feedback_at=NULL, feedback_by=NULL, feedback_note='' WHERE status='dismissed' AND feedback_at>?", C)
    x("findings（10-05 确认复原为候选）", "UPDATE findings SET confirmed_at=NULL, confirmed_by=NULL WHERE confirmed_at>?", C)
    # 其他按创建时间删除
    for t, col in (("forecasts", "created_at"), ("notifications", "created_at"), ("nudge_log", "sent_at"), ("ai_runs", "created_at"),
                   ("events", "created_at"), ("audit", "created_at"), ("briefs", "created_at"), ("note_classify", "created_at"),
                   ("feed_agent_notes", "created_at"), ("circle_picks", "created_at"), ("quick_submissions", "created_at")):
        if t in tables:
            x(t, f"DELETE FROM {t} WHERE {col}>?", C)
    x("ai_usage", "DELETE FROM ai_usage WHERE day>?", D)
    x("notifications（10-05 已读复原）", "UPDATE notifications SET read_at=NULL WHERE read_at>?", C)
    x("knowledge（催办时间戳）", "UPDATE knowledge SET value_json=replace(value_json, '2026-10-05T', '2026-10-04T'), updated_at=replace(updated_at, '2026-10-05T', '2026-10-04T') WHERE kind='nudge_state' AND updated_at>?", C)
    # 产值
    x("output_records（10-05 周）", "DELETE FROM output_records WHERE period_start>?", D)
    x("output_records（9-28 周实际值，周一才上报）", "UPDATE output_records SET actual=NULL WHERE period_end=? AND actual IS NOT NULL", D)
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data_showcase"))
    a = ap.parse_args()
    data_dir = Path(a.data_dir).resolve()
    if data_dir == (ROOT / "data").resolve() or not (data_dir / "DEMO_COPY.json").exists():
        sys.exit("只能对演示副本运行")
    conn = sqlite3.connect(data_dir / "zhimou.db", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    st = run(conn)
    conn.execute("COMMIT")
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    for k, v in st.items():
        print(f"{k}: {v}")
    print("外键问题：", bad[:10] or "无")
    mk = data_dir / "DEMO_COPY.json"
    d = json.loads(mk.read_text("utf-8"))
    d["day"] = D
    mk.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
