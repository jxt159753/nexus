const {chromium}=require(process.env.NEXUS_PLAYWRIGHT);const assert=require('node:assert/strict');
let raw='';process.stdin.setEncoding('utf8');process.stdin.on('data',p=>raw+=p);process.stdin.on('end',async()=>{
 const input=JSON.parse(raw);const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(input.origin+'/app');await page.locator('#token').fill(input.token);await page.locator('#login-form button').click();
  await page.locator('#settings-button').click();await page.getByRole('button',{name:'文件传输',exact:true}).click();
  await page.getByLabel('选择要发送的文件').setInputFiles({name:'浏览器发送.bin',mimeType:'application/octet-stream',buffer:Buffer.alloc(2*1024*1024+1,0x97)});
  await page.getByRole('button',{name:'上传文件',exact:true}).click();
  await page.getByText('已上传。点击文件旁的按钮，选择设备发送或查看送达记录。',{exact:true}).waitFor().catch(async error=>{console.error(await page.locator('dialog').allTextContents());throw error;});
  await page.locator('p').filter({hasText:'浏览器发送.bin'}).getByRole('button',{name:'发送到设备 / 查看记录',exact:true}).click();
  await page.getByRole('button',{name:'发送到所选设备',exact:true}).click();
  await page.getByText(/已送达 · 大小和 SHA-256 校验通过/).waitFor({timeout:25000});
  await page.screenshot({path:'var/file-transfer-release/live-browser-desktop.png'});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:'var/file-transfer-release/live-browser-mobile.png'});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,actual_http_upload:true,actual_node_delivery:true,bytes:2*1024*1024+1}));
 }finally{await browser.close();}
}).on('error',e=>{console.error(e);process.exit(1)});
process.on('unhandledRejection',e=>{console.error(e);process.exit(1)});
