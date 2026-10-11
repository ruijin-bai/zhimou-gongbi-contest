"""放行Agent：往前看未来几天将开工的 P6 作业，按工序列出开工/浇筑条件，核到的打勾，缺项指到责任人。

分工（与 AGENTS.md 一致，三者分开存放、分开展示）：
- 确定性数据：P6 作业与逻辑关系、质控点、隐蔽验收记录、材料批次、浇筑记录，由代码核验，得出“系统已核到：…”；
- 模型只做一件事：作业名称关键词归不进去时，在固定类别里选一个（gate.classify，结果缓存，失败回退“通用”）；
  模型不判定任何条件是否具备；
- 人工确认：有权者把条件置为 已具备/不适用/受阻，记录谁、何时、凭证备注，并写 audit；人工优先于系统核验。
系统查不到的一律是“待落实（unknown）”，绝不默认通过，也不补成“已具备”。条件为通用模板，以项目方案和监理要求为准。
"""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import time
from datetime import date, timedelta

from .. import roles as R
from ..ai.client import call_json
from ..db import all_, audit, dumps, event, insert, now_iso, one, tx
from . import p6

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS gate_items (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    activity_code TEXT NOT NULL,
    cond_key TEXT NOT NULL,
    label TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    custom INTEGER NOT NULL DEFAULT 0,
    manual_state TEXT NOT NULL DEFAULT 'pending' CHECK (manual_state IN ('pending','ready','na','blocked')),
    manual_note TEXT NOT NULL DEFAULT '',
    manual_by INTEGER,
    manual_at TEXT,
    owner_id INTEGER,
    due_date TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, activity_code, cond_key)
);
CREATE INDEX IF NOT EXISTS ix_gate_items_act ON gate_items(project_id, activity_code);
CREATE TABLE IF NOT EXISTS gate_class (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    activity_code TEXT NOT NULL,
    act_name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'rule' CHECK (source IN ('rule','ai')),
    ai_run_id INTEGER,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, activity_code)
);
"""

NOTICE = "条件为通用模板，以项目方案和监理要求为准。“待落实”不等于“未做”，只是系统没有核到，需要现场确认。"

DAYS_OPTIONS = (7, 14, 21)
LOOKBACK = 7          # 计划开始已过、但还没开工的作业也要看（应开未开）
NEAR_DAYS = 3         # 距计划开始不超过这个天数仍有未具备项 = 临近缺项
FLOAT_SLACK_D = 7     # 非关键作业总浮时不少于这个天数：晚开几天不算超期/高风险（晚开天数吃掉浮时的除外）
AI_BATCH = 10
MAX_CUSTOM = 12
MAX_ROWS = 200

VIEWERS = frozenset({"manager", "eng_sup", "safety_sup", "leader", "tech"})
ADMINS = frozenset({"manager", "eng_sup"})
TECH_STATES = ("ready", "blocked", "pending")
ALL_STATES = ("ready", "na", "blocked", "pending")

CAT_NAME = {"general": "通用", "concrete": "混凝土浇筑", "rebar_form": "钢筋/模板", "pile": "桩基", "earth": "土方/基坑",
            "pave": "路面结构层", "lifting": "吊装/起重", "marine": "水上/沉箱/预制安装", "custom": "自定义"}
CLASSIFY_CATS = tuple(k for k in CAT_NAME if k != "custom")
VERDICT_NAME = {"ready": "可开工", "gap": "有缺项", "near": "临近缺项", "blocked": "受阻"}
EFF_NAME = {"ready": "已具备", "na": "不适用", "blocked": "受阻", "missing": "缺项", "unknown": "待落实"}


class NotFound(LookupError):
    pass


# ============================================================================ 条件模板（通用工程语言，不引用具体规范条文号）

def _c(key: str, label: str, auto: str | None = None) -> dict:
    return {"key": key, "label": label, "auto": auto}


GENERAL = [
    _c("drawing", "图纸为最新版并已会审"),
    _c("scheme", "施工方案/专项方案已批"),
    _c("disclosure", "技术、安全交底已完成"),
    _c("prev_done", "前道工序已完成并验收", "prev"),
    _c("resources", "人员、机械已就位"),
    _c("material", "材料已到场（需复试的复试合格）", "material"),
    _c("qc_points", "开工前质控点已完成", "qc"),
]
SPECIFIC = {
    "concrete": [
        _c("rebar_conceal", "钢筋隐蔽验收", "conceal:rebar"),
        _c("formwork", "模板及支撑验收", "conceal:form"),
        _c("mix_design", "混凝土配合比、开盘鉴定"),
        _c("sampling", "试块与取样准备", "pours"),
        _c("trucks", "罐车、泵车已预约"),
        _c("weather", "天气窗口满足浇筑"),
    ],
    "rebar_form": [
        _c("layout", "测量放线复核"),
        _c("bar_ready", "钢筋加工件、接头试验合格"),
        _c("form_ready", "模板、支架材料及支撑体系已备"),
    ],
    "pile": [
        _c("pile_layout", "桩位复核"),
        _c("hole_accept", "成孔验收（孔深、孔径、沉渣，灌注前）", "conceal:hole"),
        _c("cage_accept", "钢筋笼验收（下笼前）", "conceal:cage"),
    ],
    "earth": [
        _c("excav_scheme", "基坑/土方开挖方案已批（含支护、边坡）"),
        _c("monitor", "监测点已布设并取得初始值"),
        _c("dewater", "降排水措施到位"),
        _c("underground", "地下管线、障碍物已核查"),
    ],
    # 路面结构层（水稳/级配碎石底基层、基层，沥青面层等）。前道工序完成、材料复试、质控点由通用项自动核验，
    # 以下各项系统核不到，一律“待落实”，由现场确认
    "pave": [
        _c("pave_base", "下承层已验收（压实度、弯沉、高程、平整度）"),
        _c("pave_mix", "配合比已批，原材料检验合格（集料级配、水泥剂量或沥青指标）"),
        _c("pave_trial", "试验段已完成并确定施工参数（松铺系数、碾压遍数）"),
        _c("pave_equip", "摊铺、碾压设备已就位并调试"),
        _c("pave_weather", "天气、气温满足摊铺碾压（雨天不施工，水稳控制延迟时间）"),
    ],
    "lifting": [
        _c("lift_scheme", "吊装方案已批（吊点、站位、警戒）"),
        _c("crane_cert", "起重设备检定验收合格，司机司索持证"),
        _c("wind", "风速、天气满足吊装限制"),
        _c("ground", "吊装场地承载满足"),
    ],
    "marine": [
        _c("tide", "潮位、水文窗口满足"),
        _c("vessel_report", "船机报备、水上水下作业许可"),
        _c("marine_equip", "船机、救生与警示设施到位"),
        _c("precast_ready", "构件强度、出运或安装条件满足"),
    ],
}

# 关键词归类（按顺序，先中先得）；先看作业名称，名称没有命中再看 WBS 路径
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("marine", ("沉箱", "水上", "水下", "海上", "潮", "船", "出运", "预制安装", "抛石")),
    ("lifting", ("吊装", "起重", "架设", "吊运", "龙门吊", "塔吊", "履带吊", "汽车吊", "吊梁")),
    ("pile", ("桩", "成孔", "钻孔", "旋挖", "护筒", "钢筋笼")),
    # 路面结构层排在混凝土、土方之前：“水泥稳定碎石基层”“沥青混凝土面层”不能落到混凝土浇筑或土方/基坑
    ("pave", ("水稳", "水泥稳定", "稳定碎石", "稳定土", "级配碎石", "沥青", "底基层", "基层", "面层", "路面", "透层", "粘层", "封层")),
    ("concrete", ("混凝土", "砼", "浇筑", "浇注", "现浇")),
    ("rebar_form", ("钢筋", "模板", "支模", "拆模", "支架", "脚手", "绑扎")),
    ("earth", ("土方", "开挖", "基坑", "基槽", "挖方", "填方", "填筑", "回填", "路基", "降水", "边坡", "支护", "换填", "清表", "碾压", "地基处理")),
    ("concrete", ("承台", "墩身", "盖梁", "台身", "桥台", "垫层", "底板", "顶板", "侧墙", "立柱", "预制", "梁板", "箱梁", "T梁")),
]


def defs_for(cat: str) -> list[dict]:
    """某类作业的模板条件：通用 + 工序专项；每条带 category（所属模板组）。"""
    out = [dict(d, category="general") for d in GENERAL]
    if cat in SPECIFIC:
        out += [dict(d, category=cat) for d in SPECIFIC[cat]]
    return out


PAVE_STRONG = ("水稳", "水泥稳定", "稳定碎石", "稳定土", "级配碎石", "沥青", "透层", "粘层", "封层")   # 明确的路面结构层材料/工序
PAVE_WIDE = ("底基层", "基层", "面层", "路面")                                                       # 宽词：可能是混凝土面层/路面


def rule_classify(name: str, wbs_path: str = "") -> str | None:
    for i, text in enumerate((name or "", wbs_path or "")):
        for cat, kws in RULES:
            hit = [k for k in kws if k in text]
            if not hit:
                continue
            if cat == "pave" and not any(k in text for k in PAVE_STRONG):
                # 只有宽词：名称里是混凝土（码头/堆场面层、水泥混凝土路面）仍归混凝土；WBS 路径不靠宽词归路面
                if i == 1 or any(k in text for k in ("混凝土", "砼", "浇筑", "浇注", "现浇")):
                    continue
            return cat
    return None


# ============================================================================ 归类（规则优先，模型兜底并缓存）

_AI_FAIL: dict[tuple[int, str], float] = {}     # 模型失败后的进程内冷却，避免每次刷新都等超时
AI_COOLDOWN = 600

CLASSIFY_SYSTEM = ("你是土建施工的工程师。请为每个施工作业在固定类别里选且只选一个，用于生成开工条件清单。\n"
                   "类别：general=通用/其它；concrete=混凝土浇筑与现浇结构；rebar_form=钢筋与模板；pile=桩基；"
                   "earth=土方、基坑、路基；pave=路面结构层（水稳、级配碎石底基层/基层、沥青面层）；lifting=吊装、起重、架设；marine=水上、沉箱、预制安装。\n"
                   "拿不准就选 general。只输出JSON：{\"items\":[{\"code\":\"作业代码\",\"category\":\"类别\"}]}")


def _norm_classify(data, codes: list[str]) -> dict[str, str]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("缺少items")
    out = {}
    for it in data["items"]:
        if isinstance(it, dict) and it.get("code") in codes and it.get("category") in CLASSIFY_CATS:
            out[it["code"]] = it["category"]
    if not out:
        raise ValueError("没有可用的归类结果")
    return out


def ai_classify(conn, project_id: int, user_id: int | None, acts: list[dict]) -> dict[str, str]:
    """让模型在固定类别里归类；任何失败都返回空（调用方回退“通用”），不阻塞主流程。"""
    now = time.monotonic()
    acts = [a for a in acts if now - _AI_FAIL.get((project_id, a["code"]), -1e9) > AI_COOLDOWN][:AI_BATCH]
    if not acts:
        return {}
    codes = [a["code"] for a in acts]
    text = "\n".join(f"{a['code']} | {a['name']} | {a.get('wbs_path') or ''}" for a in acts)
    try:
        res = call_json("gate.classify", system=CLASSIFY_SYSTEM, user="作业（代码 | 名称 | WBS）：\n" + text,
                        prompt_version="gate-v1-20261006",
                        mock=lambda: {"items": [{"code": c, "category": "general"} for c in codes]},   # 离线模拟不做真实归类
                        validate=lambda d: _norm_classify(d, codes), project_id=project_id, user_id=user_id,
                        max_tokens=800, input_meta={"count": len(codes)})
    except Exception as exc:  # noqa: BLE001  任何故障都只降级，不阻塞清单
        for c in codes:
            _AI_FAIL[(project_id, c)] = now
        log.warning("放行Agent 归类失败，回退通用：%s", exc)
        return {}
    by_code = {a["code"]: a for a in acts}
    for code, cat in res.data.items():
        conn.execute("""INSERT INTO gate_class (project_id, activity_code, act_name, category, source, ai_run_id, updated_at)
                        VALUES (?,?,?,?, 'ai', ?, ?) ON CONFLICT(project_id, activity_code)
                        DO UPDATE SET act_name=excluded.act_name, category=excluded.category, source='ai',
                                      ai_run_id=excluded.ai_run_id, updated_at=excluded.updated_at""",
                     (project_id, code, by_code[code]["name"], cat, res.run_id, now_iso()))
    return res.data


def classify_all(conn, project_id: int, acts: list[dict], user_id: int | None = None, allow_ai: bool = False) -> dict[str, dict]:
    """code -> {category, source}；source: rule | ai | default。"""
    cache = {r["activity_code"]: r for r in all_(conn, "SELECT * FROM gate_class WHERE project_id=?", (project_id,))}
    out, pending = {}, []
    for a in acts:
        cat = rule_classify(a["name"], a.get("wbs_path") or "")
        if cat:
            out[a["code"]] = {"category": cat, "source": "rule"}
            continue
        c = cache.get(a["code"])
        if c and c["act_name"] == a["name"] and c["category"] in CAT_NAME:
            out[a["code"]] = {"category": c["category"], "source": "ai"}
        else:
            out[a["code"]] = {"category": "general", "source": "default"}
            pending.append(a)
    if allow_ai and pending:
        for code, cat in ai_classify(conn, project_id, user_id, pending).items():
            out[code] = {"category": cat, "source": "ai"}
    return out


# ============================================================================ 取数（一次载入，逐作业内存核验）

def _opt(conn, sql: str, params=()) -> list[dict] | None:
    """可选表（隐蔽验收、材料批次、浇筑记录由其他功能建立）：表不存在或列不符一律视为没有数据，不报错。"""
    try:
        return all_(conn, sql, params)
    except sqlite3.Error:
        return None


class Ctx:
    def __init__(self, conn, project_id: int, acts: list[dict], today: date):
        self.today, self.project_id = today, project_id
        self.by_code = {a["code"]: a for a in acts}
        imp = p6.current_import(conn, project_id)
        rels = all_(conn, "SELECT pred_code, succ_code, kind FROM relations WHERE import_id=?", (imp["id"],)) if imp else []
        self.preds: dict[str, list[dict]] = {}
        for r in rels:
            self.preds.setdefault(r["succ_code"], []).append(r)
        self.logmax = {r["activity_code"]: r["m"] for r in all_(
            conn, """SELECT lp.activity_code, MAX(lp.pct) m FROM log_progress lp JOIN logs l ON l.id=lp.log_id
                     WHERE lp.project_id=? AND l.status IN ('submitted','approved') AND lp.voided_at IS NULL AND lp.pct IS NOT NULL GROUP BY lp.activity_code""",
            (project_id,))}
        self.qc: dict[str, list[dict]] = {}
        for f in all_(conn, """SELECT activity_code, status, due_date FROM findings WHERE project_id=? AND agent='quality'
                               AND kind='qc_reminder' AND activity_code IS NOT NULL""", (project_id,)):
            self.qc.setdefault(f["activity_code"], []).append(f)
        self.conceal = self._group(_opt(conn, "SELECT id, activity_code, part, template_key, status, inspection_date FROM conceal_records WHERE project_id=?", (project_id,)))
        self.pours = self._group(_opt(conn, "SELECT id, activity_code, part, grade, pour_date, status FROM pours WHERE project_id=?", (project_id,)))
        self.batches = _opt(conn, "SELECT id, category, name, retest_status, arrived_at FROM material_batches WHERE project_id=? ORDER BY id DESC LIMIT 500", (project_id,))
        self.items: dict[str, list[dict]] = {}
        for r in all_(conn, "SELECT * FROM gate_items WHERE project_id=? ORDER BY id", (project_id,)):
            self.items.setdefault(r["activity_code"], []).append(r)
        self.users = {u["id"]: u for u in all_(conn, "SELECT id, name, role, company, wbs_scope, active FROM users WHERE project_id=?", (project_id,))}

    @staticmethod
    def _group(rows):
        if rows is None:
            return None
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["activity_code"] or "", []).append(r)
        return out

    def members(self, act: dict) -> list[dict]:
        return [u for u in self.users.values() if u["active"] and u["role"] in ("tech", "sub_safety") and (u["wbs_scope"] or "").strip()
                and p6.in_scope(act, u["wbs_scope"])]


# ============================================================================ 自动核验：返回 {state: ok|missing|unknown, text}

def _ok(text): return {"state": "ok", "text": text}
def _missing(text): return {"state": "missing", "text": text}
def _unknown(text): return {"state": "unknown", "text": text}


def _started(ctx: Ctx, a: dict) -> bool:
    return bool(a.get("act_start") or a.get("act_finish") or (a.get("phys_pct") or 0) > 0 or ctx.logmax.get(a["code"], 0) > 0 or a.get("status") in ("进行中", "已完成"))


def _finished(ctx: Ctx, a: dict) -> bool | None:
    if a.get("act_finish") or a.get("status") == "已完成" or (a.get("phys_pct") or 0) >= 100 or ctx.logmax.get(a["code"], 0) >= 100:
        return True
    if not a.get("status") and a.get("phys_pct") is None and not a.get("act_start") and a["code"] not in ctx.logmax:
        return None            # 什么记录都没有，无法判断
    return False


def _pct_text(ctx: Ctx, a: dict) -> str:
    pct = max([x for x in (a.get("phys_pct"), ctx.logmax.get(a["code"])) if x is not None], default=None)
    return "尚未开始" if pct is None or pct <= 0 else f"完成 {pct:g}%"


def chk_prev(ctx: Ctx, act: dict) -> dict:
    rels = [r for r in ctx.preds.get(act["code"], []) if r["kind"] in ("FS", "SS")]
    if not rels:
        return _unknown("P6 中没有约束本作业开工的前置关系，系统无法核验前道工序")
    bad, unsure, good, started = [], [], [], []
    for r in rels:
        p = ctx.by_code.get(r["pred_code"])
        if not p:
            unsure.append(r["pred_code"])
            continue
        label = f"{p['code']} {p['name']}"
        if r["kind"] == "SS":
            # 开始-开始关系：前置作业开了工只说明本作业可以跟进，不等于“前道工序已完成并验收”，算待落实（U-25）
            if _started(ctx, p):
                started.append(f"{label}（{_pct_text(ctx, p)}）")
            else:
                bad.append(label + "（尚未开始）")
            continue
        fin = _finished(ctx, p)
        if fin is True:
            good.append(label)
        elif fin is None:
            unsure.append(label)
        else:
            bad.append(f"{label}（{_pct_text(ctx, p)}）")
    if bad:
        return _missing("前置作业未完成：" + "；".join(bad))
    if unsure or started:
        bits = (["前置作业已开工（开始-开始关系），前道工序是否完成并验收需现场确认：" + "；".join(started)] if started else []) + \
               (["前置作业的完成情况系统没有记录：" + "；".join(unsure)] if unsure else [])
        return _unknown("；".join(bits))
    return _ok("P6 前置作业已完成：" + "；".join(good) + "（是否已验收仍需确认）")


def chk_qc(ctx: Ctx, act: dict) -> dict:
    rows = ctx.qc.get(act["code"], [])
    if not rows:
        return _unknown("系统里没有该作业的质控提醒记录")
    active = [r for r in rows if r["status"] in ("open", "rectifying", "rejected")]
    done = [r for r in rows if r["status"] == "resolved"]
    if active:
        due = min((r["due_date"] for r in active if r["due_date"]), default=None)
        return _missing(f"质控提醒 {len(rows)} 项，{len(active)} 项未闭环" + (f"，最早期限 {due}" if due else ""))
    if done:
        return _ok(f"质控提醒 {len(done)} 项已全部闭环")
    return _unknown("质控提醒都被忽略，没有完成记录")


_CONCEAL_KW = {"rebar": ("钢筋", "rebar", "reinf"), "form": ("模板", "form"), "cage": ("钢筋笼", "钢筋", "笼", "cage", "rebar", "pile"),
               "hole": ("成孔", "孔", "hole", "bore", "桩", "pile")}
_STATUS_NAME = {"draft": "草稿", "ready": "待提交", "submitted": "已提交待验收", "passed": "已通过", "rejected": "未通过"}


def chk_conceal(ctx: Ctx, act: dict, kind: str) -> dict:
    if ctx.conceal is None:
        return _unknown("隐蔽验收记录尚未建立，系统无法核验")
    rows = ctx.conceal.get(act["code"], [])
    if not rows:
        return _unknown("系统里没有该作业的隐蔽验收记录")

    def text(r): return f"{r.get('template_key') or ''} {r.get('part') or ''}".lower()

    def explicit(r, k): return any(w in text(r) for w in _CONCEAL_KW[k])

    if kind == "rebar":      # 明确是模板验收的记录不计入钢筋，其余通过的记录计入
        match = [r for r in rows if not (explicit(r, "form") and not explicit(r, "rebar"))]
    else:
        match = [r for r in rows if explicit(r, kind)]
    passed = [r for r in match if r["status"] == "passed"]
    if passed:
        r = passed[0]
        return _ok(f"隐蔽验收已通过 {len(passed)} 条（{r.get('part') or '未注部位'}" + (f" {r['inspection_date']}" if r.get("inspection_date") else "") + "）")
    if match:
        rej = [r for r in match if r["status"] == "rejected"]
        if rej:
            return _missing(f"隐蔽验收未通过 {len(rej)} 条，尚无通过记录")
        return _unknown(f"已有 {len(match)} 条验收记录（" + "、".join(sorted({_STATUS_NAME.get(r['status'], r['status']) for r in match})) + "），尚无通过记录")
    others = [r for r in rows if r["status"] == "passed"]
    if others:
        return _unknown(f"有 {len(others)} 条已通过的隐蔽验收，但没有标明是否属于本项，请人工确认")
    return _unknown("系统里没有对应的验收记录")


_MAT_KW = {"concrete": ("水泥", "砂", "石", "骨料", "外加剂", "粉煤灰", "矿粉", "混凝土", "钢筋", "钢材"),
           "rebar_form": ("钢筋", "钢材", "焊", "套筒", "连接", "模板", "木"),
           "pile": ("钢筋", "钢材", "混凝土", "水泥", "砂", "石", "外加剂", "泥浆", "护筒"),
           "pave": ("水泥", "碎石", "石", "砂", "沥青", "矿粉", "石灰", "粉煤灰")}


# 材料Agent（tickets）的批次类别代码 → 中文，供与作业名称/材料关键词匹配
_BATCH_CAT_CN = {"rebar": "钢筋", "cement": "水泥", "sand": "砂", "stone": "石", "admixture": "外加剂"}


def chk_material(ctx: Ctx, act: dict, cat: str) -> dict:
    if ctx.batches is None:
        return _unknown("材料批次记录尚未建立，系统无法核验材料到场与复试")
    text = f"{act['name']} {act.get('wbs_path') or ''}"
    kws = _MAT_KW.get(cat, ())
    rel = []
    for b in ctx.batches:
        cn = _BATCH_CAT_CN.get(b.get("category") or "", b.get("category") or "")
        label = f"{cn}{b.get('name') or ''}"
        names = [x for x in (cn, b.get("name")) if x and len(x) >= 1 and x != "other"]
        if any(k in label for k in kws) or any(n in text for n in names):
            rel.append(b)
    if not rel:
        return _unknown(f"没有找到与本作业相关的材料批次（按类别、名称匹配；全项目共 {len(ctx.batches)} 批）")
    fail = [b for b in rel if b["retest_status"] == "fail"]
    wait = [b for b in rel if b["retest_status"] in ("pending", "sampled")]
    ok = [b for b in rel if b["retest_status"] == "pass"]
    if fail or wait:
        parts = []
        if fail:
            parts.append(f"复试不合格 {len(fail)} 批（{'、'.join(str(b.get('name') or b.get('category')) for b in fail[:2])}）")
        if wait:
            parts.append(f"复试未出结果 {len(wait)} 批")
        return _missing("相关材料" + "，".join(parts) + "。已处置的请人工确认")
    if ok:
        return _ok(f"相关材料 {len(rel)} 批，复试合格 {len(ok)} 批，没有未出结果的批次")
    return _unknown(f"相关材料 {len(rel)} 批有到场记录，但没有复试合格记录")


def chk_pours(ctx: Ctx, act: dict) -> dict:
    if ctx.pours is None:
        return _unknown("浇筑记录尚未建立，仅供提示：试块留置与取样仍需现场确认")
    rows = ctx.pours.get(act["code"], [])
    if not rows:
        return _unknown("系统里没有该作业的浇筑记录；试块留置与取样仍需现场确认")
    last = max(rows, key=lambda r: r.get("pour_date") or "")
    return _unknown(f"提示：已有 {len(rows)} 条浇筑记录（最近 {last.get('part') or '未注部位'} {last.get('grade') or ''} {last.get('pour_date') or ''}）；试块留置与取样仍需现场确认")


def run_auto(ctx: Ctx, act: dict, cat: str, kind: str | None) -> dict | None:
    if not kind:
        return None
    if kind == "prev":
        return chk_prev(ctx, act)
    if kind == "qc":
        return chk_qc(ctx, act)
    if kind == "material":
        return chk_material(ctx, act, cat)
    if kind == "pours":
        return chk_pours(ctx, act)
    if kind.startswith("conceal:"):
        return chk_conceal(ctx, act, kind.split(":", 1)[1])
    return None


# ============================================================================ 评估

def effective(auto: dict | None, manual_state: str) -> tuple[str, str]:
    """人工状态优先，否则取系统核验；系统没有核验的项是待落实。返回 (有效状态, 来源 manual|auto|none)。"""
    if manual_state in ("ready", "na", "blocked"):
        return manual_state, "manual"
    if auto is None:
        return "unknown", "none"
    return {"ok": "ready", "missing": "missing", "unknown": "unknown"}[auto["state"]], "auto"


def _cond(ctx: Ctx, act: dict, cat: str, d: dict, row: dict | None) -> dict:
    auto = run_auto(ctx, act, cat, d.get("auto"))
    ms = row["manual_state"] if row else "pending"
    eff, src = effective(auto, ms)
    u = ctx.users.get(row["manual_by"]) if row and row["manual_by"] else None
    owner = ctx.users.get(row["owner_id"]) if row and row["owner_id"] else None
    due = row["due_date"] if row else None
    return {
        "key": d["key"], "label": d["label"], "category": d["category"], "category_name": CAT_NAME.get(d["category"], d["category"]),
        "custom": bool(row and row["custom"]), "auto": bool(d.get("auto")),
        "auto_state": auto["state"] if auto else None,
        "auto_text": auto["text"] if auto else "系统没有这一项的数据，需现场人员确认",
        "effective": eff, "effective_name": EFF_NAME[eff], "source": src,
        "manual": ({"state": ms, "note": row["manual_note"], "by": row["manual_by"], "by_name": u["name"] if u else None, "at": row["manual_at"]}
                   if ms != "pending" else None),
        "conflict": bool(ms == "ready" and auto and auto["state"] == "missing"),     # 人工置“已具备”但系统核到缺项
        "owner": {"id": owner["id"], "name": owner["name"]} if owner else None, "due_date": due,
        "overdue": bool(due and due < ctx.today.isoformat() and eff not in ("ready", "na")),
    }


def conditions(ctx: Ctx, act: dict, cat: str) -> list[dict]:
    rows = {r["cond_key"]: r for r in ctx.items.get(act["code"], [])}
    out = [_cond(ctx, act, cat, d, rows.pop(d["key"], None)) for d in defs_for(cat)]
    for r in rows.values():
        if r["custom"]:
            out.append(_cond(ctx, act, cat, {"key": r["cond_key"], "label": r["label"], "category": "custom", "auto": None}, r))
    return out


def days_to_start(act: dict, today: date) -> int | None:
    s = act.get("early_start") or act.get("target_start")
    return (date.fromisoformat(s[:10]) - today).days if s else None


def verdict_of(conds: list[dict], days: int | None) -> str:
    effs = [c["effective"] for c in conds]
    if "blocked" in effs:
        return "blocked"
    if all(e in ("ready", "na") for e in effs):
        return "ready"
    return "near" if days is not None and days <= NEAR_DAYS else "gap"


def summary(conds: list[dict], days: int | None) -> dict:
    n = lambda *e: sum(1 for c in conds if c["effective"] in e)  # noqa: E731
    gaps = [c for c in conds if c["effective"] not in ("ready", "na")]
    gaps.sort(key=lambda c: ({"blocked": 0, "missing": 1, "unknown": 2}[c["effective"]], c["overdue"] is False))
    v = verdict_of(conds, days)
    return {"verdict": v, "verdict_name": VERDICT_NAME[v], "total": len(conds), "na_n": n("na"), "need_n": len(conds) - n("na"),
            "done_n": n("ready"), "gap_n": len(gaps), "missing_n": n("missing"), "unknown_n": n("unknown"), "blocked_n": n("blocked"),
            "short_n": n("missing", "blocked"), "short_labels": [c["label"] for c in gaps if c["effective"] in ("blocked", "missing")][:3],
            "top_gaps": [{"key": c["key"], "label": c["label"], "effective": c["effective"], "effective_name": c["effective_name"],
                          "owner": c["owner"]["name"] if c["owner"] else None} for c in gaps[:3]]}


_DONE_TAIL = re.compile(r"已(?:完成并验收|完成|批|到位|落实)(?=$|[（(，,])")


def gap_name(label: str) -> str:
    """条件名是完成态（“开工前质控点已完成”）；列成“缺 N 项：……”时去掉完成态，免得读成“缺：已完成”（U-35）。"""
    return _DONE_TAIL.sub("", label, count=1).strip() or label


def gap_text(short_labels: list[str], short_n: int, unknown_n: int) -> str:
    """待办与消息的统一口径：只把真缺项（受阻 + 系统核到确实缺）算作“缺”，待落实另写“待现场确认”（待落实不等于未做）。"""
    parts = []
    if short_n:
        shown = list(short_labels[:2])
        parts.append(f"缺 {short_n} 项：" + "、".join(shown) + ("等" if short_n > len(shown) else ""))
    if unknown_n:
        parts.append(f"另 {unknown_n} 项待现场确认" if parts else f"{unknown_n} 项待现场确认")
    return "；".join(parts)


def late_is_risky(days_to_start: int | None, is_critical: bool, total_float_d: float | None) -> bool:
    """应开未开是否算超期/高风险：关键线路、浮时未知、浮时不足 FLOAT_SLACK_D 天、或晚开天数已吃掉浮时，才算。"""
    if days_to_start is None or days_to_start >= 0:
        return False
    if is_critical or total_float_d is None:
        return True
    return total_float_d < FLOAT_SLACK_D or -days_to_start >= total_float_d


def _act_block(ctx: Ctx, act: dict, cat: dict) -> tuple[dict, list[dict]]:
    conds = conditions(ctx, act, cat["category"])
    days = days_to_start(act, ctx.today)
    mem = ctx.members(act)
    head = {
        "code": act["code"], "name": act["name"], "wbs_path": act.get("wbs_path") or "", "early_start": act.get("early_start"),
        "plan_start": act.get("early_start") or act.get("target_start"), "days_to_start": days,
        "is_critical": bool(act.get("is_critical")), "total_float_d": act.get("total_float_d"), "started": _started(ctx, act),
        "category": cat["category"], "category_name": CAT_NAME[cat["category"]], "category_source": cat["source"],
        "units": sorted({m["company"] or "未标注单位" for m in mem}), "owners": [m["name"] for m in mem if m["role"] == "tech"],
    }
    return {**head, **summary(conds, days)}, conds


# ============================================================================ 权限

def require_view(user: dict) -> None:
    if user["role"] not in VIEWERS:
        raise PermissionError("放行清单仅港湾项目部相关岗位和技术员可见")


def scope_ok(user: dict, act: dict) -> bool:
    return user["role"] != "tech" or p6.in_scope(act, user.get("wbs_scope") or "")


def perms(user: dict) -> dict:
    admin = user["role"] in ADMINS
    return {"edit": admin or user["role"] == "tech", "states": list(ALL_STATES if admin else TECH_STATES if user["role"] == "tech" else ()),
            "admin": admin}


# ============================================================================ 读：列表、详情、首页摘要

def _window(acts: list[dict], today: date, days: int) -> list[dict]:
    out = []
    for a in acts:
        if a.get("is_milestone") or a.get("act_finish") or a.get("act_start") or (a.get("phys_pct") or 0) > 0 or a.get("status") in ("进行中", "已完成"):
            continue
        d = days_to_start(a, today)
        if d is not None and -LOOKBACK <= d <= days:
            out.append(a)
    return out


def _urgent_key(r: dict):
    return (1 if r["verdict"] == "ready" else 0, r["days_to_start"] if r["days_to_start"] is not None else 9999, 0 if r["is_critical"] else 1, r["code"])


def build_list(conn, user: dict, days: int = 14, today: date | None = None, allow_ai: bool = True) -> dict:
    require_view(user)
    if days not in DAYS_OPTIONS:
        raise ValueError("天数只能选 7、14 或 21")
    today = today or date.today()
    pid = user["project_id"]
    imp = p6.current_import(conn, pid)
    base = {"today": today.isoformat(), "days": days, "notice": NOTICE, "perms": perms(user), "has_plan": bool(imp),
            "data_date": imp["data_date"] if imp else None, "cats": CAT_NAME}
    if not imp:
        return {**base, "kpi": {"total": 0, "ready": 0, "near": 0, "gap": 0, "blocked": 0}, "items": [], "truncated": False}
    acts = [a for a in _window(p6.current_activities(conn, pid), today, days) if scope_ok(user, a)]
    cats = classify_all(conn, pid, acts, user["id"], allow_ai=allow_ai)
    ctx = Ctx(conn, pid, p6.current_activities(conn, pid), today)
    rows = [_act_block(ctx, a, cats[a["code"]])[0] for a in acts]
    rows.sort(key=_urgent_key)
    kpi = {"total": len(rows), **{k: sum(1 for r in rows if r["verdict"] == k) for k in ("ready", "near", "gap", "blocked")}}
    return {**base, "kpi": kpi, "items": rows[:MAX_ROWS], "truncated": len(rows) > MAX_ROWS}


def _find_act(conn, user: dict, code: str) -> dict:
    imp = p6.current_import(conn, user["project_id"])
    act = one(conn, "SELECT * FROM activities WHERE import_id=? AND code=?", (imp["id"], code)) if imp else None
    if not act or act["is_milestone"]:
        raise NotFound("作业不存在或不需要开工条件（里程碑）")
    if not scope_ok(user, act):
        raise PermissionError("该作业不在你的负责范围内")
    return act


def detail(conn, user: dict, code: str, today: date | None = None) -> dict:
    require_view(user)
    today = today or date.today()
    act = _find_act(conn, user, code)
    pid = user["project_id"]
    cat = classify_all(conn, pid, [act])[code]
    ctx = Ctx(conn, pid, p6.current_activities(conn, pid), today)
    head, conds = _act_block(ctx, act, cat)
    p = perms(user)
    people = []
    if p["admin"]:
        people = [{"id": u["id"], "name": u["name"], "role_name": R.role_name(u["role"]), "company": u["company"] or ""}
                  for u in ctx.users.values() if u["active"] and u["role"] not in ("admin", "leader")]
    return {**head, "conditions": conds, "perms": p, "people": people, "notice": NOTICE, "today": today.isoformat(),
            "can_add_custom": p["admin"] and sum(1 for c in conds if c["custom"]) < MAX_CUSTOM}


def digest(conn, user: dict, today: date | None = None) -> dict:
    """首页预警小卡：未来 7 天临近缺项/受阻的作业数与前 5 项。不触发模型。"""
    data = build_list(conn, user, 7, today, allow_ai=False)
    hot = [r for r in data["items"] if r["verdict"] in ("near", "blocked")]
    return {"count": len(hot), "blocked": sum(1 for r in hot if r["verdict"] == "blocked"), "has_plan": data["has_plan"],
            "items": [{"code": r["code"], "name": r["name"], "days_to_start": r["days_to_start"], "verdict": r["verdict"],
                       "verdict_name": r["verdict_name"], "gap_n": r["gap_n"], "is_critical": r["is_critical"], "total_float_d": r["total_float_d"],
                       "short_n": r["short_n"], "unknown_n": r["unknown_n"], "blocked_n": r["blocked_n"], "short_labels": r["short_labels"],
                       "first_gap": r["top_gaps"][0]["label"] if r["top_gaps"] else None} for r in hot[:5]]}


# ============================================================================ 写：人工状态、自定义条件、责任人

def _cond_def(ctx: Ctx, act: dict, cat: str, key: str) -> dict:
    for d in defs_for(cat):
        if d["key"] == key:
            return d
    for r in ctx.items.get(act["code"], []):
        if r["cond_key"] == key and r["custom"]:
            return {"key": key, "label": r["label"], "category": "custom", "auto": None}
    raise NotFound("条件不存在")


def _prepare(conn, user: dict, code: str, key: str | None = None):
    require_view(user)
    if not perms(user)["edit"]:
        raise PermissionError("当前角色只能查看，不能修改放行清单")
    act = _find_act(conn, user, code)
    pid = user["project_id"]
    cat = classify_all(conn, pid, [act])[code]["category"]
    ctx = Ctx(conn, pid, [act], date.today())
    d = _cond_def(ctx, act, cat, key) if key else None
    return act, cat, d


def _upsert(conn, pid: int, code: str, d: dict, **cols) -> None:
    ex = one(conn, "SELECT id FROM gate_items WHERE project_id=? AND activity_code=? AND cond_key=?", (pid, code, d["key"]))
    cols["updated_at"] = now_iso()
    if ex:
        conn.execute(f"UPDATE gate_items SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), ex["id"]))
    else:
        insert(conn, "gate_items", {"project_id": pid, "activity_code": code, "cond_key": d["key"], "label": d["label"],
                                    "category": d["category"], "custom": 1 if d["category"] == "custom" else 0, **cols})


def set_state(conn, user: dict, code: str, key: str, state: str, note: str = "") -> dict:
    if state not in ALL_STATES:
        raise ValueError("状态无效")
    act, cat, d = _prepare(conn, user, code, key)
    if user["role"] == "tech" and state not in TECH_STATES:
        raise PermissionError("技术员不能把条件标为“不适用”，请联系工程主管")
    note = (note or "").strip()[:200]
    if state in ("na", "blocked") and len(note) < 2:
        raise ValueError("不适用或受阻需要写明原因")
    pid = user["project_id"]
    with tx(conn):
        if state == "pending":      # 撤销人工状态，回到系统核验
            _upsert(conn, pid, code, d, manual_state="pending", manual_note="", manual_by=None, manual_at=None)
        else:
            _upsert(conn, pid, code, d, manual_state=state, manual_note=note, manual_by=user["id"], manual_at=now_iso())
        audit(conn, user["id"], "gate.state", f"gate:{code}:{key}", f"{state} {note[:120]}")
    return detail(conn, user, code)


def add_custom(conn, user: dict, code: str, label: str) -> dict:
    act, cat, _ = _prepare(conn, user, code)
    if user["role"] not in ADMINS:
        raise PermissionError("增删自定义条件需工程主管或项目经理")
    label = (label or "").strip()
    if not 2 <= len(label) <= 60:
        raise ValueError("条件名称需 2 到 60 个字")
    pid = user["project_id"]
    rows = all_(conn, "SELECT label, custom FROM gate_items WHERE project_id=? AND activity_code=? AND custom=1", (pid, code))
    if len(rows) >= MAX_CUSTOM:
        raise ValueError(f"每个作业最多 {MAX_CUSTOM} 条自定义条件")
    if any(r["label"] == label for r in rows) or any(x["label"] == label for x in defs_for(cat)):
        raise ValueError("已有同名条件")
    key = "c_" + secrets.token_hex(3)
    with tx(conn):
        _upsert(conn, pid, code, {"key": key, "label": label, "category": "custom"})
        audit(conn, user["id"], "gate.custom_add", f"gate:{code}:{key}", label)
    return detail(conn, user, code)


def delete_custom(conn, user: dict, code: str, key: str) -> dict:
    act, cat, _ = _prepare(conn, user, code)
    if user["role"] not in ADMINS:
        raise PermissionError("增删自定义条件需工程主管或项目经理")
    if any(d["key"] == key for d in defs_for(cat)):
        raise ValueError("模板条件不能删除，不适用请标为“不适用”并写明原因")
    row = one(conn, "SELECT * FROM gate_items WHERE project_id=? AND activity_code=? AND cond_key=? AND custom=1", (user["project_id"], code, key))
    if not row:
        raise NotFound("条件不存在")
    with tx(conn):
        conn.execute("DELETE FROM gate_items WHERE id=?", (row["id"],))
        audit(conn, user["id"], "gate.custom_del", f"gate:{code}:{key}", row["label"])
    return detail(conn, user, code)


def assign(conn, user: dict, code: str, key: str, owner_id: int | None, due_date: str | None) -> dict:
    act, cat, d = _prepare(conn, user, code, key)
    if user["role"] not in ADMINS:
        raise PermissionError("指定责任人和期限需工程主管或项目经理")
    pid = user["project_id"]
    due = (due_date or "").strip() or None
    if due:
        try:
            due = date.fromisoformat(due).isoformat()
        except ValueError:
            raise ValueError("期限格式应为 YYYY-MM-DD") from None
    target = None
    if owner_id:
        target = one(conn, "SELECT * FROM users WHERE id=? AND project_id=? AND active=1", (owner_id, pid))
        if not target or target["role"] in ("admin", "leader"):
            raise ValueError("责任人无效")
    prev = one(conn, "SELECT owner_id FROM gate_items WHERE project_id=? AND activity_code=? AND cond_key=?", (pid, code, key))
    with tx(conn):
        _upsert(conn, pid, code, d, owner_id=owner_id or None, due_date=due)
        audit(conn, user["id"], "gate.assign", f"gate:{code}:{key}", f"-> user:{owner_id or '-'} due={due or '-'}")
        if target and target["id"] != user["id"] and (not prev or prev["owner_id"] != target["id"]):
            insert(conn, "notifications", {"project_id": pid, "user_id": target["id"], "alert_id": None,
                                           "title": f"开工条件：{code} {act['name']}",
                                           "body": f"{user['name']}（{R.role_name(user['role'])}）把“{d['label']}”交给你落实，期限 {due or '未设'}。",
                                           "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(), "link_json": dumps(gate_link(code))})
    return detail(conn, user, code)


def gate_link(code: str) -> dict:
    """放行类消息的跳转：开工条件页并展开这道作业（技术员导航里没有这一页，路由仍可直达）。"""
    return {"route": "gate", "params": {"open": code}}


# ============================================================================ 每日提醒

def notify_due(conn, project_id: int, today: date | None = None, within: int = NEAR_DAYS, actor_id: int | None = None) -> dict:
    """对 within 天内开工且仍有未具备项的作业，向缺项责任人（无责任人则向工程主管）发站内通知。
    同一接收人、同一作业、同一天只发一次（用 events 记录去重），可重复调用。"""
    today = today or date.today()
    acts = p6.current_activities(conn, project_id)
    ctx = Ctx(conn, project_id, acts, today)
    win = [a for a in _window(acts, today, within)]
    cats = classify_all(conn, project_id, win)
    leads = [u for u in ctx.users.values() if u["active"] and u["role"] == "eng_sup"] or \
            [u for u in ctx.users.values() if u["active"] and u["role"] == "manager"]
    sent = dup = 0
    hit = 0
    for a in win:
        head, conds = _act_block(ctx, a, cats[a["code"]])
        gaps = [c for c in conds if c["effective"] not in ("ready", "na")]
        gaps.sort(key=lambda c: {"blocked": 0, "missing": 1}.get(c["effective"], 2))       # 真缺项排前，待落实在后
        if not gaps:
            continue
        hit += 1
        by_user: dict[int, list[dict]] = {}
        for c in gaps:
            oid = c["owner"]["id"] if c["owner"] and ctx.users.get(c["owner"]["id"], {}).get("active") else None
            for uid in ([oid] if oid else [u["id"] for u in leads]):
                by_user.setdefault(uid, []).append(c)
        d = head["days_to_start"]
        when = "今天开工" if d == 0 else (f"{d} 天后开工" if d > 0 else f"已超计划开工 {-d} 天")
        for uid, cs in by_user.items():
            key = f"{today.isoformat()}|{a['code']}|{uid}"
            mark = dumps({"k": key})
            if one(conn, "SELECT id FROM events WHERE project_id=? AND kind='gate.notify' AND data_json=?", (project_id, mark)):
                dup += 1
                continue
            labels = "；".join(f"{c['label']}（{c['effective_name']}）" for c in cs[:5]) + (f" 等 {len(cs)} 项" if len(cs) > 5 else "")
            short = [c for c in cs if c["effective"] in ("blocked", "missing")]
            gtxt = gap_text([gap_name(c["label"]) for c in short], len(short), len(cs) - len(short))
            with tx(conn):
                insert(conn, "notifications", {
                    "project_id": project_id, "user_id": uid, "alert_id": None,
                    "title": f"放行预警：{a['code']} {a['name']} {when}，{gtxt}",
                    "body": f"{labels}。请在“开工条件”里落实或说明。条件为通用模板，以项目方案和监理要求为准。",
                    "channels_json": dumps({"inapp": "sent"}), "created_at": now_iso(), "link_json": dumps(gate_link(a["code"]))})
                event(conn, "gate.notify", project_id=project_id, user_id=uid, data={"k": key})
                if actor_id:
                    audit(conn, actor_id, "gate.notify", f"gate:{a['code']}", f"to user:{uid} items={len(cs)}")
            sent += 1
    return {"sent": sent, "duplicates": dup, "activities": hit, "date": today.isoformat()}
