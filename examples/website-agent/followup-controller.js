import { FOLLOWUP_ACTIVE, validFollowupText, validFollowupInput, validFollowupId } from "./followup-contract.js";
import { createLogUploadController } from "./log-upload-controller.js";

function browserStorage() {
  try { return globalThis.sessionStorage; } catch { return null; }
}

/** 一个实例只服务一份报告。切换报告前 destroy；重连只查询状态，不重新提交模型请求。 */
export function createFollowupController({ client, conversationId, runId, storage = browserStorage(),
  storageNamespace = "xiaodao-followup", makeRequestId = () => globalThis.crypto.randomUUID(),
  sha256, onDiagnosisReady = () => {}, onOpenDiagnosis = onDiagnosisReady,
  schedule = (work, delay) => setTimeout(work, delay), cancelSchedule = clearTimeout } = {}) {
  const key = `${storageNamespace}:${conversationId}:${runId}`, listeners = new Set(), items = new Map();
  let saved = { draft: "", pending: null, pending_run_id: null, receipts: [], stops: {}, uploads: [], submitted_ids: [], notified_diagnoses: [] };
  try {
    const parsed = JSON.parse(storage?.getItem(key) ?? "null");
    if (parsed && typeof parsed.draft === "string" && Array.isArray(parsed.receipts) && parsed.stops &&
      (parsed.pending === null || validFollowupInput(parsed.pending)) &&
      (parsed.pending_run_id == null || validFollowupId(parsed.pending_run_id))) saved = { ...saved, ...parsed };
  } catch { /* 损坏的本地草稿不用于生成请求。 */ }
  const state = { conversation_id: conversationId, run_id: runId, items: [], active_followup: null,
    can_ask: false, reason: null, snapshot_status: "UNAVAILABLE", next_cursor: null, last_event_id: 0,
    draft: "", pending: null, receipts: [], loading: false, sending: false, stopping: false,
    loading_older: false, connected: false, error: null, storage_warning: null, uploads: [], uploading: false, diagnoses: {} };
  let destroyed = false, generation = 0, stream, snapshot, retryTimer, authorized = false;
  const diagnosisTasks = new Map(), closed = new Set(["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"]);
  const emit = () => {
    if (destroyed) return;
    state.items = [...items.values()].sort((a, b) => a.ordinal - b.ordinal);
    const waiting = Object.entries(state.diagnoses).find(([, value]) => value.detail?.status === "WAITING_INPUT" && value.detail.current_questions?.length);
    state.input_run_id = waiting?.[0] ?? null;
    state.current_questions = waiting?.[1].detail.current_questions ?? [];
    state.can_submit = state.can_ask || !!state.input_run_id;
    for (const listener of listeners) listener(structuredClone(state));
  };
  const persist = () => {
    // 同一报告可能已挂载新实例；旧实例的迟到回执不能覆盖新草稿或请求 ID。
    if (destroyed) return;
    try {
      if (!storage) throw new Error("storage unavailable");
      storage.setItem(key, JSON.stringify(saved));
    }
    catch { state.storage_warning = "浏览器暂时无法保存草稿，刷新前请保留未确认请求的 ID 和文字。"; }
    state.draft = saved.draft; state.pending = saved.pending; state.receipts = saved.receipts;
  };
  const stopStream = () => {
    stream?.abort(); stream = null; state.connected = false;
    if (retryTimer !== undefined) cancelSchedule(retryTimer);
    retryTimer = undefined;
  };
  const uploader = createLogUploadController({ client, conversationId, makeRequestId, sha256,
    initialUploads: Array.isArray(saved.uploads) ? saved.uploads.filter((item) => item.status === "READY" &&
      validFollowupId(item.attachment_id) && typeof item.name === "string" && Number.isSafeInteger(item.size) && item.size > 0) : [],
    canChange: () => !destroyed && authorized && !state.sending && !saved.pending,
    onChange: (upload) => {
      if (destroyed) return;
      state.uploads = upload.uploads; state.uploading = upload.uploading;
      if (upload.error) state.error = upload.error;
      saved.uploads = upload.uploads.filter((item) => item.status === "READY"); persist(); emit();
    } });
  function diagnosisIdentity(item) {
    return { conversation_id: conversationId, run_id: item.diagnosis_run_id, parent_run_id: runId,
      followup_id: item.followup_id, text: item.text };
  }
  async function refreshDiagnosis(item) {
    const id = item.diagnosis_run_id, task = diagnosisTasks.get(id);
    if (destroyed || !task || task.loading) return;
    if (task.timer !== undefined) cancelSchedule(task.timer);
    task.timer = undefined;
    task.loading = true;
    try {
      const detail = await client.conversations.get(conversationId, { run_id: id, include: [], signal: task.abort.signal });
      if (destroyed || task.abort.signal.aborted) return;
      if (detail.conversation_id !== conversationId || detail.selected_run_id !== id)
        throw new Error("诊断进度与关联轮次不一致，请刷新后重试。");
      state.diagnoses[id] = { detail, stopping: task.stopping, error: null }; emit();
      if (detail.report_state === "READY") {
        const submitted = saved.submitted_ids.includes(item.request_id) || saved.receipts.some((entry) => entry.request_id === item.request_id);
        if (submitted && !saved.notified_diagnoses.includes(id)) {
          saved.notified_diagnoses.push(id); persist();
          await onDiagnosisReady(diagnosisIdentity(item));
        }
      } else if (!closed.has(detail.status)) {
        task.timer = schedule(() => { task.timer = undefined; void refreshDiagnosis(item); }, 1500);
      }
    } catch (error) {
      if (!destroyed && !task.abort.signal.aborted) {
        state.diagnoses[id] = { ...state.diagnoses[id], error: error.message, stopping: false }; emit();
      }
    } finally { task.loading = false; }
  }
  function trackDiagnoses() {
    for (const item of items.values()) {
      if (!item.diagnosis_run_id || diagnosisTasks.has(item.diagnosis_run_id)) continue;
      diagnosisTasks.set(item.diagnosis_run_id, { abort: new AbortController(), loading: false, stopping: false });
      void refreshDiagnosis(item);
    }
  }
  const reconcile = () => {
    if (saved.pending && !saved.pending_run_id && [...items.values()].some((item) => item.request_id === saved.pending.request_id)) {
      if (!saved.submitted_ids.includes(saved.pending.request_id)) saved.submitted_ids.push(saved.pending.request_id);
      if (saved.draft === saved.pending.text) saved.draft = "";
      saved.pending = null; uploader.clear();
    }
    persist();
  };
  const later = (delay = 1500) => {
    if (destroyed || retryTimer !== undefined) return;
    retryTimer = schedule(() => { retryTimer = undefined; void refresh().catch(() => {}); }, delay);
  };
  function subscribeEvents(version) {
    const controller = new AbortController(); stream = controller; state.connected = true; emit();
    void client.followups.events(conversationId, runId, { after: state.last_event_id, signal: controller.signal,
      onEvent: async (event) => {
        if (destroyed || version !== generation || controller.signal.aborted || event.sequence <= state.last_event_id) return;
        items.set(event.followup_id, structuredClone(event.data));
        if (FOLLOWUP_ACTIVE.has(event.data.status)) {
          state.active_followup = event.data; state.can_ask = false; state.reason = "BUSY";
        } else if (state.active_followup?.followup_id === event.followup_id) state.active_followup = null;
        reconcile();
        emit(); state.last_event_id = event.sequence;
        trackDiagnoses();
        // 完成时重新读取资格与会话级 busy，不用单条事件猜测服务端限制。
        if (!FOLLOWUP_ACTIVE.has(event.data.status)) later(0);
      },
    }).then(() => {
      if (destroyed || version !== generation || controller.signal.aborted) return;
      state.connected = false; emit();
      if (state.active_followup) later();
    }).catch((error) => {
      if (destroyed || version !== generation || controller.signal.aborted) return;
      state.connected = false; state.error = error.message; emit();
      if (error.status === 409 && error.code === "AGENT_FOLLOWUP_INVALID_CURSOR") later(0);
      else if (!error.status || error.status >= 500) later();
    });
  }
  async function refresh() {
    if (destroyed) return;
    const version = ++generation; stopStream(); snapshot?.abort(); snapshot = new AbortController();
    state.loading = true; state.error = null; emit();
    try {
      const view = await client.followups.list(conversationId, runId, { signal: snapshot.signal });
      if (destroyed || version !== generation) return;
      authorized = true;
      // 从最新页重建，避免久未连接时留下已被新页挤出的旧 RUNNING 项。
      items.clear();
      for (const item of view.items) items.set(item.followup_id, structuredClone(item));
      Object.assign(state, { active_followup: view.active_followup, can_ask: view.can_ask, reason: view.reason,
        snapshot_status: view.snapshot_status, next_cursor: view.next_cursor, last_event_id: view.last_event_id });
      reconcile(); state.loading = false; emit();
      subscribeEvents(version);
      state.uploads = uploader.getState().uploads; emit(); trackDiagnoses();
    } catch (error) {
      if (destroyed || version !== generation) return;
      state.loading = false; state.can_ask = false; state.error = error.message;
      if ([401, 403, 404].includes(error.status)) {
        authorized = false; items.clear(); state.active_followup = null;
        state.draft = ""; state.pending = null; state.receipts = [];
      }
      emit(); throw error;
    }
  }
  async function loadOlder() {
    if (destroyed || state.loading_older || state.next_cursor === null) return;
    const version = generation, cursor = state.next_cursor;
    state.loading_older = true; emit();
    try {
      const page = await client.followups.list(conversationId, runId, { cursor });
      if (destroyed || version !== generation) return;
      // 分页快照可能晚于实时更新返回；旧页只能补齐没有加载过的项。
      for (const item of page.items) if (!items.has(item.followup_id)) items.set(item.followup_id, structuredClone(item));
      state.next_cursor = page.next_cursor; reconcile(); trackDiagnoses();
    } catch (error) { if (!destroyed && version === generation) { state.error = error.message; throw error; } }
    finally { state.loading_older = false; emit(); }
  }
  async function submit() {
    if (destroyed || state.sending || state.uploading) return;
    if (!authorized || !saved.pending && !state.can_submit) throw new Error("当前报告暂时不能继续追问，请刷新状态。");
    if (!saved.pending) {
      if (!validFollowupText(saved.draft)) throw new Error("请填写非空追问，最多 65536 UTF-8 字节。");
      if (state.uploads.some((item) => item.status !== "READY")) throw new Error("请先完成日志上传，或移除未完成的日志。");
      const attachment_ids = uploader.attachmentIds();
      saved.pending = { request_id: makeRequestId(), text: saved.draft, ...(attachment_ids.length ? { attachment_ids } : {}) };
      saved.pending_run_id = state.input_run_id;
      saved.submitted_ids = [...new Set([...saved.submitted_ids, saved.pending.request_id])].slice(-100);
    }
    const input = structuredClone(saved.pending);
    const targetRun = saved.pending_run_id;
    state.sending = true; state.error = null; persist(); emit();
    try {
      const receipt = targetRun ? await client.conversations.send(conversationId, {
        ...input, attachment_ids: input.attachment_ids ?? [], target_run_id: targetRun,
      }) : await client.followups.send(conversationId, runId, input);
      // 已销毁实例不再写本地状态；新实例会用 GET 对上原请求的服务端记录。
      if (!saved.receipts.some((item) => item.request_id === receipt.request_id)) saved.receipts.push(receipt);
      saved.receipts = saved.receipts.slice(-100);
      if (saved.draft === input.text) saved.draft = "";
      saved.pending = null; saved.pending_run_id = null; uploader.clear(); persist();
      if (!destroyed) {
        if (targetRun) {
          const item = [...items.values()].find((entry) => entry.diagnosis_run_id === targetRun);
          if (item) await refreshDiagnosis(item);
        } else await refresh();
      }
      return receipt;
    } catch (error) {
      // 明确的客户端拒绝没有接收任务；网络或服务器不确定结果保留原请求供重试。
      if (error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429) {
        saved.pending = null; saved.pending_run_id = null; persist();
      }
      if (!destroyed && targetRun && error.status === 409 && error.code === "AGENT_RUN_CHANGED") {
        const item = [...items.values()].find((entry) => entry.diagnosis_run_id === targetRun);
        if (item) await refreshDiagnosis(item);
      }
      if (!destroyed) state.error = error.message;
      throw error;
    } finally { state.sending = false; emit(); }
  }
  async function stop() {
    const active = state.active_followup;
    if (destroyed || !active || state.stopping) return;
    const requestId = saved.stops[active.followup_id] ??= makeRequestId();
    state.stopping = true; state.error = null; persist(); emit();
    try {
      const receipt = await client.followups.stop(conversationId, active.run_id, active.followup_id, { request_id: requestId });
      if (!destroyed) await refresh();
      return receipt;
    } catch (error) { if (!destroyed) state.error = error.message; throw error; }
    finally { state.stopping = false; emit(); }
  }
  async function stopDiagnosis(id) {
    const task = diagnosisTasks.get(id), item = [...items.values()].find((entry) => entry.diagnosis_run_id === id);
    if (destroyed || !task || !item || task.stopping) return;
    const requestId = saved.stops[`diagnosis:${id}`] ??= makeRequestId();
    task.stopping = true;
    state.diagnoses[id] = { ...state.diagnoses[id], stopping: true }; persist(); emit();
    try {
      await client.conversations.stop(conversationId, { request_id: requestId, run_id: id });
      if (task.timer !== undefined) cancelSchedule(task.timer);
      task.timer = undefined; await refreshDiagnosis(item);
    } catch (error) { if (!destroyed) { state.error = error.message; emit(); } throw error; }
    finally { task.stopping = false; if (!destroyed) { state.diagnoses[id] = { ...state.diagnoses[id], stopping: false }; emit(); } }
  }
  return { start: refresh, refresh, loadOlder, submit, retryPending: submit, stop,
    uploadFiles: uploader.uploadFiles, retryUploads: () => uploader.uploadFiles([]), cancelUploads: uploader.cancel, removeUpload: uploader.remove,
    stopDiagnosis,
    refreshDiagnosis(id) { const item = [...items.values()].find((entry) => entry.diagnosis_run_id === id); if (item) return refreshDiagnosis(item); },
    openDiagnosis(followupId) { const item = items.get(followupId); if (!destroyed && item?.diagnosis_run_id) return onOpenDiagnosis(diagnosisIdentity(item)); },
    setDraft(text) { if (!destroyed && authorized && !saved.pending) { saved.draft = String(text); persist(); emit(); } },
    getState: () => structuredClone(state),
    subscribe(listener) { listeners.add(listener); listener(structuredClone(state)); return () => listeners.delete(listener); },
    destroy() {
      destroyed = true; generation++; stopStream(); snapshot?.abort(); uploader.destroy();
      for (const task of diagnosisTasks.values()) { task.abort.abort(); if (task.timer !== undefined) cancelSchedule(task.timer); }
      listeners.clear();
    },
  };
}
