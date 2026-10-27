"use strict";
const {chromium}=require(process.env.NEXUS_PLAYWRIGHT || '../var/attachment-browser/node_modules/playwright-core');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const repo=path.resolve(__dirname,'..'),release=process.env.NEXUS_UI_RELEASE && path.resolve(process.env.NEXUS_UI_RELEASE);
const output=process.env.NEXUS_UI_OUTPUT || release || path.join(repo,'var/ui-refresh-release');
fs.mkdirSync(output,{recursive:true});
const conversation={conversation_id:'10000000-0000-4000-8000-000000000001',revision:1,title:'一起完成今天的工作',archived:false};
const nodes=[{node_id:'20000000-0000-4000-8000-000000000001',label:'书房电脑',online:true,tools:['system.info@1'],roots:['workspace']},
 {node_id:'20000000-0000-4000-8000-000000000002',label:'随身手机',online:false,tools:['android.device_status@1'],roots:[]}];
const turn={turn_id:'30000000-0000-4000-8000-000000000001',revision:1,text:'帮我整理资料，并把结果给我。',status:'completed',work_status:'',response_text:'## 已整理好\n\n这里是你的工作结果。\n\n- 重点清楚，过程可以展开\n- 文件直接预览或打开',work_steps:[],presentation:{version:1,label:'已完成',phase:'completed',final_text:'## 已整理好\n\n这里是你的工作结果。\n\n- 重点清楚，过程可以展开\n- 文件直接预览或打开',live_text:'',process:[],files:[],can_collapse:true}};
let surface='web',writes=[];
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');let body='';for await(const part of req)body+=part;
 if(req.method!=='GET')writes.push({url:url.pathname,body});
 if(url.pathname.startsWith('/api/')) {
  let data={requests:[],versions:[],runs:[],nodes:[],enabled:false};
  if(url.pathname==='/api/v1/session') { data={csrf_token:'fixture-only',expires_at:'2099-01-01T00:00:00Z'};if(req.method==='POST')res.setHeader('Set-Cookie','nexus_session=fixture-session; Path=/; HttpOnly; SameSite=Strict'); }
  else if(url.pathname==='/api/v1/nodes')data={nodes};
  else if(url.pathname==='/api/v1/chat/work-catalog')data={work_digest:'a'.repeat(64),nodes,catalog:[{name:'device1__get_system_info',route_intent:'get_system_info'}]};
  else if(url.pathname==='/api/v1/chat/conversations')data={conversations:[conversation]};
  else if(url.pathname.endsWith('/turns'))data={conversation,turns:[turn],active_turn_id:null};
  else if(url.pathname==='/api/v1/client/snapshot')data={tasks:[],control:{paused:false,revision:1}};
  else if(url.pathname.includes('/permission'))data={effective_mode:'normal',source:'global',revision:1};
  else if(url.pathname==='/api/v1/records/projects')data={projects:[]};
  else if(url.pathname==='/api/v1/control')data={paused:false,revision:1};
  else if(url.pathname==='/api/v1/models/usage')data={calls:0,enabled:true,chat_enabled:true,cost_microunits:0};
  else if(url.pathname==='/api/v1/extensions')data={extension:{enabled:false}};
  else if(url.pathname==='/api/v1/assistant/preferences')data={preferences:{personality:'nexus',emoji:'natural',revision:1},personalities:[{id:'nexus',name:'NEXUS · 沉稳机智',description:'清晰、自然，保持独立判断。',examples:{natural:'资料整理好了。重点和来源都在这里，我们可以接着完成下一步。'}}],emoji_modes:[{id:'natural',name:'适量'}]};
  else if(url.pathname.includes('/attachments'))data={attachments:[],capabilities:{max_files_per_turn:4,max_file_bytes:16777216}};
  res.setHeader('Content-Type','application/json');return res.end(JSON.stringify(data));
 }
 const base=release?path.join(release,surface==='web'?'site/nexus/web':'windows-src/ui'):path.join(repo,surface==='web'?'src/nexus/web':'apps/windows/ui');
 const relative=url.pathname==='/app'?'app/app.html':url.pathname==='/app.html'?'app.html':url.pathname.replace(/^\/static\//,'').replace(/^\//,'');
 const file=path.resolve(base,relative);
 if(!file.startsWith(base+path.sep)||!fs.existsSync(file)){res.statusCode=404;return res.end();}
 res.setHeader('Content-Type',/\.m?js$/.test(file)?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');let content=fs.readFileSync(file);
 if(file.endsWith(path.sep+'visualization.js'))content=content.toString().replace('    frame = requestAnimationFrame(animate);','    window.__uiRenderedFrames = (window.__uiRenderedFrames || 0) + 1;\n    frame = requestAnimationFrame(animate);');
 res.end(content);
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin=`http://127.0.0.1:${server.address().port}`;
 const browser=await chromium.launch({headless:true,channel:'msedge'}),evidence=[];
 let nativeChild=null,nativeBrowser=null;
 try {
  for(surface of (process.env.NEXUS_UI_EXE?['native']:['web','windows'])) {
   for(let width of (surface==='native'?[1280]:[1280,1920,600,390])) {
    let page;const errors=[];
    if(surface==='native') {
     const profile=path.join(output,'native-ui-profile');fs.mkdirSync(profile,{recursive:true});
     nativeChild=spawn(process.env.NEXUS_UI_EXE,[],{env:{...process.env,APPDATA:profile,WEBVIEW2_USER_DATA_FOLDER:path.join(profile,'webview'),WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS:'--remote-debugging-port=19435'},stdio:'ignore'});
     for(let attempt=0;attempt<80;attempt++){try{nativeBrowser=await chromium.connectOverCDP('http://127.0.0.1:19435');break;}catch{await delay(250);}}
     assert(nativeBrowser,'native CDP unavailable');
     for(let attempt=0;attempt<80;attempt++){page=nativeBrowser.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('app.html'));if(page)break;await delay(250);}
     assert(page,'native main page unavailable');width=await page.evaluate(()=>innerWidth);
    } else page=await browser.newPage({viewport:{width,height:860}});
    page.on('pageerror',e=>errors.push(e.message));
    if(surface==='native') {await page.locator('#core-url').fill(origin);await page.locator('#token').fill('a'.repeat(43));await page.locator('#login-form button[type=submit]').click();}
    else await page.goto(origin+(surface==='web'?'/app':'/app.html'));
    await page.waitForFunction(()=>document.querySelector('#connection')?.textContent.includes('1 台设备在线'));
    await page.locator('#visual-canvas canvas').waitFor();
    if(surface!=='native')await page.waitForFunction(()=>window.__uiRenderedFrames>=3);
    const fullCanvas=await page.evaluate(()=>{
     const home=document.querySelector('#home-view').getBoundingClientRect();
     const canvas=document.querySelector('#visual-canvas canvas').getBoundingClientRect();
     const dock=document.querySelector('#dock').getBoundingClientRect();
     const side=document.elementFromPoint((canvas.left+dock.left)/2,dock.top+dock.height/2);
     return Math.abs(canvas.height-home.height)<2 && canvas.bottom>=dock.bottom && side?.tagName==='CANVAS';
    });
    assert(fullCanvas,'Visualization must extend behind and beside the floating composer');
    assert.equal(await page.locator('#dock-chips .chip').count(),1);
    assert.equal(await page.locator('#dock-wake').count(),0);
    await page.screenshot({path:path.join(output,`${surface}-${width}-home.png`)});
    await page.locator('#tools-menu > summary').click();
    await page.locator('#plugins-button').waitFor({state:'visible'});
    await page.keyboard.press('Escape');assert.equal(await page.locator('#tools-menu').getAttribute('open'),null);
    await page.click('#settings-button');await page.locator('#settings-devices').getByText('书房电脑').waitFor();
    const settings=await page.locator('#settings-body').innerText();
    assert.equal(await page.locator('#settings-account').getAttribute('open'),null);
    await page.locator('#settings-projects > summary').click();
    await page.getByLabel('新项目名称',{exact:true}).fill('合成项目');
    assert.equal(await page.getByLabel('新项目名称',{exact:true}).inputValue(),'合成项目');
    await page.locator('#settings-projects > summary').click();
    assert(!settings.includes('文件传输'));assert(!settings.includes('Pillow'));assert(!settings.includes('normal/full'));assert(!settings.includes('占位'));
    assert.equal(await page.locator('.settings-disclosure[open]').count(),0);
    await page.screenshot({path:path.join(output,`${surface}-${width}-settings.png`)});
    await page.click('#settings-close');await page.click('#dock-expand');
    assert.equal(await page.locator('#dock-expand').getAttribute('aria-current'),'page');
    await page.click('#view-home');assert.equal(await page.locator('#view-home').getAttribute('aria-current'),'page');
    await page.click('#dock-expand');
    await page.getByRole('heading',{name:'已整理好'}).waitFor();await page.locator('#chat-text').fill('保留这份草稿');
    await page.locator('#chat-scope').click();await page.locator('#sheet').waitFor({state:'visible'});
    assert((await page.locator('#sheet-body').innerText()).includes('上下文') || (await page.locator('#sheet-body').innerText()).includes('对话'));
    await page.click('#sheet-close');assert.equal(await page.locator('#chat-text').inputValue(),'保留这份草稿');
    const bounds=await page.locator('#chat-send').boundingBox();assert(bounds.x>=0 && bounds.x+bounds.width<=width+1 && bounds.y+bounds.height<=860);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(output,`${surface}-${width}-chat.png`)});
    await page.getByRole('button',{name:'对话选项',exact:true}).click();await page.locator('#sheet-close').focus();await page.keyboard.press('Enter');
    assert.deepEqual(errors,[]);evidence.push({surface,width,webgl:true,compact_controls:true,settings_disclosures:true,composer_visible:true,keyboard:true});if(surface!=='native')await page.close();
   }
  }
  assert.equal(writes.filter(value=>value.url!=='/api/v1/session' && !/^\/api\/v1\/ui-extensions\/conversations\/[^/]+\/poll$/.test(value.url)).length,0,'Viewing and layout changes must not send user actions');
  fs.writeFileSync(path.join(output,process.env.NEXUS_UI_EXE?'native-ui-evidence.json':'ui-browser-evidence.json'),JSON.stringify(evidence,null,2));console.log(JSON.stringify({passed:true,cases:evidence.length}));
 } finally {if(nativeBrowser)await nativeBrowser.close();if(nativeChild)nativeChild.kill();await browser.close();server.closeAllConnections();server.close();}
})().catch(e=>{console.error(e);server.closeAllConnections();server.close();process.exitCode=1;});
