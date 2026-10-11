"""现场圈讨论组（话题 / 问题 / 专题）：建组、发帖、赞与评论、关联工点或作业、关闭与置顶，权限与现场圈一致。"""
from conftest import jpeg, login, sample_xer
from test_roles import _add, _team


def test_talk_groups_flow_and_permissions(env):
    _team(env)
    _add(env, "boss", "leader", "领导")
    t1, t2, sb, eng, ss, pm = login("tech1"), login("tech2"), login("sa_b"), login("eng"), login("ss"), login("pm")
    leader, admin = login("boss"), login("admin")
    assert eng.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200

    # 建组可选工点与作业；作业显示中文简称，不露代码
    opt = t1.get("/api/talk/options").json()
    assert {k["k"] for k in opt["kinds"]} == {"topic", "issue", "special"} and opt["activities"]
    a = opt["activities"][0]
    assert a["code"] not in a["label"] and opt["parts"]

    # 建组：分包技术员可建；领导只读、系统管理员不参与；类型、名称、作业校验
    g = t1.post("/api/talk/groups", json={"kind": "issue", "title": "3#墩钢筋到场晚", "intro": "影响承台", "activity_code": a["code"]}).json()
    assert g["kind_name"] == "问题" and g["status"] == "open" and g["activity"]["label"] and g["part"] == a["part"] and g["can_manage"] and not g["can_pin"]
    assert leader.post("/api/talk/groups", json={"kind": "topic", "title": "x"}).status_code == 403
    assert admin.get("/api/talk/groups").status_code == 403
    assert t1.post("/api/talk/groups", json={"kind": "bad", "title": "x"}).status_code == 400
    assert t1.post("/api/talk/groups", json={"kind": "topic", "title": " "}).status_code == 400
    assert t1.post("/api/talk/groups", json={"kind": "topic", "title": "x", "activity_code": "NOPE"}).status_code == 400
    topic = eng.post("/api/talk/groups", json={"kind": "special", "title": "雨季施工专题", "part": "路基"}).json()

    # 全项目可见：其他分包单位、领导都看得到
    assert {x["id"] for x in sb.get("/api/talk/groups").json()["items"]} == {g["id"], topic["id"]}
    assert leader.get(f"/api/talk/groups/{g['id']}").json()["can_write"] is False

    # 发帖（文字 + 照片）：照片不做隐患识别，其他单位能看；建组人收到通知
    ph = t2.post("/api/media", data={"purpose": "talk", "scan": "0"}, files=[("files", ("a.jpg", jpeg((30, 90, 160)), "image/jpeg"))]).json()[0]
    assert ph["safety_status"] == "na"
    sa = login("sa_a")                                                        # 甲单位，与发帖人（乙）不同单位
    assert sa.get(f"/api/media/{ph['id']}/thumb").status_code == 403          # 还没发到讨论组：其他单位看不到
    p = t2.post(f"/api/talk/groups/{g['id']}/posts", json={"text": "乙方钢筋可以先借", "media_ids": [ph["id"]]}).json()
    assert p["photos"] and p["author"]["mine"]
    assert sa.get(f"/api/media/{ph['id']}/thumb").status_code == 200
    assert t2.post(f"/api/talk/groups/{g['id']}/posts", json={"text": "", "media_ids": []}).status_code == 400
    other = t1.post("/api/media", files=[("files", ("b.jpg", jpeg((10, 120, 10)), "image/jpeg"))]).json()[0]
    assert t2.post(f"/api/talk/groups/{g['id']}/posts", json={"text": "盗图", "media_ids": [other["id"]]}).status_code == 400
    assert any("发了帖" in n["title"] for n in t1.get("/api/notifications").json())
    assert leader.post(f"/api/talk/groups/{g['id']}/posts", json={"text": "x"}).status_code == 403

    # 赞、评论、回复与通知；只能删自己的
    assert eng.post(f"/api/talk/posts/{p['id']}/like").json() == {"n": 1, "me": True, "names": ["工程主管"]}
    v = eng.post(f"/api/talk/posts/{p['id']}/comments", json={"text": "同意，今天协调"}).json()
    c = v["comments"][0]
    v = t2.post(f"/api/talk/posts/{p['id']}/comments", json={"text": "好的", "reply_to": c["id"]}).json()
    assert v["comments"][1]["reply_to"]["name"] == "工程主管"
    assert any("评论了你" in n["title"] for n in t2.get("/api/notifications").json())
    assert any("回复了你" in n["title"] for n in eng.get("/api/notifications").json())
    assert t2.delete(f"/api/talk/comments/{c['id']}").status_code == 403
    assert eng.delete(f"/api/talk/comments/{c['id']}").status_code == 200

    # 置顶只给港湾管理岗；关闭给建组人或港湾管理岗；关闭后只读、不能置顶，可重新打开
    assert t1.post(f"/api/talk/groups/{g['id']}/pin", json={"pinned": True}).status_code == 403
    assert ss.post(f"/api/talk/groups/{topic['id']}/pin", json={"pinned": True}).json()["pinned"] is True
    assert pm.get("/api/talk/groups").json()["items"][0]["id"] == topic["id"]
    assert t2.post(f"/api/talk/groups/{g['id']}/status", json={"closed": True}).status_code == 403
    done = t1.post(f"/api/talk/groups/{g['id']}/status", json={"closed": True, "note": "钢筋已到"}).json()
    assert done["status"] == "closed" and done["closed"]["note"] == "钢筋已到"
    assert t2.post(f"/api/talk/groups/{g['id']}/posts", json={"text": "再说一句"}).status_code == 400
    assert eng.post(f"/api/talk/posts/{p['id']}/comments", json={"text": "x"}).status_code == 400
    assert eng.post(f"/api/talk/groups/{g['id']}/pin", json={"pinned": True}).status_code == 400
    lst = t1.get("/api/talk/groups").json()
    assert [x["id"] for x in lst["items"]] == [topic["id"]] and lst["counts"] == {"open": 1, "closed": 1}
    assert [x["id"] for x in t1.get("/api/talk/groups?status=closed").json()["items"]] == [g["id"]]
    assert [x["id"] for x in t1.get("/api/talk/groups?status=all&kind=special").json()["items"]] == [topic["id"]]
    assert eng.post(f"/api/talk/groups/{g['id']}/status", json={"closed": False}).json()["status"] == "open"

    # 帖子只能本人删除
    assert t1.delete(f"/api/talk/posts/{p['id']}").status_code == 403
    assert t2.delete(f"/api/talk/posts/{p['id']}").status_code == 200
    assert t1.get(f"/api/talk/groups/{g['id']}").json()["posts"] == []
