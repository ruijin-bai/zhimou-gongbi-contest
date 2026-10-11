// ---------------------------------------------------------------------------- 日志编辑/查看
const LogEditor = {
  components: { SrcChips, ReviewTable },
  props: ["id"],
  template: `<div v-if="log" class="rd-fx">
  <page-head :title="(log.mode === 'manual' ? '手工对照日志' : log.period === 'week' ? '周进度确认' : '施工日志') + ' · ' + log.log_date + (log.period === 'week' ? ' ～ ' + log.period_end : '')" :page="reviewable ? 'log_review' : 'log_edit'">
    <template #badge><span class="chip blue" v-if="log.auto">随手记自动整理</span>
      <span v-if="log.status" :class="['chip', STATUS_CHIP[log.status]]">{{ STATUS[log.status] }}</span>
      <span class="chip" v-if="!editable">{{ log.author.name }}</span></template>
    <template #meta><a @click="back">← 返回</a></template>
    <template #actions><div class="row small muted" v-if="editable"><span>用时 {{ mmss(active) }}</span><span v-if="saving">保存中…</span><span v-else-if="dirty">未保存</span><span v-else>已保存</span></div></template></page-head>
  <div class="banner warn" v-if="log.mode === 'manual'">手工对照模式：不使用 AI，按同一模板填写，仅计时，用于与 AI 日志对比耗时。</div>
  <div class="banner warn" v-if="log.status === 'returned'">审核退回：{{ log.review_note }}</div>
  <div class="banner info" v-if="!editable && log.status === 'draft'">作者还未提交，以下是草稿内容。</div>
  <log-progress-trust :log="log"></log-progress-trust>
  <div class="banner warn" v-if="store.me.ai.provider === 'mock' && log.mode === 'ai'">当前为离线模拟模型，仅供测试，草稿不代表真实识别能力。</div>

  <div class="rd-body" v-fit="260"><!-- 一屏化（验收 I15）：电脑端日志正文在面板内滚动，提交/审核栏留在面板外 -->
  <div class="grid g2 stack">
    <div class="zb-card rd-card" v-if="log.period !== 'week'">
      <div class="zb-ch"><ic n="camera"></ic><h2>现场照片</h2><span class="zb-n">{{ log.photos.length }}</span><span class="grow"></span>
        <span class="tiny muted" v-if="log.mode === 'ai'">AI 读取前 8 张</span></div>
      <div class="ev-grid rd-ph">
        <div class="rd-phw" v-for="(p, i) in log.photos" :key="p.id"><ev-photo :src="p.thumb" :full="p.url" :label="'P' + (i + 1)" size="f" :time="phTime(p)">{{ caption(i) ? (phTime(p) ? ' · ' : '') + caption(i) : '' }}</ev-photo>
          <button class="rd-phx" v-if="editable" @click="removePhoto(p)" title="移除这张照片" aria-label="移除这张照片"><ic n="x"></ic></button></div>
        <button type="button" class="rd-phadd" v-if="editable" :disabled="uploading" @click="upload()"><ic n="camera"></ic><span>{{ uploading ? '上传中…' : '拍照' }}</span></button>
      </div>
      <div class="zb-empty" v-if="!log.photos.length && !editable">这份日志没有附照片。</div>
      <div class="field mt" v-if="editable && log.mode === 'ai'"><label class="f">口述记录</label>
        <textarea class="in" rows="4" v-model="voice" @input="dirty = true; scheduleSave()" placeholder="示例：3号墩承台钢筋绑扎完成约60%，钢筋工12人，汽车吊1台；钢筋缺8吨；明日继续绑扎并支模。"></textarea></div>
      <div class="small mt pre" v-else-if="log.voice_text"><b>口述：</b>{{ log.voice_text }}</div>
      <div class="row mt">
        <input class="in" style="max-width:220px" v-if="editable" v-model="weather" @input="dirty = true; scheduleSave()" placeholder="天气，如晴 32℃">
        <span class="small" v-else-if="log.weather">天气：{{ log.weather }}</span>
        <div class="grow"></div>
        <button class="btn ai big" v-if="editable && log.mode === 'ai'" :disabled="drafting" @click="draft">{{ drafting ? '生成中…' : ((log.has_ai_draft || log.imported) ? '重新生成' : 'AI 生成日志') }}</button>
      </div>
    </div>
    <div class="col">
      <div class="card thinking" v-if="drafting"><div class="spinner"></div><div><b>正在分析 {{ log.photos.length }} 张照片、口述与 P6 计划</b>
        <div class="small muted">已用 {{ draftSec }} 秒，通常 15–60 秒</div></div></div>
      <div :class="['checks', flags.length ? '' : 'ok']" v-if="log.checks && ((log.has_ai_draft || log.imported) || log.status !== 'draft')">
        <div class="row between"><b>{{ flags.length ? '规则提示 ' + flags.length + ' 处' : '规则校验通过' }}</b>
          <span class="tiny muted" v-if="log.ai_run">{{ log.ai_run.provider === 'mock' ? '模拟' : log.ai_run.model }} · {{ (log.ai_run.latency_ms / 1000).toFixed(1) }}s</span></div>
        <div class="check" v-for="(c, i) in log.checks" :key="i"><span class="lv"><ic :n="c.level === 'error' ? 'ban' : c.level === 'warn' ? 'alert' : 'help'"></ic></span>
          <span><span class="muted small" v-if="secTitle(c.section)">[{{ secTitle(c.section) }}{{ c.index !== null && typeof c.index === 'number' ? ' 第' + (c.index + 1) + '条' : '' }}]</span> {{ c.message }}</span></div>
      </div>
      <div class="zb-card rd-card" v-if="editable && !(log.has_ai_draft || log.imported) && log.mode === 'ai' && log.status === 'draft' && !drafting">
        <h2>今日日志流程</h2>
        <div class="small mt">1. 上传现场照片<br>2. 口述工程量、人机、问题与次日计划<br>3. 生成日志，核对标黄项后提交</div>
        <div class="tiny muted mt">内容挂接 P6 作业，提交后自动回填进度并参与关键路径预警。</div>
      </div>
      <div class="zb-card rd-card" v-if="log.activities.length && editable">
        <div class="zb-ch"><ic n="chart"></ic><h2>今日相关 P6 作业</h2><span class="zb-n">{{ log.activities.length }}</span></div>
        <div class="small" style="max-height:180px;overflow:auto">
          <div v-for="a in log.activities" :key="a.code" class="row"><span :class="['chip', a.is_critical ? 'red' : '']">{{ a.is_critical ? '关键' : '非关键' }}</span>
            <b class="grow"><act-name :c="a.code" :n="a.name" :s="a.short_name"></act-name></b><span class="tiny muted nowrap">{{ a.early_start }}~{{ a.early_finish }}</span></div>
        </div>
      </div>
    </div>
  </div>

  <div class="zb-card rd-card mt" v-if="content && ((log.has_ai_draft || log.imported) || log.mode === 'manual' || log.status !== 'draft')">
    <div class="zb-ch"><ic n="file"></ic><h2>{{ log.period === 'week' ? '周确认内容' : '日志内容' }}</h2></div>
    <div class="field"><label class="f">{{ log.period === 'week' ? '本周概述' : '当日概述' }}</label>
      <input class="in" v-if="editable" v-model="content.summary" @input="touch"><div v-else>{{ content.summary }}</div></div>
    <div class="sec" v-for="sec in SECTIONS" :key="sec.key">
      <div class="sec-h"><h3>{{ log.period === 'week' ? ({tomorrow: '下周计划', progress: '本周完成工程量（回填P6）', labor: '人员投入（日均）', machinery: '机械投入（日均）'}[sec.key] || sec.title) : sec.title }}</h3><span class="muted small">{{ (content[sec.key] || []).length }}</span><div class="grow"></div>
        <button class="btn ghost sm" v-if="editable" @click="addRow(sec)"><ic n="plus"></ic>添加</button></div>
      <div class="empty small" style="padding:8px" v-if="!(content[sec.key] || []).length">无</div>
      <div v-for="(row, i) in content[sec.key]" :key="i" :class="['item', rowFlag(sec.key, i), editable ? 'edit' : '']">
        <template v-if="editable">
          <template v-for="c in sec.cols" :key="c.k">
            <select v-if="c.type === 'act'" :class="['in', c.grow ? 'wide' : '']" :style="colStyle(c)" v-model="row[c.k]" @change="touch">
              <option value="">（未挂接）</option>
              <option v-for="a in actOptions(row[c.k])" :key="a.code" :value="a.code">{{ a.short_name || a.name }}</option></select>
            <select v-else-if="c.type === 'sel'" class="in" :style="colStyle(c)" v-model="row[c.k]" @change="touch">
              <option v-for="o in c.opts" :key="o">{{ o }}</option></select>
            <input v-else :class="['cell', c.type === 'num' ? 'num' : '', c.grow ? 'wide' : '']" :style="colStyle(c)" :placeholder="c.label"
              :value="row[c.k]" @input="setCell(row, c, $event.target.value)" :inputmode="c.type === 'num' ? 'decimal' : 'text'">
          </template>
          <src-chips :sources="row.sources" :photos="log.photos"></src-chips>
          <button class="btn ghost sm del" @click="delRow(sec, i)" title="删除"><ic n="x"></ic></button>
        </template>
        <template v-else>
          <div class="grow small">
            <template v-for="(c, j) in visCols(sec, row)" :key="c.k"><span :class="c.grow ? '' : 'muted'">{{ c.type === 'act' ? actLabel(row[c.k]) : row[c.k] }}{{ c.k === 'pct' ? '%' : '' }}</span><span class="muted" v-if="j < visCols(sec, row).length - 1"> · </span></template>
          </div><src-chips :sources="row.sources" :photos="log.photos"></src-chips>
        </template>
      </div>
    </div>
    <div class="field sec"><label class="f">其他记事</label>
      <textarea class="in" v-if="editable" v-model="content.notes" @input="touch" rows="2"></textarea><div v-else class="pre small">{{ content.notes || '无' }}</div></div>
  </div>

  <div class="zb-card rd-card mt" v-if="log.period === 'week' && log.days"><div class="zb-ch"><ic n="calendar"></ic><h2>本周每日日志</h2></div><div class="row wrap">
    <a v-for="d in log.days" :key="d.date" :class="['chip', d.log_id ? STATUS_CHIP[d.status] || 'blue' : '']" @click="d.log_id && go('log', {id: d.log_id})">{{ d.date.slice(5) }} {{ d.log_id ? (STATUS[d.status] || '') : '无' }}{{ d.notes ? ' · ' + d.notes + '条' : '' }}</a></div></div>
  <div class="zb-card rd-card mt" v-if="log.notes && log.notes.length"><div class="zb-ch"><ic n="mic"></ic><h2>当日随手记原文</h2><span class="zb-n">{{ log.notes.length }}</span></div><div class="small rd-note" v-for="n in log.notes" :key="n.id"><b class="tiny muted">{{ n.time }}</b> {{ n.text }}</div></div>
  <div class="grid g2 stack mt" v-if="!editable">
    <div class="ai-box" v-if="log.dq"><div class="hd"><ic n="file"></ic>数据审核预审 · {{ log.dq.score }} 分</div>
      <span :class="['chip', {low: 'green', medium: 'orange', high: 'red'}[log.dq.risk]]">{{ {low: '低风险，可直接通过', medium: '有待核实项', high: '高风险'}[log.dq.risk] }}</span>
      <div class="small mt">{{ log.dq.summary }}</div><div class="small" v-for="(f, i) in log.dq.focus" :key="i">· 重点核实：{{ f }}</div>
      <template v-if="!dqRows.length"><div class="tiny muted mt" v-for="(c, i) in log.dq.checks" :key="'c' + i">[{{ c.source }}] {{ c.message }}</div></template>
      <div class="tiny muted mt" v-else>{{ dqRows.length }} 条检查结论在下方逐条核对。</div></div>
    <div class="zb-card rd-card" v-if="(log.hazards && log.hazards.length) || (log.qa && log.qa.length)">
      <div v-if="log.hazards.length"><div class="zb-ch"><ic n="helmet"></ic><h2>照片隐患</h2><span class="zb-n">{{ log.hazards.length }}</span></div>
        <div class="small" v-for="h in log.hazards" :key="h.id" style="cursor:pointer" @click="go('finding', {id: h.id})">{{ h.level_name }} · {{ h.title }}（{{ h.status_name }}）</div></div>
      <div v-if="log.qa.length" class="mt"><div class="zb-ch"><ic n="help"></ic><h2>AI 问答</h2></div><div class="small" v-for="(q, i) in log.qa" :key="i">问：{{ q.question }} <b>答：{{ q.answer || '未回答' }}</b></div></div></div>
  </div>
  <div class="zb-card rd-card mt" v-if="!editable && dqRows.length">
    <review-table title="日志预审：逐条核对数据审核Agent 的检查结论" mode="verify" :rows="dqRows" v-model="dqDec" :readonly="!reviewable" :cols="DQ_COLS"
      hint="每条结论判为“属实”或“误报”；判为误报须写明原因。全部决定后才能通过或退回。"></review-table></div>
  <div class="zb-card rd-card rd-meta mt" v-if="!editable">
    <div class="small muted">提交：{{ fmtDT(log.submitted_at) || '—' }}<span v-if="'edit_seconds' in log"> · 编写用时 {{ log.edit_seconds ? mmss(log.edit_seconds) : '—' }}</span>
      <span v-if="log.edit_ratio !== null && log.edit_ratio !== undefined"> · 相对AI草稿修改率 {{ pct(log.edit_ratio) }}</span>
      <span v-if="log.reviewer"> · 审核：{{ log.reviewer }} {{ fmtDT(log.reviewed_at) }}</span></div>
  </div>

  </div>
  <div class="bottom-bar" v-if="reviewable">
    <span class="small muted grow">{{ dqLeft ? '还有 ' + dqLeft + ' 条检查结论未决定' : '检查结论已全部决定，可通过或退回' }}</span>
    <button class="btn" :disabled="dqLeft > 0 || reviewing" @click="review('return')">退回</button>
    <button class="btn primary" :disabled="dqLeft > 0 || reviewing" @click="review('approve')">{{ reviewing ? '处理中…' : '审核通过' }}</button>
  </div>
  <div class="bottom-bar" v-if="editable">
    <span class="small muted grow">{{ flags.length ? '待确认 ' + flags.length + ' 处' : '可提交' }}</span>
    <button class="btn" @click="saveNow">保存</button>
    <button class="btn primary" :disabled="submitting || (!(log.has_ai_draft || log.imported) && log.mode === 'ai')" @click="submit">{{ submitting ? '提交中…' : '确认提交' }}</button>
  </div>
  <ai-note :page="reviewable ? 'log_review' : 'daylog'"></ai-note>
</div>
<load-state v-else loading :error="err" @retry="load"></load-state>`,
  data: () => ({ log: null, err: null, content: null, dqDec: {}, reviewing: false, DQ_COLS: { item: "检查项", ai: "AI 检查结论", ev: "依据", dec: "人工决定" }, voice: "", weather: "", dirty: false, go, saving: false, drafting: false, draftSec: 0,
    uploading: false, submitting: false, active: 0, note: "", timer: null, saveT: null, store, SECTIONS, STATUS, STATUS_CHIP }),
  computed: {
    editable() { return this.log && this.log.editable; },
    // 项目经理拿到的日志详情没有规则校验（checks）、审核状态与编写用时（后端剔除），缺字段时不报错、不显示
    flags() { return ((this.log && this.log.checks) || []).filter((c) => c.level !== "info"); },
    // 日志预审（只给工程主管）：数据审核Agent 的每条检查结论一行；待办里的“去预审”跳到这里，待办表不再另放一份
    dqRows() { return this.log && this.log.dq_review ? this.log.dq_review.rows.map((r) => ({ key: r.key, label: r.label, ai_text: r.ai_value, ai_raw: r.ai_value, evidence: r.evidence, flag: r.flag ? "错误级，需认真核实" : "" })) : []; },
    reviewable() { return !!(this.log && this.log.dq_review && this.log.dq_review.reviewable && this.dqRows.length); },
    dqLeft() { return reviewStatus(this.dqRows, this.dqDec, "verify").remaining; },
  },
  async mounted() {
    await this.load();
    this.timer = setInterval(() => {
      if (this.editable && document.visibilityState === "visible") { this.active++; if (this.active % 5 === 0) lsSet(`zb_active_${this.id}`, this.active); }
    }, 1000);
  },
  unmounted() { clearInterval(this.timer); clearTimeout(this.saveT); if (this.dirty) this.saveNow(); },
  methods: {
    fmtDT, pct,
    back() { if (roleOf() === "eng_sup") backOr("logs", "logs"); else history.back(); },   // 日志详情返回（验收 D1）：工程主管回日志列表，其他岗位回来处
    mmss(s) { s = Math.round(s); return `${Math.floor(s / 60)}分${String(s % 60).padStart(2, "0")}秒`; },
    async load() {
      this.err = null;
      try {
        const l = await GET(`/api/logs/${this.id}`);
        this.apply(l, true);
        this.active = lsGet(`zb_active_${this.id}`, 0);
      } catch (e) { this.err = e; }
    },
    apply(l, withContent) {
      this.log = l;
      this.dqDec = l.dq_review ? reviewFromSaved(l.dq_review.rows) : {};
      if (withContent) { this.content = clone(l.content); this.voice = l.voice_text; this.weather = l.weather; this.dirty = false; }
    },
    caption(i) { const n = (this.content && this.content.photo_notes || []).find((x) => x.photo === `P${i + 1}`); return n ? n.caption : ""; },
    phTime(p) { return String(p.taken_at || p.uploaded_at || "").slice(5, 16).replace("T", " "); },   // 照片时间：拍摄时间优先，没有则写上传时间（MM-DD HH:mm）
    secTitle(k) { const s = SECTIONS.find((x) => x.key === k); return s ? s.title : k === "photos" ? "照片" : ""; },
    rowFlag(sec, i) {
      const c = (this.log.checks || []).filter((x) => x.section === sec && x.index === i);
      return c.some((x) => x.level === "error") ? "err" : c.some((x) => x.level === "warn") ? "flag" : "";
    },
    colStyle(c) { return c.grow ? "flex:1;min-width:120px" : `width:${c.w || 80}px;flex-shrink:0`; },
    actOptions(cur) {
      const list = this.log.activities.slice();
      if (cur && !list.find((a) => a.code === cur)) list.unshift({ code: cur, name: "(不在今日窗口)" });
      return list;
    },
    visCols(sec, row) { return sec.cols.filter((c) => row[c.k] !== null && row[c.k] !== "" && row[c.k] !== undefined); },   // 只读行：只列有值的栏，栏间用“ · ”隔开
    actLabel(code) { if (!code) return ""; const a = this.log.activities.find((x) => x.code === code); return a ? a.short_name || a.name : code; },
    setCell(row, c, v) {
      if (c.type === "num") { const n = v.trim() === "" ? null : Number(v); row[c.k] = Number.isNaN(n) ? row[c.k] : n; }
      else row[c.k] = v;
      this.touch();
    },
    touch() { this.dirty = true; this.scheduleSave(); },
    addRow(sec) { (this.content[sec.key] = this.content[sec.key] || []).push(blankRow(sec)); this.touch(); },
    delRow(sec, i) { this.content[sec.key].splice(i, 1); this.touch(); },
    scheduleSave() { clearTimeout(this.saveT); this.saveT = setTimeout(() => this.saveNow(), 1500); },
    async saveNow() {
      if (!this.editable || !this.dirty || this.saving) return;
      this.saving = true;
      try {
        const l = await PUT(`/api/logs/${this.id}`, { content: this.content, version: this.log.version, weather: this.weather, voice_text: this.voice });
        this.log = { ...l }; this.dirty = false;
      } catch (e) { toast(e.message, true); if (e.status === 409) await this.load(); }
      finally { this.saving = false; }
    },
    async upload() {
      const got = await takePhotosToUpload();
      if (!got) return;
      const { files, scan } = got;
      this.uploading = true;
      try {
        for (let i = 0; i < files.length; i += 4) {
          const fd = new FormData(); fd.append("log_id", this.id);
          addPhotos(fd, files.slice(i, i + 4), scan);
          await POST("/api/media", fd);
        }
        const l = await GET(`/api/logs/${this.id}`); this.log = { ...l, checks: this.log.checks };
      } catch (e) { toast(e.message, true); } finally { this.uploading = false; }
    },
    async removePhoto(p) { const l = await DEL(`/api/logs/${this.id}/photos/${p.id}`); this.log = { ...l, checks: this.log.checks }; },
    async draft() {
      if ((this.log.has_ai_draft || this.log.imported) && !confirm("重新生成将覆盖当前内容，是否继续？")) return;
      await this.saveNow();
      this.drafting = true; this.draftSec = 0;
      const t = setInterval(() => this.draftSec++, 1000);
      try { const l = await POST(`/api/logs/${this.id}/draft`, { voice_text: this.voice, weather: this.weather }); this.apply(l, true); toast("草稿已生成，请核对标黄项"); }
      catch (e) { toast(e.message, true); } finally { clearInterval(t); this.drafting = false; }
    },
    async submit() {
      const errs = this.log.checks.filter((c) => c.level === "error");
      if (errs.length && !confirm(`存在 ${errs.length} 处错误提示，是否继续提交？`)) return;
      this.submitting = true;
      try {
        this.dirty = true; await this.saveNow();
        const l = await POST(`/api/logs/${this.id}/submit`, { version: this.log.version, active_seconds: this.active });
        lsDel(`zb_active_${this.id}`); this.apply(l, true); toast("已提交，进度已回填");
      } catch (e) { toast(e.message, true); } finally { this.submitting = false; }
    },
    async review(action) {
      let note = "";
      if (action === "return") {
        note = prompt("请填写退回原因（将通知技术员）：");
        if (note === null) return;
        if (!note.trim()) { toast("请填写退回原因", true); return; }
      }
      this.reviewing = true;
      try {
        const l = await POST(`/api/logs/${this.id}/review`, { action, note, decisions: reviewPayload(this.dqRows, this.dqDec) });
        this.apply(l, true); toast(action === "approve" ? "已审核通过" : "已退回");
      } catch (e) { toast(e.message, true); } finally { this.reviewing = false; }
    },
  },
};

const TodayLog = {
  components: { LogEditor },
  props: ["mode"],
  template: `<log-editor v-if="id" :id="id" :key="id"></log-editor><load-state v-else loading :error="err" @retry="open"></load-state>`,
  data: () => ({ id: null, err: null }),
  mounted() { this.open(); },
  methods: {
    async open() {
      this.err = null;
      try { const l = await POST("/api/logs", { log_date: store.route.params.date || today(), mode: this.mode || "ai" }); this.id = l.id; }
      catch (e) { this.err = e; }
    },
  },
};

// 技术员日志列表：已过去的空草稿（无摘要、无照片、无问题）不显示；今天/本周还没结束的周确认草稿保留（随手记可能还没整理）
function logIsEmptyDraft(l, todayStr) {
  const end = l.period === "week" ? (l.period_end || l.log_date) : l.log_date;
  return !!l.empty && end < todayStr;   // 是否为空由后端按内容判定（摘要/施工/进度/口述/照片/随手记都没有）
}
// 按周（周一起）分组：本周、上周默认展开，更早的折叠。日期按 UTC 解析，避免时区把日子算偏
function logWeekGroups(items, todayStr) {
  const iso = (t) => t.toISOString().slice(0, 10);
  const monday = (d) => { const t = new Date(d + "T00:00:00Z"); t.setUTCDate(t.getUTCDate() - (t.getUTCDay() + 6) % 7); return iso(t); };
  const plus = (d, n) => { const t = new Date(d + "T00:00:00Z"); t.setUTCDate(t.getUTCDate() + n); return iso(t); };
  const span = (m) => `${m.slice(5)}～${plus(m, 6).slice(5)}`;
  const thisMon = monday(todayStr), lastMon = plus(thisMon, -7);
  const byWeek = {};
  let hidden = 0;
  for (const l of items) {
    if (logIsEmptyDraft(l, todayStr)) { hidden++; continue; }
    const m = monday(l.log_date);
    (byWeek[m] = byWeek[m] || []).push(l);
  }
  const weeks = Object.keys(byWeek).sort().reverse();
  const label = (m) => (m === thisMon ? `本周（${span(m)}）` : m === lastMon ? `上周（${span(m)}）` : span(m));
  const recent = weeks.filter((m) => m >= lastMon).map((m) => ({ label: label(m), items: byWeek[m] }));
  const older = weeks.filter((m) => m < lastMon).map((m) => ({ label: label(m), items: byWeek[m] }));
  return { recent, older, olderN: older.reduce((n, g) => n + g.items.length, 0), hidden };
}

// 日志列表行的显示（U-34、S-4）：周确认写“周确认 MM-DD～MM-DD”，还没整理的写“还没整理”/“待汇总”，用时写“X 分钟”
const logDateText = (l) => (l.period === "week" ? "周确认 " + l.log_date.slice(5) + "～" + (l.period_end || "").slice(5) : l.log_date);
const logSummaryText = (l) => l.summary || (l.period === "week" ? "待汇总" : "还没整理");
const logMinutesText = (l) => (l.edit_seconds ? (l.edit_seconds / 60).toFixed(1) + " 分钟" : "—");

const LogRow = {
  props: ["l"],
  template: `<tr class="click rd-r" @click="go('log', {id: l.id})">
      <td class="nowrap rd-d">{{ logDateText(l) }}</td><td class="rd-s">{{ logSummaryText(l) }}</td>
      <td class="hide-m">{{ l.photos }}</td><td class="hide-m"><span :class="['chip', l.flags ? 'orange' : 'green']">{{ l.flags }}</span></td>
      <td class="nowrap hide-m">{{ logMinutesText(l) }}</td><td class="hide-m">{{ l.edit_ratio === null ? '—' : pct(l.edit_ratio) }}</td>
      <td class="rd-st"><span :class="['chip', STATUS_CHIP[l.status]]">{{ STATUS[l.status] }}</span></td>
      <td class="rd-mm">照片 {{ l.photos }}<template v-if="l.flags"> · <b class="rd-flag">需确认 {{ l.flags }}</b></template></td></tr>`,
  data: () => ({ STATUS, STATUS_CHIP }),
  methods: { go, pct, logDateText, logSummaryText, logMinutesText },
};

const LogList = {
  components: { LogRow, ListDigest },
  template: `<div>
  <page-head :page="isTech() ? 'logs_tech' : 'logs_eng'"><template #actions><div class="seg"><button v-for="s in tabs" :key="s[0]" :class="{on: status === s[0]}" @click="setStatus(s[0])">{{ s[1] }}</button></div></template></page-head>
  <load-state class="mb" compact v-if="err || !loaded" :loading="!loaded" :error="err" @retry="load"></load-state>
  <div class="rd-dg" v-fit="140" v-if="loaded && !isTech() && items.length > 1"><list-digest kind="logs" title="日志小结" :lead="dg.lead" key-title="建议逐份细看" key-hint="数据审核判为高风险" :keys="dg.keys" :groups="dg.groups"></list-digest></div>
  <section class="zb-card rd-logs">
    <div class="zb-ch"><ic n="file"></ic><h2>{{ listTitle }}</h2><span class="zb-n" v-if="loaded">{{ items.length }}</span>
      <span class="zb-src">{{ isTech() ? '本人日志与周确认，按周归组' : '分包技术员提交，点一行查看与审核' }}</span></div>
    <div class="scroll-x log-wrap">
    <zb-paged v-if="isTech()" tag="table" cls="t log-t" :items="techRows" :size="LOG_PAGE" unit="份">
      <template #head><thead><tr><th>日期</th><th>概述</th><th class="hide-m">照片</th><th class="hide-m">需确认</th><th class="hide-m">用时</th><th class="hide-m">修改率</th><th>状态</th></tr></thead></template>
      <template #default="{ item, i }"><tr class="lg-h" v-if="i % LOG_PAGE === 0 || techRows[i - 1]._g !== item._g"><td colspan="8" class="small muted" style="padding-top:12px"><b>{{ item._g }}</b></td></tr>
        <log-row :l="item"></log-row></template>
      <template #empty><div class="zb-empty" v-if="loaded"><empty-state compact :reason="emptyReason" :action="emptyAction"></empty-state></div></template></zb-paged>
    <zb-paged v-else tag="table" cls="t log-t" :items="items" :size="LOG_PAGE" unit="份">
      <template #head><thead><tr><th>日期</th><th>技术员</th><th>概述</th><th class="hide-m">照片</th><th class="hide-m">需确认</th><th class="hide-m">用时</th><th class="hide-m">修改率</th><th>状态</th></tr></thead></template>
      <template #default="{ item: l }"><tr class="click rd-r" @click="go('log', {id: l.id})">
      <td class="nowrap rd-d">{{ logDateText(l) }}</td><td class="nowrap rd-a">{{ l.author }}</td><td class="rd-s">{{ logSummaryText(l) }}</td>
      <td class="hide-m">{{ l.photos }}</td><td class="hide-m"><span :class="['chip', l.flags ? 'orange' : 'green']">{{ l.flags }}</span></td>
      <td class="nowrap hide-m">{{ logMinutesText(l) }}</td><td class="hide-m">{{ l.edit_ratio === null ? '—' : pct(l.edit_ratio) }}</td>
      <td class="rd-st"><span :class="['chip', STATUS_CHIP[l.status]]">{{ STATUS[l.status] }}</span></td>
      <td class="rd-mm">照片 {{ l.photos }}<template v-if="l.flags"> · <b class="rd-flag">需确认 {{ l.flags }}</b></template></td></tr></template>
      <template #empty><div class="zb-empty" v-if="loaded"><empty-state compact :reason="emptyReason" :action="emptyAction"></empty-state></div></template></zb-paged></div>
    <div class="tiny muted rd-hid" v-if="isTech() ? grp.hidden : engHidden && !status">已隐藏 {{ isTech() ? grp.hidden + ' 份没有任何内容的空草稿' : engHidden + ' 份作者还没整理的空草稿' }}</div></section>
  <div class="tiny mt" v-if="isTech()" style="text-align:right"><a class="muted" style="cursor:pointer" @click="go('manual')">手工对照计时（成效测量用）</a></div></div>`,
  data: () => ({ rows: [], status: "", loaded: false, err: null, LOG_PAGE: window.innerWidth < 861 ? 7 : 10, STATUS, STATUS_CHIP }),
  computed: {
    // 技术员的日志按周归组后逐页显示（电脑每页 10 份、手机 7 份，内部分页，不再“更早的日志”展开追加）：本周、上周在前，更早的（grp.older）接在后面几页；
    // 每页第一行和换周处带周标题
    techRows() { return [...this.grp.recent, ...this.grp.older].flatMap((g) => g.items.map((l) => ({ ...l, _g: g.label }))); },
    // 技术员看自己的全部（空草稿由 logWeekGroups 计数隐藏）；审核人看分包日志时不列作者还没整理的空草稿（U-21）
    items() { return isTech() ? this.rows : this.rows.filter((l) => !l.empty); },
    listTitle() { const t = (this.tabs.find((x) => x[0] === this.status) || ["", "全部"])[1]; return t === "全部" ? "全部日志" : t; },
    engHidden() { return isTech() ? 0 : this.rows.filter((l) => l.empty).length; },
    grp() { return logWeekGroups(this.items, today()); },
    // 日志小结（审核岗，2026-10-08）：只用列表已有字段——周确认/每日、数据审核风险、需确认处数、作者；不另算口径
    dg() {
      const its = this.items, st = { submitted: "待审核", approved: "已审核", returned: "已退回" }[this.status] || "";
      const wk = its.filter((l) => l.period === "week").length, risk = (k) => its.filter((l) => l.dq_risk === k).length, flagged = its.filter((l) => l.flags > 0).length;
      const keys = its.filter((l) => l.dq_risk === "high").map((l) => ({ id: l.id, title: logDateText(l) + " · " + l.author, tag: "高风险", tone: "red",
        why: l.flags ? `${l.flags} 处需确认` : "数据审核高风险", route: "log", params: { id: l.id } }));
      const by = {};
      for (const l of its.filter((x) => x.dq_risk !== "high")) (by[l.author || "未署名"] = by[l.author || "未署名"] || []).push(l);
      const groups = Object.entries(by).sort((a, b) => b[1].length - a[1].length).map(([a, ls]) => {
        const f = ls.filter((l) => l.flags > 0).length, w = ls.filter((l) => l.period === "week").length;
        return { k: a, title: `${a} ${ls.length} 份`, tag: f ? "有需确认" : "无疑点", tone: f ? "orange" : "green",
          text: [w ? `其中周确认 ${w} 份` : "都是每日日志", f ? `${f} 份有需确认的地方` : "都没有需确认的地方"].join("，") };
      });
      const lead = `共 ${its.length} 份${st}日志（周确认 ${wk} · 每日 ${its.length - wk}）` + (risk("high") || risk("medium") ? `，数据审核高风险 ${risk("high")} 份、中风险 ${risk("medium")} 份` : "") +
        (flagged ? `，${flagged} 份有需确认的地方` : "，都没有需确认的地方") + `；按作者归成 ${groups.length} 组。`;
      return { lead, keys, groups };
    },
    emptyReason() {
      if (this.status && !(this.status === "submitted" && !isTech())) return "这个状态下没有日志。";
      if (isTech()) return "还没有日志。随手记会在每晚 20:00 自动整理成日志，不用另外填写。";
      return this.status === "submitted" ? "没有待审核的分包日志。" : "分包单位还没有提交日志。";
    },
    emptyAction() {
      if (this.status) return { label: "看全部日志", fn: () => this.setStatus("") };
      return isTech() ? { label: "记一条随手记", route: "home", focus: "note" } : null;
    },
    tabs() { return !isTech() ? [["", "全部"], ["submitted", "待审核"], ["approved", "已审核"], ["returned", "已退回"]] : [["", "全部"], ["draft", "草稿"], ["submitted", "待审核"], ["returned", "退回"]]; } },
  created() { this.status = !isTech() ? "submitted" : ""; },
  mounted() { this.load(); },
  methods: { go, isTech, pct, logDateText, logSummaryText, logMinutesText, setStatus(s) { this.status = s; this.load(); },
    async load() { this.err = null; try { this.rows = await GET(`/api/logs?status=${this.status}`); this.loaded = true; } catch (e) { this.err = e; } } },
};

// ---------------------------------------------------------------------------- 预警
// 浮时统一写整数日历天（U-16、S-4）：只改显示，不改计算口径
function fmtDays(v) {
  if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return "—";
  return Math.round(Number(v)) + " 天";
}
// 措施期限（U-15①②）：AI 留下的占位符（“N天内”）、空着的、或早于判定日的期限都不可用，界面写“期限待定”并标红
function measureDueBad(due, asOf) {
  const t = String(due || "").trim();
  if (!t || /[NnXx]\s*(天|日|个工作日)/.test(t)) return true;
  const ds = t.match(/\d{4}-\d{2}-\d{2}/g) || [];
  return !!asOf && ds.some((d) => d < asOf);
}
// 误报 / 处置关闭的常用原因（S-7）：卡内就地点选，可再补充说明，不弹浏览器原生输入框
const FB_REASONS = {
  false_alarm: ["现场实际已完成，进度未及时上报", "作业已调整或取消", "P6 计划逻辑有误", "判定所用的 P6 数据已过时"],
  closed: ["已组织赶工", "已调整计划并报批", "已落实人员和机械"],
};

// 预警原因分析正文的容器（一屏化 10-10）：详情页（full）电脑端放进固定高度面板内滚动，手机收成“原因、影响与措施”一行、点开抽屉看全文；
// 列表里的卡片（非 full）电脑端原样展开、手机同样收进抽屉。只改排布，不改内容。
const AcFold = {
  props: { full: Boolean },
  template: `<div v-if="!full && !narrow"><slot></slot></div><div v-else-if="!narrow" class="rp-ac-fit" v-fit="120"><slot></slot></div>
    <div v-else class="rp-ac-fold"><button type="button" class="zb-link" @click="open = true">原因、影响与措施<ic n="chevron"></ic></button>
      <zb-drawer v-if="open" title="原因分析" @close="open = false"><slot></slot></zb-drawer></div>`,
  data: () => ({ narrow: window.innerWidth < 861, open: false }),
  mounted() { this.onR = () => { this.narrow = window.innerWidth < 861; }; window.addEventListener("resize", this.onR); },
  unmounted() { window.removeEventListener("resize", this.onR); },
};

const AlertCard = {
  components: { LevelChip, AcFold },
  // full：详情页用，页头多写 P6 数据日期（AC-3.6）。声明为布尔，<alert-card full> 的无值写法才是 true（Vue 3 未声明类型时传入的是空字符串）
  props: { a: Object, full: Boolean },
  emits: ["changed"],
  template: `<div :class="['zb-card', 'alert-card', 'rp-ac', a.level]">
  <div class="row between wrap"><div class="row wrap"><level-chip :level="a.level"></level-chip><span class="chip">{{ a.rule_name }}</span>
    <span :class="['chip', a.status === 'open' ? 'orange' : a.status === 'confirmed' ? 'blue' : '']">{{ ALERT_STATUS[a.status] }}</span>
    <span class="chip" v-if="a.replay" title="由历史/反推 P6 计划回放产生，不计入预警命中率与应用成效">回放</span></div>
    <span class="tiny muted">判定日 {{ a.as_of }}<template v-if="full"> · {{ p6Date ? 'P6 数据日期 ' + p6Date : 'P6 数据日期未知' }}</template></span></div>
  <h3 class="rp-ac-t" @click="go('alert', {id: a.id})">{{ a.title }}</h3>
  <div class="rp-ac-m small">
    <div><div class="muted tiny">计划应完成<help-tip text="按 P6 计划，到判定日这项作业应完成的百分比。"></help-tip></div><b>{{ fmtN(f.planned_pct) }}%</b></div>
    <div><div class="muted tiny">实际（{{ f.actual_source }}）<help-tip text="括号里是实际进度的来源：日报确认的完成量，或 P6 数据日期时的完成%。与“计划应完成”之差就是落后的进度。"></help-tip></div><b>{{ fmtN(f.actual_pct) }}%</b></div>
    <div><div class="muted tiny">滞后 / 总浮时<help-tip text="滞后：按实际进度折算，比计划晚了多少天（挣值法）。总浮时：这项作业可以推迟而不影响完工的天数。滞后超过总浮时就会拖总工期，判为红色。"></help-tip></div><b>{{ fmtN(f.slip_days) }} 天 / {{ fmtDays(f.total_float_d) }}</b></div>
    <div><div class="muted tiny">预测完成<help-tip text="按目前推进速度推算的完成日期；后面的“计划”是 P6 计划完成日期。"></help-tip></div><b>{{ f.forecast_finish || '—' }}</b> <span class="tiny muted">计划 {{ f.early_finish }}</span></div>
  </div>
  <div class="lvl-bar mt"><i :style="{width: Math.min(100, f.planned_pct || 0) + '%', background: '#cdd9e6'}"></i>
    <i :style="{width: Math.min(100, f.actual_pct || 0) + '%', background: a.level === 'red' ? 'var(--red)' : a.level === 'orange' ? 'var(--orange)' : '#d6b300'}"></i></div>
  <div class="small mt" v-if="f.milestones && f.milestones.length">可能影响里程碑：<span v-for="m in f.milestones" :key="m.code" class="chip red" style="margin-right:4px">{{ m.name }} {{ m.planned }}</span></div>
  <zb-clamp class="small mt" v-if="f.risk_signals" :lines="2" title="日报风险信号">日报风险信号：<span v-for="r in f.risk_signals" :key="r.log_id + r.desc"><a @click.stop="go('log', {id: r.log_id})">{{ logRef('L' + r.log_id) }}</a> {{ r.type }}：{{ r.desc }}；</span></zb-clamp>
  <div class="ai-box mt" v-if="a.explanation">
    <div class="hd">原因分析<template v-if="a.explanation_as_of"> · 基于 {{ a.explanation_as_of }} 判定</template><span class="chip orange" v-if="a.explanation_stale" style="margin-left:6px">基于旧判定</span><help-tip v-if="a.explanation_stale" label="基于旧判定" :text="'分析之后判定已更新（当前判定日 ' + a.as_of + '：' + a.title + '），以下分析基于旧判定，请' + (can('progress_ops') ? '点“重新分析”' : '等待工程主管重新分析') + '。'"></help-tip></div>
    <div :class="a.explanation_stale ? 'muted' : ''"><b>{{ a.explanation.judgement }}</b></div>
    <ac-fold :full="full">
    <div class="small mt" v-for="(c, i) in a.explanation.causes" :key="i">原因{{ i + 1 }}（{{ c.type }}）：{{ c.cause }}
      <a v-for="e in c.evidence" :key="e" class="src" @click="/^L\d+$/.test(e) && go('log', {id: e.slice(1)})">{{ logRef(e) }}</a></div>
    <div class="small mt" v-if="a.explanation.impact">影响：{{ a.explanation.impact }}</div>
    <div class="small mt" v-for="(m, i) in a.explanation.measures" :key="'m' + i">措施{{ i + 1 }}：{{ m.action }} <span class="muted">（{{ m.owner }}，<b v-if="measureDueBad(m.due, a.as_of)" style="color:var(--red)" title="AI 给的期限不可用（占位符或早于判定日），请人工确定">期限待定</b><template v-else>{{ m.due }}</template>）</span></div>
    <div class="tiny muted mt" v-if="a.explanation.data_gaps.length">信息缺口：{{ a.explanation.data_gaps.join('；') }}</div></ac-fold>
  </div>
  <div class="small mt muted" v-if="a.feedback_note && !can('progress_ops')">反馈：{{ a.feedback_note }}</div>
  <div class="zb-btns rp-ac-act mt" v-if="can('progress_ops')">
    <button class="btn sm" :disabled="busy" @click="explain">{{ busy ? '分析中…' : (a.explanation ? '重新分析' : '分析原因') }}</button>
    <template v-if="a.status === 'open'"><button class="btn sm primary" :disabled="sending" @click="fb('confirmed')">确认属实</button>
      <button :class="['btn', 'sm', {primary: ask === 'false_alarm'}]" @click="openAsk('false_alarm')">误报</button><button :class="['btn', 'sm', {primary: ask === 'closed'}]" @click="openAsk('closed')">处置关闭</button></template>
    <span class="small muted rp-ac-fb" v-if="a.feedback_note">反馈：{{ a.feedback_note }}</span>
  </div>
  <div class="mt" v-if="ask && a.status === 'open'">
    <div class="small"><b>{{ ask === 'false_alarm' ? '误报原因（必填）' : '处置说明（可空）' }}</b> <span class="tiny muted">点选常用原因，或在下面补充</span></div>
    <div class="row wrap mt"><span v-for="r in FB_REASONS[ask]" :key="r" :class="['chip', note === r ? 'blue' : '']" style="cursor:pointer" @click="note = r">{{ r }}</span></div>
    <textarea class="in mt" rows="2" v-model="note" :placeholder="ask === 'false_alarm' ? '为什么是误报' : '采取了什么处置（可空）'"></textarea>
    <div class="row mt"><div class="grow"></div><button class="btn sm" @click="ask = ''; note = ''">取消</button>
      <button class="btn sm primary" :disabled="sending || (ask === 'false_alarm' && !note.trim())" @click="fb(ask)">{{ sending ? '提交中…' : '提交' }}</button></div></div></div>`,
  data: () => ({ busy: false, ask: "", note: "", sending: false, ALERT_STATUS, FB_REASONS }),
  computed: {
    f() { return this.a.detail || {}; },
    // AC-3.6：预警所依据的 P6 数据日期；老预警取不到时为空，界面写“P6 数据日期未知”，不补默认值
    p6Date() { return this.a.p6_data_date || (this.a.explanation || {}).p6_data_date || ""; },
  },
  methods: {
    go, can, fmtN, fmtDays, measureDueBad,
    // 日志引用（U-15⑤）：不露 L342 这类内部编号，写成“日志 09-17 钱志远”；查不到日期时只写“日志”；非日志引用原样
    logRef(e) {
      const m = /^L(\d+)$/.exec(String(e || ""));
      if (!m) return e;
      const id = Number(m[1]);
      const l = ((this.a && this.a.logs) || []).find((x) => Number(x.log_id) === id);
      if (l) return `日志 ${String(l.date || "").slice(5)}${l.author ? " " + l.author : ""}`;
      const r = ((this.f && this.f.risk_signals) || []).find((x) => Number(x.log_id) === id);
      return r && r.date ? `日志 ${String(r.date).slice(5)}` : "日志";
    },
    async explain() { this.busy = true; try { this.$emit("changed", await POST(`/api/alerts/${this.a.id}/explain`)); } catch (e) { toast(e.message, true); } finally { this.busy = false; } },
    openAsk(status) { if (this.ask !== status) { this.ask = status; this.note = ""; } },
    async fb(status) {
      const note = status === "confirmed" ? "" : String(this.note || "").trim();
      if (status === "false_alarm" && !note) { toast("请写明误报原因", true); return; }
      this.sending = true;
      try { this.$emit("changed", await POST(`/api/alerts/${this.a.id}/feedback`, { status, note })); this.ask = ""; this.note = ""; toast("已记录"); }
      catch (e) { toast(e.message, true); } finally { this.sending = false; }
    },
  },
};

// 预警小结（2026-10-08 用户确认做样板）：列表顶部先给结论再给明细。
// - 只按已有字段归并（判定规则、颜色、滞后天数、总浮时、是否影响里程碑），数字全部取自本页预警，不调用 AI、不另算口径；
// - “需要拍板”= 红色、影响里程碑、或滞后已超过总浮时（与判定规则“滞后超过浮时为红色”同一口径），逐条列出；其余同规则同颜色的合成一句话；
// - 批量操作就是对每一条调用原来的 /api/alerts/{id}/feedback（确认属实 / 误报须选原因），不新增接口、不改状态语义。
const alertNum = (v) => (v === null || v === undefined || v === "" ? null : Number(v));
function alertIsKey(a) {
  const f = a.detail || {}, s = alertNum(f.slip_days), fl = alertNum(f.total_float_d);
  return a.level === "red" || (f.milestones && f.milestones.length > 0) || (s !== null && fl !== null && s > fl);
}
const AlertDigest = {
  props: { items: Array, status: String },
  components: { LevelChip, DigestHead },
  emits: ["changed", "only"],
  template: `<section class="ad" v-if="items.length">
    <digest-head kind="alerts" title="预警小结" :facts="facts" :fallback="lead"><template #tools>
      <button v-if="key.length && rest.length" :class="['btn', 'sm', only ? 'primary' : '']" @click="only = !only; $emit('only', only)">{{ only ? '显示全部' : '只看需拍板的 ' + key.length + ' 项' }}</button></template></digest-head>
    <div class="ad-key" v-if="key.length"><div class="ad-sub"><i class="dot red"></i>需要拍板 · {{ key.length }} 项<small>红色、影响里程碑或滞后已超过总浮时</small></div>
      <a v-for="a in key" :key="a.id" class="ad-k" @click="go('alert', {id: a.id})"><level-chip :level="a.level"></level-chip><span class="grow">{{ a.title }} <span class="chip" v-if="a.replay">回放</span></span><small>{{ why(a) }}</small><ic n="chevron"></ic></a></div>
    <div class="ad-grp" v-for="g in groups" :key="g.k"><div class="ad-gt"><level-chip :level="g.level"></level-chip><b>{{ g.items.length }} 项“{{ g.rule }}”</b><span>{{ g.text }}</span></div>
      <div class="ad-acts" v-if="canAct && g.open.length"><button class="btn sm primary" :disabled="!!busy" @click="batch(g, 'confirmed')">{{ busy === g.k + 'confirmed' ? '提交中…' : '这 ' + g.open.length + ' 项确认属实' }}</button>
        <button :class="['btn', 'sm', ask === g.k ? 'primary' : '']" :disabled="!!busy" @click="ask = ask === g.k ? '' : g.k; note = ''">标为误报</button>
        <span class="tiny muted">逐条提交，和在单条预警上操作一样留痕</span></div>
      <div class="ad-ask" v-if="ask === g.k"><span class="tiny"><b>误报原因（必选）</b></span><span v-for="r in reasons" :key="r" :class="['chip', note === r ? 'blue' : '']" @click="note = r">{{ r }}</span>
        <button class="btn sm primary" :disabled="!note || !!busy" @click="batch(g, 'false_alarm')">{{ busy ? '提交中…' : '提交 ' + g.open.length + ' 项' }}</button></div></div></section>`,
  data: () => ({ only: false, busy: "", ask: "", note: "" }),
  computed: {
    canAct() { return can("progress_ops"); },
    reasons() { return FB_REASONS.false_alarm; },
    key() { return this.items.filter(alertIsKey); },
    rest() { return this.items.filter((a) => !alertIsKey(a)); },
    groups() {
      const by = {};
      for (const a of this.rest) { const k = a.rule + "|" + a.level; (by[k] = by[k] || { k, rule: a.rule_name, level: a.level, items: [] }).items.push(a); }
      const order = { red: 0, orange: 1, yellow: 2 };
      return Object.values(by).sort((x, y) => (order[x.level] ?? 3) - (order[y.level] ?? 3) || y.items.length - x.items.length).map((g) => {
        const codes = g.items.map((a) => a.activity_short || a.activity_code), slips = g.items.map((a) => alertNum((a.detail || {}).slip_days)).filter((v) => v !== null),
          floats = g.items.map((a) => alertNum((a.detail || {}).total_float_d)).filter((v) => v !== null);
        const parts = [codes.slice(0, 3).join("、") + (codes.length > 3 ? " 等" : "")];
        if (slips.length) { const lo = Math.round(Math.min(...slips)), hi = Math.round(Math.max(...slips)); if (hi > 0) parts.push(lo === hi ? `滞后 ${hi} 天` : `滞后 ${lo}–${hi} 天`); }
        if (floats.length === g.items.length && floats.length) parts.push(`总浮时最少还有 ${Math.round(Math.min(...floats))} 天`);
        return { ...g, text: parts.join("，"), open: g.items.filter((a) => a.status === "open") };
      });
    },
    // 交给进度智能体写判断的只有这些系统已算好的内容（AI 小结的数字白名单也取自这里）
    facts() { return { lead: this.lead, key: this.key.map((a) => a.title + "：" + this.why(a)), groups: this.groups.map((g) => `${g.items.length} 项“${g.rule}”（${({ red: "红色", orange: "橙色", yellow: "黄色" })[g.level] || ""}）：${g.text}`) }; },
    lead() {
      const n = (lv) => this.items.filter((a) => a.level === lv).length, st = { open: "待处理", confirmed: "已确认", false_alarm: "误报", closed: "已关闭" }[this.status] || "";
      const bits = [["red", "红"], ["orange", "橙"], ["yellow", "黄"]].filter(([lv]) => n(lv)).map(([lv, t]) => `${t} ${n(lv)}`);
      const head = `共 ${this.items.length} 项${st}预警（${bits.join(" · ")}）`;
      if (!this.key.length) return head + "，都没有影响里程碑，滞后也都在总浮时以内，可以按下面的分组一次处理。";
      return head + `，其中 ${this.key.length} 项需要拍板，其余 ${this.rest.length} 项归成 ${this.groups.length} 组。`;
    },
  },
  methods: {
    go,
    why(a) {
      const f = a.detail || {}, s = alertNum(f.slip_days), fl = alertNum(f.total_float_d), out = [];
      if (f.milestones && f.milestones.length) out.push("影响 " + f.milestones.map((m) => m.name).join("、"));
      if (s !== null && fl !== null && s > fl) out.push(`滞后 ${Math.round(s)} 天，超过浮时 ${Math.round(fl)} 天`);
      else if (a.level === "red") out.push("红色预警");
      return out.join("；");
    },
    async batch(g, status) {
      const note = status === "false_alarm" ? this.note : "";
      if (status === "false_alarm" && !note) return;
      this.busy = g.k + status; let ok = 0;
      try { for (const a of g.open) { this.$emit("changed", await POST(`/api/alerts/${a.id}/feedback`, { status, note })); ok++; } toast(`已提交 ${ok} 项`); this.ask = ""; this.note = ""; }
      catch (e) { toast((ok ? `已提交 ${ok} 项，` : "") + e.message, true); } finally { this.busy = ""; }
    },
  },
};

const Alerts = {
  components: { AlertCard, AlertDigest },
  // 10-10 改版：先给态势（全部预警、红色预警分开写；人员反馈是确认/误报的人工计数，不是模型准确率），再筛选 + 小结 + 明细。
  // 一屏化：电脑端小结（左）与明细（右）并排，各自面板内滚动；明细内部分页（电脑每页 4 条、手机 2 条）。
  template: `<div class="rp-al-page">
  <div class="zb-kpis n4 rp-kpis" v-if="all">
    <button type="button" :class="['zb-kpi', {on: status === '' && !lvl}]" @click="lvl = ''; setStatus('')"><span class="zb-kpi-ic"><ic n="alert"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">全部预警</span>
      <b class="zb-kpi-v">{{ all.length }}<small> 项</small></b><span class="zb-kpi-s" :title="allSub">{{ allSub }}</span></span></button>
    <button type="button" :class="['zb-kpi', redN ? 'red' : '', {on: lvl === 'red'}]" @click="lvl = 'red'; setStatus('confirmed')"><span class="zb-kpi-ic"><ic n="octagon"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">红色预警</span>
      <b class="zb-kpi-v">{{ redN }}<small> 项</small></b><span class="zb-kpi-s">{{ (redN ? '已确认未关闭' : '没有已确认未关闭的红色预警') + (redOpen ? ' · 另待确认 ' + redOpen : '') }}</span></span></button>
    <button type="button" :class="['zb-kpi', openN ? 'orange' : 'green', {on: status === 'open' && !lvl}]" @click="lvl = ''; setStatus('open')"><span class="zb-kpi-ic"><ic n="clock"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">待确认</span>
      <b class="zb-kpi-v">{{ openN }}<small> 项</small></b><span class="zb-kpi-s">{{ openN ? '等待确认属实或误报' : '没有待人工反馈的预警' }}</span></span></button>
    <div class="zb-kpi rp-static"><span class="zb-kpi-ic"><ic n="users"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">人员反馈<help-tip :text="statsDef + '。例如 80% 表示人工反馈过的预警里，有八成被确认属实。这是人员反馈的计数，不是模型准确率。'" label="确认占比怎么算"></help-tip></span>
      <b class="zb-kpi-v rp-fb" v-if="stats">确认 {{ stats.confirmed }}<small> · 误报 {{ stats.false_alarm }}</small></b><b class="zb-kpi-v" v-else>—</b>
      <span class="zb-kpi-s">{{ fbSub }}</span></span></div></div>
  <div class="rp-bar"><div class="seg"><button v-for="s in tabs" :key="s[0]" :class="{on: status === s[0]}" @click="setStatus(s[0])">{{ s[1] }}</button></div>
      <span class="chip red rp-lvl" v-if="lvl === 'red'">只看红色{{ status === 'confirmed' ? '（已确认未关闭）' : '' }} <a @click="lvl = ''" aria-label="取消只看红色">×</a></span>
      <help-tip label="判定规则">判定规则：挣值法计算滞后天数并与 P6 总浮时比较，滞后超过浮时为红色。<template v-if="isAdmin">阈值在 项目与规则 中调整。</template><template v-if="seesGaps">关键作业连续多日没有日报进度属于“数据缺口”（没报，不是落后），不在此列预警，也不计入预警准确率；旧库里留存的此类历史预警标为“数据缺口（历史）”。</template></help-tip>
      <span class="grow"></span>
      <template v-if="can('progress_ops')"><label class="rp-asof"><span class="tiny muted">判定日</span><input type="date" class="in" v-model="asOf"></label><button class="btn primary sm" :disabled="busy" @click="detect">{{ busy ? '判定中…' : '运行判定' }}</button></template></div>
  <load-state class="mb" compact v-if="err || !loaded" :loading="!loaded" :error="err" @retry="load"></load-state>
  <div :class="{'rp-al-g': loaded && items.length > 1 && !lvl && shownItems.length}"><div class="rp-al-gl" v-if="loaded && items.length > 1 && !lvl">
  <div class="rp-al-dg" v-fit="160"><alert-digest :items="items" :status="status" @changed="replace" @only="(v) => (onlyKey = v)"></alert-digest></div>
  <div class="ad-fold" v-if="!onlyKey"><a @click="showAll = !showAll">{{ showAll ? '收起明细' : '展开全部 ' + items.length + ' 条明细' }}</a></div></div>
  <zb-paged v-if="shownItems.length" :items="shownItems" :size="4" :m-size="2" unit="条" cls="rp-al-list"><template #default="{ item: a }"><alert-card :key="a.id" :a="a" @changed="replace"></alert-card></template></zb-paged></div>
  <section class="zb-card" v-if="loaded && !shownItems.length && (!items.length || lvl)"><empty-state compact :reason="lvl ? (status === 'confirmed' ? '没有已确认未关闭的红色预警。' : '这个分类下没有红色预警。') : emptyReason" :action="lvl ? {label: '显示全部颜色', fn: () => (lvl = '')} : emptyAction"></empty-state></section></div>`,
  data: () => ({ store, items: [], all: null, lvl: "", status: "open", asOf: "", busy: false, stats: null, loaded: false, err: null, hasPlan: true, showAll: window.innerWidth >= 861, onlyKey: false }),   // 手机先只看小结，明细点“展开”再看（一屏化）
  computed: {
    // 明细默认展开（用户 2026-10-08：小结放上面，下面照常列出，避免大片空白），可收起；“只看需拍板”时只列需拍板的那几条
    shownItems() {
      if (this.lvl) return this.items.filter((a) => a.level === this.lvl);
      if (this.items.length <= 1) return this.items;
      if (this.onlyKey) return this.items.filter(alertIsKey);
      return this.showAll ? this.items : [];
    },
    // 态势数字只按接口返回的预警逐条数，不另算口径；全部与红色分开写，不相加。
    // “红色预警”与经理首页、领导总览同一口径（验收 B2）：已确认未关闭（status=confirmed，已关闭、误报不计）；待确认的红色另写
    redN() { return (this.all || []).filter((a) => a.level === "red" && a.status === "confirmed").length; },
    confirmedN() { return (this.all || []).filter((a) => a.status === "confirmed").length; },
    redOpen() { return (this.all || []).filter((a) => a.level === "red" && a.status === "open").length; },
    openN() { return (this.all || []).filter((a) => a.status === "open").length; },
    allSub() {
      const n = (s) => (this.all || []).filter((a) => a.status === s).length;
      return [["confirmed", "已确认"], ["false_alarm", "误报"], ["closed", "已关闭"]].map(([s, t]) => `${t} ${n(s)}`).join(" · ");
    },
    // 人员反馈：确认属实 ÷（确认属实 + 误报），是人工反馈计数；没有反馈时不出百分比
    fbSub() {
      const s = this.stats;
      if (!s) return "—";
      const n = s.confirmed + s.false_alarm;
      return n ? `确认占比 ${pct(s.precision)}（${s.confirmed}/${n}），人员反馈计数，不是模型准确率` : "还没有人员反馈，不计算确认占比";
    },
    tabs: () => [["", "全部"], ["open", "待确认"], ["confirmed", "已确认"], ["false_alarm", "误报"], ["closed", "已关闭"]],   // “全部”排第一（RJ 10-09），默认仍看待处理
    isAdmin: () => roleOf() === "admin",  // 阈值只有系统管理员能在“项目与规则”里改，其他岗位不提
    // “数据缺口”只给工程主管看（同后端 GAP_VIEW_ROLES）；项目经理只关注进度、质量、安全，不出现缺报概念（F-14③）
    seesGaps: () => roleOf() === "eng_sup",
    statsDef() {
      const d = (this.stats && this.stats.definition) || "";
      return this.seesGaps ? d : d.split(/[；;]/).filter((x) => x && !x.includes("数据缺口")).join("；");
    },
    emptyReason() {
      if (this.status && this.status !== "open") return "这个分类下没有预警。";
      if (!this.hasPlan) return "还没有 P6 计划，没有可判定的作业。";
      // 有已确认未关闭的预警时不说“没发现滞后”，免得与首页“进度风险”数字矛盾
      if (this.confirmedN) return `没有待确认的预警。已确认未关闭的预警 ${this.confirmedN} 项（红 ${this.redN}）在“已确认”页签。`;
      return "没有待确认的预警。最近一次判定没发现滞后，或还没运行过判定。";
    },
    emptyAction() {
      if (this.status && this.status !== "open") return { label: "看待确认的", fn: () => this.setStatus("open") };
      if (this.status === "open" && this.confirmedN) return { label: `看已确认未关闭的 ${this.confirmedN} 项`, fn: () => this.setStatus("confirmed") };
      if (this.status === "open" && this.all && this.all.length) return { label: `看全部 ${this.all.length} 项预警`, fn: () => this.setStatus("") };   // 运行判定已在上方工具行，不重复放
      if (!can("progress_ops")) return null;
      return this.hasPlan ? { label: "运行判定", fn: this.detect } : { label: "去导入 P6 计划", route: "progress?tab=plan", id: "plan-file" };
    },
  },
  // 首页卡片带筛选进入：#/progress?tab=alerts&status=confirmed&level=red 直接列出卡上那几条
  created() { this.fromRoute(); },
  mounted() { this.asOf = today(); this.load(); },
  watch: { "store.route.params"(p, o) { if (p && (p.status !== (o || {}).status || p.level !== (o || {}).level)) { this.fromRoute(); this.load(); } } },
  methods: {
    can, pct,
    fromRoute() {
      const p = store.route.params || {};
      if ("status" in p && ["", "open", "confirmed", "false_alarm", "closed"].includes(p.status)) this.status = p.status;
      this.lvl = ["red", "orange", "yellow"].includes(p.level) ? p.level : "";
    },
    setStatus(s) { this.status = s; this.showAll = window.innerWidth >= 861; this.onlyKey = false; this.load(); },
    async load() {
      this.err = null;
      try {
        this.items = await GET(`/api/alerts?status=${this.status}`); this.stats = await GET("/api/alerts/stats");
        this.all = this.status === "" ? this.items : await GET("/api/alerts?status=");
        try { this.hasPlan = (await GET("/api/p6/imports")).length > 0; } catch (_) { this.hasPlan = true; }
        this.loaded = true;
      } catch (e) { this.err = e; }
    },
    replace(a) {
      for (const list of [this.items, this.all]) { if (!list) continue; const i = list.findIndex((x) => x.id === a.id); if (i >= 0) list.splice(i, 1, a); }
      GET("/api/alerts/stats").then((s) => { this.stats = s; }).catch(() => {});
    },
    async detect() {
      this.busy = true;
      try {
        const r = await POST("/api/alerts/detect", { as_of: this.asOf });
        toast(`监测 ${r.monitored} 项作业，触发 ${r.triggered}，新增 ${r.new}，升级 ${r.escalated}${r.ai_pending ? '，AI 分析进行中' : ''}`);
        await this.load();
        if (r.ai_pending) setTimeout(() => this.load(), 15000);
      } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

const AlertDetail = {
  components: { AlertCard },   // “慢在哪”（x_slowdiag.js 的 SlowDiag）加载后挂到这里的 components，放在右栏
  props: ["id"],
  // 一屏化（10-10）：电脑端两列、整页一屏——左栏预警卡（原因分析在面板内滚动），右栏（慢在哪 / 相关日志证据 / 推送记录）与左栏等高、栏内滚动；
  // 日志摘录每条最多两行，全文点开抽屉；日志与推送记录内部分页（电脑每页 5 条，手机每页 1 条）。
  template: `<div v-if="a" class="rp-ad"><page-head page="alert_detail"><template #meta><a @click="back">← 返回</a></template></page-head>
    <div class="rp-ad-g"><div class="rp-ad-l"><alert-card :a="a" :full="true" @changed="load"></alert-card></div>
    <div class="rp-ad-rw"><div class="rp-ad-r">
    <slow-diag></slow-diag>
    <section class="zb-card rp-sec rp-ad-logs"><div class="zb-ch"><ic n="file"></ic><h2>相关日志证据</h2><span class="zb-n">{{ a.logs.length }}</span><span class="tiny muted">近 14 天提到这项作业的日志</span></div>
      <div><zb-paged :items="a.logs" :size="5" :m-size="1" unit="条" :fit="false"><template #default="{ item: l }">
        <div class="rp-log" :key="l.log_id"><zb-clamp :lines="2" :title="'日志 ' + l.date + ' ' + (l.author || '')"><a @click.stop="go('log', {id: l.log_id})">日志 {{ l.date }}</a> <span class="muted">{{ l.author }}</span>：{{ logText(l) }}</zb-clamp></div></template></zb-paged>
      <div class="zb-empty" v-if="!a.logs.length">近 14 天没有提到这项作业的日志，原因分析缺少日志依据。</div></div></section>
    <section class="zb-card rp-sec rp-ad-nt"><div class="zb-ch"><ic n="send"></ic><h2>推送记录</h2><span class="zb-n">{{ a.notifications.length }}</span><span class="grow"></span><button class="btn sm" v-if="can('progress_ops')" @click="renotify">重新推送</button></div>
      <div><zb-paged :items="a.notifications" :size="5" :m-size="1" unit="条" :fit="false"><template #default="{ item: n }">
        <div class="rp-nt"><b>{{ n.name }}</b><span class="tiny muted">{{ fmtDT(n.created_at) }}</span><span class="small">{{ channels(n.channels_json) }}</span></div></template></zb-paged>
      <div class="zb-empty" v-if="!a.notifications.length">还没有推送记录。</div></div></section></div></div></div>
    <ai-note page="alerts"></ai-note></div><load-state v-else loading :error="err" @retry="load"></load-state>`,
  data: () => ({ a: null, err: null }),
  mounted() { this.load(); },
  methods: {
    go, can, fmtDT,
    back() { backOr("progress", "progress", { tab: "alerts", ...(this.a && this.a.status ? { status: this.a.status } : {}) }); },
    // 日志摘录里没有累计值时接口给的是“累计None%”：显示成“累计—”，不当成数字
    logText(l) { return l.items.concat(l.general).join("；").replace(/None%?/g, "—"); },
    async load() { this.err = null; try { this.a = await GET(`/api/alerts/${this.id}`); } catch (e) { this.err = e; } },
    channels(j) { const c = JSON.parse(j); const n = { inapp: "站内", email: "邮件", webhook: "群机器人" }; const s = { sent: "已发", skipped: "未配置" };
      return Object.entries(c).map(([k, v]) => `${n[k] || k}:${s[v] || v}`).join("  "); },
    async renotify() { await POST(`/api/alerts/${this.id}/notify`); toast("已推送"); this.load(); },
  },
};

// ---------------------------------------------------------------------------- P6 计划
const Plan = {
  components: { LevelChip },
  template: `<div class="rp-plan">
  <section class="zb-card rp-gantt"><div class="zb-ch"><ic n="calendar"></ic><h2>P6 计划</h2><span class="zb-n">{{ items.length }}</span>
      <span class="tiny muted rp-cur" v-if="imports.length">当前 {{ imports[0].file_name }} · 数据日期 {{ imports[0].data_date || '未知' }}</span><span class="grow"></span>
      <div class="seg" v-if="gantt"><button :class="{on: view === 'gantt'}" @click="view = 'gantt'">横道图</button><button :class="{on: view === 'table'}" @click="view = 'table'">表格</button></div></div>
    <div class="rp-bar rp-plan-f"><input class="in rp-q" v-model="q" placeholder="搜索代码/名称/WBS" @keyup.enter="load">
      <div class="seg"><button v-for="s in tabs" :key="s[0]" :class="{on: only === s[0]}" @click="only = s[0]; load()">{{ s[1] }}</button></div></div>
    <component :is="gantt" v-if="gantt && view === 'gantt' && loaded && items.length" :items="items"></component>
    <div class="scroll-x rp-plan-t" v-show="view === 'table' || !gantt || !items.length"><zb-paged v-if="items.length" :items="items" :size="15" :m-size="8" unit="项作业" tag="table" cls="t" :fit="200"><template #head><thead><tr><th>作业</th><th>WBS</th><th>计划</th><th class="right">总浮时</th><th class="right">P6完成</th><th class="right">日志累计</th><th class="right">今日应完成</th><th>判定</th></tr></thead></template>
      <template #default="{ item: a }"><tr :key="a.code"><td><span v-if="a.is_critical" class="chip red">关键</span> <b><act-name :c="a.code" :n="a.name" :s="a.short_name"></act-name></b></td>
        <td class="small muted">{{ a.wbs_path }}</td><td class="small nowrap">{{ a.early_start }} ~ {{ a.early_finish }}</td>
        <td class="right nowrap">{{ fmtDays(a.total_float_d) }}</td><td class="right">{{ fmtN(a.phys_pct) }}%</td>
        <td class="right">{{ a.log_pct === null ? '—' : fmtN(a.log_pct) + '%' }}<div class="tiny muted" v-if="a.log_qty">{{ fmtN(a.log_qty, 2) }}{{ a.log_unit }}</div>
          <div class="tiny" v-if="a.log_qty_self || a.log_qty_approved"><trust-split :self="a.log_qty_self" :approved="a.log_qty_approved" :self-ids="a.log_ids_self" :approved-ids="a.log_ids_approved" :unit="a.log_unit"></trust-split></div></td>
        <td class="right">{{ a.plan_pct === null ? '—' : fmtN(a.plan_pct) + '%' }}</td>
        <td><level-chip v-if="a.level" :level="a.level"></level-chip><span class="small" v-if="a.slip"> 滞后{{ fmtN(a.slip) }}天</span><span v-if="a.act_finish" class="chip green">已完成</span></td></tr></template></zb-paged>
      <empty-state compact v-if="loaded && !items.length" :reason="emptyReason" :action="emptyAction"></empty-state>
      <load-state compact v-if="err || !loaded" :loading="!loaded" :error="err" @retry="load"></load-state></div></section>
  <details class="zb-card rp-imp" v-if="can('progress_ops')" :open="loaded && !imports.length"><summary class="zb-ch"><ic n="download"></ic><h2>导入 P6 计划</h2>
      <help-tip text="支持 .xer（推荐）或 Excel；XER 含数据日期。每次导入形成新批次，以最新批次为准。"></help-tip>
      <span class="tiny muted">{{ imports.length ? '新一期 P6 更新到了再导入' : '还没有导入 P6 计划' }}</span><span class="grow"></span><span class="rp-imp-o">展开</span></summary>
    <div class="rp-imp-b"><div class="row wrap"><input id="plan-file" type="file" accept=".xer,.xlsx" @change="file = $event.target.files[0]">
      <label class="small">数据日期 <input type="date" class="in" style="width:160px;display:inline-block" v-model="dataDate"></label>
      <button class="btn primary sm" :disabled="!file || busy" @click="upload">{{ busy ? '导入中…' : '导入' }}</button><span class="grow"></span><a class="btn sm" href="/api/p6/export-progress">导出进度表</a></div>
    <div class="small mt" v-if="imports.length">当前：<b>{{ imports[0].file_name }}</b> · 数据日期 {{ imports[0].data_date }} · {{ imports[0].activity_count }} 项作业 / {{ imports[0].relation_count }} 条逻辑关系 · {{ fmtDT(imports[0].imported_at) }} {{ imports[0].imported_by }}</div></div>
  </details></div>`,
  // RJ 10-09：P6 计划默认看横道图（可全屏），表格是可选视图；导入卡片放在图表之后。10-10：导入收进图表下方的折叠区（还没导入时自动展开），甘特为主；一屏化：横道图与表格都在面板内滚动（v-fit），整页一屏
  data: () => ({ items: [], imports: [], q: "", only: "", file: null, dataDate: "", busy: false, loaded: false, err: null, view: "gantt" }),
  computed: {
    tabs: () => [["", "全部"], ["critical", "关键作业"], ["alert", "触发预警"], ["active", "进行中"]],
    gantt: () => (typeof PlanGantt !== "undefined" ? PlanGantt : null),   // 横道图在 x_gantt.js
    emptyReason() {
      if (this.imports.length) return "没有符合条件的作业。";
      return can("progress_ops") ? "还没有导入 P6 计划。导入后这里会列出作业、浮时和日志进度。" : "项目还没有导入 P6 计划，请工程主管或项目经理先导入。";
    },
    emptyAction() {
      if (this.imports.length) return { label: "清除筛选", fn: () => { this.q = ""; this.only = ""; this.load(); } };
      return can("progress_ops") ? { label: "选择 .xer 文件", fn: () => { const el = document.getElementById("plan-file"); if (el) el.click(); } } : null;
    },
  },
  mounted() { this.q = store.route.params.q || ""; this.load(); },
  methods: {
    can, fmtN, fmtDT, fmtDays,
    async load() {
      this.err = null;
      try {
        const r = await GET(`/api/p6/activities?q=${encodeURIComponent(this.q)}&only=${this.only}`);
        this.imports = await GET("/api/p6/imports");
        this.items = r.items; this.loaded = true;
      } catch (e) { this.err = e; }
    },
    async upload() {
      this.busy = true;
      try {
        const fd = new FormData(); fd.append("file", this.file); if (this.dataDate) fd.append("data_date", this.dataDate);
        const r = await POST("/api/p6/import", fd);
        toast(`导入 ${r.activities} 项作业、${r.relations} 条关系，关键作业 ${r.critical} 项`); await loadMe(); this.load();
      } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};

// ---------------------------------------------------------------------------- 报表
// 工笔文书（用户 10-08 定名）：不带 fixed 时是函件 / 分析分区（#/reports?tab=letter|ana）或报表里的英文周报 / 月报（cat="en" only=类型），整页交给 x_docs.js 的 GongbiHome。
// 周报 / 月报（原来的生成、核验、发布流程）10-09 起与日报合成“报表”（main.js ReportHub），这里以 fixed="week|month" 嵌入。
const Reports = {
  components: typeof GongbiHome !== "undefined" ? { GongbiHome } : {},
  props: { fixed: String, cat: String, only: String },
  template: `<gongbi-home v-if="!fixed && gb" :cat="gbCat" :only="only" :key="gbCat"></gongbi-home><div v-else><page-head :page="fixed ? 'rpt' : 'reports'" :title="fixed ? (fixed === 'week' ? '周报' : '月报') : undefined"><template #actions><template v-if="legacy && can('report_gen')">
      <label class="small rp-date">包含日期 <input type="date" class="in" v-model="ref"></label>
      <button class="btn primary ai" :disabled="busy" @click="gen"><ic n="sparkles"></ic>{{ busy ? '生成中 ' + sec + 's' : '生成' + (kind === 'week' ? '周报' : '月报') }}</button>
      <help-tip text="表格数据取自已提交日志与 P6；叙述中的非统计数字由“数字守卫”标注。"></help-tip></template></template></page-head>
  <template v-if="legacy">
  <load-state class="mb" compact v-if="err || !loaded" :loading="!loaded" :error="err" @retry="load"></load-state>
  <div class="rp-split" v-if="wide && items.length"><div class="zb-card rp-list rd-rpl"><div class="zb-ch rd-rpl-h"><ic n="file"></ic><h2>{{ kname }}</h2><span class="zb-n">{{ items.length }}</span><span class="grow"></span><span class="zb-src">{{ listSrc }}</span></div><zb-paged :items="items" :size="8" :m-size="5" unit="份" cls="rd-rpl-b"><template #default="{ item: r }"><div :class="['rp-li', {on: r.id === sel}]" role="button" tabindex="0" @click="sel = r.id" @keyup.enter="sel = r.id">
      <b>{{ r.kind === 'week' ? '周报' : '月报' }} · {{ r.period_start.slice(5) }} ～ {{ r.period_end.slice(5) }}</b>
      <div class="row wrap rp-chips"><span :class="['chip', r.status === 'published' ? 'green' : '']">{{ r.status === 'published' ? '已发布' : '草稿' }}</span>
        <span :class="['chip', !r.warnings ? 'green' : 'orange']">数字核验 · {{ !r.warnings ? '通过' : r.status === 'published' ? '发布时保留 ' + r.warnings + ' 处' : r.warnings + ' 处待核' }}</span></div>
      <div class="tiny muted"><template v-if="r.gen_seconds > 0">AI 生成用时 {{ r.gen_seconds }} 秒 · </template>{{ fmtDT(r.created_at) }}</div></div></template></zb-paged></div>
    <div class="rp-detail"><component :is="rv" :id="sel" :key="sel" :embed="true" @changed="load"></component></div></div>
  <section class="zb-card rd-rpt" v-else><div class="zb-ch"><ic n="file"></ic><h2>{{ kname }}</h2><span class="zb-n" v-if="loaded">{{ items.length }}</span><span class="grow"></span><span class="zb-src">{{ listSrc }}</span></div>
    <div class="scroll-x" v-if="items.length"><zb-paged tag="table" cls="t rd-rt" :items="items" :size="12" :m-size="6" unit="份"><template #head><thead><tr><th>周期</th><th>状态</th><th>数字核验</th><th class="hide-m">生成用时</th><th class="hide-m">生成时间</th></tr></thead></template>
    <template #default="{ item: r }"><tr class="click" @click="go('report', {id: r.id})">
      <td class="nowrap rd-p"><b>{{ r.kind === 'week' ? '周报' : '月报' }}</b> {{ r.period_start }} ～ {{ r.period_end }}</td><td class="rd-st"><span :class="['chip', r.status === 'published' ? 'green' : '']">{{ r.status === 'published' ? '已发布' : '草稿' }}</span></td>
      <td class="rd-ck"><span :class="['chip', !r.warnings ? 'green' : r.status === 'published' ? '' : 'orange']">数字核验 · {{ !r.warnings ? '通过' : r.status === 'published' ? '发布时保留 ' + r.warnings + ' 处' : r.warnings + ' 处待核' }}</span></td>
      <td class="nowrap hide-m">{{ r.gen_seconds > 0 ? r.gen_seconds + ' 秒' : '—' }}</td><td class="small muted hide-m">{{ fmtDT(r.created_at) }}</td></tr></template></zb-paged></div>
    <div class="zb-empty" v-else-if="loaded"><empty-state compact :reason="emptyReason" :action="emptyAction"></empty-state></div></section></template>
  <ai-note page="reports"></ai-note></div>`,
  data: () => ({ items: [], kind: "week", ref: "", busy: false, sec: 0, loaded: false, err: null, wide: false, sel: null, gb: typeof GongbiHome !== "undefined" }),
  computed: {
    legacy() { return this.kind === "week" || this.kind === "month"; },
    // 分区：显式给的 cat，否则看地址 ?tab=，再否则按 ?kind= 的文书类型归区（旧链接 #/reports?kind=… 仍打开该类型所在分区）
    gbCat() { const p = store.route.params || {}; return this.cat || (["letter", "ana"].includes(p.tab) ? p.tab : (typeof gbTabOf !== "undefined" && gbTabOf(p.kind)) || "letter"); },
    rv() { return ReportView; },
    kname() { return this.kind === "month" ? "月报" : "周报"; },
    listSrc() { return roleOf() === "leader" ? "只列已发布" : "数字取自系统统计，AI 起草"; },
    emptyReason() { return can("report_gen") ? `还没有生成过${this.kname}。表格数据取自已提交日志和 P6，一键即可起草。` : roleOf() === "leader" ? `还没有已发布的${this.kname}。` : `还没有${this.kname}，请工程主管或项目经理先生成。`; },
    emptyAction() { return can("report_gen") ? { label: "生成" + this.kname, fn: () => this.gen() } : null; },
  },
  // 宽屏（≥1100px）左边报告列表、右边直接看正文（右侧就是报告页本身，同一套保存、核实、发布）；窄屏仍是表格，点开进报告页
  mounted() {
    const d = new Date(today()); d.setDate(d.getDate() - 1); this.ref = d.toISOString().slice(0, 10);
    this.mq = window.matchMedia("(min-width: 1100px)"); this.wide = this.mq.matches; this.onMq = (e) => { this.wide = e.matches; }; this.mq.addEventListener("change", this.onMq);
    if (this.fixed) { this.kind = this.fixed; this.load(); }
  },
  unmounted() { if (this.mq) this.mq.removeEventListener("change", this.onMq); },
  methods: {
    go, can, fmtDT,
    async load() {
      this.err = null;
      try { this.items = (await GET("/api/reports")).filter((r) => !this.fixed || r.kind === this.fixed); this.loaded = true; if (!this.items.some((r) => r.id === this.sel)) this.sel = this.items.length ? this.items[0].id : null; } catch (e) { this.err = e; }
    },
    async gen() {
      this.busy = true; this.sec = 0; const t = setInterval(() => this.sec++, 1000);
      try { const r = await POST("/api/reports", { kind: this.kind, ref_date: this.ref }); go("report", { id: r.id }); }
      catch (e) { toast(e.message, true); } finally { clearInterval(t); this.busy = false; }
    },
  },
};

// 周报 / 月报详情（全站一屏化 10-10）：页头（标题、流程、下载/保存/审核发布）和数字守卫固定，正文放在固定高度阅读面板里滚动；
// 电脑端目录在右侧（嵌在列表右侧时为面板上方一行），点目录在面板内定位；手机按节切换（目录一行可横滑 + 上一节/下一节），整页不超过约 2 屏。
// 阅读面板里的是文书正文：不截断，按公文排版。
const ReportView = {
  props: ["id", "embed"],   // embed：嵌在周报/月报页右侧，不出“返回”和边界说明
  emits: ["changed"],
  template: `<div v-if="r" :class="['rv', 'rd-fx', {'rv-embed': embed, 'rv-pg': pager}]"><page-head page="report" :title="title" steps="report" :current="step"><template #meta><a v-if="!embed" @click="go('rpt', {seg: r.kind})">← 返回{{ r.kind === 'week' ? '周报' : '月报' }}列表</a><span class="rv-meta">{{ r.period_start }} ～ {{ r.period_end }} · 依据日志 {{ f.logs.count }} 份<template v-if="f.logs.expected !== undefined">（应报 {{ f.logs.expected }}）</template> · 照片 {{ f.photos }} 张<template v-if="r.gen_seconds > 0"> · 生成 {{ r.gen_seconds }} 秒</template> · 按项目模板撰写</span></template>
    <template #actions><a class="btn" :href="'/api/reports/' + r.id + '/docx'">下载 Word</a>
      <template v-if="can('report_gen') && r.status !== 'published'"><button class="btn" @click="save('draft')">保存</button><button class="btn primary" v-if="can('report_publish')" :disabled="numsLeft > 0" :title="numsLeft > 0 ? '还有 ' + numsLeft + ' 处数字没核实' : null" @click="save('published')">审核发布</button></template></template></page-head>
  <div class="banner warn rv-bn" v-if="nums.length && r.status !== 'published'">数字守卫：以下数字未出现在统计数据中，请核实或删除<template v-if="can('report_publish')">；逐条核实勾选后才能发布</template>：
    <span v-for="n in nums" :key="n.field + n.number" class="nowrap" style="margin-right:10px"><b>{{ secTitle(n.field) }}「{{ n.number }}」</b>
      <label class="tiny" v-if="can('report_publish')"> <input type="checkbox" v-model="checked[n.field + n.number]">已核实</label></span></div>
  <div class="banner info rv-bn" v-else-if="nums.length">数字守卫：发布时保留了 {{ nums.length }} 处统计数据里没有的数字：<b v-for="n in nums" :key="n.field + n.number">{{ secTitle(n.field) }}「{{ n.number }}」 </b></div>
  <div class="banner warn rv-bn" v-if="tplIssues.length">模板检查：<span v-for="n in tplIssues" :key="n.field">{{ secTitle(n.field) }} {{ n.issue }}；</span></div>
  <div :class="['rv-wrap', {'rv-side': !embed}]">
  <nav class="rv-toc" v-if="toc.length > 1" aria-label="报表目录"><div class="rv-toc-h"><ic n="file"></ic>报表目录</div>
    <div class="rv-toc-l"><a v-for="t in toc" :key="t.k" :class="{on: t.k === curK}" role="button" tabindex="0" :aria-current="t.k === curK ? 'true' : null" @click="jump(t.k)" @keyup.enter="jump(t.k)">{{ t.t }}</a></div></nav>
  <div class="rv-main"><div class="rv-read" v-fit="220" ref="read" @scroll.passive="spy">
  <template v-for="sec in r.template.sections" :key="sec.key">
  <div class="zb-card rv-sec" :id="aid(sec.key)" v-show="!pager || curK === sec.key">
    <div class="zb-ch"><h2>{{ sec.title }}</h2><span class="grow"></span><span class="tiny muted" v-if="sec.type === 'narrative'">{{ (nar[sec.key] || '').length }}/{{ sec.max_chars }} 字</span></div>
    <template v-if="editable"><div class="tiny muted mb">写作要求：{{ sec.instruction }}</div>
    <textarea v-if="sec.type === 'narrative'" class="in" rows="3" v-model="nar[sec.key]" :readonly="r.status === 'published' || !can('report_gen')"></textarea>
    <textarea v-else class="in" rows="2" v-model="lists[sec.key]" :readonly="r.status === 'published' || !can('report_gen')" placeholder="每行一条"></textarea></template>
    <template v-else><p class="rv-p" v-if="sec.type === 'narrative'" v-html="prose(sec.key, nar[sec.key])"></p>
      <ol class="rv-ol" v-else><li v-for="(x, i) in listItems(sec.key)" :key="i">{{ x }}</li></ol></template>
    <div class="scroll-x mt" v-if="r.tables[sec.key] && r.tables[sec.key].rows.length"><table class="t rv-t"><thead><tr><th v-for="h in r.tables[sec.key].headers" :key="h">{{ h }}</th></tr></thead>
      <tbody><tr v-for="(row, i) in r.tables[sec.key].rows" :key="i"><td v-for="(c, j) in row" :key="j" :data-l="r.tables[sec.key].headers[j]" :class="{nw: String(c).length <= 12}">{{ c }}</td></tr></tbody></table></div>
    <div class="tiny muted trust-note" v-if="r.tables[sec.key] && r.tables[sec.key].headers.some((h) => /自报|核定/.test(h))"><trust-tag kind="self"></trust-tag>分包提交、待港湾审核的数量；<trust-tag kind="approved"></trust-tag>港湾已审核通过的数量；合计为两者之和，被退回的不计入。<span v-if="sec.table === 'output'">产值为内部产值统计（非计量认证）。</span></div></div>
  <section class="zb-card rv-k10" :id="aid('k10')" v-if="r.kpi10 && k10After === sec.key" v-show="!pager || curK === 'k10'"><div class="zb-ch"><ic n="chart"></ic><h2>产值与进度</h2><span class="zb-src">公司周报十项指标</span></div><kpi10 :d="r.kpi10" mode="report"></kpi10></section>
  </template>
  <section class="zb-card rv-media" :id="aid('media')" v-show="!pager || curK === 'media'"><div class="zb-ch"><ic n="camera"></ic><h2>现场影像</h2><span class="zb-n">{{ photos.length || f.photos || 0 }}</span><span class="zb-src">本期已提交日志所附照片</span></div>
    <div class="ev-grid rv-ev" v-if="photos.length"><ev-photo v-for="p in photos" :key="p.id" :src="p.thumb" :full="p.url" size="f" :time="p.time">{{ p.caption ? (p.time ? ' · ' : '') + p.caption : '' }}</ev-photo></div>
    <div class="zb-empty" v-else>{{ f.photos ? '本期已提交日志附照片 ' + f.photos + ' 张；报表暂未附带照片清单，接入后在这里展示。' : '本期已提交日志没有附照片。' }}</div></section>
  </div>
  <div class="rv-pgn" v-if="pager && toc.length > 1"><button type="button" class="btn" :disabled="idx <= 0" @click="move(-1)">上一节</button><span class="tiny muted">{{ idx + 1 }} / {{ toc.length }}</span><button type="button" class="btn" :disabled="idx >= toc.length - 1" @click="move(1)">下一节</button></div>
  </div></div>
  <ai-note page="reports" v-if="!embed"></ai-note></div><load-state v-else loading :error="err" @retry="load"></load-state>`,
  data: () => ({ r: null, err: null, nar: {}, lists: {}, checked: {}, cur: "", pager: false }),
  computed: {
    // 能改稿（草稿 + 有起草权限）时是编辑框；已发布或只读时按公文排版直接阅读
    editable() { return this.r.status !== "published" && can("report_gen"); },
    f() { return this.r.facts; },
    title() { return (this.r.template.title || "{project} 施工报告").replace("{project}", this.f.project); },
    nums() { return this.r.number_check.filter((x) => x.number); },
    tplIssues() { return this.r.number_check.filter((x) => x.issue); },
    step() { return reportStep(this.r, this.nums.length + this.tplIssues.length); },
    // U-18：有待核数字时逐条勾“已核实”（或改稿删掉）才能发布；已发布的不再要求
    numsLeft() { return this.r.status === "published" ? 0 : this.nums.filter((n) => !this.checked[n.field + n.number]).length; },
    // 设计 22：十项指标作为“产值与进度”一节，放在产值节之后（模板没有产值节时放在最后一节之后）；月报没有十项指标
    k10After() {
      const s = this.r.template.sections; if (!this.r.kpi10 || !s.length) return "";
      const o = s.find((x) => x.table === "output"); return (o || s[s.length - 1]).key;
    },
    // 现场影像：报表接口给出照片清单（r.photos：id、thumb、url、time、caption）时按 4:3 展示；没给就只写张数，不放示意图
    photos() { return Array.isArray(this.r.photos) ? this.r.photos : []; },
    toc() {
      const out = [];
      for (const s of this.r.template.sections) {
        out.push({ k: s.key, t: s.title.replace(/^[一二三四五六七八九十]+、/, "") });
        if (s.key === this.k10After) out.push({ k: "k10", t: "产值与进度" });
      }
      out.push({ k: "media", t: "现场影像" });
      return out;
    },
    curK() { return this.toc.some((t) => t.k === this.cur) ? this.cur : (this.toc[0] || {}).k; },
    idx() { return Math.max(0, this.toc.findIndex((t) => t.k === this.curK)); },
  },
  mounted() {
    // 手机（≤760px）按节切换；电脑端整篇在阅读面板里滚动
    this.mq = window.matchMedia("(max-width: 760px)"); this.pager = this.mq.matches; this.onMq = (e) => { this.pager = e.matches; }; this.mq.addEventListener("change", this.onMq);
    this.load();
  },
  unmounted() { if (this.mq) this.mq.removeEventListener("change", this.onMq); },
  methods: {
    go, can,
    aid(k) { return "rv" + this.r.id + "-" + k; },
    // 目录定位：手机切到该节，目录行滚回视口；电脑端在阅读面板内滚到该节（面板没限高时滚动页面）
    jump(k) {
      this.cur = k; this.lock = Date.now();
      this.$nextTick(() => {
        const toc = this.$el.querySelector ? this.$el.querySelector(".rv-toc") : null, on = toc && toc.querySelector(".rv-toc-l a.on");
        if (this.pager) {
          if (on) on.scrollIntoView({ block: "nearest", inline: "center" });
          if (toc && toc.getBoundingClientRect().top < 0) toc.scrollIntoView({ block: "start" });
          return;
        }
        const p = this.$refs.read, el = document.getElementById(this.aid(k)); if (!p || !el) return;
        if (p.scrollHeight > p.clientHeight + 2) p.scrollTo({ top: Math.max(0, el.offsetTop - 2), behavior: "smooth" });
        else el.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    },
    move(d) { const t = this.toc[this.idx + d]; if (t) this.jump(t.k); },
    // 阅读面板滚动时目录跟着高亮当前节（点目录后的平滑滚动期间不抢）
    spy() {
      if (this.pager || Date.now() - (this.lock || 0) < 900) return;
      const p = this.$refs.read; if (!p) return;
      let k = "";
      for (const t of this.toc) { const el = document.getElementById(this.aid(t.k)); if (el && el.offsetTop - 24 <= p.scrollTop) k = t.k; }
      if (p.scrollTop + p.clientHeight >= p.scrollHeight - 4 && this.toc.length) k = this.toc[this.toc.length - 1].k;
      this.cur = k || (this.toc[0] || {}).k || "";
    },
    async load() { this.err = null; try { this.apply(await GET(`/api/reports/${this.id}`)); } catch (e) { this.err = e; } },
    secTitle(k) { const s = this.r.template.sections.find((x) => x.key === k); return s ? s.title : k; },
    listItems(k) { return (this.lists[k] || "").split("\n").filter((x) => x.trim()); },
    // 阅读排版：先整体转义，再把引用编号（[L333][S14]…）做成小标签、把数字守卫留下的数字标黄（只处理转义后的纯文本，不放行任何 HTML）
    prose(key, text) {
      let h = String(text || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
      h = h.replace(/\[([A-Z]{1,2}\d+)\]/g, '<span class="rv-cite">$1</span>');
      for (const n of this.nums.filter((x) => x.field === key)) {
        const num = String(n.number).replace(/[^0-9.]/g, "").replace(/\./g, "\\.");
        if (num) h = h.replace(new RegExp("(^|[^\\d.])(" + num + ")(?![\\d.])"), '$1<mark class="rv-guard" title="数字守卫：统计数据里没有这个数字">$2</mark>');
      }
      return h;
    },
    apply(r) {
      this.r = r; this.nar = {}; this.lists = {};
      r.template.sections.forEach((s) => { if (s.type === "list") this.lists[s.key] = (r.narrative[s.key] || []).join("\n"); else this.nar[s.key] = r.narrative[s.key] || ""; });
    },
    async save(status) {
      const narrative = { ...this.nar };
      Object.entries(this.lists).forEach(([k, v]) => (narrative[k] = v.split("\n").filter((x) => x.trim())));
      const verified = status === "published" ? this.nums.filter((n) => this.checked[n.field + n.number]).map((n) => ({ field: n.field, number: n.number })) : [];
      try { this.apply(await PUT(`/api/reports/${this.id}`, { narrative, status, verified })); toast(status === "published" ? "已发布" : "已保存"); this.$emit("changed"); } catch (e) { toast(e.message, true); }
    },
  },
};

// ---------------------------------------------------------------------------- 问工笔
const ASK_SAMPLES = {
  scribe: ["最近的施工日志记录了哪些作业？", "上周现场投入了多少人？"],
  safety: ["最近记录了哪些安全隐患？", "有哪些安全问题还没整改？"],
  quality: ["最近记录了哪些质量问题？", "近期有哪些工序需要验收或检测？"],
  dq: ["哪些日志的数据不完整或有异常？"],
  progress: ["本周有哪些关键作业滞后，原因是什么？", "哪些进度预警需要关注？"],
  output: ["最近完成了哪些工程量？"],
  report: ["上周的主要进展和问题是什么？"],
  brief: ["今天最需要关注的事项有哪些？"],
  chaser: ["这周催了谁，哪些还没处理？", "哪些事项已经升级到主管？"],
  dispatch: ["上次例会的行动项完成了几条？", "哪些行动项已经超期？"],
  archivist: ["最近有哪些停工或缺料事件？", "哪些部位已经有档案概况？"],
  circle: ["最近智能体在现场圈点评了什么？", "最近的 AI 精选有哪些？"],
  gate: ["未来两周哪些作业还不能开工？", "哪些开工条件还缺？"],
  material: ["哪些材料还没复试？", "哪些试块快到 28 天了？"],
  records: ["哪些隐蔽验收还缺照片？", "哪些作业疑似缺验收资料？"],
};
// 各 Agent 问答实际查询的数据（与后端 services/qa.py 的 SCOPES 一致）
const ASK_SCOPE = {
  scribe: "施工日志的内容与整理情况",
  safety: "安全隐患台账：隐患、等级、责任人、期限、整改与 AI 复核结论",
  quality: "质量问题与质控提醒台账，以及日志中记录的质量类问题",
  dq: "日志数据审核结果：预审评分和发现的问题",
  progress: "P6 计划、进度预警、关键作业滞后及原因",
  output: "周产值（实际与计划）、月末预测和产值分析",
  report: "已生成的周报、月报内容",
  brief: "今日简报与进度预警里需要关注的事项",
  chaser: "催办记录：提醒了谁、到第几级、延后情况和事项是否已处理",
  dispatch: "已确认的例会行动项和一句话指令派单的记录",
  archivist: "从日志归并出的停工、缺料、故障等索赔事件和部位档案概况",
  circle: "现场圈里智能体的点评、回答和 AI 精选理由",
  gate: "未来 14 天要开工作业的放行清单：开工结论和缺项",
  material: "材料票据、疑点、试块到期和复试台账",
  records: "隐蔽验收记录和疑似缺验收资料的作业",
};
const ASK_DEFAULT_SAMPLES = ["本周有哪些关键作业滞后，原因是什么？", "最近记录了哪些设备故障？", "上周现场投入了多少人？"];

const Ask = {
  props: ["agent"],
  template: `<div class="ra-askp">
  <section class="zb-card ra-chatc"><div class="zb-ch"><ic n="message"></ic><h2>对话</h2><span class="zb-n" v-if="msgs.length">{{ msgs.length }}</span><span class="grow"></span>
      <span class="zb-src" :title="scopeLine">查询范围</span><span class="ra-scope" :title="scopeLine">{{ scopeLine }}</span></div>
  <div class="zb-body"><div class="chat col">
    <template v-for="(m, i) in msgs" :key="i"><div class="q">{{ m.q }}</div>
      <div class="a" v-if="m.a"><span v-for="(seg, j) in split(m.a.answer)" :key="j"><span v-if="seg.ref" class="ref" @click="openRef(seg.ref)">{{ seg.ref }}</span><template v-else>{{ seg.t }}</template></span>
        <div class="tiny muted mt">检索 {{ m.a.facts_count }} 条记录<span v-if="m.a.plan.date_from"> · {{ m.a.plan.date_from }}~{{ m.a.plan.date_to }}</span><span v-if="m.a.plan.keywords.length"> · 关键词 {{ m.a.plan.keywords.join('、') }}</span></div>
        <div class="row wrap mt ask-pin" v-if="canPin()"><button class="btn sm" :disabled="!!m.pinned" @click="pin(m, '')"><ic n="pin"></ic>{{ m.pinned ? '已固定' : '固定' }}</button>
          <select class="in" :value="m.sub || ''" @change="pin(m, $event.target.value)" title="订阅后按周期自动重跑并通知">
            <option value="">{{ m.sub ? '取消订阅' : '订阅更新' }}</option><option value="daily">每天</option><option value="mon">每周一</option><option value="fri">每周五</option></select>
          <span class="tiny muted" v-if="m.sub">已订阅，按周期自动更新并通知</span></div></div>
      <div class="a thinking" v-else-if="m.err" style="color:var(--red)">{{ m.err }}</div>
      <div class="a thinking" v-else><div class="spinner"></div>检索中</div></template>
    <div class="ra-samples" v-if="!msgs.length"><div class="tiny muted">可以这样问：</div><button class="zb-chip" v-for="s in samples" :key="s" @click="q = s; send()"><ic n="search"></ic>{{ s }}</button></div>
  </div></div></section>
  <div class="bottom-bar ra-askbar"><input class="in grow" v-model="q" @keyup.enter="send" placeholder="例：上周关键作业滞后原因"><button class="btn primary" @click="send" :disabled="!q.trim()">发送</button></div></div>`,
  mounted() { const q = store.route.params.q; if (q) { this.q = q; this.send(); } },
  data: () => ({ q: "", msgs: [] }),
  computed: { scopeText() { return this.agent ? ASK_SCOPE[this.agent.key] : ""; }, samples() { return (this.agent && ASK_SAMPLES[this.agent.key]) || ASK_DEFAULT_SAMPLES; },
    // 查询范围按最近一次回答实际检索的范围写（问到安全、质量时含对应台账）
    scopeLine() { const last = [...this.msgs].reverse().find((m) => m.a && m.a.scope); return last ? last.a.scope : this.agent && this.scopeText ? this.scopeText : "项目日志、P6 进度与预警"; } },
  methods: {
    async send() {
      const q = this.q.trim(); if (!q) return; this.q = "";
      const m = reactive({ q, a: null, err: "" }); this.msgs.push(m);
      try { m.a = await POST("/api/ask", { question: q, agent: this.agent ? this.agent.key : null }); } catch (e) { m.err = e.message; }
    },
    split(text) {
      const out = []; let last = 0; const re = /\[([A-Za-z]+\d+)\]/g; let r;
      while ((r = re.exec(text))) { if (r.index > last) out.push({ t: text.slice(last, r.index) }); out.push({ ref: r[1] }); last = re.lastIndex; }
      if (last < text.length) out.push({ t: text.slice(last) });
      return out;
    },
    canPin() { return ["manager", "eng_sup", "safety_sup"].includes(roleOf()); },
    // 固定到首页 / 订阅（x_nudge 的“我的看板”显示）；分包单位与领导不开放
    async pin(m, schedule) {
      if (!m.a) return;
      try {
        const p = await POST("/api/nudge/pins", { question: m.q, schedule, answer: m.a });
        m.pinned = true; m.sub = p.schedule;
        toast(schedule ? `已订阅（${p.schedule_name}），结果将通知并显示于首页看板` : "已固定到首页看板");
        if (typeof NUDGE !== "undefined") NUDGE.bump();
      } catch (e) { toast(e.message, true); }
    },
    openRef(ref) { if (/^L\d+$/.test(ref)) go("log", { id: ref.slice(1) }); else if (/^W\d+$/.test(ref)) go("alert", { id: ref.slice(1) }); else if (/^F\d+$/.test(ref)) go("finding", { id: ref.slice(1) }); else if (/^R\d+$/.test(ref)) go("report", { id: ref.slice(1) }); else if (/^C\d+$/.test(ref)) go("contract", { id: ref.slice(1) }); else go("progress", { tab: "plan", q: ref }); },
  },
};

// ---------------------------------------------------------------------------- 成效
const Metrics = {
  // 10-10 一屏化：指标一张卡（5 列、说明 2 行；仿真提示收进卡头一行）；卡底一行放历史回测、大模型调用、人工对照基线（后两项点开在抽屉里看/录入）与口径说明；
  // 下面接 AgentsHub 里的“AI 产出被人采纳情况”（表格在面板内滚动）。
  template: `<div v-if="m" class="ra-met">
  <section class="zb-card ra-mk-card"><div class="zb-ch"><ic n="clock"></ic><h2>记录、草稿与预警</h2><span class="grow"></span>
    <span class="ra-sim ra-sim-top" v-if="sim"><ic n="info"></ic><span><b>仿真项目</b>：本页数字不计入应用成效，不能作为试点或成效数据对外表述；不计算节省比例</span></span><span class="zb-src" v-else>系统记录 · 样本不足不下结论</span></div>
  <div class="zb-body"><div class="ra-mk">
    <div class="ra-mk-i"><b>{{ fmtN(dl.week_minutes.median) }}<small> 分钟</small></b><span>{{ dl.cadence === 'week' ? '每人每周记录耗时' : '每份日志耗时' }}中位数（n={{ dl.week_minutes.n }}）<template v-if="dl.cadence === 'week'"> · 随手记均 {{ fmtN(dl.note_seconds.median) }} 秒/条</template></span></div>
    <div class="ra-mk-i"><b>{{ fmtN(dl.baseline_week_minutes) }}<small> 分钟</small></b><span>原方式{{ dl.cadence === 'week' ? '每周' : '每份' }}耗时（手写日志 {{ fmtN(dl.manual_minutes.median) }} 分钟 × {{ dl.cadence === 'week' ? dl.workdays_per_week + ' 天' : '1' }}{{ dl.baseline_report_minutes ? ' + 手工周报 ' + dl.baseline_report_minutes + ' 分钟' : '' }}，n={{ dl.manual_minutes.n }}）</span></div>
    <div class="ra-mk-i"><b>{{ sim || dl.time_saving === null ? '—' : pct(dl.time_saving) }}</b><span>{{ sim ? '记录时间节省：仿真项目不计算' : dl.time_saving === null ? dl.time_saving_note : '记录时间节省（与手工对照同口径）' }}</span></div>
    <div class="ra-mk-i" v-if="'on_time_rate' in dl"><b>{{ dl.submitted >= 5 ? pct(dl.on_time_rate) : '—' }}</b><span>按时确认率（<template v-if="dl.submitted < 5">样本不足，</template>n={{ dl.submitted }} 份）· 随手记 {{ dl.notes }} 条</span></div>
    <div class="ra-mk-i"><b>{{ dl.edit_ratio.n >= 5 && dl.edit_ratio.median !== null ? pct(dl.edit_ratio.median) : '—' }}</b><span>AI 草稿修改率中位数（<template v-if="dl.edit_ratio.n < 5">样本不足，</template>n={{ dl.edit_ratio.n }}）</span></div>
    <div class="ra-mk-i"><b>{{ dl.edit_ratio.n >= 5 && dl.adopted_rate !== null ? pct(dl.adopted_rate) : '—' }}</b><span>草稿采纳率（修改率≤30% 的份数占比，<template v-if="dl.edit_ratio.n < 5">样本不足，</template>n={{ dl.edit_ratio.n }}）</span></div>
    <div class="ra-mk-i"><b>{{ fmtN(m.reports.gen_seconds.median) }}<small> 秒</small></b><span>周/月报生成用时中位数（n={{ m.reports.count }} 份）</span></div>
    <div class="ra-mk-i"><b>{{ fbN >= 5 && m.alerts.feedback_precision !== null ? pct(m.alerts.feedback_precision) : '—' }}</b><span>预警反馈命中率，不是准确率：确认属实 {{ m.alerts.confirmed }} / 误报 {{ m.alerts.false_alarm }}（<template v-if="fbN < 5">样本不足，</template>n={{ fbN }}）· {{ m.alerts.gap_note }}</span></div>
    <div class="ra-mk-i"><b>{{ m.alerts.data_gaps_now === null ? '—' : m.alerts.data_gaps_now }}</b><span>当前数据缺口作业数（没报，不是落后）· 历史旧预警 {{ m.alerts.data_gaps_history }} 条 · 不计入预警命中率</span></div>
  </div>
  <div class="ra-mk-f"><div class="ra-bt">历史回测：<template v-if="bt">{{ btLine }} <a :href="bt.link">查看</a></template>
    <template v-else>还没有真实项目数据的回测，这里不显示回测数字。<a v-if="canBacktest" href="#/backtest">去上传多期 XER</a></template></div>
    <button class="zb-link" @click="side = 'ai'">大模型调用 {{ m.ai_runs.length }} 类<ic n="chevron"></ic></button><button class="zb-link" @click="side = 'base'">人工对照基线 {{ m.baselines.length }} 条<ic n="chevron"></ic></button>
    <details class="defs"><summary class="small zb-link">口径说明</summary><div class="ra-defs-pop"><div v-for="(v, k) in m.definitions" :key="k">{{ k }}：{{ v }}</div></div></details></div></div></section>
  <zb-drawer v-if="side === 'ai'" :title="'大模型调用 · ' + m.ai_runs.length + ' 类'" wide @close="side = ''"><template #head><span class="zb-src">调用留痕</span></template>
    <zb-paged :items="m.ai_runs" :size="12" :m-size="6" :fit="false" tag="table" cls="t ra-rt" unit="类"><template #head><thead><tr><th>用途</th><th v-if="multiProvider">模型来源</th><th class="right">次数</th><th class="right">成功</th><th class="right">平均耗时</th></tr></thead></template>
    <template #default="{ item: r }"><tr><td class="ra-rt-h"><span :title="r.label ? null : '内部调用类型，尚无中文名'">{{ r.label || r.kind }}</span></td><td v-if="multiProvider" data-l="来源">{{ r.provider === 'jiaorong' ? '交融' : '模拟' }}</td><td class="right" data-l="次数">{{ r.n }}</td><td class="right" data-l="成功">{{ r.ok }}</td><td class="right" data-l="平均耗时">{{ r.avg_latency_s }} 秒</td></tr></template>
      <template #empty><div class="zb-empty">还没有大模型调用记录。</div></template></zb-paged>
    <div class="small mt"><template v-if="!multiProvider && m.ai_runs.length">模型来源均为{{ m.ai_runs[0].provider === 'jiaorong' ? '交融' : '离线模拟模型' }} · </template>活跃用户 {{ m.usage.active_users }} 人 · 现场照片 {{ m.usage.photos }} 张</div></zb-drawer>
  <zb-drawer v-if="side === 'base'" :title="'人工对照基线 · ' + m.baselines.length + ' 条'" wide @close="side = ''"><template #head><span class="zb-src">人工填报</span></template>
    <div class="tiny muted">记录同岗位不使用系统时的实际用时，与系统数据对比。<template v-if="!canBase">由系统管理员录入。</template></div>
      <div class="ra-form mt" v-if="canBase"><select class="in" v-model="b.kind"><option value="report_week">手工编周报</option><option value="report_month">手工编月报</option><option value="log_collect">收集汇总日志</option><option value="p6_update">手工更新P6进度</option></select>
        <input class="in ra-num" type="number" v-model.number="b.minutes" placeholder="分钟"><input class="in ra-who" v-model="b.who" placeholder="填报人">
        <input class="in grow" v-model="b.note" placeholder="说明"><button class="btn" @click="addBase">记录</button></div>
      <zb-paged class="mt" v-if="m.baselines.length" :items="m.baselines" :size="12" :m-size="6" :fit="false" tag="table" cls="t ra-rt"><template #default="{ item: x }"><tr><td class="ra-rt-h">{{ kinds[x.kind.replace('baseline.', '')] }}</td><td data-l="用时">{{ x.minutes }} 分钟</td><td class="small" data-l="填报">{{ x.who }} {{ x.note }}</td><td class="tiny muted" data-l="时间">{{ fmtDT(x.at) }}</td></tr></template></zb-paged>
      <div class="zb-empty" v-else>还没有人工对照记录，节省比例不计算。</div></zb-drawer></div>
  <load-state v-else :loading="!err" :error="err" @retry="load"></load-state>`,
  data: () => ({ m: null, err: null, side: "", b: { kind: "report_week", minutes: null, who: "", note: "" },
    kinds: { report_week: "手工编周报", report_month: "手工编月报", log_collect: "收集汇总日志", p6_update: "手工更新P6进度" },
    kindName: { "log.draft": "日志生成", "alert.explain": "预警分析", "report.week": "周报", "report.month": "月报", "qa.plan": "问答规划", "qa.answer": "问答回答", "ai.test": "连通测试" } }),
  computed: {
    dl() { return this.m.daily_log; },
    sim: () => isSim(),
    fbN() { return (this.m.alerts.confirmed || 0) + (this.m.alerts.false_alarm || 0); },   // 预警人员反馈样本数（确认属实 + 误报）
    canBase: () => roleOf() === "admin",   // 人工对照基线只有系统管理员能录（后端 metrics_ops），其他岗位不放假按钮
    multiProvider() { return new Set((this.m.ai_runs || []).map((r) => r.provider)).size > 1; },
    // 历史回测只留一行：只引用“真实项目数据”的最新一次（后端 metrics_brief），完整内容在 #/backtest（同一信息只一个完整展示位）
    bt() { return this.m.backtest || null; },
    canBacktest: () => roleOf() === "eng_sup",
    btLine() {
      const b = this.bt, kn = b.precision_n ? `（${b.precision_k}/${b.precision_n}）` : "";
      const prec = b.precision !== null && b.precision !== undefined ? `${(b.precision * 100).toFixed(1)}%${kn}` : (b.precision_n ? `样本不足${kn}` : "无预警样本");
      const out = [`回测于 ${String(b.at || "").slice(5, 10)}（${b.source_note || "真实数据，不在仿真时间线内"}）`, `${b.periods} 期`, `样本 ${b.samples}`, `命中率 ${prec}`];
      if (b.recall !== null && b.recall !== undefined) out.push(`召回率 ${(b.recall * 100).toFixed(1)}%`);
      out.push(`数据缺口 ${b.data_gaps} 个不计入`);
      if (b.weak) out.push("只有 2 期，弱口径");
      return out.join(" · ");
    },
  },
  mounted() { this.load(); },
  methods: {
    fmtN, pct, fmtDT,
    async load() { this.err = null; try { this.m = await GET("/api/metrics"); } catch (e) { this.err = e; } },
    async addBase() {
      try { await POST("/api/metrics/baseline", this.b); this.b.minutes = null; this.b.note = ""; toast("已记录"); this.load(); } catch (e) { toast(e.message, true); }
    },
  },
};

/// 设置页当前分区：催办规则（x_nudge.js 的 NudgeRules，挂在本页底部插槽）按它决定显示，只在“催办规则”分区出现
const SETTINGS_UI = reactive({ sec: "" });
const Settings = {
  template: `<div class="ra-set"><page-head :page="isAdmin ? 'settings_admin' : 'settings_user'"><template #meta>{{ isAdmin ? '系统配置：改的是规则与模板，不涉及业务审批、报表生成与发布' : '本人账号设置' }}</template>
    <template #actions><div class="seg"><button v-for="t in secs" :key="t[0]" :class="{on: sec === t[0]}" @click="sec = t[0]">{{ t[1] }}</button></div></template></page-head>
  <section class="zb-card ra-sec" v-if="sec === 'ai'"><div class="zb-ch"><ic n="bot"></ic><h2>交融大模型</h2><span class="grow"></span><a class="zb-link" v-if="isAdmin" @click="go('aimgr')">模型与额度 <ic n="chevron"></ic></a></div>
    <div class="zb-body"><div class="ra-kv"><span>状态</span><b :style="{color: ai.ready ? 'var(--green)' : 'var(--red)'}">{{ ai.ready ? '就绪' : '未就绪' }}</b><span class="small">{{ ai.label }}<span v-if="ai.reason" style="color:var(--red)"> · {{ ai.reason }}</span></span></div>
    <div class="ra-kv"><span>模型</span><span class="small">视觉模型 {{ ai.vision_model || '—' }} · 文本模型 {{ ai.text_model || '—' }}（于服务器 .env 配置）</span></div>
    <div class="zb-btns mt" v-if="isAdmin"><button class="btn" :disabled="testing" @click="test">{{ testing ? '测试中…' : '连通测试' }}</button><span class="small">{{ testResult }}</span></div></div></section>
  <section class="zb-card ra-sec" v-if="isAdmin && sec === 'project'"><div class="zb-ch"><ic n="building"></ic><h2>项目</h2></div>
    <div class="zb-body"><div class="ra-fg"><label class="field"><span class="f">项目名称</span><input class="in" v-model="pname"></label>
      <label class="field"><span class="f">P6 日历每周工作天数</span><input class="in ra-num" type="number" min="5" max="7" v-model.number="wpw"></label>
      <label class="field"><span class="f">记录节奏</span><select class="in" v-model="cadence"><option value="week">随手记 + 按周确认</option><option value="day">每日提交日志</option></select></label></div>
    <label class="ra-ck"><input type="checkbox" v-model="dailyLog"> 自动整理每日施工日志</label>
    <label class="ra-ck" title="关注/粉丝、收藏、转发、私信、划重点；点赞、评论、Agent 点评与公告不受影响"><input type="checkbox" v-model="socialEnabled"> 开启现场圈社交层（默认关闭）</label>
    <div class="zb-btns mt"><button class="btn primary" @click="saveTh">保存</button></div>
    <div class="ra-sub" v-if="claimRule"><h3>索赔通知期限</h3><div class="ra-fg"><label class="field"><span class="f">索赔通知天数</span><input class="in ra-num" type="number" min="1" max="365" v-model.number="claimRule.days"></label>
      <label class="field"><span class="f">依据</span><input class="in" v-model="claimRule.basis" :placeholder="'默认 ' + claimRule.default + ' 天，' + claimRule.clause"></label></div>
      <div class="zb-btns"><button class="btn" @click="saveClaimRule">保存</button><span class="tiny muted">索赔通知期限 = 知悉日 + 通知天数，按合同条款填写</span></div></div></div></section>
  <section class="zb-card ra-sec" v-if="isAdmin && sec === 'feat'"><div class="zb-ch"><ic n="layers"></ic><h2>增强能力</h2><span class="zb-n">{{ feats.filter((f) => f.enabled).length }}/{{ feats.length }}</span><help-tip>默认全部启用。隐藏后：对应入口不再出现在导航里，首页待办里也不再出现它的条目；已有数据不删除，旧链接（如通知里的链接）仍可直接打开，随时可重新启用。</help-tip></div>
    <div class="zb-body">
    <label class="feat-row ra-feat" v-for="f in feats" :key="f.key"><input type="checkbox" v-model="f.enabled"> <b>{{ f.name }}</b> <span class="small muted">{{ f.desc }}</span></label>
    <div class="zb-btns mt"><button class="btn primary" @click="saveTh">保存</button></div></div></section>
  <template-editor v-if="isAdmin && sec === 'tpl'"></template-editor>
  <section class="zb-card ra-sec" v-if="isAdmin && sec === 'th'"><div class="zb-ch"><ic n="alert"></ic><h2>预警阈值</h2></div>
    <div class="zb-body"><div class="ra-fg"><label class="field" v-for="(v, k) in th" :key="k"><span class="f">{{ thName[k] || k }}</span><input class="in" type="number" step="0.1" v-model.number="th[k]"></label></div>
    <div class="zb-btns mt"><button class="btn primary" @click="saveTh">保存阈值</button></div></div></section>
  <section class="zb-card ra-sec" v-if="sec === 'pw'"><div class="zb-ch"><ic n="user"></ic><h2>修改密码</h2></div><div class="zb-body"><div class="ra-fg">
    <label class="field"><span class="f">原密码</span><input class="in" type="password" v-model="pw.old" autocomplete="current-password"></label>
    <label class="field"><span class="f">新密码（至少 6 位）</span><input class="in" type="password" v-model="pw.new" autocomplete="new-password"></label></div>
    <div class="zb-btns"><button class="btn primary" @click="changePw">修改</button></div></div></section></div>`,
  data: () => ({ sec: "ai", th: {}, feats: [], testing: false, testResult: "", pw: { old: "", new: "" }, pname: "", wpw: 7, cadence: "week", dailyLog: true, socialEnabled: false, claimRule: null,
    thName: { critical_float_days: "关键作业浮时上限(天)", red_slip_days: "红色：关键作业滞后≥(天)", orange_float_ratio: "橙色：浮时消耗比例≥", orange_min_days: "橙色：最少滞后(天)",
      yellow_slip_days: "黄色：滞后≥(天)", start_grace_days: "应开未开宽限(天)", stale_days: "关键作业无进度(天)", risk_lookback_days: "日志风险回看(天)" } }),
  computed: { ai() { return store.me.ai; }, isAdmin() { return store.me.user.role === "admin"; },
    secs() { return this.isAdmin ? [["ai", "交融大模型"], ["project", "项目"], ["feat", "增强能力"], ["tpl", "报表模板"], ["th", "预警阈值"], ...(featOn("nudge") ? [["nudge", "催办规则"]] : []), ["pw", "我的密码"]] : [["ai", "交融大模型"], ["pw", "我的密码"]]; } },
  watch: { sec: { handler(v) { SETTINGS_UI.sec = v; }, immediate: true } },
  mounted() { this.load(); },
  unmounted() { SETTINGS_UI.sec = ""; },
  methods: {
    go,
    async load() { if (this.isAdmin) { this.th = (await GET("/api/project/thresholds")).values; this.feats = await GET("/api/project/features"); this.claimRule = { ...(await GET("/api/claims/notice-rule").catch(() => null) || {}), basis: "" }; if (!this.claimRule.days) this.claimRule = null; }
      const cfg = store.me.project.config || {};
      this.pname = store.me.project.name; this.wpw = cfg.workdays_per_week || 7; this.cadence = cfg.cadence || "week"; this.dailyLog = cfg.daily_log !== false; this.socialEnabled = !!cfg.social_enabled; },
    async saveTh() { try { await PUT("/api/project", { name: this.pname, thresholds: this.th, workdays_per_week: this.wpw, cadence: this.cadence, daily_log: this.dailyLog, social_enabled: this.socialEnabled,
        features: this.feats.length ? Object.fromEntries(this.feats.map((f) => [f.key, f.enabled])) : undefined }); await loadMe(); if (this.isAdmin) this.feats = await GET("/api/project/features"); toast("已保存"); } catch (e) { toast(e.message, true); } },
    async saveClaimRule() { try { const r = await PUT("/api/claims/notice-rule", { days: this.claimRule.days, basis: this.claimRule.basis }); this.claimRule.days = r.days; toast("已保存"); } catch (e) { toast(e.message, true); } },
    async test() {
      this.testing = true; this.testResult = "";
      try { const r = await POST("/api/ai/test"); this.testResult = `连通 ${r.provider === 'mock' ? '模拟' : r.model} ${r.latency_ms}ms`; }
      catch (e) { this.testResult = "失败：" + e.message; } finally { this.testing = false; }
    },
    async changePw() { try { await PUT("/api/me/password", this.pw); this.pw = { old: "", new: "" }; toast("密码已修改"); } catch (e) { toast(e.message, true); } },
  },
};

// 消息卡按钮：有跳转目标按类型给“去回复/去确认/去处理”；预警消息维持原逻辑（管理岗“查看预警”），技术员改为“去说明原因”直达首页随手记。
// 所链事项已处理（state=done）的只写“查看”，不再催人“去处理”
function noticeAction(n, role) {
  const k = n && n.link;
  if (k && k.route) {
    const a = { label: n.state ? "查看" : { circle: "去回复", week: "去确认" }[k.route] || "去处理", route: k.route, params: k.params || {} };
    if (k.focus) a.focus = k.focus;
    return a;
  }
  if (n && n.alert_id) return role === "tech" ? { label: "去说明原因", route: "home", params: {}, focus: "note" } : { label: "查看预警", route: "alert", params: { id: n.alert_id } };
  return null;
}

// 消息页（S-5、F-07）：按时间倒序、可翻更早的；整张卡可点，点开即已读；所链事项已处理的标“已处理”、预警已解除的标“已解除”（灰标，后端已计为已读）
const NOTICE_PAGE = 100;
const NOTICE_PS = 10;   // 电脑每页条数
const NOTICE_MPS = 5;   // 手机每页条数
const NOTICE_WD = "日一二三四五六";
const noticeDayShift = (d, k) => { const t = new Date(d + "T00:00:00Z"); if (isNaN(t)) return ""; t.setUTCDate(t.getUTCDate() + k); return t.toISOString().slice(0, 10); };
const noticeDayLabel = (d) => { const t = new Date(d + "T00:00:00Z"); return isNaN(t) ? (d || "日期不明") : d.slice(5) + " 周" + NOTICE_WD[t.getUTCDay()]; };
const Notices = {
  // 10-10 一屏化：消息是按日分组的记录流，放进一个面板用 zb-paged 内部分页（电脑每页 NOTICE_PS 条、手机 NOTICE_MPS 条），
  // 每页开头和换日处出日期标题（标题跟随当前页内容）；“更早的消息”向服务器取数后并入同一分页器，页面高度不变。
  // 正文最多 2 行，超出点“全文”在抽屉里看；整张卡点开即已读并跳到消息链接。
  template: `<div class="ra-nts"><page-head page="notices"><template #meta>{{ loaded ? (unreadN ? '未读 ' + unreadN + ' 条 · ' : '') + '按日期分组，新消息在前' : '' }}</template><template #actions><button class="btn sm" @click="readAll">全部已读</button></template></page-head>
    <load-state :loading="!loaded && !err" :error="err" @retry="load">
    <section class="zb-card ra-nt-box" v-if="items.length"><zb-paged :items="items" :size="NOTICE_PS" :m-size="NOTICE_MPS" :fit="240" cls="ra-nt-list"><template #default="{ item: n, i }">
    <h3 class="ra-nt-dh" v-if="dayHead(i)"><span>{{ dayLabel(n) }}</span><small>{{ dayN[dayOf(n)] }} 条</small></h3>
    <div :class="['ra-nt', 'act', {read: !!n.read_at}]" @click="open(n)">
      <div class="ra-nt-h"><span class="chip" v-if="n.state_text">{{ n.state_text }}</span><span class="chip blue" v-else-if="!n.read_at">未读</span>
        <b class="ra-nt-t">{{ n.title }}</b><span class="tiny muted nowrap">{{ fmtDT(n.created_at) }}</span></div>
      <zb-clamp class="small ra-nt-b" :text="n.body" :lines="2" pre :title="n.title"></zb-clamp>
      <div class="ra-nt-a" v-if="noticeAction(n, role)"><a class="small zb-link" v-if="noticeAction(n, role).route === 'alert'" @click.stop="open(n)">{{ noticeAction(n, role).label }} <ic n="chevron"></ic></a>
        <button :class="['btn', 'sm', n.state ? '' : 'primary']" v-else @click.stop="open(n)">{{ noticeAction(n, role).label }}</button></div></div></template></zb-paged>
    <div class="ra-nt-more" v-if="more"><span class="tiny muted">已取最近 {{ items.length }} 条</span><button class="zb-link ra-more" :disabled="busy" @click="loadMore">{{ busy ? '加载中' : '更早的消息' }}<ic n="chevron"></ic></button></div></section>
    <empty-state v-if="loaded && !items.length" reason="没有消息。预警、派单和催办的通知会出现在这里。" :action="{label: '回到首页', fn: toHome}"></empty-state></load-state>
    <zb-drawer v-if="full" :title="full.title" @close="full = null"><div class="small muted mb">{{ fmtDT(full.created_at) }}</div><div class="pre">{{ full.body || '（这条消息没有正文）' }}</div></zb-drawer></div>`,
  data: () => ({ items: [], loaded: false, err: null, more: false, busy: false, narrow: window.innerWidth < 861, NOTICE_PS, NOTICE_MPS, full: null }),
  mounted() { this.load(); this.onR = () => { this.narrow = window.innerWidth < 861; }; window.addEventListener("resize", this.onR); },
  unmounted() { window.removeEventListener("resize", this.onR); },
  computed: {
    role: () => roleOf(),
    // 未读数与铃铛同源：全部未读（服务器数），不是已取到的这一页里的未读（验收 I7）
    unreadN() { return store.me ? store.me.unread || 0 : this.items.filter((n) => !n.read_at).length; },
    // 每天的条数（按已取到的消息算）
    dayN() { const m = {}; for (const n of this.items) { const d = this.dayOf(n); m[d] = (m[d] || 0) + 1; } return m; },
  },
  methods: {
    go, fmtDT, noticeAction, runAction, toHome() { go(homeOf()); },
    dayOf(n) { return String((n && n.created_at) || "").slice(0, 10); },
    // 日期标题：服务器日期为准（今天 / 昨天 / 月-日 周几）
    dayLabel(n) { const d = this.dayOf(n), td = today(); return d === td ? "今天" : d === noticeDayShift(td, -1) ? "昨天" : noticeDayLabel(d); },
    // 每页第一条和换日处出标题
    dayHead(i) { const k = this.narrow ? NOTICE_MPS : NOTICE_PS; return i % k === 0 || this.dayOf(this.items[i - 1]) !== this.dayOf(this.items[i]); },
    async load() {
      this.err = null;
      try {
        const r = await GET(`/api/notifications?limit=${NOTICE_PAGE}`); this.items = r; this.more = r.length >= NOTICE_PAGE;
        const u = await GET("/api/notifications/unread"); if (store.me) store.me.unread = u.unread;   // 读取时已结清的已计入服务器数
      }
      catch (e) { this.err = e; } finally { this.loaded = true; }
    },
    async loadMore() {
      this.busy = true;
      try {
        const r = await GET(`/api/notifications?limit=${NOTICE_PAGE}&offset=${this.items.length}`);
        const have = new Set(this.items.map((x) => x.id));
        this.items.push(...r.filter((x) => !have.has(x.id)));   // 原地追加：分页器停在当前页，新取的排在后面几页
        this.more = r.length >= NOTICE_PAGE; this.settle(r);
      } catch (e) { toast(errText(e), true); } finally { this.busy = false; }
    },
    // 读取时被计为已读（所链事项已处理/预警已解除）的条数，从红点里减掉
    settle(rows) {
      const n = (rows || []).filter((x) => x && x.settled).length;
      if (n && store.me) store.me.unread = Math.max(0, (store.me.unread || 0) - n);
    },
    // 点开：未读的先标已读（不等服务器），有跳转目标的再去处理
    open(n) {
      const a = noticeAction(n, this.role);
      if (!n.read_at) {
        n.read_at = new Date().toISOString();
        POST(`/api/notifications/${n.id}/read`).then((r) => { if (store.me && r && typeof r.unread === "number") store.me.unread = r.unread; }).catch(() => {});
      }
      if (a) runAction(a);
      else this.full = n;   // 没有跳转目标的（公告、系统提醒）：就地看全文，不只标已读（验收 I8）
    },
    async readAll() {
      try { await POST("/api/notifications/read"); store.me.unread = 0; await this.load(); } catch (e) { toast(errText(e), true); }
    },
  },
};

