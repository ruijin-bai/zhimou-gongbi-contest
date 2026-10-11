/* x_conceal：资料Agent 📑 隐蔽工程影像记录自动成册。
   随做随拍 → 资料Agent识别每张照片拍的是什么、对照模板找缺拍项 → 人确认并录入实测数据 → 报验 → 记录监理结果 → 导出 Word。
   技术员手机优先，全程在记录页就地完成；工程主管/项目经理在电脑上看“缺资料扫描”和部位完成度。
   AI 识别结果、人工录入值、监理结果分开存放；有缺项时绝不显示“可报验”。 */
(function () {
  const ST = { draft: "待补资料", ready: "已齐待报验", submitted: "已报验", passed: "监理通过", rejected: "监理退回" };
  const ST_CHIP = { draft: "orange", ready: "blue", submitted: "blue", passed: "green", rejected: "red" };
  const GROUPS = [
    { key: "todo", title: "待补资料", sts: ["draft", "rejected"], hint: "还在拍、还在补，或被监理退回", icon: "camera" },
    { key: "ready", title: "已齐待报验", sts: ["ready"], hint: "人已确认，等报验", icon: "check-circle" },
    { key: "sub", title: "已报验", sts: ["submitted"], hint: "等监理结果", icon: "send" },
    { key: "ok", title: "已通过", sts: ["passed"], hint: "监理已通过", icon: "check-circle" },
  ];
  const READERS = ["manager", "eng_sup", "tech", "leader", "safety_sup"];
  const canRead = () => !!store.me && READERS.includes(roleOf());
  const photoCap = (p) => (p.taken_at ? "拍摄 " + fmtDT(p.taken_at) : "拍摄时间缺失");
  const geoText = (g) => (!g ? "未取得定位" : g.place || (g.lat != null ? g.lat.toFixed(5) + "," + g.lon.toFixed(5) : "未取得定位"));
  const enc = encodeURIComponent;
  const UNASSIGNED = "未归部位";

  // ---------------------------------------------------------------------- 记录页（就地完成：拍 → 整理 → 补数据 → 确认 → 报验 → 监理结果）
  const CcRecord = {
    props: ["id"],
    components: { VoiceButton },
    template: `<div class="cc ra" v-if="v">
      <header class="ph ph-plain cc-head"><div class="ph-row"><div class="ph-main"><div class="ph-title"><a class="cc-back" title="返回隐蔽验收" @click="back">←</a><h1>{{ v.title }}</h1>
          <span :class="['chip', stChip[v.status]]">{{ v.status_name }}</span><span class="chip orange" v-if="v.forced">带缺项确认</span></div>
        <div class="ph-meta tiny muted">{{ v.part }}<template v-if="v.activity_code"> · <act-name :c="v.activity_code" :n="v.activity_name" :s="v.activity_short"></act-name></template> · {{ v.template.name }} · 编制 {{ v.created_by }}
          <template v-if="!v.can.edit"> · 验收日期 {{ v.inspection_date }}</template><template v-if="v.ai.analyzed && v.ai.mock"> · <i class="zb-sys">离线模拟识别</i>仅用于开发测试</template></div></div>
        <div class="ph-act" v-if="v.can.edit"><label class="small">验收日期 <input type="date" class="in cc-date" v-model="date" @change="saveDate"></label>
          <help-tip text="引用既有照片限验收日期前后 1 天"></help-tip></div></div></header>

      <div :class="['cc-check', v.check.complete ? 'ok' : 'bad']">
        <div class="cc-conc"><ic :n="v.check.complete ? 'check-circle' : 'alert'"></ic>{{ v.check.conclusion }}</div>
        <div class="small" v-if="!v.check.complete">缺拍 {{ v.check.missing_shots.length }} 项 · 待补数据 {{ v.check.pending_items.length }} 项
          <span class="muted">（必拍 {{ v.check.counts.shots_required }} 项、必填 {{ v.check.counts.items_required }} 项）</span></div>
        <div class="row wrap mt" v-if="!v.check.complete"><span class="chip red" v-for="m in v.check.missing_shots" :key="'s' + m.key">缺拍 · {{ m.label }}</span>
          <span class="chip orange" v-for="m in v.check.pending_items" :key="'i' + m.key">待补 · {{ m.label }}</span></div>
        <div class="tiny cc-warn" v-for="(w, i) in v.check.warnings" :key="i">• {{ w.text }}</div>
        <div class="tiny muted mt" v-if="v.forced">缺项说明：{{ v.ready_note }}</div></div>

      <section class="zb-card mb"><div class="zb-ch"><ic n="camera"></ic><h2>① 必拍照片</h2><span class="grow"></span><span class="tiny muted">拍照或选取该部位已有照片</span></div>
        <div class="cc-shots">
          <div v-for="s in v.template.shots" :key="s.key" :class="['cc-shot', isMiss(s) ? 'miss' : '']">
            <div class="cc-shot-h"><b>{{ s.label }}</b><span class="chip" v-if="!s.required">可选</span><span class="chip red" v-if="isMiss(s)">缺拍</span></div>
            <div class="tiny muted" v-if="s.hint">{{ s.hint }}</div>
            <div class="cc-photos"><div class="cc-ph" v-for="p in photosOf(s.key)" :key="p.media_id">
              <ev-photo :src="p.thumb" :full="p.url" size="f" :time="cap(p)"></ev-photo><button class="cc-x" v-if="v.can.edit" title="从记录取下" @click="unlink(p)">×</button>
              <div class="tiny muted">{{ geo(p.geo) }}</div>
              <div class="tiny cc-ai" v-if="p.ai"><span class="chip blue">AI</span> {{ p.ai.scene }}<span class="chip orange" v-if="p.ai.clear === false">不够清晰</span>
                <div v-for="(r, j) in p.ai.readings" :key="j" class="muted">读数：{{ r.label }} {{ r.value != null ? r.value + (r.unit || '') : (r.text || '') }}<span class="cc-doubt" v-if="r.doubt">（存疑：{{ r.doubt }}）</span></div></div></div></div>
            <div class="row wrap" v-if="v.can.edit"><button class="btn" :disabled="busy" @click="shoot(s.key)"><ic n="camera"></ic>拍照</button><button class="btn" :disabled="busy" @click="pick(s.key)"><ic n="image"></ic>选已有</button></div></div>
          <div class="cc-shot loose" v-if="loose.length || v.can.edit">
            <div class="cc-shot-h"><b>未归类照片</b><span class="tiny muted">由资料智能体归类</span></div>
            <div class="cc-photos"><div class="cc-ph" v-for="p in loose" :key="p.media_id">
              <ev-photo :src="p.thumb" :full="p.url" size="f" :time="cap(p)"></ev-photo><button class="cc-x" v-if="v.can.edit" title="从记录取下" @click="unlink(p)">×</button>
              <div class="tiny muted">{{ geo(p.geo) }}</div>
              <div class="tiny cc-ai" v-if="p.ai"><span class="chip blue">AI</span> {{ p.ai.scene }}<span class="chip orange" v-if="p.ai.clear === false">不够清晰</span></div>
              <div class="tiny" v-if="p.ai && p.ai.shot && v.can.edit">AI 建议：{{ shotName(p.ai.shot) }} <a @click="setShot(p, p.ai.shot)">采用</a></div>
              <select class="in cc-sel" v-if="v.can.edit" @change="setShot(p, $event.target.value)"><option value="">归入</option><option v-for="s in v.template.shots" :key="s.key" :value="s.key">{{ s.label }}</option></select></div></div>
            <div class="row wrap" v-if="v.can.edit"><button class="btn" :disabled="busy" @click="shoot('')"><ic n="camera"></ic>拍照</button><button class="btn" :disabled="busy" @click="pick('')"><ic n="image"></ic>选已有</button>
              <button class="btn" v-if="hasSuggest" :disabled="busy" @click="acceptAi">采用全部 AI 归类建议</button></div></div></div></section>

      <section class="zb-card mb" v-if="v.can.analyze || v.narration || v.ai.analyzed"><div class="zb-ch"><ic n="mic"></ic><h2>② 口述与智能体整理</h2><help-tip text="口述数值列为候选，点“采用”后写入；仅记录画面可见内容，不推测尺寸。"></help-tip><span class="grow"></span>
          <span class="zb-src ai" v-if="v.ai.analyzed && !v.ai.mock">AI 已整理 {{ fmtDT(v.ai.analyzed_at) }}</span><span class="zb-src" v-else-if="v.ai.analyzed"><i class="zb-sys">离线模拟识别</i>{{ fmtDT(v.ai.analyzed_at) }}</span></div>
        <div v-if="v.can.edit"><textarea class="in" rows="3" v-model="narr" maxlength="1500" @input="narrDirty = true" placeholder="口述部位与实测数值（可语音）"></textarea>
          <div class="row wrap mt"><voice-button @text="t => { narr = (narr ? narr + '，' : '') + t; narrDirty = true; }"></voice-button>
            <button class="btn ai" :disabled="busy || (!v.photos.length && !narr.trim())" @click="analyze"><template v-if="busy === 'ai'">整理中</template><template v-else><ic n="file"></ic>智能体整理</template></button></div></div>
        <div class="small pre" v-else-if="v.narration">{{ v.narration }}</div></section>

      <section class="zb-card mb"><div class="zb-ch"><ic n="edit"></ic><h2>③ 验收数据（由人录入）</h2><span class="grow"></span><span class="tiny muted">设计值与允许偏差以设计文件、规范及监理要求为准，人工填写</span></div>
        <div v-for="it in v.template.items" :key="it.key" :class="['cc-item', isPend(it) ? 'pend' : '']">
          <div class="row wrap"><b>{{ it.label }}</b><span class="chip red" v-if="isPend(it)">待补</span><span class="chip" v-if="!it.required">选填</span>
            <span class="tiny muted" v-if="it.measure">须现场实测录入</span></div>
          <div class="cc-inrow"><input class="in" v-model.trim="form[it.key].design" :disabled="!v.can.edit" maxlength="40" placeholder="设计值/要求值（选填）" @input="dirty[it.key] = true">
            <input class="in" :type="it.type === 'number' ? 'number' : 'text'" step="any" inputmode="decimal" v-model.trim="form[it.key].value" :disabled="!v.can.edit" placeholder="实测/记录值" @input="form[it.key].src = '人工'; dirty[it.key] = true">
            <span class="cc-unit">{{ it.unit }}</span></div>
          <div class="tiny muted" v-if="form[it.key].src && form[it.key].value !== '' && form[it.key].src !== '人工'">来源：{{ form[it.key].src }}（人工确认）</div>
          <div class="tiny cc-doubt" v-if="v.fields[it.key] && v.fields[it.key].doubt">{{ v.fields[it.key].doubt }}</div>
          <div class="tiny" v-for="(c, i) in candOf(it.key)" :key="'c' + i"><template v-if="c.value != null"><span class="chip blue">AI·口述候选</span> {{ c.value }}{{ c.unit }} <a v-if="v.can.edit" @click="adopt(it, c)">采用</a></template>
            <span v-else class="cc-doubt">口述候选存疑，未采用：{{ c.doubt }}</span></div>
          <div class="tiny" v-for="(r, i) in readsOf(it.key)" :key="'r' + i"><span class="chip blue">AI·照片读数</span> {{ r.label }} {{ r.value }}{{ r.unit }} <a v-if="v.can.edit" @click="adoptRead(it, r)">采用</a></div></div>
        <div v-if="!v.template.items.length" class="muted small">该模板没有数据项。</div>
        <div class="row mt" v-if="v.can.edit && v.template.items.length"><button class="btn primary" :disabled="busy" @click="saveFields">保存数据</button></div></section>

      <section class="zb-card mb"><div class="zb-ch"><ic n="check-circle"></ic><h2>④ 确认、报验与监理结果</h2></div>
        <template v-if="v.status === 'draft'">
          <div v-if="v.can.ready">
            <div class="small" v-if="v.check.complete">资料齐全。确认后进入“已齐待报验”，不可再改，可退回补资料。</div>
            <div v-else><div class="small">存在缺项，请补齐；确无法补齐的，须填写缺项说明后确认。</div>
              <textarea class="in mt" rows="2" v-model="readyNote" maxlength="300" placeholder="缺项说明（必填），如：钢筋笼已入孔，照片待补"></textarea></div>
            <div class="row wrap mt"><button class="btn primary" :disabled="busy || (!v.check.complete && !readyNote.trim())" @click="ready"><template v-if="v.check.complete"><ic n="check-circle"></ic>确认齐全</template><template v-else>带缺项确认</template></button>
              <button class="btn danger" :disabled="busy" @click="remove">删除草稿</button></div></div>
          <div class="muted small" v-else>当前角色只读。</div></template>
        <template v-else-if="v.status === 'ready'">
          <div class="small">已确认{{ v.forced ? '（带缺项说明）' : '齐全' }}。报验后等待监理结果。</div>
          <div class="row wrap mt" v-if="v.can.submit"><input class="in cc-sup" v-model.trim="supervisor" maxlength="30" placeholder="监理工程师姓名（选填）">
            <button class="btn primary" :disabled="busy" @click="submit"><ic n="send"></ic>报验</button><button class="btn" :disabled="busy" @click="reopen">退回补资料</button></div></template>
        <template v-else-if="v.status === 'submitted'">
          <div class="small">已于 {{ fmtDT(v.submitted_at) }} 报验<template v-if="v.supervisor_name">，监理：{{ v.supervisor_name }}</template>。</div>
          <div v-if="v.can.result" class="mt"><div class="small muted mb">监理意见与日期由人工录入，系统不判定合格。</div>
            <div class="row wrap"><label class="small"><input type="radio" value="passed" v-model="res.result"> 监理通过</label><label class="small"><input type="radio" value="rejected" v-model="res.result"> 监理退回</label>
              <input type="date" class="in cc-date" v-model="res.date"><input class="in cc-sup" v-model.trim="res.sup" maxlength="30" placeholder="监理姓名（选填）"></div>
            <textarea class="in mt" rows="2" v-model="res.note" maxlength="300" :placeholder="res.result === 'rejected' ? '退回原因（必填）' : '监理意见（选填）'"></textarea>
            <div class="row mt"><button class="btn primary" :disabled="busy || (res.result === 'rejected' && !res.note.trim())" @click="record">记录监理结果</button></div></div>
          <div class="muted small mt" v-else>报验时已发消息给工程主管，等待其录入监理结果。</div></template>
        <template v-else>
          <div class="small"><span :class="['chip', stChip[v.status]]">{{ v.status_name }}</span> {{ v.result_at }}<template v-if="v.supervisor_name"> · {{ v.supervisor_name }}</template></div>
          <div class="pre small mt" v-if="v.result_note">{{ v.result_note }}</div>
          <div class="row mt" v-if="v.can.reopen"><button class="btn" :disabled="busy" @click="reopen">退回补资料</button></div></template>
        <div class="row wrap mt"><a class="btn" :href="'/api/conceal/' + v.id + '/docx'"><ic n="download"></ic>导出本条 Word</a>
          <a class="btn" :href="'/api/conceal/docx?part=' + enc(v.part)"><ic n="download"></ic>导出本部位成册</a><span class="tiny muted">成册仅含已确认记录，签认以纸质或监理系统为准</span></div></section>

      <section class="zb-card" v-if="v.history.length"><div class="zb-ch"><ic n="clock"></ic><h2>处理记录</h2><span class="zb-n">{{ v.history.length }}</span></div><div class="small cc-hist" v-for="(h, i) in v.history" :key="i">{{ fmtDT(h.at) }} · {{ h.name }} · {{ h.action }}<span class="muted" v-if="h.note"> · {{ h.note }}</span></div></section>
      <ai-note v-if="v.notice" :text="v.notice"></ai-note>

      <div class="modal-bg" v-if="picker" @click.self="picker = null"><div class="modal cc-modal"><h2>选部位已有照片</h2>
        <div class="tiny muted mb">{{ picker.note }}</div>
        <div v-if="picker.loading" class="empty">加载中</div>
        <template v-else>
          <div class="small"><b>{{ v.part }}</b> 的照片（{{ picker.photos.length }}）</div>
          <div class="cc-pick"><div v-for="p in picker.photos" :key="p.media_id" :class="['cc-pk', picker.sel.includes(p.media_id) ? 'on' : '', p.in_record ? 'used' : '']">
            <ev-photo :src="p.thumb" :full="p.url" size="f"></ev-photo><div class="tiny">{{ p.taken_at ? fmtDT(p.taken_at).slice(5) : '无拍摄时间' }}</div><div class="tiny muted">{{ p.purpose_name }}</div>
            <button type="button" class="btn sm cc-pk-b" :disabled="p.in_record" @click="toggle(p)">{{ p.in_record ? '已在记录中' : picker.sel.includes(p.media_id) ? '已选 · 取消' : '选取' }}</button></div></div>
          <div v-if="!picker.photos.length" class="muted small">前后 1 天暂无该部位照片。</div>
          <template v-if="picker.unassigned.length"><div class="small mt"><b>未归部位</b>（同期无地点照片，请核对后选取）</div>
            <div class="cc-pick"><div v-for="p in picker.unassigned" :key="p.media_id" :class="['cc-pk', picker.sel.includes(p.media_id) ? 'on' : '', p.in_record ? 'used' : '']">
              <ev-photo :src="p.thumb" :full="p.url" size="f"></ev-photo><div class="tiny">{{ p.taken_at ? fmtDT(p.taken_at).slice(5) : '无拍摄时间' }}</div>
              <button type="button" class="btn sm cc-pk-b" :disabled="p.in_record" @click="toggle(p)">{{ p.in_record ? '已在记录中' : picker.sel.includes(p.media_id) ? '已选 · 取消' : '选取' }}</button></div></div></template>
        </template>
        <div class="row mt" style="justify-content:flex-end"><button class="btn" @click="picker = null">取消</button>
          <button class="btn primary" :disabled="!picker.sel.length || busy" @click="attach">引用 {{ picker.sel.length }} 张</button></div></div></div>
    </div><div v-else class="empty">{{ err || '加载中' }}</div>`,
    data: () => ({ v: null, err: "", busy: false, narr: "", date: "", form: {}, readyNote: "", supervisor: "", res: { result: "passed", note: "", date: "", sup: "" },
                   picker: null, stChip: ST_CHIP, store, dirty: {}, narrDirty: false }),
    computed: {
      loose() { return this.v.photos.filter((p) => !p.shot); },
      hasSuggest() { return this.loose.some((p) => p.ai && p.ai.shot); },
    },
    mounted() { this.load(); },
    methods: {
      fmtDT, enc,
      cap: photoCap, geo: geoText,
      back() { go("conceal"); },
      photosOf(k) { return this.v.photos.filter((p) => p.shot === k); },
      isMiss(s) { return this.v.check.missing_shots.some((m) => m.key === s.key); },
      isPend(it) { return this.v.check.pending_items.some((m) => m.key === it.key); },
      shotName(k) { const s = this.v.template.shots.find((x) => x.key === k); return s ? s.label : k; },
      candOf(k) { return (this.v.ai.fields || []).filter((f) => f.item === k && (f.value != null || f.doubt)); },
      readsOf(k) {
        const out = [];
        this.v.photos.forEach((p) => p.ai && p.ai.readings.forEach((r) => { if (r.item === k && r.value != null) out.push(r); }));
        return out;
      },
      apply(v) {
        this.v = v; this.err = "";
        this.date = v.inspection_date;
        if (!this.narrDirty) this.narr = v.narration || "";
        const f = {};   // 没点“保存数据”的录入不被拍照/归类等操作冲掉
        v.template.items.forEach((it) => {
          const x = v.fields[it.key];
          f[it.key] = this.dirty[it.key] && this.form[it.key] ? this.form[it.key] : { value: x && x.value != null ? String(x.value) : "", design: x ? x.design || "" : "", src: x ? x.src : "" };
        });
        this.form = f;
        this.res.date = this.res.date || today();
      },
      async load() { try { this.apply(await GET("/api/conceal/" + this.id)); } catch (e) { this.err = e.message; } },
      async run(fn, key = true) {
        this.busy = key;
        try { return await fn(); } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async saveDate() { await this.run(async () => this.apply(await PUT("/api/conceal/" + this.id, { inspection_date: this.date }))); },
      async shoot(shot) {
        const files = await takePhotos(); if (!files.length) return;
        await this.run(async () => {
          const fd = new FormData(); addPhotos(fd, files); fd.append("shot", shot || "");
          this.apply(await POST("/api/conceal/" + this.id + "/photos", fd)); toast("已加入 " + files.length + " 张");
        });
      },
      async pick(shot) {
        this.picker = { shot, loading: true, photos: [], unassigned: [], sel: [], note: "" };
        try {
          const r = await GET("/api/conceal/candidates?part=" + enc(this.v.part) + "&date=" + this.v.inspection_date + "&record_id=" + this.v.id);
          Object.assign(this.picker, r, { loading: false });
        } catch (e) { toast(e.message, true); this.picker = null; }
      },
      toggle(p) {
        if (p.in_record) return;
        const s = this.picker.sel, i = s.indexOf(p.media_id);
        if (i >= 0) s.splice(i, 1); else s.push(p.media_id);
      },
      async attach() {
        const { shot, sel } = this.picker;
        await this.run(async () => {
          const r = await POST("/api/conceal/" + this.id + "/photos/attach", { media_ids: sel, shot: shot || null });
          this.apply(r); this.picker = null;
          toast(r.skipped && r.skipped.length ? "已引用，另有 " + r.skipped.length + " 张不符合条件" : "已引用 " + sel.length + " 张");
        });
      },
      async unlink(p) { if (!confirm("移出这张照片？照片库保留原件。")) return; await this.run(async () => this.apply(await DEL("/api/conceal/" + this.id + "/photos/" + p.media_id))); },
      async setShot(p, shot) { if (!shot) return; await this.run(async () => this.apply(await PUT("/api/conceal/" + this.id + "/photos/" + p.media_id, { shot }))); },
      async acceptAi() { await this.run(async () => { const r = await POST("/api/conceal/" + this.id + "/photos/accept-ai", {}); this.apply(r); toast("已采用 " + r.accepted + " 张的归类建议"); }); },
      async analyze() {
        await this.run(async () => {
          const r = await POST("/api/conceal/" + this.id + "/analyze", { narration: this.narr }); this.narrDirty = false; this.apply(r);
          toast(this.v.ai.mock ? "已整理（离线模拟识别）" : "已整理，请核对后采用");
        }, "ai");
      },
      adopt(it, c) { this.form[it.key].value = String(c.value); this.form[it.key].src = "口述"; this.dirty[it.key] = true; toast("已填入，保存后生效"); },
      adoptRead(it, r) { this.form[it.key].value = String(r.value); this.form[it.key].src = "照片读数"; this.dirty[it.key] = true; toast("已填入，保存后生效"); },
      async saveFields() {
        const body = {};
        this.v.template.items.forEach((it) => {
          const f = this.form[it.key], had = !!this.v.fields[it.key];
          if (f.value !== "" || had) body[it.key] = { value: f.value, design: f.design, src: f.src || "人工" };
        });
        await this.run(async () => { const r = await PUT("/api/conceal/" + this.id + "/fields", { fields: body }); this.dirty = {}; this.apply(r); toast("已保存"); });
      },
      async ready() {
        if (!this.v.check.complete && !confirm("存在缺项，将按“带缺项说明”确认，是否继续？")) return;
        await this.run(async () => { this.apply(await POST("/api/conceal/" + this.id + "/ready", { note: this.readyNote })); toast("已确认"); });
      },
      async reopen() { await this.run(async () => { this.apply(await POST("/api/conceal/" + this.id + "/reopen")); toast("已退回补资料"); }); },
      async submit() { await this.run(async () => { this.apply(await POST("/api/conceal/" + this.id + "/submit", { supervisor_name: this.supervisor })); toast("已报验"); }); },
      async record() {
        await this.run(async () => {
          this.apply(await POST("/api/conceal/" + this.id + "/result", { result: this.res.result, note: this.res.note, date: this.res.date, supervisor_name: this.res.sup }));
          toast("已记录监理结果");
        });
      },
      async remove() { if (!confirm("删除该草稿？照片库保留原件。")) return; await this.run(async () => { await DEL("/api/conceal/" + this.id); toast("已删除"); go("conceal"); }); },
    },
  };

  // ---------------------------------------------------------------------- 验收模板（工程主管/项目经理可增删）
  const CcTplEditor = {
    props: ["tpl", "isNew"],
    emits: ["close", "saved"],
    template: `<div class="modal-bg" @click.self="$emit('close')"><div class="modal cc-modal"><h2>{{ isNew ? '新增验收模板' : '编辑：' + tpl.name }}</h2>
      <div class="banner info">模板仅列必拍照片与数据项；设计值、允许偏差在记录中填写。</div>
      <div class="field" v-if="isNew"><label class="f">标识（如 caisson）</label><input class="in" v-model.trim="key" maxlength="24"></div>
      <div class="field"><label class="f">名称</label><input class="in" v-model.trim="f.name" maxlength="30"></div>
      <div class="field"><label class="f">分类</label><input class="in" v-model.trim="f.group" maxlength="12" placeholder="如 钢筋、模板、桩基"></div>
      <div class="field"><label class="f">匹配关键词（逗号分隔）</label><input class="in" v-model="kw"></div>
      <h3 class="mt">必拍照片</h3>
      <div class="cc-erow" v-for="(s, i) in f.shots" :key="'s' + i"><input class="in" v-model.trim="s.label" placeholder="照片名称" maxlength="30"><input class="in" v-model.trim="s.hint" placeholder="说明（如 带尺）" maxlength="40">
        <label class="small"><input type="checkbox" v-model="s.required"> 必拍</label><a class="cc-del" @click="f.shots.splice(i, 1)">删</a></div>
      <button class="btn mt" @click="addShot"><ic n="plus"></ic>照片项</button>
      <h3 class="mt">数据项</h3>
      <div class="cc-erow" v-for="(it, i) in f.items" :key="'i' + i"><input class="in" v-model.trim="it.label" placeholder="数据项名称" maxlength="30">
        <select class="in cc-sel" v-model="it.type"><option value="number">数字</option><option value="text">文字</option></select>
        <input class="in cc-unit-in" v-model.trim="it.unit" placeholder="单位" maxlength="8" :disabled="it.type !== 'number'">
        <label class="small"><input type="checkbox" v-model="it.required"> 必填</label><a class="cc-del" @click="f.items.splice(i, 1)">删</a></div>
      <button class="btn mt" @click="addItem"><ic n="plus"></ic>数据项</button>
      <div class="row mt" style="justify-content:space-between"><div><button class="btn danger" v-if="!isNew" @click="del">{{ tpl.builtin ? '隐藏此模板' : '删除' }}</button>
          <button class="btn" v-if="!isNew && tpl.builtin && tpl.customized" @click="restore">恢复内置</button></div>
        <div class="row"><button class="btn" @click="$emit('close')">取消</button><button class="btn primary" :disabled="busy" @click="save">保存</button></div></div></div></div>`,
    data() {
      const t = this.tpl ? clone(this.tpl) : { name: "", group: "", keywords: [], shots: [], items: [] };
      return { f: t, key: this.tpl ? this.tpl.key : "", kw: (t.keywords || []).join("，"), busy: false };
    },
    methods: {
      nextKey(list, p) { let n = list.length + 1; while (list.some((x) => x.key === p + n)) n++; return p + n; },
      addShot() { this.f.shots.push({ key: this.nextKey(this.f.shots, "s"), label: "", hint: "", required: true }); },
      addItem() { this.f.items.push({ key: this.nextKey(this.f.items, "d"), label: "", type: "number", unit: "", required: true, measure: true, min: null, max: null, kw: [] }); },
      async save() {
        this.busy = true;
        try {
          const body = { ...this.f, keywords: this.kw.split(/[,，、\s]+/).filter(Boolean) };
          await PUT("/api/conceal/templates/" + this.key, body); toast("模板已保存"); this.$emit("saved");
        } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async del() {
        if (!confirm("确定？已有的验收记录按建档时的模板保留，不受影响。")) return;
        try { await DEL("/api/conceal/templates/" + this.key); this.$emit("saved"); } catch (e) { toast(e.message, true); }
      },
      async restore() { try { await DEL("/api/conceal/templates/" + this.key + "?restore=1"); this.$emit("saved"); } catch (e) { toast(e.message, true); } },
    },
  };

  // ---------------------------------------------------------------------- 列表页 / 缺资料扫描 / 模板
  const ConcealPage = {
    components: { CcRecord, CcTplEditor },
    template: `<div class="cc ra">
      <cc-record v-if="id" :id="id" :key="id"></cc-record>
      <template v-else>
        <page-head page="conceal"><template #meta>资料智能体归类照片、查缺项、预填记录，人确认后报验；只核对拍了没有、记了没有，不判定合格</template>
          <template #actions><div class="seg"><button :class="{on: tab === 'list'}" @click="setTab('list')">验收记录</button><button :class="{on: tab === 'gaps'}" @click="setTab('gaps')">缺资料扫描</button>
            <button :class="{on: tab === 'tpl'}" @click="setTab('tpl')">验收模板</button></div>
            <button class="btn primary" v-if="tab === 'list' && list && list.can_write && !creating" @click="startNew"><ic n="plus"></ic>新建验收</button>
            <button class="btn primary" v-if="tab === 'tpl' && tpls && tpls.can_edit" @click="editor = {isNew: true}"><ic n="plus"></ic>新增模板</button></template></page-head>
        <div v-if="err" class="banner warn">{{ err }}</div>

        <template v-if="tab === 'list' && list">
          <section class="zb-card mb" v-if="creating && opt"><div class="zb-ch"><ic n="plus"></ic><h2>新建验收</h2></div>
            <div class="ra-form">
            <div class="field"><label class="f">部位</label><select class="in" v-model="nf.part" @change="nf.activity = ''"><option value="">请选择</option>
              <option v-for="p in opt.parts" :key="p.name" :value="p.name">{{ p.name }}</option><option value="__other">其他</option></select>
              <input class="in mt" v-if="nf.part === '__other' || !opt.parts.length" v-model.trim="nf.custom" maxlength="40" placeholder="部位名称，如 3#墩 / 1#涵">
              <div class="tiny muted mt" v-if="isTech">不关联作业时可填写任意具体部位；关联作业须在您的负责范围内。</div></div>
            <div class="field" v-if="acts.length"><label class="f">对应 P6 作业（选填）</label><select class="in" v-model="nf.activity" @change="pickAct"><option value="">不指定</option>
              <option v-for="a in acts" :key="a.code" :value="a.code">{{ a.short_name || a.name }}（{{ a.status || '—' }}）</option></select></div>
            <div class="field"><label class="f">验收项目（模板）</label><select class="in" v-model="nf.template"><option value="">请选择</option>
              <option v-for="t in opt.templates" :key="t.key" :value="t.key">{{ t.name }}</option></select></div>
            <div class="field"><label class="f">验收日期</label><input type="date" class="in cc-date" v-model="nf.date"></div></div>
            <div class="tiny muted" v-if="opt.note">{{ opt.note }}</div>
            <div class="row mt"><button class="btn primary" :disabled="busy || !partName || !nf.template" @click="create">创建并拍照</button><button class="btn" @click="creating = false">取消</button></div></section>
          <div class="muted small mb" v-if="creating && !opt">加载中</div>
          <section class="zb-card" v-if="!list.records.length"><empty-state compact reason="还没有隐蔽验收记录。随做随拍，资料智能体会把照片归到模板的必拍项并找出缺拍。"></empty-state></section>
          <section v-for="g in groups.filter((x) => x.items.length)" :key="g.key" class="zb-card mb"><div class="zb-ch"><ic :n="g.icon"></ic><h2>{{ g.title }}</h2><span class="zb-n">{{ g.items.length }}</span><span class="grow"></span><span class="tiny muted">{{ g.hint }}</span></div>
            <div class="cc-rec" v-for="r in g.items" :key="r.id" @click="go('conceal', {id: r.id})">
              <div class="grow"><b>{{ r.title }}</b><div class="tiny muted">{{ r.part }} · {{ r.template_name }} · {{ r.inspection_date }} · {{ r.created_by }} · 照片 {{ r.photos }}</div>
                <div class="tiny" v-if="r.status === 'rejected' && r.result_note"><span class="chip red">退回</span> {{ r.result_note }}</div></div>
              <div class="cc-rec-r"><span :class="['chip', chip[r.status]]">{{ r.status_name }}</span>
                <span class="chip red" v-if="r.missing_shots">缺拍 {{ r.missing_shots }}</span><span class="chip orange" v-if="r.pending_items">待补 {{ r.pending_items }}</span>
                <span class="chip orange" v-if="r.forced">带缺项</span></div></div></section>
          <ai-note v-if="list.notice" :text="list.notice"></ai-note>
        </template>

        <template v-if="tab === 'gaps' && gaps">
          <div class="zb-kpis ra-kpis mb" v-if="gaps.has_plan">
            <div class="zb-kpi green"><span class="zb-kpi-ic"><ic n="check-circle"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">已齐</span><span class="zb-kpi-v">{{ gaps.summary.done }}</span></div></div>
            <div class="zb-kpi orange"><span class="zb-kpi-ic"><ic n="file"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">待补</span><span class="zb-kpi-v">{{ gaps.summary.pending }}</span><span class="zb-kpi-s">草稿或被退回</span></div></div>
            <div class="zb-kpi red"><span class="zb-kpi-ic"><ic n="alert"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">缺失</span><span class="zb-kpi-v">{{ gaps.summary.missing }}</span><span class="zb-kpi-s">无验收记录</span></div></div>
            <div class="zb-kpi"><span class="zb-kpi-ic"><ic n="database"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">应验收工序合计</span><span class="zb-kpi-v">{{ gaps.summary.total }}</span></div></div></div>
          <section class="zb-card mb"><div class="zb-ch"><ic n="alert"></ic><h2>疑似缺验收资料</h2><span class="zb-n">{{ gaps.gaps.length }}</span></div>
            <div v-if="!gaps.gaps.length" class="zb-empty">{{ gaps.has_plan ? '已开始的作业均有验收记录' : '暂无可扫描数据' }}</div>
            <div class="scroll-x" v-else><table class="t ra-tbl"><thead><tr><th>作业</th><th>部位</th><th>验收项目</th><th>依据</th><th></th></tr></thead><tbody>
              <tr v-for="g in gaps.gaps" :key="g.activity_code + g.template_key"><td data-l="作业"><act-name :c="g.activity_code" :n="g.activity_name" :s="g.activity_short"></act-name></td><td data-l="部位">{{ g.part }}</td><td data-l="验收项目">{{ g.template_name }}</td>
                <td data-l="依据" class="small"><span :class="['chip', g.state === 'pending' ? 'orange' : 'red']">{{ g.state_name }}</span><div class="tiny muted">{{ g.basis }}</div></td>
                <td class="nowrap ra-act"><button class="btn" v-if="g.draft_id" @click="go('conceal', {id: g.draft_id})">补资料</button>
                  <button class="btn primary" v-else-if="canWrite" :disabled="busy" @click="fromGap(g)">拍照建档</button></td></tr></tbody></table></div>
            <div class="zb-foot tiny muted" v-if="gaps.note">{{ gaps.note }}</div></section>
          <section class="zb-card" v-if="gaps.parts.length"><div class="zb-ch"><ic n="database"></ic><h2>按部位完成度</h2><span class="zb-n">{{ gaps.parts.length }}</span></div>
            <div class="scroll-x"><table class="t ra-tbl"><thead><tr><th>部位</th><th>已齐</th><th>待补</th><th>缺失</th><th></th></tr></thead><tbody>
              <tr v-for="p in gaps.parts" :key="p.part"><td data-l="部位"><b>{{ p.part }}</b></td><td data-l="已齐">{{ p.done }}</td><td data-l="待补"><span :class="p.pending ? 'chip orange' : ''">{{ p.pending }}</span></td>
                <td data-l="缺失"><span :class="p.missing ? 'chip red' : ''">{{ p.missing }}</span></td>
                <td class="nowrap ra-act"><a class="small" :href="'/api/conceal/docx?part=' + enc(p.part)" v-if="p.done"><ic n="download"></ic>导出成册</a></td></tr></tbody></table></div>
            <div class="zb-foot" v-if="gaps.groups.length"><span class="tiny muted">按分部：</span><span class="chip" v-for="g in gaps.groups" :key="g.group">{{ g.group }} 已齐 {{ g.done }} / 待补 {{ g.pending }} / 缺失 {{ g.missing }}</span></div></section>
        </template>

        <template v-if="tab === 'tpl' && tpls">
          <div class="ra-tpls">
          <section class="zb-card" v-for="t in tpls.templates" :key="t.key"><div class="zb-ch"><ic n="file"></ic><h2>{{ t.name }}</h2><span class="chip" v-if="t.group">{{ t.group }}</span><span class="chip blue" v-if="t.customized">本项目已调整</span><span class="chip" v-if="t.hidden">已隐藏</span>
              <span class="grow"></span><button class="btn sm" v-if="tpls.can_edit" @click="editor = {tpl: t}">编辑</button></div>
            <div class="tiny muted">匹配：{{ t.keywords.join('、') || '—' }}</div>
            <div class="small mt"><b>必拍</b>：<span v-for="s in t.shots" :key="s.key" class="cc-tag">{{ s.label }}<template v-if="!s.required">（可选）</template></span></div>
            <div class="small mt"><b>数据项</b>：<span v-for="i in t.items" :key="i.key" class="cc-tag">{{ i.label }}<template v-if="i.unit">（{{ i.unit }}）</template><template v-if="!i.required">·选填</template></span></div></section>
          <section class="zb-card" v-if="!tpls.templates.length"><div class="zb-empty">暂无验收模板</div></section></div>
          <ai-note :text="tpls.notice + ' 数据项：' + tpls.measure_note + (tpls.can_edit ? ' 可增删验收项目与必拍清单。' : '')"></ai-note>
          <cc-tpl-editor v-if="editor" :tpl="editor.tpl" :is-new="!!editor.isNew" :key="editor.tpl ? editor.tpl.key : 'new'" @close="editor = null" @saved="editor = null; load()"></cc-tpl-editor>
        </template>
        <div v-if="!err && ((tab === 'list' && !list) || (tab === 'gaps' && !gaps) || (tab === 'tpl' && !tpls))" class="empty">加载中</div>
      </template></div>`,
    data: () => ({ list: null, gaps: null, tpls: null, opt: null, err: "", creating: false, busy: false, editor: null, chip: ST_CHIP,
                   nf: { part: "", custom: "", activity: "", template: "", date: "" } }),
    computed: {
      id() { return store.route.params.id || ""; },
      tab() { return store.route.params.tab || "list"; },
      canWrite() { return !!this.list ? this.list.can_write : ["manager", "eng_sup", "tech"].includes(roleOf()); },
      groups() { return GROUPS.map((g) => ({ ...g, items: this.list.records.filter((r) => g.sts.includes(r.status)) })); },
      isTech() { return roleOf() === "tech"; },
      partName() { return this.nf.part === "__other" || !this.opt.parts.length ? this.nf.custom : this.nf.part; },
      acts() { const p = this.opt && this.opt.parts.find((x) => x.name === this.nf.part); return p ? p.activities : []; },
    },
    async mounted() { if (!this.id) { await this.load(); if (store.route.params.new && this.list && this.list.can_write) this.startNew(); } },
    watch: { tab() { this.load(); } },
    methods: {
      go, enc,
      setTab(t) { go("conceal", t === "list" ? {} : { tab: t }); },
      async load() {
        this.err = "";
        try {
          if (this.tab === "gaps") this.gaps = await GET("/api/conceal/gaps");
          else if (this.tab === "tpl") this.tpls = await GET("/api/conceal/templates");
          else this.list = await GET("/api/conceal");
        } catch (e) { this.err = e.message; }
      },
      async startNew() {
        this.creating = true; this.nf = { part: "", custom: "", activity: "", template: "", date: today() };
        if (!this.opt) { try { this.opt = await GET("/api/conceal/options"); } catch (e) { toast(e.message, true); this.creating = false; } }
      },
      pickAct() { const a = this.acts.find((x) => x.code === this.nf.activity); if (a && a.templates.length) this.nf.template = a.templates[0]; },
      async create() {
        this.busy = true;
        try {
          const r = await POST("/api/conceal", { part: this.partName, template_key: this.nf.template, activity_code: this.nf.activity || null, inspection_date: this.nf.date || null });
          go("conceal", { id: r.id });
        } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async fromGap(g) {
        this.busy = true;
        try {
          const r = await POST("/api/conceal", { part: g.part === UNASSIGNED ? "" : g.part, template_key: g.template_key, activity_code: g.activity_code });
          go("conceal", { id: r.id });
        } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 插槽小件
  // 缺验收资料是首页统一待办（/api/todo）的来源之一；页面入口只在导航“隐蔽验收”页签，施工日志页不再放第二入口

  // 部位档案页底部：该部位的隐蔽验收记录（只读）
  const CcPartSection = {
    template: `<section class="zb-card cc-card mt" v-if="rows.length"><div class="zb-ch"><ic n="file"></ic><h2>隐蔽验收记录</h2><span class="zb-n">{{ rows.length }}</span><span class="grow"></span>
        <a class="zb-link" v-if="okCount" :href="'/api/conceal/docx?part=' + enc(part)"><ic n="download"></ic>导出本部位成册</a></div>
      <div class="cc-rec" v-for="r in rows" :key="r.id" @click="go('conceal', {id: r.id})"><div class="grow"><b>{{ r.template_name }}</b><div class="tiny muted">{{ r.inspection_date }} · {{ r.created_by }} · 照片 {{ r.photos }}</div></div>
        <div class="cc-rec-r"><span :class="['chip', chip[r.status]]">{{ r.status_name }}</span><span class="chip red" v-if="r.missing_shots">缺拍 {{ r.missing_shots }}</span><span class="chip orange" v-if="r.pending_items">待补 {{ r.pending_items }}</span></div></div></section>`,
    data: () => ({ rows: [], chip: ST_CHIP }),
    computed: { part() { return store.route.params.part || ""; }, okCount() { return this.rows.filter((r) => ["ready", "submitted", "passed"].includes(r.status)).length; } },
    async mounted() {
      if (!this.part || !canRead() || !featOn("conceal")) return;
      try { this.rows = (await GET("/api/conceal?part=" + enc(this.part), { quiet: true })).records; } catch (_) { this.rows = []; }
    },
    methods: { go, enc },
  };

  EXT.route("conceal", ConcealPage);
  EXT.slot("bottom:sites", CcPartSection);
})();
