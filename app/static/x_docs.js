/* x_docs：工笔文书（工笔文书方案 2026-10-08，用户定名“工笔文书”）。
   - 工笔文书按用途分四个同级分区（RJ 10-10）：报表（日报、周报、月报 + 英文周报 / 月报）| 函件 | 例会 | 分析；
     GongbiHome 是一个分区的页面（页签选类型，起草与版本 + 可引用事实）；中文周报 / 月报仍走原来的周月报流程；
   - GongbiPanel：同一套步骤 AI 起草 → 人工核对（数字核对标注）→ 确认（致分包文书 = 确认下发）→ 导出 Word；确认后冻结，再起草即新版本；
   - LettersPage（#/letters）：致分包文书的接收与销项：分包签收、进度类书面回复；工程主管接受或退回；
   - 系统不对外发送任何文书：通知函发出后由人在索赔证据页登记“已发出”。 */
if (typeof STEP_FLOWS !== "undefined") STEP_FLOWS.gongbi = ["AI 起草", "人工核对", "确认", "导出 Word"];
if (typeof PAGE_HEADS !== "undefined") {
  PAGE_HEADS.letters = ["致分包文书", "项目部下发的工作联系单和函件：签收、回复、销项"];
}

const GB_ESC = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const GB_SHORT = { week: "周报", month: "月报", weekly_en: "英文周报", monthly_en: "英文月报", notice_of_claim: "索赔意向通知函", delay_brief: "延误分析简报",
  work_contact: "工作联系单", letter_to_sub: "致分包函" };

// 工笔文书按用途分四个同级分区（RJ 10-10）：报表（定期汇报）| 函件（发给具体单位）| 例会（会议闭环）| 分析（专题复盘）；每种文书只属于一个分区。
// 分区名即文书类型的 group（ai/docs_registry.py 等处的类型定义）；GB_TAB 给路由用（类型清单是异步取的，导航要同步判断）。
const GB_CATS = { en: "报表", letter: "函件", meet: "例会", ana: "分析" };
const GB_TAB = { weekly_en: "en", monthly_en: "en", notice_of_claim: "letter", work_contact: "letter", letter_to_sub: "letter", precontrol_notice: "letter", docdebt_notice: "letter",
  meeting_pack: "meet", three_week_plan: "meet", delay_brief: "ana", monthly_review: "ana" };
const gbTabOf = (kind) => GB_TAB[kind] || "";

// 一个分区的页面：页头 | 分区内的文书类型页签 | 起草与版本 + 可引用事实。
// 函件区另有“签收与回复”（工程主管、安全主管跟踪下发的致分包文书），例会区另有“行动项核对”（上次例会行动项闭环），都是本分区的同级页签，不另设入口。
// 数字只取既有接口：类型 /api/gongbi/types、各类已有份数 /api/gongbi/docs、台账数 /api/cockpit 与 /api/alerts、例会 /api/meetings/review；拿不到的块不显示，不补零。
const GongbiHome = {
  props: { cat: { type: String, default: "letter" }, start: String, only: String },   // only：报表里的英文周报 / 英文月报各占一个分页，只列这一种
  template: `<div class="gb-home rd-fx">
    <page-head v-if="cat === 'en'" page="rpt" :title="cur ? cur.zh : undefined"></page-head><page-head v-else-if="cat === 'meet'" page="gb_meet"></page-head>
    <page-head v-else-if="cat === 'ana'" page="gb_ana"></page-head><page-head v-else page="gb_letter"></page-head>
    <load-state compact v-if="err || !loaded" :loading="!loaded" :error="err" @retry="load"></load-state>
    <template v-else-if="segs.length">
      <div class="seg rpt-seg gb-seg" role="tablist" :aria-label="catName + '类型'" v-if="segs.length > 1">
        <button v-for="s in segs" :key="s.k" role="tab" :aria-selected="s.k === kind ? 'true' : 'false'" :class="{on: s.k === kind}" @click="pickSeg(s.k)">{{ s.label }}<small class="gb-en-t" v-if="s.en">EN</small><span class="zb-n" v-if="cnt[s.k]" :title="'已有 ' + cnt[s.k] + ' 份（各版本分别计）'">{{ cnt[s.k] }}</span></button></div>
      <component v-if="extra" :is="extra.comp" :embed="true" :key="extra.k"></component>
      <div class="gb-cols" v-else-if="cur">
        <div class="gb-main"><gongbi-panel :kind="kind" :type="cur" :preset="preset" :recent="recent" :key="kind + ':' + pick" @changed="loadCounts" @open="open"></gongbi-panel></div>
        <gb-side :type="cur" :rules="rules" :cat="cat"></gb-side></div></template>
    <section class="zb-card" v-else><div class="zb-empty">当前岗位没有可查看的{{ catName }}。</div></section></div>`,
  data: () => ({ types: [], rules: [], kind: "", preset: null, cnt: {}, docs: [], pick: 0, loaded: false, err: null }),
  computed: {
    catName() { return GB_CATS[this.cat] || "文书"; },
    list() { return this.types.filter((t) => !t.legacy && t.group === this.catName && (!this.only || t.kind === this.only)); },
    cur() { return this.list.find((t) => t.kind === this.kind) || null; },
    extras() {
      const role = roleOf(), out = [];
      if (this.cat === "meet" && ["manager", "eng_sup"].includes(role) && featOn("cmd") && typeof window.MeetingsPage !== "undefined") out.push({ k: "_actions", label: "行动项核对", comp: window.MeetingsPage, first: true });
      if (this.cat === "letter" && ["eng_sup", "safety_sup"].includes(role)) out.push({ k: "_letters", label: "签收与回复", comp: LettersPage });
      return out;
    },
    segs() {
      const ks = this.list.map((t) => ({ k: t.kind, label: GB_SHORT[t.kind] || t.zh, en: t.lang === "en" && this.cat !== "en" }));
      return this.extras.filter((x) => x.first).concat(ks, this.extras.filter((x) => !x.first));
    },
    extra() { return this.extras.find((x) => x.k === this.kind) || null; },
    // 空态里的最近文书：只列本分区的，按起草时间取 3 份，点开直达
    recent() {
      const name = (k) => (this.list.find((t) => t.kind === k) || {}).zh || GB_SHORT[k] || k;
      return this.docs.filter((x) => gbTabOf(x.kind) === this.cat).sort((a, b) => (a.created_at < b.created_at ? 1 : -1)).slice(0, 3)
        .map((x) => ({ key: "d" + x.id, title: x.title, type: name(x.kind), ok: x.status === "confirmed", okText: ["work_contact", "letter_to_sub"].includes(x.kind) ? "已下发" : "已确认",
          warn: x.warnings ? `${x.warnings} 处待核` : "", at: x.created_at, kind: x.kind, doc: x.id }));
    },
  },
  mounted() {
    const p = store.route.params || {};
    if (p.kind && gbTabOf(p.kind) === this.cat) { this.kind = p.kind; this.preset = { ref: p.ref || "", doc: p.doc || "" }; }
    else if (this.only || this.start) this.kind = this.only || this.start;
    this.load();
  },
  methods: {
    async load() {
      this.err = null;
      try {
        const r = await GET("/api/gongbi/types"); this.types = r.types || []; this.rules = r.rules || []; this.loaded = true;
        if (!this.cur && !this.extra) { this.kind = this.segs.length ? this.segs[0].k : ""; this.preset = null; }
        this.loadCounts();
      } catch (e) { this.err = e; }
    },
    loadCounts() {
      GET("/api/gongbi/docs", { quiet: true }).then((ds) => { const c = {}; ds.forEach((d) => { c[d.kind] = (c[d.kind] || 0) + 1; }); this.cnt = c; this.docs = ds; }).catch(() => { this.cnt = {}; this.docs = []; });
    },
    pickSeg(k) { this.kind = k; this.preset = null; },
    open(x) { this.kind = x.kind; this.preset = { ref: "", doc: String(x.doc) }; this.pick++; },
  },
};

// 例会分区（#/meetings，原“例会行动项”页的地址不变）：x_cmd.js 注册路由时若有本组件就用它
const MeetHub = { components: { GongbiHome }, template: `<gongbi-home cat="meet"></gongbi-home>` };

// 右栏：可引用事实（当前台账数，不是文书里冻结的数）、例会行动项、相关资料、起草规则。链接只给本岗位能打开的页面；
// 已确认进度预警、质量/安全台账在“可引用事实”数字卡上已有入口（带筛选），相关资料不再重复列（验收 I4）
const GB_SRC = {
  period: [["预测与赶工", "progress", { tab: "forecast" }], ["质量台账", "quality"], ["安全台账", "safety"]],
  event: [["部位档案与索赔证据", "claims"], ["预测与赶工", "progress", { tab: "forecast" }]],
  sub: [["质量台账", "quality"], ["安全台账", "safety"], ["开工条件", "gate"]],
};
const GbSide = {
  props: { type: Object, rules: Array, cat: String },
  template: `<aside class="gb-side">
    <section :class="['zb-card', 'gb-facts', {shut: shut('f')}]"><div class="zb-ch" @click="tog('f')"><ic n="database"></ic><h2>可引用事实</h2><span class="grow"></span>
        <i class="zb-sys">按台账</i><span class="tiny muted" v-if="day">{{ day.slice(5) }}</span></div>
      <div class="zb-body"><div class="gb-fx" v-if="cp"><button v-for="f in facts" :key="f.k" type="button" :class="['gb-f', f.tone]" :disabled="!f.can" @click="go(f.route, f.params)">
          <span class="gb-fl">{{ f.label }}<small>{{ f.sub }}</small></span><b>{{ f.v }}</b><ic n="chevron" v-if="f.can"></ic></button></div>
        <div class="zb-empty" v-else-if="cpErr">台账数暂时取不到。</div><div class="zb-empty" v-else>读取中…</div>
        <div class="tiny muted gb-fn">当前台账数；起草时系统按所选周期或对象重新取数，冻结在文书里。<help-tip v-if="rules && rules.length" caption="起草规则" label="起草规则"><ul class="gb-rules"><li v-for="r in rules" :key="r">{{ r }}</li></ul></help-tip></div></div></section>
    <section :class="['zb-card', {shut: shut('m')}]" v-if="mt && cat !== 'meet'"><div class="zb-ch" @click="tog('m')"><ic n="users"></ic><h2>例会行动项</h2><span class="grow"></span>
        <span class="tiny muted" v-if="mt.meeting">上次 {{ mt.meeting.date.slice(5) }}</span></div>
      <div class="zb-body gb-mtl"><div class="gb-mt" v-if="mt.stats && mt.stats.total">已确认 <b>{{ mt.stats.total }}</b> 项：已完成 {{ mt.stats.done }}，未完成 {{ mt.stats.open }}<template v-if="mt.stats.overdue">，<span class="gb-red">超期 {{ mt.stats.overdue }}</span></template></div>
        <div class="gb-mt muted" v-else>{{ mt.message || '上次例会没有已确认的行动项' }}</div><a class="gb-mtgo" @click="go('meetings')">去例会核对 <ic n="chevron"></ic></a></div></section>
    <section :class="['zb-card', {shut: shut('s')}]" v-if="links.length"><div class="zb-ch" @click="tog('s')"><ic n="folder"></ic><h2>相关资料</h2><span class="grow"></span></div>
      <div class="zb-body gb-lns"><a class="gb-ln" v-for="l in links" :key="l[0]" @click="go(l[1], l[2] || {})"><ic n="file"></ic><span>{{ l[0] }}</span><ic n="chevron"></ic></a></div></section>
</aside>`,
  data: () => ({ cp: null, cpErr: false, alerts: null, mt: null, narrow: window.matchMedia("(max-width: 640px)").matches, opened: {} }),
  computed: {
    day() { return this.cp && this.cp.kpis ? this.cp.kpis.day : ""; },
    facts() {
      const k = (this.cp && this.cp.kpis) || {}, q = k.quality || {}, s = k.safety || {}, a = { red: 0, orange: 0, yellow: 0, all: 0 };
      for (const x of this.alerts || []) { a[x.level] = (a[x.level] || 0) + 1; a.all++; }
      return [
        { k: "p", label: "已确认进度预警", v: this.alerts ? a.all : "—", tone: a.red ? "red" : "", route: "progress", params: { tab: "alerts", status: "confirmed" },
          sub: this.alerts ? `其中红 ${a.red}` + (a.orange ? ` · 橙 ${a.orange}` : "") + (a.yellow ? ` · 黄 ${a.yellow}` : "") : "未取到" },
        { k: "q", label: "质量问题未闭环", v: q.open == null ? "—" : q.open, tone: q.overdue ? "red" : q.open === 0 ? "green" : "", route: "quality", params: {}, sub: q.overdue ? `超期 ${q.overdue}` : "当前台账" },
        { k: "s", label: "安全隐患未闭环", v: s.open == null ? "—" : s.open, tone: s.overdue ? "orange" : s.open === 0 ? "green" : "", route: "safety", params: {}, sub: `高风险 ${s.high || 0} · 超期 ${s.overdue || 0}` },
      ].map((f) => ({ ...f, can: this.can(f.route, f.params) }));
    },
    links() {
      const r = this.type ? this.type.ref : "", key = ["week", "month"].includes(r) ? "period" : r === "event" ? "event" : "sub";
      const out = (GB_SRC[key] || []).slice();
      if (["week", "month"].includes(r)) out.push([r === "week" ? "中文周报" : "中文月报", "rpt", { seg: r }]);
      return out.filter((l) => this.can(l[1], l[2] || {}));
    },
  },
  mounted() {
    Promise.all([GET("/api/cockpit", { quiet: true }), GET("/api/alerts?status=confirmed", { quiet: true }).catch(() => null)])
      .then(([cp, al]) => { this.cp = cp; this.alerts = al; }).catch(() => { this.cpErr = true; });
    if (["manager", "eng_sup"].includes(roleOf()) && featOn("cmd")) GET("/api/meetings/review", { quiet: true }).then((r) => { this.mt = r; }).catch(() => { this.mt = null; });
  },
  methods: {
    go,
    can(route, params = {}) { return !!navMatch(roleOf(), route, params); },
    // 手机上辅助块默认收起，点标题展开；电脑端始终展开
    shut(k) { return this.narrow && !this.opened[k]; },
    tog(k) { if (this.narrow) this.opened = { ...this.opened, [k]: !this.opened[k] }; },
  },
};

const GongbiPanel = {
  props: { kind: String, type: Object, preset: Object, recent: Array },
  emits: ["changed", "open"],
  template: `<div class="gb">
    <section class="zb-card gb-head"><div class="zb-ch"><ic n="file"></ic><h2>{{ type.zh }}</h2><span class="chip blue" v-if="type.outward">对外</span><span class="chip" v-if="type.lang === 'en'">English</span></div>
      <div class="gb-intro"><span v-if="type.lang === 'en'" class="gb-en">{{ type.name }}</span>{{ type.intro }}</div>
      <div class="gb-make" v-if="type.can_draft">
        <label class="gb-ref" v-if="refKind === 'week' || refKind === 'month'"><ic n="calendar"></ic>{{ refKind === 'week' ? '报告周' : '报告月' }}包含日期 <input type="date" class="in" v-model="ref"></label>
        <select class="in gb-sel" v-else v-model="ref" :aria-label="refHint"><option value="">{{ refHint }}</option><option v-for="o in refs" :key="o.key" :value="o.key">{{ o.label }} · {{ o.notice }}</option></select>
        <button class="btn primary ai" :disabled="busy || !ref" @click="make"><ic n="sparkles"></ic>{{ busy ? '起草中 ' + sec + 's' : 'AI 起草' }}</button></div>
      <div class="tiny muted" v-else>{{ roNote }}</div></section>
    <load-state compact v-if="err || !loaded" :loading="!loaded" :error="err" @retry="load"></load-state>
    <template v-else-if="items.length">
      <section class="zb-card gb-vers"><div class="zb-ch"><ic n="clipboard"></ic><h2>{{ type.can_draft ? '已起草' : '可查看' }}</h2><span class="zb-n">{{ items.length }}</span></div>
        <zb-paged :items="items" :size="VERS" unit="份" :fit="false"><template #default="{ item: d }"><button type="button" :class="['gb-v', {on: d.id === sel}]" :aria-current="d.id === sel ? 'true' : null" @click="sel = d.id; reading = true">
          <span class="gb-vt">{{ d.title }}</span>
          <span class="gb-vc"><span :class="['chip', d.status === 'confirmed' ? 'green' : '']">{{ d.status === 'confirmed' ? (sub ? '已下发' : '已确认') : '草稿' }}</span><span class="chip">第 {{ d.version }} 版</span>
            <span :class="['chip', d.warnings ? 'orange' : d.fallback ? '' : 'green']" :title="d.fallback ? 'AI 稿有 ' + d.fallback + ' 个栏目数字对不上，已换成规则版文字' : ''">核对 · {{ d.warnings ? d.warnings + ' 处待核' : d.fallback ? d.fallback + ' 栏换规则版' : '通过' }}</span></span>
          <span class="gb-vm">{{ d.source === 'ai' ? 'AI 起草' : '规则版文字' }} · {{ fmtDT(d.created_at) }}</span></button></template></zb-paged></section>
      <zb-drawer wide v-if="sel && reading" :title="selTitle" @close="reading = false"><div class="gb-detail gb-read"><gongbi-doc :id="sel" :key="sel" @changed="load"></gongbi-doc></div></zb-drawer></template>
    <section class="zb-card gb-none" v-else><span class="gb-none-ic"><ic n="edit"></ic></span><b>{{ type.can_draft ? '尚未起草' : '暂无可查看的文书' }}</b><p>{{ emptyReason }}</p>
      <div class="gb-recent" v-if="recent && recent.length"><div class="gb-rh">最近文书</div>
        <button v-for="x in recent" :key="x.key" type="button" class="gb-rr" @click="$emit('open', x)"><ic n="file"></ic><span class="gb-rt"><b>{{ x.title }}</b><small>{{ x.type }} · {{ fmtDT(x.at) }}</small></span>
          <span class="gb-vc"><span :class="['chip', x.ok ? 'green' : '']">{{ x.ok ? x.okText : '草稿' }}</span><span class="chip orange" v-if="x.warn">{{ x.warn }}</span></span><ic n="chevron"></ic></button></div></section></div>`,
  // 一屏化（验收 I15）：选中的文书在右侧宽抽屉里阅读、核对、确认，不再接在列表下方把整页拉长
  data: () => ({ items: [], refs: [], ref: "", busy: false, sec: 0, loaded: false, err: null, sel: null, reading: false, VERS: 4 }),
  computed: {
    selTitle() { const d = this.items.find((x) => x.id === this.sel); return d ? d.title : "文书"; },
    refKind() { return this.type.ref; },
    sub() { return this.kind === "work_contact" || this.kind === "letter_to_sub"; },
    refHint() { return { event: "选择延误事件（按通知时效排序）", source: "选择已确认的隐患、质量问题或属实预警", company: "选择分包单位与问题类别" }[this.refKind] || "选择对象"; },
    roNote() {
      if (roleOf() === "leader") return "只读：只列已确认的文书。";
      return this.type.can_confirm ? "当前岗位不起草此类文书，可确认已起草的版本。" : "当前岗位只读。";
    },
    emptyReason() {
      if (!this.type.can_draft) return roleOf() === "leader" ? `还没有已确认的${this.type.zh}。` : `还没有${this.type.zh}，由负责岗位起草后在这里查看。`;
      return `先选择${this.refKind === "week" || this.refKind === "month" ? "周期" : "对象"}，再点“AI 起草”。数字由系统填写，AI 只组织文字；起草后人工核对、确认。`;
    },
  },
  mounted() { this.reset(); },
  methods: {
    fmtDT,
    reset() {
      this.items = []; this.sel = null; this.loaded = false;
      // 默认日期：英文周报报上一个完整周（取上周日）；例会底稿、三周滚动计划看本周往后，月度类取昨天
      if (this.refKind === "week" || this.refKind === "month") { const d = new Date(today()); d.setDate(d.getDate() - (this.kind === "weekly_en" ? d.getDay() || 7 : 1)); this.ref = d.toISOString().slice(0, 10); }
      else this.ref = (this.preset && this.preset.ref) || "";
      this.load();
      if (this.type.can_draft && !["week", "month"].includes(this.refKind)) GET("/api/gongbi/refs?kind=" + this.kind).then((r) => (this.refs = r)).catch(() => (this.refs = []));
    },
    async load() {
      this.err = null;
      try {
        this.items = await GET("/api/gongbi/docs?kind=" + this.kind); this.loaded = true;
        const want = this.preset && this.preset.doc ? Number(this.preset.doc) : null;
        if (want && this.items.some((d) => d.id === want)) { this.sel = want; this.reading = true; }
        else if (!this.items.some((d) => d.id === this.sel)) this.sel = this.items.length ? this.items[0].id : null;
      } catch (e) { this.err = e; }
    },
    async make() {
      this.busy = true; this.sec = 0; const t = setInterval(() => this.sec++, 1000);
      try { const d = await POST("/api/gongbi/docs", { kind: this.kind, ref: this.ref }); toast(d.note ? "已起草：" + d.note : "已起草，请人工核对"); await this.load(); this.sel = d.id; this.reading = true; this.$emit("changed"); }
      catch (e) { toast(e.message, true); } finally { clearInterval(t); this.busy = false; }
    },
  },
};

const GongbiDoc = {
  props: { id: Number },
  emits: ["changed"],
  template: `<div v-if="d" class="rv rv-embed gb-doc"><page-head page="report" :title="d.title" purpose="" steps="gongbi" :current="step">
      <template #actions><a class="btn" :href="'/api/gongbi/docs/' + d.id + '/docx'"><ic n="download"></ic>导出 Word</a>
        <template v-if="d.can_edit"><button class="btn" @click="save(false)">保存</button>
          <button class="btn primary" v-if="d.can_confirm" :disabled="left > 0" :title="left > 0 ? '还有 ' + left + ' 处没核实' : null" @click="save(true)">{{ confirmLabel }}</button></template></template></page-head>
    <div class="muted small mb">第 {{ d.version }} 版<template v-if="d.revision_of">（修订自上一版）</template> · {{ d.source === 'ai' ? 'AI 起草' : '规则版文字' }}<template v-if="d.logs"> · 依据日志 {{ d.logs }} 份</template>
      <template v-if="d.period_start"> · {{ d.period_start }}<template v-if="d.period_end && d.period_end !== d.period_start"> ～ {{ d.period_end }}</template></template></div>
    <div class="banner info" v-if="d.note">{{ d.note }}</div>
    <div class="banner warn" v-if="d.notice">通知期限 <b>{{ d.notice.deadline }}</b>（知悉日 {{ d.notice.aware_date }}，{{ d.notice.days }} 天，{{ d.notice.clause }}），当前：{{ d.notice.status_name }}。
      系统不对外发送；发出后请到 <a @click="go('claims')">索赔证据</a> 登记“已发出”。是否构成索赔事件以商务和合同部门判断为准。</div>
    <div class="banner info" v-if="sub && d.letter">已下发 · {{ d.letter.status_name }}<template v-if="d.letter.acked_at"> · 签收于 {{ fmtDT(d.letter.acked_at) }}</template><template v-if="!onLetters"> · <a @click="go('letters', {id: d.letter.id})">查看签收与回复</a></template></div>
    <div class="banner info" v-else-if="sub && d.status !== 'confirmed'">致：{{ d.company }}。{{ kind === 'letter_to_sub' ? '项目经理确认后才算正式下发，抄送该单位全部账号' : '签发人确认后下发到分包账号的待办和消息' }}；系统不对外发送，需要纸质版请导出 Word 盖章。</div>
    <div class="banner warn" v-if="checks.length && d.status !== 'confirmed'">人工核对：以下内容未在系统事实中找到或不符合口径，请核实或改掉<template v-if="d.can_confirm">；逐条勾选“已核实”后才能确认</template>：
      <span v-for="n in checks" :key="n.field + n.number + n.issue" class="nowrap gb-ck"><b>{{ secTitle(n.field) }}「{{ n.number }}」</b><small>{{ n.issue }}</small>
        <label class="tiny" v-if="d.can_confirm"> <input type="checkbox" v-model="ok[n.field + n.number + n.issue]">已核实</label></span></div>
    <div class="card" v-for="sec in d.template.sections" :key="sec.key">
      <div class="row between"><h2>{{ sec.title }}</h2><span class="tiny muted" v-if="sec.type === 'narrative' && d.can_edit">{{ (nar[sec.key] || '').length }}/{{ sec.max_chars }}</span></div>
      <template v-if="d.can_edit"><div class="tiny muted mb">写作要求：{{ sec.instruction }}<template v-if="d.fallback.includes(sec.key)"> · <b>本栏 AI 稿数字与系统事实对不上，已换成规则版文字</b></template></div>
        <textarea v-if="sec.type === 'narrative'" class="in" :rows="sec.max_chars > 400 ? 5 : 3" v-model="nar[sec.key]"></textarea>
        <textarea v-else class="in" rows="2" v-model="lists[sec.key]" placeholder="每行一条"></textarea></template>
      <template v-else><p class="rv-p" v-if="sec.type === 'narrative'" v-html="prose(sec.key, nar[sec.key])"></p>
        <ol class="rv-ol" v-else-if="listItems(sec.key).length"><li v-for="(x, i) in listItems(sec.key)" :key="i">{{ x }}</li></ol><p class="muted small" v-else>无</p></template>
      <div class="scroll-x mt" v-if="d.tables[sec.key] && d.tables[sec.key].rows.length"><zb-paged tag="table" cls="t" :items="d.tables[sec.key].rows" :size="10" :m-size="6" unit="行" :fit="false">
        <template #head><thead><tr><th v-for="h in d.tables[sec.key].headers" :key="h">{{ h }}</th></tr></thead></template>
        <template #default="{ item: row }"><tr><td v-for="(c, j) in row" :key="j">{{ c }}</td></tr></template></zb-paged></div></div>
    <div class="tiny muted gb-foot"><help-tip caption="数字与文字从哪来" text="表格数字由系统直接填写；文字由交融大模型起草，数字逐个与系统事实核对，核对不过的栏目退回规则版文字。确认后冻结，再次起草即为新版本。"></help-tip></div></div>
    <load-state v-else loading :error="err" @retry="load"></load-state>`,
  data: () => ({ d: null, err: null, nar: {}, lists: {}, ok: {} }),
  computed: {
    kind() { return this.d.kind; },
    sub() { return this.kind === "work_contact" || this.kind === "letter_to_sub"; },
    onLetters() { return store.route.name === "letters"; },   // 在致分包文书页里不再放指回本页的链接
    checks() { return this.d.check || []; },
    left() { return this.d.status === "confirmed" ? 0 : this.checks.filter((n) => !this.ok[n.field + n.number + n.issue]).length; },
    step() { return this.d.status === "confirmed" ? 3 : this.checks.length ? 1 : 2; },
    confirmLabel() { return this.kind === "letter_to_sub" ? "项目经理确认下发" : this.sub ? "确认下发" : "确认"; },
  },
  mounted() { this.load(); },
  methods: {
    go, fmtDT,
    async load() { this.err = null; try { this.apply(await GET("/api/gongbi/docs/" + this.id)); } catch (e) { this.err = e; } },
    apply(d) {
      this.d = d; this.nar = {}; this.lists = {}; this.ok = {};
      d.template.sections.forEach((s) => { if (s.type === "list") this.lists[s.key] = (d.narrative[s.key] || []).join("\n"); else this.nar[s.key] = d.narrative[s.key] || ""; });
    },
    secTitle(k) { const s = this.d.template.sections.find((x) => x.key === k); return s ? s.title.replace(/^[一二三四五六七八九十\d.]+[、.]\s*/, "") : k; },
    listItems(k) { return (this.lists[k] || "").split("\n").filter((x) => x.trim()); },
    prose(key, text) {
      let h = GB_ESC(text).replace(/\n/g, "<br>");
      h = h.replace(/\[([A-Z]{1,2}\d+)\]/g, '<span class="rv-cite">$1</span>');
      for (const n of this.checks.filter((x) => x.field === key && x.number)) {
        const t = GB_ESC(n.number).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        h = h.replace(new RegExp(t), '<mark class="rv-guard" title="' + GB_ESC(n.issue) + '">$&</mark>');
      }
      return h;
    },
    async save(confirm) {
      const narrative = { ...this.nar };
      Object.entries(this.lists).forEach(([k, v]) => (narrative[k] = v.split("\n").filter((x) => x.trim())));
      if (confirm && !window.confirm(this.sub ? "确认后文书冻结，并下发到分包账号的待办和消息（系统不对外发送）。继续？" : "确认后文书冻结，不能再改；需要修改请重新起草新版本。继续？")) return;
      try { this.apply(await PUT("/api/gongbi/docs/" + this.id, { narrative, confirm })); toast(confirm ? (this.sub ? "已确认下发" : "已确认") : "已保存"); this.$emit("changed"); }
      catch (e) { toast(e.message, true); }
    },
  },
};
GongbiPanel.components = { GongbiDoc };
GongbiHome.components = { GongbiPanel, GbSide };

// ---------------------------------------------------------------------------- 致分包文书：接收与销项
const LettersPage = {
  components: { GongbiDoc },
  props: { embed: Boolean },   // embed：函件分区里的“签收与回复”页签，不再出页头
  template: `<div class="xs rd-fx"><page-head page="letters" :title="isSub ? '项目部来函' : ''" v-if="!embed"></page-head>
    <load-state class="mb" compact v-if="err || !loaded" :loading="!loaded" :error="err" @retry="load"></load-state>
    <div class="gb-letters" v-if="items.length"><div class="zb-card rp-list rd-rpl"><div class="zb-ch rd-rpl-h"><ic n="send"></ic><h2>{{ isSub ? '来函' : '已下发' }}</h2><span class="zb-n">{{ items.length }}</span><span class="grow"></span><span class="zb-src" v-if="openN">未销项 {{ openN }}</span></div><zb-paged :items="items" :size="8" :m-size="5" unit="份" cls="rd-rpl-b"><template #default="{ item: x }"><div :class="['rp-li', {on: x.id === sel}]" role="button" tabindex="0" @click="pick(x.id)" @keyup.enter="pick(x.id)">
        <b>{{ x.title }}</b><div class="row wrap rp-chips"><span :class="['chip', tone(x)]">{{ x.status_name }}</span><span class="chip">{{ x.area_name }}</span><span class="chip red" v-if="x.overdue">已超期</span></div>
        <div class="tiny muted">{{ x.company }} · 下发 {{ fmtDT(x.issued_at) }}<template v-if="x.due_date"> · 期限 {{ x.due_date }}</template></div></div></template></zb-paged></div>
      <div v-if="v" class="gb-lv">
        <div class="zb-card gb-act"><div class="row between wrap"><div><b>{{ v.area_name }}类 · {{ v.status_name }}</b><div class="small muted">致 {{ v.company }}：{{ v.to.join('、') }}<template v-if="v.cc"> · 抄送 {{ v.cc }} 个账号</template>
              <template v-if="v.due_date"> · 期限 {{ v.due_date }}</template></div></div>
            <div class="row wrap"><button class="btn primary" v-if="v.can_ack" @click="ack">签收</button>
              <a class="btn" v-if="v.src_type === 'finding' && v.area !== 'progress'" @click="go('finding', {id: v.src_id})">去整改单上传整改照片</a></div></div>
          <div class="gb-reply" v-if="v.reply_text"><div class="small"><b>分包回复</b> · {{ fmtDT(v.reply_at) }} · 预计完成 {{ v.reply_date }}</div><p>{{ v.reply_text }}</p></div>
          <div class="banner warn" v-if="v.return_reason && v.status === 'returned'">回复被退回：{{ v.return_reason }}</div>
          <div class="gb-form" v-if="v.can_reply"><div class="small mb"><b>书面回复</b>：赶工措施及预计完成日期</div>
            <textarea class="in" rows="3" v-model.trim="reply" placeholder="例如：增加一台旋挖钻，两班作业"></textarea>
            <div class="row wrap mt"><label class="small">预计完成 <input type="date" class="in" v-model="finish"></label><button class="btn primary" :disabled="!reply || !finish" @click="send">提交回复</button></div></div>
          <div class="gb-form" v-if="v.can_review"><div class="small mb">接受回复只代表收到措施，<b>不改任何进度数据</b>；预警是否解除仍由 P6 和日志计算决定。</div>
            <div class="row wrap"><button class="btn primary" @click="review(true)">接受回复（销项）</button><input class="in" style="max-width:260px" v-model.trim="reason" placeholder="退回原因（退回时必填）"><button class="btn" :disabled="!reason" @click="review(false)">退回</button></div></div>
          <div class="tiny muted mt" v-if="v.area !== 'progress' && v.status !== 'closed'">安全、质量类按整改单的整改复核闭环，整改单闭环后本联系单自动销项。</div></div>
        <div class="zb-card gb-act"><button class="btn" type="button" @click="docOpen = true"><ic n="file"></ic>{{ isSub ? '查看来函全文' : '查看文书正文' }}</button><span class="tiny muted"> {{ v.doc.title }}</span></div>
        <zb-drawer wide v-if="!isSub && docOpen" :title="v.doc.title" @close="docOpen = false"><div class="gb-detail gb-read"><gongbi-doc :id="v.doc.id" :key="v.doc.id"></gongbi-doc></div></zb-drawer>
        <zb-drawer wide v-if="isSub && docOpen" :title="v.doc.title" @close="docOpen = false"><div class="gb-read"><div v-for="sec in v.doc.template.sections" :key="sec.key" class="gb-rs"><b>{{ sec.title }}</b>
          <p v-if="sec.type === 'narrative'">{{ v.doc.narrative[sec.key] }}</p><ol v-else><li v-for="(x, i) in v.doc.narrative[sec.key] || []" :key="i">{{ x }}</li></ol>
          <div class="scroll-x" v-if="v.doc.tables[sec.key] && v.doc.tables[sec.key].rows.length"><zb-paged tag="table" cls="t rv-t" :items="v.doc.tables[sec.key].rows" :size="10" :m-size="5" unit="行" :fit="false">
            <template #head><thead><tr><th v-for="h in v.doc.tables[sec.key].headers" :key="h">{{ h }}</th></tr></thead></template>
            <template #default="{ item: row }"><tr><td v-for="(c, j) in row" :key="j" :data-l="v.doc.tables[sec.key].headers[j]" :class="{nw: String(c).length <= 12}">{{ c }}</td></tr></template></zb-paged></div></div></div></zb-drawer></div></div>
    <section class="zb-card" v-else-if="loaded"><div class="zb-ch"><ic n="send"></ic><h2>{{ isSub ? '来函' : '已下发' }}</h2><span class="zb-n">0</span></div>
      <div class="zb-empty"><empty-state compact :reason="(isSub ? '暂无项目部来函' : '暂无致分包文书') + '。项目部下发工作联系单或函件后，会出现在这里和你的待办里。'"></empty-state></div></section></div>`,
  data: () => ({ items: [], sel: null, v: null, loaded: false, err: null, reply: "", finish: "", reason: "", docOpen: false }),
  computed: { isSub() { return ["tech", "sub_safety"].includes(roleOf()); }, openN() { return this.items.filter((x) => x.status !== "closed").length; } },
  mounted() { this.load(); },
  methods: {
    go, fmtDT,
    tone(x) { return { closed: "green", replied: "blue", returned: "orange", issued: "orange" }[x.status] || ""; },
    async load() {
      this.err = null;
      try {
        this.items = await GET("/api/gongbi/letters"); this.loaded = true;
        const want = Number(store.route.params.id || 0);
        this.pick(this.items.some((x) => x.id === want) ? want : this.sel && this.items.some((x) => x.id === this.sel) ? this.sel : this.items.length ? this.items[0].id : null);
      } catch (e) { this.err = e; }
    },
    async pick(id) { this.sel = id; this.v = null; this.docOpen = false; this.reply = ""; this.reason = ""; if (id) { try { this.v = await GET("/api/gongbi/letters/" + id); } catch (e) { toast(e.message, true); } } },
    async act(fn, msg) { try { this.v = await fn(); toast(msg); this.items = await GET("/api/gongbi/letters"); } catch (e) { toast(e.message, true); } },
    ack() { return this.act(() => POST(`/api/gongbi/letters/${this.sel}/ack`), "已签收"); },
    send() { return this.act(() => POST(`/api/gongbi/letters/${this.sel}/reply`, { text: this.reply, finish_date: this.finish }), "已提交回复"); },
    review(accept) { return this.act(() => POST(`/api/gongbi/letters/${this.sel}/review`, { accept, reason: this.reason }), accept ? "已接受，联系单销项" : "已退回"); },
  },
};

// #/letters：分包岗位（技术员、分包安全员）是“项目部来函”；工程主管、安全主管落到函件分区的“签收与回复”页签（通知里的链接照常打开）
const LettersRoute = {
  components: { LettersPage, GongbiHome },
  template: `<letters-page v-if="sub"></letters-page><gongbi-home v-else cat="letter" start="_letters"></gongbi-home>`,
  computed: { sub() { return ["tech", "sub_safety"].includes(roleOf()); } },
};
EXT.route("letters", LettersRoute);
