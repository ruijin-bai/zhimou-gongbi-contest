/* x_demo：竞赛演示账号的岗位切换条（规则见 services/demo.py）。
   只有 .env 里 ZB_DEMO_USERS 列出的账号才有 store.me.demo，其他账号不显示；切换后回到该岗位首页并整页刷新，导航、首页、权限全部按新岗位重算。 */
const DemoBar = {
  name: "DemoBar",
  template: `<div class="demo-bar" v-if="d"><span class="small muted">竞赛演示 · 当前视角</span>
    <div class="seg"><button v-for="r in d.roles" :key="r.key" :class="{on: r.key === d.as}" :disabled="busy" @click="pick(r.key)">{{ r.name }}</button></div></div>`,
  data: () => ({ busy: false }),
  computed: { d() { return store.me && store.me.demo; } },
  methods: {
    async pick(role) {
      if (role === this.d.as) return;
      this.busy = true;
      try { await POST("/api/demo/as", { role }); await loadMe(); location.hash = "#/" + homeOf(); location.reload(); }
      catch (e) { toast(e.message, true); this.busy = false; }
    },
  },
};
EXT.slot("top:*", DemoBar, { order: 900, id: "demo" });   // 排在离线提示等页面自带的顶部条之后
