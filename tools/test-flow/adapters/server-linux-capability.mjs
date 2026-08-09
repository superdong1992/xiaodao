import fs from "node:fs";
import path from "node:path";
import { commandExists, runExecutableSync, runSync } from "../lib/util.mjs";

function argument(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1];
}

const outputRoot = path.resolve(argument("--output-root") ?? ".");
fs.mkdirSync(outputRoot, { recursive: true, mode: 0o700 });

if (!commandExists("docker")) {
  process.stderr.write("SERVER_CAPABILITY_BLOCKED: Docker is unavailable\n");
  process.exit(2);
}
const server = runSync("docker", ["version", "--format", "{{json .Server}}"]);
if (server.status !== 0) {
  process.stderr.write(`SERVER_CAPABILITY_BLOCKED: Docker server is unavailable: ${server.stderr}\n`);
  process.exit(2);
}
let metadata;
try { metadata = JSON.parse(server.stdout); } catch {
  process.stderr.write("SERVER_CAPABILITY_ERROR: Docker returned invalid server metadata\n");
  process.exit(3);
}
if (String(metadata.Os ?? metadata.OsType ?? "").toLowerCase() !== "linux") {
  process.stderr.write("SERVER_CAPABILITY_BLOCKED: the only supported server platform is Linux\n");
  process.exit(2);
}

// The cheap capability command is deliberately a fixed adapter path, never a
// shell string. A deployment can provide the executable through the manifest
// after freezing it into the server identity.
const probe = process.env.TEST_FLOW_SERVER_MODEL_PROBE;
if (!probe) {
  process.stderr.write("SERVER_CAPABILITY_BLOCKED: TEST_FLOW_SERVER_MODEL_PROBE is required for the fresh Linux model probe\n");
  process.exit(2);
}
const absoluteProbe = path.resolve(probe);
if (!fs.existsSync(absoluteProbe) || !fs.statSync(absoluteProbe).isFile()) {
  process.stderr.write("SERVER_CAPABILITY_BLOCKED: configured Linux model probe is not a file\n");
  process.exit(2);
}
const result = runExecutableSync(absoluteProbe, ["--output-root", outputRoot]);
process.stdout.write(result.stdout);
process.stderr.write(result.stderr);
if (result.status !== 0) process.exit(result.status ?? 3);
fs.writeFileSync(path.join(outputRoot, "server-linux-capability-result.json"), `${JSON.stringify({ schema_version: 1, status: "PASS", os: "linux", docker_version: metadata.Version ?? null, model_probe: "PASS" })}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
process.stdout.write("TEST_FLOW_PROGRESS request.completed\n");
