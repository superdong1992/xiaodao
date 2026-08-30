import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  auditOracle,
  semanticMethodMapping,
} from "../runtime/macos-codex-luna-fast-e2e-runner.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "..");

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

function validMethods() {
  return {
    methods: [
      {
        id: "generated-api",
        evidence_markers: [
          "API_COMPLETE service=",
          "DEADLOOP_DETECTED service=",
          "LATE_RESPONSE service=",
        ],
        activation_markers: [
          "API_COMPLETE service=",
          "DEADLOOP_DETECTED service=",
          "LATE_RESPONSE service=",
        ],
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
}

test("Fast semantic mapping permits method-local shared activation markers", () => {
  const methods = validMethods();
  const mapped = semanticMethodMapping(methods);
  assert.equal(mapped.get("api_execution_overrun").id, "generated-api");
  assert.equal(mapped.get("server_receive_queueing").id, "generated-queue");
  assert.equal(mapped.get("client_receive_blocked").id, "generated-client");

  const lowerCase = validMethods();
  for (const method of lowerCase.methods) {
    method.evidence_markers = method.evidence_markers.map((marker) => marker.toLowerCase());
    method.activation_markers = method.activation_markers.map((marker) => marker.toLowerCase());
  }
  assert.equal(
    semanticMethodMapping(lowerCase).get("client_receive_blocked").id,
    "generated-client",
  );

  methods.methods.push({
    id: "ambiguous-client",
    evidence_markers: ["LATE_RESPONSE service="],
    activation_markers: ["LATE_RESPONSE service="],
  });
  assert.throws(
    () => semanticMethodMapping(methods),
    (error) => error.code === "MACOS_CODEX_LUNA_DIAGNOSIS_SHAPE_INVALID",
  );
});

test("unrelated-log-noise fails when confirmed evidence includes a noise event", (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-luna-fast-noise-"));
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
    (error) => error.code === "MACOS_CODEX_LUNA_FORBIDDEN_TERM_PRESENT",
  );
});

test("Fast runner has no Release fixture or Methods cache dependency", () => {
  const source = fs.readFileSync(
    path.join(ROOT, "tools/test-flow/quick-validation/codex-luna/runtime/macos-codex-luna-fast-e2e-runner.mjs"),
    "utf8",
  );
  assert.equal(source.includes("tests/cases/release"), false);
  assert.equal(source.includes("defaultRegistrationInput"), false);
  assert.equal(source.includes("validateMethodsCache"), false);
});
