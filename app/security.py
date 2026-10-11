"""账号口令与会话令牌（仅标准库）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from .config import settings

_ITERATIONS = 240_000
SESSION_SECONDS = 14 * 24 * 3600


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(user_id: int, password_hash: str) -> str:
    # 口令指纹绑定令牌：改密码即令旧会话失效
    payload = {"u": user_id, "e": int(time.time()) + SESSION_SECONDS, "p": password_hash[-12:]}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(settings.secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(token: str) -> dict | None:
    try:
        body, sig = token.split(".")
        expected = _b64(hmac.new(settings.secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("e", 0) < time.time():
            return None
        return payload
    except (ValueError, json.JSONDecodeError):
        return None
