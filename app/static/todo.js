/* 各岗位首页 = 一张统一待办队列（后端 GET /api/todo 汇总，前端只负责展示与就地处理）。
   - TodoHome：电脑端岗位首页（工程主管/安全主管/项目经理）= 页头 + 结论一行（简报 headline，可展开依据；简报已失效时标“系统按当前事实生成”）+ 概览条 + 待办表 + 右侧“AI 今日要点”（≤3 条，来自简报Agent）+ 一行边界说明；
   - TodoMobile：手机端岗位（技术员/分包安全员）= 同一接口的卡片样式（状态标签、期限、“还缺：xxx”、一个主按钮），嵌在各自首页的固定顺序里；
   - 候选隐患是待办来源之一（唯一展示位）：展开后沿用 CandidateBox 就地【确认并派单】【修改后确认】【不采纳】，不跳页；T-05 的 CandidateGroup 在时按照片成组一次处理；
   - 预警确认属实/误报、赶工决定、整改闭环/退回、催办、提醒补报、认领扫码等都就地调用既有接口，完成后条目自动消失；
   - 项目经理只看拍板项，其余合成一行“需要知道：N 项”（只给数字和链接，完整列表在预警页、安全页、质量页）。
   领导（只读）和系统管理员（不参与业务决策）没有“要我办”的待办，保留各自的总览页。 */

// hash 路由串（如 finding?id=3、gate?open=A1020）→ 跳转
function todoGo(route) {
  if (!route) return;
  const [name, qs] = route.split("?"), params = {};
  (qs || "").split("&").filter(Boolean).forEach((p) => { const [k, v] = p.split("="); params[k] = decodeURIComponent(v || ""); });
  go(name, params);
}
const TODO_TONE = { red: "red", orange: "orange", blue: "blue", green: "green", gray: "" };
const fmtDue = (it) => (it.due ? it.due.slice(5) : "—");
// 统一命名（只改显示文字，接口不变）：预警动作与预警页一致为“确认属实 / 误报”；待办里超期或红色的条数叫“紧急待办”（筛选与概览格同名），
// 与简报、台账里的“超期隐患 / 高风险隐患”区分开——那是台账数，这里数的是待办条目（D-1）
const URGENT_NAME = "紧急待办";
// 整改提交被离线队列接管（T-09a 钩子返回 {queued:true}）时的就地提示
const TODO_QUEUED = "已暂存，联网后自动提交复核";
// 闭环后的一句（4.4 契约）：后端拼条目时拿得到闭环人才写“（某某确认）”，拿不到只写“已闭环”，不编造
function todoClosedText(d) {
  if (!d || d.status !== "resolved") return "";
  return "已闭环" + (d.closed_by_name ? `（${d.closed_by_name}确认）` : "");
}

// 展开区：候选 → CandidateBox；整改待复核 → 前后照片与 AI 复核建议（文案由后端按 T-13 的 verify 口径给出，闭环前只写“AI 建议”）；来历 → AiTrace（表格行与卡片共用）
const TodoBody = {
  components: { CandidateBox, AiTrace },
  props: { it: Object, open: Boolean, traceOpen: Boolean, trace: Object, terr: String },
  emits: ["changed"],
  template: `<div class="todo-body">
    <candidate-box v-if="it.inline === 'candidate' && open" :f="it.finding" @changed="$emit('changed')"></candidate-box>
    <div v-if="it.inline === 'review' && open" class="pair todo-pair">
      <div><div class="tiny muted">发现时</div><img v-if="it.detail.before" :src="'/api/media/' + it.detail.before + '/thumb'" alt="发现时" @click="store.viewer = '/api/media/' + it.detail.before + '/file'"></div>
      <div><div class="tiny muted">整改后</div><img v-if="it.detail.after" :src="'/api/media/' + it.detail.after + '/thumb'" alt="整改后" @click="store.viewer = '/api/media/' + it.detail.after + '/file'"><div class="ph" v-else>无照片</div></div>
      <div class="grow small" v-if="it.detail.verify_text"><b>{{ it.detail.verify_text }}</b>
        <span class="muted" v-if="it.detail.verify && it.detail.verify.reason && it.detail.verify_text.startsWith('AI 建议')">{{ it.detail.verify.reason }}</span>
        <div v-if="it.detail.photo_check" class="tiny tc-over">注意：{{ it.detail.photo_check }}</div>
        <div v-if="todoClosedText(it.detail)" class="small"><b>{{ todoClosedText(it.detail) }}</b></div>
        <div v-if="it.detail.assignee" class="tiny muted">责任人 {{ it.detail.assignee }}</div>
        <button class="btn sm mt" type="button" v-if="it.detail.reverify" :disabled="busy" @click="reverify">{{ busy ? '提交中' : '重新让 AI 复核' }}</button></div></div>
    <ai-trace v-if="traceOpen" :t="trace" :err="terr"></ai-trace></div>`,
  data: () => ({ store, busy: false }),
  methods: {
    todoClosedText,
    // 只在 AI 复核失败、或卡在复核中（running）超过 10 分钟时出现（后端按 reverify 接口同一条件给出）；人工随时可以直接闭环或退回。
    // 停在 pending 超过 10 分钟的，后端文案改为“AI 复核未开始，可人工闭环或退回”，不给这个按钮（reverify 只对 running/failed 开放）
    async reverify() {
      this.busy = true;
      try { await POST(`/api/findings/${this.it.finding.id}/reverify`); toast("已重新提交 AI 复核"); this.$emit("changed"); }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

// 待办里的现场照片缩略图：横向 4:3 完整显示（contain），点开原图；文件缺失显示“缺文件”，不出破图（RJ 10-10 照片约定）
const TodoThumb = {
  props: { id: Number, sm: Boolean },
  template: `<button type="button" :class="['todo-ph', sm ? 'sm' : '']" :title="bad ? '照片文件缺失' : '查看原图'" @click.stop="!bad && (store.viewer = '/api/media/' + id + '/file')">
    <img v-if="!bad" :src="'/api/media/' + id + '/thumb'" alt="现场照片" loading="lazy" @error="bad = true"><span v-else>缺文件</span></button>`,
  data: () => ({ bad: false, store }),
  watch: { id() { this.bad = false; } },
};

// 一条待办：电脑端是表格行（+展开行），手机端是卡片
const TodoItem = {
  components: { TodoBody, TodoThumb },
  props: { it: { type: Object, required: true }, cards: Boolean, focus: Boolean, total: Number },
  emits: ["changed"],
  template: `<template v-if="focus">
    <section :class="['focus-card', it.overdue ? 'over' : '', it.thumb ? '' : 'no-ph']">
      <div class="fc-pcol" v-if="it.thumb"><button type="button" class="fc-ph" :style="{backgroundImage: 'url(/api/media/' + it.thumb + '/file)'}" title="看大图" @click="store.viewer = '/api/media/' + it.thumb + '/file'"><ic n="search"></ic></button>
        <div class="fc-pcap"><ic n="camera"></ic>现场照片<a @click="store.viewer = '/api/media/' + it.thumb + '/file'">看大图</a></div></div>
      <div class="fc-in"><div class="fc-lead"><i></i>先办这件<span>· 待办按紧急程度排第一<template v-if="total > 1">，共 {{ total }} 项</template></span></div>
        <div class="fc-title">{{ it.title }}<span class="chip blue ai-tag" v-if="it.ai_tag">{{ it.ai_tag }}</span></div>
        <div class="fc-facts"><div><small>状态</small><b :class="{'tc-over': it.overdue}">{{ it.status }}</b></div><div><small>期限</small><b :class="{'tc-over': it.overdue}">{{ it.due ? fmtDue(it) : '—' }}</b></div><div><small>类别</small><b>{{ it.module_name }}</b></div></div>
        <div class="fc-reason">{{ it.reason }}</div>
        <div class="fk-acts"><button class="btn primary" v-if="primary" :disabled="busy" @click="main">{{ busy ? '处理中' : it.action_label }}<template v-if="expandable"> {{ open ? '▴' : '▾' }}</template></button>
          <button v-for="(q, i) in it.quick" :key="i" :class="['btn', q.primary && !primary ? 'primary' : '']" :disabled="busy" @click="runQuick(q)">{{ q.label }}</button>
          <a v-if="it.trace" @click="toggleTrace">{{ traceOpen ? '收起来历' : '来历' }}</a><a v-if="it.action_route && !primary" @click="todoGo(it.action_route)">详情</a><a v-for="x in links" :key="x.route" @click="todoGo(x.route)">{{ x.label }}</a></div>
        <div class="todo-note" v-if="note" role="status">{{ note }}<button class="btn sm" type="button" v-if="rej && canSafety" :disabled="busy" @click="asSafety">改作随手拍</button></div></div></section>
    <div class="focus-body" v-if="open || traceOpen"><todo-body :it="it" :open="open" :trace-open="traceOpen" :trace="trace" :terr="terr" @changed="$emit('changed')"></todo-body></div></template>
    <template v-else-if="cards">
    <div :class="['todo-card', it.overdue ? 'over' : '']">
      <div class="row wrap"><span :class="['chip', tone]">{{ it.status }}</span><span class="chip">{{ it.module_name }}</span><div class="grow"></div>
        <span :class="['tiny', it.overdue ? 'tc-over' : 'muted']" v-if="it.due">{{ it.overdue ? '已超期 ' : '期限 ' }}{{ it.due }}</span></div>
      <div class="row mt" style="align-items:flex-start;flex-wrap:nowrap"><todo-thumb v-if="it.thumb" :id="it.thumb"></todo-thumb>
        <div class="grow"><div class="small"><b>{{ it.title }}</b><span class="chip blue ai-tag" v-if="it.ai_tag">{{ it.ai_tag }}</span></div><div class="tiny muted">{{ it.reason }}</div></div></div>
      <div class="row wrap mt todo-acts"><button class="btn primary sm" v-if="primary" :disabled="busy" @click="main">{{ busy ? '处理中' : it.action_label }}<template v-if="expandable"> {{ open ? '▴' : '▾' }}</template></button>
        <button v-for="(q, i) in it.quick" :key="i" :class="['btn', 'sm', q.primary && !primary ? 'primary' : '']" :disabled="busy" @click="runQuick(q)">{{ q.label }}</button>
        <button class="btn ghost sm" v-if="it.trace" @click="toggleTrace">{{ traceOpen ? '收起来历' : '来历' }}</button>
        <button class="btn ghost sm" v-if="it.action_route && !primary" @click="todoGo(it.action_route)">详情</button>
        <button class="btn ghost sm" v-for="x in links" :key="x.route" @click="todoGo(x.route)">{{ x.label }}</button></div>
      <div class="todo-note" v-if="note" role="status">{{ note }}<button class="btn sm" type="button" v-if="rej && canSafety" :disabled="busy" @click="asSafety">改作随手拍</button></div>
      <todo-body v-if="open || traceOpen" :it="it" :open="open" :trace-open="traceOpen" :trace="trace" :terr="terr" @changed="$emit('changed')"></todo-body></div></template>
    <template v-else>
    <tr :class="['todo-row', it.overdue ? 'over' : '']">
      <td class="tc-title"><div class="tt"><todo-thumb v-if="it.thumb" :id="it.thumb" sm></todo-thumb>
        <div class="tt-b"><div class="tt-t">{{ it.title }}<span class="chip blue ai-tag" v-if="it.ai_tag">{{ it.ai_tag }}</span></div><div class="tt-r"><span class="tt-mod">{{ it.module_name }}</span>{{ it.reason }}</div></div></div></td>
      <td class="tc-when"><b v-if="it.overdue" class="tc-over">{{ it.status }}</b><span v-else :class="['chip', tone]">{{ it.status }}</span><small v-if="it.due">期限 {{ fmtDue(it) }}</small></td>
      <td class="tc-act"><div class="tc-btns">
        <button class="btn primary sm" v-if="primary" :disabled="busy" @click="main">{{ busy ? '处理中' : it.action_label }}<template v-if="expandable"> {{ open ? '▴' : '▾' }}</template></button>
        <button v-for="(q, i) in it.quick" :key="i" :class="['btn', 'sm', q.primary && !primary ? 'primary' : '']" :disabled="busy" @click="runQuick(q)">{{ q.label }}</button></div>
        <div class="todo-note" v-if="note" role="status">{{ note }}<button class="btn sm" type="button" v-if="rej && canSafety" :disabled="busy" @click="asSafety">改作随手拍</button></div>
        <div class="tc-links" v-if="it.trace || (it.action_route && !primary) || links.length"><a v-if="it.trace" @click="toggleTrace">{{ traceOpen ? '收起来历' : '来历' }}</a><a v-if="it.action_route && !primary" @click="todoGo(it.action_route)">详情</a><a v-for="x in links" :key="x.route" @click="todoGo(x.route)">{{ x.label }}</a></div></td></tr>
    <tr class="todo-xr" v-if="open || traceOpen"><td colspan="3"><todo-body :it="it" :open="open" :trace-open="traceOpen" :trace="trace" :terr="terr" @changed="$emit('changed')"></todo-body></td></tr></template>`,
  // note：就地提示（整改已暂存 / 照片被拒的原因）；rej：被拒的那张整改照片，可“改作随手拍”
  data() { return { open: false, traceOpen: false, busy: false, trace: null, terr: "", store, note: "", rej: null }; },
  computed: {
    tone() { return TODO_TONE[this.it.tone] || ""; },
    links() { return this.it.links || []; },
    canSafety() { return !!window.QUICK; },
    expandable() { return ["candidate", "review"].includes(this.it.inline); },
    // 有就地操作的条目：主按钮是“展开处理”或“拍整改照片”；没有就地操作的：主按钮直达处理页面
    primary() { return !!this.it.action_label && (["candidate", "rectify", "review"].includes(this.it.inline) || (!this.it.quick.length && !!this.it.action_route)); },
  },
  methods: {
    todoGo, fmtDue,
    main() {
      if (this.it.inline === "rectify") return this.rectify();
      if (this.expandable) { this.open = !this.open; return; }
      todoGo(this.it.action_route);
    },
    async toggleTrace() {
      this.traceOpen = !this.traceOpen;
      if (!this.traceOpen || this.trace || !this.it.trace) return;
      try { this.trace = await GET(`/api/ai/trace?type=${this.it.trace.type}&id=${this.it.trace.id}`); } catch (e) { this.terr = e.message; }
    },
    async runQuick(q) {
      let body = q.api && !Array.isArray(q.api) ? { ...(q.api.body || {}) } : undefined;
      if (q.prompt) {
        const t = prompt(q.prompt.text); if (t === null) return;
        if (q.prompt.required && !t.trim()) { toast("请填写原因", true); return; }
        body = { ...(body || {}), [q.prompt.field]: t };
      }
      if (q.confirm && !confirm(q.confirm)) return;
      this.busy = true;
      try {
        const calls = Array.isArray(q.api) ? q.api : [q.api];
        for (const a of calls) {
          const r = await api(a.method || "POST", a.url, Array.isArray(q.api) ? a.body : body);
          if (r && r.ok === false) throw new Error(r.error || "未成功，请稍后重试");
        }
        toast(q.done || "已完成"); this.$emit("changed");
      } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
    // 整改照片横拍竖拍都收（D4，显式传 allowPortrait）
    async rectify() {
      const [file] = await takePhotos({ multi: false, allowPortrait: true }); if (!file) return;
      this.busy = true; this.note = ""; this.rej = null;
      try {
        const fd = new FormData(); fd.append("purpose", "rectify"); addPhotos(fd, [file]); const [m] = await POST("/api/media", fd);
        const r = await POST("/api/findings/" + this.it.finding.id + "/rectify", { media_id: m.id });
        // 离线或信号差时（T-09a 的暂存钩子）先入队：就地提示已暂存，不刷新列表（断网刷新只会失败），补传成功后由 zb-quick-synced 刷新
        if (r && r.queued) { this.note = TODO_QUEUED; toast(TODO_QUEUED); return; }
        toast("整改照片已提交，等待安全主管复核"); this.$emit("changed");
      } catch (e) {
        // 拍摄时间早于派单超过 24 小时（422 photo_before_dispatch）：照片已存在服务器上但不挂这条整改；就地说明原因，可改作随手拍隐患上报
        if (e.code === "photo_before_dispatch") { this.rej = { file }; this.note = e.message + (window.QUICK ? "。这张照片可以改作随手拍隐患上报" : ""); }
        else toast(e.message, true);
      } finally { this.busy = false; }
    },
    // 改作随手拍：交给 T-09a 的 window.QUICK.asSafety(file)，同一张照片按 purpose=safety 入队上传并识别
    async asSafety() {
      const f = this.rej && this.rej.file; if (!f || !window.QUICK) return;
      this.busy = true;
      try { if (await window.QUICK.asSafety(f)) { this.rej = null; this.note = "已改作随手拍，上传后 AI 识别隐患"; toast(this.note); } }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

// 同一来源的重复事项（如 4 位技术员“上周进度未确认”）合成一行：可展开逐条看，也可一键全部提醒（“今日无记录”已删除）
const GROUP_SOURCES = { week_missing: "人上周进度未确认" };
const TodoGroup = {
  components: { TodoItem },
  props: { g: { type: Object, required: true }, cards: Boolean },
  emits: ["changed"],
  template: `<template v-if="cards">
      <div class="todo-card todo-group over"><div class="tt-t">{{ g.title }}</div><div class="tt-r">{{ g.reason }}</div>
        <div class="row wrap mt todo-acts"><button class="btn primary sm" :disabled="busy" @click="remindAll">{{ busy ? '发送中' : '全部提醒' }}</button><button class="btn sm" @click="open = !open">{{ open ? '收起' : '逐个查看' }}</button></div></div>
      <template v-if="open"><todo-item v-for="it in g.items" :key="it.id" :it="it" :cards="true" @changed="$emit('changed')"></todo-item></template></template>
    <template v-else>
      <tr :class="['todo-row', 'todo-group', g.overdue ? 'over' : '']"><td class="tc-title"><div class="tt"><div class="tt-b"><div class="tt-t">{{ g.title }}</div><div class="tt-r"><span class="tt-mod">{{ g.module_name }}</span>{{ g.reason }}</div></div></div></td>
        <td class="tc-when"><span :class="['chip', g.tone]">{{ g.status }}</span><small v-if="g.due" :class="g.overdue ? 'tc-over' : ''">{{ g.overdue ? '已超期 ' : '期限 ' }}{{ g.due.slice(5) }}</small></td>
        <td class="tc-act"><div class="tc-btns"><button class="btn primary sm" :disabled="busy" @click="remindAll">{{ busy ? '发送中' : '全部提醒' }}</button></div>
          <div class="tc-links"><a @click="open = !open">{{ open ? '收起明细' : '逐个查看' }}</a></div></td></tr>
      <template v-if="open"><todo-item v-for="it in g.items" :key="it.id" :it="it" @changed="$emit('changed')"></todo-item></template></template>`,
  data: () => ({ open: false, busy: false }),
  methods: {
    async remindAll() {
      const qs = this.g.items.map((i) => i.quick && i.quick[0]).filter((q) => q && q.api && !Array.isArray(q.api));
      if (!qs.length || !confirm(`向 ${qs.length} 位技术员发送提醒？`)) return;
      this.busy = true;
      try { for (const q of qs) await api(q.api.method || "POST", q.api.url, q.api.body); toast(`已提醒 ${qs.length} 人`); this.$emit("changed"); }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

// 同一张照片识别出的候选隐患合成一行，展开后挂 T-05 的 CandidateGroup 一次处理（默认全选、默认责任人与期限）。
// 只在 window.CandidateGroup 存在时启用；不存在时候选仍逐条用 CandidateBox（见 TodoBoard.entries）
const TodoCandGroup = {
  components: { TodoThumb },
  props: { g: { type: Object, required: true }, cards: Boolean },
  emits: ["changed"],
  template: `<template v-if="cards">
      <div class="todo-card"><div class="row wrap"><span :class="['chip', g.tone]">{{ g.status }}</span><span class="chip">{{ g.module_name }}</span></div>
        <div class="row mt" style="align-items:flex-start;flex-wrap:nowrap"><todo-thumb v-if="g.thumb" :id="g.thumb"></todo-thumb>
          <div class="grow"><div class="small"><b>{{ g.title }}</b><span class="chip blue ai-tag">AI 发现</span></div><div class="tiny muted">{{ g.reason }}</div></div></div>
        <div class="row wrap mt todo-acts"><button class="btn primary sm" @click="open = !open">确认处理 {{ open ? '▴' : '▾' }}</button></div>
        <div class="todo-body" v-if="open"><component :is="comp" :group-key="g.group_key" :items="findings" :default-assignee-id="g.default_assignee_id" :default-due="null"
          @done="$emit('changed')" @changed="$emit('changed')"></component></div></div></template>
    <template v-else>
      <tr :class="['todo-row', g.overdue ? 'over' : '']"><td class="tc-title"><div class="tt"><todo-thumb v-if="g.thumb" :id="g.thumb" sm></todo-thumb>
          <div class="tt-b"><div class="tt-t">{{ g.title }}<span class="chip blue ai-tag">AI 发现</span></div><div class="tt-r"><span class="tt-mod">{{ g.module_name }}</span>{{ g.reason }}</div></div></div></td>
        <td class="tc-when"><span :class="['chip', g.tone]">{{ g.status }}</span></td>
        <td class="tc-act"><div class="tc-btns"><button class="btn primary sm" @click="open = !open">确认处理 {{ open ? '▴' : '▾' }}</button></div></td></tr>
      <tr class="todo-xr" v-if="open"><td colspan="3"><div class="todo-body"><component :is="comp" :group-key="g.group_key" :items="findings" :default-assignee-id="g.default_assignee_id" :default-due="null"
        @done="$emit('changed')" @changed="$emit('changed')"></component></div></td></tr></template>`,
  data: () => ({ open: false, store }),
  computed: {
    comp() { return window.CandidateGroup; },
    findings() { return this.g.items.map((i) => i.finding); },
  },
};

// 同一来源 ≥3 条的重复事项合成一行；有 CandidateGroup 时同一张照片的候选合成一组（筛选/搜索后仍按当前可见的条目分组）
function todoEntries(list) {
  const by = {}, cand = {}, grouped = !!window.CandidateGroup;
  for (const i of list) {
    if (GROUP_SOURCES[i.source]) (by[i.source] = by[i.source] || []).push(i);
    if (grouped && i.source === "candidate" && i.group_key) (cand[i.group_key] = cand[i.group_key] || []).push(i);
  }
  const out = [], made = new Set();
  for (const i of list) {
    if (grouped && i.source === "candidate" && i.group_key) {
      if (made.has("cg:" + i.group_key)) continue;
      made.add("cg:" + i.group_key);
      const g = cand[i.group_key];
      out.push({ id: "cg:" + i.group_key, cgroup: true, items: g, group_key: i.group_key, default_assignee_id: i.default_assignee_id ?? null, module: i.module, module_name: i.module_name,
        tone: g.some((x) => x.tone === "red") ? "red" : "orange", status: i.status, overdue: false, due: null, thumb: i.thumb,
        title: g.length > 1 ? `这张照片识别出 ${g.length} 项隐患` : i.title, reason: (g.length > 1 ? g.map((x) => x.title).join("、") + "；" : "") + "确认并派单后才计入台账、通知整改人" });
      continue;
    }
    const same = by[i.source];
    if (!same || same.length < 3) { out.push(i); continue; }
    if (made.has(i.source)) continue;
    made.add(i.source);
    out.push({ id: "grp:" + i.source, items: same, module: i.module, module_name: i.module_name, tone: TODO_TONE[i.tone] || "", status: i.status, overdue: same.some((x) => x.overdue),
      due: same.map((x) => x.due).filter(Boolean).sort()[0] || null,
      title: `${same.length} ${GROUP_SOURCES[i.source]}`, reason: same.slice(0, 3).map((x) => x.title.split("（")[0]).join("、") + (same.length > 3 ? " 等" : "") + "；提醒后由技术员自行补交" });
  }
  return out;
}
const todoUrgent = (i) => i.overdue || i.tone === "red";

// 待办表 / 卡片：页签（全部/现场/进度/安全/质量/报表）带计数，“今日到期”“紧急待办”筛选，搜索；期限优先排序与超期置顶由后端完成。
// 页签与筛选的计数按表里实际的行数算（合并行算一行，U-31），点进去看到几行就写几
const TodoBoard = {
  components: { TodoItem, TodoGroup, TodoCandGroup },
  props: { d: { type: Object, required: true }, cards: Boolean, skip: String },
  emits: ["changed"],
  template: `<section :class="['todo-board', asCards ? 'as-cards' : '', d.items.length ? '' : 'is-empty', skip ? 'with-focus' : '']" id="todo-board">
    <div class="todo-cap" v-if="skip && !asCards"><b>其余待办</b></div>
    <div class="todo-bar" v-if="d.items.length"><div class="todo-tabs" role="tablist"><button v-for="m in d.modules" :key="m.key" role="tab" :aria-selected="tab === m.key" :class="{on: tab === m.key, zero: !tabN[m.key]}" @click="tab = m.key">{{ m.name }}<b>{{ tabN[m.key] }}</b></button></div>
      <div class="todo-tools row wrap" v-if="d.items.length > 3">
        <button :class="['tfilter', {on: f === 'today'}]" @click="setFilter('today')">今日到期 {{ todayN }}</button>
        <button :class="['tfilter', {on: f === 'urgent'}]" @click="setFilter('urgent')">{{ URGENT_NAME }} {{ urgentN }}</button>
        <input class="in tsearch" v-model.trim="q" placeholder="搜索事项" aria-label="搜索待办"></div></div>
    <empty-state class="todo-empty" v-if="!d.items.length" :title="emptyTitle" :reason="d.empty && !cards ? d.empty.hint : '当前没有需要你办理的事项。'" :actions="d.empty && !cards ? d.empty.actions : []"></empty-state>
    <empty-state class="todo-empty" compact v-else-if="!rows.length" reason="没有符合条件的事项。" :action="{label: '清除筛选', fn: reset}"></empty-state>
    <div class="scroll-x" v-else-if="!asCards"><table class="t todo-t"><thead><tr><th>事项与办理原因</th><th>状态与期限</th><th>下一步</th></tr></thead>
      <tbody><template v-for="e in shown" :key="e.id"><tr class="todo-grp" v-if="grpHead[e.id]"><td colspan="3"><span :class="grpHead[e.id].cls">{{ grpHead[e.id].label }}</span><i>{{ grpHead[e.id].n }}</i></td></tr><todo-cand-group v-if="e.cgroup" :g="e" @changed="$emit('changed')"></todo-cand-group><todo-group v-else-if="e.items" :g="e" @changed="$emit('changed')"></todo-group><todo-item v-else :it="e" @changed="$emit('changed')"></todo-item></template></tbody></table></div>
    <div v-else class="todo-cards-list"><template v-for="e in shown" :key="e.id"><todo-cand-group v-if="e.cgroup" :g="e" :cards="true" @changed="$emit('changed')"></todo-cand-group><todo-group v-else-if="e.items" :g="e" :cards="true" @changed="$emit('changed')"></todo-group><todo-item v-else :it="e" :cards="true" @changed="$emit('changed')"></todo-item></template></div>
    <div class="row mt" v-if="entries.length > (asCards ? 8 : LIMIT)"><a class="small" @click="all = !all">{{ all ? '收起' : '显示全部 ' + entries.length + ' 项' }}</a></div>
    <div class="todo-know" v-if="d.know && d.know.n"><span class="todo-know-k">需要知道：</span><b>{{ d.know.n }}</b> 项<span class="todo-know-p" v-for="p in d.know.parts" :key="p.source"> · <a @click="todoGo(p.route)">{{ p.label }} {{ p.n }}</a></span></div></section>`,
  data: () => ({ tab: "all", f: "", q: "", all: false, LIMIT: 12, narrow: false, URGENT_NAME }),
  mounted() { this.mq = window.matchMedia("(max-width: 760px)"); this.narrow = this.mq.matches; this.onMq = (e) => { this.narrow = e.matches; }; this.mq.addEventListener("change", this.onMq); },
  unmounted() { if (this.mq) this.mq.removeEventListener("change", this.onMq); },
  computed: {
    // 手机宽度下表格放不下：岗位首页的待办表也换成卡片（同一份数据、同一套操作）
    asCards() { return this.cards || this.narrow; },
    // 后端空状态标题以“暂无待办”开头：前端去掉这个说法，只留原因
    emptyTitle() { const t = this.d.empty ? this.d.empty.title : ""; return t.replace(/^暂无待办[：:]?/, "") || "现在没有待办"; },
    // skip：智能管理台把排第一的那条单独放在“先办这件”主卡里，这里不再重复列出（同一事项只有一个展示位）
    allEntries() { return todoEntries(this.d.items.filter((i) => i.id !== this.skip)); },
    tabN() { const n = { all: this.allEntries.length }; for (const m of this.d.modules) if (m.key !== "all") n[m.key] = this.allEntries.filter((e) => e.module === m.key).length; return n; },
    todayN() { return this.allEntries.filter((e) => (e.cgroup || !e.items ? e.due === this.d.day : e.items.some((x) => x.due === this.d.day))).length; },
    urgentN() { return this.allEntries.filter((e) => (e.items ? e.items.some(todoUrgent) : todoUrgent(e))).length; },
    rows() {
      const q = this.q.toLowerCase();
      return this.d.items.filter((i) => i.id !== this.skip).filter((i) => (this.tab === "all" || i.module === this.tab) && (!this.f || (this.f === "today" ? i.due === this.d.day : todoUrgent(i)))
        && (!q || (i.title + " " + i.reason).toLowerCase().includes(q)));
    },
    entries() { return todoEntries(this.rows); },
    shown() { return this.all ? this.entries : this.entries.slice(0, this.asCards ? 8 : this.LIMIT); },
    // 分组标题只是按已有字段归类显示（超期 / 今日到期 / 其余），不改排序：后端已按期限优先、超期置顶排好
    grpHead() {
      const kind = (e) => (e.overdue ? "late" : e.due === this.d.day ? "today" : "rest");
      const names = { late: ["已超期", "tc-over"], today: ["今日到期", ""], rest: ["其余事项", ""] };
      const n = {}; this.entries.forEach((e) => { const k = kind(e); n[k] = (n[k] || 0) + 1; });
      const out = {}; let prev = "";
      for (const e of this.shown) { const k = kind(e); if (k !== prev) out[e.id] = { label: names[k][0], cls: names[k][1], n: n[k] }; prev = k; }
      return Object.keys(n).length > 1 || this.skip ? out : {};
    },
  },
  methods: {
    todoGo,
    setFilter(f) { this.f = this.f === f ? "" : f; this.tab = "all"; },
    reset() { this.tab = "all"; this.f = ""; this.q = ""; },
  },
};

// 概览条：4 个关键数，可点击（筛选待办 / 跳转）。“待处理”“紧急待办”格与待办表的页签、同名筛选按同一张表的行数算（合并行算一行）；
// 台账类数字（超期隐患、高风险隐患、超期质量问题）后端取自简报同一套计数，与简报写的数一致（D-1）
const OV_ICONS = { 今日新增问题: "plus", 待我拍板: "flag", 待处理: "clipboard", 待整改与确认: "clipboard", [URGENT_NAME]: "alert", 超期隐患: "clock", 待确认红橙预警: "alert",
  今日随手记: "mic", 今日随手拍: "camera", 超期质量问题: "award", 本周确认进度: "check-circle", 本周确认: "check-circle", 今日新增隐患: "helmet", 本周已闭环: "check-circle" };
const TodoStrip = {
  props: { d: Object },
  emits: ["filter"],
  template: `<div class="ov-strip"><component :is="o.route || o.filter ? 'button' : 'div'" v-for="(o, i) in d.overview" :key="i" :class="['ov-stat', o.tone || '', {link: o.route || o.filter}]" @click="pick(o)">
    <span class="ov-ic"><ic :n="icon(o)"></ic></span><div class="v">{{ shown(o) }}</div><div class="l">{{ o.filter === 'urgent' ? URGENT_NAME : o.label }}</div><div class="tiny muted" v-if="o.sub && o.filter !== 'urgent'">{{ o.sub }}</div></component></div>`,
  data: () => ({ URGENT_NAME }),
  computed: { entries() { return todoEntries(this.d.items); } },
  methods: {
    // 概览格的图标只按名称挑，不影响数字与口径
    icon(o) { return OV_ICONS[o.filter === "urgent" ? URGENT_NAME : o.label] || "chart"; },
    shown(o) {
      if (o.filter === "all") return this.entries.length;
      if (o.filter === "urgent") return this.entries.filter((e) => (e.items ? e.items.some(todoUrgent) : todoUrgent(e))).length;
      return o.value;
    },
    pick(o) { if (o.route) todoGo(o.route); else if (o.filter) this.$emit("filter", o.filter); },
  },
};

// 结论一行：页头下方，取今日简报的 headline（安全主管取简报里的安全条目；后端已按 scope_ok 与零样本校验把关，项目经理只见进度、质量、安全）。
// 简报结论已失效、改由系统按当前事实生成时（source === 'system'），标签直接写“系统按当前事实生成”，失效原因一行小字直接显示，不收进“依据”（AC-3.5）；
// 其余情况标签为“AI 今日结论”。没有简报、简报不可用时整行不显示；“依据”展开简报的生成方式与事实底稿（接口没带 basis 时按需从 /api/cockpit 取同一份简报）。
const TodoConclusion = {
  props: { a: Object },
  template: `<div :class="['ai-concl', sys ? 'is-sys' : '']" v-if="text"><div class="ai-concl-row"><span class="ai-concl-k"><template v-if="sys">系统按当前事实生成</template><template v-else><ic n="bot"></ic>AI 今日结论</template></span><span class="ai-concl-t">{{ text }}</span>
      <button class="ai-concl-why" type="button" :aria-expanded="open ? 'true' : 'false'" @click="toggle">依据 {{ open ? '▴' : '▾' }}</button></div>
    <div class="ai-concl-note tiny muted" v-if="sys && a.note">{{ a.note }}</div>
    <div class="ai-concl-basis" v-if="open"><div class="tiny muted" v-if="err">{{ err }}</div>
      <template v-else-if="b"><div class="tiny">{{ b.source || '今日简报' }}<template v-if="a.at"> · 生成于 {{ fmtDT(a.at) }}</template></div>
        <div class="tiny muted" v-if="b.note && !sys">{{ b.note }}</div>
        <ul class="ai-concl-lines" v-if="b.lines && b.lines.length"><li v-for="(l, i) in b.lines" :key="i">{{ l }}</li></ul>
        <div class="tiny muted" v-else>这份简报没有留存事实底稿。</div></template>
      <div class="tiny muted" v-else>加载中</div></div></div>`,
  data: () => ({ open: false, b: null, err: "" }),
  computed: {
    text() { const a = this.a || {}, h = (a.enabled && !a.missing && a.headline) || ""; return /^简报Agent暂不可用/.test(h) ? "" : h; },
    sys() { return (this.a || {}).source === 'system'; },
  },
  watch: { "a.at"() { this.b = null; this.err = ""; this.open = false; } },
  methods: {
    fmtDT,
    async toggle() {
      this.open = !this.open;
      if (!this.open || this.b) return;
      if (this.a.basis) { this.b = this.a.basis; return; }
      try { const d = await GET("/api/cockpit"); this.b = (d.brief && d.brief.basis) || { lines: [] }; } catch (e) { this.err = e.message; }
    },
  },
};

// AI 今日要点：简报Agent 的前 3 条（只含进度、质量、安全）+ 催办/固定的提问（扩展提供；项目经理只有固定的提问）+ 智能体今日处理一行
const TodoAttention = {
  props: { d: Object, home: String },
  emits: ["changed"],
  template: `<aside class="attn"><div class="row between"><h2>AI 今日要点</h2>
      <button class="btn ghost sm" :disabled="briefing" @click="makeBrief">{{ briefing ? '生成中' : (a.missing ? '生成今日简报' : '刷新') }}</button></div>
    <template v-if="a.enabled && !a.missing"><div class="attn-item" v-for="(it, i) in items" :key="i"><span :class="['lv-dot', it.level]"></span>
        <div class="grow"><div class="attn-t"><span class="attn-area">{{ it.area }}</span>{{ it.title }}</div><div class="attn-d">{{ it.detail }}</div>
          <div class="attn-a"><span>建议：{{ it.action }}</span><a v-if="it.locate" class="attn-loc" @click="todoGo(it.locate)">定位</a></div></div></div>
      <div class="tiny muted attn-none" v-if="!items.length">{{ hidden ? '简报里的事项都已在左侧待办里。' : a.merged ? '简报要点已写在上方结论里。' : '简报里没有需要关注的事项。' }}</div>
      <div class="tiny muted mt" v-if="a.at">简报生成于 {{ fmtDT(a.at) }}</div></template>
    <div class="small muted" v-else-if="a.missing">今日简报还没有生成。简报只汇总进度、质量、安全，生成后取前 3 条显示在这里。</div>
    <details class="attn-more mt" v-if="extra.length"><summary class="small">{{ moreTitle }} <span class="muted">{{ extra.length }}</span></summary>
      <div class="attn-item" v-for="(it, n) in extra" :key="n + it.title"><span :class="['lv-dot', it.level]"></span>
        <div class="grow small"><span class="chip">{{ it.agent }}</span> <b>{{ it.title }}</b><div class="tiny muted" v-if="it.basis">{{ it.basis }}</div>
          <div class="row wrap mt" v-if="it.actions && it.actions.length"><button v-for="(x, m) in it.actions" :key="m" :class="['btn', 'sm', m ? 'ghost' : '']" :disabled="busy" @click="act(x)">{{ x.label }}</button></div></div></div></details>
    <div class="attn-did tiny muted"><template v-if="d.did.length"><ic n="bot"></ic>智能体今日处理：<span v-for="(x, i) in d.did" :key="x.key" :title="x.did">{{ x.name }} {{ x.ok }} 次<template v-if="x.runs > x.ok">（失败 {{ x.runs - x.ok }}）</template><template v-if="i < d.did.length - 1">　</template></span>　</template>
      <a v-if="agentsLink" @click="go('agents')">{{ agentsLink }} →</a></div></aside>`,
  data: () => ({ briefing: false, extra: [], busy: false }),
  computed: {
    a() { return this.d.attention; },
    // 只在本岗位导航里有“智能体”页时给链接（安全主管没有这一页）；导航编排 10-08 起各岗位统一叫“智能体”
    agentsLink() { return navRolePages(roleOf()).some((p) => p.k === "agents") ? "智能体" : ""; },
    // 同一件事只有一个完整展示位：简报条目里的作业代码（如 RD220）已经是左侧某条待办时，不在这里再写一遍
    items() {
      const have = this.d.items.map((i) => i.title + " " + i.reason).join(" ");
      const codes = (t) => t.match(/[A-Z]{1,3}\d{2,}/g) || [];
      return (this.a.items || []).filter((it) => { const c = codes(it.title + " " + it.detail); return !(c.length && c.some((x) => have.includes(x))); });
    },
    hidden() { return (this.a.items || []).length > this.items.length; },
    // 项目经理不进催办台（D7、AC-6.7），这里只有固定的提问；其他岗位照旧
    moreTitle() { return this.d.role === "manager" ? "固定的提问" : "催办与固定的提问"; },
  },
  mounted() { this.loadExtra(); this.stop = watch(() => NUDGE.ver.n, () => this.loadExtra()); },
  unmounted() { if (this.stop) this.stop(); },
  methods: {
    go, todoGo, fmtDT,
    async loadExtra() { try { this.extra = await EXT.collect(this.home); } catch (_) { this.extra = []; } },
    async makeBrief() { this.briefing = true; try { await POST("/api/brief"); this.$emit("changed"); } catch (e) { toast(e.message, true); } finally { this.briefing = false; } },
    // 扩展提醒条目的动作：直达目标页，或就地调用接口完成后刷新
    async act(a) {
      if (a.route) return go(a.route, a.params || {});
      if (!a.api || (a.confirm && !confirm(a.confirm))) return;
      this.busy = true;
      try {
        const r = await api(a.api.method || "POST", a.api.url, a.api.body);
        if (r && r.ok === false) toast("未成功：" + (r.error || "请稍后重试"), true); else toast(a.done || "已完成");
        await this.loadExtra();
      } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

const SiteShots = {
  template: `<section class="site-shots"><div class="card-h"><b>最近现场</b><span>随手记、随手拍</span></div>
    <div class="ss-grid" v-if="shots.length"><figure v-for="p in shots" :key="p.key" class="ss-it"><button type="button" class="ss-im" :style="{backgroundImage: 'url(' + p.photo.thumb + ')'}" @click="store.viewer = p.photo.url" :title="p.text">
        <em v-if="p.hz">AI 识别 {{ p.hz }} 处</em></button><figcaption><b>{{ p.place || p.text || p.tag }}</b><small>{{ p.author }} · {{ p.when }}</small></figcaption></figure></div>
    <div class="ss-empty tiny muted" v-else-if="loaded">还没有带照片的现场记录。</div>
    <a class="ss-more" @click="go('circle')">在现场圈看全部 →</a></section>`,
  data: () => ({ shots: [], loaded: false, store }),
  async mounted() {
    try {
      const d = await GET("/api/feed", { quiet: true });
      this.shots = (d.posts || []).filter((p) => p.photos && p.photos.length).slice(0, 4).map((p) => {
        const m = (p.text || "").match(/识别出 (\d+) 处隐患/) || (p.comments || []).map((c) => (c.text || "").match(/识别出 (\d+) 处隐患/)).find(Boolean);
        return { key: p.key, photo: p.photos[0], text: p.text, tag: p.tag, place: p.geo && p.geo.place, author: p.author ? p.author.name : "", when: (p.time || "").slice(5, 10), hz: m ? m[1] : "" };
      });
    } catch (_) { this.shots = []; }
    this.loaded = true;
  },
  methods: { go },
};

const todoLoad = {
  async load() { try { this.d = await GET("/api/todo"); this.err = ""; } catch (e) { if (!this.d) this.err = e.message; } },
};

// 电脑端岗位首页：页头（PageHead）→ AI 今日结论 → 概览条 → 待办表 | AI 今日要点 → 边界说明；home 取 EXT.feed 的首页键（work / eng / sdash）
// P6 数据日期只在页头 meta 里一行灰字，超过 14 天加一个小“需更新”标签，不占首屏红字
const TodoHome = {
  components: { TodoBoard, TodoStrip, TodoAttention, TodoConclusion, TodoItem, SiteShots },
  props: { title: String, home: String },
  template: `<div class="todo-home" v-if="d">
    <section class="home-hero"><div class="hero-img"><div class="hero-proj">{{ projName }}</div>
    <page-head :title="title" page="desk"><template #meta><template v-if="d.p6">P6 数据日期 {{ d.p6.data_date }}<template v-if="d.p6.stale_days > 0"> · 距今 {{ d.p6.stale_days }} 天</template><span class="chip p6-stale" v-if="d.p6.stale_days > 14">需更新</span></template>
        <a v-else-if="d.role !== 'safety_sup'" @click="go('progress', {tab: 'plan'})">未导入 P6 计划，去导入</a></template>
      <template #actions><slot name="actions"></slot></template></page-head></div>
    <todo-strip :d="d" @filter="f => $refs.board.setFilter(f)"></todo-strip></section>
    <div :class="['home-grid', focus ? '' : 'no-focus']">
      <todo-item v-if="focus" class="hg-focus" :it="focus" :focus="true" :total="d.items.length" @changed="load"></todo-item>
      <aside class="home-rail hg-rail"><todo-conclusion :a="d.attention"></todo-conclusion><todo-attention :d="d" :home="home" @changed="load"></todo-attention></aside>
      <todo-board class="hg-board" ref="board" :d="d" :skip="focus ? focus.id : ''" @changed="load"></todo-board>
      <site-shots class="hg-site"></site-shots></div>
    <ai-note page="desk" :text="d.boundary"></ai-note></div><div v-else-if="err" class="empty">{{ err }}</div><div v-else class="empty">加载中</div>`,
  data: () => ({ d: null, err: "" }),
  computed: {
    projName() { return store.me && store.me.project ? store.me.project.name : ""; },
    // “先办这件”：后端排好序的第一条普通待办（合并行、候选成组不单拎出来）
    focus() { const it = this.d && this.d.items[0]; return it && !(GROUP_SOURCES[it.source]) && !(it.source === "candidate" && it.group_key && window.CandidateGroup) ? it : null; },
  },
  mounted() { this.load(); this.onVis = () => { if (document.visibilityState === "visible") this.load(); }; document.addEventListener("visibilitychange", this.onVis); },
  unmounted() { document.removeEventListener("visibilitychange", this.onVis); },
  methods: { go, load: todoLoad.load },
};

// 手机端岗位：同一接口的卡片列表（技术员、分包安全员），顺序与各自首页固定顺序一致；strip=true 时带 4 个概览数
const TodoMobile = {
  components: { TodoBoard, TodoStrip },
  props: { strip: Boolean, title: { type: String, default: "我的待办" } },
  emits: ["loaded"],
  template: `<div class="todo-mobile" v-if="d"><div class="row between"><h2 class="todo-h">{{ title }}<span class="chip orange" v-if="d.total">{{ d.total }} 项</span></h2><span class="tiny tc-over" v-if="d.overdue">已超期 {{ d.overdue }} 项</span></div>
    <todo-strip v-if="strip" :d="d" @filter="f => $refs.board.setFilter(f)"></todo-strip>
    <todo-board ref="board" :d="d" :cards="true" @changed="changed"></todo-board></div>`,
  data: () => ({ d: null, err: "" }),
  mounted() { this.load(); this.onSync = () => this.load(); window.addEventListener("zb-quick-synced", this.onSync); },
  unmounted() { window.removeEventListener("zb-quick-synced", this.onSync); },
  methods: { load: todoLoad.load, changed() { this.load(); this.$emit("loaded"); } },
};
