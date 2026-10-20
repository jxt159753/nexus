"use strict";
const projectState={preview:null,editor:null,memories:new Map(),after:"0",before:null,todoRetry:null,epoch:0};
function projectReset() {
  chatMemoryReset(); projectState.before=null; projectState.todoRetry=null;
  for (const id of ["project-overview","project-todos","project-tasks"]) $(id).replaceChildren();
  for (const id of ["chat-project","project-workspace"]) $(id).replaceChildren(new Option("不关联项目",""));
  $("project-todo-form").reset(); $("chat-memory-content").value="";
}
function chatMemoryReset() {
  projectState.epoch++; projectState.preview=null; projectState.editor=null; projectState.memories.clear();
  for (const id of ["chat-memory-choices","chat-memory-outbound"]) $(id).replaceChildren();
  $("chat-memory-outbound").hidden=true; $("chat-memory-consent").checked=false; $("chat-memory-consent").disabled=true;
  $("chat-memory-editor").hidden=true; $("chat-memory-more").hidden=true;
}
function memoryInvalidate() {
  projectState.preview=null; $("chat-memory-consent").checked=false; $("chat-memory-consent").disabled=true; $("chat-memory-outbound").hidden=true;
}
function chatMemoryEditor(turn, memory=null) {
  projectState.editor={source:memory?.source || `chat:${turn.turn_id}`,supersedes_id:memory?.memory_id || null,conversation_id:chatState.conversation.conversation_id};
  $("chat-memory-content").value=memory?.content || turn.text;
  $("chat-memory-kind").value=memory?.kind || "fact";
  const until=memory?.valid_until ? new Date(memory.valid_until) : null;
  $("chat-memory-until").value=until ? new Date(until.getTime()-until.getTimezoneOffset()*60000).toISOString().slice(0,16) : "";
  $("chat-memory-save-consent").checked=false;
  $("chat-memory-source").textContent=`来源：${projectState.editor.source}；项目：${state.projects.find(p=>p.project_id===chatState.conversation.project_id)?.name || "不关联项目"}；保存为未经验证的信息${memory ? "，替代旧条目" : ""}。`;
  $("chat-memory-editor").hidden=false; $("chat-memory-editor").parentElement.open=true;
  $("chat-memory-content").focus();
}
async function chatMemoryLoad(more=false) {
  const conversation=chatState.conversation, epoch=projectState.epoch;
  if (!conversation) throw new Error("请先选择会话");
  if (!more) {projectState.memories.clear(); projectState.after="0"; $("chat-memory-choices").replaceChildren(); memoryInvalidate();}
  const value=await api(`/api/v1/memories?limit=100&after=${projectState.after}`+(conversation.project_id ? `&project_id=${conversation.project_id}` : ""));
  if (epoch!==projectState.epoch || conversation.conversation_id!==chatState.conversation?.conversation_id) return;
  for (const memory of value.memories.filter(m=>m.project_id===conversation.project_id)) {
    projectState.memories.set(memory.memory_id,memory);
    const row=el("article",undefined,"record"), label=el("label",undefined,"checkbox"), input=document.createElement("input");
    input.type="checkbox"; input.value=memory.memory_id; input.className="memory-pick"; input.onchange=memoryInvalidate;
    label.append(input,document.createTextNode(`${kinds[memory.kind]} · ${memory.content}`));
    const edit=el("button","修改并重新确认保存","quiet"), retract=el("button","撤销","quiet");
    edit.type=retract.type="button";
    edit.onclick=()=>chatMemoryEditor(null,memory);
    retract.onclick=action(async()=>{await api(`/api/v1/memories/${memory.memory_id}/retract`,{method:"POST",body:"{}"}); await chatMemoryLoad();});
    row.append(label,el("p",`来源：${memory.source} · ${memory.verification} · 有效至：${memory.valid_until || "未设期限"}`,"hint"),edit,retract); $("chat-memory-choices").append(row);
  }
  projectState.after=value.next_after; $("chat-memory-more").hidden=value.memories.length<100;
  if (!projectState.memories.size) $("chat-memory-choices").append(el("p","当前项目没有有效记忆；聊天不会自动保存。","hint"));
}
function memorySubmission() {
  const ids=[...document.querySelectorAll(".memory-pick:checked")].map(n=>n.value);
  if (!ids.length) return {memory_ids:[],memory_digest:null,allow_memory_model:false};
  if (!projectState.preview || !$("chat-memory-consent").checked || JSON.stringify(ids)!==JSON.stringify(projectState.preview.memories.map(m=>m.memory_id))) throw new Error("请预览所选记忆，并逐次同意外发");
  return {memory_ids:ids,memory_digest:projectState.preview.memory_digest,allow_memory_model:true};
}
async function projectOverview(older=false) {
  const id=$("project-workspace").value;
  if (!id) {$("project-overview").replaceChildren(); $("project-todos").replaceChildren(); $("project-tasks").replaceChildren(); return;}
  const value=await api(`/api/v1/projects/${id}`+(older && projectState.before ? `?before=${projectState.before}` : ""));
  if (id!==$("project-workspace").value || !state.logged) return;
  projectState.before=value.next_before; $("project-older-tasks").hidden=value.tasks.length<20;
  const box=$("project-overview"); box.replaceChildren(el("h3",value.project.name),el("p",value.project.description),el("p",Object.entries(value.task_counts).map(([k,v])=>`${statuses[k]} ${v}`).join(" · ") || "暂无执行任务","hint"));
  box.append(el("p","阶段回顾来自以下实际任务结果；待办是建议，不会自动执行。这里只查看记录，不读取项目文件。","hint"));
  for (const conv of value.conversations) {
    const link=el("button",`${conv.archived ? "[已归档] " : ""}${conv.title}`,"quiet");
    link.onclick=action(async()=>{$("chat-project").value=id; $("chat-archived").checked=!!conv.archived; chatState.conversation=null; chatMemoryReset(); await chatList(); if (!chatState.conversations.has(conv.conversation_id)) chatState.conversations.set(conv.conversation_id,conv); await chatSelect(conv.conversation_id); document.querySelector('[data-tab="chat"]').click();}); box.append(link);
  }
  if (!older) $("project-tasks").replaceChildren();
  for (const task of value.tasks) {
    const detail=await api(`/api/v1/tasks/${task.task_id}`);
    if (id!==$("project-workspace").value) return;
    $("project-tasks").append(taskCard({task:detail.task,detail}));
  }
  $("project-todos").replaceChildren(...value.todos.map(todo=>{
    const card=el("article",undefined,"record"), text=document.createElement("input"), status=document.createElement("select"), evidence=document.createElement("select"); text.value=todo.text; text.maxLength=1000;
    text.setAttribute("aria-label","待办内容"); status.setAttribute("aria-label","待办状态"); evidence.setAttribute("aria-label","完成证据任务");
    for (const [key,label] of [["planned","待办 / 建议"],["done","已执行完成（需证据）"],["cancelled","已取消"]]) status.add(new Option(label,key)); status.value=todo.status;
    evidence.add(new Option("选择本项目成功任务",""));
    if (todo.evidence_task_id) evidence.add(new Option(todo.evidence_task_id,todo.evidence_task_id));
    for (const task of value.tasks.filter(t=>t.status==="succeeded" && t.execution_outcome==="known" && t.verification)) if (task.task_id!==todo.evidence_task_id) evidence.add(new Option(task.task_id,task.task_id));
    evidence.value=todo.evidence_task_id || "";
    const save=el("button","保存修改"), history=el("button","修改历史","quiet"); let retry=null;
    save.onclick=action(async()=>{const payload={text:text.value,status:status.value,evidence_task_id:status.value==="done" ? evidence.value || null : null,expected_revision:todo.revision}; const signature=JSON.stringify(payload); if (retry?.signature!==signature) retry={signature,payload:{...payload,request_id:crypto.randomUUID()}}; await api(`/api/v1/projects/${id}/todos/${todo.todo_id}`,{method:"PATCH",body:JSON.stringify(retry.payload)}); await projectOverview();});
    history.onclick=action(async()=>{const response=await api(`/api/v1/projects/${id}/todos/${todo.todo_id}/history`); card.querySelector("pre")?.remove(); card.append(el("pre",JSON.stringify(response.events,null,2)));});
    card.append(text,status,evidence,el("p",`修订 ${todo.revision} · ${new Date(todo.updated_at).toLocaleString()}`,"hint"),save,history); return card;
  }));
}
function initProjects() {
  $("chat-project").onchange=action(async()=>{chatState.epoch++; chatState.conversation=null; chatState.turns.clear(); chatState.retry=null; chatState.createRequest=null; chatMemoryReset(); await chatList();});
  $("chat-memory-load").onclick=action(()=>chatMemoryLoad()); $("chat-memory-more").onclick=action(()=>chatMemoryLoad(true));
  $("chat-memory-preview").onclick=action(async()=>{const ids=[...document.querySelectorAll(".memory-pick:checked")].map(n=>n.value); if (!ids.length) throw new Error("请先选择记忆"); const conversation=chatState.conversation, epoch=projectState.epoch; const value=await api(`/api/v1/chat/conversations/${conversation.conversation_id}/memory-preview`,{method:"POST",body:JSON.stringify({memory_ids:ids})}); if (epoch!==projectState.epoch) return; projectState.preview=value; $("chat-memory-outbound").textContent=JSON.stringify(value.memories,null,2); $("chat-memory-outbound").hidden=false; $("chat-memory-consent").disabled=false; $("chat-memory-consent").checked=false;});
  let saveRetry=null;
  $("chat-memory-save").onclick=action(async()=>{const editor=projectState.editor; if (!editor || editor.conversation_id!==chatState.conversation?.conversation_id || !$("chat-memory-save-consent").checked) throw new Error("请核对并明确同意保存"); const payload={consent:true,content:$("chat-memory-content").value,kind:$("chat-memory-kind").value,source:editor.source,supersedes_id:editor.supersedes_id,project_id:chatState.conversation.project_id,valid_until:$("chat-memory-until").value ? new Date($("chat-memory-until").value).toISOString() : null}; const signature=JSON.stringify(payload); if (saveRetry?.signature!==signature) saveRetry={signature,payload:{...payload,request_id:crypto.randomUUID()}}; await api("/api/v1/memories",{method:"POST",body:JSON.stringify(saveRetry.payload)}); saveRetry=null; $("chat-memory-editor").hidden=true; await chatMemoryLoad(); notify("记忆已保存；尚未发送给模型");});
  for (const id of ["chat-memory-content","chat-memory-kind","chat-memory-until"]) $(id).oninput=()=>{$("chat-memory-save-consent").checked=false;};
  $("project-workspace").onchange=action(()=>projectOverview()); $("project-refresh").onclick=action(()=>projectOverview()); $("project-older-tasks").onclick=action(()=>projectOverview(true));
  $("project-todo-form").onsubmit=action(async()=>{const id=$("project-workspace").value; if (!id) throw new Error("请先选择项目"); const text=$("project-todo-text").value, signature=JSON.stringify([id,text]); if (projectState.todoRetry?.signature!==signature) projectState.todoRetry={signature,payload:{text,request_id:crypto.randomUUID()}}; await api(`/api/v1/projects/${id}/todos`,{method:"POST",body:JSON.stringify(projectState.todoRetry.payload)}); projectState.todoRetry=null; $("project-todo-text").value=""; await projectOverview();});
}
