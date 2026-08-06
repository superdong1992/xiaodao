param([string]$DriverRoot = $PSScriptRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DriverRoot = [System.IO.Path]::GetFullPath($DriverRoot)
$files = @(
    'windows-journey-lib.ps1',
    'run-windows-journey.ps1',
    'static-check.ps1',
    'README.md'
)
foreach ($name in $files) {
    $path = Join-Path $DriverRoot $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "missing driver file: $name"
    }
}

$parseErrors = @()
foreach ($name in @('windows-journey-lib.ps1', 'run-windows-journey.ps1', 'static-check.ps1')) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $DriverRoot $name), [ref]$tokens, [ref]$errors)
    $parseErrors += @($errors)
}
if ($parseErrors.Count -ne 0) {
    throw ('PowerShell parse errors: ' + (($parseErrors | ForEach-Object { $_.Message }) -join '; '))
}

$dockerResourceFiles = @(
    'create-docker-resources.ps1',
    'test-create-docker-resources.ps1',
    'README-docker-resources.md',
    'verify-docker-metadata.ps1',
    'test-verify-docker-metadata.ps1',
    'README-docker-metadata.md'
)
foreach ($name in $dockerResourceFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $DriverRoot $name) -PathType Leaf)) {
        throw "missing frozen Docker resource file: $name"
    }
}
foreach ($name in @('create-docker-resources.ps1', 'test-create-docker-resources.ps1', 'verify-docker-metadata.ps1', 'test-verify-docker-metadata.ps1')) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $DriverRoot $name), [ref]$tokens, [ref]$errors)
    if (@($errors).Count -ne 0) { throw "frozen Docker PowerShell parse failure: $name" }
}
$creatorText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'create-docker-resources.ps1'))
$creatorTestText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'test-create-docker-resources.ps1'))
$metadataText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'verify-docker-metadata.ps1'))
$metadataTestText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'test-verify-docker-metadata.ps1'))
foreach ($literal in @(
    'New-DockerResourceArgumentPlan',
    'Assert-DockerResourceArgumentPlan',
    "'--config', `$DockerConfig",
    "'--pull=never'",
    "'127.0.0.1:18000:8000/tcp'",
    'Resolve-DockerResourceHostPath $UvPath File',
    'Resolve-DockerResourceHostPath $UvxPath File',
    'Resolve-DockerResourceHostPath $ClaudePath File',
    'Get-DockerResourceExactNamePresence',
    'problem-locator.e2e.owner',
    'volume_present_after_failure',
    'container_present_after_failure',
    'FileMode]::CreateNew',
    'DOCKER_RESOURCE_OFFLINE_REGRESSION_PASSED',
    '.tmp\pl-e2e-cache\uv-0.11.32\uv',
    '.tmp\pl-e2e-cache\claude-npm-2.1.89\package\cli.js',
    'da15297d6879b2cfbe5ea3cb03725c1613d51ba72892cc996468d871f0a532fb',
    '31e409e837c16cbe9bdfd6534a1e2f6a774d937988027a4f0736ab52c7b6864d',
    'a9950ef6407fdc750bddb673852485500387e524a99d42385cb81e7d17128e01'
)) {
    if (-not (($creatorText + "`n" + $creatorTestText).Contains($literal))) {
        throw "initial Docker creator literal absent: $literal"
    }
}
foreach ($literal in @(
    'DockerMetadataImageId',
    'ExpectedBindSources',
    'OrdinalIgnoreCase',
    'Get-DockerMetadataDictionaryValueOrdinal',
    'foreach ($candidateKey in $Dictionary.Keys)',
    '[StringComparison]::Ordinal',
    'return $Dictionary[$matchingKey]',
    'JavaScriptSerializer',
    'DeserializeObject',
    'missing env',
    'case-wrong ENV',
    'duplicate env',
    'non-string dictionary key',
    "`$mount.Mode -ceq ''",
    'Resolve-DockerMetadataHostPath $UvPath File',
    'Resolve-DockerMetadataHostPath $UvxPath File',
    'Resolve-DockerMetadataHostPath $ClaudePath File',
    'wrong bind source',
    'wrong image id'
)) {
    if (-not (($metadataText + "`n" + $metadataTestText).Contains($literal))) {
        throw "initial Docker metadata literal absent: $literal"
    }
}
if ($metadataText -match '\.Contains\(\s*[''"]env[''"]\s*\)') {
    throw 'JavaScriptSerializer settings lookup must not use overload-prone IDictionary.Contains'
}
$dictionaryHelper = [regex]::Match($metadataText, '(?ms)^function Get-DockerMetadataDictionaryValueOrdinal \{.*?^\}')
if (-not $dictionaryHelper.Success -or $dictionaryHelper.Value.Contains('.Contains(')) {
    throw 'Ordinal IDictionary helper must exist and must not use Contains'
}
$dictionaryKeysIndex = $dictionaryHelper.Value.IndexOf('$Dictionary.Keys', [StringComparison]::Ordinal)
$dictionaryCountIndex = $dictionaryHelper.Value.IndexOf('$matchingKeyCount -eq 1', [StringComparison]::Ordinal)
$dictionaryIndexerIndex = $dictionaryHelper.Value.IndexOf('return $Dictionary[$matchingKey]', [StringComparison]::Ordinal)
if ($dictionaryKeysIndex -lt 0 -or $dictionaryCountIndex -le $dictionaryKeysIndex -or $dictionaryIndexerIndex -le $dictionaryCountIndex) {
    throw 'Ordinal IDictionary helper must enumerate keys, require one exact match, then use the indexer'
}
foreach ($literal in @(
    '$settingsSerializer.DeserializeObject($validSettingsJson)',
    'Get-DockerMetadataSettingsSecrets -Settings $validSettings',
    "-Label 'missing env'",
    "-Label 'case-wrong ENV'",
    "-Label 'duplicate env'",
    "-Label 'non-string dictionary key'"
)) {
    if (-not $metadataTestText.Contains($literal)) { throw "initial Docker metadata settings regression absent: $literal" }
}
$rawSecretIndex = $metadataText.IndexOf("Assert-DockerMetadata (`$rawHits -eq 0) 'RAW_SECRET_HIT'", [StringComparison]::Ordinal)
$containerParseIndex = $metadataText.IndexOf('$containerRaw | ConvertFrom-Json', [StringComparison]::Ordinal)
if ($rawSecretIndex -lt 0 -or $containerParseIndex -lt 0 -or $rawSecretIndex -ge $containerParseIndex) {
    throw 'Docker metadata raw secret scan must precede JSON parse'
}
foreach ($pattern in @('(?i)Invoke-Expression', '(?i)docker\s+(?:container\s+)?rm', '(?i)docker\s+volume\s+rm', '(?i)Remove-Item')) {
    if ($creatorText -match $pattern) { throw "forbidden initial Docker creator behavior: $pattern" }
}
$executionText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'execution-order.txt'))
$step03 = [regex]::Match($executionText, '(?m)^03=(.+)$')
$step04 = [regex]::Match($executionText, '(?m)^04=(.+)$')
$step05 = [regex]::Match($executionText, '(?m)^05=(.+)$')
if (-not $step03.Success -or -not $step04.Success -or -not $step05.Success) { throw 'Docker execution steps absent' }
foreach ($literal in @('create-docker-resources.ps1', '-ContainerName', '-VolumeName', '-EvidenceRoot', '-DockerConfig', '-SettingsPath', '-XiaodaoSource', '-LogparseSource', '-McpSource', '-UvPath', '-UvxPath', '-ClaudePath', '.tmp\pl-e2e-cache\uv-0.11.32\uv', '.tmp\pl-e2e-cache\uv-0.11.32\uvx', '.tmp\pl-e2e-cache\claude-npm-2.1.89\package\cli.js')) {
    if (-not $step03.Groups[1].Value.Contains($literal)) { throw "step03 frozen creator argument absent: $literal" }
}
if ($step03.Groups[1].Value -match '(?i)(?:-Command|Invoke-Expression|docker\.exe|node\.exe|javascript)') {
    throw 'step03 must contain only the frozen file invocation, not inline host preflight'
}
if ($executionText -match '(?:uv-cache-attempt|claude-cache-attempt)') { throw 'attempt-specific cache path is forbidden' }
if (-not $step04.Groups[1].Value.Contains('verify_orchestration_hashes.sh') -or -not $step05.Groups[1].Value.Contains('verify-docker-metadata.ps1')) {
    throw 'Docker hash and metadata gate order is not frozen'
}
if (-not $step03.Groups[1].Value.Contains('sandbox-external host permission') -or
    -not $step05.Groups[1].Value.Contains('sandbox-external host permission')) {
    throw 'initial Docker and settings host permission boundary is not explicit'
}
$expectedInitialGates = @(
    '06=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/verify_shell_syntax.sh',
    '07=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/bootstrap_apt.sh',
    '08.1=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/uv_mount_preflight.sh',
    '08.2=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/verify_uv_preflight.sh',
    '09=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/bootstrap_uv.sh',
    '10=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/setup_sources.sh',
    '11=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/setup_venvs.sh',
    '12=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/verify_python_syntax.sh',
    '13=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/setup_fixtures.sh',
    '14=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/setup_claude.sh',
    '15=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/setup_nonroot_runtime.sh',
    '16=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/verify_nonroot_python_launchers.sh',
    '17.1=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_preclean.sh',
    '17.2=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_target.sh',
    '17.3=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_full.sh',
    '17.4=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_post.sh',
    '18.1=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 bash /evidence/gate_installed_distribution.sh',
    '18.2=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_native_independent.sh',
    '19=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_real_logparse.sh',
    '20.1=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_secret_scanner_harness.sh',
    '20.2=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_real_agent.sh',
    '20.3=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_real_route_agent.sh',
    '20.4=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_real_diagnose_agent.sh'
)
$actualInitialGates = @($executionText -split "`r?`n" | Where-Object { $_ -cmatch '^(?:06|07|08\.[12]|09|10|11|12|13|14|15|16|17\.[1-4]|18\.[12]|19|20\.[1-4])=' })
if ($actualInitialGates.Count -ne $expectedInitialGates.Count) { throw 'initial gate matrix count mismatch' }
for ($index = 0; $index -lt $expectedInitialGates.Count; $index++) {
    if ($actualInitialGates[$index] -cne $expectedInitialGates[$index]) {
        throw "initial gate matrix mismatch at index $index"
    }
}
$expectedInitialRuntime = @(
    '21.1=docker.exe --config C:\Users\admin\.docker exec --detach pl-e2e-fix52-20260802-205054 sh /evidence/start_service_supervisor.sh ; require service-supervisor-launch.txt',
    '21.2=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/gate_service_preflight.sh ; require service-process-isolation.json and service-preflight.json with PASS',
    '21.3=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\windows-service-preflight.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 ; require windows-live-ready-preflight.json with five passing readiness checks',
    '21.4=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-journey.ps1 -Mode Phase1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 ; require windows-claude-version.stdout.txt, windows-claude-version.stderr.txt, phase1.prompt.txt, phase1.stream-json.stdout.ndjson, phase1.stderr.txt, phase1.client-dfx.jsonl, phase1.authoritative.json, and phase1-state.json',
    '21.5=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-journey.ps1 -Mode Upload -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 ; require upload.curl.stdout.txt, upload.curl.stderr.txt, upload.response.json, upload.response.headers.txt, and upload-state.json',
    '21.6=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-journey.ps1 -Mode Phase3 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 ; require hook-failure.prompt.txt, hook-failure.stream-json.stdout.ndjson, hook-failure.stderr.txt, hook-failure.claude-debug.log, hook-failure.authoritative.json, phase3.prompt.txt, phase3.stream-json.stdout.ndjson, phase3.stderr.txt, phase3.client-dfx.jsonl, phase3.authoritative.json, and journey-authoritative-summary.json',
    '21.7=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\run-windows-http-capture.ps1 -EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054 -Phase Before ; require diagnosis-result.before.json, diagnosis-result.before.headers, diagnosis-result.before.meta.json, diagnosis-result.before.curl.stdout.txt, and diagnosis-result.before.curl.stderr.txt',
    '21.8=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/stop_service.sh ; require service-exit-status.txt with exit 143, service-log-secret-scan.json with zero hits, service.log, and service-stop-verification.txt',
    '21.9=docker.exe --config C:\Users\admin\.docker exec pl-e2e-fix52-20260802-205054 sh /evidence/capture_state_before_restart.sh ; require validate-state.before.json, state-export.before.json, and state-admin-before-restart.txt, then preserve the initial container until the frozen restart resource creator performs its bounded stop'
)
$actualInitialRuntime = @($executionText -split "`r?`n" | Where-Object { $_ -cmatch '^21\.[1-9]=' })
if ($actualInitialRuntime.Count -ne $expectedInitialRuntime.Count) { throw 'initial runtime matrix count must be exactly nine' }
for ($index = 0; $index -lt $expectedInitialRuntime.Count; $index++) {
    if ($actualInitialRuntime[$index] -cne $expectedInitialRuntime[$index]) {
        throw "initial runtime matrix mismatch at substep $($index + 1)"
    }
}
$readmeText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'README.md'))
foreach ($line in $expectedInitialRuntime) {
    $command = ($line -replace '^21\.[1-9]=', '') -replace ' ; require .+$', ''
    if (-not $readmeText.Contains($command)) { throw "README initial runtime command absent: $command" }
}
foreach ($line in $expectedInitialGates) {
    $command = $line.Substring($line.IndexOf('=') + 1)
    if (-not $readmeText.Contains($command)) { throw "README initial gate command absent: $command" }
}
. (Join-Path $DriverRoot 'windows-journey-lib.ps1')

$journeyPropertyProbe = '{"empty":[],"singleton":[{"id":"only"}],"multi":["first","second"],"scalar":"scalar"}' | ConvertFrom-Json
$journeyEmpty = Get-JourneyProperty $journeyPropertyProbe 'empty' -Required
$journeySingleton = Get-JourneyProperty $journeyPropertyProbe 'singleton' -Required
$journeyMulti = Get-JourneyProperty $journeyPropertyProbe 'multi' -Required
$journeyScalar = Get-JourneyProperty $journeyPropertyProbe 'scalar' -Required
if ($journeyEmpty -isnot [System.Array] -or @($journeyEmpty).Count -ne 0 -or
    -not [object]::ReferenceEquals($journeyEmpty, $journeyPropertyProbe.PSObject.Properties['empty'].Value)) {
    throw 'Get-JourneyProperty must preserve an empty JSON array'
}
if ($journeySingleton -isnot [System.Array] -or @($journeySingleton).Count -ne 1 -or $journeySingleton[0].id -cne 'only' -or
    -not [object]::ReferenceEquals($journeySingleton, $journeyPropertyProbe.PSObject.Properties['singleton'].Value)) {
    throw 'Get-JourneyProperty must preserve a singleton JSON array'
}
if ($journeyMulti -isnot [System.Array] -or @($journeyMulti).Count -ne 2 -or $journeyMulti[0] -cne 'first' -or $journeyMulti[1] -cne 'second' -or
    -not [object]::ReferenceEquals($journeyMulti, $journeyPropertyProbe.PSObject.Properties['multi'].Value)) {
    throw 'Get-JourneyProperty must preserve a multi-element JSON array'
}
if ($journeyScalar -isnot [string] -or $journeyScalar -cne 'scalar') {
    throw 'Get-JourneyProperty must preserve a JSON scalar'
}

$expectedDriverRoot = [System.IO.Path]::GetFullPath($DriverRoot)
$defaultEvidenceRoot = Resolve-JourneyEvidenceRoot -EvidenceRoot $null -EvidenceRootExplicitlyBound $false -RuntimeScriptRoot $DriverRoot
$explicitEvidenceRoot = Resolve-JourneyEvidenceRoot -EvidenceRoot $DriverRoot -EvidenceRootExplicitlyBound $true -RuntimeScriptRoot $DriverRoot
Assert-Journey ($defaultEvidenceRoot -ceq $expectedDriverRoot) 'unbound EvidenceRoot must resolve to the runtime script root'
Assert-Journey ($explicitEvidenceRoot -ceq $expectedDriverRoot) 'explicit EvidenceRoot must resolve to the same full path'
foreach ($invalidEvidenceRoot in @('', '   ')) {
    $failed = $false
    try {
        [void](Resolve-JourneyEvidenceRoot -EvidenceRoot $invalidEvidenceRoot -EvidenceRootExplicitlyBound $true -RuntimeScriptRoot $DriverRoot)
    }
    catch {
        $failed = $true
    }
    Assert-Journey $failed 'explicit empty or whitespace EvidenceRoot must fail closed'
}

function Invoke-JourneyUserDispositionCase {
    param([object[]]$Blocks, [bool]$IncludeTopLevelResult)
    $message = [PSCustomObject][ordered]@{ role = 'user'; content = $Blocks }
    $event = [PSCustomObject][ordered]@{ type = 'user'; message = $message }
    if ($IncludeTopLevelResult) {
        $event | Add-Member -NotePropertyName tool_use_result -NotePropertyValue ([PSCustomObject][ordered]@{ marker = 'present' })
    }
    return Get-JourneyUserContentDisposition -Event $event -Message $message -Content $Blocks
}

function Assert-JourneyUserDispositionFails {
    param([object[]]$Blocks, [bool]$IncludeTopLevelResult, [string]$Label)
    $failed = $false
    try {
        [void](Invoke-JourneyUserDispositionCase -Blocks $Blocks -IncludeTopLevelResult $IncludeTopLevelResult)
    }
    catch {
        $failed = $true
    }
    Assert-Journey $failed $Label
}

$emptyBlocks = [object[]]@()
$emptyText = [PSCustomObject][ordered]@{ type = 'text'; text = '' }
$ordinaryText = [PSCustomObject][ordered]@{ type = 'text'; text = 'status update' }
$toolResultOne = [PSCustomObject][ordered]@{ type = 'tool_result'; tool_use_id = 'toolu_static_1' }
$toolResultTwo = [PSCustomObject][ordered]@{ type = 'tool_result'; tool_use_id = 'toolu_static_2' }
Assert-Journey ((Invoke-JourneyUserDispositionCase -Blocks $emptyBlocks -IncludeTopLevelResult $false) -ceq 'ignore_text') 'empty user content must be ignored'
Assert-Journey ((Invoke-JourneyUserDispositionCase -Blocks @($emptyText) -IncludeTopLevelResult $false) -ceq 'ignore_text') 'empty text block must be ignored'
Assert-Journey ((Invoke-JourneyUserDispositionCase -Blocks @($emptyText, $ordinaryText) -IncludeTopLevelResult $false) -ceq 'ignore_text') 'ordinary text blocks must be ignored'
Assert-Journey ((Invoke-JourneyUserDispositionCase -Blocks @($toolResultOne) -IncludeTopLevelResult $true) -ceq 'tool_result') 'unique tool_result must remain authoritative'
Assert-JourneyUserDispositionFails -Blocks @($ordinaryText, $toolResultOne) -IncludeTopLevelResult $true -Label 'mixed text and tool_result must fail closed'
Assert-JourneyUserDispositionFails -Blocks @($toolResultOne, $toolResultTwo) -IncludeTopLevelResult $true -Label 'multiple tool_result blocks must fail closed'
Assert-JourneyUserDispositionFails -Blocks @($ordinaryText) -IncludeTopLevelResult $true -Label 'top-level result without content tool_result must fail closed'
Assert-JourneyUserDispositionFails -Blocks @($toolResultOne) -IncludeTopLevelResult $false -Label 'content tool_result without top-level result must fail closed'

$expectedRequirementNames = @('caller_service', 'server_service', 'rpc_method', 'problem_time')
$expectedRequirementKinds = @('INPUT', 'INPUT', 'INPUT', 'INPUT')
function New-StaticRequirement([string]$Name, [string]$Kind) {
    return [PSCustomObject][ordered]@{ status = 'OPEN'; name = $Name; kind = $Kind }
}
function New-StaticCaseRecord([object[]]$Requirements) {
    $view = [PSCustomObject][ordered]@{ status = 'WAITING_INPUT'; pending_requirements = $Requirements }
    return [PSCustomObject][ordered]@{
        tool_name = 'problem_locator_get_case'
        result = [PSCustomObject][ordered]@{ ok = $true; data = [PSCustomObject][ordered]@{ case_view = $view }; error = $null }
    }
}
function Assert-StaticRequirementFailure([object[]]$Requirements, [string]$Label) {
    $view = [PSCustomObject][ordered]@{ pending_requirements = $Requirements }
    $failed = $false
    try { Assert-JourneyOpenRequirements $view $expectedRequirementNames $expectedRequirementKinds $Label }
    catch { $failed = $true }
    Assert-Journey $failed "$Label must fail closed"
    Assert-Journey (-not (Test-JourneyCaseWithOpenNames (New-StaticCaseRecord $Requirements) 'WAITING_INPUT' $expectedRequirementNames $expectedRequirementKinds)) "$Label predicate must be false"
}
$shuffledRequirements = @(
    (New-StaticRequirement 'problem_time' 'INPUT'),
    (New-StaticRequirement 'caller_service' 'INPUT'),
    (New-StaticRequirement 'rpc_method' 'INPUT'),
    (New-StaticRequirement 'server_service' 'INPUT')
)
$shuffledView = [PSCustomObject][ordered]@{ pending_requirements = $shuffledRequirements }
Assert-JourneyOpenRequirements $shuffledView $expectedRequirementNames $expectedRequirementKinds 'shuffled requirement set'
Assert-Journey (Test-JourneyCaseWithOpenNames (New-StaticCaseRecord $shuffledRequirements) 'WAITING_INPUT' $expectedRequirementNames $expectedRequirementKinds) 'shuffled requirement predicate must pass'
$duplicateRequirements = @(
    (New-StaticRequirement 'caller_service' 'INPUT'),
    (New-StaticRequirement 'caller_service' 'INPUT'),
    (New-StaticRequirement 'rpc_method' 'INPUT'),
    (New-StaticRequirement 'problem_time' 'INPUT')
)
Assert-StaticRequirementFailure $duplicateRequirements 'duplicate requirement name'
$wrongKindRequirements = @($shuffledRequirements | ForEach-Object {
    if ($_.name -ceq 'rpc_method') { New-StaticRequirement $_.name 'ATTACHMENT' } else { New-StaticRequirement $_.name $_.kind }
})
Assert-StaticRequirementFailure $wrongKindRequirements 'wrong requirement kind'

$restartRoot = Join-Path $DriverRoot 'restart'
$coreNames = @('verify_service_process.py', 'start_service_supervisor.sh', 'stop_service.sh')
foreach ($name in $coreNames) {
    $mainPath = Join-Path $DriverRoot $name
    $restartPath = Join-Path $restartRoot $name
    if (-not (Test-Path -LiteralPath $mainPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $restartPath -PathType Leaf)) {
        throw "missing service-isolation core file: $name"
    }
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
$verifierRequired = @(
    'os.fork()',
    'os.setgroups([])',
    'os.setgid(SERVICE_GID)',
    'os.setuid(SERVICE_UID)',
    'os.getresuid()',
    'os.getresgid()',
    'os.getgroups() == []',
    'MAX_PIPE_BYTES = 8192',
    'MAX_RECEIPT_BYTES = 4096',
    'MAX_SERVICE_LOG_BYTES = 8 * 1024 * 1024',
    'select.select([read_fd]',
    'time.monotonic() + VERIFY_TIMEOUT_SECONDS',
    'os.waitpid(child_pid, os.WNOHANG)',
    'deadline = time.monotonic() + 1.0',
    'SERVICE_CHILD_REAP_TIMEOUT',
    'info.st_uid == 0 and info.st_gid == 0',
    'stat.S_IMODE(info.st_mode) == 0o600',
    'os.O_EXCL',
    'os.O_NOFOLLOW',
    'os.fstat(fd)',
    'os.fsync(fd)',
    'canonical_bytes({"ok": True, "summary": summary})',
    'SERVICE_CHILD_VERIFICATION_FAILED',
    'environment_key_count',
    'os.pidfd_open(int(pid_text), 0)',
    'signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)',
    'select.select([pidfd], [], [], TERMINATE_TIMEOUT_SECONDS)',
    'ARCHIVED_SERVICE_LOG_FILE = Path("/evidence/service.log")',
    'def archive_service_log() -> None:',
    'elif mode == "archive-log" and len(args) == 1:',
    'raise SystemExit("SERVICE_PROCESS_VERIFICATION_FAILED")'
)
foreach ($literal in $verifierRequired) {
    if (-not $verifierText.Contains($literal)) {
        throw "service verifier self-check literal absent: $literal"
    }
}
$pidfdOpenIndex = $verifierText.IndexOf('pidfd = os.pidfd_open(int(pid_text), 0)', [System.StringComparison]::Ordinal)
$sameUidInspectIndex = $verifierText.IndexOf('inspect_via_same_uid_child(pid_text, expected_starttime, close_in_child=pidfd)', [System.StringComparison]::Ordinal)
$pidfdSignalIndex = $verifierText.IndexOf('signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)', [System.StringComparison]::Ordinal)
$pidfdWaitIndex = $verifierText.IndexOf('select.select([pidfd], [], [], TERMINATE_TIMEOUT_SECONDS)', [System.StringComparison]::Ordinal)
if (-not (0 -le $pidfdOpenIndex -and $pidfdOpenIndex -lt $sameUidInspectIndex -and
    $sameUidInspectIndex -lt $pidfdSignalIndex -and $pidfdSignalIndex -lt $pidfdWaitIndex)) {
    throw 'pidfd termination order must be open, same-UID inspect, signal, bounded wait'
}
$supervisorRequired = @(
    'test "$(stat -c ''%u:%g:%a'' "$pid_file")" = 0:0:600',
    'test "$(stat -c ''%u:%g:%a'' "$starttime_file")" = 0:0:600',
    'Started server process [',
    'Application startup complete.',
    'Shutting down',
    'Application shutdown complete.',
    'Finished server process [',
    'started_count != 1',
    'started_line < startup_line',
    'shutdown_line < finished_line',
    'cleanup_probe" -lt 300',
    'cleanup_state" = Z',
    'kill -KILL "$service_pid"',
    "trap 'on_supervisor_exit `$?' EXIT",
    "trap 'on_supervisor_signal 129' HUP",
    "trap 'on_supervisor_signal 130' INT",
    "trap 'on_supervisor_signal 143' TERM",
    'trap - EXIT HUP INT TERM',
    'test "$service_status" -eq 143',
    'verify_service_process.py archive-log',
    'verify_service_process.py exit "$service_status"'
)
foreach ($literal in $supervisorRequired) {
    if (-not $supervisorText.Contains($literal)) {
        throw "service supervisor self-check literal absent: $literal"
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
    throw 'service shutdown evidence order must be scan, lifecycle, status 143, secure archive, exit receipt'
}
$stopRequired = @(
    'verify_service_process.py terminate',
    'service-exit-status.txt',
    'verify_service_process.py stop'
)
foreach ($literal in $stopRequired) {
    if (-not $stopText.Contains($literal)) {
        throw "service stop self-check literal absent: $literal"
    }
}
$stopForbiddenPatterns = @(
    '(?m)^\s*(?:kill|cat)\b',
    '(?i)/proc/',
    '(?i)pid_file',
    '(?i)service_pid'
)
foreach ($pattern in $stopForbiddenPatterns) {
    if ($stopText -match $pattern) {
        throw "stop_service must use only verified pidfd termination: $pattern"
    }
}
$coreForbiddenPatterns = @(
    '(?i)SYS_PTRACE',
    '(?i)--privileged',
    '(?m)^\s*print\s*\(',
    '(?i)sys\.stdout',
    '(?i)traceback',
    'service_exit_code=0',
    'test "\$service_status" -eq 0'
)
foreach ($pattern in $coreForbiddenPatterns) {
    if ($coreText -match $pattern) {
        throw "forbidden service-isolation behavior matched: $pattern"
    }
}
$fixtureText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'setup_fixtures.sh'))
if ($fixtureText -match '(?m)^\s*chmod\b') {
    throw 'setup_fixtures.sh must not mutate generated product permissions with chmod'
}

$boundaryName = 'verify_nonroot_logparse_catalog.py'
$boundaryPath = Join-Path $DriverRoot $boundaryName
$restartBoundaryPath = Join-Path $restartRoot $boundaryName
if (-not (Test-Path -LiteralPath $boundaryPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $restartBoundaryPath -PathType Leaf)) {
    throw 'missing main/restart nonroot Logparse catalog boundary verifier'
}
if ((Get-FileHash -LiteralPath $boundaryPath -Algorithm SHA256).Hash -cne
    (Get-FileHash -LiteralPath $restartBoundaryPath -Algorithm SHA256).Hash) {
    throw 'main/restart nonroot Logparse catalog boundary verifier mismatch'
}
$boundaryText = [System.IO.File]::ReadAllText($boundaryPath)
$boundaryRunnerText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'verify_nonroot_python_launchers.sh'))
$sourceSetupText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'setup_sources.sh'))
$preGatesText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'run_pre_gates.sh'))
foreach ($literal in @(
    'logparse_source_kind=directory',
    'cp -a /source/logparse/. /opt/src/logparse/',
    "printf 'logparse_source_kind=%s\n'"
)) {
    if (-not $sourceSetupText.Contains($literal)) {
        throw "setup_sources.sh archive-source support marker absent: $literal"
    }
}
if ($sourceSetupText -match '(?m)^logparse_commit=[0-9a-f]{40,64}$') {
    throw 'setup_sources.sh must not pin Logparse to a fixed commit'
}
foreach ($literal in @(
    'from problem_locator.integrations.logparse.broker import build_logparse_runtime',
    'from problem_locator.runtime.catalog import VersionedAssetCatalog',
    'os.getresuid() == (SERVICE_UID,) * 3',
    'os.getresgid() == (SERVICE_GID,) * 3',
    'root / ".git"',
    'info.st_uid == 0 and info.st_gid == 0',
    'os.walk(root, followlinks=False)',
    'not os.access(entry, os.W_OK, effective_ids=True)',
    'asset, broker_factory = build_logparse_runtime(',
    'catalog = VersionedAssetCatalog(',
    'diagnosis-skill/diagnose-service-takeover',
    'catalog.check([asset.ref, selected]).missing_refs == []',
    '"logparse_tree_writable_entries": 0',
    '"status": "PASS"',
    'NONROOT_LOGPARSE_CATALOG_VERIFICATION_FAILED'
)) {
    if (-not $boundaryText.Contains($literal)) {
        throw "nonroot Logparse catalog verifier literal absent: $literal"
    }
}
foreach ($literal in @(
    'runuser -u plagent -- /usr/bin/env -i',
    '/evidence/verify_nonroot_logparse_catalog.py',
    'nonroot-logparse-catalog-verification.json',
    'install -m 0600 -o 0 -g 0',
    '"asset_runtime_build":"PASS"',
    '"catalog_startup_scan":"PASS"',
    '"logparse_tree_writable_entries":0',
    '"status":"PASS"'
)) {
    if (-not $boundaryRunnerText.Contains($literal)) {
        throw "main nonroot boundary runner literal absent: $literal"
    }
}
$targetNodes = @(
    'tests/unit/application/test_external_commands.py::test_submit_supplement_accepts_canonical_fact_order_for_multiple_inputs',
    'tests/unit/integrations/test_logparse_primitives.py::test_git_inventory_trusts_only_the_exact_configured_repository',
    'tests/unit/integrations/test_logparse_primitives.py::test_git_inventory_ignores_ambient_repository_and_config_redirection',
    'tests/unit/integrations/test_logparse_primitives.py::test_git_inventory_rejects_a_safe_directory_wildcard_path'
)
foreach ($node in $targetNodes) {
    if ([regex]::Matches($preGatesText, [regex]::Escape($node)).Count -ne 1) {
        throw "target gate must contain exact node once: $node"
    }
}
if ($preGatesText -match '(?m)^\s*tests/unit/integrations/test_logparse_primitives\.py\s*\\\s*$') {
    throw 'target gate must not run the entire test_logparse_primitives.py file'
}
foreach ($pattern in @(
    '(?i)git\s+config\s+--system',
    '(?i)safe\.directory[^\r\n]*\*',
    '(?i)chown[^\r\n]*/opt/src/logparse'
)) {
    if (($sourceSetupText + "`n" + $boundaryRunnerText + "`n" + $boundaryText) -match $pattern) {
        throw "forbidden Logparse trust workaround matched: $pattern"
    }
}
$expectedPatchHash = '2fdff7d3d71fb4938a35fa9c0805889aad6595f90879b4ce5fd7585fea1ccc74'
if ((Get-FileHash -LiteralPath (Join-Path $DriverRoot 'source.patch') -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expectedPatchHash) {
    throw 'unexpected 32-file source patch hash'
}
$patchFiles = @([System.IO.File]::ReadAllLines((Join-Path $DriverRoot 'source.patch.files.txt')))
$expectedPatchFiles = [string[]]@(
    '.claude/skills/diagnose-service-takeover/SKILL.md',
    '.claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py',
    'src/problem_locator/application/external_commands.py',
    'src/problem_locator/integrations/logparse/fingerprint.py',
    'src/problem_locator/integrations/logparse/outputs.py',
    'src/problem_locator/interfaces/mcp_server.py',
    'src/problem_locator/runtime/agent_backend.py',
    'src/problem_locator/runtime/assets/output-contracts/diagnose/output-contract.md',
    'src/problem_locator/runtime/assets/output-contracts/review/output-contract.md',
    'src/problem_locator/runtime/assets/output-contracts/route/output-contract.md',
    'src/problem_locator/runtime/assets/profiles/specialist/profile.md',
    'src/problem_locator/runtime/context_builder.py',
    'tests/e2e/test_installed_distribution_gate.py',
    'tests/e2e/test_real_diagnose_agent_contract_gate.py',
    'tests/e2e/test_real_route_agent_contract_gate.py',
    'tests/fixtures/components/logparse/fixture-manifest.json',
    'tests/fixtures/components/logparse/source-copy.json',
    'tests/fixtures/components/logparse/wiki/service-takeover.md',
    'tests/fixtures/components/runtime-context/expected-section-order.json',
    'tests/fixtures/components/runtime-context/fixture-manifest.json',
    'tests/fixtures/rpc_timeout/fixture-manifest.json',
    'tests/fixtures/rpc_timeout/fake_agent.py',
    'tests/integration/test_s07_settings_catalog_runtime_seam.py',
    'tests/unit/application/test_external_commands.py',
    'tests/unit/integrations/test_generator_v2.py',
    'tests/unit/integrations/test_logparse_outputs.py',
    'tests/unit/integrations/test_logparse_primitives.py',
    'tests/unit/integrations/test_logparse_real_e2e.py',
    'tests/unit/interfaces/test_mcp_server.py',
    'tests/unit/runtime/test_agent_backend.py',
    'tests/unit/runtime/test_catalog.py',
    'tests/unit/runtime/test_context_builder.py'
)
if ($patchFiles.Count -ne $expectedPatchFiles.Count) { throw 'source patch must contain exactly 32 files' }
for ($patchIndex = 0; $patchIndex -lt $expectedPatchFiles.Count; $patchIndex++) {
    if ($patchFiles[$patchIndex] -cne $expectedPatchFiles[$patchIndex]) {
        throw "source patch file identity/order mismatch at index $patchIndex"
    }
}
$patchText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'source.patch'))
foreach ($literal in @(
    '_write(decoy / "decoy-only.txt", b"must never be enumerated\n")',
    '_git(decoy, "add", "decoy-only.txt")',
    'assert sorted(fingerprint_module._git_paths(target.resolve())) == sorted(',
    '!= sorted(user_facts, key=lambda item: item.item_id)',
    'def test_submit_supplement_accepts_canonical_fact_order_for_multiple_inputs() -> None:',
    'assert trigger.payload.stable_target_changed is False',
    'def test_builtin_output_contract_pins_safe_atomic_output_path(role: str) -> None:',
    'class _WorkspaceRootWriteGuard:',
    'def test_agent_process_starts_with_nonwritable_workspace_root_and_restores_it(',
    'never create `err.txt`, stdout/stderr captures',
    'TARGET_LOGS_REQUEST_SELF_CHECK_PASSED'
)) {
    if ([regex]::Matches($patchText, [regex]::Escape($literal)).Count -ne 1) {
        throw "attempt52 source patch must contain the exact deterministic decoy regression once: $literal"
    }
}
foreach ($literal in @(
    'Apply this deterministic group-A branch',
    'request only the missing group-A names',
    'must not add or request `order_id`, `log_archive`',
    'Only after this branch is inapplicable'
)) {
    if ([regex]::Matches($patchText, [regex]::Escape($literal)).Count -ne 2) {
        throw "attempt52 source patch must contain the deterministic missing-group-A branch and its regression exactly twice: $literal"
    }
}
foreach ($literal in @(
    'Never create a temporary file at workspace root',
    'p = Path("output/job_outcome.json")',
    'temporary = p.with_name("job_outcome.json.tmp")'
)) {
    if ([regex]::Matches($patchText, [regex]::Escape($literal)).Count -ne 4) {
        throw "attempt52 source patch must pin the safe atomic output literal for all three roles plus its regression: $literal"
    }
}
$attemptHistoryText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'attempt-status.txt')) + "`n" +
    [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'execution-order.txt'))
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
    'attempt41_failure_stage=host-resource-preflight-after-volume-creation-before-container-creation',
    'attempt52_resource_creator=frozen-11-parameter-array-only-creator-with-source-directories-cache-files-and-no-cleanup',
    'docker_metadata_raw_secret_order=scan-before-ConvertFrom-Json-stable-errors',
    'attempt42_failure_stage=initial-host-metadata-step05-before-apt',
    'attempt42_failure_root_cause=JavaScriptSerializer-generic-dictionary-Contains-overload-MethodCountCouldNotFindBest',
    'attempt43_settings_dictionary_fix=Ordinal-IDictionary-Keys-enumeration-exact-unique-string-key-then-indexer',
    'attempt43_settings_deserialize_regression=PS5-JavaScriptSerializer-valid-missing-env-case-wrong-ENV-fail-closed',
    'attempt43_failure_stage=host-sandbox-before-resource-creation',
    'attempt43_stable_error=DOCKER_RESOURCE_DOCKER_CONFIG',
    'attempt43_escalated_inspect=PASS-container-absent-volume-absent',
    'host_docker_settings_permission=requires-sandbox-external-read-and-execute-authority'
)) {
    if (-not $attemptHistoryText.Contains($literal)) {
        throw "attempt41 failure history literal absent: $literal"
    }
}
$envBlock = [regex]::Match($verifierText, '(?ms)^EXPECTED_ENV = \{(.*?)^\}')
if (-not $envBlock.Success -or [regex]::Matches($envBlock.Groups[1].Value, '(?m)^\s{4}"[A-Z_]+":').Count -ne 17) {
    throw 'service process environment allowlist must remain exactly 17 keys'
}

$driverText = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'windows-journey-lib.ps1')) + "`n" + [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'run-windows-journey.ps1'))
$requiredLiterals = @(
    'C:\Program Files\nodejs\node.exe',
    'C:\Users\admin\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\cli.js',
    '--setting-sources',
    "'user,project'",
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
    'http://127.0.0.1:9',
    'diagnosis-skill/diagnose-service-takeover',
    '6caca2c58e3678b3857d39f728e40d765a121ef0ea152381852687d5e3e3583f',
    'c8f16d4203a35181b688662813939b9b5312ae98ffb02cf86766cc3495d9bd26',
    '93dc9033f10ced86e51c15ed4744817979ef04d4664065c41208f5a1c47f4b1f',
    '2684354560',
    '--max-filesize',
    '--connect-timeout',
    '--max-time',
    'FileMode]::CreateNew',
    'structuredContent',
    "'tool_use_result'",
    'final result subtype must be success',
    '194f69fecd8dc8d40d1aedeb6fc25d2b7b4922b176be2b15be73ffe386cc5064',
    '2367',
    'Get-JourneyUserContentDisposition',
    '$toolResultBlocks.Count -eq 1',
    '$byId.ContainsKey($id)',
    "return 'ignore_text'",
    '$evidenceRootExplicitlyBound = $PSBoundParameters.ContainsKey(''EvidenceRoot'')',
    'Resolve-JourneyEvidenceRoot -EvidenceRoot $EvidenceRoot',
    'return [System.IO.Path]::GetFullPath($EvidenceRoot)',
    'ConvertTo-JourneyCanonicalRequirementPairs',
    'comparing by name and ignoring array order'
)
foreach ($literal in $requiredLiterals) {
    if (-not $driverText.Contains($literal)) {
        throw "required driver literal absent: $literal"
    }
}
$journeyPromptSource = [System.IO.File]::ReadAllText((Join-Path $DriverRoot 'run-windows-journey.ps1'))
if (-not $driverText.Contains('$groupKinds = @(''INPUT'', ''INPUT'', ''INPUT'', ''INPUT'')')) {
    throw 'Phase1 validator must require the real INPUT DTO kind'
}
if (-not $driverText.Contains("@('order_id') @('INPUT')")) {
    throw 'Phase3 validator must require the real INPUT DTO kind'
}
if ($driverText.Contains("@('PARAMETER'") -or $journeyPromptSource.Contains('OPEN PARAMETER requirement')) {
    throw 'obsolete PARAMETER DTO kind is forbidden in journey validation and prompts'
}
foreach ($literal in @('OPEN INPUT requirements', 'OPEN INPUT requirement named order_id')) {
    if (-not $journeyPromptSource.Contains($literal)) { throw "real INPUT journey prompt literal absent: $literal" }
}
if ([regex]::Matches($journeyPromptSource, '(?m)^/problem-locator-client\r?$').Count -ne 0) {
    throw 'slash-form client Skill preloading is forbidden in journey prompts'
}
if ([regex]::Matches($journeyPromptSource, '(?m)return @"\r?\nPerform phase 1 ').Count -ne 1) {
    throw 'Phase1 prompt must begin directly with its controlled instruction'
}
if ([regex]::Matches($journeyPromptSource, '(?m)return @"\r?\nPerform phase 3 ').Count -ne 1) {
    throw 'Phase3 prompt must begin directly with its controlled instruction'
}

$forbiddenPatterns = @(
    '(?i)settings\.json',
    '(?i)ANTHROPIC_(?:API_KEY|AUTH_TOKEN)',
    '(?i)DEEPSEEK_API_KEY',
    '(?i)Get-ChildItem\s+(?:-Path\s+)?Env:',
    '(?i)Set-Content[^\r\n]*\.mcp\.json',
    '(?i)Out-File[^\r\n]*\.mcp\.json',
    '(?i)Convert-JourneyJsonCandidate',
    'Get-JourneyProperty \$Block ''content'''
)
foreach ($pattern in $forbiddenPatterns) {
    if ($driverText -match $pattern) {
        throw "forbidden secret/config behavior matched: $pattern"
    }
}
if ($driverText -match '\[string\]\$EvidenceRoot\s*=\s*\$PSScriptRoot') {
    throw 'EvidenceRoot parameter must not reference PSScriptRoot in its default expression'
}
if ($driverText.Contains('requirements in this order')) {
    throw 'Phase1 prompt must not impose CaseView requirement array order'
}

$possibleOutputs = @(Get-JourneyAllOutputNames)
$plannedAll = @(Get-JourneyPlannedOutputNames -Mode All -IncludeVersion $true)
if ($possibleOutputs.Count -ne 24) {
    throw 'static output inventory must contain exactly 24 runtime files'
}
$possibleSorted = @($possibleOutputs | Sort-Object)
$plannedSorted = @($plannedAll | Sort-Object)
for ($index = 0; $index -lt $possibleSorted.Count; $index++) {
    if ($possibleSorted[$index] -cne $plannedSorted[$index]) {
        throw 'All-mode output preflight does not cover every possible runtime output'
    }
}
foreach ($name in $possibleOutputs) {
    if (-not $driverText.Contains("'$name'")) {
        throw "runtime output is absent from source inventory: $name"
    }
    if (Test-Path -LiteralPath (Join-Path $DriverRoot $name)) {
        throw "template must not contain runtime output: $name"
    }
}

$finalizationFiles = @(
    'finalize-attempt52.ps1',
    'test-finalize-attempt52.ps1',
    'scan-final-evidence.ps1',
    'README-finalization.md',
    'failure-history.json'
)
foreach ($name in $finalizationFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $DriverRoot $name) -PathType Leaf)) {
        throw "missing attempt52 finalization file: $name"
    }
}
foreach ($name in @('finalize-attempt52.ps1', 'test-finalize-attempt52.ps1', 'scan-final-evidence.ps1')) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $DriverRoot $name), [ref]$tokens, [ref]$errors)
    if (@($errors).Count -ne 0) { throw "attempt52 finalization PowerShell parse failure: $name" }
}
$finalizerText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'finalize-attempt52.ps1'))
$finalizerTestText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'test-finalize-attempt52.ps1'))
$finalScannerText = [IO.File]::ReadAllText((Join-Path $DriverRoot 'scan-final-evidence.ps1'))
$finalizationReadme = [IO.File]::ReadAllText((Join-Path $DriverRoot 'README-finalization.md'))
foreach ($literal in @(
    'D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054',
    'C:\Users\admin\.docker',
    'C:\Users\admin\.claude\settings.json',
    'pl-e2e-fix52-20260802-205054',
    'pl-e2e-fix52-restart-20260802-205054',
    'pl-e2e-fix52-data-20260802-205054',
    'pl-e2e-fix44-20260802-101150',
    'pl-e2e-fix44-data-20260802-101150',
    'pl-e2e-fix47-20260802-131942',
    'pl-e2e-fix47-data-20260802-131942',
    'pl-e2e-fix48-20260802-144245',
    'pl-e2e-fix48-data-20260802-144245',
    'pl-e2e-fix49-20260802-153112',
    'pl-e2e-fix49-data-20260802-153112',
    'pl-e2e-fix50-20260802-200018',
    'pl-e2e-fix50-data-20260802-200018',
    'pl-e2e-fix51-20260802-201232',
    'pl-e2e-fix51-data-20260802-201232',
    'ubuntu@sha256:3131b4cc82a783df6c9df078f86e01819a13594b865c2cad47bd1bca2b7063bb',
    'ad826729bfa315e9210dfa317648b0d42d08592767349ed37dfe137fac16a061',
    "'stop', '--timeout', '10'",
    "'container', 'rm'",
    "'volume', 'rm'",
    'container_remove_force = $false',
    'volume_remove_force = $false',
    'Invoke-Attempt52AuthorizedCleanup',
    'pre-cleanup-secret-scan.json',
    'cleanup-authorization.json',
    'cleanup-receipt.json',
    'final-verification-report.json',
    'final-secret-scan.json',
    'UNKNOWN_E2E_CONTAINER',
    'UNKNOWN_E2E_VOLUME',
    'old_attempt42_bounded_stop=PASS',
    'old_attempt44_bounded_stop=PASS',
    'old_attempt47_bounded_stop=PASS',
    'old_attempt48_bounded_stop=PASS',
    'old_attempt49_bounded_stop=PASS',
    'old_attempt50_failed_resources_preserved=PASS',
    'old_attempt51_failed_resources_preserved=PASS',
    'linux_identity_sha256',
    "xiaodao_base = 'c31cc03848155d03b9a35776555e413f26b264ad'",
    "problem_locator_mcp = '97d0446580f49e7b1add1c5fc6d6a41c97884884'",
    "'execution-order.txt', 'restart\execution-order.restart.txt', 'real-agent-command-template.txt', 'real-route-agent-command-template.txt', 'real-diagnose-agent-command-template.txt', 'installed-gate-command-template.txt'",
    '07-real-route-agent.xml',
    '08-real-diagnose-agent.xml',
    'secret-scan-real-route-agent',
    'secret-scan-real-diagnose-agent'
)) {
    if (-not $finalizerText.Contains($literal)) { throw "attempt52 finalizer literal absent: $literal" }
}
$containerAllowlistBlock = [regex]::Match($finalizerText, '(?ms)^\$script:Attempt52ContainerAllowlist = \[string\[\]\]@\((.*?)^\)')
$volumeAllowlistBlock = [regex]::Match($finalizerText, '(?ms)^\$script:Attempt52VolumeAllowlist = \[string\[\]\]@\((.*?)^\)')
if (-not $containerAllowlistBlock.Success -or [regex]::Matches($containerAllowlistBlock.Groups[1].Value, "(?m)^\s*'pl-e2e-[^']+'[,]?$").Count -ne 49) {
    throw 'attempt52 finalizer must contain exactly 49 literal container names'
}
if (-not $volumeAllowlistBlock.Success -or [regex]::Matches($volumeAllowlistBlock.Groups[1].Value, "(?m)^\s*'pl-e2e-[^']+'[,]?$").Count -ne 49) {
    throw 'attempt52 finalizer must contain exactly 49 literal volume names'
}
if ($finalizerText -match '(?i)Get-ChildItem[^\r\n]*-Recurse' -or $finalScannerText -match '(?i)Get-ChildItem[^\r\n]*-Recurse') {
    throw 'attempt52 finalization evidence walking must not use recursive enumeration'
}
if ($finalizerText.Contains("'--time'") -or $finalizerText -match "'container',\s*'rm',\s*'--force'" -or $finalizerText -match "'volume',\s*'rm',\s*'--force'") {
    throw 'attempt52 finalization cleanup argv drifted to unsafe timeout/force form'
}
foreach ($literal in @(
    "[ValidateSet('pre-cleanup-secret-scan.json', 'pre-final-secret-scan.json', 'final-secret-scan.json')]",
    'Get-FinalScanDictionaryValueOrdinal',
    'foreach ($candidateKey in $Dictionary.Keys)',
    'return $Dictionary[$matchingKey]',
    'Queue[System.IO.DirectoryInfo]',
    'SCAN_REPARSE_POINT',
    'SETTINGS_RAW_KEY_COUNT'
)) {
    if (-not $finalScannerText.Contains($literal)) { throw "attempt52 final scanner literal absent: $literal" }
}
if ($finalScannerText.Contains('$settings.Contains(') -or $finalScannerText.Contains('$envObject.Contains(')) {
    throw 'attempt52 final scanner must not use overload-prone dictionary Contains'
}
$validationWriteIndex = $finalizerText.IndexOf('Write-Attempt52CreateNewJson $validationPath $validation', [StringComparison]::Ordinal)
$preCleanupScanIndex = $finalizerText.IndexOf("& `$ScanInvoker `$EvidenceRoot `$SettingsPath 'pre-cleanup-secret-scan.json'", [StringComparison]::Ordinal)
$authorizationWriteIndex = $finalizerText.IndexOf('Write-Attempt52CreateNewJson $authorizationPath $authorization', [StringComparison]::Ordinal)
$cleanupIndex = $finalizerText.IndexOf('$cleanup = Invoke-Attempt52AuthorizedCleanup', [StringComparison]::Ordinal)
$cleanupReceiptIndex = $finalizerText.IndexOf('Write-Attempt52CreateNewJson $cleanupReceiptPath $cleanupReceipt', [StringComparison]::Ordinal)
$finalReportIndex = $finalizerText.IndexOf("Write-Attempt52CreateNewJson (Join-Path `$EvidenceRoot 'final-verification-report.json') `$finalReport", [StringComparison]::Ordinal)
$finalScanIndex = $finalizerText.IndexOf("& `$ScanInvoker `$EvidenceRoot `$SettingsPath 'final-secret-scan.json'", [StringComparison]::Ordinal)
$closureIndexes = @($validationWriteIndex, $preCleanupScanIndex, $authorizationWriteIndex, $cleanupIndex, $cleanupReceiptIndex, $finalReportIndex, $finalScanIndex)
if (@($closureIndexes | Where-Object { $_ -lt 0 }).Count -ne 0) { throw 'attempt52 finalization closure marker absent' }
for ($index = 1; $index -lt $closureIndexes.Count; $index++) {
    if ($closureIndexes[$index] -le $closureIndexes[$index - 1]) { throw 'attempt52 finalization closure order drifted' }
}
$afterFinalScan = $finalizerText.Substring($finalScanIndex)
if ($afterFinalScan.Contains('Write-Attempt52CreateNewJson') -or $afterFinalScan.Contains('[IO.File]::Open(')) {
    throw 'attempt52 final secret scan must be the absolute last evidence write'
}
$failureHistoryPath = Join-Path $DriverRoot 'failure-history.json'
if ((Get-FileHash -LiteralPath $failureHistoryPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne 'ad826729bfa315e9210dfa317648b0d42d08592767349ed37dfe137fac16a061') {
    throw 'attempt52 frozen failure history hash drifted'
}
$failureHistory = [IO.File]::ReadAllText($failureHistoryPath) | ConvertFrom-Json
if ([int]$failureHistory.schema_version -ne 1 -or $failureHistory.status -cne 'FROZEN' -or @($failureHistory.entries).Count -ne 51) {
    throw 'attempt52 frozen failure history envelope drifted'
}
if (@($failureHistory.entries | Where-Object { @($_.supplemental_sources).Count -gt 0 } | ForEach-Object { $_.supplemental_sources }).Count -ne 22) {
    throw 'attempt52 frozen failure history supplemental source count drifted'
}
if ((Test-Path -LiteralPath (Join-Path $DriverRoot '.generate-failure-history.ps1')) -or (Test-Path -LiteralPath (Join-Path $DriverRoot '.augment-failure-history.ps1'))) {
    throw 'attempt52 failure history build-temp script must not be frozen'
}
foreach ($literal in @('ATTEMPT52_FINALIZER_OFFLINE_REGRESSION_PASSED', 'actual_settings_reads=0', 'POST_INVENTORY_FAILED', 'UNKNOWN_E2E_CONTAINER', 'CLOSURE_ORDER_', 'MANIFEST_MISSING', 'MANIFEST_ASSET_TAMPER', 'MANIFEST_ASSET_MISSING', 'MANIFEST_EXTRA_ENTRY', 'MANIFEST_ENTRY_ORDER', 'MANIFEST_STATUS_TAMPER', 'MANIFEST_BEFORE_DOCKER_INVENTORY')) {
    if (-not $finalizerTestText.Contains($literal)) { throw "attempt52 finalizer regression literal absent: $literal" }
}
foreach ($literal in @('finalize-attempt52.ps1', '-EvidenceRoot D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054', '-DockerConfig C:\Users\admin\.docker', '-SettingsPath C:\Users\admin\.claude\settings.json', 'absolute last evidence write')) {
    if (-not $finalizationReadme.Contains($literal)) { throw "attempt52 finalization README literal absent: $literal" }
}
$readmeStaticCommand = 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\static-check.ps1"'
$readmeOfflineTestCommand = 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "D:\code\xiaodao\.tmp\pl-e2e-evidence\attempt52-20260802-205054\test-finalize-attempt52.ps1"'
$readmeStaticCommandIndex = $finalizationReadme.IndexOf($readmeStaticCommand, [StringComparison]::Ordinal)
$readmeOfflineTestCommandIndex = $finalizationReadme.IndexOf($readmeOfflineTestCommand, [StringComparison]::Ordinal)
if ($readmeStaticCommandIndex -lt 0 -or $readmeOfflineTestCommandIndex -le $readmeStaticCommandIndex -or -not $finalizationReadme.Contains('Only after that command reports `STATIC_CHECK_PASSED`')) {
    throw 'attempt52 finalization README must freeze manifests before the offline finalizer regression'
}

$finalizationManifestPath = Join-Path $DriverRoot 'finalization-driver-manifest.json'
$journeyManifestPath = Join-Path $DriverRoot 'windows-journey-driver-manifest.json'
if ((Test-Path -LiteralPath $finalizationManifestPath) -or (Test-Path -LiteralPath $journeyManifestPath)) {
    throw 'refusing to overwrite a static-check manifest'
}
$expectedFinalizationManifestFiles = @(
    'finalize-attempt52.ps1',
    'scan-final-evidence.ps1',
    'test-finalize-attempt52.ps1',
    'README-finalization.md',
    'failure-history.json',
    'restart/capture_linux_identity.sh',
    'execution-order.txt',
    'restart/execution-order.restart.txt',
    'static-check.ps1',
    'restart/static-check-restart.ps1',
    'attempt-status.txt'
)
$finalizationManifestEntries = New-Object System.Collections.Generic.List[object]
foreach ($relativePath in $expectedFinalizationManifestFiles) {
    $boundPath = Join-Path $DriverRoot $relativePath.Replace('/', '\')
    $boundItem = Get-Item -LiteralPath $boundPath -Force
    if (($boundItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $boundItem.PSIsContainer -or $boundItem.Length -le 0) {
        throw "attempt52 finalization driver manifest source is not an ordinary nonempty file: $relativePath"
    }
    [void]$finalizationManifestEntries.Add([PSCustomObject][ordered]@{
        path = $relativePath
        size = [int64]$boundItem.Length
        sha256 = (Get-FileHash -LiteralPath $boundPath -Algorithm SHA256).Hash.ToLowerInvariant()
    })
}
$finalizationManifest = [PSCustomObject][ordered]@{
    schema_version = 1
    status = 'FROZEN'
    files = [object[]]$finalizationManifestEntries.ToArray()
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
$utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
$finalizationManifestBytes = $utf8.GetBytes((($finalizationManifest | ConvertTo-Json -Depth 10 -Compress) + "`n"))
$finalizationManifestStream = [IO.File]::Open($finalizationManifestPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try {
    $finalizationManifestStream.Write($finalizationManifestBytes, 0, $finalizationManifestBytes.Length)
    $finalizationManifestStream.Flush($true)
}
finally { $finalizationManifestStream.Dispose() }
try { $finalizationManifestReadback = [IO.File]::ReadAllText($finalizationManifestPath, $utf8Strict) | ConvertFrom-Json -ErrorAction Stop }
catch { throw 'attempt52 finalization driver manifest readback failed' }
$rootProperties = @($finalizationManifestReadback.PSObject.Properties.Name)
if ($rootProperties.Count -ne 3 -or $rootProperties[0] -cne 'schema_version' -or $rootProperties[1] -cne 'status' -or $rootProperties[2] -cne 'files' -or
    [int]$finalizationManifestReadback.schema_version -ne 1 -or $finalizationManifestReadback.status -cne 'FROZEN' -or @($finalizationManifestReadback.files).Count -ne $expectedFinalizationManifestFiles.Count) {
    throw 'attempt52 finalization driver manifest readback envelope drifted'
}
for ($index = 0; $index -lt $expectedFinalizationManifestFiles.Count; $index++) {
    $entry = @($finalizationManifestReadback.files)[$index]
    $entryProperties = @($entry.PSObject.Properties.Name)
    if ($entryProperties.Count -ne 3 -or $entryProperties[0] -cne 'path' -or $entryProperties[1] -cne 'size' -or $entryProperties[2] -cne 'sha256' -or
        $entry.path -cne $expectedFinalizationManifestFiles[$index] -or [int64]$entry.size -ne [int64]$finalizationManifestEntries[$index].size -or $entry.sha256 -cne $finalizationManifestEntries[$index].sha256) {
        throw "attempt52 finalization driver manifest readback entry drifted: $index"
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
    stdout_stderr_separated = $true
    authoritative_source = 'stream-json tool_use/tool_result pairs only'
    user_text_event_regression = 'passed'
    mixed_or_multiple_tool_result_fail_closed = $true
    possible_runtime_outputs = $possibleOutputs
    files = $manifestFiles
}
$manifestPath = $journeyManifestPath
$manifestBytes = $utf8.GetBytes((($manifest | ConvertTo-Json -Depth 20) + "`n"))
$manifestStream = [System.IO.File]::Open($manifestPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
try {
    $manifestStream.Write($manifestBytes, 0, $manifestBytes.Length)
    $manifestStream.Flush($true)
}
finally {
    $manifestStream.Dispose()
}
Confirm-JourneyDriverManifest -DriverRoot $DriverRoot
Write-Output 'STATIC_CHECK_PASSED'
