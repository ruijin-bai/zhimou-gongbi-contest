/* x_tickets：材料Agent。拍小票 → Agent 识别成草稿 → 人核对确认 → 自动累计浇筑方量、提醒试块、长出材料进场批次与复试台账。
   四个分页：待确认 / 浇筑与试块 / 材料批次 / 预警。草稿在人点“确认入账”之前不计入任何累计；理论方量由人填写，缺则显示“理论方量待补”；
   偏差、少留试块、已用未复试都只是“疑点，请核实”，系统不下合格结论。离线模拟模型识别的草稿会显著标注。 */
(function () {
  const WRITE = ["tech", "eng_sup", "manager"];
  const canWrite = () => !!store.me && WRITE.includes(roleOf());
  const canQuality = () => !!store.me && can("site_quality") && roleOf() !== "admin";
  const canRules = () => !!store.me && ["manager", "eng_sup"].includes(roleOf());
  const isMock = () => !!(store.me && store.me.ai && store.me.ai.provider === "mock");

  // 单据名称只有这一张表，与后端 ai/ticket_agent.py 的 DOC_KINDS 一字不差（U-33，tests/test_capture_front.py 核对）；选择处、草稿、列表同一叫法
  const KIND = { concrete_ticket: "混凝土小票", material_receipt: "材料进场单", cert: "合格证/检测报告" };
  const KIND_PICK = [["auto", "自动判断"], ...Object.entries(KIND)];
  const FIELDS = {
    concrete_ticket: [["supplier", "供应商/搅拌站"], ["ticket_no", "票号"], ["vehicle", "车牌/车号"], ["grade", "强度等级", "text", "如 C30、C35P8"], ["qty", "本车方量 m³", "num"],
      ["slump", "坍落度 mm", "num"], ["temp", "入模温度 ℃", "num"], ["part", "浇筑部位", "text", "按票面填写"], ["receiver", "签收人"], ["remark", "备注"]],
    material_receipt: [["supplier", "供应商/厂家"], ["ticket_no", "单号"], ["vehicle", "车牌/车号"], ["material", "材料名称"], ["spec", "规格/牌号", "text", "如 HRB400E Φ25"],
      ["qty", "数量", "num"], ["unit", "单位", "text", "吨、袋"], ["batch_no", "批号/炉批号"], ["receiver", "签收人"], ["remark", "备注"]],
    cert: [["supplier", "厂家/出具单位"], ["ticket_no", "编号"], ["material", "材料名称"], ["spec", "规格/牌号"], ["batch_no", "批号/炉批号"], ["remark", "备注"]],
  };
  const REQUIRED = { concrete_ticket: ["qty", "grade"], material_receipt: ["material", "qty", "unit"], cert: [] };
  const CAT = { rebar: "钢筋", cement: "水泥", sand: "砂", stone: "石子", admixture: "外加剂", other: "其他" };
  const RETEST_CHIP = { not_required: "", pending: "orange", sampled: "blue", pass: "green", fail: "red" };
  const RETEST = { not_required: "无需复试", pending: "待取样", sampled: "已送检", pass: "复试合格", fail: "复试不合格" };
  const LV_CHIP = { high: "red", medium: "orange", low: "" };
  const POUR_ST = { planned: "计划", pouring: "浇筑中", done: "已完成" };
  const q2 = (v) => fmtN(v, 2);
  const errMsg = (e) => (e && e.message) || "操作失败";
  const dayOf = (s) => (s ? String(s).slice(0, 10) : "");
  const timeOf = (s) => (s && String(s).length >= 16 ? String(s).slice(11, 16) : "");

  // 识别超时（U-04、AC-1.11）：pending/running 超过 10 分钟（按服务器校正时钟，不信手机时间）就不再转圈，改为“识别超时 · 重新识别／手工填写”。
  // 等待起点取上传时间与本机最近一次点“重新识别”的时间中较晚者（接口不回传开始识别的时间）
  const XT_STUCK_MS = 10 * 60000, XT_RETRY_KEY = "zb_xt_retry";
  const nowMs = () => +(typeof wmNow === "function" ? wmNow() : new Date());
  const xtRecognizing = (t) => t.ai_status === "pending" || t.ai_status === "running";
  function xtWaitMs(t, now) {
    const r = (lsGet(XT_RETRY_KEY, {}) || {})[t.id], base = Math.max(Date.parse(t.created_at) || 0, +r || 0);
    return base ? Math.max(0, (now || nowMs()) - base) : 0;
  }
  // 上传时间比服务器当前时间还晚 5 分钟以上（仿真副本的演示时钟、服务器时钟回拨等）：等待时长算出来是 0，按时长永远判不出超时，
  // 页面会一直转圈。这类记录如实显示“识别未完成”，不再轮询；本机刚点过“重新识别”的 10 分钟内仍按正常识别显示
  const XT_AHEAD_MS = 5 * 60000;
  function xtAhead(t, now) {
    const c = Date.parse(t.created_at) || 0, n = now || nowMs(), r = +((lsGet(XT_RETRY_KEY, {}) || {})[t.id]) || 0;
    if (r && n - r >= 0 && n - r < XT_STUCK_MS) return false;
    return !!c && c - n > XT_AHEAD_MS;
  }
  const xtStuck = (t, now) => xtRecognizing(t) && (xtWaitMs(t, now) > XT_STUCK_MS || xtAhead(t, now));
  function xtWaitText(ms) { const m = Math.floor(ms / 60000); return m < 60 ? `${m} 分钟` : m < 2880 ? `${Math.floor(m / 60)} 小时` : `${Math.floor(m / 1440)} 天`; }
  function xtMarkRetry(id) {  // 只留一天内的记录
    const now = nowMs(), m = lsGet(XT_RETRY_KEY, {}) || {};
    for (const k of Object.keys(m)) if (!(now - m[k] < 86400000)) delete m[k];
    m[id] = now; lsSet(XT_RETRY_KEY, m);
  }

  // ---------------------------------------------------------------------- 一张票据草稿：识别中 / 识别超时 / 字段核对 / 确认入账
  const XtDraft = {
    components: { ReviewTable },
    props: ["t", "now"],
    emits: ["changed"],
    template: `<section class="zb-card xt-card">
      <div class="xt-head">
        <div class="xt-thumbs"><ev-photo v-for="(m, i) in t.media" :key="m.id" :src="m.thumb" :full="m.url" size="s" :label="t.media.length > 1 ? '原图 ' + (i + 1) : ''"></ev-photo>
          <ev-photo v-if="!t.media.length" size="s"></ev-photo></div>
        <div class="grow">
          <div class="row wrap"><select class="in xt-kind" v-model="kind" :disabled="busy"><option v-for="(n, k) in KIND" :key="k" :value="k">{{ n }}</option></select>
            <i class="zb-sys" v-if="t.mock">离线模拟识别</i>
            <span class="chip blue" v-else-if="t.ai_status === 'done'">AI 已识别</span>
            <span class="chip orange" v-else-if="stuck">识别未完成</span>
            <span class="chip red" v-else-if="t.ai_status === 'failed'">识别失败</span>
            <span class="tiny muted nowrap">{{ t.uploader }} · 上传 {{ fmtDT(t.created_at) }}</span></div>
          <div class="tiny muted mt" v-if="t.ai_status === 'done'">识别结果以原图为准，存疑项已标红，核对后确认。</div>
        </div>
      </div>
      <div class="thinking" v-if="recognizing && !stuck"><div class="spinner"></div>识别中…<span v-if="waitMin >= 1">（已等待 {{ waitMin }} 分钟）</span></div>
      <div class="banner warn mt" v-if="stuck"><template v-if="ahead">识别未完成：记录停在“{{ t.ai_status === 'running' ? '识别中' : '排队中' }}”，上传时间晚于服务器当前时间，无法判断是否仍在识别</template><template v-else>识别超时（已等待 {{ waitText }}）</template>
        · <a @click="retry">重新识别</a>／<a v-if="!manual" @click="manual = true">手工填写</a><span v-else>在下方手工填写</span></div>
      <div class="banner warn mt" v-if="t.ai_status === 'failed'">识别失败：{{ t.ai_error || '请稍后重试' }}。可手工填写或
        <a @click="retry">重新识别</a>。</div>
      <template v-if="t.ai_status === 'done' || t.ai_status === 'failed' || manual">
        <div class="tiny muted mt" v-if="t.mock"><i class="zb-sys">离线模拟模型</i>字段为演示数据，须对照原图核对</div>
        <div class="banner warn mt" v-if="t.dups.length"><b>疑似重复：</b><div v-for="d in t.dups" :key="d.text">{{ d.text }}</div>
          <label class="row mt"><input type="checkbox" v-model="force"> 已核对，非重复票据，仍入账</label></div>
        <div class="mt"><review-table title="识别字段核对：确认后才入账" :rows="rowsT" v-model="decisions" :bulk="true" :cols="TK_COLS"
          hint="每个字段一行：AI 读到的值、对照的原图，由你决定确认、修改、不采纳或无法判断；必填项只能确认或修改，看不清的不要猜。"></review-table></div>
        <div class="xt-grid mt" v-if="kind !== 'concrete_ticket'">
          <label class="xt-f"><span class="xt-fl">材料类别</span>
            <select class="in" v-model="category" :disabled="busy"><option v-for="(n, k) in CAT" :key="k" :value="k">{{ n }}</option></select></label>
        </div>
        <div class="xt-pour mt" v-if="kind === 'concrete_ticket'">
          <div class="sec-h"><h3>归入浇筑单元</h3><span class="tiny muted">系统仅推荐；未选则方量暂不计入</span></div>
          <div class="xt-opts">
            <label :class="['xt-opt', {on: pick === null}]"><input type="radio" :value="null" v-model="pick"> 暂不归入</label>
            <label v-for="c in cands" :key="c.id" :class="['xt-opt', {on: pick === c.id}]"><input type="radio" :value="c.id" v-model="pick">
              <b>{{ c.label }}</b> <span class="chip">{{ c.status_name }}</span><span class="tiny muted"> {{ c.reasons.join('、') }} · {{ c.design_qty_text }}</span></label>
            <label :class="['xt-opt', {on: pick === 'new'}]"><input type="radio" value="new" v-model="pick"> <ic n="plus"></ic>新建浇筑单元</label>
          </div>
          <div class="xt-grid mt" v-if="pick === 'new'">
            <label class="xt-f"><span class="xt-fl">部位<b class="xt-req">*</b></span><input class="in" v-model.trim="np.part" placeholder="如 3#墩承台"></label>
            <label class="xt-f"><span class="xt-fl">强度等级</span><input class="in" v-model.trim="np.grade" placeholder="C30"></label>
            <label class="xt-f"><span class="xt-fl">浇筑日期</span><input class="in" type="date" v-model="np.pour_date"></label>
            <label class="xt-f"><span class="xt-fl">理论方量 m³（选填）</span><input class="in" type="number" step="any" v-model="np.design_qty" placeholder="留空显示“理论方量待补”"></label>
          </div>
        </div>
        <div class="banner warn mt" v-if="err">{{ err }}</div>
        <div class="row mt wrap"><button class="btn primary" :disabled="busy || left > 0" @click="confirm">{{ busy ? '入账中…' : '确认入账' }}</button>
          <span class="tiny rt-left" v-if="left > 0">还有 {{ left }} 项字段未决定</span>
          <button class="btn danger" :disabled="busy" @click="voidIt">作废</button>
          <span class="tiny muted" v-if="!t.can_confirm">仅上传人或工程主管可确认</span></div>
      </template>
      <div class="row mt" v-else-if="t.can_void"><button class="btn danger sm" @click="voidIt">作废</button></div>
    </section>`,
    data() { return { KIND, CAT, TK_COLS: { item: "字段", ai: "AI 识别", ev: "依据（原图 / 存疑）", dec: "人工决定" }, decisions: {}, kind: this.t.doc_kind, category: this.t.category || "other", force: false, busy: false, err: "", cands: [], pick: null, manual: false,
      np: { part: this.t.fields.part || "", grade: this.t.fields.grade || "", pour_date: dayOf(this.t.fields.issued_at) || today(), design_qty: "" } }; },
    computed: {
      recognizing() { return xtRecognizing(this.t); },
      stuck() { return xtStuck(this.t, this.now); },
      ahead() { return xtAhead(this.t, this.now); },
      waitMin() { return Math.floor(xtWaitMs(this.t, this.now) / 60000); },
      waitText() { return xtWaitText(xtWaitMs(this.t, this.now)); },
      fieldDefs() { return FIELDS[this.kind]; },
      req() { return REQUIRED[this.kind]; },
      // 识别字段一行一个：AI 原值 | 依据（原图、存疑原因）| 人工决定。必填项只能确认/修改；AI 没识别出的选填项不强制决定
      rowsT() {
        const ai = this.t.ai_fields || {}, doubts = this.t.doubts || {};
        const defs = this.fieldDefs.slice();
        defs.splice(Math.min(2, defs.length), 0, ["issued_at", this.kind === "concrete_ticket" ? "发货时间" : "到场/出具日期", "text", "如 2026-10-06 08:30"]);
        return defs.map((f) => {
          const k = f[0], raw = ai[k] === undefined || ai[k] === "" ? null : ai[k];
          const required = this.req.includes(k) || (k === "issued_at" && this.kind !== "cert"), num = f[2] === "num";
          const ev = (this.t.media || []).map((m, i) => ({ type: "photo", url: m.url, thumb: m.thumb, text: `原图 ${i + 1}（点开放大）` }));
          if (doubts[k]) ev.unshift({ type: "text", text: "存疑：" + doubts[k] });
          else if ((this.t.unclear || []).includes(k)) ev.unshift({ type: "text", text: "AI 标注：票面上看不清" });
          return { key: k, label: f[1] + (required ? " *" : ""), numeric: num, input: "text", placeholder: f[3] || "", ai_raw: raw, ai_text: raw === null ? "" : String(raw),
            flag: doubts[k] ? "存疑：" + doubts[k] : "", evidence: ev, optional: raw === null && !required, modifyLabel: raw === null ? "填写" : "",
            opts: required ? (raw === null ? ["modify"] : ["confirm", "modify"]) : (raw === null ? ["modify", "unknown"] : undefined) };
        });
      },
      left() { return reviewStatus(this.rowsT, this.decisions, "adopt").remaining; },
    },
    watch: {
      "t.ai_status"() { this.decisions = {}; this.kind = this.t.doc_kind; this.category = this.t.category || "other"; this.loadCands(); },
      kind() { this.loadCands(); },
    },
    mounted() { if (this.t.ai_status === "done") this.loadCands(); },
    methods: {
      fmtDT,
      // 候选浇筑单元用的“当前值”：已决定的取人工值，没决定先用 AI 原值
      tent(k) {
        const d = this.decisions[k] || {}, r = this.rowsT.find((x) => x.key === k) || {};
        const v = d.decision === "confirm" ? r.ai_raw : d.decision === "modify" ? d.final : d.decision ? null : r.ai_raw;
        return v === null || v === undefined ? "" : String(v);
      },
      async loadCands() {
        if (this.kind !== "concrete_ticket" || this.t.ai_status !== "done") return;
        try {
          const part = this.tent("part"), grade = this.tent("grade"), day = this.tent("issued_at").slice(0, 10);
          const q = [`ticket_id=${this.t.id}`, part ? "part=" + encodeURIComponent(part) : "", grade ? "grade=" + encodeURIComponent(grade) : "", day ? "day=" + day : ""].filter(Boolean).join("&");
          const r = await GET("/api/pours/candidates?" + q);
          this.cands = r.items;
          if (r.suggest_new) { this.np.part = this.np.part || r.suggest_new.part || ""; this.np.grade = this.np.grade || r.suggest_new.grade || ""; }
        } catch (_) { this.cands = []; }
      },
      payload() {
        const fields = {};
        for (const r of this.rowsT) {  // 每个字段都显式给出：确认=AI 原值，修改=人工值，不采纳/无法判断/未决定=空（不补零）
          const d = this.decisions[r.key] || {};
          const v = d.decision === "confirm" ? r.ai_raw : d.decision === "modify" ? (r.numeric ? +d.final : String(d.final).trim()) : null;
          fields[r.key] = v === undefined || v === "" ? null : v;
        }
        const body = { doc_kind: this.kind, fields, force_dup: this.force, decisions: reviewPayload(this.rowsT, this.decisions) };
        if (this.kind !== "concrete_ticket") body.category = this.category;
        else if (this.pick === "new") body.new_pour = { part: this.np.part, grade: this.np.grade || fields.grade, pour_date: this.np.pour_date || (fields.issued_at || "").slice(0, 10), design_qty: this.np.design_qty === "" ? null : +this.np.design_qty };
        else if (this.pick) body.pour_id = this.pick;
        return body;
      },
      async confirm() {
        this.err = "";
        if (this.pick === "new" && !this.np.part) { this.err = "请填写部位"; return; }
        this.busy = true;
        try {
          const r = await PUT("/api/tickets/" + this.t.id, this.payload());
          toast("已确认入账" + (r.warnings && r.warnings.length ? "；" + r.warnings.join("；") : ""), !!(r.warnings && r.warnings.length));
          this.$emit("changed");
        } catch (e) { this.err = errMsg(e); } finally { this.busy = false; }
      },
      async voidIt() {
        if (!window.confirm("确认作废该票据？作废后不计入累计。")) return;
        this.busy = true;
        try { await POST(`/api/tickets/${this.t.id}/void`, { reason: "拍错/重复" }); toast("已作废"); this.$emit("changed"); } catch (e) { toast(errMsg(e), true); } finally { this.busy = false; }
      },
      async retry() {
        if (this.busy) return;
        this.busy = true;
        try { await POST(`/api/tickets/${this.t.id}/reocr`); xtMarkRetry(this.t.id); this.manual = false; this.$emit("changed"); }
        catch (e) {
          // 超时的票据服务器仍记为“排队中”时不让重试：就地展开手工填写，不让人卡在这里
          if (e.status === 409 && xtStuck(this.t, this.now)) { this.manual = true; toast("暂时不能重新识别（服务器仍记为识别中），请在下方手工填写，或作废后重拍", true); }
          else toast(errMsg(e), true);
        } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 浇筑单元卡（累计 / 偏差 / 试块）
  const XtPour = {
    props: ["p", "open"],
    emits: ["changed"],
    template: `<section :class="['zb-card xt-card', {hl: open}]" :id="'xt-pour-' + p.id">
      <div class="row between wrap"><div class="row wrap"><h3>{{ p.part }}</h3><span class="chip blue" v-if="p.grade">{{ p.grade }}</span><span class="chip">{{ p.status_name }}</span>
          <span class="tiny muted" v-if="p.pour_date">{{ p.pour_date }}</span></div>
        <select class="in xt-st" v-if="canWrite" :value="p.status" @change="setStatus($event.target.value)"><option v-for="(n, k) in POUR_ST" :key="k" :value="k">{{ n }}</option></select></div>
      <div class="banner info mt" v-if="p.partial">仅显示本人/本单位上传的小票，完整累计与偏差由港湾岗位查看。</div>
      <div class="xt-kpis mt">
        <div class="xt-kpi"><div class="v">{{ q2(p.actual_qty) }}<small> m³</small></div><div class="l">已确认累计 · {{ p.trucks }} 车</div></div>
        <div class="xt-kpi"><div class="v" :class="{pending: p.design_qty === null}">{{ p.design_qty === null ? '待补' : q2(p.design_qty) }}<small v-if="p.design_qty !== null"> m³</small></div>
          <div class="l">{{ p.design_qty === null ? '理论方量待补' : '理论方量（人工填写）' }} <a v-if="canWrite" @click="editDesign">{{ p.design_qty === null ? '填写' : '修改' }}</a></div></div>
        <div class="xt-kpi"><div class="v" :class="{warn: p.deviation_flag}">{{ p.deviation_pct === null ? '—' : (p.deviation_pct > 0 ? '+' : '') + p.deviation_pct + '%' }}</div><div class="l">偏差（阈值 ±{{ p.threshold_pct }}%）</div></div>
        <div class="xt-kpi"><div class="v" :class="{warn: p.specimens.missing > 0}">{{ p.specimens.required === null ? '—' : p.specimens.required }} / {{ p.specimens.registered }}</div><div class="l">应留 / 已留试块（组）</div></div>
      </div>
      <div class="banner warn mt" v-if="p.deviation_flag">{{ p.deviation_text }}</div>
      <div class="tiny muted mt" v-else-if="p.deviation_text">{{ p.deviation_text }}{{ p.design_qty === null ? '：填写后比对，系统不估算。' : '' }}</div>
      <div class="banner warn mt" v-if="p.specimens.missing > 0">疑似少留试块：应留 {{ p.specimens.required }} 组，已登记 {{ p.specimens.registered }} 组，差 {{ p.specimens.missing }} 组。请核实。</div>
      <div class="tiny muted mt">{{ p.specimens.basis }}</div>
      <div class="tiny muted" v-if="p.grade_mismatch">{{ p.grade_mismatch }} 张小票强度等级与本单元不一致，请核对归属。</div>

      <div class="sec"><div class="sec-h"><h3>车次记录</h3><span class="tiny muted">票面缺失项显示“未记录”</span>
        <button class="btn ghost sm" @click="showRec = !showRec">{{ showRec ? '收起' : '展开 ' + p.trucks + ' 车' }}</button></div>
        <div class="scroll-x" v-if="showRec && p.records && p.records.length"><table class="t nw ra-tbl"><thead><tr><th>时间</th><th>车号</th><th>方量</th><th>坍落度</th><th>入模温度</th><th>票号</th></tr></thead>
          <tbody><tr v-for="r in p.records" :key="r.ticket_id"><td data-l="时间">{{ r.time.slice(5) }}</td><td data-l="车号">{{ r.vehicle || '未记录' }}</td><td data-l="方量">{{ q2(r.qty) }}</td>
            <td data-l="坍落度" :class="{muted: r.slump === null}">{{ r.slump === null ? '未记录' : r.slump + ' mm' }}</td><td data-l="入模温度" :class="{muted: r.temp === null}">{{ r.temp === null ? '未记录' : r.temp + ' ℃' }}</td><td data-l="票号">{{ r.ticket_no || '—' }}</td></tr></tbody></table></div>
        <div class="tiny muted" v-if="showRec && !(p.records && p.records.length)">暂无已确认小票</div></div>

      <div class="sec"><div class="sec-h"><h3>试块（留置 {{ p.specimens.registered }} 组）</h3>
        <button class="btn sm" v-if="canWrite" @click="adding = !adding"><ic n="plus"></ic>登记试块</button></div>
        <div class="xt-grid" v-if="adding"><label class="xt-f"><span class="xt-fl">组号</span><input class="in" v-model.trim="sp.group_no" :placeholder="'默认第 ' + (p.specimens.registered + 1) + ' 组'"></label>
          <label class="xt-f"><span class="xt-fl">成型日期</span><input class="in" type="date" v-model="sp.molded_date" :max="today()"></label>
          <label class="xt-f"><span class="xt-fl">养护方式</span><select class="in" v-model="sp.curing"><option>标准养护</option><option>同条件养护</option></select></label>
          <div class="xt-f"><span class="xt-fl">&nbsp;</span><button class="btn primary" :disabled="busy" @click="addSpec">登记</button></div></div>
        <div class="xt-spec" v-for="s in p.specimen_list" :key="s.id">
          <div class="row wrap"><b>第 {{ s.group_no }} 组</b><span class="tiny muted">{{ s.curing }} · 成型 {{ s.molded_date }}</span>
            <span :class="['chip', chip(s.age7)]">7天 {{ s.age7.due.slice(5) }} {{ label(s.age7, s.r7) }}</span>
            <span :class="['chip', chip(s.age28)]">28天 {{ s.age28.due.slice(5) }} {{ label(s.age28, s.r28) }}</span></div>
          <div class="tiny" v-if="s.r7">7天：{{ s.r7.mpa }} MPa{{ s.r7.report_no ? ' · 报告 ' + s.r7.report_no : '' }}{{ s.r7.tested_at ? ' · ' + s.r7.tested_at : '' }}</div>
          <div class="tiny" v-if="s.r28">28天：{{ s.r28.mpa }} MPa{{ s.r28.report_no ? ' · 报告 ' + s.r28.report_no : '' }}{{ s.r28.tested_at ? ' · ' + s.r28.tested_at : '' }}<div class="muted">{{ s.r28.ref }}</div></div>
          <div class="row wrap mt" v-if="quality && forms[s.id] && !(s.r28 && s.r7)">
            <select class="in xt-age" v-model.number="forms[s.id].age"><option :value="28" v-if="!s.r28">28天</option><option :value="7" v-if="!s.r7">7天</option></select>
            <input class="in xt-num" type="number" step="any" v-model="forms[s.id].mpa" placeholder="强度 MPa"><input class="in xt-rep" v-model.trim="forms[s.id].report_no" placeholder="报告号">
            <input class="in xt-date" type="date" v-model="forms[s.id].tested_at" :max="today()"><button class="btn sm primary" :disabled="busy" @click="saveRes(s)">录入试验室结果</button></div>
          <div class="tiny muted" v-else-if="!quality && !s.r28">试验结果由工程主管/项目经理录入，强度评定以试验室/监理为准。</div></div>
        <div class="tiny muted" v-if="!p.specimen_list.length">暂无试块登记</div></div>
      <div class="banner warn mt" v-if="err">{{ err }}</div>
    </section>`,
    data: () => ({ POUR_ST, showRec: false, adding: false, busy: false, err: "", sp: { group_no: "", molded_date: "", curing: "标准养护" }, forms: {} }),
    computed: { canWrite, quality() { return canQuality(); } },
    watch: { "p.specimen_list": { immediate: true, handler(list) { for (const s of list || []) if (!this.forms[s.id]) this.forms[s.id] = { age: s.r28 ? 7 : 28, mpa: "", report_no: "", tested_at: "" }; } } },
    methods: {
      q2, today,
      chip(a) { return { done: "green", overdue: "red", soon: "orange", ok: "" }[a.state]; },
      label(a, r) { return a.state === "done" ? "已录入" : a.state === "overdue" ? "超期 " + -a.days_left + " 天未录" : a.state === "soon" ? "还剩 " + a.days_left + " 天" : ""; },
      async run(fn) { this.busy = true; this.err = ""; try { await fn(); this.$emit("changed"); } catch (e) { this.err = errMsg(e); } finally { this.busy = false; } },
      setStatus(v) { return this.run(() => PUT("/api/pours/" + this.p.id, { status: v })); },
      editDesign() {
        const v = window.prompt("理论方量（m³，留空表示待补）", this.p.design_qty === null ? "" : this.p.design_qty);
        if (v === null) return;
        return this.run(() => PUT("/api/pours/" + this.p.id, { design_qty: v.trim() === "" ? null : +v }));
      },
      addSpec() { return this.run(async () => { await POST(`/api/pours/${this.p.id}/specimens`, { group_no: this.sp.group_no, molded_date: this.sp.molded_date || today(), curing: this.sp.curing }); this.adding = false; this.sp.group_no = ""; toast("试块已登记"); }); },
      saveRes(s) {
        const f = this.forms[s.id];
        if (f.mpa === "") { this.err = "请填写强度值"; return; }
        return this.run(async () => { await POST(`/api/specimens/${s.id}/result`, { age: f.age, mpa: +f.mpa, report_no: f.report_no, tested_at: f.tested_at || null }); delete this.forms[s.id]; toast("已录入，强度评定以试验室/监理为准"); });
      },
    },
  };

  // ---------------------------------------------------------------------- 材料批次行
  const XtBatch = {
    props: ["b"],
    emits: ["changed"],
    template: `<section class="zb-card xt-card">
      <div class="row between wrap"><div class="row wrap"><span class="chip blue">{{ b.category_name }}</span><b>{{ b.name }}</b><span class="muted">{{ b.spec }}</span>
          <span class="tiny muted">批号 {{ b.batch_no || '未记录' }}</span></div>
        <span :class="['chip', RC[b.retest_status]]">{{ b.retest_name }}</span></div>
      <div class="row wrap mt small"><span>累计 <b>{{ q2(b.qty) }}</b> {{ b.unit }}</span><span class="muted">到场 {{ b.arrived_at || '—' }}</span><span class="muted">{{ b.supplier }}</span>
        <span class="muted" v-if="b.report_no">报告 {{ b.report_no }}{{ b.reported_at ? ' · ' + b.reported_at : '' }}</span></div>
      <div class="tiny muted mt" v-if="b.sample_hint">{{ b.sample_hint }}</div>
      <div class="tiny muted mt">来自 {{ b.tickets.length }} 张票据：{{ b.tickets.map((t) => t.kind_name + (t.qty ? ' ' + q2(t.qty) + t.unit : '')).join('；') }}</div>
      <div class="row wrap mt" v-if="canWrite">
        <button class="btn sm" v-if="b.retest_status === 'pending'" :disabled="busy" @click="mark('sampled')">标记已送检</button>
        <button class="btn sm" v-if="b.retest_status === 'sampled'" :disabled="busy" @click="mark('pending')">改回待取样</button>
        <template v-if="quality && ['pending', 'sampled', 'fail', 'pass'].includes(b.retest_status)">
          <input class="in xt-rep" v-model.trim="rep" placeholder="复试报告号"><input class="in xt-date" type="date" v-model="rdate" :max="today()">
          <button class="btn sm primary" :disabled="busy" @click="mark('pass')">录入：合格</button><button class="btn sm danger" :disabled="busy" @click="mark('fail')">录入：不合格</button></template>
        <button class="btn ghost sm" v-if="quality && b.retest_status === 'not_required'" :disabled="busy" @click="mark('pending')">改为需复试</button>
        <button class="btn ghost sm" v-else-if="quality && b.retest_status === 'pending'" :disabled="busy" @click="mark('not_required')">无需复试</button></div>
      <div class="banner warn mt" v-if="err">{{ err }}</div>
    </section>`,
    data() { return { RC: RETEST_CHIP, busy: false, err: "", rep: this.b.report_no || "", rdate: "" }; },
    computed: { canWrite, quality() { return canQuality(); } },
    methods: {
      q2, today,
      async mark(st) {
        this.err = "";
        if ((st === "pass" || st === "fail") && !this.rep) { this.err = "请先填写复试报告号"; return; }
        this.busy = true;
        try { await PUT(`/api/batches/${this.b.id}/retest`, { status: st, report_no: this.rep, reported_at: this.rdate || null }); toast("已更新复试状态"); this.$emit("changed"); }
        catch (e) { this.err = errMsg(e); } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 规则设置（项目经理/工程主管）
  const XtRules = {
    emits: ["saved"],
    template: `<section class="zb-card xt-card" v-if="r">
      <div class="row between"><h3>规则设置</h3><span class="tiny muted">{{ r.can_edit ? '' : '只读' }}</span></div>
      <div class="tiny muted mt">{{ r.note_concrete }}</div><div class="tiny muted">{{ r.note_material }}</div>
      <div class="xt-grid mt">
        <label class="xt-f"><span class="xt-fl">每组试块方量 m³</span><input class="in" type="number" step="any" v-model.number="r.concrete_rules.specimen_per_m3" :disabled="!r.can_edit"></label>
        <label class="xt-f"><span class="xt-fl">每日最少试块组数</span><input class="in" type="number" v-model.number="r.concrete_rules.specimens_per_day" :disabled="!r.can_edit"></label>
        <label class="xt-f"><span class="xt-fl">方量偏差阈值 ±%</span><input class="in" type="number" step="any" v-model.number="r.concrete_rules.deviation_pct" :disabled="!r.can_edit"></label>
        <label class="xt-f"><span class="xt-fl">28天提前提醒（天）</span><input class="in" type="number" v-model.number="r.concrete_rules.warn_days" :disabled="!r.can_edit"></label>
        <label class="xt-f"><span class="xt-fl">钢筋批量上限 t</span><input class="in" type="number" step="any" v-model.number="r.material_rules.rebar.batch_t" :disabled="!r.can_edit"></label>
        <label class="xt-f"><span class="xt-fl">袋装水泥批量上限 t</span><input class="in" type="number" step="any" v-model.number="r.material_rules.cement.bag_t" :disabled="!r.can_edit"></label>
        <label class="xt-f"><span class="xt-fl">散装水泥批量上限 t</span><input class="in" type="number" step="any" v-model.number="r.material_rules.cement.bulk_t" :disabled="!r.can_edit"></label>
      </div>
      <div class="row wrap mt"><span class="small">需复试类别：</span>
        <label v-for="k in ['rebar','cement','sand','stone','admixture']" :key="k" class="row small"><input type="checkbox" v-model="r.material_rules[k].required" :disabled="!r.can_edit"> {{ CAT[k] }}</label></div>
      <div class="tiny muted mt"><help-tip caption="生效范围" text="规则调整仅适用于后续进场批次。"></help-tip></div>
      <div class="row mt" v-if="r.can_edit"><button class="btn primary" :disabled="busy" @click="save">保存规则</button></div>
      <div class="banner warn mt" v-if="err">{{ err }}</div></section>`,
    data: () => ({ r: null, CAT, busy: false, err: "" }),
    async mounted() { try { this.r = await GET("/api/tickets/rules"); } catch (e) { this.err = errMsg(e); } },
    methods: {
      async save() {
        this.busy = true; this.err = "";
        try {
          const mr = {};
          for (const k of ["rebar", "cement", "sand", "stone", "admixture"]) { const m = this.r.material_rules[k]; mr[k] = { required: m.required, batch_t: m.batch_t, bag_t: m.bag_t, bulk_t: m.bulk_t }; }
          this.r = await PUT("/api/tickets/rules", { concrete_rules: this.r.concrete_rules, material_rules: mr });
          toast("规则已保存"); this.$emit("saved");
        } catch (e) { this.err = errMsg(e); } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 页面
  const TicketsPage = {
    components: { XtDraft, XtPour, XtBatch, XtRules, ...(typeof ListDigest !== "undefined" ? { ListDigest } : {}) },   // 小结组件在 x_digest.js；单独加载本文件（如测试）时没有它就不挂
    template: `<div class="xt ra">
      <page-head page="tickets"><template #meta><template v-if="mock"><i class="zb-sys">离线模拟模型</i>识别字段为演示数据，须对照原图核对</template><template v-else>拍小票识别为草稿，人核对后确认入账；草稿不计入累计</template><template v-if="!canWrite"> · 当前岗位只读</template></template></page-head>
      <div class="xt-snap" v-if="canWrite">
        <button class="btn primary xt-shoot" :disabled="busy" @click="snap"><ic n="camera"></ic>{{ busy ? '上传中…' : '拍小票' }}</button>
        <div class="xt-kindrow"><span class="tiny muted">单据类型</span><div class="seg"><button v-for="k in KIND_PICK" :key="k[0]" :class="{on: kind === k[0]}" @click="setKind(k[0])">{{ k[1] }}</button></div>
          <help-tip caption="怎么拍" text="单次 1 至 4 张视为同一票据，多张票据请分别拍摄；横拍竖拍都可以，过磅单、送货单按材料进场单拍。"></help-tip></div>
      </div>
      <div class="row between wrap xt-tabrow">
        <div class="seg xt-tabs"><button v-for="t in tabs" :key="t[0]" :class="{on: tab === t[0]}" @click="tab = t[0]">{{ t[1] }}<span class="xt-n" v-if="t[2]">{{ t[2] }}</span></button></div>
        <span class="tiny muted" v-if="loading">加载中…</span></div>
      <div class="banner warn mt" v-if="err">{{ err }}</div>

      <div v-show="tab === 'pending'" class="mt">
        <list-digest v-if="drafts.length > 1" kind="tickets" title="票据小结" :lead="dg.lead" key-title="建议先核对" key-hint="识别存疑、疑似重复或识别失败" :keys="dg.keys" :groups="dg.groups"></list-digest>
        <zb-paged v-if="drafts.length" :items="drafts" :size="3" :m-size="1" :fit="false" unit="张" cls="xt-list"><template #default="{ item: t }"><xt-draft :t="t" :now="now" @changed="reload"></xt-draft></template></zb-paged>
        <section class="zb-card xt-card" v-if="!drafts.length && !loading"><div class="zb-empty">暂无待确认票据{{ canWrite ? '，拍小票后在这里核对' : '' }}</div></section>
        <section class="zb-card xt-card" v-if="recent.length"><div class="zb-ch"><ic n="check-circle"></ic><h2>最近已确认</h2><span class="zb-n">{{ recent.length }}</span><span class="grow"></span><span class="zb-src">仅已确认票据计入累计</span></div>
          <zb-paged :items="recent" :size="8" :m-size="2" :fit="140" unit="张" cls="xt-rlist"><template #default="{ item: t }"><div class="xt-recent">
            <ev-photo :src="t.media.length ? t.media[0].thumb : ''" :full="t.media.length ? t.media[0].url : ''" size="s"></ev-photo>
            <div class="grow xt-rb"><div class="row wrap"><span class="chip green">已确认</span><b>{{ t.kind_name }}</b><span class="small">{{ summary(t) }}</span></div>
              <div class="tiny muted">{{ t.uploader }} · {{ fmtDT(t.confirmed_at) }}<template v-if="t.pour"> · 归入 {{ t.pour.label }}</template></div>
              <span class="chip orange" v-if="!t.pour && t.doc_kind === 'concrete_ticket'">未归浇筑单元</span></div>
            <div class="xt-ra"><button class="btn sm" v-if="t.doc_kind === 'concrete_ticket' && !t.pour && canWrite" @click="assign(t)">归入单元</button>
              <button class="btn ghost sm" v-if="t.can_void" @click="voidConfirmed(t)">作废</button></div></div></template></zb-paged></section>
      </div>

      <div v-show="tab === 'pours'" class="mt">
        <div class="row between wrap mb"><div class="small muted">{{ poursNote }}</div><button class="btn" v-if="canWrite" @click="newPour = !newPour"><ic n="plus"></ic>新建浇筑单元</button></div>
        <section class="zb-card xt-card" v-if="newPour"><div class="xt-grid">
          <label class="xt-f"><span class="xt-fl">部位<b class="xt-req">*</b></span><input class="in" v-model.trim="np.part" placeholder="如 3#墩承台"></label>
          <label class="xt-f"><span class="xt-fl">强度等级</span><input class="in" v-model.trim="np.grade" placeholder="C30"></label>
          <label class="xt-f"><span class="xt-fl">浇筑日期</span><input class="in" type="date" v-model="np.pour_date"></label>
          <label class="xt-f"><span class="xt-fl">理论方量 m³（选填）</span><input class="in" type="number" step="any" v-model="np.design_qty" placeholder="留空显示“理论方量待补”"></label>
          <label class="xt-f"><span class="xt-fl">P6 作业（选填）</span><input class="in" v-model.trim="np.activity_code"></label></div>
          <div class="row mt"><button class="btn primary" @click="createPour">创建</button><button class="btn" @click="newPour = false">取消</button></div></section>
        <zb-paged v-if="pours.length" :items="pours" :size="6" :m-size="3" :fit="240" unit="个单元" cls="xt-list"><template #default="{ item: p }"><xt-pour :p="p" :open="focusPour === p.id" @changed="reload"></xt-pour></template></zb-paged>
        <section class="zb-card xt-card" v-if="!pours.length && !loading"><div class="zb-empty">暂无浇筑单元。确认混凝土小票时可归入或新建单元，方量按已确认小票累计。</div></section>
      </div>

      <div v-show="tab === 'batches'" class="mt">
        <div class="row wrap mb"><div class="seg"><button :class="{on: !cat}" @click="cat = ''">全部</button><button v-for="(n, k) in CAT" :key="k" :class="{on: cat === k}" @click="cat = k">{{ n }}</button></div>
          <span class="tiny muted">{{ batchNote }}</span></div>
        <zb-paged v-if="shownBatches.length" :items="shownBatches" :size="6" :m-size="3" :fit="240" unit="批" cls="xt-list"><template #default="{ item: b }"><xt-batch :b="b" @changed="reload"></xt-batch></template></zb-paged>
        <section class="zb-card xt-card" v-if="!shownBatches.length && !loading"><div class="zb-empty">{{ cat ? '该类别暂无材料批次' : '暂无材料批次。材料进场单确认入账后自动生成批次与复试台账。' }}</div></section>
      </div>

      <div v-show="tab === 'alerts'" class="mt">
        <div class="tiny muted mb">{{ risks.note }}</div>
        <zb-paged v-if="risks.items.length" :items="risks.items" :size="6" :m-size="3" :fit="240" cls="xt-list"><template #default="{ item: i }"><section class="zb-card xt-card xt-risk">
          <div class="row wrap"><span :class="['chip', LV[i.level]]">{{ i.level === 'high' ? '重点核对' : i.level === 'medium' ? '请核实' : '提示' }}</span><b>{{ i.title }}</b></div>
          <div class="small mt">{{ i.detail }}</div>
          <div class="tiny muted mt" v-for="b in i.basis.slice(0, 8)" :key="b">· {{ b }}</div>
          <div class="row mt"><button class="btn sm" @click="jump(i)">去处理</button></div></section></template></zb-paged>
        <section class="zb-card xt-card" v-if="!risks.items.length && !loading"><div class="zb-empty">暂无待核对疑点</div></section>
        <div class="sec" v-if="rulesOpen || canRules"><div class="sec-h"><h3>规则</h3><button class="btn ghost sm" @click="rulesOpen = !rulesOpen">{{ rulesOpen ? '收起' : '查看/调整' }}</button></div>
          <xt-rules v-if="rulesOpen" @saved="reload"></xt-rules></div>
      </div>
    </div>`,
    data() {
      return { KIND_PICK, CAT, LV: LV_CHIP, kind: lsGet("zb_xt_kind", "auto"), tab: store.route.params.tab || "pending", busy: false, loading: false, err: "",
        drafts: [], recent: [], pours: [], batches: [], poursNote: "", batchNote: "", risks: { items: [], note: "", counts: {}, pending_tickets: 0 }, cat: store.route.params.category || "",
        newPour: false, np: { part: "", grade: "", pour_date: today(), design_qty: "", activity_code: "" }, rulesOpen: false, focusPour: +store.route.params.pour || 0, timer: 0, polls: 0,
        now: nowMs(), clock: 0 };
    },
    computed: {
      // 票据小结（2026-10-08）：只用待确认票据已有字段——单据类型、存疑字段、疑似重复、识别失败；不另算口径
      dg() {
        const ds = this.drafts, bad = (t) => Object.keys(t.doubts || {}).length || (t.dups || []).length || t.ai_status === "failed";
        const keys = ds.filter(bad).map((t) => ({ id: t.id, title: t.kind_name + (t.uploader ? " · " + t.uploader : ""), tag: t.ai_status === "failed" ? "识别失败" : (t.dups || []).length ? "疑似重复" : "存疑", tone: "orange",
          why: t.ai_status === "failed" ? "可重新识别或手填" : (t.dups || []).length ? "与已有票据相似" : Object.keys(t.doubts || {}).length + " 个字段存疑" }));
        const by = {};
        for (const t of ds.filter((x) => !bad(x))) (by[t.kind_name] = by[t.kind_name] || []).push(t);
        const groups = Object.entries(by).map(([k, ts]) => ({ k, title: `${ts.length} 张${k}`, tag: "无疑点", tone: "green", text: ts.some((t) => t.ai_status === "running") ? "部分还在识别中" : "识别完成，没有存疑字段，可以逐张快速确认" }));
        return { lead: `共 ${ds.length} 张待确认票据` + (keys.length ? `，其中 ${keys.length} 张需要先核对` : "，都没有存疑字段") + (groups.length ? `；其余 ${ds.length - keys.length} 张按单据类型归成 ${groups.length} 组。` : "。"), keys, groups };
      },
      canWrite, canRules, mock: isMock,
      tabs() {
        const ra = this.risks.items.filter((i) => i.level !== "low").length;
        return [["pending", "待确认", this.drafts.length], ["pours", "浇筑与试块", 0], ["batches", "材料批次", 0], ["alerts", "预警", ra]];
      },
      shownBatches() { return this.batches.filter((b) => !this.cat || b.category === this.cat); },
    },
    mounted() { this.reload(); this.clock = setInterval(() => (this.now = nowMs()), 30000); },  // 每 30 秒刷新“已等待”与超时判断
    unmounted() { clearTimeout(this.timer); clearInterval(this.clock); },
    methods: {
      fmtDT,
      setKind(k) { this.kind = k; lsSet("zb_xt_kind", k); },
      summary(t) {
        const f = t.fields;
        if (t.doc_kind === "concrete_ticket") return `${f.grade || ''} ${q2(f.qty)} m³${f.slump !== null && f.slump !== undefined ? ' · 坍落度 ' + f.slump : ''}${f.vehicle ? ' · ' + f.vehicle : ''}`;
        if (t.doc_kind === "material_receipt") return `${f.material || ''} ${f.spec || ''} ${q2(f.qty)}${f.unit || ''}${f.batch_no ? ' · 批号 ' + f.batch_no : ''}`;
        return `${f.material || ''} ${f.batch_no ? '批号 ' + f.batch_no : ''}`;
      },
      async reload() {
        this.loading = true; this.err = "";
        try {
          const [d, c, p, b, r] = await Promise.all([GET("/api/tickets?status=draft&limit=50"), GET("/api/tickets?status=confirmed&limit=8"), GET("/api/pours"), GET("/api/batches"), GET("/api/tickets/risks")]);
          this.drafts = d.items.slice().reverse(); this.recent = c.items; this.pours = p.items; this.poursNote = p.note; this.batches = b.items; this.batchNote = b.note; this.risks = r;
          this.now = nowMs();
          if (!this.drafts.some((t) => xtRecognizing(t) && !xtStuck(t, this.now))) this.polls = 0;
        } catch (e) { this.err = errMsg(e); } finally { this.loading = false; }
        this.schedule();
      },
      // 只为还在正常识别的票据轮询：前 2 分钟 2 秒一次，之后 15 秒一次；超过 10 分钟的按识别超时处理，不再轮询
      schedule() {
        clearTimeout(this.timer);
        if (this.drafts.some((t) => xtRecognizing(t) && !xtStuck(t, this.now))) {
          this.polls++;
          this.timer = setTimeout(async () => {
            try { const d = await GET("/api/tickets?status=draft&limit=50", { quiet: true }); this.drafts = d.items.slice().reverse(); } catch (_) { /* 下一轮再试 */ }
            this.now = nowMs();
            this.schedule();
          }, this.polls <= 60 ? 2000 : 15000);
        }
      },
      async snap() {
        let files = await takePhotos({ allowPortrait: true }); if (!files.length) return;  // 小票横拍竖拍都收（AC-7）
        if (files.length > 4) { toast("单张票据最多 4 张照片，已取前 4 张", true); files = files.slice(0, 4); }
        this.busy = true;
        try {
          const fd = new FormData(); fd.append("doc_kind", this.kind); addPhotos(fd, files);
          const t = await api("POST", "/api/tickets/upload", fd, { noQueue: true });
          this.tab = "pending"; this.polls = 0;
          this.drafts = [t, ...this.drafts.filter((x) => x.id !== t.id)].slice().sort((a, b) => a.id - b.id);
          this.schedule();
          toast("已上传，识别中");
        } catch (e) { toast(errMsg(e), true); } finally { this.busy = false; }
      },
      async createPour() {
        if (!this.np.part) { toast("请填写部位", true); return; }
        try {
          await POST("/api/pours", { part: this.np.part, grade: this.np.grade || null, pour_date: this.np.pour_date || null,
            design_qty: this.np.design_qty === "" ? null : +this.np.design_qty, activity_code: this.np.activity_code || "" });
          this.newPour = false; this.np = { part: "", grade: "", pour_date: today(), design_qty: "", activity_code: "" }; toast("浇筑单元已创建"); this.reload();
        } catch (e) { toast(errMsg(e), true); }
      },
      async assign(t) {
        const list = this.pours.filter((p) => p.status !== "done");
        if (!list.length) { toast("暂无可归入的浇筑单元，请先新建", true); this.tab = "pours"; return; }
        const pick = window.prompt("归入浇筑单元，输入编号：\n" + list.map((p, i) => `${i + 1}. ${p.label}`).join("\n"), "1");
        const p = list[(+pick || 0) - 1]; if (!p) return;
        try { const r = await POST(`/api/tickets/${t.id}/pour`, { pour_id: p.id }); toast("已归入 " + p.label + (r.warnings && r.warnings.length ? "；" + r.warnings.join("；") : ""), !!(r.warnings && r.warnings.length)); this.reload(); } catch (e) { toast(errMsg(e), true); }
      },
      async voidConfirmed(t) {
        if (!window.confirm("确认作废该票据？其方量/数量将从累计中扣除。")) return;
        try { await POST(`/api/tickets/${t.id}/void`, { reason: "更正" }); toast("已作废并重新累计"); this.reload(); } catch (e) { toast(errMsg(e), true); }
      },
      jump(i) {
        const l = i.link || {};
        this.tab = l.tab || "pending";
        if (l.category) this.cat = l.category;
        if (l.pour_id) { this.focusPour = l.pour_id; this.$nextTick(() => { const el = document.getElementById("xt-pour-" + l.pour_id); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); }); }
        else window.scrollTo({ top: 0, behavior: "smooth" });
      },
    },
  };

  // 材料与砼疑点是首页统一待办（/api/todo）的来源之一；页面入口只在导航“材料与砼”页签，施工日志页不再放第二入口
  EXT.route("tickets", TicketsPage);
})();
