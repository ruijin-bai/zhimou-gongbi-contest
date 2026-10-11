"""列表页 AI 小结（2026-10-08 用户确认）：预警、安全隐患、质量问题、日志审核、材料票据等列表页顶部的一段判断。

分工（AI 只负责措辞，数字由系统给）：
- 归类、计数、天数等全部由页面按系统接口返回的数据先算好，作为 facts 传入；
- 模型只写 1–2 句判断（先处理哪项、哪些可以批量处理），不得推测原因、不得新增数字；
- 出口做数字校验：AI 文字里出现的每个数字都必须能在 facts 里找到（编号里的数字除外），不过就改用调用方给的规则版文字，
  并如实标注“AI 未通过数字校验，改用系统归并文字”；模型不可用同样退回规则版；
- 同一份 facts 当天只调用一次（按内容摘要缓存），刷新页面不重复调用。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from ..ai.client import AIError, call_json

PROMPT_VERSION = "digest-v1"
KINDS = {
    "alerts": ("进度", "进度预警"),
    "safety": ("安全", "安全隐患"),
    "quality": ("质量", "质量问题"),
    "logs": ("数据审核", "待审核日志"),
    "tickets": ("材料", "材料与混凝土票据"),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS digest_cache (
  key TEXT PRIMARY KEY,
  project_id INTEGER,
  kind TEXT,
  text TEXT,
  source TEXT,
  note TEXT,
  run_id INTEGER,
  provider TEXT,
  created_at TEXT
);
"""

SYSTEM = """你是施工项目的{agent}智能体，正在帮管理人员看“{list}”列表。
下面的 JSON 是系统已经统计好的归类结果：lead 是总数句，key 是需要逐条拍板的事项，groups 是可以合并处理的分组（含数量、编号、天数）。
请写一段 1～2 句、不超过 90 字的中文判断：先点出最该先处理的一项或一组，再说哪些可以合并一次处理，最后给一个处理顺序建议。
硬性要求：
1. 只能使用 JSON 里出现过的数字和编号，不得新增任何数字、日期、百分比、金额；
2. 不得推测原因，不得写“已经”“将会”等没有依据的结论；
3. 不要复述全部明细，不要使用 markdown。
只输出 JSON：{{"text": "……"}}"""

NUM_RE = re.compile(r"\d+(?:\.\d+)?")
CODE_RE = re.compile(r"[A-Za-z]{1,6}[-_]?\d+[A-Za-z0-9#+~\-]*|\d+#|K\d+\+\d+")


def _key(project_id: int, kind: str, facts: dict) -> str:
    raw = json.dumps({"p": project_id, "k": kind, "f": facts, "d": datetime.now().date().isoformat(), "v": PROMPT_VERSION},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def allowed_numbers(facts: dict) -> set[float]:
    """facts 里出现的所有数字（含 lead/分组文字里的数字），作为 AI 文字的白名单。"""
    txt = json.dumps(facts, ensure_ascii=False)
    return {float(x) for x in NUM_RE.findall(CODE_RE.sub(" ", txt))} | {float(x) for x in NUM_RE.findall(txt)}


def check(text: str, facts: dict) -> list[str]:
    """返回 AI 文字里没有出处的数字（空表示通过）。编号（CV110、K0+860、1#）整体剔除后再取数字。"""
    ok = allowed_numbers(facts)
    bad = []
    for x in NUM_RE.findall(CODE_RE.sub(" ", text or "")):
        v = float(x)
        if v not in ok:
            bad.append(x)
    return bad


def mock_text(facts: dict, fallback: str) -> str:
    """离线模拟模型的“判断”：只从 facts 里挑词拼句（先处理哪项、哪组可合并），不产生新数字。"""
    k, g = list(facts.get("key") or []), list(facts.get("groups") or [])
    parts = []
    if k:
        parts.append("先处理" + str(k[0]).split("：")[0])
    if g:
        parts.append(str(g[0]).split("：")[0] + "可以合并一次处理")
        if len(g) > 1:
            parts.append("其余 " + str(len(g) - 1) + " 组随后逐组确认")
    return ("；".join(parts) + "。") if parts else fallback


def summarize(conn, project_id: int, user_id: int | None, kind: str, facts: dict, fallback: str, cached_only: bool = False) -> dict:
    """返回 {text, source: ai|rule, note, provider, run_id}。source=rule 时 text 即调用方给的规则版文字。"""
    if kind not in KINDS:
        raise ValueError("不支持的列表类型")
    key = _key(project_id, kind, facts)
    hit = conn.execute("SELECT text, source, note, run_id, provider FROM digest_cache WHERE key=?", (key,)).fetchone()
    if hit:   # 业务库的行是字典（db.connect 设了 row_factory），这里按列名取
        h = dict(hit) if not isinstance(hit, tuple) else dict(zip(("text", "source", "note", "run_id", "provider"), hit))
        return {"text": h["text"], "source": h["source"], "note": h["note"] or "", "run_id": h["run_id"], "provider": h["provider"] or "", "cached": True}
    if cached_only:   # 没有现成结果：只给规则版，不调用模型
        return {"text": fallback, "source": "rule", "note": "", "run_id": None, "provider": "", "cached": False, "pending": True}
    agent, list_name = KINDS[kind]
    out = {"text": fallback, "source": "rule", "note": "", "run_id": None, "provider": ""}

    def validate(d):
        t = (d or {}).get("text") if isinstance(d, dict) else None
        if not isinstance(t, str) or not t.strip():
            raise ValueError("缺少 text")
        return {"text": re.sub(r"\s+", " ", t.strip())[:160]}

    try:
        res = call_json(f"digest.{kind}", system=SYSTEM.format(agent=agent, list=list_name),
                        user=json.dumps(facts, ensure_ascii=False), prompt_version=PROMPT_VERSION,
                        mock=lambda: {"text": mock_text(facts, fallback)}, validate=validate, project_id=project_id, user_id=user_id,
                        input_meta={"kind": kind}, max_tokens=400, temperature=0.2)
        out.update(run_id=res.run_id, provider=res.provider)
        bad = check(res.data["text"], {**facts, "_n": len(facts.get("groups") or []) - 1})
        if bad:
            out["note"] = "AI 小结里有统计中没有的数字（" + "、".join(bad[:3]) + "），已改用系统归并文字"
        elif res.degraded:
            out["note"] = "AI 输出不合格式，已改用系统归并文字"
        else:   # 离线模拟模型：照样走数字核对，标 source=mock（界面写“离线模拟模型”），不当作 AI 判断
            out.update(text=res.data["text"], source="mock" if res.provider == "mock" else "ai")
    except AIError as exc:
        out["note"] = "AI 暂不可用，显示系统归并文字（" + str(exc)[:40] + "）"
    except Exception as exc:  # noqa: BLE001  小结失败不影响列表本身
        out["note"] = "AI 小结生成失败，显示系统归并文字"
        out["error"] = str(exc)[:80]
    if out["source"] != "ai":   # 只缓存真实模型成功的小结；失败、核对不过、离线模拟都不缓存，下次重新试（RJ 2026-10-09）
        return out
    conn.execute("INSERT OR REPLACE INTO digest_cache(key, project_id, kind, text, source, note, run_id, provider, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (key, project_id, kind, out["text"], out["source"], out["note"], out["run_id"], out["provider"], datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return out
