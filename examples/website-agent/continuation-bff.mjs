import { validFollowupId } from "./followup-contract.js";
import { validateContinuationInput, validateContinuationDraft, validateContinuationReceipt,
  validateReportVersions } from "./continuation-contract.js";

/** 继续诊断沿用网站凭据、错误过滤和上传通道。 */
export async function handleContinuations({ request, response, url, authContext, api, json, jsonBody, HttpError }) {
  const runMatch = url.pathname.match(/^\/api\/agent\/conversations\/([^/]+)\/runs\/([^/]+)\/(continuation-drafts|report-versions)$/);
  const draftMatch = url.pathname.match(/^\/api\/agent\/conversations\/([^/]+)\/continuation-drafts\/([^/]+)(\/confirm)?$/);
  if (!runMatch && !draftMatch) return false;
  const match = runMatch ?? draftMatch, [, conversationId, key, suffix] = match;
  const action = runMatch ? suffix === "report-versions" ? "versions" : "create" : suffix ? "confirm" : request.method === "GET" ? "get" : "update";
  const method = request.method ?? "GET", expected = { versions: "GET", create: "POST", confirm: "POST", get: "GET", update: "PATCH" }[action];
  if (method !== expected) throw new HttpError(404, "接口不存在。");
  if (![conversationId, key].every(validFollowupId) || url.search)
    throw new HttpError(400, "会话、轮次或草稿标识无效，且此接口不接受查询参数。", "VALIDATION_ERROR");
  if (method === "GET" && (request.headers["transfer-encoding"] !== undefined ||
    request.headers["content-length"] !== undefined && request.headers["content-length"] !== "0"))
    throw new HttpError(400, "读取草稿和报告版本不接受请求体。", "VALIDATION_ERROR");
  let input;
  if (method !== "GET") {
    input = await jsonBody(request);
    try { validateContinuationInput(input, action); }
    catch (error) { throw new HttpError(400, error.message, "VALIDATION_ERROR"); }
  }
  const result = await api(authContext, url.pathname.replace(/^\/api\/agent\//, "/api/v1/agent/"), {
    method, ...(input ? { body: JSON.stringify(input), headers: { "Content-Type": "application/json" } } : {}),
  });
  try {
    if (action === "versions") validateReportVersions(result, conversationId, key);
    else if (action === "confirm") validateContinuationReceipt(result, conversationId, input.request_id);
    else validateContinuationDraft(result, conversationId, runMatch ? { runId: key } : { draftId: key });
  } catch { throw new HttpError(502, "定位服务返回的继续诊断内容与当前会话不一致或格式无效。"); }
  json(response, 200, { ok: true, data: result, error: null });
  return true;
}
