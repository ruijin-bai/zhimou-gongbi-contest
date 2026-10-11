"""领导“多项目履约总览”需要的两个虚构小项目（名称、编码、账号全部虚构，界面带仿真标注）。

做法：复用 app/sim.py 的仿真生成器（不改它），只换项目名、编码、账号前缀；两个小项目周期更短、规模更小。
用法：python -m tools.showcase.minis --data-dir data_showcase [--day 2026-10-05] [--agents]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MINIS = [
    {"prefix": "lk", "code": "SIM-LINK", "name": "仿真港区连接线项目", "weeks": 6, "seed": 71, "lag": 0},
    {"prefix": "br", "code": "SIM-BRIDGE", "name": "仿真桥梁加固项目", "weeks": 4, "seed": 72, "lag": 7},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data_showcase"))
    ap.add_argument("--day", default="2026-10-04")
    ap.add_argument("--agents", action="store_true", help="在小项目上跑预警/简报等 Agent（AI 文字由仿真撰写给出，不调用外部模型）")
    a = ap.parse_args()
    data_dir = Path(a.data_dir).resolve()
    if data_dir == (ROOT / "data").resolve() or not (data_dir / "DEMO_COPY.json").exists():
        sys.exit("只能对演示副本运行")
    os.environ["ZB_DATA_DIR"] = str(data_dir)
    os.environ["ZB_SCHEDULER"] = "0"
    os.environ.setdefault("ZB_AI_PROVIDER", "mock")
    os.environ["ZB_ENV_FILE"] = "NUL"
    sys.path.insert(0, str(ROOT))
    import time_machine
    trav = time_machine.travel(datetime.fromisoformat(a.day + "T20:30:00").astimezone(), tick=True)
    trav.start()
    from app import sim
    from app.db import connect, init_db
    init_db()
    if a.agents:
        from tools.showcase import author
        author.install()
    day = date.fromisoformat(a.day)
    orig = (sim.SIM_CODE, dict(sim.TECHS), dict(sim.COMPANY_OF), list(sim.ROLE_ACCOUNTS))
    for m in MINIS:
        conn = connect()
        old = conn.execute("SELECT id FROM projects WHERE code=?", (m["code"],)).fetchone()
        if old:
            print("已有", m["code"])
            conn.close()
            if a.agents:
                print(sim.run_agents(old["id"]))
            continue
        pre = m["prefix"]
        for u in ("sim_pm", "sim_leader"):  # 腾出生成器写死的两个账号名，建完再还原
            conn.execute("UPDATE users SET username='tmp_'||username WHERE username=?", (u,))
        conn.close()
        sim.SIM_CODE = m["code"]
        sim.TECHS = {k: (f"{pre}_{v[0][4:]}", v[1], v[2]) for k, v in orig[1].items()}
        sim.COMPANY_OF = {f"{pre}_{k[4:]}": v.replace("甲分包单位", "丙分包单位").replace("乙分包单位", "丁分包单位") for k, v in orig[2].items()}
        sim.ROLE_ACCOUNTS = [(f"{pre}_{u[4:]}", n, r, c.replace("甲分包单位", "丙分包单位").replace("乙分包单位", "丁分包单位")) for u, n, r, c in orig[3]]
        try:
            res = sim.build(weeks=m["weeks"], seed=m["seed"], end=day - timedelta(days=m["lag"] + 1), password="sim12345")
        finally:
            conn = connect()
            for u, new in (("sim_pm", f"{pre}_pm"), ("sim_leader", f"{pre}_leader")):
                conn.execute("UPDATE users SET username=? WHERE username=? AND project_id=(SELECT id FROM projects WHERE code=?)", (new, u, m["code"]))
            for u in ("sim_pm", "sim_leader"):
                conn.execute("UPDATE users SET username=? WHERE username=?", (u, "tmp_" + u))
            conn.execute("UPDATE projects SET name=? WHERE code=?", (m["name"], m["code"]))
            conn.close()
        sim.SIM_CODE, sim.TECHS, sim.COMPANY_OF, sim.ROLE_ACCOUNTS = orig
        print("已生成", m["code"], res["logs"], "条日志")
        if a.agents:
            print(sim.run_agents(res["project_id"]))
    trav.stop()


if __name__ == "__main__":
    main()
