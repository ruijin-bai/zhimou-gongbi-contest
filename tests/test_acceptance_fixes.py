"""T-17 修复 P0 总验收缺陷（docs/验收_P0_2026-10-07.md 的 D-1～D-5、D-7）。

- D-1 预警详情页要显示“P6 数据日期”：用 Vue 真渲染 AlertDetail（tests/_render_harness.js），不只查源码字符串；
- D-2 安全主管闭环 / 退回整改后通知整改责任人（带跳转），被退回的整改在责任人待办里写明退回原因；
- D-3 项目经理首页：概览格只数待工程主管确认的红橙预警，名称与待办里“已确认红色预警”区分开；
- D-4 整改被退回后，旧的 AI 建议标为“已被人工否定”，不再写“待安全主管确认”；
- D-5 mock 理由自带全角括号时不再套一层（“（（离线模拟））”）；
- D-7 项目经理不进催办台：前端路由拦截 #/chase，后端催办台接口（规则、运行、记录、今日动态）对项目经理返回 403。
"""
import json
import re
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

from conftest import login, sample_xer
from test_facts import _add_alert
from test_frontline_f import NODE
from test_rectify_review import _dispatched, _photo
from test_roles import _team

from app import db as dbmod
from app.services import site_agents as SA

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HARNESS = Path(__file__).resolve().parent / "_render_harness.js"


def render(component: str, props: dict, me: dict, fetch: dict, hash_: str = "") -> dict:
    """在 node 里用 Vue 真渲染组件，返回 {text, fetched, errs}。"""
    if not NODE:
        pytest.skip("没有 node")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fp:
        json.dump({"component": component, "props": props, "me": me, "fetch": fetch, "hash": hash_}, fp, ensure_ascii=False)
    try:
        r = subprocess.run([NODE, str(HARNESS), str(STATIC), fp.name], capture_output=True, text=True, encoding="utf-8", timeout=90)
    finally:
        Path(fp.name).unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _me(c) -> dict:
    return c.get("/api/me").json()


# ============================================================================ D-1 预警详情页显示 P6 数据日期

def test_d1_alert_detail_renders_p6_data_date(env):
    _team(env)
    eng = login("eng")
    assert eng.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    aid = _add_alert(env["project_id"], code="A1020", level="red", slip=8.0)
    a = eng.get(f"/api/alerts/{aid}").json()
    assert a["p6_data_date"]                                    # 接口本来就返回
    out = render("AlertDetail", {"id": aid}, _me(eng), {f"/api/alerts/{aid}": a})
    assert not out["errs"], out["errs"]
    assert f"判定日 {a['as_of']}" in out["text"], out["text"][:300]
    assert f"P6 数据日期 {a['p6_data_date']}" in out["text"], out["text"][:300]
    # 取不到时写“未知”，不补默认值
    out = render("AlertDetail", {"id": aid}, _me(eng), {f"/api/alerts/{aid}": {**a, "p6_data_date": None, "explanation": None}})
    assert "P6 数据日期未知" in out["text"]


def test_d1_alert_card_in_lists_keeps_short_header(env):
    """列表里的预警卡（不带 full）只写判定日，不写 P6 数据日期：渲染结果区分得开，说明 full 确实传到了。"""
    _team(env)
    eng = login("eng")
    assert eng.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    aid = _add_alert(env["project_id"], code="A1020", level="red", slip=8.0)
    a = eng.get(f"/api/alerts/{aid}").json()
    out = render("AlertCard", {"a": a}, _me(eng), {})
    assert not out["errs"], out["errs"]
    assert f"判定日 {a['as_of']}" in out["text"] and "P6 数据日期" not in out["text"]


# ============================================================================ D-2 闭环 / 退回后通知整改责任人，退回原因进待办

def _submitted(env):
    tech, ss, fid = _dispatched(env)
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech)["id"]}).status_code == 200
    return tech, ss, fid


def _msgs(c, word):
    return [n for n in c.get("/api/notifications").json() if word in n["title"]]


def test_d2_return_notifies_assignee_with_link_and_reason(env):
    tech, ss, fid = _submitted(env)
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "open", "note": "照片看不到防护鞋，请重拍"}).status_code == 200
    got = _msgs(tech, "退回")
    assert len(got) == 1, [n["title"] for n in tech.get("/api/notifications").json()]
    assert got[0]["link"] == {"route": "finding", "params": {"id": str(fid)}}
    assert "照片看不到防护鞋，请重拍" in got[0]["body"] and "安全主管" in got[0]["body"]
    assert not _msgs(ss, "退回")                                # 操作人自己不收
    # 责任人待办：被退回的整改写明“被退回”和原因
    it = next(i for i in tech.get("/api/todo").json()["items"] if i["id"] == f"rectify:{fid}")
    assert it["status"] == "被退回" and "照片看不到防护鞋，请重拍" in it["reason"], it


def test_d2_close_notifies_assignee(env):
    tech, ss, fid = _submitted(env)
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "resolved", "note": ""}).status_code == 200
    got = _msgs(tech, "闭环")
    assert len(got) == 1 and got[0]["link"] == {"route": "finding", "params": {"id": str(fid)}}
    assert "安全主管" in got[0]["body"]
    assert not any(i["id"] == f"rectify:{fid}" for i in tech.get("/api/todo").json()["items"])


def test_d2_subcontractor_submitter_also_notified(env):
    """分包安全员替本单位提交的整改：提交人和责任人都收到退回消息。"""
    tech, ss, fid = _dispatched(env)
    sa = login("sa_a")
    assert sa.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(sa)["id"]}).status_code == 200
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "open", "note": "防护栏高度不够"}).status_code == 200
    for c in (tech, sa):
        got = _msgs(c, "退回")
        assert len(got) == 1 and "防护栏高度不够" in got[0]["body"]
    it = next(i for i in sa.get("/api/todo").json()["items"] if i["id"] == f"rectify:{fid}")
    assert it["status"] == "被退回" and "防护栏高度不够" in it["reason"]


# ============================================================================ D-4 退回后旧的 AI 建议标为已被人工否定

def test_d4_verify_label_after_return():
    L = SA.verify_label
    t = L({"state": "done", "suggest": "resolved"}, "open")
    assert "已被人工否定" in t and "待安全主管确认" not in t and t.startswith("已退回重改")
    t = L({"state": "done", "suggest": "not_resolved", "remaining": "临边防护缺失"}, "open")
    assert t.startswith("已退回重改") and "仍存在临边防护缺失" in t and "否定" not in t
    assert L({"state": "pending"}, "open") == "已退回重改"
    assert L({"state": "failed"}, "open") == "已退回重改"
    # 其余状态不变（T-13 契约）
    assert L({"state": "done", "suggest": "resolved"}, "rectifying") == "AI 建议：已消除，待安全主管确认"
    assert L({"state": "done", "suggest": "resolved"}, "resolved") == "AI 建议：已消除"


def test_d4_finding_detail_marks_ai_suggestion_overruled(env):
    tech, ss, fid = _submitted(env)
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "open", "note": "照片看不到防护鞋，请重拍"}).status_code == 200
    f = tech.get(f"/api/findings/{fid}").json()
    assert f["returned"] is True and f["verify"]["suggest"] == "resolved"
    out = render("FindingDetail", {"id": fid}, _me(tech), {f"/api/findings/{fid}": f, "/api/assignees": []})
    assert not out["errs"], out["errs"]
    assert "已被人工否定" in out["text"], out["text"][:400]
    assert "待安全主管确认" not in out["text"]
    assert "退回原因：照片看不到防护鞋，请重拍" in out["text"]
    # 待复核时照常是 AI 建议
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": _photo(tech)["id"]}).status_code == 200
    f = tech.get(f"/api/findings/{fid}").json()
    assert f["returned"] is False
    out = render("FindingDetail", {"id": fid}, _me(tech), {f"/api/findings/{fid}": f, "/api/assignees": []})
    assert "AI 建议：已消除，待安全主管确认" in out["text"] and "已被人工否定" not in out["text"]


# ============================================================================ D-5 mock 理由不再套双括号

def test_d5_no_double_brackets(env):
    tech, ss, fid = _submitted(env)
    assert SA.paren("（离线模拟）") == "（离线模拟）" and SA.paren("照片清楚") == "（照片清楚）" and SA.paren("") == ""
    it = next(i for i in ss.get("/api/todo").json()["items"] if i["id"] == f"review:{fid}")
    assert "离线模拟" in it["reason"] and "（（" not in it["reason"] and "））" not in it["reason"], it["reason"]
    from app.services import feed_agents, qa
    with closing(dbmod.connect()) as conn:
        row = conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
        fact = feed_agents.verify_fact(conn, dict(row))
    assert "离线模拟" in fact and "（（" not in fact, fact
    q = qa._verify_fact(dbmod.loads(row["verify_json"]), row["status"])
    assert "离线模拟" in q and "（（" not in q, q


# ============================================================================ D-3 同屏红橙预警只有一个口径

from test_todo import world  # noqa: E402,F401  夹具：含一条待确认红色预警 + 一条已确认红色预警


def test_d3_pm_overview_counts_pending_alerts_and_names_them_apart(world):
    pm = login("pm")
    d = pm.get("/api/todo").json()
    ov = {o["label"]: o for o in d["overview"]}
    assert "红橙进度预警" not in ov                                  # 旧名称与“已确认红色预警”读起来是同一个指标
    tile = ov["待确认红橙预警"]
    assert tile["sub"] == "待工程主管确认"
    with closing(dbmod.connect()) as conn:
        pending = conn.execute("SELECT COUNT(*) n FROM alerts WHERE project_id=? AND status='open' AND rule<>'stale' AND level IN ('red','orange')",
                               (world["project_id"],)).fetchone()["n"]
    assert tile["value"] == pending >= 1
    crash = [i for i in d["items"] if i["source"] == "crash"]
    assert crash and crash[0]["status"] == "已确认红色预警"              # 待办里的是另一件事：已确认、待拍板的赶工决定
    assert not set(ov) & {i["status"] for i in d["items"]}
    # “需要知道”里的预警数与概览格是同一个数、同一个名称口径
    part = next(p for p in d["know"]["parts"] if p["route"] == "progress?tab=alerts&status=open")   # 带筛选进入（验收 I4）
    assert part["n"] == tile["value"] and "待工程主管确认" in part["label"]


def test_d3_system_headline_says_pending():
    from app.services import brief
    k = {"alerts": {"red": 0, "orange": 0, "yellow": 3}, "safety": {"open": 2, "high": 1, "overdue": 0, "resolved": 0, "total": 2},
         "quality": {"open": 0, "reminders": 0, "overdue": 0}, "samples": {"安全": 2, "质量": 1}}
    t = brief.template(k, {"alerts": k["alerts"], "lag_alerts": []})
    assert t["headline"].startswith("待确认进度预警 红0 橙0 黄3")


# ============================================================================ D-7 项目经理不进催办台

def test_d7_nudge_desk_api_forbidden_for_pm(env):
    _team(env)
    pm, eng, ss = login("pm"), login("eng"), login("ss")
    for r in (pm.get("/api/nudge/rules"), pm.put("/api/nudge/rules", json={"daily_cap": 5}), pm.post("/api/nudge/run", json={"dry": True}),
              pm.get("/api/nudge/log"), pm.get("/api/nudge/today")):
        assert r.status_code == 403, r.text
    for c in (eng, ss):
        assert c.get("/api/nudge/rules").status_code == 200 and c.get("/api/nudge/today").status_code == 200
    assert pm.get("/api/nudge/pins").status_code == 200              # 固定的提问照常可用


def test_d7_pm_typing_chase_url_is_blocked(env):
    _team(env)
    pm, eng = login("pm"), login("eng")
    out = render("Root", {}, _me(pm), {}, hash_="#/chase")
    assert not out["errs"], out["errs"]
    assert "不在项目经理的查看范围" in out["text"], out["text"][:300]
    assert not any(u.startswith("/api/nudge/rules") or u.startswith("/api/nudge/log") for u in out["fetched"]), out["fetched"]
    # 工程主管照常打开催办台
    out = render("Root", {}, _me(eng), {"/api/nudge/rules": eng.get("/api/nudge/rules").json(), "/api/nudge/log?limit=60": []}, hash_="#/chase")
    assert "不在项目经理的查看范围" not in out["text"] and "/api/nudge/rules" in out["fetched"], out["fetched"]
