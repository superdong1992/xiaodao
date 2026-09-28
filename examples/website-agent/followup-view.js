import { createMarkdownBlock } from "./report-view.js";

const STATUS = { QUEUED: "等待回答", RUNNING: "正在回答", CANCELLING: "正在停止", COMPLETED: "已回答",
  FAILED: "回答失败", INTERRUPTED: "已中断", CANCELLED: "已停止" };
const REASON = { DISABLED: "报告追问功能尚未启用。", UNSUPPORTED: "这份报告暂不支持追问。",
  EXPIRED: "这份报告已过保留期。", BUSY: "本会话仍有追问正在回答，请等待完成或停止后再发送。",
  LIMIT_EXCEEDED: "这份报告的追问已达上限，请新建对话。", CONTEXT_LIMIT: "追问上下文已达上限，请新建对话。" };

/** 挂载独立问答区；正式报告和赞踩仍由原页面管理。返回清理函数。 */
export function mountFollowupView(container, controller) {
  const doc = container.ownerDocument;
  const node = (tag, text) => { const value = doc.createElement(tag); if (text !== undefined) value.textContent = text; return value; };
  const root = node("section"); root.className = "xiaodao-followup"; root.setAttribute("aria-label", "报告追问");
  const context = node("p"), notice = node("p"), error = node("p"), warning = node("p"), list = node("ol");
  error.setAttribute("role", "alert"); notice.setAttribute("aria-live", "polite");
  const form = node("form"), label = node("label", "继续询问这份报告"), input = node("textarea");
  input.rows = 4; input.setAttribute("aria-label", "追问内容"); label.append(input);
  const submit = node("button", "发送追问"), refresh = node("button", "刷新记录"), older = node("button", "加载更早问答"), stop = node("button", "停止回答");
  submit.type = "submit"; for (const button of [refresh, older, stop]) button.type = "button";
  const run = (work) => { void work().catch((failure) => { error.textContent = failure.message; }); };
  input.oninput = () => controller.setDraft(input.value);
  form.onsubmit = (event) => { event.preventDefault(); run(() => controller.submit()); };
  refresh.onclick = () => run(() => controller.refresh()); older.onclick = () => run(() => controller.loadOlder()); stop.onclick = () => run(() => controller.stop());
  form.append(label, submit); root.append(node("h2", "报告追问"), node("p", "可解释报告、核对疑问或补充文字。回答追加在下方，原报告和评价保持原样。"),
    context, notice, error, warning, older, list, form, refresh, stop);
  container.replaceChildren(root);
  const unsubscribe = controller.subscribe((state) => {
    context.textContent = state.snapshot_status === "READY" ? "可结合报告和原日志回答。" :
      ["PENDING", "BUILDING"].includes(state.snapshot_status) ? "原日志副本正在准备，当前仅依据报告和已有问答，不重新核对日志。" :
        "原日志不可用，当前仅依据报告和已有问答，不重新核对日志。";
    notice.textContent = state.loading ? "正在读取追问记录……" : state.pending ? "上次提交结果尚未确认；再次发送会沿用原请求 ID 和文字。" : REASON[state.reason] ?? "";
    error.textContent = state.error ?? ""; warning.textContent = state.storage_warning ?? "";
    if (input.value !== state.draft) input.value = state.draft;
    input.disabled = state.loading || state.sending || !!state.pending || !state.can_ask;
    submit.textContent = state.sending ? "正在发送……" : state.pending ? "重试原追问" : "发送追问";
    submit.disabled = state.loading || state.sending || (!state.pending && (!state.can_ask || !state.draft.trim()));
    refresh.disabled = state.loading; older.disabled = state.loading_older || state.next_cursor === null;
    stop.hidden = !state.active_followup;
    stop.disabled = state.stopping || state.active_followup?.status === "CANCELLING";
    stop.textContent = state.active_followup?.run_id !== state.run_id ? "停止另一份报告的回答" : "停止回答";
    const entries = doc.createDocumentFragment();
    for (const item of state.items) {
      const row = node("li"); row.dataset.followupId = item.followup_id;
      row.append(node("h3", `第 ${item.ordinal} 次追问 · ${STATUS[item.status]}`), node("p", item.text),
        node("p", item.context_mode === "REPORT_AND_LOGS" ? "回答依据：报告和原日志" : "回答依据：仅报告和已有问答，未重新核对原日志"));
      if (item.answer_markdown !== null) row.append(createMarkdownBlock(doc, item.answer_markdown));
      if (item.failure) row.append(node("p", item.failure.message));
      entries.append(row);
    }
    list.replaceChildren(entries);
  });
  return () => { unsubscribe(); controller.destroy(); };
}
