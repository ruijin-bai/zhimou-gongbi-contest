// 实际视口截图（RJ 10-10 验收口径：100% 缩放、截 innerWidth × innerHeight，不截全页）。
// 用法：node tools/viewport_shot.mjs <base> <user_id> <路由，如 work> <宽> <输出.png> [高] [vp]
//   末尾写 vp 只截视口；不写则截全页。输出里 boxes 是主要区块的上下边（px），overflowX 非 0 表示横向溢出。
// 只对一次性演示实例运行（tools.demo_copy + tools.demo_serve），拒绝 8600/8601（连业务库的服务）。
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
const [BASE, UID, HASH, W, OUT, H0, MODE] = process.argv.slice(2);
const VP = MODE === "vp";
if (/:860[01](?!\d)/.test(BASE)) { console.error("拒绝对 8600、8601 截图（连业务库的服务），请用一次性演示实例"); process.exit(2); }
const W_ = +W, H = +(H0 || (W_ < 600 ? 844 : 900)), mobile = W_ < 600;
const PORT = 9400 + Math.floor(Math.random() * 400);
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const udd = path.join(path.dirname(path.resolve(OUT)), "_edge_" + PORT);
const edge = spawn(EDGE, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${udd}`, "--no-first-run", "--disable-gpu", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let targets;
for (let i = 0; i < 50; i++) { try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); if (targets.length) break; } catch (_) {} await sleep(300); }
const ws = new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let seq = 0; const pend = new Map(); const logs = [];
ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown") logs.push("EXC " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 300));
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") logs.push("err " + m.params.args.map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 300));
  if (m.method === "Network.responseReceived" && m.params.response.status >= 400) logs.push(`HTTP ${m.params.response.status} ${m.params.response.url.replace(BASE, "")}`); });
const cmd = (method, params = {}) => new Promise((r) => { const id = ++seq; pend.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
const ev = async (expr) => (await cmd("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result?.result?.value;
await cmd("Runtime.enable"); await cmd("Page.enable"); await cmd("Network.enable");
await cmd("Emulation.setDeviceMetricsOverride", { width: W_, height: H, deviceScaleFactor: 1, mobile });
await cmd("Page.navigate", { url: BASE + "/static/favicon.svg" }); await sleep(1200);
await ev(`localStorage.setItem("zb_dev_user","${UID}"); 1`);
await ev(`fetch("/api/dev/login",{method:"POST",headers:{"x-zb":"1","content-type":"application/json"},body:JSON.stringify({user_id:${UID}})}).then(r=>r.status)`);
await ev(`localStorage.setItem("zb_dev_user","${UID}"); sessionStorage.removeItem("zb_no_auto"); 1`);
await cmd("Page.navigate", { url: BASE + "/#/" + HASH }); await sleep(4500);
const lm = await cmd("Page.getLayoutMetrics");
const ch = Math.min(Math.ceil(lm.result.cssContentSize.height), 5000);
const overflowX = await ev(`document.documentElement.scrollWidth > window.innerWidth + 1 ? document.documentElement.scrollWidth : 0`);
const shot = VP ? await cmd("Page.captureScreenshot", { format: "png" }) : await cmd("Page.captureScreenshot", { format: "png", captureBeyondViewport: true, clip: { x: 0, y: 0, width: W_, height: Math.max(ch, H), scale: 1 } });
const boxes = await ev(`JSON.stringify([...document.querySelectorAll("main > div > section, main > div > .zb-kpis, main > div > .zb-row, .hdr, .topbar")].map(e=>{const r=e.getBoundingClientRect();return e.className.split(" ")[0]+":"+Math.round(r.top)+"-"+Math.round(r.bottom)}))`);
fs.writeFileSync(OUT, Buffer.from(shot.result.data, "base64"));
console.log(JSON.stringify({ out: OUT, height: ch, overflowX, boxes, logs: logs.filter((l) => !/media\/\d+\/(thumb|file)|output\/overview/.test(l)) }));
ws.close();
spawnSync("taskkill", ["/pid", String(edge.pid), "/T", "/F"], { stdio: "ignore" });
try { fs.rmSync(udd, { recursive: true, force: true }); } catch (_) {}
process.exit(0);
