"""由“工程量清单逐期完成量”反推 P6 各期进度更新（每期一份 XER），并可把清单—作业关联补成系统的“建议”映射。

用法：
  python -m tools.boq_backcast --xer 基线.xer --names 带中文名的同源.xer --boq 清单及产值更新记录.xlsx \
      --rules 关联规则.json --out 输出目录 [--link 项目编码]

口径（输出的 summary.json 里也写明）：
- 关联：按规则文件把清单项分组关联到作业（一组清单项 ↔ 一组作业）；
- 组完成比例的分母两种口径（--denominator）：
  contract（默认）＝组内清单项总产值（至2025年底剩余＋2025年已完成，即合同量口径）；
  p6＝优先用 P6 作业自带的计划工程量（UDF 数量，单位与清单项一致且组内作业都填了才用，按清单单价折成产值），
      否则用该组作业的 P6 费用资源计划金额；分子都是清单累计产值；比例超过 100% 按 100% 计并在 summary 标出；
  两种口径下，规则文件 eac.items 里填了“工程部预计总量”的清单项都优先用它（组内清单项全部填了才用）；
- 规则里 mode=logic 的组不按量反推，其作业交给逻辑推定（如挖淤跟着同段换砂走）；
- 等效计划日：在 P6 基线最早日期上找一个日期 τ，使组内作业按基线线性推进到 τ 时的加权完成比例（权重＝原定工期）等于组完成比例；
  各作业完成%＝其基线窗口在 τ 时的完成比例；实际开始/完成日＝τ 跨过作业基线开始/完成的时刻，在相邻两期之间线性插值；
- 规则文件 plan_based 列出的、清单无法证明进度的作业（设计、采购、调遣、临时工程、施工准备）按基线计划日期推定；
- 其余没有清单对应的作业只按逻辑推定：有 FS 后续已开工的视为已完成（完成日＝后续最早开工前一天），
  SS 后续已开工的视为已开工；其余不动（未开始）。推定的作业在 summary 里单列；
- 剩余工作按 P6 作业日历从数据日期次日重排（FS/SS/FF/SF＋滞后，开始不早于约束），总浮时＝重排完成日到基线最迟完成日的工作日。
这是由产值反推的模拟进度，不是现场实测；对外不得作为实际进度或成效表述。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import p6, p6cal  # noqa: E402

FMT = "%Y-%m-%d %H:%M"
ORIGIN = date(2024, 1, 1)


def d2i(d: date) -> int:
    return (d - ORIGIN).days


def i2d(i: float) -> date:
    return ORIGIN + timedelta(days=math.floor(i + 1e-9))


def pdate(s: str | None) -> date | None:
    v = p6._d(s)
    return date.fromisoformat(v) if v else None


# --------------------------------------------------------------------------- 清单逐期完成量

def read_boq(path: Path) -> tuple[list[date], dict[str, dict]]:
    """→ (各期日期, {清单编号: {name, unit, total_value, cum: [各期累计产值]}})。
    表头：第 3 行为期次（“至2025年底剩余”“2025年已完成”“2026年N月已完成”，之后为周日期），第 4 行为列名；
    月度列为“工程数量/产值”两列（当月完成），周度列为“本周工程量/本周产值/开累工程量/开累产值”四列。"""
    from openpyxl import load_workbook

    ws = load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    h3, h4 = rows[2], rows[3]
    num = lambda v: float(v) if isinstance(v, (int, float)) else 0.0  # noqa: E731
    cols_month, cols_week, remain_col, done25_col = [], [], None, None
    for j, v in enumerate(h3):
        s = str(v or "").strip()
        if s.startswith("至2025年底剩余"):
            remain_col = j
        elif s.startswith("2025年已完成"):
            done25_col = j
        elif s.startswith("2026年") and s.endswith("月已完成"):
            m = int(s[5:-4])
            end = (date(2026, m, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            cols_month.append((end, j))
        elif isinstance(v, datetime):
            assert str(h4[j]).strip() == "本周工程量" and str(h4[j + 3]).strip() == "开累产值", f"第 {j} 列周度表头不认识"
            cols_week.append((v.date(), j))
    if remain_col is None or done25_col is None:
        raise SystemExit("清单表头里没有“至2025年底剩余”“2025年已完成”")
    periods = [date(2025, 12, 31)] + [d for d, _ in cols_month] + [d for d, _ in cols_week]
    items = {}
    for r in rows[4:]:
        code = str(r[0] or "").strip()
        if not code or code.upper().startswith("TOTAL") or code == "汇总":
            continue
        total = num(r[remain_col + 1]) + num(r[done25_col + 1])
        if total <= 0 and not any(isinstance(c, (int, float)) and c for c in r[remain_col + 2:]):
            continue
        cum, acc = [], num(r[done25_col + 1])
        cum.append(acc)
        for _, j in cols_month:
            acc += num(r[j + 1])
            cum.append(acc)
        for _, j in cols_week:
            v = r[j + 3]
            cum.append(num(v) if isinstance(v, (int, float)) else acc)
            acc = cum[-1]
        cq, accq = [num(r[done25_col])], num(r[done25_col])
        for _, j in cols_month:
            accq += num(r[j])
            cq.append(accq)
        for _, j in cols_week:
            v = r[j + 2]
            cq.append(num(v) if isinstance(v, (int, float)) else accq)
            accq = cq[-1]
        rate = num(r[4]) or (cum[-1] / cq[-1] if cq[-1] else 0.0)
        items[code] = {"name": str(r[1] or "").strip(), "unit": str(r[2] or "").strip(), "total_value": total, "cum": cum, "cum_qty": cq, "rate": rate}
    return periods, items


# --------------------------------------------------------------------------- XER

class Plan:
    def __init__(self, xer: Path, names: Path | None):
        self.text = p6.decode(xer.read_bytes())
        self.t = p6.parse_xer(self.text)
        cn = p6.parse_xer(p6.decode(names.read_bytes())) if names else {}
        self.cn_task = {r["task_id"]: r["task_name"] for r in cn.get("TASK", [])}
        self.cn_wbs = {r["wbs_id"]: r["wbs_name"] for r in cn.get("PROJWBS", [])}
        self.wbs = {w["wbs_id"]: w for w in self.t["PROJWBS"]}
        self.tasks = {r["task_code"]: r for r in self.t["TASK"]}
        self.by_id = {r["task_id"]: r["task_code"] for r in self.t["TASK"]}
        rows = []
        for c in self.t["CALENDAR"]:
            spec = p6cal.parse_calendar(c.get("clndr_data") or "")
            rows.append({"clndr_id": c["clndr_id"], "name": c.get("clndr_name", ""), "day_hr": p6._f(c.get("day_hr_cnt")) or 8.0, "spec": spec})
        self.cals = p6cal.build_calendars(rows, ORIGIN)
        default = self.t["PROJECT"][0].get("clndr_id")
        self.cal_default = self.cals.get(default) or next(iter(self.cals.values()))
        self.rels = [(self.by_id[r["pred_task_id"]], self.by_id[r["task_id"]], r.get("pred_type", "PR_FS")[3:], (p6._f(r.get("lag_hr_cnt")) or 0) / 8.0)
                     for r in self.t["TASKPRED"] if r["pred_task_id"] in self.by_id and r["task_id"] in self.by_id]
        self.preds, self.succs = defaultdict(list), defaultdict(list)
        for p, s, k, lag in self.rels:
            self.preds[s].append((p, k, lag))
            self.succs[p].append((s, k, lag))
        self.order = self._topo()
        from app.services import p6ext
        self.ext = {e["code"]: e for e in p6ext.extract(self.t, set(self.tasks))[1]}
        cost_ids = {r["rsrc_id"] for r in self.t.get("RSRC", []) if r.get("rsrc_type") == "RT_Mat" and not (r.get("unit_id") or "").strip()}
        self.cost = defaultdict(float)
        for r in self.t.get("TASKRSRC", []):
            if r["rsrc_id"] in cost_ids and r["task_id"] in self.by_id:
                self.cost[self.by_id[r["task_id"]]] += p6._f(r.get("target_qty")) or 0.0

    def name(self, code: str) -> str:
        r = self.tasks[code]
        return self.cn_task.get(r["task_id"]) or r["task_name"]

    def path(self, code: str) -> str:
        out, w = [], self.tasks[code]["wbs_id"]
        while w in self.wbs and self.wbs[w].get("proj_node_flag") != "Y":
            out.append(self.cn_wbs.get(w) or self.wbs[w]["wbs_name"])
            w = self.wbs[w].get("parent_wbs_id")
        return " / ".join(reversed(out))

    def cal(self, code: str) -> p6cal.Calendar:
        return self.cals.get(self.tasks[code].get("clndr_id")) or self.cal_default

    def dur(self, code: str) -> float:
        r = self.tasks[code]
        return (p6._f(r.get("target_drtn_hr_cnt")) or 0.0) / (self.cal(code).day_hr or 8.0)

    def is_ms(self, code: str) -> bool:
        return self.tasks[code]["task_type"] in ("TT_Mile", "TT_FinMile")

    def window(self, code: str) -> tuple[float, float]:
        """基线窗口（日序号，完成为收工时刻）。"""
        r = self.tasks[code]
        s = pdate(r.get("early_start_date") or r.get("target_start_date"))
        f = pdate(r.get("early_end_date") or r.get("target_end_date"))
        return float(d2i(s)), float(d2i(f) + (0 if self.is_ms(code) else 1))

    def _topo(self) -> list[str]:
        indeg = {c: 0 for c in self.tasks}
        for _, s, _, _ in self.rels:
            indeg[s] += 1
        q = deque(sorted(c for c, n in indeg.items() if n == 0))
        out = []
        while q:
            c = q.popleft()
            out.append(c)
            for s, _, _ in self.succs[c]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    q.append(s)
        return out + [c for c in self.tasks if c not in set(out)]


def select(plan: Plan, group: dict, milestones: bool = False) -> list[str]:
    out = []
    for c in plan.tasks:
        if plan.is_ms(c) and not milestones:
            continue
        pth, nm = plan.path(c), plan.name(c)
        for s in group["select"]:
            if "code" in s and c not in s["code"]:
                continue
            if "wbs" in s and s["wbs"] not in pth:
                continue
            if "name" in s and s["name"] not in nm:
                continue
            if "name_not" in s and s["name_not"] in nm:
                continue
            out.append(c)
            break
    return sorted(out, key=lambda c: (plan.window(c)[0], c))


# --------------------------------------------------------------------------- 反推

def planned_frac(plan: Plan, code: str, tau: float) -> float:
    s, f = plan.window(code)
    if tau <= s:
        return 0.0
    if tau >= f:
        return 1.0
    cal = plan.cal(code)
    tot = cal.work_between(s, f)
    return cal.work_between(s, tau) / tot if tot > 0 else (tau - s) / (f - s)


def group_frac(plan: Plan, codes: list[str], tau: float) -> float:
    w = [max(plan.dur(c), 1.0) for c in codes]
    return sum(wi * planned_frac(plan, c, tau) for wi, c in zip(w, codes)) / sum(w)


def solve_tau(plan: Plan, codes: list[str], target: float) -> float:
    lo = min(plan.window(c)[0] for c in codes)
    hi = max(plan.window(c)[1] for c in codes)
    if target <= 0:
        return lo
    if target >= 1:
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2
        if group_frac(plan, codes, mid) < target:
            lo = mid
        else:
            hi = mid
    return hi


def cross_time(points: list[tuple[float, float]], x: float) -> float | None:
    """points=[(t, τ)…]（τ 不减）：τ 首次达到 x 的时刻 t，相邻点之间线性插值。"""
    for (t0, a), (t1, b) in zip(points, points[1:]):
        if a < x <= b:
            return t0 + (t1 - t0) * ((x - a) / (b - a) if b > a else 1.0)
    return None


_UNIT = {"m³": "m3", "㎡": "m2", "m²": "m2", "no": "no", "no.": "no", "nr.": "no", "piece": "no"}


def p6_denominator(plan: Plan, codes: list[str], its: list[str], items: dict) -> tuple[float, str]:
    """→ (分母产值, 口径说明)。依次：P6 计划工程量（组内只有一个清单项，且单位一致的作业按工期权重覆盖组的 80% 以上，按覆盖率放大），
    P6 费用资源计划金额，最后退回合同量。"""
    from app.services import p6ext
    if len(its) == 1 and codes:
        i = its[0]
        u = _UNIT.get(items[i]["unit"].lower(), items[i]["unit"].lower())
        w = {c: max(plan.dur(c), 1.0) for c in codes}
        hit = {c: v for c in codes for v, uu in [p6ext.quantity_of(plan.ext.get(c, {}))] if v and uu == u}
        cover = sum(w[c] for c in hit) / sum(w.values())
        if hit and cover >= 0.8:
            q = sum(hit.values()) / cover
            return q * items[i]["rate"], f"P6 计划工程量 {q:,.0f} {u}（{i}，{len(hit)}/{len(codes)} 项作业有数量）"
    cost = sum(plan.cost.get(c, 0.0) for c in codes)
    if cost > 0:
        return cost, f"P6 费用资源计划金额 {cost:,.0f}"
    return sum(items[i]["total_value"] for i in its), "P6 无计划量与费用，退回合同量"


def backcast(plan: Plan, periods: list[date], items: dict, groups: list[dict], plan_based: dict | None = None, denominator: str = "contract",
             eac: dict | None = None) -> dict:
    gres = []
    eac = {k: float(v) for k, v in (eac or {}).items() if v}
    for g in groups:
        codes = select(plan, g)
        its = [i for i in g["items"] if i in items]
        if g.get("mode") == "logic":
            gres.append({**g, "codes": codes, "basis": "不按量反推（逻辑推定）", "over_100": None, "items_found": its, "missing_items": [],
                         "frac": [0.0] * len(periods), "tau": [None] * len(periods), "logic": True})
            continue
        total, basis = sum(items[i]["total_value"] for i in its), "合同量（清单总产值）"
        if its and all(i in eac for i in its):
            total, basis = sum(eac[i] * items[i]["rate"] for i in its), "工程部预计总量（" + "、".join(f"{i} {eac[i]:,.0f}" for i in its) + "）"
        elif denominator == "p6":
            total, basis = p6_denominator(plan, codes, its, items)
        raw = [sum(items[i]["cum"][k] for i in its) / total if total else 0.0 for k in range(len(periods))]
        fr = [min(1.0, x) for x in raw]
        taus = [solve_tau(plan, codes, f) if codes else None for f in fr]
        gres.append({**g, "codes": codes, "basis": basis, "over_100": round(max(raw or [0]) * 100, 1) if max(raw or [0]) > 1 else None, "items_found": its, "missing_items": [i for i in g["items"] if i not in items], "frac": fr, "tau": taus})
    mapped = {}
    for g in gres:
        if not g["codes"] or g.get("logic"):
            continue
        # 第 0 点：假设首期之前按 1:1 速度推进（不早于开工令日）
        t1, tau1 = float(d2i(periods[0]) + 1), g["tau"][0]
        tau0 = min(plan.window(c)[0] for c in g["codes"])
        t0 = max(float(d2i(date(2025, 11, 4))), t1 - max(0.0, tau1 - tau0))
        pts = [(t0, tau0)] + [(float(d2i(p) + 1), max(g["tau"][: k + 1])) for k, p in enumerate(periods)]
        for c in g["codes"]:
            s, f = plan.window(c)
            mapped[c] = {"group": g["key"], "start_t": cross_time(pts, s + 1e-6), "finish_t": cross_time(pts, f - 1e-6) if plan.window(c)[1] > s else None,
                         "pts": pts}
    pb = select(plan, plan_based, milestones=True) if plan_based else []
    return {"groups": gres, "mapped": mapped, "plan_based": [c for c in pb if c not in mapped]}


PLAN_BASIS = "按基线计划推定（无清单对应）"


def period_state(plan: Plan, bc: dict, k: int, dd: date) -> dict[str, dict]:
    """第 k 期（数据日期 dd）各作业状态：{code: {pct, act_start, act_finish, basis}}。"""
    ddx = d2i(dd) + 1   # 数据日期当天收工
    st: dict[str, dict] = {}
    for c, m in bc["mapped"].items():
        g = next(x for x in bc["groups"] if x["key"] == m["group"])
        tau = max(t for t in g["tau"][: k + 1])
        pct = round(planned_frac(plan, c, tau) * 100, 1)
        if pct >= 99.5:   # 等效计划日已到完成日附近：按完成计，避免后续已开工、本作业停在 99.9% 的逻辑倒置
            pct = 100.0
        a_s = i2d(m["start_t"]) if m["start_t"] is not None and m["start_t"] <= ddx and pct > 0 else None
        a_f = i2d(m["finish_t"] - 1e-6) if pct >= 100 and m["finish_t"] is not None and m["finish_t"] <= ddx else None
        if pct >= 100 and a_f is None:
            a_f = dd
        if pct > 0 and a_s is None:
            a_s = min(dd, a_f or dd)
        st[c] = {"pct": pct, "act_start": a_s, "act_finish": a_f if pct >= 100 else None, "basis": "清单产值"}
    # 无清单对应的设计、采购、调遣、临时工程等：按基线计划日期推定
    for c in bc.get("plan_based", []):
        if c in st:
            continue
        s, f = plan.window(c)
        pct = round(planned_frac(plan, c, ddx) * 100, 1) if not plan.is_ms(c) else (100.0 if s < ddx else 0.0)
        if pct > 0:
            st[c] = {"pct": pct, "act_start": i2d(s), "act_finish": i2d(f - (0 if plan.is_ms(c) else 1e-6)) if pct >= 100 else None, "basis": PLAN_BASIS}
    # 逻辑推定：逆拓扑，后续已开工 → 前序已完成（FS/FF）或已开工（SS）
    for c in reversed(plan.order):
        if c in st and not (st[c]["basis"] == PLAN_BASIS and st[c]["pct"] < 100):   # 清单依据优先；按计划推定未完的可被逻辑改为完成
            continue
        fs_starts, ss_starts = [], []
        for s, kind, _ in plan.succs[c]:
            x = st.get(s)
            if not x or not x["act_start"]:
                continue
            (ss_starts if kind == "SS" else fs_starts).append(x["act_start"])
        if fs_starts:
            fin = min(fs_starts) - timedelta(days=1)
            fin = min(fin, dd)
            beg = fin if plan.is_ms(c) else i2d(plan.cal(c).sub(d2i(fin) + 1, max(plan.dur(c), 0)))
            st[c] = {"pct": 100.0, "act_start": beg, "act_finish": fin, "basis": "逻辑推定（后续已开工）"}
        elif ss_starts and not plan.is_ms(c):
            beg = min(ss_starts)
            cal = plan.cal(c)
            done = cal.work_between(d2i(beg), ddx)
            pct = round(min(99.0, done / plan.dur(c) * 100), 1) if plan.dur(c) > 0 else 50.0
            st[c] = {"pct": pct, "act_start": beg, "act_finish": None, "basis": "逻辑推定（SS 后续已开工，按计划工期估完成%）"}
    # 里程碑：紧前全部完成即完成（完成日取紧前最晚完成）
    for c in plan.order:
        if not plan.is_ms(c) or (c in st and st[c]["act_finish"]):
            continue
        ps = plan.preds[c]
        if ps and all(st.get(p) and st[p]["act_finish"] for p, _, _ in ps):
            f = max(st[p]["act_finish"] for p, _, _ in ps)
            st[c] = {"pct": 100.0, "act_start": f, "act_finish": f, "basis": "逻辑推定（紧前全部完成）"}
    return st


def reschedule(plan: Plan, st: dict, dd: date) -> dict[str, tuple[float, float, float]]:
    """→ {code: (ES, EF, 尚需工期 工作日)}，日序号；EF 为收工时刻。"""
    ddx = float(d2i(dd) + 1)
    res = {}
    for c in plan.order:
        r, cal, x = plan.tasks[c], plan.cal(c), st.get(c)
        dur = plan.dur(c)
        if x and x["act_finish"]:
            s = float(d2i(x["act_start"] or x["act_finish"]))
            res[c] = (s, float(d2i(x["act_finish"]) + (0 if plan.is_ms(c) else 1)), 0.0)
            continue
        if x and x["act_start"]:
            rem = max(dur * (1 - x["pct"] / 100), 0.5 if dur else 0.0)
            res[c] = (float(d2i(x["act_start"])), cal.add(ddx, rem), rem)
            continue
        es = ddx
        for t, cd in ((r.get("cstr_type"), r.get("cstr_date")), (r.get("cstr_type2"), r.get("cstr_date2"))):
            if t in ("CS_MSOA", "CS_MSO") and pdate(cd):
                es = max(es, float(d2i(pdate(cd))))
        for p, kind, lag in plan.preds[c]:
            ps, pf, _ = res[p]
            if kind == "SS":
                es = max(es, cal.add(ps, lag))
            elif kind == "FF":
                es = max(es, cal.sub(cal.add(pf, lag), dur))
            elif kind == "SF":
                es = max(es, cal.sub(cal.add(ps, lag), dur))
            else:
                es = max(es, cal.add(pf, lag))
        res[c] = (es, cal.add(es, dur), dur)
    return res


def write_xer(plan: Plan, st: dict, sched: dict, dd: date, out: Path) -> dict:
    stamp = f"{dd.isoformat()} 17:00"
    counts = defaultdict(int)
    new_rows = {}
    for c, r in plan.tasks.items():
        x, (es, ef, rem) = st.get(c), sched[c]
        cal = plan.cal(c)
        hpd = cal.day_hr or 8.0
        row = dict(r)
        if plan.cn_task.get(r["task_id"]):
            row["task_name"] = plan.cn_task[r["task_id"]]
        pct = x["pct"] if x else 0.0
        done = bool(x and x["act_finish"])
        started = bool(x and x["act_start"])
        row["status_code"] = "TK_Complete" if done else ("TK_Active" if started else "TK_NotStart")
        row["phys_complete_pct"] = f"{100.0 if done else pct:g}"
        row["act_start_date"] = f"{x['act_start']} 08:00" if started else ""
        row["act_end_date"] = f"{x['act_finish']} 17:00" if done else ""
        row["remain_drtn_hr_cnt"] = f"{0 if done else round(rem * hpd, 2):g}"
        e_s, e_f = i2d(es), i2d(ef - (0 if plan.is_ms(c) else 1e-6))
        row["early_start_date"] = row["restart_date"] = f"{e_s} 08:00"
        row["early_end_date"] = row["reend_date"] = f"{e_f} 17:00"
        lf = pdate(r.get("late_end_date"))
        if lf and not done:
            lfx = float(d2i(lf) + 1)
            tf = cal.work_between(ef, lfx) if lfx >= ef else -cal.work_between(lfx, ef)
            row["total_float_hr_cnt"] = f"{round(tf * hpd, 1):g}"
            row["driving_path_flag"] = "Y" if tf <= 0 else "N"
        counts[row["status_code"]] += 1
        new_rows[r["task_id"]] = row
    proj = dict(plan.t["PROJECT"][0])
    proj["last_recalc_date"] = proj["next_data_date"] = stamp if "next_data_date" in proj else stamp
    # 按原文件逐行替换 TASK、PROJECT、PROJWBS（中文名）的记录，其它表原样保留
    lines, cur, fields = [], None, []
    for line in plan.text.splitlines():
        parts = line.split("\t")
        if parts[0] == "%T":
            cur = parts[1].strip()
        elif parts[0] == "%F":
            fields = [p.strip() for p in parts[1:]]
        elif parts[0] == "%R" and cur in ("TASK", "PROJECT", "PROJWBS"):
            rec = dict(zip(fields, parts[1:]))
            if cur == "TASK":
                rec = new_rows.get(rec.get("task_id"), rec)
            elif cur == "PROJECT":
                rec = {**rec, "last_recalc_date": stamp, **({"next_data_date": stamp} if "next_data_date" in rec else {})}
            elif cur == "PROJWBS" and plan.cn_wbs.get(rec.get("wbs_id")):
                rec = {**rec, "wbs_name": plan.cn_wbs[rec["wbs_id"]]}
            line = "%R\t" + "\t".join(str(rec.get(f, "")) for f in fields)
        lines.append(line)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dict(counts)


# --------------------------------------------------------------------------- 写系统映射（建议）

def link(project_code: str, plan: Plan, groups: list[dict], items: dict) -> dict:
    from app.db import all_, audit, connect, init_db, now_iso, one
    init_db()
    conn = connect()
    proj = one(conn, "SELECT id FROM projects WHERE code=?", (project_code,))
    if not proj:
        raise SystemExit(f"没有项目 {project_code}")
    pid = proj["id"]
    boq = {r["code"]: r for r in all_(conn, "SELECT id, code FROM boq_items WHERE project_id=?", (pid,))}
    have = {r["activity_code"]: r["status"] for r in all_(conn, "SELECT activity_code, status FROM boq_map WHERE project_id=?", (pid,))}
    made, kept, nobq = 0, 0, set()
    with conn:
        for g in groups:
            for c in select(plan, g):
                main = next((v for k, v in (g.get("main_by_name") or {}).items() if k in plan.name(c)), g["main"])
                if have.get(c) in ("confirmed", "rejected"):
                    kept += 1
                    continue
                if main not in boq:
                    nobq.add(main)
                    continue
                reason = f"规则关联（{g['label']}）：{g['reason']}"[:200]
                conn.execute("""INSERT INTO boq_map (project_id, activity_code, boq_id, factor, status, reason, ai_run_id, updated_at)
                                VALUES (?,?,?,?,?,?,NULL,?) ON CONFLICT(project_id, activity_code) DO UPDATE SET boq_id=excluded.boq_id,
                                factor=excluded.factor, reason=excluded.reason, ai_run_id=NULL, updated_at=excluded.updated_at
                                WHERE boq_map.status='suggested'""", (pid, c, boq[main]["id"], 1.0, "suggested", reason, now_iso()))
                made += 1
        audit(conn, None, "boq_map.rule_suggest", f"project:{pid}", f"suggested={made} kept={kept}")
    conn.close()
    return {"suggested": made, "kept_human_decisions": kept, "boq_missing": sorted(nobq)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--xer", required=True, type=Path)
    ap.add_argument("--names", type=Path)
    ap.add_argument("--boq", required=True, type=Path)
    ap.add_argument("--rules", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--prefix", default="LOT1_产值反推")
    ap.add_argument("--denominator", choices=("contract", "p6"), default="contract", help="组完成比例的分母口径")
    ap.add_argument("--link", default="", help="项目编码：把关联写成系统“建议”映射（不改已确认/已驳回的）")
    a = ap.parse_args()
    plan = Plan(a.xer, a.names)
    periods, items = read_boq(a.boq)
    rules = json.loads(a.rules.read_text(encoding="utf-8"))
    groups = rules["groups"]
    a.out.mkdir(parents=True, exist_ok=True)
    bc = backcast(plan, periods, items, groups, rules.get("plan_based"), a.denominator, (rules.get("eac") or {}).get("items"))
    progressed = sorted(c for c, v in items.items() if v["cum"][-1] > 0)
    linked = {i for g in groups for i in g["items"]}
    summary = {"periods": [], "groups": [], "unlinked_progress_items": [c for c in progressed if c not in linked],
               "method": __doc__.split("口径", 1)[1].strip()}
    for g in bc["groups"]:
        summary["groups"].append({"key": g["key"], "label": g["label"], "basis": g["basis"], "over_100": g["over_100"], "items": g["items_found"], "missing_items": g["missing_items"],
                                  "activities": g["codes"], "frac_last": round(g["frac"][-1] * 100, 1)})
    link_rows = []
    for g in bc["groups"]:
        for c in g["codes"]:
            link_rows.append({"group": g["label"], "activity": c, "name": plan.name(c), "wbs": plan.path(c), "items": "、".join(g["items_found"])})
    for k, dd in enumerate(periods):
        st = period_state(plan, bc, k, dd)
        sched = reschedule(plan, st, dd)
        fn = a.out / f"{a.prefix}_{dd.strftime('%Y%m%d')}.xer"
        counts = write_xer(plan, st, sched, dd, fn)
        fin = max(sched.values(), key=lambda v: v[1])[1]
        summary["periods"].append({"data_date": dd.isoformat(), "file": fn.name, "status": counts,
                                   "logic_groups": {g["key"]: sum(1 for c in g["codes"] if st.get(c, {}).get("act_finish")) for g in bc["groups"] if g.get("logic")},
                                   "inferred": sum(1 for v in st.values() if v["basis"].startswith("逻辑")),
                                   "plan_based": sum(1 for v in st.values() if v["basis"] == PLAN_BASIS),
                                   "project_finish": i2d(fin - 1e-6).isoformat(),
                                   "groups": {g["key"]: round(g["frac"][k] * 100, 1) for g in bc["groups"]}})
    (a.out / f"{a.prefix}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "清单-作业关联"
    ws.append(["关联组", "作业代码", "作业名称", "WBS", "清单项", "清单名称", "单位", "关联依据"])
    reasons = {g["label"]: g["reason"] for g in groups}
    for r in link_rows:
        codes = r["items"].split("、") if r["items"] else []
        ws.append([r["group"], r["activity"], r["name"], r["wbs"], r["items"],
                   "；".join(items[c]["name"][:60] for c in codes if c in items), "、".join(items[c]["unit"] for c in codes if c in items), reasons.get(r["group"], "")])
    ws2 = wb.create_sheet("逐期组完成比例")
    keys = [g["key"] for g in bc["groups"]]
    ws2.append(["数据日期"] + [g["label"] for g in bc["groups"]] + ["已完成作业", "进行中作业", "逻辑推定", "按计划推定", "重排完工"])
    for p in summary["periods"]:
        ws2.append([p["data_date"]] + [p["groups"][k] for k in keys] + [p["status"].get("TK_Complete", 0), p["status"].get("TK_Active", 0),
                                                                        p["inferred"], p["plan_based"], p["project_finish"]])
    wb.save(a.out / f"{a.prefix}_关联表与逐期比例.xlsx")

    if a.link:
        summary["link"] = link(a.link, plan, groups, items)
    print(json.dumps({k: summary[k] for k in ("groups", "unlinked_progress_items")}, ensure_ascii=False)[:3000])
    for p in summary["periods"]:
        print(p["data_date"], p["status"], "逻辑推定", p["inferred"], "按计划", p["plan_based"], "完工", p["project_finish"], p["groups"])
    if a.link:
        print("映射", summary["link"])


if __name__ == "__main__":
    main()
