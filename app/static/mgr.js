/* 项目部（电脑端）：项目经理只关注 进度 / 质量 / 安全，不审核日志。项目首页 = 统一待办（todo.js：页头、概览条、待办表、需要关注），只放“要我决定的事”，其余按 进度 / 质量 / 安全 / 产值（只读） / 报表 / 智能应用 归集；Agent 卡片墙在“智能体与成效”页。 */

// 项目经理首页（RJ 10-10 采用 09_V8）：x_pmhome.js 的 PmHome；工程主管、安全主管首页在 x_enghome.js（D 线程）
const Workbench = {
  components: { PmHome },
  template: `<pm-home></pm-home>`,
};

// 质量 / 安全 看板
// 首页指标卡的筛选口径，与 services/todo.py 的首页数字同源：今日新增＝今天发现（按发现日）的已确认事项；超期、高风险＝待处理中的；本周闭环＝本周一起闭环的
const fbToday = () => (store.me && store.me.today) || new Date().toISOString().slice(0, 10);
const fbMonday = () => { const d = new Date(fbToday() + "T00:00:00"); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const FB_ONLY = {
  open: { name: "未闭环（不含质控提醒）", status: "active", test: (f) => f.kind !== "qc_reminder" },   // 首页“质量未闭环 N”点进来看到的就是这 N 条（验收 I5）
  new: { name: "今日新增", status: "", test: (f) => (f.created_at || "").slice(0, 10) === fbToday() },
  overdue: { name: "超期", status: "active", test: (f) => !!f.overdue },
  high: { name: "高风险", status: "active", test: (f) => f.level === "high" },
  week: { name: "本周闭环", status: "resolved", test: (f) => (f.resolved_at || "").slice(0, 10) >= fbMonday() },
};
const FindingBoard = {
  components: { FindingDetail, ListDigest },
  props: ["agent", "page"],
  template: `<div class="rs-board"><page-head :page="page" :steps="agent === 'safety' ? 'hazard' : ''" :current="stepNow" :counts="stepCounts">
    <template #meta>{{ scopeText }}<template v-if="agent === 'safety' && candidateN"> · {{ candidateN }} 条 AI 候选隐患待确认<template v-if="candHere">，<a @click="go(homeOf())">到首页待办里确认 →</a></template><template v-else>，由安全员、安全主管在待办里确认</template></template></template>
    <template #actions><div class="seg"><button v-for="s in tabs" :key="s[0]" :class="{on: status === s[0]}" @click="setStatus(s[0])">{{ s[1] }}</button></div>
      <button class="btn sm" v-if="agent === 'safety' && can('site_safety')" :disabled="!!busy" @click="rescan">{{ busy === 'scan' ? 'AI 识别中…' : '重新识别' }}</button>
      <button class="btn sm" v-if="agent === 'quality' && can('site_quality')" :disabled="!!busy" @click="qc">{{ busy === 'qc' ? 'AI 生成中…' : '生成质控提醒' }}</button></template></page-head>
  <div class="tiny muted mb" v-if="busy" role="status">{{ busy === 'scan' ? 'AI 正在逐张识别照片，照片多时要几分钟，可以先做别的' : 'AI 正在按工序生成质控提醒，请稍候' }}</div>
  <load-state :loading="!loaded" :error="err" @retry="load"></load-state>
  <div class="zb-card rs-dg-m" v-if="loaded && items.length > 1"><div class="rs-dg-l"><span class="zb-sys">系统统计</span>{{ dg.lead }}</div>
    <button type="button" class="zb-link" @click="dgOpen = true">看{{ agent === 'safety' ? '隐患' : '质量' }}小结 <ic n="chevron"></ic></button></div>
  <zb-drawer v-if="dgOpen && loaded && items.length > 1" :wide="true" :title="agent === 'safety' ? '隐患小结' : '质量小结'" @close="dgOpen = false">
  <list-digest :kind="agent === 'safety' ? 'safety' : 'quality'" :title="agent === 'safety' ? '隐患小结' : '质量小结'" :lead="dg.lead"
    key-title="需要先处理" :key-hint="agent === 'safety' ? '高风险且已超期' : '已超期'" :keys="dg.keys" :groups="dg.groups">
    <template #acts="{ g }"><div class="ad-acts" v-if="can('nudge') && g.nudge.length"><button class="btn sm" :disabled="!!nbusy" @click="nudgeAll(g)">{{ nbusy === g.k ? '催办中…' : '催办这 ' + g.nudge.length + ' 项的责任人' }}</button>
      <help-tip text="逐条发催办，和在单条事项上点“催办”一样"></help-tip></div></template></list-digest></zb-drawer>
  <div class="rs-only-m" v-if="loaded && !split && onlyName"><button type="button" class="zb-chip rs-only" @click="clearOnly">只看{{ onlyName }} ×</button></div>
  <div class="fb-split rs-split" v-if="loaded && split && items.length"><div class="zb-card rs-list"><div class="zb-ch"><ic :n="agent === 'safety' ? 'shield' : 'search'"></ic><h2>{{ listTitle }}</h2><span class="zb-n">{{ items.length }}</span><button type="button" class="zb-chip rs-only" v-if="onlyName" @click="clearOnly" title="取消筛选">只看{{ onlyName }} ×</button></div>
      <zb-paged :items="items" :size="8" :fit="200" unit="项" cls="rs-list-b"><template #default="{ item: f }"><div :class="['rs-li', {on: f.id === sel}]" role="button" tabindex="0" :aria-current="f.id === sel ? 'true' : null" @click="sel = f.id" @keyup.enter="sel = f.id">
        <ev-photo size="s" :src="thumbOf(f)" :full="fullOf(f)" :label="f.ref_type === 'media' ? '' : srcName(f)"></ev-photo>
        <div class="rs-li-b"><b><span :class="['chip', LEVEL_F[f.level]]">{{ f.level_name }}</span>{{ f.title }}</b>
          <div class="tiny muted rs-li-m"><span>{{ f.assignee || '未指派' }} · {{ srcName(f) }}</span><b :class="{'tc-over': f.overdue}">{{ f.overdue ? '超期 ' : '期限 ' }}{{ (f.due_date || '—').slice(5) || '—' }}</b></div></div></div></template></zb-paged></div>
    <div class="fb-detail rs-fd-pane" v-fit="240"><finding-detail :id="sel" :embed="true"></finding-detail></div></div>
  <zb-paged v-else-if="loaded && items.length" :items="items" :size="8" :m-size="4" unit="项" cls="fb-grid rs-cards"><template #default="{ item: f }"><div class="zb-card rs-card" role="button" tabindex="0" @click="go('finding', {id: f.id})" @keyup.enter="go('finding', {id: f.id})">
    <ev-photo size="s" :src="thumbOf(f)" :full="fullOf(f)" :label="f.ref_type === 'media' ? '' : srcName(f)"></ev-photo>
    <div class="rs-li-b"><div class="row wrap"><span :class="['chip', LEVEL_F[f.level]]">{{ f.level_name }}</span><span class="chip">{{ f.status_name }}</span>
        <span :class="['tiny', {'tc-over': f.overdue}]">{{ f.overdue ? '超期' : '期限' }} {{ (f.due_date || '—').slice(5) || '—' }}</span></div>
      <b class="rs-card-t">{{ f.title }}</b><div class="tiny muted">{{ f.assignee || '未指派' }} · {{ srcName(f) }}</div></div></div></template></zb-paged>
  <div class="zb-card rs-empty" v-if="loaded && !items.length"><div class="zb-ch"><ic :n="agent === 'safety' ? 'shield' : 'search'"></ic><h2>{{ listTitle }}</h2><span class="zb-n">0</span></div>
    <div class="zb-empty"><b>{{ emptyTitle }}</b><p>{{ emptyReason }}</p><a class="zb-link" v-if="status !== 'active'" @click="setStatus('active')">看待处理的 →</a>
      <a class="zb-link" v-else-if="agent === 'safety' && isSubOrg()" @click="go('shome')">去随手拍 →</a></div></div>
  <ai-note :page="agent === 'safety' ? 'safety' : 'quality'"></ai-note></div>`,
  // 首页指标卡带 ?only= 进来（今日新增 / 超期 / 高风险 / 本周闭环）：先按对应状态取数，再按与首页同一口径就地筛选（RJ 10-10：指标卡各自指向明细）
  data: () => { const o = FB_ONLY[store.route.params.only] ? store.route.params.only : ""; return { only: o, items: [], activeItems: [], candidateN: 0, loaded: false, err: null, busy: "", status: o ? FB_ONLY[o].status : "active", LEVEL_F, wide: false, sel: null, nbusy: "", dgOpen: false }; },
  computed: {
    // 小结（2026-10-08）：只用本页列表已有字段归类——等级、是否超期、类别（标题冒号前）、责任人、来源；数字都是条数，不另算口径
    dg() {
      const its = this.items, word = this.agent === "safety" ? "隐患" : "问题", cat = (f) => (String(f.title).split(/[：:]/)[1] ? String(f.title).split(/[：:]/)[0] : f.kind === "reported" ? "日志上报" : "其他");
      const st = { active: "待处理", resolved: "已闭环", dismissed: "已忽略" }[this.status] || "";
      const lv = (k) => its.filter((f) => f.level === k).length, over = its.filter((f) => f.overdue).length;
      const bits = [["high", "高"], ["medium", "中"], ["low", "低"]].filter(([k]) => lv(k)).map(([k, t]) => `${t} ${lv(k)}`);
      const isKey = (f) => (this.agent === "safety" ? f.level === "high" && f.overdue : f.overdue);
      const keys = its.filter(isKey).map((f) => ({ id: f.id, title: f.title, tag: f.level_name, tone: LEVEL_F[f.level],
        why: [f.assignee || "未指派", f.due_date ? "期限 " + f.due_date.slice(5) : ""].filter(Boolean).join(" · "), route: "finding", params: { id: f.id } }));
      const by = {};
      for (const f of its.filter((x) => !isKey(x))) (by[cat(f)] = by[cat(f)] || []).push(f);
      const groups = Object.entries(by).sort((a, b) => b[1].length - a[1].length).map(([c, fs]) => {
        const people = [...new Set(fs.map((f) => f.assignee || "未指派"))], o = fs.filter((f) => f.overdue).length;
        const src = fs.every((f) => f.kind === "reported") ? "都来自日志上报" : fs.every((f) => f.kind !== "reported" && f.kind !== "qc_reminder") ? "都是 AI 发现、已确认" : "";
        return { k: c, title: `${fs.length} 项“${c}”`, tag: fs.some((f) => f.level === "high") ? "含高风险" : "中低", tone: fs.some((f) => f.level === "high") ? "red" : "orange",
          text: [`责任人 ${people.slice(0, 3).join("、")}${people.length > 3 ? " 等" : ""}`, o ? `其中 ${o} 项已超期` : "都在期限内", src].filter(Boolean).join("，"),
          nudge: fs.filter((f) => f.overdue && f.assignee_id && ["open", "rejected"].includes(f.status)) };
      });
      const lead = `共 ${its.length} 项${st}${word}（${bits.join(" · ")}）` + (over ? `，其中 ${over} 项已超期` : "") +
        (keys.length ? `；${keys.length} 项${this.agent === "safety" ? "高风险且已超期" : "已超期"}，需要先处理` : "") + (groups.length ? `；其余按类别归成 ${groups.length} 组` : "") + "。";
      return { lead, keys, groups };
    },
    // 宽屏（≥1100px）左列表右详情：右侧就是隐患详情本身（同一组件、同一套操作），窄屏仍是卡片，点开进详情页
    split() { return this.wide; },
    tabs: () => [["", "全部"], ["active", "待处理"], ["resolved", "已闭环"], ["dismissed", "已忽略"]],   // “全部”排第一（RJ 10-09），默认仍看待处理
    // 候选只在待办里确认（同一条候选只有一个决策入口）：安全员、安全主管的首页待办里有候选，其余岗位只看数量
    candHere: () => ["safety_sup", "sub_safety"].includes(roleOf()),
    // 隐患流程各阶段的真实数量（候选 / 确认派单 / 整改 / 复查闭环），当前步骤 = 第一个有数量的阶段
    stepCounts() { return this.agent === "safety" ? hazardCounts(this.activeItems, this.candidateN) : []; },
    stepNow() { return this.stepCounts.findIndex((n) => n > 0); },
    emptyTitle() {
      const st = { "": "全部", active: "待处理", resolved: "已闭环", dismissed: "已忽略" }[this.status] || "";
      return `${st}：没有${this.agent === "safety" ? "隐患" : "质量问题或质控提醒"}`;
    },
    emptyReason() {
      if (this.status && this.status !== "active") return "这个分类下还没有记录。";
      if (this.agent === "safety") return "照片识别出的新隐患先作为 AI 候选，经安全员或安全主管确认后才进入台账。";
      return "质量问题与质控提醒闭环后在“已闭环”里查看。" + (can("site_quality") ? "质控提醒可在页头“生成质控提醒”按工序生成。" : "");
    },
    // 页头一行：本页范围（分包岗位只看本单位，接口按单位过滤）
    scopeText() { const u = store.me && store.me.user; return isSubOrg() ? `本单位：${(u && u.company) || "—"}` : "全项目"; },
    onlyName() { return this.only ? FB_ONLY[this.only].name : ""; },
    listTitle() { return (this.agent === "safety" ? "隐患" : "质量事项") + ({ "": "（全部）", active: "（待处理）", resolved: "（已闭环）", dismissed: "（已忽略）" }[this.status] || ""); },
  },
  mounted() { this.mq = window.matchMedia("(min-width: 1100px)"); this.wide = this.mq.matches; this.onMq = (e) => { this.wide = e.matches; }; this.mq.addEventListener("change", this.onMq); this.load(); },
  unmounted() { if (this.mq) this.mq.removeEventListener("change", this.onMq); },
  watch: { items(v) { if (!v.some((f) => f.id === this.sel)) this.sel = v.length ? v[0].id : null; } },
  methods: {
    go, can, isSubOrg, homeOf,
    setStatus(s) { this.status = s; this.only = ""; this.load(); },
    clearOnly() { this.only = ""; this.load(); },
    // 快速切换分类时只认最后一次请求的结果（先发后到的旧结果不覆盖当前列表）
    async load() {
      const n = (this.seq = (this.seq || 0) + 1);
      this.err = null;
      let items;
      try { items = await GET(`/api/findings?agent=${this.agent}&status=${this.status}`); } catch (e) { if (n === this.seq) this.err = e; return; }
      if (n !== this.seq) return;
      this.items = this.only ? items.filter(FB_ONLY[this.only].test) : items;
      if (this.agent === "safety") {  // 流程各阶段的数量：待处理的已确认隐患 + 待确认候选
        try {
          const act = this.status === "active" ? items : await GET("/api/findings?agent=safety&status=active");
          const cn = (await GET("/api/findings?agent=safety&candidates=only", { quiet: true })).length;
          if (n === this.seq) { this.activeItems = act; this.candidateN = cn; }
        } catch (_) { if (n === this.seq) { this.activeItems = []; this.candidateN = 0; } }
      }
      if (n === this.seq) this.loaded = true;
    },
    srcName(f) { return f.kind === "reported" ? "日志上报" : f.kind === "qc_reminder" ? "质控提醒" : (f.body && f.body.source === "扫码") ? "扫码上报" : "AI 发现"; },
    thumbOf(f) { return f.ref_type === "media" ? `/api/media/${f.ref_id}/thumb` : ""; },
    fullOf(f) { return f.ref_type === "media" ? `/api/media/${f.ref_id}/file` : ""; },
    // 批量催办：对组内每条超期且已指派的事项调用原来的催办接口（与单条“催办”相同，后端照常留痕、通知责任人）
    async nudgeAll(g) {
      this.nbusy = g.k; let ok = 0;
      try { for (const f of g.nudge) { await POST(`/api/findings/${f.id}/nudge`, { text: "" }); ok++; } toast(`已催办 ${ok} 项`); }
      catch (e) { toast((ok ? `已催办 ${ok} 项，` : "") + e.message, true); } finally { this.nbusy = ""; }
    },
    async rescan() { await this.ai("scan", async () => { const r = await POST("/api/safety/rescan"); toast(`识别 ${r.scanned} 张，新增候选隐患 ${r.created.length}`); }); },
    async qc() { await this.ai("qc", async () => { const r = await POST("/api/quality/reminders"); toast(`新增质控提醒 ${r.created} 条`); }); },
    // 调 AI 的按钮：进行中禁用并写明在做什么，防止重复点（S-7）
    async ai(kind, fn) {
      if (this.busy) return;
      this.busy = kind;
      try { await fn(); this.load(); } catch (e) { toast(e.message, true); } finally { this.busy = ""; }
    },
  },
};

// 质量、安全与进度同级，各自独立成页
const QualityHub = {
  components: { FindingBoard },
  template: `<finding-board agent="quality" page="quality"></finding-board>`,
};

const SafetyHub = {
  components: { FindingBoard },
  template: `<finding-board agent="safety" page="safety"></finding-board>`,
};

// 进度：预测 / 预警 / P6
// 预测与赶工（10-10 改版）：态势（三个模拟完工日期，标明是模拟预测、不是实际日期）→ 节点 → 赶工方案（“查看赶工方案”：有真实方案就地展开查阅，
// 没有写“尚未生成”并在原处给生成动作，不假装已有结果）→ 预测关键路径、工效偏低 → 雨季与节假日。写按钮只给 progress_ops（领导只读，后端同样 403）。
// 一屏化（10-10）：电脑一屏——方法说明收进“预测方法”小问号；节点至多 3 个（其余“查看全部”抽屉）；关键路径面板内滚动；赶工方案全文、雨季与节假日明细进抽屉。
const Forecast = {
  template: `<div class="rp-fc">
  <div class="rp-bar"><span class="rp-meta" v-if="f && !f.none"><span class="chip">模拟预测</span>计算于 {{ fmtDT(f.created_at) }} · {{ f.iterations }} 次模拟 · P6 数据日期 {{ f.data_date || '—' }}<b class="tc-over" v-if="f.stale_p6"> · 与当前 P6 批次（{{ f.current_data_date }}）不一致，预测已过期，请重新预测</b>
      <help-tip v-if="loaded" label="预测方法" :text="methodNote"></help-tip></span><span class="grow"></span>
    <button class="btn sm" v-if="can('progress_ops')" :disabled="busy" @click="run"><ic n="refresh"></ic>{{ busy ? '计算中' : '重新预测' }}</button></div>
  <empty-state v-if="loaded && (!f || f.none)" :reason="emptyReason" :action="emptyAction"></empty-state>
  <template v-else-if="loaded">
    <div class="zb-kpis n3 rp-kpis">
      <div class="zb-kpi rp-static"><span class="zb-kpi-ic"><ic n="calendar"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">预测完工<span class="rp-dt">（确定性）</span><help-tip text="按日志实测进度和工效，沿 P6 逻辑关系推算出的单一完工日期，没有考虑工效波动。"></help-tip></span>
        <b class="zb-kpi-v">{{ f.project_finish.forecast || '—' }}</b><span class="zb-kpi-s">模拟预测，非实际日期</span></span></div>
      <div class="zb-kpi rp-static"><span class="zb-kpi-ic"><ic n="target"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">完工 P50<help-tip text="按工效波动做多次模拟：一半的模拟结果早于这个日期，一半晚于，可理解为“五五开”的完工日。"></help-tip></span>
        <b class="zb-kpi-v">{{ f.project_finish.p50 || '—' }}</b><span class="zb-kpi-s">{{ f.iterations }} 次模拟的中位</span></span></div>
      <div class="zb-kpi rp-static"><span class="zb-kpi-ic"><ic n="flag"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">完工 P80<help-tip text="100 次模拟里约 80 次不晚于这个日期，比 P50 稳妥，可作对外承诺的参考。"></help-tip></span>
        <b class="zb-kpi-v">{{ f.project_finish.p80 || '—' }}</b><span class="zb-kpi-s">80% 把握不晚于此日</span></span></div></div>
    <zb-more :items="f.milestones" :limit="3" :m-limit="2" title="节点" unit="个" cls="fc-ms"><template #default="{ item: m }"><section class="zb-card rp-ms" :key="m.code">
      <div class="zb-ch"><ic n="flag"></ic><h2><act-name :c="m.code" :n="m.name" :s="m.short_name"></act-name></h2><span class="grow"></span>
        <div :class="['fc-prob', probClass(m.on_time_prob)]"><b>{{ m.prob_label || probText(m.on_time_prob) }}</b><span>{{ m.prob_detail || '按期概率' }}<help-tip text="按期概率＝模拟中这个节点不晚于 P6 计划日期的次数占比。例如 70% 表示 100 次模拟里约 70 次能按期。"></help-tip></span></div></div>
      <div class="rp-ms-d"><span>P6 计划 <b>{{ m.p6_date || '—' }}</b></span><span>模拟预测 <b>{{ m.forecast || '—' }}</b></span>
        <span class="muted">偏差 {{ m.slip_days > 0 ? '+' + fmtN(m.slip_days) + ' 天' : fmtN(m.slip_days) + ' 天' }}<help-tip text="偏差＝预测日期减 P6 计划日期：正数表示预计晚于计划，负数表示早于计划。"></help-tip></span></div>
      <div class="tiny fc-cd" v-if="cdMap[m.code]">控制日期 {{ cdMap[m.code].deadline }}（{{ cdMap[m.code].type }}）· {{ cdMap[m.code].days_left >= 0 ? '还剩 ' + cdMap[m.code].days_left + ' 天' : '已过 ' + (-cdMap[m.code].days_left) + ' 天' }}
        <template v-if="cdMap[m.code].margin_days !== null"> · 预测余量 <b :class="cdMap[m.code].margin_days < 0 ? 'neg' : 'pos'">{{ cdMap[m.code].margin_days > 0 ? '+' : '' }}{{ cdMap[m.code].margin_days }} 天</b><help-tip text="预测余量＝控制日期减预测日期：正数是还剩的缓冲天数，负数表示按预测已超出控制日期。"></help-tip></template></div>
      <div class="fc-axis" :aria-label="'P6 计划 ' + m.p6_date + '，预测 ' + m.forecast + '，P50 ' + m.p50 + '，P80 ' + m.p80">
        <div class="fc-bar"></div><div class="fc-band" :style="band(m)"></div><div class="fc-p6" :style="{left: at(m, m.p6_date)}"></div><div class="fc-dot" :style="{left: at(m, m.forecast)}"></div>
        <span class="fc-lb t" :style="{left: at(m, m.p6_date)}"><b>P6 计划</b> {{ m.p6_date.slice(5) }}</span>
        <span class="fc-lb b" :style="{left: at(m, m.p50)}">P50 {{ m.p50.slice(5) }}</span><span class="fc-lb b" :style="{left: at(m, m.p80)}">P80 {{ m.p80.slice(5) }}</span></div></section></template></zb-more>
    <div class="fc-low">
      <section class="zb-card fc-cp"><div class="zb-ch"><ic n="trend"></ic><h2>预测关键路径</h2><help-tip text="按预测会决定完工日的作业链，按预测完成日排列。右侧数字是预测比 P6 计划晚（+）或早（−）的天数；标签是预测依据（实测工效或 P6 原定工期）。日期为模拟预测完成日。"></help-tip>
          <span class="zb-n">{{ f.critical_path.length }}</span></div>
        <div class="fc-cp-b"><zb-paged :items="f.critical_path" :size="10" :m-size="3" unit="项" tag="ol" cls="fc-tl" :fit="150"><template #default="{ item: c }">
          <li :key="c.code" :class="{node: msCodes.includes(c.code)}"><span class="fc-d">{{ c.forecast_finish }}</span><span class="fc-pt"></span>
            <span class="fc-t"><b><act-name :c="c.code" :n="c.name" :s="c.short_name"></act-name></b></span><span class="fc-x"><span class="chip blue" v-if="msCodes.includes(c.code)">节点</span><span class="chip" v-else>{{ c.basis }}</span><span>{{ c.slip_days > 0 ? '+' + fmtN(c.slip_days) : fmtN(c.slip_days) }}</span></span></li></template></zb-paged></div></section>
      <section class="zb-card rp-rec" id="fc-recovery"><div class="zb-ch"><ic n="sparkles"></ic><h2>赶工方案</h2>
          <help-tip text="措施由 AI 提出；“工期×0.8”表示该作业工期压缩到原来的 80%；提前天数由系统按 P6 逻辑关系（CPM，关键路径法）重新计算，不是 AI 估计。是否采用由人决定。"></help-tip>
          <span class="chip orange" v-if="hasRec && recStale" title="方案生成后预测已重新计算">基于旧预测</span></div>
        <div class="zb-body">
          <template v-if="!hasRec"><div class="zb-empty rp-none"><b>尚未生成</b>：当前没有赶工方案。<template v-if="!can('progress_ops')">由项目经理或工程主管生成后，在这里查阅。</template></div>
            <div class="zb-btns" v-if="can('progress_ops')"><button class="btn sm" :disabled="rbusy" @click="recover"><ic n="sparkles"></ic>{{ rbusy ? '生成中 · 已 ' + rsec + ' 秒' : '生成赶工方案' }}</button></div>
            <div class="tiny muted" v-if="can('progress_ops')">按当前预测提出措施，系统按 CPM 复算提前天数</div></template>
          <template v-else>
            <div class="rp-rec-l"><span :class="['zb-src', {ai: recSrc.startsWith('AI')}]">{{ recSrc }}</span><zb-clamp :text="rec.diagnosis" :lines="2" title="赶工方案 · 诊断"></zb-clamp>
              <span class="tiny muted">{{ (rec.verified.measures || []).length }} 条措施<template v-if="rec.forecast_at"> · 基于 {{ fmtDT(rec.forecast_at) }} 的预测</template></span></div>
            <div class="zb-btns"><button class="btn sm primary" @click="showRec = true">查看赶工方案</button></div></template></div></section>
      <div class="fc-side">
        <section class="zb-card fc-slow"><div class="zb-ch"><ic n="hourglass"></ic><h2>工效偏低作业</h2><help-tip text="“慢于计划 25%”表示按日志实测的推进速度，剩余工作要比 P6 计划多用约 25% 的时间（工期系数 1.25）。"></help-tip><span class="zb-n">{{ f.slow_activities.length }}</span></div>
          <zb-more :items="f.slow_activities" :limit="3" :m-limit="3" title="工效偏低作业" unit="项"><template #default="{ item: s }">
            <div class="readrow" :key="s.code"><b><act-name :c="s.code" :n="s.name" :s="s.short_name"></act-name></b> <span class="chip orange">慢于计划 {{ fmtN((s.productivity_factor - 1) * 100) }}%</span> <span class="tiny muted">{{ s.basis }}</span></div></template></zb-more>
          <div class="fc-ok" v-if="!f.slow_activities.length"><ic n="check-circle"></ic>没有工效明显偏低的作业。</div></section>
        <section class="zb-card fc-rain" v-if="rain && !rain.none && rain.calendars.length"><div class="zb-ch"><ic n="calendar"></ic><h2>雨季与节假日</h2><span class="grow"></span>
            <button type="button" class="zb-link" @click="showRain = true">详情<ic n="chevron"></ic></button></div>
          <div class="small">{{ rain.activity_count ? '未来 ' + rain.window_days + ' 天有 ' + rain.activity_count + ' 项作业因例外日少工作日' : '未来 ' + rain.window_days + ' 天没有作业因例外日少工作日' }}</div></section></div></div>
    <zb-drawer v-if="showRec && hasRec" title="赶工方案" :wide="true" @close="showRec = false">
      <div class="rp-rec-b"><div class="tiny muted"><span :class="['zb-src', {ai: recSrc.startsWith('AI')}]">{{ recSrc }}</span><template v-if="rec.forecast_at"> · 基于 {{ fmtDT(rec.forecast_at) }} 的预测</template><span class="chip orange" v-if="recStale"> 基于旧预测</span></div>
        <div class="zb-read mt"><b>{{ rec.diagnosis }}</b></div>
        <div class="small mt" v-for="(m, i) in rec.verified.measures" :key="i">{{ i + 1 }}. <b><act-name :c="m.activity_code" :s="m.activity_short"></act-name></b> {{ m.action }}（工期×{{ m.duration_factor }}；代价：{{ m.cost }}；风险：{{ m.risk }}）
          <span class="chip green" v-for="(g, c) in m.gain" :key="c" v-show="g > 0">{{ tName(c) }} 提前 {{ fmtN(g) }} 天</span></div>
        <div class="small mt">组合效果（系统复算）：<span v-for="t in rec.verified.targets" :key="t.code" class="chip blue" style="margin-right:4px">{{ t.short_name || t.name }} {{ t.base }} → {{ t.after }}（{{ t.gain_days > 0 ? '提前' + fmtN(t.gain_days) + '天' : '无改善' }}）</span></div>
        <div class="tiny muted mt">{{ rec.note }}</div></div>
      <template #foot v-if="can('progress_ops')"><button class="btn sm" :disabled="rbusy" @click="recover"><ic n="refresh"></ic>{{ rbusy ? '生成中 · 已 ' + rsec + ' 秒' : '按当前预测重新生成' }}</button></template></zb-drawer>
    <zb-drawer v-if="showRain && rain && !rain.none" title="雨季与节假日" @close="showRain = false">
      <div class="tiny muted">P6 日历的例外日 · 未来 {{ rain.window_days }} 天</div>
      <div class="rn-cal" v-for="c in rain.calendars" :key="c.clndr_id"><b>{{ c.name }}</b><span class="tiny muted">{{ c.activities }} 项作业</span>
        <span class="chip blue" v-if="c.rainy_months.length">雨季 {{ pkMonths(c.rainy_months) }}</span><span class="tiny muted" v-else>无雨季月</span></div>
      <div class="small mt" v-if="rain.activity_count">未来 {{ rain.window_days }} 天有 {{ rain.activity_count }} 项作业因例外日少工作日：</div>
      <zb-paged :items="rain.activities" :size="10" :m-size="6" unit="项" :fit="false"><template #default="{ item: a }"><div class="readrow" :key="a.code"><span v-if="a.critical" class="chip red">关键</span> <b><act-name :c="a.code" :n="a.name" :s="a.short_name"></act-name></b>
        <span class="tiny muted">{{ a.start }} ~ {{ a.finish }}</span> <span class="chip orange">少 {{ fmtN(a.lost_days) }} 个工作日</span></div></template></zb-paged>
      <div class="tiny muted mt">{{ rain.note }}</div></zb-drawer>
  </template>
  <load-state v-else loading></load-state></div>`,
  data: () => ({ f: null, rec: null, busy: false, rbusy: false, rsec: 0, loaded: false, hasPlan: false, cd: null, rain: null, showRec: false, showRain: false }),
  computed: {
    // 预测方法与日历口径收进“预测方法”小问号（一屏化：不常驻一行说明）
    methodNote() { const f = this.f || {}; return [f.method, f.calendar_note, f.legacy ? f.legacy.note + "，完工 " + f.legacy.project_finish : ""].filter(Boolean).join(" · "); },
    msCodes() { return this.f && this.f.milestones ? this.f.milestones.map((m) => m.code) : []; },
    cdMap() { return Object.fromEntries(((this.cd && this.cd.rows) || []).map((r) => [r.code, r])); },
    hasRec() { return !!(this.rec && !this.rec.none && this.rec.verified); },
    // 来源如实写：只有真 AI 才写“AI 提出”；离线模拟模型、系统模板各写各的
    // 当前接的是离线模拟模型时，接口仍记 source=ai，这里按运行模型如实改写（同 main.js 页脚的判断）
    recSrc() { const s = this.rec && this.rec.source, mock = store.me && store.me.ai && store.me.ai.provider === "mock";
      if (s === "ai" && mock) return "离线模拟模型 · 系统按 CPM 复算";
      return s === "ai" ? "AI 提出 · 系统按 CPM 复算" : s === "mock" ? "离线模拟模型 · 系统按 CPM 复算" : s === "template" ? "系统模板 · 按 CPM 复算" : "系统按 CPM 复算"; },
    recStale() { return !!(this.rec && this.rec.forecast_at && this.f && this.f.created_at && this.rec.forecast_at !== this.f.created_at); },
    emptyReason() {
      if (!can("progress_ops")) return "还没有预测。请项目部先导入 P6 计划并运行预测。";
      return this.hasPlan ? "已导入 P6 计划，还没有运行过预测。" : "还没有预测：预测要先导入 P6 计划，并有日志里的实测进度。";
    },
    emptyAction() {
      if (!can("progress_ops")) return null;
      return this.hasPlan ? { label: "运行预测", fn: this.run } : { label: "去导入 P6 计划", route: "progress?tab=plan", id: "plan-file" };
    },
  },
  async mounted() {
    this.f = await GET("/api/schedule/forecast"); this.rec = await GET("/api/schedule/recovery");
    try { this.hasPlan = (await GET("/api/p6/imports")).length > 0; } catch (_) { this.hasPlan = false; }
    this.loaded = true;
    if (store.route.params.open === "recovery" && this.hasRec) this.showRec = true;
    this.extras();
  },
  methods: {
    // 赶工组合里按代码给的里程碑增益：显示中文简称（取自同一方案的目标里程碑）
    tName(c) { const t = ((this.rec && this.rec.verified && this.rec.verified.targets) || []).find((x) => x.code === c); return t ? t.short_name || t.name : c; },
    can, fmtN, fmtDT, probText, probClass, pkMonths: (ms) => (typeof pkMonths === "function" ? pkMonths(ms) : ms.join("、")),
    // 控制节点倒计时、雨季提示（x_plan / routers/plan.py）；拿不到时不显示，不影响预测
    async extras() {
      try { [this.cd, this.rain] = await Promise.all([GET("/api/plan/countdown"), GET("/api/plan/rain")]); } catch (_) { this.cd = this.rain = null; }
    },
    // 节点时间刻度：每个节点各自取 P6/预测/P50/P80 的最早到最晚，两头各留一周，位置按日期等比例
    span(m) { const ds = [m.p6_date, m.forecast, m.p50, m.p80].filter(Boolean).map((s) => Date.parse(s)); return [Math.min(...ds) - 7 * 864e5, Math.max(...ds) + 7 * 864e5]; },
    at(m, s) { const [lo, hi] = this.span(m); return ((Date.parse(s) - lo) / (hi - lo) * 100).toFixed(2) + "%"; },
    band(m) { const a = parseFloat(this.at(m, m.p50)), b = parseFloat(this.at(m, m.p80)); return { left: a + "%", width: Math.max(b - a, 1) + "%" }; },
    async run() { this.busy = true; try { await POST("/api/schedule/forecast", {}); this.f = await GET("/api/schedule/forecast"); this.extras(); } catch (e) { toast(e.message, true); } finally { this.busy = false; } },
    // 赶工方案要调用模型，可能要几分钟：按钮上显示已等秒数（验收 I10，不编造预计时长）
    async recover() {
      this.rbusy = true; this.rsec = 0; const t = setInterval(() => { this.rsec++; }, 1000);
      try { this.rec = await POST("/api/schedule/recovery"); this.showRec = true; } catch (e) { toast(e.message, true); } finally { clearInterval(t); this.rbusy = false; }
    },
  },
};

const ProgressHub = {
  components: { Forecast, Alerts, Plan },
  template: `<div><page-head :page="tab"></page-head>
    <forecast v-if="tab === 'forecast'"></forecast><alerts v-else-if="tab === 'alerts'"></alerts><plan v-else></plan>
    <ai-note v-if="tab !== 'plan'" :page="tab === 'alerts' ? 'alerts' : 'forecast'"></ai-note></div>`,
  data: () => ({ tab: "forecast" }),
  created() { this.tab = store.route.params.tab || "forecast"; },   // 页签在页面上方的导航条里（nav.js），切换即换路由
};

// 产值周图（2026-10-08 换皮第二轮）：每周两根柱——周记录（导入的周产值记录）与系统统计（按日志工程量算，分“核定 / 自报”两层叠放）；
// 计划为虚线；柱顶直接标数；没有数据的周只在底线画一个灰点（如实留空，不补零）。只换画法，数值与口径同 /api/output/overview。
// 横轴从第一周有数据的前一周开始（至少显示 8 周），全空的前段不再占地方。
const OutputChart = {
  props: { series: { type: Array, required: true }, currency: String, fit: Boolean },
  template: `<div class="oc" ref="box">
    <div class="oc-sum" v-if="sums.rec !== null || sums.sys !== null">
      <div v-if="sums.rec !== null"><small>周记录合计（{{ recWeeks }} 周）</small><b>{{ m(sums.rec) }}</b><span>{{ currency }}</span></div>
      <div v-if="sums.sys !== null"><small>系统统计合计<template v-if="split"> · 核定 {{ m(sums.appr) }} / 自报 {{ m(sums.self) }}</template></small><b class="sys">{{ m(sums.sys) }}</b><span>{{ currency }}</span></div>
      <div v-if="trend"><small>最近一周（{{ trend.week }}）比上一周</small><b :class="trend.d >= 0 ? 'up' : 'down'">{{ trend.d >= 0 ? '▲' : '▼' }} {{ Math.abs(trend.pct) }}%</b><span>{{ trend.src }}</span></div></div>
    <div class="oc-legend"><span><i class="k rec"></i>周记录</span><template v-if="split"><span><i class="k appr"></i>系统统计 · 核定</span><span><i class="k self"></i>系统统计 · 自报</span></template><span v-else><i class="k appr"></i>系统统计</span><span v-if="hasPlan"><i class="k plan"></i>更新计划</span><span v-if="base.length"><i class="k base"></i>基准计划</span></div>
    <div class="oc-wrap" :style="{height: H + 'px'}" @mouseleave="hv = -1">
      <svg :width="w" :height="H" role="img" :aria-label="'近 ' + shown.length + ' 周产值'">
        <defs><linearGradient id="ocRec" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2a86d8"/><stop offset="1" stop-color="#0d5ea8"/></linearGradient>
          <linearGradient id="ocAppr" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#22b3a7"/><stop offset="1" stop-color="#0f9e94"/></linearGradient></defs>
        <g class="oc-grid"><template v-for="t in ticks" :key="t.v"><line :x1="L" :x2="w - R" :y1="t.y" :y2="t.y" :class="t.v ? 'g' : 'base'"/><text :x="L - 8" :y="t.y + 4" text-anchor="end">{{ t.v ? m(t.v) : '0' }}</text></template></g>
        <g v-for="(it, i) in shown" :key="it.week">
          <rect v-if="hv === i" :x="sx(i) + 2" :y="T - 6" :width="slot - 4" :height="plotH + 6" rx="6" class="oc-hl"/>
          <template v-if="it.has">
            <path v-if="it.rec" :d="bar(gx(i, 0), y(it.rec), bw)" fill="url(#ocRec)"/>
            <path v-if="it.appr" :d="bar(gx(i, 1), y(it.appr), bw, !it.self)" fill="url(#ocAppr)"/>
            <path v-if="it.self" :d="bar(gx(i, 1), y((it.appr || 0) + it.self), bw, true, y(it.appr || 0) - 2)" class="oc-self"/>
            <text v-if="it.rec && lab(i)" :x="gx(i, 0) + bw / 2" :y="y(it.rec) - 7" text-anchor="middle" class="oc-v">{{ m(it.rec) }}</text>
            <text v-if="it.sys && lab(i)" :x="gx(i, 1) + bw / 2" :y="y(it.sys) - 7" text-anchor="middle" class="oc-v sys">{{ m(it.sys) }}</text></template>
          <circle v-else :cx="sx(i) + slot / 2" :cy="T + plotH" r="3" class="oc-none"><title>{{ it.week }} 无数据</title></circle>
          <text v-if="slot >= 40 || (shown.length - 1 - i) % 2 === 0" :x="sx(i) + slot / 2" :y="H - 8" text-anchor="middle" :class="['oc-x', i === shown.length - 1 ? 'now' : '']">{{ i === shown.length - 1 ? '本周' : it.week.slice(5) }}</text>
          <rect :x="sx(i)" :y="0" :width="slot" :height="H" fill="transparent" @mouseenter="hv = i" @click="hv = i"/></g>
        <polyline v-if="base.length > 1" :points="base.map((p) => p.join(',')).join(' ')" class="oc-plan oc-base"/>
        <circle v-for="(p, i) in base" :key="'b' + i" :cx="p[0]" :cy="p[1]" r="3" class="oc-pt oc-base"/>
        <polyline v-if="plan.length > 1" :points="plan.map((p) => p.join(',')).join(' ')" class="oc-plan"/>
        <circle v-for="(p, i) in plan" :key="'p' + i" :cx="p[0]" :cy="p[1]" r="3.5" class="oc-pt"/></svg>
      <div v-if="hv >= 0 && shown[hv]" class="bc-tip" :style="tip"><b>{{ shown[hv].week }} 这一周</b>
        <template v-if="shown[hv].has"><div v-if="shown[hv].rec !== null"><span>周记录</span>{{ m(shown[hv].rec) }} {{ currency }}</div>
          <div v-if="shown[hv].sys !== null"><span>系统统计</span>{{ m(shown[hv].sys) }} {{ currency }}</div>
          <div v-if="split && shown[hv].sys !== null"><span>　其中核定 / 自报</span>{{ m(shown[hv].appr || 0) }} / {{ m(shown[hv].self || 0) }}</div>
          <div v-if="shown[hv].plan"><span>更新计划</span>{{ m(shown[hv].plan) }} {{ currency }}</div>
          <div v-if="shown[hv].base"><span>基准计划</span>{{ m(shown[hv].base) }} {{ currency }}</div></template>
        <div v-else><span>无数据</span>—</div></div></div></div>`,
  data: () => ({ hv: -1, w: 700, H: 300, L: 56, R: 12, T: 26 }),
  computed: {
    // 项目经理的接口不给自报/核定拆分（日志审核数据）：只写系统统计合计，不显示“核定 X / 自报 0”这种错误拆分（验收 I6）
    split() { return this.series.some((s) => "system_self" in s); },
    recWeeks() { return this.shown.filter((x) => x.rec !== null).length; },   // 合计写实际有周记录的周数，不按图上显示的周数（D4）
    rows() {
      return this.series.map((s) => {
        const rec = s.record_actual ?? null, sys = s.system ?? null, plan = s.planned ?? null;
        return { week: s.week, rec, sys, base: s.baseline ?? null, appr: s.system_approved ?? (sys !== null && s.system_self === undefined ? sys : null), self: s.system_self || 0, plan, has: rec !== null || sys !== null };
      });
    },
    shown() {
      const r = this.rows, first = r.findIndex((x) => x.has || x.plan || x.base);
      if (first < 0) return r.slice(-8);
      return r.slice(Math.max(0, Math.min(first - 1, r.length - 8)));
    },
    plotH() { return this.H - this.T - 28; },
    slot() { return Math.max(24, (this.w - this.L - this.R) / Math.max(1, this.shown.length)); },
    bw() { return Math.max(6, Math.min(34, this.slot * 0.3)); },
    max() {
      const m = Math.max(1, ...this.shown.map((x) => Math.max(x.rec || 0, x.sys || 0, x.plan || 0, x.base || 0)));
      const raw = m * 1.12 / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / p;
      return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p * 4;
    },
    ticks() { return [0, 1, 2, 3, 4].map((k) => ({ v: this.max / 4 * k, y: this.y(this.max / 4 * k) })); },
    hasPlan() { return this.shown.some((x) => x.plan); },
    plan() { return this.shown.map((x, i) => (x.plan ? [this.sx(i) + this.slot / 2, this.y(x.plan)] : null)).filter(Boolean); },
    base() { return this.shown.map((x, i) => (x.base ? [this.sx(i) + this.slot / 2, this.y(x.base)] : null)).filter(Boolean); },
    sums() {
      const s = (k) => { const v = this.shown.filter((x) => x[k] !== null && x.has); return v.length ? v.reduce((a, x) => a + (x[k] || 0), 0) : null; };
      const sys = s("sys");
      return { rec: s("rec"), sys, appr: sys === null ? null : this.shown.reduce((a, x) => a + (x.sys !== null ? x.appr || 0 : 0), 0), self: sys === null ? null : this.shown.reduce((a, x) => a + (x.sys !== null ? x.self || 0 : 0), 0) };
    },
    // 环比只在最近两个“有数据的周”之间、同一来源（优先周记录）比较；不足两周不显示
    trend() {
      const pick = (k) => this.shown.filter((x) => x[k] !== null && x[k] > 0);
      for (const [k, src] of [["rec", "周记录"], ["sys", "系统统计"]]) {
        const v = pick(k); if (v.length < 2) continue;
        const a = v[v.length - 2][k], b = v[v.length - 1][k];
        return { week: v[v.length - 1].week.slice(5), d: b - a, pct: Math.round((b - a) / a * 100), src };
      }
      return null;
    },
    tip() { const cx = this.sx(this.hv) + this.slot / 2, flip = cx > this.w * 0.6; return flip ? { right: Math.max(0, this.w - cx + 12) + "px", top: "8px" } : { left: cx + 12 + "px", top: "8px" }; },
  },
  mounted() {
    const el = this.$refs.box, set = () => { if (el && el.clientWidth) this.w = el.clientWidth; this.fitH(); };
    set(); if (window.ResizeObserver) { this._ro = new ResizeObserver(set); this._ro.observe(el); }
    if (this.fit) { this._rs = () => { this._n = 0; this.fitH(); }; window.addEventListener("resize", this._rs); }
  },
  beforeUnmount() { if (this._ro) this._ro.disconnect(); if (this._rs) window.removeEventListener("resize", this._rs); },
  methods: {
    // fit（产值页一屏化）：电脑端图高随页面余量伸缩（140～340），让整页恰好一屏；手机固定 170。只改图高，不改数值。
    fitH() {
      if (!this.fit) return;
      if (window.innerWidth < 861) { this.H = 170; return; }
      if (typeof fitContentBottom !== "function" || (this._n = (this._n || 0) + 1) > 6) return;
      requestAnimationFrame(() => {
        const slack = Math.floor(window.innerHeight - (fitContentBottom() - window.scrollY) - 2);
        const h = Math.max(140, Math.min(340, this.H + slack));
        if (Math.abs(h - this.H) > 1) { this.H = h; this.$nextTick(() => this.fitH()); }
      });
    },
    m(v) { return v === null || v === undefined ? "—" : money(v, "").replace(/\.0(?=[万亿])/, ""); },
    sx(i) { return this.L + i * this.slot; },
    gx(i, k) { const gap = 3, total = this.bw * 2 + gap; return this.sx(i) + (this.slot - total) / 2 + k * (this.bw + gap); },
    y(v) { return this.T + this.plotH - (v / this.max) * this.plotH; },
    // 柱顶多时只标最高的和最近的，避免满屏数字
    lab(i) { const has = this.shown.map((x, j) => (x.has ? j : -1)).filter((j) => j >= 0); if (has.length <= 8) return true; const top = this.shown.reduce((b, x, j) => (Math.max(x.rec || 0, x.sys || 0) > Math.max(this.shown[b].rec || 0, this.shown[b].sys || 0) ? j : b), has[0]); return i === top || i === has[has.length - 1]; },
    bar(x, y, w, round = true, base) {
      const b = base === undefined ? this.T + this.plotH : base, r = round ? Math.min(5, w / 2, Math.max(0, b - y)) : 0;
      return `M${x},${b} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${b} Z`;
    },
  },
};

// 公司周报十项指标（23 号约定，RJ 10-10）：产值页（mode="page"）与周报（mode="report"）共用这一个组件、同一份数据（/api/output/overview 的 kpi10，
// 或 /api/reports/{id} 的 kpi10：已发布为发布时快照，草稿为实时查询）。名称照用户原名完整显示；缺数显示“—”并写明原因，不补零；
// 分组：上周履约 → 整体进度 → 年度与开累。项目整体完成百分比按产值口径（开累产值 ÷ 清单总产值），总体计划完成百分比来源未核实不计算。
const KPI10_IC = { last_baseline: "file", last_planned: "edit", last_actual: "check", last_diff: "calculator", pct: "target", plan_pct: "chart",
  gap: "sliders", year_plan: "calendar", year_actual: "database", cum: "trend" };
const Kpi10 = {
  props: { d: { type: Object, required: true }, mode: { type: String, default: "page" } },
  template: `<div v-if="d.kind === 'none'" class="zb-empty k10-none">{{ d.note }}</div>
  <div v-else-if="mode === 'report'" class="k10r">
    <div class="k10r-meta"><span>所属周 <b>{{ span }}</b></span><span>金额单位 <b>{{ d.unit }}</b></span>
      <span :class="['k10-tag', d.kind]">{{ d.kind === 'snapshot' ? '发布时快照' : '实时查询 · 发布时冻结' }}</span></div>
    <template v-for="(g, gi) in groups" :key="g.name"><div class="k10r-h">{{ '一二三'[gi] }}、{{ g.name }}</div>
      <div :class="['k10r-row', 'n' + g.items.length]"><div v-for="x in g.items" :key="x.key" class="k10r-c"><span class="k10-ic"><ic :n="ic(x.key)"></ic></span>
        <div class="k10r-b"><small>{{ x.label }}</small><div class="k10-v" :class="tone(x)">{{ val(x) }}<em v-if="x.value != null">{{ x.unit }}</em></div>
          <i v-if="x.value == null">{{ x.why }}</i><i v-else-if="x.key === 'last_diff'">完成产值 − 更新计划</i></div></div></div></template>
    <div class="k10r-foot">{{ d.basis }} 来源版本 {{ d.source.version }} · 数据截至 {{ d.source.data_date || '—' }}<template v-if="d.kind === 'snapshot'"> · 冻结于 {{ fmtDT(d.at) }}</template></div></div>
  <div v-else class="k10 k10c">
    <section class="zb-card k10-last"><div class="zb-ch"><ic n="database"></ic><h2>上周产值</h2><span class="zb-src">{{ span }}</span><span class="grow"></span><span class="tiny muted" v-if="d.actual_src">完成值取自{{ d.actual_src }}</span></div>
      <div class="k10-li" v-for="b in bars" :key="b.key"><span class="k10-li-l">{{ by[b.k] ? by[b.k].label : b.name }}<i class="why" v-if="b.v == null && by[b.k]" :title="by[b.k].why">{{ by[b.k].why }}</i></span>
        <div class="trk" v-if="b.v != null"><i :class="b.key" :style="{ width: b.w + '%' }"></i></div><b :class="{ na: b.v == null }">{{ b.v == null ? '—' : fmt(b.v) }}</b></div>
      <div class="k10-li k10-li-d" v-for="x in g('上周产值').filter((y) => y.key === 'last_diff')" :key="x.key"><span class="k10-li-l">{{ x.label }}<help-tip v-if="x.note" :text="x.note"></help-tip><i class="why" v-if="x.value == null" :title="x.why">{{ x.why }}</i></span>
        <b :class="tone(x)">{{ val(x) }}<em v-if="x.value != null && x.unit !== d.unit">{{ x.unit }}</em></b></div></section>
    <section class="zb-card k10-prog"><div class="zb-ch"><ic n="target"></ic><h2>项目整体进度</h2><span class="grow"></span><span class="tiny muted">按产值口径</span></div>
      <div class="k10-li" v-for="x in g('整体进度').slice(0, 2)" :key="x.key"><span class="k10-li-l">{{ x.label }}<help-tip v-if="x.note" :text="x.note"></help-tip><i class="why" v-if="x.value == null" :title="x.why">{{ x.why }}</i></span>
        <div class="trk" v-if="x.value != null"><i :style="{ width: clamp(x.value) + '%' }"></i></div><b :class="{ na: x.value == null }">{{ x.value == null ? '—' : x.value + '%' }}</b></div>
      <div class="k10-li k10-li-d" v-for="x in g('整体进度').slice(2)" :key="x.key"><span class="k10-li-l">{{ x.label }}<help-tip v-if="x.note" :text="x.note"></help-tip><i class="why" v-if="x.value == null" :title="x.why">{{ x.why }}</i></span>
        <b :class="tone(x)">{{ val(x) }}<em v-if="x.value != null">{{ x.unit }}</em></b></div></section>
    <section class="zb-card k10-year"><div class="zb-ch"><ic n="calendar"></ic><h2>年度与开累</h2><span class="zb-src">{{ d.year }} 年</span></div>
      <div class="k10-li" v-for="x in g('年度与开累').slice(0, 2)" :key="x.key"><span class="k10-li-l">{{ x.label }}<i class="why" v-if="x.value == null" :title="x.why">{{ x.why }}</i></span>
        <b :class="{ na: x.value == null }">{{ val(x) }}<em v-if="x.value != null && x.unit !== d.unit">{{ x.unit }}</em></b></div>
      <div class="k10-li"><span class="k10-li-l">年度完成率<i class="why" v-if="d.year_pct == null" title="本年度计划产值未报送或为零，不计算">年度计划未报送或为零，不计算</i></span>
        <b :class="{ na: d.year_pct == null }">{{ d.year_pct == null ? '—' : d.year_pct + '%' }}</b></div>
      <div class="k10-li k10-li-d k10-cum" v-for="x in g('年度与开累').slice(2)" :key="x.key"><span class="k10-li-l">{{ x.label }}<i class="why" :title="x.value == null ? x.why : ''">{{ x.value == null ? x.why : (d.cum_src || '') + ' · 截至 ' + (d.source.data_date || '—') }}</i></span>
        <b :class="{ na: x.value == null }">{{ val(x) }}<em v-if="x.value != null && x.unit !== d.unit">{{ x.unit }}</em></b></div></section>
    <slot name="source"></slot></div>`,
  computed: {
    span() { const p = this.d.period || {}; return p.start ? p.start.slice(5) + "～" + (p.end || "").slice(5) : "—"; },
    by() { return Object.fromEntries((this.d.items || []).map((x) => [x.key, x])); },
    groups() { const out = []; for (const x of this.d.items || []) { let gr = out.find((y) => y.name === x.group); if (!gr) out.push(gr = { name: x.group, items: [] }); gr.items.push(x); } return out; },
    bars() {
      const rows = [["base", "基准计划", "last_baseline"], ["plan", "更新计划", "last_planned"], ["act", "实际完成", "last_actual"]].map(([key, name, k]) => ({ key, name, k, v: this.by[k] ? this.by[k].value : null }));
      const max = Math.max(0, ...rows.map((r) => r.v || 0));
      return rows.map((r) => ({ ...r, w: r.v == null || !max ? 0 : Math.max(1.5, r.v / max * 100) }));
    },
  },
  methods: {
    fmtDT,
    g(name) { return (this.d.items || []).filter((x) => x.group === name); },
    ic(k) { return KPI10_IC[k] || "chart"; },
    fmt(v) { return (+v).toLocaleString("zh-CN", { maximumFractionDigits: 2 }); },
    val(x) {
      if (x.value == null) return "—";
      if (x.key === "last_diff" || x.key === "gap") return (x.value > 0 ? "+" : x.value < 0 ? "−" : "") + this.fmt(Math.abs(x.value));
      return this.fmt(x.value);
    },
    tone(x) { return x.value == null ? "na" : (x.key === "last_diff" || x.key === "gap") && x.value ? (x.value > 0 ? "up" : "down") : ""; },
    clamp(v) { return v == null ? 0 : Math.max(0, Math.min(100, v)); },
  },
};

// 计划参数录入（工程主管、管理员；output_ops）：合同额只作参考，清单总产值是项目整体完成百分比的分母，年度计划产值。项目经理只读，不出现。
const OutputPlanForm = {
  props: { r: { type: Object, required: true } },
  emits: ["saved", "close"],
  template: `<div class="orp-form">
    <label>合同额（参考，{{ cur || '原币' }}）<input class="in" type="number" min="0" step="any" v-model="f.contract" placeholder="未录入"></label>
    <label>清单总产值（{{ cur || '原币' }}）<input class="in" type="number" min="0" step="any" v-model="f.boq" placeholder="未录入"></label>
    <label>{{ r.year.year }} 年度计划产值<input class="in" type="number" min="0" step="any" v-model="f.year" placeholder="未录入"></label>
    <div class="zb-btns"><button class="btn primary sm" :disabled="busy" @click="save">保存</button><button class="btn ghost sm" @click="$emit('close')">取消</button></div>
    <span class="tiny muted">每周基准 / 更新产值计划随周产值记录一起导入（列名：基准产值计划、更新产值计划）。</span></div>`,
  data() { const c = this.r.cumulative; return { busy: false, f: { contract: c.contract ?? "", boq: c.boq_total ?? "", year: this.r.year.plan ?? "" } }; },
  computed: { cur() { return this.r.currency || ""; } },
  methods: {
    async save() {
      this.busy = true;
      const n = (v) => (v === "" || v === null ? null : Number(v));
      try {
        await PUT("/api/output/plan", { contract_amount: n(this.f.contract), boq_total: n(this.f.boq), year: this.r.year.year, year_plan: n(this.f.year) });
        toast("已保存"); this.$emit("saved");
      } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

// 进度与产值 · 产值页（设计包 21）：十项指标为主体，下方是真实多期趋势与来源详情。一屏化（10-10）：十项指标收成三张卡一行，周图高度随余量伸缩，
// 数据接入（导入、映射表）与录入计划参数进抽屉，AI 分析截两行、全文在抽屉；只改排布，口径与数值不变。项目经理只读（RJ 10-10）：导入、匹配、确认、录入计划、分析按钮
// 只给 output_ops（工程主管、管理员），后端同样拒绝。
const OutputPage = {
  components: { BarChart, OutputChart, Kpi10, OutputPlanForm },
  template: `<div v-if="o" class="op-page">
  <page-head page="output" title="产值"></page-head>
  <div class="op-bar"><b>公司周报报送口径</b><span class="op-per">上周 {{ per }}</span><span class="chip blue" v-if="sim" title="仿真项目数据，只用于演示，不作成效或试点数据">仿真演示</span>
    <span class="tiny muted">金额单位：{{ o.kpi10.unit }}</span><span class="grow"></span>
    <div class="zb-btns" v-if="can('output_ops')"><button class="btn ghost sm" @click="showMaps = true">数据接入</button><button class="btn ghost sm" @click="edit = true">录入计划参数</button>
      <button class="btn ai sm" :disabled="abusy" @click="analyze">{{ abusy ? '分析中' : 'AI 产值分析' }}</button></div></div>
  <empty-state v-if="!o.maps_confirmed && can('output_ops')" :reason="mapReason" :action="mapAction"></empty-state>
  <kpi10 :d="o.kpi10" mode="page"></kpi10>
  <div class="op-low">
    <section class="zb-card oc-card" id="output-trend"><div class="zb-ch"><ic n="chart"></ic><h2>每周产值</h2><span class="zb-src">近 16 周内有数据的周</span>
        <help-tip :text="split ? '优先取周记录，缺失时按日志工程量统计。按日志统计的产值分两层：自报＝分包提交、待港湾审核；核定＝港湾已审核；合计为两者之和，被退回的不计入。' : '优先取周记录，缺失时按日志工程量统计。周记录为报送值；系统统计按日志工程量 × 清单单价测算（内部统计，非计量认证）。'"></help-tip></div>
      <output-chart :series="o.series" :currency="o.currency" :fit="true"></output-chart>
      <div class="tiny op-split" v-if="split && last.src === '系统统计' && (last.self || last.approved)">上周系统统计：<trust-split :self="last.self" :approved="last.approved" :self-ids="last.selfIds" :approved-ids="last.approvedIds" :fmt="(v) => money(v, '')"></trust-split></div></section>
    <div class="op-side">
      <section class="zb-card op-src"><div class="zb-ch"><ic n="file"></ic><h2>数据来源与口径</h2><help-tip :text="o.kpi10.basis"></help-tip></div>
        <div class="op-src-r"><b>报送值</b><span>导入的周产值记录<help-tip text="公司周报报送口径：导入的周产值记录（基准 / 更新计划、完成）。"></help-tip></span></div>
        <div class="op-src-r"><b>系统内部测算</b><span>非计量认证<help-tip text="日志工程量 × 清单单价测算的内部统计，非计量认证，不与报送值混加。"></help-tip></span></div>
        <div class="tiny muted op-src-l">来源版本 {{ o.kpi10.source.version }} · 周产值记录 {{ o.kpi10.source.records }} 条 · 数据截至 {{ o.kpi10.source.data_date || '—' }}<template v-if="o.report.source"> · 计划来源：{{ o.report.source }}</template></div>
        <zb-clamp class="tiny orp-miss" v-if="o.report.missing.length" :lines="2" title="未报送（不补零）" :text="'未报送（不补零）：' + o.report.missing.join('、')"></zb-clamp>
        <div class="tiny muted op-ro" v-if="!can('output_ops')">只读查阅<help-tip :text="'清单导入、映射确认与计划参数由工程主管维护。清单 ' + o.boq_items + ' 项，已确认映射 ' + o.maps_confirmed + ' 条。'"></help-tip></div></section>
      <section class="zb-card op-ana" v-if="o.analysis"><div class="zb-ch"><ic n="sparkles"></ic><h2>产值分析</h2><span class="zb-src">{{ o.analysis.period_label ? o.analysis.period_label + ' · ' : '' }}{{ fmtDT(o.analysis.at) }}</span></div>
        <zb-clamp :text="o.analysis.summary" :lines="2" title="产值分析"><template #full><b>{{ o.analysis.summary }}</b>
          <div class="tiny muted" v-if="o.analysis.note">{{ o.analysis.note }}</div>
          <div class="small mt" v-if="o.analysis.drivers.length">主要作业：{{ o.analysis.drivers.join('；') }}</div>
          <div class="small" v-for="(a, i) in o.analysis.actions" :key="i">建议{{ i + 1 }}：{{ a }}</div></template></zb-clamp></section></div></div>
  <zb-drawer v-if="edit && can('output_ops')" title="录入计划参数" @close="edit = false">
    <output-plan-form :r="o.report" @saved="edit = false; load()" @close="edit = false"></output-plan-form></zb-drawer>
  <zb-drawer v-if="showMaps && can('output_ops')" title="数据接入" :wide="true" @close="showMaps = false">
    <div id="output-maps"><div class="row wrap"><label class="btn" id="output-boq" tabindex="0">导入工程量清单<input type="file" accept=".xlsx" hidden @change="imp('boq', $event)"></label>
      <label class="btn">导入周产值记录<input type="file" accept=".xlsx" hidden @change="imp('records', $event)"></label>
      <button class="btn ai" :disabled="mbusy" @click="suggest">{{ mbusy ? '匹配中' : 'AI 匹配清单' }}</button>
      <button class="btn primary" v-if="suggested.length" @click="setMap(suggested.map(m => m.id), 'confirmed')">全部确认 {{ suggested.length }} 条</button></div>
    <div class="tiny muted mt">内部产值统计（非计量认证）：产值 = 工程量 × 映射系数 × 单价，不是业主计量或结算金额。清单 {{ o.boq_items }} 项，已确认映射 {{ o.maps_confirmed }} 条、待确认 {{ o.maps_suggested }} 条。</div>
    <div class="scroll-x mt" v-if="maps.length"><zb-paged :items="maps" :size="10" :m-size="5" unit="条映射" tag="table" cls="t op-maps" :fit="false"><template #head><thead><tr><th>P6 作业</th><th>清单子目</th><th>单位</th><th class="right">单价</th><th>AI 理由</th><th>状态</th><th></th></tr></thead></template>
      <template #default="{ item: m }"><tr :key="m.id"><td><b><act-name :c="m.activity_code" :n="m.activity_name" :s="m.activity_short"></act-name></b></td><td>{{ m.boq_code }} {{ m.boq_name }}</td><td>{{ m.unit }}</td>
        <td class="right">{{ fmtN(m.unit_price, 2) }}</td><td class="tiny muted">{{ m.reason }}</td>
        <td><span :class="['chip', m.status === 'confirmed' ? 'green' : m.status === 'rejected' ? 'red' : 'orange']">{{ {confirmed: '已确认', suggested: '待确认', rejected: '已否决'}[m.status] }}</span></td>
        <td class="nowrap"><button class="btn ghost sm" v-if="m.status !== 'confirmed'" @click="setMap([m.id], 'confirmed')">确认</button>
          <button class="btn ghost sm" v-if="m.status !== 'rejected'" @click="setMap([m.id], 'rejected')">否决</button></td></tr></template></zb-paged></div></div></zb-drawer>
  <ai-note page="output"></ai-note></div>
  <load-state v-else loading :error="err" @retry="load"></load-state>`,
  data: () => ({ o: null, maps: [], abusy: false, mbusy: false, err: "", edit: false, showMaps: false }),
  computed: {
    projName() { return store.me && store.me.project ? store.me.project.name : ""; },
    sim() { return typeof isSim === "function" && isSim(); },
    split() { return this.o.series.some((w) => "system_self" in w); },   // 项目经理的接口不给自报/核定拆分
    per() { const p = this.o.kpi10.period || {}; return p.start ? p.start.slice(5) + "～" + (p.end || "").slice(5) : "—"; },
    // 没有已确认的清单映射时，产值算不出来：按真实进度给出下一步（导入清单 → AI 匹配 → 确认）
    mapReason() {
      const pend = this.suggested.length;
      if (pend) return `有 ${pend} 条 AI 建议的清单映射等你确认，确认后才能算产值。`;
      return this.o.boq_items ? "还没有 P6 作业与清单子目的映射，产值暂时算不出来。" : "还没有导入工程量清单，产值暂时算不出来。";
    },
    mapAction() {
      if (this.suggested.length) return { label: "确认清单映射", fn: () => { this.showMaps = true; } };
      return this.o.boq_items ? { label: "AI 匹配清单", fn: this.suggest } : { label: "导入工程量清单", fn: () => { this.showMaps = true; } };
    },
    last() {
      const w = this.o.series[this.o.series.length - 2] || {};
      return { value: w.record_actual ?? w.system, src: w.record_actual !== null && w.record_actual !== undefined ? "周记录" : "系统统计",
               self: w.system_self, approved: w.system_approved, selfIds: w.log_ids_self || [], approvedIds: w.log_ids_approved || [] };
    },
    suggested() { return this.maps.filter((m) => m.status === "suggested"); },
  },
  mounted() { this.load(); },
  methods: {
    can, money, fmtN, fmtDT,
    async load() { this.err = ""; try { this.o = await GET("/api/output/overview"); this.maps = await GET("/api/output/maps"); } catch (e) { this.err = e.message; } },
    async imp(kind, ev) { const f = ev.target.files[0]; ev.target.value = ""; if (!f) return; const fd = new FormData(); fd.append("file", f);
      try { const r = await POST(`/api/output/${kind}`, fd); toast(`已导入 ${r.items ?? r.rows} 条`); this.load(); } catch (e) { toast(e.message, true); } },
    async suggest() { this.mbusy = true; try { const r = await POST("/api/output/map/suggest"); toast(`AI 建议 ${r.suggested} 条映射`); this.load(); } catch (e) { toast(e.message, true); } finally { this.mbusy = false; } },
    async setMap(ids, status) { await POST("/api/output/maps", { ids, status }); this.load(); },
    async analyze() { this.abusy = true; try { await POST("/api/output/analyze"); this.load(); } catch (e) { toast(e.message, true); } finally { this.abusy = false; } },
  },
};

// 智能体按“替谁做什么”分组展示（只分组，数字全部取自 /api/agents，不改统计口径）
const AGENT_GROUPS = [
  { name: "现场记录与资料", does: "把口述和照片整理成日志，审核数据质量，归集验收资料与材料票据，让人只确认有问题的部分。", keys: ["scribe", "dq", "records", "material"] },
  { name: "安全与质量", does: "盯照片里的隐患、工序的质控点和开工放行条件，发现问题就提醒并跟踪整改。", keys: ["safety", "quality", "gate"] },
  { name: "进度与产值", does: "判断进度风险、给出赶工方案，建立清单映射并核算产值，数字由系统统计复算。", keys: ["progress", "output"],
    without: { output: { name: "进度", does: "判断进度风险、给出赶工方案，数字由系统统计复算。" } } },
  { name: "简报与报表", does: "把各智能体的结果汇成每日简报、周报月报，并沉淀部位档案与索赔证据。", keys: ["brief", "report", "archivist"] },
  { name: "协同与问答", does: "回答有出处的提问，把一句话指令转成派单，按链条催办，并在现场圈里点评和接力。", keys: ["analyst", "dispatch", "chaser", "circle"] },
];

/// 智能体中心（10-10 改版：每组一张 zb-card，组内是智能体小卡；徽标统一中港蓝；采纳数写明是人员处置计数与样本数，样本不足显示“—（样本不足）”）
const AgentsCenter = {
  props: ["compact"],
  // 10-10 一屏化：各分组放进一个面板（电脑端面板内滚动，分组标题吸顶）；卡片职责 2 行；手机每个智能体一行，点开在抽屉里看职责、输入输出与人员反馈并可提问。
  template: `<div v-if="d" class="ra-agents">
  <div class="zb-card ra-pen" v-if="penEmpty"><div class="ra-pen-one"><ic n="chart"></ic><b>AI 渗透率</b><span>{{ penNote }}</span></div></div>
  <section class="zb-card" v-else><div class="zb-ch"><ic n="chart"></ic><h2>AI 渗透率</h2><span class="grow"></span><span class="zb-src">按系统记录统计</span></div>
    <div class="zb-body"><div class="ra-pen-g"><div v-for="(v, k) in d.penetration" :key="k" class="ra-pen-i"><b>{{ v.rate === null || v.rate === undefined ? '—' : pct(v.rate) }}</b>
      <span>{{ k }}<template v-if="v.note">（{{ v.note }}）</template><template v-else>（{{ v.ai }}/{{ v.n }}）</template></span></div></div>
    <details class="tiny muted mt"><summary>口径说明</summary>{{ d.definition }}</details></div></section>
  <section class="zb-card ra-ag-box"><div class="ra-ag-scroll" v-fit="220">
  <div class="ra-ag-sec" v-for="g in groups" :key="g.name"><div class="zb-ch ra-ag-gh"><ic :n="GROUP_IC[g.name] || 'bot'"></ic><h2>{{ g.name }}</h2><span class="zb-n">{{ g.active.length }}</span><span class="ra-does" :title="g.does">{{ g.does }}</span></div>
  <div class="ra-ag-grid"><div :class="['ra-ag', {askable: askable(a)}]" v-for="a in g.active" :key="a.key"
      @click="pick(a)" :title="askable(a) ? '点开向' + a.name + '提问' : null">
    <div class="ra-ag-h"><span class="ra-ag-ic"><ic :n="a.icon"></ic></span><b>{{ a.name }}</b><span class="grow"></span><button class="zb-chip ra-ask" v-if="askable(a)" @click.stop="ask(a)"><ic n="message"></ic>提问</button></div>
    <div class="ra-ag-d" :title="a.duty">{{ a.duty }}</div>
    <div class="ra-ag-io" :title="'输入：' + a.inputs + '；输出：' + a.outputs">输入：{{ a.inputs }}<br>输出：{{ a.outputs }}</div>
    <div class="ra-ag-f"><span :title="a.last ? '最近运行 ' + fmtDT(a.last) : ''">近 7 天 {{ a.runs_7d }} 次</span>
      <span class="chip orange" v-if="a.findings.open">待处理 {{ a.findings.open }}</span><span class="chip" v-if="a.findings.resolved">闭环 {{ a.findings.resolved }}</span></div>
    <div class="ra-ag-fb" v-if="a.feedback" title="采纳=人确认并处置闭环（预警为确认属实）；驳回=人标记误报或忽略并写原因。只统计人已处置的事项，是人员反馈计数，不是准确率。">
      <span>人员反馈 {{ a.feedback.decided }} 件：采纳 {{ a.feedback.adopted }} · 驳回 {{ a.feedback.rejected }}</span>
      <span class="ra-rate" v-if="a.feedback.rate !== null">采纳率 {{ pct(a.feedback.rate) }}（{{ a.feedback.adopted }}/{{ a.feedback.decided }}）</span><span class="ra-rate muted" v-else>采纳率 —（样本不足）</span></div>
    <div class="ra-ag-rs" v-if="a.feedback && a.feedback.reasons.length"><span class="muted">驳回原因（人填写）：</span><span class="reason" v-for="r in a.feedback.reasons" :key="r.text">{{ r.text }} ×{{ r.n }}</span></div>
    </div></div></div>
  <details class="ra-idle" v-if="idle.length"><summary class="zb-ch ra-ag-gh"><ic n="clock"></ic><h2>尚未运行的智能体</h2><span class="zb-n">{{ idle.length }}</span><span class="grow"></span><span class="zb-link">展开</span></summary>
    <div class="ra-ag-grid"><div :class="['ra-ag', 'idle', {askable: askable(a)}]" v-for="a in idle" :key="a.key" @click="pick(a)"><div class="ra-ag-h"><span class="ra-ag-ic"><ic :n="a.icon"></ic></span><b>{{ a.name }}</b><span class="grow"></span><button class="zb-chip ra-ask" v-if="askable(a)" @click.stop="ask(a)"><ic n="message"></ic>提问</button></div>
      <div class="ra-ag-d">{{ a.duty }}</div><div class="ra-ag-f"><span>所属：{{ a.group }}</span></div></div></div></details></div></section>
  <zb-drawer v-if="cur" :title="cur.name" @close="cur = null"><div class="ra-ag-dw">
    <p class="zb-read">{{ cur.duty }}</p>
    <div class="small"><b>输入</b>：{{ cur.inputs || '—' }}</div><div class="small"><b>输出</b>：{{ cur.outputs || '—' }}</div>
    <div class="small muted">近 7 天 {{ cur.runs_7d || 0 }} 次<template v-if="cur.last"> · 最近运行 {{ fmtDT(cur.last) }}</template><template v-if="cur.group"> · 所属：{{ cur.group }}</template></div>
    <div v-if="cur.findings && (cur.findings.open || cur.findings.resolved)"><span class="chip orange" v-if="cur.findings.open">待处理 {{ cur.findings.open }}</span> <span class="chip" v-if="cur.findings.resolved">闭环 {{ cur.findings.resolved }}</span></div>
    <div class="ra-ag-fb" v-if="cur.feedback"><span>人员反馈 {{ cur.feedback.decided }} 件：采纳 {{ cur.feedback.adopted }} · 驳回 {{ cur.feedback.rejected }}</span>
      <span class="ra-rate" v-if="cur.feedback.rate !== null">采纳率 {{ pct(cur.feedback.rate) }}（{{ cur.feedback.adopted }}/{{ cur.feedback.decided }}）</span><span class="ra-rate muted" v-else>采纳率 —（样本不足）</span>
      <span class="tiny muted">只统计人已处置的事项，是人员反馈计数，不是准确率。</span></div>
    <div class="ra-ag-rs" v-if="cur.feedback && cur.feedback.reasons.length"><span class="muted">驳回原因（人填写）：</span><span class="reason" v-for="r in cur.feedback.reasons" :key="r.text">{{ r.text }} ×{{ r.n }}</span></div></div>
    <template #foot v-if="askable(cur)"><button class="btn primary" @click="ask(cur)"><ic n="message"></ic>向{{ cur.name }}提问</button></template></zb-drawer>
  </div><load-state v-else loading :error="err" @retry="load"></load-state>`,
  data: () => ({ d: null, err: null, cur: null, GROUP_IC: { "现场记录与资料": "edit", "安全与质量": "shield", "进度与产值": "trend", "进度": "trend", "简报与报表": "file", "协同与问答": "message", "其他": "bot" } }),
  computed: {
    // 渗透率各项都没有数（如仿真项目一律不计）时，整张卡收成一行，不铺满屏的“—”
    penEmpty() { return Object.values(this.d.penetration || {}).every((v) => v.rate === null || v.rate === undefined); },
    penNote() { const n = Object.values(this.d.penetration || {}).map((v) => v.note).find(Boolean); return n || "还没有可统计的记录"; },
    allGroups() {
      const by = Object.fromEntries(this.d.agents.map((a) => [a.key, a])), used = new Set();
      const gs = AGENT_GROUPS.map((g) => {
        const alt = Object.entries(g.without || {}).find(([k]) => !by[k]);   // 接口按岗位剔除了某 Agent（如项目经理不看产值）时换用对应的分组名
        return { name: alt ? alt[1].name : g.name, does: alt ? alt[1].does : g.does, list: g.keys.map((k) => { used.add(k); return by[k]; }).filter(Boolean) };
      });
      const rest = this.d.agents.filter((a) => !used.has(a.key));
      if (rest.length) gs.push({ name: "其他", does: "尚未归入上述分组的智能体。", list: rest });
      return gs.filter((g) => g.list.length).map((g) => ({ ...g, active: g.list.filter((a) => a.runs > 0), idle: g.list.filter((a) => !a.runs) }));
    },
    // 主区只平铺运行过的智能体；从未运行（调用次数 0）的统一收进页底折叠区
    groups() { return this.allGroups.filter((g) => g.active.length); },
    idle() { return this.allGroups.flatMap((g) => g.idle.map((a) => ({ ...a, group: g.name }))); },
  },
  mounted() { this.load(); },
  methods: {
    pct, fmtDT,
    async load() { this.err = null; try { this.d = await GET("/api/agents"); } catch (e) { this.err = e; } },
    // 能不能问由后端按岗位给出（qa.can_ask：有问答范围、且本岗位看得到它的数据）；点卡片进入问工笔页，只查这个智能体的数据
    askable(a) { return !!a.askable; },
    ask(a) { if (this.askable(a)) go("agents", { tab: "ask", agent: a.key, name: a.name }); },
    // 电脑端点能提问的卡片直接进问答；手机（卡片只剩一行）和不能提问的卡片在抽屉里看详情
    pick(a) { if (window.innerWidth >= 861 && this.askable(a)) this.ask(a); else this.cur = a; },
  },
};

/// AI 产出被人采纳情况（应用成效页下半部分）：只用系统已有记录；样本不足 / 无记录如实标注。采纳、驳回是人员处置计数，不是准确率
const AdoptionTable = {
  template: `<section class="zb-card ra-sec" v-if="a"><div class="zb-ch"><ic n="check-circle"></ic><h2>AI 产出被人采纳情况</h2><span class="zb-n">{{ a.agents.length }}</span><help-tip>只数系统里已有的记录：AI 调用取自调用留痕，采纳/驳回取自人的实际处置（人员反馈计数，括号内为 采纳数/已处置数）；样本不足 {{ a.min_samples }} 显示“—（样本不足）”，没有记录显示“—”。采纳率不是准确率。</help-tip><span class="grow"></span><span class="tiny muted ra-idle-l" v-if="a.idle.length" :title="'还没有记录的智能体：' + a.idle.join('、')">另 {{ a.idle.length }} 个智能体还没有记录</span><span class="chip orange ra-mock" v-if="a.has_mock" title="含离线模拟模型的产出，仅供开发测试，不计入应用成效">含离线模拟 · 不计入成效</span><span class="zb-src">人员处置计数</span>
    <details class="defs ra-defs"><summary class="small zb-link">口径说明</summary><div class="ra-defs-pop"><div v-for="(v, k) in a.definitions" :key="k">{{ k }}：{{ v }}</div></div></details></div>
    <div class="zb-body">
    <div class="ra-sim" v-if="a.simulation && !sim"><ic n="info"></ic><span>仿真数据，不计入应用成效，不能作为试点或成效数据对外表述。</span></div>
    <empty-state compact class="mt" v-if="!a.agents.length" reason="还没有 AI 调用或人工处置记录。AI 产出被人确认或驳回后，会在这里按智能体统计。"></empty-state>
    <zb-paged v-else :items="a.agents" :size="10" :m-size="2" :fit="100" tag="table" cls="t ra-rt ra-adp" unit="个智能体"><template #head><thead><tr><th>智能体</th><th class="right">调用</th><th class="right">成功率</th><th class="right">降级/修复/补跑</th>
      <th class="right">采纳</th><th class="right">驳回</th><th class="right">修改率</th><th>驳回原因（人填写）</th></tr></thead></template>
    <template #default="{ item: r }"><tr>
      <td class="ra-rt-h"><b>{{ r.name }}</b><span class="chip orange" v-if="r.mock" style="margin-left:6px">含模拟 {{ r.mock }}</span></td>
      <td class="right" data-l="调用">{{ r.calls || '—' }}</td>
      <td class="right" data-l="成功率">{{ cell(r.success) }}</td>
      <td class="right" data-l="降级/修复/补跑">{{ r.calls ? (r.degraded + ' / ' + r.repaired + ' / ' + r.regen) : '—' }}</td>
      <td class="right" data-l="采纳">{{ cell(r.adoption) }}</td>
      <td class="right" data-l="驳回">{{ cell(r.rejection) }}</td>
      <td class="right" data-l="修改率">{{ !r.edit_ratio ? '—' : r.edit_ratio.median === null ? '—（样本不足，n=' + r.edit_ratio.n + '）' : pct(r.edit_ratio.median) + '（n=' + r.edit_ratio.n + '）' }}</td>
      <td class="small" data-l="驳回原因"><span v-if="!r.reasons.length" class="muted">—</span><span class="reason" v-for="x in r.reasons" :key="x.text">{{ x.text }} ×{{ x.n }}</span></td></tr>
      <tr class="ra-rt-sub" v-if="r.sources.length || r.confirm_only.length"><td colspan="8" class="tiny muted">
        <span v-for="s in r.sources" :key="s.label" style="margin-right:14px">{{ s.label }}：{{ s.adopted }} / {{ s.rejected }}</span>
        <span v-for="s in r.confirm_only" :key="s.label" style="margin-right:14px">{{ s.label }}：{{ s.done }} / {{ s.offered }}</span></td></tr></template></zb-paged>
    </div></section>`,
  data: () => ({ a: null }),
  computed: { sim: () => isSim() },   // sim：页面上方已有仿真提示时不再重复   // 手机只列前 2 个智能体，其余进抽屉
  async mounted() { try { this.a = (await GET("/api/metrics")).ai_adoption; } catch (e) { /* 成效页主体已提示 */ } },
  methods: {
    pct,
    cell(x) { return !x || !x.n ? "—" : x.rate === null ? `—（样本不足，n=${x.n}）` : `${pct(x.rate)}（${x.k}/${x.n}）`; },
  },
};

const AgentsHub = {
  components: { AgentsCenter, Ask, Metrics, AdoptionTable },
  template: `<div :class="'ra-hub ra-' + tab"><page-head :page="tab" :title="tab === 'ask' && agent ? '问' + agent.name : undefined">
      <template #meta>{{ meta }}<span class="chip" v-if="sim && tab !== 'ask'">仿真演示数据</span></template>
      <template #actions v-if="tab === 'ask' && agent && hasAgents"><button class="btn ghost sm" @click="go('agents')">换个智能体</button></template></page-head>
    <agents-center v-if="tab === 'agents'"></agents-center>
    <ask v-else-if="tab === 'ask'" :agent="agent" :key="agent ? agent.key : 'all'"></ask><div class="ra-metrics" v-else><metrics></metrics><adoption-table></adoption-table></div>
    <ai-note :page="tab"></ai-note></div>`,
  data: () => ({ tab: "agents", agent: null }),
  computed: {
    hasAgents: () => !!store.me && navRolePages(store.me.user.role).some((x) => x.k === "agents"),
    sim: () => isSim(),
    // 页头一行：范围与来源；仿真项目另加标记（应用成效的数字不能读成成效或试点数据）
    meta() {
      if (this.tab === "ask") return "回答只依据系统记录并标出处";
      if (this.tab === "metrics") return "数字取自系统记录；样本不足不下结论，不计算准确率";
      return "按替谁做什么分组；运行次数、处置数取自系统记录";
    },
  },
  methods: { go },
  created() {
    const p = store.route.params;
    this.tab = p.tab || "agents";
    if (this.tab === "metrics" && store.me && !navRolePages(store.me.user.role).some((x) => x.k === "metrics")) this.tab = "agents";
    if (p.agent) this.agent = { key: p.agent, name: p.name || p.agent };
  },
};

// 报表模板编辑（设置页内）
const TemplateEditor = {
  template: `<section class="zb-card ra-sec"><div class="zb-ch"><ic n="file"></ic><h2>周报/月报模板</h2><span class="zb-n" v-if="t">{{ t.sections.length }} 栏</span><span class="grow"></span>
      <div class="seg"><button :class="{on: kind === 'week'}" @click="kind = 'week'; load()">周报</button><button :class="{on: kind === 'month'}" @click="kind = 'month'; load()">月报</button></div></div>
    <div class="zb-body"><div class="tiny muted">报表按模板逐栏撰写；栏目标题、写作要求、字数上限在此定义。这里只改模板，不生成、不编辑、不发布报表。</div>
    <div v-if="t"><div class="field mt"><label class="f">报告标题（{project} 为项目名）</label><input class="in" v-model="t.title"></div>
      <div class="ra-tpl" v-for="(s, i) in t.sections" :key="s.key"><div class="ra-tpl-h"><span class="ra-tpl-n">{{ i + 1 }}</span><input class="in" v-model="s.title">
        <span class="chip">{{ s.type === 'list' ? '列表' : '叙述' }}</span><span class="chip blue" v-if="s.table">附表：{{ tables[s.table] }}</span><span class="grow"></span>
        <label class="small nowrap" v-if="s.type === 'narrative'">字数≤ <input class="in ra-num" type="number" v-model.number="s.max_chars"></label>
        <button class="btn ghost sm" @click="t.sections.splice(i, 1)">删除</button></div>
        <textarea class="in" rows="2" v-model="s.instruction" placeholder="写作要求"></textarea></div>
      <div class="zb-btns mt"><button class="btn primary" @click="save">保存模板</button><button class="btn" @click="reset">恢复默认</button></div></div>
    <load-state compact v-else loading></load-state></div></section>`,
  data: () => ({ kind: "week", t: null, tables: { progress: "形象进度", milestones: "节点预测", output: "产值", safety: "安全隐患", issues: "问题清单", next: "下期计划" } }),
  mounted() { this.load(); },
  methods: {
    async load() { this.t = await GET(`/api/reports/template/${this.kind}`); },
    async save() { try { this.t = await PUT(`/api/reports/template/${this.kind}`, this.t); toast("模板已保存"); } catch (e) { toast(e.message, true); } },
    async reset() { if (!confirm("恢复默认模板？")) return; await DEL(`/api/reports/template/${this.kind}`); this.load(); },
  },
};
