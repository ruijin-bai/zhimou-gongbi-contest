"""I7/I8（验收 2026-10-11）：私信、公告通知有去处；领导看公告全文；消息页未读与铃铛同源。"""
from conftest import login
from test_roles import _team
from test_social import enable_social


def test_dm_and_notice_notifications_have_targets(env):
    _team(env)
    enable_social(env)
    u = env["users"]
    sa, t1, pm = login("sa_a"), login("tech1"), login("pm")
    sa.post("/api/social/dm/send", json={"to": u["tech1"], "text": "下午过去看"})
    dm = next(n for n in t1.get("/api/notifications").json() if "给你发来私信" in n["title"])
    assert dm["link"] == {"route": "circle", "params": {"dm": str(u["sa_a"])}}
    long_body = "停电检修安排：" + "明天 8:00-12:00 全场停电，" * 10
    assert pm.post("/api/circle/notices", json={"title": "停电", "body": long_body, "audience": "all"}).status_code == 200
    tn = next(n for n in t1.get("/api/notifications").json() if n["title"] == "公告：停电")
    assert tn["link"]["route"] == "circle" and tn["body"] == long_body   # 存全文，不再截到 80 字
    n = t1.get("/api/notifications/unread").json()["unread"]
    assert n == sum(1 for x in t1.get("/api/notifications").json() if not x["read_at"])


def test_unread_endpoint_counts_beyond_first_page(env):
    from app import db as dbmod
    conn = dbmod.connect()
    for i in range(130):
        dbmod.insert(conn, "notifications", {"project_id": env["project_id"], "user_id": env["users"]["tech1"], "alert_id": None,
                                             "title": f"提醒{i}", "body": "", "channels_json": "{}", "created_at": dbmod.now_iso()})
    conn.close()
    t1 = login("tech1")
    assert len(t1.get("/api/notifications").json()) == 100
    assert t1.get("/api/notifications/unread").json()["unread"] == 130
