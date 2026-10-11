/* x_nudge：催办Agent 升级链（催办台 + 今日动态）与订阅式提问（固定的提问）。
   - 路由 chase：催办记录（升级链、立即运行/预览、升级记录），导航入口在工程主管的“智能体”组，安全主管从首页条目进入；规则由系统管理员在“项目与规则”页底部（NudgeRules）修改；
   - EXT.feed work / eng / sdash：向首页“需要关注”区的折叠块“催办与固定的提问”提供条目 = 催办Agent 今日动态 + 固定/订阅的提问（无内容不出条目；要我办的事走统一待办 /api/todo）；
     项目经理只拍板，不进催办记录（D7、AC-6.7）：他的首页只有固定的提问，没有催办Agent 今日动态和任何指向 #/chase 的入口；
   - 问工笔回答下的“固定到首页/订阅”按钮在 views.js，通过 NUDGE.bump() 通知（首页每次进入时重新取数）。 */

const NUDGE_ROLES = ["safety_sup", "eng_sup", "manager"];
const NUDGE_STAGES = ["提醒本人", "单位安全员", "港湾主管", "项目经理"];
const NUDGE_AREA = { safety: "安全隐患", quality: "质量质控", log: "日志/周确认", action: "行动项" };
const NUDGE = { ver: reactive({ n: 0 }), bump() { this.ver.n++; } };
const nudgeOk = () => !!store.me && NUDGE_ROLES.includes(store.me.user.role);
const stripRefs = (t) => String(t || "").replace(/\[[A-Za-z]+\d+\]/g, "").replace(/\s+/g, " ").trim();
function nudgeRefs(a) {
  if (!a) return [];
  const list = (a.refs && a.refs.length) ? a.refs : (String(a.answer || "").match(/\[([A-Za-z]+\d+)\]/g) || []).map((s) => s.slice(1, -1));
  return [...new Set(list)].slice(0, 8);
}
function nudgeOpenRef(ref) {
  if (/^L\d+$/.test(ref)) go("log", { id: ref.slice(1) });
  else if (/^W\d+$/.test(ref)) go("alert", { id: ref.slice(1) });
  else go("progress", { tab: "plan", q: ref });
}

// ---------------------------------------------------------------------------- 首页“需要关注”区条目（原“我的看板”卡）
// 催办Agent 今日动态：有自动提醒才出条目（没有就不占位）；固定/订阅的提问各成一条，刷新、取消就地完成。
async function nudgeFeedItems() {
  if (!nudgeOk() || !featOn("nudge")) return [];
  const desk = roleOf() !== "manager";
  const [t, pins] = await Promise.all([desk ? GET("/api/nudge/today", { quiet: true }).catch(() => null) : null, GET("/api/nudge/pins", { quiet: true }).catch(() => [])]);
  const out = [];
  if (t && t.enabled && t.objects) {
    const defer = t.deferred_quiet + t.deferred_cap;
    out.push({ agent: "催办Agent", level: t.escalated_n ? "orange" : "info",
      title: `今日自动提醒 ${t.persons} 人 · ${t.objects} 件事项${t.escalated_n ? `，已升级主管 ${t.escalated_n} 件` : ""}`,
      basis: `按升级链逐级提醒，处理后自动停止${defer ? `；延后 ${defer} 件（${t.deferred_quiet ? "静默时段" : "每日上限"}）` : ""}`,
      actions: [{ label: "催办记录", route: "chase" }] });
  }
  for (const p of pins || []) {
    const s = p.answer ? stripRefs(p.answer.answer) : "";
    out.push({ agent: "问工笔", level: "info", title: p.question, at: p.last_run_at || "",
      basis: p.last_error ? `刷新失败：${p.last_error}，仍显示上次结果。${s.slice(0, 80)}` : (s ? (s.length > 120 ? s.slice(0, 120) + "…" : s) : "暂无答案，请刷新"),
      actions: [{ label: "刷新", api: { method: "POST", url: `/api/nudge/pins/${p.id}/refresh` }, done: "已更新" },
        ...(nudgeRefs(p.answer).slice(0, 2).map((r) => ({ label: "出处 " + r, route: /^L\d+$/.test(r) ? "log" : /^W\d+$/.test(r) ? "alert" : "progress", params: /^[LW]\d+$/.test(r) ? { id: r.slice(1) } : { tab: "plan", q: r } }))),
        { label: "取消固定", api: { method: "DELETE", url: `/api/nudge/pins/${p.id}` }, done: "已取消" }] });
  }
  return out;
}
EXT.feed(["work", "eng", "sdash"], nudgeFeedItems);

/// ---------------------------------------------------------------------------- 催办记录
// 10-10 改版：升级链一张卡（手机竖排）→ 最近记录小结（只数下面这批记录）→ 本次运行结果 → 升级记录（手机按卡片行显示）
// 10-10 一屏化：说明收进页头问号；升级链压成一行；升级记录是纯罗列，用 zb-paged 内部分页（电脑每页 15 条、手机 5 条）。
const ChaseDesk = {
  template: `<div v-if="rules" class="ra-chase">
  <page-head page="chase"><template #meta>每 30 分钟自动检查 · 规则由系统管理员设置<span class="chip orange cd-off" v-if="sched && !sched.on">后台未开启</span><help-tip caption="说明">每 30 分钟自动检查一次，不改变业务状态，不提醒公司领导与系统管理员；规则由系统管理员在“项目与规则”里设置。<template v-if="sched && !sched.on">{{ sched.note }}</template></help-tip></template>
    <template #actions><button class="btn" :disabled="busy" @click="run(true)">预览</button><button class="btn primary" :disabled="busy" @click="run(false)">{{ busy ? '运行中' : '立即运行' }}</button></template></page-head>

  <section class="zb-card cd-chain"><div class="zb-ch"><ic n="layers"></ic><h2>升级链</h2><help-tip caption="升级规则">进度类催办不发给分包安全员，本人之后直接升级到工程主管；中低风险隐患最多升级到安全主管，只有高风险隐患升级到项目经理。某一级无对应人员时，自动顺延至下一级。港湾主管：安全类为安全主管，其他为工程主管。<template v-if="'logs' in form.sources">日志/周确认缺报只升级到工程主管，不升级到项目经理（项目经理只关注进度、质量、安全）。</template></help-tip><span class="grow"></span><span class="zb-src">{{ form.enabled === false ? '自动催办未启用' : '处理后自动停止' }}</span></div>
    <ol class="cd-flow">
      <li><b>① 责任人本人</b></li><li class="cd-gap">+{{ form.gap_hours[0] }}小时</li>
      <li><b>② 所属单位安全员</b></li><li class="cd-gap">+{{ form.gap_hours[1] }}小时</li>
      <li><b>③ 港湾主管</b></li><li class="cd-gap">+{{ form.gap_hours[2] }}小时</li>
      <li class="cd-top"><b>④ 项目经理</b><small>只升级高风险隐患</small></li></ol></section>

  <section class="zb-card" v-if="rep"><div class="zb-ch"><ic n="send"></ic><h2>{{ rep.dry ? '预览结果（未发送）' : '本次运行' }}</h2><span class="grow"></span><span class="tiny muted">{{ fmtDT(rep.at) }}</span><button class="zb-link" @click="rep = null">收起</button></div>
    <div class="zb-body"><div class="small">{{ rep.summary }}</div>
    <zb-paged :items="repRows" :size="6" :m-size="4" :fit="120" cls="cd-rep"><template #default="{ item: s }">
      <div class="cd-sent" v-if="s.kind === 'sent'"><span class="chip blue">{{ s.stage_name }}</span> <b>{{ s.to.name }}</b><span class="muted tiny">（{{ s.to.role_name }}）</span>
        <span class="tiny"> · {{ s.items.map((x) => x.subject).slice(0, 3).join('；') }}<template v-if="s.items.length > 3"> 等 {{ s.items.length }} 件</template></span></div>
      <div class="cd-sent" v-else><span class="chip orange">延后</span> <b>{{ s.to.name }}</b> <span class="tiny muted">{{ s.why }} · {{ s.items.length }} 件</span></div></template></zb-paged>
    <details class="mt" v-if="rep.skipped.length"><summary class="small">未发送原因（{{ rep.skipped.length }} 件）</summary>
      <zb-paged :items="rep.skipped" :size="8" :fit="false"><template #default="{ item: s }"><div class="tiny cd-why"><b>{{ s.subject }}</b> <span class="muted">— {{ s.reason }}</span></div></template></zb-paged></details></div>
  </section>

  <section class="zb-card"><div class="zb-ch"><ic n="clock"></ic><h2>最近升级记录</h2><span class="zb-n">{{ log.length }}</span><span class="grow"></span><button class="zb-link" @click="load"><ic n="refresh"></ic>刷新</button></div>
    <div class="zb-body"><div class="cd-sum" v-if="log.length"><span>以下 {{ log.length }} 条中：</span><span>已处理 <b>{{ tally.handled }}</b></span><span>升级到港湾主管及以上 <b>{{ tally.up }}</b></span><span>无可提醒人员已顺延 <b>{{ tally.skipped }}</b></span></div>
    <zb-paged v-if="log.length" :items="log" :size="15" :m-size="5" :fit="200" tag="table" cls="t ra-rt cd-log">
      <template #head><thead><tr><th>时间</th><th>阶段</th><th>提醒了谁</th><th>事项</th><th>当时状态</th><th></th></tr></thead></template>
      <template #default="{ item: r }"><tr :class="{muted: r.status === 'skipped'}">
        <td class="nowrap tiny ra-rt-t">{{ fmtDT(r.at) }}</td>
        <td class="nowrap ra-rt-tag"><span :class="['chip', r.stage >= 3 ? 'orange' : 'blue']">{{ r.stage_name }}</span></td>
        <td class="small" data-l="提醒">{{ r.status === 'skipped' ? '无可提醒人员，已顺延' : r.to }}<span class="tiny muted" v-if="r.to_role"> {{ r.to_role }}</span></td>
        <td class="small ra-rt-h"><a v-if="r.link && r.link.startsWith('finding')" @click="open(r.link)">{{ r.subject }}</a><template v-else>{{ r.subject }}</template>
          <span class="tiny muted"> {{ r.area_name }}<template v-if="r.owner"> · 责任人 {{ r.owner }}</template></span></td>
        <td class="tiny" data-l="当时状态">{{ r.status_text }}</td>
        <td class="tiny nowrap ra-rt-tag"><span class="chip green" v-if="r.handled">已处理</span><span class="muted" v-else-if="r.via">{{ r.via === 'ai' ? 'AI 话术' : '模板话术' }}</span></td></tr></template></zb-paged>
    <div class="zb-empty" v-if="!log.length">暂无升级记录</div></div></section>
  </div><load-state v-else :loading="!err" :error="err" @retry="load"></load-state>`,
  data: () => ({ rules: null, form: null, sched: null, log: [], rep: null, busy: false, err: null }),
  computed: {
    // 只数本页列出的这批记录，不代表全部历史
    // 本次运行的发送与延后，并成一个列表分页
    repRows() { const r = this.rep; return r ? [...r.sent.map((x) => ({ ...x, kind: "sent" })), ...r.deferred.map((x) => ({ ...x, kind: "deferred" }))] : []; },
    tally() { const l = this.log; return { handled: l.filter((r) => r.handled).length, up: l.filter((r) => r.stage >= 3 && r.status !== "skipped").length, skipped: l.filter((r) => r.status === "skipped").length }; },
  },
  mounted() { this.load(); },
  methods: {
    fmtDT,
    async load() {
      this.err = null;
      try {
        const r = await GET("/api/nudge/rules");
        this.rules = r.rules; this.form = clone(r.rules); this.sched = { on: r.scheduler, note: r.scheduler_note };
        this.log = await GET("/api/nudge/log?limit=60");
      } catch (e) { this.err = e; }
    },
    async run(dry) {
      this.busy = true;
      try { this.rep = await POST("/api/nudge/run", { dry }); if (!dry) { toast("已运行，发送提醒 " + this.rep.sent.length + " 条"); this.log = await GET("/api/nudge/log?limit=60"); NUDGE.bump(); } }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
    open(link) { const [n, qs] = link.split("?"); const [, id] = (qs || "").split("="); go(n, id ? { id } : {}); },
  },
};

EXT.route("chase", ChaseDesk);

// ---------------------------------------------------------------------------- 催办规则（系统管理员“项目与规则”页底部；导航编排 10-08 起规则归管理员）
const NudgeRules = {
  // 只在“项目与规则”的“催办规则”分区显示（views.js 的 SETTINGS_UI 记录当前分区）
  template: `<div v-if="form && shown">
  <section class="zb-card ra-sec"><div class="zb-ch"><ic n="clock"></ic><h2>催办规则</h2><span class="grow"></span><span class="zb-src">工程主管、安全主管在“催办记录”里只读查看</span></div>
    <div class="zb-body"><div class="cd-form">
      <label class="cd-f ra-ck"><input type="checkbox" v-model="form.enabled"> 启用自动催办</label>
      <label class="cd-f">临期提前 <input class="in cd-n" type="number" min="0" max="7" v-model.number="form.lead_days"> 天提醒本人</label>
      <div class="cd-f">升级间隔（小时）
        <input class="in cd-n" type="number" min="1" v-model.number="form.gap_hours[0]"> →
        <input class="in cd-n" type="number" min="1" v-model.number="form.gap_hours[1]"> →
        <input class="in cd-n" type="number" min="1" v-model.number="form.gap_hours[2]"></div>
      <div class="cd-f">静默时段 <input class="in cd-t" type="time" v-model="form.quiet_start"> 至 <input class="in cd-t" type="time" v-model="form.quiet_end"> <span class="tiny muted">期间不发送，次日顺延；起止相同则不设</span></div>
      <label class="cd-f">每人每日上限 <input class="in cd-n" type="number" min="1" max="20" v-model.number="form.daily_cap"> 条</label>
      <div class="cd-f">催办范围
        <label class="ra-ck"><input type="checkbox" v-model="form.sources.findings"> 隐患与质控</label>
        <label class="ra-ck" v-if="'logs' in form.sources"><input type="checkbox" v-model="form.sources.logs"> 日志/周确认</label>
        <label class="ra-ck"><input type="checkbox" v-model="form.sources.actions"> 行动项</label></div></div>
    <div class="zb-btns mt"><button class="btn primary" :disabled="busy" @click="save">保存规则</button><button class="btn" @click="reset">恢复默认</button>
      <span class="tiny muted" v-if="sched && !sched.on">{{ sched.note }}</span></div></div></section>
  </div>`,
  data: () => ({ form: null, defaults: null, sched: null, busy: false }),
  computed: { shown: () => typeof SETTINGS_UI === "undefined" || SETTINGS_UI.sec === "nudge" },
  async mounted() {
    if (roleOf() !== "admin") return;
    try { const r = await GET("/api/nudge/rules"); this.defaults = r.defaults; this.form = clone(r.rules); this.sched = { on: r.scheduler, note: r.scheduler_note }; } catch (_) { /* 催办未启用等：不显示 */ }
  },
  methods: {
    reset() { this.form = clone(this.defaults); },
    async save() {
      this.busy = true;
      try { const r = await PUT("/api/nudge/rules", this.form); this.form = clone(r.rules); toast("催办规则已保存"); }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};
EXT.slot("bottom:settings", NudgeRules, { id: "nudge-rules" });

