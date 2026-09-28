import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { validFollowupId, validFollowupRequestId, validFollowupText, validateFollowupView,
  validateFollowupReceipt, FOLLOWUP_FAILURE_MESSAGES } from "./followup-contract.js";

/** 独立路由，复用主 BFF 本次请求的凭据和错误边界。 */
export async function handleFollowups({ request, response, url, authContext, api, upstreamFetch,
  json, jsonBody, HttpError, safeError, boundedResponseJson, validateQuery, checkLimit }) {
  const match = url.pathname.match(/^\/api\/agent\/conversations\/([^/]+)\/runs\/([^/]+)\/followups(?:\/(events)|\/([^/]+)\/stop)?$/);
  if (!match) return false;
  const [, conversationId, runId, events, followupId] = match, method = request.method ?? "GET";
  if (![conversationId, runId, ...(followupId ? [followupId] : [])].every(validFollowupId))
    throw new HttpError(400, "会话、轮次或追问标识无效。", "VALIDATION_ERROR");
  if (events ? method !== "GET" : followupId ? method !== "POST" : !["GET", "POST"].includes(method))
    throw new HttpError(404, "接口不存在。");
  const path = `/api/v1/agent/conversations/${conversationId}/runs/${runId}/followups`;
  if (method === "GET" && (request.headers["transfer-encoding"] !== undefined ||
    request.headers["content-length"] !== undefined && request.headers["content-length"] !== "0"))
    throw new HttpError(400, "读取追问不接受请求体。", "VALIDATION_ERROR");
  if (events || method === "POST") {
    if (url.search) throw new HttpError(400, "此接口不接受查询参数。", "VALIDATION_ERROR");
  } else { validateQuery(url, ["cursor", "limit"]); checkLimit(url.searchParams.get("limit")); }
  if (events) {
    const headers = new Headers({ Accept: "text/event-stream" }), cursor = request.headers["last-event-id"];
    if (cursor !== undefined) {
      if (typeof cursor !== "string" || !/^(0|[1-9][0-9]{0,18})$/.test(cursor))
        throw new HttpError(400, "事件游标无效。", "VALIDATION_ERROR");
      headers.set("Last-Event-ID", cursor);
    }
    const controller = new AbortController();
    response.once("close", () => controller.abort());
    const upstream = await upstreamFetch(authContext, `${path}/events`, { headers, signal: controller.signal });
    if (!upstream.ok) {
      const envelope = await boundedResponseJson(upstream), error = safeError(envelope.error);
      throw new HttpError([400, 401, 403, 404, 409, 413, 422, 429, 500, 503, 504].includes(upstream.status) ? upstream.status : 502,
        error.message, error.code, error.details, error.retryable);
    }
    if (!upstream.body || !upstream.headers.get("content-type")?.startsWith("text/event-stream")) {
      await upstream.body?.cancel(); throw new HttpError(502, "暂时无法订阅追问事件。");
    }
    response.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no" });
    await pipeline(Readable.fromWeb(upstream.body), response);
    return true;
  }
  let body;
  if (method === "POST") {
    const input = await jsonBody(request);
    if (!validFollowupRequestId(input.request_id) || Object.keys(input).length !== (followupId ? 1 : 2) ||
      !followupId && !validFollowupText(input.text))
      throw new HttpError(400, "追问请求字段无效，请保留原 request_id 和非空文字。", "VALIDATION_ERROR");
    body = followupId ? { request_id: input.request_id } : { request_id: input.request_id, text: input.text };
  }
  // 正文每项最多两段 64 KiB，另留 active_followup 和 JSON 最多六倍转义空间。
  const maxBytes = method === "GET" ? ((Number(url.searchParams.get("limit") ?? 50) + 1) * (2 * 65536 * 6 + 8192) + 65536) : 65536;
  const result = await api(authContext, `${path}${followupId ? `/${followupId}/stop` : url.search}`, {
    method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined,
  }, maxBytes);
  try {
    if (method === "GET") validateFollowupView(result, conversationId, runId);
    else validateFollowupReceipt(result, conversationId, runId, body.request_id, followupId);
  } catch { throw new HttpError(502, "定位服务返回的追问与所选报告不一致或格式无效。"); }
  if (method === "GET") {
    for (const item of [...result.items, ...(result.active_followup ? [result.active_followup] : [])])
      if (item.failure) item.failure.message = FOLLOWUP_FAILURE_MESSAGES[item.failure.code];
  }
  json(response, 200, { ok: true, data: result, error: null });
  return true;
}
