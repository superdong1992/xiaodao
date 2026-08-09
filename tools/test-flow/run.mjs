#!/usr/bin/env node
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runFlow } from "./lib/engine.mjs";
import { redactError } from "./lib/util.mjs";

const TOOL_ROOT = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_REPO_ROOT = path.resolve(TOOL_ROOT, "..", "..");

const HELP = `Problem Locator test flow

Usage:
  tools/test-flow/run.sh [options]
  tools/test-flow/run.ps1 [options]

Core options:
  --track dev|release            Default: dev
  --goal <proof-goal>            dev.default, dev.real, release.full
  --stage <stage-id>             Required by dev.real
  --plan-only                    Resolve proof, identity, reuse and admission only
  --client auto|windows|macos|linux
  --resume auto|fresh
  --allow-real-model             Required for a Dev real-model proof

Structured retry/override intent:
  --reason <text>
  --hypothesis <text>
  --expected-evidence <text>

Environment/dependency inputs:
  --repo-root <absolute-path>
  --evidence-root <path>
  --base <git-commit>
  --logparse-source <absolute-path>
  --mcp-source <absolute-path>
  --claude-entry <absolute-cli.js> Release/real required; never falls back to global claude
  --claude-settings <absolute-path> Source for env-only temporary settings; Hooks are never copied
  --docker-context colima          Required for macOS Release
  --cache-root <absolute-path>     Default: <repo>/.tmp/test-flow-cache
  --cross-job-adapter <absolute-executable>
  --rollout-parity-spec <absolute-json>

Exit codes:
  0 PASS or PASS_WITH_WARNINGS
  1 functional or persistent performance FAIL
  2 BLOCKED / INCONCLUSIVE
  3 framework, security, finalization or cleanup ERROR
`;

function parseArguments(argv) {
  const options = {};
  const valueOptions = new Map([
    ["--track", "track"],
    ["--goal", "goal"],
    ["--stage", "stage"],
    ["--client", "client"],
    ["--resume", "resume"],
    ["--reason", "reason"],
    ["--hypothesis", "hypothesis"],
    ["--expected-evidence", "expectedEvidence"],
    ["--repo-root", "repoRoot"],
    ["--evidence-root", "evidenceRoot"],
    ["--base", "base"],
    ["--logparse-source", "logparseSource"],
    ["--mcp-source", "mcpSource"],
    ["--claude-entry", "claudeEntry"],
    ["--claude-settings", "claudeSettings"],
    ["--docker-context", "dockerContext"],
    ["--cache-root", "cacheRoot"],
    ["--cross-job-adapter", "crossJobAdapter"],
    ["--rollout-parity-spec", "rolloutParitySpec"],
  ]);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--help" || argument === "-h") options.help = true;
    else if (argument === "--plan-only") options.planOnly = true;
    else if (argument === "--allow-real-model") options.allowRealModel = true;
    else if (valueOptions.has(argument)) {
      if (index + 1 >= argv.length) throw new Error(`ARGUMENT_VALUE_MISSING:${argument}`);
      options[valueOptions.get(argument)] = argv[++index];
    } else throw new Error(`ARGUMENT_UNKNOWN:${argument}`);
  }
  if (options.track && !["dev", "release"].includes(options.track)) throw new Error(`TRACK_UNKNOWN:${options.track}`);
  if (options.client && !["auto", "windows", "macos", "linux"].includes(options.client)) throw new Error(`CLIENT_UNKNOWN:${options.client}`);
  if (options.resume && !["auto", "fresh"].includes(options.resume)) throw new Error(`RESUME_UNKNOWN:${options.resume}`);
  return options;
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(HELP);
    return;
  }
  const repoRoot = path.resolve(options.repoRoot ?? DEFAULT_REPO_ROOT);
  delete options.repoRoot;
  for (const name of ["logparseSource", "mcpSource", "claudeEntry", "claudeSettings", "cacheRoot", "crossJobAdapter", "rolloutParitySpec"]) {
    if (!options[name]) continue;
    if (!path.isAbsolute(options[name])) throw new Error(`ARGUMENT_ABSOLUTE_REQUIRED:${name}`);
    options[name] = path.resolve(options[name]);
  }
  const result = await runFlow(repoRoot, options);
  if (options.planOnly) {
    process.stdout.write(`${JSON.stringify(result.plan, null, 2)}\n`);
  } else {
    process.stdout.write(`${JSON.stringify({
      run_id: result.verdict.run_id,
      overall: result.verdict.overall,
      functional_status: result.verdict.functional_status,
      performance_status: result.verdict.performance_status,
      operation_status: result.verdict.operation_status,
      verification_status: result.verdict.verification_status,
      exit_code: result.verdict.exit_code,
      attempt_root: result.attemptRoot,
    }, null, 2)}\n`);
  }
  process.exitCode = result.exitCode;
}

main().catch((error) => {
  process.stderr.write(`${JSON.stringify({ status: "ERROR", error: redactError(error) })}\n`);
  process.exitCode = 3;
});
