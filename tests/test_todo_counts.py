"""I1/I2（验收 2026-10-11）：首页候选数与台账同源（“另有 N 项”折叠的条数计入）；分包安全员首页三格与列表同一单位、带筛选。"""
from pathlib import Path

from app.services import todo

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_cap_more_item_carries_folded_count():
    rows = [todo.item("candidate", i, "safety", f"隐患{i}", "", "候选") for i in range(todo.PER_SOURCE + 3)]
    out = todo._cap(rows, "candidate", len(rows), "待确认隐患", "safety", "safety")
    listed = [i for i in out if i["source"] == "candidate"]
    more = [i for i in out if i["source"] == "candidate_more"]
    assert len(more) == 1 and more[0]["more_n"] == 3
    assert len(listed) + more[0]["more_n"] == len(rows)


def test_front_counts_use_same_unit_and_filters():
    t = (STATIC / "todo.js").read_text(encoding="utf8")
    assert "function todoCounts(list)" in t and 'this.f === "cand"' in t and 'this.f === "overdue"' in t
    r = (STATIC / "roles.js").read_text(encoding="utf8")
    assert "jump('overdue')" in r and "jump('cand')" in r and "todoCounts(t.items || [])" in r
    e = (STATIC / "x_enghome.js").read_text(encoding="utf8")
    assert "todoCounts(this.d.items).candItems" in e
