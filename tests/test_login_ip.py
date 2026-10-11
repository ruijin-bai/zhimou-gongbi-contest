from fastapi.testclient import TestClient

from app.db import all_, connect
from app.main import app
from conftest import PW


def _audit(action):
    conn = connect()
    try:
        return all_(conn, "SELECT user_id, detail FROM audit WHERE action=? ORDER BY id", (action,))
    finally:
        conn.close()


def _client():
    c = TestClient(app)
    c.headers.update({"x-zb": "1"})
    return c


def test_login_records_cloudflare_ip(env):
    c = _client()
    r = c.post("/api/login", json={"username": "pm", "password": PW},
               headers={"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "198.51.100.1"})
    assert r.status_code == 200
    assert _audit("login")[-1] == {"user_id": env["users"]["pm"], "detail": "ip=203.0.113.7"}


def test_login_falls_back_to_forwarded_then_direct(env):
    c = _client()
    c.post("/api/login", json={"username": "pm", "password": PW}, headers={"X-Forwarded-For": "198.51.100.1, 10.0.0.1"})
    c.post("/api/login", json={"username": "pm", "password": PW})
    assert [r["detail"] for r in _audit("login")[-2:]] == ["ip=198.51.100.1", "ip=testclient"]


def test_failed_login_recorded_with_ip(env):
    c = _client()
    assert c.post("/api/login", json={"username": "pm", "password": "wrong"}, headers={"CF-Connecting-IP": "203.0.113.9"}).status_code == 401
    assert c.post("/api/login", json={"username": "nobody", "password": "x"}, headers={"CF-Connecting-IP": "203.0.113.9"}).status_code == 401
    rows = _audit("login.fail")
    assert rows[-2:] == [{"user_id": env["users"]["pm"], "detail": "ip=203.0.113.9"},
                         {"user_id": None, "detail": "ip=203.0.113.9 账号=nobody"}]
