"use strict";

import { api, uuid } from "./api.js";
import { el, button, openSheet, closeSheet, notify } from "./render.js";

export async function openReminderCenter(openConversation, openEvents) {
  const body = el("div", "reminder-center");
  const controls = el("section", "settings-section");
  const records = el("section", "settings-section");
  const error = el("p", "error");
  const native = window.__TAURI__?.core?.invoke;
  const clientKey = "nexus.reminder.client-id";
  const name = document.createElement("input");
  name.placeholder = "这台电脑的提醒名称"; name.maxLength = 80;
  name.value = "我的电脑";
  if (native) {
    controls.append(name, button("开启本机系统提醒", "primary", async () => {
      try {
        let id = localStorage.getItem(clientKey);
        if (!id) { id = uuid(); localStorage.setItem(clientKey, id); }
        const result = await api("/api/v1/reminders/clients", { method: "POST", body: JSON.stringify({client_id:id,name:name.value.trim() || "我的电脑",platform:"windows"}) });
        await native("reminders_start", {token:result.token,clientId:result.client_id});
        notify("本机提醒已开启；关闭到托盘后仍可接收"); await refresh();
      } catch (e) { error.textContent = `未能开启提醒：${e.message || e}`; }
    }), button("停止本机接收", "quiet", async () => {
      try {
        await native("reminders_stop");
        const id = localStorage.getItem(clientKey);
        if (id) await api(`/api/v1/reminders/clients/${id}`, {method:"DELETE"});
        notify("本机接收已停止，Core 任务仍按原计划运行"); await refresh();
      } catch (e) { error.textContent = `本机已停止；请核对服务端接收状态：${e.message || e}`; }
    }));
  }
  controls.append(el("p", "hint", "在对话中说明工作内容、时间和提醒设备，即可创建定时或长期任务。周期任务默认持续到手动停止；一次性任务只触发一次。"));
  if (openEvents) controls.append(button("观察事件与运行健康", "quiet", () => { closeSheet(); openEvents(); }));
  body.append(controls, error, records);
  async function act(action) {
    error.textContent = "";
    try { await action(); await refresh(); } catch (e) { error.textContent = `操作未完成：${e.message || e}`; }
  }
  async function refresh() {
    const [inbox, automation, clients] = await Promise.all([
      api("/api/v1/reminders"), api("/api/v1/plugins/automation"), api("/api/v1/reminders/clients"),
    ]);
    records.replaceChildren(el("h3", "", "接收设备"));
    for (const client of clients.clients) {
      const row = el("div", "autonomy-row");
      row.append(el("strong", "", client.name), el("p", "hint", `${client.platform} · ${client.enabled ? "已启用" : "已关闭"} · 最近连接 ${client.last_seen_at ? new Date(client.last_seen_at).toLocaleString() : "尚未连接"}`));
      if (client.enabled) row.append(button("停用接收", "quiet", () => act(() => api(`/api/v1/reminders/clients/${client.client_id}`, {method:"DELETE"}))));
      records.append(row);
    }
    if (!clients.clients.length) records.append(el("p", "hint", "在手机或 Windows 客户端开启系统提醒后，这里会显示接收设备。"));
    records.append(el("h3", "", "定时与长期任务"));
    for (const item of [...automation.schedules, ...automation.goals]) {
      const schedule = !!item.schedule_id;
      const id = item.schedule_id || item.goal_id;
      const paused = schedule ? !!item.paused : item.status !== "active";
      const ended = schedule && !item.run_until_stopped && (item.runs >= item.max_runs || new Date(item.expires_at).getTime() <= Date.now());
      const row = el("article", "autonomy-row");
      row.append(el("strong", "", item.title || item.objective), el("p", "hint", `${item.status === "complete" ? "已完成" : ended ? "已结束" : paused ? "已暂停" : "进行中"} · ${item.run_until_stopped ? "无结束日期" : `截至 ${new Date(item.expires_at).toLocaleString()}`}`));
      if (schedule && !ended) row.append(el("p", "", `下次 ${new Date(item.next_at).toLocaleString()}`));
      const path = `/api/v1/plugins/conversations/${item.conversation_id}/${schedule ? "schedules" : "goals"}/${id}`;
      if (!ended && item.status !== "complete") row.append(button(paused ? "恢复" : "暂停", "quiet", () => act(() => api(path, {method:"PATCH",body:JSON.stringify({request_id:uuid(),changes:schedule ? {paused:!paused} : {status:paused ? "active" : "paused"}})}))));
      if (schedule) row.append(button("停止后续运行", "quiet", () => act(() => api(path, {method:"DELETE",body:JSON.stringify({request_id:uuid()})}))));
      row.append(button("来源会话", "quiet", () => { closeSheet(); openConversation(item.conversation_id); }));
      records.append(row);
    }
    records.append(el("h3", "", "提醒内容"));
    if (!inbox.reminders.length) records.append(el("p", "hint", "暂无提醒"));
    for (const item of inbox.reminders) {
      const row = el("article", "autonomy-row");
      row.append(el("strong", "", item.title), el("p", "", item.body), el("p", "hint", `${new Date(item.created_at).toLocaleString()} · ${item.read_at ? "已读" : "未读"}`));
      for (const delivery of item.deliveries) row.append(el("p", "hint", `${delivery.name}：${delivery.posted_at ? "客户端已报告提交系统通知" : "等待客户端接收"}`));
      if (!item.read_at) row.append(button("标记已读", "quiet", () => act(() => api(`/api/v1/reminders/${item.reminder_id}/read`, {method:"POST"}))));
      row.append(button("打开结果", "quiet", () => { closeSheet(); openConversation(item.conversation_id); }));
      records.append(row);
    }
  }
  openSheet({title:"提醒与持续任务",body,actions:[{label:"刷新",className:"quiet",onClick:()=>act(async()=>{})},{label:"关闭",className:"quiet",onClick:closeSheet}]});
  await act(async () => {});
  // Only the open panel polls. Closing/replacing it stops the next refresh.
  const poll = async () => {
    if (!body.isConnected) return;
    if (!document.hidden) {
      try { await refresh(); } catch (e) { error.textContent = `同步未完成：${e.message || e}`; }
    }
    if (body.isConnected) setTimeout(poll, 5000);
  };
  setTimeout(poll, 5000);
}
