"""Key 池、分线路模型、额度与管理员后台（不联网：用假的 httpx 响应）。"""
import json
import re

import pytest

from conftest import login
from app import db as dbmod
from app.ai import client, pool
from app.config import settings

SK1, SK2, SK3 = "sk-test-key-aaaaaaaaaaaa1111", "sk-test-key-bbbbbbbbbbbb2222", "sk-test-key-cccccccccccc3333"


@pytest.fixture()
def pooled(env, monkeypatch):
    """三把假 Key + 真实路径（jiaorong），httpx 被替换成可编程的假响应。"""
    monkeypatch.setattr(settings, "ai_provider", "jiaorong")
    conn = dbmod.connect()
    ids = [pool.add_key(conn, k, f"K{i + 1}", lanes) for i, (k, lanes) in enumerate(((SK1, ["vision"]), (SK2, ["core"]), (SK3, ["agent"])))]
    conn.close()
    calls = []
    script = {}  # key 尾号 -> (状态码, usage)

    class Resp:
        def __init__(self, code, usage):
            self.status_code, self._usage, self.text = code, usage, "err"

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": self._usage}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            tail = headers["Authorization"][-4:]
            calls.append((tail, json["model"]))
            code, usage = script.get(tail, (200, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}))
            return Resp(code, usage)

    def fake_post(url, headers=None, json=None, **kw):
        return FakeClient().post(url, headers=headers, json=json)

    monkeypatch.setattr(client.httpx, "Client", FakeClient)
    monkeypatch.setattr(client.httpx, "post", fake_post)
    pool._cool.clear()
    pool._cfg_cache["val"] = None
    yield {"ids": ids, "calls": calls, "script": script}
    pool._cool.clear()


def test_lane_of_and_token_math():
    assert pool.lane_of("safety.scan", True) == "vision"
    assert pool.lane_of("feed.comment") == "agent" and pool.lane_of("nudge.plan") == "agent"
    assert pool.lane_of("log.draft") == "core" and pool.lane_of("ai.test") == "admin" and pool.lane_of("asr") == "asr"
    assert pool.tokens_of({"total_tokens": 7}) == 7
    assert pool.tokens_of({"prompt_tokens": 3, "completion_tokens": 4}) == 7
    assert pool.tokens_of({"first": {"total_tokens": 5}, "repair": {"total_tokens": 2}}) == 7
    assert [pool.plan_lanes(4, i) for i in range(4)] == [["vision"], ["core"], ["agent"], ["asr", "admin"]]
    assert pool.plan_lanes(1, 0) == ["vision", "core", "agent", "asr", "admin"]


def test_lane_routes_to_its_key(pooled):
    _, usage = client._post("m", [], 8, 0, "vision")
    assert usage["_key"] == "K1"
    client._post("m", [], 8, 0, "core")
    client._post("m", [], 8, 0, "agent")
    assert [c[0] for c in pooled["calls"]] == ["1111", "2222", "3333"]
    conn = dbmod.connect()
    um = pool.usage_map(conn)
    conn.close()
    assert um[pooled["ids"][0]]["today"] == {"calls": 1, "fails": 0, "tokens": 15}


def test_quota_exhausted_falls_over_then_reports(pooled):
    conn = dbmod.connect()
    conn.execute("UPDATE ai_keys SET daily_calls=1 WHERE id=?", (pooled["ids"][1],))
    conn.close()
    client._post("m", [], 8, 0, "core")  # K2 用掉唯一一次
    _, u = client._post("m", [], 8, 0, "core")  # 换成别的 Key
    assert u["_key"] != "K2"
    conn = dbmod.connect()
    conn.execute("UPDATE ai_keys SET daily_calls=1, daily_tokens=0")
    conn.close()
    with pytest.raises(client.AIError, match="额度已用完"):
        for _ in range(6):
            client._post("m", [], 8, 0, "core")


def test_401_marks_invalid_and_429_cools(pooled):
    pooled["script"]["1111"] = (401, {})
    _, u = client._post("m", [], 8, 0, "vision")  # 主责 K1 失效 → 自动换 Key
    assert u["_key"] != "K1"
    conn = dbmod.connect()
    row = dbmod.one(conn, "SELECT status FROM ai_keys WHERE id=?", (pooled["ids"][0],))
    conn.close()
    assert row["status"] == "invalid"
    pooled["script"]["2222"] = (429, {})
    _, u = client._post("m", [], 8, 0, "core")
    assert u["_key"] == "K3" and pooled["ids"][1] in pool._cool


def test_paused_and_user_cap(pooled, env):
    conn = dbmod.connect()
    pool.set_config(conn, {"paused": 1})
    conn.close()
    with pytest.raises(client.AIError, match="暂停"):
        client._post("m", [], 8, 0, "core")
    conn = dbmod.connect()
    pool.set_config(conn, {"paused": 0, "user_daily_calls": 1})
    conn.close()
    uid = env["users"]["tech1"]
    kw = dict(system="s", user="u", prompt_version="t", mock=lambda: {}, user_id=uid)
    client.call_json("qa.answer", **kw)
    with pytest.raises(client.AIError, match="个人上限"):
        client.call_json("qa.answer", **kw)
    client.call_json("qa.answer", **{**kw, "user_id": env["users"]["admin"]})  # 管理员不受限
    conn = dbmod.connect()
    pool.set_config(conn, {"user_daily_calls": 0})
    conn.close()


def test_lane_model_override(pooled):
    conn = dbmod.connect()
    pool.set_config(conn, {"model_agent": "jiaorong-test-agent-model"})
    conn.close()
    client.call_json("feed.comment", system="s", user="u", prompt_version="t", mock=lambda: {})
    assert pooled["calls"][-1] == ("3333", "jiaorong-test-agent-model")
    conn = dbmod.connect()
    pool.set_config(conn, {"model_agent": settings.ai_text_model})
    conn.close()


def test_admin_api_hides_secrets_and_guards(pooled, env):
    adm, pm = login("admin"), login("pm")
    assert pm.get("/api/admin/ai").status_code == 403
    assert pm.post("/api/admin/ai/keys", json={"key": "sk-whatever-12345678"}).status_code == 403
    r = adm.get("/api/admin/ai")
    assert r.status_code == 200
    body = r.text
    assert not re.search(r"sk-test-key-[a-z]{12}", body) and "3333" in body
    assert [k["label"] for k in r.json()["keys"]] == ["K1", "K2", "K3"]

    k = adm.post("/api/admin/ai/keys", json={"key": "sk-test-key-dddddddddddd4444", "label": "新Key", "lanes": ["asr", "bogus"], "daily_tokens": 1000})
    assert k.status_code == 200
    new = k.json()["id"]
    assert adm.post("/api/admin/ai/keys", json={"key": "sk-test-key-dddddddddddd4444"}).status_code == 400  # 重复
    assert adm.post("/api/admin/ai/keys", json={"key": "含 空格 的key12345"}).status_code == 400
    assert pool.secret_of(pool.fingerprint("sk-test-key-dddddddddddd4444")).endswith("4444")

    assert adm.put(f"/api/admin/ai/keys/{new}", json={"enabled": False, "daily_calls": 5, "lanes": ["core"]}).status_code == 200
    got = next(x for x in adm.get("/api/admin/ai").json()["keys"] if x["id"] == new)
    assert got["state"] == "off" and got["daily_calls"] == 5 and got["lanes"] == ["core"] and got["mask"] == "sk-…4444"

    cfg = adm.get("/api/admin/ai").json()["config"]
    bad = {**cfg, "paused": False, "model_vision": ""}
    assert adm.put("/api/admin/ai/config", json=bad).status_code == 400
    bad = {**cfg, "paused": False, "model_core": "x y"}
    assert adm.put("/api/admin/ai/config", json=bad).status_code == 400

    t = adm.post(f"/api/admin/ai/keys/{pooled['ids'][0]}/test")
    assert t.status_code == 200 and t.json()["ok"] is True
    assert adm.post("/api/admin/ai/lane-test", json={"lane": "core"}).json()["key"] == "K2"
    assert adm.post("/api/admin/ai/rebalance").status_code == 200

    assert adm.delete(f"/api/admin/ai/keys/{new}").status_code == 200
    assert pool.secret_of(pool.fingerprint("sk-test-key-dddddddddddd4444")) == ""
    audit = [a["action"] for a in adm.get("/api/admin/audit?limit=50&action=ai.").json()]
    assert {"ai.key.add", "ai.key.update", "ai.key.delete", "ai.key.test", "ai.rebalance"} <= set(audit)
    assert "sk-test-key" not in json.dumps(adm.get("/api/admin/audit?limit=100").json())


def test_env_seed_imports_once_and_respects_deletion(env, monkeypatch):
    monkeypatch.setenv("ZB_AI_API_KEYS", f"{SK1},{SK2}")
    conn = dbmod.connect()
    pool.seed_from_env(conn)
    labels = [r["label"] for r in dbmod.all_(conn, "SELECT label FROM ai_keys ORDER BY id")]
    assert len(labels) == 2
    assert [json.loads(r["lanes_json"]) for r in dbmod.all_(conn, "SELECT lanes_json FROM ai_keys ORDER BY id")] == [["vision", "core"], ["agent", "asr", "admin"]]
    pool.seed_from_env(conn)  # 再启动：不重复导入
    assert dbmod.one(conn, "SELECT COUNT(*) n FROM ai_keys")["n"] == 2
    pool.delete_key(conn, dbmod.one(conn, "SELECT id FROM ai_keys ORDER BY id LIMIT 1")["id"])
    pool.seed_from_env(conn)  # 管理员删掉的不会被 .env 又导回
    assert dbmod.one(conn, "SELECT COUNT(*) n FROM ai_keys")["n"] == 1
    conn.close()
