import { validFollowupId } from "./followup-contract.js";
import { validBrowserFeedbackComment, normalizeFeedbackComment, validateFeedbackCommentReceipt } from "./feedback-comment-contract.js";

/** loadFeedbackAnalysis 必须校验网站资料归属，并按主体、目标和 requestId 持久保存首次资料快照。 */
export async function handleFeedbackComments({ request, response, url, authContext, api, json,
  jsonBody, HttpError, loadFeedbackAnalysis }) {
  const match = url.pathname.match(/^\/api\/agent\/conversations\/([^/]+)\/runs\/([^/]+)(?:\/followups\/([^/]+))?\/feedback\/comments$/);
  if (!match) return false;
  if (request.method !== "POST") throw new HttpError(404, "接口不存在。");
  const [, conversationId, runId, followupId] = match;
  if (url.search || ![conversationId, runId, ...(followupId ? [followupId] : [])].every(validFollowupId))
    throw new HttpError(400, "反馈目标无效，且此接口不接受查询参数。", "VALIDATION_ERROR");
  const input = await jsonBody(request);
  if (!validBrowserFeedbackComment(input))
    throw new HttpError(400, "浏览器只可提交 request_id 和可选 text；管理员资料须由网站后端提供。", "VALIDATION_ERROR");
  const path = `/api/v1/agent/conversations/${conversationId}/runs/${runId}` +
    (followupId ? `/followups/${followupId}` : "") + "/feedback";
  let analysis = null;
  if (loadFeedbackAnalysis) {
    // 先核对小刀会话归属；换票后的旧请求重放仍由写接口判断，不能在此要求 DISLIKE。
    await api(authContext, path, { method: "GET" }, 65536);
    analysis = await loadFeedbackAnalysis({ authContext, conversationId, runId, followupId: followupId ?? null,
      requestId: input.request_id, text: input.text?.trim() ? input.text : null });
    if (analysis !== null && analysis !== undefined &&
        (typeof analysis !== "object" || Array.isArray(analysis) ||
         Object.keys(analysis).some((key) => !["analysis_text", "analysis_reference"].includes(key))))
      throw new HttpError(500, "网站分析资料格式无效。");
  }
  let body;
  try { body = normalizeFeedbackComment({ ...input, ...analysis }); }
  catch { throw new HttpError(400, "请填写有效意见或附带有效的管理员分析资料。", "VALIDATION_ERROR"); }
  const result = await api(authContext, path + "/comments", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, 65536);
  try { validateFeedbackCommentReceipt(result, conversationId, runId, followupId, input.request_id); }
  catch { throw new HttpError(502, "定位服务返回的反馈回执与提交对象不一致或格式无效。"); }
  json(response, 200, { ok: true, data: result, error: null });
  return true;
}
