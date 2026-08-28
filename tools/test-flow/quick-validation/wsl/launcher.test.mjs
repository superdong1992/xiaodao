import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { loadConfiguration, resolveGoalClosure } from "../../lib/config.mjs";
import {
  CLAUDE_DEEPSEEK_METHODS_CALLS,
  CLAUDE_DEEPSEEK_MODEL_CERT_MAX_CALLS,
  CLAUDE_DEEPSEEK_MODEL_CERT_NORMAL_CALLS,
} from "../claude-deepseek/runtime/claude-deepseek-contract.mjs";
import {
  MACOS_CODEX_LUNA_E2E_CALLS,
  MACOS_CODEX_LUNA_E2E_MAX_CALLS,
} from "../codex-luna/runtime/macos-codex-luna-e2e-contract.mjs";

const WSL_ROOT = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(WSL_ROOT, "..", "..", "..", "..");

test("WSL launcher delegates the fixed Evidence V2 certification to the central Test Flow", () => {
  const source = fs.readFileSync(path.join(WSL_ROOT, "run.sh"), "utf8");
  assert.match(source, /tools\/test-flow\/run\.sh/u);
  assert.match(source, /--track release/u);
  assert.match(source, /--goal release\.evidence-v2-certification/u);
  assert.match(source, /--client linux/u);
  assert.match(source, /--scenario multiple-rpc-timeouts/u);
  assert.match(source, /--resume fresh/u);
  assert.match(source, /--allow-codex-posthoc-budget/u);
  assert.doesNotMatch(source, /--all-scenarios|NINE_ISOLATED_CONTAINERS|container-suite\.mjs/u);
  assert.doesNotMatch(source, /quick-validation\/claude-deepseek\/run\.sh|quick-validation\/codex-luna\/run\.sh/u);
});

test("WSL launcher keeps dependency cache read-only and formal evidence writable", () => {
  const source = fs.readFileSync(path.join(WSL_ROOT, "run.sh"), "utf8");
  assert.match(source, /src=\$cache_root,dst=\/cache,readonly/u);
  assert.match(source, /src=\$evidence_root,dst=\/evidence"/u);
  assert.doesNotMatch(source, /src=\$evidence_root,dst=\/evidence,readonly/u);
  assert.match(source, /TEST_FLOW_QUICK_UBUNTU2204_CONTAINER=1/u);
  assert.match(source, /TEST_FLOW_PYTHON=\/opt\/venvs\/xiaodao\/bin\/python/u);
  assert.match(source, /TEST_FLOW_QUICK_PYTHON=\/opt\/venvs\/xiaodao\/bin\/python/u);
});

test("WSL launcher reads the frozen runtime as root and transfers only the exact attempt to the invoking uid", () => {
  const source = fs.readFileSync(path.join(WSL_ROOT, "run.sh"), "utf8");
  assert.match(source, /host_uid=\$\(id -u\)/u);
  assert.match(source, /host_gid=\$\(id -g\)/u);
  assert.match(source, /--user 0:0/u);
  assert.match(source, /--env HOME=\/root/u);
  assert.match(source, /TEST_FLOW_HOST_UID=\$host_uid/u);
  assert.match(source, /TEST_FLOW_HOST_GID=\$host_gid/u);
  assert.match(source, /chown -R -- "\$TEST_FLOW_HOST_UID:\$TEST_FLOW_HOST_GID" "\$attempt_root"/u);
  assert.doesNotMatch(source, /chown[^\n]*(?:\/cache|\$cache_root|\$repo_root|[" ]\/evidence[" ])/u);
  assert.match(source, /\/usr\/bin\/codex --version/u);
  assert.match(source, /\/opt\/venvs\/xiaodao\/bin\/python --version/u);
  assert.match(source, /node \/opt\/claude-cache\/package\/cli\.js --version/u);
  assert.match(source, /test -r \/opt\/claude-cache\/cache-seal\.json/u);
  assert.match(source, /test -r \/run\/secrets\/codex-auth\.json/u);
  assert.match(source, /test -r \/run\/secrets\/claude-settings\.json/u);
  assert.match(source, /test -w \/evidence/u);
});

test("WSL launcher preserves the Test Flow exit and requires its exact verdict to remain caller-readable", () => {
  const source = fs.readFileSync(path.join(WSL_ROOT, "run.sh"), "utf8");
  assert.match(source, /container_status=\$\{PIPESTATUS\[0\]\}/u);
  assert.match(source, /attempt_root.*value\.attempt_root/u);
  assert.match(source, /attempt_name=\$\{attempt_root#\/evidence\/\}/u);
  assert.match(source, /case "\$attempt_root" in \/evidence\/run-\*/u);
  assert.match(source, /host_attempt="\$evidence_root\/\$attempt_name"/u);
  assert.match(source, /test -r "\$host_attempt\/verdict\.json"/u);
  assert.match(source, /stat -c %u:%g "\$host_attempt\/verdict\.json"/u);
  assert.match(source, /exit "\$container_status"/u);
});

test("WSL host extracts one exact attempt_root with awk and does not require host Node", () => {
  const source = fs.readFileSync(path.join(WSL_ROOT, "run.sh"), "utf8");
  const hostSource = source.slice(source.indexOf('cache_root=""'));
  assert.match(hostSource, /attempt_root=\$\(awk/u);
  assert.match(hostSource, /if \(count == 0\) exit 4/u);
  assert.match(hostSource, /if \(count != 1 \|\| invalid == 1\) exit 5/u);
  assert.match(hostSource, /ATTEMPT_ROOT_OUTPUT_INVALID/u);
  assert.doesNotMatch(hostSource, /attempt_root=\$\(node\b/u);
  assert.doesNotMatch(hostSource, /\bnode -e\b/u);
});

test("central certification closure owns Core, one generation, P1, P2 and release verdict", () => {
  const config = loadConfiguration(REPO_ROOT);
  const closure = resolveGoalClosure(config, {
    goalId: "release.evidence-v2-certification",
    track: "release",
    client: "linux",
  });
  const ids = closure.stages.map((stage) => stage.id);
  assert.equal(ids.includes("deterministic.full"), true);
  assert.equal(ids.filter((id) => id === "real.skill-generation").length, 1);
  assert.equal(ids.filter((id) => id === "real.macos-claude-deepseek-e2e").length, 1);
  assert.equal(ids.filter((id) => id === "real.macos-codex-luna-e2e").length, 1);
  assert.equal(ids.at(-1), "evidence-v2.release-verdict");
});

test("provider call contracts remain generation 1, P1 2/4 and P2 2/4", () => {
  assert.equal(CLAUDE_DEEPSEEK_METHODS_CALLS, 1);
  assert.equal(CLAUDE_DEEPSEEK_MODEL_CERT_NORMAL_CALLS, 2);
  assert.equal(CLAUDE_DEEPSEEK_MODEL_CERT_MAX_CALLS, 4);
  assert.equal(MACOS_CODEX_LUNA_E2E_CALLS, 2);
  assert.equal(MACOS_CODEX_LUNA_E2E_MAX_CALLS, 4);
});

test("container shell sources are LF-only", () => {
  for (const name of ["prepare-image.sh", "run.sh"]) {
    assert.doesNotMatch(fs.readFileSync(path.join(WSL_ROOT, name), "utf8"), /\r/u, name);
  }
});
