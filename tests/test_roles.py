import os
import sqlite3
import tempfile

from conftest import PW, confirm_hazards, jpeg, login, sample_xer

from app import db as dbmod
from app.security import hash_password


def _add(env, username, role, name, company="", scope=""):
    conn = dbmod.connect()
    uid = dbmod.insert(conn, "users", {"username": username, "name": name, "role": role, "password_hash": hash_password(PW), "project_id": env["project_id"],
                                       "wbs_scope": scope, "email": "", "active": 1, "created_at": dbmod.now_iso(), "company": company})
    conn.close()
    env["users"][username] = uid
    return uid


def _team(env):
    # 甲：技术员 tech1 + 安全员 sa_a；乙：技术员 tech2 + 安全员 sa_b；港湾：工程主管、安全主管
    conn = dbmod.connect()
    conn.execute("UPDATE users SET company='甲' WHERE username='tech1'")
    conn.execute("UPDATE users SET company='乙' WHERE username='tech2'")
    conn.close()
    _add(env, "sa_a", "sub_safety", "安全员甲", "甲")
    _add(env, "sa_b", "sub_safety", "安全员乙", "乙")
    _add(env, "eng", "eng_sup", "工程主管")
    _add(env, "ss", "safety_sup", "安全主管")


def _hazard(env):
    tech = login("tech1")
    tech.post("/api/media", data={"purpose": "safety"}, files=[("files", ("hz.jpg", jpeg((200, 50, 50)), "image/jpeg"))])
    confirm_hazards()  # 候选需人确认才进台账；本组用例关心的是确认之后的台账/权限行为
    hz = tech.get("/api/findings?agent=safety&status=active&mine=1").json()
    assert hz
    return tech, hz[0]["id"]


def test_migration_to_v4_keeps_users_and_adds_roles():
    conn = sqlite3.connect(os.path.join(tempfile.mkdtemp(prefix="zb_mig_"), "old.db"), isolation_level=None)
    conn.row_factory = dbmod._dict_factory
    conn.executescript("""CREATE TABLE projects (id INTEGER PRIMARY KEY);
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('tech','manager','leader','admin')), password_hash TEXT NOT NULL,
            project_id INTEGER, wbs_scope TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        INSERT INTO users (username, name, role, password_hash, created_at) VALUES ('u', 'U', 'tech', 'x', 'now');
        PRAGMA user_version=3;""")
    dbmod._migrate(conn)
    row = conn.execute("SELECT role, company FROM users").fetchone()
    assert row == {"role": "tech", "company": ""}
    conn.execute("INSERT INTO users (username, name, role, password_hash, created_at) VALUES ('s', 'S', 'sub_safety', 'x', 'now')")
    conn.close()


def test_role_homes_and_visibility(env):
    _team(env)
    login("pm").post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    _hazard(env)
    me = login("sa_a").get("/api/me").json()
    assert me["home"] == "shome" and me["user"]["org"] == "sub" and "dispatch" in me["can"] and "site_safety" not in me["can"]
    home = login("sa_a").get("/api/safety/home")
    assert home.status_code == 200 and home.json()["counts"]["pending"] == 1 and home.json()["company"] == "甲"
    assert login("sa_b").get("/api/safety/home").json()["counts"]["pending"] == 0  # 乙单位看不到甲的隐患
    assert login("eng").get("/api/eng/overview").status_code == 200
    assert login("eng").get("/api/production/overview").status_code == 200
    ov = login("ss").get("/api/safety/overview").json()
    assert ov["kpis"]["open"] == 1 and ov["by_company"][0]["company"] == "甲"
    assert login("admin").get("/api/admin/overview").json()["users"]["total"] >= 6
    assert login("admin").get("/api/admin/audit").status_code == 200
    assert login("pm").get("/api/companies").status_code == 200
    # 分包单位不能看港湾汇总
    for path in ("/api/safety/overview", "/api/eng/overview", "/api/companies", "/api/alerts", "/api/reports", "/api/cockpit"):
        assert login("sa_a").get(path).status_code == 403, path
        assert login("tech1").get(path).status_code == 403, path
    assert login("eng").get("/api/admin/overview").status_code == 403
    assert login("ss").get("/api/safety/home").status_code == 403
    assert login("sa_a").get("/api/logs").status_code == 403  # 安全员不处理施工日志


def test_company_scope_and_dispatch(env):
    _team(env)
    tech, fid = _hazard(env)
    sa_a, sa_b = login("sa_a"), login("sa_b")
    assert [f["id"] for f in sa_a.get("/api/findings?agent=safety&status=active").json()] == [fid]
    assert sa_b.get("/api/findings?agent=safety&status=active").json() == []
    assert sa_b.get(f"/api/findings/{fid}").status_code == 404
    media = sa_a.get(f"/api/findings/{fid}").json()["ref_id"]
    assert sa_a.get(f"/api/media/{media}/thumb").status_code == 200
    assert sa_b.get(f"/api/media/{media}/thumb").status_code == 200   # 随手拍照片在现场圈里，全项目可见（10-09）；隐患单本身仍只到本单位（上面 404）
    # 安全员只能在本单位内派单
    assert sa_a.put(f"/api/findings/{fid}/assignee", json={"assignee_id": env["users"]["tech2"]}).status_code == 400
    assert sa_b.put(f"/api/findings/{fid}/assignee", json={"assignee_id": env["users"]["tech2"]}).status_code == 403
    r = sa_a.put(f"/api/findings/{fid}/assignee", json={"assignee_id": env["users"]["sa_a"]})
    assert r.status_code == 200 and r.json()["assignee"] == "安全员甲"
    # 港湾安全主管可派给任何人并催办；技术员收到消息
    ss = login("ss")
    assert ss.put(f"/api/findings/{fid}/assignee", json={"assignee_id": env["users"]["tech1"]}).status_code == 200
    assert ss.post(f"/api/findings/{fid}/nudge", json={"text": "今天必须完成"}).status_code == 200
    assert any("催办" in n["title"] for n in tech.get("/api/notifications").json())
    assert tech.put(f"/api/findings/{fid}/assignee", json={"assignee_id": env["users"]["tech1"]}).status_code == 403
    assert tech.post(f"/api/findings/{fid}/nudge", json={}).status_code == 403
    assert login("eng").get("/api/assignees").json()  # 港湾可看到全部可派对象
    assert {a["name"] for a in sa_a.get("/api/assignees").json()} == {"张工", "安全员甲"}


def test_close_authority_by_role(env):
    _team(env)
    tech, fid = _hazard(env)
    # 分包单位不能自行闭环、忽略安全隐患；工程主管也不是安全隐患的确认人
    assert tech.post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 400
    assert login("sa_a").post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 400
    assert login("eng").post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 400
    assert login("ss").post(f"/api/findings/{fid}/status", json={"status": "resolved"}).status_code == 200
    assert login("ss").get("/api/safety/overview").json()["kpis"]["resolved_7d"] == 1


def test_user_admin_roles_and_company(env):
    admin = login("admin")
    r = admin.post("/api/users", json={"username": "sa9", "name": "安全员九", "role": "sub_safety", "password": "abcdef", "company": "丙分包"})
    assert r.status_code == 200 and r.json()["company"] == "丙分包" and r.json()["role_name"] == "安全员"
    assert admin.post("/api/users", json={"username": "x1", "name": "X", "role": "sub_head", "password": "abcdef"}).status_code == 400
    # 港湾岗位不记录分包单位
    r = admin.post("/api/users", json={"username": "e9", "name": "工程九", "role": "eng_sup", "password": "abcdef", "company": "丙分包"})
    assert r.json()["company"] == ""
    roles = admin.get("/api/roles").json()
    assert [x["key"] for x in roles["roles"] if x["org"] == "sub"] == ["sub_safety", "tech"]
    assert login("pm").put(f"/api/users/{r.json()['id']}", json={"username": "e9", "name": "x", "role": "tech"}).status_code == 403


def test_ai_did_by_role(env):
    """原 /api/ai/feed（已下线）的“智能体今日处理”口径，现由 /api/todo 的 did 承担；项目级汇总对分包单位仍 403（/api/cockpit）。"""
    _team(env)
    login("pm").post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")})
    _hazard(env)
    assert login("pm").get("/api/ai/feed").status_code == 404
    pm = login("pm").get("/api/todo").json()
    assert all(a["runs"] >= a["ok"] > -1 for a in pm["did"])  # 只数真实调用，不补数
    assert any(a["key"] == "safety" for a in pm["did"])
    ss = login("ss").get("/api/todo").json()
    assert ss["did"] and all(a["key"] == "safety" for a in ss["did"])
    assert all(i["module"] == "safety" for i in ss["items"] if i["ai"])  # 安全主管的 AI 待办只有安全
    assert login("sa_a").get("/api/cockpit").status_code == 403  # 分包单位不看项目级汇总
