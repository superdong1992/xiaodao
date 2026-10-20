import { renderReport } from "./report-view.js";
import { LOG_FILE_ACCEPT } from "./log-upload-controller.js";

const node = (doc, tag, text) => { const value = doc.createElement(tag); if (text !== undefined) value.textContent = text; return value; };
const unique = (values) => [...new Set(values)];
const size = (bytes) => Number.isFinite(bytes) ? `${(bytes / 1024 / 1024).toFixed(1)} MiB` : "";

/** 统一追问页面只挂载版本浏览，不提供另一套输入、草稿或确认入口。 */
export function mountReportVersionsView(container, { client, conversationId, runId, onSelectRun = () => {} }) {
  const doc = container.ownerDocument, make = (tag, text) => node(doc, tag, text);
  const root = make("section"), error = make("p"), comparison = make("div"), delta = make("div");
  root.className = "xiaodao-continuation"; root.setAttribute("aria-label", "报告版本");
  error.setAttribute("role", "alert"); comparison.className = "xiaodao-continuation__comparison";
  const version = make("select"), left = make("select"), right = make("select");
  const open = make("button", "查看所选版本"), compare = make("button", "并排查看"), refresh = make("button", "刷新版本");
  for (const button of [open, compare, refresh]) button.type = "button";
  root.append(make("h2", "报告版本"), error);
  for (const [title, select] of [["查看版本", version], ["左侧报告", left], ["右侧报告", right]]) {
    const label = make("label", title); label.append(select); root.append(label);
  }
  root.append(open, compare, refresh, make("p", "新报告开头的“本轮变化”说明判断的变化。输入变化对照右侧报告的直接上一版。"), delta, comparison);
  container.replaceChildren(root);
  let disposed = false, versions, sequence = 0;
  const abort = new AbortController();
  const run = (work) => { void Promise.resolve().then(work).catch((failure) => { if (!disposed) error.textContent = failure.message; }); };
  async function load() {
    open.disabled = compare.disabled = refresh.disabled = true;
    try {
      const value = await client.continuations.versions(conversationId, runId, { signal: abort.signal });
      if (disposed) return;
      versions = value; error.textContent = "";
      for (const select of [version, left, right]) {
        select.replaceChildren();
        for (const entry of value.versions) {
          const option = make("option", `第 ${entry.version} 版${entry.run_id === runId ? "（当前查看）" : ""}`);
          option.value = entry.run_id; select.append(option);
        }
      }
      version.value = right.value = runId;
      left.value = value.versions.find((entry) => entry.run_id === runId)?.parent_run_id ?? value.root_run_id;
    } finally { if (!disposed) { open.disabled = !versions; compare.disabled = !versions || versions.versions.length < 2; refresh.disabled = false; } }
  }
  refresh.onclick = () => run(load);
  open.onclick = () => run(() => onSelectRun(version.value));
  compare.onclick = () => run(async () => {
    const current = ++sequence, ids = [left.value, right.value];
    if (ids[0] === ids[1]) throw new Error("请选择两个不同的报告版本。");
    compare.disabled = true;
    try {
      const views = await Promise.all(ids.map((id) => client.conversations.get(conversationId, { run_id: id, include: ["report"], signal: abort.signal })));
      if (disposed || current !== sequence) return;
      if (views.some((value, index) => value.conversation_id !== conversationId || value.selected_run_id !== ids[index]))
        throw new Error("报告归属与所选版本不一致，请刷新后重试。");
      const fragment = doc.createDocumentFragment();
      views.forEach((value, index) => {
        const column = make("section"), target = make("div"), entry = versions.versions.find((entry) => entry.run_id === ids[index]);
        column.append(make("h3", `第 ${entry.version} 版`), target);
        renderReport(target, value.result, { resourcesExpired: value.resources_expired }); fragment.append(column);
      });
      comparison.replaceChildren(fragment);
      const selected = versions.versions.find((entry) => entry.run_id === ids[1]);
      renderInputChanges(delta, selected.input_changes, selected.runtime_changes ?? []);
    } finally { if (!disposed && current === sequence) compare.disabled = false; }
  });
  run(load);
  return () => { disposed = true; sequence++; abort.abort(); };
}

/** 输入差异来自服务端记录，语义变化以新报告的“本轮变化”为准。 */
export function renderInputChanges(container, changes, runtimeChanges = []) {
  const doc = container.ownerDocument, fragment = doc.createDocumentFragment();
  if (changes) {
    fragment.append(node(doc, "h4", "本轮输入变化"));
    if (changes.before_problem_text !== changes.after_problem_text) {
      for (const [title, text] of [["原问题", changes.before_problem_text], ["更新后的问题", changes.after_problem_text]]) {
        fragment.append(node(doc, "h5", title), node(doc, "pre", text));
      }
    } else fragment.append(node(doc, "p", "问题描述未变更。"));
    for (const [title, values] of [["新增日志", changes.added_attachments], ["移除日志", changes.removed_attachments]]) {
      const list = node(doc, "ul");
      for (const item of values) list.append(node(doc, "li", `${item.name}${size(item.size) ? ` · ${size(item.size)}` : ""}`));
      fragment.append(node(doc, "h5", title), values.length ? list : node(doc, "p", "无"));
    }
  }
  if (runtimeChanges.length) {
    fragment.append(node(doc, "h4", "诊断环境变化"));
    const list = node(doc, "ul"); for (const text of runtimeChanges) list.append(node(doc, "li", text)); fragment.append(list);
  }
  container.replaceChildren(fragment);
}

export function mountContinuationView(container, controller, { client, conversationId, runId, onSelectRun = () => {} } = {}) {
  const doc = container.ownerDocument, make = (tag, text) => node(doc, tag, text);
  const root = make("section"); root.className = "xiaodao-continuation"; root.setAttribute("aria-label", "继续诊断与报告版本");
  root.append(make("h2", "继续诊断"), make("p", "补充日志、更正问题或指定下一步目标，核对后生成新一版报告。旧报告和追问记录会保留。"));
  const notice = make("p"), error = make("p"), storageNotice = make("p"); error.setAttribute("role", "alert"); notice.setAttribute("role", "status");
  const refresh = make("button", "刷新状态"), retry = make("button", "重试上次请求"); refresh.type = retry.type = "button";
  const newDraft = make("button", "重新准备继续诊断"); newDraft.type = "button";
  const cancelUpload = make("button", "取消校验和上传"); cancelUpload.type = "button";
  root.append(notice, error, storageNotice, refresh, retry, newDraft, cancelUpload);
  const initial = make("form"), editor = make("form"), inputDelta = make("div"), uploaded = make("ul");
  function field(form, title, tag = "textarea") {
    const label = make("label", title), input = make(tag); label.append(input); form.append(label); return input;
  }
  const correction = field(initial, "补充或更正说明（可选）"); correction.rows = 3;
  correction.placeholder = "例如：超时发生在 14:30；先前填写的 14:00 有误。";
  const goal = field(initial, "本轮诊断目标（可选）"); goal.rows = 2;
  goal.placeholder = "例如：结合新增服务端日志，验证是否为连接池耗尽。";
  const changeNote = field(initial, "变更说明（可选）"); changeNote.rows = 2;
  const logMode = field(initial, "日志处理方式", "select");
  for (const [value, title] of [["APPEND", "追加：沿用原日志，加入新日志"], ["REPLACE", "替换：本轮使用新选择的日志"]]) {
    const option = make("option", title); option.value = value; logMode.append(option);
  }
  logMode.value = "APPEND";
  const files = field(initial, "选择新日志文件或归档（可多选）", "input"); files.type = "file"; files.multiple = true; files.accept = LOG_FILE_ACCEPT;
  const uploadHint = make("p", "支持 zip、tar、tar.gz、tgz、gz。最多 20 个包，单包不超过 2.5 GiB，总计不超过 5 GiB。");
  const prepare = make("button", "整理并预览"); prepare.type = "submit";
  initial.append(uploadHint, prepare);
  const problem = field(editor, "核对更新后的完整问题"); problem.rows = 6;
  const editGoal = field(editor, "本轮诊断目标（可选）"); editGoal.rows = 2;
  const editNote = field(editor, "变更说明（可选）"); editNote.rows = 2;
  const finalLogMode = field(editor, "日志处理方式", "select");
  for (const [value, title] of [["APPEND", "追加日志"], ["REPLACE", "替换日志"]]) { const option = make("option", title); option.value = value; finalLogMode.append(option); }
  const finalList = make("div"), editFiles = field(editor, "再补充日志包", "input"); editFiles.type = "file"; editFiles.multiple = true; editFiles.accept = files.accept;
  editor.append(make("h3", "本轮最终使用的日志"), make("p", "取消勾选的日志不会用于本轮诊断。修改后请先保存预览。"), finalList);
  const save = make("button", "保存并更新预览"), confirm = make("button", "确认并继续诊断"); save.type = "submit"; confirm.type = "button";
  const dirtyNotice = make("p"); editor.append(save, dirtyNotice, inputDelta, confirm);
  const followupArea = make("section"), followupText = make("pre"), sendFollowup = make("button", "发送报告追问");
  sendFollowup.type = "button";
  followupArea.append(make("h3", "这是对当前报告的追问，无需新建诊断"), followupText, sendFollowup);
  const versionsArea = make("section"); versionsArea.className = "xiaodao-continuation__versions";
  versionsArea.append(make("h3", "报告版本"));
  const versionSelect = field(versionsArea, "查看版本", "select"), viewVersion = make("button", "查看所选版本"); viewVersion.type = "button";
  versionsArea.append(viewVersion);
  const beforeSelect = field(versionsArea, "左侧报告", "select"), afterSelect = field(versionsArea, "右侧报告", "select");
  const compare = make("button", "并排查看"), comparison = make("div"), versionDelta = make("div"); compare.type = "button";
  comparison.className = "xiaodao-continuation__comparison";
  versionsArea.append(compare, make("p", "新报告开头的“本轮变化”说明结论、证据和建议的变化；下方输入变化记录本轮实际提交的内容。"), versionDelta, comparison);
  root.append(initial, uploaded, editor, followupArea, versionsArea); container.replaceChildren(root);
  let disposed = false, current, draftKey = null, versionsKey = null, dirty = false, compared = 0;
  let checkboxes = [], uploadedIds = new Set();
  const run = async (work) => { try { await work(); } catch (value) { if (!disposed) error.textContent = value.message; } };
  function markDirty() { dirty = true; updateButtons(); }
  function updateButtons() {
    if (!current) return;
    const locked = current.busy || current.loading || current.uploading || !!current.pending;
    const incompleteUploads = current.uploads.some((item) => item.status !== "READY");
    for (const input of [correction, goal, changeNote, logMode, files]) input.disabled = locked || !current.versions?.can_continue;
    for (const input of [problem, editGoal, editNote, finalLogMode, editFiles, ...checkboxes]) input.disabled = locked || !["DRAFT", "FAILED"].includes(current.draft?.status);
    prepare.disabled = locked || !current.versions?.can_continue || incompleteUploads;
    save.disabled = locked || incompleteUploads || !dirty || !["DRAFT", "FAILED"].includes(current.draft?.status);
    confirm.disabled = locked || incompleteUploads || dirty || current.draft?.status !== "DRAFT" || !current.versions?.can_continue;
    refresh.disabled = current.busy || current.loading || current.uploading;
    retry.hidden = !current.pending && !current.uploads.some((item) => ["FAILED", "CANCELLED"].includes(item.status)); retry.disabled = current.busy || current.uploading;
    cancelUpload.hidden = !current.uploading;
    newDraft.hidden = !["CONFIRMED", "FOLLOWUP"].includes(current.draft?.status) || !current.versions?.can_continue;
    newDraft.disabled = locked;
    sendFollowup.disabled = locked || current.draft?.status !== "FOLLOWUP";
    sendFollowup.textContent = current.followup_receipt ? "查看报告追问" : "发送报告追问";
    dirtyNotice.textContent = dirty ? "有尚未保存的修改，请先更新预览。" : "请核对问题和最终日志清单，确认后开始新一轮诊断。";
  }
  const unsubscribe = controller.subscribe((state) => {
    current = state;
    error.textContent = state.error ?? ""; storageNotice.textContent = state.storage_warning ?? "";
    const status = state.draft?.status;
    notice.textContent = state.uploading ? "正在校验并上传日志，请稍候。" : state.busy ? "正在提交，请稍候。" :
      state.loading ? "正在读取报告版本和草稿。" : status === "PREPARING" ? "正在整理补充信息，完成后请核对草稿。" :
      status === "FAILED" ? state.draft.failure_message || "整理未完成，可直接编辑完整问题后保存。" :
      status === "FOLLOWUP" ? state.followup_receipt ? "追问已发送，点击“查看报告追问”查看回答。" : "核对下方问题后，发送到当前报告的追问记录。" :
      status === "CONFIRMED" ? state.versions?.can_continue ? "上一轮未产生新版报告，可以从这份报告重新准备继续诊断。" :
        "已创建新一轮诊断，可在历史记录中查看进度。" :
      state.versions?.can_continue === false ? "当前版本暂不能继续诊断，仍可查看报告版本。" : "";
    initial.hidden = !!state.draft; editor.hidden = !["DRAFT", "FAILED"].includes(status);
    followupArea.hidden = status !== "FOLLOWUP";
    followupText.textContent = status === "FOLLOWUP" ? state.draft.followup_text : "";
    uploaded.replaceChildren();
    for (const item of state.uploads) {
      const progress = Math.min(100, Math.floor((item.checked_bytes ?? 0) / item.size * 100));
      const row = make("li", `${item.name} · ${size(item.size)} · ${{ READY: "已上传", FAILED: "校验或上传失败，可重试", CANCELLED: "已取消，可重试",
        WAITING: "等待校验", CHECKING: `正在校验 ${progress}%`, UPLOADING: "正在上传" }[item.status]}`);
      const remove = make("button", "移出本次选择"); remove.type = "button"; remove.disabled = state.busy || state.uploading || !!state.pending;
      remove.addEventListener("click", () => controller.removeUpload(item.local_id));
      if (!state.draft || item.status !== "READY") row.append(remove);
      uploaded.append(row);
    }
    const newDraftKey = state.draft ? `${state.draft.draft_id}:${state.draft.revision}:${status}` : null;
    if (newDraftKey !== draftKey && state.draft) {
      draftKey = newDraftKey; const draft = state.draft;
      problem.value = draft.effective_problem_text; editGoal.value = draft.diagnosis_goal; editNote.value = draft.change_note; finalLogMode.value = draft.log_mode;
      finalList.replaceChildren(); checkboxes = []; uploadedIds = new Set([...uploadedIds, ...draft.attachment_ids]);
      for (const item of draft.attachments) addLog(item);
      for (const item of draft.input_changes.removed_attachments) if (!draft.attachment_ids.includes(item.attachment_id)) addLog(item, false);
      for (const item of state.uploads) if (item.status === "READY" && uploadedIds.has(item.attachment_id) && !checkboxes.some((input) => input.value === item.attachment_id))
        addLog(item, false);
      dirty = status === "FAILED";
      renderInputChanges(inputDelta, draft.input_changes);
    }
    if (state.draft) for (const item of state.uploads) {
      if (item.status === "READY" && !uploadedIds.has(item.attachment_id)) {
        addLog(item); uploadedIds.add(item.attachment_id); dirty = true;
      }
    }
    const newVersionsKey = JSON.stringify(state.versions?.versions.map((item) => [item.run_id, item.version]));
    if (state.versions && newVersionsKey !== versionsKey) {
      versionsKey = newVersionsKey;
      for (const select of [versionSelect, beforeSelect, afterSelect]) {
        select.replaceChildren();
        for (const item of state.versions.versions) {
          const option = make("option", `第 ${item.version} 版${item.run_id === runId ? "（当前查看）" : ""}`); option.value = item.run_id; select.append(option);
        }
      }
      const selected = state.versions.versions.find((item) => item.run_id === runId);
      versionSelect.value = afterSelect.value = runId;
      beforeSelect.value = selected?.parent_run_id ?? state.versions.root_run_id;
      compare.disabled = state.versions.versions.length < 2;
    }
    updateButtons();
  });
  function addLog(item, checked = true) {
    const label = make("label"), checkbox = make("input"); checkbox.type = "checkbox"; checkbox.value = item.attachment_id; checkbox.checked = checked;
    checkbox.addEventListener("change", markDirty); checkboxes.push(checkbox);
    label.append(checkbox, make("span", `${item.name}${size(item.size) ? ` · ${size(item.size)}` : ""}`)); finalList.append(label);
  }
  for (const input of [problem, editGoal, editNote, finalLogMode]) input.addEventListener("input", markDirty);
  initial.addEventListener("submit", (event) => { event.preventDefault(); void run(() => controller.create({
    text: correction.value, diagnosis_goal: goal.value, change_note: changeNote.value, log_mode: logMode.value,
    attachment_ids: unique(current.uploads.filter((item) => item.status === "READY").map((item) => item.attachment_id)),
  })); });
  editor.addEventListener("submit", (event) => { event.preventDefault(); void run(() => controller.update({
    effective_problem_text: problem.value, diagnosis_goal: editGoal.value, change_note: editNote.value,
    log_mode: finalLogMode.value, attachment_ids: unique(checkboxes.filter((item) => item.checked).map((item) => item.value)),
  })); });
  for (const input of [files, editFiles]) input.addEventListener("change", () => {
    const selected = [...input.files]; input.value = ""; void run(() => controller.uploadFiles(selected));
  });
  confirm.addEventListener("click", () => { if (!confirm.disabled) void run(() => controller.confirm()); });
  sendFollowup.addEventListener("click", () => { if (!sendFollowup.disabled) void run(() => controller.sendFollowup()); });
  refresh.addEventListener("click", () => { void run(() => controller.refresh()); });
  retry.addEventListener("click", () => { void run(() => controller.retry()); });
  cancelUpload.addEventListener("click", () => controller.cancelUploads());
  newDraft.addEventListener("click", () => { if (!newDraft.disabled) controller.newDraft(); });
  viewVersion.addEventListener("click", () => { void run(() => onSelectRun(versionSelect.value)); });
  compare.addEventListener("click", () => { void run(async () => {
    const sequence = ++compared, left = beforeSelect.value, right = afterSelect.value;
    if (left === right) throw new Error("请选择两个不同的报告版本。");
    compare.disabled = true;
    try {
      const views = await Promise.all([left, right].map((id) => client.conversations.get(conversationId, { run_id: id, include: ["report"] })));
      if (disposed || sequence !== compared) return;
      if (views.some((view, index) => view.conversation_id !== conversationId || view.selected_run_id !== [left, right][index]))
        throw new Error("报告归属与所选版本不一致，请刷新后重试。");
      const content = doc.createDocumentFragment();
      for (const [index, view] of views.entries()) {
        const column = make("section"), id = [left, right][index], version = current.versions.versions.find((item) => item.run_id === id);
        const target = make("div"); column.append(make("h4", `第 ${version.version} 版`), target);
        renderReport(target, view.result, { resourcesExpired: view.resources_expired }); content.append(column);
      }
      comparison.replaceChildren(content);
      const selected = current.versions.versions.find((item) => item.run_id === right);
      renderInputChanges(versionDelta, selected.input_changes, selected.runtime_changes ?? []);
      if (selected.parent_run_id !== left) versionDelta.prepend(make("p", "输入变化对照的是右侧版本的直接上一版。"));
    } finally { if (!disposed && sequence === compared) compare.disabled = false; }
  }); });
  return () => { disposed = true; compared++; unsubscribe(); controller.destroy(); };
}
