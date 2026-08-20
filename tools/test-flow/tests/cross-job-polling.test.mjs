import assert from "node:assert/strict";
import test from "node:test";

import {
  fixedGetCasePollInput,
  fixedGetCasePollingInvariant,
} from "../lib/cross-job-polling.mjs";

test("CrossJob polling keeps one complete null-target get-case input", () => {
  const caseId = "00000000-0000-0000-0000-000000000001";
  assert.deepEqual(fixedGetCasePollInput(caseId), {
    case_id: caseId,
    wait_for_job_id: null,
    wait_seconds: 30,
  });
  const instruction = fixedGetCasePollingInvariant(caseId);
  assert.match(
    instruction,
    /\{"case_id":"00000000-0000-0000-0000-000000000001","wait_for_job_id":null,"wait_seconds":30\}/,
  );
  assert.match(instruction, /Keep wait_for_job_id null for every poll/);
  assert.match(instruction, /including RUNNING, WAITING_INPUT, and REVIEWING/);
  assert.match(instruction, /make at most one immediate corrected call/);
  assert.match(instruction, /never repeat the malformed input/);
});

test("CrossJob polling refuses to render a template without a case id", () => {
  assert.throws(() => fixedGetCasePollInput(""), /non-empty string/);
  assert.throws(() => fixedGetCasePollingInvariant(null), /non-empty string/);
});
