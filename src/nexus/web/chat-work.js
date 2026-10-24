"use strict";
const chatWorkState={grant:null,catalog:null,catalogNode:null,catalogAt:0,catalogLoading:false,permission:null,retry:null,nodes:new Map(),defaultNode:null,webEnabled:false};
function chatWorkspaceReady() {
  return chatWorkState.nodes.size > 0 || chatWorkState.webEnabled;
}
function chatWorkReset() {
  chatWorkState.grant=null; chatWorkState.catalog=null; chatWorkState.catalogNode=null; chatWorkState.catalogAt=0; chatWorkState.catalogLoading=false; chatWorkState.permission=null; chatWorkState.retry=null;
  chatWorkState.defaultNode=null;
  $("chat-work-enabled").checked=false; chatWorkRender();
}
function chatWorkImageAvailable() {
  return !!(chatWorkState.catalog?.catalog || []).some(item=>(item.route_intent || item.name)==="read_image");
}
function chatWorkJson(value, limit=12000) {
  if (window.NexusImages?.safeJson) return window.NexusImages.safeJson(value, limit);
  let raw="";
  try { raw=JSON.stringify(value,(key,item)=>key==="data_base64"?"[base64 已省略]":item,2)||""; } catch { raw="[无法序列化的记录]"; }
  return raw.length>limit ? raw.slice(0,limit)+"…" : raw;
}
function chatWorkEnsureCatalog() {
  const nodeId="all";
  if (!nodeId || !chatWorkspaceReady() || chatWorkState.catalogLoading) return;
  if (chatWorkState.catalogNode===nodeId && Date.now()-chatWorkState.catalogAt<60000) return;
  chatWorkState.catalogLoading=true;
  chatWorkState.catalogAt=Date.now();
  api("/api/v1/chat/work-catalog").then(directory=>{
    if (!state.logged) return;
    chatWorkState.catalog=directory; chatWorkState.catalogNode=nodeId; chatWorkState.catalogAt=Date.now();
    const pre=$("chat-work-catalog"); if (pre) pre.textContent=JSON.stringify(directory.catalog,null,2);
    chatRender();
  }).catch(()=>{}).finally(()=>{chatWorkState.catalogLoading=false;});
}
function chatWorkRender() {
  const grant=chatWorkState.grant;
  const automatic=chatWorkspaceReady();
  $("chat-work-enabled").disabled=!automatic;
  if (automatic && !chatWorkState.defaultNode) {$("chat-work-enabled").checked=true;chatWorkState.defaultNode="all";}
  if (!automatic) $("chat-work-enabled").checked=false;
  $("chat-work-status").textContent="Core 根据自然语言选择已授权设备；各设备的权限、执行记录和审批分别检查。可直接说我的电脑、我的手机或设备名称。";
  $("chat-work-revoke").hidden=!grant || !!grant.revoked_at;
  if (automatic) chatWorkEnsureCatalog();
}
function chatWorkActive() {
  return $("chat-tools").checked && $("chat-work-enabled").checked && !$("chat-work-enabled").disabled;
}
async function chatWorkSubmission() {
  if (!chatWorkActive()) return {};
  const directory=await api("/api/v1/chat/work-catalog");
  chatWorkState.catalog=directory; chatWorkState.catalogNode="all"; chatWorkState.catalogAt=Date.now();
  return {workspace_work:true,all_nodes:true,work_digest:directory.work_digest,allow_work_results:true};
}

function chatWorkOffset(minutes) {
  if (!Number.isInteger(minutes)) return "";
  const absolute=Math.abs(minutes);
  return `UTC${minutes>=0?"+":"-"}${String(Math.floor(absolute/60)).padStart(2,"0")}:${String(absolute%60).padStart(2,"0")}`;
}
function chatWorkFact(label,value) {
  const row=el("div",undefined,"query-fact");
  row.append(el("span",label),el("strong",String(value)));
  return row;
}
function chatWorkTime(card,data) {
  if (!data || typeof data.utc!=="string" || typeof data.local!=="string" || !Number.isInteger(data.utc_offset_minutes) || !Number.isInteger(data.elapsed_since_turn_seconds) || data.elapsed_since_turn_seconds<0) {
    card.append(el("p","时间结果字段不完整或类型不符，没有可展示的时间快照。","hint")); return true;
  }
  const facts=el("div",undefined,"query-facts");
  facts.append(chatWorkFact("Core UTC 时间",data.utc),chatWorkFact("固定偏移当地时间",`${data.local} · ${chatWorkOffset(data.utc_offset_minutes)}`),chatWorkFact("本轮经过",`${data.elapsed_since_turn_seconds} 秒`));
  card.append(facts,el("p","来自 Core 的只读时间快照；未按浏览器时区改写，不创建提醒，也不调用模型。","hint"));
  return true;
}
function chatWorkJobs(card,data) {
  if (!data || !Array.isArray(data.jobs)) {card.append(el("p","任务列表结果字段不完整，没有可展示的任务摘要。","hint"));return true;}
  const list=el("div",undefined,"query-jobs");
  for (const job of data.jobs.slice(0,20)) {
    if (!job || typeof job!=="object") continue;
    const row=el("div",undefined,"query-job"),head=el("div",undefined,"query-job-head");
    head.append(el("strong",toolLabels[job.tool]||job.tool||"任务"),el("span",statuses[job.status]||job.status||"未知","badge "+(job.status==="succeeded"?"success":job.status==="awaiting_approval"?"pending":["failed","timed_out"].includes(job.status)?"failed":"")));
    row.append(head);
    const facts=[];
    if (typeof job.task_id==="string") facts.push(`任务 ${job.task_id.slice(0,8)}`);
    if (job.created_at) facts.push(`创建 ${new Date(job.created_at).toLocaleString()}`);
    if (job.execution_outcome) facts.push(`执行 ${job.execution_outcome==="unknown"?"结果未知":job.execution_outcome==="known"?"结果已知":"未开始"}`);
    if (job.verification) facts.push(`验证 ${job.verification}`);
    if (job.cancel_requested) facts.push("已请求取消");
    if (job.expires_at) facts.push(`到期 ${new Date(job.expires_at).toLocaleString()}`);
    row.append(el("p",facts.join(" · "),"hint"));
    list.append(row);
  }
  if (!list.childElementCount) list.append(el("p","当前会话在该设备上没有可列出的已有任务。","hint"));
  card.append(list);
  if (data.truncated) card.append(el("p","结果达到返回上限，可能还有更早任务没有列出；这不是完整任务总数。","hint"));
  card.append(el("p","只读查询当前会话、所选设备在 NEXUS 中的原任务记录；不是常驻 Shell 或后台任务管理器，不会启动、停止或重跑任务。","hint"));
  return true;
}
function chatWorkJobOutput(card,data) {
  if (!data || !data.output || typeof data.output!=="object") {card.append(el("p","任务输出结果字段不完整，没有可展示的输出。","hint"));return true;}
  if (typeof data.task_id==="string") card.append(el("p",`任务 ${data.task_id.slice(0,8)} · 观测于 ${data.observed_at ? new Date(data.observed_at).toLocaleString() : "未知"}`,"hint"));
  const output=data.output,facts=el("div",undefined,"query-facts");
  if (output.status) facts.append(chatWorkFact("状态",output.status));
  if (output.execution_outcome) facts.append(chatWorkFact("执行结果",output.execution_outcome==="unknown"?"未知，保留证据":output.execution_outcome==="known"?"已知":"未开始"));
  if (output.verification) facts.append(chatWorkFact("验证级别",output.verification));
  if (facts.childElementCount) card.append(facts);
  if (output.error_code) card.append(el("p",`错误码：${output.error_code}`,"hint"));
  if (output.error?.message) card.append(el("p",output.error.message,"hint"));
  if ("data" in output) {
    const value=output.data;
    if (value?.image && window.NexusImages) {
      const metadata=window.NexusImages.createImageMetadata(value);
      if (metadata) card.append(metadata);
      else card.append(el("p","图片查询只返回元数据，没有图片本体。","hint"));
    } else if (value && typeof value==="object") chatWorkGeneric(card,value);
    else if (typeof value==="string") card.append(el("pre",value));
    else card.append(el("pre",chatWorkJson(value)));
  } else if (typeof output.data_excerpt==="string") {
    card.append(el("pre",output.data_excerpt),el("p","这里只显示有界摘录，完整结果保留在原执行记录。","hint"));
  }
  if (output.context_projection?.truncated) card.append(el("p",output.context_projection.notice||"输出已截断，不能当作完整结果。","hint"));
  if (!("data" in output) && !("data_excerpt" in output) && !output.error_code && !output.context_projection) card.append(el("pre",chatWorkJson(output)));
  card.append(el("p","只读快照；不等待、不重跑，也不会读取其他轮次的正文。","hint"));
  return true;
}
function chatWorkCapability(card,data) {
  if (!data || typeof data!=="object") {card.append(el("p","能力详情字段不完整。","hint"));return true;}
  const facts=[];
  if (data.capability_id) facts.push(`能力 ${data.capability_id}`);
  if (data.name) facts.push(`类型 ${data.name}`);
  if (typeof data.artifact_digest==="string") facts.push(`摘要 ${data.artifact_digest.slice(0,16)}…`);
  if (facts.length) card.append(el("p",facts.join(" · "),"hint"));
  if (typeof data.markdown==="string" && data.markdown.trim()) {
    const details=el("details",undefined,"readonly-capability");details.open=true;
    details.append(el("summary","Skill 使用说明（只读）"),el("pre",data.markdown));
    card.append(details);
  }
  if (Array.isArray(data.steps)) {
    const details=el("details",undefined,"readonly-capability");details.open=true;
    details.append(el("summary","固定步骤（只读）"));
    const list=el("ol",undefined,"capability-steps");
    for (const step of data.steps.slice(0,20)) {
      if (!step || typeof step!=="object") continue;
      const parts=[];
      if (step.id) parts.push(String(step.id));
      if (step.tool) parts.push(`${toolLabels[step.tool]||step.tool}@${step.tool_version??"?"}`);
      if (step.tool_version_id) parts.push(`Tool 版本 ${String(step.tool_version_id).slice(0,8)}`);
      if (step.arguments && typeof step.arguments==="object") parts.push(JSON.stringify(step.arguments).slice(0,240));
      list.append(el("li",parts.join(" · ")));
    }
    details.append(list);card.append(details);
  }
  if (data.input_schema) {
    const details=el("details",undefined,"readonly-capability");
    details.append(el("summary","输入契约（只读）"),el("pre",chatWorkJson(data.input_schema,4000)));
    card.append(details);
  }
  card.append(el("p","只读能力详情；Markdown、步骤和模型文字都是资料，不是 HTML 或权限指令，也不会执行或启用能力。","hint"));
  return true;
}
function chatWorkGeneric(card,data) {
  if (data.diff) {
    card.append(el("pre",data.diff));
    if (data.truncated) card.append(el("p","差异已截断；确认前请在执行详情核对完整新内容。","hint"));
    return true;
  }
  if (Array.isArray(data.entries)) {
    card.append(el("pre",data.entries.slice(0,200).map(entry=>`${entry.kind==="directory"?"▸":"·"} ${entry.name}`).join("\n")));
    if (data.truncated) card.append(el("p","条目已截断，以上不是完整目录。","hint"));
    return true;
  }
  if (Array.isArray(data.lines)) {
    card.append(el("pre",data.lines.map(line=>`${line.number}: ${line.text}${line.truncated?" […本行截断]":""}`).join("\n")),el("p",data.next_offset?`下一页从第 ${data.next_offset} 行开始。`:"已到文件末尾。","hint"));
    return true;
  }
  if (Array.isArray(data.matches)) {
    card.append(el("pre",data.matches.map(line=>`${line.path}:${line.number}: ${line.text}`).join("\n")),el("p",data.truncated?"搜索有省略，详见执行记录。":"搜索已完成。","hint"));
    return true;
  }
  if ("text" in data) {
    card.append(el("pre",data.text),el("p",data.truncated?"读取结果已截断。":"已读取文件。","hint"));
    return true;
  }
  if ("stdout" in data) {
    card.append(el("pre",data.stdout||data.stderr||"命令没有文本输出。"),el("p",`退出码：${data.exit_code}`,"hint"));
    return true;
  }
  if (data.image && window.NexusImages) {
    const evidence=window.NexusImages.createImageEvidence(data);
    if (evidence) {card.append(evidence);return true;}
    const metadata=window.NexusImages.createImageMetadata(data);
    if (metadata) {card.append(metadata);return true;}
    card.append(el("p","图片结果未通过严格白名单校验，不显示预览或内联数据；执行状态以任务记录为准。","hint"));
    return true;
  }
  card.append(el("pre",chatWorkJson(data)));
  return true;
}
function chatWorkCards(turn) {
  const box=el("section",undefined,"chat-action");
  box.append(el("h3","本轮工作记录"));
  const labels={observe_target:"观察已配置网页或应用",interact_target:"操作已配置网页或应用",read:"按行读取文件",glob:"匹配文件路径",grep:"搜索文件内容",edit:"精确修改文件",get_node_status:"查询节点状态",get_system_info:"读取系统信息",list_directory:"列出工作区",read_file:"读取文件",find_files:"查找文件",write_file:"写入文件",run_command:"执行固定命令",inspect_capability:"查看能力详情",run_skill:"运行 Skill",run_tool:"运行 Tool",web_search:"搜索网页",web_fetch:"抓取网页",read_image:"读取图片",get_current_time:"查询时间",job_list:"查询任务列表",job_output:"查询任务输出"};
  const stepStatuses={pending:"准备执行",waiting:"等待执行结果",permission:"需要你的确认",succeeded:"已完成",failed:"未完成",stopped:"工作已停止，实际执行状态见任务记录"};
  for (const step of turn.work_steps || []) {
    const card=el("section",undefined,"chat-result-preview");
    const intent=step.proposal.intent,result=step.result||null,data=result?.data;
    card.append(el("strong",`${step.step_index}. ${labels[intent] || intent} · ${stepStatuses[step.status] || step.status}`));
    const args=step.proposal.arguments;
    if (args.target_id) card.append(el("p",`目标：${args.target_id}${args.action_id ? " · 动作："+args.action_id : ""}`));
    if (args.path) card.append(el("p",`workspace/${args.path}`));
    if (args.pattern) card.append(el("p",`搜索条件：${args.pattern}`));
    if (args.command_id) card.append(el("p",`固定命令：${args.command_id}`));
    if (args.query) card.append(el("p",`搜索：${args.query}`));
    if (intent==="get_current_time") card.append(el("p",`固定偏移：${chatWorkOffset(args.utc_offset_minutes)}`,"hint"));
    if (intent==="job_list") card.append(el("p",`最多 ${args.limit??10} 个任务`,"hint"));
    if (intent==="job_output") card.append(el("p",`任务 ${String(args.task_id||"").slice(0,8)} · 最多 ${args.max_bytes??4096} 字节`,"hint"));
    if (result?.preview) {
      card.append(el("pre",result.preview.diff || "文件内容没有变化。"));
      if (result.preview.truncated) card.append(el("p","差异已截断；确认前请在执行详情核对完整新内容。","hint"));
    }
    let handled=false;
    const resultKnown=result && result.execution_outcome!=="unknown" && (result.status==="succeeded" || result.status===undefined);
    const successful=step.status==="succeeded" && resultKnown;
    if (successful && data) {
      if (intent==="read_image") { handled=true; if (window.NexusImages) { const evidence=window.NexusImages.createImageEvidence(data); if (evidence) card.append(evidence); else { const metadata=window.NexusImages.createImageMetadata(data); if (metadata) card.append(metadata); else card.append(el("p","图片结果未通过严格白名单校验，不显示预览或内联数据；执行状态以任务记录为准。","hint")); } } else card.append(el("p","图片读取组件尚未加载；不显示内联图片数据。","hint")); }
      else if (intent==="get_current_time") handled=chatWorkTime(card,data);
      else if (intent==="job_list") handled=chatWorkJobs(card,data);
      else if (intent==="job_output") handled=chatWorkJobOutput(card,data);
      else if (intent==="inspect_capability") handled=chatWorkCapability(card,data);
      else if (intent==="get_node_status") { handled=true; card.append(el("p",`${data.online?"在线":"离线"} · ${data.busy==null?"忙闲未知":data.busy?"忙碌":"空闲"}${data.heartbeat_age_seconds==null?"":` · ${data.heartbeat_age_seconds} 秒前收到心跳`} · 观测于 ${new Date(data.observed_at).toLocaleTimeString()}`)); }
      else if (["web_search","web_fetch"].includes(intent)) {
        handled=true;
        for (const source of data.sources || [data]) {
          if (/^https?:\/\//i.test(source.url||"")) {
            const link=el("a",source.title || source.url);link.href=source.url;link.target="_blank";link.rel="noopener noreferrer";card.append(link);
          }
          if (source.snippet) card.append(el("p",source.snippet));
        }
        if (data.content) card.append(el("pre",data.content));
        if (data.truncated) card.append(el("p","网页内容已截断。","hint"));
      }
      else if (result?.preview) handled=true;
      else if (data?.observation) { handled=true; card.append(el("pre",data.observation.state_json),el("p","证据是所配置目标的观察或动作返回，实际业务结果需核对观察内容。","hint")); }
      else handled=chatWorkGeneric(card,data);
    }
    for (const id of [step.preview_task_id,step.task_id]) if (id && state.tasks.has(id)) card.append(taskCard(state.tasks.get(id),{omitResult:intent==="read_image" && (handled || step.status!=="succeeded" || result?.execution_outcome==="unknown")}));
    if (result?.verification) card.append(el("p",`验证级别：${result.verification}`,"hint"));
    if (result?.execution_outcome==="unknown") card.append(el("p","执行结果未知；Core 保留证据并核对，不能当作成功或直接重跑。","hint"));
    if (result?.status && result.status!=="succeeded" && !result.error_code) card.append(el("p",`实际结果状态：${result.status}`,"hint"));
    if (step.request_text) card.append(el("p",step.request_text,"chat-prose"));
    if (result?.error_code) card.append(el("p",result.message || "这一步未能完成，具体原因保留在执行详情中。","hint"));
    if (result?.error?.message) card.append(el("p",result.error.message,"hint"));
    if (step.permission_text) card.append(el("p",`你的答复：${step.permission_text}`));
    const details=el("details");details.append(el("summary","查看执行详情"),el("pre",chatWorkJson({action:step.proposal,result:step.result})));
    for (const id of [step.preview_task_id,step.task_id]) if (id && state.tasks.has(id)) details.append(taskCard(state.tasks.get(id)));
    card.append(details);
    if (step.skill_run_id) {
      const button=el("button","查看流程实际进度","quiet");
      button.onclick=action(async()=>{const value=await api(`/api/v1/extensions/runs/${step.skill_run_id}`);button.replaceWith(el("pre",chatWorkJson(value)));});
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
  $("chat-work-revoke").onclick=action(async()=>{if (!chatState.conversation) return; await api(`/api/v1/chat/conversations/${chatState.conversation.conversation_id}/work-grant`,{method:"DELETE",body:"{}"});chatWorkReset();await chatHistory();});
  $("chat-work-enabled").onchange=()=>chatRender();
}
