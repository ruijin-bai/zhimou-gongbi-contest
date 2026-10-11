/* x_contract：合同 / 规范条款库（2026-10-08 增补能力 10）。“资料台账”线的一个页签。
   - 工程主管导入（项目经理、安全主管只读）合同条件、特殊条款、技术规范（粘贴或上传 .txt/.docx），按条款号自动切分；原文只存本机业务库，导入前请确认保密要求；
   - 这里只做导入和按条款号/关键词查原文；提问仍走顶部统一输入框：问到合同、条款、规范时自动检索条款库，回答标 [C编号]，点开回到这里看原文。 */
const ContractPage = {
  // 页面顺序：查条款（各岗位都用）→ 已导入文件 → 导入（只给 can_import，即工程主管；项目经理、安全主管看不到导入表单与删除，后端 import/delete 同样 403）
  template: `<div class="ra ct"><page-head title="合同条款库" purpose="合同条件、特殊条款、技术规范的原文；提问时自动引用，回答标出条款出处">
      <template #meta>在顶部输入框直接问，回答会引用这里的条款原文并标 C 编号 · 条款库只存本机<template v-if="d && !d.can_import"> · 本岗只读，由工程主管导入</template></template></page-head>
    <section class="zb-card mb" v-if="d && d.docs.length"><div class="zb-ch"><ic n="search"></ic><h2>查条款</h2><span class="zb-n" v-if="searched">{{ hits.length }}</span><span class="grow"></span><span class="zb-src">按条款号或关键词检索原文，不调用模型</span></div>
      <div class="ct-q"><input class="in grow" v-model="q" placeholder="条款号或关键词，如 20.2、notice、雨季" @keyup.enter="search"><button class="btn primary" @click="search"><ic n="search"></ic>查找</button></div>
      <div class="ct-hit" v-for="h in hits" :key="h.id" :id="'c' + h.id" :class="{on: String(h.id) === String(focus)}">
        <div class="row wrap" style="gap:6px"><span class="chip blue">C{{ h.id }}</span><b>{{ h.clause_no }} {{ h.title }}</b><span class="tiny muted grow">《{{ h.doc_title }}》</span></div>
        <div class="small ct-text">{{ h.text }}</div></div>
      <div class="zb-empty" v-if="searched && !hits.length">没有找到匹配的条款</div></section>
    <section class="zb-card mb" v-if="d"><div class="zb-ch"><ic n="book"></ic><h2>已导入文件</h2><span class="zb-n">{{ d.docs.length }}</span></div>
      <div class="readrow row ct-doc" v-for="x in d.docs" :key="x.id"><span class="chip">{{ d.kinds[x.kind] || '其他' }}</span><span class="grow">{{ x.title }}</span>
        <span class="tiny muted">{{ x.clauses }} 条 · {{ x.created_at.slice(0, 10) }}</span><button class="btn ghost sm" v-if="d.can_import" :title="'删除《' + x.title + '》'" @click="del(x)"><ic n="x"></ic></button></div>
      <empty-state compact v-if="!d.docs.length" :reason="d.can_import ? '还没有导入合同或规范。导入后，提问时会自动引用条款原文。' : '还没有导入合同或规范，请工程主管导入。'"></empty-state></section>
    <section class="zb-card" v-if="d && d.can_import"><div class="zb-ch"><ic n="arrow-up"></ic><h2>导入文件</h2><span class="grow"></span><span class="zb-src">导入前请确认合同文件的保密要求</span></div>
      <div class="ct-imp"><input class="in" v-model="f.title" placeholder="文件名称，如 合同通用条件（FIDIC 2017）">
        <select class="in" v-model="f.kind"><option v-for="(v, k) in d.kinds" :key="k" :value="k">{{ v }}</option></select>
        <input type="file" accept=".txt,.docx" @change="file = $event.target.files[0]"></div>
      <textarea class="in mt" rows="4" v-model="f.text" placeholder="或粘贴条款文字；行首写条款号（Sub-Clause 20.2 / 20.2.1 / 第 12 条）的会按条款切分"></textarea>
      <div class="row mt"><button class="btn primary" :disabled="busy || !(f.text.trim() || file)" @click="upload">{{ busy ? '导入中' : '导入' }}</button></div></section>
    <load-state v-if="!d || err" :loading="!d && !err" :error="err" @retry="load"></load-state></div>`,
  data: () => ({ d: null, err: null, f: { title: "", kind: "contract", text: "" }, file: null, busy: false, q: "", hits: [], searched: false, focus: "" }),
  async mounted() {
    await this.load();
    const id = store.route && store.route.params && store.route.params.id;
    if (id) { this.focus = id; this.q = ""; try { const r = await GET("/api/contract/clause/" + id); this.hits = [r]; this.searched = true; } catch (_) {} }
  },
  methods: {
    async load() { this.err = null; try { this.d = await GET("/api/contract/docs"); } catch (e) { this.err = e; } },
    async upload() {
      const fd = new FormData(); fd.append("title", this.f.title); fd.append("kind", this.f.kind); fd.append("text", this.f.text);
      if (this.file) fd.append("file", this.file);
      this.busy = true;
      try { const r = await POST("/api/contract/docs", fd); toast(`已导入 ${r.clauses} 条`); this.f = { title: "", kind: "contract", text: "" }; this.file = null; await this.load(); }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
    async del(x) { if (!confirm(`删除《${x.title}》？已引用它的回答里的 C 编号将打不开。`)) return; try { await DEL("/api/contract/docs/" + x.id); await this.load(); } catch (e) { toast(e.message, true); } },
    async search() { if (!this.q.trim()) return; try { this.hits = (await GET("/api/contract/search?q=" + encodeURIComponent(this.q))).items; this.searched = true; } catch (e) { toast(e.message, true); } },
  },
};
EXT.route("contract", ContractPage);
EXT.nav(["manager", "eng_sup", "safety_sup"], { k: "contract", route: "contract", label: "合同条款", short: "条款", icon: "book", line: "arch" });
