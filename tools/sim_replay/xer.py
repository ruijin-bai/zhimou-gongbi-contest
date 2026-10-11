"""按剧本生成 P6 XER：基线（开工日）与各期更新（数据日期 + 实际开始 + 完成百分比 + 剩余工期，按数据日期重算 CPM）。"""
from __future__ import annotations

from datetime import date, timedelta

from . import scenario as S


def _status(truth: dict, dd: date) -> dict[str, dict]:
    """截至数据日期前一天的实际状态：{code: {start, pct, done}}。"""
    acc = S.cumulative(truth, dd - timedelta(days=1))
    first: dict[str, str] = {}
    for k in sorted(truth):
        if k >= dd.isoformat():
            break
        for works in truth[k].values():
            for w in works:
                if w.qty > 0:
                    first.setdefault(w.code, k)
    out = {}
    for code, name, wbs, dur, qty, *_ in S.PLAN:
        if code == "M100":
            out[code] = {"start": S.START.isoformat(), "pct": 100.0 if dd > S.START else 0.0}
            continue
        pct = min(100.0, acc[code] / qty * 100) if qty else 0.0
        out[code] = {"start": first.get(code), "pct": round(pct, 1)}
    return out


def _schedule(dd: date | None, st: dict | None) -> dict[str, dict]:
    """CPM（天，相对开工日）。dd 为空=基线；否则已开工作业从数据日期起按剩余工期排，未开工的不早于数据日期。"""
    off = (dd - S.START).days if dd else 0
    dur, es, ef = {}, {}, {}
    for code, name, wbs, d0, qty, unit, price, owner, preds in S.PLAN:
        s = st.get(code) if st else None
        if s and s["pct"] >= 100:
            dur[code] = 0
        elif s and s["start"]:
            dur[code] = round(d0 * (1 - s["pct"] / 100), 1)
        else:
            dur[code] = d0
        if s and s["start"]:
            es[code] = float(off) if s["pct"] < 100 else float(off)
            ef[code] = es[code] + dur[code]
            continue
        v = float(off) if dd else 0.0
        for pc, kind, lag in preds:
            ps = st.get(pc) if st else None
            started = bool(ps and ps["start"])
            if kind == "FS":
                v = max(v, ef[pc] + lag)
            elif kind == "SS":
                v = max(v, (es[pc] if not started else (date.fromisoformat(ps["start"]) - S.START).days) + lag)
            elif kind == "FF":
                v = max(v, ef[pc] + lag - dur[code])
        es[code], ef[code] = v, v + dur[code]
    end = max(ef.values())
    succ: dict[str, list] = {}
    for code, *_x, preds in S.PLAN:
        for pc, kind, lag in preds:
            succ.setdefault(pc, []).append((code, kind, lag))
    ls, lf = {}, {}
    for code, *_x, preds in reversed(S.PLAN):
        f = end
        for sc, kind, lag in succ.get(code, []):
            if kind == "FS":
                f = min(f, ls[sc] - lag)
            elif kind == "SS":
                f = min(f, ls[sc] - lag + dur[code])
            elif kind == "FF":
                f = min(f, lf[sc] - lag)
        lf[code], ls[code] = f, f - dur[code]
    return {c: {"es": es[c], "ef": ef[c], "tf": max(0.0, round(ls[c] - es[c], 1)), "dur": dur[c]} for c in es}


def build(truth: dict | None = None, dd: date | None = None) -> str:
    """dd 为空：基线；否则为该数据日期的更新。"""
    st = _status(truth, dd) if dd else None
    base = _schedule(None, None)
    cur = _schedule(dd, st) if dd else base
    ids = {p[0]: str(1000 + i) for i, p in enumerate(S.PLAN)}
    day = lambda off: S.START + timedelta(days=round(off))
    rows, preds = [], []
    for code, name, wbs, d0, qty, unit, price, owner, pr in S.PLAN:
        s = st.get(code) if st else None
        pct = s["pct"] if s else 0.0
        started = bool(s and s["start"]) or (code == "M100" and dd is not None)
        status = "TK_Complete" if pct >= 100 else ("TK_Active" if started else "TK_NotStart")
        act_s = f"{s['start'] if s and s['start'] else S.START.isoformat()} 08:00" if started else ""
        act_e = f"{(s or {}).get('start') or S.START.isoformat()} 17:00" if status == "TK_Complete" else ""
        c = cur[code]
        b = base[code]
        rows.append([ids[code], "1", wbs.split("/")[0], "1", f"{pct:g}", code, name, "TT_Mile" if d0 == 0 else "TT_Task", status,
                     str(c["tf"] * 8), str(c["dur"] * 8), str(d0 * 8), act_s, act_e,
                     f"{day(c['es'])} 08:00", f"{day(c['ef'])} 17:00", f"{day(b['es'])} 08:00", f"{day(b['ef'])} 17:00", "Y" if c["tf"] <= 0 else "N"])
        preds += [(ids[code], ids[p], kind, lag) for p, kind, lag in pr]
    wbs_names = sorted({p[2].split("/")[0] for p in S.PLAN})
    recalc = dd or S.START
    out = ["ERMHDR\t19.12", "%T\tPROJECT", "%F\tproj_id\tproj_short_name\tlast_recalc_date", f"%R\t1\t{S.PROJECT['code']}\t{recalc} 08:00",
           "%T\tCALENDAR", "%F\tclndr_id\tclndr_name\tday_hr_cnt", "%R\t1\t7天工作制\t8",
           "%T\tPROJWBS", "%F\twbs_id\tproj_id\tparent_wbs_id\twbs_short_name\twbs_name\tproj_node_flag", "%R\tROOT\t1\t\tROOT\t深水港疏港路\tY"]
    out += [f"%R\t{w}\t1\tROOT\t{w}\t{w}\tN" for w in wbs_names]
    out += ["%T\tTASK", "%F\ttask_id\tproj_id\twbs_id\tclndr_id\tphys_complete_pct\ttask_code\ttask_name\ttask_type\tstatus_code\ttotal_float_hr_cnt"
            "\tremain_drtn_hr_cnt\ttarget_drtn_hr_cnt\tact_start_date\tact_end_date\tearly_start_date\tearly_end_date\ttarget_start_date\ttarget_end_date\tdriving_path_flag"]
    out += ["%R\t" + "\t".join(r) for r in rows]
    out += ["%T\tTASKPRED", "%F\ttask_pred_id\ttask_id\tpred_task_id\tpred_type\tlag_hr_cnt"]
    out += [f"%R\t{i}\t{s}\t{p}\tPR_{k}\t{lag * 8}" for i, (s, p, k, lag) in enumerate(preds)]
    out.append("%E")
    return "\n".join(out) + "\n"
