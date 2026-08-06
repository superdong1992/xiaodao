# Windows Claude Code journey driver

For the production fixes discovered by this journey, the Linux-to-Linux coverage gaps,
and the regression gates that must remain in place, see
[`doc/windows-to-linux-e2e-retrospective.md`](../../../doc/windows-to-linux-e2e-retrospective.md).

This template drives only the public Windows Claude Code → Remote MCP → HTTP upload path. It never reads, copies, prints, or materializes credential settings. Claude Code itself receives `--setting-sources user,project`; the MCP configuration is an inline, strict, credential-free JSON argument.

Copy these four source files into the service-ready attempt evidence directory, then run the static check there. Do not copy any prior attempt output or state:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\static-check.ps1
```

Run from `D:\code\xiaodao` after the parent harness has declared the service ready:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <attempt-evidence>\run-windows-journey.ps1 -Mode Phase1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <attempt-evidence>\run-windows-journey.ps1 -Mode Upload
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <attempt-evidence>\run-windows-journey.ps1 -Mode Phase3
```

`-Mode All` runs the same three gates sequentially. Phase 1 creates one Case with empty facts, validates parameter group A as an exact name/kind set independent of the randomly sorted requirement array, supplies it, waits for the real `log_archive` requirement, and prepares the descriptor. Upload validates the fixed 2367-byte ZIP and its SHA-256, validates the descriptor exactly, and invokes native `curl.exe` with its four headers. Before Phase 3 mutates the Case, a real Claude Code probe points the client Hook at an unwritable target and uses `hook-failure.claude-debug.log` to prove that the Hook reports its exit-1 logging failure while the read-only Remote MCP call still succeeds. Phase 3 then explicitly submits the READY attachment, supplies `order_id`, observes `REVIEWING` then `RESOLVED`, and requires exactly two public artifacts: `diagnosis-result.json` (`USER_RESULT`) and `result.zip` (`USER_RESULT_ARCHIVE`). The accepted `LOGPARSE_RUN` remains internal.

Raw Claude stdout is retained as stream-json NDJSON and stderr is separate. Console output and `*.authoritative.json` use only correlated `tool_use`/`tool_result` records; assistant prose is never used as business state. Every service URL is constrained to `127.0.0.1:18000`; the model-driven subprocess is fixed to official npm Claude Code 2.1.89, clears `HTTP_PROXY`, `HTTPS_PROXY`, and their lowercase aliases, and keeps an exact `NO_PROXY` covering loopback plus the configured model API host. The separate direct HTTP schema gate runs with unusable HTTP and HTTPS proxies to prove MCP proxy bypass, while the model-driven journey remains separate. Every output file is fail-closed against overwrite.

## Frozen Docker host boundary

Before any container-side gate, run `test-create-docker-resources.ps1` offline and then invoke `create-docker-resources.ps1` exactly as frozen in `execution-order.txt`. Inline PowerShell, inline JavaScript, and hand-built Docker commands are not authorized. The creator receives explicit source directories and regular cache-file paths, proves the exact resource names absent through successful Docker list calls, and preserves partial resources on failure. After creation, run the container-side orchestration hash gate first and only then the eleven-parameter `verify-docker-metadata.ps1`; see `README-docker-resources.md` and `README-docker-metadata.md`.

Attempt 52 preserves the attempt 43 fix for the attempt 42 step-05 settings lookup that invoked an overload-ambiguous generic-dictionary `Contains` method. Both initial and restart metadata verifiers enumerate `IDictionary.Keys` with ordinal comparison, require string keys and one exact match, and only then use the indexer. Their PowerShell 5.1 offline regressions deserialize real synthetic JSON through `JavaScriptSerializer` and cover valid, missing, and case-wrong `env` objects without reading the actual settings file.

Attempt 43 stopped before resource creation with stable error `DOCKER_RESOURCE_DOCKER_CONFIG` because the host sandbox could not access the Docker configuration. An independent sandbox-external inspect proved both the exact container and volume absent. Attempt 52 retains the same frozen file calls; steps 03 and 05 must run with sandbox-external authority for Docker configuration, `docker.exe`, and actual settings access.

The sandbox-external host also performed exact bounded stops of old containers when required to release loopback port 18000. Failed attempts 47 through 49 remain preserved after their exact bounded-stop receipts. Attempt50's created-but-never-started container and empty volume remain preserved under `old_attempt50_failed_resources_preserved=PASS`; this is cleanup preparation, not an attempt52 clean-chain result.

## Frozen initial gate matrix

After steps 03 through 05, execute these commands from `D:\code\xiaodao` exactly in order and stop on the first nonzero exit:

```text
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/verify_shell_syntax.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/bootstrap_apt.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/uv_mount_preflight.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/verify_uv_preflight.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/bootstrap_uv.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/setup_sources.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/setup_venvs.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/verify_python_syntax.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/setup_fixtures.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/setup_claude.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/setup_nonroot_runtime.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/verify_nonroot_python_launchers.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_preclean.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_target.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_full.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_post.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/gate_installed_distribution.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_native_independent.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_real_logparse.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_secret_scanner_harness.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_real_agent.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_real_route_agent.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_real_diagnose_agent.sh
```

## Frozen initial runtime matrix

After steps 03 through 20 pass, run the following nine commands from `D:\code\xiaodao`, exactly in this order. Do not introduce a wrapper or inline command:

```text
docker.exe --config C:\Users\admin\.docker exec --detach pl-e2e-fix52-20260802-205054 sh /evidence/start_service_supervisor.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_service_preflight.sh
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\windows-service-preflight.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-journey.ps1 -Mode Phase1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-journey.ps1 -Mode Upload -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-journey.ps1 -Mode Phase3 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-http-capture.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 -Phase Before
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/stop_service.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/capture_state_before_restart.sh
```

The receipt groups, in the same order, are: `service-supervisor-launch.txt`; `service-process-isolation.json` plus `service-preflight.json`; `windows-live-ready-preflight.json`; the Phase1, Upload, and Phase3 authoritative files ending in `journey-authoritative-summary.json`; the before-restart HTTP headers, metadata, and bodies for both `diagnosis-result.json` and `result.zip`; `service-exit-status.txt`, `service-log-secret-scan.json`, `service.log`, and `service-stop-verification.txt`; then `validate-state.before.json`, `state-export.before.json`, and `state-admin-before-restart.txt`. `execution-order.txt` is authoritative for the complete exact receipt names.

The real model can occasionally emit `problem_locator_get_case` with `{}` even after the prompt explicitly requires `case_id`. The journey gate records, rather than hides, this nondeterminism: at most three such calls per phase may be classified as validation corrections, and only when the strict server response is exactly a zero-side-effect `VALIDATION_ERROR` for a missing `case_id` and a later `get_case` succeeds. Every other invalid shape remains fatal, including a string `problem_spec`, non-object `submit_supplement.inputs`, and legacy `prepare_attachment` field names.
