from fastapi.testclient import TestClient

from app.main import app


def _client():
    c = TestClient(app)
    c.headers.update({"x-zb": "1"})
    return c


def test_dev_login_off_by_default(env, monkeypatch):
    monkeypatch.delenv("ZB_DEV_LOGIN", raising=False)
    c = _client()
    assert c.get("/api/dev/accounts").json() == []
    assert c.post("/api/dev/login", json={"user_id": env["users"]["admin"]}).status_code == 404
    assert c.get("/api/me").status_code == 401


def test_dev_login_when_enabled(env, monkeypatch):
    monkeypatch.setenv("ZB_DEV_LOGIN", "1")
    c = _client()
    accounts = c.get("/api/dev/accounts").json()
    assert {a["username"] for a in accounts} == {"admin", "pm", "tech1", "tech2"}
    assert "password_hash" not in accounts[0]
    r = c.post("/api/dev/login", json={"user_id": env["users"]["pm"]})
    assert r.status_code == 200 and r.json()["role"] == "manager"
    assert c.get("/api/me").json()["user"]["username"] == "pm"


def test_dev_login_project_filter(env, monkeypatch):
    """ZB_DEV_PROJECT 只列该项目编码下的账号；库里没有该项目的账号时照常列全部。"""
    monkeypatch.setenv("ZB_DEV_LOGIN", "1")
    monkeypatch.setenv("ZB_DEV_PROJECT", "T1")
    c = _client()
    assert {a["project_code"] for a in c.get("/api/dev/accounts").json()} == {"T1"}
    monkeypatch.setenv("ZB_DEV_PROJECT", "NOPE")
    assert {a["username"] for a in c.get("/api/dev/accounts").json()} == {"admin", "pm", "tech1", "tech2"}
