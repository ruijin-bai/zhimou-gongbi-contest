/* 根组件。导航由 nav.js 的集中配置生成：一级菜单 = 业务线（≤6 条，各带一行副标题），子页面是页面上方的页签；岗位只做过滤，增强能力可在项目设置里隐藏。 */

// ---------------------------------------------------------------------------- 加载与出错状态（S-1，各页面复用）
// 用法：<load-state :loading="!d && !err" :error="err" @retry="load"></load-state>
// - error：api() 抛出的 Error（带 status / offline）或一句话；有 error 时显示可读原因和“重试”，优先于 loading；
//   不写 @retry 时“重试”重新载入整个应用；
// - loading：显示“加载中”（text 可改写）；两者都没有时渲染默认插槽，可以包住页面内容，也可以只放在 v-else 分支里；
// - compact：嵌在卡片、列表、展开区里的小号样式。
function errText(e) {
  if (!e) return "";
  if (typeof e === "string") return e;
  if (e.offline) return "信号不好，没连上服务器。网络恢复后点“重试”。";
  if (e.status >= 500) return `服务器暂时出错（${e.status}），稍后点“重试”。`;
  return e.message || "没有加载出来";
}
const LoadState = {
  props: { loading: Boolean, error: null, text: String, title: String, compact: Boolean, onRetry: Function },
  template: `<div v-if="error" :class="['es', 'ls-err', {'es-c': compact}]" role="alert"><div class="es-t" v-if="!compact">{{ title || '没有加载出来' }}</div>
      <div class="es-r">{{ reason }}</div>
      <div class="es-a"><button type="button" :class="['btn', 'primary', {sm: compact}]" @click="retry">重试</button></div></div>
    <div v-else-if="loading" :class="['ls-load', compact ? 'tiny muted' : 'empty']" role="status">{{ text || '加载中' }}</div>
    <slot v-else></slot>`,
  computed: { reason() { return errText(this.error); } },
  methods: { retry() { if (this.onRetry) this.onRetry(); else location.reload(); } },
};

// 全局兜底：页面里没接住的报错（加载失败、渲染出错）不再白屏或一直“加载中”，在页面顶部给出原因和“重试”（重新打开本页）；
// 按钮点击里的报错只弹提示，不占页面；401 已由 api() 转到登录页，不再提示。换页即清掉。
const PAGE = reactive({ err: null, n: 0 });
function pageError(e, onClick = false) {
  if (!e || e.status === 401 || !store.me) return;
  console.error(e);
  if (onClick) toast(errText(e), true);
  else PAGE.err = errText(e) || "页面出错";
}
function installErrorHandling(app) {
  // 生产版 Vue 的 info 是错误类型链接：#runtime-5 / #runtime-6 为原生 / 组件事件处理函数
  app.config.errorHandler = (err, _vm, info) => pageError(err, /#runtime-[56]$/.test(String(info || "")));
  window.addEventListener("unhandledrejection", (ev) => { if (ev.reason && ev.reason.status !== 401) { ev.preventDefault(); pageError(ev.reason); } });
}
watch(() => store.route, () => { PAGE.err = null; });

// 项目经理只关注进度、质量、安全，不看日志与周确认（F-14）：这些页面即使直达也不打开，日志详情（周报引用、现场圈链接）仍可看，字段由后端裁剪。
// 催办台（chase）也不进：项目经理只拍板（D7、AC-6.7），地址栏直接输入 #/chase 同样拦下，后端催办台接口对项目经理返回 403（D-7）
const PM_OFF_ROUTES = ["logs", "week", "capture", "manual", "chase"];
const pmOutOfScope = (role, name) => role === "manager" && PM_OFF_ROUTES.includes(name);
const pmOutReason = (name) => name === "chase" ? "超期事项的逐级催办由工程主管、安全主管跟进；升级到你的事项在智能管理台的待办里。"
  : "施工日志与周确认由工程主管跟进；智能管理台可以看进度、质量、安全。";

// 报表（RJ 10-09）：日报（施工日志）、周报、月报合在“工笔文书 · 报表”一页，上方切换。各分页沿用原地址，书签与通知里的链接照常打开：
// 日报 #/logs；技术员的周报就是周进度确认 #/week（#/capture）；其他岗位的周报 / 月报 #/rpt?seg=week|month，旧的 #/reports?kind=week|month 也打开这里。
// 项目经理不看日志（AGENTS.md），领导、安全主管不审日志：这三个岗位只有周报、月报。
function rptSegs(role) {
  const out = [];
  if (role === "tech" || role === "eng_sup") out.push({ k: "day", label: "日报", route: "logs", params: {} });
  out.push(role === "tech" ? { k: "week", label: "周报", route: "week", params: {} } : { k: "week", label: "周报", route: "rpt", params: { seg: "week" } });
  if (role !== "tech") out.push({ k: "month", label: "月报", route: "rpt", params: { seg: "month" } });
  // 英文周报 / 月报也是定期汇报，与周报、月报同级归“报表”（RJ 10-10 工笔文书分四区）；技术员、系统管理员没有
  if (["manager", "eng_sup", "safety_sup", "leader"].includes(role)) out.push({ k: "weekly_en", label: "英文周报", route: "rpt", params: { seg: "weekly_en" } }, { k: "monthly_en", label: "英文月报", route: "rpt", params: { seg: "monthly_en" } });
  return out;
}
function isRptRoute(role, r) {
  return r.name === "rpt" || r.name === "logs" || (role === "tech" && (r.name === "week" || r.name === "capture")) || (r.name === "reports" && (RPT_KINDS.includes(r.params.kind) || gbTab(r.params.kind) === "en"));
}
const ReportHub = {
  components: { LogList, TechLog, Reports },
  template: `<div class="rpt-hub"><div class="seg rpt-seg" role="tablist" aria-label="报表类型" v-if="segs.length > 1">
      <button v-for="s in segs" :key="s.k" role="tab" :aria-selected="s.k === seg ? 'true' : 'false'" :class="{on: s.k === seg}" @click="go(s.route, s.params)">{{ s.label }}</button></div>
    <log-list v-if="seg === 'day'"></log-list><tech-log v-else-if="seg === 'week' && tech"></tech-log><reports v-else-if="en" cat="en" :only="seg" :key="seg"></reports><reports v-else :fixed="seg" :key="seg"></reports></div>`,
  computed: {
    tech() { return roleOf() === "tech"; },
    en() { return gbTab(this.seg) === "en"; },
    segs() { return rptSegs(roleOf()); },
    seg() {
      const r = store.route, ks = this.segs.map((s) => s.k);
      const want = r.name === "logs" ? "day" : r.name === "week" || r.name === "capture" ? "week" : r.name === "reports" ? r.params.kind : r.params.seg;
      return ks.includes(want) ? want : ks[0];
    },
  },
  methods: { go },
};

const Root = {
  components: { ExtSlot, WatermarkCamera, WatermarkSheet, CircleFeed, AskBar, Login, LogEditor, TechHome, TechLog, TaskList, FindingDetail, LogList, Workbench, QualityHub, SafetyHub, ProgressHub,
    AlertDetail, OutputPage, Reports, ReportView, AgentsHub, Settings, Notices, Cockpit, TodayLog, ReportHub, SafeHome, LedgerPage, EngHome, SafetyDash, SysHome, UsersPage, AuditPage },
  template: `<div>
  <login v-if="!store.me && store.route.name === 'login'"></login>
  <div v-else-if="store.me" class="shell" :data-role="store.me.user.role">
    <aside class="side"><div class="logo"><img class="logo-mk" src="/static/brand/logo-eye.png" alt=""><div class="logo-tx"><b>智眸·工笔</b><small>施工现场 AI 助手</small><small class="logo-org">中国港湾 CHEC</small></div></div>
      <nav class="navlines"><a v-for="l in nav.top" :key="l.k" :class="['nl', {on: isOnLine(l)}]" :title="l.title" @click="enterLine(l)"><ic :n="l.icon"></ic>
          <span class="nl-t"><b>{{ l.name }}</b><small v-if="l.sub !== l.name">{{ l.sub }}</small></span></a>
        <div class="sep" v-if="nav.top.length && nav.bottom.length"></div>
        <a v-for="l in nav.bottom" :key="l.k" :class="['nl', 'bt', {on: isOnLine(l)}]" :title="l.title" @click="enterLine(l)"><ic :n="l.icon"></ic>
          <span class="nl-t"><b>{{ l.name }}</b><small v-if="l.pages.length > 1">{{ l.sub }}</small></span>
          <span class="badge-dot" v-if="l.k === 'msg' && store.me.unread">{{ store.me.unread }}</span></a></nav>
      <div class="foot"><div class="whoami"><span class="av">{{ store.me.user.name.slice(0, 1) }}</span><div><b>{{ store.me.user.name }}</b><div class="tiny">{{ roleName }}<span v-if="store.me.user.company"> · {{ store.me.user.company }}</span></div></div></div>
        <select v-if="store.me.projects.length > 1" class="in" style="margin:6px 0;background:#132f4c;color:#cfdbe8;border-color:#24476b" :value="store.me.project.id" @change="switchProject($event.target.value)">
          <option v-for="p in store.me.projects" :key="p.id" :value="p.id">{{ p.name }}</option></select>
        <div class="foot-row"><span v-if="store.me.ai.provider === 'mock'" class="ai-stat warn"><i class="dot"></i>离线模拟模型</span><span v-else :class="['ai-stat', store.me.ai.ready ? 'ok' : 'warn']"><i class="dot"></i>交融大模型 {{ store.me.ai.ready ? '已连接' : '未就绪' }}</span>
        <span class="foot-acts"><a class="foot-out" @click="openPw">修改密码</a><a class="foot-out" @click="logout">退出登录</a></span></div></div>
      <div class="side-culture">包容 诚信 创新 奉献</div></aside>
    <div class="grow" style="min-width:0">
      <div class="hdr"><div class="hdr-proj"><ic n="building"></ic><select v-if="store.me.projects.length > 1" :value="store.me.project.id" @change="switchProject($event.target.value)"><option v-for="p in store.me.projects" :key="p.id" :value="p.id">{{ p.name }}</option></select><span v-else>{{ store.me.project ? store.me.project.name : '' }}</span><span class="sim-tag" v-if="sim" title="仿真演示数据，不计入应用成效">仿真演示</span></div>
        <div class="hdr-ask" v-if="showAsk && desk"><ask-bar></ask-bar></div><span class="grow" v-else></span>
        <template v-if="role === 'admin'"><span class="hdr-date"><ic n="calendar"></ic>{{ today }}</span>
        <span v-if="store.me.ai.provider === 'mock'" class="ai-stat warn"><i class="dot"></i>离线模拟模型</span><span v-else :class="['ai-stat', store.me.ai.ready ? 'ok' : 'warn']"><i class="dot"></i>交融大模型 {{ store.me.ai.ready ? '已连接' : '未就绪' }}</span></template>
        <help-tip v-if="orphanNote" class="hdr-note" :text="orphanNote" label="本页说明"></help-tip>
        <a class="hdr-bell" title="消息" @click="go('notices')"><ic n="bell"></ic><span class="badge-dot" v-if="store.me.unread">{{ store.me.unread }}</span></a>
        <div class="hdr-me" @click="meMenu = !meMenu" @mouseleave="meMenu = false"><span class="av">{{ store.me.user.name.slice(0, 1) }}</span><div><b>{{ store.me.user.name }}</b><small>{{ roleName }}<template v-if="store.me.user.company"> · {{ store.me.user.company }}</template></small></div><ic n="chevron-down"></ic>
          <div class="hdr-menu" v-if="meMenu" @click.stop="meMenu = false"><a @click="openPw">修改密码</a><a @click="logout">退出登录</a></div></div></div>
      <div class="topbar"><span class="t"><img class="logo-mk" src="/static/brand/logo-eye.png" alt="">智眸·工笔 <span class="tiny" style="opacity:.7">{{ roleName }}</span><span class="sim-mini" v-if="sim">仿真</span></span><help-tip v-if="orphanNote" class="hdr-note" :text="orphanNote" label="本页说明"></help-tip><a class="tb-ic" title="消息" @click="go('notices')"><ic n="bell"></ic><span class="badge-dot" v-if="store.me.unread">{{ store.me.unread }}</span></a>
        <a class="tb-ic" :title="sheetName" @click="sheet = true"><ic :n="sheetLines.length ? 'menu' : 'user'"></ic></a></div>
      <div class="askrow" v-if="showAsk"><ask-bar v-if="!desk"></ask-bar><ext-slot name="askbar"></ext-slot></div>
      <div class="hubbar" v-if="hubPages.length > 1"><a v-for="p in hubPages" :key="p.k" :class="{on: curPage && curPage.k === p.k}" @click="goPage(p)"><ic :n="p.icon"></ic>{{ p.label }}</a></div>
      <main :class="['main', r.name === 'cockpit' ? 'wide' : '']" :key="routeKey">
        <div class="page-err" v-if="page.err"><load-state compact :error="page.err" @retry="retryPage"></load-state></div>
        <ext-slot :name="'top:' + r.name" v-if="!outOfScope"></ext-slot>
        <empty-state v-if="outOfScope" title="不在项目经理的查看范围" :reason="outReason" :action="{ label: '回首页', route: homeRoute }"></empty-state>
        <circle-feed v-else-if="r.name === 'circle'"></circle-feed>
        <safe-home v-else-if="r.name === 'shome'"></safe-home>
        <ledger-page v-else-if="r.name === 'ledger'"></ledger-page>
        <eng-home v-else-if="r.name === 'eng'"></eng-home>
        <safety-dash v-else-if="r.name === 'sdash'"></safety-dash>
        <sys-home v-else-if="r.name === 'sysh'"></sys-home>
        <users-page v-else-if="r.name === 'users'"></users-page>
        <audit-page v-else-if="r.name === 'audit'"></audit-page>
        <tech-home v-else-if="r.name === 'home' || r.name === 'snap'"></tech-home>
        <report-hub v-else-if="r.name === 'rpt' || isRpt"></report-hub>
        <tech-log v-else-if="r.name === 'capture' || r.name === 'week'"></tech-log>
        <task-list v-else-if="r.name === 'tasks'"></task-list>
        <today-log v-else-if="r.name === 'manual'" mode="manual"></today-log>
        <component :is="logView" v-else-if="r.name === 'log'" :id="r.params.id"></component>
        <log-list v-else-if="r.name === 'logs'"></log-list>
        <finding-detail v-else-if="r.name === 'finding'" :id="r.params.id"></finding-detail>
        <workbench v-else-if="r.name === 'work'"></workbench>
        <cockpit v-else-if="r.name === 'cockpit'"></cockpit>
        <quality-hub v-else-if="r.name === 'quality'"></quality-hub>
        <safety-hub v-else-if="r.name === 'safety'"></safety-hub>
        <progress-hub v-else-if="r.name === 'progress'"></progress-hub>
        <alert-detail v-else-if="r.name === 'alert'" :id="r.params.id"></alert-detail>
        <output-page v-else-if="r.name === 'output'"></output-page>
        <reports v-else-if="r.name === 'reports'"></reports>
        <report-view v-else-if="r.name === 'report'" :id="r.params.id"></report-view>
        <agents-hub v-else-if="r.name === 'agents'"></agents-hub>
        <settings v-else-if="r.name === 'settings'"></settings>
        <notices v-else-if="r.name === 'notices'"></notices>
        <component :is="extView" v-else-if="extView"></component>
        <div v-else class="empty">页面不存在</div>
        <ext-slot :name="'bottom:' + r.name" v-if="!outOfScope"></ext-slot>
        <!-- 页脚说明行已撤（RJ 10-10）：仿真标记在顶栏项目名旁，AI 与口径说明并进页头或顶栏的“?”，模型状态只给系统管理员看（顶栏） -->
      </main></div>
    <nav class="tabbar"><a v-for="l in tabLines" :key="l.k" :class="{on: isOnLine(l)}" @click="enterLine(l)"><span class="i"><ic :n="l.icon"></ic></span>{{ l.name }}</a>
      <a v-if="overflow.length" :class="{on: overflow.some(isOnLine)}" @click="sheet = true"><span class="i"><ic n="menu"></ic></span>更多</a></nav>
    <div class="sheet-bg" v-if="sheet" @click.self="sheet = false"><div class="sheet"><div class="row between"><b>{{ sheetName }}</b><a class="small" @click="sheet = false">关闭</a></div>
      <div class="sh-line small muted" v-if="!sheetLines.length">{{ store.me.user.name }} · {{ roleName }}<span v-if="store.me.user.company"> · {{ store.me.user.company }}</span></div>
      <div class="sh-line" v-for="l in sheetLines" :key="l.k"><div class="sh-t" v-if="l.pages.length > 1 || l.pages[0].label !== l.name"><ic :n="l.icon"></ic>{{ l.name }}</div>
        <div class="sh-pages"><a v-for="p in l.pages" :key="p.k" :class="{on: curPage && curPage.k === p.k}" @click="goPage(p)"><ic :n="p.icon"></ic>{{ p.label }}<span class="badge-dot" v-if="p.k === 'notices' && store.me.unread">{{ store.me.unread }}</span></a></div></div>
      <div class="sh-line row" style="gap:18px"><a class="small muted" @click="openPw">修改密码</a><a class="small muted" @click="logout">退出登录</a></div></div></div>
    <div class="modal-bg" v-if="pw" @click.self="pw = null"><div class="modal pw-dlg"><h2>修改密码</h2>
      <label class="f">原密码</label><input class="in" type="password" v-model="pw.old" autocomplete="current-password">
      <label class="f mt">新密码</label><input class="in" type="password" v-model="pw.new" placeholder="至少 6 位" autocomplete="new-password" @keyup.enter="savePw">
      <div class="row mt" style="justify-content:flex-end;gap:8px"><button class="btn" @click="pw = null">取消</button><button class="btn primary" :disabled="pw.busy" @click="savePw">确认修改</button></div></div></div>
  </div>
  <div v-if="store.toast" :class="['toast', store.toast.err ? 'err' : '']">{{ store.toast.msg }}</div>
  <div class="viewer" v-if="store.viewer" @click="store.viewer = null"><img :src="store.viewer"></div>
  <watermark-camera v-if="store.cam && store.cam.mode !== 'sheet'"></watermark-camera><watermark-sheet v-if="store.cam && store.cam.mode === 'sheet'"></watermark-sheet><ext-slot name="global"></ext-slot></div>`,
  data: () => ({ store, sheet: false, pw: null, page: PAGE, meMenu: false, desk: window.matchMedia("(min-width: 861px)").matches }),
  // 电脑端统一输入框放在顶栏中间（09_V8），手机端仍在顶栏下面一行；同一时刻只渲染一个，输入状态不分叉
  mounted() { this.mqDesk = window.matchMedia("(min-width: 861px)"); this.onDesk = (e) => { this.desk = e.matches; }; this.mqDesk.addEventListener("change", this.onDesk); },
  unmounted() { this.mqDesk.removeEventListener("change", this.onDesk); },
  computed: {
    r() { return store.route; },
    extView() { return EXT.routes[store.route.name]; },
    role() { return store.me ? store.me.user.role : ""; },  // 退出登录瞬间 store.me 为空，根组件仍会重算一次
    featKey() { return JSON.stringify(store.me && store.me.project ? store.me.project.features : null); },
    // 当前页（取岗位的页面，不看开关）与导航：当前页所属能力即使被隐藏也留在页签里，不让界面跳走
    curPage() { return navMatch(this.role, store.route.name, store.route.params, { findingAgent: store.findingAgent }); },
    nav() { void this.featKey; return buildNav(this.role, (store.me && store.me.project && store.me.project.features) || {}, this.curPage ? this.curPage.k : ""); },
    curLine() { const p = this.curPage; return p ? [...this.nav.top, ...this.nav.bottom].find((l) => l.pages.some((x) => x.k === p.k)) || null : null; },
    hubPages() { return this.curLine ? this.curLine.pages : []; },
    // 手机底栏：业务线 ≤5 条全部放；超过则前 4 条 + “更多”。顶栏菜单里放底部功能与溢出的业务线
    tabLines() { const t = this.nav.top; return t.length > 5 ? t.slice(0, 4) : t; },
    overflow() { const t = this.nav.top; return t.length > 5 ? t.slice(4) : []; },
    sheetLines() { return [...this.overflow, ...this.nav.bottom.filter((l) => l.k !== "msg" || this.overflow.length)]; },
    // 一线岗位（技术员、安全员）的菜单里只有修改密码与退出登录：叫“我的”，不叫“全部功能”（U-09）
    sheetName() { return this.sheetLines.length ? "全部功能" : "我的"; },
    routeKey() { return store.route.name + JSON.stringify(store.route.params) + (PAGE.n ? "#" + PAGE.n : ""); },   // PAGE.n：出错后“重试”重新打开本页
    outOfScope() { return pmOutOfScope(this.role, this.r.name); },
    isRpt() { return isRptRoute(this.role, this.r); },
    outReason() { return pmOutReason(this.r.name); },
    homeRoute() { return homeOf(); },
    logView() { return isTech() ? "tech-log" : "log-editor"; },
    roleName() { return store.me.user.role_name || store.me.user.role; },
    sim() { return isSim(); },
    // 没有页头的页面（各岗位首页等）的 <ai-note> 说明：放在顶栏一个“?”里，不占正文行高
    orphanNote() { const d = typeof demoDay === "function" ? demoDay() : null; return d && d.heads.length ? "" : PAGE_NOTE.list.map((x) => x.text).filter(Boolean).join(" "); },
    today() { const t = store.me && store.me.today; const d = t ? new Date(t + "T00:00:00") : new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} 周${"日一二三四五六"[d.getDay()]}`; },
    // 问工笔对话页自带输入框，顶部不再重复放一个
    showAsk() { return !isSubOrg() && this.role !== "admin" && !(this.r.name === "agents" && this.r.params.tab === "ask"); },
  },
  watch: { curPage(p) { if (p) lsSet("zb_line_" + p.line, p.k); }, "store.route.name"() { this.sheet = false; } },
  methods: {
    go,
    retryPage() { PAGE.err = null; PAGE.n++; },
    isOnLine(l) { return !!this.curLine && this.curLine.k === l.k; },
    goPage(p) { this.sheet = false; go(p.route, p.params || {}); },
    // 点一级菜单：进入该业务线上次停留的页面，没有则第一个页面
    enterLine(l) { const last = lsGet("zb_line_" + l.k, null); this.goPage(l.pages.find((p) => p.k === last) || l.pages[0]); },
    // 修改密码就地弹窗（侧栏底部与手机顶栏菜单共用），不再进设置页
    openPw() { this.sheet = false; this.pw = { old: "", new: "", busy: false }; },
    async savePw() {
      const f = this.pw; if (!f || f.busy) return;
      if (!f.old) return toast("请输入原密码", true);
      if ((f.new || "").length < 6) return toast("新密码至少 6 位", true);
      f.busy = true;
      try { await PUT("/api/me/password", { old: f.old, new: f.new }); this.pw = null; toast("密码已修改"); }
      catch (e) { toast(e.message, true); f.busy = false; }
    },
    async logout() { await POST("/api/logout"); store.me = null; try { sessionStorage.setItem("zb_no_auto", "1"); } catch (_) { /* 忽略 */ } go("login"); },
    async switchProject(id) { await PUT("/api/me/project", { project_id: +id }); await loadMe(); go(homeOf()); location.reload(); },
  },
};

(async function boot() {
  parseRoute();
  try { await loadMe(); } catch (_) { store.me = null; }
  if (!store.me) go("login");
  else if (!store.route.name || store.route.name === "login") go(homeOf());
  const app = createApp(Root);
  installErrorHandling(app);
  app.component("load-state", LoadState);
  app.component("template-editor", TemplateEditor);
  app.component("ic", Ic);
  app.component("kpi10", Kpi10);   // 周报十项指标（mgr.js），产值页与周报视图共用
  app.component("page-head", PageHead);
  app.component("ev-photo", EvPhoto);   // 现场证据照片：4:3 完整显示、缺文件占位、点开原图（x_ui.js）
  app.component("help-tip", HelpTip);
  app.component("empty-state", EmptyState);
  app.component("ai-note", AiNote);
  app.component("sim-badge", SimBadge);
  app.component("trust-tag", TrustTag);
  app.component("act-name", ActName);
  app.component("trust-split", TrustSplit);
  app.component("log-progress-trust", LogProgressTrust);
  app.component("zb-drawer", ZbDrawer);   // 全站一屏化共享件（x_ui.js）：抽屉、正文截断、前 N 条 + 查看全部、面板内滚动
  app.component("zb-clamp", ZbClamp);
  app.component("zb-more", ZbMore);
  app.component("zb-paged", ZbPaged);
  app.directive("fit", FitDirective);
  app.mount("#app");
})();
