const {chromium}=require(process.env.NEXUS_PLAYWRIGHT);
const fs=require('node:fs');const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 const page=await browser.newPage({viewport:{width:390,height:844}});const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://127.0.0.1:39871/**',route=>route.fulfill({contentType:route.request().url().endsWith('.js')?'text/javascript':'text/html',body:route.request().url().endsWith('.js')?fs.readFileSync('src/nexus/web/app/file-transfers.js','utf8'):'<!doctype html><html><body>Fixture</body></html>'}));
 await page.goto('http://127.0.0.1:39871/');
 await page.evaluate(async()=>{
  const {openFileTransfer}=await import('/file-transfers.js');window.calls=[];window.tasks=[];window.done=false;window.fail=true;
  const node='22222222-2222-4222-8222-222222222222';
  const api=async(path,opts)=>{
   if(path.endsWith('/targets'))return {nodes:[{node_id:node,label:'工作手机',available:true,reason:'ready'},{node_id:'33333333-3333-4333-8333-333333333333',label:'离线电脑',available:false,reason:'offline'}]};
   if(opts){window.calls.push(JSON.parse(opts.body));if(window.fail){window.fail=false;throw Error('lost response');}
    window.tasks=[{task_id:'44444444-4444-4444-8444-444444444444',node_id:node,request:{request_id:JSON.parse(opts.body).request_id},status:'running',action:{arguments:{path:'NEXUS-Inbox/request/资料.bin'}}}];return {task:window.tasks[0]};}
   return {tasks:window.tasks.map(t=>({...t,status:window.done?'succeeded':'running'}))};
  };
  await openFileTransfer({conversation_id:'11111111-1111-4111-8111-111111111111',attachment_id:'55555555-5555-4555-8555-555555555555',filename:'资料.bin'},api,()=>crypto.randomUUID());
 });
 await page.getByRole('button',{name:'发送到所选设备',exact:true}).click();
 await page.getByRole('button',{name:'用原编号核对并重试',exact:true}).click();
 await page.getByText('工作手机：正在传输').waitFor();
 const ids=await page.evaluate(()=>window.calls.map(c=>c.request_id));assert.equal(ids.length,2);assert.equal(ids[0],ids[1]);
 assert.equal(await page.getByRole('button',{name:'该次传输已提交'}).isDisabled(),true);
 await page.evaluate(()=>window.done=true);await page.getByText('工作手机：已送达 · 大小和 SHA-256 校验通过').waitFor();
 assert.equal(await page.getByRole('button',{name:'取消传输'}).count(),0);
 await page.screenshot({path:'var/file-transfer-release/browser-mobile.png',fullPage:true});
 await page.getByRole('button',{name:'关闭',exact:true}).click();assert.equal(await page.locator('dialog').count(),0);assert.deepEqual(errors,[]);
 await browser.close();console.log(JSON.stringify({passed:true,stable_retry:true,verified_delivery:true,mobile:true}));
})().catch(e=>{console.error(e);process.exit(1)});
