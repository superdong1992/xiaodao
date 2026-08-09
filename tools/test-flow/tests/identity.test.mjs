import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { pythonImportPathIdentity } from "../lib/identity.mjs";

test("Python import identity changes when external PYTHONPATH content changes", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-python-path-"));
  try {
    const repository = path.join(root, "repo");
    const external = path.join(root, "external");
    fs.mkdirSync(repository);
    fs.mkdirSync(external);
    const module = path.join(external, "dependency.py");
    fs.writeFileSync(module, "VALUE = 1\n");

    const first = pythonImportPathIdentity(repository, { sys_path: [repository, external] });
    assert.deepEqual(first[0], { index: 0, kind: "repository", path: "." });
    assert.equal(first[1].status, "PRESENT");

    fs.writeFileSync(module, "VALUE = 2\n");
    const second = pythonImportPathIdentity(repository, { sys_path: [repository, external] });
    assert.notEqual(first[1].digest, second[1].digest);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
