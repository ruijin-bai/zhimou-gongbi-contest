/* x_site：档案Agent。部位档案（sites：列表 + 详情时间线）与工期/费用索赔证据包（claims：事件 → 证据清单 → 待补充项）。
   部位档案对港湾岗位/领导看全项目，分包单位只看本单位；证据包只对项目经理、工程主管、安全主管开放，领导只读。 */
(function () {
  const WRITERS = ["manager", "eng_sup", "safety_sup"];
  const canWrite = () => !!store.me && WRITERS.includes(roleOf());
  const canClaims = () => !!store.me && (WRITERS.includes(roleOf()) || roleOf() === "leader");
  const TYPE_ICON = { photo: "📷", progress: "🏗️", qc: "🔍", hazard: "🦺", issue: "⚠️", note: "🎙️" };
  const LV = { high: "高", medium: "中", low: "低", info: "提示" };
  const LV_CHIP = { high: "red", medium: "orange", low: "yellow", info: "blue" };
  const FSTATUS = { open: "待整改", rectifying: "待复核", resolved: "已闭环", rejected: "复核未通过", dismissed: "已忽略" };
  const photoCap = (p) => (p.taken_at ? "拍摄 " + fmtDT(p.taken_at) : "拍摄时间未知 · 上传 " + fmtDT(p.uploaded_at));
  const geoText = (g) => (!g ? "未取得定位" : g.place || (g.lat != null ? g.lat.toFixed(5) + "," + g.lon.toFixed(5) : "未取得定位"));
  const qtyText = (list) => (list && list.length ? list.map((q) => fmtN(q.qty, 2) + (q.unit || "")).join("、") : "—");

  // ---------------------------------------------------------------------- 通用小件
  const SiteTabs = {
    props: ["cur"],
    template: `<div class="seg"><button :class="{on: cur === 'sites'}" @click="go('sites')"><ic n="database"></ic>部位档案</button>
      <button v-if="show" :class="{on: cur === 'claims'}" @click="go('claims')"><ic n="file"></ic>索赔证据包</button></div>`,
    computed: { show() { return canClaims(); } },
    methods: { go },
  };

  // 照片缩略图：点开大图；每张标注水印拍摄时间与定位，没有就如实写“未知/未取得”
  const XsPhotos = {
    props: ["photos", "limit"],
    template: `<div class="ev-grid xs-photos" v-if="photos && photos.length">
      <ev-photo v-for="p in shown" :key="p.id" :src="p.thumb" :full="p.url" size="f" :time="cap(p)"><br>{{ geo(p.geo) }}</ev-photo>
      <div class="tiny muted" v-if="photos.length > shown.length">另有 {{ photos.length - shown.length }} 张，见导出文件</div></div>`,
    computed: { shown() { return this.photos.slice(0, this.limit || 12); } },
    methods: { cap: photoCap, geo: geoText },
  };

  // ---------------------------------------------------------------------- 部位档案
  const SitesPage = {
    components: { SiteTabs, XsPhotos },
    template: `<div class="xs ra">
      <page-head page="sites" :title="part || undefined" :purpose="part ? '' : undefined">
        <template #meta><template v-if="part"><a @click="go('sites')">← 全部部位</a><template v-if="d"> · 范围：{{ d.scope }} · 日志与工程量只计已确认日志</template></template>
          <template v-else-if="list">范围：{{ list.scope }} · {{ list.note }}</template></template>
        <template #actions><template v-if="part && d"><a v-if="canExport" class="btn" :href="'/api/sites/docx?part=' + enc(part)"><ic n="download"></ic>导出 Word</a>
            <button v-if="d.can_write" class="btn primary" :disabled="busy" @click="summarize">{{ busy ? '生成中' : (d.summary ? '重写概况' : '生成概况') }}</button></template>
          <site-tabs v-else cur="sites"></site-tabs></template></page-head>
      <div v-if="err" class="banner warn">{{ err }}</div>

      <template v-if="!part && list">
        <div class="xs-bar"><input class="in xs-q" v-model.trim="q" placeholder="搜索部位，如 3#墩">
          <button v-if="list.unresolved && list.can_write" class="btn" :disabled="busy" @click="organize">{{ busy ? '归并中' : '归并未识别部位（' + list.unresolved + '）' }}</button>
          <span v-else-if="list.unresolved" class="tiny muted">{{ list.unresolved }} 处部位未识别，已归入“未归部位”</span>
          <span class="grow"></span><span class="tiny muted">共 {{ list.parts.length }} 个部位 · 部位按 P6 作业 WBS 与墩台编号识别</span></div>
        <zb-paged v-if="shownParts.length" :items="shownParts" :size="12" :m-size="5" :fit="200" unit="个部位" cls="xs-grid"><template #default="{ item: p }">
          <section class="zb-card xs-card" tabindex="0" @click="go('sites', {part: p.name})" @keyup.enter="go('sites', {part: p.name})">
            <div class="zb-ch"><ic n="pin"></ic><h2>{{ p.name }}</h2><span class="grow"></span><span class="tiny muted">{{ p.last_update ? '更新 ' + p.last_update.slice(5) : '暂无记录' }}</span></div>
            <div class="row wrap"><span class="chip blue"><ic n="camera"></ic>照片 {{ p.photos }}</span>
              <span :class="['chip', p.hazards_open ? 'red' : (p.hazards ? 'green' : '')]"><ic n="helmet"></ic>隐患 {{ p.hazards }}<template v-if="p.hazards_open"> · 未闭环 {{ p.hazards_open }}</template></span>
              <span class="chip" v-if="p.qc_total"><ic n="search"></ic>质控 {{ p.qc_done }}/{{ p.qc_total }}</span>
              <span class="chip" v-if="p.issues"><ic n="alert"></ic>问题 {{ p.issues }}</span></div>
            <div class="zb-foot tiny muted">日志 {{ p.log_days }} 天<template v-if="p.activity_count"> · P6 作业 {{ p.activity_count }} 项</template></div></section></template></zb-paged>
        <section v-else class="zb-card xs-none"><div class="zb-empty">{{ q ? '没有匹配“' + q + '”的部位' : '暂无部位档案。日志、照片、隐患挂接到部位后自动成档。' }}</div></section>
      </template>

      <template v-if="part && d">
        <div class="zb-kpis ra-kpis mb">
          <div class="zb-kpi"><span class="zb-kpi-ic"><ic n="camera"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">照片</span><span class="zb-kpi-v">{{ s.photos }}</span></div></div>
          <div :class="['zb-kpi', s.hazards_open ? 'red' : '']"><span class="zb-kpi-ic"><ic n="helmet"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">隐患 未闭环 / 共</span><span class="zb-kpi-v">{{ s.hazards_open }}<small> / {{ s.hazards }}</small></span></div></div>
          <div class="zb-kpi"><span class="zb-kpi-ic"><ic n="search"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">质控提醒 已完成 / 共</span><span class="zb-kpi-v">{{ s.qc_done }}<small> / {{ s.qc_total }}</small></span></div></div>
          <div class="zb-kpi"><span class="zb-kpi-ic"><ic n="calendar"></ic></span><div class="zb-kpi-b"><span class="zb-kpi-l">日志天数</span><span class="zb-kpi-v">{{ s.log_days }}</span><span class="zb-kpi-s">只计已确认日志</span></div></div></div>

        <section class="zb-card mb" v-if="d.can_write || d.summary"><div class="zb-ch"><ic n="database"></ic><h2>部位概况</h2><span class="grow"></span>
          <span :class="['zb-src', d.summary.source === 'ai' ? 'ai' : '']" v-if="d.summary">{{ d.summary.source === 'ai' ? 'AI 按统计起草' : '系统按统计生成' }} · {{ fmtDT(d.summary.at) }}</span></div>
          <zb-clamp v-if="d.summary" :text="d.summary.text" :lines="3" pre title="部位概况"></zb-clamp><div v-else class="zb-empty">暂无概况，可点页头“生成概况”；数字仅取自下方统计。</div>
          <div class="tiny muted mt" v-if="d.summary && d.summary.stale"><span class="chip orange">可能过期</span> 档案已更新，概况未重写。</div>
          <div class="zb-foot tiny muted">概况数字须出自统计，未通过校验时改用规则文字。</div></section>

        <section class="zb-card mb" v-if="s.activities.length"><div class="zb-ch"><ic n="layers"></ic><h2>作业与工程量</h2><span class="zb-n">{{ s.activities.length }}</span><span class="grow"></span><span class="zb-src">日志工程量 · 已确认日志</span></div>
          <div class="scroll-x"><table class="t ra-tbl"><thead><tr><th>P6 作业</th><th>计划</th><th>日志工程量合计</th><th>日志累计%</th></tr></thead>
          <tbody><tr v-for="a in s.activities" :key="a.code"><td data-l="P6 作业"><act-name :c="a.code" :n="a.name" :s="a.short_name"></act-name> <span class="chip red" v-if="a.critical">关键</span></td>
            <td data-l="计划" class="nowrap small">{{ a.plan_start || '—' }} ~ {{ a.plan_finish || '—' }}</td><td data-l="日志工程量">{{ qty(a.qty) }}</td>
            <td data-l="日志累计%">{{ a.log_pct == null ? '—' : fmtN(a.log_pct) + '%（' + a.log_pct_date.slice(5) + '）' }}</td></tr></tbody></table></div></section>

        <section class="zb-card"><div class="zb-ch"><ic n="clock"></ic><h2>时间线</h2><span class="grow"></span>
          <div class="seg xs-seg"><button :class="{on: !type}" @click="setType('')">全部</button>
            <template v-for="(n, k) in d.types" :key="k"><button v-if="d.type_counts[k]" :class="{on: type === k}" @click="setType(k)"><ic :n="icon[k]"></ic>{{ n }} {{ d.type_counts[k] }}</button></template></div></div>
          <div v-if="!d.items.length" class="zb-empty">{{ type ? '该类型暂无记录' : '该部位暂无记录' }}</div>
          <template v-for="g in groups" :key="g.date"><div class="xs-day">{{ g.date }}</div>
            <div v-for="(it, i) in g.items" :key="g.date + i" :class="['xs-item', 't-' + it.type]">
              <div class="xs-ico"><ic :n="icon[it.type]"></ic></div>
              <div class="grow">
                <div class="row wrap"><b>{{ it.title }}</b>
                  <span v-if="it.type === 'hazard'" :class="['chip', lvChip[it.level]]">{{ lv[it.level] }}级</span>
                  <span v-if="it.type === 'hazard'" :class="['chip', it.open ? 'orange' : (it.status === 'resolved' ? 'green' : '')]">{{ fstatus[it.status] }}</span>
                  <span v-if="it.type === 'qc'" :class="['chip', it.done ? 'green' : 'orange']">{{ it.done ? '已完成' : '待完成' }}</span>
                  <span v-if="it.type === 'photo' && it.shared" class="chip">同日志多部位</span>
                  <span class="tiny muted">{{ it.time }} {{ it.author }}<template v-if="it.weather"> · {{ it.weather }}</template></span></div>
                <template v-if="it.type === 'progress'">
                  <div class="small" v-for="(w, j) in it.works" :key="'w' + j">{{ w.desc || w.process }}</div>
                  <div class="small xs-prog" v-for="(p, j) in it.progress" :key="'p' + j"><act-name :c="p.code" :n="p.name" :s="p.short_name"></act-name>：完成 {{ p.qty == null ? '未记录' : fmtN(p.qty, 2) + (p.unit || '') }}<template v-if="p.pct != null">，累计 {{ fmtN(p.pct) }}%</template></div>
                  <a class="tiny" v-if="harbour" @click="go('log', {id: it.log_id})">查看日志</a></template>
                <template v-else-if="it.type === 'issue'"><div class="small">{{ it.text }}</div><div class="tiny muted" v-if="it.measure">措施：{{ it.measure }}</div>
                  <a class="tiny" v-if="harbour" @click="go('log', {id: it.log_id})">查看日志</a></template>
                <template v-else-if="it.type === 'hazard'"><div class="tiny muted">{{ it.agent === 'safety' ? '安全' : '质量' }}<template v-if="it.due_date"> · 整改期限 {{ it.due_date }}</template><template v-if="it.resolved_at"> · 闭环 {{ fmtDT(it.resolved_at) }}</template></div>
                  <xs-photos :photos="it.photos"></xs-photos><a class="tiny" @click="go('finding', {id: it.finding_id})">查看整改详情</a></template>
                <template v-else-if="it.type === 'qc'"><ul class="xs-pts"><li v-for="(p, j) in it.points" :key="j">{{ p.stage }}：{{ p.item }}<span class="muted" v-if="p.record">（{{ p.record }}）</span></li></ul></template>
                <template v-else-if="it.type === 'note'"><div class="small pre">{{ it.text }}</div><xs-photos :photos="it.photos"></xs-photos></template>
                <template v-else><xs-photos :photos="it.photos"></xs-photos><div class="tiny muted" v-if="it.time_src === 'uploaded'">无拍摄时间，按上传时间排序</div></template>
              </div></div></template>
          <div class="tiny muted" v-if="d.truncated">仅显示最近 400 条，完整记录见导出文件。</div></section>
      </template>
      <div v-if="!list && !d && !err" class="empty">加载中</div></div>`,
    data: () => ({ list: null, d: null, err: "", q: "", type: "", busy: false, icon: TYPE_ICON, lv: LV, lvChip: LV_CHIP, fstatus: FSTATUS }),
    computed: {
      part() { return store.route.params.part || ""; },
      s() { return this.d.stats; },
      canExport() { return true; },
      harbour() { return !isSubOrg(); },
      shownParts() { const q = this.q.toLowerCase(); return this.list.parts.filter((p) => !q || p.name.toLowerCase().includes(q)); },
      groups() {
        const out = [];
        for (const it of this.d.items) { const g = out[out.length - 1]; if (g && g.date === it.date) g.items.push(it); else out.push({ date: it.date, items: [it] }); }
        return out;
      },
    },
    mounted() { this.load(); },
    methods: {
      go, fmtDT, fmtN, enc: encodeURIComponent, qty: qtyText,
      async load() {
        this.err = "";
        try {
          if (this.part) this.d = await GET("/api/sites/detail?part=" + encodeURIComponent(this.part) + (this.type ? "&types=" + this.type : ""));
          else this.list = await GET("/api/sites");
        } catch (e) { this.err = e.message; }
      },
      setType(t) { this.type = t; this.load(); },
      async summarize() {
        this.busy = true;
        try { const r = await POST("/api/sites/summary", { part: this.part }); toast(r.source === "ai" ? "概况已生成" : "模型输出未通过数字校验，已改用规则概况"); await this.load(); }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async organize() {
        this.busy = true;
        try { const r = await POST("/api/sites/organize"); toast(r.merged ? "已归并 " + r.merged + " 处" : "暂无可归并项"); await this.load(); }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 证据展示（预览与证据包共用）
  const ClaimEvidence = {
    props: ["e", "nar"],
    components: { XsPhotos },
    template: `<div class="xs-ev">
      <div v-if="nar && nar.statement" class="xs-state"><b>事件说明</b><div class="pre">{{ nar.statement }}</div></div>
      <div class="xs-sec"><b>受影响作业与工期</b>
        <div class="small" v-if="!e.impact.activities.length">受影响作业：<span class="chip orange">待补</span> 日志问题未关联 P6 作业</div>
        <div class="scroll-x" v-else><table class="t ra-tbl"><thead><tr><th>作业</th><th>P6 计划</th><th>总浮时</th><th>关键</th><th>事件日工程量</th><th>估算损失</th></tr></thead><tbody>
          <tr v-for="a in e.impact.activities" :key="a.code"><td data-l="作业"><act-name :c="a.code" :n="a.name" :s="a.short_name"></act-name></td><td data-l="P6 计划" class="nowrap small">{{ a.plan_start || '—' }}~{{ a.plan_finish || '—' }}</td>
            <td data-l="总浮时">{{ a.float_d == null ? '未知' : fmtN(a.float_d) + ' 天' }}</td><td data-l="关键">{{ a.critical ? '是' : (a.critical === false ? '否' : '未知') }}</td>
            <td data-l="事件日工程量" class="small">{{ a.event_qty.length ? a.event_qty.map(q => q.date.slice(5) + ' ' + fmtN(q.qty, 2) + a.unit).join('、') : '无记录' }}</td>
            <td data-l="估算损失"><b v-if="a.loss_days != null">{{ fmtN(a.loss_days) }} 天</b><span v-else class="chip orange">待补</span></td></tr></tbody></table></div>
        <div class="small mt">受影响工期合计：<b v-if="e.impact.est_loss_days != null">{{ fmtN(e.impact.est_loss_days) }} 天（估算，待核实）</b><span v-else class="chip orange">待补</span>
          <span v-if="e.impact.halt_dates.length"> · 日志含停工字样：{{ e.impact.halt_dates.join('、') }}</span></div>
        <div class="tiny muted">{{ e.impact.basis }}。</div></div>
      <div class="xs-sec" v-if="nar && nar.evidence && nar.evidence.length"><b>证据清单</b><ul><li v-for="(t, i) in nar.evidence" :key="i" class="small">{{ t }}</li></ul></div>
      <div class="xs-sec"><b>当日日志原文</b>
        <div v-for="l in e.logs" :key="l.id" class="xs-log"><div class="tiny muted">L{{ l.id }} · {{ l.date }} · {{ l.author }} · 天气：{{ l.weather || '日志未记录' }}<span v-if="!l.confirmed" class="chip orange">未周确认</span></div>
          <div class="small pre">{{ l.voice_text || l.summary || '（无口述原文）' }}</div>
          <div class="small" v-for="(i, j) in l.issues" :key="j">• {{ i.type }}/{{ i.severity }}：{{ i.desc }}<span class="muted"> · 措施：{{ i.measure || '未填' }}</span></div></div></div>
      <div class="xs-sec"><b>当日人机投入（费用依据）</b><div class="scroll-x"><table class="t ra-tbl"><thead><tr><th>日期</th><th>人员</th><th>机械</th></tr></thead><tbody>
        <tr v-for="x in e.labor_machinery" :key="x.log_id"><td data-l="日期" class="nowrap">{{ x.date }}</td>
          <td data-l="人员" class="small">{{ x.labor.length ? x.labor.map(p => p.trade + (p.count != null ? ' ' + fmtN(p.count) + '人' : '')).join('、') : '未记录' }}</td>
          <td data-l="机械" class="small">{{ x.machinery.length ? x.machinery.map(p => p.name + (p.count != null ? ' ' + fmtN(p.count) + '台' : '') + '（' + p.status + '）').join('、') : '未记录' }}</td></tr></tbody></table></div></div>
      <div class="xs-sec"><b>相关照片</b> <span class="tiny muted">水印拍摄时间与定位取自照片入库记录</span>
        <xs-photos v-if="e.photos.length" :photos="e.photos"></xs-photos><div v-else class="small"><span class="chip orange">待补</span> 事件日无挂接照片</div></div>
      <div class="xs-sec" v-if="e.findings.length"><b>相关指令 / 整改</b><div class="small" v-for="f in e.findings" :key="f.id">• F{{ f.id }} {{ f.title }}（{{ fstatus[f.status] }}，{{ f.created }}）</div></div>
      <div class="xs-sec xs-gaps"><b>待补充项</b><ul><li v-for="(g, i) in allGaps" :key="i" class="small"><ic n="square"></ic>{{ g.item }}<span class="muted">（{{ g.why }}）</span></li></ul></div></div>`,
    data: () => ({ fstatus: FSTATUS }),
    computed: { allGaps() { return this.e.gaps.concat(((this.nar && this.nar.gaps) || []).map((g) => ({ item: g, why: "智能体建议补充" }))); } },
    methods: { fmtN },
  };

  // ---------------------------------------------------------------------- 索赔证据包
  const ClaimsPage = {
    components: { SiteTabs, ClaimEvidence },
    template: `<div class="xs ra">
      <page-head page="claims" :title="pack ? pack.title : undefined" :purpose="id ? '' : undefined">
        <template #meta><template v-if="id"><a @click="go('claims')">← 全部证据包</a> · </template>{{ notice }}</template>
        <template #actions><a v-if="pack && writer" class="btn primary" :href="'/api/claims/packs/' + pack.id + '/docx'"><ic n="download"></ic>导出 Word</a><site-tabs v-if="!id" cur="claims"></site-tabs></template></page-head>
      <div v-if="err" class="banner warn">{{ err }}</div>

      <template v-if="pack">
        <div class="tiny muted mb">{{ pack.created_by }} 生成于 {{ fmtDT(pack.created_at) }} · <span :class="['zb-src', pack.narrative.source === 'ai' ? 'ai' : '']">说明：{{ pack.narrative.source === 'ai' ? 'AI 整理' : '系统按规则整理' }}</span></div>
        <div class="banner info" v-if="pack.narrative.note">{{ pack.narrative.note }}</div>
        <section class="zb-card mb"><div class="zb-ch"><ic n="file"></ic><h2>总说明</h2></div><zb-clamp :text="pack.narrative.overview" :lines="3" pre title="总说明"></zb-clamp></section>
        <section class="zb-card mb" v-for="(e, i) in pack.events" :key="e.key"><div class="zb-ch"><ic n="flag"></ic><h2>事件{{ i + 1 }}：{{ e.cat_name }}</h2><span class="tiny muted">{{ e.company }} · {{ e.date_from }} ~ {{ e.date_to }}</span></div>
          <claim-evidence :e="e" :nar="pack.narrative.events[i]"></claim-evidence></section>
      </template>

      <template v-else-if="!id">
        <section class="zb-card mb"><div class="zb-ch"><ic n="flag"></ic><h2>可汇集的事件</h2><span class="zb-n">{{ shown.length }}</span><span class="grow"></span>
          <div class="row wrap xs-filter"><label class="small">从 <input type="date" class="in xs-date" v-model="from" @change="load"></label>
            <label class="small">到 <input type="date" class="in xs-date" v-model="to" @change="load"></label>
            <select class="in xs-sel" v-model="cat"><option value="">全部类别</option><option v-for="(n, k) in cats" :key="k" :value="k">{{ n }}</option></select></div></div>
          <div class="tiny muted mb">事件取自施工日志的问题记录<help-tip>事件取自施工日志中的停工、缺料、设备故障、天气、变更/指令类问题；天气仅用日志记录。
            <template v-if="nt.note">通知时效：知悉日起 {{ nt.days }} 天内向监理发出索赔意向通知（{{ nt.clause }}，项目规则可由系统管理员调整），剩 7 天提醒工程主管，剩 3 天升级到项目经理；{{ nt.note }}</template></help-tip></div>
          <zb-paged :items="shown" :size="10" :m-size="4" :fit="200" tag="table" cls="t ra-tbl xs-ev-tbl" unit="个事件"><template #head><thead><tr><th v-if="writer"></th><th>类别</th><th>单位</th><th>时间</th><th>问题记录</th><th>受影响作业</th><th>通知时效</th><th></th></tr></thead></template>
            <template #default="{ item: e }">
              <tr class="click" @click="toggle(e)"><td v-if="writer" data-l="选入" @click.stop><input type="checkbox" class="xs-ck" :checked="sel.includes(e.key)" @change="pick(e.key)"></td>
                <td data-l="类别"><span class="chip blue">{{ e.cat_name }}</span><span class="chip red" v-if="e.halt">含停工</span></td><td data-l="单位" class="small">{{ e.company }}</td>
                <td data-l="时间" class="nowrap small">{{ e.date_from }}<template v-if="e.date_to !== e.date_from"> ~ {{ e.date_to.slice(5) }}</template><div class="tiny muted">记录 {{ e.log_days }} 天</div></td>
                <td data-l="问题记录" class="small">{{ e.descs[0] }}<div class="tiny muted" v-if="e.n_issues > 1">等 {{ e.n_issues }} 条</div></td>
                <td data-l="受影响作业" class="small">{{ e.activities.length ? e.activities.map(a => a.short_name || a.code).join('、') : '待补' }}</td>
                <td data-l="通知时效" class="nowrap"><template v-if="ns(e)"><span :class="['chip', ns(e).tone]">{{ ns(e).label }}</span><div class="tiny muted">期限 {{ ns(e).deadline }}</div></template></td>
                <td class="tiny ra-act"><span class="chip orange" v-if="e.unconfirmed">含未周确认日志</span> {{ open === e.key ? '收起' : '查看证据' }}
                  <div v-if="writer && ns(e) && ns(e).status === 'pending'"><a class="nc-draft" @click.stop="draft(e)">{{ ns(e).letter ? '查看通知函' : '起草通知函' }}</a></div></td></tr>
              <tr v-if="open === e.key" class="xs-exrow"><td :colspan="writer ? 8 : 7" class="xs-expand">
                <div class="nc-box" v-if="ns(e)"><b>通知时效</b><span>知悉日 {{ ns(e).aware_date }}<template v-if="!ns(e).aware_default">（已人工修改）</template> · 期限 {{ ns(e).deadline }} · {{ ns(e).status_name }}<template v-if="ns(e).reason">：{{ ns(e).reason }}</template></span>
                  <template v-if="writer"><a v-if="ns(e).status === 'pending'" @click="decide(e, 'sent')">登记已发出</a><a v-if="ns(e).status === 'pending'" @click="decide(e, 'waived')">不发（填原因）</a>
                    <a v-if="ns(e).status !== 'pending'" @click="decide(e, 'pending')">撤回决定</a><a @click="aware(e)">改知悉日</a></template>
                  <small>系统不对外发送通知函，发出由人完成，这里只登记。</small></div>
                <div v-if="!ev[e.key]" class="muted small">加载中</div><claim-evidence v-else :e="ev[e.key]"></claim-evidence></td></tr></template>
            <template #empty><div class="zb-empty">所选时段暂无索赔相关事件</div></template></zb-paged>
          <div class="zb-foot xs-make" v-if="writer"><input class="in xs-title" v-model.trim="title" maxlength="60" placeholder="证据包名称（选填）">
            <button class="btn primary" :disabled="!sel.length || busy" @click="make">{{ busy ? '整理中' : '生成证据包（' + sel.length + '）' }}</button>
            <span class="tiny muted">每份最多 8 个事件；生成与导出均记入审计。</span></div>
          <div class="zb-foot tiny muted" v-else>当前角色只读；生成与导出需项目经理、工程主管或安全主管权限。</div></section>
        <section class="zb-card"><div class="zb-ch"><ic n="file"></ic><h2>已生成的证据包</h2><span class="zb-n">{{ packs.length }}</span></div>
          <div v-if="!packs.length" class="zb-empty">暂无证据包。在上方勾选事件后生成。</div>
          <zb-paged v-else :items="packs" :size="8" :m-size="4" :fit="140" tag="table" cls="t ra-tbl" unit="份"><template #head><thead><tr><th>名称</th><th>事件数</th><th>生成人</th><th>生成时间</th><th>已导出</th></tr></thead></template>
          <template #default="{ item: p }"><tr class="click" @click="go('claims', {id: p.id})"><td data-l="名称"><b>{{ p.title }}</b></td><td data-l="事件数">{{ p.n_events }}</td><td data-l="生成人">{{ p.created_by }}</td><td data-l="生成时间" class="small muted">{{ fmtDT(p.created_at) }}</td><td data-l="已导出" class="small">{{ p.exported_at ? fmtDT(p.exported_at) : '—' }}</td></tr></template></zb-paged></section>
      </template>
      <div v-else-if="!err" class="empty">加载中</div></div>`,
    data: () => ({ nt: { notices: {} }, events: [], cats: {}, packs: [], pack: null, notice: "AI整理，待商务/项目经理核实后使用", err: "", from: "", to: "", cat: "", sel: [], open: "", ev: {}, title: "", busy: false, writer: false }),
    computed: {
      id() { return store.route.params.id || ""; },
      // 按通知时效排序：待处理在前、剩余天数少的（含已过期）最前；已发出 / 不发排后（工笔文书方案 2026-10-08）
      shown() {
        const rank = (e) => { const n = this.ns(e); return !n ? [2, 0] : n.status === "pending" ? [0, n.remain] : [1, 0]; };
        return this.events.filter((e) => !this.cat || e.cat === this.cat).slice().sort((a, b) => { const x = rank(a), y = rank(b); return x[0] - y[0] || x[1] - y[1]; });
      },
    },
    mounted() { this.writer = canWrite(); this.load(); },
    methods: {
      go, fmtDT,
      async load() {
        this.err = "";
        try {
          if (this.id) { this.pack = await GET("/api/claims/packs/" + this.id); this.notice = this.pack.notice; return; }
          const q = [this.from && "date_from=" + this.from, this.to && "date_to=" + this.to].filter(Boolean).join("&");
          const r = await GET("/api/claims/events" + (q ? "?" + q : ""));
          this.events = r.events; this.cats = r.cats; this.notice = r.notice; this.writer = r.can_write;
          this.sel = this.sel.filter((k) => r.events.some((e) => e.key === k));
          this.packs = await GET("/api/claims/packs");
          try { this.nt = await GET("/api/claims/notices"); } catch (_) { this.nt = { notices: {} }; }
        } catch (e) { this.err = e.message; }
      },
      ns(e) { return (this.nt.notices || {})[e.key] || null; },
      async draft(e) {
        const n = this.ns(e);
        if (n && n.letter) { go("reports", { kind: "notice_of_claim", doc: n.letter.id }); return; }
        try { toast("通知函起草中…"); const d = await POST("/api/gongbi/docs", { kind: "notice_of_claim", ref: e.key }); go("reports", { kind: "notice_of_claim", doc: d.id }); }
        catch (x) { toast(x.message, true); }
      },
      async decide(e, status) {
        let reason = "";
        if (status === "waived") { reason = (window.prompt("不发通知函的原因（必填，例如：商务判断属承包商自身原因）") || "").trim(); if (!reason) return; }
        if (status === "sent" && !window.confirm("登记为“已发出”？请确认通知函已由人发给监理（系统不对外发送）。")) return;
        const n = this.ns(e);
        try { await PUT("/api/claims/notices", { key: e.key, status, reason, letter_id: status === "sent" && n && n.letter && n.letter.status === "confirmed" ? n.letter.id : null }); toast("已登记"); this.nt = await GET("/api/claims/notices"); }
        catch (x) { toast(x.message, true); }
      },
      async aware(e) {
        const n = this.ns(e);
        const d = (window.prompt("知悉日（YYYY-MM-DD，不早于事件起始日；修改留痕）", n ? n.aware_date : e.date_from) || "").trim();
        if (!d) return;
        const note = (window.prompt("修改依据（选填，例如：监理书面确认日）") || "").trim();
        try { await PUT("/api/claims/notices", { key: e.key, aware_date: d, note }); toast("已修改，期限已重算"); this.nt = await GET("/api/claims/notices"); }
        catch (x) { toast(x.message, true); }
      },
      pick(k) { const i = this.sel.indexOf(k); if (i >= 0) this.sel.splice(i, 1); else if (this.sel.length < 8) this.sel.push(k); else toast("每份最多 8 个事件", true); },
      async toggle(e) {
        this.open = this.open === e.key ? "" : e.key;
        if (this.open && !this.ev[e.key]) { try { this.ev[e.key] = await GET("/api/claims/events/detail?key=" + encodeURIComponent(e.key)); } catch (x) { toast(x.message, true); } }
      },
      async make() {
        this.busy = true;
        try { const p = await POST("/api/claims/packs", { keys: this.sel, title: this.title }); toast("证据包已生成，待核实后使用"); go("claims", { id: p.id }); }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
    },
  };

  EXT.route("sites", SitesPage);
  EXT.route("claims", ClaimsPage);
  EXT.alias("claims", "sites");
})();
