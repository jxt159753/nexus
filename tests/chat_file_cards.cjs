const {chromium}=require(process.env.NEXUS_PLAYWRIGHT);
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'}),page=await browser.newPage();const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const raw=Buffer.from('<script>window.evil=1</script>\n原始文件内容\r\n');
 await page.route('http://127.0.0.1:39872/**',route=>{
  const path=new URL(route.request().url()).pathname;
  if(path.startsWith('/api/'))return route.fulfill({contentType:'application/octet-stream',body:raw});
  if(path.endsWith('.js'))return route.fulfill({contentType:'text/javascript',body:fs.readFileSync('src/nexus/web/app'+path)});
  return route.fulfill({contentType:'text/html',body:'<!doctype html><html lang=zh><meta charset="utf-8"><body style="background:#10151d;color:#eee;font:15px/1.5 system-ui;margin:24px;max-width:760px"><h2>NEXUS · 文件已送达</h2></body></html>'});
 });
 await page.goto('http://127.0.0.1:39872/');
 await page.evaluate(async({sha,size,text})=>{
  const {renderChatFiles}=await import('/chat-files.js');
  const cid='11111111-1111-4111-8111-111111111111',aid='22222222-2222-4222-8222-222222222222';
  window.file={conversation_id:cid,source_kind:'attachment',source_id:aid,filename:'demo.txt',size_bytes:size,sha256:sha,status:'已送达设备',download_url:`/api/v1/chat/conversations/${cid}/attachments/${aid}/content`,preview_url:`/api/v1/chat/conversations/${cid}/files/attachment/${aid}/preview`};
  window.testAPI=async path=>{if(path.endsWith('/preview'))return {preview:{kind:'text',text,truncated:false}};throw Error('Unexpected route');};
  document.body.append(renderChatFiles([window.file],window.testAPI,()=>crypto.randomUUID()));
 },{sha:crypto.createHash('sha256').update(raw).digest('hex'),size:raw.length,text:raw.toString()});
 await page.getByRole('button',{name:'打开预览',exact:true}).click();
 assert.equal(await page.locator('dialog pre').textContent(),raw.toString());assert.equal(await page.evaluate(()=>window.evil),undefined);
 await page.screenshot({path:'var/file-chat-release/cards-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:'var/file-chat-release/cards-mobile.png',fullPage:true});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await page.getByRole('button',{name:'关闭',exact:true}).click();
 const event=page.waitForEvent('download');await page.getByRole('button',{name:'下载原件',exact:true}).click();const download=await event;
 assert.equal(download.suggestedFilename(),'demo.txt');await download.saveAs('var/file-chat-release/downloaded-demo.txt');assert.deepEqual(fs.readFileSync('var/file-chat-release/downloaded-demo.txt'),raw);
 // Exercise the actual native bridge response shape with the same immutable bytes.
 await page.evaluate(({base64})=>{window.__NEXUS_NATIVE__={attachmentRequest:async()=>({status:200,body:JSON.stringify({data_base64:base64})})};},{base64:raw.toString('base64')});
 const nativeEvent=page.waitForEvent('download');await page.getByRole('button',{name:'下载原件',exact:true}).click();await nativeEvent;
 assert.deepEqual(errors,[]);await browser.close();console.log(JSON.stringify({passed:true,preview_safe:true,download_verified:true,native_bridge:true,mobile:true}));
})().catch(e=>{console.error(e);process.exit(1)});
