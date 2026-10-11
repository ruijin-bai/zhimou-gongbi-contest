// 按岗位体检截图：用本机 Edge 无头浏览器，以各岗位身份遍历导航里的全部页面，保存截图 + 页面文字 + 控制台/接口错误。
// 用法：node tools/role_shots.mjs [岗位,岗位] --base http://localhost:8612 [--out 目录] [--cdp-port 9333]
// --base 必填，只对一次性实例（ZB_DEV_LOGIN=1、mock AI、临时数据目录）运行：截图会登录各岗位并打开现场圈、周确认（打开即写库或调用 AI），
// 所以拒绝 8600、8601（连业务库的服务）。CDP 端口默认 9333，同时跑多个截图任务时用 --cdp-port 错开。
// 账号按用户名在 /api/dev/accounts 里查（默认仿真账号 sim_*）；输出目录下 <岗位>/NN_<页>.jpg/.txt 与 index.json。
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const die = (msg) => { console.error(msg); process.exit(2); };
const BASE = String(opt("--base", "") || "").replace(/\/+$/, "");
if (!BASE || BASE.startsWith("--")) die("请用 --base 指定一次性实例的地址，例如 --base http://localhost:8612（不再默认 8600）");
let target = null;
try { target = new URL(BASE); } catch (_) { die(`--base 不是有效网址：${BASE}`); }
if (!/^https?:$/.test(target.protocol)) die(`--base 只支持 http/https：${BASE}`);
if (["8600", "8601"].includes(target.port) || /:860[01](?!\d)/.test(BASE)) die(`拒绝对 8600、8601 截图（连业务库的服务）：${BASE}，请改用一次性实例`);
const PORT = Number(opt("--cdp-port", "9333"));
if (!Number.isInteger(PORT) || PORT < 1024 || PORT > 65535) die("--cdp-port 须为 1024–65535 的整数");
const OUT = path.resolve(opt("--out", "role_shots"));
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const USERS = [
  { user: "sim_zhao", role: "tech", mobile: true }, { user: "sim_safe_a", role: "sub_safety", mobile: true },
  { user: "sim_eng", role: "eng_sup" }, { user: "sim_safety", role: "safety_sup" }, { user: "sim_pm", role: "manager" },
  { user: "sim_leader", role: "leader" }, { user: "sim_admin", role: "admin" },
];
const only = argv[0] && !argv[0].startsWith("--") ? argv[0].split(",") : null;
const accounts = await (await fetch(BASE + "/api/dev/accounts")).json();
for (const u of USERS) u.id = (accounts.find((a) => a.username === u.user) || {}).id;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

fs.mkdirSync(OUT, { recursive: true });
const udd = path.join(OUT, "_edge");
const edge = spawn(EDGE, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${udd}`, "--no-first-run", "--disable-gpu", "about:blank"], { stdio: "ignore" });
let targets;
for (let i = 0; i < 40; i++) { try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); if (targets.length) break; } catch (_) {} await sleep(300); }
const page = targets.find((t) => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let seq = 0; const pend = new Map(); let logs = [];
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown") logs.push("EXC " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 300));
  if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) logs.push(m.params.type + " " + m.params.args.map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 300));
  if (m.method === "Network.responseReceived" && m.params.response.status >= 400 && m.params.response.url.includes("/api/")) logs.push(`HTTP ${m.params.response.status} ${m.params.response.url.replace(BASE, "")}`);
});
const cmd = (method, params = {}) => new Promise((r) => { const id = ++seq; pend.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
const ev = async (expr) => { const r = await cmd("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }); return r.result?.result?.value; };
await cmd("Runtime.enable"); await cmd("Page.enable"); await cmd("Network.enable");

const index = [];
for (const u of USERS) {
  if (only && !only.includes(u.role)) continue;
  if (!u.id) { console.log("找不到账号", u.user); continue; }
  const dir = path.join(OUT, u.role); fs.mkdirSync(dir, { recursive: true });
  const W = u.mobile ? 390 : 1440, H = u.mobile ? 844 : 900;
  await cmd("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: u.mobile ? 1.5 : 1, mobile: !!u.mobile });
  await cmd("Page.navigate", { url: BASE + "/" }); await sleep(3800);
  await ev(`fetch("/api/dev/login",{method:"POST",headers:{"x-zb":"1","content-type":"application/json"},body:JSON.stringify({user_id:${u.id}})}).then(r=>r.status)`);
  await ev(`localStorage.setItem("zb_dev_user","${u.id}"); sessionStorage.removeItem("zb_no_auto"); 1`);
  await cmd("Page.navigate", { url: BASE + "/" }); await sleep(4500);
  const pages = await ev(`(() => { const r = store.me.user.role; const n = buildNav(r, (store.me.project||{}).features||{}); return { home: location.hash, pages: [...n.top, ...n.bottom].flatMap(l => l.pages.map(p => ({k:p.k, label:p.label, line:l.name, route:p.route, params:p.params||{}}))) }; })()`);
  if (!pages) { console.log("no nav for", u.role); continue; }
  let i = 0;
  for (const p of pages.pages) {
    i++; logs = [];
    const q = new URLSearchParams(p.params).toString();
    await ev(`location.hash = "#/${p.route}${q ? "?" + q : ""}"; 1`);
    await sleep(3500);
    const text = await ev(`(document.querySelector("main")||document.body).innerText`);
    const lm = await cmd("Page.getLayoutMetrics");
    const ch = Math.min(Math.ceil(lm.result.cssContentSize.height), u.mobile ? 5000 : 3600);
    const shot = await cmd("Page.captureScreenshot", { format: "jpeg", quality: 70, captureBeyondViewport: true, clip: { x: 0, y: 0, width: W, height: Math.max(ch, H), scale: 1 } });
    const name = `${String(i).padStart(2, "0")}_${p.k}`;
    fs.writeFileSync(path.join(dir, name + ".jpg"), Buffer.from(shot.result.data, "base64"));
    fs.writeFileSync(path.join(dir, name + ".txt"), `# ${u.role} · ${p.line} › ${p.label}  (#/${p.route}${q ? "?" + q : ""})\n# 控制台/接口错误：${logs.length ? "\n" + logs.join("\n") : "无"}\n\n${text || ""}`);
    index.push({ role: u.role, k: p.k, label: p.label, line: p.line, file: `${u.role}/${name}`, errors: logs.length, height: ch });
    console.log(u.role, name, ch, logs.length ? "ERR" + logs.length : "");
  }
}
fs.writeFileSync(path.join(OUT, "index.json"), JSON.stringify(index, null, 1));
ws.close();
// Windows 上 kill 只结束主进程，Edge 的子进程会占着用户数据目录：按进程树结束
if (process.platform === "win32") spawnSync("taskkill", ["/pid", String(edge.pid), "/T", "/F"], { stdio: "ignore" }); else edge.kill();
process.exit(0);
