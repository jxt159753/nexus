import { api } from "./api.js";
import { el, button } from "./render.js";

export function renderAssistantSettings(parent) {
  const section = el("section", "settings-section assistant-settings");
  section.append(el("h2", "", "人格与表达"));
  const content = el("div");
  section.append(content);
  parent.append(section);

  async function load() {
    content.replaceChildren(el("p", "hint", "正在读取表达偏好…"));
    try {
      const value = await api("/api/v1/assistant/preferences");
      if (section.isConnected) show(value);
    } catch (error) {
      if (!section.isConnected) return;
      content.replaceChildren(el("p", "hint", error.status === 404
        ? "当前服务暂不支持人格设置，更新后即可使用。" : "暂时无法读取表达偏好。"));
      content.append(button("重新读取", "quiet", load));
    }
  }

  function show(value) {
    let saved = value.preferences;
    const form = el("form", "field");
    function select(label, name, entries, selected) {
      const field = el("div", "assistant-field");
      const caption = el("label", "", label);
      caption.htmlFor = `assistant-${name}`;
      field.append(caption);
      const input = el("select");
      input.id = caption.htmlFor;
      input.name = name;
      for (const entry of entries) {
        const option = el("option", "", entry.name);
        option.value = entry.id;
        input.append(option);
      }
      input.value = selected;
      field.append(input);
      form.append(field);
      return input;
    }
    const personality = select("选择人格", "personality", value.personalities, saved.personality);
    const description = el("p", "hint");
    form.append(description);
    const emoji = select("回复中的 Emoji", "emoji", value.emoji_modes, saved.emoji);
    const preview = el("blockquote", "assistant-preview");
    preview.setAttribute("aria-live", "polite");
    form.append(el("p", "hint", "表达示例"), preview);
    form.append(el("p", "hint", "保存到你的账户，各端共用，从下一轮对话生效。任务能力和权限不随人格改变。"));
    const save = el("button", "primary", "保存表达偏好");
    save.type = "submit";
    const status = el("p", "hint");
    status.setAttribute("role", "status");
    const reload = button("读取最新设置", "quiet", load);
    reload.hidden = true;
    let stale = false;
    function updatePreview() {
      const selected = value.personalities.find((item) => item.id === personality.value);
      description.textContent = selected.description;
      preview.textContent = selected.examples[emoji.value];
      save.disabled = stale || (personality.value === saved.personality && emoji.value === saved.emoji);
    }
    personality.addEventListener("change", updatePreview);
    emoji.addEventListener("change", updatePreview);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = personality.disabled = emoji.disabled = true;
      status.textContent = "正在保存…";
      try {
        const result = await api("/api/v1/assistant/preferences", { method: "PUT", body: JSON.stringify({
          personality: personality.value, emoji: emoji.value, expected_revision: saved.revision,
        }) });
        saved = result.preferences;
        status.textContent = "已保存，从下一轮对话生效。";
      } catch (error) {
        stale = error.status === 409;
        status.textContent = stale ? "设置已在其他设备更新，请读取最新设置后再修改。"
          : "尚未确认保存成功。可以读取最新设置核对，你的选择仍保留在此处。";
        reload.hidden = false;
      } finally {
        personality.disabled = emoji.disabled = false;
        updatePreview();
      }
    });
    form.append(save, reload, status);
    updatePreview();
    content.replaceChildren(form);
  }
  void load();
}
