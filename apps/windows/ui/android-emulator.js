import { api, state, nodeName, uuid } from './api.js';
import { el } from './render.js';

const LABELS = { stopped: '已停止，应用和登录状态已保留', starting: '正在启动，请稍后查看状态',
  stopping: '正在关闭并保存数据，请稍后查看状态',
  ready: '虚拟手机已就绪', failed: '启动失败，请检查本机模拟器环境' };

// A button click is the owner's explicit approval of this exact request. All
// actions still pass through Harness approvals, task records and node receipts.
export async function phoneAction(release, nodeId, conversationId, tool, input = {}) {
  const submitted = await api('/api/v1/tasks', { method: 'POST', body: JSON.stringify({
    node_id: nodeId, request: { request_id: uuid(), intent: 'execute_plugin', arguments: {
      plugin_id: release.plugin_id, tool, input, release_digest: release.digest,
      context_id: conversationId, invocation_id: uuid(),
    } },
  }) });
  const taskId = submitted.task.task_id;
  const approved = new Set();
  const until = Date.now() + 60000;
  while (Date.now() < until) {
    const detail = await api(`/api/v1/tasks/${taskId}`);
    const task = detail.task;
    if (task.status === 'awaiting_approval') {
      const approval = detail.approvals.find((item) => !item.decision && !approved.has(item.approval_id));
      if (approval) {
        approved.add(approval.approval_id);
        await api(`/api/v1/approvals/${approval.approval_id}`, { method: 'POST', body: JSON.stringify({ decision: 'approve' }) });
      }
    }
    if (task.status === 'succeeded' && task.execution_outcome === 'known' && task.verification) return task.result.data;
    if (['failed', 'cancelled', 'timed_out'].includes(task.status)) {
      throw new Error(task.error?.message || '本次操作未完成，请查看执行记录后再试');
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`仍在等待执行结果，请在任务中查看 ${taskId}；不要重复提交相同操作`);
}

export function renderAndroidEmulator(body, release, conversationId) {
  const box = el('section', 'plugin-row');
  box.append(el('h2', '', 'NEXUS 虚拟手机'), el('p', '', '使用电脑上已配置的 MuMu 或 Android 模拟器。应用与登录状态保留在原实例，电脑关闭后不可用。'));
  const target = el('select');
  target.setAttribute('aria-label', '运行虚拟手机的电脑');
  for (const node of state.nodes.filter((item) => item.tools?.includes('plugin.call@1'))) {
    target.append(new Option(`${nodeName(node.node_id)}${node.online ? '' : '（离线）'}`, node.node_id));
  }
  const message = el('p', 'hint', '点击启动，然后查看状态。首次启动可能需要一些时间。');
  const controls = el('div', 'actions');
  const apps = el('select'); apps.setAttribute('aria-label', '虚拟手机已安装应用');
  apps.append(new Option('先点击“查看应用”', ''));
  const appNames = { 'com.tencent.mm': '微信', 'com.ss.android.ugc.aweme': '抖音',
    'com.ss.android.ugc.aweme.mobile': '抖音', 'com.max.xiaoheihe': '小黑盒' };
  const image = el('img');
  image.alt = '虚拟手机最近一次截图'; image.hidden = true;
  image.style.cssText = 'max-width:100%;max-height:62vh;margin:12px auto;cursor:crosshair;';
  let frame = null;
  let busy = false;
  const buttons = [];
  const operation = async (name, input = {}) => {
    if (busy || !box.isConnected) return;
    if (!target.value || !conversationId) { message.textContent = '请先选择一个会话，并连接已配置虚拟手机的 Windows 设备。'; return; }
    busy = true; buttons.forEach((button) => button.disabled = true); target.disabled = true;
    message.textContent = '正在执行…';
    try {
      const result = await phoneAction(release, target.value, conversationId, name, input);
      if (!box.isConnected) return;
      if (name === 'observe') {
        frame = result;
        const picture = result.image;
        if (!picture || !['image/png', 'image/jpeg'].includes(picture.media_type)) throw new Error('截图无效');
        image.src = `data:${picture.media_type};base64,${picture.data_base64}`;
        image.hidden = false;
        message.textContent = '已获取画面；可点击图中目标。操作后请再次点击“查看画面”。';
      } else if (name === 'list_apps') {
        apps.replaceChildren(...result.packages.filter((name) => name !== 'ai.nexus.emulatorinput').map((name) => new Option(appNames[name] || name, name)));
        message.textContent = apps.options.length ? '选择应用后点击打开。' : '尚未安装应用，请在本机虚拟手机窗口下载安装。';
      } else {
        if (name !== 'status') frame = null;
        message.textContent = (LABELS[result.status] ? `${result.name ? result.name + '：' : ''}${LABELS[result.status]}` : '') || result.notice || '操作已返回，请查看画面核验。';
        if (name === 'stop') { image.hidden = true; image.removeAttribute('src'); }
      }
    } catch (error) {
      if (box.isConnected) message.textContent = error.message || '操作失败，请查看任务记录';
    } finally {
      busy = false; target.disabled = false; buttons.forEach((button) => button.disabled = false);
    }
  };
  const add = (label, action) => {
    const button = el('button', 'quiet', label); button.type = 'button';
    button.addEventListener('click', action); buttons.push(button); controls.append(button);
  };
  add('启动虚拟手机', () => operation('start'));
  add('查看状态', () => operation('status'));
  add('查看画面', () => operation('observe'));
  add('停止虚拟手机', () => operation('stop'));
  add('查看应用', () => operation('list_apps'));
  add('打开所选应用', () => { if (apps.value) operation('open_app', { package: apps.value }); });
  const act = (action, extra = {}) => {
    if (!frame) { message.textContent = '请先查看最新画面。'; return; }
    operation('act', { observation_digest: frame.observation_digest, action, ...extra });
  };
  add('返回', () => act('back')); add('主页', () => act('home')); add('最近应用', () => act('recents'));
  add('向上滑动', () => frame ? act('swipe', { x: Math.floor(frame.image.width/2), y: Math.floor(frame.image.height*.75), end_x: Math.floor(frame.image.width/2), end_y: Math.floor(frame.image.height*.3) }) : act('swipe'));
  image.addEventListener('click', (event) => {
    if (!frame || busy) return;
    const rect = image.getBoundingClientRect();
    act('tap', { x: Math.min(frame.image.width-1, Math.floor((event.clientX-rect.left)*frame.image.width/rect.width)),
      y: Math.min(frame.image.height-1, Math.floor((event.clientY-rect.top)*frame.image.height/rect.height)) });
  });
  const text = el('input'); text.type = 'text'; text.maxLength = 2000;
  text.placeholder = '先在手机中选中输入框，再输入文字（支持中文）';
  text.setAttribute('aria-label', '输入到虚拟手机的文字');
  const typing = el('button', 'quiet', '输入文字'); typing.type = 'button'; buttons.push(typing);
  typing.addEventListener('click', () => {
    if (!frame) { message.textContent = '请先点击目标输入框，再查看最新画面。'; return; }
    if (text.value) operation('type_text', { observation_digest: frame.observation_digest, text: text.value });
  });
  target.addEventListener('change', () => { frame = null; apps.replaceChildren(new Option('先点击“查看应用”', '')); image.hidden = true; image.removeAttribute('src'); });
  const inputRow = el('div', 'actions'); inputRow.append(text, typing);
  box.append(target, controls, apps, message, image, inputRow, el('p', 'hint', '画面按需获取，不持续录屏。账号注册、密码、验证码和系统授权请在电脑弹出的模拟器窗口完成。完成后可让 NEXUS 查看并操作已登录的应用。'));
  if (!release?.enabled) { buttons.forEach((button) => button.disabled = true); message.textContent = '请先启用虚拟手机插件。'; }
  body.append(box);
}
