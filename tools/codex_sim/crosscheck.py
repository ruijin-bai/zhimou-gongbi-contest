"""Read back isolated page APIs and reconcile them with source rows."""
import sys,json,requests
from datetime import date
from runtime import configure,ROOT,DATA
configure();sys.path.insert(0,str(ROOT))
from app.db import connect,one,all_,loads
from app.services import output
BASE='http://127.0.0.1:8663'
s=requests.Session();s.headers['x-zb']='1'
assert s.post(BASE+'/api/login',json={'username':'sim_eng','password':'SimRoad2026!'},timeout=15).status_code==200
read={}
def get(path):
    r=s.get(BASE+path,timeout=60);r.raise_for_status();read[path]=r.json();return read[path]
o=get('/api/output/overview');p=get('/api/reports/6');b=get('/api/cockpit');safety=get('/api/safety/overview')
fs=get('/api/findings?agent=safety&status=');qs=get('/api/findings?agent=quality&status=')
al=get('/api/alerts');get('/api/p6/imports');get('/api/reports');get('/api/gongbi/docs');get('/api/sub-profiles')
assert s.post(BASE+'/api/login',json={'username':'sim_leader','password':'SimRoad2026!'},timeout=15).status_code==200
leader=get('/api/cockpit');read['/api/cockpit#leader']=leader
assert s.post(BASE+'/api/login',json={'username':'sim_pm','password':'SimRoad2026!'},timeout=15).status_code==200
manager_output=get('/api/output/overview');read['/api/output/overview#manager']=manager_output
c=connect();rows=[]
def row(name,values,pages,detail=''):
    numeric=[v for v in values if v is not None]
    ok=max(numeric)-min(numeric)<.005 if numeric else False
    rows.append({'metric':name,'values':values,'page_routes':pages,'consistent':ok,'detail':detail})
ledger=output.compute(c,1,'2026-01-05','2026-10-04')['total']
row('开累内部产值 USD',[o['report']['cumulative']['actual'],next(v['value']*10000 for v in p['kpi10']['items'] if v['key']=='cum'),manager_output['report']['cumulative']['actual']],['output','report?id=6','work'],'周报按万美元保留两位，表中还原美元会有呈现舍入；经理首页源字段另以经理身份读回，首页按万美元保留一位')
# Display-rounded report value is intentionally tested separately from USD cents.
rows[-1]['consistent']=abs(ledger-o['report']['cumulative']['actual'])<.005 and abs(ledger-manager_output['report']['cumulative']['actual'])<.005 and round(ledger/10000,2)==next(v['value'] for v in p['kpi10']['items'] if v['key']=='cum')
for key,label in [('pct','整体完成百分比（按产值）'),('year_actual','年度已完成产值（万美元）'),('year_plan','年度计划产值（万美元）'),('last_actual','截止周完成产值（万美元）')]:
    ov=next(v['value'] for v in o['kpi10']['items'] if v['key']==key)
    pv=next(v['value'] for v in p['kpi10']['items'] if v['key']==key)
    row(label,[ov,pv],['output','report?id=6'],'仅两个不同页面完整展示；没有伪造第三个展示位')
for key,label,derive in [
 ('total','已确认隐患总数',lambda f:len(f)),
 ('open','隐患未关闭',lambda f:sum(r['status'] in ('open','rectifying','rejected') for r in f)),
 ('high','未关闭高风险隐患',lambda f:sum(r['status'] in ('open','rectifying','rejected') and r['level']=='high' for r in f)),
 ('overdue','超期未整改隐患',lambda f:sum(r['status'] in ('open','rejected') and (r.get('due_date') or '9999')<'2026-10-11' for r in f)),
 ('resolved','已关闭隐患',lambda f:sum(r['status']=='resolved' for r in f))]:
    second=leader['kpis']['safety'][key] if key=='resolved' else safety['kpis'][key]
    row(label,[b['kpis']['safety'][key],second,derive(fs)],['work','cockpit' if key=='resolved' else 'sdash','safety'],'分别核对页面接口底稿及全状态安全台账；完整数字展示仍取现有UI')
extra={'photos':one(c,'SELECT COUNT(*) n FROM media WHERE project_id=1')['n'],
 'reports_week':one(c,"SELECT COUNT(*) n FROM reports WHERE project_id=1 AND kind='week'")['n'],
 'reports_month':one(c,"SELECT COUNT(*) n FROM reports WHERE project_id=1 AND kind='month'")['n'],
 'p6_imports':one(c,'SELECT COUNT(*) n FROM p6_imports WHERE project_id=1')['n'],
 'safety_candidates':safety['candidate_count'],
 'ledger_usd':ledger,'week_record_usd':one(c,'SELECT SUM(actual) v FROM output_records WHERE project_id=1')['v']}
(DATA/'crosscheck.json').write_text(json.dumps({'rows':rows,'extra':extra,'all_consistent':all(r['consistent'] for r in rows),'three_distinct_pages_for_every_metric':False,'boundary':'部分指标现有UI只提供两个完整展示位；未改变业务界面或假称三个页面展示。'},ensure_ascii=False,indent=2),encoding='utf-8')
(DATA/'crosscheck_readback.json').write_text(json.dumps(read,ensure_ascii=False,indent=2),encoding='utf-8')
c.close();s.close();print(json.dumps({'checks':len(rows),'inconsistent':[r['metric'] for r in rows if not r['consistent']],'extra':extra},ensure_ascii=False,indent=2))
