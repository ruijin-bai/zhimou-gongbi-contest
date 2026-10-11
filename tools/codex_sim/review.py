"""Per-role UI inspection; original repository checkers, unmodified."""
import subprocess, sys, json
from runtime import ROOT,DATA

BASE='http://127.0.0.1:8663'
SHOTS=ROOT.parent/'仿真数据赛马'/'codex截图'
SHOTS.mkdir(parents=True,exist_ok=True)
roles=['tech','sub_safety','eng_sup','safety_sup','manager','leader','admin']
summary=[]
for role in roles:
    checks=[('role_shots', ['node','tools/role_shots.mjs',role,'--base',BASE,'--out',str(SHOTS/'岗位巡检'/role),'--cdp-port','9433']),
      ('fit', ['node','tools/fit_check.mjs','--base',BASE,'--roles',role,'--vp','1440x900,390x844,360x780,430x932','--shots','--out',str(SHOTS/'版式巡检'/role),'--cdp-port','9434']),
      ('links',['node','tools/home_links.mjs','--base',BASE,'--roles',role,'--out',str(DATA/'checks'/role/'links'),'--cdp-port','9435'])]
    for name,cmd in checks:
        out=DATA/'checks'/role; out.mkdir(parents=True,exist_ok=True)
        with (out/f'{name}.log').open('w',encoding='utf-8') as log:
            p=subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        summary.append({'role':role,'check':name,'exit':p.returncode})
        (DATA/'ui_check_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        print(role,name,p.returncode,flush=True)
    print('ROLE COMPLETE',role,flush=True)
