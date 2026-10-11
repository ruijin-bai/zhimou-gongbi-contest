from datetime import date, timedelta

from conftest import login
from test_roles import _add, _team

from app import db as dbmod
from app.services import cmd as cmd_svc
from app.services import findings as find_svc


def _finding(env, title, assignee="tech1", due_days=2, status="open", **body):
    conn = dbmod.connect()
    fid = find_svc.create(conn, env["project_id"], "safety", "hazard", "medium", title, body, due_days=due_days,
                          assignee_id=env["users"][assignee] if assignee else None, fingerprint=f"t:{title}")
    if status != "open":
        conn.execute("UPDATE findings SET status=? WHERE id=?", (status, fid))
    conn.close()
    return fid


def _db_one(sql, *p):
    conn = dbmod.connect()
    try:
        return dbmod.one(conn, sql, p)
    finally:
        conn.close()


def _db_all(sql, *p):
    conn = dbmod.connect()
    try:
        return dbmod.all_(conn, sql, p)
    finally:
        conn.close()


def test_extract_due_phrases():
    t = date(2026, 10, 7)  # 周三
    f = lambda s: cmd_svc.extract_due(s, t)  # noqa: E731
    assert f("明天下班前")[1] == date(2026, 10, 8)
    assert f("周五前")[1] == date(2026, 10, 9)
    assert f("周一")[1] == date(2026, 10, 12)         # 单说“周一”且本周已过，指下周
    assert f("本周一")[1] == date(2026, 10, 5)        # 明说“本周”就不顺延（已过的期限由动作卡提示）
    assert f("下周二")[1] == date(2026, 10, 13)
    assert f("3天内")[1] == date(2026, 10, 10)
    assert f("10月15日前")[1] == date(2026, 10, 15)
    assert f("月底")[1] == date(2026, 10, 31)
    assert f("2026-11-02")[1] == date(2026, 11, 2)
    assert f("后天")[1] == date(2026, 10, 9)
    # “3号墩”是部位，不是日期；没有期限则原话为空
    assert f("3号墩钢筋整改") == ("", None, "3号墩钢筋整改")
    hint, d, rest = f("3#墩整改明天下班前完成")
    assert hint == "明天下班前" and d == date(2026, 10, 8) and "明天" not in rest


def test_parse_assign_card_with_evidence(env):
    _team(env)
    f1 = _finding(env, "钢筋工程：3#墩钢筋保护层垫块缺失", assignee="tech1")
    _finding(env, "临边防护：5#墩临边防护栏杆缺失", assignee="tech1")
    pm = login("pm")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = pm.post("/api/cmd/parse", json={"text": "3#墩钢筋整改派给乙分包李工,明天下班前"})
    assert r.status_code == 200, r.text
    card = r.json()["card"]
    assert card["type"] == "assign" and card["allowed"] and card["ready"]
    assert card["finding_id"] == f1 and card["assignee_id"] == env["users"]["tech2"]
    assert card["due_date"] == tomorrow
    assert any("责任人" in e for e in card["evidence"]) and any("对象" in e for e in card["evidence"])
    # 解析阶段不改任何业务数据
    assert _db_one("SELECT assignee_id FROM findings WHERE id=?", f1)["assignee_id"] == env["users"]["tech1"]
    ev = _db_one("SELECT * FROM events WHERE kind='cmd.parse'")
    assert ev and ev["user_id"] == env["users"]["pm"]
    assert _db_one("SELECT kind, ok FROM ai_runs WHERE kind='cmd.parse'")["ok"] == 1


def test_parse_ambiguity_asks_instead_of_guessing(env):
    _team(env)
    _add(env, "li2", "tech", "李工", "甲")      # 与乙单位李工重名
    a = _finding(env, "钢筋工程：3#墩钢筋绑扎间距超标", assignee="tech1")
    b = _finding(env, "钢筋工程：3#墩钢筋保护层垫块缺失", assignee="tech1")
    pm = login("pm")
    # 重名但说了单位 → 唯一
    c = pm.post("/api/cmd/parse", json={"text": "3#墩钢筋绑扎派给乙分包李工"}).json()["card"]
    assert c["assignee_id"] == env["users"]["tech2"]
    # 没说单位 → 候选点选，不预选
    c = pm.post("/api/cmd/parse", json={"text": "3#墩钢筋绑扎派给李工"}).json()["card"]
    assert c["assignee_id"] is None and len(c["assignee_candidates"]) == 2 and not c["ready"]
    assert any("责任人" in q for q in c["questions"])
    # 对象有并列候选 → 不预选
    c = pm.post("/api/cmd/parse", json={"text": "3#墩钢筋整改派给张工"}).json()["card"]
    assert c["finding_id"] is None and {x["id"] for x in c["finding_candidates"]} == {a, b} and not c["ready"]
    # 只说单位 → 列出该单位的人
    c = pm.post("/api/cmd/parse", json={"text": "3#墩钢筋绑扎交给乙分包"}).json()["card"]
    assert c["assignee_id"] is None and {p["name"] for p in c["assignee_candidates"]} == {"李工", "安全员乙"}
    # 找不到对象 / 听不懂
    c = pm.post("/api/cmd/parse", json={"text": "8#墩支架派给张工"}).json()["card"]
    assert c["finding_id"] is None and c["finding_candidates"] == [] and not c["ready"]
    c = pm.post("/api/cmd/parse", json={"text": "帮我新建一个待办"}).json()["card"]
    assert c["type"] == "unsupported" and not c["allowed"] and c["deny_reason"]


def test_execute_assign_and_nudge_reuse_existing_logic(env):
    _team(env)
    fid = _finding(env, "钢筋工程：3#墩钢筋保护层垫块缺失", assignee="tech1", due_days=1)
    pm, t2 = login("pm"), login("tech2")
    due = (date.today() + timedelta(days=3)).isoformat()
    r = pm.post("/api/cmd/execute", json={"type": "assign", "finding_id": fid, "assignee_id": env["users"]["tech2"],
                                           "due_date": due, "source": "3#墩钢筋整改派给乙分包李工"})
    assert r.status_code == 200, r.text
    row = _db_one("SELECT assignee_id, due_date FROM findings WHERE id=?", fid)
    assert row == {"assignee_id": env["users"]["tech2"], "due_date": due}
    assert [n for n in t2.get("/api/notifications").json() if "新派单" in n["title"] and due in n["body"]]
    acts = {a["action"] for a in _db_all("SELECT action FROM audit")}
    assert {"finding.assign", "cmd.assign"} <= acts and _db_one("SELECT 1 x FROM events WHERE kind='cmd.exec'")

    r = pm.post("/api/cmd/execute", json={"type": "nudge", "finding_id": fid, "note": "今天内处理"})
    assert r.status_code == 200 and "已催办" in r.json()["message"]
    assert any("催办" in n["title"] for n in t2.get("/api/notifications").json())
    assert "cmd.nudge" in {a["action"] for a in _db_all("SELECT action FROM audit")}

    # 校验：缺字段、过去的期限、已闭环事项、没有责任人的催办
    assert pm.post("/api/cmd/execute", json={"type": "assign", "finding_id": fid}).status_code == 400
    old = (date.today() - timedelta(days=1)).isoformat()
    assert pm.post("/api/cmd/execute", json={"type": "assign", "finding_id": fid, "assignee_id": env["users"]["tech1"], "due_date": old}).status_code == 400
    assert _db_one("SELECT assignee_id FROM findings WHERE id=?", fid)["assignee_id"] == env["users"]["tech2"]
    done = _finding(env, "已闭环事项", status="resolved")
    assert pm.post("/api/cmd/execute", json={"type": "assign", "finding_id": done, "assignee_id": env["users"]["tech1"]}).status_code == 400
    orphan = _finding(env, "无人负责的事项", assignee=None)
    assert pm.post("/api/cmd/execute", json={"type": "nudge", "finding_id": orphan}).status_code == 400
    assert pm.post("/api/cmd/execute", json={"type": "assign", "finding_id": fid, "assignee_id": 99999}).status_code == 400
    # 派单失败时期限不应被改动（整体回滚）
    other = (date.today() + timedelta(days=9)).isoformat()
    assert pm.post("/api/cmd/execute", json={"type": "assign", "finding_id": done, "assignee_id": env["users"]["tech1"], "due_date": other}).status_code == 400
    assert _db_one("SELECT due_date FROM findings WHERE id=?", done)["due_date"] != other


def test_nudge_by_person_lists_their_open_items(env):
    _team(env)
    a = _finding(env, "临边防护：6#墩栏杆缺失", assignee="tech2", due_days=-1)
    _finding(env, "用电：配电箱无接地", assignee="tech1")
    pm = login("pm")
    c = pm.post("/api/cmd/parse", json={"text": "催一下乙分包李工"}).json()["card"]
    assert c["type"] == "nudge" and c["finding_id"] == a and c["ready"]
    c = pm.post("/api/cmd/parse", json={"text": "催办配电箱"}).json()["card"]
    assert c["type"] == "nudge" and c["finding"]["title"].startswith("用电") and c["assignee"] is None


def test_command_permissions(env):
    _team(env)
    fid = _finding(env, "钢筋工程：3#墩钢筋保护层垫块缺失")
    body = {"text": "3#墩钢筋派给张工"}
    # 分包、领导、管理员不能下指令
    _add(env, "boss", "leader", "领导")
    for u in ("tech1", "sa_a", "boss", "admin"):
        c = login(u)
        assert c.post("/api/cmd/parse", json=body).status_code == 403, u
        assert c.post("/api/cmd/execute", json={"type": "assign", "finding_id": fid, "assignee_id": env["users"]["tech2"]}).status_code == 403, u
        assert c.post("/api/cmd/minutes", json={"text": "议题一：测试议题内容"}).status_code == 403, u
        assert c.get("/api/cmd/findings?q=钢筋").status_code == 403, u
        assert c.get("/api/meetings/review").status_code == 403, u
    assert _db_one("SELECT assignee_id FROM findings WHERE id=?", fid)["assignee_id"] == env["users"]["tech1"]
    # 港湾三个管理岗可用；缺能力的卡片标记不可执行
    for u in ("pm", "eng", "ss"):
        assert login(u).post("/api/cmd/parse", json=body).status_code == 200, u
    assert login("pm").post("/api/cmd/parse", json={"text": ""}).status_code == 400
    # 若角色缺少该能力（如被收回催办权），卡片不可执行并说明原因
    conn = dbmod.connect()
    try:
        user = dbmod.one(conn, "SELECT * FROM users WHERE username='eng'")
        from app import roles as R
        old = R.CAPS["nudge"]
        R.CAPS["nudge"] = old - {"eng_sup"}
        try:
            card = cmd_svc.build_card(conn, user, {"action": "nudge", "object": "钢筋", "assignee": "", "due_hint": "", "due_date": "", "note": ""},
                                      "催办钢筋", date.today())
        finally:
            R.CAPS["nudge"] = old
    finally:
        conn.close()
    assert not card["allowed"] and not card["ready"] and "无催办权限" in card["deny_reason"]


def test_minutes_to_actions_and_confirm(env):
    _team(env)
    fid = _finding(env, "钢筋工程：3#墩钢筋保护层垫块缺失", assignee="tech1")
    pm, t2 = login("pm"), login("tech2")
    text = ("会议名称：第12次生产例会\n"
            "议题一：3#墩钢筋整改。结论：乙分包李工明天下班前完成3#墩钢筋整改。\n"
            "议题二：雨季施工准备。\n"
            "行动项：甲分包张工周五前提交雨季专项方案。\n"
            "行动项：业主代表确认变更签证。\n"
            "项目部要求各分包加强班前教育")
    r = pm.post("/api/cmd/minutes", json={"text": text})
    assert r.status_code == 200, r.text
    out = r.json()
    mid = out["meeting"]["id"]
    assert out["meeting"]["title"] == "第12次生产例会" and not out["meeting"]["confirmed"]
    assert [t["topic"] for t in out["meeting"]["topics"]][:2] == ["3#墩钢筋整改", "雨季施工准备"]
    assert out["meeting"]["topics"][0]["conclusion"].startswith("乙分包李工")
    cards = out["cards"]
    assert len(cards) == 3
    a0, a1, a2 = cards
    assert a0["owner_id"] == env["users"]["tech2"] and a0["due_date"] == (date.today() + timedelta(days=1)).isoformat()
    assert a0["finding_id"] == fid                       # 关联到既有整改单
    assert a1["owner_id"] == env["users"]["tech1"] and a1["due_date"]
    assert a2["owner_id"] is None and a2["owner_candidates"] == [] and a2["questions"]   # 业主代表不在系统内：不猜
    # 确认前没有任何行动项落库，也没有“上次例会”
    assert _db_one("SELECT COUNT(*) n FROM meeting_items")["n"] == 0
    assert pm.get("/api/meetings/review").json()["message"] == "暂无上次例会"

    it = pm.post(f"/api/meetings/{mid}/items", json={"idx": 0, "text": a0["text"], "owner_id": a0["owner_id"], "due_date": a0["due_date"],
                                                      "finding_id": a0["finding_id"]})
    assert it.status_code == 200, it.text
    assert it.json()["status"] == "open" and it.json()["finding"]["id"] == fid and it.json()["owner"] == "李工"
    # 同一条不能重复确认；责任人有账号则收到通知；整改单本身不被改动
    assert pm.post(f"/api/meetings/{mid}/items", json={"idx": 0, "text": a0["text"]}).status_code == 400
    assert [n for n in t2.get("/api/notifications").json() if n["title"].startswith("例会行动项")]
    assert _db_one("SELECT status, assignee_id FROM findings WHERE id=?", fid) == {"status": "open", "assignee_id": env["users"]["tech1"]}
    # 外部责任人用文字记录；无效责任人/整改单被拒
    ext = pm.post(f"/api/meetings/{mid}/items", json={"idx": 2, "text": a2["text"], "owner_name": "业主代表"}).json()
    assert ext["owner"] == "业主代表" and ext["owner_id"] is None
    assert pm.post(f"/api/meetings/{mid}/items", json={"idx": 1, "text": "x", "owner_id": env["users"]["admin"]}).status_code == 400
    assert pm.post(f"/api/meetings/{mid}/items", json={"idx": 1, "text": "x", "finding_id": 99999}).status_code == 400
    assert pm.post(f"/api/meetings/{mid}/items", json={"idx": 1, "text": " "}).status_code == 400
    assert {"meeting.item"} <= {a["action"] for a in _db_all("SELECT action FROM audit")}
    assert _db_all("SELECT 1 x FROM events WHERE kind IN ('cmd.minutes','meeting.item')")


def _confirm(pm, mid, text, owner=None, due=None, idx=None, finding=None):
    r = pm.post(f"/api/meetings/{mid}/items", json={"idx": idx, "text": text, "owner_id": owner, "due_date": due, "finding_id": finding})
    assert r.status_code == 200, r.text
    return r.json()


def test_review_numbers_come_from_table(env):
    _team(env)
    pm, eng, t1, t2 = login("pm"), login("eng"), login("tech1"), login("tech2")
    assert pm.get("/api/meetings/review").json() == {"meetings": [], "meeting": None, "stats": None, "open_items": [], "done_items": [],
                                                     "message": "暂无上次例会"}
    today = date.today()
    old = pm.post("/api/cmd/minutes", json={"text": "议题一：上次的议题内容", "date": (today - timedelta(days=7)).isoformat()}).json()["meeting"]["id"]
    _confirm(pm, old, "旧会议的事项", env["users"]["tech1"], today.isoformat())
    mid = pm.post("/api/cmd/minutes", json={"text": "议题一：这次的议题内容", "date": (today - timedelta(days=1)).isoformat()}).json()["meeting"]["id"]
    # 草稿（未确认）不算会议
    draft = pm.post("/api/cmd/minutes", json={"text": "议题一：只是草稿没确认"}).json()["meeting"]["id"]
    ids = [
        _confirm(pm, mid, "事项甲", env["users"]["tech1"], (today + timedelta(days=3)).isoformat())["id"],
        _confirm(pm, mid, "事项乙", env["users"]["tech2"], (today - timedelta(days=2)).isoformat())["id"],
        _confirm(pm, mid, "事项丙", env["users"]["tech2"], (today - timedelta(days=1)).isoformat())["id"],
        _confirm(pm, mid, "事项丁", None, None)["id"],
    ]
    # 责任人本人可完成；无关的人不能；撤销完成只有管理岗
    assert t1.post(f"/api/meetings/items/{ids[0]}/done", json={}).status_code == 200
    assert t1.post(f"/api/meetings/items/{ids[1]}/done", json={}).status_code == 403
    assert t2.post(f"/api/meetings/items/{ids[2]}/done", json={}).status_code == 200
    assert t2.post(f"/api/meetings/items/{ids[2]}/done", json={"done": False}).status_code == 403
    assert eng.post(f"/api/meetings/items/{ids[2]}/done", json={"done": False}).status_code == 200
    assert login("admin").post(f"/api/meetings/items/{ids[3]}/done", json={}).status_code == 403
    assert eng.post(f"/api/meetings/items/{ids[3]}/done", json={}).status_code == 200

    rv = pm.get("/api/meetings/review").json()
    assert rv["meeting"]["id"] == mid and rv["is_last"] and [m["id"] for m in rv["meetings"]] == [mid, old]
    assert draft not in [m["id"] for m in rv["meetings"]]
    assert rv["stats"] == {"total": 4, "done": 2, "overdue": 2, "open": 0}
    assert [i["text"] for i in rv["open_items"]] == ["事项乙", "事项丙"] and rv["open_items"][0]["days_over"] == 2
    assert {i["text"] for i in rv["done_items"]} == {"事项甲", "事项丁"}
    # 数字与表一致
    n = _db_one("SELECT COUNT(*) n, SUM(status='done') d FROM meeting_items WHERE meeting_id=?", mid)
    assert (n["n"], n["d"]) == (4, 2)
    # 能选历史会议；上次例会无行动项时如实说明
    assert pm.get(f"/api/meetings/review?meeting_id={old}").json()["stats"]["total"] == 1
    assert pm.get("/api/meetings/review?meeting_id=99999").status_code == 400
    me = t2.get("/api/meetings/mine").json()
    assert [i["text"] for i in me] == ["事项乙", "事项丙"] and login("admin").get("/api/meetings/mine").status_code == 403
    only = pm.post("/api/cmd/minutes", json={"text": "议题一：没有行动项的会"}).json()["meeting"]["id"]
    assert pm.post(f"/api/meetings/{only}/save").status_code == 200
    rv = pm.get("/api/meetings/review").json()
    assert rv["meeting"]["id"] == only and rv["stats"]["total"] == 0 and "没有已确认的行动项" in rv["message"]


def test_project_isolation(env):
    _team(env)
    pm = login("pm")
    mid = pm.post("/api/cmd/minutes", json={"text": "议题一：隔离性测试议题"}).json()["meeting"]["id"]
    assert pm.post(f"/api/meetings/{mid}/save").status_code == 200
    conn = dbmod.connect()
    pid2 = dbmod.insert(conn, "projects", {"name": "另一个项目", "code": "T2", "config_json": "{}", "created_at": dbmod.now_iso()})
    conn.execute("UPDATE users SET project_id=? WHERE username='eng'", (pid2,))
    conn.close()
    other = login("eng")
    assert other.post(f"/api/meetings/{mid}/items", json={"idx": 0, "text": "越权"}).status_code == 400
    assert other.get(f"/api/meetings/review?meeting_id={mid}").status_code == 400


def test_nudge_with_place_not_in_finding_text_still_matches(env):
    """“1#墩泥浆池围挡”：隐患标题里没有部位号（部位在照片地点里）时，不因部位号缺失把明显匹配的事项判没。"""
    _team(env)
    a = _finding(env, "临边洞口：泥浆池周边未设置围挡及警示标识", assignee="tech2", due_days=-1)
    _finding(env, "用电：配电箱无接地", assignee="tech1")
    pm = login("pm")
    c = pm.post("/api/cmd/parse", json={"text": "催一下李工，1#墩泥浆池围挡今天下班前必须整改完"}).json()["card"]
    assert c["type"] == "nudge" and (c["finding_id"] == a or a in [x["id"] for x in c["finding_candidates"]])


def test_nudge_person_with_unmatched_object_lists_their_items(env):
    """点了人但事项描述没对上：列出这个人手上进行中的事项供点选，而不是“没找到对象”。"""
    _team(env)
    a = _finding(env, "临边防护：6#墩栏杆缺失", assignee="tech2", due_days=-1)
    pm = login("pm")
    c = pm.post("/api/cmd/parse", json={"text": "催一下李工，那个吊车的事抓紧"}).json()["card"]
    assert c["type"] == "nudge" and a in [x["id"] for x in c["finding_candidates"]]
