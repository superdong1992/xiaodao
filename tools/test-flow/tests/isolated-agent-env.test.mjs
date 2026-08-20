import assert from "node:assert/strict";
import test from "node:test";

import {
  assertIsolatedAgentInboundEnvironment,
  buildIsolatedAgentEnvironment,
  environmentKeySummary,
  ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY,
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_LIMIT,
  validEnvironmentKeySummary,
} from "../runtime-support/isolated-agent-env.mjs";


test("isolated Agent environment keeps only runtime necessities and explicit Test Flow inputs", () => {
  const ambient = {
    PATH: "/runtime/bin",
    HOME: "/runtime/home",
    SystemRoot: "C:\\Windows",
    TEMP: "/runtime/tmp",
    ANTHROPIC_AUTH_TOKEN: "provider-secret-canary",
    HTTPS_PROXY: "http://proxy-secret-canary",
    AWS_SECRET_ACCESS_KEY: "cloud-secret-canary",
    AZURE_CLIENT_SECRET: "cloud-secret-canary",
    GITHUB_TOKEN: "ci-secret-canary",
    CI: "true",
    CLAUDE_CODE_MAX_OUTPUT_TOKENS: "32000",
    MAX_STRUCTURED_OUTPUT_RETRIES: "99",
    S08_REAL_AGENT_GATE: "ambient-spoof",
    TEST_FLOW_AMBIENT_SECRET_CANARY: "ambient-secret-canary",
  };
  const environment = buildIsolatedAgentEnvironment({
    ambient,
    explicit: {
      CLAUDE_CONFIG_DIR: "/isolated/config",
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
      S08_REAL_AGENT_COMMAND: "/isolated/wrapper",
      S08_REAL_AGENT_GATE: "1",
    },
  });
  assert.deepEqual(environment, {
    PATH: "/runtime/bin",
    HOME: "/runtime/home",
    SystemRoot: "C:\\Windows",
    TEMP: "/runtime/tmp",
    CLAUDE_CONFIG_DIR: "/isolated/config",
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    S08_REAL_AGENT_COMMAND: "/isolated/wrapper",
    S08_REAL_AGENT_GATE: "1",
  });
  assert.equal(ISOLATED_AGENT_ENV_POLICY_VERSION, "isolated-agent-env-allowlist-v3");
  assert.equal(JSON.stringify(environment).includes("secret-canary"), false);
  assert.equal(Object.hasOwn(environment, ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY), false);
  assert.equal(Object.hasOwn(environment, ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY), false);
});

test("Claude output and structured retry controls are child-only and cannot be inherited or supplied inbound", () => {
  assert.throws(
    () => assertIsolatedAgentInboundEnvironment({ PATH: "/bin", [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: "99" }),
    /ISOLATED_AGENT_INBOUND_KEY_FORBIDDEN/,
  );
  assert.throws(
    () => assertIsolatedAgentInboundEnvironment({ PATH: "/bin", [ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY]: "32000" }),
    /ISOLATED_AGENT_INBOUND_KEY_FORBIDDEN/,
  );
  assert.throws(
    () => buildIsolatedAgentEnvironment({
      ambient: { PATH: "/bin", [ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY]: "32000" },
      explicit: {
        [ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY]: "64000",
        [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: "99",
      },
    }),
    /ISOLATED_AGENT_EXPLICIT_KEY_FORBIDDEN/,
  );
  const child = buildIsolatedAgentEnvironment({
    ambient: { PATH: "/bin", [ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY]: "32000" },
    explicit: {
      [ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY]: "64000",
      [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: String(ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_LIMIT),
    },
    allowClaudeChildControls: true,
  });
  assert.deepEqual(child, {
    PATH: "/bin",
    [ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY]: "64000",
    [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: "2",
  });
});

test("the Skill-generation audit path is explicit-only and reaches pytest", () => {
  const environment = buildIsolatedAgentEnvironment({
    ambient: {
      PATH: "/bin",
      S08_REAL_SKILL_GENERATION_AUDIT_PATH: "/ambient/spoof.json",
    },
    explicit: {
      S08_REAL_SKILL_GENERATION_AUDIT_PATH: "/evidence/scenario-evaluation-audit.json",
    },
  });
  assert.deepEqual(environment, {
    PATH: "/bin",
    S08_REAL_SKILL_GENERATION_AUDIT_PATH: "/evidence/scenario-evaluation-audit.json",
  });
  assert.deepEqual(
    assertIsolatedAgentInboundEnvironment(environment).key_names,
    ["PATH", "S08_REAL_SKILL_GENERATION_AUDIT_PATH"],
  );
  assert.deepEqual(buildIsolatedAgentEnvironment({ ambient: environment }), { PATH: "/bin" });
});

test("environment receipt contains only sorted key names and a verifiable digest", () => {
  const summary = environmentKeySummary({ HOME: "/secret/home", PATH: "/secret/bin" });
  assert.deepEqual(summary.key_names, ["HOME", "PATH"]);
  assert.equal(summary.key_count, 2);
  assert.equal(validEnvironmentKeySummary(summary), true);
  assert.equal(JSON.stringify(summary).includes("/secret/"), false);
  assert.equal(validEnvironmentKeySummary({ ...summary, key_names_sha256: "0".repeat(64) }), false);
});

test("Windows runtime key casing is canonicalized without inheriting adjacent variables", () => {
  const environment = buildIsolatedAgentEnvironment({
    ambient: { Path: "C:\\runtime", SYSTEMROOT: "C:\\Windows", ComSpec: "forbidden" },
    platform: "win32",
  });
  assert.deepEqual(environment, { PATH: "C:\\runtime", SystemRoot: "C:\\Windows" });
});

test("unknown explicit keys and incomplete session credentials fail closed", () => {
  assert.throws(
    () => buildIsolatedAgentEnvironment({ ambient: {}, explicit: { ANTHROPIC_API_KEY: "forbidden" } }),
    /ISOLATED_AGENT_EXPLICIT_KEY_FORBIDDEN/,
  );
  assert.throws(
    () => buildIsolatedAgentEnvironment({
      ambient: { PROBLEM_LOCATOR_LOGPARSE_TOKEN: "session" },
      allowSessionCredentials: true,
    }),
    /ISOLATED_AGENT_SESSION_CREDENTIALS_INCOMPLETE/,
  );
  assert.throws(
    () => assertIsolatedAgentInboundEnvironment({ PATH: "/bin", AWS_SESSION_TOKEN: "forbidden" }),
    /ISOLATED_AGENT_INBOUND_KEY_FORBIDDEN/,
  );
  const broker = buildIsolatedAgentEnvironment({
    ambient: {
      PATH: "/bin",
      PROBLEM_LOCATOR_LOGPARSE_ENDPOINT: "http://127.0.0.1:1234",
      PROBLEM_LOCATOR_LOGPARSE_TOKEN: "session",
    },
    allowSessionCredentials: true,
  });
  assert.deepEqual(Object.keys(broker).sort(), ["PATH", "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT", "PROBLEM_LOCATOR_LOGPARSE_TOKEN"]);
});

test("pytest-owned inbound keys are accepted by the wrapper but never reach Claude", () => {
  const inbound = {
    PATH: "/bin",
    LC_CTYPE: "C.UTF-8",
    PYTEST_CURRENT_TEST: "tests/real/test_gate.py::test_gate (call)",
    PYTEST_VERSION: "8.3.5",
  };
  const summary = assertIsolatedAgentInboundEnvironment(inbound);
  assert.deepEqual(summary.key_names, ["LC_CTYPE", "PATH", "PYTEST_CURRENT_TEST", "PYTEST_VERSION"]);
  const claude = buildIsolatedAgentEnvironment({ ambient: inbound });
  assert.deepEqual(claude, { PATH: "/bin", LC_CTYPE: "C.UTF-8" });
  assert.equal(Object.hasOwn(claude, "PYTEST_CURRENT_TEST"), false);
  assert.equal(Object.hasOwn(claude, "PYTEST_VERSION"), false);
});

test("Windows-injected inbound keys are accepted case-insensitively but never reach Claude", () => {
  const inbound = {
    Path: "C:\\runtime",
    SYSTEMROOT: "C:\\Windows",
    HOMEDRIVE: "C:",
    HOMEPATH: "\\Users\\runner",
    SYSTEMDRIVE: "C:",
    USERDOMAIN: "TEST-DOMAIN",
    USERNAME: "runner",
  };
  const summary = assertIsolatedAgentInboundEnvironment(inbound, { platform: "win32" });
  assert.deepEqual(summary.key_names, [
    "HOMEDRIVE",
    "HOMEPATH",
    "Path",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "USERDOMAIN",
    "USERNAME",
  ]);
  assert.deepEqual(buildIsolatedAgentEnvironment({ ambient: inbound, platform: "win32" }), {
    PATH: "C:\\runtime",
    SystemRoot: "C:\\Windows",
  });
});
