// 离线暂存补传的端到端检查（P0-1，规划审查 G-01）：本机 Edge 无头 + CDP，以技术员身份在一次性实例上跑 7 个用例，结果输出 JSON，有失败时退出码 1。
// 用法：node tools/offline_check.mjs --base http://127.0.0.1:8619 [--user sim_zhao] [--cdp-port 9344] [--only C1,C3] [--stub] [--out 结果.json] [--edge Edge路径]
//   --base 必填，只能指向一次性实例（mock AI、临时数据目录，做法见升级可行性评估 4.1）；指向 8600/8601（连业务库的服务）直接拒绝，退出码 2。
//   --cdp-port 默认 9344（role_shots 用 9333），用户数据目录用临时目录，跑完删除。需要开发态快捷登录（ZB_DEV_LOGIN=1）。
//   --stub：服务端（T-08 录音补传、迟到清单、整改补传）还没合入时用：在页面里给缺的接口打桩——录音补传转成一条随手记（时间取录音时间），
//           迟到清单给一条 5 天前的记录，整改补传转调 /api/findings/{id}/rectify。只验证前端队列；合入后不加 --stub，直接跑真实接口。
// 用例（AC 见评估 4.2、4.3 的 T-09a）：
//   C1 离线说一句：2 秒内出现“已记下，待上传”，IndexedDB 里多 1 条录音
//   C2 联网补传后，录音转成的随手记时间等于录音时间（录音时间先往前拨最多 2 小时，证明不是上传时间）
//   C3 离线提交整改，联网后整改单状态为 rectifying（待复核）
//   C4 用 Fetch.requestPaused 在请求阶段挂起（服务器收不到）：超时后转入暂存，界面不一直转圈；之后补传成功
//   C5 在响应阶段挂起（服务器已处理）：超时转暂存后按原 client_id 重发，同时放行挂起的那次，服务器上只有 1 条
//   C6 迟到 5 天的记录在那一周的周确认页带“迟到补传 5 天”标签
//   C7 页面已加载后断网，在应用内打开整改单（打开过的、没打开过的）和首页都不白屏，并提示“没有网络，显示本机暂存内容”
// 只在一次性实例上写数据（C3 没有可整改的隐患时会造一条并派给该技术员；C6 打开周确认页会建本周草稿）。
//
// --sw：离线启动（T-10c，P0-1 AC-5 的自动化部分），只跑下面 S1～S7，不跑 C1～C7。要求安全上下文：http://localhost 或 https（serve --https）。
//   https 时脚本取实例证书的公钥指纹，用 --ignore-certificate-errors-spki-list 让 Edge 信任它（不改系统证书）。
//   断网 = 页面级 Network.emulateNetworkConditions（navigator.onLine、offline/online 事件）+ 浏览器级 Fetch 拦截本实例的全部请求并按断网失败
//         （页面级仿真管不到 Service Worker 自己发的请求；这样 SW 取网络必失败，只能用缓存）。
//   S1 在线打开：SW 接管页面；缓存里有首页外壳、没有 /api；本机身份缓存（zb_me_offline）不含账号名、邮箱、令牌
//   S2 断网冷启动首页：页面来自 SW 缓存（不是浏览器断网页），进入离线模式，页头提示，随手记（说一句、拍一张、打字）都在
//   S3 离线打字记一条、说一句、拍一张：都进本机暂存，随手记列表和暂存条里显示待上传
//   S4 恢复网络：自动补传，服务器上各有 1 条（录音可以还在转写），退出离线模式、页头提示消失
//   S5 退出登录后本机身份缓存清掉；再断网冷启动只到登录页
//   S6 管理岗（项目经理）登录过后断网冷启动：不白屏，有页头提示
//   S7 分包安全员登录过后断网冷启动：首页取不到数据，但随手拍照常可用，拍一张进本机暂存
//   --shots 目录：S2、S3、S6、S7 各存一张手机尺寸截图（离线界面核对用）
import { spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import tls from "node:tls";

const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 && argv[i + 1] !== undefined && !argv[i + 1].startsWith("--") ? argv[i + 1] : d; };
const die = (msg) => { process.stderr.write(msg + "\n"); process.exit(2); };

const RAW = opt("--base", "");
if (!RAW) die("缺少 --base：只能指向一次性实例，例如 --base http://127.0.0.1:8619");
let BASE_URL;
try { BASE_URL = new URL(RAW); } catch (_) { die(`--base 不是有效地址：${RAW}`); }
const PORT = BASE_URL.port || (BASE_URL.protocol === "https:" ? "443" : "80");
if (["8600", "8601"].includes(PORT) || /:860[01](?!\d)/.test(RAW)) die(`拒绝：${RAW} 是连业务库的服务（8600/8601），离线检查只在一次性实例上跑`);
const BASE = BASE_URL.origin;
const CDP = +opt("--cdp-port", "9344");
const USER = opt("--user", "sim_zhao");
const STUB = argv.includes("--stub");
const ONLY = opt("--only", "") ? opt("--only", "").split(",").map((s) => s.trim().toUpperCase()) : null;
const OUT = opt("--out", "");
const SW = argv.includes("--sw");
const SHOTS = opt("--shots", "");
const EDGE = opt("--edge", process.env.EDGE_PATH || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const OFFLINE_TIP = "没有网络，显示本机暂存内容";
const OFFLINE_MODE_TIP = "离线模式：记录会先存在本机，联网后自动上传";
if (SW && BASE_URL.protocol !== "https:" && !/^(localhost|127\.0\.0\.1|\[::1\])$/.test(BASE_URL.hostname)) die("--sw 需要安全上下文：用 https（serve --https）或 http://localhost");

// https 一次性实例的证书由本机根证书签发，node 与 Edge 都不认：node 只对 --base 这一个地址关掉校验；Edge 按公钥指纹单独信任这张证书
let SPKI = "";
if (BASE_URL.protocol === "https:") {
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
  const host = BASE_URL.hostname.replace(/^\[|\]$/g, "");
  SPKI = await new Promise((res) => {
    const s = tls.connect({ host, port: +PORT, servername: net.isIP(host) ? undefined : host, rejectUnauthorized: false }, () => {
      const c = s.getPeerCertificate(); s.end();
      try { res(crypto.createHash("sha256").update(new crypto.X509Certificate(c.raw).publicKey.export({ type: "spki", format: "der" })).digest("base64")); } catch (_) { res(""); }
    });
    s.on("error", () => res(""));
  });
}

// ---------------------------------------------------------------------------- 页面里的接口桩（--stub）
const STUB_SRC = `(() => {
  const real = window.fetch.bind(window);
  const J = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json" } });
  const jpost = (u, b, signal) => real(u, { method: "POST", headers: { "x-zb": "1", "content-type": "application/json" }, credentials: "same-origin", body: JSON.stringify(b), signal });
  const day = (n) => { const d = new Date(Date.now() - n * 86400000); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); };
  window.__zbStub = { voice: 0, rectify: 0, media: 0 };
  window.fetch = async (input, init = {}) => {
    const u = String(typeof input === "string" ? input : input.url).replace(location.origin, ""), m = String(init.method || "GET").toUpperCase();
    if (u.startsWith("/api/quick/voice-pending")) return J([]);
    if (u.startsWith("/api/quick/late-notes")) return J([{ note_id: -1, note_date: day(5), received_date: day(0), late_days: 5, week_confirmed: false, text: "（桩）5 天前的随手记" }]);
    if (m === "POST" && u === "/api/quick/sync/voice") {  // 录音补传 → 直接记成一条随手记，时间取录音时间
      const fd = init.body, cid = fd.get("client_id"); window.__zbStub.voice++;
      const r = await jpost("/api/quick/sync/note", { client_id: "s" + cid, text: "（桩）录音转写 " + cid.slice(-6), source: "voice", seconds: +fd.get("seconds") || null, client_time: fd.get("recorded_at") }, init.signal);
      if (!r.ok) return r;
      const n = await r.json();
      return J({ pending_id: n.id, status: "pending", recorded_at: fd.get("recorded_at"), late_days: 0, client_id: cid });
    }
    if (m === "POST" && u === "/api/quick/sync/rectify") {  // 整改补传 → 现有的提交整改接口
      const b = JSON.parse(init.body); window.__zbStub.rectify++;
      return jpost("/api/findings/" + b.finding_id + "/rectify", { media_id: b.media_id }, init.signal);
    }
    if (m === "POST" && u === "/api/quick/sync/media" && init.body instanceof FormData && init.body.get("purpose") === "rectify") {  // 现有补传接口不收整改照片：按日志照片传、不识别，提交整改时再记为整改照片
      const fd = new FormData(); for (const [k, v] of init.body.entries()) fd.append(k, k === "purpose" ? "log" : k === "scan" ? "0" : v);
      window.__zbStub.media++;
      return real(input, { ...init, body: fd });
    }
    return real(input, init);
  };
})();`;

// ---------------------------------------------------------------------------- 浏览器与 CDP
if (!fs.existsSync(EDGE)) die(`找不到 Edge：${EDGE}（可用 --edge 指定）`);
let accounts;
try { accounts = await (await fetch(BASE + "/api/dev/accounts")).json(); } catch (e) { die(`连不上 ${BASE}：${e.message}`); }
if (!Array.isArray(accounts) || !accounts.length) die("取不到开发态账号：一次性实例要设 ZB_DEV_LOGIN=1");
const tech = accounts.find((a) => a.username === USER) || accounts.find((a) => a.role === "tech");
const sup = accounts.find((a) => a.username === "sim_safety") || accounts.find((a) => a.role === "safety_sup");
const mgr = accounts.find((a) => a.role === "manager") || accounts.find((a) => a.role === "leader");
const subSafety = accounts.find((a) => a.role === "sub_safety");
if (!tech) die(`找不到技术员账号 ${USER}`);
// CDP 端口已有浏览器在听（多半是上次没关掉的）：不接管它，免得用上别人的页面和本机暂存
const cdpBusy = async (host) => { try { await fetch(`http://${host}:${CDP}/json/version`, { signal: AbortSignal.timeout(1500) }); return true; } catch (_) { return false; } };
if (await cdpBusy("127.0.0.1") || await cdpBusy("[::1]")) die(`CDP 端口 ${CDP} 已被占用（可能是上次没关掉的 Edge），先关掉或用 --cdp-port 换一个`);
const udd = fs.mkdtempSync(path.join(os.tmpdir(), "zb_offline_check_"));

const edge = spawn(EDGE, ["--headless=new", `--remote-debugging-port=${CDP}`, `--user-data-dir=${udd}`, "--no-first-run", "--no-default-browser-check", "--disable-gpu",
  "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", ...(SPKI ? [`--ignore-certificate-errors-spki-list=${SPKI}`] : []), "about:blank"], { stdio: "ignore" });
edge.on("error", () => { /* 启动失败由下面等不到页面来报 */ });
// 浏览器级连接：取真正的浏览器进程号（Windows 上 spawn 出来的 msedge.exe 只是启动器，会马上退出），收尾时用它关浏览器
let bws = null, browserPid = 0, bseq = 100000, ws = null;
const blocked = [];
const bcmd = (method, params = {}, sessionId = "") => new Promise((res, rej) => {  // sessionId：发给浏览器下挂着的某个目标（如 Service Worker）
  if (!bws) return rej(new Error("没有浏览器连接"));
  const id = ++bseq, h = (e) => { const m = JSON.parse(e.data); if (m.id !== id) return; bws.removeEventListener("message", h); if (m.error) rej(new Error(m.error.message)); else res(m.result); };
  bws.addEventListener("message", h); bws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
});
const cdpUp = async () => { try { await fetch(`http://127.0.0.1:${CDP}/json/version`, { signal: AbortSignal.timeout(800) }); return true; } catch (_) { return false; } };
// 收尾：先让浏览器自己关（Browser.close），等 CDP 端口不再响应；关不掉再结束浏览器进程树；最后删临时用户目录
async function finish(code) {
  try { if (ws) ws.close(); } catch (_) { /* 已断开 */ }
  if (bws) { try { await Promise.race([bcmd("Browser.close"), sleep(3000)]); } catch (_) { /* 下面兜底 */ } }
  for (let i = 0; i < 25 && await cdpUp(); i++) await sleep(300);
  if (await cdpUp()) {
    const pid = browserPid || edge.pid;
    if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" }); else { try { process.kill(pid, "SIGKILL"); } catch (_) { /* 已退出 */ } }
    await sleep(1000);
  }
  for (let i = 0; i < 10; i++) { try { fs.rmSync(udd, { recursive: true, force: true }); break; } catch (_) { await sleep(500); } }  // 进程刚退出时文件可能还占着
  process.exit(code);
}
const fail = async (msg) => { process.stderr.write(msg + "\n"); await finish(2); };
process.on("unhandledRejection", (e) => fail("检查脚本出错：" + ((e && e.message) || e)));  // 任何意外都先关掉浏览器再退出
process.on("SIGINT", () => finish(130));
let targets = [];
for (let i = 0; i < 50 && !targets.some((t) => t.type === "page"); i++) { try { targets = await (await fetch(`http://127.0.0.1:${CDP}/json/list`)).json(); } catch (_) { /* 还没起来 */ } await sleep(300); }
const page = targets.find((t) => t.type === "page");
if (!page) await fail(`Edge 没有在 CDP 端口 ${CDP} 上起来（可用 --cdp-port 换一个）`);
try {
  bws = new WebSocket((await (await fetch(`http://127.0.0.1:${CDP}/json/version`)).json()).webSocketDebuggerUrl);
  await new Promise((r, j) => { bws.addEventListener("open", r); bws.addEventListener("error", j); });
  browserPid = ((await bcmd("SystemInfo.getProcessInfo")).processInfo.find((x) => x.type === "browser") || {}).id || 0;
  // --sw 的断网：浏览器级拦截到的本实例请求（含 Service Worker 发出的）一律按断网失败，记下拦了哪些
  bws.addEventListener("message", (e) => {
    const m = JSON.parse(e.data);
    if (m.method !== "Fetch.requestPaused") return;
    blocked.push(m.params.request.url.replace(BASE, ""));
    bcmd("Fetch.failRequest", { requestId: m.params.requestId, errorReason: "InternetDisconnected" }).catch(() => {});
  });
} catch (_) { /* 收尾时退回按端口与启动进程号处理 */ }
ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.addEventListener("open", r); ws.addEventListener("error", j); });
let seq = 0;
const pend = new Map(), listeners = new Set(), pageErrors = [];
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); if (m.error) p.rej(new Error(m.error.message)); else p.res(m.result); return; }
  if (m.method === "Runtime.exceptionThrown") pageErrors.push((m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text || "").slice(0, 200));
  for (const h of listeners) h(m);
});
const cmd = (method, params = {}) => new Promise((res, rej) => { const id = ++seq; pend.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params })); });
async function ev(expr) {
  const r = await cmd("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error((r.exceptionDetails.exception?.description || r.exceptionDetails.text || "页面脚本出错").slice(0, 300));
  return r.result.value;
}
async function waitFor(expr, ms = 10000, step = 200) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { try { const v = await ev(expr); if (v) return v; } catch (_) { /* 页面切换中 */ } await sleep(step); }
  return null;
}
async function setOffline(off) {
  await cmd("Network.emulateNetworkConditions", { offline: off, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
  await ev(`window.dispatchEvent(new Event(${JSON.stringify(off ? "offline" : "online")})); 1`);
}
const go = async (hash) => { await ev(`location.hash = ${JSON.stringify(hash)}; 1`); await sleep(300); };
const mainText = () => ev(`(document.querySelector("main") || document.body).innerText`);
// 本机暂存（IndexedDB zb_quick.q）里的记录，只取判断要用的字段
const idb = () => ev(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => rs.map((r) => ({ cid: r.cid, kind: r.kind, status: r.status, created: r.created, text: r.body ? r.body.text : "", err: r.err || "", purpose: r.purpose || "" })))`);
const apiGet = (p) => ev(`fetch(${JSON.stringify(p)}, { headers: { "x-zb": "1" }, credentials: "same-origin" }).then((r) => r.json())`);
const serverNotes = async () => ((await apiGet("/api/tech/home")).notes || []);
const clickText = (sel, text) => ev(`(() => { const b = [...document.querySelectorAll(${JSON.stringify(sel)})].find((x) => x.textContent.includes(${JSON.stringify(text)})); if (!b) return false; b.click(); return true; })()`);
// 一张现拍的照片（画布生成，内容带随机数，避免按 sha256 去重成同一张）
const PHOTO_JS = `(async () => {
  const cv = document.createElement("canvas"); cv.width = 640; cv.height = 480; const g = cv.getContext("2d");
  g.fillStyle = "#7a8a99"; g.fillRect(0, 0, 640, 480); g.fillStyle = "#c0392b"; g.font = "36px sans-serif"; g.fillText("offline_check " + Date.now() + " " + Math.random(), 16, 240);
  return await new Promise((r) => cv.toBlob(r, "image/jpeg", 0.9));
})()`;

await cmd("Runtime.enable"); await cmd("Page.enable"); await cmd("Network.enable");
await cmd("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1.5, mobile: true });
try { await cmd("Browser.grantPermissions", { origin: BASE, permissions: ["geolocation"] }); } catch (_) { /* 拿不到权限也不影响：只是记录不带定位 */ }
try { await cmd("Emulation.setGeolocationOverride", { latitude: 6.45, longitude: 3.39, accuracy: 10 }); } catch (_) { /* 同上 */ }
if (STUB) await cmd("Page.addScriptToEvaluateOnNewDocument", { source: STUB_SRC });

async function openHome() {
  await cmd("Page.navigate", { url: BASE + "/?oc=" + Date.now() + "#/home" });  // 带查询串：整页重新加载（只改 # 不会重新登录身份）
  return waitFor(`typeof store !== "undefined" && store.me && store.me.user.role === "tech" && !!document.querySelector(".cap-card")`, 20000);
}
// 以技术员身份登录（开发态快捷登录），停在首页
await cmd("Page.navigate", { url: BASE + "/" });
await waitFor(`document.readyState === "complete"`, 15000);
await ev(`fetch("/api/dev/login", { method: "POST", headers: { "x-zb": "1", "content-type": "application/json" }, body: JSON.stringify({ user_id: ${tech.id} }) }).then((r) => r.status)`);
await ev(`localStorage.setItem("zb_dev_user", "${tech.id}"); sessionStorage.removeItem("zb_no_auto"); 1`);
if (!await openHome()) await fail("技术员首页没有打开（登录失败？）");

// ---------------------------------------------------------------------------- 夹具：一条派给该技术员、可以拍整改照片的隐患
let findingId = 0;
async function asUser(uid) {
  const r = await fetch(BASE + "/api/dev/login", { method: "POST", headers: { "x-zb": "1", "content-type": "application/json" }, body: JSON.stringify({ user_id: uid }) });
  const ck = (r.headers.getSetCookie ? r.headers.getSetCookie() : [r.headers.get("set-cookie")]).filter(Boolean).map((c) => c.split(";")[0]).join("; ");
  return async (method, p, body) => {
    const x = await fetch(BASE + p, { method, headers: { "x-zb": "1", "content-type": "application/json", cookie: ck }, body: body ? JSON.stringify(body) : undefined });
    return { status: x.status, data: await x.json().catch(() => null) };
  };
}
async function getFinding() {
  if (findingId) return findingId;
  const list = await apiGet("/api/findings?status=active&mine=1");
  const f = (Array.isArray(list) ? list : []).find((x) => x.agent === "safety" && ["open", "rejected"].includes(x.status) && !x.candidate);
  if (f) return (findingId = f.id);
  if (!sup) throw new Error("没有可整改的隐患，也找不到安全主管账号来派一条");
  // 技术员拍一张让 AI（mock）识别出候选隐患，再由安全主管确认并派给该技术员
  const made = await ev(`(async () => {
    const blob = await ${PHOTO_JS};
    const fd = new FormData(); fd.append("purpose", "safety"); fd.append("files", blob, "oc.jpg"); fd.append("taken_at", wmIso(serverNow()));
    const r = await fetch("/api/media", { method: "POST", body: fd, headers: { "x-zb": "1" } }); const ms = await r.json(); if (!r.ok) return { err: JSON.stringify(ms) };
    for (let k = 0; k < 40; k++) {
      await new Promise((x) => setTimeout(x, 1500));
      const st = await (await fetch("/api/quick/media-status?ids=" + ms[0].id, { headers: { "x-zb": "1" } })).json();
      if (!st.pending) return { ids: st.findings.map((x) => x.id) };
    }
    return { err: "识别超时" };
  })()`);
  if (made.err || !made.ids.length) throw new Error("造隐患失败：" + (made.err || "没有识别出候选"));
  const call = await asUser(sup.id);
  const c = await call("POST", `/api/findings/${made.ids[0]}/confirm`, { assignee_id: tech.id });
  if (c.status !== 200) throw new Error(`安全主管确认派单失败：${c.status} ${JSON.stringify(c.data)}`);
  return (findingId = made.ids[0]);
}

// ---------------------------------------------------------------------------- 用例
const state = {};
const CASES = [
  ["C1", "离线说一句，2 秒内显示“已记下，待上传”，本机暂存多 1 条", async () => {
    state.baseline = (await serverNotes()).map((n) => n.id);
    const before = (await idb()).filter((r) => r.kind === "voice").length;
    await setOffline(true);
    const r = await ev(`(async () => {
      const b = document.querySelector(".cap-hero .voice.big button.mic"); if (!b) return { err: "找不到“说一句”按钮" };
      b.click(); await new Promise((x) => setTimeout(x, 1800));
      if (!b.classList.contains("on")) return { err: "没有开始录音：" + (store.toast ? store.toast.msg : "麦克风不可用") };
      const t0 = performance.now(); b.click();
      while (performance.now() - t0 < 5000) { if (document.querySelector(".cap-card").innerText.includes("已记下，待上传")) return { ms: Math.round(performance.now() - t0) }; await new Promise((x) => setTimeout(x, 50)); }
      return { err: "5 秒内没有出现“已记下，待上传”" };
    })()`);
    const voices = (await idb()).filter((r) => r.kind === "voice");
    state.voice = voices.sort((a, b) => (a.created < b.created ? 1 : -1))[0];
    return { ok: !r.err && r.ms <= 2000 && voices.length === before + 1, detail: { ...r, before, after: voices.length } };
  }],
  ["C2", "联网补传后，随手记的时间等于录音时间", async () => {
    if (!state.voice) return { ok: false, detail: { err: "C1 没有留下录音" } };
    // 录音时间往前拨（最多 2 小时，不跨过零点），证明入账用的是录音时间而不是上传时间
    const s = await ev(`(async () => {
      const r = (await qrun("q", "readonly", (st) => st.getAll())).find((x) => x.cid === ${JSON.stringify(state.voice.cid)}); if (!r) return { err: "本机暂存里找不到这条录音" };
      const now = new Date(), mid = new Date(now); mid.setHours(0, 0, 0, 0);
      const shift = Math.max(0, Math.min(2 * 3600e3, now - mid - 10 * 60e3));
      r.created = new Date(Date.parse(r.created) - shift).toISOString(); await qrun("q", "readwrite", (st) => st.put(r)); await qRefresh();
      return { at: r.created, hhmm: new Date(r.created).toTimeString().slice(0, 5), shift_min: Math.round(shift / 60000) };
    })()`);
    if (s.err) return { ok: false, detail: s };
    await setOffline(false);
    const t0 = Date.now();
    const gone = await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => !rs.some((r) => r.cid === ${JSON.stringify(state.voice.cid)}))`, 40000, 500);
    let note = null;
    for (let k = 0; k < 60 && gone && !note; k++) {
      note = (await serverNotes()).find((n) => !state.baseline.includes(n.id) && n.source === "voice" && n.time === s.hhmm) || null;
      if (!note) await sleep(1000);
    }
    const vp = STUB ? [] : await apiGet("/api/quick/voice-pending").catch(() => null);
    return { ok: !!gone && !!note, detail: { ...s, uploaded_in_ms: gone ? Date.now() - t0 : null, note: note && { id: note.id, time: note.time, text: note.text }, voice_pending: vp,
      queue_left: gone ? 0 : (await idb()).filter((r) => r.kind === "voice") } };
  }],
  ["C3", "离线提交整改，联网后状态为 rectifying", async () => {
    const fid = await getFinding();
    await go(`#/finding?id=${fid}`);
    if (!await waitFor(`(document.querySelector("main") || document.body).innerText.includes("S${fid}")`, 10000)) return { ok: false, detail: { err: "整改单没有打开", fid } };
    await ev(`(async () => {
      const blob = await ${PHOTO_JS};
      const f = new File([blob], "rect.jpg", { type: "image/jpeg", lastModified: Date.now() }); f.takenAt = wmIso(serverNow()); f.geo = null; f.place = "";
      window.takePhotos = async () => [f]; return 1;
    })()`);
    await setOffline(true);
    if (!await clickText("button", "拍整改照片")) { await setOffline(false); return { ok: false, detail: { err: "没有“拍整改照片”按钮（隐患状态不对？）", fid } }; }
    const queued = await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => rs.some((r) => r.kind === "action") && (document.querySelector("main") || document.body).innerText.includes("整改照片已暂存，联网后自动提交复核"))`, 8000);
    const kinds = (await idb()).map((r) => r.kind);
    await setOffline(false);
    const t0 = Date.now();
    const gone = await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => !rs.some((r) => r.kind === "action" || r.purpose === "rectify"))`, 45000, 500);
    const f = await apiGet(`/api/findings/${fid}`);
    const left = gone ? [] : await idb();
    return { ok: !!queued && !!gone && f.status === "rectifying", detail: { fid, queued: !!queued, kinds_offline: kinds, synced_in_ms: gone ? Date.now() - t0 : null, status: f.status,
      verify_state: f.verify && f.verify.state, queue_left: left, stub: STUB ? await ev("window.__zbStub") : null } };
  }],
  ["C4", "请求挂起：超时后转入暂存，不一直转圈；之后补传成功", async () => {
    await go("#/home"); await waitFor(`!!document.querySelector(".cap-card")`, 10000);
    const text = "离线检查C4 " + Date.now();
    let held = null, passed = 0;
    const h = (m) => {
      if (m.method !== "Fetch.requestPaused") return;
      if (!held) { held = m.params.requestId; return; }  // 第一次：挂着不放（服务器收不到）
      passed++; cmd("Fetch.continueRequest", { requestId: m.params.requestId }).catch(() => {});
    };
    listeners.add(h);
    await cmd("Fetch.enable", { patterns: [{ urlPattern: "*/api/quick/sync/note*", requestStage: "Request" }] });
    try {
      const t0 = Date.now();
      await ev(`(() => { const i = document.getElementById("todo-focus-note"); i.value = ${JSON.stringify(text)}; i.dispatchEvent(new Event("input")); return 1; })()`);
      await sleep(250);  // 等输入框内容生效、按钮可点
      await clickText(".cap-text button", "记");
      const q = await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => rs.some((r) => r.kind === "note" && r.body.text === ${JSON.stringify(text)}))`, 40000, 300);
      const ms = Date.now() - t0;
      const ui = await ev(`(() => { const c = document.querySelector(".cap-card"); const row = [...c.querySelectorAll(".tnote")].find((x) => x.innerText.includes(${JSON.stringify(text)}));
        return { row: !!row, chip: !!row && row.innerText.includes("待上传"), photo_btn_enabled: !c.querySelector(".cap-photo").disabled, spinner: !!document.querySelector(".cap-card .spinner") }; })()`);
      if (held) cmd("Fetch.continueRequest", { requestId: held }).catch(() => {});  // 页面早已放弃这次请求，放行与否都不影响
      const gone = await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => !rs.some((r) => r.kind === "note" && r.body.text === ${JSON.stringify(text)}))`, 40000, 500);
      const n = (await serverNotes()).filter((x) => x.text === text).length;
      return { ok: !!held && !!q && ui.row && ui.chip && ui.photo_btn_enabled && !ui.spinner && !!gone && n === 1,
        detail: { held: !!held, queued_after_ms: q ? ms : null, ui, resent: passed, server_count: n } };
    } finally { listeners.delete(h); await cmd("Fetch.disable").catch(() => {}); }
  }],
  ["C5", "响应挂起（服务器已处理）：超时重发并放行，服务器上只有 1 条", async () => {
    await go("#/home"); await waitFor(`!!document.querySelector(".cap-card")`, 10000);
    const text = "离线检查C5 " + Date.now();
    let held = null, resent = 0;
    const h = (m) => {
      if (m.method !== "Fetch.requestPaused") return;
      if (!held) { held = m.params.requestId; return; }  // 第一次：服务器已处理完，响应挂着不放
      resent++;  // 重发的那次：放行，同时把挂着的第一次也放行
      cmd("Fetch.continueRequest", { requestId: m.params.requestId }).catch(() => {});
      cmd("Fetch.continueRequest", { requestId: held }).catch(() => {});
    };
    listeners.add(h);
    await cmd("Fetch.enable", { patterns: [{ urlPattern: "*/api/quick/sync/note*", requestStage: "Response" }] });
    try {
      await ev(`(() => { const i = document.getElementById("todo-focus-note"); i.value = ${JSON.stringify(text)}; i.dispatchEvent(new Event("input")); return 1; })()`);
      await sleep(250);  // 等输入框内容生效、按钮可点
      await clickText(".cap-text button", "记");
      const q = await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => rs.find((r) => r.kind === "note" && r.body.text === ${JSON.stringify(text)})).then((r) => r && r.cid)`, 40000, 300);
      const gone = q && await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => !rs.some((r) => r.cid === ${JSON.stringify(q || "")}))`, 40000, 500);
      const n = (await serverNotes()).filter((x) => x.text === text).length;
      return { ok: !!held && !!q && resent >= 1 && !!gone && n === 1, detail: { held: !!held, client_id: q, resent, server_count: n } };
    } finally { listeners.delete(h); await cmd("Fetch.disable").catch(() => {}); }
  }],
  ["C6", "迟到 5 天的记录带“迟到补传 5 天”标签", async () => {
    const day = await ev(`(() => { const d = new Date(Date.now() - 5 * 86400000); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); })()`);
    let made = null;
    if (!STUB) {  // 真实接口：补传一条 5 天前记下的随手记（按记录时间入账）
      made = await ev(`(async () => { const at = new Date(Date.now() - 5 * 86400000); const r = await fetch("/api/quick/sync/note", { method: "POST", headers: { "x-zb": "1", "content-type": "application/json" },
        body: JSON.stringify({ client_id: "oc6" + Date.now(), text: "离线检查C6 五天前的记录", source: "text", client_time: at.toISOString() }) }); return { status: r.status }; })()`);
    }
    await go(`#/week?date=${day}`);
    const ok = await waitFor(`(document.querySelector("main") || document.body).innerText.includes("迟到补传 5 天")`, 15000, 300);
    const late = STUB ? null : await apiGet("/api/quick/late-notes?days=21").catch(() => null);
    return { ok: !!ok, detail: { week_of: day, created: made, late_notes: Array.isArray(late) ? late.slice(0, 5) : late } };
  }],
  ["C7", "页面已加载后断网，应用内打开整改单与首页不白屏", async () => {
    const fid = await getFinding();
    await go(`#/finding?id=${fid}`);
    await waitFor(`(document.querySelector("main") || document.body).innerText.includes("S${fid}")`, 10000);
    await go("#/home"); await waitFor(`!!document.querySelector(".cap-card")`, 10000);
    await setOffline(true);
    try {
      const look = async (hash, must) => {
        await go(hash); await sleep(1500);
        const t = (await mainText()) || "";
        return { hash, chars: t.trim().length, tip: t.includes(OFFLINE_TIP), must: must ? t.includes(must) : true };
      };
      const seen = await look(`#/finding?id=${fid}`, `S${fid}`);
      const unseen = await look("#/finding?id=999999", "");
      const home = await look("#/home", "随手记");
      const all = [seen, unseen, home];
      return { ok: all.every((x) => x.chars > 20 && x.tip && x.must), detail: all };
    } finally { await setOffline(false); }
  }],
];

// ---------------------------------------------------------------------------- --sw：离线启动（T-10c）
// 断网 / 恢复：页面级仿真 + 本实例 Service Worker 目标上的同样仿真（浏览器级 Fetch 拦截管不到 SW 自己发的请求）+ 浏览器级拦截兜底。
// 找不到 SW 或没有浏览器级连接就直接报错，不假装断了网
const swSessions = new Map();
async function swAttach() {
  const { targetInfos } = await bcmd("Target.getTargets");
  for (const t of targetInfos.filter((x) => x.type === "service_worker" && x.url.startsWith(BASE))) {
    if (swSessions.has(t.targetId)) continue;
    const { sessionId } = await bcmd("Target.attachToTarget", { targetId: t.targetId, flatten: true });
    await bcmd("Network.enable", {}, sessionId).catch(() => {});
    swSessions.set(t.targetId, sessionId);
  }
  return [...swSessions.values()];
}
async function setNet(off) {
  if (!bws) throw new Error("没有浏览器级 CDP 连接，无法让 Service Worker 断网");
  const sws = await swAttach();
  if (off && !sws.length) throw new Error("找不到本实例的 Service Worker（没注册上？）");
  for (const sid of sws) await bcmd("Network.emulateNetworkConditions", { offline: off, latency: 0, downloadThroughput: -1, uploadThroughput: -1 }, sid).catch(() => {});
  if (off) { blocked.length = 0; await bcmd("Fetch.enable", { patterns: [{ urlPattern: BASE + "/*" }] }); }
  else await bcmd("Fetch.disable");
  await setOffline(off);
}
// 整页重新打开（冷启动）：记下文档响应来自哪里（fromServiceWorker、SW 的来源 cache-storage / network）
async function coldOpen(hash, ready, ms = 25000) {
  let doc = null;
  const h = (m) => {
    if (m.method !== "Network.responseReceived" || m.params.type !== "Document" || !m.params.response.url.startsWith(BASE)) return;
    const r = m.params.response;
    doc = { status: r.status, fromServiceWorker: !!r.fromServiceWorker, source: r.serviceWorkerResponseSource || "" };
  };
  listeners.add(h);
  try {
    const nav = await cmd("Page.navigate", { url: BASE + "/?oc=" + Date.now() + hash });
    const ok = nav.errorText ? null : await waitFor(ready, ms);
    await sleep(300);
    return { ok: !!ok, doc, nav_error: nav.errorText || "" };
  } finally { listeners.delete(h); }
}
// 以某个账号在页面里登录（开发态快捷登录），再整页打开首页
async function loginAs(acc, ready) {
  await ev(`fetch("/api/dev/login", { method: "POST", headers: { "x-zb": "1", "content-type": "application/json" }, body: JSON.stringify({ user_id: ${acc.id} }) }).then((r) => r.status)`);
  await ev(`localStorage.setItem("zb_dev_user", "${acc.id}"); sessionStorage.removeItem("zb_no_auto"); 1`);
  return coldOpen("", `typeof store !== "undefined" && store.me && store.me.user.id === ${acc.id} && !store.offline && ${ready}`);
}
const meCache = () => ev(`localStorage.getItem(${JSON.stringify("zb_me_offline")})`);
const offlineState = () => ev(`(() => { const m = (document.querySelector("main") || document.body).innerText || ""; return { offline: typeof store !== "undefined" && !!store.offline,
  banner: m.replace(/\\s+/g, "").includes(${JSON.stringify(OFFLINE_MODE_TIP)}), chars: m.trim().length, head: m.trim().slice(0, 160) }; })()`);
async function shot(name) {
  if (!SHOTS) return "";
  fs.mkdirSync(SHOTS, { recursive: true });
  const f = path.join(SHOTS, name + ".jpg");
  fs.writeFileSync(f, Buffer.from((await cmd("Page.captureScreenshot", { format: "jpeg", quality: 80 })).data, "base64"));
  return f;
}
const FAKE_PHOTO = `(async () => { const blob = await ${PHOTO_JS}; const f = new File([blob], "sw.jpg", { type: "image/jpeg", lastModified: Date.now() });
  f.takenAt = wmIso(serverNow()); f.geo = null; f.place = ""; window.takePhotosToUpload = async () => ({ files: [f], scan: 0 }); return 1; })()`;

const SW_CASES = [
  ["S1", "在线打开：SW 接管页面，缓存有首页外壳、没有 /api，本机身份缓存只有显示字段", async () => {
    const ctl = await waitFor(`!!(navigator.serviceWorker && navigator.serviceWorker.controller)`, 20000);
    // 等安装时的预缓存写完（首页 + 页面引用的全部静态资源）
    const keys = await waitFor(`(async () => { const out = []; for (const n of await caches.keys()) { const c = await caches.open(n); for (const r of await c.keys()) out.push(r.url.replace(location.origin, "")); }
      return out.includes("/") && out.some((u) => u.startsWith("/static/core.js")) && out.some((u) => u.startsWith("/static/main.js")) ? out : null; })()`, 20000, 500) || [];
    const raw = await meCache(), me = raw ? JSON.parse(raw) : null;
    const leak = ["username", "email", "token", "password", tech.username].filter((s) => raw && raw.includes(s));
    state.baseline = (await serverNotes()).map((n) => n.id);
    return { ok: !!ctl && keys.length > 0 && !keys.some((u) => u.startsWith("/api")) && !!me && me.user.id === tech.id && !leak.length,
      detail: { controlled: !!ctl, cached: keys.length, api_cached: keys.filter((u) => u.startsWith("/api")), me_keys: me ? Object.keys(me) : null, user_keys: me ? Object.keys(me.user) : null, leak } };
  }],
  ["S2", "断网冷启动首页：来自 SW 缓存，离线模式，页头提示，随手记可用", async () => {
    await setNet(true);
    const r = await coldOpen("#/home", `typeof store !== "undefined" && store.offline && !!document.querySelector(".cap-card")`);
    const ui = await ev(`(() => { const c = document.querySelector(".cap-card"); return !c ? null : { voice: !!c.querySelector(".voice.big button.mic"), photo: !!c.querySelector(".cap-photo") && !c.querySelector(".cap-photo").disabled,
      input: !!document.getElementById("todo-focus-note") }; })()`);
    const st = await offlineState();
    await shot("S2_tech_offline_home");
    const fromCache = !!r.doc && r.doc.fromServiceWorker && r.doc.source === "cache-storage";  // SW 取网络失败，页面外壳来自 SW 缓存
    return { ok: r.ok && fromCache && st.offline && st.banner && !!ui && ui.voice && ui.photo && ui.input,
      detail: { ...r, from_cache: fromCache, page_blocked: blocked.slice(0, 8), ui, state: st } };
  }],
  ["S3", "离线打字、说一句、拍一张：都进本机暂存，列表显示待上传", async () => {
    const text = "离线检查S3 " + Date.now();
    state.s3text = text;
    const before = await idb();
    await ev(`(() => { const i = document.getElementById("todo-focus-note"); i.value = ${JSON.stringify(text)}; i.dispatchEvent(new Event("input")); return 1; })()`);
    await sleep(250);
    await clickText(".cap-text button", "记");
    const typed = await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => rs.some((r) => r.kind === "note" && r.body.text === ${JSON.stringify(text)}))`, 8000);
    const voice = await ev(`(async () => {
      const b = document.querySelector(".cap-hero .voice.big button.mic"); if (!b) return { err: "找不到“说一句”按钮" };
      b.click(); await new Promise((x) => setTimeout(x, 1800));
      if (!b.classList.contains("on")) return { err: "没有开始录音：" + (store.toast ? store.toast.msg : "麦克风不可用") };
      const t0 = performance.now(); b.click();
      while (performance.now() - t0 < 5000) { if (document.querySelector(".cap-card").innerText.includes("已记下，待上传")) return { ms: Math.round(performance.now() - t0) }; await new Promise((x) => setTimeout(x, 50)); }
      return { err: "5 秒内没有出现“已记下，待上传”" };
    })()`);
    await ev(FAKE_PHOTO);
    await ev(`document.querySelector(".cap-card .cap-photo").click(), 1`);
    const photo = await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => rs.some((r) => r.kind === "media") && rs.some((r) => r.kind === "note" && (r.body.media_ids || []).length))`, 10000);
    const after = await idb();
    const ui = await ev(`(() => { const c = document.querySelector(".cap-card"), bar = document.querySelector(".qbar");
      return { rows: [...c.querySelectorAll(".tnote")].filter((x) => x.innerText.includes("待上传")).length, typed_row: [...c.querySelectorAll(".tnote")].some((x) => x.innerText.includes(${JSON.stringify(text)})),
               bar: bar ? bar.innerText.trim() : "" }; })()`);
    await shot("S3_tech_offline_queued");
    const kinds = (rs) => rs.reduce((o, r) => ((o[r.kind] = (o[r.kind] || 0) + 1), o), {});
    return { ok: !!typed && !voice.err && !!photo && ui.typed_row && ui.rows >= 3 && ui.bar.includes("待上传"),
      detail: { before: kinds(before), after: kinds(after), voice, ui, still_offline: (await offlineState()).offline } };
  }],
  ["S4", "恢复网络：自动补传，服务器上各有 1 条，退出离线模式", async () => {
    await setNet(false);
    const t0 = Date.now();
    const gone = await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => rs.length === 0)`, 60000, 500);
    const left = gone ? [] : await idb();
    let notes = [], vp = [];
    for (let k = 0; k < 20; k++) {  // 录音在服务器后台转写：转完是一条随手记，没转完在转写清单里
      notes = (await serverNotes()).filter((n) => !(state.baseline || []).includes(n.id));
      vp = await apiGet("/api/quick/voice-pending").catch(() => []);
      if (notes.some((n) => n.source === "voice") || (Array.isArray(vp) && vp.length)) break;
      await sleep(1000);
    }
    const typed = notes.filter((n) => n.text === state.s3text).length;
    const photo = notes.filter((n) => n.source === "photo" && (n.media || []).length).length;
    const voice = notes.filter((n) => n.source === "voice").length + (Array.isArray(vp) ? vp.length : 0);
    const st = await waitFor(`!store.offline && !(document.querySelector("main") || document.body).innerText.replace(/\\s+/g, "").includes(${JSON.stringify(OFFLINE_MODE_TIP)})`, 20000);
    return { ok: !!gone && typed === 1 && photo === 1 && voice === 1 && !!st,
      detail: { synced_in_ms: gone ? Date.now() - t0 : null, queue_left: left, server: { typed, photo, voice, voice_pending: Array.isArray(vp) ? vp.map((v) => v.status) : vp }, left_offline_mode: !!st } };
  }],
  ["S5", "退出登录清掉本机身份；再断网冷启动只到登录页", async () => {
    const had = !!(await meCache());
    await clickText("a", "退出登录");
    const cleared = await waitFor(`localStorage.getItem("zb_me_offline") === null && !store.me`, 10000);
    await setNet(true);
    try {
      const r = await coldOpen("", `typeof store !== "undefined" && !store.me && !!document.querySelector("form.login")`);
      const st = await offlineState();
      return { ok: had && !!cleared && r.ok && !!r.doc && r.doc.source === "cache-storage" && !st.offline, detail: { had_cache: had, cleared: !!cleared, ...r, state: st } };
    } finally { await setNet(false); }
  }],
  ["S6", "项目经理登录过后断网冷启动：不白屏，有页头提示", async () => {
    if (!mgr) return { ok: false, detail: { err: "没有项目经理/领导账号" } };
    const on = await loginAs(mgr, `(document.querySelector("main") || document.body).innerText.trim().length > 20`);
    if (!on.ok) return { ok: false, detail: { err: "在线登录后首页没打开", ...on } };
    await setNet(true);
    try {
      const r = await coldOpen("", `typeof store !== "undefined" && store.offline && !!document.querySelector("main")`);
      await sleep(3000);  // 等各块接口失败、出错态落定
      const st = await offlineState();
      await shot("S6_manager_offline");
      return { ok: r.ok && !!r.doc && r.doc.source === "cache-storage" && st.banner && st.chars > 20 && !st.head.includes("页面不存在"), detail: { role: mgr.role, ...r, state: st } };
    } finally { await setNet(false); }
  }],
  ["S7", "分包安全员登录过后断网冷启动：随手拍照常可用，拍一张进本机暂存", async () => {
    if (!subSafety) return { ok: false, detail: { err: "没有分包安全员账号" } };
    const on = await loginAs(subSafety, `!!document.querySelector("#todo-focus-snap")`);
    if (!on.ok) return { ok: false, detail: { err: "在线登录后首页没打开", ...on } };
    await setNet(true);
    try {
      const r = await coldOpen("#/shome", `typeof store !== "undefined" && store.offline && !!document.querySelector("#todo-focus-snap")`);
      const st = await offlineState();
      const snaps = await ev(`document.querySelectorAll("#todo-focus-snap").length`);
      await shot("S7_sub_safety_offline");
      await ev(FAKE_PHOTO);
      await ev(`document.querySelector("#todo-focus-snap").click(), 1`);
      const queued = await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => rs.some((r) => r.kind === "media" && r.purpose === "safety"))`, 10000);
      return { ok: r.ok && !!r.doc && r.doc.source === "cache-storage" && st.banner && snaps === 1 && !!queued, detail: { ...r, state: st, snap_buttons: snaps, queued: !!queued } };
    } finally {
      await setNet(false);
      await waitFor(`qrun("q", "readonly", (s) => s.getAll()).then((rs) => rs.length === 0)`, 40000, 500);  // 补传掉，不留在一次性浏览器里
    }
  }],
];

const ALL = [...CASES, ...SW_CASES];
const ORDER = SW ? ["S1", "S2", "S3", "S4", "S5", "S6", "S7"] : ["C1", "C2", "C4", "C5", "C3", "C7", "C6"];  // C2 接着 C1 的录音；C7 用 C3 打开过的整改单；S 按链路顺序
const results = {};
for (const id of ORDER) {
  const [, name, fn] = ALL.find((c) => c[0] === id);
  if (ONLY && !ONLY.includes(id)) continue;
  const t0 = Date.now(), errs0 = pageErrors.length;
  let r;
  try { r = await fn(); } catch (e) { r = { ok: false, detail: { err: e.message } }; await (SW ? setNet(false) : setOffline(false)).catch(() => {}); }
  results[id] = { id, name, ok: !!r.ok, ms: Date.now() - t0, detail: r.detail, page_errors: pageErrors.slice(errs0, errs0 + 5) };
  process.stderr.write(`${id} ${r.ok ? "通过" : "失败"}  ${name}\n`);
}
const cases = ALL.map((c) => results[c[0]]).filter(Boolean);
const out = { base: BASE, user: tech.username, stub: STUB, sw: SW, at: new Date().toISOString(), ok: cases.length > 0 && cases.every((c) => c.ok), cases };
const json = JSON.stringify(out, null, 1);
if (OUT) fs.writeFileSync(OUT, json);
process.stdout.write(json + "\n");
await finish(out.ok ? 0 : 1);
