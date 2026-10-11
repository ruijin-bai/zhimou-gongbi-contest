/* x_precontrol：两周高风险作业安全预控（安全主管智能管理台，2026-10-08 增补能力 3）。
   - 卡片数据 GET /api/precontrol：放行清单里未来 14 天开工的吊装、水上、深基坑、交通导改类作业 + 负责单位近 30 天同类隐患 + 一句预控重点；
   - “起草预控提醒”就地起草（工笔文书 precontrol_notice），安全主管看过后点“确认下发”才进分包安全员的待办和消息，签收即销项；
   - 没有这类作业时整张卡不显示；不给项目经理。 */
const PrecontrolCard = {
  template: `<section class="card mt pc-card" v-if="d && d.items.length"><div class="row between"><h2><ic n="helmet"></ic> 未来两周高风险作业</h2>
      <span class="tiny muted">{{ d.items.length }} 项 · 取自放行清单</span></div>
    <div class="tiny muted">{{ d.source === 'ai' ? '预控重点由安全智能体按下列统计写，数字已与统计核对' : d.source === 'mock' ? '预控重点为离线模拟模型输出（仅供开发测试）' : '预控重点按系统统计生成' }}<template v-if="d.note">；{{ d.note }}</template></div>
    <div class="pc-row" v-for="r in d.items" :key="k(r)">
      <div class="pc-h"><b class="nowrap">{{ dayText(r) }}</b><span class="grow"><act-name :c="r.code" :n="r.name" :s="r.short_name"></act-name></span>
        <span class="chip orange">{{ r.kind_name }}</span><span class="chip" v-if="r.critical">关键</span></div>
      <div class="pc-m tiny"><span>{{ r.company || '未标注负责单位' }}</span>
        <span :class="['chip', r.hist.n ? 'red' : 'green']">近 30 天同类隐患 {{ r.hist.n }} 次</span>
        <span class="muted" v-if="r.gate.short.length">放行缺：{{ r.gate.short.slice(0, 2).join('、') }}</span></div>
      <div class="pc-f small"><ic n="sparkles"></ic><span>{{ r.focus }}</span></div>
      <div class="pc-a" v-if="r.company">
        <span class="chip green" v-if="r.notice && r.notice.letter_status">预控提醒已下发 · {{ r.notice.letter_status }}</span>
        <button class="btn sm" v-else-if="!dr[k(r)]" :disabled="busy" @click="make(r)">{{ r.notice ? '查看预控提醒草稿' : '起草预控提醒' }}</button></div>
      <div class="pc-draft" v-if="dr[k(r)]"><div class="tiny muted">预控提醒草稿（致{{ r.company }}分包安全员）· {{ dr[k(r)].source === 'ai' ? 'AI 起草' : '规则版文字' }}，确认后才下发</div>
        <p class="small" v-for="s in dr[k(r)].template.sections" :key="s.key"><b>{{ s.title }}</b> {{ dr[k(r)].narrative[s.key] }}</p>
        <div class="banner warn" v-if="dr[k(r)].check.length">有 {{ dr[k(r)].check.length }} 处数字未在统计中找到，请到工笔文书核对后再下发</div>
        <div class="row" style="gap:6px"><button class="btn primary sm" :disabled="busy || dr[k(r)].check.length > 0" @click="confirm(r)">确认下发</button>
          <button class="btn ghost sm" @click="close(r)">收起</button></div></div></div>
    <div class="tiny muted mt">系统只在站内下发给分包安全员，不对外发送；不写罚款。</div></section>`,
  data: () => ({ d: null, dr: {}, busy: false }),
  mounted() { if (store.me && store.me.user.role === "safety_sup") this.load(); },
  methods: {
    k(r) { return r.code + "|" + r.company; },
    async load() { try { this.d = await GET("/api/precontrol", { quiet: true }); } catch (_) { this.d = null; } },
    dayText(r) {
      if (!r.start) return "日期未知";
      const s = String(r.start).slice(0, 10), w = "日一二三四五六"[new Date(s + "T00:00:00").getDay()];
      return s.slice(5) + " 周" + w + (r.days_to_start === 0 ? "（今天）" : r.days_to_start < 0 ? "（应开未开）" : "");
    },
    async make(r) {
      this.busy = true;
      try {
        const doc = r.notice && r.notice.status === "draft" ? await GET("/api/gongbi/docs/" + r.notice.doc_id) : await POST("/api/gongbi/docs", { kind: "precontrol_notice", ref: this.k(r) });
        this.dr = { ...this.dr, [this.k(r)]: doc };
      } catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
    close(r) { const x = { ...this.dr }; delete x[this.k(r)]; this.dr = x; },
    async confirm(r) {
      const doc = this.dr[this.k(r)]; this.busy = true;
      try { await PUT("/api/gongbi/docs/" + doc.id, { narrative: doc.narrative, confirm: true }); toast("已下发到" + r.company + "分包安全员"); this.close(r); await this.load(); }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
  },
};
EXT.slot("bottom:sdash", PrecontrolCard, { id: "precontrol" });
