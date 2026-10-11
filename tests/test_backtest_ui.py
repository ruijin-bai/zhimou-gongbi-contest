"""T-04b 历史回测页面（P0-2 前端）：x_backtest.js / x_backtest.css 的静态检查与页面逻辑。

- 页面数据取自真实接口：用 T-04a 的合成多期 XER 以工程主管身份跑一次 source=test 的回测，把接口返回交给 node 里的页面组件；
- 比例只用后端算好的值：分母为 0 写“无预警样本”，分母不足写“样本不足（k/n）”，不出百分比，与 Word 同一写法（AC-2.11）；
- 结论卡、来源徽标与“回测于 MM-DD（…）”，测试数据明确标注；页面不出现“准确率”字样（AC-2.12、AC-2.15）；
- 误报/漏报清单只列 FP/FN，点开有各期原始值（AC-2.3）；Word 下载链接（AC-2.4）；敏感性表 3×3 与“采用阈值”只给工程主管；
- 上传必须选来源、至少 2 期，空状态按钮直接打开文件选择（U-05①）；
- 导航：只给工程主管，挂在“预警”页签之后，管理员不设入口（G-13）；
- x_backtest.css 只用颜色令牌（N-10⑪）。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from conftest import login
from test_backtest import _add_user, _files, _period
from test_ui_fixes import run_js

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
NODE = shutil.which("node")


def read(n):
    return (STATIC / n).read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- node 里跑页面组件（不渲染 DOM：编译模板 + 调用 computed/methods）

HARNESS = r"""
const vm = require('vm'), fs = require('fs'), path = require('path');
const [dir, viewFile] = process.argv.slice(2);
const VIEW = JSON.parse(fs.readFileSync(viewFile, 'utf8'));
const noop = () => {};
function el() {
  const o = { style: {}, setAttribute: noop, appendChild: noop, childNodes: [], textContent: '', children: [] };
  const dec = (v) => String(v).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&');
  Object.defineProperty(o, 'innerHTML', {
    set(v) { const m = /^<div foo="([\s\S]*)">$/.exec(String(v)); this.textContent = dec(v); this.children = m ? [{ getAttribute: () => dec(m[1]) }] : []; },
    get() { return this.textContent; },
  });
  return o;
}
const calls = [];
const ctx = { console, setTimeout, clearTimeout, navigator: { userAgent: '' }, location: { hash: '' }, addEventListener: noop,
  document: { addEventListener: noop, createElement: el, querySelector: () => null, documentElement: { style: {} } } };
ctx.window = ctx; ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(dir, 'vendor/vue.global.prod.js'), 'utf8'), ctx);
// 与 core.js 同形的扩展点与全局函数（桩）
ctx.EXT = { routes: {}, navPages: [], aliases: {}, route(n, c) { this.routes[n] = c; },
  nav(role, page) { this.navPages.push({ short: '', icon: 'layers', ...page, roles: role === '*' ? ['*'] : [].concat(role) }); } };
ctx.store = { me: { user: { role: 'eng_sup' }, project: { features: {} } }, route: { name: 'backtest', params: {} } };
ctx.roleOf = () => ctx.store.me.user.role;
ctx.go = (...a) => calls.push(['go', ...a]);
ctx.toast = (m, e) => calls.push(['toast', m, !!e]);
ctx.LEVEL = { red: '红色', orange: '橙色', yellow: '黄色' };
ctx.history = { replaceState: (a, b, u) => calls.push(['replace', u]) };
ctx.FormData = class { constructor() { this.items = []; } append(k, v, n) { this.items.push([k, typeof v === 'object' ? (n || v.name) : String(v)]); } };
let replies = {}, postReply = null;
ctx.GET = async (u) => { calls.push(['GET', u]); const r = replies[u]; if (r instanceof Error) throw r; return r === undefined ? null : r; };
ctx.POST = async (u, b) => { calls.push(['POST', u, b && b.items ? b.items : b]); if (postReply instanceof Error) throw postReply; return postReply; };
const src = fs.readFileSync(path.join(dir, 'x_backtest.js'), 'utf8');
vm.runInContext(src + `
;this.__m = { btRateText, btRateParts, btLead, btMeta, btHeadline, btSplitFiles, btCanRun, btFilesHint, btExcludeOk,
  comps: { BacktestPage, BtSummary, BtPeriods, BtOdd, BtSens } };`, ctx);
vm.runInContext(fs.readFileSync(path.join(dir, 'nav.js'), 'utf8') + '\n;this.__nav = { buildNav, navMatch };', ctx);
const M = ctx.__m, C = M.comps;

// 组件实例替身：data + 绑定 methods + computed 取值器
function inst(comp, extra) {
  const self = Object.assign(comp.data ? comp.data() : {}, extra || {});
  self.$emit = (...a) => calls.push(['emit', ...a]);
  for (const [k, f] of Object.entries(comp.methods || {})) self[k] = f.bind(self);
  for (const [k, f] of Object.entries(comp.computed || {})) Object.defineProperty(self, k, { get: () => f.call(self), configurable: true });
  return self;
}
const clone = (o) => JSON.parse(JSON.stringify(o));
const http = (status) => Object.assign(new Error('x'), { status });
const lastOf = (kind) => calls.filter((c) => c[0] === kind).pop();

(async () => {
  const out = {};
  // 1. 模板全部能编译
  out.compileErrs = [];
  for (const [k, c] of Object.entries(C)) {
    try { ctx.Vue.compile(c.template, { onError: (e) => { throw e; }, onWarn: noop }); }
    catch (e) { out.compileErrs.push(k + ': ' + String(e.message).slice(0, 160)); }
  }
  out.routes = Object.keys(ctx.EXT.routes);
  out.navPages = ctx.EXT.navPages;
  out.nav = {};
  for (const r of ['tech', 'sub_safety', 'manager', 'eng_sup', 'safety_sup', 'leader', 'admin']) {
    const n = ctx.__nav.buildNav(r, {});
    out.nav[r] = n.top.concat(n.bottom).map((l) => ({ k: l.k, sub: l.sub, pages: l.pages.map((p) => p.k) }));
  }
  const m = ctx.__nav.navMatch('eng_sup', 'backtest', {});
  out.match = m ? m.k : null;

  // 2. 纯函数
  const z = 'zero';
  out.rate = {
    ok: M.btRateText({ k: 15, n: 20, rate: 0.75 }), few: M.btRateText({ k: 2, n: 3, rate: null }), none: M.btRateText({ k: 0, n: 0, rate: null }, '无预警样本'),
    noneRecall: M.btRateText({ k: 0, n: 0, rate: null }), missing: M.btRateText(null),
    partsOk: M.btRateParts({ k: 15, n: 20, rate: 0.75 }), partsFew: M.btRateParts({ k: 2, n: 3, rate: null }), partsNone: M.btRateParts({ k: 0, n: 0, rate: null }, '无预警样本'),
  };
  out.lead = [M.btLead(2.1), M.btLead(-3), M.btLead(0), M.btLead(null)];
  out.can = { none: M.btCanRun({ files: [], source: 'real' }), one: M.btCanRun({ files: [1], source: 'real' }), noSrc: M.btCanRun({ files: [1, 2, 3], source: '' }),
              two: M.btCanRun({ files: [1, 2], source: 'test' }) };
  out.hint = [M.btFilesHint(0), M.btFilesHint(1), M.btFilesHint(2), M.btFilesHint(6)];
  out.split = M.btSplitFiles([{ name: 'a.xer' }, { name: 'B.XER' }, { name: 'c.xlsx' }]);
  out.split = { ok: out.split.ok.map((f) => f.name), bad: out.split.bad.map((f) => f.name) };
  out.exclOk = [M.btExcludeOk(VIEW.periods_report, []), M.btExcludeOk(VIEW.periods_report, VIEW.periods_report.slice(0, 2).map((p) => p.data_date)),
                M.btExcludeOk(VIEW.periods_report, VIEW.periods_report.slice(0, 3).map((p) => p.data_date))];
  const real = { ...clone(VIEW), source: 'real', source_name: '真实项目数据', source_note: '真实数据，不在仿真时间线内', label: 'LPR1', created_at: '2026-10-08T21:30:00' };
  out.meta = { test: M.btMeta(VIEW), real: M.btMeta(real) };
  out.headline = { test: M.btHeadline(VIEW), real: M.btHeadline(real) };

  // 3. 结论卡：后端口径原样展示；分母为 0 / 不足时不出百分比
  const sum = inst(C.BtSummary, { v: VIEW });
  out.summary = { prec: sum.prec, rec: sum.rec, share: sum.share, lead: sum.lead, cmp: sum.cmp, notes: sum.notes };
  const noAlert = clone(VIEW);
  Object.assign(noAlert.summary, { tp: 0, fp: 0, precision: { k: 0, n: 0, rate: null }, recall: { k: 0, n: 4, rate: null }, no_alert_samples: true });
  const s2 = inst(C.BtSummary, { v: noAlert });
  out.noAlert = { prec: s2.prec, rec: s2.rec, headline: M.btHeadline(noAlert) };
  // 回测、现场反馈、仿真三类数字分开：结论卡说明里指向预警页签
  sum.go('progress', { tab: 'alerts' });
  out.goAlerts = lastOf('go');

  // 4. 误报 / 漏报：只列 FP/FN，分页 20，点开取各期原始值
  const odd = inst(C.BtOdd, { v: VIEW });
  out.odd = { total: odd.odd.length, outcomes: [...new Set(odd.odd.map((r) => r.outcome))].sort(), shown: odd.shown.length, fp: odd.count('FP'), fn: odd.count('FN'),
              rowsFPFN: VIEW.rows.filter((r) => r.outcome === 'FP' || r.outcome === 'FN').length };
  const first = odd.list[0];
  odd.flip(first);
  out.odd.opened = !!odd.open[odd.key(first)];
  out.odd.hist = odd.hist(first).map((h) => h.data_date);
  odd.setKind('FN');
  out.odd.fnOnly = odd.list.every((r) => r.outcome === 'FN');

  // 5. 敏感性表与采用阈值（工程主管）
  // 测试来源：能看格子，但不能采用（按钮置灰，adopt 不发请求）
  const tsens = inst(C.BtSens, { v: VIEW, canAdopt: true, liveRed: 1 });
  tsens.pick(2, 3);
  calls.length = 0;
  await tsens.adopt();
  out.sensTest = { isReal: tsens.isReal, sel: tsens.sel, posted: calls.some((c) => c[0] === 'POST') };
  // 真实来源：可采用
  const sens = inst(C.BtSens, { v: real, canAdopt: true, liveRed: 1 });
  out.sens = { reds: sens.reds, tols: sens.tols, cells: sens.cells.length, current: sens.cells.filter((c) => c.current).map((c) => [c.red_slip_days, c.tolerance_days]), isReal: sens.isReal };
  sens.pick(2, 3);
  out.sens.sel = sens.sel;
  postReply = { ...clone(VIEW), adopted: { red_slip_days: 2, tolerance_days: 3, previous_red_slip_days: 1, at: '2026-10-08T21:40:00', by: '工程主管' } };
  await sens.adopt();
  out.sens.adoptPost = lastOf('POST');
  out.sens.emitted = lastOf('emit')[1];
  out.sens.selAfter = sens.sel;
  const ro = inst(C.BtSens, { v: real, canAdopt: false, liveRed: 1 });
  ro.pick(2, 3);
  out.sens.readonlySel = ro.sel;

  // 6. 页面：默认先看真实数据，没有再看测试数据；两者都没有显示空状态，按钮直接打开文件选择
  let clicks = 0;
  const fileEl = { click: () => clicks++ };
  const P = (role, extra) => { ctx.store.me.user.role = role; return inst(C.BacktestPage, { $refs: { file: fileEl }, ...(extra || {}) }); };
  replies = { '/api/backtest/latest?source=real': null, '/api/backtest/latest?source=test': VIEW, '/api/project/thresholds': { values: { red_slip_days: 1 } } };
  let pg = P('eng_sup');
  await pg.load();
  out.page = { fallback: { src: pg.src, id: pg.v && pg.v.id, err: pg.err, liveRed: pg.liveRed, raw: !!(pg.v && pg.v.__v_skip) } };
  replies = { '/api/backtest/latest?source=real': null, '/api/backtest/latest?source=test': null };
  pg = P('eng_sup');
  await pg.load();
  out.page.empty = { v: pg.v, title: pg.emptyTitle, reason: pg.emptyReason, action: pg.emptyAction && pg.emptyAction.label, alt: pg.emptyAlt };
  clicks = 0; pg.emptyAction.fn();
  out.page.empty.clicks = clicks; out.page.empty.panelAfterClick = pg.panel;
  pg.onPick({ target: { files: [{ name: 'p1.xer', size: 2048 }, { name: 'p2.XER', size: 4096 }, { name: 'note.docx', size: 10 }], value: 'x' } });
  out.page.picked = { files: pg.files.map((f) => f.name), bad: pg.bad, panel: pg.panel, can: pg.can };
  calls.length = 0;
  await pg.run();
  out.page.noSourcePosted = calls.some((c) => c[0] === 'POST'); out.page.noSourceErr = pg.runErr;
  pg.form.source = 'test'; pg.form.label = ' 合成 ';
  postReply = clone(VIEW);
  await pg.run();
  out.page.runPost = lastOf('POST');
  out.page.afterRun = { src: pg.src, panel: pg.panel, files: pg.files.length, id: pg.v.id, toast: lastOf('toast') };
  // 剔除期次重跑：本次会话刚跑过同一批文件 → 直接带 exclude_periods 重跑
  calls.length = 0;
  const ex = [VIEW.periods_report[1].data_date];
  pg.rerun(ex);
  await new Promise((r) => setTimeout(r, 0));
  out.page.rerunPost = lastOf('POST');
  // 没有这批文件（如刷新后）→ 预填来源、剔除期次，并直接弹出文件选择
  const pg2 = P('eng_sup', { v: VIEW });
  clicks = 0; calls.length = 0;
  pg2.rerun(ex);
  out.page.rerunNoFiles = { clicks, panel: pg2.panel, source: pg2.form.source, exclude: pg2.form.exclude, posted: calls.some((c) => c[0] === 'POST') };
  // 管理员：只读，不能上传、不能采用；项目经理等访问接口 403
  const adm = P('admin');
  replies = { '/api/backtest/latest?source=real': null, '/api/backtest/latest?source=test': null };
  await adm.load();
  clicks = 0; adm.openPicker();
  out.page.admin = { canRun: adm.canRun, canAdopt: adm.canAdopt, action: adm.emptyAction, clicks, reason: adm.emptyReason };
  replies = { '/api/backtest/latest?source=real': http(403) };
  const pm = P('manager');
  await pm.load();
  out.page.pm = { err: pm.err, v: pm.v };
  // ?id=N 打开指定一次
  ctx.store.route.params = { id: String(VIEW.id) };
  replies = { ['/api/backtest/' + VIEW.id]: VIEW };
  const byId = P('eng_sup');
  await byId.load();
  out.page.byId = { id: byId.v && byId.v.id, src: byId.src, get: calls.filter((c) => c[0] === 'GET').map((c) => c[1]).includes('/api/backtest/' + VIEW.id) };
  ctx.store.route.params = {};
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""

_CACHE: dict = {}


def _run_api_view(env) -> dict:
    _add_user(env, "eng", "eng_sup")
    eng = login("eng")
    r = eng.post("/api/backtest/run", files=_files([_period(k) for k in range(4)]), data={"source": "test", "label": "合成四期"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert eng.get("/api/backtest/latest?source=test").json()["id"] == v["id"]
    return v


@pytest.fixture()
def ui(env):
    if not NODE:
        pytest.skip("没有 node，跳过回测页面逻辑检查")
    if "out" not in _CACHE:
        view = _run_api_view(env)
        tmp = Path(tempfile.mkdtemp(prefix="zb_btui_"))
        try:
            vf, h = tmp / "view.json", tmp / "bt_harness.js"
            vf.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
            h.write_text(HARNESS, encoding="utf-8")
            r = subprocess.run([NODE, str(h), str(STATIC), str(vf)], capture_output=True, text=True, encoding="utf-8", timeout=90)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        assert r.returncode == 0, r.stderr[:1500]
        _CACHE["out"], _CACHE["view"] = json.loads(r.stdout), view
    return _CACHE["out"], _CACHE["view"]


# ----------------------------------------------------------------------------- 接口契约：页面读取的字段都在接口返回里

def test_api_view_has_every_field_the_page_reads(ui):
    _, v = ui
    for k in ("id", "created_at", "created_by", "source", "source_name", "source_note", "label", "params", "summary", "lead_time",
              "periods_report", "sensitivity", "rows", "gap_rows", "history", "adopted"):
        assert k in v, k
    s = v["summary"]
    for k in ("periods", "periods_uploaded", "excluded_periods", "data_date_from", "data_date_to", "fact_date", "weak", "samples", "tp", "fp", "fn", "tn",
              "precision", "recall", "delay_share", "accuracy_ref", "no_alert_samples", "data_gaps", "skipped", "fact_actual", "fact_forecast",
              "same_period_samples", "strict", "by_activity", "by_period", "notes", "definitions"):
        assert k in s, k
    assert set(s["precision"]) == {"k", "n", "rate"}
    for k in ("mean_days", "median_days", "compared", "delayed", "earlier", "same", "later", "only_ours", "never"):
        assert k in v["lead_time"], k
    for k in ("index", "data_date", "file", "activities", "pct_types", "completed", "overlap_prev", "flags", "suspect", "excluded", "role"):
        assert k in v["periods_report"][0], k
    for k in ("code", "name", "plan_date", "judge_date", "critical", "float", "plan_finish", "pct_start", "pct_plan", "pct_actual", "slip", "level",
              "rule", "predicted", "fact_finish", "fact_kind", "true_delay", "outcome"):
        assert k in v["rows"][0], k
    for k in ("red_slip_days", "tolerance_days", "current", "samples", "precision", "recall", "data_gaps"):
        assert k in v["sensitivity"][0], k
    h = next(iter(v["history"].values()))[0]
    for k in ("data_date", "status", "pct", "pct_type", "total_float_d", "early_start", "early_finish", "act_start", "act_finish"):
        assert k in h, k
    assert v["source"] == "test" and v["source_name"] == "仿真/测试数据"


# ----------------------------------------------------------------------------- 模板、注册与导航

def test_templates_compile_and_route_registered(ui):
    out, _ = ui
    assert out["compileErrs"] == []
    assert out["routes"] == ["backtest"]
    assert out["navPages"] == [{"short": "", "icon": "target", "k": "backtest", "route": "backtest", "label": "历史回测", "line": "ai", "after": "metrics", "roles": ["eng_sup"]}]


def test_nav_only_eng_sup_tab_right_after_alerts(ui):
    """导航编排 10-08：历史回测是应用成效的证据，放在“智能体”组、紧挨应用成效（原在进度组预警之后）。"""
    out, _ = ui
    ai = next(line for line in out["nav"]["eng_sup"] if line["k"] == "ai")
    assert ai["pages"] == ["agents", "metrics", "backtest", "chase"]
    assert ai["sub"] == "智能体 · 成效 · 催办"                          # 页签不改业务线副标题
    prog = next(line for line in out["nav"]["eng_sup"] if line["k"] == "prog")
    assert "backtest" not in prog["pages"]
    assert out["match"] == "backtest"                                   # 在本页时高亮“历史回测”页签
    for role, lines in out["nav"].items():
        if role != "eng_sup":
            assert all("backtest" not in line["pages"] for line in lines), role   # 管理员、项目经理、领导等都没有入口（G-13）


# ----------------------------------------------------------------------------- 口径：不补算、不补零、不出“准确率”

def test_rates_follow_backend_and_never_invent_percentages(ui):
    out, v = ui
    r = out["rate"]
    assert r["ok"] == "75.0%（15/20）"
    assert r["few"] == "样本不足（2/3）" and "%" not in r["few"]               # 分母不足：不出百分比（AC-2.11）
    assert r["none"] == "无预警样本（0/0）" and r["noneRecall"] == "—（0/0）" and r["missing"] == "—"
    assert r["partsOk"] == {"v": "75.0%", "kn": "15/20"} and r["partsFew"] == {"v": "样本不足", "kn": "2/3"} and r["partsNone"] == {"v": "无预警样本", "kn": ""}
    # 与 Word 导出同一写法
    from app.services import backtest as bt
    for x in ({"k": 15, "n": 20, "rate": 0.75}, {"k": 2, "n": 3, "rate": None}, {"k": 0, "n": 0, "rate": None}):
        assert bt.fmt_rate(x, "无预警样本") in (r["ok"], r["few"], r["none"])
    s = out["summary"]
    p = v["summary"]["precision"]
    assert s["prec"] == {"v": f"{p['rate'] * 100:.1f}%", "kn": f"{p['k']}/{p['n']}"}
    cmp = {row[0]: row[1:4] for row in s["cmp"]}
    assert cmp["命中率（主指标）"][0] == bt.fmt_rate(v["summary"]["precision"], "无预警样本")
    assert cmp["召回率"][1] == bt.fmt_rate(v["summary"]["strict"]["recall"])
    assert cmp["实际延误占比"][2] == bt.fmt_rate(v["summary"]["by_activity"]["delay_share"])
    assert any(k.startswith("(TP+TN)/N（仅作参考") for k in cmp)
    na = out["noAlert"]
    assert na["prec"] == {"v": "无预警样本", "kn": ""} and na["rec"] == {"v": "样本不足", "kn": "0/4"}
    assert "命中率 无预警样本" in na["headline"] and "%" not in na["headline"].split("召回率")[0]


def test_headline_meta_and_source_marking(ui):
    out, v = ui
    assert out["meta"]["real"] == "回测于 10-08（真实数据，不在仿真时间线内）"          # AC-2.15
    assert out["meta"]["test"].startswith("回测于 ") and out["meta"]["test"].endswith("（仿真/测试数据，不计入应用成效）")
    h = out["headline"]["real"]
    s = v["summary"]
    assert h.startswith("真实项目 · LPR1 · ") and f"{s['periods']} 期" in h and f"数据日期 {s['data_date_from']}～{s['data_date_to']}" in h
    assert f"样本 {s['samples']}" in h and "命中率 " in h and "召回率 " in h and f"数据缺口 {s['data_gaps']} 个不计入" in h
    assert "平均提前 " in h or "平均晚 " in h or "平均与 P6 同期" in h
    assert out["headline"]["test"].startswith("测试数据 · 合成四期 · ")                # 测试数据在结论里明确标出
    assert out["lead"] == ["提前 2.1 天", "晚 3 天", "与 P6 同期", "—"]
    assert out["goAlerts"] == ["go", "progress", {"tab": "alerts"}]                     # 现场人工反馈命中率在预警页签，不并入本页
    js = read("x_backtest.js")
    assert "测试数据：" in js and "v.source !== 'real'" in js and "source_name" in js
    assert "准确率" not in js                                                            # 不造准确率
    assert "仿真项目 · 不计入应用成效" not in js                                          # 仿真角标只在侧栏，来源徽标另写
    assert "预警页签" in js or "预警</a>页签" in js


def test_page_head_and_boundary_note_copy():
    """页头与底部说明的文案在 x_ui.js（PAGE_HEADS.backtest / AI_NOTES.backtest），本模块只写 page="backtest"，不再另存一份。"""
    if not NODE:
        pytest.skip("没有 node")
    code = ("const vm=require('vm'),fs=require('fs');const ctx={console,isSim:()=>false,go:()=>{},store:{},document:{},setTimeout};vm.createContext(ctx);"
            "vm.runInContext(fs.readFileSync(process.argv[1],'utf8')+';this.__o={h:PAGE_HEADS.backtest,n:AI_NOTES.backtest};',ctx);"
            "process.stdout.write(JSON.stringify(ctx.__o));")
    r = subprocess.run([NODE, "-e", code, str(STATIC / "x_ui.js")], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    title, purpose = o["h"]
    note = o["n"]
    assert title == "历史回测" and len(purpose) <= 25
    for w in ("准确率", "保证", "自动审批", "100%", "填报", "缺报", "产值", "数据审核"):
        assert w not in purpose + note, w
    assert "不调用 AI" in note
    js = read("x_backtest.js")
    assert '<page-head page="backtest">' in js and '<ai-note page="backtest">' in js
    assert "BT_HEAD" not in js and "BT_NOTE" not in js and "PAGE_HEADS" not in js.split("*/", 1)[1]   # 不再有后备文案
    assert "演示日" in read("x_ui.js")                                                     # T-07 的演示日标识原样保留


def test_alerts_note_role_scoped_no_gap_words_for_pm():
    """预警页底部说明：“没报数据不等于落后”只给工程主管（数据缺口归他管）；项目经理等看到的说明不出现“没报”类字样（AGENTS.md）。"""
    if not NODE:
        pytest.skip("没有 node")
    code = ("const vm=require('vm'),fs=require('fs');const ctx={console,isSim:()=>false,go:()=>{},store:{},document:{},setTimeout,role:''};"
            "ctx.roleOf=()=>ctx.role;vm.createContext(ctx);"
            "vm.runInContext(fs.readFileSync(process.argv[1],'utf8')+';this.__n=(r,p,t)=>{this.role=r;const c=AiNote.computed.note.call({page:p,text:t,notes:AI_NOTES});return c;};',ctx);"
            "const out={};for(const r of ['manager','eng_sup','leader','admin'])out[r]=ctx.__n(r,'alerts');out.text=ctx.__n('manager','alerts','自定义');out.other=ctx.__n('manager','forecast');"
            "process.stdout.write(JSON.stringify(out));")
    r = subprocess.run([NODE, "-e", code, str(STATIC / "x_ui.js")], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert "没报数据不等于落后" in o["eng_sup"]
    for role in ("manager", "leader", "admin"):
        assert o[role].startswith("预警由进度规则计算") and not any(w in o[role] for w in ("没报", "填报", "缺报", "没记录")), role
    assert o["text"] == "自定义" and o["other"].startswith("预测与赶工方案")


def test_metrics_page_keeps_one_backtest_line_only():
    """应用成效页的回测只留一行数字加链接（完整内容在 #/backtest）；只引用真实数据，没有就只写一句并给去上传的链接。"""
    js = read("views.js")
    m = js[js.index("const Metrics = {"):js.index("\n};", js.index("const Metrics = {"))]
    for gone in ("P6 预警历史回测", "bt-file", "/api/p6/backtest", "async backtest()", "bt: []", "busy", "<div class=\"l\">准确率</div>", "要求≥75%"):
        assert gone not in m, gone
    assert m.count("历史回测：") == 1 and ':href="bt.link"' in m and 'href="#/backtest"' in m and "还没有真实项目数据的回测" in m
    if not NODE:
        pytest.skip("没有 node")
    o = run_js(r"""
      store.me = { user: { role: 'eng_sup', name: 'x' }, can: ['progress_ops'], project: { features: {} } };
      const c = Metrics.computed;
      const real = { at: '2026-10-08T21:30:00', source_note: '真实数据，不在仿真时间线内', periods: 6, samples: 420, precision: 0.6, precision_k: 12, precision_n: 20,
                     recall: 0.5, data_gaps: 7, weak: false, link: '#/backtest?id=3' };
      const line = (b) => c.btLine.call({ bt: b });
      return { real: line(real), few: line({ ...real, precision: null, precision_k: 2, precision_n: 3, recall: null }),
               none: line({ ...real, precision: null, precision_k: 0, precision_n: 0, recall: null }), weak: line({ ...real, periods: 2, weak: true }),
               eng: c.canBacktest.call({}),
               multi: [c.multiProvider.call({ m: { ai_runs: [{ provider: 'jiaorong' }, { provider: 'jiaorong' }] } }), c.multiProvider.call({ m: { ai_runs: [{ provider: 'jiaorong' }, { provider: 'mock' }] } })] };
    """)
    assert o["real"] == "回测于 10-08（真实数据，不在仿真时间线内） · 6 期 · 样本 420 · 命中率 60.0%（12/20） · 召回率 50.0% · 数据缺口 7 个不计入"
    assert "命中率 样本不足（2/3）" in o["few"] and "%" not in o["few"] and "召回率" not in o["few"]
    assert "命中率 无预警样本" in o["none"] and o["weak"].endswith("只有 2 期，弱口径")
    assert o["eng"] is True and o["multi"] == [False, True]             # U-23④ 只有一种来源时不列来源


def test_plan_empty_state_opens_file_picker_and_load_error_kept():
    if not NODE:
        pytest.skip("没有 node")
    js = read("views.js")
    plan = js[js.index("const Plan = {"):js.index("\n};", js.index("const Plan = {"))]
    assert "<load-state" in plan and 'class="empty">加载中' not in plan and "fmtDays" in plan     # S-1；T-12b 的 fmtDays 保留
    o = run_js(r"""
      store.me = { user: { role: 'eng_sup', name: 'x' }, can: ['progress_ops'], project: { features: {} } };
      const clicks = [];
      document.getElementById = (id) => ({ click: () => clicks.push(id), focus: () => {}, scrollIntoView: () => {} });
      const a = Plan.computed.emptyAction.call({ imports: [] });
      a.fn();
      const ctx = { q: '', only: '', items: [], imports: [], loaded: false, err: null };
      await Plan.methods.load.call(ctx);          // 桩里没有 /api/p6/activities：按出错处理，错误留给 <load-state>
      return { label: a.label, clicks, err: ctx.err && ctx.err.message, loaded: ctx.loaded };
    """)
    assert o == {"label": "选择 .xer 文件", "clicks": ["plan-file"], "err": "请先登录", "loaded": False}   # U-05② 直接 click()，不只移焦点


# ----------------------------------------------------------------------------- 明细、敏感性表、采用阈值、Word

def test_odd_list_only_fp_fn_paginated_and_expandable(ui):
    out, _ = ui
    o = out["odd"]
    assert o["total"] == o["rowsFPFN"] == o["fp"] + o["fn"] and o["total"] > 0
    assert o["outcomes"] == ["FN", "FP"]
    assert o["shown"] == min(20, o["total"])
    assert o["opened"] and len(o["hist"]) >= 3                    # 点开看到各期原始值（AC-2.3）
    assert o["hist"] == sorted(o["hist"])
    assert o["fnOnly"]


def test_sensitivity_grid_and_adopt_only_for_eng_sup(ui):
    out, v = ui
    s = out["sens"]
    assert s["reds"] == [1, 2, 3] and s["tols"] == [0, 3, 7] and s["cells"] == 9
    assert s["current"] == [[v["params"]["thresholds"]["red_slip_days"], v["params"]["tolerance_days"]]]
    assert s["sel"] == [2, 3]
    assert s["adoptPost"] == ["POST", f"/api/backtest/{v['id']}/adopt", {"red_slip_days": 2, "tolerance_days": 3}]
    assert s["emitted"] == "adopted" and s["selAfter"] is None
    assert s["readonlySel"] is None                               # 非工程主管点格子不会出现采用面板
    assert s["isReal"] is True
    t = out["sensTest"]                                           # leader 决定：测试数据不能用于调整线上预警
    assert t == {"isReal": False, "sel": [2, 3], "posted": False}
    js = read("x_backtest.js")
    assert ':disabled="busy || same || !isReal"' in js and "测试数据不能用于调整线上预警" in js
    js = read("x_backtest.js")
    assert "/docx" in js and "导出 Word" in js                    # AC-2.4 下载入口
    assert "prompt(" not in js and "confirm(" not in js           # 采用阈值就地确认，不弹对话框


# ----------------------------------------------------------------------------- 上传与页面状态

def test_upload_requires_source_and_two_files(ui):
    out, _ = ui
    c = out["can"]
    assert not c["none"]["ok"] and not c["one"]["ok"] and "至少要 2 期" in c["one"]["why"]
    assert not c["noSrc"]["ok"] and "数据来源" in c["noSrc"]["why"]
    assert c["two"]["ok"]
    assert out["hint"][0] == "" and "还差 1 期" in out["hint"][1] and "弱口径" in out["hint"][2] and out["hint"][3] == "已选 6 期"
    assert out["split"] == {"ok": ["a.xer", "B.XER"], "bad": ["c.xlsx"]}
    assert out["exclOk"] == [True, True, False]                    # 4 期剔除 3 期就不足 2 期
    p = out["page"]
    assert p["picked"]["files"] == ["p1.xer", "p2.XER"] and p["picked"]["bad"] == ["note.docx"] and p["picked"]["panel"]
    assert not p["picked"]["can"]["ok"]                            # 选了文件但还没选来源
    assert p["noSourcePosted"] is False and "数据来源" in p["noSourceErr"]
    post = p["runPost"]
    assert post[1] == "/api/backtest/run"
    items = post[2]
    assert [x for x in items if x[0] == "files"] == [["files", "p1.xer"], ["files", "p2.XER"]]
    assert ["source", "test"] in items and ["label", "合成"] in items and ["tolerance_days", "0"] in items
    assert not any(x[0] == "exclude_periods" for x in items)
    a = p["afterRun"]
    assert a["src"] == "test" and a["panel"] is False and a["files"] == 0
    assert a["toast"][1].startswith("回测完成") and "%" not in a["toast"][1]


def test_empty_state_button_opens_file_picker_directly(ui):
    out, _ = ui
    e = out["page"]["empty"]
    assert e["v"] is None and e["title"] == "还没有回测" and e["action"] == "选择 .xer 文件" and e["alt"] is None
    assert e["clicks"] == 1                                        # 空状态按钮直接 input.click()（U-05①），不跳页
    assert e["panelAfterClick"] is False                           # 取消选择时页面不变；选好文件才展开上传面板
    js = read("x_backtest.js")
    assert re.search(r'<input v-if="canRun" ref="file" type="file" accept="\.xer" multiple hidden', js)   # 原生文件框隐藏，只有一个入口


def test_page_load_prefers_real_then_test_and_role_handling(ui):
    out, v = ui
    p = out["page"]
    assert p["fallback"]["src"] == "test" and p["fallback"]["id"] == v["id"] and p["fallback"]["err"] == ""
    assert p["fallback"]["raw"] is True                            # 大结果不做深层响应式
    assert p["fallback"]["liveRed"] == 1
    assert p["byId"] == {"id": v["id"], "src": "test", "get": True}
    rr = p["rerunPost"]
    assert rr[1] == "/api/backtest/run" and ["exclude_periods", json.dumps([v["periods_report"][1]["data_date"]])] in rr[2]
    assert ["source", "test"] in rr[2] and ["label", "合成四期"] in rr[2]
    nf = p["rerunNoFiles"]
    assert nf == {"clicks": 1, "panel": True, "source": "test", "exclude": [v["periods_report"][1]["data_date"]], "posted": False}
    adm = p["admin"]
    assert adm["canRun"] is False and adm["canAdopt"] is False and adm["action"] is None and adm["clicks"] == 0 and "工程主管" in adm["reason"]
    assert "工程主管" in p["pm"]["err"] and p["pm"]["v"] is None


def test_adopt_rejected_for_test_source(env):
    """leader 决定（10-08）：仿真/测试数据的回测不能用于调整线上预警，采用阈值返回 409，项目设置与审计都不动。"""
    from contextlib import closing

    from app import db as dbmod
    from app.services import progress as prog
    _add_user(env, "eng", "eng_sup")
    eng = login("eng")
    t = eng.post("/api/backtest/run", files=_files([_period(k) for k in range(4)]), data={"source": "test"}).json()
    r = eng.post(f"/api/backtest/{t['id']}/adopt", json={"red_slip_days": 2, "tolerance_days": 3})
    assert r.status_code == 409 and "测试数据不能用于调整线上预警" in r.json()["detail"]
    with closing(dbmod.connect()) as conn:
        assert prog.thresholds(conn, env["project_id"])["red_slip_days"] == prog.DEFAULT_THRESHOLDS["red_slip_days"]
        assert conn.execute("SELECT adopted_at FROM backtests WHERE id=?", (t["id"],)).fetchone()["adopted_at"] is None
        assert conn.execute("SELECT COUNT(*) n FROM audit WHERE action='backtest.adopt'").fetchone()["n"] == 0
    assert login("pm").post(f"/api/backtest/{t['id']}/adopt", json={"red_slip_days": 2, "tolerance_days": 3}).status_code == 403   # 权限先于来源
    real = eng.post("/api/backtest/run", files=_files([_period(k) for k in range(4)]), data={"source": "real"}).json()
    assert eng.post(f"/api/backtest/{real['id']}/adopt", json={"red_slip_days": 2, "tolerance_days": 3}).status_code == 200


def test_pm_and_leader_get_403_from_backtest_api(env):
    _add_user(env, "lead", "leader")
    for u in ("pm", "lead"):
        assert login(u).get("/api/backtest/latest").status_code == 403, u


# ----------------------------------------------------------------------------- 静态：颜色令牌、资源注册

COLOR_PROPS = re.compile(r"^(color|background(-color)?|border(-(top|right|bottom|left))?(-color)?|outline(-color)?|box-shadow|fill|stroke|text-decoration-color|caret-color)$")
NAMED = re.compile(r"\b(white|black|red|green|blue|orange|yellow|purple|violet|gray|grey|pink|navy|teal|silver|gold)\b", re.I)


def _defined_tokens() -> set:
    toks = set()
    for f in STATIC.glob("*.css"):
        toks |= set(re.findall(r"(--[\w-]+)\s*:", f.read_text(encoding="utf-8")))
    return toks


def test_css_uses_color_tokens_only():
    css = read("x_backtest.css")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), "x_backtest.css 不得写十六进制色值"
    assert not re.search(r"\b(rgba?|hsla?|hwb|lab|lch|color-mix)\(", body), "x_backtest.css 不得写函数色值"
    decls = re.findall(r"([\w-]+)\s*:\s*([^;{}]+)", re.sub(r"[^{}]*\{", "{", body))
    assert decls
    for prop, val in decls:
        if COLOR_PROPS.match(prop.strip()):
            assert not NAMED.search(re.sub(r"var\([^)]*\)", "", val)), (prop, val)
    toks = _defined_tokens()
    used = set(re.findall(r"var\((--[\w-]+)", body))
    assert used and used <= toks, used - toks
    assert not {"--violet", "--violet-soft"} & used                # AI/主色统一中港蓝，不用紫
    js = read("x_backtest.js")
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", js), "模板里也不写色值"


def test_index_registers_assets_once():
    html = read("index.html")
    assert html.count("/static/x_backtest.css?v=3") == 1 and html.count("/static/x_backtest.js?v=5") == 1
    assert html.index("/static/x_backtest.js") < html.index("/static/nav.js") < html.index("/static/main.js")
    assert html.index("/static/core.js") < html.index("/static/x_backtest.js")
    assert "x_ui.css?v=17" in html and "x_ui.js?v=13" in html and "views.js?v=72" in html      # 不动已有版本号（G-12）；views.js、x_ui.js 改过就跟随新版本号（T-17、D 版换皮、计划体检页头、P6 横道图、回放预警标识）
    if NODE:
        r = subprocess.run([NODE, "--check", str(STATIC / "x_backtest.js")], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
