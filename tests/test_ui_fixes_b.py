"""T-12b UI 低级错误（views.js 与消息车道）。

A 级：
- S-1 views.js 的页面（日志、预警、报表、周报详情、消息）接入 <load-state>：加载失败给原因和“重试”，不白屏、不一直“加载中”；
- F-14③ 预警页说明里的“数据缺口（没报，不是落后）”只给工程主管看，项目经理看不到“数据缺口”；
- AC-3.6 预警详情显示“判定日 X · P6 数据日期 Y”，取不到写“P6 数据日期未知”，不补默认值；
- U-14 预警已关闭/误报后，相关消息卡标“已解除”（灰标）；
- 消息页空状态“回到首页”（D10）；x_ui 的 log_view 副标题不出现“审核”；x_nudge 催办链按 D9 写；
- 预警列表/详情序列化时把连接传给 serialize_alert，老预警不再逐条另开连接。

C 级：
- F-07 / S-5④ 点开即已读（POST /api/notifications/{id}/read）；所链事项已处理的显示“已处理”并计为已读；
- S-5② 按 created_at 倒序、可翻“更早的消息”；S-5③ 旧消息读取时按 nudge_log / 标题里的隐患名反查跳转；
- S-5⑤ 截断的标题补“…”；S-5⑦ 日期不折行；
- U-15 措施期限占位符/早于判定日写“待定”；分析与判定日不一致统一提示；卡上不露 W/L 内部编号；
- U-16 / S-4 总浮时取整写“天”；时长写“分钟”；波浪号统一“～”；
- U-18 有待核数字时逐条勾“已核实”才能发布，已发布的列表不再写“待核”；
- U-21 流程卡只给作者；审核人看未提交草稿写“作者还未提交”；分包日志“全部”不列空草稿；
- U-23 应用成效概览：样本不足（n<5）不显示比例，“分”写“分钟”；
- U-34 周确认行不写“（未生成）”；
- S-7 预警“误报/处置关闭”不用浏览器原生 prompt，改为卡内就地填写、常用原因可点选。
"""
import json
import re
from datetime import datetime, timedelta

import pytest

from conftest import login
from test_roles import _team
from test_ui_fixes import read, run_js

from app import db as dbmod
from app import main as main_mod  # noqa: F401  (确保应用已加载)


def _conn():
    return dbmod.connect()


def _notif(env, user, title, body="内容", alert_id=None, link=None, created_at=None, read=False):
    conn = _conn()
    try:
        return dbmod.insert(conn, "notifications", {"project_id": env["project_id"], "user_id": env["users"][user], "alert_id": alert_id,
                                                    "title": title, "body": body, "channels_json": "{}",
                                                    "created_at": created_at or dbmod.now_iso(), "read_at": dbmod.now_iso() if read else None,
                                                    "link_json": json.dumps(link) if link else None})
    finally:
        conn.close()


def _alert(env, status="open", import_id=None, explanation=None):
    conn = _conn()
    try:
        return dbmod.insert(conn, "alerts", {"project_id": env["project_id"], "import_id": import_id or 0, "fingerprint": f"fp{datetime.now().timestamp()}",
                                             "activity_code": "A1010", "activity_name": "桩基", "level": "yellow", "rule": "behind",
                                             "title": "A1010 桩基：进度滞后", "detail_json": "{}", "explanation_json": json.dumps(explanation) if explanation else None,
                                             "status": status, "first_detected": "2026-10-01", "last_detected": "2026-10-07", "as_of": "2026-10-07"})
    finally:
        conn.close()


def _finding(env, title="临边洞口：右侧基坑边坡无防护栏杆，人员靠近存在坠落风险", assignee="tech1", status="open"):
    from app.services import findings as find_svc
    conn = _conn()
    try:
        fid = find_svc.create(conn, env["project_id"], "safety", "hazard", "high", title, {}, fingerprint=f"t:{title}",
                              assignee_id=env["users"][assignee] if assignee else None, due_days=2)
        conn.execute("UPDATE findings SET status=? WHERE id=?", (status, fid))
        return fid
    finally:
        conn.close()


def _set(sql, args=()):
    conn = _conn()
    try:
        conn.execute(sql, args)
    finally:
        conn.close()


def _unread(c):
    return c.get("/api/me").json()["unread"]


# ============================================================================ 后端：消息车道

def test_open_one_notice_marks_only_it_read(env):
    a = _notif(env, "tech1", "消息A")
    b = _notif(env, "tech1", "消息B")
    other = _notif(env, "tech2", "别人的消息")
    t1 = login("tech1")
    assert _unread(t1) == 2
    r = t1.post(f"/api/notifications/{a}/read")
    assert r.status_code == 200 and r.json()["unread"] == 1
    rows = {n["id"]: n for n in t1.get("/api/notifications").json()}
    assert rows[a]["read_at"] and not rows[b]["read_at"]
    assert t1.post(f"/api/notifications/{other}/read").status_code == 404      # 不能标别人的消息
    assert t1.post(f"/api/notifications/{a}/read").json()["unread"] == 1      # 重复标记不报错


def test_notices_sorted_by_time_and_paged(env):
    old = _notif(env, "tech1", "旧事", created_at="2026-09-22T16:40:00+08:00")   # id 大但时间早（仿真回放补写的旧消息）
    new = _notif(env, "tech1", "新事", created_at="2026-10-07T09:00:00+08:00")
    newest = _notif(env, "tech1", "最新", created_at="2026-10-07T10:00:00+08:00")
    _set("UPDATE notifications SET id=id+100 WHERE id=?", (old,))   # 让旧消息的 id 最大
    t1 = login("tech1")
    items = t1.get("/api/notifications").json()
    assert [n["title"] for n in items] == ["最新", "新事", "旧事"]
    assert [n["title"] for n in t1.get("/api/notifications?limit=2").json()] == ["最新", "新事"]
    assert [n["title"] for n in t1.get("/api/notifications?limit=2&offset=2").json()] == ["旧事"]
    assert newest and new


def test_closed_alert_notice_shows_cleared_and_counts_as_read(env):
    aid = _alert(env, status="open")
    nid = _notif(env, "pm", "【黄色预警】A1010 桩基", alert_id=aid)
    pm = login("pm")
    n = next(x for x in pm.get("/api/notifications").json() if x["id"] == nid)
    assert n["state"] is None and n["state_text"] is None and not n["read_at"]
    _set("UPDATE alerts SET status='closed', feedback_note='系统：触发条件已解除' WHERE id=?", (aid,))
    n = next(x for x in pm.get("/api/notifications").json() if x["id"] == nid)
    assert n["state"] == "cleared" and n["state_text"] == "已解除"
    assert n["read_at"] and n["settled"] is True
    assert _unread(pm) == 0
    n = next(x for x in pm.get("/api/notifications").json() if x["id"] == nid)
    assert n["state_text"] == "已解除" and not n.get("settled")   # 只在第一次被计为已读时带 settled


def test_dispatch_notice_done_when_finding_closed(env):
    _team(env)
    fid = _finding(env)
    link = {"route": "finding", "params": {"id": str(fid)}}
    nid = _notif(env, "tech1", "新派单：临边洞口", link=link)
    rev = _notif(env, "ss", "整改待复核：临边洞口", link=link)
    t1, ss = login("tech1"), login("ss")
    assert next(x for x in t1.get("/api/notifications").json() if x["id"] == nid)["state"] is None
    _set("UPDATE findings SET status='rectifying' WHERE id=?", (fid,))
    assert next(x for x in t1.get("/api/notifications").json() if x["id"] == nid)["state_text"] == "已处理"   # 责任人已提交整改
    assert next(x for x in ss.get("/api/notifications").json() if x["id"] == rev)["state"] is None             # 待复核：还轮到安全主管
    _set("UPDATE findings SET status='resolved' WHERE id=?", (fid,))
    assert next(x for x in ss.get("/api/notifications").json() if x["id"] == rev)["state_text"] == "已处理"
    assert _unread(t1) == 0 and _unread(ss) == 0


def test_returned_log_notice_done_after_resubmit(env):
    conn = _conn()
    try:
        lid = dbmod.insert(conn, "logs", {"project_id": env["project_id"], "author_id": env["users"]["tech1"], "log_date": "2026-10-05",
                                          "status": "returned", "started_at": dbmod.now_iso(),
                                          "period": "week", "period_end": "2026-10-11"})
    finally:
        conn.close()
    nid = _notif(env, "tech1", "10-05 起一周进度确认被退回", link={"route": "log", "params": {"id": str(lid)}})
    t1 = login("tech1")
    assert next(x for x in t1.get("/api/notifications").json() if x["id"] == nid)["state"] is None
    _set("UPDATE logs SET status='submitted' WHERE id=?", (lid,))
    assert next(x for x in t1.get("/api/notifications").json() if x["id"] == nid)["state_text"] == "已处理"


def test_week_reminder_done_after_week_submitted(env):
    conn = _conn()
    try:
        lid = dbmod.insert(conn, "logs", {"project_id": env["project_id"], "author_id": env["users"]["tech1"], "log_date": "2026-10-05",
                                          "status": "draft", "started_at": dbmod.now_iso(),
                                          "period": "week", "period_end": "2026-10-11"})
    finally:
        conn.close()
    nid = _notif(env, "tech1", "请确认本周进度", link={"route": "week", "params": {}}, created_at="2026-10-09T18:00:00+08:00")
    prev = _notif(env, "tech1", "请确认本周进度", link={"route": "week", "params": {}}, created_at="2026-10-02T18:00:00+08:00")
    t1 = login("tech1")
    assert next(x for x in t1.get("/api/notifications").json() if x["id"] == nid)["state"] is None
    _set("UPDATE logs SET status='submitted' WHERE id=?", (lid,))
    rows = {x["id"]: x for x in t1.get("/api/notifications").json()}
    assert rows[nid]["state_text"] == "已处理"
    assert rows[prev]["state"] is None     # 上一周的提醒不因本周提交而算已处理


def test_old_notices_get_links_by_reverse_lookup(env):
    _team(env)
    fid = _finding(env, title="基坑东侧临边防护缺失")
    # 旧“新派单”消息：当时没存跳转目标，按标题里的隐患名反查
    old = _notif(env, "tech1", "新派单：基坑东侧临边防护缺失")
    # 旧催办消息：按 nudge_log.notif_id 找到所催的事项
    nud = _notif(env, "sa_a", "协助跟进：基坑东侧临边防护缺失")
    conn = _conn()
    try:
        conn.execute("INSERT INTO nudge_log (project_id, obj_type, obj_key, stage, to_user_id, status, subject, area, link, notif_id, sent_at) "
                     "VALUES (?,?,?,?,?,'sent',?,?,?,?,?)", (env["project_id"], "finding", str(fid), 2, env["users"]["sa_a"],
                                                            "基坑东侧临边防护缺失", "safety", f"finding?id={fid}", nud, dbmod.now_iso()))
    finally:
        conn.close()
    unknown = _notif(env, "tech1", "新派单：库里没有的事项")
    want = {"route": "finding", "params": {"id": str(fid)}}
    rows = {x["id"]: x for x in login("tech1").get("/api/notifications").json()}
    assert rows[old]["link"] == want and rows[unknown]["link"] is None
    sa = {x["id"]: x for x in login("sa_a").get("/api/notifications").json()}
    assert sa[nud]["link"] == want
    # 已存了 link 的消息不改
    keep = _notif(env, "tech1", "新派单：基坑东侧临边防护缺失", link={"route": "circle", "params": {"key": "note:1"}})
    assert {x["id"]: x for x in login("tech1").get("/api/notifications").json()}[keep]["link"] == {"route": "circle", "params": {"key": "note:1"}}


def test_old_nudges_without_notif_id_and_week_reminders_get_links(env):
    _team(env)
    fid = _finding(env, title="场地泥泞积水，存在滑倒风险", assignee="tech1")
    want = {"route": "finding", "params": {"id": str(fid)}}
    pm_old = _notif(env, "pm", "待决策：场地泥泞积水，存在滑倒风险")            # 旧版第 4 级标题，nudge_log 里没记 notif_id
    ss_old = _notif(env, "ss", "升级提醒：场地泥泞积水，存在滑倒风险")
    multi = _notif(env, "ss", "升级提醒：4件事项仍未处理")                     # 合并多件、又没记 notif_id 的：查不到就不放按钮
    wk_t = _notif(env, "tech1", "请确认本周进度")
    wk_e = _notif(env, "ss", "请确认本周进度")                                  # 周确认页只给技术员
    pm = {x["id"]: x for x in login("pm").get("/api/notifications").json()}
    ss = {x["id"]: x for x in login("ss").get("/api/notifications").json()}
    t1 = {x["id"]: x for x in login("tech1").get("/api/notifications").json()}
    assert pm[pm_old]["link"] == want and ss[ss_old]["link"] == want
    assert ss[multi]["link"] is None and ss[wk_e]["link"] is None
    assert t1[wk_t]["link"] == {"route": "week", "params": {}}
    # 催办类：事项离开 open/rejected（已提交整改）即“已处理”
    _set("UPDATE findings SET status='rectifying' WHERE id=?", (fid,))
    assert {x["id"]: x for x in login("ss").get("/api/notifications").json()}[ss_old]["state_text"] == "已处理"


def test_old_gap_nudge_not_mismatched_to_same_named_finding(env):
    _team(env)
    fid = _finding(env, title="RD110 K0+000~K2+000 清表及土方开挖：边坡无防护", status="resolved")
    subj = "RD110 K0+000~K2+000 清表及土方开挖：关键作业无进度上报"
    nid = _notif(env, "tech1", f"催办提醒：{subj[:22]}")      # 数据缺口催办，旧消息没记 notif_id
    conn = _conn()
    try:
        conn.execute("INSERT INTO nudge_log (project_id, obj_type, obj_key, stage, to_user_id, status, subject, area, link, sent_at) "
                     "VALUES (?,?,?,?,?,'sent',?,?,?,?)", (env["project_id"], "datagap", "RD110:x:1", 1, env["users"]["tech1"],
                                                          subj, "gap", "home", dbmod.now_iso()))
    finally:
        conn.close()
    n = {x["id"]: x for x in login("tech1").get("/api/notifications").json()}[nid]
    assert n["link"] == {"route": "home", "params": {}} and n["state"] is None    # 不错配到同名的已闭环隐患 #fid
    assert n["title"].endswith("…") and fid


def test_truncated_titles_get_ellipsis(env):
    _team(env)
    full = "临边洞口：右侧基坑边坡无防护栏杆，人员靠近存在坠落风险，需立即整改并设置警示标志，夜间加设照明"
    assert len(full) > 40
    fid = _finding(env, title=full)
    cut = _notif(env, "tech1", f"新派单：{full[:40]}", link={"route": "finding", "params": {"id": str(fid)}})
    whole = _notif(env, "tech1", "新派单：短标题", link={"route": "finding", "params": {"id": str(_finding(env, title='短标题'))}})
    nud = _notif(env, "tech1", f"催办提醒：{full[:22]}")
    conn = _conn()
    try:
        conn.execute("INSERT INTO nudge_log (project_id, obj_type, obj_key, stage, to_user_id, status, subject, area, link, notif_id, sent_at) "
                     "VALUES (?,?,?,?,?,'sent',?,?,?,?,?)", (env["project_id"], "finding", str(fid), 1, env["users"]["tech1"],
                                                            full[:60], "safety", f"finding?id={fid}", nud, dbmod.now_iso()))
    finally:
        conn.close()
    rows = {x["id"]: x for x in login("tech1").get("/api/notifications").json()}
    assert rows[cut]["title"] == f"新派单：{full[:40]}…"
    assert rows[nud]["title"] == f"催办提醒：{full[:22]}…"
    assert rows[whole]["title"] == "新派单：短标题"


def test_alert_list_and_detail_pass_connection(env, monkeypatch):
    from app.services import progress as prog
    seen = []
    real = prog._p6_data_date
    monkeypatch.setattr(prog, "_p6_data_date", lambda a, e, conn=None: seen.append(conn is not None) or real(a, e, conn))
    conn = _conn()
    try:
        imp = dbmod.insert(conn, "p6_imports", {"project_id": env["project_id"], "file_name": "a.xer", "sha256": "x", "data_date": "2026-10-03",
                                                "activity_count": 0, "relation_count": 0, "imported_at": dbmod.now_iso(), "imported_by": env["users"]["pm"]})
    finally:
        conn.close()
    aid = _alert(env, import_id=imp)
    _alert(env)
    pm = login("pm")
    items = pm.get("/api/alerts?status=").json()
    assert len(items) == 2 and all(seen) and len(seen) == 2
    d = pm.get(f"/api/alerts/{aid}").json()
    assert d["p6_data_date"] == "2026-10-03" and all(seen)


# ============================================================================ 前端：消息页

def test_notices_page_load_state_home_and_nowrap():
    js = read("views.js")
    tpl = js[js.index("const Notices"):]
    assert "<load-state" in tpl and ':error="err"' in tpl and '@retry="load"' in tpl
    assert "回到首页" in tpl and "回到工作台" not in js
    assert 'tiny muted nowrap">{{ fmtDT(n.created_at) }}' in tpl                     # S-5⑦ 日期不折行
    assert "n.state_text" in tpl                                                     # U-14 / F-07 灰标
    assert "更早的消息" in tpl                                                        # S-5② 翻页
    assert '@click="open(n)"' in tpl                                                 # S-5③④ 整张卡可点，点开即已读


def test_notices_open_marks_read_then_navigates_and_load_error_kept():
    out = run_js(r"""
      store.me = { user: { role: 'tech', name: 'x' }, unread: 3, project: { features: {} } };
      const went = [];
      const vm = { items: [], role: 'tech', noticeAction, store };
      // 1 点开未读消息：本地立即变已读，并把服务器返回的未读数写回红点
      const n = { id: 5, read_at: null, link: { route: 'finding', params: { id: '9' } } };
      const realGo = go; go = (name, p) => went.push([name, p]);
      Notices.methods.open.call(vm, n);
      await new Promise((r) => setTimeout(r, 20));
      // 2 已读的、没有跳转目标的消息：不再请求，只是没反应
      const m = { id: 6, read_at: '2026-10-07', link: null };
      Notices.methods.open.call(vm, m);
      go = realGo;
      // 3 加载失败：保留错误给 <load-state>，不一直“加载中”
      const ctx = { items: [], loaded: false, err: null, more: false, settle: Notices.methods.settle };
      await Notices.methods.load.call(ctx);
      // 4 读取时计为已读的条目，红点相应减少（上一步 401 会清掉登录态，这里重新放回）
      store.me = { user: { role: 'tech', name: 'x' }, unread: 4, project: { features: {} } };
      Notices.methods.settle.call({}, [{ settled: true }, { settled: true }, {}]);
      return { read: !!n.read_at, went, err: ctx.err && ctx.err.message, loaded: ctx.loaded, unread: store.me.unread,
               handled: noticeAction({ state: 'done', link: { route: 'finding', params: { id: '1' } } }, 'tech').label };
    """, {"/api/notifications/5/read": {"ok": True, "unread": 2}})
    assert out["read"] is True
    assert out["went"] == [["finding", {"id": "9"}]]
    assert out["err"] == "请先登录" and out["loaded"] is True   # 桩没有 /api/notifications，按未登录报错即“出错”分支
    assert out["unread"] == 2
    assert out["handled"] == "查看"                          # 已处理的事项不再写“去处理”


# ============================================================================ 前端：S-1 其余页面

@pytest.mark.parametrize("comp", ["LogEditor", "TodayLog", "LogList", "Alerts", "AlertDetail", "Reports", "ReportView"])
def test_views_pages_use_load_state(comp):
    js = read("views.js")
    start = js.index(f"const {comp} = {{")
    body = js[start:js.index("\n};", start)]
    assert "<load-state" in body, comp
    assert "err" in body, comp
    assert 'class="empty">加载中' not in body, comp


def test_report_view_load_error_is_kept():
    out = run_js(r"""
      const ctx = { id: 7, r: null, err: null, apply: ReportView.methods.apply };
      await ReportView.methods.load.call(ctx);
      return { err: ctx.err && ctx.err.message, r: ctx.r };
    """)
    assert out == {"err": "请先登录", "r": None}     # 桩没有这个接口，按未登录报错：错误留给 <load-state>


# ============================================================================ 前端：预警

def _card():
    js = read("views.js")
    return js[js.index("const AlertCard = {"):js.index("\n};", js.index("const AlertCard = {"))]


def test_alert_detail_shows_p6_data_date():
    card = _card()
    assert "W{{ a.id }}" not in card                                 # U-15⑤ 不露内部编号
    assert "P6 数据日期未知" in card and "'P6 数据日期 ' + p6Date" in card
    js = read("views.js")
    det = js[js.index("const AlertDetail = {"):]
    assert '<alert-card :a="a" :full="true"' in det                # D-1：布尔值显式传入；页面真渲染见 test_acceptance_fixes
    out = run_js(r"""
      const f = AlertCard.computed.p6Date;
      return [f.call({ a: { p6_data_date: '2026-10-03', explanation: { p6_data_date: '2026-09-01' } } }),
              f.call({ a: { p6_data_date: null, explanation: { p6_data_date: '2026-09-01' } } }),
              f.call({ a: { p6_data_date: null, explanation: null } })];
    """)
    assert out == ["2026-10-03", "2026-09-01", ""]


def test_alert_measure_due_placeholder_becomes_pending():
    out = run_js(r"""
      return [measureDueBad('N天内', '2026-09-20'), measureDueBad('N 天内', '2026-09-20'), measureDueBad('', '2026-09-20'),
              measureDueBad('不早于2026-09-20 / 2026-10-07', '2026-10-07'), measureDueBad('2026-10-01', '2026-10-07'),
              measureDueBad('2026-10-12', '2026-10-07'), measureDueBad('3天内', '2026-10-07')];
    """)
    assert out == [True, True, True, True, True, False, False]
    card = _card()
    assert "measureDueBad(m.due, a.as_of)" in card and "待定" in card


def test_alert_stale_analysis_unified_hint_and_readable_refs():
    card = _card()
    assert "已过期</span>" not in card and "基于旧判定" in card
    assert "L{{ r.log_id }}" not in card and "logRef(" in card
    out = run_js(r"""
      const ctx = { a: { logs: [{ log_id: 342, date: '2026-09-17', author: '钱志远' }] }, f: { risk_signals: [{ log_id: 7, date: '2026-09-20' }] } };
      const r = (e) => AlertCard.methods.logRef.call(ctx, e);
      return [r('L342'), r('L7'), r('L99'), r('A1010')];
    """)
    assert out == ["日志 09-17 钱志远", "日志 09-20", "日志", "A1010"]


def test_alert_feedback_inline_without_native_prompt():
    card = _card()
    assert "prompt(" not in card and "confirm(" not in card
    assert ">确认属实</button>" in card and ">误报</button>" in card        # 名称不变
    assert "FB_REASONS" in card
    out = run_js(r"""
      const posted = [];
      const ctx = { a: { id: 3 }, ask: 'false_alarm', note: '  ', sending: false, $emit: () => {} };
      await AlertCard.methods.fb.call(ctx, 'false_alarm');          // 误报原因必填：空的不提交
      const blocked = ctx.ask;
      return { blocked, toast: store.toast && store.toast.msg, reasons: Object.keys(FB_REASONS) };
    """)
    assert out["blocked"] == "false_alarm" and "原因" in out["toast"]
    assert set(out["reasons"]) == {"false_alarm", "closed"}


def test_alerts_gap_sentence_only_for_eng_sup():
    js = read("views.js")
    tpl = js[js.index("const Alerts = {"):js.index("const AlertDetail")]
    m = re.search(r'<template v-if="seesGaps">[^<]*没报，不是落后[^<]*</template>', tpl)
    assert m, "数据缺口说明须包在 seesGaps 里"
    assert ":text=\"statsDef + " in tpl and "stats.definition" not in tpl.split("computed:")[0]      # 命中率口径收进小问号（RJ 10-09）
    out = run_js(r"""
      const def = '人工反馈口径：确认属实/(确认属实+误报)，未反馈与系统关闭的不计入；数据缺口不计入预警准确率';
      store.me = { user: { role: 'manager', name: 'x' }, project: { features: {} } };
      const pm = [Alerts.computed.seesGaps.call({}), Alerts.computed.statsDef.call({ stats: { definition: def }, seesGaps: false })];
      store.me.user.role = 'eng_sup';
      const eng = [Alerts.computed.seesGaps.call({}), Alerts.computed.statsDef.call({ stats: { definition: def }, seesGaps: true })];
      return { pm, eng };
    """)
    assert out["pm"][0] is False and "数据缺口" not in out["pm"][1] and "人工反馈口径" in out["pm"][1]
    assert out["eng"][0] is True and "数据缺口" in out["eng"][1]


# ============================================================================ 前端：日志、计划、报表、成效

def test_log_rows_wording_tilde_and_minutes():
    js = read("views.js")
    lst = js[js.index("const logDateText"):js.index("// ---------------------------------------------------------------------------- 预警")]
    assert "（未生成）" not in lst and "还没整理" in lst                     # U-34
    assert "'~'" not in lst and '"～"' in lst                              # S-4 波浪号统一
    assert "+ '分'" not in lst and "分钟" in lst                            # S-4 时长
    assert "engHidden" in lst                                            # U-21 分包日志“全部”不列空草稿


def test_log_list_hides_empty_drafts_for_reviewers():
    out = run_js(r"""
      const rows = [{ id: 1, empty: false }, { id: 2, empty: true }, { id: 3 }];
      store.me = { user: { role: 'eng_sup', name: 'x' }, project: { features: {} } };
      const eng = LogList.computed.items.call({ rows }).map((x) => x.id);
      const hidden = LogList.computed.engHidden.call({ rows });
      store.me.user.role = 'tech';
      const tech = LogList.computed.items.call({ rows }).map((x) => x.id);
      return { eng, hidden, tech };
    """)
    assert out == {"eng": [1, 3], "hidden": 1, "tech": [1, 2, 3]}


def test_log_editor_flow_card_only_for_author():
    js = read("views.js")
    ed = js[js.index("const LogEditor = {"):js.index("data: ()", js.index("const LogEditor = {"))]
    assert re.search(r'v-if="editable && !\(log\.has_ai_draft \|\| log\.imported\)[^"]*">\s*<h2>今日日志流程', ed)
    assert "作者还未提交" in ed


def test_plan_float_integer_days():
    js = read("views.js")
    plan = js[js.index("const Plan = {"):js.index("const Reports = {")]
    assert "fmtDays(a.total_float_d)" in plan and "fmtN(a.total_float_d)" not in plan
    assert "fmtDays(f.total_float_d)" in _card()
    out = run_js("return [fmtDays(18.8), fmtDays(0), fmtDays(null), fmtDays(-2.4)];")
    assert out == ["19 天", "0 天", "—", "-2 天"]


def test_report_publish_requires_checking_numbers():
    js = read("views.js")
    rv = js[js.index("const ReportView = {"):js.index("\n};", js.index("const ReportView = {"))]
    assert "numsLeft" in rv and ':disabled="numsLeft > 0"' in rv and "已核实" in rv
    lst = js[js.index("const Reports = {"):js.index("const ReportView = {")]
    assert "发布时保留" in lst
    out = run_js(r"""
      const nums = [{ field: 'a', number: '1000' }, { field: 'b', number: '12' }];
      const f = ReportView.computed.numsLeft;
      return [f.call({ nums, checked: {}, r: { status: 'draft' } }), f.call({ nums, checked: { a1000: true }, r: { status: 'draft' } }),
              f.call({ nums, checked: { a1000: true, b12: true }, r: { status: 'draft' } }), f.call({ nums, checked: {}, r: { status: 'published' } })];
    """)
    assert out == [2, 1, 0, 0]


def test_metrics_overview_minutes_and_small_samples():
    js = read("views.js")
    m = js[js.index("const Metrics = {"):js.index("历史回测：")]
    assert '<small class="small">分</small>' not in m and "分钟" in m
    assert "dl.edit_ratio.n >= 5" in m


# ============================================================================ 追加：x_ui / x_nudge 文案

def test_log_view_subtitle_has_no_review_word():
    ui = read("x_ui.js")
    line = next(x for x in ui.splitlines() if x.strip().startswith("log_view:")).split("//")[0]   # 只看文案，不看行尾注释
    assert '["施工日志", "查看日志内容"]' in line and "审核" not in line


def test_nudge_chain_follows_d9():
    js = read("x_nudge.js")
    assert "项目经理待决策" not in js
    assert "进度类催办不发给分包安全员" in js and "中低风险隐患最多升级到安全主管" in js
    assert "v-if=\"'logs' in form.sources\"" in js
