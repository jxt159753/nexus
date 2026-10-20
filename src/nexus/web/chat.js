"use strict";
const chatState = {conversation:null, conversations:new Map(), turns:new Map(), previews:new Map(), actionConsents:new Set(), confirming:null, active:null, retry:null, createRequest:null, before:null, listBefore:null, latest:true, loading:false, enabled:false, epoch:0};
const chatLabels = {queued:"�ȴ��ظ�",generating:"���ڻظ�",completed:"�����",incomplete:"�ﵽ������ޣ��ظ�δ��������",stopping:"����ֹͣ",stopped:"��ֹͣ",interrupted:"�����жϣ�δ�Զ��ط�",failed:"�ظ�ʧ��",expired:"�����ѹ���"};
function chatNodes(nodes) {
  chatWorkState.nodes=new Map(nodes.map(node=>[node.node_id,node]));
  const selected=$("chat-node").value;
  $("chat-node").replaceChildren(...nodes.map(n=>new Option(`${n.node_id} �� ${n.online ? "����" : "����"}`,n.node_id)));
  if (!nodes.length) $("chat-node").add(new Option("�������ýڵ㣻�ɼ�����ͨ����",""));
  if (nodes.some(n=>n.node_id===selected)) $("chat-node").value=selected;
  if (chatSkillState.node && chatSkillState.node!==$("chat-node").value) chatSkillsInvalidate();
}
function chatReset() {
  chatWorkReset();
  chatMemoryReset();
  chatState.epoch++; chatState.conversation=null; chatState.conversations.clear(); chatState.turns.clear(); chatState.previews.clear(); chatState.actionConsents.clear(); chatState.confirming=null; chatState.active=null; chatState.retry=null; chatState.createRequest=null; chatState.before=null; chatState.listBefore=null; chatState.latest=true;
  $("chat-messages").replaceChildren(); $("chat-conversation").replaceChildren(new Option("�½�һ���Ự��ʼ", "")); $("chat-form").reset(); $("chat-title-form").reset(); $("chat-send").disabled=true; $("chat-stop").disabled=true;
  chatSkillsReset();
}
function chatAvailability(enabled,webEnabled=false) {
  chatState.enabled=enabled;
  chatWorkState.webEnabled=webEnabled;
  $("chat-availability").textContent=enabled ? "DeepSeek �� ������׷�ʣ������¼������ Core" : "����ģ����δ���ã����лỰ�Կɲ鿴������ Core ��ʽ�����������á�";
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
    chatWorkReset();
    chatMemoryReset();
    chatState.epoch++; chatState.turns.clear(); chatState.previews.clear(); chatState.actionConsents.clear(); chatState.retry=null; chatState.before=null; chatState.latest=true; $("chat-context").checked=true; $("chat-text").value="";
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
      chatMemoryReset();
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
function chatResultPreview(turn, card, canContinue) {
  const value=chatState.previews.get(turn.turn_id), box=el("section",undefined,"chat-result-preview");
  const load=el("button",value ? "������ѡ�ֶ�Ԥ��" : "Ԥ���ɷ��͵Ľ��","quiet");
  const choices=[];
  const send=el("button","ͬ�ⷢ����Щ����� DeepSeek ���������ջش�","primary");
  send.disabled=!canContinue || !value || value.expected_revision!==chatState.conversation?.revision || new Date(value.expires_at)<=new Date();
  load.disabled=!canContinue;
  if (!canContinue) box.append(el("p","���ν����δ���͸�ģ�͡����к�����Ϣ���ִΣ����ֲ�����׷�ӻش�","hint"));
  if (value) {
    const labels={os_name:"����ϵͳ",os_version:"ϵͳ�汾",architecture:"�������ܹ�",python_version:"Python �汾",root_id:"����������",entries:"Ŀ¼��Ŀ���ƺ�����",truncated:"�Ƿ�ض�"};
    box.append(el("p","ѡ�񱾴��ⷢ�ֶΣ���ѡ�������Ԥ����ԭ���뼰��ʱ��ѡ������Ҳ���ٴη��͡�","hint"));
    for (const field of value.available_fields) {
      const label=el("label",undefined,"checkbox"), input=document.createElement("input"); input.type="checkbox"; input.checked=value.fields.includes(field); input.value=field;
      input.onchange=()=>{send.disabled=true;}; choices.push(input); label.append(input,document.createTextNode(labels[field] || `���� ${field} �İ��������`)); box.append(label);
    }
    box.append(el("pre",JSON.stringify(value.data,null,2)),el("p",`����Ϊʵ���ⷢ�������Ч�� ${new Date(value.expires_at).toLocaleTimeString()}������ٵ���һ�Σ��뱾�����÷��ù��� $0.05 ���ޡ�`,"hint"));
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
    const skillRevision=JSON.stringify([turn.skill_run?.revision,turn.skill_run?.steps.map(s=>[s.status,state.tasks.get(s.task_id)?.task.revision,s.tool_execution])]);
    const revision=`${turn.revision}:${task?.task.revision ?? ""}:${chatState.conversation?.revision}:${chatState.previews.get(turn.turn_id)?.result_digest ?? ""}:${skillRevision}:${chatSkillState.busy.has(turn.turn_id)}`;
    if (article.dataset.revision!==revision) {
      article.dataset.revision=revision;
      const question=el("div",undefined,"chat-user"), answer=el("div",undefined,"chat-assistant");
      question.append(el("strong","��"),el("div",turn.text,"chat-prose"));
      answer.append(el("strong","NEXUS"),chatContent(turn.phase==="summary" ? (turn.initial_response_text || "��ȷ��ֻ��������") : turn.response_text || (turn.work_status==="waiting" ? "����ִ�б��ֹ�������鿴�·�ʵ�ʽ��ȡ�" : turn.work_status==="permission" ? "��Ҫ��Ĵ𸴣���鿴�·���������" : turn.proposal ? "�����ֻ���������ȴ���ľ�����" : turn.status==="queued" ? "�ȴ�ģ�͡�" : turn.status==="generating" ? "�������ɡ�" : "���޻ظ�����")));
      const cost=turn.cost_status==="unknown" ? " �� ����δ֪������Ԥ��" : turn.cost_microunits!=null ? ` �� �����Ͻ� $${turn.cost_microunits/1000000}` : "";
      const replyStatus=turn.work_status==="waiting" ? "����������" : turn.work_status==="permission" ? "�ȴ���Ĵ�" : turn.skill_plan && turn.phase==="reply" ? "���̽���������" : chatLabels[turn.status];
      answer.append(el("p",`${replyStatus}${turn.error_code ? " �� "+turn.error_code : ""}${cost}${turn.web_call_count ? ` �� ��ҳ���� ${turn.web_call_count} �Σ�������ã�` : ""}${turn.context_truncated ? " �� ���ֽ����������Ѳü�" : ""}`,"hint"));
      const inspect=el("button","�鿴���ַ�������","quiet");
      inspect.onclick=action(async()=>{const value=await api(`/api/v1/chat/turns/${turn.turn_id}`); const details=el("pre",JSON.stringify({messages:value.turn.context_messages,skill_catalog:value.turn.skill_catalog,work_catalog:value.turn.work_catalog,work_messages:value.turn.work_messages,result_messages:value.turn.result_context_messages},null,2)); inspect.replaceWith(details);});
      answer.append(inspect);
      const failureHints={MODEL_RESPONSE_INVALID:"ģ�ͷ��صĸ�ʽ�޷�У�飬������ֹͣ��",MODEL_TOOL_CALL_LIMIT:"ģ������Ķ�����������ʣ���ȣ�δִ������������",MODEL_REQUEST_FAILED:"ģ�����ӻ���Ӧ����ʧ�ܣ�������ֹͣ��",MODEL_TIMEOUT:"�ȴ�ģ����Ӧ��ʱ��������ֹͣ��",MODEL_OUTCOME_UNKNOWN:"δ�յ�����ģ����Ӧ��������ʵ�ʷ����޷�ȷ�ϣ��ⲻ��ʾ�豸�Ѿ�ִ�С�"};
      if (failureHints[turn.error_code]) answer.append(el("p",failureHints[turn.error_code],"hint"));
      if (turn.error_code?.startsWith("WEB_")) answer.append(el("p","��ҳ����δ��ɣ�δ�Զ����ԣ�����ȷ���Ʒ�ʱ�����ô�Ԥ����","hint"));
      if (turn.work_grant_id) answer.append(chatWorkCards(turn));
      if (turn.allow_skills && turn.status==="completed" && !turn.proposal) answer.append(el("p","����û��ѡ����ִ�����̣�δ�����豸ִ�С��ɺ˶Կ��������򲹳�Ŀ�ꡣ","hint"));
      if (turn.skill_plan) answer.append(chatSkillCard(turn,!chatState.active && turn===turns.at(-1)));
      else if (turn.proposal) {
        const card=el("section",undefined,"chat-action");
        card.append(el("h3",turn.proposal.intent==="get_system_info" ? "��ȡϵͳ��Ϣ" : "�г� workspace Ŀ¼"),el("pre",JSON.stringify({node_id:turn.node_id,action:turn.proposal,tool_version:1,expires_at:turn.expires_at},null,2)));
        if (task) {
          card.append(taskCard(task));
          if (turn.auto_result_status==="waiting") card.append(el("p","���������ΰ���������ش������ڵȴ�������ɣ��ɹ�����Զ��������ջش�","hint"));
          else if (turn.phase==="reply" && task.task.status==="succeeded") card.append(el("p",turn.auto_result_status==="stopped" ? "��ֹͣ�Զ��ش�����������ɣ������δ���͸�ģ�͡�" : "��������ɣ������δ���͸�ģ�͡���Ԥ����ͬ��ش���ģ�Ͳ��ܾݴ˻ش�","hint"));
          if (turn.auto_result_status==="unavailable") card.append(el("p","�Զ��ش�δ��ɣ�δ����ģ�͵��á���鿴����״̬�͹��߽����","hint"));
        }
        else if (turn.action_status==="proposed" && new Date(turn.expires_at)>new Date() && !chatState.conversation?.archived) {
          const buttons=el("div",undefined,"button-row");
          const label=el("label",undefined,"checkbox"), consent=document.createElement("input");
          consent.type="checkbox"; consent.checked=chatState.actionConsents.has(turn.turn_id); consent.disabled=!turn.result_supported;
          label.append(consent,document.createTextNode("�������ΰ���������ش� DeepSeek���ɹ����Զ��ش�"));
          card.append(label,el("p",turn.proposal.intent==="get_system_info" ? "���ش�����ϵͳ��ϵͳ�汾���������ܹ��� Python �汾��������·�����ļ����ݻ�ƾ֤��" : "���ش� workspace ������ֱ����Ŀ���������ͼ��ض�״̬���������ļ����ݡ�����·����ƾ֤��","hint"));
          const confirm=el("button","ȷ��ִ��","primary");
          const setConsent=()=>{if (consent.checked) chatState.actionConsents.add(turn.turn_id); else chatState.actionConsents.delete(turn.turn_id); confirm.textContent=consent.checked ? "ȷ��ִ�в��ش����" : "ȷ��ִ��";};
          consent.onchange=setConsent; setConsent();
          for (const [decision,label] of [["confirm","ȷ��ִ��"],["reject","��ִ��"]]) {
            const button=decision==="confirm" ? confirm : el("button",label,"quiet");
            button.onclick=action(async()=>{button.disabled=true; if (decision==="confirm" && consent.checked) {chatState.confirming=turn.turn_id; chatRender();} try {const value=await api(`/api/v1/chat/turns/${turn.turn_id}/action`,{method:"POST",body:JSON.stringify({proposal_digest:turn.proposal_digest,decision,allow_result_model:decision==="confirm" && consent.checked})}); chatState.actionConsents.delete(turn.turn_id); if (value.task) await updateTask(value.task.task_id); await chatHistory();} finally {if (chatState.confirming===turn.turn_id) chatState.confirming=null; button.disabled=false; chatRender();}});
            buttons.append(button);
          }
          card.append(el("p","ȷ�Ϻ��Լ��Ȩ�ޣ�Ŀ¼������������ѡ��ͬ�ⷢ�ͱ��ΰ����������ԭ�����ģ�����ٵ���һ�Σ����ñ��� $0.05��δ��ѡ��ִ�к�Ԥ��ȷ�ϡ�","hint"),buttons);
        } else if (!turn.task_id) card.append(el("p",turn.action_status==="rejected" ? "���Ѿܾ����������" : "�����ѹ��ڻ�Ự�ѹ鵵���������������","hint"));
        if (turn.result_supported && turn.phase==="reply" && turn.status==="completed" && task?.task.status==="succeeded" && turn.auto_result_status!=="waiting" && !chatState.conversation?.archived) chatResultPreview(turn,card,!chatState.active && turn===turns.at(-1));
        answer.append(card);
      }
      if (turn.phase==="summary") answer.append(el("strong","���ջش�"),chatContent(turn.response_text || (turn.status==="queued" ? "�ȴ�ģ�ͽ�������" : turn.status==="generating" ? "���ڸ��ݽ�����ɻش�" : "���ν��δ��ɣ�����ִ�н���ѱ�����")));
      if (turn.memory_references?.length) {
        const refs=el("details"); refs.append(el("summary","�������õļ�������Դ"),el("pre",JSON.stringify(turn.memory_references,null,2))); answer.append(refs);
      }
      const saveMemory=el("button","�ӱ���������ȷ�������","quiet"); saveMemory.onclick=()=>chatMemoryEditor(turn); answer.append(saveMemory);
      article.replaceChildren(question,answer);
    }
    log.append(article);
  }
  for (const [id,node] of existing) if (!keep.has(id)) node.remove();
  if (!turns.length && !log.querySelector(".chat-empty")) log.append(el("p","�½��Ự���� NEXUS ����һ���뷨��","empty chat-empty"));
  if (nearBottom && chatState.latest) log.scrollTop=log.scrollHeight;
  const conversation=chatState.conversation;
  if (document.activeElement!==$("chat-title")) $("chat-title").value=conversation?.title || "";
  $("chat-archive").disabled=!conversation; $("chat-archive").textContent=conversation?.archived ? "ȡ���鵵" : "�鵵";
  const flowRunning=!!chatState.turns.get(chatState.active)?.skill_run && chatState.turns.get(chatState.active)?.phase!=="summary";
  $("chat-stop").disabled=!chatState.active || flowRunning;
  const waiting=chatState.turns.get(chatState.active)?.auto_result_status==="waiting";
  $("chat-stop").textContent=waiting ? "ֹͣ�Զ��ش�" : "ֹͣ�ظ�";
  $("chat-send").disabled=!chatState.enabled || !conversation || !!conversation.archived || !!chatState.active || !!chatState.confirming;
  chatWorkRender();
  const permission=chatState.turns.get(chatState.active)?.work_status==="permission";
  if (permission && !conversation?.archived) $("chat-send").disabled=false;
  $("chat-send").textContent=permission ? "���ͱ���Ȩ�޴�" : chatWorkActive() ? "���Ͳ��������ֹ��߽���ش� DeepSeek" : "���͸� DeepSeek";
  $("chat-stop").textContent=chatState.turns.get(chatState.active)?.work_grant_id ? "ֹͣ���ֹ���" : $("chat-stop").textContent;
  $("chat-text").placeholder=permission ? "�ظ���ͬ�� / ��ͬ�⣨������ģ�ͣ�" : "˵������������Ŀ��";
  const working=chatWorkActive();
  $("chat-tools").parentElement.lastChild.textContent=working ? "����ʹ�������õĹ����빤����" : "��������ϵͳ��Ϣ��Ŀ¼���ߣ�ִ��ǰ��ȷ�ϣ�";
  for (const id of ["chat-skill-load","chat-skills-catalog"]) $(id).hidden=working;
  $("chat-skills").parentElement.hidden=working;
  $("chat-outbound-notice").textContent=working ? "���ͼ�ͬ���ⷢ�������롢��ѡ�����ġ���������Ŀ¼��ʵ�ʹ��߽������ҳ�����ʺ͹���ҳ����ַ�ᷢ�͸� DeepSeek��ÿ����� 4 ��������5 ��ģ�ͽ�������ҳÿ���н���շ� $0.00075���ϼƹ��� $0.05 ��ÿ�� $1��" : "��������͸� DeepSeek����ͬ���ⷢ�������뼰��ѡ�����ĺ�����Ŀ¼���������߿���ȷ�Ͽ������Զ��ش����ಽ���̳ɹ�����Ԥ���������ͬ������ÿ�� $1��ÿ�ֹ��� $0.05����ͨ���� 1 �Ρ������������ 2 �ε��á�";
  $("chat-context-info").textContent=flowRunning ? "��������ִ�У������Ϸ��鿴���衢������ȡ���������̡���ɺ��Ԥ�������׷�ʡ�" : waiting ? "�ȴ����߽�����Զ��ش���ɺ󼴿�׷�ʡ�ֹͣ�Զ��ش�����ȡ�����ߣ�ȡ��ִ����ʹ�����񿨡�" : chatState.active ? "�����������ѹ̶���ֹͣ����ɺ�ɷ�����һ����" : "ÿ�η�����฽�����Ự��� 10 �������Ի������ް����ֲü���";
}
function initChat() {
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
    if (!conversation || chatState.active) throw new Error("�����½��Ự��ȴ���ǰ�ظ�����");
    const allowTools=$("chat-tools").checked && !!$("chat-node").value, nodeId=allowTools ? $("chat-node").value : null;
    const allowSkills=!chatWorkActive() && $("chat-skills").checked;
    if (allowSkills && (!allowTools || !chatSkillState.catalog || chatSkillState.node!==nodeId)) throw new Error("�����²鿴����Ŀ¼������������");
    const skillsDigest=allowSkills ? chatSkillState.catalog.skills_digest : null;
    const memories=memorySubmission();
    const work=await chatWorkSubmission();
    const signature=JSON.stringify([conversation.conversation_id,$("chat-text").value,$("chat-context").checked,allowTools,nodeId,allowSkills,skillsDigest,memories,work]);
    if (chatState.retry?.signature!==signature) chatState.retry={signature,payload:{request_id:crypto.randomUUID(),expected_revision:conversation.revision,text:$("chat-text").value,include_context:$("chat-context").checked,allow_model:true,allow_tools:allowTools,node_id:nodeId,allow_skills:allowSkills,skills_digest:skillsDigest,...memories,...work}};
    await api(`/api/v1/chat/conversations/${conversation.conversation_id}/turns`,{method:"POST",body:JSON.stringify(chatState.retry.payload)});
    chatState.latest=true; await chatHistory();
    $("chat-skills").checked=false;
    } catch(error) {
      if (error.message==="IDEMPOTENCY_CONFLICT") {chatState.retry=null; await chatHistory(); notify("�Ự�ѱ仯����˶������ĺ����·���",true);}
      else notify(error.message,true);
    } finally {chatRender();}
  };
  $("chat-stop").onclick=action(async()=>{if (chatState.active) await api(`/api/v1/chat/turns/${chatState.active}/stop`,{method:"POST",body:"{}"}); await chatHistory(); await usage();});
  $("chat-older").onclick=action(async()=>chatHistory(true));
  $("chat-latest").onclick=action(async()=>{chatState.turns.clear(); chatState.latest=true; await chatHistory();});
  let nextPoll=0;
  setInterval(async()=>{if (!state.logged || $("chat-view").hidden || !chatState.latest || chatState.loading || Date.now()<nextPoll) return; chatState.loading=true; try {await chatHistory();} catch(error) {notify(error.message,true);} finally {chatState.loading=false; nextPoll=Date.now()+(chatState.active ? 500 : 3000);}},250);
}
