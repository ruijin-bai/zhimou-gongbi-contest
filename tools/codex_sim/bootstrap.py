"""Accounts only, for the required pre-generation page walkthrough."""
import sys, hashlib
from runtime import configure, ROOT, DATA
configure()
sys.path.insert(0, str(ROOT))
from app.db import init_db, connect, insert, dumps
init_db()
c=connect()
if not c.execute('SELECT id FROM projects').fetchone():
    insert(c,'projects',dict(id=1,name='仿真公路项目',code='SIM-ROAD',config_json=dumps({'simulation':True,'cadence':'weekly','workdays_per_week':6}),created_at='2026-01-05T08:00:00+01:00'))
    accounts=[('sim_zhao','tech','林望舒','远辰路基公司'),('sim_safe_a','sub_safety','孟泽安','远辰路基公司'),('sim_eng','eng_sup','苏景澄','澄岳国际工程'),('sim_safety','safety_sup','沈予宁','澄岳国际工程'),('sim_pm','manager','陆知远','澄岳国际工程'),('sim_leader','leader','顾清和','澄岳国际工程'),('sim_admin','admin','程砚秋','澄岳国际工程')]
    for i,(username,role,name,company) in enumerate(accounts,1):
        salt=hashlib.sha256(f'codex-sim-user-{i}'.encode()).digest()[:16]
        digest=hashlib.pbkdf2_hmac('sha256',b'SimRoad2026!',salt,240000).hex()
        insert(c,'users',dict(id=i,username=username,name=name,role=role,company=company,password_hash=f'pbkdf2$240000${salt.hex()}${digest}',project_id=1,created_at='2026-01-05T08:00:00+01:00'))
c.execute("INSERT OR REPLACE INTO ai_settings(k,v) VALUES ('paused','1')")
c.close()
(DATA/'.gitignore').write_text('*\n!.gitignore\n',encoding='utf-8')
(DATA/'isolated.env').write_text('# No external services in this simulation.\n',encoding='utf-8')
print('Bootstrap ready: isolated schema and 7 fictional accounts')
