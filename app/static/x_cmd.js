/* x_cmd：指挥Agent。统一输入框（aifeed.js 的 AskBar）判出“指令/纪要”后的处理面板（一句话派单/催办动作卡、纪要整理）+ 例会行动项页（meetings）。
   动作卡：Agent 解析 → 带依据的卡片就地展示 → 人改字段/点选候选 → 点“确认执行”才真正落库。 */
(function () {
  const COMMANDERS = ["manager", "eng_sup", "safety_sup"];
  const isCommander = () => !!store.me && featOn("cmd") && COMMANDERS.includes(roleOf()) && can("dispatch");   // 项目设置里隐藏“指挥”后，输入框只剩提问
  const bus = reactive({ tick: 0 });          // 行动项变化时通知“例会行动项”页刷新
  let assigneeCache = null;
  async function loadAssignees() { if (!assigneeCache) assigneeCache = await GET("/api/assignees"); return assigneeCache; }
  const personLabel = (a) => `${a.name} · ${a.company}${a.role_name ? " · " + a.role_name : ""}`;

  // ---------------------------------------------------------------------- 一句话指令的动作卡
  const CmdCard = {
    props: ["card", "source"],
    emits: ["done", "cancel"],
    template: `<div class="cmd-card">
      <div class="row between wrap"><b><ic n="target"></ic>{{ card.title }}</b><span :class="['chip', res ? 'green' : 'blue']">{{ res ? '已执行' : '动作卡 · 待确认' }}</span></div>
      <ul class="cmd-evid"><li v-for="(e, i) in card.evidence" :key="i">{{ e }}</li></ul>
      <div v-if="card.type === 'unsupported'" class="banner info">{{ card.deny_reason }}</div>
      <template v-else>
        <div class="cmd-f"><label>对象</label><div class="grow">
          <div v-if="sel" class="cmd-pick on"><span class="chip">#{{ sel.id }}</span><span class="grow">{{ sel.title }}</span>
            <span class="tiny muted">{{ sel.status_name }} · 责任人 {{ sel.assignee || '未派' }} · 期限 {{ sel.due_date || '未设' }}</span>
            <a v-if="!res" class="small" @click="sel = null">换一个</a></div>
          <template v-else>
            <div class="cmd-pick" v-for="c in card.finding_candidates.concat(found)" :key="c.id" @click="sel = c">
              <span class="chip">#{{ c.id }}</span><span class="grow">{{ c.title }}</span>
              <span class="tiny muted">{{ c.status_name }} · {{ c.assignee || '未派' }} · 期限 {{ c.due_date || '未设' }}</span></div>
            <div class="row mt"><input class="in" v-model.trim="q" placeholder="搜索事项：关键词或编号" @keyup.enter="search"><button class="btn sm" @click="search">搜索</button></div>
          </template></div></div>
        <div class="cmd-f" v-if="card.type === 'assign'"><label>责任人</label><div class="grow">
          <div class="row wrap" v-if="card.assignee_candidates.length && !res"><button v-for="p in card.assignee_candidates" :key="p.id" :class="['btn', 'sm', assigneeId === p.id ? 'primary' : '']" @click="assigneeId = p.id">{{ p.name }} · {{ p.company }}</button></div>
          <select class="in" :class="{mt: card.assignee_candidates.length}" v-model="assigneeId" :disabled="!!res"><option :value="null">请选择责任人</option>
            <option v-for="p in people" :key="p.id" :value="p.id">{{ personLabel(p) }}</option></select></div></div>
        <div class="cmd-f" v-if="card.type === 'assign'"><label>期限</label><div class="grow"><input class="in" type="date" v-model="due" :min="today" :disabled="!!res" style="max-width:180px"><span class="tiny muted"> 留空则沿用原期限</span></div></div>
        <div class="cmd-f" v-else><label>催办语</label><div class="grow"><input class="in" v-model="note" maxlength="200" placeholder="请尽快处理" :disabled="!!res"></div></div>
        <div class="cmd-q" v-if="!res && hints.length"><div v-for="(h, i) in hints" :key="i"><ic n="help"></ic>{{ h }}</div></div>
        <div class="banner warn" v-if="!res && block">{{ block }}</div>
        <div class="banner info" v-if="res"><ic n="check-circle"></ic>{{ res.message }}</div>
        <div class="row mt" v-if="!res"><button class="btn primary" :disabled="!ready || busy" @click="run">{{ busy ? '执行中' : '确认执行' }}</button>
          <button class="btn" @click="$emit('cancel')">取消</button><help-tip text="确认后派单或催办，并通知责任人；记录留存于审计"></help-tip></div>
        <div class="row mt" v-else><button class="btn" @click="$emit('done')">继续下指令</button></div>
      </template>
      <div class="row mt" v-if="card.type === 'unsupported'"><button class="btn" @click="$emit('cancel')">关闭</button></div>
    </div>`,
    data() {
      const c = this.card;
      return { sel: c.finding, found: [], q: "", assigneeId: c.assignee_id, due: c.due_date || "", note: c.note || "", people: [], busy: false, res: null, today: today() };
    },
    computed: {
      block() {
        if (!this.card.allowed) return this.card.deny_reason;
        if (this.card.type === "nudge" && this.sel && !this.sel.assignee_id) return "该事项尚无责任人，请先派单";
        if (this.due && this.due < this.today) return "期限早于今日，请修改";
        return "";
      },
      hints() {
        const h = [];
        if (!this.sel) h.push(this.card.finding_candidates.length ? "存在多个候选对象，请选择" : "未找到对象，请搜索选择");
        if (this.card.type === "assign" && !this.assigneeId) h.push("请确认责任人");
        return h;
      },
      ready() { return !!(this.card.allowed && this.sel && !this.block && (this.card.type === "nudge" || this.assigneeId)); },
    },
    async mounted() { try { this.people = await loadAssignees(); } catch (_) { this.people = []; } },
    methods: {
      personLabel,
      async search() {
        if (!this.q) return;
        try { this.found = (await GET("/api/cmd/findings?q=" + encodeURIComponent(this.q))).filter((c) => !this.card.finding_candidates.some((x) => x.id === c.id)); if (!this.found.length) toast("暂无匹配的进行中事项"); }
        catch (e) { toast(e.message, true); }
      },
      async run() {
        this.busy = true;
        try {
          this.res = await POST("/api/cmd/execute", { type: this.card.type, finding_id: this.sel.id, assignee_id: this.card.type === "assign" ? this.assigneeId : null,
            due_date: this.card.type === "assign" ? (this.due || null) : null, note: this.card.type === "nudge" ? this.note : "", source: this.source || "" });
          if (this.res.finding && this.sel) this.sel = { ...this.sel, assignee: this.res.finding.assignee, due_date: this.res.finding.due_date };
          toast(this.res.message); bus.tick++;
        } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 行动项动作卡
  const CmdItem = {
    props: ["card", "mid"],
    emits: ["confirmed"],
    template: `<div class="cmd-card cmd-item">
      <div class="row between wrap"><span class="chip">行动项 {{ card.idx + 1 }}</span><span :class="['chip', saved ? 'green' : 'blue']">{{ saved ? '已记入' : '待确认' }}</span></div>
      <textarea class="in mt" rows="2" v-model="text" :disabled="!!saved"></textarea>
      <ul class="cmd-evid"><li v-for="(e, i) in card.evidence" :key="i">{{ e }}</li></ul>
      <div class="cmd-f"><label>责任人</label><div class="grow">
        <div class="row wrap" v-if="card.owner_candidates.length && !saved"><button v-for="p in card.owner_candidates" :key="p.id" :class="['btn', 'sm', ownerId === p.id ? 'primary' : '']" @click="ownerId = p.id">{{ p.name }} · {{ p.company }}</button></div>
        <select class="in" v-model="ownerId" :disabled="!!saved"><option :value="null">（不指定系统内人员）</option><option v-for="p in people" :key="p.id" :value="p.id">{{ personLabel(p) }}</option></select>
        <input class="in mt" v-if="!ownerId && !saved" v-model.trim="ownerName" maxlength="60" placeholder="外部责任人，如“业主代表”"></div></div>
      <div class="cmd-f"><label>期限</label><div class="grow"><input class="in" type="date" v-model="due" :disabled="!!saved" style="max-width:180px"></div></div>
      <div class="cmd-f" v-if="card.finding_candidates.length"><label>关联整改单</label><div class="grow">
        <select class="in" v-model="findingId" :disabled="!!saved"><option :value="null">不关联</option>
          <option v-for="c in card.finding_candidates" :key="c.id" :value="c.id">#{{ c.id }} {{ c.title.slice(0, 36) }}（{{ c.status_name }}）</option></select></div></div>
      <div class="cmd-q" v-if="!saved && card.questions.length"><div v-for="(h, i) in card.questions" :key="i"><ic n="help"></ic>{{ h }}</div></div>
      <div class="row mt" v-if="!saved"><button class="btn primary sm" :disabled="busy || !text.trim()" @click="confirm">{{ busy ? '记录中' : '确认记入' }}</button>
        <help-tip text="确认后通知责任人；关联整改单不改变其状态"></help-tip></div>
      <div class="banner info mt" v-else><ic n="check-circle"></ic>已记入行动项<span v-if="saved.owner_id">，已通知 {{ saved.owner }}</span></div>
    </div>`,
    data() {
      const c = this.card;
      return { text: c.text, ownerId: c.owner_id, ownerName: c.owner_name || "", due: c.due_date || "", findingId: c.finding_id, people: [], busy: false, saved: null };
    },
    async mounted() { try { this.people = await loadAssignees(); } catch (_) { this.people = []; } },
    methods: {
      personLabel,
      async confirm() {
        this.busy = true;
        try {
          this.saved = await POST(`/api/meetings/${this.mid}/items`, { idx: this.card.idx, text: this.text, owner_id: this.ownerId, owner_name: this.ownerId ? "" : this.ownerName,
            due_date: this.due || null, finding_id: this.findingId });
          bus.tick++; this.$emit("confirmed", this.saved);
        } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 纪要整理（指令条的纪要模式、例会页共用）
  const CmdMinutes = {
    components: { CmdItem, VoiceButton },
    props: { init: { type: String, default: "" } },   // 统一输入框交来的纪要文字：有则直接整理
    emits: ["changed"],
    template: `<div class="cmd-min">
      <div v-if="!out">
        <div class="row wrap"><input class="in" style="max-width:260px" v-model.trim="title" maxlength="60" placeholder="会议名称（选填）">
          <input class="in" type="date" v-model="date" style="max-width:170px"><div class="grow"></div><voice-button @text="onVoice"></voice-button></div>
        <textarea class="in mt" rows="7" v-model="text" placeholder="粘贴或口述例会内容，可用“议题”“结论”“行动项”标注"></textarea>
        <div class="row mt"><button class="btn primary" :disabled="busy || text.trim().length < 6" @click="parse">{{ busy ? '整理中' : '整理纪要' }}</button>
          <help-tip text="仅整理，行动项须逐条确认后记入"></help-tip></div>
      </div>
      <div v-else>
        <div class="row between wrap"><b><ic n="edit"></ic>{{ out.meeting.title }}</b><span class="tiny muted">{{ out.meeting.date }} · 行动项 {{ out.cards.length }} 条，已确认 {{ n }} 条</span></div>
        <div class="cmd-topics mt" v-if="out.meeting.topics.length"><div v-for="(t, i) in out.meeting.topics" :key="i" class="cmd-topic">
          <b>{{ i + 1 }}. {{ t.topic }}</b><div class="small" v-if="t.conclusion">结论：{{ t.conclusion }}</div><div class="small muted" v-else>未提结论</div></div></div>
        <div class="empty" v-if="!out.cards.length">暂无行动项，可直接保存纪要</div>
        <cmd-item v-for="c in out.cards" :key="c.idx" :card="c" :mid="out.meeting.id" @confirmed="n++"></cmd-item>
        <div class="row mt"><button class="btn primary" :disabled="busy" @click="finish">{{ n || saved ? '完成' : '只保存纪要' }}</button>
          <button class="btn" @click="reset" v-if="!n">重新整理</button></div>
      </div>
    </div>`,
    data() { return { text: this.init, title: "", date: today(), out: null, busy: false, n: 0, saved: false }; },
    mounted() { if (this.init.trim().length >= 6) this.parse(); },
    methods: {
      onVoice(t) { this.text = (this.text ? this.text + "\n" : "") + t; },
      async parse() {
        this.busy = true;
        try { this.out = await POST("/api/cmd/minutes", { text: this.text, title: this.title, date: this.date }); this.n = 0; this.saved = false; }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async finish() {
        try { await POST(`/api/meetings/${this.out.meeting.id}/save`); this.saved = true; bus.tick++; toast("纪要已保存"); this.reset(true); this.$emit("changed"); }
        catch (e) { toast(e.message, true); }
      },
      reset(clear) { this.out = null; this.n = 0; if (clear === true) { this.text = ""; this.title = ""; } },
    },
  };

  // ---------------------------------------------------------------------- 统一输入框的指令/纪要面板
  // 输入框本身是 aifeed.js 的 AskBar（唯一入口）；它判断出“指令”或“纪要”后经 AI_INPUT.req 把文字交到这里：
  // 指令 → 解析成动作卡（点“确认执行”才生效）；纪要 → 整理成纪要与行动项（逐条确认后记入）。
  AI_INPUT.commander = isCommander;
  const CmdHost = {
    components: { CmdCard, CmdMinutes },
    template: `<div class="cmd" v-if="req && ok">
      <div class="cmd-panel">
        <div class="row between"><span class="tiny muted">{{ req.kind === 'minutes' ? '纪要转行动项' : '指令 · 动作卡' }}</span><a class="small" @click="close">收起</a></div>
        <div class="small cmd-src" v-if="req.kind === 'cmd'">“{{ req.text }}”</div>
        <div class="small muted mt" v-if="busy">解析中…</div>
        <cmd-minutes v-if="req.kind === 'minutes'" :key="req.key" :init="req.text" @changed="close"></cmd-minutes>
        <cmd-card v-else-if="card" :key="req.key" :card="card" :source="source" @done="close" @cancel="close"></cmd-card>
      </div></div>`,
    data: () => ({ card: null, source: "", busy: false }),
    computed: { ok() { return isCommander(); }, req() { return AI_INPUT.req; } },
    watch: { req: { immediate: true, handler(r) { this.card = null; if (r && r.kind === "cmd") this.parse(r); } } },
    methods: {
      close() { AI_INPUT.req = null; this.card = null; },
      async parse(r) {
        this.busy = true;
        try {
          const x = await POST("/api/cmd/parse", { text: r.text });
          if (AI_INPUT.req === r) { this.card = x.card; this.source = x.text; }
        } catch (e) { toast(e.message, true); if (AI_INPUT.req === r) this.close(); } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 例会行动项页（上次例会闭环核对；纪要录入只走顶部统一输入框）
  const MeetingsPage = {
    props: { embed: Boolean },   // embed：例会分区（x_docs.js MeetHub）里的“行动项核对”页签，不再出页头
    template: `<div class="rd-fx">
      <page-head page="meetings" v-if="!embed"></page-head>
      <load-state class="mb" compact v-if="err || !rv" :loading="!rv && !err" :error="err" @retry="load()"></load-state>
      <section class="zb-card rd-mt" v-if="rv">
        <div class="zb-ch"><ic n="users"></ic><h2>上次例会闭环核对</h2><span class="zb-src" v-if="rv.meeting">{{ rv.is_last ? '上次例会' : '历史例会' }}：{{ rv.meeting.title }} · {{ rv.meeting.date }}</span><span class="grow"></span>
          <select class="in rd-mt-sel" v-if="rv.meetings.length > 1" :value="rv.meeting && rv.meeting.id" @change="load($event.target.value)" aria-label="选择例会">
            <option v-for="m in rv.meetings" :key="m.id" :value="m.id">{{ m.date }} {{ m.title }}（{{ m.items }}项）</option></select></div>
        <div class="zb-empty" v-if="!rv.meeting">还没有已保存的例会纪要。</div>
        <template v-else>
          <div class="zb-kpis rd-kpis">
            <div class="zb-kpi"><span class="zb-kpi-ic"><ic n="clipboard"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">行动项</span><span class="zb-kpi-v">{{ rv.stats.total }}</span><span class="zb-kpi-s">已确认记入</span></div></div>
            <div :class="['zb-kpi', rv.stats.done ? 'green' : '']"><span class="zb-kpi-ic"><ic n="check-circle"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">已闭环</span><span class="zb-kpi-v">{{ rv.stats.done }}</span><span class="zb-kpi-s">{{ rv.stats.total ? '共 ' + rv.stats.total + ' 项' : '—' }}</span></div></div>
            <div :class="['zb-kpi', rv.stats.overdue ? 'red' : '']"><span class="zb-kpi-ic"><ic n="clock"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">超期</span><span class="zb-kpi-v">{{ rv.stats.overdue }}</span><span class="zb-kpi-s">已过期限未完成</span></div></div>
            <div :class="['zb-kpi', openN ? 'orange' : '']"><span class="zb-kpi-ic"><ic n="hourglass"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">未完成</span><span class="zb-kpi-v">{{ openN }}</span><span class="zb-kpi-s">含超期</span></div></div></div>
          <div class="muted small mt" v-if="rv.message">{{ rv.message }}</div>
          <template v-if="rv.open_items.length"><h3 class="rd-mt-h">未完成清单 <span class="zb-n">{{ rv.open_items.length }}</span></h3>
            <zb-paged :items="rv.open_items" :size="6" :m-size="4" unit="项" cls="cmd-rows"><template #default="{ item: i }"><div class="cmd-row">
              <div class="grow"><div>{{ i.text }}</div>
                <div class="tiny muted">责任人 {{ i.owner || '未指定' }}<span v-if="i.owner_company"> · {{ i.owner_company }}</span> · 期限 {{ i.due_date || '未设' }}
                  <a v-if="i.finding" @click="go('finding', {id: i.finding.id})"> · 整改单#{{ i.finding.id }}（{{ i.finding.status_name }}）</a></div></div>
              <span class="chip red" v-if="i.status === 'overdue'">超期 {{ i.days_over }} 天</span><span class="chip" v-else>进行中</span>
              <button class="btn sm" @click="done(i, true)">标记完成</button></div></template></zb-paged></template>
          <div class="zb-empty" v-else-if="rv.stats.total">行动项已全部闭环。</div>
          <details class="mt" v-if="rv.done_items.length"><summary class="small">已闭环 {{ rv.done_items.length }} 项</summary>
            <zb-paged :items="rv.done_items" :size="6" :m-size="4" unit="项" cls="cmd-rows" :fit="false"><template #default="{ item: i }"><div class="cmd-row"><div class="grow"><div>{{ i.text }}</div>
              <div class="tiny muted">责任人 {{ i.owner || '未指定' }} · 完成于 {{ fmtDT(i.done_at) }}</div></div>
              <span class="chip green">已完成</span><button class="btn ghost sm" @click="done(i, false)">撤销</button></div></template></zb-paged></details>
        </template>
      </section>
      <div class="tiny muted mt"><help-tip caption="怎样记录本次例会" text="记录本次例会：在页面顶部输入框粘贴或口述纪要，AI 识别为纪要后整理成行动项，逐条确认记入（这里只做上次例会的闭环核对）。"></help-tip></div>
    </div>`,
    data: () => ({ rv: null, err: null }),
    watch: { "bus.tick"() { this.load(this.rv && this.rv.meeting ? this.rv.meeting.id : undefined); } },
    computed: { bus: () => bus, openN() { return this.rv && this.rv.stats ? Math.max(0, this.rv.stats.total - this.rv.stats.done) : 0; } },
    mounted() { this.load(); },
    methods: {
      go, fmtDT,
      async load(id) {
        this.err = null;
        try { this.rv = await GET("/api/meetings/review" + (id ? "?meeting_id=" + id : "")); }
        catch (e) { if (this.rv) toast(e.message, true); else this.err = e; }
      },
      async done(i, v) {
        try { await POST(`/api/meetings/items/${i.id}/done`, { done: v }); toast(v ? "已标记完成" : "已撤销完成"); await this.load(this.rv.meeting.id); }
        catch (e) { toast(e.message, true); }
      },
    },
  };

  // RJ 10-10 工笔文书分四区：#/meetings 是“例会”分区（例会底稿、三周滚动计划、行动项核对），行动项核对就是本页
  window.MeetingsPage = MeetingsPage;
  EXT.route("meetings", typeof MeetHub !== "undefined" ? MeetHub : MeetingsPage);
  EXT.slot("askbar", CmdHost);
})();
