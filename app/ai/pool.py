"""交融 Key 池、分线路模型与额度。

- 线路（lane）：按用途把调用分成 vision/core/agent/asr/admin，每条线路可单独指定模型；
- Key：每个 Key 可设“主责线路”、每日/累计 token 与每日调用额度；选 Key 时先主责线路、再按当日用量占比均衡，
  额度用尽、被拒（401）或限流（429）自动换下一个；
- 密钥明文只存数据目录下的 ai_keys.json（不进 Git、不写库、不回显）；库里只有指纹、尾号、额度与用量；
- 模型与额度配置存 ai_settings，管理员在“模型与额度”页修改，立即生效，无需重启。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date

from ..config import settings
from ..db import all_, connect, dumps, insert, loads, now_iso, one

LANES = {
    "vision": ("照片识别", "安全/质量照片识别、带照片的日志整理（最耗额度）"),
    "core": ("日志与进度", "日志整理、进度判定、报表、预警解读、产值与质控"),
    "agent": ("圈子·催办·问答", "现场圈评论与荣誉、催办计划、例会行动项、问工笔、索赔包、放行清单、隐蔽验收成册"),
    "asr": ("语音转写", "语音转文字"),
    "admin": ("连通测试", "管理员连通测试与自检"),
}
# 推荐分配：按 Key 个数把线路分给各 Key；多出的 Key 轮流加到最忙的线路上
_PLANS = {
    1: [["vision", "core", "agent", "asr", "admin"]],
    2: [["vision", "core"], ["agent", "asr", "admin"]],
    3: [["vision"], ["core"], ["agent", "asr", "admin"]],
    4: [["vision"], ["core"], ["agent"], ["asr", "admin"]],
}
_BUSY_ORDER = ["vision", "core", "agent"]

_lock = threading.Lock()
_cool: dict[int, float] = {}  # key_id -> 冷却到的 monotonic 时间（仅进程内）
_cfg_cache: dict = {"at": 0.0, "val": None}
_secret_cache: dict = {"mtime": None, "val": {}}


@dataclass
class PoolKey:
    id: int
    label: str
    secret: str
    fp: str


def lane_of(kind: str, has_images: bool = False) -> str:
    if kind == "asr":
        return "asr"
    if kind.startswith(("ai.test", "ai.check")):
        return "admin"
    if has_images:
        return "vision"
    if kind.startswith(("feed.", "nudge.", "cmd.", "qa.", "claim.", "site.", "gate.", "conceal.")):
        return "agent"
    return "core"


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def mask(secret: str) -> str:
    return f"sk-…{secret[-4:]}" if secret else ""


def plan_lanes(n: int, index: int) -> list[str]:
    """n 个 Key 时第 index 个（从 0 起）的推荐主责线路。"""
    if n <= 4:
        return list(_PLANS[max(n, 1)][index])
    if index < 4:
        return list(_PLANS[4][index])
    return [_BUSY_ORDER[(index - 4) % len(_BUSY_ORDER)]]  # 额外的 Key 分担最忙的线路


# ----------------------------------------------------------------------------- 密钥文件

def _secret_path():
    return settings.data_dir / "ai_keys.json"


def _read_secrets() -> dict[str, str]:
    p = _secret_path()
    try:
        m = p.stat().st_mtime
    except OSError:
        return {}
    if _secret_cache["mtime"] != m:
        try:
            _secret_cache["val"] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _secret_cache["val"] = {}
        _secret_cache["mtime"] = m
    return _secret_cache["val"]


def _write_secrets(data: dict[str, str]) -> None:
    p = _secret_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)
    _secret_cache["mtime"] = None


def secret_of(fp: str) -> str:
    return _read_secrets().get(fp, "")


# ----------------------------------------------------------------------------- 配置

def _defaults() -> dict:
    vision = settings.ai_vision_model
    text = settings.ai_text_model
    return {
        "model_vision": vision, "model_core": text, "model_agent": text, "model_asr": settings.ai_asr_model, "model_admin": text,
        "max_images": settings.ai_max_images, "timeout": settings.ai_timeout, "user_daily_calls": 0, "paused": 0,
    }


def get_config(fresh: bool = False) -> dict:
    now = time.monotonic()
    if not fresh and _cfg_cache["val"] is not None and now - _cfg_cache["at"] < 3:
        return _cfg_cache["val"]
    cfg = _defaults()
    try:
        conn = connect()
        try:
            for r in all_(conn, "SELECT k, v FROM ai_settings"):
                if r["k"] in cfg:
                    cfg[r["k"]] = loads(r["v"], cfg[r["k"]])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001  表尚未建立时退回默认
        pass
    _cfg_cache.update(at=now, val=cfg)
    return cfg


def set_config(conn, values: dict) -> None:
    allowed = set(_defaults())
    for k, v in values.items():
        if k in allowed:
            conn.execute("INSERT INTO ai_settings(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, dumps(v)))
    _cfg_cache["val"] = None


def model_for(lane: str) -> str:
    cfg = get_config()
    return str(cfg.get(f"model_{lane}") or cfg["model_core"] or "")


def max_images() -> int:
    return int(get_config()["max_images"])


def timeout() -> float:
    return float(get_config()["timeout"])


def paused() -> bool:
    return bool(get_config()["paused"])


# ----------------------------------------------------------------------------- 用量

def today() -> str:
    return date.today().isoformat()


def tokens_of(usage: dict | None) -> int:
    if not isinstance(usage, dict):
        return 0
    if any(k in usage for k in ("first", "regen", "repair")):
        return sum(tokens_of(usage.get(k)) for k in ("first", "regen", "repair"))
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)):
        return int(total)
    return int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)


def record(key: PoolKey | None, lane: str, ok: bool, tokens: int = 0) -> None:
    if key is None:
        return
    try:
        conn = connect()
        try:
            conn.execute("""INSERT INTO ai_usage(key_id, day, lane, calls, fails, tokens) VALUES (?, ?, ?, 1, ?, ?)
                            ON CONFLICT(key_id, day, lane) DO UPDATE SET calls=calls+1, fails=fails+excluded.fails, tokens=tokens+excluded.tokens""",
                         (key.id, today(), lane, 0 if ok else 1, int(tokens)))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001  记账失败不影响业务调用
        pass


def usage_map(conn) -> dict[int, dict]:
    """{key_id: {today:{calls,fails,tokens}, total:{...}}}"""
    out: dict[int, dict] = {}
    day = today()
    for r in all_(conn, "SELECT key_id, day, SUM(calls) calls, SUM(fails) fails, SUM(tokens) tokens FROM ai_usage GROUP BY key_id, day"):
        u = out.setdefault(r["key_id"], {"today": {"calls": 0, "fails": 0, "tokens": 0}, "total": {"calls": 0, "fails": 0, "tokens": 0}})
        for k in ("calls", "fails", "tokens"):
            u["total"][k] += r[k] or 0
            if r["day"] == day:
                u["today"][k] += r[k] or 0
    return out


def user_calls_today(conn, user_id: int) -> int:
    row = one(conn, "SELECT COUNT(*) n FROM ai_runs WHERE created_by=? AND provider<>'mock' AND created_at>=?", (user_id, today()))
    return int(row["n"]) if row else 0


# ----------------------------------------------------------------------------- 选 Key

def exhausted_reason(row: dict, usage: dict | None) -> str:
    u = usage or {"today": {"calls": 0, "tokens": 0}, "total": {"tokens": 0}}
    if row["daily_tokens"] and u["today"]["tokens"] >= row["daily_tokens"]:
        return "今日 token 额度已用完"
    if row["daily_calls"] and u["today"]["calls"] >= row["daily_calls"]:
        return "今日调用次数已用完"
    if row["total_tokens"] and u["total"]["tokens"] >= row["total_tokens"]:
        return "累计 token 额度已用完"
    return ""


def acquire(lane: str, exclude: tuple[int, ...] = ()) -> PoolKey | None:
    """挑选一个可用 Key：主责线路优先，其次按当日用量占比均衡。没有任何 Key 记录时退回环境变量里的单 Key。"""
    conn = connect()
    try:
        rows = all_(conn, "SELECT * FROM ai_keys ORDER BY id")
        usage = usage_map(conn)
    finally:
        conn.close()
    if not rows:
        if settings.ai_api_key and -1 not in exclude:
            return PoolKey(-1, "环境变量", settings.ai_api_key, fingerprint(settings.ai_api_key))
        return None
    now = time.monotonic()
    cands = []
    for r in rows:
        if r["id"] in exclude or not r["enabled"] or r["status"] == "invalid":
            continue
        if _cool.get(r["id"], 0) > now:
            continue
        secret = secret_of(r["fp"])
        if not secret or exhausted_reason(r, usage.get(r["id"])):
            continue
        u = usage.get(r["id"], {"today": {"calls": 0, "tokens": 0}})
        ratio = (u["today"]["tokens"] / r["daily_tokens"]) if r["daily_tokens"] else (u["today"]["calls"] / 200.0)
        mine = lane in loads(r["lanes_json"], [])
        cands.append((0 if mine else 1, ratio, r["id"], PoolKey(r["id"], r["label"], secret, r["fp"])))
    if not cands:
        return None
    cands.sort(key=lambda c: c[:3])
    return cands[0][3]


def cooldown_wait(lane: str) -> float | None:
    """可用 Key 全都只是在冷却（限流）时，返回最近一把恢复所需的秒数；已有不在冷却的可用 Key 返回 0；没有任何可用 Key 返回 None。
    只用于“稍等再试”的判断，不改变选 Key 的规则。"""
    conn = connect()
    try:
        rows = all_(conn, "SELECT * FROM ai_keys ORDER BY id")
        usage = usage_map(conn)
    finally:
        conn.close()
    now = time.monotonic()
    if not rows:  # 只有环境变量里的单 Key
        return max(0.0, _cool.get(-1, 0) - now) if settings.ai_api_key else None
    waits = []
    for r in rows:
        if not r["enabled"] or r["status"] == "invalid" or not secret_of(r["fp"]) or exhausted_reason(r, usage.get(r["id"])):
            continue
        waits.append(max(0.0, _cool.get(r["id"], 0) - now))
    return min(waits) if waits else None


def cool(key: PoolKey, seconds: float) -> None:
    with _lock:
        _cool[key.id] = time.monotonic() + seconds


def mark_status(key: PoolKey, status: str, note: str = "") -> None:
    if key.id < 0:
        return
    conn = connect()
    try:
        conn.execute("UPDATE ai_keys SET status=?, status_note=?, checked_at=? WHERE id=?", (status, note[:120], now_iso(), key.id))
    finally:
        conn.close()


def unavailable_message(lane: str) -> str:
    conn = connect()
    try:
        rows = all_(conn, "SELECT * FROM ai_keys ORDER BY id")
        usage = usage_map(conn)
    finally:
        conn.close()
    if not rows:
        return "未配置交融 API Key，无法调用模型"
    if all(not r["enabled"] for r in rows):
        return "所有 Key 均已停用，请管理员在“模型与额度”中启用"
    bad = [r for r in rows if r["enabled"] and r["status"] == "invalid"]
    spent = [r for r in rows if r["enabled"] and r["status"] != "invalid" and exhausted_reason(r, usage.get(r["id"]))]
    if spent and len(spent) + len(bad) >= sum(1 for r in rows if r["enabled"]):
        return "各 Key 的 AI 额度已用完，请管理员在“模型与额度”中调整额度或添加 Key"
    return "暂无可用的交融 Key（被限流或失效），请稍后重试或联系管理员"


# ----------------------------------------------------------------------------- 增删与导入

def add_key(conn, secret: str, label: str = "", lanes: list[str] | None = None, **quota) -> int:
    secret = secret.strip()
    fp = fingerprint(secret)
    if one(conn, "SELECT 1 FROM ai_keys WHERE fp=?", (fp,)):
        raise ValueError("这个 Key 已在池中")
    secrets_ = dict(_read_secrets())
    secrets_[fp] = secret
    _write_secrets(secrets_)
    n = one(conn, "SELECT COUNT(*) n FROM ai_keys")["n"]
    return insert(conn, "ai_keys", {
        "label": label.strip() or f"交融Key{n + 1}", "fp": fp, "tail": secret[-4:], "enabled": 1,
        "lanes_json": dumps([l for l in (lanes or []) if l in LANES]),
        "daily_tokens": int(quota.get("daily_tokens") or 0), "total_tokens": int(quota.get("total_tokens") or 0),
        "daily_calls": int(quota.get("daily_calls") or 0), "status": "unknown", "status_note": "", "note": quota.get("note", ""),
        "created_at": now_iso(),
    })


def delete_key(conn, key_id: int) -> None:
    row = one(conn, "SELECT fp FROM ai_keys WHERE id=?", (key_id,))
    if not row:
        return
    conn.execute("DELETE FROM ai_keys WHERE id=?", (key_id,))
    conn.execute("DELETE FROM ai_usage WHERE key_id=?", (key_id,))
    secrets_ = dict(_read_secrets())
    if secrets_.pop(row["fp"], None) is not None:
        _write_secrets(secrets_)
    _cool.pop(key_id, None)


def rebalance(conn) -> None:
    ids = [r["id"] for r in all_(conn, "SELECT id FROM ai_keys WHERE enabled=1 ORDER BY id")]
    for i, kid in enumerate(ids):
        conn.execute("UPDATE ai_keys SET lanes_json=? WHERE id=?", (dumps(plan_lanes(len(ids), i)), kid))


def seed_from_env(conn) -> None:
    """首次启动把 .env 中的 Key 导入池；已导入或已被管理员删除的不再重复导入。"""
    raw = [os.environ.get("ZB_AI_API_KEY", ""), *os.environ.get("ZB_AI_API_KEYS", "").split(","), os.environ.get("ZHIMOU_AI_API_KEY", "")]
    keys, seen = [], set()
    for k in (x.strip() for x in raw):
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    row = one(conn, "SELECT v FROM ai_settings WHERE k='seeded_fps'")
    done = set(loads(row["v"], []) if row else [])
    fresh = [k for k in keys if fingerprint(k) not in done]
    if not fresh:
        return
    existing = one(conn, "SELECT COUNT(*) n FROM ai_keys")["n"]
    total = existing + len(fresh)
    for i, k in enumerate(fresh):
        try:
            add_key(conn, k, lanes=plan_lanes(total, i) if existing == 0 else [])
        except ValueError:
            pass
        done.add(fingerprint(k))
    conn.execute("INSERT INTO ai_settings(k, v) VALUES ('seeded_fps', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (dumps(sorted(done)),))
