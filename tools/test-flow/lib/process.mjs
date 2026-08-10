import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { ensureDirectory, FlowError, readJson } from "./util.mjs";

const SEMANTIC_TYPES = new Set([
  "tool_result",
  "request.completed",
  "job.started",
  "job.stage_changed",
  "job.state_changed",
  "job.completed",
  "stage.progress",
  "stage.completed",
  "mcp.tool.completed",
  "attachment.upload.completed",
  "case.status.changed",
  "job.pending_persisted",
  "job.queued",
  "job.queue.duplicate",
  "job.claimed",
  "job.outcome.produced",
  "job.outcome.applied",
  "job.outcome.rejected",
  "job.outcome.stale",
]);
const PROGRESS_ALLOWLIST_VERSION = "test-flow-progress-v2";

function semanticEvent(line) {
  if (line.startsWith("TEST_FLOW_PROGRESS ")) {
    const type = line.slice("TEST_FLOW_PROGRESS ".length).trim().split(/\s+/, 1)[0];
    return SEMANTIC_TYPES.has(type) ? type : null;
  }
  let value;
  try { value = JSON.parse(line); } catch { return null; }
  if (SEMANTIC_TYPES.has(value?.event_type)) return value.event_type;
  if (value?.type === "result") return "request.completed";
  if (value?.type === "user" && Array.isArray(value.message?.content)) {
    if (value.message.content.some((block) => block?.type === "tool_result")) return "tool_result";
  }
  return null;
}

function addUsage(usage, line) {
  let value;
  try { value = JSON.parse(line); } catch { return; }
  const candidates = [value?.usage, value?.message?.usage, value?.result?.usage].filter(Boolean);
  for (const candidate of candidates) {
    for (const [source, target] of [
      ["input_tokens", "input_tokens"],
      ["output_tokens", "output_tokens"],
      ["cache_creation_input_tokens", "cache_creation_input_tokens"],
      ["cache_read_input_tokens", "cache_read_input_tokens"],
    ]) {
      if (Number.isFinite(candidate[source])) usage[target] += candidate[source];
    }
  }
  if (Number.isFinite(value?.total_cost_usd)) usage.cost_usd = Math.max(usage.cost_usd, value.total_cost_usd);
  if (Number.isFinite(value?.cost_usd)) usage.cost_usd = Math.max(usage.cost_usd, value.cost_usd);
}

class CappedLog {
  constructor(filePath, limitBytes) {
    ensureDirectory(path.dirname(filePath));
    this.filePath = filePath;
    this.limitBytes = limitBytes;
    this.bytes = 0;
    this.truncated = false;
    this.descriptor = fs.openSync(filePath, "wx", 0o600);
  }

  write(chunk) {
    if (this.truncated) return;
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    const room = this.limitBytes - this.bytes;
    if (room > 0) {
      const written = buffer.subarray(0, room);
      fs.writeSync(this.descriptor, written);
      this.bytes += written.length;
    }
    if (buffer.length > room) this.truncated = true;
  }

  close() {
    fs.fsyncSync(this.descriptor);
    fs.closeSync(this.descriptor);
  }
}

function killTree(child, invocation) {
  if (!child.pid) return Promise.resolve({ requested: false, forced: false });
  if (process.platform === "win32") {
    try {
      fs.writeFileSync(invocation.cancelPath, "terminate\n", { encoding: "utf8", mode: 0o600, flag: "wx" });
    } catch {
      let forced = false;
      try { forced = child.kill(); } catch {}
      return Promise.resolve({ requested: true, forced, mechanism: "wrapper-job-close-fallback" });
    }
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        let forced = false;
        try { forced = child.kill(); } catch {}
        resolve({ requested: true, forced, mechanism: "wrapper-control-timeout" });
      }, 5000);
      timer.unref();
      child.once("exit", () => {
        clearTimeout(timer);
        resolve({ requested: true, forced: false, mechanism: "wrapper-control" });
      });
    });
  }
  let signalled = false;
  try { process.kill(-child.pid, "SIGTERM"); signalled = true; } catch {}
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      let forced = false;
      try { process.kill(-child.pid, "SIGKILL"); forced = true; } catch {}
      resolve({ requested: signalled, forced });
    }, 5000);
    timer.unref();
    child.once("exit", () => { clearTimeout(timer); resolve({ requested: signalled, forced: false }); });
  });
}

function windowsInvocation({ repoRoot, command, args, cwd, environment, stdoutPath, stderrPath, rawLogLimitBytes }) {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-"));
  const specPath = path.join(temporaryRoot, "spec.json");
  const statusPath = path.join(temporaryRoot, "status.json");
  const cancelPath = path.join(temporaryRoot, "cancel");
  fs.writeFileSync(specPath, JSON.stringify({
    executable: command,
    arguments: args,
    working_directory: cwd,
    environment,
    stdout_path: stdoutPath,
    stderr_path: stderrPath,
    raw_log_limit_bytes: rawLogLimitBytes,
    cancel_path: cancelPath,
  }), { encoding: "utf8", mode: 0o600, flag: "wx" });
  return {
    command: "powershell.exe",
    args: ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path.join(repoRoot, "tools", "test-flow", "adapters", "windows-process.ps1"), "-SpecPath", specPath, "-StatusPath", statusPath],
    temporaryRoot,
    statusPath,
    cancelPath,
    childWritesLogs: true,
  };
}

function pollNewLines(filePath, state, onLine) {
  if (!fs.existsSync(filePath)) return;
  const size = fs.statSync(filePath).size;
  if (size <= state.offset) return;
  const descriptor = fs.openSync(filePath, "r");
  try {
    const buffer = Buffer.alloc(size - state.offset);
    fs.readSync(descriptor, buffer, 0, buffer.length, state.offset);
    state.offset = size;
    state.tail += buffer.toString("utf8");
    const lines = state.tail.split(/\r?\n/);
    state.tail = lines.pop() ?? "";
    for (const line of lines) onLine(line);
  } finally { fs.closeSync(descriptor); }
}

export function runProcess({
  repoRoot,
  attemptRoot,
  stage,
  command,
  args = [],
  cwd = repoRoot,
  env = {},
  hardTimeoutSeconds,
  noProgressSeconds = null,
  rawLogLimitBytes = 128 * 1024 * 1024,
  eventWriter,
  executionId = stage.id,
  pollMilliseconds = 250,
  progressAllowlistVersion = PROGRESS_ALLOWLIST_VERSION,
}) {
  if (progressAllowlistVersion !== PROGRESS_ALLOWLIST_VERSION) {
    return Promise.reject(new FlowError(
      "PROCESS_PROGRESS_VERSION",
      `Unsupported progress allowlist version ${progressAllowlistVersion}`,
    ));
  }
  return new Promise((resolve, reject) => {
    const logsRoot = path.join(attemptRoot, "payload", "logs");
    ensureDirectory(logsRoot);
    const safeExecutionId = String(executionId).replace(/[^A-Za-z0-9_.-]/g, "-");
    const stdoutPath = path.join(logsRoot, `${safeExecutionId}.stdout.log`);
    const stderrPath = path.join(logsRoot, `${safeExecutionId}.stderr.log`);
    const environment = { ...process.env, ...env };
    let invocation = { command, args, temporaryRoot: null, statusPath: null, childWritesLogs: false };
    let stdoutLog = null;
    let stderrLog = null;
    if (process.platform === "win32") {
      invocation = windowsInvocation({ repoRoot, command, args, cwd, environment: env, stdoutPath, stderrPath, rawLogLimitBytes });
    } else {
      stdoutLog = new CappedLog(stdoutPath, rawLogLimitBytes);
      stderrLog = new CappedLog(stderrPath, rawLogLimitBytes);
    }
    const child = spawn(invocation.command, invocation.args, {
      cwd,
      env: environment,
      windowsHide: true,
      detached: process.platform !== "win32",
      stdio: invocation.childWritesLogs ? ["ignore", "ignore", "ignore"] : ["ignore", "pipe", "pipe"],
    });
    const started = process.hrtime.bigint();
    let lastProgress = started;
    let lastProgressType = "process.started";
    let termination = null;
    let killPromise = Promise.resolve(null);
    let settled = false;
    const usage = { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, cost_usd: 0 };
    const lineStates = { stdout: { tail: "", offset: 0 }, stderr: { tail: "", offset: 0 } };

    const requestTermination = (trigger) => {
      if (settled) return;
      settled = true;
      clearInterval(monitor);
      const now = process.hrtime.bigint();
      termination = {
        trigger,
        elapsed_seconds: Number(now - started) / 1e9,
        silence_seconds: Number(now - lastProgress) / 1e9,
        last_progress_type: lastProgressType,
        kill: null,
      };
      killPromise = killTree(child, invocation).then((kill) => { termination.kill = kill; return kill; });
    };
    const onLine = (line) => {
      addUsage(usage, line);
      const type = semanticEvent(line);
      if (type) {
        lastProgress = process.hrtime.bigint();
        lastProgressType = type;
        eventWriter?.write("stage.progress", { stageId: stage.id, data: { semantic_type: type } });
      }
    };
    function streamHandler(log, state) {
      return (chunk) => {
        log.write(chunk);
        if (log.truncated) requestTermination("RAW_LOG_LIMIT");
        state.tail += chunk.toString("utf8");
        const lines = state.tail.split(/\r?\n/);
        state.tail = lines.pop() ?? "";
        for (const line of lines) onLine(line);
      };
    }
    if (!invocation.childWritesLogs) {
      child.stdout.on("data", streamHandler(stdoutLog, lineStates.stdout));
      child.stderr.on("data", streamHandler(stderrLog, lineStates.stderr));
    }
    eventWriter?.write("stage.started", { stageId: stage.id, data: { kind: stage.kind } });

    const monitor = setInterval(() => {
      if (settled) return;
      const windowsRawLimit = invocation.childWritesLogs && [stdoutPath, stderrPath].some(
        (filePath) => fs.existsSync(filePath) && fs.statSync(filePath).size > rawLogLimitBytes,
      );
      if (windowsRawLimit) {
        requestTermination("RAW_LOG_LIMIT");
        return;
      }
      if (invocation.childWritesLogs) {
        pollNewLines(stdoutPath, lineStates.stdout, onLine);
        pollNewLines(stderrPath, lineStates.stderr, onLine);
      }
      const now = process.hrtime.bigint();
      const elapsed = Number(now - started) / 1e9;
      const silent = Number(now - lastProgress) / 1e9;
      const hardExpired = elapsed >= hardTimeoutSeconds;
      const progressExpired = noProgressSeconds !== null && silent >= noProgressSeconds;
      if (!hardExpired && !progressExpired) return;
      requestTermination(hardExpired ? "HARD_TIMEOUT" : "NO_PROGRESS");
    }, pollMilliseconds);

    child.once("error", (error) => {
      clearInterval(monitor);
      settled = true;
      stdoutLog?.close();
      stderrLog?.close();
      if (invocation.temporaryRoot) fs.rmSync(invocation.temporaryRoot, { recursive: true, force: true });
      reject(error);
    });
    child.once("exit", async (code, signal) => {
      clearInterval(monitor);
      await killPromise;
      if (invocation.childWritesLogs) {
        if (!fs.existsSync(stdoutPath) || fs.statSync(stdoutPath).size <= rawLogLimitBytes) pollNewLines(stdoutPath, lineStates.stdout, onLine);
        if (!fs.existsSync(stderrPath) || fs.statSync(stderrPath).size <= rawLogLimitBytes) pollNewLines(stderrPath, lineStates.stderr, onLine);
      }
      stdoutLog?.close();
      stderrLog?.close();
      const elapsedSeconds = Number(process.hrtime.bigint() - started) / 1e9;
      let windowsStatus = null;
      if (invocation.statusPath && fs.existsSync(invocation.statusPath)) {
        try { windowsStatus = readJson(invocation.statusPath); } catch {}
      }
      if (invocation.temporaryRoot) fs.rmSync(invocation.temporaryRoot, { recursive: true, force: true });
      const windowsLimitExceeded = windowsStatus?.raw_log_limit_exceeded === true;
      const stdoutTruncated = invocation.childWritesLogs
        ? windowsLimitExceeded || (fs.existsSync(stdoutPath) && fs.statSync(stdoutPath).size > rawLogLimitBytes)
        : stdoutLog?.truncated ?? false;
      const stderrTruncated = invocation.childWritesLogs
        ? windowsLimitExceeded || (fs.existsSync(stderrPath) && fs.statSync(stderrPath).size > rawLogLimitBytes)
        : stderrLog?.truncated ?? false;
      if (!termination && (stdoutTruncated || stderrTruncated)) {
        termination = {
          trigger: "RAW_LOG_LIMIT",
          elapsed_seconds: elapsedSeconds,
          silence_seconds: Number(process.hrtime.bigint() - lastProgress) / 1e9,
          last_progress_type: lastProgressType,
          kill: { requested: false, forced: false },
        };
      }
      const result = {
        status: termination?.trigger === "RAW_LOG_LIMIT" ? "ERROR" : termination ? "INCONCLUSIVE" : code === 0 ? "PASS" : "FAIL",
        exit_code: code,
        signal,
        elapsed_seconds: Math.round(elapsedSeconds * 1000) / 1000,
        termination,
        usage,
        stdout_path: path.relative(attemptRoot, stdoutPath).split(path.sep).join("/"),
        stderr_path: path.relative(attemptRoot, stderrPath).split(path.sep).join("/"),
        stdout_truncated: stdoutTruncated,
        stderr_truncated: stderrTruncated,
        windows_job: windowsStatus ? {
          assigned: windowsStatus.job_assigned,
          process_id: windowsStatus.process_id,
          controller_termination: windowsStatus.controller_termination === true,
        } : null,
      };
      eventWriter?.write(result.status === "PASS" ? "stage.completed" : "stage.failed", {
        stageId: stage.id,
        data: { status: result.status, exit_code: code, trigger: termination?.trigger ?? null },
      });
      settled = true;
      resolve(result);
    });
  });
}
