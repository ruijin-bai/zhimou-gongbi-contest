"""Read only domain/API verification, limited to our data and port 8663."""
import sys,json,hashlib
from datetime import date,timedelta
from pathlib import Path
from runtime import configure, ROOT, DATA
configure(); sys.path.insert(0,str(ROOT))
import requests
from PIL import Image
from app.db import connect,one,all_,loads
from app.services import output,claims

BASE='http://127.0.0.1:8663'
c=connect(); checks=[]; errors=[]
def check(label,ok,detail=''):
    checks.append({'check':label,'pass':bool(ok),'detail':detail})
    if not ok: errors.append(label)
m=json.loads((DATA/'manifest.json').read_text(encoding='utf-8'))
check('SQLite integrity',one(c,'PRAGMA integrity_check')['integrity_check']=='ok')
check('Foreign keys',not all_(c,'PRAGMA foreign_key_check'))
check('BOQ 120',one(c,'SELECT COUNT(*) n FROM boq_items WHERE project_id=1')['n']==120)
check('No contracts',all(one(c,f'SELECT COUNT(*) n FROM {t}')['n']==0 for t in ('contract_docs','contract_clauses','claim_packs','claim_notices')))
check('No derived contract events',not claims.all_events(c,1))
check('External AI keys zero',one(c,'SELECT COUNT(*) n FROM ai_keys')['n']==0)
check('AI paused',one(c,"SELECT v FROM ai_settings WHERE k='paused'")['v']=='1')
check('No successful model runs',one(c,'SELECT COUNT(*) n FROM ai_runs WHERE ok=1')['n']==0)
check('No daily quantity double count',one(c,"SELECT COUNT(*) n FROM log_progress lp JOIN logs l ON l.id=lp.log_id WHERE l.project_id=1 AND l.period='day'")['n']==0)
q=output.compute(c,1,'2026-01-05','2026-10-04')
boq=one(c,'SELECT SUM(contract_qty*unit_price) v FROM boq_items WHERE project_id=1')['v']
records=one(c,'SELECT SUM(actual) v FROM output_records WHERE project_id=1')['v']
check('Actual one-source reconciliation at USD-cent precision',abs(q['total']-records)<.005,f"quantity ledger {q['total']:.2f}; weekly record {records:.2f}")
check('Completion 55-65 percent',55<=q['total']/boq*100<=65,f'{q["total"]/boq*100:.3f}%')
check('No unmapped quantities or unit mismatch',not q['unmapped'] and not q['unit_mismatch'])
days=all_(c,"SELECT log_date,COUNT(DISTINCT author_id) n FROM logs WHERE project_id=1 AND period='day' GROUP BY log_date ORDER BY log_date")
expected=(date(2026,10,4)-date(2026,1,5)).days+1
check('273 uninterrupted daily dates',len(days)==expected and all(r['n']==3 for r in days))
check('39 uninterrupted confirmed weeks',one(c,"SELECT COUNT(DISTINCT log_date) n FROM logs WHERE project_id=1 AND period='week' AND status='approved'")['n']==39)
ps=all_(c,'SELECT data_date FROM p6_imports WHERE project_id=1 ORDER BY id')
check('Baseline plus six updates',len(ps)==7 and ps[-1]['data_date']=='2026-10-04')
media=all_(c,'SELECT * FROM media WHERE project_id=1')
for r in media:
    p=DATA/'media'/r['file']
    check(f'Media {r["id"]} exists and matches hash',p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==r['sha256'])
    with Image.open(p) as im: check(f'Media {r["id"]} 4:3',im.width*3==im.height*4)
check('At least 70 scene and 15 document images',sum(r['purpose']!='ticket' for r in media)>=70 and sum(r['purpose']=='ticket' for r in media)>=15)
for f in all_(c,"SELECT * FROM findings WHERE project_id=1 AND agent='safety'"):
    if f['confirmed_at'] is None:
        check(f'Candidate {f["id"]} not dispatched',f['assignee_id'] is None and f['due_date'] is None and f['resolved_at'] is None)
    if f['status']=='resolved':
        uploader=one(c,'SELECT uploader_id,taken_at FROM media WHERE id=?',(f['rectify_media_id'],))
        check(f'Closure {f["id"]} independent human review',f['feedback_by']==4 and uploader['uploader_id']!=f['feedback_by'] and uploader['taken_at']>=f['confirmed_at'] and f['resolved_at']>=uploader['taken_at'])
for r in all_(c,"SELECT kind,period_end FROM reports WHERE project_id=1"):
    check('Report cutoff '+r['kind']+' '+r['period_end'],r['period_end']<='2026-10-04')
api=[]
def get(s,path):
    r=s.get(BASE+path,timeout=30)
    if r.status_code!=200:
        api.append({'path':path,'status':r.status_code,'error':r.text[:180]}); return None
    try: data=r.json()
    except ValueError: data=None
    api.append({'path':path,'status':200,'size':len(r.content)})
    return data
rolepaths={
 'tech':['/api/tech/home','/api/logs','/api/feed','/api/conceal','/api/tickets','/api/notifications'],
 'sub_safety':['/api/safety/home','/api/feed','/api/findings','/api/quick/summary','/api/notifications'],
 'eng_sup':['/api/eng/overview','/api/p6/imports','/api/p6/activities','/api/alerts','/api/output/overview','/api/gongbi/docs','/api/reports','/api/conceal','/api/tickets','/api/sub-profiles','/api/notifications'],
 'safety_sup':['/api/safety/overview','/api/findings','/api/reports','/api/gongbi/docs','/api/notifications'],
 'manager':['/api/cockpit','/api/alerts','/api/findings','/api/output/overview','/api/reports','/api/gongbi/docs','/api/notifications'],
 'leader':['/api/cockpit','/api/output/overview','/api/reports','/api/gongbi/docs'],
 'admin':['/api/admin/overview','/api/admin/audit','/api/users','/api/agents','/api/notifications']}
users={r['role']:r['username'] for r in all_(c,'SELECT username,role FROM users WHERE id<=7')}
for role,paths in rolepaths.items():
    s=requests.Session(); s.headers['x-zb']='1'
    login=s.post(BASE+'/api/login',json={'username':users[role],'password':'SimRoad2026!'},timeout=15)
    check('Login '+role,login.status_code==200)
    for p in paths: get(s,p)
    s.close()
anon=requests.get(BASE+'/q/SIMROAD2026WORKERQR001234567890',timeout=10)
check('Worker QR anonymous and no project name',anon.status_code==200 and '仿真公路项目' not in anon.text)
(DATA/'domain_checks.json').write_text(json.dumps({'checks':checks,'api':api,'failed':errors},ensure_ascii=False,indent=2),encoding='utf-8')
c.close()
print(json.dumps({'checks':len(checks),'failed':errors,'api_errors':[r for r in api if r['status']!=200]},ensure_ascii=False,indent=2))
