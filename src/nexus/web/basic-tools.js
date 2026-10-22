"use strict";
const basicState={nodes:[],preview:null,retry:null};
const toolLabels={get_system_info:"读取系统信息",list_directory:"列出 workspace 目录",read_file:"读取文本文件",find_files:"查找文件",preview_file_write:"预览文件差异",write_file:"写入文件",run_command:"运行受控命令",read_image:"读取工作区图片"};
function basicReset() {
  basicState.nodes=[]; basicState.retry=null; $("basic-tool-form").reset();
  $("basic-command").replaceChildren(new Option("此设备暂无受控命令",""));
  $("basic-tool").onchange();
}
function basicSignature() {return JSON.stringify([$("node").value,$("task-project").value,$("basic-path").value,$("basic-content").value]);}
function basicInvalidate() {basicState.preview=null; $("basic-write-preview").replaceChildren(); $("basic-write-reviewed").checked=false; $("basic-write-confirm").disabled=true;}
function basicNodes(nodes) {
  basicState.nodes=nodes;
  const node=nodes.find(n=>n.node_id===$("node").value), selected=$("basic-command").value;
  $("basic-command").replaceChildren(...Object.keys(node?.command_digests || {}).map(key=>new Option(key,key)));
  if (!$("basic-command").options.length) $("basic-command").add(new Option("此设备暂无受控命令",""));
  if (node?.command_digests?.[selected]) $("basic-command").value=selected;
}
function basicUpdatePreview() {
  const preview=basicState.preview;
  if (!preview || preview.signature!==basicSignature()) {if (preview) basicInvalidate(); return;}
  const task=state.tasks.get(preview.task_id)?.task, box=$("basic-write-preview");
  box.replaceChildren();
  if (!task?.result || task.status!=="succeeded") {box.append(el("p","正在等待预览结果。","hint")); return;}
  box.append(el("pre",task.result.diff || "内容未发生变化"),el("p",`原文件摘要：${task.result.previous_sha256 || "文件尚不存在"}；新摘要：${task.result.content_sha256}${task.result.truncated ? "；差异显示已截断，请同时核对输入框的完整新内容" : ""}`,"hint"));
  $("basic-write-confirm").disabled=false;
}
async function basicSubmit(intent,args) {
  if (!$("node").value) throw new Error("请先选择设备");
  const payload={node_id:$("node").value,request:{intent,arguments:args,project_id:$("task-project").value || null}};
  const signature=JSON.stringify(payload);
  if (basicState.retry?.signature!==signature) basicState.retry={signature,payload:{...payload,request:{...payload.request,request_id:crypto.randomUUID()}}};
  const value=await api("/api/v1/tasks",{method:"POST",body:JSON.stringify(basicState.retry.payload)});
  basicState.retry=null; await updateTask(value.task.task_id); notify(intent==="run_command" ? "命令任务已记录，请在任务卡核对" : "文件操作已记录，正在执行"); return value.task;
}
function initBasicTools() {
  $("basic-tool").onchange=()=>{const intent=$("basic-tool").value; $("basic-path-fields").hidden=["find_files","run_command"].includes(intent); $("basic-search-fields").hidden=intent!=="find_files"; $("basic-write-fields").hidden=intent!=="preview_file_write"; $("basic-command-fields").hidden=intent!=="run_command"; $("basic-write-confirm").hidden=intent!=="preview_file_write"; $("basic-write-reviewed").parentElement.hidden=true; basicInvalidate();};
  $("basic-tool").onchange();
  for (const id of ["basic-path","basic-content"]) $(id).oninput=basicInvalidate;
  $("basic-write-reviewed").onchange=basicUpdatePreview;
  $("basic-tool-form").onsubmit=action(async()=>{const intent=$("basic-tool").value; let args={root_id:"workspace"};
    if (intent==="find_files") args={...args,pattern:$("basic-pattern").value,limit:50,max_depth:4};
    else if (intent==="run_command") {const node=basicState.nodes.find(n=>n.node_id===$("node").value), id=$("basic-command").value; if (!id) throw new Error("此设备没有已配置的受控命令"); args={...args,command_id:id,template_digest:node.command_digests[id]};}
    else args={...args,path:$("basic-path").value,...(intent==="preview_file_write" ? {content:$("basic-content").value} : {max_bytes:8192})};
    const signature=basicSignature(), task=await basicSubmit(intent,args);
    if (intent==="preview_file_write" && signature===basicSignature()) {basicState.preview={task_id:task.task_id,signature}; basicUpdatePreview();}
  });
  $("basic-write-confirm").onclick=action(async()=>{basicUpdatePreview(); if ($("basic-write-confirm").disabled) throw new Error("请先取得预览结果并核对差异"); const task=state.tasks.get(basicState.preview.task_id)?.task;
    await basicSubmit("write_file",{root_id:"workspace",path:$("basic-path").value,content:$("basic-content").value,expected_sha256:task.result.previous_sha256,preview_task_id:task.task_id,preview_digest:task.result.preview_digest}); basicInvalidate();
  });
}
