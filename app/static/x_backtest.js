/* x_backtest.js：历史回测页面（P0-2，T-04b）。路由 #/backtest，可带 ?id=N 打开指定一次（应用成效页那一行的链接）。
   - 入口：挂在“进度与产值”线“预警”页签之后，只给工程主管；管理员不设入口，读接口仍可直达（G-13）；
   - 内容依次为：结论卡（主指标命中率，同时给召回率、实际延误占比、样本数、平均提前量、数据缺口，D1）→ 期次校验与混淆矩阵
     → 误报/漏报清单（点开看各期原始值）→ 3×3 阈值敏感性表与“采用阈值”（只给工程主管，且只能采用真实项目数据的回测；测试数据按钮置灰，接口返回 409）→ 口径说明；可导出 Word；
   - 页头与底部说明的文案在 x_ui.js 的 PAGE_HEADS.backtest / AI_NOTES.backtest；
   - 比例一律用后端算好的值，前端不补算、不补零：分母为 0 写“无预警样本”，分母不足写“样本不足（k/n）”，不出百分比（AC-2.11）；
   - 来源徽标（真实项目数据 / 仿真/测试数据）与侧栏的仿真角标分开；本页只放回测数字，现场人工反馈命中率在“预警”页签，二者不合并（AC-2.12）；
   - 上传时必须选数据来源；空状态按钮直接打开文件选择（U-05①）；XER 原文件不保存，剔除期次重跑要用同一批文件；
   - 明细很大（700 作业 × 6 期约 2MB）：结果整体不做深层响应式，清单分页、各期原始值点开才渲染。 */

const BT_SOURCES = [["real", "真实项目数据"], ["test", "仿真/测试数据"]];
const BT_TOLS = [0, 3, 7];
const BT_MIN_FILES = 2;
const BT_STEP = 20;
const BT_SKIP = { no_dates: "缺计划日期", missing_in_judge: "判定期缺该作业", missing_in_last: "最晚一期缺该作业", float_unknown: "总浮时未知", no_fact_date: "没有事实日期" };
const BT_LEAD = [["earlier", "比 P6 更早"], ["same", "与 P6 同期"], ["later", "比 P6 更晚"], ["only_ours", "仅本规则判出"], ["never", "本规则未判出"]];
const BT_PCT_TYPE = { CP_Drtn: "工期%", CP_Units: "数量%", CP_Phys: "实物%", "未标注": "未标注" };
const BT_RULE = { behind: "进度滞后", late_start: "应开未开" };
const BT_OUTCOME = { FP: ["误报", "orange"], FN: ["漏报", "red"] };

// ---------------------------------------------------------------------------- 纯函数（tests/test_backtest_ui.py 在 node 里直接跑）
const btN = (v) => (v === null || v === undefined || v === "" || !Number.isFinite(+v) ? "—" : String(+(+v).toFixed(1)));
// 与 Word 导出（services/backtest.fmt_rate）同一写法：x.x%（k/n）；分母不足写“样本不足（k/n）”；分母为 0 写 zero
function btRateText(r, zero = "—") {
  if (!r) return "—";
  const kn = `（${r.k}/${r.n}）`;
  if (r.rate !== null && r.rate !== undefined) return `${(r.rate * 100).toFixed(1)}%${kn}`;
  return (r.n ? "样本不足" : zero) + kn;
}
// 指标卡用：大字 v + 小字 k/n
function btRateParts(r, zero = "—") {
  if (!r) return { v: "—", kn: "" };
  if (r.rate !== null && r.rate !== undefined) return { v: `${(r.rate * 100).toFixed(1)}%`, kn: `${r.k}/${r.n}` };
  return r.n ? { v: "样本不足", kn: `${r.k}/${r.n}` } : { v: zero, kn: "" };
}
// 提前量：正数＝本规则比 P6 总浮时转负更早
function btLead(d) {
  if (d === null || d === undefined) return "—";
  const x = +d;
  return x > 0 ? `提前 ${btN(x)} 天` : x < 0 ? `晚 ${btN(-x)} 天` : "与 P6 同期";
}
const btMMDD = (s) => (s ? String(s).slice(5, 10) : "");
// 页头一行：回测于 MM-DD（真实数据，不在仿真时间线内）
function btMeta(v) { return `回测于 ${btMMDD(v.created_at)}（${v.source_note || v.source_name || ""}）`; }
// 结论一句话：来源 · 标签 · N 期 · 数据日期 · 样本 · 命中率 · 召回率 · 平均提前 · 数据缺口
function btHeadline(v) {
  const s = v.summary || {}, lt = v.lead_time || {};
  const out = [v.source === "real" ? "真实项目" : "测试数据"];
  if (v.label) out.push(v.label);
  out.push(`${s.periods} 期`, `数据日期 ${s.data_date_from}～${s.data_date_to}`, `样本 ${s.samples}`,
    `命中率 ${btRateText(s.precision, "无预警样本")}`, `召回率 ${btRateText(s.recall)}`);
  if (lt.mean_days !== null && lt.mean_days !== undefined) out.push(`平均${btLead(lt.mean_days)}`);
  out.push(`数据缺口 ${s.data_gaps} 个不计入`);
  return out.join(" · ");
}
// 只收 .xer；其余如实列出
function btSplitFiles(list) {
  const ok = [], bad = [];
  for (const f of list || []) (/\.xer$/i.test(f.name || "") ? ok : bad).push(f);
  return { ok, bad };
}
// 能否开始回测：至少 2 期，且必须选数据来源
function btCanRun(f) {
  const n = (f.files || []).length;
  if (n < BT_MIN_FILES) return { ok: false, why: n ? `还差 ${BT_MIN_FILES - n} 期：回测至少要 2 期 .xer（建议 3 期以上）` : "请选择 .xer 文件：至少 2 期，建议 3 期以上" };
  if (!f.source) return { ok: false, why: "请选择数据来源：真实项目数据，或仿真/测试数据" };
  return { ok: true, why: "" };
}
function btFilesHint(n) {
  if (!n) return "";
  if (n < BT_MIN_FILES) return `已选 ${n} 期，还差 ${BT_MIN_FILES - n} 期`;
  if (n === BT_MIN_FILES) return "已选 2 期：事实与判定取自同一期，结果为弱口径，仅供参考（建议 3 期以上）";
  return `已选 ${n} 期`;
}
// 剔除后至少还要 2 期
const btExcludeOk = (periods, excl) => (periods || []).filter((p) => !(excl || []).includes(p.data_date)).length >= BT_MIN_FILES;
const btSize = (b) => (b >= 1048576 ? `${(b / 1048576).toFixed(1)}MB` : `${Math.max(1, Math.round(b / 1024))}KB`);
const btPctTypes = (m) => Object.entries(m || {}).map(([k, n]) => `${BT_PCT_TYPE[k] || k} ${n}`).join("、") || "—";

// ---------------------------------------------------------------------------- 结论卡
const BtSummary = {
  props: ["v"],
  template: `<div class="zb-card bt-sum">
    <div class="bt-line">{{ headline }}</div>
    <div class="grid g3 mt bt-kpis">
      <div class="kpi bt-main"><div class="v">{{ prec.v }}<small v-if="prec.kn">（{{ prec.kn }}）</small></div><div class="l">命中率（主指标）：发出的红色预警中，后来确实延误的比例</div></div>
      <div class="kpi"><div class="v">{{ rec.v }}<small v-if="rec.kn">（{{ rec.kn }}）</small></div><div class="l">召回率：确实延误的样本中，被提前判红的比例</div></div>
      <div class="kpi"><div class="v">{{ share.v }}<small v-if="share.kn">（{{ share.kn }}）</small></div><div class="l">实际延误占比：样本里本来就有多少延误</div></div>
      <div class="kpi"><div class="v">{{ s.samples }}</div><div class="l">样本（作业×期次）· 数据缺口 {{ s.data_gaps }} 个不计入</div></div>
      <div class="kpi"><div class="v">{{ lead }}</div><div class="l">平均提前量（与 P6 总浮时转负相比，{{ lt.compared || 0 }} 个作业可比<template v-if="lt.median_days !== null && lt.median_days !== undefined">，中位数{{ btLead(lt.median_days) }}</template>）</div></div>
      <div class="kpi"><div class="v">{{ s.fact_actual }}<small> / {{ s.fact_forecast }}</small></div><div class="l">事实来源：实际完成 / P6 预测完成（取最晚一期 {{ s.fact_date }}）</div></div>
    </div>
    <ul class="bt-notes" v-if="notes.length"><li v-for="(n, i) in notes" :key="i">{{ n }}</li></ul>
    <h3 class="mt">三种口径对照</h3>
    <div class="scroll-x"><table class="t bt-cmp"><thead><tr><th>指标</th><th class="right">作业×期次（主表）</th><th class="right">只算已完工</th><th class="right">按作业去重</th></tr></thead>
      <tbody><tr v-for="r in cmp" :key="r[0]" :class="{'bt-ref': r[4]}"><td>{{ r[0] }}</td><td class="right">{{ r[1] }}</td><td class="right">{{ r[2] }}</td><td class="right">{{ r[3] }}</td></tr></tbody></table></div>
    <div class="small mt" v-if="lt.delayed">提前量：确实延误的作业 {{ lt.delayed }} 个，<span v-for="c in leadCats" :key="c[0]" class="bt-cat">{{ c[1] }} {{ lt[c[0]] || 0 }}</span></div>
    <div class="tiny muted mt">本页只含回测结果。现场人工反馈的命中率（人确认属实/误报）在<a @click="go('progress', {tab: 'alerts'})">预警</a>页签，仿真项目的数字不计入应用成效；三者口径不同，不合并。</div>
  </div>`,
  computed: {
    s() { return this.v.summary || {}; },
    lt() { return this.v.lead_time || {}; },
    headline() { return btHeadline(this.v); },
    prec() { return btRateParts(this.s.precision, "无预警样本"); },
    rec() { return btRateParts(this.s.recall); },
    share() { return btRateParts(this.s.delay_share); },
    lead() { return btLead(this.lt.mean_days); },
    leadCats: () => BT_LEAD,
    notes() {
      const out = [...(this.s.notes || [])];
      const sk = Object.entries(this.s.skipped || {}).filter(([, n]) => n);
      if (sk.length) out.push(`另有 ${sk.reduce((a, [, n]) => a + n, 0)} 个作业×期次未进入样本：` + sk.map(([k, n]) => `${BT_SKIP[k] || k} ${n}`).join("、"));
      if (this.s.same_period_samples) out.push(`其中 ${this.s.same_period_samples} 个样本的判定期就是最晚一期（事实与判定同期）`);
      return out;
    },
    cmp() {
      const a = this.s, b = a.strict || {}, c = a.by_activity || {}, z = "无预警样本";
      const row = (name, f, ref) => [name, f(a), f(b), f(c), ref];
      return [
        row("样本数", (x) => btN(x.samples)), row("TP 命中", (x) => btN(x.tp)), row("FP 误报", (x) => btN(x.fp)), row("FN 漏报", (x) => btN(x.fn)), row("TN", (x) => btN(x.tn)),
        row("命中率（主指标）", (x) => btRateText(x.precision, z)), row("召回率", (x) => btRateText(x.recall)), row("实际延误占比", (x) => btRateText(x.delay_share)),
        row("(TP+TN)/N（仅作参考，天然偏高）", (x) => btRateText(x.accuracy_ref), true),
      ];
    },
  },
  methods: { go, btLead },
};

// ---------------------------------------------------------------------------- 期次校验与混淆矩阵
const BtPeriods = {
  props: ["v", "canEdit", "excl"],
  emits: ["update:excl", "rerun"],
  template: `<div class="zb-card bt-per"><div class="row between wrap"><h2>期次与混淆矩阵</h2>
      <span class="tiny muted">上传 {{ s.periods_uploaded }} 期，参与 {{ s.periods }} 期</span></div>
    <div class="scroll-x"><table class="t bt-per-t"><thead><tr><th v-if="canEdit">剔除</th><th>期次</th><th>数据日期</th><th>文件</th><th class="right">作业</th><th>完成%类型</th><th class="right">已完工</th><th class="right">与上期代码重合</th><th>说明</th></tr></thead>
      <tbody><tr v-for="p in v.periods_report" :key="p.data_date" :class="{'bt-off': p.excluded}">
        <td v-if="canEdit"><input type="checkbox" :checked="excl.includes(p.data_date)" @change="toggle(p.data_date)" :aria-label="'剔除 ' + p.data_date"></td>
        <td>{{ p.index + 1 }}</td><td class="nowrap">{{ p.data_date }}</td><td class="small">{{ p.file }}</td><td class="right">{{ p.activities }}</td>
        <td class="small">{{ btPctTypes(p.pct_types) }}</td><td class="right">{{ p.completed }}</td>
        <td class="right">{{ p.overlap_prev === null || p.overlap_prev === undefined ? '—' : (p.overlap_prev * 100).toFixed(0) + '%' }}</td>
        <td class="small"><span class="chip" v-if="p.excluded">已剔除</span><span class="chip blue" v-else-if="p.role">{{ p.role }}</span>
          <span v-for="f in p.flags" :key="f" :class="['chip', p.suspect ? 'orange' : '']">{{ f }}</span></td></tr></tbody></table></div>
    <div class="row wrap mt" v-if="canEdit && changed"><button class="btn primary" :disabled="!ok" @click="$emit('rerun', excl.slice())">按勾选的期次重新回测</button>
      <span class="small" v-if="!ok">剔除后至少要保留 2 期</span><span class="tiny muted" v-else>系统不保存 XER 原文件；本次会话里没有这批文件时，需重新选择同一批文件</span></div>
    <div class="bt-mx-wrap mt">
      <div class="bt-mx" role="table" aria-label="混淆矩阵（作业×期次）">
        <div class="bt-mx-h"></div><div class="bt-mx-h">后来确实延误</div><div class="bt-mx-h">后来未延误</div>
        <div class="bt-mx-h">判为红色</div><div class="bt-mx-c"><b>{{ s.tp }}</b><span>TP 命中</span></div><div class="bt-mx-c"><b>{{ s.fp }}</b><span>FP 误报</span></div>
        <div class="bt-mx-h">未判红</div><div class="bt-mx-c"><b>{{ s.fn }}</b><span>FN 漏报</span></div><div class="bt-mx-c"><b>{{ s.tn }}</b><span>TN</span></div>
      </div>
      <div class="scroll-x grow"><table class="t"><thead><tr><th>计划期 → 判定期</th><th class="right">样本</th><th class="right">TP</th><th class="right">FP</th><th class="right">FN</th><th class="right">TN</th><th class="right">数据缺口</th></tr></thead>
        <tbody><tr v-for="b in s.by_period || []" :key="b.judge_date"><td class="nowrap">{{ b.plan_date }} → {{ b.judge_date }}<span class="tiny muted" v-if="b.same_period">（与事实同期）</span></td>
          <td class="right">{{ b.samples }}</td><td class="right">{{ b.tp }}</td><td class="right">{{ b.fp }}</td><td class="right">{{ b.fn }}</td><td class="right">{{ b.tn }}</td><td class="right">{{ b.data_gaps }}</td></tr></tbody></table></div>
    </div></div>`,
  computed: {
    s() { return this.v.summary || {}; },
    changed() { const a = [...this.excl].sort().join(","), b = [...(this.s.excluded_periods || [])].sort().join(","); return a !== b; },
    ok() { return btExcludeOk(this.v.periods_report, this.excl); },
  },
  methods: {
    btPctTypes,
    toggle(d) { const x = this.excl.includes(d) ? this.excl.filter((y) => y !== d) : [...this.excl, d]; this.$emit("update:excl", x); },
  },
};

// ---------------------------------------------------------------------------- 误报 / 漏报清单（点开看各期原始值）与数据缺口
const BtOdd = {
  props: ["v"],
  template: `<div class="zb-card bt-odd"><div class="row between wrap"><h2>误报与漏报</h2>
      <div class="seg"><button v-for="t in tabs" :key="t[0]" :class="{on: kind === t[0]}" @click="setKind(t[0])">{{ t[1] }}（{{ count(t[0]) }}）</button></div></div>
    <div class="tiny muted mb"><help-tip caption="误报 / 漏报怎么算" text="误报：判红但后来没延误；漏报：没判红但后来延误。点一行看这个作业在各期的原始数据。"></help-tip></div>
    <div class="scroll-x" v-if="list.length"><table class="t bt-odd-t"><thead><tr><th>类别</th><th>作业</th><th>判定期</th><th>判定</th><th class="right">总浮时</th><th>计划完成</th><th>事实完成</th><th class="right">推迟</th><th></th></tr></thead>
      <tbody><template v-for="r in shown" :key="key(r)">
        <tr class="click" @click="flip(r)" :aria-expanded="!!open[key(r)]">
          <td><span :class="['chip', OUT[r.outcome][1]]">{{ OUT[r.outcome][0] }}</span></td>
          <td><span class="chip red" v-if="r.critical">关键</span> <b><act-name :c="r.code" :n="r.name" :s="r.short_name"></act-name></b></td>
          <td class="nowrap small">{{ r.judge_date }}</td>
          <td class="small nowrap">{{ r.predicted ? '判红' : (r.level ? LEVEL[r.level] || r.level : '未预警') }}<template v-if="r.slip !== null && r.slip !== undefined"> · 滞后 {{ btN(r.slip) }} 天</template></td>
          <td class="right">{{ btN(r.float) }}</td><td class="nowrap small">{{ r.plan_finish }}</td>
          <td class="nowrap small">{{ r.fact_finish }} <span :class="['chip', r.fact_kind === 'actual' ? 'green' : '']">{{ r.fact_kind === 'actual' ? '实际' : 'P6 预测' }}</span></td>
          <td class="right">{{ btN(r.true_delay) }} 天</td><td class="tiny muted">{{ open[key(r)] ? '收起' : '各期' }}</td></tr>
        <tr v-if="open[key(r)]" class="bt-hist"><td colspan="9">
          <div class="small">判定时（{{ r.plan_date }} 的计划与浮时 + {{ r.judge_date }} 的完成%）：计划应完成 {{ pctOf(r.pct_plan) }}，实际 {{ pctOf(r.pct_actual) }}（上期 {{ pctOf(r.pct_start) }}）<template v-if="r.rule">，规则：{{ RULE[r.rule] || r.rule }}</template>；
            事实取最晚一期：{{ r.fact_kind === 'actual' ? '实际完成' : 'P6 预测完成' }} {{ r.fact_finish }}，比计划完成推迟 {{ btN(r.true_delay) }} 天，总浮时 {{ btN(r.float) }} 天</div>
          <div class="scroll-x mt" v-if="hist(r).length"><table class="t bt-sub"><thead><tr><th>数据日期</th><th>状态</th><th class="right">完成%</th><th class="right">总浮时</th><th>计划开始</th><th>计划完成</th><th>实际开始</th><th>实际完成</th></tr></thead>
            <tbody><tr v-for="h in hist(r)" :key="h.data_date"><td class="nowrap">{{ h.data_date }}</td><td>{{ h.status || '—' }}</td>
              <td class="right">{{ pctOf(h.pct) }}<span class="tiny muted" v-if="h.pct_type"> {{ PT[h.pct_type] || h.pct_type }}</span></td><td class="right">{{ btN(h.total_float_d) }}</td>
              <td class="nowrap">{{ h.early_start || '—' }}</td><td class="nowrap">{{ h.early_finish || '—' }}</td><td class="nowrap">{{ h.act_start || '—' }}</td><td class="nowrap">{{ h.act_finish || '—' }}</td></tr></tbody></table></div>
          <div class="tiny muted mt" v-else>没有这个作业的各期原始数据。</div></td></tr></template></tbody></table></div>
    <div class="empty small" v-else>{{ kind === 'FP' ? '没有误报。' : kind === 'FN' ? '没有漏报。' : '没有误报，也没有漏报。' }}</div>
    <div class="row mt" v-if="list.length > shown.length"><a class="small" @click="limit += STEP">再显示 {{ Math.min(STEP, list.length - shown.length) }} 条（共 {{ list.length }} 条）</a></div>
    <details class="mt bt-gaps" v-if="gaps.length"><summary class="small">数据缺口 {{ gaps.length }} 个（完成%未知或没有进度记录，不进入混淆矩阵）</summary>
      <div class="scroll-x"><table class="t mt"><thead><tr><th>作业</th><th>判定期</th><th>原因</th></tr></thead>
        <tbody><tr v-for="g in gaps.slice(0, glimit)" :key="g.code + g.judge_date"><td><b><act-name :c="g.code" :n="g.name" :s="g.short_name"></act-name></b></td><td class="nowrap small">{{ g.judge_date }}</td>
          <td class="small">{{ g.reason }}<template v-if="g.idle_days !== null && g.idle_days !== undefined">（{{ g.idle_days }} 天无记录）</template></td></tr></tbody></table></div>
      <a class="small" v-if="gaps.length > glimit" @click="glimit += STEP">再显示 {{ Math.min(STEP, gaps.length - glimit) }} 个</a></details></div>`,
  data: () => ({ kind: "", limit: BT_STEP, glimit: BT_STEP, open: {}, STEP: BT_STEP, OUT: BT_OUTCOME, RULE: BT_RULE, PT: BT_PCT_TYPE, LEVEL }),
  computed: {
    tabs: () => [["", "全部"], ["FP", "误报"], ["FN", "漏报"]],
    odd() { return (this.v.rows || []).filter((r) => r.outcome === "FP" || r.outcome === "FN"); },
    list() { return this.kind ? this.odd.filter((r) => r.outcome === this.kind) : this.odd; },
    shown() { return this.list.slice(0, this.limit); },
    gaps() { return this.v.gap_rows || []; },
  },
  methods: {
    btN,
    count(k) { return k ? this.odd.filter((r) => r.outcome === k).length : this.odd.length; },
    setKind(k) { this.kind = k; this.limit = BT_STEP; },
    key: (r) => `${r.code}|${r.judge_date}`,
    flip(r) { const k = this.key(r); this.open = { ...this.open, [k]: !this.open[k] }; },
    hist(r) { return (this.v.history || {})[r.code] || []; },
    pctOf: (x) => (x === null || x === undefined ? "—" : `${btN(x)}%`),
  },
};

// ---------------------------------------------------------------------------- 3×3 阈值敏感性表与“采用阈值”（只给工程主管，留审计）
const BtSens = {
  props: ["v", "canAdopt", "liveRed"],
  emits: ["adopted"],
  template: `<div class="zb-card bt-sens-card"><div class="row between wrap"><h2>阈值敏感性</h2>
      <span class="tiny muted" v-if="canAdopt">{{ isReal ? '点一格查看这组阈值，确认后可采用' : '测试数据不能用于调整线上预警，只能查看' }}</span></div>
    <div class="tiny muted mb"><help-tip caption="阈值与容差的含义" text="红色阈值：关键作业、或滞后超过总浮时的作业，滞后达到这个天数判红；容差：判断“确实延误”时允许多推迟的天数。"></help-tip></div>
    <div class="scroll-x"><table class="t bt-sens"><thead><tr><th>红色阈值 \\ 容差</th><th v-for="t in tols" :key="t" class="right">容差 {{ t }} 天</th></tr></thead>
      <tbody><tr v-for="r in reds" :key="r"><th class="nowrap">{{ r }} 天</th>
        <td v-for="t in tols" :key="t" :class="cls(r, t)" :tabindex="canAdopt ? 0 : null" :role="canAdopt ? 'button' : null" @click="pick(r, t)" @keydown.enter="pick(r, t)">
          <template v-if="cell(r, t)"><div class="bt-cv">命中 {{ btRateText(cell(r, t).precision, '无预警样本') }}</div>
            <div class="small">召回 {{ btRateText(cell(r, t).recall) }}</div><div class="tiny muted">样本 {{ cell(r, t).samples }} · 缺口 {{ cell(r, t).data_gaps }}</div>
            <div class="bt-tags"><span class="chip" v-if="cell(r, t).current">本次所用</span><span class="chip blue" v-if="isAdopted(r, t)">已采用</span></div></template>
          <span v-else class="muted">—</span></td></tr></tbody></table></div>
    <div class="small mt" v-if="v.adopted">已采用：红色阈值 {{ btN(v.adopted.red_slip_days) }} 天、容差 {{ btN(v.adopted.tolerance_days) }} 天<template v-if="v.adopted.previous_red_slip_days !== undefined">（原红色阈值 {{ btN(v.adopted.previous_red_slip_days) }} 天）</template> · {{ v.adopted.by || '' }} {{ btMMDD(v.adopted.at) }}</div>
    <div class="bt-adopt mt" v-if="canAdopt && sel && cell(sel[0], sel[1])">
      <div><b>采用：红色阈值 {{ sel[0] }} 天、容差 {{ sel[1] }} 天</b>（命中 {{ btRateText(cell(sel[0], sel[1]).precision, '无预警样本') }}，召回 {{ btRateText(cell(sel[0], sel[1]).recall) }}）</div>
      <div class="small mt" v-if="isReal">红色阈值写入项目设置<template v-if="liveRed !== null && liveRed !== undefined">（现行 {{ btN(liveRed) }} 天）</template>，之后的线上预警按新阈值判定；容差只记入本次回测，不写入项目设置。操作留审计。</div>
      <div class="small mt bt-warn" v-if="!isReal">测试数据不能用于调整线上预警。要调整阈值，请用真实项目数据的回测。</div>
      <div class="row wrap mt"><button class="btn primary" :disabled="busy || same || !isReal" @click="adopt">{{ busy ? '采用中…' : same ? '已是现行阈值' : '确认采用' }}</button><button class="btn" :disabled="busy" @click="sel = null">取消</button></div></div></div>`,
  data: () => ({ sel: null, busy: false }),
  computed: {
    cells() { return this.v.sensitivity || []; },
    reds() { return [...new Set(this.cells.map((c) => c.red_slip_days))].sort((a, b) => a - b); },
    tols() { return [...new Set(this.cells.map((c) => c.tolerance_days))].sort((a, b) => a - b); },
    isReal() { return this.v.source === "real"; },   // leader 决定（10-08）：测试来源不能采用阈值，接口也返回 409
    same() { const a = this.v.adopted; return !!(this.sel && a && a.red_slip_days === this.sel[0] && a.tolerance_days === this.sel[1] && this.liveRed === this.sel[0]); },
  },
  methods: {
    btRateText, btN, btMMDD,
    cell(r, t) { return this.cells.find((c) => c.red_slip_days === r && c.tolerance_days === t) || null; },
    isAdopted(r, t) { const a = this.v.adopted; return !!(a && a.red_slip_days === r && a.tolerance_days === t); },
    cls(r, t) { return { "bt-cell": true, pick: this.canAdopt, sel: !!(this.sel && this.sel[0] === r && this.sel[1] === t) }; },
    pick(r, t) { if (this.canAdopt && this.cell(r, t)) this.sel = [r, t]; },
    async adopt() {
      if (!this.sel || !this.isReal) return;
      this.busy = true;
      try {
        const v = await POST(`/api/backtest/${this.v.id}/adopt`, { red_slip_days: this.sel[0], tolerance_days: this.sel[1] });
        toast(`已采用：红色阈值 ${this.sel[0]} 天`); this.sel = null; this.$emit("adopted", v);
      } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

// ---------------------------------------------------------------------------- 页面
const BacktestPage = {
  components: { BtSummary, BtPeriods, BtOdd, BtSens },
  template: `<div class="bt">
    <page-head page="backtest">
      <template #badge><template v-if="v"><span :class="['chip', v.source === 'real' ? 'blue' : 'orange']">{{ v.source_name }}</span><span class="chip orange" v-if="v.summary && v.summary.weak">弱口径</span></template></template>
      <template #meta><template v-if="v">{{ btMeta(v) }}<span v-if="v.created_by"> · {{ v.created_by }} 运行</span></template></template>
      <template #actions><div class="seg" v-if="!err"><button v-for="s in SOURCES" :key="s[0]" :class="{on: src === s[0]}" @click="switchSrc(s[0])">{{ s[1] }}</button></div>
        <a class="btn" v-if="v" :href="'/api/backtest/' + v.id + '/docx'">导出 Word</a>
        <button class="btn primary" v-if="canRun && !err" @click="openPicker()">上传多期 XER</button></template></page-head>
    <input v-if="canRun" ref="file" type="file" accept=".xer" multiple hidden @change="onPick">
    <div class="banner warn" v-if="v && v.source !== 'real'">测试数据：这次回测用的是仿真/测试数据，只用于核对页面和口径，不计入应用成效，也不能对外表述为预警效果。</div>
    <div class="zb-card bt-up" v-if="canRun && panel">
      <div class="row between wrap"><h2>上传多期 P6 更新 XER</h2><a class="small" v-if="!busy" @click="closePanel">收起</a></div>
      <div class="tiny muted">同一项目、同一套作业编码的连续多期更新：至少 2 期，建议 3 期以上（最好 6 期）。只在本机计算，不调用 AI，原文件不保存，不读费用字段。</div>
      <div class="row wrap mt"><button class="btn" :disabled="busy" @click="openPicker()">{{ files.length ? '重新选择文件' : '选择 .xer 文件' }}</button><span class="small">{{ btFilesHint(files.length) }}</span></div>
      <ul class="bt-files" v-if="files.length"><li v-for="f in files" :key="f.name + f.size"><span>{{ f.name }}</span><span class="tiny muted">{{ btSize(f.size) }}</span></li></ul>
      <div class="small bt-warn mt" v-if="bad.length">以下文件不是 .xer，已忽略：{{ bad.join('、') }}</div>
      <div class="bt-src mt" role="radiogroup" aria-label="数据来源"><span class="small">数据来源（必选）</span>
        <label v-for="s in SOURCES" :key="s[0]" :class="['bt-opt', {on: form.source === s[0]}]"><input type="radio" name="bt-src" :value="s[0]" v-model="form.source" :disabled="busy">{{ s[1] }}</label></div>
      <div class="row wrap mt bt-fields">
        <label class="small">标签（选填，会显示在报告上）<input class="in" v-model="form.label" maxlength="40" placeholder="如 SEC1 更新01-06" :disabled="busy"></label>
        <label class="small">回测容差<select class="in" v-model.number="form.tol" :disabled="busy"><option v-for="t in tolOpts" :key="t" :value="t">{{ t }} 天</option></select></label></div>
      <div class="small mt" v-if="form.exclude.length">将剔除的期次：<span class="chip" v-for="d in form.exclude" :key="d">{{ d }} <a @click="form.exclude = form.exclude.filter((x) => x !== d)" aria-label="取消剔除">×</a></span></div>
      <div class="row wrap mt"><button class="btn primary" :disabled="!can.ok || busy" @click="run">{{ busy ? '回测中…' : '开始回测' }}</button>
        <span class="small bt-warn" v-if="runErr">{{ runErr }}</span><span class="small muted" v-else-if="!can.ok">{{ can.why }}</span></div></div>
    <load-state v-if="loading" loading></load-state>
    <empty-state v-else-if="err" :reason="err"></empty-state>
    <empty-state v-else-if="!v && !panel" :title="emptyTitle" :reason="emptyReason" :action="emptyAction" :alt="emptyAlt"></empty-state>
    <template v-else-if="v">
      <!-- 一屏化（验收 I15）：四块结果分页签逐块看，不再上下堆成近 3 屏 -->
      <div class="seg bt-tabs mb"><button v-for="s in SECS" :key="s[0]" :class="{on: sec === s[0]}" @click="sec = s[0]">{{ s[1] }}</button></div>
      <div class="bt-sec" v-fit="200">
      <template v-if="sec === 'sum'"><bt-summary :v="v"></bt-summary>
        <div class="small mt"><a @click="defsOpen = true">口径说明（{{ Object.keys(v.summary.definitions || {}).length }} 条）</a></div></template>
      <bt-periods v-else-if="sec === 'per'" :v="v" :can-edit="canRun" v-model:excl="excl" @rerun="rerun"></bt-periods>
      <bt-odd v-else-if="sec === 'odd'" :v="v" :key="'odd' + v.id"></bt-odd>
      <bt-sens v-else :v="v" :can-adopt="canAdopt" :live-red="liveRed" :key="'sens' + v.id" @adopted="onAdopted"></bt-sens></div>
      <zb-drawer v-if="defsOpen" title="口径说明" @close="defsOpen = false"><div class="small mb" v-for="(d, k) in v.summary.definitions" :key="k"><b>{{ k }}</b>：{{ d }}</div></zb-drawer>
    </template>
    <ai-note page="backtest"></ai-note></div>`,
  data: () => ({ v: null, src: "real", known: {}, loading: true, err: "", panel: false, files: [], bad: [], busy: false, runErr: "",
    form: { source: "", label: "", tol: 0, exclude: [] }, excl: [], last: null, liveRed: null, SOURCES: BT_SOURCES,
    sec: "sum", defsOpen: false, SECS: [["sum", "汇总"], ["per", "期次与混淆矩阵"], ["odd", "误报与漏报"], ["sens", "阈值敏感性"]] }),
  computed: {
    role: () => roleOf(),
    canRun() { return this.role === "eng_sup"; },          // 管理员只读（G-13）；接口另有本地校验
    canAdopt() { return this.role === "eng_sup"; },
    can() { return btCanRun({ files: this.files, source: this.form.source }); },
    tolOpts() { return [...new Set([...BT_TOLS, +this.form.tol || 0])].sort((a, b) => a - b); },
    srcName() { return (BT_SOURCES.find((s) => s[0] === this.src) || [])[1] || ""; },
    emptyTitle() { return this.known.real === false && this.known.test === false ? "还没有回测" : `还没有${this.srcName}的回测`; },
    emptyReason() {
      if (!this.canRun) return "回测由工程主管上传多期 P6 更新 XER 后运行，这里只能查看结果。";
      return "回测要用同一项目连续多期的 P6 更新 XER：至少 2 期，建议 3 期以上。只在本机计算，不调用 AI，原文件不保存。";
    },
    emptyAction() { return this.canRun ? { label: "选择 .xer 文件", fn: () => this.openPicker() } : null; },
    emptyAlt() {
      const other = this.src === "real" ? "test" : "real";
      return this.known[other] ? { label: `看${BT_SOURCES.find((s) => s[0] === other)[1]}的回测`, fn: () => this.switchSrc(other) } : null;
    },
  },
  mounted() { this.load(); },
  methods: {
    btMeta, btFilesHint, btSize,
    setView(v) { this.v = v ? Vue.markRaw(v) : null; this.excl = v ? [...((v.summary || {}).excluded_periods || [])] : []; },
    async load() {
      this.loading = true; this.err = "";
      try {
        const id = (store.route.params || {}).id;
        if (id) {
          const v = await GET(`/api/backtest/${encodeURIComponent(id)}`);
          this.setView(v); this.src = v.source; this.known[v.source] = true;
        } else {
          let v = await GET("/api/backtest/latest?source=real"), src = "real";
          this.known.real = !!v;
          if (!v) { v = await GET("/api/backtest/latest?source=test"); this.known.test = !!v; if (v) src = "test"; }
          this.setView(v); this.src = src;
        }
      } catch (e) { this.err = e.status === 403 ? "历史回测只对工程主管开放，不在你的查看范围。" : e.status === 404 ? "没有找到这次回测记录。" : e.message; }
      finally { this.loading = false; }
      if (this.canAdopt && !this.err) await this.loadThresholds();
    },
    async loadThresholds() { try { this.liveRed = (await GET("/api/project/thresholds")).values.red_slip_days; } catch (_) { this.liveRed = null; } },
    async switchSrc(s) {
      if (this.busy) return;
      this.src = s; this.loading = true; this.err = "";
      try { const v = await GET(`/api/backtest/latest?source=${s}`); this.known[s] = !!v; this.setView(v); }
      catch (e) { this.err = e.message; } finally { this.loading = false; }
    },
    // 空状态、页头按钮、重跑都直接弹出文件选择，不先跳到别处（U-05①）
    openPicker(preset) {
      if (!this.canRun) return;
      if (preset) { this.form = { ...this.form, ...preset }; this.panel = true; }   // 重跑：先把预填的来源、剔除期次亮出来；其余等选好文件再展开
      this.runErr = "";
      const el = this.$refs.file;
      if (el) el.click();
    },
    onPick(ev) {
      const { ok, bad } = btSplitFiles([...(ev.target.files || [])]);
      ev.target.value = "";
      if (!ok.length && !bad.length) return;
      this.files = ok; this.bad = bad.map((f) => f.name); this.panel = true; this.runErr = "";
    },
    closePanel() { this.panel = false; this.files = []; this.bad = []; this.runErr = ""; this.form = { source: "", label: "", tol: 0, exclude: [] }; },
    async run(files) {
      const list = Array.isArray(files) ? files : this.files;
      const c = btCanRun({ files: list, source: this.form.source });
      if (!c.ok) { this.runErr = c.why; return; }
      this.busy = true; this.runErr = "";
      try {
        const fd = new FormData();
        list.forEach((f) => fd.append("files", f, f.name));
        fd.append("source", this.form.source); fd.append("label", (this.form.label || "").trim()); fd.append("tolerance_days", String(+this.form.tol || 0));
        if (this.form.exclude.length) fd.append("exclude_periods", JSON.stringify(this.form.exclude));
        const v = await POST("/api/backtest/run", fd);
        this.last = { id: v.id, files: list, form: { ...this.form, exclude: [] } };
        this.files = []; this.bad = []; this.panel = false; this.form = { source: "", label: "", tol: 0, exclude: [] };
        this.setView(v); this.src = v.source; this.known[v.source] = true;
        if (store.route.params && store.route.params.id) history.replaceState(null, "", "#/backtest");
        const s = v.summary || {};
        toast(`回测完成：${s.periods} 期，样本 ${s.samples}，数据缺口 ${s.data_gaps} 个不计入`);
      } catch (e) { this.runErr = e.message; toast(e.message, true); }
      finally { this.busy = false; }
    },
    // 剔除期次后重跑：本次会话刚跑过这一批就直接重跑；否则预填来源/标签/容差/剔除，请人重新选择同一批文件
    rerun(excl) {
      const v = this.v;
      if (!v) return;
      const preset = { source: v.source, label: v.label || "", tol: +((v.params || {}).tolerance_days || 0), exclude: excl };
      if (this.last && this.last.id === v.id && this.last.files.length) { this.form = { ...preset }; this.run(this.last.files); return; }
      this.openPicker(preset);
    },
    onAdopted(v) { this.setView(v); this.loadThresholds(); },
  },
};

EXT.route("backtest", BacktestPage);
EXT.nav(["eng_sup"], { k: "backtest", route: "backtest", label: "历史回测", short: "", icon: "target", line: "ai", after: "metrics" });   // 回测是应用成效的证据，紧挨成效页（导航编排 10-08）
