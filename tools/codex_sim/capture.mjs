// Viewport screenshots and original page/API readback. Own instance and output paths only.
import {spawn,spawnSync} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
const root=process.cwd();
const OUT=path.resolve(root,'../仿真数据赛马/codex截图/交付视口');
const DATA=path.resolve(root,'data_codex');
fs.mkdirSync(OUT,{recursive:true});
const BASE='http://127.0.0.1:8663', PORT=9440;
const udd=path.join(DATA,'capture-edge');
const edge=spawn('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',['--headless=new',`--remote-debugging-port=${PORT}`,`--user-data-dir=${udd}`,'--no-first-run','--disable-gpu','about:blank'],{stdio:'ignore'});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let tabs;
for(let i=0;i<50;i++){try{tabs=await(await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();if(tabs.length)break;}catch{}await sleep(250);}
const ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);
await new Promise(r=>ws.addEventListener('open',r));
let seq=0; const pending=new Map(); let errors=[];
ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){pending.get(m.id)?.(m);pending.delete(m.id);}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.exception?.description||m.params.exceptionDetails.text);});
const cmd=(method,params={})=>new Promise(r=>{const id=++seq;pending.set(id,r);ws.send(JSON.stringify({id,method,params}));});
const ev=async(expression)=>(await cmd('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true})).result?.result?.value;
await cmd('Runtime.enable');await cmd('Page.enable');
const roles=[[1,'技术质量','home'],[2,'分包安全','shome'],[3,'工程主管','eng'],[4,'安全主管','sdash'],[5,'项目经理','work'],[6,'公司领导','cockpit'],[7,'管理员','sysh']];
const reps=[[3,'P6计划','progress?tab=plan'],[3,'进度预测与赶工','progress'],[3,'产值与年度','output'],[4,'安全闭环','safety'],[3,'质量与分包','quality'],[3,'现场圈','circle'],[3,'材料与小票','tickets'],[3,'文书周报','report?id=6'],[3,'隐蔽验收','conceal?id=1'],[3,'例会','meetings'],[3,'双语函件','letters']];
const tasks=[...roles.map(([id,label,route])=>({id,label:label+'首页',route})),...reps.map(([id,label,route])=>({id,label,route})),{id:null,label:'工人扫码首页',url:BASE+'/q/SIMROAD2026WORKERQR001234567890'}];
const results=[];
for(const vp of [{w:1440,h:900},{w:390,h:844}]){
 await cmd('Emulation.setDeviceMetricsOverride',{width:vp.w,height:vp.h,deviceScaleFactor:1,mobile:vp.w<600});
 for(const t of tasks){
  errors=[];
  await cmd('Page.navigate',{url:BASE+'/static/favicon.svg'});await sleep(100);
  if(t.id){
   await ev(`fetch('/api/dev/login',{method:'POST',headers:{'x-zb':'1','content-type':'application/json'},body:JSON.stringify({user_id:${t.id}})}).then(r=>r.status)`);
   await ev(`localStorage.setItem('zb_dev_user','${t.id}');sessionStorage.removeItem('zb_no_auto');1`);
  }else{await cmd('Network.clearBrowserCookies'); await ev(`localStorage.clear();1`);}
  await cmd('Page.navigate',{url:t.url||BASE+'/#/'+t.route});await sleep(2300);
  // Multi-project overview makes several dependent requests; capture rendered data.
  for(let ready=0;ready<80;ready++){
   const text=await ev('document.body.innerText');
   if(text&&!text.includes('加载中'))break;
   await sleep(250);
  }
  await ev('document.fonts.ready.then(()=>true)');
  await ev(`Promise.all([...document.images].filter(i=>{const r=i.getBoundingClientRect();return r.top<innerHeight&&r.bottom>0}).map(i=>i.complete?Promise.resolve():new Promise(r=>{i.addEventListener('load',r,{once:true});i.addEventListener('error',r,{once:true});setTimeout(r,4000)})))`);
  const state=await ev(`({url:location.href,viewport:[innerWidth,innerHeight],title:document.title,text:document.body.innerText,broken:[...document.images].filter(i=>{const r=i.getBoundingClientRect();return r.top<innerHeight&&r.bottom>0&&i.complete&&i.naturalWidth===0}).map(i=>i.src),width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight})`);
  const shot=await cmd('Page.captureScreenshot',{format:'png'});
  const filename=`${vp.w}x${vp.h}_${t.label}.png`;
  fs.writeFileSync(path.join(OUT,filename),Buffer.from(shot.result.data,'base64'));
  results.push({label:t.label,roleId:t.id,route:t.route,filename,errors,...state});
  console.log(filename,errors.length?'ERROR':'ok');
 }
}
fs.writeFileSync(path.join(DATA,'screenshots_readback.json'),JSON.stringify(results,null,2));
ws.close();spawnSync('taskkill',['/pid',String(edge.pid),'/T','/F'],{stdio:'ignore'});
// Browser profile intentionally retained only in ignored data_codex; no broad cleanup.
