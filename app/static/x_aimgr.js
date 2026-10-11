/* x_aimgr：系统管理员的“模型与额度”——Key 池、分线路模型、额度与用量。
   路由 aimgr（AI 治理 → 模型与额度）。密钥只能新增/删除/测试，页面永不显示完整 Key。 */

const AIM_STATE = { ok: ["green", "可用"], off: ["", "已停用"], bad: ["red", "失效"], spent: ["orange", "额度用尽"], cool: ["orange", "限流中"] };
const aimNum = (n) => (n >= 10000 ? (n / 10000).toFixed(1).replace(/\.0$/, "") + "万" : String(n || 0));   // 数量单位统一用“万”，不混用 k
const aimPct = (used, cap) => (cap ? Math.min(100, Math.round((used / cap) * 100)) : 0);

const AiMgrPage = {
  template: `<div v-if="o" class="ra-aim"><page-head page="aimgr"><template #meta>修改即时生效，密钥不显示<help-tip>这里只管模型与额度，不涉及业务处理；额度按本系统记账的 token 控制，不等同平台侧余额。</help-tip></template><template #actions><button class="btn" @click="rebalance">推荐分配</button><button class="btn primary" @click="openAdd">添加 Key</button></template></page-head>
    <div class="ra-sim aim-warn" v-if="o.provider === 'mock'"><ic n="info"></ic>离线模拟模式：暂无可用 Key，配置将在接入 Key 后生效。</div>
    <div class="ra-sim aim-warn" v-else-if="cfg.paused"><ic n="pause"></ic>已暂停全部 AI 调用。<a class="small" @click="togglePause">恢复</a></div>
    <div class="zb-kpis n4">
      <div :class="['zb-kpi', 'ld-static', o.totals.today.fails ? 'orange' : '']"><span class="zb-kpi-ic"><ic n="chart"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">今日调用</span><b class="zb-kpi-v">{{ o.totals.today.calls }}</b><span class="zb-kpi-s">{{ o.totals.today.fails ? '其中失败 ' + o.totals.today.fails : '无失败' }}</span></span></div>
      <div class="zb-kpi ld-static"><span class="zb-kpi-ic"><ic n="sparkles"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">今日 token</span><b class="zb-kpi-v">{{ num(o.totals.today.tokens) }}</b><span class="zb-kpi-s">本系统记账</span></span></div>
      <div class="zb-kpi ld-static"><span class="zb-kpi-ic"><ic n="database"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">累计 token</span><b class="zb-kpi-v">{{ num(o.totals.total.tokens) }}</b><span class="zb-kpi-s">不等同平台侧余额</span></span></div>
      <div :class="['zb-kpi', 'ld-static', o.keys.length ? (usable ? 'green' : 'red') : '']"><span class="zb-kpi-ic"><ic n="link"></ic></span><span class="zb-kpi-b"><span class="zb-kpi-l">可用 Key</span><b class="zb-kpi-v">{{ usable }}<small> / {{ o.keys.length }}</small></b><span class="zb-kpi-s">{{ o.keys.length ? '用尽、失效或限流时自动切换' : '未登记 Key' }}</span></span></div></div>
    <div class="seg ra-aim-seg"><button v-for="t in SECS" :key="t[0]" :class="{on: sec === t[0]}" @click="sec = t[0]">{{ t[1] }}</button></div>

    <section class="zb-card" v-if="sec === 'keys'"><div class="zb-ch"><ic n="link"></ic><h2>Key 池与额度</h2><span class="zb-n">{{ o.keys.length }}</span><span class="grow"></span><span class="zb-src">额度 0 为不限</span></div>
      <div class="zb-body"><zb-paged :items="o.keys" :size="10" :m-size="4" :fit="160" tag="table" cls="t ra-rt" unit="把"><template #head><thead><tr><th>Key</th><th>状态</th><th>主责线路</th><th style="min-width:170px">今日 token</th><th class="right">今日调用</th><th class="right">累计 token</th><th></th></tr></thead></template>
        <template #default="{ item: k }"><tr :style="{opacity: k.enabled ? 1 : .55}">
          <td class="ra-rt-h"><b>{{ k.label }}</b><div class="tiny muted">{{ k.mask }}<span v-if="k.note"> · {{ k.note }}</span></div></td>
          <td data-l="状态"><span :class="['chip', STATE[k.state][0]]">{{ STATE[k.state][1] }}</span><div class="tiny muted">{{ k.state === 'ok' ? '' : k.state_note }}<template v-if="k.state === 'ok' && k.checked_at">{{ k.state_note }}</template></div></td>
          <td data-l="主责线路"><span class="chip blue" v-for="l in k.lanes" :key="l" style="margin:1px">{{ laneName(l) }}</span><span class="tiny muted" v-if="!k.lanes.length">仅作备用</span></td>
          <td data-l="今日 token"><div class="aim-bar" :title="k.usage.today.tokens + ' tokens'"><i :class="{hot: pct(k.usage.today.tokens, k.daily_tokens) >= 85}" :style="{width: (k.daily_tokens ? pct(k.usage.today.tokens, k.daily_tokens) : Math.min(100, k.usage.today.tokens / 2000)) + '%'}"></i></div>
            <div class="tiny muted">{{ num(k.usage.today.tokens) }} / {{ k.daily_tokens ? num(k.daily_tokens) : '不限' }}</div></td>
          <td class="right" data-l="今日调用">{{ k.usage.today.calls }}<span class="tiny muted" v-if="k.daily_calls"> / {{ k.daily_calls }}</span><span class="tiny" style="color:var(--red)" v-if="k.usage.today.fails"> · 失败{{ k.usage.today.fails }}</span></td>
          <td class="right" data-l="累计 token">{{ num(k.usage.total.tokens) }}<span class="tiny muted" v-if="k.total_tokens"> / {{ num(k.total_tokens) }}</span></td>
          <td class="nowrap ra-rt-a"><button class="btn sm" :disabled="busy === 't' + k.id" @click="test(k)">{{ busy === 't' + k.id ? '测试中' : '测试' }}</button>
            <button class="btn sm" @click="openEdit(k)">编辑</button></td></tr></template>
          <template #empty><div class="zb-empty">暂无 Key。点页头“添加 Key”登记后，按主责线路分担调用。</div></template></zb-paged></div></section>

    <section class="zb-card" v-if="sec === 'lanes'"><div class="zb-ch"><ic n="sliders"></ic><h2>线路与模型</h2><span class="zb-n">{{ o.lanes.length }}</span><span class="grow"></span><span class="zb-src">各线路可单独指定模型，由主责 Key 优先承担</span></div>
      <div class="zb-body"><div class="ra-tw" v-fit="160"><table class="t ra-rt"><thead><tr><th>线路</th><th style="min-width:250px">模型</th><th>主责 Key</th><th></th></tr></thead>
        <tbody><tr v-for="l in o.lanes" :key="l.key"><td class="ra-rt-h"><b>{{ l.name }}</b><div class="tiny muted">{{ l.desc }}</div></td>
          <td data-l="模型"><input class="in" :list="'aim-' + l.key" v-model.trim="cfg['model_' + l.key]" :placeholder="l.key === 'asr' ? '留空则不启用语音转写' : '模型名'"><datalist :id="'aim-' + l.key"><option v-for="m in modelsFor(l.key)" :key="m.id" :value="m.id"></option></datalist></td>
          <td class="small" data-l="主责 Key"><span v-if="l.keys.length">{{ l.keys.join('、') }}</span><span class="muted" v-else>无，由备用 Key 承担</span></td>
          <td class="nowrap ra-rt-a"><button class="btn sm" v-if="l.key !== 'asr'" :disabled="busy === 'l' + l.key" @click="laneTest(l)">{{ busy === 'l' + l.key ? '测试中' : '测试' }}</button>
            <div class="tiny" :style="{color: laneRes[l.key] && laneRes[l.key].ok ? 'var(--green)' : 'var(--red)'}" v-if="laneRes[l.key]"><ic :n="laneRes[l.key].ok ? 'check' : 'x'"></ic>{{ laneRes[l.key].ok ? laneRes[l.key].key + ' ' + laneRes[l.key].latency_ms + 'ms' : laneRes[l.key].error }}</div></td></tr></tbody></table></div>
      <div class="row mt ra-aim-f"><div class="tiny muted" v-if="modelErr">{{ modelErr }}</div>
      <div class="tiny muted" v-else-if="!modelsDone">加载模型清单中</div>
      <div class="tiny muted" v-else-if="!models.length">没有取到可选模型清单，可手动输入模型名。</div>
      <div class="tiny muted" v-else>共 {{ models.length }} 个可选模型；图像线路须选多模态（VL / V）模型。</div><div class="grow"></div>
      <button class="btn primary" @click="saveCfg">保存模型与限额</button></div></div></section>

    <section class="zb-card" v-if="sec === 'limit'"><div class="zb-ch"><ic n="shield"></ic><h2>全局限额</h2><span class="grow"></span><span class="zb-src">{{ o.host_ok ? '境内白名单内' : '不在境内白名单' }}</span></div>
      <div class="zb-body"><div class="ra-fg"><div class="field"><label class="f">每人每日调用上限（0 为不限）</label><input class="in" type="number" min="0" v-model.number="cfg.user_daily_calls"></div>
        <div class="field"><label class="f">单次照片数上限</label><input class="in" type="number" min="1" max="20" v-model.number="cfg.max_images"></div>
        <div class="field"><label class="f">调用超时（秒）</label><input class="in" type="number" min="20" max="600" v-model.number="cfg.timeout"></div></div>
      <div class="tiny muted">接口地址 {{ o.base_url }}（{{ o.host_ok ? '境内白名单内' : '不在境内白名单，已拒绝调用' }}）；地址与白名单不可在页面修改。今日 {{ o.user_calls_today }} 位用户使用 AI。</div>
      <div class="row mt2"><button class="btn" :class="{danger: !cfg.paused}" @click="togglePause">{{ cfg.paused ? '恢复 AI 调用' : '暂停全部 AI' }}</button><div class="grow"></div>
        <button class="btn primary" @click="saveCfg">保存模型与限额</button></div></div></section>

    <div class="zb-row ra-aim-r" v-if="sec === 'use'">
      <section class="zb-card"><div class="zb-ch"><ic n="chart"></ic><h2>近 14 天 token</h2><span class="grow"></span><span class="zb-src">本系统记账</span></div><div class="zb-empty" v-if="!o.by_day.some((d) => d.tokens || d.calls)">近 14 天没有按日记账的调用。</div><div class="bars aim-days" v-else><div v-for="d in o.by_day" :key="d.day" :title="d.day + '：' + d.tokens + ' tokens / ' + d.calls + ' 次'"><i :style="{height: dayPct(d) + '%'}"></i></div></div>
        <div class="row between tiny muted mt"><span>{{ o.by_day[0].day.slice(5) }}</span><span>{{ o.by_day[o.by_day.length - 1].day.slice(5) }}</span></div></section>
      <section class="zb-card"><div class="zb-ch"><ic n="layers"></ic><h2>近 7 天用量分布</h2><span class="zb-n">{{ o.by_kind.length }}</span></div><zb-paged :items="o.by_kind" :size="10" :m-size="5" :fit="160" tag="table" cls="t ra-rt" unit="类"><template #head><thead><tr><th>用途</th><th>线路</th><th class="right">次数</th><th class="right">token</th></tr></thead></template>
        <template #default="{ item: k }"><tr><td class="small ra-rt-h">{{ k.kind }}</td><td class="small muted" data-l="线路">{{ laneName(k.lane) }}</td><td class="right" data-l="次数">{{ k.calls }}<span class="tiny" style="color:var(--red)" v-if="k.fails"> · 失败 {{ k.fails }}</span></td><td class="right" data-l="token">{{ num(k.tokens) }}</td></tr></template>
          <template #empty><div class="zb-empty">近 7 天没有调用。</div></template></zb-paged></section></div>

    <div class="modal-bg" v-if="form" @click.self="form = null"><div class="modal"><h2>{{ form.id ? '编辑 Key' : '添加 Key' }}</h2>
      <div class="field mt" v-if="!form.id"><label class="f">API Key（保存后不再显示）</label><input class="in" type="password" autocomplete="off" v-model.trim="form.key" placeholder="sk-…"></div>
      <div class="tiny muted" v-else>{{ form.mask }}</div>
      <div class="field"><label class="f">名称</label><input class="in" v-model.trim="form.label" placeholder="如：照片识别"></div>
      <div class="field"><label class="f">主责线路（多选；不选则仅作备用）</label>
        <label class="small aim-ck" v-for="l in o.lanes" :key="l.key"><input type="checkbox" :value="l.key" v-model="form.lanes"> {{ l.name }}</label></div>
      <div class="grid g3"><div class="field"><label class="f">每日 token</label><input class="in" type="number" min="0" v-model.number="form.daily_tokens"></div>
        <div class="field"><label class="f">累计 token</label><input class="in" type="number" min="0" v-model.number="form.total_tokens"></div>
        <div class="field"><label class="f">每日调用次数</label><input class="in" type="number" min="0" v-model.number="form.daily_calls"></div></div>
      <div class="tiny muted">额度 0 为不限；额度按本系统记账的 token 控制，不等同平台侧余额。</div>
      <div class="field mt"><label class="f">备注</label><input class="in" v-model.trim="form.note" maxlength="100"></div>
      <div class="row mt2"><button class="btn danger" v-if="form.id" @click="remove">删除</button><label class="small" v-if="form.id"><input type="checkbox" v-model="form.enabled"> 启用</label><div class="grow"></div>
        <button class="btn" @click="form = null">取消</button><button class="btn primary" @click="save">保存</button></div></div></div></div><load-state v-else loading :error="err" @retry="load"></load-state>`,
  data: () => ({ o: null, err: null, cfg: {}, models: [], modelsDone: false, modelErr: "", form: null, busy: "", laneRes: {}, STATE: AIM_STATE,
    // 10-10 一屏化：四个分区用页签切换，每次只显示一块，长表在面板内滚动
    sec: "keys", SECS: [["keys", "Key 池与额度"], ["lanes", "线路与模型"], ["limit", "全局限额"], ["use", "用量"]] }),
  computed: { usable() { return this.o.keys.filter((k) => k.state === "ok" || k.state === "cool").length; } },
  mounted() { this.load(); this.loadModels(); },
  methods: {
    num: aimNum, pct: aimPct,
    laneName(k) { const l = this.o.lanes.find((x) => x.key === k); return l ? l.name : k; },
    dayPct(d) { const max = Math.max(1, ...this.o.by_day.map((x) => x.tokens)); return d.tokens ? Math.max(4, Math.round((d.tokens / max) * 100)) : 0; },
    modelsFor(lane) {
      const tag = (m) => m.tag;
      if (lane === "vision") return this.models.filter((m) => tag(m) === "vision");
      if (lane === "asr") return this.models.filter((m) => tag(m) === "asr");
      return this.models.filter((m) => tag(m) === "text" || tag(m) === "vision");
    },
    async load() { this.err = null; try { this.o = await GET("/api/admin/ai"); this.cfg = { ...this.o.config, paused: !!this.o.config.paused }; } catch (e) { this.err = e; } },
    async loadModels() {
      try { const r = await GET("/api/admin/ai/models"); this.models = r.models || []; this.modelErr = r.error || ""; } catch (e) { this.modelErr = "模型清单加载失败，可手动输入模型名"; }
      finally { this.modelsDone = true; }
    },
    async saveCfg() {
      try { await PUT("/api/admin/ai/config", this.cfg); toast("已保存，即时生效"); await this.load(); }
      catch (e) { toast(e.message, true); }
    },
    async togglePause() {
      if (!this.cfg.paused && !confirm("暂停后所有用户的 AI 功能不可用，确认暂停？")) return;
      this.cfg.paused = !this.cfg.paused; await this.saveCfg();
    },
    openAdd() { this.form = { key: "", label: "", lanes: [], daily_tokens: 0, total_tokens: 0, daily_calls: 0, note: "" }; },
    openEdit(k) { this.form = { id: k.id, mask: k.mask, label: k.label, lanes: [...k.lanes], enabled: k.enabled, daily_tokens: k.daily_tokens, total_tokens: k.total_tokens, daily_calls: k.daily_calls, note: k.note }; },
    async save() {
      const f = this.form, body = { label: f.label, lanes: f.lanes, daily_tokens: f.daily_tokens || 0, total_tokens: f.total_tokens || 0, daily_calls: f.daily_calls || 0, note: f.note };
      try {
        if (f.id) await PUT("/api/admin/ai/keys/" + f.id, { ...body, enabled: f.enabled });
        else { if (!f.key) return toast("请填写 Key", true); await POST("/api/admin/ai/keys", { ...body, key: f.key }); }
        this.form = null; toast("已保存"); await this.load();
      } catch (e) { toast(e.message, true); }
    },
    async remove() {
      if (!confirm("删除后将清除该 Key 的密钥与用量记录，确认删除？")) return;
      try { await DEL("/api/admin/ai/keys/" + this.form.id); this.form = null; toast("已删除"); await this.load(); } catch (e) { toast(e.message, true); }
    },
    async test(k) {
      this.busy = "t" + k.id;
      try { const r = await POST(`/api/admin/ai/keys/${k.id}/test`); toast(r.ok ? `${k.label} 可用（${r.latency_ms}ms）` : `${k.label}：${r.error}`, !r.ok); await this.load(); }
      catch (e) { toast(e.message, true); } finally { this.busy = ""; }
    },
    async laneTest(l) {
      this.busy = "l" + l.key;
      try { this.laneRes = { ...this.laneRes, [l.key]: await POST("/api/admin/ai/lane-test", { lane: l.key }) }; await this.load(); }
      catch (e) { this.laneRes = { ...this.laneRes, [l.key]: { ok: false, error: e.message } }; } finally { this.busy = ""; }
    },
    async rebalance() {
      if (!confirm("按启用的 Key 重新分配主责线路，额度设置不变，确认执行？")) return;
      try { await POST("/api/admin/ai/rebalance"); toast("已重新分配"); await this.load(); } catch (e) { toast(e.message, true); }
    },
  },
};

EXT.route("aimgr", AiMgrPage);
