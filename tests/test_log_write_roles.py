"""B1（验收 2026-10-11）：施工日志/周进度确认的新建、编辑、整理、提交只给技术员；其他岗位直调接口返回 403。"""
from app import db as dbmod
from app.security import hash_password

from conftest import PW, login

OTHER_ROLES = ("leader", "manager", "eng_sup", "safety_sup", "admin", "sub_safety")


def _add_roles(env):
    conn = dbmod.connect()
    for role in OTHER_ROLES:
        if role in ("manager", "admin"):
            continue
        dbmod.insert(conn, "users", {"username": f"u_{role}", "name": role, "role": role, "password_hash": hash_password(PW),
                                     "project_id": env["project_id"], "wbs_scope": "", "email": "", "active": 1, "created_at": dbmod.now_iso()})
    conn.close()


def _client(role):
    return login({"manager": "pm", "admin": "admin"}.get(role, f"u_{role}"))


def test_only_tech_writes_logs(env):
    _add_roles(env)
    tech = login("tech1")
    log = tech.post("/api/logs", json={}).json()
    assert log["id"]
    for role in OTHER_ROLES:
        c = _client(role)
        assert c.post("/api/logs", json={}).status_code == 403, role
        assert c.post("/api/logs", json={"period": "week"}).status_code == 403, role
        assert c.put(f"/api/logs/{log['id']}", json={"content": {}, "version": log["version"]}).status_code == 403, role
        assert c.post(f"/api/logs/{log['id']}/submit", json={"version": log["version"]}).status_code == 403, role
        assert c.post(f"/api/logs/{log['id']}/draft", json={}).status_code == 403, role
        assert c.post(f"/api/logs/{log['id']}/carry").status_code == 403, role
        assert c.post(f"/api/logs/{log['id']}/answer", json={"index": 0, "answer": "x"}).status_code == 403, role
        assert c.post("/api/logs/organize", json={}).status_code == 403, role
    # 技术员本人照常保存、提交
    saved = tech.put(f"/api/logs/{log['id']}", json={"content": {"summary": "浇筑3#墩"}, "version": log["version"]})
    assert saved.status_code == 200, saved.text
    assert tech.post(f"/api/logs/{log['id']}/submit", json={"version": saved.json()["version"]}).status_code == 200
    conn = dbmod.connect()
    n = dbmod.one(conn, "SELECT COUNT(*) n FROM logs l JOIN users u ON u.id=l.author_id WHERE u.role!='tech'")["n"]
    conn.close()
    assert n == 0
