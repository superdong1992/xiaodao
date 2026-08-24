import assert from "node:assert/strict";
import test from "node:test";

import {
  clientPrompt,
  extractCommandHttpEntries,
  structuredMcpData,
} from "../runtime-support/macos-codex-luna-e2e-runner.mjs";

test("client prompt contains only mapped facts, archive identity, finite workflow, and no oracle", () => {
  const prompt = clientPrompt({
    mapped: {
      raw_problem_text: "problem",
      statement: "statement",
      expected_behavior: "expected",
      actual_behavior: "actual",
      scope: "scope",
      goals: ["goal"],
      non_goals: ["non-goal"],
      constraints: ["constraint"],
      completion_criteria: ["criterion"],
      initial_user_fact_names: ["problem_time"],
      initial_user_fact_values: ["time"],
    },
    archivePath: "/private/tmp/run/input/logs.zip",
    archive: { size: 123, sha256: "a".repeat(64) },
    runId: "run-one",
  });
  assert.match(prompt, /create_case → get_case/);
  assert.match(prompt, /轮询总时限 12 分钟/);
  assert.match(prompt, /\/private\/tmp\/run\/input\/logs\.zip/);
  assert.doesNotMatch(prompt, /CONFIRMED|API_COMPLETE|expected_status|forbidden_evidence_terms/);
});

test("structured MCP result extractor accepts structuredContent or text JSON and rejects errors", () => {
  assert.deepEqual(structuredMcpData({ tool: "x", result: { structuredContent: { ok: true, data: { case_id: "case" }, error: null } } }), { case_id: "case" });
  assert.deepEqual(structuredMcpData({ tool: "x", result: { content: [{ type: "text", text: '{"ok":true,"data":{"artifacts":[]},"error":null}' }] } }), { artifacts: [] });
  assert.throws(
    () => structuredMcpData({ tool: "x", result: { structuredContent: { ok: false, data: null, error: { code: "FAIL" } } } }),
    (error) => error.code === "MACOS_CODEX_LUNA_MCP_RESULT_INVALID",
  );
});

test("command HTTP extraction records curl method and URL without treating unrelated shell as HTTP", () => {
  assert.deepEqual(extractCommandHttpEntries([
    { item_id: "curl", command: "/usr/bin/curl -X PUT 'http://127.0.0.1:4321/upload/token' --data-binary '@/private/tmp/logs.zip'" },
    { item_id: "hash", command: "/usr/bin/shasum -a 256 /private/tmp/logs.zip" },
  ]), [{ method: "PUT", url: "http://127.0.0.1:4321/upload/token", source: "client-command:curl" }]);
});
