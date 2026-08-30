import assert from "node:assert/strict";
import fs from "node:fs";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  auditOracle,
  auditFastInvocations,
  auditRuntime,
  productionRuntimeArguments,
  semanticMethodMapping,
} from "../runtime/claude-deepseek-fast-e2e-runner.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "..");
const RUNTIME = path.join(ROOT, "tools/test-flow/quick-validation/claude-deepseek/runtime/claude_deepseek_model_cert_runtime.py");

function writeConfirmedNoiseFixture(evidenceRoot) {
  const methods = {
    methods: [
      { id: "generated-api", activation_markers: ["API_COMPLETE service="] },
      { id: "generated-queue", activation_markers: ["QUEUE_HISTORY print_time_ms="] },
      { id: "generated-client", activation_markers: ["LATE_RESPONSE service="] },
    ],
  };
  const graph = {
    hits: [
      { hit_ref: "hit-target", method_id: "generated-client", marker: "LATE_RESPONSE service=", line: "LATE_RESPONSE service=svc_profile api=Lookup request_id=601" },
      { hit_ref: "hit-noise", method_id: "generated-client", marker: "LATE_RESPONSE service=", line: "LATE_RESPONSE service=svc_noise api=NoiseApi request_id=999" },
    ],
    events: [
      { event_ref: "event-target", method_id: "generated-client", identity_tokens: ["request_id=601"], evidence_hit_refs: ["hit-target"] },
      { event_ref: "event-noise", method_id: "generated-client", identity_tokens: ["request_id=999"], evidence_hit_refs: ["hit-noise"] },
    ],
  };
  fs.mkdirSync(evidenceRoot);
  fs.writeFileSync(path.join(evidenceRoot, "methods.json"), JSON.stringify(methods));
  fs.writeFileSync(path.join(evidenceRoot, "methods-evidence-graph-v2.json"), JSON.stringify(graph));
  return {
    methods_result: {
      status: "RESOLVED",
      confirmed_method_ids: ["generated-client"],
      confirmed_hit_refs: ["hit-target", "hit-noise"],
      confirmed_event_refs: ["event-target", "event-noise"],
      reasons: [],
      limitations: [],
    },
  };
}

test("Fast E2E Runtime arguments bind one historical scenario without formal certification inputs", () => {
  const options = {
    sourceRoot: ROOT,
    sourceWiki: path.join(ROOT, "tests/cases/release/rpc-timeout-anonymized/input/wiki.md"),
    scenarioRoot: path.join(ROOT, "experiments/rpc-skill-feasibility/cases/api-execution-overrun"),
    scenario: "api-execution-overrun",
    registrationRoot: "/registration",
    workRoot: "/work",
    evidenceRoot: "/evidence",
    claudeEntry: "/claude/cli.js",
    stagedSettings: "/private/settings.json",
    configRoot: "/private/config",
    privateRoot: "/private",
    usageRoot: "/usage",
    runId: "fast-run",
  };
  const args = productionRuntimeArguments(options);
  const value = (name) => args[args.indexOf(name) + 1];
  assert.equal(value("--scenario-id"), options.scenario);
  assert.equal(value("--scenario-root"), options.scenarioRoot);
  assert.equal(value("--registration-root"), options.registrationRoot);
  for (const forbidden of ["--source-snapshot-digest", "--core-verdict", "--model-cert"]) {
    assert.equal(args.includes(forbidden), false, forbidden);
  }
  assert.equal(args.includes("--source-wiki"), false);
});

test("insufficient evidence permits exactly zero role calls", () => {
  const audit = auditFastInvocations([], "insufficient-evidence");
  assert.equal(audit.actual_call_count, 0);
  assert.deepEqual(audit.repair_counts, { specialist: 0, reviewer: 0 });
  assert.throws(
    () => auditFastInvocations([{ role: "SPECIALIST" }], "insufficient-evidence"),
    (error) => error.code === "CLAUDE_DEEPSEEK_DIAGNOSIS_STATUS_MISMATCH",
  );
});

test("zero-call Runtime receipt still binds the historical scenario and production Runtime", () => {
  const receipt = {
    status: "PASS",
    execution_mode: "real-model",
    production_runtime: "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime",
    scenario_id: "insufficient-evidence",
    scenario: { scenario_id: "insufficient-evidence" },
    role_attempts: [],
    model_invocations: 0,
  };
  assert.equal(auditRuntime(receipt, [], "insufficient-evidence").prompt_count, 0);
  assert.throws(
    () => auditRuntime(receipt, [], "api-execution-overrun"),
    (error) => error.code === "CLAUDE_DEEPSEEK_FAST_E2E_SCENARIO_IDENTITY_MISMATCH",
  );
});

test("semantic method mapping uses authored cause markers and not generated IDs", () => {
  const methods = {
    methods: [
      {
        id: "generated-api",
        evidence_markers: ["API_COMPLETE service=", "DEADLOOP_DETECTED service=", "LATE_RESPONSE service="],
        activation_markers: ["API_COMPLETE service=", "DEADLOOP_DETECTED service=", "LATE_RESPONSE service="],
      },
      {
        id: "generated-queue",
        evidence_markers: ["QUEUE_HISTORY print_time_ms=", "LATE_RESPONSE service="],
        activation_markers: ["QUEUE_HISTORY print_time_ms=", "LATE_RESPONSE service="],
      },
      {
        id: "generated-client",
        evidence_markers: ["LATE_RESPONSE service="],
        activation_markers: ["LATE_RESPONSE service="],
      },
    ],
  };
  const mapped = semanticMethodMapping(methods);
  assert.equal(mapped.get("api_execution_overrun").id, "generated-api");
  assert.equal(mapped.get("server_receive_queueing").id, "generated-queue");
  assert.equal(mapped.get("client_receive_blocked").id, "generated-client");

  const lowerCase = structuredClone(methods);
  for (const method of lowerCase.methods) {
    method.evidence_markers = method.evidence_markers.map((marker) => marker.toLowerCase());
    method.activation_markers = method.activation_markers.map((marker) => marker.toLowerCase());
  }
  assert.equal(
    semanticMethodMapping(lowerCase).get("client_receive_blocked").id,
    "generated-client",
  );

  const duplicateOwner = structuredClone(methods);
  duplicateOwner.methods.push({
    id: "ambiguous-client",
    evidence_markers: ["LATE_RESPONSE service="],
    activation_markers: ["LATE_RESPONSE service="],
  });
  assert.throws(
    () => semanticMethodMapping(duplicateOwner),
    (error) => error.code === "CLAUDE_DEEPSEEK_DIAGNOSIS_SHAPE_INVALID",
  );
});

test("unrelated-log-noise fails when confirmed evidence includes a noise event", (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-fast-noise-"));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const evidenceRoot = path.join(root, "evidence");
  const runtimeReceipt = writeConfirmedNoiseFixture(evidenceRoot);

  assert.throws(
    () => auditOracle({
      sourceRoot: ROOT,
      scenarioId: "unrelated-log-noise",
      evidenceRoot,
      runtimeReceipt,
    }),
    (error) => error.code === "CLAUDE_DEEPSEEK_FORBIDDEN_TERM_PRESENT",
  );
});

test("Fast runner does not import the Core, Release oracle, or model-cert builder", () => {
  const source = fs.readFileSync(
    path.join(ROOT, "tools/test-flow/quick-validation/claude-deepseek/runtime/claude-deepseek-fast-e2e-runner.mjs"),
    "utf8",
  );
  for (const forbidden of [
    "evidence-v2-core.mjs",
    "evidence-v2-certification.mjs",
    "evidence-v2-scenario-oracle.mjs",
    "claude-deepseek-e2e-runner.mjs",
    "tests/cases/release",
    '"model-cert.json"',
    '"model-cert-input.json"',
  ]) assert.equal(source.includes(forbidden), false, forbidden);
});

test("Runtime driver parameterization reads the historical case and raw logs", { skip: !process.env.TEST_FLOW_QUICK_PYTHON }, () => {
  const scenario = "insufficient-evidence";
  const scenarioRoot = path.join(ROOT, "experiments/rpc-skill-feasibility/cases", scenario);
  const probe = "import hashlib,json,runpy,sys,types; from pathlib import Path; mark=types.SimpleNamespace(parametrize=lambda *a,**k:(lambda f:f)); sys.modules['pytest']=types.SimpleNamespace(fixture=lambda f:f,mark=mark); ns=runpy.run_path(sys.argv[1]); names,values,contents=ns['_declared_scenario_inputs'](Path(sys.argv[2]),sys.argv[3]); print(json.dumps({'names':names,'values':values,'sources':{key:hashlib.sha256(raw).hexdigest() for key,raw in contents.items()}},sort_keys=True))";
  const result = spawnSync(process.env.TEST_FLOW_QUICK_PYTHON, [
    "-c", probe, RUNTIME, scenarioRoot, scenario,
  ], { cwd: ROOT, env: process.env, encoding: "utf8", timeout: 120_000 });
  assert.equal(result.status, 0, result.stderr);
  const receipt = JSON.parse(result.stdout);
  const historical = JSON.parse(fs.readFileSync(path.join(scenarioRoot, "case.json"), "utf8"));
  assert.deepEqual(receipt.names, [
    "problem_time",
    "client_slot",
    "client_process_name",
    "server_slot",
    "server_process_name",
    "service",
    "api",
  ]);
  assert.deepEqual(receipt.values, [
    "2026-08-23T02:00:04.950Z",
    historical.client_slot,
    historical.client_process,
    historical.server_slot,
    historical.server_process,
    historical.service,
    historical.api,
  ]);
  const sourceHash = (file) => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  assert.deepEqual(receipt.sources, {
    client: sourceHash(path.join(scenarioRoot, "raw/client.log")),
    server: sourceHash(path.join(scenarioRoot, "raw/server.log")),
  });
});
