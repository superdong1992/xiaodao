param(
    [ValidateSet('Fast', 'Release', 'ReleaseGates')][string]$Profile = 'Release',
    [string]$RepoRoot = 'D:\code\xiaodao',
    [string]$LogparseSource = 'D:\code\logparse',
    [string]$McpSource = 'D:\code\problem-locator-mcp',
    [string]$SettingsPath = 'C:\Users\admin\.claude\settings.json',
    [string]$DockerConfig = 'C:\Users\admin\.docker',
    [string]$BusinessEvidenceRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false, $true)
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$LogparseSource = [IO.Path]::GetFullPath($LogparseSource)
$McpSource = [IO.Path]::GetFullPath($McpSource)
$SettingsPath = [IO.Path]::GetFullPath($SettingsPath)
$DockerConfig = [IO.Path]::GetFullPath($DockerConfig)
if (-not [string]::IsNullOrWhiteSpace($BusinessEvidenceRoot)) {
    $BusinessEvidenceRoot = [IO.Path]::GetFullPath($BusinessEvidenceRoot)
}
$toolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$harnessRoot = Join-Path $toolRoot 'harness'
. (Join-Path $toolRoot 'business-patch-identity.ps1')
$evidenceBase = Join-Path $RepoRoot '.tmp\pl-e2e-evidence'
$cacheRoot = Join-Path $RepoRoot '.tmp\pl-e2e-cache'
$uvContext = Join-Path $cacheRoot 'uv-0.11.32'
$claudeContext = Join-Path $cacheRoot 'claude-npm-2.1.89'
$claudePackageSource = 'C:\Users\admin\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code'
$claudeCliSha256 = 'a9950ef6407fdc750bddb673852485500387e524a99d42385cb81e7d17128e01'
$docker = (Get-Command docker.exe -ErrorAction Stop).Source
$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$script:stage = 'host-preflight'
$script:createdContainers = [Collections.Generic.List[string]]::new()
$script:createdVolumes = [Collections.Generic.List[string]]::new()
$script:timings = [Collections.Generic.List[object]]::new()
$script:success = $false
$script:failure = $null
$script:baseCacheHit = $false
$script:warmSeconds = $null

function Assert-E2E([bool]$Condition, [string]$Code) {
    if (-not $Condition) { throw "E2E_$Code" }
}

function Write-E2EUtf8([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, $utf8)
}

function Write-E2EJson([string]$Path, $Value) {
    Write-E2EUtf8 $Path (($Value | ConvertTo-Json -Depth 40) + "`n")
}

function Invoke-Docker([string[]]$Arguments, [switch]$Quiet) {
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $docker @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedPreference
    }
    if (-not $Quiet) { $output | ForEach-Object { Write-Output ([string]$_) } }
    if ($exitCode -ne 0) { throw "E2E_DOCKER_EXIT_${exitCode}:$($Arguments[0])" }
    return [string[]]@($output | ForEach-Object { [string]$_ })
}

function Invoke-Step([string]$Name, [scriptblock]$Action) {
    $script:stage = $Name
    $watch = [Diagnostics.Stopwatch]::StartNew()
    Write-Output "E2E_STEP_START $Name"
    try {
        & $Action
        $status = 'PASS'
    }
    catch {
        $status = 'FAIL'
        throw
    }
    finally {
        $watch.Stop()
        $script:timings.Add([PSCustomObject][ordered]@{
            name = $Name
            status = $status
            elapsed_seconds = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
        })
        Write-Output "E2E_STEP_END $Name status=$status elapsed=$([Math]::Round($watch.Elapsed.TotalSeconds, 1))s"
    }
}

function Get-NextAttemptNumber {
    $max = 0
    if (Test-Path -LiteralPath $evidenceBase -PathType Container) {
        foreach ($directory in @(Get-ChildItem -LiteralPath $evidenceBase -Directory)) {
            if ($directory.Name -cmatch '^attempt([0-9]+)-') {
                $value = [int]$Matches[1]
                if ($value -gt $max) { $max = $value }
            }
        }
    }
    return $max + 1
}

function Copy-EvidenceBundle([string]$Target) {
    Assert-E2E (-not (Test-Path -LiteralPath $Target)) 'EVIDENCE_TARGET_EXISTS'
    [IO.Directory]::CreateDirectory($Target) | Out-Null
    # Copy children explicitly and keep all filesystem mutation under the
    # newly-created evidence root.
    foreach ($entry in @(Get-ChildItem -LiteralPath $harnessRoot -Force)) {
        Copy-Item -LiteralPath $entry.FullName -Destination $Target -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $toolRoot 'bounded-process.ps1') -Destination (Join-Path $Target 'bounded-process.ps1') -Force
    Copy-Item -LiteralPath (Join-Path $toolRoot 'business-patch-identity.ps1') -Destination (Join-Path $Target 'business-patch-identity.ps1') -Force
    $clientAssets = Join-Path $Target 'client-assets'
    [IO.Directory]::CreateDirectory($clientAssets) | Out-Null
    Copy-Item -LiteralPath (Join-Path $RepoRoot '.claude\skills\logparse-diagnose') -Destination $clientAssets -Recurse -Force

    # Python compilation may have left local cache files below the harness.
    # They are not source evidence and cannot be normalized as UTF-8 text.
    $targetPrefix = [IO.Path]::GetFullPath($Target).TrimEnd('\') + '\'
    foreach ($cacheDirectory in @(Get-ChildItem -LiteralPath $Target -Recurse -Directory -Force | Where-Object { $_.Name -ceq '__pycache__' })) {
        $cachePath = [IO.Path]::GetFullPath($cacheDirectory.FullName)
        Assert-E2E ($cachePath.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) 'EVIDENCE_CACHE_PATH_OUTSIDE_TARGET'
        Remove-Item -LiteralPath $cachePath -Recurse -Force
    }
    foreach ($compiledFile in @(Get-ChildItem -LiteralPath $Target -Recurse -File -Filter '*.pyc' -Force)) {
        $compiledPath = [IO.Path]::GetFullPath($compiledFile.FullName)
        Assert-E2E ($compiledPath.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) 'EVIDENCE_COMPILED_PATH_OUTSIDE_TARGET'
        Remove-Item -LiteralPath $compiledPath -Force
    }
    foreach ($scriptFile in @(Get-ChildItem -LiteralPath $Target -Recurse -File)) {
        $scriptText = [IO.File]::ReadAllText($scriptFile.FullName, $utf8)
        [IO.File]::WriteAllText(
            $scriptFile.FullName,
            $scriptText.Replace("`r`n", "`n").Replace("`r", "`n"),
            $utf8
        )
    }
}

function Copy-RestartRuntime([string]$MainRoot) {
    $restartRoot = Join-Path $MainRoot 'restart'
    $names = @(
        'capture_state_before_restart.sh',
        'gate_service_preflight.sh', 'prepare_claude_settings.py',
        'prepare_nonroot_settings.py', 'prepare_real_zip.py', 'restart_nonroot_runtime_init.sh',
        'scan_service_log_secrets.py', 'service_preflight.py', 'setup_claude.sh',
        'setup_fixtures.sh', 'setup_sources.sh', 'setup_venvs.sh', 'snapshot_data_root.py',
        'start_service_supervisor.sh', 'stop_service.sh', 'verify_claude_manifest.py',
        'verify_nonroot_logparse_catalog.py', 'verify_restart_nonempty_runtime.sh', 'client-assets',
        'verify_service_process.py', 'windows-service-preflight.ps1'
    )
    foreach ($name in $names) {
        Copy-Item -LiteralPath (Join-Path $MainRoot $name) -Destination (Join-Path $restartRoot $name) -Recurse -Force
    }
}

function Copy-SourcePatch([string]$SourceRoot, [string]$TargetRoot) {
    foreach ($name in @('source-input.patch', 'source-input.patch.sha256', 'source.patch.new-files.txt', 'source-patch-host-freeze.txt')) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot $name) -Destination (Join-Path $TargetRoot $name) -Force
    }
}

function Get-BaseCacheKey {
    $paths = @(
        (Join-Path $toolRoot 'Dockerfile'), (Join-Path $toolRoot 'logparse-requirements.txt'),
        (Join-Path $RepoRoot 'pyproject.toml'), (Join-Path $RepoRoot 'uv.lock'),
        (Join-Path $uvContext 'uv'), (Join-Path $uvContext 'uvx'),
        (Join-Path $claudeContext 'package\package.json'), (Join-Path $claudeContext 'package\cli.js')
    )
    $text = ($paths | ForEach-Object { (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant() }) -join "`n"
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($utf8.GetBytes($text))).Replace('-', '').ToLowerInvariant()) }
    finally { $hash.Dispose() }
}

function Ensure-BaseImage([string]$Image, [string]$BuildLog) {
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $docker --config $DockerConfig image inspect $Image 1>$null 2>$null
        $inspectExitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $savedPreference }
    if ($inspectExitCode -eq 0) {
        $script:baseCacheHit = $true
        Write-E2EUtf8 $BuildLog "base_image=$Image`ncache_hit=true`n"
        return
    }
    $script:baseCacheHit = $false
    $arguments = @(
        '--config', $DockerConfig, 'buildx', 'build', '--load', '--pull=false',
        '--progress=plain', '--tag', $Image,
        '--build-context', "uvcache=$uvContext",
        '--build-context', "claudecache=$claudeContext",
        '--file', (Join-Path $toolRoot 'Dockerfile'), $RepoRoot
    )
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $docker @arguments 2>&1 | Tee-Object -FilePath $BuildLog)
        $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $savedPreference }
    if ($exitCode -ne 0) { throw 'E2E_BASE_IMAGE_BUILD' }
}

function New-E2EVolume([string]$Name, [string]$Owner) {
    [void](Invoke-Docker @('--config', $DockerConfig, 'volume', 'create', '--driver', 'local', '--label', "problem-locator.e2e.owner=$Owner", $Name) -Quiet)
    $script:createdVolumes.Add($Name)
}

function New-E2EContainer(
    [string]$Name,
    [string]$Volume,
    [string]$Evidence,
    [string]$Image,
    [bool]$PublishPort,
    [AllowNull()][string]$AuditInput
) {
    $arguments = @(
        '--config', $DockerConfig, 'run', '--detach', '--pull=never', '--init',
        '--name', $Name, '--network', 'bridge', '--restart=no',
        '--label', "problem-locator.e2e.owner=$Name",
        '--env', 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        '--env', "E2E_CONTAINER_NAME=$Name", '--env', "E2E_VOLUME_NAME=$Volume"
    )
    if ($PublishPort) { $arguments += @('--publish', '127.0.0.1:18000:8000/tcp') }
    $arguments += @(
        '--tmpfs', '/root/.claude:rw,noexec,nosuid,nodev,mode=0700,size=536870912',
        '--tmpfs', '/run/plagent-claude:rw,noexec,nosuid,nodev,mode=0700,size=536870912',
        '--mount', "type=bind,src=$RepoRoot,dst=/source/xiaodao,readonly",
        '--mount', "type=bind,src=$LogparseSource,dst=/source/logparse,readonly",
        '--mount', "type=bind,src=$McpSource,dst=/source/problem-locator-mcp,readonly",
        '--mount', "type=bind,src=$SettingsPath,dst=/run/host-claude-settings.json,readonly",
        '--mount', "type=bind,src=$Evidence\client-assets\logparse-diagnose,dst=/run/plagent-claude/.claude/skills/logparse-diagnose,readonly",
        '--mount', "type=bind,src=$claudeContext\package,dst=/cache/claude-npm-2.1.89,readonly",
        '--mount', "type=bind,src=$Evidence,dst=/evidence",
        '--mount', "type=volume,src=$Volume,dst=/var/lib/problem-locator"
    )
    if (-not [string]::IsNullOrWhiteSpace($AuditInput)) {
        $arguments += @('--mount', "type=bind,src=$AuditInput,dst=/audit-input,readonly")
    }
    $arguments += @($Image, 'sleep', 'infinity')
    [void](Invoke-Docker $arguments -Quiet)
    $script:createdContainers.Add($Name)
}

function Invoke-ContainerScript([string]$Container, [string]$Shell, [string]$Script, [string[]]$Extra = @()) {
    $arguments = @('--config', $DockerConfig, 'exec', $Container, $Shell, "/evidence/$Script") + $Extra
    [void](Invoke-Docker $arguments)
}

function Start-ContainerScript([string]$Container, [string]$Shell, [string]$Script, [string[]]$Extra = @()) {
    $arguments = @('--config', $DockerConfig, 'exec', '--detach', $Container, $Shell, "/evidence/$Script") + $Extra
    [void](Invoke-Docker $arguments -Quiet)
}

function Wait-StatusFile([string]$Path, [int]$TimeoutSeconds) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $values = @{}
            foreach ($line in [IO.File]::ReadAllLines($Path, $utf8)) {
                if ($line -cmatch '^([a-z_]+)=([^\r\n]*)$') { $values[$Matches[1]] = $Matches[2] }
            }
            Assert-E2E ($values.ContainsKey('exit_code')) 'STATUS_EXIT_CODE'
            Assert-E2E ([int]$values.exit_code -eq 0) "ASYNC_GROUP_FAILED_$([IO.Path]::GetFileName($Path))"
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "E2E_ASYNC_TIMEOUT:$Path"
}

function Test-ContainerRunning([string]$Name) {
    $output = @(& $docker --config $DockerConfig inspect --format '{{.State.Running}}' $Name 2>$null)
    return $LASTEXITCODE -eq 0 -and @($output).Count -eq 1 -and [string]$output[0] -ceq 'true'
}

function Stop-ExactContainer([string]$Name) {
    if (Test-ContainerRunning $Name) {
        [void](Invoke-Docker @('--config', $DockerConfig, 'stop', '--time', '10', $Name) -Quiet)
    }
}

function Remove-CurrentResources {
    foreach ($name in @($script:createdContainers.ToArray())) {
        Stop-ExactContainer $name
        [void](Invoke-Docker @('--config', $DockerConfig, 'container', 'rm', $name) -Quiet)
    }
    foreach ($name in @($script:createdVolumes.ToArray())) {
        [void](Invoke-Docker @('--config', $DockerConfig, 'volume', 'rm', $name) -Quiet)
    }
}

function Get-PytestTotals([string]$Root) {
    $tests = 0L; $failures = 0L; $errors = 0L; $skipped = 0L
    foreach ($file in @(Get-ChildItem -LiteralPath $Root -Recurse -File -Filter '*.xml')) {
        try { [xml]$xml = [IO.File]::ReadAllText($file.FullName, $utf8) } catch { continue }
        $nodes = @($xml.SelectNodes('//testsuite'))
        if ($nodes.Count -eq 0) { continue }
        # Only aggregate leaf suites; top-level suites otherwise double count.
        foreach ($node in @($nodes | Where-Object { $_.SelectNodes('./testsuite').Count -eq 0 })) {
            $tests += [int64]$node.tests; $failures += [int64]$node.failures
            $errors += [int64]$node.errors; $skipped += [int64]$node.skipped
        }
    }
    return [PSCustomObject][ordered]@{ tests = $tests; failures = $failures; errors = $errors; skipped = $skipped }
}

if (-not (Test-Path -LiteralPath $claudeContext -PathType Container)) {
    Assert-E2E (Test-Path -LiteralPath $claudePackageSource -PathType Container) 'CLAUDE_NPM_SOURCE'
    [IO.Directory]::CreateDirectory($claudeContext) | Out-Null
    Copy-Item -LiteralPath $claudePackageSource -Destination (Join-Path $claudeContext 'package') -Recurse
}
$claudePackage = [IO.File]::ReadAllText((Join-Path $claudeContext 'package\package.json'), $utf8) | ConvertFrom-Json
Assert-E2E ([string]$claudePackage.name -ceq '@anthropic-ai/claude-code') 'CLAUDE_NPM_PACKAGE_NAME'
Assert-E2E ([string]$claudePackage.version -ceq '2.1.89') 'CLAUDE_NPM_PACKAGE_VERSION'
Assert-E2E ((Get-FileHash -LiteralPath (Join-Path $claudeContext 'package\cli.js') -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $claudeCliSha256) 'CLAUDE_NPM_CLI_SHA256'

foreach ($path in @($RepoRoot, $LogparseSource, $McpSource, $DockerConfig, $harnessRoot, $uvContext, $claudeContext)) {
    Assert-E2E (Test-Path -LiteralPath $path -PathType Container) "PATH_$path"
}
Assert-E2E (Test-Path -LiteralPath $SettingsPath -PathType Leaf) 'SETTINGS_PATH'
foreach ($path in @((Join-Path $uvContext 'uv'), (Join-Path $uvContext 'uvx'), (Join-Path $claudeContext 'package\package.json'), (Join-Path $claudeContext 'package\cli.js'))) {
    Assert-E2E (Test-Path -LiteralPath $path -PathType Leaf) "CACHE_$path"
}
[IO.Directory]::CreateDirectory($evidenceBase) | Out-Null
$businessReport = $null
if ($Profile -ceq 'ReleaseGates') {
    Assert-E2E (-not [string]::IsNullOrWhiteSpace($BusinessEvidenceRoot)) 'BUSINESS_EVIDENCE_REQUIRED'
    $businessPrefix = [IO.Path]::GetFullPath($evidenceBase).TrimEnd('\') + '\'
    Assert-E2E ($BusinessEvidenceRoot.StartsWith($businessPrefix, [StringComparison]::OrdinalIgnoreCase)) 'BUSINESS_EVIDENCE_SCOPE'
    foreach ($name in @('verification-report.json', 'final-secret-scan.json', 'source-input.patch', 'journey-authoritative-summary.json', 'phase1-state.json')) {
        Assert-E2E (Test-Path -LiteralPath (Join-Path $BusinessEvidenceRoot $name) -PathType Leaf) "BUSINESS_EVIDENCE_$name"
    }
    Assert-E2E (Test-Path -LiteralPath (Join-Path $BusinessEvidenceRoot 'restart\final-state-audit.json') -PathType Leaf) 'BUSINESS_EVIDENCE_STATE_AUDIT'
    $businessReport = [IO.File]::ReadAllText((Join-Path $BusinessEvidenceRoot 'verification-report.json'), $utf8) | ConvertFrom-Json
    $businessSecretScan = [IO.File]::ReadAllText((Join-Path $BusinessEvidenceRoot 'final-secret-scan.json'), $utf8) | ConvertFrom-Json
    $businessStateAudit = [IO.File]::ReadAllText((Join-Path $BusinessEvidenceRoot 'restart\final-state-audit.json'), $utf8) | ConvertFrom-Json
    Assert-E2E ([string]$businessReport.status -ceq 'PASS') 'BUSINESS_EVIDENCE_STATUS'
    Assert-E2E ([string]$businessReport.profile -ceq 'Fast') 'BUSINESS_EVIDENCE_PROFILE'
    Assert-E2E ([string]$businessReport.base_commit -ceq 'c31cc03848155d03b9a35776555e413f26b264ad') 'BUSINESS_EVIDENCE_BASE'
    Assert-E2E ([string]$businessReport.final_result -ceq 'ACCEPTED') 'BUSINESS_EVIDENCE_RESULT'
    Assert-E2E ([string]$businessReport.state_audit -ceq 'PASS') 'BUSINESS_EVIDENCE_AUDIT_REPORT'
    Assert-E2E ([string]$businessSecretScan.status -ceq 'PASS' -and [int64]$businessSecretScan.sensitive_value_occurrences -eq 0) 'BUSINESS_EVIDENCE_SECRET_SCAN'
    Assert-E2E ([string]$businessStateAudit.status -ceq 'PASS') 'BUSINESS_EVIDENCE_STATE_AUDIT_STATUS'
}
$attempt = Get-NextAttemptNumber
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$label = "attempt$attempt-$timestamp"
$evidence = Join-Path $evidenceBase $label
$initialContainer = "pl-e2e-fast$attempt-$timestamp"
$restartContainer = "pl-e2e-fast$attempt-restart-$timestamp"
$dataVolume = "pl-e2e-fast$attempt-data-$timestamp"
$releaseDetContainer = "pl-e2e-fast$attempt-release-det-$timestamp"
$releaseAgentContainer = "pl-e2e-fast$attempt-release-agent-$timestamp"
$releaseDetVolume = "pl-e2e-fast$attempt-release-det-data-$timestamp"
$releaseAgentVolume = "pl-e2e-fast$attempt-release-agent-data-$timestamp"
$baseKey = Get-BaseCacheKey
$baseImage = "problem-locator-e2e-base:$($baseKey.Substring(0,16))"

try {
    Invoke-Step 'evidence-freeze' {
        Copy-EvidenceBundle $evidence
        Copy-RestartRuntime $evidence
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $toolRoot 'freeze-source-patch.ps1') -RepoRoot $RepoRoot -EvidenceRoot $evidence
        if ($LASTEXITCODE -ne 0) { throw 'E2E_FREEZE_SCRIPT' }
        if ($Profile -ceq 'ReleaseGates') {
            $currentPatchHash = (Get-FileHash -LiteralPath (Join-Path $evidence 'source-input.patch') -Algorithm SHA256).Hash.ToLowerInvariant()
            $businessPatchPath = Join-Path $BusinessEvidenceRoot 'source-input.patch'
            $businessPatchHash = (Get-FileHash -LiteralPath $businessPatchPath -Algorithm SHA256).Hash.ToLowerInvariant()
            Assert-E2E ($businessPatchHash -ceq [string]$businessReport.patch_sha256) 'BUSINESS_EVIDENCE_PATCH_FILE'
            $currentBusinessIdentity = Get-E2EBusinessPatchIdentity -PatchPath (Join-Path $evidence 'source-input.patch')
            $previousBusinessIdentity = Get-E2EBusinessPatchIdentity -PatchPath $businessPatchPath
            Assert-E2E ($currentBusinessIdentity.sha256 -ceq $previousBusinessIdentity.sha256) 'BUSINESS_EVIDENCE_PRODUCTION_PATCH'
            Write-E2EUtf8 (Join-Path $evidence 'business-evidence-reuse.txt') ((
                "business_evidence_root=$BusinessEvidenceRoot`n" +
                "business_patch_sha256=$businessPatchHash`n" +
                "current_patch_sha256=$currentPatchHash`n" +
                "production_patch_sha256=$($currentBusinessIdentity.sha256)`n" +
                "production_file_count=$($currentBusinessIdentity.production_file_count)`n" +
                "current_total_patch_files=$($currentBusinessIdentity.total_file_count)`n" +
                "business_total_patch_files=$($previousBusinessIdentity.total_file_count)`n" +
                "full_patch_equal=$($currentPatchHash -ceq $businessPatchHash)`n" +
                "test_only_delta_allowed=true`n"
            ))
        }
        Copy-SourcePatch $evidence (Join-Path $evidence 'restart')
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $toolRoot 'write-driver-manifests.ps1') -EvidenceRoot $evidence
        if ($LASTEXITCODE -ne 0) { throw 'E2E_MANIFEST_SCRIPT' }
    }
    Invoke-Step 'base-image' { Ensure-BaseImage $baseImage (Join-Path $evidence 'base-image-build.log') }

    $warm = [Diagnostics.Stopwatch]::StartNew()
    if ($Profile -cne 'ReleaseGates') {
    Invoke-Step 'fast-environment' {
        New-E2EVolume $dataVolume $initialContainer
        New-E2EContainer $initialContainer $dataVolume $evidence $baseImage $true $null
        foreach ($entry in @(
            @('bash','setup_sources.sh'), @('bash','setup_venvs.sh'), @('bash','setup_fixtures.sh'),
            @('bash','setup_claude.sh'), @('sh','setup_nonroot_runtime.sh'),
            @('sh','verify_nonroot_python_launchers.sh'), @('sh','gate_target.sh'),
            @('sh','gate_real_logparse.sh')
        )) { Invoke-ContainerScript $initialContainer $entry[0] $entry[1] }
    }
    Invoke-Step 'fast-service-preflight' {
        Start-ContainerScript $initialContainer 'sh' 'start_service_supervisor.sh'
        Invoke-ContainerScript $initialContainer 'sh' 'gate_service_preflight.sh'
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $evidence 'windows-service-preflight.ps1') -EvidenceRoot $evidence
        if ($LASTEXITCODE -ne 0) { throw 'E2E_WINDOWS_PREFLIGHT' }
    }
    Invoke-Step 'fast-windows-claude-phase1' {
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $evidence 'run-windows-journey.ps1') -Mode Phase1 -EvidenceRoot $evidence
        if ($LASTEXITCODE -ne 0) { throw 'E2E_WINDOWS_PHASE1' }
    }
    Invoke-Step 'fast-real-upload' {
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $evidence 'run-windows-journey.ps1') -Mode Upload -EvidenceRoot $evidence
        if ($LASTEXITCODE -ne 0) { throw 'E2E_WINDOWS_UPLOAD' }
    }
    Invoke-Step 'fast-windows-claude-phase3' {
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $evidence 'run-windows-journey.ps1') -Mode Phase3 -EvidenceRoot $evidence
        if ($LASTEXITCODE -ne 0) { throw 'E2E_WINDOWS_PHASE3' }
    }
    Invoke-Step 'fast-before-restart-audit' {
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $evidence 'run-windows-http-capture.ps1') -EvidenceRoot $evidence -Phase Before
        if ($LASTEXITCODE -ne 0) { throw 'E2E_HTTP_BEFORE' }
        Invoke-ContainerScript $initialContainer 'sh' 'stop_service.sh'
        Invoke-ContainerScript $initialContainer 'sh' 'capture_state_before_restart.sh'
        Stop-ExactContainer $initialContainer
    }
    Invoke-Step 'fast-restart-environment' {
        $restartEvidence = Join-Path $evidence 'restart'
        New-E2EContainer $restartContainer $dataVolume $restartEvidence $baseImage $true $evidence
        foreach ($entry in @(
            @('bash','setup_sources.sh'), @('bash','setup_venvs.sh'), @('bash','setup_fixtures.sh'),
            @('bash','setup_claude.sh'), @('sh','restart_nonroot_runtime_init.sh'),
            @('sh','verify_restart_nonempty_runtime.sh')
        )) { Invoke-ContainerScript $restartContainer $entry[0] $entry[1] }
        Start-ContainerScript $restartContainer 'sh' 'start_service_supervisor.sh'
        Invoke-ContainerScript $restartContainer 'sh' 'gate_service_preflight.sh'
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $restartEvidence 'windows-service-preflight.ps1') -EvidenceRoot $restartEvidence
        if ($LASTEXITCODE -ne 0) { throw 'E2E_RESTART_PREFLIGHT' }
    }
    Invoke-Step 'fast-restart-persistence' {
        $restartEvidence = Join-Path $evidence 'restart'
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $restartEvidence 'run-windows-restart-verify.ps1') -EvidenceRoot $evidence
        if ($LASTEXITCODE -ne 0) { throw 'E2E_RESTART_QUERY' }
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $restartEvidence 'download-windows-restart-artifact.ps1') -EvidenceRoot $evidence
        if ($LASTEXITCODE -ne 0) { throw 'E2E_RESTART_DOWNLOAD' }
        & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $evidence 'run-windows-http-capture.ps1') -EvidenceRoot $evidence -Phase After
        if ($LASTEXITCODE -ne 0) { throw 'E2E_HTTP_AFTER' }
        Invoke-ContainerScript $restartContainer 'sh' 'stop_service.sh'
        Invoke-ContainerScript $restartContainer 'sh' 'capture_state_after_restart.sh'
        Invoke-ContainerScript $restartContainer 'sh' 'run_final_audits.sh'
        Invoke-ContainerScript $restartContainer 'sh' 'capture_linux_identity.sh'
        Stop-ExactContainer $restartContainer
    }
    }

    if ($Profile -in @('Release', 'ReleaseGates')) {
        Invoke-Step 'release-parallel-setup' {
            $releaseRoot = Join-Path $evidence 'release'
            $detEvidence = Join-Path $releaseRoot 'deterministic'
            $agentEvidence = Join-Path $releaseRoot 'agents'
            Copy-EvidenceBundle $detEvidence
            Copy-EvidenceBundle $agentEvidence
            Copy-SourcePatch $evidence $detEvidence
            Copy-SourcePatch $evidence $agentEvidence
            New-E2EVolume $releaseDetVolume $releaseDetContainer
            New-E2EVolume $releaseAgentVolume $releaseAgentContainer
            New-E2EContainer $releaseDetContainer $releaseDetVolume $detEvidence $baseImage $false $null
            New-E2EContainer $releaseAgentContainer $releaseAgentVolume $agentEvidence $baseImage $false $null
            Start-ContainerScript $releaseDetContainer 'sh' 'setup_release_environment.sh'
            Start-ContainerScript $releaseAgentContainer 'sh' 'setup_release_environment.sh'
            Wait-StatusFile (Join-Path $detEvidence 'release-setup.status') 120
            Wait-StatusFile (Join-Path $agentEvidence 'release-setup.status') 120
        }
        Invoke-Step 'release-parallel-gates' {
            $detEvidence = Join-Path $evidence 'release\deterministic'
            $agentEvidence = Join-Path $evidence 'release\agents'
            Start-ContainerScript $releaseDetContainer 'sh' 'run_release_group.sh' @('deterministic')
            Start-ContainerScript $releaseAgentContainer 'sh' 'run_release_group.sh' @('agents')
            Wait-StatusFile (Join-Path $detEvidence 'release-deterministic.status') 360
            Wait-StatusFile (Join-Path $agentEvidence 'release-agents.status') 360
        }
    }
    $warm.Stop()
    $script:warmSeconds = [Math]::Round($warm.Elapsed.TotalSeconds, 3)
    if ($Profile -in @('Release', 'ReleaseGates')) { Assert-E2E ($warm.Elapsed.TotalSeconds -le 480) 'RELEASE_SLA_EXCEEDED' }
    $script:success = $true
}
catch {
    $script:failure = $_
}

if (-not $script:success) {
    foreach ($name in @($script:createdContainers.ToArray())) {
        try { Stop-ExactContainer $name } catch {}
    }
}

$summaryPath = Join-Path $evidence 'verification-report.json'
$businessRoot = if ($Profile -ceq 'ReleaseGates') { $BusinessEvidenceRoot } else { $evidence }
$journey = if (Test-Path -LiteralPath (Join-Path $businessRoot 'journey-authoritative-summary.json')) { [IO.File]::ReadAllText((Join-Path $businessRoot 'journey-authoritative-summary.json'), $utf8) | ConvertFrom-Json } else { $null }
$phase1 = if (Test-Path -LiteralPath (Join-Path $businessRoot 'phase1-state.json')) { [IO.File]::ReadAllText((Join-Path $businessRoot 'phase1-state.json'), $utf8) | ConvertFrom-Json } else { $null }
$audit = if (Test-Path -LiteralPath (Join-Path $businessRoot 'restart\final-state-audit.json')) { [IO.File]::ReadAllText((Join-Path $businessRoot 'restart\final-state-audit.json'), $utf8) | ConvertFrom-Json } else { $null }
$sourcePinsPath = if ($Profile -ceq 'ReleaseGates') { Join-Path $evidence 'release\deterministic\source-pins.txt' } else { Join-Path $evidence 'source-pins.txt' }
$logparseCommit = $null
$logparseSourceKind = $null
if (Test-Path -LiteralPath $sourcePinsPath -PathType Leaf) {
    $sourcePinsText = [IO.File]::ReadAllText($sourcePinsPath, $utf8)
    $logparseMatch = [regex]::Match($sourcePinsText, '(?m)^logparse=([0-9a-f]{40,64})\r?$')
    if ($logparseMatch.Success) { $logparseCommit = $logparseMatch.Groups[1].Value }
    $sourceKindMatch = [regex]::Match($sourcePinsText, '(?m)^logparse_source_kind=(git|directory)\r?$')
    if ($sourceKindMatch.Success) { $logparseSourceKind = $sourceKindMatch.Groups[1].Value }
}
$patchSha = if (Test-Path -LiteralPath (Join-Path $evidence 'source-input.patch')) { (Get-FileHash -LiteralPath (Join-Path $evidence 'source-input.patch') -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null }
$pytest = Get-PytestTotals $evidence
$report = [PSCustomObject][ordered]@{
    schema_version = 1
    status = $(if ($script:success) { 'PASS' } else { 'FAIL' })
    profile = $Profile
    attempt = $label
    failed_stage = $(if ($script:success) { $null } else { $script:stage })
    failure_code = $(if ($script:success) { $null } else { $script:failure.Exception.Message })
    base_commit = 'c31cc03848155d03b9a35776555e413f26b264ad'
    patch_sha256 = $patchSha
    logparse_source_kind = $logparseSourceKind
    logparse_commit = $logparseCommit
    problem_locator_mcp_commit = '97d0446580f49e7b1add1c5fc6d6a41c97884884'
    ubuntu_digest = 'sha256:3131b4cc82a783df6c9df078f86e01819a13594b865c2cad47bd1bca2b7063bb'
    base_image = $baseImage
    base_image_cache_hit = $script:baseCacheHit
    python_version = '3.12.13'
    uv_version = '0.11.32'
    claude_version = '2.1.89'
    model = 'deepseek-v4-flash[1m]'
    no_mock_business_journey = $true
    business_evidence_attempt = $(if ($null -ne $businessReport) { [string]$businessReport.attempt } else { $label })
    business_elapsed_seconds = $(if ($null -ne $businessReport) { [double]$businessReport.warm_elapsed_seconds } else { $script:warmSeconds })
    warm_release_sla_seconds = 480
    warm_elapsed_seconds = $script:warmSeconds
    pytest = $pytest
    case_id = $(if ($null -ne $journey) { $journey.case_id } else { $null })
    artifact_id = $(if ($null -ne $journey) { $journey.public_artifact.artifact_id } else { $null })
    result_sha256 = $(if ($null -ne $journey) { $journey.public_artifact.sha256 } else { $null })
    final_result = $(if ($null -ne $journey) { $journey.final_result.status } else { $null })
    validation_correction_count = $(
        $(if ($null -ne $phase1 -and $null -ne $phase1.validation_corrections) { @($phase1.validation_corrections).Count } else { 0 }) +
        $(if ($null -ne $journey -and $null -ne $journey.validation_corrections) { @($journey.validation_corrections).Count } else { 0 })
    )
    state_audit = $(if ($null -ne $audit) { $audit.status } else { $null })
    resources_preserved_on_failure = (-not $script:success)
    timings = [object[]]$script:timings.ToArray()
}

if ($script:success) {
    Invoke-Step 'cleanup-current-run' { Remove-CurrentResources }
    Write-E2EJson (Join-Path $evidence 'cleanup-receipt.json') ([PSCustomObject][ordered]@{
        status = 'PASS'; containers_removed = [object[]]$script:createdContainers.ToArray(); volumes_removed = [object[]]$script:createdVolumes.ToArray()
    })
}
Write-E2EJson $summaryPath $report

try {
    & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $evidence 'scan-final-evidence.ps1') -EvidenceRoot $evidence -SettingsPath $SettingsPath -OutputName final-secret-scan.json
    if ($LASTEXITCODE -ne 0) { throw 'E2E_FINAL_SECRET_SCAN' }
}
catch {
    if ($script:success) { throw }
}

if (-not $script:success) {
    throw $script:failure
}
Write-Output "E2E_RELEASE_PASSED evidence=$evidence elapsed=$($script:warmSeconds)s"
