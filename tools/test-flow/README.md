# Test Flow

`test-flow` is the only entry point for new test activity. It resolves a proof goal into stable Stages, computes exact producer/proof identities, rejects expensive work with missing prerequisites, reuses only sealed compatible evidence, and commits one immutable `verdict.json` last.

## Tracks

Dev is the default feedback track:

```sh
./tools/test-flow/run.sh --plan-only
./tools/test-flow/run.sh
```

It runs `framework.self-test`, affected deterministic selectors, then `tests/deterministic`. If the affected selectors already cover at least half of the deterministic test files, that duplicate pass is folded into the following full Stage, so a broad change runs the suite once. SameJob is deterministic and is covered there. No real model is allowed by the default goal.

Before pytest collection, the same frozen Python runtime performs a loopback-bind
capability probe because the deterministic suite contains broker tests. A sandbox
that forbids local sockets is reported immediately as `BLOCKED/INFRA`, rather
than manufacturing a large batch of product failures after most tests have run.

The runner uses the locked offline `uv` environment when available. To bind an
already-provisioned interpreter explicitly, set `TEST_FLOW_PYTHON` to its
absolute Python 3.12 executable. That executable, its byte hash, the Python and
pytest versions, and the core dependency versions become part of the producer
identity. Every effective Python import root outside the repository is also
content-hashed in import order, including an explicitly supplied `PYTHONPATH`;
safe hashes of Python/pytest environment controls prevent reuse across changed
runtime settings. A missing or incompatible runtime blocks the Stage before
testing.

A single isolated real proof is explicit and starts with a plan:

```sh
./tools/test-flow/run.sh \
  --track dev \
  --goal dev.real \
  --stage real.route \
  --allow-real-model \
  --reason "route prompt changed" \
  --hypothesis "the current host now emits the flat call" \
  --expected-evidence "one valid RouteDecision and no removed fields" \
  --plan-only
```

Remove `--plan-only` only after the selected Stage, identity inputs, reuse decisions, and token/cost advisory are correct. The framework never retries a real call automatically. An unchanged failed identity requires all three structured intent fields above.

Release is a fresh proof, not a continuation of Dev state:

```sh
node ./tools/test-flow/prepare-release-cache.mjs --docker-context colima
node ./tools/test-flow/prepare-release-settings.mjs \
  --output /private/tmp/problem-locator-release-settings.json

./tools/test-flow/run.sh \
  --track release \
  --goal release.full \
  --claude-entry /absolute/xiaodao/.tmp/test-flow-cache/claude/2.1.89/package/cli.js \
  --claude-settings /private/tmp/problem-locator-release-settings.json \
  --docker-context colima \
  --logparse-source /absolute/logparse \
  --mcp-source /absolute/problem-locator-mcp \
  --plan-only
```

The macOS adapter validates the supplied settings file, materializes only the seven
allowlisted environment values into the attempt-scoped scratch directory, and bind
mounts that staged copy into Colima. This keeps an absolute source such as
`/private/tmp/...` from being passed directly to the Linux Docker daemon; scratch is
removed before the evidence payload is sealed.

Cache preparation is the only step allowed to download the official Claude npm tarball, uv archive, base image, Python runtime, or locked dependencies. It verifies the frozen artifact hashes and builds the `linux/amd64` image. Release itself uses `--pull never` and offline installs only. The separate settings preparation command reads only the existing auth token and HTTPS endpoint from the process environment; it writes a mode-`0600` seven-key allowlist, pins all three model aliases to `deepseek-v4-flash[1m]`, and never changes global Claude settings.

On macOS, Release admission requires a clean commit, the explicit cached official npm `cli.js`, exact output `2.1.89 (Claude Code)`, the package manifest and complete package tree, Node identity, env-only settings/model fingerprint, Docker context `colima`, a Linux `x86_64` Server, the prepared image, and the two exact clean external source commits. Global Claude Code is deliberately ignored, so an installed `2.1.201` can remain the normal global version. The repository-owned macOS CrossJob adapter is selected automatically; `--cross-job-adapter` is not required on macOS.

The source settings file may contain other top-level Claude settings, but the runner materializes only the exact seven-key `env` allowlist into an attempt-scoped mode-`0600` file. Hooks, permissions, local MCP definitions, proxies, and client DFX are not copied or installed. The native macOS Client connects by strict HTTP MCP directly to the fresh Linux container. Client phases carry hard USD caps of 3/5/1; each automatically dispatched Linux agent already carries its own USD 3 cap. The plan therefore reports the complete conservative client+server cost envelope instead of charging environment or upload-only Stages for nonexistent model calls. Release lineage is always `GENESIS`, the named DATA_ROOT volume must be observed empty before container initialization, and no checkpoint is ever admitted as Release input.

The post-restart phase is strictly read-only (`get_case` and `list_artifacts`). Its
per-instance business Journey stream may therefore be empty, but the diagnostics
stream remains mandatory and must prove exact Client-to-Server MCP correspondence,
successful artifact downloads, and a clean service shutdown.

## Proof layout

```text
tests/
  deterministic/{contracts,unit,integration,journey}
  real/{agent,logparse}
  platform/{distribution,server_linux,client,compat}
```

Declarative Goal, Stage, Gate, and identity definitions live in `config/*.v1.json`. Symbolic actions are allowlisted in code; configuration cannot inject a shell command.

## CrossJob adapter contract

The adapter is an executable invoked once per external Stage. It receives scalar arguments only:

- `--stage <stable-stage-id>`
- `--attempt-root <absolute-attempt-root>`
- `--client windows|macos|linux`
- `--resource-registry <resources.ndjson>` and the exact `--resource-label`
- `--fresh-data-root` only for `journey.cross-job.environment`
- `--restored-data-root`, `--restored-continuation`, and `--restored-checkpoint-id` only after a Dev checkpoint restore
- `--checkpoint-output-source <path>` at a natural checkpoint boundary

Every created container or volume must be appended to the registry with the exact run label before use. A checkpoint source is written only after the service is stopped and state is quiescent. Its JSON contains `schema_version: 1`, an absolute `state_root`, a typed scalar/array-only `continuation`, and a quiescence receipt proving zero running/queued jobs, zero active workers/workspaces, and a passing state validation.

Checkpoint export separately classifies retained temporary state before omitting it, after the installed product's `validate-state` command has validated the authoritative StateFile and its canonical encoding. A completed upload stage is discardable only when its canonical marker matches a `READY` Attachment. A proposal stage is discardable only when its marker matches a saved Outcome (or the matching formal server audit artifact), its owner Job is terminal, and that Outcome already has an authoritative processing record. Any unprocessed durable outbox, incomplete or unknown stage, non-empty quarantine or state-temporary area, filesystem link, or unexpected layout rejects the checkpoint. The classifier writes a sanitized receipt so this failure is distinguishable from Docker or archive-copy failures.

Route→Upload, Upload→Diagnose, automatic Diagnose+Review→Publish/Restart, and Publish/Restart→end are the only checkpoint boundaries. Diagnose and its automatically dispatched Review are one indivisible reuse segment. Restore always verifies and rescans the old seal, extracts into a unique empty root, then requires the adapter to import it into a new empty server volume. Release ignores all business checkpoints and starts from GENESIS.

## Events and timeouts

Each producer owns one flushed NDJSON file under `payload/events`. Raw stdout/stderr bytes do not count as progress. Only allowlisted tool completion, HTTP completion, job lifecycle, state change, or explicit Stage progress events reset the no-progress clock. Real work uses a 360-second no-progress limit and a 1200-second Stage hard limit; output streams are capped. Missing, truncated, over-limit, invalid, or sequence-broken required evidence cannot PASS.

The Linux service supervisor sets `DFX_LOG_DIR` outside `DATA_ROOT` and relays `journey.jsonl` plus diagnostic JSON into separate producer streams. This keeps checkpoints free of observability files and makes Review an observed automatic segment rather than an artificial pause.

## Verdict and exit codes

Finalization stops writers first, seals and scans the immutable payload, applies the exact labeled resource policy, records the live resource receipt, scans finalization metadata, and atomically creates `verdict.json` last. Cleanup failure may preserve `functional_status: PASS` and reusable functional evidence, but `overall` is `ERROR` and the process exits nonzero.

- `0`: `PASS` or `PASS_WITH_WARNINGS`
- `1`: functional failure or two consecutive significant Release performance regressions
- `2`: `BLOCKED` or `INCONCLUSIVE`
- `3`: framework, evidence, security, finalization, or cleanup error

No `verdict.json` means `UNFINALIZED`, never PASS. `verification-report.json` is compatibility metadata only.

## Evidence retention

Evidence is never deleted automatically:

```sh
node tools/test-flow/evidence.mjs report
node tools/test-flow/evidence.mjs prune --dry-run --keep-last 10
node tools/test-flow/evidence.mjs prune --run-id run-YYYYMMDDTHHMMSSZ-1234abcd --execute
```

The execute form requires an exact run ID and reports that removal has no automatic recovery.

## One-time migration parity

`release.rollout-parity` is a bounded migration goal, not a recurring gate. It first runs the cheap deterministic admission path, then executes exactly `legacy` followed by `candidate` from an immutable JSON spec. Both inherit the same environment and must remain on the same clean commit. The adapter reserves an immutable ledger entry before starting, so interruption cannot silently trigger another costly pair.

The spec contains `schema_version`, `source_commit`, and two command objects. Each command has an absolute `executable`, its SHA-256, a scalar `arguments` array, and at least one frozen input `{path, sha256}`. Secrets stay in the inherited environment, never in this file. Run it only once after inspecting the plan:

```sh
./tools/test-flow/run.sh \
  --track release \
  --goal release.rollout-parity \
  --rollout-parity-spec /absolute/parity.v1.json \
  --logparse-source /absolute/logparse \
  --mcp-source /absolute/problem-locator-mcp \
  --cross-job-adapter /absolute/cross-job-adapter \
  --plan-only
```
