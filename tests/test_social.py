from conftest import jpeg, login
from test_roles import _add, _team


def enable_social(env, on=True):
    """社交层默认关闭；这些测试验证开启后的行为。"""
    r = login("admin").put("/api/project", json={"name": "示例大桥（合成测试）", "social_enabled": on})
    assert r.status_code == 200, r.text


def _keys(c, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return [p["key"] for p in c.get(f"/api/feed?{qs}").json()["posts"]]


def _setup(env):
    _team(env)
    enable_social(env)
    t1, t2, sa, eng = login("tech1"), login("tech2"), login("sa_a"), login("eng")
    n1 = t1.post("/api/notes", json={"text": "3号墩承台钢筋绑扎完成六成", "lat": 30.6, "lon": 114.3, "acc": 12, "place": "3号墩承台"}).json()
    n2 = t2.post("/api/notes", json={"text": "乙分包路基碾压"}).json()
    return t1, t2, sa, eng, f"note:{n1['id']}", f"note:{n2['id']}"


def test_fav_and_my_view(env):
    t1, t2, sa, eng, k1, k2 = _setup(env)
    assert sa.post("/api/social/fav", json={"key": k1}).json() == {"key": k1, "me": True}
    assert next(p for p in sa.get("/api/feed").json()["posts"] if p["key"] == k1)["fav"]["me"] is True
    assert _keys(sa, view="fav") == [k1]
    # 现场圈全项目可见：其他分包的动态也能收藏；不存在的动态不能收藏；再点一次取消
    assert sa.post("/api/social/fav", json={"key": k2}).status_code == 200 and sa.post("/api/social/fav", json={"key": k2}).json()["me"] is False
    assert sa.post("/api/social/fav", json={"key": "note:9999"}).status_code == 400
    assert sa.post("/api/social/fav", json={"key": k1}).json()["me"] is False and _keys(sa, view="fav") == []
    # 领导只读也能收藏；管理员不参与
    _add(env, "boss", "leader", "领导")
    assert login("boss").post("/api/social/fav", json={"key": k1}).status_code == 200
    assert login("admin").post("/api/social/fav", json={"key": k1}).status_code == 403


def test_follow_profile_and_follow_view(env):
    t1, t2, sa, eng, k1, k2 = _setup(env)
    u = env["users"]
    # 现场圈全项目可见：跨分包也能看主页、关注（私信仍只到本单位）；不能关注自己
    assert sa.post("/api/social/follow", json={"user_id": u["tech1"]}).json()["i_follow"] is True
    assert sa.post("/api/social/follow", json={"user_id": u["sa_a"]}).status_code == 400
    p2 = sa.get(f"/api/social/users/{u['tech2']}").json()
    assert p2["posts_visible"] is True and p2["can_dm"] is False
    assert any("关注了你" in n["title"] for n in t1.get("/api/notifications").json())
    prof = eng.get(f"/api/social/users/{u['tech1']}").json()
    assert prof["followers"] == 1 and prof["places"] == [{"place": "3号墩承台", "n": 1}] and prof["can_follow"] and prof["week"]["start"]
    # 关注视图只含关注的人；主页按作者看动态
    assert _keys(sa, view="follow") == [k1]
    assert _keys(eng, view="follow") == []
    eng.post("/api/social/follow", json={"user_id": u["tech2"]})
    assert _keys(eng, view="follow") == [k2]
    assert _keys(eng, author=u["tech1"]) == [k1] and _keys(sa, author=u["tech2"]) == [k2]
    # 关注/粉丝名单只含可见同事
    assert [p["name"] for p in sa.get(f"/api/social/users/{u['tech1']}/follows?type=followers").json()] == ["安全员甲"]
    # 取消关注
    assert sa.post("/api/social/follow", json={"user_id": u["tech1"], "on": False}).json()["i_follow"] is False
    # 领导、管理员不能关注
    _add(env, "boss", "leader", "领导")
    assert login("boss").post("/api/social/follow", json={"user_id": u["tech1"]}).status_code == 403
    assert login("admin").get(f"/api/social/users/{u['tech1']}").status_code == 403


def test_repost_keeps_scope(env):
    t1, t2, sa, eng, k1, k2 = _setup(env)
    r = sa.post("/api/social/repost", json={"key": k1, "text": "大家注意这个部位 @张工"}).json()
    rp = next(p for p in sa.get("/api/feed").json()["posts"] if p["key"] == r["key"])
    assert rp["tag"] == "转发" and rp["text"].startswith("大家注意") and rp["orig"]["hidden"] is False and rp["orig"]["text"].startswith("3号墩")
    assert next(p for p in sa.get("/api/feed").json()["posts"] if p["key"] == k1)["reposts"] == 1
    titles = [n["title"] for n in t1.get("/api/notifications").json()]
    assert any("转发了你的现场动态" in t for t in titles) and True
    # 现场圈全项目可见：乙分包也看到甲的转发、也能转发甲的动态；不能转发转发、不能转发不存在的动态
    assert r["key"] in _keys(t2)
    assert t2.post("/api/social/repost", json={"key": k1}).status_code == 200
    assert sa.post("/api/social/repost", json={"key": r["key"]}).status_code == 400
    assert sa.post("/api/social/repost", json={"key": "note:9999"}).status_code == 400
    r2 = eng.post("/api/social/repost", json={"key": k2, "text": "路基碾压关注"}).json()
    assert next(p for p in sa.get("/api/feed").json()["posts"] if p["key"] == r2["key"])["orig"]["hidden"] is False
    # 转发的动态可点赞评论
    assert sa.post("/api/feed/comments", json={"key": r["key"], "text": "收到"}).status_code == 200


def test_comment_reply_quote_mention_feature(env):
    t1, t2, sa, eng, k1, k2 = _setup(env)
    c1 = sa.post("/api/feed/comments", json={"key": k1, "text": "这一句要复核", "quote": "钢筋绑扎完成六成"}).json()
    assert c1["quote"] == "钢筋绑扎完成六成"
    c2 = t1.post("/api/feed/comments", json={"key": k1, "text": "好的 @工程主管 麻烦看下", "reply_to": c1["id"]}).json()
    assert c2["reply_to"] == {"id": c1["id"], "name": "安全员甲"}
    assert any("回复了你的评论" in n["title"] for n in sa.get("/api/notifications").json())
    assert any("提到了你" in n["title"] for n in eng.get("/api/notifications").json())
    assert t1.post("/api/feed/comments", json={"key": k1, "text": "x", "reply_to": 9999}).status_code == 400
    # 精选：港湾管理岗可设置，精选排最前；分包不可
    assert t1.post(f"/api/social/comments/{c2['id']}/feature", json={"featured": True}).status_code == 403
    assert login("pm").post(f"/api/social/comments/{c2['id']}/feature", json={"featured": True}).status_code == 200
    post = next(p for p in t1.get("/api/feed").json()["posts"] if p["key"] == k1)
    assert post["comments"][0]["id"] == c2["id"] and post["comments"][0]["featured"] and post["comments"][1]["quote"] == "钢筋绑扎完成六成"


def test_article_summary_and_highlights(env):
    t1, t2, sa, eng, k1, k2 = _setup(env)
    pm = login("pm")
    n = pm.post("/api/circle/notices", json={"title": "明日停电检修", "body": "时间：明天 8:00-12:00\n范围：全场\n要求：关闭配电箱", "audience": "all"}).json()
    key = n["key"]
    art = sa.get(f"/api/social/article?key={key}").json()
    assert art["title"] == "明日停电检修" and len(art["paragraphs"]) == 3 and art["reads"] >= 1 and art["summary"] is None
    # 总结：只复述原文，引用段落；缓存
    r = sa.post("/api/social/article/summary", json={"key": key}).json()
    assert r["summary"]["source"] == "rule"  # 先立刻给出按原文摘录的总结，润色在后台
    st = sa.get(f"/api/social/article/summary?key={key}").json()  # 测试客户端会等后台任务结束
    assert st["pending"] is False
    sm = st["summary"]
    assert sm["one"] and sm["points"] and all(p["refs"] and p["refs"][0].startswith("P") for p in sm["points"]) and sm["source"] in ("ai", "rule")
    assert sa.get(f"/api/social/article?key={key}").json()["summary"]["one"] == sm["one"]
    # 划重点：默认私有；选中文字必须在该段内；公开后他人只见“N 人划过”
    h = sa.post("/api/social/highlights", json={"key": key, "para": "P1", "quote": "8:00-12:00", "note": "记得通知班组"}).json()
    assert h["public"] is False
    assert sa.post("/api/social/highlights", json={"key": key, "para": "P2", "quote": "8:00-12:00"}).status_code == 400
    assert t1.get(f"/api/social/article?key={key}").json()["highlights"] == {"mine": [], "others": []}
    sa.put(f"/api/social/highlights/{h['id']}", json={"public": True})
    hl = t1.get(f"/api/social/article?key={key}").json()["highlights"]
    assert hl["mine"] == [] and hl["others"] == [{"para": "P1", "quote": "8:00-12:00", "n": 1, "note": ""}]
    assert [x["quote"] for x in sa.get("/api/social/highlights/mine").json()["items"]] == ["8:00-12:00"]
    assert t1.delete(f"/api/social/highlights/{h['id']}").status_code == 400  # 只能删自己的
    assert sa.delete(f"/api/social/highlights/{h['id']}").status_code == 200
    # 领导只读：划重点只能私有
    _add(env, "boss", "leader", "领导")
    boss = login("boss")
    hb = boss.post("/api/social/highlights", json={"key": key, "para": "P2", "quote": "全场", "public": True}).json()
    assert hb["public"] is False
    # 不存在的动态
    assert sa.get("/api/social/article?key=note:9999").status_code == 400
    assert login("admin").get(f"/api/social/article?key={key}").status_code == 403


def test_article_for_submitted_log(env):
    from test_agents import _setup as agent_setup, _submit_today
    pm, tech = agent_setup()
    _submit_today(tech)
    post = next(p for p in pm.get("/api/feed").json()["posts"] if p["kind"] == "log")
    assert post["article"] is True
    art = pm.get(f"/api/social/article?key={post['key']}").json()
    assert art["paragraphs"] and all(p["id"].startswith("P") for p in art["paragraphs"])


def test_dm_private_and_unreadable_by_admin(env):
    t1, t2, sa, eng, k1, k2 = _setup(env)
    u = env["users"]
    # 引用动态发私信；对方未读 +1，通知里不含私信内容
    m = sa.post("/api/social/dm/send", json={"to": u["tech1"], "text": "这个部位下午我过去看", "ref_key": k1}).json()
    assert m["mine"] and m["ref"]["key"] == k1 and m["ref"]["text"].startswith("3号墩")
    assert t1.get("/api/social/summary").json()["dm_unread"] == 1
    notes = t1.get("/api/notifications").json()
    assert any("给你发来私信" in n["title"] for n in notes) and not any("下午我过去" in (n.get("body") or "") for n in notes)
    th = t1.get("/api/social/dm/threads").json()["threads"]
    assert th[0]["peer"]["name"] == "安全员甲" and th[0]["unread"] == 1 and th[0]["last"]["text"].startswith("这个部位")
    conv = t1.get(f"/api/social/dm/with/{u['sa_a']}").json()
    assert [x["text"] for x in conv["messages"]] == ["这个部位下午我过去看"] and conv["messages"][0]["mine"] is False
    assert t1.get("/api/social/summary").json()["dm_unread"] == 0
    r = t1.post("/api/social/dm/send", json={"to": u["sa_a"], "text": "好的"}).json()
    assert sa.get(f"/api/social/dm/with/{u['tech1']}?mark=0").json()["peer_read"] >= m["id"]
    # 第三方（含港湾、管理员）读不到：会话只由双方各自读取
    assert eng.get(f"/api/social/dm/with/{u['tech1']}").json()["messages"] == []
    assert eng.get("/api/social/dm/threads").json()["threads"] == []
    assert login("admin").get(f"/api/social/dm/with/{u['tech1']}").status_code == 403
    assert login("admin").get("/api/social/dm/threads").status_code == 403
    assert login("admin").get("/api/social/summary").status_code == 403
    # 跨分包不能私信；不能给自己；领导只读不能发
    assert t1.post("/api/social/dm/send", json={"to": u["tech2"], "text": "hi"}).status_code == 400
    assert t1.post("/api/social/dm/send", json={"to": u["tech1"], "text": "hi"}).status_code == 400
    _add(env, "boss", "leader", "领导")
    assert login("boss").post("/api/social/dm/send", json={"to": u["tech1"], "text": "hi"}).status_code == 403
    # 空消息、超长消息被拒；引用看不到的动态被拒
    assert t1.post("/api/social/dm/send", json={"to": u["sa_a"], "text": " "}).status_code == 400
    assert t1.post("/api/social/dm/send", json={"to": u["sa_a"], "text": "好" * 1201}).status_code == 422
    assert t1.post("/api/social/dm/send", json={"to": u["sa_a"], "text": "看", "ref_key": "note:9999"}).status_code == 400
    # 不提供删除与撤回
    assert t1.delete(f"/api/social/dm/with/{u['sa_a']}").status_code in (404, 405)
    assert t1.delete(f"/api/social/dm/messages/{r['id']}").status_code in (404, 405)


def test_dm_photo_only_for_participants(env):
    t1, t2, sa, eng, k1, k2 = _setup(env)
    u = env["users"]
    f = sa.post("/api/social/dm/photo", files=[("files", ("a.jpg", jpeg((30, 90, 150)), "image/jpeg"))]).json()[0]
    assert sa.get(f["thumb"]).status_code == 200                      # 自己能看
    assert t1.get(f["thumb"]).status_code == 404                      # 发出前对方也看不到
    sa.post("/api/social/dm/send", json={"to": u["tech1"], "text": "", "file_ids": [f["id"]]})
    assert t1.get(f["url"]).status_code == 200                        # 收件人能看
    assert eng.get(f["url"]).status_code == 404 and t2.get(f["url"]).status_code == 404
    assert login("admin").get(f["url"]).status_code == 403
    # 私信照片不进照片库：不出现在现场圈，不被他人的媒体接口读到
    assert not any(p["photos"] for p in eng.get("/api/feed").json()["posts"] if p["kind"] in ("snap", "rect"))
    # 只能发自己上传的照片
    assert t1.post("/api/social/dm/send", json={"to": u["sa_a"], "text": "", "file_ids": [f["id"]]}).status_code == 400


def test_same_place_view(env):
    t1, t2, sa, eng, k1, k2 = _setup(env)
    snap = t1.post("/api/media", data={"purpose": "safety", "geo_lat": "30.6005", "geo_lon": "114.3003", "geo_acc": "9", "place": ""},
                   files=[("files", ("a.jpg", jpeg((200, 40, 40)), "image/jpeg"))]).json()[0]
    far = t1.post("/api/notes", json={"text": "很远的地方", "lat": 31.5, "lon": 115.0}).json()
    # 按地点文字：只有 3 号墩承台那条随手记
    assert _keys(eng, place="3号墩承台") == [k1]
    # 按坐标（约 200 米内）：随手记与随手拍都在，很远的那条不在
    near = set(_keys(eng, lat=30.6, lon=114.3))
    assert k1 in near and f"snap:{snap['id']}" in near and f"note:{far['id']}" not in near
    # 同地点视图不混入日志、公告等；现场圈全项目可见，乙分包也看得到
    assert _keys(t2, place="3号墩承台") == [k1]
