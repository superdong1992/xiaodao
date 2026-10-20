import { createMarkdownBlock } from "./report-view.js";
import { LOG_FILE_ACCEPT } from "./log-upload-controller.js";

const STATUS = { QUEUED: "等待回答", RUNNING: "正在回答", CANCELLING: "正在停止", COMPLETED: "已回答",
  FAILED: "回答失败", INTERRUPTED: "已中断", CANCELLED: "已停止" };
const REASON = { DISABLED: "报告追问功能尚未启用。", UNSUPPORTED: "这份报告暂不支持追问。",
  EXPIRED: "本轮已结束追问，报告和历史问答仍可查看。", BUSY: "本会话仍有追问正在回答，请等待完成或停止后再发送。",
  LIMIT_EXCEEDED: "这份报告的追问已达上限，请新建对话。", CONTEXT_LIMIT: "追问上下文已达上限，请新建对话。" };

/** 挂载独立问答区；正式报告和赞踩仍由原页面管理。返回清理函数。 */
export function mountFollowupView(container, controller) {
  const doc = container.ownerDocument;
  const node = (tag, text) => { const value = doc.createElement(tag); if (text !== undefined) value.textContent = text; return value; };
  const root = node("section"); root.className = "xiaodao-followup"; root.setAttribute("aria-label", "报告追问");
  const context = node("p"), notice = node("p"), error = node("p"), warning = node("p"), list = node("ol"), questions = node("div");
  error.setAttribute("role", "alert"); notice.setAttribute("aria-live", "polite");
  const form = node("form"), label = node("label", "继续询问这份报告"), input = node("textarea");
  input.rows = 4; input.setAttribute("aria-label", "追问内容"); label.append(input);
  const fileLabel = node("label", "补充日志（可选，可多选）"), files = node("input"), uploads = node("ul");
  files.type = "file"; files.multiple = true; files.accept = LOG_FILE_ACCEPT; fileLabel.append(files);
  const retryUploads = node("button", "重试日志上传"), cancelUploads = node("button", "取消校验和上传");
  retryUploads.type = cancelUploads.type = "button";
  const submit = node("button", "发送追问"), refresh = node("button", "刷新记录"), older = node("button", "加载更早问答"), stop = node("button", "停止回答");
  submit.type = "submit"; for (const button of [refresh, older, stop]) button.type = "button";
  const run = (work) => { void Promise.resolve().then(work).catch((failure) => { error.textContent = failure.message; }); };
  input.oninput = () => controller.setDraft(input.value);
  form.onsubmit = (event) => { event.preventDefault(); run(() => controller.submit()); };
  files.onchange = () => { const selected = [...files.files]; files.value = ""; run(() => controller.uploadFiles(selected)); };
  retryUploads.onclick = () => run(() => controller.retryUploads()); cancelUploads.onclick = () => controller.cancelUploads();
  refresh.onclick = () => run(() => controller.refresh()); older.onclick = () => run(() => controller.loadOlder()); stop.onclick = () => run(() => controller.stop());
  form.append(questions, label, fileLabel, node("p", "可补充多个日志包。系统会结合当前信息回答，必要时继续诊断并生成新版报告。"), uploads, retryUploads, cancelUploads, submit);
  root.append(node("h2", "报告追问"), node("p", "直接提问、更正信息或补充日志。处理结果会记录在这条追问下，原报告保留。"),
    context, notice, error, warning, older, list, form, refresh, stop);
  container.replaceChildren(root);
  const unsubscribe = controller.subscribe((state) => {
    context.textContent = state.snapshot_status === "READY" ? "可结合报告和原日志回答。" :
      ["PENDING", "BUILDING"].includes(state.snapshot_status) ? "原日志副本正在准备；需要进一步核对时，系统会继续诊断。" :
        "本次追问尚无原日志副本；需要时系统会继续诊断。";
    notice.textContent = state.loading ? "正在读取追问记录……" : state.pending ? "上次提交结果尚未确认；再次发送会沿用原请求 ID 和文字。" : REASON[state.reason] ?? "";
    error.textContent = state.error ?? ""; warning.textContent = state.storage_warning ?? "";
    if (input.value !== state.draft) input.value = state.draft;
    const canSubmit = state.can_submit ?? state.can_ask;
    const locked = state.loading || state.sending || !!state.pending || state.uploading || !canSubmit;
    input.disabled = files.disabled = locked;
    submit.textContent = state.sending ? "正在发送……" : state.pending ? "重试原请求" : state.input_run_id ? "发送补充" : "发送追问";
    submit.disabled = state.loading || state.sending || state.uploading || (!state.pending &&
      (!canSubmit || !state.draft.trim() || (state.uploads ?? []).some((item) => item.status !== "READY")));
    questions.replaceChildren();
    if (state.current_questions?.length) {
      questions.append(node("h3", "请补充以下信息，诊断会继续处理"));
      for (const question of state.current_questions) questions.append(node("p", question));
    }
    cancelUploads.hidden = !state.uploading;
    retryUploads.hidden = !(state.uploads ?? []).some((item) => ["FAILED", "CANCELLED"].includes(item.status));
    retryUploads.disabled = locked;
    uploads.replaceChildren();
    for (const upload of state.uploads ?? []) {
      const progress = Math.min(100, Math.floor((upload.checked_bytes ?? 0) / upload.size * 100));
      const row = node("li", `${upload.name} · ${{ READY: "已上传", WAITING: "等待校验", CHECKING: `正在校验 ${progress}%`,
        UPLOADING: "正在上传", FAILED: "上传失败", CANCELLED: "已取消" }[upload.status]}`);
      const remove = node("button", "移除"); remove.type = "button"; remove.disabled = locked;
      remove.onclick = () => controller.removeUpload(upload.local_id); row.append(remove); uploads.append(row);
    }
    refresh.disabled = state.loading; older.disabled = state.loading_older || state.next_cursor === null;
    stop.hidden = !state.active_followup;
    stop.disabled = state.stopping || state.active_followup?.status === "CANCELLING";
    stop.textContent = state.active_followup?.run_id !== state.run_id ? "停止另一份报告的回答" : "停止回答";
    const entries = doc.createDocumentFragment();
    for (const item of state.items) {
      const row = node("li"); row.dataset.followupId = item.followup_id;
      row.append(node("h3", `第 ${item.ordinal} 次追问 · ${item.diagnosis_run_id ? "已继续诊断" : STATUS[item.status]}`), node("p", item.text),
        node("p", item.context_mode === "REPORT_AND_LOGS" ? "回答依据：报告和原日志" : "回答依据：仅报告和已有问答，未重新核对原日志"));
      if (item.answer_markdown !== null) row.append(createMarkdownBlock(doc, item.answer_markdown));
      if (item.failure) row.append(node("p", item.failure.message));
      if (item.attachment_ids?.length) row.append(node("p", `本次补充了 ${item.attachment_ids.length} 个日志包。`));
      if (item.diagnosis_run_id) {
        const diagnosis = state.diagnoses?.[item.diagnosis_run_id], detail = diagnosis?.detail;
        const terminal = ["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"].includes(detail?.status);
        if (detail?.report_state === "READY") row.append(node("p", "新版报告已生成。"));
        else if (terminal) {
          const statusText = { FAILED: "本轮诊断失败。", INTERRUPTED: "本轮诊断已中断。", CANCELLED: "本轮诊断已停止。",
            COMPLETED: "本轮诊断已结束，但未生成新版报告。" }[detail.status];
          const failure = ["FAILED", "INTERRUPTED"].includes(detail.status) && typeof detail.failure?.message === "string"
            ? detail.failure.message.trim() : "";
          row.append(node("p", failure || statusText), node("p", "可在下方补充信息或日志后重新询问，系统会根据材料决定下一步。原报告仍可查看。"));
        } else row.append(node("p", diagnosis?.error ?? detail?.progress?.message ?? "后台正在继续诊断，原报告仍可查看。"));
        const open = node("button", "查看新版报告"); open.type = "button"; open.hidden = detail?.report_state !== "READY";
        open.onclick = () => run(() => controller.openDiagnosis(item.followup_id)); row.append(open);
        if (diagnosis?.error) {
          const refreshDiagnosis = node("button", "刷新诊断进度"); refreshDiagnosis.type = "button";
          refreshDiagnosis.onclick = () => run(() => controller.refreshDiagnosis(item.diagnosis_run_id)); row.append(refreshDiagnosis);
        }
        if (!terminal && detail?.report_state !== "READY" && detail?.capabilities?.can_stop) {
          const stopDiagnosis = node("button", diagnosis.stopping ? "正在停止诊断……" : "停止诊断"); stopDiagnosis.type = "button";
          stopDiagnosis.disabled = diagnosis.stopping;
          stopDiagnosis.onclick = () => run(() => controller.stopDiagnosis(item.diagnosis_run_id)); row.append(stopDiagnosis);
        }
      }
      entries.append(row);
    }
    list.replaceChildren(entries);
  });
  return () => { unsubscribe(); controller.destroy(); };
}
