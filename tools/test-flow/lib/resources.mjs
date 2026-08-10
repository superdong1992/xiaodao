import fs from "node:fs";
import path from "node:path";
import { commandExists, ensureDirectory, runSync } from "./util.mjs";

const SAFE_NAME = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/;

export class ResourceRegistry {
  constructor(
    attemptRoot,
    runId,
    {
      commandAvailable = commandExists,
      runCommand = runSync,
      dockerContext = null,
    } = {},
  ) {
    this.attemptRoot = attemptRoot;
    this.runId = runId;
    this.filePath = path.join(attemptRoot, "payload", "resources.ndjson");
    this.resources = [];
    this.commandAvailable = commandAvailable;
    this.runCommand = runCommand;
    this.dockerContext = dockerContext;
  }

  docker(args) {
    return this.runCommand("docker", [
      ...(this.dockerContext && this.dockerContext !== "default" ? ["--context", this.dockerContext] : []),
      ...args,
    ]);
  }

  register(kind, name, label) {
    if (!["container", "volume"].includes(kind)) throw new Error(`RESOURCE_KIND:${kind}`);
    if (!SAFE_NAME.test(name)) throw new Error(`RESOURCE_NAME:${name}`);
    if (label !== `problem-locator.test-flow.run=${this.runId}`) throw new Error("RESOURCE_LABEL_MISMATCH");
    if (this.resources.some((entry) => entry.kind === kind && entry.name === name)) throw new Error(`RESOURCE_DUPLICATE:${kind}:${name}`);
    const record = { schema_version: 2, kind, name, label };
    ensureDirectory(path.dirname(this.filePath));
    fs.appendFileSync(this.filePath, `${JSON.stringify(record)}\n`, { encoding: "utf8", mode: 0o600 });
    this.resources.push(record);
  }

  loadExternalRecords() {
    if (!fs.existsSync(this.filePath)) return;
    for (const line of fs.readFileSync(this.filePath, "utf8").split(/\r?\n/).filter(Boolean)) {
      const record = JSON.parse(line);
      if (this.resources.some((entry) => entry.kind === record.kind && entry.name === record.name)) continue;
      if (record.schema_version !== 2 || !["container", "volume"].includes(record.kind) || !SAFE_NAME.test(record.name) || record.label !== `problem-locator.test-flow.run=${this.runId}`) {
        throw new Error("RESOURCE_EXTERNAL_RECORD_INVALID");
      }
      this.resources.push(record);
    }
  }

  async apply({ preserve }) {
    this.loadExternalRecords();
    if (this.resources.length === 0) return { schema_version: 2, status: "PASS", policy: preserve ? "PRESERVE" : "DELETE", inspected: [], remaining: [] };
    if (!this.commandAvailable("docker")) return { schema_version: 2, status: "ERROR", policy: preserve ? "PRESERVE" : "DELETE", code: "DOCKER_MISSING", inspected: [], remaining: this.resources };
    const inspected = [];
    const remaining = [];
    let failed = false;
    const orderedResources = [
      ...this.resources.filter((resource) => resource.kind === "container"),
      ...this.resources.filter((resource) => resource.kind === "volume"),
    ];
    for (const resource of orderedResources) {
      const kindCommand = resource.kind === "container" ? "container" : "volume";
      const inspect = this.docker([kindCommand, "inspect", resource.name]);
      if (inspect.status !== 0) {
        inspected.push({ kind: resource.kind, name: resource.name, before: "ABSENT", action: "NONE", after: "ABSENT" });
        continue;
      }
      let metadata;
      try { metadata = JSON.parse(inspect.stdout)[0]; } catch { failed = true; remaining.push(resource); continue; }
      const labels = resource.kind === "container" ? metadata.Config?.Labels : metadata.Labels;
      const actualLabel = labels?.["problem-locator.test-flow.run"];
      if (actualLabel !== this.runId) {
        failed = true;
        inspected.push({ kind: resource.kind, name: resource.name, before: "PRESENT", action: "REFUSED_LABEL_MISMATCH", after: "PRESENT" });
        remaining.push(resource);
        continue;
      }
      let action = "NONE";
      if (resource.kind === "container") {
        const stop = this.docker(["container", "stop", "--time", "10", resource.name]);
        if (stop.status !== 0) failed = true;
        action = preserve ? "STOP" : "STOP_DELETE";
        if (!preserve) {
          const remove = this.docker(["container", "rm", resource.name]);
          if (remove.status !== 0) failed = true;
        }
      } else if (!preserve) {
        action = "DELETE";
        const remove = this.docker(["volume", "rm", resource.name]);
        if (remove.status !== 0) failed = true;
      } else {
        action = "PRESERVE";
      }
      const after = this.docker([kindCommand, "inspect", resource.name]);
      const afterStatus = after.status === 0 ? "PRESENT" : "ABSENT";
      let afterState = null;
      if (afterStatus === "PRESENT") {
        try {
          const afterMetadata = JSON.parse(after.stdout)[0];
          const afterLabels = resource.kind === "container"
            ? afterMetadata.Config?.Labels
            : afterMetadata.Labels;
          if (afterLabels?.["problem-locator.test-flow.run"] !== this.runId) failed = true;
          if (resource.kind === "container") {
            afterState = afterMetadata.State?.Running === false ? "STOPPED" : "RUNNING_OR_UNKNOWN";
          }
        } catch {
          failed = true;
          afterState = "INVALID_INSPECT";
        }
      }
      inspected.push({ kind: resource.kind, name: resource.name, before: "PRESENT", action, after: afterStatus, after_state: afterState });
      if (afterStatus === "PRESENT") remaining.push(resource);
      if (
        (!preserve && afterStatus !== "ABSENT")
        || (preserve && afterStatus !== "PRESENT")
        || (preserve && resource.kind === "container" && afterState !== "STOPPED")
      ) failed = true;
    }
    return {
      schema_version: 2,
      status: failed ? "ERROR" : "PASS",
      policy: preserve ? "PRESERVE" : "DELETE",
      inspected,
      remaining,
    };
  }
}
