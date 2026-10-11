// 全站一屏化度量（RJ 2026-10-10）：七个岗位 × 导航里的每个页面（另加几类详情页）× 电脑/手机多档视口，量
//   页面高度 ÷ 视口高（页面本身是否出现纵向滚动）、横向溢出、大段文字（单块正文超过 3 行）、卡片数，输出超标清单。
// 口径（RJ 10-10 两档）：一档＝首页/总览类（各岗位首页、领导总览、管理员概况、预测与赶工、产值），电脑 1366×768 与 1440×900 严格一屏；
//      二档＝列表、台账、详情、文书、配置类，页头与筛选固定、内容在固定高度面板内滚动或分页，整页同样不超过视口（面板内滚动不算超）。
//      纯罗列列表必须用 <zb-paged> 内部分页：同一容器里同类子项超过 12 个且不在分页组件里，记为“未分页长列表”。
//      现场圈按朋友圈方式连续下滑，豁免分页检查与手机 2 屏上限。
//      正文区底部不得有独立的说明段落（免责/口径/来源说明并进页头“?”），记为“底部说明段落”。
//      非首页不得有实景大横幅，页头高度电脑 ≤ 60px、手机 ≤ 156px（各岗位首页除外）。
//      电脑容差 2px；手机页面不超过约 2 屏（≤ 2.1 倍）；任何宽度都不得横向溢出；正文块可见超过 3 行且多于 80 字算“大段文字”（两档都要截断）。
// 用法：node tools/fit_check.mjs --base http://127.0.0.1:8690 [--roles manager,eng_sup] [--vp 1366x768,1440x900,360x780,390x844,430x932]
//        [--out 目录] [--cdp-port 9333] [--pages work,eng] [--no-detail] [--shots] [--stress（追加长英文串，查真实数据下的横向溢出）]
//        node tools/fit_check.mjs --report a/fit.json,b/fit.json --out 目录   （只把几次度量合并成一份清单，不开浏览器）
// 只对一次性演示实例（tools.demo_copy + tools.demo_serve，mock AI、调度关闭）运行：会登录各岗位、打开现场圈等页面（可能写库），
// 所以拒绝 8600、8601（连业务库的服务）。输出：<out>/fit.json、<out>/fit.md；--shots 另存视口截图 <out>/<岗位>/<视口>_<页>.jpg。
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const flag = (k) => argv.includes(k);
const die = (msg) => { console.error(msg); process.exit(2); };
const BASE = String(opt("--base", "") || "").replace(/\/+$/, "");
const REPORT = opt("--report", "");
if (!REPORT && (!BASE || BASE.startsWith("--"))) die("请用 --base 指定一次性演示实例的地址，例如 --base http://127.0.0.1:8690");
if (/:860[01](?!\d)/.test(BASE)) die(`拒绝对 8600、8601 度量（连业务库的服务）：${BASE}`);
const PORT = Number(opt("--cdp-port", "9333"));
const OUT = path.resolve(opt("--out", "fit_check"));
const SHOTS = flag("--shots");
const STRESS = flag("--stress");
const EDGE = process.env.EDGE_PATH || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const VPS = String(opt("--vp", "1366x768,1440x900,360x780,390x844,430x932")).split(",").map((s) => { const [w, h] = s.split("x").map(Number); return { w, h, name: s, mobile: w < 861 }; });
const ROLES = [
  { user: "sim_zhao", role: "tech", name: "技术员" }, { user: "sim_safe_a", role: "sub_safety", name: "分包安全员" },
  { user: "sim_eng", role: "eng_sup", name: "工程主管" }, { user: "sim_safety", role: "safety_sup", name: "安全主管" },
  { user: "sim_pm", role: "manager", name: "项目经理" }, { user: "sim_leader", role: "leader", name: "领导" }, { user: "sim_admin", role: "admin", name: "系统管理员" },
];
const onlyRoles = opt("--roles", "") ? opt("--roles").split(",") : null;
const onlyPages = opt("--pages", "") ? opt("--pages").split(",") : null;
const MOBILE_MAX = 2.1;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const accounts = REPORT ? [] : await (await fetch(BASE + "/api/dev/accounts")).json();
for (const u of ROLES) u.id = (accounts.find((a) => a.username === u.user) || {}).id;

fs.mkdirSync(OUT, { recursive: true });
const udd = path.join(OUT, "_edge_" + PORT);
const edge = REPORT ? null : spawn(EDGE, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${udd}`, "--no-first-run", "--disable-gpu", "about:blank"], { stdio: "ignore" });
let targets;
for (let i = 0; i < 50 && !REPORT; i++) { try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); if (targets.length) break; } catch (_) {} await sleep(300); }
const ws = REPORT ? null : new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
if (ws) await new Promise((r) => ws.addEventListener("open", r));
let seq = 0; const pend = new Map(); let errs = [];
ws && ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown") errs.push("EXC " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 200));
});
const cmd = (method, params = {}) => new Promise((r) => { const id = ++seq; pend.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
const ev = async (expr) => (await cmd("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result?.result?.value;
if (ws) { await cmd("Runtime.enable"); await cmd("Page.enable"); }

// 页面内度量（返回纯数据）
const MEASURE = `(() => {
  const W = Math.min(innerWidth, window.__fitW || innerWidth), H = innerHeight, de = document.documentElement;
  const docH = Math.max(de.scrollHeight, document.body.scrollHeight);
  const overX = Math.max(0, de.scrollWidth - W);
  const main = document.querySelector("main") || document.body;
  const vis = (el) => { const r = el.getBoundingClientRect(); if (!r.width || !r.height) return false; const s = getComputedStyle(el); return s.visibility !== "hidden" && s.display !== "none"; };
  const name = (el) => { let s = el.tagName.toLowerCase(); const c = [...el.classList].filter((x) => !/^(on|shut|row|grow)$/.test(x)).slice(0, 2); if (c.length) s += "." + c.join("."); return s; };
  const pathOf = (el) => { const a = []; for (let x = el; x && x !== main && a.length < 4; x = x.parentElement) a.unshift(name(x)); return a.join(" > "); };
  // 横向溢出的罪魁：右边超出视口、且不在可横向滚动的容器里
  const wide = [];
  if (overX > 1) {
    for (const el of document.querySelectorAll("body *")) {
      const r = el.getBoundingClientRect(); if (r.right <= W + 1 || !r.width) continue; if (el.closest(".tabbar, .topbar, .sheet-bg, .zb-dw")) continue;
      let clipped = false; for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) { const o = getComputedStyle(p).overflowX; if (o !== "visible") { const pr = p.getBoundingClientRect(); if (pr.right <= W + 1) { clipped = true; break; } } }
      if (!clipped) wide.push({ el: pathOf(el), right: Math.round(r.right) });
    }
    wide.sort((a, b) => b.right - a.right);
  }
  const offenders = []; const seen = new Set();
  for (const w of wide) { if (seen.has(w.el)) continue; seen.add(w.el); offenders.push(w); if (offenders.length >= 4) break; }
  // 大段文字：块级、没有块级子元素、可见高度超过 3 行、文字多于 80 字（截断/折叠后的不算）
  const longs = [];
  for (const el of main.querySelectorAll("p, div, li, td, span, section, article, blockquote, pre, dd")) {
    if (!vis(el)) continue;
    const s = getComputedStyle(el); if (s.display === "inline") continue;
    if ([...el.children].some((c) => { const d = getComputedStyle(c).display; return d !== "inline" && d !== "inline-block" && d !== "none" && c.tagName !== "BR"; })) continue;
    const t = (el.innerText || "").trim(); if (t.length <= 80) continue;
    const lh = parseFloat(s.lineHeight) || parseFloat(s.fontSize) * 1.5; const h = el.getBoundingClientRect().height;
    const shown = Math.min(h, el.clientHeight || h);
    if (shown / lh <= 3.4) continue;
    longs.push({ el: pathOf(el), chars: t.length, lines: Math.round(shown / lh), text: t.slice(0, 40).replace(/\\s+/g, " ") });
  }
  longs.sort((a, b) => b.lines - a.lines);
  // 未分页的长列表：同一容器里同样标签+类名的可见子项超过 12 个，且不在 <zb-paged> 里（RJ 10-10：纯罗列列表必须内部分页）
  const lists = [];
  for (const el of main.querySelectorAll("*")) {
    if (el.children.length <= 12 || el.closest(".zb-paged") || !vis(el)) continue;
    const sig = {}; for (const c of el.children) { if (getComputedStyle(c).display === "none") continue; const k = c.tagName + "." + (c.classList[0] || ""); sig[k] = (sig[k] || 0) + 1; }
    const top = Object.entries(sig).sort((a, b) => b[1] - a[1])[0];
    if (top && top[1] > 12 && !/^(OPTION|svg|g|rect|line|text|path|circle)\./i.test(top[0])) lists.push({ el: pathOf(el), n: top[1] });
  }
  // 正文区底部的独立说明段落（RJ 10-10：免责/口径/来源说明不占正文行高，并进页头“?”）：main 里最靠下的可见文字块，
  // 不在卡片/面板里、没有可点元素、字号小或带 note/muted/tiny 类，就记一处
  let foot = null;
  { let best = null, bb = -1;
    for (const el of main.querySelectorAll("*")) { if (el.children.length || !vis(el) || !(el.innerText || "").trim()) continue; const b = el.getBoundingClientRect().bottom; if (b > bb) { bb = b; best = el; } }
    if (best) { const blk = best.closest("p, div, section, footer") || best;
      const inCard = blk.closest(".zb-card, .card, .zb-dw, table, .zb-paged, nav, .ph, .zb-hero");
      const hasAct = blk.querySelector && blk.querySelector("a, button, input, select, textarea");
      const fs = parseFloat(getComputedStyle(best).fontSize) || 14;
      if (!inCard && !hasAct && (fs <= 13.5 || /note|muted|tiny|tip|hint/.test(blk.className + " " + best.className))) foot = (blk.innerText || "").trim().slice(0, 40).replace(/\\s+/g, " "); } }
  // 非首页的页头高度（RJ 10-10：实景大横幅只留各岗位首页，其余页面一行紧凑页头）：取 main 里第一个页头/横幅的高度（不含流程步骤条）
  const hd = main.querySelector(".zb-hero, .home-hero, .ph-band > .ph-row, .ph-plain > .ph-row, .ph > .ph-row");
  const headH = hd && vis(hd) ? Math.round(hd.getBoundingClientRect().height) : 0;
  const heroImg = !!main.querySelector(".zb-hero, .home-hero");
  // 卡片：不嵌套在另一张卡片里的 .card / .zb-card / .zb-kpi
  const cards = [...main.querySelectorAll(".card, .zb-card, .zb-kpi, .tl-card, .feed-item, .fi")].filter((el) => vis(el) && !el.parentElement.closest(".card, .zb-card"));
  // 最高的几个区块（主要原因）：main 下第一层到第二层元素按高度排
  const blocks = [];
  const top = main.children.length === 1 ? main.children[0] : main;
  for (const el of [...top.children]) { if (!vis(el)) continue; const r = el.getBoundingClientRect(); blocks.push({ el: name(el), h: Math.round(r.height), txt: (el.querySelector("h2,h3,b,strong")?.innerText || "").slice(0, 16) }); }
  blocks.sort((a, b) => b.h - a.h);
  const tabbar = document.querySelector(".tabbar"); const tbH = tabbar && vis(tabbar) ? Math.round(tabbar.getBoundingClientRect().height) : 0;
  return { W, H, docH, ratio: +(docH / H).toFixed(2), overX, offenders, longs: longs.slice(0, 5), nLong: longs.length, lists: lists.slice(0, 4), nList: lists.length, foot, headH, heroImg, cards: cards.length, blocks: blocks.slice(0, 4), tbH, hash: location.hash };
})()`;

async function settle() {
  await sleep(700);
  let last = "", same = 0;
  for (let i = 0; i < 24; i++) {
    const sig = await ev(`(document.documentElement.scrollHeight) + ":" + ((document.querySelector("main")||document.body).innerText.length) + ":" + document.querySelectorAll(".loading,.spin,.ls-load").length`);
    if (sig === last) { if (++same >= 3) return; } else { same = 0; last = sig; }
    await sleep(250);
  }
}

// 详情页：按岗位能看到的第一条记录打开（接口返回什么就用什么，不造数据）
const DETAILS = [
  { k: "alert-detail", label: "预警详情", api: "/api/alerts", route: "alert", roles: ["manager", "eng_sup", "leader"] },
  { k: "finding-detail", label: "隐患详情", api: "/api/findings", route: "finding", roles: ["tech", "sub_safety", "manager", "eng_sup", "safety_sup"] },
  { k: "log-detail", label: "日志详情", api: "/api/logs", route: "log", roles: ["tech", "eng_sup"] },
  { k: "report-detail", label: "周报详情", api: "/api/reports", route: "report", roles: ["manager", "eng_sup", "safety_sup", "leader"] },
];

// 一档：首页/总览/驾驶舱类（严格一屏）；其余为二档
const TIER1 = new Set(["home", "shome", "work", "eng", "sdash", "sysh", "cockpit", "progress", "output"]);
const tierOf = (k) => (TIER1.has(k) ? 1 : 2);
// 豁免：现场圈按朋友圈方式连续下滑（RJ 10-10），不查分页、不设手机 2 屏上限；电脑端仍须在固定外框内滚动、任何宽度不得横向溢出
const FEED = new Set(["circle"]);
function writeReport(rows) {
// 汇总表：按岗位 + 页面一行，各视口一列
  const vpNames = [...new Set(rows.map((r) => r.vp))];
  const byPage = new Map();
  for (const r of rows) { const key = r.role + "|" + r.k; if (!byPage.has(key)) byPage.set(key, []); byPage.get(key).push(r); }
  const cell = (r) => r ? `${r.pass ? "" : "**"}${r.ratio}${r.overX > 1 ? " ↔" + r.overX : ""}${r.pass ? "" : "**"}` : "";
  const reason = (list) => {
    const a = [];
    const bad = list.filter((r) => !r.pass);
    const x = bad.find((r) => r.overX > 1); if (x) a.push(`横向溢出 ${x.overX}px（${x.vp}）：${x.offenders.map((o) => o.el).slice(0, 2).join("；")}`);
    const y = bad.filter((r) => r.overY).sort((p, q) => q.ratio - p.ratio)[0];
    if (y) a.push(`最高块：${y.blocks.slice(0, 2).map((b) => `${b.el}${b.txt ? "「" + b.txt + "」" : ""} ${b.h}px`).join("、")}`);
    const hb = list.find((r) => r.headBad); if (hb) a.push(`页头过高 ${hb.headH}px${hb.heroImg ? '（带实景横幅）' : ''}（${hb.vp}）`);
    const ft = list.find((r) => r.foot); if (ft) a.push(`底部说明段落「${ft.foot}…」`);
    const pl = list.find((r) => r.nList); if (pl) a.push(`未分页长列表 ${pl.nList} 处（如 ${pl.lists[0].el} ${pl.lists[0].n} 项）`);
    const l = list.find((r) => r.nLong); if (l) a.push(`大段文字 ${l.nLong} 处（如 ${l.longs[0].lines} 行「${l.longs[0].text}…」）`);
    return a.join("；");
  };
  let md = `# 全站一屏化度量\n\n- 实例：${BASE || "合并 " + REPORT}（一次性演示实例） · 生成：${new Date().toISOString().slice(0, 16).replace("T", " ")}\n`;
  md += `- 口径：一档（首页/总览）电脑严格一屏；二档（列表、台账、详情、文书、配置）页头筛选固定、内容在面板内滚动，整页同样 ≤ 1；电脑页面高度 ÷ 视口高 ≤ 1（容差 2px）；手机 ≤ ${MOBILE_MAX}（约 2 屏）；任何宽度不得横向溢出（↔ 为溢出像素）；正文块可见超过 3 行且多于 80 字记为大段文字。\n`;
  md += `- 单元格是“页面高度 ÷ 视口高”，**加粗** = 超标。\n\n`;
  const order = (l) => (tierOf(l[0].k) === 1 ? 0 : 1);
  const fails = [...byPage.values()].filter((l) => l.some((r) => !r.pass) || l.some((r) => r.nLong || r.nList || r.foot)).sort((a, b) => order(a) - order(b));
  const deskFail = rows.filter((r) => !r.mobile && !r.pass).length, mobFail = rows.filter((r) => r.mobile && !r.pass).length;
  md += `合计 ${byPage.size} 个页面 × ${vpNames.length} 档视口：电脑超标 ${deskFail} 项，手机超标 ${mobFail} 项，含大段文字的页面 ${[...byPage.values()].filter((l) => l.some((r) => r.nLong)).length} 个，含未分页长列表的页面 ${[...byPage.values()].filter((l) => l.some((r) => r.nList)).length} 个，底部有说明段落的页面 ${[...byPage.values()].filter((l) => l.some((r) => r.foot)).length} 个。\n\n`;
  md += `## 超标清单\n\n| 岗位 | 页面 | ${vpNames.join(" | ")} | 卡片 | 主要原因 |\n|---|---|${vpNames.map(() => "---").join("|")}|---|---|\n`;
  for (const l of fails) { const f = l[0]; md += `| ${f.roleName} | ${f.line} › ${f.label} | ${tierOf(f.k)} | ${vpNames.map((n) => cell(l.find((r) => r.vp === n))).join(" | ")} | ${Math.max(...l.map((r) => r.cards))} | ${reason(l)} |\n`; }
  md += `\n## 已达标\n\n| 岗位 | 页面 | ${vpNames.join(" | ")} |\n|---|---|${vpNames.map(() => "---").join("|")}|\n`;
  for (const l of byPage.values()) { if (fails.includes(l)) continue; const f = l[0]; md += `| ${f.roleName} | ${f.line} › ${f.label} | ${vpNames.map((n) => cell(l.find((r) => r.vp === n))).join(" | ")} |\n`; }
  fs.writeFileSync(path.join(OUT, "fit.md"), md);
  console.log(`\n电脑超标 ${deskFail} · 手机超标 ${mobFail} → ${path.join(OUT, "fit.md")}`);
  }

if (REPORT) {
  const all = REPORT.split(",").flatMap((f) => JSON.parse(fs.readFileSync(f, "utf8")));
  fs.mkdirSync(OUT, { recursive: true }); writeReport(all); process.exit(0);
}
const rows = [];
for (const u of ROLES) {
  if (onlyRoles && !onlyRoles.includes(u.role)) continue;
  if (!u.id) { console.log("找不到账号", u.user); continue; }
  await cmd("Emulation.setDeviceMetricsOverride", { width: 1366, height: 768, deviceScaleFactor: 1, mobile: false });
  await cmd("Page.navigate", { url: BASE + "/static/favicon.svg" }); await sleep(600);
  await ev(`fetch("/api/dev/login",{method:"POST",headers:{"x-zb":"1","content-type":"application/json"},body:JSON.stringify({user_id:${u.id}})}).then(r=>r.status)`);
  await ev(`localStorage.setItem("zb_dev_user","${u.id}"); sessionStorage.removeItem("zb_no_auto"); 1`);
  await cmd("Page.navigate", { url: BASE + "/" }); await sleep(3000);
  const nav = await ev(`(() => { const r = store.me.user.role; const n = buildNav(r, (store.me.project||{}).features||{}); return [...n.top, ...n.bottom].flatMap(l => l.pages.map(p => ({k:p.k, label:p.label, line:l.name, route:p.route, params:p.params||{}}))); })()`);
  if (!nav) { console.log("no nav for", u.role); continue; }
  const pages = nav.map((p) => ({ ...p, hash: `#/${p.route}${Object.keys(p.params).length ? "?" + new URLSearchParams(p.params) : ""}` }));
  if (!flag("--no-detail")) {
    for (const d of DETAILS) {
      if (!d.roles.includes(u.role)) continue;
      const id = await ev(`fetch(${JSON.stringify(d.api)},{headers:{"x-zb":"1"}}).then(r=>r.ok?r.json():null).then(j=>{const a=Array.isArray(j)?j:(j&&(j.items||j.rows||j.list||j.alerts||j.findings||j.logs||j.reports))||[];const x=a.find(o=>o&&o.id);return x?x.id:null}).catch(()=>null)`);
      if (id) pages.push({ k: d.k, label: d.label, line: "详情", route: d.route, hash: `#/${d.route}?id=${id}` });
    }
  }
  for (const vp of VPS) {
    await cmd("Emulation.setDeviceMetricsOverride", { width: vp.w, height: vp.h, deviceScaleFactor: 1, mobile: vp.mobile });
    await cmd("Emulation.setTouchEmulationEnabled", { enabled: vp.mobile });
    await cmd("Page.navigate", { url: BASE + "/" }); await sleep(2500);
    for (const p of pages) {
      if (onlyPages && !onlyPages.includes(p.k)) continue;
      errs = [];
      await ev(`location.hash = ${JSON.stringify(p.hash)}; window.scrollTo(0,0); 1`);
      await settle();
      await ev(`window.scrollTo(0,0); 1`);
      // --stress：模拟真实项目（SEC1）里的长英文作业名、长编码：给 main 里每个叶子文字块追加一段不带空格的长串，主要看横向溢出
      if (STRESS) { await ev(`(() => { for (const el of (document.querySelector("main")||document.body).querySelectorAll("*")) { if (el.children.length || !el.textContent.trim() || /^(SCRIPT|STYLE|OPTION|TEXTAREA)$/.test(el.tagName) || el.closest(".htip-b")) continue; el.textContent += " SEC1-K12+350_PILE-CAP-CONCRETE-POURING-SECTION-B-ABUTMENT-A3"; } return 1; })()`); await sleep(300); }
      await ev(`window.__fitW = ${vp.w}; 1`);
      const m = await ev(MEASURE);
      if (!m) { console.log("measure failed", u.role, p.k, vp.name); continue; }
      // 手机模拟下内容过宽时浏览器会把布局视口撑宽（整页缩小显示），innerWidth 超出设定宽度的部分同样记为横向溢出
      const iw = await ev("innerWidth");
      if (iw > vp.w) { m.overX = Math.max(m.overX, iw - vp.w); m.ratio = +(m.docH * vp.w / iw / vp.h).toFixed(2); }
      const limit = vp.mobile ? MOBILE_MAX : 1;
      if (FEED.has(p.k)) { m.nList = 0; m.lists = []; }
      const HOME = ["home", "shome", "work", "eng", "sdash", "sysh", "cockpit"];
      m.headBad = !HOME.includes(p.k) && (m.heroImg || m.headH > (vp.mobile ? 156 : 60));   // 非首页：不得有实景横幅，页头 ≤ 60px（手机 156px：标题、元信息各一行，操作最多两行——360 宽放不下分段加按钮时折一次）
      const overY = vp.mobile ? (!FEED.has(p.k) && m.ratio > MOBILE_MAX) : m.docH > m.H + 2;
      const row = { role: u.role, roleName: u.name, k: p.k, label: p.label, line: p.line, hash: p.hash, vp: vp.name, mobile: vp.mobile, ...m, limit, overY, overXFail: m.overX > 1, pass: !overY && !(m.overX > 1) && !m.headBad, errors: errs.slice(0, 3) };
      rows.push(row);
      if (SHOTS) {
        const dir = path.join(OUT, u.role); fs.mkdirSync(dir, { recursive: true });
        const shot = await cmd("Page.captureScreenshot", { format: "jpeg", quality: 72 });
        fs.writeFileSync(path.join(dir, `${vp.name}_${p.k}.jpg`), Buffer.from(shot.result.data, "base64"));
      }
      console.log(`${row.pass ? "ok  " : "FAIL"} ${u.role.padEnd(10)} ${vp.name.padEnd(9)} ${p.k.padEnd(15)} ${String(m.ratio).padStart(5)}x ${m.overX ? "overX=" + m.overX : ""} long=${m.nLong} list=${m.nList}${m.foot ? " foot" : ""} cards=${m.cards}`);
    }
  }
}
fs.writeFileSync(path.join(OUT, "fit.json"), JSON.stringify(rows, null, 1));

writeReport(rows);
ws.close();
if (process.platform === "win32") spawnSync("taskkill", ["/pid", String(edge.pid), "/T", "/F"], { stdio: "ignore" }); else edge.kill();
try { fs.rmSync(udd, { recursive: true, force: true }); } catch (_) {}
process.exit(rows.some((r) => !r.pass) ? 1 : 0);
