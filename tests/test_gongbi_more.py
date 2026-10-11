"""工笔文书增补类型：例会底稿、三周滚动计划、月度复盘。表格数字由程序填，规则版只拼事实，数字守卫通过；不写人名、产值。离线模拟模型，合成数据。"""
from datetime import date, timedelta

from conftest import TODAY, login
from test_site import D, _world

from app import db as dbmod
from app.ai import docs_registry as reg
from app.services import gongbi_docs as gd  # noqa: F401  注册增补类型
from app.services import gongbi_more as gm


def _meeting(env, items):
    conn = dbmod.connect()
    pid = env["project_id"]
    mid = dbmod.insert(conn, "meetings", {"project_id": pid, "title": "第 12 周生产例会", "meeting_date": D(7), "created_at": dbmod.now_iso(), "confirmed_at": dbmod.now_iso()})
    for text, owner, due, status in items:
        dbmod.insert(conn, "meeting_items", {"project_id": pid, "meeting_id": mid, "text": text, "owner_id": env["users"][owner], "due_date": due, "status": status,
                                             "created_at": dbmod.now_iso()})
    conn.close()


def _draft(c, kind, ref=None):
    r = c.post("/api/gongbi/docs", json={"kind": kind, "ref": ref or TODAY.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def test_types_listed_with_roles(env):
    _world(env)
    kinds = {t["kind"]: t for t in login("pm").get("/api/gongbi/types").json()["types"]}
    for k, g in (("meeting_pack", "例会"), ("three_week_plan", "例会"), ("monthly_review", "分析")):
        assert k in kinds and kinds[k]["group"] == g and kinds[k]["can_draft"]
    # RJ 10-10：工笔文书按用途分四个同级分区，每种文书只属于其中一个
    assert {t["group"] for t in kinds.values()} == {"报表", "函件", "例会", "分析"}


def test_meeting_pack_first_section_is_last_actions(env):
    _world(env)
    _meeting(env, [("请甲分包周五前补齐 3#墩钢筋", "tech1", D(2), "open"), ("路基排水沟清理", "tech2", D(9), "done"), ("提交吊装方案", "tech1", D(-3), "open")])
    d = _draft(login("pm"), "meeting_pack")
    assert d["template"]["sections"][0]["key"] == "actions"
    assert d["narrative"]["actions"].startswith(f"上次例会（{D(7)}）行动项 3 项，已完成 1 项，未完成 1 项，超期 1 项")
    rows = d["tables"]["actions"]["rows"]
    assert [r[4] for r in rows] == ["超期", "已完成", "未完成"]
    assert all("张工" not in str(x) and "李工" not in str(x) for r in rows for x in r)   # 不写人名，写单位/岗位
    assert "甲" in rows[0][2]
    # 未闭环高风险隐患（world 里 3#墩临边）进入底稿
    assert any("临边" in r[2] for r in d["tables"]["hazards"]["rows"])
    assert d["check"] == []          # 规则版只拼事实，数字守卫通过
    assert "建议本次议题" in d["template"]["sections"][-1]["title"] and d["narrative"]["topics"]


def test_meeting_pack_without_previous_meeting(env):
    _world(env)
    d = _draft(login("pm"), "meeting_pack")
    assert "还没有已确认的例会纪要" in d["narrative"]["actions"] and d["tables"].get("actions", {"rows": []})["rows"] == []


def test_three_week_plan(env):
    _world(env)
    d = _draft(login("pm"), "three_week_plan")
    start = TODAY - timedelta(days=TODAY.weekday())
    assert d["period_start"] == start.isoformat() and d["period_end"] == (start + timedelta(days=20)).isoformat()
    names = [r[0] for r in d["tables"]["summary"]["rows"]]   # 作业栏显示中文简称（app/services/act_names.py）
    assert "3#墩承台钢筋绑扎" in names and "开工" not in names          # 里程碑不列
    assert "参考推演" in d["narrative"]["summary"] and d["narrative"]["en_summary"].startswith("Three-week look-ahead")
    assert d["check"] == []


def test_three_week_plan_needs_p6(env):
    r = login("pm").post("/api/gongbi/docs", json={"kind": "three_week_plan", "ref": TODAY.isoformat()})
    assert r.status_code in (400, 422) and "P6" in r.text


def test_monthly_review_counts_closed_items(env):
    _world(env)
    conn = dbmod.connect()
    now = dbmod.now_iso()
    from app.services import findings as find_svc
    for i in range(3):
        fid = find_svc.create(conn, env["project_id"], "safety", "hazard", "medium", f"未系安全带 {i}", {"category": "高处作业", "measure": "现场教育并补系安全带"},
                              fingerprint=f"t:mr{i}", due_days=2)
        conn.execute("UPDATE findings SET status='resolved', confirmed_at=created_at, resolved_at=? WHERE id=?", (now, fid))
    conn.close()
    d = _draft(login("pm"), "monthly_review")
    assert d["period_start"] == TODAY.replace(day=1).isoformat()
    row = next(r for r in d["tables"]["top"]["rows"] if r[0] == "安全·高处作业")
    assert row[1] == 3
    assert "安全·高处作业 3 次" in d["narrative"]["repeat"]
    assert d["narrative"]["lessons"] and "现场教育并补系安全带" in d["narrative"]["lessons"][0]
    assert d["check"] == []
    # 确认后，经验条目可供问工笔引用
    pm = login("pm")
    assert pm.put(f"/api/gongbi/docs/{d['id']}", json={"narrative": d["narrative"], "confirm": True}).status_code == 200
    conn = dbmod.connect()
    got = gm.lessons(conn, env["project_id"])
    conn.close()
    assert got and got[0].startswith(f"[月度复盘 {TODAY.isoformat()[:7]}]")


def test_number_guard_falls_back_per_section(env, monkeypatch):
    _world(env)
    _meeting(env, [("补齐钢筋", "tech1", D(2), "open")])
    pm_user = {"id": env["users"]["pm"], "project_id": env["project_id"], "role": "manager"}
    conn = dbmod.connect()
    facts = gm.meeting_facts(conn, pm_user, "meeting_pack", TODAY.isoformat())
    conn.close()
    bad = {**gm.meeting_rule("meeting_pack", facts), "opening": "本周有 99 项重点。"}
    nar, issues, back = gd.finalize("meeting_pack", bad, facts, ai=True)
    assert back == ["opening"] and "99" not in nar["opening"] and issues == []
    assert reg.facts_text("meeting_pack", facts).startswith("【统计事实】")


def test_docx_export(env):
    _world(env)
    pm = login("pm")
    d = _draft(pm, "three_week_plan")
    r = pm.get(f"/api/gongbi/docs/{d['id']}/docx")
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_roles(env):
    _world(env)
    assert login("tech1").post("/api/gongbi/docs", json={"kind": "meeting_pack", "ref": date.today().isoformat()}).status_code == 403
