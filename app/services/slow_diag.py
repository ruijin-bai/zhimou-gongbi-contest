"""工效偏低 / 进度滞后的原因诊断（2026-10-08 增补能力 4）：“慢在哪、证据是哪几条日志”。

- 程序先统计：近 14 天挂接该作业的已确认日志（含自动整理稿）里，人员、设备、材料、天气、图纸/方案、待验收、停工七类信号
  各出现几天、是哪几份日志（[L编号]），以及日均人数与机械台数；没有信号的类别不列；
- 主因按出现天数最多的类别定（同天数按停工 > 设备 > 材料 > 人员 > 天气 > 图纸 > 待验收）；没有任何信号就写“日志未记录原因”，不猜；
- 交融只写一句归因（≤60 字），出口校验：引用的 [L编号] 必须在统计里，数字必须出自统计；不过或模型不可用用规则句；同一份统计只调用一次；
- 结论放在预警详情（GET /api/alerts/{id}/slow-diag），并写进赶工方案（progress_agent.recovery）的提示词依据里。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta

from ..ai.client import AIError, call_json, cause_of
from ..db import all_, loads, now_iso, one
from . import act_names as _an

PROMPT_VERSION = "slowdiag-v1-20261008"
DAYS = 14
ORDER = ("stop", "equipment", "material", "labor", "weather", "drawing", "inspect")
NAME = {"stop": "停工", "equipment": "设备", "material": "材料", "labor": "人员", "weather": "天气", "drawing": "图纸/方案", "inspect": "待验收"}
ISSUE_TYPE = {"设备": "equipment", "机械": "equipment", "材料": "material", "人员": "labor", "劳务": "labor", "天气": "weather", "图纸": "drawing", "设计": "drawing"}
WORDS = {
    "stop": ("停工", "停滞", "暂停施工", "无法施工"),
    "equipment": ("故障", "维修", "停机", "坏了", "抛锚", "配件"),
    "material": ("缺料", "材料不足", "未到场", "没到", "断料", "供应不上", "待料", "进场不足"),
    "labor": ("人手不足", "人员不足", "缺人", "工人少", "班组未到", "人员未到"),
    "weather": ("下雨", "降雨", "暴雨", "大风", "雷雨", "高温"),
    "drawing": ("图纸", "方案未批", "未审批", "变更", "设计未"),
    "inspect": ("待验收", "等验收", "报验", "监理未到", "等待检测"),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS slowdiag_cache (
    key TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
"""


def _hits(text: str) -> set[str]:
    return {k for k, ws in WORDS.items() if any(w in (text or "") for w in ws)}


def signals(conn, pid: int, code: str, day: str | None = None) -> dict:
    """近 14 天挂接该作业的日志信号。"""
    end = date.fromisoformat(day) if day else date.today()
    since = (end - timedelta(days=DAYS)).isoformat()
    rows = all_(conn, """SELECT id, log_date, weather, content_json FROM logs WHERE project_id=? AND mode='ai' AND period='day'
                         AND (status IN ('submitted','approved') OR auto=1) AND log_date>=? AND log_date<=? ORDER BY log_date""", (pid, since, end.isoformat()))
    by: dict[str, dict] = {k: {"days": set(), "refs": [], "samples": []} for k in ORDER}
    logs, labor, mach = [], [], []
    for r in rows:
        c = loads(r["content_json"], {}) or {}
        mine = any(w.get("activity_code") == code for w in c.get("works", [])) or any(p.get("activity_code") == code for p in c.get("progress", [])) \
            or any(i.get("activity_code") == code for i in c.get("issues", []))
        if not mine:
            continue
        logs.append(r["id"])
        labor.append(sum(int(x.get("count") or 0) for x in c.get("labor", []) if str(x.get("count") or "").isdigit()))
        mach.append(sum(int(x.get("count") or 0) for x in c.get("machinery", []) if str(x.get("count") or "").isdigit()))
        found: dict[str, str] = {}
        for i in c.get("issues", []):
            if i.get("activity_code") not in (code, "", None):
                continue
            desc = str(i.get("desc") or "")
            ks = _hits(desc) | ({ISSUE_TYPE[i.get("type")]} if i.get("type") in ISSUE_TYPE else set())
            for k in ks:
                found.setdefault(k, desc[:40])
        for m in c.get("machinery", []):
            if any(w in str(m.get("status") or "") for w in ("故障", "维修", "停机")):
                found.setdefault("equipment", f"{m.get('name') or '机械'}{m.get('status')}")
        if any(w in (r["weather"] or "") for w in ("雨", "暴雨", "大风", "雷")):
            found.setdefault("weather", f"天气：{r['weather']}")
        for k, s in found.items():
            by[k]["days"].add(r["log_date"])
            by[k]["refs"].append(r["id"])
            if len(by[k]["samples"]) < 2:
                by[k]["samples"].append({"log_id": r["id"], "date": r["log_date"], "text": s})
    sig = [{"type": k, "name": NAME[k], "days": len(v["days"]), "refs": sorted(set(v["refs"]))[:6], "samples": v["samples"]} for k, v in by.items() if v["days"]]
    sig.sort(key=lambda s: (-s["days"], ORDER.index(s["type"])))
    avg = lambda xs: round(sum(x for x in xs if x) / len([x for x in xs if x]), 1) if any(xs) else None  # noqa: E731  只算记了人机的日子
    return {"code": code, "since": since, "until": end.isoformat(), "logs": len(logs), "log_ids": logs[-10:], "signals": sig,
            "avg_labor": avg(labor), "avg_machinery": avg(mach), "main": sig[0]["type"] if sig else None}


def rule_text(s: dict) -> str:
    if not s["logs"]:
        return f"近 {DAYS} 天没有挂接该作业的日志，无法判断慢的原因。"
    if not s["signals"]:
        return f"近 {DAYS} 天 {s['logs']} 份相关日志未记录停工、设备、材料、人员、天气等原因，原因待现场说明。"
    m = s["signals"][0]
    refs = "".join(f"[L{i}]" for i in m["refs"][:3])
    rest = "；".join(f"{x['name']} {x['days']} 天" for x in s["signals"][1:3])
    return f"主因：{m['name']}（近 {DAYS} 天有 {m['days']} 天日志提到 {refs}）" + (f"；其次：{rest}" if rest else "") + "。"


SYSTEM = """你是施工项目的进度智能体。系统已统计某作业近 14 天日志里各类原因信号出现的天数和日志编号。
请写一句归因（不超过 60 字）：点出主因并引用日志编号（如[L12]），可提一个次要原因。
硬性要求：只用输入里的数字和日志编号；不得推测输入里没有的原因；没有信号就说“日志未记录原因”。不要 markdown。
只输出 JSON：{"text": "……"}"""
_NUM = re.compile(r"\d+(?:\.\d+)?")


def guard(text: str, s: dict) -> list[str]:
    bad = []
    refs = {f"L{i}" for x in s["signals"] for i in x["refs"]} | {f"L{i}" for i in s["log_ids"]}
    for r in re.findall(r"L\d+", text):
        if r not in refs:
            bad.append(f"引用{r}不在统计里")
    src = json.dumps(s, ensure_ascii=False) + f" {DAYS}"
    ok = {f"{float(x):g}" for x in _NUM.findall(src)}
    clean = re.sub(r"L\d+|\d{4}-\d{2}-\d{2}|[A-Za-z]+\d+", "", text)
    bad += [f"数字{x}无出处" for x in _NUM.findall(clean) if f"{float(x):g}" not in ok]
    return bad


def _norm(d) -> dict:
    t = str((d or {}).get("text") or "").strip() if isinstance(d, dict) else ""
    if not t:
        raise ValueError("缺少 text")
    return {"text": t[:120]}


def diagnose(conn, pid: int, code: str, name: str = "", user_id: int | None = None, day: str | None = None, ai: bool = True,
             cached_only: bool = False) -> dict:
    s = signals(conn, pid, code, day)
    out = {**s, "name": name, "text": rule_text(s), "source": "rule", "note": ""}
    if not ai or not s["signals"]:
        return out
    key = hashlib.sha256(json.dumps({"p": pid, "s": s, "v": PROMPT_VERSION}, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:32]
    hit = one(conn, "SELECT * FROM slowdiag_cache WHERE key=?", (key,))
    if hit:
        return {**out, "text": hit["text"], "source": hit["source"], "note": hit["note"] or ""}
    if cached_only:   # 打开页面不调用模型（验收 I9）：先给系统统计句，人点“AI 归因”再生成
        return {**out, "can_ai": True}
    try:
        res = call_json("progress.slowdiag", system=SYSTEM, user=json.dumps({"作业": _an.fmt(code, name), **s}, ensure_ascii=False, default=str),
                        prompt_version=PROMPT_VERSION, mock=lambda: {"text": out["text"]},
                        validate=_norm,
                        project_id=pid, user_id=user_id, max_tokens=300, temperature=0.2, input_meta={"code": code})
        bad = guard(res.data["text"], s)
        if bad:
            out["note"] = "AI 归因未通过核对（" + "、".join(bad[:2]) + "），用系统统计句"
        elif res.provider == "mock":   # 离线模拟：标 source=mock，界面写“离线模拟模型”，不当作 AI 归因
            out.update(text=res.data["text"], source="mock", note="离线模拟模型输出，仅供开发测试")
        else:
            out.update(text=res.data["text"], source="ai")
    except AIError as exc:
        out["note"] = "AI 暂不可用，用系统统计句（" + cause_of(exc) + "）"
    if out["source"] != "ai":   # 只缓存真实模型成功的归因，失败不缓存（RJ 2026-10-09）
        return out
    conn.execute("INSERT OR REPLACE INTO slowdiag_cache(key, project_id, code, text, source, note, created_at) VALUES (?,?,?,?,?,?,?)",
                 (key, pid, code, out["text"], out["source"], out["note"], now_iso()))
    return out


def prompt_lines(conn, pid: int, slow: list[dict]) -> str:
    """赶工方案提示词用：工效偏低作业的原因统计（规则句，不额外调用模型）。"""
    lines = [f"{_an.fmt(x['code'], x.get('name'))}：{rule_text(signals(conn, pid, x['code']))}" for x in slow[:6]]
    return "【工效偏低原因（系统按日志统计）】\n" + ("\n".join(lines) if lines else "无")
