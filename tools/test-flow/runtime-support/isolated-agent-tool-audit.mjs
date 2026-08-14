import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const SKILL_GENERATION_TRACE_CODES = Object.freeze({
  EVENTS_INVALID: "SKILL_TRACE_EVENTS_INVALID",
  STREAM_ERROR: "SKILL_TRACE_STREAM_ERROR",
  INIT_INVALID: "SKILL_TRACE_INIT_INVALID",
  INIT_CWD_MISMATCH: "SKILL_TRACE_INIT_CWD_MISMATCH",
  PERMISSION_MODE_INVALID: "SKILL_TRACE_PERMISSION_MODE_INVALID",
  TOOL_INVENTORY_INVALID: "SKILL_TRACE_TOOL_INVENTORY_INVALID",
  RESULT_INVALID: "SKILL_TRACE_RESULT_INVALID",
  RESULT_NOT_SUCCESS: "SKILL_TRACE_RESULT_NOT_SUCCESS",
  TOOL_EVENT_INVALID: "SKILL_TRACE_TOOL_EVENT_INVALID",
  TOOL_NOT_ALLOWED: "SKILL_TRACE_TOOL_NOT_ALLOWED",
  TOOL_USE_ID_INVALID: "SKILL_TRACE_TOOL_USE_ID_INVALID",
  TOOL_RESULT_UNMATCHED: "SKILL_TRACE_TOOL_RESULT_UNMATCHED",
  TOOL_RESULT_DUPLICATE: "SKILL_TRACE_TOOL_RESULT_DUPLICATE",
  TOOL_RESULT_ERROR: "SKILL_TRACE_TOOL_RESULT_ERROR",
  TOOL_RESULT_MISSING: "SKILL_TRACE_TOOL_RESULT_MISSING",
  SKILL_INVOCATION_INVALID: "SKILL_TRACE_SKILL_INVOCATION_INVALID",
  SKILL_RESULT_INVALID: "SKILL_TRACE_SKILL_RESULT_INVALID",
  ROOT_INVALID: "SKILL_TRACE_ROOT_INVALID",
  SKILL_DOCUMENT_INVALID: "SKILL_TRACE_SKILL_DOCUMENT_INVALID",
  SKILL_LINK_INVALID: "SKILL_TRACE_SKILL_LINK_INVALID",
  REQUIRED_REFERENCE_INVALID: "SKILL_TRACE_REQUIRED_REFERENCE_INVALID",
  PATH_ABSOLUTE: "SKILL_TRACE_PATH_ABSOLUTE",
  PATH_TRAVERSAL: "SKILL_TRACE_PATH_TRAVERSAL",
  PATH_NOT_NORMALIZED: "SKILL_TRACE_PATH_NOT_NORMALIZED",
  PATH_MISSING: "SKILL_TRACE_PATH_MISSING",
  PATH_SYMLINK: "SKILL_TRACE_PATH_SYMLINK",
  PATH_HARDLINK: "SKILL_TRACE_PATH_HARDLINK",
  PATH_KIND_INVALID: "SKILL_TRACE_PATH_KIND_INVALID",
  READ_INPUT_INVALID: "SKILL_TRACE_READ_INPUT_INVALID",
  REQUIRED_READ_PARTIAL: "SKILL_TRACE_REQUIRED_READ_PARTIAL",
  REQUIRED_READ_ORDER_INVALID: "SKILL_TRACE_REQUIRED_READ_ORDER_INVALID",
  READ_UNLINKED: "SKILL_TRACE_READ_UNLINKED",
  REQUIRED_READ_MISSING: "SKILL_TRACE_REQUIRED_READ_MISSING",
  WRITE_INPUT_INVALID: "SKILL_TRACE_WRITE_INPUT_INVALID",
  WRITE_COUNT_INVALID: "SKILL_TRACE_WRITE_COUNT_INVALID",
  WRITE_PATH_INVALID: "SKILL_TRACE_WRITE_PATH_INVALID",
  WRITE_CONTENT_MISMATCH: "SKILL_TRACE_WRITE_CONTENT_MISMATCH",
});

export class SkillGenerationTraceAuditError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "SkillGenerationTraceAuditError";
    this.code = code;
    this.details = details;
  }
}

const ALLOWED_TOOLS = Object.freeze(["Skill", "Read", "Write"]);
export const SKILL_GENERATION_TRACE_SCHEMA_VERSION = 2;
export const SKILL_GENERATION_TOOL_ATTEMPT_POLICY = Object.freeze({
  schema_version: 1,
  version: "skill-generation-tool-attempts-v2",
  classification: "locally-recomputed-required-fields-absent",
  max_empty_write_rejections: 1,
  empty_write_rejection_requires_explicit_error: true,
  empty_write_rejection_must_follow_required_reads: true,
  empty_write_rejection_must_immediately_precede_success: true,
  empty_write_rejection_result_must_precede_success: true,
  successful_write_count: 1,
  successful_write_must_be_final_tool: true,
});
const REQUIRED_INPUT_PATHS = Object.freeze([
  "inputs/wiki.md",
  "inputs/clarifications.md",
]);
const DEFAULT_REQUIRED_REFERENCES = Object.freeze([
  "references/generation-spec-v5-reference.md",
  "references/verification-contract-v2-reference.md",
]);
const DEFAULT_REQUIRED_RECEIPT_READS = Object.freeze([
  ...REQUIRED_INPUT_PATHS.map((relative) => `workspace/${relative}`),
  ...DEFAULT_REQUIRED_REFERENCES.map((relative) => `skill/${relative}`),
]);
const OUTPUT_PATH = "output/generation-spec.json";

function fail(code, message, details = {}) {
  throw new SkillGenerationTraceAuditError(code, message, details);
}

function requireAudit(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, expected) {
  return isPlainObject(value)
    && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0");
}

function portableRelativePath(value) {
  requireAudit(typeof value === "string" && value.length > 0 && !value.includes("\0"), SKILL_GENERATION_TRACE_CODES.PATH_NOT_NORMALIZED, "Tool paths must be non-empty portable relative paths");
  requireAudit(!path.posix.isAbsolute(value) && !path.win32.isAbsolute(value), SKILL_GENERATION_TRACE_CODES.PATH_ABSOLUTE, "Absolute tool paths are forbidden");
  const slashed = value.replaceAll("\\", "/");
  const segments = slashed.split("/");
  requireAudit(!segments.includes(".."), SKILL_GENERATION_TRACE_CODES.PATH_TRAVERSAL, "Tool paths must not traverse parent directories");
  requireAudit(!segments.some((segment) => segment === "" || segment === ".") && slashed === value && path.posix.normalize(slashed) === slashed, SKILL_GENERATION_TRACE_CODES.PATH_NOT_NORMALIZED, "Tool paths must use normalized forward-slash syntax");
  return slashed;
}

function dotSegmentPath(value) {
  const slashed = value.replaceAll("\\", "/");
  const withoutRoot = slashed
    .replace(/^[A-Za-z]:\//, "")
    .replace(/^\/+/, "");
  return withoutRoot.split("/").some((segment) => segment === "" || segment === "." || segment === "..");
}

function pathInside(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function inspectRoot(root, label) {
  requireAudit(typeof root === "string" && path.isAbsolute(root), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} must be an absolute path`);
  let metadata;
  try {
    metadata = fs.lstatSync(root);
  } catch (error) {
    fail(SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} is unavailable`, { cause: error?.code ?? "UNKNOWN" });
  }
  requireAudit(!metadata.isSymbolicLink() && metadata.isDirectory(), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} must be a real directory`);
  let real;
  try {
    real = fs.realpathSync.native(root);
  } catch (error) {
    fail(SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} cannot be resolved`, { cause: error?.code ?? "UNKNOWN" });
  }
  const resolved = path.resolve(root);
  const same = process.platform === "win32"
    ? real.toLowerCase() === resolved.toLowerCase()
    : real === resolved;
  requireAudit(same, SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} must not contain symlinked path components`);
}

function inspectRegularFile(root, relativePath) {
  inspectRoot(root, "Path root");
  let current = path.resolve(root);
  const segments = portableRelativePath(relativePath).split("/");
  for (let index = 0; index < segments.length; index += 1) {
    current = path.join(current, segments[index]);
    let metadata;
    try {
      metadata = fs.lstatSync(current);
    } catch (error) {
      fail(SKILL_GENERATION_TRACE_CODES.PATH_MISSING, `Audited path does not exist: ${relativePath}`, { path: relativePath, cause: error?.code ?? "UNKNOWN" });
    }
    requireAudit(!metadata.isSymbolicLink(), SKILL_GENERATION_TRACE_CODES.PATH_SYMLINK, `Audited paths must not contain symlinks: ${relativePath}`, { path: relativePath });
    const final = index === segments.length - 1;
    requireAudit(final ? metadata.isFile() : metadata.isDirectory(), SKILL_GENERATION_TRACE_CODES.PATH_KIND_INVALID, `Audited path has an invalid node kind: ${relativePath}`, { path: relativePath });
    if (final) requireAudit(metadata.nlink === 1, SKILL_GENERATION_TRACE_CODES.PATH_HARDLINK, `Audited files must not be hardlinked: ${relativePath}`, { path: relativePath });
  }
  requireAudit(pathInside(root, current), SKILL_GENERATION_TRACE_CODES.PATH_TRAVERSAL, `Audited path escapes its root: ${relativePath}`, { path: relativePath });
  return current;
}

function ordinaryMarkdownLinks(document) {
  const links = [];
  const ordinaryText = ordinaryMarkdownText(document);
  const pattern = /(?<!!)\[[^\]\r\n]*\]\(\s*(?:<([^>\r\n]+)>|([^\s)]+))(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/g;
  for (const match of ordinaryText.matchAll(pattern)) links.push(match[1] ?? match[2]);
  return links;
}

function ordinaryMarkdownText(document) {
  return document
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/^[ \t]*(?:```|~~~)[^\r\n]*\r?\n[\s\S]*?^[ \t]*(?:```|~~~)[ \t]*$/gm, "")
    .replace(/`[^`\r\n]*`/g, "");
}

export function discoverLinkedSkillReferences(skillRoot) {
  inspectRoot(skillRoot, "Skill root");
  const skillDocumentPath = inspectRegularFile(skillRoot, "SKILL.md");
  let document;
  try {
    document = fs.readFileSync(skillDocumentPath, "utf8");
  } catch (error) {
    fail(SKILL_GENERATION_TRACE_CODES.SKILL_DOCUMENT_INVALID, "The Skill document could not be read as UTF-8", { cause: error?.code ?? "UNKNOWN" });
  }
  const ordinaryText = ordinaryMarkdownText(document);
  requireAudit(
    !/!\[[^\]\r\n]*\]\(\s*(?:<[^>\r\n]+>|[^\s)]+)|\[[^\]\r\n]+\]\[[^\]\r\n]*\]|<a\s+[^>]*href\s*=/i.test(ordinaryText),
    SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID,
    "Only ordinary inline Markdown links may declare Skill references",
  );
  const references = new Set();
  for (const target of ordinaryMarkdownLinks(document)) {
    requireAudit(!/^[a-z][a-z0-9+.-]*:/i.test(target) && !target.startsWith("#"), SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID, "Skill reference links must be local file paths");
    let normalized;
    try {
      normalized = portableRelativePath(target);
    } catch (error) {
      fail(SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID, "A local Skill link is not a safe relative reference", { cause: error?.code ?? "UNKNOWN" });
    }
    requireAudit(normalized.startsWith("references/"), SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID, "Local Skill links must remain under references/", { path: normalized });
    inspectRegularFile(skillRoot, normalized);
    references.add(normalized);
  }
  return [...references].sort();
}

function validatedLinkedReferences(skillRoot, linkedReferences) {
  requireAudit(Array.isArray(linkedReferences) && linkedReferences.length > 0, SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "linkedReferences must be a non-empty array");
  const discovered = discoverLinkedSkillReferences(skillRoot);
  const normalized = linkedReferences.map((relative) => portableRelativePath(relative));
  requireAudit(new Set(normalized).size === normalized.length, SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "linkedReferences must be unique");
  requireAudit(normalized.every((relative) => relative.startsWith("references/")), SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "linkedReferences must remain under references/");
  requireAudit([...normalized].sort().join("\0") === discovered.join("\0"), SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "linkedReferences must exactly match safe ordinary references discovered from SKILL.md");
  for (const relative of normalized) inspectRegularFile(skillRoot, relative);
  return discovered;
}

function permissionAbsolute(filePath) {
  const resolved = path.resolve(filePath);
  let portable;
  const drive = /^([A-Za-z]):[\\/](.*)$/.exec(resolved);
  if (drive) portable = `${drive[1]}/${drive[2].replaceAll("\\", "/")}`;
  else portable = resolved.split(path.sep).join("/").replace(/^\/+/, "");
  return `//${portable.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)")}`;
}

export function skillGenerationPermissionRules({ workspaceRoot, skillRoot, linkedReferences, sourceRoot = null }) {
  inspectRoot(workspaceRoot, "Workspace root");
  inspectRoot(skillRoot, "Skill root");
  if (sourceRoot !== null) {
    inspectRoot(sourceRoot, "Source root");
    requireAudit(!pathInside(sourceRoot, workspaceRoot) && !pathInside(workspaceRoot, sourceRoot), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, "The audited workspace must be outside the source repository");
  }
  for (const relative of REQUIRED_INPUT_PATHS) inspectRegularFile(workspaceRoot, relative);
  const references = validatedLinkedReferences(skillRoot, linkedReferences);
  return [
    "Skill(wiki-to-diagnosis-skill)",
    "Read(/inputs/wiki.md)",
    "Read(/inputs/clarifications.md)",
    ...references.map((relative) => `Read(${permissionAbsolute(path.join(skillRoot, ...relative.split("/")))})`),
    // Claude Code's Edit(path) permission category authorizes both Edit and
    // Write file operations. The Write tool itself remains the only exposed
    // and trace-audited file mutation tool for this workflow.
    "Edit(/output/generation-spec.json)",
  ];
}

function sameAbsolutePath(left, right) {
  const resolvedLeft = path.resolve(left);
  const resolvedRight = path.resolve(right);
  return process.platform === "win32"
    ? resolvedLeft.toLowerCase() === resolvedRight.toLowerCase()
    : resolvedLeft === resolvedRight;
}

function observedPath(value, { workspaceRoot, skillRoot, linkedReferences, mode }) {
  requireAudit(typeof value === "string" && value.length > 0 && !value.includes("\0"), SKILL_GENERATION_TRACE_CODES.PATH_NOT_NORMALIZED, "Tool paths must be non-empty strings");
  requireAudit(!dotSegmentPath(value), SKILL_GENERATION_TRACE_CODES.PATH_TRAVERSAL, "Tool paths must not contain empty, current, or parent segments");
  const absolute = path.isAbsolute(value);
  const foreignAbsolute = process.platform === "win32"
    ? path.posix.isAbsolute(value)
    : path.win32.isAbsolute(value);
  if (!absolute && foreignAbsolute) {
    fail(SKILL_GENERATION_TRACE_CODES.PATH_ABSOLUTE, "Foreign-platform absolute tool paths are forbidden");
  }
  const workspaceCandidates = mode === "read" ? REQUIRED_INPUT_PATHS : [OUTPUT_PATH];
  if (!absolute) {
    const relative = portableRelativePath(value);
    requireAudit(workspaceCandidates.includes(relative), mode === "read" ? SKILL_GENERATION_TRACE_CODES.READ_UNLINKED : SKILL_GENERATION_TRACE_CODES.WRITE_PATH_INVALID, "Relative tool path is outside the fixed workspace allowlist", { path: relative });
    inspectRegularFile(workspaceRoot, relative);
    return { receiptPath: `workspace/${relative}`, absolutePath: path.join(workspaceRoot, ...relative.split("/")) };
  }

  for (const relative of workspaceCandidates) {
    const candidate = path.join(workspaceRoot, ...relative.split("/"));
    if (sameAbsolutePath(value, candidate)) {
      inspectRegularFile(workspaceRoot, relative);
      requireAudit(sameAbsolutePath(fs.realpathSync.native(value), fs.realpathSync.native(candidate)), SKILL_GENERATION_TRACE_CODES.PATH_SYMLINK, "Absolute workspace path must resolve exactly to its allowed file");
      return { receiptPath: `workspace/${relative}`, absolutePath: candidate };
    }
  }
  if (mode === "read") {
    for (const relative of linkedReferences) {
      const candidate = path.join(skillRoot, ...relative.split("/"));
      if (sameAbsolutePath(value, candidate)) {
        inspectRegularFile(skillRoot, relative);
        requireAudit(sameAbsolutePath(fs.realpathSync.native(value), fs.realpathSync.native(candidate)), SKILL_GENERATION_TRACE_CODES.PATH_SYMLINK, "Absolute Skill reference path must resolve exactly to its discovered file");
        return { receiptPath: `skill/${relative}`, absolutePath: candidate };
      }
    }
  }
  fail(mode === "read" ? SKILL_GENERATION_TRACE_CODES.READ_UNLINKED : SKILL_GENERATION_TRACE_CODES.WRITE_PATH_INVALID, "Absolute tool path is outside the exact audited allowlist");
}

function normalizeRequiredReferences(values, links) {
  requireAudit(Array.isArray(values) && values.length > 0, SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "At least one required Skill reference is needed");
  const normalized = values.map((value) => {
    let candidate;
    try {
      candidate = portableRelativePath(value);
    } catch (error) {
      fail(SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "A required Skill reference is invalid", { cause: error?.code ?? "UNKNOWN" });
    }
    requireAudit(candidate.startsWith("references/") && links.has(candidate), SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "A required Skill reference is not linked by SKILL.md", { path: candidate });
    return candidate;
  });
  requireAudit(new Set(normalized).size === normalized.length, SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "Required Skill references must be unique");
  return normalized;
}

function parseToolRecords(events) {
  const records = [];
  const byId = new Map();
  for (const [eventIndex, event] of events.entries()) {
    const content = event?.message?.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (block?.type === "tool_use") {
        requireAudit(event.type === "assistant" && event.message?.role === "assistant", SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID, "tool_use must be emitted by an assistant message");
        requireAudit(ALLOWED_TOOLS.includes(block.name), SKILL_GENERATION_TRACE_CODES.TOOL_NOT_ALLOWED, `Tool is not allowed: ${String(block.name)}`);
        requireAudit(typeof block.id === "string" && block.id.length > 0 && !byId.has(block.id), SKILL_GENERATION_TRACE_CODES.TOOL_USE_ID_INVALID, "tool_use IDs must be non-empty and unique");
        const record = {
          ordinal: records.length,
          id: block.id,
          tool: block.name,
          input: block.input,
          result: null,
          use_event_index: eventIndex,
          result_event_index: null,
        };
        records.push(record);
        byId.set(record.id, record);
      } else if (block?.type === "tool_result") {
        requireAudit(event.type === "user" && event.message?.role === "user", SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID, "tool_result must be emitted by a user message");
        const record = byId.get(block.tool_use_id);
        requireAudit(record !== undefined, SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_UNMATCHED, "tool_result does not match a tool_use");
        requireAudit(record.result === null, SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_DUPLICATE, "A tool_use has more than one tool_result");
        const raw = event.tool_use_result;
        const failed = block.is_error === true
          || raw?.isError === true
          || raw?.is_error === true
          || raw?.success === false;
        record.result = {
          raw,
          outcome: failed ? "ERROR" : "SUCCESS",
          explicit_error: block.is_error === true,
          contradictory_success: failed && raw?.success === true,
        };
        record.result_event_index = eventIndex;
      }
    }
  }
  requireAudit(records.every((record) => record.result !== null), SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_MISSING, "Every tool_use must have exactly one tool_result");
  return records;
}

function emptyWriteValidationRejection(record) {
  return record.tool === "Write"
    && exactKeys(record.input, [])
    && record.result?.outcome === "ERROR"
    && record.result.explicit_error === true
    && record.result.contradictory_success === false;
}

function validateSkillInvocation(records) {
  const skills = records.filter((record) => record.tool === "Skill");
  requireAudit(skills.length === 1 && skills[0].ordinal === 0 && exactKeys(skills[0].input, ["skill"]) && skills[0].input.skill === "wiki-to-diagnosis-skill", SKILL_GENERATION_TRACE_CODES.SKILL_INVOCATION_INVALID, "The first and only Skill call must load wiki-to-diagnosis-skill with exact input");
  requireAudit(isPlainObject(skills[0].result.raw) && skills[0].result.raw.success === true, SKILL_GENERATION_TRACE_CODES.SKILL_RESULT_INVALID, "The Skill tool_result must explicitly report success");
  requireAudit(records.slice(1).every((record) => skills[0].result_event_index < record.use_event_index), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "The Skill must finish loading before any Read or Write starts");
}

function validateReadInput(record) {
  requireAudit(isPlainObject(record.input) && typeof record.input.file_path === "string", SKILL_GENERATION_TRACE_CODES.READ_INPUT_INVALID, "Read input must contain file_path");
  const allowed = new Set(["file_path", "offset", "limit", "pages"]);
  requireAudit(Object.keys(record.input).every((key) => allowed.has(key)), SKILL_GENERATION_TRACE_CODES.READ_INPUT_INVALID, "Read input contains an unsupported field");
  return record.input.file_path;
}

function validateWriteInput(record) {
  requireAudit(
    exactKeys(record.input, ["file_path", "content"])
      && typeof record.input.file_path === "string"
      && typeof record.input.content === "string"
      && record.input.content.trim().length > 0,
    SKILL_GENERATION_TRACE_CODES.WRITE_INPUT_INVALID,
    "Write input must contain only non-empty string file_path and content fields",
  );
  return record.input.file_path;
}

function sha256File(filePath) {
  return crypto.createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
}

function validReceiptPath(value, prefixes) {
  try {
    const normalized = portableRelativePath(value);
    return prefixes.some((prefix) => normalized.startsWith(prefix));
  } catch {
    return false;
  }
}

export function validSkillGenerationTraceAuditReceipt(value) {
  if (!exactKeys(value, [
    "schema_version", "status", "workflow", "skill", "tool_inventory", "permission_mode",
    "permission_policy_sha256", "attempt_policy", "attempt_policy_sha256", "tool_sequence",
    "accepted_validation_rejections", "required_reads", "observed_reads", "linked_references",
    "output", "terminal",
  ])) return false;
  if (value.schema_version !== SKILL_GENERATION_TRACE_SCHEMA_VERSION
    || value.status !== "PASS"
    || value.workflow !== "skill-generation"
    || value.skill !== "wiki-to-diagnosis-skill"
    || value.permission_mode !== "dontAsk"
    || JSON.stringify(value.tool_inventory) !== JSON.stringify(ALLOWED_TOOLS)
    || !/^[a-f0-9]{64}$/.test(value.permission_policy_sha256 ?? "")
    || JSON.stringify(value.attempt_policy) !== JSON.stringify(SKILL_GENERATION_TOOL_ATTEMPT_POLICY)
    || value.attempt_policy_sha256 !== crypto.createHash("sha256").update(JSON.stringify(SKILL_GENERATION_TOOL_ATTEMPT_POLICY)).digest("hex")) return false;
  if (!Array.isArray(value.tool_sequence)
    || value.tool_sequence.length === 0
    || !Array.isArray(value.accepted_validation_rejections)
    || !Array.isArray(value.required_reads)
    || value.required_reads.length === 0
    || !Array.isArray(value.observed_reads)
    || !Array.isArray(value.linked_references)) return false;
  if (!value.tool_sequence.every((record, ordinal) => record?.ordinal === ordinal)) return false;
  if (!value.tool_sequence.every((record) => ALLOWED_TOOLS.includes(record?.tool))) return false;

  const skillRecords = value.tool_sequence.filter((record) => record?.tool === "Skill");
  const readRecords = value.tool_sequence.filter((record) => record?.tool === "Read");
  const writeRecords = value.tool_sequence.filter((record) => record?.tool === "Write");
  const successfulWrites = writeRecords.filter((record) => record?.outcome === "SUCCESS");
  const rejectedWrites = writeRecords.filter((record) => record?.outcome === "REJECTED");
  if (skillRecords.length !== 1
    || !exactKeys(skillRecords[0], ["ordinal", "tool", "outcome"])
    || skillRecords[0].ordinal !== 0
    || skillRecords[0].outcome !== "SUCCESS"
    || successfulWrites.length !== 1
    || successfulWrites[0].ordinal !== value.tool_sequence.length - 1
    || !exactKeys(successfulWrites[0], ["ordinal", "tool", "outcome", "path"])
    || successfulWrites[0].path !== "workspace/output/generation-spec.json"
    || rejectedWrites.length > SKILL_GENERATION_TOOL_ATTEMPT_POLICY.max_empty_write_rejections
    || writeRecords.length !== successfulWrites.length + rejectedWrites.length) return false;
  if (!readRecords.every((record) => exactKeys(record, ["ordinal", "tool", "outcome", "path"])
    && record.outcome === "SUCCESS"
    && validReceiptPath(record.path, ["workspace/inputs/", "skill/references/"]))) return false;
  if (!rejectedWrites.every((record) => exactKeys(record, ["ordinal", "tool", "outcome", "classification"])
    && record.classification === "EMPTY_INPUT_REQUIRED_FIELDS_ABSENT"
    && record.ordinal === successfulWrites[0].ordinal - 1)) return false;

  if (value.accepted_validation_rejections.length !== rejectedWrites.length
    || !value.accepted_validation_rejections.every((record, index) => exactKeys(record, ["ordinal", "tool", "classification", "input_key_names", "result_completed_before_success"])
      && record.ordinal === rejectedWrites[index].ordinal
      && record.tool === "Write"
      && record.classification === "EMPTY_INPUT_REQUIRED_FIELDS_ABSENT"
      && Array.isArray(record.input_key_names)
      && record.input_key_names.length === 0
      && record.result_completed_before_success === true)) return false;
  if (JSON.stringify(value.required_reads) !== JSON.stringify(DEFAULT_REQUIRED_RECEIPT_READS)
    || !value.required_reads.every((readPath) => validReceiptPath(readPath, ["workspace/inputs/", "skill/references/"]))
    || new Set(value.linked_references).size !== value.linked_references.length
    || !value.linked_references.every((readPath) => validReceiptPath(readPath, ["skill/references/"]))
    || !DEFAULT_REQUIRED_RECEIPT_READS.filter((readPath) => readPath.startsWith("skill/")).every((readPath) => value.linked_references.includes(readPath))
    || !readRecords.filter((record) => record.path.startsWith("skill/")).every((record) => value.linked_references.includes(record.path))) return false;
  const expectedObservedReads = readRecords.map((record) => ({ ordinal: record.ordinal, path: record.path }));
  if (JSON.stringify(value.observed_reads) !== JSON.stringify(expectedObservedReads)
    || !value.observed_reads.every((record) => exactKeys(record, ["ordinal", "path"])
      && Number.isSafeInteger(record.ordinal)
      && record.ordinal > 0
      && record.ordinal < successfulWrites[0].ordinal)) return false;
  if (!value.required_reads.every((required) => value.observed_reads.some((record) => record.path === required))) return false;
  if (!exactKeys(value.output, ["ordinal", "path", "size_bytes", "sha256"])
    || value.output.ordinal !== successfulWrites[0].ordinal
    || value.output.path !== successfulWrites[0].path
    || !Number.isSafeInteger(value.output.size_bytes)
    || value.output.size_bytes <= 0
    || !/^[a-f0-9]{64}$/.test(value.output.sha256 ?? "")) return false;
  return exactKeys(value.terminal, ["subtype", "is_error"])
    && value.terminal.subtype === "success"
    && value.terminal.is_error === false;
}

/**
 * Audits a completed Claude stream-json trace for the isolated Wiki conversion
 * workflow. requiredReferencePaths are relative to skillRoot. All tool paths
 * and every path returned in the receipt are relative to workspaceRoot.
 */
export function auditSkillGenerationTrace({
  events,
  workspaceRoot,
  skillRoot,
  sourceRoot = null,
  requiredReferencePaths = DEFAULT_REQUIRED_REFERENCES,
}) {
  requireAudit(Array.isArray(events) && events.length > 0 && events.every(isPlainObject), SKILL_GENERATION_TRACE_CODES.EVENTS_INVALID, "events must be a non-empty array of stream-json objects");
  requireAudit(!events.some((event) => event.type === "error"), SKILL_GENERATION_TRACE_CODES.STREAM_ERROR, "The stream contains an explicit error event");
  inspectRoot(workspaceRoot, "Workspace root");
  inspectRoot(skillRoot, "Skill root");
  if (sourceRoot !== null) {
    inspectRoot(sourceRoot, "Source root");
    requireAudit(!pathInside(sourceRoot, workspaceRoot) && !pathInside(workspaceRoot, sourceRoot), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, "The audited workspace must be outside the source repository");
  }

  const initEvents = events.filter((event) => event.type === "system" && event.subtype === "init");
  requireAudit(initEvents.length === 1, SKILL_GENERATION_TRACE_CODES.INIT_INVALID, "The trace must contain exactly one init event");
  const init = initEvents[0];
  requireAudit(typeof init.cwd === "string" && path.resolve(init.cwd) === path.resolve(workspaceRoot), SKILL_GENERATION_TRACE_CODES.INIT_CWD_MISMATCH, "The init cwd must equal the audited workspace");
  requireAudit(init.permissionMode === "dontAsk", SKILL_GENERATION_TRACE_CODES.PERMISSION_MODE_INVALID, "The effective permission mode must be dontAsk");
  requireAudit(Array.isArray(init.tools) && init.tools.length === ALLOWED_TOOLS.length && new Set(init.tools).size === ALLOWED_TOOLS.length && ALLOWED_TOOLS.every((tool) => init.tools.includes(tool)), SKILL_GENERATION_TRACE_CODES.TOOL_INVENTORY_INVALID, "The init tool inventory must contain only Skill, Read and Write");

  const terminalEvents = events.filter((event) => event.type === "result");
  requireAudit(terminalEvents.length === 1 && events.at(-1) === terminalEvents[0], SKILL_GENERATION_TRACE_CODES.RESULT_INVALID, "The trace must end with exactly one result event");
  const terminal = terminalEvents[0];
  requireAudit(terminal.subtype === "success" && terminal.is_error === false, SKILL_GENERATION_TRACE_CODES.RESULT_NOT_SUCCESS, "The terminal result must report success");

  const linkedReferences = discoverLinkedSkillReferences(skillRoot);
  const links = new Set(linkedReferences);
  const requiredReferences = normalizeRequiredReferences(requiredReferencePaths, links);
  const requiredReads = [
    ...REQUIRED_INPUT_PATHS.map((relative) => `workspace/${relative}`),
    ...requiredReferences.map((relative) => `skill/${relative}`),
  ];

  for (const inputPath of REQUIRED_INPUT_PATHS) inspectRegularFile(workspaceRoot, inputPath);
  const records = parseToolRecords(events);
  const failedRecords = records.filter((record) => record.result.outcome === "ERROR");
  const acceptedValidationRejections = failedRecords.filter(emptyWriteValidationRejection);
  requireAudit(
    failedRecords.length === acceptedValidationRejections.length,
    SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_ERROR,
    "Only one explicitly failed, strictly empty Write input may be treated as a non-mutating validation rejection",
  );
  requireAudit(
    acceptedValidationRejections.length <= SKILL_GENERATION_TOOL_ATTEMPT_POLICY.max_empty_write_rejections,
    SKILL_GENERATION_TRACE_CODES.WRITE_COUNT_INVALID,
    "At most one strictly empty Write validation rejection is allowed",
  );
  validateSkillInvocation(records);

  const reads = [];
  for (const record of records.filter((candidate) => candidate.tool === "Read")) {
    const toolPath = validateReadInput(record);
    const normalized = observedPath(toolPath, { workspaceRoot, skillRoot, linkedReferences, mode: "read" });
    if (requiredReads.includes(normalized.receiptPath)) {
      requireAudit(!Object.hasOwn(record.input, "offset") && !Object.hasOwn(record.input, "limit") && !Object.hasOwn(record.input, "pages"), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_PARTIAL, "Every required input and contract reference must be read in full", { path: normalized.receiptPath });
    }
    reads.push({ ordinal: record.ordinal, path: normalized.receiptPath, result_event_index: record.result_event_index });
  }
  const observedReadPaths = new Set(reads.map((read) => read.path));
  const missingReads = requiredReads.filter((relative) => !observedReadPaths.has(relative));
  requireAudit(missingReads.length === 0, SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_MISSING, "The trace did not read every required input and Skill reference", { paths: missingReads });

  const writes = records.filter((candidate) => candidate.tool === "Write");
  const successfulWrites = writes.filter((record) => record.result.outcome === "SUCCESS");
  requireAudit(successfulWrites.length === 1, SKILL_GENERATION_TRACE_CODES.WRITE_COUNT_INVALID, "The trace must contain exactly one successful Write invocation");
  const successfulWrite = successfulWrites[0];
  requireAudit(successfulWrite.ordinal === records.length - 1, SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "The successful Write must be the final tool invocation");
  for (const requiredRead of requiredReads) {
    requireAudit(reads.some((read) => read.path === requiredRead && read.result_event_index < successfulWrite.use_event_index), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "Every required Read must finish before the successful Write starts", { path: requiredRead });
  }
  if (acceptedValidationRejections.length === 1) {
    const rejectedWrite = acceptedValidationRejections[0];
    requireAudit(rejectedWrite.ordinal === successfulWrite.ordinal - 1, SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "The empty Write validation rejection must immediately precede the successful Write");
    requireAudit(rejectedWrite.result_event_index < successfulWrite.use_event_index, SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "The empty Write validation rejection must finish before the successful Write starts");
    for (const requiredRead of requiredReads) {
      requireAudit(reads.some((read) => read.path === requiredRead && read.result_event_index < rejectedWrite.use_event_index), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "Every required Read must finish before an empty Write validation rejection", { path: requiredRead });
    }
  }
  const writeToolPath = validateWriteInput(successfulWrite);
  const normalizedWrite = observedPath(writeToolPath, { workspaceRoot, skillRoot, linkedReferences, mode: "write" });
  const writePath = normalizedWrite.receiptPath;
  const outputAbsolute = normalizedWrite.absolutePath;
  requireAudit(fs.readFileSync(outputAbsolute, "utf8") === successfulWrite.input.content, SKILL_GENERATION_TRACE_CODES.WRITE_CONTENT_MISMATCH, "The output bytes do not match the successful Write input");

  return {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "PASS",
    workflow: "skill-generation",
    skill: "wiki-to-diagnosis-skill",
    tool_inventory: [...ALLOWED_TOOLS],
    permission_mode: init.permissionMode,
    permission_policy_sha256: crypto.createHash("sha256").update(JSON.stringify(
      skillGenerationPermissionRules({ workspaceRoot, skillRoot, linkedReferences, sourceRoot }),
    )).digest("hex"),
    attempt_policy: SKILL_GENERATION_TOOL_ATTEMPT_POLICY,
    attempt_policy_sha256: crypto.createHash("sha256").update(JSON.stringify(
      SKILL_GENERATION_TOOL_ATTEMPT_POLICY,
    )).digest("hex"),
    tool_sequence: records.map((record) => {
      if (record.tool === "Read") return {
        ordinal: record.ordinal,
        tool: record.tool,
        outcome: "SUCCESS",
        path: observedPath(validateReadInput(record), { workspaceRoot, skillRoot, linkedReferences, mode: "read" }).receiptPath,
      };
      if (record === acceptedValidationRejections[0]) return {
        ordinal: record.ordinal,
        tool: record.tool,
        outcome: "REJECTED",
        classification: "EMPTY_INPUT_REQUIRED_FIELDS_ABSENT",
      };
      if (record.tool === "Write") return { ordinal: record.ordinal, tool: record.tool, outcome: "SUCCESS", path: writePath };
      return { ordinal: record.ordinal, tool: record.tool, outcome: "SUCCESS" };
    }),
    accepted_validation_rejections: acceptedValidationRejections.map((record) => ({
      ordinal: record.ordinal,
      tool: "Write",
      classification: "EMPTY_INPUT_REQUIRED_FIELDS_ABSENT",
      input_key_names: [],
      result_completed_before_success: true,
    })),
    required_reads: requiredReads,
    observed_reads: reads.map(({ ordinal, path: readPath }) => ({ ordinal, path: readPath })),
    linked_references: linkedReferences.map((relative) => `skill/${relative}`),
    output: {
      ordinal: successfulWrite.ordinal,
      path: writePath,
      size_bytes: fs.statSync(outputAbsolute).size,
      sha256: sha256File(outputAbsolute),
    },
    terminal: { subtype: terminal.subtype, is_error: terminal.is_error },
  };
}
