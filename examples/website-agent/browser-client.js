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

  async function request(path, { method = "GET", body, requestHeaders, signal } = {}) {
    const combined = new Headers(typeof headers === "function" ? headers() : headers);
    for (const [key, value] of new Headers(requestHeaders)) combined.set(key, value);
    // 浏览器根据 Blob/File 计算真实长度，不允许代码设置这个请求头。
    combined.delete("Content-Length");
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

  return {
    createConversation: (requestId) => post(`${base}/conversations`, { request_id: requestId }),
    sendMessage: (id, message) => post(`${conversationPath(id)}/messages`, message),
    getConversation: (id, options) => request(conversationPath(id), options),
    getStatus: (id, options) => request(`${conversationPath(id)}/status`, options),
    getReport: (id, options) => request(`${conversationPath(id)}/report`, options),
    prepareAttachment: (id, metadata) => post(`${conversationPath(id)}/attachments`, metadata),
    eventsUrl: (id) => `${conversationPath(id)}/events`,

    /** prepared 是 prepareAttachment 的返回值。哈希在预约前计算一次，上传不再读取整份文件。 */
    async uploadAttachment(prepared, file) {
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
  };
}
