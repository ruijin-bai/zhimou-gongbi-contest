"""Rebuild only data_codex/zhimou.db. Fixed seed 20261011. No external AI.
Run: python -X utf8 tools/codex_sim/generate.py
The rebuild preserves previous versions as local backups, and never opens other DBs.
"""
import sys, os, math, random, hashlib, json, sqlite3, shutil
from datetime import date, timedelta
from pathlib import Path
from runtime import configure, ROOT, DATA
configure()
sys.path.insert(0,str(ROOT))
from app.db import init_db, connect, insert, dumps, loads, one, all_
from images import scene, document
import time_machine

SEED=20261011
START=date(2026,1,5)
CUT=date(2026,10,4)
DEMO=date(2026,10,11)
END=date(2027,3,4)
STAMP='2026-10-04T18:00:00+01:00'
PASSWORD='SimRoad2026!'
COMPANIES=['远辰路基公司','星湾桥梁公司','青岑路面公司']
ROSTER=[
 ('sim_zhao','林望舒','tech',COMPANIES[0]),('sim_safe_a','孟泽安','sub_safety',COMPANIES[0]),
 ('sim_eng','苏景澄','eng_sup','澄岳国际工程'),('sim_safety','沈予宁','safety_sup','澄岳国际工程'),
 ('sim_pm','陆知远','manager','澄岳国际工程'),('sim_leader','顾清和','leader','澄岳国际工程'),
 ('sim_admin','程砚秋','admin','澄岳国际工程'),
 ('sim_bridge','许庭舟','tech',COMPANIES[1]),('sim_pave','宋雨棠','tech',COMPANIES[2]),
 ('sim_safe_b','贺星遥','sub_safety',COMPANIES[1]),('sim_safe_c','唐若川','sub_safety',COMPANIES[2]),
 ('sim_survey','季明初','tech',COMPANIES[0]),('sim_quality','余青岚','eng_sup','澄岳国际工程'),
 ('sim_lab','温见山','tech',COMPANIES[1]),('sim_material','白云珂','tech',COMPANIES[2]),
 ('sim_worker1','陈书野','tech',COMPANIES[0]),('sim_worker2','江望尘','tech',COMPANIES[1]),
 ('sim_worker3','宁晓舟','tech',COMPANIES[2]),('sim_plant','林星沐','tech',COMPANIES[2]),
 ('sim_foreman','夏文笙','tech',COMPANIES[0]),('sim_qa','周序安','eng_sup','澄岳国际工程'),
]
PROSE={
 '路基':['分层摊铺后复核横坡，压实遍数按试验段工艺执行。','清除局部软弱土并换填级配料，施工缝采用台阶搭接。','雨后先排除积水，含水率复测符合控制区间后恢复碾压。','测量组完成中线与高程复核，边坡修整同步推进。','压实试验点覆盖轮迹与边缘，低密实区域已补压复验。'],
 '中桥':['钻孔清孔后复核孔深与沉渣，监理见证钢筋笼下放。','承台钢筋连接与保护层垫块已自检，浇筑前复核预埋件。','墩身模板拼缝封堵完成，作业平台临边防护同步验收。','预制梁台座标高复测完成，混凝土试件按同条件组留置。','支座垫石表面凿毛清洁，安装前复核中心线与标高。'],
 '小桥':['基础持力层经复核后封闭，避免降雨扰动。','桥台背墙浇筑分层振捣，重点控制接缝与预留孔。','台背回填采用小型夯实设备，结构两侧对称施工。','桥面铺装厚度与排水坡度复核，伸缩缝槽口保持清洁。','盖板吊装前检查吊点与索具，交通导改人员到位。'],
 '涵洞':['基底验槽后及时铺设垫层，排水沟保持畅通。','箱涵钢筋定位完成，预留洞口与止水带固定复核。','涵身混凝土分层浇筑并连续养护，接口避免冷缝。','洞口翼墙砌筑同步校核线形，砂浆饱满度抽查。','涵背两侧对称填筑，覆盖厚度满足后再开放施工便道。'],
 '路面':['基层摊铺采用双机梯队，碾压区温湿条件持续记录。','混合料到场逐车测温，摊铺机保持稳定行驶。','纵向接缝先切齐再涂粘层，避免污染已完成面。','试验段确定松铺系数后分段实施，厚度点位留痕。','雨前及时收面，湿基层不喷洒透层油。'],
 '交安':['护栏立柱位置避让排水设施，施工前复核埋深。','标志基础定位与预埋螺栓方向经测量复核。','导改区临时标志保持连续，夜间反光设施已检查。','标线试划区先做表面清洁，玻璃珠撒布均匀。','路侧交通防护与永久设施分阶段转换，交叉作业专人指挥。']}
GROUPS=[('EAR','路基','m3',28000,8.5,1,'2026-01-05','2026-11-15',.89),
 ('MBR','中桥','m3',950,190,8,'2026-01-26','2026-12-20',.76),
 ('SBR','小桥','m3',350,175,8,'2026-01-12','2026-09-13',1.0),
 ('CUL','涵洞','m3',520,140,1,'2026-02-02','2026-11-08',.80),
 ('PAV','路面','m2',32000,12.8,9,'2026-06-15','2027-02-14',.28),
 ('TRF','交安','m',2600,24,9,'2026-09-14','2027-03-04',.05)]

def ts(day,h=9): return f'{day}T{h:02d}:00:00+01:00'
def put(table,**kw): return insert(c,table,kw)
def jput(table,**kw): return put(table,**{k:dumps(v) if isinstance(v,(dict,list)) else v for k,v in kw.items()})
def knowledge(kind,data,pid=1,key='latest'):
    return jput('knowledge',project_id=pid,kind=kind,key=key,value_json=data,updated_at=STAMP)
def password_hash(i):
    salt=hashlib.sha256(f'codex-sim-user-{i}'.encode()).digest()[:16]
    dg=hashlib.pbkdf2_hmac('sha256',PASSWORD.encode(),salt,240000).hex()
    return f'pbkdf2$240000${salt.hex()}${dg}'

def reset_own():
    target=DATA/'zhimou.db'
    assert target.resolve().parent==DATA.resolve() and DATA.name=='data_codex'
    if target.exists():
        # Refuse rebuild against a running 8663 instance; SQLite backup before reset.
        import socket
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1',8663))==0:
                raise SystemExit('Stop the isolated 8663 service before rebuilding.')
        backup=DATA/'backups'; backup.mkdir(exist_ok=True)
        old=sqlite3.connect(target)
        digest=hashlib.sha256(target.read_bytes()).hexdigest()[:12]
        dest=sqlite3.connect(backup/f'before-{digest}.db')
        old.backup(dest); dest.close(); old.close()
        target.unlink()
        for ext in ('-wal','-shm'):
            p=Path(str(target)+ext)
            if p.exists(): p.unlink()
    (DATA/'.gitignore').write_text('*\n!.gitignore\n',encoding='utf-8',newline='\n')
    (DATA/'isolated.env').write_text('# Isolated authored simulation; no credentials.\n',encoding='utf-8')

def project_users():
    platform={'source':'仿真主数据底稿（非真实平台接入）','stage':'活跃在建','stage_code':'02',
      'master_code':'PJSIM20260001','foreign_name':'Synthetic Road Project','country':'加纳','region':'非洲',
      'category_code':'09','implementation_code':'03','construction_nature_code':'01',
      'manager':'陆知远','actual_start':'2026-01-05','expected_start':'2026-01-05',
      'expected_handover':'2027-03-04','actual_handover':None,'completion_date':None,
      'content':'10 km 双向四车道，路基、中桥1座(3×25 m)、小桥1座(1×16 m)、箱涵5道、路面及交安',
      'employer':'晨溪交通发展局','engineer':'雁桥工程咨询','contractor':'澄岳国际工程',
      'supplier':'晨洲建材','longitude':None,'latitude':None}
    cfg={'simulation':True,'cadence':'weekly','workdays_per_week':6,'daily_auto':False,'platform':platform,
      'features':{'gongbi':True,'conceal':True,'tickets':True,'gate':True,'talk':True},
      'thresholds':{'red_days':10,'orange_days':5,'yellow_days':2,'stale_days':3}}
    jput('projects',id=1,name='仿真公路项目',code='SIM-ROAD',config_json=cfg,created_at=ts(START))
    for i,(username,name,role,company) in enumerate(ROSTER,1):
        scope={'远辰路基公司':'EAR,CUL','星湾桥梁公司':'MBR,SBR','青岑路面公司':'PAV,TRF'}.get(company,'')
        put('users',id=i,username=username,name=name,role=role,company=company,wbs_scope=scope,password_hash=password_hash(i),project_id=1,created_at=ts(START))
    put('ai_settings',k='paused',v='1')
    return cfg

def quantities():
    days=[START+timedelta(days=i) for i in range((CUT-START).days+1)]
    items=[]; daily={d.isoformat():[] for d in days}
    for gi,(prefix,group,unit,baseqty,price,author,ds,de,fraction) in enumerate(GROUPS):
        for k in range(20):
            seg=k//4; typ=k%4
            part=(f'K{seg*2:02d}+000—K{(seg+1)*2:02d}+000' if group in ('路基','路面','交安') else
                  '中桥 K6+450' if group=='中桥' else '小桥 K2+180' if group=='小桥' else f'箱涵 C{seg+1:02d}')
            detail={'路基':['挖方及整形','选料填筑','软基换填','边坡及排水'],
                    '中桥':['钻孔灌注桩','承台及系梁','墩台及盖梁','预制梁及桥面'],
                    '小桥':['扩大基础','桥台及背墙','盖板及桥面','台背回填'],
                    '涵洞':['基底及垫层','涵身混凝土','翼墙及洞口','涵背及防护'],
                    '路面':['级配碎石底基层','水稳基层','沥青下面层','沥青上面层'],
                    '交安':['护栏及端头','标志及立柱','标线及道钉','隔离及导改']}[group][typ]
            code=f'{prefix}{k+1:03d}'
            q=round(baseqty*(.8+.05*(k%9)),2)
            rate=round(price*(.8+.12*typ),2)
            # Items represent separately measured work fronts. Bridge superstructure follows substructure.
            frac=max(0,min(1,fraction+(.06*(1.5-typ) if fraction<1 else 0)+.012*(seg-2)))
            if group=='中桥': frac=[1,.94,.70,.32][typ]-.01*seg
            if group=='路面': frac=[.72,.43,.14,0][typ]-.005*seg if typ<3 else 0
            if group=='交安': frac=[.16,.035,0,0][typ]
            frac=max(0,min(1,frac))
            begin=date.fromisoformat(ds)+timedelta(days=typ*(15 if group in ('中桥','小桥','涵洞') else 7)+seg*2)
            finish=date.fromisoformat(de)+timedelta(days=seg*2-8)
            if group=='中桥': begin=[date(2026,1,26),date(2026,3,2),date(2026,4,13),date(2026,7,20)][typ]+timedelta(days=seg*4)
            if group=='路面': begin=[date(2026,6,15),date(2026,7,20),date(2026,9,7),date(2026,11,2)][typ]+timedelta(days=seg*3)
            if group=='交安': begin=date(2026,9,14)+timedelta(days=typ*21+seg*2)
            eligible=[d for d in days if d>=begin]
            weights=[]
            for d in eligible:
                rain=.53 if d.month in (5,6,7) else .79 if d.month==8 else 1.25 if d.month==9 else .90
                ramp=.3+.7*min(1,(d-begin).days/35)
                variation=1+.12*math.sin(d.toordinal()*.43+k)
                weights.append(rain*ramp*variation*(.38 if d.weekday()==6 else 1))
            completed=round(q*frac,4)
            amounts=[]; sofar=0.0; money_sofar=0.0
            for n,d in enumerate(eligible):
                amount=round(completed*weights[n]/sum(weights),6) if weights else 0
                # Every source line carries an exact USD-cent value. This prevents
                # sum(round(week)) from disagreeing with round(sum(all quantities)).
                money=round(amount*rate,2)
                if n==len(eligible)-1: money=round(round(completed*rate,2)-money_sofar,2)
                amount=money/rate
                money_sofar=round(money_sofar+money,2)
                sofar+=amount
                if amount>0:
                    daily[d.isoformat()].append({'activity_code':code,'qty':amount,'unit':unit,'pct':round(sofar/q*100,4),'part':part,'process':detail,'author':author,'group':group,'note':'测量或小票底稿，经周确认核定','sources':['V']})
            item={'code':code,'name':f'{part} {detail}','group':group,'part':part,'unit':unit,'qty':q,'price':rate,'fraction':frac,'author':author,'begin':begin,'finish':finish,'daily':dict(zip([d.isoformat() for d in eligible], [r for r in []]))}
            items.append(item)
    return items,daily

def boq(items,cfg):
    for a in items:
        bid=put('boq_items',project_id=1,code='B-'+a['code'],name=a['name'],unit=a['unit'],unit_price=a['price'],currency='USD',contract_qty=a['qty'])
        put('boq_map',project_id=1,activity_code=a['code'],boq_id=bid,factor=1,status='confirmed',reason='仿真测量分项与P6作业一对一核对',confirmed_by=3,updated_at=STAMP)
    total=round(sum(a['qty']*a['price'] for a in items),2)
    cfg['output_plan']={'boq_total':total,'year_plan':{'2026':round(total*.79,2),'2027':round(total*.21,2)},'currency':'USD','report_lag_days':0,'source':'仿真年度目标底稿；清单总产值来自 BOQ 单价×清单量'}
    cfg['standard_provenance']={'152':'年度营业额目标未仿真','153':'年度营业收入目标未仿真','154':'output_records.period_start 年份','155':'按月聚合，month_source.json','156':'美元计划除以10000；只作标准交换视图','157':'营业额未仿真','164':'media.purpose','165':'media.file','166':'media.taken_at','167':'media.original_name'}
    c.execute('UPDATE projects SET config_json=? WHERE id=1',(dumps(cfg),))
    return total

def xer(items,daily):
    pdir=DATA/'p6'; pdir.mkdir(exist_ok=True)
    periods=[START,date(2026,3,1),date(2026,5,3),date(2026,7,5),date(2026,8,2),date(2026,9,6),CUT]
    from app.services.p6 import import_schedule
    for pi,dd in enumerate(periods):
        lines=['ERMHDR\t23.12\t2026-10-04\tProject\tSynthetic programme']
        def table(name,fields,rows):
            lines.extend(['%T\t'+name,'%F\t'+'\t'.join(fields)])
            lines.extend('%R\t'+'\t'.join(str(x) if x is not None else '' for x in row) for row in rows)
        table('PROJECT',['proj_id','proj_short_name','proj_name','last_recalc_date'],[['1','SIM-ROAD','仿真公路项目',dd.isoformat()]])
        table('CALENDAR',['clndr_id','clndr_name','day_hr_cnt','week_hr_cnt','default_flag'],[['1','六天工作制',8,48,'Y']])
        table('PROJWBS',['wbs_id','parent_wbs_id','proj_id','wbs_name','wbs_short_name','proj_node_flag'],[['1','','1','仿真公路项目','SIM','Y']]+[[str(g+2),'1','1',GROUPS[g][1],GROUPS[g][0],'N'] for g in range(6)])
        fields=['task_id','proj_id','wbs_id','clndr_id','task_code','task_name','task_type','status_code','target_start_date','target_end_date','early_start_date','early_end_date','late_start_date','late_end_date','act_start_date','act_end_date','target_drtn_hr_cnt','remain_drtn_hr_cnt','total_float_hr_cnt','phys_complete_pct','complete_pct_type','driving_path_flag']
        rows=[]
        bypct={}
        for day,vals in daily.items():
            if day<=dd.isoformat():
                for v in vals: bypct[v['activity_code']]=v['pct']
        for n,a in enumerate(items,1):
            pct=bypct.get(a['code'],0) if pi else 0
            slip=0 if pi==0 else (18 if pi==3 else 14 if pi==4 else 9 if pi==5 else 6 if pi==6 else 4)
            # Wet season changes controlling path from earthworks to the bridge, then pavement.
            critical=(a['group']=='路基' and pi<3) or (a['group']=='中桥' and pi in (3,4)) or (a['group']=='路面' and pi>=5)
            finish=a['finish']+timedelta(days=slip if a['group'] in ('路基','中桥','路面') else 0)
            dur=max(1,(a['finish']-a['begin']).days*6/7)
            status='TK_Complete' if pct>=99.999 else 'TK_Active' if pct>0 else 'TK_NotStart'
            actual_end=dd.isoformat() if status=='TK_Complete' else ''
            rows.append([n,1,2+GROUPS.index(next(g for g in GROUPS if g[1]==a['group'])),1,a['code'],a['name'],'TT_Task',status,a['begin'],a['finish'],a['begin'],finish,a['begin'],finish,a['begin'] if pct else '',actual_end,round(dur*8,2),round(dur*(1-pct/100)*8,2),-slip*8 if critical else (10+n%14)*8,pct,'CP_Phys','Y' if critical else 'N'])
        for mid,mname,mday in [(121,'中桥结构完成',date(2026,12,20)),(122,'全线交安完成',END),(123,'主线基层完成',date(2026,11,30))]:
            rows.append([mid,1,1,1,f'MS{mid}',mname,'TT_FinMile','TK_NotStart',mday,mday,mday,mday+timedelta(days=0 if pi==0 else 6),mday,mday,'','',0,0,0,0,'CP_Phys','Y'])
        table('TASK',fields,rows)
        rels=[]
        # Parallel, overlapping measured packages; documented SS links do not imply quantities add twice.
        for g in range(6):
            for seg in range(5):
                for typ in range(1,4):
                    pred=g*20+seg*4+typ; succ=pred+1
                    rels.append([succ,pred,'PR_SS',max(0,(items[succ-1]['begin']-items[pred-1]['begin']).days)*8])
        for k in range(20):
            rels.append([81+k,1+k,'PR_SS',160*8]); rels.append([101+k,81+k,'PR_SS',90*8])
        rels.extend([[121,24,'PR_FS',0],[121,28,'PR_FS',0],[123,82,'PR_FS',0],[122,104,'PR_FS',0],[122,108,'PR_FS',0]])
        table('TASKPRED',['task_id','pred_task_id','pred_type','lag_hr_cnt'],rels)
        raw=('\n'.join(lines)+'\n%E\n').encode('utf-8')
        filename=('baseline' if pi==0 else f'update-{pi}')+'-'+dd.isoformat()+'.xer'
        (pdir/filename).write_bytes(raw)
        imp=import_schedule(c,1,3,filename,raw)
        c.execute('UPDATE p6_imports SET imported_at=? WHERE id=?',(ts(dd,19),imp['import_id']))
    return one(c,'SELECT * FROM p6_imports WHERE id=?',(imp['import_id'],))

def logs_notes(daily):
    ids={}; week_ids={}; note_ids=[]
    for day,vals in daily.items():
        d=date.fromisoformat(day)
        rain=d.month in (5,6,7) and d.day%4!=0
        for author in (1,8,9):
            rows=[v for v in vals if v['author']==author]
            # Four meaningful work fronts per daily record; quantity detail retained in daily source file.
            picks=rows[::max(1,len(rows)//4)][:4]
            works=[dict(part=v['part'],process=v['process'],desc=PROSE[v['group']][(d.toordinal()+i)%5],activity_code=v['activity_code'],sources=['V']) for i,v in enumerate(picks)]
            issues=[]
            if rain and author==1:
                issues=[dict(type='施工组织',severity='中',desc=['雨季路床含水率偏高，开放面暂缓碾压。','便道运输窗口受限，已组织清沟排水。','雨季有效作业窗口缩短，排水完成后复核含水率。','雨后软弱点位已标识，先处理再恢复填筑。','湿基层暂不摊铺，班组转入材料检查与机具保养。'][d.day%5],measure='调整至排水、机具保养与检验，恢复前复测',activity_code='EAR001',sources=['V'])]
            if d.month==9 and d.day%6==0 and author==9:
                issues=[dict(type='施工组织',severity='中',desc='水稳拌合料运输周转衔接不顺，末班摊铺窗口受限。',measure='分时调车并提前锁定次日骨料供料',activity_code='PAV002',sources=['V'])]
            content={'summary':('雨后排水及复测，按可用工作面组织施工。' if rain else '按已放行工作面组织分段施工与检验。'),
              'works':works or [{'part':'施工准备区','process':'施工准备','desc':'检查运输便道、机具与测量控制点，尚未形成正式完成量。','activity_code':'','sources':['V']}],
              'progress':[{k:v[k] for k in ('activity_code','qty','unit','pct','note','sources')} for v in rows],
              'labor':[{'trade':'机械操作手','count':5+author%3,'sources':['V']},{'trade':'钢筋及模板工' if author==8 else '路基及摊铺工','count':12+author%4,'sources':['V']}],
              'machinery':[{'name':'旋挖钻机' if author==8 else '压路机','count':2,'status':'正常','sources':['V']},{'name':'运输车','count':4+author%3,'status':'正常','sources':['V']}],
              'materials':[],'issues':issues,'tomorrow':[{'desc':'复核已放行作业条件，推进连续工作面。','activity_code':picks[0]['activity_code'] if picks else '', 'sources':['PLAN']}],
              'photo_notes':[],'notes':'','uncertain':[]}
            lid=jput('logs',project_id=1,author_id=author,log_date=day,mode='ai',period='day',auto=1,status='approved',weather='阵雨' if rain else '晴间多云',voice_text='；'.join(w['desc'] for w in works),content_json=content,ai_draft_json=content,started_at=ts(day,8),drafted_at=ts(day,18),submitted_at=ts(day,19),reviewed_by=3,reviewed_at=ts(day,20),review_note='仿真人工核对记录：测量数量与施工描述一致',checks_json=[],qa_json=[])
            ids[(day,author)]=lid
        if d.weekday() in (1,3,6):
            for author in (1,8,9):
                group={1:'路基',8:'中桥',9:'路面'}[author]
                text=PROSE[group][(d.toordinal()+author)%5]
                voice=(d.day+author)%3==0
                nid=jput('notes',project_id=1,author_id=author,note_date=day,text=text,source='voice' if voice else 'text',seconds=22.4 if voice else None,geo_place='中桥 K6+450' if author==8 else 'K04+000 工作面',created_at=ts(day,11))
                note_ids.append(nid)
                if voice:
                    put('voice_pending',project_id=1,user_id=author,client_id=f'voice-{nid}',seconds=22.4,recorded_at=ts(day,10),received_at=ts(day,11),status='done',tries=1,note_id=nid,updated_at=ts(day,11))
        if d.weekday()==6:
            monday=d-timedelta(days=6)
            for author in (1,8,9):
                ag={}; byday=[]
                for n in range(7):
                    wd=(monday+timedelta(days=n)).isoformat()
                    for v in daily.get(wd,[]):
                        if v['author']!=author: continue
                        row=ag.setdefault(v['activity_code'],{**v,'qty':0})
                        row['qty']+=v['qty']; row['pct']=v['pct']
                        byday.append((wd,v))
                progress=[{k:v[k] for k in ('activity_code','qty','unit','pct','note','sources')} for v in ag.values()]
                lid=jput('logs',project_id=1,author_id=author,log_date=monday.isoformat(),period='week',period_end=day,status='approved',mode='ai',content_json={'summary':'按每日测量及小票底稿逐项核对，周完成量核定。','works':[],'progress':progress,'labor':[],'machinery':[],'materials':[],'issues':[],'tomorrow':[],'uncertain':[]},started_at=ts(day,15),submitted_at=ts(day,17),reviewed_by=3,reviewed_at=ts(day,18),review_note='逐项复核，来源为七日日报，避免日周重复计量')
                week_ids[(monday.isoformat(),author)]=lid
                for wd,v in byday:
                    put('log_progress',log_id=lid,project_id=1,log_date=wd,activity_code=v['activity_code'],qty=v['qty'],unit=v['unit'],pct=v['pct'],note=f"日报 L{ids[(wd,author)]} → 周确认 L{lid}")
                for v in list(ag.values())[:4]:
                    jput('review_fields',project_id=1,kind='week',ref_type='log',ref_id=lid,field_key=v['activity_code'],label=v['process'],ai_value=v['qty'],final_value=v['qty'],decision='confirm',reason='与七日日报测量底稿合计一致',decided_by=author,decided_at=ts(day,17))
    (DATA/'daily_quantity_source.json').write_text(dumps(daily),encoding='utf-8')
    return ids,week_ids,note_ids

def pictures(ids,note_ids):
    md=DATA/'media'/'synthetic'; md.mkdir(parents=True,exist_ok=True)
    medias=[]
    for i in range(1,106):
        doc=i>84
        kind=(['混凝土小票','进场单','合格证','检测报告'][(i-85)//6%4] if doc else ['桩基','材料','施工过程','隐患','整改','路面','桥梁'][((i-1)//12)%7])
        day=(date(2026,8,3)+timedelta(days=(i*7)%63)).isoformat()
        part='中桥 K6+450' if kind in ('桩基','桥梁','混凝土小票','整改','隐患') else 'K04+000—K06+000'
        rel=f'synthetic/{i:03d}.jpg'; path=DATA/'media'/rel
        (document if doc else scene)(path,i,kind,day,part)
        purpose='ticket' if doc else 'safety' if kind=='隐患' else 'rectify' if kind=='整改' else 'conceal' if kind=='桩基' else 'log'
        uploader=2 if purpose=='safety' else 1 if purpose=='rectify' else 8 if kind in ('桩基','桥梁','混凝土小票') else 9
        mid=put('media',project_id=1,uploader_id=uploader,log_id=ids.get((day,uploader if uploader in (1,8,9) else 1)) if purpose=='log' else None,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),file=rel,ai_file=rel,thumb=rel,original_name=f'{kind} {part} SIM-{i:03d}.jpg',width=1200,height=900,taken_at=ts(day),purpose=purpose,safety_status='done' if purpose=='safety' else 'na',uploaded_at=ts(day,10),geo_place=part)
        medias.append(mid)
        if i<=30:
            nid=note_ids[-i]
            c.execute('UPDATE notes SET media_json=? WHERE id=?',(dumps([mid]),nid))
    return medias

def safety_quality(medias):
    kinds=['临边防护','临时用电','起重吊装','设备作业','交通导改','高处作业']
    descs=['基坑边缘防护缺口未封闭，人员通行存在坠落风险。','配电箱进线防水弯缺失，雨后需检查漏电保护。','吊装警戒范围与通行便道交叠，须增设隔离与指挥。','压路机倒车区域有人员穿行，视线遮挡点未设监护。','导改区反光锥连续性不足，夜间车辆容易误入工作面。','作业平台踢脚板局部松动，工具可能坠落至下方通道。']
    fids=[]
    for i in range(42):
        category=kinds[i%6]; created=(date(2026,8,12)+timedelta(days=i%54)).isoformat()
        if i>=34: created='2026-10-03' if i%2 else '2026-10-04'
        candidate=i>=36
        status='open' if candidate else 'resolved' if i<25 else 'rectifying' if i<30 else 'open'
        confirmed=None if candidate else ts(created,11)
        closed=(date.fromisoformat(created)+timedelta(days=2+i%3)).isoformat() if status=='resolved' else None
        due=min(CUT,date.fromisoformat(created)+timedelta(days=3)).isoformat()
        def hazard_photo(category,day,uid):
            idx=106+i*2+(1 if category=='整改' else 0)
            rel=f'synthetic/{idx:03d}.jpg'; path=DATA/'media'/rel
            scene(path,idx,category,day,'中桥 K6+450' if i%2 else 'K04+000 工作面')
            return put('media',project_id=1,uploader_id=uid,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),file=rel,ai_file=rel,thumb=rel,original_name=f'{category} SIM-H{i+1:03d}.jpg',width=1200,height=900,taken_at=ts(day,12 if category=='整改' else 9),uploaded_at=ts(day,13 if category=='整改' else 10),purpose='rectify' if category=='整改' else 'safety',safety_status='done',geo_place='中桥 K6+450' if i%2 else 'K04+000 工作面')
        mid=hazard_photo('隐患',created,2)
        after=hazard_photo('整改',closed or '2026-10-04',1) if status in ('resolved','rectifying') else None
        body={'category':category,'part':'中桥 K6+450' if i%2 else 'K04+000 工作面','description':descs[i%6],
          'risk':descs[i%6],'desc':descs[i%6],'measure':['先封闭缺口并补设双道栏杆，复核人员通道。','完善防水弯并试验漏保，拍摄箱内接线与防雨罩。','调整隔离区，吊装前完成专项交底与索具检查。','设倒车监护与人车分流，补齐盲区警示标识。','补齐反光锥和爆闪灯，夜间复查视认距离。','固定踢脚板并清理散落工具，复核安全绳挂点。'][i%6],
          'media_ids':[mid],'simulation_provenance':'预写仿真照片识别候选及人工决定','repeat_of':fids[i-6] if i>=6 and i%6==0 else None}
        fid=jput('findings',project_id=1,agent='safety',kind='hazard',level='high' if i%7==0 else 'medium' if i%3 else 'low',title=f"{body['part']} {category}复查" if i%6==0 and i>0 else f"{body['part']} {category}缺项",body_json=body,ref_type='media',ref_id=mid,activity_code='MBR003' if i%2 else 'EAR001',fingerprint=f'sim-hazard-{i}',status=status,assignee_id=None if candidate else (1 if i%2==0 else 8),due_date=None if candidate else due,rectify_media_id=after,verify_json={'verdict':'pass' if status=='resolved' else 'pending','note':'防护已连续设置，整改照片与现场复查一致。' if status=='resolved' else '整改影像已提交，待安全主管复核。','suggestion':'检查整改部位与原隐患位置对应关系'} if after else None,confirmed_by=None if candidate else 4,confirmed_at=confirmed,feedback_by=4 if closed else None,feedback_at=ts(closed,16) if closed else None,feedback_note='安全主管完成复核' if closed else '',created_at=ts(created),updated_at=ts(closed or created,16),resolved_at=ts(closed,16) if closed else None)
        fids.append(fid)
        if confirmed:
            put('audit',user_id=4,action='findings.confirm',target=f'finding:{fid}',detail='仿真人工确认并派发',created_at=confirmed)
            if after: put('audit',user_id=1,action='findings.rectify',target=f'finding:{fid}',detail=f'提交整改照片 M{after}',created_at=ts(closed or '2026-10-04',13))
            if closed: put('audit',user_id=4,action='findings.resolve',target=f'finding:{fid}',detail='人工复核后闭环',created_at=ts(closed,16))
    for i in range(15):
        day=(date(2026,9,12)+timedelta(days=i)).isoformat()
        jput('findings',project_id=1,agent='quality',kind='qc_reminder' if i%3==0 else 'quality_issue',level='medium',title=['桥台钢筋保护层复测','路基压实点位复验','基层厚度抽检复核','箱涵止水带固定复核','沥青接缝平整度复测'][i%5],body_json={'part':'中桥 K6+450' if i%2 else 'K04+000 工作面','category':'质量检查','description':'实测点位已登记，局部偏差由施工班组调整后复验。','requirement':'提供复验记录与影像，未经复核不进入下一工序。'},ref_type='log',ref_id=1,activity_code='MBR003' if i%2 else 'PAV002',status='resolved' if i<10 else 'open',assignee_id=8 if i%2 else 9,due_date='2026-10-04',confirmed_by=3,confirmed_at=ts(day,11),resolved_at=ts(day,17) if i<10 else None,created_at=ts(day),updated_at=ts(day,17))
    return fids

def materials_conceal():
    from app.services import conceal
    templates={t['key']:t for t in conceal.BUILTIN}
    for i in range(24):
        day=(date(2026,8,1)+timedelta(days=i*2)).isoformat()
        typ=['pile','rebar','formwork','foundation_pit'][i%4]
        tpl=templates[typ]
        status=['passed','passed','passed','submitted','ready','rejected'][i%6]
        fields=({'pile_no':f'P{i+1:02d}','depth':24.5+i%3,'dia':1200,'sediment':35,'pos_dev':12} if typ=='pile' else {'spec':'HRB400 D25','spacing':150,'cover':50,'lap_len':1000} if typ=='rebar' else {'dim':1200,'vertical':3,'joint_gap':2,'support':'钢管支架，横距0.9m'} if typ=='formwork' else {'length':12,'width':8,'elev':34.56,'strata':'砂质黏土；仿真监理验槽确认','water':'排水沟及集水坑运行'})
        fields={key:{'value':value,'source':'人工','confirmed':True} for key,value in fields.items()}
        photos=[{'media_id':(i*3+j)%12+1,'shot':s['key'],'shot_src':'人工','source':'仿真影像','caption':s['label']} for j,s in enumerate(tpl['shots'])]
        jput('conceal_records',project_id=1,activity_code=f'MBR{(i%20)+1:03d}',part='中桥 K6+450',template_key=typ,title=f'{tpl["name"]} · 检验批 SIM-Q{i+1:03d}',status=status,inspection_date=day,fields_json=fields,photos_json=photos,ai_json={'fields':[],'summary':'实测与影像逐项核对，签认以人工决定为准。'},missing_json={'shots':[],'items':[]},supervisor_name='雁桥工程咨询 · 齐沐远',submitted_at=ts(day,13) if status in ('passed','submitted','rejected') else None,result_note='实测指标符合仿真设计及工艺要求，允许下道工序。' if status=='passed' else '补充接头位置近景及垫块检查记录。' if status=='rejected' else '',result_at=ts(day,16) if status in ('passed','rejected') else None,created_by=8,created_at=ts(day),narration='钢筋、尺寸、保护层及关键隐蔽部位已逐项检查。',ready_note='逐项核对资料与影像',ready_by=8,ready_at=ts(day,12),submitted_by=8,result_by=3 if status in ('passed','rejected') else None,template_json=tpl,history_json=[{'action':'create','name':'许庭舟','at':ts(day)},{'action':'submit','name':'许庭舟','at':ts(day,13)},{'action':status,'name':'苏景澄','at':ts(day,16)}],updated_at=ts(day,16))
    for i in range(18):
        day=(date(2026,8,5)+timedelta(days=i*3)).isoformat()
        category=['rebar','cement','stone','sand','admixture','other'][i%6]
        batch=put('material_batches',project_id=1,category=category,name={'rebar':'钢筋','cement':'水泥','stone':'碎石','sand':'河砂','admixture':'减水剂','other':'支座'}[category],spec='HRB400 D25' if category=='rebar' else '工程级材料',supplier='晨洲建材',batch_no=f'SIM-B{i+1:03d}',qty=24,unit='t',arrived_at=day,retest_status='pending' if i==17 else 'sampled' if i==16 else 'fail' if i==15 else 'pass',report_no=f'SIM-LAB-{i+1:03d}' if i<16 else None,reported_at=day if i<16 else None,retest_note='抽样按批次登记；不合格批次已隔离' if i==15 else '仿真试验报告结果录入',sampled_at=day,created_by=13,created_at=ts(day),updated_by=13,updated_at=ts(day,17))
        fields={'ticket_no':f'SIM-R{i+1:03d}','supplier':'晨洲建材','ticket_date':day,'material':'钢筋及工程材料','batch_no':f'SIM-B{i+1:03d}','qty':24,'unit':'t'}
        jput('tickets',project_id=1,doc_kind='material_receipt' if i%2==0 else 'cert',status='confirmed' if i<16 else 'draft',ai_status='done',ai_json=fields,confirmed_json=fields if i<16 else None,ticket_no=fields['ticket_no'],supplier='晨洲建材',ticket_date=day,material=fields['material'],category=category,qty=24,unit='t',batch_no=fields['batch_no'],batch_id=batch,media_ids_json=[91+i%15],provider='authored',uploaded_by=8,confirmed_by=3 if i<16 else None,confirmed_at=ts(day,13) if i<16 else None,created_at=ts(day),updated_at=ts(day,13))
    for i in range(12):
        day=(date(2026,8,17)+timedelta(days=i*3)).isoformat()
        pour=put('pours',project_id=1,part=f'中桥 P{i+1:02d}承台',activity_code='MBR002',grade='C35',design_qty=32,pour_date=day,status='done',note='浇筑前验收、连续供料与试件留置已登记',created_by=8,created_at=ts(day),updated_at=ts(day,16))
        for truck in range(4):
            f={'ticket_no':f'SIM-C{i+1:02d}-{truck+1}','supplier':'晨洲建材','vehicle':f'SYN-{i+1:02d}{truck+1}','ticket_date':day,'part':f'中桥 P{i+1:02d}承台','grade':'C35','qty':8.0,'unit':'m3','slump':160+truck*5,'temp':29+truck%2,'receiver':'许庭舟'}
            tid=jput('tickets',project_id=1,doc_kind='concrete_ticket',status='confirmed',ai_status='done',ai_json=f,confirmed_json=f,**f,pour_id=pour,media_ids_json=[85+(i+truck)%6],provider='authored',uploaded_by=8,confirmed_by=8,confirmed_at=ts(day,12),created_at=ts(day),updated_at=ts(day,12))
            jput('review_fields',project_id=1,kind='ticket',ref_type='ticket',ref_id=tid,field_key='qty',label='方量',ai_value=8,final_value=8,decision='confirm',reason='原票据与收货记录核对一致',decided_by=8,decided_at=ts(day,12))
        dayd=date.fromisoformat(day); due7=dayd+timedelta(days=7); due28=dayd+timedelta(days=28)
        put('specimens',project_id=1,pour_id=pour,group_no=f'SIM-SP-{i+1:02d}',grade='C35',molded_date=day,due_7=due7.isoformat(),due_28=due28.isoformat(),r7_mpa=29.5+i*.3 if due7<=CUT else None,r7_report_no=f'SIM-7D-{i+1:02d}' if due7<=CUT else None,r7_tested_at=due7.isoformat() if due7<=CUT else None,r7_by=13 if due7<=CUT else None,r28_mpa=40.1+i*.2 if due28<=CUT else None,r28_report_no=f'SIM-28D-{i+1:02d}' if due28<=CUT else None,r28_tested_at=due28.isoformat() if due28<=CUT else None,r28_by=13 if due28<=CUT else None,created_by=8,created_at=ts(day))

def social_notes(note_ids):
    for i,nid in enumerate(note_ids[-48:]):
        day=(date(2026,9,1)+timedelta(days=i%34)).isoformat(); key=f'note:{nid}'
        for j,uid in enumerate((1,8,9)):
            put('feed_likes',project_id=1,post_key=key,user_id=uid,created_at=ts(day,13+j))
        for j,uid in enumerate((3,4)):
            put('feed_comments',project_id=1,post_key=key,user_id=uid,text=['请把复测点位与部位编号对应留存，便于下道工序交接。','已看到现场防护调整，班前交底继续覆盖新增作业人员。','运输与施工便道请错峰使用，避免吊装作业交叉。','试验结果按材料批次登记，暂不合格材料先隔离。','该做法可在同类工作面复用，注意雨后再次检查。'][(i+j)%5],created_at=ts(day,15+j))
        put('feed_reads',project_id=1,post_key=key,user_id=5,read_at=ts(day,17))
    notices=['雨季工作面恢复检查安排','中桥吊装窗口协调','材料送检与批次隔离要求','主线摊铺运输交通组织','夜间导改与反光设施复查','周例会与三周滚动计划核对']
    for i,title in enumerate(notices):
        nid=put('circle_notices',project_id=1,author_id=3,title=title,body=['雨后恢复前检查边坡、排水、便道承载和临电防水；有异常先隔离再处置。','吊装区设置专用警戒范围；运输车辆在等待区停放，服从现场指挥。','送检资料与进场批号一一对应；复试未完成不得进入永久结构。','拌合站、运输队与摊铺队按确认窗口组织，现场保留停机与到料记录。','临时交通设施按夜间视认要求复查，损坏锥桶及时替换。','例会前核对上期行动项的现场证据；未闭环项说明阻碍与下一步安排。'][i],pinned=1 if i==5 else 0,audience='all',status='published',created_at=ts('2026-10-04',10))
        for uid in (1,3,4,5,8,9): put('circle_notice_reads',notice_id=nid,user_id=uid,read_at=ts('2026-10-04',17))
    themes=['雨后路床恢复经验','中桥钢筋及模板接口','沥青接缝质量专题','施工便道交通组织','箱涵隐蔽验收资料','夜间临电与防护复查']
    for i,title in enumerate(themes):
        gid=put('talk_groups',project_id=1,kind=['topic','issue','special'][i%3],title=title,intro='集中讨论工艺、证据与工作面接口；办理结论回到原台账记录。',part='中桥 K6+450' if i%2 else 'K04+000 工作面',activity_code='MBR003' if i%2 else 'EAR001',author_id=3,status='closed' if i==1 else 'open',pinned=1 if i==0 else 0,closed_by=3 if i==1 else None,closed_at=ts('2026-10-04',17) if i==1 else None,close_note='接口问题已在例会行动项核对完成' if i==1 else '',created_at=ts('2026-09-07'),last_at=ts('2026-10-04',17))
        for j in range(5):
            pid=jput('talk_posts',project_id=1,group_id=gid,user_id=[1,8,9,3,4][j],text=PROSE[['路基','中桥','路面'][i%3]][j],media_json=[1+i*5+j],created_at=ts('2026-10-04',9+j))
            put('talk_comments',project_id=1,post_id=pid,user_id=3 if j!=3 else 8,text='对应工作面已核对，保留原记录与复验结果。',created_at=ts('2026-10-04',16))
            for uid in (1,4,8): put('talk_likes',post_id=pid,user_id=uid,created_at=ts('2026-10-04',17))
    for a,b in [(1,3),(3,8),(3,9),(2,4),(4,10),(4,11),(3,5),(4,5)]:
        tid=put('dm_threads',project_id=1,user_lo=min(a,b),user_hi=max(a,b),created_at=ts('2026-09-28'),last_at=ts('2026-10-04',16))
        for j in range(6):
            put('dm_messages',thread_id=tid,project_id=1,sender_id=a if j%2==0 else b,text=['请核对下周工作面接口，测量控制点资料已整理。','已收到，现场将按确认的放行条件安排。','雨后复测点位请附到原验收记录。','已补齐点位照片，待主管核对。','运输窗口已与现场指挥协调，避免交叉。','按会议行动项执行，完成后登记证据。'][j],created_at=ts('2026-10-04',10+j))
    token='SIMROAD2026WORKERQR001234567890'
    qid=put('quick_tokens',project_id=1,token=token,label='现场通道扫码',created_by=2,org='sub',company=COMPANIES[0],created_at=ts('2026-09-28'),expires_at='2027-01-01T23:59:00+01:00')
    for i in range(9):
        jput('quick_submissions',project_id=1,token_id=qid,receipt=f'SIM-QR-{i+1:03d}',client_id=f'sim-phone-{i}',reporter_name=ROSTER[15+i%3][1],reporter_team='现场施工班组',note=['通道侧反光锥倒伏，已提醒值班安全员。','材料堆场排水沟需清理，降雨后有局部积水。','桥下通行线与吊装区隔离需复查。'][i%3],media_json=[37+i],status='claimed' if i<6 else 'new',claimed_by=2 if i<6 else None,claimed_at=ts('2026-10-04',16) if i<6 else None,created_at=ts('2026-10-04',10+i%6))

def alerts_nudge(items,imp,fids):
    aids=[]
    from app.services import alerts
    for i in range(18):
        a=items[(80+i)%120] if i<8 else items[(20+i)%120]
        day=(date(2026,9,17)+timedelta(days=i)).isoformat()
        level=['red','orange','yellow'][i%3]
        detail={'slip_days':12-i%5,'remaining_days':60,'total_float_d':-6 if level=='red' else 4,'plan_pct':min(100,a['fraction']*100+8),'actual_pct':a['fraction']*100,'gap':-8,'data_date':'2026-10-04','as_of':day,'evidence':{'log_ids':[1,2]},'basis':'雨季运输受限与连续工作面调整'}
        aid=jput('alerts',project_id=1,import_id=imp['id'],fingerprint=f'codex-alert-{i}',activity_code=a['code'],activity_name=a['name'],level=level,rule='slip',title=f"{a['name']}：工作面衔接需协调",detail_json=detail,explanation_json={'summary':'雨季有效工作窗口减少，运输与验收接口影响连续施工。','causes':['便道排水与车辆周转限制','检验资料接口需提前复核'],'actions':['优先开放已复核工作面','落实运输窗口及备用设备'],'evidence':[{'ref':'L1','text':'施工记录已登记'}]},status='closed' if i<6 else 'confirmed' if i<13 else 'open',feedback_by=3 if i<13 else None,feedback_note='工程主管复核，已纳入滚动计划' if i<13 else '',feedback_at=ts(day,16) if i<13 else None,first_detected=ts(day),last_detected=ts(day,12),as_of=day)
        aids.append(aid)
        if i<13:
            put('alert_decisions',project_id=1,alert_id=aid,decision='adopt' if i%4 else 'defer',level_at=level,slip_at=detail['slip_days'],feedback_at=ts(day,16),plan_at='2026-10-04',note='优先落实材料与运输窗口，再复核剩余工期。',decided_by=5,decided_at=ts(day,17))
    for i in range(28):
        uid=[1,8,9,3,4,5,2][i%7]
        area='安全' if i%2 else '进度'
        finding=fids[25+i%11]
        route='finding' if area=='安全' else 'alert'
        ref=finding if area=='安全' else aids[6+i%12]
        notif=jput('notifications',project_id=1,user_id=uid,alert_id=ref if route=='alert' else None,title=f'{area}事项需要跟进',body='请在原台账核对办理依据、期限与下一步安排。',channels_json={'inapp':'sent'},created_at=ts('2026-10-04',10+i%8),read_at=ts('2026-10-04',18) if i<13 else None,link_json={'route':route,'params':{'id':ref}})
        put('nudge_log',project_id=1,obj_type='finding' if route=='finding' else 'alert',obj_key=str(ref),stage=1 if uid in (1,8,9,2) else 2,to_user_id=uid,status='sent',subject=f'{area}事项跟进',status_text='已提醒',owner_id=1 if area=='安全' else 8,area=area,link=f'/{route}?id={ref}',via='inapp',notif_id=notif,reason='仿真人工督办：按期限与责任人跟进',sent_at=ts('2026-10-04',10+i%8))
    for i,a in enumerate(items[-8:]):
        put('gate_class',project_id=1,activity_code=a['code'],act_name=a['name'],category='general',source='rule',updated_at=STAMP)
        put('gate_items',project_id=1,activity_code=a['code'],cond_key='sim_readiness',label='测量、材料与交通导改交底',custom=1,manual_state='blocked' if i==0 else 'pending' if i<3 else 'ready',manual_note='已核对开工作业条件' if i>=3 else '待补齐界面资料与交底签认',manual_by=3,manual_at=STAMP,owner_id=9,due_date='2026-10-04',updated_at=STAMP)
    return aids

def meetings():
    for i in range(6):
        day=(date(2026,8,30)+timedelta(days=7*i)).isoformat()
        action=[{'text':t,'owner_id':u,'owner_name':ROSTER[u-1][1],'company':ROSTER[u-1][3],'due_date':day} for t,u in [('复核雨后路床含水率与压实点位',1),('补齐中桥钢筋与预埋件检验照片',8),('协调拌合站运输窗口与摊铺界面',9),('复查临电防雨与交通隔离设施',2)]]
        mid=jput('meetings',project_id=1,title=f'周生产例会 · {day}',meeting_date=day,summary_json={'summary':'按进度、质量、安全核对工作面接口与上期行动项。','items':action,'decisions':['优先开放已放行工作面','未完成检验不进入下一工序']},source_text='例会核对雨季工作面恢复、桥梁验收接口、路面运输和现场防护。',created_by=3,created_at=ts(day,15),confirmed_at=ts(day,16))
        for j,a in enumerate(action):
            put('meeting_items',project_id=1,meeting_id=mid,src_idx=j,text=a['text'],owner_id=a['owner_id'],owner_name=a['owner_name'],owner_company=a['company'],due_date=day,status='done' if i<4 else 'open',created_by=3,created_at=ts(day,16),done_at=ts(day,18) if i<4 else None,done_by=a['owner_id'] if i<4 else None)

def output_records():
    from app.services import output
    weeks=[]
    monday=START
    while monday<=CUT:
        end=monday+timedelta(days=6)
        actual=output.compute(c,1,monday.isoformat(),min(end,CUT).isoformat())['total']
        # Plan is an independent prospective source. Actual is strictly a computed mirror, never an arbitrary curve.
        baseline=round(actual*(1.24 if monday.month in (5,6,7) else .87 if monday.month==9 else 1.04),2)
        planned=round(actual*(1.07 if monday.month in (5,6,7) else .98 if monday.month==9 else 1.02),2)
        put('output_records',project_id=1,period_start=monday.isoformat(),period_end=end.isoformat(),actual=actual,planned=planned,baseline=baseline,currency='USD',source='sim',note='实际 = 正式核定工程量×清单单价；原始量见周确认。计划来自仿真年/月计划底稿。')
        weeks.append({'week':monday.isoformat(),'actual_usd':actual,'baseline_usd':baseline,'updated_plan_usd':planned})
        monday+=timedelta(days=7)
    # Do not fabricate a post-cutoff P6 update or actual; next-period plan is forward-looking.
    put('output_records',project_id=1,period_start='2026-10-05',period_end='2026-10-11',planned=round(weeks[-1]['updated_plan_usd']*1.08,2),baseline=round(weeks[-1]['baseline_usd']*1.02,2),currency='USD',source='sim',note='截止日编制的下周计划；实际未发生，不补零')
    months=[]
    for month in range(1,11):
        s=date(2026,month,1); e=min(CUT,(s+timedelta(days=32)).replace(day=1)-timedelta(days=1))
        val=output.compute(c,1,s.isoformat(),e.isoformat())['total']
        months.append({'year':2026,'month':month,'through':e.isoformat(),'actual_usd':val,'actual_wan_usd':round(val/10000,6),'source':'log_progress × confirmed boq_map × boq_items','standard_156':'计划列独立于实际列'})
    (DATA/'month_source.json').write_text(dumps(months),encoding='utf-8')
    return weeks

def documents():
    from app.services import reports, gongbi_docs, gongbi_more, sub_letters, output
    from app.ai import report_agent, docs_registry as reg
    from app.services import schedule
    user=one(c,'SELECT * FROM users WHERE id=3')
    periods=[('week',(date(2026,8,30)+timedelta(days=7*i)).isoformat()) for i in range(6)]+[('month','2026-08-01'),('month','2026-09-01')]
    for idx,(kind,ref) in enumerate(periods):
        start,end=reports.period_for(kind,ref)
        f=reports.build_facts(c,1,start,end)
        tpl=report_agent.default_template(kind); f['template']=tpl
        nar={'overview':f"本期依据 {f['logs']['count']} 份日报核对施工事实。路基分层施工、中桥结构及路面工作面按放行条件推进。",
             'progress':['雨季工作窗口缩短，路基优先处理排水与复测，避免湿填料直接碾压。','桥梁结构与机具调配同步协调，检验资料完成后组织工序衔接。','路面运输窗口已纳入滚动计划，持续跟踪到料节奏与接缝质量。','现场按已开放工作面组织作业，受限区提前准备材料及测量依据。','赶工以工作面连续与设备周转为主，未通过检验的区域保持隔离。'][idx%5],
             'milestones':'节点预测以当前 P6 与正式完成量为依据，属于参考推演；主管核对接口与资源条件后执行。',
             'output':f"本期内部产值 {f['output']['total']:,.2f} USD，由正式核定工程量乘清单单价汇总。非业主计量认证。",
             'safety':f"本期发现已确认隐患 {f['safety']['found']} 项，其中已闭环 {f['safety']['resolved']} 项；未确认候选另行处置。",
             'quality':'隐蔽验收、材料复试与混凝土试件按批次留痕，局部问题调整后复验。',
             'issues':'运输周转、雨后排水及资料接口为主要约束；分别落实等待区、排水清理和检验资料核对。',
             'next_plan':'优先推进已放行作业，复核交通导改、材料供应与桥梁检验接口。',
             'coordination':['请监理提前协调桥梁隐蔽验收窗口。','分包在例会核对运输与交叉作业接口。']}
        rid=jput('reports',project_id=1,kind=kind,period_start=start,period_end=end,facts_json=f,narrative_json=nar,number_check_json=[],status='published',created_by=3,created_at=ts(end,20),gen_seconds=None)
        # Freeze the same ten-indicator source for Chinese reports.
        kp=output.kpi10(c,1,week=start if kind=='week' else None,today=date.fromisoformat(end)+timedelta(days=7))
        if kind=='week': knowledge('report_kpi10',{**kp,'kind':'snapshot'},key=str(rid))
        c.execute('UPDATE reports SET facts_json=? WHERE id=?',(dumps(f),rid))
        enkind='weekly_en' if kind=='week' else 'monthly_en'
        ef=gongbi_docs.period_facts(c,1,enkind,ref)
        en={'summary':f"This period's site record comprises {ef['logs']['count']} daily logs. Earthworks, bridge works and pavement interfaces were reviewed against the current programme.",
          'progress':'Wet-season access restrictions reduced productive windows. Drainage, moisture checks and work-front release were coordinated before resuming compaction.',
          'milestones':'Programme dates and forecast dates are reported separately. The forecast is a planning scenario requiring review of access and resources.',
          'safety':f"{ef['safety']['found']} confirmed hazards were recorded in this period; {ef['safety']['resolved']} were closed following human verification.",
          'quality':'Inspection records, material batch references and concrete specimens are maintained together. Unverified batches remain segregated.',
          'issues':'Transport turn-round and inspection interfaces require continued coordination between the site team and the Engineer.',
          'next_plan':'Prioritise released work fronts; maintain traffic separation and confirm material and inspection windows.',
          'coordination':["The Engineer is requested to coordinate inspection windows for concealed bridge works."],
          'resources':'Plant and workforce records are retained in the daily logs and summarised in the resource table.',
          'notices':'No contractual notices are simulated in this dataset.',
          'comparison':'Actual work quantities and programme forecasts are kept separate; recovery measures remain subject to site review.'}
        jput('gongbi_docs',project_id=1,kind=enkind,ref=start,title=f'仿真公路项目 — {reg.TYPES[enkind]["name"]} {start}',lang='en',period_start=start,period_end=end,facts_json=ef,narrative_json={s['key']:en.get(s['key'],'') for s in reg.TYPES[enkind]['sections']},check_json={'guard':[],'warnings':[]},source='authored',status='confirmed',created_by=3,created_at=ts(end,20),confirmed_by=5,confirmed_at=ts(end,21))
    # Five professional engineering letter subjects, in the existing two internal correspondence types.
    subjects=['雨后路床恢复与排水整改','中桥钢筋保护层与模板复验','箱涵隐蔽资料补充与报验','摊铺运输及交通导改协调','材料复试隔离与复验安排']
    subjects_en=['Drainage and release of earthwork fronts','Bridge reinforcement and formwork reinspection','Culvert inspection records and hold-point release','Paving transport and temporary traffic coordination','Material retesting and batch segregation']
    for i in range(10):
        kind='work_contact' if i<5 else 'letter_to_sub'; company=COMPANIES[i%3]
        # Facts and status originate in a real synthetic finding, not a contract event.
        finding=one(c,"SELECT * FROM findings WHERE confirmed_at IS NOT NULL AND status<>'resolved' ORDER BY id LIMIT 1 OFFSET ?",(i%11,))
        if kind=='work_contact':
            f=sub_letters.source_facts(c,1,f'finding:{finding["id"]}')
            company=f['company']
        else:
            company=COMPANIES[i%2]
            f=sub_letters.company_facts(c,1,f'{company}|safety')
        zh=['请先完成排水与含水率复测，符合工艺条件后恢复填筑。','请补齐保护层测点及接缝复验记录，再申请下一工序。','请将隐蔽照片、实测点位与报验部位编号对应归档。','请按已确认运输窗口组织，便道与作业区保持隔离。','请按批次隔离待复试材料，未经确认不得投入永久结构。'][i%5]
        en=['Please complete drainage and moisture checks before releasing the fill area.','Please submit cover measurements and joint reinspection records before the next operation.','Please reconcile inspection photographs and measurements with the submitted location reference.','Please maintain the agreed transport window and separation between haul routes and work zones.','Please segregate the batch until the retest result is confirmed; do not incorporate it into permanent works.'][i%5]
        nar={'subject':subjects[i%5]+' / '+subjects_en[i%5], 'fact':finding['title']+f" [S{finding['id']}]",'requirement':zh+'\n'+en,'deadline':'请按台账期限回复，并附可核对的现场记录。 / Reply by the recorded due date.','basis':'已确认现场问题与检验记录，不引用未仿真的合同条款。'}
        did=jput('gongbi_docs',project_id=1,kind=kind,ref=f'finding:{finding["id"]}' if kind=='work_contact' else f'{company}|safety',title=subjects[i%5]+'（中英）',lang='zh',facts_json=f,narrative_json=nar,check_json={'guard':[],'warnings':[]},source='authored',status='confirmed',created_by=3,created_at=ts('2026-09-28',10),confirmed_by=3 if kind=='work_contact' else 5,confirmed_at=ts('2026-09-28',11))
        uid=1 if company==COMPANIES[0] else 8 if company==COMPANIES[1] else 9
        status=['closed','replied','acked','issued','returned'][i%5]
        jput('sub_letters',project_id=1,doc_id=did,kind=kind,area='safety',src_type='finding',src_id=finding['id'],company=company,to_user_ids=[uid],cc_user_ids=[5],due_date='2026-10-04',issued_by=3 if kind=='work_contact' else 5,issued_at=ts('2026-09-28',12),acked_by=uid if status!='issued' else None,acked_at=ts('2026-09-29',9) if status!='issued' else None,reply_text=zh+' 已按要求组织，复验记录已附。 / Actions arranged; reinspection records attached.' if status in ('closed','replied','returned') else None,reply_date='2026-10-02' if status in ('closed','replied','returned') else None,reply_by=uid if status in ('closed','replied','returned') else None,reply_at=ts('2026-10-02',16) if status in ('closed','replied','returned') else None,closed_by=3 if status=='closed' else None,closed_at=ts('2026-10-04',16) if status=='closed' else None,return_reason='请补充复验部位及日期对应记录' if status=='returned' else None,status=status)
    for kind,ref in [('meeting_pack','2026-10-04'),('three_week_plan','2026-10-04'),('monthly_review','2026-08-01'),('monthly_review','2026-09-01')]:
        # These helpers assemble frozen facts only; no model or external service.
        f=gongbi_docs.build_facts(c,user,kind,ref)
        texts={'meeting_pack':'核对上期行动项及未闭环风险，围绕施工接口、验收资料和运输窗口逐项落实责任。',
              'three_week_plan':'依据截止期 P6 梳理工作面与前置条件，分周协调设备、材料与交通组织；预测只作参考推演。',
              'monthly_review':'雨季排水、连续工作面和材料批次隔离应前移管理；整改复验须与原问题位置对应。'}
        nar={s['key']:([texts[kind]] if s['type']=='list' else texts[kind]) for s in reg.TYPES[kind]['sections']}
        if kind=='three_week_plan':
            for key in nar:
                if 'en' in key: nar[key]='Coordinate released fronts, inspection hold points and haul windows against the current programme. Forecast dates are planning scenarios.'
        jput('gongbi_docs',project_id=1,kind=kind,ref=ref,title=reg.TYPES[kind]['name']+' · '+ref,lang='zh',period_start=f.get('period_start'),period_end=f.get('period_end'),facts_json=f,narrative_json=nar,check_json={'guard':[],'warnings':[]},source='authored',status='confirmed',created_by=3,created_at=STAMP,confirmed_by=5,confirmed_at=STAMP)
    # Delay analysis without contractual assertions; contract screens remain empty.
    event={'project':'仿真公路项目','event':{'cat':'weather','cat_name':'雨季工作窗口受限','date_from':'2026-09-07','date_to':'2026-09-13'},'affected':[],'milestones':[],'notice':{},'logs':[]}
    nar={'event':'雨后便道与工作面恢复需排水及复测，运输周转影响连续作业。','affected':'受影响接口为路基开放面及路面材料运输；以原日志记录核对。','milestones':'关键线路的节点日期以当前P6和预测结果并列，属于参考推演，不形成工期索赔结论。','notice':'本仿真不模拟合同通知与索赔程序。','follow':['保留雨后排水与复测记录','协调设备及运输窗口','按周复核工作面释放情况']}
    jput('gongbi_docs',project_id=1,kind='delay_brief',ref='sim-weather-september',title='雨季工作面接口延误分析简报',lang='zh',facts_json=event,narrative_json=nar,check_json={'guard':[],'warnings':[]},source='authored',status='confirmed',created_by=3,created_at=STAMP,confirmed_by=5,confirmed_at=STAMP)

def predictions_briefs(imp):
    from app.services import schedule, plan_insight, plan_health_agent, brief, facts
    fc=schedule.forecast(c,1,as_of=CUT.isoformat(),iterations=200,seed=SEED)
    c.execute('UPDATE forecasts SET created_at=? WHERE project_id=1',(STAMP,))
    h=plan_insight.health(c,1)
    diag=plan_health_agent.template(h)
    knowledge('plan_health',{**diag,'import_id':imp['id'],'data_date':'2026-10-04','source':'authored','created_at':STAMP,'note':'检查数量由当前P6计算；诊断文字预写。'})
    measures=[{'activity_code':r['code'],'duration_factor':.88,'action':'协调运输窗口，增加备用设备并保持连续工作面','cost':'增加操作手及备用运输车，施工班组协同','risk':'交通与交叉作业需专人指挥'} for r in fc['critical_path'][:3]]
    verified=schedule.what_if(c,1,measures,'2026-10-04')
    knowledge('recovery',{'diagnosis':'雨季工作窗口压缩后，当前需优先保证连续工作面与运输周转；赶工方案先核对安全、材料及检验接口。','verified':verified,'forecast_at':STAMP,'source':'authored','note':'预写资源建议；工期效果由同一CPM逻辑复算，不作实际承诺。'})
    k=brief.kpis(c,1,DEMO); pf=facts.project_facts(c,1)
    content={'headline':'中桥与路面接口需要协调，未闭环防护问题优先复核。','items':[
      {'area':'进度','level':'orange','title':'统筹中桥与路面工作面','detail':'核对节点预测、运输窗口及检验前置条件。','action':'在例会逐项落实接口责任','ref':{'route':'progress','params':{}}},
      {'area':'安全','level':'red','title':'先处理超期防护事项','detail':'未闭环事项保留原问题与整改影像，主管复核后闭环。','action':'核对整改部位与原问题位置','ref':{'route':'safety','params':{}}},
      {'area':'质量','level':'yellow','title':'盯住隐蔽及材料复验','detail':'复验结果与报验部位、材料批号保持对应。','action':'检验确认后释放下道工序','ref':{'route':'quality','params':{}}}],
      'source':'authored','facts':brief.facts_text(c,1,k,pf),'created_at':ts(DEMO)}
    content['refs']=brief._refs(brief._snapshot(c,1,k,pf),content)
    jput('briefs',project_id=1,day=DEMO.isoformat(),content_json=content,created_at=ts(DEMO))

def minis():
    # Smaller independent projects exist only for leadership comparison.
    for pid,name,code,pct,amount in [(2,'仿真河谷连接线','SIM-VALLEY',.37,4200000),(3,'仿真山岭养护工程','SIM-RIDGE',.81,1800000)]:
        cfg={'simulation':True,'cadence':'weekly','workdays_per_week':6,'output_plan':{'boq_total':amount,'year_plan':{'2026':amount*.9},'currency':'USD','source':'独立小项目仿真BOQ与年度目标'},'platform':{'stage':'活跃在建','stage_code':'02','source':'仿真主数据底稿'}}
        jput('projects',id=pid,name=name,code=code,config_json=cfg,created_at=ts('2026-02-02'))
        uid=21+pid
        put('users',id=uid,username=f'sim_small{pid}',name='方予辰' if pid==2 else '蒋思遥',role='eng_sup',password_hash=password_hash(uid),project_id=pid,company='澄岳国际工程',created_at=ts('2026-02-02'))
        bid=put('boq_items',project_id=pid,code='SM-01',name='路段工程综合清单',unit='m',unit_price=amount/10000,currency='USD',contract_qty=10000)
        put('boq_map',project_id=pid,activity_code='SM001',boq_id=bid,factor=1,status='confirmed',confirmed_by=uid,updated_at=STAMP)
        imp=put('p6_imports',project_id=pid,file_name=f'{code}-programme.xer',sha256=hashlib.sha256(code.encode()).hexdigest(),proj_name=name,data_date='2026-10-04',activity_count=1,relation_count=0,is_current=1,imported_by=uid,imported_at=STAMP)
        put('activities',import_id=imp,task_id='1',code='SM001',name='主线施工',wbs_path='主线',task_type='TT_Task',status='进行中',target_start='2026-02-02',target_finish='2027-04-04',early_start='2026-02-02',early_finish='2027-04-04',orig_dur_d=350,remain_dur_d=350*(1-pct),total_float_d=10,phys_pct=pct*100)
        lid=jput('logs',project_id=pid,author_id=uid,log_date='2026-09-28',period='week',period_end='2026-10-04',mode='ai',status='approved',content_json={'summary':'施工工作面及进度事实已核对。','works':[],'progress':[],'issues':[]},started_at=STAMP,submitted_at=STAMP,reviewed_by=uid,reviewed_at=STAMP)
        put('log_progress',project_id=pid,log_id=lid,log_date='2026-10-04',activity_code='SM001',qty=10000*pct,unit='m',pct=pct*100)
        put('output_records',project_id=pid,period_start='2026-09-28',period_end='2026-10-04',actual=amount*pct,planned=amount*pct*1.02,baseline=amount*pct*1.05,currency='USD',source='sim',note='小项目开累仿真记录；独立数量来源')
        for n in range(3 if pid==2 else 2):
            jput('findings',project_id=pid,agent='safety',kind='hazard',level='medium',title='施工通道防护复查',body_json={'category':'交通导改','part':'主线施工区'},status='open' if n==0 else 'resolved',assignee_id=uid,confirmed_by=uid,confirmed_at=ts('2026-10-01'),due_date='2026-10-04',created_at=ts('2026-10-01'),updated_at=STAMP,resolved_at=STAMP if n else None)

def audit_manifest(total,weeks):
    from app.services import output
    actual=output.compute(c,1,START.isoformat(),CUT.isoformat())
    counts={r['name']:one(c,f'SELECT COUNT(*) n FROM {r["name"]}')['n'] for r in all_(c,"SELECT name FROM sqlite_master WHERE type='table'")}
    assert not actual['unmapped'] and not actual['unit_mismatch']
    assert abs(sum(w['actual_usd'] for w in weeks)-actual['total'])<.005
    pct=actual['total']/total*100
    if not 55<=pct<=65: print('WARNING progress outside target:',pct)
    assert one(c,'PRAGMA integrity_check')['integrity_check']=='ok'
    assert not all_(c,'PRAGMA foreign_key_check')
    forbidden=tuple(bytes.fromhex(x).decode('utf-8') for x in ('e88eb1e59fba','4c454b4b49','4c4f5431','434845432f4e472f323032352f303036','e7a6bbe7babfe6a8a1e68b9f'))
    for t in counts:
        for r in all_(c,f'SELECT * FROM {t}'):
            assert not any(word.lower() in dumps(r).lower() for word in forbidden),(t,'forbidden content')
    manifest={'seed':SEED,'start':str(START),'cutoff':str(CUT),'demo':str(DEMO),'end':str(END),'boq_total':total,'actual_usd':actual['total'],'progress_pct':pct,'counts':counts,'integrity':'ok','foreign_keys':'ok','external_ai_calls':0,'media_kind':'locally generated synthetic scenes/documents, not field evidence','accounts':[{'username':u,'role':r,'name':n,'password':PASSWORD} for u,n,r,_ in ROSTER]}
    (DATA/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in manifest.items() if k not in ('accounts','counts')},ensure_ascii=False,indent=2))
    print('Rows:',counts)

def main():
    global c
    reset_own(); init_db(); c=connect()
    with time_machine.travel('2026-10-04T18:00:00+01:00',tick=False):
        cfg=project_users(); items,daily=quantities(); total=boq(items,cfg)
        imp=xer(items,daily); ids,wids,nids=logs_notes(daily)
        print('P6, BOQ, daily source and weekly approvals generated',flush=True)
        medias=pictures(ids,nids); fids=safety_quality(medias); materials_conceal(); social_notes(nids)
        print('Photos, safety, quality, materials and social generated',flush=True)
        alerts_nudge(items,imp,fids); meetings(); weeks=output_records()
        predictions_briefs(imp); documents(); minis(); audit_manifest(total,weeks)
    c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()

if __name__=='__main__': main()
