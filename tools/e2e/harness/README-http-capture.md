# Windows HTTP capture driver

This PowerShell 5.1 bundle captures the public `USER_RESULT` before and after
the Linux service restart and proves that the internal `LOGPARSE_RUN` remains
unavailable. It never reads Claude settings, environment variables, or tokens,
and it never invokes Claude. Its only runtime network process is the exact
`C:\Windows\System32\curl.exe`, restricted to the fixed loopback service URL.

Copy the four source files and the generated
`windows-http-capture-driver-manifest.json` into the clean attempt evidence
directory. Keep the restart driver and its manifest in the attempt's `restart`
subdirectory. The driver validates all source manifests, authoritative
summaries, and required prior evidence before reserving any new output.

Run the first capture after the initial Windows journey has resolved the Case
and while the first Linux service is still running:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-http-capture.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 -Phase Before
```

Run the second capture after the fresh restart container is ready and
`restart-authoritative-summary.json` has been generated:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-http-capture.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 -Phase After
```

Every runtime output is atomically reserved with `FileMode.CreateNew`; an
existing or reparse-point input/output fails closed. Curl disables user config,
uses no proxy, follows no redirects, permits only HTTP, uses bounded connect
and total timeouts, and enforces a maximum response size. The four curl
write-out fields are converted to strict JSON metadata containing exactly
`http_code`, `num_redirects`, `size_download`, and `url_effective`.

The `After` phase reads the canonical `state-export.before.json`, requires one
resolved Case, exactly one `USER_RESULT`, exactly one `LOGPARSE_RUN`, and no
execution failures, then requests the internal artifact's exact content URL.
It requires one HTTP response block with status 404 and the exact
`ARTIFACT_NOT_FOUND` error envelope. It also requires the public result body to
be byte-identical to the `Before` download.
