"""仿真撰写：补齐演示库时，系统里需要“AI 文字”的地方改由预先撰写的仿真文字填入，不调用任何外部模型（RJ 2026-10-11）。

install() 把 app 内所有模块引用的 call_json 换成本地函数：
- 先取调用方自带的规则版/离线结果（mock()）作为结构底稿，再按 kind 交给 WRITERS 里的撰写函数改写成正式文字；
- 撰写函数只使用事实底稿里已有的数字（照样过系统的数字守卫）；
- ai_runs 记 provider='sim'、model='sim-authored'，可据此识别这些文字是仿真撰写，不是模型输出；
- 网络调用（_call、语音转写）一律拦截，保证不出网。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

PROVIDER = "sim"
MODEL = "sim-authored"
WRITERS: dict[str, Callable[[dict, Any, dict, str], Any]] = {}   # kind -> fn(facts, base, meta, user_text) -> data
DUMP: list[dict] | None = None


def writer(*kinds):
    def deco(fn):
        for k in kinds:
            WRITERS[k] = fn
        return fn
    return deco


def _facts_of(mock) -> dict:
    """从 mock 闭包里取事实底稿（gongbi 的 lambda: reg.rule(kind, facts) 等）。取不到返回 {}。"""
    for cell in (getattr(mock, "__closure__", None) or ()):
        try:
            v = cell.cell_contents
        except ValueError:
            continue
        if isinstance(v, dict) and ("project" in v or "period_start" in v or "event" in v):
            return v
    return {}


def closure_dicts(fn) -> list[dict]:
    out = []
    for cell in (getattr(fn, "__closure__", None) or ()):
        try:
            v = cell.cell_contents
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def _strip(o):
    """系统离线结果里的“（离线模拟）”前缀去掉：这些位置的文字由仿真撰写接管。"""
    if isinstance(o, str):
        return o.replace("（离线模拟）", "")
    if isinstance(o, list):
        return [_strip(x) for x in o]
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items()}
    return o


def install():
    import importlib
    import app.main  # noqa: F401  载入全部路由与服务
    from app.ai import client
    from app.db import connect, dumps, insert, now_iso

    def fake_call_json(kind, *, system, user, prompt_version, images=None, mock, validate=None, fallback=None,
                       project_id=None, user_id=None, input_meta=None, max_tokens=3000, temperature=0.2):
        t0 = time.monotonic()
        base = mock()
        facts = _facts_of(mock)
        meta = dict(input_meta or {})
        fn = WRITERS.get(kind)
        data = base
        if fn:
            try:
                meta["_closure"] = closure_dicts(mock)
                data = fn(facts, base, meta, user)
            except Exception as e:  # noqa: BLE001  撰写失败就用底稿，并留痕
                meta["author_error"] = f"{type(e).__name__}: {e}"[:200]
                data = base
        data = _strip(data)
        if validate:
            data = validate(data)
        meta.pop("_closure", None)
        meta.update({"authored": bool(fn), "image_count": len(images or [])})
        if DUMP is not None:
            DUMP.append({"kind": kind, "meta": meta, "user": user[:6000], "base": base, "data": data})
        conn = connect()
        try:
            rid = insert(conn, "ai_runs", {
                "project_id": project_id, "kind": kind, "provider": PROVIDER, "model": MODEL, "prompt_version": prompt_version,
                "input_meta_json": dumps(meta), "output_text": json.dumps(data, ensure_ascii=False)[:60000], "ok": 1,
                "latency_ms": int((time.monotonic() - t0) * 1000), "tokens_json": "{}", "error": None, "created_by": user_id, "created_at": now_iso()})
        finally:
            conn.close()
        return client.AIResult(data=data, run_id=rid, provider=PROVIDER, model=MODEL, latency_ms=0, repaired=False, degraded="", events=None)

    def no_net(*a, **k):
        raise client.AIError("演示库补齐不调用外部模型", "other")

    client._call = no_net
    client.transcribe = no_net
    client.call_json = fake_call_json
    for name, mod in list(sys.modules.items()):
        if name.startswith("app") and getattr(mod, "call_json", None) is not None and mod is not client:
            mod.call_json = fake_call_json
        if name.startswith("app") and getattr(mod, "transcribe", None) is not None and mod is not client:
            mod.transcribe = no_net
    from tools.showcase import texts  # noqa: F401  注册撰写函数
    return fake_call_json


def save_dump(path: Path):
    if DUMP is not None:
        path.write_text(json.dumps(DUMP, ensure_ascii=False, indent=1, default=str), "utf-8")
