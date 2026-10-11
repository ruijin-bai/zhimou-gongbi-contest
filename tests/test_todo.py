"""T05：各岗位首页统一待办队列（GET /api/todo）。

- 每个来源至少生成一条条目；
- 角色过滤：分包只见本单位，项目经理不出现日志预审/缺报/数据缺口/产值类条目，领导/管理员只读无待办；
- AI 候选隐患是待办来源之一（唯一展示位），确认后条目消失；扫码待认领与候选是同一件事时只出一条；
- 空库返回带按钮的引导，不是一片“暂无”。
T-06 待办收敛（D7、D19）：
- 项目经理只留赶工决定（crash）、升级到经理的高风险隐患（escalated）、周报发布、本人的例会行动项，其余合成一行“需要知道：N 项”；
- 赶工决定只给项目经理，就地完成、只留痕；“暂不处理”按失效条件重新出现（N-7）；
- 工程主管不再有“今日无记录”；周确认模式下预审指向周日志（F-02）；周确认提醒直达那一周（F-08）；隐蔽验收待录入结果与退回原因（F-04）；
- 分包安全员候选置顶、整改卡写责任人（U-03）；整改复核按 verify 契约写“AI 建议”（N-2）；系统结论的标签（AC-3.5）。
"""
import json
from contextlib import closing
from datetime import date, timedelta

import pytest
from conftest import PW, TODAY, jpeg, login, sample_xer
from test_roles import _add, _team

from app import db as dbmod
from app.services import brief
from app.services import findings as F
from app.services import quick as quick_svc
from app.services import todo as todo_svc

PM_BAD = brief._OUT_OF_SCOPE


def _get(c):
    r = c.get("/api/todo")
    assert r.status_code == 200, r.text
    return r.json()


def _src(d, source):
    return [i for i in d["items"] if i["source"] == source]


def _sources(d):
    return {i["source"] for i in d["items"]}


def _snap(c, color=(200, 50, 50)):
    c.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg(color), "image/jpeg"))])


def _finding(env, title, *, agent="safety", level="high", kind="hazard", assignee="tech1", due_days=3, status="open", confirmed=True):
    with closing(dbmod.connect()) as conn:
        fid = F.create(conn, env["project_id"], agent, kind, level, title, {"measure": "按方案整改", "category": "高处作业"},
                       assignee_id=env["users"][assignee] if assignee else None, due_days=due_days, confirmed_by=env["users"]["ss"] if confirmed else None,
                       candidate=not confirmed)
        if status != "open":
            conn.execute("UPDATE findings SET status=? WHERE id=?", (status, fid))
        return fid


@pytest.fixture()
def world(env):
    """甲/乙两个分包单位 + 港湾岗位 + P6 计划 + 各来源的原始记录。"""
    _team(env)
    _add(env, "lead", "leader", "公司领导")
    eng = login("eng")
    assert eng.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    assert eng.post("/api/quality/reminders").status_code == 200       # 质控提醒 → 技术员的“质控待执行”
    pid = env["project_id"]
    u = env["users"]
    _snap(login("tech1"))                                                # AI 候选隐患（甲单位）
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE findings SET level='high' WHERE confirmed_at IS NULL AND status='open'")
    with closing(dbmod.connect()) as conn:
        env["fid_cand"] = conn.execute("SELECT id FROM findings WHERE confirmed_at IS NULL AND status='open'").fetchone()["id"]
    env["fid_rect"] = _finding(env, "甲：临边防护缺失", assignee="tech1", due_days=3)                         # 待整改（技术员/安全员）
    env["fid_over"] = _finding(env, "甲：吊装警戒缺失", assignee="tech1", due_days=-2)                        # 超期督办
    env["fid_review"] = _finding(env, "乙：基坑防护已整改", assignee="tech2", status="rectifying")           # 整改待复核
    env["fid_unas"] = _finding(env, "未指派的高风险隐患", assignee=None)                                     # 未指派
    env["fid_q"] = _finding(env, "钢筋间距偏差", agent="quality", kind="reported", level="medium", assignee="tech1")   # 质量问题
    with closing(dbmod.connect()) as conn:
        now = dbmod.now_iso()
        ws = (TODAY - timedelta(days=TODAY.weekday())).isoformat()
        pws = (TODAY - timedelta(days=TODAY.weekday() + 7)).isoformat()
        base = {"project_id": pid, "mode": "ai", "started_at": now, "content_json": "{}", "checks_json": "[]"}
        env["log_hi"] = dbmod.insert(conn, "logs", {**base, "author_id": u["tech1"], "log_date": (TODAY - timedelta(days=1)).isoformat(), "status": "submitted", "period": "day",
                                                   "dq_json": json.dumps({"risk": "high", "score": 40, "summary": "照片与日志矛盾"})})
        env["log_lo"] = dbmod.insert(conn, "logs", {**base, "author_id": u["tech2"], "log_date": (TODAY - timedelta(days=1)).isoformat(), "status": "submitted", "period": "day",
                                                   "dq_json": json.dumps({"risk": "low", "score": 95, "summary": "正常"})})
        dbmod.insert(conn, "logs", {**base, "author_id": u["tech1"], "log_date": pws, "status": "draft", "period": "week", "period_end": (date.fromisoformat(pws) + timedelta(days=6)).isoformat()})
        bid = dbmod.insert(conn, "boq_items", {"project_id": pid, "code": "B1", "name": "承台混凝土", "unit": "m3", "unit_price": 100.0, "currency": "CNY"})
        dbmod.insert(conn, "boq_map", {"project_id": pid, "activity_code": "A1020", "boq_id": bid, "status": "suggested", "reason": "名称接近", "updated_at": now})
        dbmod.insert(conn, "reports", {"project_id": pid, "kind": "week", "period_start": ws, "period_end": TODAY.isoformat(), "facts_json": "{}", "narrative_json": "{}",
                                       "status": "draft", "created_by": u["eng"], "created_at": now})
        mid = dbmod.insert(conn, "meetings", {"project_id": pid, "title": "周例会", "meeting_date": TODAY.isoformat(), "created_by": u["eng"], "created_at": now, "confirmed_at": now})
        dbmod.insert(conn, "meeting_items", {"project_id": pid, "meeting_id": mid, "text": "补齐 3#墩 交底记录", "owner_id": u["tech1"], "due_date": (TODAY - timedelta(days=1)).isoformat(),
                                             "created_at": now})
        dbmod.insert(conn, "meeting_items", {"project_id": pid, "meeting_id": mid, "text": "约见业主协调征地", "owner_id": u["pm"], "due_date": (TODAY + timedelta(days=2)).isoformat(),
                                             "created_at": now})                       # 指派给项目经理本人的例会行动项（D19 保留）
    # 数据缺口：数据日期早于今天且 A1010 之后没有任何日报
    login("eng").post("/api/p6/import", files={"file": ("plan.xer", sample_xer(data_date=(TODAY - timedelta(days=5)).isoformat()), "application/octet-stream")})
    login("eng").post("/api/alerts/detect", json={"as_of": TODAY.isoformat()})
    from test_facts import _add_alert
    env["aid_open"] = _add_alert(pid, code="A1020", level="red", slip=8.0)                 # 红色进度预警（判定之后再写，避免被重新判定清掉）
    # 工程主管已确认属实的红色预警：项目经理据此做赶工决定（规则名与判定的指纹错开，避免撞上已有预警）
    env["aid_conf"] = _add_alert(pid, code="A2010", level="red", slip=6.0, status="confirmed", rule="risk_signal")
    # 扫码上报：没有候选隐患的一条（识别后未发现问题）
    with closing(dbmod.connect()) as conn:
        sa_a = conn.execute("SELECT * FROM users WHERE username='sa_a'").fetchone()
        qr = quick_svc.create_token(conn, sa_a, "3号墩承台", 30)
        t = conn.execute("SELECT * FROM quick_tokens WHERE id=?", (qr["id"],)).fetchone()
        quick_svc.LIMITER.reset()          # 进程内按来源地址限流：用到本夹具的用例多于每小时上限，每次造数前清零（同 test_quick）
        quick_svc.submit(conn, t, [jpeg((10, 120, 200))], "老王", "钢筋班", "围挡缺口", "cid-1", "1.2.3.4")
    return env


# ----------------------------------------------------------------------------- 每个来源至少一条

def test_every_source_generates_items(world):
    seen: dict[str, set] = {}
    for u in ("tech1", "sa_a", "eng", "ss", "pm"):
        seen[u] = _sources(_get(login(u)))
    allsrc = set().union(*seen.values())
    expected = {"candidate", "rectify", "qc", "my_finding", "week", "gate", "meeting", "review", "overdue", "unassigned", "quick", "alert", "crash", "escalated",
                "quality", "log_review", "log_low", "week_missing", "data_gap", "mapping", "report"}
    missing = expected - allsrc
    # my_finding 只在派给本人的非隐患非质控事项时出现（质量问题 reported 种类也会出）；允许它缺席，其余来源必须有
    missing.discard("my_finding")
    assert not missing, f"这些来源没有生成条目：{sorted(missing)}；各角色：{ {k: sorted(v) for k, v in seen.items()} }"


def test_item_fields_complete_and_routes_valid(world):
    for u in ("tech1", "sa_a", "eng", "ss", "pm"):
        d = _get(login(u))
        assert d["items"], u
        for i in d["items"]:
            assert {"id", "module", "title", "reason", "status", "due", "action_label", "action_route", "overdue"} <= set(i)
            assert i["module"] in {"site", "progress", "safety", "quality", "report"}
        assert len({i["id"] for i in d["items"]}) == len(d["items"])          # 条目 id 唯一
        assert d["tabs"]["all"] == d["total"] and sum(d["tabs"][k] for k in ("site", "progress", "safety", "quality", "report")) == len(d["items"])
        assert d["overview"] and len(d["overview"]) == 4
        assert d["boundary"].startswith("AI 只整理线索")
    d = _get(login("tech1"))
    due = [i["due"] for i in d["items"] if not i["overdue"] and i["due"]]
    assert all(x >= TODAY.isoformat() for x in due)                            # 未超期的期限都不早于今天
    over = [i for i in d["items"] if i["overdue"]]
    first_non_over = next((n for n, i in enumerate(d["items"]) if not i["overdue"]), len(d["items"]))
    assert over and [i["overdue"] for i in d["items"][:first_non_over]] == [True] * first_non_over   # 超期置顶


# ----------------------------------------------------------------------------- 角色过滤

def test_sub_company_isolation(world):
    a, b = _get(login("sa_a")), _get(login("sa_b"))
    ta, tb = _get(login("tech1")), _get(login("tech2"))
    assert world["fid_cand"] in {i["finding"]["id"] for i in _src(a, "candidate")}        # 甲单位候选只给甲单位安全员
    assert not _src(b, "candidate")
    titles_b = " ".join(i["title"] for i in b["items"] + tb["items"])
    assert "甲：" not in titles_b and "3#墩" not in " ".join(i["title"] for i in tb["items"] if i["source"] != "gate")
    assert {i["finding"]["id"] for i in _src(a, "rectify")} == {world["fid_rect"], world["fid_over"]}
    assert all(i["source"] in ("rectify", "qc", "my_finding", "week", "gate", "meeting", "ticket_pending", "conceal_todo") for i in ta["items"])
    assert not _src(b, "rectify") and not _src(b, "quick")                                # 乙单位看不到甲单位的整改、甲单位生成的二维码上报
    # 技术员乙：只有自己的整改（没有甲的），且分包单位绝不出现港湾侧来源
    for d in (ta, tb, a, b):
        assert not (_sources(d) & {"alert", "log_review", "log_missing", "week_missing", "data_gap", "mapping", "report", "review", "overdue", "unassigned", "decision"})
    for i in tb["items"]:
        assert "吊装警戒" not in i["title"] and "临边防护" not in i["title"]


def test_pm_has_no_log_review_gap_or_output_items(world):
    d = _get(login("pm"))
    assert d["items"]
    assert not (_sources(d) & todo_svc.PM_FORBIDDEN)
    for i in d["items"]:
        assert not PM_BAD.search(f"{i['title']} {i['reason']} {i['status']}"), i
    assert not any("预审" in i["title"] or "缺口" in i["status"] or "缺报" in i["status"] for i in d["items"])
    # 项目经理只看拍板项：已确认红色预警的赶工决定、升级到经理的高风险隐患、周报发布、本人的例会行动项
    assert {"crash", "escalated", "report", "meeting"} <= _sources(d)
    # 概览条与“智能体今日处理”也不带日志/产值
    assert not any(PM_BAD.search(f"{o['label']} {o.get('sub', '')}") for o in d["overview"])
    assert all(a["key"] in todo_svc.PM_AGENTS for a in d["did"])
    # 工程主管同一项目里确实有这些条目（说明被过滤的是角色，不是数据没造出来）
    e = _get(login("eng"))
    assert {"log_review", "week_missing", "data_gap", "mapping"} <= _sources(e)


# ----------------------------------------------------------------------------- T-06 项目经理待办收敛（AC-6.1～6.3、6.5）

PM_SOURCES = {"crash", "escalated", "report", "meeting", "claim_notice"}   # claim_notice：索赔通知时效剩 3 天升级到项目经理（用户 10-08 定）


def test_pm_items_whitelist_and_need_to_know_line(world):
    """AC-6.1/6.2/6.3：项目经理待办只有三类拍板项与本人例会行动项；其余合成一行“需要知道：N 项”。"""
    d = _get(login("pm"))
    srcs = {s.replace("_more", "") for s in _sources(d)}
    assert srcs <= PM_SOURCES, srcs - PM_SOURCES
    assert todo_svc.PM_ALLOWED == frozenset(PM_SOURCES)
    for bad in ("candidate", "gate", "gate_more", "quick", "ticket_risk", "ticket_pending", "conceal_gap", "conceal_todo", "alert", "finding",
                "log_review", "log_low", "log_missing", "week_missing", "data_gap", "mapping"):
        assert bad not in _sources(d), bad
    # 本人的例会行动项保留，别人的不进
    meet = [i["title"] for i in _src(d, "meeting")]
    assert meet == ["约见业主协调征地"]
    # 只有一张周报草稿：发布是项目经理的事
    assert _src(d, "report")[0]["action_label"] == "去审核"
    k = d["know"]
    assert k and k["n"] == sum(p["n"] for p in k["parts"]) and k["n"] >= 2
    routes = {p["route"] for p in k["parts"]}
    assert "progress?tab=alerts" in routes and "safety" in routes
    text = "需要知道 " + " ".join(p["label"] for p in k["parts"])
    assert brief.scope_ok(text) and not PM_BAD.search(text)
    for name in ("张工", "李工", "安全员甲", "安全员乙"):                       # 不出现人名（岗位名不算人名）
        assert name not in text, name
    assert all(p["source"] not in todo_svc.PM_FORBIDDEN for p in k["parts"])
    # 只有项目经理有这一行
    for u in ("eng", "ss", "tech1", "sa_a"):
        assert _get(login(u)).get("know") is None, u


def test_pm_overview_counts_only_decisions(world):
    """U-13③：概览“待我拍板”只数真正要项目经理办的件数；红橙预警数与“需要知道”里的预警数是同一个数。"""
    d = _get(login("pm"))
    labels = [o["label"] for o in d["overview"]]
    assert labels == ["今日新增问题", "待我拍板", "超期隐患", "待确认红橙预警"]
    assert d["overview"][1]["value"] == d["total"]
    alerts_part = next(p for p in d["know"]["parts"] if p["route"] == "progress?tab=alerts")
    assert d["overview"][3]["value"] == alerts_part["n"]


def test_overview_ledger_numbers_match_brief_signature(world):
    """D-1（AC-2 同屏同一指标只有一个数）：概览条里的台账数与简报计数签名（refs.counts）同一来源、同一口径；
    数待办条目的格子叫“待处理/待我拍板/紧急待办”，与“超期隐患/高风险隐患”区分开。"""
    with closing(dbmod.connect()) as conn:
        sig = brief.count_sig({"day": TODAY.isoformat(), **brief.counts(conn, world["project_id"], TODAY)})
    assert sig["超期隐患"] >= 1 and sig["高风险隐患"] >= 1
    for u in ("pm", "ss"):
        o = {x["label"]: x for x in _get(login(u))["overview"]}
        assert o["超期隐患"]["value"] == sig["超期隐患"] and o["超期隐患"]["sub"] == f"高风险隐患 {sig['高风险隐患']}", u
        assert "超期/高风险" not in o and "紧急待办" not in o
    pm = {x["label"]: x for x in _get(login("pm"))["overview"]}
    assert pm["待确认红橙预警"]["value"] == sig["红色预警"] + sig["橙色预警"]
    e = {x["label"]: x for x in _get(login("eng"))["overview"]}
    assert e["超期质量问题"]["value"] == sig["超期质量问题"] and e["超期质量问题"]["sub"] == f"质量问题未闭环 {sig['质量问题未闭环']}"
    # 简报里写的数也是这一套：生成时记下的签名与概览条同源
    with closing(dbmod.connect()) as conn:
        k = brief.kpis(conn, world["project_id"], TODAY)
    assert brief.count_sig(k)["超期隐患"] == sig["超期隐患"] and brief.count_sig(k)["高风险隐患"] == sig["高风险隐患"]
    # 数待办条目的格子只在一线岗位，叫“紧急待办”，和待办表的同名筛选是同一个数
    t = _get(login("tech1"))
    tile = next(x for x in t["overview"] if x.get("filter") == "urgent")
    assert tile["label"] == "紧急待办" and tile["value"] == sum(1 for i in t["items"] if i["overdue"] or i["tone"] == "red")


def test_pm_alerts_only_confirmed_red_become_crash_items(world):
    """AC-6.5：只有工程主管已确认属实的红色预警进项目经理待办；未确认的红、橙预警只计入“需要知道”。"""
    from test_facts import _add_alert
    pid = world["project_id"]
    _add_alert(pid, code="A3010", level="orange", slip=3.0, status="confirmed", rule="risk_signal")   # 已确认的橙色：不进待办
    _add_alert(pid, code="A4010", level="orange", slip=2.0, status="open", rule="risk_signal")         # 未确认的橙色：只计入
    d = _get(login("pm"))
    crash = _src(d, "crash")
    assert [i["id"] for i in crash] == [f"crash:{world['aid_conf']}"]
    assert crash[0]["tone"] == "red" and crash[0]["action_route"] == f"alert?id={world['aid_conf']}"
    alerts_part = next(p for p in d["know"]["parts"] if p["route"] == "progress?tab=alerts")
    with closing(dbmod.connect()) as conn:
        n_open = conn.execute("SELECT COUNT(*) n FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' AND level IN ('red','orange')", (pid,)).fetchone()["n"]
    assert alerts_part["n"] == n_open >= 2
    # 工程主管照常做“确认属实 / 误报”反馈；确认后，项目经理才看到赶工决定
    eng = login("eng")
    assert any(i["id"] == f"alert:{world['aid_open']}" for i in _src(_get(eng), "alert"))
    assert eng.post(f"/api/alerts/{world['aid_open']}/feedback", json={"status": "confirmed", "note": ""}).status_code == 200
    d2 = _get(login("pm"))
    assert f"crash:{world['aid_open']}" in {i["id"] for i in _src(d2, "crash")}
    assert next(p for p in d2["know"]["parts"] if p["route"] == "progress?tab=alerts")["n"] == n_open - 1


def test_pm_escalated_hazards(world):
    """升级到经理的高风险隐患：催办链阶段 4 已发给项目经理的未闭环高风险，或已超期的高风险；其余高风险只计入“需要知道”。"""
    pid, u = world["project_id"], world["users"]
    d = _get(login("pm"))
    esc = {i["finding"]["id"]: i for i in _src(d, "escalated")}
    assert world["fid_over"] in esc                                         # 高风险且已超期
    assert world["fid_rect"] not in esc                                     # 高风险但未超期、未升级：只计入“需要知道”
    it = esc[world["fid_over"]]
    assert it["due"] and it["overdue"] and it["action_route"] == f"finding?id={world['fid_over']}"
    assert "建议：建议" not in it["reason"]                                    # U-13②
    with closing(dbmod.connect()) as conn:                                   # 责任人所属单位：经理首页按单位归成协调事项（只归类，不派单）
        unit = conn.execute("SELECT u.company FROM findings f JOIN users u ON u.id=f.assignee_id WHERE f.id=?", (world["fid_over"],)).fetchone()
    assert it["company"] == ((unit["company"] if unit else "") or "").strip()
    safety_part = next(p for p in d["know"]["parts"] if p["route"] == "safety")
    assert safety_part["n"] >= 1
    # 催办链阶段 4 已发给项目经理：未超期的高风险也算“已升级”
    with closing(dbmod.connect()) as conn:
        dbmod.insert(conn, "nudge_log", {"project_id": pid, "obj_type": "finding", "obj_key": str(world["fid_rect"]), "stage": 4, "to_user_id": u["pm"],
                                         "status": "sent", "subject": "甲：临边防护缺失", "status_text": "", "area": "safety", "sent_at": dbmod.now_iso()})
        # 只认领、未发出的不算（T-01 的 claimed 语义）
        dbmod.insert(conn, "nudge_log", {"project_id": pid, "obj_type": "finding", "obj_key": str(world["fid_unas"]), "stage": 4, "to_user_id": u["pm"],
                                         "status": "claimed", "subject": "未指派", "status_text": "", "area": "safety", "sent_at": dbmod.now_iso()})
    d2 = _get(login("pm"))
    esc2 = {i["finding"]["id"]: i for i in _src(d2, "escalated")}
    assert world["fid_rect"] in esc2 and world["fid_unas"] not in esc2
    assert esc2[world["fid_rect"]]["requested"] and not esc2[world["fid_over"]]["requested"]   # 催办链确有提请 → 经理首页“待协调”；只是超期 → “需关注”
    assert next(p for p in d2["know"]["parts"] if p["route"] == "safety")["n"] == safety_part["n"] - 1
    # 闭环后不再出现
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE findings SET status='resolved' WHERE id=?", (world["fid_rect"],))
    assert world["fid_rect"] not in {i["finding"]["id"] for i in _src(_get(login("pm")), "escalated")}



def test_pm_items_ai_tags(world):
    """项目经理拍板项的小标签：赶工决定是“AI 分析”（预警由规则判定、经人确认，AI 只分析原因与提方案），升级隐患是“AI 发现”；周报、例会不变。"""
    d = _get(login("pm"))
    assert {i["ai_tag"] for i in _src(d, "crash")} == {"AI 分析"}
    assert {i["ai_tag"] for i in _src(d, "escalated")} == {"AI 发现"}
    assert {i["ai_tag"] for i in _src(d, "report")} == {"AI 起草"} and all(i["ai_tag"] is None for i in _src(d, "meeting"))
    assert {i["ai_tag"] for i in _src(_get(login("ss")), "candidate")} == {"AI 发现"}

# ----------------------------------------------------------------------------- T-06 赶工决定（AC-6.6）

def _decide(c, aid, decision, **kw):
    return c.post(f"/api/todo/alerts/{aid}/crash-decision", json={"decision": decision, **kw})


def _crash(d, aid):
    return next((i for i in _src(d, "crash") if i["id"] == f"crash:{aid}"), None)


def test_crash_decision_only_manager_and_needs_plan_to_adopt(world):
    aid = world["aid_conf"]
    for u in ("eng", "ss", "tech1", "sa_a", "admin", "lead"):
        assert _decide(login(u), aid, "defer").status_code == 403, u
    pm = login("pm")
    it = _crash(_get(pm), aid)
    assert "尚无赶工方案" in it["reason"]
    gen = [q for q in it["quick"] if q["label"] == "生成赶工方案"]
    assert gen and gen[0]["api"]["url"] == "/api/schedule/recovery"
    assert not [q for q in it["quick"] if q["label"] == "采纳赶工方案"]
    assert _decide(pm, aid, "adopt").status_code == 409                        # 没有方案不能“采纳”
    assert _decide(pm, aid, "maybe").status_code == 400
    assert _decide(pm, world["aid_open"], "defer").status_code == 409          # 未经工程主管确认的预警不做赶工决定
    assert _decide(pm, 999999, "defer").status_code == 404
    # 生成赶工方案（既有接口）后可以采纳
    assert pm.post("/api/schedule/recovery").status_code == 200
    it = _crash(_get(pm), aid)
    assert "尚无赶工方案" not in it["reason"] and "赶工方案" in it["reason"]
    assert [q["label"] for q in it["quick"]] == ["采纳赶工方案", "暂不处理"]
    assert any(x["route"] == "progress?tab=forecast" for x in it.get("links") or [])


def test_crash_adopt_only_records_and_item_disappears(world):
    aid, pm = world["aid_conf"], login("pm")
    assert pm.post("/api/schedule/recovery").status_code == 200
    with closing(dbmod.connect()) as conn:
        n_notif = conn.execute("SELECT COUNT(*) n FROM notifications").fetchone()["n"]
        n_find = conn.execute("SELECT COUNT(*) n FROM findings").fetchone()["n"]
    r = _decide(pm, aid, "adopt", note="按方案加开一个工作面")
    assert r.status_code == 200, r.text
    assert _crash(_get(pm), aid) is None                                      # 就地完成，条目消失
    with closing(dbmod.connect()) as conn:
        row = conn.execute("SELECT * FROM alert_decisions WHERE alert_id=?", (aid,)).fetchone()
        assert row["decision"] == "adopt" and row["level_at"] == "red" and row["slip_at"] == 6.0 and row["decided_at"] and row["decided_by"] == world["users"]["pm"]
        assert conn.execute("SELECT COUNT(*) n FROM audit WHERE action='alert.crash' AND target=?", (f"alert:{aid}",)).fetchone()["n"] == 1
        # 只留痕，不派单、不发通知
        assert conn.execute("SELECT COUNT(*) n FROM notifications").fetchone()["n"] == n_notif
        assert conn.execute("SELECT COUNT(*) n FROM findings").fetchone()["n"] == n_find
        assert conn.execute("SELECT status FROM alerts WHERE id=?", (aid,)).fetchone()["status"] == "confirmed"
    # 采纳之后预警继续升级（等级高于决定时）：重新出现
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE alert_decisions SET level_at='orange' WHERE alert_id=?", (aid,))
    it = _crash(_get(pm), aid)
    assert it and it["status"].startswith("上次已采纳")
    # 采纳之后单是滞后天数增加：不重新出现（只有“暂不处理”按天数失效）
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE alert_decisions SET level_at='red', slip_at=1.0 WHERE alert_id=?", (aid,))
    assert _crash(_get(pm), aid) is None


def _defer_then(world, sql: str, params=()):
    aid, pm = world["aid_conf"], login("pm")
    assert _decide(pm, aid, "defer").status_code == 200
    assert _crash(_get(pm), aid) is None
    with closing(dbmod.connect()) as conn:
        conn.execute(sql, params)
    return _crash(_get(pm), aid)


def test_crash_defer_reappears_when_slip_grows_3_days(world):
    from test_facts import _bump
    aid = world["aid_conf"]
    it = _defer_then(world, "SELECT 1")
    assert it is None
    _bump(aid, level="red", slip=8.0)                       # 多 2 天：仍不出现
    assert _crash(_get(login("pm")), aid) is None
    _bump(aid, level="red", slip=9.0)                       # 比决定时多 3 天：重新出现，注明上次暂不处理
    it = _crash(_get(login("pm")), aid)
    md = TODAY.isoformat()[5:]
    assert it and it["status"] == f"上次暂不处理（{md}）" and "3" in it["reason"]


def test_crash_defer_reappears_after_7_days(world):
    old = (TODAY - timedelta(days=7)).isoformat() + "T09:00:00+08:00"
    it = _defer_then(world, "UPDATE alert_decisions SET decided_at=?", (old,))
    assert it and it["status"] == f"上次暂不处理（{old[5:10]}）"
    six = (TODAY - timedelta(days=6)).isoformat() + "T09:00:00+08:00"
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE alert_decisions SET decided_at=?", (six,))
    assert _crash(_get(login("pm")), world["aid_conf"]) is None


def test_crash_defer_reappears_when_level_rises_or_alert_reopened(world):
    it = _defer_then(world, "UPDATE alert_decisions SET level_at='orange'")
    assert it and it["status"].startswith("上次暂不处理")
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE alert_decisions SET level_at='red'")
    assert _crash(_get(login("pm")), world["aid_conf"]) is None
    # 预警被关闭后又重开、由工程主管重新确认属实：重新出现
    eng = login("eng")
    aid = world["aid_conf"]
    assert eng.post(f"/api/alerts/{aid}/feedback", json={"status": "closed", "note": ""}).status_code == 200
    assert _crash(_get(login("pm")), aid) is None
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE alerts SET status='open' WHERE id=?", (aid,))           # 判定重开
    assert _crash(_get(login("pm")), aid) is None                                      # 重开后先等工程主管确认
    assert eng.post(f"/api/alerts/{aid}/feedback", json={"status": "confirmed", "note": ""}).status_code == 200
    it = _crash(_get(login("pm")), aid)
    assert it and it["status"].startswith("上次暂不处理")


# ----------------------------------------------------------------------------- T-06 工程主管（AC-6.4、6.8、6.9、6.10）

def test_eng_has_no_today_missing_but_keeps_last_week(world):
    """AC-6.4：工程主管首页没有“今日无记录”，“上周未确认”保留，且一键提醒直达那一周（F-08）。"""
    d = _get(login("eng"))
    assert not {"log_missing", "log_missing_more"} & _sources(d)
    assert not any("今日无" in i["title"] or "今日无" in i["status"] for i in d["items"])
    wk = _src(d, "week_missing")
    assert wk and all(q["api"]["url"] == "/api/todo/remind-week" for i in wk for q in i["quick"])
    lws = (TODAY - timedelta(days=TODAY.weekday() + 7)).isoformat()
    assert all(i["quick"][0]["api"]["body"]["week"] == lws for i in wk)
    assert "log_missing" in todo_svc.PM_FORBIDDEN                           # 口径守卫不放松


def test_remind_week_links_to_that_week(world):
    """AC-6.9：提醒落在那一周的周确认；只给工程主管；只发给技术员；写审计。"""
    u = world["users"]
    ws = (TODAY - timedelta(days=TODAY.weekday() + 14)).isoformat()
    for who in ("pm", "ss", "tech1", "sa_a"):
        assert login(who).post("/api/todo/remind-week", json={"user_id": u["tech2"], "week": ws}).status_code == 403, who
    eng = login("eng")
    assert eng.post("/api/todo/remind-week", json={"user_id": u["sa_a"], "week": ws}).status_code == 400   # 只提醒技术员
    mid = (date.fromisoformat(ws) + timedelta(days=3)).isoformat()
    r = eng.post("/api/todo/remind-week", json={"user_id": u["tech2"], "week": mid})                         # 周中任意一天按所在周算
    assert r.status_code == 200, r.text
    n = [x for x in login("tech2").get("/api/notifications").json() if "周" in x["title"]][0]
    assert n["link"] == {"route": "week", "params": {"date": ws}}
    assert ws[5:] in n["body"]
    with closing(dbmod.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM audit WHERE action='user.nudge' AND target=?", (f"user:{u['tech2']}",)).fetchone()["n"] == 1
        dbmod.insert(conn, "logs", {"project_id": world["project_id"], "mode": "ai", "started_at": dbmod.now_iso(), "content_json": "{}", "checks_json": "[]",
                                    "author_id": u["tech2"], "log_date": ws, "status": "submitted", "period": "week"})
    assert eng.post("/api/todo/remind-week", json={"user_id": u["tech2"], "week": ws}).status_code == 409   # 那一周已提交，不再提醒


def _week_log(env, uid, ws, status, note=""):
    with closing(dbmod.connect()) as conn:
        return dbmod.insert(conn, "logs", {"project_id": env["project_id"], "mode": "ai", "started_at": dbmod.now_iso(), "content_json": "{}", "checks_json": "[]",
                                           "author_id": uid, "log_date": ws, "status": status, "period": "week", "review_note": note,
                                           "period_end": (date.fromisoformat(ws) + timedelta(days=6)).isoformat()})


def test_tech_lists_all_returned_or_unsubmitted_past_weeks(world):
    """AC-6.9：技术员待办列出所有被退回或未提交的往期周确认（不只看上一周），各自直达那一周。"""
    u = world["users"]
    ws = TODAY - timedelta(days=TODAY.weekday())
    w3, w4 = (ws - timedelta(days=21)).isoformat(), (ws - timedelta(days=28)).isoformat()
    _week_log(world, u["tech1"], w3, "returned", "桩长与记录不符")
    _week_log(world, u["tech1"], w4, "approved")
    wk = {i["action_route"]: i for i in _src(_get(login("tech1")), "week")}
    pws = (ws - timedelta(days=7)).isoformat()
    assert f"week?date={pws}" in wk and f"week?date={w3}" in wk and f"week?date={w4}" not in wk
    assert "桩长与记录不符" in wk[f"week?date={w3}"]["reason"] and wk[f"week?date={w3}"]["status"] == "被退回"
    assert all(i["overdue"] for i in wk.values())


def test_tech_week_missing_row_counts_in_weekly_mode(world):
    """周确认模式下，上周连周日志行都没有也算未提交（原先 prev 为空就漏掉）。"""
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE projects SET config_json='{\"cadence\": \"week\"}' WHERE id=?", (world["project_id"],))
        conn.execute("UPDATE users SET created_at='2020-01-01T00:00:00+08:00' WHERE username='tech2'")
    pws = (TODAY - timedelta(days=TODAY.weekday() + 7)).isoformat()
    wk = _src(_get(login("tech2")), "week")
    assert [i["action_route"] for i in wk] == [f"week?date={pws}"]
    # 新开的账号不追之前的周
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE users SET created_at=? WHERE username='tech2'", (dbmod.now_iso(),))
    assert not _src(_get(login("tech2")), "week")


def test_weekly_mode_pre_review_points_to_week_log(world):
    """AC-6.8（F-02）：周确认模式下，工程主管待办不列日日志“去预审”；预审条目指向周日志，打开即有审核操作。"""
    u = world["users"]
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE projects SET config_json='{\"cadence\": \"week\"}' WHERE id=?", (world["project_id"],))
    pws = (TODAY - timedelta(days=TODAY.weekday() + 7)).isoformat()
    wid = _week_log(world, u["tech2"], pws, "submitted")
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE logs SET dq_json=? WHERE id=?", (json.dumps({"risk": "medium", "score": 70, "summary": "两处待核实", "checks": [
            {"level": "warn", "message": "桩长与计划不符", "source": "规则"}]}), wid))
    eng = login("eng")
    d = _get(eng)
    rv = _src(d, "log_review")
    assert [i["action_route"] for i in rv] == [f"log?id={wid}"]                     # 日日志 log_hi 不再列出
    assert "周" in rv[0]["title"] and rv[0]["status"] == "待核实"
    assert not any(i["id"] == f"log_review:{world['log_hi']}" for i in d["items"])
    lg = eng.get(f"/api/logs/{wid}").json()
    assert lg["dq_review"] and lg["dq_review"]["reviewable"] is True and lg["dq_review"]["rows"]
    # 低风险周日志照样可一键通过
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE logs SET dq_json=? WHERE id=?", (json.dumps({"risk": "low", "score": 95, "summary": "正常", "checks": []}), wid))
    lo = _src(_get(eng), "log_low")
    assert lo and lo[0]["quick"][0]["api"]["body"]["ids"] == [wid]
    # 按日提交的项目照旧列日日志
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE projects SET config_json='{\"cadence\": \"day\"}' WHERE id=?", (world["project_id"],))
    assert any(i["action_route"] == f"log?id={world['log_hi']}" for i in _src(_get(eng), "log_review"))


def _conceal(env, status, by="tech1", note=""):
    with closing(dbmod.connect()) as conn:
        return dbmod.insert(conn, "conceal_records", {"project_id": env["project_id"], "part": "3#墩", "template_key": "rebar", "title": "3#墩承台钢筋",
                                                      "status": status, "inspection_date": TODAY.isoformat(), "created_by": env["users"][by],
                                                      "created_at": dbmod.now_iso(), "submitted_at": dbmod.now_iso() if status != "draft" else None,
                                                      "supervisor_name": "王监理", "result_note": note})


def test_conceal_submitted_and_rejected_in_todo(world):
    """AC-6.10（F-04）：工程主管待办有“已报验，待录入监理结果”；技术员的退回条目带退回原因；项目经理都不出现。"""
    sub = _conceal(world, "submitted")
    rej = _conceal(world, "rejected", note="保护层垫块不足")
    _conceal(world, "draft")
    e = _get(login("eng"))
    res = _src(e, "conceal_result")
    assert [i["action_route"] for i in res] == [f"conceal?id={sub}"]
    assert "已报验" in res[0]["reason"] and res[0]["status"] == "待录入监理结果"
    t = _get(login("tech1"))
    back = [i for i in _src(t, "conceal_todo") if i["action_route"] == f"conceal?id={rej}"]
    assert back and "保护层垫块不足" in back[0]["reason"] and back[0]["status"] == "监理退回"
    assert any("草稿" in i["reason"] or "待补" in i["title"] for i in _src(t, "conceal_todo") if i["action_route"] == "conceal")
    assert not {"conceal_result", "conceal_todo", "conceal_gap"} & _sources(_get(login("pm")))
    assert "conceal_result" in todo_svc.PM_FORBIDDEN


# ----------------------------------------------------------------------------- T-06 分包安全员（AC-6.11）、复核文案（N-2）、候选分组（T-05 契约）

def test_sub_safety_candidates_pinned_and_rectify_names_assignee(world):
    d = _get(login("sa_a"))
    first = d["items"][0]
    assert first["source"] == "candidate"                                       # 候选置顶，不被超期整改挤到折叠区
    rect = {i["finding"]["id"]: i for i in _src(d, "rectify")}
    assert "责任人 张工" in rect[world["fid_rect"]]["reason"] and "责任人 张工" in rect[world["fid_over"]]["reason"]
    # 技术员看自己的整改不必写自己
    t = {i["finding"]["id"]: i for i in _src(_get(login("tech1")), "rectify")}
    assert "责任人" not in t[world["fid_rect"]]["reason"]


def test_candidate_items_carry_group_key_and_default_assignee(world):
    c = _src(_get(login("sa_a")), "candidate")[0]
    with closing(dbmod.connect()) as conn:
        ref = conn.execute("SELECT ref_type, ref_id FROM findings WHERE id=?", (world["fid_cand"],)).fetchone()
    assert c["group_key"] == f"{ref['ref_type']}:{ref['ref_id']}" and c["group_key"].startswith("media:")
    assert "default_assignee_id" in c


def _review_reason(world, verify: dict | None) -> dict:
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE findings SET verify_json=? WHERE id=?", (json.dumps(verify) if verify is not None else None, world["fid_review"]))
    return _src(_get(login("ss")), "review")[0]


def test_review_item_uses_ai_suggestion_wording(world):
    """N-2：隐患闭环之前待办里不出现“已确认消除/已消除”这类肯定说法，一律写“AI 建议：……”。"""
    from datetime import datetime
    old = (datetime.now().astimezone() - timedelta(minutes=20)).isoformat(timespec="seconds")
    new = (datetime.now().astimezone() - timedelta(minutes=2)).isoformat(timespec="seconds")
    cases = [({"resolved": True, "reason": "防护已恢复"}, "AI 建议：已消除，待安全主管确认", False),
             ({"state": "done", "suggest": "resolved", "resolved": True, "reason": "x"}, "AI 建议：已消除，待安全主管确认", False),
             ({"state": "done", "suggest": "not_resolved", "resolved": False, "remaining": "临边缺口"}, "AI 建议：仍存在临边缺口", False),
             ({"resolved": False, "reason": "仍缺"}, "AI 建议：仍存在", False),
             ({"state": "pending"}, "AI 复核中", False),
             ({"state": "running", "started_at": new}, "AI 复核中", False),
             ({"state": "running", "started_at": old}, "AI 复核未完成，可人工复核", True),
             ({"state": "failed"}, "AI 复核失败，待人工复核", True),
             ({"resolved": None, "reason": "AI复核失败"}, "AI 复核失败，待人工复核", False)]     # 旧记录没有 state：与 reverify 接口同口径，不给按钮
    for v, want, rev in cases:
        it = _review_reason(world, v)
        assert want in it["reason"] and it["detail"]["verify_text"].startswith(want), (v, it["reason"])
        stripped = it["reason"].replace("AI 建议：已消除", "")
        assert "已消除" not in stripped and "已确认消除" not in it["reason"], it["reason"]
        assert it["detail"]["reverify"] is rev, v
        assert [q["label"] for q in it["quick"]] == ["确认闭环", "退回重改"]          # 人工随时可以闭环或退回
    # 与 T-13 的统一口径一致；拍摄时间标记一并写出（AC-F5）
    from app.services import site_agents
    v = {"state": "done", "suggest": "resolved", "resolved": True, "reason": "x", "photo_check": "unknown"}
    it = _review_reason(world, v)
    assert it["detail"]["verify_text"] == site_agents.verify_label(v, "rectifying") and "拍摄时间未知" in it["reason"]


def test_brief_system_source_label_reaches_todo(env):
    """AC-3.5：简报结论已失效改由系统生成时，首页接口带 source=system 与失效原因，前端据此换标签。"""
    from test_brief_headline import FACTS, _put_brief
    from test_facts import _add_alert, _setup_site
    _setup_site(env)
    aid = _add_alert(env["project_id"])
    _put_brief(env["project_id"], "A1020 滞后 8 天需赶工", FACTS)
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE alerts SET status='closed' WHERE id=?", (aid,))
    for u in ("pm",):
        att = _get(login(u))["attention"]
        assert att["source"] == "system" and att["note"] and "3#墩承台钢筋绑扎" in att["note"]
    _put_brief(env["project_id"], "质量问题已在整改", FACTS)
    att = _get(login("pm"))["attention"]
    assert att["source"] == "ai" and not att["note"]


def test_safety_sup_conclusion_is_safety_and_points_not_repeated(env):
    """U-11：安全主管的结论取简报里的安全条目；S-9①：要点区不再重复结论那一句。"""
    from test_brief_headline import FACTS, _put_brief
    from test_facts import _setup_site
    _setup_site(env)
    items = [{"level": "high", "area": "进度", "title": "A1020 滞后", "detail": "滞后 8 天", "action": "赶工"},
             {"level": "medium", "area": "质量", "title": "钢筋间距待复核", "detail": "3#墩承台", "action": "整改"},
             {"level": "medium", "area": "安全", "title": "高处作业防护待加强", "detail": "临边防护松动", "action": "督促整改"}]
    _put_brief(env["project_id"], "A1020 滞后 8 天需赶工", FACTS, items=items)
    ss = _get(login("ss"))["attention"]
    assert ss["headline"].startswith("高处作业防护待加强") and "临边防护松动" in ss["headline"]
    assert "高处作业防护待加强" not in [i["title"] for i in ss["items"]] and ss["merged"] is True   # 前端据此写“简报要点已写在上方结论里”
    eng = _get(login("eng"))["attention"]
    assert eng["headline"] == "3#墩承台钢筋绑扎 滞后 8 天需赶工"
    pm = _get(login("pm"))["attention"]
    assert pm["headline"] == "3#墩承台钢筋绑扎 滞后 8 天需赶工" and [i["area"] for i in pm["items"]] == ["进度", "质量", "安全"]
    # 要点第一条与结论同一句：剔除
    items2 = [{"level": "high", "area": "进度", "title": "A1020 滞后 8 天需赶工", "detail": "滞后 8 天", "action": "赶工"}] + items[1:]
    _put_brief(env["project_id"], "A1020 滞后 8 天需赶工", FACTS, items=items2)
    pm = _get(login("pm"))["attention"]
    assert "A1020 滞后 8 天需赶工" not in [i["title"] for i in pm["items"]]


def test_pm_attention_only_progress_quality_safety(env, monkeypatch):
    with closing(dbmod.connect()) as conn:
        conn.execute("INSERT INTO briefs (project_id, day, content_json, created_at) VALUES (?,?,?,?)",
                     (env["project_id"], date.today().isoformat(), json.dumps({"headline": "进度预警 红1", "items": [
                         {"level": "high", "area": "进度", "title": "A1020 滞后", "detail": "滞后 8 天", "action": "赶工"},
                         {"level": "medium", "area": "产值", "title": "产值超计划", "detail": "x", "action": "x"},
                         {"level": "medium", "area": "质量", "title": "现场记录填报率不足", "detail": "x", "action": "x"},
                         {"level": "medium", "area": "安全", "title": "2项隐患", "detail": "x", "action": "督促"},
                         {"level": "info", "area": "质量", "title": "质量问题 1 项", "detail": "x", "action": "督促"}]}), dbmod.now_iso()))
    d = _get(login("pm"))
    att = d["attention"]
    assert att["enabled"] and not att["missing"] and len(att["items"]) <= 3
    assert [i["area"] for i in att["items"]] == ["进度", "安全", "质量"]            # 越界条目已丢弃，其余保持简报顺序
    assert all(i["locate"] for i in att["items"])
    # 没有今日简报：给生成入口（前端按钮），不编造
    with closing(dbmod.connect()) as conn:
        conn.execute("DELETE FROM briefs")
    att = _get(login("pm"))["attention"]
    assert att["enabled"] and att["missing"] and att["items"] == []


def test_readonly_roles_have_no_todo(world):
    for u in ("lead", "admin"):
        d = _get(login(u))
        assert d["readonly"] is True and d["items"] == [] and d["total"] == 0 and d["overview"] == []


def test_role_comes_from_session_not_query(world):
    r = login("tech1").get("/api/todo?role=manager")
    assert r.json()["role"] == "tech"
    assert login("pm").get("/api/todo?role=tech").json()["role"] == "manager"
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    anon.headers.update({"x-zb": "1"})
    assert anon.get("/api/todo").status_code == 401


# ----------------------------------------------------------------------------- 候选隐患：唯一展示位 + 就地处理

def test_candidate_single_place_and_confirm_removes_item(world):
    sa = login("sa_a")
    d = _get(sa)
    c = _src(d, "candidate")
    assert len(c) == 1 and c[0]["inline"] == "candidate" and c[0]["status"] == "候选·待确认" and c[0]["finding"]["id"] == world["fid_cand"]
    assert c[0]["finding"]["title"] and "body" in c[0]["finding"]
    assert c[0]["module"] == "safety"
    # 候选不在“待整改”口径里：整改来源只有已确认的两条
    assert {i["finding"]["id"] for i in _src(d, "rectify")} == {world["fid_rect"], world["fid_over"]}
    # 原独立“待确认隐患”区块不再在首页接口里单列：安全员首页数据仍有 candidate_count（供其它页面使用），但待办表是唯一展示位
    r = sa.post(f"/api/findings/{world['fid_cand']}/confirm", json={"assignee_id": None, "due_date": None})
    assert r.status_code == 200, r.text
    d2 = _get(sa)
    assert not _src(d2, "candidate")                                         # 确认后条目消失
    assert world["fid_cand"] in {i["finding"]["id"] for i in _src(d2, "rectify")}      # 确认并派单后进入原流程（待整改）


def test_candidate_dismiss_requires_reason_and_item_disappears(world):
    ss = login("ss")
    assert len(_src(_get(ss), "candidate")) == 1
    assert ss.post(f"/api/findings/{world['fid_cand']}/dismiss", json={"reason": " "}).status_code == 400
    assert ss.post(f"/api/findings/{world['fid_cand']}/dismiss", json={"reason": "光线造成的误识别"}).status_code == 200
    assert not _src(_get(ss), "candidate")


def test_pm_no_candidates_and_safety_sup_sees_all(world):
    """D7：项目经理退出 AI 候选确认（权限保留，只是不进待办），高风险候选也不进；安全主管看全部。"""
    assert not _src(_get(login("pm")), "candidate")                         # 夹具里的候选是高风险
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE findings SET level='medium' WHERE id=?", (world["fid_cand"],))
    assert len(_src(_get(login("ss")), "candidate")) == 1
    assert not _src(_get(login("pm")), "candidate")
    assert not _src(_get(login("eng")), "candidate") and not _src(_get(login("tech1")), "candidate")   # 工程主管、技术员无权决定
    # 权限保留：项目经理仍能在详情页确认候选
    assert login("pm").post(f"/api/findings/{world['fid_cand']}/confirm", json={"assignee_id": None, "due_date": None}).status_code == 200


def test_qr_pending_and_candidate_are_one_entry(world):
    """扫码上报识别出候选隐患时，只出候选一条；没有候选的上报才出“待认领”。"""
    sa = login("sa_a")
    d = _get(sa)
    assert len(_src(d, "quick")) == 1 and "3号墩承台" in _src(d, "quick")[0]["title"]
    assert _src(d, "quick")[0]["quick"][0]["api"]["url"].endswith("/claim")
    # 给该上报的照片造一个候选隐患：待认领条目让位给候选
    with closing(dbmod.connect()) as conn:
        s = conn.execute("SELECT * FROM quick_submissions").fetchone()
        mid = json.loads(s["media_json"])[0]
        sa_row = conn.execute("SELECT id FROM users WHERE username='sa_a'").fetchone()
        F.create(conn, world["project_id"], "safety", "hazard", "high", "扫码：围挡缺口", {"desc": "围挡缺口", "category": "临边洞口", "source": "扫码"},
                 ref_type="media", ref_id=mid, assignee_id=world["users"]["sa_a"], candidate=True)
    d2 = _get(sa)
    assert not _src(d2, "quick")
    assert any("扫码" in i["title"] and i["source"] == "candidate" for i in d2["items"])


# ----------------------------------------------------------------------------- 各来源的细节

def test_tech_items_and_actions(world):
    d = _get(login("tech1"))
    s = _sources(d)
    assert {"rectify", "qc", "gate", "week", "meeting"} <= s
    rect = {i["finding"]["id"]: i for i in _src(d, "rectify")}
    assert rect[world["fid_over"]]["overdue"] is True and rect[world["fid_over"]]["tone"] == "red" and rect[world["fid_rect"]]["inline"] == "rectify"
    wk = _src(d, "week")[0]
    assert wk["overdue"] is True and wk["action_route"].startswith("week?date=")
    g = _src(d, "gate")[0]
    assert g["action_route"].startswith("gate?open=") and g["module"] == "progress"      # 放行清单缺项并入待办（原 top:todo 小卡从未渲染）
    m = _src(d, "meeting")[0]
    assert m["quick"][0]["api"]["url"].endswith("/done") and m["overdue"]
    # 本周进度确认就在首页卡片上，不在待办里重复
    assert not any(i["title"] == "本周进度确认" for i in d["items"])
    labels = [o["label"] for o in d["overview"]]
    assert labels == ["今日随手记", "待处理", "紧急待办", "本周确认"]


def test_eng_items_and_actions(world):
    d = _get(login("eng"))
    lo = _src(d, "log_low")[0]
    assert lo["quick"][0]["api"]["body"]["ids"] == [world["log_lo"]]
    hi = _src(d, "log_review")[0]
    assert hi["status"] == "高风险" and hi["action_route"] == f"log?id={world['log_hi']}"
    assert not _src(d, "log_missing")                                         # “今日无记录”已删除（C12）
    assert {i["title"].split("（")[0] for i in _src(d, "week_missing")} >= {"李工"} and all(i["quick"] for i in _src(d, "week_missing"))
    gap = _src(d, "data_gap")[0]
    assert gap["status"] == "数据缺口" and gap["quick"][0]["api"] and "没报" in gap["reason"]
    assert _src(d, "alert")[0]["quick"][0]["api"]["url"].endswith("/feedback")
    assert _src(d, "mapping")[0]["action_route"] == "output" and _src(d, "report")[0]["action_route"].startswith("report?id=")
    ov = d["overview"]
    assert [o["label"] for o in ov] == ["今日随手记", "待处理", "超期质量问题", "本周确认进度"]
    assert ov[0].get("sub") != "条随手记"                                        # F-18③：不再拼成“2 今日新增记录 条随手记”
    # 一键提醒 / 通过低风险 复用既有接口：执行后条目消失
    eng = login("eng")
    r = eng.post("/api/logs/batch-approve", json={"ids": lo["quick"][0]["api"]["body"]["ids"]})
    assert r.status_code == 200 and r.json()["approved"] == 1
    assert not _src(_get(eng), "log_low")


def test_safety_sup_items_and_review_detail(world):
    d = _get(login("ss"))
    rv = _src(d, "review")[0]
    assert rv["inline"] == "review" and rv["status"] == "整改待复核" and [q["label"] for q in rv["quick"]] == ["确认闭环", "退回重改"]
    assert rv["quick"][1]["prompt"]["required"] is True
    ov = _src(d, "overdue")[0]
    assert ov["overdue"] and ov["quick"][0]["label"] == "催办" and "已超期" in ov["reason"]
    assert _src(d, "unassigned")[0]["tone"] == "red"
    assert [o["label"] for o in d["overview"]] == ["今日新增隐患", "待处理", "超期隐患", "本周已闭环"]
    assert d["overdue"] >= 1 and d["tabs"]["safety"] >= 4


def test_gate_item_for_tech_limited_to_own_scope(world):
    t1, t2 = _get(login("tech1")), _get(login("tech2"))
    assert _src(t1, "gate")
    for i in _src(t2, "gate"):
        assert not any(k in i["title"] for k in ("3#墩",))                                # 乙单位技术员（路基范围）看不到 3#墩作业


def test_items_capped_per_source_with_more_row(env):
    _team(env)
    eng = login("eng")
    eng.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    for n in range(12):
        _finding(env, f"质量问题{n}", agent="quality", kind="reported", level="medium", assignee="tech1")
    d = _get(eng)
    q = _src(d, "quality")
    assert len(q) == todo_svc.PER_SOURCE
    more = _src(d, "quality_more")[0]
    assert "另有 4 项" in more["title"] and more["tone"] == "gray" and more["action_route"] == "quality"


# ----------------------------------------------------------------------------- 空库引导

def test_empty_state_has_guidance_and_buttons(env):
    _team(env)
    pm, eng, tech, sa = login("pm"), login("eng"), login("tech1"), login("sa_a")
    d = _get(pm)
    assert d["items"] == [] and d["total"] == 0
    e = d["empty"]
    assert "P6" in e["title"] and e["actions"] and e["actions"][0]["route"] == "progress?tab=plan"
    assert _get(tech)["empty"]["actions"][0].get("focus") == "note"
    assert _get(sa)["empty"]["actions"][0].get("focus") == "snap"
    with closing(dbmod.connect()) as conn:                                  # 还没有技术员时，工程主管没有“今日无记录”之类条目，同样得到引导
        conn.execute("UPDATE users SET active=0 WHERE role='tech'")
    e_eng = _get(eng)
    assert e_eng["items"] == [] and e_eng["empty"]["actions"][0]["route"] == "progress?tab=plan" and e_eng["p6"] is None
    # 管理员/领导：只读，不给引导
    assert _get(login("admin"))["empty"] is None
    # 导入 P6 后空库仍有说明（项目经理只写原因，不放“看项目总览”这类只多点一次的按钮），而不是空白
    eng.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    e2 = _get(pm)["empty"]
    assert e2 is None or (e2["hint"] and not e2["actions"])


def test_one_source_failure_does_not_break_todo(world, monkeypatch):
    def boom(c):
        raise RuntimeError("坏了")
    monkeypatch.setitem(todo_svc.SOURCES, "tech", [boom] + todo_svc.SOURCES["tech"][1:])
    d = _get(login("tech1"))
    assert d["items"] and d["total"] == len(d["items"])
    assert {"gate", "week"} <= _sources(d)           # 其余来源照常


# ----------------------------------------------------------------------------- 前端接线（无构建，静态检查）

def test_frontend_wiring_single_place_for_candidates_and_gate():
    import shutil
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    read = lambda n: (root / n).read_text(encoding="utf-8")  # noqa: E731
    html = read("index.html")
    assert "/static/todo.js" in html and "/static/todo.css" in html
    assert html.index("/static/aifeed.js") < html.index("/static/todo.js") < html.index("/static/tech.js") < html.index("/static/mgr.js") < html.index("/static/roles.js")
    # 候选隐患只在待办表里有完整展示位：安全员首页不再另放“待确认隐患”区块，各首页都用同一待办组件
    roles, mgr, tech = read("roles.js"), read("mgr.js"), read("tech.js")
    assert "待确认隐患" not in roles and "candidate-box" not in roles and "shome:due" not in roles
    assert "todo-mobile" in roles and "todo-mobile" in tech
    # 工程主管、安全主管首页（RJ 10-10 设计包 13、14）是 x_enghome.js 的 EngBoard / SafetyBoard：待办仍是 todo.js 的同一个 TodoBoard；
    # 安全主管的 AI 候选只在“AI 候选确认”一处办理，待我处理表里剔除候选，不重复展示
    eh = read("x_enghome.js")
    assert "<eng-board>" in roles and "<safety-board>" in roles and "todo-board" in eh
    assert 'i.source !== "candidate"' in eh and eh.count("<candidate-box") == 1
    assert html.index("/static/todo.js") < html.index("/static/x_enghome.js") < html.index("/static/roles.js")
    # 项目经理首页（RJ 10-10 采用 09_V8）是 x_pmhome.js 的 PmHome，待办行仍是 todo.js 的同一个 TodoItem
    pm = read("x_pmhome.js")
    assert "pm-home" in mgr and "extends: TodoItem" in pm
    assert html.index("/static/todo.js") < html.index("/static/x_pmhome.js") < html.index("/static/mgr.js")
    assert "ai-feed" not in roles + mgr and "AiFeed" not in read("aifeed.js")
    # 放行清单缺项并入待办；无效的 top:todo 注册已清理；扩展不再各自往首页顶部塞卡片
    assert 'EXT.slot("top:todo"' not in read("x_gate.js") and "GateDigest" not in read("x_gate.js")
    for n in ("x_gate.js", "x_conceal.js", "x_tickets.js", "x_quick.js"):
        assert "EXT.feed(" not in read(n), n
    assert "CandidateBox" in read("todo.js") and "candidate-box" in read("todo.js")
    node = shutil.which("node")
    if node:
        for n in ("todo.js", "roles.js", "mgr.js", "tech.js", "aifeed.js", "core.js", "x_pmhome.js", "x_enghome.js"):
            r = subprocess.run([node, "--check", str(root / n)], capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"{n}: {r.stderr}"


def test_frontend_wiring_t06():
    """T-06 前端静态核对：系统结论标签（AC-3.5）、AI 建议文案（N-2）、需要知道一行、候选分组挂载（T-05 契约）、整改暂存提示。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    js, cp = (root / "todo.js").read_text(encoding="utf-8"), (root / "cockpit.js").read_text(encoding="utf-8")
    concl = js[js.index("const TodoConclusion"):js.index("const TodoAttention")]
    assert "系统按当前事实生成" in concl and "source === 'system'" in concl and "AI 今日结论" in concl
    assert "a.note" in concl                                                   # 失效原因一行小字直接显示，不收进“依据”
    assert "系统按当前事实生成" in cp and "source === 'system'" in cp and "AI 今日结论" not in cp
    body = js[js.index("const TodoBody"):js.index("const TodoItem")]
    assert "'已消除'" not in body and "'未消除'" not in body and "verify_text" in body
    assert "重新让 AI 复核" in body and "/reverify" in body
    assert "需要知道" in js and "d.know" in js
    assert "window.CandidateGroup" in js and "candidate-box" in js and "group_key" in js
    assert "queued" in js and "已暂存" in js
    assert "log_missing" not in js[js.index("const GROUP_SOURCES"):js.index("const TodoGroup")]


def test_action_routes_point_to_existing_pages(world):
    """“下一步”按钮必须能到达真实存在的页面（main.js 内置路由 + 各 x_*.js 注册的扩展路由）。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    main = (root / "main.js").read_text(encoding="utf-8")
    builtin = set(re.findall(r"r\.name === '([a-z]+)'", main)) | {"home", "week", "logs", "work", "cockpit"}
    ext = set()
    for f in root.glob("x_*.js"):
        ext |= set(re.findall(r'EXT\.route\("([a-z]+)"', f.read_text(encoding="utf-8")))
    known = builtin | ext
    assert {"finding", "alert", "log", "gate", "progress", "qrcodes", "tickets", "conceal"} <= known
    for u in ("tech1", "sa_a", "eng", "ss", "pm"):
        for i in _get(login(u))["items"]:
            if i["action_route"]:
                assert i["action_route"].split("?")[0] in known, (u, i["id"], i["action_route"])
