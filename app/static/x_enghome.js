/* 工程主管、安全主管 · 智能管理台（RJ 10-10 设计包 13、14；沿用 09_V8 外壳与 x_v8.css 的 zb-* 共享组件，参考实现 x_pmhome.js）。
   - 只用既有接口（/api/todo、/api/eng/overview、/api/production/overview、/api/safety/overview），不新增统计口径，不硬编码设计稿示例数字、姓名与案例；
   - 版式：页头 → 指标 → 今日简报一行 → 领域摘要 / 待我处理 → 辅助分析（可滚动）。完整事务只在“待我处理”表（TodoBoard，就地办理）里出现一次，领域卡只放摘要与来源入口；
   - 工程主管的安全区固定叫“安全提示”：只读摘要 + 有权查看的来源，不放候选确认、派单、闭环按钮（安全管理归安全主管）；
   - 安全主管：AI 候选只在“AI 候选确认”一处展示与办理（CandidateGroup 就地确认/派单/不采纳），待我处理表不再重复列候选；
     删去设计稿自编的“近期闭环案例”“较昨日”，本周闭环只给台账计数，不代表仍需复核的条数；
   - 简报标签按真实来源：只有 source=ai 才写“AI按当前事实整理”，系统模板、系统重拼、未生成如实标出；
   - 现场照片横向 4:3 完整显示（contain），点开原图；文件缺失如实显示；
   - 手机：标题、指标网格、简报、待我处理在前；领域摘要与辅助分析默认收起，标题行即 44px 触控区。
   - “数据与依据”（清单映射、P6 依据）不是每天看的东西（RJ 10-10），不放首页：清单映射在“进度与产值 › 产值”，P6 依据在“进度与产值 › P6 计划”；
   - 一屏化（RJ 10-10 两档口径的一档）：电脑 1366×768、1440×900 页面本身不滚动。待我处理表放进 v-fit 面板内滚动；
     辅助分析合成一张分页签卡片（工程主管：人机投入 / 阻碍问题，各分包今日记录与本周确认移到“分包履约画像”页；安全主管：分包对比 / 隐患分布 / 近 14 天）；
     AI 候选只摊开第一张照片，其余与“确认处理”在抽屉里就地办理；催办与固定的提问收成一行入口，点开抽屉；口径说明收进小问号；
     手机待我处理只摊开前 2 项，其余“查看全部”。 */
const EH_NARROW = "(max-width: 640px)";
const ehShort = (c) => (c || "未登记单位").replace(/[（(].*?[）)]/g, "");

// 手机上辅助块默认收起、点标题展开；电脑端始终展开（与经理首页同一做法）
const ehFold = {
  data: () => ({ narrow: window.matchMedia(EH_NARROW).matches, opened: {} }),
  mounted() { this.mq = window.matchMedia(EH_NARROW); this.onMq = (e) => { this.narrow = e.matches; }; this.mq.addEventListener("change", this.onMq); },
  unmounted() { this.mq.removeEventListener("change", this.onMq); },
  methods: {
    shut(k) { return this.narrow && !this.opened[k]; },
    tog(k) { if (this.narrow) this.opened = { ...this.opened, [k]: !this.opened[k] }; },
  },
};

// 指标卡一行（zb-kpis）：k = {k, label, icon, value, unit?, sub, tone, route? | fn?}
const EhKpis = {
  props: { list: Array },
  template: `<div :class="['zb-kpis', 'n' + list.length]">
    <button v-for="k in list" :key="k.k" type="button" :class="['zb-kpi', k.tone]" @click="pick(k)"><span class="zb-kpi-ic"><ic :n="k.icon"></ic></span>
      <span class="zb-kpi-b"><span class="zb-kpi-l">{{ k.label }}</span><b class="zb-kpi-v">{{ k.value }}<small v-if="k.unit">{{ k.unit }}</small></b><span class="zb-kpi-s" :title="k.sub">{{ k.sub }}</span></span><ic n="chevron" class="zb-kpi-go"></ic></button></div>`,
  methods: { pick(k) { if (k.fn) k.fn(); else todoGo(k.route); } },
};

// 今日简报一行：取 /api/todo 的 attention（后端已按岗位挑结论句、按 scope_ok 把关）。标签按真实来源，不冒用“AI按当前事实整理”
const EhBrief = {
  props: { a: Object },
  emits: ["changed"],
  template: `<section class="zb-card eh-brief"><span :class="['zb-src', a && a.source === 'ai' ? 'ai' : '']">{{ label }}<help-tip v-if="tip" :text="tip"></help-tip></span>
    <span class="eh-brief-t" :title="text">{{ text }}</span>
    <button class="zb-link" type="button" :disabled="busy" @click="make"><ic n="refresh"></ic>{{ busy ? '生成中' : (missing ? '生成' : '刷新') }}</button></section>`,
  data: () => ({ busy: false }),
  computed: {
    missing() { return !this.a || !this.a.enabled || this.a.missing; },
    label() { return this.missing ? "今日简报尚未生成" : (PM_BRIEF_LABEL[this.a.source] || "今日简报"); },
    text() {
      if (this.missing) return "简报只汇总进度、质量、安全，点“生成”由简报Agent 按当前事实整理。";
      const h = this.a.headline || "";
      return /^简报Agent暂不可用/.test(h) || !h ? "简报里没有需要关注的结论。" : h;
    },
    tip() {
      if (this.missing) return "";
      return [this.a.at ? "生成于 " + fmtDT(this.a.at) : "", this.a.source === "system" ? this.a.note : ""].filter(Boolean).join("。");
    },
  },
  methods: {
    async make() {
      if (this.busy) return;
      this.busy = true;
      try { await POST("/api/brief"); toast("简报已更新"); this.$emit("changed"); } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

// 催办与固定的提问（EXT.feed 的 eng / sdash 来源，与原首页同一份；无条目整块不出）
const EhExtra = {
  props: { home: String },
  template: `<span class="eh-extra" v-if="extra.length"><button type="button" class="zb-link" @click="open = true"><ic n="clock"></ic>催办与固定的提问<span class="zb-n">{{ extra.length }}</span><ic n="chevron"></ic></button>
    <zb-drawer v-if="open" title="催办与固定的提问" @close="open = false">
    <div class="eh-x" v-for="(it, n) in extra" :key="n + it.title"><span :class="['lv-dot', it.level]"></span>
      <div class="grow small"><span class="chip">{{ it.agent }}</span> <b>{{ it.title }}</b><div class="tiny muted" v-if="it.basis">{{ it.basis }}</div>
        <div class="zb-btns mt" v-if="it.actions && it.actions.length"><button v-for="(x, m) in it.actions" :key="m" :class="['btn', 'sm', m ? 'ghost' : '']" :disabled="busy" @click="act(x)">{{ x.label }}</button></div></div></div></zb-drawer></span>`,
  data: () => ({ extra: [], busy: false, open: false }),
  mounted() { this.load(); this.stop = watch(() => NUDGE.ver.n, () => this.load()); },
  unmounted() { if (this.stop) this.stop(); },
  methods: {
    async load() { try { this.extra = await EXT.collect(this.home); } catch (_) { this.extra = []; } },
    async act(a) {
      if (a.route) return go(a.route, a.params || {});
      if (!a.api || (a.confirm && !confirm(a.confirm))) return;
      this.busy = true;
      try {
        const r = await api(a.api.method || "POST", a.api.url, a.api.body);
        if (r && r.ok === false) toast("未成功：" + (r.error || "请稍后重试"), true); else toast(a.done || "已完成");
        await this.load();
      } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

// 双系列小柱图（近 14 天）：沿用 zb-bars 的样式；日期少于 2 天或全为 0 时由调用方给空态
const EhPairBars = {
  props: { days: Array, a: String, b: String, aName: String, bName: String, label: String },
  template: `<div><svg class="zb-bars eh-bars" :viewBox="'0 0 ' + W + ' 86'" role="img" :aria-label="label">
      <g v-for="(t, i) in ticks" :key="'t' + i"><line x1="24" :x2="W - 2" :y1="t.y" :y2="t.y" class="zb-grid"></line><text x="18" :y="t.y + 4" class="zb-ax" text-anchor="end">{{ t.v }}</text></g>
      <g v-for="(d, i) in bars" :key="d.date"><title>{{ d.date }}  {{ aName }} {{ d[a] || 0 }} / {{ bName }} {{ d[b] || 0 }}</title>
        <rect :x="d.x" :y="d.y1" :width="bw" :height="d.h1" class="zb-b1"></rect><rect :x="d.x + bw + 1" :y="d.y2" :width="bw" :height="d.h2" class="zb-b2"></rect>
        <text v-if="i % step === 0 || i === bars.length - 1" :x="d.x + bw" y="84" class="zb-ax" text-anchor="middle">{{ d.date.slice(5) }}</text></g></svg>
    <div class="zb-foot"><span class="zb-legend"><i class="b1"></i>{{ aName }}<i class="b2"></i>{{ bName }}</span><slot></slot></div></div>`,
  data: () => ({ W: 420 }),
  computed: {
    max() { return Math.max(1, ...this.days.map((d) => Math.max(d[this.a] || 0, d[this.b] || 0))); },
    slot() { return (this.W - 30) / Math.max(1, this.days.length); },
    bw() { return Math.max(3, Math.min(12, this.slot / 2 - 2)); },
    step() { return this.days.length > 8 ? 3 : 1; },
    ticks() { const m = this.max, s = m <= 4 ? 1 : Math.ceil(m / 4), out = []; for (let v = 0; v <= m; v += s) out.push({ v, y: 70 - (v / m) * 58 }); return out; },
    bars() {
      return this.days.map((d, i) => {
        const h1 = ((d[this.a] || 0) / this.max) * 58, h2 = ((d[this.b] || 0) / this.max) * 58;
        return { ...d, x: 28 + i * this.slot + (this.slot - 2 * this.bw - 1) / 2, h1, h2, y1: 70 - h1, y2: 70 - h2 };
      });
    },
  },
};

// 待我处理（同一份 /api/todo，同一套就地操作）：外层卡片只加标题与计数；表格、页签、筛选、分组都是 TodoBoard 原样
const EhTodo = {
  components: { TodoBoard },
  props: { d: Object, note: String, tip: String },
  emits: ["changed"],
  template: `<section class="zb-card eh-todo" id="eh-todo"><div class="zb-ch"><ic n="clipboard"></ic><h2>待我处理</h2><span class="zb-n" v-if="n">{{ n }}</span>
      <span class="tiny muted" v-if="late">其中已超期 {{ late }}</span><help-tip v-if="tip" :text="tip"></help-tip><span class="grow"></span><span class="tiny muted" v-if="note">{{ note }}</span></div>
    <div v-fit="180" :class="['eh-todo-fit', {all}]"><todo-board :d="d" @changed="$emit('changed')"></todo-board></div>
    <button type="button" class="zb-m-more eh-todo-all" v-if="n > 2" @click="all = !all">{{ all ? '收起' : '查看全部 ' + n + ' 项' }}</button></section>`,
  data: () => ({ all: false }),
  computed: {
    entries() { return todoEntries(this.d.items); },
    n() { return this.entries.length; },
    late() { return this.entries.filter((e) => e.overdue).length; },
  },
};

// ============================================================================ 工程主管
const EngBoard = {
  mixins: [ehFold],
  components: { EhKpis, EhBrief, EhExtra, EhTodo, EhPairBars },
  template: `<div class="eh-home">
    <section class="zb-hero"><div class="zb-hero-tx"><h1>{{ projName }}</h1><div class="zb-hero-sub">工程主管 · 智能管理台</div></div></section>
    <load-state compact v-if="err && !d" :error="err" @retry="load"></load-state>
    <load-state compact v-else-if="!d" :loading="true"></load-state>
    <template v-if="d">
    <eh-kpis :list="kpis"></eh-kpis><component :is="subToday" v-if="weekOpen && subToday" bare @close="weekOpen = false"></component>
    <eh-brief :a="d.attention" @changed="load"></eh-brief>

    <div class="zb-row eh-r4">
      <section :class="['zb-card', 'eh-dom', {shut: shut('prog')}]"><div class="zb-ch" @click="tog('prog')"><ic n="trend"></ic><h2>进度</h2><span class="grow"></span>
          <span class="tiny muted" v-if="p6">P6 数据日期 {{ p6.data_date }}<template v-if="p6.stale_days > 14"> · <b class="tc-over">需更新</b></template></span></div>
        <div class="zb-body">
          <div class="eh-li" v-if="slip.length"><span class="eh-k">进度信号</span><span class="eh-v"><template v-for="(s, i) in slip.slice(0, 2)" :key="s.code"><span :class="['chip', s.level === 'red' ? 'red' : 'orange']">{{ s.level === 'red' ? '红' : s.level === 'orange' ? '橙' : '黄' }}</span>{{ s.short_name || s.name }}<small>（{{ s.rule }}）</small><template v-if="i < Math.min(2, slip.length) - 1">；</template></template><template v-if="slip.length > 2"> 等 {{ slip.length }} 项</template></span></div>
          <div class="eh-li" v-else><span class="eh-k">进度信号</span><span class="eh-v muted">暂无</span></div>
          <div class="eh-li" v-if="ms"><span class="eh-k">关键节点</span><span class="eh-v" :title="ms.name"><b>{{ ms.short_name || ms.name }}</b> 计划 {{ ms.p6_date || '—' }}<small> · 模拟 P50 {{ ms.p50 || '—' }} · 按期概率估计 {{ ms.prob_label || '未知' }}</small></span></div>
          <div class="eh-li" v-else><span class="eh-k">关键节点</span><span class="eh-v muted">还没有里程碑预测</span></div>
          <div class="eh-li" v-if="!p6"><span class="eh-v"><a @click="go('progress', {tab: 'plan'})">未导入 P6 计划，去导入</a></span></div>
          <div class="zb-foot"><a @click="todoGo('progress?tab=alerts')">预警</a><a v-if="has('gate')" @click="todoGo('gate')">开工条件</a><a @click="todoGo('progress?tab=forecast')">预测与赶工 <ic n="chevron"></ic></a></div></div></section>

      <section :class="['zb-card', 'eh-dom', {shut: shut('qual')}]"><div class="zb-ch" @click="tog('qual')"><ic n="shield"></ic><h2>质量</h2><span class="grow"></span><span class="tiny muted">当前台账</span></div>
        <div class="zb-body">
          <div class="eh-nums"><div><b>{{ q.open || 0 }}</b><span>未闭环</span></div><div :class="{red: q.overdue}"><b>{{ q.overdue || 0 }}</b><span>超期</span></div><div><b>{{ q.reminders || 0 }}</b><span>质控提醒</span></div></div>
          <div class="eh-li" v-if="qTodo"><span class="eh-v">资料与试验核对 <b>{{ qTodo }}</b> 项，在待我处理办理</span></div>
          <div class="zb-foot"><a @click="todoGo('quality')">质量台账</a><a v-if="has('conceal')" @click="todoGo('conceal')">隐蔽验收</a><a v-if="has('tickets')" @click="todoGo('tickets')">材料与砼 <ic n="chevron"></ic></a></div></div></section>

      <section :class="['zb-card', 'eh-dom', 'eh-tip', {shut: shut('safe')}]"><div class="zb-ch" @click="tog('safe')"><ic n="helmet"></ic><h2>安全提示</h2><span class="grow"></span><em class="zb-sys">只读</em></div>
        <div class="zb-body">
          <div class="eh-li" v-if="s.open"><span class="eh-v"><em class="zb-sys">按台账</em>隐患未闭环 <b>{{ s.open }}</b>（高风险 {{ s.high || 0 }}、超期 {{ s.overdue || 0 }}）</span></div>
          <div class="eh-li" v-else-if="s.total !== undefined"><span class="eh-v"><em class="zb-sys">按台账</em>当前无未闭环安全隐患</span></div>
          <div class="eh-li" v-if="units.length"><span class="eh-v" :title="units.map((u) => u.name + ' ' + u.open).join('；')">{{ units.map((u) => u.name + ' ' + u.open + (u.overdue ? '（超期 ' + u.overdue + '）' : '')).join('；') }}</span></div>
          <div class="eh-li eh-tall" v-for="t in tips" :key="t.title"><span class="eh-v" :title="t.detail"><span :class="['lv-dot', t.level]"></span>{{ t.title }}</span></div>
          <div class="zb-foot"><span class="tiny muted">派单、闭环归安全主管</span><a @click="todoGo('safety')">安全台账 <ic n="chevron"></ic></a></div></div></section>

    </div>

    <div class="zb-row eh-r2">
      <eh-todo :d="d" :tip="d.boundary" @changed="load"></eh-todo>
      <section :class="['zb-card', 'eh-tabs', {shut: shut('side')}]"><div class="zb-ch"><div class="eh-seg" role="tablist">
          <button v-for="t in sideTabs" :key="t.k" type="button" role="tab" :aria-selected="side === t.k" :class="{on: side === t.k}" @click="pickSide(t.k)">{{ t.name }}<b v-if="t.n">{{ t.n }}</b></button></div>
          <span class="grow"></span><help-tip v-if="sideTip" :text="sideTip"></help-tip><button type="button" class="zb-link eh-fold-t" v-if="narrow" @click="tog('side')">{{ shut('side') ? '展开' : '收起' }}</button></div>
        <div class="zb-body">
          <div class="zb-empty" v-if="!p">分包日志汇总加载中或暂不可用。</div>
          <template v-else-if="side === 'trend'"><eh-pair-bars v-if="trendOk" :days="p.trend" a="labor" b="machinery" a-name="人" b-name="机械台" label="近 14 天分包日志人数与机械台数"><span class="tiny muted">分包日志合计 · 无日志的日期不画柱</span></eh-pair-bars>
            <div class="zb-empty" v-else>近 14 天还没有分包日志，没有人数和台数可汇总。</div></template>
          <template v-else-if="side === 'block'"><div class="zb-btns eh-bk-n" v-if="p.blockers.counts.length"><span class="chip orange" v-for="c in p.blockers.counts" :key="c.type">{{ c.type }} {{ c.n }}</span></div>
            <template v-for="(b, i) in p.blockers.items.slice(0, 2)" :key="i"><div class="eh-bk"><div class="small eh-bk-t" :title="b.desc"><span class="chip">{{ b.type }}</span> {{ b.desc }}</div>
              <div class="tiny muted eh-bk-s" :title="b.measure">{{ b.date.slice(5) }} · {{ b.author }} · {{ ehShort(b.company) }}<span v-if="b.measure"> · 措施：{{ b.measure }}</span><span v-if="b.activity_code"> · 影响 {{ b.activity_short || b.activity_code }}</span></div></div></template>
            <button type="button" class="zb-link zb-more-all" v-if="p.blockers.items.length > 2" @click="bkAll = true">查看全部 {{ p.blockers.items.length }} 条<ic n="chevron"></ic></button>
            <zb-drawer v-if="bkAll" :title="'阻碍进度的问题 · 近 7 天 · ' + p.blockers.items.length + ' 条'" @close="bkAll = false">
              <zb-paged :items="p.blockers.items" :size="10" :m-size="5" unit="条" :fit="false"><template #default="{ item: b }"><div class="eh-bk"><div class="small eh-bk-t" :title="b.desc"><span class="chip">{{ b.type }}</span> {{ b.desc }}</div>
              <div class="tiny muted eh-bk-s" :title="b.measure">{{ b.date.slice(5) }} · {{ b.author }} · {{ ehShort(b.company) }}<span v-if="b.measure"> · 措施：{{ b.measure }}</span><span v-if="b.activity_code"> · 影响 {{ b.activity_short || b.activity_code }}</span></div></div></template></zb-paged></zb-drawer>
            <div class="zb-empty" v-if="!p.blockers.items.length">近 7 天日志里没有材料、设备、图纸、外部类阻碍。</div></template>
          <div class="zb-foot"><eh-extra home="eng"></eh-extra></div></div></section>
    </div>
    </template></div>`,
  data: () => ({ d: null, e: null, p: null, err: null, side: "trend", bkAll: false, weekOpen: false }),
  computed: {
    projName() { return store.me && store.me.project ? store.me.project.name : ""; },
    ov() { const o = {}; for (const x of (this.d && this.d.overview) || []) o[x.label] = x; return o; },
    p6() { return (this.e && this.e.p6) || (this.d && this.d.p6) || null; },
    q() { return (this.e && this.e.quality) || {}; },
    s() { return (this.p && this.p.kpis && this.p.kpis.safety) || {}; },
    slip() { return (this.e && this.e.slip) || []; },
    ms() { return (this.p && this.p.kpis && this.p.kpis.milestone) || null; },
    qTodo() { return this.d.items.filter((i) => i.module === "quality").length; },
    units() { return ((this.p && this.p.companies) || []).filter((c) => c.safety_open).map((c) => ({ name: ehShort(c.company), open: c.safety_open, overdue: c.safety_overdue })); },
    // 本岗相关提醒：简报里排第一的安全条目（只取标题，原文在悬停提示）；整改办理仍在安全台账
    tips() { return ((this.d.attention && this.d.attention.items) || []).filter((i) => i.area === "安全").slice(0, 1); },
    // 辅助分析三页签：分包单位、人机投入、阻碍问题（后两项依赖分包日志汇总，拿不到就不出）
    // 各分包今日记录、本周确认不是每天必看（RJ 10-10），移到“智眸现场 › 分包履约画像”页顶部
    subToday() { return typeof SubToday !== "undefined" ? SubToday : null; },   // x_subprofile.js 在本文件之后加载，用时再取
    sideTabs() { return this.p ? [{ k: "trend", name: "人机投入" }, { k: "block", name: "阻碍问题", n: this.p.blockers.items.length }] : []; },
    sideTip() {
      if (this.side === "trend") return "近 14 天分包日志里的人数与机械台数合计。";
      return "近 7 天日志里材料、设备、图纸、外部类阻碍。";
    },
    trendOk() { return this.p.trend.length > 1 && this.p.trend.some((t) => t.labor || t.machinery); },
    kpis() {
      const a = this.ov["今日随手记"], w = this.ov["本周确认进度"], ent = todoEntries(this.d.items), late = ent.filter((x) => x.overdue).length;
      return [
        { k: "note", label: "今日随手记", icon: "mic", value: a ? a.value : "—", sub: a ? a.sub || "" : "", route: "circle" },   // 随手记就是现场圈里的动态，今天的排在最上面
        { k: "todo", label: "待我处理", icon: "clipboard", value: ent.length, sub: late ? `其中已超期 ${late}` : "按期限排序", tone: late ? "orange" : "", fn: () => this.toTodo() },
        { k: "week", label: "本周确认", icon: "calendar", value: w ? w.value : "—", sub: w ? w.sub || "" : "", fn: () => { this.weekOpen = true; } },   // 就地抽屉：各分包本周确认明细（与分包履约画像同一张表）
        { k: "qual", label: "质量未闭环", icon: "shield", value: this.q.open || 0, sub: "当前台账" + (this.q.overdue ? ` · 超期 ${this.q.overdue}` : ""), tone: this.q.overdue ? "red" : "green", route: "quality" },
      ];
    },
  },
  mounted() { this.load(); },
  methods: {
    go, todoGo, ehShort,
    has(k) { return navRolePages(roleOf()).some((p) => p.k === k); },
    pickSide(k) { this.side = k; if (this.narrow) this.opened = { ...this.opened, side: true }; },
    toTodo() { const el = document.getElementById("eh-todo"); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); },
    async load() {
      try {
        const [d, e] = await Promise.all([GET("/api/todo"), GET("/api/eng/overview")]);
        this.d = d; this.e = e; this.err = null;
      } catch (x) { this.err = x; return; }
      // 分包单位、人机投入、阻碍问题是辅助块：单独加载，失败只影响这几块
      GET("/api/production/overview", { quiet: true }).then((p) => { this.p = p; }).catch(() => { this.p = null; });
    },
  },
};

// ============================================================================ 安全主管
// AI 候选确认：按照片成组，每组一张 4:3 现场照片 + 识别出的条目；【确认处理】就地展开 CandidateGroup（确认并派单 / 当场已纠正 / 不采纳）
const EhCands = {
  props: { groups: Array },
  emits: ["changed"],
  template: `<section class="zb-card eh-cand" id="eh-cand"><div class="zb-ch"><ic n="sparkles"></ic><h2>AI 候选确认</h2><span class="zb-n" v-if="n">{{ n }}</span><span class="grow"></span><span class="zb-src">AI 识别 · 人工确认后入台账</span></div>
    <div class="zb-empty" v-if="!groups.length">当前没有待确认的 AI 候选。现场照片识别出疑似隐患后会出现在这里。</div>
    <div class="eh-cg" v-for="g in groups.slice(0, 1)" :key="g.key"><eh-cand-row :g="g" @open="cur = g"></eh-cand-row></div>
    <div class="pm-more" v-if="groups.length > 1"><button class="zb-link" type="button" @click="all = true">另有 {{ groups.length - 1 }} 张照片的候选，查看全部 <ic n="chevron"></ic></button></div>
    <zb-drawer v-if="all" :title="'AI 候选确认 · ' + groups.length + ' 张照片'" :wide="true" @close="all = false">
      <zb-paged :items="groups" :size="6" :m-size="4" unit="张照片" :fit="false"><template #default="{ item: g }"><div class="eh-cg"><eh-cand-row :g="g" @open="cur = g"></eh-cand-row></div></template></zb-paged></zb-drawer>
    <zb-drawer v-if="cur" :title="'确认处理 · ' + cur.items.length + ' 项'" :wide="true" @close="cur = null">
      <eh-cand-row :g="cur" :bare="true"></eh-cand-row>
      <div class="eh-cg-x"><component :is="comp" v-if="comp" :group-key="cur.group_key || ''" :items="cur.items.map((i) => i.finding)" :default-assignee-id="cur.default_assignee_id" :default-due="null" @done="done" @changed="done"></component>
        <candidate-box v-else v-for="it in cur.items" :key="it.id" :f="it.finding" @changed="done"></candidate-box></div></zb-drawer></section>`,
  components: { CandidateBox, EhCandRow: {
    props: { g: Object, bare: Boolean },
    emits: ["open"],
    template: `<div class="eh-cg-h"><button type="button" class="zb-ph eh-ph" v-if="g.thumb" @click="store.viewer = '/api/media/' + g.thumb + '/file'" title="查看原图">
        <img v-if="!broken" :src="'/api/media/' + g.thumb + '/thumb'" alt="候选隐患所在现场照片" loading="lazy" @error="broken = true"><span v-else class="zb-ph-miss">照片文件不可用</span></button>
      <div class="eh-cg-b"><div class="eh-cg-t" v-for="it in (bare ? g.items : g.items.slice(0, 3))" :key="it.id"><span :class="['chip', it.tone === 'red' ? 'red' : 'orange']">{{ lv(it) }}</span><span :title="it.title">{{ it.title }}</span></div>
        <div class="tiny muted" v-if="!bare && g.items.length > 3">另有 {{ g.items.length - 3 }} 项，确认处理时逐条查看</div>
        <div class="tiny muted eh-cg-sc" v-if="scene" :title="scene">场景：{{ scene }}</div>
        <div class="zb-btns" v-if="!bare"><button class="btn primary sm" type="button" @click="$emit('open')">{{ '确认处理' + (g.items.length > 1 ? '（' + g.items.length + ' 项）' : '') }}</button>
          <a v-if="g.items.length === 1" @click="todoGo('finding?id=' + g.items[0].finding.id)">详情</a></div></div></div>`,
    data: () => ({ broken: false, store }),
    computed: { scene() { const f = this.g.items[0].finding; return (f && f.body && f.body.scene) || ""; } },
    methods: { todoGo, lv(it) { return (it.finding && it.finding.level_name) || (it.tone === "red" ? "高" : "中"); } },
  } },
  data: () => ({ all: false, cur: null }),
  computed: {
    n() { return this.groups.reduce((a, g) => a + g.items.length, 0); },
    comp() { return window.CandidateGroup || null; },
  },
  methods: { done() { this.cur = null; this.$emit("changed"); } },
};

const SafetyBoard = {
  mixins: [ehFold],
  components: { EhKpis, EhBrief, EhExtra, EhTodo, EhCands, EhPairBars },
  template: `<div class="eh-home">
    <section class="zb-hero"><div class="zb-hero-tx"><h1>{{ projName }}</h1><div class="zb-hero-sub">安全主管 · 智能管理台</div></div>
      <button class="btn sm eh-hero-btn" type="button" v-if="can('site_safety')" :disabled="scanning" @click="rescan" title="对未识别的现场照片重新跑一遍 AI 识别">{{ scanning ? '识别中' : '重新识别照片' }}</button></section>
    <load-state compact v-if="err && !d" :error="err" @retry="load"></load-state>
    <load-state compact v-else-if="!d" :loading="true"></load-state>
    <template v-if="d && s">
    <eh-kpis :list="kpis"></eh-kpis>
    <eh-brief :a="d.attention" @changed="load"></eh-brief>

    <div class="zb-row eh-r2 sd">
      <eh-todo :d="board" :tip="d.boundary" @changed="load" :note="cands.length ? 'AI 候选在右侧确认' : ''"></eh-todo>
      <div class="eh-side"><eh-cands :groups="cands" @changed="load"></eh-cands>
      <section :class="['zb-card', 'eh-tabs', {shut: shut('side')}]"><div class="zb-ch"><div class="eh-seg" role="tablist">
          <button v-for="t in sideTabs" :key="t.k" type="button" role="tab" :aria-selected="side === t.k" :class="{on: side === t.k}" @click="pickSide(t.k)">{{ t.name }}</button></div>
          <span class="grow"></span><span class="tiny muted eh-tabs-m">{{ sideMeta }}</span><help-tip :text="sideTip"></help-tip><button type="button" class="zb-link eh-fold-t" v-if="narrow" @click="tog('side')">{{ shut('side') ? '展开' : '收起' }}</button></div>
        <div class="zb-body">
          <template v-if="side === 'co'"><table class="eh-t" v-if="s.by_company.length"><thead><tr><th>单位</th><th>未闭环</th><th>超期</th><th>高风险</th><th>30天闭环率</th></tr></thead>
            <tbody><tr v-for="c in s.by_company" :key="c.company"><td :title="c.company"><b>{{ ehShort(c.company) }}</b></td><td>{{ c.open }}</td><td :class="{'tc-over': c.overdue}">{{ c.overdue }}</td><td>{{ c.high }}</td>
              <td :title="c.avg_days === null ? '' : '平均闭环 ' + c.avg_days + ' 天'">{{ c.close_rate === null ? '—' : pct(c.close_rate) }}</td></tr></tbody></table>
            <div class="zb-empty" v-else>还没有已确认的隐患记录。</div></template>
          <template v-else-if="side === 'cat'"><div class="eh-hb" v-for="c in s.by_category.slice(0, 5)" :key="c.name"><span :title="c.name">{{ c.name }}</span><i><b :style="{width: (c.n / catMax * 100) + '%'}"></b></i><em>{{ c.n }}</em></div>
            <div class="tiny muted" v-if="s.by_category.length > 5">另有 {{ s.by_category.length - 5 }} 类，见安全台账</div>
            <div class="zb-empty" v-if="!s.by_category.length">没有未闭环的隐患类别。</div></template>
          <template v-else><eh-pair-bars v-if="s.trend.length > 1" :days="s.trend" a="new" b="resolved" a-name="新增" b-name="闭环" label="近 14 天已确认隐患每日新增与闭环数">
              <span class="tiny muted">30 天闭环率 {{ k.close_rate_30d === null ? '—' : pct(k.close_rate_30d) }} · 平均 {{ k.avg_close_days === null ? '—' : k.avg_close_days + ' 天' }}</span></eh-pair-bars>
            <div class="zb-empty" v-else>还没有足够的隐患记录画趋势。</div></template>
          <div class="zb-foot"><eh-extra home="sdash"></eh-extra></div></div></section></div>
    </div>
    </template></div>`,
  data: () => ({ d: null, s: null, err: null, scanning: false, side: "co" }),
  computed: {
    projName() { return store.me && store.me.project ? store.me.project.name : ""; },
    k() { return this.s.kpis; },
    ov() { const o = {}; for (const x of (this.d && this.d.overview) || []) o[x.label] = x; return o; },
    weekDone() { const w = this.ov["本周已闭环"]; return w ? w.value : "—"; },
    // 候选只在右侧“AI 候选确认”一处展示与办理；待我处理表里不再重复
    cands() {
      const out = [], by = {};
      for (const it of this.d.items.filter((i) => i.source === "candidate")) {
        const key = it.group_key || "f:" + it.id;
        if (!by[key]) out.push((by[key] = { key, group_key: it.group_key, thumb: it.thumb, default_assignee_id: it.default_assignee_id ?? null, items: [] }));
        by[key].items.push(it);
      }
      return out;
    },
    board() { return { ...this.d, items: this.d.items.filter((i) => i.source !== "candidate") }; },
    catMax() { return Math.max(1, ...this.s.by_category.map((c) => c.n)); },
    sideTabs() { return [{ k: "co", name: "分包对比" }, { k: "cat", name: "隐患分布" }, { k: "trend", name: "近 14 天" }]; },
    sideMeta() { return this.side === "co" ? "全部分包单位" : this.side === "cat" ? "未闭环合计 " + this.k.open : "本周闭环 " + this.weekDone; },
    sideTip() {
      if (this.side === "co") return "三列有交叠（同一项可既超期又高风险），不横向相加；闭环率＝近 30 天发现且已闭环 ÷ 近 30 天发现，无样本为“—”。";
      if (this.side === "cat") return "按类别统计当前未闭环的已确认隐患。";
      return "新增按发现日、闭环按闭环日统计的已确认隐患。";
    },
    // 指标卡各自带筛选进安全页（mgr.js 的 FB_ONLY，与首页数字同一口径），不再四张卡进同一页
    kpis() {
      const k = this.k, nw = this.ov["今日新增隐患"], cn = this.cands.reduce((a, g) => a + g.items.length, 0);
      return [
        { k: "new", label: "今日新增", icon: "plus", value: nw ? nw.value : "—", sub: "已确认 · 按发现日", route: "safety?only=new" },
        { k: "cand", label: "待确认候选", icon: "sparkles", value: cn, sub: "AI 识别，待人工确认", tone: cn ? "orange" : "", fn: () => this.scrollTo("eh-cand") },
        { k: "late", label: "超期未闭环", icon: "clock", value: k.overdue, sub: `未闭环 ${k.open} 项中`, tone: k.overdue ? "red" : "", route: "safety?only=overdue" },
        { k: "high", label: "高风险", icon: "alert", value: k.high, sub: "未闭环中", tone: k.high ? "orange" : "", route: "safety?only=high" },
        { k: "done", label: "本周闭环", icon: "check-circle", value: this.weekDone, sub: k.review ? `另有整改待复核 ${k.review}` : "按闭环日", tone: "green", route: "safety?only=week" },
      ];
    },
  },
  mounted() { this.load(); },
  methods: {
    can, pct, ehShort,
    pickSide(k) { this.side = k; if (this.narrow) this.opened = { ...this.opened, side: true }; },
    scrollTo(id) { const el = document.getElementById(id); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); },
    async load() {
      try {
        const [d, s] = await Promise.all([GET("/api/todo"), GET("/api/safety/overview")]);
        this.d = d; this.s = s; this.err = null;
      } catch (x) { this.err = x; }
    },
    async rescan() {
      this.scanning = true;
      try { const r = await POST("/api/safety/rescan"); toast("识别 " + r.scanned + " 张，新增候选 " + r.created.length); this.load(); }
      catch (e) { toast(e.message, true); } finally { this.scanning = false; }
    },
  },
};
