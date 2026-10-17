/** 离线预览专用：只修改浏览器内存，报告变化文字均为合成样例。 */
export function createPreviewContinuations({ conversations, id, now, finish, schedule = setTimeout, delay = 1200 }) {
  const drafts = new Map(), receipts = new Map(), uploads = new Map();
  const diagnoses = new Map();
  const problem = (view) => view.result.report?.problem_statement ?? "付款服务调用库存服务超时，请定位原因。";
  function rootRun(record, runId) { const parent = record.runs.get(runId).parent_run_id; return parent ? rootRun(record, parent) : runId; }
  function changes(source, text, attachments) {
    const previous = source.attachments ?? [];
    return { before_problem_text: problem(source), after_problem_text: text,
      added_attachments: attachments.filter((item) => !previous.some((old) => old.attachment_id === item.attachment_id)),
      removed_attachments: previous.filter((item) => !attachments.some((next) => next.attachment_id === item.attachment_id)) };
  }
  function metadata(record, ids) {
    const candidates = [...record.runs.values()].flatMap((view) => view.attachments ?? []);
    return ids.map((value) => uploads.get(value) ?? candidates.find((item) => item.attachment_id === value))
      .filter((item) => item?.conversation_id === record.id && item.status === "READY");
  }
  const handle = async (url, init) => {
    const method = init.method ?? "GET";
    if (url.pathname === "/api/agent/attachments" && method === "POST") {
      const input = JSON.parse(init.body), key = `upload:${input.conversation_id}:${input.request_id}`;
      if (!conversations.has(input.conversation_id)) return finish("会话不存在。", 404);
      if (receipts.has(key)) return finish(receipts.get(key));
      const attachment = { attachment_id: id(), conversation_id: input.conversation_id, request_id: input.request_id,
        name: input.name, content_type: input.content_type, size: input.declared_size, sha256: input.declared_sha256,
        status: "RESERVED", created_at: now(), case_attachment_id: null };
      uploads.set(attachment.attachment_id, attachment);
      const result = { attachment, upload: { attachment_id: attachment.attachment_id, method: "PUT",
        url: `/api/agent/attachments/${attachment.attachment_id}/content`, expected_content_length: attachment.size,
        max_bytes: 2_684_354_560, expires_at: null, required_headers: { "Idempotency-Key": attachment.attachment_id,
          "Content-Type": attachment.content_type, "X-Content-SHA256": attachment.sha256 } } };
      receipts.set(key, result); return finish(result);
    }
    const upload = url.pathname.match(/^\/api\/agent\/attachments\/([^/]+)\/content$/);
    if (upload && method === "PUT") {
      const item = uploads.get(upload[1]);
      if (!item || init.body?.size !== item.size) return finish("预览上传与预约不一致。", 400);
      item.status = "READY"; return finish(item);
    }
    const runMatch = url.pathname.match(/^\/api\/agent\/conversations\/([^/]+)\/runs\/([^/]+)\/(continuation-drafts|report-versions)$/);
    const draftMatch = url.pathname.match(/^\/api\/agent\/conversations\/([^/]+)\/continuation-drafts\/([^/]+)(\/confirm)?$/);
    if (!runMatch && !draftMatch) return null;
    const match = runMatch ?? draftMatch, record = conversations.get(match[1]);
    if (!record) return finish("预览会话不存在。", 404);
    const source = runMatch ? record.runs.get(match[2]) : record.runs.get(drafts.get(match[2])?.parent_run_id);
    if (!source) return finish("诊断轮次或草稿不存在。", 404);
    if (runMatch && match[3] === "report-versions" && method === "GET") {
      const root = rootRun(record, match[2]);
      const versions = [...record.runs.values()].filter((view) => rootRun(record, view.run_id) === root && view.report_state === "READY").map((view) => ({
        run_id: view.run_id, parent_run_id: view.parent_run_id ?? null, version: view.report_version ?? 1,
        created_at: view.current_run.created_at, input_changes: view.input_changes ?? null,
        diagnosis_goal: view.diagnosis_goal ?? "", runtime_changes: [],
      }));
      return finish({ conversation_id: record.id, root_run_id: root, latest_run_id: versions.at(-1).run_id,
        versions, can_continue: source.report_state === "READY" && source.result.format === "markdown" && match[2] === record.current && !source.resources_expired });
    }
    const input = init.body ? JSON.parse(init.body) : null;
    const requestKey = input ? `${record.id}:${method}:${url.pathname}:${input.request_id}` : null;
    if (requestKey && receipts.has(requestKey)) return finish(receipts.get(requestKey));
    if (runMatch && method === "POST") {
      const attachmentIds = [...new Set([...(input.log_mode === "REPLACE" ? [] : (source.attachments ?? []).map((item) => item.attachment_id)), ...(input.attachment_ids ?? [])])];
      const attachments = metadata(record, attachmentIds);
      if (attachments.length !== attachmentIds.length) return finish("所选日志不可用。", 409);
      // 固定合成样例只演示 FOLLOWUP 页面，真实语义判断由服务端 INTAKE 完成。
      const reportQuestion = input.text?.trim() === "请解释报告中的结论。" && input.effective_problem_text === undefined &&
        !input.diagnosis_goal?.trim() && !input.change_note?.trim() && !input.attachment_ids?.length && input.log_mode !== "REPLACE";
      const text = reportQuestion ? problem(source) : input.effective_problem_text ?? (input.text?.trim() ? `${problem(source)}\n\n用户补充：${input.text.trim()}` : problem(source));
      const draft = { conversation_id: record.id, draft_id: id(), parent_run_id: source.run_id, revision: 1, status: reportQuestion ? "FOLLOWUP" : "DRAFT",
        followup_text: reportQuestion ? input.text.trim() : "",
        source_problem_text: problem(source), effective_problem_text: text, change_note: input.change_note ?? "",
        diagnosis_goal: input.diagnosis_goal ?? "", log_mode: input.log_mode ?? "APPEND", attachment_ids: attachmentIds, attachments,
        input_changes: changes(source, text, attachments), confirmed_run_id: null, failure_message: null, created_at: now(), updated_at: now() };
      drafts.set(draft.draft_id, draft); receipts.set(requestKey, structuredClone(draft)); return finish(draft);
    }
    const draft = drafts.get(match[2]);
    if (!draft || draft.conversation_id !== record.id) return finish("草稿不存在。", 404);
    if (method === "GET") return finish(draft);
    if (draft.status === "FOLLOWUP") return finish("这是报告追问，请发送到原报告的追问记录；更正信息请重新准备草稿。", 409);
    if (input.expected_revision !== draft.revision) return finish("草稿已更新，请刷新后重试。", 409);
    if (method === "PATCH" && !match[3]) {
      const attachments = metadata(record, input.attachment_ids ?? draft.attachment_ids);
      Object.assign(draft, { ...input, attachment_ids: attachments.map((item) => item.attachment_id), attachments,
        revision: draft.revision + 1, updated_at: now(), status: "DRAFT" });
      delete draft.request_id; delete draft.expected_revision;
      draft.input_changes = changes(source, draft.effective_problem_text, attachments);
      receipts.set(requestKey, structuredClone(draft)); return finish(draft);
    }
    if (method === "POST" && match[3]) {
      const runId = id(), timestamp = now(), view = structuredClone(source);
      Object.assign(view, { run_id: runId, selected_run_id: runId, parent_run_id: source.run_id,
        report_version: (source.report_version ?? 1) + 1, status: "COMPLETED", report_state: "READY",
        input_changes: structuredClone(draft.input_changes), diagnosis_goal: draft.diagnosis_goal,
        attachments: structuredClone(draft.attachments), archive_status: "NOT_REQUIRED", artifacts: [] });
      view.current_run = { ...source.current_run, run_id: runId, ordinal: source.current_run.ordinal + 1,
        status: "COMPLETED", report_state: "READY", created_at: timestamp, updated_at: timestamp };
      view.result = { ...source.result, format: "markdown", report: null, artifact: null, archive_status: "NOT_REQUIRED",
        markdown: `# 继续诊断报告（合成样例）\n\n## 本轮变化\n已采纳更新后的问题，并按核对后的日志清单生成此示例。这里仅演示新旧报告的呈现方式，没有分析真实日志。\n\n## 本轮问题\n${draft.effective_problem_text}\n\n## 诊断目标\n${draft.diagnosis_goal || "进一步核对原诊断的原因与证据。"}\n\n## 当前判断\n示例记录保留请求超时这一判断；真实根因需要服务端诊断结果支持。\n` };
      record.runs.set(runId, view); record.current = runId; record.updated = timestamp;
      record.history.push({ id: id(), run_id: runId, type: "diagnosis.result", created_at: timestamp, message: null,
        questions: null, result: { status: "COMPLETED", report_state: "READY", case_id: view.case_id,
          case_status: view.case_status, source_job_id: view.source_job_id, failure: null } });
      Object.assign(draft, { status: "CONFIRMED", confirmed_run_id: runId });
      const receipt = { conversation_id: record.id, run_id: runId, parent_run_id: source.run_id, request_id: input.request_id, status: "ACCEPTED" };
      receipts.set(requestKey, receipt); return finish(receipt);
    }
    return finish("预览不支持此操作。", 404);
  };
  handle.startFromFollowup = (cid, rid, input) => {
    const record = conversations.get(cid), source = record.runs.get(rid), runId = id(), timestamp = now();
    const attachments = metadata(record, [...new Set([...(source.attachments ?? []).map((item) => item.attachment_id), ...(input.attachment_ids ?? [])])]);
    const waiting = input.text === "请继续核对超时根因。" && !input.attachment_ids?.length;
    const current = structuredClone(source), text = `${problem(source)}\n\n用户补充：${input.text}`;
    const status = waiting ? "WAITING_INPUT" : "RUNNING";
    Object.assign(current, { run_id: runId, selected_run_id: runId, parent_run_id: rid, report_version: (source.report_version ?? 1) + 1,
      status, report_state: "PENDING", current_questions: waiting ? ["请补充故障发生时间。"] : [],
      progress: { stage: "DIAGNOSE", message: "正在核对补充信息和日志。" }, attachments, input_changes: changes(source, text, attachments) });
    current.current_run = { ...source.current_run, run_id: runId, ordinal: source.current_run.ordinal + 1, status,
      report_state: "PENDING", created_at: timestamp, updated_at: timestamp };
    current.result = { ...source.result, report_state: "PENDING", report: null, markdown: null, artifact: null, format: null };
    record.runs.set(runId, current); record.current = runId; record.updated = timestamp;
    const publish = () => {
      if (!conversations.has(cid) || current.status !== "RUNNING") return;
      Object.assign(current, { status: "COMPLETED", report_state: "READY", current_questions: [], progress: null });
      Object.assign(current.current_run, { status: "COMPLETED", report_state: "READY", updated_at: now() });
      current.result = { ...source.result, format: "markdown", report: null, artifact: null, archive_status: "NOT_REQUIRED",
        markdown: `# 继续诊断报告（合成样例）\n\n## 本轮变化\n已结合用户补充重新核对。此内容仅演示后台升级后的报告交付，不代表真实日志分析。\n\n## 本轮输入\n${text}\n` };
      record.history.push({ id: id(), run_id: runId, type: "diagnosis.result", created_at: now(), message: null, questions: null,
        result: { status: "COMPLETED", report_state: "READY", case_id: current.case_id, case_status: current.case_status, source_job_id: current.source_job_id, failure: null } });
    };
    diagnoses.set(runId, publish);
    if (!waiting) schedule(publish, delay);
    return runId;
  };
  handle.resumeDiagnosis = (record, input) => {
    const key = `supplement:${record.id}:${input.request_id}`;
    if (receipts.has(key)) return receipts.get(key);
    const current = record.runs.get(input.target_run_id);
    if (!current || current.status !== "WAITING_INPUT" || !diagnoses.has(input.target_run_id)) return null;
    current.status = current.current_run.status = "RUNNING"; current.current_questions = [];
    const result = { conversation_id: record.id, run_id: input.target_run_id, request_id: input.request_id,
      message_id: id(), event_id: record.history.length + 1, status: "ACCEPTED" };
    receipts.set(key, result); schedule(diagnoses.get(input.target_run_id), delay); return result;
  };
  return handle;
}
