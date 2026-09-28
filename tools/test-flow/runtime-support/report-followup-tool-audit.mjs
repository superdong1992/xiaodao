import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const TOOLS = ["Grep", "Read"];
const DIGEST = /^[a-f0-9]{64}$/;
const sha256 = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");

function requireTrace(condition, code) {
  if (!condition) throw Object.assign(new Error(code), { code });
}

function inside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
}

function inspectInput(workspaceRoot, inputPath) {
  requireTrace(typeof inputPath === "string" && inputPath.length > 0 && !inputPath.includes("\0"), "FOLLOWUP_TRACE_PATH_INVALID");
  const inputRoot = path.resolve(workspaceRoot, "inputs");
  const resolved = path.resolve(workspaceRoot, inputPath);
  requireTrace(inside(inputRoot, resolved), "FOLLOWUP_TRACE_PATH_ESCAPE");
  let current = inputRoot;
  const parts = path.relative(inputRoot, resolved).split(path.sep).filter(Boolean);
  for (const part of [null, ...parts]) {
    if (part !== null) current = path.join(current, part);
    const stat = fs.lstatSync(current);
    requireTrace(!stat.isSymbolicLink() && (stat.isDirectory() || (stat.isFile() && stat.nlink === 1)), "FOLLOWUP_TRACE_PATH_UNSAFE");
  }
  requireTrace(inside(fs.realpathSync.native(inputRoot), fs.realpathSync.native(resolved)), "FOLLOWUP_TRACE_PATH_ESCAPE");
  return resolved;
}

export function reportFollowupInputManifest(workspaceRoot) {
  const inputRoot = inspectInput(workspaceRoot, "inputs");
  const rows = [];
  const visit = (directory) => {
    for (const name of fs.readdirSync(directory).sort()) {
      const absolute = inspectInput(workspaceRoot, path.join(directory, name));
      if (fs.lstatSync(absolute).isDirectory()) visit(absolute);
      else {
        const bytes = fs.readFileSync(absolute);
        rows.push({ path: path.relative(inputRoot, absolute).split(path.sep).join("/"), size: bytes.length, sha256: sha256(bytes) });
      }
    }
  };
  visit(inputRoot);
  return { file_count: rows.length, sha256: sha256(JSON.stringify(rows)) };
}

export function reportFollowupPermissionArguments() {
  return ["--tools", "Read,Grep", "--permission-mode", "dontAsk"];
}

export function auditReportFollowupTrace({ events, workspaceRoot, before }) {
  requireTrace(Array.isArray(events), "FOLLOWUP_TRACE_EVENTS_INVALID");
  const init = events.filter((event) => event.type === "system" && event.subtype === "init");
  requireTrace(init.length === 1 && init[0].permissionMode === "dontAsk"
    && Array.isArray(init[0].tools) && [...init[0].tools].sort().join(",") === TOOLS.join(",")
    && Array.isArray(init[0].mcp_servers) && init[0].mcp_servers.length === 0
    && path.resolve(init[0].cwd ?? "") === path.resolve(workspaceRoot), "FOLLOWUP_TRACE_INIT_INVALID");
  const uses = new Map();
  const counts = { reads: 0, searches: 0, log_reads: 0, log_searches: 0 };
  for (const event of events) {
    if (!Array.isArray(event.message?.content)) continue;
    for (const block of event.message.content) {
      if (block.type === "tool_use") {
        requireTrace(event.type === "assistant" && TOOLS.includes(block.name), "FOLLOWUP_TRACE_TOOL_NOT_ALLOWED");
        requireTrace(typeof block.id === "string" && block.id.length > 0 && !uses.has(block.id), "FOLLOWUP_TRACE_TOOL_ID_INVALID");
        const input = block.input;
        requireTrace(input && typeof input === "object" && !Array.isArray(input), "FOLLOWUP_TRACE_TOOL_INPUT_INVALID");
        const absolute = inspectInput(workspaceRoot, block.name === "Read" ? input.file_path : input.path);
        requireTrace(block.name !== "Grep" || (typeof input.pattern === "string" && input.pattern.length > 0), "FOLLOWUP_TRACE_TOOL_INPUT_INVALID");
        requireTrace(block.name !== "Read" || fs.lstatSync(absolute).isFile(), "FOLLOWUP_TRACE_TOOL_INPUT_INVALID");
        const logRoot = path.resolve(workspaceRoot, "inputs", "logs");
        const log = inside(logRoot, absolute);
        const field = block.name === "Read" ? "reads" : "searches";
        counts[field] += 1;
        if (log) counts[`log_${field}`] += 1;
        uses.set(block.id, false);
      } else if (block.type === "tool_result") {
        requireTrace(event.type === "user" && uses.has(block.tool_use_id) && uses.get(block.tool_use_id) === false
          && block.is_error !== true, "FOLLOWUP_TRACE_TOOL_RESULT_INVALID");
        uses.set(block.tool_use_id, true);
      }
    }
  }
  requireTrace([...uses.values()].every(Boolean), "FOLLOWUP_TRACE_TOOL_RESULT_MISSING");
  const after = reportFollowupInputManifest(workspaceRoot);
  requireTrace(before?.sha256 === after.sha256 && before.file_count === after.file_count, "FOLLOWUP_TRACE_INPUT_CHANGED");
  return { schema_version: 1, status: "PASS", workflow: "report-followup", input_files: after.file_count,
    input_manifest_sha256: after.sha256, ...counts };
}

export function validReportFollowupTraceReceipt(audit) {
  if (audit?.schema_version !== 1 || audit.workflow !== "report-followup") return false;
  if (audit.status === "FAIL") return /^FOLLOWUP_TRACE_[A-Z0-9_]+$/.test(audit.code ?? "")
    && Object.keys(audit).sort().join(",") === "code,schema_version,status,workflow";
  const expected = ["schema_version", "status", "workflow", "input_files", "input_manifest_sha256", "reads", "searches", "log_reads", "log_searches"].sort().join(",");
  return audit.status === "PASS" && Object.keys(audit).sort().join(",") === expected
    && DIGEST.test(audit.input_manifest_sha256) && Number.isSafeInteger(audit.input_files) && audit.input_files > 0
    && ["reads", "searches", "log_reads", "log_searches"].every((key) => Number.isSafeInteger(audit[key]) && audit[key] >= 0)
    && audit.log_reads <= audit.reads && audit.log_searches <= audit.searches;
}
