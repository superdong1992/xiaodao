#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { verifyVerdict } from "./lib/evidence.mjs";
import { canonicalJson, readJson } from "./lib/util.mjs";

const TOOL_ROOT = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_REPO_ROOT = path.resolve(TOOL_ROOT, "..", "..");
const SAFE_RUN_ID = /^run-[0-9TZ]+-[a-f0-9]{8}$/;

function parse(argv) {
  const command = argv.shift() ?? "report";
  const options = { runIds: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--execute") options.execute = true;
    else if (argument === "--dry-run") options.dryRun = true;
    else if (argument === "--evidence-root") options.evidenceRoot = argv[++index];
    else if (argument === "--keep-last") options.keepLast = Number.parseInt(argv[++index], 10);
    else if (argument === "--run-id") options.runIds.push(argv[++index]);
    else throw new Error(`ARGUMENT_UNKNOWN:${argument}`);
  }
  if (!["report", "prune"].includes(command)) throw new Error(`COMMAND_UNKNOWN:${command}`);
  if (command === "report" && (options.execute || options.dryRun)) throw new Error("REPORT_MODE_INVALID");
  if (options.execute && options.dryRun) throw new Error("PRUNE_MODE_CONFLICT");
  if (options.execute && options.runIds.length === 0) throw new Error("PRUNE_EXPLICIT_RUN_ID_REQUIRED");
  if (!Number.isInteger(options.keepLast ?? 10) || (options.keepLast ?? 10) < 0) throw new Error("KEEP_LAST_INVALID");
  return { command, options };
}

function directorySize(root) {
  let bytes = 0;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const child = path.join(root, entry.name);
    if (entry.isDirectory() && !entry.isSymbolicLink()) bytes += directorySize(child);
    else if (entry.isFile() && !entry.isSymbolicLink()) bytes += fs.statSync(child).size;
  }
  return bytes;
}

function evidenceInventory(evidenceRoot) {
  if (!fs.existsSync(evidenceRoot)) return [];
  const inventory = [];
  for (const entry of fs.readdirSync(evidenceRoot, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
    const attemptRoot = path.join(evidenceRoot, entry.name);
    let verification;
    try { verification = verifyVerdict(attemptRoot); } catch { verification = { status: "INVALID", verdict: null }; }
    inventory.push({
      run_id: entry.name,
      attempt_root: attemptRoot,
      verification_status: verification.status,
      overall: verification.verdict?.overall ?? null,
      functional_status: verification.verdict?.functional_status ?? null,
      evidence_reusable: verification.status === "PASS" && verification.verdict?.evidence_reusable === true,
      committed_at_utc: verification.verdict?.committed_at_utc ?? null,
      size_bytes: directorySize(attemptRoot),
      verdict: verification.verdict,
    });
  }
  inventory.sort((left, right) => String(left.committed_at_utc ?? left.run_id).localeCompare(String(right.committed_at_utc ?? right.run_id)));
  return inventory;
}

function referencedRuns(inventory) {
  const result = new Set();
  for (const item of inventory) {
    if (item.verification_status !== "PASS") continue;
    for (const stage of item.verdict?.stages ?? []) {
      const source = stage.reused_from?.run_id ?? stage.reused_from?.reuse?.run_id;
      if (source) result.add(source);
    }
  }
  return result;
}

function report(evidenceRoot, keepLast = 10, runIds = []) {
  const attempts = evidenceInventory(evidenceRoot);
  const protectedRuns = referencedRuns(attempts);
  const newest = new Set(attempts.slice(-keepLast).map((item) => item.run_id));
  const selected = runIds.length > 0
    ? new Set([...new Set(runIds)].map((runId) => path.basename(exactAttempt(evidenceRoot, runId))))
    : null;
  const rows = attempts.filter((item) => !selected || selected.has(item.run_id)).map(({ verdict: _verdict, ...item }) => ({
    ...item,
    retention: item.verification_status !== "PASS"
      ? "MANUAL_REVIEW"
      : protectedRuns.has(item.run_id)
      ? "KEEP_REFERENCED"
      : newest.has(item.run_id)
        ? "KEEP_RECENT"
        : item.evidence_reusable
          ? "KEEP_REUSABLE"
          : "MANUAL_REVIEW",
  }));
  return {
    schema_version: 2,
    evidence_root: evidenceRoot,
    automatic_deletion: false,
    attempt_count: rows.length,
    total_size_bytes: rows.reduce((sum, item) => sum + item.size_bytes, 0),
    attempts: rows,
  };
}

function exactAttempt(evidenceRoot, runId) {
  if (!SAFE_RUN_ID.test(runId)) throw new Error(`RUN_ID_INVALID:${runId}`);
  const root = path.resolve(evidenceRoot);
  const target = path.resolve(root, runId);
  if (path.dirname(target) !== root || !fs.existsSync(target) || !fs.statSync(target).isDirectory() || fs.lstatSync(target).isSymbolicLink()) {
    throw new Error(`RUN_ID_NOT_FOUND:${runId}`);
  }
  const attempt = readJson(path.join(target, "attempt.json"));
  if (attempt.run_id !== runId) throw new Error(`RUN_ID_RECEIPT_MISMATCH:${runId}`);
  return target;
}

function prune(evidenceRoot, options) {
  const inventory = report(evidenceRoot, options.keepLast ?? 10);
  const fullInventory = evidenceInventory(evidenceRoot);
  const selected = options.runIds.length > 0
    ? options.runIds
    : inventory.attempts.filter((item) => item.retention === "MANUAL_REVIEW").map((item) => item.run_id);
  const selectedSet = new Set(selected);
  const dependentRuns = new Map();
  for (const item of fullInventory) {
    if (item.verification_status !== "PASS") continue;
    for (const stage of item.verdict?.stages ?? []) {
      const source = stage.reused_from?.run_id;
      if (!source) continue;
      const values = dependentRuns.get(source) ?? [];
      values.push(item.run_id);
      dependentRuns.set(source, values);
    }
  }
  const targets = selected.map((runId) => {
    const dependents = [...new Set(dependentRuns.get(runId) ?? [])].filter((dependent) => !selectedSet.has(dependent)).sort();
    return { run_id: runId, path: exactAttempt(evidenceRoot, runId), dependents, blocked: dependents.length > 0 };
  });
  if (!options.execute) {
    return { schema_version: 2, mode: "DRY_RUN", automatic_deletion: false, targets };
  }
  const blocked = targets.filter((target) => target.blocked);
  if (blocked.length > 0) throw new Error(`PRUNE_REFERENCED_SOURCE:${blocked.map((target) => `${target.run_id}->${target.dependents.join(",")}`).join(";")}`);
  for (const target of targets) fs.rmSync(target.path, { recursive: true, force: false });
  return {
    schema_version: 2,
    mode: "EXECUTED",
    automatic_deletion: false,
    recovery: "NONE",
    removed: targets.map(({ run_id }) => run_id),
  };
}

function main() {
  const { command, options } = parse(process.argv.slice(2));
  const evidenceRoot = path.resolve(options.evidenceRoot ?? path.join(DEFAULT_REPO_ROOT, ".tmp", "test-flow-evidence"));
  const value = command === "report" ? report(evidenceRoot, options.keepLast ?? 10, options.runIds) : prune(evidenceRoot, options);
  process.stdout.write(canonicalJson(value));
}

try { main(); } catch (error) {
  process.stderr.write(`${JSON.stringify({ status: "ERROR", message: String(error?.message ?? error) })}\n`);
  process.exitCode = 3;
}
