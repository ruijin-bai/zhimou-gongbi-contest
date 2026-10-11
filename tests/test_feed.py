from conftest import confirm_hazards, jpeg, login
from test_roles import _add, _team


def _post_keys(c, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return [p["key"] for p in c.get(f"/api/feed?{qs}").json()["posts"]]


def test_feed_collects_posts_and_social(env):
    _team(env)
    t1, t2, sa, sb, eng = login("tech1"), login("tech2"), login("sa_a"), login("sa_b"), login("eng")
    photo = t1.post("/api/media", files=[("files", ("a.jpg", jpeg(), "image/jpeg"))]).json()[0]
    n1 = t1.post("/api/notes", json={"text": "3号墩承台浇完", "media_ids": [photo["id"]], "source": "voice"}).json()
    n2 = t2.post("/api/notes", json={"text": "乙分包路基碾压"}).json()
    t1.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))])

    # 现场圈全项目可见（RJ 10-09）：甲、乙分包与港湾都看到全部动态
    mine = t1.get("/api/feed").json()
    kinds = [p["kind"] for p in mine["posts"]]
    assert "snap" in kinds and "note" in kinds and mine["scope"] == "全项目"
    both = {f"note:{n1['id']}", f"note:{n2['id']}"}
    assert both <= set(_post_keys(t1)) and both <= set(_post_keys(t2)) and both <= set(_post_keys(sa)) and both <= set(_post_keys(sb))
    assert {f"note:{n1['id']}", f"note:{n2['id']}"} <= set(_post_keys(eng))
    snap = next(p for p in mine["posts"] if p["kind"] == "snap")
    assert snap["ai"] and snap["ai"][0]["title"] and "隐患" in snap["text"]
    note = next(p for p in mine["posts"] if p["key"] == f"note:{n1['id']}")
    assert note["tag"] == "口述记录" and len(note["photos"]) == 1 and note["author"]["mine"]
    assert _post_keys(t1, mine=1) == [k for k in _post_keys(t1) if k != f"note:{n2['id']}"]

    # 现场圈里的照片全项目可看；不在现场圈里的照片（如日志附图）其他单位仍不能看
    assert sa.get(note["photos"][0]["thumb"]).status_code == 200
    assert sb.get(note["photos"][0]["thumb"]).status_code == 200
    other = t1.post("/api/media", files=[("files", ("b.jpg", jpeg((10, 120, 10)), "image/jpeg"))]).json()[0]
    assert sb.get(f"/api/media/{other['id']}/thumb").status_code == 403

    # 点赞切换、评论、通知发布人
    key = f"note:{n1['id']}"
    assert sa.post("/api/feed/like", json={"key": key}).json() == {"n": 1, "me": True, "names": ["安全员甲"]}
    assert eng.post("/api/feed/like", json={"key": key}).json()["n"] == 2
    assert sa.post("/api/feed/like", json={"key": key}).json()["me"] is False
    c = eng.post("/api/feed/comments", json={"key": key, "text": "收到，注意养护"}).json()
    assert c["mine"] and c["role_name"] == "工程主管"
    post = next(p for p in t1.get("/api/feed").json()["posts"] if p["key"] == key)
    assert post["likes"]["n"] == 1 and post["comments"][0]["text"] == "收到，注意养护" and not post["comments"][0]["mine"]
    assert any("评论了你的现场动态" in n["title"] for n in t1.get("/api/notifications").json())

    # 看不到的动态不能点赞评论；空评论、超长评论被拒；只能删自己的评论
    assert sb.post("/api/feed/like", json={"key": key}).status_code == 200            # 现场圈全项目可见，其他单位也能点赞评论
    assert sb.post("/api/feed/like", json={"key": "note:9999"}).status_code == 400
    assert eng.post("/api/feed/comments", json={"key": key, "text": "  "}).status_code == 400
    assert eng.post("/api/feed/comments", json={"key": key, "text": "长" * 301}).status_code == 400
    assert eng.post("/api/feed/like", json={"key": "note:99999"}).status_code == 400
    assert t1.delete(f"/api/feed/comments/{c['id']}").status_code == 403
    assert eng.delete(f"/api/feed/comments/{c['id']}").status_code == 200


def test_feed_rectify_log_roles_and_paging(env):
    _team(env)
    t1, eng = login("tech1"), login("eng")
    t1.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))])
    confirm_hazards()
    fid = t1.get("/api/findings?agent=safety&status=active&mine=1").json()[0]["id"]
    rect = t1.post("/api/media", data={"purpose": "rectify"}, files=[("files", ("r.jpg", jpeg((20, 210, 20)), "image/jpeg"))]).json()[0]
    assert t1.post(f"/api/findings/{fid}/rectify", json={"media_id": rect["id"]}).status_code == 200
    rp = next(p for p in t1.get("/api/feed").json()["posts"] if p["kind"] == "rect")
    assert rp["compare"] and len(rp["photos"]) == 2 and rp["ai"][0]["id"] == fid and "整改" in rp["text"]

    # 周确认提交后上墙，作者可点进日志，港湾可看
    from test_agents import _setup, _submit_today
    pm, tech = _setup()
    _submit_today(tech)
    assert any(p["kind"] == "log" and p["link"] for p in pm.get("/api/feed").json()["posts"])

    # 分页：游标取更早的动态且不重复
    for i in range(5):
        t1.post("/api/notes", json={"text": f"记录{i}"})
    first = eng.get("/api/feed?limit=3").json()
    assert len(first["posts"]) == 3 and first["next"]
    rest = eng.get(f"/api/feed?limit=50&before={first['next']}").json()["posts"]
    assert not {p["key"] for p in first["posts"]} & {p["key"] for p in rest}

    # 管理员不参与；领导只读
    _add(env, "boss", "leader", "领导")
    boss = login("boss")
    assert login("admin").get("/api/feed").status_code == 403
    assert boss.get("/api/feed").status_code == 200
    key = first["posts"][0]["key"]
    assert boss.post("/api/feed/like", json={"key": key}).status_code == 403
    assert boss.post("/api/feed/comments", json={"key": key, "text": "x"}).status_code == 403
