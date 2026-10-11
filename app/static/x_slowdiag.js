/* x_slowdiag：预警详情里的“慢在哪”（2026-10-08 增补能力 4）。
   程序先按近 14 天挂接该作业的日志统计停工、设备、材料、人员、天气、图纸、待验收信号（天数 + 日志编号），
   进度智能体只写一句归因，日志编号与数字逐一核对，不过或模型不可用显示系统统计句。
   10-10 改版：zb-card；一屏化后放在预警详情右栏，归因句至多 3 行、全文进抽屉，信号改紧凑行（摘录一行，全文进抽屉）；来源标签按实际来源写（AI / 离线模拟模型 / 系统统计）；不露 L123 这类内部编号，链接写“日志 N”；手机上表格改为卡片行。 */
const SlowDiag = {
  template: `<section class="zb-card rp-sec rp-sd" v-if="d"><div class="zb-ch"><ic n="search"></ic><h2>慢在哪</h2>
      <span :class="['zb-src', {ai: d.source === 'ai' && !mock}]">{{ d.source === 'ai' ? (mock ? '离线模拟模型 · 已核对' : 'AI 归因 · 已核对') : d.source === 'mock' ? '离线模拟模型' : '系统统计' }}</span>
      <span class="tiny muted">近 14 天 {{ d.logs }} 份相关日志</span><span class="grow"></span>
      <button class="btn sm" type="button" v-if="d.can_ai" :disabled="busy" @click="load(1)">{{ busy ? '归因中…' : 'AI 归因' }}</button></div>
    <zb-clamp class="sd-lead" :lines="narrow ? 2 : 3" title="慢在哪"><span v-html="cite(d.text)"></span><template #full><div v-html="cite(d.text)"></div><div class="tiny muted mt" v-if="d.note">{{ d.note }}</div></template></zb-clamp>
    <div class="rp-sd-rows" v-if="d.signals.length"><div class="rp-sd-row" v-for="s in d.signals" :key="s.type">
      <div class="rp-sd-h"><b>{{ s.name }}</b><span class="chip">{{ s.days }} 天</span><span class="grow"></span><a v-for="(i, k) in s.refs" :key="i" class="sd-ref" @click="go('log', {id: i})">日志 {{ k + 1 }}</a></div>
      <zb-clamp class="small muted" :lines="1" :title="s.name + ' · 日志摘录'" :text="s.samples.map((x) => x.date.slice(5) + ' ' + x.text).join('；')"></zb-clamp></div></div>
    <div class="tiny muted mt" v-if="d.avg_labor !== null || d.avg_machinery !== null">同期日均：人员 {{ d.avg_labor === null ? '未记' : d.avg_labor + ' 人' }} · 机械 {{ d.avg_machinery === null ? '未记' : d.avg_machinery + ' 台' }}</div></section>`,
  data: () => ({ d: null, busy: false, narrow: window.innerWidth < 861 }),
  computed: { mock: () => !!(store.me && store.me.ai && store.me.ai.provider === "mock") },
  mounted() { this.load(0); },
  methods: {
    go,
    // 打开详情只取已有归因与系统统计（run=0 不调用模型，验收 I9）；点“AI 归因”才生成
    async load(run) {
      const id = store.route && store.route.params && store.route.params.id;
      if (!id || !["manager", "eng_sup", "leader"].includes(store.me.user.role)) return;
      if (run) this.busy = true;
      try { this.d = await GET(`/api/alerts/${id}/slow-diag?run=${run ? 1 : 0}`, { quiet: !run }); } catch (e) { if (!run) this.d = null; else toast(e.message, true); }
      finally { this.busy = false; }
    },
    cite(t) { return String(t || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])).replace(/\[L(\d+)\]/g, '<span class="rv-cite">日志</span>'); },
  },
};
// 一屏化（10-10）：不再挂在页面底部插槽，改为预警详情右栏的一块（AlertDetail 在 views.js，先于本文件加载）
if (typeof AlertDetail !== "undefined") AlertDetail.components.SlowDiag = SlowDiag;
