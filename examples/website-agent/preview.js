import { renderReport } from "./report-view.js";
import { createAgentClient } from "./browser-client.js";
import { createPreviewApi } from "./preview-model.js";

const element = (selector) => document.querySelector(selector);
const report = element("#report"), directory = element("#scenarios"), note = element("#scenario-note");
const history = element("#history"), responseText = element("#response");
let client, currentId, current, before = null;
const busy = async (work) => { try { await work(); } catch (error) { note.textContent = error.message; } };

function showHistory(entries, prepend = false) {
  if (!prepend) history.replaceChildren();
  const fragment = document.createDocumentFragment();
  for (const entry of entries) {
    const row = document.createElement("li"); row.dataset.historyId = entry.id;
    if (entry.type === "user.message") row.textContent = `用户：${entry.message.text}`;
    if (entry.type === "assistant.question") row.textContent = `追问：${entry.questions.join("；")}`;
    if (entry.type === "diagnosis.result") {
      const button = document.createElement("button");
      button.textContent = entry.result.report_state === "READY" ? "查看这一轮报告" : `查看结束记录（${entry.result.status}）`;
      button.onclick = () => busy(() => select(currentId, entry.run_id)); row.append(button);
    }
    fragment.append(row);
  }
  if (prepend) history.prepend(fragment); else history.append(fragment);
}
async function refreshDirectory() {
  const page = await client.conversations.list(); directory.replaceChildren();
  for (const item of page.items) {
    const button = document.createElement("button"); button.type = "button";
    button.textContent = item.title; button.setAttribute("aria-pressed", String(item.conversation_id === currentId));
    button.onclick = () => busy(() => select(item.conversation_id)); directory.append(button);
  }
  return page.items;
}
async function select(id, run_id) {
  currentId = id; current = await client.conversations.get(id, { run_id, history_limit: 3 });
  before = current.history_next_cursor;
  element("#conversation-title").textContent = current.title;
  note.textContent = `第 ${current.current_run.ordinal} 轮 · ${current.current_run.status}。所有操作仅修改浏览器内的合成样例。`;
  element("#stop").disabled = !current.capabilities.can_stop;
  element("#rediagnose").disabled = !current.capabilities.can_rediagnose;
  element("#older").disabled = before === null;
  renderReport(report, current.result); showHistory(current.history);
  responseText.textContent = JSON.stringify({ ok: true, data: current, error: null }, null, 2);
  await refreshDirectory();
}
element("#rename").onclick = () => busy(async () => {
  const title = prompt("会话标题（最多 80 个字符）", current.title);
  if (title === null) return;
  await client.conversations.rename(currentId, title); await select(currentId);
});
element("#stop").onclick = () => busy(async () => {
  await client.conversations.stop(currentId, { request_id: crypto.randomUUID(), run_id: current.current_run.run_id });
  await select(currentId);
});
element("#delete").onclick = () => busy(async () => {
  if (!confirm("删除这个预览会话及其历史？刷新页面即可恢复合成样例。")) return;
  await client.conversations.delete(currentId);
  const items = await refreshDirectory();
  if (items.length) await select(items[0].conversation_id);
  else { history.replaceChildren(); report.replaceChildren(); responseText.textContent = "";
    note.textContent = "预览会话已全部删除，刷新页面可恢复。"; element("#management").hidden = true; }
});
element("#rediagnose").onclick = () => busy(async () => {
  const text = element("#new-problem").value;
  if (!text.trim()) throw new Error("请先填写新一轮的问题。");
  await client.conversations.send(currentId, { request_id: crypto.randomUUID(), text, attachment_ids: [] });
  await select(currentId);
});
element("#older").onclick = () => busy(async () => {
  const page = await client.conversations.get(currentId, { include: ["history"], history_before: before, history_limit: 3 });
  showHistory(page.history, true); before = page.history_next_cursor; element("#older").disabled = before === null;
});
await busy(async () => {
  const response = await fetch("/sample-data.json");
  if (!response.ok) throw new Error("无法读取预览数据，请重新启动 preview.mjs。");
  client = createAgentClient({ fetchImpl: createPreviewApi(await response.json()) });
  const items = await refreshDirectory(); await select(items[0].conversation_id);
});
