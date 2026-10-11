/* AI 候选隐患的处理（T-05 候选收敛，P0-5）：安全智能体从照片识别出的隐患先是“候选·待确认”，由本单位安全员或安全主管（及项目经理）决定。
   CandidateGroup：同一张照片识别出的候选一次处理——默认全选，责任人、期限带默认值且可就地改；
     【确认并派单】一次完成确认+指派+期限；【当场已纠正】一键闭环入台账（标“当场纠正、无整改照片”，D6）；
     【不采纳】从选项里选原因，可补充说明（不弹输入框）；同部位同类别 14 天内未闭环的提示“可能是同一问题，并入 #N”。
     全部经 POST /api/candidates/batch 提交，服务端逐条校验、整批生效或整批拒绝。确认前不进台账、不通知整改人。
     props：groupKey（如 "media:123"）、items（待办 _fmin 结构，或隐患详情）、defaultAssigneeId、defaultDue；事件 done({decided})、changed()。
   CandidateBox：待办以外的地方只读，给“去处理”回到首页待办（同一条候选只有一个决策入口）；隐患详情页（单条深链）里挂同一个 CandidateGroup。 */
const CAND_DUE_HINT = "期限不填则按风险等级默认（高：当天，中：2 天，低：6 天，从确认当天算起）";
const CAND_CACHE = { people: null, reasons: null };
const candPeople = () => CAND_CACHE.people || (CAND_CACHE.people = GET("/api/assignees").catch((e) => { CAND_CACHE.people = null; throw e; }));
const candReasons = () => CAND_CACHE.reasons || (CAND_CACHE.reasons = GET("/api/candidates/reasons").catch((e) => { CAND_CACHE.reasons = null; throw e; }));

const CandidateGroup = {
  props: { groupKey: { type: String, default: "" }, items: { type: Array, required: true }, defaultAssigneeId: { type: Number, default: null }, defaultDue: { type: String, default: null } },
  emits: ["done", "changed"],
  template: `<div class="cand-box">
    <div class="tiny muted" v-if="items.length > 1">这张照片识别出 {{ items.length }} 项，默认全选；去掉勾选的不随本次处理。</div>
    <div class="mt" v-for="f in items" :key="f.id">
      <label class="small"><input type="checkbox" v-model="sel[f.id]" :disabled="!!busy || !canDecide" v-if="items.length > 1">
        <span :class="['chip', tone(f)]">{{ levelName(f) }}</span> <template v-if="b(f).category">{{ b(f).category }} · </template>{{ b(f).desc || f.title }}</label>
      <div class="tiny muted" v-if="b(f).scene || b(f).measure"><template v-if="b(f).scene">场景：{{ b(f).scene }}　</template><template v-if="b(f).measure">AI 建议措施：{{ b(f).measure }}</template></div>
      <div class="tiny tc-over" v-if="sim(f)">可能是同一问题，并入 #{{ sim(f).id }}（{{ sim(f).title }}）
        <a class="link" href="" v-if="canDecide" @click.prevent="merge(f)">并入 #{{ sim(f).id }}</a></div>
      <div class="tiny" v-if="canDecide"><a class="link" href="" @click.prevent="toggleEdit(f)">{{ ed[f.id] ? '不修改' : '修改等级/描述' }}</a></div>
      <div class="grid g2" v-if="ed[f.id]">
        <label class="tiny muted">风险等级<select class="in" v-model="ed[f.id].level"><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></label>
        <label class="tiny muted">类别<input class="in" v-model="ed[f.id].category" maxlength="40"></label>
        <label class="tiny muted" style="grid-column:1/-1">描述<input class="in" v-model="ed[f.id].desc" maxlength="200"></label>
        <label class="tiny muted" style="grid-column:1/-1">整改措施<input class="in" v-model="ed[f.id].measure" maxlength="200"></label></div></div>
    <div class="tiny muted mt" v-if="!canDecide">待本单位安全员或安全主管确认。确认前不计入台账、不通知整改人。</div>
    <div class="cand-form mt" v-else>
      <div class="grid g2">
        <label class="tiny muted">责任人<select class="in" v-model.number="assignee"><option :value="0">请选择责任人</option>
          <option v-for="a in people" :key="a.id" :value="a.id">{{ a.name }}（{{ a.company }} · {{ a.role_name }}）{{ a.id === dflt ? '· 默认' : '' }}</option></select></label>
        <label class="tiny muted">整改期限<input class="in" type="date" v-model="due" :min="today()"></label></div>
      <div class="tiny muted">{{ dueHint }}。确认前不计入台账、不通知整改人；当场已纠正＝直接闭环入台账，标“当场纠正、无整改照片”。</div>
      <div class="row wrap mt" v-if="!dismissing">
        <button class="btn primary sm" :disabled="!!busy || !n || !assignee" @click="submit('confirm')">{{ busy === 'confirm' ? '提交中' : '确认并派单' + cnt }}</button>
        <button class="btn sm" :disabled="!!busy || !n" @click="submit('onspot')">{{ busy === 'onspot' ? '提交中' : '当场已纠正' + cnt }}</button>
        <button class="btn sm" :disabled="!!busy || !n" @click="openDismiss">不采纳</button></div>
      <div class="tiny muted" v-if="!dismissing && n && !assignee">没有合适的默认责任人，请先选择；当场已纠正不需要责任人。</div>
      <div class="mt" v-if="dismissing">
        <div class="grid g2">
          <label class="tiny muted">不采纳原因<select class="in" v-model="reason"><option value="">请选择</option><option v-for="r in reasons" :key="r.code" :value="r.code">{{ r.label }}</option></select></label>
          <label class="tiny muted">补充说明<input class="in" v-model="note" maxlength="200" :placeholder="reason === 'other' ? '选“其他”时必填' : '选填'"></label></div>
        <div class="row wrap mt"><button class="btn sm" :disabled="!!busy || !n || !reason || (reason === 'other' && !note.trim())" @click="submit('dismiss')">{{ busy === 'dismiss' ? '提交中' : '确认不采纳' + cnt }}</button>
          <button class="btn ghost sm" :disabled="!!busy" @click="dismissing = false">取消</button></div></div></div></div>`,
  data() {
    return { sel: Object.fromEntries(this.items.map((f) => [f.id, true])), ed: {}, hints: {}, people: [], reasons: [], assignee: this.defaultAssigneeId || 0,
      due: this.defaultDue || "", dismissing: false, reason: "", note: "", busy: "" };
  },
  computed: {
    canDecide() { return can("site_safety") || roleOf() === "sub_safety"; },
    chosen() { return this.items.length === 1 ? this.items : this.items.filter((f) => this.sel[f.id]); },   // 只剩一条时不显示勾选框，就是这一条
    n() { return this.chosen.length; },
    cnt() { return this.items.length > 1 ? `（${this.n} 项）` : ""; },
    dflt() { return this.defaultAssigneeId || this.hintDefault || 0; },
    hintDefault() {   // 同一张照片的候选默认责任人一致时才作为默认（F-16 规则由后端 findings.default_assignee 计算）
      const ds = [...new Set(this.items.map((f) => (this.hints[f.id] || {}).default_assignee_id || 0))];
      return ds.length === 1 ? ds[0] : 0;
    },
    dueHint() { return this.due ? "期限按所选日期" : CAND_DUE_HINT; },
  },
  watch: {
    dflt(v) { if (!this.assignee && v) this.assignee = v; },
    items(list) { for (const f of list) if (!(f.id in this.sel)) this.sel[f.id] = true; },   // 处理一部分后列表刷新：新出现的默认勾选，已有的保持
  },
  async mounted() {
    if (!this.canDecide) return;
    try { this.people = await candPeople(); } catch (e) { toast(e.message, true); }
    try { this.hints = (await GET("/api/candidates/hints?ids=" + this.items.map((f) => f.id).join(","))).items || {}; } catch (e) { this.hints = {}; }
  },
  methods: {
    today,
    b(f) { return f.body || f; },
    sim(f) { return (this.hints[f.id] || {}).similar || null; },
    tone(f) { return LEVEL_F[(this.ed[f.id] && this.ed[f.id].level) || f.level] || ""; },
    levelName(f) { const l = this.ed[f.id] && this.ed[f.id].level; return l ? { high: "高", medium: "中", low: "低" }[l] : f.level_name; },
    toggleEdit(f) {
      if (this.ed[f.id]) { const { [f.id]: _, ...rest } = this.ed; this.ed = rest; return; }
      this.ed = { ...this.ed, [f.id]: { level: f.level, category: this.b(f).category || "", desc: this.b(f).desc || "", measure: this.b(f).measure || "" } };
    },
    edits(f) {   // 只带改动过的字段（与 AI 原值相同的不算修改）
      const e = this.ed[f.id], out = {};
      if (!e) return out;
      for (const k of ["level", "category", "desc", "measure"]) { const o = k === "level" ? f.level : (this.b(f)[k] || ""); if ((e[k] || "") !== o) out[k] = e[k]; }
      return out;
    },
    async openDismiss() {
      this.dismissing = true;
      if (!this.reasons.length) { try { this.reasons = await candReasons(); } catch (e) { toast(e.message, true); } }
    },
    submit(action) {
      const items = this.chosen.map((f) => {
        const it = { id: f.id, action };
        if (action === "confirm") Object.assign(it, { assignee_id: this.assignee || null, due_date: this.due || null }, this.edits(f));
        if (action === "onspot" && this.assignee) it.assignee_id = this.assignee;
        if (action === "dismiss") Object.assign(it, { reason_code: this.reason, reason_note: this.note.trim() || null });
        return it;
      });
      const n = items.length;
      const msg = { confirm: this.assignee && this.assignee !== store.me.user.id ? `已确认并派单 ${n} 项，已通知责任人` : `已确认并派单 ${n} 项`,
        onspot: `已记为当场纠正并闭环 ${n} 项`, dismiss: `已不采纳 ${n} 项` }[action];
      return this.send(action, items, msg);
    },
    merge(f) {
      const t = this.sim(f);
      return this.send("dismiss", [{ id: f.id, action: "dismiss", reason_code: "duplicate", merge_into: t.id }], `已并入 #${t.id}`);
    },
    async send(action, items, msg) {
      if (!items.length || this.busy) return;
      this.busy = action;
      try { const r = await POST("/api/candidates/batch", { items }); toast(msg); this.dismissing = false; this.$emit("done", { decided: r.decided }); this.$emit("changed"); }
      catch (e) { toast(e.message, true); } finally { this.busy = ""; }
    },
  },
};
window.CandidateGroup = CandidateGroup;

const CandidateBox = {
  components: { CandidateGroup },
  props: { f: { type: Object, required: true }, thumb: { type: Boolean, default: true } },
  emits: ["changed"],
  template: `<div class="cand-box">
    <candidate-group v-if="here" :group-key="'finding:' + f.id" :items="[f]" :default-assignee-id="null" :default-due="null" @changed="$emit('changed')"></candidate-group>
    <template v-else>
      <div class="row wrap" style="align-items:flex-start;flex-wrap:nowrap" v-if="thumb && f.ref_type === 'media'">
        <ev-photo size="s" :src="'/api/media/' + f.ref_id + '/thumb'" :full="'/api/media/' + f.ref_id + '/file'" alt="识别所依据的照片"></ev-photo>
        <div class="grow"><div class="tiny muted">AI 依据：<template v-if="b.category">{{ b.category }} · </template>{{ b.desc || f.title }}<template v-if="b.scene">（场景：{{ b.scene }}）</template></div>
          <div class="tiny" v-if="b.measure">AI 建议措施：{{ b.measure }}</div></div></div>
      <template v-else><div class="tiny muted">AI 依据：<template v-if="b.category">{{ b.category }} · </template>{{ b.desc || f.title }}<template v-if="b.scene">（场景：{{ b.scene }}）</template></div>
        <div class="tiny" v-if="b.measure">AI 建议措施：{{ b.measure }}</div></template>
      <div class="tiny muted">确认前不计入台账、不通知整改人。</div>
      <div class="row wrap mt" v-if="canDecide"><button class="btn primary sm" @click="go(homeOf())">去处理</button><span class="tiny muted">在首页待办里按照片一次处理</span></div>
      <div class="tiny muted mt" v-else>待本单位安全员或安全主管确认。</div></template></div>`,
  data: () => ({ store }),
  computed: {
    b() { return this.f.body || this.f; },
    here() { return store.route.name === "finding"; },   // 隐患详情页是这条候选的单条深链：就地处理；其余页面只读
    canDecide() { return can("site_safety") || roleOf() === "sub_safety"; },
  },
  methods: { go, homeOf },
};

/* 来历：一条 AI 产出怎么来的——依据、哪次 AI 运行、谁在何时处理。全部取自已有记录，缺的环节写“无记录”。 */
const AiTrace = {
  props: ["t", "err"],
  template: `<div class="ai-trace tiny" role="region" aria-label="AI 产出来历">
    <div class="muted" v-if="err">{{ err }}</div><div class="muted" v-else-if="!t">加载中…</div>
    <template v-else>
      <div class="row wrap"><span class="chip">{{ t.detector }}</span><span class="chip" :class="{blue: t.run && !t.run.mock}" v-if="t.run">{{ t.run.mock ? '离线模拟（非真实模型）' : t.run.model }}</span><span class="muted" v-else>无 AI 运行记录</span></div>
      <div class="muted" v-if="t.run">{{ t.run.agent || t.run.kind }} · 提示词 {{ t.run.prompt_version }} · {{ t.run.latency_ms != null ? t.run.latency_ms + ' ms · ' : '' }}{{ fmtDT(t.run.at) }}<template v-if="!t.run.ok"> · 本次调用失败</template></div>
      <div class="mt"><b>依据</b></div>
      <ul class="tr-basis"><li v-for="(b, n) in t.basis" :key="n">{{ b }}</li></ul>
      <div class="mt"><b>处理过程</b><span class="muted">（当前：{{ t.status_name }}）</span></div>
      <ol class="tr-steps"><li v-for="(s, n) in t.steps" :key="n"><span class="muted">{{ s.at ? fmtDT(s.at) : '—' }}</span> {{ s.who }}<template v-if="s.role_name && s.role_name !== s.who">（{{ s.role_name }}）</template>：{{ s.what }}<template v-if="s.note"> —— {{ s.note }}</template></li></ol>
      <div class="muted" v-if="!t.human">尚无人工确认或驳回记录。</div>
    </template></div>`,
  methods: { fmtDT },
};

// 统一 AI 输入框：全站常驻在港湾侧岗位页面顶部，只有这一个输入入口。
//   提问 → 问工笔（进入问工笔页自动发送，回答带出处）；指令 → x_cmd.js 的动作卡（须点“确认执行”才生效）；
//   纪要 → x_cmd.js 的纪要整理（行动项逐条确认）。意图由 /api/ai/intent 判断（规则优先，拿不准再问轻量模型），
//   仍不确定时列出可点选的意图让人选，绝不猜着执行。右侧小切换可手动指定（自动/问/指令/纪要）。
// 聚焦且为空时下拉示例问题：按岗位和项目里真实存在的数据出题（/api/ask/suggest），没有数据就不出。
// 指令/纪要的面板由 x_cmd.js 通过 EXT.slot("askbar") 渲染，经 AI_INPUT.req 接收请求。
const AI_INPUT = reactive({ req: null, seq: 0, commander: () => false });

const INTENT_HINT = { ask: "查数据、问原因，回答附出处", cmd: "生成派单/催办动作卡，确认后才执行", minutes: "整理成纪要和行动项，逐条确认后记入" };

const AskBar = {
  components: { VoiceButton },
  template: `<div class="askbar"><div class="ask-wrap"><div class="ask-pill"><span class="ask-ic"><ic n="sparkles"></ic></span>
    <textarea ref="in" class="ask-in" rows="1" v-model="q" maxlength="12000" @input="grow" @keydown.enter="onEnter" @focus="show = true" @blur="hide" @keyup.esc="esc"
      :placeholder="ph" aria-label="问工笔、下指令或记纪要"></textarea>
    <select class="ask-mode" v-if="cmdr" v-model="mode" title="输入类型：默认由 AI 判断，也可手动指定" aria-label="输入类型">
      <option value="auto">自动</option><option value="ask">问</option><option value="cmd">指令</option><option value="minutes">纪要</option></select>
    <voice-button @text="onVoice"></voice-button>
    <button class="ask-go" :disabled="!q.trim() || busy" @click="send()" aria-label="发送"><ic n="arrow-up"></ic></button></div>
    <div class="ask-sug" v-if="show && !q.trim() && items.length" role="listbox" aria-label="示例问题">
      <div class="tiny muted ask-sug-h">试试问</div>
      <button class="ask-sug-i" v-for="s in items" :key="s.q" role="option" @mousedown.prevent="pick(s.q)"><span>{{ s.q }}</span><small class="muted">{{ s.why }}</small></button></div>
    <div class="ask-sug ask-choice" v-if="choice" role="listbox" aria-label="请选择意图">
      <div class="tiny muted ask-sug-h">{{ choice.reason }}，这句话想做什么？</div>
      <button class="ask-sug-i" v-for="o in choice.options" :key="o.intent" role="option" @mousedown.prevent="send(o.intent)"><span>{{ o.label }}</span><small class="muted">{{ hint(o.intent) }}</small></button>
      <button class="ask-sug-i" @mousedown.prevent="choice = null"><span class="muted">取消，继续编辑</span></button></div></div></div>`,
  data: () => ({ q: "", show: false, items: [], loaded: false, mode: "auto", busy: false, choice: null }),
  computed: {
    cmdr() { return !!AI_INPUT.commander(); },
    // 10-10 RJ（09_V8）：管理岗唯一提示“提问、下指令”，只读岗位只写“提问”；纪要识别能力不变（自动判断，或右侧切到“纪要”）
    ph() {
      if (!this.cmdr) return "提问";
      return { auto: "提问、下指令", ask: "提问", cmd: "下指令：如 3#墩钢筋整改派给乙分包，明日下班前", minutes: "粘贴或口述例会内容，整理为纪要和行动项" }[this.mode];
    },
  },
  watch: { q() { this.choice = null; } },
  async mounted() {
    try { this.items = (await GET("/api/ask/suggest")).items || []; } catch (e) { this.items = []; }
    this.loaded = true;
  },
  methods: {
    hint: (i) => INTENT_HINT[i] || "",
    hide() { setTimeout(() => { this.show = false; }, 120); },
    esc() { this.show = false; this.choice = null; },
    grow() { const el = this.$refs.in; if (!el) return; el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 150) + "px"; },
    pick(q) { this.q = q; this.show = false; this.send("ask"); },
    onEnter(e) { if (e.shiftKey || e.isComposing) return; e.preventDefault(); this.send(); },
    onVoice(t) { this.q = (this.q ? this.q + "\n" : "") + t; this.$nextTick(() => { this.grow(); this.send(); }); },
    async send(force) {
      const t = this.q.trim(); if (!t || this.busy) return;
      this.choice = null; this.show = false;
      let intent = force || (this.cmdr && this.mode !== "auto" ? this.mode : "");
      if (!intent) {
        if (!this.cmdr) intent = "ask";
        else {
          this.busy = true;
          try { const r = await POST("/api/ai/intent", { text: t }); if (r.confident) intent = r.intent; else { this.choice = r; return; } }
          catch (e) { toast(e.message, true); return; } finally { this.busy = false; }
        }
      }
      this.dispatch(this.cmdr ? intent : "ask", t);
    },
    dispatch(intent, t) {
      this.q = ""; this.mode = "auto"; this.$nextTick(this.grow);
      if (intent === "ask") go("agents", { tab: "ask", q: t });
      else AI_INPUT.req = { kind: intent, text: t, key: ++AI_INPUT.seq };
    },
  },
};
