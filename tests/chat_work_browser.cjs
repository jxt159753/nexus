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
    await page.getByText("会话工作权限",{exact:true}).click();await page.locator("#chat-work-grant").click();
    await page.waitForFunction(()=>document.querySelector("#chat-work-enabled").checked);
    await page.locator("#chat-text").fill("show node status");await page.locator("#chat-send").click();
    await page.waitForFunction(()=>document.querySelectorAll(".chat-turn").length===1 && !document.querySelector("#chat-send").disabled);
    assert.ok((await page.locator(".chat-turn").innerText()).includes("查询节点状态 · 已完成"));
    assert.equal(await page.getByRole("button",{name:"确认执行",exact:true}).count(),0);
    await page.locator("#chat-text").fill("write example file");await page.locator("#chat-send").click();
    await page.waitForFunction(()=>document.querySelector("#chat-send").textContent==="发送本次权限答复");
    assert.ok((await page.locator(".chat-turn").last().innerText()).includes("+approved via natural chat"));
    await page.screenshot({path:input.desktop,fullPage:true});
    await page.reload();await page.locator("#console").waitFor();
    await page.waitForFunction(()=>document.querySelector("#chat-send").textContent==="发送本次权限答复");
    await page.locator("#chat-text").fill("同意");await page.locator("#chat-send").click();
    await page.waitForFunction(()=>document.querySelectorAll(".chat-turn").length===2 && !document.querySelector("#chat-send").disabled && document.querySelector("#chat-send").textContent!=="发送本次权限答复");
    assert.ok((await page.locator(".chat-turn").last().innerText()).includes("你的答复：同意"));
    await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:input.mobile,fullPage:true});
    await page.getByText("会话工作权限",{exact:true}).click();await page.locator("#chat-work-revoke").click();await page.waitForFunction(()=>document.querySelector("#chat-work-enabled").disabled);
    assert.deepEqual(errors,[]);process.stdout.write(JSON.stringify({passed:true,real_node:true,synthetic_native_sse:true,natural_write_approval:true,refresh_recovery:true,viewports:["1440x1000","390x844"],errors,real_model_calls:0}));
  } finally {await browser.close();}
}).on("error",e=>{console.error(e.message);process.exitCode=1;});
process.on("unhandledRejection",e=>{console.error(e.message);process.exitCode=1;});
