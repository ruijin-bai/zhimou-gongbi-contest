/* x_plan.js：计划体检页（#/planck）。挂在“进度与产值”线“预警”页签之后，项目经理、工程主管可见。
   - 体检：DCMA 14 点检查的结果（系统算）+ 计划体检智能体的诊断与建议（AI 写，数字以检查结果为准）；
   - 两期对比：当前与上一期 P6 更新的计划/实际日期变化；
   - 计划工效对实测工效：两期之间完成%增量 × 作业数量 ÷ 工作日，对比 P6 工效 UDF；
   - 控制节点倒计时与雨季提示在“预测与赶工”页（mgr.js Forecast），这里不重复；不涉及产值。 */

const PK_LEVEL = { bad: ["未通过", "red"], warn: ["需关注", "orange"], ok: ["通过", "green"] };
const pkSign = (d) => (d === null || d === undefined ? "—" : d > 0 ? `+${d}` : `${d}`);

const PlanCheckPage = {
  template: `<div class="pk"><page-head page="planck"><template #actions>
      <button class="btn primary ai" v-if="can('progress_ops') && h && !h.none" :disabled="abusy" @click="runAgent"><ic n="sparkles"></ic>{{ abusy ? '诊断中' : (h.agent ? '重新诊断' : 'AI 体检诊断') }}</button></template></page-head>
  <load-state class="mb" v-if="err || !h" :loading="!h" :error="err" @retry="load"></load-state>
  <empty-state v-else-if="h.none" reason="还没有导入 P6 计划。导入 XER 后，这里会体检计划逻辑、约束、浮时和工效。" :action="can('progress_ops') ? {label: '去导入 P6 计划', route: 'progress?tab=plan'} : null"></empty-state>
  <template v-else>
    <div class="zb-kpis n4 rp-kpis">
      <div :class="['zb-kpi', 'rp-static', h.summary.bad ? 'red' : 'green']"><span class="zb-kpi-ic"><ic n="x"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">未通过</span>
        <b class="zb-kpi-v">{{ h.summary.bad }}<small> 项</small></b><span class="zb-kpi-s">{{ h.summary.bad ? '先整理这些计划问题' : '没有未通过的检查' }}</span></span></div>
      <div :class="['zb-kpi', 'rp-static', h.summary.warn ? 'orange' : '']"><span class="zb-kpi-ic"><ic n="alert"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">需关注</span>
        <b class="zb-kpi-v">{{ h.summary.warn }}<small> 项</small></b><span class="zb-kpi-s">接近阈值</span></span></div>
      <div class="zb-kpi rp-static green"><span class="zb-kpi-ic"><ic n="check-circle"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">通过</span>
        <b class="zb-kpi-v">{{ h.summary.ok }}<small> 项</small></b><span class="zb-kpi-s">共 {{ h.checks.length }} 项检查</span></span></div>
      <div class="zb-kpi rp-static"><span class="zb-kpi-ic"><ic n="calendar"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">本期计划</span>
        <b class="zb-kpi-v">{{ h.activities }}<small> 项作业</small></b><span class="zb-kpi-s" :title="h.file + ' · 数据日期 ' + (h.data_date || '未知')">{{ h.relations }} 条关系 · 数据 {{ h.data_date ? h.data_date.slice(5) : '未知' }}</span></span></div></div>
    <div class="rp-bar pk-tabs"><div class="seg" role="tablist"><button v-for="t in VIEWS" :key="t[0]" role="tab" :aria-selected="view === t[0]" :class="{on: view === t[0]}" @click="view = t[0]">{{ t[1] }}</button></div>
      <span class="tiny muted rp-cur">{{ h.file }}</span></div>
    <section class="zb-card rp-sec pk-sec" v-if="view === 'health'"><div class="zb-ch"><ic n="clipboard"></ic><h2>计划体检</h2><help-tip text="逐项检查 P6 计划本身是否合理：逻辑关系是否完整、约束和浮时是否异常、工效是否可信。每行的“数量 / 总数（百分比）”是有问题的作业占全部作业的比例，比例越高越该先整理计划。"></help-tip>
        <span class="zb-src">系统按 P6 导入数据计算</span><help-tip label="检查方法" :text="h.method"></help-tip></div>
      <div class="banner info" v-if="!h.has_ext">这一期是旧版导入，没有日历、约束和工效数据；用 XER 重新导入后，约束、工效类检查才完整。</div>
      <div class="ai-box pk-agent" v-if="h.agent"><div class="hd">计划体检智能体 · {{ srcText(h.agent.source) }} · {{ fmtDT(h.agent.created_at) }}</div>
        <zb-clamp :text="h.agent.diagnosis" :lines="2" title="计划体检智能体 · 诊断与建议" more="诊断与建议"><template #full><b>{{ h.agent.diagnosis }}</b>
          <ol class="pk-adv"><li v-for="(a, i) in h.agent.advice" :key="i"><span class="chip blue">{{ a.title }}</span> {{ a.text }}</li></ol>
          <div class="tiny muted">{{ h.agent.note }}</div></template></zb-clamp></div>
      <div class="pk-checks"><zb-paged :items="shownChecks" :size="8" :m-size="5" unit="项检查" :fit="200"><template #default="{ item: c }">
        <div :key="c.key" :class="['pk-c', 'lv-' + c.level, {open: open[c.key]}]">
          <div class="pk-row" role="button" tabindex="0" @click="toggle(c)" @keyup.enter="toggle(c)">
            <span :class="['chip', lv(c)[1]]">{{ lv(c)[0] }}</span><b>{{ c.title }}</b>
            <span class="pk-n" :title="c.total ? '有问题的作业 ' + c.count + ' 项，占全部 ' + c.total + ' 项的 ' + fmtN(c.share) + '%' : null">{{ c.count }}<small v-if="c.total"> / {{ c.total }}（{{ fmtN(c.share) }}%）</small></span>
            <span class="grow"></span><span class="tiny muted pk-rule">{{ c.rule }}</span><ic n="chevron-down" :class="['pk-chev', {rot: open[c.key]}]" v-if="c.count"></ic></div>
          <div class="pk-body" v-if="open[c.key] && c.count"><div class="small muted">{{ c.why }}</div>
            <zb-more :items="c.sample" :limit="3" :m-limit="3" :title="c.title" unit="项"><template #default="{ item: s, full }"><div class="readrow" :key="s.code + (s.note || '')"><b><act-name :c="s.code" :n="s.name" :s="s.short_name"></act-name></b> <span class="tiny muted" v-if="s.note">{{ s.note }}</span></div>
              <div class="tiny muted" v-if="full && s === c.sample[c.sample.length - 1] && c.count > c.sample.length">另有 {{ c.count - c.sample.length }} 项，可在“P6 计划”页按代码搜索。</div></template></zb-more>
            <div class="tiny muted" v-if="c.sample.length <= 3 && c.count > c.sample.length">另有 {{ c.count - c.sample.length }} 项，可在“P6 计划”页按代码搜索。</div></div></div></template></zb-paged>
        <button type="button" class="rp-okfold" v-if="okChecks.length" :aria-expanded="showOk" @click="showOk = !showOk"><ic n="check-circle"></ic>通过的 {{ okChecks.length }} 项<span class="grow"></span>{{ showOk ? '收起' : '展开' }}<ic n="chevron-down" :class="['pk-chev', {rot: showOk}]"></ic></button></div></section>

    <section class="zb-card rp-sec pk-sec" v-else-if="view === 'cmp'"><div class="zb-ch"><ic n="layers"></ic><h2>两期对比</h2><help-tip text="把这一期 P6 更新和上一期逐项对比：完工日、节点日期、开工完工数量、关键作业和作业增减各变了多少。“+N 天”表示比上一期推迟，“−N 天”表示提前。"></help-tip><span class="tiny muted" v-if="cmp && cmp.previous">{{ cmp.previous.data_date }} → {{ cmp.current.data_date }}</span>
        <help-tip v-if="cmp && !cmp.none && cmp.note" label="对比口径" :text="cmp.note"></help-tip></div>
      <load-state compact v-if="!cmp" :loading="true"></load-state>
      <div class="zb-empty" v-else-if="cmp.none">{{ cmp.reason || '没有可对比的上一期。' }}</div>
      <template v-else>
        <div class="pk-kpis rp-k5"><div><div class="l">P6 完工</div><div class="v">{{ cmp.project_finish.after || '—' }}</div><div :class="['tiny', cmp.project_finish.days > 0 ? 'pk-late' : 'muted']">较上期 {{ pkSign(cmp.project_finish.days) }} 天</div></div>
          <div><div class="l">本期开工 / 完工</div><div class="v">{{ cmp.counts.started }} / {{ cmp.counts.finished }}</div><div class="tiny muted">项作业</div></div>
          <div><div class="l">完成日推迟</div><div class="v">{{ cmp.counts.slipped }}</div><div class="tiny muted">提前 {{ cmp.counts.gained }} 项</div></div>
          <div><div class="l">关键作业</div><div class="v">{{ cmp.counts.critical_after }}</div><div class="tiny muted">新进入 {{ cmp.counts.critical_new }} 项</div></div>
          <div><div class="l">作业增减</div><div class="v">+{{ cmp.counts.added }} / −{{ cmp.counts.removed }}</div><div class="tiny muted">关系 {{ cmp.counts.relations_before }} → {{ cmp.counts.relations_after }}</div></div></div>
        <div class="pk-two">
          <div class="pk-col"><h3 class="pk-h3">节点变化</h3>
            <div v-fit><div class="readrow" v-for="m in movedMs" :key="m.code"><b><act-name :c="m.code" :n="m.name" :s="m.short_name"></act-name></b> <span class="tiny muted">{{ m.before }} → {{ m.after }}</span>
              <span :class="['chip', m.days > 0 ? 'red' : 'green']">{{ pkSign(m.days) }} 天</span></div>
            <div class="muted small" v-if="!movedMs.length">各节点日期与上期一致。</div></div></div>
          <div class="pk-col"><h3 class="pk-h3">完成日推迟最多的作业</h3>
            <div v-fit><zb-more :items="cmp.slipped.slice(0, 8)" :limit="8" :m-limit="4" title="完成日推迟最多的作业" unit="项"><template #default="{ item: r }"><div class="readrow" :key="r.code"><span v-if="r.critical" class="chip red">关键</span> <b><act-name :c="r.code" :n="r.name" :s="r.short_name"></act-name></b>
              <span class="tiny muted">{{ r.before }} → {{ r.after }}</span> <span class="chip orange">+{{ r.days }} 天</span></div></template></zb-more>
            <div class="muted small" v-if="!cmp.slipped.length">没有完成日推迟的作业。</div></div></div></div>
        <div class="small pk-se" v-if="cmp.started.length || cmp.finished.length">
          <span class="muted">本期开工：</span><span v-for="s in cmp.started.slice(0, 6)" :key="'s' + s.code" class="pk-tag"><act-name :c="s.code" :n="s.name" :s="s.short_name"></act-name></span>
          <span class="muted" v-if="cmp.finished.length">　本期完工：</span><span v-for="s in cmp.finished.slice(0, 6)" :key="'f' + s.code" class="pk-tag"><act-name :c="s.code" :n="s.name" :s="s.short_name"></act-name></span></div></template></section>

    <section class="zb-card rp-sec pk-sec" v-else><div class="zb-ch"><ic n="hourglass"></ic><h2>计划工效对实测工效</h2><help-tip text="系数＝计划工效 ÷ 实测工效。×1.00 表示和计划一样快；×1.25 表示实际比计划慢，同样的量要多用约 25% 的时间（橙色）；小于 1 表示比计划快（绿色）。"></help-tip><span class="tiny muted" v-if="pr && pr.previous">{{ pr.previous.data_date }} → {{ pr.current.data_date }}</span>
        <help-tip v-if="pr && !pr.none" label="计算方法" :text="pr.method + '。未比较：无工效或数量 ' + pr.skipped.no_prod + ' 项，单位对不上 ' + pr.skipped.unit + ' 项。'"></help-tip></div>
      <load-state compact v-if="!pr" :loading="true"></load-state>
      <div class="zb-empty" v-else-if="pr.none">{{ pr.reason }}</div>
      <template v-else>
        <div class="scroll-x"><zb-paged v-if="pr.rows.length" :items="pr.rows" :size="10" :m-size="5" unit="项作业" tag="table" cls="t pk-pr" :fit="160"><template #head><thead><tr><th>作业</th><th class="right">计划工效</th><th class="right">实测工效</th><th class="right">系数</th><th class="right">期间完成</th><th class="right">工作日</th></tr></thead></template>
          <template #default="{ item: r }"><tr :key="r.code"><td><span v-if="r.critical" class="chip red">关键</span> <b><act-name :c="r.code" :n="r.name" :s="r.short_name"></act-name></b><div class="tiny muted">数量 {{ r.qty_text }} · 完成 {{ fmtN(r.pct_before) }}% → {{ fmtN(r.pct_after) }}%</div></td>
            <td class="right nowrap">{{ fmtN(r.plan_rate) }} {{ r.unit }}/天</td><td class="right nowrap">{{ fmtN(r.actual_rate) }} {{ r.unit }}/天</td>
            <td class="right"><span :class="['chip', r.level === 'slow' ? 'orange' : r.level === 'fast' ? 'green' : '']">{{ r.factor === null ? '—' : '×' + fmtN(r.factor, 2) }}</span></td>
            <td class="right nowrap">{{ fmtN(r.qty_done) }} {{ r.unit }}</td><td class="right">{{ fmtN(r.workdays) }}</td></tr></template></zb-paged>
          <div class="zb-empty" v-else>两期之间没有可比的作业（需要完成%有增量、且有数量与计划工效）。</div></div></template></section>
  </template>
  <ai-note page="planck"></ai-note></div>`,
  // 一屏化（10-10）：体检 / 两期对比 / 工效对比改为页内页签（不再三张卡上下堆叠）；长列表在面板内滚动；方法与口径说明收进小问号；智能体诊断截两行、全文进抽屉
  data: () => ({ h: null, cmp: null, pr: null, err: null, abusy: false, open: {}, showOk: false, view: "health",
    VIEWS: [["health", "计划体检"], ["cmp", "两期对比"], ["prod", "工效对比"]] }),
  computed: {
    // 10-10：未通过、需关注在前；通过的收起（点“通过的 N 项”展开），长列表先给汇总
    badChecks() { const o = { bad: 0, warn: 1 }; return this.h.checks.filter((c) => c.level !== "ok").sort((x, y) => (o[x.level] ?? 2) - (o[y.level] ?? 2)); },
    okChecks() { return this.h.checks.filter((c) => c.level === "ok"); },
    shownChecks() { return this.showOk ? this.badChecks.concat(this.okChecks) : this.badChecks; },
    movedMs() { return this.cmp && this.cmp.milestones ? this.cmp.milestones.filter((m) => m.days).slice(0, 8) : []; },
  },
  mounted() { this.load(); },
  methods: {
    can, fmtN, fmtDT, pkSign,
    lv(c) { return PK_LEVEL[c.level] || ["", ""]; },
    srcText: (s) => (s === "ai" && store.me.ai && store.me.ai.provider === "mock" ? "离线模拟模型" : s === "ai" ? "AI 撰写" : s === "mock" ? "离线模拟模型" : s ? "系统模板" : "来源未标注"),   // 只有真 AI 才写 AI
    toggle(c) { if (c.count) this.open = { ...this.open, [c.key]: !this.open[c.key] }; },
    async load() {
      this.err = null;
      try {
        this.h = await GET("/api/plan/health");
        if (this.h.none) return;
        const first = this.h.checks.find((c) => c.level !== "ok" && c.count);
        if (first && !Object.keys(this.open).length) this.open = { [first.key]: true };
        [this.cmp, this.pr] = await Promise.all([GET("/api/plan/compare"), GET("/api/plan/productivity")]);
      } catch (e) { this.err = e; }
    },
    async runAgent() {
      this.abusy = true;
      try { const a = await POST("/api/plan/health/agent"); this.h = { ...this.h, agent: a }; } catch (e) { toast(e.message, true); } finally { this.abusy = false; }
    },
  },
};

// 雨季月份压成区间：["2026-10","2027-05","2027-06"] → "2026-10、2027-05～06"
function pkMonths(ms) {
  const out = [];
  let a = null, b = null;
  const next = (m) => { const [y, mo] = m.split("-").map(Number); return mo === 12 ? `${y + 1}-01` : `${y}-${String(mo + 1).padStart(2, "0")}`; };
  for (const m of ms) {
    if (b && next(b) === m) { b = m; continue; }
    if (a) out.push(a === b ? a : `${a}～${b.slice(0, 4) === a.slice(0, 4) ? b.slice(5) : b}`);
    a = b = m;
  }
  if (a) out.push(a === b ? a : `${a}～${b.slice(0, 4) === a.slice(0, 4) ? b.slice(5) : b}`);
  return out.join("、");
}

EXT.route("planck", PlanCheckPage);
EXT.nav(["manager", "eng_sup"], { k: "planck", route: "planck", label: "计划体检", short: "体检", icon: "clipboard", line: "prog", after: "plan" });   // 进度第二个：计划 → 体检 → 预警（RJ 10-09）
