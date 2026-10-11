"""仿真项目生成器（深水港疏港路，仿真）。

- 数据全部写入一个独立的“仿真项目”（config.simulation=true），界面全程标注，不计入应用成效；
- 若提供真实周产值记录（xlsx：周起/实际产值/计划产值），按记录反推每日工程量，使仿真产值与记录一致；
  否则使用合成 S 曲线；
- 计划经由真实的 XER 导入路径入库；日志、问题、产值映射均走正式业务表；
- 可选 run_agents：在仿真数据上运行数据审核/预警/预测/简报等 Agent（使用当前配置的模型）。
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from .db import all_, connect, dumps, init_db, insert, now_iso, one
from .security import hash_password

SIM_CODE = "DEMO-SIM"

# (code, name, wbs, 开始周, 工期天, 工程量, 单位, 单价USD, 前置, 负责人键)
PLAN = [
    ("M100", "开工", "里程碑", 0, 0, 0, "", 0, [], None),
    ("RD110", "K0+000~K2+000 清表及土方开挖", "路基工程/K0-K2", 0, 35, 60000, "m3", 6.5, ["M100"], "road"),
    ("RD120", "K0+000~K2+000 软基换填", "路基工程/K0-K2", 3, 42, 28000, "m3", 18.0, ["RD110"], "road"),
    ("RD130", "K0+000~K2+000 路基填筑", "路基工程/K0-K2", 7, 56, 85000, "m3", 9.8, ["RD120"], "road"),
    ("RD210", "K2+000~K4+500 清表及土方开挖", "路基工程/K2-K4.5", 4, 42, 72000, "m3", 6.5, ["M100"], "road"),
    ("RD220", "K2+000~K4+500 路基填筑", "路基工程/K2-K4.5", 10, 63, 98000, "m3", 9.8, ["RD210"], "road"),
    ("BR110", "立交桥桩基施工", "桥涵工程/立交桥", 1, 70, 96, "根", 5200, ["M100"], "bridge"),
    ("BR120", "立交桥承台施工", "桥涵工程/立交桥", 9, 42, 12, "个", 38000, ["BR110"], "bridge"),
    ("BR130", "立交桥墩身施工", "桥涵工程/立交桥", 14, 49, 24, "个", 26000, ["BR120"], "bridge"),
    ("BR140", "T梁预制", "桥涵工程/立交桥", 6, 98, 120, "片", 9800, ["M100"], "bridge"),
    ("BR150", "T梁架设", "桥涵工程/立交桥", 21, 35, 120, "片", 2600, ["BR130", "BR140"], "bridge"),
    ("M200", "立交桥主体完成", "里程碑", 26, 0, 0, "", 0, ["BR150"], None),
    ("CV110", "1#盖板涵施工", "桥涵工程/涵洞", 2, 35, 42, "m", 1900, ["M100"], "bridge"),
    ("CV120", "2#圆管涵施工", "桥涵工程/涵洞", 5, 28, 60, "m", 950, ["M100"], "bridge"),
    ("DR110", "K0~K2 边沟及排水管", "排水工程", 9, 56, 4000, "m", 85, ["RD130"], "drain"),
    ("DR120", "K2~K4.5 边沟及排水管", "排水工程", 15, 56, 5000, "m", 85, ["RD220"], "drain"),
    ("PV110", "K0~K2 水泥稳定碎石底基层", "路面工程/K0-K2", 15, 28, 26000, "m2", 7.2, ["RD130"], "pave"),
    ("PV120", "K0~K2 水泥稳定碎石基层", "路面工程/K0-K2", 18, 28, 26000, "m2", 9.5, ["PV110"], "pave"),
    ("PV130", "K0~K2 沥青混凝土下面层", "路面工程/K0-K2", 22, 21, 24000, "m2", 14.0, ["PV120"], "pave"),
    ("PV210", "K2~K4.5 水泥稳定碎石底基层", "路面工程/K2-K4.5", 20, 35, 32000, "m2", 7.2, ["RD220"], "pave"),
    ("PV220", "K2~K4.5 水泥稳定碎石基层", "路面工程/K2-K4.5", 24, 35, 32000, "m2", 9.5, ["PV210"], "pave"),
    ("PV230", "K2~K4.5 沥青混凝土下面层", "路面工程/K2-K4.5", 29, 28, 30000, "m2", 14.0, ["PV220"], "pave"),
    ("PV300", "全线沥青混凝土上面层", "路面工程/全线", 33, 28, 54000, "m2", 12.0, ["PV130", "PV230", "M200"], "pave"),
    ("TE100", "交通标线及护栏", "交通工程", 37, 21, 9000, "m", 32, ["PV300"], "drain"),
    ("M900", "全线通车", "里程碑", 40, 0, 0, "", 0, ["TE100"], None),
]
TECHS = {"road": ("sim_zhao", "赵工（仿真）", "路基工程"), "bridge": ("sim_qian", "钱工（仿真）", "桥涵工程"),
         "pave": ("sim_sun", "孙工（仿真）", "路面工程"), "drain": ("sim_li", "李工（仿真）", "排水工程,交通工程")}
CREW = {
    "road": ([("普工", 10), ("机手", 6)], [("挖掘机", 3), ("自卸车", 8), ("压路机", 2)]),
    "bridge": ([("钢筋工", 12), ("混凝土工", 8), ("木工", 6)], [("旋挖钻", 2), ("汽车吊", 1), ("混凝土罐车", 3)]),
    "pave": ([("普工", 8), ("机手", 6)], [("摊铺机", 1), ("压路机", 3), ("自卸车", 6)]),
    "drain": ([("普工", 9), ("瓦工", 4)], [("挖掘机", 1), ("自卸车", 2)]),
}
# 仿真的两家分包单位及各岗位演示账号（技术员归属见 COMPANY_OF）
COMPANY_OF = {"sim_zhao": "甲分包单位（路基桥涵）", "sim_qian": "甲分包单位（路基桥涵）", "sim_sun": "乙分包单位（路面排水）", "sim_li": "乙分包单位（路面排水）"}
ROLE_ACCOUNTS = [  # (账号, 姓名, 角色, 所属分包单位)
    ("sim_safe_a", "周安全员（仿真·甲）", "sub_safety", "甲分包单位（路基桥涵）"),
    ("sim_safe_b", "吴安全员（仿真·乙）", "sub_safety", "乙分包单位（路面排水）"),
    ("sim_eng", "郑工程主管（仿真）", "eng_sup", ""),
    ("sim_safety", "王安全主管（仿真）", "safety_sup", ""),
    ("sim_admin", "系统管理员（仿真）", "admin", ""),
]


def ensure_role_accounts(conn, project_id: int, password: str = "sim12345") -> list[str]:
    """给仿真项目补齐各岗位演示账号并标注分包单位；只新增缺失账号，不改动其他数据。"""
    added = []
    for uname, company in COMPANY_OF.items():
        conn.execute("UPDATE users SET company=? WHERE username=? AND project_id=? AND company=''", (company, uname, project_id))
    for uname, name, role, company in ROLE_ACCOUNTS:
        if one(conn, "SELECT 1 FROM users WHERE username=?", (uname,)):
            continue
        insert(conn, "users", {"username": uname, "name": name, "role": role, "password_hash": hash_password(password), "project_id": project_id,
                               "wbs_scope": "", "email": "", "active": 1, "created_at": now_iso(), "company": company})
        added.append(uname)
    return added


EVENTS = [  # (概率, 类型, 严重度, 描述, 效率系数)
    (0.05, "天气", "中", "午后强降雨停工约3小时", 0.6),
    (0.03, "设备", "中", "{mach}故障，维修半天", 0.7),
    (0.025, "材料", "中", "{mat}进场不足，影响施工", 0.65),
    (0.02, "外部", "低", "当地社区道路占用协调，短暂停工", 0.8),
    (0.015, "安全", "中", "巡查发现部分工人未系安全带，已现场纠正", 1.0),
    (0.012, "质量", "低", "局部压实度检测不合格，返工处理", 0.85),
]
MATS = {"road": "填料", "bridge": "钢筋", "pave": "沥青", "drain": "管材"}


def _floats() -> dict[str, int]:
    """按计划开始周与 FS 逻辑做后推，得到各作业总浮时（天），作为 P6 浮时的仿真值。"""
    es = {p[0]: p[3] * 7 for p in PLAN}
    ef = {p[0]: es[p[0]] + p[4] for p in PLAN}
    end = max(ef.values())
    succ: dict[str, list[str]] = {}
    for p in PLAN:
        for pr in p[8]:
            succ.setdefault(pr, []).append(p[0])
    lf: dict[str, int] = {}
    for p in reversed(PLAN):
        c = p[0]
        lf[c] = min([lf[s] - next(x[4] for x in PLAN if x[0] == s) for s in succ.get(c, [])] or [end])
    return {c: max(0, lf[c] - ef[c]) for c in ef}


def _xer(start: date) -> str:
    rows, preds = [], []
    ids = {}
    tf = _floats()
    for i, (code, name, wbs, wk, dur, *_rest) in enumerate(PLAN):
        ids[code] = str(1000 + i)
    for code, name, wbs, wk, dur, qty, unit, price, pr, own in PLAN:
        es = start + timedelta(days=wk * 7)
        ef = es + timedelta(days=dur)
        first = code == "M100"  # 开工里程碑已完成
        rows.append([ids[code], "1", wbs.split("/")[0], "1", "100" if first else "0", code, name, "TT_Mile" if dur == 0 else "TT_Task", "TK_Complete" if first else "TK_NotStart",
                     str(tf[code] * 8), str(dur * 8), str(dur * 8), f"{es} 08:00" if first else "", f"{es} 08:00" if first else "", f"{es} 08:00", f"{ef} 17:00", f"{es} 08:00", f"{ef} 17:00", "Y" if tf[code] == 0 else "N"])
        preds += [(ids[code], ids[p]) for p in pr]
    wbs_names = sorted({p[2].split("/")[0] for p in PLAN})
    out = ["ERMHDR\t19.12", "%T\tPROJECT", "%F\tproj_id\tproj_short_name\tlast_recalc_date", f"%R\t1\tDEMO-SIM\t{start} 08:00",
           "%T\tCALENDAR", "%F\tclndr_id\tclndr_name\tday_hr_cnt", "%R\t1\t7天工作制\t8",
           "%T\tPROJWBS", "%F\twbs_id\tproj_id\tparent_wbs_id\twbs_short_name\twbs_name\tproj_node_flag", "%R\tROOT\t1\t\tROOT\t深水港疏港路\tY"]
    out += [f"%R\t{w}\t1\tROOT\t{w}\t{w}\tN" for w in wbs_names]
    out += ["%T\tTASK", "%F\ttask_id\tproj_id\twbs_id\tclndr_id\tphys_complete_pct\ttask_code\ttask_name\ttask_type\tstatus_code\ttotal_float_hr_cnt"
            "\tremain_drtn_hr_cnt\ttarget_drtn_hr_cnt\tact_start_date\tact_end_date\tearly_start_date\tearly_end_date\ttarget_start_date\ttarget_end_date\tdriving_path_flag"]
    out += ["%R\t" + "\t".join(r) for r in rows]
    out += ["%T\tTASKPRED", "%F\ttask_pred_id\ttask_id\tpred_task_id\tpred_type\tlag_hr_cnt"]
    out += [f"%R\t{i}\t{s}\t{p}\tPR_FS\t0" for i, (s, p) in enumerate(preds)]
    out.append("%E")
    return "\n".join(out) + "\n"


def _synthetic_weeks(start: date, end: date) -> dict[str, float]:
    """无真实记录时的合成周产值：按计划工程量×单价在计划窗口内均摊。"""
    w: dict[str, float] = {}
    for code, name, wbs, wk, dur, qty, unit, price, pr, own in PLAN:
        if not dur:
            continue
        per_day = qty * price / dur
        for d in range(dur):
            day = start + timedelta(days=wk * 7 + d)
            if day > end:
                break
            key = (day - timedelta(days=day.weekday())).isoformat()
            w[key] = w.get(key, 0) + per_day
    return w


def build(records: list[dict] | None = None, weeks: int = 18, seed: int = 2026, end: date | None = None, password: str = "sim12345") -> dict:
    """生成仿真项目。records=[{week:'YYYY-MM-DD'(周一), actual:float, planned:float|None}]。"""
    from .services import p6
    init_db()
    rng = random.Random(seed)
    end = end or (date.today() - timedelta(days=1))  # 今天留给现场实际演示
    conn = connect()
    if one(conn, "SELECT 1 FROM projects WHERE code=?", (SIM_CODE,)):
        raise ValueError("仿真项目已存在；如需重建请先删除数据目录中的仿真项目（python -m app simulate --reset）")
    if records:
        recs = sorted(records, key=lambda r: r["week"])
        start = date.fromisoformat(recs[0]["week"])
    else:
        start = end - timedelta(days=end.weekday()) - timedelta(days=7 * (weeks - 1))
        recs = None
    pid = insert(conn, "projects", {"name": "深水港疏港路（仿真）", "code": SIM_CODE,
                                    "config_json": dumps({"simulation": True, "workdays_per_week": 7, "cadence": "week", "daily_log": True}), "created_at": now_iso()})
    users = {}
    for key, (uname, name, scope) in TECHS.items():
        users[key] = insert(conn, "users", {"username": uname, "name": name, "role": "tech", "password_hash": hash_password(password),
                                            "project_id": pid, "wbs_scope": scope, "email": "", "active": 1, "created_at": now_iso(),
                                            "company": COMPANY_OF.get(uname, "")})
    pm = insert(conn, "users", {"username": "sim_pm", "name": "项目经理（仿真）", "role": "manager", "password_hash": hash_password(password),
                                "project_id": pid, "wbs_scope": "", "email": "", "active": 1, "created_at": now_iso()})
    insert(conn, "users", {"username": "sim_leader", "name": "公司领导（仿真）", "role": "leader", "password_hash": hash_password(password),
                           "project_id": pid, "wbs_scope": "", "email": "", "active": 1, "created_at": now_iso()})
    ensure_role_accounts(conn, pid, password)
    p6.import_schedule(conn, pid, pm, "DEMO-SIM-baseline.xer", _xer(start).encode("utf-8"), start.isoformat())
    # 清单与已确认映射（仿真中由商务一次性确认）
    for code, name, wbs, wk, dur, qty, unit, price, pr, own in PLAN:
        if not dur:
            continue
        bid = insert(conn, "boq_items", {"project_id": pid, "code": "B-" + code, "name": name, "unit": unit, "unit_price": price, "currency": "USD", "contract_qty": qty})
        insert(conn, "boq_map", {"project_id": pid, "activity_code": code, "boq_id": bid, "factor": 1, "status": "confirmed", "reason": "仿真：一对一",
                                 "confirmed_by": pm, "updated_at": now_iso()})
    targets = {r["week"]: r["actual"] for r in recs} if recs else _synthetic_weeks(start, end)
    plan_week = _synthetic_weeks(start, end + timedelta(days=400))
    if recs:
        for r in recs:
            insert(conn, "output_records", {"project_id": pid, "period_start": r["week"], "period_end": (date.fromisoformat(r["week"]) + timedelta(days=6)).isoformat(),
                                            "planned": r.get("planned"), "actual": r["actual"], "currency": r.get("currency") or "USD", "source": "record"})
    else:
        for wk_key, v in plan_week.items():
            if wk_key <= end.isoformat():
                insert(conn, "output_records", {"project_id": pid, "period_start": wk_key, "period_end": (date.fromisoformat(wk_key) + timedelta(days=6)).isoformat(),
                                                "planned": round(v, 2), "actual": None, "currency": "USD", "source": "sim", "note": "仿真计划"})
    # 进度推进：每周按目标产值缩放当周各作业的计划日产量
    done = {p[0]: 0.0 for p in PLAN}
    started: dict[str, date] = {}
    finished: dict[str, date] = {}
    plan_by = {p[0]: p for p in PLAN}
    logs_n = 0
    weekly: dict[tuple, dict] = {}
    day = start
    lag = {"BR110": 0.72, "BR120": 0.8, "PV130": 0.85}  # 仿真情景：桥梁桩基工效偏低，形成关键路径风险
    while day <= end:
        wk_key = (day - timedelta(days=day.weekday())).isoformat()
        scale = (targets.get(wk_key, 0) / plan_week[wk_key]) if plan_week.get(wk_key) else 1.0
        scale = max(0.3, min(1.6, scale))
        for own, uid in users.items():
            works, progress, issues = [], [], []
            eff = 1.0
            ev = None
            for prob, typ, sev, desc, f in EVENTS:
                if rng.random() < prob:
                    ev = (typ, sev, desc.format(mach=CREW[own][1][0][0], mat=MATS[own]), f)
                    eff = min(eff, f)
                    break
            for code, name, wbs, wk, dur, qty, unit, price, pr, o in PLAN:
                if o != own or not dur or done[code] >= qty:
                    continue
                es = start + timedelta(days=wk * 7)
                preds_ok = all(done[p] >= plan_by[p][5] * 0.6 or not plan_by[p][4] for p in pr)
                if day < es or not preds_ok:
                    continue
                rate = qty / dur * scale * eff * lag.get(code, 1.0) * rng.uniform(0.8, 1.15)
                q = min(qty - done[code], rate)
                if unit in ("根", "个", "片"):
                    q = float(int(q + rng.random()))
                else:
                    q = round(q, 0)
                if q <= 0:
                    continue
                started.setdefault(code, day)
                done[code] += q
                pct = round(min(100.0, done[code] / qty * 100), 1)
                if pct >= 100:
                    finished[code] = day
                works.append({"part": wbs.split("/")[-1], "process": name, "desc": f"{name}，完成{q:g}{unit}", "activity_code": code, "sources": ["V"]})
                progress.append({"activity_code": code, "qty": q, "unit": unit, "pct": pct, "note": "", "sources": ["V"]})
            if not works:
                continue
            labor = [{"trade": t, "count": float(max(1, n + rng.randint(-2, 2))), "sources": ["V"]} for t, n in CREW[own][0]]
            mach = [{"name": m, "count": float(n), "status": "正常", "sources": ["V"]} for m, n in CREW[own][1]]
            if ev:
                issues.append({"type": ev[0], "desc": ev[2], "severity": ev[1], "measure": "已协调处理", "activity_code": works[0]["activity_code"], "sources": ["V"]})
                if ev[0] == "设备":
                    mach[0]["status"] = "故障"
            content = {"summary": "；".join(w["desc"] for w in works[:3]), "works": works, "progress": progress, "labor": labor, "machinery": mach,
                       "materials": [], "issues": issues, "tomorrow": [{"desc": f"继续{works[0]['process']}", "activity_code": works[0]["activity_code"], "sources": ["V"]}],
                       "photo_notes": [], "notes": "", "uncertain": []}
            voice = "；".join(w["desc"] for w in works) + ("。" + ev[2] if ev else "") + "。明天继续施工"
            # 每日日志由现场记录Agent从随手记整理（auto=1）；状态随周确认生效
            ws = day - timedelta(days=day.weekday())
            week_done = ws + timedelta(days=6) <= end
            last_week = week_done and (end - (ws + timedelta(days=6))).days < 7
            status = ("submitted" if last_week else "approved") if week_done else "draft"
            sub_at = f"{(ws + timedelta(days=6)).isoformat()}T{rng.randint(17, 21)}:{rng.randint(10, 59)}:00" if week_done else None
            lid = insert(conn, "logs", {"project_id": pid, "author_id": uid, "log_date": day.isoformat(), "mode": "ai", "status": status,
                                        "weather": rng.choice(["晴 32℃", "多云 30℃", "阵雨 28℃"]), "voice_text": voice, "content_json": dumps(content),
                                        "checks_json": "[]", "started_at": f"{day.isoformat()}T07:00:00", "submitted_at": sub_at, "drafted_at": f"{day.isoformat()}T20:00:00",
                                        "reviewed_by": pm if status == "approved" else None, "reviewed_at": sub_at if status == "approved" else None,
                                        "qa_json": "[]", "period": "day", "auto": 1})
            for k, part in enumerate(voice.split("；")):
                if part.strip():
                    insert(conn, "notes", {"project_id": pid, "author_id": uid, "note_date": day.isoformat(), "text": part.strip()[:300],
                                           "media_json": "[]", "source": "voice", "seconds": float(rng.randint(6, 25)),
                                           "created_at": f"{day.isoformat()}T{8 + 3 * k:02d}:{rng.randint(0, 59):02d}:00"})
            wk = weekly.setdefault((uid, ws.isoformat()), {"progress": {}, "labor": {}, "mach": {}, "works": [], "issues": [], "days": 0, "done": week_done, "status": status, "sub_at": sub_at})
            wk["days"] += 1
            for p in progress:
                w = wk["progress"].setdefault(p["activity_code"], {"activity_code": p["activity_code"], "qty": 0.0, "unit": p["unit"], "pct": None, "note": "", "sources": []})
                w["qty"] += p["qty"]
                w["pct"] = p["pct"]
            for x in labor:
                wk["labor"].setdefault(x["trade"], []).append(x["count"])
            for x in mach:
                wk["mach"].setdefault(x["name"], []).append(x["count"])
            wk["works"] += works[:2]
            wk["issues"] += issues
            logs_n += 1
        day += timedelta(days=1)
    # 周确认：已过完的每一周每人一份（上周的待审核，更早的已审核），正式进度只来自周确认
    for (uid, ws), wk in weekly.items():
        if not wk["done"]:
            continue
        we = (date.fromisoformat(ws) + timedelta(days=6)).isoformat()
        content = {"summary": f"本周记录{wk['days']}天；" + "；".join(w["desc"] for w in wk["works"][:3]),
                   "works": wk["works"][:12], "progress": [dict(v, qty=round(v["qty"], 2), sources=["D1"]) for v in wk["progress"].values()],
                   "labor": [{"trade": k, "count": round(sum(v) / len(v), 1), "sources": ["D1"]} for k, v in wk["labor"].items()],
                   "machinery": [{"name": k, "count": round(sum(v) / len(v), 1), "status": "正常", "sources": ["D1"]} for k, v in wk["mach"].items()],
                   "materials": [], "issues": wk["issues"][:8], "tomorrow": [], "photo_notes": [], "notes": "", "uncertain": []}
        lid = insert(conn, "logs", {"project_id": pid, "author_id": uid, "log_date": ws, "period_end": we, "period": "week", "mode": "ai",
                                    "status": wk["status"], "content_json": dumps(content), "checks_json": "[]", "started_at": wk["sub_at"],
                                    "submitted_at": wk["sub_at"], "reviewed_by": pm if wk["status"] == "approved" else None,
                                    "reviewed_at": wk["sub_at"] if wk["status"] == "approved" else None, "qa_json": "[]"})
        conn.executemany("INSERT INTO log_progress (log_id, project_id, log_date, activity_code, qty, unit, pct, note) VALUES (?,?,?,?,?,?,?,?)",
                         [(lid, pid, min(we, end.isoformat()), p["activity_code"], p["qty"], p["unit"], p["pct"], "") for p in content["progress"]])
    conn.close()
    return {"project_id": pid, "start": start.isoformat(), "end": end.isoformat(), "logs": logs_n, "weeks": len(targets),
            "accounts": ["sim_pm", "sim_leader"] + [a[0] for a in ROLE_ACCOUNTS] + [t[0] for t in TECHS.values()], "password": password, "from_records": bool(recs)}


def run_agents(project_id: int, recent_days: int = 7) -> dict:
    """在仿真数据上运行各 Agent（数据审核、问题并轨、预测、预警、质控提醒、简报）。"""
    from .services import alerts as alert_svc, brief, progress, schedule, site_agents
    conn = connect()
    try:
        since = (date.today() - timedelta(days=recent_days)).isoformat()
        logs = all_(conn, "SELECT * FROM logs WHERE project_id=? AND period='week' AND status='submitted'", (project_id,))
        for lg in all_(conn, "SELECT * FROM logs WHERE project_id=? AND period='day'", (project_id,)):
            site_agents.quality_from_log(conn, lg)
        for lg in logs:
            site_agents.dq_review(conn, lg["id"], None)
        fc = schedule.forecast(conn, project_id)
        det = progress.detect(conn, project_id)
        alert_svc.process_new(conn, det["new"][:6], None, explain_limit=4)
        qc = site_agents.quality_reminders(conn, project_id)
        b = brief.generate(conn, project_id, None)
        return {"dq_reviewed": len(logs), "alerts": len(det["new"]), "milestones": len((fc or {}).get("milestones", [])), "qc": qc, "brief": b.get("headline")}
    finally:
        conn.close()


def reset() -> bool:
    init_db()
    conn = connect()
    try:
        p = one(conn, "SELECT id FROM projects WHERE code=?", (SIM_CODE,))
        if not p:
            return False
        pid = p["id"]
        conn.execute("PRAGMA foreign_keys=OFF")
        for t in ("log_progress", "notes", "notifications", "alerts", "findings", "boq_map", "boq_items", "output_records", "briefs", "knowledge",
                  "forecasts", "reports", "events", "ai_runs", "media"):
            conn.execute(f"DELETE FROM {t} WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM logs WHERE project_id=?", (pid,))
        imps = [r["id"] for r in all_(conn, "SELECT id FROM p6_imports WHERE project_id=?", (pid,))]
        for i in imps:
            conn.execute("DELETE FROM activities WHERE import_id=?", (i,))
            conn.execute("DELETE FROM relations WHERE import_id=?", (i,))
        conn.execute("DELETE FROM p6_imports WHERE project_id=?", (pid,))
        other = one(conn, "SELECT id FROM projects WHERE id<>? ORDER BY id LIMIT 1", (pid,))
        conn.execute("UPDATE users SET project_id=? WHERE project_id=? AND username NOT LIKE 'sim\\_%' ESCAPE '\\'", (other["id"] if other else None, pid))
        conn.execute("DELETE FROM users WHERE project_id=? AND username LIKE 'sim\\_%' ESCAPE '\\'", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.execute("PRAGMA foreign_keys=ON")
        return True
    finally:
        conn.close()
