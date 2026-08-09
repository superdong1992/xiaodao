Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$utf8 = New-Object Text.UTF8Encoding($false, $true)

function Assert-E2ETest {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "E2E_HARNESS_TEST_FAILED:$Message" }
}

function Assert-E2EThrows {
    param([scriptblock]$Action, [string]$ExpectedMessage)
    try {
        & $Action
    }
    catch {
        Assert-E2ETest ($_.Exception.Message.Contains($ExpectedMessage)) "unexpected error: $($_.Exception.Message)"
        return
    }
    throw "E2E_HARNESS_TEST_FAILED:expected error $ExpectedMessage"
}

foreach ($relative in @('bounded-process.ps1', 'freeze-source-patch.ps1', 'run-windows-linux-e2e.ps1', 'harness/windows-journey-lib.ps1', 'harness/windows-http-capture-lib.ps1', 'harness/restart/windows-restart-lib.ps1')) {
    $path = Join-Path $root $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        if ($relative -ceq 'run-windows-linux-e2e.ps1') { continue }
        throw "E2E_HARNESS_TEST_FAILED:missing $relative"
    }
    $tokens = $null
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    Assert-E2ETest ($errors.Count -eq 0) "PowerShell syntax $relative"
}

$orchestratorText = [IO.File]::ReadAllText((Join-Path $root 'run-windows-linux-e2e.ps1'), $utf8)
Assert-E2ETest ($orchestratorText.Contains("[ValidateSet('Fast', 'Release', 'ReleaseGates')]")) 'release-only profile is explicit'
Assert-E2ETest ($orchestratorText.Contains('$Profile -cne ''ReleaseGates''')) 'release-only profile skips the business journey'
Assert-E2ETest ($orchestratorText.Contains("'BUSINESS_EVIDENCE_PRODUCTION_PATCH'")) 'release-only profile pins the successful production patch'
Assert-E2ETest ($orchestratorText.Contains("'BUSINESS_EVIDENCE_SECRET_SCAN'")) 'release-only profile requires a clean business secret scan'
Assert-E2ETest ($orchestratorText.Contains("'BUSINESS_EVIDENCE_STATE_AUDIT_STATUS'")) 'release-only profile requires the persistence audit'

. (Join-Path $root 'business-patch-identity.ps1')
$patchIdentityRoot = Join-Path ([IO.Path]::GetTempPath()) ('pl-e2e-patch-identity-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($patchIdentityRoot) | Out-Null
try {
    $productionSection = "diff --git a/src/example.py b/src/example.py`nindex 1111111..2222222 100644`n--- a/src/example.py`n+++ b/src/example.py`n@@ -1 +1 @@`n-old`n+new`n"
    $testSectionA = "diff --git a/tests/test_example.py b/tests/test_example.py`nindex 3333333..4444444 100644`n--- a/tests/test_example.py`n+++ b/tests/test_example.py`n@@ -1 +1 @@`n-old-test`n+new-test-a`n"
    $testSectionB = $testSectionA.Replace('+new-test-a', '+new-test-b')
    $productionSectionB = $productionSection.Replace('+new', '+newer')
    $patchA = Join-Path $patchIdentityRoot 'a.patch'
    $patchB = Join-Path $patchIdentityRoot 'b.patch'
    $patchC = Join-Path $patchIdentityRoot 'c.patch'
    [IO.File]::WriteAllText($patchA, $productionSection + $testSectionA, $utf8)
    [IO.File]::WriteAllText($patchB, $productionSection + $testSectionB, $utf8)
    [IO.File]::WriteAllText($patchC, $productionSectionB + $testSectionA, $utf8)
    $identityA = Get-E2EBusinessPatchIdentity -PatchPath $patchA
    $identityB = Get-E2EBusinessPatchIdentity -PatchPath $patchB
    $identityC = Get-E2EBusinessPatchIdentity -PatchPath $patchC
    Assert-E2ETest ($identityA.sha256 -ceq $identityB.sha256) 'test-only patch changes preserve business identity'
    Assert-E2ETest ($identityA.sha256 -cne $identityC.sha256) 'production patch changes invalidate business identity'
    Assert-E2ETest ($identityA.production_file_count -eq 1 -and $identityA.total_file_count -eq 2) 'business identity counts exact files'
}
finally {
    [IO.Directory]::Delete($patchIdentityRoot, $true)
}

$journeyDriverText = [IO.File]::ReadAllText((Join-Path $root 'harness\run-windows-journey.ps1'), $utf8)
Assert-E2ETest ($journeyDriverText.Contains('input JSON is non-empty and has exactly these seven properties')) 'phase1 prepare input preflight prompt'
Assert-E2ETest ($journeyDriverText.Contains('Never invoke problem_locator_prepare_attachment with {}, null, or omitted input')) 'phase1 empty prepare prohibition prompt'
Assert-E2ETest ($journeyDriverText.Contains('a second empty invocation is forbidden')) 'phase1 repeated empty prepare prohibition prompt'

$restartDriverText = [IO.File]::ReadAllText((Join-Path $root 'harness\restart\windows-restart-lib.ps1'), $utf8)
Assert-E2ETest ($restartDriverText.Contains('$script:RestartExpectedDiscoveredTools')) 'restart discovery tool inventory is explicit'
foreach ($toolName in @(
    'problem_locator_cancel_case', 'problem_locator_create_case', 'problem_locator_get_case',
    'problem_locator_list_artifacts', 'problem_locator_prepare_attachment',
    'problem_locator_resume_case', 'problem_locator_submit_supplement'
)) {
    Assert-E2ETest ($restartDriverText.Contains("mcp__problem-locator__$toolName")) "restart discovery inventory missing $toolName"
}
Assert-E2ETest ($restartDriverText.Contains("@('Skill', `$script:RestartFullGetTool, `$script:RestartFullListTool) -ccontains `$name")) 'restart execution remains restricted to Skill/get/list'
Assert-E2ETest ($restartDriverText.Contains('-TimeoutSeconds ($script:RestartCurlMaxTimeSeconds + 15)')) 'restart curl has an explicit bounded outer timeout'

. (Join-Path $root 'harness\windows-http-capture-lib.ps1')
$argumentsParameter = (Get-Command Invoke-HcCapturedProcess).Parameters['Arguments']
$allowsEmptyArguments = @($argumentsParameter.Attributes | Where-Object { $_ -is [Management.Automation.AllowEmptyStringAttribute] }).Count -eq 1
Assert-E2ETest $allowsEmptyArguments 'HTTP curl process arguments must allow the intentional empty proxy value'
$httpDriverText = [IO.File]::ReadAllText((Join-Path $root 'harness\windows-http-capture-lib.ps1'), $utf8)
Assert-E2ETest ($httpDriverText.Contains("'user_text_event_regression', 'mixed_or_multiple_tool_result_fail_closed'")) 'HTTP restart manifest validator matches the generated fail-closed fields'
$httpProcessRoot = Join-Path ([IO.Path]::GetTempPath()) ("problem-locator-e2e-http-process-" + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($httpProcessRoot) | Out-Null
try {
    $httpProcessStdout = Join-Path $httpProcessRoot 'stdout.txt'
    $httpProcessStderr = Join-Path $httpProcessRoot 'stderr.txt'
    New-HcOutputReservations -EvidenceRoot $httpProcessRoot -Names @('stdout.txt', 'stderr.txt')
    $httpProcessExit = Invoke-HcCapturedProcess `
        -FilePath 'C:\Windows\System32\cmd.exe' `
        -Arguments @('/d', '/c', 'exit', '0', '') `
        -WorkingDirectory $httpProcessRoot `
        -StdoutPath $httpProcessStdout `
        -StderrPath $httpProcessStderr
    Assert-E2ETest ($httpProcessExit -eq 0) 'HTTP captured process accepts and transports an empty argument'
    $canonicalPath = Join-Path $httpProcessRoot 'canonical.json'
    [IO.File]::WriteAllText($canonicalPath, '{"Name":"value","backslash":"\\d","empty":{},"nested":{"items":[]},"quote":"\""}' + "`n", $utf8)
    $canonicalValue = Read-HcCanonicalJson -Path $canonicalPath -Label 'PowerShell 5.1 canonical JSON regression'
    Assert-E2ETest ($null -ne $canonicalValue) 'canonical JSON supports named and empty nested objects'
}
finally {
    if (Test-Path -LiteralPath $httpProcessRoot -PathType Container) {
        [IO.Directory]::Delete($httpProcessRoot, $true)
    }
}

$serviceBudgetFiles = @(
    'harness\gate_native_independent.sh',
    'harness\gate_installed_distribution.sh',
    'harness\verify_service_process.py',
    'harness\setup_claude.sh',
    'harness\run_pre_gates.sh',
    'harness\start_service_supervisor.sh'
)
foreach ($relative in $serviceBudgetFiles) {
    $text = [IO.File]::ReadAllText((Join-Path $root $relative), $utf8)
    Assert-E2ETest ($text.Contains('--max-budget-usd 3.00')) "service budget missing in $relative"
    Assert-E2ETest (-not $text.Contains('--max-budget-usd 1.00')) "stale service budget in $relative"
}

$auditRoot = Join-Path $root 'harness\state-audit'
$auditManifestPath = Join-Path $auditRoot 'template-manifest.sha256'
$auditManifestRecords = @([IO.File]::ReadAllLines($auditManifestPath, $utf8) | ForEach-Object {
    Assert-E2ETest ($_ -cmatch '^([0-9a-f]{64})  ([A-Za-z0-9_.-]+)$') 'state audit manifest line format'
    [PSCustomObject]@{ sha256 = $Matches[1]; name = $Matches[2] }
})
Assert-E2ETest ($auditManifestRecords.Count -eq 2) 'state audit manifest file count'
Assert-E2ETest ((@($auditManifestRecords.name | Sort-Object) -join ',') -ceq 'audit_http_capture.py,audit_state_and_result.py') 'state audit manifest filenames'
foreach ($record in $auditManifestRecords) {
    $auditText = [IO.File]::ReadAllText((Join-Path $auditRoot $record.name), $utf8)
    $normalizedAuditBytes = $utf8.GetBytes(
        $auditText.Replace("`r`n", "`n").Replace("`r", "`n")
    )
    $auditHasher = [Security.Cryptography.SHA256]::Create()
    try {
        $actualAuditHash = [BitConverter]::ToString(
            $auditHasher.ComputeHash($normalizedAuditBytes)
        ).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $auditHasher.Dispose()
    }
    Assert-E2ETest ($actualAuditHash -ceq $record.sha256) "state audit manifest SHA-256: $($record.name)"
}
$stateAuditText = [IO.File]::ReadAllText((Join-Path $auditRoot 'audit_state_and_result.py'), $utf8)
Assert-E2ETest ($stateAuditText.Contains('expected_candidate_evidence_ids')) 'state audit accepts only derived candidate evidence IDs'
Assert-E2ETest ($stateAuditText.Contains('expected_parse_evidence_ids | expected_candidate_evidence_ids')) 'state audit final evidence union'
Assert-E2ETest (-not $stateAuditText.Contains('"CANDIDATE_NEW_EVIDENCE"')) 'state audit stale zero-candidate-evidence assumption'

. (Join-Path $root 'harness\windows-journey-lib.ps1')

function New-PrepareRecord {
    param([int]$Ordinal, [bool]$Ok, [string]$ErrorCode = 'VALIDATION_ERROR', $InputValue = $null)
    $result = if ($Ok) {
        [PSCustomObject][ordered]@{ ok = $true; data = [PSCustomObject]@{}; error = $null }
    }
    else {
        [PSCustomObject][ordered]@{
            ok = $false
            data = $null
            error = [PSCustomObject][ordered]@{ code = $ErrorCode; message = 'validation failed' }
        }
    }
    return [PSCustomObject][ordered]@{
        ordinal = $Ordinal
        tool_name = 'problem_locator_prepare_attachment'
        input = $InputValue
        result = $result
    }
}

$single = Resolve-JourneyPhase1PrepareAttempts -Records @((New-PrepareRecord 1 $true))
Assert-E2ETest ($single.successful.ordinal -eq 1) 'single prepare selection'
Assert-E2ETest (@($single.corrections).Count -eq 0) 'single prepare correction count'

$corrected = Resolve-JourneyPhase1PrepareAttempts -Records @(
    (New-PrepareRecord 6 $false),
    (New-PrepareRecord 7 $true)
)
Assert-E2ETest ($corrected.successful.ordinal -eq 7) 'corrected prepare selection'
Assert-E2ETest (@($corrected.corrections).Count -eq 1) 'corrected prepare correction count'
Assert-E2ETest ($corrected.corrections[0].failed_ordinal -eq 6) 'corrected failure ordinal'

Assert-E2EThrows {
    [void](Resolve-JourneyPhase1PrepareAttempts -Records @(
        (New-PrepareRecord 1 $false),
        (New-PrepareRecord 2 $false),
        (New-PrepareRecord 3 $true)
    ))
} 'at most one recoverable'

Assert-E2EThrows {
    [void](Resolve-JourneyPhase1PrepareAttempts -Records @(
        (New-PrepareRecord 1 $false 'CONFLICT'),
        (New-PrepareRecord 2 $true)
    ))
} 'only VALIDATION_ERROR'

Assert-E2EThrows {
    [void](Resolve-JourneyPhase1PrepareAttempts -Records @(
        (New-PrepareRecord 1 $false 'VALIDATION_ERROR' ([PSCustomObject]@{ case_id = 'not-empty' })),
        (New-PrepareRecord 2 $true)
    ))
} 'must have empty input'

$shortProcessRoot = Join-Path ([IO.Path]::GetTempPath()) ("problem-locator-e2e-short-process-" + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($shortProcessRoot) | Out-Null
try {
    for ($index = 0; $index -lt 32; $index++) {
        $shortResult = Invoke-E2EBoundedProcess `
            -FilePath 'C:\Windows\System32\cmd.exe' `
            -ArgumentLine '/d /c exit 0' `
            -WorkingDirectory $shortProcessRoot `
            -StdoutPath (Join-Path $shortProcessRoot "stdout-$index.txt") `
            -StderrPath (Join-Path $shortProcessRoot "stderr-$index.txt") `
            -TimeoutSeconds 5
        Assert-E2ETest ($shortResult.exit_code -eq 0) "short process exit code $index"
        Assert-E2ETest ($shortResult.job_assigned) "short process Job assignment $index"
    }
}
finally {
    if (Test-Path -LiteralPath $shortProcessRoot -PathType Container) {
        [IO.Directory]::Delete($shortProcessRoot, $true)
    }
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("problem-locator-e2e-timeout-" + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($testRoot) | Out-Null
try {
    $pidPath = Join-Path $testRoot 'pids.txt'
    $stdoutPath = Join-Path $testRoot 'stdout.txt'
    $stderrPath = Join-Path $testRoot 'stderr.txt'
    $receiptPath = Join-Path $testRoot 'timeout.json'
    $childScriptPath = Join-Path $testRoot 'child.ps1'
    $child = @'
param([Parameter(Mandatory = $true)][string]$PidPath)
$ErrorActionPreference = 'Stop'
[IO.File]::WriteAllText($PidPath, "$PID", [Text.Encoding]::ASCII)
while ($true) { Start-Sleep -Seconds 1 }
'@
    [IO.File]::WriteAllText($childScriptPath, $child, $utf8)
    $quotedChildPath = '"' + $childScriptPath.Replace('"', '\"') + '"'
    $quotedPidPath = '"' + $pidPath.Replace('"', '\"') + '"'
    Assert-E2EThrows {
        $boundedArguments = @{
            FilePath = Join-Path $PSHOME 'powershell.exe'
            ArgumentLine = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File $quotedChildPath -PidPath $quotedPidPath"
            WorkingDirectory = $testRoot
            StdoutPath = $stdoutPath
            StderrPath = $stderrPath
            TimeoutSeconds = 1
            TimeoutReceiptPath = $receiptPath
        }
        [void](Invoke-E2EBoundedProcess @boundedArguments)
    } 'E2E_PROCESS_TIMEOUT'
    Assert-E2ETest (Test-Path -LiteralPath $receiptPath -PathType Leaf) 'timeout receipt'
    $receipt = [IO.File]::ReadAllText($receiptPath, $utf8) | ConvertFrom-Json
    Assert-E2ETest ($receipt.result -ceq 'TIMEOUT') 'timeout receipt result'
    Assert-E2ETest (-not $receipt.arguments_recorded) 'timeout receipt excludes arguments'
    $pids = @([IO.File]::ReadAllText($pidPath, [Text.Encoding]::ASCII))
    foreach ($processId in $pids) {
        Assert-E2ETest ($null -eq (Get-Process -Id ([int]$processId) -ErrorAction SilentlyContinue)) "timed-out process remains: $processId"
    }
}
finally {
    if (Test-Path -LiteralPath $testRoot -PathType Container) {
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            try {
                [IO.Directory]::Delete($testRoot, $true)
                break
            }
            catch [IO.IOException] {
                if ($attempt -eq 10) { throw }
                Start-Sleep -Milliseconds 200
            }
        }
    }
}

Write-Output 'E2E_HARNESS_TESTS_PASSED'
