/* 项目经理 · 智能管理台（RJ 10-10 采用 09_V8 设计稿；同日收敛修订：1440×900 一屏、1366×768 优先关键数/简报/待办/三领域）。
   按“态势 → 简报 | 待我处理 → 进度/质量/安全 → 文书 | 现场”排，每块只放有限摘要，完整内容在详情页。
   - 只用既有接口（/api/todo、/api/cockpit、/api/alerts、/api/reports、/api/gongbi/types、/api/feed），不新增统计口径，不硬编码设计稿里的示例数字；
   - 简报标签按真实来源：AI 生成并通过核对才写“AI按当前事实整理”，系统模板、事实变化后系统重拼、没有简报都如实标出；
   - 待我处理按部门长、分包领导层面的协调事项组织：升级到经理的高风险问题按责任单位归成一行“待协调”（只是归类展示，不生成任务、不派单），
     展开才见逐条依据；赶工决定等仍是原待办行（TodoItem 的同一套就地操作）。“建议对接”按岗位分工给出，不是已任命的责任人；
   - 累计产值卡只在产值接口对本岗位开放时出现，拿不到就不显示，不补零；
   - 现场照片 4:3 完整显示（contain），点开看原图；文件缺失如实显示；
   - 手机：标题、指标网格、简报、经理事项在前；进度/质量/安全默认收起，点标题展开。
   - 一屏化（RJ 10-10）：第四行收成一行入口——工笔文书只放最近一份报表与“起草文书”（全部文书类型在抽屉里分类列出），
     现场动态只放两张 4:3 小图与“进入现场圈”；口径说明（AI 只整理线索、产值非计量认证）收进“待我处理”标题旁的小问号；
     简报每方面至多两行、待办说明一行，全文在悬停提示与依据页。 */
const PM_BRIEF_LABEL = { ai: "AI按当前事实整理", template: "AI 文字未通过核对，改用系统模板", system: "系统按当前事实生成" };
const PM_CONTACT = { progress: "工程主管", quality: "工程主管", safety: "安全主管", report: "工程主管", site: "工程主管" };
const PM_TODO_SHOW = 3;
const pmWan = (v) => (v == null ? "—" : (Math.round(v / 1000) / 10).toLocaleString("zh-CN"));   // 原值 → “万”，一位小数

// 经理待办的一行：沿用 TodoItem 的数据与就地操作（主按钮、快捷决定、确认提示），只换成“事项｜建议对接｜下一步”三栏、去掉来历与长说明
const PmTodoRow = {
  extends: TodoItem,
  template: `<tr :class="['pm-tr', it.overdue ? 'over' : '']"><td class="pm-tt"><b :title="it.title">{{ it.title }}</b>
      <small :title="it.reason"><span :class="['chip', tone]">{{ it.status }}</span>{{ short }}</small></td>
    <td class="pm-to"><span :title="contact">{{ contact }}</span></td>
    <td class="pm-ta"><div class="zb-btns"><button class="btn primary sm" v-if="primary" :disabled="busy" @click="main">{{ busy ? '处理中' : it.action_label }}</button>
      <button v-for="(q, i) in it.quick" :key="i" :class="['btn', 'sm', q.primary && !primary ? 'primary' : '']" :disabled="busy" @click="runQuick(q)">{{ q.label }}</button>
      <a v-for="x in links" :key="x.route" @click="todoGo(x.route)">{{ x.label }}</a><a v-if="it.action_route && !primary" @click="todoGo(it.action_route)">详情</a></div>
      <div class="todo-note" v-if="note" role="status">{{ note }}</div></td></tr>`,
  computed: {
    contact() { return PM_CONTACT[this.it.module] || "—"; },
    // 说明只留标题里没有的信息：去掉括号里的机制说明，丢掉数字都已在标题里出现的分句；完整说明在悬停提示
    short() {
      const nums = (t) => t.match(/\d+/g) || [];
      const segs = this.it.reason.replace(/（[^）]*）|\([^)]*\)/g, "").split("；").map((x) => x.trim()).filter(Boolean);
      const keep = segs.filter((x) => { const n = nums(x); return !(n.length && n.filter((v) => this.it.title.includes(v)).length * 2 >= n.length); });
      return (keep.length ? keep : segs.slice(-1)).join("；");
    },
  },
};

// 升级到经理的高风险问题，按责任单位归成一行：催办链确已提请项目经理的是“待协调”（有诉求），只是超期、未走到提请的是“需关注”。
// 展开看逐条依据（每条进详情页，催办、改派等仍在详情页办）；只是归类展示，系统不据此派单、不指定责任人
const PmCoordRow = {
  props: { g: Object },
  template: `<tr :class="['pm-tr', g.requested ? 'over' : '']"><td class="pm-tt"><b :title="title">{{ title }}</b><small :title="reason"><span :class="['chip', g.requested ? 'red' : 'orange']">{{ g.requested ? '待协调' : '需关注' }}</span>{{ reason }}</small></td>
      <td class="pm-to"><span :title="contact">{{ contact }}</span></td>
      <td class="pm-ta"><div class="zb-btns"><button :class="['btn', 'sm', g.requested ? 'primary' : '']" @click="open = !open">{{ open ? '收起依据' : '查看依据' }}</button></div></td></tr>
    <tr class="pm-tx" v-if="open"><td colspan="3"><div class="pm-tx-i" v-for="it in g.items" :key="it.id"><span :class="['chip', it.overdue ? 'red' : 'orange']">{{ it.status }}</span>
      <span class="pm-tx-t" :title="it.reason">{{ it.title }}</span><a @click="todoGo(it.action_route)">详情</a></div>
      <div class="tiny muted">按责任人所属单位归类；系统未据此派发任务或指定责任人。</div></td></tr>`,
  data: () => ({ open: false }),
  computed: {
    unit() { return (this.g.company || "").replace(/[（(].*?[）)]/g, ""); },   // 简称：“甲分包单位（路基桥梁）”→“甲分包单位”
    kind() {
      const s = this.g.items.filter((i) => i.module === "safety").length, q = this.g.items.length - s;
      return `高风险${s ? "隐患 " + s + " 项" : ""}${s && q ? "、" : ""}${q ? "质量问题 " + q + " 项" : ""}`;
    },
    title() { return `${this.unit || "未登记单位"}：${this.kind}${this.g.requested ? "已提请经理" : "超期未闭环"}`; },
    reason() {
      const late = this.g.items.filter((i) => i.overdue), max = Math.max(0, ...late.map((i) => +((i.status.match(/(\d+)/) || [])[1] || 0)));
      const t = late.length ? `最长超期 ${max} 天` : "催办链已提请";
      return this.g.requested ? `${t}；需协调该单位领导层督办` : `${t}；安全主管督办`;
    },
    contact() { return this.g.requested ? `安全主管、${this.unit || "该单位"}负责人` : "安全主管"; },
  },
  methods: { todoGo },
};

const PmHome = {
  components: { PmTodoRow, PmCoordRow },
  template: `<div class="pm-home">
    <section class="zb-hero"><div class="zb-hero-tx"><h1>{{ projName }}</h1><div class="zb-hero-sub">项目经理 · 智能管理台</div></div></section>
    <load-state compact v-if="err && !d" :error="err" @retry="load"></load-state>
    <load-state compact v-else-if="!d" :loading="true"></load-state>
    <template v-if="d">
    <div :class="['zb-kpis', 'n' + kpis.length]">
      <button v-for="k in kpis" :key="k.k" :class="['zb-kpi', k.tone]" @click="todoGo(k.route)"><span class="zb-kpi-ic"><ic :n="k.icon"></ic></span>
        <span class="zb-kpi-b"><span class="zb-kpi-l">{{ k.label }}</span><b class="zb-kpi-v">{{ k.value }}<small v-if="k.unit">{{ k.unit }}</small></b><span class="zb-kpi-s" :title="k.title || k.sub">{{ k.sub }}</span></span><ic n="chevron" class="zb-kpi-go"></ic></button></div>

    <div class="zb-row pm-r2">
      <section class="zb-card pm-brief"><div class="zb-ch"><ic n="file"></ic><h2>管理简报</h2><span class="grow"></span>
          <span :class="['zb-src', brief && brief.source === 'ai' ? 'ai' : '']">{{ briefLabel }}<help-tip v-if="briefNote" :text="briefNote"></help-tip></span>
          <button class="zb-link" :disabled="busy" @click="refresh"><ic n="refresh"></ic>{{ busy ? '生成中' : (brief ? '刷新' : '生成') }}</button></div>
        <div class="pm-bl" v-for="a in briefRows" :key="a.area"><span :class="['pm-bl-ic', a.cls]"><ic :n="a.icon"></ic></span><b>{{ a.area }}：</b>
          <span class="pm-bl-t" :title="a.full"><em v-if="a.sys" class="zb-sys">{{ a.sys }}</em>{{ a.text }}</span><a class="pm-bl-src" v-if="!narrow" @click="todoGo(a.route)">{{ a.src }}</a></div>
        <div class="pm-basis-m" v-if="narrow"><button v-for="a in briefRows" :key="a.area" class="zb-chip" @click="todoGo(a.route)">{{ a.src }}</button></div></section>

      <section class="zb-card pm-todo"><div class="zb-ch"><ic n="clipboard"></ic><h2>待我处理</h2><help-tip :text="d.boundary + (out ? '产值为内部产值统计（非计量认证）。' : '')"></help-tip><span class="zb-n" v-if="entries.length">{{ entries.length }}</span><span class="pm-tally tiny muted" v-if="tally">{{ tally }}</span><span class="grow"></span>
          <a class="zb-link" v-if="know.n" @click="todoGo(know.parts[0].route)" :title="know.parts.map((p) => p.label + ' ' + p.n).join('；')">需要知道 {{ know.n }} 项</a></div>
        <table class="pm-todo-t" v-if="entries.length"><thead><tr><th>事项</th><th>建议对接</th><th>下一步</th></tr></thead>
          <tbody><template v-for="e in shownEntries" :key="e.key"><pm-coord-row v-if="e.group" :g="e"></pm-coord-row><pm-todo-row v-else :it="e.it" @changed="load"></pm-todo-row></template></tbody></table>
        <div class="pm-more" v-if="entries.length > PM_TODO_SHOW"><button class="zb-link" @click="allTodo = !allTodo">{{ allTodo ? '收起' : '另有 ' + (entries.length - PM_TODO_SHOW) + ' 项，展开' }}</button></div>
        <div class="zb-empty" v-if="!entries.length">{{ d.empty && d.empty.title ? d.empty.title : '现在没有需要你协调或拍板的事项' }}</div></section>
    </div>

    <div class="zb-row pm-r3">
      <section :class="['zb-card', 'pm-prog', {shut: shut('prog')}]"><div class="zb-ch" @click="tog('prog')"><ic n="flag"></ic><h2>进度与关键节点</h2><span class="grow"></span><span class="tiny muted" v-if="p6">P6 数据日期 {{ p6.data_date }}</span></div>
        <div class="zb-body"><ol class="pm-ms" v-if="ms.length"><li v-for="m in ms" :key="m.code"><i></i><div><div class="pm-ms-h"><b>{{ m.short_name || m.name }}</b><span>计划 {{ m.p6_date || '—' }}</span></div>
          <div class="pm-ms-f">模拟 P50 {{ m.p50 || '—' }} · 按期概率估计 {{ m.prob_label || '未知' }}</div></div></li></ol>
        <div class="zb-empty" v-else>还没有里程碑预测，导入 P6 计划并运行预测后显示。</div>
        <div class="zb-foot"><a @click="todoGo('progress?tab=alerts')">已确认进度预警 <b>{{ alertN.all }}</b> 项</a><a @click="todoGo('progress?tab=forecast')">预测与赶工 <ic n="chevron"></ic></a></div></div></section>

      <section :class="['zb-card', 'pm-qual', {shut: shut('qual')}]"><div class="zb-ch" @click="tog('qual')"><ic n="shield"></ic><h2>质量态势</h2><span class="grow"></span><span class="tiny muted">未闭环 {{ q.open || 0 }} 项</span></div>
        <div class="zb-body"><div class="pm-q-zero" v-if="!q.open"><span class="pm-q-ok"><ic n="check-circle"></ic></span><div><b>当前台账暂无未闭环问题</b><div class="tiny muted">验收与整改详情在质量台账查询</div></div></div>
        <div class="pm-q-nums" v-else><div><b>{{ q.open }}</b><span>未闭环</span></div><div :class="{red: q.overdue}"><b>{{ q.overdue || 0 }}</b><span>超期</span></div><div><b>{{ q.reminders || 0 }}</b><span>待执行质控提醒</span></div></div>
        <div class="zb-foot c"><a @click="todoGo('quality')">查看质量台账 <ic n="chevron"></ic></a></div></div></section>

      <section :class="['zb-card', 'pm-safe', {shut: shut('safe')}]"><div class="zb-ch" @click="tog('safe')"><ic n="chart"></ic><h2>安全变化 · 近 7 天</h2><span class="grow"></span><span class="tiny muted">新增 {{ week.n }} · 其中已闭环 {{ week.r }}</span></div>
        <div class="zb-body"><svg class="zb-bars" viewBox="0 0 420 80" role="img" :aria-label="'近 7 天按发现日新增已确认隐患 ' + week.n + ' 项，其中现已闭环 ' + week.r + ' 项'">
          <g v-for="(t, i) in ticks" :key="'t' + i"><line x1="22" x2="416" :y1="t.y" :y2="t.y" class="zb-grid"></line><text x="16" :y="t.y + 4" class="zb-ax" text-anchor="end">{{ t.v }}</text></g>
          <g v-for="b in bars" :key="b.day">
            <rect :x="b.x" :y="b.y1" width="14" :height="b.h1" class="zb-b1"></rect><rect :x="b.x + 16" :y="b.y2" width="14" :height="b.h2" class="zb-b2"></rect>
            <text :x="b.x + 7" :y="b.y1 - 3" class="zb-bv" text-anchor="middle" v-if="b.n">{{ b.n }}</text><text :x="b.x + 23" :y="b.y2 - 3" class="zb-bv" text-anchor="middle" v-if="b.r">{{ b.r }}</text>
            <text :x="b.x + 15" y="79" class="zb-ax" text-anchor="middle">{{ b.day.slice(5) }}</text></g></svg>
        <div class="zb-foot"><span class="zb-legend"><i class="b1"></i>新增<i class="b2"></i>其中现已闭环</span><span class="tiny muted">按发现日 · 安全台账已确认隐患</span></div></div></section>
    </div>

    <div class="zb-row pm-r4">
      <section class="zb-card pm-docs"><div class="zb-ch"><ic n="file"></ic><h2>工笔文书</h2>
          <span class="pm-r4-rp" v-if="lastRpt"><span class="pm-r4-m" :title="'最近报表：' + lastRpt.period_start + '～' + lastRpt.period_end">{{ lastRpt.kind === 'week' ? '周报' : '月报' }} {{ lastRpt.period_start.slice(5) }}～{{ lastRpt.period_end.slice(5) }}</span>
            <span :class="['chip', lastRpt.status === 'published' ? 'green' : '']">{{ lastRpt.status === 'published' ? '已发布' : '草稿' }}</span><a @click.stop="todoGo('report?id=' + lastRpt.id)">打开</a></span>
          <span class="grow"></span><button class="zb-link" type="button" v-if="docGroups.length" @click="docsOpen = true">起草文书 · {{ types.length }} 类 <ic n="chevron"></ic></button></div></section>

      <section class="zb-card pm-site"><div class="zb-ch"><ic n="camera"></ic><h2>现场动态</h2>
          <span class="pm-r4-shots" v-if="shots.length"><span class="pm-r4-shot" v-for="p in shots" :key="p.key"><button type="button" class="zb-ph" @click="store.viewer = p.photo.url" :title="'查看原图：' + (p.place || p.text || '')">
            <img v-if="!p.broken" :src="p.photo.thumb" alt="" loading="lazy" @error="p.broken = true"><span v-else class="zb-ph-miss">文件不可用</span></button>
            <span class="pm-r4-cap" :title="(p.place || p.text || p.tag) + ' · ' + p.author + ' · ' + p.when"><b>{{ p.place || p.text || p.tag }}</b><small>{{ p.author }} · {{ p.when }}</small></span></span></span>
          <span class="pm-r4-m" v-else-if="shotsLoaded">还没有带照片的现场记录</span>
          <span class="grow"></span><a class="zb-link" @click.stop="todoGo('circle')">进入现场圈 <ic n="chevron"></ic></a></div></section>
    </div>
    <zb-drawer v-if="docsOpen" title="起草文书" @close="docsOpen = false"><div class="pm-dw-g" v-for="g in docGroups" :key="g.name"><b>{{ g.name }}</b>
      <div class="zb-btns"><button v-for="t in g.items" :key="t.kind" type="button" class="zb-chip" @click="docsOpen = false; openDoc(t)">{{ t.short }}</button></div></div></zb-drawer>
    </template></div>`,
  data: () => ({ narrow: window.matchMedia("(max-width: 640px)").matches, opened: {}, allTodo: false, docsOpen: false, PM_TODO_SHOW,
    d: null, cp: null, alerts: [], out: null, rpts: [], types: [], shots: [], shotsLoaded: false, err: null, busy: false, store }),
  computed: {
    projName() { return store.me && store.me.project ? store.me.project.name : ""; },
    k() { return (this.cp && this.cp.kpis) || {}; },
    q() { return this.k.quality || {}; },
    s() { return this.k.safety || {}; },
    p6() { return this.k.p6 || (this.d && this.d.p6) || null; },
    // 进度风险：已确认（未关闭）的进度预警，接口已剔除“数据缺口”旧预警；主数字是红色，橙黄附后
    alertN() { const n = { red: 0, orange: 0, yellow: 0, all: 0 }; for (const a of this.alerts) { n[a.level] = (n[a.level] || 0) + 1; n.all++; } return n; },
    kpis() {
      const a = this.alertN, out = [
        { k: "prog", label: "进度风险", icon: "alert", value: a.red, tone: a.red ? "red" : "", route: "progress?tab=alerts",
          sub: "已确认红色预警" + (a.orange || a.yellow ? ` · 另橙 ${a.orange} · 黄 ${a.yellow}` : "") },
        { k: "qual", label: "质量未闭环", icon: "shield", value: this.q.open || 0, tone: this.q.overdue ? "red" : "green", route: "quality",
          sub: "当前台账" + (this.q.overdue ? ` · 超期 ${this.q.overdue}` : "") },
        { k: "safe", label: "安全未闭环", icon: "helmet", value: this.s.open || 0, tone: this.s.overdue ? "orange" : "", route: "safety",
          sub: `高风险 ${this.s.high || 0} · 超期 ${this.s.overdue || 0}` },
      ];
      const r = this.out && this.out.report;
      if (r && r.cumulative && r.cumulative.actual != null) {
        const cur = r.currency || this.out.currency || "";
        // 卡片只放一行副标题（上周完成值，没有时写来源），完整来源放悬停提示；币种未登记只写“万”，不加货币符号
        const lw = r.last_week && r.last_week.actual != null ? `上周 ${pmWan(r.last_week.actual)} 万${cur ? " " + cur : ""}` : "";
        out.push({ k: "out", label: "累计产值", icon: "chart", value: pmWan(r.cumulative.actual), unit: " 万" + (cur ? " " + cur : ""), tone: "", route: "output",
          sub: lw || r.cumulative.src || "周产值记录", title: [lw, r.cumulative.src].filter(Boolean).join(" · ") });
      }
      return out;
    },
    brief() { return (this.cp && this.cp.brief) || null; },
    briefLabel() { return this.brief ? PM_BRIEF_LABEL[this.brief.source] || "今日简报" : "今日简报尚未生成"; },
    briefNote() {
      if (!this.brief) return "点“生成”由简报Agent 按当前事实整理";
      return [this.brief.created_at ? "生成于 " + fmtDT(this.brief.created_at) : "", this.brief.source === "system" ? this.brief.note : ""].filter(Boolean).join("。");
    },
    // 进度、质量各一行：取简报里该方面排第一的事项，简报没写到的方面用系统统计如实补一句（标“统计”）；
    // 安全一行按经理层面表达（关注、督办、协调）：数字取安全台账，提请协调的单位取催办链已提请的升级项，引用简报头条隐患名；整句标“按台账”，不冒充 AI 结论
    briefRows() {
      const items = (this.brief && this.brief.items) || [];
      const out = [];
      for (const [area, icon, cls, src, route] of [["进度", "trend", "p", "P6 预警", "progress?tab=alerts"], ["质量", "shield", "q", "质量台账", "quality"]]) {
        const it = items.find((x) => x.area === area), r = { area, icon, cls, src, route };
        out.push(it ? { ...r, text: it.action ? `${it.title}，${it.action}` : `${it.title}：${it.detail}`, full: `${it.title}：${it.detail}${it.action ? "。建议：" + it.action : ""}` }
          : { ...r, text: this.factLine(area), full: "", sys: "统计" });
      }
      out.push({ area: "安全", icon: "helmet", cls: "s", src: "安全台账", route: "safety", ...this.safetyLine() });
      return out;
    },
    know() { return (this.d && this.d.know) || { n: 0, parts: [] }; },
    // 待办归类：升级到经理的高风险问题按责任单位、是否经催办链提请分组（待协调 / 需关注），其余待办原样；顺序按后端排好的紧急程度，组的位置取组内第一条
    entries() {
      const out = [], groups = {};
      for (const it of (this.d && this.d.items) || []) {
        if (it.source === "escalated") {
          const c = it.company || "", k = c + (it.requested ? "|asked" : "|watch");
          if (!groups[k]) out.push((groups[k] = { key: "g:" + k, group: true, company: c, requested: !!it.requested, items: [] }));
          groups[k].items.push(it);
        } else out.push({ key: it.id, it });
      }
      return out;
    },
    tally() {
      const g = this.entries.filter((e) => e.group), a = g.filter((e) => e.requested).length, w = g.length - a;
      return [a ? `待协调 ${a}` : "", w ? `需关注 ${w}` : ""].filter(Boolean).join(" · ");
    },
    shownEntries() { return this.allTodo ? this.entries : this.entries.slice(0, PM_TODO_SHOW); },
    ms() { return ((this.cp && this.cp.milestones) || []).slice(0, 2); },
    // 近 7 天（含今天）按发现日统计已确认安全隐患：当天新增数，及其中现在已闭环的数；某天没有记录即 0（台账计数，不是缺数）
    week() {
      const day = this.k.day || today(), by = {};
      for (const r of (this.cp && this.cp.safety_trend) || []) by[r.day] = r;
      const days = [];
      for (let i = 6; i >= 0; i--) { const t = new Date(day + "T00:00:00"); t.setDate(t.getDate() - i); const ds = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`; days.push({ day: ds, n: (by[ds] || {}).n || 0, r: (by[ds] || {}).resolved || 0 }); }
      return { days, n: days.reduce((a, x) => a + x.n, 0), r: days.reduce((a, x) => a + x.r, 0), max: Math.max(1, ...days.map((x) => x.n)) };
    },
    ticks() { const m = this.week.max, step = m <= 4 ? 1 : Math.ceil(m / 4); const out = []; for (let v = 0; v <= m; v += step) out.push({ v, y: 66 - (v / m) * 54 }); return out; },
    bars() { const m = this.week.max; return this.week.days.map((x, i) => { const h1 = (x.n / m) * 54, h2 = (x.r / m) * 54; return { ...x, x: 34 + i * 55, h1, h2, y1: 66 - h1, y2: 66 - h2 }; }); },
    docGroups() {
      const out = [];
      for (const t of this.types) {
        const name = t.legacy ? "报表" : t.group;
        let g = out.find((x) => x.name === name);
        if (!g) out.push((g = { name, items: [] }));
        g.items.push({ ...t, short: (typeof GB_SHORT !== "undefined" && GB_SHORT[t.kind]) || t.zh || t.name });
      }
      return out;
    },
    lastRpt() { return this.rpts[0] || null; },
  },
  mounted() {
    this.load(); this.loadSide();
    this.mq = window.matchMedia("(max-width: 640px)"); this.onMq = (e) => { this.narrow = e.matches; }; this.mq.addEventListener("change", this.onMq);
  },
  unmounted() { this.mq.removeEventListener("change", this.onMq); },
  methods: {
    todoGo, fmtDT,
    // 手机上辅助块默认收起，点标题展开；电脑端始终展开
    shut(k) { return this.narrow && !this.opened[k]; },
    tog(k) { if (this.narrow) this.opened = { ...this.opened, [k]: !this.opened[k] }; },
    safetyLine() {
      const s = this.s, it = ((this.brief && this.brief.items) || []).find((x) => x.area === "安全");
      if (!s.open) return { text: "当前台账无未闭环安全隐患", full: "", sys: "统计" };
      const g = this.entries.filter((e) => e.group && e.items.some((i) => i.module === "safety"));
      const short = (c) => (c || "未登记单位").replace(/[（(].*?[）)]/g, "");
      const asked = [...new Set(g.filter((e) => e.requested).map((e) => short(e.company)))], watch = [...new Set(g.filter((e) => !e.requested).map((e) => short(e.company)))];
      let t = `未闭环 ${s.open}（高风险 ${s.high || 0}、超期 ${s.overdue || 0}）`;
      if (asked.length) t += `；${asked.join("、")}已提请协调，督办其领导层落实整改`;
      else if (watch.length) t += `；督办${watch.join("、")}超期整改，必要时协调其领导层`;
      else t += "；由安全主管跟进整改";
      return { text: t, full: it ? `简报头条隐患：${it.title}（${it.detail}）` : "", sys: "按台账" };
    },
    factLine(area) {
      if (area === "进度") return this.alertN.all ? `已确认进度预警 ${this.alertN.all} 项（红 ${this.alertN.red}），详见预警页` : "暂无已确认的进度预警";
      if (area === "质量") return this.q.open ? `质量问题未闭环 ${this.q.open} 项${this.q.overdue ? `，其中超期 ${this.q.overdue} 项` : ""}` : "当前台账无未闭环质量问题";
      return this.s.open ? `安全隐患未闭环 ${this.s.open} 项，高风险 ${this.s.high || 0}，超期 ${this.s.overdue || 0}` : "当前台账无未闭环安全隐患";
    },
    async load() {
      try {
        const [d, cp, al] = await Promise.all([GET("/api/todo"), GET("/api/cockpit"), GET("/api/alerts?status=confirmed")]);
        this.d = d; this.cp = cp; this.alerts = al; this.err = null;
      } catch (e) { this.err = e; }
    },
    // 次要块各自加载，失败只影响本块
    async loadSide() {
      GET("/api/output/overview", { quiet: true }).then((o) => { this.out = o; }).catch(() => { this.out = null; });
      GET("/api/reports", { quiet: true }).then((r) => { this.rpts = r; }).catch(() => { this.rpts = []; });
      GET("/api/gongbi/types", { quiet: true }).then((r) => { this.types = r.types || []; }).catch(() => { this.types = []; });
      try {
        const f = await GET("/api/feed", { quiet: true });
        this.shots = (f.posts || []).filter((p) => p.photos && p.photos.length).slice(0, 2).map((p) => ({ key: p.key, photo: p.photos[0], text: p.text, tag: p.tag,
          place: p.geo && p.geo.place, author: p.author ? p.author.name : "", when: (p.time || "").slice(5, 10), broken: false }));
      } catch (_) { this.shots = []; }
      this.shotsLoaded = true;
    },
    openDoc(t) { if (t.kind === "week" || t.kind === "month") go("rpt", { seg: t.kind }); else go("reports", { kind: t.kind }); },
    async refresh() {
      if (this.busy) return;
      this.busy = true;
      try { await POST("/api/brief"); this.cp = await GET("/api/cockpit"); toast("简报已更新"); } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};
