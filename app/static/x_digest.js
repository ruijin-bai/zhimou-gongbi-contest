/* 列表页小结（2026-10-08 用户确认：预警、安全隐患、质量问题、日志审核、材料票据等列表页都加）。
   - DigestHead：小结头 + 一段 AI 判断。页面先按系统接口数据把归类、计数算好（facts + 规则版文字 fallback），
     POST /api/digest 让对应智能体写 1～2 句判断；后端做数字校验，不过或模型不可用时返回规则版文字并说明原因（见 services/digest.py）。
   - ListDigest：通用小结卡 = DigestHead + “需要拍板”逐条 + 其余分组一句话；分组后面的批量操作由页面用插槽 #acts 提供（没有就不显示）。
   明细列表照常显示在下方（默认展开），小结只是先给结论。 */
const DigestHead = {
  props: { kind: String, facts: Object, fallback: String, title: { type: String, default: "小结" } },
  template: `<div class="dg-head"><div class="ad-h"><span class="ad-ic"><ic n="sparkles"></ic></span>
      <div class="grow"><b>{{ r && r.source === 'ai' && r.provider !== 'mock' ? 'AI ' + title : title }}<span class="chip blue dg-tag" v-if="r && r.source === 'ai' && r.provider !== 'mock'">AI按当前事实整理</span><i class="zb-sys dg-tag" v-else-if="ai">离线模拟模型输出 · 不作结论</i><i class="zb-sys dg-tag" v-else-if="r && !loading">系统按当前事实生成</i></b>
        <small v-if="loading">{{ agentName }}正在看这些数据…</small>
        <small v-else-if="ai">{{ agentName }}根据下方统计写的判断，数字已与系统统计逐一核对<template v-if="r.provider === 'mock'">（离线模拟模型）</template></small>
        <small v-else>按规则自动归并，数字取自下方列表<template v-if="r && r.note">；{{ r.note }}</template></small></div><slot name="tools"></slot></div>
    <p class="ad-lead">{{ ai ? r.text : fallback }}</p>
    <div class="dg-basis" v-if="ai && r.text !== fallback"><span>系统统计：</span>{{ fallback }}</div></div>`,
  data: () => ({ r: null, loading: false, seq: 0 }),
  computed: {
    ai() { return !!(this.r && (this.r.source === "ai" || this.r.source === "mock")); },   // mock：照样显示，下方小字标“离线模拟模型”
    agentName() { return { alerts: "进度智能体", safety: "安全智能体", quality: "质量智能体", logs: "数据审核智能体", tickets: "材料智能体" }[this.kind] || "智能体"; },
    sig() { return JSON.stringify([this.kind, this.facts]); },
  },
  watch: { sig: { immediate: true, handler() { this.load(); } } },
  methods: {
    async load() {
      if (!this.facts || !this.fallback) return;
      if (!["eng_sup", "safety_sup", "manager", "leader"].includes(store.me.user.role)) { this.r = { source: "rule" }; return; }  // AI 小结只给港湾岗位，分包岗位直接用规则归并文字
      const n = ++this.seq; this.loading = true;
      try { const r = await POST("/api/digest", { kind: this.kind, facts: this.facts, fallback: this.fallback }); if (n === this.seq) this.r = r; }
      catch (_) { if (n === this.seq) this.r = { source: "rule", note: "AI 小结暂不可用" }; }
      finally { if (n === this.seq) this.loading = false; }
    },
  },
};

// 通用小结卡：key = [{id, title, why, tone, route, params}]，groups = [{k, tone, title, text, n}]
const ListDigest = {
  components: { DigestHead },
  props: { kind: String, title: String, lead: String, keyTitle: { type: String, default: "需要先处理" }, keyHint: String, keys: { type: Array, default: () => [] }, groups: { type: Array, default: () => [] } },
  template: `<section class="ad" v-if="lead">
    <digest-head :kind="kind" :title="title" :facts="facts" :fallback="lead"><template #tools><slot name="tools"></slot></template></digest-head>
    <div class="ad-key" v-if="keys.length"><div class="ad-sub"><i class="dot red"></i>{{ keyTitle }} · {{ keys.length }} 项<small v-if="keyHint">{{ keyHint }}</small></div>
      <a v-for="x in keys.slice(0, 6)" :key="x.id" class="ad-k" @click="x.route && go(x.route, x.params || {})"><span :class="['chip', x.tone || 'red']">{{ x.tag || '重点' }}</span><span class="grow">{{ x.title }}</span><small>{{ x.why }}</small><ic n="chevron" v-if="x.route"></ic></a>
      <div class="tiny muted ad-more" v-if="keys.length > 6">另有 {{ keys.length - 6 }} 项见下方列表</div></div>
    <div class="ad-grp" v-for="g in groups" :key="g.k"><div class="ad-gt"><span :class="['chip', g.tone || '']">{{ g.tag || '分组' }}</span><b>{{ g.title }}</b><span>{{ g.text }}</span></div>
      <slot name="acts" :g="g"></slot></div></section>`,
  computed: {
    // 交给 AI 的只有这些已统计好的内容（标题、原因、分组句），不含个人信息以外的原始记录
    facts() { return { lead: this.lead, key: this.keys.slice(0, 8).map((x) => [x.title, x.why].filter(Boolean).join("：")), groups: this.groups.map((g) => g.title + "：" + g.text) }; },
  },
  methods: { go },
};
