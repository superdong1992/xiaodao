import fs from "node:fs";
import path from "node:path";
import {
  RELEASE_CLAUDE_VERSION_OUTPUT,
  RELEASE_DOCKER_ARCH,
  RELEASE_DOCKER_CONTEXT,
  RELEASE_DOCKER_OS,
} from "../lib/release-inputs.mjs";
import { runSync } from "../lib/util.mjs";

function argument(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1];
}

function blocked(code, message) {
  process.stderr.write(`${code}: ${message}\n`);
  process.exit(2);
}

function failed(code, message) {
  process.stderr.write(`${code}: ${message}\n`);
  process.exit(3);
}

function docker(context, args) {
  return runSync("docker", context && context !== "default" ? ["--context", context, ...args] : args);
}

const outputRoot = path.resolve(argument("--output-root") ?? ".");
const context = argument("--docker-context");
const repoRoot = path.resolve(argument("--repo-root") ?? ".");
const logparseSource = path.resolve(argument("--logparse-source") ?? ".");
const runtimeProfileDigest = argument("--runtime-profile-digest");
const image = argument("--image");
const containerName = argument("--container-name");
const resourceLabel = argument("--resource-label");
const model = argument("--model");
const serviceMaxTurns = argument("--service-agent-max-turns");
const serviceMaxTotalTokens = argument("--service-agent-max-total-tokens");
const serviceMaxBudgetUsd = argument("--service-agent-max-budget-usd");
const serviceHardTimeoutSeconds = argument("--service-agent-hard-timeout-seconds");
fs.mkdirSync(outputRoot, { recursive: true, mode: 0o700 });

if (process.platform === "darwin" && context !== RELEASE_DOCKER_CONTEXT) blocked("SERVER_CAPABILITY_CONTEXT", "the macOS Release server is bound to Docker context colima");
if (!image || !runtimeProfileDigest || !model || !serviceMaxTurns || !serviceMaxTotalTokens || !serviceMaxBudgetUsd || !serviceHardTimeoutSeconds || !fs.existsSync(path.join(repoRoot, "pyproject.toml")) || !fs.existsSync(path.join(logparseSource, "config.yaml")) || !containerName || !/^problem-locator\.test-flow\.run=run-[A-Za-z0-9-]+$/.test(resourceLabel ?? "")) {
  failed("SERVER_CAPABILITY_ARGUMENTS", "image, registered container name, and exact run label are required");
}

const server = docker(context, ["version", "--format", "{{json .Server}}"]);
if (server.status !== 0) blocked("SERVER_CAPABILITY_DOCKER", "Docker server is unavailable");
let metadata;
try { metadata = JSON.parse(server.stdout); } catch { failed("SERVER_CAPABILITY_METADATA", "Docker returned invalid server metadata"); }
const serverOs = String(metadata.Os ?? metadata.OsType ?? "").toLowerCase();
const serverArch = String(metadata.Arch ?? metadata.Architecture ?? "").toLowerCase();
if (serverOs !== RELEASE_DOCKER_OS) blocked("SERVER_CAPABILITY_OS", "the only supported server platform is Linux");
if (!["amd64", "x86_64"].includes(serverArch)) blocked("SERVER_CAPABILITY_ARCH", "the Release server must be x86_64/amd64");

const imageInspect = docker(context, ["image", "inspect", image]);
if (imageInspect.status !== 0) blocked("SERVER_CAPABILITY_IMAGE", "the explicitly prepared offline Release image is absent");
let imageMetadata;
try { imageMetadata = JSON.parse(imageInspect.stdout)[0]; } catch { failed("SERVER_CAPABILITY_IMAGE_METADATA", "invalid image metadata"); }
if (imageMetadata.Os !== RELEASE_DOCKER_OS || imageMetadata.Architecture !== RELEASE_DOCKER_ARCH) {
  blocked("SERVER_CAPABILITY_IMAGE_PLATFORM", "the cached Release image is not linux/amd64");
}

const run = docker(context, [
  "run",
  "--name", containerName,
  "--label", resourceLabel,
  "--pull", "never",
  "--network", "none",
  "--platform", "linux/amd64",
  "--mount", `type=bind,src=${repoRoot},dst=/opt/src/xiaodao,readonly`,
  "--mount", `type=bind,src=${logparseSource},dst=/opt/src/logparse,readonly`,
  "--mount", `type=bind,src=${outputRoot},dst=/evidence`,
  "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=2g",
  "--env", "PYTHONNOUSERSITE=1",
  "--env", "PYTHONPYCACHEPREFIX=/tmp/pycache",
  "--env", "S08_NATIVE_STARTUP_GATE=linux",
  "--env", "S08_INSTALLED_DISTRIBUTION_GATE=1",
  "--env", "S08_UV=/usr/local/bin/uv",
  "--env", "S08_PYTHON_312=/opt/venvs/xiaodao/bin/python",
  "--env", "S08_UV_OFFLINE=1",
  "--env", "S08_UV_CACHE_DIR=/tmp/uv-cache",
  "--env", "SKILL_DIR=/opt/src/xiaodao/tests/fixtures/components/runtime-catalog/skill-dir",
  "--env", "LOGPARSE_REPO=/opt/src/logparse",
  "--env", "LOGPARSE_CONFIG_PATH=/opt/src/logparse/config.yaml",
  "--env", "LOGPARSE_PYTHON=/opt/venvs/logparse/bin/python",
  "--env", `CLAUDE_COMMAND=/usr/bin/timeout --foreground --signal=TERM --kill-after=5s ${serviceHardTimeoutSeconds}s /usr/local/bin/claude -p --no-session-persistence --dangerously-skip-permissions --model ${model} --max-turns ${serviceMaxTurns} --max-budget-usd ${serviceMaxBudgetUsd} --tools Bash,Read,Write,Skill`,
  image,
  "sh", "-eu", "-c", "claude --version; node -p process.arch; cd /opt/src/xiaodao; /opt/venvs/xiaodao/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/pytest --junitxml=/evidence/platform-server.xml tests/platform/server_linux/test_native_startup_gate.py::test_native_linux_startup_gate tests/platform/distribution/test_installed_distribution_gate.py::test_clean_installed_distribution_import_cli_and_server_gate",
]);
process.stdout.write(run.stdout);
process.stderr.write(run.stderr);
if (run.status !== 0) blocked("SERVER_CAPABILITY_RUNTIME", "the offline Linux CLI probe failed");
const lines = run.stdout.split(/\r?\n/).filter(Boolean);
if (lines[0] !== RELEASE_CLAUDE_VERSION_OUTPUT || lines[1] !== "x64") {
  blocked("SERVER_CAPABILITY_CLAUDE", "Linux Agent CLI must be official npm 2.1.89 on x64 Node");
}
if (!fs.existsSync(path.join(outputRoot, "platform-server.xml"))) failed("SERVER_CAPABILITY_PLATFORM_EVIDENCE", "Linux platform JUnit evidence is missing");
const created = docker(context, ["container", "inspect", containerName]);
if (created.status !== 0) failed("SERVER_CAPABILITY_CONTAINER_RECEIPT", "registered probe container is not inspectable");
let createdMetadata;
try { createdMetadata = JSON.parse(created.stdout)[0]; } catch { failed("SERVER_CAPABILITY_CONTAINER_METADATA", "invalid probe container metadata"); }
const [labelName, labelValue] = resourceLabel.split("=", 2);
if (createdMetadata.Config?.Labels?.[labelName] !== labelValue || createdMetadata.State?.Running !== false) {
  failed("SERVER_CAPABILITY_CONTAINER_IDENTITY", "probe container label or stopped state differs from the registry");
}

fs.writeFileSync(path.join(outputRoot, "server-linux-capability-result.json"), `${JSON.stringify({
  schema_version: 2,
  runtime_profile_digest: runtimeProfileDigest,
  status: "PASS",
  claims: {
    "linux-runtime": "PASS",
    "installed-distribution": "PASS",
    "native-startup": "PASS"
  },
  docker_context: context,
  os: RELEASE_DOCKER_OS,
  architecture: "x86_64",
  docker_version: metadata.Version ?? null,
  image,
  image_id: imageMetadata.Id,
  claude_version: RELEASE_CLAUDE_VERSION_OUTPUT,
  node_architecture: "x64",
  network: "none",
  pull_policy: "never",
  model,
  service_agent_caps: {
    max_turns: Number(serviceMaxTurns),
    max_total_tokens: Number(serviceMaxTotalTokens),
    max_budget_usd: Number(serviceMaxBudgetUsd),
    hard_timeout_seconds: Number(serviceHardTimeoutSeconds),
  },
})}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
process.stdout.write("TEST_FLOW_PROGRESS request.completed\n");
