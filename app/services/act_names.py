"""P6 作业代码 → 简短中文名（全站统一显示口径）。

人看“CON-4230”是懵的，看“43号墩位 墩柱施工”才知道是哪儿。规则只用 P6 里已有的作业名称和 WBS 路径，不编造：
- 名称里有中文：去掉夹在中文名里的英文整句与括号里的纯英文缩写/类型注记（如“Temporary Access Road Construction”“（CRCP）”“(Type:1)”），
  桩号、幅别、尺寸照留；“工程：桩号”的冒号改成空格；
- 同一批计划里重名的作业（如 64 个“墩柱施工”），按 WBS 从末级往上补最少的层级把它们区分开（“43号墩位 墩柱施工”），
  层级名里的括号注记（“（2600m）”）去掉；补到 2 级仍重名就保持原样，代码在悬停里看；
- 名称里没有中文：照用原名并标“中文名待补”，清单见 missing()，交项目部在 P6 里补中文名。

用法：
- names(conn, project_id) → {代码: 简称}，按项目当前计划（旧批次的代码作补充）缓存，P6 重新导入后自动失效；
- humanize(text, mp) 把文字里的“代码 名称”“代码「名称」”“[代码] 名称”“名称（代码）”和单独出现的代码换成简称；
- enrich(obj, mp) 给接口返回里带 code / activity_code 的对象补 short_name / activity_short 字段，并把展示文字里的代码换成简称；
- 只换展示：数据库、接口里的 code 字段、AI 结构化输出与出口校验仍按代码；AI 叙述在校验通过后才在出口换成简称。
"""
from __future__ import annotations

import re
import threading
from contextvars import ContextVar
from typing import Any

from ..db import all_, one

_CJK = re.compile(r"[一-鿿]")
# 括号里的纯英文注记：（CRCP）、(Tpye:1)、(Type 2)；保留含数字尺寸的“(1-φ1.2m)”这类
_PAREN_EN = re.compile(r"\s*[（(]\s*(?:[A-Za-z]{2,}(?:\s*[:：]\s*[A-Za-z0-9]+)?|[A-Za-z]+\s+\d+)\s*[)）]")
# 夹在中文名里的英文整句：两个及以上英文单词（首字母大写或全大写），不吃桩号 CH0+000 / K0+000
_EN_PHRASE = re.compile(r"(?<![A-Za-z0-9+])(?:[A-Za-z][a-z]+|[A-Z]{2,})(?:[\s/&,-]+(?:[A-Za-z][a-z]+|[A-Z]{2,}|of|and|for|to|the))+(?![A-Za-z0-9+])")
_SEG_PAREN = re.compile(r"\s*[（(][^（）()]*[)）]")
MISSING_TAG = "（中文名待补）"

_cache: dict[tuple, dict[str, str]] = {}
_lock = threading.Lock()
CURRENT: ContextVar[dict[str, str] | None] = ContextVar("act_names_current", default=None)


def has_cjk(s: str | None) -> bool:
    return bool(_CJK.search(s or ""))


def clean(name: str | None) -> str:
    """单个作业名称 → 去掉英文注记的中文名（不涉及重名区分）。没有中文时原样返回。"""
    s = (name or "").strip()
    if not has_cjk(s):
        return s
    s = _PAREN_EN.sub("", s)
    s = _EN_PHRASE.sub(" ", s)
    s = re.sub(r"\s*[：:]\s*", " ", s)
    s = re.sub(r"\s*([–—~～])\s*", r"\1", s)          # “CH0+000 – CH3+500” → “CH0+000–CH3+500”
    s = re.sub(r"\s+", " ", s).strip(" ：:，,;；-–")
    return s or (name or "").strip()


def _segs(wbs_path: str | None) -> list[str]:
    out = []
    for p in (wbs_path or "").split("/"):
        p = re.sub(r"\s*([–—~～])\s*", r"\1", _SEG_PAREN.sub("", p)).strip()
        if p:
            out.append(p)
    return out


class NameMap(dict):
    """{代码: 简称}，另带 raw = {代码: P6 原名}（识别文字里“代码 原名”用）。"""

    def __init__(self, *a, raw: dict | None = None, **kw):
        super().__init__(*a, **kw)
        self.raw: dict[str, str] = dict(raw or {})


def build(rows: list[dict]) -> NameMap:
    """一批作业（code, name, wbs_path）→ {代码: 简称}。纯函数，便于测试。"""
    base = {r["code"]: clean(r["name"]) for r in rows}
    segs = {r["code"]: _segs(r.get("wbs_path")) for r in rows}
    out = NameMap(base, raw={r["code"]: (r["name"] or "").strip() for r in rows})
    groups: dict[str, list[str]] = {}
    for c, b in base.items():
        groups.setdefault(b, []).append(c)
    for b, codes in groups.items():
        if len(codes) < 2:
            continue
        cands = []
        for depth in (1, 2):
            cand = {}
            for c in codes:
                tail = [x for x in segs[c][-depth:] if x != b and x not in b]
                cand[c] = (" ".join(tail) + " " + b).strip() if tail else b
            cands.append(cand)
        # 每个作业取能区分开的最浅一级；补到 2 级仍与同组重名（名称和 WBS 都相同）就保持原名，代码在悬停里看
        for c in codes:
            for cand in cands:
                if list(cand.values()).count(cand[c]) == 1:
                    out[c] = cand[c]
                    break
    for r in rows:
        if not has_cjk(r["name"]):
            out[r["code"]] = (r["name"] or r["code"]).strip() + MISSING_TAG
    return out


def _sig(conn, project_id: int) -> tuple:
    r = all_(conn, "SELECT id, sha256, imported_at, is_current FROM p6_imports WHERE project_id=? ORDER BY id", (project_id,))
    return (project_id, tuple((x["id"], x["sha256"], x["imported_at"], x["is_current"]) for x in r))


def names(conn, project_id: int | None) -> dict[str, str]:
    """{代码: 简称}。当前批次优先，旧批次里已不存在的代码也给名字（历史预警、日志会引用）。"""
    if not project_id or conn is None:   # 取不到连接（如单测替身）就不换名，不影响业务
        return {}
    key = _sig(conn, project_id)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    rows = all_(conn, "SELECT a.code, a.name, a.wbs_path, i.is_current, i.id imp FROM activities a JOIN p6_imports i ON i.id=a.import_id "
                      "WHERE i.project_id=? ORDER BY i.is_current, i.id", (project_id,))
    by_imp: dict[int, list[dict]] = {}
    for r in rows:
        by_imp.setdefault(r["imp"], []).append(r)
    mp = NameMap()
    for imp_rows in by_imp.values():                   # 旧批次在前、当前批次最后，当前批次覆盖
        b = build(imp_rows)
        mp.update(b)
        mp.raw.update(b.raw)
    with _lock:
        for k in [k for k in _cache if k[0] == project_id]:
            _cache.pop(k, None)
        _cache[key] = mp
    return mp


def bind(conn, project_id: int | None) -> dict[str, str]:
    """把本项目的简称表设为当前上下文（请求或后台任务内），fmt()/label() 无需再传连接。"""
    mp = names(conn, project_id)
    CURRENT.set(mp)
    return mp


def missing(conn, project_id: int) -> list[dict]:
    """当前计划里没有中文名的作业（需在 P6 补中文名）。"""
    from . import p6
    imp = p6.current_import(conn, project_id)
    if not imp:
        return []
    return [r for r in all_(conn, "SELECT code, name, wbs_path FROM activities WHERE import_id=? ORDER BY code", (imp["id"],))
            if not has_cjk(r["name"])]


def _find(code: str | None, name: str | None) -> dict | None:
    """没绑定上下文（后台线程）时，从已加载的简称表里找含这个代码（且原名一致）的那一张。"""
    if not code:
        return None
    for m in list(_cache.values()):
        if code in m and (name is None or getattr(m, "raw", {}).get(code) in (None, name)):
            return m
    return None


def label(code: str | None, name: str | None = None, mp: dict[str, str] | None = None) -> str:
    """一个作业给人看的名字：简称 → 清洗后的原名 → 代码（都没有时）。"""
    mp = (CURRENT.get() or _find(code, name)) if mp is None else mp
    if code and mp and code in mp:
        return mp[code]
    if name:
        return clean(name) if has_cjk(name) else name + MISSING_TAG
    return code or ""


def fmt(code: str | None, name: str | None = None, mp: dict[str, str] | None = None) -> str:
    """交给 AI 的事实文字里写作业：“简称（代码）”，让模型照抄简称；代码留在括号里供结构化输出与校验对照。"""
    lb = label(code, name, mp)
    return f"{lb}（{code}）" if code and lb and lb != code else (lb or code or "")


# ----------------------------------------------------------------------------- 文字替换

_rx_cache: dict[int, tuple[dict, Any]] = {}


def _regex(mp: dict[str, str]):
    hit = _rx_cache.get(id(mp))
    if hit is not None and hit[0] is mp:
        return hit[1]
    codes = sorted((c for c in mp if c), key=len, reverse=True)
    rx = re.compile(r"(?<![A-Za-z0-9_\-])(" + "|".join(re.escape(c) for c in codes) + r")(?![A-Za-z0-9_])") if codes else None
    if len(_rx_cache) > 16:
        _rx_cache.clear()
    _rx_cache[id(mp)] = (mp, rx)
    return rx


def humanize(text: Any, mp: dict[str, str] | None = None, names_: dict[str, str] | None = None) -> Any:
    """文字里的作业代码 → 简称。names_ 可给 {代码: 原名}（缺省时只认简称本身和清洗后的原名紧跟在代码后）。"""
    if not isinstance(text, str) or not text:
        return text
    mp = CURRENT.get() if mp is None else mp
    if not mp or text.strip() in mp:   # 整段只是一个代码：是标识或“作业代码”栏的值，不改
        return text
    raw = dict(getattr(mp, "raw", {}) or {})
    if names_:
        raw.update(names_)
    names_ = raw
    rx = _regex(mp)
    if rx is None or not rx.search(text):
        return text
    out: list[str] = []
    pos = 0
    for m in rx.finditer(text):
        if m.start() < pos:
            continue
        code, short = m.group(1), mp[m.group(1)]
        s, e = m.start(), m.end()
        # 方括号/括号包住的代码：[CON-1000] / （CON-1000） / (CON-1000)
        before = text[pos:s]
        wrapped = None
        for lb_, rb_ in (("[", "]"), ("【", "】"), ("（", "）"), ("(", ")")):
            if before.endswith(lb_) and text[e:e + 1] == rb_:
                wrapped = (lb_, rb_)
                break
        if wrapped:
            s, e = s - 1, e + 1
            before = text[pos:s]
            # “名称（代码）”：前面已经写了名字就整段换成简称
            cands = [x for x in (short, clean((names_ or {}).get(code)), (names_ or {}).get(code), _plain(short)) if x]
            for nm in sorted(set(cands), key=len, reverse=True):
                if wrapped[0] in "（(" and before.rstrip().endswith(nm):
                    cut = before.rstrip()
                    before = cut[: len(cut) - len(nm)]
                    break
        # 代码后面紧跟的名字（空格、「」、冒号）一并吃掉，避免“简称 原名”重复
        rest = text[e:]
        tail = re.match(r"[ 　]*(?:[「“]?)", rest)
        j = tail.end() if tail else 0
        cands = [x for x in ((names_ or {}).get(code), short, clean((names_ or {}).get(code)), _plain(short)) if x]
        ate = False
        for nm in sorted(set(cands), key=len, reverse=True):
            if rest[j:].startswith(nm):
                k = j + len(nm)
                if rest[j - 1:j] in ("「", "“") and rest[k:k + 1] in ("」", "”"):
                    k += 1
                e += k
                ate = True
                break
        out.append(before)
        out.append(short if ate or not rest[:1].strip() or rest[:1] in "，。；：、,.;:）)」】 \n" else short + " ")
        pos = e
    out.append(text[pos:])
    return "".join(out)


def _plain(short: str) -> str:
    """简称去掉补上的 WBS 前缀后的作业本名（“43号墩位 墩柱施工”→“墩柱施工”），用于识别“代码 原名”。"""
    return short.split(" ")[-1] if " " in short else ""


# ----------------------------------------------------------------------------- 接口出口

CODE_KEYS = {"code": "short_name", "activity_code": "activity_short", "act_code": "act_short",
             "pred_code": "pred_short", "succ_code": "succ_short"}
# 这些键的值是标识、原始输入或审计原文，不改写
SKIP_KEYS = {"code", "codes", "activity_code", "act_code", "pred_code", "succ_code", "key", "ref", "route", "fingerprint", "id",
             "url", "href", "path", "sha256", "file_name", "token", "prompt", "system", "user_prompt", "output_text", "raw",
             "input_meta", "input_meta_json", "tokens_json", "wbs_path", "boq_code", "fp", "src_ref", "target", "target_key",
             "dedupe_key", "value", "refs"}


def enrich(obj: Any, mp: dict[str, str], _depth: int = 0) -> Any:
    """接口返回 → 补简称字段、展示文字换简称。原对象就地修改并返回。"""
    if not mp or _depth > 40:
        return obj
    if isinstance(obj, dict):
        names_ = {}
        for k, sk in CODE_KEYS.items():
            v = obj.get(k)
            if isinstance(v, str) and v in mp:
                if sk not in obj:
                    obj[sk] = mp[v]
                nm = obj.get("name") if k == "code" else obj.get(k.replace("code", "name"))
                if isinstance(nm, str):
                    names_[v] = nm
        for k, v in list(obj.items()):
            if k in SKIP_KEYS or k in CODE_KEYS.values() or k.endswith(("_code", "_key", "_id", "_route", "_url", "_path", "_ref")):
                continue
            if isinstance(v, str):
                obj[k] = humanize(v, mp, names_ or None)
            elif isinstance(v, (dict, list)):
                enrich(v, mp, _depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                obj[i] = humanize(v, mp)
            elif isinstance(v, (dict, list)):
                enrich(v, mp, _depth + 1)
    return obj


def humanize_doc(doc, mp: dict[str, str] | None = None) -> None:
    """Word 文档（python-docx）里的作业代码 → 简称：正文、表格逐个 run 替换（代码不会跨 run）。"""
    mp = CURRENT.get() if mp is None else mp
    if not mp:
        return

    def paras(container):
        for p in container.paragraphs:
            yield p
        for t in getattr(container, "tables", []):
            for row in t.rows:
                for cell in row.cells:
                    yield from paras(cell)

    seen = set()
    for p in paras(doc):
        if id(p._p) in seen:
            continue
        seen.add(id(p._p))
        for r in p.runs:
            if r.text:
                new = humanize(r.text, mp)
                if new != r.text:
                    r.text = new
