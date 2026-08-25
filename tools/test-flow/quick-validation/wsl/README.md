# Ubuntu 22.04 container wrapper for Quick Validation

This directory adds a sealed Ubuntu 22.04 execution boundary around the existing public Quick Validation
Goals. It does not define another Goal, Proof, Stage, Gate, model call, cache, evidence format, or validator.
Inside the container it delegates mechanically to the same canonical entry used by native macOS:

```text
tools/test-flow/run.sh --track dev --client macos --goal dev.macos-*
```

The `macos` client value is the existing Goal contract label. The plan and runtime receipts separately record
that the orchestrator is the sealed Linux/x64 container. The native macOS commands and identities remain
unchanged.

Build the image from the frozen local caches. The pinned Ubuntu base and all expensive layers are reused when
they already exist; the script does not request a fresh pull:

```bash
bash tools/test-flow/quick-validation/wsl/prepare-image.sh \
  --cache-root /home/xiaodao/quick-validation/cache \
  --codex-root /home/xiaodao/quick-validation/cache/codex/0.149.1/bin \
  --logparse-source /home/xiaodao/quick-validation/src/logparse
```

Plan the existing Codex/Luna Methods Goal in the container:

```bash
bash tools/test-flow/quick-validation/wsl/run.sh \
  --provider codex-luna \
  --cache-root /home/xiaodao/quick-validation/cache \
  --evidence-root /home/xiaodao/quick-validation/evidence/ubuntu2204-central \
  --codex-auth /home/xiaodao/quick-validation/secrets/codex-auth.json \
  -- --goal dev.macos-codex-luna-methods \
  --allow-codex-posthoc-budget \
  --allow-real-model \
  --reason 'Run the existing Codex Luna Quick Validation in Ubuntu 22.04' \
  --hypothesis 'The sealed container changes only the platform runtime boundary' \
  --expected-evidence 'The existing central Codex Quick verdict and evidence' \
  --plan-only
```

Plan the existing Claude/DeepSeek Methods Goal in the container:

```bash
bash tools/test-flow/quick-validation/wsl/run.sh \
  --provider claude-deepseek \
  --cache-root /home/xiaodao/quick-validation/cache \
  --evidence-root /home/xiaodao/quick-validation/evidence/ubuntu2204-central \
  --claude-settings /home/xiaodao/quick-validation/secrets/claude-settings.json \
  -- --goal dev.macos-claude-deepseek-methods \
  --allow-real-model \
  --reason 'Run the existing Claude DeepSeek Quick Validation in Ubuntu 22.04' \
  --hypothesis 'The sealed container changes only the platform runtime boundary' \
  --expected-evidence 'The existing central Claude Quick verdict and evidence' \
  --plan-only
```

After a Methods cache has passed, select the matching `dev.macos-*-e2e` Goal with the same wrapper. Remove
`--plan-only` only after reviewing that exact plan and its admission. The wrapper accepts only these four
existing Quick Goals and owns all platform/runtime paths.

The container runs as UID/GID 0 with Docker's normal root capability set, a read-only root filesystem,
Docker init, a container-private executable scratch tmpfs, provider-scoped read-only credentials, and no
Docker socket, added capability, privileged mode, host PID, or host network. Codex alone receives
`seccomp=unconfined` so its existing bubblewrap permission profile can create the Linux user namespace and
uid map required by command execution. The wrapper does not add a nested `setpriv`/cap-drop boundary.
`/private/tmp`, `/usr/bin/python3`, and BSD `stat -f %z` are thin compatibility boundaries for the existing
tests; their test logic is not copied into the wrapper.

For the Codex service phases, Linux project metadata stays in a disposable project beneath the product
Workspace `runtime/` directory; the product Workspace root therefore remains exactly `inputs/`, `output/`,
and `runtime/`. Only the phase's bounded ordinary draft is copied back before the existing product finalizer
or validator runs. Trusted read-only hard-linked inputs are copied into independent files, while generated
drafts must remain single-link files.

Both provider E2E service runners receive the frozen finalizer and Logparse CLI paths explicitly and start
isolated Python with `-I -B`. Each provider forwards semantic `stage.progress` heartbeats to the existing
Test Flow watchdog. Claude keeps its own private settings, tool-permission, and service Workspace design; it
does not inherit the Codex bubblewrap or service-project mirror.
