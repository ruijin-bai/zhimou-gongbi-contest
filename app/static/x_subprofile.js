/* x_subprofile：分包综合履约画像（2026-10-08 增补能力 9）。“现场”线安全、质量之后的一个页签，给工程主管、安全主管、项目经理。
   每家分包一张卡：近 30 天安全、质量、进度预警、联系单（工程主管另有周确认按时率；项目经理不看日志填报）；
   数字由系统统计，AI 只写一段评价并经数字核对。不排名、不打分、不写罚款。 */
// 各分包今日 · 本周（RJ 10-10 从工程主管首页移来：不是每天必看，放在分包页查阅）。只给工程主管：
// 今日记录、本周确认是日志/周确认填报情况，项目经理不看（PM_SCOPE）；接口不给 record_today 时“今日记录”整列不出现。
const SubToday = {
  template: `<div class="sp-today-w"><section class="zb-card sp-today" v-if="!bare && isEng && p && p.companies.length"><div class="zb-ch"><ic n="users"></ic><h2>各分包今日 · 本周</h2>
      <span class="sp-today-m">今日记录、本周确认、今日人数 · {{ p.companies.length }} 家</span><span class="grow"></span>
      <button type="button" class="zb-link" @click="open = true">查看 <ic n="chevron"></ic></button></div></section>
    <zb-drawer v-if="(open || bare) && isEng && p" title="各分包今日 · 本周" @close="shut">
    <table class="eh-t"><thead><tr><th>单位</th><th v-if="fill">今日记录</th><th>本周确认</th><th>今日人数</th></tr></thead>
      <tbody><tr v-for="c in p.companies" :key="c.company"><td :title="c.company"><b>{{ c.company }}</b></td><td v-if="fill">{{ c.record_today }}/{{ c.techs }}</td>
        <td :class="{'tc-over': c.techs && c.week_confirmed < c.techs}">{{ c.week_confirmed }}/{{ c.techs }}</td><td>{{ c.people_today || '—' }}</td></tr></tbody></table>
    <div class="tiny muted mt">本周确认＝技术员本周进度确认已提交；今日人数取分包日志，无日志为“—”。</div></zb-drawer></div>`,
  // bare：不出卡片，直接打开抽屉（工程主管首页“本周确认”指标卡就地查看明细用），关闭时通知外面
  props: { bare: Boolean },
  emits: ["close"],
  data: () => ({ p: null, open: false }),
  computed: {
    isEng() { return roleOf() === "eng_sup"; },
    fill() { return this.p.companies.some((c) => c.record_today !== undefined); },
  },
  methods: { shut() { this.open = false; if (this.bare) this.$emit("close"); } },
  mounted() { if (this.isEng) GET("/api/production/overview", { quiet: true }).then((p) => { this.p = p; }).catch(() => { this.p = null; }); },
};
const SubProfilePage = {
  template: `<div class="rs-sp"><page-head title="分包履约画像" purpose="每家分包近 30 天的安全、质量、进度和联系单情况，供管理参考，不排名">
      <template #meta><template v-if="d && d.items.length">近 {{ d.days }} 天（{{ d.items[0].since }} ~ {{ d.items[0].until }}） · 数字由系统统计 · 不排名、不打分</template><template v-else>近 30 天 · 数字由系统统计 · 不排名、不打分</template> <help-tip label="数字与评语来源" :text="SRC_NOTE"></help-tip></template>
      <template #actions><button class="btn sm" type="button" v-if="d && d.items.some((p) => p.eval.can_ai)" :disabled="busy" @click="load(1)">{{ busy ? '生成中…' : 'AI 评语' }}</button></template></page-head>
    <sub-today></sub-today>
    <load-state class="mb" v-if="!d || err" :loading="!d && !err" :error="err" @retry="load"></load-state>
    <template v-if="d"><div class="rs-sp-grid" v-if="d.items.length"><section class="zb-card rs-sp-card" v-for="p in d.items" :key="p.company">
        <div class="zb-ch"><ic n="building"></ic><h2>{{ p.company }}</h2><span class="grow"></span><span class="zb-src rs-sp-src">技术员 {{ p.techs }} 人</span></div>
        <table class="t rs-sp-t"><thead><tr><th>事项</th><th>近 30 天</th><th>处理情况</th><th>需关注</th></tr></thead><tbody>
          <tr><th scope="row">安全隐患</th><td data-l="近 30 天">{{ p.safety.n }} 项<span class="muted">（高风险 {{ p.safety.high }}）</span></td><td data-l="处理情况">闭环 {{ p.safety.closed }} · 平均 {{ dd(p.safety.avg_days) }}</td><td data-l="需关注" :class="{'tc-over': p.safety.overdue}">超期 {{ p.safety.overdue }}</td></tr>
          <tr><th scope="row">质量问题</th><td data-l="近 30 天">{{ p.quality.n }} 项</td><td data-l="处理情况">闭环 {{ p.quality.closed }} · 平均 {{ dd(p.quality.avg_days) }}</td><td data-l="需关注" :class="{'tc-over': p.quality.overdue}">超期 {{ p.quality.overdue }}</td></tr>
          <tr><th scope="row">进度预警</th><td data-l="近 30 天">{{ p.progress.alerts }} 条<span class="muted">（红 {{ p.progress.red }}）</span></td><td data-l="处理情况">负责作业 {{ p.progress.acts }} 项</td><td data-l="需关注">属实 {{ p.progress.confirmed }}</td></tr>
          <tr><th scope="row">工作联系单</th><td data-l="近 30 天">{{ p.letters.n }} 份</td><td data-l="处理情况">24 小时内签收 {{ p.letters.acked_24h }}{{ p.letters.rate === null ? '' : '（' + p.letters.rate + '%）' }}</td><td data-l="需关注" :class="{'tc-over': p.letters.overdue}">超期未回复 {{ p.letters.overdue }}</td></tr>
          <tr v-if="p.logs"><th scope="row">周确认</th><td data-l="近 30 天">{{ p.logs.weeks }} 份</td><td data-l="处理情况">按时 {{ p.logs.on_time }}{{ p.logs.rate === null ? '' : '（' + p.logs.rate + '%）' }}</td><td data-l="需关注">—</td></tr>
        </tbody></table>
        <div :class="['rs-sp-eval', 'is-' + p.eval.source]"><div class="rs-sp-eh"><b>评语</b><span :class="['chip', p.eval.source === 'ai' ? 'blue' : '']">{{ evalTag(p.eval.source) }}</span></div>
          <zb-clamp class="rs-sp-et" :text="p.eval.text" :lines="3" :title="p.company + ' · 评语'"></zb-clamp><zb-clamp class="tiny muted" v-if="p.eval.note" :text="p.eval.note" :lines="1" title="评语说明"></zb-clamp></div></section></div>
      <div class="zb-card" v-else><div class="zb-empty"><b>还不能按单位统计</b><p>还没有标注所属分包单位的账号。请系统管理员在账号里填写“所属分包单位”。</p></div></div>
    </template></div>`,
  components: { SubToday },
  data: () => ({ d: null, err: null, busy: false,
    SRC_NOTE: "数字取自系统已确认的隐患、质量问题、进度预警和联系单；评语只在真实模型输出且数字逐个核对通过时标“AI按当前事实整理”，离线模拟模型输出仅供开发测试、不作结论，其余为系统按统计生成的句子。" }),
  mounted() { this.load(); },
  methods: {
    dd(v) { return v === null || v === undefined ? "—" : v + " 天"; },
    // 评语来源如实标注：真实模型且通过数字核对 → AI；离线模拟 → 不作结论；规则兜底 → 系统生成
    evalTag(src) { return src === "ai" ? "AI按当前事实整理" : src === "mock" ? "离线模拟模型输出 · 不作结论" : "系统按当前事实生成"; },
    // 打开只取已有评语（run=0 不调用模型，验收 I9）；点“AI 评语”才生成
    async load(run) {
      this.err = null; if (run) this.busy = true;
      try { this.d = await GET("/api/sub-profiles" + (run ? "?run=1" : "")); } catch (e) { if (run) toast(e.message, true); else this.err = e; } finally { this.busy = false; }
    },
  },
};
EXT.route("subs", SubProfilePage);
EXT.nav(["eng_sup", "safety_sup", "manager"], { k: "subs", route: "subs", label: "分包履约画像", short: "履约画像", icon: "users", line: "site", after: "quality" });
