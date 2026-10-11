"""合同 / 规范条款库与问答引用（2026-10-08 增补能力 10）。

- 导入：工程主管把合同条件、特殊条款、技术规范的文字（粘贴或上传 .txt/.docx）导入本项目的条款库，按条款号自动切分
  （Sub-Clause 20.2 / Clause 4.20 / 20.2.1 / 第 12 条），切不出条款号的按段落切；原文只存在本机业务库，导入前请确认保密要求；
- 检索：按条款号、中英文关键词打分（不调用模型），返回条款原文；
- 问答：统一输入框“提问”里问到合同、条款、规范时，问工笔把命中的条款原文作为检索结果交给交融，回答只能依据这些原文并标 [C编号]
  （条款编号见条款库），没有命中就说没有找到，不编条款号；
- 与队友作品“中港智典”（制度问答）是同一类技术，复选时注意不要两个作品讲同一个卖点。
"""
from __future__ import annotations

import io
import re

from ..db import all_, audit, insert, now_iso, one

IMPORTERS = frozenset({"eng_sup"})  # 项目经理只关注进度、质量、安全，条款库只读
READERS = frozenset({"manager", "eng_sup", "safety_sup"})
KINDS = {"contract": "合同通用条件", "particular": "合同专用/特殊条款", "spec": "技术规范", "other": "其他"}
MAX_CHARS = 400_000
CONTRACT_RE = re.compile(r"合同|条款|规范|FIDIC|[Ss]ub-?[Cc]lause|[Cc]lause|[Ss]pecification|通用条件|专用条件|特殊条件|索赔通知|期限.*通知")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contract_docs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    clauses INTEGER NOT NULL DEFAULT 0,
    chars INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contract_clauses (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    doc_id INTEGER NOT NULL REFERENCES contract_docs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    clause_no TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_contract_clauses ON contract_clauses(project_id, doc_id);
"""

# 行首的条款号：Sub-Clause 20.2 / Clause 4.20 / 20.2.1 / 第12条 / 第 12.3 条
_HEAD = re.compile(r"^\s*(?:(?:Sub-?Clause|Clause|Article)\s+(\d+(?:\.\d+){0,3})|第\s*(\d+(?:\.\d+){0,3})\s*条|(\d+(?:\.\d+){1,3}))[\s.:：、]*(.{0,80})$", re.I)


class KbError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def text_of_upload(name: str, raw: bytes) -> str:
    if name.lower().endswith(".docx"):
        from docx import Document
        d = Document(io.BytesIO(raw))
        return "\n".join(p.text for p in d.paragraphs)
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise KbError(400, "文件编码无法识别，请另存为 UTF-8 文本或 .docx")


def split_clauses(text: str) -> list[dict]:
    """按行首条款号切分；一条也切不出时按空行分段（每段≤800字），条款号写“段N”。"""
    out: list[dict] = []
    cur = None
    for line in (text or "").splitlines():
        m = _HEAD.match(line)
        if m and len(line.strip()) <= 120:
            no = m.group(1) or m.group(2) or m.group(3)
            cur = {"clause_no": no, "title": (m.group(4) or "").strip(), "lines": []}
            out.append(cur)
        elif cur is not None:
            if line.strip():
                cur["lines"].append(line.strip())
    if out:
        return [{"clause_no": c["clause_no"], "title": c["title"][:120], "text": "\n".join(c["lines"])[:6000] or c["title"]} for c in out]
    paras, buf = [], ""
    for p in re.split(r"\n\s*\n", text or ""):
        p = p.strip()
        if not p:
            continue
        if len(buf) + len(p) > 800 and buf:
            paras.append(buf)
            buf = ""
        buf = (buf + "\n" + p).strip()
    if buf:
        paras.append(buf)
    return [{"clause_no": f"段{i + 1}", "title": p.split("\n")[0][:40], "text": p[:6000]} for i, p in enumerate(paras)]


def import_doc(conn, user: dict, title: str, kind: str, text: str) -> dict:
    if user["role"] not in IMPORTERS:
        raise KbError(403, "条款库只由工程主管导入")
    title, text = (title or "").strip()[:80], (text or "").strip()
    if not title or not text:
        raise KbError(400, "请填写文件名称并粘贴或上传条款文字")
    if len(text) > MAX_CHARS:
        raise KbError(400, f"文字超过 {MAX_CHARS // 10000} 万字，请分册导入")
    cs = split_clauses(text)
    did = insert(conn, "contract_docs", {"project_id": user["project_id"], "title": title, "kind": kind if kind in KINDS else "other", "clauses": len(cs),
                                         "chars": len(text), "created_by": user["id"], "created_at": now_iso()})
    for i, c in enumerate(cs):
        insert(conn, "contract_clauses", {"project_id": user["project_id"], "doc_id": did, "seq": i, **c})
    audit(conn, user["id"], "contract.import", f"contract_doc:{did}", f"{title} {len(cs)}条")
    return {"id": did, "title": title, "clauses": len(cs)}


def delete_doc(conn, user: dict, did: int) -> dict:
    if user["role"] not in IMPORTERS:
        raise KbError(403, "条款库只由工程主管维护")
    d = one(conn, "SELECT * FROM contract_docs WHERE id=? AND project_id=?", (did, user["project_id"]))
    if not d:
        raise KbError(404, "文件不存在")
    conn.execute("DELETE FROM contract_clauses WHERE doc_id=?", (did,))
    conn.execute("DELETE FROM contract_docs WHERE id=?", (did,))
    audit(conn, user["id"], "contract.delete", f"contract_doc:{did}", d["title"])
    return {"ok": True}


def docs(conn, pid: int) -> list[dict]:
    return all_(conn, "SELECT id, title, kind, clauses, chars, created_at FROM contract_docs WHERE project_id=? ORDER BY id DESC", (pid,))


def _terms(q: str) -> tuple[list[str], list[str]]:
    nums = re.findall(r"\d+(?:\.\d+)+|\b\d{1,3}\b", q)
    en = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", q) if w.lower() not in ("the", "and", "for", "what", "which", "clause", "sub")]
    zh = []
    for seg in re.findall(r"[一-龥]+", q):
        seg = re.sub(r"什么|多少|哪些|哪条|怎么|如何|规定|合同|条款|规范|要求|是否|可以|需要|我们|项目", " ", seg)
        for s in seg.split():
            zh += [s[i:i + 2] for i in range(max(1, len(s) - 1))] if len(s) >= 2 else []
    return nums, list(dict.fromkeys(en + zh))


def search(conn, pid: int, q: str, limit: int = 6) -> list[dict]:
    nums, words = _terms(q or "")
    if not nums and not words:
        return []
    rows = all_(conn, """SELECT c.*, d.title doc_title, d.kind FROM contract_clauses c JOIN contract_docs d ON d.id=c.doc_id WHERE c.project_id=?""", (pid,))
    scored = []
    for r in rows:
        head, body = (r["title"] or "").lower(), (r["text"] or "").lower()
        s = 10 * sum(1 for n in nums if r["clause_no"] == n) + 3 * sum(1 for n in nums if r["clause_no"].startswith(n + "."))
        s += sum(3 for w in words if w in head) + sum(1 for w in words if w in body)
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: (-x[0], x[1]["doc_id"], x[1]["seq"]))
    return [{"id": r["id"], "doc_id": r["doc_id"], "doc_title": r["doc_title"], "kind_name": KINDS.get(r["kind"], "其他"), "clause_no": r["clause_no"],
             "title": r["title"], "text": r["text"], "score": s} for s, r in scored[:limit]]


def qa_facts(conn, user: dict, words: list[str], question: str = "") -> list[str]:
    """问工笔的检索结果：命中条款原文（截 500 字），编号 [C条款库编号]。"""
    hits = search(conn, user["project_id"], question or " ".join(words))
    return [f"[C{h['id']}] 《{h['doc_title']}》第 {h['clause_no']} 条款{(' ' + h['title']) if h['title'] else ''}：{h['text'][:500]}" for h in hits]


def has_library(conn, pid: int) -> bool:
    return bool(one(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='contract_docs'")) and bool(one(conn, "SELECT 1 x FROM contract_docs WHERE project_id=? LIMIT 1", (pid,)))
