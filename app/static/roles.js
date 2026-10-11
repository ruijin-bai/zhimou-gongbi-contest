/* 各角色专属首页与页面：安全员（分包）、工程主管、安全主管、系统管理员。技术员见 tech.js，项目经理见 mgr.js / cockpit.js。 */

// ---------------------------------------------------------------------------- 小部件
// 双系列柱图：每个日期两根柱（如 新增/闭环、人/机）
const PairBars = {
  props: { items: Array, a: String, b: String, aName: String, bName: String, height: { type: Number, default: 110 } },
  template: `<div><div class="pbars" :style="{height: height + 'px'}"><div class="pcol" v-for="(it, i) in items" :key="i" :title="tip(it)">
      <div class="pbar a" :style="{height: h(it[a]) + '%'}"></div><div class="pbar b" :style="{height: h(it[b]) + '%'}"></div></div></div>
    <div class="row between tiny muted" style="margin-top:4px"><span>{{ items.length ? items[0].label : '' }}</span>
      <span class="legend"><i class="a"></i>{{ aName }} <i class="b"></i>{{ bName }}</span><span>{{ items.length ? items[items.length - 1].label : '' }}</span></div></div>`,
  computed: { max() { return Math.max(1, ...this.items.map((i) => Math.max(i[this.a] || 0, i[this.b] || 0))); } },
  methods: { h(v) { return v ? Math.max(5, (v / this.max) * 100) : 0; }, tip(it) { return it.label + "  " + this.aName + " " + (it[this.a] ?? 0) + " / " + this.bName + " " + (it[this.b] ?? 0); } },
};

// 横向占比条（类别分布、分包对比）
const HBars = {
  props: { items: Array, color: { type: String, default: "var(--brand)" } },
  template: `<div><div class="hbar-row" v-for="it in items" :key="it.name"><span class="hbar-name small">{{ it.name }}</span>
    <div class="hbar"><i :style="{width: (it.n / max * 100) + '%', background: color}"></i></div><b class="hbar-n small">{{ it.n }}</b></div>
    <empty-state compact v-if="!items.length" reason="没有未闭环的隐患类别。"></empty-state></div>`,
  computed: { max() { return Math.max(1, ...this.items.map((i) => i.n)); } },
};

const Stat = {
  props: ["v", "l", "tone", "sub"],
  template: `<div :class="['kpi', tone || '']"><div class="v">{{ v }}</div><div class="l">{{ l }}</div><div class="tiny muted" v-if="sub">{{ sub }}</div></div>`,
};

const LedgerPage = {
  components: { FindingBoard },
  template: `<finding-board agent="safety" page="ledger"></finding-board>`,
};

const RISK = { high: ["red", "高风险"], medium: ["orange", "待核实"], low: ["green", "低风险"], none: ["", "未预审"] };

// ---------------------------------------------------------------------------- 安全员（分包·手机）
const SafeHome = {
  components: { SafetySnap, TodoMobile },
  // 一屏化（RJ 10-10）：本单位的人员名单不是每天看的，收成页头“本单位 N 人”入口，点开抽屉看；待我处理只摊开前 2 项，其余“查看全部”。
  // 设计 18（RJ 10-10，线程 E）：短横幅写明本人与本单位范围 → 随手拍（唯一拍照入口）→ 三格指标 → 待我处理（候选确认、整改都在这一处）→ 巡查提示与人员名单点开看
  template: `<div v-if="d" class="zb-m sh-home">
  <section class="zb-hero zb-m-hero"><page-head page="shome" title="随手拍"><template #meta>{{ store.me.user.name }} · {{ d.company || '本单位' }} · 只看本单位 · <a class="sh-team-a" @click="open.team = true">本单位 {{ d.team.length }} 人</a></template></page-head></section>
  <div class="zb-card zb-m-snap"><safety-snap :embedded="true"></safety-snap></div>
  <div class="zb-kpis n3 zb-m-kpis">
    <button type="button" :class="['zb-kpi', todo && todo.total ? 'orange' : '']" @click="jump"><div class="zb-kpi-b"><span class="zb-kpi-l">待我处理</span><span class="zb-kpi-v">{{ todo ? todo.total : '—' }}</span></div></button>
    <button type="button" :class="['zb-kpi', todo && todo.overdue ? 'red' : '']" @click="jump"><div class="zb-kpi-b"><span class="zb-kpi-l">其中超期</span><span class="zb-kpi-v">{{ todo ? todo.overdue : '—' }}</span></div></button>
    <button type="button" class="zb-kpi" @click="jump"><div class="zb-kpi-b"><span class="zb-kpi-l">其中 AI 候选</span><span class="zb-kpi-v">{{ todo ? todo.cand : '—' }}</span></div></button></div>
  <div id="m-todo" :class="['zb-m-todo', {all: allTodo}]"><div v-fit="200" class="zb-m-todo-b"><todo-mobile ref="todo" title="待我处理" @vue:mounted="hookTodo"></todo-mobile></div>
    <button type="button" class="zb-m-more" v-if="todo && todo.n > 2" @click="allTodo = !allTodo">{{ allTodo ? '收起' : '查看全部 ' + todo.n + ' 项' }}</button></div>
  <div :class="['zb-card', {shut: !open.focus}]"><div class="zb-ch zb-m-fold" @click="open.focus = !open.focus"><ic n="eye"></ic><h2>今日重点作业巡查</h2><span class="zb-n">{{ d.focus.length }}</span><span class="grow"></span><span class="zb-m-tg">{{ open.focus ? '收起' : '展开' }}</span></div>
    <div class="zb-body"><div class="readrow" v-for="(a, i) in d.focus" :key="i + a.code">
      <div class="row wrap"><span :class="['chip', a.critical ? 'red' : '']">{{ a.critical ? '关键' : '一般' }}</span><b class="small">{{ a.short_name || a.name }}</b>
        <span class="chip orange" v-for="t in a.tags" :key="t">{{ t }}</span></div>
      <div class="tiny muted">{{ a.part }} · {{ a.active ? '施工中' : '计划' }} {{ a.start }} ~ {{ a.finish }}</div></div>
      <div class="zb-empty" v-if="!d.focus.length">本单位负责范围内今天没有在施作业，作业开工后这里会出现巡查提示。</div>
      <div class="tiny muted" v-else>按作业名称关键词提示</div></div></div>
  <zb-drawer v-if="open.team" :title="'本单位人员 · ' + d.team.length + ' 人'" @close="open.team = false"><div class="row wrap"><span class="chip blue" v-for="m in d.team" :key="m.id">{{ m.name }} · {{ m.role_name }}</span></div>
    <div class="tiny muted mt">仅可派单给本单位人员，闭环由项目部安全主管复核。</div></zb-drawer>
  <ai-note page="shome"></ai-note>
</div><load-state v-else loading :error="err" @retry="load"></load-state>`,
  data: () => ({ d: null, err: null, store, todo: null, allTodo: false, open: { focus: false, team: false } }),
  mounted() { this.load(); },
  methods: {
    async load() { this.err = null; try { this.d = await GET("/api/safety/home"); } catch (e) { this.err = e; } },
    // 指标读下面待办组件已取到的同一份数据（候选数 = 待办里来源为 AI 候选的条数），不另发请求
    hookTodo() { if (this.unTodo) this.unTodo(); this.unTodo = this.$watch(() => this.$refs.todo && this.$refs.todo.d, (t) => { this.todo = t ? { total: t.total || 0, overdue: t.overdue || 0, cand: (t.items || []).filter((i) => i.source === "candidate").length, n: todoEntries(t.items || []).length } : null; }, { immediate: true }); },
    jump() { const el = document.getElementById("m-todo"); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); },
  },
};

// ---------------------------------------------------------------------------- 工程主管、安全主管（港湾）
// RJ 10-10 设计包 13、14：页面在 x_enghome.js（EngBoard / SafetyBoard，D 线程维护），这里只保留路由用的组件名
const EngHome = { components: { EngBoard }, template: `<eng-board></eng-board>` };
const SafetyDash = { components: { SafetyBoard }, template: `<safety-board></safety-board>` };

// ---------------------------------------------------------------------------- 系统管理员
// 审计的操作名、对象名由后端写成中文（role_views.audit_names：action_name / target_name），这里不再另存一份对照表

// 系统概况（RJ 10-10 采用 16_系统管理员_系统概况，紫改中港蓝）：账号、AI、运行、审计这些系统事务；管理员不参与业务（不生成、不发布报表，不做工程审批）。
// - 各岗位账号数取真实账号表，不另行分配；没有公告来源，不放系统公告；
// - 失败数是近 7 天历史记录，不等于当前待处理数；调度器关闭是隔离环境的启动设置，不标成故障；
// - 扫码网址用于免登录现场上报（不是扫码登录）；审计只列已记录的操作；
// - 手机：标题、指标、运行状态、AI 连通在前，其余块点标题展开。
// - 一屏化（RJ 10-10）：免登录现场上报扫码网址是部署配置、不是每天看的，收成页头一个低调入口“扫码网址设置”，表单在抽屉里就地保存；最近审计电脑端摊开 4 条，全部在“AI 与审计”。
const SYS_ORG = { harbour: "港湾", sub: "分包", system: "系统" };
const SysHome = {
  template: `<div class="sy-home">
  <section class="zb-hero"><div class="zb-hero-tx"><page-head page="sysh"><template #meta>系统管理员 · 账号、AI 与运行审计；不参与业务审批与报表</template>
    <template #actions><button class="zb-link sy-qr-a" v-if="qb" type="button" @click="qrOpen = true" :title="qb.base ? '扫码网址：' + qb.base : '扫码网址未填写，按本机局域网地址生成'"><i :class="['sy-dot', qbLocal ? 'warn' : 'ok']"></i>扫码网址设置</button>
      <button class="zb-chip" :disabled="testing" @click="test"><ic n="refresh"></ic>{{ testing ? '测试中' : 'AI 连通测试' }}</button></template></page-head></div></section>
  <load-state compact v-if="!o" :loading="!err" :error="err" @retry="load"></load-state>
  <template v-if="o">
  <div class="zb-kpis n4">
    <component :is="k.route ? 'button' : 'div'" v-for="k in kpis" :key="k.k" :class="['zb-kpi', k.tone, k.route ? '' : 'ld-static']" @click="k.route && go(k.route)">
      <span class="zb-kpi-ic"><ic :n="k.icon"></ic></span>
      <span class="zb-kpi-b"><span class="zb-kpi-l">{{ k.label }}</span><b class="zb-kpi-v">{{ k.value }}<small v-if="k.unit">{{ k.unit }}</small></b><span class="zb-kpi-s" :title="k.sub">{{ k.sub }}</span></span>
      <ic n="chevron" class="zb-kpi-go" v-if="k.route"></ic></component></div>

  <div class="zb-row sy-r2">
    <section :class="['zb-card', 'sy-fold', {shut: shut('run')}]"><div class="zb-ch" @click="tog('run')"><ic n="signal"></ic><h2>运行状态</h2><span class="grow"></span></div>
      <div class="zb-body">
        <div class="sy-kv"><ic n="bot"></ic><span>AI 模式</span><span class="sy-v"><i :class="['sy-dot', o.ai.ready ? 'ok' : 'bad']"></i>{{ o.ai.label }}<small v-if="o.ai.reason">{{ o.ai.reason }}</small></span></div>
        <div class="sy-kv" v-if="o.system.scheduler"><ic n="clock"></ic><span>后台调度</span><span class="sy-v" :title="o.system.scheduler.detail"><i :class="['sy-dot', o.system.scheduler.on ? 'ok' : 'warn']"></i>{{ o.system.scheduler.on ? '已开启' : '已关闭' }}
          <small>{{ o.system.scheduler.on ? o.system.scheduler.detail : '按启动设置关闭（隔离环境），定时任务需手动运行' }}</small></span></div>
        <div class="sy-kv"><ic n="calendar"></ic><span>定时任务</span><span class="sy-v">每日判定 {{ o.system.last_detect ? fmtDT(o.system.last_detect).slice(5) : '未运行' }}<small>晚间整理 {{ o.system.last_evening ? fmtDT(o.system.last_evening).slice(5) : '未运行' }}</small></span></div>
        <div class="sy-kv"><ic n="database"></ic><span>P6 计划</span><span class="sy-v" v-if="o.system.p6" :title="o.system.p6.file">数据日期 {{ o.system.p6.data_date }}<small>距今 {{ o.system.p6.stale_days }} 天</small></span><span class="sy-v" v-else>未导入</span></div></div></section>

    <section :class="['zb-card', 'sy-fold', {shut: shut('ai')}]"><div class="zb-ch" @click="tog('ai')"><ic n="link"></ic><h2>AI 连通</h2><span class="grow"></span><a class="zb-link" @click.stop="go('aimgr')">模型与额度 <ic n="chevron"></ic></a></div>
      <div class="zb-body">
        <div class="sy-kv"><ic n="sliders"></ic><span>Key</span><span class="sy-v" v-if="o.ai.keys">{{ o.ai.keys.usable }} / {{ o.ai.keys.total }} 可用</span><span class="sy-v" v-else>未登记</span></div>
        <div class="sy-kv"><ic n="camera"></ic><span>视觉模型</span><span class="sy-v">{{ o.ai.vision_model || '—' }}</span></div>
        <div class="sy-kv"><ic n="chart"></ic><span>近 7 天</span><span class="sy-v">运行 {{ o.ai.runs_7d }} 次 · 失败 {{ o.ai.failed_7d }} · 均耗时 {{ o.ai.avg_latency_s === null ? '—' : o.ai.avg_latency_s + 's' }}
          <small>失败为历史记录，不等于当前待处理</small></span></div>
        <div class="sy-kv" v-if="testResult"><ic n="check-circle"></ic><span>连通测试</span><span class="sy-v">{{ testResult }}</span></div>
        <div class="sy-kv" v-else-if="o.ai.recent_failures.length" :title="o.ai.recent_failures[0].error"><ic n="alert"></ic><span>最近失败</span>
          <span class="sy-v">{{ fmtDT(o.ai.recent_failures[0].created_at).slice(5) }} {{ o.ai.recent_failures[0].kind }}<small>{{ (o.ai.recent_failures[0].error || '').slice(0, 40) }}</small></span></div></div></section>

    <section :class="['zb-card', 'sy-fold', {shut: shut('proj')}]"><div class="zb-ch" @click="tog('proj')"><ic n="building"></ic><h2>项目与数据</h2><span class="grow"></span><a class="zb-link" @click.stop="go('settings')">项目与规则 <ic n="chevron"></ic></a></div>
      <div class="zb-body">
        <div class="sy-kv" v-for="p in o.projects" :key="p.id"><ic n="building"></ic><span>{{ p.current ? '当前项目' : '项目' }}</span><span class="sy-v">{{ p.name }}<small>{{ p.simulation ? '仿真项目 · ' : '' }}启用账号 {{ p.users }} 个</small></span></div>
        <div class="sy-kv"><ic n="database"></ic><span>数据库</span><span class="sy-v">{{ o.system.db_mb }} MB<small>照片 {{ o.system.media_count }} 张 · {{ o.system.media_mb }} MB</small></span></div>
        <div class="sy-kv"><ic n="bell"></ic><span>未读消息</span><span class="sy-v">{{ o.system.unread_notifications }} 条<small>全部账号合计</small></span></div></div></section>
  </div>

  <div class="zb-row sy-r3">
    <section :class="['zb-card', 'sy-fold', {shut: shut('users')}]"><div class="zb-ch" @click="tog('users')"><ic n="users"></ic><h2>账号与权限</h2><span class="grow"></span><a class="zb-link" @click.stop="go('users')">管理账号 <ic n="chevron"></ic></a></div>
      <div class="zb-body"><div class="sy-roles"><div class="sy-role" v-for="r in o.users.by_role" :key="r.role"><b>{{ r.name }}</b><small>{{ SYS_ORG[r.org] || r.org }}</small>
          <span>启用 <b>{{ r.active }}</b><template v-if="r.inactive"> · 停用 {{ r.inactive }}</template></span></div></div>
        <div class="tiny muted mt" v-if="o.users.never_logged.length" :title="o.users.never_logged.join('、')">从未登录：{{ o.users.never_logged.slice(0, 4).join('、') }}<template v-if="o.users.never_logged.length > 4"> 等 {{ o.users.never_logged.length }} 人</template></div></div></section>

    <section :class="['zb-card', 'sy-fold', {shut: shut('audit')}]"><div class="zb-ch" @click="tog('audit')"><ic n="shield"></ic><h2>AI 治理与审计</h2><span class="grow"></span><a class="zb-link" @click.stop="go('audit')">AI 与审计 <ic n="chevron"></ic></a></div>
      <div class="zb-body"><div class="sy-flow" aria-label="AI 治理流程：AI 提取，人工确认，业务处理">
          <div class="sy-step"><ic n="file"></ic><b>AI 提取</b><small>从现场记录、资料中提取</small></div><ic n="chevron"></ic>
          <div class="sy-step"><ic n="check-circle"></ic><b>人工确认</b><small>岗位人员核对事实</small></div><ic n="chevron"></ic>
          <div class="sy-step"><ic n="clipboard"></ic><b>业务处理</b><small>进入台账与流程闭环</small></div></div>
        <div class="sy-au" v-for="a in o.audit.slice(0, 4)" :key="a.id"><time>{{ fmtDT(a.created_at).slice(5) }}</time><span :title="(a.name || '系统') + ' ' + a.action_name + ' ' + (a.target_name || '')"><b>{{ a.name || '系统' }}</b> {{ a.action_name }} {{ a.target_name }}</span></div>
        <div class="zb-empty" v-if="!o.audit.length">还没有操作记录。</div></div></section>
  </div>

  <zb-drawer v-if="qrOpen && qb" title="免登录现场上报扫码网址" @close="qrOpen = false">
    <div class="sy-qr"><span class="tiny muted nowrap">二维码网址前缀</span><input class="in" v-model.trim="qbase" :placeholder="qb.lan || 'http://可访问地址:8600'"><button class="btn sm" :disabled="qbusy" @click="saveQb">保存</button></div>
    <div class="tiny muted mt">留空时按本机局域网地址生成{{ qb.lan ? '（当前 ' + qb.lan + '）' : '，但本机未取到局域网地址' }}；正式部署请填写域名。安全员页面只提示“手机需与电脑在同一网络”。</div>
    <div class="banner warn mt" v-if="qbLocal">当前为 localhost 地址，手机无法打开。请改为局域网地址或部署域名。</div></zb-drawer>
  </template></div>`,
  data: () => ({ o: null, err: null, testing: false, testResult: "", qb: null, qbase: "", qbusy: false, qrOpen: false, opened: {}, SYS_ORG,
    narrow: window.matchMedia("(max-width: 640px)").matches }),
  computed: {
    qbLocal() { return /^https?:\/\/(localhost|127\.|\[::1\])/i.test(this.qbase || (this.qb && this.qb.lan) || location.origin); },
    kpis() {
      const o = this.o, u = o.users, never = u.never_logged.length;
      return [
        { k: "users", label: "启用账号", icon: "users", value: u.active, unit: " / " + u.total, route: "users", tone: "", sub: never ? never + " 人从未登录" : "均已登录" },
        { k: "ai", label: "AI 状态", icon: "bot", value: o.ai.ready ? "就绪" : "未就绪", route: "aimgr", tone: o.ai.ready ? "green" : "red", sub: (o.ai.label || "").replace(/模型?（.*$/, "") },
        { k: "runs", label: "近 7 天 AI 运行", icon: "chart", value: o.ai.runs_7d, route: "audit", tone: "", sub: `历史失败 ${o.ai.failed_7d}` },
        { k: "db", label: "数据库", icon: "database", value: o.system.db_mb + " MB", tone: "", sub: `照片 ${o.system.media_count} 张` },
      ];
    },
  },
  mounted() {
    this.load();
    this.mq = window.matchMedia("(max-width: 640px)"); this.onMq = (e) => { this.narrow = e.matches; }; this.mq.addEventListener("change", this.onMq);
  },
  unmounted() { if (this.mq) this.mq.removeEventListener("change", this.onMq); },
  methods: {
    go, fmtDT,
    // 手机上运行状态、AI 连通默认展开，其余块默认收起，点标题切换；电脑端始终展开
    shut(k) { return this.narrow && (["run", "ai"].includes(k) ? !!this.opened[k] : !this.opened[k]); },
    tog(k) { if (this.narrow) this.opened = { ...this.opened, [k]: !this.opened[k] }; },
    async load() {
      this.err = null;
      try { this.o = await GET("/api/admin/overview"); } catch (e) { this.err = e; return; }
      try { this.qb = await GET("/api/quick/base"); this.qbase = this.qb.base; } catch (_) { this.qb = null; }
    },
    async saveQb() {
      this.qbusy = true;
      try { const r = await PUT("/api/quick/base", { base: this.qbase }); this.qbase = r.base; this.qb = { ...this.qb, base: r.base }; toast("已保存"); }
      catch (e) { toast(e.message, true); } finally { this.qbusy = false; }
    },
    async test() { this.testing = true; this.testResult = ""; try { const r = await POST("/api/ai/test"); this.testResult = "连通：" + (r.provider === "mock" ? "模拟" : r.model) + " " + r.latency_ms + "ms"; } catch (e) { this.testResult = "失败：" + e.message; } finally { this.testing = false; } },
  },
};

const CAP_NAME = { progress_ops: "P6与进度判定/赶工", site_safety: "安全隐患闭环/忽略", site_quality: "质量与数据事项闭环", output_ops: "产值映射与导入", log_review: "分包日志审核",
  report_gen: "生成/编辑周月报", report_publish: "审核发布报表", metrics_ops: "应用成效与AI调用", dispatch: "派单/改派", nudge: "催办" };

const UsersPage = {
  template: `<div v-if="info" class="ra-users"><page-head page="users"><template #meta>启用 {{ users.filter((u) => u.active).length }} / 共 {{ users.length }} 个账号<help-tip>账号取自账号表。分包单位设技术员、安全员；港湾设工程主管、安全主管、项目经理。</help-tip></template><template #actions>
    <button class="btn primary" @click="edit({role: 'tech', active: true, wbs_scope: '', email: '', company: ''})">新增账号</button></template></page-head>
  <section class="zb-card"><div class="zb-ch"><ic n="users"></ic><h2>账号</h2><span class="zb-n">{{ shown.length }}</span><span class="grow"></span><span class="zb-src">账号表</span></div>
    <div class="zb-body"><select class="in ra-sel ra-role-sel" v-model="role" aria-label="按岗位筛选"><option value="">全部岗位（{{ users.length }}）</option>
      <optgroup v-for="g in chipGroups" :key="g.org" :label="g.label"><option v-for="r in g.roles" :key="r.key" :value="r.key">{{ r.name }}（{{ count(r.key) }}）</option></optgroup></select>
    <div class="rgroups">
      <div class="rchips"><button :class="['rchip', {on: !role}]" @click="role = ''">全部 <b>{{ users.length }}</b></button></div>
      <div class="rchips" v-for="g in chipGroups" :key="g.org"><span class="tiny muted">{{ g.label }}</span>
        <button v-for="r in g.roles" :key="r.key" :class="['rchip', {on: role === r.key}]" @click="role = r.key">{{ r.name }} <b>{{ count(r.key) }}</b></button></div></div>
    <div class="small muted rduty" v-if="curRole"><b>{{ curRole.name }}</b> · {{ info.orgs[curRole.org] }} · {{ curRole.device }}：{{ curRole.duty }}</div>
    <zb-paged :items="shown" :size="15" :m-size="6" :fit="200" tag="table" cls="t ra-rt" unit="个">
      <template #head><thead><tr><th>账号</th><th>姓名</th><th>角色</th><th>所属单位</th><th>负责范围</th><th>最近登录</th><th>状态</th><th></th></tr></thead></template>
      <template #default="{ item: u }"><tr :class="{'ra-off': !u.active}"><td class="ra-rt-h"><b class="ra-dm">{{ u.name }} </b><span class="ra-un">{{ u.username }}</span></td><td class="ra-dk">{{ u.name }}</td><td data-l="角色"><span :class="['chip', 'r-chip-' + u.role]">{{ u.role_name }}</span></td>
        <td class="small" data-l="单位">{{ u.org === 'sub' ? (u.company || '未标注') : u.org === 'harbour' ? '港湾' : '系统' }}</td><td class="small" data-l="范围">{{ u.wbs_scope || (u.org === 'sub' ? '全部' : '—') }}</td>
        <td class="tiny muted" data-l="最近登录">{{ u.last_login ? fmtDT(u.last_login) : '从未登录' }}</td><td data-l="状态"><i :class="['sy-dot', u.active ? 'ok' : '']"></i>{{ u.active ? '启用' : '停用' }}</td><td class="ra-rt-a"><button class="btn sm" @click="edit(u)">编辑</button></td></tr></template>
      <template #empty><div class="zb-empty">这个岗位还没有账号。</div></template></zb-paged></div></section>
  <section class="zb-card ra-mx"><div class="zb-ch"><ic n="shield"></ic><h2>权限矩阵</h2><span class="grow"></span><span class="zb-src">系统固定定义</span><button class="zb-link" @click="mx = true">查看<ic n="chevron"></ic></button></div></section>
  <zb-drawer v-if="mx" title="权限矩阵" wide @close="mx = false">
    <div class="ra-tw"><table class="t ra-mxt"><thead><tr><th>能力</th><th v-for="r in matrixRoles" :key="r.key" class="right">{{ r.name }}</th></tr></thead>
      <tbody><tr v-for="(n, c) in CAP_NAME" :key="c"><td>{{ n }}</td><td class="right" v-for="r in matrixRoles" :key="r.key"><span v-if="r.caps.includes(c)" style="color:var(--green)">●</span><span v-else class="muted">·</span></td></tr></tbody></table></div>
    <div class="tiny muted mt">各岗位仅可查看相关数据，分包单位仅限本单位范围。安全隐患闭环/忽略限安全主管与项目经理，质量与日志审核限工程主管与项目经理。</div></zb-drawer>

  <div class="modal-bg" v-if="form" @click.self="form = null"><div class="modal"><h2>{{ form.id ? '编辑账号' : '新增账号' }}</h2>
    <div class="field mt"><label class="f">账号</label><input class="in" v-model.trim="form.username" :disabled="!!form.id"></div>
    <div class="field"><label class="f">姓名</label><input class="in" v-model.trim="form.name"></div>
    <div class="field"><label class="f">角色</label><select class="in" v-model="form.role">
      <optgroup v-for="g in roleGroups" :key="g.org" :label="g.label"><option v-for="r in g.roles" :key="r.key" :value="r.key">{{ r.name }}</option></optgroup></select>
      <div class="tiny muted mt">{{ roleDuty }}</div></div>
    <div class="field" v-if="isSub"><label class="f">所属分包单位</label><input class="in" list="company-list" v-model.trim="form.company" placeholder="例如：XX劳务公司"><datalist id="company-list"><option v-for="c in companies" :key="c" :value="c"></option></datalist></div>
    <div class="field" v-if="isSub"><label class="f">负责范围（留空为全部）</label><input class="in" v-model="form.wbs_scope" placeholder="WBS 名称或作业代码前缀，逗号分隔"></div>
    <div class="field"><label class="f">邮箱（接收预警）</label><input class="in" v-model.trim="form.email"></div>
    <div class="field"><label class="f">{{ form.id ? '重置密码（留空不改）' : '初始密码（至少6位）' }}</label><input class="in" type="password" v-model="form.password"></div>
    <label class="small" v-if="form.id"><input type="checkbox" v-model="form.active"> 启用</label>
    <div class="row mt2"><div class="grow"></div><button class="btn" @click="form = null">取消</button><button class="btn primary" @click="saveUser">保存</button></div></div></div></div><load-state v-else loading :error="err" @retry="load"></load-state>`,
  data: () => ({ info: null, users: [], form: null, role: "", err: null, CAP_NAME, mx: false }),
  computed: {
    shown() { return this.users.filter((u) => !this.role || u.role === this.role); },
    curRole() { return this.role ? this.info.roles.find((r) => r.key === this.role) : null; },
    roleGroups() { return ["sub", "harbour", "system"].map((org) => ({ org, label: this.info.orgs[org], roles: this.info.roles.filter((r) => r.org === org && r.key !== "leader") })); },
    // 筛选按钮列全部岗位（含公司领导），账号数都取自账号表；新增/编辑的岗位下拉沿用 roleGroups
    chipGroups() { return ["sub", "harbour", "system"].map((org) => ({ org, label: this.info.orgs[org], roles: this.info.roles.filter((r) => r.org === org && (r.key !== "leader" || this.count(r.key))) })).filter((g) => g.roles.length); },
    roleDuty() { const r = this.info.roles.find((x) => x.key === this.form.role); return r ? r.duty : ""; },
    isSub() { const r = this.info.roles.find((x) => x.key === this.form.role); return !!r && r.org === "sub"; },
    companies() { return [...new Set(this.users.filter((u) => u.company).map((u) => u.company))]; },
    matrixRoles() { return this.info.roles.filter((r) => r.key !== "leader"); },
  },
  mounted() { this.load(); },
  methods: {
    fmtDT,
    count(k) { return this.users.filter((u) => u.role === k && u.active).length; },
    async load() { this.err = null; try { [this.info, this.users] = await Promise.all([GET("/api/roles"), GET("/api/users")]); } catch (e) { this.err = e; } },
    edit(u) { this.form = { ...u, password: "" }; },
    async saveUser() {
      try { if (this.form.id) await PUT("/api/users/" + this.form.id, this.form); else await POST("/api/users", this.form); this.form = null; toast("已保存"); this.load(); }
      catch (e) { toast(e.message, true); }
    },
  },
};

const AuditPage = {
  template: `<div class="ra-audit"><page-head page="audit"><template #meta>{{ tab === 'audit' ? '只列系统已记录的操作，最近 200 条' : '每次大模型调用的留痕，最近 100 条；仅发往境内白名单主机' }}</template>
    <template #actions><div class="seg"><button v-for="t in tabs" :key="t[0]" :class="{on: tab === t[0]}" @click="tab = t[0]; load()">{{ t[1] }}</button></div></template></page-head>
  <section class="zb-card" v-if="tab === 'audit'"><div class="zb-ch"><ic n="shield"></ic><h2>操作审计</h2><span class="zb-n">{{ rows.length }}</span><span class="grow"></span>
      <select class="in ra-sel" v-model="action" @change="load"><option value="">全部操作</option><option v-for="a in actions" :key="a[0]" :value="a[0]">{{ a[1] }}</option></select>
      <label class="tiny ra-ck"><input type="checkbox" v-model="dev" @change="load"> 显示开发态登录</label></div>
    <div class="zb-body"><load-state compact v-if="err" :error="err" @retry="load"></load-state>
    <zb-paged v-else :items="rows" :size="20" :m-size="8" :fit="200" tag="table" cls="t ra-rt">
      <template #head><thead><tr><th>时间</th><th>人员</th><th>操作</th><th>对象</th><th>说明</th></tr></thead></template>
      <template #default="{ item: a }"><tr><td class="nowrap small ra-rt-t">{{ fmtDT(a.created_at) }}</td><td class="ra-rt-h"><b>{{ a.name || '系统' }}</b> <span class="ra-dm">{{ a.action_name }}</span></td><td class="ra-dk">{{ a.action_name }}</td><td class="small muted" data-l="对象">{{ a.target_name || '—' }}</td><td class="small muted ra-au-d" data-l="说明" :title="a.detail || null">{{ (a.detail || '').slice(0, 80) || '—' }}</td></tr></template>
      <template #empty><empty-state compact reason="还没有操作记录。登录、改设置、处置事项后会记在这里。"></empty-state></template></zb-paged></div></section>
  <section class="zb-card" v-else><div class="zb-ch"><ic n="bot"></ic><h2>AI 调用</h2><span class="zb-n">{{ rows.length }}</span><help-tip>记录每次大模型调用：模型、提示词版本、耗时与结果；仅发往境内白名单主机。</help-tip><span class="grow"></span><span class="zb-src" v-if="rows.length">失败 {{ rows.filter((r) => !r.ok).length }} 条（历史记录，不等于当前待处理）</span></div>
    <div class="zb-body"><load-state compact v-if="err" :error="err" @retry="load"></load-state>
    <zb-paged v-else :items="rows" :size="20" :m-size="8" :fit="200" tag="table" cls="t ra-rt">
      <template #head><thead><tr><th>时间</th><th>用途</th><th>模型</th><th>提示词版本</th><th class="right">耗时</th><th>结果</th></tr></thead></template>
      <template #default="{ item: r }"><tr><td class="nowrap small ra-rt-t">{{ fmtDT(r.created_at) }}</td><td class="ra-rt-h"><b>{{ r.kind }}</b></td><td class="small" data-l="模型">{{ r.provider === 'mock' ? '离线模拟' : r.model }}</td><td class="small muted" data-l="提示词">{{ r.prompt_version || '—' }}</td>
        <td class="right" data-l="耗时">{{ r.latency_ms === null ? '—' : (r.latency_ms / 1000).toFixed(1) + 's' }}</td><td data-l="结果"><span :class="['chip', r.ok ? 'green' : 'red']">{{ r.ok ? '成功' : '失败' }}</span><span class="tiny muted" v-if="r.error"> {{ r.error.slice(0, 50) }}</span></td></tr></template>
      <template #empty><empty-state compact reason="还没有 AI 调用记录。日志、预警、问答等用到大模型时会记在这里。"></empty-state></template></zb-paged></div></section></div>`,
  data: () => ({ tab: "audit", rows: [], action: "", dev: false, err: null }),
  computed: { tabs: () => [["audit", "操作审计"], ["ai", "AI 调用"]],
    actions: () => [["login", "登录"], ["user.", "账号管理"], ["project.", "项目设置"], ["finding.", "隐患/事项处置"], ["log.", "日志"], ["report.", "报表"], ["ai.", "AI模型与额度"]] },
  mounted() { this.load(); },
  methods: { fmtDT, async load() { this.err = null;   // 10-10 一屏化：列表用 zb-paged 内部分页（电脑 20 条、手机 8 条）
    try { this.rows = this.tab === "audit" ? await GET("/api/admin/audit?limit=200&action=" + encodeURIComponent(this.action) + "&dev=" + (this.dev ? 1 : 0)) : await GET("/api/ai/runs"); }
    catch (e) { this.rows = []; this.err = e; } } },
};
