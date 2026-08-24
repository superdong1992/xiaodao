import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  buildMethodsEnvironment,
  safeMethodsRunnerError,
} from "../runtime-support/macos-codex-luna-methods-runner.mjs";

test("Methods model commands receive only the selected validator runtime ahead of the system PATH", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-codex-luna-methods-env-"));
  const pythonEntry = path.join(root, "python");
  fs.writeFileSync(pythonEntry, "runtime", { mode: 0o500 });
  const environment = buildMethodsEnvironment(
    { PATH: "/ambient/secret", API_TOKEN: "secret" },
    {
      codexHome: path.join(root, "codex-home"),
      home: path.join(root, "home"),
      temporary: path.join(root, "tmp"),
      pythonEntry,
    },
  );
  assert.equal(environment.PATH, `${root}:/usr/bin:/bin:/usr/sbin:/sbin`);
  assert.equal(environment.API_TOKEN, undefined);
});

test("Methods runner failure exposes only closed protocol identity fields", () => {
  const error = new Error("rejected");
  error.code = "CODEX_LUNA_APP_SERVER_NOTIFICATION_REJECTED";
  error.details = {
    method: "thread/name/updated",
    line: 42,
    item_type: null,
    function_name: "ignored-if-present-but-safe",
    id: 7,
    role: "developer",
    field: "initial_turns",
    path: "/secret/path",
    token: "credential-canary",
    nested: { raw: "message" },
  };
  assert.deepEqual(safeMethodsRunnerError(error), {
    schema_version: 1,
    status: "FAIL",
    code: "CODEX_LUNA_APP_SERVER_NOTIFICATION_REJECTED",
    message: "rejected",
    details: {
      method: "thread/name/updated",
      line: 42,
      item_type: null,
      function_name: "ignored-if-present-but-safe",
      id: 7,
      role: "developer",
      field: "initial_turns",
    },
  });
});
