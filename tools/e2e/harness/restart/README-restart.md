# Windows Claude Code restart verification driver

## Frozen restart Docker boundary

After the initial journey has produced and validated all before-restart evidence, execute step 03 of `execution-order.restart.txt` exactly. `create-restart-docker-resources.ps1` is the only authorized host-side resource transition: it validates the initial creator/metadata/scan receipts, performs the bounded stop while preserving the initial container, reuses the exact owner-labelled volume, and creates the fixed-digest restart container through audited argv arrays. It never cleans up a failed transition and writes only a sanitized CreateNew receipt.

After `/evidence/verify_orchestration_hashes.sh` passes inside that container, execute the frozen step 05 invocation of `verify-restart-docker-metadata.ps1`. That verifier checks the creator receipt against the live container ID, exact image reference and content ID, all ten mounts and their host-source bindings, the one loopback port, the two tmpfs mounts, the persistent volume label, and a nonempty DATA_ROOT. Raw Docker and DATA_ROOT output is secret-scanned before JSON parsing. See `README-restart-docker-resources.md` for the receipt and failure-preservation contracts.

The restart verifier uses the same attempt-43 settings contract as the initial verifier: ordinal enumeration over `IDictionary.Keys`, exact unique string-key selection, then indexer access. Its PowerShell 5.1 offline suite runs `JavaScriptSerializer.DeserializeObject` on real synthetic JSON and fails closed for missing or case-wrong `env`; no overload-ambiguous dictionary `Contains` call is permitted.

The three mounted executable caches are neutral, attempt-independent files under `D:\code\xiaodao\.tmp\pl-e2e-cache`; the creator and verifier fail closed on their type and fixed SHA-256. No inline PowerShell, JavaScript, Docker command string, token-bearing Docker environment, or attempt-specific cache path is an authorized replacement for steps 03 or 05.

Attempt 43 stopped in the host sandbox before any resource mutation with `DOCKER_RESOURCE_DOCKER_CONFIG`; a separate sandbox-external inspect proved both the exact container and volume absent. The attempt-44 frozen creator and verifier are unchanged apart from identity. Their Docker configuration, `docker.exe`, and actual settings operations require sandbox-external host authority.

The exact bounded sandbox-external stops of failed attempts 47 through 49 are recorded by their `old_attempt*_bounded_stop=PASS` receipts. Attempt50's created-but-never-started container and empty volume are preserved under `old_attempt50_failed_resources_preserved=PASS`. None is an attempt52 clean-chain result.

## Frozen restart execution matrix

After the exact step-03 creator and step-05 metadata verifier invocations in `execution-order.restart.txt`, run the following commands from `D:\code\xiaodao` at their numbered positions. Stop immediately on the first nonzero exit or missing required receipt. The restart Windows preflight writes only beneath the restart evidence root, while the read-only query/download and After HTTP capture deliberately write their runtime evidence to the main root.

```text
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_orchestration_hashes.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_shell_syntax.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/bootstrap_apt.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/uv_mount_preflight.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_uv_preflight.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/bootstrap_uv.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/setup_sources.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/setup_venvs.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_python_syntax.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/setup_fixtures.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/setup_claude.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/restart_nonroot_runtime_init.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_restart_nonempty_runtime.sh
docker.exe --config C:\Users\admin\.docker exec --detach pl-e2e-fix52-restart-20260802-205054 sh /evidence/start_service_supervisor.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/gate_service_preflight.sh
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\restart\windows-service-preflight.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\restart
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\restart\run-windows-restart-verify.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\restart\download-windows-restart-artifact.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-http-capture.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 -Phase After
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/stop_service.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/capture_state_after_restart.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/run_final_audits.sh
docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/capture_linux_identity.sh
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\finalize-attempt52.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 -DockerConfig C:\Users\admin\.docker -SettingsPath C:\Users\admin\.claude\settings.json
```

This bundle verifies the persisted result after the Linux service is restarted. It is deliberately read-only: Claude Code can load `problem-locator-client` and can call only `problem_locator_get_case` and `problem_locator_list_artifacts`. The dynamic Case ID comes from the already validated `journey-authoritative-summary.json`; it is never supplied as an unverified command-line value.

Copy these five source files into the same clean-attempt evidence directory that contains the original journey driver, `windows-journey-driver-manifest.json`, and `journey-authoritative-summary.json`. Then create the restart manifest without contacting the service or invoking Claude:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\static-check-restart.ps1
```

After the parent harness has restarted the service with the same persistent data volume and declared it ready, run the read-only Claude query from `D:\code\xiaodao`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <attempt-evidence>\run-windows-restart-verify.ps1
```

The query validates both driver manifests, the pre-restart authoritative summary, and the exact client Skill hash before reserving output files or starting any process. It uses native Claude Code 2.1.150, `--setting-sources user,project`, the `haiku` alias (which must report the effective model `deepseek-v4-flash[1m]`), an inline strict loopback MCP configuration, and `Skill(problem-locator-client)` as the first tool call. Business state is accepted only from uniquely correlated stream-json `tool_use`/`tool_result` pairs carrying top-level `tool_use_result.structuredContent`.

The optional deterministic download is a separate read-only HTTP step:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <attempt-evidence>\download-windows-restart-artifact.ps1
```

It derives the loopback URL, expected length, and SHA-256 from the validated restart summary, then uses `curl.exe` with bounded time and size. All runtime evidence names are reserved atomically with `FileMode.CreateNew`; existing evidence is never overwritten. None of these source files reads, copies, prints, or materializes credential settings.
