/* x_ui.js：通用“AI 建议｜依据｜人工决定”核对表 ReviewTable，周进度确认、日志预审、材料小票识别三处共用。
   - 输入 rows：[{key, label, sub?, ai_text, ai_raw, evidence:[{type, ref, text, url?, thumb?, log_id?, route?}], flag?, note?,
                  numeric?, input?, placeholder?, unit?, pre?, opts?, optional?}]
     ai_text 是给人看的 AI 整理结果（空 = “AI 未给出”）；ai_raw 是 AI 原值（决定“确认”时采用它）；
     evidence 可点开：照片（放大）、日志（跳到该日志）、P6 作业/其他（文字）；flag 是需要人注意的提示（如与每日合计不一致）；
     opts 可限定该行可选的决定；optional=true 的行不计入“必须决定”（如 AI 没识别出的选填项）。
   - v-model 是 {key: {decision, final, reason}}：adopt 模式的决定为 确认 / 修改 / 不采纳 / 无法判断；
     verify 模式（日志预审）为 属实 / 误报（误报必填原因）。未决定的行显示“候选·待确认”，顶部显示“已决定 x/N 项”。
   - 辅助：reviewStatus() 统计已决定/剩余/问题；reviewPayload() 生成提交给后端的决定列表。
   - 窄屏（≤720px）每行折成一张卡片，不出现横向滚动。 */

const RT_SETS = {
  adopt: [["confirm", "确认"], ["modify", "修改"], ["reject", "不采纳"], ["unknown", "无法判断"]],
  verify: [["true", "属实"], ["false", "误报"]],
};
const RT_NAME = { confirm: "确认", modify: "修改", reject: "不采纳", unknown: "无法判断", true: "属实", false: "误报" };
const RT_CHIP = { confirm: "green", modify: "blue", reject: "", unknown: "orange", true: "orange", false: "" };
const RT_COLS = { item: "核对项", ai: "AI 整理", ev: "依据", dec: "人工决定" };

// 一行的问题（空字符串 = 没问题）：修改必须有值（数字行须为不小于 0 的数）；误报必须写原因
function reviewProblem(row, d) {
  if (!d || !d.decision) return "";
  if (d.decision === "modify") {
    const v = d.final;
    if (v === undefined || v === null || String(v).trim() === "") return "请填写修改后的值";
    if (row.numeric && (!Number.isFinite(+v) || +v < 0)) return "请填写不小于 0 的数字";
  }
  if (d.decision === "false" && !String(d.reason || "").trim()) return "误报需填写原因";
  return "";
}
// 统计：total=必须决定的行数（optional 行不计）；done=已决定且无问题；remaining=还差几项（含有问题的）
function reviewStatus(rows, dec, mode) {
  const set = RT_SETS[mode || "adopt"].map((x) => x[0]);
  let total = 0, done = 0, problems = 0;
  for (const r of rows) {
    const d = (dec || {})[r.key], ok = !!(d && set.includes(d.decision)) && !reviewProblem(r, d);
    if (!r.optional) total++;
    if (ok && !r.optional) done++;
    if (d && d.decision && reviewProblem(r, d)) problems++;
  }
  return { total, done, remaining: total - done, problems, complete: total === done && problems === 0 };
}
// 提交给后端的决定：[{key, decision, final?, reason?}]；数字行的最终值转成数字
function reviewPayload(rows, dec) {
  const out = [];
  for (const r of rows) {
    const d = (dec || {})[r.key];
    if (!d || !d.decision) continue;
    const o = { key: r.key, decision: d.decision };
    if (d.decision === "modify") o.final = r.numeric ? +d.final : String(d.final).trim();
    if (d.decision === "false") o.reason = String(d.reason || "").trim();
    out.push(o);
  }
  return out;
}
// 从后端已保存的决定（rows[].saved）还原本地决定表
function reviewFromSaved(rows) {
  const m = {};
  for (const r of rows) if (r.saved) m[r.key] = { decision: r.saved.decision, final: r.saved.final === null || r.saved.final === undefined ? "" : r.saved.final, reason: r.saved.reason || "" };
  return m;
}

const ReviewTable = {
  props: {
    title: String, rows: { type: Array, required: true }, modelValue: { type: Object, default: () => ({}) },
    mode: { type: String, default: "adopt" }, readonly: Boolean, bulk: Boolean, cols: { type: Object, default: () => RT_COLS }, hint: String,
  },
  emits: ["update:modelValue"],
  template: `<div :class="['rt', {ro: readonly}]">
    <div class="rt-head"><b v-if="title">{{ title }}</b>
      <span :class="['rt-count', {ok: st.complete}]">已决定 {{ st.done }}/{{ st.total }} 项</span>
      <span class="rt-bar" aria-hidden="true"><i :style="{width: pctDone + '%'}"></i></span><div class="grow"></div>
      <button class="btn ghost sm" v-if="bulk && !readonly && bulkable.length" @click="bulkConfirm">无存疑项一键确认（{{ bulkable.length }}）</button></div>
    <div class="tiny muted rt-hint" v-if="hint">{{ hint }}</div>
    <div class="rt-grid" role="table">
      <div class="rt-tr rt-th" role="row"><div role="columnheader">{{ cols.item }}</div><div role="columnheader">{{ cols.ai }}</div><div role="columnheader">{{ cols.ev }}</div><div role="columnheader">{{ cols.dec }}</div></div>
      <div v-for="r in rows" :key="r.key" :class="['rt-tr', rowCls(r)]" role="row">
        <div class="rt-c rt-item" role="cell"><b>{{ r.label }}</b><div class="tiny muted" v-if="r.sub">{{ r.sub }}</div>
          <span class="chip orange" v-if="!decided(r) && !r.optional && !readonly">候选·待确认</span>
          <span :class="['chip', RT_CHIP[cur(r).decision]]" v-else-if="cur(r).decision">{{ RT_NAME[cur(r).decision] }}</span></div>
        <div class="rt-c rt-ai" :data-l="cols.ai" role="cell">
          <div class="rt-aiv" v-if="r.ai_text !== null && r.ai_text !== undefined && r.ai_text !== ''">{{ r.ai_text }}</div><div class="rt-aiv muted" v-else>AI 未给出</div>
          <div class="rt-flag" v-if="r.flag"><ic n="alert"></ic>{{ r.flag }}</div><div class="tiny muted" v-if="r.note">{{ r.note }}</div></div>
        <div class="rt-c rt-ev" :data-l="cols.ev" role="cell">
          <button type="button" class="rt-evb" :aria-expanded="!!opened[r.key]" @click="opened[r.key] = !opened[r.key]">依据 {{ evs(r).length }} 条 {{ opened[r.key] ? '▴' : '▾' }}</button>
          <ul class="rt-evl" v-if="opened[r.key]"><li v-for="(e, i) in evs(r)" :key="i">
            <img v-if="e.thumb || e.url" class="rt-evimg" :src="e.thumb || e.url" alt="" @click="openEv(e)">
            <a v-if="clickable(e)" @click="openEv(e)">{{ e.text }}</a><span v-else>{{ e.text }}</span></li></ul></div>
        <div class="rt-c rt-dec" :data-l="cols.dec" role="cell">
          <template v-if="readonly"><span class="small">{{ decText(r) }}</span></template>
          <template v-else>
            <div class="rt-seg" role="group" :aria-label="r.label + ' 的人工决定'"><button type="button" v-for="o in optsOf(r)" :key="o[0]" :class="{on: cur(r).decision === o[0]}" @click="pick(r, o[0])">{{ o[1] }}</button></div>
            <input v-if="cur(r).decision === 'modify'" class="in rt-in" :type="r.input || (r.numeric ? 'number' : 'text')" :step="r.numeric ? 'any' : null" :min="r.numeric ? 0 : null" inputmode="decimal"
              :placeholder="r.placeholder || '修改后的值'" :value="cur(r).final" @input="setField(r, 'final', $event.target.value)" :aria-label="r.label + ' 修改后的值'">
            <input v-if="cur(r).decision === 'false'" class="in rt-in" type="text" maxlength="200" placeholder="误报原因（必填）" :value="cur(r).reason" @input="setField(r, 'reason', $event.target.value)" :aria-label="r.label + ' 误报原因'">
            <div class="tiny rt-warn" v-if="problemOf(r)">{{ problemOf(r) }}</div></template></div>
      </div></div>
    <div class="empty small" v-if="!rows.length">没有需要核对的项</div></div>`,
  data() { return { opened: {}, local: { ...(this.modelValue || {}) }, RT_NAME, RT_CHIP }; },
  watch: { modelValue(v) { this.local = { ...(v || {}) }; } },
  computed: {
    st() { return reviewStatus(this.rows, this.local, this.mode); },
    pctDone() { return this.st.total ? Math.round((this.st.done / this.st.total) * 100) : 100; },
    // 一键确认只作用于“AI 有值、无存疑提示、还没决定”的行；有提示的行必须人逐行看
    bulkable() { return this.rows.filter((r) => !r.optional && !this.decided(r) && !r.flag && r.ai_raw !== null && r.ai_raw !== undefined && (this.mode === "adopt")); },
  },
  methods: {
    evs(r) { return r.evidence || []; },
    cur(r) { return this.local[r.key] || {}; },
    decided(r) { const d = this.cur(r); return !!d.decision && !reviewProblem(r, d); },
    problemOf(r) { return reviewProblem(r, this.cur(r)); },
    // r.opts 可限定该行可选的决定；r.modifyLabel 可把“修改”改叫“填写”（AI 没识别出值时）
    optsOf(r) { const all = RT_SETS[this.mode].map((o) => (o[0] === "modify" && r.modifyLabel ? ["modify", r.modifyLabel] : o)); return r.opts ? all.filter((o) => r.opts.includes(o[0])) : all; },
    rowCls(r) { return { flag: !!r.flag, undecided: !this.decided(r) && !r.optional && !this.readonly, bad: !!this.problemOf(r) }; },
    emit(key, v) { this.local = { ...this.local, [key]: v }; this.$emit("update:modelValue", this.local); },  // 先改本地副本再通知父级：连续点击不会互相覆盖
    pick(r, k) {
      const c = this.cur(r);
      if (c.decision === k) return;
      this.emit(r.key, { decision: k, final: k === "modify" ? (c.final !== undefined && c.final !== "" ? c.final : (r.pre !== undefined ? r.pre : (r.ai_raw === null || r.ai_raw === undefined ? "" : r.ai_raw))) : "",
        reason: k === "false" ? (c.reason || "") : "" });
    },
    setField(r, f, v) { this.emit(r.key, { ...this.cur(r), [f]: v }); },
    bulkConfirm() { const m = { ...this.local }; for (const r of this.bulkable) m[r.key] = { decision: "confirm", final: "", reason: "" }; this.local = m; this.$emit("update:modelValue", m); },
    clickable(e) { return !!(e.log_id || e.route || e.url); },
    openEv(e) {
      if (e.log_id) go("log", { id: e.log_id });
      else if (e.route) go(e.route.name, e.route.params || {});
      else if (e.url) store.viewer = e.url;
    },
    decText(r) {
      const d = this.cur(r);
      if (!d.decision) return "未逐条核对";
      if (d.decision === "modify") return `${r.modifyLabel || "修改为"} ${d.final}${r.unit ? " " + r.unit : ""}`;
      if (d.decision === "false") return `误报：${d.reason || ""}`;
      return RT_NAME[d.decision];
    },
  },
};

/* ===================================================================== T08：页头 PageHead / 空状态 EmptyState / 边界说明 AiNote / 仿真角标 SimBadge
   - PageHead：标题 + 一句话目的（≤25 字大白话）+ 可选步骤条 + 右侧主操作；同一页面只用一个页头（TodoHome 的页头、各页 h1 都已换成它；hubbar 页签是导航，不是页头）。
     文案集中在 PAGE_HEADS，页面只写 page="键"；steps 用 STEP_FLOWS 里的流程名，current 由下面三个函数按真实状态算出，不写死。
   - EmptyState：原因 + 一个可执行按钮（action / actions[0]），actions[1] 作为文字链接备选；按钮要么跳到真能操作的页面（route，可带 focus 直达输入处），要么就地执行（fn）。
     当前岗位没有权限做的事不放按钮，只写原因和该找谁（只读岗位）。
   - AiNote：含 AI 输出的页面底部一行灰色边界说明，措辞按页面实际能力写，不夸大。
   - SimBadge：仿真项目统一角标“仿真项目 · 不计入应用成效”，只在侧栏项目名下显示一次（页头不再逐页重复）。 */

const PAGE_HEADS = {
  desk: ["", "先办理待办，再关注异常"],
  tech: ["随手记与待办", "说一句、拍一张，AI 帮你整理成日志"],
  shome: ["今日安全", "随手拍隐患，先处理待整改的事"],
  sysh: ["系统概况", "看 AI 是否连通、系统运行是否正常"],
  cockpit: ["项目总览", "一屏看进度、质量、安全现状"],
  week: ["本周进度确认", "核对本周完成量，提交后更新进度计划"],
  daylog: ["施工日志", "核对 AI 整理的日志，确认后提交"],
  log_edit: ["施工日志", "核对日志内容，确认后提交"],
  log_view: ["施工日志", "查看日志内容"],  // log_edit 页由非作者（审核人等）打开时用，见 PageHead.ph
  log_review: ["日志预审", "逐条核对检查结论，再通过或退回"],
  logs_tech: ["我的日志", "查看 AI 整理的日志和审核状态"],
  logs_eng: ["分包日志", "查看分包提交的日志和审核状态"],
  alerts: ["进度预警", "看哪些作业可能拖期，确认是否属实"],
  alert_detail: ["预警详情", "看依据和原因，确认是否属实"],
  forecast: ["预测与赶工", "估算各节点能否按期，给出赶工建议"],
  plan: ["P6 计划", "导入最新 P6 计划，对照日志进度"],
  planck: ["计划体检", "检查 P6 计划的逻辑与工效，对比两期更新"],
  output: ["产值", "按完成量估算产值，非计量结算金额"],
  safety: ["安全隐患", "先确认 AI 候选，再跟踪整改闭环"],
  ledger: ["隐患台账", "本单位隐患：待整改、待复核、已闭环"],
  quality: ["质量", "跟踪质控提醒和质量问题，按时闭环"],
  finding: ["事项详情", "看依据和进展，按流程处理"],
  tasks: ["我的待办", "处理派给你的整改和质控"],
  gate: ["开工条件", "开工前逐项核对条件，缺项提醒到人"],
  reports: ["文书", "对外报告、通知函、联系单：AI 起草，人确认"],
  rpt: ["报表", "日报、周报、月报：数字取自系统，人核对后发布"],
  letters: ["致分包文书", "项目部下发的工作联系单和函件：签收、回复、销项"],
  // RJ 10-10 工笔文书分四区：报表（rpt）| 函件 | 例会 | 分析
  gb_letter: ["函件", "发给监理或分包单位：AI 起草，人确认后发出"],
  gb_meet: ["例会", "例会底稿、三周计划与行动项核对"],
  gb_ana: ["分析", "延误事件与月度复盘：AI 起草，人确认"],
  report: ["报表", "核对数字和文字，审核后发布"],
  ask: ["问工笔", "用一句话查记录，回答附出处"],
  agents: ["智能体", "看各智能体做了什么，点开任一个向它提问"],
  metrics: ["应用成效", "看耗时和采纳情况，样本不足不下结论"],
  backtest: ["历史回测", "用多期 P6 实绩检验预警规则，由人选定阈值"],
  users: ["账号与权限", "新增账号、分配岗位，查看权限矩阵"],
  audit: ["AI 与审计", "查看谁做了什么，以及 AI 调用记录"],
  notices: ["消息", "预警、派单、催办的通知都在这里"],
  settings_admin: ["项目与规则", "管理 AI 连接、增强能力、模板和阈值"],
  settings_user: ["设置", "查看 AI 连接状态，修改自己的密码"],
  aimgr: ["模型与额度", "管理 Key 池、各用途模型与额度"],
  meetings: ["例会行动项", "行动项逐条确认，下次例会前核对闭环"],
  conceal: ["隐蔽验收", "照片归到必拍项，找缺拍，确认后报验"],
  chase: ["催办记录", "催办Agent 逐级提醒了谁、处理了没有"],
  qrcodes: ["扫码上报", "生成二维码，班组扫码免登录上报隐患"],
  tickets: ["材料与砼", "拍小票自动识别，人核对后才入账"],
  circle: ["现场圈", "现场发布自动汇集，可点赞评论"],
  sites: ["部位档案", "按部位汇集照片、工程量、质控、隐患"],
  claims: ["索赔证据包", "整理停工等事件的工期与费用证据"],
};
const STEP_FLOWS = {
  week: ["汇总", "核对", "提交", "项目部核定"],
  hazard: ["候选", "确认派单", "整改", "复查闭环"],
  report: ["起草", "数字守卫检查", "审核", "发布"],
};
// 各页底部的边界说明：按页面实际能力写，只说“AI 做了什么、谁来定”，不夸大
const AI_NOTES = {
  desk: "AI 只整理线索与依据，办理结论由人员确认。",
  tech: "AI 只整理你说的和拍的，日志与进度由你确认后才提交。",
  shome: "AI 只识别照片并给出建议，隐患由安全人员确认后才派单。",
  tasks: "事项里有 AI 识别的隐患和 AI 生成的质控点，整改是否到位由安全、质量人员确认。",
  cockpit: "今日要点由简报智能体汇总，只写进度、质量、安全；数字取自系统统计。",
  week: "AI 汇总来自每日日志，数量以你逐项核对为准；提交后由项目部核定。",
  daylog: "日志由 AI 根据口述、照片和 P6 计划整理，提交前请自己核对。",
  log_review: "检查结论由 AI 给出，属实还是误报由你决定。",
  alerts: "预警由进度规则计算、AI 分析原因，是否属实由人员确认。",
  forecast: "预测与赶工方案是按已报数据的推算和 AI 建议，不会改动 P6 计划，是否采用由人决定。",
  planck: "检查项与数字由系统按 P6 导入数据计算，诊断建议由 AI 撰写；不会改动 P6 计划，是否调整由计划人员决定。",
  output: "产值是内部统计（非计量认证）；清单映射由 AI 建议，确认后才计入。",
  safety: "AI 识别的隐患先作候选，人确认后才派单；整改是否消除由安全人员确认。",
  quality: "质控点由 AI 按工序生成，是否执行、验收是否合格由人员确认。",
  gate: "放行智能体只列条件、核对数据并提醒，是否具备由有权者确认。",
  reports: "报表由 AI 按模板起草，数字取自系统统计；发布前须人员审核。",
  ask: "回答只依据系统里的记录并标出处，不替代人的判断。",
  agents: "各智能体只起草、发现和提醒，处置由人员确认。",
  metrics: "数字均来自系统记录，口径见各卡的“口径说明”；样本不足时不下结论，也不计算节省比例。",
  backtest: "回测是确定性计算，不调用 AI，数据不出本机；检验的是判定规则在 P6 实绩上的表现，不检验随手记的提取；阈值由工程主管选定。",
};

// 页面里“哪一步在进行”：都由真实状态算出。返回当前步骤序号（0 起）；等于步骤总数表示全部完成；-1 表示不适用
function weekStep(log, remaining) {
  if (!log) return 0;
  if (log.status === "approved") return 4;
  if (log.status === "submitted") return 3;
  if (!log.has_ai_draft) return 0;
  const rows = (log.week_review && log.week_review.rows) || [];
  return rows.length && remaining > 0 ? 1 : 2;
}
function hazardStep(f) {
  if (!f || f.status === "dismissed") return -1;
  if (f.candidate) return 1;
  if (f.status === "resolved") return 4;
  if (f.status === "rectifying") return 3;
  return f.assignee_id ? 2 : 1;
}
// 隐患列表页：各阶段的真实数量，当前步骤 = 第一个有数量的阶段
function hazardCounts(active, candidates) {
  const c = [candidates || 0, 0, 0, 0];
  for (const f of active || []) {
    if (f.candidate) continue;
    if (f.status === "rectifying") c[3]++;
    else if (["open", "rejected"].includes(f.status)) c[f.assignee_id ? 2 : 1]++;
  }
  return c;
}
function reportStep(r, issues) {
  if (!r) return 0;
  if (r.status === "published") return 4;
  return issues > 0 ? 1 : 2;
}

// 让某个输入/按钮就位：目标可能还没渲染（刚跳转过来），最多等约 3 秒
function focusSoon(id, tries = 25) {
  const el = document.getElementById(id);
  if (el) {
    if (el.tagName === "INPUT" && el.type === "file") { el.click(); return; }  // 文件框只聚焦不会弹出选择（U-05③），直接打开
    if (el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "center" });
    if (el.focus) setTimeout(() => el.focus({ preventScroll: true }), 220);
    return;
  }
  if (tries > 0) setTimeout(() => focusSoon(id, tries - 1), 120);
}
// 空状态/待办按钮的统一执行：fn 就地执行；route（如 "progress?tab=plan"）跳转；focus（短名→todo-focus-xxx）或 id 直达输入处
function runAction(a) {
  if (!a) return;
  if (typeof a.fn === "function") return a.fn();
  const fid = a.id || (a.focus ? "todo-focus-" + a.focus : "");
  if (a.route) {
    const [name, qs] = String(a.route).split("?"), params = { ...(a.params || {}) };
    (qs || "").split("&").filter(Boolean).forEach((p) => { const [k, v] = p.split("="); params[k] = decodeURIComponent(v || ""); });
    go(name, params);
    if (fid) focusSoon(fid);
    return;
  }
  if (fid) focusSoon(fid);
}

const SimBadge = { template: `<span class="sim-badge" title="仿真数据，仅用于演示">仿真项目 · 不计入应用成效</span>` };

// T-07 演示日标识：演示实例（tools/demo_serve.py）的服务器时钟定在演示日，页头标“演示日 MM-DD”；普通实例的演示时钟接口返回 demo=false，不显示。
// 一页里可能不止一个页头组件，只由最先挂载、仍在页面上的那个显示，保证全页只出现一次。
let DEMO_DAY = null, demoSeq = 0;
function demoDay() {
  if (!DEMO_DAY) {
    DEMO_DAY = Vue.reactive({ day: "", heads: [] });
    fetch("/api/demo/clock", { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d && d.demo) DEMO_DAY.day = d.today || d.day || ""; }).catch(() => {});
  }
  return DEMO_DAY;
}

// 统一的阅读提示（RJ 10-09：操作提示不常驻页面）：一个小问号，鼠标停留或键盘聚焦时显示，手机上点一下显示、再点或点别处收起。
// 用法：<help-tip text="一句话说明"></help-tip>，或用插槽写多行；label 是读屏用的名称；caption 在问号旁显示一个短词（如“说明”）。提示框靠近右边缘时自动向左展开，不出屏。
let HELP_OPEN = null;
const HelpTip = {
  props: { text: String, label: { type: String, default: "说明" }, caption: String },   // caption：问号旁的一个短词（整行说明收起后留的入口）
  template: `<span :class="['htip', {open: open, r: right}]" @mouseenter="place" @focusin="place" @keydown.esc="open = false">
    <button type="button" class="htip-b" :aria-label="label" :aria-expanded="open ? 'true' : 'false'" @click.stop="toggle">?</button><span class="htip-c" v-if="caption" @click.stop="toggle">{{ caption }}</span>
    <span class="htip-pop" role="tooltip" @click.stop><template v-if="text">{{ text }}</template><slot></slot></span></span>`,
  data: () => ({ open: false, right: false }),
  unmounted() { if (HELP_OPEN === this) HELP_OPEN = null; document.removeEventListener("click", this.shut); },
  methods: {
    place() { const r = this.$el.getBoundingClientRect(); this.right = r.left + 300 > window.innerWidth - 8; },
    toggle() {
      this.place(); this.open = !this.open;
      if (this.open) { if (HELP_OPEN && HELP_OPEN !== this) HELP_OPEN.shut(); HELP_OPEN = this; document.addEventListener("click", this.shut); }
      else document.removeEventListener("click", this.shut);
    },
    shut() { this.open = false; document.removeEventListener("click", this.shut); },
  },
};

// V8 页头分级（10-10 其余页面改版）：各业务线的列表、台账、配置页用中港蓝短横幅（ph-band，比首页 zb-hero 矮），
// 详情页（事项、预警、日志、报告）只用紧凑标题（ph-plain）。首页、产值页、文书页自带 zb-hero，页头嵌在横幅里时不再加横幅。
const PH_BAND_ROUTES = ["circle", "safety", "quality", "subs", "qrcodes", "ledger", "progress", "planck", "gate", "backtest", "rpt", "logs", "week", "reports", "letters",
  "meetings", "conceal", "tickets", "sites", "claims", "contract", "xlsx", "agents", "chase", "aimgr", "audit", "users", "settings", "notices", "tasks"];
const PH_HOME_ROUTES = ["home", "snap", "shome", "work", "eng", "sdash", "sysh", "cockpit"];   // 各岗位首页：保留实景横幅
const PH_PLAIN_ROUTES = ["finding", "alert", "log", "report", "manual", "capture"];
const PH_NEST = ".zb-hero, .home-hero, .cp-hero, .m-hero, .home-head, .rv-embed, .gb-detail, .modal, .ph-band";
const PageHead = {
  components: { SimBadge, HelpTip },
  props: { page: String, title: String, purpose: String, steps: String, current: { type: Number, default: -1 }, counts: Array },
  // 页面用途不再常驻一行，收进标题旁的小问号（RJ 10-09）
  template: `<header :class="['ph', {'ph-band': kind === 'band', 'ph-plain': kind === 'plain'}]">
    <div class="ph-row"><div class="ph-main"><div class="ph-title"><h1>{{ ttl }}</h1><help-tip v-if="pur || note" label="这一页做什么"><template v-if="pur">{{ pur }}</template><span class="htip-note" v-if="note">{{ note }}</span></help-tip><slot name="badge"></slot><span class="chip blue" v-if="demoTag" title="演示实例：服务器时钟定在演示日，数据是演示副本">{{ demoTag }}</span></div>
        <div class="ph-meta"><slot name="meta"></slot></div></div>
      <div class="ph-act"><slot name="actions"></slot></div></div>
    <ol class="ph-steps" v-if="list.length" :aria-label="'流程：' + list.join('，')">
      <li v-for="(s, i) in list" :key="s" :class="{done: i < current, cur: i === current}" :aria-current="i === current ? 'step' : null">
        <span class="n"><ic n="check" v-if="i < current"></ic><template v-else>{{ i + 1 }}</template></span>{{ s }}<b class="ph-cnt" v-if="counts && counts[i]">{{ counts[i] }}</b></li></ol></header>`,
  data: () => ({ nested: true }),
  created() { this.demo = demoDay(); this.demoId = ++demoSeq; },
  mounted() { this.demo.heads.push(this.demoId); const p = this.$el && this.$el.parentElement; this.nested = !!(p && p.closest && p.closest(PH_NEST)); },
  unmounted() { const i = this.demo.heads.indexOf(this.demoId); if (i >= 0) this.demo.heads.splice(i, 1); },
  computed: {
    // 日志页（log_edit）只有作者（技术员）是“确认后提交”，审核人等打开时换成查看视角（F-18②）
    ph() { return PAGE_HEADS[this.page === "log_edit" && typeof isTech === "function" && !isTech() ? "log_view" : this.page] || ["", ""]; },
    ttl() { return this.title || this.ph[0]; },
    pur() { return this.purpose !== undefined ? this.purpose : this.ph[1]; },
    list() { return STEP_FLOWS[this.steps] || []; },
    // 本页 <ai-note> 登记的说明：只由最先挂载的那个页头显示（与演示日标识同一规则）
    note() { const s = this.demo; return s && s.heads[0] === this.demoId ? PAGE_NOTE.list.map((x) => x.text).filter(Boolean).join(" ") : ""; },
    demoTag() { const s = this.demo; return s && s.day && s.heads[0] === this.demoId ? "演示日 " + s.day.slice(5) : ""; },
    kind() {
      if (this.nested) return "";
      const n = (typeof store !== "undefined" && store.route && store.route.name) || "";
      // RJ 10-10：实景大横幅只留各岗位首页；其余页面一律紧凑页头（ph-band 现为一行紧凑样式，见 x_v8.css 末尾）
      return PH_HOME_ROUTES.includes(n) ? "" : PH_PLAIN_ROUTES.includes(n) ? "plain" : "band";
    },
  },
};

// 现场证据照片（10-10 统一）：横向 4:3 完整显示（contain，不拉伸不裁），点开看原图；文件缺失显示“缺文件”，不出破图。
// 用法：<ev-photo :src="缩略图" :full="原图" label="整改前" time="上传时间" size="s|m|l|f"></ev-photo>；size 只定宽度（f 为占满容器），比例恒为 4:3。
const EvPhoto = {
  props: { src: String, full: String, label: String, time: String, size: { type: String, default: "m" }, alt: { type: String, default: "现场照片" } },
  template: `<figure :class="['ev-ph', 'ev-' + size]"><button type="button" class="ev-ph-b" :title="bad || !src ? '' : '点开看原图'" :disabled="bad || !src" @click.stop="open">
      <img v-if="src && !bad" :src="src" :alt="alt" loading="lazy" @error="bad = true"><span v-else class="ev-ph-miss">{{ src ? '缺文件' : '无照片' }}</span>
      <span class="ev-ph-tag" v-if="label">{{ label }}</span></button>
    <figcaption v-if="time || $slots.default"><template v-if="time">{{ time }}</template><slot></slot></figcaption></figure>`,
  data: () => ({ bad: false }),
  watch: { src() { this.bad = false; } },
  methods: { open() { if (this.src && !this.bad) store.viewer = this.full || this.src; } },
};

const EmptyState = {
  props: { title: String, reason: String, action: Object, alt: Object, actions: Array, compact: Boolean },
  template: `<div :class="['es', {'es-c': compact}]" role="status"><div class="es-t" v-if="title">{{ title }}</div><div class="es-r">{{ reason }}</div>
    <div class="es-a" v-if="main || sub"><button type="button" :class="['btn', 'primary', {sm: compact}]" v-if="main" @click="run(main)">{{ main.label }}</button>
      <a class="small" v-if="sub" @click="run(sub)">{{ sub.label }}</a></div></div>`,
  computed: {
    main() { return this.action || (this.actions && this.actions[0]) || null; },
    sub() { return this.alt || (this.actions && this.actions[1]) || null; },
  },
  methods: { run: runAction },
};

// 只给某岗位的说明（覆盖 AI_NOTES 同名键）：数据缺口归工程主管管，“没报数据不等于落后”只对他说；
// 项目经理等岗位的说明不出现“没报、填报”类字样（AGENTS.md），与预警页正文的 seesGaps 同一口径
const AI_NOTES_BY_ROLE = {
  eng_sup: { alerts: "预警由进度规则计算、AI 分析原因，是否属实由人员确认；没报数据不等于落后。" },
};

// 页面底部的边界/口径说明不再占正文行高（RJ 10-10）：<ai-note> 本身不渲染文字，只把说明登记给本页的“?”——
// 有页头（PageHead）时并进页头标题旁的同一个问号；没有页头的页面（各岗位首页等）由顶栏的“本页说明”问号显示。每页至多一个入口。
const PAGE_NOTE = typeof Vue !== "undefined" ? Vue.reactive({ list: [] }) : { list: [] };   // 测试桩里没有 Vue
const AiNote = {
  props: { page: String, text: String },
  template: `<span class="ai-note" hidden></span>`,
  data: () => ({ notes: AI_NOTES }),
  mounted() { PAGE_NOTE.list.push({ text: "" }); this.reg = PAGE_NOTE.list[PAGE_NOTE.list.length - 1]; this.reg.text = this.note; },
  updated() { if (this.reg) this.reg.text = this.note; },
  watch: { note(v) { if (this.reg) this.reg.text = v; } },
  unmounted() { const i = PAGE_NOTE.list.indexOf(this.reg); if (i >= 0) PAGE_NOTE.list.splice(i, 1); },
  computed: {
    note() {
      const role = typeof roleOf === "function" ? roleOf() : "";
      return this.text || ((AI_NOTES_BY_ROLE[role] || {})[this.page]) || this.notes[this.page] || "";
    },
  },
};

/* ======================================================================
   全站一屏化共享件（RJ 2026-10-10）：电脑端页面本身不出现纵向滚动，长列表/表格在固定高度面板内滚动，
   被略去的条目和全文进抽屉或下一层页面；手机关键信息在首屏，页面最多约 2 屏。各页面只用这几件，不各写一套。
   - v-fit：放在“会变长”的面板主体上（表格外层、列表、正文）。电脑端（宽 ≥ 861）按页面实际超出视口的量给它设
     max-height、面板内滚动（表头吸顶）；页面放得下时不设限。同一页多个 v-fit 按各自内容高度分配剩余高度
     （同一行并排的算一组）；可写 v-fit="200" 指定最小高度（默认 140）。手机不生效。
   - <zb-drawer title @close>：右侧抽屉（手机为底部抽屉），Esc/点遮罩关闭；插槽 head、默认、foot；wide 为宽抽屉。
   - <zb-clamp :text lines title>：正文最多 lines 行（默认 2），超出才出现“全文”，点开在抽屉里看全文；
     也可用默认插槽放富文本、full 插槽放抽屉里的完整内容。
   - <zb-paged :items size :m-size>：纯罗列列表的内部分页（见文件末尾）；<zb-more> 只用于“前几条 + 其余进抽屉”的摘要块。
   - <zb-more :items limit :m-limit title unit>：只显示前 limit 条（手机 m-limit 条），其余“查看全部 N 条”进抽屉（抽屉里每页 12 条分页），
     作用域插槽 #default="{ item, i, full }" 渲染每条（full 为 true 表示在抽屉里）。
   ====================================================================== */
const FIT = { els: new Set(), raf: 0, ro: null, mains: new WeakSet() };
function fitSchedule() { if (!FIT.raf) FIT.raf = requestAnimationFrame(() => { FIT.raf = 0; fitRun(); }); }
// 块的下外边距：父块没有下内边距/边框时，最后一个子块的下外边距会穿透出来（外边距折叠），取整条链上的最大值
function fitTailMargin(el, cs) {
  let m = parseFloat(cs.marginBottom) || 0;
  for (let x = el, xs = cs; !(parseFloat(xs.paddingBottom) || parseFloat(xs.borderBottomWidth)) && xs.display === "block" && xs.overflow === "visible";) {
    let k = x.lastElementChild, ks = null;   // 最后一个参与排版的子块（跳过隐藏的、绝对定位的）
    for (; k; k = k.previousElementSibling) { ks = getComputedStyle(k); if (ks.display !== "none" && ks.position !== "absolute" && ks.position !== "fixed") break; }
    if (!k) break;
    m = Math.max(m, parseFloat(ks.marginBottom) || 0); x = k; xs = ks;
  }
  return m;
}
// 页面内容的自然底边：main 里最后一块的底边 + main 的下内边距（main 在纵向弹性布局里会被撑高，不能直接用它的高度）
function fitContentBottom() {
  const main = document.querySelector("main.main"); if (!main) return document.documentElement.scrollHeight;
  let b = main.getBoundingClientRect().top;
  for (const c of main.children) { const cs = getComputedStyle(c); if (cs.display === "none") continue; b = Math.max(b, c.getBoundingClientRect().bottom + fitTailMargin(c, cs)); }   // 零高的插槽块也算（它们的外边距同样占位）
  return b + scrollY + (parseFloat(getComputedStyle(main).paddingBottom) || 0);
}
function fitRun() {
  const els = [...FIT.els].filter((el) => el.isConnected);
  if (window.innerWidth < 861) { for (const el of els) { el.style.maxHeight = ""; el.classList.remove("zb-fitted"); } return; }
  if (!els.length) return;
  // 纵向范围有重叠的面板（并排两栏，哪怕起点高低不同）算同一组：组的高度按“最低底边”算，组内每块的上限 = 组上限 − 它比组顶低出的距离
  const items = els.map((el) => { const r = el.getBoundingClientRect(); return { el, top: r.top + scrollY, h: el.offsetHeight, need: Math.max(el.scrollHeight, el.offsetHeight), min: el._fitMin || 140 }; }).sort((a, b) => a.top - b.top);
  const groups = [];
  for (const it of items) {
    const g = groups.find((x) => it.top < x.bot - 4 && it.top + it.h > x.top + 4);
    if (g) { g.items.push(it); g.bot = Math.max(g.bot, it.top + it.h); }
    else groups.push({ top: it.top, bot: it.top + it.h, items: [it] });
  }
  for (const g of groups) {
    g.h = g.bot - g.top;
    g.need = Math.max(...g.items.map((it) => it.top - g.top + it.need));
    g.min = Math.max(...g.items.map((it) => it.top - g.top + it.min));
  }
  const other = fitContentBottom() - groups.reduce((s, g) => s + g.h, 0);
  const avail = window.innerHeight - other - 2;
  const need = groups.reduce((s, g) => s + g.need, 0);
  // 按“注水”分配：需要少于份额的组拿足所需，最小高度高于份额的组拿最小高度，余下的再按需要量分给其他组
  if (need > avail) {
    let left = avail, rest = groups.slice();
    for (let changed = true; changed && rest.length;) {
      changed = false; const tot = rest.reduce((s, g) => s + g.need, 0);
      for (const g of rest.slice()) {
        const share = left * g.need / tot;
        if (g.need <= share || g.min >= share) { g.cap = g.need <= share ? g.need : g.min; left -= g.cap; rest.splice(rest.indexOf(g), 1); changed = true; }
      }
    }
    const tot = rest.reduce((s, g) => s + g.need, 0);
    for (const g of rest) g.cap = Math.max(g.min, Math.floor(left * g.need / tot));
  }
  for (const g of groups) {
    const cap = need <= avail ? 0 : g.cap;
    for (const it of g.items) {
      const c = cap ? Math.max(it.min, cap - (it.top - g.top)) : 0;
      const v = c && it.need > c ? c + "px" : "";
      if (it.el.style.maxHeight !== v) it.el.style.maxHeight = v;
      it.el.classList.toggle("zb-fitted", !!v);
    }
  }
}
const FitDirective = {
  mounted(el, b) {
    if (b.value === false) return;   // v-fit="false"：不参与（如 <zb-paged :fit="false">）
    el._fitMin = +b.value || 0; FIT.els.add(el);
    if (!FIT.ro) { FIT.ro = new ResizeObserver(fitSchedule); window.addEventListener("resize", fitSchedule); }
    FIT.ro.observe(el); if (el.firstElementChild) FIT.ro.observe(el.firstElementChild);
    const main = el.closest("main"); if (main && !FIT.mains.has(main)) { FIT.mains.add(main); FIT.ro.observe(main); for (const c of main.children) FIT.ro.observe(c); }
    fitSchedule(); setTimeout(fitSchedule, 400);
  },
  updated() { fitSchedule(); },
  unmounted(el) { FIT.els.delete(el); if (FIT.ro) FIT.ro.unobserve(el); },
};

let DRAWER_OPEN = 0;
const ZbDrawer = {
  props: { title: String, wide: Boolean },
  emits: ["close"],
  template: `<teleport to="body"><div class="zb-dw" @keydown.esc.stop="$emit('close')"><div class="zb-dw-mask" @click="$emit('close')"></div>
    <aside :class="['zb-dw-p', {wide}]" role="dialog" aria-modal="true" :aria-label="title" tabindex="-1" ref="p">
      <header class="zb-dw-h"><b>{{ title }}</b><slot name="head"></slot><button type="button" class="zb-dw-x" aria-label="关闭" @click="$emit('close')"><ic n="x"></ic></button></header>
      <div class="zb-dw-b"><slot></slot></div><footer class="zb-dw-f" v-if="$slots.foot"><slot name="foot"></slot></footer></aside></div></teleport>`,
  mounted() { DRAWER_OPEN++; document.documentElement.classList.add("zb-dw-on"); this.$nextTick(() => this.$refs.p && this.$refs.p.focus()); },
  unmounted() { if (--DRAWER_OPEN <= 0) { DRAWER_OPEN = 0; document.documentElement.classList.remove("zb-dw-on"); } },
};

const ZbClamp = {
  components: { ZbDrawer },
  props: { text: String, lines: { type: Number, default: 2 }, title: { type: String, default: "全文" }, pre: Boolean, more: { type: String, default: "全文" } },
  template: `<div class="zb-clamp"><div ref="t" :class="['zb-clamp-t', {pre}]" :style="{ WebkitLineClamp: lines }"><slot>{{ text }}</slot></div>
    <button v-if="over" type="button" class="zb-link zb-clamp-more" @click.stop="open = true">{{ more }}<ic n="chevron"></ic></button>
    <zb-drawer v-if="open" :title="title" @close="open = false"><div :class="['zb-read', {pre}]"><slot name="full"><slot>{{ text }}</slot></slot></div></zb-drawer></div>`,
  data: () => ({ over: false, open: false }),
  mounted() { this.check(); this.ro = new ResizeObserver(() => this.check()); this.ro.observe(this.$refs.t); },
  updated() { this.check(); },
  unmounted() { if (this.ro) this.ro.disconnect(); },
  methods: { check() { const t = this.$refs.t; if (!t) return; const o = t.scrollHeight > t.clientHeight + 2; if (o !== this.over) this.over = o; } },
};

const ZbMore = {
  components: { ZbDrawer },
  props: { items: { type: Array, default: () => [] }, limit: { type: Number, default: 5 }, mLimit: Number, title: { type: String, default: "全部" }, unit: { type: String, default: "条" }, tag: { type: String, default: "div" }, cls: String },
  template: `<component :is="tag" :class="['zb-more', cls]"><template v-for="(item, i) in shown" :key="key(item, i)"><slot :item="item" :i="i" :full="false"></slot></template></component>
    <button v-if="items.length > n" type="button" class="zb-link zb-more-all" @click="open = true">查看全部 {{ items.length }} {{ unit }}<ic n="chevron"></ic></button>
    <zb-drawer v-if="open" :title="title + ' · ' + items.length + ' ' + unit" @close="open = false"><zb-paged :items="items" :size="12" :tag="tag" :cls="['zb-more', 'full', cls].filter(Boolean).join(' ')" :unit="unit" :fit="false"><template #default="{ item, i }"><slot :item="item" :i="i" :full="true"></slot></template></zb-paged></zb-drawer>`,
  data: () => ({ open: false, narrow: window.innerWidth < 861 }),
  mounted() { this.onR = () => { this.narrow = window.innerWidth < 861; }; window.addEventListener("resize", this.onR); },
  unmounted() { window.removeEventListener("resize", this.onR); },
  computed: {
    n() { return this.narrow && this.mLimit ? this.mLimit : this.limit; },
    shown() { return this.items.slice(0, this.n); },
  },
  methods: { key(item, i) { return item && typeof item === "object" && item.id != null ? item.id : i; } },
};

/* <zb-paged>：纯罗列的列表（预警、隐患、待办、催办记录、台账、消息、记录流……）统一内部分页（RJ 10-10：只能罗列的文字列表必须分页，不靠拉长页面）。
   每页固定条数（size，手机 m-size），底部一行“共 N 条 · 上一页 1 2 … 下一页”，手机简化为“上一页 2/5 下一页”；
   items 变了：当前页第一条（按 id）还在就停在它所在的页（刷新、就地处理），否则（筛选、搜索）回第 1 页。电脑端当前页列表自带 v-fit（页码条始终在屏内；一页放不下时页内滚动），:fit="false" 关闭。作用域插槽 #default="{ item, i }"（i 为全表序号）；
   表格用 tag="table"（cls="t" 等），#head 插槽放 <thead>，默认插槽渲染 <tr>；#empty 插槽放空态。 */
const ZbPaged = {
  props: { items: { type: Array, default: () => [] }, size: { type: Number, default: 10 }, mSize: Number, tag: { type: String, default: "div" }, cls: String, unit: { type: String, default: "条" }, fit: { type: [Number, Boolean], default: 120 } },
  template: `<div class="zb-paged"><div class="zb-pg-body" v-fit="fit"><component :is="tag" :class="['zb-pg-list', cls]"><slot name="head"></slot>
      <tbody v-if="tag === 'table'"><template v-for="(item, i) in shown" :key="key(item, start + i)"><slot :item="item" :i="start + i"></slot></template></tbody>
      <template v-else v-for="(item, i) in shown" :key="key(item, start + i)"><slot :item="item" :i="start + i"></slot></template></component></div>
    <slot name="empty" v-if="!items.length"></slot>
    <nav class="zb-pg" v-if="pages > 1" aria-label="分页"><span class="zb-pg-n">共 {{ items.length }} {{ unit }}</span><span class="grow"></span>
      <button type="button" class="zb-pg-b prev" :disabled="p <= 1" @click="go(p - 1)" aria-label="上一页"><ic n="chevron"></ic><span v-if="narrow">上一页</span></button>
      <template v-if="!narrow"><template v-for="(x, k) in nums" :key="k"><span class="zb-pg-gap" v-if="x === 0">…</span><button v-else type="button" :class="['zb-pg-b', {on: x === p}]" :aria-current="x === p ? 'page' : null" @click="go(x)">{{ x }}</button></template></template>
      <span class="zb-pg-c" v-else>{{ p }} / {{ pages }}</span>
      <button type="button" class="zb-pg-b next" :disabled="p >= pages" @click="go(p + 1)" aria-label="下一页"><span v-if="narrow">下一页</span><ic n="chevron"></ic></button></nav></div>`,
  data: () => ({ p: 1, narrow: window.innerWidth < 861 }),
  mounted() { this.onR = () => { this.narrow = window.innerWidth < 861; }; window.addEventListener("resize", this.onR); },
  unmounted() { window.removeEventListener("resize", this.onR); },
  // 换了一批数据：当前页第一条还在就翻到它所在的页（刷新、就地处理后不跳回第 1 页），否则（筛选、搜索）回第 1 页
  watch: {
    items(now, before) {
      const first = (before || [])[this.start]; const k = first == null ? null : this.key(first, -1);
      const at = k == null || k === -1 ? -1 : now.findIndex((x, i) => this.key(x, i) === k);
      this.p = at >= 0 ? Math.floor(at / this.n) + 1 : 1;
    },
    pages(n) { if (this.p > n) this.p = Math.max(1, n); },
  },
  computed: {
    n() { return this.narrow && this.mSize ? this.mSize : this.size; },
    pages() { return Math.max(1, Math.ceil(this.items.length / this.n)); },
    start() { return (this.p - 1) * this.n; },
    shown() { return this.items.slice(this.start, this.start + this.n); },
    // 页码：最多 7 格，首尾常驻，中间用 … 省略（0 表示省略号）
    nums() {
      const t = this.pages, p = this.p;
      if (t <= 7) return Array.from({ length: t }, (_, i) => i + 1);
      const a = [1]; const lo = Math.max(2, Math.min(p - 1, t - 4)), hi = Math.min(t - 1, Math.max(p + 1, 5));
      if (lo > 2) a.push(0); for (let x = lo; x <= hi; x++) a.push(x); if (hi < t - 1) a.push(0); a.push(t); return a;
    },
  },
  methods: { go(x) { this.p = Math.min(Math.max(1, x), this.pages); }, key(item, i) { return item && typeof item === "object" && item.id != null ? item.id : i; } },
};
