import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const localPython = path.join(repo, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const python = process.env.TEST_FLOW_QUICK_PYTHON || (fs.existsSync(localPython) ? localPython : "python3");
const init = { type: "system", subtype: "init", model: "test-model", tools: [] };
const terminal = { type: "result", subtype: "success", is_error: false, num_turns: 1, result: JSON.stringify({ schema_version: 1, action: "SUBMIT_SUPPLEMENT" }), total_cost_usd: 0.05,
  usage: { input_tokens: 100, output_tokens: 50, cache_creation_input_tokens: 10, cache_read_input_tokens: 20 } };

function audit(lines, { exclude = false } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pl-intake-proof-"));
  try {
    const id = crypto.randomUUID();
    const runtime = path.join(root, "workspaces", id, "runtime");
    fs.mkdirSync(runtime, { recursive: true });
    const raw = lines.map((item) => JSON.stringify(item)).join("\n") + "\n";
    fs.writeFileSync(path.join(runtime, "stdout.log"), raw);
    fs.writeFileSync(path.join(runtime, "stderr.log"), "");
    const output = path.join(root, "receipt.json");
    const args = ["-B", path.join(repo, "tools/test-flow/runtime-support/audit_intake_usage.py"),
      "--workspaces-root", path.join(root, "workspaces"), "--output", output, "--model", "test-model",
      "--max-turns", "1", "--max-total-tokens", "100000", "--max-budget-usd", "1", "--hard-timeout-seconds", "120"];
    if (exclude) args.push("--exclude-execution-id", id);
    const result = spawnSync(python, args, { cwd: repo, encoding: "utf8", timeout: 15_000 });
    return { ...result, id, raw, receipt: fs.existsSync(output) ? JSON.parse(fs.readFileSync(output, "utf8")) : null };
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
}

test("INTAKE usage audits the actual one-call terminal without inventing a domain Job", () => {
  const result = audit([init, terminal]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.receipt.new_execution_ids, [result.id]);
  const [invocation] = result.receipt.invocations;
  assert.equal(invocation.class, "server-intake");
  assert.equal(invocation.phase, "INTAKE");
  assert.equal(invocation.action, "SUBMIT_SUPPLEMENT");
  assert.equal(invocation.usage.total_tokens, 180);
  assert.equal(invocation.usage.cost_usd, 0.05);
  assert.equal(invocation.stdout_sha256, crypto.createHash("sha256").update(result.raw).digest("hex"));
  assert.equal(invocation.stdout_size, Buffer.byteLength(result.raw));
  assert.equal(Object.hasOwn(invocation, "job_id"), false);
  assert.deepEqual(invocation.effective_caps, { max_turns: 1, max_total_tokens: 100000, max_budget_usd: 1, hard_timeout_seconds: 120 });
});

for (const [name, lines, failure] of [
  ["tools", [{ ...init, tools: ["Read"] }, terminal], "INTAKE_TOOLS_NOT_DISABLED"],
  ["hidden repair", [init, terminal, init, terminal], "INTAKE_REPAIR_OR_INCOMPLETE_STREAM"],
  ["incomplete stream", [init], "INTAKE_REPAIR_OR_INCOMPLETE_STREAM"],
  ["wrong model", [{ ...init, model: "other-model" }, terminal], "MODEL_IDENTITY_MISMATCH"],
  ["excessive turns", [init, { ...terminal, num_turns: 2 }], "MODEL_TERMINAL_INVALID"],
  ["over budget", [init, { ...terminal, total_cost_usd: 1.01 }], "MODEL_BUDGET_CAP_EXCEEDED"],
  ["report creation", [init, { ...terminal, result: JSON.stringify({ schema_version: 1, action: "CREATE_RESULT" }) }], "INTAKE_ACTION_INVALID"],
  ["legacy case creation", [init, { ...terminal, result: JSON.stringify({ schema_version: 1, action: "CREATE_CASE" }) }], "INTAKE_ACTION_INVALID"],
  ["unsupported schema", [init, { ...terminal, result: JSON.stringify({ schema_version: 2, action: "SUBMIT_SUPPLEMENT" }) }], "INTAKE_SCHEMA_VERSION_INVALID"],
  ["boolean schema", [init, { ...terminal, result: JSON.stringify({ schema_version: true, action: "SUBMIT_SUPPLEMENT" }) }], "INTAKE_SCHEMA_VERSION_INVALID"],
]) test(`INTAKE proof rejects ${name}`, () => {
  const result = audit(lines);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, new RegExp(failure));
  assert.equal(result.receipt, null);
});

test("later stages exclude already-audited INTAKE execution IDs", () => {
  const result = audit([init, terminal], { exclude: true });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.receipt.invocations, []);
  assert.deepEqual(result.receipt.new_execution_ids, []);
});
