/* 导航的唯一配置：业务线 → 子页面。岗位只做过滤（roles），增强能力只做显隐（feat，见项目设置里的“增强能力”）。
   - 一级菜单 = 业务线（顶部 6 条以内，每条一行副标题）；子页面在页面上方用页签切换（hubbar）；
   - 底部（系统管理/更多功能/消息）不占 6 个一级位；
   - 同一件事一个入口：不放与首页待办重复的页（整改与质控）、过场页（问工笔、例会、个人设置），这些路由仍可直达；
   - 路由不变：旧链接（书签、通知里的链接）照常直达；某增强能力被隐藏时只是不出现在导航里，页面本身仍可打开；
   - 扩展模块要新增入口：EXT.nav(角色或角色数组, {k, route, label, short, icon, line, feat?, after?})（见 core.js），不再各自成为一级菜单；
   - 图标一律用 icons.js 的线条图标名。 */
// 2026-10-08 用户同意的编排（docs/导航编排方案_2026-10-08.md）：按“今天要办 → 现场闭环 → 计划预测 → 文书函件 → 资料台账 → 智能体”分 6 组，
// 同一组在所有岗位同一个名字（首页那组除外，各岗位首页本来不同）；“更多功能”取消，催办记录归智能体，催办规则归系统管理员的“项目与规则”
// 2026-10-09 RJ 调整：管理岗首页改名“智能管理台”；日志移出“现场”，与周报、月报合成“工笔文书 · 报表”；现场圈排现场第一个；进度按 计划 → 体检 → 预警 → 开工条件 → 预测 排
const NAV_LINES = [
  { k: "desk", name: "智能管理台", icon: "dashboard", as: { tech: ["随手记", "mic"], sub_safety: ["随手拍", "helmet"], admin: ["系统概况", "dashboard"], leader: ["项目总览", "monitor"] } },
  // 10-10 RJ 采用 09_V8 设计：顺序为 智能管理台 → 进度与产值 → 智眸现场 → 工笔文书 → 资料台账 → 智能体（产值归进度的支持层，按岗位过滤）
  { k: "prog", name: "进度与产值", icon: "trend" },
  // 10-09 RJ：“现场”改名“智眸现场”，与“工笔文书”对仗（智眸看现场、工笔写文书）；k 与各页路由不变，旧链接照常打开
  { k: "site", name: "智眸现场", icon: "helmet" },
  // 提问的唯一入口是港湾岗位页面顶部的统一输入框，不放“问工笔”
  // 分包岗位（技术员、安全员）没有工笔文书：这一组对他们只是“收发文”（收项目部来函、签收、书面回复），RJ 10-09
  { k: "rep", name: "工笔文书", icon: "file", as: { tech: ["收发文", "send"], sub_safety: ["收发文", "send"] } },
  { k: "arch", name: "资料台账", icon: "folder" },
  { k: "ai", name: "智能体", icon: "bot", as: { admin: ["AI 治理", "bot"] } },
  // 以下在侧栏底部，不计入 6 个一级菜单
  { k: "sys", name: "系统管理", icon: "wrench", bottom: true },
  { k: "msg", name: "消息", icon: "bell", bottom: true },
];

const _HB = ["manager", "eng_sup", "safety_sup"];            // 港湾管理岗
const _ALL = ["tech", "sub_safety", "manager", "eng_sup", "safety_sup", "leader", "admin"];
const _CIRCLE = ["tech", "sub_safety", "manager", "eng_sup", "safety_sup"];

// lineAs 按岗位改所属业务线；k 唯一；route/params 是点击后去的页面（同一路由的多个页签用 tab/dflt 区分）；alias 是同属本页的其它路由（详情页等，用于高亮）；short 用于一级菜单的副标题；as 按岗位改页签名称；feat 对应一个增强能力开关
const NAV_PAGES = [
  // ---- 首页一组（D10 起管理岗首页统一一个名字；10-09 RJ 定为“智能管理台”，页头写岗位，如“工程主管 · 智能管理台”）
  { k: "home", route: "home", alias: ["snap"], label: "随手记与待办", short: "随手记 · 待办", icon: "mic", line: "desk", roles: ["tech"] },
  { k: "shome", route: "shome", label: "今日安全", short: "随手拍 · 待整改", icon: "helmet", line: "desk", roles: ["sub_safety"] },
  { k: "work", route: "work", label: "智能管理台", short: "待办 · AI 要点", icon: "dashboard", line: "desk", roles: ["manager"] },
  { k: "eng", route: "eng", label: "智能管理台", short: "待办 · AI 要点", icon: "dashboard", line: "desk", roles: ["eng_sup"] },
  { k: "sdash", route: "sdash", label: "智能管理台", short: "待办 · AI 要点", icon: "dashboard", line: "desk", roles: ["safety_sup"] },
  { k: "sysh", route: "sysh", label: "系统概况", short: "概况", icon: "dashboard", line: "desk", roles: ["admin"] },
  { k: "cockpit", route: "cockpit", label: "项目总览", short: "总览", icon: "monitor", line: "desk", roles: ["leader"] },   // 领导的首页；港湾岗位看各自首页
  // ---- 现场：现场圈（亮点，排第一）、问题闭环（技术员的整改与质控在首页“我的待办”里，不再单列；日志 10-09 起在“工笔文书 · 报表”）
  { k: "circle", route: "circle", label: "现场圈", short: "现场圈", icon: "megaphone", line: "site", roles: _CIRCLE },
  { k: "ledger", route: "ledger", label: "隐患台账", short: "隐患", icon: "book", line: "site", roles: ["sub_safety"] },
  { k: "safety", route: "safety", label: "安全", short: "安全", icon: "helmet", line: "site", roles: _HB },
  { k: "quality", route: "quality", label: "质量", short: "质量", icon: "search", line: "site", roles: ["manager", "eng_sup"] },
  { k: "qrcodes", route: "qrcodes", label: "扫码上报", short: "扫码", icon: "camera", line: "site", roles: ["sub_safety", "safety_sup"], feat: "qr" },
  // ---- 进度：P6 计划 → 计划体检（x_plan.js）→ 预警 → 开工条件 → 预测与赶工 → 产值（项目经理只看进度，不看产值）
  // 进度页不带 tab 的旧链接仍打开“预测与赶工”（dflt），书签与通知里的链接不变
  { k: "plan", route: "progress", params: { tab: "plan" }, tab: "plan", label: "P6 计划", short: "计划", icon: "calendar", line: "prog", roles: ["eng_sup"] },
  { k: "alerts", route: "progress", params: { tab: "alerts" }, tab: "alerts", alias: ["alert"], label: "预警", short: "预警", icon: "octagon", line: "prog", roles: ["manager", "eng_sup", "leader"] },
  { k: "gate", route: "gate", label: "开工条件", short: "开工条件", icon: "check-circle", line: "prog", roles: ["eng_sup"], feat: "gate" },
  { k: "progress", route: "progress", params: { tab: "forecast" }, tab: "forecast", dflt: true, label: "预测与赶工", short: "预测", icon: "trend", line: "prog", roles: ["manager", "eng_sup", "leader"] },
  { k: "output", route: "output", label: "产值", short: "产值", icon: "cash", line: "prog", roles: ["manager", "eng_sup", "leader"] },
  // ---- 工笔文书：报表（日报＝施工日志、周报、月报合在一页，按岗位给 日/周/月）、其余文书（英文报告、索赔通知函、致分包文书等）与例会
  // 报表的各分页仍是原地址：日报 #/logs、技术员周报 #/week、周报/月报 #/rpt?seg=week|month（旧的 #/reports?kind=week|month 也打开这里）
  { k: "rpt", route: "rpt", alias: ["logs", "log", "manual", "week", "capture", "report"], label: "报表", short: "报表", icon: "clipboard", line: "rep",
    roles: ["tech", "manager", "eng_sup", "safety_sup", "leader"], lineAs: { tech: "desk" } },   // 技术员不用工笔文书（RJ 10-09 功能 10）：他的日报、周报放在“随手记”组
  // RJ 10-10：工笔文书只是总入口，按用途分四个同级分区：报表（定期汇报，含英文周报 / 月报）| 函件（发给具体单位，含致分包文书的签收与回复）| 例会 | 分析；
  // 原“文书”“致分包文书”页签取消。旧地址照常打开：#/reports?kind=… 按类型归区，#/letters 对工程主管、安全主管落到函件分区的“签收与回复”
  { k: "letter", route: "reports", params: { tab: "letter" }, tab: "letter", dflt: true, alias: ["letters"], label: "函件", short: "函件", icon: "send", line: "rep", roles: ["manager", "eng_sup", "safety_sup", "leader"] },
  { k: "letters", route: "letters", label: "项目部来函", short: "来函", icon: "send", line: "rep", roles: ["tech", "sub_safety"] },
  { k: "meetings", route: "meetings", label: "例会", short: "例会", icon: "users", line: "rep", roles: ["manager", "eng_sup", "safety_sup", "leader"] },
  { k: "ana", route: "reports", params: { tab: "ana" }, tab: "ana", label: "分析", short: "分析", icon: "chart", line: "rep", roles: ["manager", "eng_sup", "safety_sup", "leader"] },
  // ---- 资料台账：验收、材料、档案与索赔证据
  { k: "conceal", route: "conceal", label: "隐蔽验收", short: "验收", icon: "clipboard", line: "arch", roles: ["tech", "eng_sup"], feat: "conceal" },
  { k: "tickets", route: "tickets", label: "材料与砼", short: "材料", icon: "brick", line: "arch", roles: ["tech", "eng_sup"], feat: "tickets" },
  { k: "sites", route: "sites", alias: ["claims"], label: "部位档案与索赔证据", short: "档案", icon: "database", line: "arch", roles: ["eng_sup"], feat: "archive" },
  // ---- 智能体（系统管理员这一组叫“AI 治理”）；历史回测由 x_backtest.js 挂在应用成效之后
  { k: "aimgr", route: "aimgr", label: "模型与额度", short: "额度", icon: "calculator", line: "ai", roles: ["admin"] },
  { k: "audit", route: "audit", label: "AI 与审计", short: "审计", icon: "file", line: "ai", roles: ["admin"] },
  { k: "agents", route: "agents", params: { tab: "agents" }, tab: "agents", dflt: true, tabs: ["ask"], label: "智能体", short: "智能体", icon: "bot", line: "ai", roles: ["manager", "eng_sup", "admin"] },
  { k: "metrics", route: "agents", params: { tab: "metrics" }, tab: "metrics", label: "应用成效", short: "成效", icon: "chart", line: "ai", roles: ["eng_sup", "admin"] },   // 项目经理只看进度、质量、安全：应用成效不给
  { k: "chase", route: "chase", label: "催办记录", short: "催办", icon: "clock", line: "ai", roles: ["eng_sup"], feat: "nudge" },   // 规则由系统管理员在“项目与规则”里设置
  // ---- 底部：系统管理 / 消息
  // 问工笔（agents?tab=ask）、个人设置（settings）路由保留可直达，但不单独进导航：提问走页面顶部的统一输入框和智能体卡片
  { k: "users", route: "users", label: "账号与权限", short: "账号", icon: "users", line: "sys", roles: ["admin"] },
  { k: "settings", route: "settings", label: "项目与规则", short: "规则", icon: "sliders", line: "sys", roles: ["admin"] },
  { k: "notices", route: "notices", label: "消息", short: "", icon: "bell", line: "msg", roles: _ALL },
];

const RPT_KINDS = ["week", "month"];
// 文书类型 → 工笔文书分区（en 报表 / letter 函件 / meet 例会 / ana 分析），映射在 x_docs.js；没加载时一律当函件
const gbTab = (kind) => (typeof gbTabOf === "function" ? gbTabOf(kind) : "");

// 增强能力是否启用：没写的一律视为启用（与后端 services/features.py 同一口径）
function featOn(k) {
  const f = store.me && store.me.project && store.me.project.features;
  return !(f && f[k] === false);
}

function _navPages() {
  const out = NAV_PAGES.slice();
  for (const p of EXT.navPages) {
    let i = p.after ? out.findIndex((x) => x.k === p.after) : -1;
    out.splice(i >= 0 ? i + 1 : out.length, 0, p);
  }
  return out;
}

// 某岗位能看到的全部页面（不看开关），标签按岗位取 as
function navRolePages(role) {
  return _navPages().filter((p) => p.roles.includes(role) || p.roles.includes("*")).map((p) => ({ ...p, label: (p.as && p.as[role]) || p.label, line: (p.lineAs && p.lineAs[role]) || p.line }));
}

// 构建某岗位的导航：{ top: [业务线…], bottom: [业务线…] }；keep 为当前页的 k：即使它所属能力被隐藏，也留在页签里不让界面跳走
function buildNav(role, flags = {}, keep = "") {
  const pages = navRolePages(role).filter((p) => !p.feat || flags[p.feat] !== false || p.k === keep);
  const lines = [];
  for (const l of NAV_LINES) {
    const ps = pages.filter((p) => p.line === l.k);
    if (!ps.length) continue;
    const o = (l.as && l.as[role]) || [];
    lines.push({ k: l.k, name: o[0] || l.name, icon: o[1] || l.icon, bottom: !!l.bottom, pages: ps,
      sub: ps.map((p) => p.short).filter(Boolean).slice(0, 4).join(" · "), title: ps.map((p) => p.label).join(" · ") });
  }
  return { top: lines.filter((l) => !l.bottom), bottom: lines.filter((l) => l.bottom) };
}

// 当前路由属于哪个页面（取该岗位的页面，不看开关）。详情页归属沿用原有规则：日志详情 → 日志/质量；隐患详情 → 台账/首页待办/安全/质量
function navMatch(role, name, params = {}, ctx = {}) {
  const pages = navRolePages(role);
  // 同一路由下的页签页（如进度的 预测/预警/P6 计划）按 params.tab 区分；没写 tab 的路由取标了 dflt 的那个；tabs 是归属本页的其它页签（如从智能体卡片点进的提问）；alias（详情页等）直接归属
  const by = (route) => pages.find((p) => (p.alias || []).includes(route) || (p.route === route && (!p.tab || params.tab === p.tab || (p.dflt && !params.tab) || (p.tabs || []).includes(params.tab)) && (!p.notTab || params.tab !== p.notTab)));
  let want = [name, EXT.aliases[name]].filter(Boolean);
  if (name === "log") want = role === "manager" ? ["quality"] : ["rpt", "quality"];   // 项目经理不看日志：从周报引用点开的日志详情归“质量”
  else if (name === "reports" && RPT_KINDS.includes(params.kind)) want = ["rpt"];      // 旧地址 #/reports?kind=week|month 现在是“报表”里的周报/月报
  else if (name === "reports" && !params.tab && params.kind) {                         // 旧地址 #/reports?kind=…：按文书类型归到所在分区
    const t = gbTab(params.kind);
    if (t === "en") want = ["rpt"]; else if (t === "meet") want = ["meetings"]; else params = { ...params, tab: t || "letter" };
  }
  else if (name === "finding") want = role === "tech" ? ["home"] : role === "sub_safety" ? ["ledger"] : [ctx.findingAgent === "quality" ? "quality" : "safety"];
  for (const w of want) { const p = by(w); if (p) return p; }
  return null;
}
