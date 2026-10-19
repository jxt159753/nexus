"use strict";
const chatState = {conversation:null, conversations:new Map(), turns:new Map(), previews:new Map(), active:null, retry:null, createRequest:null, before:null, listBefore:null, latest:true, loading:false, enabled:false, epoch:0};
const chatLabels = {queued:"等待回复",generating:"正在回复",completed:"已完成",incomplete:"达到输出上限，回复未完整结束",stopping:"正在停止",stopped:"已停止",interrupted:"服务中断，未自动重发",failed:"回复失败",expired:"请求已过期"};
function chatNodes(nodes) {
  const selected=$("chat-node").value;
  $("chat-node").replaceChildren(...nodes.map(n=>new Option(`${n.node_id} · ${n.online ? "在线" : "离线"}`,n.node_id)));
  if (!nodes.length) $("chat-node").add(new Option("暂无配置节点；可继续普通聊天",""));
  if (nodes.some(n=>n.node_id===selected)) $("chat-node").value=selected;
}
function chatReset() {
  chatState.epoch++; chatState.conversation=null; chatState.conversations.clear(); chatState.turns.clear(); chatState.previews.clear(); chatState.active=null; chatState.retry=null; chatState.createRequest=null; chatState.before=null; chatState.listBefore=null; chatState.latest=true;
  $("chat-messages").replaceChildren(); $("chat-conversation").replaceChildren(new Option("新建一个会话开始", "")); $("chat-form").reset(); $("chat-title-form").reset(); $("chat-send").disabled=true; $("chat-stop").disabled=true;
}
function chatAvailability(enabled) {
  chatState.enabled=enabled;
  $("chat-availability").textContent=enabled ? "DeepSeek · 可连续追问，聊天记录保存在 Core" : "聊天模型尚未启用；已有会话仍可查看。请在 Core 显式启用聊天配置。";
  $("chat-send").disabled=!enabled || !chatState.conversation || !!chatState.conversation.archived || !!chatState.active;
}
async function chatList(older=false) {
  const epoch=chatState.epoch, archived=$("chat-archived").checked;
  const value=await api(`/api/v1/chat/conversations?archived=${archived}`+(older && chatState.listBefore ? `&before=${chatState.listBefore}` : ""));
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
    chatState.epoch++; chatState.turns.clear(); chatState.previews.clear(); chatState.retry=null; chatState.before=null; chatState.latest=true; $("chat-context").checked=true; $("chat-text").value="";
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
  chatState.conversations.set(conversation.conversation_id,chatState.conversation);
  const option=[...$("chat-conversation").options].find(o=>o.value===conversation.conversation_id);
  if (option) option.textContent=chatState.conversation.title;
  if (older) chatState.latest=false;
  for (const turn of value.turns) {
    const previous=chatState.turns.get(turn.turn_id);
    if (!previous || turn.revision>=previous.revision) chatState.turns.set(turn.turn_id,turn);
    if (turn.task_id && (!state.tasks.has(turn.task_id) || (!terminal.has(state.tasks.get(turn.task_id).task.status) && state.socket?.readyState!==WebSocket.OPEN))) await updateTask(turn.task_id);
    if (chatState.retry?.payload.request_id===turn.request_id) {
      if ($("chat-text").value===chatState.retry.payload.text) $("chat-text").value="";
      chatState.retry=null;
    }
  }
  const ordered=[...chatState.turns.values()].sort((a,b)=>a.cursor-b.cursor);
  while (ordered.length>100) {const removed=chatState.latest ? ordered.shift() : ordered.pop(); chatState.turns.delete(removed.turn_id);}
  chatState.before=ordered[0]?.cursor;
  $("chat-older").hidden=value.turns.length<20;
  $("chat-latest").hidden=chatState.latest;
  chatRender();
  if (wasActive && !chatState.active) await usage();
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
function chatResultPreview(turn, card) {
  const value=chatState.previews.get(turn.turn_id), box=el("section",undefined,"chat-result-preview");
  const load=el("button",value ? "更新所选字段预览" : "预览可发送的结果","quiet");
  const choices=[];
  const send=el("button","同意发送这些结果给 DeepSeek 并生成最终回答","primary");
  send.disabled=!value || value.expected_revision!==chatState.conversation?.revision || new Date(value.expires_at)<=new Date();
  if (value) {
    const labels={os_name:"操作系统",os_version:"系统版本",architecture:"处理器架构",python_version:"Python 版本",root_id:"工作区别名",entries:"目录条目名称和类型",truncated:"是否截断"};
    box.append(el("p","选择本次外发字段；改选后请更新预览。原输入及当时所选上下文也将再次发送。","hint"));
    for (const field of value.available_fields) {
      const label=el("label",undefined,"checkbox"), input=document.createElement("input"); input.type="checkbox"; input.checked=value.fields.includes(field); input.value=field;
      input.onchange=()=>{send.disabled=true;}; choices.push(input); label.append(input,document.createTextNode(labels[field])); box.append(label);
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
function chatRender() {
  const log=$("chat-messages"), nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<90;
  const existing=new Map([...log.querySelectorAll("[data-turn-id]")].map(n=>[n.dataset.turnId,n]));
  const turns=[...chatState.turns.values()].sort((a,b)=>a.cursor-b.cursor), keep=new Set();
  if (turns.length) log.querySelectorAll(".chat-empty").forEach(n=>n.remove());
  for (const turn of turns) {
    keep.add(turn.turn_id); let article=existing.get(turn.turn_id);
    if (!article) {article=el("article",undefined,"chat-turn"); article.dataset.turnId=turn.turn_id;}
    const task=turn.task_id ? state.tasks.get(turn.task_id) : null;
    const revision=`${turn.revision}:${task?.task.revision ?? ""}:${chatState.conversation?.revision}:${chatState.previews.get(turn.turn_id)?.result_digest ?? ""}`;
    if (article.dataset.revision!==revision) {
      article.dataset.revision=revision;
      const question=el("div",undefined,"chat-user"), answer=el("div",undefined,"chat-assistant");
      question.append(el("strong","你"),el("div",turn.text,"chat-prose"));
      answer.append(el("strong","NEXUS"),chatContent(turn.phase==="summary" ? (turn.initial_response_text || "已确认只读动作。") : turn.response_text || (turn.proposal ? "已提出只读动作，等待你的决定。" : turn.status==="queued" ? "等待模型…" : turn.status==="generating" ? "正在生成…" : "尚无回复内容")));
      const cost=turn.cost_status==="unknown" ? " · 费用未知，保留预留" : turn.cost_microunits!=null ? ` · 费用上界 $${turn.cost_microunits/1000000}` : "";
      answer.append(el("p",`${chatLabels[turn.status]}${turn.error_code ? " · "+turn.error_code : ""}${cost}${turn.context_truncated ? " · 本轮较早上下文已裁剪" : ""}`,"hint"));
      const inspect=el("button","查看本轮发送内容","quiet");
      inspect.onclick=action(async()=>{const value=await api(`/api/v1/chat/turns/${turn.turn_id}`); const details=el("pre",JSON.stringify(value.turn.context_messages,null,2)); inspect.replaceWith(details);});
      answer.append(inspect);
      if (turn.proposal) {
        const card=el("section",undefined,"chat-action");
        card.append(el("h3",turn.proposal.intent==="get_system_info" ? "读取系统信息" : "列出 workspace 目录"),el("pre",JSON.stringify({node_id:turn.node_id,action:turn.proposal,tool_version:1,expires_at:turn.expires_at},null,2)));
        if (task) card.append(taskCard(task));
        else if (turn.action_status==="proposed" && new Date(turn.expires_at)>new Date() && !chatState.conversation?.archived) {
          const buttons=el("div",undefined,"button-row");
          for (const [decision,label] of [["confirm","确认执行"],["reject","不执行"]]) {
            const button=el("button",label,decision==="confirm" ? "primary" : "quiet");
            button.onclick=action(async()=>{button.disabled=true; try {const value=await api(`/api/v1/chat/turns/${turn.turn_id}/action`,{method:"POST",body:JSON.stringify({proposal_digest:turn.proposal_digest,decision})}); if (value.task) await updateTask(value.task.task_id); await chatHistory();} finally {button.disabled=false;}});
            buttons.append(button);
          }
          card.append(el("p","确认后进入权限检查；目录仍需单独审批。结果可在预览并同意后回传模型。","hint"),buttons);
        } else if (!turn.task_id) card.append(el("p",turn.action_status==="rejected" ? "你已拒绝这个动作。" : "动作已过期或会话已归档，请重新提出请求。","hint"));
        if (turn.result_supported && turn.phase==="reply" && turn.status==="completed" && task?.task.status==="succeeded" && !chatState.active && turn===turns.at(-1) && !chatState.conversation?.archived) chatResultPreview(turn,card);
        answer.append(card);
      }
      if (turn.phase==="summary") answer.append(el("strong","最终回答"),chatContent(turn.response_text || (turn.status==="queued" ? "等待模型解读结果…" : turn.status==="generating" ? "正在根据结果生成回答…" : "本次解读未完成；工具执行结果已保留。")));
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
  $("chat-stop").disabled=!chatState.active;
  $("chat-send").disabled=!chatState.enabled || !conversation || !!conversation.archived || !!chatState.active;
  $("chat-context-info").textContent=chatState.active ? "本轮上下文已固定；停止或完成后可发送下一条。" : "每次发送最多附带本会话最近 10 轮完整对话，超限按整轮裁剪。";
}
function initChat() {
  $("chat-new").onclick=action(async()=>{
    $("chat-new").disabled=true; $("chat-text").disabled=true; $("chat-text").value="";
    chatState.epoch++; chatState.conversation=null; chatState.active=null; chatState.turns.clear(); chatRender();
    try {
      chatState.createRequest ||= crypto.randomUUID();
      const value=await api("/api/v1/chat/conversations",{method:"POST",body:JSON.stringify({request_id:chatState.createRequest})});
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
    if (!conversation || chatState.active) throw new Error("请先新建会话或等待当前回复结束");
    const allowTools=$("chat-tools").checked && !!$("chat-node").value, nodeId=allowTools ? $("chat-node").value : null;
    const signature=JSON.stringify([conversation.conversation_id,$("chat-text").value,$("chat-context").checked,allowTools,nodeId]);
    if (chatState.retry?.signature!==signature) chatState.retry={signature,payload:{request_id:crypto.randomUUID(),expected_revision:conversation.revision,text:$("chat-text").value,include_context:$("chat-context").checked,allow_model:true,allow_tools:allowTools,node_id:nodeId}};
    await api(`/api/v1/chat/conversations/${conversation.conversation_id}/turns`,{method:"POST",body:JSON.stringify(chatState.retry.payload)});
    chatState.latest=true; await chatHistory();
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
