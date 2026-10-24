"use strict";

const chatSkillState={catalog:null,node:null,request:0,drafts:new Map(),busy:new Set()};
const skillLabels={running:"流程执行中",cancelling:"正在取消，等待执行停止",succeeded:"流程已完成",failed:"流程失败",cancelled:"流程已取消",timed_out:"流程已超时",pending:"尚未开始",dispatchable:"等待设备执行",approval:"等待本次目录审批",unknown:"执行结果未知，正在核对"};
const skillStepLabels={running:"执行中",succeeded:"已验证",failed:"失败",cancelled:"已取消",timed_out:"已超时"};
const skillToolLabels={"system.info":"读取系统信息","filesystem.list":"列出 workspace 直接条目"};

function chatSkillsReset() {
  chatSkillState.catalog=null; chatSkillState.node=null; chatSkillState.request++;
  chatSkillState.drafts.clear(); chatSkillState.busy.clear();
  $("chat-skills").checked=false; $("chat-skills").disabled=true;
  $("chat-skills-catalog").replaceChildren();
}
function chatSkillsInvalidate() {
  chatSkillState.catalog=null; chatSkillState.node=null; chatSkillState.request++;
  $("chat-skills").checked=false; $("chat-skills").disabled=true;
  $("chat-skills-catalog").replaceChildren(el("p","设备或工具选项已变化，请重新查看可用流程。","hint"));
}
function initChatSkills() {
  $("chat-tools").addEventListener("change",chatSkillsInvalidate);
  $("chat-skill-load").onclick=action(async()=>{
    const epoch=chatState.epoch;
    const value=await api("/api/v1/chat/work-catalog");
    if (!state.logged || epoch!==chatState.epoch) return;
    const skills=(value.catalog || []).filter(item=>(item.route_intent || item.name)==="run_skill");
    const box=$("chat-skills-catalog");
    box.replaceChildren(el("p","流程随工作工具目录提供给 Core，由 Core 选择已授权设备。","hint"),el("pre",JSON.stringify(skills,null,2)));
  });
}

function chatSkillCard(turn, canContinue) {
  const plan=turn.skill_plan, run=turn.skill_run, card=el("section",undefined,"chat-action chat-skill");
  card.append(el("h3",`多步流程 · ${plan.capability_id} · r${plan.revision}`),el("p",`设备：${plan.node_id} · 最多 ${plan.steps.length} 步 · 有效至 ${new Date(plan.expires_at).toLocaleTimeString()}`,"hint"));
  const steps=el("ol",undefined,"skill-steps");
  for (const definition of plan.steps) {
    const step=run?.steps.find(s=>s.step_id===definition.id), item=el("li");
    const task=step?.task_id ? state.tasks.get(step.task_id) : null;
    const stepStatus=task && ["failed","cancelled","timed_out"].includes(task.task.status) ? task.task.status : step?.status;
    item.append(el("strong",skillToolLabels[definition.tool] || definition.tool || "已批准的 JSON 工具"));
    item.append(el("p",`${definition.id} · ${step ? (skillStepLabels[stepStatus] || skillLabels[stepStatus] || stepStatus) : definition.approval_required ? "执行时另需目录审批" : "确认后检查权限"}`,"hint"));
    const detail=el("details"); detail.append(el("summary","设备与数据范围"),el("pre",JSON.stringify({node_id:definition.node_id,arguments:definition.arguments,tool_version:definition.tool_version,tool_version_id:definition.tool_version_id},null,2))); item.append(detail);
    if (task) item.append(taskCard(task));
    else if (step?.result) item.append(el("pre",JSON.stringify(step.result,null,2)));
    if (step?.tool_execution?.error) item.append(el("p",`工具状态：${step.tool_execution.status} · ${step.tool_execution.error.code}`,"hint"));
    steps.append(item);
  }
  card.append(steps);
  const metadata=el("details"); metadata.append(el("summary","固定版本与确认内容"),el("pre",JSON.stringify({version_id:plan.version_id,artifact_digest:plan.artifact_digest,input:plan.input,proposal_digest:turn.proposal_digest},null,2))); card.append(metadata);
  if (run) {
    card.append(el("p",`${skillLabels[run.status] || run.status}${run.error ? " · "+run.error.code : ""}`,"hint"));
    if (["running","cancelling"].includes(run.status)) {
      const cancel=el("button",run.cancel_requested ? "已请求取消，等待核对" : "取消整个流程","danger");
      cancel.disabled=!!run.cancel_requested || chatSkillState.busy.has(turn.turn_id);
      cancel.onclick=action(async()=>{
        chatSkillState.busy.add(turn.turn_id); cancel.disabled=true;
        try {await api(`/api/v1/extensions/runs/${run.run_id}/cancel`,{method:"POST",body:"{}"}); await chatHistory();}
        finally {chatSkillState.busy.delete(turn.turn_id); chatRender();}
      }); card.append(cancel);
    }
    if (run.status==="succeeded") {
      card.append(el("p","所有步骤已有验证结果。目录被截断时，统计仅覆盖已返回条目。","hint"));
      if (turn.phase==="reply" && turn.result_supported && !chatState.conversation?.archived) chatResultPreview(turn,card,canContinue);
    } else card.append(el("p","以步骤和执行记录为准；未完成或未知结果不会发送给模型总结。","hint"));
    return card;
  }
  if (turn.action_status!=="proposed" || new Date(plan.expires_at)<=new Date() || chatState.conversation?.archived || !canContinue) {
    card.append(el("p",turn.action_status==="rejected" ? "你已拒绝此流程，未创建执行。" : "此计划已过期或会话已变化，请重新提出目标。","hint")); return card;
  }
  const form=el("form",undefined,"skill-parameters"), inputs=[];
  const draft=chatSkillState.drafts.get(turn.turn_id);
  let dirty=!!draft;
  const confirm=el("button","确认以上计划并执行","primary"); confirm.type="button";
  confirm.disabled=!plan.ready || dirty || chatSkillState.busy.has(turn.turn_id);
  for (const [name,schema] of Object.entries(plan.input_schema.properties)) {
    const label=el("label",schema.title || name), id=`skill-${turn.turn_id}-${inputs.length}`;
    label.htmlFor=id;
    const value=draft?.[name] ?? plan.input[name]; let input;
    if (schema.enum) {input=el("select"); input.add(new Option("请选择","")); schema.enum.forEach((v,i)=>input.add(new Option(String(v),String(i)))); input.value=draft?.[name] ?? (value===undefined ? "" : String(schema.enum.findIndex(v=>JSON.stringify(v)===JSON.stringify(value))));}
    else if (schema.type==="boolean") {input=el("select"); input.add(new Option("请选择","")); input.add(new Option("是","true")); input.add(new Option("否","false")); input.value=value===undefined ? "" : String(value);}
    else if (["object","array","null"].includes(schema.type)) {input=el("textarea"); input.rows=3; input.value=typeof value==="string" ? value : value===undefined ? "" : JSON.stringify(value,null,2);}
    else {input=el("input"); input.type=schema.type==="integer" ? "number" : "text"; input.value=value ?? ""; if (schema.minimum!==undefined) input.min=schema.minimum; if (schema.maximum!==undefined) input.max=schema.maximum; if (schema.maxLength!==undefined) input.maxLength=schema.maxLength;}
    input.id=id; input.required=(plan.input_schema.required || []).includes(name); inputs.push({name,schema,input});
    input.oninput=()=>{dirty=true; confirm.disabled=true; chatSkillState.drafts.set(turn.turn_id,Object.fromEntries(inputs.map(v=>[v.name,v.input.value])));};
    form.append(label,input); if (schema.description) form.append(el("p",schema.description,"hint"));
  }
  const preview=el("button","核对参数并更新计划","quiet"); preview.type="submit";
  form.onsubmit=action(async()=>{
    const input={};
    for (const field of inputs) {
      const value=field.input.value;
      if (value==="" && !field.input.required) continue;
      input[field.name]=field.schema.enum ? field.schema.enum[Number(value)] : field.schema.type==="integer" ? Number(value) : field.schema.type==="boolean" ? value==="true" : ["object","array","null"].includes(field.schema.type) ? JSON.parse(value) : value;
    }
    preview.disabled=true;
    try {await api(`/api/v1/chat/turns/${turn.turn_id}/plan-preview`,{method:"POST",body:JSON.stringify({input})}); chatSkillState.drafts.delete(turn.turn_id); await chatHistory();}
    finally {preview.disabled=false;}
  });
  confirm.onclick=action(async()=>{
    if (dirty || !plan.ready || chatSkillState.busy.has(turn.turn_id)) return;
    chatSkillState.busy.add(turn.turn_id); chatState.confirming=turn.turn_id; confirm.disabled=true;
    try {await api(`/api/v1/chat/turns/${turn.turn_id}/action`,{method:"POST",body:JSON.stringify({proposal_digest:turn.proposal_digest,decision:"confirm"})}); await chatHistory();}
    finally {chatSkillState.busy.delete(turn.turn_id); chatState.confirming=null; chatRender();}
  });
  const reject=el("button","不执行","quiet"); reject.type="button";
  reject.onclick=action(async()=>{reject.disabled=true; await api(`/api/v1/chat/turns/${turn.turn_id}/action`,{method:"POST",body:JSON.stringify({proposal_digest:turn.proposal_digest,decision:"reject"})}); await chatHistory();});
  const buttons=el("div",undefined,"button-row"); buttons.append(preview,confirm,reject);
  form.append(el("p",plan.ready ? "修改参数后须重新核对计划，旧确认失效。" : "请补齐参数并更新计划；确认前不会执行。","hint"),buttons);
  card.append(form,el("p",plan.result_policy,"hint")); return card;
}
