param([string]$DriverRoot = $PSScriptRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DriverRoot = [System.IO.Path]::GetFullPath($DriverRoot)
$files = @(
    'windows-restart-lib.ps1',
    'run-windows-restart-verify.ps1',
    'download-windows-restart-artifact.ps1',
    'static-check-restart.ps1',
    'README-restart.md'
)
foreach ($name in $files) {
    $path = Join-Path $DriverRoot $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "missing restart driver file: $name"
    }
}

$restartResourceFiles = @(
    'create-restart-docker-resources.ps1',
    'verify-restart-docker-metadata.ps1',
    'test-restart-docker-resources.ps1',
    'README-restart-docker-resources.md'
)
foreach ($name in $restartResourceFiles) {
    $path = Join-Path $DriverRoot $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "missing frozen restart Docker resource file: $name"
    }
}

$parseErrors = @()
foreach ($name in @(
    'windows-restart-lib.ps1',
    'run-windows-restart-verify.ps1',
    'download-windows-restart-artifact.ps1',
    'static-check-restart.ps1',
    'create-restart-docker-resources.ps1',
    'verify-restart-docker-metadata.ps1',
    'test-restart-docker-resources.ps1'
)) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $DriverRoot $name), [ref]$tokens, [ref]$errors)
    $parseErrors += @($errors)
}
if ($parseErrors.Count -ne 0) {
    throw ('PowerShell parse errors: ' + (($parseErrors | ForEach-Object { $_.Message }) -join '; '))
}

$restartCreatorText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'create-restart-docker-resources.ps1'))
$restartMetadataText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'verify-restart-docker-metadata.ps1'))
$restartResourceTestText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'test-restart-docker-resources.ps1'))
$restartResourceReadmeText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'README-restart-docker-resources.md'))
$restartResourceText = $restartCreatorText + "`n" + $restartMetadataText + "`n" + $restartResourceTestText + "`n" + $restartResourceReadmeText
foreach ($literal in @(
    "`$script:RestartDockerInitialContainerName = 'pl-e2e-fix52-20260802-205054'",
    "`$script:RestartDockerContainerName = 'pl-e2e-fix52-restart-20260802-205054'",
    "`$script:RestartDockerVolumeName = 'pl-e2e-fix52-data-20260802-205054'",
    "`$script:RestartMetadataInitialContainerName = 'pl-e2e-fix52-20260802-205054'",
    "`$script:RestartMetadataContainerName = 'pl-e2e-fix52-restart-20260802-205054'",
    "`$script:RestartMetadataVolumeName = 'pl-e2e-fix52-data-20260802-205054'",
    'ubuntu@sha256:3131b4cc82a783df6c9df078f86e01819a13594b865c2cad47bd1bca2b7063bb',
    'sha256:3131b4cc82a783df6c9df078f86e01819a13594b865c2cad47bd1bca2b7063bb',
    "'container', 'stop', '--timeout', '10'",
    'problem-locator.e2e.owner',
    'bounded docker stop of the initial container',
    'docker-resource-receipt.json',
    'docker-metadata-receipt.json',
    'secret-scan.json',
    'restart-docker-resource-receipt.json',
    'restart-docker-metadata-receipt.json',
    'restart-docker-metadata-secret-scan.json',
    'CREATOR_CONTAINER_ID_BINDING',
    'RAW_NATIVE_SECRET_HIT',
    'DATA_ROOT_FIND_COUNT',
    'HOST_MOUNT_COUNT',
    'Get-RestartDockerMetadataOrdinalDictionaryValue',
    'Get-RestartDockerMetadataSettingsValues',
    'Get-RestartDockerMetadataReadOnlyState',
    "'SETTINGS_ROOT_ENV'",
    '$settingsSerializer.DeserializeObject($validSettingsJson)',
    'case-wrong ENV',
    'non-string root key',
    'duplicate exact env key',
    'false-target explicit ReadOnly true',
    'read-only target missing ReadOnly',
    'mount_count = 10',
    'initial_container_stopped',
    'restart_container_created',
    'initial_container_present_after_failure',
    'restart_container_present_after_failure',
    'volume_present_after_failure',
    'da15297d6879b2cfbe5ea3cb03725c1613d51ba72892cc996468d871f0a532fb',
    '31e409e837c16cbe9bdfd6534a1e2f6a774d937988027a4f0736ab52c7b6864d',
    '6c086a0f5fbf684d4148bb69629268b4f5109498c1a7be757acf18c51fd04f4b'
)) {
    if (-not $restartResourceText.Contains($literal)) {
        throw "restart Docker resource literal absent: $literal"
    }
}
if ($restartResourceText -match "'--time'|--time\s+10") {
    throw 'restart Docker resource suite must use docker stop --timeout 10, never --time'
}
if ($restartCreatorText -match '(?i)Invoke-Expression|Start-Process|cmd\.exe') {
    throw 'restart Docker creator contains forbidden command-string or shell indirection'
}
if ($restartMetadataText -match '\.Contains\(\s*[''"]env[''"]\s*\)') {
    throw 'restart metadata verifier must enumerate IDictionary.Keys instead of using overload-prone Contains(env)'
}
foreach ($settingsLiteral in @(
    'foreach ($candidateKey in @($Dictionary.Keys))',
    '[string]::Equals([string]$candidateKey, $TargetKey, [StringComparison]::Ordinal)',
    'Assert-RestartDockerMetadata ($candidateKey -is [string]) "$Code`_KEY_TYPE"',
    'Assert-RestartDockerMetadata ($ordinalMatchCount -eq 1) "$Code`_KEY_COUNT"',
    'return $Dictionary[$ordinalMatchedKey]'
)) {
    if (-not $restartMetadataText.Contains($settingsLiteral)) {
        throw "restart metadata ordinal settings lookup literal absent: $settingsLiteral"
    }
}
foreach ($textAndName in @(
    [PSCustomObject]@{ Text = $restartCreatorText; Name = 'creator' },
    [PSCustomObject]@{ Text = $restartMetadataText; Name = 'metadata verifier' }
)) {
    foreach ($pathTypeLiteral in @(
        "Resolve-RestartDockerCreateHostPath `$UvPath File",
        "Resolve-RestartDockerCreateHostPath `$UvxPath File",
        "Resolve-RestartDockerCreateHostPath `$ClaudePath File",
        "Resolve-RestartDockerMetadataHostPath `$UvPath File",
        "Resolve-RestartDockerMetadataHostPath `$UvxPath File",
        "Resolve-RestartDockerMetadataHostPath `$ClaudePath File"
    )) {
        if ($textAndName.Text.Contains($pathTypeLiteral)) { continue }
        if (($textAndName.Name -ceq 'creator' -and $pathTypeLiteral.Contains('Metadata')) -or
            ($textAndName.Name -ceq 'metadata verifier' -and $pathTypeLiteral.Contains('Create'))) { continue }
        throw "restart Docker $($textAndName.Name) does not require executable cache PathType File: $pathTypeLiteral"
    }
}
$restartRawScanIndex = $restartMetadataText.IndexOf("Assert-RestartDockerMetadata (`$rawHits -eq 0) 'RAW_NATIVE_SECRET_HIT'", [StringComparison]::Ordinal)
$restartNativeParseIndex = $restartMetadataText.IndexOf('$containerArray = @($containerRaw | ConvertFrom-Json)', [StringComparison]::Ordinal)
if (-not (0 -le $restartRawScanIndex -and $restartRawScanIndex -lt $restartNativeParseIndex)) {
    throw 'restart raw Docker/find secret scan must precede native metadata JSON parsing'
}
$restartCreatorReceiptParseIndex = $restartMetadataText.IndexOf('$creatorReceipt = $creatorRaw | ConvertFrom-Json', [StringComparison]::Ordinal)
$restartContractIndex = $restartMetadataText.IndexOf('$receipt = Assert-RestartDockerMetadataContract', [StringComparison]::Ordinal)
if (-not (0 -le $restartCreatorReceiptParseIndex -and $restartCreatorReceiptParseIndex -lt $restartRawScanIndex -and
    $restartNativeParseIndex -lt $restartContractIndex)) {
    throw 'restart metadata verifier receipt/native parse and contract order is invalid'
}

$restartExecutionText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'execution-order.restart.txt'))
$restartStep03 = @($restartExecutionText -split "`r?`n" | Where-Object { $_.StartsWith('03=', [StringComparison]::Ordinal) })
$restartStep04 = @($restartExecutionText -split "`r?`n" | Where-Object { $_.StartsWith('04=', [StringComparison]::Ordinal) })
$restartStep05 = @($restartExecutionText -split "`r?`n" | Where-Object { $_.StartsWith('05=', [StringComparison]::Ordinal) })
if ($restartStep03.Count -ne 1 -or $restartStep04.Count -ne 1 -or $restartStep05.Count -ne 1) {
    throw 'restart execution order must contain exactly one step 03, 04, and 05'
}
foreach ($literal in @(
    'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\restart\create-restart-docker-resources.ps1',
    '-InitialContainerName pl-e2e-fix52-20260802-205054',
    '-RestartContainerName pl-e2e-fix52-restart-20260802-205054',
    '-VolumeName pl-e2e-fix52-data-20260802-205054',
    '-DockerConfig C:\Users\admin\.docker',
    '-SettingsPath C:\Users\admin\.claude\settings.json',
    '-UvPath D:\code\xiaodao\.tmp\pl-e2e-cache\uv-0.11.32\uv',
    '-UvxPath D:\code\xiaodao\.tmp\pl-e2e-cache\uv-0.11.32\uvx',
    '-ClaudePath D:\code\xiaodao\.tmp\pl-e2e-cache\claude-2.1.150\claude'
)) {
    if (-not $restartStep03[0].Contains($literal)) { throw "restart step03 frozen creator literal absent: $literal" }
}
if ($restartStep04[0] -cne '04=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_orchestration_hashes.sh ; require all 43 restart manifest entries and every main orchestration manifest entry to verify') {
    throw 'restart step04 must be the frozen in-container orchestration hash gate'
}
foreach ($literal in @(
    'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\restart\verify-restart-docker-metadata.ps1',
    '-InitialContainerName pl-e2e-fix52-20260802-205054',
    '-RestartContainerName pl-e2e-fix52-restart-20260802-205054',
    '-VolumeName pl-e2e-fix52-data-20260802-205054',
    '-DockerConfig C:\Users\admin\.docker',
    '-SettingsPath C:\Users\admin\.claude\settings.json',
    '-UvPath D:\code\xiaodao\.tmp\pl-e2e-cache\uv-0.11.32\uv',
    '-UvxPath D:\code\xiaodao\.tmp\pl-e2e-cache\uv-0.11.32\uvx',
    '-ClaudePath D:\code\xiaodao\.tmp\pl-e2e-cache\claude-2.1.150\claude'
)) {
    if (-not $restartStep05[0].Contains($literal)) { throw "restart step05 frozen metadata verifier literal absent: $literal" }
}
foreach ($step in @($restartStep03[0], $restartStep05[0])) {
    if ($step -match '(?i)(?:-Command\b|Invoke-Expression|docker\.exe|node\.exe|uv-cache-attempt|claude-cache-attempt)') {
        throw 'restart frozen host resource step contains inline execution or attempt-specific cache behavior'
    }
}
if (-not $restartStep03[0].Contains('sandbox-external host permission') -or
    -not $restartStep05[0].Contains('sandbox-external host permission')) {
    throw 'restart Docker and settings host permission boundary is not explicit'
}
$expectedRestartRuntime = @(
    '04=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_orchestration_hashes.sh ; require all 43 restart manifest entries and every main orchestration manifest entry to verify',
    '06=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_shell_syntax.sh',
    '07=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/bootstrap_apt.sh',
    '08.1=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/uv_mount_preflight.sh',
    '08.2=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_uv_preflight.sh',
    '09=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/bootstrap_uv.sh',
    '10=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/setup_sources.sh',
    '11=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/setup_venvs.sh',
    '12=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_python_syntax.sh',
    '13=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/setup_fixtures.sh',
    '14=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 bash /evidence/setup_claude.sh',
    '15=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/restart_nonroot_runtime_init.sh ; require nonroot-restart-data-root-before.json, nonroot-restart-data-root-after.json, and nonroot-restart-init.txt',
    '16=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/verify_restart_nonempty_runtime.sh ; require nonroot-logparse-catalog-verification.json with PASS and restart-nonempty-runtime-verification.txt',
    '17=docker.exe --config C:\Users\admin\.docker exec --detach pl-e2e-fix52-restart-20260802-205054 sh /evidence/start_service_supervisor.sh ; require service-supervisor-launch.txt',
    '18.1=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/gate_service_preflight.sh ; require service-process-isolation.json and service-preflight.json with PASS',
    '18.2=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\restart\windows-service-preflight.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\restart ; require restart\windows-live-ready-preflight.json with five passing readiness checks',
    '19.1=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\restart\run-windows-restart-verify.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 ; require windows-restart-claude-version.stdout.txt, windows-restart-claude-version.stderr.txt, restart.prompt.txt, restart.stream-json.stdout.ndjson, restart.stderr.txt, restart.authoritative.json, and restart-authoritative-summary.json',
    '19.2=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\restart\download-windows-restart-artifact.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 ; require restart-download.curl.stdout.txt, restart-download.curl.stderr.txt, restart-download.response.headers.txt, restart-diagnosis-result.json, and restart-download-verification.json',
    '19.3=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-http-capture.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 -Phase After ; require diagnosis-result.after.json, diagnosis-result.after.headers, diagnosis-result.after.meta.json, diagnosis-result.after.curl.stdout.txt, diagnosis-result.after.curl.stderr.txt, internal-logparse.after.headers, internal-logparse.after.meta.json, internal-logparse.after.body.json, internal-logparse.after.curl.stdout.txt, and internal-logparse.after.curl.stderr.txt',
    '20=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/stop_service.sh ; require service-exit-status.txt with exit 143, service-log-secret-scan.json with zero hits, service.log, and service-stop-verification.txt',
    '21.1=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/capture_state_after_restart.sh ; require validate-state.after.json, state-export.after.json, and state-admin-after-restart.txt',
    '21.2=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/run_final_audits.sh ; require final-state-audit.json and http-artifact-audit.json with PASS plus final-audit-gate.txt',
    '21.3=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-restart-20260802-205054 sh /evidence/capture_linux_identity.sh ; require linux-identity.json with status PASS, id ubuntu, nonempty version_id and pretty_name, and uname_machine x86_64',
    '22=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\finalize-attempt52.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 -DockerConfig C:\Users\admin\.docker -SettingsPath C:\Users\admin\.claude\settings.json ; require validation-report.json PASS, pre-cleanup-secret-scan.json PASS with zero hits, cleanup-authorization.json AUTHORIZED, cleanup-receipt.json PASS with every exact allowlisted present resource absent, final-verification-report.json PASS, and final-secret-scan.json PASS with zero hits as the absolute final evidence write'
)
$actualRestartRuntime = @($restartExecutionText -split "`r?`n" | Where-Object { $_ -cmatch '^(?:04|06|07|08\.[12]|09|10|11|12|13|14|15|16|17|18\.[12]|19\.[123]|20|21\.[123]|22)=' })
if ($actualRestartRuntime.Count -ne $expectedRestartRuntime.Count) { throw 'restart runtime matrix count mismatch' }
for ($index = 0; $index -lt $expectedRestartRuntime.Count; $index++) {
    if ($actualRestartRuntime[$index] -cne $expectedRestartRuntime[$index]) {
        throw "restart runtime matrix mismatch at index $index"
    }
}
$restartReadmeText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'README-restart.md'))
foreach ($line in $expectedRestartRuntime) {
    $command = ($line.Substring($line.IndexOf('=') + 1)) -replace ' ; require .+$', ''
    if (-not $restartReadmeText.Contains($command)) { throw "restart README runtime command absent: $command" }
}
$linuxIdentityPath = Join-Path $DriverRoot 'capture_linux_identity.sh'
if (-not (Test-Path -LiteralPath $linuxIdentityPath -PathType Leaf)) { throw 'Linux identity capture script absent' }
$linuxIdentityText = [IO.File]::ReadAllText($linuxIdentityPath)
foreach ($literal in @(
    'platform.freedesktop_os_release()',
    'platform.machine()',
    '"id": release.get("ID", "")',
    '"version_id": release.get("VERSION_ID", "")',
    '"pretty_name": release.get("PRETTY_NAME", "")',
    'os.O_EXCL',
    'os.O_NOFOLLOW',
    '/evidence/linux-identity.json',
    '0:0:644'
)) {
    if (-not $linuxIdentityText.Contains($literal)) { throw "Linux identity capture literal absent: $literal" }
}
foreach ($pattern in @('(?i)curl|(?i)wget|(?i)Invoke-Expression|(?i)docker|(?i)https?://')) {
    if ($linuxIdentityText -match $pattern) { throw "Linux identity capture forbidden behavior: $pattern" }
}
. (Join-Path $DriverRoot 'windows-restart-lib.ps1')

$restartPropertyProbe = '{"empty":[],"singleton":[{"id":"only"}],"multi":["first","second"],"scalar":"scalar"}' | ConvertFrom-Json
$restartEmpty = Get-RestartProperty $restartPropertyProbe 'empty' -Required
$restartSingleton = Get-RestartProperty $restartPropertyProbe 'singleton' -Required
$restartMulti = Get-RestartProperty $restartPropertyProbe 'multi' -Required
$restartScalar = Get-RestartProperty $restartPropertyProbe 'scalar' -Required
if ($restartEmpty -isnot [System.Array] -or @($restartEmpty).Count -ne 0 -or
    -not [object]::ReferenceEquals($restartEmpty, $restartPropertyProbe.PSObject.Properties['empty'].Value)) {
    throw 'Get-RestartProperty must preserve an empty JSON array'
}
if ($restartSingleton -isnot [System.Array] -or @($restartSingleton).Count -ne 1 -or $restartSingleton[0].id -cne 'only' -or
    -not [object]::ReferenceEquals($restartSingleton, $restartPropertyProbe.PSObject.Properties['singleton'].Value)) {
    throw 'Get-RestartProperty must preserve a singleton JSON array'
}
if ($restartMulti -isnot [System.Array] -or @($restartMulti).Count -ne 2 -or $restartMulti[0] -cne 'first' -or $restartMulti[1] -cne 'second' -or
    -not [object]::ReferenceEquals($restartMulti, $restartPropertyProbe.PSObject.Properties['multi'].Value)) {
    throw 'Get-RestartProperty must preserve a multi-element JSON array'
}
if ($restartScalar -isnot [string] -or $restartScalar -cne 'scalar') {
    throw 'Get-RestartProperty must preserve a JSON scalar'
}

function Invoke-RestartUserDispositionCase {
    param([object[]]$Blocks, [bool]$IncludeTopLevelResult)
    $message = [PSCustomObject][ordered]@{ role = 'user'; content = $Blocks }
    $event = [PSCustomObject][ordered]@{ type = 'user'; message = $message }
    if ($IncludeTopLevelResult) {
        $event | Add-Member -NotePropertyName tool_use_result -NotePropertyValue ([PSCustomObject][ordered]@{ marker = 'present' })
    }
    return Get-RestartUserContentDisposition -Event $event -Message $message -Content $Blocks
}

function Assert-RestartUserDispositionFails {
    param([object[]]$Blocks, [bool]$IncludeTopLevelResult, [string]$Label)
    $failed = $false
    try {
        [void](Invoke-RestartUserDispositionCase -Blocks $Blocks -IncludeTopLevelResult $IncludeTopLevelResult)
    }
    catch {
        $failed = $true
    }
    Assert-Restart $failed $Label
}

$emptyBlocks = [object[]]@()
$emptyText = [PSCustomObject][ordered]@{ type = 'text'; text = '' }
$ordinaryText = [PSCustomObject][ordered]@{ type = 'text'; text = 'status update' }
$toolResultOne = [PSCustomObject][ordered]@{ type = 'tool_result'; tool_use_id = 'toolu_static_1' }
$toolResultTwo = [PSCustomObject][ordered]@{ type = 'tool_result'; tool_use_id = 'toolu_static_2' }
Assert-Restart ((Invoke-RestartUserDispositionCase -Blocks $emptyBlocks -IncludeTopLevelResult $false) -ceq 'ignore_text') 'empty user content must be ignored'
Assert-Restart ((Invoke-RestartUserDispositionCase -Blocks @($emptyText) -IncludeTopLevelResult $false) -ceq 'ignore_text') 'empty text block must be ignored'
Assert-Restart ((Invoke-RestartUserDispositionCase -Blocks @($emptyText, $ordinaryText) -IncludeTopLevelResult $false) -ceq 'ignore_text') 'ordinary text blocks must be ignored'
Assert-Restart ((Invoke-RestartUserDispositionCase -Blocks @($toolResultOne) -IncludeTopLevelResult $true) -ceq 'tool_result') 'unique tool_result must remain authoritative'
Assert-RestartUserDispositionFails -Blocks @($ordinaryText, $toolResultOne) -IncludeTopLevelResult $true -Label 'mixed text and tool_result must fail closed'
Assert-RestartUserDispositionFails -Blocks @($toolResultOne, $toolResultTwo) -IncludeTopLevelResult $true -Label 'multiple tool_result blocks must fail closed'
Assert-RestartUserDispositionFails -Blocks @($ordinaryText) -IncludeTopLevelResult $true -Label 'top-level result without content tool_result must fail closed'
Assert-RestartUserDispositionFails -Blocks @($toolResultOne) -IncludeTopLevelResult $false -Label 'content tool_result without top-level result must fail closed'

$mainRoot = [System.IO.Path]::GetFullPath((Join-Path $DriverRoot '..'))
$coreNames = @('verify_service_process.py', 'start_service_supervisor.sh', 'stop_service.sh')
foreach ($name in $coreNames) {
    $mainPath = Join-Path $mainRoot $name
    $restartPath = Join-Path $DriverRoot $name
    $mainHash = (Get-FileHash -LiteralPath $mainPath -Algorithm SHA256).Hash
    $restartHash = (Get-FileHash -LiteralPath $restartPath -Algorithm SHA256).Hash
    if ($mainHash -cne $restartHash) {
        throw "main/restart service-isolation core mismatch: $name"
    }
}
$verifierText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'verify_service_process.py'))
$supervisorText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'start_service_supervisor.sh'))
$stopText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'stop_service.sh'))
$coreText = $verifierText + "`n" + $supervisorText + "`n" + $stopText
foreach ($literal in @(
    'os.fork()', 'os.setgroups([])', 'os.setgid(SERVICE_GID)', 'os.setuid(SERVICE_UID)',
    'os.getresuid()', 'os.getresgid()', 'os.getgroups() == []', 'MAX_PIPE_BYTES = 8192',
    'select.select([read_fd]', 'os.waitpid(child_pid, os.WNOHANG)',
    'deadline = time.monotonic() + 1.0', 'SERVICE_CHILD_REAP_TIMEOUT',
    'info.st_uid == 0 and info.st_gid == 0', 'stat.S_IMODE(info.st_mode) == 0o600',
    'os.O_EXCL', 'os.O_NOFOLLOW', 'os.fstat(fd)', 'os.fsync(fd)',
    'canonical_bytes({"ok": True, "summary": summary})',
    'SERVICE_CHILD_VERIFICATION_FAILED', 'environment_key_count',
    'os.pidfd_open(int(pid_text), 0)',
    'signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)',
    'select.select([pidfd], [], [], TERMINATE_TIMEOUT_SECONDS)',
    'ARCHIVED_SERVICE_LOG_FILE = Path("/evidence/service.log")',
    'def archive_service_log() -> None:', 'elif mode == "archive-log" and len(args) == 1:'
)) {
    if (-not $verifierText.Contains($literal)) {
        throw "restart service verifier self-check literal absent: $literal"
    }
}
$pidfdOpenIndex = $verifierText.IndexOf('pidfd = os.pidfd_open(int(pid_text), 0)', [System.StringComparison]::Ordinal)
$sameUidInspectIndex = $verifierText.IndexOf('inspect_via_same_uid_child(pid_text, expected_starttime, close_in_child=pidfd)', [System.StringComparison]::Ordinal)
$pidfdSignalIndex = $verifierText.IndexOf('signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)', [System.StringComparison]::Ordinal)
$pidfdWaitIndex = $verifierText.IndexOf('select.select([pidfd], [], [], TERMINATE_TIMEOUT_SECONDS)', [System.StringComparison]::Ordinal)
if (-not (0 -le $pidfdOpenIndex -and $pidfdOpenIndex -lt $sameUidInspectIndex -and
    $sameUidInspectIndex -lt $pidfdSignalIndex -and $pidfdSignalIndex -lt $pidfdWaitIndex)) {
    throw 'restart pidfd termination order must be open, same-UID inspect, signal, bounded wait'
}
foreach ($literal in @(
    'Started server process [', 'Application startup complete.', 'Shutting down',
    'Application shutdown complete.', 'Finished server process [', 'started_count != 1',
    'started_line < startup_line', 'shutdown_line < finished_line',
    'cleanup_probe" -lt 300', 'cleanup_state" = Z', 'kill -KILL "$service_pid"',
    "trap 'on_supervisor_exit `$?' EXIT", "trap 'on_supervisor_signal 129' HUP",
    "trap 'on_supervisor_signal 130' INT", "trap 'on_supervisor_signal 143' TERM",
    'trap - EXIT HUP INT TERM', 'test "$service_status" -eq 143',
    'verify_service_process.py archive-log', 'verify_service_process.py exit "$service_status"'
)) {
    if (-not $supervisorText.Contains($literal)) {
        throw "restart service supervisor self-check literal absent: $literal"
    }
}
$scanIndex = $supervisorText.IndexOf('scan_service_log_secrets.py', [System.StringComparison]::Ordinal)
$lifecycleIndex = $supervisorText.IndexOf('awk -v pid="$service_pid"', [System.StringComparison]::Ordinal)
$status143Index = $supervisorText.IndexOf('test "$service_status" -eq 143', [System.StringComparison]::Ordinal)
$archiveIndex = $supervisorText.IndexOf('verify_service_process.py archive-log', [System.StringComparison]::Ordinal)
$exitReceiptIndex = $supervisorText.IndexOf('verify_service_process.py exit "$service_status"', [System.StringComparison]::Ordinal)
if (-not (0 -le $scanIndex -and $scanIndex -lt $lifecycleIndex -and
    $lifecycleIndex -lt $status143Index -and $status143Index -lt $archiveIndex -and
    $archiveIndex -lt $exitReceiptIndex)) {
    throw 'restart shutdown evidence order must be scan, lifecycle, status 143, secure archive, exit receipt'
}
foreach ($literal in @(
    'verify_service_process.py terminate', 'service-exit-status.txt',
    'verify_service_process.py stop'
)) {
    if (-not $stopText.Contains($literal)) {
        throw "restart service stop self-check literal absent: $literal"
    }
}
foreach ($pattern in @('(?m)^\s*(?:kill|cat)\b', '(?i)/proc/', '(?i)pid_file', '(?i)service_pid')) {
    if ($stopText -match $pattern) {
        throw "restart stop_service must use only verified pidfd termination: $pattern"
    }
}
foreach ($pattern in @(
    '(?i)SYS_PTRACE', '(?i)--privileged', '(?m)^\s*print\s*\(',
    '(?i)sys\.stdout', '(?i)traceback', 'service_exit_code=0',
    'test "\$service_status" -eq 0'
)) {
    if ($coreText -match $pattern) {
        throw "forbidden restart service-isolation behavior matched: $pattern"
    }
}
$fixtureText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'setup_fixtures.sh'))
if ($fixtureText -match '(?m)^\s*chmod\b') {
    throw 'restart setup_fixtures.sh must not mutate generated product permissions with chmod'
}

$boundaryName = 'verify_nonroot_logparse_catalog.py'
$boundaryPath = Join-Path $DriverRoot $boundaryName
$mainBoundaryPath = Join-Path $mainRoot $boundaryName
if ((Get-FileHash -LiteralPath $boundaryPath -Algorithm SHA256).Hash -cne
    (Get-FileHash -LiteralPath $mainBoundaryPath -Algorithm SHA256).Hash) {
    throw 'main/restart nonroot Logparse catalog boundary verifier mismatch'
}
$boundaryText = [System.IO.File]::ReadAllText($boundaryPath)
$boundaryRunnerText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'verify_restart_nonempty_runtime.sh'))
$sourceSetupText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'setup_sources.sh'))
foreach ($literal in @(
    'build_logparse_runtime', 'VersionedAssetCatalog',
    'os.getresuid() == (SERVICE_UID,) * 3', 'os.getresgid() == (SERVICE_GID,) * 3',
    'root / ".git"', 'info.st_uid == 0 and info.st_gid == 0',
    'os.walk(root, followlinks=False)',
    'not os.access(entry, os.W_OK, effective_ids=True)',
    'catalog.check([asset.ref, selected]).missing_refs == []',
    '"logparse_tree_writable_entries": 0', '"status": "PASS"',
    'NONROOT_LOGPARSE_CATALOG_VERIFICATION_FAILED'
)) {
    if (-not $boundaryText.Contains($literal)) {
        throw "restart nonroot boundary verifier literal absent: $literal"
    }
}
foreach ($literal in @(
    'runuser -u plagent -- /usr/bin/env -i',
    '/evidence/verify_nonroot_logparse_catalog.py',
    'nonroot-logparse-catalog-verification.json',
    'install -m 0600 -o 0 -g 0',
    '"asset_runtime_build":"PASS"', '"catalog_startup_scan":"PASS"',
    '"logparse_tree_writable_entries":0', '"status":"PASS"'
)) {
    if (-not $boundaryRunnerText.Contains($literal)) {
        throw "restart nonroot boundary runner literal absent: $literal"
    }
}
foreach ($pattern in @(
    '(?i)git\s+config\s+--system',
    '(?i)safe\.directory[^\r\n]*\*',
    '(?i)chown[^\r\n]*/opt/src/logparse'
)) {
    if (($sourceSetupText + "`n" + $boundaryRunnerText + "`n" + $boundaryText) -match $pattern) {
        throw "forbidden restart Logparse trust workaround matched: $pattern"
    }
}
$mainPatchPath = Join-Path $mainRoot 'source.patch'
$expectedPatchHash = '2fdff7d3d71fb4938a35fa9c0805889aad6595f90879b4ce5fd7585fea1ccc74'
if ((Get-FileHash -LiteralPath $mainPatchPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expectedPatchHash) {
    throw 'restart view of the 32-file source patch has an unexpected hash'
}
$mainPatchText = [System.IO.File]::ReadAllText($mainPatchPath)
foreach ($literal in @(
    '_write(decoy / "decoy-only.txt", b"must never be enumerated\n")',
    '_git(decoy, "add", "decoy-only.txt")',
    'assert sorted(fingerprint_module._git_paths(target.resolve())) == sorted(',
    '!= sorted(user_facts, key=lambda item: item.item_id)',
    'def test_submit_supplement_accepts_canonical_fact_order_for_multiple_inputs() -> None:',
    'assert trigger.payload.stable_target_changed is False'
)) {
    if ([regex]::Matches($mainPatchText, [regex]::Escape($literal)).Count -ne 1) {
        throw "restart view of attempt41 deterministic decoy regression is invalid: $literal"
    }
}
$attemptHistoryText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'attempt-status.txt')) + "`n" +
    [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'execution-order.restart.txt'))
foreach ($literal in @(
    'attempt36_failure_stage=pre-Claude-parser-and-route-agent',
    'attempt36_parser_root_cause=PowerShell-function-pipeline-collapsed-empty-and-singleton-JSON-arrays',
    'attempt36_route_failure=BACKEND_EXIT_FAILED-Exceeded-USD-budget-0.1',
    'attempt41_parser_fix=three-Get-Property-helpers-preserve-array-identity-and-static-empty-singleton-multi-scalar-regressions',
    'attempt41_prompt_fix=Phase1-Phase3-restart-first-action-Skill-result-before-MCP',
    'attempt41_service_budget=1.00',
    'attempt41_real_agent_budget=0.10',
    'attempt41_attempt36_trace_reader_probe=PASS-14-line-real-NDJSON-',
    'attempt41_real_skill_first_probe=PASS-Claude-2.1.150-deepseek-v4-flash[1m]-',
    'attempt41_real_skill_first_probe_secret_scan_hits=0',
    'Confirm-JourneyDriverManifest',
    'Confirm-HcDriverManifest',
    'Confirm-RestartDriverManifest',
    'attempt41_product_source_patch_delta=external-command-canonical-fact-order-two-file-regression',
    'runtime_boundary_facts=independent-root-owned-receipts-not-driver-manifest-properties',
    'initial_container=pl-e2e-fix52-20260802-205054',
    'container=pl-e2e-fix52-restart-20260802-205054',
    'volume=pl-e2e-fix52-data-20260802-205054',
    'restart_resource_creator=create-restart-docker-resources.ps1',
    'restart_resource_creator_offline_regression=PASS',
    'restart_metadata_verifier=verify-restart-docker-metadata.ps1',
    'restart_metadata_verifier_offline_regression=PASS',
    'restart_resource_receipt=not-created-before-runtime',
    'restart_metadata_receipt=not-created-before-runtime',
    'restart_metadata_secret_scan=not-created-before-runtime',
    'restart_mount_contract=10-exact-mounts,8-read-only-host-inputs,1-read-write-evidence,1-existing-data-volume',
    'restart_volume_owner_label=problem-locator.e2e.owner=pl-e2e-fix52-20260802-205054',
    'host_cache_policy=neutral-file-mounts-with-fixed-sha256',
    'attempt42_failure_stage=initial-host-metadata-step05-before-apt',
    'attempt42_failure_root_cause=JavaScriptSerializer-generic-dictionary-Contains-overload-MethodCountCouldNotFindBest',
    'attempt43_settings_dictionary_fix=Ordinal-IDictionary-Keys-enumeration-exact-unique-string-key-then-indexer',
    'attempt43_settings_deserialize_regression=PS5-JavaScriptSerializer-valid-missing-env-case-wrong-ENV-fail-closed',
    'attempt43_restart_readonly_fix=missing-HostConfig-Mount-ReadOnly-normalizes-false-only-for-expected-writable-targets',
    'attempt43_failure_stage=host-sandbox-before-resource-creation',
    'attempt43_stable_error=DOCKER_RESOURCE_DOCKER_CONFIG',
    'attempt43_escalated_inspect=PASS-container-absent-volume-absent',
    'old_attempt42_bounded_stop=PASS',
    'host_docker_settings_permission=requires-sandbox-external-read-and-execute-authority'
)) {
    if (-not $attemptHistoryText.Contains($literal)) {
        throw "restart attempt41 failure history literal absent: $literal"
    }
}
$envBlock = [regex]::Match($verifierText, '(?ms)^EXPECTED_ENV = \{(.*?)^\}')
if (-not $envBlock.Success -or [regex]::Matches($envBlock.Groups[1].Value, '(?m)^\s{4}"[A-Z_]+":').Count -ne 17) {
    throw 'restart service process environment allowlist must remain exactly 17 keys'
}

$auditedSourceFiles = @(
    'windows-restart-lib.ps1',
    'run-windows-restart-verify.ps1',
    'download-windows-restart-artifact.ps1',
    'README-restart.md'
)
$sourceText = (($auditedSourceFiles | ForEach-Object { [System.IO.File]::ReadAllText((Join-Path $DriverRoot $_)) }) -join "`n")
$restartDriverSourceText = ((@(
    'windows-restart-lib.ps1',
    'run-windows-restart-verify.ps1',
    'download-windows-restart-artifact.ps1'
) | ForEach-Object { [System.IO.File]::ReadAllText((Join-Path $DriverRoot $_)) }) -join "`n")
$requiredLiterals = @(
    'C:\Users\admin\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe',
    '--setting-sources',
    "'user,project'",
    '--model',
    "'haiku'",
    '--strict-mcp-config',
    '--tools=Skill',
    '--allowedTools',
    'Skill(problem-locator-client)',
    "'dontAsk'",
    '--no-chrome',
    '--no-session-persistence',
    'http://127.0.0.1:18000/mcp',
    'alwaysLoad',
    'deepseek-v4-flash[1m]',
    'diagnosis-skill/diagnose-service-takeover',
    '6caca2c58e3678b3857d39f728e40d765a121ef0ea152381852687d5e3e3583f',
    'problem_locator_get_case',
    'problem_locator_list_artifacts',
    'FileMode]::CreateNew',
    'structuredContent',
    "'tool_use_result'",
    'final result subtype must be success',
    '--max-filesize',
    '--connect-timeout',
    '--max-time',
    'Get-RestartUserContentDisposition',
    '$toolResultBlocks.Count -eq 1',
    '$byId.ContainsKey($id)',
    "return 'ignore_text'"
)
foreach ($literal in $requiredLiterals) {
    if (-not $sourceText.Contains($literal)) {
        throw "required restart driver literal absent: $literal"
    }
}
$restartPromptSource = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'run-windows-restart-verify.ps1'))
if ([regex]::Matches($restartPromptSource, '(?m)^/problem-locator-client\r?$').Count -ne 0) {
    throw 'slash-form client Skill preloading is forbidden in restart prompt'
}
if ([regex]::Matches($restartPromptSource, '(?m)^\$prompt = @"\r?\nPerform the read-only ').Count -ne 1) {
    throw 'restart prompt must begin directly with its controlled instruction'
}

$forbiddenPatterns = @(
    '(?i)settings\.json',
    '(?i)ANTHROPIC_(?:API_KEY|AUTH_TOKEN)',
    '(?i)DEEPSEEK_API_KEY',
    '(?i)Get-ChildItem\s+(?:-Path\s+)?Env:',
    '(?i)Set-Content[^\r\n]*\.mcp\.json',
    '(?i)Out-File[^\r\n]*\.mcp\.json',
    'problem_locator_create_case',
    'problem_locator_prepare_attachment',
    'problem_locator_submit_supplement',
    'problem_locator_resume_case',
    'problem_locator_cancel_case',
    'Get-RestartProperty \$Block ''content'''
)
foreach ($pattern in $forbiddenPatterns) {
    if ($restartDriverSourceText -match $pattern) {
        throw "forbidden restart-driver behavior matched: $pattern"
    }
}

$possibleOutputs = @(Get-RestartAllOutputNames)
$expectedOutputs = @((Get-RestartQueryOutputNames) + (Get-RestartDownloadOutputNames))
Assert-RestartExactStrings @($possibleOutputs | Sort-Object) @($expectedOutputs | Sort-Object) 'static runtime output inventory'
if ($possibleOutputs.Count -ne 12) {
    throw 'restart output inventory must contain exactly 12 runtime files'
}
foreach ($name in $possibleOutputs) {
    if (-not $sourceText.Contains("'$name'")) {
        throw "runtime output is absent from restart source inventory: $name"
    }
    if (Test-Path -LiteralPath (Join-Path $DriverRoot $name)) {
        throw "restart template must not contain runtime output: $name"
    }
}

$manifestFiles = @()
foreach ($name in $files) {
    $item = Get-Item -LiteralPath (Join-Path $DriverRoot $name)
    $manifestFiles += [PSCustomObject][ordered]@{
        name = $name
        size = $item.Length
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$manifest = [PSCustomObject][ordered]@{
    schema_version = 1
    static_check = 'passed'
    network_or_claude_invoked = $false
    reads_or_copies_secret_settings = $false
    inline_strict_mcp = $true
    claude_business_tools = @('problem_locator_get_case', 'problem_locator_list_artifacts')
    first_tool = 'Skill(problem-locator-client)'
    authoritative_source = 'uniquely correlated stream-json tool_use/tool_result structuredContent only'
    user_text_event_regression = 'passed'
    mixed_or_multiple_tool_result_fail_closed = $true
    all_runtime_outputs_create_new = $true
    possible_runtime_outputs = $possibleOutputs
    files = $manifestFiles
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
$manifestPath = Join-Path $DriverRoot 'windows-restart-driver-manifest.json'
if (Test-Path -LiteralPath $manifestPath) {
    throw 'refusing to overwrite the restart static-check manifest'
}
$manifestBytes = $utf8.GetBytes((($manifest | ConvertTo-Json -Depth 20) + "`n"))
$manifestStream = [System.IO.File]::Open($manifestPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
try {
    $manifestStream.Write($manifestBytes, 0, $manifestBytes.Length)
    $manifestStream.Flush($true)
}
finally {
    $manifestStream.Dispose()
}
Confirm-RestartDriverManifest -DriverRoot $DriverRoot
Write-Output 'RESTART_STATIC_CHECK_PASSED'
