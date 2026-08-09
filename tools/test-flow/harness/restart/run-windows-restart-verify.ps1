param([string]$EvidenceRoot = $PSScriptRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'windows-restart-lib.ps1')

$EvidenceRoot = [System.IO.Path]::GetFullPath($EvidenceRoot)
Assert-Restart (Test-Path -LiteralPath $EvidenceRoot -PathType Container) 'evidence directory is absent'

# These checks intentionally precede output reservation and every process start.
Confirm-RestartDriverManifest $PSScriptRoot
Confirm-PreRestartJourneyManifest $EvidenceRoot
$pre = Read-PreRestartSummaryValidated $EvidenceRoot
Confirm-RestartClientSkill

New-RestartOutputReservations -EvidenceRoot $EvidenceRoot -Names (Get-RestartQueryOutputNames)
Confirm-RestartClaudeVersion $EvidenceRoot

$caseId = Get-RestartStringProperty $pre 'case_id'
$prompt = @"
Perform the read-only post-restart persistence verification for existing Case $caseId. Treat only Remote MCP tool_result structured payloads as authoritative; do not infer business state from prose.

0. Your first action MUST call the Skill tool with skill=problem-locator-client (exact input {"skill":"problem-locator-client"}). Until that Skill tool_result is received successfully, do not call any problem_locator MCP tool and do not continue the workflow.
1. The successfully completed first action above is the only problem-locator-client Skill invocation.
2. Call problem_locator_get_case exactly once with case_id "$caseId", wait_for_job_id null, and wait_seconds 0.
3. Call problem_locator_list_artifacts exactly once with case_id "$caseId".
4. Stop immediately. Do not create, prepare, submit, resume, cancel, upload, download, or call any other tool.
"@

$promptPath = Join-Path $EvidenceRoot 'restart.prompt.txt'
$stdoutPath = Join-Path $EvidenceRoot 'restart.stream-json.stdout.ndjson'
$stderrPath = Join-Path $EvidenceRoot 'restart.stderr.txt'
$auditPath = Join-Path $EvidenceRoot 'restart.authoritative.json'
$summaryPath = Join-Path $EvidenceRoot 'restart-authoritative-summary.json'

Write-RestartUtf8 -Path $promptPath -Text ($prompt + "`n")
$arguments = Get-RestartClaudeArguments -Prompt $prompt
$exitCode = Invoke-RestartCapturedProcess -FilePath $script:RestartClaudeExe -Arguments (@($script:RestartClaudeEntryPoint) + $arguments) -WorkingDirectory $script:RestartRepoRoot -StdoutPath $stdoutPath -StderrPath $stderrPath -TimeoutSeconds $script:RestartClaudeQueryTimeoutSeconds
Assert-Restart ($exitCode -eq 0) 'restart Claude exit code'

$audit = Read-RestartClaudeAudit $stdoutPath
Write-RestartJson -Path $auditPath -Value $audit
$summary = Confirm-RestartPersistenceResult -Audit $audit -PreSummary $pre -EvidenceRoot $EvidenceRoot
Write-RestartJson -Path $summaryPath -Value $summary

Write-Output "restart_query=passed case_id=$($summary.case_id) artifact_id=$($summary.public_artifact.artifact_id) revision=$($summary.resolved_case_revision)"
