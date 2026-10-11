/* 现场圈：现场所有发布（随手记、随手拍、整改、周确认）自动汇成动态流，可点赞、评论。
   Agent 化增强：Agent 同事点评（带依据）、一张照片的 Agent 接力时间线、评论里 @工笔 就地问答、
   港湾公告（置顶/已读回执）、AI精选（Agent 从真实动态里挑选并写理由）。公告发布框 notice-composer 在 x_circle.js 注册；
   顶部分段“讨论组”（话题/问题/专题小组）的 talk-board 在 x_talk.js 注册。 */

const AGO_UNITS = [[86400, "天"], [3600, "小时"], [60, "分钟"]];
// 相对时间按服务器校正后的时钟算（core.js 的 serverNow()），手机时钟不准时不会把旧动态写成“刚刚”；没有该函数或取不到时退回浏览器时钟
function circleNow() {
  try { if (typeof serverNow === "function") { const t = +serverNow(); if (t) return t; } } catch (_) { /* 退回浏览器时钟 */ }
  return Date.now();
}
function ago(iso) {
  const t = new Date(iso).getTime();
  if (!t) return "";
  const s = Math.max(0, (circleNow() - t) / 1000);
  if (s < 60) return "刚刚";
  if (s >= 86400 * 7) return fmtDT(iso);
  const u = AGO_UNITS.find((x) => s >= x[0]);
  return `${Math.floor(s / u[0])}${u[1]}前`;
}

// 分包岗位看 Agent 点评、依据、接力时不显示作业编码（如 RD220），只显示作业名。
// 编码→名称取自本页已加载的“作业”依据（依据文字以“编码 名称，…”开头、标签就是编码）；对不上名称的编码原样保留，不猜。
function actLearn(map, label, text) {
  const isCode = (x) => /^[A-Z]{1,4}\d{2,6}$/.test(x);
  if (!label || !text || !isCode(label) || !text.startsWith(label + " ")) return map;
  const name = text.slice(label.length + 1).split(/[，,。；;：:（(]/)[0].trim();
  if (name && !isCode(name)) map[label] = name;
  return map;
}
function actName(s, map) {
  if (!s) return s;
  const esc = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  for (const [code, name] of Object.entries(map || {})) {
    const c = esc(code), n = esc(name);
    s = s.replace(new RegExp(`\\b${c}\\b\\s*(?=${n})`, "g"), "")
      .replace(new RegExp(`(${n})\\s*[（(]${c}[）)]`, "g"), "$1")
      .replace(new RegExp(`\\b${c}\\b`, "g"), name);
  }
  // 名称后括注的编码（“基槽开挖（A1020）”）去掉括注；日志/照片/问答引用（L12、D2、P1、F1）不动
  return s.replace(/[（(](?![LDPF]\d)[A-Z]{1,4}\d{2,6}[）)]/g, "");
}

// 一条评论（人或智能体）：动态下就地显示与“全部评论”抽屉共用；抽屉里不放“回复”（回复在动态下的输入框）
const CM_TPL = (inDw) => `<div :class="['cm', 'small', c.agent ? 'ag-cm' : '']">
        <template v-if="c.agent"><span class="ag-ic"><ic :n="c.agent.icon"></ic></span>
          <div class="ag-body"><div><b>{{ c.name }}</b> <span class="chip ag-chip">{{ c.agent.qa ? '智能体 · 回答' : '智能体 · 点评' }}</span>
            <span class="chip" v-if="c.agent.audience === 'harbour'">仅港湾可见</span> <span class="tiny muted">{{ agentAt(p, c) }}</span></div>
            <div class="ag-q tiny muted" v-if="c.agent.qa && c.agent.question">问：{{ c.agent.question }}</div>
            <div class="ag-stale tiny" v-if="c.agent.stale">情况已变化，此点评已过时 <a @click="evOpen['s' + c.id] = !evOpen['s' + c.id]">{{ evOpen['s' + c.id] ? '收起原文 ▴' : '看原文 ▾' }}</a></div>
            <div :class="['pre', 'ag-txt', c.agent.stale ? 'muted ag-stale-text' : '', {open: evOpen['t' + c.id]}]" v-if="!c.agent.stale || evOpen['s' + c.id]"
              :title="evOpen['t' + c.id] ? '' : '点击看全文'" @click="evOpen['t' + c.id] = !evOpen['t' + c.id]">{{ tx(c.text) }}</div>
            <div class="tiny muted" v-if="c.agent.qa && c.agent.scope === 'post'">分包账号：仅依据本动态可见内容回答</div>
            <div class="row wrap"><a class="tiny" v-if="c.agent.evidence.length" @click="evOpen[c.id] = !evOpen[c.id]">{{ c.agent.qa ? '出处' : '依据' }}（{{ c.agent.evidence.length }}）{{ evOpen[c.id] ? '▴' : '▾' }}</a>
              <a class="tiny" v-if="c.agent.mine" @click="delAns(p, c)">删除</a></div>
            <ul class="ag-ev" v-if="evOpen[c.id]"><li v-for="(e, i) in c.agent.evidence" :key="i">{{ tx(e.text) }}
              <a v-if="e.link" @click="go(e.link.route, e.link.params)"> {{ tx(e.label) || '查看' }} →</a></li></ul></div></template>
        <template v-else><span class="chip orange" v-if="c.featured">精选</span> <b>{{ c.name }}</b><span class="tiny muted">（{{ c.role_name }}）</span><span class="cm-re" v-if="c.reply_to"> 回复 <b>{{ c.reply_to.name }}</b></span>：
          <span class="cm-quote" v-if="c.quote">「{{ c.quote }}」</span><template v-for="(sg, i) in atParts(c.text)" :key="i"><b v-if="sg.at" class="at">{{ sg.t }}</b><template v-else>{{ sg.t }}</template></template>
          <span class="tiny muted">{{ ago(c.time) }}</span>${inDw ? "" : '<a class="tiny" v-if="canWrite" @click="reply(p, c)"> 回复</a>'}
          <a class="tiny" v-if="social && meta.can_publish" @click="feature(p, c)"> {{ c.featured ? '取消精选' : '设为精选' }}</a><a class="tiny" v-if="c.mine" @click="delCmt(p, c)"> 删除</a></template></div>`;

const CircleFeed = {
  template: `<div class="circle"><page-head page="circle"><template #meta><template v-if="d">{{ d.scope }}</template></template><template #actions><div class="seg"><button v-for="v in views" :key="v[0]" :class="{on: view === v[0]}" @click="setView(v[0])">{{ v[1] }}</button></div>
      <button class="btn sm" v-if="social && canWrite" @click="openDm()"><ic n="message"></ic> 私信<span class="badge-dot" v-if="dmUnread">{{ dmUnread }}</span></button>
      <button class="btn sm" v-if="meta.can_publish" @click="compose = !compose"><ic n="megaphone"></ic> 发公告</button>
      <button class="btn sm" v-if="agentOn && canWrite && view !== 'talk'" :disabled="rv.busy" @click="runReview" title="让智能体为当前列出的动态补点评">{{ rv.busy ? '点评中 · 已 ' + rv.sec + ' 秒' : 'AI 点评' }}</button>
      <button class="btn sm" :disabled="busy" @click="load()">刷新</button></template></page-head>
  <div class="banner warn" v-if="rv.err">{{ rv.err }} <a @click="runReview">重试</a></div>
  <div class="tiny muted mb" v-else-if="rv.done">{{ rv.done }}</div>
  <notice-composer v-if="compose && meta.can_publish" :companies="meta.companies" @done="onPublished" @cancel="compose = false"></notice-composer>
  <div class="card cf-board talk-board" v-if="view === 'talk'" v-fit="300"><talk-board :gid="talkG" :tick="talkTick" @open="openTalk"></talk-board></div>
  <template v-else>
  <div class="place-chip chip blue" v-if="place"><ic n="map-pin"></ic> 同地点：{{ place.label }} <a @click="clearPlace">✕ 返回全部</a></div>
  <div class="place-chip chip blue" v-if="view === 'notes_today'"><ic n="mic"></ic> 只看今日随手记 <a @click="setView('')">✕ 返回全部</a></div>
  <div class="seg mb" v-if="social && view === 'fav'"><button :class="{on: favTab === 'posts'}" @click="favTab = 'posts'">收藏的动态</button><button :class="{on: favTab === 'hl'}" @click="favTab = 'hl'">我的重点</button></div>
  <my-highlights v-if="social && view === 'fav' && favTab === 'hl'" @open="openReader"></my-highlights>
  <load-state v-else-if="!d" loading :error="err" @retry="load()"></load-state>
  <template v-else>
  <div class="card cf-board" v-if="posts.length" v-fit="300"><template v-for="(p, i) in posts" :key="p.key">
  <div class="cf-day" v-if="dayHead(i)">{{ dayText(p) }}</div>
  <article :class="['post', 'k-' + p.kind, p.pinned ? 'pinned' : '']">
    <div class="row post-hd"><span :class="['av', p.author.agent ? 'ag' : (p.author.id ? 'click' : '')]" @click="openProfile(p.author)"><ic v-if="p.author.agent" :n="p.author.icon"></ic><template v-else>{{ p.author.name.slice(0, 1) }}</template></span>
      <div class="grow"><div><b>{{ p.author.name }}</b> <span :class="['chip', p.author.agent ? 'ag-chip' : 'blue']">{{ p.tag }}</span> <span class="chip blue pick-chip" v-if="p.ai_pick"><ic n="bot"></ic> AI精选</span></div>
        <div class="tiny muted">{{ p.author.role_name }}<template v-if="p.author.company"> · {{ p.author.company }}</template> · {{ ago(p.time) }}</div></div></div>
    <zb-clamp class="post-tx small" v-if="p.text" :lines="3" pre :title="p.author.name + ' · ' + p.tag"><template v-for="(sg, i) in atParts(p.text)" :key="i"><b v-if="sg.at" class="at">{{ sg.t }}</b><template v-else>{{ sg.t }}</template></template></zb-clamp>
    <zb-clamp class="post-pick tiny" v-if="p.ai_pick" :lines="2" title="AI 精选理由"><b>AI 精选理由：</b>{{ tx(p.ai_pick.reason) }} <span class="muted">（{{ p.ai_pick.source === 'ai' ? 'AI 生成' : '规则生成' }}）</span></zb-clamp>
    <orig-card v-if="p.kind === 'repost'" :orig="p.orig"></orig-card>
    <div class="notice" v-if="p.kind === 'notice'">
      <div class="nt-t"><b>{{ p.notice.title }}</b> <span class="chip">{{ p.notice.scope_label }}</span></div>
      <zb-clamp class="post-tx small" :text="p.notice.body" :lines="3" pre :title="p.notice.title"></zb-clamp>
      <div class="row wrap nt-ft"><span :class="['chip', p.notice.read_me || p.author.mine ? 'green' : 'orange']"><template v-if="p.notice.read_me || p.author.mine"><ic n="check"></ic> 已阅</template><template v-else>未阅</template></span>
        <button class="btn sm" v-if="!p.notice.read_me && !p.author.mine && canRead" @click="markRead(p)">已阅</button>
        <template v-if="p.notice.can_manage"><a class="small" @click="toggleReads(p)">已读 {{ p.notice.stats.read }}/{{ p.notice.stats.total }} · 名单 {{ readsOpen[p.key] ? '▴' : '▾' }}</a>
          <a class="small" @click="pin(p)">{{ p.pinned ? '取消置顶' : '置顶' }}</a><a class="small" @click="withdraw(p)">撤回</a></template></div>
      <div class="nt-reads" v-if="readsOpen[p.key]"><div v-if="!reads[p.key]" class="tiny muted">加载中</div><template v-else>
        <div class="small"><b>未读（{{ reads[p.key].unread.length }}）</b>：<span v-if="!reads[p.key].unread.length" class="muted">全部已读</span>
          <span v-for="(u, i) in reads[p.key].unread" :key="u.id">{{ i ? '、' : '' }}{{ u.name }}<span class="tiny muted">（{{ u.company || u.role_name }}）</span></span></div>
        <div class="small mt"><b>已读（{{ reads[p.key].read.length }}）</b>：<span v-if="!reads[p.key].read.length" class="muted">暂无</span>
          <span v-for="(u, i) in reads[p.key].read" :key="u.id">{{ i ? '、' : '' }}{{ u.name }}<span class="tiny muted"> {{ ago(u.read_at) }}</span></span></div></template></div></div>
    <div :class="['post-geo', p.geo ? '' : 'none']" v-if="p.geo_slot"><ic n="map-pin"></ic> <a v-if="p.geo" title="看同一地点的动态" @click="samePlace(p)">{{ geoText(p.geo) }}</a><template v-else>{{ geoText(p.geo) }}</template></div>
    <div :class="[p.compare ? 'ev-pair' : p.photos.length > 1 ? 'ev-grid' : 'rs-one', 'rs-pics']" v-if="p.photos.length">
      <ev-photo v-for="(ph, i) in p.photos" :key="ph.id" :size="p.photos.length === 1 && !p.compare ? 'l' : 'f'" :src="ph.thumb" :full="ph.url"
        :label="p.compare ? (i === 0 ? '整改前' : '整改后') : ''" :time="ph.taken_at ? '拍摄 ' + fmtDT(ph.taken_at) : ''"></ev-photo></div>
    <div class="post-ai" v-if="p.ai.length"><div class="row wrap" v-for="a in p.ai" :key="a.id"><span class="chip"><ic n="bot"></ic> 安全智能体</span>
      <span :class="['chip', LEVEL_F[a.level]]">{{ { high: '高', medium: '中', low: '低', info: '提示' }[a.level] }}</span>
      <span class="small grow">{{ a.title }}</span><span :class="['chip', a.candidate ? 'orange' : '']">{{ a.candidate ? '候选·待确认' : (FSTATUS[a.status] || a.status) }}</span></div>
      <div class="tiny muted" v-if="p.ai[0].reason">复核：{{ p.ai[0].reason }}</div></div>
    <div class="row post-ops"><button :class="['btn', 'ghost', 'sm', p.likes.me ? 'liked' : '']" :disabled="!canWrite" @click="like(p)"><ic n="heart"></ic> 赞<template v-if="p.likes.n"> {{ p.likes.n }}</template></button>
      <button class="btn ghost sm" :disabled="!canWrite" @click="openCmt(p)"><ic n="message"></ic> 评论<template v-if="p.comments.length"> {{ p.comments.length }}</template></button>
      <button v-if="social" :class="['btn', 'ghost', 'sm', p.fav && p.fav.me ? 'faved' : '']" @click="fav(p)"><ic n="pin"></ic> {{ p.fav && p.fav.me ? '已收藏' : '收藏' }}</button>
      <button class="btn ghost sm" v-if="social && canWrite && p.kind !== 'repost'" @click="repostOpen = repostOpen === p.key ? '' : p.key"><ic n="send"></ic> 转发<template v-if="p.reposts"> {{ p.reposts }}</template></button>
      <button class="btn ghost sm" v-if="social && p.article" @click="openReader(p.key)"><ic n="book"></ic> 阅读</button>
      <button class="btn ghost sm" v-if="social && canWrite && p.author.id && !p.author.mine && !p.author.agent" @click="openDm(p.author.id, p.key)"><ic n="message"></ic> 私信</button>
      <button class="btn ghost sm" v-if="agentOn && RELAY_KINDS.includes(p.kind)" @click="toggleRelay(p)"><ic n="link"></ic> 智能体接力 {{ relayOpen[p.key] ? '▴' : '▾' }}</button>
      <div class="grow"></div><a class="small" v-if="p.link" @click="go(p.link.route, p.link.params)">查看详情 →</a></div>
    <repost-box v-if="social && repostOpen === p.key" :post="p" @cancel="repostOpen = ''" @done="onReposted"></repost-box>
    <div class="relay" v-if="relayOpen[p.key]"><div v-if="!relay[p.key]" class="tiny muted">加载中</div>
      <div v-else-if="!relay[p.key].length" class="tiny muted">暂无智能体处理记录</div>
      <ol class="relay-steps" v-else><li v-for="(st, i) in relay[p.key]" :key="i"><span class="rl-ic"><ic :n="st.icon"></ic></span>
        <div class="rl-b"><div><b>{{ st.name }}</b> <span class="tiny muted" v-if="st.time">{{ ago(st.time) }}</span></div><div class="small">{{ tx(st.text) }}</div>
          <a class="tiny" v-if="st.link" @click="go(st.link.route, st.link.params)">{{ tx(st.label) || '查看' }} →</a></div></li></ol>
      <div class="tiny muted">各步骤均取自系统记录，无记录的环节不显示。</div></div>
    <div class="post-social" v-if="p.likes.n || p.comments.length || cmt === p.key">
      <div class="tiny post-likes" v-if="p.likes.n"><ic n="heart"></ic> {{ p.likes.names.join('、') }}<template v-if="p.likes.n > p.likes.names.length"> 等 {{ p.likes.n }} 人</template></div>
      <template v-for="c in cmtInline(p)" :key="c.id">${CM_TPL(false)}</template>
      <button type="button" class="zb-link cf-cm-all" v-if="cmt !== p.key && p.comments.length > cmtInline(p).length" @click="cmtAll = p.key">查看全部 {{ p.comments.length }} 条评论<ic n="chevron"></ic></button>
      <zb-drawer v-if="cmtAll === p.key" :title="'全部评论 · ' + p.comments.length + ' 条'" @close="cmtAll = ''">
        <zb-paged :items="p.comments" :size="12" :fit="false" unit="条" cls="cm-list">
          <template #default="{ item: c }">${CM_TPL(true)}</template></zb-paged></zb-drawer>
      <div class="reply-chip" v-if="cmt === p.key && replyTo && replyTo.key === p.key">回复 {{ replyTo.name }} <a @click="replyTo = null">✕</a></div>
      <div class="row mt" v-if="cmt === p.key"><input class="in grow" v-model="draft" maxlength="300" :placeholder="replyTo && replyTo.key === p.key ? '回复 ' + replyTo.name : (agentOn ? ASK_HINT : '输入评论')" @keyup.enter="send(p)" ref="cin">
        <mention-btn @pick="addAt"></mention-btn>
        <button class="btn sm" v-if="agentOn" title="向工笔提问" @click="askMention">@工笔</button>
        <button class="btn primary sm" :disabled="!draft.trim() || busy" @click="send(p)">{{ isAsk ? '提问' : '发送' }}</button></div>
      <div class="tiny muted ag-tip" v-if="cmt === p.key && isAsk">{{ asking ? '工笔检索记录中' : askTip }}</div></div>
  </article></template>
  <div class="row cf-more" ref="tail" v-if="d.next"><button class="btn" :disabled="busy" @click="more">{{ busy ? '加载中' : '加载更早动态' }}</button></div>
  <div class="tiny muted cf-end" v-else-if="posts.length > 5">没有更早的动态了</div></div>
  <div class="empty card" v-if="!posts.length">{{ emptyText }}</div>
  </template></template>
  <profile-panel v-if="drawer && drawer.type === 'profile'" :uid="drawer.id" :social="social" @close="drawer = null" @dm="(id) => openDm(id)" @place="applyPlace" @open="load(true)"></profile-panel>
  <dm-panel v-if="social && drawer && drawer.type === 'dm'" :peer="drawer.id" :ref-key="drawer.ref" @close="closeDm" @unread="onUnread"></dm-panel>
  <reader-panel v-if="social && drawer && drawer.type === 'reader'" :post-key="drawer.key" @close="drawer = null" @changed="load(true)"></reader-panel>
  </div>`,
  data: () => ({ rv: { busy: false, sec: 0, err: "", done: "" }, d: null, err: null, posts: [], view: "", place: null, favTab: "posts", repostOpen: "", replyTo: null, drawer: null, dmUnread: 0,
    VIEWS: [["", "全部"], ["ai", "AI精选"], ["talk", "讨论组"], ["follow", "关注"], ["mine", "我发的"], ["fav", "收藏"]], talkG: 0, talkTick: 0, busy: false, cmt: "", draft: "", store, LEVEL_F, timer: null,
    meta: { can_publish: false, companies: [] }, compose: false, asking: false,
    // 手机：首屏只放前几条动态，“查看更多”每次多放 5 条（电脑端整条动态流在固定高度面板内滚动，见 v-fit）
    narrow: window.innerWidth < 861, cmtAll: "",
    relayOpen: {}, relay: {}, evOpen: {}, readsOpen: {}, reads: {}, RELAY_KINDS: ["note", "snap", "rect", "log"],
    ASK_HINT: "输入评论，@工笔 可提问",
    FSTATUS: { open: "待整改", rectifying: "待复核", resolved: "已闭环", rejected: "复核未通过", dismissed: "已忽略" } }),
  computed: {
    canWrite() { return !["leader", "admin"].includes(roleOf()); },
    // 社交层（关注/收藏/转发/私信/划重点）默认关闭，管理员在“项目与规则”开启；点赞、评论、Agent 点评、公告不受影响
    social() { const p = store.me && store.me.project; return !!(p && p.config && p.config.social_enabled); },
    // 项目设置里隐藏“现场圈 Agent”后：不显示 Agent 点评、接力时间线、AI精选与 @工笔（动态、点赞、评论、公告照旧）
    agentOn() { return featOn("circle_agent"); },
    // AI精选、讨论组对所有人可见，不受社交层开关影响
    views() { return (this.social ? this.VIEWS : this.VIEWS.filter((v) => ["", "ai", "talk"].includes(v[0]))).filter((v) => v[0] !== "ai" || this.agentOn); },
    mine() { return this.view === "mine"; },
    emptyText() {
      return this.place ? "这个地点暂时没有其他动态" : this.view === "mine" ? "暂无我发布的内容" : this.view === "follow" ? "还没有关注的人发布内容，点头像可以关注同事"
        : this.view === "fav" ? "还没有收藏。点动态下方的“收藏”即可"
        : this.view === "ai" ? "暂无精选：只有出现已闭环整改、已派单的高/中风险隐患或低风险周确认时才会挑选" : "暂无发布内容";
    },
    canRead() { return roleOf() !== "admin"; },
    isAsk() { return this.agentOn && this.draft.trim().startsWith("@工笔"); },
    askTip() { return isSubOrg() ? "工笔仅依据本动态可见内容回答，并标注依据。" : "工笔查询项目记录并标注出处；回答仅港湾岗位可见。"; },
    // 本页已加载的作业编码→名称（来自 Agent 依据与接力记录），只给分包岗位做显示替换
    actMap() {
      const m = {};
      for (const p of this.posts) for (const c of p.comments) if (c.agent) for (const e of c.agent.evidence || []) actLearn(m, e.label, e.text);
      for (const steps of Object.values(this.relay)) for (const st of steps || []) actLearn(m, st.label, st.text);
      return m;
    },
  },
  created() {
    // 朋友圈式连续下滑（RJ 10-10 例外：现场圈不分页）：电脑端在面板内、手机端在页面上滚到底部附近自动取更早的动态
    this.io = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting) && this.d && this.d.next && !this.busy && this.view !== "talk") this.more(); }, { rootMargin: "0px 0px 240px 0px" });
    this.seen = new Set(); this.sentRead = new Set(); this.fromRoute(); if (!this.agentOn && this.view === "ai") this.view = ""; },
  // 消息里的讨论组链接 #/circle?view=talk&g=ID：在现场圈页上点也要切过去
  watch: { "store.route.params"() { this.fromRoute(true); } },
  async mounted() {
    try { this.meta = await GET("/api/circle/meta"); } catch (_) { /* 取不到就不显示发布入口 */ }
    await this.load();
    this.onR = () => { this.narrow = window.innerWidth < 861; }; window.addEventListener("resize", this.onR);
    if (this.social) { this.pollDm(); this.dmTimer = setInterval(() => { if (!document.hidden) this.pollDm(); }, 30000); }
    // 安全Agent 识别、他人新发布与评论需要自动出现；正在评论或已翻页时不整页刷新，免得打断
    this.timer = setInterval(() => { if (!document.hidden && !this.cmt && this.view !== "talk" && this.posts.length <= 20) this.load(true); }, 30000);
  },
  updated() { const t = this.$refs.tail; if (t !== this.ioEl) { if (this.ioEl) this.io.unobserve(this.ioEl); this.ioEl = t; if (t) this.io.observe(t); } },
  unmounted() { clearInterval(this.timer); clearInterval(this.dmTimer); window.removeEventListener("resize", this.onR); if (this.io) this.io.disconnect(); },
  methods: {
    go, ago, geoText, fmtDT,
    // 评论就地只露前 2 条（手机 1 条）；正在评论时露最近 3 条（含刚发的）；全部评论在抽屉里按 12 条分页
    cmtInline(p) { const c = p.comments; return this.cmt === p.key ? c.slice(-3) : c.slice(0, this.narrow ? 1 : 2); },
    // 动态流按日期分组：第一条或与上一条不是同一天时出日期标题（置顶公告不出标题）
    dayHead(i) { const p = this.posts[i]; if (p.pinned) return false; return i === 0 || this.posts[i - 1].pinned || this.dayText(this.posts[i - 1]) !== this.dayText(p); },
    dayText(p) { if (!p || !p.time) return ""; const d = fmtDT(p.time).slice(0, 10), t = fmtDT(new Date(circleNow()).toISOString()).slice(0, 10); return d === t ? "今天" : d; },
    // Agent 点评比动态晚一天以上（后来补评的）：写“补评于 MM-DD”，不在旧动态下写“1小时前”（U-20）
    agentAt(p, c) { const late = new Date(c.time) - new Date(p.time) > 86400000; return late ? "补评于 " + fmtDT(c.time).slice(5, 10) : ago(c.time); },
    tx(s) { return isSubOrg() ? actName(s, this.actMap) : s; },  // 分包岗位：作业编码换成作业名
    fromRoute(reload) {
      const q = (store.route && store.route.params) || {};
      if (q.view === "notes_today") { this.view = "notes_today"; this.place = null; if (reload) this.load(); return; }
      if (q.dm && this.social) { this.openDm(+q.dm || null); return; }   // 私信通知直达对话（验收 I8）
      if (q.view !== "talk") return;
      this.view = "talk"; this.talkG = +q.g || 0; this.place = null;
      if (reload) this.talkTick++;
    },
    openTalk(id) { this.talkG = id || 0; window.scrollTo(0, 0); },
    async load(quiet) {
      if (this.view === "talk") { this.talkTick++; return; }   // 讨论组自己取数
      if (!quiet) this.busy = true;
      try { this.d = await GET(`/api/feed?limit=20${this.qs()}`); this.err = null; this.posts = this.hideAgent(this.d.posts); window.__circlePosts = this.posts; }
      catch (e) { if (!this.d) this.err = e; else if (!quiet) toast(e.message, true); } finally { this.busy = false; }
      this.afterLoad();
    },
    // 新动态：公告记已读。Agent 同事点评不再打开即生成，人点“AI 点评”才调用（RJ 10-11 决策，同验收 I9/I10 的做法）
    afterLoad() { this.markNotices(); },
    async runReview() {
      if (this.rv.busy) return;
      this.rv = { busy: true, sec: 0, err: "", done: "" }; this.seen = new Set();
      const t = setInterval(() => { this.rv.sec++; }, 1000);
      try { const n = await this.review(0, true); this.rv.done = n ? `已补 ${n} 条点评` : "没有需要补点评的动态"; }
      catch (e) { this.rv.err = "AI 点评没有生成：" + (e.message || "请稍后重试"); }
      finally { clearInterval(t); this.rv.busy = false; }
    },
    hideAgent(ps) { if (!this.agentOn) ps.forEach((p) => { p.comments = p.comments.filter((c) => !c.agent); p.ai_pick = null; }); return ps; },
    // 只由 runReview（人点“AI 点评”）调用；返回新增的点评条数，失败抛出由按钮旁如实显示
    async review(round) {
      if (!this.agentOn || !this.canWrite) return 0;   // 领导、管理员只读，不触发 Agent 点评生成
      const keys = this.posts.filter((p) => this.RELAY_KINDS.includes(p.kind) && !this.seen.has(p.key)).map((p) => p.key).slice(0, 30);
      if (!keys.length || round > 4) return 0;
      keys.forEach((k) => this.seen.add(k));
      const r = await POST("/api/circle/review", { keys });
      let n = 0;
      for (const [k, cs] of Object.entries(r.comments || {})) {
        const p = this.posts.find((x) => x.key === k);
        if (!p) continue;
        const add = cs.filter((c) => !p.comments.some((x) => x.id === c.id)); n += add.length;
        p.comments = p.comments.concat(add).sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : (a.agent ? 1 : 0) - (b.agent ? 1 : 0)));
      }
      if (r.picked) this.load(true);
      else if ((r.left || []).length) { r.left.forEach((k) => this.seen.delete(k)); n += await this.review(round + 1); }
      return n;
    },
    markNotices() {
      if (!this.canRead) return;
      for (const p of this.posts) if (p.kind === "notice" && !p.notice.read_me && !p.author.mine) this.markRead(p, true);
    },
    async markRead(p, quiet) {
      if (quiet && this.sentRead.has(p.key)) return;
      this.sentRead.add(p.key);
      try { await POST(`/api/circle/notices/${p.notice.id}/read`); p.notice.read_me = true; }
      catch (e) { this.sentRead.delete(p.key); if (!quiet) toast(e.message, true); }
    },
    async toggleReads(p) {
      this.readsOpen[p.key] = !this.readsOpen[p.key];
      if (this.readsOpen[p.key]) { try { this.reads[p.key] = await GET(`/api/circle/notices/${p.notice.id}/reads`); } catch (e) { this.readsOpen[p.key] = false; toast(e.message, true); } }
    },
    async pin(p) { try { await POST(`/api/circle/notices/${p.notice.id}/pin`, { pinned: !p.pinned }); await this.load(true); } catch (e) { toast(e.message, true); } },
    async withdraw(p) {
      if (!confirm("撤回后成员不可见，确认撤回该公告？")) return;
      try { await DEL(`/api/circle/notices/${p.notice.id}`); await this.load(true); toast("已撤回"); } catch (e) { toast(e.message, true); }
    },
    async onPublished() { this.compose = false; toast("公告已发布"); await this.load(true); },
    async toggleRelay(p) {
      this.relayOpen[p.key] = !this.relayOpen[p.key];
      if (this.relayOpen[p.key] && !this.relay[p.key]) {
        try { this.relay[p.key] = (await GET(`/api/circle/relay?key=${encodeURIComponent(p.key)}`)).steps; }
        catch (e) { this.relayOpen[p.key] = false; toast(e.message, true); }
      }
    },
    async delAns(p, c) { try { await DEL(`/api/circle/agent-comments/${c.agent.raw_id}`); p.comments = p.comments.filter((x) => x.id !== c.id); } catch (e) { toast(e.message, true); } },
    askMention() {
      this.draft = "@工笔 " + this.draft.replace(/^@工笔\s*/, "");
      this.$nextTick(() => { const i = this.$refs.cin; (Array.isArray(i) ? i[0] : i)?.focus(); });
    },
    async more() {
      this.busy = true;
      try { const r = await GET(`/api/feed?limit=20&before=${encodeURIComponent(this.d.next)}${this.qs()}`); this.posts.push(...this.hideAgent(r.posts)); this.d = { ...this.d, next: r.next }; }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
      this.afterLoad();
    },
    qs() {
      const g = this.place;
      if (g) return "&" + [g.place ? "place=" + encodeURIComponent(g.place) : "", g.lat != null ? "lat=" + g.lat + "&lon=" + g.lon : ""].filter(Boolean).join("&");
      return this.view === "mine" ? "&mine=1" : this.view ? "&view=" + this.view : "";
    },
    setView(v) { if (this.view !== v || this.place || (v === "talk" && this.talkG)) { this.view = v; this.place = null; this.cmt = ""; this.replyTo = null; this.talkG = 0; if (v !== "talk") this.load(); } },
    samePlace(p) { const g = p.geo; this.applyPlace({ place: g.place || "", lat: g.lat, lon: g.lon, label: geoText(g) }); },
    applyPlace(g) { this.drawer = null; this.place = { place: g.place || "", lat: g.lat ?? null, lon: g.lon ?? null, label: g.label || g.place }; this.view = ""; this.load(); },
    clearPlace() { this.place = null; this.load(); },
    openProfile(a) { if (a && a.id && !a.agent) this.drawer = { type: "profile", id: a.id }; },
    openDm(id, ref) { this.drawer = { type: "dm", id: id || null, ref: ref || null }; },
    closeDm() { this.drawer = null; this.pollDm(); },
    onUnread(n) { if (n !== null) this.dmUnread = n; else this.pollDm(); },
    async pollDm() { if (!this.social || !this.canWrite) return; try { this.dmUnread = (await GET("/api/social/summary")).dm_unread; } catch (_) { /* 未读数拿不到不打扰 */ } },
    openReader(key) { this.drawer = { type: "reader", key }; },
    async fav(p) { try { p.fav = await POST("/api/social/fav", { key: p.key }); if (this.view === "fav" && !p.fav.me) this.posts = this.posts.filter((x) => x.key !== p.key); } catch (e) { toast(e.message, true); } },
    onReposted() { this.repostOpen = ""; this.load(true); },
    reply(p, c) { this.cmt = p.key; this.replyTo = { key: p.key, id: c.id, name: c.name }; this.$nextTick(() => { const i = this.$refs.cin; (Array.isArray(i) ? i[0] : i)?.focus(); }); },
    addAt(name) { this.draft += (this.draft && !/\s$/.test(this.draft) ? " " : "") + "@" + name + " "; },
    async feature(p, c) {
      try { await POST(`/api/social/comments/${c.id}/feature`, { featured: !c.featured }); c.featured = !c.featured; p.comments = [...p.comments].sort((a, b) => (b.featured ? 1 : 0) - (a.featured ? 1 : 0)); }
      catch (e) { toast(e.message, true); }
    },
    atParts: (t) => window.atParts(t),
    async like(p) { try { p.likes = await POST("/api/feed/like", { key: p.key }); } catch (e) { toast(e.message, true); } },
    openCmt(p) { this.cmt = this.cmt === p.key ? "" : p.key; this.draft = ""; this.replyTo = null; if (this.cmt) this.$nextTick(() => { const i = this.$refs.cin; (Array.isArray(i) ? i[0] : i)?.focus(); }); },
    async send(p) {
      if (!this.draft.trim()) return;
      this.busy = true;
      try {
        if (this.isAsk) {  // @工笔：就地问答，回答作为 Agent 评论出现在本帖下，不跳页
          this.asking = true;
          const r = await POST("/api/circle/ask", { key: p.key, text: this.draft });
          p.comments.push(r.comment, r.answer); this.draft = "";
        } else {
          const re = this.replyTo && this.replyTo.key === p.key ? this.replyTo.id : null;
          p.comments.push(await POST("/api/feed/comments", { key: p.key, text: this.draft, reply_to: re })); this.draft = ""; this.replyTo = null;
        }
      } catch (e) { toast(e.message, true); } finally { this.busy = false; this.asking = false; }
    },
    async delCmt(p, c) { try { await DEL(`/api/feed/comments/${c.id}`); p.comments = p.comments.filter((x) => x.id !== c.id); } catch (e) { toast(e.message, true); } },
  },
};
