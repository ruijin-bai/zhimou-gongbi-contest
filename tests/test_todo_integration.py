"""T-16 待办与整改的联调遗留（A 级）。

1. 整改提交被离线队列接管（{queued:true}）时就地提示“已暂存，联网后自动提交复核”；整改照片被拒（422 photo_before_dispatch）时就地给“改作随手拍”，
   调 window.QUICK.asSafety(file)（T-09a 提供）；
2. 待办里拍整改照片显式传 allowPortrait: true（D4）；
3. 隐蔽验收：工程主管待办有“待录入监理结果”并直达 #/conceal?id=；被监理退回的，编制人（含工程主管本人编制的）待办逐条带退回原因，
   退回条目超出上限时单独一行，不并进“N 份待补”；项目经理待办不出现隐蔽验收，PM_FORBIDDEN 只增不减；
4. 复核停在 pending 超过 10 分钟：写“AI 复核未开始，可人工闭环或退回”，人工闭环/退回照常可用；“重新让 AI 复核”仍只对 running/failed 开放（不改后端规则）；
5. 闭环人：拼条目时能拿到（findings.feedback_by）就带 closed_by_name，前端写“已闭环（某某确认）”，拿不到只写“已闭环”；
6. “催办与固定的提问”折叠标题：项目经理只写“固定的提问”，其他岗位不变。
"""
from contextlib import closing
from datetime import datetime, timedelta

from conftest import login
from test_todo import _conceal, _get, _src, _sources, world  # noqa: F401 — world 是共用夹具
from test_ui_fixes import read, run_js

from app import db as dbmod
from app.services import findings as find_svc
from app.services import todo as todo_svc

# T-06 时 PM_FORBIDDEN 的集合：只能收紧，不能放松
PM_FORBIDDEN_T06 = {"log_review", "log_low", "log_missing", "week_missing", "data_gap", "mapping", "candidate", "gate", "gate_more", "quick",
                    "ticket_risk", "ticket_pending", "conceal_gap", "conceal_todo", "conceal_result", "alert"}


# ============================================================================ 3. 隐蔽验收待办

def test_eng_sup_has_pending_result_item_that_opens_the_record(world):
    sub = _conceal(world, "submitted")
    e = _get(login("eng"))
    res = _src(e, "conceal_result")
    assert len(res) == 1
    it = res[0]
    assert it["status"] == "待录入监理结果" and it["action_label"] == "录入结果"
    assert it["action_route"] == f"conceal?id={sub}"                      # 前端 todoGo → #/conceal?id=
    assert "已报验" in it["reason"]
    # 只给工程主管：项目经理、安全主管都没有
    for u in ("pm", "ss"):
        assert not {"conceal_result", "conceal_todo", "conceal_gap"} & _sources(_get(login(u))), u


def test_rejected_record_reason_reaches_author_including_eng_sup(world):
    """工程主管自己编制、被监理退回的记录，也要逐条带退回原因出现在他的待办里（以前只有技术员有）。"""
    mine = _conceal(world, "rejected", by="eng", note="缺保护层实测")
    other = _conceal(world, "rejected", by="tech1", note="垫块不足")
    e = _get(login("eng"))
    back = [i for i in _src(e, "conceal_todo") if i["action_route"] == f"conceal?id={mine}"]
    assert back and "缺保护层实测" in back[0]["reason"] and back[0]["status"] == "监理退回"
    # 别人编制的退回不算工程主管要补的（他只负责录入监理结果）
    assert not [i for i in _src(e, "conceal_todo") if i["action_route"] == f"conceal?id={other}"]
    t = _get(login("tech1"))
    tb = [i for i in _src(t, "conceal_todo") if i["action_route"] == f"conceal?id={other}"]
    assert tb and "垫块不足" in tb[0]["reason"]


def test_many_rejected_not_folded_into_drafts_line(world):
    """退回超过逐条上限时另起一行“另有 N 份被监理退回”，不再并进“N 份待补”（草稿行只数草稿）。"""
    n = todo_svc.PER_SOURCE + 2
    for k in range(n):
        _conceal(world, "rejected", note=f"原因{k}")
    _conceal(world, "draft")
    t = _get(login("tech1"))
    ct = _src(t, "conceal_todo")
    each = [i for i in ct if i["action_route"].startswith("conceal?id=")]
    assert len(each) == todo_svc.PER_SOURCE and all(i["reason"].startswith("监理退回：原因") for i in each)
    more = [i for i in ct if "被监理退回" in i["title"]]
    assert len(more) == 1 and "2" in more[0]["title"]
    drafts = [i for i in ct if "待补" in i["title"]]
    assert len(drafts) == 1 and drafts[0]["title"].startswith("1 份")


def test_pm_forbidden_only_tightens_and_pm_sees_no_conceal(world):
    assert PM_FORBIDDEN_T06 <= todo_svc.PM_FORBIDDEN
    _conceal(world, "submitted", by="pm")
    _conceal(world, "rejected", by="pm", note="退回")
    assert not {"conceal_result", "conceal_todo", "conceal_gap"} & _sources(_get(login("pm")))


# ============================================================================ 4. 复核卡在 pending

def _set_review(world, verify: dict, updated_min_ago: int):
    at = (datetime.now().astimezone() - timedelta(minutes=updated_min_ago)).isoformat(timespec="seconds")
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE findings SET verify_json=?, updated_at=? WHERE id=?", (dbmod.dumps(verify), at, world["fid_review"]))


def test_pending_over_10_minutes_is_not_shown_as_running(world):
    _set_review(world, {"state": "pending", "started_at": None}, updated_min_ago=25)
    ss = login("ss")
    it = _src(_get(ss), "review")[0]
    assert it["detail"]["verify_text"] == "AI 复核未开始，可人工闭环或退回"
    assert "AI 复核未开始，可人工闭环或退回" in it["reason"] and "AI 复核中" not in it["reason"]
    assert [q["label"] for q in it["quick"]] == ["确认闭环", "退回重改"]       # 人工闭环 / 退回照常
    assert it["detail"]["reverify"] is False                                   # 后端规则不变：pending 不能“重新让 AI 复核”
    assert ss.post(f"/api/findings/{world['fid_review']}/reverify").status_code == 409


def test_pending_recent_still_running_wording(world):
    _set_review(world, {"state": "pending", "started_at": None}, updated_min_ago=3)
    it = _src(_get(login("ss")), "review")[0]
    assert it["detail"]["verify_text"] == "AI 复核中"


def test_stale_running_wording_unchanged(world):
    old = (datetime.now().astimezone() - timedelta(minutes=20)).isoformat(timespec="seconds")
    _set_review(world, {"state": "running", "started_at": old}, updated_min_ago=20)
    it = _src(_get(login("ss")), "review")[0]
    assert it["detail"]["verify_text"] == "AI 复核未完成，可人工复核" and it["detail"]["reverify"] is True


# ============================================================================ 5. 闭环人姓名

def _ctx_and_finding(world, fid):
    conn = dbmod.connect()
    u = dbmod.one(conn, "SELECT * FROM users WHERE id=?", (world["users"]["ss"],))
    f = [x for x in find_svc.list_for(conn, world["project_id"], agent="safety", status=None, limit=500) if x["id"] == fid][0]
    return conn, todo_svc.Ctx(conn, u), f


def test_closed_by_name_comes_from_feedback_by(world):
    fid = world["fid_review"]
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE findings SET status='resolved', feedback_by=?, resolved_at=? WHERE id=?", (world["users"]["ss"], dbmod.now_iso(), fid))
    conn, c, f = _ctx_and_finding(world, fid)
    try:
        d = todo_svc._review_item(c, f)["detail"]
        assert d["status"] == "resolved" and d["closed_by_name"] == "安全主管"
    finally:
        conn.close()


def test_closed_without_known_closer_has_no_name(world):
    fid = world["fid_review"]
    with closing(dbmod.connect()) as conn:
        conn.execute("UPDATE findings SET status='resolved', feedback_by=NULL WHERE id=?", (fid,))
    conn, c, f = _ctx_and_finding(world, fid)
    try:
        assert todo_svc._review_item(c, f)["detail"]["closed_by_name"] is None
    finally:
        conn.close()


def test_open_review_item_has_no_closer(world):
    it = _src(_get(login("ss")), "review")[0]
    assert it["detail"]["status"] == "rectifying" and it["detail"]["closed_by_name"] is None


# ============================================================================ 前端（node 加载全部脚本，桩掉 api / 相机）

STUBS = r"""
  const seen = { toasts: [], emits: [] };
  FormData = function () { this.append = () => {}; };
  takePhotos = async (o) => { seen.opts = o; return [FILE]; };
  addPhotos = () => {};
  toast = (m, e) => { seen.toasts.push([m, !!e]); };
  const vmOf = () => ({ it: { finding: { id: 7 }, inline: 'rectify', quick: [] }, busy: false, note: '', rej: null, $emit: (e) => seen.emits.push(e) });
"""


def test_rectify_queued_shows_inline_note_and_portrait_allowed():
    out = run_js(r"""
      const FILE = { name: 'r.jpg', size: 3 };
    """ + STUBS + r"""
      api = async (m, u) => { if (u === '/api/media') return [{ id: 'local:m1', queued: true }]; if (u === '/api/findings/7/rectify') return { queued: true, client_id: 'a1', finding_id: 7 }; };
      const vm = vmOf();
      await TodoItem.methods.rectify.call(vm);
      return { opts: seen.opts, note: vm.note, toasts: seen.toasts, emits: seen.emits, busy: vm.busy };
    """)
    assert out["opts"]["allowPortrait"] is True and out["opts"]["multi"] is False
    assert out["note"] == "已暂存，联网后自动提交复核"
    assert ["已暂存，联网后自动提交复核", False] in out["toasts"]
    assert out["emits"] == [] and out["busy"] is False                   # 不刷新列表（断网刷新只会失败），补传成功后由 zb-quick-synced 刷新


def test_rectify_rejected_offers_as_safety_inline():
    out = run_js(r"""
      const FILE = { name: 'r.jpg', size: 3 };
    """ + STUBS + r"""
      api = async (m, u) => {
        if (u === '/api/media') return [{ id: 12 }];
        const e = new Error('整改照片拍摄于 10-01 08:00，早于派单时间超过 24 小时'); e.status = 422; e.code = 'photo_before_dispatch'; throw e;
      };
      window.QUICK = { asSafety: async (f) => { seen.as = f === FILE; return true; } };
      const vm = vmOf();
      await TodoItem.methods.rectify.call(vm);
      const mid = { rej: !!vm.rej, note: vm.note, errToasts: seen.toasts.filter((t) => t[1]).length };
      await TodoItem.methods.asSafety.call(vm);
      return { mid, as: seen.as, rej: vm.rej, note: vm.note, toasts: seen.toasts };
    """)
    assert out["mid"]["rej"] is True and "早于派单" in out["mid"]["note"]
    assert out["mid"]["errToasts"] == 0                                     # 不是一闪而过的报错，而是就地给出可操作的选项
    assert out["as"] is True and out["rej"] is None
    assert "已改作随手拍" in out["note"]


def test_rectify_other_errors_still_toast():
    out = run_js(r"""
      const FILE = { name: 'r.jpg', size: 3 };
    """ + STUBS + r"""
      api = async (m, u) => { if (u === '/api/media') return [{ id: 12 }]; const e = new Error('该隐患当前不需要整改'); e.status = 400; throw e; };
      const vm = vmOf();
      await TodoItem.methods.rectify.call(vm);
      return { rej: vm.rej, toasts: seen.toasts };
    """)
    assert out["rej"] is None and ["该隐患当前不需要整改", True] in out["toasts"]


def test_todo_item_template_has_inline_note_and_as_safety_button():
    """模板语法由 test_pagehead 的 _compile_harness 统一编译检查；这里只核对就地提示与按钮在“先办这件”主卡、卡片和表格行各有一处（第二轮加了主卡形态）。"""
    out = run_js("return { item: TodoItem.template, body: TodoBody.template };")
    assert out["item"].count("改作随手拍") == 3 and out["item"].count("todo-note") == 3
    assert "todoClosedText(it.detail)" in out["body"]


def test_takephotos_call_in_todo_passes_allow_portrait_explicitly():
    src = read("todo.js")
    assert "takePhotos({ multi: false, allowPortrait: true })" in src


def test_closed_text_helper():
    out = run_js(r"""
      return [todoClosedText({ status: 'resolved', closed_by_name: '王安全' }), todoClosedText({ status: 'resolved', closed_by_name: null }),
              todoClosedText({ status: 'rectifying', closed_by_name: null }), todoClosedText(null)];
    """)
    assert out == ["已闭环（王安全确认）", "已闭环", "", ""]


def test_pinned_title_by_role():
    out = run_js(r"""
      const t = (role) => TodoAttention.computed.moreTitle.call({ d: { role } });
      return { pm: t('manager'), eng: t('eng_sup'), ss: t('safety_sup') };
    """)
    assert out == {"pm": "固定的提问", "eng": "催办与固定的提问", "ss": "催办与固定的提问"}
    assert "{{ moreTitle }}" in read("todo.js")
