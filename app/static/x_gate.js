/* x_gate：放行Agent（🚦 放行清单）。
   往前看未来 7/14/21 天将开工的 P6 作业，按工序列出开工/浇筑条件：系统核到的打勾，没核到的是“待落实”（不等于没做），缺项指到责任人。
   - 路由 gate：天数切换 + KPI + 按最紧迫排序的作业卡片，点开就地展开条件清单，就地标记/指派，不跳页；一屏化（10-10）：卡片列表内部分页（电脑每页 4 项、手机 3 项，从待办直达时翻到该项所在页），说明收进问号；
   - 放行清单缺项是首页统一待办（/api/todo）的来源之一（技术员只看自己负责范围），“看放行条件”直达 gate 页并展开对应作业；原 top:todo 小卡（todo 只是导航分区键、不是路由，从未渲染）已清理；
   - “系统已核到”与“某某确认”分开显示；人工优先于系统核验；条件为通用模板，以项目方案和监理要求为准。 */
(function () {
  const VERDICT = { ready: ["green", "可开工"], near: ["orange", "临近缺项"], gap: ["yellow", "有缺项"], blocked: ["red", "受阻"] };
  const EFF = { ready: ["green", "已具备", "✓"], na: ["", "不适用", "–"], blocked: ["red", "受阻", "⛔"], missing: ["red", "缺项", "✗"], unknown: ["yellow", "待落实", "help"] };
  const MAN = { ready: "确认已具备", na: "标为不适用", blocked: "标为受阻" };
  const NOTE_HINT = { ready: "凭证或说明（选填）", na: "不适用原因（必填）", blocked: "受阻原因（必填）" };
  const when = (d) => (d == null ? "未排期" : d === 0 ? "今日开工" : d > 0 ? d + " 天后开工" : "应开未开 " + -d + " 天");

  // ---------------------------------------------------------------------- 放行清单页
  const GatePage = {
    template: `<div class="xg">
      <page-head page="gate"><template #actions><div class="row xg-days"><div class="seg"><button v-for="n in [7, 14, 21]" :key="n" :class="{on: days === n}" @click="setDays(n)">未来 {{ n }} 天</button></div>
          <button v-if="data && data.perms.admin" class="btn sm" :disabled="busy" @click="notifyNow">发送预警</button></div></template></page-head>
      <div v-if="err" class="banner warn">{{ err }}</div>
      <template v-if="data">
        <empty-state v-if="!data.has_plan" reason="放行清单要看未来几天将开工的作业，需要先导入 P6 计划。" :action="can('progress_ops') ? {label: '去导入 P6 计划', route: 'progress?tab=plan', id: 'plan-file'} : null"></empty-state>
        <template v-else>
          <div class="zb-kpis n4 rp-kpis xg-kpis">
            <button type="button" :class="['zb-kpi', {on: f === ''}]" @click="f = ''"><span class="zb-kpi-ic"><ic n="calendar"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">窗口内作业</span>
              <b class="zb-kpi-v">{{ data.kpi.total }}<small> 项</small></b><span class="zb-kpi-s">未来 {{ days }} 天排期开工</span></span></button>
            <button type="button" :class="['zb-kpi', data.kpi.ready ? 'green' : '', {on: f === 'ready'}]" @click="f = f === 'ready' ? '' : 'ready'"><span class="zb-kpi-ic"><ic n="check-circle"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">可开工</span>
              <b class="zb-kpi-v">{{ data.kpi.ready }}<small> 项</small></b><span class="zb-kpi-s">条件已具备</span></span></button>
            <button type="button" :class="['zb-kpi', data.kpi.near ? 'orange' : '', {on: f === 'near'}]" @click="f = f === 'near' ? '' : 'near'"><span class="zb-kpi-ic"><ic n="clock"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">临近缺项</span>
              <b class="zb-kpi-v">{{ data.kpi.near }}<small> 项</small></b><span class="zb-kpi-s">3 天内或已超开工日</span></span></button>
            <button type="button" :class="['zb-kpi', data.kpi.blocked ? 'red' : '', {on: f === 'blocked'}]" @click="f = f === 'blocked' ? '' : 'blocked'"><span class="zb-kpi-ic"><ic n="ban"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">受阻</span>
              <b class="zb-kpi-v">{{ data.kpi.blocked }}<small> 项</small></b><span class="zb-kpi-s">有条件被标为受阻</span></span></button></div>
          <div class="tiny muted xg-sub">另有 {{ data.kpi.gap }} 项有缺项（开工较晚）。<a v-if="f" @click="f = ''">显示全部</a> 数据日期 {{ data.data_date || '未知' }} · 按紧迫度排序<help-tip v-if="data.notice" label="条件说明" :text="data.notice"></help-tip></div>
          <empty-state v-if="!shown.length" :reason="f ? '该类别下没有作业。' : '未来 ' + days + ' 天没有排期开工的作业。'" :action="emptyAction"></empty-state>
          <zb-paged ref="pg" v-if="shown.length" :items="shown" :size="4" :m-size="3" unit="项作业" cls="xg-list" :fit="200"><template #default="{ item: it }"><div :key="it.code" :id="'xg-' + it.code" :class="['zb-card', 'xg-card', 'v-' + it.verdict]">
            <div class="xg-head" @click="toggle(it.code)">
              <div class="row wrap"><span :class="['chip', VERDICT[it.verdict][0]]">{{ it.verdict === 'near' && it.days_to_start < 0 ? '已超开工日' : it.verdict_name }}</span>
                <span class="xg-title grow" style="word-break: normal; overflow-wrap: break-word"><act-name :c="it.code" :n="it.name" :s="it.short_name"></act-name></span>
                <span :class="['chip', it.days_to_start <= 3 ? 'orange' : 'blue']">{{ when(it.days_to_start) }}</span>
                <span class="chip red" v-if="it.is_critical">关键线路</span>
                <span class="xg-arrow"><ic :n="open === it.code ? 'arrow-up' : 'chevron'"></ic></span></div>
              <div class="tiny muted mt">{{ it.wbs_path || '—' }} · 计划 {{ it.plan_start || '—' }}<template v-if="it.total_float_d != null"> · 总浮时 {{ fmtN(it.total_float_d) }} 天</template> · {{ it.category_name }}
                · 责任：<template v-if="it.units.length">{{ it.units.join('、') }}<template v-if="it.owners.length">（{{ it.owners.join('、') }}）</template></template><template v-else>未配置责任范围</template></div>
              <div class="xg-prog"><div class="xg-bar"><i :style="{width: (it.need_n ? it.done_n * 100 / it.need_n : 0) + '%'}"></i></div>
              <div class="tiny">已具备 {{ it.done_n }} / {{ it.need_n }}<span v-if="it.na_n" class="muted"> （另 {{ it.na_n }} 项不适用）</span>
                <span v-if="it.missing_n"> · 缺项 <b class="xg-red">{{ it.missing_n }}</b></span><span v-if="it.unknown_n"> · 待落实 {{ it.unknown_n }}</span><span v-if="it.blocked_n"> · 受阻 <b class="xg-red">{{ it.blocked_n }}</b></span></div></div>
              <div class="xg-gaps" v-if="open !== it.code && it.top_gaps.length">
                <span v-for="g in it.top_gaps" :key="g.key" :class="['chip', EFF[g.effective][0]]">{{ g.label }}<template v-if="g.owner"> · {{ g.owner }}</template></span>
                <span class="tiny muted" v-if="it.gap_n > it.top_gaps.length">等 {{ it.gap_n }} 项</span></div></div>

            <div class="xg-body" v-if="open === it.code">
              <div v-if="!det[it.code]" class="zb-empty">加载中</div>
              <template v-else>
                <template v-for="g in groups(det[it.code])" :key="g.name">
                  <div class="xg-grp">{{ g.name }}</div>
                  <div v-for="c in g.list" :key="c.key" :class="['xg-c', 'e-' + c.effective]">
                    <div class="xg-c-top"><span :class="['chip', EFF[c.effective][0]]"><ic :n="EFF[c.effective][2]"></ic>{{ EFF[c.effective][1] }}</span>
                      <b class="xg-lbl">{{ c.label }}</b><span class="chip" v-if="c.custom">自定义</span>
                      <span class="chip orange" v-if="c.conflict">与系统核验不一致</span>
                      <span class="chip blue" v-if="c.owner"><ic n="user"></ic>{{ c.owner.name }}</span>
                      <span :class="['chip', c.overdue ? 'red' : '']" v-if="c.due_date">期限 {{ c.due_date.slice(5) }}</span></div>
                    <div class="tiny xg-sys" v-if="c.auto">{{ c.auto_state === 'unknown' ? '系统未核到：' : '系统已核到：' }}{{ c.auto_text }}</div>
                    <div class="tiny muted xg-sys" v-else>{{ c.auto_text }}</div>
                    <div class="tiny xg-man" v-if="c.manual">{{ c.manual.by_name || '有权者' }} {{ MAN[c.manual.state] || '' }} · {{ fmtDT(c.manual.at) }}<template v-if="c.manual.note">：{{ c.manual.note }}</template></div>
                    <div class="xg-act" v-if="det[it.code].perms.edit">
                      <button class="btn sm" :disabled="busy" @click="edit(it.code, c, 'ready')">已具备</button>
                      <button class="btn sm" :disabled="busy" @click="edit(it.code, c, 'blocked')">受阻</button>
                      <button class="btn sm" v-if="det[it.code].perms.admin" :disabled="busy" @click="edit(it.code, c, 'na')">不适用</button>
                      <button class="btn sm ghost" v-if="c.manual" :disabled="busy" @click="revoke(it.code, c)">撤销人工</button>
                      <button class="btn sm ghost" v-if="det[it.code].perms.admin" :disabled="busy" @click="editOwner(it.code, c)">指派</button>
                      <button class="btn sm ghost danger" v-if="c.custom && det[it.code].perms.admin" :disabled="busy" @click="del(it.code, c)">删除</button></div>
                    <div class="xg-ed" v-if="ed && ed.code === it.code && ed.key === c.key">
                      <template v-if="ed.mode === 'state'"><div class="small mb"><b>{{ MAN[ed.state] }}</b>：{{ c.label }}</div>
                        <div class="row"><input class="in" v-model.trim="ed.note" maxlength="200" :placeholder="NOTE_HINT[ed.state]" @keyup.enter="saveState">
                          <button class="btn primary sm" :disabled="busy || needNote" @click="saveState">确认</button><button class="btn sm" @click="ed = null">取消</button></div>
                        <div class="tiny muted mt" v-if="needNote">{{ ed.state === 'blocked' ? '标为受阻要写明原因（至少 2 个字），责任人才知道卡在哪里' : '标为不适用要写明原因（至少 2 个字）' }}</div></template>
                      <template v-else><div class="small mb"><b>指派责任人和期限</b>：{{ c.label }}</div>
                        <div class="row"><select class="in" v-model.number="ed.owner_id"><option :value="0">不指定</option>
                            <option v-for="p in det[it.code].people" :key="p.id" :value="p.id">{{ p.name }} · {{ p.role_name }}<template v-if="p.company"> · {{ p.company }}</template></option></select>
                          <input class="in" type="date" v-model="ed.due" style="flex:0 1 150px">
                          <button class="btn primary sm" :disabled="busy" @click="saveOwner">保存</button><button class="btn sm" @click="ed = null">取消</button></div></template></div>
                  </div>
                </template>
                <div class="xg-add" v-if="det[it.code].can_add_custom">
                  <input class="in" v-model.trim="newLabel" maxlength="60" placeholder="自定义条件，如：业主代表到场见证" @keyup.enter="addCustom(it.code)">
                  <button class="btn sm" :disabled="busy || newLabel.length < 2" @click="addCustom(it.code)"><ic n="plus"></ic>增加条件</button></div>
                <div class="tiny muted mt">{{ det[it.code].notice }}</div>
              </template></div>
          </div></template></zb-paged>
        </template>
      </template>
      <load-state v-else-if="!err" loading></load-state>
      <ai-note page="gate"></ai-note></div>`,
    data: () => ({ data: null, err: "", days: lsGet("xg_days", 14), f: "", open: "", det: {}, ed: null, newLabel: "", busy: false, VERDICT, EFF, MAN, NOTE_HINT }),
    computed: {
      shown() { return this.data.items.filter((r) => !this.f || r.verdict === this.f); },
      needNote() { return !!this.ed && this.ed.mode === "state" && ["na", "blocked"].includes(this.ed.state) && (this.ed.note || "").length < 2; },
      emptyAction() {
        if (this.f) return { label: "显示全部", fn: () => { this.f = ""; } };
        return this.days < 21 ? { label: "看未来 21 天", fn: () => this.setDays(21) } : null;
      },
    },
    async mounted() {
      await this.load();
      const o = store.route.params.open;
      if (o && this.data && this.data.items.some((r) => r.code === o)) {
        this.f = "";
        await nextTick(); const pg = this.$refs.pg, idx = this.shown.findIndex((r) => r.code === o);   // 翻到这项所在的页
        if (pg && idx >= 0) pg.go(Math.floor(idx / pg.n) + 1);
        await this.toggle(o);
        nextTick(() => { const el = document.getElementById("xg-" + o); if (el && el.scrollIntoView) el.scrollIntoView({ block: "start" }); });
      }
    },
    methods: {
      go, can, fmtDT, fmtN, when,
      async load() {
        this.err = "";
        try { this.data = await GET("/api/gate?days=" + this.days); } catch (e) { this.err = e.message; }
      },
      setDays(n) { if (n === this.days) return; this.days = n; lsSet("xg_days", n); this.load(); },
      groups(d) {
        const out = [];
        for (const c of d.conditions) {
          let g = out.find((x) => x.name === c.category_name);
          if (!g) out.push((g = { name: c.category_name, list: [] }));
          g.list.push(c);
        }
        return out;
      },
      async toggle(code) {
        this.ed = null;
        if (this.open === code) { this.open = ""; return; }
        this.open = code;
        await this.loadDetail(code);
      },
      async loadDetail(code) {
        try { this.det[code] = await GET("/api/gate/" + encodeURIComponent(code)); }
        catch (e) { toast(e.message, true); this.open = ""; }
      },
      edit(code, c, state) { this.ed = { code, key: c.key, mode: "state", state, note: state === "ready" && c.manual && c.manual.state === "ready" ? c.manual.note : "" }; },
      editOwner(code, c) { this.ed = { code, key: c.key, mode: "owner", owner_id: c.owner ? c.owner.id : 0, due: c.due_date || "" }; },
      // 写操作返回最新详情：就地替换，并静默刷新列表上的摘要
      async apply(code, p) {
        this.busy = true;
        try { this.det[code] = await p; this.ed = null; await this.load(); }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      saveState() {
        const e = this.ed;
        if (["na", "blocked"].includes(e.state) && e.note.length < 2) return toast("请填写原因", true);
        return this.apply(e.code, PUT(`/api/gate/${encodeURIComponent(e.code)}/items/${encodeURIComponent(e.key)}`, { state: e.state, note: e.note }));
      },
      revoke(code, c) { return this.apply(code, PUT(`/api/gate/${encodeURIComponent(code)}/items/${encodeURIComponent(c.key)}`, { state: "pending" })); },
      saveOwner() {
        const e = this.ed;
        return this.apply(e.code, PUT(`/api/gate/${encodeURIComponent(e.code)}/items/${encodeURIComponent(e.key)}/owner`, { owner_id: e.owner_id || null, due_date: e.due || null }));
      },
      async addCustom(code) {
        if (this.newLabel.length < 2) return;
        await this.apply(code, POST(`/api/gate/${encodeURIComponent(code)}/items`, { label: this.newLabel }));
        this.newLabel = "";
      },
      del(code, c) {
        if (!confirm("删除自定义条件“" + c.label + "”？")) return;
        return this.apply(code, DEL(`/api/gate/${encodeURIComponent(code)}/items/${encodeURIComponent(c.key)}`));
      },
      async notifyNow() {
        this.busy = true;
        try {
          const r = await POST("/api/gate/notify");
          toast(r.sent ? `已提醒 ${r.sent} 人次（${r.activities} 项作业）` : (r.duplicates ? "今日已提醒" : "3 天内开工作业无缺项，无需提醒"));
        } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
    },
  };

  EXT.route("gate", GatePage);
})();
