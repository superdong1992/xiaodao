import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import {
  RELEASE_POSTGRES_IMAGE,
  RELEASE_POSTGRES_IMAGE_ID,
  dockerContextArgs,
  validateReleasePostgresImage,
} from "../lib/release-inputs.mjs";
import { runSync } from "../lib/util.mjs";

const SECRET_TARGET = "/run/test-flow-postgres";

function executor(dockerContext, runCommand) {
  return (args, allowFailure = false) => {
    const result = runCommand("docker", dockerContextArgs(dockerContext ?? "default", args), { timeout: 70000 });
    // Docker output can contain environment values. Never include it in errors.
    if (!allowFailure && result.status !== 0) throw new Error("POSTGRES_SIDECAR_COMMAND_FAILED");
    return result;
  };
}

export function postgresSecretMount(sidecar) {
  return `type=bind,src=${sidecar.secret_directory},dst=${SECRET_TARGET},readonly`;
}

export function postgresSidecarReceipt(sidecar) {
  return Object.fromEntries(["schema_version", "run_id", "scope", "container", "volume", "network", "image", "image_id", "database", "user", "initial_database"].map((key) => [key, sidecar[key]]));
}

export function validPostgresSidecarReceipt(receipt, { runId, scope, databaseName, expectedImageId } = {}) {
  if (!receipt || typeof runId !== "string" || !runId || expectedImageId !== RELEASE_POSTGRES_IMAGE_ID) return false;
  const suffix = crypto.createHash("sha256").update(`${runId}:${scope}`).digest("hex").slice(0, 24);
  const expected = {
    schema_version: 1, run_id: runId, scope,
    container: `pltf-pg-${suffix}`, volume: `pltf-pgdata-${suffix}`, network: `pltf-pgnet-${suffix}`,
    image: RELEASE_POSTGRES_IMAGE, image_id: expectedImageId, database: databaseName,
    user: "pl_test_admin", initial_database: "EMPTY",
  };
  return Object.keys(receipt).length === Object.keys(expected).length
    && Object.entries(expected).every(([key, value]) => receipt[key] === value);
}

export function startPostgresSidecar({ attemptRoot, runId, scope, dockerContext,
  resourceRegistry, resourceLabel, databaseName, runCommand = runSync }) {
  if (!path.isAbsolute(attemptRoot) || path.basename(attemptRoot) !== runId
    || !/^[a-z0-9-]{1,32}$/.test(scope) || !/^[a-z][a-z0-9_]*_test(?:_[a-z0-9_]+)?$/.test(databaseName)
    || resourceLabel !== `problem-locator.test-flow.run=${runId}`
    || path.resolve(resourceRegistry) !== path.join(path.resolve(attemptRoot), "payload", "resources.ndjson")) {
    throw new Error("POSTGRES_SIDECAR_CONFIGURATION_INVALID");
  }
  validateReleasePostgresImage({ status: "PRESENT", context: dockerContext, docker_cli: "docker" }, runCommand);
  const suffix = crypto.createHash("sha256").update(`${runId}:${scope}`).digest("hex").slice(0, 24);
  const sidecar = {
    schema_version: 1, run_id: runId, scope,
    container: `pltf-pg-${suffix}`, volume: `pltf-pgdata-${suffix}`, network: `pltf-pgnet-${suffix}`,
    image: RELEASE_POSTGRES_IMAGE, image_id: RELEASE_POSTGRES_IMAGE_ID,
    database: databaseName, user: "pl_test_admin", initial_database: "EMPTY",
    secret_directory: path.join(attemptRoot, "scratch", `postgres-secrets-${scope}`),
  };
  const docker = executor(dockerContext, runCommand);
  // Do not adopt any existing resource, including a stopped container or empty volume.
  for (const kind of ["container", "volume", "network"]) {
    const result = docker([kind, "inspect", sidecar[kind]], true);
    if (result.status === 0) throw new Error("POSTGRES_SIDECAR_RESOURCE_ALREADY_EXISTS");
    if (!/no such|not found/i.test(`${result.stderr ?? ""} ${result.stdout ?? ""}`)) {
      throw new Error("POSTGRES_SIDECAR_RESOURCE_INSPECTION_FAILED");
    }
  }
  fs.mkdirSync(path.dirname(sidecar.secret_directory), { recursive: true, mode: 0o700 });
  fs.mkdirSync(sidecar.secret_directory, { recursive: false, mode: 0o700 });
  const password = crypto.randomBytes(32).toString("hex");
  fs.writeFileSync(path.join(sidecar.secret_directory, "password"), `${password}\n`, { flag: "wx", mode: 0o600 });
  fs.writeFileSync(path.join(sidecar.secret_directory, "database-url"),
    `postgresql://${sidecar.user}:${password}@problem-locator-postgres:5432/${sidecar.database}\n`, { flag: "wx", mode: 0o600 });
  const register = (kind) => fs.appendFileSync(resourceRegistry,
    `${JSON.stringify({ schema_version: 2, kind, name: sidecar[kind], label: resourceLabel })}\n`, { mode: 0o600 });
  register("network");
  docker(["network", "create", "--internal", "--label", resourceLabel, sidecar.network]);
  register("volume");
  docker(["volume", "create", "--label", resourceLabel, sidecar.volume]);
  register("container");
  docker(["run", "--detach", "--name", sidecar.container, "--label", resourceLabel,
    "--pull", "never", "--platform", "linux/amd64", "--network", sidecar.network,
    "--network-alias", "problem-locator-postgres",
    "--env", `POSTGRES_USER=${sidecar.user}`, "--env", `POSTGRES_DB=${sidecar.database}`,
    "--env", `POSTGRES_PASSWORD_FILE=${SECRET_TARGET}/password`,
    "--env", "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
    "--mount", postgresSecretMount(sidecar),
    "--mount", `type=volume,src=${sidecar.volume},dst=/var/lib/postgresql/data`,
    sidecar.image_id]);
  // -h forces the final TCP server; the initdb temporary server is socket-only.
  docker(["exec", sidecar.container, "sh", "-eu", "-c",
    'i=0; until pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do i=$((i+1)); test "$i" -lt 60; sleep 1; done']);
  const empty = docker(["exec", sidecar.container, "psql", "-U", sidecar.user, "-d", sidecar.database,
    "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c",
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f');"]);
  if (empty.stdout.trim() !== "0") throw new Error("POSTGRES_SIDECAR_DATABASE_NOT_EMPTY");
  assertPostgresSidecar(sidecar, { dockerContext, runCommand });
  return sidecar;
}

export function assertPostgresSidecar(sidecar, { dockerContext, runCommand = runSync } = {}) {
  if (!sidecar || !validPostgresSidecarReceipt(postgresSidecarReceipt(sidecar), {
    runId: sidecar.run_id, scope: sidecar.scope, databaseName: sidecar.database, expectedImageId: RELEASE_POSTGRES_IMAGE_ID,
  })) {
    throw new Error("POSTGRES_SIDECAR_IDENTITY_DRIFT");
  }
  const docker = executor(dockerContext, runCommand);
  const container = JSON.parse(docker(["container", "inspect", sidecar.container]).stdout)[0];
  const volume = JSON.parse(docker(["volume", "inspect", sidecar.volume]).stdout)[0];
  const network = JSON.parse(docker(["network", "inspect", sidecar.network]).stdout)[0];
  const label = "problem-locator.test-flow.run";
  if (container.Image !== sidecar.image_id || container.State?.Running !== true
    || container.Name !== `/${sidecar.container}` || container.Config?.Image !== sidecar.image_id
    || !container.Config?.Env?.includes(`POSTGRES_DB=${sidecar.database}`)
    || !container.Config?.Env?.includes(`POSTGRES_USER=${sidecar.user}`)
    || container.Config?.Labels?.[label] !== sidecar.run_id || volume.Labels?.[label] !== sidecar.run_id
    || network.Labels?.[label] !== sidecar.run_id || network.Internal !== true
    || !container.NetworkSettings?.Networks?.[sidecar.network]
    || Object.keys(container.HostConfig?.PortBindings ?? {}).length !== 0
    || !container.Mounts?.some((mount) => mount.Type === "volume" && mount.Name === sidecar.volume && mount.Destination === "/var/lib/postgresql/data")) {
    throw new Error("POSTGRES_SIDECAR_IDENTITY_DRIFT");
  }
  if (!fs.statSync(path.join(sidecar.secret_directory, "database-url")).isFile()) throw new Error("POSTGRES_SIDECAR_SECRET_MISSING");
  return postgresSidecarReceipt(sidecar);
}

export function attachPostgresServer(sidecar, container, { dockerContext, runCommand = runSync } = {}) {
  assertPostgresSidecar(sidecar, { dockerContext, runCommand });
  executor(dockerContext, runCommand)(["network", "connect", sidecar.network, container]);
}
