/** 独立文字反馈合同；浏览器只能提交用户意见，管理员资料由网站后端附带。 */
import { validFollowupId, validFollowupRequestId } from "./followup-contract.js";
const encoder = new TextEncoder();
const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const optionalText = (value, limit) => value == null || typeof value === "string" && encoder.encode(value).length <= limit;
export function validBrowserFeedbackComment(value) {
  return record(value) && validFollowupRequestId(value.request_id) &&
    Object.keys(value).every((key) => ["request_id", "text"].includes(key)) && optionalText(value.text, 16384);
}
export function normalizeFeedbackComment(value) {
  if (!record(value) || !validFollowupRequestId(value.request_id) ||
      !Object.keys(value).every((key) => ["request_id", "text", "analysis_text", "analysis_reference"].includes(key)) ||
      !optionalText(value.text, 16384) || !optionalText(value.analysis_text, 65536) ||
      value.analysis_reference != null && (typeof value.analysis_reference !== "string" || [...value.analysis_reference].length > 256))
    throw new TypeError("反馈字段或长度无效。");
  const content = (value) => typeof value === "string" && value.trim() ? value : null;
  const result = { request_id: value.request_id, text: content(value.text),
    analysis_text: content(value.analysis_text), analysis_reference: content(value.analysis_reference) };
  if (!result.text && !result.analysis_text || result.analysis_reference && !result.analysis_text)
    throw new TypeError("请填写意见或附带管理员分析资料；资料来源标识须与分析资料一起提交。");
  return result;
}
export function validateFeedbackCommentReceipt(value, conversationId, runId, followupId, requestId) {
  const keys = ["schema_version", "conversation_id", "run_id", "followup_id", "comment_id", "request_id", "created_at"];
  if (!record(value) || Object.keys(value).length !== keys.length || !keys.every((key) => Object.hasOwn(value, key)) ||
      value.schema_version !== 1 || value.conversation_id !== conversationId || value.run_id !== runId ||
      value.followup_id !== (followupId ?? null) || value.request_id !== requestId || !validFollowupId(value.comment_id) ||
      typeof value.created_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value.created_at) ||
      !Number.isFinite(Date.parse(value.created_at))) throw new TypeError("反馈回执与提交对象不一致或格式无效。");
  return value;
}
