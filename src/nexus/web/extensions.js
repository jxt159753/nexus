"use strict";
const extensionState = {ready:null, nodes:[], versions:[], runs:[], draft:null, loading:false};
const extensionTestLabels = {untested:"未测试", passed:"测试通过", failed:"测试失败", unavailable:"测试不可用"};
const extensionRunLabels = {running:"运行中", cancelling:"正在停止", succeeded:"已完成", failed:"失败", cancelled:"已取消", timed_out:"已超时"};
const extensionStepLabels = {pending:"待开始", dispatchable:"待派发", approval:"等待目录审批", running:"执行中", succeeded:"已成功", failed:"已失败", unknown:"结果未知", cancelled:"已取消"};

function extensionReset() {
  extensionState.ready=null; extensionState.nodes=[]; extensionState.versions=[]; extensionState.runs=[]; extensionState.draft=null;
  ["extension-outbound","extension-versions","extension-runs"].forEach(id=>{const node=$(id); if(node) node.replaceChildren();});
  if ($("extension-form")) $("extension-form").reset();
  if ($("extension-status")) $("extension-status").textContent="读取中";
}
function extensionBadge(label, kind="") {
  return el("span",label,"badge "+kind);
}
function extensionNodeOptions(selected="") {
  const select=$("extension-node");
  const current=selected || select.value;
  select.replaceChildren(new Option("不绑定节点",""),...extensionState.nodes.map(n=>new Option(`${n.node_id} · ${n.online?"在线":"离线"}`,n.node_id)));
  if (extensionState.nodes.some(n=>n.node_id===current)) select.value=current;
}
async function extensionStatus() {
  if (!state.logged) return;
  const value=await api("/api/v1/extensions");
  extensionState.ready=value.extension;
  const ready=value.extension;
  $("extension-status").textContent=ready.enabled ? (ready.fixture_generation ? "已启用 · 开发夹具" : ready.generation_enabled && ready.model_available ? "已启用 · 模型生成" : "已启用 · 生成未配置") : "已关闭";
  $("extension-status").className="badge "+(ready.enabled?"success":"pending");
}
function extensionDetails(version) {
  const details=el("details");
  details.append(el("summary","查看版本内容、契约与测试记录","hint"));
  details.append(el("pre",JSON.stringify({
    capability_id:version.capability_id,revision:version.revision,kind:version.kind,
    parent_version_id:version.parent_version_id,artifact_digest:version.artifact_digest,
    environment:version.environment,environment_digest:version.environment_digest,
    manifest:version.manifest,steps:version.steps,markdown:version.markdown,
    source:version.source,tests:version.tests,approval:version.approval,test:version.test
  },null,2)));
  return details;
}
function extensionTaskButton(taskId) {
  if (!taskId) return null;
  const button=el("button","查看步骤任务","quiet");
  button.onclick=action(async()=>{await updateTask(taskId);notify("任务已更新到任务视图");});
  return button;
}
function extensionVersionCard(version) {
  const card=el("article",undefined,"record"); card.dataset.versionId=version.version_id;
  const head=el("div",undefined,"section-heading");
  head.append(el("h3",`${version.kind==="skill"?"Skill":"Tool"} · ${version.capability_id}@r${version.revision}`),
    extensionBadge(version.enabled?"已启用":"未启用",version.enabled?"success":"pending"),
    extensionBadge(extensionTestLabels[version.test_status]||version.test_status,version.test_status==="passed"?"success":version.test_status==="failed"?"failed":"pending"));
  card.append(head,el("p",`摘要 ${version.artifact_digest.slice(0,16)}… · ${new Date(version.created_at).toLocaleString()}${version.approval?` · 批准至 ${new Date(version.approval.expires_at).toLocaleString()}`:""}`,"meta"));
  if (version.test?.status==="unavailable") card.append(el("p","测试不可用：缺少合格 Sandbox Node/镜像，不能批准或执行生成代码。","hint"));
  if (version.test?.status==="failed") card.append(el("p","测试失败；服务端拒绝批准。不能靠候选自报的 passed 通过。","hint"));
  if (version.approval) card.append(el("pre",JSON.stringify({decision:version.approval.decision,node_id:version.approval.node_id,expires_at:version.approval.expires_at,parameters_digest:version.approval.parameters_digest},null,2)));
  const buttons=el("div",undefined,"button-row");
  const test=el("button","运行受控测试","quiet");
  test.onclick=action(async()=>{test.disabled=true;try{await api(`/api/v1/extensions/versions/${version.version_id}/test`,{method:"POST",body:"{}"});await extensionList();notify("测试记录已更新");}finally{test.disabled=false;}});
  buttons.append(test);
  if (version.test_status==="passed" && version.test && version.approval_status!=="approved" && !version.enabled) {
    const approve=el("button","批准此具体版本","primary");
    approve.onclick=action(async()=>{approve.disabled=true;try{await api(`/api/v1/extensions/versions/${version.version_id}/approve`,{method:"POST",body:JSON.stringify({decision:"approve",artifact_digest:version.artifact_digest,test_id:version.test.test_id})});await extensionList();notify("已记录 owner 对具体版本的批准；仍需显式启用");}finally{approve.disabled=false;}});
    const reject=el("button","拒绝此版本","quiet");
    reject.onclick=action(async()=>{await api(`/api/v1/extensions/versions/${version.version_id}/approve`,{method:"POST",body:JSON.stringify({decision:"reject",artifact_digest:version.artifact_digest,test_id:version.test.test_id})});await extensionList();notify("已拒绝该版本");});
    buttons.append(approve,reject);
  }
  if (version.approval_status==="approved" && !version.enabled) {
    const enable=el("button","启用此版本","primary");
    enable.onclick=action(async()=>{enable.disabled=true;try{await api(`/api/v1/extensions/versions/${version.version_id}/enable`,{method:"POST",body:"{}"});await extensionList();notify("版本已启用；业务步骤仍需授权");}finally{enable.disabled=false;}});
    buttons.append(enable);
  }
  if (version.enabled) {
    const disable=el("button","停用","danger");
    disable.onclick=action(async()=>{await api(`/api/v1/extensions/versions/${version.version_id}/disable`,{method:"POST",body:JSON.stringify({reason:"owner disabled from capability view"})});await extensionList();notify("已停用；后续步骤不会再派发");});
    buttons.append(disable);
  }
  if (version.kind==="skill" && version.enabled) {
    const run=el("button","使用此 Skill","primary");
    run.onclick=action(async()=>{
      const node=$("extension-node").value || $("node").value;
      if (!node) throw new Error("请先配置并选择执行节点");
      run.disabled=true;
      try{const value=await api("/api/v1/extensions/runs",{method:"POST",body:JSON.stringify({version_id:version.version_id,node_id:node,input:{}})});await extensionList();notify(`Skill 已启动：${value.run.run_id.slice(0,8)}…`);}finally{run.disabled=false;}
    });
    buttons.append(run);
  }
  card.append(extensionDetails(version),buttons);
  return card;
}
function extensionRunCard(run) {
  const card=el("article",undefined,"record"); card.dataset.runId=run.run_id;
  card.append(el("h3",`${run.capability_id||"Skill"}@r${run.version_revision||"?"} · ${extensionRunLabels[run.status]||run.status}`),
    el("p",`${run.run_id} · 节点 ${run.node_id} · ${new Date(run.created_at).toLocaleString()}${run.verification?" · "+run.verification:""}`,"meta"));
  if (run.error) card.append(el("p",`${run.error.code} · 失败/未知不会自动重跑已完成步骤`,"hint"));
  const list=el("ol");
  for (const step of run.steps) {
    const item=el("li");
    item.append(el("strong",`${step.step_id} · ${extensionStepLabels[step.status]||step.status}`));
    const task=extensionTaskButton(step.task_id); if (task) item.append(task);
    if (step.result) item.append(el("pre",JSON.stringify(step.result,null,2)));
    list.append(item);
  }
  card.append(list);
  if (run.result) card.append(el("pre",JSON.stringify(run.result,null,2)));
  if (["running","cancelling"].includes(run.status)) {
    const cancel=el("button","停止这次 Skill","danger");
    cancel.onclick=action(async()=>{await api(`/api/v1/extensions/runs/${run.run_id}/cancel`,{method:"POST",body:"{}"});await extensionList();notify("已请求停止；以任务执行证据为准");});
    card.append(cancel);
  }
  return card;
}
function extensionRender() {
  $("extension-versions").replaceChildren(...extensionState.versions.map(extensionVersionCard));
  if (!extensionState.versions.length) $("extension-versions").append(el("p","还没有候选版本。先预览并生成一个请求。","empty"));
  $("extension-runs").replaceChildren(...extensionState.runs.map(extensionRunCard));
  if (!extensionState.runs.length) $("extension-runs").append(el("p","还没有 Skill 运行。","empty"));
}
async function extensionList() {
  if (!state.logged) return;
  const [versions,runs,nodes]=await Promise.all([api("/api/v1/extensions/versions"),api("/api/v1/extensions/runs"),api("/api/v1/nodes")]);
  if (!state.logged) return;
  extensionState.versions=versions.versions;
  extensionState.runs=runs.runs;
  extensionState.nodes=nodes.nodes;
  extensionNodeOptions();
  extensionRender();
}
async function extensionPage() {
  await Promise.all([extensionStatus(),extensionList()]);
}
function extensionPreviewBox(request) {
  extensionState.draft=request;
  const box=$("extension-outbound"); box.replaceChildren();
  box.append(el("p",`请求 ${request.request_id} · ${request.outbound.mode==="fixture"?"开发夹具（不调用模型）":"将外发 DeepSeek"}`,"hint"));
  box.append(el("pre",JSON.stringify(request.outbound,null,2)));
  box.append(el("p",`预览摘要 ${request.preview_digest} · 只有确认这份内容后才会生成候选。`,"hint"));
  $("extension-consent").disabled=false; $("extension-consent").checked=false; $("extension-generate").disabled=true;
}
function initExtensions() {
  $("extension-preview").onclick=action(async()=>{
    if (!state.logged) return;
    const need=$("extension-need").value.trim();
    if (!need) throw new Error("请先填写能力需求");
    const payload={request_id:crypto.randomUUID(),kind:$("extension-kind").value,need,capability_id:$("extension-capability").value.trim()||null,parent_version_id:$("extension-parent").value.trim()||null,node_id:$("extension-node").value||null};
    const value=await api("/api/v1/extensions/requests",{method:"POST",body:JSON.stringify(payload)});
    extensionPreviewBox(value.request);
    notify("外发预览已生成；核对后再生成候选");
  });
  $("extension-consent").onchange=()=>{$("extension-generate").disabled=!$("extension-consent").checked || !extensionState.draft;};
  $("extension-form").onsubmit=async(event)=>{
    event.preventDefault();
    const draft=extensionState.draft;
    if (!draft || !$("extension-consent").checked) return;
    $("extension-generate").disabled=true;
    try {
      await api(`/api/v1/extensions/requests/${draft.request_id}/generate`,{method:"POST",body:JSON.stringify({preview_digest:draft.preview_digest,allow_model:true})});
      extensionState.draft=null; $("extension-consent").checked=false; $("extension-outbound").replaceChildren();
      notify("生成请求已保存；候选需要测试和你的逐版批准");
      setTimeout(()=>{extensionPage().catch(error=>notify(error.message,true));},400);
    } finally {$("extension-generate").disabled=true;}
  };
  $("extension-refresh").onclick=action(async()=>{await extensionPage();notify("能力状态已刷新");});
  setInterval(()=>{if (state.logged && !$("extensions-view").hidden && !extensionState.loading) {extensionState.loading=true; extensionList().catch(error=>notify(error.message,true)).finally(()=>{extensionState.loading=false;});}},2500);
}
initExtensions();
