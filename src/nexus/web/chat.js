"use strict";
const chatState = {conversation:null, conversations:new Map(), turns:new Map(), previews:new Map(), actionConsents:new Set(), confirming:null, active:null, retry:null, createRequest:null, before:null, listBefore:null, latest:true, loading:false, enabled:false, epoch:0};
const chatPresentationState = new Map();
const chatDrafts = new Map(), chatNavigations = new Set();
let chatSentRequest = null;
async function chatOpenSource(id) {
  if (chatAttachmentSelection().length) throw new Error("请先发送或移除未发送附件，再打开来源会话。文字草稿会保留。");
  const value = await api(`/api/v1/chat/conversations/${id}/turns`);
  chatState.conversations.set(id, value.conversation);
  if (![...$("chat-conversation").options].some(option => option.value === id)) $("chat-conversation").append(new Option(value.conversation.title,id));
  $("chat-conversation").value = id;
  await chatSelect(id);
}
const chatLabels = {queued:"等待回复",generating:"正在回复",completed:"已完成",incomplete:"达到输出上限，回复未完整结束",stopping:"正在停止",stopped:"已停止",interrupted:"服务中断，未自动重发",failed:"回复失败",expired:"请求已过期"};
function chatNodes(nodes) {
  chatWorkState.nodes=new Map(nodes.map(node=>[node.node_id,node]));

}
function chatReset() {
  chatDrafts.clear(); chatNavigations.clear(); chatSentRequest = null;
  chatWorkReset();
  chatMemoryReset(); chatReferenceReset();
  chatState.epoch++; chatState.conversation=null; chatState.conversations.clear(); chatState.turns.clear(); chatState.previews.clear(); chatState.actionConsents.clear(); chatState.confirming=null; chatState.active=null; chatState.retry=null; chatState.createRequest=null; chatState.before=null; chatState.listBefore=null; chatState.latest=true;
  $("chat-messages").replaceChildren(); $("chat-conversation").replaceChildren(new Option("新建一个会话开始", "")); $("chat-form").reset(); $("chat-title-form").reset(); $("chat-send").disabled=true; $("chat-stop").disabled=true;
  chatSkillsReset();
}
function chatAvailability(enabled,webEnabled=false) {
  chatState.enabled=enabled;
  chatWorkState.webEnabled=webEnabled;
  $("chat-availability").textContent=enabled ? "DeepSeek · 可连续追问，聊天记录保存在 Core" : "聊天模型尚未启用；已有会话仍可查看。请在 Core 显式启用聊天配置。";
  $("chat-send").disabled=!enabled || !chatState.conversation || !!chatState.conversation.archived || !!chatState.active || !!chatState.confirming;
}
async function chatList(older=false) {
  const epoch=chatState.epoch, archived=$("chat-archived").checked;
  const project=$("chat-project").value;
  const value=await api(`/api/v1/chat/conversations?archived=${archived}&project_only=true`+(project ? `&project_id=${project}` : "")+(older && chatState.listBefore ? `&before=${chatState.listBefore}` : ""));
  if (!state.logged || epoch!==chatState.epoch || archived!==$("chat-archived").checked) return;
  if (!older) chatState.conversations.clear();
  value.conversations.forEach(c=>chatState.conversations.set(c.conversation_id,c));
  const selected=chatState.conversation?.conversation_id;
  $("chat-conversation").replaceChildren(...[...chatState.conversations.values()].map(c=>new Option(c.title,c.conversation_id)));
  chatState.listBefore=value.conversations.at(-1)?.cursor;
  $("chat-more-conversations").hidden=value.conversations.length<20;
  const id=chatState.conversations.has(selected) ? selected : $("chat-conversation").value;
  if (id) {$("chat-conversation").value=id; await chatSelect(id);} else {chatState.conversation=null; chatState.turns.clear(); chatState.active=null; chatRender();}
}
async function chatSelect(id) {
  if (chatState.conversation?.conversation_id!==id) {
    chatDrafts.set(chatState.conversation?.conversation_id || "", $("chat-text").value);
    chatWorkReset();
    chatMemoryReset(); chatReferenceReset();
    chatState.epoch++; chatState.turns.clear(); chatState.previews.clear(); chatState.actionConsents.clear(); chatState.retry=null; chatState.before=null; chatState.latest=true; $("chat-context").checked=true; $("chat-text").value=chatDrafts.get(id) || "";
    chatSkillsReset();
  }
  chatState.conversation=chatState.conversations.get(id);
  await chatHistory();
}
async function chatHistory(older=false) {
  const conversation=chatState.conversation;
  if (!conversation) return;
  const epoch=chatState.epoch;
  const value=await api(`/api/v1/chat/conversations/${conversation.conversation_id}/turns`+(older && chatState.before ? `?before=${chatState.before}` : ""));
  if (!state.logged || epoch!==chatState.epoch || chatState.conversation?.conversation_id!==conversation.conversation_id) return;
  if (value.conversation.revision>=chatState.conversation.revision) chatState.conversation=value.conversation;
  const wasActive=chatState.active;
  chatState.active=value.active_turn_id;
  chatWorkState.grant=value.work_grant;
  chatState.conversations.set(conversation.conversation_id,chatState.conversation);
  const option=[...$("chat-conversation").options].find(o=>o.value===conversation.conversation_id);
  if (option) option.textContent=chatState.conversation.title;
  if (older) chatState.latest=false;
  for (const turn of value.turns) {
    const previous=chatState.turns.get(turn.turn_id);
    if (!previous || turn.revision>=previous.revision) chatState.turns.set(turn.turn_id,turn);
    if (turn.task_id && (!state.tasks.has(turn.task_id) || (!terminal.has(state.tasks.get(turn.task_id).task.status) && state.socket?.readyState!==WebSocket.OPEN))) await updateTask(turn.task_id);
    for (const step of turn.skill_run?.steps || []) {
      if (step.task_id && (!state.tasks.has(step.task_id) || !terminal.has(state.tasks.get(step.task_id).task.status))) await updateTask(step.task_id);
    }
    for (const step of turn.work_steps || []) {
      for (const id of [step.preview_task_id,step.task_id]) {
        if (id && (!state.tasks.has(id) || !terminal.has(state.tasks.get(id).task.status))) await updateTask(id);
      }
    }
    if (chatState.retry?.payload.request_id===turn.request_id) {
      if ($("chat-text").value===chatState.retry.payload.text) $("chat-text").value="";
      chatState.retry=null;
      chatMemoryReset(); chatReferenceReset();
    }
  }
  const ordered=[...chatState.turns.values()].sort((a,b)=>a.cursor-b.cursor);
  while (ordered.length>100) {const removed=chatState.latest ? ordered.shift() : ordered.pop(); chatState.turns.delete(removed.turn_id);}
  chatState.before=ordered[0]?.cursor;
  $("chat-older").hidden=value.turns.length<20;
  $("chat-latest").hidden=chatState.latest;
  chatRender();
  if (wasActive && !chatState.active) await usage();
  const routed = ordered.find(turn => turn.request_id === chatSentRequest && turn.presentation?.phase === "completed" && turn.presentation?.navigation);
  const navigation = routed?.presentation.navigation;
  if (navigation && !chatNavigations.has(navigation.step_id)) {
    chatNavigations.add(navigation.step_id);
    await chatOpenSource(navigation.conversation_id);
  }
}
function chatContent(text) {
  const fragment=document.createDocumentFragment();
  const pattern=/```[^\n]*\n([\s\S]*?)(?:```|$)/g;
  let match, offset=0;
  while ((match=pattern.exec(text))) {
    if (match.index>offset) fragment.append(el("div",text.slice(offset,match.index),"chat-prose"));
    const pre=el("pre"); pre.append(el("code",match[1])); fragment.append(pre); offset=pattern.lastIndex;
  }
  if (offset<text.length) fragment.append(el("div",text.slice(offset),"chat-prose"));
  return fragment;
}
function chatResultPreview(turn, card, canContinue) {
  const value=chatState.previews.get(turn.turn_id), box=el("section",undefined,"chat-result-preview");
  const load=el("button",value ? "更新所选字段预览" : "预览可发送的结果","quiet");
  const choices=[];
  const send=el("button","同意发送这些结果给 DeepSeek 并生成最终回答","primary");
  send.disabled=!canContinue || !value || value.expected_revision!==chatState.conversation?.revision || new Date(value.expires_at)<=new Date();
  load.disabled=!canContinue;
  if (!canContinue) box.append(el("p","本次结果尚未发送给模型。已有后续消息或活动轮次，本轮不能再追加回答。","hint"));
  if (value) {
    const labels={os_name:"操作系统",os_version:"系统版本",architecture:"处理器架构",python_version:"Python 版本",root_id:"工作区别名",entries:"目录条目名称和类型",truncated:"是否截断"};
    box.append(el("p","选择本次外发字段；改选后请更新预览。原输入及当时所选上下文也将再次发送。","hint"));
    for (const field of value.available_fields) {
      const label=el("label",undefined,"checkbox"), input=document.createElement("input"); input.type="checkbox"; input.checked=value.fields.includes(field); input.value=field;
      input.onchange=()=>{send.disabled=true;}; choices.push(input); label.append(input,document.createTextNode(labels[field] || `步骤 ${field} 的白名单结果`)); box.append(label);
    }
    box.append(el("pre",JSON.stringify(value.data,null,2)),el("p",`以上为实际外发结果；有效至 ${new Date(value.expires_at).toLocaleTimeString()}。最多再调用一次，与本轮已用费用共用 $0.05 上限。`,"hint"));
  }
  load.onclick=action(async()=>{
    load.disabled=true;
    try {
      const fields=choices.length ? choices.filter(c=>c.checked).map(c=>c.value) : null;
      const reply=await api(`/api/v1/chat/turns/${turn.turn_id}/result-preview`,{method:"POST",body:JSON.stringify({fields})});
      chatState.previews.set(turn.turn_id,reply.preview); await chatHistory();
    } finally {load.disabled=false;}
  });
  send.onclick=action(async()=>{
    send.disabled=true;
    try {
      await api(`/api/v1/chat/turns/${turn.turn_id}/result-consent`,{method:"POST",body:JSON.stringify({result_digest:value.result_digest,expected_revision:value.expected_revision,allow_model:true})});
      chatState.previews.delete(turn.turn_id); await chatHistory();
    } finally {if (send.isConnected) send.disabled=false;}
  });
  box.append(load); if (value) box.append(send); card.append(box);
}
function chatPresentationPanel(turn,p) {
  let ui=chatPresentationState.get(turn.turn_id);
  if (!ui) {
    ui={open:!(p.phase==="completed" && p.collapse_process && p.final_text!==null),manual:false,phase:p.phase};
    chatPresentationState.set(turn.turn_id,ui);
  } else if (ui.phase!==p.phase) {
    if (p.phase==="completed" && !ui.manual) ui.open=!(p.collapse_process && p.final_text!==null);
    ui.phase=p.phase;
  }
  if (p.phase==="awaiting_input") ui.open=true;
  const details=el("details","chat-process");
  details.open=ui.open;
  const summary=el("summary",ui.open?"收起处理过程":"查看处理过程");
  details.append(summary);
  details.addEventListener("toggle",()=>{ui.open=details.open;ui.manual=true;summary.textContent=details.open?"收起处理过程":"查看处理过程";});
  for (const item of p.process || []) {
    const box=el("div",undefined,"chat-process-text"); box.append(chatContent(item.text || "")); details.append(box);
    if (item.truncated) details.append(el("p","这条摘要已裁剪，完整内容保留在 Core 记录中。","hint"));
  }
  if (p.process_truncated) details.append(el("p","较早的公开摘要已裁剪；完整记录仍在 Core 的执行日志中。","hint"));
  if (p.live_text) {
    details.append(el("p",p.phase==="responding" ? "正在生成" : "已保留的回复片段","hint"));
    const live=el("div",undefined,"chat-process-text"); live.append(chatContent(p.live_text)); details.append(live);
  }
  return details;
}
function chatRender() {
  const log=$("chat-messages"), nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<90;
  const existing=new Map([...log.querySelectorAll("[data-turn-id]")].map(n=>[n.dataset.turnId,n]));
  const turns=[...chatState.turns.values()].sort((a,b)=>a.cursor-b.cursor), keep=new Set();
  if (turns.length) log.querySelectorAll(".chat-empty").forEach(n=>n.remove());
  for (const turn of turns) {
    keep.add(turn.turn_id); let article=existing.get(turn.turn_id);
    if (!article) {article=el("article",undefined,"chat-turn"); article.dataset.turnId=turn.turn_id;}
    const task=turn.task_id ? state.tasks.get(turn.task_id) : null;
    const skillRevision=JSON.stringify([turn.skill_run?.revision,turn.skill_run?.steps.map(s=>[s.status,state.tasks.get(s.task_id)?.task.revision,s.tool_execution])]);
    const revision=`${turn.revision}:${JSON.stringify(turn.presentation || null)}:${task?.task.revision ?? ""}:${chatState.conversation?.revision}:${chatState.previews.get(turn.turn_id)?.result_digest ?? ""}:${skillRevision}:${chatSkillState.busy.has(turn.turn_id)}`;
    if (article.dataset.revision!==revision) {
      article.dataset.revision=revision;
      const question=el("div",undefined,"chat-user"), answer=el("div",undefined,"chat-assistant");
      const presentation=turn.presentation?.version===1 ? turn.presentation : null;
      question.append(el("strong","你"),el("div",turn.text,"chat-prose"));
      answer.append(el("strong","NEXUS"));
      if(presentation?.files?.length) {
        const slot=el("div");answer.append(slot);
        import("./app/chat-files.js").then(({renderChatFiles})=>{if(slot.isConnected)slot.append(renderChatFiles(presentation.files,api,()=>crypto.randomUUID()));});
      }
      for (const source of presentation?.conversations || []) {
        const sourceCard = el("div", "", "card");
        const open = el("button", "打开会话", "quiet");
        open.onclick = action(() => chatOpenSource(source.conversation_id));
        sourceCard.append(el("strong",source.title),el("p",[source.project_name,source.created_at?.slice(0,10),source.archived ? "已归档" : ""].filter(Boolean).join(" · "),"hint"),open);
        answer.append(sourceCard);
      }
      if (presentation) {
        if (presentation.final_text!==null) answer.append(el("strong",presentation.phase==="completed" ? "最终回答" : "本轮反馈"),chatContent(presentation.final_text));
        answer.append(chatPresentationPanel(turn,presentation));
      } else {
        answer.append(chatContent(turn.phase==="summary" ? (turn.initial_response_text || "已确认只读动作。") : turn.response_text || (turn.work_status==="waiting" ? "正在执行本轮工作，请查看下方实际进度。" : turn.work_status==="permission" ? "需要你的答复，请查看下方具体请求。" : turn.proposal ? "已提出只读动作，等待你的决定。" : turn.status==="queued" ? "等待模型…" : turn.status==="generating" ? "正在生成…" : "尚无回复内容")));
      }
      const cost=turn.cost_status==="unknown" ? " · 部分费用待核对" : turn.cost_microunits!=null ? ` · 费用上界 $${turn.cost_microunits/1000000}` : "";
      const replyStatus=presentation ? presentation.label : (turn.work_status==="waiting" ? "工作进行中" : turn.work_status==="permission" ? "等待你的答复" : turn.skill_plan && turn.phase==="reply" ? "流程建议已生成" : chatLabels[turn.status]);
      answer.append(el("p",`${replyStatus}${cost}${turn.web_call_count ? ` · 网页请求 ${turn.web_call_count} 次` : ""}${turn.context_truncated ? " · 部分历史或工具上下文已裁剪，完整执行结果见工作记录" : ""}`,"hint"));
      if (turn.error_code) {
        const failure=el("details");
        failure.append(el("summary","技术详情"),el("pre",turn.error_code));
        answer.append(failure);
      }
      const inspect=el("button","查看本轮发送内容","quiet");
      inspect.onclick=action(async()=>{const value=await api(`/api/v1/chat/turns/${turn.turn_id}`); const details=el("pre",JSON.stringify({messages:value.turn.context_messages,skill_catalog:value.turn.skill_catalog,work_catalog:value.turn.work_catalog,work_messages:value.turn.work_messages,result_messages:value.turn.result_context_messages},null,2)); inspect.replaceWith(details);});
      answer.append(inspect);
      chatJournalButton(turn,answer);
      const failureHints={MODEL_RESPONSE_INVALID:"模型返回的格式无法校验，本轮已停止。",MODEL_TOOL_CALL_LIMIT:"模型提出的动作超过本轮剩余额度，未执行这批动作。",MODEL_REQUEST_FAILED:"模型连接或响应解析失败，本轮已停止。",MODEL_TIMEOUT:"等待模型响应超时，本轮已停止。",MODEL_OUTCOME_UNKNOWN:"未收到完整模型响应或用量，实际费用无法确认；这不表示设备已经执行。"};
      if (!presentation?.final_text && failureHints[turn.error_code]) answer.append(el("p",failureHints[turn.error_code],"hint"));
      if (!presentation?.final_text && turn.error_code?.startsWith("WEB_")) answer.append(el("p","这次网页请求没有取得可用内容；已有资料仍然保留。","hint"));
      if (turn.work_grant_id) answer.append(chatWorkCards(turn));
      if (turn.allow_skills && turn.status==="completed" && !turn.proposal) answer.append(el("p","本轮没有选出可执行流程，未创建设备执行。可核对可用能力或补充目标。","hint"));
      if (turn.skill_plan) answer.append(chatSkillCard(turn,!chatState.active && turn===turns.at(-1)));
      else if (turn.proposal) {
        const card=el("section",undefined,"chat-action");
        card.append(el("h3",turn.proposal.intent==="get_system_info" ? "读取系统信息" : "列出 workspace 目录"),el("pre",JSON.stringify({node_id:turn.node_id,action:turn.proposal,tool_version:1,expires_at:turn.expires_at},null,2)));
        if (task) {
          card.append(taskCard(task));
          if (turn.auto_result_status==="waiting") card.append(el("p","已允许本次白名单结果回传，正在等待工具完成；成功后会自动生成最终回答。","hint"));
          else if (turn.phase==="reply" && task.task.status==="succeeded") card.append(el("p",turn.auto_result_status==="stopped" ? "已停止自动回传。工具已完成，结果尚未发送给模型。" : "工具已完成，结果尚未发送给模型。请预览并同意回传，模型才能据此回答。","hint"));
          if (turn.auto_result_status==="unavailable") card.append(el("p","自动回传未完成，未增加模型调用。请查看本轮状态和工具结果。","hint"));
        }
        else if (turn.action_status==="proposed" && new Date(turn.expires_at)>new Date() && !chatState.conversation?.archived) {
          const buttons=el("div",undefined,"button-row");
          const label=el("label",undefined,"checkbox"), consent=document.createElement("input");
          consent.type="checkbox"; consent.checked=chatState.actionConsents.has(turn.turn_id); consent.disabled=!turn.result_supported;
          label.append(consent,document.createTextNode("允许本次白名单结果回传 DeepSeek，成功后自动回答"));
          card.append(label,el("p",turn.proposal.intent==="get_system_info" ? "仅回传操作系统、系统版本、处理器架构和 Python 版本；不发送路径、文件内容或凭证。" : "仅回传 workspace 别名、直接条目名称与类型及截断状态；不发送文件内容、绝对路径或凭证。","hint"));
          const confirm=el("button","确认执行","primary");
          const setConsent=()=>{if (consent.checked) chatState.actionConsents.add(turn.turn_id); else chatState.actionConsents.delete(turn.turn_id); confirm.textContent=consent.checked ? "确认执行并回传结果" : "确认执行";};
          consent.onchange=setConsent; setConsent();
          for (const [decision,label] of [["confirm","确认执行"],["reject","不执行"]]) {
            const button=decision==="confirm" ? confirm : el("button",label,"quiet");
            button.onclick=action(async()=>{button.disabled=true; if (decision==="confirm" && consent.checked) {chatState.confirming=turn.turn_id; chatRender();} try {const value=await api(`/api/v1/chat/turns/${turn.turn_id}/action`,{method:"POST",body:JSON.stringify({proposal_digest:turn.proposal_digest,decision,allow_result_model:decision==="confirm" && consent.checked})}); chatState.actionConsents.delete(turn.turn_id); if (value.task) await updateTask(value.task.task_id); await chatHistory();} finally {if (chatState.confirming===turn.turn_id) chatState.confirming=null; button.disabled=false; chatRender();}});
            buttons.append(button);
          }
          card.append(el("p","确认后仍检查权限；目录另需审批。勾选即同意发送本次白名单结果及原上下文，最多再调用一次，共用本轮 $0.05；未勾选可执行后预览确认。","hint"),buttons);
        } else if (!turn.task_id) card.append(el("p",turn.action_status==="rejected" ? "你已拒绝这个动作。" : "动作已过期或会话已归档，请重新提出请求。","hint"));
        if (turn.result_supported && turn.phase==="reply" && turn.status==="completed" && task?.task.status==="succeeded" && turn.auto_result_status!=="waiting" && !chatState.conversation?.archived) chatResultPreview(turn,card,!chatState.active && turn===turns.at(-1));
        answer.append(card);
      }
      if (!presentation && turn.phase==="summary") answer.append(el("strong","最终回答"),chatContent(turn.response_text || (turn.status==="queued" ? "等待模型解读结果…" : turn.status==="generating" ? "正在根据结果生成回答…" : "本次解读未完成；工具执行结果已保留。")));
      if (turn.memory_references?.length) {
        const refs=el("details"); refs.append(el("summary","本轮引用的记忆与来源"),el("pre",JSON.stringify(turn.memory_references,null,2))); answer.append(refs);
      }
      const saveMemory=el("button","从本条输入明确保存记忆","quiet"); saveMemory.onclick=()=>chatMemoryEditor(turn); answer.append(saveMemory);
      article.replaceChildren(question,answer);
    }
    log.append(article);
  }
  for (const [id,node] of existing) if (!keep.has(id)) node.remove();
  if (!turns.length && !log.querySelector(".chat-empty")) log.append(el("p","新建会话，和 NEXUS 讨论一个想法。","empty chat-empty"));
  if (nearBottom && chatState.latest) log.scrollTop=log.scrollHeight;
  const conversation=chatState.conversation;
  if (document.activeElement!==$("chat-title")) $("chat-title").value=conversation?.title || "";
  $("chat-archive").disabled=!conversation; $("chat-archive").textContent=conversation?.archived ? "取消归档" : "归档";
  const flowRunning=!!chatState.turns.get(chatState.active)?.skill_run && chatState.turns.get(chatState.active)?.phase!=="summary";
  $("chat-stop").disabled=!chatState.active || flowRunning;
  const waiting=chatState.turns.get(chatState.active)?.auto_result_status==="waiting";
  $("chat-stop").textContent=waiting ? "停止自动回传" : "停止回复";
  $("chat-send").disabled=!chatState.enabled || !conversation || !!conversation.archived || !!chatState.active || !!chatState.confirming;
  chatWorkRender();
  chatControlFields();
  if (chatState.active && conversation && !conversation.archived && !chatState.confirming) $("chat-send").disabled=false;
  const permission=chatState.turns.get(chatState.active)?.work_status==="permission";
  if (permission && !conversation?.archived) $("chat-send").disabled=false;
  $("chat-send").textContent=chatState.active && !permission ? ($("chat-input-mode").value==="steer" ? "发送纠正，下一步生效" : "排队发送后续任务") : permission ? "发送本次权限答复" : chatWorkActive() ? "发送并允许本轮工具结果回传 DeepSeek" : "发送给 DeepSeek";
  $("chat-stop").textContent=chatState.turns.get(chatState.active)?.work_grant_id ? "停止本轮工作" : $("chat-stop").textContent;
  $("chat-text").placeholder=permission ? "回复：同意 / 不同意（不调用模型）" : "说出你的问题或工作目标";
  const working=chatWorkActive();
  $("chat-tools").parentElement.lastChild.textContent=working ? "本轮使用已配置的工具与工作区" : "允许建议系统信息与目录工具（执行前需确认）";
  for (const id of ["chat-skill-load","chat-skills-catalog"]) $(id).hidden=working;
  $("chat-skills").parentElement.hidden=working;
  const imageNote=working && chatWorkImageAvailable() ? " 图片工具已由服务器返回：配置工作区内的图片可能生成最多 1024px / 8 KiB 的有界快照并随本轮结果发送给 DeepSeek；可能继续缩小或使用有损 JPEG，GIF/WebP 只取首帧，透明区域显示白底。点击发送即本轮明确同意，历史选择不代替本轮同意。" : "";
  $("chat-outbound-notice").textContent=working ? "发送即同意外发本轮输入、所选上下文、可用能力目录、实际工具结果及按问题查到的本人其他会话摘录与项目记录；网页搜索词和公开页面网址会发送给 DeepSeek。每轮最多 4 个动作、5 次工作模型交换；勾选压缩时最多额外一次摘要调用，共用原费用上限；网页每次有结果收费 $0.00075，合计共用 $0.05 和每日 $1。"+imageNote : "点击“发送给 DeepSeek”即同意外发本次输入及所选上下文和流程目录。勾选压缩时最多额外一次摘要调用，共用原费用上限。单步工具可在确认卡允许自动回传；多步流程成功后先预览结果，再同意解读。每天 $1、每轮共用 $0.05，普通聊天 1 次、含结果解读最多 2 次调用。"+imageNote;
  $("chat-context-info").textContent=flowRunning ? "流程正在执行，请在上方查看步骤、审批或取消整个流程。完成后可预览结果并追问。" : waiting ? "等待工具结果并自动回答，完成后即可追问。停止自动回传不会取消工具；取消执行请使用任务卡。" : chatState.active ? "可选择纠正当前工作或排队后续任务；纠正在已派发操作结束后生效，不会扩大权限。" : "上下文保留近期原文和所选资料；同意压缩时按 token 估算与请求容量生成旧历史摘要，原始记录保留。可用 /compact 手动压缩。";
}
function initChat() {
  initChatControls();
  initChatWork();
  initChatSkills();
  $("chat-new").onclick=action(async()=>{
    $("chat-new").disabled=true; $("chat-text").disabled=true; $("chat-text").value="";
    chatState.epoch++; chatState.conversation=null; chatState.active=null; chatState.turns.clear(); chatRender();
    try {
      chatState.createRequest ||= crypto.randomUUID();
      const value=await api("/api/v1/chat/conversations",{method:"POST",body:JSON.stringify({request_id:chatState.createRequest,project_id:$("chat-project").value || null})});
      chatState.createRequest=null; $("chat-archived").checked=false; await chatList();
      if (!chatState.conversations.has(value.conversation.conversation_id)) chatState.conversations.set(value.conversation.conversation_id,value.conversation);
      await chatSelect(value.conversation.conversation_id);
    } finally {$("chat-new").disabled=false; $("chat-text").disabled=false; chatRender();}
  });
  $("chat-conversation").onchange=action(async()=>chatSelect($("chat-conversation").value));
  $("chat-more-conversations").onclick=action(async()=>chatList(true));
  $("chat-archived").onchange=action(async()=>{chatState.epoch++; chatState.conversation=null; chatState.turns.clear(); await chatList();});
  $("chat-title-form").onsubmit=action(async()=>{if (!chatState.conversation) return; await api(`/api/v1/chat/conversations/${chatState.conversation.conversation_id}`,{method:"PATCH",body:JSON.stringify({title:$("chat-title").value})}); await chatList();});
  $("chat-archive").onclick=action(async()=>{if (!chatState.conversation) return; $("chat-archived").disabled=true; try {await api(`/api/v1/chat/conversations/${chatState.conversation.conversation_id}`,{method:"PATCH",body:JSON.stringify({archived:!chatState.conversation.archived})}); chatState.conversation=null; chatState.turns.clear(); await chatList();} finally {$("chat-archived").disabled=false;}});
  $("chat-form").onsubmit=async(event)=>{
    event.preventDefault();
    if ($("chat-send").disabled) return;
    $("chat-send").disabled=true;
    try {
    const conversation=chatState.conversation;
    if (chatState.turns.get(chatState.active)?.work_status==="permission") {await chatWorkAnswer(chatState.turns.get(chatState.active));return;}
    if (!conversation) throw new Error("请先新建会话");
    if (chatState.active && $("chat-input-mode").value==="steer") {
      const signature=JSON.stringify([chatState.active,$("chat-text").value]);
      if (chatControlState.retry?.signature!==signature) chatControlState.retry={signature,payload:{request_id:crypto.randomUUID(),text:$("chat-text").value,allow_model:true}};
      await api(`/api/v1/chat/turns/${chatState.active}/steer`,{method:"POST",body:JSON.stringify(chatControlState.retry.payload)});
      chatControlState.retry=null;$("chat-text").value="";notify("纠正已记录，将在当前执行结束后的下一步生效");return;
    }
    const compactOnly=$("chat-text").value.trim()==="/compact";
    const allowCompaction=$("chat-compaction").checked;
    if (compactOnly && (!allowCompaction || !$("chat-context").checked)) throw new Error("请勾选会话上下文和压缩同意");
    const allowTools=!compactOnly && chatWorkActive(), nodeId=null;
    const allowSkills=false, skillsDigest=null;
    const memories=compactOnly ? {} : {...memorySubmission(),...chatReferenceSubmission()};
    const work=compactOnly ? {} : await chatWorkSubmission();
    const signature=JSON.stringify([conversation.conversation_id,$("chat-text").value,$("chat-context").checked,allowTools,nodeId,allowSkills,skillsDigest,memories,work,allowCompaction,compactOnly]);
    if (chatState.retry?.signature!==signature) chatState.retry={signature,payload:{request_id:crypto.randomUUID(),expected_revision:conversation.revision,text:$("chat-text").value,include_context:$("chat-context").checked,allow_compaction:allowCompaction,compact_only:compactOnly,allow_model:true,enqueue:!!chatState.active,allow_tools:allowTools,node_id:nodeId,allow_skills:allowSkills,skills_digest:skillsDigest,...memories,...work}};
    await api(`/api/v1/chat/conversations/${conversation.conversation_id}/turns`,{method:"POST",body:JSON.stringify(chatState.retry.payload)});
    chatSentRequest = chatState.retry.payload.request_id;
    chatState.latest=true; await chatHistory();
    $("chat-skills").checked=false;
    } catch(error) {
      if (error.message==="IDEMPOTENCY_CONFLICT") {chatState.retry=null; await chatHistory(); notify("会话已变化，请核对上下文后重新发送",true);}
      else notify(error.message,true);
    } finally {chatRender();}
  };
  $("chat-stop").onclick=action(async()=>{if (chatState.active) await api(`/api/v1/chat/turns/${chatState.active}/stop`,{method:"POST",body:"{}"}); await chatHistory(); await usage();});
  $("chat-older").onclick=action(async()=>chatHistory(true));
  $("chat-latest").onclick=action(async()=>{chatState.turns.clear(); chatState.latest=true; await chatHistory();});
  let nextPoll=0;
  setInterval(async()=>{if (!state.logged || $("chat-view").hidden || !chatState.latest || chatState.loading || Date.now()<nextPoll) return; chatState.loading=true; try {await chatHistory();} catch(error) {notify(error.message,true);} finally {chatState.loading=false; nextPoll=Date.now()+(chatState.active ? 500 : 3000);}},250);
}
