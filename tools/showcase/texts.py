"""仿真撰写的文字（由 author.WRITERS 调用）：周报、月报、英文周月报、索赔意向通知、延误分析、联系单、例会底稿、滚动计划、月度复盘。

写法规则（与系统数字守卫一致）：
- 数字只取自事实底稿（工程量、百分比、日期、编号），不自行计算或估计；
- 引用编号 [L日志] [W预警] [S隐患]；对外文书不写人名、金额、产值；
- 内容为虚构仿真项目“仿真公路项目”的演示文字，不代表任何真实项目。
"""
from __future__ import annotations

from tools.showcase.author import writer

EN = {"BR110": "bored piling for the interchange bridge", "BR120": "pile caps for the interchange bridge", "BR130": "piers for the interchange bridge",
      "BR140": "T-beam precasting", "BR150": "T-beam erection", "CV110": "No.1 box slab culvert", "CV120": "No.2 pipe culvert",
      "MB100": "site camp and batching plant", "PR100": "T-beam precast yard", "RD110": "clearing and excavation K0+000–K2+000",
      "RD120": "soft ground replacement K0+000–K2+000", "RD130": "embankment filling K0+000–K2+000", "RD210": "clearing and excavation K2+000–K4+500",
      "RD220": "embankment filling K2+000–K4+500", "DR110": "side ditches and drainage K0–K2", "M100": "Commencement", "M200": "Interchange bridge structure complete",
      "M900": "Whole route open to traffic", "PV110": "cement-stabilised sub-base K0–K2"}
PREP_ZH = {"BR110": "钢筋笼提前一天加工、商品混凝土按桩位预约", "RD110": "挖掘机与自卸车保持编组、弃土场路线雨后及时整平", "RD120": "砂砾换填料备足、压路机到位",
           "RD210": "清表机械转场、沿线社区占道协调", "CV110": "盖板预制与模板周转", "CV120": "管节到场验收与吊装警戒", "PR100": "台座、龙门吊轨道基础与模板安装",
           "MB100": "驻地收尾与拌合站标定", "RD130": "填料试验段与压实度检测准备", "BR140": "T梁模板与张拉设备"}
PREP_EN = {"BR110": "reinforcement cages prefabricated one day ahead and ready-mixed concrete booked per pile", "RD110": "excavator and dump-truck fleets kept together",
           "RD120": "granular replacement material stockpiled and roller on standby", "RD210": "clearing plant to be relocated and community access agreed",
           "CV110": "slab precasting and formwork rotation", "CV120": "pipe units inspected on delivery and lifting zone barricaded",
           "PR100": "casting beds, gantry rail foundations and side forms", "MB100": "camp finishing works and batching plant calibration"}
ISSUE_EN = {"天气": "Adverse weather", "设备": "Equipment", "材料": "Materials", "安全": "Health and safety", "质量": "Quality", "人员": "Labour",
            "协调": "Coordination", "进度": "Progress"}
KEYWORDS = {"BR110": ("桩", "旋挖"), "RD110": ("土方", "清表", "挖方", "弃土"), "RD120": ("换填", "软基"), "CV110": ("盖板涵",), "CV120": ("圆管涵", "管节"),
            "PR100": ("T梁", "台座", "预制场"), "MB100": ("驻地", "拌合站", "板房")}


UNIT_EN = {"根": "piles", "个": "nos", "片": "beams", "m3": "m3", "m2": "m2", "m": "m", "项": "item"}
TRADE_EN = {"普工": "general labourers", "钢筋工": "steel fixers", "混凝土工": "concrete workers", "机手": "plant operators", "电焊工": "welders",
            "电工": "electricians", "木工": "carpenters", "模板工": "formwork carpenters", "机械维修工": "mechanics", "中方人员": "Chinese staff"}
MACH_EN = {"挖掘机": "excavators", "自卸车": "dump trucks", "装载机": "wheel loaders", "吊车": "mobile cranes", "汽车吊": "truck cranes",
           "履带吊": "crawler cranes", "旋挖钻机": "rotary drilling rigs", "推土机": "dozers", "压路机": "rollers", "混凝土罐车": "concrete truck mixers",
           "混凝土泵车": "concrete pumps", "叉车": "forklifts"}
SIG = [  # (关键词, 中文归并标签, 英文描述)
    (("液压",), "旋挖钻液压故障", "hydraulic failure of the rotary drilling rig"),
    (("未到场", "迟到", "未进场"), "第二台旋挖钻未到场", "late arrival of the second rotary drilling rig"),
    (("工效", "仅完成", "仅1台"), "桩基工效偏低", "piling output below the planned rate with a single rig"),
    (("罐车",), "混凝土罐车调度", "irregular availability of hired concrete truck mixers"),
    (("放假", "本地工"), "节日本地工减少", "a reduced local workforce over the public holiday"),
    (("滑倒", "涉水"), "积水湿滑", "waterlogged, slippery ground at the work fronts"),
    (("雷", "雨", "积水"), "降雨停工", "heavy rain and thunderstorms suspending outdoor works"),
]


def _sig(i):
    for words, zh, en in SIG:
        if any(w in i["desc"] for w in words):
            return zh, en
    en = {"安全": "an unsafe condition at the work front, corrected on site", "质量": "a quality check item raised during inspection",
          "天气": "adverse weather", "人员": "a shortage of labour at the work front"}.get(i["type"], "a site issue")
    return i["type"] + i["desc"][:6], en


def _g(v):
    return "未知" if v is None else f"{v:g}"


def _qty(p, sep="、"):
    return sep.join(f"{q['qty']:g}{q['unit']}" for q in p.get("qty", []))


def _pname(p):
    return f"{p['name']}（{p['code']}）"


def _issue_for(f, code, types=None):
    """滞后原因：先找描述里点到该作业的设备/材料/人员问题，再找其他非天气、非安全类问题；找不到返回 None（由调用方按降雨处理）。"""
    words = KEYWORDS.get(code, ())
    hits = [i for i in f.get("issues", []) if any(w in i["desc"] for w in words)]
    for pref in (("设备", "材料", "人员"), ("质量", "协调", "进度")):
        for i in hits:
            if i["type"] in pref:
                return i
    return None


def _top_issues(f, n=5):
    order = {"设备": 0, "材料": 1, "安全": 2, "质量": 3, "人员": 4, "天气": 5}
    sev = {"高": 0, "中": 1, "低": 2}
    seen, out = set(), []
    for i in sorted(f.get("issues", []), key=lambda i: (sev.get(i["severity"], 3), order.get(i["type"], 6), i["date"])):
        key = _sig(i)[0]
        if key in seen or i["severity"] == "低":
            continue
        seen.add(key)
        out.append(i)
        if len(out) >= n:
            break
    return out


def _wet(f):
    return any(i["type"] == "天气" and i["severity"] in ("中", "高") for i in f.get("issues", []))


# ============================================================================ 中文周报 / 月报

@writer("report.week", "report.month")
def report_zh(f, base, meta, user):
    month = f["period_end"][8:10] in ("28", "29", "30", "31") and f["period_start"][8:10] == "01"
    per, nxt = ("本月", "下月") if month else ("本周", "下周")
    prog = sorted(f.get("progress", []), key=lambda p: (not p.get("critical"), p["code"]))
    done = [f"{p['name']}{_qty(p)}" for p in prog if p.get("qty")]
    lg = f["logs"]
    ov = (f"{per}依据已确认每日日志{lg['count']}份、周进度确认{lg.get('weeks', 0)}份。主要完成：" + "；".join(done[:6]) + "。"
          + ("期间多次降雨，土方与桩基作业时有中断，雨后均及时组织排水复工。" if _wet(f) else "现场天气总体正常，各作业面连续施工。"))
    behind = [p for p in prog if p.get("gap") is not None and p["gap"] < 0]
    lines = []
    for p in behind[:4]:
        i = _issue_for(f, p["code"])
        lines.append(f"{_pname(p)}期末累计{_g(p['end_pct'])}%，计划{_g(p['plan_pct'])}%，偏差{_g(p['gap'])}个百分点"
                     + (f"，主要原因：{i['desc'].rstrip('。')}[L{i['log_id']}]" if i else "，主要受降雨停工影响" if _wet(f) else "") + "。")
    ahead = [p for p in prog if p.get("gap") is not None and p["gap"] >= 0 and p.get("end_pct") is not None]
    if ahead:
        lines.append(("其余作业" if lines else "各作业基本按计划推进：") + "、".join(f"{p['name']}累计{_g(p['end_pct'])}%" for p in ahead[:4]) + "。")
    progress = "".join(lines) or "各作业基本按计划推进。"
    risky = [m for m in f.get("milestones", []) if m.get("prob_pct") is not None and m["prob_pct"] < 60]
    ms = ("；".join(f"{m['name']}（{m['code']}）P6日期{m['p6_date']}，预测{m['forecast']}，P80为{m['p80']}，按期概率{m['prob_pct']}%" for m in risky[:3])
          + "。主要驱动为关键线路上的立交桥桩基施工工效，需按双机作业追回。") if risky else "关键节点预计按期。"
    o = f.get("output") or {}
    if o.get("total"):
        cur = o.get("currency") or ""
        top = "、".join(f"{a['name']}{a['value']:g}" for a in (o.get("by_activity") or [])[:3])
        out = (f"{per}系统统计产值{o['total']:g}（{cur}，内部统计，非计量认证），其中港湾核定{_g(o.get('approved_total'))}"
               + (f"；更新产值计划{o['planned']:g}，基准产值计划{_g(o.get('baseline'))}，完成" + ("低于" if o["total"] < o["planned"] else "高于") + "更新计划" if o.get("planned") else "")
               + f"。主要贡献作业：{top}。")
    else:
        out = "产值未统计。"
    s = f.get("safety") or {}
    cats = sorted((s.get("by_category") or {}).items(), key=lambda x: -x[1])[:3]
    saf = (f"{per}发现隐患{s.get('found', 0)}项（高风险{s.get('high', 0)}项），已闭环{s.get('resolved', 0)}项，未闭环{s.get('open', 0)}项；主要类别为"
           + "、".join(f"{k}{v}项" for k, v in cats) + "。")
    oh = s.get("open_high") or []
    if oh:
        saf += "未闭环高风险：" + "；".join(f"[S{h['id']}]{h['title'].split('：')[-1][:14]}（期限{h['due_date']}）" for h in oh[:2]) + "，已列入例会督办。"
    q = f.get("quality") or {}
    qual = (f"{per}记录质量问题{q.get('issues', 0)}项、质控提醒{q.get('reminders', 0)}项。"
            + ("未发现质量问题；桩基混凝土按小票逐车核对坍落度，试块按浇筑部位留置。" if not q.get("issues") else "问题已逐项整改并复查。"))
    iss = []
    for k, i in enumerate(_top_issues(f), 1):
        m = (i.get("measure") or "").rstrip("。")
        iss.append(f"{k}.{i['type']}：{i['desc'].rstrip('。')}[L{i['log_id']}]" + (f"；措施：{m}。" if m and m not in ("无特殊措施", "未写") else "。"))
    nx = f.get("next", [])
    crit = [n for n in nx if n.get("critical")]
    nplan = ((f"{nxt}重点推进关键作业" + "、".join(n["name"] for n in crit[:3]) + "；") if crit else "")
    nplan += ("同步推进" + "、".join([n["name"] for n in nx if not n.get("critical")][:3]) + "。") if nx else ""
    preps = [PREP_ZH[n["code"]] for n in nx if n["code"] in PREP_ZH][:2]
    if preps:
        nplan += "需提前准备：" + "；".join(preps) + "。"
    coord = []
    if any(i["type"] == "设备" for i in f.get("issues", [])):
        coord.append("第二台旋挖钻机进场时间需公司设备部门协调租赁方落实")
    if _wet(f):
        coord.append("雨季停工时段请监理现场签认，作为工期事件依据")
    if any(i["type"] == "材料" for i in f.get("issues", [])):
        coord.append("外租混凝土罐车调度需与供应商确定固定台班")
    return {**base, "overview": ov, "progress": progress, "milestones": ms, "output": out, "safety": saf, "quality": qual,
            "issues": "\n".join(iss) or "无重大问题。", "next_plan": nplan or "按 P6 计划推进。", "coordination": coord}


# ============================================================================ 英文周报 / 月报

@writer("report.weekly_en", "report.monthly_en")
def report_en(f, base, meta, user):
    wk = meta.get("kind") == "weekly_en"
    per = "week" if wk else "month"
    prog = sorted(f.get("progress", []), key=lambda p: (not p.get("critical"), p["code"]))
    qty = [f"{EN.get(p['code'], p['code'])} ({p['code']}): " + ", ".join(f"{q['qty']:g} {UNIT_EN.get(q['unit'], q['unit'])}" for q in p["qty"]) for p in prog if p.get("qty")]
    wet = _wet(f)
    behind = [p for p in prog if p.get("gap") is not None and p["gap"] < 0]
    out = dict(base)
    out["summary"] = (f"This report covers {f['period_start']} to {f['period_end']} and is based on {f['logs']['count']} confirmed daily logs and {f['photos']} site photos. "
                      f"Work this {per} concentrated on " + "; ".join(qty[:5]) + ". "
                      + ("Repeated heavy rainfall and thunderstorms interrupted earthworks and piling; working areas were drained and works resumed after each event. " if wet else "")
                      + ("Bored piling on the critical path remains behind programme and is being recovered with a second rig." if any(p["code"] == "BR110" for p in behind)
                         else "Activities are generally progressing on programme."))
    lines = []
    for p in behind[:4]:
        i = _issue_for(f, p["code"])
        cause = _sig(i)[1] if i else ("rainfall stoppages" if wet else "site constraints")
        lines.append(f"{p['code']} {EN.get(p['code'], '')} is {-p['gap']:g} percentage points behind programme (cumulative {_g(p['end_pct'])}% against {_g(p['plan_pct'])}% planned), "
                     f"mainly due to {cause}" + (f" [L{i['log_id']}]" if i else "") + ".")
    ok = [p for p in prog if p.get("gap") is not None and p["gap"] >= 0]
    if ok:
        lines.append(("Other activities" if lines else "Activities are generally on programme:") + " " + "; ".join(f"{p['code']} cumulative {_g(p['end_pct'])}%" for p in ok[:5]) + ".")
    out["progress"] = " ".join(lines) or "Activities are generally progressing on programme."
    if "comparison" in out:
        risky = [m for m in f.get("milestones", []) if m.get("prob_pct") is not None and m["prob_pct"] < 60]
        out["comparison"] = out["progress"] + (" " + "; ".join(f"{m['code']} {EN.get(m['code'], '')} is forecast for {m['forecast']} against {m['p6_date']} (on-time probability {m['prob_pct']}%)" for m in risky[:3]) + "." if risky else " Key milestones are forecast to be achieved on time.") \
            + " Recovery measures: a second rotary rig for piling, extended working hours after rain and earlier cage fabrication."
    risky = [m for m in f.get("milestones", []) if m.get("prob_pct") is not None and m["prob_pct"] < 60]
    if "milestones" in out:
        out["milestones"] = ("; ".join(f"{m['code']} {EN.get(m['code'], '')} is forecast for {m['forecast']} against the programme date {m['p6_date']} (P80 {m['p80']}, on-time probability {m['prob_pct']}%)" for m in risky[:3])
                             + ". The main driver is the productivity of bored piling on the critical path.") if risky else "Key milestones are forecast to be achieved on time."
    s = f.get("safety") or {}
    out["safety"] = (f"{s.get('found', 0)} hazards were identified this {per} ({s.get('high', 0)} high risk), of which {s.get('resolved', 0)} have been closed and {s.get('open', 0)} remain open. "
                     "The main categories were personal protective equipment, mobile plant segregation and lifting operations. "
                     + "".join(f"Open high-risk hazard [S{h['id']}] is tracked to its due date {h['due_date']}. " for h in (s.get("open_high") or [])[:3])
                     + "Toolbox talks on wet-weather working and plant exclusion zones were held at each work front.")
    q = f.get("quality") or {}
    out["quality"] = (f"{q.get('issues', 0)} quality issues and {q.get('reminders', 0)} QC reminders were recorded this {per}. "
                      + ("Concrete for piles was checked against delivery tickets truck by truck and test cubes were taken for each pour." if not q.get("issues") else "Each issue has been corrected and re-inspected."))
    if "issues" in out:
        il = []
        for k, i in enumerate(_top_issues(f, 5), 1):
            measure = {"设备": "repairs carried out on site and the second rig called forward", "材料": "fixed truck slots agreed with the supplier",
                       "安全": "drainage, warning signs and anti-slip footwear provided", "天气": "works suspended during storms and resumed after drainage",
                       "人员": "local crews recalled and tasks re-sequenced", "质量": "rectified and re-inspected"}.get(i["type"], "addressed on site")
            il.append(f"{k}. {ISSUE_EN.get(i['type'], i['type'])}: {_sig(i)[1]} [L{i['log_id']}]. Measure: {measure}.")
        out["issues"] = "\n".join(il) or "No significant issues were recorded."
    nx = f.get("next", [])
    out["next_plan"] = ("Planned activities: " + "; ".join(f"{n['code']} {EN.get(n['code'], '')}" for n in nx[:7])
                        + (". Resources to be prepared: " + "; ".join([PREP_EN[n["code"]] for n in nx if n["code"] in PREP_EN][:3]) if any(n["code"] in PREP_EN for n in nx) else "") + ".")
    if "coordination" in out:
        co = []
        if wet:
            co.append("Joint record of rainfall stoppages to be signed by the Engineer's site representative.")
        if any(p["code"] == "BR110" for p in behind):
            co.append("Review of the piling recovery sequence for the interchange bridge.")
        out["coordination"] = co
    if "resources" in out:
        lab = "; ".join(f"{TRADE_EN.get(x['name'], x['name'])} {x['avg']:g}" for x in f.get("labor", [])) or "not recorded"
        mac = "; ".join(f"{MACH_EN.get(x['name'], x['name'])} {x['avg']:g}" for x in f.get("machinery", [])) or "not recorded"
        out["resources"] = (f"Average daily personnel by trade: {lab}. Average daily equipment on Site: {mac}. "
                            "Staffing was maintained at all work fronts; plant availability was the main constraint on piling.")
    if "notices" in out and f.get("notices"):
        out["notices"] = base["notices"] + " Entitlement is not assessed in this report."
    return out


# ============================================================================ 索赔意向通知 / 延误分析

CAT_EVENT = {"weather": "Exceptionally heavy rainfall and thunderstorms at the Site caused suspension of outdoor works, waterlogging of excavations and haul roads, and loss of productive working time",
             "equipment": "Breakdown and late arrival of the rotary drilling rig reduced piling output",
             "material": "Late delivery of materials interrupted the planned sequence of works",
             "stop": "Instructed suspension of work stopped activities at the Site",
             "change": "An instruction or external cause interrupted the planned sequence of works"}
CAT_ZH = {"weather": "现场强降雨及雷暴天气导致室外作业暂停、基坑和便道积水，有效工作时间减少",
          "equipment": "旋挖钻机故障及第二台设备迟到，桩基工效下降", "material": "材料迟到打乱了原定施工顺序",
          "stop": "指令停工导致现场作业中断", "change": "指令或外部原因打乱了原定施工顺序"}


def _cat(f):
    return (f.get("event") or {}).get("cat") or (f.get("ref") or f.get("key") or "").split("-")[0]


@writer("report.notice_of_claim")
def notice(f, base, meta, user):
    cat = (meta.get("ref") or "").split("-")[0]
    out = dict(base)
    out["event"] = CAT_EVENT.get(cat, "An event occurred") + ". " + base["event"]
    out["zh"] = base["zh"].replace("承包商就以下事件发出通知：", f"承包商就以下事件发出通知：{CAT_ZH.get(cat, '')}。")
    return out


@writer("report.delay_brief")
def delay_brief(f, base, meta, user):
    cat = (meta.get("ref") or "").split("-")[0]
    out = dict(base)
    out["event"] = CAT_ZH.get(cat, "") + "。" + base["event"]
    follow = list(base.get("follow") or [])
    follow += {"weather": ["补充当日气象记录与停工时段照片，请监理现场签认", "统计受影响作业的停工时段，与日志逐日核对"],
               "equipment": ["补充设备租赁合同约定的进场日期与催告记录", "评估是否属承包商自身风险，商务部门判断是否提出"],
               "material": ["补充供货合同交货期与到货记录"]}.get(cat, [])
    out["follow"] = follow[:5]
    return out


# ============================================================================ 致分包文书

TAIL = "整改完成后请上传整改后照片并在系统中回复，经项目部复查确认后销项。"


@writer("report.work_contact", "report.letter_to_sub", "report.precontrol_notice", "report.docdebt_notice")
def sub_doc(f, base, meta, user):
    out = dict(base)
    req = out.get("requirement") or ""
    if req and TAIL not in req and meta.get("kind") != "precontrol_notice":
        out["requirement"] = req.rstrip("。") + "。" + (TAIL if f.get("src_type") != "alert" else "回复中请写明增加的设备、人员和每日作业时间。")
    if meta.get("kind") == "precontrol_notice":
        out["requirement"] = req.rstrip("。") + "。开工前请将交底记录和设备检定证书上传系统，安全主管现场核查后放行。"
    return out


# ============================================================================ 例会底稿 / 滚动计划 / 复盘

@writer("report.meeting_pack")
def meeting(f, base, meta, user):
    out = dict(base)
    act = f.get("actions") or {}
    head = []
    if act.get("open") or act.get("overdue"):
        head.append("上次例会未完成的行动项逐项过（" + "、".join(i["text"][:14] for i in act.get("items", []) if i["status"] != "done")[:40] + "）")
    head += list(base.get("topics") or [])
    out["opening"] = ("本周会上需拍板：" + "；".join(head[:4]) + "。" if head else "本周没有需要拍板的超期事项。") + base.get("opening", "").split("：", 1)[-1]
    return out


@writer("report.three_week_plan")
def plan(f, base, meta, user):
    out = dict(base)
    out["critical"] = base.get("critical", "") + "关键作业以立交桥桩基为主线，钢筋笼加工、混凝土供应与钻机作业按日对表。"
    out["en_summary"] = base.get("en_summary", "") + " Piling on the interchange bridge remains the critical driver; cage fabrication and concrete supply are coordinated daily."
    return out


@writer("report.monthly_review")
def review(f, base, meta, user):
    out = dict(base)
    lessons = []
    for c in f.get("repeat", [])[:5]:
        m = (c["measures"][0] if c.get("measures") else "").rstrip("。")
        lessons.append(f"{c['area']}·{c['cat']}本月闭环{c['n']}次，平均{_g(c['avg_days'])}天闭环；" + (f"有效做法是{m}；" if m else "")
                       + "下次在班前交底时提前点名该类问题，开工前先查。")
    if lessons:
        out["lessons"] = lessons
    out["summary"] = base.get("summary", "") + "雨季和单机桩基是本月影响进度与安全的两条主线。"
    return out


# ============================================================================ 首页管理简报（演示实例浏览时生成）

@writer("brief.daily")
def brief(f, base, meta, user):
    import re
    reds = re.findall(r"\[W\d+\] ([^\n：]+)：([^（\n]+)", user)
    act, why = (reds[0] if reds else ("关键作业", "进度滞后"))
    act = re.sub(r"^[A-Z]+\d+\s*", "", act).strip()
    items = []
    for i in base.get("items") or []:
        area = i.get("area")
        if area == "进度":
            i = {**i, "detail": f"{act}{why.strip()}，影响下游里程碑", "action": "组织赶工评审，按作业落实增加的设备与班组"}
        elif area == "安全":
            i = {**i, "detail": "高风险隐患集中在基坑临边、气瓶存放和个人防护，多为近两天新发", "action": "督促责任单位当日整改，安全主管现场复查"}
        elif area == "质量":
            i = {**i, "detail": "整改未闭环会影响后续工序验收", "action": "督促整改并复查"}
        items.append(i)
    if not items:
        return {"headline": base.get("headline", ""), "items": []}
    parts = [f"{act}滞后需赶工评审" if any(i.get("area") == "进度" for i in items) else "", "高风险隐患需当日闭环" if any(i.get("area") == "安全" for i in items) else ""]
    return {"headline": "，".join(x for x in parts if x) or base.get("headline", ""), "items": items}
