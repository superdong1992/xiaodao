import { validFollowupId, validFollowupRequestId, validFollowupText, validateFollowupView,
  validateFollowupReceipt, validateFollowupEvent } from "./followup-contract.js";

/**
 * 浏览器 → 网站同源后端。成功时直接返回 data，失败时抛 AgentApiError。
 * 不生成请求 ID、不自动重试、不轮询；由页面保存同一逻辑请求的 ID 和内容。
 */
export class AgentApiError extends Error {
  constructor(message, { status = 0, code = "WEBSITE_REQUEST_FAILED", details = [], retryable = false } = {}) {
    super(message);
    this.name = "AgentApiError";
    Object.assign(this, { status, code, details, retryable });
  }
}

/** headers 可传函数，每次请求读取最新的 CSRF token；不要在浏览器填写 xiaodao 内网地址。 */
export function createAgentClient({ basePath = "/api/agent", fetchImpl = globalThis.fetch,
  headers = () => ({}) } = {}) {
  const base = basePath.replace(/\/$/, "");
  if (!/^\/(?:[\w-]+\/)*[\w-]+$/.test(base)) {
    throw new TypeError("basePath 必须是网站同源路径，例如 /api/agent。");
  }
  const conversationPath = (id) => `${base}/conversations/${encodeURIComponent(id)}`;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const feedbackPath = (id, runId) => {
    if (typeof id !== "string" || !uuid.test(id) || typeof runId !== "string" || !uuid.test(runId))
      throw new TypeError("会话和轮次标识必须是小写规范 UUID。");
    return `${conversationPath(id)}/runs/${runId}/feedback`;
  };
  const feedbackResult = (data, id, runId) => {
    const keys = ["schema_version", "conversation_id", "run_id", "can_rate", "rating", "updated_at"];
    if (!data || typeof data !== "object" || Array.isArray(data) ||
        Object.keys(data).length !== keys.length || keys.some((key) => !Object.hasOwn(data, key)) ||
        data.schema_version !== 1 || data.conversation_id !== id || data.run_id !== runId ||
        typeof data.can_rate !== "boolean" || ![null, "LIKE", "DISLIKE"].includes(data.rating) ||
        (data.rating === null ? data.updated_at !== null : typeof data.updated_at !== "string" ||
          !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(data.updated_at) ||
          !Number.isFinite(Date.parse(data.updated_at)))) {
      throw new AgentApiError("评价响应与当前报告不一致或格式无效，请重新读取评价状态。", {
        status: 502, code: "WEBSITE_INVALID_RESPONSE",
      });
    }
    return data;
  };

  async function request(path, { method = "GET", body, requestHeaders, signal } = {}) {
    const combined = new Headers(typeof headers === "function" ? headers() : headers);
    for (const [key, value] of new Headers(requestHeaders)) combined.set(key, value);
    // 浏览器根据 Blob/File 计算真实长度，不允许代码设置这个请求头。
    combined.delete("Content-Length");
    combined.delete("X-Agent-Owner-Key");
    let response;
    try {
      response = await fetchImpl(path, { method, body, headers: combined, signal,
        credentials: "same-origin", redirect: "error", cache: "no-store" });
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      throw new AgentApiError("网络连接中断，请先查询任务状态；重试提交时保留原请求 ID。", {
        code: "WEBSITE_NETWORK_ERROR",
      });
    }
    let envelope;
    try { envelope = await response.json(); }
    catch (error) {
      if (error?.name === "AbortError") throw error;
      throw new AgentApiError("网站未返回有效的 JSON，请检查登录状态和反向代理配置。", {
        status: response.status, code: "WEBSITE_INVALID_RESPONSE",
      });
    }
    if (!response.ok || envelope?.ok !== true || envelope.error !== null || envelope.data == null) {
      const error = envelope?.error;
      throw new AgentApiError(typeof error?.message === "string" ? error.message : "请求未完成，请稍后查询。", {
        status: response.status,
        code: typeof error?.code === "string" ? error.code : "WEBSITE_INVALID_RESPONSE",
        details: Array.isArray(error?.details) ? error.details : [],
        retryable: error?.retryable === true,
      });
    }
    return envelope.data;
  }

  const post = (path, body) => request(path, { method: "POST", body: JSON.stringify(body),
    requestHeaders: { "Content-Type": "application/json" } });
  const pageSize = (value) => {
    if (value !== undefined && (!Number.isSafeInteger(value) || value < 1 || value > 100))
      throw new TypeError("分页条数必须是 1 到 100 的整数。");
  };
  const withQuery = (path, query) => `${path}${query.size ? `?${query.toString().replaceAll("%2C", ",")}` : ""}`;
  const followupPath = (id, runId) => {
    if (!validFollowupId(id) || !validFollowupId(runId)) throw new TypeError("会话和轮次标识必须是小写规范 UUID。");
    return `${conversationPath(id)}/runs/${runId}/followups`;
  };
  const checked = (validate) => {
    try { return validate(); }
    catch { throw new AgentApiError("追问响应与所选报告不一致或格式无效，请刷新后重试。", { status: 502, code: "WEBSITE_INVALID_RESPONSE" }); }
  };

  return {
    conversations: {
      create: (requestId) => post(`${base}/conversations`, { request_id: requestId }),
      send: (id, message) => post(`${conversationPath(id)}/messages`, message),
      list({ cursor, limit, signal } = {}) {
        pageSize(limit);
        const query = new URLSearchParams();
        if (cursor !== undefined) query.set("cursor", cursor);
        if (limit !== undefined) query.set("limit", limit);
        return request(withQuery(`${base}/conversations`, query), { signal });
      },
      rename: (id, title) => request(conversationPath(id), { method: "PATCH", body: JSON.stringify({ title }),
        requestHeaders: { "Content-Type": "application/json" } }),
      stop: (id, { request_id, run_id }) => post(`${conversationPath(id)}/stop`, { request_id, run_id }),
      delete: (id) => request(conversationPath(id), { method: "DELETE" }),
      get(id, { include, run_id, history_before, history_limit, signal } = {}) {
        const query = new URLSearchParams();
        if (include !== undefined) {
          if (!Array.isArray(include) || new Set(include).size !== include.length ||
              include.some((item) => !["history", "report", "artifacts"].includes(item))) {
            throw new TypeError("include 必须是不重复的 history、report、artifacts 数组；空数组仅查询状态。");
          }
          query.set("include", include.length ? include.join(",") : "none");
        }
        pageSize(history_limit);
        if (run_id !== undefined) query.set("run_id", run_id);
        if (history_before !== undefined) query.set("history_before", history_before);
        if (history_limit !== undefined) query.set("history_limit", history_limit);
        return request(withQuery(conversationPath(id), query), { signal });
      },
      async getFeedback(id, runId, { signal } = {}) {
        return feedbackResult(await request(feedbackPath(id, runId), { signal }), id, runId);
      },
      async setFeedback(id, runId, feedback) {
        const path = feedbackPath(id, runId);
        if (!feedback || typeof feedback !== "object" || Array.isArray(feedback) ||
            Object.keys(feedback).length !== 2 || typeof feedback.request_id !== "string" ||
            !feedback.request_id.trim() || [...feedback.request_id].length > 128 ||
            !["LIKE", "DISLIKE"].includes(feedback.rating)) {
          throw new TypeError("评价只接受 request_id 和 rating；request_id 为 1 到 128 个字符，rating 为 LIKE 或 DISLIKE。");
        }
        const data = await request(path, { method: "PUT",
          body: JSON.stringify({ request_id: feedback.request_id, rating: feedback.rating }),
          requestHeaders: { "Content-Type": "application/json" } });
        return feedbackResult(data, id, runId);
      },
      eventsUrl: (id) => `${conversationPath(id)}/events`,
    },
    followups: {
      async list(id, runId, { cursor, limit, signal } = {}) {
        const path = followupPath(id, runId), query = new URLSearchParams();
        pageSize(limit);
        if (cursor !== undefined) query.set("cursor", cursor);
        if (limit !== undefined) query.set("limit", limit);
        const data = await request(withQuery(path, query), { signal });
        return checked(() => validateFollowupView(data, id, runId));
      },
      async send(id, runId, input) {
        const path = followupPath(id, runId);
        if (!input || Object.keys(input).length !== 2 || !validFollowupRequestId(input.request_id) || !validFollowupText(input.text))
          throw new TypeError("追问只接受 request_id 和非空 text；request_id 最多 128 个字符，text 最多 65536 UTF-8 字节。");
        const data = await post(path, { request_id: input.request_id, text: input.text });
        return checked(() => validateFollowupReceipt(data, id, runId, input.request_id));
      },
      async stop(id, runId, followupId, input) {
        const path = followupPath(id, runId);
        if (!validFollowupId(followupId) || !input || Object.keys(input).length !== 1 || !validFollowupRequestId(input.request_id))
          throw new TypeError("停止追问需要有效的 followup_id 和稳定 request_id。");
        const data = await post(`${path}/${followupId}/stop`, { request_id: input.request_id });
        return checked(() => validateFollowupReceipt(data, id, runId, input.request_id, followupId));
      },
      /** 消费独立追问流；只在 onEvent 成功后继续读取。断开不等于停止任务。 */
      async events(id, runId, { after = 0, signal, onEvent } = {}) {
        const path = followupPath(id, runId);
        if (!Number.isSafeInteger(after) || after < 0 || typeof onEvent !== "function") throw new TypeError("请提供有效的事件游标和 onEvent。");
        const combined = new Headers(typeof headers === "function" ? headers() : headers);
        combined.delete("Content-Length"); combined.delete("X-Agent-Owner-Key");
        combined.set("Accept", "text/event-stream"); combined.set("Last-Event-ID", String(after));
        const response = await fetchImpl(`${path}/events`, { headers: combined, signal,
          credentials: "same-origin", redirect: "error", cache: "no-store" });
        if (!response.ok) {
          const envelope = await response.json();
          throw new AgentApiError(envelope?.error?.message ?? "追问事件暂时无法读取。", {
            status: response.status, code: envelope?.error?.code, retryable: envelope?.error?.retryable === true,
          });
        }
        if (!response.body || !response.headers.get("content-type")?.startsWith("text/event-stream")) {
          await response.body?.cancel();
          throw new AgentApiError("追问事件响应格式无效。", { status: 502, code: "WEBSITE_INVALID_RESPONSE" });
        }
        const reader = response.body.getReader(), decoder = new TextDecoder("utf-8", { fatal: true });
        let buffer = "", data = [], frameLength = 0;
        const abort = () => { void reader.cancel().catch(() => {}); };
        signal?.addEventListener("abort", abort, { once: true });
        try {
          while (!signal?.aborted) {
            const chunk = await reader.read();
            if (chunk.done) break;
            buffer += decoder.decode(chunk.value, { stream: true });
            let boundary;
            while ((boundary = buffer.indexOf("\n")) !== -1) {
              const line = buffer.slice(0, boundary).replace(/\r$/, ""); buffer = buffer.slice(boundary + 1);
              frameLength += line.length;
              if (frameLength > 1024 * 1024) throw new AgentApiError("追问事件超过大小上限。", { code: "WEBSITE_INVALID_RESPONSE" });
              if (!line) {
                if (data.length) {
                  const event = checked(() => validateFollowupEvent(JSON.parse(data.join("\n")), id, runId));
                  if (event.sequence > after) { await onEvent(event); after = event.sequence; }
                }
                data = []; frameLength = 0;
              } else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
            }
            if (buffer.length + frameLength > 1024 * 1024) throw new AgentApiError("追问事件超过大小上限。", { code: "WEBSITE_INVALID_RESPONSE" });
          }
        } finally {
          signal?.removeEventListener("abort", abort);
          await reader.cancel().catch(() => {}); reader.releaseLock();
        }
      },
    },
    attachments: {
      prepare: (id, metadata) => post(`${base}/attachments`, { ...metadata, conversation_id: id }),

      /** prepared 是 attachments.prepare 的返回值；上传不重新读取或计算文件哈希。 */
      async upload(prepared, file) {
        const { attachment, upload } = prepared ?? {};
        const uploadHeaders = new Headers(upload?.required_headers);
        if (!attachment || !upload || upload.method !== "PUT" ||
            !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(attachment.attachment_id) ||
            upload.attachment_id !== attachment.attachment_id || !(file instanceof Blob) ||
            file.size < 1 || file.size !== upload.expected_content_length || file.size !== attachment.size ||
            uploadHeaders.get("Idempotency-Key") !== attachment.attachment_id ||
            uploadHeaders.get("Content-Type") !== attachment.content_type ||
            !/^[0-9a-f]{64}$/.test(attachment.sha256) ||
            uploadHeaders.get("X-Content-SHA256") !== attachment.sha256) {
          throw new TypeError("上传文件与预约不一致，请使用预约时的原始文件。");
        }
        // 固定走本站路径，描述符中的 URL 不用于跨域寻址。
        return request(`${base}/attachments/${encodeURIComponent(attachment.attachment_id)}/content`, {
          method: "PUT", body: file, requestHeaders: uploadHeaders,
        });
      },
    },
  };
}
