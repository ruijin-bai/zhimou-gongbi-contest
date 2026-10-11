"""RJ 10-09 整体视觉与页面分布意见（导航改名、报表合并、进度顺序、现场圈排版、小问号提示、“全部”置首）。

- 统一的阅读提示 HelpTip（小问号）：全局注册；页头的页面用途收进小问号，不再常驻一行；
- 报表：日报（施工日志）、周报、月报合在“工笔文书 · 报表”一页，按岗位给分页；原地址（#/logs、#/week、#/reports?kind=week|month）仍打开对应分页；
  项目经理没有日报；“文书”页不再重复列周报、月报；
- 进度：P6 计划默认横道图、可全屏，表格是可选视图；预测、预警、体检的专业指标带悬停解释；
- 现场圈：一块铺满宽度的底板，动态按朋友圈排版；
- 列表的分类筛选“全部”排第一。
"""
import re
from pathlib import Path

from test_ui_fixes import run_js

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def read(n):
    return (STATIC / n).read_text(encoding="utf-8")


def test_help_tip_component_and_page_head_purpose_moves_into_it():
    assert 'app.component("help-tip", HelpTip)' in read("main.js")
    out = run_js(r"""
      return { props: Object.keys(HelpTip.props), ph: PageHead.template, tip: HelpTip.template };
    """)
    assert {"text", "label", "caption"} <= set(out["props"])            # 模板能否编译由 test_pagehead 的全组件编译检查覆盖
    assert "<help-tip" in out["ph"] and "ph-purpose" not in out["ph"]            # 页面用途收进小问号
    assert 'role="tooltip"' in out["tip"] and "aria-label" in out["tip"] and "<slot" in out["tip"]


def test_report_hub_segments_by_role_and_old_addresses():
    out = run_js(r"""
      const segs = (r) => rptSegs(r).map((s) => s.k + ':' + s.route + (s.params.seg ? '?seg=' + s.params.seg : ''));
      const is = (role, name, params) => isRptRoute(role, { name, params: params || {} });
      const seg = (role, name, params) => { store.me = { user: { role } }; store.route = { name, params: params || {} };
        return ReportHub.computed.seg.call({ segs: rptSegs(role) }); };
      return {
        tech: segs('tech'), eng: segs('eng_sup'), pm: segs('manager'), leader: segs('leader'),
        is: [is('tech', 'logs'), is('tech', 'week'), is('eng_sup', 'rpt'), is('manager', 'reports', { kind: 'week' }), is('manager', 'reports', { kind: 'month' }),
             is('manager', 'reports', {}), is('eng_sup', 'reports', { kind: 'notice_of_claim' }), is('eng_sup', 'week'), is('manager', 'reports', { kind: 'weekly_en' })],
        seg: [seg('tech', 'logs'), seg('tech', 'week'), seg('eng_sup', 'rpt', {}), seg('eng_sup', 'rpt', { seg: 'month' }), seg('manager', 'reports', { kind: 'week' }),
              seg('leader', 'logs'), seg('manager', 'rpt', { seg: 'day' })],
        docTypes: GongbiHome.computed.list.call({ catName: '函件', only: '', types: [{ kind: 'week', legacy: true, group: '报表' }, { kind: 'month', legacy: true, group: '报表' },
          { kind: 'weekly_en', legacy: false, group: '报表' }, { kind: 'notice_of_claim', legacy: false, group: '函件' }] }).map((t) => t.kind),
        tabs: ['weekly_en', 'monthly_en', 'notice_of_claim', 'work_contact', 'letter_to_sub', 'precontrol_notice', 'docdebt_notice', 'meeting_pack', 'three_week_plan', 'delay_brief', 'monthly_review'].map(gbTabOf),
        reportsTpl: Reports.template,
      };
    """)
    assert out["tech"] == ["day:logs", "week:week"]                                   # 技术员的周报就是周进度确认
    en = ["weekly_en:rpt?seg=weekly_en", "monthly_en:rpt?seg=monthly_en"]          # RJ 10-10：英文周报 / 月报也是定期汇报，与周报、月报同级在“报表”
    assert out["eng"] == ["day:logs", "week:rpt?seg=week", "month:rpt?seg=month"] + en
    assert out["pm"] == out["leader"] == ["week:rpt?seg=week", "month:rpt?seg=month"] + en   # 项目经理不看日志：没有日报
    assert out["is"] == [True, True, True, True, True, False, False, False, True]     # 其余文书归函件 / 例会 / 分析分区；非技术员的 #/week 不进报表；旧的英文周报链接进报表
    assert out["seg"] == ["day", "week", "day", "month", "week", "week", "week"]      # 拿不到的分页落到第一个可用分页
    assert out["docTypes"] == ["notice_of_claim"]                                     # 分区页只列本区的类型
    assert out["tabs"] == ["en"] * 2 + ["letter"] * 5 + ["meet"] * 2 + ["ana"] * 2    # 每种文书只属于一个分区
    assert '<gongbi-home v-if="!fixed && gb" :cat="gbCat"' in out["reportsTpl"]      # 不带 fixed 是函件 / 分析分区或英文报告（GongbiHome）


def test_report_hub_is_rendered_for_old_routes_and_pm_still_blocked_from_logs():
    main = read("main.js")
    assert "<report-hub v-else-if=\"r.name === 'rpt' || isRpt\"></report-hub>" in main
    # 项目经理直达 #/logs 仍先被“不在查看范围”拦下（outOfScope 在报表之前判断）
    assert main.index('v-if="outOfScope"') < main.index("<report-hub")
    assert 'go(\'rpt\', {seg: r.kind})' in read("views.js")                         # 周报 / 月报详情“返回”回到报表


def test_plan_defaults_to_gantt_with_fullscreen_and_table_is_optional():
    views, gantt = read("views.js"), read("x_gantt.js")
    plan = views[views.index("const Plan = {"):views.index("// ---------------------------------------------------------------------------- 报表")]
    assert 'view: "gantt"' in plan
    assert plan.index("view = 'gantt'") < plan.index("view = 'table'")              # 先横道图，后表格
    assert plan.index("<component :is=\"gantt\"") < plan.index("导入 P6 计划")         # 图表在导入卡片之前
    assert "'gt-full': full" in gantt and "setFull(!full)" in gantt and "Escape" in gantt
    out = run_js(r"""
      const vm = { full: false, _esc: null }; const m = PlanGantt.methods;
      const cls = []; document.removeEventListener = () => {}; document.body = { classList: { toggle: (c, on) => cls.push(c + ':' + on) } };
      m.setFull.call(vm, true); const on = vm.full; m.setFull.call(vm, false);
      return { on, off: vm.full, cls, icon: !!ICONS.maximize };
    """)
    assert out == {"on": True, "off": False, "cls": ["gt-lock:true", "gt-lock:false"], "icon": True}


def test_progress_metrics_have_hover_explanations():
    mgr, views, plan = read("mgr.js"), read("views.js"), read("x_plan.js")
    fc = mgr[mgr.index("const Forecast = {"):mgr.index("const ProgressHub")]
    for k in ("五五开", "比 P50 稳妥", "按期概率", "偏差＝", "预测余量＝", "CPM", "工期系数"):
        assert re.search(r'<help-tip text="[^"]*' + re.escape(k), fc), k
    card = views[views.index("const AlertCard = {"):views.index("const Alerts = {")]
    for k in ("计划应完成", "总浮时：", "推进速度"):
        assert re.search(r'<help-tip text="[^"]*' + re.escape(k), card), k
    assert re.search(r'<help-tip text="系数＝计划工效 ÷ 实测工效', plan)
    # 预警页的判定规则收进小问号；数据缺口说明仍只给工程主管（seesGaps）
    al = views[views.index("const Alerts = {"):views.index("const AlertDetail")]
    assert '<help-tip label="判定规则">判定规则' in al and '<div class="tiny muted mb">判定规则' not in al


def test_filters_put_all_first():
    out = run_js(r"""
      store.me = { user: { role: 'eng_sup' } };
      const eng = LogList.computed.tabs.call({}); store.me.user.role = 'tech'; const tech = LogList.computed.tabs.call({});
      return { alerts: Alerts.computed.tabs(), board: FindingBoard.computed.tabs(), eng, tech, plan: Plan.computed.tabs(), circle: CircleFeed.data().VIEWS };
    """)
    for k, tabs in out.items():
        assert tabs[0][1] == "全部", (k, tabs)
    q = read("x_quick.js")
    assert q.index("setSeg('all')\">全部") < q.index("setSeg('new')")


def test_circle_feed_is_one_full_width_board_in_moments_layout():
    tpl = read("circle.js")
    assert '<div class="card cf-board" v-if="posts.length" v-fit="300">' in tpl   # 动态流整板一个卡片，面板内连续滚动（朋友圈式，不分页）
    assert "<article :class=\"['post', 'k-' + p.kind" in tpl and "['card', 'post'" not in tpl   # 动态不再一条一张卡片
    css = read("x_skin.css")
    assert ".circle .cf-board > article.post > :not(.post-hd) { margin-left: 50px; }" in css
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" not in css.split("现场圈（RJ 10-09）")[1].split("顶栏的")[0]   # 不再左右两栏
