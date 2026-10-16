import { validFollowupId, validFollowupRequestId, validFollowupText } from "./followup-contract.js";

const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value) => typeof value === "string" && new TextEncoder().encode(value).length <= 65536;
const ids = (value) => Array.isArray(value) && value.length <= 20 && value.every(validFollowupId) && new Set(value).size === value.length;
export const CONTINUATION_FAILURE_MESSAGES = {
  AGENT_CONTINUATION_UNSUPPORTED: "这份报告暂不支持继续诊断。",
  AGENT_DIAGNOSIS_BUSY: "本会话还有诊断正在运行，请结束后再继续诊断。",
  AGENT_DRAFT_NOT_FOUND: "继续诊断草稿不存在，请重新读取会话。",
  AGENT_DRAFT_CHANGED: "草稿正在整理、已确认或已更新，请刷新后核对内容。",
  AGENT_REPORT_REQUIRED: "请先等待正式报告生成。",
  AGENT_REPORT_CHANGED: "已有新版报告，请从最新报告继续诊断。",
  AGENT_INPUT_UNAVAILABLE: "原问题信息不可用，请重新提供完整问题。",
  AGENT_FOLLOWUP_REQUIRED: "这是对当前报告的追问，请发送到原报告的问答区；需要更正信息时，请重新准备草稿。",
};

export function validateContinuationInput(input, action) {
  const fields = action === "confirm" ? ["request_id", "expected_revision"] : ["request_id", "effective_problem_text",
    "change_note", "diagnosis_goal", "log_mode", "attachment_ids", ...(action === "create" ? ["text"] : ["expected_revision"])];
  if (!object(input) || !validFollowupRequestId(input.request_id) || Object.keys(input).some((key) => !fields.includes(key)) ||
    action !== "create" && (!Number.isSafeInteger(input.expected_revision) || input.expected_revision < 1) ||
    ["text", "effective_problem_text", "change_note", "diagnosis_goal"].some((key) => Object.hasOwn(input, key) && !text(input[key])) ||
    Object.hasOwn(input, "effective_problem_text") && !input.effective_problem_text.trim() ||
    Object.hasOwn(input, "diagnosis_goal") && [...input.diagnosis_goal].length > 8192 ||
    Object.hasOwn(input, "log_mode") && !["APPEND", "REPLACE"].includes(input.log_mode) ||
    Object.hasOwn(input, "attachment_ids") && !ids(input.attachment_ids))
    throw new TypeError("继续诊断请求无效，请核对文字、日志清单、请求 ID 和草稿版本。");
  return input;
}

function attachment(value) {
  return object(value) && validFollowupId(value.attachment_id) && typeof value.name === "string";
}
function changes(value) {
  return object(value) && text(value.before_problem_text) && text(value.after_problem_text) &&
    Array.isArray(value.added_attachments) && value.added_attachments.every(attachment) &&
    Array.isArray(value.removed_attachments) && value.removed_attachments.every(attachment);
}
export function validateContinuationDraft(value, conversationId, { draftId, runId } = {}) {
  if (!object(value) || value.conversation_id !== conversationId || !validFollowupId(value.draft_id) ||
    draftId && value.draft_id !== draftId || !validFollowupId(value.parent_run_id) || runId && value.parent_run_id !== runId ||
    !Number.isSafeInteger(value.revision) || value.revision < 1 ||
    !["PREPARING", "DRAFT", "FAILED", "CONFIRMED", "FOLLOWUP"].includes(value.status) ||
    ["source_problem_text", "effective_problem_text", "change_note", "diagnosis_goal"].some((key) => !text(value[key])) ||
    Object.hasOwn(value, "followup_text") && !text(value.followup_text) ||
    value.status === "FOLLOWUP" && !validFollowupText(value.followup_text) ||
    !["APPEND", "REPLACE"].includes(value.log_mode) || !ids(value.attachment_ids) ||
    !Array.isArray(value.attachments) || !value.attachments.every(attachment) ||
    value.attachments.length !== value.attachment_ids.length ||
    new Set(value.attachments.map((item) => item.attachment_id)).size !== value.attachments.length ||
    value.attachments.some((item) => !value.attachment_ids.includes(item.attachment_id)) || !changes(value.input_changes) ||
    value.failure_message != null && typeof value.failure_message !== "string" ||
    value.status === "CONFIRMED" && !validFollowupId(value.confirmed_run_id))
    throw new TypeError("继续诊断草稿与当前会话不一致或格式无效。");
  return value;
}
export function validateContinuationReceipt(value, conversationId, requestId) {
  if (!object(value) || value.conversation_id !== conversationId || value.request_id !== requestId ||
    !validFollowupId(value.run_id) || !validFollowupId(value.parent_run_id) || value.status !== "ACCEPTED")
    throw new TypeError("继续诊断回执与当前请求不一致。");
  return value;
}
export function validateReportVersions(value, conversationId, runId) {
  if (!object(value) || value.conversation_id !== conversationId || !validFollowupId(value.root_run_id) ||
    !validFollowupId(value.latest_run_id) || typeof value.can_continue !== "boolean" || !Array.isArray(value.versions) ||
    !value.versions.some((item) => item.run_id === runId) ||
    new Set(value.versions.map((item) => item.run_id)).size !== value.versions.length ||
    value.versions.some((item) => !object(item) || !validFollowupId(item.run_id) ||
      item.parent_run_id !== null && !validFollowupId(item.parent_run_id) ||
      !Number.isSafeInteger(item.version) || item.version < 1 || typeof item.created_at !== "string" ||
      !Array.isArray(item.runtime_changes) || item.runtime_changes.some((change) => typeof change !== "string") ||
      item.input_changes != null && !changes(item.input_changes)))
    throw new TypeError("报告版本记录与当前会话不一致或格式无效。");
  return value;
}
