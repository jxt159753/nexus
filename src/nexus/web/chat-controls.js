"use strict";
const chatReferenceState={snapshot:null,conversation:null};
const chatControlState={retry:null};
function chatReferenceReset() {
  chatReferenceState.snapshot=null;chatReferenceState.conversation=null;
  $("chat-reference-consent").checked=false;$("chat-reference-consent").disabled=true;
  $("chat-reference-outbound").hidden=true;
}
function chatControlFields() {
  const current=chatState.turns.get(chatState.active),mode=$("chat-input-mode");
  mode.hidden=!current;
  $("chat-compact").disabled=!!current || !chatState.conversation || !!chatState.conversation.archived;
  mode.options[1].disabled=!current?.work_grant_id || !!current?.recovery_required;
  if (mode.value==="steer" && mode.options[1].disabled) mode.value="queue";
  $("chat-reference-consent").disabled=!chatReferenceState.snapshot || chatReferenceState.conversation!==chatState.conversation?.conversation_id;
}
function chatReferenceSubmission() {
  if (!$("chat-reference-consent").checked) return {};
  if (!chatReferenceState.snapshot || chatReferenceState.conversation!==chatState.conversation?.conversation_id) throw new Error("请重新预览历史资料");
  return {reference_turn_ids:chatReferenceState.snapshot.references.map(item=>item.turn_id),references_digest:chatReferenceState.snapshot.references_digest,allow_reference_model:true};
}
function chatJournalButton(turn,answer) {
  if (turn.context_compaction_status) {
    const text={started:"正在压缩较早历史，原文保留…",completed:"本轮已生成上下文摘要，原文和费用记录保留。",failed:"上下文压缩失败，原文未替换。",stopped:"上下文压缩已停止。",interrupted:"上下文压缩被中断；未自动重试。"};
    answer.append(el("p",text[turn.context_compaction_status],"hint"));
  }
  const button=el("button","查看逐次请求、回复与中断记录","quiet");
  button.onclick=action(async()=>{const value=await api(`/api/v1/chat/turns/${turn.turn_id}/journal`);const details=el("details");details.open=true;details.append(el("summary","逐次交换记录（不含认证请求头）"),el("pre",JSON.stringify(value,null,2)));button.replaceWith(details);});
  answer.append(button);
  if (turn.queue_after_turn_id && turn.status==="queued") {
    answer.append(el("p","已排队；前序工作及其执行停止确认完成后运行。发送时选定的上下文保持固定。","hint"));
    const cancel=el("button","取消这条排队消息","quiet");cancel.onclick=action(async()=>{await api(`/api/v1/chat/turns/${turn.turn_id}/stop`,{method:"POST",body:"{}"});await chatHistory();});answer.append(cancel);
  }
  if (turn.recovery_required) {
    answer.append(el("p","重启后已暂停本轮工作。先核对原任务结果；未知执行、未结束的尝试或过期权限会阻止继续。","hint"));
    const resume=el("button","核对后继续本轮","primary");
    resume.onclick=action(async()=>{await api(`/api/v1/chat/turns/${turn.turn_id}/resume`,{method:"POST",body:JSON.stringify({request_id:crypto.randomUUID(),consent:true})});await chatHistory();});answer.append(resume);
  }
  if (turn.references?.length) {
    const references=el("details");references.append(el("summary","本轮明确引用的历史资料"),el("pre",JSON.stringify(turn.references,null,2)));answer.append(references);
  }
}
function initChatControls() {
  $("chat-compact").onclick=()=>{ $("chat-text").value="/compact";$("chat-context").checked=true;$("chat-compaction").checked=true;$("chat-text").focus();notify("点击发送将压缩本会话可用历史，费用纳入原上限，原文保留。"); };
  $("chat-input-mode").onchange=()=>chatRender();
  $("chat-reference-ids").oninput=()=>{chatReferenceState.snapshot=null;$("chat-reference-consent").checked=false;chatControlFields();};
  $("chat-reference-preview").onclick=action(async()=>{
    if (!chatState.conversation) throw new Error("请先选择会话");
    const ids=$("chat-reference-ids").value.split(/[\s,，]+/).filter(Boolean);
    if (!ids.length || ids.length>3) throw new Error("请输入 1–3 个历史轮次 ID");
    const value=await api(`/api/v1/chat/conversations/${chatState.conversation.conversation_id}/reference-preview`,{method:"POST",body:JSON.stringify({turn_ids:ids})});
    chatReferenceState.snapshot=value;chatReferenceState.conversation=chatState.conversation.conversation_id;
    $("chat-reference-outbound").textContent=JSON.stringify(value.references,null,2);$("chat-reference-outbound").hidden=false;$("chat-reference-consent").checked=false;chatControlFields();
  });
}
