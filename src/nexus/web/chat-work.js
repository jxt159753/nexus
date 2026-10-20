"use strict";
const chatWorkState={grant:null,catalog:null,permission:null,retry:null,nodes:new Map(),defaultNode:null,webEnabled:false};
function chatWorkspaceReady() {
  const node=chatWorkState.nodes.get($("chat-node").value);
  return !!node && (chatWorkState.webEnabled || node.roots.includes("workspace") && ["filesystem.read@1","filesystem.search@1","filesystem.write@1"].some(tool=>node.tools.includes(tool)));
}
function chatWorkReset() {
  chatWorkState.grant=null; chatWorkState.catalog=null; chatWorkState.permission=null; chatWorkState.retry=null;
  chatWorkState.defaultNode=null;
  $("chat-work-enabled").checked=false; chatWorkRender();
}
function chatWorkRender() {
  const grant=chatWorkState.grant;
  const automatic=chatWorkspaceReady();
  const usable=grant && grant.valid!==false && !grant.revoked_at && new Date(grant.expires_at)>new Date() && grant.used_operations<grant.max_operations && grant.node_id===$("chat-node").value;
  $("chat-work-enabled").disabled=!(usable || automatic);
  if (automatic && chatWorkState.defaultNode!==$("chat-node").value) {$("chat-work-enabled").checked=true;chatWorkState.defaultNode=$("chat-node").value;}
  if (!usable && !automatic) $("chat-work-enabled").checked=false;
  $("chat-work-controls").hidden=automatic;
  $("chat-work-status").textContent=automatic ? "已配置的 workspace 内读写、查找默认可用，无需会话授权或写入确认。路径检查、文件变更检查、执行记录和预算仍生效。" : usable ? `本会话授权有效至 ${new Date(grant.expires_at).toLocaleTimeString()}，已用 ${grant.used_operations}/${grant.max_operations} 次操作。` : "当前设备未配置完整工作区工具；仅已登记的能力可用。";
  $("chat-work-grant").disabled=!chatState.conversation || !!chatState.active || !!chatState.conversation?.archived || !$("chat-node").value;
  $("chat-work-revoke").disabled=!grant || !!grant.revoked_at;
}
function chatWorkActive() {
  return $("chat-tools").checked && $("chat-work-enabled").checked && !$("chat-work-enabled").disabled;
}
async function chatWorkSubmission() {
  if (!chatWorkActive()) return {};
  const directory=await api(`/api/v1/chat/work-catalog?node_id=${$("chat-node").value}`);
  chatWorkState.catalog=directory;
  return chatWorkspaceReady() ? {workspace_work:true,work_digest:directory.work_digest,allow_work_results:true} : {work_grant_id:chatWorkState.grant.grant_id,work_digest:directory.work_digest,allow_work_results:true};
}
function chatWorkCards(turn) {
  const box=el("section",undefined,"chat-action");
  box.append(el("h3","本轮工作记录"));
  const labels={get_node_status:"查询节点状态",get_system_info:"读取系统信息",list_directory:"列出工作区",read_file:"读取文件",find_files:"查找文件",write_file:"写入文件",run_command:"执行固定命令",run_skill:"运行 Skill",run_tool:"运行 Tool",web_search:"搜索网页",web_fetch:"抓取网页"};
  const statuses={pending:"准备执行",waiting:"等待执行结果",permission:"需要你的确认",succeeded:"已完成",failed:"未完成",stopped:"工作已停止，实际执行状态见任务记录"};
  for (const step of turn.work_steps || []) {
    const card=el("section",undefined,"chat-result-preview");
    card.append(el("strong",`${step.step_index}. ${labels[step.proposal.intent] || step.proposal.intent} · ${statuses[step.status]}`));
    const args=step.proposal.arguments,data=step.result?.data,preview=step.result?.preview;
    if (args.path) card.append(el("p",`workspace/${args.path}`));
    if (args.pattern) card.append(el("p",`文件名：${args.pattern}`));
    if (args.command_id) card.append(el("p",`固定命令：${args.command_id}`));
    if (args.query) card.append(el("p",`搜索：${args.query}`));
    if (preview) {
      card.append(el("pre",preview.diff || "文件内容没有变化。"));
      if (preview.truncated) card.append(el("p","差异已截断；确认前请在执行详情核对完整新内容。","hint"));
    }
    if (data && ["web_search","web_fetch"].includes(step.proposal.intent)) {
      for (const source of data.sources || [data]) {
        const link=el("a",source.title || source.url);link.href=source.url;link.target="_blank";link.rel="noopener noreferrer";card.append(link);
        if (source.snippet) card.append(el("p",source.snippet));
      }
      if (data.content) card.append(el("pre",data.content));
      if (data.truncated) card.append(el("p","网页内容已截断。","hint"));
    }
    else if (data && step.proposal.intent==="get_node_status") card.append(el("p",`${data.online ? "在线" : "离线"} · ${data.busy==null ? "忙闲未知" : data.busy ? "忙碌" : "空闲"}${data.heartbeat_age_seconds==null ? "" : ` · ${data.heartbeat_age_seconds} 秒前收到心跳`} · 观测于 ${new Date(data.observed_at).toLocaleTimeString()}`));
    else if (data && "text" in data) card.append(el("pre",data.text),el("p",data.truncated ? "读取结果已截断。" : "已读取文件。","hint"));
    else if (data && "stdout" in data) card.append(el("pre",data.stdout || data.stderr || "命令没有文本输出。"),el("p",`退出码：${data.exit_code}`,"hint"));
    else if (data && step.proposal.intent!=="write_file") card.append(el("pre",JSON.stringify(data,null,2)));
    if (step.request_text) card.append(el("p",step.request_text,"chat-prose"));
    if (step.result?.error_code) card.append(el("p",`本次工具未完成：${step.result.error_code}`,"hint"));
    if (step.permission_text) card.append(el("p",`你的答复：${step.permission_text}`));
    const details=el("details");details.append(el("summary","查看执行详情"),el("pre",JSON.stringify({action:step.proposal,result:step.result},null,2)));
    for (const id of [step.preview_task_id,step.task_id]) if (id && state.tasks.has(id)) details.append(taskCard(state.tasks.get(id)));
    card.append(details);
    if (step.skill_run_id) {
      const button=el("button","查看流程实际进度","quiet");
      button.onclick=action(async()=>{const value=await api(`/api/v1/extensions/runs/${step.skill_run_id}`);button.replaceWith(el("pre",JSON.stringify(value,null,2)));});
      card.append(button);
    }
    box.append(card);
  }
  return box;
}
async function chatWorkAnswer(turn) {
  const step=turn.work_steps.find(s=>s.status==="permission");
  if (!step) throw new Error("待确认动作已变化，请刷新后重试");
  const signature=JSON.stringify([turn.turn_id,step.step_id,turn.revision,$("chat-text").value]);
  if (chatWorkState.retry?.signature!==signature) chatWorkState.retry={signature,payload:{request_id:crypto.randomUUID(),step_id:step.step_id,expected_revision:turn.revision,text:$("chat-text").value}};
  await api(`/api/v1/chat/turns/${turn.turn_id}/permission`,{method:"POST",body:JSON.stringify(chatWorkState.retry.payload)});
  chatWorkState.retry=null; $("chat-text").value=""; await chatHistory();
}
function initChatWork() {
  $("chat-work-grant").onclick=action(async()=>{
    const conv=chatState.conversation;
    if (!conv) return;
    const directory=await api(`/api/v1/chat/work-catalog?node_id=${$("chat-node").value}`);
    $("chat-work-catalog").textContent=JSON.stringify(directory.catalog,null,2);
    const value=await api(`/api/v1/chat/conversations/${conv.conversation_id}/work-grant`,{method:"POST",body:JSON.stringify({request_id:crypto.randomUUID(),node_id:$("chat-node").value,profile:$("chat-work-profile").value,consent:true,duration_minutes:30,max_operations:20})});
    chatWorkState.grant=value.grant; chatWorkState.catalog=directory; chatWorkRender();
    $("chat-work-enabled").checked=true; $("chat-tools").checked=true; $("chat-skills").checked=false; chatRender();
  });
  $("chat-work-revoke").onclick=action(async()=>{if (!chatState.conversation) return; await api(`/api/v1/chat/conversations/${chatState.conversation.conversation_id}/work-grant`,{method:"DELETE",body:"{}"});chatWorkReset();await chatHistory();});
  $("chat-work-enabled").onchange=()=>chatRender();
}
