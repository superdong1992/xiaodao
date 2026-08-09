import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

export class FlowError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "FlowError";
    this.code = code;
    this.details = details;
  }
}

export function assertFlow(condition, code, message, details = {}) {
  if (!condition) throw new FlowError(code, message, details);
}

export function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalize(value[key])]),
    );
  }
  return value;
}

export function canonicalJson(value) {
  return `${JSON.stringify(canonicalize(value))}\n`;
}

export function sha256Bytes(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

export function sha256File(filePath) {
  const hash = crypto.createHash("sha256");
  const descriptor = fs.openSync(filePath, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const count = fs.readSync(descriptor, buffer, 0, buffer.length, null);
      if (count === 0) break;
      hash.update(buffer.subarray(0, count));
    }
  } finally {
    fs.closeSync(descriptor);
  }
  return hash.digest("hex");
}

export function ensureDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
}

export function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

export function writeJsonSync(filePath, value) {
  ensureDirectory(path.dirname(filePath));
  fs.writeFileSync(filePath, canonicalJson(value), { encoding: "utf8", flag: "wx", mode: 0o600 });
}

export function atomicCreateJson(filePath, value) {
  ensureDirectory(path.dirname(filePath));
  assertFlow(!fs.existsSync(filePath), "EVIDENCE_ALREADY_EXISTS", `Refusing to replace ${filePath}`);
  const temporary = path.join(
    path.dirname(filePath),
    `.${path.basename(filePath)}.${process.pid}.${crypto.randomUUID()}.tmp`,
  );
  const descriptor = fs.openSync(temporary, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, canonicalJson(value), "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  try {
    // A hard-link publish is atomic and, unlike rename, cannot replace a verdict
    // that another finalizer already committed.
    fs.linkSync(temporary, filePath);
    let directoryDescriptor = null;
    try {
      directoryDescriptor = fs.openSync(path.dirname(filePath), "r");
      fs.fsyncSync(directoryDescriptor);
    } catch (error) {
      if (process.platform !== "win32") throw error;
    } finally {
      if (directoryDescriptor !== null) fs.closeSync(directoryDescriptor);
    }
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

export function runSync(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd,
    env: options.env ?? process.env,
    encoding: "utf8",
    windowsHide: true,
    maxBuffer: options.maxBuffer ?? 16 * 1024 * 1024,
  });
  return {
    command,
    args,
    status: result.status,
    signal: result.signal,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    error: result.error,
  };
}

export function runExecutableSync(executable, args, options = {}) {
  if (process.platform === "win32" && /\.(?:cmd|bat)$/i.test(executable)) {
    return runSync(process.env.ComSpec || "cmd.exe", ["/d", "/s", "/c", executable, ...args], options);
  }
  if (process.platform === "win32" && /\.ps1$/i.test(executable)) {
    return runSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", executable, ...args], options);
  }
  return runSync(executable, args, options);
}

export function commandExists(command) {
  const probe = process.platform === "win32"
    ? runSync("where.exe", [command])
    : runSync("sh", ["-c", "command -v -- \"$1\"", "test-flow", command]);
  return probe.status === 0 && probe.stdout.trim().length > 0;
}

export function resolveCommand(command) {
  const probe = process.platform === "win32"
    ? runSync("where.exe", [command])
    : runSync("sh", ["-c", "command -v -- \"$1\"", "test-flow", command]);
  return probe.status === 0 ? probe.stdout.trim().split(/\r?\n/, 1)[0] : null;
}

function pythonProbe(command, prefix, repoRoot, environment) {
  const source = [
    "import importlib.metadata, json, os, platform, sys",
    "names = ('anyio', 'fastapi', 'jsonschema', 'pydantic', 'pytest', 'starlette')",
    "packages = {}",
    "for name in names:",
    "    try:",
    "        packages[name] = importlib.metadata.version(name)",
    "    except importlib.metadata.PackageNotFoundError:",
    "        packages[name] = None",
    "print(json.dumps({'executable': os.path.realpath(sys.executable), 'python_version': platform.python_version(), 'version_info': list(sys.version_info[:3]), 'packages': packages, 'sys_path': [os.path.realpath(item or os.getcwd()) for item in sys.path]}, sort_keys=True))",
  ].join("\n");
  const probe = runSync(command, [...prefix, "-c", source], {
    cwd: repoRoot,
    env: environment,
  });
  if (probe.status !== 0) return null;
  let details;
  try {
    details = JSON.parse(probe.stdout.trim().split(/\r?\n/).at(-1));
  } catch {
    return null;
  }
  if (
    !Array.isArray(details.version_info)
    || details.version_info[0] !== 3
    || details.version_info[1] !== 12
    || typeof details.packages?.pytest !== "string"
  ) return null;
  const launcher = fs.existsSync(command) ? fs.realpathSync(command) : command;
  const pythonExecutable = details.executable;
  return {
    command,
    details,
    identity: {
      status: "PRESENT",
      launcher,
      launcher_sha256: fs.existsSync(launcher) && fs.statSync(launcher).isFile()
        ? sha256File(launcher)
        : null,
      python_executable: pythonExecutable,
      python_sha256: fs.existsSync(pythonExecutable) && fs.statSync(pythonExecutable).isFile()
        ? sha256File(pythonExecutable)
        : null,
      python_version: details.python_version,
      packages: details.packages,
    },
  };
}

export function resolvePythonTestRuntime(repoRoot, environment = process.env) {
  const configuredPython = environment.TEST_FLOW_PYTHON;
  if (configuredPython) {
    const executable = path.isAbsolute(configuredPython)
      ? configuredPython
      : resolveCommand(configuredPython);
    if (!executable || !fs.existsSync(executable)) return null;
    const resolved = pythonProbe(executable, [], repoRoot, environment);
    return resolved === null
      ? null
      : { ...resolved, interpreterPrefix: [], prefix: ["-m", "pytest"], kind: "python" };
  }

  const configuredUv = environment.UV;
  const uv = configuredUv
    ? (path.isAbsolute(configuredUv) ? configuredUv : resolveCommand(configuredUv))
    : resolveCommand("uv");
  if (uv && fs.existsSync(uv)) {
    const prefix = ["run", "--offline", "--frozen", "python"];
    const resolved = pythonProbe(uv, prefix, repoRoot, environment);
    if (resolved !== null) {
      return {
        ...resolved,
        interpreterPrefix: prefix,
        prefix: [...prefix, "-m", "pytest"],
        kind: "uv",
      };
    }
    if (configuredUv) return null;
  }

  for (const candidate of ["python3.12", "python.exe", "python3", "python"]) {
    const executable = resolveCommand(candidate);
    if (!executable) continue;
    const resolved = pythonProbe(executable, [], repoRoot, environment);
    if (resolved !== null) {
      return { ...resolved, interpreterPrefix: [], prefix: ["-m", "pytest"], kind: "python" };
    }
  }
  return null;
}

export function removeTreeWritable(targetPath, allowedRoot) {
  const target = path.resolve(targetPath);
  const boundary = path.resolve(allowedRoot);
  assertFlow(
    target !== boundary && target.startsWith(`${boundary}${path.sep}`),
    "CLEANUP_PATH_OUTSIDE_ATTEMPT",
    `Refusing to remove ${target}`,
  );
  if (!fs.existsSync(target)) return;
  const rootMetadata = fs.lstatSync(target);
  assertFlow(
    rootMetadata.isDirectory() && !rootMetadata.isSymbolicLink(),
    "CLEANUP_ROOT_INVALID",
    `Cleanup root is not a plain directory: ${target}`,
  );

  function prepare(current) {
    const metadata = fs.lstatSync(current);
    if (metadata.isSymbolicLink()) return;
    if (metadata.isDirectory()) {
      fs.chmodSync(current, 0o700);
      for (const name of fs.readdirSync(current)) prepare(path.join(current, name));
      return;
    }
    if (process.platform === "win32") fs.chmodSync(current, 0o600);
  }

  prepare(target);
  fs.rmSync(target, { recursive: true, force: true });
}

export function normalizeRepoPath(repoRoot, targetPath) {
  const absolute = path.resolve(repoRoot, targetPath);
  const relative = path.relative(repoRoot, absolute).split(path.sep).join("/");
  assertFlow(
    relative !== ".." && !relative.startsWith("../") && !path.isAbsolute(relative),
    "PATH_OUTSIDE_REPOSITORY",
    `${targetPath} is outside the repository`,
  );
  return { absolute, relative: relative || "." };
}

export function timestampForPath(date = new Date()) {
  return date.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

export function redactError(error) {
  if (error instanceof FlowError) {
    return { code: error.code, message: error.message, details: error.details };
  }
  return { code: "UNEXPECTED", message: String(error?.message ?? error) };
}
