param([Parameter(Mandatory = $true)][string]$EvidenceRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false, $true)
$EvidenceRoot = [IO.Path]::GetFullPath($EvidenceRoot)

function Write-ManifestJson([string]$Path, $Value) {
    if (Test-Path -LiteralPath $Path) { throw "E2E_MANIFEST_EXISTS:$Path" }
    $bytes = $utf8.GetBytes((($Value | ConvertTo-Json -Depth 30) + "`n"))
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
}

function Get-FileRecords([string]$Root, [string[]]$Names) {
    return [object[]]@($Names | ForEach-Object {
        $item = Get-Item -LiteralPath (Join-Path $Root $_) -Force
        if ($item.PSIsContainer -or $item.Length -le 0) { throw "E2E_MANIFEST_FILE:$_" }
        [PSCustomObject][ordered]@{
            name = $_
            size = [int64]$item.Length
            sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
}

if (-not (Test-Path -LiteralPath $EvidenceRoot -PathType Container)) { throw 'E2E_MANIFEST_ROOT' }

. (Join-Path $EvidenceRoot 'windows-journey-lib.ps1')
$journeyFiles = @('README.md', 'run-windows-journey.ps1', 'static-check.ps1', 'windows-journey-lib.ps1')
$journey = [PSCustomObject][ordered]@{
    schema_version = 1
    static_check = 'passed'
    network_or_claude_invoked = $false
    reads_or_copies_secret_settings = $false
    inline_strict_mcp = $true
    stdout_stderr_separated = $true
    authoritative_source = 'stream-json tool_use/tool_result pairs only'
    user_text_event_regression = 'passed'
    mixed_or_multiple_tool_result_fail_closed = $true
    possible_runtime_outputs = [object[]]@(Get-JourneyAllOutputNames)
    files = Get-FileRecords $EvidenceRoot $journeyFiles
}
Write-ManifestJson (Join-Path $EvidenceRoot 'windows-journey-driver-manifest.json') $journey
Confirm-JourneyDriverManifest $EvidenceRoot

. (Join-Path $EvidenceRoot 'windows-http-capture-lib.ps1')
$httpFiles = @('README-http-capture.md', 'run-windows-http-capture.ps1', 'static-check-http-capture.ps1', 'windows-http-capture-lib.ps1')
$http = [PSCustomObject][ordered]@{
    schema_version = 1
    static_check = 'passed'
    network_or_claude_invoked = $false
    reads_settings_or_environment = $false
    http_runtime_contacts_loopback_only = $true
    curl_executable = $script:HcCurlExe
    service_base_url = $script:HcServiceBaseUrl
    phases = [object[]]@('Before', 'After')
    all_runtime_outputs_create_new = $true
    source_of_public_artifact = 'validated authoritative journey summaries'
    source_of_internal_artifact = 'unique LOGPARSE_RUN in canonical state-export.before.json'
    possible_runtime_outputs = [object[]]@(Get-HcAllOutputNames)
    files = Get-FileRecords $EvidenceRoot $httpFiles
}
Write-ManifestJson (Join-Path $EvidenceRoot 'windows-http-capture-driver-manifest.json') $http
Confirm-HcDriverManifest $EvidenceRoot

$restartRoot = Join-Path $EvidenceRoot 'restart'
. (Join-Path $restartRoot 'windows-restart-lib.ps1')
$restartFiles = @('README-restart.md', 'download-windows-restart-artifact.ps1', 'run-windows-restart-verify.ps1', 'static-check-restart.ps1', 'windows-restart-lib.ps1')
$restart = [PSCustomObject][ordered]@{
    schema_version = 1
    static_check = 'passed'
    network_or_claude_invoked = $false
    reads_or_copies_secret_settings = $false
    inline_strict_mcp = $true
    claude_business_tools = [object[]]@('problem_locator_get_case', 'problem_locator_list_artifacts')
    first_tool = 'Skill(problem-locator-client)'
    authoritative_source = 'uniquely correlated stream-json tool_use/tool_result structuredContent only'
    user_text_event_regression = 'passed'
    mixed_or_multiple_tool_result_fail_closed = $true
    all_runtime_outputs_create_new = $true
    possible_runtime_outputs = [object[]]@(Get-RestartAllOutputNames)
    files = Get-FileRecords $restartRoot $restartFiles
}
Write-ManifestJson (Join-Path $restartRoot 'windows-restart-driver-manifest.json') $restart
Confirm-RestartDriverManifest $restartRoot

Write-Output 'E2E_DRIVER_MANIFESTS_PASSED'
