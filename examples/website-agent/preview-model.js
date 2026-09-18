/** 只用于离线交互预览的内存数据源；不连接服务，不执行诊断。 */
export function createPreviewApi(samples) {
  let serial = 100;
  const id = () => `00000000-0000-0000-0000-${String(++serial).padStart(12, "0")}`;
  const now = () => new Date().toISOString();
  const conversations = new Map();
  const finish = (data, status = 200) => new Response(JSON.stringify({ ok: status === 200,
    data: status === 200 ? data : null, error: status === 200 ? null : {
      code: "PREVIEW_REQUEST_FAILED", message: data, details: [], retryable: false,
    } }), { status, headers: { "Content-Type": "application/json" } });
  function capabilities(view) {
    const active = ["INTAKE", "WAITING_INPUT", "RUNNING"].includes(view.status);
    return { can_send: !active || view.status !== "RUNNING", can_stop: active,
      can_rediagnose: !active, can_rename: true, can_delete: true };
  }
  function summary(record) {
    const view = record.runs.get(record.current);
    return { conversation_id: record.id, title: record.title, current_run: view.current_run,
      capabilities: capabilities(view), created_at: record.created, updated_at: record.updated };
  }
  for (const sample of samples) {
    const view = structuredClone(sample.response.data), conversationId = id(), runId = id();
    view.conversation_id = conversationId; view.selected_run_id = runId; view.run_id = runId;
    view.result.conversation_id = conversationId;
    view.current_run = { run_id: runId, ordinal: 1, status: view.status, case_id: view.case_id,
      job_id: view.job_id, case_status: view.case_status, archive_status: view.archive_status,
      report_state: view.report_state, created_at: view.created_at, updated_at: view.updated_at };
    const history = [{ id: id(), run_id: runId, type: "user.message", created_at: view.created_at,
      message: { message_id: id(), request_id: "preview", text: "付款服务调用库存服务超时，请定位原因。",
        attachment_ids: [], status: "APPLIED", created_at: view.created_at, notice: null, run_id: runId }, questions: null, result: null }];
    if (view.report_state !== "PENDING") history.push({ id: id(), run_id: runId, type: "diagnosis.result",
      created_at: view.updated_at, message: null, questions: null, result: { case_id: view.case_id,
        status: view.report_state === "READY" ? "COMPLETED" : view.status, failure: view.failure,
        case_status: view.case_status, report_state: view.report_state, source_job_id: view.source_job_id } });
    conversations.set(conversationId, { id: conversationId, title: sample.label, note: sample.note,
      current: runId, runs: new Map([[runId, view]]), history, created: view.created_at, updated: view.updated_at });
  }
  return async (path, init = {}) => {
    const url = new URL(path, "http://preview.local"), method = init.method ?? "GET";
    if (url.pathname === "/api/agent/conversations" && method === "GET") {
      const offset = Number(url.searchParams.get("cursor") ?? 0), limit = Number(url.searchParams.get("limit") ?? 20);
      const items = [...conversations.values()].map(summary);
      return finish({ items: items.slice(offset, offset + limit), next_cursor: offset + limit < items.length ? String(offset + limit) : null });
    }
    const match = url.pathname.match(/^\/api\/agent\/conversations\/([^/]+)(?:\/(messages|stop))?$/);
    const record = match && conversations.get(match[1]);
    if (!record) return finish("预览会话不存在或已删除。", 404);
    const body = init.body ? JSON.parse(init.body) : null;
    if (method === "DELETE") { conversations.delete(record.id); return finish({ conversation_id: record.id, status: "DELETED" }); }
    if (method === "PATCH") {
      if (typeof body?.title !== "string" || !body.title.trim() || body.title.length > 80) return finish("标题应为 1 到 80 个字符。", 400);
      record.title = body.title; record.updated = now(); return finish(summary(record));
    }
    if (method === "POST" && match[2] === "stop") {
      const view = record.runs.get(body.run_id);
      if (!view) return finish("诊断轮次不存在。", 404);
      const active = capabilities(view).can_stop;
      if (active) {
        view.status = "CANCELLED"; view.current_run.status = "CANCELLED";
        view.report_state = "UNAVAILABLE"; view.current_run.report_state = "UNAVAILABLE";
        view.result = { ...view.result, report_state: "UNAVAILABLE", report: null, markdown: null, artifact: null, format: null };
        record.history.push({ id: id(), run_id: body.run_id, type: "diagnosis.result", created_at: now(),
          message: null, questions: null, result: { status: "CANCELLED", case_id: view.case_id, case_status: "CANCELLED",
            report_state: "UNAVAILABLE", source_job_id: null, failure: null } });
      }
      return finish({ conversation_id: record.id, run_id: body.run_id, request_id: body.request_id,
        status: active ? "CANCELLED" : "ALREADY_FINISHED" });
    }
    if (method === "POST" && match[2] === "messages") {
      const previous = record.runs.get(record.current), runId = id(), timestamp = now();
      const view = structuredClone(previous);
      // 演示一轮全新的等待状态；不会沿用上一轮报告。
      Object.assign(view, { run_id: runId, selected_run_id: runId, status: "WAITING_INPUT", report_state: "PENDING",
        case_id: null, case_revision: null, source_job_id: null, job_id: null, case_status: null,
        archive_status: "NOT_REQUIRED", artifacts: [], failure: null, current_questions: ["请补充此轮问题对应的日志。"] });
      view.current_run = { ...view.current_run, run_id: runId, ordinal: previous.current_run.ordinal + 1,
        status: view.status, report_state: "PENDING", case_id: null, job_id: null, case_status: null,
        archive_status: "NOT_REQUIRED", created_at: timestamp, updated_at: timestamp };
      view.result = { schema_version: 1, conversation_id: record.id, case_id: null, case_revision: null,
        case_status: null, archive_status: "NOT_REQUIRED", report_state: "PENDING", source_job_id: null,
        format: null, report: null, markdown: null, artifact: null, failure: null };
      record.current = runId; record.runs.set(runId, view); record.updated = timestamp;
      const message = { message_id: id(), run_id: runId, request_id: body.request_id, text: body.text,
        attachment_ids: [], status: "APPLIED", created_at: timestamp, notice: null };
      record.history.push({ id: message.message_id, run_id: runId, type: "user.message", created_at: timestamp,
        message, questions: null, result: null }, { id: id(), run_id: runId, type: "assistant.question", created_at: timestamp,
        message: null, questions: view.current_questions, result: null });
      return finish({ conversation_id: record.id, run_id: runId, request_id: body.request_id,
        message_id: message.message_id, event_id: record.history.length, status: "ACCEPTED" });
    }
    if (method !== "GET" || match[2]) return finish("预览不支持此操作。", 404);
    const selected = url.searchParams.get("run_id") ?? record.current, source = record.runs.get(selected);
    if (!source) return finish("诊断轮次不存在。", 404);
    const view = structuredClone(source), include = url.searchParams.get("include");
    view.schema_version = 3; view.title = record.title; view.current_run = structuredClone(record.runs.get(record.current).current_run);
    view.capabilities = capabilities(record.runs.get(record.current)); view.included = include === "none" ? [] : include?.split(",") ?? ["history", "report", "artifacts"];
    const end = Number(url.searchParams.get("history_before") ?? record.history.length), limit = Number(url.searchParams.get("history_limit") ?? 50);
    view.history = view.included.includes("history") ? structuredClone(record.history.slice(Math.max(0, end - limit), end)) : null;
    view.history_next_cursor = view.history && end > limit ? String(end - limit) : null;
    if (!view.included.includes("history")) view.attachments = null;
    if (!view.included.includes("report")) view.result = null;
    if (!view.included.includes("artifacts")) view.artifacts = null;
    return finish(view);
  };
}
