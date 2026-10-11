"""T-09b 拍照与票据（前端，node 直接加载 camera.js / x_tickets.js，桩掉浏览器与 Vue）：

- 竖拍（D4、AC-7、AC-1.9）：小票、隐患照片（会进识别队列的随手拍/日志/随手记）、整改照片允许竖拍；隐蔽验收、私信照片仍只收横屏；
- 拍摄时间以服务器校正时钟为准：有 serverNow()（T-09a）就用它，没有或取值异常时退回 wmClock（N-10③）；
- 上传状态（F-18④、F-20、AC-1.13）：上传完成之前不显示“已收到，识别中”，慢网时显示已上传秒数；
- 票据识别超时（U-04、AC-1.11）：pending/running 超过 10 分钟显示“识别超时 · 重新识别／手工填写”；
- 票据名称（U-33）：前端名称表与后端 DOC_KINDS 一致，选择处与列表同一叫法。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
NODE = shutil.which("node")


def read(n):
    return (STATIC / n).read_text(encoding="utf-8")


# 浏览器与 Vue 的最小桩：定时器只登记不触发（由用例手动 fire），画布记录 fillText，Image.decode 取 ctx.imgW/imgH
HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const ls = {};
const ctx = { console, toasts: [], posts: [], timers: [], ls };
ctx.setTimeout = (f, ms) => { ctx.timers.push({ f, ms, kind: 't' }); return ctx.timers.length; };
ctx.setInterval = (f, ms) => { ctx.timers.push({ f, ms, kind: 'i' }); return ctx.timers.length; };
ctx.clearTimeout = ctx.clearInterval = (id) => { if (id && ctx.timers[id - 1]) ctx.timers[id - 1].cleared = true; };
ctx.fire = (kind, ms) => { for (const t of ctx.timers.slice()) if (!t.cleared && t.kind === kind && (ms === undefined || t.ms === ms)) { if (kind === 't') t.cleared = true; t.f(); } };
ctx.requestAnimationFrame = () => 0; ctx.cancelAnimationFrame = () => {};
ctx.Vue = { reactive: (o) => o, markRaw: (o) => o };
ctx.store = { me: { user: { id: 7 }, project: { name: '测试项目' }, ai: {} }, cam: null, route: { params: {} } };
ctx.EXT = { slot: () => {}, route: (k, c) => { (ctx.routes = ctx.routes || {})[k] = c; } };
ctx.LEVEL_F = {};
ctx.toast = (m, e) => ctx.toasts.push(String(m));
ctx.lsGet = (k, d) => (k in ls ? JSON.parse(JSON.stringify(ls[k])) : d);
ctx.lsSet = (k, v) => { ls[k] = JSON.parse(JSON.stringify(v)); };
ctx.GET = async () => ({ headers: { get: () => null } });
ctx.POST = async (u, b) => { ctx.posts.push(u); return {}; };
ctx.PUT = async () => ({}); ctx.api = async () => ({});
ctx.can = () => false; ctx.roleOf = () => 'tech'; ctx.go = () => {}; ctx.confirm = () => true;
ctx.fmtDT = (s) => (s ? String(s).replace('T', ' ').slice(0, 16) : ''); ctx.fmtN = (v) => String(v); ctx.today = () => '2026-10-07';
ctx.ReviewTable = {}; ctx.reviewStatus = () => ({ remaining: 0 }); ctx.reviewPayload = () => [];
ctx.navigator = { geolocation: null, mediaDevices: null };
ctx.isSecureContext = false;
ctx.matchMedia = () => ({ matches: true });
ctx.screen = { orientation: { lock: (o) => { ctx.locked = o; return Promise.resolve(); }, unlock: () => {} } };
const fakeCtx2d = () => ({ texts: [], font: '', fillStyle: '', textBaseline: '', save() {}, restore() {}, beginPath() {}, moveTo() {}, arcTo() {}, closePath() {}, clip() {},
  fillRect() {}, drawImage() {}, xs: [], fillText(t, x) { this.texts.push(String(t)); this.xs.push(Math.round(x)); }, measureText(t) { return { width: String(t).length * 10 }; } });
ctx.canvases = [];
ctx.document = { documentElement: { requestFullscreen: () => { ctx.fullscreen = true; return Promise.resolve(); } }, fullscreenElement: null, exitFullscreen: () => {},
  createElement: () => { const g = fakeCtx2d(); const c = { width: 0, height: 0, g, getContext: () => g, toBlob: (cb) => cb({ size: 1 }) }; ctx.canvases.push(c); return c; } };
ctx.imgW = 1920; ctx.imgH = 1080;
ctx.Image = function () { this.complete = false; this.naturalWidth = 0; this.naturalHeight = 0; this.decode = async () => { this.naturalWidth = ctx.imgW; this.naturalHeight = ctx.imgH; }; };
ctx.URL = { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} };
ctx.File = function (parts, name, opts) { this.name = name; this.type = opts.type; this.lastModified = opts.lastModified; };
ctx.window = ctx;
vm.createContext(ctx);
let src = '';
for (const f of __FILES__) src += fs.readFileSync(f, 'utf8') + '\n;';
vm.runInContext(__SETUP__, ctx);
const p = vm.runInContext(src + '\n;(async () => { const out = {}; ' + __SNIPPET__ + '\n; return out; })()', ctx);
Promise.resolve(p).then((o) => process.stdout.write(JSON.stringify(o)), (e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def run_js(files, snippet, setup=""):
    if not NODE:
        pytest.skip("没有 node")
    code = (HARNESS.replace("__FILES__", json.dumps([str(STATIC / f) for f in files]))
            .replace("__SETUP__", json.dumps(setup or "0")).replace("__SNIPPET__", json.dumps(snippet)))
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr[-1500:]
    return json.loads(r.stdout)


def cam(snippet, setup=""):
    return run_js(["camera.js"], snippet, setup)


# ----------------------------------------------------------------------------- 服务器校正时钟（N-10③）
def test_wmnow_prefers_server_now_date():
    out = cam("out.d = wmNow() - SN;", "var SN = Date.now() + 3600000; this.serverNow = () => new Date(SN);")
    assert abs(out["d"]) < 50


def test_wmnow_accepts_server_now_millis():
    out = cam("out.d = wmNow() - SN; out.off = Math.round((wmOff() - 3600000) / 1000);", "var SN = Date.now() + 3600000; this.serverNow = () => SN;")
    assert abs(out["d"]) < 50 and out["off"] == 0


def test_wmnow_falls_back_to_wmclock_without_server_now():
    out = cam("wmClock.off = -7200000; out.d = Math.round((wmNow() - Date.now() + 7200000) / 1000); out.t = typeof serverNow;")
    assert out == {"d": 0, "t": "undefined"}


def test_wmnow_falls_back_when_server_now_is_bad():
    out = cam("wmClock.off = 5000; out.a = Math.round((wmNow() - Date.now()) / 1000); this.serverNow = () => NaN; out.b = Math.round((wmNow() - Date.now()) / 1000);",
              "this.serverNow = () => { throw new Error('no clock'); };")
    assert out == {"a": 5, "b": 5}


# ----------------------------------------------------------------------------- 竖拍规则（D4、AC-7、AC-1.9）
def test_portrait_policy_by_caller():
    out = cam("""
      out.ticket = wmPortraitOk({ allowPortrait: true });
      out.plain = wmPortraitOk({});
      out.rectify = wmPortraitOk({ multi: false });
      out.optout = wmPortraitOk({ multi: false, allowPortrait: false });
      takePhotos(); out.camPlain = store.cam.portrait;
      takePhotos({ allowPortrait: true }); out.camTicket = store.cam.portrait;
      takePhotosToUpload(); out.camHazard = store.cam.portrait;
      out.orient = [wmOrientOk(1080, 1920, false), wmOrientOk(1080, 1920, true), wmOrientOk(1920, 1080, false)];
    """)
    assert out == {"ticket": True, "plain": False, "rectify": True, "optout": False, "camPlain": False, "camTicket": True, "camHazard": True,
                   "orient": [False, True, True]}


def _stamp(portrait, w, h, extra=""):
    return cam(f"""
      const f = await wmStampFile({{ lastModified: Date.now() - 60000 }}, {json.dumps(portrait)});
      out.ok = !!f; out.toasts = toasts; out.taken = f ? f.takenAt : null;
      out.expect = wmIso(new Date(Date.now() - 60000 + wmOff()));
    """, f"this.imgW = {w}; this.imgH = {h}; {extra}")


def test_system_camera_accepts_portrait_when_allowed():
    out = _stamp(True, 1080, 1920)
    assert out["ok"] and not any("横屏" in t for t in out["toasts"]), out


def test_system_camera_rejects_portrait_for_other_photos():
    out = _stamp(False, 1080, 1920)
    assert not out["ok"] and any("横屏" in t for t in out["toasts"]), out
    assert _stamp(False, 1920, 1080)["ok"]


def test_system_camera_time_uses_server_clock():
    out = _stamp(True, 1080, 1920, "var SN = Date.now() + 3 * 3600000; this.serverNow = () => new Date(SN);")
    assert out["ok"] and out["taken"] == out["expect"], out


def test_in_app_camera_portrait_gate():
    out = cam("""
      const c = WatermarkCamera.computed;
      out.portraitOk = c.canShoot.call({ ready: true, land: false, ok: true, saving: false, portrait: true });
      out.portraitNo = c.canShoot.call({ ready: true, land: false, ok: true, saving: false, portrait: false });
      out.landOk = c.canShoot.call({ ready: true, land: true, ok: true, saving: false, portrait: false });
      store.cam = { multi: true, portrait: true }; out.fromStore = c.portrait.call({});
      // 允许竖拍时不进全屏、不锁横屏；只收横屏的照片仍然锁
      const m = WatermarkCamera.mounted, fake = (p) => ({ portrait: p, open() {}, $forceUpdate() {} });
      const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
      m.call(fake(true)); await settle(); out.pFs = fullscreen; out.pLock = locked;
      m.call(fake(false)); await settle(); out.lFs = fullscreen; out.lLock = locked;
    """, "this.locked = null; this.fullscreen = false;")
    assert out["portraitOk"] and not out["portraitNo"] and out["landOk"] and out["fromStore"]
    assert (out["pFs"], out["pLock"]) == (False, None) and (out["lFs"], out["lLock"]) == (True, "landscape"), out


def test_in_app_camera_shoot_accepts_portrait_frame():
    out = cam("""
      const shoot = WatermarkCamera.methods.shoot;
      const mk = (p) => ({ canShoot: true, portrait: p, saving: false, flash: false, shots: [], $refs: { v: { videoWidth: 720, videoHeight: 1280 } }, finish() { this.done = true; } });
      store.cam = { multi: true };
      const a = mk(true); await shoot.call(a); out.portrait = a.shots.length;
      const b = mk(false); await shoot.call(b); out.land = b.shots.length;
    """)
    assert out == {"portrait": 1, "land": 0}


def test_in_app_camera_templates_follow_portrait_flag():
    src = read("camera.js")
    tpl = re.search(r"const WatermarkCamera = \{\s*template: `(.*?)`", src, re.S).group(1)
    assert 'class="cam-rot" v-if="ready && !land && !portrait"' in tpl
    sheet = re.search(r"const WatermarkSheet = \{\s*template: `(.*?)`", src, re.S).group(1)
    assert "须横屏拍摄" in sheet and "portrait" in sheet  # 只在只收横屏时提示


def _calls(src):
    """(位置, 调用名, 参数文本)：全部 takePhotos(...) / takePhotosToUpload(...) 调用。"""
    return [(m.start(), m.group(1), m.group(2)) for m in re.finditer(r"\b(takePhotos|takePhotosToUpload)\(([^()]*)\)", src)]


def _portrait(name, args):
    if "allowPortrait: false" in args:
        return False
    return name == "takePhotosToUpload" or "allowPortrait: true" in args or "multi: false" in args


def test_every_caller_gets_the_intended_orientation_rule():
    """整改照片（purpose=rectify）、小票、会进识别队列的照片允许竖拍；隐蔽验收、私信照片只收横屏。新增调用方须显式归类。"""
    seen = {}
    for f in sorted(STATIC.glob("*.js")):
        if f.name == "camera.js":
            continue
        src = f.read_text(encoding="utf-8")
        calls = _calls(src)
        for pos in [m.start() for m in re.finditer(r'"purpose", "rectify"', src)]:
            prev = [c for c in calls if c[0] < pos]
            assert prev and _portrait(prev[-1][1], prev[-1][2]), f"{f.name}: 整改照片须允许竖拍"
        for _, name, args in calls:
            seen.setdefault(f.name, []).append(_portrait(name, args))
    assert seen["x_tickets.js"] == [True], seen
    assert seen["x_conceal.js"] == [False] and seen["x_social.js"] == [False], seen
    assert all(seen["views.js"]) and all(seen["tech.js"]) and all(seen["todo.js"]), seen


def test_watermark_place_and_work_remembered_per_user():
    out = cam("lsSet('zb_wm_place_7', 'CH10+950'); lsSet('zb_wm_work_7', '承台浇筑'); wmFormLoad(); out.f = [wmForm.area, wmForm.work];")
    assert out["f"] == ["CH10+950", "承台浇筑"]


# ----------------------------------------------------------------------------- 上传状态（F-18④、F-20、AC-1.13）
def test_feedback_does_not_claim_received_before_upload_finishes():
    out = cam("""
      const h = photoFeedback.begin(1, true), b = fbState.items[0];
      out.before = b.text; out.stateBefore = b.state;
      for (let i = 0; i < 12; i++) fire('i', 1000);
      out.slow = b.text;
      h.sent([{ id: 5 }]);
      out.after = b.text; out.stateAfter = b.state;
      out.tickerCleared = timers.filter((t) => t.kind === 'i')[0].cleared;   // 上传计时停了；之后另起“识别中 · 已等 N 秒”的计时（验收 I10）
      out.waitTicker = timers.filter((t) => t.kind === 'i').length === 2;
    """)
    assert out["waitTicker"], out
    assert "识别中" not in out["before"] and "已收到" not in out["before"] and "上传" in out["before"], out
    assert out["stateBefore"] == "upload" and "12 秒" in out["slow"] and "识别中" not in out["slow"], out
    assert out["after"] == "已收到 1 张，识别中" and out["stateAfter"] == "wait" and out["tickerCleared"], out


def test_feedback_no_scan_queued_and_fail_texts():
    out = cam("""
      const a = photoFeedback.begin(2, false), ba = fbState.items[0]; out.a0 = ba.text; a.sent([{ id: 1 }, { id: 2 }]); out.a1 = ba.text;
      const q = photoFeedback.begin(1, true), bq = fbState.items[fbState.items.length - 1]; q.sent([{ id: 'local:x', queued: true }]); out.q = bq.text;
      const f = photoFeedback.begin(1, true), bf = fbState.items[fbState.items.length - 1]; f.fail('网络不可用'); out.f = bf.text; out.fs = bf.state;
      out.allCleared = timers.filter((t) => t.kind === 'i').every((t) => t.cleared);
    """)
    assert "上传" in out["a0"] and "已收到" not in out["a0"]
    assert out["a1"] == "已上传 2 张（未做隐患识别）"
    assert out["q"].startswith("已暂存 1 张")
    assert out["f"] == "照片上传失败：网络不可用" and out["fs"] == "fail" and out["allCleared"], out


def test_feedback_upload_icon_exists():
    assert re.search(r"upload: \"arrow-up\"", read("camera.js")) and '"arrow-up":' in read("icons.js")


# ----------------------------------------------------------------------------- 票据（U-04、U-33、AC-7、AC-1.11）
def tk(snippet, setup=""):
    pre = "const T = routes.tickets, D = T.components.XtDraft;"
    return run_js(["camera.js", "x_tickets.js"], pre + snippet, setup)


def _iso_ago(minutes):
    return f"new Date(NOW - {minutes} * 60000).toISOString()"


NOW = "var NOW = Date.parse('2026-10-07T10:00:00+08:00'); this.serverNow = () => new Date(NOW);"


def test_ticket_stuck_after_ten_minutes():
    out = tk(f"""
      const st = (s, m) => D.computed.stuck.call({{ t: {{ id: 1, ai_status: s, created_at: {_iso_ago('m')} }}, now: 0 }});
      out.p9 = st('pending', 9); out.p11 = st('pending', 11); out.r30 = st('running', 30); out.f = st('failed', 99); out.d = st('done', 99);
      out.w = D.computed.waitText.call({{ t: {{ id: 1, ai_status: 'running', created_at: {_iso_ago(15 * 24 * 60)} }}, now: 0 }});
      out.w2 = D.computed.waitText.call({{ t: {{ id: 1, ai_status: 'running', created_at: {_iso_ago(25)} }}, now: 0 }});
    """, NOW)
    assert (out["p9"], out["p11"], out["r30"], out["f"], out["d"]) == (False, True, True, False, False), out
    assert out["w"] == "15 天" and out["w2"] == "25 分钟", out


def test_ticket_stuck_uses_server_clock_not_phone_clock():
    # 手机时钟比服务器快一天：按服务器时钟只等了 3 分钟，不算超时
    out = tk(f"""out.s = D.computed.stuck.call({{ t: {{ id: 1, ai_status: 'pending', created_at: {_iso_ago(3)} }}, now: 0 }});""",
             NOW + " Date.now = () => NOW + 86400000;")
    assert out["s"] is False


def test_ticket_stuck_template_and_actions():
    src = read("x_tickets.js")
    tpl = re.search(r"const XtDraft = \{.*?template: `(.*?)`", src, re.S).group(1)
    assert "识别超时" in tpl and "重新识别" in tpl and "手工填写" in tpl
    assert re.search(r'class="thinking" v-if="recognizing && !stuck"', tpl), "超时后不再显示一直转的“识别中…”"
    assert "|| manual" in tpl, "点“手工填写”就地展开核对表"


def test_ticket_retry_success_resets_wait_clock():
    out = tk(f"""
      const t = {{ id: 9, ai_status: 'running', created_at: {_iso_ago(60 * 24)} }};
      const vm = {{ t, now: 0, manual: true, emitted: [], $emit(e) {{ this.emitted.push(e); }} }};
      out.before = D.computed.stuck.call(vm);
      await D.methods.retry.call(vm);
      out.posts = posts; out.emitted = vm.emitted; out.manual = vm.manual;
      t.ai_status = 'pending';
      out.after = D.computed.stuck.call(vm);
    """, NOW)
    assert out["before"] is True and out["posts"] == ["/api/tickets/9/reocr"] and out["emitted"] == ["changed"], out
    assert out["after"] is False and out["manual"] is False, out


def test_ticket_retry_refused_opens_manual_fill():
    out = tk(f"""
      POST = async () => {{ const e = new Error('票据正在识别中或已确认，不能重试'); e.status = 409; throw e; }};
      const vm = {{ t: {{ id: 3, ai_status: 'pending', created_at: {_iso_ago(60)} }}, now: 0, manual: false, $emit() {{}} }};
      await D.methods.retry.call(vm);
      out.manual = vm.manual; out.toasts = toasts;
    """, NOW)
    assert out["manual"] is True and any("手工填写" in t for t in out["toasts"]), out


def test_ticket_polling_skips_stuck_drafts():
    out = tk(f"""
      const sched = T.methods.schedule;
      const vm = (ts) => ({{ drafts: ts, polls: 0, timer: 0, now: 0 }});
      const a = vm([{{ id: 1, ai_status: 'running', created_at: {_iso_ago(30)} }}]); sched.call(a);
      out.stuckOnly = timers.filter((t) => !t.cleared && t.kind === 't').length;
      const b = vm([{{ id: 2, ai_status: 'pending', created_at: {_iso_ago(1)} }}]); sched.call(b);
      out.fresh = timers.filter((t) => !t.cleared && t.kind === 't').length;
    """, NOW)
    assert out == {"stuckOnly": 0, "fresh": 1}, out


def test_ticket_snap_allows_portrait():
    out = tk("""
      let got = null; takePhotos = async (o) => { got = o; return []; };
      await T.methods.snap.call({ kind: 'auto', busy: false });
      out.opts = got;
    """)
    assert out["opts"] == {"allowPortrait": True}


def test_ticket_kind_names_match_backend():
    from app.ai.ticket_agent import DOC_KINDS
    out = tk("""
      out.kind = D.data.call({ t: { doc_kind: 'cert', fields: {}, category: null } }).KIND;
      out.pick = T.data.call({}).KIND_PICK;
    """)
    assert out["kind"] == DOC_KINDS
    picks = dict(out["pick"])
    assert picks.pop("auto") == "自动判断" and picks == DOC_KINDS
