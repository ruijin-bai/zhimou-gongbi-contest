"""T-12a UI 低级错误（无主文件车道）。

A 级：
- S-1 统一的加载/出错状态组件 <load-state> 与全局错误兜底（errorHandler、unhandledrejection）：加载失败不白屏、不一直“加载中”；
- F-14 项目经理直达 #/logs、#/week 时显示“不在项目经理的查看范围”；
- AC-6.7 项目经理首页没有任何指向 #/chase 的入口，固定的提问保留；
- N-2 现场圈点评与接力里的整改复核按 4.4 verify 契约写“AI 建议……”，隐患由人确认闭环之前不出现“已消除”一类肯定说法；
- G-16 tools/role_shots.mjs 必须指定 --base，并拒绝 8600、8601。

前端部分用 node 在 vm 里按 index.html 的顺序加载全部脚本（做法同 _compile_harness.js），fetch 换成桩，直接调组件的
computed / 方法与纯函数，不渲染。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import login
from test_rectify_review import _ai, _dispatched, _finding, _photo, _set_verify

from app import db as dbmod

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
NODE = shutil.which("node")

LOADER = r"""
const vm = require('vm'), fs = require('fs'), path = require('path');
const dir = process.argv[2];
const REPLIES = /*REPLIES*/;
const noop = () => {};
const ev = {};
function el() { return { style: {}, setAttribute: noop, appendChild: noop, childNodes: [], children: [], textContent: '', innerHTML: '' }; }
const ctx = { console: { log: noop, warn: noop, error: noop, info: noop, debug: noop }, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: noop,
  navigator: { userAgent: '' }, localStorage: { getItem: () => null, setItem: noop, removeItem: noop }, sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  location: { hash: '', reload: () => { ctx.__reloaded = (ctx.__reloaded || 0) + 1; } }, addEventListener: (t, fn) => { ev[t] = fn; },
  document: { addEventListener: noop, createElement: el, querySelector: () => null, documentElement: { style: {} }, hidden: false },
  fetch: async (url) => {
    const u = String(url).split('?')[0];
    if (!(u in REPLIES)) return { status: 401, ok: false, json: async () => ({ detail: '请先登录' }) };
    return { status: 200, ok: true, json: async () => REPLIES[u] };
  } };
ctx.window = ctx; ctx.self = ctx; ctx.__ev = ev;
for (const n of ['Image', 'FormData', 'Blob', 'File', 'XMLHttpRequest', 'MediaRecorder', 'ResizeObserver', 'IntersectionObserver', 'HTMLElement', 'Element', 'Event', 'CustomEvent']) ctx[n] = function () {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(dir, 'vendor/vue.global.prod.js'), 'utf8'), ctx);
const order = fs.readFileSync(path.join(dir, 'index.html'), 'utf8').match(/\/static\/[\w.\/-]+\.js/g).filter((x) => !x.includes('vendor'));
let src = '';
for (const f of order) src += fs.readFileSync(path.join(dir, f.replace(/^\/static\//, '')), 'utf8') + '\n;';
ctx.__done = (o) => process.stdout.write(JSON.stringify(o === undefined ? null : o), () => process.exit(0));
src += `\n;(async () => { await new Promise((r) => setTimeout(r, 30)); return await (async () => {\n/*SNIPPET*/\n})(); })()
  .then((o) => __done(o)).catch((e) => __done({ __err: String((e && e.stack) || e) }));`;
vm.runInContext(src, ctx);
"""


def run_js(snippet: str, replies: dict | None = None):
    if not NODE:
        pytest.skip("没有 node，跳过前端校验")
    h = Path(tempfile.mkdtemp(prefix="zb_ui_")) / "ui_fix_harness.js"
    h.write_text(LOADER.replace("/*REPLIES*/", json.dumps(replies or {}, ensure_ascii=False)).replace("/*SNIPPET*/", snippet), encoding="utf-8")
    r = subprocess.run([NODE, str(h), str(STATIC)], capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert r.returncode == 0, r.stderr[:800]
    out = json.loads(r.stdout)
    assert not (isinstance(out, dict) and "__err" in out), out
    return out


def read(n):
    return (STATIC / n).read_text(encoding="utf-8")


# ============================================================================ S-1 加载与出错状态

def test_load_state_component_is_global_and_explains_errors():
    main = read("main.js")
    assert 'app.component("load-state", LoadState)' in main
    out = run_js(r"""
      const off = new Error('网络不可用，请检查网络后重试'); off.offline = true;
      const e500 = new Error('请求失败（500）'); e500.status = 500;
      const e404 = new Error('隐患不存在'); e404.status = 404;
      const r = (error) => LoadState.computed.reason.call({ error });
      Vue.compile(LoadState.template, { onError: (e) => { throw e; } });
      return { off: r(off), e500: r(e500), e404: r(e404), str: r('自定义原因'), none: r(null), props: Object.keys(LoadState.props),
               tpl: LoadState.template, errText: typeof errText };
    """)
    assert "信号" in out["off"] or "网络" in out["off"]
    assert "服务器" in out["e500"] and "500" in out["e500"]
    assert out["e404"] == "隐患不存在"           # 后端给了具体原因就照写
    assert out["str"] == "自定义原因" and out["none"] == ""
    assert {"loading", "error"} <= set(out["props"]) and out["errText"] == "function"
    # 出错时有可读原因和“重试”，优先于“加载中”；都没有时渲染默认插槽
    tpl = out["tpl"]
    assert "重试" in tpl and "加载中" in tpl and "<slot" in tpl
    assert tpl.index('v-if="error"') < tpl.index('v-else-if="loading"')


def test_load_state_retry_calls_handler_or_reloads():
    out = run_js(r"""
      let n = 0;
      LoadState.methods.retry.call({ onRetry: () => { n++; } });
      LoadState.methods.retry.call({ onRetry: undefined });
      return { n, reloaded: __reloaded || 0 };
    """)
    assert out == {"n": 1, "reloaded": 1}


def test_global_error_handler_shows_reason_instead_of_blank_page():
    main = read("main.js")
    assert "installErrorHandling(app)" in main
    out = run_js(r"""
      const app = { config: {} };
      installErrorHandling(app);
      const res = {};
      // 0 未登录（登录页）时不提示
      app.config.errorHandler(new Error('x'), null, 'https://vuejs.org/error-reference/#runtime-m');
      res.loggedOut = PAGE.err;
      store.me = { user: { role: 'tech', name: 'x' }, project: { features: {} } };
      // 1 生命周期里加载失败（如事项详情 mounted 里 GET 失败）：页面顶部给出原因和“重试”
      const e1 = new Error('隐患不存在'); e1.status = 404;
      app.config.errorHandler(e1, null, 'https://vuejs.org/error-reference/#runtime-m');
      res.hook = PAGE.err;
      // 2 重试：清掉错误并重新打开本页（routeKey 变化）
      const k0 = Root.computed.routeKey.call({});
      Root.methods.retryPage.call({});
      res.retried = { err: PAGE.err, changed: Root.computed.routeKey.call({}) !== k0 };
      // 3 没接住的 promise 失败（各页 mounted 里 this.load() 不 await）同样兜底
      const off = new Error('网络不可用'); off.offline = true;
      let prevented = false;
      __ev.unhandledrejection({ reason: off, preventDefault() { prevented = true; } });
      res.rej = PAGE.err; res.prevented = prevented;
      // 4 按钮点击出错（runtime-5/6）只弹提示，不占页面
      PAGE.err = null;
      const e2 = new Error('保存失败'); e2.status = 400;
      app.config.errorHandler(e2, null, 'https://vuejs.org/error-reference/#runtime-5');
      res.click = { err: PAGE.err, toast: store.toast && store.toast.msg };
      // 5 401 已经回登录页，不再提示
      const e401 = new Error('请先登录'); e401.status = 401;
      app.config.errorHandler(e401, null, 'https://vuejs.org/error-reference/#runtime-m');
      res.unauth = PAGE.err;
      // 6 换页清掉上一页的错误
      PAGE.err = '旧错误'; store.route = { name: 'circle', params: {} };
      await Vue.nextTick(); await new Promise((r) => setTimeout(r, 0));
      res.routeCleared = PAGE.err;
      return res;
    """)
    assert out["loggedOut"] is None
    assert out["hook"] == "隐患不存在"
    assert out["retried"] == {"err": None, "changed": True}
    assert "网络" in out["rej"] or "信号" in out["rej"]
    assert out["prevented"] is True
    assert out["click"] == {"err": None, "toast": "保存失败"}
    assert out["unauth"] is None
    assert out["routeCleared"] is None


def test_root_template_has_page_error_banner_and_keeps_invariants():
    main = read("main.js")
    tpl = main[main.index("template: `"):main.index("data: () =>")]
    # 页面级兜底就在 <main> 里、页面组件之前，复用 load-state
    m = tpl[tpl.index("<main"):tpl.index("</main>")]
    assert re.search(r'<load-state[^>]*:error="page\.err"[^>]*@retry="retryPage"', m)
    assert m.index("page.err") < m.index("<circle-feed")
    assert main.count("hubbar") == 1


# ============================================================================ F-14 项目经理口径的路由拦截

def test_pm_out_of_scope_routes():
    out = run_js(r"""
      const f = (role, name) => pmOutOfScope(role, name);
      return { pm: ['logs', 'week', 'capture', 'manual'].map((n) => f('manager', n)),
               pmOk: ['work', 'log', 'circle', 'progress', 'safety', 'quality', 'reports', 'finding', 'alert'].map((n) => f('manager', n)),
               others: ['eng_sup', 'tech', 'safety_sup', 'leader'].map((r) => f(r, 'logs') || f(r, 'week')),
               rootPm: Root.computed.outOfScope.call({ role: 'manager', r: { name: 'week', params: {} } }),
               rootEng: Root.computed.outOfScope.call({ role: 'eng_sup', r: { name: 'logs', params: {} } }) };
    """)
    assert out["pm"] == [True] * 4
    assert out["pmOk"] == [False] * 9          # 日志详情（周报引用、现场圈链接）仍可打开，字段由后端裁剪
    assert out["others"] == [False] * 4
    assert out["rootPm"] is True and out["rootEng"] is False


def test_pm_out_of_scope_branch_comes_first_and_is_plain():
    main = read("main.js")
    tpl = main[main.index("<main"):main.index("</main>")]
    i = tpl.index('v-if="outOfScope"')
    assert i < tpl.index("<log-list") and i < tpl.index("<tech-log") and i < tpl.index("<circle-feed")
    tag = tpl[tpl.rfind("<", 0, i):tpl.index(">", i)]
    assert "不在项目经理的查看范围" in tag
    for w in ("填报", "缺报", "没记录", "数据审核", "确认率", "产值"):
        assert w not in tag, w
    # 页面组件改成同一条 v-if 链，被拦截时一个都不渲染
    assert '<circle-feed v-else-if="r.name === \'circle\'"' in tpl


# ============================================================================ AC-6.7 项目经理首页没有催办台入口

NUDGE_REPLIES = {
    "/api/nudge/today": {"enabled": True, "objects": 3, "persons": 2, "escalated_n": 1, "deferred_quiet": 0, "deferred_cap": 0},
    "/api/nudge/pins": [{"id": 7, "question": "本周 3# 墩进度怎样", "last_run_at": "2026-10-07T08:00", "last_error": "",
                         "answer": {"answer": "按计划推进 [L12]", "refs": ["L12"]}}],
}


def _feed_for(role):
    return run_js(r"""
      store.me = { user: { role: '%s', name: 'x' }, project: { features: {} } };
      const items = await nudgeFeedItems();
      const home = { manager: 'work', eng_sup: 'eng', safety_sup: 'sdash' }['%s'];
      const all = await EXT.collect(home);
      return { items, all };
    """ % (role, role), NUDGE_REPLIES)


def test_pm_home_has_no_chase_entry_but_keeps_pins():
    out = _feed_for("manager")
    blob = json.dumps(out, ensure_ascii=False)
    assert "chase" not in blob and "催办台" not in blob and "催办Agent" not in blob
    assert [i["agent"] for i in out["items"]] == ["问工笔"] and out["items"][0]["title"] == "本周 3# 墩进度怎样"
    assert [a["label"] for a in out["items"][0]["actions"]][0] == "刷新"


def test_supervisors_still_reach_chase_desk():
    out = _feed_for("eng_sup")
    chase = [i for i in out["items"] if i["agent"] == "催办Agent"]
    assert chase and chase[0]["actions"] == [{"label": "催办记录", "route": "chase"}]


# ============================================================================ N-2 现场圈点评与接力的整改复核口径

AFFIRM = re.compile(r"已(?:确认)?消除|已闭环|整改到位")


def _rect_key(tech, mid):
    key = f"rect:{mid}"
    assert any(p["key"] == key for p in tech.get("/api/feed?limit=50").json()["posts"])
    return key


def _relay_safety(c, key):
    steps = c.get(f"/api/circle/relay?key={key}").json()["steps"]
    return [s["text"] for s in steps if s["agent"] == "safety"]


def _safety_comment(c, key):
    c.post("/api/circle/review", json={"keys": [key]})
    post = next(p for p in c.get("/api/feed?limit=50").json()["posts"] if p["key"] == key)
    return next(x for x in post["comments"] if x.get("agent") and x["agent"]["view"] == "safety")


def test_rectify_review_is_ai_suggestion_until_human_closes(env):
    tech, ss, fid = _dispatched(env)
    m = _photo(tech)
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": m["id"]}).status_code == 200
    key = _rect_key(tech, m["id"])
    # 接力：AI 结论只是建议
    texts = _relay_safety(ss, key)
    assert any("AI 建议：已消除，待安全主管确认" in t for t in texts), texts
    assert not any("已确认消除" in t or "复核未通过" in t for t in texts)
    # 点评：正文与依据都不出现肯定说法
    c = _safety_comment(ss, key)
    blob = c["text"] + " ".join(e["text"] for e in c["agent"]["evidence"])
    assert "AI 建议：已消除，待安全主管确认" in blob
    for s in re.split(r"[。！？\n；]", blob):
        assert not (AFFIRM.search(s) and not re.search("建议|待", s)), s
    assert not c["agent"]["stale"]
    # 安全主管确认闭环之后才写“已闭环（谁确认）”
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 200
    assert any("已闭环（安全主管确认）" in t for t in _relay_safety(ss, key))


def test_relay_shows_pending_failed_and_returned_states(env, monkeypatch):
    tech, ss, fid = _dispatched(env)
    _ai(monkeypatch, verdict={"resolved": False, "same_scene": True, "reason": "临边仍无防护", "remaining": "临边防护缺失"})
    m = _photo(tech)
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": m["id"]}).status_code == 200
    key = _rect_key(tech, m["id"])
    texts = _relay_safety(ss, key)
    assert any("AI 建议：仍存在临边防护缺失" in t for t in texts)
    assert not any("识别中" in t for t in texts), texts          # 整改照片不做隐患识别，不再挂一条永远的“识别中”
    _set_verify(fid, state="failed", suggest=None)
    assert any("AI 复核失败，待人工复核" in t for t in _relay_safety(ss, key))
    _set_verify(fid, state="pending")
    assert any("AI 复核中" in t for t in _relay_safety(ss, key))
    # 安全主管退回重改：不再写“待安全主管确认”
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "open", "note": "防护栏高度不够"}).status_code == 200
    texts = _relay_safety(ss, key)
    assert any("安全主管已退回重改" in t for t in texts) and not any("待安全主管确认" in t for t in texts)


def test_ai_comment_claiming_closure_falls_back_to_template(env, monkeypatch):
    from app.ai import client
    tech, ss, fid = _dispatched(env)
    m = _photo(tech)
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": m["id"]}).status_code == 200
    orig = client.call_json

    def bad(kind, **kw):
        if kind == "feed.comment":
            kw["mock"] = lambda: {"comments": [{"view": "safety", "text": "整改到位，隐患已消除，辛苦了"}]}
        return orig(kind, **kw)
    monkeypatch.setattr("app.services.feed_agents.call_json", bad)
    c = _safety_comment(ss, _rect_key(tech, m["id"]))
    assert c["agent"]["source"] == "rule" and "隐患已消除" not in c["text"] and "AI 建议" in c["text"]


def test_cached_closure_comment_is_marked_stale_until_closed(env):
    """早先生成的点评（T-13 之前）里写着“已确认消除”：隐患还没人确认闭环时按当前事实判为过时，依据改写为当前的 AI 建议。"""
    tech, ss, fid = _dispatched(env)
    m = _photo(tech)
    assert tech.post(f"/api/findings/{fid}/rectify", json={"media_id": m["id"]}).status_code == 200
    key = _rect_key(tech, m["id"])
    conn = dbmod.connect()
    try:
        conn.execute("INSERT INTO feed_agent_notes (project_id, post_key, perspective, text, evidence_json, source, ai_run_id, audience, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (env["project_id"], key, "safety", "整改照片复核通过，隐患已确认消除。",
                      dbmod.dumps([{"text": "这张整改照片经安全Agent 复核：已确认消除", "ref": {"type": "finding", "id": fid, "label": f"隐患 #{fid}"}}]),
                      "ai", None, "all", dbmod.now_iso()))
    finally:
        conn.close()
    c = _safety_comment(ss, key)
    assert c["agent"]["stale"] and c["agent"]["stale_reasons"]
    ev = " ".join(e["text"] for e in c["agent"]["evidence"])
    assert "已确认消除" not in ev and "AI 建议：已消除，待安全主管确认" in ev
    # 人确认闭环之后，原点评不再算过时
    assert ss.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 200
    assert _finding(fid)["status"] == "resolved"
    c = _safety_comment(ss, key)
    assert not c["agent"]["stale"]


# ============================================================================ G-16 role_shots 守卫

def _shots(*args):
    if not NODE:
        pytest.skip("没有 node")
    return subprocess.run([NODE, str(ROOT / "tools" / "role_shots.mjs"), *args], capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=str(ROOT))


@pytest.mark.parametrize("args", [[], ["tech"], ["--base"], ["--base", "http://localhost:8600"], ["tech", "--base", "http://127.0.0.1:8601/"],
                                  ["--base", "http://localhost:8600/#/work"], ["--base", "not a url"]])
def test_role_shots_requires_base_and_refuses_business_ports(args):
    r = _shots(*args)
    assert r.returncode != 0
    assert "--base" in r.stderr or "8600" in r.stderr or "8601" in r.stderr, r.stderr


def test_role_shots_cdp_port_is_configurable():
    src = (ROOT / "tools" / "role_shots.mjs").read_text(encoding="utf-8")
    assert '"--cdp-port"' in src and 'opt("--base", "http' not in src     # 不再有默认目标
    r = _shots("--base", "http://localhost:8612", "--cdp-port", "abc")
    assert r.returncode != 0 and "--cdp-port" in r.stderr


# ============================================================================ C 级：F-06 消息带跳转

def _notes_of(uid, like):
    conn = dbmod.connect()
    try:
        return conn.execute("SELECT title, link_json FROM notifications WHERE user_id=? AND title LIKE ? ORDER BY id", (uid, like)).fetchall()
    finally:
        conn.close()


def test_gate_messages_link_to_the_gate_item(env):
    from test_roles import _add
    from conftest import sample_xer
    from app.services import gate as gate_svc
    _add(env, "eng", "eng_sup", "工程主管")
    pm = login("pm")
    assert pm.post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    t1 = env["users"]["tech1"]
    assert pm.put("/api/gate/A1020/items/drawing/owner", json={"owner_id": t1}).status_code == 200
    rows = _notes_of(t1, "开工条件%")
    assert rows and dbmod.loads(rows[-1]["link_json"]) == {"route": "gate", "params": {"open": "A1020"}}
    conn = dbmod.connect()
    try:
        assert gate_svc.notify_due(conn, env["project_id"])["sent"] >= 1
        warn = conn.execute("SELECT title, link_json FROM notifications WHERE title LIKE '放行预警%'").fetchall()
    finally:
        conn.close()
    assert warn
    for w in warn:
        link = dbmod.loads(w["link_json"])
        assert link["route"] == "gate" and link["params"]["open"] in w["title"]


def test_meeting_action_messages_link_somewhere(env):
    from test_cmd import _finding
    from test_roles import _team
    _team(env)
    fid = _finding(env, "钢筋工程：3#墩钢筋保护层垫块缺失", assignee="tech1")
    pm = login("pm")
    text = "会议名称：第12次生产例会\n议题一：3#墩钢筋整改。结论：乙分包李工明天下班前完成3#墩钢筋整改。\n行动项：甲分包张工周五前提交雨季专项方案。"
    out = pm.post("/api/cmd/minutes", json={"text": text}).json()
    mid, cards = out["meeting"]["id"], out["cards"]
    a0, a1 = cards[0], cards[1]
    assert pm.post(f"/api/meetings/{mid}/items", json={"idx": 0, "text": a0["text"], "owner_id": a0["owner_id"], "due_date": a0["due_date"], "finding_id": fid}).status_code == 200
    assert pm.post(f"/api/meetings/{mid}/items", json={"idx": 1, "text": a1["text"], "owner_id": a1["owner_id"], "due_date": a1["due_date"]}).status_code == 200
    with_f = _notes_of(a0["owner_id"], "例会行动项%")
    assert dbmod.loads(with_f[-1]["link_json"]) == {"route": "finding", "params": {"id": str(fid)}}
    plain = _notes_of(a1["owner_id"], "例会行动项%")
    assert dbmod.loads(plain[-1]["link_json"]) == {"route": "home", "params": {}}     # 技术员首页的待办里有这条，可就地标记完成


# ============================================================================ C 级：U-35 放行预警标题不把条件名写成完成态

def test_gate_gap_names_are_not_in_done_tone():
    from app.services import gate as gate_svc
    g = gate_svc.gap_name
    assert g("开工前质控点已完成") == "开工前质控点"
    assert g("前道工序已完成并验收") == "前道工序"
    assert g("施工方案/专项方案已批") == "施工方案/专项方案"
    assert g("基坑/土方开挖方案已批（含支护、边坡）") == "基坑/土方开挖方案（含支护、边坡）"
    assert g("图纸为最新版并已会审") == "图纸为最新版并已会审"          # 不是“已完成/已批”结尾的不动
    assert g("自定义条件") == "自定义条件"


# ============================================================================ C 级：U-17 剔除产值节后重新编号（项目经理问答引用报表时；报表本身产值节只读照常显示）

def test_pm_report_sections_renumbered():
    from app.ai import report_agent
    from app.services import reports as report_svc
    tpl = report_agent.default_template("week")
    assert [s["title"] for s in report_svc.pm_scope({"progress": []}, tpl, {}, [])[1]["sections"]] == [s["title"] for s in tpl["sections"]]
    _f, t, _n, _c = report_svc.pm_scope({"progress": []}, tpl, {}, [], drop_output=True)
    titles = [s["title"] for s in t["sections"]]
    assert not any("产值" in x for x in titles)
    nums = [x.split("、")[0] for x in titles if "、" in x]
    assert nums == list("一二三四五六七八")[:len(nums)] and len(nums) == len(titles)
    assert titles[3].startswith("四、安全管理")
    # 完整版（工程主管、领导）不变
    assert [s["title"] for s in tpl["sections"]][3].startswith("四、产值")


# ============================================================================ C 级：U-27 审计按时间倒序、操作与对象写中文

def test_audit_list_sorted_by_time_with_readable_names(env):
    from app.services import role_views as rv
    conn = dbmod.connect()
    try:
        u = env["users"]["tech1"]
        for at, action, target in (("2026-10-07T10:44:00", "nudge.auto", "finding:1"), ("2026-09-22T09:00:00", "ticket.upload", "ticket:3"),
                                   ("2026-10-07T10:28:00", "user.update", f"user:{u}"), ("2026-09-22T08:00:00", "finding.confirm", "finding:9")):
            dbmod.insert(conn, "audit", {"user_id": u, "action": action, "target": target, "detail": "", "created_at": at})
        rows = rv.audit_list(conn, 50)
    finally:
        conn.close()
    mine = [r for r in rows if r["action"] in ("nudge.auto", "ticket.upload", "user.update", "finding.confirm")]
    assert [r["created_at"][:16] for r in mine] == ["2026-10-07T10:44", "2026-10-07T10:28", "2026-09-22T09:00", "2026-09-22T08:00"]
    names = {r["action"]: r["action_name"] for r in mine}
    assert all(re.search("[一-龥]", v) and "." not in v for v in names.values()), names
    tgt = {r["action"]: r["target_name"] for r in mine}
    assert tgt["user.update"] == "账号 张工" and "#9" in tgt["finding.confirm"] and ":" not in tgt["ticket.upload"]


# ============================================================================ C 级：前端各项

def test_frontline_menu_is_mine_and_topbar_has_no_logout():
    """U-09：一线岗位菜单里只有修改密码与退出登录，叫“我的”；顶栏不再常驻“退出”。S-8：侧栏副标题与标题相同不显示。"""
    main = read("main.js")
    top = main[main.index('<div class="topbar">'):main.index('<div class="askrow"')]
    assert "logout" not in top and ">退出<" not in top
    assert '<small v-if="l.sub !== l.name">' in main
    out = run_js(r"""
      return { tech: Root.computed.sheetName.call({ sheetLines: [] }), pm: Root.computed.sheetName.call({ sheetLines: [{ k: 'more' }] }) };
    """)
    assert out == {"tech": "我的", "pm": "全部功能"}


def test_home_pages_are_named_home_with_role():
    """D10 / P1-11 / U-11：管理岗首页统一一个名字，页头写岗位；2026-10-08 定名“智能工作台”（取代“首页”），10-09 RJ 改为“智能管理台”。不出现“驾驶舱”，“工作台”只以“智能管理台”出现。"""
    out = run_js(r"""
      const lab = (role, k) => navRolePages(role).find((p) => p.k === k).label;
      const line = (role) => buildNav(role, {}).top[0].name;
      return { labels: [lab('manager', 'work'), lab('eng_sup', 'eng'), lab('safety_sup', 'sdash')], lines: ['manager', 'eng_sup', 'safety_sup'].map(line),
               titles: [EngBoard.template, SafetyBoard.template].map((t) => (t.match(/<div class="zb-hero-sub">([^<]+)</) || [])[1]), pm: PmHome.template };
    """)
    assert out["labels"] == ["智能管理台"] * 3 and out["lines"] == ["智能管理台"] * 3
    assert out["titles"] == ["工程主管 · 智能管理台", "安全主管 · 智能管理台"]
    assert "项目经理 · 智能管理台" in out["pm"]          # 项目经理首页 10-10 起是 x_pmhome.js 的 PmHome（09_V8 页头：项目名 + 岗位 · 智能管理台）
    for n in ("nav.js", "mgr.js", "roles.js", "x_enghome.js"):
        src = read(n)
        assert "驾驶舱" not in src and "工作台" not in src.replace("智能管理台", ""), n


def test_my_pages_use_load_state_instead_of_endless_loading():
    """S-1：本车道各页加载失败给原因和重试，不再一直“加载中”。"""
    for n in ("mgr.js", "roles.js", "circle.js", "x_aimgr.js"):
        src = read(n)
        assert 'class="empty">加载中</div>' not in src and 'class="empty card">加载中' not in src, n
        assert "<load-state" in src, n
    assert ':error="err" @retry="load"' in read("mgr.js")


def test_finding_board_ai_buttons_busy_and_candidate_hint():
    """S-7：重新识别、生成质控提醒进行中禁用并写明在做什么；U-12：安全页写明候选在哪里确认；S-6：隐患卡片手机单列。"""
    mgr = read("mgr.js")
    tpl = mgr[mgr.index("const FindingBoard"):mgr.index("const QualityHub")]
    assert ':disabled="!!busy" @click="rescan"' in tpl and ':disabled="!!busy" @click="qc"' in tpl and "AI 识别中" in tpl
    assert "条 AI 候选隐患待确认" in tpl and "到首页待办里确认" in tpl
    assert "fb-grid" in tpl and ".fb-grid { grid-template-columns: minmax(0, 1fr); }" in read("x_ui.css")
    out = run_js(r"""
      let calls = 0, toasts = [];
      const self = { busy: '', load() {}, };
      const p = FindingBoard.methods.ai.call(self, 'scan', async () => { calls++; await new Promise((r) => setTimeout(r, 20)); });
      const during = self.busy;
      await FindingBoard.methods.ai.call(self, 'scan', async () => { calls++; });   // 进行中再点：不重复调用
      await p;
      return { during, calls, after: self.busy };
    """)
    assert out == {"during": "scan", "calls": 1, "after": ""}


def test_agents_center_penetration_and_adoption_chip():
    """U-22：渗透率全为空（仿真项目不计）时整张卡收成一行；U-36：采纳率不用绿色。"""
    mgr = read("mgr.js")
    assert 'class="chip green" v-if="a.feedback.rate' not in mgr
    out = run_js(r"""
      const c = AgentsCenter.computed;
      const sim = { d: { penetration: { '日志': { rate: null, note: '仿真项目不计' }, '预警': { rate: null, note: '仿真项目不计' } } } };
      const real = { d: { penetration: { '日志': { rate: 0.5, ai: 1, n: 2 }, '预警': { rate: null } } } };
      return { simEmpty: c.penEmpty.call(sim), simNote: c.penNote.call(sim), realEmpty: c.penEmpty.call(real) };
    """)
    assert out == {"simEmpty": True, "simNote": "仿真项目不计", "realEmpty": False}


def test_aimgr_units_and_model_list_state():
    """U-37：数量单位统一用“万”；模型清单取完后不再一直写“加载中”。"""
    out = run_js(r"""return [aimNum(999), aimNum(53400), aimNum(508000), aimNum(10000), aimNum(0)];""")
    assert out == ["999", "5.3万", "50.8万", "1万", "0"]
    src = read("x_aimgr.js")
    assert 'v-else-if="!modelsDone">加载模型清单中' in src and "finally { this.modelsDone = true; }" in src


def test_circle_relative_time_uses_server_clock_when_available():
    out = run_js(r"""
      const iso = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
      const before = ago(iso);
      globalThis.serverNow = () => new Date(Date.now() + 2 * 3600 * 1000);   // 服务器比手机快 2 小时
      const after = ago(iso);
      globalThis.serverNow = () => { throw new Error('x'); };
      const broken = ago(iso);
      const late = CircleFeed.methods.agentAt({ time: '2026-09-22T08:00:00' }, { time: '2026-10-07T10:00:00' });
      const fresh = CircleFeed.methods.agentAt({ time: iso }, { time: iso }) ;
      return { before, after, broken, late, fresh };
    """)
    assert out["before"] == "3小时前" and out["after"] == "5小时前" and out["broken"] == "3小时前"
    assert out["late"] == "补评于 10-07" and out["fresh"] == "3小时前"


def test_gate_reason_required_inline_and_overdue_label():
    """F-11③：受阻、不适用不写原因时就地提示并禁用确认；U-25：应开未开的作业不叫“临近缺项（3 天内）”。"""
    src = read("x_gate.js")
    assert ':disabled="busy || needNote" @click="saveState"' in src and 'v-if="needNote"' in src
    assert "临近缺项（3 天内）<" not in src and "已超开工日" in src
    out = run_js(r"""
      const comp = EXT.routes.gate;
      const f = (ed) => comp.computed.needNote.call({ ed });
      return [f({ mode: 'state', state: 'blocked', note: '' }), f({ mode: 'state', state: 'blocked', note: '缺图纸' }), f({ mode: 'state', state: 'ready', note: '' }), f(null)];
    """)
    assert out == [True, False, False, False]


def test_gate_ss_predecessor_started_is_not_ready():
    """U-25：开始-开始关系的前置作业只是开了工，不能把“前道工序已完成并验收”判成已具备。"""
    from types import SimpleNamespace
    from app.services import gate as gate_svc
    def ctx(pred, kind="SS"):
        return SimpleNamespace(preds={"B": [{"pred_code": "A", "kind": kind}]}, by_code={"A": {"code": "A", "name": "RD110 清表", **pred}}, logmax={})
    r = gate_svc.chk_prev(ctx({"phys_pct": 40.6, "act_start": "2026-09-01"}), {"code": "B"})
    assert r["state"] == "unknown" and "开始-开始" in r["text"] and "40.6" in r["text"] and "已完成：" not in r["text"]
    assert gate_svc.chk_prev(ctx({"phys_pct": 0}), {"code": "B"})["state"] == "missing"
    assert gate_svc.chk_prev(ctx({"phys_pct": 100, "act_finish": "2026-09-30"}, "FS"), {"code": "B"})["state"] == "ok"


def test_feed_comment_tone_guard():
    """U-20：点评只报事实，不下“落实到位”评价、不用“哦~”语气词。"""
    from app.services import feed_agents as fa
    assert fa.tone_problems("质控点确认已完成，质量管控落实到位")
    assert fa.tone_problems("要持续跟进哦~")
    assert not fa.tone_problems("已派发质控提醒，还没有处理")
    assert fa.PROMPT_VERSION.startswith("feed-comment-v3")


def test_load_state_error_style_has_no_side_bar():
    """出错状态用整圈细边框 + 浅红底，不用卡片单侧粗色条。"""
    css = read("x_ui.css")
    rules = [ln for ln in css.splitlines() if "ls-err" in ln and "{" in ln]
    assert rules and not any("border-left" in ln for ln in rules)


def test_no_demo_label_on_hero():
    """RJ 10-09：去掉首页头图右上角的“示意图”字样（技术员首页、管理岗工作台、领导总览）。"""
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    for f in ("tech.js", "todo.js", "cockpit.js", "x_skin.css"):
        s = (root / f).read_text(encoding="utf-8")
        assert "hero-demo" not in s and ">示意图<" not in s, f
