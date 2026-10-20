"use strict";
const {chromium}=require("playwright"),assert=require("node:assert/strict");
let raw="";process.stdin.setEncoding("utf8");process.stdin.on("data",part=>raw+=part);
process.stdin.on("end",async()=>{
  const input=JSON.parse(raw),browser=await chromium.launch({headless:true,channel:process.env.NEXUS_TEST_BROWSER_CHANNEL || "msedge"});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  try {
    await page.goto(input.origin);await page.locator("#token").fill(input.token);await page.locator('#login-form button').click();await page.locator("#console").waitFor();
    await page.locator("#chat-new").click();await page.waitForFunction(()=>!document.querySelector("#chat-send").disabled);
    await page.waitForFunction(()=>document.querySelector("#chat-work-enabled").checked);
    assert.equal(await page.locator("#chat-work-controls").isVisible(),false);
    await page.locator("#chat-text").fill("show node status");await page.locator("#chat-send").click();
    await page.waitForFunction(()=>document.querySelectorAll(".chat-turn").length===1 && !document.querySelector("#chat-send").disabled);
    assert.ok((await page.locator(".chat-turn").innerText()).includes("查询节点状态 · 已完成"));
    assert.equal(await page.getByRole("button",{name:"确认执行",exact:true}).count(),0);
    await page.locator("#chat-text").fill("list files and write computer information to a new txt");await page.locator("#chat-send").click();
    await page.waitForFunction(()=>document.querySelectorAll(".chat-turn").length===2 && !document.querySelector("#chat-send").disabled);
    assert.ok((await page.locator(".chat-turn").last().innerText()).includes("写入文件 · 已完成"));
    assert.equal(await page.locator("#chat-send").textContent()==="发送本次权限答复",false);
    await page.locator("#chat-text").fill("search public information and read the source page");await page.locator("#chat-send").click();
    await page.waitForFunction(()=>document.querySelectorAll(".chat-turn").length===3 && !document.querySelector("#chat-send").disabled);
    const web=page.locator(".chat-turn").last();assert.ok((await web.innerText()).includes("抓取网页 · 已完成"));
    assert.equal(await web.locator('a[href="https://example.com/"]').count(),2);
    assert.ok((await web.innerText()).includes("网页请求 2 次"));
    await page.screenshot({path:input.desktop,fullPage:true});
    await page.reload();await page.locator("#console").waitFor();
    await page.waitForFunction(()=>document.querySelectorAll(".chat-turn").length===3 && !document.querySelector("#chat-send").disabled && document.querySelector("#chat-send").textContent!=="发送本次权限答复");
    assert.ok((await page.locator(".chat-turn").nth(1).innerText()).includes("写入文件 · 已完成"));
    await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:input.mobile,fullPage:true});
    assert.deepEqual(errors,[]);process.stdout.write(JSON.stringify({passed:true,real_node:true,synthetic_native_sse:true,batch_reads_then_write:true,no_extra_file_approval:true,web_search_fetch_links:true,refresh_recovery:true,viewports:["1440x1000","390x844"],errors,real_model_calls:0,real_web_api_calls:0}));
  } finally {await browser.close();}
}).on("error",e=>{console.error(e.message);process.exitCode=1;});
process.on("unhandledRejection",e=>{console.error(e.message);process.exitCode=1;});
