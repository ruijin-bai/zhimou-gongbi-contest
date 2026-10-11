/* x_talk：现场圈“讨论组”（RJ 10-09）。话题 / 问题 / 专题三类小组，组内发帖（文字 + 照片）、赞、评论与回复，
   可关联工点或 P6 作业；建组人或港湾管理岗可关闭（问题组 = 标为已解决），港湾管理岗可置顶。
   挂在现场圈顶部分段“讨论组”下，就地切换列表与组内，不跳页；消息里的链接 #/circle?view=talk&g=ID 直接打开某个组。
   权限由后端 /api/talk/* 决定（全项目可见，领导只读，系统管理员不参与）。 */
(function () {
  const KIND_IC = { topic: "message", issue: "alert", special: "book" };

  const TalkBoard = {
    props: ["gid", "tick"],
    emits: ["open"],
    template: `<div class="talk">
    <template v-if="!g">
      <div class="talk-bar row wrap">
        <div class="seg"><button v-for="k in KINDS" :key="k[0]" :class="{on: kind === k[0]}" @click="kind = k[0]; load()">{{ k[1] }}</button></div>
        <div class="seg"><button :class="{on: status === 'open'}" @click="status = 'open'; load()">进行中<template v-if="counts.open"> {{ counts.open }}</template></button>
          <button :class="{on: status === 'closed'}" @click="status = 'closed'; load()">已关闭<template v-if="counts.closed"> {{ counts.closed }}</template></button></div>
        <input class="in talk-q" v-model.trim="q" placeholder="搜名称、工点" @keyup.enter="load()">
        <div class="grow"></div>
        <button class="btn primary sm" v-if="canWrite" @click="openNew"><ic n="plus"></ic> 建讨论组</button></div>
      <div class="talk-new" v-if="creating">
        <div class="row wrap talk-kinds"><label v-for="k in KINDS.slice(1)" :key="k[0]" :class="['talk-kind', nf.kind === k[0] ? 'on' : '']">
          <input type="radio" v-model="nf.kind" :value="k[0]"><ic :n="KIND_IC[k[0]]"></ic> {{ k[1] }}<span class="tiny muted">{{ KIND_TIP[k[0]] }}</span></label></div>
        <input class="in mt" v-model="nf.title" maxlength="40" :placeholder="TITLE_TIP[nf.kind]">
        <textarea class="in mt" v-model="nf.intro" rows="2" maxlength="300" placeholder="说明（可不写）：背景、想讨论什么"></textarea>
        <div class="row wrap mt talk-link">
          <select class="in" v-model="nf.activity_code" @change="pickAct"><option value="">不关联作业</option>
            <option v-for="a in opts.activities" :key="a.code" :value="a.code">{{ a.label }}</option></select>
          <input class="in" v-model="nf.part" maxlength="30" list="talk-parts" placeholder="工点（可不填），如 3#墩"><datalist id="talk-parts"><option v-for="p in opts.parts" :key="p" :value="p"></option></datalist></div>
        <div class="row mt" style="justify-content:flex-end"><button class="btn" @click="creating = false">取消</button>
          <button class="btn primary" :disabled="busy || !nf.title.trim()" @click="create">{{ busy ? '创建中' : '建组' }}</button></div></div>
      <load-state v-if="!list" loading :error="err" @retry="load()"></load-state>
      <template v-else>
        <zb-paged :items="list" :size="10" :m-size="5" :fit="false" unit="个讨论组"><template #default="{ item: x }"><div class="talk-g" @click="$emit('open', x.id)">
          <span :class="['talk-ic', 'k-' + x.kind]"><ic :n="KIND_IC[x.kind]"></ic></span>
          <div class="grow"><div class="talk-t"><b>{{ x.title }}</b> <span class="chip blue">{{ x.kind_name }}</span>
              <span class="chip orange" v-if="x.pinned">置顶</span><span class="chip green" v-if="x.status !== 'open'">{{ x.kind === 'issue' ? '已解决' : '已关闭' }}</span></div>
            <div class="tiny muted"><template v-if="x.part"><ic n="map-pin"></ic> {{ x.part }} · </template><template v-if="x.activity">{{ x.activity.label }} · </template>
              {{ x.author.name }}发起 · {{ x.posts }} 帖<template v-if="x.people"> · {{ x.people }} 人参与</template> · {{ ago(x.last_at) }}</div></div></div></template></zb-paged>
        <div class="empty" v-if="!list.length">{{ emptyText }}</div>
      </template>
    </template>
    <template v-else>
      <div class="talk-hd">
        <a class="small" @click="$emit('open', 0)">‹ 全部讨论组</a>
        <div class="talk-t mt"><span :class="['talk-ic', 'k-' + g.kind]"><ic :n="KIND_IC[g.kind]"></ic></span><b class="talk-h">{{ g.title }}</b>
          <span class="chip blue">{{ g.kind_name }}</span><span class="chip orange" v-if="g.pinned">置顶</span>
          <span class="chip green" v-if="g.status !== 'open'">{{ g.kind === 'issue' ? '已解决' : '已关闭' }}</span></div>
        <div class="small pre mt" v-if="g.intro">{{ g.intro }}</div>
        <div class="tiny muted mt"><template v-if="g.part"><ic n="map-pin"></ic> {{ g.part }} · </template><template v-if="g.activity">作业：{{ g.activity.label }} · </template>
          {{ g.author.name }}（{{ g.author.company || g.author.role_name }}）发起于 {{ fmtDT(g.created_at) }}</div>
        <div class="talk-closed small" v-if="g.closed">{{ g.closed.by }} {{ fmtDT(g.closed.at) }} {{ g.kind === 'issue' ? '标为已解决' : '关闭了讨论' }}<template v-if="g.closed.note">：{{ g.closed.note }}</template></div>
        <div class="row wrap mt talk-acts" v-if="g.can_manage || g.can_pin">
          <button class="btn sm" v-if="g.can_pin && g.status === 'open'" @click="pin">{{ g.pinned ? '取消置顶' : '置顶' }}</button>
          <button class="btn sm" v-if="g.can_manage && g.status === 'open'" @click="close">{{ g.kind === 'issue' ? '标为已解决' : '关闭讨论' }}</button>
          <button class="btn sm" v-if="g.can_manage && g.status !== 'open'" @click="reopen">重新打开</button></div></div>
      <div class="talk-compose" v-if="d.can_write">
        <textarea class="in" v-model="draft" rows="2" maxlength="1000" placeholder="说点什么，@姓名 可提醒同事"></textarea>
        <div class="pics n4 mt" v-if="pics.length"><div class="pic" v-for="(m, i) in pics" :key="m.id"><img :src="m.thumb"><a class="talk-x" @click="pics.splice(i, 1)">✕</a></div></div>
        <div class="row mt"><label class="btn ghost sm"><ic n="camera"></ic> 照片<input type="file" accept="image/*" multiple hidden @change="addPics"></label>
          <span class="tiny muted" v-if="uploading">上传中</span><div class="grow"></div>
          <button class="btn primary sm" :disabled="busy || uploading || (!draft.trim() && !pics.length)" @click="post">发帖</button></div></div>
      <div class="tiny muted talk-ro" v-else-if="g.status !== 'open'">讨论已{{ g.kind === 'issue' ? '解决' : '关闭' }}，只能查看</div>
      <article class="post" v-for="p in d.posts" :key="p.id">
        <div class="row post-hd"><span class="av">{{ p.author.name.slice(0, 1) }}</span>
          <div class="grow"><div><b>{{ p.author.name }}</b></div><div class="tiny muted">{{ p.author.role_name }}<template v-if="p.author.company"> · {{ p.author.company }}</template> · {{ ago(p.time) }}</div></div></div>
        <div class="post-tx small pre" v-if="p.text"><template v-for="(sg, i) in atParts(p.text)" :key="i"><b v-if="sg.at" class="at">{{ sg.t }}</b><template v-else>{{ sg.t }}</template></template></div>
        <div class="ev-grid rs-pics" v-if="p.photos.length">
          <ev-photo v-for="ph in p.photos" :key="ph.id" size="f" :src="ph.thumb" :full="ph.url"></ev-photo></div>
        <div class="row post-ops"><button :class="['btn', 'ghost', 'sm', p.likes.me ? 'liked' : '']" :disabled="!canWrite" @click="like(p)"><ic n="heart"></ic> 赞<template v-if="p.likes.n"> {{ p.likes.n }}</template></button>
          <button class="btn ghost sm" :disabled="!d.can_write" @click="openCmt(p)"><ic n="message"></ic> 评论<template v-if="p.comments.length"> {{ p.comments.length }}</template></button>
          <div class="grow"></div><a class="tiny" v-if="p.author.mine" @click="delPost(p)">删除</a></div>
        <div class="post-social" v-if="p.likes.n || p.comments.length || cmt === p.id">
          <div class="tiny post-likes" v-if="p.likes.n"><ic n="heart"></ic> {{ p.likes.names.join('、') }}<template v-if="p.likes.n > p.likes.names.length"> 等 {{ p.likes.n }} 人</template></div>
          <div class="cm small" v-for="c in p.comments" :key="c.id"><b>{{ c.name }}</b><span class="tiny muted">（{{ c.role_name }}）</span><span class="cm-re" v-if="c.reply_to"> 回复 <b>{{ c.reply_to.name }}</b></span>：{{ c.text }}
            <span class="tiny muted">{{ ago(c.time) }}</span><a class="tiny" v-if="d.can_write" @click="reply(p, c)"> 回复</a><a class="tiny" v-if="c.mine" @click="delCmt(p, c)"> 删除</a></div>
          <div class="reply-chip" v-if="cmt === p.id && replyTo">回复 {{ replyTo.name }} <a @click="replyTo = null">✕</a></div>
          <div class="row mt" v-if="cmt === p.id"><input class="in grow" v-model="cdraft" maxlength="300" :placeholder="replyTo ? '回复 ' + replyTo.name : '输入评论'" @keyup.enter="comment(p)" ref="cin">
            <button class="btn primary sm" :disabled="!cdraft.trim() || busy" @click="comment(p)">发送</button></div></div>
      </article>
      <div class="empty" v-if="!d.posts.length">{{ d.can_write ? '还没有人发言，写下第一帖' : '还没有人发言' }}</div>
      <div class="row mt" style="justify-content:center" v-if="d.next"><button class="btn" :disabled="busy" @click="more">加载更早的帖子</button></div>
    </template></div>`,
    data: () => ({ KINDS: [["", "全部"], ["topic", "话题"], ["issue", "问题"], ["special", "专题"]], KIND_IC,
      KIND_TIP: { topic: "随便聊聊、经验分享", issue: "要解决的事，解决后关闭", special: "围绕一件大事持续讨论" },
      TITLE_TIP: { topic: "话题名称，如：冬季混凝土养护经验", issue: "问题，如：3#墩钢筋到场晚", special: "专题名称，如：雨季施工专题" },
      kind: "", status: "open", q: "", list: null, counts: {}, err: null, canWrite: false,
      creating: false, opts: { parts: [], activities: [] }, nf: { kind: "topic", title: "", intro: "", part: "", activity_code: "" },
      d: null, draft: "", pics: [], uploading: false, cmt: 0, cdraft: "", replyTo: null, busy: false, store }),
    computed: {
      g() { return this.gid && this.d && this.d.group && this.d.group.id === this.gid ? this.d.group : null; },
      emptyText() {
        if (this.q) return "没有匹配的讨论组";
        return this.status === "closed" ? "没有已关闭的讨论组" : this.canWrite ? "还没有讨论组。点“建讨论组”，按话题、问题或专题拉同事一起讨论" : "还没有讨论组";
      },
    },
    watch: { gid() { this.reload(); }, tick() { this.reload(); } },
    mounted() { this.reload(); },
    methods: {
      ago, fmtDT, atParts: (t) => window.atParts(t),
      reload() { if (this.gid) this.open(); else this.load(); },
      async load() {
        try { const r = await GET(`/api/talk/groups?kind=${this.kind}&status=${this.status}&q=${encodeURIComponent(this.q)}`); this.list = r.items; this.counts = r.counts; this.canWrite = r.can_write; this.err = null; }
        catch (e) { this.err = e; }
      },
      async open() {
        try { this.d = await GET(`/api/talk/groups/${this.gid}`); this.canWrite = !["leader", "admin"].includes(roleOf()); this.cmt = 0; this.replyTo = null; }
        catch (e) { toast(e.message, true); this.$emit("open", 0); }
      },
      async more() {
        this.busy = true;
        try { const r = await GET(`/api/talk/groups/${this.gid}?before=${this.d.next}`); this.d.posts.push(...r.posts); this.d.next = r.next; }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async openNew() {
        this.creating = !this.creating;
        if (this.creating && !this.opts.activities.length) { try { this.opts = await GET("/api/talk/options"); } catch (_) { /* 取不到就只能手填工点 */ } }
      },
      pickAct() { const a = this.opts.activities.find((x) => x.code === this.nf.activity_code); if (a && a.part && !this.nf.part) this.nf.part = a.part; },
      async create() {
        this.busy = true;
        try { const g = await POST("/api/talk/groups", this.nf); this.creating = false; this.nf = { kind: "topic", title: "", intro: "", part: "", activity_code: "" }; this.$emit("open", g.id); }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async setStatus(closed, note) { try { this.d = { ...this.d, group: await POST(`/api/talk/groups/${this.gid}/status`, { closed, note }) }; this.open(); } catch (e) { toast(e.message, true); } },
      close() {
        const note = prompt(this.g.kind === "issue" ? "怎么解决的？（可不写）" : "关闭说明（可不写）", "");
        if (note !== null) this.setStatus(true, note);
      },
      reopen() { this.setStatus(false, ""); },
      async pin() { try { this.d = { ...this.d, group: await POST(`/api/talk/groups/${this.gid}/pin`, { pinned: !this.g.pinned }) }; } catch (e) { toast(e.message, true); } },
      async addPics(ev) {
        const files = Array.from(ev.target.files || []).slice(0, 9 - this.pics.length); ev.target.value = "";
        if (!files.length) return;
        this.uploading = true;
        try { const fd = new FormData(); fd.append("purpose", "talk"); addPhotos(fd, files, false); this.pics = this.pics.concat(await POST("/api/media", fd)); }
        catch (e) { toast(e.message, true); } finally { this.uploading = false; }
      },
      async post() {
        this.busy = true;
        try { const p = await POST(`/api/talk/groups/${this.gid}/posts`, { text: this.draft, media_ids: this.pics.map((m) => m.id) }); this.d.posts.unshift(p); this.draft = ""; this.pics = []; }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async delPost(p) { if (!confirm("删除这条帖子？")) return; try { await DEL(`/api/talk/posts/${p.id}`); this.d.posts = this.d.posts.filter((x) => x.id !== p.id); } catch (e) { toast(e.message, true); } },
      async like(p) { try { p.likes = await POST(`/api/talk/posts/${p.id}/like`); } catch (e) { toast(e.message, true); } },
      focus() { this.$nextTick(() => { const i = this.$refs.cin; (Array.isArray(i) ? i[0] : i)?.focus(); }); },
      openCmt(p) { this.cmt = this.cmt === p.id ? 0 : p.id; this.cdraft = ""; this.replyTo = null; if (this.cmt) this.focus(); },
      reply(p, c) { this.cmt = p.id; this.replyTo = { id: c.id, name: c.name }; this.focus(); },
      async comment(p) {
        if (!this.cdraft.trim()) return;
        this.busy = true;
        try { const v = await POST(`/api/talk/posts/${p.id}/comments`, { text: this.cdraft, reply_to: this.replyTo ? this.replyTo.id : null }); p.comments = v.comments; this.cdraft = ""; this.replyTo = null; }
        catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
      async delCmt(p, c) { try { p.comments = (await DEL(`/api/talk/comments/${c.id}`)).comments; } catch (e) { toast(e.message, true); } },
    },
  };
  // circle.js 先于本文件加载：把讨论组挂到现场圈组件上
  CircleFeed.components = Object.assign(CircleFeed.components || {}, { TalkBoard });
})();
