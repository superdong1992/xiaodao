import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const GUIDE_PATH = path.join(REPO_ROOT, "docs", "browser-rest-api.md");
const OPENAPI_PATH = path.join(REPO_ROOT, "schemas", "v2", "web-api.openapi.snapshot.json");

const BUSINESS_OPERATIONS = [
  "GET /api/v1/artifacts/{artifact_id}/content",
  "GET /api/v1/cases/{case_id}",
  "GET /api/v1/cases/{case_id}/artifacts",
  "POST /api/v1/cases",
  "POST /api/v1/cases/{case_id}/attachments",
  "POST /api/v1/cases/{case_id}/supplements",
  "PUT /api/v1/attachments/{attachment_id}/content",
];
const SERVICE_OPERATIONS = ["GET /live", "GET /ready"];
const DOCUMENTATION_OPERATIONS = ["GET /docs", "GET /openapi.json"];

function guideText() {
  assert.ok(fs.existsSync(GUIDE_PATH), "missing standalone browser REST API guide");
  return fs.readFileSync(GUIDE_PATH, "utf8");
}

function openApiDocument() {
  const document = JSON.parse(fs.readFileSync(OPENAPI_PATH, "utf8"));
  assert.ok(document.paths && document.components?.schemas, "versioned REST contract must be a complete OpenAPI document");
  return document;
}

function openApiOperations(document) {
  const methods = new Set(["get", "post", "put", "patch", "delete"]);
  return Object.entries(document.paths)
    .flatMap(([urlPath, item]) => Object.keys(item)
      .filter((method) => methods.has(method))
      .map((method) => `${method.toUpperCase()} ${urlPath}`))
    .sort();
}

function guideOperations(markdown) {
  return markdown.split(/\r?\n/)
    .map((line) => /^###\s+(GET|POST|PUT|PATCH|DELETE)\s+`(\/[^`]+)`\s*$/.exec(line))
    .filter(Boolean)
    .map((match) => `${match[1]} ${match[2]}`)
    .sort();
}

function tableRow(markdown, token) {
  const marker = `\`${token}\``;
  return markdown.split(/\r?\n/).find((line) => line.trimStart().startsWith("|") && line.includes(marker));
}

function hasMeaningfulTableRow(markdown, token) {
  const row = tableRow(markdown, token);
  if (!row) return false;
  const cells = row.split("|").slice(1, -1).map((cell) => cell.trim());
  return cells.length >= 3 && Boolean(cells.at(-1));
}

function hasModelFieldTableRow(markdown, modelName, fieldName) {
  return markdown.split(/\r?\n/u).some((line) => {
    if (!line.trimStart().startsWith("|")) return false;
    const cells = line.split("|").slice(1, -1).map((cell) => cell.trim());
    return cells[0] === `\`${modelName}\``
      && cells[1] === `\`${fieldName}\``
      && Boolean(cells.at(-1));
  });
}

function componentRefs(value, refs = new Set()) {
  if (Array.isArray(value)) {
    for (const nested of value) componentRefs(nested, refs);
  } else if (value && typeof value === "object") {
    if (typeof value.$ref === "string" && value.$ref.startsWith("#/components/schemas/")) {
      refs.add(value.$ref.split("/").at(-1));
    }
    for (const nested of Object.values(value)) componentRefs(nested, refs);
  }
  return refs;
}

function responseComponentNames(document) {
  const pending = componentRefs(
    Object.values(document.paths).flatMap((pathItem) => Object.values(pathItem)
      .map((operation) => operation.responses ?? {})),
  );
  const visited = new Set();
  while ([...pending].some((name) => !visited.has(name))) {
    const name = [...pending].find((candidate) => !visited.has(candidate));
    visited.add(name);
    for (const nested of componentRefs(document.components.schemas[name])) pending.add(nested);
  }
  return visited;
}

test("the standalone browser guide contains only browser REST integration material", () => {
  const guide = guideText();
  assert.doesNotMatch(
    guide,
    /\b(?:MCP|Claude|Skill)\b|problem_locator_[a-z_]+/i,
    "browser guide contains cross-protocol concepts or tool names",
  );
  for (const token of [
    "request_id",
    "case_revision",
    "diagnosis_state_revision",
    "case_view",
    "wait_timed_out",
    "dispatch_pending",
    "Content-Length",
    "X-Content-SHA256",
    "CONFIG",
  ]) {
    assert.ok(guide.includes(`\`${token}\``), `guide is missing required browser behavior: ${token}`);
  }
  for (const phrase of ["Python `fullmatch`", 'credentials: "omit"']) {
    assert.ok(guide.includes(phrase), `guide is missing required browser behavior: ${phrase}`);
  }
});

test("the guide documents exactly the public OpenAPI operations and browser helper paths", () => {
  const guide = guideText();
  const openapi = openApiDocument();
  assert.doesNotMatch(
    JSON.stringify(openapi),
    /\b(?:MCP|Claude)\b|problem_locator_[a-z_]+/i,
    "browser OpenAPI contract contains cross-protocol concepts or tool names",
  );
  const contractOperations = openApiOperations(openapi);
  assert.deepEqual(contractOperations, [...BUSINESS_OPERATIONS, ...SERVICE_OPERATIONS].sort());
  assert.deepEqual(
    guideOperations(guide),
    [...BUSINESS_OPERATIONS, ...SERVICE_OPERATIONS, ...DOCUMENTATION_OPERATIONS].sort(),
  );
  assert.deepEqual(contractOperations.filter((item) => item.includes(" /api/v1/")), BUSINESS_OPERATIONS);
});

test("the guide tables cover every published field, Case state, and public error code", () => {
  const guide = guideText();
  const document = openApiDocument();
  const schemas = document.components.schemas;
  const fieldNames = new Set(
    Object.values(schemas).flatMap((schema) => Object.keys(schema.properties ?? {})),
  );
  assert.ok(fieldNames.size > 0, "OpenAPI has no published fields");
  assert.deepEqual(
    [...fieldNames].sort().filter((name) => !hasMeaningfulTableRow(guide, name)),
    [],
    "OpenAPI fields missing a guide table row with meaning",
  );

  for (const schemaName of ["CaseStatus", "ErrorCode"]) {
    const values = schemas[schemaName]?.enum;
    assert.ok(Array.isArray(values) && values.length > 0, `OpenAPI is missing ${schemaName} enum values`);
    assert.deepEqual(
      values.filter((value) => !hasMeaningfulTableRow(guide, value)),
      [],
      `${schemaName} values missing a guide table row with meaning/action`,
    );
  }

  const missingResponseModels = [...responseComponentNames(document)]
    .filter((name) => schemas[name].properties)
    .filter((name) => !name.startsWith("SuccessEnvelope_"))
    .filter((name) => !guide.includes(`| \`${name}\` |`))
    .sort();
  assert.deepEqual(
    missingResponseModels,
    [],
    "reachable response models must use their exact OpenAPI names in the guide",
  );

  const missingModelFields = [...responseComponentNames(document)]
    .flatMap((name) => {
      const displayName = name.startsWith("SuccessEnvelope_")
        ? "SuccessEnvelope<T>"
        : name;
      return Object.keys(schemas[name].properties ?? {})
        .filter((field) => !hasModelFieldTableRow(guide, displayName, field))
        .map((field) => `${name}.${field}`);
    })
    .sort();
  assert.deepEqual(
    missingModelFields,
    [],
    "reachable response fields must be documented against their exact model",
  );
});

test("the TypeScript example implements large-file integrity and actionable recovery", () => {
  const guide = guideText();

  assert.match(guide, /interface WorkerSha256Port\s*\{/);
  assert.match(guide, /type WorkerSha256Factory = \(\) => Promise<WorkerSha256Port>/);
  assert.match(
    guide,
    /if \(blob\.size > MAX_ATTACHMENT_BYTES_V1\)[\s\S]*?const worker = await createWorker\(\)/,
    "oversized attachments must fail before the hashing Worker starts",
  );
  assert.match(guide, /measureBlobIncrementally\(/);
  assert.match(
    guide,
    /measured\.sha256 !== upload\.required_headers\["X-Content-SHA256"\]/,
    "upload must compare the measured file digest with the prepared digest",
  );

  assert.match(guide, /response\.body\.getReader\(\)/);
  assert.match(guide, /size \+= value\.byteLength/);
  assert.match(guide, /await worker\.update\(value\.slice\(\)\)/);
  assert.match(guide, /await sink\.write\(value\)/);
  assert.match(guide, /await sink\.commit\(\)/);
  assert.match(guide, /await sink\.abort\(error\)/);
  assert.doesNotMatch(
    guide,
    /response\.blob\(\)/,
    "the general artifact path must not buffer the complete response as one Blob",
  );

  assert.match(guide, /if \(view\.status === "NEW"\) await abortableDelay\(/);
  assert.doesNotMatch(guide, /pending_requirements:\s*unknown\[\]/);
  assert.doesNotMatch(guide, /artifacts:\s*unknown\[\]/);
  assert.doesNotMatch(guide, /\[field:\s*string\]:\s*unknown/);
  for (const field of [
    "pending_requirements: PendingRequirement[]",
    "artifacts: ArtifactSummary[]",
    "final_result: CandidateConclusion | null",
    "unresolved_result: UnresolvedResult | null",
    "generic_result: GenericResult | null",
    "failure: CaseFailure | null",
  ]) {
    assert.ok(guide.includes(field), `typed CaseView is missing ${field}`);
  }

  const attachmentNotReady = tableRow(guide, "ATTACHMENT_NOT_READY");
  assert.match(attachmentNotReady, /幂等重放 PUT/);
  assert.match(attachmentNotReady, /READY/);
  assert.match(attachmentNotReady, /supplement/);
  assert.doesNotMatch(attachmentNotReady, /查询\/确认上传/);
});
