/* 智眸工笔 B 版前端：Vue 3（免构建），哈希路由。 */
const { createApp, reactive, ref, computed, onMounted, onUnmounted, watch, nextTick } = Vue;

// ---------------------------------------------------------------------------- 基础
// offline：离线模式（断网时凭本机缓存的上次身份进入，见下方“离线启动”），页头提示，记录先存本机
const store = reactive({ me: null, route: { name: "", params: {} }, toast: null, viewer: null, findingAgent: "", cam: null, offline: false });

function toast(msg, err = false) {
  store.toast = { msg, err };
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (store.toast = null), err ? Math.min(15000, 5000 + 80 * String(msg || "").length) : 2500);  // 长的出错原因（如 AI 调用失败）多留一会儿，读得完
}

// ---------------------------------------------------------------------------- 服务器校正时钟
// 偏差 = 响应头 Date（秒级，取 +500ms 中值）− 本机往返的中点；每次接口返回都校一次，存 localStorage，离线启动也能用。
// serverNow() 返回按服务器校正的当前时间（Date）：离线记录时间、水印时间、相对时间都用它，不信任手机本机时间。
const ZB_CLOCK_KEY = "zb_clock_off";
const zbClock = { off: Number(lsGet(ZB_CLOCK_KEY, 0)) || 0, saved: 0, n: 0 };
zbClock.saved = zbClock.off;
function clockSample(date, t0, t1) {
  const sd = Date.parse(date || ""), rtt = t1 - t0;
  if (!sd || !(rtt >= 0) || (rtt > 3000 && zbClock.n)) return;  // 往返慢的样本误差大，已有样本时不用
  zbClock.off = Math.round(sd + 500 - (t0 + t1) / 2); zbClock.n++;
  if (Math.abs(zbClock.off - zbClock.saved) >= 1000) { zbClock.saved = zbClock.off; lsSet(ZB_CLOCK_KEY, zbClock.off); }
}
function serverNow() { return new Date(Date.now() + zbClock.off); }

// ---------------------------------------------------------------------------- 请求
// 弱网暂存钩子 window.__zbOffline（x_quick.js 提供，可选），在线时不改变行为：
//   pre：发请求前改写为带 client_id 的幂等接口、直接入队（引用了暂存照片），或标记 queue（这条可暂存）；
//   err：网络层失败（fetch 抛 TypeError）或可暂存的请求超时后入队，返回伪响应；ok：成功后通知（整改照片留底，被拒时可“改作随手拍”）。
// 超时只加在可暂存的写请求上，其余请求（含 AI 起草这类慢请求）不变：纯文字 15 秒；上传 20 秒 + 体积 ÷ 50KB/s，最长 90 秒。
// 超时与网络失败同样处理：按原 client_id 入队，补传走幂等接口；服务器其实已处理完的，补传也只拿到同一结果。
const API_T = { text: 15000, upload: 20000, rate: 50 * 1024, max: 90000 };
function apiTimeout(body) {
  if (!(body instanceof FormData)) return API_T.text;
  let n = 0;
  for (const [, v] of body.entries()) n += typeof v === "string" ? v.length : (v && v.size) || 0;
  return Math.min(API_T.max, Math.round(API_T.upload + (n / API_T.rate) * 1000));
}
// 出错信息：detail 可能是文字、校验错误列表，或带 code 的对象（如 422 photo_before_dispatch）
function errInfo(data, status) {
  const d = data && data.detail, obj = d && typeof d === "object" && !Array.isArray(d);
  let msg = obj ? d.msg || d.message || "" : d;
  if (Array.isArray(msg)) msg = msg.map((m) => m.msg).join("；");
  return { msg: msg || `请求失败（${status}）`, code: (data && data.code) || (obj && d.code) || "" };
}
function netError(method, timeout) {
  const e = new Error(String(method).toUpperCase() === "GET" ? "信号不好，请稍后重试" : "信号不好，已保留你填的内容");
  e.offline = true; e.timeout = !!timeout; return e;
}

async function api(method, url, body, opts = {}) {
  const off = !opts.noQueue && typeof window.__zbOffline === "function" ? window.__zbOffline : null;
  let queue = false;
  if (off) {
    const pre = await off("pre", method, url, body);
    if (pre && pre.data !== undefined) return pre.data;
    if (pre && pre.url) { url = pre.url; body = pre.body; }
    queue = !!(pre && pre.queue);
  }
  const logout = String(method).toUpperCase() === "POST" && url === "/api/logout";
  if (logout) localCacheClear();  // 退出登录：本机缓存的身份与业务数据先清掉（离线时点也清）；暂存队列不动
  const init = { method, headers: { "x-zb": "1" }, credentials: "same-origin" };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  // 离线模式下读接口也加超时：连着 Wi-Fi 但服务器不可达时，页面尽快给出“信号不好”，不一直“加载中”
  const ms = opts.timeout || (queue ? apiTimeout(body) : store.offline && String(method).toUpperCase() === "GET" ? OFFLINE_T.get : 0);
  const ctl = ms && typeof AbortController === "function" ? new AbortController() : null;
  if (ctl) init.signal = ctl.signal;
  const timer = ctl ? setTimeout(() => ctl.abort(), ms) : 0, t0 = Date.now();
  let res, data = null;
  try {
    res = await fetch(url, init);
    clockSample(res.headers && res.headers.get("date"), t0, Date.now());
    if (!opts.raw) { try { data = await res.json(); } catch (err) { if (ctl && ctl.signal.aborted) throw err; data = null; } }
  } catch (err) {
    const timedOut = !!(ctl && ctl.signal.aborted);
    if (!(err instanceof TypeError) && !timedOut) throw err;
    const q = off ? await off("err", method, url, body, { timeout: timedOut }) : null;
    if (q && q.data !== undefined) return q.data;
    if (logout) throw new Error("没有网络，联网后才能退出登录");  // 登录凭据在服务器那边作废，离线做不到
    throw netError(method, timedOut);
  } finally { clearTimeout(timer); }
  if (store.offline && !meProbing) setTimeout(() => meRecheck(), 0);  // 离线模式下服务器有了回应：马上确认是否已连回
  if (res.status === 401 && !opts.quiet) { localCacheClear(); store.offline = false; store.me = null; go("login"); throw new Error("请先登录"); }
  if (opts.raw) return res;
  if (!res.ok) {
    const i = errInfo(data, res.status);
    const e = new Error(i.msg); e.status = res.status; e.code = i.code; throw e;
  }
  if (off) { try { off("ok", method, url, body, data); } catch (_) { /* 通知失败不影响结果 */ } }
  return data;
}
const GET = (u, o) => api("GET", u, undefined, o);
const POST = (u, b, o) => api("POST", u, b === undefined ? {} : b, o);
const PUT = (u, b) => api("PUT", u, b);
const DEL = (u) => api("DELETE", u);

// ---------------------------------------------------------------------------- 扩展点（x_*.js 各自注册，不改主文件）
// EXT.route("name", 组件)            新页面，哈希路由 #/name
// EXT.nav(角色或角色数组, {k, route, label, short, icon, line, feat?, after?})  新增导航页面：挂到 nav.js 的某条业务线（site/prog/qs/rep/ai/more…）下，成为该业务线页面上方的一个页签，
//   不会成为新的一级菜单；feat 填增强能力开关键（项目设置里可隐藏）。内置页面的业务线配置集中在 nav.js 的 NAV_LINES / NAV_PAGES。
// EXT.slot("top:work", 组件, {order, id, after})
//                                    在某页面顶部/底部插入组件：top:<路由名> / bottom:<路由名>；"top:*" 每个页面顶部；"askbar" 问工笔条旁；"global" 全局常驻；页面也可自带内部插槽（如 shome:due）
//   第三个参数可选：order 数值越小越靠前（默认 0，同值按注册先后）；id 给本条起名；after 填同一插槽内另一条的 id，紧跟其后。不传则与以前完全一致。
// EXT.alias("name", "导航高亮的路由")  子页面归属哪个导航项
// EXT.feed("work" 或 ["work","eng","sdash"], provider)  向岗位首页“需要关注”区的“催办与固定的提问”折叠块提供条目（不再往首页插独立卡片；要我办的事走后端 /api/todo 待办表，不走这里）。
//   provider() 返回（或 resolve 为）数组，条目：{agent, level: red|orange|yellow|info, title, basis, actions: [{label, route, params} | {label, api: {method, url, body}, confirm?}], at}
//   TodoAttention 汇总后按 level、at 排序；无条目整块不显示。provider 抛错或返回空都不影响其它来源。
const EXT = {
  routes: {}, slots: {}, aliases: {}, navPages: [], feeds: {},
  feed(homes, provider) { [].concat(homes).forEach((h) => (this.feeds[h] = this.feeds[h] || []).push(provider)); },
  async collect(home) {
    const rs = await Promise.allSettled((this.feeds[home] || []).map((p) => Promise.resolve().then(p)));
    const rk = { red: 0, orange: 1, yellow: 2, info: 3 };
    return rs.flatMap((r) => (r.status === "fulfilled" && Array.isArray(r.value) ? r.value : [])).filter((i) => i && i.title)
      .map((i, n) => ({ ...i, level: rk[i.level] === undefined ? "info" : i.level, _n: n }))
      .sort((a, b) => rk[a.level] - rk[b.level] || String(b.at || "").localeCompare(String(a.at || "")) || a._n - b._n);
  },
  route(name, comp) { this.routes[name] = comp; },
  slot(name, comp, o = {}) { (this.slots[name] = this.slots[name] || []).push({ comp, order: Number(o.order) || 0, id: o.id || "", after: o.after || "" }); },
  // 某插槽按 order → 注册先后排好的组件列表；after 指向本插槽内存在的 id 时，紧跟该条（锚点不存在或成环则退回按 order 的位置）
  // "top:*" 是所有页面顶部共用的插槽（如离线模式提示），排在各页面自己的 top 插槽之前参与排序
  slotList(name) {
    const all = [...(/^top:(?!\*$)/.test(name) ? this.slots["top:*"] || [] : []), ...(this.slots[name] || [])];
    const es = all.map((e, n) => ({ ...e, n })).sort((a, b) => a.order - b.order || a.n - b.n);
    const anchored = (e) => e.after && es.some((x) => x !== e && x.id === e.after);
    const out = es.filter((e) => !anchored(e));
    let rest = es.filter(anchored);
    while (rest.length) {
      const next = [];
      for (const e of rest) {
        let i = out.findIndex((x) => x.id === e.after);
        if (i < 0) { next.push(e); continue; }
        while (out[i + 1] && out[i + 1].after === e.after) i++;
        out.splice(i + 1, 0, e);
      }
      if (next.length === rest.length) { out.push(...next); break; }
      rest = next;
    }
    return out.map((e) => e.comp);
  },
  alias(name, to) { this.aliases[name] = to; },
  nav(role, page) { this.navPages.push({ short: "", icon: "layers", ...page, roles: role === "*" ? ["*"] : [].concat(role) }); },
};
const ExtSlot = {
  props: ["name"],
  template: `<component v-for="(c, i) in list" :is="c" :key="name + i"></component>`,
  computed: { list() { return EXT.slotList(this.name); } },
};

function go(name, params = {}) {
  const q = Object.entries(params).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  location.hash = `#/${name}${q ? "?" + q : ""}`;
}
function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const [name, qs] = h.split("?");
  const params = {};
  (qs || "").split("&").filter(Boolean).forEach((p) => { const [k, v] = p.split("="); params[k] = decodeURIComponent(v || ""); });
  store.route = { name: name || "", params };
}
window.addEventListener("hashchange", parseRoute);

const can = (cap) => !!(store.me && store.me.can && store.me.can.includes(cap));
const roleOf = () => (store.me ? store.me.user.role : "");
const ORG_NAME = { sub: "分包单位", harbour: "港湾", system: "系统" };
const fmtDT = (s) => (s ? s.replace("T", " ").slice(0, 16) : "");
const fmtN = (v, d = 1) => (v === null || v === undefined || v === "" ? "—" : (+v).toFixed(d).replace(/\.0+$/, ""));
const pct = (v) => (v === null || v === undefined ? "—" : `${fmtN(v * 100)}%`);
const today = () => (store.me ? store.me.today : new Date().toISOString().slice(0, 10));
const LEVEL = { red: "红色", orange: "橙色", yellow: "黄色" };
const STATUS = { draft: "草稿", submitted: "待审核", approved: "已审核", returned: "已退回", none: "未填" };
const STATUS_CHIP = { draft: "", submitted: "blue", approved: "green", returned: "red", none: "orange" };
const ALERT_STATUS = { open: "待处理", confirmed: "已确认", false_alarm: "误报", closed: "已关闭" };
const ISSUE_TYPES = ["安全", "质量", "材料", "设备", "人员", "天气", "图纸", "外部", "其他"];
const SRC_NAME = { V: "口述", PLAN: "P6计划", Y: "昨日日志", X: "Excel 日报" };
const clone = (o) => JSON.parse(JSON.stringify(o));
function lsGet(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* 隐私模式忽略 */ } }
function lsDel(k) { try { localStorage.removeItem(k); } catch (_) { /* ignore */ } }

// ---------------------------------------------------------------------------- 日志栏目定义
const SECTIONS = [
  { key: "works", title: "施工内容", cols: [
    { k: "part", label: "部位", w: 110 }, { k: "process", label: "工序", w: 120 }, { k: "desc", label: "内容", grow: true },
    { k: "activity_code", label: "P6作业", type: "act", w: 130 }] },
  { key: "progress", title: "完成工程量（回填P6进度）", cols: [
    { k: "activity_code", label: "P6作业", type: "act", grow: true }, { k: "qty", label: "今日完成量", type: "num" },
    { k: "unit", label: "单位", w: 60 }, { k: "pct", label: "累计%", type: "num" }, { k: "note", label: "备注", w: 120 }] },
  { key: "labor", title: "人员投入", cols: [{ k: "trade", label: "工种", grow: true }, { k: "count", label: "人数", type: "num" }] },
  { key: "machinery", title: "机械投入", cols: [{ k: "name", label: "机械", grow: true }, { k: "count", label: "台数", type: "num" }, { k: "status", label: "状态", w: 80 }] },
  { key: "materials", title: "材料", cols: [{ k: "name", label: "材料", grow: true }, { k: "action", label: "类别", w: 70 }, { k: "qty", label: "数量", type: "num" }, { k: "unit", label: "单位", w: 60 }] },
  { key: "issues", title: "质量安全及问题", cols: [
    { k: "type", label: "类型", type: "sel", opts: ISSUE_TYPES, w: 76 }, { k: "severity", label: "等级", type: "sel", opts: ["高", "中", "低"], w: 60 },
    { k: "desc", label: "问题", grow: true }, { k: "measure", label: "措施", w: 160 }, { k: "activity_code", label: "影响作业", type: "act", w: 120 }] },
  { key: "tomorrow", title: "次日计划", cols: [{ k: "desc", label: "计划", grow: true }, { k: "activity_code", label: "P6作业", type: "act", w: 130 }] },
];
function blankRow(sec) {
  const r = { sources: [] };
  sec.cols.forEach((c) => (r[c.k] = c.type === "num" ? null : c.type === "sel" ? c.opts[0] : ""));
  if (sec.key === "issues") { r.type = "其他"; r.severity = "中"; }
  return r;
}

// ---------------------------------------------------------------------------- 通用组件
const SrcChips = {
  props: ["sources", "photos"],
  template: `<span><span v-for="s in sources || []" :key="s" :class="['src', s.startsWith('P') && s !== 'PLAN' ? '' : s]"
      :title="title(s)" @click.stop="open(s)">{{ s === 'PLAN' ? '计划' : s === 'V' ? '口述' : s === 'Y' ? '昨日' : s === 'N' ? '随手记' : s === 'X' ? 'Excel' : s }}</span></span>`,
  methods: {
    title(s) { return SRC_NAME[s] || (/^D\d/.test(s) ? `本周第${s.slice(1)}份每日日志` : s === "N" ? "随手记原文" : `照片 ${s}`); },
    open(s) { const i = /^P(\d+)$/.exec(s); if (i && this.photos && this.photos[+i[1] - 1]) store.viewer = this.photos[+i[1] - 1].url; },
  },
};

const LevelChip = { props: ["level"], template: `<span :class="['chip', level]">{{ LEVEL[level] || level }}</span>`, data: () => ({ LEVEL }) };

// ---------------------------------------------------------------------------- 登录
const Login = {
  // V8 品牌（10-10）：中港蓝底 + 装饰性港口背景（非现场证据）；产品标志沿用 brand/logo-eye.png，不用生成图里的企业标志；文化“包容 诚信 创新 奉献”
  template: `<div class="login-wrap ra-login"><div class="ra-lg-brand" aria-hidden="true"><div class="ra-lg-mark"><img src="/static/brand/logo-eye.png" alt=""><div><b>智眸·工笔</b><small>施工现场 AI 助手</small></div></div>
      <div class="ra-lg-org">中国港湾 CHEC</div><div class="ra-lg-cul">包容 诚信 创新 奉献</div></div>
    <form class="login" @submit.prevent="submit">
    <div class="ra-lg-h"><img class="ra-lg-logo" src="/static/brand/logo-eye.png" alt=""><div><h1>智眸·工笔</h1><div class="sub">分包现场记录 · 港湾协同管控</div></div></div>
    <div class="field"><label class="f">账号</label><input class="in" v-model.trim="u" autocomplete="username" autofocus></div>
    <div class="field"><label class="f">密码</label><input class="in" type="password" v-model="p" autocomplete="current-password"></div>
    <button class="btn primary big block mt2" :disabled="busy">{{ busy ? '登录中' : '登录' }}</button>
    <div v-if="dev.length" class="mt2 devbox">
      <div class="tiny muted" style="margin-bottom:8px">开发态快捷登录（ZB_DEV_LOGIN=1）</div>
      <div v-for="g in devGroups" :key="g.org" class="devgrp"><div class="tiny muted">{{ ORG_NAME[g.org] }}</div>
        <div class="row wrap" style="gap:6px"><button type="button" :class="['btn', 'rolebtn', 'r-' + a.role]" v-for="a in g.items" :key="a.id" :disabled="busy" @click="quick(a)"
          :title="a.username + (a.company ? ' · ' + a.company : '')"><b>{{ a.role_name }}</b><span class="tiny muted">{{ a.name }}</span></button></div></div></div>
    <div class="tiny muted mt" style="text-align:center">内部数据仅经中国交建交融大模型处理</div><div class="ra-lg-cul2">包容 诚信 创新 奉献</div></form></div>`,
  data: () => ({ u: "", p: "", busy: false, dev: [], ORG_NAME }),
  computed: { devGroups() { return ["sub", "harbour", "system"].map((org) => ({ org, items: this.dev.filter((a) => a.org === org) })).filter((g) => g.items.length); } },
  async created() {
    try { this.dev = await GET("/api/dev/accounts", { quiet: true }); } catch { this.dev = []; }
    // 开发态免登录：自动以上次选的账号（默认第一个，即管理员）进入；点「退出登录」后停止自动登录，直到再点一次角色按钮。
    let skip = false, last = "";
    try { skip = !!sessionStorage.getItem("zb_no_auto"); last = localStorage.getItem("zb_dev_user") || ""; } catch (_) { /* 无存储时照常自动登录 */ }
    if (this.dev.length && !skip) this.quick(this.dev.find((a) => String(a.id) === last) || this.dev[0]);
  },
  methods: {
    async quick(a) {
      this.busy = true;
      try {
        await POST("/api/dev/login", { user_id: a.id }); await loadMe();
        try { sessionStorage.removeItem("zb_no_auto"); localStorage.setItem("zb_dev_user", String(a.id)); } catch (_) { /* 忽略 */ }
        go(homeOf());
      } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
    async submit() {
      this.busy = true;
      // quiet：密码错误显示“用户名或密码错误”，不当成登录过期
      try { await POST("/api/login", { username: this.u, password: this.p }, { quiet: true }); await loadMe(); go(homeOf()); }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

// ---------------------------------------------------------------------------- 离线启动（T-10c，P0-1 AC-5）
// 每次在线取到 /api/me，在本机留一份“上次的身份”：只有显示身份与导航要用的字段，不含账号名、邮箱、令牌（令牌在 httponly cookie 里，脚本读不到）。
// 断网（或服务器不可达）时打开系统：本机有这份缓存 = 这台手机登录过，凭它进入“离线模式”——页头提示，随手记照常记、先存本机暂存队列；
// 没有缓存（没登录过、已退出、登录已过期）就回登录页。退出登录、接口返回 401 时清掉缓存。
// 离线模式下每 15 秒（或来网、或任一接口有回应时）重新取 /api/me：取到就退出离线模式，并发出 online 事件让暂存队列补传、首页重新拉取。
const ME_CACHE_KEY = "zb_me_offline";
const OFFLINE_T = { me: 8000, get: 8000, probe: 15000 };  // 有缓存时 /api/me 最多等 8 秒；离线模式下读接口超时；探测间隔
const ME_CFG_KEYS = ["simulation", "social_enabled"];      // 项目配置里前端导航/角标要用的开关
let meProbing = false, meProbeT = 0;
// 退出登录、登录过期（401）时清掉的本机缓存：上次的身份，以及前面各页留在本机的业务数据（首页、整改单、整改单列表、水印上填的部位与作业）。
// 不清：暂存队列（IndexedDB zb_quick，没上传的记录按人隔离，原用户再登录时补传）、时钟偏差、导航记忆、拍照识别偏好这类不含业务内容的设置。
const LOCAL_DATA_PREFIXES = [ME_CACHE_KEY, "zb_home_cache_", "zb_fcache_", "zb_tasks_", "zb_wm_place_", "zb_wm_work_"];
function localCacheClear() {
  try {
    const ks = [];
    for (let i = 0; i < localStorage.length; i++) ks.push(localStorage.key(i));
    ks.filter((k) => k && LOCAL_DATA_PREFIXES.some((p) => k.startsWith(p))).forEach(lsDel);
  } catch (_) { /* 无存储时没有可清的 */ }
}
function meSlim(me) {
  const u = me.user || {}, p = me.project, cfg = (p && p.config) || {};
  return {
    user: { id: u.id, name: u.name, role: u.role, role_name: u.role_name, org: u.org, company: u.company, wbs_scope: u.wbs_scope },
    can: me.can || [], home: me.home || "",
    project: p ? { id: p.id, name: p.name, config: Object.fromEntries(ME_CFG_KEYS.filter((k) => k in cfg).map((k) => [k, cfg[k]])), features: p.features || {} } : null,
    ai: me.ai ? { provider: me.ai.provider, ready: !!me.ai.ready, asr: !!me.ai.asr } : null,
    version: me.version || "", at: Date.now(),
  };
}
const localDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
function meOffline(c) {
  return { ...c, ai: c.ai || { provider: "", ready: false, asr: false }, projects: [], unread: 0, p6: null, today: localDay(serverNow()), offline: true };
}
async function loadMe() {
  const cached = lsGet(ME_CACHE_KEY, null);
  let me;
  try { me = await GET("/api/me", { quiet: true, timeout: cached ? OFFLINE_T.me : 0 }); }
  catch (e) {
    if (e.status === 401) localCacheClear();
    if (!e.offline || !cached || !cached.user) throw e;
    if (!(store.offline && store.me)) store.me = meOffline(cached);  // 已在离线模式：不换对象，页面不重绘
    store.offline = true; offlineProbe();
    return;
  }
  clearTimeout(meProbeT);
  store.me = me; store.offline = false;
  lsSet(ME_CACHE_KEY, meSlim(me));
}
function offlineProbe() { clearTimeout(meProbeT); if (store.offline) meProbeT = setTimeout(() => meRecheck(), OFFLINE_T.probe); }
async function meRecheck(fromEvent) {
  if (!store.offline || meProbing) return;
  meProbing = true;
  try {
    await loadMe();
    if (!store.offline && !fromEvent) window.dispatchEvent(new Event("online"));  // 浏览器没报“来网”（连着 Wi-Fi、服务器刚恢复）：补发一次
  } catch (e) {
    if (e.status === 401) { store.offline = false; store.me = null; go("login"); }  // 登录已过期：暂存队列保留，重新登录后补传
  } finally { meProbing = false; offlineProbe(); }
}
window.addEventListener("online", () => meRecheck(true));

// 页头提示（所有页面顶部共用插槽）：只在离线模式下出现。
// 手机上暂存条（x_quick 的 .qbar，浮在右上角）会压住第一行右半边：标题单独一行靠左，说明放第二行，不被挡住
const OfflineBanner = {
  name: "OfflineBanner",
  template: `<div class="banner info zb-offline" role="status" v-if="store.offline"><b style="display:block">离线模式：</b>记录会先存在本机，联网后自动上传</div>`,
  data: () => ({ store }),
};
EXT.slot("top:*", OfflineBanner, { order: -1000 });

// Service Worker（T-10b 的 /sw.js）：只缓存应用外壳，不缓存 /api；只能在安全上下文（HTTPS 或本机）注册
if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch((e) => console.warn("SW 注册失败", e));
  });
}


// ---------------------------------------------------------------------------- 角色与格式
const isLeader = () => store.me && store.me.user.role === "leader";
const isTech = () => store.me && store.me.user.role === "tech";
function homeOf() { return (store.me && store.me.home) || "work"; }
const isSubOrg = () => !!(store.me && store.me.user.org === "sub");
const isSim = () => !!(store.me && store.me.project && store.me.project.config && store.me.project.config.simulation);
function money(v, cur) {
  if (v === null || v === undefined) return "—";
  const a = Math.abs(v);
  const s = a >= 1e8 ? (v / 1e8).toFixed(2) + "亿" : a >= 1e4 ? (v / 1e4).toFixed(1) + "万" : (+v).toFixed(0);
  return s + (cur ? " " + cur : "");
}
const probClass = (p) => (p === null || p === undefined ? "" : p >= 0.8 ? "green" : p >= 0.6 ? "yellow" : "red");
const probText = (p) => (p === null || p === undefined ? "—" : `${Math.round(p * 100)}%`);
const LEVEL_F = { high: "red", medium: "orange", low: "yellow", info: "blue" };

// ---------------------------------------------------------------------------- 图表（内联 SVG：按实际宽度绘制；柱=实际，虚线=计划；颜色由 CSS 变量 --bc-* 控制）
const BarChart = {
  props: { items: Array, height: { type: Number, default: 180 }, fmt: { type: Function, default: (v) => fmtN(v) }, dark: Boolean, target: String, actual: String },
  template: `<div class="bc" ref="box">
    <div class="bc-legend"><span><i class="k bar"></i>{{ actual || '实际' }}</span><span><i class="k line"></i>{{ target || '计划' }}</span></div>
    <div class="bc-wrap" :style="{ height: height + 'px' }" @mouseleave="hover = -1">
      <svg :width="w" :height="height" role="img">
        <g class="bc-grid"><template v-for="t in ticks" :key="t.v">
          <line :x1="padL" :x2="w - padR" :y1="t.y" :y2="t.y" :class="t.v ? 'g' : 'base'"/>
          <text :x="padL - 8" :y="t.y + 4" text-anchor="end">{{ axis(t.v) }}</text></template></g>
        <g v-for="(it, i) in items" :key="i">
          <rect v-if="hover === i" :x="slotX(i)" :y="padT" :width="slot" :height="plotH" class="bc-hl"/>
          <path v-if="it.value" :d="barPath(i, it.value)" :class="['bc-bar', it.muted ? 'muted' : '']"/>
          <text v-if="showLabel(i)" :x="slotX(i) + slot / 2" :y="height - 6" text-anchor="middle" class="bc-x">{{ it.label }}</text>
          <rect :x="slotX(i)" :y="0" :width="slot" :height="height" fill="transparent" @mouseenter="hover = i" @click="hover = i"/>
        </g>
        <polyline v-if="plan.length > 1" :points="plan.map((p) => p.join(',')).join(' ')" class="bc-plan"/>
        <circle v-for="(p, i) in plan" :key="'p' + i" :cx="p[0]" :cy="p[1]" r="2.5" class="bc-pt"/>
      </svg>
      <div v-if="hover >= 0 && items[hover]" class="bc-tip" :style="tipStyle">
        <b>{{ items[hover].label }}</b>
        <div><span>{{ items[hover].name || actual || '实际' }}</span>{{ items[hover].value === null || items[hover].value === undefined ? '无数据' : fmt(items[hover].value) }}</div>
        <div v-if="items[hover].target"><span>{{ target || '计划' }}</span>{{ fmt(items[hover].target) }}</div>
        <div v-if="items[hover].target && items[hover].value" class="rate"><span>完成率</span>{{ Math.round(items[hover].value / items[hover].target * 100) }}%</div>
      </div>
    </div>
  </div>`,
  data: () => ({ hover: -1, w: 600, padL: 46, padR: 8, padT: 10 }),
  computed: {
    padB() { return 22; },
    plotW() { return Math.max(40, this.w - this.padL - this.padR); },
    plotH() { return this.height - this.padT - this.padB; },
    slot() { return this.plotW / Math.max(1, this.items.length); },
    bw() { return Math.min(26, Math.max(3, this.slot * 0.56)); },
    step() { return Math.max(1, Math.ceil(this.items.length / Math.max(1, Math.floor(this.plotW / 58)))); },
    max() {
      const m = Math.max(1, ...this.items.map((i) => Math.max(i.value || 0, i.target || 0)));
      const raw = m / 3, p = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / p;
      return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p * 3;
    },
    ticks() { return [0, 1, 2, 3].map((k) => ({ v: this.max / 3 * k, y: this.y(this.max / 3 * k) })); },
    plan() { return this.items.map((it, i) => (it.target ? [this.slotX(i) + this.slot / 2, this.y(it.target)] : null)).filter(Boolean); },
    tipStyle() {
      const cx = this.slotX(this.hover) + this.slot / 2, flip = cx > this.w * 0.62;
      return flip ? { right: Math.max(0, this.w - cx + 10) + "px", top: "4px" } : { left: cx + 10 + "px", top: "4px" };
    },
  },
  mounted() {
    const el = this.$refs.box, set = () => { if (el && el.clientWidth) this.w = el.clientWidth; };
    set();
    if (window.ResizeObserver) { this._ro = new ResizeObserver(set); this._ro.observe(el); }
  },
  beforeUnmount() { if (this._ro) this._ro.disconnect(); },
  methods: {
    axis(v) { return v ? money(v, "").replace(/\.0+(?=[万亿]|$)/, "") : "0"; },
    slotX(i) { return this.padL + i * this.slot; },
    y(v) { return this.padT + this.plotH - (v / this.max) * this.plotH; },
    showLabel(i) { return (this.items.length - 1 - i) % this.step === 0; },
    barPath(i, v) {
      const x = this.slotX(i) + (this.slot - this.bw) / 2, w = this.bw, base = this.padT + this.plotH, y = this.y(v), r = Math.min(3, w / 2, base - y);
      return `M${x},${base} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${base} Z`;
    },
  },
};

// ---------------------------------------------------------------------------- 工程量可信分级：自报（分包提交、待港湾审核）/ 核定（港湾已审核）
// 小标签只表示“谁认的”，合计口径不变（自报 + 核定）。悬停显示来源日志，点击打开第一份来源日志。
const TrustTag = {
  props: { kind: String, ids: { type: Array, default: () => [] } },
  template: `<span :class="['trust-tag', kind === 'approved' ? 'appr' : 'self', ids.length ? 'link' : '']" :title="tip" @click.stop="open">{{ kind === 'approved' ? '核定' : '自报' }}</span>`,
  computed: {
    tip() {
      const base = this.kind === "approved" ? "核定：港湾已审核通过的数量" : "自报：分包提交、待港湾审核的数量";
      return this.ids.length ? `${base}
来源日志：${this.ids.map((i) => "L" + i).join("、")}（点击打开第一份）` : base;
    },
  },
  methods: { open() { if (this.ids.length) go("log", { id: this.ids[0] }); } },
};
// P6 作业：显示简短中文名（接口出口补的 short_name / activity_short，见 app/services/act_names.py），代码与 P6 原名放悬停
const ActName = {
  props: { c: String, n: String, s: String },
  template: `<span class="act-name" :title="tip">{{ s || n || c }}</span>`,
  computed: { tip() { return [this.c, this.n && this.n !== this.s ? this.n : ""].filter(Boolean).join(" · "); } },
};
// 一个数字的自报/核定拆分：两层都没有数量时不显示（不补零）
const TrustSplit = {
  components: { TrustTag },
  props: { self: { default: null }, approved: { default: null }, selfIds: { type: Array, default: () => [] }, approvedIds: { type: Array, default: () => [] },
           fmt: { type: Function, default: (v) => fmtN(v, 2) }, unit: { type: String, default: "" } },
  template: `<span class="trust-split" v-if="self || approved"><span class="ts-i"><trust-tag kind="self" :ids="selfIds"></trust-tag>{{ fmt(self || 0) }}{{ unit }}</span>
    <span class="ts-i"><trust-tag kind="approved" :ids="approvedIds"></trust-tag>{{ fmt(approved || 0) }}{{ unit }}</span></span>`,
};
// 日志详情：已计入的工程量（自报/核定）与被退回失效的原数量（留痕，不计入）
const LogProgressTrust = {
  components: { TrustTag },
  props: { log: Object },
  template: `<div v-if="pr && (pr.active.length || pr.voided.length)" class="trust-box">
    <div v-if="pr.active.length && counted"><b>已计入统计的工程量</b>
      <div class="small" v-for="(r, i) in pr.active" :key="'a' + i"><trust-tag :kind="log.status === 'approved' ? 'approved' : 'self'" :ids="[log.id]"></trust-tag>
        <act-name :c="r.activity_code" :s="r.activity_short"></act-name> <template v-if="r.qty !== null">{{ fmtN(r.qty, 2) }}{{ r.unit }}</template><template v-else>仅填累计%</template></div></div>
    <div v-if="pr.voided.length" :class="{ mt: pr.active.length && counted }"><b>已退回，原数量（不计入）</b>
      <div class="small" v-for="(r, i) in pr.voided" :key="'v' + i"><act-name :c="r.activity_code" :s="r.activity_short"></act-name> <template v-if="r.qty !== null">{{ fmtN(r.qty, 2) }}{{ r.unit }}</template><template v-else>仅填累计%</template>
        <span class="muted">· {{ r.void_reason }}</span></div></div></div>`,
  computed: { pr() { return this.log && this.log.progress_rows; }, counted() { return ["submitted", "approved"].includes(this.log.status); } },
  methods: { fmtN },
};
// 仿真项目角标统一样式见 x_ui.js 的 SimBadge；只在侧栏项目名下显示一次（手机顶栏用“仿真”小标），页头不再重复
const SimBanner = { template: `<div class="sim-strip" v-if="isSim()"><sim-badge></sim-badge></div>`, methods: { isSim } };

// ---------------------------------------------------------------------------- 语音输入（交融 ASR，降级为输入法语音）
// mode="note"（技术员随手记）：录完立即交出录音（@audio：blob、录音时间、时长），由暂存队列上传、服务器后台转写，人不用等；
// 其余位置（提问、纪要、隐蔽验收口述、日志补充）仍同步转写，拿到文字再继续（@text）。
const VoiceButton = {
  emits: ["text", "audio"],
  props: ["big", "mode"],
  template: `<div :class="['voice', big ? 'big' : '']">
    <button type="button" :class="['mic', rec ? 'on' : '', busy ? 'busy' : '']" :title="hint" @click="toggle" :disabled="busy">
      <svg v-if="!rec && !busy" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>
      <span class="mic-i" v-else>{{ busy ? '…' : '■' }}</span><span class="mic-t">{{ label }}</span></button>
    <div class="small muted" v-if="big">{{ hint }}</div>
    <input ref="file" type="file" accept="audio/*" capture hidden @change="pick"></div>`,
  data: () => ({ rec: null, chunks: [], busy: false, sec: 0, t: null, at: null }),
  computed: {
    label() { return this.busy ? "转写中" : this.rec ? `停止 ${this.sec}s` : this.big ? "说一句" : "语音"; },
    can() { return window.isSecureContext && navigator.mediaDevices && window.MediaRecorder; },
    asr() { return store.me && store.me.ai && store.me.ai.asr; },
    hint() {
      if (!this.asr) return this.mode === "note" ? "点一下开始说：录音先保存，配置语音识别后再转成文字" : "可使用输入法语音输入";
      if (this.busy) return "转写中";
      if (this.rec) return `录音中 ${this.sec}s，点击结束`;
      if (this.mode === "note") return this.can ? "点一下开始说，说完再点一下就记下了" : "点一下用系统录音机录，录完就记下了";
      return this.can ? "点击开始录音" : "点击录音（系统录音机）";
    },
  },
  methods: {
    async toggle() {
      if (!this.asr && this.mode !== "note") { toast("未配置语音识别模型，请使用输入法语音"); return; }  // 随手记的录音照样保存，事后再转写
      if (!this.can) { this.$refs.file.click(); return; }
      if (this.rec) { this.rec.stop(); return; }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        this.chunks = []; this.sec = 0;
        const r = new MediaRecorder(stream);
        r.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
        r.onstop = () => { stream.getTracks().forEach((t) => t.stop()); clearInterval(this.t); this.rec = null; this.send(new Blob(this.chunks, { type: r.mimeType || "audio/webm" })); };
        r.start(); this.rec = r; this.at = serverNow(); this.t = setInterval(() => { this.sec++; if (this.sec >= 180) r.stop(); }, 1000);
      } catch (e) { toast("无法使用麦克风：" + e.message, true); }
    },
    pick(ev) {
      const f = ev.target.files[0]; ev.target.value = ""; if (!f) return;
      // 系统录音机交回的文件：文件时间是录完那一刻，按服务器时钟校正；不是刚录的就按现在算
      this.sec = 0; this.at = f.lastModified && Math.abs(Date.now() - f.lastModified) < 1800000 ? new Date(f.lastModified + zbClock.off) : serverNow();
      this.send(f, f.name);
    },
    async send(blob, name) {
      if (this.mode === "note") {
        this.$emit("audio", { blob, name: name || (blob.type.includes("mp4") ? "voice.m4a" : "voice.webm"), mime: blob.type || "", seconds: this.sec || null, recordedAt: this.at || serverNow() });
        this.at = null; return;
      }
      this.busy = true;
      try {
        const fd = new FormData(); fd.append("file", blob, name || (blob.type.includes("mp4") ? "voice.m4a" : "voice.webm"));
        const r = await POST("/api/asr", fd); this.$emit("text", r.text);
      } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};
