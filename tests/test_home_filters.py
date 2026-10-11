"""I4/I5（验收 2026-10-11）：首页指标点进去看到对应数字——今日随手记按今天筛、质量未闭环不含质控提醒、领导质量/安全卡可点。"""
from datetime import date, timedelta
from pathlib import Path

from app import db as dbmod

from conftest import login

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_feed_notes_today_view(env):
    t1, t2 = login("tech1"), login("tech2")
    t1.post("/api/notes", json={"text": "3号墩承台浇完"})
    t2.post("/api/notes", json={"text": "路基碾压"})
    conn = dbmod.connect()   # 一条昨天的随手记不应出现
    dbmod.insert(conn, "notes", {"project_id": env["project_id"], "author_id": env["users"]["tech1"], "text": "昨天的",
                                 "note_date": (date.today() - timedelta(days=1)).isoformat(), "created_at": dbmod.now_iso()})
    conn.close()
    d = login("pm").get("/api/feed?view=notes_today").json()
    assert d["total"] == 2 and len(d["posts"]) == 2
    assert "今日随手记 2 条" in d["scope"]


def test_front_cards_carry_filters():
    eh = (STATIC / "x_enghome.js").read_text(encoding="utf8")
    assert 'route: "circle?view=notes_today"' in eh and 'route: "quality?only=open"' in eh
    assert 'open: { name: "未闭环（不含质控提醒）"' in (STATIC / "mgr.js").read_text(encoding="utf8")
    ck = (STATIC / "cockpit.js").read_text(encoding="utf8")
    assert 'fn: () => this.reveal("q")' in ck and 'fn: () => this.reveal("s")' in ck


def test_circle_agent_review_is_click_triggered():
    """RJ 10-11：现场圈打开时不再自动生成 Agent 点评，人点“AI 点评”才调用 /api/circle/review。"""
    c = (STATIC / "circle.js").read_text(encoding="utf8")
    assert "afterLoad() { this.markNotices(); }" in c and '@click="runReview"' in c
    assert c.count("this.review(0") == 1 and "const n = await this.review(0, true)" in c
