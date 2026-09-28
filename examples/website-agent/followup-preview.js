import { FOLLOWUP_ACTIVE } from "./followup-contract.js";

/** 合成追问服务，只供 preview-model 使用。不会读日志或调用模型。 */
export function createPreviewFollowups({ conversations, id, now, schedule = setTimeout, delay = 1200 }) {
  const runs = new Map(), encoder = new TextEncoder();
  const finish = (data, code, status = 200) => new Response(JSON.stringify({ ok: status === 200,
    data: status === 200 ? data : null, error: status === 200 ? null : { code, message: data, details: [], retryable: false } }),
  { status, headers: { "Content-Type": "application/json" } });
  const stateFor = (cid, rid) => {
    const key = `${cid}:${rid}`;
    if (!runs.has(key)) runs.set(key, { cid, rid, items: [], events: [], listeners: new Set(), requests: new Map() });
    return runs.get(key);
  };
  const activeFor = (cid) => [...runs.values()].filter((state) => state.cid === cid).flatMap((state) => state.items).find((item) => FOLLOWUP_ACTIVE.has(item.status)) ?? null;
  const publish = (state, item, type = "followup.updated") => {
    const event = { schema_version: 1, sequence: state.events.length + 1, conversation_id: state.cid,
      run_id: state.rid, followup_id: item.followup_id, type, created_at: now(), data: structuredClone(item) };
    state.events.push(event);
    for (const notify of [...state.listeners]) notify(event);
  };
  return async (url, init) => {
    const match = url.pathname.match(/^\/api\/agent\/conversations\/([^/]+)\/runs\/([^/]+)\/followups(?:\/(events)|\/([^/]+)\/stop)?$/);
    if (!match) return null;
    const [, cid, rid, events, fid] = match, method = init.method ?? "GET", record = conversations.get(cid), view = record?.runs.get(rid);
    if (!view) return finish("预览报告不存在。", "AGENT_RUN_NOT_FOUND", 404);
    const state = stateFor(cid, rid), active = activeFor(cid);
    const supported = view.report_state === "READY" && view.result.format === "markdown";
    const snapshotStatus = record.snapshotStatus ?? "READY";
    if (events && method === "GET") {
      const after = Number(new Headers(init.headers).get("Last-Event-ID") ?? 0);
      let cleanup;
      const body = new ReadableStream({
        start(controller) {
          let closed = false;
          const close = () => { if (!closed) { closed = true; cleanup(); controller.close(); } };
          const notify = (event) => {
            if (closed) return;
            controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
            if (!state.items.some((item) => FOLLOWUP_ACTIVE.has(item.status))) close();
          };
          cleanup = () => { state.listeners.delete(notify); init.signal?.removeEventListener("abort", close); };
          controller.enqueue(encoder.encode(": connected\n\n"));
          for (const event of state.events.filter((event) => event.sequence > after))
            controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
          if (init.signal?.aborted || !state.items.some((item) => FOLLOWUP_ACTIVE.has(item.status))) close();
          else { state.listeners.add(notify); init.signal?.addEventListener("abort", close, { once: true }); }
        },
        cancel() { cleanup?.(); },
      });
      return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
    }
    if (method === "GET" && !fid) {
      const end = Number(url.searchParams.get("cursor") ?? state.items.length), limit = Number(url.searchParams.get("limit") ?? 50);
      return finish({ schema_version: 1, conversation_id: cid, run_id: rid, can_ask: supported && !active,
        reason: !supported ? "UNSUPPORTED" : active ? "BUSY" : null, snapshot_status: supported ? snapshotStatus : "UNAVAILABLE",
        active_followup: active, items: state.items.slice(Math.max(0, end - limit), end),
        next_cursor: end > limit ? String(end - limit) : null, last_event_id: state.events.length });
    }
    const input = init.body ? JSON.parse(init.body) : null;
    if (method === "POST" && fid) {
      const item = state.items.find((item) => item.followup_id === fid);
      if (!item) return finish("预览追问不存在。", "AGENT_FOLLOWUP_NOT_FOUND", 404);
      const wasActive = FOLLOWUP_ACTIVE.has(item.status);
      if (wasActive) { item.status = "CANCELLED"; item.updated_at = now(); publish(state, item); }
      return finish({ conversation_id: cid, run_id: rid, followup_id: fid, request_id: input.request_id,
        event_id: state.events.length, status: wasActive ? "CANCELLED" : "ALREADY_FINISHED" });
    }
    if (method === "POST" && !events && !fid) {
      const existing = state.requests.get(input.request_id);
      if (existing) return existing.text === input.text ? finish(existing.receipt) : finish("同一请求的文字不能更改。", "AGENT_IDEMPOTENCY_CONFLICT", 409);
      if (!supported) return finish("这份预览报告暂不支持追问。", "AGENT_FOLLOWUP_UNSUPPORTED", 409);
      if (active) return finish("本会话仍有追问正在回答。", "AGENT_FOLLOWUP_BUSY", 409);
      const timestamp = now(), item = { followup_id: id(), run_id: rid, request_id: input.request_id,
        ordinal: state.items.length + 1, status: "RUNNING", context_mode: snapshotStatus === "READY" ? "REPORT_AND_LOGS" : "REPORT_ONLY",
        text: input.text, answer_markdown: null, failure: null, created_at: timestamp, updated_at: timestamp };
      state.items.push(item); publish(state, item, "followup.accepted");
      const receipt = { conversation_id: cid, run_id: rid, followup_id: item.followup_id, request_id: input.request_id,
        event_id: state.events.length, status: "ACCEPTED" };
      state.requests.set(input.request_id, { text: input.text, receipt });
      schedule(() => {
        if (!conversations.has(cid) || !FOLLOWUP_ACTIVE.has(item.status)) return;
        item.status = "COMPLETED"; item.updated_at = now();
        item.answer_markdown = `${item.context_mode === "REPORT_ONLY" ? "本回答仅依据报告和已有问答，未重新核对原日志。\n\n" : "以下为合成示例，未读取真实日志。\n\n"}## 对这次追问的说明\n\n${input.text}\n\n报告只能确认已记录的超时现象。要确认更深层原因，还需核对调用链和同一时段的服务状态。正式报告保持原样。`;
        publish(state, item);
      }, delay);
      return finish(receipt);
    }
    return finish("预览不支持此操作。", "PREVIEW_REQUEST_FAILED", 404);
  };
}
