"""现场圈 AI精选：只从真实动态里挑，有数据才入选，理由带数字守卫，按读者可见范围过滤。"""
from conftest import confirm_hazards, jpeg, login
from test_circle_agents import _setup_site
from test_circle_agents import _snap as _snap_raw

from app import db as dbmod
from app.ai.client import AIError
from app.services import circle_picks as picks


def _snap(c, color=(200, 50, 50)):
    """随手拍并视为已确认（AI 候选隐患未经人确认不进“已派单”候选，见 test_candidate.py）。"""
    _snap_raw(c, color)
    confirm_hazards()


def _hazard_closed(tech, n=1):
    """随手拍 n 处隐患并全部提交整改照片，AI 复核只给建议，由安全主管人工确认闭环。"""
    for i in range(n):
        _snap(tech, (200 - i * 20, 50, 50))
    ss = login("ss")
    for f in tech.get("/api/findings?agent=safety&status=active&mine=1").json():
        rect = tech.post("/api/media", data={"purpose": "rectify"}, files=[("files", (f"r{f['id']}.jpg", jpeg((20 + f["id"], 210, 20)), "image/jpeg"))]).json()[0]
        assert tech.post(f"/api/findings/{f['id']}/rectify", json={"media_id": rect["id"]}).status_code == 200
        assert ss.post(f"/api/findings/{f['id']}/status", json={"status": "resolved"}).status_code == 200


def _ai_posts(c, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return c.get(f"/api/feed?view=ai&{qs}").json()


def _rows(project_id):
    conn = dbmod.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM circle_picks WHERE project_id=? ORDER BY id", (project_id,)).fetchall()]
    finally:
        conn.close()


def _fresh():
    picks._auto_tried.clear()


# ----------------------------------------------------------------------------- 1 没有候选不生成

def test_no_candidates_no_picks(env):
    eng = _setup_site(env)
    t1 = login("tech1")
    t1.post("/api/notes", json={"text": "今天天气不错"})  # 普通随手记不是候选
    _fresh()
    out = eng.post("/api/circle/review", json={"keys": []}).json()
    assert out["picked"] is False
    assert _rows(env["project_id"]) == []
    d = _ai_posts(eng)
    assert d["posts"] == [] and d["next"] is None
    # 全部视图里的动态都带 ai_pick 字段，且为空
    posts = eng.get("/api/feed").json()["posts"]
    assert posts and all(p["ai_pick"] is None for p in posts)


# ----------------------------------------------------------------------------- 2 整改闭环被精选 + 6 feed.ai_pick

def test_closed_rectification_is_picked_with_reason(env):
    eng = _setup_site(env)
    t1 = login("tech1")
    t1.post("/api/notes", json={"text": "普通记录"})
    _hazard_closed(t1)
    _fresh()
    out = eng.post("/api/circle/review", json={"keys": []}).json()
    assert out["picked"] is True
    rows = _rows(env["project_id"])
    assert len(rows) == 1 and rows[0]["post_key"].startswith("rect:") and rows[0]["source"] == "ai" and rows[0]["ai_run_id"]
    assert 0 < len(rows[0]["reason"]) <= 40

    d = _ai_posts(eng)
    assert [p["key"] for p in d["posts"]] == [rows[0]["post_key"]]
    p = d["posts"][0]
    assert p["kind"] == "rect" and p["compare"] is True
    assert p["ai_pick"]["reason"] == rows[0]["reason"] and p["ai_pick"]["source"] == "ai"
    # 同一条动态在“全部”里也带 ai_pick，其余动态为空
    allp = {x["key"]: x for x in eng.get("/api/feed?limit=50").json()["posts"]}
    assert allp[p["key"]]["ai_pick"]["reason"] == rows[0]["reason"]
    assert all(x["ai_pick"] is None for k, x in allp.items() if k != p["key"])
    # 该隐患已闭环，对应随手拍不再重复精选；当天再打开不再挑
    assert eng.post("/api/circle/review", json={"keys": []}).json()["picked"] is False
    _fresh()
    assert eng.post("/api/circle/review", json={"keys": []}).json()["picked"] is False  # 当天已挑过一轮
    assert len(_rows(env["project_id"])) == 1
    # 经 call_json(kind=feed.pick) 留痕
    conn = dbmod.connect()
    try:
        assert conn.execute("SELECT COUNT(*) n FROM ai_runs WHERE kind='feed.pick'").fetchone()["n"] == 1
    finally:
        conn.close()


def test_max_three_per_round_and_no_repeat(env):
    _setup_site(env)
    t1 = login("tech1")
    _hazard_closed(t1, 5)  # 多条已闭环整改 → 每轮最多 3 条
    conn = dbmod.connect()
    try:
        first = picks.generate_picks(conn, env["project_id"])
        assert len(first) == 3 and len({c["post_key"] for c in first}) == 3
        assert all(c["post_key"].startswith("rect:") for c in first)
        second = picks.generate_picks(conn, env["project_id"])  # 已精选的不重复，剩下的才进
        assert len(second) == 1 and second[0]["post_key"] not in {c["post_key"] for c in first}
        assert picks.generate_picks(conn, env["project_id"]) == []  # 没有候选就不凑数
    finally:
        conn.close()


def test_candidate_rules_snap_dispatched_and_low_risk_week_confirm(env):
    from datetime import date, timedelta
    _setup_site(env)
    t1, t2 = login("tech1"), login("tech2")
    _snap(t1)  # 中风险隐患，已派单给拍摄者 → 规则②
    conn = dbmod.connect()
    try:
        def week_log(user, risk, score, submitted):
            mon = date.today() - timedelta(days=date.today().weekday())
            dbmod.insert(conn, "logs", {
                "project_id": env["project_id"], "author_id": env["users"][user], "log_date": (mon - timedelta(days=7)).isoformat(), "mode": "ai",
                "status": "submitted", "started_at": dbmod.now_iso(), "submitted_at": submitted, "period": "week",
                "period_end": (mon - timedelta(days=1)).isoformat(), "dq_json": dbmod.dumps({"score": score, "risk": risk, "summary": "ok", "checks": []})})
        week_log("tech1", "low", 96, dbmod.now_iso())      # 低风险且评分≥90 → 规则③
        week_log("tech2", "low", 85, dbmod.now_iso())      # 评分不足 → 不入选
        cands = picks.candidates(conn, env["project_id"])
        assert [(c["rule"], c["key"].split(":")[0]) for c in cands] == [(2, "snap"), (3, "log")]
        assert "已派单" in cands[0]["facts"] and "周进度确认已提交" in cands[1]["facts"]
        # 精选对项目经理也可见：规则③的事实与模板理由不写“数据审核”与评分（AGENTS.md）
        assert not any(w in cands[1]["facts"] + cands[1]["tpl"] for w in ("数据审核", "评分", "96"))
        new = picks.generate_picks(conn, env["project_id"])
        assert len(new) == 2 and all(0 < len(n["reason"]) <= 40 for n in new)
        assert not any("数据审核" in n["reason"] for n in new)
    finally:
        conn.close()
    assert {p["key"] for p in _ai_posts(login("eng"), limit=50)["posts"]} == {n["post_key"] for n in new}
    assert {p["key"] for p in _ai_posts(t2, limit=50)["posts"]} == {n["post_key"] for n in new}   # 现场圈全项目可见


# ----------------------------------------------------------------------------- 3 精选全项目可见（RJ 10-09：现场圈大家都能看）

def test_subcontractor_sees_all_picks(env):
    eng = _setup_site(env)
    t1, t2, sa, sb = login("tech1"), login("tech2"), login("sa_a"), login("sa_b")
    _hazard_closed(t1)
    _fresh()
    assert eng.post("/api/circle/review", json={"keys": []}).json()["picked"] is True
    key = _rows(env["project_id"])[0]["post_key"]
    assert [p["key"] for p in _ai_posts(eng)["posts"]] == [key]
    assert [p["key"] for p in _ai_posts(t1)["posts"]] == [key]
    assert [p["key"] for p in _ai_posts(sa)["posts"]] == [key]  # 同单位安全员
    assert [p["key"] for p in _ai_posts(t2)["posts"]] == [key] and [p["key"] for p in _ai_posts(sb)["posts"]] == [key]
    assert key in [p["key"] for p in t2.get("/api/feed?limit=50").json()["posts"]]
    # 领导只读可见全项目
    from test_roles import _add
    _add(env, "boss", "leader", "领导")
    assert [p["key"] for p in _ai_posts(login("boss"))["posts"]] == [key]
    assert login("admin").get("/api/feed?view=ai").status_code == 403


# ----------------------------------------------------------------------------- 4 数字守卫与失败回退

def _break_ai(monkeypatch, reason=None):
    from app.ai import client
    orig = client.call_json

    def bad(kind, **kw):
        if kind == "feed.pick":
            if reason is None:
                raise AIError("模型不可用")
            keys = [c["key"] for c in kw["mock"]()["picks"]]
            kw["mock"] = lambda: {"picks": [{"key": k, "reason": reason} for k in keys]}
        return orig(kind, **kw)
    monkeypatch.setattr("app.services.circle_picks.call_json", bad)


def test_ai_reason_with_foreign_numbers_falls_back_to_rule(env, monkeypatch):
    eng = _setup_site(env)
    _hazard_closed(login("tech1"))
    _break_ai(monkeypatch, "本周整改闭环 99 项，效率提升 77%")
    _fresh()
    assert eng.post("/api/circle/review", json={"keys": []}).json()["picked"] is True
    rows = _rows(env["project_id"])
    assert len(rows) == 1 and rows[0]["source"] == "rule" and rows[0]["ai_run_id"] is None
    assert "99" not in rows[0]["reason"] and "77" not in rows[0]["reason"] and rows[0]["reason"]
    assert _ai_posts(eng)["posts"][0]["ai_pick"]["source"] == "rule"


def test_ai_failure_falls_back_to_rule_reason(env, monkeypatch):
    eng = _setup_site(env)
    _hazard_closed(login("tech1"))
    _break_ai(monkeypatch, None)
    _fresh()
    assert eng.post("/api/circle/review", json={"keys": []}).json()["picked"] is True
    rows = _rows(env["project_id"])
    assert len(rows) == 1 and rows[0]["source"] == "rule" and rows[0]["reason"]


def test_ai_reason_validation_rules():
    cands = [{"key": "rect:1", "facts": "整改闭环：隐患“3号墩”经复核已闭环", "author_id": 7}]
    names = {7: "张工"}
    ok = picks._norm({"picks": [{"key": "rect:1", "reason": "3号墩整改已闭环"}]}, cands, names)
    assert ok == [{"key": "rect:1", "reason": "3号墩整改已闭环"}]
    for bad in ({"picks": []}, {"picks": [{"key": "rect:9", "reason": "x"}]}, {"picks": [{"key": "rect:1", "reason": "x" * 41}]},
                {"picks": [{"key": "rect:1", "reason": "张工做得好"}]}, {"picks": [{"key": "rect:1", "reason": "闭环 5 项"}]},
                {"picks": [{"key": "rect:1", "reason": "好"}, {"key": "rect:1", "reason": "好"}]}):
        try:
            picks._norm(bad, cands, names)
        except ValueError:
            continue
        raise AssertionError(f"应当被拒绝：{bad}")


# ----------------------------------------------------------------------------- 5 view=ai 分页与可见性

def test_ai_view_paging_and_visibility(env):
    eng = _setup_site(env)
    t1, t2 = login("tech1"), login("tech2")
    mine = [t1.post("/api/notes", json={"text": f"甲记录{i}"}).json()["id"] for i in range(3)]
    other = [t2.post("/api/notes", json={"text": f"乙记录{i}"}).json()["id"] for i in range(2)]
    conn = dbmod.connect()
    try:
        # 按精选先后：甲、乙交错，再加一条已不存在的动态和一个旧库残留的 honor 键
        order = [f"note:{mine[0]}", f"note:{other[0]}", "note:99999", f"note:{mine[1]}", "honor:1", f"note:{other[1]}", f"note:{mine[2]}"]
        for k in order:
            dbmod.insert(conn, "circle_picks", {"project_id": env["project_id"], "post_key": k, "reason": "测试理由", "source": "rule", "created_at": dbmod.now_iso()})
    finally:
        conn.close()

    def walk(c, limit):
        keys, cur = [], None
        for _ in range(10):
            d = _ai_posts(c, limit=limit, **({"before": cur} if cur else {}))
            keys += [p["key"] for p in d["posts"]]
            cur = d["next"]
            if not cur:
                return keys
        raise AssertionError("分页没有结束")
    expect_all = [f"note:{mine[2]}", f"note:{other[1]}", f"note:{mine[1]}", f"note:{other[0]}", f"note:{mine[0]}"]  # 精选时间倒序，失效/旧键被跳过
    assert walk(eng, 2) == expect_all
    assert walk(eng, 50) == expect_all
    assert walk(t1, 1) == expect_all and walk(t2, 2) == expect_all  # 全项目可见，翻页不漏不重
    # 旧的 honor 点赞/收藏键不报错：点赞报“动态不存在”
    assert t1.post("/api/feed/like", json={"key": "honor:1"}).status_code == 400
    assert all(p["ai_pick"]["reason"] == "测试理由" for p in _ai_posts(eng, limit=50)["posts"])
