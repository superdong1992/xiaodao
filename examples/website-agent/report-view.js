/** Render conversation.result. No requests, HTML parsing, or model calls. */
const REPORT_STATUS = { COMPLETED: "已完成", PARTIAL: "部分结果", INCONCLUSIVE: "尚无定论" };
const CRITERION_STATUS = { SATISFIED: "已满足", PARTIALLY_SATISFIED: "部分满足", UNSATISFIED: "未满足", UNKNOWN: "尚未确认" };
const TIME_STATUS = { RELEVANT: "时间相关", NOT_RELEVANT: "时间不相关", UNKNOWN: "时间关系尚未确认" };
const RULE_STATUS = { VERIFIED_PASS: "规则通过", VERIFIED_FAIL: "规则未通过", UNVERIFIABLE: "无法核验", SEMANTIC_ONLY: "语义判断", NOT_APPLICABLE: "不适用" };
const ARRAY_FIELDS = ["findings", "causal_factors", "candidate_factors", "excluded_factors",
  "supporting_evidence_bindings", "completion_criteria_mapping", "verification_rules",
  "evidence_gaps", "limitations", "recommendations", "safety_notes"];

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validate(data) {
  if (!object(data) || !["PENDING", "READY", "UNAVAILABLE"].includes(data.report_state)) {
    throw new TypeError("报告状态不符合接口约定。");
  }
  if (data.report_state !== "READY") return;
  if (data.format === "problem-locator-diagnosis-v3") {
    const report = data.report;
    if (!object(report) || report.schema_version !== 3 || report.format_id !== data.format ||
        !Object.hasOwn(REPORT_STATUS, report.status) || typeof report.problem_statement !== "string" ||
        (report.root_cause !== null && typeof report.root_cause !== "string") ||
        ARRAY_FIELDS.some((key) => !Array.isArray(report[key])) || !object(report.time_relevance)) {
      throw new TypeError("诊断报告缺少必需字段。");
    }
  } else if (data.format === "markdown") {
    if (typeof data.markdown !== "string") throw new TypeError("Markdown 报告正文无效。");
  } else if (data.format === "generic-v1") {
    if (!object(data.report) || typeof data.report.conclusion !== "string" ||
        typeof data.report.root_cause_analysis !== "string") throw new TypeError("历史报告正文无效。");
  } else throw new TypeError("暂不支持此报告格式。");
}

/**
 * Replace a container with a readable report; keep its previous content on error.
 * The caller handles fetching and decides whether a failed refresh needs a notice.
 * Returns the mounted section. Import report-view.css separately in the page.
 */
export function renderReport(container, data) {
  if (!container?.ownerDocument || typeof container.replaceChildren !== "function") {
    throw new TypeError("请提供有效的报告容器。");
  }
  validate(data);
  const doc = container.ownerDocument;
  const node = (tag, className, text) => {
    const element = doc.createElement(tag);
    if (className) element.className = `xiaodao-report__${className}`;
    if (text !== undefined) element.textContent = String(text);
    return element;
  };
  const paragraph = (text, muted = false) => node("p", muted ? "muted" : "text", text);
  const details = (title, build) => {
    const element = node("details", "details");
    element.append(node("summary", "summary", title));
    if (build) {
      let loaded = false;
      let errorNotice;
      element.addEventListener("toggle", () => {
        if (!element.open || loaded) return;
        // A failed expansion never replaces the report or publishes half a
        // detail block. Closing and reopening gives the user another attempt.
        try {
          const content = doc.createDocumentFragment();
          build(content);
          element.append(content);
          loaded = true;
          errorNotice?.remove();
        } catch {
          if (!errorNotice) {
            errorNotice = paragraph("详情暂时无法显示，请关闭后重新展开。", true);
            element.append(errorNotice);
          }
        }
      });
    }
    return element;
  };
  const raw = (value) => node("pre", "raw", typeof value === "string" ? value : JSON.stringify(value, null, 2));
  const list = (values, empty, render = (item) => paragraph(item)) => {
    if (!values.length) return paragraph(empty, true);
    const element = node("ul", "list");
    for (const value of values) {
      const item = node("li", "item");
      item.append(render(value));
      element.append(item);
    }
    return element;
  };
  const evidenceId = (binding) => binding?.existing_evidence_id ?? binding?.evidence_proposal_key ?? "未提供证据标识";
  const citations = (values = []) => {
    if (!values.length) return paragraph("报告未附引用位置。", true);
    return details(`查看引用（${values.length}）`, (content) => content.append(list(values, "", (citation) => {
      const item = node("div", "citation");
      const located = citation.archive_name !== null && citation.archive_name !== undefined;
      item.append(paragraph(located ? `${citation.archive_name} · 第 ${citation.line_start}—${citation.line_end} 行` : "未提供日志行位置"));
      if (citation.excerpt !== null && citation.excerpt !== undefined) item.append(raw(citation.excerpt));
      item.append(paragraph(`证据：${evidenceId(citation.evidence_binding)}`, true));
      if (citation.raw_bytes_sha256) item.append(paragraph(`SHA-256：${citation.raw_bytes_sha256}`, true));
      return item;
    })));
  };
  const fragment = doc.createDocumentFragment();
  const root = node("section");
  root.className = "xiaodao-report";
  root.setAttribute("aria-label", "诊断报告");
  const header = node("header", "header");
  const ready = data.report_state === "READY";
  header.append(node("h2", "heading", ready ? "诊断报告" : data.report_state === "PENDING" ? "等待诊断结果" : "暂无诊断报告"));
  const label = !ready ? (data.report_state === "PENDING" ? "等待结果" : "未生成报告") :
    data.format === "problem-locator-diagnosis-v3" ? REPORT_STATUS[data.report.status] :
    data.case_status === "RESOLVED" ? "已完成" : data.case_status === "PARTIALLY_RESOLVED" ? "部分结果" :
    data.case_status === "UNRESOLVED" ? "尚无定论" : "报告已就绪";
  header.append(node("span", "badge", label));
  root.append(header);

  const archiveUnknown = data.failure?.details?.some((item) => item.field === "phase" && item.actual === "ARCHIVE_STATUS_COMMIT");
  if (ready && archiveUnknown) root.append(node("p", "notice", "报告已生成，但归档状态暂时无法确认。"));
  else if (ready && data.archive_status === "FAILED") root.append(node("p", "notice", "报告已生成，但归档文件生成失败。"));
  else if (ready && data.archive_status === "PENDING") root.append(paragraph("报告已生成，归档文件仍在准备中。", true));
  if (data.failure && !archiveUnknown) root.append(node("p", "notice", data.failure.message || "任务状态异常，请查看诊断详情。"));

  const cards = node("div", "cards");
  const card = (title, ...content) => {
    const element = node("section", "card");
    element.append(node("h3", "title", title), ...content);
    cards.append(element);
    return element;
  };
  if (!ready) {
    root.append(paragraph(data.report_state === "PENDING" ? "诊断尚未完成，或正在等待补充信息。" : "本次任务已结束，未生成可展示的报告。"));
  } else if (data.format === "markdown") {
    card("诊断报告", paragraph("以下为报告原文，保留原始格式。", true), raw(data.markdown));
  } else if (data.format === "generic-v1") {
    card("历史诊断结论", paragraph(data.report.conclusion));
    card("原因分析", paragraph(data.report.root_cause_analysis));
  } else {
    const report = data.report;
    card("定位结论", paragraph(report.root_cause ?? "当前没有可确认的根因。"))
      .className += " xiaodao-report__card--wide";
    card("问题描述", paragraph(report.problem_statement));
    if (report.evidence_gaps.length) card("证据缺口", list(report.evidence_gaps, ""));
    card("处置建议", list(report.recommendations, "报告未提供处置建议。"));
    if (report.findings.length) card("关键发现", list(report.findings, "", (finding) => {
      const item = node("div");
      item.append(paragraph(finding.statement));
      if (typeof finding.confidence === "number") item.append(paragraph(`模型置信度：${Math.round(finding.confidence * 100)}%`, true));
      item.append(citations(finding.citations));
      return item;
    }));
    if (report.causal_factors.length || report.candidate_factors.length || report.excluded_factors.length) {
      const factors = node("div");
      for (const [title, entries] of [["原因与因素", report.causal_factors], ["待确认因素", report.candidate_factors], ["已排除因素", report.excluded_factors]]) {
        if (!entries.length) continue;
        factors.append(node("h4", "subtitle", title), list(entries, "", (factor) => {
        const item = node("div");
        item.append(paragraph(factor.statement), citations(factor.citations));
        return item;
        }));
      }
      card("因素分析", factors);
    }
    card("完成情况", list(report.completion_criteria_mapping, "报告未列出完成条件。", (entry) => {
      const item = node("div");
      item.append(node("span", "badge", CRITERION_STATUS[entry.status] ?? entry.status), paragraph(entry.criterion), paragraph(entry.explanation, true));
      return item;
    }));
    if (report.limitations.length || report.safety_notes.length) {
      const notes = [];
      if (report.limitations.length) notes.push(node("h4", "subtitle", "适用限制"), list(report.limitations, ""));
      if (report.safety_notes.length) notes.push(node("h4", "subtitle", "安全说明"), list(report.safety_notes, ""));
      card("限制与安全说明", ...notes);
    }
    const time = report.time_relevance;
    card("时间相关性", paragraph(TIME_STATUS[time.assessment] ?? "时间关系尚未确认"), paragraph(time.explanation));
  }
  if (ready) root.append(cards);

  const technical = details("技术详情", (content) => {
    if (ready && data.format === "problem-locator-diagnosis-v3") {
      const report = data.report;
      content.append(node("h3", "title", "报告格式"), raw({ schema_version: report.schema_version,
        format_id: report.format_id, source_job_type: report.source_job_type }));
      content.append(paragraph("这里保留报告中的规则状态，不表示所有发现都经过独立审核。", true),
        details(`规则记录（${report.verification_rules.length}）`, (rules) => rules.append(
          list(report.verification_rules, "报告未列出规则记录。", (rule) =>
            details(`${rule.rule_id} · ${RULE_STATUS[rule.status] ?? rule.status}`,
              (entry) => entry.append(paragraph(rule.explanation), raw(rule)))))),
        details(`证据关联（${report.supporting_evidence_bindings.length}）`, (entry) => entry.append(raw(report.supporting_evidence_bindings))),
        details("时间记录", (entry) => entry.append(raw(report.time_relevance))));
    } else if (ready && data.format === "generic-v1") {
      content.append(node("h3", "title", "历史结果记录"), raw(data.report));
    }
    content.append(node("h3", "title", "任务与产物"), raw({
      conversation_id: data.conversation_id ?? null, case_id: data.case_id ?? null,
      case_revision: data.case_revision ?? null, source_job_id: data.source_job_id ?? null,
      format: data.format ?? null, case_status: data.case_status ?? null, archive_status: data.archive_status ?? null,
      artifact: data.artifact ?? null,
    }));
    if (data.failure) content.append(node("h3", "title", "失败信息"), raw(data.failure));
  });
  root.append(technical);
  fragment.append(root);
  container.replaceChildren(fragment);
  return root;
}
