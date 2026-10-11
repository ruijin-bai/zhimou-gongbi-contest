/* x_gantt.js：P6 计划横道图（views.js Plan 页“横道图”视图）。
   - 作业清单与筛选沿用 Plan 页（/api/p6/activities）；目标计划日期与数据日期取 /api/plan/gantt；
   - 条形：当前计划/实际（已开工从实际开始画，完成取实际完成或最早完成），内填完成%；细灰线为目标计划（P6 target）；菱形为里程碑；
   - 颜色：主色＝普通作业，红＝关键作业，绿＝已完成（只表示状态）；按 WBS 分组，可收起。只展示，不改动 P6 计划。
   - 全屏（RJ 10-09）：铺满浏览器窗口查看，Esc 或“退出全屏”返回；不依赖浏览器全屏接口，手机与嵌入页面同样可用。
   - 一屏化（10-10）：图表区高度按页面余量（v-fit），整页不出现纵向滚动；全屏时不受此限。 */

const GT_SCALES = [["week", "周", 8], ["month", "月", 2.4], ["quarter", "季", 0.8]];
const GT_LEFT = 300;
const gtDay = (s) => (s ? Date.parse(s + "T00:00:00") : null);

const PlanGantt = {
  props: { items: Array },
  template: `<div :class="['gt', {'gt-full': full}]">
  <div class="row wrap gt-bar"><div class="seg"><button v-for="s in scales" :key="s[0]" :class="{on: scale === s[0]}" @click="scale = s[0]">{{ s[1] }}</button></div>
    <span class="gt-lg"><i class="gt-sw"></i>计划/实际</span><span class="gt-lg"><i class="gt-sw crit"></i>关键</span><span class="gt-lg"><i class="gt-sw done"></i>已完成</span>
    <span class="gt-lg"><i class="gt-sw base"></i>目标计划</span><span class="gt-lg"><i class="gt-dm"></i>里程碑</span>
    <span class="gt-lg"><i class="gt-ln"></i>数据日期 {{ dd || '—' }}</span><span class="gt-lg"><i class="gt-ln td"></i>今天</span>
    <help-tip text="条形为 P6 当前计划/实际日期（已开工从实际开始画，内填完成%），细灰线为目标计划；鼠标停在作业上看日期与浮时。只展示 P6 导入数据，不改动计划。"></help-tip>
    <span class="grow"></span><a class="small" @click="toggleAll">{{ allClosed ? '全部展开' : '全部收起' }}</a>
    <button type="button" class="btn sm" @click="setFull(!full)"><ic :n="full ? 'x' : 'maximize'"></ic>{{ full ? '退出全屏' : '全屏' }}</button></div>
  <div class="gt-wrap" ref="wrap" v-if="rows.length" v-fit="220">
    <div class="gt-in" :style="{width: (left + width) + 'px'}">
      <div class="gt-row gt-head"><div class="gt-l">作业（{{ count }}）</div>
        <div class="gt-r" :style="{width: width + 'px'}"><span v-for="t in ticks" :key="t.k" :class="['gt-tick', {yr: t.yr}]" :style="{left: t.x + 'px'}">{{ t.label }}</span></div></div>
      <div class="gt-body">
        <div class="gt-vg" v-for="t in ticks" :key="'g' + t.k" :style="{left: (left + t.x) + 'px'}"></div>
        <div class="gt-vl" v-if="xdd !== null" :style="{left: (left + xdd) + 'px'}"></div>
        <div class="gt-vl td" v-if="xtd !== null" :style="{left: (left + xtd) + 'px'}"></div>
        <template v-for="g in rows" :key="g.key">
          <div class="gt-row gt-grp" @click="toggle(g.key)"><div class="gt-l"><ic n="chevron-down" :class="['pk-chev', {rot: !closed[g.key]}]"></ic><b>{{ g.name || '（无 WBS）' }}</b><span class="tiny muted">{{ g.items.length }}</span></div>
            <div class="gt-r" :style="{width: width + 'px'}"><div class="gt-sum" :style="span(g.s, g.f)"></div></div></div>
          <template v-if="!closed[g.key]"><div class="gt-row" v-for="a in g.items" :key="a.code" :title="tip(a)">
            <div class="gt-l"><act-name :c="a.code" :n="a.name" :s="a.short_name"></act-name></div>
            <div class="gt-r" :style="{width: width + 'px'}">
              <div class="gt-base" v-if="a.bs && a.bf && !a.is_milestone" :style="span(a.bs, a.bf)"></div>
              <div v-if="a.is_milestone" :class="['gt-dm', cls(a)]" :style="{left: (x(a.f || a.s) - 5) + 'px'}"></div>
              <div v-else-if="a.s && a.f" :class="['gt-b', cls(a)]" :style="span(a.s, a.f)"><div class="gt-p" :style="{width: Math.min(100, Math.max(0, a.pct || 0)) + '%'}"></div></div>
            </div></div></template>
        </template></div></div></div>
  <div class="zb-empty" v-else>没有可画的作业（缺计划日期）。</div></div>`,
  data: () => ({ scale: "month", base: {}, dd: null, closed: {}, full: false, narrow: typeof window !== "undefined" && window.innerWidth <= 860 }),
  computed: {
    scales: () => GT_SCALES,
    left() { return this.narrow ? 160 : GT_LEFT; },   // 与 x_gantt.css 的左栏宽度一致（手机 160）
    ppd() { return (GT_SCALES.find((s) => s[0] === this.scale) || GT_SCALES[1])[2]; },
    acts() {
      return (this.items || []).map((a) => {
        const b = this.base[a.code] || [];
        return { ...a, s: a.act_start || a.early_start, f: a.act_finish || a.early_finish, bs: b[0], bf: b[1], pct: a.act_finish ? 100 : a.phys_pct };
      });
    },
    count() { return this.acts.filter((a) => a.s || a.f).length; },
    range() {
      let lo = Infinity, hi = -Infinity;
      for (const a of this.acts) for (const d of [a.s, a.f, a.bs, a.bf]) if (d) { const t = gtDay(d); if (t < lo) lo = t; if (t > hi) hi = t; }
      if (this.dd) { const t = gtDay(this.dd); lo = Math.min(lo, t); hi = Math.max(hi, t); }
      return lo === Infinity ? null : [lo - 10 * 864e5, hi + 20 * 864e5];
    },
    width() { return this.range ? Math.ceil((this.range[1] - this.range[0]) / 864e5 * this.ppd) : 0; },
    rows() {
      const by = new Map();
      for (const a of this.acts) {
        if (!a.s && !a.f) continue;
        const k = a.wbs_path || "";
        if (!by.has(k)) by.set(k, { key: k, name: k, items: [] });
        by.get(k).items.push(a);
      }
      const out = [...by.values()];
      for (const g of out) {
        g.items.sort((p, q) => String(p.s || p.f).localeCompare(String(q.s || q.f)) || p.code.localeCompare(q.code));
        g.s = g.items.map((a) => a.s || a.f).sort()[0];
        g.f = g.items.map((a) => a.f || a.s).sort().slice(-1)[0];
      }
      return out.sort((p, q) => String(p.s).localeCompare(String(q.s)));
    },
    ticks() {
      if (!this.range) return [];
      const out = [], step = this.scale === "quarter" ? 3 : 1;
      const d = new Date(this.range[0]); d.setDate(1); d.setHours(0, 0, 0, 0);
      while (d.getTime() <= this.range[1]) {
        const m = d.getMonth(), t = d.getTime();
        if (m % step === 0 && t >= this.range[0]) {
          const lbl = `${d.getFullYear()}-${String(m + 1).padStart(2, "0")}`;
          out.push({ k: lbl, x: (t - this.range[0]) / 864e5 * this.ppd, yr: m === 0, label: this.scale === "week" || m === 0 || !out.length ? lbl : `${m + 1}月` });
        }
        d.setMonth(m + 1);
      }
      return out;
    },
    xdd() { return this.dd && this.range ? this.x(this.dd) : null; },
    xtd() {
      if (!this.range) return null;
      const n = new Date(), t = `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, "0")}-${String(n.getDate()).padStart(2, "0")}`;
      return gtDay(t) >= this.range[0] && gtDay(t) <= this.range[1] ? this.x(t) : null;
    },
    allClosed() { return this.rows.length > 0 && this.rows.every((g) => this.closed[g.key]); },
  },
  async mounted() {
    try { const r = await GET("/api/plan/gantt"); if (!r.none) { this.base = r.baseline || {}; this.dd = r.data_date; } } catch (_) { /* 拿不到目标计划时只画当前计划 */ }
    // 打开时把数据日期滚到可见处
    this.$nextTick(() => { const w = this.$refs.wrap; if (w && this.xdd !== null) w.scrollLeft = Math.max(0, this.xdd - 200); });
  },
  unmounted() { this.setFull(false); },
  methods: {
    fmtN,
    setFull(on) {
      this.full = on;
      document.body.classList.toggle("gt-lock", on);
      if (on) { this._esc = (e) => { if (e.key === "Escape") this.setFull(false); }; document.addEventListener("keydown", this._esc); }
      else if (this._esc) { document.removeEventListener("keydown", this._esc); this._esc = null; }
    },
    x(d) { return (gtDay(d) - this.range[0]) / 864e5 * this.ppd; },
    span(s, f) { const a = this.x(s), b = this.x(f) + this.ppd; return { left: a + "px", width: Math.max(3, b - a) + "px" }; },
    cls(a) { return a.act_finish ? "done" : a.is_critical ? "crit" : ""; },
    toggle(k) { this.closed = { ...this.closed, [k]: !this.closed[k] }; },
    toggleAll() { const c = !this.allClosed; this.closed = Object.fromEntries(this.rows.map((g) => [g.key, c])); },
    tip(a) {
      const l = [`${a.code} ${a.name}`, `当前计划 ${a.early_start || "—"} ~ ${a.early_finish || "—"}`];
      if (a.act_start) l.push(`实际 ${a.act_start} ~ ${a.act_finish || "进行中"}`);
      if (a.bs) l.push(`目标计划 ${a.bs} ~ ${a.bf || "—"}`);
      const tf = a.total_float_d === null || a.total_float_d === undefined ? "" : ` · 总浮时 ${fmtN(a.total_float_d)} 天`;
      l.push(`完成 ${fmtN(a.pct)}%${tf}${a.is_critical ? " · 关键" : ""}`);
      return l.join("\n");
    },
  },
};
