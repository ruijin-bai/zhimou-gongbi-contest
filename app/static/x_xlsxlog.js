/* x_xlsxlog：Excel 日报导入（技术员，RJ 2026-10-09 意见 7）。入口在随手记首页“日志与周确认”卡片里一行。
   - 上传项目现用的“生产日报”.xlsx → 规则识别（不调用模型、不补数）→ 预览核对；识别出的每一行都是“候选·待确认”；
   - 进度表逐行决定：计入工程量（选 P6 作业、核对数量）/ 只记施工内容 / 不导入；全部决定后才能生成当天的日志草稿；
   - 草稿照常在日志页核对、提交，导入本身不提交、不改进度计划。接口见 app/routers/xlsxlog.py。 */
const XL_ACTS = [["p6", "计入工程量"], ["text", "只记施工内容"], ["skip", "不导入"]];
const xlFmt = (v, u) => (v === null || v === undefined ? "未填" : `${+v}${u || ""}`);

const XlsxImportPage = {
  template: `<div class="ra xl"><page-head title="导入 Excel 日报" purpose="把平时填的生产日报 Excel 转成系统日志草稿，逐行确认后才生成">
      <template #meta>规则识别，不调用模型、不补数；每行都是候选，逐行决定后才生成当天日志草稿</template></page-head>
    <section class="zb-card mb"><div class="xl-pick">
      <label class="btn primary"><ic n="file"></ic> {{ d ? '换一份' : '选择 Excel 文件' }}<input type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden @change="pick"></label>
      <span class="tiny muted grow">支持项目“生产日报/Daily Report”模板的 .xlsx；旧版 .xls 请先在 Excel 里另存为 .xlsx</span></div>
      <div class="small muted mt" v-if="busy">识别中…</div>
      <div class="zb-empty" v-if="!d && !busy">还没有选文件。选好后在这里逐行核对，生成的日志草稿仍在日志页核对、提交。</div></section>
    <template v-if="d">
      <div class="banner warn" v-for="(w, i) in p.warnings" :key="'w' + i">{{ w }}</div>
      <div class="banner info" v-if="d.imported_before">这份文件 {{ d.imported_before.at.slice(0, 16).replace('T', ' ') }} 已导入过一次，再导入会把内容再并入一遍。
        <a @click="go('log', {id: d.imported_before.log_id})">查看那次的日志</a></div>
      <section class="zb-card mb"><div class="zb-ch"><ic n="calendar"></ic><h2>{{ p.section || '日报' }}</h2><span class="grow"></span><span class="zb-src">{{ d.filename }}</span></div>
        <div class="xl-kv">
          <label>施工日期<input class="in" type="date" v-model="day" :max="today()"></label>
          <span>天气<b>{{ p.weather || '未填' }}</b></span>
          <span>填报人<b>{{ p.reporter || '未填' }}</b></span>
          <span>复核人<b>{{ p.reviewer || '未填' }}</b></span></div>
        <div class="banner warn mt" v-if="logLocked">这天的日志已{{ d.day_log.status === 'approved' ? '审核' : '提交' }}，不能再导入；如需更正请联系审核人退回。</div>
        <div class="tiny muted mt" v-else-if="d.day_log && day === p.date">这天已有日志草稿：导入的工程量按作业替换，其余内容追加在后面。</div></section>

      <section class="zb-card mb"><label class="xl-sec"><input type="checkbox" v-model="keep.staff"><b>人员</b><span class="chip orange">候选</span></label>
        <div class="small">中方 {{ xlFmt(p.staff_cn, ' 人') }} · 属地 {{ xlFmt(p.staff_local, ' 人') }}</div>
        <label class="xl-sec mt"><input type="checkbox" v-model="keep.machinery"><b>进场设备</b><span class="chip orange">候选</span></label>
        <div class="small">{{ p.equipment || '未填' }}</div>
        <div class="tiny muted" v-if="p.equipment && !p.machines.length">没有分项，按合计 {{ xlFmt(p.equipment_count, ' 台') }} 记</div></section>

      <section class="zb-card mb" v-if="p.overview.length"><label class="xl-sec"><input type="checkbox" v-model="keep.overview"><b>当日施工内容（{{ p.overview.length }} 条）</b><span class="chip orange">候选</span></label>
        <ol class="xl-lines"><li v-for="(x, i) in p.overview" :key="i"><span class="tiny muted" v-if="x.group">{{ x.group }} · </span>{{ x.text }}</li></ol></section>

      <section class="zb-card mb"><div class="zb-ch"><ic n="layers"></ic><h2>现场进度表</h2><span class="grow"></span>
          <span :class="['rt-count', {ok: left === 0}]">已决定 {{ p.rows.length - left }}/{{ p.rows.length }} 行</span></div>
        <div class="tiny muted">每行都要决定。系统给的 P6 作业只是按桩号和名称猜的候选，请核对；没有当日数量的行不能计入工程量。</div>
        <div class="row mt" v-if="p.rows.length"><button class="btn sm" @click="suggestAll">按建议先填一遍（仍可逐行改）</button></div>
        <div class="xl-row" v-for="r in p.rows" :key="r.i" :class="{done: !!dec[r.i].action}">
          <div class="row wrap" style="gap:6px"><b>{{ r.area ? r.area + ' · ' : '' }}{{ r.content }}</b>
            <span class="chip orange" v-if="!dec[r.i].action">候选·待确认</span><span class="tiny muted grow">Excel 第 {{ r.excel_row }} 行</span></div>
          <div class="small muted" v-if="r.location">{{ r.location }}</div>
          <div class="small">当日 <b>{{ xlFmt(r.daily, r.unit) }}</b> · 累计 {{ xlFmt(r.cum, r.unit) }}<template v-if="r.pct_text"> · 占比 {{ r.pct_text }}</template></div>
          <div class="rt-seg mt" role="group"><button type="button" v-for="o in XL_ACTS" :key="o[0]" :class="{on: dec[r.i].action === o[0]}" :disabled="o[0] === 'p6' && r.daily === null" @click="dec[r.i].action = o[0]">{{ o[1] }}</button></div>
          <div class="row wrap mt" style="gap:6px" v-if="dec[r.i].action === 'p6'">
            <select class="in grow" v-model="dec[r.i].activity_code" style="min-width:0">
              <option value="" disabled>选择 P6 作业</option>
              <optgroup label="系统候选（请核对）" v-if="r.candidates.length"><option v-for="c in r.candidates" :key="'c' + c.code" :value="c.code">{{ c.code }} {{ c.name }}</option></optgroup>
              <optgroup label="全部作业"><option v-for="a in d.activities" :key="a.code" :value="a.code">{{ a.code }} {{ a.name }}</option></optgroup></select>
            <input class="in" type="number" min="0" step="any" inputmode="decimal" style="width:100px" v-model="dec[r.i].qty"><span class="small">{{ r.unit }}</span></div>
          <div class="tiny rt-warn" v-if="dec[r.i].action === 'p6' && !dec[r.i].activity_code">请选择 P6 作业</div></div>
        <empty-state compact v-if="!p.rows.length" reason="没有识别出进度表的行，只能导入文字部分。"></empty-state></section>

      <section class="zb-card mb" v-if="p.tomorrow.length"><label class="xl-sec"><input type="checkbox" v-model="keep.tomorrow"><b>明日计划（{{ p.tomorrow.length }} 条）</b><span class="chip orange">候选</span></label>
        <ol class="xl-lines"><li v-for="(x, i) in p.tomorrow" :key="i"><span class="tiny muted" v-if="x.group">{{ x.group }} · </span>{{ x.text }}</li></ol></section>

      <section class="zb-card xl-go"><button class="btn primary big block" :disabled="!ready || saving" @click="confirmIt">{{ saving ? '生成中' : left ? '还有 ' + left + ' 行没决定' : '生成 ' + day + ' 的日志草稿' }}</button>
        <div class="tiny muted mt">生成后在日志页核对、提交；导入本身不提交，也不改进度计划。</div></section>
    </template></div>`,
  data: () => ({ d: null, busy: false, saving: false, day: "", dec: {}, keep: { overview: true, tomorrow: true, staff: true, machinery: true }, XL_ACTS }),
  computed: {
    p() { return this.d.parsed; },
    left() { return this.p.rows.filter((r) => !this.ok(r)).length; },
    logLocked() { return !!(this.d.day_log && this.day === this.p.date && ["submitted", "approved"].includes(this.d.day_log.status)); },
    ready() { return !!this.day && this.left === 0 && !this.logLocked && !this.d.confirmed_at; },
  },
  async mounted() {
    const id = store.route && store.route.params && store.route.params.id;
    if (id) { try { this.apply(await GET("/api/logs/xlsx/" + id)); } catch (e) { toast(e.message, true); } }
  },
  methods: {
    go, today, xlFmt,
    apply(d) {
      this.d = d; this.day = d.parsed.date || "";
      this.dec = Object.fromEntries(d.parsed.rows.map((r) => [r.i, { action: "", activity_code: "", qty: r.daily === null ? "" : r.daily }]));
    },
    ok(r) { const x = this.dec[r.i]; return !!x && (x.action === "text" || x.action === "skip" || (x.action === "p6" && !!x.activity_code && String(x.qty).trim() !== "" && +x.qty >= 0)); },
    async pick(ev) {
      const f = ev.target.files[0]; ev.target.value = ""; if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      this.busy = true;
      try { this.apply(await POST("/api/logs/xlsx", fd)); } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
    // 只填还没决定的行：有当日数量且有候选 → 计入工程量（第一个候选）；有数量没候选 → 只记施工内容；没有数量或为 0 → 不导入
    suggestAll() {
      for (const r of this.p.rows) {
        const x = this.dec[r.i]; if (x.action) continue;
        if (r.daily && r.candidates.length) Object.assign(x, { action: "p6", activity_code: r.candidates[0].code });
        else x.action = r.daily ? "text" : "skip";
      }
    },
    async confirmIt() {
      this.saving = true;
      try {
        const decisions = this.p.rows.map((r) => ({ i: r.i, ...this.dec[r.i], qty: this.dec[r.i].qty === "" ? null : +this.dec[r.i].qty }));
        const res = await POST(`/api/logs/xlsx/${this.d.import_id}/confirm`, { log_date: this.day, decisions, keep: this.keep });
        toast(`已生成日志草稿：工程量 ${res.progress} 条，施工内容 ${res.works} 条，请核对后提交`);
        go("log", { id: res.log_id });
      } catch (e) { toast(e.message, true); } finally { this.saving = false; }
    },
  },
};
EXT.route("xlsx", XlsxImportPage);
