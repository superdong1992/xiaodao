# Problem Locator V1

Problem Locator is a single-instance diagnosis service. It accepts a structured
problem, gathers facts and attachments, runs pinned routing/diagnosis/review
jobs, and publishes a reviewed `USER_RESULT` artifact. V1 uses a durable local
JSON state file and filesystem resources; all business writes go through the
application service and its repository ports.

## Requirements and installation

- CPython 3.12 (the package requires `>=3.12,<3.13`)
- `uv` and the checked-in `uv.lock`
- a pinned Logparse checkout, config file, and Python launcher
- a Claude-compatible command for real Agent jobs

Install the exact locked runtime and development dependencies:

```sh
uv sync --frozen --all-groups
uv lock --check
```

Do not upgrade the locked MCP, HTTP, or storage-facing dependencies as part of
an operational install.

## Configuration

Copy `.env.example` to a private file and replace every placeholder with an
absolute path. An explicit `--env-file` is parsed as UTF-8 dotenv data. Values
already present in the process environment take precedence over the file.

| Variable | Required | Default | Meaning |
|---|---:|---|---|
| `DATA_ROOT` | yes | — | Exclusive durable state/resources/jobs root |
| `PUBLIC_BASE_URL` | yes | — | External HTTP(S) base URL, without query or fragment |
| `SKILL_DIR` | yes | — | Directory containing pinned diagnosis skills |
| `LOGPARSE_REPO` | yes | — | Pinned Logparse Git checkout |
| `LOGPARSE_CONFIG_PATH` | yes | — | Logparse configuration inside that checkout |
| `BIND_HOST` | no | `127.0.0.1` | Uvicorn bind host |
| `PORT` | no | `8000` | Uvicorn port |
| `CLAUDE_COMMAND` | no | `claude` | Agent command parsed as an argv template |
| `LOGPARSE_PYTHON` | no | current Python | Lexical Python launcher for Logparse |

Runtime limits are frozen contract constants, not configuration. V1 rejects
`JOB_CONCURRENCY` and unknown limit/max/retention overrides so an operator
cannot believe an ineffective limit was applied. Never configure or persist
`PROBLEM_LOCATOR_LOGPARSE_ENDPOINT` or `PROBLEM_LOCATOR_LOGPARSE_TOKEN`; those
capabilities are created per Job and are removed when the broker session ends.

## Run the service

Validate configuration and start exactly one worker:

```sh
uv run python -m problem_locator serve --env-file /absolute/path/to/service.env
```

V1 permits one service process and one Uvicorn worker for a `DATA_ROOT`. A
second process fails the instance-lock readiness check. Do not place a
multi-worker process manager in front of the same root.

Process interfaces:

- MCP transport: `/mcp`
- liveness: `GET /live`
- readiness: `GET /ready`
- prepare attachment: `POST /api/v1/cases/{case_id}/attachments`
- upload prepared bytes: `PUT /api/v1/attachments/{attachment_id}/content`
- download a public artifact: `GET /api/v1/artifacts/{artifact_id}/content`

The seven Remote MCP tools are:

- `problem_locator_create_case`
- `problem_locator_prepare_attachment`
- `problem_locator_submit_supplement`
- `problem_locator_get_case`
- `problem_locator_resume_case`
- `problem_locator_cancel_case`
- `problem_locator_list_artifacts`

The bundled `.claude/skills/problem-locator-client` skill documents safe
request IDs, revision handling, upload headers, and artifact hash checks. File
bytes travel only over HTTP; they are never embedded in MCP messages.

`/live` indicates that the HTTP process is serving. `/ready` additionally
checks configuration, the instance lock, state validity, data directories, and
startup recovery. During recovery, or after a fatal state/worker fault,
liveness can remain true while readiness is false.

## Attachment and result behavior

Preparing an attachment creates metadata and an upload descriptor. Uploading
the bytes verifies their exact size and SHA-256 and moves the attachment to
`READY`; upload alone never advances a Case. The caller must explicitly submit
the READY attachment as a supplement.

`WorkspaceAttachmentInput.filename_suffix` is required but nullable. Archive
suffix and content-type validation uses the frozen public contract helpers;
paths, uppercase aliases, and mismatched suffixes are rejected.

Only downloadable public artifacts are listed by default. A reviewed
`USER_RESULT` can be downloaded and must match its advertised byte count and
SHA-256. Internal `LOGPARSE_RUN` directories are durable inputs for later Jobs
but are never downloadable.

## Startup recovery and retry semantics

On each start the scheduler creates a new runtime epoch and completes recovery
before it accepts new claims:

1. Replay every durable, finalized but unconfirmed Job Outcome byte-for-byte.
2. Only after replay, mark an old `RUNNING` Job with no finalized Outcome as
   `INTERRUPTED`.
3. Redispatch already-persisted `PENDING` Jobs.

Outcome submission retries reuse the same finalized receipt and never rerun
the Agent. Asset/configuration errors and typed state-read errors park the
worker and fail readiness. Recovered Jobs retain every frozen runtime binding;
the current Catalog cannot replace them with a newer version. An interrupted
REVIEW resumes as REVIEW, never as DIAGNOSE.

A command whose business mutation committed but whose post-commit Case reread
failed returns the durable receipt with `case_view=null`. Treat that response
as persisted success and query the Case again; do not create a second logical
request.

## Validate, export, back up, and restore

These administration commands acquire the same exclusive instance lock. Run
them only while the service for that `DATA_ROOT` is stopped:

```sh
uv run python -m problem_locator validate-state \
  --data-root /absolute/path/to/problem-locator-data

uv run python -m problem_locator export-state \
  --data-root /absolute/path/to/problem-locator-data \
  --output /absolute/path/outside-data-root/state-export.json
```

`validate-state` emits a canonical `ValidationReport`. `export-state` writes a
canonical `StateExport` containing one state generation, complete object
counts, and a sorted resource size/hash inventory. The output must be outside
`DATA_ROOT` and is an audit/migration artifact, not a resource backup.

For a recoverable backup:

1. Stop the service and wait for shutdown to finish.
2. Run `validate-state` and `export-state`.
3. Copy the complete `DATA_ROOT` tree atomically enough to preserve
   `state.json`, `jobs/**`, and `resources/**` from the same stopped point.
4. Keep the export beside the backup for count/hash reconciliation.

To restore, keep the damaged root read-only, copy a complete known-good backup
to a new absolute root, run `validate-state`, compare its export counts and
hashes, then start the service against the new root. Do not hand-edit
`state.json`, discard a finalized outbox file, or silently fall back to
`state.json.prev`.

The r3 state schema is intentionally incompatible with pre-release r2 data.
Old data may only be rebuilt or migrated offline into a fresh r3 installation;
the service contains no in-place r2 compatibility path.

## PostgreSQL migration boundary

V1 does not ship PostgreSQL, an ORM, dual writes, or a distributed lock. Start
an offline PostgreSQL migration design when any of these becomes true:

- a second service instance or high availability is required;
- `state.json` approaches 16 MiB;
- retained history approaches 500 Cases;
- state write latency is materially affecting operation.

The migration must stop writes, export one canonical generation, import
through equivalent repository/resource records, reconcile every object count
and resource hash, and keep the original JSON root read-only until acceptance.
Domain/application/runtime code depends on frozen ports rather than the JSON
adapter so this remains an offline adapter replacement, not a business-model
fork.

## Security and known constraints

- V1 is intended for a controlled network with trusted users, pinned Skills,
  and a trusted Agent command. It does not implement tenant authorization.
- The process and Agent are not an operating-system sandbox. Run them under a
  dedicated OS account with only the required repository/data access.
- Secrets, raw environment values, server paths, log archive bytes, broker
  tokens, and internal execution logs must not appear in MCP/HTTP responses.
- Logparse is fingerprinted at startup. The first eligible diagnosis Job may
  parse once; continuation Jobs consume the persisted `LOGPARSE_RUN` and must
  not unpack or parse the original archive again.
- V1 has fixed concurrency `1`, fixed context/workspace/output limits, local
  filesystem durability, and no multi-instance failover.
- Native Windows and Linux startup validation, macOS process-tree/cancellation
  validation, deterministic fake E2E, and real Logparse smoke are release
  gates; test or handoff records must state which platform was actually run.

### Native startup gates

The native gates deliberately remain skipped on any other operating system;
that skip is an unexecuted release gate, never a pass. Each runner must use the
same release-candidate Git head, CPython 3.12, locked dependencies, and a clean
Logparse checkout at
`a233b500d9c99e6815d1ffd82cb4ca55bbfe657a`.

The current S08 candidate has no native Windows or Linux startup result. It
therefore must not be described as cross-platform release-ready. Until those
two native commands pass against the same candidate head, record them as
unexecuted gates in `handoff/S08.json` under `known_limitations` and `risks`,
and state the same restriction in `integration_notes`; do not add them to the
handoff `tests` array as passed results.

macOS shell (run on the release-candidate head):

```sh
uv sync --frozen --all-groups
export S08_NATIVE_STARTUP_GATE=darwin
export SKILL_DIR="$(pwd)/.claude/skills"
export LOGPARSE_REPO=/absolute/path/to/logparse
export LOGPARSE_CONFIG_PATH=/absolute/path/to/logparse/config.yaml
export LOGPARSE_PYTHON=/absolute/path/to/logparse/.venv/bin/python
export CLAUDE_COMMAND=claude
uv run pytest tests/e2e/test_native_startup_gate.py::test_native_macos_startup_gate -q -p no:cacheprovider
```

Windows PowerShell:

```powershell
uv sync --frozen --all-groups
$env:S08_NATIVE_STARTUP_GATE = "windows"
$env:SKILL_DIR = (Resolve-Path ".claude\skills").Path
$env:LOGPARSE_REPO = "C:\absolute\path\to\logparse"
$env:LOGPARSE_CONFIG_PATH = "C:\absolute\path\to\logparse\config.yaml"
$env:LOGPARSE_PYTHON = "C:\absolute\path\to\logparse\.venv\Scripts\python.exe"
$env:CLAUDE_COMMAND = "claude"
uv run pytest tests/e2e/test_native_startup_gate.py::test_native_windows_startup_gate -q -p no:cacheprovider
```

Linux shell:

```sh
uv sync --frozen --all-groups
export S08_NATIVE_STARTUP_GATE=linux
export SKILL_DIR="$(pwd)/.claude/skills"
export LOGPARSE_REPO=/absolute/path/to/logparse
export LOGPARSE_CONFIG_PATH=/absolute/path/to/logparse/config.yaml
export LOGPARSE_PYTHON=/absolute/path/to/logparse/.venv/bin/python
export CLAUDE_COMMAND=claude
uv run pytest tests/e2e/test_native_startup_gate.py::test_native_linux_startup_gate -q -p no:cacheprovider
```

Each test asserts the native OS, Logparse commit and clean tree, startup from an
env file, `/live`, all five `/ready` checks, bounded shutdown, canonical
`validate-state` and `export-state`, instance-lock release, and a second
recovery startup. A successful result must report the exact release-candidate
SHA, OS/build, architecture, Python version, command, and pytest count.

The real Agent Backend release smoke is separate from the deterministic fake
Agent E2E. Run it only in an isolated temporary workspace with an authenticated
Claude Code installation; the command below disables repository customizations,
session persistence, and every tool except writing the fixed output file:

```sh
export S08_REAL_AGENT_GATE=1
export S08_REAL_AGENT_COMMAND='/absolute/path/to/claude -p --safe-mode --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Write --model haiku --effort low --max-budget-usd 0.10'
uv run pytest tests/e2e/test_real_agent_backend_gate.py -q -p no:cacheprovider
```

The gate verifies an actual Claude Code version, stdin delivery through the
production `AgentBackend`, exact canonical `AgentJobOutcome` bytes, immutable
input/runtime markers, output topology, bounded execution, and process-tree
cleanup. A skipped result is not a pass.

### Clean installed-distribution gate

This gate builds the release-candidate wheel, exports only runtime dependencies
from `uv.lock` with hashes, installs both into a new CPython 3.12 environment,
and runs every installed command from outside the source tree. Set
`S08_UV_OFFLINE=1` only when the selected uv cache is already complete; leave it
at `0` on a cold runner.

```sh
export S08_INSTALLED_DISTRIBUTION_GATE=1
export S08_UV="$(command -v uv)"
export S08_UV_OFFLINE=0
export SKILL_DIR="$(pwd)/.claude/skills"
export LOGPARSE_REPO=/absolute/path/to/logparse
export LOGPARSE_CONFIG_PATH=/absolute/path/to/logparse/config.yaml
export LOGPARSE_PYTHON=/absolute/path/to/logparse/.venv/bin/python
export CLAUDE_COMMAND=/absolute/path/to/claude
uv run pytest tests/e2e/test_installed_distribution_gate.py -q -p no:cacheprovider
```

Expected result: exactly one passed test. It asserts a wheel-only import from
the fresh environment's `site-packages`, the locked runtime versions, absence
of pytest and Hatchling from that runtime environment, the pinned clean
Logparse commit and Skill product hash, installed env-file startup, `/live`,
all five `/ready` checks, bounded shutdown, and canonical installed
`validate-state`/`export-state` commands.

If a native result is available before the final S08 handoff-only commit, add
its real command and summary to `handoff/S08.json.tests[]`. If it arrives after
that immutable tip, do not amend or rewrite the handoff; attach the same fields
to the downstream release verification record and retain the S08 limitation
until an approved successor handoff incorporates the evidence.

## Release checks

Run the full explicit test roots; a bare historical pytest configuration must
not be assumed to include every suite:

```sh
uv run pytest tests/contracts tests/unit tests/integration tests/e2e
uv run python -m compileall -q src tests
uv lock --check
git diff --check
```

Real Logparse, process-tree/cancellation, clean-environment installation,
installed import/CLI/server smoke, fixture manifests, and Git ancestry/blob
integrity are separate release-candidate gates and must not be reported as
passed unless actually executed.
