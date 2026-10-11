/* 公司领导 · 多项目履约总览（RJ 10-10 采用 15_公司领导_多项目总览_修订；只读，没有确认、派单、审批、修改按钮）。每分钟自动刷新，可全屏展示。
   - 多项目结构：项目组合表按“领导可见的项目”逐行列出；数据接口只返回当前项目，其他项目行只给“切换查看”，不估算、不汇总；
     只有一个项目时如实写“其他项目尚未接入”，不造项目和对比；
   - 指标都取当前项目的台账：进度看已确认的红色预警（全部预警另写），质量/安全看未闭环；有多个项目时注明“当前项目”；
   - 需关注事项取今日简报：只有 source 为 ai 才写“AI按当前事实整理”；简报结论失效、改由系统按当前事实生成时（source === 'system'）如实标出并写原因（AC-3.5）；
     没有简报时按台账列出（标“按台账”），不冒充 AI 结论；
   - 产值是内部统计：周记录与系统核定分两列、不相加；币种未登记不加货币符号；系统核定不等于业主计量认证；
   - 分包安全表三列（未闭环/超期/高风险）有交叠，不横向相加；
   - 手机：标题、指标、项目组合、需关注事项在前，里程碑/质量/安全默认收起。 */
const LD_BRIEF_LABEL = { ai: "AI按当前事实整理", template: "AI 文字未通过核对，改用系统模板", system: "系统按当前事实生成" };
const LD_AREA = { 进度: 0, 质量: 1, 安全: 2 };
const LD_SHOW = 3;
const ldWan = (v) => (v == null ? "—" : (Math.round(v / 1000) / 10).toLocaleString("zh-CN"));   // 原值 → “万”，一位小数

const Cockpit = {
  template: `<div :class="['ld-home', full ? 'full' : '']">
  <section class="zb-hero ld-hero"><div class="zb-hero-tx"><page-head page="cockpit" title="多项目履约总览"><template #meta>公司领导 · 只读 · 已接入 {{ projects.length }} 个项目<template v-if="p6"> · P6 数据日期 {{ p6.data_date }}</template></template>
    <template #actions><button class="zb-chip ld-full" @click="toggleFull"><ic n="layers"></ic>{{ full ? '退出全屏' : '全屏展示' }}</button></template></page-head></div></section>
  <load-state compact v-if="err && !d" :error="err" @retry="load"></load-state>
  <load-state compact v-else-if="!d" :loading="true"></load-state>
  <template v-if="d">
  <div class="zb-kpis n4">
    <component :is="k.route ? 'button' : 'div'" v-for="k in kpis" :key="k.k" :class="['zb-kpi', k.tone, k.route ? '' : 'ld-static']" @click="k.route && go(k.route, k.params)">
      <span class="zb-kpi-ic"><ic :n="k.icon"></ic></span>
      <span class="zb-kpi-b"><span class="zb-kpi-l">{{ k.label }}</span><b class="zb-kpi-v">{{ k.value }}</b><span class="zb-kpi-s" :title="k.tip || k.sub">{{ k.sub }}</span></span>
      <ic n="chevron" class="zb-kpi-go" v-if="k.route"></ic></component></div>

  <div class="zb-row ld-r2">
    <section class="zb-card ld-port"><div class="zb-ch"><ic n="building"></ic><h2>项目组合</h2><span class="zb-n">{{ projects.length }}</span><span class="grow"></span>
        <span class="tiny muted">产值为上周内部统计 · {{ cur || '币种未登记' }}</span><help-tip text="系统核定不等于业主计量认证，产值口径与计量口径不同。"></help-tip></div>
      <table class="ld-pt"><thead><tr><th>项目</th><th>进度</th><th class="r">质量未闭环</th><th class="r">安全未闭环</th><th class="r">周记录产值</th><th class="r">系统核定产值</th><th></th></tr></thead>
        <tbody><tr v-for="p in rows" :key="p.id" :class="{on: p.current}"><td class="ld-pn"><b :title="p.name">{{ p.name }}</b><small v-if="p.sim">仿真项目</small></td>
          <template v-if="p.current"><td><span :class="['chip', prog.tone]">{{ prog.text }}</span></td><td class="r">{{ q.open || 0 }}</td>
            <td class="r"><b :class="{red: s.overdue}">{{ s.open || 0 }}</b></td><td class="r">{{ out.record }}</td><td class="r">{{ out.approved }}</td>
            <td class="r"><a class="zb-link" @click="go('progress', {tab: 'alerts'})">进度预警 <ic n="chevron"></ic></a></td></template>
          <template v-else><td colspan="5" class="tiny muted">数据按项目分开查看，此处不做跨项目估算</td>
            <td class="r"><button class="zb-link" :disabled="busy" @click="switchTo(p)">切换查看 <ic n="chevron"></ic></button></td></template></tr></tbody></table>
      <div class="ld-none" v-if="projects.length < 2"><ic n="file"></ic><b>其他项目尚未接入</b><small>接入后在此逐项目列出，不做估算或虚构对比</small></div></section>

    <section class="zb-card ld-focus"><div class="zb-ch"><ic n="clipboard"></ic><h2>需关注事项</h2><span class="zb-n" v-if="focus.length">{{ focus.length }}</span><span class="grow"></span>
        <span :class="['zb-src', brief && brief.source === 'ai' ? 'ai' : '']">{{ briefLabel }}<help-tip v-if="briefNote || stale" :text="[stale ? staleNote : '', briefNote].filter(Boolean).join('。')"></help-tip></span></div>
      <div class="ld-fi" v-for="(it, i) in shownFocus" :key="i"><span :class="['lv-dot', it.level]"></span>
        <div class="ld-ft"><b><em v-if="it.sys" class="zb-sys">{{ it.sys }}</em>[{{ it.area }}] {{ it.title }}</b><small :title="it.detail + (it.action ? ' → ' + it.action : '')">{{ it.detail }}<template v-if="it.action"> → {{ it.action }}</template></small></div>
        <a class="zb-link" v-if="it.area === '进度'" @click="go('progress', {tab: 'alerts'})">查看</a></div>
      <div class="pm-more" v-if="focus.length > LD_SHOW"><button class="zb-link" @click="all = true">查看全部 {{ focus.length }} 项 <ic n="chevron"></ic></button></div>
      <zb-drawer v-if="all" :title="'需关注事项 · ' + focus.length + ' 项'" @close="all = false"><zb-paged :items="focus" :size="10" :m-size="6" unit="项" :fit="false"><template #default="{ item: it }"><div class="ld-fi"><span :class="['lv-dot', it.level]"></span>
        <div class="ld-ft"><b><em v-if="it.sys" class="zb-sys">{{ it.sys }}</em>[{{ it.area }}] {{ it.title }}</b><small :title="it.detail + (it.action ? ' → ' + it.action : '')">{{ it.detail }}<template v-if="it.action"> → {{ it.action }}</template></small></div>
        <a class="zb-link" v-if="it.area === '进度'" @click="go('progress', {tab: 'alerts'})">查看</a></div></template></zb-paged></zb-drawer>
      <div class="zb-empty" v-if="!focus.length">当前没有需关注的进度、质量、安全事项。</div></section>
  </div>

  <div class="zb-row ld-r3">
    <section :class="['zb-card', {shut: shut('ms')}]"><div class="zb-ch" @click="tog('ms')"><ic n="flag"></ic><h2>进度里程碑</h2><span class="grow"></span><span class="tiny muted" v-if="ms.length">计划与模拟预测分开写</span></div>
      <div class="zb-body"><ol class="pm-ms" v-if="ms.length"><li v-for="m in ms" :key="m.code"><i></i><div><div class="pm-ms-h"><b>{{ m.short_name || m.name }}</b><span>计划 {{ m.p6_date || '—' }}</span></div>
          <div class="pm-ms-f" :title="m.prob_text">模拟 P50 {{ m.p50 || '—' }} · 按期概率估计 {{ m.prob_label || probText(m.on_time_prob) }}<template v-if="m.prob_detail"> · {{ m.prob_detail }}</template></div></div></li></ol>
        <div class="zb-empty" v-else>还没有里程碑预测，项目部导入 P6 计划并运行预测后显示。</div></div></section>

    <section :class="['zb-card', {shut: shut('q')}]"><div class="zb-ch" @click="tog('q')"><ic n="shield"></ic><h2>质量情况</h2><span class="grow"></span><span class="tiny muted">当前台账</span></div>
      <div class="zb-body"><div class="pm-q-zero" v-if="!q.open"><span class="pm-q-ok"><ic n="check-circle"></ic></span><div><b>质量未闭环 0</b><div class="tiny muted">当前台账没有未闭环质量问题</div></div></div>
        <div class="pm-q-nums" v-else><div><b>{{ q.open }}</b><span>未闭环</span></div><div :class="{red: q.overdue}"><b>{{ q.overdue || 0 }}</b><span>超期</span></div><div><b>{{ q.reminders || 0 }}</b><span>待执行质控提醒</span></div></div></div></section>

    <section :class="['zb-card', {shut: shut('s')}]"><div class="zb-ch" @click="tog('s')"><ic n="helmet"></ic><h2>安全情况</h2><span class="grow"></span><span class="tiny muted">累计闭环 {{ s.resolved || 0 }}</span></div>
      <div class="zb-body"><table class="ld-st" v-if="comps.length"><thead><tr><th>分包单位</th><th class="r">未闭环</th><th class="r">超期</th><th class="r">高风险</th></tr></thead>
          <tbody><tr v-for="c in comps" :key="c.company"><td :title="c.company">{{ unit(c.company) }}</td><td class="r">{{ c.safety_open }}</td>
            <td class="r"><b :class="{red: c.safety_overdue}">{{ c.safety_overdue }}</b></td><td class="r">{{ c.safety_high }}</td></tr></tbody></table>
        <div class="zb-empty" v-else>还没有分包单位的安全记录。</div>
        <div class="tiny muted ld-st-n" v-if="comps.length">三列有交叠（一条隐患可同时超期、高风险），不能相加</div></div></section>
  </div>
  </template></div>`,
  data: () => ({ d: null, comps: [], alerts: [], err: null, full: false, t: null, all: false, busy: false, opened: {}, LD_SHOW,
    narrow: window.matchMedia("(max-width: 640px)").matches }),
  computed: {
    projects() { const me = store.me || {}; return me.projects && me.projects.length ? me.projects : me.project ? [me.project] : []; },
    pid() { return store.me && store.me.project ? store.me.project.id : null; },
    sim() { const p = store.me && store.me.project; return !!(p && p.config && p.config.simulation); },
    rows() { return this.projects.map((p) => ({ ...p, current: p.id === this.pid, sim: p.id === this.pid && this.sim })); },
    k() { return (this.d && this.d.kpis) || {}; },
    q() { return this.k.quality || {}; },
    s() { return this.k.safety || {}; },
    p6() { return this.k.p6 || null; },
    // 进度看已确认（未关闭）的进度预警，口径与经理首页一致：主数是红色，全部预警另写
    alertN() { const n = { red: 0, orange: 0, yellow: 0, all: 0 }; for (const a of this.alerts) { n[a.level] = (n[a.level] || 0) + 1; n.all++; } return n; },
    prog() {
      const a = this.alertN;
      if (a.red) return { tone: "red", text: `红色预警 ${a.red}` };
      if (a.orange) return { tone: "orange", text: `橙色预警 ${a.orange}` };
      return a.all ? { tone: "", text: `黄色预警 ${a.yellow}` } : { tone: "green", text: "无已确认预警" };
    },
    scope() { return this.projects.length > 1 ? "当前项目 · " : ""; },
    kpis() {
      const a = this.alertN, n = this.projects.length;
      return [
        { k: "proj", label: "已接入项目", icon: "building", value: n, tone: "", sub: n > 1 ? "当前查看：" + ((store.me.project || {}).name || "") : "其他项目尚未接入" },
        { k: "prog", label: "进度红色预警", icon: "alert", value: a.red, tone: a.red ? "red" : "", route: "progress", params: { tab: "alerts" },
          sub: this.scope + `已确认 · 全部 ${a.all}`, tip: a.all ? `全部已确认进度预警 ${a.all}：红 ${a.red} · 橙 ${a.orange} · 黄 ${a.yellow}` : "" },
        { k: "qual", label: "质量未闭环", icon: "shield", value: this.q.open || 0, tone: this.q.overdue ? "red" : "green", sub: this.scope + "当前台账" + (this.q.overdue ? ` · 超期 ${this.q.overdue}` : "") },
        { k: "safe", label: "安全未闭环", icon: "helmet", value: this.s.open || 0, tone: this.s.overdue ? "orange" : "", sub: this.scope + `高风险 ${this.s.high || 0} · 超期 ${this.s.overdue || 0}` },
      ];
    },
    // 产值：上一个完整周。周记录与系统核定是两个口径，分两列写，不相加
    o() { return this.k.output || null; },
    cur() { return (this.o && this.o.currency) || ""; },
    out() {
      const o = this.o;
      if (!o) return { record: "—", approved: "—" };
      return { record: o.week_source === "record" ? ldWan(o.week) + " 万" : "—", approved: o.week_approved != null ? ldWan(o.week_approved) + " 万" : "—" };
    },
    brief() { return (this.d && this.d.brief) || null; },
    briefLabel() { return this.brief ? LD_BRIEF_LABEL[this.brief.source] || "今日简报" : "按台账整理"; },
    briefNote() {
      if (!this.brief) return "今日简报尚未生成（简报由项目部生成，领导只读），先按台账列出";
      return this.brief.created_at ? "生成于 " + fmtDT(this.brief.created_at) : "";
    },
    // 简报失效原因照写；“点刷新重新生成”是给项目部的提示，领导只读，不显示
    stale() { return !!(this.brief && this.brief.source === "system" && this.brief.note); },
    staleNote() { return (this.brief.note || "").replace(/[，,]?\s*点[“"]?刷新[”"]?[^。]*。?/, "。").replace(/。。$/, "。"); },
    // 简报事项按进度 → 质量 → 安全排；没有简报时按台账列红色预警和超期整改的单位
    focus() {
      if (this.brief) return [...(this.brief.items || [])].sort((x, y) => (LD_AREA[x.area] ?? 9) - (LD_AREA[y.area] ?? 9));
      const out = [], a = this.alertN;
      if (a.red) out.push({ level: "high", area: "进度", title: `已确认红色预警 ${a.red} 项`, detail: `全部已确认进度预警 ${a.all} 项`, sys: "按台账" });
      if (this.q.overdue) out.push({ level: "medium", area: "质量", title: `质量问题超期 ${this.q.overdue} 项`, detail: `质量未闭环 ${this.q.open} 项`, sys: "按台账" });
      for (const c of this.comps.filter((x) => x.safety_overdue))
        out.push({ level: "medium", area: "安全", title: `${this.unit(c.company)}超期整改 ${c.safety_overdue} 项`, detail: `该单位安全未闭环 ${c.safety_open} 项，其中高风险 ${c.safety_high} 项`, sys: "按台账" });
      return out;
    },
    shownFocus() { return this.focus.slice(0, LD_SHOW); },   // 其余在“查看全部”抽屉里分页看，首页不拉长
    ms() { return ((this.d && this.d.milestones) || []).slice(0, 3); },
  },
  async mounted() {
    await this.load(); this.t = setInterval(() => this.load(), 60000);
    this.mq = window.matchMedia("(max-width: 640px)"); this.onMq = (e) => { this.narrow = e.matches; }; this.mq.addEventListener("change", this.onMq);
  },
  unmounted() { clearInterval(this.t); if (this.mq) this.mq.removeEventListener("change", this.onMq); if (document.fullscreenElement) document.exitFullscreen().catch(() => null); },
  methods: {
    go, probText, fmtDT,
    unit(c) { return (c || "未登记单位").replace(/[（(].*?[）)]/g, ""); },   // 简称：“甲分包单位（路基桥梁）”→“甲分包单位”
    shut(k) { return this.narrow && !this.opened[k]; },
    tog(k) { if (this.narrow) this.opened = { ...this.opened, [k]: !this.opened[k] }; },
    async load() {
      try {
        const [d, comps, al] = await Promise.all([GET("/api/cockpit"), GET("/api/companies"), GET("/api/alerts?status=confirmed")]);
        this.d = d; this.comps = comps; this.alerts = al; this.err = null;
      } catch (e) { this.err = e; }
    },
    // 切换当前项目（领导与管理员可切换），切换后整页按新项目重新加载
    async switchTo(p) {
      this.busy = true;
      try { await PUT("/api/me/project", { project_id: p.id }); location.reload(); } catch (e) { toast(e.message, true); this.busy = false; }
    },
    toggleFull() {
      this.full = !this.full;
      try { if (this.full) document.documentElement.requestFullscreen(); else if (document.fullscreenElement) document.exitFullscreen(); } catch (_) { /* 不支持全屏时仍可用 */ }
    },
  },
};
