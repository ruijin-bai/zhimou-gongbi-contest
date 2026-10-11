"""竞赛演示账号：ZB_DEMO_USERS 里的账号可在项目经理、工程主管、安全主管之间一键切换；切换后权限、首页、口径守卫都按所选岗位走。"""
from contextlib import closing

from conftest import login

from app import db as dbmod


def _me(c):
    return c.get("/api/me").json()


def test_off_by_default(env, monkeypatch):
    monkeypatch.delenv("ZB_DEMO_USERS", raising=False)
    pm = login("pm")
    assert _me(pm)["demo"] is None
    assert pm.post("/api/demo/as", json={"role": "eng_sup"}).status_code == 403
    pm.cookies.set("zb_as", "eng_sup")                      # 手动塞 cookie 也没用
    assert _me(pm)["user"]["role"] == "manager"


def test_demo_account_switches_roles(env, monkeypatch):
    monkeypatch.setenv("ZB_DEMO_USERS", "pm")
    pm = login("pm")
    me = _me(pm)
    assert me["demo"]["as"] == "manager" and [r["key"] for r in me["demo"]["roles"]] == ["manager", "eng_sup", "safety_sup"]
    assert pm.put("/api/output/plan", json={"contract_amount": 1}).status_code == 403   # 项目经理视角：产值只读，写操作照常拒绝
    assert pm.post("/api/demo/as", json={"role": "eng_sup"}).json() == {"as": "eng_sup"}
    me = _me(pm)
    assert me["user"]["role"] == "eng_sup" and me["home"] == "eng" and me["demo"]["as"] == "eng_sup"
    assert pm.get("/api/output/overview").status_code == 200   # 工程主管视角：能看产值
    assert pm.post("/api/demo/as", json={"role": "safety_sup"}).status_code == 200
    assert _me(pm)["home"] == "sdash"
    for bad in ("admin", "leader", "tech", "sub_safety"):       # 只能在三个港湾管理岗之间切换
        assert pm.post("/api/demo/as", json={"role": bad}).status_code == 400, bad
    with closing(dbmod.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM audit WHERE action='demo.as'").fetchone()["n"] == 2
        assert conn.execute("SELECT role FROM users WHERE username='pm'").fetchone()["role"] == "manager"   # 账号本身的岗位不变


def test_only_listed_harbour_accounts(env, monkeypatch):
    monkeypatch.setenv("ZB_DEMO_USERS", "pm,admin,tech1")
    for u in ("admin", "tech1"):                                # 系统管理员、分包岗位列进去也不生效
        c = login(u)
        assert _me(c)["demo"] is None
        assert c.post("/api/demo/as", json={"role": "manager"}).status_code == 403


def test_removing_setting_restores_account(env, monkeypatch):
    monkeypatch.setenv("ZB_DEMO_USERS", "pm")
    pm = login("pm")
    pm.post("/api/demo/as", json={"role": "eng_sup"})
    monkeypatch.delenv("ZB_DEMO_USERS")
    me = _me(pm)
    assert me["user"]["role"] == "manager" and me["demo"] is None
