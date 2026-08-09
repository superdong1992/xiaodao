import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { ResourceRegistry } from "../lib/resources.mjs";

function fakeDocker(runId, { stopWorks = true } = {}) {
  const state = new Map([
    ["container:flow-container", { kind: "container", running: true }],
    ["volume:flow-volume", { kind: "volume" }],
  ]);
  function metadata(resource) {
    const labels = { "problem-locator.test-flow.run": runId };
    return resource.kind === "container"
      ? { Config: { Labels: labels }, State: { Running: resource.running } }
      : { Labels: labels };
  }
  return {
    state,
    runCommand(_command, args) {
      const [kind, operation] = args;
      const name = args.at(-1);
      const key = `${kind}:${name}`;
      const resource = state.get(key);
      if (operation === "inspect") {
        return resource
          ? { status: 0, stdout: JSON.stringify([metadata(resource)]), stderr: "" }
          : { status: 1, stdout: "", stderr: "missing" };
      }
      if (kind === "container" && operation === "stop") {
        if (resource && stopWorks) resource.running = false;
        return { status: stopWorks ? 0 : 1, stdout: "", stderr: "" };
      }
      if (operation === "rm") {
        state.delete(key);
        return { status: 0, stdout: "", stderr: "" };
      }
      return { status: 1, stdout: "", stderr: "unexpected" };
    },
  };
}

function registry(root, runId, docker) {
  const value = new ResourceRegistry(root, runId, {
    commandAvailable: () => true,
    runCommand: docker.runCommand,
  });
  const label = `problem-locator.test-flow.run=${runId}`;
  value.register("container", "flow-container", label);
  value.register("volume", "flow-volume", label);
  return value;
}

test("preserve policy proves the container stopped and volume remained", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-resources-"));
  try {
    const runId = "run-resource-preserve";
    const docker = fakeDocker(runId);
    const receipt = await registry(root, runId, docker).apply({ preserve: true });
    assert.equal(receipt.status, "PASS");
    assert.equal(receipt.remaining.length, 2);
    assert.equal(
      receipt.inspected.find((item) => item.kind === "container")?.after_state,
      "STOPPED",
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("preserve policy errors when live inspect still sees a running container", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-resources-"));
  try {
    const runId = "run-resource-running";
    const docker = fakeDocker(runId, { stopWorks: false });
    const receipt = await registry(root, runId, docker).apply({ preserve: true });
    assert.equal(receipt.status, "ERROR");
    assert.equal(
      receipt.inspected.find((item) => item.kind === "container")?.after_state,
      "RUNNING_OR_UNKNOWN",
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("delete policy proves every exact registered resource absent", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-resources-"));
  try {
    const runId = "run-resource-delete";
    const docker = fakeDocker(runId);
    const receipt = await registry(root, runId, docker).apply({ preserve: false });
    assert.equal(receipt.status, "PASS");
    assert.deepEqual(receipt.remaining, []);
    assert.ok(receipt.inspected.every((item) => item.after === "ABSENT"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
