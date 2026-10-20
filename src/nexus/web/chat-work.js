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
  $("chat-work-status").textContent=automatic ? "�����õ� workspace �ڶ�д������Ĭ�Ͽ��ã�����Ự��Ȩ��д��ȷ�ϡ�·����顢�ļ������顢ִ�м�¼��Ԥ������Ч��" : usable ? `���Ự��Ȩ��Ч�� ${new Date(grant.expires_at).toLocaleTimeString()}������ ${grant.used_operations}/${grant.max_operations} �β�����` : "��ǰ�豸δ�����������������ߣ����ѵǼǵ��������á�";
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
  box.append(el("h3","���ֹ�����¼"));
  const labels={get_node_status:"��ѯ�ڵ�״̬",get_system_info:"��ȡϵͳ��Ϣ",list_directory:"�г�������",read_file:"��ȡ�ļ�",find_files:"�����ļ�",write_file:"д���ļ�",run_command:"ִ�й̶�����",run_skill:"���� Skill",run_tool:"���� Tool",web_search:"������ҳ",web_fetch:"ץȡ��ҳ"};
  const statuses={pending:"׼��ִ��",waiting:"�ȴ�ִ�н��",permission:"��Ҫ���ȷ��",succeeded:"�����",failed:"δ���",stopped:"������ֹͣ��ʵ��ִ��״̬�������¼"};
  for (const step of turn.work_steps || []) {
    const card=el("section",undefined,"chat-result-preview");
    card.append(el("strong",`${step.step_index}. ${labels[step.proposal.intent] || step.proposal.intent} �� ${statuses[step.status]}`));
    const args=step.proposal.arguments,data=step.result?.data,preview=step.result?.preview;
    if (args.path) card.append(el("p",`workspace/${args.path}`));
    if (args.pattern) card.append(el("p",`�ļ�����${args.pattern}`));
    if (args.command_id) card.append(el("p",`�̶����${args.command_id}`));
    if (args.query) card.append(el("p",`������${args.query}`));
    if (preview) {
      card.append(el("pre",preview.diff || "�ļ�����û�б仯��"));
      if (preview.truncated) card.append(el("p","�����ѽضϣ�ȷ��ǰ����ִ������˶����������ݡ�","hint"));
    }
    if (data && ["web_search","web_fetch"].includes(step.proposal.intent)) {
      for (const source of data.sources || [data]) {
        const link=el("a",source.title || source.url);link.href=source.url;link.target="_blank";link.rel="noopener noreferrer";card.append(link);
        if (source.snippet) card.append(el("p",source.snippet));
      }
      if (data.content) card.append(el("pre",data.content));
      if (data.truncated) card.append(el("p","��ҳ�����ѽضϡ�","hint"));
    }
    else if (data && step.proposal.intent==="get_node_status") card.append(el("p",`${data.online ? "����" : "����"} �� ${data.busy==null ? "æ��δ֪" : data.busy ? "æµ" : "����"}${data.heartbeat_age_seconds==null ? "" : ` �� ${data.heartbeat_age_seconds} ��ǰ�յ�����`} �� �۲��� ${new Date(data.observed_at).toLocaleTimeString()}`));
    else if (data && "text" in data) card.append(el("pre",data.text),el("p",data.truncated ? "��ȡ����ѽضϡ�" : "�Ѷ�ȡ�ļ���","hint"));
    else if (data && "stdout" in data) card.append(el("pre",data.stdout || data.stderr || "����û���ı������"),el("p",`�˳��룺${data.exit_code}`,"hint"));
    else if (data && step.proposal.intent!=="write_file") card.append(el("pre",JSON.stringify(data,null,2)));
    if (step.request_text) card.append(el("p",step.request_text,"chat-prose"));
    if (step.result?.error_code) card.append(el("p",`���ι���δ��ɣ�${step.result.error_code}`,"hint"));
    if (step.permission_text) card.append(el("p",`��Ĵ𸴣�${step.permission_text}`));
    const details=el("details");details.append(el("summary","�鿴ִ������"),el("pre",JSON.stringify({action:step.proposal,result:step.result},null,2)));
    for (const id of [step.preview_task_id,step.task_id]) if (id && state.tasks.has(id)) details.append(taskCard(state.tasks.get(id)));
    card.append(details);
    if (step.skill_run_id) {
      const button=el("button","�鿴����ʵ�ʽ���","quiet");
      button.onclick=action(async()=>{const value=await api(`/api/v1/extensions/runs/${step.skill_run_id}`);button.replaceWith(el("pre",JSON.stringify(value,null,2)));});
      card.append(button);
    }
    box.append(card);
  }
  return box;
}
async function chatWorkAnswer(turn) {
  const step=turn.work_steps.find(s=>s.status==="permission");
  if (!step) throw new Error("��ȷ�϶����ѱ仯����ˢ�º�����");
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
