import assert from "node:assert/strict";
import test from "node:test";

import {
  semanticMethodMapping,
} from "../runtime/macos-codex-luna-fast-e2e-runner.mjs";

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
