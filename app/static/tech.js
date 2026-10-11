/* 技术员手机端：说一说 + 拍一拍 → AI 写好 → 点选回答 → 提交。尽量不打字、不选清单。 */

// 周几（MM-DD）：周确认、首页周条统一用这一种日期标签（不用 D1/D2）
function weekDayName(date) {
  const d = new Date(date + "T00:00:00");
  return "周" + "一二三四五六日"[(d.getDay() + 6) % 7];
}
function weekDayLabel(date) {
  return weekDayName(date) + "（" + String(date).slice(5) + "）";
}
// 某天有没有实际内容：有随手记、照片或日志摘要才算；只建了空草稿不算
function dayHasContent(d) {
  return !!d && ((d.notes || 0) > 0 || (d.photos || 0) > 0 || !!String(d.summary || "").trim());
}

// 规则提示：写明是哪一段第几条（与电脑端日志页一致）；同样的提示合并成一行，写明条数和位置（U-06）
function groupChecks(checks, names) {
  const groups = [], by = new Map();
  for (const c of checks || []) {
    const k = c.level + "|" + c.message;
    if (!by.has(k)) { const g = { level: c.level, message: c.message, refs: [] }; by.set(k, g); groups.push(g); }
    by.get(k).refs.push({ section: c.section, index: typeof c.index === "number" ? c.index : null });
  }
  return groups.map((g) => {
    const secs = new Map();
    for (const r of g.refs) if (names[r.section]) { if (!secs.has(r.section)) secs.set(r.section, []); if (r.index !== null) secs.get(r.section).push(r.index + 1); }
    const where = [...secs].map(([s, ns]) => names[s] + (ns.length ? ` 第${numRuns(ns)}条` : "")).join("；");
    return { ...g, n: g.refs.length, where, text: g.refs.length > 1 ? `${g.refs.length} 条：${g.message}` : g.message };
  });
}
// [1,2,3,4,7] → "1～4、7"：连续 3 条以上写成区间
function numRuns(ns) {
  const a = [...new Set(ns)].sort((x, y) => x - y), out = [];
  for (let i = 0; i < a.length;) {
    let j = i; while (j + 1 < a.length && a[j + 1] === a[j] + 1) j++;
    out.push(j - i >= 2 ? `${a[i]}～${a[j]}` : a.slice(i, j + 1).join("、")); i = j + 1;
  }
  return out.join("、");
}

// 迟到记录（D5、F-12）：落在本周的迟到补传已按记录时间归入当天，只标“迟到补传 N 天”；
// 所属周已确认的（迟到补传或确认后补记）不改动已确认的数量，列入下一次周确认，由技术员决定是否补入。
function lateSplit(list, start, end) {
  const inWeek = [], carry = [];
  for (const n of list || []) {
    if (n.note_date >= start && n.note_date <= end) { if (n.late_days >= 1 || n.week_confirmed) inWeek.push(n); }
    else if (n.note_date < start && n.week_confirmed) carry.push(n);
  }
  return { inWeek, carry };
}
function lateText(n) { return n.late_days >= 1 ? `迟到补传 ${n.late_days} 天` : "确认后补记"; }
// 照片拍完过了 10 分钟以上才传上来：标“补传”（拍摄时间以水印时间为准）
function lateUpload(m) {
  if (!m || !m.taken_at || !m.uploaded_at) return false;
  const a = Date.parse(m.taken_at), b = Date.parse(m.uploaded_at);
  return a > 0 && b - a > 10 * 60000;
}

// 整改复核的显示（4.4 契约，文案与 site_agents.verify_label 一致）：AI 结论只是建议，隐患闭环（人工确认，status=resolved）之前
// 一律写“AI 建议：……，待安全主管确认”，不出现肯定已经消除的说法；闭环后另起一行写“已闭环”。
// 旧记录没有 state：由旧键 resolved 推出（true、false、null 分别按“建议已经消除”“建议仍存在”“复核失败”处理）。
const VERIFY_STALE_MS = 10 * 60000;
const PHOTO_CHECK = { unknown: "拍摄时间未知", early: "拍摄时间早于派单，请核实", clock_skew: "手机时间异常" };
function verifyView(f, now) {
  const v = f && f.verify;
  if (!v) return null;
  let state = v.state, suggest = v.suggest;
  if (!state) { suggest = v.resolved === true ? "resolved" : v.resolved === false ? "not_resolved" : null; state = suggest ? "done" : "failed"; }
  const started = Date.parse(v.started_at || "") || 0;
  const stale = state === "running" && !!started && now - started > VERIFY_STALE_MS;
  const closed = f.status === "resolved";
  // D-4：整改已被人退回重改（或闭环后重新打开）：这是上一次提交的 AI 建议，标“已被人工否定”，不再写“待安全主管确认”，AI 理由不再展示
  if (f.status === "open") {
    const text = state === "done" && suggest === "resolved" ? "已退回重改；AI 原建议（隐患消除）已被人工否定"
      : state === "done" && suggest ? "已退回重改；AI 原建议：仍存在" + (v.remaining || "原隐患") : "已退回重改";
    return { text, closed: "", reason: "", marks: [], reverify: false, busy: false, back: true };
  }
  const text = state === "pending" || state === "running" ? (stale ? "AI 复核未完成，可人工复核" : "AI 复核中")
    : state === "failed" || !suggest ? "AI 复核失败，待人工复核"
    : suggest === "resolved" ? (closed ? "AI 建议：已消除" : "AI 建议：已消除，待安全主管确认")
    : "AI 建议：仍存在" + (v.remaining || "原隐患");
  const marks = PHOTO_CHECK[v.photo_check] ? [PHOTO_CHECK[v.photo_check] + (v.photo_check === "clock_skew" && v.photo_taken_at ? `（手机记的是 ${String(v.photo_taken_at).replace("T", " ").slice(5, 16)}）` : "")] : [];
  return { text, closed: closed ? "已闭环" + (f.closed_by_name ? `（${f.closed_by_name}确认）` : "") : "", reason: state === "done" || state === "failed" ? v.reason || "" : "",
           marks, reverify: !closed && f.status === "rectifying" && (state === "failed" || stale), busy: !closed && (state === "pending" || state === "running") && !stale };
}

// 整改单本机暂存（AC-1.12）：打开过的整改单留一份（最近 30 张），断网后在应用内再打开照常显示，并提示“没有网络，显示本机暂存内容”
const fcKey = () => "zb_fcache_" + (store.me ? store.me.user.id : 0);
function fcGet(id) { const m = lsGet(fcKey(), {}); return m && m[id] ? m[id].f : null; }
function fcPut(f) {
  const m = lsGet(fcKey(), {}) || {};
  m[f.id] = { at: Date.now(), f };
  lsSet(fcKey(), Object.fromEntries(Object.entries(m).sort((a, b) => b[1].at - a[1].at).slice(0, 30)));
}

const PHOTO_MISS_SVG = "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 30"><rect width="40" height="30" fill="#eef2f8"/><text x="20" y="19" font-size="8" text-anchor="middle" fill="#64789b">缺文件</text></svg>');
const TechHome = {
  components: { VoiceButton, TodoMobile },
  template: `<div v-if="d" class="zb-m tech-home">
  <section class="zb-hero zb-m-hero"><page-head page="tech" title="随手记"><template #meta>{{ store.me.user.name }} · {{ store.me.user.company || '本单位' }} · {{ store.me.user.wbs_scope || '全部工点' }}</template></page-head></section>
  <div class="banner info" v-if="offline && !store.offline">没有网络，显示本机暂存内容</div>
  <div class="zb-card zb-m-cap"><div class="zb-ch"><ic n="edit"></ic><h2>记录现场事实</h2></div>
    <div class="cap-hero"><voice-button :big="true" mode="note" @audio="saveAudio"></voice-button>
      <button type="button" class="cap-photo" :disabled="busy || upl" @click="savePhoto"><ic n="camera"></ic><span>{{ upl ? '上传中' : '拍一张' }}</span><small>{{ upl ? '传完再拍下一张' : '自动加水印' }}</small></button></div>
    <div class="row cap-text"><input id="todo-focus-note" class="in grow" v-model="text" placeholder="或打字，如 3号墩承台浇筑完成" @keyup.enter="saveText">
      <button class="btn primary" :disabled="!text.trim() || busy" @click="saveText">记</button></div></div>
  <div class="zb-kpis n3 zb-m-kpis">
    <button type="button" class="zb-kpi" @click="jump('m-notes')"><div class="zb-kpi-b"><span class="zb-kpi-l">今日记录</span><span class="zb-kpi-v">{{ notes.length }}</span></div></button>
    <button type="button" :class="['zb-kpi', todo && todo.total ? 'orange' : '']" @click="jump('m-todo')"><div class="zb-kpi-b"><span class="zb-kpi-l">待我处理</span><span class="zb-kpi-v">{{ todo ? todo.total : '—' }}</span></div></button>
    <button type="button" :class="['zb-kpi', todo && todo.overdue ? 'red' : '']" @click="jump('m-todo')"><div class="zb-kpi-b"><span class="zb-kpi-l">已超期</span><span class="zb-kpi-v">{{ todo ? todo.overdue || 0 : '—' }}</span></div></button></div>
  <div :class="['zb-card', {all: allNotes}]" id="m-notes"><div class="zb-ch"><ic n="clock"></ic><h2>我今天的记录</h2><span class="grow"></span><span class="zb-n" v-if="notes.length">{{ notes.length }}</span></div>
    <div v-if="notes.length"><zb-paged :items="shownNotes" :size="8" :m-size="6" unit="条" :fit="false"><template #default="{ item: n }"><div class="tnote"><div class="readrow row">
      <span class="tiny muted nowrap">{{ n.time }}</span><span class="grow small">{{ n.voice ? voiceLabel(n) : n.text || '（照片）' }}<span class="tiny muted" :title="geoText(n.geo)" v-if="n.geo"> <ic n="map-pin"></ic></span>
        <span class="tiny muted geo-none" v-else-if="!n.queued && !n.asr" @click.stop="wmGeoHelp()"> 未取得定位</span>
        <span class="chip orange" v-if="n.queued" title="信号恢复后自动上传">{{ n.voice ? '已记下，待上传' : '待上传' }}</span>
        <template v-else-if="n.asr"><span :class="['chip', n.asr === 'failed' ? 'red' : 'blue']" :title="n.err">{{ n.asr === 'failed' ? '转写失败' : '转写中' }}</span>
          <template v-if="n.asr === 'failed'"> <a class="small" @click="voiceRetry(n)">重试</a> · <a class="small" @click="voiceType(n)">改打字</a></template></template></span>
      <img v-for="u in n.thumbs || []" :key="u" :src="u" alt="" style="width:34px;height:34px;object-fit:cover;border-radius:4px" @error="imgMiss" @click="store.viewer = u">
      <img v-for="m in n.media" :key="m" :src="'/api/media/' + m + '/thumb'" alt="" style="width:34px;height:34px;object-fit:cover;border-radius:4px" @error="imgMiss" @click="store.viewer = '/api/media/' + m + '/file'">
      <button type="button" class="btn ghost sm nowrap" v-for="a in n.audio || []" :key="'a' + a" :title="'回放原声'" @click="playAudio(a)"><ic n="mic"></ic>{{ playing === a ? '停' : '原声' }}</button>
      <a class="tiny muted nowrap" v-if="canPick(n)" @click="pickOpen = pickOpen === n.id ? null : n.id">归类</a>
      <button class="btn ghost sm" v-if="n.asr !== 'pending' && n.asr !== 'running'" @click="del(n)"><ic n="x"></ic></button></div>
      <div class="tsug" v-if="pickOpen === n.id && canPick(n)"><div class="tiny muted">这条记录其实是：</div>
        <div class="row mt" style="gap:6px;flex-wrap:wrap"><button class="btn sm" v-for="k in PICK_KINDS" :key="k[0]" :disabled="sugBusy" @click="sugPick(n, k[0])">{{ k[1] }}</button>
          <button class="btn ghost sm" @click="pickOpen = null">取消</button></div></div>
      <div class="tsug" v-if="sugOf(n)">
        <div class="tsug-q"><ic n="sparkles"></ic><span>{{ sugOf(n).suggestion.question }}</span></div>
        <div class="tiny muted" v-if="sugOf(n).suggestion.reason">{{ sugOf(n).suggestion.reason }} · AI 只建议，点“是”才处理，原记录不变</div>
        <div class="row mt" v-if="sugOf(n).need" style="gap:6px;flex-wrap:wrap">
          <input class="in" style="width:120px" v-if="sugOf(n).need.part" v-model="fill.part" placeholder="部位，如 3#墩">
          <select class="in" style="width:auto" v-if="sugOf(n).need.template" v-model="fill.tpl"><option value="" disabled>验收类型</option><option v-for="t in sugOf(n).templates" :key="t.key" :value="t.key">{{ t.name }}</option></select>
          <button class="btn primary sm" :disabled="sugBusy" @click="sugYes(n)">确定</button><button class="btn sm" @click="sugCancel(n)">取消</button></div>
        <div class="row mt" v-else style="gap:6px">
          <button class="btn primary sm" :disabled="sugBusy" @click="sugYes(n)">{{ sugOf(n).suggestion.needs_photo ? '是，拍一张' : '是' }}</button>
          <button class="btn sm" :disabled="sugBusy" @click="sugNo(n)">不是</button></div></div>
      <div class="tsug done" v-else-if="doneOf(n)"><ic n="check"></ic><span>{{ doneOf(n).label }}</span>
        <a v-if="doneOf(n).route" @click="go(doneOf(n).route.name, doneOf(n).route.params)">查看</a></div>
      <div class="tsug fq" v-else-if="fqOf(n)">
        <div class="tsug-q"><ic n="sparkles"></ic><span>{{ fqOf(n).ask.question }}</span></div>
        <div class="row mt" style="gap:6px;flex-wrap:wrap" v-if="fqOf(n).ask.need_activity">
          <button type="button" v-for="a in fqOf(n).ask.candidates" :key="a.code" :class="['opt', {on: fqForm[n.id].code === a.code}]" @click="fqPick(n, a.code)">{{ a.name }}</button></div>
        <div class="row mt fq-row">
          <input class="in" v-if="fqOf(n).ask.need_qty" type="number" inputmode="decimal" min="0" style="width:84px" v-model="fqForm[n.id].qty" placeholder="多少">
          <select class="in" v-if="fqOf(n).ask.need_qty" style="width:auto" v-model="fqForm[n.id].unit"><option v-for="u in fqOf(n).ask.units" :key="u" :value="u">{{ u }}</option></select>
          <input class="in grow" v-else v-model="fqForm[n.id].text" maxlength="200" placeholder="或说一句">
          <button class="btn primary sm" :disabled="fqBusy" @click="fqYes(n)">补上</button>
          <button class="btn ghost sm" :disabled="fqBusy" @click="fqNo(n)">先不管</button></div>
        <div class="tiny muted mt">可以不答，周确认时再处理；补上的写回这条随手记</div></div>
      <div class="tsug done" v-else-if="fqDone(n)"><ic n="check"></ic><span>已补充{{ fqDone(n).supplement.replace(/^（补充/, '').replace(/）$/, '') }}</span></div></div></template></zb-paged><div class="th-more"><button type="button" class="zb-m-more th-ask" v-if="!allNotes && asks" @click="allNotes = true"><ic n="sparkles"></ic>AI 有 {{ asks }} 个问题等你确认</button><button type="button" class="zb-m-more" v-if="notes.length > shownNotes.length" @click="allNotes = true">其余 {{ notes.length - shownNotes.length }} 条</button><button type="button" class="zb-m-more" v-else-if="allNotes && (notes.length > 2 || asks)" @click="allNotes = false">收起</button></div></div>
    <div class="zb-empty" v-else>今天还没有随手记。上面说一句、拍一张就行，不用填任何结构。</div></div>
  <div id="m-todo" :class="['zb-m-todo', {all: allTodo}]"><div v-fit="200" class="zb-m-todo-b"><todo-mobile ref="todo" title="待我处理" @loaded="load" @vue:mounted="hookTodo"></todo-mobile></div>
    <button type="button" class="zb-m-more" v-if="todo && todo.n > 2" @click="allTodo = !allTodo">{{ allTodo ? '收起' : '查看全部 ' + todo.n + ' 项' }}</button></div>
  <div class="zb-card zb-m-rec" v-if="d.week">
    <div class="zb-m-row zb-m-go" @click="go('week')"><div class="grow"><div class="zb-m-t">本周进度确认 <span :class="['chip', STATUS_CHIP[d.week.status]]">{{ STATUS[d.week.status] }}</span></div>
      <div class="rec-days"><div v-for="w in d.week.days" :key="w.date"><i :class="{on: dayHasContent(w)}"></i><span>{{ weekDayName(w.date).slice(1) }}</span></div></div>
      <div class="zb-m-s">{{ d.week.start }} ~ {{ d.week.end }} · 每周确认一次，提交后更新进度计划</div></div><ic n="chevron"></ic></div>
    <div class="zb-m-row" v-if="d.daily_log"><div class="grow"><div class="zb-m-t">今日施工日志 <span class="chip" v-if="!d.day_log">待整理</span></div>
      <div class="zb-m-s"><template v-if="d.day_log && d.day_log.drafted_at">已按随手记整理，更新于 {{ fmtDT(d.day_log.drafted_at).slice(11) }} · </template>每晚 20:00 统一整理<template v-if="d.auto_organize === false">（自动整理当前未开启，可点“立即整理”）</template></div></div>
      <div class="zb-btns"><a v-if="d.day_log" @click="go('log', {id: d.day_log.id})">查看</a>
        <button class="btn sm" v-if="d.notes.length" :disabled="org" @click="organize">{{ org ? '整理中' : '立即整理' }}</button></div></div>
    <div class="zb-m-row"><div class="grow"><div class="zb-m-t">已用 Excel 写了日报</div><div class="zb-m-s">导入生产日报 .xlsx，逐行核对后生成当天日志草稿</div></div>
      <button class="btn sm" @click="go('xlsx')"><ic n="file"></ic>导入</button></div></div>
  <ai-note page="tech"></ai-note>
</div><empty-state v-else-if="err" :reason="err" :action="{ label: '重试', fn: retry }"></empty-state><div v-else class="empty">加载中</div>`,
  data: () => ({ d: null, text: "", busy: false, upl: false, org: false, t0: 0, sug: {}, sugBusy: false, pickOpen: null, PICK_KINDS: [["hazard", "安全隐患"], ["ticket", "材料/混凝土小票"], ["conceal", "隐蔽验收照片"]], fq: {}, fqForm: {}, fqBusy: false, fill: { part: "", tpl: "" }, offline: false, err: "", replaceVp: 0, playing: 0, todo: null, allNotes: false, allTodo: false,
    store, STATUS, STATUS_CHIP, LEVEL_F }),
  computed: {
    // 弱网暂存的随手记、录音（待上传）与服务器上转写中的录音排在前面，联网同步、转写完成后自然并入服务器记录
    notes() { return [...(window.QUICK ? window.QUICK.pendingNotes() : []), ...this.d.notes]; },
    // 首页只摊开最近 2 条和还在上传/转写的条目；AI 的追问只给条数，点开回答（一屏化 RJ 10-10：随手记 + 最要紧的待办在首屏）
    shownNotes() { const l = this.notes; if (this.allNotes || l.length <= 2) return l; return l.filter((n, i) => i >= l.length - 2 || n.queued || n.asr); },
    asks() { return this.notes.filter((n) => this.sugOf(n) || this.fqOf(n)).length; },
  },
  async mounted() {
    wmGeoOnce(8000); await this.load(); this.t0 = Date.now();  // 提前请求定位授权并预热，记录时直接带上位置
    this.onSynced = () => this.load();
    for (const ev of ["zb-quick-synced", "zb-voice-done", "online"]) window.addEventListener(ev, this.onSynced);  // 补传完、转写完、来网了：重新拉取
    if (window.QUICK) window.QUICK.loadVoice();
  },
  unmounted() { for (const ev of ["zb-quick-synced", "zb-voice-done", "online"]) window.removeEventListener(ev, this.onSynced); clearTimeout(this.sugTimer); clearTimeout(this.fqTimer); this.gone = true; if (this.au) this.au.pause(); },
  methods: {
    go, fmtDT, geoText, wmGeoHelp, dayHasContent, weekDayName,
    async load() {
      const ckey = "zb_home_cache_" + store.me.user.id;
      try { const d = await GET("/api/tech/home"); this.d = d; this.offline = false; this.err = ""; lsSet(ckey, d); }
      catch (e) {  // 信号差时用上次的首页数据撑着，随手记照常可用；本机也没有时给出原因和重试，不白屏（AC-1.12）
        if (!e.offline) { if (this.d) toast(e.message, true); else this.err = e.message; return; }
        const c = this.d || lsGet(ckey, null);
        // 本机没有首页内容：离线模式（凭上次登录离线进入）下只给随手记，日志/周确认要联网；应用内断网时仍提示联网后再打开
        if (!c && !store.offline) { this.err = "没有网络，显示本机暂存内容：这台手机上还没有首页内容，联网后再打开"; return; }
        if (!c) this.d = { today: today(), notes: [], day_log: null, daily_log: false, week: null };
        else this.d = c.today === today() ? c : { ...c, today: today(), notes: [], day_log: null };  // 隔天的暂存不把昨天的随手记当今天的
        this.offline = true;
      }
      this.loadSug();
    },
    retry() { this.err = ""; this.load(); },
    // 指标行的“待我处理/已超期”直接读下面待办组件已取到的数据，不再单独请求
    hookTodo() { if (this.unTodo) this.unTodo(); this.unTodo = this.$watch(() => this.$refs.todo && this.$refs.todo.d, (t) => { this.todo = t ? { total: t.total || 0, overdue: t.overdue || 0, n: todoEntries(t.items || []).length } : null; }, { immediate: true }); },
    // 照片文件缺失（如演示副本不含原图）：显示真实空态“缺文件”，不显示破图
    imgMiss(e) { const im = e.target; if (im.dataset.miss) return; im.dataset.miss = "1"; im.src = PHOTO_MISS_SVG; },
    jump(id) { const el = document.getElementById(id); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); },
    // 归类建议：AI 只建议。拿不准或普通进度记录不出建议；离线/失败时随手记照常
    async loadSug(tries = 0) {
      clearTimeout(this.sugTimer);
      const ids = this.d.notes.map((n) => n.id).filter((i) => Number.isInteger(i));
      if (!ids.length || this.gone) return;
      try {
        const r = await GET("/api/notes/classify?ids=" + ids.join(","), { quiet: true });
        this.sug = Object.fromEntries(r.items.map((i) => [i.note_id, { ...(this.sug[i.note_id] && this.sug[i.note_id].need ? { need: this.sug[i.note_id].need, templates: this.sug[i.note_id].templates } : {}), ...i }]));
        if (tries < 4 && r.items.some((i) => i.state === "pending")) this.sugTimer = setTimeout(() => this.loadSug(tries + 1), 4000);
      } catch (_) { /* 建议拿不到不影响记录 */ }
      this.loadFq();
    },
    // 追问一句：缺作业或数量时就地问一句；模型润色在后台，回来前不显示（超过 15 秒服务端改给规则问句）
    async loadFq(tries = 0) {
      clearTimeout(this.fqTimer);
      const ids = this.d.notes.map((n) => n.id).filter((i) => Number.isInteger(i));
      if (!ids.length || this.gone) return;
      try {
        const r = await GET("/api/notes/followup?ids=" + ids.join(","), { quiet: true });
        const form = { ...this.fqForm };
        for (const i of r.items) if (i.ask && !form[i.note_id]) form[i.note_id] = { code: "", qty: "", unit: i.ask.units[0] || "", text: "" };
        this.fqForm = form;
        this.fq = Object.fromEntries(r.items.map((i) => [i.note_id, i]));
        if (tries < 5 && r.items.some((i) => i.state === "pending")) this.fqTimer = setTimeout(() => this.loadFq(tries + 1), 4000);
      } catch (_) { /* 追问拿不到不影响记录 */ }
    },
    fqOf(n) { const s = this.fq[n.id]; return s && s.state === "asked" && s.ask && this.fqForm[n.id] ? s : null; },
    fqDone(n) { const s = this.fq[n.id]; return s && s.state === "answered" && s.answer ? s.answer : null; },
    fqPick(n, code) { const f = this.fqForm[n.id]; f.code = f.code === code ? "" : code; },
    async fqYes(n) {
      const f = this.fqForm[n.id]; if (!f || this.fqBusy) return;
      const body = { code: f.code, unit: f.unit, text: f.text.trim() };
      if (String(f.qty).trim() !== "") body.qty = Number(f.qty);
      if (!body.code && body.qty == null && !body.text) { toast("点一个作业、填个数量，或说一句", true); return; }
      this.fqBusy = true;
      try {
        const r = await POST(`/api/notes/${n.id}/followup/answer`, body);
        this.fq = { ...this.fq, [n.id]: { note_id: n.id, state: "answered", answer: r.answer } };
        const it = this.d.notes.find((x) => x.id === n.id); if (it) it.text = r.text;
        toast("已补上");
      } catch (e) { toast(e.message, true); } finally { this.fqBusy = false; }
    },
    async fqNo(n) {
      this.fqBusy = true;
      try { await POST(`/api/notes/${n.id}/followup/dismiss`); this.fq = { ...this.fq, [n.id]: { note_id: n.id, state: "dismissed" } }; }
      catch (e) { toast(e.message, true); } finally { this.fqBusy = false; }
    },
    sugOf(n) { const s = this.sug[n.id]; return s && s.state === "suggested" && s.suggestion ? s : null; },
    doneOf(n) { const s = this.sug[n.id]; return s && s.state === "accepted" && s.result ? s.result : null; },
    async sugYes(n) {
      const s = this.sugOf(n); if (!s || this.sugBusy) return;
      this.sugBusy = true;
      try {
        const body = {};
        if (s.suggestion.needs_photo) {  // 纯文字的隐患：拍一张照，走既有“随手拍隐患”识别（疑似隐患待安全员确认）
          const got = await takePhotosToUpload(); if (!got) return;
          const fd = new FormData(); fd.append("purpose", "safety"); addPhotos(fd, got.files, got.scan);
          const ms = await POST("/api/media", fd);
          if (ms.some((m) => m.queued)) { toast("无信号，照片已暂存；联网后请再点“是”"); return; }
          body.media_ids = ms.map((m) => m.id);
        }
        body.kind = s.suggestion.kind;
        if (s.need) { body.part = this.fill.part.trim(); body.template_key = this.fill.tpl || s.need.template_key || ""; }
        const r = await POST(`/api/notes/${n.id}/classify/accept`, body);
        if (r.need) {
          this.fill = { part: r.need.part_value || "", tpl: r.need.template_key || "" };
          this.sug = { ...this.sug, [n.id]: { ...s, need: r.need, templates: r.templates } };
          return;
        }
        this.sug = { ...this.sug, [n.id]: { note_id: n.id, state: "accepted", result: r.result } };
        toast(r.result.label);
      } catch (e) { toast(e.message, true); } finally { this.sugBusy = false; }
    },
    // 人工重新归类（含点过“不是”的）：选类别后走同一条“是”的流程，只生成草稿
    canPick(n) { const s = this.sug[n.id]; return Number.isInteger(n.id) && !n.queued && !n.asr && !this.sugOf(n) && !(s && s.state === "accepted") && !(s && s.state === "pending"); },
    async sugPick(n, kind) {
      try {
        const r = await POST(`/api/notes/${n.id}/classify`, { kind });
        this.pickOpen = null; this.sug = { ...this.sug, [n.id]: r };
        if (r.state === "suggested") await this.sugYes(n);
      } catch (e) { toast(e.message, true); }
    },
    sugCancel(n) { const s = this.sug[n.id]; if (s) this.sug = { ...this.sug, [n.id]: { ...s, need: null } }; },
    async sugNo(n) {
      this.sugBusy = true;
      try { await POST(`/api/notes/${n.id}/classify/dismiss`); this.sug = { ...this.sug, [n.id]: { note_id: n.id, state: "dismissed" } }; }
      catch (e) { toast(e.message, true); } finally { this.sugBusy = false; }
    },
    // 记上了（含转入本机暂存）返回 true；没记上返回 false，由调用方把内容还给人
    async add(body) {
      this.busy = true;
      try {
        if (body.lat == null) { const g = await wmGeoOnce(6000); if (g) Object.assign(body, { lat: g.lat, lon: g.lon, acc: Math.round(g.acc) }); }
        const r = await POST("/api/notes", body);
        if (r && r.queued) { toast("无信号，已暂存，联网后自动上传"); return true; }
        toast("已记录"); await this.load(); setTimeout(() => this.load(), 4000); setTimeout(() => this.loadSug().catch(() => {}), 9000);
        return true;
      } catch (e) { toast(e.message, true); return false; } finally { this.busy = false; this.t0 = Date.now(); }
    },
    // 说一句：录完立即存本机，马上算记下了；上传与转写在后台，列表里显示“已记下，待上传 / 转写中 / 转写失败”
    async saveAudio(a) {
      if (!window.QUICK) return;
      const g = wmGeoNow();
      try { const r = await window.QUICK.addVoice({ ...a, geo: g ? { lat: g.lat, lon: g.lon, acc: Math.round(g.acc) } : null }); toast(r.queued ? "已记下，待上传" : "已记下，转写中"); }
      catch (e) { toast(e.message, true); }
    },
    voiceLabel(n) { return "（语音" + (n.seconds ? " " + Math.round(n.seconds) + " 秒" : "") + "）"; },
    // 录音一律保留：随手记旁可回放原声（转写不准时以原声为准）
    playAudio(pid) {
      if (this.au) { this.au.pause(); this.au = null; }
      if (this.playing === pid) { this.playing = 0; return; }
      this.au = new Audio(`/api/quick/voice/${pid}/audio`); this.au.onended = () => { this.playing = 0; };
      this.au.play().then(() => { this.playing = pid; }).catch(() => toast("播放不了这段录音，请联网后再试", true));
    },
    async voiceRetry(n) { try { await window.QUICK.voiceRetry(n.pid); toast("已重新转写"); } catch (e) { toast(e.message, true); } },
    // 改打字：先打字记下，记上之后这条转写失败的录音自动删掉（不先删，免得什么都没留下）
    voiceType(n) { this.replaceVp = n.pid; const el = document.getElementById("todo-focus-note"); if (el) el.focus(); toast("打字记下后，这条录音会自动删掉"); },
    saveText() {
      const t = this.text.trim(); if (!t) return;
      const secs = Math.min(300, (Date.now() - this.t0) / 1000);
      this.text = "";
      this.add({ text: t, source: "text", seconds: secs }).then((ok) => {
        if (!ok) { if (!this.text) this.text = t; return; }  // 没记上：把字还回输入框
        if (this.replaceVp) { const id = this.replaceVp; this.replaceVp = 0; window.QUICK.voiceDiscard(id).catch(() => {}); }
      });
    },
    // 拍一张：可边拍边说（或在上传确认里补录）。语音只存本机、跟在这条照片记录后面补传，服务器后台转成文字补进这条记录
    async savePhoto() {
      if (this.upl) return;
      const got = await takePhotosToUpload({ voice: true }); if (!got) return;
      const { files, scan, voice } = got;
      const cid = window.QUICK ? window.QUICK.newCid("n") : "";
      let noteCid = "";
      const fb = photoFeedback.begin(files.length, scan);  // 拍完即刻回应；识别在后台，不挡着继续记
      const fd = new FormData(); addPhotos(fd, files, scan);
      this.upl = true;  // 上传完成前拍照按钮不可再点，按钮上写“上传中”；慢网超时自动转本机暂存
      try {
        const ms = await POST("/api/media", fd); fb.sent(ms); this.upl = false;
        if (await this.add({ text: this.text, media_ids: ms.map((m) => m.id), source: "photo", seconds: 8, ...(cid ? { client_id: cid } : {}) })) noteCid = cid;
        this.text = "";
      }
      catch (e) { fb.fail(e.message); toast(e.message, true); }
      finally {
        this.upl = false;
        if (voice) this.saveAudio({ ...voice, noteCid });  // 照片没记上也不丢录音：单独成一条语音随手记
      }
    },
    async del(n) {
      if (!confirm(n.queued ? "删除这条还没上传的记录？删除后不能恢复。" : n.asr ? "删除这条录音？删除后不能恢复。" : "删除这条随手记？删除后不能恢复，当天日志草稿会按剩下的记录重新整理。")) return;
      if (n.queued) { await window.QUICK.discard(n.id); return; }  // 还没上传的，直接从本机暂存里去掉
      if (n.asr) { try { await window.QUICK.voiceDiscard(n.pid); } catch (e) { toast(e.message, true); } return; }
      try {
        const r = await DEL(`/api/notes/${n.id}`);
        this.d.notes = this.d.notes.filter((x) => x.id !== n.id);
        if (r && r.hint) toast(r.hint);  // 日志草稿有人工修改：没有自动清空，提示自己核对
      } catch (e) { toast(e.message, true); }
    },
    async organize() {
      // 已确认的日子不再重新整理（F-12）：之后记的内容不改动已确认的数量，列入下一次周确认的“迟到记录”，由技术员决定是否补入
      const done = (st) => ["submitted", "approved"].includes(st);
      if (done(this.d.week.status) || (this.d.day_log && done(this.d.day_log.status))) { toast("这天的记录已确认，之后记的内容不改动已确认的数量，会列入下次周确认的“迟到记录”"); return; }
      this.org = true; try { const l = await POST("/api/logs/organize", {}); go("log", { id: l.id }); } catch (e) { toast(e.message, true); } finally { this.org = false; }
    },
  },
};

// 日志采集：语音优先
const TechLog = {
  components: { VoiceButton, SrcChips, ReviewTable },
  template: `<div v-if="log" class="rd-fx"><page-head :page="isWeek ? 'week' : 'daylog'" :title="isWeek ? undefined : log.log_date + ' 施工日志'" :steps="isWeek ? 'week' : ''" :current="step">
    <template #badge><span :class="['chip', STATUS_CHIP[log.status]]">{{ STATUS[log.status] }}</span></template>
    <template #meta><template v-if="isWeek">{{ log.log_date }} ~ {{ log.period_end }}</template></template></page-head>
  <div class="banner info" v-if="!isWeek && log.auto && weeklyMode">由随手记自动整理，随本周确认一并提交。<a v-if="log.week_log_id" @click="go('log', {id: log.week_log_id})">去本周确认 →</a></div>
  <div class="banner warn" v-if="log.status === 'returned'">审核退回：{{ log.review_note }}</div>
  <div class="banner warn" v-if="!isWeek && log.pending_notes">你改过这份草稿，之后又记了 {{ log.pending_notes }} 条随手记，还没并入。<a @click="reorganize">重新整理（会覆盖你的修改）</a></div>
  <log-progress-trust :log="log"></log-progress-trust>
  <div class="banner warn" v-if="store.me.ai.provider === 'mock'">离线模拟模型：草稿不代表真实识别能力</div>
  <div class="card" v-if="isWeek"><h2>本周记录</h2>
    <div class="wk-days"><div v-for="d in log.days" :key="'t' + d.date" :class="['wk-day', {on: dayHasContent(d), today: d.date === today()}]"><b>{{ weekDayLabel(d.date).slice(0, 2) }}</b><span>{{ d.date.slice(8) }}</span><i>{{ dayHasContent(d) ? '有记录' : '无记录' }}</i></div></div>
    <div class="wk-empty" v-if="editable && weekEmpty"><span class="wk-empty-i"><ic n="mic"></ic></span><b>本周还没有记录</b><div class="tiny muted">本周还没有记录，先去随手记说一句，AI 会把每天的记录整理进周确认</div>
      <button class="btn primary" type="button" @click="go('home')"><ic n="mic"></ic> 去随手记</button></div>
    <div class="readrow row" v-for="d in log.days.filter(dayHasContent)" :key="d.date"><span class="chip nowrap" :class="dayHasContent(d) ? 'blue' : ''">{{ weekDayLabel(d.date) }}</span>
      <span class="tiny muted nowrap" v-if="d.log_id" title="周日志里的来源标签 D 编号对应这一天">D{{ dayRef(d) }}</span>
      <span class="grow small">{{ d.summary || (d.notes ? d.notes + ' 条随手记' : '无记录') }}</span>
      <a class="small" v-if="d.log_id && dayHasContent(d)" @click="go('log', {id: d.log_id})">查看</a></div>
    <div class="rd-wsup" v-if="editable"><div class="mt"><voice-button @text="addVoice"></voice-button></div>
      <textarea class="in mt" rows="2" v-model="voice" @input="dirty = true" placeholder="补充说明（可选）：漏记或需更正内容"></textarea></div></div>
  <div class="card mt" v-if="isWeek && (late.inWeek.length || (editable && late.carry.length))"><h2>迟到记录</h2>
    <div class="tiny muted">按记录时间归入当天。所属周已确认的，不改动已确认的数量；要不要补进本周，由你决定。</div>
    <div class="readrow row" v-for="n in late.inWeek" :key="'i' + n.note_id"><span class="chip orange nowrap">{{ lateText(n) }}</span>
      <span class="grow small">{{ weekDayLabel(n.note_date) }}{{ n.time ? ' ' + n.time : '' }} · {{ n.text || '随手记' }}<span class="tiny muted" v-if="n.week_confirmed"> · 本周已确认，数量未改动</span></span></div>
    <template v-if="editable"><div class="readrow row" v-for="n in late.carry" :key="'c' + n.note_id"><span class="chip orange nowrap">{{ lateText(n) }}</span>
      <span class="grow small">{{ weekDayLabel(n.note_date) }}{{ n.time ? ' ' + n.time : '' }} · {{ n.text || '随手记' }}<span class="tiny muted"> · 所属周已确认</span></span>
      <button class="btn sm" :disabled="lateUsed.includes(n.note_id)" @click="takeLate(n)">{{ lateUsed.includes(n.note_id) ? '已补入' : '补入本周' }}</button></div></template></div>
  <template v-if="editable && !isWeek">
    <div class="card"><h2>① 口述记录</h2>
      <voice-button @text="addVoice"></voice-button>
      <textarea class="in mt" rows="4" v-model="voice" @input="dirty = true" placeholder="例：3号墩承台钢筋绑扎完成六成，钢筋工12人；钢筋缺口8吨；明日继续绑扎、支模。"></textarea>
      <div class="tiny muted mt">说明施工内容、完成量、问题与次日计划；人员机械与昨日相同可省略。</div></div>
    <div class="card"><div class="row between"><h2>② 现场拍照 <span class="muted small">可选</span></h2><span class="tiny muted">照片用于形象进度与安全识别</span></div>
      <div class="ev-grid rd-ph mt">
        <div class="rd-phw" v-for="(p, i) in log.photos" :key="p.id"><ev-photo :src="p.thumb" :full="p.url" :label="'P' + (i + 1)" size="f" :time="phTime(p)">{{ safetyText(p) ? (phTime(p) ? ' · ' : '') + safetyText(p) : '' }}</ev-photo>
          <button class="rd-phx" @click="removePhoto(p)" title="移除这张照片" aria-label="移除这张照片"><ic n="x"></ic></button></div>
        <button type="button" class="rd-phadd" :disabled="uploading" @click="upload()"><ic n="camera"></ic><span>{{ uploading ? '上传中' : '拍照' }}</span></button></div></div>
  </template>
  <button v-if="editable" class="btn ai big block mt" :disabled="drafting || weekEmpty || (!isWeek && !voice.trim() && !log.photos.length)" @click="draft">
    {{ drafting ? (isWeek ? 'AI 汇总中 ' : 'AI 生成中 ') + sec + 's' : isWeek ? ((log.has_ai_draft || log.imported) ? '重新汇总' : 'AI 汇总本周') : ((log.has_ai_draft || log.imported) ? '③ 重新生成' : '③ 生成日志') }}</button>

  <details class="card mt" v-if="openQs.length && editable"><summary><b>待确认疑问 {{ openQs.length }} 项</b> <span class="tiny muted">可选答，不影响提交</span></summary>
    <div class="qcard" v-for="q in openQs" :key="q.i"><div>{{ q.question }}</div>
      <div class="opts"><button v-for="o in q.options" :key="o" class="opt" @click="answer(q.i, o)">{{ o }}</button>
        <button class="opt" @click="other(q.i)">其他</button></div></div>
    <div class="tiny muted mt">全部回答后，将据此自动修正日志。</div></details>

  <template v-if="(log.has_ai_draft || log.imported) || !editable">
    <div class="card mt" v-if="isWeek && log.week_review && log.week_review.rows.length">
      <review-table title="本周完成工程量：逐个作业核对（提交后更新进度计划）" :rows="wrRows" v-model="decisions" :readonly="!editable" :bulk="true" :cols="WR_COLS"
        hint="AI 汇总来自每日日志，点开“依据”可看是哪几天哪条记录。选“无法判断”或“不采纳”的作业不提交数量，不会补零。"></review-table></div>
    <div class="card mt" v-if="log.hazards && log.hazards.length"><h2><ic n="helmet"></ic> 照片识别隐患 {{ log.hazards.length }} 项</h2>
      <div class="readrow" v-for="h in log.hazards" :key="h.id" @click="go('finding', {id: h.id})" style="cursor:pointer"><span :class="['chip', LEVEL_F[h.level]]">{{ h.level_name }}</span><span class="chip orange" v-if="h.candidate">候选</span> {{ h.title }}</div></div>
    <div class="card mt"><div class="row between"><h2>日志预览</h2><button class="btn sm" v-if="editable" @click="toggleEdit">{{ editing ? '完成修改' : '修改' }}</button></div>
      <div class="small mt"><b>{{ c.summary }}</b></div>
      <div v-for="sec in SECTIONS" :key="sec.key" class="sec" v-show="!(isWeek && sec.key === 'progress') && ((c[sec.key] || []).length || editing)">
        <div class="sec-h"><h3>{{ secTitle(sec) }}</h3><div class="grow"></div>
          <button class="btn ghost sm" v-if="editing && (sec.key === 'labor' || sec.key === 'machinery')" @click="carry">同昨日</button>
          <button class="btn ghost sm" v-if="editing" @click="addRow(sec)"><ic n="plus"></ic></button></div>
        <div v-for="(row, i) in c[sec.key]" :key="i" :id="'row-' + sec.key + '-' + i" :class="['item', rowFlag(sec.key, i)]">
          <template v-if="editing">
            <template v-for="col in sec.cols" :key="col.k">
              <select v-if="col.type === 'act'" :class="['in', col.grow ? 'wide' : '']" v-model="row[col.k]" @change="dirty = true"><option value="">（未挂接）</option>
                <option v-for="a in actOptions(row[col.k])" :key="a.code" :value="a.code">{{ a.short_name || a.name }}</option></select>
              <select v-else-if="col.type === 'sel'" class="in" v-model="row[col.k]" @change="dirty = true"><option v-for="o in col.opts" :key="o">{{ o }}</option></select>
              <input v-else :class="['cell', col.type === 'num' ? 'num' : '', col.grow ? 'wide' : '']" :placeholder="col.label" :value="row[col.k]"
                @input="setCell(row, col, $event.target.value)" :inputmode="col.type === 'num' ? 'decimal' : 'text'">
            </template><button class="btn ghost sm del" @click="c[sec.key].splice(i, 1); dirty = true"><ic n="x"></ic></button>
          </template>
          <div v-else class="grow small">{{ readable(sec.key, row) }} <src-chips :sources="row.sources" :photos="log.photos"></src-chips></div>
        </div></div>
      <div class="sec" v-if="c.notes"><h3>其他</h3><div class="small">{{ c.notes }}</div></div>
    </div>
    <div class="card mt checks" v-if="warns.length"><b>规则提示 {{ warns.length }}</b>
      <div class="check" v-for="(w, i) in warnGroups" :key="i"><span><ic :n="w.level === 'error' ? 'ban' : 'alert'"></ic></span>
        <span><a class="muted small" v-if="w.where" @click="jump(w)">[{{ w.where }}]</a> {{ w.text }}</span></div></div>
  </template>
  <ai-note :page="isWeek ? 'week' : 'daylog'"></ai-note>
  <div class="bottom-bar" v-if="editable && (log.has_ai_draft || log.imported) && (isWeek || !weeklyMode)">
    <span class="small muted grow">用时 {{ Math.floor(active / 60) }}分{{ active % 60 }}秒<b class="rt-left" v-if="wrLeft"> · 还有 {{ wrLeft }} 项作业未决定</b></span>
    <button class="btn primary big" :disabled="submitting || wrLeft > 0" @click="submit">{{ submitting ? '提交中' : '确认提交' }}</button></div>
</div><div v-else class="empty">{{ err || '加载中' }}</div>`,
  data: () => ({ log: null, c: {}, decisions: {}, WR_COLS: { item: "作业", ai: "AI 汇总的本周工程量", ev: "依据（哪几天哪条）", dec: "人工决定" }, voice: "", dirty: false, drafting: false, sec: 0, uploading: false, submitting: false, editing: false,
    active: 0, timer: null, err: "", lateList: [], lateUsed: [], store, STATUS, STATUS_CHIP, SECTIONS, LEVEL_F }),
  computed: {
    editable() { return this.log && this.log.editable; },
    isWeek() { return this.log && this.log.period === "week"; },
    weeklyMode() { return ((store.me.project || {}).config || {}).cadence !== "day"; },
    openQs() { return (this.log.qa || []).map((q, i) => ({ ...q, i })).filter((q) => !q.answer); },
    // 周确认里“与每日合计不一致/没有数量”的提示已在核对表对应行显示，这里不重复
    warns() { return (this.log.checks || []).filter((x) => x.level !== "info" && !(this.isWeek && this.log.week_review && /与每日记录合计|没有该作业的数量/.test(x.message))); },
    warnGroups() {
      return groupChecks(this.warns, { works: "施工内容", progress: "工程量", labor: "人员", machinery: "机械", materials: "材料", issues: "问题",
                                       tomorrow: this.isWeek ? "下周计划" : "次日计划", photos: "照片" });
    },
    // 本周 7 天都没有记录、也没写补充说明：AI 没东西可汇总，不让空跑（U-07）
    weekEmpty() { return this.isWeek && !(this.log.days || []).some(dayHasContent) && !(this.voice || "").trim(); },
    late() { return this.isWeek ? lateSplit(this.lateList, this.log.log_date, this.log.period_end) : { inWeek: [], carry: [] }; },
    wrRows() {
      return ((this.log.week_review && this.log.week_review.rows) || []).map((r) => ({
        key: r.key, label: r.label, numeric: true, unit: r.unit, ai_raw: r.ai_value, flag: r.flag,
        ai_text: r.ai_value === null || r.ai_value === undefined ? "" : String(Math.round(r.ai_value * 1000) / 1000) + (r.unit ? " " + r.unit : "") + (r.pct !== null && r.pct !== undefined ? `，累计 ${r.pct}%` : ""),
        evidence: r.evidence }));
    },
    wrLeft() { return this.isWeek && this.log.week_review && this.editable ? reviewStatus(this.wrRows, this.decisions, "adopt").remaining : 0; },
    // 周确认流程：汇总 → 核对 → 提交 → 项目部核定；当前步骤由日志真实状态与未决定的行数算出
    step() { return this.isWeek ? weekStep(this.log, this.wrLeft) : -1; },
  },
  async mounted() {
    try {
      const id = store.route.params.id;
      const week = store.route.name === "week";
      const l = id ? await GET(`/api/logs/${id}`) : await POST("/api/logs", { log_date: store.route.params.date || today(), mode: "ai", period: week ? "week" : "day" });
      this.apply(l);
      this.active = lsGet(`zb_active_${l.id}`, 0);
      this.loadLate();
    } catch (e) { this.err = e.message; }
    this.timer = setInterval(() => { if (this.editable && document.visibilityState === "visible") { this.active++; if (this.active % 5 === 0) lsSet(`zb_active_${this.log.id}`, this.active); } }, 1000);
    this.poll = setInterval(() => this.refreshPhotos(), 8000);
    this.onSynced = () => { if (this.log) GET(`/api/logs/${this.log.id}`).then((l) => { this.log = { ...this.log, photos: l.photos, hazards: l.hazards }; }).catch(() => {}); };
    window.addEventListener("zb-quick-synced", this.onSynced);
  },
  unmounted() { clearInterval(this.timer); clearInterval(this.poll); window.removeEventListener("zb-quick-synced", this.onSynced); },
  methods: {
    go, today, dayHasContent, weekDayLabel, lateText,
    // 迟到记录（D5）：服务器还没有这个接口或离线时不显示
    async loadLate() {
      if (!this.isWeek) return;
      this.lateUsed = lsGet("zb_late_used_" + this.log.id, []) || [];
      try { const r = await GET("/api/quick/late-notes?days=21", { quiet: true }); this.lateList = Array.isArray(r) ? r : []; } catch (_) { this.lateList = []; }
    },
    // 补入本周：写进本周补充说明，重新汇总后才计入；所属周已确认的数量不动
    takeLate(n) {
      this.voice = (this.voice ? this.voice + "\n" : "") + `补记 ${weekDayLabel(n.note_date)}：${n.text || "随手记 #" + n.note_id}`; this.dirty = true;
      this.lateUsed = [...this.lateUsed, n.note_id]; lsSet("zb_late_used_" + this.log.id, this.lateUsed);
      toast("已补入本周补充说明，重新汇总后计入");
    },
    jump(w) {
      const r = w.refs.find((x) => x.section && x.index !== null), el = r && document.getElementById(`row-${r.section}-${r.index}`);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    },
    dayRef(d) { return this.log.days.filter((x) => x.log_id && x.date <= d.date).length || "-"; },  // 与日志正文来源标签的 D 编号一致（按有日志的天排序）
    apply(l) {
      this.log = l; this.c = clone(l.content); this.voice = l.voice_text; this.dirty = false;
      this.decisions = l.week_review ? reviewFromSaved(l.week_review.rows) : {};  // 退回后重新提交：带出上次的决定，可改
    },
    async reorganize() {  // 新随手记未并入人工改过的草稿：技术员确认后才按全部随手记重新整理（覆盖手改）
      if (!confirm("按今天全部随手记重新整理，会覆盖你在草稿上的修改。继续？")) return;
      try { this.apply(await POST("/api/logs/organize", { day: this.log.log_date })); toast("已重新整理"); } catch (e) { toast(e.message, true); }
    },
    addVoice(t) { this.voice = (this.voice ? this.voice + "。" : "") + t; this.dirty = true; },
    phTime(p) { return String(p.taken_at || p.uploaded_at || "").slice(5, 16).replace("T", " "); },   // 照片时间：拍摄时间优先，没有则写上传时间（MM-DD HH:mm）
    safetyText(p) { return { queued: "待上传", pending: "识别中", hazard: "发现隐患", clear: "未见隐患", failed: "识别失败", skipped: "未识别" }[p.safety_status] || ""; },
    async refreshPhotos() {
      if (!this.log || !this.log.photos.some((p) => p.safety_status === "pending")) return;
      const l = await GET(`/api/logs/${this.log.id}`); this.log = { ...this.log, photos: l.photos, hazards: l.hazards };
    },
    async toggleEdit() {
      if (this.editing) { try { await this.save(); if (this.dirty === false) toast("已保存"); } catch (e) { toast(e.message, true); } }
      this.editing = !this.editing;
    },
    secTitle(sec) { return this.isWeek ? ({ tomorrow: "下周计划", progress: "本周完成工程量（提交后更新进度计划）", labor: "人员投入（日均）", machinery: "机械投入（日均）" }[sec.key] || sec.title) : sec.title; },
    readable(key, r) {
      const a = (code) => { if (!code) return ""; const nm = (c) => { const x = this.log.activities.find((v) => v.code === c); return x ? x.short_name || x.name : c; };
        return `［${String(code).split(/[,，、]/).map((c) => nm(c.trim())).join("、")}］`; };
      if (key === "works") return `${r.part || ""}${r.process ? " " + r.process : ""}：${r.desc} ${a(r.activity_code)}`;
      if (key === "progress") return `${a(r.activity_code)} ${r.qty !== null && r.qty !== undefined ? (this.isWeek ? "本周完成 " : "完成 ") + r.qty + (r.unit || "") : ""}${r.pct !== null && r.pct !== undefined ? "，累计 " + r.pct + "%" : ""} ${r.note || ""}`;
      if (key === "labor") return `${r.trade} ${r.count ?? "?"} 人`;
      if (key === "machinery") return `${r.name} ${r.count ?? "?"} 台 · ${r.status || ""}`;
      if (key === "materials") return `${r.name} ${r.action || ""} ${r.qty ?? ""}${r.unit || ""}`;
      if (key === "issues") return `[${r.type}/${r.severity}] ${r.desc}${r.measure ? "；措施：" + r.measure : ""} ${a(r.activity_code)}`;
      if (key === "tomorrow") return `${r.desc} ${a(r.activity_code)}`;
      return "";
    },
    rowFlag(sec, i) { const c = this.log.checks.filter((x) => x.section === sec && x.index === i); return c.some((x) => x.level === "error") ? "err" : c.some((x) => x.level === "warn") ? "flag" : ""; },
    actOptions(cur) { const list = this.log.activities.slice(); if (cur && !list.find((a) => a.code === cur)) list.unshift({ code: cur, name: "" }); return list; },
    setCell(row, col, v) { if (col.type === "num") { const n = v.trim() === "" ? null : Number(v); if (!Number.isNaN(n)) row[col.k] = n; } else row[col.k] = v; this.dirty = true; },
    addRow(sec) { (this.c[sec.key] = this.c[sec.key] || []).push(blankRow(sec)); this.dirty = true; },
    async save() {
      if (!this.dirty) return;
      const l = await PUT(`/api/logs/${this.log.id}`, { content: this.c, version: this.log.version, voice_text: this.voice });
      this.log = l; this.dirty = false;
    },
    async upload() {
      const got = await takePhotosToUpload(); if (!got) return;
      const { files, scan } = got;
      this.uploading = true;
      const fb = photoFeedback.begin(files.length, scan), sent = [];
      try {
        for (let i = 0; i < files.length; i += 4) { const fd = new FormData(); fd.append("log_id", this.log.id); addPhotos(fd, files.slice(i, i + 4), scan); sent.push(...(await POST("/api/media", fd))); }
        fb.sent(sent);
        if (sent.some((m) => m.queued)) {  // 没有信号：先在本页显示，联网后自动上传
          this.log = { ...this.log, photos: [...this.log.photos, ...sent.filter((m) => m.queued)] }; toast("无信号，照片已暂存，联网后自动上传"); return;
        }
        const l = await GET(`/api/logs/${this.log.id}`); this.log = { ...this.log, photos: l.photos };
      } catch (e) { fb.fail(e.message); toast(e.message, true); } finally { this.uploading = false; }
    },
    async removePhoto(p) {
      if (p.queued) { await window.QUICK.discard(p.id); this.log = { ...this.log, photos: this.log.photos.filter((x) => x.id !== p.id) }; return; }
      const l = await DEL(`/api/logs/${this.log.id}/photos/${p.id}`); this.log = { ...this.log, photos: l.photos };
    },
    async draft() {
      this.drafting = true; this.sec = 0; const t = setInterval(() => this.sec++, 1000);
      try { await this.save(); this.apply(await POST(`/api/logs/${this.log.id}/draft`, { voice_text: this.voice })); }
      catch (e) { toast(e.message, true); } finally { clearInterval(t); this.drafting = false; }
    },
    async answer(i, v) {
      try {
        const l = await POST(`/api/logs/${this.log.id}/answer`, { index: i, answer: v });
        this.log = { ...this.log, qa: l.qa };
        if (!this.openQs.length) { toast("已回答，正在更新日志"); await this.draft(); }
      } catch (e) { toast(e.message, true); }
    },
    other(i) { const v = prompt("请输入说明："); if (v && v.trim()) this.answer(i, v.trim()); },
    async carry() { try { await this.save(); this.apply(await POST(`/api/logs/${this.log.id}/carry`)); toast("已沿用昨日人员机械"); } catch (e) { toast(e.message, true); } },
    async submit() {
      this.submitting = true;
      try {
        await this.save();
        const body = { version: this.log.version, active_seconds: this.active };
        if (this.isWeek && this.log.week_review) {  // 周进度确认：每个作业都有决定才提交；无法判断/不采纳的不提交数量
          if (this.wrLeft) { toast(`还有 ${this.wrLeft} 项作业未决定`, true); return; }
          body.decisions = reviewPayload(this.wrRows, this.decisions);
        }
        this.apply(await POST(`/api/logs/${this.log.id}/submit`, body));
        lsDel(`zb_active_${this.log.id}`); toast(this.isWeek ? "本周已确认：每日日志一并提交，进度计划随之更新" : "已提交，进度计划随之更新，审核智能体预审中"); }
      catch (e) { toast(e.message, true); } finally { this.submitting = false; }
    },
  },
};

// 随手拍隐患
const SafetySnap = {
  props: ["embedded"],
  template: `<div><template v-if="embedded">
    <button id="todo-focus-snap" class="btn big block cta" :disabled="busy" @click="upload"><ic n="camera"></ic> 随手拍隐患<span class="tiny">{{ busy ? '上传中' : '拍完 AI 识别，疑似隐患等安全员确认后再派单' }}</span></button></template>
  <template v-else><h1 class="mb">随手拍隐患</h1>
  <div class="card"><div class="small muted">拍摄现场照片，AI 识别疑似隐患，等安全员确认后再派单。</div>
    <button class="btn ai big block mt" :disabled="busy" @click="upload"><ic n="camera"></ic> 拍照上报</button>
    <div class="thinking" v-if="busy"><div class="spinner"></div>上传中</div></div></template>
  <div class="card mt" v-for="f in found" :key="f.id" @click="go('finding', {id: f.id})" style="cursor:pointer">
    <span :class="['chip', LEVEL_F[f.level]]">{{ f.level_name }}</span> <b>{{ f.title }}</b><div class="small mt">整改：{{ f.body.measure }}</div></div>
  </div>`,
  data: () => ({ busy: false, found: [], alive: true, LEVEL_F }),
  unmounted() { this.alive = false; },
  methods: {
    go,
    // 只等上传完成就放行（可接着拍下一批）；识别在后台，结果由气泡提示，下面的列表在后台轮询到后追加
    async upload() {
      const got = await takePhotosToUpload(); if (!got) return;
      const { files, scan } = got;
      this.busy = true;
      const fb = photoFeedback.begin(files.length, scan);
      try {
        const fd = new FormData(); fd.append("purpose", "safety"); addPhotos(fd, files, scan);
        const ms = await POST("/api/media", fd); fb.sent(ms);
        if (ms.some((m) => m.queued)) { toast("无信号，照片已暂存，联网后自动" + (scan ? "上传并识别" : "上传")); return; }
        if (scan) this.track(new Set(ms.map((m) => m.id)));
      } catch (e) { fb.fail(e.message); toast(e.message, true); } finally { this.busy = false; }
    },
    async track(ids) {
      for (let k = 0; k < 30 && this.alive; k++) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const st = await GET("/api/quick/media-status?ids=" + [...ids].join(","), { quiet: true });
          if (st.pending) continue;
          const all = await GET("/api/findings?agent=safety&status=active&mine=1", { quiet: true });
          this.found.push(...all.filter((f) => ids.has(f.ref_id) && !this.found.some((x) => x.id === f.id)));
          return;
        } catch (_) { /* 下一轮再试 */ }
      }
    },
  },
};
// 离线模式下安全员首页取不到数据（只显示“没有加载出来”），随手拍照常可用：照片先存本机，联网后上传并识别。
// 只在离线模式出现；连回服务器即隐藏，首页自己的随手拍按钮仍是唯一入口。
const OfflineSnap = {
  name: "OfflineSnap",
  components: { SafetySnap },
  template: `<div class="mb" v-if="store.offline"><safety-snap :embedded="true"></safety-snap></div>`,
  data: () => ({ store }),
};
EXT.slot("top:shome", OfflineSnap, { order: -900 });

// 我的待办 / 发现详情（技术员、安全员整改，港湾各岗位复核共用）
const TaskList = {
  template: `<div><page-head page="tasks" :title="isTech() ? '我的待办' : '整改单'"><template #actions><div class="seg" v-if="!isTech()"><button v-for="s in segs" :key="s[0]" :class="{on: seg === s[0]}" @click="seg = s[0]">{{ s[1] }} {{ count(s[0]) }}</button></div></template></page-head>
    <div class="banner info" v-if="offline">没有网络，显示本机暂存内容</div>
    <div class="rs-cards"><div class="zb-card rs-card" v-for="t in shown" :key="t.id" role="button" tabindex="0" @click="go('finding', {id: t.id})" @keyup.enter="go('finding', {id: t.id})">
      <ev-photo size="s" :src="t.ref_type === 'media' ? '/api/media/' + t.ref_id + '/thumb' : ''" :full="t.ref_type === 'media' ? '/api/media/' + t.ref_id + '/file' : ''"></ev-photo>
      <div class="rs-li-b"><div class="row wrap"><span :class="['chip', LEVEL_F[t.level]]">{{ t.agent_name }}</span><span class="chip">{{ t.status_name }}</span>
        <span :class="['tiny', {'tc-over': t.overdue}]">{{ t.overdue ? '已超期 ' : '期限 ' }}{{ t.due_date || '—' }}</span></div>
      <b class="rs-card-t">{{ t.title }}</b><div class="tiny muted" v-if="!isTech()">责任人：{{ t.assignee || '未指派' }}</div></div></div></div>
    <empty-state v-if="err" :reason="err" :action="{ label: '重试', fn: load }"></empty-state>
    <empty-state v-else-if="loaded && !shown.length" :reason="emptyReason" :action="emptyAction"></empty-state>
    <ai-note page="tasks"></ai-note></div>`,
  data: () => ({ items: [], seg: "all", loaded: false, offline: false, err: "", LEVEL_F }),
  computed: {
    emptyReason() {
      if (isTech()) return "目前没有派给你的整改或质控。";
      return this.seg === "all" ? "没有待处理的整改单。" : "这个分类下没有整改单。";
    },
    emptyAction() {
      if (isTech()) return null;       // 没有派给自己的事项就是没有，不放与原因无关的去往别页的按钮
      if (this.seg !== "all") return { label: "看全部", fn: () => { this.seg = "all"; } };
      return isSubOrg() ? { label: "随手拍隐患", route: "shome", focus: "snap" } : null;
    },
    segs: () => [["all", "全部"], ["over", "超期"], ["todo", "待整改"], ["review", "待复核"]],
    shown() { return this.items.filter((t) => this.match(t, this.seg)); },
  },
  mounted() { this.load(); },
  methods: {
    go, isTech,
    async load() {
      const key = "zb_tasks_" + (store.me ? store.me.user.id : 0);
      this.err = "";
      try { this.items = await GET(isTech() ? "/api/findings?status=active&mine=1" : "/api/findings?status=active"); this.offline = false; lsSet(key, this.items); }
      catch (e) {  // 断网：用上次的列表撑着；本机也没有时给出原因和重试，不白屏
        const c = e.offline ? lsGet(key, null) : null;
        if (c) { this.items = c; this.offline = true; } else this.err = e.offline ? "没有网络，显示本机暂存内容：这台手机上还没有整改单列表，联网后再看" : e.message;
      }
      this.loaded = true;
    },
    match(t, k) { return k === "all" || (k === "over" && t.overdue) || (k === "todo" && ["open", "rejected"].includes(t.status)) || (k === "review" && t.status === "rectifying"); },
    count(k) { return this.items.filter((t) => this.match(t, k)).length; },
  },
};

// 派单：从可选人员里选责任人（分包单位账号只会看到本单位人员）
const AssignBox = {
  props: ["finding"],
  emits: ["changed"],
  template: `<div class="row wrap rs-assign"><select class="in" v-model="sel" aria-label="改派给"><option :value="0" disabled>改派给</option>
      <option v-for="a in people" :key="a.id" :value="a.id">{{ a.name }}（{{ a.company }} · {{ a.role_name }}）</option></select>
    <button class="btn" :disabled="!sel || sel === finding.assignee_id || busy" @click="go">派单</button></div>`,
  data() { return { people: [], sel: 0, busy: false }; },
  async mounted() {
    this.sel = this.finding.assignee_id || 0;
    try { this.people = await (AssignBox._p || (AssignBox._p = GET("/api/assignees"))); } catch (e) { AssignBox._p = null; toast(e.message, true); }   // 取人员失败不缓存失败结果，下次打开再取
  },
  methods: { async go() { this.busy = true; try { const f = await PUT(`/api/findings/${this.finding.id}/assignee`, { assignee_id: this.sel }); toast("已派单，已通知对方"); this.$emit("changed", f); } catch (e) { toast(e.message, true); } finally { this.busy = false; } } },
};

// 本页会话内打开过的事项（只在内存）：台账页在宽窄布局之间切换（窗口缩放、横竖屏、整页截图）会重建右侧详情，
// 有这份就先显示上次内容、后台刷新，不再停在“加载中”（10-10 差距分析：安全台账右侧详情偶发停在加载中）
const FD_MEM = new Map();
const FindingDetail = {
  components: { AssignBox, CandidateBox },
  props: ["id", "embed"],   // embed：嵌在安全/质量页右侧详情栏里，不再出页头和边界说明（页面本身已有）
  template: `<div v-if="f" :class="['rs-fd', {'fd-embed': embed, 'rs-fd-busy': switching}]" :aria-busy="switching ? 'true' : null"><page-head page="finding" :steps="stepFlow" :current="stepNow" v-if="!embed"><template #meta><a @click="back">← 返回</a> · S{{ f.id }} · 发现 {{ fmtDT(f.created_at) }}</template></page-head>
  <div class="banner info" v-if="offline">没有网络，显示本机暂存内容</div>
  <section class="zb-card rs-fd-card" :key="f.id"><div class="rs-fd-a"><div class="row wrap rs-fd-chips"><span :class="['chip', LEVEL_F[f.level]]">{{ f.level_name }}</span><span class="chip">{{ f.agent_name }}</span>
      <span :class="['chip', f.candidate ? 'orange' : '']">{{ f.status_name }}</span><span class="tiny muted" v-if="embed">S{{ f.id }} · {{ fmtDT(f.created_at) }}</span>
      <a class="zb-link rs-fd-open" v-if="embed" @click="go('finding', {id: f.id})">单独打开 <ic n="chevron"></ic></a></div>
    <h2 class="rs-fd-t">{{ f.title }}</h2>
    <candidate-box v-if="f.candidate" :f="f" :thumb="false" @changed="load"></candidate-box>
    <dl class="rs-facts" v-if="facts.length"><template v-for="x in facts" :key="x[0]"><dt>{{ x[0] }}</dt><dd :class="x[2]">{{ x[1] }}<b v-if="x[0] === '责任人' && f.overdue" class="tc-over"> 已超期</b></dd></template></dl>
    <div v-if="f.body.points"><div class="small mt" v-for="(p, i) in f.body.points" :key="i"><ic n="square"></ic> [{{ p.stage }}] {{ p.item }} <span class="muted">（{{ p.record }}）</span></div></div>
    <div v-if="f.body.checks"><div class="small mt" v-for="(c, i) in f.body.checks" :key="i">· {{ c.message }}</div></div>
    <a v-if="f.ref_type === 'log' && !isSubOrg()" class="small" @click="go('log', {id: f.ref_id})">查看日志 L{{ f.ref_id }} →</a></div>
    <div class="rs-ev" v-if="showPair"><div class="ev-pair">
      <ev-photo size="f" :src="f.photo && f.photo.thumb" :full="f.photo && f.photo.url" label="整改前 · 发现时" alt="发现时现场照片">{{ beforeCap }}</ev-photo>
      <ev-photo size="f" :src="f.rectify_photo && f.rectify_photo.thumb" :full="f.rectify_photo && f.rectify_photo.url" label="整改后" alt="整改后现场照片">{{ afterCap }}<span class="chip orange" v-if="lateUpload(f.rectify_photo)" title="拍完过了一段时间才传上来，时间以拍摄时间为准">补传</span></ev-photo></div></div>
    <div class="rs-fd-b"><div class="banner info mt" v-for="a in pend" :key="a.cid">
      <template v-if="a.status === 'failed'">整改照片没有提交：{{ a.err }}<template v-if="a.code === 'photo_before_dispatch' && a.canSafety"> <a @click="asSafety(a)">改作随手拍</a></template></template>
      <template v-else>整改照片已暂存，联网后自动提交复核</template></div>
    <div :class="['ai-box', 'mt', {muted: vv.back}]" v-if="vv"><div class="hd">安全智能体整改复核<template v-if="vv.back">（上一次提交）</template></div>
      <div class="small" v-if="vv.closed"><b>{{ vv.closed }}</b></div>
      <b>{{ vv.text }}</b>
      <div class="small" v-if="vv.reason">{{ vv.reason }}</div>
      <div class="row wrap mt" v-if="vv.marks.length && canJudge"><span class="chip orange" v-for="m in vv.marks" :key="m">{{ m }}</span></div>
      <div class="row wrap mt" v-if="vv.reverify && canReverify"><button class="btn sm" :disabled="busy" @click="reverify">重新让 AI 复核</button></div></div>
    <div class="row wrap mt rs-fd-acts" v-if="!isLeader() && !f.candidate">
      <button class="btn ai" v-if="canRectify && !pend.length" :disabled="upl" @click="rectify"><ic n="camera"></ic> {{ upl ? '上传中' : '拍整改照片' }}</button>
      <button class="btn primary" v-if="f.status !== 'resolved' && canClose" @click="setStatus('resolved')">{{ f.agent === 'safety' ? '人工确认闭环' : '已完成' }}</button>
      <button class="btn" v-if="f.status === 'rectifying' && canJudge" @click="setStatus('open', true)">退回重改</button>
      <button class="btn" v-if="canJudge && f.status !== 'dismissed' && f.status !== 'resolved'" @click="setStatus('dismissed')">误报/忽略</button>
      <button class="btn" v-if="canJudge && ['resolved', 'dismissed'].includes(f.status)" @click="setStatus('open')">重新打开</button>
      <button class="btn" v-if="can('nudge') && f.assignee_id && active" @click="nudge"><ic n="bell"></ic> 催办</button>
    </div>
    <div class="mt rs-fd-assign" v-if="can('dispatch') && active"><div class="tiny muted mb">派单 / 改派</div><assign-box :finding="f" @changed="load"></assign-box></div>
    <div class="thinking" v-if="upl"><div class="spinner"></div>整改照片上传中，传完即提交复核</div>
    <div class="small mt" v-if="f.feedback_note && f.returned"><b>退回原因：</b>{{ f.feedback_note }}</div>
    <div class="small muted mt" v-else-if="f.feedback_note">处理说明：{{ f.feedback_note }}</div></div></section>
  <ai-note :page="f.agent === 'safety' ? 'safety' : 'quality'" v-if="!embed"></ai-note></div>
  <load-state v-else :loading="!err" :error="err || null" text="正在打开事项…" @retry="load"></load-state>`,
  data: () => ({ f: null, busy: false, upl: false, err: "", offline: false, now: Date.now(), polls: 0, switching: false, store, LEVEL_F }),
  computed: {
    // 事实一行一项（来源、措施、依据、责任人），候选未确认前只看 AI 依据（在 candidate-box 里）
    facts() {
      const f = this.f, b = f.body || {}, out = [];
      if (b.source === "扫码") out.push(["来源", "扫码上报" + (b.reporter ? `（${b.reporter}）` : "") + ` · 回执 ${b.receipt || "—"}` + (b.report_note ? ` · 说明：${b.report_note}` : "")]);
      if (f.candidate) return out;
      if (b.measure) out.push(["整改措施", b.measure]);
      if (b.basis) out.push(["依据", b.basis, "muted"]);
      if (f.assignee) out.push(["责任人", `${f.assignee} · 期限 ${f.due_date || "—"}`]);
      return out;
    },
    // 整改前后对照：安全隐患或已有照片时显示，两张等宽 4:3；没有的一侧如实写原因
    showPair() { return !!(this.f.photo || this.f.rectify_photo || (this.f.agent === "safety" && this.f.ref_type === "media")); },
    beforeCap() { const p = this.f.photo; if (!p) return "没有发现时照片"; return p.taken_at ? "拍摄 " + fmtDT(p.taken_at) : p.uploaded_at ? "上传 " + fmtDT(p.uploaded_at) : "时间未知"; },
    afterCap() {
      const p = this.f.rectify_photo;
      if (p) return p.taken_at ? "拍摄 " + fmtDT(p.taken_at) + " " : p.uploaded_at ? "上传 " + fmtDT(p.uploaded_at) + " " : "时间未知 ";
      if (this.f.candidate) return "确认派单后由责任人上传";
      return this.f.status === "resolved" ? "未上传整改照片" : this.f.status === "dismissed" ? "已忽略，无整改" : "待责任人整改后上传";
    },
    // 隐患流程：候选 → 确认派单 → 整改 → 复查闭环（质量事项与已不采纳的隐患不画流程）
    stepFlow() { return this.f.agent === "safety" && this.f.status !== "dismissed" ? "hazard" : ""; },
    stepNow() { return hazardStep(this.f); },
    active() { return ["open", "rectifying", "rejected"].includes(this.f.status) && !this.f.candidate; },
    // 安全事项由安全主管确认，质量/数据事项由工程主管确认；分包单位只提交整改
    canJudge() { return can(this.f.agent === "safety" ? "site_safety" : "site_quality"); },
    canClose() { return this.f.agent === "safety" ? can("site_safety") : (can("site_quality") || isSubOrg()); },
    canRectify() { return this.f.agent === "safety" && !this.f.candidate && ["open", "rejected"].includes(this.f.status) && (isSubOrg() || can("site_safety")); },
    // 复核显示按 4.4 契约（verifyView）；重新让 AI 复核只给安全主管、项目经理
    vv() { return verifyView(this.f, this.now); },
    canReverify() { return ["safety_sup", "manager"].includes(roleOf()); },
    // 这条隐患还在本机排队（或被拒）的整改提交
    pend() { return window.QUICK ? window.QUICK.pendingActions(this.f.id) : []; },
  },
  // 安全/质量页右侧详情：换选中事项时不卸载重建，旧内容淡显到新内容到达（不再闪成“加载中”），先发后到的旧结果丢弃
  watch: { id() { this.polls = 0; this.err = ""; this.switching = !!this.f; this.load(); } },
  mounted() {
    const m = FD_MEM.get(+this.id);
    if (m) { this.f = m; this.switching = true; }
    this.load();
    this.onSynced = (e) => { if (((e && e.detail && e.detail.items) || []).some((x) => x.fid === +this.id)) this.load(); };
    this.onOnline = () => this.load();
    window.addEventListener("zb-quick-synced", this.onSynced); window.addEventListener("online", this.onOnline);
  },
  unmounted() { this.gone = true; clearTimeout(this.pt); window.removeEventListener("zb-quick-synced", this.onSynced); window.removeEventListener("online", this.onOnline); },
  methods: {
    go, can, isLeader, isSubOrg, fmtDT, lateUpload, back() { history.back(); },
    async load() {
      this.now = Date.now();
      const id = this.id, n = (this.seq = (this.seq || 0) + 1);
      try {
        const f = await GET(`/api/findings/${id}`);
        if (n !== this.seq) return;
        this.f = f; this.offline = false; this.err = ""; this.switching = false; FD_MEM.set(+id, f);
        try { fcPut(f); } catch (_) { /* 本机暂存写不进（空间满）不影响显示 */ }
      }
      catch (e) {  // 断网：用本机暂存的这张整改单撑着，不白屏（AC-1.12）；本机也没有时给出原因和重试
        if (n !== this.seq) return;
        this.switching = false;
        if (this.f && this.f.id !== +id) this.f = null;   // 换了事项：不把上一条的内容当成这一条
        const c = e.offline ? this.f || fcGet(id) : null;
        if (c) { this.f = c; this.offline = true; }
        else if (this.f) toast(e.message, true);
        else this.err = e.offline ? "没有网络，显示本机暂存内容：这张整改单还没在这台手机上打开过，联网后再看" : e.message;
      }
      if (this.f) store.findingAgent = this.f.agent;
      this.watchVerify();
    },
    // AI 复核在后台进行：复核中每 5 秒看一次结果，最多 3 分钟
    watchVerify() {
      clearTimeout(this.pt);
      if (this.gone || this.offline || !this.vv || !this.vv.busy || this.polls >= 36) return;
      this.polls++; this.pt = setTimeout(() => this.load(), 5000);
    },
    async rectify() {
      const [file] = await takePhotos({ multi: false }); if (!file) return;
      this.upl = true;  // 上传完成前按钮不可再点；慢网超时自动转本机暂存
      try {
        const fd = new FormData(); fd.append("purpose", "rectify"); addPhotos(fd, [file]); const [m] = await POST("/api/media", fd);
        const r = await POST(`/api/findings/${this.id}/rectify`, { media_id: m.id });
        if (r && r.queued) { toast("已暂存，联网后自动提交复核"); return; }  // 本机暂存：不刷新（断网刷新只会失败），补传成功后自动刷新
        this.polls = 0; await this.load();
      } catch (e) {
        if (e.code === "photo_before_dispatch") this.rejected(e.message, file); else toast(e.message, true);
      } finally { this.upl = false; }
    },
    // 拍摄时间早于派单超过 24 小时：这张照片不挂到这条整改（服务器上已保存）；可以改作随手拍隐患上报，照片不丢
    async rejected(msg, file) {
      if (window.QUICK && confirm(msg + "\n这张照片可以改作随手拍隐患上报，要改吗？")) { if (await window.QUICK.asSafety(file)) toast("已改作随手拍，上传后 AI 识别隐患"); }
      else toast(msg, true);
    },
    async asSafety(a) { if (await window.QUICK.asSafety(a.cid)) toast("已改作随手拍，上传后 AI 识别隐患"); },
    async reverify() {
      this.busy = true;
      try { await POST(`/api/findings/${this.id}/reverify`); toast("已重新让 AI 复核"); this.polls = 0; await this.load(); }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
    async setStatus(s, back) {
      const note = s === "dismissed" ? prompt("请填写原因：") : back ? prompt("请填写退回原因（将通知责任人）：") : "";
      if (note === null) return;
      if (back && !note.trim()) { toast("请填写退回原因", true); return; }
      try { await POST(`/api/findings/${this.id}/status`, { status: s, note: note || "" }); await this.load(); toast("已更新"); } catch (e) { toast(e.message, true); }
    },
    async nudge() { const t = prompt("催办说明（可空）：", ""); if (t === null) return; try { await POST(`/api/findings/${this.id}/nudge`, { text: t }); toast("已催办，已通知对方"); } catch (e) { toast(e.message, true); } },
  },
};
