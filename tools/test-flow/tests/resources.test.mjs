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

test("delete policy removes containers before a volume even when the volume was registered first", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-resources-"));
  try {
    const runId = "run-resource-volume-first";
    const docker = fakeDocker(runId);
    const calls = [];
    const original = docker.runCommand;
    docker.runCommand = (command, args) => {
      calls.push([...args]);
      return original(command, args);
    };
    const value = new ResourceRegistry(root, runId, { commandAvailable: () => true, runCommand: docker.runCommand });
    const label = `problem-locator.test-flow.run=${runId}`;
    value.register("volume", "flow-volume", label);
    value.register("container", "flow-container", label);
    const receipt = await value.apply({ preserve: false });
    assert.equal(receipt.status, "PASS");
    const containerRemove = calls.findIndex((args) => args[0] === "container" && args[1] === "rm");
    const volumeRemove = calls.findIndex((args) => args[0] === "volume" && args[1] === "rm");
    assert.ok(containerRemove >= 0 && volumeRemove > containerRemove);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("every Docker resource operation is bound to the configured context", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-resources-"));
  try {
    const runId = "run-resource-context";
    const calls = [];
    const value = new ResourceRegistry(root, runId, {
      commandAvailable: () => true,
      dockerContext: "colima",
      runCommand: (_command, args) => {
        calls.push([...args]);
        return { status: 1, stdout: "", stderr: "absent" };
      },
    });
    value.register("container", "flow-container", `problem-locator.test-flow.run=${runId}`);
    const receipt = await value.apply({ preserve: true });
    assert.equal(receipt.status, "PASS");
    assert.ok(calls.length > 0);
    assert.ok(calls.every((args) => args[0] === "--context" && args[1] === "colima"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("logical default resource cleanup follows the selected Docker Desktop context without naming the legacy default context", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-resources-"));
  try {
    const runId = "run-resource-logical-default";
    const calls = [];
    const value = new ResourceRegistry(root, runId, {
      commandAvailable: () => true,
      dockerContext: "default",
      runCommand: (_command, args) => {
        calls.push([...args]);
        return { status: 1, stdout: "", stderr: "absent" };
      },
    });
    value.register("container", "flow-container", `problem-locator.test-flow.run=${runId}`);
    assert.equal((await value.apply({ preserve: true })).status, "PASS");
    assert.ok(calls.length > 0);
    assert.ok(calls.every((args) => args[0] !== "--context"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
