import { FOLLOWUP_ACTIVE, validFollowupText } from "./followup-contract.js";

function browserStorage() {
  try { return globalThis.sessionStorage; } catch { return null; }
}

/** 一个实例只服务一份报告。切换报告前 destroy；重连只查询状态，不重新提交模型请求。 */
export function createFollowupController({ client, conversationId, runId, storage = browserStorage(),
  storageNamespace = "xiaodao-followup", makeRequestId = () => globalThis.crypto.randomUUID(),
  schedule = (work, delay) => setTimeout(work, delay), cancelSchedule = clearTimeout } = {}) {
  const key = `${storageNamespace}:${conversationId}:${runId}`, listeners = new Set(), items = new Map();
  let saved = { draft: "", pending: null, receipts: [], stops: {} };
  try {
    const parsed = JSON.parse(storage?.getItem(key) ?? "null");
    if (parsed && typeof parsed.draft === "string" && Array.isArray(parsed.receipts) && parsed.stops &&
      (parsed.pending === null || typeof parsed.pending?.request_id === "string" && validFollowupText(parsed.pending.text))) saved = parsed;
  } catch { /* 损坏的本地草稿不用于生成请求。 */ }
  const state = { conversation_id: conversationId, run_id: runId, items: [], active_followup: null,
    can_ask: false, reason: null, snapshot_status: "UNAVAILABLE", next_cursor: null, last_event_id: 0,
    draft: "", pending: null, receipts: [], loading: false, sending: false, stopping: false,
    loading_older: false, connected: false, error: null, storage_warning: null };
  let destroyed = false, generation = 0, stream, snapshot, retryTimer, authorized = false;
  const emit = () => {
    if (destroyed) return;
    state.items = [...items.values()].sort((a, b) => a.ordinal - b.ordinal);
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
  const reconcile = () => {
    if (saved.pending && [...items.values()].some((item) => item.request_id === saved.pending.request_id)) {
      if (saved.draft === saved.pending.text) saved.draft = "";
      saved.pending = null;
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
      state.next_cursor = page.next_cursor; reconcile();
    } catch (error) { if (!destroyed && version === generation) { state.error = error.message; throw error; } }
    finally { state.loading_older = false; emit(); }
  }
  async function submit() {
    if (destroyed || state.sending) return;
    if (!authorized || !saved.pending && !state.can_ask) throw new Error("当前报告暂时不能继续追问，请刷新状态。");
    if (!saved.pending) {
      if (!validFollowupText(saved.draft)) throw new Error("请填写非空追问，最多 65536 UTF-8 字节。");
      saved.pending = { request_id: makeRequestId(), text: saved.draft };
    }
    const input = structuredClone(saved.pending);
    state.sending = true; state.error = null; persist(); emit();
    try {
      const receipt = await client.followups.send(conversationId, runId, input);
      // 已销毁实例不再写本地状态；新实例会用 GET 对上原请求的服务端记录。
      if (!saved.receipts.some((item) => item.request_id === receipt.request_id)) saved.receipts.push(receipt);
      saved.receipts = saved.receipts.slice(-100);
      if (saved.draft === input.text) saved.draft = "";
      saved.pending = null; persist();
      if (!destroyed) await refresh();
      return receipt;
    } catch (error) {
      // 明确的客户端拒绝没有接收任务；网络或服务器不确定结果保留原请求供重试。
      if (error.status >= 400 && error.status < 500) { saved.pending = null; persist(); }
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
  return { start: refresh, refresh, loadOlder, submit, retryPending: submit, stop,
    setDraft(text) { if (!destroyed && authorized && !saved.pending) { saved.draft = String(text); persist(); emit(); } },
    getState: () => structuredClone(state),
    subscribe(listener) { listeners.add(listener); listener(structuredClone(state)); return () => listeners.delete(listener); },
    destroy() { destroyed = true; generation++; stopStream(); snapshot?.abort(); listeners.clear(); },
  };
}
