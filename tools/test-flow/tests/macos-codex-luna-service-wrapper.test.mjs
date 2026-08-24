import assert from "node:assert/strict";
import test from "node:test";

import {
  controlledEnvironment,
  parseArguments,
  serverInvocationPhase,
} from "../runtime-support/macos-codex-luna-service-wrapper.mjs";

test("server wrapper deterministically distinguishes route, logparse, diagnose, and review", () => {
  assert.equal(serverInvocationPhase('<<<SECTION 3 JOB_INSTRUCTION>>>\n{"job_type":"ROUTE"}', "/private/tmp/job"), "ROUTE");
  assert.equal(serverInvocationPhase('<<<SECTION 3 JOB_INSTRUCTION>>>\n{"job_type":"DIAGNOSE"}', "/private/tmp/job.logparse-preprocess"), "LOGPARSE");
  assert.equal(serverInvocationPhase('<<<SECTION 3 JOB_INSTRUCTION>>>\n{"job_type":"DIAGNOSE"}', "/private/tmp/job"), "DIAGNOSE");
  assert.equal(serverInvocationPhase('<<<SECTION 3 JOB_INSTRUCTION>>>\n{"job_type":"REVIEW"}', "/private/tmp/job"), "REVIEW");
  assert.throws(() => serverInvocationPhase("no job", "/private/tmp/job"), (error) => error.code === "MACOS_CODEX_LUNA_SERVICE_PHASE_INVALID");
});

test("server wrapper forwards only controlled process inputs plus an all-or-none broker capability", () => {
  const basic = controlledEnvironment({ PATH: "/bad", HOME: "/secret", RANDOM_SECRET: "secret" });
  assert.equal(basic.environment.PATH, "/usr/bin:/bin:/usr/sbin:/sbin");
  assert.equal(Object.hasOwn(basic.environment, "HOME"), false);
  assert.equal(Object.hasOwn(basic.environment, "RANDOM_SECRET"), false);
  assert.deepEqual(basic.brokerKeys, []);
  const broker = controlledEnvironment({
    PROBLEM_LOCATOR_LOGPARSE_ENDPOINT: "http://127.0.0.1:4321/session",
    PROBLEM_LOCATOR_LOGPARSE_TOKEN: "opaque-token",
  });
  assert.deepEqual(broker.brokerKeys, ["PROBLEM_LOCATOR_LOGPARSE_ENDPOINT", "PROBLEM_LOCATOR_LOGPARSE_TOKEN"]);
  assert.throws(
    () => controlledEnvironment({ PROBLEM_LOCATOR_LOGPARSE_ENDPOINT: "http://127.0.0.1:4321/session" }),
    (error) => error.code === "MACOS_CODEX_LUNA_SERVICE_BROKER_ENV_INVALID",
  );
});

test("server wrapper CLI accepts only complete unique name/value arguments", () => {
  const values = parseArguments([
    "--codex-entry", "/codex",
    "--auth-source", "/auth",
    "--skill-source", "/skill",
    "--private-root", "/private",
    "--evidence-root", "/evidence",
    "--usage-root", "/usage",
    "--run-id", "run",
  ]);
  assert.equal(values["run-id"], "run");
  assert.throws(() => parseArguments(["--run-id", "run"]), (error) => error.code === "MACOS_CODEX_LUNA_SERVICE_ARGUMENT_MISSING");
  assert.throws(() => parseArguments(["--run-id", "run", "--run-id", "again"]), (error) => error.code === "MACOS_CODEX_LUNA_SERVICE_ARGUMENT_DUPLICATE");
});
