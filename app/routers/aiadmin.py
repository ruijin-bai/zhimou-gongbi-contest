"""系统管理员的“模型与额度”后台：Key 池、分线路模型、额度与用量。

密钥只能新增/删除/测试，永不回显（只显示尾号）；AI 地址与境内白名单不可在界面修改。
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_keys (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    fp TEXT NOT NULL UNIQUE,
    tail TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    lanes_json TEXT NOT NULL DEFAULT '[]',
    daily_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    daily_calls INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN ('unknown','ok','invalid')),
    status_note TEXT NOT NULL DEFAULT '',
    checked_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_usage (
    key_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    lane TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    fails INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, day, lane)
);
CREATE INDEX IF NOT EXISTS ix_ai_runs_user_day ON ai_runs(created_by, created_at);
"""


def migrate(conn) -> None:
    from ..ai import pool
    pool.seed_from_env(conn)


class ConfigIn(BaseModel):
    model_vision: str = Field("", max_length=80)
    model_core: str = Field("", max_length=80)
    model_agent: str = Field("", max_length=80)
    model_asr: str = Field("", max_length=80)
    model_admin: str = Field("", max_length=80)
    max_images: int = Field(8, ge=1, le=20)
    timeout: float = Field(180, ge=20, le=600)
    user_daily_calls: int = Field(0, ge=0, le=100000)
    paused: bool = False


class KeyIn(BaseModel):
    key: str = Field(min_length=8, max_length=200)
    label: str = Field("", max_length=30)
    lanes: list[str] = []
    daily_tokens: int = Field(0, ge=0)
    total_tokens: int = Field(0, ge=0)
    daily_calls: int = Field(0, ge=0)
    note: str = Field("", max_length=100)


class KeyPatch(BaseModel):
    label: str | None = Field(None, max_length=30)
    enabled: bool | None = None
    lanes: list[str] | None = None
    daily_tokens: int | None = Field(None, ge=0)
    total_tokens: int | None = Field(None, ge=0)
    daily_calls: int | None = Field(None, ge=0)
    note: str | None = Field(None, max_length=100)


class LaneIn(BaseModel):
    lane: str


def _tokens_from_json(raw: str | None) -> int:
    from ..ai import pool
    from ..db import loads
    return pool.tokens_of(loads(raw, {}))


def register(app) -> None:
    from .. import main as M
    from ..ai import client, pool
    from ..config import settings
    from ..db import all_, audit, dumps, loads, now_iso, one

    def key_row(conn, kid: int) -> dict:
        row = one(conn, "SELECT * FROM ai_keys WHERE id=?", (kid,))
        if not row:
            raise HTTPException(404, "Key 不存在")
        return row

    def view_key(row: dict, usage: dict, now: float) -> dict:
        u = usage.get(row["id"], {"today": {"calls": 0, "fails": 0, "tokens": 0}, "total": {"calls": 0, "fails": 0, "tokens": 0}})
        spent = pool.exhausted_reason(row, u)
        cooling = max(0, int(pool._cool.get(row["id"], 0) - now))
        if not row["enabled"]:
            state, note = "off", "已停用"
        elif row["status"] == "invalid":
            state, note = "bad", row["status_note"] or "Key 无效"
        elif spent:
            state, note = "spent", spent
        elif cooling:
            state, note = "cool", f"被限流，{cooling}s 后恢复"
        else:
            state, note = "ok", row["status_note"] if row["status"] == "ok" else "未测试"
        return {"id": row["id"], "label": row["label"], "mask": f"sk-…{row['tail']}", "enabled": bool(row["enabled"]),
                "lanes": loads(row["lanes_json"], []), "daily_tokens": row["daily_tokens"], "total_tokens": row["total_tokens"],
                "daily_calls": row["daily_calls"], "status": row["status"], "state": state, "state_note": note,
                "checked_at": row["checked_at"], "note": row["note"], "usage": u,
                "has_secret": bool(pool.secret_of(row["fp"]))}

    @app.get("/api/admin/ai")
    def ai_overview(user=Depends(M.admin), conn=Depends(M.db)):
        cfg = pool.get_config(fresh=True)
        usage = pool.usage_map(conn)
        now = time.monotonic()
        keys = [view_key(r, usage, now) for r in all_(conn, "SELECT * FROM ai_keys ORDER BY id")]
        tot = {"today": {"calls": 0, "fails": 0, "tokens": 0}, "total": {"calls": 0, "fails": 0, "tokens": 0}}
        for k in keys:
            for w in ("today", "total"):
                for f in ("calls", "fails", "tokens"):
                    tot[w][f] += k["usage"][w][f]
        lanes = [{"key": k, "name": v[0], "desc": v[1], "model": cfg.get(f"model_{k}", ""),
                  "keys": [x["label"] for x in keys if k in x["lanes"] and x["enabled"]]} for k, v in pool.LANES.items()]
        since = (date.today() - timedelta(days=13)).isoformat()
        series = {r["day"]: r for r in all_(conn, "SELECT day, SUM(calls) calls, SUM(fails) fails, SUM(tokens) tokens FROM ai_usage WHERE day>=? GROUP BY day", (since,))}
        days = [(date.today() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
        by_day = [{"day": d, "calls": (series.get(d) or {}).get("calls", 0) or 0, "tokens": (series.get(d) or {}).get("tokens", 0) or 0,
                   "fails": (series.get(d) or {}).get("fails", 0) or 0} for d in days]
        week = (date.today() - timedelta(days=6)).isoformat()
        kinds: dict[str, dict] = {}
        for r in all_(conn, "SELECT kind, ok, tokens_json FROM ai_runs WHERE provider<>'mock' AND created_at>=? ORDER BY id DESC LIMIT 20000", (week,)):
            k = kinds.setdefault(r["kind"], {"kind": r["kind"], "lane": pool.lane_of(r["kind"]), "calls": 0, "fails": 0, "tokens": 0})
            k["calls"] += 1
            k["fails"] += 0 if r["ok"] else 1
            k["tokens"] += _tokens_from_json(r["tokens_json"])
        by_kind = sorted(kinds.values(), key=lambda x: (-x["tokens"], -x["calls"]))[:12]
        return {"provider": settings.ai_provider, "base_url": settings.ai_base_url, "host_ok": settings.ai_host_allowed(),
                "config": cfg, "lanes": lanes, "keys": keys, "totals": tot, "by_day": by_day, "by_kind": by_kind,
                "user_calls_today": (one(conn, "SELECT COUNT(DISTINCT created_by) n FROM ai_runs WHERE provider<>'mock' AND created_at>=?", (pool.today(),)) or {}).get("n", 0)}

    @app.put("/api/admin/ai/config")
    def ai_config(body: ConfigIn, user=Depends(M.admin), conn=Depends(M.db)):
        values = body.model_dump()
        values["paused"] = 1 if body.paused else 0
        for k in ("model_vision", "model_core", "model_agent", "model_admin"):
            values[k] = values[k].strip()
            if not values[k]:
                raise HTTPException(400, "图像、日志、协同和测试线路都必须选择模型")
        values["model_asr"] = values["model_asr"].strip()
        for k in [k for k in values if k.startswith("model_")]:
            if not re.fullmatch(r"[\w.\-:/]*", values[k]):
                raise HTTPException(400, "模型名只能包含字母、数字和 . - _ : /")
        pool.set_config(conn, values)
        audit(conn, user["id"], "ai.config", "ai", dumps({k: v for k, v in values.items()}))
        return {"ok": True}

    @app.get("/api/admin/ai/models")
    def ai_models(user=Depends(M.admin)):
        """从交融实时拉取可用模型清单，供下拉选择。"""
        if settings.ai_provider == "mock":
            return {"models": [], "error": "当前为离线模拟模式"}
        key = pool.acquire("admin")
        if key is None:
            return {"models": [], "error": pool.unavailable_message("admin")}
        try:
            r = httpx.get(f"{settings.ai_base_url}/models", headers={"Authorization": f"Bearer {key.secret}", "User-Agent": client._UA},
                          timeout=30, trust_env=settings.ai_trust_env, follow_redirects=False)
            r.raise_for_status()
            ids = sorted({m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id")})
        except (httpx.HTTPError, ValueError, KeyError):
            return {"models": [], "error": "拉取模型清单失败，可手工输入模型名"}
        def tag(i: str) -> str:
            low = i.lower()
            if any(w in low for w in ("embed", "rerank", "bge", "large-zh")):
                return "embed"
            if "vl" in low or low.endswith("v") or "glm4.5v" in low:
                return "vision"
            if any(w in low for w in ("asr", "whisper", "paraformer", "sense")):
                return "asr"
            return "text"
        return {"models": [{"id": i, "tag": tag(i)} for i in ids], "error": ""}

    @app.post("/api/admin/ai/keys")
    def key_add(body: KeyIn, user=Depends(M.admin), conn=Depends(M.db)):
        if not re.fullmatch(r"[\x21-\x7e]{8,200}", body.key.strip()):
            raise HTTPException(400, "Key 格式不正确（不能含空格或中文）")
        kid = pool.add_key(conn, body.key, body.label, body.lanes, daily_tokens=body.daily_tokens, total_tokens=body.total_tokens,
                           daily_calls=body.daily_calls, note=body.note)
        audit(conn, user["id"], "ai.key.add", f"key:{kid}", body.label)
        return {"ok": True, "id": kid}

    @app.put("/api/admin/ai/keys/{kid}")
    def key_patch(kid: int, body: KeyPatch, user=Depends(M.admin), conn=Depends(M.db)):
        row = key_row(conn, kid)
        sets: dict = {}
        if body.label is not None:
            sets["label"] = body.label.strip() or row["label"]
        if body.enabled is not None:
            sets["enabled"] = 1 if body.enabled else 0
            if body.enabled and row["status"] == "invalid":
                sets["status"], sets["status_note"] = "unknown", "已重新启用，待测试"
        if body.lanes is not None:
            sets["lanes_json"] = dumps([l for l in body.lanes if l in pool.LANES])
        for f in ("daily_tokens", "total_tokens", "daily_calls"):
            if getattr(body, f) is not None:
                sets[f] = getattr(body, f)
        if body.note is not None:
            sets["note"] = body.note
        if sets:
            conn.execute(f"UPDATE ai_keys SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), kid))
        pool._cool.pop(kid, None)
        audit(conn, user["id"], "ai.key.update", f"key:{kid}", dumps({k: v for k, v in sets.items()}))
        return {"ok": True}

    @app.delete("/api/admin/ai/keys/{kid}")
    def key_delete(kid: int, user=Depends(M.admin), conn=Depends(M.db)):
        row = key_row(conn, kid)
        pool.delete_key(conn, kid)
        audit(conn, user["id"], "ai.key.delete", f"key:{kid}", row["label"])
        return {"ok": True}

    @app.post("/api/admin/ai/keys/{kid}/test")
    def key_test(kid: int, user=Depends(M.admin), conn=Depends(M.db)):
        """用这把 Key 发一条极小的请求：确认是否有效、是否被限流，并更新状态。"""
        row = key_row(conn, kid)
        secret = pool.secret_of(row["fp"])
        if not secret:
            raise HTTPException(400, "该 Key 的密钥文件缺失，请删除后重新添加")
        if settings.ai_provider == "mock":
            raise HTTPException(400, "当前为离线模拟模式，无法测试真实 Key")
        if not settings.ai_host_allowed():
            raise HTTPException(400, "AI 接口地址不在境内白名单内")
        key = pool.PoolKey(row["id"], row["label"], secret, row["fp"])
        started = time.monotonic()
        try:
            r = httpx.post(f"{settings.ai_base_url}/chat/completions", timeout=60, trust_env=settings.ai_trust_env, follow_redirects=False,
                           headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json", "User-Agent": client._UA},
                           json={"model": pool.model_for("admin"), "messages": [{"role": "user", "content": "回复OK"}], "max_tokens": 8, "stream": False})
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"无法连接交融：{type(exc).__name__}"}
        ms = int((time.monotonic() - started) * 1000)
        if r.status_code == 200:
            try:
                used = pool.tokens_of(r.json().get("usage"))
            except ValueError:
                used = 0
            pool.record(key, "admin", True, used)
            conn.execute("UPDATE ai_keys SET status='ok', status_note=?, checked_at=? WHERE id=?", (f"测试通过 {ms}ms", now_iso(), kid))
            pool._cool.pop(kid, None)
            audit(conn, user["id"], "ai.key.test", f"key:{kid}", "ok")
            return {"ok": True, "latency_ms": ms}
        pool.record(key, "admin", False)
        if r.status_code == 401:
            conn.execute("UPDATE ai_keys SET status='invalid', status_note='HTTP 401 Key 无效或已被回收', checked_at=? WHERE id=?", (now_iso(), kid))
        else:
            conn.execute("UPDATE ai_keys SET status_note=?, checked_at=? WHERE id=?", (f"测试返回 HTTP {r.status_code}", now_iso(), kid))
        audit(conn, user["id"], "ai.key.test", f"key:{kid}", f"HTTP {r.status_code}")
        return {"ok": False, "error": f"交融返回 HTTP {r.status_code}"}

    @app.post("/api/admin/ai/rebalance")
    def ai_rebalance(user=Depends(M.admin), conn=Depends(M.db)):
        """按启用的 Key 个数重新做推荐分配（图像 / 日志进度 / 协同 / 语音与测试）。"""
        pool.rebalance(conn)
        audit(conn, user["id"], "ai.rebalance", "ai")
        return {"ok": True}

    @app.post("/api/admin/ai/lane-test")
    def lane_test(body: LaneIn, user=Depends(M.admin)):
        """用该线路当前的模型和 Key 调度做一次真实调用。"""
        if body.lane not in pool.LANES or body.lane == "asr":
            raise HTTPException(400, "该线路不支持文字测试")
        if settings.ai_provider == "mock":
            return {"ok": True, "mock": True, "model": "mock", "latency_ms": 0}
        started = time.monotonic()
        try:
            text, usage = client._post(pool.model_for(body.lane), [{"role": "user", "content": "回复OK"}], 8, 0, body.lane)
        except client.AIError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "model": pool.model_for(body.lane), "key": usage.get("_key", ""), "latency_ms": int((time.monotonic() - started) * 1000)}
