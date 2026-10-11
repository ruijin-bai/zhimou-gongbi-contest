"""体检 F3/F4/F5/G：消息能点进对应单据、技术员预警直达随手记、技术员日志按周分组、预警页阈值文案。

- F3 notifications 加可空列 link_json（{"route", "params"}）；旧库启动自动补列，旧记录为空；
  现场圈评论/回复/@ → 现场圈，派单/催办/升级催办 → 隐患详情，周确认提醒 → 周确认页，日志退回 → 该日志；
  消息页有跳转目标才放按钮（去回复/去处理/去确认），预警消息维持原逻辑。
- F4 技术员收到的预警：按钮“去说明原因”到首页随手记；管理岗仍“查看预警”。
- F5 技术员施工日志：默认只显示本周和上周、按周分组，更早的折叠；隐藏空草稿；工程主管“分包日志”不变。
- G 预警页“阈值在 项目与规则 中调整”只对系统管理员显示。
"""
import json
from datetime import date, datetime, timedelta

from conftest import login
from test_frontline_f import fn_src, read, run_node
from test_nudge import _finding, _run
from test_roles import _team
from test_trust import _draft_today, _setup_daily, _submit

from app import db as dbmod
from app import main as main_mod

T0 = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)


def _rows(uid, like="%"):
    conn = dbmod.connect()
    try:
        return conn.execute("SELECT * FROM notifications WHERE user_id=? AND title LIKE ? ORDER BY id", (uid, like)).fetchall()
    finally:
        conn.close()


def _link(row):
    return json.loads(row["link_json"]) if row["link_json"] else None


# ----------------------------------------------------------------------------- 迁移
def test_old_db_without_link_column_gets_it_on_init(env):
    conn = dbmod.connect()
    conn.execute("INSERT INTO notifications (project_id, user_id, title, body, created_at) VALUES (?,?,?,?,?)",
                 (env["project_id"], env["users"]["tech1"], "旧消息", "旧内容", dbmod.now_iso()))
    conn.execute("ALTER TABLE notifications DROP COLUMN link_json")
    conn.execute("PRAGMA user_version=8")
    conn.close()
    for _ in range(2):  # 重复启动不报错
        dbmod._initialized = False
        dbmod.init_db()
    conn = dbmod.connect()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(notifications)").fetchall()}
    assert "link_json" in cols
    assert conn.execute("PRAGMA user_version").fetchone()["user_version"] == dbmod.SCHEMA_VERSION >= 9
    old = conn.execute("SELECT * FROM notifications WHERE title='旧消息'").fetchone()
    assert old["body"] == "旧内容" and old["link_json"] is None
    conn.close()
    # 接口：旧记录 link 为空
    items = login("tech1").get("/api/notifications").json()
    assert items[0]["title"] == "旧消息" and items[0]["link"] is None


# ----------------------------------------------------------------------------- 现场圈评论/回复/@
def test_circle_comment_reply_mention_link_to_circle(env):
    _team(env)
    t1, sa, eng = login("tech1"), login("sa_a"), login("eng")
    n1 = t1.post("/api/notes", json={"text": "3号墩承台浇完"}).json()
    key = f"note:{n1['id']}"
    c = eng.post("/api/feed/comments", json={"key": key, "text": "收到 @安全员甲 注意养护"}).json()
    sa.post("/api/feed/comments", json={"key": key, "text": "好的", "reply_to": c["id"]})
    for who, like in (("tech1", "%评论了你的现场动态"), ("sa_a", "%在评论中提到了你"), ("eng", "%回复了你的评论")):
        rows = _rows(env["users"][who], like)
        assert rows, who
        assert _link(rows[-1]) == {"route": "circle", "params": {"key": key}}, who
    api = next(n for n in t1.get("/api/notifications").json() if "评论了你的现场动态" in n["title"])
    assert api["link"] == {"route": "circle", "params": {"key": key}} and "link_json" not in api


# ----------------------------------------------------------------------------- 派单/催办/升级催办 → 隐患详情
def test_dispatch_and_nudge_link_to_finding(env):
    _team(env)
    fid = _finding(env, None)
    ss = login("ss")
    assert ss.put(f"/api/findings/{fid}/assignee", json={"assignee_id": env["users"]["tech1"]}).status_code == 200
    assert ss.post(f"/api/findings/{fid}/nudge", json={"text": "今天完成"}).status_code == 200
    want = {"route": "finding", "params": {"id": str(fid)}}
    assert _link(_rows(env["users"]["tech1"], "新派单%")[-1]) == want
    assert _link(_rows(env["users"]["tech1"], "催办：%")[-1]) == want


def test_candidate_confirm_dispatch_links_to_finding(env):
    from test_candidate import _cand
    _, fid = _cand(env)
    assert login("sa_a").post(f"/api/findings/{fid}/confirm", json={"assignee_id": env["users"]["tech1"]}).status_code == 200
    assert _link(_rows(env["users"]["tech1"], "新派单%")[-1]) == {"route": "finding", "params": {"id": str(fid)}}


def test_auto_nudge_escalation_links_to_finding(env):
    _team(env)
    fid = _finding(env, "tech1")
    _run(env, T0)
    _run(env, T0 + timedelta(hours=25))
    want = {"route": "finding", "params": {"id": str(fid)}}
    assert _link(_rows(env["users"]["tech1"], "催办提醒%")[-1]) == want
    assert _link(_rows(env["users"]["sa_a"])[-1]) == want  # 升级到安全员也能直达


def test_auto_nudge_log_targets_link_only_for_tech(env):
    from app.services import nudge as svc
    # 周确认未提交的催办：技术员 → 周确认页；升级给非技术员时不带“周确认页”这种技术员专属路由
    assert svc._notif_link([{"link": "week", "overdue": 1}], {"role": "tech"}) == {"route": "week", "params": {}}
    assert svc._notif_link([{"link": "week", "overdue": 1}], {"role": "sub_safety"}) is None
    assert svc._notif_link([{"link": "home", "overdue": 1}], {"role": "eng_sup"}) is None
    assert svc._notif_link([{"link": "", "overdue": 1}], {"role": "tech"}) is None
    # 一条消息合并多件事：取超期最久的那件
    items = [{"link": "finding?id=3", "overdue": 1}, {"link": "finding?id=7", "overdue": 5}]
    assert svc._notif_link(items, {"role": "safety_sup"}) == {"route": "finding", "params": {"id": "7"}}


# ----------------------------------------------------------------------------- 周确认提醒 → 周确认页
def test_weekly_reminder_links_to_week(env, monkeypatch):
    conn = dbmod.connect()
    conn.execute("UPDATE projects SET config_json='{\"cadence\":\"week\",\"daily_log\":false}'")
    conn.close()

    class Fri(date):
        @classmethod
        def today(cls):
            d = date.today()
            return d + timedelta(days=(4 - d.weekday()) % 7)
    monkeypatch.setattr(main_mod, "date", Fri)
    main_mod._evening(env["project_id"])
    rows = _rows(env["users"]["tech1"], "请确认本周进度")
    assert rows and _link(rows[-1]) == {"route": "week", "params": {}}


# ----------------------------------------------------------------------------- 日志退回 → 该日志
def test_log_return_notifies_author_with_link(env):
    pm, tech = _setup_daily()
    d = _draft_today(tech)
    _submit(tech, d["id"], d["version"])
    assert login("admin").post(f"/api/logs/{d['id']}/review", json={"action": "return", "note": "桩数与现场不符"}).status_code == 200  # 项目经理不审核日志
    rows = _rows(env["users"]["tech1"], "%退回%")
    assert len(rows) == 1 and "桩数与现场不符" in rows[0]["body"]
    assert _link(rows[0]) == {"route": "log", "params": {"id": str(d["id"])}}
    for w in ("产值", "填报率"):
        assert w not in rows[0]["title"] + rows[0]["body"]


# ----------------------------------------------------------------------------- 预警消息：技术员不带 link（前端按岗位处理），管理岗原逻辑
def test_alert_notifications_keep_alert_id_without_link(monkeypatch):
    from test_frontline_f import _notify
    mgr, tech = _notify(monkeypatch)
    assert mgr["alert_id"] == 1 and tech["alert_id"] == 1
    assert not mgr.get("link_json") and not tech.get("link_json")


# ----------------------------------------------------------------------------- 前端：消息页按钮（F3 + F4）
def test_notices_action_buttons():
    src = read("views.js")
    out = run_node(fn_src(src, "noticeAction") + """
const L = (r, p) => JSON.stringify({ route: r, params: p || {} });
process.stdout.write(JSON.stringify([
  noticeAction({ link: { route: 'circle', params: { key: 'note:1' } } }, 'tech'),
  noticeAction({ link: { route: 'finding', params: { id: '5' } } }, 'sub_safety'),
  noticeAction({ link: { route: 'week', params: {} } }, 'tech'),
  noticeAction({ link: { route: 'log', params: { id: '9' } } }, 'tech'),
  noticeAction({ alert_id: 3 }, 'tech'),
  noticeAction({ alert_id: 3 }, 'manager'),
  noticeAction({ alert_id: null, link: null }, 'tech'),
  noticeAction({}, 'eng_sup'),
]));""")
    circle, finding, week, log, tech_alert, mgr_alert, none1, none2 = out
    assert circle == {"label": "去回复", "route": "circle", "params": {"key": "note:1"}}
    assert finding["label"] == "去处理" and finding["route"] == "finding" and finding["params"] == {"id": "5"}
    assert week["label"] == "去确认" and week["route"] == "week"
    assert log["label"] == "去处理" and log["route"] == "log" and log["params"] == {"id": "9"}
    assert tech_alert == {"label": "去说明原因", "route": "home", "params": {}, "focus": "note"}
    assert mgr_alert == {"label": "查看预警", "route": "alert", "params": {"id": 3}}
    assert none1 is None and none2 is None
    tpl = src[src.index("const Notices"):]
    assert "noticeAction(n, role)" in tpl and "runAction(" in tpl
    assert "查看预警 →" not in tpl  # 文案统一由 noticeAction 给出


# ----------------------------------------------------------------------------- 前端：技术员日志按周分组（F5）
def test_tech_log_list_groups_by_week_and_hides_empty_drafts():
    src = read("views.js")
    out = run_node(fn_src(src, "logIsEmptyDraft") + "\n" + fn_src(src, "logWeekGroups") + """
const it = (id, d, extra) => Object.assign({ id, log_date: d, status: 'submitted', summary: '有内容', photos: 0, issues: 0, period: 'day' }, extra || {});
const items = [
  it(1, '2026-10-07'), it(2, '2026-10-05'),                          // 本周（周一 10-05）
  it(3, '2026-10-06', { status: 'draft', summary: '', photos: 0, empty: true }),  // 本周空草稿 → 隐藏
  it(4, '2026-10-02'), it(5, '2026-09-28', { period: 'week', period_end: '2026-10-04' }),  // 上周
  it(6, '2026-09-30', { status: 'draft', summary: '', photos: 2 }),  // 有照片的草稿保留
  it(7, '2026-09-21'), it(8, '2026-09-01'),                          // 更早
  it(9, '2026-09-02', { status: 'draft', summary: '  ', photos: 0, empty: true }), // 更早的空草稿 → 隐藏
  it(10, '2026-10-07', { status: 'draft', summary: '', photos: 0, empty: true }), // 今天的草稿：随手记可能还没整理，不隐藏
];
const r = logWeekGroups(items, '2026-10-07');
process.stdout.write(JSON.stringify({
  recent: r.recent.map((g) => [g.label, g.items.map((x) => x.id)]),
  older: r.older.map((g) => [g.label, g.items.map((x) => x.id)]),
  olderN: r.olderN, hidden: r.hidden,
  empty: [logIsEmptyDraft(it(1, '2026-10-01', { status: 'draft', summary: '', empty: true }), '2026-10-07'),
          logIsEmptyDraft(it(1, '2026-10-01', { status: 'returned', summary: '', empty: false }), '2026-10-07'),
          logIsEmptyDraft(it(1, '2026-10-07', { status: 'draft', summary: '', empty: true }), '2026-10-07')],   // 今天的不藏
}));""")
    assert out["recent"] == [["本周（10-05～10-11）", [1, 2, 10]], ["上周（09-28～10-04）", [4, 5, 6]]]
    assert out["older"] == [["09-21～09-27", [7]], ["08-31～09-06", [8]]]
    assert out["olderN"] == 2 and out["hidden"] == 2
    assert out["empty"] == [True, False, False]
    tpl = src[src.index("const LogList"):src.index("// ---------------------------------------------------------------------------- 预警")]
    # 10-10 一屏化：更早的周不再“展开追加”，按周归组后接在后面几页（zb-paged 内部分页）
    assert "grp.older" in tpl and '<zb-paged v-if="isTech()"' in tpl and ":items=\"techRows\"" in tpl
    # 只对技术员分组；工程主管“分包日志”仍是原来的平铺表（同样分页）
    assert ':items="items"' in tpl


# ----------------------------------------------------------------------------- 前端：预警页阈值文案（G）
def test_alerts_threshold_hint_only_for_admin():
    src = read("views.js")
    tpl = src[src.index("const Alerts"):]
    tpl = tpl[:tpl.index("\n};")]
    assert "阈值于「设置」调整" not in src
    assert '<template v-if="isAdmin">阈值在 项目与规则 中调整。</template>' in tpl
    assert 'isAdmin: () => roleOf() === "admin"' in tpl
