"""T-09a 离线·前端队列（P0-1 前端部分）：node 加载前端脚本做行为测试 + 静态检查。

- core.js：可暂存的写请求超时（纯文字 15 秒；上传 20 秒 + 体积 ÷ 50KB/s，最长 90 秒）转暂存，其余请求不加超时；
  网络失败提示“信号不好，已保留你填的内容”；422 的结构化 detail 带出 code；serverNow() 按响应头 Date 校正并存本机；
  VoiceButton 的 note 模式录完即交出录音（不等转写）。
- x_quick.js：语音一律先存本机再传；整改提交引用暂存照片时入队（action），补传时先传照片再提交；
  整改照片被拒（photo_before_dispatch）标失败写原因，可“改作随手拍”（同一张照片改 purpose=safety 重新入队）；
  补传自身也有超时，挂住的请求不卡队列；超时重发沿用原 client_id；服务器上未转写完的录音并入随手记列表。
- tech.js：整改复核按 4.4 契约显示“AI 建议”，闭环前不出现“已消除”这类肯定说法；规则提示合并（U-06）；迟到记录（D5）。
- tools/offline_check.mjs：拒绝 8600/8601，--base 必填。浏览器端到端用例由该脚本在一次性实例上跑，不在 pytest 里启动浏览器。
- T-10c 离线启动（AC-5）：core.js 缓存上次的 /api/me（只留显示身份与导航的字段），断网时已登录过的用户凭它进入离线模式，
  页头提示“离线模式：……”；退出登录、登录过期清缓存；服务器连回来自动退出离线模式；core.js 注册 SW，index.html 带 manifest。
  tech.js 首页没有本机首页缓存时也给出随手记；安全员首页离线时给出随手拍。offline_check.mjs 的 --sw 模式（S1～S6）跑真机链路。

浏览器 API（IndexedDB、fetch）在 node 里用最小桩代替，只测本仓库代码的分支与数据流。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
TOOL = ROOT / "tools" / "offline_check.mjs"
NODE = shutil.which("node")


def read(n):
    return (STATIC / n).read_text(encoding="utf-8")


# 把 index.html 里的前端脚本（main.js 除外：它一加载就挂载应用）在 vm 里加载，再跑一段测试代码，输出 JSON
HARNESS = r"""
const vm = require('vm'), fs = require('fs'), path = require('path');
const [dir, snippetFile] = process.argv.slice(2);
const noop = () => {};
// 最小 IndexedDB：只实现 x_quick.js 用到的 open/upgrade、objectStore 的 put/get/getAll/delete、事务完成回调
function fakeIDB() {
  const dbs = {};
  return { open(name) {
    const rq = {};
    setTimeout(() => {
      const fresh = !dbs[name], stores = dbs[name] = dbs[name] || {};
      const db = {
        createObjectStore(n, o) { stores[n] = { key: o.keyPath, rows: new Map() }; },
        transaction(n) {
          const tx = { oncomplete: null, onerror: null, onabort: null, error: null }, s = stores[n];
          let pending = 0;
          const req = (fn) => { pending++; const r = { result: undefined, onsuccess: null };
            setTimeout(() => { r.result = fn(); if (r.onsuccess) r.onsuccess(); if (--pending === 0) setTimeout(() => tx.oncomplete && tx.oncomplete(), 0); }, 0); return r; };
          tx.objectStore = () => ({
            put: (v) => req(() => { s.rows.set(v[s.key], { ...v }); return v[s.key]; }),
            delete: (k) => req(() => { s.rows.delete(k); }),
            get: (k) => req(() => { const v = s.rows.get(k); return v ? { ...v } : undefined; }),
            getAll: () => req(() => [...s.rows.values()].map((v) => ({ ...v }))),
          });
          setTimeout(() => { if (pending === 0 && tx.oncomplete) tx.oncomplete(); }, 0);
          return tx;
        },
      };
      rq.result = db;
      if (fresh && rq.onupgradeneeded) rq.onupgradeneeded();
      if (rq.onsuccess) rq.onsuccess();
    }, 0);
    return rq;
  } };
}
function el() {
  const o = { style: {}, setAttribute: noop, appendChild: noop, childNodes: [], textContent: '', children: [] };
  const dec = (v) => String(v).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&');
  Object.defineProperty(o, 'innerHTML', {
    set(v) { const m = /^<div foo="([\s\S]*)">$/.exec(String(v)); this.textContent = dec(v); this.children = m ? [{ getAttribute: () => dec(m[1]) }] : []; },
    get() { return this.textContent; },
  });
  return o;
}
const mem = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k),
  get length() { return m.size; }, key: (i) => { const ks = [...m.keys()]; return i < ks.length ? ks[i] : null; } }; };
const et = new EventTarget();
const ctx = { console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: noop, navigator: { userAgent: '', onLine: true },
  localStorage: mem(), sessionStorage: mem(), location: { hash: '', origin: 'http://127.0.0.1:8619' },
  addEventListener: et.addEventListener.bind(et), removeEventListener: et.removeEventListener.bind(et), dispatchEvent: et.dispatchEvent.bind(et),
  document: { addEventListener: noop, createElement: el, querySelector: () => null, getElementById: () => null, documentElement: { style: {} },
              visibilityState: 'visible', body: { classList: { add: noop, remove: noop } } },
  indexedDB: fakeIDB(), crypto: require('crypto').webcrypto, FormData, Blob, File, AbortController, Response, Headers, Event, CustomEvent, EventTarget,
  URL, TextEncoder, DOMException, confirm: () => true, prompt: () => '', matchMedia: () => ({ matches: false }), isSecureContext: true };
ctx.window = ctx; ctx.self = ctx;
for (const n of ['Image', 'XMLHttpRequest', 'MediaRecorder', 'ResizeObserver', 'IntersectionObserver', 'HTMLElement', 'Element']) ctx[n] = function () {};
ctx.fetch = async () => { throw new TypeError('no network'); };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(dir, 'vendor/vue.global.prod.js'), 'utf8'), ctx);
const order = fs.readFileSync(path.join(dir, 'index.html'), 'utf8').match(/\/static\/[\w.\/-]+\.js/g).filter((x) => !x.includes('vendor') && !x.includes('main.js'));
let src = '';
for (const f of order) src += fs.readFileSync(path.join(dir, f.replace(/^\/static\//, '').replace(/\?.*$/, '')), 'utf8') + '\n;';
vm.runInContext(src, ctx);
const errs = [];
process.on('unhandledRejection', (e) => errs.push(String((e && e.message) || e)));
const body = fs.readFileSync(snippetFile, 'utf8');
Promise.resolve(vm.runInContext('(async () => {\n' + body + '\n})()', ctx)).then(
  (out) => process.stdout.write(JSON.stringify({ out: out === undefined ? null : out, errs }), () => process.exit(0)),
  (e) => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
"""

# 每段测试共用的开头：技术员已登录；关掉自动补传的定时触发，由测试显式调用 qSync
SETUP = """
qKick = () => {};
store.me = { user: { id: 7, role: 'tech', name: '张工' }, today: '2026-10-07', ai: { asr: true, provider: 'mock' }, can: [] };
await qReady;
const J = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { 'content-type': 'application/json' } });
const hang = (u, init) => new Promise((res, rej) => { if (init && init.signal) init.signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))); });
const fields = (fd) => Object.fromEntries([...fd.entries()].map(([k, v]) => [k, typeof v === 'string' ? v : 'blob:' + v.size]));
const allQ = () => qrun('q', 'readonly', (s) => s.getAll());
"""


def run_app(snippet: str):
    if not NODE:
        pytest.skip("没有 node")
    with tempfile.TemporaryDirectory() as td:
        h, s = Path(td) / "harness.js", Path(td) / "snippet.js"
        h.write_text(HARNESS, encoding="utf-8")
        s.write_text(SETUP + snippet, encoding="utf-8")
        r = subprocess.run([NODE, str(h), str(STATIC), str(s)], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(r.stdout)
    assert not res["errs"], res["errs"]
    return res["out"]


# ============================================================================ core.js：超时、错误提示、服务器时钟、语音 note 模式

def test_timeout_constants_and_upload_formula():
    out = run_app("""
const fd = new FormData(); fd.append('purpose', 'log'); fd.append('files', new Blob([new Uint8Array(512000)]), 'a.jpg');
const big = new FormData(); big.append('files', new Blob([new Uint8Array(20 * 1024 * 1024)]), 'b.jpg');
return { T: API_T, json: apiTimeout({ text: 'x' }), none: apiTimeout(undefined), up: apiTimeout(fd), big: apiTimeout(big) };
""")
    assert out["T"] == {"text": 15000, "upload": 20000, "rate": 51200, "max": 90000}
    assert out["json"] == 15000 and out["none"] == 15000
    assert out["up"] == round(20000 + 512003 / 51200 * 1000)      # 20 秒 + 体积 ÷ 50KB/s
    assert out["big"] == 90000                                     # 最长 90 秒


def test_queueable_post_times_out_into_queue_and_get_has_no_timeout():
    out = run_app("""
API_T.text = 60;
const calls = [];
window.__zbOffline = (phase, m, u, b, info) => {
  calls.push([phase, u, !!(info && info.timeout)]);
  if (phase === 'pre' && u === '/api/notes') return { url: '/api/quick/sync/note', body: { ...b, client_id: 'n-1' }, queue: true };
  if (phase === 'err') return { data: { queued: true, via: u } };
  return null;
};
fetch = hang;
const t0 = Date.now(); const r = await api('POST', '/api/notes', { text: 'x' }); const ms = Date.now() - t0;
let settled = false;
api('GET', '/api/tech/home').then(() => (settled = true), () => (settled = true));
api('POST', '/api/logs/1/draft', {}).then(() => (settled = true), () => (settled = true));   // 不可暂存的写请求（AI 起草可能很久）也不加超时
await new Promise((res) => setTimeout(res, 250));
return { r, ms, calls, settled };
""")
    assert out["r"] == {"queued": True, "via": "/api/quick/sync/note"}
    assert out["ms"] < 1500
    assert ["err", "/api/quick/sync/note", True] in out["calls"]
    assert out["settled"] is False


def test_network_error_message_and_structured_422():
    out = run_app("""
window.__zbOffline = null;
const res = {};
fetch = async () => { throw new TypeError('Failed to fetch'); };
try { await api('POST', '/api/logs/1/answer', {}); } catch (e) { res.post = [e.message, e.offline]; }
try { await api('GET', '/api/findings/1'); } catch (e) { res.get = [e.message, e.offline]; }
fetch = async () => J({ detail: { code: 'photo_before_dispatch', msg: '整改照片拍摄时间早于派单超过 24 小时' } }, 422);
try { await api('POST', '/api/quick/sync/rectify', {}); } catch (e) { res.obj = [e.message, e.code, e.status]; }
fetch = async () => J({ detail: '整改照片拍摄时间早于派单超过 24 小时', code: 'photo_before_dispatch' }, 422);
try { await api('POST', '/api/quick/sync/rectify', {}); } catch (e) { res.top = [e.message, e.code]; }
fetch = async () => J({ detail: [{ msg: 'a' }, { msg: 'b' }] }, 422);
try { await api('POST', '/api/x', {}); } catch (e) { res.list = [e.message, e.code]; }
return res;
""")
    assert out["post"] == ["信号不好，已保留你填的内容", True]
    assert out["get"] == ["信号不好，请稍后重试", True]
    assert out["obj"] == ["整改照片拍摄时间早于派单超过 24 小时", "photo_before_dispatch", 422]
    assert out["top"] == ["整改照片拍摄时间早于派单超过 24 小时", "photo_before_dispatch"]
    assert out["list"] == ["a；b", ""]


def test_server_clock_from_date_header_persists():
    out = run_app("""
window.__zbOffline = null;
const skew = 3600 * 1000;
fetch = async () => new Response('{}', { status: 200, headers: { date: new Date(Date.now() + skew).toUTCString() } });
await api('GET', '/api/me');
return { d: serverNow().getTime() - Date.now(), saved: JSON.parse(localStorage.getItem('zb_clock_off')), fn: typeof serverNow, isDate: serverNow() instanceof Date };
""")
    assert abs(out["d"] - 3600_000) < 1500
    assert abs(out["saved"] - out["d"]) < 1500
    assert out["fn"] == "function" and out["isDate"] is True


def test_voice_button_note_mode_hands_over_audio_without_asr():
    out = run_app("""
let asr = 0;
fetch = async (u) => { if (String(u).includes('/api/asr')) asr++; return J({ text: 'x' }); };
const emitted = [];
const me = { mode: 'note', sec: 8, at: new Date('2026-10-07T05:00:00Z'), busy: false, $emit: (n, p) => emitted.push([n, p]) };
await VoiceButton.methods.send.call(me, new Blob(['abc'], { type: 'audio/webm' }));
const plain = { mode: undefined, sec: 3, at: null, busy: false, $emit: (n, p) => emitted.push([n, p]) };
await VoiceButton.methods.send.call(plain, new Blob(['abc'], { type: 'audio/webm' }));
return { asr, emitted: emitted.map(([n, p]) => n === 'audio' ? [n, p.seconds, p.recordedAt.toISOString(), p.name, p.blob.size] : [n, p]), busy: me.busy };
""")
    assert out["emitted"][0] == ["audio", 8, "2026-10-07T05:00:00.000Z", "voice.webm", 3]
    assert out["emitted"][1] == ["text", "x"] and out["asr"] == 1           # 其他位置（提问、纪要）仍同步转写
    assert out["busy"] is False


# ============================================================================ x_quick.js：哪些请求可暂存、队列补传

def test_hook_plan_rewrites_and_enqueues():
    out = run_app("""
zbClock.off = 3600 * 1000;
const p1 = qPlan('POST', '/api/notes', { text: 'a' });
const p2 = qPlan('POST', '/api/notes', { text: 'a', media_ids: ['local:mx'] });
const p3 = qPlan('POST', '/api/findings/12/rectify', { media_id: 55 });
const p4 = qPlan('POST', '/api/findings/12/rectify', { media_id: 'local:m1' });
const fd = new FormData(); fd.append('purpose', 'rectify');
return {
  p1: { url: p1.url, queue: p1.queue, cid: p1.body.client_id.length > 8, skew: Date.parse(p1.body.client_time) - Date.now() },
  p2: p2.enqueue,
  p3: { url: p3.url, queue: p3.queue, finding_id: p3.body.finding_id, media_id: p3.body.media_id, cid: p3.body.client_id.length > 8 },
  p4: p4.enqueue, p5: qPlan('POST', '/api/media', fd), p6: qPlan('GET', '/api/notes'), p7: qPlan('POST', '/api/logs/1/draft', {}),
};
""")
    assert out["p1"]["url"] == "/api/quick/sync/note" and out["p1"]["queue"] is True and out["p1"]["cid"]
    assert abs(out["p1"]["skew"] - 3600_000) < 2000                 # client_time 用服务器校正时钟
    assert out["p2"] == "note"
    assert out["p3"] == {"url": "/api/quick/sync/rectify", "queue": True, "finding_id": 12, "media_id": 55, "cid": True}
    assert out["p4"] == "action"
    assert out["p5"] == {"queue": True}
    assert out["p6"] is None and out["p7"] is None


def test_voice_is_stored_locally_first_then_uploaded():
    out = run_app("""
const sent = [];
fetch = async (u, init) => { if (u !== '/api/quick/sync/voice') return J([]); sent.push([u, fields(init.body)]); return J({ pending_id: 3, status: 'pending', recorded_at: '', late_days: 0, client_id: init.body.get('client_id') }); };
const at = new Date(Date.now() - 2 * 3600e3);
const r = await QUICK.addVoice({ blob: new Blob(['abcd'], { type: 'audio/webm' }), name: 'voice.webm', seconds: 6, recordedAt: at,
                                 geo: { lat: 6.4, lon: 3.4, acc: 9, place: '3#墩' } });
const before = QUICK.pendingNotes().map((n) => [n.voice, n.queued, n.seconds, n.id.startsWith('local:v')]);
const n0 = QQ.n, label = QQ.items[0].label, sentBefore = sent.length;
await qSync(true);
return { queued: r.queued, before, n0, label, sentBefore, n1: QQ.n, sent, at: at.toISOString() };
""")
    assert out["queued"] is True and out["sentBefore"] == 0          # 先存本机，不等网络
    assert out["before"] == [[True, True, 6, True]] and out["n0"] == 1 and "录音" in out["label"]
    assert out["n1"] == 0
    (url, f), = out["sent"]
    assert url == "/api/quick/sync/voice"
    assert f["client_id"].startswith("v") and f["recorded_at"] == out["at"] and f["seconds"] == "6"
    assert f["geo_lat"] == "6.4" and f["geo_lon"] == "3.4" and f["geo_acc"] == "9" and f["place"] == "3#墩" and f["file"] == "blob:4"


def test_voice_formats_the_server_cannot_transcribe_are_refused_up_front():
    out = run_app("""
const res = {};
const tryAdd = async (k, blob, name) => { try { await QUICK.addVoice({ blob, name, seconds: 3 }); res[k] = 'ok'; } catch (e) { res[k] = e.message; } };
const ftyp = (brand) => new Blob([new Uint8Array([0, 0, 0, 24]), 'ftyp' + brand, new Uint8Array(20)], { type: '' });
await tryAdd('amr', new Blob(['#!AMR\\n', new Uint8Array(30)], { type: '' }), 'rec.dat');
await tryAdd('amrType', new Blob(['abc'], { type: 'audio/amr' }), 'a');
await tryAdd('threeGp', ftyp('3gp4'), 'voice');
await tryAdd('threeGpName', new Blob(['abc']), 'Recording 12.3gp');
await tryAdd('empty', new Blob([]), 'voice.webm');
await tryAdd('big', new Blob([new Uint8Array(25 * 1024 * 1024 + 1)]), 'voice.webm');
await tryAdd('m4a', ftyp('M4A '), 'voice.m4a');
await tryAdd('webm', new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 1, 2])], { type: 'audio/webm' }), 'voice.webm');
res.stored = (await allQ()).map((r) => r.name);
return res;
""")
    for k in ("amr", "amrType", "threeGp", "threeGpName"):
        assert "AMR/3GP" in out[k] and "改打字" in out[k], (k, out[k])
    assert out["empty"] == "录音是空的，请重录" and out["big"] == "录音超过 25MB，请分几段说"
    assert out["m4a"] == "ok" and out["webm"] == "ok"
    assert sorted(out["stored"]) == ["voice.m4a", "voice.webm"]                   # 不收的一条都没存进本机，不会显示“已记下”


def test_server_rejection_marks_failed_without_retrying():
    out = run_app("""
fetch = async () => { throw new TypeError('offline'); };
await POST('/api/notes', { text: 'a' });
let calls = 0;
fetch = async () => { calls++; return J({ detail: '同一个 client_id 已用于其他记录' }, 400); };
await qSync(true); await qSync(true);
const r = (await allQ())[0];
return { calls, status: r.status, tries: r.tries, err: r.err, bad: QQ.bad };
""")
    assert out == {"calls": 1, "status": "failed", "tries": 0, "err": "同一个 client_id 已用于其他记录", "bad": 1}


def test_offline_rectify_waits_for_photo_then_submits():
    out = run_app("""
fetch = async () => { throw new TypeError('offline'); };
const fd = new FormData(); fd.append('purpose', 'rectify'); addPhotos(fd, [Object.assign(new File([new Uint8Array(10)], 'r.jpg', { type: 'image/jpeg' }), { takenAt: '2026-10-07T09:00' })]);
const [m] = await POST('/api/media', fd);
const r = await POST('/api/findings/12/rectify', { media_id: m.id });
const kinds = QQ.items.map((x) => x.kind), pend = QUICK.pendingActions(12).map((a) => [a.status, a.fid]);
const sent = []; let purpose = '', rb = null;
fetch = async (u, init) => {
  sent.push(u);
  if (u === '/api/quick/sync/media') { purpose = init.body.get('purpose'); return J({ id: 88, client_id: init.body.get('client_id') }); }
  if (u === '/api/quick/sync/rectify') { rb = JSON.parse(init.body); return J({ id: 12, status: 'rectifying' }); }
  return J({}, 404);
};
await qSync(true);
return { mid: m.id, r, kinds, pend, sent, purpose, rb: { ...rb, client_id: rb.client_id.length > 8 }, left: QQ.items.length };
""")
    assert out["mid"].startswith("local:")
    assert out["r"]["queued"] is True and out["r"]["finding_id"] == 12
    assert out["kinds"] == ["media", "action"] and out["pend"] == [["pending", 12]]
    assert out["sent"] == ["/api/quick/sync/media", "/api/quick/sync/rectify"] and out["purpose"] == "rectify"
    assert out["rb"] == {"client_id": True, "finding_id": 12, "media_id": 88}
    assert out["left"] == 0


def test_rejected_rectify_photo_can_become_safety_snap():
    out = run_app("""
fetch = async () => { throw new TypeError('offline'); };
const fd = new FormData(); fd.append('purpose', 'rectify'); addPhotos(fd, [Object.assign(new File([new Uint8Array(12)], 'r.jpg', { type: 'image/jpeg' }), { takenAt: '2026-10-01T09:00' })]);
const [m] = await POST('/api/media', fd);
await POST('/api/findings/12/rectify', { media_id: m.id });
fetch = async (u, init) => u === '/api/quick/sync/media' ? J({ id: 88 }) : J({ detail: { code: 'photo_before_dispatch', msg: '整改照片拍摄时间早于派单超过 24 小时' } }, 422);
await qSync(true);
const a = QQ.items.find((x) => x.kind === 'action');
const failed = { status: a.status, code: a.code, err: a.err, canSafety: a.canSafety, bad: QQ.bad };
const ok = await QUICK.asSafety(a.cid);
const recs = (await allQ()).map((r) => [r.kind, r.purpose || '', r.blob ? r.blob.size : 0, r.taken_at || '']);
const sent = [];
fetch = async (u, init) => { sent.push([u, fields(init.body)]); return J({ id: 90 }); };
await qSync(true);
return { failed, ok, recs, sent: sent.map(([u, f]) => [u, f.purpose, f.scan, f.taken_at, f.file]), left: QQ.items.length };
""")
    f = out["failed"]
    assert f["status"] == "failed" and f["code"] == "photo_before_dispatch" and "早于派单" in f["err"] and f["canSafety"] is True and f["bad"] == 1
    assert out["ok"] is True
    assert out["recs"] == [["media", "safety", 12, "2026-10-01T09:00"]]          # 整改提交去掉，同一张照片改作随手拍入队，照片不丢
    assert out["sent"] == [["/api/quick/sync/media", "safety", "1", "2026-10-01T09:00", "blob:12"]]
    assert out["left"] == 0


def test_timed_out_note_is_resent_with_same_client_id():
    out = run_app("""
API_T.text = 60;
const first = [];
fetch = (u, init) => { first.push([u, JSON.parse(init.body)]); return hang(u, init); };
const r = await POST('/api/notes', { text: '3号墩承台浇筑完成', source: 'text' });
const rec = (await allQ())[0];
const again = [];
fetch = async (u, init) => { again.push([u, JSON.parse(init.body)]); return J({ id: 501, time: '09:00', text: '3号墩承台浇筑完成', client_id: JSON.parse(init.body).client_id }); };
await qSync(true);
return { r, first: first.map(([u, b]) => [u, b.client_id, b.client_time]), again: again.map(([u, b]) => [u, b.client_id, b.client_time]), rec: [rec.kind, rec.cid], left: QQ.items.length };
""")
    assert out["r"]["queued"] is True
    (u1, cid1, t1), = out["first"]
    (u2, cid2, t2), = out["again"]
    assert u1 == u2 == "/api/quick/sync/note"
    assert cid1 == cid2 == out["r"]["client_id"] == out["rec"][1] and t1 == t2      # 超时转暂存后重发：同一 client_id、同一记录时间
    assert out["left"] == 0


def test_hung_sync_request_times_out_and_does_not_block_queue():
    out = run_app("""
fetch = async () => { throw new TypeError('offline'); };
await POST('/api/notes', { text: 'a' });
API_T.text = 60;
fetch = hang;
const t0 = Date.now(); await qSync(true); const ms = Date.now() - t0;
const rec = (await allQ())[0];
return { ms, tries: rec.tries, err: rec.err, status: rec.status, sync: QQ.sync };
""")
    assert out["ms"] < 1500
    assert out["tries"] == 1 and out["status"] == "pending" and out["err"] and out["sync"] == "net"


def test_server_voice_pending_is_merged_into_note_list():
    out = run_app("""
let calls = 0;
fetch = async (u) => (calls++, String(u).startsWith('/api/quick/voice-pending'))
  ? J([{ pending_id: 5, status: 'running', recorded_at: '2026-10-07T08:30:00+01:00', seconds: 7, last_error: '' },
       { pending_id: 6, status: 'failed', recorded_at: '2026-10-07T08:40:00+01:00', seconds: 4, last_error: 'ASR 超时' }])
  : J({}, 404);
await QUICK.loadVoice(true);
const first = calls;
await QUICK.loadVoice();                     // 紧接着再看一次：这个接口有副作用，4 秒内不重复调用
return { list: QUICK.pendingNotes().map((n) => [n.id, n.voice, n.asr, n.seconds, n.err]), first, again: calls - first };
""")
    assert out["list"] == [["vp:5", True, "running", 7, ""], ["vp:6", True, "failed", 4, "ASR 超时"]]
    assert out["first"] == 1 and out["again"] == 0


def test_voice_pending_endpoint_missing_is_silent():
    out = run_app("""
fetch = async () => J({ detail: 'Not Found' }, 404);
await QUICK.loadVoice(true);
return QUICK.pendingNotes().length;
""")
    assert out == 0


# ============================================================================ tech.js：复核显示（N-2）、规则提示（U-06）、迟到记录（D5）

def test_verify_view_follows_contract_and_never_claims_resolved_before_closure():
    out = run_app("""
const now = Date.parse('2026-10-07T12:00:00');
const V = (verify, status = 'rectifying', extra = {}) => verifyView({ status, verify, ...extra }, now);
return {
  sugRes: V({ state: 'done', suggest: 'resolved', resolved: true, reason: '已加装防护栏' }),
  sugNot: V({ state: 'done', suggest: 'not_resolved', resolved: false, remaining: '临边无防护' }).text,
  pending: V({ state: 'pending', suggest: null }),
  running: V({ state: 'running', suggest: null, started_at: '2026-10-07T11:55:00' }),
  stale: V({ state: 'running', suggest: null, started_at: '2026-10-07T11:45:00' }),
  failed: V({ state: 'failed', suggest: null, reason: '模型超时' }),
  legacyTrue: V({ resolved: true, reason: '（离线模拟）' }).text,
  legacyFalse: V({ resolved: false, remaining: '缺挡脚板' }).text,
  legacyNull: V({ resolved: null, reason: 'AI复核失败' }).text,
  doneNoSuggest: V({ state: 'done', suggest: null }).text,
  closed: V({ state: 'done', suggest: 'resolved' }, 'resolved', { closed_by_name: '王涛' }),
  closedNoName: V({ resolved: true }, 'resolved').closed,
  openAgain: V({ state: 'failed', suggest: null }, 'open').reverify,
  marks: ['unknown', 'early', 'clock_skew', 'ok', undefined].map((c) => V({ state: 'done', suggest: 'resolved', photo_check: c }).marks),
  skew: V({ state: 'done', suggest: 'resolved', photo_check: 'clock_skew', photo_taken_at: '2026-10-08T09:30' }).marks,
  none: verifyView({ status: 'open', verify: null }, now),
};
""")
    assert out["sugRes"]["text"] == "AI 建议：已消除，待安全主管确认" and out["sugRes"]["reason"] == "已加装防护栏"
    assert out["sugRes"]["reverify"] is False
    assert out["sugNot"] == "AI 建议：仍存在临边无防护"
    assert out["pending"]["text"] == "AI 复核中" and out["pending"]["busy"] is True
    assert out["running"]["text"] == "AI 复核中" and out["running"]["reverify"] is False
    assert out["stale"]["text"] == "AI 复核未完成，可人工复核" and out["stale"]["reverify"] is True and out["stale"]["busy"] is False
    assert out["failed"]["text"] == "AI 复核失败，待人工复核" and out["failed"]["reverify"] is True
    assert out["legacyTrue"] == "AI 建议：已消除，待安全主管确认"            # 旧记录的 resolved 也只当建议
    assert out["legacyFalse"] == "AI 建议：仍存在缺挡脚板"
    assert out["legacyNull"] == "AI 复核失败，待人工复核"
    assert out["doneNoSuggest"] == "AI 复核失败，待人工复核"                   # 与 site_agents.verify_label 一致
    # 人工闭环之后：AI 建议去掉“待安全主管确认”，另起一行写已闭环；不再提供重新复核
    assert out["closed"]["text"] == "AI 建议：已消除" and out["closed"]["closed"] == "已闭环（王涛确认）" and out["closed"]["reverify"] is False
    assert out["closedNoName"] == "已闭环"
    assert out["openAgain"] is False                                            # 只有待复核的才能重新让 AI 复核（服务端同样 409）
    assert out["marks"] == [["拍摄时间未知"], ["拍摄时间早于派单，请核实"], ["手机时间异常"], [], []]
    assert out["skew"] == ["手机时间异常（手机记的是 10-08 09:30）"]
    assert out["none"] is None


def test_tech_js_has_no_positive_claim_before_closure():
    src = read("tech.js")
    assert "已确认消除" not in src and "未消除" not in src
    # “已消除”只出现在 AI 建议里；不带“待安全主管确认”的那一处只用于人工闭环之后
    assert src.count("已消除") == src.count("AI 建议：已消除") == 2
    assert 'closed ? "AI 建议：已消除" : "AI 建议：已消除，待安全主管确认"' in src


def test_rule_hints_are_located_and_merged():
    out = run_app("""
const names = { works: '施工内容', progress: '工程量', issues: '问题' };
const cs = [...Array(7)].map((_, i) => ({ level: 'warn', message: '没有标注依据（照片/口述/计划），请确认是否属实', section: 'works', index: i }));
cs.push({ level: 'error', message: '数量为负', section: 'progress', index: 0 });
cs.push({ level: 'warn', message: '没有标注依据（照片/口述/计划），请确认是否属实', section: 'issues', index: 1 });
cs.push({ level: 'warn', message: '缺少天气', section: null, index: null });
return groupChecks(cs, names).map((g) => [g.level, g.n, g.where, g.text, g.refs.length]);
""")
    assert out == [
        ["warn", 8, "施工内容 第1～7条；问题 第2条", "8 条：没有标注依据（照片/口述/计划），请确认是否属实", 8],
        ["error", 1, "工程量 第1条", "数量为负", 1],
        ["warn", 1, "", "缺少天气", 1],
    ]


def test_late_records_split_and_labels():
    out = run_app("""
const L = [
  { note_id: 1, note_date: '2026-10-06', received_date: '2026-10-07', late_days: 1, week_confirmed: false, text: 'a' },
  { note_id: 2, note_date: '2026-10-02', received_date: '2026-10-07', late_days: 5, week_confirmed: true, text: 'b' },
  { note_id: 3, note_date: '2026-10-01', received_date: '2026-10-01', late_days: 0, week_confirmed: true, text: 'c' },
  { note_id: 4, note_date: '2026-10-03', received_date: '2026-10-07', late_days: 4, week_confirmed: false, text: 'd' },
  { note_id: 5, note_date: '2026-10-07', received_date: '2026-10-07', late_days: 0, week_confirmed: false, text: 'e' },
];
const s = lateSplit(L, '2026-10-05', '2026-10-11');
return { inWeek: s.inWeek.map((n) => n.note_id), carry: s.carry.map((n) => n.note_id), t: L.map(lateText), bad: lateSplit(null, 'a', 'b'),
         up: [lateUpload({ taken_at: '2026-10-07T09:00', uploaded_at: '2026-10-07T09:20:00' }), lateUpload({ taken_at: '2026-10-07T09:00', uploaded_at: '2026-10-07T09:05:00' }),
              lateUpload({ taken_at: null, uploaded_at: '2026-10-07T09:05:00' }), lateUpload(null)] };
""")
    assert out["inWeek"] == [1] and out["carry"] == [2, 3]
    assert out["t"] == ["迟到补传 1 天", "迟到补传 5 天", "确认后补记", "迟到补传 4 天", "确认后补记"]
    assert out["bad"] == {"inWeek": [], "carry": []}
    assert out["up"] == [True, False, False, False]


# ============================================================================ 静态检查：各页面接上了队列与提示

def test_pages_are_wired_to_queue_and_offline_states():
    core, quick, tech = read("core.js"), read("x_quick.js"), read("tech.js")
    assert "AbortController" in core and "function serverNow" in core and "信号不好，已保留你填的内容" in core
    assert 'purpose === "rectify") return null' not in quick                       # 整改照片也能暂存
    for s in ('"/api/quick/sync/voice"', '"/api/quick/sync/rectify"', "/api/quick/voice-pending", "photo_before_dispatch", "改作随手拍"):
        assert s in quick, s
    # 首页：说一句走 note 模式，录完即“已记下，待上传”；转写中 / 失败·重试 / 改打字
    home = tech[tech.index("const TechHome"):tech.index("// 日志采集")]
    assert 'mode="note"' in home and '@audio="saveAudio"' in home
    for s in ("已记下，待上传", "转写中", "转写失败", "重试", "改打字", "没有网络，显示本机暂存内容"):
        assert s in home, s
    assert re.search(r"async del\(n\) \{[^}]*confirm\(", home), "删除随手记要先确认（U-08）"
    # 周确认：没有记录时不能点 AI 汇总（U-07）；迟到记录区（D5）
    log = tech[tech.index("const TechLog"):tech.index("// 随手拍隐患")]
    assert "weekEmpty" in log and "本周还没有记录，先去" in log and "迟到记录" in log and "/api/quick/late-notes" in log
    # 整改单：离线不白屏、暂存分支、补传标记、重新复核
    fd = tech[tech.index("const FindingDetail"):]
    for s in ("没有网络，显示本机暂存内容", "r && r.queued", "已暂存，联网后自动提交复核", "补传", "/reverify", "photo_before_dispatch", "整改照片上传中"):
        assert s in fd, s
    assert "正在对比整改前后照片" not in fd


def test_frontend_syntax():
    if not NODE:
        pytest.skip("没有 node")
    for p in (STATIC / "core.js", STATIC / "tech.js", STATIC / "x_quick.js", TOOL):
        r = subprocess.run([NODE, "--check", str(p)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert r.returncode == 0, f"{p.name}: {r.stderr}"


# ============================================================================ tools/offline_check.mjs：守卫与用例清单

def test_offline_check_refuses_business_ports_and_requires_base():
    if not NODE:
        pytest.skip("没有 node")
    for base in ("http://127.0.0.1:8600", "http://localhost:8601/", "https://192.168.1.171:8601/#/home"):
        r = subprocess.run([NODE, str(TOOL), "--base", base], capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert r.returncode == 2, (base, r.stdout, r.stderr)
        assert "拒绝" in r.stderr, r.stderr
    r = subprocess.run([NODE, str(TOOL)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 2 and "--base" in r.stderr
    src = TOOL.read_text(encoding="utf-8")
    assert "9344" in src and "--cdp-port" in src and "--use-fake-device-for-media-stream" in src and "Fetch.requestPaused" in src
    for c in ("C1", "C2", "C3", "C4", "C5", "C6", "C7"):
        assert f'"{c}"' in src, c


# ============================================================================ T-10c 离线启动（P0-1 AC-5）：上次的 /api/me 缓存、离线模式、SW 注册

ME_FULL = """
const ME = { user: { id: 7, username: 'zhang', name: '张工', role: 'tech', role_name: '技术员', org: 'sub', company: '一分包', wbs_scope: '3号墩',
                     email: 'z@x.cn', project_id: 1, active: 1 },
  can: ['note'], home: 'home',
  project: { id: 1, name: '仿真项目', code: 'SIM', config_json: '{"simulation":true,"k":"v"}', config: { simulation: true, social_enabled: true, k: 'v' }, features: { quick: true } },
  projects: [{ id: 1, name: '仿真项目', code: 'SIM' }], ai: { provider: 'mock', ready: true, asr: true, label: '离线模拟模型', vision_model: 'mock' },
  unread: 3, version: '9', today: '2026-10-06', p6: { file: 'a.xer', data_date: '2026-10-01', activities: 9 } };
const localDay = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
store.me = null;
"""


def test_me_cache_keeps_only_identity_and_nav_fields_and_offline_boot_uses_it():
    out = run_app(ME_FULL + """
fetch = async () => J(ME);
await loadMe();
const online = { offline: store.offline, unread: store.me.unread };
const cached = JSON.parse(localStorage.getItem(ME_CACHE_KEY));
fetch = async () => { throw new TypeError('Failed to fetch'); };
store.me = null;
await loadMe();
return { online, cached, me: store.me, offline: store.offline, day: localDay(serverNow()) };
""")
    assert out["online"] == {"offline": False, "unread": 3}
    c = out["cached"]
    assert set(c) <= {"user", "can", "home", "project", "ai", "version", "at"}, c
    assert c["user"] == {"id": 7, "name": "张工", "role": "tech", "role_name": "技术员", "org": "sub", "company": "一分包", "wbs_scope": "3号墩"}
    assert c["project"] == {"id": 1, "name": "仿真项目", "config": {"simulation": True, "social_enabled": True}, "features": {"quick": True}}
    assert c["ai"] == {"provider": "mock", "ready": True, "asr": True} and c["can"] == ["note"] and c["home"] == "home"
    raw = json.dumps(c, ensure_ascii=False)
    for s in ("username", "zhang", "email", "z@x.cn", "config_json", "token", "unread", '"k"'):
        assert s not in raw, s
    me = out["me"]
    assert out["offline"] is True and me["offline"] is True
    assert me["user"]["id"] == 7 and me["home"] == "home" and me["unread"] == 0 and me["projects"] == []
    assert me["today"] == out["day"]                       # 离线时“今天”按服务器校正时钟算，不用缓存那天


def test_offline_boot_without_cache_or_with_expired_login_goes_to_login():
    out = run_app(ME_FULL + """
const res = {};
fetch = async () => { throw new TypeError('Failed to fetch'); };
try { await loadMe(); res.nocache = 'entered'; } catch (e) { res.nocache = [e.offline, store.me, store.offline]; }
lsSet(ME_CACHE_KEY, { user: { id: 7, name: '张工', role: 'tech' }, home: 'home', can: [] });
fetch = async () => J({ detail: '请先登录' }, 401);
try { await loadMe(); res.expired = 'entered'; } catch (e) { res.expired = [e.status, localStorage.getItem(ME_CACHE_KEY)]; }
fetch = async () => J({ detail: 'boom' }, 500);
lsSet(ME_CACHE_KEY, { user: { id: 7, name: '张工', role: 'tech' }, home: 'home', can: [] });
try { await loadMe(); res.err500 = 'entered'; } catch (e) { res.err500 = [e.status, store.offline]; }
return res;
""")
    assert out["nocache"] == [True, None, False]          # 没登录过（本机没有缓存）：离线进不去，回登录页
    assert out["expired"] == [401, None]                   # 登录已过期：清掉缓存
    assert out["err500"] == [500, False]                   # 服务器报错不是离线，不进离线模式


def test_logout_clears_me_cache_online_and_offline():
    out = run_app(ME_FULL + """
const res = {};
fetch = async () => J(ME); await loadMe();
fetch = async () => J({ ok: true });
await api('POST', '/api/logout', {});
res.online = localStorage.getItem(ME_CACHE_KEY);
fetch = async () => J(ME); await loadMe();
fetch = async () => { throw new TypeError('Failed to fetch'); };
try { await api('POST', '/api/logout', {}); res.offline = 'ok'; } catch (e) { res.offline = [e.message, localStorage.getItem(ME_CACHE_KEY)]; }
fetch = async () => J(ME); await loadMe();
fetch = async () => J({ detail: '请先登录' }, 401);
try { await api('GET', '/api/tech/home'); } catch (e) { res.r401 = [e.message, localStorage.getItem(ME_CACHE_KEY), store.offline]; }
return res;
""")
    assert out["online"] is None
    assert out["offline"][1] is None and "联网" in out["offline"][0]   # 离线点退出：本机缓存照样清掉，并说明要联网才能退出
    assert out["r401"] == ["请先登录", None, False]


BIZ_SEED = """
const BIZ = ['zb_home_cache_7', 'zb_fcache_7', 'zb_tasks_7', 'zb_wm_place_7', 'zb_wm_work_7', 'zb_home_cache_8'];
const KEEP = ['zb_clock_off', 'zb_line_site', 'zb_scan_pref_7', 'zb_dev_user', 'xg_days'];
const seed = async () => {
  fetch = async () => J(ME); await loadMe();
  for (const k of [...BIZ, ...KEEP]) localStorage.setItem(k, JSON.stringify({ k }));
  await qPut({ cid: 'n-keep', kind: 'note', uid: 7, seq: 1, created: '2026-10-07T08:00:00', status: 'pending', tries: 0, next: 0, err: '', body: { text: '没上传的', media_ids: [] } });
};
const left = async () => ({ biz: BIZ.filter((k) => localStorage.getItem(k) !== null), me: localStorage.getItem(ME_CACHE_KEY),
  keep: KEEP.filter((k) => localStorage.getItem(k) !== null).length, queue: (await allQ()).map((r) => r.cid) });
"""


def test_logout_and_401_clear_business_caches_but_keep_unsent_queue():
    out = run_app(ME_FULL + BIZ_SEED + """
const res = {};
await seed(); fetch = async () => J({ ok: true }); await api('POST', '/api/logout', {}); res.online = await left();
await seed(); fetch = async () => { throw new TypeError('Failed to fetch'); };
try { await api('POST', '/api/logout', {}); } catch (_) { /* 离线退出：提示要联网 */ } res.offline = await left();
await seed(); fetch = async () => J({ detail: '请先登录' }, 401);
try { await api('GET', '/api/todo'); } catch (_) { /* 转登录 */ } res.r401 = await left();
store.me = { user: { id: 7 } }; await seed(); fetch = async () => J({ detail: '请先登录' }, 401);
try { await loadMe(); } catch (_) { /* 登录过期 */ } res.me401 = await left();
return res;
""")
    for k in ("online", "offline", "r401", "me401"):
        r = out[k]
        assert r["biz"] == [] and r["me"] is None, (k, r)          # 首页、整改单、列表、水印填写、上次身份都清掉（各用户的都清）
        assert r["keep"] == 5, (k, r)                               # 时钟偏差、导航记忆、偏好等设置不动
        assert r["queue"] == ["n-keep"], (k, r)                     # 没上传的暂存记录不删


def test_queue_is_isolated_per_user_and_resumes_for_original_user():
    out = run_app("""
const rec = (cid, uid, text) => ({ cid, kind: 'note', uid, seq: uid * 10, created: '2026-10-07T08:00:00', status: 'pending', tries: 0, next: 0, err: '', body: { text, media_ids: [] } });
await qPut(rec('n-a', 7, 'A 的记录')); await qPut(rec('n-b', 8, 'B 的记录'));
const sent = [];
fetch = async (u, init) => { if (String(u).includes('/api/quick/sync/note')) { const b = JSON.parse(init.body); sent.push(b.client_id); return J({ id: 1, late_days: 0 }); } return J([]); };
store.me = { user: { id: 8, role: 'tech', name: 'B' }, today: '2026-10-07', ai: { asr: true }, can: [] };
await qRefresh();
const seenB = QQ.items.map((x) => x.cid), notesB = QUICK.pendingNotes().map((n) => n.text);
await qSyncCore(true);
const afterB = { sent: [...sent], queue: (await allQ()).map((r) => r.cid) };
store.me = { user: { id: 7, role: 'tech', name: 'A' }, today: '2026-10-07', ai: { asr: true }, can: [] };
await qRefresh();
const seenA = QQ.items.map((x) => x.cid);
await qSyncCore(true);
return { seenB, notesB, afterB, seenA, sent, queue: (await allQ()).map((r) => r.cid) };
""")
    assert out["seenB"] == ["n-b"] and out["notesB"] == ["B 的记录"]        # 换人登录：看不到上一个人的暂存
    assert out["afterB"] == {"sent": ["n-b"], "queue": ["n-a"]}            # 也不替上一个人补传
    assert out["seenA"] == ["n-a"] and out["sent"] == ["n-b", "n-a"] and out["queue"] == []   # 原用户再登录时补传


def test_offline_mode_recovers_when_server_is_back():
    out = run_app(ME_FULL + """
OFFLINE_T.get = 80;
const res = { online: 0 };
window.addEventListener('online', () => res.online++);
fetch = async () => J(ME); await loadMe();
fetch = async () => { throw new TypeError('Failed to fetch'); };
store.me = null; await loadMe();
res.mode = store.offline;
fetch = hang;                                           // 连着 Wi-Fi 但服务器不可达：离线模式下 GET 不干等
const t0 = Date.now();
try { await api('GET', '/api/tech/home'); } catch (e) { res.get = [e.offline, Date.now() - t0 < 1500]; }
fetch = async () => J(ME);
await meRecheck();
res.after = [store.offline, store.me.unread, store.me.offline || false];
return res;
""")
    assert out["mode"] is True
    assert out["get"] == [True, True]
    assert out["after"] == [False, 3, False]
    assert out["online"] == 1                               # 服务器重新连上：通知暂存队列补传、首页重新拉取


def test_offline_banner_is_on_every_page_head_and_safety_snap_fallback():
    out = run_app("""
const names = (n) => EXT.slotList(n).map((c) => c.name || '');
return { home: names('top:home'), work: names('top:work'), shome: names('top:shome'), login: names('login') };
""")
    assert out["home"][0] == "OfflineBanner" and out["work"][0] == "OfflineBanner"
    assert out["shome"][:2] == ["OfflineBanner", "OfflineSnap"]
    assert "OfflineBanner" not in out["login"]
    core, tech = read("core.js"), read("tech.js")
    banner = core[core.index("const OfflineBanner"):]
    banner = banner[:banner.index("};")]
    assert "离线模式：</b>记录会先存在本机，联网后自动上传" in banner and 'v-if="store.offline"' in banner
    snap = tech[tech.index("const OfflineSnap"):]
    assert 'v-if="store.offline"' in snap and "safety-snap" in snap


def test_tech_home_offline_without_home_cache_still_shows_quick_note():
    out = run_app(ME_FULL + """
fetch = async () => J(ME); await loadMe();
fetch = async () => { throw new TypeError('Failed to fetch'); };
store.me = null; await loadMe();
const vm = { d: null, err: '', offline: false, loadSug() {} };
await TechHome.methods.load.call(vm);
return { d: vm.d, err: vm.err, offline: vm.offline, today: today() };
""")
    assert out["err"] == "" and out["offline"] is True
    assert out["d"]["notes"] == [] and out["d"]["today"] == out["today"] and out["d"]["week"] is None
    home = read("tech.js")
    home = home[home.index("const TechHome"):home.index("// 日志采集")]
    assert 'class="zb-card zb-m-rec" v-if="d.week"' in home        # 没有首页缓存时只显示随手记，不显示依赖接口的日志/周确认行
    assert 'v-if="offline && !store.offline"' in home               # 离线模式的页头已有提示，首页不再重复一条


def test_service_worker_registration_and_manifest():
    core, html = read("core.js"), read("index.html")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    i = core.index("serviceWorker")
    seg = core[i - 200:i + 400]
    assert "window.isSecureContext" in seg and 'register("/sw.js", { scope: "/" })' in seg


def test_offline_check_has_sw_mode_cases():
    src = TOOL.read_text(encoding="utf-8")
    for s in ('"--sw"', '"S1"', '"S2"', '"S3"', '"S4"', '"S5"', '"S6"', "fromServiceWorker", "ignore-certificate-errors-spki-list",
              "离线模式：记录会先存在本机，联网后自动上传", "zb_me_offline", "cache-storage", "--shots"):
        assert s in src, s
