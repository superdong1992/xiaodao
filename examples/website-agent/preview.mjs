/** 仅供本地查看合成报告；没有诊断、代理、上传或登录接口。 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = new URL("./", import.meta.url);
const id = (suffix) => `00000000-0000-0000-0000-${String(suffix).padStart(12, "0")}`;
const now = "2026-09-17T02:00:00.000Z";

// 使用仓库的有效合同样例作为底稿，只替换展示文案和各场景状态。
export function previewSamples() {
  const report = JSON.parse(readFileSync(new URL("../../tests/fixtures/contracts/positive/user-result.json", here), "utf8"));
  report.problem_statement = "付款服务调用库存服务时出现 RPC 超时，请定位原因。";
  report.root_cause = "库存 RPC 超过请求截止时间，付款流程未能及时收到响应。";
  report.causal_factors[0].statement = report.root_cause;
  report.completion_criteria_mapping[0].criterion = "确认发生超时的请求及现有日志能够支持的原因。";
  report.completion_criteria_mapping[0].explanation = "日志记录了同一请求的超时事件。";
  report.verification_rules[0].explanation = "日志能够支持超时判断；更深层原因仍需结合服务端上下文。";
  report.time_relevance.explanation = "当前样例没有足够的事件时间，无法判断与问题时段的关联。";
  report.limitations = ["本例为合成数据，仅演示报告字段和界面布局。", "当前日志不足以区分网络等待与服务端处理延迟。"];
  report.recommendations = ["按请求标识对照调用端和库存服务日志。", "检查库存服务处理耗时及调用链的超时设置。"];
  report.safety_notes = ["修改超时配置前，先评估下游承载能力。"];
  report.findings = [{ statement: "同一请求出现 deadline exceeded，调用端按超时结束。", confidence: 0.9,
    evidence_bindings: structuredClone(report.supporting_evidence_bindings),
    citations: structuredClone(report.causal_factors[0].citations) }];

  const base = { schema_version: 1, conversation_id: id(1), case_id: id(2), case_revision: 8,
    case_status: "RESOLVED", archive_status: "PENDING", report_state: "READY", source_job_id: id(3),
    format: "problem-locator-diagnosis-v3", report: null, markdown: null, artifact: null, failure: null };
  function published(body, options = {}) {
    const markdown = typeof body === "string";
    const bytes = Buffer.from(markdown ? body : JSON.stringify(body));
    return { ...base, report: markdown ? null : body, markdown: markdown ? body : null,
      format: markdown ? "markdown" : base.format,
      artifact: { artifact_id: id(4), kind: markdown ? "GENERIC_REPORT" : "USER_RESULT",
        name: markdown ? "generic-diagnosis.md" : "diagnosis-result.json",
        content_type: markdown ? "text/markdown" : "application/json", resource_kind: "FILE",
        size: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex"),
        created_by_job_id: id(3), created_at: now, downloadable: true }, ...options };
  }
  const sample = (label, note, result) => ({ label, note, response: { ok: true, error: null,
    data: { schema_version: 3, conversation_id: result.conversation_id,
      status: result.report_state === "UNAVAILABLE" ? "FAILED" :
        result.report_state === "PENDING" || result.archive_status === "PENDING" ? "RUNNING" : "COMPLETED",
      case_id: result.case_id, job_id: null, case_status: result.case_status,
      case_revision: result.case_revision, source_job_id: result.source_job_id,
      archive_status: result.archive_status, report_state: result.report_state,
      current_questions: [], progress: null, failure: result.failure,
      title: label, selected_run_id: id(6), run_id: id(6),
      current_run: { run_id: id(6), ordinal: 1, status: result.report_state === "UNAVAILABLE" ? "FAILED" :
        result.report_state === "PENDING" || result.archive_status === "PENDING" ? "RUNNING" : "COMPLETED",
        case_id: result.case_id, job_id: null, case_status: result.case_status, archive_status: result.archive_status,
        report_state: result.report_state, created_at: now, updated_at: now },
      capabilities: { can_send: result.report_state !== "PENDING", can_stop: result.report_state === "PENDING",
        can_rediagnose: result.report_state !== "PENDING", can_rename: true, can_delete: true },
      history: [], history_next_cursor: null, attachments: [], last_event_id: 0, created_at: now, updated_at: now,
      included: ["history", "report", "artifacts"], result,
      artifacts: result.artifact ? [{ ...result.artifact,
        download_url: `/api/agent/conversations/${result.conversation_id}/files/${result.artifact.artifact_id}/content?run_id=${id(6)}` }] : [],
    } } });
  const partial = structuredClone(report);
  partial.status = "PARTIAL";
  partial.root_cause = null;
  partial.evidence_gaps = ["缺少库存服务同一时段的日志，尚无法确定超时的完整原因。"];
  partial.completion_criteria_mapping[0].status = "PARTIALLY_SATISFIED";
  const inconclusive = structuredClone(partial);
  inconclusive.status = "INCONCLUSIVE";
  inconclusive.findings = [];
  inconclusive.causal_factors = [];
  inconclusive.supporting_evidence_bindings = [];
  inconclusive.verification_rules = [];
  inconclusive.completion_criteria_mapping[0] = { ...inconclusive.completion_criteria_mapping[0],
    status: "UNKNOWN", evidence_bindings: [], explanation: "日志覆盖不足，当前无法判断。" };
  const empty = { ...base, source_job_id: null, format: null };
  return [
    sample("完整结果", "READY 表示报告可读；报告内部 COMPLETED 表示已完成诊断。日志归档仍可在后台生成。", published(report)),
    sample("部分结果", "部分结果也正常展示。根因为空时明确说明，保留已有发现并突出证据缺口。", published(partial, { case_status: "PARTIALLY_RESOLVED" })),
    sample("暂无法确定", "INCONCLUSIVE 不是接口报错。用户仍可查看缺口、限制和后续建议。", published(inconclusive, { case_status: "UNRESOLVED", archive_status: "NOT_REQUIRED" })),
    sample("等待结果", "PENDING 包括正在诊断和等待补充；追问与具体进度由 status 或 SSE 展示。", { ...empty, report_state: "PENDING", case_status: "RUNNING" }),
    sample("未生成报告", "任务已结束但没有正式报告时，展示失败原因和诊断关联 ID。", { ...empty, report_state: "UNAVAILABLE", case_status: "FAILED", archive_status: "NOT_REQUIRED",
      failure: { code: "AGENT_EXECUTION_FAILED", message: "本次定位未能完成，请重新发起任务。", retryable: false,
        details: [{ field: "diagnostic_id", actual: "preview-diagnostic-001" }] } }),
    sample("归档状态未知", "归档故障不会撤回报告；页面展示提示，同时保留可读结果。", published(report, {
      failure: { code: "DISPATCH_REJECTED", message: "报告已生成，但归档状态暂时无法确认。", retryable: false,
        details: [{ field: "phase", actual: "ARCHIVE_STATUS_COMMIT" }, { field: "persistence", actual: "UNKNOWN" }] } })),
    sample("通用 Markdown", "通用报告使用独立格式。示例默认按原文展示；接入现有 Markdown 组件时禁用不可信 HTML。", published(
      "# 通用诊断报告\n\n## 当前判断\n日志显示请求超时，现有信息不足以确认更深层原因。\n\n## 下一步\n- 补充服务端同一时段的日志。\n- 按请求标识关联调用链。\n")),
    sample("历史报告", "历史 generic-v1 使用固定的 conclusion 和 root_cause_analysis 字段。", {
      ...base, format: "generic-v1", archive_status: "NOT_REQUIRED", report: { status: "RESOLVED", conclusion: "该请求在调用库存服务时超时。",
        root_cause_analysis: "从现有日志可确认 RPC 超过截止时间。", skill_name: "generic-problem-locator",
        source_job_id: id(3), source_outcome_id: id(5), occurred_at: now } }),
  ];
}

export function createPreviewServer() {
  const files = new Map([
    ["/", ["preview.html", "text/html"]], ["/preview.js", ["preview.js", "text/javascript"]],
    ["/preview.css", ["preview.css", "text/css"]], ["/report-view.js", ["report-view.js", "text/javascript"]],
    ["/report-view.css", ["report-view.css", "text/css"]],
    ["/browser-client.js", ["browser-client.js", "text/javascript"]],
    ["/preview-model.js", ["preview-model.js", "text/javascript"]],
  ]);
  const samples = Buffer.from(JSON.stringify(previewSamples()));
  return createServer((request, response) => {
    const route = request.url;
    const entry = files.get(route);
    if (request.method !== "GET" || (!entry && route !== "/sample-data.json")) {
      response.writeHead(404); response.end("仅提供本地报告预览。"); return;
    }
    const body = entry ? readFileSync(new URL(entry[0], here)) : samples;
    response.writeHead(200, { "Content-Type": `${entry ? entry[1] : "application/json"}; charset=utf-8`,
      "Content-Length": body.length, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
      "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'" });
    response.end(body);
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  createPreviewServer().listen(8788, "127.0.0.1", () => {
    console.log("报告离线预览：http://127.0.0.1:8788/（合成数据，不调用模型）");
  });
}
