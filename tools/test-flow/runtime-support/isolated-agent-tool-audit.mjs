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
  SOURCE_IDENTITY_INVALID: "SKILL_TRACE_SOURCE_IDENTITY_INVALID",
  SOURCE_LOG_TEMPLATES_INVALID: "SKILL_TRACE_SOURCE_LOG_TEMPLATES_INVALID",
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
  OUTPUT_TREE_INVALID: "SKILL_TRACE_OUTPUT_TREE_INVALID",
  METHODS_CONTRACT_INVALID: "SKILL_TRACE_METHODS_CONTRACT_INVALID",
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
const REQUIRED_INPUT_PATH = "inputs/wiki.md";
const REQUIRED_SOURCE_IDENTITY_PATH = "runtime/source-wiki-identity.json";
const REQUIRED_REFERENCE_PATH = "references/output-contract.md";
const SOURCE_LOG_TEMPLATES_REFERENCE = "references/source-log-templates.md";
const REQUIRED_RECEIPT_READS = Object.freeze([
  `workspace/${REQUIRED_INPUT_PATH}`,
  `workspace/${REQUIRED_SOURCE_IDENTITY_PATH}`,
  `skill/${REQUIRED_REFERENCE_PATH}`,
]);
const SKILL_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const REFERENCE_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*\.md$/;

export const SKILL_GENERATION_TRACE_SCHEMA_VERSION = 6;

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

function uniqueNonEmptyStrings(value) {
  return Array.isArray(value)
    && value.length > 0
    && value.every((item) => typeof item === "string" && item.length > 0)
    && value.length === new Set(value).size;
}

function orderedSubsequence(values, sequence) {
  let cursor = 0;
  for (const value of values) {
    const index = sequence.indexOf(value, cursor);
    if (index < 0) return false;
    cursor = index + 1;
  }
  return true;
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (isPlainObject(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

function sha256Bytes(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function decodeUtf8(bytes, code, message) {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    fail(code, message);
  }
}

function wikiLogTemplates(text) {
  const templates = [];
  let inTextFence = false;
  for (const rawLine of text.split(/\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/u)) {
    const stripped = rawLine.trim();
    if (stripped === "```text") {
      inTextFence = true;
      continue;
    }
    if (stripped === "```" && inTextFence) {
      inTextFence = false;
      continue;
    }
    if (!inTextFence || stripped.length === 0) continue;
    if (/\{[A-Za-z_][A-Za-z0-9_]*\}|%[A-Za-z]/.test(stripped)) templates.push(stripped);
  }
  return templates;
}

function templateInventorySha256(templates) {
  return sha256Bytes(canonicalJson({ version: 1, templates }));
}

function sourceLogTemplatesBytes(templates) {
  return Buffer.from(`# Source log templates\n\n\`\`\`text\n${templates.join("\n")}\n\`\`\`\n`, "utf8");
}

function portableRelativePath(value) {
  requireAudit(typeof value === "string" && value.length > 0 && !value.includes("\0"), SKILL_GENERATION_TRACE_CODES.PATH_NOT_NORMALIZED, "Tool paths must be non-empty portable paths");
  requireAudit(!path.posix.isAbsolute(value) && !path.win32.isAbsolute(value), SKILL_GENERATION_TRACE_CODES.PATH_ABSOLUTE, "Expected a relative tool path");
  const slashed = value.replaceAll("\\", "/");
  const segments = slashed.split("/");
  requireAudit(!segments.includes(".."), SKILL_GENERATION_TRACE_CODES.PATH_TRAVERSAL, "Tool paths must not traverse parent directories");
  requireAudit(!segments.some((segment) => segment === "" || segment === ".") && slashed === value && path.posix.normalize(slashed) === slashed, SKILL_GENERATION_TRACE_CODES.PATH_NOT_NORMALIZED, "Tool paths must use normalized forward-slash syntax");
  return slashed;
}

function pathInside(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function sameAbsolutePath(left, right) {
  const a = path.resolve(left);
  const b = path.resolve(right);
  return process.platform === "win32" ? a.toLowerCase() === b.toLowerCase() : a === b;
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
  requireAudit(sameAbsolutePath(real, root), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} must not contain symlinked path components`);
}

function inspectDirectory(root, relativePath) {
  let current = path.resolve(root);
  for (const segment of portableRelativePath(relativePath).split("/")) {
    current = path.join(current, segment);
    let metadata;
    try {
      metadata = fs.lstatSync(current);
    } catch (error) {
      fail(SKILL_GENERATION_TRACE_CODES.PATH_MISSING, `Audited directory does not exist: ${relativePath}`, { cause: error?.code ?? "UNKNOWN" });
    }
    requireAudit(!metadata.isSymbolicLink(), SKILL_GENERATION_TRACE_CODES.PATH_SYMLINK, `Audited paths must not contain symlinks: ${relativePath}`);
    requireAudit(metadata.isDirectory(), SKILL_GENERATION_TRACE_CODES.PATH_KIND_INVALID, `Audited path is not a directory: ${relativePath}`);
  }
  requireAudit(pathInside(root, current), SKILL_GENERATION_TRACE_CODES.PATH_TRAVERSAL, `Audited path escapes its root: ${relativePath}`);
  return current;
}

function inspectRegularFile(root, relativePath) {
  const normalized = portableRelativePath(relativePath);
  const segments = normalized.split("/");
  let current = path.resolve(root);
  for (const [index, segment] of segments.entries()) {
    current = path.join(current, segment);
    let metadata;
    try {
      metadata = fs.lstatSync(current);
    } catch (error) {
      fail(SKILL_GENERATION_TRACE_CODES.PATH_MISSING, `Audited path does not exist: ${relativePath}`, { cause: error?.code ?? "UNKNOWN" });
    }
    requireAudit(!metadata.isSymbolicLink(), SKILL_GENERATION_TRACE_CODES.PATH_SYMLINK, `Audited paths must not contain symlinks: ${relativePath}`);
    const final = index === segments.length - 1;
    requireAudit(final ? metadata.isFile() : metadata.isDirectory(), SKILL_GENERATION_TRACE_CODES.PATH_KIND_INVALID, `Audited path has an invalid node kind: ${relativePath}`);
    if (final) requireAudit(metadata.nlink === 1, SKILL_GENERATION_TRACE_CODES.PATH_HARDLINK, `Audited files must not be hardlinked: ${relativePath}`);
  }
  requireAudit(pathInside(root, current), SKILL_GENERATION_TRACE_CODES.PATH_TRAVERSAL, `Audited path escapes its root: ${relativePath}`);
  return current;
}

function inspectSourceIdentity(workspaceRoot) {
  const wikiPath = inspectRegularFile(workspaceRoot, REQUIRED_INPUT_PATH);
  const identityPath = inspectRegularFile(workspaceRoot, REQUIRED_SOURCE_IDENTITY_PATH);
  const wikiBytes = fs.readFileSync(wikiPath);
  const wikiTemplates = wikiLogTemplates(decodeUtf8(
    wikiBytes,
    SKILL_GENERATION_TRACE_CODES.SOURCE_IDENTITY_INVALID,
    "inputs/wiki.md must be UTF-8",
  ));
  let bytes;
  let identity;
  try {
    bytes = fs.readFileSync(identityPath);
    identity = JSON.parse(decodeUtf8(
      bytes,
      SKILL_GENERATION_TRACE_CODES.SOURCE_IDENTITY_INVALID,
      "Source Wiki identity must be UTF-8 JSON",
    ));
  } catch {
    fail(SKILL_GENERATION_TRACE_CODES.SOURCE_IDENTITY_INVALID, "Source Wiki identity must be UTF-8 JSON");
  }
  requireAudit(
    exactKeys(identity, [
      "algorithm", "schema_version", "sha256", "source_path",
      "log_template_extraction_version", "log_templates", "log_template_inventory_sha256",
    ])
      && identity.schema_version === 2
      && identity.algorithm === "sha256"
      && identity.source_path === REQUIRED_INPUT_PATH
      && /^[a-f0-9]{64}$/.test(identity.sha256 ?? "")
      && identity.log_template_extraction_version === 1
      && Array.isArray(identity.log_templates)
      && identity.log_templates.every((template) => typeof template === "string" && template.length > 0)
      && /^[a-f0-9]{64}$/.test(identity.log_template_inventory_sha256 ?? ""),
    SKILL_GENERATION_TRACE_CODES.SOURCE_IDENTITY_INVALID,
    "Source Wiki identity does not match its closed schema",
  );
  requireAudit(
    bytes.equals(Buffer.from(`${canonicalJson(identity)}\n`, "utf8")),
    SKILL_GENERATION_TRACE_CODES.SOURCE_IDENTITY_INVALID,
    "Source Wiki identity must use canonical JSON bytes",
  );
  requireAudit(
    identity.sha256 === sha256Bytes(wikiBytes),
    SKILL_GENERATION_TRACE_CODES.SOURCE_IDENTITY_INVALID,
    "Source Wiki identity digest does not match inputs/wiki.md",
  );
  requireAudit(
    JSON.stringify(identity.log_templates) === JSON.stringify(wikiTemplates)
      && identity.log_template_inventory_sha256 === templateInventorySha256(wikiTemplates),
    SKILL_GENERATION_TRACE_CODES.SOURCE_IDENTITY_INVALID,
    "Source Wiki log template inventory does not match inputs/wiki.md",
  );
  return identity;
}

function ordinaryMarkdownText(document) {
  return document
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/^[ \t]*(?:```|~~~)[^\r\n]*\r?\n[\s\S]*?^[ \t]*(?:```|~~~)[ \t]*$/gm, "")
    .replace(/`[^`\r\n]*`/g, "");
}

function ordinaryMarkdownLinks(document) {
  const links = [];
  const pattern = /(?<!!)\[[^\]\r\n]*\]\(\s*(?:<([^>\r\n]+)>|([^\s)]+))(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/g;
  for (const match of ordinaryMarkdownText(document).matchAll(pattern)) links.push(match[1] ?? match[2]);
  return links;
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
  requireAudit(!/!\[[^\]\r\n]*\]\(\s*(?:<[^>\r\n]+>|[^\s)]+)|\[[^\]\r\n]+\]\[[^\]\r\n]*\]|<a\s+[^>]*href\s*=/i.test(ordinaryText), SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID, "Only ordinary inline Markdown links may declare Skill references");
  const references = new Set();
  for (const target of ordinaryMarkdownLinks(document)) {
    requireAudit(!/^[a-z][a-z0-9+.-]*:/i.test(target) && !target.startsWith("#"), SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID, "Skill reference links must be local file paths");
    let normalized;
    try {
      normalized = portableRelativePath(target);
    } catch (error) {
      fail(SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID, "A local Skill link is not a safe relative reference", { cause: error?.code ?? "UNKNOWN" });
    }
    requireAudit(normalized.startsWith("references/"), SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID, "Local Skill links must remain under references/");
    inspectRegularFile(skillRoot, normalized);
    references.add(normalized);
  }
  const discovered = [...references].sort();
  requireAudit(discovered.length === 1 && discovered[0] === REQUIRED_REFERENCE_PATH, SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "The meta Skill must link exactly references/output-contract.md");
  return discovered;
}

function validatedLinkedReferences(skillRoot, linkedReferences) {
  requireAudit(Array.isArray(linkedReferences), SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "linkedReferences must be an array");
  const discovered = discoverLinkedSkillReferences(skillRoot);
  const normalized = linkedReferences.map((relative) => portableRelativePath(relative));
  requireAudit(normalized.length === 1 && normalized[0] === REQUIRED_REFERENCE_PATH && normalized.join("\0") === discovered.join("\0"), SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "linkedReferences must exactly match the meta Skill output contract");
  return discovered;
}

function permissionAbsolute(filePath) {
  const resolved = path.resolve(filePath);
  const drive = /^([A-Za-z]):[\\/](.*)$/.exec(resolved);
  const portable = drive
    ? `${drive[1]}/${drive[2].replaceAll("\\", "/")}`
    : resolved.split(path.sep).join("/").replace(/^\/+/, "");
  return `//${portable.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)")}`;
}

export function skillGenerationPermissionRules({ workspaceRoot, skillRoot, linkedReferences, sourceRoot = null }) {
  inspectRoot(workspaceRoot, "Workspace root");
  inspectRoot(skillRoot, "Skill root");
  if (sourceRoot !== null) {
    inspectRoot(sourceRoot, "Source root");
    requireAudit(!pathInside(sourceRoot, workspaceRoot) && !pathInside(workspaceRoot, sourceRoot), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, "The audited workspace must be outside the source repository");
  }
  inspectSourceIdentity(workspaceRoot);
  const references = validatedLinkedReferences(skillRoot, linkedReferences);
  return [
    "Skill(wiki-to-diagnosis-skill)",
    "Read(/inputs/wiki.md)",
    "Read(/runtime/source-wiki-identity.json)",
    `Read(${permissionAbsolute(path.join(skillRoot, ...references[0].split("/")))})`,
    // Claude Code's Edit permission category authorizes Write. The trace audit
    // closes this wildcard to one Methods package and its exact files.
    "Edit(/output/**)",
  ];
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
        const record = { ordinal: records.length, id: block.id, tool: block.name, input: block.input, result: null, use_event_index: eventIndex, result_event_index: null };
        records.push(record);
        byId.set(record.id, record);
      } else if (block?.type === "tool_result") {
        requireAudit(event.type === "user" && event.message?.role === "user", SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID, "tool_result must be emitted by a user message");
        const record = byId.get(block.tool_use_id);
        requireAudit(record !== undefined, SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_UNMATCHED, "tool_result does not match a tool_use");
        requireAudit(record.result === null, SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_DUPLICATE, "A tool_use has more than one tool_result");
        const raw = event.tool_use_result;
        const failed = block.is_error === true || raw?.isError === true || raw?.is_error === true || raw?.success === false;
        record.result = { raw, outcome: failed ? "ERROR" : "SUCCESS" };
        record.result_event_index = eventIndex;
      }
    }
  }
  requireAudit(records.every((record) => record.result !== null), SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_MISSING, "Every tool_use must have exactly one tool_result");
  requireAudit(records.every((record) => record.result.outcome === "SUCCESS"), SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_ERROR, "Failed tool calls are forbidden in Methods generation");
  return records;
}

function validateSkillInvocation(records) {
  const skills = records.filter((record) => record.tool === "Skill");
  requireAudit(skills.length === 1 && skills[0].ordinal === 0 && exactKeys(skills[0].input, ["skill"]) && skills[0].input.skill === "wiki-to-diagnosis-skill", SKILL_GENERATION_TRACE_CODES.SKILL_INVOCATION_INVALID, "The first and only Skill call must load wiki-to-diagnosis-skill with exact input");
  requireAudit(isPlainObject(skills[0].result.raw) && skills[0].result.raw.success === true, SKILL_GENERATION_TRACE_CODES.SKILL_RESULT_INVALID, "The Skill tool_result must explicitly report success");
  requireAudit(records.slice(1).every((record) => skills[0].result_event_index < record.use_event_index), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "The Skill must finish loading before any Read or Write starts");
}

function validateReadInput(record) {
  requireAudit(exactKeys(record.input, ["file_path"]) && typeof record.input.file_path === "string", SKILL_GENERATION_TRACE_CODES.READ_INPUT_INVALID, "Read input must contain only file_path; partial reads are forbidden");
  return record.input.file_path;
}

function observedReadPath(toolPath, { workspaceRoot, skillRoot }) {
  if (path.isAbsolute(toolPath)) {
    if (sameAbsolutePath(toolPath, path.join(workspaceRoot, REQUIRED_INPUT_PATH))) {
      inspectRegularFile(workspaceRoot, REQUIRED_INPUT_PATH);
      return `workspace/${REQUIRED_INPUT_PATH}`;
    }
    if (sameAbsolutePath(toolPath, path.join(workspaceRoot, REQUIRED_SOURCE_IDENTITY_PATH))) {
      inspectSourceIdentity(workspaceRoot);
      return `workspace/${REQUIRED_SOURCE_IDENTITY_PATH}`;
    }
    if (sameAbsolutePath(toolPath, path.join(skillRoot, REQUIRED_REFERENCE_PATH))) {
      inspectRegularFile(skillRoot, REQUIRED_REFERENCE_PATH);
      return `skill/${REQUIRED_REFERENCE_PATH}`;
    }
    fail(SKILL_GENERATION_TRACE_CODES.READ_UNLINKED, "Read may access only inputs/wiki.md, its source identity, and the linked output contract");
  }
  const relative = portableRelativePath(toolPath);
  requireAudit(
    relative === REQUIRED_INPUT_PATH || relative === REQUIRED_SOURCE_IDENTITY_PATH,
    SKILL_GENERATION_TRACE_CODES.READ_UNLINKED,
    "Relative Read paths may access only inputs/wiki.md or runtime/source-wiki-identity.json",
  );
  if (relative === REQUIRED_SOURCE_IDENTITY_PATH) inspectSourceIdentity(workspaceRoot);
  else inspectRegularFile(workspaceRoot, relative);
  return `workspace/${relative}`;
}

function validateWriteInput(record) {
  requireAudit(exactKeys(record.input, ["file_path", "content"])
    && typeof record.input.file_path === "string"
    && typeof record.input.content === "string"
    && record.input.content.length > 0,
  SKILL_GENERATION_TRACE_CODES.WRITE_INPUT_INVALID,
  "Write input must contain only non-empty string file_path and content fields");
}

function normalizedWorkspaceWrite(toolPath, workspaceRoot) {
  let relative;
  if (path.isAbsolute(toolPath)) {
    requireAudit(pathInside(workspaceRoot, toolPath), SKILL_GENERATION_TRACE_CODES.WRITE_PATH_INVALID, "Write path must remain inside the isolated workspace");
    relative = path.relative(workspaceRoot, path.resolve(toolPath)).split(path.sep).join("/");
  } else {
    relative = portableRelativePath(toolPath);
  }
  const match = /^output\/([a-z0-9]+(?:-[a-z0-9]+)*)\/(SKILL\.md|methods\.json|references\/([a-z0-9]+(?:-[a-z0-9]+)*\.md))$/.exec(relative);
  requireAudit(match !== null, SKILL_GENERATION_TRACE_CODES.WRITE_PATH_INVALID, "Write paths must be output/<skill>/{SKILL.md,methods.json,references/*.md}", { path: relative });
  return { relative, skillName: match[1], absolute: path.join(workspaceRoot, ...relative.split("/")) };
}

function inspectOutputPackage(workspaceRoot, skillName, writes, sourceIdentity) {
  requireAudit(SKILL_NAME.test(skillName), SKILL_GENERATION_TRACE_CODES.OUTPUT_TREE_INVALID, "Generated Skill name is invalid");
  const outputRoot = inspectDirectory(workspaceRoot, "output");
  const outputEntries = fs.readdirSync(outputRoot, { withFileTypes: true });
  requireAudit(outputEntries.length === 1 && outputEntries[0].name === skillName && outputEntries[0].isDirectory() && !outputEntries[0].isSymbolicLink(), SKILL_GENERATION_TRACE_CODES.OUTPUT_TREE_INVALID, "output must contain exactly one real Skill directory");
  const packageRoot = inspectDirectory(workspaceRoot, `output/${skillName}`);
  const rootEntries = fs.readdirSync(packageRoot, { withFileTypes: true });
  requireAudit(rootEntries.map((entry) => entry.name).sort().join("\0") === ["SKILL.md", "methods.json", "references"].join("\0"), SKILL_GENERATION_TRACE_CODES.OUTPUT_TREE_INVALID, "Generated package root entries are invalid");
  const referencesRoot = inspectDirectory(workspaceRoot, `output/${skillName}/references`);
  const references = fs.readdirSync(referencesRoot, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name));
  requireAudit(references.length > 0, SKILL_GENERATION_TRACE_CODES.OUTPUT_TREE_INVALID, "references must not be empty");
  requireAudit(references.every((entry) => !entry.isSymbolicLink()), SKILL_GENERATION_TRACE_CODES.PATH_SYMLINK, "Generated references must not contain symlinks");
  requireAudit(references.every((entry) => entry.isFile() && REFERENCE_NAME.test(entry.name)), SKILL_GENERATION_TRACE_CODES.OUTPUT_TREE_INVALID, "references must contain only kebab-case Markdown files");

  const actualRelativePaths = [
    `output/${skillName}/SKILL.md`,
    `output/${skillName}/methods.json`,
    ...references.map((entry) => `output/${skillName}/references/${entry.name}`),
  ].sort();
  const writeRelativePaths = writes.map((write) => write.normalized.relative).sort();
  requireAudit(new Set(writeRelativePaths).size === writeRelativePaths.length && actualRelativePaths.join("\0") === writeRelativePaths.join("\0"), SKILL_GENERATION_TRACE_CODES.OUTPUT_TREE_INVALID, "Every package file must be created by exactly one successful Write and no extra package files may exist");

  const files = actualRelativePaths.map((relative) => {
    const absolute = inspectRegularFile(workspaceRoot, relative);
    const write = writes.find((candidate) => candidate.normalized.relative === relative);
    const bytes = fs.readFileSync(absolute);
    requireAudit(bytes.equals(Buffer.from(write.record.input.content, "utf8")), SKILL_GENERATION_TRACE_CODES.WRITE_CONTENT_MISMATCH, "Generated bytes do not match the successful Write input", { path: relative });
    return {
      path: `workspace/${relative}`,
      size_bytes: bytes.length,
      sha256: sha256Bytes(bytes),
      write_ordinal: write.record.ordinal,
    };
  });
  let methods;
  try {
    methods = JSON.parse(fs.readFileSync(path.join(packageRoot, "methods.json"), "utf8"));
  } catch {
    fail(SKILL_GENERATION_TRACE_CODES.OUTPUT_TREE_INVALID, "methods.json must be UTF-8 JSON");
  }
  requireAudit(isPlainObject(methods) && methods.schema_version === 1 && methods.skill_name === skillName, SKILL_GENERATION_TRACE_CODES.OUTPUT_TREE_INVALID, "methods.json must bind schema version 1 to the generated Skill name");
  requireAudit(
    Array.isArray(methods.shared_references)
      && methods.shared_references[0] === SOURCE_LOG_TEMPLATES_REFERENCE,
    SKILL_GENERATION_TRACE_CODES.SOURCE_LOG_TEMPLATES_INVALID,
    `methods.json shared_references[0] must be ${SOURCE_LOG_TEMPLATES_REFERENCE}`,
  );
  requireAudit(
    Array.isArray(methods.methods)
      && methods.methods.length > 0
      && methods.methods.every((method) => isPlainObject(method) && method.reference !== SOURCE_LOG_TEMPLATES_REFERENCE),
    SKILL_GENERATION_TRACE_CODES.SOURCE_LOG_TEMPLATES_INVALID,
    `${SOURCE_LOG_TEMPLATES_REFERENCE} must not be used as a method reference`,
  );
  const methodIds = new Set();
  for (const [index, method] of methods.methods.entries()) {
    requireAudit(
      exactKeys(method, ["activation_markers", "evidence_markers", "id", "priority", "reference", "title"])
        && SKILL_NAME.test(method.id ?? "") && !methodIds.has(method.id)
        && typeof method.title === "string" && method.title.trim().length > 0
        && typeof method.reference === "string" && method.reference.startsWith("references/")
        && method.reference !== SOURCE_LOG_TEMPLATES_REFERENCE
        && Number.isSafeInteger(method.priority) && method.priority === index + 1
        && uniqueNonEmptyStrings(method.evidence_markers)
        && uniqueNonEmptyStrings(method.activation_markers)
        && orderedSubsequence(method.activation_markers, method.evidence_markers),
      SKILL_GENERATION_TRACE_CODES.METHODS_CONTRACT_INVALID,
      "Generated methods.json method fields and activation markers are invalid",
      { method_index: index },
    );
    methodIds.add(method.id);
  }
  const sourceLogTemplatesRelative = `output/${skillName}/${SOURCE_LOG_TEMPLATES_REFERENCE}`;
  const sourceLogTemplatesFile = files.find((file) => file.path === `workspace/${sourceLogTemplatesRelative}`);
  requireAudit(
    sourceLogTemplatesFile !== undefined,
    SKILL_GENERATION_TRACE_CODES.SOURCE_LOG_TEMPLATES_INVALID,
    `Generated package must contain ${SOURCE_LOG_TEMPLATES_REFERENCE}`,
  );
  const expectedSourceLogTemplatesBytes = sourceLogTemplatesBytes(sourceIdentity.log_templates);
  requireAudit(
    fs.readFileSync(inspectRegularFile(workspaceRoot, sourceLogTemplatesRelative)).equals(expectedSourceLogTemplatesBytes),
    SKILL_GENERATION_TRACE_CODES.SOURCE_LOG_TEMPLATES_INVALID,
    `${SOURCE_LOG_TEMPLATES_REFERENCE} must exactly render the source identity template inventory`,
  );
  return {
    package: {
      skill_name: skillName,
      root: `workspace/output/${skillName}`,
      file_count: files.length,
      files,
      method_marker_sets: methods.methods.map((method) => ({
        method_id: method.id,
        evidence_markers: [...method.evidence_markers],
        activation_markers: [...method.activation_markers],
      })),
      content_tree_sha256: sha256Bytes(canonicalJson({ version: 1, files: files.map(({ path: filePath, size_bytes, sha256 }) => ({ path: filePath, size_bytes, sha256 })) })),
    },
    sourceLogTemplates: {
      extraction_version: sourceIdentity.log_template_extraction_version,
      count: sourceIdentity.log_templates.length,
      inventory_sha256: sourceIdentity.log_template_inventory_sha256,
      reference_path: sourceLogTemplatesFile.path,
      reference_sha256: sourceLogTemplatesFile.sha256,
    },
  };
}

function validReceiptFile(value, skillName) {
  if (!exactKeys(value, ["path", "size_bytes", "sha256", "write_ordinal"])) return false;
  const prefix = `workspace/output/${skillName}/`;
  const relative = typeof value.path === "string" && value.path.startsWith(prefix)
    ? value.path.slice(prefix.length)
    : "";
  return typeof value.path === "string"
    && (relative === "SKILL.md" || relative === "methods.json" || /^references\/[a-z0-9]+(?:-[a-z0-9]+)*\.md$/.test(relative))
    && Number.isSafeInteger(value.size_bytes)
    && value.size_bytes > 0
    && /^[a-f0-9]{64}$/.test(value.sha256 ?? "")
    && Number.isSafeInteger(value.write_ordinal);
}

export function validSkillGenerationTraceAuditReceipt(value) {
  if (!exactKeys(value, [
    "schema_version", "status", "workflow", "skill", "tool_inventory", "permission_mode",
    "permission_policy_sha256", "tool_sequence", "required_reads", "observed_reads",
    "linked_references", "package", "source_log_templates", "terminal",
  ])) return false;
  if (value.schema_version !== SKILL_GENERATION_TRACE_SCHEMA_VERSION
    || value.status !== "PASS"
    || value.workflow !== "skill-generation"
    || value.skill !== "wiki-to-diagnosis-skill"
    || value.permission_mode !== "dontAsk"
    || JSON.stringify(value.tool_inventory) !== JSON.stringify(ALLOWED_TOOLS)
    || !/^[a-f0-9]{64}$/.test(value.permission_policy_sha256 ?? "")
    || JSON.stringify(value.required_reads) !== JSON.stringify(REQUIRED_RECEIPT_READS)
    || JSON.stringify(value.linked_references) !== JSON.stringify([`skill/${REQUIRED_REFERENCE_PATH}`])) return false;
  if (!Array.isArray(value.tool_sequence) || !Array.isArray(value.observed_reads) || value.tool_sequence.length < 7) return false;
  if (!value.tool_sequence.every((record, ordinal) => record?.ordinal === ordinal && ALLOWED_TOOLS.includes(record.tool) && record.outcome === "SUCCESS")) return false;
  const skillRecords = value.tool_sequence.filter((record) => record.tool === "Skill");
  const readRecords = value.tool_sequence.filter((record) => record.tool === "Read");
  const writeRecords = value.tool_sequence.filter((record) => record.tool === "Write");
  if (skillRecords.length !== 1 || skillRecords[0].ordinal !== 0 || !exactKeys(skillRecords[0], ["ordinal", "tool", "outcome"])) return false;
  if (readRecords.length !== 3 || writeRecords.length < 3) return false;
  if (!readRecords.every((record) => exactKeys(record, ["ordinal", "tool", "outcome", "path"]))) return false;
  if (!writeRecords.every((record) => exactKeys(record, ["ordinal", "tool", "outcome", "path"]))) return false;
  if (!readRecords.every((record) => record.ordinal < writeRecords[0].ordinal) || !writeRecords.every((record, index) => record.ordinal === writeRecords[0].ordinal + index)) return false;
  if (new Set(readRecords.map((record) => record.path)).size !== 3 || !REQUIRED_RECEIPT_READS.every((required) => readRecords.some((record) => record.path === required))) return false;
  const expectedObserved = readRecords.map((record) => ({ ordinal: record.ordinal, path: record.path }));
  if (JSON.stringify(value.observed_reads) !== JSON.stringify(expectedObserved)) return false;
  const packageValue = value.package;
  if (!exactKeys(packageValue, ["skill_name", "root", "file_count", "files", "method_marker_sets", "content_tree_sha256"])
    || !SKILL_NAME.test(packageValue.skill_name ?? "")
    || packageValue.root !== `workspace/output/${packageValue.skill_name}`
    || !Array.isArray(packageValue.files)
    || packageValue.file_count !== packageValue.files.length
    || packageValue.file_count !== writeRecords.length
    || !packageValue.files.every((file) => validReceiptFile(file, packageValue.skill_name))
    || new Set(packageValue.files.map((file) => file.path)).size !== packageValue.files.length
    || packageValue.files.map((file) => file.path).join("\0") !== packageValue.files.map((file) => file.path).sort().join("\0")
    || packageValue.files.filter((file) => file.path === `${packageValue.root}/SKILL.md`).length !== 1
    || packageValue.files.filter((file) => file.path === `${packageValue.root}/methods.json`).length !== 1
    || !packageValue.files.some((file) => file.path.startsWith(`${packageValue.root}/references/`))
    || !/^[a-f0-9]{64}$/.test(packageValue.content_tree_sha256 ?? "")) return false;
  if (!Array.isArray(packageValue.method_marker_sets) || packageValue.method_marker_sets.length === 0
    || !packageValue.method_marker_sets.every((item) => exactKeys(item, ["activation_markers", "evidence_markers", "method_id"])
      && SKILL_NAME.test(item.method_id ?? "")
      && uniqueNonEmptyStrings(item.evidence_markers)
      && uniqueNonEmptyStrings(item.activation_markers)
      && orderedSubsequence(item.activation_markers, item.evidence_markers))
    || new Set(packageValue.method_marker_sets.map((item) => item.method_id)).size !== packageValue.method_marker_sets.length) return false;
  const expectedDigest = sha256Bytes(canonicalJson({ version: 1, files: packageValue.files.map(({ path: filePath, size_bytes, sha256 }) => ({ path: filePath, size_bytes, sha256 })) }));
  if (packageValue.content_tree_sha256 !== expectedDigest) return false;
  if (!writeRecords.every((record) => packageValue.files.some((file) => file.path === record.path && file.write_ordinal === record.ordinal))) return false;
  const sourceLogTemplates = value.source_log_templates;
  const expectedReferencePath = `${packageValue.root}/${SOURCE_LOG_TEMPLATES_REFERENCE}`;
  const referenceFile = packageValue.files.find((file) => file.path === expectedReferencePath);
  if (!exactKeys(sourceLogTemplates, [
    "extraction_version", "count", "inventory_sha256", "reference_path", "reference_sha256",
  ])
    || sourceLogTemplates.extraction_version !== 1
    || !Number.isSafeInteger(sourceLogTemplates.count)
    || sourceLogTemplates.count < 0
    || !/^[a-f0-9]{64}$/.test(sourceLogTemplates.inventory_sha256 ?? "")
    || sourceLogTemplates.reference_path !== expectedReferencePath
    || !/^[a-f0-9]{64}$/.test(sourceLogTemplates.reference_sha256 ?? "")
    || referenceFile === undefined
    || referenceFile.sha256 !== sourceLogTemplates.reference_sha256) return false;
  return exactKeys(value.terminal, ["subtype", "is_error"])
    && value.terminal.subtype === "success"
    && value.terminal.is_error === false;
}

export function auditSkillGenerationTrace({ events, workspaceRoot, skillRoot, sourceRoot = null }) {
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
  requireAudit(typeof init.cwd === "string" && sameAbsolutePath(init.cwd, workspaceRoot), SKILL_GENERATION_TRACE_CODES.INIT_CWD_MISMATCH, "The init cwd must equal the audited workspace");
  requireAudit(init.permissionMode === "dontAsk", SKILL_GENERATION_TRACE_CODES.PERMISSION_MODE_INVALID, "The effective permission mode must be dontAsk");
  requireAudit(Array.isArray(init.tools) && init.tools.length === ALLOWED_TOOLS.length && new Set(init.tools).size === ALLOWED_TOOLS.length && ALLOWED_TOOLS.every((tool) => init.tools.includes(tool)), SKILL_GENERATION_TRACE_CODES.TOOL_INVENTORY_INVALID, "The init tool inventory must contain only Skill, Read and Write");
  const terminalEvents = events.filter((event) => event.type === "result");
  requireAudit(terminalEvents.length === 1 && events.at(-1) === terminalEvents[0], SKILL_GENERATION_TRACE_CODES.RESULT_INVALID, "The trace must end with exactly one result event");
  const terminal = terminalEvents[0];
  requireAudit(terminal.subtype === "success" && terminal.is_error === false, SKILL_GENERATION_TRACE_CODES.RESULT_NOT_SUCCESS, "The terminal result must report success");

  const linkedReferences = discoverLinkedSkillReferences(skillRoot);
  const sourceIdentity = inspectSourceIdentity(workspaceRoot);
  const records = parseToolRecords(events);
  validateSkillInvocation(records);
  const reads = records.filter((record) => record.tool === "Read");
  requireAudit(reads.length === 3, SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_MISSING, "The trace must read exactly the Wiki, source identity, and linked output contract once each");
  const observedReads = reads.map((record) => ({ ordinal: record.ordinal, path: observedReadPath(validateReadInput(record), { workspaceRoot, skillRoot }), result_event_index: record.result_event_index }));
  requireAudit(new Set(observedReads.map((record) => record.path)).size === 3 && REQUIRED_RECEIPT_READS.every((required) => observedReads.some((record) => record.path === required)), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_MISSING, "The trace did not read all three required sources exactly once");

  const writeRecords = records.filter((record) => record.tool === "Write");
  requireAudit(writeRecords.length >= 3, SKILL_GENERATION_TRACE_CODES.WRITE_COUNT_INVALID, "Methods generation requires SKILL.md, methods.json, and at least one reference Write");
  requireAudit(records.slice(1, 4).every((record) => record.tool === "Read") && records.slice(4).every((record) => record.tool === "Write"), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "All three required Reads must complete before a contiguous final sequence of Writes");
  for (const read of observedReads) requireAudit(read.result_event_index < writeRecords[0].use_event_index, SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "All three required Reads must complete before the first Write");

  const writes = writeRecords.map((record) => {
    validateWriteInput(record);
    return { record, normalized: normalizedWorkspaceWrite(record.input.file_path, workspaceRoot) };
  });
  const skillNames = new Set(writes.map((write) => write.normalized.skillName));
  requireAudit(skillNames.size === 1, SKILL_GENERATION_TRACE_CODES.WRITE_PATH_INVALID, "All Writes must target one generated Skill directory");
  const auditedOutput = inspectOutputPackage(workspaceRoot, [...skillNames][0], writes, sourceIdentity);

  return {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "PASS",
    workflow: "skill-generation",
    skill: "wiki-to-diagnosis-skill",
    tool_inventory: [...ALLOWED_TOOLS],
    permission_mode: init.permissionMode,
    permission_policy_sha256: sha256Bytes(JSON.stringify(skillGenerationPermissionRules({ workspaceRoot, skillRoot, linkedReferences, sourceRoot }))),
    tool_sequence: records.map((record) => {
      if (record.tool === "Read") return { ordinal: record.ordinal, tool: "Read", outcome: "SUCCESS", path: observedReadPath(validateReadInput(record), { workspaceRoot, skillRoot }) };
      if (record.tool === "Write") {
        const normalized = normalizedWorkspaceWrite(record.input.file_path, workspaceRoot);
        return { ordinal: record.ordinal, tool: "Write", outcome: "SUCCESS", path: `workspace/${normalized.relative}` };
      }
      return { ordinal: record.ordinal, tool: "Skill", outcome: "SUCCESS" };
    }),
    required_reads: [...REQUIRED_RECEIPT_READS],
    observed_reads: observedReads.map(({ ordinal, path: readPath }) => ({ ordinal, path: readPath })),
    linked_references: linkedReferences.map((relative) => `skill/${relative}`),
    package: auditedOutput.package,
    source_log_templates: auditedOutput.sourceLogTemplates,
    terminal: { subtype: terminal.subtype, is_error: terminal.is_error },
  };
}
