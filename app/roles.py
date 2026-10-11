"""角色与权限：一处定义，后端校验、前端导航和账号管理共用。

分包单位（现场执行）：技术员、安全员；港湾（项目管理）：工程主管、安全主管、项目经理；
系统管理员负责账号与系统。`leader` 是早期保留的只读角色，不在 8 个业务角色之内。
"""
from __future__ import annotations

ROLES: dict[str, dict] = {
    "tech":       {"name": "技术员",   "org": "sub",      "device": "手机", "home": "home",
                   "duty": "现场说一句、拍一张，周确认一次；处理派给自己的整改与质控"},
    "sub_safety": {"name": "安全员",   "org": "sub",      "device": "手机", "home": "shome",
                   "duty": "本单位现场安全巡查、随手拍隐患、组织整改并上传整改照片"},
    "eng_sup":    {"name": "工程主管", "org": "harbour",  "device": "电脑", "home": "eng",
                   "duty": "计划、质量与生产：P6与进度预警、分包日志数据质量、质量问题、人机投入与阻碍问题、清单映射与周月报起草"},
    "safety_sup": {"name": "安全主管", "org": "harbour",  "device": "电脑", "home": "sdash",
                   "duty": "全场安全：隐患库、分包安全对比、超期督办、整改复核与闭环"},
    "manager":    {"name": "项目经理", "org": "harbour",  "device": "电脑", "home": "work",
                   "duty": "只处理要自己拍板的事，一屏掌握进度、质量、安全，审核发布报表"},
    "leader":     {"name": "公司领导", "org": "harbour",  "device": "大屏", "home": "cockpit",
                   "duty": "只读的项目总览（早期保留角色）"},
    "admin":      {"name": "系统管理员", "org": "system", "device": "电脑", "home": "sysh",
                   "duty": "账号与权限、项目与规则、AI 连通与审计，不参与业务决策"},
}
ORG_NAME = {"sub": "分包单位", "harbour": "港湾", "system": "系统"}
ROLE_ORDER = ["admin", "manager", "eng_sup", "safety_sup", "leader", "sub_safety", "tech"]

# 项目经理只看进度、质量、安全（AGENTS.md）。以下口径都按这一组岗位剔除，后端剔除字段/拒绝接口，前端无字段不显示：
# - 日志/周确认的按时率、应报份数等填报率数字；工程量“分包自报/港湾核定”拆分（属日志审核数据）；
# - 应用成效（记录耗时、日志预审、手工对照、AI 调用）；智能应用页只列 PM_AGENTS 范围内的 Agent。
# 产值（RJ 10-10）：项目经理只读——可看产值页、报表产值节与周报十项指标；不导入、不确认映射、不维护计划（不在 output_ops），
# 报表产值节原文不改；经理主动问产值可以答（qa 用 PM_ASK_AGENTS 与 brief.ask_scope_ok）；简报、待办、催办仍不向项目经理推产值。
PM_SCOPE = frozenset({"manager"})
NO_FILL_RATE = PM_SCOPE
# 项目经理关注范围内的 Agent：首页“智能体今日处理”、智能应用页共用
PM_AGENTS = frozenset({"safety", "quality", "progress", "brief", "analyst", "report"})
# 项目经理可以主动提问的 Agent（RJ 10-10：经理主动询问产值应允许）；首页、智能应用页、简报、待办、催办仍按 PM_AGENTS，不主动推产值
PM_ASK_AGENTS = PM_AGENTS | {"output"}

SUB_ROLES = frozenset(k for k, v in ROLES.items() if v["org"] == "sub")
HARBOUR_ROLES = frozenset(k for k, v in ROLES.items() if v["org"] == "harbour")  # 港湾岗位（含只读的公司领导）
# 能看项目级汇总数据的角色（分包单位只看自己单位范围，走专门接口）
VIEW_ROLES = frozenset(ROLES) - SUB_ROLES
# 现场圈可进入的岗位（与前端 nav.js 的 _CIRCLE 一致）：公告通知只对这些岗位给“去现场圈”的跳转
CIRCLE_ROLES = frozenset({"tech", "sub_safety", "manager", "eng_sup", "safety_sup"})
# 施工日志/周进度确认的作者：只有技术员（分包安全员不处理日志）。新建、编辑、整理、提交接口按此白名单拒绝其他岗位
LOG_WRITERS = frozenset({"tech"})

_HARBOUR_OPS = {"manager", "admin"}
CAPS: dict[str, frozenset[str]] = {k: frozenset(v) for k, v in {
    # 进度域：P6 导入导出、判定、预测、赶工、预警反馈
    "progress_ops": _HARBOUR_OPS | {"eng_sup"},
    # 现场安全：重新识别、人工闭环/忽略安全隐患、督办
    "site_safety": _HARBOUR_OPS | {"safety_sup"},
    # 质量与数据：生成质控提醒、闭环/忽略质量与数据审核事项
    "site_quality": _HARBOUR_OPS | {"eng_sup"},
    # 产值：清单/周产值导入、AI 匹配、确认/否决映射、计划参数、产值分析。项目经理只读产值，不在此列
    "output_ops": {"admin", "eng_sup"},
    # 分包日志/周确认审核（含批量通过）：项目经理不审核日志（AGENTS.md），周报的审核发布走 report_publish，不受影响
    "log_review": {"admin", "eng_sup"},
    # 报表生成、编辑与审核发布是业务产出：系统管理员不参与（RJ 10-09），只留项目经理、工程主管
    "report_gen": {"manager", "eng_sup"},
    "report_publish": {"manager"},
    # 应用成效基线（手工对照）、AI 调用记录、AI 建议采纳统计：只给系统管理员
    "metrics_ops": {"admin"},
    # 派单（改责任人）与催办
    "dispatch": _HARBOUR_OPS | {"safety_sup", "eng_sup", "sub_safety"},
    "nudge": _HARBOUR_OPS | {"safety_sup", "eng_sup"},
}.items()}


def can(role: str, cap: str) -> bool:
    return role in CAPS.get(cap, ())


def caps_of(role: str) -> list[str]:
    return sorted(c for c, rs in CAPS.items() if role in rs)


def hides_fill_rate(role: str) -> bool:
    return role in NO_FILL_RATE


def output_readonly(role: str) -> bool:
    """产值只读：能看产值页与报表产值节，不能改报表产值节原文（写接口另由 output_ops 拒绝）。"""
    return role in PM_SCOPE and not can(role, "output_ops")


def hides_review_split(role: str) -> bool:
    """工程量的“分包自报/港湾核定”拆分（日志审核状态）对该岗位不显示。"""
    return role in PM_SCOPE


def hides_metrics(role: str) -> bool:
    """应用成效页（记录耗时、日志预审率、手工对照）对该岗位不开放。"""
    return role in PM_SCOPE


def is_sub(role: str) -> bool:
    return role in SUB_ROLES


def role_name(role: str) -> str:
    return ROLES.get(role, {}).get("name", role)
