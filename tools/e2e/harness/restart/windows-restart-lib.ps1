Set-StrictMode -Version Latest

if (-not (Get-Command Invoke-E2EBoundedProcess -ErrorAction SilentlyContinue)) {
    $boundedProcessPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'bounded-process.ps1'
    . $boundedProcessPath
}

$script:RestartRepoRoot = 'D:\code\xiaodao'
$script:RestartClaudeExe = 'C:\Users\admin\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe'
$script:RestartClaudeVersion = '2.1.150'
$script:RestartModelAlias = 'haiku'
$script:RestartEffectiveModel = 'deepseek-v4-flash[1m]'
$script:RestartMcpUrl = 'http://127.0.0.1:18000/mcp'
$script:RestartServiceBaseUrl = 'http://127.0.0.1:18000'
$script:RestartClientSkillSha256 = '6caca2c58e3678b3857d39f728e40d765a121ef0ea152381852687d5e3e3583f'
$script:RestartSkillId = 'diagnosis-skill/diagnose-service-takeover'
$script:RestartSkillVersion = '3.0.5'
$script:RestartSkillHash = 'ae47a1a63e6cf4849f83b0f9d49db608c1e93ebe1713f21d58c910990b0857a4'
$script:RestartGetTool = 'problem_locator_get_case'
$script:RestartListTool = 'problem_locator_list_artifacts'
$script:RestartFullGetTool = "mcp__problem-locator__$($script:RestartGetTool)"
$script:RestartFullListTool = "mcp__problem-locator__$($script:RestartListTool)"
$script:RestartExpectedDiscoveredTools = @(
    'Skill',
    'mcp__problem-locator__problem_locator_cancel_case',
    'mcp__problem-locator__problem_locator_create_case',
    'mcp__problem-locator__problem_locator_get_case',
    'mcp__problem-locator__problem_locator_list_artifacts',
    'mcp__problem-locator__problem_locator_prepare_attachment',
    'mcp__problem-locator__problem_locator_resume_case',
    'mcp__problem-locator__problem_locator_submit_supplement'
)
$script:RestartUuidPattern = '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
$script:RestartSha256Pattern = '^[0-9a-f]{64}$'
$script:RestartAttemptPattern = '^attempt[0-9]+-[0-9]{8}-[0-9]{6}$'
$script:RestartUtf8 = New-Object System.Text.UTF8Encoding($false)
$script:RestartReservedOutputs = @{}
$script:RestartCompletedOutputs = @{}
$script:RestartCurlExe = 'C:\Windows\System32\curl.exe'
$script:RestartCurlConnectTimeoutSeconds = 10
$script:RestartCurlMaxTimeSeconds = 120
$script:RestartClaudeVersionTimeoutSeconds = 20
$script:RestartClaudeQueryTimeoutSeconds = 120

function Assert-Restart {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw "restart assertion failed: $Message"
    }
}

function Test-RestartProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    return $null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]
}

function Get-RestartProperty {
    param(
        $Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [switch]$Required
    )
    if (-not (Test-RestartProperty $Object $Name)) {
        if ($Required) {
            throw "restart assertion failed: required JSON property '$Name' is absent"
        }
        return $null
    }
    $value = $Object.PSObject.Properties[$Name].Value
    if ($value -is [System.Array]) {
        return ,$value
    }
    return $value
}

function Get-RestartStringProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    $value = Get-RestartProperty $Object $Name -Required
    Assert-Restart ($value -is [string]) "$Name must be a JSON string"
    return $value
}

function Get-RestartBooleanProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    $value = Get-RestartProperty $Object $Name -Required
    Assert-Restart ($value -is [bool]) "$Name must be a JSON boolean"
    return $value
}

function Get-RestartIntegerProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    $value = Get-RestartProperty $Object $Name -Required
    $integerTypes = @([byte], [sbyte], [int16], [uint16], [int32], [uint32], [int64], [uint64])
    Assert-Restart ($null -ne $value -and $integerTypes -contains $value.GetType()) "$Name must be a JSON integer"
    return [int64]$value
}

function Assert-RestartJsonObject {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Restart ($Value -is [System.Management.Automation.PSCustomObject]) "$Label must be a JSON object"
}

function Assert-RestartJsonArray {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Restart ($Value -is [System.Array]) "$Label must be a JSON array"
}

function Assert-RestartStringArray {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-RestartJsonArray $Value $Label
    foreach ($item in @($Value)) {
        Assert-Restart ($item -is [string]) "$Label entries must be strings"
    }
}

function Assert-RestartExactStrings {
    param($Actual, [string[]]$Expected, [Parameter(Mandatory = $true)][string]$Label)
    $actualArray = @($Actual)
    Assert-Restart ($actualArray.Count -eq $Expected.Count) "$Label count"
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        Assert-Restart ([string]$actualArray[$index] -ceq $Expected[$index]) "$Label[$index]"
    }
}

function Assert-RestartExactProperties {
    param($Object, [string[]]$Expected, [Parameter(Mandatory = $true)][string]$Label)
    Assert-RestartJsonObject $Object $Label
    Assert-RestartExactStrings @($Object.PSObject.Properties.Name | Sort-Object) @($Expected | Sort-Object) "$Label properties"
}

function Assert-RestartUuid {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Restart ($Value -is [string] -and $Value -cmatch $script:RestartUuidPattern) "$Label must be a lowercase UUID"
}

function Assert-RestartSha256 {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Restart ($Value -is [string] -and $Value -cmatch $script:RestartSha256Pattern) "$Label must be a lowercase SHA-256"
}

function Read-RestartJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-Restart (Test-Path -LiteralPath $Path -PathType Leaf) "required JSON file is absent: $Path"
    try {
        return [System.IO.File]::ReadAllText($Path, $script:RestartUtf8) | ConvertFrom-Json
    }
    catch {
        throw "restart assertion failed: invalid JSON file $Path"
    }
}

function Assert-RestartJsonEquivalent {
    param($Actual, $Expected, [Parameter(Mandatory = $true)][string]$Label)
    if ($null -eq $Actual -or $null -eq $Expected) {
        Assert-Restart ($null -eq $Actual -and $null -eq $Expected) "$Label null mismatch"
        return
    }
    $actualIsObject = $Actual -is [System.Management.Automation.PSCustomObject]
    $expectedIsObject = $Expected -is [System.Management.Automation.PSCustomObject]
    if ($actualIsObject -or $expectedIsObject) {
        Assert-Restart ($actualIsObject -and $expectedIsObject) "$Label object type mismatch"
        $actualNames = @($Actual.PSObject.Properties.Name | Sort-Object)
        $expectedNames = @($Expected.PSObject.Properties.Name | Sort-Object)
        Assert-RestartExactStrings $actualNames $expectedNames "$Label property names"
        foreach ($name in $expectedNames) {
            Assert-RestartJsonEquivalent (Get-RestartProperty $Actual $name -Required) (Get-RestartProperty $Expected $name -Required) "$Label.$name"
        }
        return
    }
    $actualIsArray = $Actual -is [System.Array]
    $expectedIsArray = $Expected -is [System.Array]
    if ($actualIsArray -or $expectedIsArray) {
        Assert-Restart ($actualIsArray -and $expectedIsArray) "$Label array type mismatch"
        $actualItems = @($Actual)
        $expectedItems = @($Expected)
        Assert-Restart ($actualItems.Count -eq $expectedItems.Count) "$Label array count"
        for ($index = 0; $index -lt $expectedItems.Count; $index++) {
            Assert-RestartJsonEquivalent $actualItems[$index] $expectedItems[$index] "$Label[$index]"
        }
        return
    }
    if ($Actual -is [string] -or $Expected -is [string]) {
        Assert-Restart ($Actual -is [string] -and $Expected -is [string] -and $Actual -ceq $Expected) "$Label string mismatch"
        return
    }
    Assert-Restart ($Actual -eq $Expected -and $Actual.GetType() -eq $Expected.GetType()) "$Label scalar mismatch"
}

function Get-RestartQueryOutputNames {
    return @(
        'windows-restart-claude-version.stdout.txt',
        'windows-restart-claude-version.stderr.txt',
        'restart.prompt.txt',
        'restart.stream-json.stdout.ndjson',
        'restart.stderr.txt',
        'restart.authoritative.json',
        'restart-authoritative-summary.json'
    )
}

function Get-RestartDownloadOutputNames {
    return @(
        'restart-download.curl.stdout.txt',
        'restart-download.curl.stderr.txt',
        'restart-download.response.headers.txt',
        'restart-diagnosis-result.json',
        'restart-archive-download.curl.stdout.txt',
        'restart-archive-download.curl.stderr.txt',
        'restart-archive-download.response.headers.txt',
        'restart-result.zip',
        'restart-download-verification.json'
    )
}

function Get-RestartAllOutputNames {
    return @((Get-RestartQueryOutputNames) + (Get-RestartDownloadOutputNames))
}

function New-RestartOutputReservations {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot, [Parameter(Mandatory = $true)][string[]]$Names)
    $root = [System.IO.Path]::GetFullPath($EvidenceRoot).TrimEnd('\') + '\'
    Assert-Restart (@($Names | Sort-Object -Unique).Count -eq $Names.Count) 'planned restart output names must be unique'
    $paths = @()
    foreach ($name in $Names) {
        Assert-Restart ([System.IO.Path]::GetFileName($name) -ceq $name) "planned output must be a plain filename: $name"
        $path = [System.IO.Path]::GetFullPath((Join-Path $EvidenceRoot $name))
        Assert-Restart ($path.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) "planned output escaped evidence root: $name"
        Assert-Restart (-not (Test-Path -LiteralPath $path)) "planned output already exists: $path"
        $paths += $path
    }
    foreach ($path in $paths) {
        $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $stream.Dispose()
        $key = $path.ToLowerInvariant()
        $script:RestartReservedOutputs[$key] = $true
        $script:RestartCompletedOutputs[$key] = $false
    }
}

function Assert-RestartReservedUnused {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    $key = $full.ToLowerInvariant()
    Assert-Restart ($script:RestartReservedOutputs.ContainsKey($key)) "output was not atomically reserved: $full"
    Assert-Restart (-not [bool]$script:RestartCompletedOutputs[$key]) "output reservation already consumed: $full"
}

function Complete-RestartExternalOutput {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-RestartReservedUnused $Path
    Assert-Restart (Test-Path -LiteralPath $Path -PathType Leaf) "external output is absent: $Path"
    $script:RestartCompletedOutputs[[System.IO.Path]::GetFullPath($Path).ToLowerInvariant()] = $true
}

function Write-RestartUtf8 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    Assert-RestartReservedUnused $Path
    $bytes = $script:RestartUtf8.GetBytes($Text)
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Truncate, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    $script:RestartCompletedOutputs[[System.IO.Path]::GetFullPath($Path).ToLowerInvariant()] = $true
}

function Write-RestartJson {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)]$Value)
    Write-RestartUtf8 -Path $Path -Text (($Value | ConvertTo-Json -Depth 100) + "`n")
}

function Get-RestartAttemptLabel {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $leaf = Split-Path -Leaf ([System.IO.Path]::GetFullPath($EvidenceRoot))
    Assert-Restart ($leaf -cmatch $script:RestartAttemptPattern) 'evidence directory must use a clean attempt name'
    return ($leaf -split '-', 2)[0]
}

function Confirm-RestartManifestFiles {
    param($Files, [string[]]$ExpectedNames, [string]$Root, [string]$Label)
    Assert-RestartJsonArray $Files "$Label files"
    $actualNames = @($Files | ForEach-Object { Get-RestartStringProperty $_ 'name' } | Sort-Object)
    Assert-RestartExactStrings $actualNames @($ExpectedNames | Sort-Object) "$Label filenames"
    foreach ($record in @($Files)) {
        Assert-RestartExactProperties $record @('name', 'size', 'sha256') "$Label file record"
        $name = Get-RestartStringProperty $record 'name'
        Assert-Restart ([System.IO.Path]::GetFileName($name) -ceq $name) "$Label manifest filename"
        $path = Join-Path $Root $name
        Assert-Restart (Test-Path -LiteralPath $path -PathType Leaf) "$Label file absent: $name"
        $item = Get-Item -LiteralPath $path
        Assert-Restart ((Get-RestartIntegerProperty $record 'size') -eq $item.Length) "$Label size mismatch: $name"
        $expectedHash = Get-RestartStringProperty $record 'sha256'
        Assert-RestartSha256 $expectedHash "$Label SHA-256: $name"
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        Assert-Restart ($actualHash -ceq $expectedHash) "$Label SHA-256 mismatch: $name"
    }
}

function Confirm-RestartDriverManifest {
    param([Parameter(Mandatory = $true)][string]$DriverRoot)
    $manifest = Read-RestartJson (Join-Path $DriverRoot 'windows-restart-driver-manifest.json')
    Assert-RestartExactProperties $manifest @('schema_version', 'static_check', 'network_or_claude_invoked', 'reads_or_copies_secret_settings', 'inline_strict_mcp', 'claude_business_tools', 'first_tool', 'authoritative_source', 'user_text_event_regression', 'mixed_or_multiple_tool_result_fail_closed', 'all_runtime_outputs_create_new', 'possible_runtime_outputs', 'files') 'restart driver manifest'
    Assert-Restart ((Get-RestartIntegerProperty $manifest 'schema_version') -eq 1) 'restart manifest schema_version'
    Assert-Restart ((Get-RestartStringProperty $manifest 'static_check') -ceq 'passed') 'restart manifest static_check'
    Assert-Restart (-not (Get-RestartBooleanProperty $manifest 'network_or_claude_invoked')) 'restart static check must be offline'
    Assert-Restart (-not (Get-RestartBooleanProperty $manifest 'reads_or_copies_secret_settings')) 'restart driver must not access settings secrets'
    Assert-Restart (Get-RestartBooleanProperty $manifest 'inline_strict_mcp') 'restart manifest inline strict MCP'
    Assert-Restart (Get-RestartBooleanProperty $manifest 'all_runtime_outputs_create_new') 'restart manifest CreateNew outputs'
    Assert-Restart ((Get-RestartStringProperty $manifest 'first_tool') -ceq 'Skill(problem-locator-client)') 'restart first tool declaration'
    Assert-Restart ((Get-RestartStringProperty $manifest 'authoritative_source') -ceq 'uniquely correlated stream-json tool_use/tool_result structuredContent only') 'restart authoritative source declaration'
    Assert-Restart ((Get-RestartStringProperty $manifest 'user_text_event_regression') -ceq 'passed') 'restart user text regression'
    Assert-Restart (Get-RestartBooleanProperty $manifest 'mixed_or_multiple_tool_result_fail_closed') 'restart mixed/multiple tool_result fail closed'
    $tools = Get-RestartProperty $manifest 'claude_business_tools' -Required
    Assert-RestartStringArray $tools 'restart manifest business tools'
    Assert-RestartExactStrings $tools @($script:RestartGetTool, $script:RestartListTool) 'restart manifest business tools'
    $outputs = Get-RestartProperty $manifest 'possible_runtime_outputs' -Required
    Assert-RestartStringArray $outputs 'restart manifest outputs'
    Assert-RestartExactStrings @($outputs | Sort-Object) @(Get-RestartAllOutputNames | Sort-Object) 'restart manifest outputs'
    Confirm-RestartManifestFiles (Get-RestartProperty $manifest 'files' -Required) @('README-restart.md', 'download-windows-restart-artifact.ps1', 'run-windows-restart-verify.ps1', 'static-check-restart.ps1', 'windows-restart-lib.ps1') $DriverRoot 'restart driver manifest'
}

function Confirm-PreRestartJourneyManifest {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $manifest = Read-RestartJson (Join-Path $EvidenceRoot 'windows-journey-driver-manifest.json')
    Assert-RestartExactProperties $manifest @('schema_version', 'static_check', 'network_or_claude_invoked', 'reads_or_copies_secret_settings', 'inline_strict_mcp', 'stdout_stderr_separated', 'authoritative_source', 'user_text_event_regression', 'mixed_or_multiple_tool_result_fail_closed', 'possible_runtime_outputs', 'files') 'pre-restart journey manifest'
    Assert-Restart ((Get-RestartIntegerProperty $manifest 'schema_version') -eq 1) 'journey manifest schema_version'
    Assert-Restart ((Get-RestartStringProperty $manifest 'static_check') -ceq 'passed') 'journey manifest static_check'
    Assert-Restart (-not (Get-RestartBooleanProperty $manifest 'network_or_claude_invoked')) 'journey static check must be offline'
    Assert-Restart (-not (Get-RestartBooleanProperty $manifest 'reads_or_copies_secret_settings')) 'journey driver must not access settings secrets'
    Assert-Restart (Get-RestartBooleanProperty $manifest 'inline_strict_mcp') 'journey manifest inline strict MCP'
    Assert-Restart (Get-RestartBooleanProperty $manifest 'stdout_stderr_separated') 'journey manifest output separation'
    Assert-Restart ((Get-RestartStringProperty $manifest 'authoritative_source') -ceq 'stream-json tool_use/tool_result pairs only') 'journey authoritative source declaration'
    Assert-Restart ((Get-RestartStringProperty $manifest 'user_text_event_regression') -ceq 'passed') 'journey user text regression'
    Assert-Restart (Get-RestartBooleanProperty $manifest 'mixed_or_multiple_tool_result_fail_closed') 'journey mixed/multiple tool_result fail closed'
    $expectedOutputs = @(
        'windows-claude-version.stdout.txt', 'windows-claude-version.stderr.txt',
        'phase1.prompt.txt', 'phase1.stream-json.stdout.ndjson', 'phase1.stderr.txt', 'phase1.client-dfx.jsonl', 'phase1.authoritative.json', 'phase1-state.json',
        'upload.curl.stdout.txt', 'upload.curl.stderr.txt', 'upload.response.json', 'upload.response.headers.txt', 'upload-state.json',
        'hook-failure.prompt.txt', 'hook-failure.stream-json.stdout.ndjson', 'hook-failure.stderr.txt', 'hook-failure.claude-debug.log', 'hook-failure.authoritative.json',
        'phase3.prompt.txt', 'phase3.stream-json.stdout.ndjson', 'phase3.stderr.txt', 'phase3.client-dfx.jsonl', 'phase3.authoritative.json', 'journey-authoritative-summary.json'
    )
    $outputs = Get-RestartProperty $manifest 'possible_runtime_outputs' -Required
    Assert-RestartStringArray $outputs 'journey manifest outputs'
    Assert-RestartExactStrings @($outputs | Sort-Object) @($expectedOutputs | Sort-Object) 'journey manifest outputs'
    Confirm-RestartManifestFiles (Get-RestartProperty $manifest 'files' -Required) @('README.md', 'run-windows-journey.ps1', 'static-check.ps1', 'windows-journey-lib.ps1') $EvidenceRoot 'pre-restart journey manifest'
}

function Confirm-RestartClientSkill {
    $path = Join-Path $script:RestartRepoRoot '.claude\skills\problem-locator-client\SKILL.md'
    Assert-Restart (Test-Path -LiteralPath $path -PathType Leaf) 'problem-locator-client Skill is absent'
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-Restart ($actual -ceq $script:RestartClientSkillSha256) 'problem-locator-client Skill SHA-256'
}

function Assert-RestartSelectedSkill {
    param($Skill, [Parameter(Mandatory = $true)][string]$Label)
    Assert-RestartExactProperties $Skill @('id', 'version', 'content_hash') $Label
    Assert-Restart ((Get-RestartStringProperty $Skill 'id') -ceq $script:RestartSkillId) "$Label id"
    Assert-Restart ((Get-RestartStringProperty $Skill 'version') -ceq $script:RestartSkillVersion) "$Label version"
    Assert-Restart ((Get-RestartStringProperty $Skill 'content_hash') -ceq $script:RestartSkillHash) "$Label product hash"
}

function Assert-RestartFinalResult {
    param($FinalResult, [Parameter(Mandatory = $true)][string]$Label)
    Assert-RestartExactProperties $FinalResult @('conclusion_id', 'revision', 'content_hash', 'statement', 'supporting_evidence_refs', 'completion_criteria_mapping', 'proposed_by_job_id', 'status') $Label
    Assert-RestartUuid (Get-RestartStringProperty $FinalResult 'conclusion_id') "$Label conclusion_id"
    Assert-Restart ((Get-RestartIntegerProperty $FinalResult 'revision') -gt 0) "$Label revision"
    Assert-RestartSha256 (Get-RestartStringProperty $FinalResult 'content_hash') "$Label content_hash"
    Assert-Restart (-not [string]::IsNullOrWhiteSpace((Get-RestartStringProperty $FinalResult 'statement'))) "$Label statement"
    Assert-RestartUuid (Get-RestartStringProperty $FinalResult 'proposed_by_job_id') "$Label proposed_by_job_id"
    Assert-Restart ((Get-RestartStringProperty $FinalResult 'status') -ceq 'ACCEPTED') "$Label status"
    $supporting = Get-RestartProperty $FinalResult 'supporting_evidence_refs' -Required
    Assert-RestartStringArray $supporting "$Label supporting evidence"
    Assert-Restart (@($supporting).Count -gt 0) "$Label supporting evidence nonempty"
    foreach ($id in @($supporting)) { Assert-RestartUuid $id "$Label supporting evidence ID" }
    $mappings = Get-RestartProperty $FinalResult 'completion_criteria_mapping' -Required
    Assert-RestartJsonArray $mappings "$Label completion mappings"
    Assert-Restart (@($mappings).Count -gt 0) "$Label completion mappings nonempty"
    $index = 0
    foreach ($mapping in @($mappings)) {
        Assert-RestartExactProperties $mapping @('criterion_index', 'criterion', 'satisfied', 'evidence_refs', 'explanation') "$Label mapping"
        Assert-Restart ((Get-RestartIntegerProperty $mapping 'criterion_index') -eq $index) "$Label mapping index"
        Assert-Restart (-not [string]::IsNullOrWhiteSpace((Get-RestartStringProperty $mapping 'criterion'))) "$Label mapping criterion"
        Assert-Restart (Get-RestartBooleanProperty $mapping 'satisfied') "$Label mapping satisfied"
        Assert-Restart (-not [string]::IsNullOrWhiteSpace((Get-RestartStringProperty $mapping 'explanation'))) "$Label mapping explanation"
        $refs = Get-RestartProperty $mapping 'evidence_refs' -Required
        Assert-RestartStringArray $refs "$Label mapping evidence"
        Assert-Restart (@($refs).Count -gt 0) "$Label mapping evidence nonempty"
        foreach ($id in @($refs)) { Assert-RestartUuid $id "$Label mapping evidence ID" }
        $index++
    }
}

function Assert-RestartArtifactView {
    param(
        $Artifact,
        [Parameter(Mandatory = $true)][string]$CaseId,
        [Parameter(Mandatory = $true)][string]$ExpectedName,
        [Parameter(Mandatory = $true)][string]$ExpectedContentType,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-RestartExactProperties $Artifact @('artifact_id', 'name', 'content_type', 'size', 'sha256', 'created_at', 'download_url') $Label
    $artifactId = Get-RestartStringProperty $Artifact 'artifact_id'
    Assert-RestartUuid $artifactId "$Label artifact_id"
    Assert-Restart ((Get-RestartStringProperty $Artifact 'name') -ceq $ExpectedName) "$Label name"
    Assert-Restart ((Get-RestartStringProperty $Artifact 'content_type') -ceq $ExpectedContentType) "$Label content_type"
    Assert-Restart ((Get-RestartIntegerProperty $Artifact 'size') -gt 0) "$Label size"
    Assert-RestartSha256 (Get-RestartStringProperty $Artifact 'sha256') "$Label SHA-256"
    Assert-Restart (-not [string]::IsNullOrWhiteSpace((Get-RestartStringProperty $Artifact 'created_at'))) "$Label created_at"
    $expectedUrl = "$($script:RestartServiceBaseUrl)/api/v1/artifacts/$artifactId/content?case_id=$CaseId"
    Assert-Restart ((Get-RestartStringProperty $Artifact 'download_url') -ceq $expectedUrl) "$Label loopback download URL"
}

function Read-PreRestartSummaryValidated {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $path = Join-Path $EvidenceRoot 'journey-authoritative-summary.json'
    $summary = Read-RestartJson $path
    Assert-RestartExactProperties $summary @('schema_version', 'attempt', 'case_id', 'attachment_id', 'resolved_case_revision', 'diagnosis_state_revision', 'selected_skill_ref', 'final_result', 'observed_statuses', 'public_artifact', 'public_result_archive', 'request_ids', 'phase3_mcp_call_count', 'validation_corrections') 'pre-restart authoritative summary'
    Assert-Restart ((Get-RestartIntegerProperty $summary 'schema_version') -eq 1) 'pre-restart summary schema_version'
    Assert-Restart ((Get-RestartStringProperty $summary 'attempt') -ceq (Get-RestartAttemptLabel $EvidenceRoot)) 'pre-restart summary attempt'
    $caseId = Get-RestartStringProperty $summary 'case_id'
    Assert-RestartUuid $caseId 'pre-restart case_id'
    Assert-RestartUuid (Get-RestartStringProperty $summary 'attachment_id') 'pre-restart attachment_id'
    Assert-Restart ((Get-RestartIntegerProperty $summary 'resolved_case_revision') -gt 0) 'pre-restart case revision'
    Assert-Restart ((Get-RestartIntegerProperty $summary 'diagnosis_state_revision') -gt 0) 'pre-restart diagnosis revision'
    Assert-RestartSelectedSkill (Get-RestartProperty $summary 'selected_skill_ref' -Required) 'pre-restart selected Skill'
    Assert-RestartFinalResult (Get-RestartProperty $summary 'final_result' -Required) 'pre-restart final result'
    Assert-RestartArtifactView (Get-RestartProperty $summary 'public_artifact' -Required) $caseId 'diagnosis-result.json' 'application/json' 'pre-restart public result artifact'
    Assert-RestartArtifactView (Get-RestartProperty $summary 'public_result_archive' -Required) $caseId 'result.zip' 'application/zip' 'pre-restart public archive artifact'
    $statuses = Get-RestartProperty $summary 'observed_statuses' -Required
    Assert-RestartStringArray $statuses 'pre-restart observed statuses'
    $statusArray = @($statuses)
    Assert-Restart ($statusArray.Count -gt 1) 'pre-restart observed statuses count'
    Assert-Restart ($statusArray -ccontains 'REVIEWING') 'pre-restart must have observed REVIEWING'
    Assert-Restart ($statusArray[-1] -ceq 'RESOLVED') 'pre-restart final observed status'
    $ids = Get-RestartProperty $summary 'request_ids' -Required
    Assert-RestartExactProperties $ids @('create', 'submit_a', 'prepare', 'submit_attachment', 'submit_order') 'pre-restart request IDs'
    $label = Get-RestartAttemptLabel $EvidenceRoot
    Assert-Restart ((Get-RestartStringProperty $ids 'create') -ceq "$label-windows-create-v1") 'pre-restart create request ID'
    Assert-Restart ((Get-RestartStringProperty $ids 'submit_a') -ceq "$label-windows-submit-a-v1") 'pre-restart submit A request ID'
    Assert-Restart ((Get-RestartStringProperty $ids 'prepare') -ceq "$label-windows-prepare-log-v1") 'pre-restart prepare request ID'
    Assert-Restart ((Get-RestartStringProperty $ids 'submit_attachment') -ceq "$label-windows-submit-attachment-v1") 'pre-restart attachment request ID'
    Assert-Restart ((Get-RestartStringProperty $ids 'submit_order') -ceq "$label-windows-submit-order-v1") 'pre-restart order request ID'
    Assert-Restart ((Get-RestartIntegerProperty $summary 'phase3_mcp_call_count') -gt 0) 'pre-restart phase3 MCP count'
    return $summary
}

function ConvertTo-RestartWindowsArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    if ($Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $backslashes++; continue }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) { [void]$builder.Append(('\' * $backslashes)); $backslashes = 0 }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) { [void]$builder.Append(('\' * ($backslashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-RestartCapturedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath,
        [Parameter(Mandatory = $true)][ValidateRange(1, 3600)][int]$TimeoutSeconds
    )
    Assert-Restart (Test-Path -LiteralPath $FilePath -PathType Leaf) "executable is absent: $FilePath"
    Assert-Restart (Test-Path -LiteralPath $WorkingDirectory -PathType Container) 'working directory is absent'
    Assert-RestartReservedUnused $StdoutPath
    Assert-RestartReservedUnused $StderrPath
    $argumentLine = (($Arguments | ForEach-Object { ConvertTo-RestartWindowsArgument ([string]$_) }) -join ' ')
    $result = Invoke-E2EBoundedProcess -FilePath $FilePath -ArgumentLine $argumentLine -WorkingDirectory $WorkingDirectory -StdoutPath $StdoutPath -StderrPath $StderrPath -TimeoutSeconds $TimeoutSeconds -TimeoutReceiptPath "$StdoutPath.timeout.json"
    return $result.exit_code
}

function Get-RestartMcpConfigJson {
    $config = [ordered]@{
        mcpServers = [ordered]@{
            'problem-locator' = [ordered]@{
                type = 'http'
                url = $script:RestartMcpUrl
                alwaysLoad = $true
            }
        }
    }
    return ($config | ConvertTo-Json -Compress -Depth 10)
}

function Get-RestartClaudeArguments {
    param([Parameter(Mandatory = $true)][string]$Prompt)
    return @(
        '--print',
        '--input-format', 'text',
        '--output-format', 'stream-json',
        '--verbose',
        '--model', 'haiku',
        '--max-turns', '6',
        '--setting-sources', 'user,project',
        '--mcp-config', (Get-RestartMcpConfigJson),
        '--strict-mcp-config',
        '--tools=Skill',
        '--allowedTools',
        'Skill(problem-locator-client)',
        $script:RestartFullGetTool,
        $script:RestartFullListTool,
        '--permission-mode', 'dontAsk',
        '--no-chrome',
        '--no-session-persistence',
        $Prompt
    )
}

function Confirm-RestartClaudeVersion {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $stdout = Join-Path $EvidenceRoot 'windows-restart-claude-version.stdout.txt'
    $stderr = Join-Path $EvidenceRoot 'windows-restart-claude-version.stderr.txt'
    $exitCode = Invoke-RestartCapturedProcess -FilePath $script:RestartClaudeExe -Arguments @('--version') -WorkingDirectory $script:RestartRepoRoot -StdoutPath $stdout -StderrPath $stderr -TimeoutSeconds $script:RestartClaudeVersionTimeoutSeconds
    Assert-Restart ($exitCode -eq 0) 'Windows Claude --version exit code'
    $versionText = [System.IO.File]::ReadAllText($stdout, $script:RestartUtf8).Trim()
    Assert-Restart ($versionText -ceq "$($script:RestartClaudeVersion) (Claude Code)") 'Windows Claude must be exactly 2.1.150'
}

function Get-RestartToolResultPayload {
    param($Event, [Parameter(Mandatory = $true)][string]$ExpectedToolUseId)
    Assert-Restart (Test-RestartProperty $Event 'tool_use_result') 'user tool_result event must carry top-level tool_use_result'
    Assert-Restart (-not (Test-RestartProperty $Event 'toolUseResult')) 'camel-case toolUseResult fallback is forbidden'
    $raw = Get-RestartProperty $Event 'tool_use_result' -Required
    Assert-RestartJsonObject $raw 'top-level tool_use_result'
    if (Test-RestartProperty $raw 'tool_use_id') {
        Assert-Restart ((Get-RestartStringProperty $raw 'tool_use_id') -ceq $ExpectedToolUseId) 'tool_use_result.tool_use_id mismatch'
    }
    if (Test-RestartProperty $raw 'toolUseId') {
        Assert-Restart ((Get-RestartStringProperty $raw 'toolUseId') -ceq $ExpectedToolUseId) 'tool_use_result.toolUseId mismatch'
    }
    if (Test-RestartProperty $raw 'isError') { Assert-Restart (-not (Get-RestartBooleanProperty $raw 'isError')) 'tool_use_result.isError' }
    if (Test-RestartProperty $raw 'is_error') { Assert-Restart (-not (Get-RestartBooleanProperty $raw 'is_error')) 'tool_use_result.is_error' }
    Assert-Restart (Test-RestartProperty $raw 'structuredContent') 'MCP tool_use_result must carry structuredContent'
    $payload = Get-RestartProperty $raw 'structuredContent' -Required
    Assert-RestartJsonObject $payload 'MCP structuredContent'
    return $payload
}

function Get-RestartUserContentDisposition {
    param($Event, $Message, $Content)
    Assert-Restart ((Get-RestartStringProperty $Message 'role') -ceq 'user') 'user event role'
    Assert-Restart (-not (Test-RestartProperty $Event 'toolUseResult')) 'camel-case toolUseResult fallback is forbidden'
    $blocks = @($Content)
    $toolResultBlocks = @()
    foreach ($block in $blocks) {
        Assert-RestartJsonObject $block 'user stream-json content block'
        if ((Get-RestartStringProperty $block 'type') -ceq 'tool_result') {
            $toolResultBlocks += $block
        }
    }
    $hasTopLevelResult = Test-RestartProperty $Event 'tool_use_result'
    if ($toolResultBlocks.Count -eq 0 -and -not $hasTopLevelResult) {
        foreach ($block in $blocks) {
            Assert-Restart ((Get-RestartStringProperty $block 'type') -ceq 'text') 'user event without tool_result may contain only text blocks'
        }
        return 'ignore_text'
    }
    Assert-Restart ($blocks.Count -eq 1) 'each user tool-result event must contain exactly one block'
    Assert-Restart ($toolResultBlocks.Count -eq 1) 'user tool-result event must contain exactly one tool_result'
    Assert-Restart $hasTopLevelResult 'user tool_result event must carry top-level tool_use_result'
    Assert-RestartJsonObject (Get-RestartProperty $Event 'tool_use_result' -Required) 'top-level tool_use_result'
    [void](Get-RestartStringProperty $toolResultBlocks[0] 'tool_use_id')
    return 'tool_result'
}

function Read-RestartClaudeAudit {
    param([Parameter(Mandatory = $true)][string]$StreamPath)
    Assert-Restart (Test-Path -LiteralPath $StreamPath -PathType Leaf) 'restart Claude stream-json is absent'
    $toolUses = @()
    $byId = @{}
    $initEvents = @()
    $finalResults = @()
    $lastType = $null
    $lineNumber = 0
    foreach ($line in [System.IO.File]::ReadLines($StreamPath, $script:RestartUtf8)) {
        $lineNumber++
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try { $event = $line | ConvertFrom-Json }
        catch { throw "restart assertion failed: invalid stream-json line $lineNumber" }
        Assert-RestartJsonObject $event "stream-json event line $lineNumber"
        $eventType = Get-RestartStringProperty $event 'type'
        $lastType = $eventType
        if ($eventType -ceq 'system' -and (Get-RestartStringProperty $event 'subtype') -ceq 'init') { $initEvents += $event }
        if ($eventType -ceq 'result') { $finalResults += $event }
        if (-not (Test-RestartProperty $event 'message')) { continue }
        $message = Get-RestartProperty $event 'message' -Required
        Assert-RestartJsonObject $message "stream-json message line $lineNumber"
        if (-not (Test-RestartProperty $message 'content')) { continue }
        $content = Get-RestartProperty $message 'content' -Required
        Assert-RestartJsonArray $content "stream-json content line $lineNumber"
        if ($eventType -ceq 'user') {
            $disposition = Get-RestartUserContentDisposition -Event $event -Message $message -Content $content
            if ($disposition -ceq 'ignore_text') {
                continue
            }
        }
        foreach ($block in @($content)) {
            Assert-RestartJsonObject $block "stream-json block line $lineNumber"
            $blockType = Get-RestartStringProperty $block 'type'
            if ($blockType -ceq 'tool_use') {
                Assert-Restart ($eventType -ceq 'assistant') 'tool_use must be in assistant event'
                Assert-Restart ((Get-RestartStringProperty $message 'role') -ceq 'assistant') 'tool_use assistant role'
                $id = Get-RestartStringProperty $block 'id'
                Assert-Restart (-not $byId.ContainsKey($id)) "duplicate tool_use ID $id"
                $name = Get-RestartStringProperty $block 'name'
                Assert-Restart (@('Skill', $script:RestartFullGetTool, $script:RestartFullListTool) -ccontains $name) "unexpected restart tool $name"
                $input = Get-RestartProperty $block 'input' -Required
                Assert-RestartJsonObject $input "tool input $name"
                $record = [PSCustomObject][ordered]@{
                    ordinal = $toolUses.Count
                    tool_use_id = $id
                    full_name = $name
                    input = $input
                    result = $null
                }
                $toolUses += $record
                $byId[$id] = $record
                continue
            }
            if ($blockType -ceq 'tool_result') {
                Assert-Restart ($eventType -ceq 'user') 'tool_result must be in user event'
                $id = Get-RestartStringProperty $block 'tool_use_id'
                Assert-Restart ($byId.ContainsKey($id)) "tool_result without matching tool_use $id"
                $record = $byId[$id]
                Assert-Restart ($null -eq $record.result) "duplicate tool_result $id"
                if (Test-RestartProperty $block 'is_error') { Assert-Restart (-not (Get-RestartBooleanProperty $block 'is_error')) "tool_result is_error $id" }
                if ($record.full_name -ceq 'Skill') {
                    $record.result = [PSCustomObject]@{ skill_loaded = $true }
                }
                else {
                    $record.result = Get-RestartToolResultPayload -Event $event -ExpectedToolUseId $id
                }
            }
        }
    }
    Assert-Restart ($initEvents.Count -eq 1) 'stream-json must contain exactly one system/init event'
    Assert-Restart ($finalResults.Count -eq 1) 'stream-json must contain exactly one final result event'
    Assert-Restart ($lastType -ceq 'result') 'final non-empty stream-json event must be result'
    $final = $finalResults[0]
    Assert-Restart ((Get-RestartStringProperty $final 'subtype') -ceq 'success') 'final result subtype must be success'
    Assert-Restart (-not (Get-RestartBooleanProperty $final 'is_error')) 'final result is_error must be false'
    $init = $initEvents[0]
    Assert-Restart ((Get-RestartStringProperty $init 'cwd').Equals($script:RestartRepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) 'Claude cwd must be repository root'
    Assert-Restart ((Get-RestartStringProperty $init 'model') -ceq $script:RestartEffectiveModel) 'effective model must be deepseek-v4-flash[1m]'
    Assert-Restart ((Get-RestartStringProperty $init 'permissionMode') -ceq 'dontAsk') 'permission mode must be dontAsk'
    $reportedTools = Get-RestartProperty $init 'tools' -Required
    Assert-RestartStringArray $reportedTools 'system/init tools'
    Assert-RestartExactStrings @($reportedTools | Sort-Object) @($script:RestartExpectedDiscoveredTools | Sort-Object) 'system/init tools'
    $servers = Get-RestartProperty $init 'mcp_servers' -Required
    Assert-RestartJsonArray $servers 'system/init MCP servers'
    Assert-Restart (@($servers).Count -eq 1) 'strict MCP must load exactly one server'
    $server = @($servers)[0]
    Assert-RestartJsonObject $server 'system/init MCP server'
    $serverName = if (Test-RestartProperty $server 'name') { Get-RestartStringProperty $server 'name' } else { Get-RestartStringProperty $server 'serverName' }
    Assert-Restart ($serverName -ceq 'problem-locator') 'strict MCP server name'
    if (Test-RestartProperty $server 'status') { Assert-Restart ((Get-RestartStringProperty $server 'status') -ceq 'connected') 'strict MCP server status' }
    foreach ($record in $toolUses) { Assert-Restart ($null -ne $record.result) "missing tool_result for $($record.full_name)" }
    Assert-Restart ($toolUses.Count -eq 3) 'restart Claude must invoke exactly three tools'
    Assert-Restart ($toolUses[0].full_name -ceq 'Skill') 'problem-locator-client Skill must be the first tool_use'
    Assert-Restart ($toolUses[1].full_name -ceq $script:RestartFullGetTool) 'get_case must be the first MCP call'
    Assert-Restart ($toolUses[2].full_name -ceq $script:RestartFullListTool) 'list_artifacts must be the second MCP call'
    $skillInputNames = @($toolUses[0].input.PSObject.Properties.Name)
    Assert-Restart ($skillInputNames -ccontains 'skill') 'Skill input requires skill'
    foreach ($name in $skillInputNames) {
        Assert-Restart (@('skill', 'args') -ccontains $name) "Skill input has unexpected property $name"
    }
    Assert-Restart ((Get-RestartStringProperty $toolUses[0].input 'skill') -ceq 'problem-locator-client') 'Skill input must be exact'
    if (Test-RestartProperty $toolUses[0].input 'args') {
        [void](Get-RestartStringProperty $toolUses[0].input 'args')
    }
    return [PSCustomObject][ordered]@{
        init = [PSCustomObject][ordered]@{
            cwd = $script:RestartRepoRoot
            model_alias = $script:RestartModelAlias
            effective_model = $script:RestartEffectiveModel
            permission_mode = 'dontAsk'
            tools = [object[]]@($reportedTools)
            mcp_servers = @([PSCustomObject]@{ name = 'problem-locator'; url = $script:RestartMcpUrl; always_load = $true })
        }
        mcp_records = @($toolUses[1], $toolUses[2])
        skill_invocation_count = 1
        final_result = [PSCustomObject][ordered]@{ subtype = 'success'; is_error = $false }
    }
}

function Get-RestartSuccessData {
    param($Record)
    $result = $Record.result
    Assert-RestartExactProperties $result @('ok', 'data', 'error') 'MCP Envelope'
    Assert-Restart (Get-RestartBooleanProperty $result 'ok') 'MCP tool returned business error'
    Assert-Restart ($null -eq (Get-RestartProperty $result 'error' -Required)) 'success Envelope error must be null'
    $data = Get-RestartProperty $result 'data' -Required
    Assert-RestartJsonObject $data 'MCP success data'
    return $data
}

function Assert-RestartArtifactSummary {
    param($Summary, $Artifact, [Parameter(Mandatory = $true)][string]$ExpectedKind)
    Assert-RestartExactProperties $Summary @('artifact_id', 'kind', 'name', 'content_type', 'resource_kind', 'size', 'sha256', 'created_by_job_id', 'created_at', 'downloadable') 'post-restart ArtifactSummary'
    Assert-Restart ((Get-RestartStringProperty $Summary 'artifact_id') -ceq (Get-RestartStringProperty $Artifact 'artifact_id')) 'ArtifactSummary artifact_id'
    Assert-Restart ((Get-RestartStringProperty $Summary 'kind') -ceq $ExpectedKind) 'ArtifactSummary kind'
    Assert-Restart ((Get-RestartStringProperty $Summary 'name') -ceq (Get-RestartStringProperty $Artifact 'name')) 'ArtifactSummary name'
    Assert-Restart ((Get-RestartStringProperty $Summary 'content_type') -ceq (Get-RestartStringProperty $Artifact 'content_type')) 'ArtifactSummary content_type'
    Assert-Restart ((Get-RestartStringProperty $Summary 'resource_kind') -ceq 'FILE') 'ArtifactSummary resource_kind'
    Assert-Restart ((Get-RestartIntegerProperty $Summary 'size') -eq (Get-RestartIntegerProperty $Artifact 'size')) 'ArtifactSummary size'
    Assert-Restart ((Get-RestartStringProperty $Summary 'sha256') -ceq (Get-RestartStringProperty $Artifact 'sha256')) 'ArtifactSummary SHA-256'
    Assert-RestartUuid (Get-RestartStringProperty $Summary 'created_by_job_id') 'ArtifactSummary created_by_job_id'
    Assert-Restart ((Get-RestartStringProperty $Summary 'created_at') -ceq (Get-RestartStringProperty $Artifact 'created_at')) 'ArtifactSummary created_at'
    Assert-Restart (Get-RestartBooleanProperty $Summary 'downloadable') 'ArtifactSummary downloadable'
}

function Confirm-RestartPersistenceResult {
    param($Audit, $PreSummary, [Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $records = @($Audit.mcp_records)
    Assert-Restart ($records.Count -eq 2) 'restart MCP call count'
    $caseId = Get-RestartStringProperty $PreSummary 'case_id'
    $get = $records[0]
    $list = $records[1]
    Assert-RestartJsonObject $get.input 'post-restart get_case input'
    $getInputNames = @($get.input.PSObject.Properties.Name)
    Assert-Restart ($getInputNames -ccontains 'case_id') 'post-restart get_case input requires case_id'
    Assert-Restart ($getInputNames -ccontains 'wait_seconds') 'post-restart get_case input requires wait_seconds'
    foreach ($name in $getInputNames) {
        Assert-Restart (@('case_id', 'wait_for_job_id', 'wait_seconds') -ccontains $name) "post-restart get_case input has unexpected property $name"
    }
    Assert-Restart ((Get-RestartStringProperty $get.input 'case_id') -ceq $caseId) 'post-restart get_case case_id'
    Assert-Restart ((Get-RestartIntegerProperty $get.input 'wait_seconds') -eq 0) 'post-restart get_case wait_seconds'
    if (Test-RestartProperty $get.input 'wait_for_job_id') {
        Assert-Restart ($null -eq (Get-RestartProperty $get.input 'wait_for_job_id' -Required)) 'post-restart get_case wait_for_job_id'
    }
    $getData = Get-RestartSuccessData $get
    Assert-RestartExactProperties $getData @('case_view', 'wait_timed_out') 'post-restart get_case data'
    Assert-Restart (-not (Get-RestartBooleanProperty $getData 'wait_timed_out')) 'post-restart get_case must not time out'
    $view = Get-RestartProperty $getData 'case_view' -Required
    Assert-RestartExactProperties $view @('case_id', 'status', 'case_revision', 'diagnosis_state_revision', 'problem_spec', 'user_facts', 'confirmed_facts', 'open_questions', 'pending_requirements', 'active_job', 'selected_skill_ref', 'final_result', 'failure', 'artifacts', 'created_at', 'updated_at') 'post-restart CaseView'
    Assert-Restart ((Get-RestartStringProperty $view 'case_id') -ceq $caseId) 'post-restart CaseView case_id'
    Assert-Restart ((Get-RestartStringProperty $view 'status') -ceq 'RESOLVED') 'post-restart Case status'
    Assert-Restart ((Get-RestartIntegerProperty $view 'case_revision') -eq (Get-RestartIntegerProperty $PreSummary 'resolved_case_revision')) 'persisted Case revision'
    Assert-Restart ((Get-RestartIntegerProperty $view 'diagnosis_state_revision') -eq (Get-RestartIntegerProperty $PreSummary 'diagnosis_state_revision')) 'persisted diagnosis revision'
    Assert-Restart ($null -eq (Get-RestartProperty $view 'active_job' -Required)) 'resolved Case active_job must be null'
    Assert-Restart ($null -eq (Get-RestartProperty $view 'failure' -Required)) 'resolved Case failure must be null'
    $selectedSkill = Get-RestartProperty $view 'selected_skill_ref' -Required
    Assert-RestartSelectedSkill $selectedSkill 'post-restart selected Skill'
    Assert-RestartJsonEquivalent $selectedSkill (Get-RestartProperty $PreSummary 'selected_skill_ref' -Required) 'persisted selected Skill'
    $finalResult = Get-RestartProperty $view 'final_result' -Required
    Assert-RestartFinalResult $finalResult 'post-restart final result'
    Assert-RestartJsonEquivalent $finalResult (Get-RestartProperty $PreSummary 'final_result' -Required) 'persisted final result'
    $caseArtifacts = Get-RestartProperty $view 'artifacts' -Required
    Assert-RestartJsonArray $caseArtifacts 'post-restart Case artifacts'
    Assert-Restart (@($caseArtifacts).Count -eq 2) 'post-restart Case public artifact count'

    Assert-RestartExactProperties $list.input @('case_id') 'post-restart list_artifacts input'
    Assert-Restart ((Get-RestartStringProperty $list.input 'case_id') -ceq $caseId) 'post-restart list_artifacts case_id'
    $listData = Get-RestartSuccessData $list
    Assert-RestartExactProperties $listData @('artifacts') 'post-restart list_artifacts data'
    $artifacts = Get-RestartProperty $listData 'artifacts' -Required
    Assert-RestartJsonArray $artifacts 'post-restart public artifacts'
    Assert-Restart (@($artifacts).Count -eq 2) 'post-restart public artifact count'
    $resultArtifacts = @($artifacts | Where-Object { (Get-RestartStringProperty $_ 'name') -ceq 'diagnosis-result.json' })
    $archiveArtifacts = @($artifacts | Where-Object { (Get-RestartStringProperty $_ 'name') -ceq 'result.zip' })
    Assert-Restart ($resultArtifacts.Count -eq 1) 'post-restart result ArtifactView count'
    Assert-Restart ($archiveArtifacts.Count -eq 1) 'post-restart archive ArtifactView count'
    $artifact = $resultArtifacts[0]
    $archive = $archiveArtifacts[0]
    Assert-RestartArtifactView $artifact $caseId 'diagnosis-result.json' 'application/json' 'post-restart public result artifact'
    Assert-RestartArtifactView $archive $caseId 'result.zip' 'application/zip' 'post-restart public archive artifact'
    Assert-RestartJsonEquivalent $artifact (Get-RestartProperty $PreSummary 'public_artifact' -Required) 'persisted public artifact'
    Assert-RestartJsonEquivalent $archive (Get-RestartProperty $PreSummary 'public_result_archive' -Required) 'persisted public archive'
    $resultSummaries = @($caseArtifacts | Where-Object { (Get-RestartStringProperty $_ 'kind') -ceq 'USER_RESULT' })
    $archiveSummaries = @($caseArtifacts | Where-Object { (Get-RestartStringProperty $_ 'kind') -ceq 'USER_RESULT_ARCHIVE' })
    Assert-Restart ($resultSummaries.Count -eq 1) 'post-restart USER_RESULT ArtifactSummary count'
    Assert-Restart ($archiveSummaries.Count -eq 1) 'post-restart USER_RESULT_ARCHIVE ArtifactSummary count'
    Assert-RestartArtifactSummary $resultSummaries[0] $artifact 'USER_RESULT'
    Assert-RestartArtifactSummary $archiveSummaries[0] $archive 'USER_RESULT_ARCHIVE'

    $prePath = Join-Path $EvidenceRoot 'journey-authoritative-summary.json'
    $preHash = (Get-FileHash -LiteralPath $prePath -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-RestartSha256 $preHash 'pre-restart summary file SHA-256'
    return [PSCustomObject][ordered]@{
        schema_version = 1
        attempt = Get-RestartAttemptLabel $EvidenceRoot
        pre_restart_summary_sha256 = $preHash
        case_id = $caseId
        attachment_id = Get-RestartStringProperty $PreSummary 'attachment_id'
        resolved_case_revision = Get-RestartIntegerProperty $view 'case_revision'
        diagnosis_state_revision = Get-RestartIntegerProperty $view 'diagnosis_state_revision'
        selected_skill_ref = $selectedSkill
        final_result = $finalResult
        public_artifact = $artifact
        public_result_archive = $archive
        get_case_wait_timed_out = $false
        mcp_call_order = @($script:RestartGetTool, $script:RestartListTool)
        claude_version = $script:RestartClaudeVersion
        model_alias = $script:RestartModelAlias
        effective_model = $script:RestartEffectiveModel
        persistence_unchanged = $true
    }
}

function Read-RestartSummaryValidated {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot, $PreSummary)
    $summary = Read-RestartJson (Join-Path $EvidenceRoot 'restart-authoritative-summary.json')
    Assert-RestartExactProperties $summary @('schema_version', 'attempt', 'pre_restart_summary_sha256', 'case_id', 'attachment_id', 'resolved_case_revision', 'diagnosis_state_revision', 'selected_skill_ref', 'final_result', 'public_artifact', 'public_result_archive', 'get_case_wait_timed_out', 'mcp_call_order', 'claude_version', 'model_alias', 'effective_model', 'persistence_unchanged') 'restart authoritative summary'
    Assert-Restart ((Get-RestartIntegerProperty $summary 'schema_version') -eq 1) 'restart summary schema_version'
    Assert-Restart ((Get-RestartStringProperty $summary 'attempt') -ceq (Get-RestartAttemptLabel $EvidenceRoot)) 'restart summary attempt'
    $preHash = (Get-FileHash -LiteralPath (Join-Path $EvidenceRoot 'journey-authoritative-summary.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-Restart ((Get-RestartStringProperty $summary 'pre_restart_summary_sha256') -ceq $preHash) 'restart summary pre-summary SHA-256'
    Assert-Restart ((Get-RestartStringProperty $summary 'case_id') -ceq (Get-RestartStringProperty $PreSummary 'case_id')) 'restart summary case_id'
    Assert-Restart ((Get-RestartStringProperty $summary 'attachment_id') -ceq (Get-RestartStringProperty $PreSummary 'attachment_id')) 'restart summary attachment_id'
    Assert-Restart ((Get-RestartIntegerProperty $summary 'resolved_case_revision') -eq (Get-RestartIntegerProperty $PreSummary 'resolved_case_revision')) 'restart summary Case revision'
    Assert-Restart ((Get-RestartIntegerProperty $summary 'diagnosis_state_revision') -eq (Get-RestartIntegerProperty $PreSummary 'diagnosis_state_revision')) 'restart summary diagnosis revision'
    Assert-RestartSelectedSkill (Get-RestartProperty $summary 'selected_skill_ref' -Required) 'restart summary selected Skill'
    Assert-RestartJsonEquivalent (Get-RestartProperty $summary 'selected_skill_ref' -Required) (Get-RestartProperty $PreSummary 'selected_skill_ref' -Required) 'restart/pre selected Skill'
    Assert-RestartFinalResult (Get-RestartProperty $summary 'final_result' -Required) 'restart summary final result'
    Assert-RestartJsonEquivalent (Get-RestartProperty $summary 'final_result' -Required) (Get-RestartProperty $PreSummary 'final_result' -Required) 'restart/pre final result'
    Assert-RestartArtifactView (Get-RestartProperty $summary 'public_artifact' -Required) (Get-RestartStringProperty $summary 'case_id') 'diagnosis-result.json' 'application/json' 'restart summary public result artifact'
    Assert-RestartArtifactView (Get-RestartProperty $summary 'public_result_archive' -Required) (Get-RestartStringProperty $summary 'case_id') 'result.zip' 'application/zip' 'restart summary public archive artifact'
    Assert-RestartJsonEquivalent (Get-RestartProperty $summary 'public_artifact' -Required) (Get-RestartProperty $PreSummary 'public_artifact' -Required) 'restart/pre public artifact'
    Assert-RestartJsonEquivalent (Get-RestartProperty $summary 'public_result_archive' -Required) (Get-RestartProperty $PreSummary 'public_result_archive' -Required) 'restart/pre public archive artifact'
    Assert-Restart (-not (Get-RestartBooleanProperty $summary 'get_case_wait_timed_out')) 'restart summary wait timeout'
    $order = Get-RestartProperty $summary 'mcp_call_order' -Required
    Assert-RestartStringArray $order 'restart summary MCP order'
    Assert-RestartExactStrings $order @($script:RestartGetTool, $script:RestartListTool) 'restart summary MCP order'
    Assert-Restart ((Get-RestartStringProperty $summary 'claude_version') -ceq $script:RestartClaudeVersion) 'restart summary Claude version'
    Assert-Restart ((Get-RestartStringProperty $summary 'model_alias') -ceq $script:RestartModelAlias) 'restart summary model alias'
    Assert-Restart ((Get-RestartStringProperty $summary 'effective_model') -ceq $script:RestartEffectiveModel) 'restart summary effective model'
    Assert-Restart (Get-RestartBooleanProperty $summary 'persistence_unchanged') 'restart summary persistence flag'
    return $summary
}

function Get-RestartResponseHeaders {
    param([Parameter(Mandatory = $true)][string]$Path)
    $text = [System.IO.File]::ReadAllText($Path, $script:RestartUtf8)
    $lines = @($text -split "`r?`n")
    $statusIndexes = @()
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -cmatch '^HTTP/[0-9.]+ [0-9]{3}(?: |$)') { $statusIndexes += $index }
    }
    Assert-Restart ($statusIndexes.Count -eq 1) 'download must have exactly one HTTP response block'
    $statusLine = $lines[$statusIndexes[0]]
    Assert-Restart ($statusLine -cmatch '^HTTP/[0-9.]+ 200(?: |$)') 'download HTTP status line'
    $headers = @{}
    for ($index = $statusIndexes[0] + 1; $index -lt $lines.Count; $index++) {
        $line = $lines[$index]
        if ([string]::IsNullOrEmpty($line)) { break }
        $colon = $line.IndexOf(':')
        Assert-Restart ($colon -gt 0) 'malformed HTTP response header'
        $name = $line.Substring(0, $colon).Trim().ToLowerInvariant()
        $value = $line.Substring($colon + 1).Trim()
        Assert-Restart (-not $headers.ContainsKey($name)) "duplicate HTTP response header $name"
        $headers[$name] = $value
    }
    return [PSCustomObject]@{ status_line = $statusLine; headers = $headers }
}

function Invoke-RestartArtifactDownload {
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        $Summary,
        [Parameter(Mandatory = $true)][ValidateSet('public_artifact', 'public_result_archive')][string]$ArtifactProperty,
        [Parameter(Mandatory = $true)][string]$Prefix,
        [Parameter(Mandatory = $true)][string]$BodyName,
        [Parameter(Mandatory = $true)][string]$ExpectedContentType,
        [switch]$JsonObject
    )
    $artifact = Get-RestartProperty $Summary $ArtifactProperty -Required
    $caseId = Get-RestartStringProperty $Summary 'case_id'
    $artifactId = Get-RestartStringProperty $artifact 'artifact_id'
    $size = Get-RestartIntegerProperty $artifact 'size'
    $sha256 = Get-RestartStringProperty $artifact 'sha256'
    $url = Get-RestartStringProperty $artifact 'download_url'
    $expectedUrl = "$($script:RestartServiceBaseUrl)/api/v1/artifacts/$artifactId/content?case_id=$caseId"
    Assert-Restart ($url -ceq $expectedUrl) 'download URL must be exact loopback URL'
    $stdout = Join-Path $EvidenceRoot "$Prefix.curl.stdout.txt"
    $stderr = Join-Path $EvidenceRoot "$Prefix.curl.stderr.txt"
    $headers = Join-Path $EvidenceRoot "$Prefix.response.headers.txt"
    $body = Join-Path $EvidenceRoot $BodyName
    Assert-RestartReservedUnused $headers
    Assert-RestartReservedUnused $body
    $arguments = @(
        '--silent', '--show-error', '--fail-with-body',
        '--connect-timeout', [string]$script:RestartCurlConnectTimeoutSeconds,
        '--max-time', [string]$script:RestartCurlMaxTimeSeconds,
        '--max-filesize', [string]$size,
        '--dump-header', $headers,
        '--output', $body,
        '--write-out', '%{http_code}',
        $url
    )
    $exitCode = Invoke-RestartCapturedProcess -FilePath $script:RestartCurlExe -Arguments $arguments -WorkingDirectory $script:RestartRepoRoot -StdoutPath $stdout -StderrPath $stderr -TimeoutSeconds ($script:RestartCurlMaxTimeSeconds + 15)
    Complete-RestartExternalOutput $headers
    Complete-RestartExternalOutput $body
    Assert-Restart ($exitCode -eq 0) 'restart artifact curl exit code'
    Assert-Restart ([System.IO.File]::ReadAllText($stdout, $script:RestartUtf8).Trim() -ceq '200') 'restart artifact curl HTTP code'
    $headerResult = Get-RestartResponseHeaders $headers
    Assert-Restart ($headerResult.headers.ContainsKey('content-type')) 'download Content-Type header absent'
    Assert-Restart ($headerResult.headers['content-type'] -ceq $ExpectedContentType) 'download Content-Type header'
    Assert-Restart ($headerResult.headers.ContainsKey('content-length')) 'download Content-Length header absent'
    Assert-Restart ($headerResult.headers['content-length'] -ceq [string]$size) 'download Content-Length header'
    $item = Get-Item -LiteralPath $body
    Assert-Restart ($item.Length -eq $size) 'downloaded artifact byte count'
    $actualHash = (Get-FileHash -LiteralPath $body -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-Restart ($actualHash -ceq $sha256) 'downloaded artifact SHA-256'
    if ($JsonObject) {
        $payload = Read-RestartJson $body
        Assert-RestartJsonObject $payload 'downloaded UserResult payload'
    }
    return [PSCustomObject][ordered]@{
        schema_version = 1
        attempt = Get-RestartAttemptLabel $EvidenceRoot
        case_id = $caseId
        artifact_id = $artifactId
        download_url = $url
        http_status = 200
        content_type = $headerResult.headers['content-type']
        content_length = $size
        size = $item.Length
        sha256 = $actualHash
        json_object = [bool]$JsonObject
        curl_connect_timeout_seconds = $script:RestartCurlConnectTimeoutSeconds
        curl_max_time_seconds = $script:RestartCurlMaxTimeSeconds
        curl_max_filesize = $size
    }
}
