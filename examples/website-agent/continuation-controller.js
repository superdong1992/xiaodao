import { validFollowupId, validFollowupRequestId, validFollowupText, validateFollowupReceipt } from "./followup-contract.js";
import { validateContinuationInput } from "./continuation-contract.js";
import { sha256File } from "./file-sha256.js";
import { createLogUploadController } from "./log-upload-controller.js";

const storageDefault = () => { try { return globalThis.sessionStorage; } catch { return null; } };
function validatePendingInput(input, action) {
  if (action !== "followup") return validateContinuationInput(input, action);
  if (!input || Object.keys(input).length !== 2 || !validFollowupRequestId(input.request_id) || !validFollowupText(input.text))
    throw new TypeError("报告追问的文字或请求 ID 无效，请重新读取草稿。");
  return input;
}

/** 草稿保存和确认是两个请求；网络失败后只重放保留下来的原请求。 */
export function createContinuationController({ client, conversationId, runId, onConfirmed = () => {},
  onFollowup = () => {},
  storage = storageDefault(), makeRequestId = () => globalThis.crypto.randomUUID(), sha256 = sha256File,
  schedule = setTimeout, cancelSchedule = clearTimeout } = {}) {
  const key = `xiaodao-continuation:${conversationId}:${runId}`, listeners = new Set();
  const state = { draft: null, versions: null, uploads: [], pending: null, busy: false, uploading: false,
    loading: false, error: null, storage_warning: null, followup_receipt: null };
  let saved = { draft_id: null, pending: null }, disposed = false, timer, refreshing;
  try {
    const restored = JSON.parse(storage?.getItem(key) ?? "null");
    if (restored && (restored.draft_id === null || validFollowupId(restored.draft_id))) {
      if (restored.pending !== null) {
        if (!["create", "update", "confirm", "followup"].includes(restored.pending?.action)) throw new Error();
        validatePendingInput(restored.pending.input, restored.pending.action);
        if (restored.pending.action !== "create" && !validFollowupId(restored.draft_id)) throw new Error();
      }
      if (restored.followup) {
        if (!validFollowupId(restored.draft_id)) throw new Error();
        validatePendingInput(restored.followup.input, "followup");
        if (restored.followup.receipt != null)
          validateFollowupReceipt(restored.followup.receipt, conversationId, runId, restored.followup.input.request_id);
        if (restored.pending?.action === "followup" &&
          (restored.pending.input.request_id !== restored.followup.input.request_id || restored.pending.input.text !== restored.followup.input.text)) throw new Error();
      }
      saved = { draft_id: restored.draft_id, pending: restored.pending,
        ...(restored.followup ? { followup: restored.followup } : {}) };
    }
  } catch { /* 损坏的本地数据不参与提交。 */ }
  function emit() {
    if (disposed) return;
    state.pending = saved.pending;
    state.followup_receipt = saved.followup?.receipt ?? null;
    for (const listener of listeners) listener(structuredClone(state));
  }
  function persist() {
    if (disposed) return;
    try { if (storage) storage.setItem(key, JSON.stringify(saved)); }
    catch { state.storage_warning = "浏览器无法保存草稿状态，刷新页面前请保留当前草稿和请求 ID。"; }
  }
  const uploader = createLogUploadController({ client, conversationId, makeRequestId, sha256,
    canChange: () => !disposed && !state.busy && !saved.pending,
    onChange: (upload) => { Object.assign(state, upload); emit(); } });
  const later = () => {
    if (disposed || timer !== undefined || state.draft?.status !== "PREPARING") return;
    timer = schedule(() => { timer = undefined; void refreshDraft().catch(() => {}); }, 1200);
  };
  function acceptDraft(draft) {
    if (draft.parent_run_id !== runId) throw new Error("草稿不属于所选报告，请刷新后重试。");
    if (saved.draft_id !== draft.draft_id) delete saved.followup;
    state.draft = draft; saved.draft_id = draft.draft_id; persist(); later();
  }
  async function refreshDraft() {
    if (disposed || !saved.draft_id) return;
    try {
      const draft = await client.continuations.get(conversationId, saved.draft_id);
      if (disposed) return;
      acceptDraft(draft); emit();
    } catch (error) { if (!disposed) { state.error = error.message; emit(); throw error; } }
  }
  async function refresh() {
    if (disposed || refreshing) return refreshing;
    state.loading = true; state.error = null; emit();
    refreshing = (async () => {
      try {
        const versions = await client.continuations.versions(conversationId, runId);
        if (disposed) return;
        state.versions = versions;
        if (saved.draft_id) await refreshDraft();
      } catch (error) { if (!disposed) { state.error = error.message; throw error; } }
      finally { refreshing = null; state.loading = false; emit(); }
    })();
    return refreshing;
  }
  async function perform(action, input) {
    if (disposed || state.busy || state.uploading) return;
    if (saved.pending && saved.pending.action !== action) throw new Error("上次请求尚未确认，请先重试或刷新草稿。");
    if (action === "followup" && state.draft?.status !== "FOLLOWUP" || action === "confirm" && state.draft?.status === "FOLLOWUP")
      throw new Error("请按草稿提示发送报告追问；报告追问不会创建新诊断。");
    if (action === "followup" && saved.followup?.receipt) {
      const result = structuredClone(saved.followup.receipt);
      await onFollowup(result); return result;
    }
    if (!saved.pending) {
      if (action === "create" && !state.versions?.can_continue) throw new Error("当前报告暂不支持继续诊断，请刷新状态。");
      if (!["create", "followup"].includes(action) && !(action === "update" ? ["DRAFT", "FAILED"] : ["DRAFT"]).includes(state.draft?.status))
        throw new Error("请等待草稿整理完成，再核对并确认。");
      const body = action === "followup" ? saved.followup?.input ?? { request_id: makeRequestId(), text: state.draft.followup_text } :
        { ...input, request_id: makeRequestId(), ...(action === "create" ? {} : { expected_revision: state.draft.revision }) };
      validatePendingInput(body, action);
      if (action === "followup") saved.followup = { input: body, receipt: null };
      saved.pending = { action, input: body }; persist();
    }
    state.busy = true; state.error = null; emit();
    const pending = structuredClone(saved.pending);
    try {
      const result = action === "followup" ? await client.followups.send(conversationId, runId, pending.input) :
        action === "create" ? await client.continuations.create(conversationId, runId, pending.input) :
        await client.continuations[action](conversationId, saved.draft_id, pending.input);
      if (disposed) return;
      if (action === "confirm") {
        if (result.parent_run_id !== runId) throw new Error("继续诊断回执不属于所选报告。");
        state.draft = { ...state.draft, status: "CONFIRMED", confirmed_run_id: result.run_id };
      } else if (action === "followup") {
        validateFollowupReceipt(result, conversationId, runId, pending.input.request_id);
        saved.followup = { input: pending.input, receipt: structuredClone(result) };
      } else acceptDraft(result);
      saved.pending = null; persist(); emit();
      if (action === "confirm") await onConfirmed(result);
      if (action === "followup") await onFollowup(result);
      return result;
    } catch (error) {
      if (!disposed) {
        state.error = error.message;
        // 版本冲突已明确拒绝本次写入，重新读取后让用户核对；网络未知结果保留原请求。
        if (["REVISION_CONFLICT", "AGENT_DRAFT_CHANGED"].includes(error.code)) {
          saved.pending = null; persist(); await refreshDraft().catch(() => {});
        } else if (error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429) {
          saved.pending = null; persist();
        }
        throw error;
      }
    } finally { state.busy = false; emit(); }
  }
  return {
    subscribe(listener) { listeners.add(listener); listener(structuredClone({ ...state, pending: saved.pending,
      followup_receipt: saved.followup?.receipt ?? null })); return () => listeners.delete(listener); },
    start: refresh, refresh,
    create: (input) => perform("create", input), update: (input) => perform("update", input), confirm: () => perform("confirm", {}),
    sendFollowup: () => perform("followup", {}),
    retry: () => saved.pending ? perform(saved.pending.action, saved.pending.input) : uploader.uploadFiles([]),
    uploadFiles: uploader.uploadFiles,
    cancelUploads: uploader.cancel,
    newDraft() {
      if (disposed || state.busy || state.loading || state.uploading || saved.pending ||
        !["CONFIRMED", "FOLLOWUP"].includes(state.draft?.status) || !state.versions?.can_continue) return false;
      saved = { draft_id: null, pending: null }; state.draft = null; state.uploads = []; state.error = null;
      uploader.clear(); persist(); emit(); return true;
    },
    removeUpload: uploader.remove,
    destroy() { disposed = true; uploader.destroy(); if (timer !== undefined) cancelSchedule(timer); listeners.clear(); },
  };
}
