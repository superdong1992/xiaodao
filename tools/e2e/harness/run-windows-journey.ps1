param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Phase1', 'Upload', 'Phase3', 'All')]
    [string]$Mode,

    [string]$EvidenceRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$evidenceRootExplicitlyBound = $PSBoundParameters.ContainsKey('EvidenceRoot')
$runtimeScriptRoot = $PSScriptRoot
. (Join-Path $runtimeScriptRoot 'windows-journey-lib.ps1')

$EvidenceRoot = Resolve-JourneyEvidenceRoot -EvidenceRoot $EvidenceRoot -EvidenceRootExplicitlyBound $evidenceRootExplicitlyBound -RuntimeScriptRoot $runtimeScriptRoot
Assert-Journey (Test-Path -LiteralPath $EvidenceRoot -PathType Container) 'evidence directory is absent'
Confirm-JourneyDriverManifest $runtimeScriptRoot
Confirm-JourneyClientSkill
$requestIds = Get-JourneyRequestIds $EvidenceRoot

# Validate every prerequisite that already exists before reserving any output or
# starting a process that can write remotely.  All mode creates these states in
# this invocation, so its per-phase validators remain the authoritative gates.
if ($Mode -eq 'Upload') {
    [void](Read-JourneyPhase1StateValidated $EvidenceRoot)
}
if ($Mode -in @('Upload', 'All')) {
    $preflightZipPath = Join-Path $EvidenceRoot $script:JourneyZipName
    Assert-Journey (Test-Path -LiteralPath $preflightZipPath -PathType Leaf) 'real ZIP is absent from the attempt evidence directory'
    Assert-Journey ((Get-Item -LiteralPath $preflightZipPath).Length -eq $script:JourneyZipSize) 'real ZIP byte count'
    $preflightZipDigest = (Get-FileHash -LiteralPath $preflightZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-Journey ($preflightZipDigest -ceq $script:JourneyZipSha256) 'real ZIP SHA-256'
}
if ($Mode -eq 'Phase3') {
    [void](Read-JourneyUploadStateValidated $EvidenceRoot)
}

$includeVersion = $Mode -in @('Phase1', 'All')
if ($Mode -eq 'Phase3') {
    $versionStdoutExists = Test-Path -LiteralPath (Join-Path $EvidenceRoot 'windows-claude-version.stdout.txt') -PathType Leaf
    $versionStderrExists = Test-Path -LiteralPath (Join-Path $EvidenceRoot 'windows-claude-version.stderr.txt') -PathType Leaf
    Assert-Journey ($versionStdoutExists -eq $versionStderrExists) 'Windows Claude version evidence pair must be complete before phase3'
    $includeVersion = -not $versionStdoutExists
}
$plannedOutputNames = Get-JourneyPlannedOutputNames -Mode $Mode -IncludeVersion $includeVersion
New-JourneyOutputReservations -EvidenceRoot $EvidenceRoot -Names $plannedOutputNames

function New-Phase1Prompt {
    $problemSpec = Get-JourneyProblemSpec | ConvertTo-Json -Compress -Depth 20
    return @"
Perform phase 1 of the controlled Problem Locator acceptance journey. Use only the Skill tool and the seven problem_locator Remote MCP tools. Treat MCP tool_result structured payloads as authoritative; do not infer state from prose.

0. Your first action MUST call the Skill tool with skill=problem-locator-client (exact input {"skill":"problem-locator-client"}). Until that Skill tool_result is received successfully, do not call any problem_locator MCP tool and do not continue the workflow.
1. Call problem_locator_create_case exactly once with request_id "$($requestIds.create)", this exact problem_spec JSON, initial_user_facts [], and wait_seconds 0:
$problemSpec
2. Poll problem_locator_get_case with a non-empty object containing case_id from the authoritative create_case result, wait_for_job_id null or the authoritative active job_id, and wait_seconds no greater than 30 until the authoritative Case view is WAITING_INPUT and exactly contains this set of four OPEN INPUT requirements, comparing by name and ignoring array order: caller_service, server_service, rpc_method, problem_time. Before every poll, silently verify that case_id is present; never call this tool with {}, null, or omitted input.
3. In one problem_locator_submit_supplement call, use request_id "$($requestIds.submit_a)", the latest displayed case_revision, attachment_ids [], wait_seconds 0, and exactly these inputs without normalization: caller_service=checkout-synthetic, server_service=inventory-synthetic, rpc_method=ReserveStock, problem_time=2026-07-31T00:00:03.000Z.
4. Poll problem_locator_get_case with the same non-empty input rules until the authoritative view is WAITING_ATTACHMENT with exactly one OPEN ATTACHMENT requirement named log_archive.
5. Call problem_locator_prepare_attachment exactly once using request_id "$($requestIds.prepare)", the latest displayed revision, name "$($script:JourneyZipName)", content_type "application/zip", declared_size $($script:JourneyZipSize), and declared_sha256 "$($script:JourneyZipSha256)". Immediately before emitting that tool call, silently verify that its input JSON is non-empty and has exactly these seven properties: request_id, case_id, expected_case_revision, name, content_type, declared_size, declared_sha256. Never invoke problem_locator_prepare_attachment with {}, null, or omitted input, and do not narrate between this verification and the tool call. If an empty-input VALIDATION_ERROR nevertheless occurs, the immediately following retry must contain all seven properties; a second empty invocation is forbidden and you must stop instead of retrying it.
6. Stop immediately after the successful prepare tool_result. Do not upload bytes, submit the attachment, submit order_id, list artifacts, resume, cancel, create another Case, or use any non-allowed tool.
"@
}

function New-Phase3Prompt {
    $uploadState = Read-JourneyUploadStateValidated $EvidenceRoot
    $caseId = Get-JourneyStringProperty $uploadState 'case_id'
    $attachmentId = Get-JourneyStringProperty $uploadState 'attachment_id'
    $caseRevision = Get-JourneyIntegerProperty $uploadState 'case_revision'
    return @"
Perform phase 3 of the same controlled Problem Locator acceptance journey. Use only the Skill tool and the seven problem_locator Remote MCP tools. Treat MCP tool_result structured payloads as authoritative; do not infer state from prose. The outer PowerShell upload validator has authoritatively established Case $caseId, READY attachment $attachmentId, and current case_revision $caseRevision.

0. Your first action MUST call the Skill tool with skill=problem-locator-client (exact input {"skill":"problem-locator-client"}). Until that Skill tool_result is received successfully, do not call any problem_locator MCP tool and do not continue the workflow.
1. First call problem_locator_submit_supplement exactly once with request_id "$($requestIds.submit_attachment)", case_id "$caseId", expected_case_revision $caseRevision, inputs {}, attachment_ids ["$attachmentId"], and wait_seconds 0.
2. Poll problem_locator_get_case using a non-empty object with case_id "$caseId", wait_for_job_id null or the authoritative active job_id, and wait_seconds no greater than 30 until the authoritative Case view is WAITING_INPUT with exactly one OPEN INPUT requirement named order_id. Before every poll, silently verify that case_id is present; never call this tool with {}, null, or omitted input.
3. Call problem_locator_submit_supplement exactly once with request_id "$($requestIds.submit_order)", the latest displayed revision, inputs {"order_id":"synthetic-order-0001"}, attachment_ids [], and wait_seconds 0.
4. Poll promptly with problem_locator_get_case using the same non-empty input rules. First observe REVIEWING in an authoritative result, then continue polling with wait_for_job_id set to the authoritative active review job_id until an authoritative result is RESOLVED with final_result.status ACCEPTED. Do not skip the REVIEWING observation.
5. After RESOLVED, call problem_locator_list_artifacts exactly once for this Case and stop. Do not download, resume, cancel, create another Case, or use any non-allowed tool.
"@
}

function Run-Phase1 {
    $audit = Invoke-JourneyClaudePhase -Phase phase1 -EvidenceRoot $EvidenceRoot -Prompt (New-Phase1Prompt)
    $state = Invoke-JourneyPhase1Validation -Audit $audit -EvidenceRoot $EvidenceRoot
    Write-JourneyJson -Path (Join-Path $EvidenceRoot 'phase1-state.json') -Value $state
    Write-Output "phase1=passed case_id=$($state.case_id) attachment_id=$($state.attachment_id)"
}

function Run-Upload {
    $state = Invoke-JourneyUpload -EvidenceRoot $EvidenceRoot
    Write-Output "upload=passed case_id=$($state.case_id) attachment_id=$($state.attachment_id) revision=$($state.case_revision)"
}

function Run-Phase3 {
    $uploadState = Read-JourneyUploadStateValidated $EvidenceRoot
    [void](Invoke-JourneyHookFailureProbe -EvidenceRoot $EvidenceRoot -CaseId (Get-JourneyStringProperty $uploadState 'case_id'))
    $audit = Invoke-JourneyClaudePhase -Phase phase3 -EvidenceRoot $EvidenceRoot -Prompt (New-Phase3Prompt)
    $state = Invoke-JourneyPhase3Validation -Audit $audit -EvidenceRoot $EvidenceRoot
    Write-JourneyJson -Path (Join-Path $EvidenceRoot 'journey-authoritative-summary.json') -Value $state
    Write-Output "phase3=passed case_id=$($state.case_id) artifact_id=$($state.public_artifact.artifact_id) revision=$($state.resolved_case_revision)"
}

switch ($Mode) {
    'Phase1' { Run-Phase1 }
    'Upload' { Run-Upload }
    'Phase3' { Run-Phase3 }
    'All' {
        Run-Phase1
        Run-Upload
        Run-Phase3
    }
}
