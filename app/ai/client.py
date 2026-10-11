"""交融大模型调用层（OpenAI 兼容 /chat/completions）。

- 内部数据只发往白名单主机（默认仅交融 c4ai.ccccltd.cn），境外接口在代码层拒绝；
- 每次调用写 ai_runs：模型、提示词版本、输入摘要（不含图片与原文）、耗时、成败；
- 可靠性（均有上限，且遵守 Key 池换 Key 与“紧急暂停”）：网络错误/超时/5xx 指数退避重试；429 换 Key，无 Key 可换时稍等后再试；
  输出被截断或为空 → 同一请求放大输出上限重答一次；输出不合格 → 带原始要求请模型修正一次；仍失败则按调用方给的降级函数处理，
  无降级则明确报错，绝不伪造结果。重试/修复/降级都写入 ai_runs 的 input_meta（events、repaired、regen、degraded、http_calls）；
- mock 仅用于离线开发与自动测试，界面会显著标注，记录 provider=mock。
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from ..config import settings
from ..db import connect, dumps, insert, now_iso
from . import pool

log = logging.getLogger(__name__)

# 重试上限（每次 _post 调用各自计数）与退避；测试里可把 _sleep 换成空函数
MAX_NET_RETRIES = 2            # 连不上/连接超时/连接被重置等网络错误
MAX_READ_TIMEOUT_RETRIES = 1   # 读超时本身就很久（默认 180 秒），只再试一次
MAX_SERVER_RETRIES = 2         # 5xx 与响应体异常
MAX_RATE_ROUNDS = 2            # 所有 Key 都被限流时，最多等待两轮
MAX_COOL_WAIT = 20.0           # 单次等待限流冷却的上限（秒），更长的冷却直接报错而不是让用户干等
_sleep = time.sleep

# 交融安全规则会拦截无浏览器 UA 的脚本请求（交融对接文档要求）
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class AIError(RuntimeError):
    """模型不可用、被拒、超时或输出无法解析。信息可展示给用户，不含密钥与原文。

    kind：net 网络 / timeout 超时 / rate 限流 / server 5xx / quota 额度或无可用 Key / paused 已暂停 /
    bad_request 请求被拒（400/413/422，常见于图片过大）/ format 输出无法解析 / config 配置 / other。
    调用方可据此决定是否值得再试（例如 bad_request、format 值得拆小重试，net、quota 不值得马上再试）。
    """

    def __init__(self, msg: str = "", kind: str = "other", events: list[str] | None = None):
        super().__init__(msg)
        self.kind = kind
        self.events = list(events or [])


# 失败原因 → 说给人听的原因与下一步（模型故障时不用模板兜底，原因要说到位）
_CAUSE = {
    "net": ("连不上交融大模型（网络不通）", "检查网络后点重试"),
    "timeout": ("交融大模型响应超时，自动重试后仍未返回", "稍后点重试"),
    "rate": ("交融大模型限流，可用的 Key 都在冷却", "稍等几分钟再试"),
    "server": ("交融大模型服务端出错", "稍后点重试，持续出现请联系系统管理员"),
    "quota": ("AI 额度已用完或没有可用的 Key", "请系统管理员在“模型与额度”里处理"),
    "paused": ("系统管理员已暂停 AI 调用", "请联系系统管理员恢复"),
    "bad_request": ("请求被交融拒绝，常见于内容或图片过大", "缩小范围或减少图片后再试"),
    "format": ("模型返回的内容不合格，已让模型修正一次仍不合格", "点重试，反复出现请联系系统管理员"),
    "config": ("AI 接口配置不对", "请系统管理员检查配置"),
}


def explain(exc: "AIError") -> str:
    """AIError → 给用户看的完整说明：模型有问题、这次没生成内容（不用模板代替）、原因、下一步、原始错误。"""
    cause, advice = _CAUSE.get(getattr(exc, "kind", "other"), ("模型调用出错", "稍后点重试"))
    detail = str(exc).strip()
    extra = f"（{detail}）" if detail and detail not in cause and cause not in detail else ""
    return f"AI 调用失败：模型有问题，这次没有生成内容，也不会用模板代替。原因：{cause}{extra}。{advice}。"


@dataclass
class AIResult:
    data: Any
    run_id: int
    provider: str
    model: str
    latency_ms: int
    repaired: bool = False   # 经过“请按格式重答”的修复才得到
    degraded: str = ""       # 非空表示走了调用方的降级路径（内容不是严格按约定结构产出）
    events: list[str] | None = None


def provider_status() -> dict:
    if settings.ai_provider == "mock":
        return {"provider": "mock", "ready": True, "label": "离线模拟模型（仅开发测试，不可用于正式成果）",
                "vision_model": "mock", "text_model": "mock", "asr": True}
    conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) n FROM ai_keys").fetchone()["n"]
        usable = conn.execute("SELECT COUNT(*) n FROM ai_keys WHERE enabled=1 AND status<>'invalid'").fetchone()["n"]
    except Exception:  # noqa: BLE001
        total = usable = 0
    finally:
        conn.close()
    has_key = usable > 0 if total else bool(settings.ai_api_key)
    ready = has_key and settings.ai_host_allowed() and not pool.paused()
    reason = ""
    if pool.paused():
        reason = "管理员已暂停 AI 调用"
    elif not has_key:
        reason = "没有可用的交融 API Key" if total else "未配置交融 API Key（ZB_AI_API_KEY）"
    elif not settings.ai_host_allowed():
        reason = "AI 地址不在境内白名单，已拒绝"
    return {"provider": "jiaorong", "ready": ready, "reason": reason, "label": "中国交建交融大模型",
            "vision_model": pool.model_for("vision"), "text_model": pool.model_for("core"), "asr": bool(pool.model_for("asr")),
            "keys": {"total": total, "usable": usable}}


_PY_LITERALS = {"True": "true", "False": "false", "None": "null"}


def _lenient_fix(text: str) -> str:
    """修常见的“几乎合法”JSON：对象/数组末尾多余的逗号、Python 风格的 True/False/None。只改字符串之外的部分。"""
    out: list[str] = []
    i, n, in_str, esc = 0, len(text), False, False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch == ",":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        elif ch in "TFN":
            lit = next((k for k in _PY_LITERALS if text.startswith(k, i)), None)
            if lit and not (i + len(lit) < n and (text[i + len(lit)].isalnum() or text[i + len(lit)] == "_")) and not (out and (out[-1].isalnum() or out[-1] == "_")):
                out.append(_PY_LITERALS[lit])
                i += len(lit)
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = _lenient_fix(text)
        if fixed == text:
            raise
        return json.loads(fixed)


def extract_json(text: str) -> Any:
    """宽容地提取 JSON：容忍 ```json 围栏、前后说明文字、<think> 段、末尾多余逗号、Python 风格 True/False/None；
    返回第一个完整的 JSON 对象（整段本身就是数组时返回数组，由调用方的校验决定是否接受）。截断的输出报“不完整”。"""
    if text is None:
        raise ValueError("空输出")
    cleaned = re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.S).strip().lstrip("﻿")
    if not cleaned:
        raise ValueError("空输出")
    fences = [f.strip() for f in re.findall(r"```(?:json|JSON)?\s*(.*?)```", cleaned, flags=re.S)]
    for cand in [*(f for f in fences if f), cleaned]:
        try:
            return _loads(cand)
        except json.JSONDecodeError:
            pass
    body = next((f for f in fences if "{" in f), cleaned)
    start = body.find("{")
    if start < 0:
        raise ValueError("输出中没有 JSON 对象")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(body)):
        ch = body[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return _loads(body[start:i + 1])
                except json.JSONDecodeError as exc:
                    raise ValueError(f"JSON 格式错误：{exc.msg}") from None
    raise ValueError("JSON 对象不完整（输出可能被截断）")


def _image_part(path: str) -> dict:
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def _backoff(n: int) -> float:
    """第 n 次（从 0 起）重试前的等待秒数：1.5、3、6…，上限 10，带少量抖动避免并发请求同时重试。"""
    return min(10.0, 1.5 * (2 ** n)) + random.uniform(0, 0.4)


def _retry_after(resp: Any, default: float, cap: float = 60.0) -> float:
    try:
        v = float((getattr(resp, "headers", None) or {}).get("retry-after", ""))
        return max(1.0, min(cap, v))
    except (TypeError, ValueError):
        return default


def _dispatch(lane: str, send: Callable[[pool.PoolKey], Any], events: list[str], tries: list[int]) -> tuple[pool.PoolKey, Any]:
    """带重试与 Key 调度地发出一次请求，只在拿到 HTTP 200 时返回 (key, resp)。

    - 网络错误/连接超时：退避重试 MAX_NET_RETRIES 次；读超时：重试 MAX_READ_TIMEOUT_RETRIES 次；5xx：重试 MAX_SERVER_RETRIES 次；
    - 401 停用该 Key；402/403/429 冷却该 Key 并换下一个；没有 Key 可换但只是都在冷却时，稍等（≤MAX_COOL_WAIT）再试，最多 MAX_RATE_ROUNDS 轮；
    - 每次循环都先看“紧急暂停”，暂停后不再重试；
    - events 记录每次重试/换 Key/等待，tries[0] 累计实际发出的 HTTP 请求数，供 ai_runs 留痕。
    """
    def fail(msg: str, kind: str) -> AIError:
        err = AIError(msg, kind, events)
        err.tries = tries[0]
        return err

    tried: list[int] = []
    net_n = to_n = srv_n = rate_n = 0
    while True:
        if pool.paused():
            raise fail("管理员已暂停 AI 调用", "paused")
        key = pool.acquire(lane, tuple(tried))
        if key is None:
            wait = pool.cooldown_wait(lane)
            if wait is not None and wait <= MAX_COOL_WAIT and rate_n < MAX_RATE_ROUNDS:
                rate_n += 1
                events.append(f"Key 限流冷却中，等待 {wait:.0f} 秒后重试")
                _sleep(wait + 0.5)
                tried.clear()
                continue
            raise fail(pool.unavailable_message(lane), "rate" if wait is not None else "quota")
        tried.append(key.id)
        tries[0] += 1
        retried = net_n + to_n + srv_n + rate_n
        try:
            resp = send(key)
        except httpx.TimeoutException as exc:
            pool.record(key, lane, False)
            connect_phase = isinstance(exc, httpx.ConnectTimeout)
            if connect_phase and net_n < MAX_NET_RETRIES:
                net_n += 1
            elif not connect_phase and to_n < MAX_READ_TIMEOUT_RETRIES:
                to_n += 1
            else:
                suffix = f"（已重试 {retried} 次）" if retried else ""
                raise fail(f"交融模型响应超时{suffix}，请稍后重试或减少照片数量", "timeout") from None
            events.append(f"{'连接' if connect_phase else '读取'}超时，重试")
            tried.remove(key.id)
            _sleep(_backoff(net_n + to_n - 1))
            continue
        except httpx.HTTPError as exc:
            pool.record(key, lane, False)
            if net_n >= MAX_NET_RETRIES:
                suffix = f"（已重试 {retried} 次）" if retried else ""
                raise fail(f"无法连接交融模型：{type(exc).__name__}{suffix}", "net") from None
            net_n += 1
            events.append(f"网络错误 {type(exc).__name__}，重试")
            tried.remove(key.id)
            _sleep(_backoff(net_n - 1))
            continue
        code = resp.status_code
        if code == 200:
            return key, resp
        pool.record(key, lane, False)
        if code == 401:  # Key 失效：停止调度该 Key，等管理员在后台重测
            pool.mark_status(key, "invalid", "HTTP 401 Key 无效或已被回收")
            events.append("HTTP 401，Key 失效，换 Key")
            continue
        if code in (402, 403, 429):  # 额度/限流：冷却一会儿，换下一个 Key
            pool.cool(key, 300 if code == 402 else 120 if code == 403 else _retry_after(resp, 20.0))
            events.append(f"HTTP {code}，Key 冷却，换 Key")
            continue
        if code >= 500 and srv_n < MAX_SERVER_RETRIES:
            srv_n += 1
            events.append(f"HTTP {code}，重试")
            tried.remove(key.id)
            _sleep(_backoff(srv_n - 1))
            continue
        suffix = f"（已重试 {retried} 次）" if retried else ""
        kind = "server" if code >= 500 else "bad_request" if code in (400, 413, 422) else "other"
        raise fail(f"交融模型返回 HTTP {code}{suffix}：{resp.text[:200].replace(key.secret, '***')}", kind)


def _post(model: str, messages: list[dict], max_tokens: int, temperature: float, lane: str = "core") -> tuple[str, dict]:
    events: list[str] = []
    if not settings.ai_host_allowed():
        raise AIError("AI 接口地址不在境内白名单内：按公司规定禁止将内部数据发往境外接口", "config")
    if pool.paused():
        raise AIError("管理员已暂停 AI 调用", "paused")
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": False}
    # 非流式请求要等整段生成完：输出上限越大需要的时间越长，读超时随之放宽（只放宽，不收紧管理员的设置）
    read_s = max(pool.timeout(), min(300.0, 40.0 + max_tokens * 0.05))
    timeout = httpx.Timeout(read_s, connect=20.0)
    tries = [0]

    def send(key: pool.PoolKey):
        with httpx.Client(timeout=timeout, trust_env=settings.ai_trust_env, follow_redirects=False) as client:
            return client.post(
                f"{settings.ai_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key.secret}", "Content-Type": "application/json", "User-Agent": _UA},
                json=payload,
            )

    key, resp = _dispatch(lane, send, events, tries)
    try:
        body = resp.json()
        choice = body["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        pool.record(key, lane, False)
        err = AIError("交融模型响应格式异常", "server", events)
        err.tries = tries[0]
        raise err from None
    if isinstance(content, list):  # 部分兼容接口返回分段内容
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    usage = dict(body.get("usage") or {})
    pool.record(key, lane, True, pool.tokens_of(usage))
    usage["_key"] = key.label
    usage["_finish"] = choice.get("finish_reason") if isinstance(choice, dict) else None
    usage["_events"] = events
    usage["_tries"] = tries[0]
    return content or "", usage


def _user_cap_check(user_id: int | None) -> None:
    cap = int(pool.get_config().get("user_daily_calls") or 0)
    if not cap or user_id is None:
        return
    conn = connect()
    try:
        role = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if role and role["role"] == "admin":
            return
        if pool.user_calls_today(conn, user_id) >= cap:
            raise AIError(f"今日 AI 调用已达个人上限（{cap} 次），明天自动恢复，或联系管理员调整")
    finally:
        conn.close()


class _BadOutput(ValueError):
    """模型有输出，但解析或校验不通过（可重答/修复）。"""


_NONE = object()


def _parse(text: str, validate: Callable[[Any], Any] | None) -> Any:
    try:
        data = extract_json(text)
        return validate(data) if validate else data
    except Exception as exc:  # noqa: BLE001  校验函数里的任何异常都按“输出不合格”处理，交给重答/修复，而不是让调用方 500
        if isinstance(exc, ValueError):
            msg = str(exc)
        elif isinstance(exc, KeyError):
            msg = f"缺少字段 {exc}"
        else:
            msg = f"结构不符（{type(exc).__name__}）"
        raise _BadOutput(msg) from None


def _messages(system: str, user: str, images: list[str]) -> list[dict]:
    if not images:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        parts = [_image_part(p) for p in images]
    except OSError:
        raise AIError("照片文件无法读取，请重新上传", "bad_request") from None
    # 与此前已实测通过的交融多模态请求保持同一形态：单条 user 消息，文字在前、图片在后
    return [{"role": "user", "content": [{"type": "text", "text": f"{system}\n\n{user}"}] + parts}]


def _repair_messages(system: str, user: str, bad: str, err: Exception, has_images: bool) -> list[dict]:
    """修复请求：带上原始格式要求与请求（早先版本只给“不符合要求”，模型并不知道要的是什么结构），不再重发图片。"""
    note = "（原请求附有照片，这里不再提供；请只整理你上次输出里已有的内容。）" if has_images else ""
    return [
        {"role": "system", "content": "你是JSON格式修复器。只输出一个合法JSON对象，不要任何解释，不要```围栏。\n\n【原始输出格式要求】\n" + system[:6000]},
        {"role": "user", "content": f"【原始请求】{note}\n{user[:6000]}\n\n【你上次的输出】\n{bad[:10000]}\n\n"
                                    f"问题：{err}\n请只修正格式、补齐缺失的字段；保持原有内容与事实，不要新增原输出和原请求里没有的信息。输出完整JSON。"},
    ]


def _call(model: str, messages: list[dict], max_tokens: int, temperature: float, lane: str, st: dict) -> tuple[str, dict]:
    """调用 _post 并把重试事件与 HTTP 请求数累计到 st（成功或失败都记）。"""
    try:
        text, usage = _post(model, messages, max_tokens, temperature, lane)
    except AIError as exc:
        st["events"].extend(exc.events)
        st["calls"] += int(getattr(exc, "tries", 0) or 0)
        raise
    st["events"].extend(usage.pop("_events", []))
    st["calls"] += int(usage.pop("_tries", 1) or 1)
    return text, usage


# 作业称呼：人看代码是懵的。事实文字里作业写成“简称（代码）”，叙述只写简称；结构化字段（activity_code 等）仍填代码供系统核对
_ACT_REF = re.compile(r"（[A-Z]{1,5}-?\d{2,6}）")
ACT_NAME_RULE = ("【作业称呼】给人看的文字里提到 P6 作业时，只写事实中“简称（代码）”的中文简称，不写作业代码；"
                 "要求输出代码的结构化字段（如 activity_code、refs）照常填代码。")


def call_json(
    kind: str,
    *,
    system: str,
    user: str,
    prompt_version: str,
    images: list[str] | None = None,
    mock: Callable[[], Any],
    validate: Callable[[Any], Any] | None = None,
    fallback: Callable[[str], Any] | None = None,
    project_id: int | None = None,
    user_id: int | None = None,
    input_meta: dict | None = None,
    max_tokens: int = 3000,
    temperature: float = 0.2,
) -> AIResult:
    """调用模型并返回解析/校验后的 JSON。images 为已压缩的本地 JPEG 路径。

    输出不合格时的处理顺序（每步最多一次）：被截断/为空 → 同一请求放大输出上限重答；
    仍不合格 → 带原始要求请模型修正；仍不合格 → 若调用方提供 fallback（原始文本 → 结构，失败则抛 ValueError）则降级，
    否则报错。降级结果会在 AIResult.degraded 与 ai_runs.input_meta 中标明，调用方应据此向人如实说明。
    """
    images = images or []
    if _ACT_REF.search(user):   # 事实里作业写成“简称（代码）”（services/act_names.fmt）时，统一要求叙述用简称
        system = system.rstrip() + "\n" + ACT_NAME_RULE
    provider = settings.ai_provider
    lane = pool.lane_of(kind, bool(images))
    model = "mock" if provider == "mock" else pool.model_for(lane)
    meta = dict(input_meta or {})
    meta.update({"image_count": len(images), "prompt_chars": len(system) + len(user), "lane": lane})
    started = time.monotonic()
    output_text: str | None = None
    usage: dict = {}
    error: str | None = None
    err_kind = "other"
    data: Any = None
    repaired, degraded = False, ""
    st: dict = {"events": [], "calls": 0}
    try:
        if provider == "mock":
            data = mock()
            output_text = json.dumps(data, ensure_ascii=False)
            if validate:
                data = validate(data)
        else:
            _user_cap_check(user_id)
            messages = _messages(system, user, images)
            parts: dict[str, dict] = {}
            texts: list[str] = []
            text, u = _call(model, messages, max_tokens, temperature, lane, st)
            parts["first"], output_text = u, text
            texts.append(text)
            try:
                data = _parse(text, validate)
            except _BadOutput as bad:
                data, why = _NONE, bad
                if u.get("_finish") == "length" or not text.strip():
                    # 输出被截断或为空：补救“修复”救不回缺失的部分，直接放大输出上限、同一请求重答一次
                    meta["regen"] = True
                    text, u2 = _call(model, messages, min(8000, int(max_tokens * 1.7) + 200), 0.0, lane, st)
                    parts["regen"], output_text = u2, text
                    texts.append(text)
                    try:
                        data = _parse(text, validate)
                    except _BadOutput as bad2:
                        why = bad2
                if data is _NONE:
                    text, u3 = _call(pool.model_for("core"), _repair_messages(system, user, output_text or "", why, bool(images)),
                                     max_tokens, 0.0, "core" if lane == "vision" else lane, st)
                    parts["repair"], output_text = u3, text
                    texts.append(text)
                    try:
                        data = _parse(text, validate)
                        repaired = True
                    except _BadOutput as bad3:
                        why = bad3
                        if fallback is None:
                            raise AIError(f"模型输出无法解析为约定结构：{why}", "format") from None
                        for t in reversed([t for t in texts if t.strip()]):
                            try:
                                data = fallback(t)
                                degraded = f"原始输出不符约定结构（{why}），已按降级规则取用"
                                break
                            except Exception:  # noqa: BLE001
                                continue
                        if data is _NONE:
                            raise AIError(f"模型输出无法解析为约定结构：{why}", "format") from None
            usage = parts["first"] if len(parts) == 1 else parts
    except AIError as exc:
        error, err_kind = str(exc), exc.kind
    except (ValueError, TypeError, KeyError) as exc:  # 离线 mock 的校验失败
        error, err_kind = f"模型输出无法解析为约定结构：{exc}", "format"
    except Exception as exc:  # noqa: BLE001  任何未预料的异常也要留痕并以 AIError 交给调用方降级，而不是 500 且无记录
        log.exception("AI 调用内部错误：%s", kind)
        error, err_kind = f"内部错误（{type(exc).__name__}）", "other"
    latency = int((time.monotonic() - started) * 1000)
    if st["calls"]:
        meta["http_calls"] = st["calls"]
    if st["events"]:
        meta["events"] = st["events"][:12]
    if repaired:
        meta["repaired"] = True
    if degraded:
        meta["degraded"] = degraded
    conn = connect()
    try:
        run_id = insert(conn, "ai_runs", {
            "project_id": project_id, "kind": kind, "provider": provider, "model": model,
            "prompt_version": prompt_version, "input_meta_json": dumps(meta),
            "output_text": (output_text or "")[:60000], "ok": 0 if error else 1, "latency_ms": latency,
            "tokens_json": dumps(usage), "error": error, "created_by": user_id, "created_at": now_iso(),
        })
    finally:
        conn.close()
    if error:
        raise AIError(error, err_kind, st["events"])
    return AIResult(data=data, run_id=run_id, provider=provider, model=model, latency_ms=latency,
                    repaired=repaired, degraded=degraded, events=st["events"] or None)


def transcribe(path: str, filename: str, *, project_id: int | None, user_id: int | None) -> AIResult:
    """语音转写：调用交融 OpenAI 兼容 /audio/transcriptions（语音模型在“模型与额度”或 ZB_AI_ASR_MODEL 配置）。"""
    provider = settings.ai_provider
    started = time.monotonic()
    text, error = None, None
    asr_model = pool.model_for("asr") if provider != "mock" else ""
    model = "mock" if provider == "mock" else asr_model
    usage: dict = {}
    events: list[str] = []
    tries = [0]
    try:
        if provider == "mock":
            text = "（离线模拟转写）3#墩桩基施工今天完成2根桩，累计完成82%。明天继续桩基施工"
        else:
            if not asr_model:
                raise AIError("未配置交融语音识别模型，请管理员在“模型与额度”中设置，或先用手机输入法的语音输入")
            if not settings.ai_host_allowed():
                raise AIError("AI 接口地址不在境内白名单内")
            if pool.paused():
                raise AIError("管理员已暂停 AI 调用")
            _user_cap_check(user_id)
            def send(key: pool.PoolKey):
                with open(path, "rb") as fh, httpx.Client(timeout=httpx.Timeout(pool.timeout(), connect=20.0), trust_env=settings.ai_trust_env) as c:
                    return c.post(f"{settings.ai_base_url}/audio/transcriptions",
                                  headers={"Authorization": f"Bearer {key.secret}", "User-Agent": _UA},
                                  data={"model": asr_model, "language": "zh", "response_format": "json"},
                                  files={"file": (filename, fh, "application/octet-stream")})

            key, resp = _dispatch("asr", send, events, tries)
            pool.record(key, "asr", True)
            usage = {"_key": key.label}
            text = str((resp.json() or {}).get("text", "")).strip()
            if not text:
                raise AIError("语音识别结果为空")
    except AIError as exc:
        error = str(exc)
    except (httpx.HTTPError, ValueError, OSError) as exc:
        error = f"语音识别失败：{type(exc).__name__}"
    latency = int((time.monotonic() - started) * 1000)
    conn = connect()
    try:
        run_id = insert(conn, "ai_runs", {"project_id": project_id, "kind": "asr", "provider": provider, "model": model or "",
                                          "prompt_version": "asr", "input_meta_json": dumps({"file": filename[-40:], "lane": "asr", "http_calls": tries[0], **({"events": events[:12]} if events else {})}),
                                          "output_text": (text or "")[:4000], "ok": 0 if error else 1, "latency_ms": latency,
                                          "tokens_json": dumps(usage), "error": error, "created_by": user_id, "created_at": now_iso()})
    finally:
        conn.close()
    if error:
        raise AIError(error)
    return AIResult(data=text, run_id=run_id, provider=provider, model=model, latency_ms=latency)
