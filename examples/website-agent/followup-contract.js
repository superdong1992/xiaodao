/** 浏览器和 BFF 共用的追问合同；不包含请求、存储或模型调用。 */
export const FOLLOWUP_ACTIVE = new Set(["QUEUED", "RUNNING", "CANCELLING"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const states = ["QUEUED", "RUNNING", "CANCELLING", "COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"];
const encoder = new TextEncoder();
const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const exact = (value, keys) => record(value) && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
const integer = (value, min = 0) => Number.isSafeInteger(value) && value >= min;
const date = (value) => typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
const bounded = (value) => typeof value === "string" && encoder.encode(value).length <= 65536;
export const validFollowupId = (value) => typeof value === "string" && UUID.test(value);
export const validFollowupRequestId = (value) => typeof value === "string" && !!value.trim() && [...value].length <= 128;
export const validFollowupText = (value) => bounded(value) && !!value.trim() && [...value].length <= 65536;
export const FOLLOWUP_FAILURE_MESSAGES = {
  AGENT_FOLLOWUP_FAILED: "本次追问未能完成，请稍后重新提问。",
  AGENT_FOLLOWUP_INTERRUPTED: "本次追问已中断，请重新提问。",
  AGENT_FOLLOWUP_INPUT_CHANGED: "本次追问的依据已变化，请刷新后重新提问。",
};
function requireValid(condition) {
  if (!condition) throw new TypeError("追问响应与所选报告不一致或格式无效。");
}
export function validateFollowupItem(value, runId) {
  requireValid(exact(value, ["followup_id", "run_id", "request_id", "ordinal", "status", "context_mode", "text", "answer_markdown", "failure", "created_at", "updated_at"]) &&
    validFollowupId(value.followup_id) && validFollowupId(value.run_id) && (runId === undefined || value.run_id === runId) &&
    validFollowupRequestId(value.request_id) && integer(value.ordinal, 1) && states.includes(value.status) &&
    ["REPORT_ONLY", "REPORT_AND_LOGS"].includes(value.context_mode) && validFollowupText(value.text) &&
    (value.answer_markdown === null || bounded(value.answer_markdown)) && date(value.created_at) && date(value.updated_at) &&
    (value.failure === null || exact(value.failure, ["code", "message"]) && Object.hasOwn(FOLLOWUP_FAILURE_MESSAGES, value.failure.code) && typeof value.failure.message === "string"));
  requireValid((value.status === "COMPLETED") === (value.answer_markdown !== null) &&
    (value.answer_markdown === null || !!value.answer_markdown.trim()) &&
    ["FAILED", "INTERRUPTED"].includes(value.status) === (value.failure !== null));
  return value;
}
export function validateFollowupView(value, conversationId, runId) {
  requireValid(exact(value, ["schema_version", "conversation_id", "run_id", "can_ask", "reason", "snapshot_status", "active_followup", "items", "next_cursor", "last_event_id"]) &&
    value.schema_version === 1 && value.conversation_id === conversationId && value.run_id === runId &&
    typeof value.can_ask === "boolean" && [null, "DISABLED", "UNSUPPORTED", "EXPIRED", "BUSY", "LIMIT_EXCEEDED", "CONTEXT_LIMIT"].includes(value.reason) &&
    ["DISABLED", "UNAVAILABLE", "PENDING", "BUILDING", "READY", "FAILED"].includes(value.snapshot_status) &&
    Array.isArray(value.items) && value.items.length <= 100 && integer(value.last_event_id) &&
    (value.next_cursor === null || typeof value.next_cursor === "string"));
  let previous = 0;
  const ids = new Set();
  for (const item of value.items) {
    validateFollowupItem(item, runId);
    requireValid(item.ordinal > previous && !ids.has(item.followup_id));
    previous = item.ordinal; ids.add(item.followup_id);
  }
  if (value.active_followup !== null) {
    validateFollowupItem(value.active_followup);
    requireValid(FOLLOWUP_ACTIVE.has(value.active_followup.status));
  }
  return value;
}
export function validateFollowupReceipt(value, conversationId, runId, requestId, followupId) {
  requireValid(exact(value, ["conversation_id", "run_id", "followup_id", "request_id", "event_id", "status"]) &&
    value.conversation_id === conversationId && value.run_id === runId && value.request_id === requestId &&
    validFollowupId(value.followup_id) && integer(value.event_id, 1) &&
    (followupId === undefined ? value.status === "ACCEPTED" : value.followup_id === followupId && ["CANCELLING", "CANCELLED", "ALREADY_FINISHED"].includes(value.status)));
  return value;
}
export function validateFollowupEvent(value, conversationId, runId) {
  requireValid(exact(value, ["schema_version", "sequence", "conversation_id", "run_id", "followup_id", "type", "created_at", "data"]) &&
    value.schema_version === 1 && integer(value.sequence, 1) && value.conversation_id === conversationId && value.run_id === runId &&
    ["followup.accepted", "followup.updated"].includes(value.type) && date(value.created_at));
  validateFollowupItem(value.data, runId);
  requireValid(value.followup_id === value.data.followup_id);
  return value;
}
