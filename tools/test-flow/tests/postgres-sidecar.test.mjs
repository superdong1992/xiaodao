import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  startPostgresSidecar, assertPostgresSidecar, attachPostgresServer,
  postgresSidecarReceipt, validPostgresSidecarReceipt,
} from "../runtime-support/postgres-sidecar.mjs";
import { RELEASE_POSTGRES_IMAGE, RELEASE_POSTGRES_IMAGE_ID, validateReleasePostgresImage } from "../lib/release-inputs.mjs";

function dockerFixture(t, { existing = false, tableCount = "0", unavailable = false } = {}) {
  const attemptRoot = fs.mkdtempSync(path.join(os.tmpdir(), "pltf-pg-contract-"));
  fs.mkdirSync(path.join(attemptRoot, "payload"));
  t.after(() => fs.rmSync(attemptRoot, { recursive: true, force: true }));
  const runId = path.basename(attemptRoot);
  const calls = [];
  const resources = new Map();
  const runCommand = (command, rawArgs) => {
    assert.equal(command, "docker");
    const args = rawArgs[0] === "--context" ? rawArgs.slice(2) : rawArgs;
    calls.push(args);
    const ok = (value = "") => ({ status: 0, stdout: typeof value === "string" ? value : JSON.stringify([value]), stderr: "" });
    if (args[0] === "image") return ok({ Id: RELEASE_POSTGRES_IMAGE_ID, Os: "linux", Architecture: "amd64" });
    if (args[1] === "inspect") {
      if (unavailable) return { status: 1, stdout: "", stderr: "Cannot connect to the Docker daemon" };
      if (resources.has(args[2])) return ok(resources.get(args[2]));
      return existing ? ok({}) : { status: 1, stdout: "", stderr: "No such resource" };
    }
    if (args[1] === "create") {
      resources.set(args.at(-1), { Name: args.at(-1), Internal: args[0] === "network", Labels: { "problem-locator.test-flow.run": runId } });
      return ok();
    }
    if (args[0] === "run") {
      const option = (key) => args[args.indexOf(key) + 1];
      const env = args.flatMap((value, index) => value === "--env" ? [args[index + 1]] : []);
      const volume = args.find((value) => value.startsWith("type=volume,"));
      resources.set(option("--name"), {
        Name: `/${option("--name")}`, Image: args.at(-1), State: { Running: true },
        Config: { Image: args.at(-1), Env: env, Labels: { "problem-locator.test-flow.run": runId } },
        HostConfig: { PortBindings: {} }, NetworkSettings: { Networks: { [option("--network")]: {} } },
        Mounts: [{ Type: "volume", Name: volume.split(",")[1].slice(4), Destination: "/var/lib/postgresql/data" }],
      });
      return ok();
    }
    if (args[0] === "exec") return ok(args[2] === "psql" ? `${tableCount}\n` : "");
    if (args[0] === "network" && args[1] === "connect") return ok();
    throw new Error(`unexpected mock command: ${args[0]}`);
  };
  return {
    args: { attemptRoot, runId, scope: "crossjob", dockerContext: "default", resourceLabel: `problem-locator.test-flow.run=${runId}`,
      resourceRegistry: path.join(attemptRoot, "payload", "resources.ndjson"), databaseName: "problem_locator_release_test", runCommand },
    calls, resources, runCommand,
  };
}

test("PostgreSQL planning checks the exact offline digest without pulling or starting a container", () => {
  const calls = [];
  const identity = { status: "PRESENT", context: "default", docker_cli: "docker" };
  const runner = (_command, args) => {
    calls.push(args);
    return { status: 0, stdout: JSON.stringify([{ Id: RELEASE_POSTGRES_IMAGE_ID, Os: "linux", Architecture: "amd64" }]) };
  };
  assert.equal(validateReleasePostgresImage(identity, runner).image_id, RELEASE_POSTGRES_IMAGE_ID);
  assert.deepEqual(calls, [["image", "inspect", RELEASE_POSTGRES_IMAGE]]);
  for (const metadata of [{ Id: "mutable", Os: "linux", Architecture: "amd64" }, { Id: RELEASE_POSTGRES_IMAGE_ID, Os: "linux", Architecture: "arm64" }]) {
    assert.throws(() => validateReleasePostgresImage(identity, () => ({ status: 0, stdout: JSON.stringify([metadata]) })), /IDENTITY_MISMATCH/);
  }
  assert.throws(() => validateReleasePostgresImage(identity, () => ({ status: 1 })), /IMAGE_MISSING/);
});

test("fresh PostgreSQL sidecar keeps secrets private and attaches both server instances to the same database", (t) => {
  const fixture = dockerFixture(t);
  const sidecar = startPostgresSidecar(fixture.args);
  const receipt = postgresSidecarReceipt(sidecar);
  assert.equal(validPostgresSidecarReceipt(receipt, { runId: sidecar.run_id, scope: "crossjob", databaseName: sidecar.database, expectedImageId: RELEASE_POSTGRES_IMAGE_ID }), true);
  assert.equal(Object.hasOwn(receipt, "secret_directory"), false);
  const password = fs.readFileSync(path.join(sidecar.secret_directory, "password"), "utf8").trim();
  assert.equal(JSON.stringify(fixture.calls).includes(password), false);
  assert.equal(JSON.stringify(receipt).includes(password), false);
  const create = fixture.calls.find((call) => call[0] === "run");
  assert.ok(create.includes("never"));
  assert.ok(!create.includes("--publish"));
  assert.ok(create.includes("POSTGRES_PASSWORD_FILE=/run/test-flow-postgres/password"));
  assert.ok(fixture.calls.some((call) => call[0] === "network" && call.includes("--internal")));
  attachPostgresServer(sidecar, "server-initial", fixture.args);
  attachPostgresServer(sidecar, "server-restart", fixture.args);
  assert.deepEqual(fixture.calls.filter((call) => call[1] === "connect").map((call) => call.slice(2)), [[sidecar.network, "server-initial"], [sidecar.network, "server-restart"]]);
  assert.equal(fixture.calls.filter((call) => call[0] === "run").length, 1);
  assert.deepEqual(assertPostgresSidecar(sidecar, fixture.args), receipt);
  fixture.resources.get(sidecar.container).Image = "other-image";
  assert.throws(() => assertPostgresSidecar(sidecar, fixture.args), /IDENTITY_DRIFT/);
});

test("PostgreSQL admission refuses existing resources, uncertain inspections and nonempty databases", (t) => {
  for (const [options, error] of [[{ existing: true }, /RESOURCE_ALREADY_EXISTS/], [{ unavailable: true }, /RESOURCE_INSPECTION_FAILED/], [{ tableCount: "1" }, /DATABASE_NOT_EMPTY/]]) {
    const fixture = dockerFixture(t, options);
    assert.throws(() => startPostgresSidecar(fixture.args), error);
    if (!options.tableCount) assert.equal(fixture.calls.some((call) => ["run", "create"].includes(call[0]) || call[1] === "create"), false);
  }
});
