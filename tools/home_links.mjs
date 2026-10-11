// 首页点击目标检查（RJ 2026-10-10：工程主管点“今日随手记”“本周确认”都跳到报表日报——指标卡各自应指向对应明细）。
// 以七个岗位登录各自首页，逐个点击指标卡（.zb-kpi）和卡片底部/标题行的入口链接，记下点开后到了哪里：
//   换页 → 目标地址（#/路由?参数）；就地抽屉 → “抽屉：标题”；就地展开/滚动 → “就地”。
// 判定：同一首页的指标卡目标彼此不重复；目标页存在（不是“页面不存在”、不报错、不是“不在查看范围”）。
// 用法：node tools/home_links.mjs --base http://127.0.0.1:8661 [--roles eng_sup,manager] [--out 目录] [--cdp-port 9333]
// 只对一次性演示实例运行（拒绝 8600、8601）。输出 <out>/home_links.md、home_links.json；有问题时退出码 1。
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const BASE = String(opt("--base", "") || "").replace(/\/+$/, "");
if (!BASE || /:860[01](?!\d)/.test(BASE)) { console.error("请用 --base 指定一次性演示实例（不能是 8600、8601）"); process.exit(2); }
const PORT = Number(opt("--cdp-port", "9333"));
const OUT = path.resolve(opt("--out", "home_links"));
const EDGE = process.env.EDGE_PATH || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const ROLES = [
  { user: "sim_zhao", role: "tech", name: "技术员", w: 390, h: 844 }, { user: "sim_safe_a", role: "sub_safety", name: "分包安全员", w: 390, h: 844 },
  { user: "sim_eng", role: "eng_sup", name: "工程主管" }, { user: "sim_safety", role: "safety_sup", name: "安全主管" },
  { user: "sim_pm", role: "manager", name: "项目经理" }, { user: "sim_leader", role: "leader", name: "领导" }, { user: "sim_admin", role: "admin", name: "系统管理员" },
];
const only = opt("--roles", "") ? opt("--roles").split(",") : null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const accounts = await (await fetch(BASE + "/api/dev/accounts")).json();
for (const u of ROLES) u.id = (accounts.find((a) => a.username === u.user) || {}).id;

fs.mkdirSync(OUT, { recursive: true });
const udd = path.join(OUT, "_edge_" + PORT);
const edge = spawn(EDGE, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${udd}`, "--no-first-run", "--disable-gpu", "about:blank"], { stdio: "ignore" });
let targets;
for (let i = 0; i < 50; i++) { try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); if (targets.length) break; } catch (_) {} await sleep(300); }
const ws = new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let seq = 0; const pend = new Map();
ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } });
const cmd = (method, params = {}) => new Promise((r) => { const id = ++seq; pend.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
const ev = async (expr) => (await cmd("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result?.result?.value;
await cmd("Runtime.enable"); await cmd("Page.enable");

// 首页上要检查的可点元素：指标卡 + 卡片标题行/底部的入口链接（按钮式办理动作不在此列）
const LIST = `[...document.querySelectorAll("main .zb-kpi, main .zb-foot a, main .zb-ch a.zb-link, main .zb-ch button.zb-link")].filter((e) => e.getBoundingClientRect().width)`;
const rows = [];
for (const u of ROLES) {
  if (only && !only.includes(u.role)) continue;
  if (!u.id) continue;
  await cmd("Emulation.setDeviceMetricsOverride", { width: u.w || 1440, height: u.h || 900, deviceScaleFactor: 1, mobile: !!u.w });
  await cmd("Page.navigate", { url: BASE + "/static/favicon.svg" }); await sleep(500);
  await ev(`fetch("/api/dev/login",{method:"POST",headers:{"x-zb":"1","content-type":"application/json"},body:JSON.stringify({user_id:${u.id}})}).then(r=>r.status)`);
  await ev(`localStorage.setItem("zb_dev_user","${u.id}"); sessionStorage.removeItem("zb_no_auto"); 1`);
  await cmd("Page.navigate", { url: BASE + "/" }); await sleep(3500);
  const home = await ev("location.hash");
  const n = await ev(`${LIST}.length`);
  for (let i = 0; i < n; i++) {
    await ev(`location.hash = ${JSON.stringify(home)}; 1`); await sleep(1800);
    const info = await ev(`(() => { const e = ${LIST}[${i}]; if (!e) return null; const kpi = e.classList.contains("zb-kpi");
      const label = kpi ? (e.querySelector(".zb-kpi-l")||e).innerText.trim() : e.innerText.trim().replace(/\\s+/g, " ");
      const card = (e.closest(".zb-card, section")?.querySelector(".zb-ch h2")?.innerText || "").trim();
      e.click(); return { kind: kpi ? "指标卡" : "入口", label: label.slice(0, 24), card: card.slice(0, 16) }; })()`);
    if (!info) continue;
    await sleep(1800);
    const res = await ev(`(() => { const dw = document.querySelector(".zb-dw-h b, .sd-hd b, .modal h2");
      const main = document.querySelector("main"); const t = main ? main.innerText : "";
      return { hash: location.hash, drawer: dw ? dw.innerText.trim() : "", bad: /页面不存在|不在.*查看范围|出错了|加载失败/.test(t.slice(0, 400)) }; })()`);
    const target = res.drawer ? "抽屉：" + res.drawer : res.hash === home ? "就地" : res.hash;
    rows.push({ role: u.role, roleName: u.name, home, ...info, target, bad: res.bad && !res.drawer });
    if (res.drawer) await ev(`(document.querySelector(".zb-dw-x, .sd-hd button, .modal .btn")||{click(){}}).click(); 1`);
  }
}
// 判定
const problems = [];
const byRole = {};
for (const r of rows) (byRole[r.role] = byRole[r.role] || []).push(r);
for (const [role, list] of Object.entries(byRole)) {
  const k = list.filter((r) => r.kind === "指标卡" && r.target !== "就地");
  const seen = {};
  for (const r of k) { (seen[r.target] = seen[r.target] || []).push(r.label); }
  for (const [t, ls] of Object.entries(seen)) if (ls.length > 1) problems.push(`${list[0].roleName}：指标卡 ${ls.map((x) => "「" + x + "」").join("、")} 指向同一处 ${t}`);
  for (const r of list) if (r.bad) problems.push(`${r.roleName}：${r.kind}「${r.label}」打开后页面异常（${r.target}）`);
}
fs.writeFileSync(path.join(OUT, "home_links.json"), JSON.stringify(rows, null, 1));
let md = `# 首页点击 → 目标页对照\n\n- 实例：${BASE} · ${new Date().toISOString().slice(0, 16).replace("T", " ")}（UTC）\n- 判定：同一首页的指标卡目标不重复；目标页能正常打开。\n\n`;
md += problems.length ? `## 问题 ${problems.length} 项\n\n${problems.map((p) => "- " + p).join("\n")}\n\n` : "## 问题：无\n\n";
md += `| 岗位 | 类型 | 所在块 | 名称 | 点开后 |\n|---|---|---|---|---|\n`;
for (const r of rows) md += `| ${r.roleName} | ${r.kind} | ${r.card || "—"} | ${r.label} | ${r.target}${r.bad ? " ⚠" : ""} |\n`;
fs.writeFileSync(path.join(OUT, "home_links.md"), md);
console.log(md);
ws.close();
if (process.platform === "win32") spawnSync("taskkill", ["/pid", String(edge.pid), "/T", "/F"], { stdio: "ignore" }); else edge.kill();
try { fs.rmSync(udd, { recursive: true, force: true }); } catch (_) {}
process.exit(problems.length ? 1 : 0);
