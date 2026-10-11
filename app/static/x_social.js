/* x_social：现场圈社交层的前端组件。
   头像主页抽屉（关注/粉丝/去过的地点）、私信抽屉（只有会话双方可读）、简报阅读抽屉（AI 总结、划重点、对句评论）、
   转发框与转发卡片、@同事选择器。主体在 circle.js，这里在它之后加载并把组件挂上去。 */
(function () {
  const canWrite = () => !["leader", "admin"].includes(roleOf());
  const isHarbour = () => !isSubOrg();

  // 文字里的 @姓名 高亮：拆成片段，由模板逐段渲染（不用 v-html，避免注入）
  function atParts(text) {
    return String(text || "").split(/(@[^\s，。,.:：；;!！?？]+)/).filter((t) => t !== "").map((t) => ({ t, at: t[0] === "@" && t.length > 1 }));
  }
  window.atParts = atParts;

  let peopleCache = null;
  async function loadPeople() {
    if (!peopleCache) { try { peopleCache = await GET("/api/social/people"); } catch (_) { peopleCache = []; } }
    return peopleCache;
  }

  // ---------------------------------------------------------------------- @同事选择器
  const MentionBtn = {
    emits: ["pick"],
    template: `<span class="mn"><button class="btn sm" type="button" title="@同事" @click="toggle">@同事</button>
      <div class="mn-pop" v-if="open"><input class="in" v-model.trim="q" placeholder="搜索姓名或单位" ref="q">
        <div class="mn-list"><a v-for="p in shown" :key="p.id" @click="pick(p)"><b>{{ p.name }}</b><span class="tiny muted"> {{ p.role_name }}<template v-if="p.company"> · {{ p.company }}</template></span></a>
          <div class="tiny muted" v-if="!shown.length">没有匹配的同事</div></div></div></span>`,
    data: () => ({ open: false, q: "", all: [] }),
    computed: { shown() { const q = this.q; return this.all.filter((p) => !q || p.name.includes(q) || p.company.includes(q) || p.role_name.includes(q)).slice(0, 12); } },
    methods: {
      async toggle() { this.open = !this.open; if (this.open) { this.all = await loadPeople(); this.$nextTick(() => this.$refs.q && this.$refs.q.focus()); } },
      pick(p) { this.$emit("pick", p.name); this.open = false; this.q = ""; },
    },
  };

  // ---------------------------------------------------------------------- 转发
  const OrigCard = {
    props: ["orig"],
    template: `<div class="orig" v-if="orig">
      <div class="tiny muted" v-if="orig.hidden">原动态不可见或已删除</div>
      <template v-else><div class="tiny"><b>{{ orig.author.name }}</b> <span class="chip">{{ orig.tag }}</span> <span class="muted">{{ ago(orig.time) }}</span></div>
        <div class="small pre" v-if="orig.text">{{ orig.text }}</div>
        <div class="orig-pics" v-if="orig.photos.length"><ev-photo v-for="ph in orig.photos" :key="ph.id" size="s" :src="ph.thumb" :full="ph.url"></ev-photo></div>
        <div class="tiny muted post-geo" v-if="orig.geo_slot"><ic n="map-pin"></ic> {{ geoText(orig.geo) }}</div></template></div>`,
    data: () => ({ store }),
    methods: { ago, geoText },
  };

  const RepostBox = {
    props: ["post"],
    emits: ["done", "cancel"],
    components: { MentionBtn },
    template: `<div class="rp-box"><div class="tiny muted">转发到现场圈（只发给能看到原动态的人）</div>
      <div class="orig-wrap"><orig-card :orig="preview"></orig-card></div>
      <textarea class="in" v-model="text" rows="2" maxlength="300" placeholder="说点什么（可选），可 @同事 提醒他看" ref="t"></textarea>
      <div class="row between mt"><mention-btn @pick="at"></mention-btn><div class="row"><button class="btn sm" @click="$emit('cancel')">取消</button>
        <button class="btn primary sm" :disabled="busy" @click="go">{{ busy ? '转发中' : '转发' }}</button></div></div></div>`,
    data() {
      const p = this.post;
      return { text: "", busy: false, preview: { key: p.key, hidden: false, author: p.author, tag: p.tag, time: p.time, text: (p.text || (p.notice && p.notice.title) || "").slice(0, 200), photos: p.photos.slice(0, 3), geo: p.geo, geo_slot: p.geo_slot } };
    },
    mounted() { this.$refs.t.focus(); },
    methods: {
      at(name) { this.text += (this.text && !/\s$/.test(this.text) ? " " : "") + "@" + name + " "; this.$refs.t.focus(); },
      async go() {
        this.busy = true;
        try { await POST("/api/social/repost", { key: this.post.key, text: this.text }); toast("已转发"); this.$emit("done"); }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 抽屉外壳
  const Drawer = {
    props: ["title"],
    emits: ["close"],
    template: `<div class="sd-wrap" @keyup.esc="$emit('close')"><div class="sd-mask" @click="$emit('close')"></div>
      <aside class="sd-panel"><header class="sd-hd"><b class="grow">{{ title }}</b><button class="btn ghost sm" @click="$emit('close')"><ic n="x"></ic></button></header>
        <div class="sd-bd"><slot></slot></div><slot name="foot"></slot></aside></div>`,
  };

  // ---------------------------------------------------------------------- 主页
  const ProfilePanel = {
    props: ["uid", "social"],
    emits: ["close", "dm", "place", "open"],
    components: { Drawer },
    template: `<drawer :title="p ? p.name : '主页'" @close="$emit('close')">
      <div v-if="!p" class="tiny muted">加载中</div>
      <template v-else>
        <div class="row pf-hd"><span class="av lg">{{ p.name.slice(0, 1) }}</span>
          <div class="grow"><div><b class="pf-nm">{{ p.name }}</b> <span class="chip blue">{{ p.role_name }}</span></div><div class="tiny muted">{{ p.company || '港湾' }}</div></div></div>
        <div class="row pf-nums" v-if="social"><a @click="tab = 'following'">关注 <b>{{ p.following }}</b></a><a @click="tab = 'followers'">粉丝 <b>{{ p.followers }}</b></a>
          <span class="tiny muted" v-if="p.follows_me && !p.mine">· 关注了你</span></div>
        <div class="row mt" v-if="social && !p.mine && (p.can_follow || p.can_dm)">
          <button :class="['btn', 'sm', p.i_follow ? '' : 'primary']" v-if="p.can_follow" :disabled="busy" @click="toggleFollow">{{ p.i_follow ? '已关注' : '关注' }}</button>
          <button class="btn sm" v-if="p.can_dm" @click="$emit('dm', p.id)"><ic n="message"></ic> 发消息</button></div>
        <div class="pf-sec" v-if="p.places.length || p.located_30d"><div class="tiny muted">近 30 天去过的地点（取自照片与随手记的手机定位）</div>
          <div class="row wrap mt"><a class="chip pf-pl" v-for="x in p.places" :key="x.place" @click="$emit('place', { place: x.place })"><ic n="map-pin"></ic> {{ x.place }} · {{ x.n }}</a>
            <span class="tiny muted" v-if="!p.places.length">{{ p.located_30d }} 条记录带定位，未填地点名称</span></div></div>
        <div class="pf-sec tiny" v-if="p.week"><span class="muted">本周确认（{{ p.week.start }} 起）</span>
          <span :class="['chip', p.week.status === 'submitted' || p.week.status === 'approved' ? 'green' : 'orange']">{{ weekText }}</span></div>
        <div class="seg mt" v-if="social"><button :class="{on: tab === 'posts'}" @click="tab = 'posts'">动态</button><button :class="{on: tab === 'following'}" @click="tab = 'following'">关注</button>
          <button :class="{on: tab === 'followers'}" @click="tab = 'followers'">粉丝</button></div>
        <div class="mt" v-if="!social || tab === 'posts'">
          <div class="tiny muted" v-if="!p.posts_visible">这位同事的动态不在你的可见范围内。</div>
          <div class="tiny muted" v-else-if="!posts.length">{{ loaded ? '暂无动态' : '加载中' }}</div>
          <div class="pf-post" v-for="x in posts" :key="x.key"><div class="tiny"><span class="chip">{{ x.tag }}</span> <span class="muted">{{ ago(x.time) }}</span></div>
            <div class="small pre" v-if="x.text">{{ x.text.slice(0, 120) }}</div>
            <div class="orig-pics" v-if="x.photos.length"><ev-photo v-for="ph in x.photos.slice(0, 3)" :key="ph.id" size="s" :src="ph.thumb" :full="ph.url"></ev-photo></div>
            <div class="tiny muted" v-if="x.geo_slot"><ic n="map-pin"></ic> {{ geoText(x.geo) }}</div></div></div>
        <div class="mt" v-else><div class="tiny muted" v-if="!list.length">{{ tab === 'following' ? '还没有关注任何人' : '还没有粉丝' }}</div>
          <a class="pf-person" v-for="u in list" :key="u.id" @click="openUser(u.id)"><span class="av">{{ u.name.slice(0, 1) }}</span>
            <span><b>{{ u.name }}</b><span class="tiny muted"> {{ u.role_name }}<template v-if="u.company"> · {{ u.company }}</template></span></span></a></div>
      </template></drawer>`,
    data: () => ({ p: null, posts: [], list: [], tab: "posts", busy: false, loaded: false, store, cur: 0 }),
    computed: { weekText() { const s = this.p.week.status; return s === "submitted" || s === "approved" ? "已确认" : s === "returned" ? "被退回" : s ? "草稿中" : "未开始"; } },
    watch: {
      uid: { immediate: true, handler(v) { this.cur = v; this.load(); } },
      tab(v) { if (v !== "posts") this.loadList(); },
    },
    methods: {
      ago, geoText,
      openUser(id) { this.cur = id; this.tab = "posts"; this.load(); },
      async load() {
        this.p = null; this.posts = []; this.loaded = false;
        try {
          this.p = await GET("/api/social/users/" + this.cur);
          if (this.p.posts_visible) this.posts = (await GET("/api/feed?limit=10&author=" + this.cur)).posts;
        } catch (e) { toast(e.message, true); this.$emit("close"); }
        this.loaded = true;
      },
      async loadList() { try { this.list = await GET("/api/social/users/" + this.cur + "/follows?type=" + this.tab); } catch (e) { toast(e.message, true); } },
      async toggleFollow() {
        this.busy = true;
        try { Object.assign(this.p, await POST("/api/social/follow", { user_id: this.p.id, on: !this.p.i_follow })); this.$emit("open"); }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 私信
  const DmPanel = {
    props: ["peer", "refKey"],
    emits: ["close", "unread"],
    components: { Drawer },
    template: `<drawer :title="cur ? cur.name : '私信'" @close="$emit('close')">
      <template v-if="!cur">
        <div class="tiny muted dm-tip">私信只有你和对方能看到，系统管理员和其他同事都无法查阅；发出后不能删除或撤回。</div>
        <div class="dm-th" v-for="t in threads" :key="t.peer.id" @click="open(t.peer)"><span class="av">{{ t.peer.name.slice(0, 1) }}</span>
          <div class="grow"><div><b>{{ t.peer.name }}</b> <span class="tiny muted">{{ t.peer.company || t.peer.role_name }}</span></div>
            <div class="tiny muted dm-pv">{{ t.last.mine ? '我：' : '' }}{{ t.last.text }}</div></div>
          <div class="tiny muted">{{ ago(t.last.time) }}</div><span class="badge-dot" v-if="t.unread">{{ t.unread }}</span></div>
        <div class="tiny muted" v-if="loaded && !threads.length">还没有私信</div>
        <div class="mt"><div class="tiny muted">新私信：选择同事</div><input class="in mt" v-model.trim="q" placeholder="搜索姓名或单位">
          <a class="pf-person" v-for="p in shown" :key="p.id" @click="open(p)"><span class="av">{{ p.name.slice(0, 1) }}</span>
            <span><b>{{ p.name }}</b><span class="tiny muted"> {{ p.role_name }}<template v-if="p.company"> · {{ p.company }}</template></span></span></a></div>
      </template>
      <template v-else>
        <a class="small" @click="back">← 全部私信</a>
        <div class="dm-box" ref="box">
          <div v-for="m in msgs" :key="m.id" :class="['dm-m', m.mine ? 'me' : '']">
            <div class="dm-b"><div class="dm-ref" v-if="m.ref"><div class="tiny muted">引用动态 · {{ m.ref.author }} · {{ m.ref.tag }}</div>
                <div class="small">{{ m.ref.text }}</div><ev-photo v-if="m.ref.thumb" size="s" :src="m.ref.thumb"></ev-photo></div>
              <div class="pre" v-if="m.text"><template v-for="(s, i) in atParts(m.text)" :key="i"><b v-if="s.at" class="at">{{ s.t }}</b><template v-else>{{ s.t }}</template></template></div>
              <div class="dm-ph" v-if="m.files.length"><ev-photo v-for="f in m.files" :key="f.id" size="m" :src="f.thumb" :full="f.url"></ev-photo></div></div>
            <div class="tiny muted">{{ m.time.slice(5, 16).replace('T', ' ') }}<template v-if="m.mine && m.id <= peerRead"> · 已读</template>
              <a v-if="isTech && m.text" @click="toNote(m)"> · 记为随手记</a></div></div>
          <div class="tiny muted" v-if="!msgs.length">还没有消息，发一句吧</div></div>
      </template>
      <template #foot><div class="dm-ft" v-if="cur">
        <div class="dm-refc" v-if="ref"><span class="tiny">引用：{{ ref.text }}</span><a class="tiny" @click="ref = null"> 取消</a></div>
        <div class="row"><button class="btn sm" :disabled="busy" @click="photo"><ic n="camera"></ic></button>
          <textarea class="in grow" v-model="text" rows="1" maxlength="1000" placeholder="输入消息" @keydown.enter.exact.prevent="send"></textarea>
          <button class="btn primary sm" :disabled="busy || !text.trim()" @click="send">发送</button></div></div></template></drawer>`,
    data: () => ({ cur: null, threads: [], msgs: [], peerRead: 0, text: "", ref: null, busy: false, loaded: false, q: "", all: [], timer: null, store, last: 0 }),
    computed: {
      isTech() { return roleOf() === "tech"; },
      shown() { const t = new Set(this.threads.map((x) => x.peer.id)); const q = this.q; return this.all.filter((p) => !t.has(p.id) && (!q || p.name.includes(q) || p.company.includes(q))).slice(0, 8); },
    },
    async mounted() {
      this.all = (await loadPeople());
      await this.loadThreads();
      if (this.peer) { const p = this.all.find((x) => x.id === this.peer) || (await this.fetchPeer(this.peer)); if (p) await this.open(p, this.refKey); }
      this.timer = setInterval(() => { if (!document.hidden) this.cur ? this.poll() : this.loadThreads(); }, 6000);
    },
    unmounted() { clearInterval(this.timer); },
    methods: {
      ago, atParts,
      async fetchPeer(id) { try { const r = await GET("/api/social/dm/with/" + id + "?mark=0"); return r.peer; } catch (e) { toast(e.message, true); return null; } },
      async loadThreads() {
        try { this.threads = (await GET("/api/social/dm/threads")).threads; this.$emit("unread", this.threads.reduce((n, t) => n + t.unread, 0)); }
        catch (e) { toast(e.message, true); }
        this.loaded = true;
      },
      async open(p, refKey) {
        this.cur = p; this.msgs = []; this.last = 0; this.text = ""; this.ref = null;
        try {
          const r = await GET("/api/social/dm/with/" + p.id);
          this.msgs = r.messages; this.peerRead = r.peer_read; this.last = r.messages.length ? r.messages[r.messages.length - 1].id : 0;
          if (refKey) this.ref = await this.refCard(refKey);
        } catch (e) { toast(e.message, true); }
        this.$nextTick(this.scroll); this.$emit("unread", null);
      },
      async refCard(key) {
        // 引用卡片由服务端在发送时生成快照；这里先用现场圈里已有的内容预览
        const p = (window.__circlePosts || []).find((x) => x.key === key);
        return { key, text: p ? (p.text || (p.notice && p.notice.title) || p.tag || "").slice(0, 60) : "引用的动态" };
      },
      async back() { this.cur = null; await this.loadThreads(); },
      scroll() { const b = this.$refs.box; if (b) b.scrollTop = b.scrollHeight; },
      async poll() {
        if (!this.cur) return;
        try {
          const r = await GET("/api/social/dm/with/" + this.cur.id + "?after=" + this.last);
          this.peerRead = r.peer_read;
          if (r.messages.length) { this.msgs.push(...r.messages); this.last = r.messages[r.messages.length - 1].id; this.$nextTick(this.scroll); }
        } catch (_) { /* 轮询失败不打扰 */ }
      },
      async send() {
        if (!this.text.trim() || this.busy) return;
        this.busy = true;
        try {
          const m = await POST("/api/social/dm/send", { to: this.cur.id, text: this.text, ref_key: this.ref ? this.ref.key : null });
          this.msgs.push(m); this.last = m.id; this.text = ""; this.ref = null; this.$nextTick(this.scroll);
        } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async photo() {
        const files = await takePhotos(); if (!files.length) return;
        this.busy = true;
        try {
          const fd = new FormData(); files.forEach((f) => fd.append("files", f));
          const fs = await POST("/api/social/dm/photo", fd);
          const m = await POST("/api/social/dm/send", { to: this.cur.id, text: "", file_ids: fs.map((f) => f.id) });
          this.msgs.push(m); this.last = m.id; this.$nextTick(this.scroll);
        } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async toNote(m) {
        if (!confirm("把这条消息的文字记为你的随手记？记下后会出现在你的今日记录里。")) return;
        try { await POST("/api/notes", { text: m.text.slice(0, 2000), source: "text" }); toast("已记为随手记"); } catch (e) { toast(e.message, true); }
      },
    },
  };

  // ---------------------------------------------------------------------- 简报阅读
  const ReaderPanel = {
    props: ["postKey"],
    emits: ["close", "changed"],
    components: { Drawer },
    template: `<drawer :title="a ? a.title : '阅读'" @close="$emit('close')">
      <div v-if="!a" class="tiny muted">加载中</div>
      <article v-else class="rd" ref="art" @mouseup="onSel" @touchend="onSel">
        <div class="rd-meta tiny muted"><span class="chip">{{ a.tag }}</span> {{ a.author.name }} · {{ ago(a.time) }} · 阅读 {{ a.reads }}</div>
        <div class="rd-sum card">
          <div class="row between"><b><ic n="sparkles"></ic> 智能体总结</b><button class="btn sm" :disabled="sumBusy" @click="summarize(!!a.summary)">{{ sumBusy ? '生成中' : (a.summary ? '重新总结' : '生成总结') }}</button></div>
          <template v-if="a.summary"><div class="rd-one">{{ a.summary.one }}</div>
            <ol class="rd-pts"><li v-for="(pt, i) in a.summary.points" :key="i">{{ pt.text }} <a class="chip" v-for="r in pt.refs" :key="r" @click="jump(r)">{{ r }}</a></li></ol>
            <div class="tiny muted">{{ polish ? '已按原文摘录，智能体正在润色…' : (a.summary.source === 'ai' ? '由智能体依据本文生成' : '按原文摘录') }}，仅供阅读参考；数字与结论以原文为准。</div></template>
          <div class="tiny muted" v-else>点“生成总结”，智能体只复述本文内容，并标出出处段落。</div></div>
        <div :class="['rd-p', flash === p.id ? 'flash' : '']" v-for="p in a.paragraphs" :key="p.id" :data-pid="p.id" :id="'rd-' + p.id">
          <div class="tiny muted rd-lb" v-if="p.label && labelAt(p)">{{ p.label }}</div>
          <p><template v-for="(s, i) in segs(p)" :key="i"><mark v-if="s.cls" :class="['hl', s.cls]" @click.stop="markClick(s, p)">{{ s.t }}</mark><template v-else>{{ s.t }}</template></template><span class="tiny muted rd-id">{{ p.id }}</span></p>
          <div class="rd-qc" v-for="c in quoted(p)" :key="c.id"><div class="tiny muted">「{{ c.quote }}」</div><b>{{ c.name }}</b>：{{ c.text }}</div></div>
        <div class="rd-photos" v-if="a.photos.length"><ev-photo v-for="ph in a.photos" :key="ph.id" size="m" :src="ph.thumb" :full="ph.url"></ev-photo></div>
        <div class="rd-cm"><b>评论 {{ a.comments.length }}</b>
          <div class="cm small" v-for="c in a.comments.filter((x) => !x.quote)" :key="c.id"><span class="chip orange" v-if="c.featured">精选</span> <b>{{ c.name }}</b>：{{ c.text }}</div>
          <div class="tiny muted" v-if="!a.comments.length">还没有评论。选中文字可对着某一句评论。</div></div>
      </article>
      <div class="rd-tb" v-if="sel" :style="{ left: sel.x + 'px', top: sel.y + 'px' }" @mousedown.prevent @touchstart.stop>
        <template v-if="!sel.form"><button class="btn sm primary" @click="sel.form = 'hl'">划重点</button><button class="btn sm" v-if="a && a.can_write" @click="sel.form = 'cm'">评论此句</button></template>
        <div class="rd-form" v-else><div class="tiny muted">「{{ sel.quote.slice(0, 40) }}{{ sel.quote.length > 40 ? '…' : '' }}」</div>
          <input class="in" v-model="note" :maxlength="sel.form === 'hl' ? 100 : 300" :placeholder="sel.form === 'hl' ? '批注（可空）' : '写评论'">
          <label class="tiny" v-if="sel.form === 'hl' && a.can_write"><input type="checkbox" v-model="pub"> 公开给同事看（默认只有我自己看到）</label>
          <div class="row"><button class="btn sm" @click="closeSel">取消</button><button class="btn sm primary" :disabled="busy || (sel.form === 'cm' && !note.trim())" @click="commit">{{ sel.form === 'hl' ? '划重点' : '发表' }}</button></div></div></div>
      <div class="rd-tb" v-if="mk" :style="{ left: mk.x + 'px', top: mk.y + 'px' }">
        <template v-if="mk.mine"><div class="tiny" v-if="mk.h.note">批注：{{ mk.h.note }}</div><div class="tiny muted">{{ mk.h.public ? '已公开' : '仅自己可见' }}</div>
          <div class="row"><button class="btn sm" v-if="a.can_write" @click="togglePub">{{ mk.h.public ? '设为私有' : '公开' }}</button><button class="btn sm" @click="delHl">取消重点</button><button class="btn sm" @click="mk = null">关闭</button></div></template>
        <template v-else><div class="small">{{ mk.n }} 人划过重点</div><div class="row"><button class="btn sm" @click="mk = null">关闭</button></div></template></div>
      <template #foot><div class="rd-ft" v-if="a"><button :class="['btn', 'sm', a.fav.me ? 'primary' : '']" @click="fav"><ic n="pin"></ic> {{ a.fav.me ? '已收藏' : '收藏' }}</button>
        <template v-if="a.can_write"><input class="in grow" v-model="draft" maxlength="300" placeholder="写评论" @keyup.enter="comment"><button class="btn primary sm" :disabled="busy || !draft.trim()" @click="comment">发表</button></template>
        <span class="tiny muted grow" v-else>当前角色只读</span></div></template></drawer>`,
    data: () => ({ a: null, polish: false, sel: null, mk: null, note: "", pub: false, busy: false, sumBusy: false, draft: "", flash: "", store }),
    watch: { postKey: { immediate: true, handler() { this.load(); } } },
    unmounted() { clearInterval(this.sumT); },
    methods: {
      ago,
      async load() { this.a = null; try { this.a = await GET("/api/social/article?key=" + encodeURIComponent(this.postKey)); } catch (e) { toast(e.message, true); this.$emit("close"); } },
      labelAt(p) { const i = this.a.paragraphs.indexOf(p); return i === 0 || this.a.paragraphs[i - 1].label !== p.label; },
      jump(id) { const el = document.getElementById("rd-" + id); if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); this.flash = id; setTimeout(() => (this.flash = ""), 1600); } },
      quoted(p) { return this.a.comments.filter((c) => c.quote && p.text.includes(c.quote)); },
      // 把原文按“我的重点 / 他人公开的重点”切成片段（同一段里按出现位置依次标记，不重叠）
      segs(p) {
        const marks = [];
        for (const h of this.a.highlights.mine) if (h.para === p.id) marks.push({ q: h.quote, cls: "mine", h });
        for (const o of this.a.highlights.others) if (o.para === p.id) marks.push({ q: o.quote, cls: "other", n: o.n });
        const hits = [];
        for (const m of marks) { const i = p.text.indexOf(m.q); if (i >= 0) hits.push({ i, e: i + m.q.length, m }); }
        hits.sort((x, y) => x.i - y.i);
        const out = []; let pos = 0;
        for (const h of hits) {
          if (h.i < pos) continue;
          if (h.i > pos) out.push({ t: p.text.slice(pos, h.i) });
          out.push({ t: p.text.slice(h.i, h.e), cls: h.m.cls, h: h.m.h, n: h.m.n }); pos = h.e;
        }
        if (pos < p.text.length) out.push({ t: p.text.slice(pos) });
        return out.length ? out : [{ t: p.text }];
      },
      onSel() {
        setTimeout(() => {
          const s = window.getSelection();
          if (!s || s.isCollapsed || !this.a) return;
          const q = s.toString().trim();
          const node = s.anchorNode && (s.anchorNode.nodeType === 3 ? s.anchorNode.parentElement : s.anchorNode);
          const pEl = node && node.closest ? node.closest(".rd-p") : null;
          const end = s.focusNode && (s.focusNode.nodeType === 3 ? s.focusNode.parentElement : s.focusNode);
          if (!q || !pEl || !end || end.closest(".rd-p") !== pEl) return;
          const para = this.a.paragraphs.find((x) => x.id === pEl.dataset.pid);
          if (!para || !para.text.includes(q)) return;
          const r = s.getRangeAt(0).getBoundingClientRect();
          this.mk = null; this.note = ""; this.pub = false;
          this.sel = { para: para.id, quote: q.slice(0, 200), x: Math.max(8, Math.min(window.innerWidth - 270, r.left)), y: Math.min(window.innerHeight - 190, r.bottom + 8), form: "" };
        }, 30);
      },
      closeSel() { this.sel = null; const s = window.getSelection(); if (s) s.removeAllRanges(); },
      async commit() {
        this.busy = true;
        try {
          if (this.sel.form === "hl") {
            const h = await POST("/api/social/highlights", { key: this.postKey, para: this.sel.para, quote: this.sel.quote, note: this.note, public: this.pub });
            this.a.highlights.mine = this.a.highlights.mine.filter((x) => x.id !== h.id).concat(h); toast("已划重点");
          } else {
            const c = await POST("/api/feed/comments", { key: this.postKey, text: this.note, quote: this.sel.quote });
            this.a.comments.push(c); this.$emit("changed");
          }
          this.closeSel();
        } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      markClick(s, p) {
        const el = event.target.getBoundingClientRect();
        const pos = { x: Math.max(8, Math.min(window.innerWidth - 270, el.left)), y: Math.min(window.innerHeight - 150, el.bottom + 6) };
        this.sel = null; this.mk = s.cls === "mine" ? { ...pos, mine: true, h: s.h } : { ...pos, mine: false, n: s.n };
      },
      async togglePub() {
        try { const h = await PUT("/api/social/highlights/" + this.mk.h.id, { public: !this.mk.h.public }); Object.assign(this.mk.h, h); } catch (e) { toast(e.message, true); }
      },
      async delHl() {
        try { await DEL("/api/social/highlights/" + this.mk.h.id); this.a.highlights.mine = this.a.highlights.mine.filter((x) => x.id !== this.mk.h.id); this.mk = null; } catch (e) { toast(e.message, true); }
      },
      async summarize(force) {
        this.sumBusy = true;
        try { const r = await POST("/api/social/article/summary", { key: this.postKey, force }); this.a.summary = r.summary; if (r.pending) this.watchSummary(); }
        catch (e) { toast(e.message, true); } finally { this.sumBusy = false; }
      },
      // 先看到按原文摘录的总结；智能体润色在后台进行，完成后自动替换
      watchSummary() {
        clearInterval(this.sumT); let n = 0; this.polish = true;
        this.sumT = setInterval(async () => {
          n++;
          try {
            const r = await GET("/api/social/article/summary?key=" + encodeURIComponent(this.postKey));
            if (r.summary) this.a.summary = r.summary;
            if (!r.pending || n > 60) { clearInterval(this.sumT); this.polish = false; }
          } catch (_) { clearInterval(this.sumT); this.polish = false; }
        }, 4000);
      },
      async fav() { try { this.a.fav = await POST("/api/social/fav", { key: this.postKey }); this.$emit("changed"); } catch (e) { toast(e.message, true); } },
      async comment() {
        if (!this.draft.trim()) return;
        this.busy = true;
        try { this.a.comments.push(await POST("/api/feed/comments", { key: this.postKey, text: this.draft })); this.draft = ""; this.$emit("changed"); }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
    },
  };

  // ---------------------------------------------------------------------- 我的重点列表（收藏分段里）
  const MyHighlights = {
    emits: ["open"],
    template: `<div><div class="empty card" v-if="items && !items.length">还没有划过重点。打开简报（周确认、日志、公告）选中文字即可划重点。</div>
      <div class="card hl-item" v-for="h in items || []" :key="h.id" @click="$emit('open', h.key)">
        <div class="small">「{{ h.quote }}」</div><div class="tiny" v-if="h.note">批注：{{ h.note }}</div>
        <div class="tiny muted"><span class="chip">{{ h.tag }}</span> {{ ago(h.time) }} · {{ h.public ? '已公开' : '仅自己可见' }}</div></div></div>`,
    data: () => ({ items: null }),
    async mounted() { try { this.items = (await GET("/api/social/highlights/mine")).items; } catch (e) { toast(e.message, true); this.items = []; } },
    methods: { ago },
  };

  CircleFeed.components = Object.assign(CircleFeed.components || {}, { MentionBtn, OrigCard, RepostBox, ProfilePanel, DmPanel, ReaderPanel, MyHighlights });
})();
