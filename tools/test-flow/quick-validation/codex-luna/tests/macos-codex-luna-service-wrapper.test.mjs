import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  controlledEnvironment,
  canonicalizeMethodsDraft,
  parseArguments,
  publishLinuxServiceDraft,
  removeLinuxServiceProject,
  repositorySkillPaths,
  runServiceLogparseCommand,
  safeServiceError,
  sealServiceOutcomeDraft,
  serverInvocationPhase,
  stageLinuxServiceProject,
} from "../runtime/macos-codex-luna-service-wrapper.mjs";

test("service Skill keeps missing registered artifacts in the post-route requirement flow", () => {
  const skill = fs.readFileSync(path.join(process.cwd(), "tools", "test-flow", "quick-validation", "codex-luna", "fixtures", "service-skill", "problem-locator-service-agent", "SKILL.md"), "utf8");
  assert.match(skill, /required_artifacts.*later DIAGNOSE job can request/);
  assert.match(skill, /select that registration as `MATCHED`/);
  assert.match(skill, /Do not return `NO_CAPABILITY` merely because `log_archive` is absent/);
});

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
    "--finalizer-entry", "/venv/bin/problem-locator-seal-outcome-draft",
    "--logparse-entry", "/venv/bin/problem-locator-logparse",
    "--expected-cli-version", "0.149.1",
    "--private-root", "/private",
    "--evidence-root", "/evidence",
    "--usage-root", "/usage",
    "--run-id", "run",
  ]);
  assert.equal(values["run-id"], "run");
  assert.throws(() => parseArguments(["--run-id", "run"]), (error) => error.code === "MACOS_CODEX_LUNA_SERVICE_ARGUMENT_MISSING");
  assert.throws(() => parseArguments(["--run-id", "run", "--run-id", "again"]), (error) => error.code === "MACOS_CODEX_LUNA_SERVICE_ARGUMENT_DUPLICATE");
});

test("server wrapper reports only allowlisted scalar error details", () => {
  const error = Object.assign(new Error("Codex app-server request failed"), {
    code: "CODEX_LUNA_APP_SERVER_RESPONSE_ERROR",
    details: {
      id: 5,
      response_code: -32600,
      response_message: "invalid request",
      item_type: "fileChange",
      cwd_matches: false,
      errors_count: 1,
      skills_errors: '[{"message":"permission denied"}]',
      secret: "must-not-escape",
      nested: { token: "must-not-escape" },
    },
  });
  const receipt = safeServiceError(error);
  assert.deepEqual(receipt.details, { id: 5, response_code: -32600, response_message: "invalid request", item_type: "fileChange", cwd_matches: false, errors_count: 1, skills_errors: '[{"message":"permission denied"}]' });
  assert.doesNotMatch(JSON.stringify(receipt), /must-not-escape/);
});

test("server wrapper enumerates only repository-owned Skill entry files", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-repo-skills-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, ".agents", "skills", "one"), { recursive: true });
  fs.mkdirSync(path.join(root, ".agents", "skills", "missing"), { recursive: true });
  fs.writeFileSync(path.join(root, ".agents", "skills", "one", "SKILL.md"), "---\nname: one\n---\n");
  assert.deepEqual(repositorySkillPaths(root), [path.join(root, ".agents", "skills", "one", "SKILL.md")]);
});

test("Linux service project contains Codex metadata without changing the product Workspace root shape", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "linux-luna-service-project-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const workspace = path.join(root, "workspace");
  fs.mkdirSync(path.join(workspace, "inputs"), { recursive: true });
  fs.mkdirSync(path.join(workspace, "output"), { recursive: true });
  fs.mkdirSync(path.join(workspace, "runtime", "tool-state"), { recursive: true });
  const resource = path.join(root, "immutable-resource.json");
  fs.writeFileSync(resource, "{}\n");
  fs.linkSync(resource, path.join(workspace, "inputs", "request.json"));
  assert.equal(fs.statSync(path.join(workspace, "inputs", "request.json")).nlink >= 2, true);
  fs.writeFileSync(path.join(workspace, "runtime", "context.txt"), "fixed context\n");

  const project = stageLinuxServiceProject(workspace);
  assert.equal(path.dirname(project), path.join(workspace, "runtime"));
  assert.equal(fs.readFileSync(path.join(project, "inputs", "request.json"), "utf8"), "{}\n");
  assert.equal(fs.statSync(path.join(project, "inputs", "request.json")).nlink, 1);
  for (const name of [".agents", ".codex", ".git"]) fs.mkdirSync(path.join(project, name));
  const draft = '{"schema_version":2,"result_type":"NO_CAPABILITY"}\n';
  fs.writeFileSync(path.join(project, "output", "job_outcome.draft.json"), draft);

  const publication = publishLinuxServiceDraft({ phase: "ROUTE", workspaceRoot: workspace, projectRoot: project });
  assert.equal(publication.status, "PASS");
  assert.equal(fs.readFileSync(path.join(workspace, "output", "job_outcome.draft.json"), "utf8"), draft);
  removeLinuxServiceProject({ workspaceRoot: workspace, projectRoot: project });

  assert.deepEqual(fs.readdirSync(workspace).sort(), ["inputs", "output", "runtime"]);
  assert.deepEqual(fs.readdirSync(path.join(workspace, "runtime")).sort(), ["context.txt", "tool-state"]);
});

test("server wrapper seals a service outcome draft before the Agent process exits", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-sealer-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const finalizer = path.join(root, ".venv", "bin", "problem-locator-seal-outcome-draft");
  const workspace = path.join(root, "workspace");
  fs.mkdirSync(path.dirname(finalizer), { recursive: true });
  fs.mkdirSync(path.join(workspace, "runtime", "tool-state"), { recursive: true });
  fs.mkdirSync(path.join(workspace, "inputs"), { recursive: true });
  fs.mkdirSync(path.join(workspace, "output"), { recursive: true });
  fs.writeFileSync(finalizer, "#!/bin/sh\nprintf '{\"schema_version\":2}' > runtime/tool-state/agent-job-outcome-draft.finalized\n", { mode: 0o700 });
  const receipt = sealServiceOutcomeDraft({ phase: "ROUTE", workspaceRoot: workspace, finalizerEntry: finalizer });
  assert.equal(receipt.status, "PASS");
  assert.equal(receipt.invoked, true);
  assert.match(receipt.marker_sha256, /^[a-f0-9]{64}$/);
  assert.deepEqual(sealServiceOutcomeDraft({ phase: "LOGPARSE", workspaceRoot: workspace, sourceRoot: root }), { required: false, invoked: false, status: "SKIP" });
  assert.deepEqual(sealServiceOutcomeDraft({ phase: "DIAGNOSE", workspaceRoot: workspace, sourceRoot: root }), { required: false, invoked: false, status: "SKIP" });
  assert.deepEqual(sealServiceOutcomeDraft({ phase: "REVIEW", workspaceRoot: workspace, sourceRoot: root }), { required: false, invoked: false, status: "SKIP" });
});

test("server wrapper runs the one product-owned Logparse command without persisting broker credentials", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-logparse-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const command = path.join(root, ".venv", "bin", "problem-locator-logparse");
  const workspace = path.join(root, "workspace");
  const request = "output/proposals/methods-preprocess/request.json";
  const result = "output/proposals/methods-preprocess/result.json";
  fs.mkdirSync(path.dirname(command), { recursive: true });
  fs.mkdirSync(path.join(workspace, "output", "proposals", "methods-preprocess"), { recursive: true });
  fs.writeFileSync(path.join(workspace, request), "{}\n");
  fs.writeFileSync(command, "#!/bin/sh\ntest -n \"$PROBLEM_LOCATOR_LOGPARSE_ENDPOINT\" || exit 3\ntest -n \"$PROBLEM_LOCATOR_LOGPARSE_TOKEN\" || exit 4\nprintf '{}\\n' > \"$5\"\n", { mode: 0o700 });
  const receipt = runServiceLogparseCommand({
    phase: "LOGPARSE",
    prompt: `Run exactly one command:\nproblem-locator-logparse parse-targets --request ${request} --result ${result}\n`,
    workspaceRoot: workspace,
    logparseEntry: command,
    environment: { PROBLEM_LOCATOR_LOGPARSE_ENDPOINT: "http://127.0.0.1:1/session", PROBLEM_LOCATOR_LOGPARSE_TOKEN: "secret-canary" },
  });
  assert.equal(receipt.status, "PASS");
  assert.equal(receipt.invoked, true);
  assert.doesNotMatch(JSON.stringify(receipt), /secret-canary|127\.0\.0\.1/);
});

test("server wrapper mechanically canonicalizes only Methods diagnosis and review drafts", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-methods-draft-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const diagnosis = path.join(root, "output", "method-diagnosis.draft.json");
  fs.mkdirSync(path.dirname(diagnosis), { recursive: true });
  fs.writeFileSync(diagnosis, '{\n  "z": 1,\n  "a": {"y": 2, "x": 3}\n}\n');
  const receipt = canonicalizeMethodsDraft({ phase: "DIAGNOSE", workspaceRoot: root });
  assert.equal(receipt.status, "PASS");
  assert.equal(fs.readFileSync(diagnosis, "utf8"), '{"a":{"x":3,"y":2},"z":1}\n');
  assert.deepEqual(canonicalizeMethodsDraft({ phase: "ROUTE", workspaceRoot: root }), { required: false, invoked: false, status: "SKIP" });
  const review = path.join(root, "output", "method-review.draft.json");
  fs.writeFileSync(review, '{"verdict":"PASS","schema_version":1}\n');
  assert.equal(canonicalizeMethodsDraft({ phase: "REVIEW", workspaceRoot: root }).status, "PASS");
});
