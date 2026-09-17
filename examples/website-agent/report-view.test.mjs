import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { renderReport } from "./report-view.js";

// A small DOM double checks what a browser would receive without parsing HTML.
// Text never turns into elements; innerHTML is deliberately unavailable.
class DomNode {
  constructor(ownerDocument, tagName) {
    this.ownerDocument = ownerDocument;
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.className = "";
    this.ownText = "";
    this.replacements = 0;
    this.open = false;
    this.listeners = new Map();
    this.parentNode = null;
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("HTML parsing is forbidden"); }
  get innerHTML() { throw new Error("HTML parsing is forbidden"); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  dispatchEvent(event) { this.listeners.get(event.type)?.(event); }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((node) => node !== this);
    this.parentNode = null;
  }
  append(...children) {
    for (const child of children) {
      assert.ok(child instanceof DomNode);
      if (child.tagName === "#fragment") this.append(...child.children.splice(0));
      else { child.parentNode = this; this.children.push(child); }
    }
  }
  replaceChildren(...children) {
    this.replacements++;
    this.children = [];
    this.ownText = "";
    this.append(...children);
  }
}

function container() {
  const doc = {
    createElement(tag) { return new DomNode(doc, tag); },
    createDocumentFragment() { return new DomNode(doc, "#fragment"); },
  };
  return doc.createElement("main");
}

function nodes(root) { return [root, ...root.children.flatMap(nodes)]; }
function matching(root, tag, text) {
  return nodes(root).filter((node) => node.tagName === tag && (text === undefined || node.textContent.includes(text)));
}
function expand(root, title) {
  const element = matching(root, "details").find((node) => node.children[0].textContent === title);
  assert.ok(element, title);
  element.open = true;
  element.dispatchEvent({ type: "toggle" });
  return element;
}
function reopen(element) {
  element.open = false;
  element.dispatchEvent({ type: "toggle" });
  element.open = true;
  element.dispatchEvent({ type: "toggle" });
}

const fixture = JSON.parse(fs.readFileSync(new URL("../../tests/fixtures/contracts/positive/user-result.json", import.meta.url), "utf8"));
function data(overrides = {}) {
  return {
    schema_version: 1, conversation_id: "conversation-1", case_id: "case-1", case_revision: 3,
    case_status: "RESOLVED", archive_status: "READY", report_state: "READY", source_job_id: "job-1",
    format: "problem-locator-diagnosis-v3", report: structuredClone(fixture), markdown: null,
    artifact: { artifact_id: "artifact-1", sha256: "a".repeat(64) }, failure: null, ...overrides,
  };
}

test("structured report renders the authoritative fields as readable cards, with technical records folded", () => {
  const target = container();
  const value = data();
  // A BFF-only presentation field must never override the report itself.
  value.sections = [{ title: "定位结论", value: "WRONG SECTION RESULT" }];
  const root = renderReport(target, value);
  assert.equal(target.children[0], root);
  assert.equal(target.replacements, 1);
  assert.match(root.textContent, /已完成/);
  for (const text of [fixture.root_cause, fixture.problem_statement, fixture.causal_factors[0].statement,
    fixture.completion_criteria_mapping[0].criterion, fixture.completion_criteria_mapping[0].explanation,
    fixture.recommendations[0], fixture.limitations[0], fixture.safety_notes[0]]) assert.ok(root.textContent.includes(text), text);
  assert.ok(!root.textContent.includes("WRONG SECTION RESULT"));
  assert.ok(!root.textContent.includes("[object Object]"));
  const technical = matching(root, "details").find((node) => node.children[0].textContent === "技术详情");
  assert.ok(technical);
  assert.equal(technical.attributes.open, undefined);
  assert.equal(technical.children.length, 1);
  expand(root, "技术详情");
  expand(root, "规则记录（1）");
  expand(root, `证据关联（${fixture.supporting_evidence_bindings.length}）`);
  assert.equal(matching(technical, "summary", "语义判断").length, 1);
  assert.ok(technical.textContent.includes("不表示所有发现都经过独立审核"));
  assert.ok(technical.textContent.includes(fixture.source_job_type));
  assert.ok(technical.textContent.includes(fixture.supporting_evidence_bindings[0].existing_evidence_id));
});

test("findings expose expandable exact citations and show unlocated references honestly", () => {
  const value = data();
  value.report.findings = [{ statement: "请求在连接池等待", confidence: 0.87, evidence_bindings: [], citations: [{
    archive_name: "请求日志.log", line_start: 12, line_end: 14, excerpt: "request_id=abc\n等待 3000ms",
    raw_bytes_sha256: "b".repeat(64), evidence_binding: { existing_evidence_id: "evidence-1", evidence_proposal_key: null },
  }, { archive_name: null, line_start: null, line_end: null, excerpt: null, raw_bytes_sha256: null,
    evidence_binding: { existing_evidence_id: null, evidence_proposal_key: "fact-1" } }] }];
  const root = renderReport(container(), value);
  assert.ok(root.textContent.includes("请求在连接池等待"));
  assert.ok(root.textContent.includes("模型置信度：87%"));
  assert.equal(matching(root, "summary", "查看引用（2）").length, 1);
  expand(root, "查看引用（2）");
  assert.ok(root.textContent.includes("请求日志.log · 第 12—14 行"));
  assert.equal(matching(root, "pre", "request_id=abc\n等待 3000ms").length, 1);
  assert.ok(root.textContent.includes("未提供日志行位置"));
  assert.ok(root.textContent.includes("fact-1"));
});

for (const [status, label] of [["PARTIAL", "部分结果"], ["INCONCLUSIVE", "尚无定论"]]) {
  test(`${status} keeps useful content when root cause is null and arrays are empty`, () => {
    const value = data();
    const report = value.report;
    report.status = status;
    report.root_cause = null;
    report.findings = [];
    report.causal_factors = [];
    report.candidate_factors = [];
    report.excluded_factors = [];
    report.completion_criteria_mapping = [];
    report.supporting_evidence_bindings = [];
    report.verification_rules = [];
    report.evidence_gaps = ["缺少服务端日志"];
    report.recommendations = [];
    report.limitations = [];
    report.safety_notes = [];
    const root = renderReport(container(), value);
    assert.ok(root.textContent.includes(label));
    assert.ok(root.textContent.includes("当前没有可确认的根因。"));
    assert.ok(root.textContent.includes("缺少服务端日志"));
    assert.equal(matching(root, "h3", "关键发现").length, 0);
    assert.equal(matching(root, "h3", "因素分析").length, 0);
    assert.equal(matching(root, "h3", "限制与安全说明").length, 0);
    assert.ok(root.textContent.includes("报告未提供处置建议。"));
    assert.ok(!root.textContent.includes("[object Object]"));
  });
}

test("candidate and excluded factors and all completion states stay distinguishable", () => {
  const value = data();
  value.report.candidate_factors = [{ statement: "待确认：连接泄漏", citations: [] }];
  value.report.excluded_factors = [{ statement: "已排除：DNS 错误", citations: [] }];
  value.report.completion_criteria_mapping = ["SATISFIED", "PARTIALLY_SATISFIED", "UNSATISFIED", "UNKNOWN"].map((status, index) => ({
    criterion_index: index, criterion: `条件 ${index}`, status, explanation: `说明 ${index}`, evidence_bindings: [],
  }));
  const root = renderReport(container(), value);
  for (const text of ["待确认：连接泄漏", "已排除：DNS 错误", "已满足", "部分满足", "未满足", "尚未确认", "条件 3", "说明 3"]) {
    assert.ok(root.textContent.includes(text), text);
  }
});

for (const [assessment, text] of [["RELEVANT", "时间相关"], ["NOT_RELEVANT", "时间不相关"], ["UNKNOWN", "时间关系尚未确认"]]) {
  test(`time relevance uses assessment=${assessment} and accepts absent times`, () => {
    const value = data();
    value.report.time_relevance = { assessment, problem_time: null, derived_anchor_time: null,
      observations: [], explanation: "时间记录说明", citations: [] };
    const root = renderReport(container(), value);
    assert.ok(matching(root, "section").some((section) => section.children[0]?.textContent === "时间相关性" && section.textContent.includes(text)));
    assert.ok(root.textContent.includes("时间记录说明"));
  });
}

for (const reportState of ["PENDING", "UNAVAILABLE"]) {
  test(`${reportState} renders a state notice instead of an empty report`, () => {
    const value = data({ report_state: reportState, format: null, report: null, markdown: null, artifact: null, source_job_id: null,
      failure: reportState === "UNAVAILABLE" ? { code: "AGENT_INTERRUPTED", message: "本次任务已中断。", details: [], retryable: false } : null });
    const root = renderReport(container(), value);
    assert.equal(matching(root, "section").filter((item) => item.className === "xiaodao-report__card").length, 0);
    assert.ok(root.textContent.includes(reportState === "PENDING" ? "等待诊断结果" : "暂无诊断报告"));
    if (value.failure) assert.ok(root.textContent.includes(value.failure.message));
    assert.ok(!root.textContent.includes(fixture.root_cause));
  });
}

test("Markdown stays exact readable text and never activates markup or fetches its URLs", (context) => {
  const fetching = context.mock.method(globalThis, "fetch", () => { throw new Error("renderer must not fetch"); });
  const markdown = '# 诊断 🧭\r\n<script>alert("x")</script>\r\n![图](https://private.internal/image)\r\n';
  const root = renderReport(container(), data({ format: "markdown", report: null, markdown }));
  assert.equal(matching(root, "pre").filter((node) => node.textContent === markdown).length, 1);
  assert.equal(matching(root, "script").length, 0);
  assert.equal(matching(root, "img").length, 0);
  assert.equal(matching(root, "a").length, 0);
  assert.equal(fetching.mock.callCount(), 0);
});

test("legacy Generic V1 renders both stable text fields without assuming an artifact", () => {
  const legacy = { status: "UNRESOLVED", conclusion: "暂未定位原因", root_cause_analysis: "需要服务端日志",
    skill_name: "generic-locator", source_job_id: "job-1" };
  const root = renderReport(container(), data({ format: "generic-v1", report: legacy, artifact: null, case_status: "UNRESOLVED" }));
  assert.ok(root.textContent.includes("尚无定论"));
  assert.ok(root.textContent.includes("暂未定位原因"));
  assert.ok(root.textContent.includes("需要服务端日志"));
  expand(root, "技术详情");
  assert.ok(root.textContent.includes("generic-locator"));
});

for (const format of ["problem-locator-diagnosis-v3", "markdown", "generic-v1"]) {
  for (const archive of ["PENDING", "FAILED", "UNKNOWN"]) {
    test(`${format} report survives archive state ${archive}`, () => {
      const content = "报告正文仍可阅读";
      const value = data({ format, archive_status: archive === "UNKNOWN" ? "PENDING" : archive });
      if (format === "markdown") { value.report = null; value.markdown = content; }
      else if (format === "generic-v1") value.report = { conclusion: content, root_cause_analysis: "分析", status: "RESOLVED" };
      else value.report.problem_statement = content;
      if (archive === "UNKNOWN") value.failure = { code: "DISPATCH_REJECTED", message: "状态暂时无法确认",
        details: [{ field: "phase", actual: "ARCHIVE_STATUS_COMMIT" }, { field: "persistence", actual: "UNKNOWN" }] };
      const root = renderReport(container(), value);
      assert.ok(root.textContent.includes(content));
      assert.ok(root.textContent.includes(archive === "UNKNOWN" ? "归档状态暂时无法确认" : archive === "FAILED" ? "归档文件生成失败" : "归档文件仍在准备中"));
    });
  }
}

test("untrusted report strings stay text, including headings, excerpts and notices", () => {
  const attack = '<img src=x onerror="alert(1)"><script>fetch("/private")</script>';
  const value = data({ failure: { code: "AGENT_EXECUTION_FAILED", message: attack, details: [] } });
  value.report.root_cause = attack;
  value.report.problem_statement = attack;
  value.report.recommendations = [attack];
  value.report.findings = [{ statement: attack, confidence: 0.5, citations: [{ archive_name: attack,
    line_start: 1, line_end: 1, excerpt: attack, raw_bytes_sha256: null, evidence_binding: null }] }];
  const root = renderReport(container(), value);
  assert.ok(root.textContent.includes(attack));
  assert.equal(matching(root, "img").length, 0);
  assert.equal(matching(root, "script").length, 0);
  assert.ok(nodes(root).every((node) => Object.keys(node.attributes).every((name) => !name.startsWith("on"))));
  expand(root, "查看引用（1）");
  assert.equal(matching(root, "pre").filter((node) => node.textContent === attack).length, 1);
});

test("repeated render replaces once without accumulating content or mutating input", () => {
  const target = container();
  const value = data();
  const before = JSON.stringify(value);
  renderReport(target, value);
  const second = renderReport(target, data({ format: "markdown", report: null, markdown: "第二份报告" }));
  assert.equal(target.replacements, 2);
  assert.deepEqual(target.children, [second]);
  assert.ok(target.textContent.includes("第二份报告"));
  assert.ok(!target.textContent.includes(fixture.problem_statement));
  assert.equal(JSON.stringify(value), before);
});

test("invalid responses and late render failures leave the previous display intact", () => {
  const target = container();
  const original = renderReport(target, data());
  for (const value of [null, data({ report_state: "UNKNOWN" }), data({ format: "html" }), data({ report: {} }),
    data({ format: "markdown", markdown: null }), data({ format: "generic-v1", report: {} }),
    data({ report: { ...structuredClone(fixture), findings: [null] } })]) {
    assert.throws(() => renderReport(target, value));
    assert.equal(target.replacements, 1);
    assert.deepEqual(target.children, [original]);
  }
});

test("initial render defers large citation bodies and rule records until separately expanded", () => {
  const value = data();
  let excerptReads = 0;
  let ruleSerializations = 0;
  const largeText = "日志内容\n".repeat(100_000);
  const citation = { archive_name: "large.log", line_start: 1, line_end: 100_000,
    raw_bytes_sha256: "a".repeat(64), evidence_binding: null };
  Object.defineProperty(citation, "excerpt", { get() { excerptReads++; return largeText; } });
  value.report.findings = [{ statement: "可先阅读的发现", confidence: 0.8, citations: [citation] }];
  value.report.verification_rules = Array.from({ length: 1_000 }, (_, index) => ({
    rule_id: `rule-${index}`, status: "SEMANTIC_ONLY", explanation: `规则说明 ${index}`,
    toJSON() { ruleSerializations++; return { rule_id: this.rule_id, detail: largeText }; },
  }));
  const target = container();
  const root = renderReport(target, value);
  const initialCount = nodes(root).length;
  assert.ok(root.textContent.includes("可先阅读的发现"));
  assert.equal(excerptReads, 0);
  assert.equal(ruleSerializations, 0);
  assert.equal(matching(root, "summary", "rule-").length, 0);
  assert.ok(initialCount < 200);
  const references = expand(root, "查看引用（1）");
  assert.ok(excerptReads > 0);
  assert.equal(matching(references, "pre")[0].textContent, largeText);
  const reads = excerptReads;
  reopen(references);
  assert.equal(excerptReads, reads);
  assert.equal(matching(references, "pre").length, 1);

  const technical = expand(root, "技术详情");
  assert.equal(ruleSerializations, 0);
  assert.equal(matching(root, "summary", "rule-").length, 0);
  const rules = expand(root, "规则记录（1000）");
  assert.equal(matching(root, "summary", "rule-").length, 1_000);
  assert.equal(ruleSerializations, 0);
  const first = expand(root, "rule-0 · 语义判断");
  assert.equal(ruleSerializations, 1);
  assert.ok(first.textContent.includes("规则说明 0"));
  assert.ok(first.textContent.includes("日志内容"));
  for (const element of [first, rules, technical]) reopen(element);
  assert.equal(ruleSerializations, 1);
  assert.equal(matching(first, "pre").length, 1);
  assert.equal(target.replacements, 1);
});

test("detail expansion errors preserve the main report and can retry without duplicated output", () => {
  const value = data();
  let unavailable = true;
  value.artifact = { toJSON() { if (unavailable) throw new Error("failed record"); return { artifact_id: "ok" }; } };
  const target = container();
  const root = renderReport(target, value);
  const technical = expand(root, "技术详情");
  assert.ok(root.textContent.includes(fixture.root_cause));
  assert.ok(technical.textContent.includes("详情暂时无法显示"));
  assert.equal(matching(technical, "h3").length, 0);
  unavailable = false;
  reopen(technical);
  assert.ok(!technical.textContent.includes("详情暂时无法显示"));
  assert.equal(matching(technical, "h3", "任务与产物").length, 1);
  assert.ok(technical.textContent.includes('"artifact_id": "ok"'));
  reopen(technical);
  assert.equal(matching(technical, "h3", "任务与产物").length, 1);
  assert.equal(target.replacements, 1);
});

test("conclusion spans the card grid and actionable gaps and recommendations precede findings", () => {
  const value = data();
  value.report.evidence_gaps = ["请补充日志"];
  value.report.findings = [{ statement: "发现", confidence: 0.5, citations: [] }];
  const root = renderReport(container(), value);
  const cards = nodes(root).filter((node) => node.className.split(" ").includes("xiaodao-report__card"));
  assert.ok(cards[0].className.includes("xiaodao-report__card--wide"));
  const titles = cards.map((card) => card.children[0].textContent);
  assert.ok(titles.indexOf("证据缺口") < titles.indexOf("关键发现"));
  assert.ok(titles.indexOf("处置建议") < titles.indexOf("关键发现"));
});
