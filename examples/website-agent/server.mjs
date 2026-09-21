/** 网站后端的唯一实现；Node.js 18+ 可直接导入，无需 npm 依赖。
 * @typedef {{ id: string }} User
 * @typedef {{ authenticate(request: import('node:http').IncomingMessage): Promise<User|null> }} Access
 * authenticate 应验证登录态和 CSRF / Origin；身份来自网站服务端，不接受前端自报 user_id。
 */
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { lstat, mkdtemp, readFile, readdir, rm, rmdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import { clearInterval, clearTimeout, setInterval, setTimeout } from "node:timers";
import { fileURLToPath, pathToFileURL } from "node:url";
export const denyAccess = {
    authenticate: async ()=>null
};
export class HttpError extends Error {
    status;
    code;
    details;
    retryable;
    constructor(status, message, code = "WEBSITE_AGENT_ERROR", details = [], retryable = false){
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
export const DOWNLOAD_TOTAL_TIMEOUT_MS = 30 * 60 * 1000;
export const DOWNLOAD_TEMP_TTL_MS = 60 * 60 * 1000;
const DOWNLOAD_SWEEP_INTERVAL_MS = 10 * 60 * 1000;
const DOWNLOAD_DIRECTORY = /^xiaodao-website-p([1-9][0-9]*)-[A-Za-z0-9_-]+$/;
const activeDownloads = new Set();

async function processIdentity(pid) {
    if (process.platform !== "linux") return null;
    try {
        const [stat, boot] = await Promise.all([
            readFile(`/proc/${pid}/stat`, "utf8"),
            readFile("/proc/sys/kernel/random/boot_id", "utf8"),
        ]);
        const start = stat.slice(stat.lastIndexOf(")") + 2).trim().split(/\s+/)[19];
        return /^[0-9]+$/.test(start ?? "") ? `${boot.trim()}:${start}` : null;
    } catch {
        return null;
    }
}

function processAlive(pid) {
    try { process.kill(pid, 0); return true; }
    catch (error) { return error.code !== "ESRCH"; }
}

function reportSpoolCleanupFailure(error) {
    console.error("网站下载临时目录未能清理，将在下一轮重试。", error?.code ?? "CLEANUP_FAILED");
}

async function removeDownloadDirectory(directory) {
    await rm(join(directory, "payload"), { force: true });
    await rm(join(directory, "owner.json"), { force: true });
    try { await rmdir(directory); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
}

/** 仅回收已过期且可以确认不再使用的本示例临时目录。 */
export async function sweepDownloadSpools({ root = tmpdir(), now = Date.now() } = {}) {
    for (const entry of await readdir(root, { withFileTypes: true })) {
        const match = DOWNLOAD_DIRECTORY.exec(entry.name);
        if (!match || !entry.isDirectory()) continue;
        const pid = Number(match[1]);
        if (!Number.isSafeInteger(pid) || pid > 2_147_483_647) continue;
        const directory = join(root, entry.name);
        if (activeDownloads.has(directory)) continue;
        try {
            const metadata = await lstat(directory);
            if (!metadata.isDirectory() || now - metadata.mtimeMs < DOWNLOAD_TEMP_TTL_MS) continue;
            let owner = null;
            try {
                const ownerPath = join(directory, "owner.json"), ownerMetadata = await lstat(ownerPath);
                if (!ownerMetadata.isFile() || ownerMetadata.size > 1024) continue;
                owner = JSON.parse(await readFile(ownerPath, "utf8"));
                if (owner.schema_version !== 1 || owner.pid !== pid ||
                    !(owner.process_identity === null || typeof owner.process_identity === "string")) continue;
            } catch (error) {
                if (error.code !== "ENOENT") throw error;
            }
            // 同进程中只有不在 activeDownloads 的目录才可重试清理。
            // 其他进程仍存活时，Linux 的启动标识可排除 PID 被复用的旧目录。
            let unused = pid === process.pid || !processAlive(pid);
            if (!unused && owner?.process_identity) {
                const currentIdentity = await processIdentity(pid);
                unused = currentIdentity !== null && currentIdentity !== owner.process_identity;
            }
            if (unused && !activeDownloads.has(directory)) await removeDownloadDirectory(directory);
        } catch (error) {
            if (error.code !== "ENOENT") reportSpoolCleanupFailure(error);
        }
    }
}
// JSON 字符串转义最多膨胀六倍。此处限制传输封装，原始报告仍由原生 API 单独限额。
const MAX_REPORT_RESPONSE_BYTES = 6 * MAX_REPORT_BYTES + 64 * 1024;
const KINDS = new Set([
    "USER_RESULT",
    "USER_RESULT_ARCHIVE",
    "AUDIT_BUNDLE",
    "GENERIC_REPORT"
]);
const ZIP_NOTICE = "该文件包含原始目标日志，可能含有业务信息。请确认后下载。";
const AUDIT_NOTICE = "该文件是本次定位的审计包，请按内部数据管理要求保存。";
const PUBLIC_CODES = new Set([
    "VALIDATION_ERROR",
    "CASE_NOT_FOUND",
    "JOB_NOT_FOUND",
    "JOB_CASE_MISMATCH",
    "ATTACHMENT_NOT_FOUND",
    "ARTIFACT_NOT_FOUND",
    "RESOURCE_NOT_FOUND",
    "INVALID_CASE_STATE",
    "ACTIVE_JOB_EXISTS",
    "NEW_CASE_REQUIRED",
    "REVISION_CONFLICT",
    "IDEMPOTENCY_CONFLICT",
    "RESOURCE_CASE_MISMATCH",
    "ATTACHMENT_NOT_READY",
    "UPLOAD_INCOMPLETE",
    "RESOURCE_HASH_MISMATCH",
    "RESOURCE_SIZE_MISMATCH",
    "RESOURCE_LIMIT_EXCEEDED",
    "PATH_VIOLATION",
    "CONTEXT_LIMIT",
    "ASSET_VERSION_UNAVAILABLE",
    "OUTCOME_MISSING",
    "OUTCOME_INVALID",
    "BACKEND_START_FAILED",
    "BACKEND_CANCELLED",
    "BACKEND_TIMEOUT",
    "BACKEND_OUTPUT_LIMIT",
    "BACKEND_EXIT_FAILED",
    "WORKSPACE_LIMIT",
    "WORKSPACE_PREPARE_FAILED",
    "RESOURCE_STAGE_FAILED",
    "EXECUTION_RECORD_FAILED",
    "LOGPARSE_FAILED",
    "LOGPARSE_OUTPUT_INVALID",
    "DISPATCH_REJECTED",
    "CLAIM_REJECTED",
    "INSTANCE_LOCKED",
    "STATE_CORRUPT",
    "STATE_SCHEMA_UNSUPPORTED",
    "STATE_WRITE_FAILED",
    "RESOURCE_PUBLISH_FAILED",
    "CONFIG_INVALID",
    "NO_CAPABILITY",
    "AGENT_EXECUTION_FAILED",
    "AGENT_INTERRUPTED",
    "AGENT_DISPATCH_INTERRUPTED",
    "AGENT_INTAKE_UNCERTAIN",
    "AGENT_NO_MATCHING_INPUT",
    "AGENT_UNAVAILABLE",
    "INTAKE_OUTPUT_INVALID",
    "INTAKE_INPUT_INVALID",
    "INTAKE_CONTEXT_LIMIT",
    "INTAKE_EXECUTION_FAILED",
    "INTAKE_ASSET_UNAVAILABLE",
    "AGENT_CONVERSATION_NOT_FOUND",
    "AGENT_CONVERSATION_CLOSED",
    "AGENT_IDEMPOTENCY_CONFLICT",
    "AGENT_ATTACHMENT_NOT_READY",
    "AGENT_MESSAGE_LIMIT",
    "AGENT_INVALID_CURSOR",
    "AGENT_EVENT_CURSOR_EXPIRED",
    "AGENT_ATTACHMENT_LIMIT",
    "AGENT_ATTACHMENT_NOT_FOUND",
    "AGENT_ATTACHMENT_STATE_CONFLICT",
    "UPLOAD_IN_PROGRESS",
    "CONVERSATION_CLOSED",
    "ATTACHMENT_CONVERSATION_MISMATCH",
    "RESOURCE_INVALID",
    "AGENT_RUN_NOT_FOUND",
    "AGENT_RUN_CHANGED",
    "AGENT_DELETE_REQUIRED",
    "AGENT_CONVERSATION_DELETED",
    "AGENT_CANCELLING"
]);
const PUBLIC_PHASES = new Set([
    "AGENT",
    "CREATE_CASE",
    "INTAKE",
    "CASE_QUERY",
    "PREPARE_ATTACHMENT",
    "IMPORT_ATTACHMENT",
    "SUBMIT_SUPPLEMENT",
    "RESTART",
    "ASSET_RESOLUTION",
    "CONTEXT_BUILD",
    "WORKSPACE_PREPARE",
    "BACKEND_START",
    "BACKEND_EXECUTE",
    "TOOL_EXECUTE",
    "OUTCOME_VALIDATE",
    "RESOURCE_STAGE",
    "EXECUTION_RECORD",
    "ADMISSION",
    "CLAIM_COMMIT",
    "OUTCOME_COMMIT",
    "FAILURE_COMMIT",
    "ARCHIVE_STATUS_COMMIT",
    "DISPATCH",
    "WORKER_EXECUTION",
    "CLAIM_DELIVERY",
    "RESULT_DELIVERY",
    "FAILURE_RECORD",
    "DISPATCH_PAUSED"
]);
const DIAGNOSTIC_ID = /^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|diag-[A-Za-z0-9_-]{1,128})$/;
const FAILURE_LOCATION = /^(?:inputs|input_names|input_values|initial_user_fact_names|initial_user_fact_values|problem_spec|attachment_ids|attachments|declared_size|declared_sha256|content_type|expected_case_revision|idempotency_key|methods_result|verification_result|findings|evidence|evidence_refs|matched_rules|rules)(?:(?:\.[a-z][a-z0-9_]{0,63})|(?:\[[0-9]{1,5}\])){0,8}$/;
function safeError(value, terminal = false) {
    const code = typeof value?.code === "string" && PUBLIC_CODES.has(value.code) ? value.code : "WEBSITE_AGENT_ERROR";
    const details = [];
    for (const item of Array.isArray(value?.details) ? value.details.slice(0, 32) : []){
        if (item?.field === "phase" && PUBLIC_PHASES.has(item.actual)) details.push({
            field: "phase",
            actual: item.actual
        });
        else if (item?.field === "diagnostic_id" && typeof item.actual === "string" && DIAGNOSTIC_ID.test(item.actual)) details.push({
            field: "diagnostic_id",
            actual: item.actual
        });
        else if (item?.field === "reason_code" && typeof item.actual === "string" && /^[A-Z][A-Z0-9_]{0,127}$/.test(item.actual)) details.push({
            field: "reason_code",
            actual: item.actual
        });
        else if (item?.field === "location" && typeof item.actual === "string" && item.actual.length <= 160 && FAILURE_LOCATION.test(item.actual)) details.push({
            field: "location",
            actual: item.actual
        });
        else if (item?.field === "persistence" && item.actual === "UNKNOWN") details.push({
            field: "persistence",
            actual: "UNKNOWN"
        });
        else if (item?.field === "retained_after_sequence" && Number.isSafeInteger(item.actual) && item.actual >= 0) details.push({
            field: "retained_after_sequence",
            actual: item.actual
        });
        else if ([
            "case_id",
            "job_id",
            "runtime_epoch"
        ].includes(item?.field) && typeof item.actual === "string" && UUID.test(item.actual)) details.push({
            field: item.field,
            actual: item.actual
        });
        else if ([
            "cause_code",
            "secondary_error_code"
        ].includes(item?.field) && PUBLIC_CODES.has(item.actual)) details.push({
            field: item.field,
            actual: item.actual
        });
        else if (item?.field === "occurred_at" && typeof item.actual === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(item.actual)) details.push({
            field: item.field,
            actual: item.actual
        });
    }
    const messages = {
        AGENT_CONVERSATION_CLOSED: "本轮诊断已结束，可以发送新问题开始下一轮。",
        AGENT_CONVERSATION_NOT_FOUND: "会话不存在或已删除。",
        AGENT_RUN_NOT_FOUND: "诊断轮次不存在。",
        AGENT_RUN_CHANGED: "诊断轮次已变化，请刷新后重试。",
        AGENT_EVENT_CURSOR_EXPIRED: "历史事件已过保留期，请先刷新会话状态，再从 last_event_id 重新订阅。",
        AGENT_DELETE_REQUIRED: "会话正在删除，请稍后查看目录。",
        AGENT_CANCELLING: "正在停止本轮诊断，请稍后再发送。",
        AGENT_IDEMPOTENCY_CONFLICT: "同一 request_id 的内容不能更改。",
        AGENT_ATTACHMENT_NOT_READY: "附件未上传完成或不属于本会话。",
        INTAKE_OUTPUT_INVALID: "补充信息整理失败，请核对输入后新建任务。",
        INTAKE_CONTEXT_LIMIT: "会话内容过长，请新建任务并精简输入。",
        AGENT_INTERRUPTED: "本次任务已中断，请重新发起。",
        DISPATCH_REJECTED: "定位服务暂时无法接收任务，请稍后重试。",
        STATE_WRITE_FAILED: "定位状态暂时无法确认，请稍后查询。"
    };
    const archiveUnknown = details.some((item)=>item.field === "phase" && item.actual === "ARCHIVE_STATUS_COMMIT");
    const dispatchPaused = details.some((item)=>item.field === "phase" && item.actual === "DISPATCH_PAUSED");
    return {
        code,
        message: archiveUnknown ? "报告已生成，但归档状态暂时无法确认。" : dispatchPaused ? "服务异常，已接收的任务暂时无法继续。" : messages[code] ?? (terminal ? "本次定位未能完成，请重新发起任务。" : "定位服务未能完成请求，请核对错误信息。"),
        details,
        retryable: !terminal && value?.retryable === true
    };
}
function json(response, status, value) {
    const body = Buffer.from(JSON.stringify(value));
    response.writeHead(status, {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": body.length,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"
    });
    response.end(body);
}
async function jsonBody(request) {
    if (request.headers["content-type"]?.split(";")[0] !== "application/json") {
        throw new HttpError(400, "请使用 application/json 提交请求。");
    }
    let size = 0;
    const chunks = [];
    for await (const chunk of request){
        size += chunk.length;
        if (size > MAX_JSON_BYTES) throw new HttpError(413, "请求内容过大。");
        chunks.push(Buffer.from(chunk));
    }
    try {
        const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (!value || Array.isArray(value) || typeof value !== "object") throw new Error();
        return value;
    } catch  {
        throw new HttpError(400, "请求不是有效的 JSON 对象。");
    }
}
async function boundedResponseJson(response, maxBytes = MAX_REPORT_BYTES) {
    if (!response.body) throw new HttpError(502, "定位服务返回了空响应。");
    let size = 0;
    const chunks = [];
    for await (const chunk of Readable.fromWeb(response.body)){
        size += chunk.length;
        if (size > maxBytes) throw new HttpError(502, "定位服务响应超出接入限制。");
        chunks.push(Buffer.from(chunk));
    }
    try {
        return JSON.parse(Buffer.concat(chunks).toString("utf8"));
    } catch  {
        throw new HttpError(502, "定位服务返回了无效响应。");
    }
}
const INCLUDES = [
    "history",
    "report",
    "artifacts"
];
function includesFromUrl(url) {
    validateQuery(url, [
        "include",
        "run_id",
        "history_before",
        "history_limit"
    ]);
    if (url.searchParams.has("run_id") && !UUID.test(url.searchParams.get("run_id"))) throw new HttpError(400, "轮次标识无效。");
    checkLimit(url.searchParams.get("history_limit"));
    const value = url.searchParams.get("include");
    if (value === null) return [
        ...INCLUDES
    ];
    if (value === "none") return [];
    const selected = value.split(",");
    if (new Set(selected).size !== selected.length || selected.some((item)=>!INCLUDES.includes(item))) {
        throw new HttpError(400, "include 仅支持 history、report、artifacts，或单独使用 none。");
    }
    return INCLUDES.filter((item)=>selected.includes(item));
}
function validateQuery(url, allowed) {
    const keys = [
        ...url.searchParams.keys()
    ];
    if (new Set(keys).size !== keys.length || keys.some((key)=>!allowed.includes(key) || !url.searchParams.get(key))) throw new HttpError(400, "查询参数无效或重复。");
}
function checkLimit(value) {
    if (value !== null && (!/^[1-9][0-9]{0,2}$/.test(value) || Number(value) > 100)) throw new HttpError(400, "分页条数必须是 1 到 100 的整数。");
}
function validateResult(result, conversation) {
    if (!result || result.schema_version !== 1 || result.conversation_id !== conversation.conversation_id || result.case_id !== conversation.case_id || result.report_state !== conversation.report_state || result.source_job_id !== conversation.source_job_id) {
        throw new HttpError(502, "报告与会话记录不一致。");
    }
    if (result.report_state !== "READY") {
        if (result.format !== null || result.report !== null || result.markdown !== null || result.artifact !== null) {
            throw new HttpError(502, "报告尚不可用，但响应中包含了报告内容。");
        }
    } else if (result.format === "problem-locator-diagnosis-v3") {
        const expected = {
            RESOLVED: "COMPLETED",
            PARTIALLY_RESOLVED: "PARTIAL",
            UNRESOLVED: "INCONCLUSIVE"
        }[conversation.case_status];
        if (!result.report || typeof result.report !== "object" || Array.isArray(result.report) || result.markdown !== null || result.report.schema_version !== 3 || result.report.format_id !== result.format || result.report.status !== expected) {
            throw new HttpError(502, "定位报告格式或状态与任务不一致。");
        }
    } else if (result.format === "markdown") {
        if (typeof result.markdown !== "string" || result.report !== null) throw new HttpError(502, "Markdown 诊断报告缺少必需内容。");
    } else if (result.format === "generic-v1") {
        if (typeof result.report?.conclusion !== "string" || typeof result.report?.root_cause_analysis !== "string" || result.markdown !== null) throw new HttpError(502, "通用诊断结果缺少必需内容。");
    } else throw new HttpError(502, "定位报告格式不符合约定。");
    if (result.failure) result.failure = safeError(result.failure, true);
}
/** @param {{upstream: string, access?: Access, ownerNamespace?: string, fetchImpl?: typeof fetch}} options */
export function createAgentBackend(options) {
    const access = options.access ?? denyAccess;
    const fetchImpl = options.fetchImpl ?? fetch;
    const base = new URL(options.upstream.endsWith("/") ? options.upstream : `${options.upstream}/`);
    if (![
        "http:",
        "https:"
    ].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
        throw new Error("XIAODAO_BASE_URL 必须是不含凭据和查询参数的 HTTP(S) 地址。");
    }
    const upstreamUrl = (path)=>new URL(path.replace(/^\//, ""), base);
    const ownerNamespace = options.ownerNamespace ?? "xiaodao-website";
    if (!ownerNamespace.trim()) throw new Error("网站归属命名空间不能为空。");
    const upstreamFetch = (ownerKey, path, init)=>{
        const headers = new Headers(init?.headers);
        headers.set("X-Agent-Owner-Key", ownerKey);
        return fetchImpl(upstreamUrl(path), {
            ...init,
            headers,
            redirect: "manual"
        });
    };
    const api = async (ownerKey, path, init, maxBytes = MAX_REPORT_BYTES)=>{
        const response = await upstreamFetch(ownerKey, path, init);
        if (response.status >= 300 && response.status < 400) throw new HttpError(502, "定位服务返回了未允许的重定向。");
        const envelope = await boundedResponseJson(response, maxBytes);
        if (!response.ok || envelope.ok !== true || envelope.error !== null) {
            const error = safeError(envelope.error);
            throw new HttpError([
                400,
                404,
                409,
                413,
                422,
                500,
                503,
                504
            ].includes(response.status) ? response.status : 502, error.message, error.code, error.details, error.retryable);
        }
        return envelope.data;
    };
    function validatedArtifacts(conversation) {
        const caseId = conversation.case_id, sourceJob = conversation.source_job_id;
        const result = [];
        const seen = new Set();
        for (const artifact of conversation.artifacts){
            if (!artifact || typeof artifact !== "object" || Array.isArray(artifact)) {
                throw new HttpError(502, "产物信息与会话不一致。");
            }
            if (!KINDS.has(artifact.kind) || typeof caseId !== "string" || !UUID.test(caseId) || typeof artifact.artifact_id !== "string" || !UUID.test(artifact.artifact_id) || seen.has(artifact.artifact_id) || artifact.downloadable !== true || artifact.resource_kind !== "FILE" || typeof sourceJob !== "string" || !UUID.test(sourceJob) || artifact.created_by_job_id !== sourceJob || !Number.isSafeInteger(artifact.size) || artifact.size < 0 || artifact.size > MAX_DOWNLOAD_BYTES || typeof artifact.sha256 !== "string" || !SHA.test(artifact.sha256) || typeof artifact.name !== "string" || !artifact.name.trim()) {
                throw new HttpError(502, "产物身份或校验信息不一致。");
            }
            const expectedType = {
                USER_RESULT: "application/json",
                USER_RESULT_ARCHIVE: "application/zip",
                AUDIT_BUNDLE: "application/zip",
                GENERIC_REPORT: "text/markdown"
            }[artifact.kind];
            if (artifact.content_type !== expectedType || artifact.kind === "USER_RESULT" && artifact.name !== "diagnosis-result.json" || artifact.kind === "USER_RESULT_ARCHIVE" && artifact.name !== "result.zip") {
                throw new HttpError(502, "报告种类或权威产物引用不一致。");
            }
            seen.add(artifact.artifact_id);
            result.push({
                artifact_id: artifact.artifact_id,
                kind: artifact.kind,
                name: artifact.name,
                content_type: artifact.content_type,
                size: artifact.size,
                sha256: artifact.sha256,
                resource_kind: artifact.resource_kind,
                created_by_job_id: artifact.created_by_job_id,
                created_at: artifact.created_at,
                downloadable: true,
                download_url: `/api/agent/conversations/${conversation.conversation_id}/files/${artifact.artifact_id}/content?run_id=${conversation.selected_run_id}`,
                download_notice: artifact.kind === "USER_RESULT_ARCHIVE" ? ZIP_NOTICE : artifact.kind === "AUDIT_BUNDLE" ? AUDIT_NOTICE : null
            });
        }
        return result;
    }
    async function conversationDetail(ownerKey, conversationId, included, query = "") {
        const limit = included.includes("report") ? MAX_REPORT_RESPONSE_BYTES + (included.includes("history") ? MAX_REPORT_BYTES : 0) : MAX_REPORT_BYTES;
        const result = await api(ownerKey, `/api/v1/agent/conversations/${conversationId}${query}`, undefined, limit);
        const requestedRun = new URLSearchParams(query).get("run_id");
        if (!result || result.schema_version !== 3 || result.conversation_id !== conversationId || !UUID.test(result.selected_run_id) || !UUID.test(result.current_run?.run_id) || !result.capabilities || result.selected_run_id !== (requestedRun ?? result.current_run.run_id) || ![
            "PENDING",
            "READY",
            "UNAVAILABLE"
        ].includes(result.report_state) || JSON.stringify(result.included) !== JSON.stringify(included) || (included.includes("history") ? !Array.isArray(result.history) || !Array.isArray(result.attachments) : result.history !== null || result.attachments !== null) || (included.includes("artifacts") ? !Array.isArray(result.artifacts) : result.artifacts !== null) || !included.includes("report") && result.result !== null) {
            throw new HttpError(502, "定位服务返回的会话详情不符合约定。");
        }
        if (included.includes("report")) validateResult(result.result, result);
        if (included.includes("artifacts")) result.artifacts = validatedArtifacts(result);
        if (included.includes("history")) for (const entry of result.history){
            if (entry?.result?.failure) entry.result.failure = safeError(entry.result.failure, true);
        }
        if (result.failure) result.failure = safeError(result.failure, true);
        return result;
    }
    async function downloadResponse(ownerKey, artifact, signal) {
        const response = await fetchImpl(artifact.download_url, {
            redirect: "manual",
            signal,
            headers: {
                "X-Agent-Owner-Key": ownerKey
            }
        });
        const contentLength = response.headers.get("content-length");
        const contentHash = response.headers.get("x-content-sha256");
        if (response.status !== 200 || !response.body || contentLength !== null && contentLength !== String(artifact.size) || contentHash !== null && contentHash !== artifact.sha256 || response.headers.get("content-type")?.split(";")[0] !== artifact.content_type || ![
            null,
            "identity"
        ].includes(response.headers.get("content-encoding"))) {
            await response.body?.cancel();
            throw new HttpError(502, "下载响应与产物信息不一致。");
        }
        return response;
    }
    async function verifiedReport(ownerKey, artifact) {
        if (artifact.size > MAX_REPORT_BYTES) throw new HttpError(502, "报告超出接入限制。");
        const response = await downloadResponse(ownerKey, artifact);
        const content = Buffer.alloc(artifact.size);
        const hash = createHash("sha256");
        let size = 0;
        for await (const chunk of Readable.fromWeb(response.body)){
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
    async function verifiedDownload(ownerKey, artifact, clientResponse, use) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(new HttpError(504, "下载超时，请重新下载。")), DOWNLOAD_TOTAL_TIMEOUT_MS);
        timeout.unref();
        const disconnected = () => {
            if (!clientResponse.writableFinished) controller.abort(new HttpError(499, "下载连接已断开。"));
        };
        clientResponse.once("close", disconnected);
        let directory;
        try {
            const response = await downloadResponse(ownerKey, artifact, controller.signal);
            directory = await mkdtemp(join(tmpdir(), `xiaodao-website-p${process.pid}-`));
            activeDownloads.add(directory);
            const file = join(directory, "payload");
            await writeFile(join(directory, "owner.json"), JSON.stringify({
                schema_version: 1, pid: process.pid, process_identity: await processIdentity(process.pid),
                created_at: new Date().toISOString(),
            }), { flag: "wx", mode: 0o600 });
            const hash = createHash("sha256");
            let size = 0;
            const verifier = new Transform({
                transform (chunk, _encoding, done) {
                    size += chunk.length;
                    if (size > artifact.size) return done(new HttpError(502, "下载内容大小不匹配。"));
                    hash.update(chunk);
                    done(null, chunk);
                }
            });
            await pipeline(Readable.fromWeb(response.body), verifier, createWriteStream(file, {
                flags: "wx",
                mode: 0o600
            }), { signal: controller.signal });
            if (size !== artifact.size || hash.digest("hex") !== artifact.sha256) {
                throw new HttpError(502, "下载内容的大小或 SHA-256 校验失败。");
            }
            await use(file, controller.signal);
        } catch (error) {
            throw controller.signal.aborted ? controller.signal.reason : error;
        } finally{
            clearTimeout(timeout);
            clientResponse.removeListener("close", disconnected);
            if (directory) {
                activeDownloads.delete(directory);
                try { await removeDownloadDirectory(directory); }
                catch (error) { reportSpoolCleanupFailure(error); }
            }
        }
    }
    const server = createServer(async (request, response)=>{
        try {
            const user = await access.authenticate(request);
            if (!user || typeof user.id !== "string" || !user.id.trim()) throw new HttpError(401, "请先登录。");
            const ownerKey = createHash("sha256").update(JSON.stringify([
                ownerNamespace,
                user.id
            ])).digest("hex");
            const url = new URL(request.url ?? "/", "http://website.local");
            const creation = url.pathname === "/api/agent/conversations";
            const reservation = url.pathname === "/api/agent/attachments";
            const matched = url.pathname.match(/^\/api\/agent\/conversations\/([^/]+)(?:\/(messages|events|stop)|\/files\/([^/]+)\/content)?$/);
            const uploadMatch = url.pathname.match(/^\/api\/agent\/attachments\/([^/]+)\/content$/);
            const method = request.method ?? "GET";
            if (creation && method === "GET") {
                validateQuery(url, [
                    "cursor",
                    "limit"
                ]);
                checkLimit(url.searchParams.get("limit"));
                const result = await api(ownerKey, `/api/v1/agent/conversations${url.search}`);
                if (!Array.isArray(result?.items) || !(result.next_cursor === null || typeof result.next_cursor === "string")) throw new HttpError(502, "定位服务返回的会话目录不符合约定。");
                json(response, 200, {
                    ok: true,
                    data: result,
                    error: null
                });
                return;
            }
            if (creation && method === "POST") {
                if (url.search) throw new HttpError(400, "此接口不接受查询参数。");
                const body = await jsonBody(request);
                if (typeof body.request_id !== "string" || !body.request_id.trim()) throw new HttpError(400, "request_id 不能为空。");
                // 保留旧网站请求键，升级后重放仍命中历史收据；owner 隔离由原生存储负责。
                const requestId = createHash("sha256").update(JSON.stringify([
                    user.id,
                    body.request_id
                ])).digest("hex");
                const result = await api(ownerKey, "/api/v1/agent/conversations", {
                    method,
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({
                        ...body,
                        request_id: requestId
                    })
                });
                json(response, 200, {
                    ok: true,
                    data: result,
                    error: null
                });
                return;
            }
            if (reservation && method === "POST") {
                if (url.search) throw new HttpError(400, "此接口不接受查询参数。");
                const body = await jsonBody(request);
                if (typeof body.conversation_id !== "string" || !UUID.test(body.conversation_id)) {
                    throw new HttpError(400, "会话标识无效。");
                }
                const result = await api(ownerKey, "/api/v1/agent/attachments", {
                    method,
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify(body)
                });
                const attachmentId = result?.attachment?.attachment_id;
                if (typeof attachmentId !== "string" || !UUID.test(attachmentId) || result.attachment.conversation_id !== body.conversation_id || result.upload?.attachment_id !== attachmentId) {
                    throw new HttpError(502, "定位服务返回的附件归属无效。");
                }
                result.upload.url = `/api/agent/attachments/${attachmentId}/content`;
                json(response, 200, {
                    ok: true,
                    data: result,
                    error: null
                });
                return;
            }
            if (uploadMatch && method === "PUT") {
                const attachmentId = uploadMatch[1];
                if (!UUID.test(attachmentId)) throw new HttpError(400, "附件标识无效。");
                if (url.search) throw new HttpError(400, "此接口不接受查询参数。");
                const headers = new Headers();
                for (const key of [
                    "idempotency-key",
                    "content-type",
                    "content-length",
                    "x-content-sha256"
                ]){
                    const value = request.headers[key];
                    if (typeof value !== "string") throw new HttpError(400, "上传缺少必需请求头。");
                    headers.set(key, value);
                }
                const size = Number(headers.get("content-length"));
                if (!Number.isSafeInteger(size) || size < 1 || size > MAX_ATTACHMENT_BYTES) throw new HttpError(413, "附件大小超出支持范围。");
                const result = await api(ownerKey, `/api/v1/agent/attachments/${attachmentId}/content`, {
                    method,
                    headers,
                    body: request,
                    duplex: "half"
                });
                json(response, 200, {
                    ok: true,
                    data: result,
                    error: null
                });
                return;
            }
            if (!matched || !UUID.test(matched[1])) throw new HttpError(404, "接口不存在。");
            const [, conversationId, action, artifactId] = matched;
            if (artifactId && !UUID.test(artifactId)) throw new HttpError(404, "接口不存在。");
            if (url.search && action) throw new HttpError(400, "此接口不接受查询参数。");
            if (action === "events" && method === "GET" && !artifactId) {
                const headers = new Headers({
                    Accept: "text/event-stream"
                });
                const cursor = request.headers["last-event-id"];
                if (cursor !== undefined) {
                    if (typeof cursor !== "string" || !/^(0|[1-9][0-9]{0,18})$/.test(cursor)) throw new HttpError(400, "事件游标无效。");
                    headers.set("Last-Event-ID", cursor);
                }
                const controller = new AbortController();
                response.once("close", ()=>controller.abort());
                const upstream = await upstreamFetch(ownerKey, `/api/v1/agent/conversations/${conversationId}/events`, {
                    headers,
                    signal: controller.signal
                });
                if (!upstream.ok) {
                    const envelope = await boundedResponseJson(upstream);
                    const error = safeError(envelope.error);
                    throw new HttpError([
                        400,
                        404,
                        409,
                        413,
                        422,
                        500,
                        503,
                        504
                    ].includes(upstream.status) ? upstream.status : 502, error.message, error.code, error.details, error.retryable);
                }
                if (!upstream.body || !upstream.headers.get("content-type")?.startsWith("text/event-stream")) {
                    await upstream.body?.cancel();
                    throw new HttpError(502, "暂时无法订阅会话事件。");
                }
                response.writeHead(200, {
                    "Content-Type": "text/event-stream; charset=utf-8",
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no"
                });
                await pipeline(Readable.fromWeb(upstream.body), response);
                return;
            }
            if (!action && !artifactId && method === "GET") {
                const included = includesFromUrl(url);
                const query = new URLSearchParams(url.search);
                if (query.has("include")) query.set("include", included.length ? included.join(",") : "none");
                const result = await conversationDetail(ownerKey, conversationId, included, query.size ? `?${query.toString().replaceAll("%2C", ",")}` : "");
                json(response, 200, {
                    ok: true,
                    data: result,
                    error: null
                });
                return;
            }
            if (artifactId && method === "GET") {
                validateQuery(url, [
                    "run_id",
                    "download",
                    "acknowledge_raw_logs"
                ]);
                const runId = url.searchParams.get("run_id");
                if (runId !== null && !UUID.test(runId)) throw new HttpError(400, "轮次标识无效。");
                const current = await conversationDetail(ownerKey, conversationId, [
                    "artifacts"
                ], `?include=artifacts${runId ? `&run_id=${runId}` : ""}`);
                const artifact = current.artifacts.find((item)=>item.artifact_id === artifactId);
                if (!artifact) throw new HttpError(404, "产物不存在。");
                // 响应 URL 没有寻址权；仅用配置前缀和已核验的身份构造目标。
                const target = upstreamUrl(`/api/v1/agent/conversations/${conversationId}/files/${artifact.artifact_id}/content`);
                target.searchParams.set("run_id", current.selected_run_id);
                artifact.download_url = target.href;
                if ([
                    "USER_RESULT_ARCHIVE",
                    "AUDIT_BUNDLE"
                ].includes(artifact.kind)) {
                    const expected = artifact.kind === "USER_RESULT_ARCHIVE" ? "archive" : "audit";
                    const keys = [
                        ...url.searchParams.keys()
                    ];
                    const allowed = expected === "archive" ? [
                        "run_id",
                        "download",
                        "acknowledge_raw_logs"
                    ] : [
                        "run_id",
                        "download"
                    ];
                    if (new Set(keys).size !== keys.length || keys.some((key)=>!allowed.includes(key))) {
                        throw new HttpError(400, "下载参数无效。");
                    }
                    if (url.searchParams.get("download") !== expected || expected === "archive" && url.searchParams.get("acknowledge_raw_logs") !== "true") {
                        throw new HttpError(409, expected === "archive" ? ZIP_NOTICE : "请先确认需要下载审计包。");
                    }
                } else if ([
                    ...url.searchParams.keys()
                ].some((key)=>key !== "run_id")) throw new HttpError(400, "此报告不接受下载参数。");
                const downloadHeaders = {
                    "Content-Type": artifact.content_type,
                    "Content-Length": artifact.size,
                    "X-Content-SHA256": artifact.sha256,
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                    "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(artifact.name)}`
                };
                if ([
                    "USER_RESULT",
                    "GENERIC_REPORT"
                ].includes(artifact.kind) && artifact.size <= MAX_REPORT_BYTES) {
                    const content = await verifiedReport(ownerKey, artifact);
                    response.writeHead(200, downloadHeaders);
                    response.end(content);
                } else {
                    await verifiedDownload(ownerKey, artifact, response, async (path, signal)=>{
                        response.writeHead(200, downloadHeaders);
                        await pipeline(createReadStream(path), response, { signal });
                    });
                }
                return;
            }
            const mutation = (action === "messages" || action === "stop") && method === "POST" || !action && !artifactId && [
                "PATCH",
                "DELETE"
            ].includes(method);
            if (!mutation || artifactId) throw new HttpError(404, "接口不存在。");
            if (url.search) throw new HttpError(400, "此接口不接受查询参数。");
            const body = method === "DELETE" ? undefined : await jsonBody(request);
            const result = await api(ownerKey, `/api/v1/agent/conversations/${conversationId}${action ? `/${action}` : ""}`, {
                method,
                headers: body ? {
                    "Content-Type": "application/json"
                } : undefined,
                body: body ? JSON.stringify(body) : undefined
            });
            json(response, 200, {
                ok: true,
                data: result,
                error: null
            });
        } catch (error) {
            if (response.headersSent || response.destroyed) {
                response.destroy();
                return;
            }
            const publicError = error instanceof HttpError ? error : new HttpError(502, "网站暂时无法连接定位服务。");
            json(response, publicError.status, {
                ok: false,
                data: null,
                error: {
                    code: publicError.code,
                    message: publicError.message,
                    details: publicError.details,
                    retryable: publicError.retryable
                }
            });
        }
    });
    let sweeping = false;
    const sweep = async () => {
        if (sweeping) return;
        sweeping = true;
        try { await sweepDownloadSpools(); }
        catch (error) { reportSpoolCleanupFailure(error); }
        finally { sweeping = false; }
    };
    void sweep();
    const sweepTimer = setInterval(sweep, DOWNLOAD_SWEEP_INTERVAL_MS);
    sweepTimer.unref();
    server.once("close", () => clearInterval(sweepTimer));
    return server;
}
export async function startAgentBackend() {
    const authModule = process.env.WEBSITE_AUTH_MODULE;
    const access = authModule ? (await import(pathToFileURL(resolve(authModule)).href)).access : denyAccess;
    const server = createAgentBackend({
        upstream: process.env.XIAODAO_BASE_URL ?? "http://127.0.0.1:8000",
        access,
        ownerNamespace: process.env.WEBSITE_OWNER_NAMESPACE ?? "xiaodao-website"
    });
    const port = Number(process.env.PORT ?? "8787");
    server.listen(port, "127.0.0.1", ()=>{
        console.log(`网站 Agent 接入示例已启动：http://127.0.0.1:${port}`);
        if (!authModule) console.log("尚未配置 WEBSITE_AUTH_MODULE，所有业务请求默认拒绝。");
    });
    return server;
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await startAgentBackend();
