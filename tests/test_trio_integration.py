"""三个新功能之间的数据约定：放行清单读取隐蔽验收（conceal）、材料批次与浇筑（tickets）的真实表，而不是测试桩。"""
from datetime import date

import pytest
from conftest import login, sample_xer
from test_roles import _add

from app import db as dbmod


@pytest.fixture()
def world(env):
    _add(env, "eng", "eng_sup", "工程主管")
    assert login("pm").post("/api/p6/import", files={"file": ("plan.xer", sample_xer(), "application/octet-stream")}).status_code == 200
    return env


def _cond(d, key):
    return next(c for c in d["conditions"] if c["key"] == key)


def test_gate_reads_real_conceal_and_batches(world):
    eng = login("eng")
    pid = world["project_id"]
    # 隐蔽验收：走真实接口建立并推进到“通过”
    tpl = eng.get("/api/conceal/templates").json()
    keys = [t["key"] for t in (tpl["templates"] if isinstance(tpl, dict) else tpl)]
    assert "rebar" in keys and "formwork" in keys
    conn = dbmod.connect()
    for key, status in (("rebar", "passed"), ("formwork", "submitted")):
        dbmod.insert(conn, "conceal_records", {"project_id": pid, "activity_code": "A1030", "part": "3#墩承台", "template_key": key,
                                                "title": "t", "status": status, "inspection_date": date.today().isoformat(),
                                                "created_at": dbmod.now_iso()})
    # 材料批次：用材料Agent真实的类别代码
    dbmod.insert(conn, "material_batches", {"project_id": pid, "category": "rebar", "name": "HRB400E Φ25", "qty": 30, "unit": "t",
                                             "retest_status": "pending", "arrived_at": date.today().isoformat(), "created_at": dbmod.now_iso()})
    conn.close()
    d = eng.get("/api/gate/A1030").json()
    assert _cond(d, "rebar_conceal")["auto_state"] == "ok"
    assert _cond(d, "formwork")["auto_state"] == "unknown"          # 仅已提交，不算通过
    m = _cond(d, "material")
    assert m["auto_state"] == "missing" and "未出结果" in m["auto_text"]
    conn = dbmod.connect()
    conn.execute("UPDATE material_batches SET retest_status='pass'")
    conn.close()
    assert _cond(eng.get("/api/gate/A1030").json(), "material")["auto_state"] == "ok"


def test_gate_pile_template_covers_hole_and_cage(world):
    # 无需依赖具体作业：直接用模块函数核验 pile 模板键能匹配成孔/钢筋笼
    from app.services import gate as g
    ctx = type("C", (), {"conceal": {"P1": [{"id": 1, "activity_code": "P1", "part": "4#墩桩基", "template_key": "pile", "status": "passed", "inspection_date": None}]}})()
    act = {"code": "P1", "name": "4#墩桩基施工"}
    assert g.chk_conceal(ctx, act, "hole")["state"] == "ok"
    assert g.chk_conceal(ctx, act, "cage")["state"] == "ok"
