"""现场圈 Agent 化：Agent 同事点评、@工笔、接力时间线、AI精选（见 test_circle_picks.py）、公告与已读回执。"""
from conftest import PW, jpeg, login, sample_xer
from test_agents import _setup, _submit_today
from test_roles import _add, _team

from app import db as dbmod


def _setup_site(env):
    """甲/乙两个分包单位 + 港湾岗位 + P6 计划 + 质控提醒。"""
    _team(env)
    eng = login("eng")
    assert eng.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    assert eng.post("/api/quality/reminders").status_code == 200
    return eng


def _snap(c, color=(200, 50, 50)):
    c.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg(color), "image/jpeg"))])


def _key(c, kind):
    return next(p["key"] for p in c.get("/api/feed").json()["posts"] if p["kind"] == kind)


def _kinds() -> set[str]:
    conn = dbmod.connect()
    try:
        return {r["kind"] for r in conn.execute("SELECT kind FROM ai_runs").fetchall()}
    finally:
        conn.close()


def _agent_comments(c, key):
    post = next((p for p in c.get("/api/feed?limit=50").json()["posts"] if p["key"] == key), None)
    return [x for x in post["comments"] if x.get("agent")] if post else []


# ----------------------------------------------------------------------------- 1 Agent 同事点评

def test_agent_comments_come_only_from_real_data(env):
    eng = _setup_site(env)
    t1, t2 = login("tech1"), login("tech2")
    n_work = t1.post("/api/notes", json={"text": "3号墩承台浇完"}).json()
    n_chat = t1.post("/api/notes", json={"text": "今天天气不错"}).json()
    _snap(t1)
    snap_key = _key(t1, "snap")
    work_key, chat_key = f"note:{n_work['id']}", f"note:{n_chat['id']}"

    r = t1.post("/api/circle/review", json={"keys": [work_key, chat_key, snap_key]})
    assert r.status_code == 200, r.text
    got = r.json()["comments"]
    # 无依据不评：闲聊随手记没有任何点评
    assert chat_key not in got
    views = {c["agent"]["view"] for c in got[work_key]}
    assert views == {"quality", "progress"}
    qual = next(c for c in got[work_key] if c["agent"]["view"] == "quality")
    assert "质控" in qual["text"] and "尚未见完成记录" in qual["text"] and qual["role_name"] == "Agent"
    assert qual["agent"]["evidence"] and qual["agent"]["evidence"][0]["link"]["route"] == "finding"
    prog = next(c for c in got[work_key] if c["agent"]["view"] == "progress")
    assert "3#墩承台钢筋绑扎" in prog["text"] and "P6 计划" in prog["text"]
    # 安全视角：来自安全Agent 对随手拍的识别结果
    assert [c["agent"]["view"] for c in got[snap_key]] == ["safety"]
    assert "识别出 1 处隐患" in got[snap_key][0]["text"] and got[snap_key][0]["agent"]["evidence"]

    # 幂等：再次触发不重复生成，读取 feed 时点评随动态返回并和人的评论区分开
    assert t1.post("/api/circle/review", json={"keys": [work_key, snap_key]}).json()["comments"] == {}
    cm = _agent_comments(t1, work_key)
    assert len(cm) == 2 and all(c["mine"] is False for c in cm)
    eng.post("/api/feed/comments", json={"key": work_key, "text": "收到"})
    post = next(p for p in eng.get("/api/feed?limit=50").json()["posts"] if p["key"] == work_key)
    assert len(post["comments"]) == 3 and sum(1 for c in post["comments"] if c.get("agent")) == 2

    # 记录改变（隐患被整改闭环）后，已生成的点评不会被改写，也不会重复评
    assert t2.post("/api/circle/review", json={"keys": [work_key]}).json()["comments"] == {}
    assert len(_agent_comments(t2, work_key)) == 2                     # 现场圈全项目可见：乙单位也看到同样的点评
    assert _kinds() >= {"feed.comment"}  # 点评经 call_json(kind=feed.comment) 并留痕


def test_agent_comment_number_guard_falls_back_to_template(env, monkeypatch):
    """模型写出依据以外的数字时丢弃改写，使用依据模板；不编造。"""
    from app.ai import client
    from app.config import settings
    eng = _setup_site(env)
    t1 = login("tech1")
    n = t1.post("/api/notes", json={"text": "3号墩承台浇完"}).json()
    orig = client.call_json

    def bad(kind, **kw):
        if kind == "feed.comment":
            kw["mock"] = lambda: {"comments": [{"view": "quality", "text": "这里有 99 个质控点没做"}, {"view": "progress", "text": "进度完成 77%"}]}
        return orig(kind, **kw)
    monkeypatch.setattr("app.services.feed_agents.call_json", bad)
    got = t1.post("/api/circle/review", json={"keys": [f"note:{n['id']}"]}).json()["comments"][f"note:{n['id']}"]
    assert got and all("99" not in c["text"] and "77%" not in c["text"] for c in got)
    assert all(c["agent"]["source"] == "rule" for c in got)
    assert settings.ai_provider == "mock"


def test_relay_timeline_uses_real_records(env):
    eng = _setup_site(env)
    t1, sb = login("tech1"), login("tech2")
    _snap(t1)
    key = _key(t1, "snap")
    steps = t1.get(f"/api/circle/relay?key={key}").json()["steps"]
    agents = [s["agent"] for s in steps]
    assert agents[0] == "scribe" and "safety" in agents
    assert "质量" not in "".join(s["text"] for s in steps)  # 没有关联作业，就没有质量/进度环节
    assert "quality" not in agents and "progress" not in agents and "dq" not in agents
    assert next(s for s in steps if s["agent"] == "safety")["link"]["route"] == "finding"
    assert sb.get(f"/api/circle/relay?key={key}").status_code == 200  # 现场圈全项目可见
    assert sb.get("/api/circle/relay?key=snap:9999").status_code == 400

    # 日志：现场记录 → 安全 → 质量(无) → 数据审核 → 进度
    pm, tech = _setup()
    _submit_today(tech)
    lkey = _key(pm, "log")
    ls = eng.get(f"/api/circle/relay?key={lkey}").json()["steps"]
    order = [s["agent"] for s in ls]
    assert order[0] == "scribe" and "dq" in order and "progress" in order
    assert order.index("dq") < order.index("progress")
    assert any("3#墩桩基施工" in s["text"] for s in ls if s["agent"] == "progress")
    # 数据审核细节只给工程主管等港湾岗位和作者本人；项目经理不审核日志，接力里没有数据审核这一步（AGENTS.md）
    assert "dq" in [s["agent"] for s in tech.get(f"/api/circle/relay?key={lkey}").json()["steps"]]
    pls = pm.get(f"/api/circle/relay?key={lkey}").json()["steps"]
    assert "dq" not in [s["agent"] for s in pls] and "progress" in [s["agent"] for s in pls]
    assert not any("数据审核" in s["text"] or "数据审核" in s["name"] for s in pls)
    assert "修改约" in next(s for s in ls if s["agent"] == "scribe")["text"]                           # 草稿修改率属应用成效，只给工程主管等岗位
    assert not any("修改约" in s["text"] or "%" in s["text"] for s in pls if s["agent"] == "scribe")


# ----------------------------------------------------------------------------- 2 @工笔

def test_ask_gongbi_in_comments_with_scope(env):
    eng = _setup_site(env)
    t1, sa, sb = login("tech1"), login("sa_a"), login("tech2")
    n = t1.post("/api/notes", json={"text": "3号墩承台浇完"}).json()
    key = f"note:{n['id']}"

    # 港湾岗位：走问工笔（全项目记录），回答仅港湾可见，不外泄给分包单位
    r = eng.post("/api/circle/ask", json={"key": key, "text": "@工笔 3#墩承台现在进度怎么样"})
    assert r.status_code == 200, r.text
    ans = r.json()["answer"]
    assert ans["agent"]["qa"] and ans["agent"]["audience"] == "harbour" and ans["agent"]["scope"] == "project" and ans["agent"]["mine"]
    assert r.json()["comment"]["text"].startswith("@工笔")
    post = next(p for p in eng.get("/api/feed").json()["posts"] if p["key"] == key)
    texts = [c["text"] for c in post["comments"]]
    assert texts.index(r.json()["comment"]["text"]) + 1 == texts.index(ans["text"])  # 回答紧跟提问
    t_post = next(p for p in t1.get("/api/feed").json()["posts"] if p["key"] == key)
    assert not [c for c in t_post["comments"] if c.get("agent", {}).get("qa")]

    # 分包单位：只基于该动态可见内容回答（不检索全项目日志/预警）
    r2 = sa.post("/api/circle/ask", json={"key": key, "text": "@工笔 承台是什么时候发的"})
    assert r2.status_code == 200, r2.text
    a2 = r2.json()["answer"]["agent"]
    assert a2["scope"] == "post" and a2["audience"] == "all"
    seen = next(p for p in t1.get("/api/feed").json()["posts"] if p["key"] == key)
    assert any(c.get("agent", {}).get("qa") for c in seen["comments"])
    r3 = sa.post("/api/circle/ask", json={"key": key, "text": "@工笔 本月全项目的预警有哪些"})
    assert r3.status_code == 200 and "没有找到" in r3.json()["answer"]["text"]

    # 校验：必须 @工笔 开头、问题非空、看不到的动态不能问、领导/管理员不参与
    assert eng.post("/api/circle/ask", json={"key": key, "text": "进度怎么样"}).status_code == 400
    assert eng.post("/api/circle/ask", json={"key": key, "text": "@工笔  "}).status_code == 400
    assert sb.post("/api/circle/ask", json={"key": "note:9999", "text": "@工笔 不存在"}).status_code == 400
    _add(env, "boss", "leader", "领导")
    assert login("boss").post("/api/circle/ask", json={"key": key, "text": "@工笔 进度"}).status_code == 403
    assert login("admin").post("/api/circle/ask", json={"key": key, "text": "@工笔 进度"}).status_code == 403
    # 公告可以问；旧库里残留的 honor:N 动态键已不存在，不报 500
    nid = eng.post("/api/circle/notices", json={"title": "停电", "body": "明日7点停电"}).json()["key"]
    assert t1.post("/api/circle/ask", json={"key": nid, "text": "@工笔 什么时候停电"}).status_code == 200
    assert eng.post("/api/circle/ask", json={"key": "honor:1", "text": "@工笔 谁入选"}).status_code == 400
    # 提问者可删除自己得到的回答
    raw = ans["agent"]["raw_id"]
    assert sa.delete(f"/api/circle/agent-comments/{raw}").status_code == 403
    assert eng.delete(f"/api/circle/agent-comments/{raw}").status_code == 200


# ----------------------------------------------------------------------------- 3 公告与已读回执

def test_notice_publish_pin_scope_and_read_receipts(env):
    eng = _setup_site(env)
    t1, t2, sa, ss, pm = login("tech1"), login("tech2"), login("sa_a"), login("ss"), login("pm")
    _add(env, "boss", "leader", "领导")
    boss = login("boss")
    for c in (t1, sa, boss, login("admin")):
        r = c.post("/api/circle/notices", json={"title": "x", "body": "y"})
        assert r.status_code == 403  # 分包、领导只读，管理员不参与
    assert eng.get("/api/circle/meta").json() == {"can_publish": True, "companies": ["乙", "甲"], "can_write": True}
    assert t1.get("/api/circle/meta").json()["can_publish"] is False

    # 校验
    assert eng.post("/api/circle/notices", json={"title": " ", "body": "y"}).status_code == 400
    assert eng.post("/api/circle/notices", json={"title": "t", "body": ""}).status_code == 400
    assert eng.post("/api/circle/notices", json={"title": "t", "body": "y", "audience": "company", "company": "丙"}).status_code == 400

    # 发布置顶全项目公告 + 仅甲单位公告
    n1 = eng.post("/api/circle/notices", json={"title": "明日停电检修", "body": "7:00-12:00 全场停电", "pinned": True}).json()
    n2 = ss.post("/api/circle/notices", json={"title": "甲单位安全例会", "body": "周三 8:00", "audience": "company", "company": "甲"}).json()
    id1, id2 = n1["notice"]["id"], n2["notice"]["id"]
    assert n1["notice"]["can_manage"] and n1["notice"]["stats"] == {"read": 0, "total": 7}

    # 通知应读人员（不含发布人）
    notes = t1.get("/api/notifications").json()
    assert {n["title"] for n in notes} >= {"公告：明日停电检修", "公告：甲单位安全例会"}
    assert not any("甲单位安全例会" in n["title"] for n in t2.get("/api/notifications").json())

    # 可见范围：乙单位只见全项目公告；甲单位见两条；置顶在第一页最上方
    p1 = t1.get("/api/feed").json()["posts"]
    assert p1[0]["key"] == f"notice:{id1}" and p1[0]["pinned"] and p1[0]["tag"] == "置顶公告"
    assert f"notice:{id2}" in [p["key"] for p in p1]
    assert f"notice:{id2}" not in [p["key"] for p in t2.get("/api/feed").json()["posts"]]
    assert t2.post(f"/api/circle/notices/{id2}/read").status_code == 400
    assert t2.post("/api/feed/like", json={"key": f"notice:{id2}"}).status_code == 400
    assert boss.get("/api/feed").json()["posts"][0]["key"] == f"notice:{id1}"

    # 已读回执
    assert p1[0]["notice"]["read_me"] is False and p1[0]["notice"]["stats"] is None
    assert t1.post(f"/api/circle/notices/{id1}/read").json()["read"] is True
    assert t1.post(f"/api/circle/notices/{id1}/read").status_code == 200  # 幂等
    sa.post(f"/api/circle/notices/{id1}/read")
    boss.post(f"/api/circle/notices/{id1}/read")
    assert next(p for p in t1.get("/api/feed").json()["posts"] if p["key"] == f"notice:{id1}")["notice"]["read_me"] is True
    reads = eng.get(f"/api/circle/notices/{id1}/reads").json()
    assert {r["name"] for r in reads["read"]} == {"张工", "安全员甲", "领导"}
    assert reads["total"] == 7 and len(reads["unread"]) == 4
    assert t1.get(f"/api/circle/notices/{id1}/reads").status_code == 403  # 成员看不到名单
    assert ss.get(f"/api/circle/notices/{id1}/reads").status_code == 403  # 非发布人（非项目经理）也不行
    assert pm.get(f"/api/circle/notices/{id1}/reads").status_code == 200  # 项目经理可看
    assert eng.get(f"/api/feed?mine=1").json()["posts"][0]["notice"]["stats"]["read"] == 3
    r2 = ss.get(f"/api/circle/notices/{id2}/reads").json()  # 限定单位：应读人员只有甲单位 2 人
    assert r2["total"] == 2

    # 取消置顶后回到时间流；撤回后消失
    assert eng.post(f"/api/circle/notices/{id1}/pin", json={"pinned": False}).json()["pinned"] is False
    assert not next(p for p in t1.get("/api/feed").json()["posts"] if p["key"] == f"notice:{id1}")["pinned"]
    assert t1.delete(f"/api/circle/notices/{id1}").status_code == 403
    assert eng.delete(f"/api/circle/notices/{id1}").status_code == 200
    assert f"notice:{id1}" not in [p["key"] for p in t1.get("/api/feed").json()["posts"]]

    # 公告也可点赞评论；评论通知发布人
    assert t1.post("/api/feed/comments", json={"key": f"notice:{id2}", "text": "收到"}).status_code == 200
    assert any("评论了你的现场动态" in n["title"] for n in ss.get("/api/notifications").json())


def test_pinned_notice_not_repeated_when_paging(env):
    eng = _setup_site(env)
    t1 = login("tech1")
    pid = eng.post("/api/circle/notices", json={"title": "置顶", "body": "内容", "pinned": True}).json()["notice"]["id"]
    for i in range(6):
        t1.post("/api/notes", json={"text": f"记录{i}"})
    first = eng.get("/api/feed?limit=3").json()
    assert first["posts"][0]["key"] == f"notice:{pid}" and len(first["posts"]) == 4 and first["next"]
    rest = eng.get(f"/api/feed?limit=50&before={first['next']}").json()["posts"]
    assert f"notice:{pid}" not in [p["key"] for p in rest]
    assert not {p["key"] for p in first["posts"]} & {p["key"] for p in rest}
