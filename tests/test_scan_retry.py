"""I10（验收 2026-10-11）：随手拍识别失败/超时可见，可由上传人（本单位安全员、安全主管）重新识别。离线模拟模型。"""
from app import db as dbmod

from conftest import jpeg, login
from test_roles import _team


def _photo(c):
    return c.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))]).json()[0]


def test_failed_scan_shows_in_todo_and_can_rescan(env):
    _team(env)
    sa = login("sa_a")
    m = _photo(sa)
    conn = dbmod.connect()
    conn.execute("UPDATE media SET safety_status='failed' WHERE id=?", (m["id"],))
    conn.close()
    it = [i for i in sa.get("/api/todo").json()["items"] if i["source"] == "scan_failed"]
    assert len(it) == 1 and "识别失败" in it[0]["reason"] and it[0]["quick"][0]["api"]["url"] == "/api/quick/media-rescan"
    assert not [i for i in login("sa_b").get("/api/todo").json()["items"] if i["source"] == "scan_failed"]   # 别的单位看不到
    assert login("sa_b").post("/api/quick/media-rescan", json={"ids": [m["id"]]}).status_code == 409        # 也不能替别人重识别
    r = sa.post("/api/quick/media-rescan", json={"ids": [m["id"]]})
    assert r.status_code == 200 and r.json()["ids"] == [m["id"]]
    st = sa.get(f"/api/quick/media-status?ids={m['id']}").json()
    assert st["items"][0]["status"] in ("hazard", "clear")   # 后台已重新识别（TestClient 同步执行后台任务）
    assert not [i for i in sa.get("/api/todo").json()["items"] if i["source"] == "scan_failed"]
    assert sa.post("/api/quick/media-rescan", json={}).status_code == 409
