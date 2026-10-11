/* x_circle：现场圈 Agent 化的子组件。
   现场圈主体在 circle.js（Agent 同事点评、接力时间线、@工笔、公告卡、AI精选徽标与理由）；
   这里提供“发公告”输入框 notice-composer，就地展开在现场圈顶部，发布后不跳页。 */
(function () {
  const NoticeComposer = {
    props: ["companies"],
    emits: ["done", "cancel"],
    template: `<div class="card notice-composer">
      <div class="row between wrap"><b><ic n="megaphone"></ic> 发布公告</b><help-tip text="成员打开现场圈即记已读，可查看已读名单"></help-tip></div>
      <div class="mt"><label class="f">标题</label><input class="in" v-model="title" maxlength="40" placeholder="如：明日全场停电检修"></div>
      <div class="mt"><label class="f">内容</label><textarea class="in" v-model="body" rows="4" maxlength="500" placeholder="时间、范围、要求"></textarea></div>
      <div class="row wrap mt">
        <label class="f nc-f">发布范围</label>
        <select class="in nc-sel" v-model="audience"><option value="all">全项目</option><option value="company" :disabled="!companies.length">某分包单位</option></select>
        <select class="in nc-sel" v-if="audience === 'company'" v-model="company"><option value="" disabled>选择分包单位</option><option v-for="c in companies" :key="c" :value="c">{{ c }}</option></select>
        <label class="nc-pin"><input type="checkbox" v-model="pinned"> 置顶</label>
      </div>
      <div class="row mt" style="justify-content:flex-end"><button class="btn" @click="$emit('cancel')">取消</button>
        <button class="btn primary" :disabled="busy || !title.trim() || !body.trim() || (audience === 'company' && !company)" @click="submit">{{ busy ? '发布中' : '发布公告' }}</button></div>
    </div>`,
    data: () => ({ title: "", body: "", audience: "all", company: "", pinned: false, busy: false }),
    methods: {
      async submit() {
        this.busy = true;
        try {
          await POST("/api/circle/notices", { title: this.title, body: this.body, pinned: this.pinned, audience: this.audience, company: this.audience === "company" ? this.company : "" });
          this.$emit("done");
        } catch (e) { toast(e.message, true); } finally { this.busy = false; }
      },
    },
  };
  // circle.js 先于本文件加载：把子组件挂到现场圈组件上
  CircleFeed.components = Object.assign(CircleFeed.components || {}, { NoticeComposer });
})();
