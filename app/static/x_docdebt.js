/* x_docdebt：部位档案页的“资料欠账”（2026-10-08 增补能力 8，只给工程主管）。
   完工作业（P6 实际完成或周确认累计 100%）缺隐蔽验收记录、相关材料批次未复试的，按作业列出；全部确定性规则。
   “起草补齐通知”就地起草致分包文书 docdebt_notice，工程主管看过点“确认下发”才进分包技术员的待办，签收即销项。没有欠账时整张卡不显示。 */
const DocDebtCard = {
  template: `<section class="card mt dd-card" v-if="d && d.items.length"><div class="row between"><h2><ic n="folder"></ic> 资料欠账</h2>
      <span class="tiny muted">完工作业 {{ d.summary.acts }} 项 · 缺验收 {{ d.summary.conceal }} · 未复试 {{ d.summary.material }}</span></div>
    <div class="tiny muted">{{ d.note }}</div>
    <div class="pc-row" v-for="r in d.items" :key="r.code">
      <div class="pc-h"><b><act-name :c="r.code" :n="r.name" :s="r.short_name"></act-name></b><span class="chip">{{ r.part }}</span><span class="tiny muted grow">{{ r.basis }}</span></div>
      <ul class="dd-list"><li v-for="(x, i) in r.debts" :key="i" class="small"><span :class="['chip', x.type === 'conceal' ? 'orange' : 'red']">{{ x.type === 'conceal' ? '验收' : '复试' }}</span> {{ x.text }}</li></ul>
      <div class="pc-a"><span class="chip green" v-if="r.notice && r.notice.letter_status">补齐通知已下发 · {{ r.notice.letter_status }}</span>
        <template v-else-if="!dr[r.code]"><button v-for="u in r.units" :key="u" class="btn sm" :disabled="busy" @click="make(r, u)">起草补齐通知 · {{ u }}</button>
          <span class="tiny muted" v-if="!r.units.length">没有找到负责该作业的分包技术员（按 WBS 范围），请先在账号里设置范围</span></template></div>
      <div class="pc-draft" v-if="dr[r.code]"><div class="tiny muted">资料补齐通知草稿 · {{ dr[r.code].source === 'ai' ? 'AI 起草' : '规则版文字' }}，确认后才下发</div>
        <p class="small" v-for="s in dr[r.code].template.sections" :key="s.key"><b>{{ s.title }}</b> {{ dr[r.code].narrative[s.key] }}</p>
        <div class="banner warn" v-if="dr[r.code].check.length">有 {{ dr[r.code].check.length }} 处数字未在统计中找到，请到工笔文书核对后再下发</div>
        <div class="row" style="gap:6px"><button class="btn primary sm" :disabled="busy || dr[r.code].check.length > 0" @click="confirm(r)">确认下发</button>
          <button class="btn ghost sm" @click="close(r)">收起</button></div></div></div></section>`,
  data: () => ({ d: null, dr: {}, busy: false }),
  mounted() { if (store.me && store.me.user.role === "eng_sup") this.load(); },
  methods: {
    async load() { try { this.d = await GET("/api/doc-debt", { quiet: true }); } catch (_) { this.d = null; } },
    async make(r, u) {
      this.busy = true;
      try { this.dr = { ...this.dr, [r.code]: await POST("/api/gongbi/docs", { kind: "docdebt_notice", ref: r.code + "|" + u }) }; }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
    close(r) { const x = { ...this.dr }; delete x[r.code]; this.dr = x; },
    async confirm(r) {
      const doc = this.dr[r.code]; this.busy = true;
      try { await PUT("/api/gongbi/docs/" + doc.id, { narrative: doc.narrative, confirm: true }); toast("已下发"); this.close(r); await this.load(); }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};
EXT.slot("bottom:sites", DocDebtCard, { id: "docdebt" });
