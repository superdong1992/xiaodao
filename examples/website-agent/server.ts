/** Node.js 24+：无需 npm 依赖的内部网站后端接入示例。 */
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import { createHash } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { mkdtemp, rm, rmdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import { fileURLToPath, pathToFileURL } from "node:url";

type User = { id: string };
export type Access = {
  // 在此验证网站登录态、Cookie/CSRF 或 Bearer token，不接受前端自报 user_id。
  authenticate(request: IncomingMessage): Promise<User | null>;
  ownsConversation(user: User, conversationId: string): Promise<boolean>;
  rememberConversation(user: User, conversationId: string): Promise<void>;
  ownsAttachment(user: User, attachmentId: string): Promise<boolean>;
  rememberAttachment(user: User, conversationId: string, attachmentId: string): Promise<void>;
};

export const denyAccess: Access = {
  authenticate: async () => null,
  ownsConversation: async () => false,
  rememberConversation: async () => { throw new HttpError(503, "网站尚未配置会话归属存储。"); },
  ownsAttachment: async () => false,
  rememberAttachment: async () => { throw new HttpError(503, "网站尚未配置附件归属存储。"); },
};

export class HttpError extends Error {
  status: number;
  code: string;
  details: Json[];
  retryable: boolean;
  constructor(status: number, message: string, code = "WEBSITE_AGENT_ERROR", details: Json[] = [], retryable = false) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.retryable = retryable;
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA = /^[0-9a-f]{64}$/;
const MAX_JSON_BYTES = 1024 * 1024;
const MAX_ATTACHMENT_BYTES = 2_684_354_560;
const MAX_DOWNLOAD_BYTES = 5_368_709_120;
const MAX_REPORT_BYTES = 16 * 1024 * 1024;
const KINDS = new Set(["USER_RESULT", "USER_RESULT_ARCHIVE", "AUDIT_BUNDLE", "GENERIC_REPORT"]);
const ZIP_NOTICE = "该文件包含原始目标日志，可能含有业务信息。请确认后下载。";
const AUDIT_NOTICE = "该文件是本次定位的审计包，请按内部数据管理要求保存。";
const PUBLIC_CODES = new Set([
  "VALIDATION_ERROR", "CASE_NOT_FOUND", "JOB_NOT_FOUND", "JOB_CASE_MISMATCH", "ATTACHMENT_NOT_FOUND",
  "ARTIFACT_NOT_FOUND", "RESOURCE_NOT_FOUND", "INVALID_CASE_STATE", "ACTIVE_JOB_EXISTS", "NEW_CASE_REQUIRED",
  "REVISION_CONFLICT", "IDEMPOTENCY_CONFLICT", "RESOURCE_CASE_MISMATCH", "ATTACHMENT_NOT_READY", "UPLOAD_INCOMPLETE",
  "RESOURCE_HASH_MISMATCH", "RESOURCE_SIZE_MISMATCH", "RESOURCE_LIMIT_EXCEEDED", "PATH_VIOLATION", "CONTEXT_LIMIT",
  "ASSET_VERSION_UNAVAILABLE", "OUTCOME_MISSING", "OUTCOME_INVALID", "BACKEND_START_FAILED", "BACKEND_CANCELLED",
  "BACKEND_TIMEOUT", "BACKEND_OUTPUT_LIMIT", "BACKEND_EXIT_FAILED", "WORKSPACE_LIMIT", "WORKSPACE_PREPARE_FAILED",
  "RESOURCE_STAGE_FAILED", "EXECUTION_RECORD_FAILED", "LOGPARSE_FAILED", "LOGPARSE_OUTPUT_INVALID", "DISPATCH_REJECTED",
  "CLAIM_REJECTED", "INSTANCE_LOCKED", "STATE_CORRUPT", "STATE_SCHEMA_UNSUPPORTED", "STATE_WRITE_FAILED",
  "RESOURCE_PUBLISH_FAILED", "CONFIG_INVALID", "NO_CAPABILITY", "AGENT_EXECUTION_FAILED", "AGENT_INTERRUPTED",
  "AGENT_DISPATCH_INTERRUPTED", "AGENT_INTAKE_UNCERTAIN", "AGENT_NO_MATCHING_INPUT", "AGENT_UNAVAILABLE",
  "INTAKE_OUTPUT_INVALID", "INTAKE_INPUT_INVALID", "INTAKE_CONTEXT_LIMIT", "INTAKE_EXECUTION_FAILED",
  "INTAKE_ASSET_UNAVAILABLE", "AGENT_CONVERSATION_NOT_FOUND", "AGENT_CONVERSATION_CLOSED", "AGENT_IDEMPOTENCY_CONFLICT",
  "AGENT_ATTACHMENT_NOT_READY", "AGENT_MESSAGE_LIMIT", "AGENT_INVALID_CURSOR", "AGENT_ATTACHMENT_LIMIT",
  "AGENT_ATTACHMENT_NOT_FOUND", "AGENT_ATTACHMENT_STATE_CONFLICT", "UPLOAD_IN_PROGRESS", "CONVERSATION_CLOSED",
  "ATTACHMENT_CONVERSATION_MISMATCH", "RESOURCE_INVALID",
]);
const PUBLIC_PHASES = new Set(["AGENT", "CREATE_CASE", "INTAKE", "CASE_QUERY", "PREPARE_ATTACHMENT", "IMPORT_ATTACHMENT",
  "SUBMIT_SUPPLEMENT", "RESTART", "ASSET_RESOLUTION", "CONTEXT_BUILD", "WORKSPACE_PREPARE", "BACKEND_START",
  "BACKEND_EXECUTE", "TOOL_EXECUTE", "OUTCOME_VALIDATE", "RESOURCE_STAGE", "EXECUTION_RECORD", "ADMISSION",
  "CLAIM_COMMIT", "OUTCOME_COMMIT", "FAILURE_COMMIT", "ARCHIVE_STATUS_COMMIT", "DISPATCH", "WORKER_EXECUTION",
  "CLAIM_DELIVERY", "RESULT_DELIVERY", "FAILURE_RECORD", "DISPATCH_PAUSED"]);
const DIAGNOSTIC_ID = /^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|diag-[A-Za-z0-9_-]{1,128})$/;
const FAILURE_LOCATION = /^(?:inputs|input_names|input_values|initial_user_fact_names|initial_user_fact_values|problem_spec|attachment_ids|attachments|declared_size|declared_sha256|content_type|expected_case_revision|idempotency_key|methods_result|verification_result|findings|evidence|evidence_refs|matched_rules|rules)(?:(?:\.[a-z][a-z0-9_]{0,63})|(?:\[[0-9]{1,5}\])){0,8}$/;

type Json = Record<string, any>;
type Artifact = {
  artifact_id: string; kind: string; name: string; content_type: string;
  size: number; sha256: string; download_url: string;
};

function safeError(value: Json, terminal = false) {
  const code = typeof value?.code === "string" && PUBLIC_CODES.has(value.code) ? value.code : "WEBSITE_AGENT_ERROR";
  const details: Json[] = [];
  for (const item of Array.isArray(value?.details) ? value.details.slice(0, 32) : []) {
    if (item?.field === "phase" && PUBLIC_PHASES.has(item.actual)) details.push({ field: "phase", actual: item.actual });
    else if (item?.field === "diagnostic_id" && typeof item.actual === "string" && DIAGNOSTIC_ID.test(item.actual))
      details.push({ field: "diagnostic_id", actual: item.actual });
    else if (item?.field === "reason_code" && typeof item.actual === "string" && /^[A-Z][A-Z0-9_]{0,127}$/.test(item.actual))
      details.push({ field: "reason_code", actual: item.actual });
    else if (item?.field === "location" && typeof item.actual === "string" && item.actual.length <= 160 && FAILURE_LOCATION.test(item.actual))
      details.push({ field: "location", actual: item.actual });
    else if (item?.field === "persistence" && item.actual === "UNKNOWN") details.push({ field: "persistence", actual: "UNKNOWN" });
    else if (["case_id", "job_id", "runtime_epoch"].includes(item?.field) && typeof item.actual === "string" && UUID.test(item.actual))
      details.push({ field: item.field, actual: item.actual });
    else if (["cause_code", "secondary_error_code"].includes(item?.field) && PUBLIC_CODES.has(item.actual))
      details.push({ field: item.field, actual: item.actual });
    else if (item?.field === "occurred_at" && typeof item.actual === "string" &&
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(item.actual))
      details.push({ field: item.field, actual: item.actual });
  }
  const messages: Record<string, string> = {
    AGENT_CONVERSATION_CLOSED: "本次任务已经结束，请另建任务。",
    AGENT_IDEMPOTENCY_CONFLICT: "同一 request_id 的内容不能更改。",
    AGENT_ATTACHMENT_NOT_READY: "附件未上传完成或不属于本会话。",
    INTAKE_OUTPUT_INVALID: "补充信息整理失败，请核对输入后新建任务。",
    INTAKE_CONTEXT_LIMIT: "会话内容过长，请新建任务并精简输入。",
    AGENT_INTERRUPTED: "本次任务已中断，请重新发起。",
    DISPATCH_REJECTED: "定位服务暂时无法接收任务，请稍后重试。",
    STATE_WRITE_FAILED: "定位状态暂时无法确认，请稍后查询。",
  };
  const archiveUnknown = details.some((item) => item.field === "phase" && item.actual === "ARCHIVE_STATUS_COMMIT");
  const dispatchPaused = details.some((item) => item.field === "phase" && item.actual === "DISPATCH_PAUSED");
  return { code, message: archiveUnknown ? "报告已生成，但归档状态暂时无法确认。" :
    dispatchPaused ? "服务异常，已接收的任务暂时无法继续。" :
    messages[code] ?? (terminal ? "本次定位未能完成，请重新发起任务。" : "定位服务未能完成请求，请核对错误信息。"),
    details, retryable: !terminal && value?.retryable === true };
}

function json(response: ServerResponse, status: number, value: unknown) {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8", "Content-Length": body.length,
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
  });
  response.end(body);
}

async function jsonBody(request: IncomingMessage): Promise<Json> {
  if (request.headers["content-type"]?.split(";")[0] !== "application/json") {
    throw new HttpError(400, "请使用 application/json 提交请求。");
  }
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_JSON_BYTES) throw new HttpError(413, "请求内容过大。");
    chunks.push(Buffer.from(chunk));
  }
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (!value || Array.isArray(value) || typeof value !== "object") throw new Error();
    return value;
  } catch { throw new HttpError(400, "请求不是有效的 JSON 对象。"); }
}

async function boundedResponseJson(response: Response): Promise<Json> {
  if (!response.body) throw new HttpError(502, "定位服务返回了空响应。");
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of Readable.fromWeb(response.body as any)) {
    size += chunk.length;
    if (size > MAX_REPORT_BYTES) throw new HttpError(502, "定位服务响应超出接入限制。");
    chunks.push(Buffer.from(chunk));
  }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); }
  catch { throw new HttpError(502, "定位服务返回了无效响应。"); }
}

export function reportSections(report: Json) {
  const fields = ["schema_version", "format_id", "status", "source_job_type", "problem_statement",
    "root_cause", "findings", "causal_factors", "candidate_factors", "excluded_factors",
    "supporting_evidence_bindings", "completion_criteria_mapping", "verification_rules",
    "time_relevance", "evidence_gaps", "limitations", "recommendations", "safety_notes"];
  if (JSON.stringify(Object.keys(report).sort()) !== JSON.stringify(fields.sort()) ||
      report.schema_version !== 3 || report.format_id !== "problem-locator-diagnosis-v3" ||
      !["COMPLETED", "PARTIAL", "INCONCLUSIVE"].includes(report.status) ||
      (report.root_cause !== null && typeof report.root_cause !== "string")) {
    throw new HttpError(502, "定位报告格式不符合约定，请勿展示未校验内容。");
  }
  const arrayFields = ["findings", "causal_factors", "candidate_factors", "excluded_factors",
    "completion_criteria_mapping", "verification_rules", "supporting_evidence_bindings",
    "evidence_gaps", "limitations", "recommendations", "safety_notes"];
  if (arrayFields.some((field) => !Array.isArray(report[field])) ||
      typeof report.problem_statement !== "string" || !report.problem_statement.trim() ||
      !report.time_relevance || typeof report.time_relevance !== "object" ||
      (report.status === "COMPLETED" && !report.root_cause) ||
      (report.status === "INCONCLUSIVE" && report.root_cause !== null)) {
    throw new HttpError(502, "定位报告缺少必需内容。");
  }
  // 保留原字段和值，前端按字段渲染为文本；不要用 innerHTML 或执行报告里的指令。
  return [
    { title: "定位结论", value: report.root_cause },
    { title: "问题描述", value: report.problem_statement },
    { title: "关键发现", value: report.findings },
    { title: "原因与因素", value: { confirmed: report.causal_factors, candidates: report.candidate_factors, excluded: report.excluded_factors } },
    { title: "完成条件", value: report.completion_criteria_mapping },
    { title: "服务端验证", value: { rules: report.verification_rules, evidence: report.supporting_evidence_bindings } },
    { title: "时间相关性", value: report.time_relevance },
    { title: "证据缺口", value: report.evidence_gaps },
    { title: "限制", value: report.limitations },
    { title: "处置建议与安全说明", value: { recommendations: report.recommendations, safety: report.safety_notes } },
  ];
}

export function createAgentBackend(options: {
  upstream: string; access?: Access; fetchImpl?: typeof fetch;
}) {
  const access = options.access ?? denyAccess;
  const fetchImpl = options.fetchImpl ?? fetch;
  const base = new URL(options.upstream.endsWith("/") ? options.upstream : `${options.upstream}/`);
  if (!["http:", "https:"].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
    throw new Error("XIAODAO_BASE_URL 必须是不含凭据和查询参数的 HTTP(S) 地址。");
  }
  const upstreamUrl = (path: string) => new URL(path.replace(/^\//, ""), base);
  const upstreamFetch = (path: string, init?: RequestInit) => fetchImpl(upstreamUrl(path), { ...init, redirect: "manual" });
  const api = async (path: string, init?: RequestInit): Promise<Json> => {
    const response = await upstreamFetch(path, init);
    if (response.status >= 300 && response.status < 400) throw new HttpError(502, "定位服务返回了未允许的重定向。");
    const envelope = await boundedResponseJson(response);
    if (!response.ok || envelope.ok !== true || envelope.error !== null) {
      // 只返回受控公共错误，不转发 HTML、模型输出或堆栈。
      const error = safeError(envelope.error);
      throw new HttpError([400, 404, 409, 413, 422, 500, 503, 504].includes(response.status) ? response.status : 502,
        error.message, error.code, error.details, error.retryable);
    }
    return envelope.data;
  };

  async function artifacts(conversationId: string) {
    const conversation = await api(`/api/v1/agent/conversations/${conversationId}`);
    if (!conversation.case_id || !UUID.test(conversation.case_id)) throw new HttpError(409, "定位任务尚未生成报告。");
    const caseId = conversation.case_id as string;
    const state = await api(`/api/v1/cases/${caseId}`);
    const view = state.case_view;
    if (view?.case_id !== caseId || !Array.isArray(view.artifacts)) {
      throw new HttpError(502, "产物信息与会话不一致。");
    }
    const sourceJob = view.final_result?.proposed_by_job_id ?? view.unresolved_result?.source_job_id ??
      view.generic_result_v2?.source_job_id ?? view.generic_result?.source_job_id;
    const result: Artifact[] = [];
    const seen = new Set();
    // CaseView already projects the authoritative downloadable artifacts. One
    // snapshot keeps the result and its archive list at the same revision.
    for (const artifact of view.artifacts as Json[]) {
      if (!artifact || typeof artifact !== "object" || Array.isArray(artifact)) {
        throw new HttpError(502, "产物信息与会话不一致。");
      }
      if (!KINDS.has(artifact.kind)) continue;
      if (typeof artifact.artifact_id !== "string" || !UUID.test(artifact.artifact_id) || seen.has(artifact.artifact_id) ||
          artifact.downloadable !== true || artifact.resource_kind !== "FILE" ||
          typeof sourceJob !== "string" || !UUID.test(sourceJob) || artifact.created_by_job_id !== sourceJob ||
          !Number.isSafeInteger(artifact.size) || artifact.size < 0 || artifact.size > MAX_DOWNLOAD_BYTES ||
          typeof artifact.sha256 !== "string" || !SHA.test(artifact.sha256) ||
          typeof artifact.name !== "string" || !artifact.name.trim()) {
        throw new HttpError(502, "产物身份或校验信息不一致。");
      }
      const expectedType = { USER_RESULT: "application/json", USER_RESULT_ARCHIVE: "application/zip", AUDIT_BUNDLE: "application/zip", GENERIC_REPORT: "text/markdown" }[artifact.kind];
      const expectedId = artifact.kind === "USER_RESULT" ? view.unresolved_result?.user_result_artifact_id :
        artifact.kind === "AUDIT_BUNDLE" ? view.unresolved_result?.audit_artifact_id :
        artifact.kind === "GENERIC_REPORT" ? view.generic_result_v2?.report_artifact_id : undefined;
      if (artifact.content_type !== expectedType || (expectedId && expectedId !== artifact.artifact_id) ||
          (artifact.kind === "USER_RESULT" && artifact.name !== "diagnosis-result.json") ||
          (artifact.kind === "USER_RESULT_ARCHIVE" && artifact.name !== "result.zip")) {
        throw new HttpError(502, "报告种类或权威产物引用不一致。");
      }
      const expectedUrl = upstreamUrl(`/api/v1/artifacts/${artifact.artifact_id}/content`);
      expectedUrl.searchParams.set("case_id", caseId);
      seen.add(artifact.artifact_id);
      // 上游公布的 URL 没有寻址权；下载始终使用配置的内部地址和已核验的 ID。
      result.push({ artifact_id: artifact.artifact_id, kind: artifact.kind, name: artifact.name,
        content_type: artifact.content_type, size: artifact.size, sha256: artifact.sha256,
        download_url: expectedUrl.href });
    }
    return { view, artifacts: result };
  }

  async function downloadResponse(artifact: Artifact): Promise<Response> {
    const response = await fetchImpl(artifact.download_url, { redirect: "manual" });
    const contentLength = response.headers.get("content-length");
    const contentHash = response.headers.get("x-content-sha256");
    if (response.status !== 200 || !response.body ||
        (contentLength !== null && contentLength !== String(artifact.size)) ||
        (contentHash !== null && contentHash !== artifact.sha256) ||
        response.headers.get("content-type")?.split(";")[0] !== artifact.content_type ||
        ![null, "identity"].includes(response.headers.get("content-encoding"))) {
      await response.body?.cancel();
      throw new HttpError(502, "下载响应与产物信息不一致。");
    }
    return response;
  }

  async function verifiedReport(artifact: Artifact): Promise<Buffer> {
    if (artifact.size > MAX_REPORT_BYTES) throw new HttpError(502, "报告超出接入限制。");
    const response = await downloadResponse(artifact);
    // The exact size was bounded and checked before allocation. Never publish
    // partially received bytes or parse model text before actual-byte hashing.
    const content = Buffer.alloc(artifact.size);
    const hash = createHash("sha256");
    let size = 0;
    for await (const chunk of Readable.fromWeb(response.body as any)) {
      if (chunk.length > artifact.size - size) throw new HttpError(502, "下载内容大小不匹配。");
      content.set(chunk, size);
      hash.update(chunk);
      size += chunk.length;
    }
    if (size !== artifact.size || hash.digest("hex") !== artifact.sha256) {
      throw new HttpError(502, "下载内容的大小或 SHA-256 校验失败。");
    }
    return content;
  }

  async function verifiedDownload(artifact: Artifact, use: (path: string) => Promise<void>) {
    const response = await downloadResponse(artifact);
    const directory = await mkdtemp(join(tmpdir(), "xiaodao-website-"));
    const file = join(directory, "payload");
    try {
      const hash = createHash("sha256");
      let size = 0;
      const verifier = new Transform({ transform(chunk, _encoding, done) {
        size += chunk.length;
        if (size > artifact.size) return done(new HttpError(502, "下载内容大小不匹配。"));
        hash.update(chunk);
        done(null, chunk);
      }});
      await pipeline(Readable.fromWeb(response.body as any), verifier, createWriteStream(file, { flags: "wx", mode: 0o600 }));
      if (size !== artifact.size || hash.digest("hex") !== artifact.sha256) {
        throw new HttpError(502, "下载内容的大小或 SHA-256 校验失败。");
      }
      await use(file);
    } finally {
      await rm(file, { force: true });
      await rmdir(directory);
    }
  }

  return createServer(async (request, response) => {
    try {
      const user = await access.authenticate(request);
      if (!user) throw new HttpError(401, "请先登录。");
      const url = new URL(request.url ?? "/", "http://website.local");
      const creation = url.pathname === "/api/agent/conversations";
      const matched = url.pathname.match(/^\/api\/agent\/conversations\/([^/]+)(?:\/(messages|events|attachments|artifacts|report))?(?:\/([^/]+)\/content)?$/);
      const uploadMatch = url.pathname.match(/^\/api\/agent\/attachments\/([^/]+)\/content$/);
      const method = request.method ?? "GET";
      if (creation && method === "POST") {
        if (url.search) throw new HttpError(400, "此接口不接受查询参数。");
        const body = await jsonBody(request);
        // 由网站生成命名空间，防止不同用户选择相同 request_id 命中同一会话。
        if (typeof body.request_id !== "string" || !body.request_id.trim()) throw new HttpError(400, "request_id 不能为空。");
        const requestId = createHash("sha256").update(JSON.stringify([user.id, body.request_id])).digest("hex");
        const result = await api("/api/v1/agent/conversations", {
          method, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, request_id: requestId }),
        });
        await access.rememberConversation(user, result.conversation_id);
        json(response, 200, { ok: true, data: result, error: null });
        return;
      }
      if (uploadMatch && method === "PUT") {
        const attachmentId = uploadMatch[1];
        if (!UUID.test(attachmentId)) throw new HttpError(400, "附件标识无效。");
        if (!await access.ownsAttachment(user, attachmentId)) throw new HttpError(403, "无权上传此附件。");
        if (url.search) throw new HttpError(400, "此接口不接受查询参数。");
        const headers = new Headers();
        for (const key of ["idempotency-key", "content-type", "content-length", "x-content-sha256"]) {
          const value = request.headers[key];
          if (typeof value !== "string") throw new HttpError(400, "上传缺少必需请求头。");
          headers.set(key, value);
        }
        const size = Number(headers.get("content-length"));
        if (!Number.isSafeInteger(size) || size < 1 || size > MAX_ATTACHMENT_BYTES) throw new HttpError(413, "附件大小超出支持范围。");
        const result = await api(`/api/v1/agent/attachments/${attachmentId}/content`, {
          method, headers, body: request as any, duplex: "half",
        } as RequestInit);
        json(response, 200, { ok: true, data: result, error: null });
        return;
      }
      if (!matched || !UUID.test(matched[1])) throw new HttpError(404, "接口不存在。");
      const [, conversationId, action, artifactId] = matched;
      if (!await access.ownsConversation(user, conversationId)) throw new HttpError(403, "无权访问此会话。");
      if (artifactId && (action !== "artifacts" || !UUID.test(artifactId))) throw new HttpError(404, "接口不存在。");
      if (url.search && !(action === "artifacts" && artifactId)) throw new HttpError(400, "此接口不接受查询参数。");

      if (action === "events" && method === "GET" && !artifactId) {
        const headers = new Headers({ Accept: "text/event-stream" });
        const cursor = request.headers["last-event-id"];
        if (cursor !== undefined) {
          if (typeof cursor !== "string" || !/^(0|[1-9][0-9]{0,18})$/.test(cursor)) throw new HttpError(400, "事件游标无效。");
          headers.set("Last-Event-ID", cursor);
        }
        const controller = new AbortController();
        // 仅关闭本次上游订阅，不调用取消或删除定位任务的接口。
        response.once("close", () => controller.abort());
        const upstream = await upstreamFetch(`/api/v1/agent/conversations/${conversationId}/events`, { headers, signal: controller.signal });
        if (!upstream.ok) {
          const envelope = await boundedResponseJson(upstream);
          const error = safeError(envelope.error);
          throw new HttpError([400, 404, 409, 413, 422, 500, 503, 504].includes(upstream.status) ? upstream.status : 502,
            error.message, error.code, error.details, error.retryable);
        }
        if (!upstream.body || !upstream.headers.get("content-type")?.startsWith("text/event-stream")) {
          await upstream.body?.cancel();
          throw new HttpError(502, "暂时无法订阅会话事件。");
        }
        response.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no" });
        await pipeline(Readable.fromWeb(upstream.body as any), response);
        return;
      }
      if ((action === "artifacts" || action === "report") && method === "GET") {
        const current = await artifacts(conversationId);
        const websiteUrl = (id: string) => `/api/agent/conversations/${conversationId}/artifacts/${id}/content`;
        if (action === "artifacts" && !artifactId) {
          json(response, 200, { ok: true, data: { artifacts: current.artifacts.map((item) => ({ ...item,
            download_url: websiteUrl(item.artifact_id),
            download_notice: item.kind === "USER_RESULT_ARCHIVE" ? ZIP_NOTICE : item.kind === "AUDIT_BUNDLE" ? AUDIT_NOTICE : null,
          })) }, error: null });
          return;
        }
        if (action === "report") {
          const reports = current.artifacts.filter((item) => ["USER_RESULT", "GENERIC_REPORT"].includes(item.kind));
          if (!reports.length && current.view.generic_result && !current.view.generic_result_v2) {
            const legacy = current.view.generic_result;
            if (typeof legacy.conclusion !== "string" || typeof legacy.root_cause_analysis !== "string") {
              throw new HttpError(502, "通用诊断结果缺少必需内容。");
            }
            json(response, 200, { ok: true, data: { format: "generic-v1", report: legacy }, error: null });
            return;
          }
          if (reports.length !== 1) throw new HttpError(409, "报告尚未就绪，或产物列表不完整。");
          const report = reports[0];
          const text = (await verifiedReport(report)).toString("utf8");
          if (report.kind === "GENERIC_REPORT") {
            json(response, 200, { ok: true, data: { format: "markdown", markdown: text }, error: null });
          } else {
            let payload: Json;
            try { payload = JSON.parse(text); } catch { throw new HttpError(502, "报告 JSON 无效。"); }
            const expected = { RESOLVED: "COMPLETED", PARTIALLY_RESOLVED: "PARTIAL", UNRESOLVED: "INCONCLUSIVE" }[current.view.status as string];
            if (!expected || payload.status !== expected) throw new HttpError(502, "报告状态与任务不一致。");
            json(response, 200, { ok: true, data: { format: "problem-locator-diagnosis-v3", report: payload, sections: reportSections(payload) }, error: null });
          }
          return;
        }
        const artifact = current.artifacts.find((item) => item.artifact_id === artifactId);
        if (!artifact) throw new HttpError(404, "产物不存在。");
        if (["USER_RESULT_ARCHIVE", "AUDIT_BUNDLE"].includes(artifact.kind)) {
          const expected = artifact.kind === "USER_RESULT_ARCHIVE" ? "archive" : "audit";
          const keys = [...url.searchParams.keys()];
          const allowed = expected === "archive" ? ["download", "acknowledge_raw_logs"] : ["download"];
          if (new Set(keys).size !== keys.length || keys.some((key) => !allowed.includes(key))) {
            throw new HttpError(400, "下载参数无效。");
          }
          if (url.searchParams.get("download") !== expected ||
              (expected === "archive" && url.searchParams.get("acknowledge_raw_logs") !== "true")) {
            throw new HttpError(409, expected === "archive" ? ZIP_NOTICE : "请先确认需要下载审计包。");
          }
        } else if (url.search) throw new HttpError(400, "此报告不接受下载参数。");
        const downloadHeaders = { "Content-Type": artifact.content_type, "Content-Length": artifact.size,
            "X-Content-SHA256": artifact.sha256, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
            "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(artifact.name)}` };
        if (["USER_RESULT", "GENERIC_REPORT"].includes(artifact.kind) && artifact.size <= MAX_REPORT_BYTES) {
          const content = await verifiedReport(artifact);
          response.writeHead(200, downloadHeaders);
          response.end(content);
        } else {
          await verifiedDownload(artifact, async (path) => {
            response.writeHead(200, downloadHeaders);
            await pipeline(createReadStream(path), response);
          });
        }
        return;
      }
      const allowed = (!action && method === "GET") || (["messages", "attachments"].includes(action) && method === "POST" && !artifactId);
      if (!allowed) throw new HttpError(404, "接口不存在。");
      const suffix = action ? `/${action}` : "";
      const body = method === "POST" ? await jsonBody(request) : undefined;
      const result = await api(`/api/v1/agent/conversations/${conversationId}${suffix}`, body ? {
        method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      } : undefined);
      if (!action && result.failure) result.failure = safeError(result.failure, true);
      if (action === "attachments") {
        const attachmentId = result.attachment?.attachment_id;
        if (!UUID.test(attachmentId)) throw new HttpError(502, "定位服务返回的附件标识无效。");
        await access.rememberAttachment(user, conversationId, attachmentId);
        result.upload.url = `/api/agent/attachments/${attachmentId}/content`;
      }
      json(response, 200, { ok: true, data: result, error: null });
    } catch (error) {
      if (response.headersSent || response.destroyed) { response.destroy(); return; }
      const publicError = error instanceof HttpError ? error : new HttpError(502, "网站暂时无法连接定位服务。");
      json(response, publicError.status, { ok: false, data: null, error: { code: publicError.code, message: publicError.message,
        details: publicError.details, retryable: publicError.retryable } });
    }
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const authModule = process.env.WEBSITE_AUTH_MODULE;
  const access = authModule ? (await import(pathToFileURL(resolve(authModule)).href)).access : denyAccess;
  const server = createAgentBackend({ upstream: process.env.XIAODAO_BASE_URL ?? "http://127.0.0.1:8000", access });
  const port = Number(process.env.PORT ?? "8787");
  server.listen(port, "127.0.0.1", () => {
    console.log(`网站 Agent 接入示例已启动：http://127.0.0.1:${port}`);
    if (!authModule) console.log("尚未配置 WEBSITE_AUTH_MODULE，所有业务请求默认拒绝。");
  });
}
