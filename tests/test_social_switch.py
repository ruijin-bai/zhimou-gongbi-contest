"""社交层总开关：默认关闭时写操作被拒、读接口为空；点赞、评论（含回复与 @）不受影响；开启后恢复。"""
from conftest import login
from test_roles import _team
from test_social import enable_social


def _post(env):
    _team(env)
    t1, sa = login("tech1"), login("sa_a")
    n1 = t1.post("/api/notes", json={"text": "3号墩承台钢筋绑扎完成六成"}).json()
    return t1, sa, f"note:{n1['id']}"


def test_social_off_by_default_rejects_writes(env):
    t1, sa, key = _post(env)
    u = env["users"]
    cfg = login("tech1").get("/api/me").json()["project"]["config"]
    assert not cfg.get("social_enabled")
    for r in (
        sa.post("/api/social/fav", json={"key": key}),
        sa.post("/api/social/follow", json={"user_id": u["tech1"]}),
        sa.post("/api/social/repost", json={"key": key, "text": "转"}),
        sa.post("/api/social/dm/send", json={"to": u["tech1"], "text": "你好"}),
        sa.post("/api/social/highlights", json={"key": key, "para": "P1", "quote": "x"}),
        sa.post("/api/social/article/summary", json={"key": key}),
        sa.post("/api/social/comments/1/feature", json={"featured": True}),
    ):
        assert r.status_code == 403 and "功能未开启" in r.json()["detail"], r.text
    # 读接口：返回空，不报错
    assert sa.get("/api/social/summary").json() == {"dm_unread": 0}
    assert sa.get("/api/social/dm/threads").json() == {"threads": []}
    assert sa.get("/api/social/highlights/mine").json() == {"items": []}
    assert sa.get(f"/api/social/users/{u['tech1']}/follows?type=followers").json() == []
    # 主线不受影响：点赞、评论（含回复与 @）、@同事候选
    assert sa.post("/api/feed/like", json={"key": key}).status_code == 200
    c = sa.post("/api/feed/comments", json={"key": key, "text": "收到 @张工"})
    assert c.status_code == 200, c.text
    assert sa.post("/api/feed/comments", json={"key": key, "text": "回复", "reply_to": c.json()["id"]}).status_code == 200
    assert sa.get("/api/social/people").status_code == 200


def test_social_on_allows_writes(env):
    t1, sa, key = _post(env)
    u = env["users"]
    assert sa.post("/api/social/fav", json={"key": key}).status_code == 403
    enable_social(env)
    assert sa.post("/api/social/fav", json={"key": key}).json() == {"key": key, "me": True}
    assert sa.post("/api/social/follow", json={"user_id": u["tech1"]}).json()["i_follow"] is True
    assert sa.post("/api/social/dm/send", json={"to": u["tech1"], "text": "你好"}).status_code == 200
    assert t1.get("/api/social/summary").json()["dm_unread"] == 1
    # 再关闭：写被拒，数据保留，未读数不再提示
    enable_social(env, on=False)
    assert sa.post("/api/social/fav", json={"key": key}).status_code == 403
    assert t1.get("/api/social/summary").json() == {"dm_unread": 0}
    enable_social(env)
    assert t1.get("/api/social/summary").json()["dm_unread"] == 1
