"use strict";
const {chromium} = require(process.env.NEXUS_PLAYWRIGHT || "../var/attachment-browser/node_modules/playwright-core");
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const root=path.resolve(process.env.NEXUS_DEVICE_RELEASE || 'var/unified-device-release');
const delay=ms=>new Promise(r=>setTimeout(r,ms));
const conversation={conversation_id:'10000000-0000-4000-8000-000000000001',revision:1,title:'设备同步测试',archived:false};
const nodes=[{node_id:'20000000-0000-4000-8000-000000000001',label:'书房电脑',online:true,tools:['system.info@1'],roots:['workspace']},
 {node_id:'20000000-0000-4000-8000-000000000002',label:'随身手机',online:false,tools:['android.device_status@1'],roots:['android:device']}];
let slow=false,pending=0,image=false,nodeFailure=false,mode='web',requests=[];
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');let body='';for await(const part of req)body+=part;
 requests.push({path:url.pathname,query:url.search,method:req.method,body:body?JSON.parse(body):null});
 if(url.pathname.startsWith('/api/')) {
  res.setHeader('Content-Type','application/json');res.setHeader('Cache-Control','no-store');let data={requests:[],versions:[],runs:[],nodes:[],enabled:false};
  if(url.pathname==='/api/v1/session') {data={csrf_token:'fixture-only',expires_at:'2099-01-01T00:00:00Z'};if(req.method==='POST')res.setHeader('Set-Cookie','nexus_session=fixture-session; Path=/; HttpOnly; SameSite=Strict');}
  else if(url.pathname==='/api/v1/nodes') {if(nodeFailure){res.statusCode=503;data={detail:'BUSY'};}else data={nodes};}
  else if(url.pathname.match(/\/nodes\/.*\/name$/)) {nodes.find(n=>url.pathname.includes(n.node_id)).label=JSON.parse(body).name;data={};}
  else if(url.pathname==='/api/v1/chat/work-catalog')data={work_digest:'a'.repeat(64),nodes:nodes.map(n=>({node_id:n.node_id,name:n.label,online:n.online})),catalog:[{name:'device1__get_system_info',route_intent:'get_system_info'},...(image?[{name:'device1__read_image',route_intent:'read_image'}]:[])]};
  else if(url.pathname==='/api/v1/chat/conversations')data={conversations:[conversation]};
  else if(url.pathname.endsWith('/turns')) {
   if(slow&&req.method==='GET'){pending++;await delay(8000);pending--;}
   data={conversation,turns:[],active_turn_id:null};
  } else if(url.pathname==='/api/v1/client/snapshot')data={tasks:[],control:{paused:false,revision:1}};
  else if(url.pathname.includes('/permission'))data={effective_mode:'normal',source:'global',revision:1};
  else if(url.pathname==='/api/v1/records/projects')data={projects:[]};
  else if(url.pathname==='/api/v1/control')data={paused:false,revision:1};
  else if(url.pathname==='/api/v1/models/usage')data={calls:0,enabled:true,chat_enabled:true,cost_microunits:0};
  else if(url.pathname==='/api/v1/extensions')data={extension:{enabled:false}};
  else if(url.pathname.includes('/attachments')){res.statusCode=404;data={detail:'NOT_FOUND'};}
  res.end(JSON.stringify(data));return;
 }
 let relative=url.pathname.replace(/^\/static\//,'').replace(/^\//,'');
 if(mode==='web'&&url.pathname==='/app')relative='app/app.html';
 const base=path.join(root,mode==='web'?'web':'windows-src/ui');
 const file=path.resolve(base,relative||'app.html');
 if(!file.startsWith(base+path.sep)||!fs.existsSync(file)){res.statusCode=404;res.end();return;}
 res.setHeader('Content-Type',/\.m?js$/.test(file)?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));
});
async function run(){
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin=`http://127.0.0.1:${server.address().port}`;
 let nativeChild=null,nativeBrowser=null;
 const browser=await chromium.launch({headless:true,channel:'msedge'});const evidence=[];
 try {
  for(mode of (process.env.NEXUS_DEVICE_EXE?['native']:process.env.NEXUS_LEGACY_ONLY?[]:['web','windows'])) {
   slow=false;nodeFailure=false;nodes[1].online=false;image=false;requests=[];
   let page;
   if(mode==='native') {
    const profile=path.join(root,'native-profile');fs.mkdirSync(profile,{recursive:true});
    nativeChild=spawn(process.env.NEXUS_DEVICE_EXE,[],{env:{...process.env,APPDATA:profile,WEBVIEW2_USER_DATA_FOLDER:path.join(profile,'webview'),WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS:'--remote-debugging-port=19434'},stdio:'ignore'});
    for(let attempt=0;attempt<80;attempt++){try{nativeBrowser=await chromium.connectOverCDP('http://127.0.0.1:19434');break;}catch{await delay(250);}}
    assert(nativeBrowser,'native CDP unavailable');
    for(let attempt=0;attempt<80;attempt++){page=nativeBrowser.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('app.html'));if(page)break;await delay(250);}
    assert(page,'native main page unavailable');
   } else page=await browser.newPage({viewport:{width:1280,height:900}});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.addInitScript(()=>localStorage.setItem('nexus.chat.prefs.v1',JSON.stringify({nodeId:'wrong-old-selection',resultFeedback:true,tools:true})));
   if(mode==='native') {
    await page.locator('#core-url').fill(origin);await page.locator('#token').fill('a'.repeat(43));await page.locator('#login-form button[type=submit]').click();
   } else await page.goto(origin+(mode==='web'?'/app':'/app.html'));
   await page.waitForFunction(()=>document.getElementById('connection').textContent.includes('1/2')).catch(async e=>{console.error(errors,await page.locator('body').innerText(),requests.slice(-8));throw e;});
   slow=true;while(!pending)await delay(100);
   const start=Date.now();nodes[1].online=true;
   await page.waitForFunction(()=>document.getElementById('connection').textContent.includes('2/2'),null,{timeout:4000});
   const latency=Date.now()-start;assert(pending>0,'device sync waited for slow history');
   slow=false;
   await page.click('#settings-button');
   await page.waitForFunction(()=>document.getElementById('settings-devices')?.textContent.includes('随身手机'));
   assert.equal(await page.locator('text=设为目标').count(),0);
   await page.getByRole('button',{name:'改名',exact:true}).last().click();
   await page.locator('#sheet-body input').fill('出门手机');
   await delay(2200);assert.equal(await page.locator('#sheet-body input').inputValue(),'出门手机');
   await page.locator('#sheet-actions').getByRole('button',{name:'保存',exact:true}).click();
   await page.waitForFunction(()=>document.getElementById('settings-devices')?.textContent.includes('出门手机'));
   await page.screenshot({path:path.join(root,`${mode}-devices.png`)});
   await page.click('#settings-close');
   const modulePath=mode==='web'?'/static/app/api.js':new URL('api.js',page.url()).href;
   const submission=await page.evaluate(async p=>{const a=await import(p);return a.prepareSend('告诉我电脑系统',{allowWork:true});},modulePath);
   assert.equal(submission.payload.all_nodes,true);assert.equal(submission.payload.node_id,undefined);assert.equal(submission.payload.allow_work_results,true);
   image=true;
   const consent=await page.evaluate(async p=>{const a=await import(p);return a.prepareSend('看图片');},modulePath);
   assert.equal(consent.kind,'consent');assert.equal(consent.imageAvailable,true);
   nodeFailure=true;
   await page.waitForFunction(()=>document.getElementById('connection').textContent.includes('重新同步'),null,{timeout:4000});
   nodeFailure=false;nodes[0].online=false;
   await page.waitForFunction(()=>document.getElementById('connection').textContent.includes('1/2'),null,{timeout:7000});
   assert(!requests.some(r=>r.path==='/api/v1/chat/work-catalog'&&r.query));
   assert.deepEqual(errors,[]);evidence.push({mode,latency_ms:latency,slow_history_independent:true,global_catalog:true,rename_preserves_draft:true,image_consent:true,failure_recovery:true});
   if(mode!=='native')await page.close();nodes[0].online=true;nodes[1].label='随身手机';
  }
  if(!process.env.NEXUS_DEVICE_EXE) {
   mode='web';slow=false;nodeFailure=false;image=false;
   const legacy=await browser.newPage();const errors=[];legacy.on('pageerror',e=>errors.push(e.message));
   await legacy.goto(origin+'/index.html');await legacy.waitForFunction(()=>!document.getElementById('console').hidden).catch(async e=>{console.error(errors,await legacy.locator('body').innerText(),requests.filter(r=>r.path.startsWith('/api')).slice(-15));throw e;});
   await legacy.locator('[data-tab=chat]').click();await legacy.locator('#chat-text').fill('告诉我电脑系统');
   await legacy.locator('#chat-send').click();await legacy.waitForTimeout(300);
   const sent=requests.filter(r=>r.method==='POST'&&r.path.endsWith('/turns')).at(-1);
   assert(sent,'legacy chat did not submit');assert.equal(sent.body.all_nodes,true);assert.equal(sent.body.node_id,null);
   assert.equal(await legacy.locator('#chat-node').count(),0);assert.deepEqual(errors,[]);
   evidence.push({mode:'legacy',global_catalog:true,no_manual_device:true});await legacy.close();
  }
  fs.writeFileSync(path.join(root,process.env.NEXUS_DEVICE_EXE?'native-evidence.json':process.env.NEXUS_LEGACY_ONLY?'legacy-evidence.json':'browser-evidence.json'),JSON.stringify(evidence,null,2));console.log(JSON.stringify(evidence));
 }finally{if(nativeBrowser)await nativeBrowser.close();if(nativeChild)nativeChild.kill();await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));}
}
run().catch(e=>{console.error(e);server.closeAllConnections();server.close();process.exitCode=1;});
