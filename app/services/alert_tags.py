"""预警标记：给已有预警加标记（如“回放产生”），不改动预警本身的判定内容。

replay＝由历史/反推 P6 计划批量导入时，后台预警判定按回放数据产生的预警（如由产值反推的多期进度）。
这些预警页面照常可见（带“回放”标识），但不计入预警人工反馈命中率、应用成效与智能体采纳统计。
"""
from __future__ import annotations

from ..db import all_, audit, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_tags (
    alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (alert_id, tag)
);
"""

REPLAY = "replay"
# 拼在 alerts 查询的 WHERE 后：排除回放产生的预警
NOT_REPLAY = " AND id NOT IN (SELECT alert_id FROM alert_tags WHERE tag='replay')"


def tag(conn, alert_ids: list[int], tag_: str, note: str = "", user_id: int | None = None) -> int:
    with conn:
        n = sum(conn.execute("INSERT OR IGNORE INTO alert_tags (alert_id, tag, note, created_at) VALUES (?,?,?,?)",
                             (int(i), tag_, note[:200], now_iso())).rowcount for i in alert_ids)
        audit(conn, user_id, f"alert.tag.{tag_}", "alerts", f"{n} 条：{note[:120]}")
    return n


def replay_ids(conn, ids: list[int] | None = None) -> set[int]:
    if ids is not None and not ids:
        return set()
    q = "SELECT alert_id FROM alert_tags WHERE tag='replay'"
    if ids is not None:
        q += f" AND alert_id IN ({','.join('?' * len(ids))})"
    return {r["alert_id"] for r in all_(conn, q, list(ids or []))}


def replay_count(conn, project_id: int) -> int:
    return all_(conn, "SELECT COUNT(*) n FROM alerts WHERE project_id=? AND id IN (SELECT alert_id FROM alert_tags WHERE tag='replay')",
                (project_id,))[0]["n"]
