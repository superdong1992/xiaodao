Set-StrictMode -Version Latest

if (-not (Get-Command Invoke-E2EBoundedProcess -ErrorAction SilentlyContinue)) {
    $boundedProcessPath = Join-Path $PSScriptRoot 'bounded-process.ps1'
    if (-not (Test-Path -LiteralPath $boundedProcessPath -PathType Leaf)) {
        $boundedProcessPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'bounded-process.ps1'
    }
    . $boundedProcessPath
}

$script:JourneyRepoRoot = 'D:\code\xiaodao'
$script:JourneyClaudeExe = 'C:\Users\admin\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe'
$script:JourneyClaudeVersion = '2.1.150'
$script:JourneyModelAlias = 'sonnet'
$script:JourneyEffectiveModel = 'deepseek-v4-flash[1m]'
$script:JourneyMcpUrl = 'http://127.0.0.1:18000/mcp'
$script:JourneyServiceBaseUrl = 'http://127.0.0.1:18000'
$script:JourneyUnusableProxyUrl = 'http://127.0.0.1:9'
$script:JourneyHookSettingsPath = Join-Path $script:JourneyRepoRoot '.claude\skills\problem-locator-client\references\client-hooks-settings.json'
$script:JourneyZipName = 'synthetic-rpc-service-takeover.zip'
$script:JourneyZipSize = 2367
$script:JourneyZipSha256 = '194f69fecd8dc8d40d1aedeb6fc25d2b7b4922b176be2b15be73ffe386cc5064'
$script:JourneySkillId = 'diagnosis-skill/diagnose-service-takeover'
$script:JourneySkillVersion = '3.0.5'
$script:JourneySkillHash = 'ae47a1a63e6cf4849f83b0f9d49db608c1e93ebe1713f21d58c910990b0857a4'
$script:JourneyClientSkillSha256 = '6caca2c58e3678b3857d39f728e40d765a121ef0ea152381852687d5e3e3583f'
$script:JourneyClientHookSha256 = 'c8f16d4203a35181b688662813939b9b5312ae98ffb02cf86766cc3495d9bd26'
$script:JourneyClientHookSettingsSha256 = '93dc9033f10ced86e51c15ed4744817979ef04d4664065c41208f5a1c47f4b1f'
$script:JourneyMaxAttachmentBytes = 2684354560
$script:JourneyMaxCurlJsonBytes = 1048576
$script:JourneyCurlConnectTimeoutSeconds = 10
$script:JourneyCurlMaxTimeSeconds = 120
$script:JourneyClaudeVersionTimeoutSeconds = 20
$script:JourneyClaudePhase1TimeoutSeconds = 180
$script:JourneyClaudePhase3TimeoutSeconds = 480
$script:JourneyCurlProcessTimeoutSeconds = 135
$script:JourneyMcpTools = @(
    'problem_locator_create_case',
    'problem_locator_prepare_attachment',
    'problem_locator_submit_supplement',
    'problem_locator_get_case',
    'problem_locator_resume_case',
    'problem_locator_cancel_case',
    'problem_locator_list_artifacts'
)
$script:JourneyFullMcpTools = @(
    $script:JourneyMcpTools | ForEach-Object { "mcp__problem-locator__$_" }
)
$script:JourneyUuidPattern = '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
$script:JourneySha256Pattern = '^[0-9a-f]{64}$'
$script:JourneyUtf8 = New-Object System.Text.UTF8Encoding($false)
$script:JourneyReservedOutputs = @{}
$script:JourneyCompletedOutputs = @{}

function Assert-Journey {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw "journey assertion failed: $Message"
    }
}

function Resolve-JourneyEvidenceRoot {
    param(
        [AllowNull()][AllowEmptyString()][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][bool]$EvidenceRootExplicitlyBound,
        [Parameter(Mandatory = $true)][string]$RuntimeScriptRoot
    )
    Assert-Journey (-not [string]::IsNullOrWhiteSpace($RuntimeScriptRoot)) 'runtime script root must be nonempty'
    $resolvedScriptRoot = [System.IO.Path]::GetFullPath($RuntimeScriptRoot)
    if (-not $EvidenceRootExplicitlyBound) {
        return $resolvedScriptRoot
    }
    Assert-Journey (-not [string]::IsNullOrWhiteSpace($EvidenceRoot)) 'explicit evidence root must be nonempty'
    return [System.IO.Path]::GetFullPath($EvidenceRoot)
}

function Test-JourneyProperty {
    param($Object, [string]$Name)
    return $null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]
}

function Get-JourneyProperty {
    param(
        $Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [switch]$Required
    )
    if (-not (Test-JourneyProperty $Object $Name)) {
        if ($Required) {
            throw "journey assertion failed: required JSON property '$Name' is absent"
        }
        return $null
    }
    $value = $Object.PSObject.Properties[$Name].Value
    if ($value -is [System.Array]) {
        return ,$value
    }
    return $value
}

function Get-JourneyStringProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    $value = Get-JourneyProperty $Object $Name -Required
    Assert-Journey ($value -is [string]) "$Name must be a JSON string"
    return $value
}

function Get-JourneyBooleanProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    $value = Get-JourneyProperty $Object $Name -Required
    Assert-Journey ($value -is [bool]) "$Name must be a JSON boolean"
    return $value
}

function Get-JourneyIntegerProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    $value = Get-JourneyProperty $Object $Name -Required
    $integerTypes = @(
        [byte], [sbyte], [int16], [uint16], [int32], [uint32], [int64], [uint64]
    )
    Assert-Journey ($null -ne $value) "$Name must be a JSON integer"
    Assert-Journey ($integerTypes -contains $value.GetType()) "$Name must be a JSON integer"
    return [int64]$value
}

function Assert-JourneyJsonObject {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Journey ($Value -is [System.Management.Automation.PSCustomObject]) "$Label must be a JSON object"
}

function Assert-JourneyJsonArray {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Journey ($Value -is [System.Array]) "$Label must be a JSON array"
}

function Assert-JourneyStringArray {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-JourneyJsonArray $Value $Label
    foreach ($item in @($Value)) {
        Assert-Journey ($item -is [string]) "$Label entries must be JSON strings"
    }
}

function Assert-JourneyExactProperties {
    param($Object, [string[]]$Expected, [Parameter(Mandatory = $true)][string]$Label)
    Assert-JourneyJsonObject $Object $Label
    $actual = @($Object.PSObject.Properties.Name | Sort-Object)
    $sortedExpected = @($Expected | Sort-Object)
    Assert-JourneyExactStrings $actual $sortedExpected "$Label properties"
}

function Get-JourneyAllOutputNames {
    return @(
        'windows-claude-version.stdout.txt',
        'windows-claude-version.stderr.txt',
        'phase1.prompt.txt',
        'phase1.stream-json.stdout.ndjson',
        'phase1.stderr.txt',
        'phase1.client-dfx.jsonl',
        'phase1.authoritative.json',
        'phase1-state.json',
        'upload.curl.stdout.txt',
        'upload.curl.stderr.txt',
        'upload.response.json',
        'upload.response.headers.txt',
        'upload-state.json',
        'phase3.prompt.txt',
        'phase3.stream-json.stdout.ndjson',
        'phase3.stderr.txt',
        'phase3.client-dfx.jsonl',
        'phase3.authoritative.json',
        'hook-failure.prompt.txt',
        'hook-failure.stream-json.stdout.ndjson',
        'hook-failure.stderr.txt',
        'hook-failure.claude-debug.log',
        'hook-failure.authoritative.json',
        'journey-authoritative-summary.json'
    )
}

function Get-JourneyPlannedOutputNames {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Phase1', 'Upload', 'Phase3', 'All')][string]$Mode,
        [bool]$IncludeVersion = $true
    )
    $version = if ($IncludeVersion) { @('windows-claude-version.stdout.txt', 'windows-claude-version.stderr.txt') } else { @() }
    $phase1 = @('phase1.prompt.txt', 'phase1.stream-json.stdout.ndjson', 'phase1.stderr.txt', 'phase1.client-dfx.jsonl', 'phase1.authoritative.json', 'phase1-state.json')
    $upload = @('upload.curl.stdout.txt', 'upload.curl.stderr.txt', 'upload.response.json', 'upload.response.headers.txt', 'upload-state.json')
    $phase3 = @('hook-failure.prompt.txt', 'hook-failure.stream-json.stdout.ndjson', 'hook-failure.stderr.txt', 'hook-failure.claude-debug.log', 'hook-failure.authoritative.json', 'phase3.prompt.txt', 'phase3.stream-json.stdout.ndjson', 'phase3.stderr.txt', 'phase3.client-dfx.jsonl', 'phase3.authoritative.json', 'journey-authoritative-summary.json')
    switch ($Mode) {
        'Phase1' { return @($version + $phase1) }
        'Upload' { return @($upload) }
        'Phase3' { return @($version + $phase3) }
        'All' { return @($version + $phase1 + $upload + $phase3) }
    }
}

function New-JourneyOutputReservations {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot, [Parameter(Mandatory = $true)][string[]]$Names)
    $root = [System.IO.Path]::GetFullPath($EvidenceRoot).TrimEnd('\') + '\'
    Assert-Journey (@($Names | Sort-Object -Unique).Count -eq $Names.Count) 'planned output names must be unique'
    $paths = @()
    foreach ($name in $Names) {
        Assert-Journey ([System.IO.Path]::GetFileName($name) -ceq $name) "planned output must be a plain filename: $name"
        $path = [System.IO.Path]::GetFullPath((Join-Path $EvidenceRoot $name))
        Assert-Journey ($path.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) "planned output escaped evidence root: $name"
        Assert-Journey (-not (Test-Path -LiteralPath $path)) "planned output already exists: $path"
        $paths += $path
    }
    foreach ($path in $paths) {
        $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $stream.Dispose()
        $script:JourneyReservedOutputs[$path.ToLowerInvariant()] = $true
        $script:JourneyCompletedOutputs[$path.ToLowerInvariant()] = $false
    }
}

function Assert-JourneyReservedUnused {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    $key = $full.ToLowerInvariant()
    Assert-Journey ($script:JourneyReservedOutputs.ContainsKey($key)) "output was not atomically reserved: $full"
    Assert-Journey (-not [bool]$script:JourneyCompletedOutputs[$key]) "output reservation was already consumed: $full"
}

function Complete-JourneyExternalOutput {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-JourneyReservedUnused $Path
    $script:JourneyCompletedOutputs[[System.IO.Path]::GetFullPath($Path).ToLowerInvariant()] = $true
}

function Protect-JourneySensitiveOutput {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-JourneyReservedUnused $Path
    Assert-Journey (Test-Path -LiteralPath $Path -PathType Leaf) 'sensitive output file is absent'
    $sensitiveValues = @(
        [string]$env:ANTHROPIC_AUTH_TOKEN,
        [string]$env:ANTHROPIC_BASE_URL
    ) | Where-Object { -not [string]::IsNullOrEmpty($_) } | Select-Object -Unique
    Assert-Journey ($sensitiveValues.Count -eq 2) 'Claude authentication token and base URL are required for evidence redaction'

    $buffer = [System.IO.File]::ReadAllBytes($Path)
    foreach ($sensitiveValue in $sensitiveValues) {
        $needle = $script:JourneyUtf8.GetBytes($sensitiveValue)
        Assert-Journey ($needle.Length -gt 0) 'sensitive evidence needle must be nonempty'
        if ($buffer.Length -lt $needle.Length) { continue }
        for ($offset = 0; $offset -le $buffer.Length - $needle.Length; $offset++) {
            if ($buffer[$offset] -ne $needle[0]) { continue }
            $matches = $true
            for ($index = 1; $index -lt $needle.Length; $index++) {
                if ($buffer[$offset + $index] -ne $needle[$index]) {
                    $matches = $false
                    break
                }
            }
            if (-not $matches) { continue }
            for ($index = 0; $index -lt $needle.Length; $index++) {
                $buffer[$offset + $index] = [byte]0x2a
            }
            $offset += $needle.Length - 1
        }
    }

    # Claude can load ANTHROPIC_BASE_URL from its settings after process
    # environment setup. The journey driver deliberately does not read that
    # settings file, so mask every HTTPS URL in the debug-only artifact before
    # it enters the evidence set.
    $httpsPrefix = [System.Text.Encoding]::ASCII.GetBytes('https://')
    for ($offset = 0; $offset -le $buffer.Length - $httpsPrefix.Length; $offset++) {
        $matches = $true
        for ($index = 0; $index -lt $httpsPrefix.Length; $index++) {
            if ($buffer[$offset + $index] -ne $httpsPrefix[$index]) {
                $matches = $false
                break
            }
        }
        if (-not $matches) { continue }
        $end = $offset + $httpsPrefix.Length
        while ($end -lt $buffer.Length) {
            $value = $buffer[$end]
            if ($value -le 0x20 -or $value -eq 0x22 -or $value -eq 0x27 -or $value -eq 0x3c -or $value -eq 0x3e -or $value -eq 0x5c) {
                break
            }
            $end++
        }
        for ($index = $offset; $index -lt $end; $index++) {
            $buffer[$index] = [byte]0x2a
        }
        $offset = $end - 1
    }

    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Truncate, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $stream.Write($buffer, 0, $buffer.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

function Confirm-JourneyDriverManifest {
    param([Parameter(Mandatory = $true)][string]$DriverRoot)
    $manifestPath = Join-Path $DriverRoot 'windows-journey-driver-manifest.json'
    $manifest = Read-JourneyJson $manifestPath
    Assert-JourneyExactProperties $manifest @('schema_version', 'static_check', 'network_or_claude_invoked', 'reads_or_copies_secret_settings', 'inline_strict_mcp', 'stdout_stderr_separated', 'authoritative_source', 'user_text_event_regression', 'mixed_or_multiple_tool_result_fail_closed', 'possible_runtime_outputs', 'files') 'driver manifest'
    Assert-Journey ((Get-JourneyIntegerProperty $manifest 'schema_version') -eq 1) 'driver manifest schema_version'
    Assert-Journey ((Get-JourneyStringProperty $manifest 'static_check') -ceq 'passed') 'driver manifest static_check'
    Assert-Journey (-not (Get-JourneyBooleanProperty $manifest 'network_or_claude_invoked')) 'static check must not invoke network/Claude'
    Assert-Journey (-not (Get-JourneyBooleanProperty $manifest 'reads_or_copies_secret_settings')) 'driver must not read/copy settings secrets'
    Assert-Journey (Get-JourneyBooleanProperty $manifest 'inline_strict_mcp') 'driver manifest inline MCP'
    Assert-Journey (Get-JourneyBooleanProperty $manifest 'stdout_stderr_separated') 'driver manifest output separation'
    Assert-Journey ((Get-JourneyStringProperty $manifest 'authoritative_source') -ceq 'stream-json tool_use/tool_result pairs only') 'driver manifest authoritative source'
    Assert-Journey ((Get-JourneyStringProperty $manifest 'user_text_event_regression') -ceq 'passed') 'driver manifest user text regression'
    Assert-Journey (Get-JourneyBooleanProperty $manifest 'mixed_or_multiple_tool_result_fail_closed') 'driver manifest mixed/multiple tool_result fail closed'
    $possibleOutputs = Get-JourneyProperty $manifest 'possible_runtime_outputs' -Required
    Assert-JourneyStringArray $possibleOutputs 'driver manifest possible_runtime_outputs'
    Assert-JourneyExactStrings @($possibleOutputs | Sort-Object) @(Get-JourneyAllOutputNames | Sort-Object) 'driver manifest possible runtime outputs'
    $files = Get-JourneyProperty $manifest 'files' -Required
    Assert-JourneyJsonArray $files 'driver manifest files'
    $expectedNames = @('README.md', 'run-windows-journey.ps1', 'static-check.ps1', 'windows-journey-lib.ps1')
    $actualNames = @($files | ForEach-Object { Get-JourneyStringProperty $_ 'name' } | Sort-Object)
    Assert-JourneyExactStrings $actualNames $expectedNames 'driver manifest filenames'
    foreach ($record in $files) {
        Assert-JourneyExactProperties $record @('name', 'size', 'sha256') 'driver manifest file record'
        $name = Get-JourneyStringProperty $record 'name'
        $path = Join-Path $DriverRoot $name
        Assert-Journey (Test-Path -LiteralPath $path -PathType Leaf) "manifest file absent: $name"
        $item = Get-Item -LiteralPath $path
        Assert-Journey ((Get-JourneyIntegerProperty $record 'size') -eq $item.Length) "manifest size mismatch: $name"
        $expectedHash = Get-JourneyStringProperty $record 'sha256'
        Assert-Journey ($expectedHash -cmatch $script:JourneySha256Pattern) "manifest SHA-256 shape: $name"
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        Assert-Journey ($actualHash -ceq $expectedHash) "manifest SHA-256 mismatch: $name"
    }
}

function Confirm-JourneyClientSkill {
    $assets = @(
        @{
            Label = 'problem-locator-client Skill'
            Path = Join-Path $script:JourneyRepoRoot '.claude\skills\problem-locator-client\SKILL.md'
            Sha256 = $script:JourneyClientSkillSha256
        },
        @{
            Label = 'problem-locator-client Hook'
            Path = Join-Path $script:JourneyRepoRoot '.claude\skills\problem-locator-client\scripts\problem-locator-client-dfx.ps1'
            Sha256 = $script:JourneyClientHookSha256
        },
        @{
            Label = 'problem-locator-client Hook settings'
            Path = $script:JourneyHookSettingsPath
            Sha256 = $script:JourneyClientHookSettingsSha256
        }
    )
    foreach ($asset in $assets) {
        Assert-Journey (Test-Path -LiteralPath $asset.Path -PathType Leaf) "$($asset.Label) is absent"
        $actual = (Get-FileHash -LiteralPath $asset.Path -Algorithm SHA256).Hash.ToLowerInvariant()
        Assert-Journey ($actual -ceq $asset.Sha256) "$($asset.Label) SHA-256"
    }
}

function Get-JourneyNoProxyParts {
    param(
        [AllowNull()][AllowEmptyString()][string]$PreviousNoProxy,
        [AllowNull()][AllowEmptyString()][string]$PreviousLowerNoProxy
    )
    Assert-Journey (-not [string]::IsNullOrWhiteSpace($env:ANTHROPIC_BASE_URL)) 'ANTHROPIC_BASE_URL is required for the proxy bypass gate'
    $modelApiUri = $null
    $validModelApiUri = [Uri]::TryCreate($env:ANTHROPIC_BASE_URL, [UriKind]::Absolute, [ref]$modelApiUri)
    Assert-Journey ($validModelApiUri -and $modelApiUri.Scheme -ceq 'https') 'ANTHROPIC_BASE_URL must be an absolute HTTPS URL'
    Assert-Journey ([string]::IsNullOrEmpty($modelApiUri.UserInfo)) 'ANTHROPIC_BASE_URL must not contain user info'
    Assert-Journey (-not [string]::IsNullOrWhiteSpace($modelApiUri.DnsSafeHost)) 'ANTHROPIC_BASE_URL host is required'
    $modelApiHost = $modelApiUri.DnsSafeHost
    $parts = @(
        (($PreviousNoProxy + ',' + $PreviousLowerNoProxy) -split '[,\s]+' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        '127.0.0.1'
        'localhost'
        $modelApiHost
        "$($modelApiHost):$($modelApiUri.Port)"
        ".$modelApiHost"
    ) | Select-Object -Unique
    Assert-Journey (-not ($parts -contains '*')) 'Windows journey forbids NO_PROXY=*'
    return [string[]]$parts
}

function Write-JourneyUtf8 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    Assert-JourneyReservedUnused $Path
    $bytes = $script:JourneyUtf8.GetBytes($Text)
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Truncate, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    $script:JourneyCompletedOutputs[[System.IO.Path]::GetFullPath($Path).ToLowerInvariant()] = $true
}

function Write-JourneyJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    Write-JourneyUtf8 -Path $Path -Text (($Value | ConvertTo-Json -Depth 100) + "`n")
}

function Read-JourneyJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-Journey (Test-Path -LiteralPath $Path -PathType Leaf) "required JSON file is absent: $Path"
    return [System.IO.File]::ReadAllText($Path, $script:JourneyUtf8) | ConvertFrom-Json
}

function Assert-JourneyExactStrings {
    param(
        $Actual,
        [string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $actualArray = @($Actual)
    Assert-Journey ($actualArray.Count -eq $Expected.Count) "$Label count"
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        Assert-Journey ([string]$actualArray[$index] -ceq $Expected[$index]) "$Label[$index]"
    }
}

function Assert-JourneyUuid {
    param($Value, [string]$Label)
    Assert-Journey ($Value -is [string] -and $Value -cmatch $script:JourneyUuidPattern) "$Label must be a lowercase UUID"
}

function ConvertTo-JourneyWindowsArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    if ($Value.Length -eq 0) {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-JourneyCapturedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath,
        [Parameter(Mandatory = $true)][ValidateRange(1, 3600)][int]$TimeoutSeconds
    )
    Assert-Journey (Test-Path -LiteralPath $FilePath -PathType Leaf) "executable is absent: $FilePath"
    Assert-Journey (Test-Path -LiteralPath $WorkingDirectory -PathType Container) "working directory is absent"
    Assert-JourneyReservedUnused $StdoutPath
    Assert-JourneyReservedUnused $StderrPath

    $argumentLine = (($Arguments | ForEach-Object { ConvertTo-JourneyWindowsArgument ([string]$_) }) -join ' ')
    $processArguments = @{
        FilePath = $FilePath
        ArgumentLine = $argumentLine
        WorkingDirectory = $WorkingDirectory
        StdoutPath = $StdoutPath
        StderrPath = $StderrPath
        TimeoutSeconds = $TimeoutSeconds
        TimeoutReceiptPath = "$StdoutPath.timeout.json"
    }
    $result = Invoke-E2EBoundedProcess @processArguments
    Complete-JourneyExternalOutput $StdoutPath
    Complete-JourneyExternalOutput $StderrPath
    return $result.exit_code
}

function Get-JourneyAttemptLabel {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $leaf = Split-Path -Leaf ([System.IO.Path]::GetFullPath($EvidenceRoot))
    Assert-Journey ($leaf -cmatch '^attempt[0-9]+-[0-9]{8}-[0-9]{6}$') 'evidence directory must use the clean attempt name'
    return ($leaf -split '-', 2)[0]
}

function Get-JourneyRequestIds {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $label = Get-JourneyAttemptLabel $EvidenceRoot
    return [ordered]@{
        create = "$label-windows-create-v1"
        submit_a = "$label-windows-submit-a-v1"
        prepare = "$label-windows-prepare-log-v1"
        submit_attachment = "$label-windows-submit-attachment-v1"
        submit_order = "$label-windows-submit-order-v1"
    }
}

function Get-JourneyProblemSpec {
    return [ordered]@{
        statement = 'A checkout-to-inventory ReserveStock RPC times out during a service takeover.'
        expected_behavior = 'The checkout operation completes after inventory reservation.'
        actual_behavior = 'During an active service takeover, the ReserveStock RPC times out and checkout does not complete.'
        scope = 'checkout-to-inventory service-takeover RPC diagnosis'
        goals = @('Locate the service-takeover timeout cause using the supplied logs.')
        non_goals = @('Modify production systems.')
        constraints = @('Use only evidence persisted in this diagnosis case.')
        completion_criteria = @('Identify the timed-out request and an evidence-backed root cause.')
    }
}

function Get-JourneyMcpConfigJson {
    $config = [ordered]@{
        mcpServers = [ordered]@{
            'problem-locator' = [ordered]@{
                type = 'http'
                url = $script:JourneyMcpUrl
                alwaysLoad = $true
            }
        }
    }
    return ($config | ConvertTo-Json -Compress -Depth 10)
}

function Get-JourneyClaudeArguments {
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][ValidateSet('phase1', 'phase3')][string]$Phase,
        [AllowNull()][AllowEmptyString()][string]$DebugFile
    )
    $maxTurns = if ($Phase -ceq 'phase1') { '20' } else { '30' }
    $arguments = @(
        '--print',
        '--output-format', 'stream-json',
        '--verbose',
        '--model', $script:JourneyModelAlias,
        '--max-turns', $maxTurns,
        '--setting-sources', 'user,project',
        '--settings', $script:JourneyHookSettingsPath,
        '--mcp-config', (Get-JourneyMcpConfigJson),
        '--strict-mcp-config',
        '--tools=Skill',
        '--allowedTools',
        'Skill(problem-locator-client)'
    )
    $arguments += $script:JourneyFullMcpTools
    if (-not [string]::IsNullOrWhiteSpace($DebugFile)) {
        $arguments += @('--debug', 'hooks', '--debug-file', $DebugFile)
    }
    $arguments += @(
        '--permission-mode', 'dontAsk',
        '--no-chrome',
        '--no-session-persistence',
        $Prompt
    )
    return $arguments
}

function Confirm-JourneyClaudeVersion {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $stdout = Join-Path $EvidenceRoot 'windows-claude-version.stdout.txt'
    $stderr = Join-Path $EvidenceRoot 'windows-claude-version.stderr.txt'
    $stdoutExists = Test-Path -LiteralPath $stdout -PathType Leaf
    $stderrExists = Test-Path -LiteralPath $stderr -PathType Leaf
    Assert-Journey ($stdoutExists -eq $stderrExists) 'Windows Claude version evidence pair must be complete'
    $stdoutKey = [System.IO.Path]::GetFullPath($stdout).ToLowerInvariant()
    $isFreshReservation = $script:JourneyReservedOutputs.ContainsKey($stdoutKey) -and -not [bool]$script:JourneyCompletedOutputs[$stdoutKey]
    if ($stdoutExists -and -not $isFreshReservation) {
        $versionText = [System.IO.File]::ReadAllText($stdout, $script:JourneyUtf8).Trim()
        Assert-Journey ($versionText -ceq "$($script:JourneyClaudeVersion) (Claude Code)") 'cached Windows Claude version'
        return
    }
    Assert-Journey $isFreshReservation 'Windows Claude version outputs were not pre-reserved'
    $exitCode = Invoke-JourneyCapturedProcess -FilePath $script:JourneyClaudeExe -Arguments @('--version') -WorkingDirectory $script:JourneyRepoRoot -StdoutPath $stdout -StderrPath $stderr -TimeoutSeconds $script:JourneyClaudeVersionTimeoutSeconds
    Assert-Journey ($exitCode -eq 0) 'Windows Claude --version exit code'
    $versionText = [System.IO.File]::ReadAllText($stdout, $script:JourneyUtf8).Trim()
    Assert-Journey ($versionText -ceq "$($script:JourneyClaudeVersion) (Claude Code)") 'Windows Claude must be exactly 2.1.150'
}

function Get-JourneyToolResultPayload {
    param($Event, [string]$ExpectedToolUseId)
    Assert-Journey (Test-JourneyProperty $Event 'tool_use_result') 'user tool_result event must carry one top-level tool_use_result object'
    Assert-Journey (-not (Test-JourneyProperty $Event 'toolUseResult')) 'camel-case toolUseResult fallback is forbidden'
    $rawResult = Get-JourneyProperty $Event 'tool_use_result' -Required
    Assert-JourneyJsonObject $rawResult 'top-level tool_use_result'
    if (Test-JourneyProperty $rawResult 'tool_use_id') {
        Assert-Journey ((Get-JourneyStringProperty $rawResult 'tool_use_id') -ceq $ExpectedToolUseId) 'top-level tool_use_result.tool_use_id mismatch'
    }
    if (Test-JourneyProperty $rawResult 'toolUseId') {
        Assert-Journey ((Get-JourneyStringProperty $rawResult 'toolUseId') -ceq $ExpectedToolUseId) 'top-level tool_use_result.toolUseId mismatch'
    }
    if (Test-JourneyProperty $rawResult 'isError') {
        Assert-Journey (-not (Get-JourneyBooleanProperty $rawResult 'isError')) 'top-level tool_use_result.isError must be false'
    }
    if (Test-JourneyProperty $rawResult 'is_error') {
        Assert-Journey (-not (Get-JourneyBooleanProperty $rawResult 'is_error')) 'top-level tool_use_result.is_error must be false'
    }
    Assert-Journey (Test-JourneyProperty $rawResult 'structuredContent') 'MCP tool_use_result must carry structuredContent'
    $payload = Get-JourneyProperty $rawResult 'structuredContent' -Required
    Assert-JourneyJsonObject $payload 'MCP tool_result structuredContent'
    return $payload
}

function Get-JourneyToolName {
    param([Parameter(Mandatory = $true)][string]$FullName)
    $prefix = 'mcp__problem-locator__'
    if ($FullName.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
        return $FullName.Substring($prefix.Length)
    }
    return $FullName
}

function Read-JourneyHookEvents {
    param([Parameter(Mandatory = $true)][string]$Path)

    Assert-Journey (Test-Path -LiteralPath $Path -PathType Leaf) 'client Hook DFX log is absent'
    $events = @()
    $lineNumber = 0
    foreach ($line in [IO.File]::ReadLines($Path, $script:JourneyUtf8)) {
        $lineNumber++
        Assert-Journey (-not [string]::IsNullOrWhiteSpace($line)) "client Hook DFX line $lineNumber is empty"
        try {
            $event = $line | ConvertFrom-Json
        }
        catch {
            throw "journey assertion failed: invalid client Hook DFX line $lineNumber"
        }
        Assert-JourneyJsonObject $event "client Hook DFX line $lineNumber"
        $events += $event
    }
    Assert-Journey ($events.Count -gt 0) 'client Hook DFX log is empty'
    return $events
}

function Assert-JourneyHookEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Audit
    )

    $events = @(Read-JourneyHookEvents $Path)
    foreach ($record in @($Audit.mcp_records)) {
        $started = @($events | Where-Object {
            (Get-JourneyStringProperty $_ 'event') -ceq 'client.hook.tool.started' -and
            (Get-JourneyStringProperty $_ 'tool_use_id') -ceq $record.tool_use_id
        })
        Assert-Journey ($started.Count -eq 1) "Hook started event for $($record.tool_use_id)"
        $startedEvent = $started[0]
        Assert-Journey ((Get-JourneyStringProperty $startedEvent 'source') -ceq 'claude_code_hook') 'Hook source'
        Assert-Journey ((Get-JourneyStringProperty $startedEvent 'hook_version') -ceq '1.0.3') 'Hook version'
        Assert-Journey ((Get-JourneyStringProperty $startedEvent 'tool_name') -ceq $record.full_name) 'Hook full tool name'
        Assert-Journey ((Get-JourneyStringProperty $startedEvent 'logical_tool') -ceq $record.tool_name) 'Hook logical tool name'
        $arguments = Get-JourneyProperty $startedEvent 'arguments' -Required
        Assert-JourneyJsonObject $arguments 'Hook arguments'
        Assert-Journey (
            (($arguments | ConvertTo-Json -Depth 100 -Compress) -ceq ($record.input | ConvertTo-Json -Depth 100 -Compress))
        ) "Hook arguments differ from stream-json for $($record.tool_use_id)"
        $types = Get-JourneyProperty $startedEvent 'argument_json_types' -Required
        Assert-JourneyJsonObject $types 'Hook argument_json_types'
        if (Test-JourneyProperty $record.input 'problem_spec') {
            Assert-Journey ((Get-JourneyStringProperty $types 'problem_spec') -ceq 'object') 'problem_spec Hook JSON type'
        }
        if (Test-JourneyProperty $record.input 'inputs') {
            Assert-Journey ((Get-JourneyStringProperty $types 'inputs') -ceq 'object') 'inputs Hook JSON type'
        }
        if (Test-JourneyProperty $record.input 'attachment_ids') {
            Assert-Journey ((Get-JourneyStringProperty $types 'attachment_ids') -ceq 'array') 'attachment_ids Hook JSON type'
        }
        if ($record.tool_name -ceq 'problem_locator_prepare_attachment') {
            Assert-Journey (Test-JourneyProperty $arguments 'name') 'prepare Hook arguments require name'
            Assert-Journey (Test-JourneyProperty $arguments 'declared_size') 'prepare Hook arguments require declared_size'
            Assert-Journey (-not (Test-JourneyProperty $arguments 'attachment_name')) 'prepare Hook arguments forbid attachment_name'
            Assert-Journey (-not (Test-JourneyProperty $arguments 'declared_byte_count')) 'prepare Hook arguments forbid declared_byte_count'
        }
        $returned = @($events | Where-Object {
            (Get-JourneyStringProperty $_ 'event') -ceq 'client.hook.tool.returned' -and
            (Get-JourneyStringProperty $_ 'tool_use_id') -ceq $record.tool_use_id
        })
        Assert-Journey ($returned.Count -eq 1) "Hook returned event for $($record.tool_use_id)"
    }
}

function Get-JourneyUserContentDisposition {
    param($Event, $Message, $Content)
    Assert-Journey ((Get-JourneyStringProperty $Message 'role') -ceq 'user') 'user event message role must be user'
    Assert-Journey (-not (Test-JourneyProperty $Event 'toolUseResult')) 'camel-case toolUseResult fallback is forbidden'
    $blocks = @($Content)
    $toolResultBlocks = @()
    foreach ($block in $blocks) {
        Assert-JourneyJsonObject $block 'user stream-json content block'
        if ((Get-JourneyStringProperty $block 'type') -ceq 'tool_result') {
            $toolResultBlocks += $block
        }
    }
    $hasTopLevelResult = Test-JourneyProperty $Event 'tool_use_result'
    if ($toolResultBlocks.Count -eq 0 -and -not $hasTopLevelResult) {
        foreach ($block in $blocks) {
            Assert-Journey ((Get-JourneyStringProperty $block 'type') -ceq 'text') 'user event without tool_result may contain only text blocks'
        }
        return 'ignore_text'
    }
    Assert-Journey ($blocks.Count -eq 1) 'each user tool-result event must contain exactly one content block'
    Assert-Journey ($toolResultBlocks.Count -eq 1) 'user tool-result event must contain exactly one tool_result block'
    Assert-Journey $hasTopLevelResult 'user tool_result event must carry one top-level tool_use_result object'
    Assert-JourneyJsonObject (Get-JourneyProperty $Event 'tool_use_result' -Required) 'top-level tool_use_result'
    [void](Get-JourneyStringProperty $toolResultBlocks[0] 'tool_use_id')
    return 'tool_result'
}

function Read-JourneyClaudeAudit {
    param([Parameter(Mandatory = $true)][string]$StreamPath)
    Assert-Journey (Test-Path -LiteralPath $StreamPath -PathType Leaf) 'Claude stream-json stdout is absent'
    $toolUses = @()
    $byId = @{}
    $initEvents = @()
    $finalResults = @()
    $lineNumber = 0
    $lastEventType = $null
    foreach ($line in [System.IO.File]::ReadLines($StreamPath, $script:JourneyUtf8)) {
        $lineNumber++
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        try {
            $event = $line | ConvertFrom-Json
        }
        catch {
            throw "journey assertion failed: invalid stream-json line $lineNumber"
        }
        Assert-JourneyJsonObject $event "stream-json event line $lineNumber"
        $eventType = Get-JourneyStringProperty $event 'type'
        $lastEventType = $eventType
        if ($eventType -ceq 'system' -and (Get-JourneyStringProperty $event 'subtype') -ceq 'init') {
            $initEvents += $event
        }
        if ($eventType -ceq 'result') {
            $finalResults += $event
        }
        if (-not (Test-JourneyProperty $event 'message')) {
            continue
        }
        $message = Get-JourneyProperty $event 'message'
        Assert-JourneyJsonObject $message "stream-json message line $lineNumber"
        if (-not (Test-JourneyProperty $message 'content')) {
            continue
        }
        $content = Get-JourneyProperty $message 'content'
        Assert-JourneyJsonArray $content "stream-json message content line $lineNumber"
        if ($eventType -ceq 'user') {
            $disposition = Get-JourneyUserContentDisposition -Event $event -Message $message -Content $content
            if ($disposition -ceq 'ignore_text') {
                continue
            }
        }
        foreach ($block in @($content)) {
            Assert-JourneyJsonObject $block "stream-json content block line $lineNumber"
            $blockType = Get-JourneyStringProperty $block 'type'
            if ($blockType -ceq 'tool_use') {
                Assert-Journey ($eventType -ceq 'assistant') 'tool_use is authoritative only in an assistant event'
                Assert-Journey ((Get-JourneyStringProperty $message 'role') -ceq 'assistant') 'tool_use message role must be assistant'
                $id = Get-JourneyStringProperty $block 'id'
                Assert-Journey (-not $byId.ContainsKey($id)) "duplicate tool_use id $id"
                $fullName = Get-JourneyStringProperty $block 'name'
                $name = Get-JourneyToolName $fullName
                $allowed = @('Skill') + $script:JourneyFullMcpTools
                Assert-Journey ($allowed -ccontains $fullName) "unexpected Claude tool $fullName"
                $input = Get-JourneyProperty $block 'input' -Required
                Assert-JourneyJsonObject $input "tool_use input for $fullName"
                $use = [PSCustomObject][ordered]@{
                    ordinal = $toolUses.Count
                    tool_use_id = $id
                    full_name = $fullName
                    tool_name = $name
                    input = $input
                    result = $null
                }
                $toolUses += $use
                $byId[$id] = $use
                continue
            }
            if ($blockType -ceq 'tool_result') {
                Assert-Journey ($eventType -ceq 'user') 'tool_result is authoritative only in a user event'
                Assert-Journey ((Get-JourneyStringProperty $message 'role') -ceq 'user') 'tool_result message role must be user'
                $id = Get-JourneyStringProperty $block 'tool_use_id'
                Assert-Journey ($byId.ContainsKey($id)) "tool_result without matching tool_use: $id"
                $use = $byId[$id]
                Assert-Journey ($null -eq $use.result) "duplicate tool_result: $id"
                if (Test-JourneyProperty $block 'is_error') {
                    Assert-Journey (-not (Get-JourneyBooleanProperty $block 'is_error')) "tool_result marked is_error: $id"
                }
                if ($use.full_name -ceq 'Skill') {
                    $use.result = [PSCustomObject]@{ skill_loaded = $true }
                }
                else {
                    $use.result = Get-JourneyToolResultPayload -Event $event -ExpectedToolUseId $id
                }
            }
        }
    }
    Assert-Journey ($initEvents.Count -eq 1) 'stream-json must contain exactly one system/init event'
    Assert-Journey ($finalResults.Count -eq 1) 'stream-json must contain exactly one final result event'
    Assert-Journey ($lastEventType -ceq 'result') 'the final non-empty stream-json event must be result'
    $finalResultEvent = $finalResults[0]
    Assert-Journey ((Get-JourneyStringProperty $finalResultEvent 'subtype') -ceq 'success') 'final result subtype must be success'
    Assert-Journey (-not (Get-JourneyBooleanProperty $finalResultEvent 'is_error')) 'final result is_error must be false'
    $init = $initEvents[0]
    Assert-Journey ((Get-JourneyStringProperty $init 'cwd').Equals($script:JourneyRepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) 'Claude cwd must be repository root'
    Assert-Journey ((Get-JourneyStringProperty $init 'model') -ceq $script:JourneyEffectiveModel) 'effective Windows Claude model must be deepseek-v4-flash[1m]'
    Assert-Journey ((Get-JourneyStringProperty $init 'permissionMode') -ceq 'dontAsk') 'permission mode must be dontAsk'

    $reportedToolsValue = Get-JourneyProperty $init 'tools' -Required
    Assert-JourneyJsonArray $reportedToolsValue 'system/init tools'
    $reportedTools = @($reportedToolsValue | ForEach-Object { Assert-Journey ($_ -is [string]) 'system/init tool names must be strings'; $_ }) | Sort-Object
    $expectedTools = @(@('Skill') + $script:JourneyFullMcpTools | Sort-Object)
    Assert-JourneyExactStrings -Actual $reportedTools -Expected $expectedTools -Label 'system/init tools'

    $mcpServersValue = Get-JourneyProperty $init 'mcp_servers' -Required
    Assert-JourneyJsonArray $mcpServersValue 'system/init mcp_servers'
    $mcpServers = @($mcpServersValue)
    Assert-Journey ($mcpServers.Count -eq 1) 'strict MCP config must load exactly one server'
    Assert-JourneyJsonObject $mcpServers[0] 'system/init MCP server'
    $serverName = if (Test-JourneyProperty $mcpServers[0] 'name') { Get-JourneyStringProperty $mcpServers[0] 'name' } else { Get-JourneyStringProperty $mcpServers[0] 'serverName' }
    Assert-Journey ($serverName -ceq 'problem-locator') 'strict MCP server name'
    if (Test-JourneyProperty $mcpServers[0] 'status') {
        Assert-Journey ((Get-JourneyStringProperty $mcpServers[0] 'status') -ceq 'connected') 'problem-locator MCP must be connected'
    }

    foreach ($use in $toolUses) {
        Assert-Journey ($null -ne $use.result) "missing tool_result for $($use.full_name)"
    }
    $skillUses = @($toolUses | Where-Object { $_.full_name -ceq 'Skill' })
    Assert-Journey ($skillUses.Count -eq 1) 'Claude must invoke Skill(problem-locator-client) exactly once'
    Assert-Journey ($skillUses[0].ordinal -eq 0) 'problem-locator-client Skill must be the first tool_use'
    $skillInputNames = @($skillUses[0].input.PSObject.Properties.Name)
    Assert-Journey ($skillInputNames -ccontains 'skill') 'Skill tool input requires skill'
    foreach ($name in $skillInputNames) {
        Assert-Journey (@('skill', 'args') -ccontains $name) "Skill tool input has unexpected property $name"
    }
    Assert-Journey ((Get-JourneyStringProperty $skillUses[0].input 'skill') -ceq 'problem-locator-client') 'Skill tool input must be exact'
    if (Test-JourneyProperty $skillUses[0].input 'args') {
        [void](Get-JourneyStringProperty $skillUses[0].input 'args')
    }
    $mcpRecords = @($toolUses | Where-Object { $_.full_name -cne 'Skill' })
    Assert-Journey ($mcpRecords.Count -gt 0) 'Claude did not call an MCP business tool'
    return [PSCustomObject][ordered]@{
        init = [PSCustomObject][ordered]@{
            cwd = $script:JourneyRepoRoot
            effective_model = $script:JourneyEffectiveModel
            permission_mode = 'dontAsk'
            tools = @('Skill') + $script:JourneyFullMcpTools
            mcp_servers = @([PSCustomObject]@{ name = 'problem-locator'; url = $script:JourneyMcpUrl; always_load = $true })
        }
        mcp_records = $mcpRecords
        skill_invocation_count = 1
        final_result = [PSCustomObject][ordered]@{ subtype = 'success'; is_error = $false }
    }
}

function Get-JourneySuccessData {
    param($Record)
    $result = $Record.result
    Assert-JourneyExactProperties $result @('ok', 'data', 'error') "$($Record.tool_name) Envelope"
    Assert-Journey (Get-JourneyBooleanProperty $result 'ok') "$($Record.tool_name) returned a business error"
    Assert-Journey ($null -eq (Get-JourneyProperty $result 'error' -Required)) "$($Record.tool_name) success error must be null"
    $data = Get-JourneyProperty $result 'data' -Required
    Assert-JourneyJsonObject $data "$($Record.tool_name) success data"
    return $data
}

function Get-JourneyRecordApplicationResponse {
    param($Record)
    $data = Get-JourneySuccessData $Record
    if ($Record.tool_name -ceq 'problem_locator_prepare_attachment') {
        Assert-JourneyExactProperties $data @('application_response', 'upload') 'prepare success data'
        $response = Get-JourneyProperty $data 'application_response' -Required
        Assert-JourneyJsonObject $response 'prepare application_response'
        return $response
    }
    if ($Record.tool_name -in @('problem_locator_create_case', 'problem_locator_submit_supplement', 'problem_locator_resume_case', 'problem_locator_cancel_case')) {
        return $data
    }
    return $null
}

function Assert-JourneyApplicationResponseShape {
    param($Response, [string]$Label)
    Assert-JourneyExactProperties $Response @('business_receipt', 'case_view', 'wait_timed_out', 'dispatch_pending') "$Label ApplicationResponse"
    [void](Get-JourneyBooleanProperty $Response 'wait_timed_out')
    [void](Get-JourneyBooleanProperty $Response 'dispatch_pending')
    $receipt = Get-JourneyProperty $Response 'business_receipt' -Required
    Assert-JourneyJsonObject $receipt "$Label business_receipt"
    $caseView = Get-JourneyProperty $Response 'case_view' -Required
    if ($null -ne $caseView) {
        Assert-JourneyJsonObject $caseView "$Label case_view"
    }
}

function Assert-JourneyReceipt {
    param(
        $Response,
        [string]$Operation,
        [string]$Status,
        [string]$PrimaryId,
        [string]$CaseId,
        [bool]$RequiresJob,
        [string]$Label
    )
    Assert-JourneyApplicationResponseShape $Response $Label
    $receipt = Get-JourneyProperty $Response 'business_receipt' -Required
    Assert-JourneyExactProperties $receipt @('operation', 'primary_resource_id', 'case_id', 'case_revision', 'job_id', 'status') "$Label receipt"
    Assert-Journey ((Get-JourneyStringProperty $receipt 'operation') -ceq $Operation) "$Label receipt operation"
    Assert-Journey ((Get-JourneyStringProperty $receipt 'status') -ceq $Status) "$Label receipt status"
    Assert-Journey ((Get-JourneyStringProperty $receipt 'primary_resource_id') -ceq $PrimaryId) "$Label receipt primary_resource_id"
    Assert-Journey ((Get-JourneyStringProperty $receipt 'case_id') -ceq $CaseId) "$Label receipt case_id"
    $revision = Get-JourneyIntegerProperty $receipt 'case_revision'
    Assert-Journey ($revision -gt 0) "$Label receipt case_revision"
    $jobId = Get-JourneyProperty $receipt 'job_id' -Required
    if ($RequiresJob) {
        Assert-JourneyUuid $jobId "$Label receipt job_id"
    }
    else {
        Assert-Journey ($null -eq $jobId) "$Label receipt job_id must be null"
    }
    return $revision
}

function Assert-JourneyCaseViewIdentity {
    param($CaseView, [string]$CaseId, [string]$Label)
    Assert-JourneyJsonObject $CaseView "$Label case_view"
    Assert-Journey ((Get-JourneyStringProperty $CaseView 'case_id') -ceq $CaseId) "$Label case_view.case_id"
    Assert-Journey ((Get-JourneyIntegerProperty $CaseView 'case_revision') -gt 0) "$Label case_view.case_revision"
    Assert-Journey ((Get-JourneyIntegerProperty $CaseView 'diagnosis_state_revision') -gt 0) "$Label diagnosis_state_revision"
    [void](Get-JourneyStringProperty $CaseView 'status')
}

function Assert-JourneyCaseIdentityAndRevisionOrder {
    param($Records, [string]$CaseId, [int64]$StartingRevision)
    $lastRevision = $StartingRevision
    foreach ($record in @($Records)) {
        $data = Get-JourneySuccessData $record
        $response = Get-JourneyRecordApplicationResponse $record
        if ($null -ne $response) {
            Assert-JourneyApplicationResponseShape $response $record.tool_name
            $receipt = Get-JourneyProperty $response 'business_receipt' -Required
            $receiptRevision = Get-JourneyIntegerProperty $receipt 'case_revision'
            Assert-Journey ($receiptRevision -ge $lastRevision) "$($record.tool_name) receipt revisions must be monotonic"
            $lastRevision = $receiptRevision
            $view = Get-JourneyProperty $response 'case_view' -Required
            if ($null -ne $view) {
                Assert-JourneyCaseViewIdentity $view $CaseId $record.tool_name
                $viewRevision = Get-JourneyIntegerProperty $view 'case_revision'
                Assert-Journey ($viewRevision -ge $lastRevision) "$($record.tool_name) Case view revisions must be monotonic"
                $lastRevision = $viewRevision
            }
            continue
        }
        if ($record.tool_name -ceq 'problem_locator_get_case') {
            Assert-JourneyExactProperties $data @('case_view', 'wait_timed_out') 'get_case data'
            [void](Get-JourneyBooleanProperty $data 'wait_timed_out')
            $view = Get-JourneyProperty $data 'case_view' -Required
            Assert-Journey ($null -ne $view) 'get_case healthy view must not be null'
            Assert-JourneyCaseViewIdentity $view $CaseId 'get_case'
            $viewRevision = Get-JourneyIntegerProperty $view 'case_revision'
            Assert-Journey ($viewRevision -ge $lastRevision) 'get_case revisions must be monotonic'
            $lastRevision = $viewRevision
        }
    }
    return $lastRevision
}

function Get-JourneyOpenRequirements {
    param($CaseView)
    $requirements = Get-JourneyProperty $CaseView 'pending_requirements' -Required
    Assert-JourneyJsonArray $requirements 'pending_requirements'
    return @($requirements | Where-Object { (Get-JourneyStringProperty $_ 'status') -ceq 'OPEN' })
}

function ConvertTo-JourneyCanonicalRequirementPairs {
    param([string[]]$Names, [string[]]$Kinds, [string]$Label)
    Assert-Journey ($Names.Count -eq $Kinds.Count) "$Label name-kind count"
    $seen = @{}
    [string[]]$pairs = @()
    for ($index = 0; $index -lt $Names.Count; $index++) {
        $name = $Names[$index]
        $kind = $Kinds[$index]
        Assert-Journey (-not [string]::IsNullOrWhiteSpace($name)) "$Label name nonempty"
        Assert-Journey (-not [string]::IsNullOrWhiteSpace($kind)) "$Label kind nonempty"
        Assert-Journey (-not $seen.ContainsKey($name)) "$Label duplicate requirement name"
        $seen[$name] = $true
        $pairs += "$name`0$kind"
    }
    [Array]::Sort($pairs, [StringComparer]::Ordinal)
    return $pairs
}

function Assert-JourneyOpenRequirements {
    param(
        $CaseView,
        [string[]]$Names,
        [string[]]$Kinds,
        [string]$Label
    )
    $open = @(Get-JourneyOpenRequirements $CaseView)
    $actualNames = @($open | ForEach-Object { Get-JourneyStringProperty $_ 'name' })
    $actualKinds = @($open | ForEach-Object { Get-JourneyStringProperty $_ 'kind' })
    $actualPairs = @(ConvertTo-JourneyCanonicalRequirementPairs $actualNames $actualKinds "$Label actual")
    $expectedPairs = @(ConvertTo-JourneyCanonicalRequirementPairs $Names $Kinds "$Label expected")
    Assert-JourneyExactStrings -Actual $actualPairs -Expected $expectedPairs -Label "$Label canonical name-kind pairs"
}

function Assert-JourneySkillRef {
    param($CaseView)
    $skill = Get-JourneyProperty $CaseView 'selected_skill_ref' -Required
    Assert-JourneyJsonObject $skill 'selected_skill_ref'
    Assert-Journey ((Get-JourneyStringProperty $skill 'id') -ceq $script:JourneySkillId) 'selected Skill id'
    Assert-Journey ((Get-JourneyStringProperty $skill 'version') -ceq $script:JourneySkillVersion) 'selected Skill version'
    Assert-Journey ((Get-JourneyStringProperty $skill 'content_hash') -ceq $script:JourneySkillHash) 'selected Skill product hash'
}

function Assert-JourneyProblemSpecInput {
    param($Actual)
    Assert-JourneyJsonObject $Actual 'problem_spec input'
    $expected = Get-JourneyProblemSpec
    foreach ($name in @('statement', 'expected_behavior', 'actual_behavior', 'scope')) {
        Assert-Journey ((Get-JourneyStringProperty $Actual $name) -ceq [string]$expected[$name]) "problem_spec.$name"
    }
    foreach ($name in @('goals', 'non_goals', 'constraints', 'completion_criteria')) {
        $arrayValue = Get-JourneyProperty $Actual $name -Required
        Assert-JourneyStringArray $arrayValue "problem_spec.$name"
        Assert-JourneyExactStrings -Actual $arrayValue -Expected @($expected[$name]) -Label "problem_spec.$name"
    }
}

function Test-JourneyCaseWithOpenNames {
    param($Record, [string]$Status, [string[]]$Names, [string[]]$Kinds)
    if ($Record.tool_name -cne 'problem_locator_get_case') {
        return $false
    }
    $data = Get-JourneySuccessData $Record
    $view = Get-JourneyProperty $data 'case_view' -Required
    if ((Get-JourneyStringProperty $view 'status') -cne $Status) {
        return $false
    }
    $open = @(Get-JourneyOpenRequirements $view)
    try {
        $actualNames = @($open | ForEach-Object { Get-JourneyStringProperty $_ 'name' })
        $actualKinds = @($open | ForEach-Object { Get-JourneyStringProperty $_ 'kind' })
        $actualPairs = @(ConvertTo-JourneyCanonicalRequirementPairs $actualNames $actualKinds 'observed requirements')
        $expectedPairs = @(ConvertTo-JourneyCanonicalRequirementPairs $Names $Kinds 'expected requirements')
        if ($actualPairs.Count -ne $expectedPairs.Count) { return $false }
        for ($index = 0; $index -lt $expectedPairs.Count; $index++) {
            if ($actualPairs[$index] -cne $expectedPairs[$index]) { return $false }
        }
    }
    catch {
        return $false
    }
    return $true
}

function Assert-JourneyGetArguments {
    param($Record, [string]$CaseId)
    $input = $Record.input
    Assert-JourneyJsonObject $input 'get_case input'
    $inputNames = @($input.PSObject.Properties.Name)
    Assert-Journey ($inputNames -ccontains 'case_id') 'get_case input requires case_id'
    Assert-Journey ($inputNames -ccontains 'wait_seconds') 'get_case input requires wait_seconds'
    foreach ($name in $inputNames) {
        Assert-Journey (@('case_id', 'wait_for_job_id', 'wait_seconds') -ccontains $name) "get_case input has unexpected property $name"
    }
    Assert-Journey ((Get-JourneyStringProperty $input 'case_id') -ceq $CaseId) 'get_case case_id'
    $waitSeconds = Get-JourneyIntegerProperty $input 'wait_seconds'
    Assert-Journey ($waitSeconds -ge 0 -and $waitSeconds -le 30) 'get_case wait_seconds range'
    if (Test-JourneyProperty $input 'wait_for_job_id') {
        $waitId = Get-JourneyProperty $input 'wait_for_job_id'
        if ($null -ne $waitId) {
            Assert-JourneyUuid $waitId 'get_case wait_for_job_id'
        }
    }
}

function Test-JourneyNull {
    param($Value)
    return $null -eq $Value
}

function Read-JourneyPhase1StateValidated {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $state = Read-JourneyJson (Join-Path $EvidenceRoot 'phase1-state.json')
    Assert-JourneyExactProperties $state @('schema_version', 'attempt', 'case_id', 'attachment_id', 'prepared_case_revision', 'selected_skill_ref', 'upload_descriptor', 'request_ids', 'phase1_mcp_call_count', 'validation_corrections') 'phase1 state'
    Assert-Journey ((Get-JourneyIntegerProperty $state 'schema_version') -eq 1) 'phase1 state schema_version'
    Assert-Journey ((Get-JourneyStringProperty $state 'attempt') -ceq (Get-JourneyAttemptLabel $EvidenceRoot)) 'phase1 state attempt'
    $caseId = Get-JourneyStringProperty $state 'case_id'
    $attachmentId = Get-JourneyStringProperty $state 'attachment_id'
    Assert-JourneyUuid $caseId 'phase1 state case_id'
    Assert-JourneyUuid $attachmentId 'phase1 state attachment_id'
    Assert-Journey ((Get-JourneyIntegerProperty $state 'prepared_case_revision') -gt 0) 'phase1 state prepared_case_revision'
    Assert-Journey ((Get-JourneyIntegerProperty $state 'phase1_mcp_call_count') -gt 0) 'phase1 state MCP call count'
    $corrections = Get-JourneyProperty $state 'validation_corrections' -Required
    Assert-JourneyJsonArray $corrections 'phase1 validation corrections'
    Assert-Journey (@($corrections).Count -le 4) 'phase1 validation correction count'
    $prepareCorrectionCount = @($corrections | Where-Object { (Get-JourneyStringProperty $_ 'tool_name') -ceq 'problem_locator_prepare_attachment' }).Count
    $getCorrectionCount = @($corrections | Where-Object { (Get-JourneyStringProperty $_ 'tool_name') -ceq 'problem_locator_get_case' }).Count
    Assert-Journey ($prepareCorrectionCount -le 1) 'phase1 prepare_attachment validation correction count'
    Assert-Journey ($getCorrectionCount -le 3) 'phase1 get_case validation correction count'
    foreach ($correction in @($corrections)) {
        Assert-JourneyExactProperties $correction @('tool_name', 'error_code', 'failed_ordinal', 'successful_ordinal', 'zero_side_effect_required') 'phase1 validation correction'
        Assert-Journey (@('problem_locator_prepare_attachment', 'problem_locator_get_case') -ccontains (Get-JourneyStringProperty $correction 'tool_name')) 'phase1 correction tool'
        Assert-Journey ((Get-JourneyStringProperty $correction 'error_code') -ceq 'VALIDATION_ERROR') 'phase1 correction code'
        Assert-Journey ((Get-JourneyIntegerProperty $correction 'failed_ordinal') -lt (Get-JourneyIntegerProperty $correction 'successful_ordinal')) 'phase1 correction order'
        Assert-Journey (Get-JourneyBooleanProperty $correction 'zero_side_effect_required') 'phase1 correction side-effect audit requirement'
    }
    $stateSkill = Get-JourneyProperty $state 'selected_skill_ref' -Required
    Assert-JourneyJsonObject $stateSkill 'phase1 state selected_skill_ref'
    Assert-Journey ((Get-JourneyStringProperty $stateSkill 'id') -ceq $script:JourneySkillId) 'phase1 state selected Skill id'
    Assert-Journey ((Get-JourneyStringProperty $stateSkill 'version') -ceq $script:JourneySkillVersion) 'phase1 state selected Skill version'
    Assert-Journey ((Get-JourneyStringProperty $stateSkill 'content_hash') -ceq $script:JourneySkillHash) 'phase1 state selected Skill hash'
    $expectedRequestIds = Get-JourneyRequestIds $EvidenceRoot
    $stateRequestIds = Get-JourneyProperty $state 'request_ids' -Required
    Assert-JourneyExactProperties $stateRequestIds @($expectedRequestIds.Keys) 'phase1 state request_ids'
    foreach ($name in $expectedRequestIds.Keys) {
        Assert-Journey ((Get-JourneyStringProperty $stateRequestIds $name) -ceq $expectedRequestIds[$name]) "phase1 state request_id $name"
    }
    $descriptor = Get-JourneyProperty $state 'upload_descriptor' -Required
    Assert-JourneyExactProperties $descriptor @('attachment_id', 'method', 'url', 'required_headers', 'max_bytes', 'expires_at') 'phase1 state UploadDescriptor'
    Assert-Journey ((Get-JourneyStringProperty $descriptor 'attachment_id') -ceq $attachmentId) 'phase1 state descriptor attachment_id'
    Assert-Journey ((Get-JourneyStringProperty $descriptor 'method') -ceq 'PUT') 'phase1 state descriptor method'
    Assert-Journey ((Get-JourneyStringProperty $descriptor 'url') -ceq "$($script:JourneyServiceBaseUrl)/api/v1/attachments/$attachmentId/content") 'phase1 state descriptor URL'
    Assert-Journey ((Get-JourneyIntegerProperty $descriptor 'max_bytes') -eq $script:JourneyMaxAttachmentBytes) 'phase1 state descriptor max_bytes'
    Assert-Journey ($null -eq (Get-JourneyProperty $descriptor 'expires_at' -Required)) 'phase1 state descriptor expires_at'
    $headers = Get-JourneyProperty $descriptor 'required_headers' -Required
    Assert-JourneyExactProperties $headers @('Content-Length', 'Content-Type', 'Idempotency-Key', 'X-Content-SHA256') 'phase1 state descriptor headers'
    Assert-Journey ((Get-JourneyStringProperty $headers 'Idempotency-Key') -ceq $attachmentId) 'phase1 state Idempotency-Key'
    Assert-Journey ((Get-JourneyStringProperty $headers 'Content-Type') -ceq 'application/zip') 'phase1 state Content-Type'
    Assert-Journey ((Get-JourneyStringProperty $headers 'Content-Length') -ceq [string]$script:JourneyZipSize) 'phase1 state Content-Length'
    Assert-Journey ((Get-JourneyStringProperty $headers 'X-Content-SHA256') -ceq $script:JourneyZipSha256) 'phase1 state X-Content-SHA256'
    return $state
}

function Read-JourneyUploadStateValidated {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $state = Read-JourneyJson (Join-Path $EvidenceRoot 'upload-state.json')
    Assert-JourneyExactProperties $state @('schema_version', 'attempt', 'case_id', 'attachment_id', 'status', 'case_revision', 'zip_size', 'zip_sha256', 'upload_url', 'explicit_descriptor_headers', 'http_status') 'upload state'
    Assert-Journey ((Get-JourneyIntegerProperty $state 'schema_version') -eq 1) 'upload state schema_version'
    Assert-Journey ((Get-JourneyStringProperty $state 'attempt') -ceq (Get-JourneyAttemptLabel $EvidenceRoot)) 'upload state attempt'
    $caseId = Get-JourneyStringProperty $state 'case_id'
    $attachmentId = Get-JourneyStringProperty $state 'attachment_id'
    Assert-JourneyUuid $caseId 'upload state case_id'
    Assert-JourneyUuid $attachmentId 'upload state attachment_id'
    Assert-Journey ((Get-JourneyStringProperty $state 'status') -ceq 'READY') 'upload state status'
    $uploadRevision = Get-JourneyIntegerProperty $state 'case_revision'
    Assert-Journey ($uploadRevision -gt 0) 'upload state case_revision'
    Assert-Journey ((Get-JourneyIntegerProperty $state 'zip_size') -eq $script:JourneyZipSize) 'upload state ZIP size'
    Assert-Journey ((Get-JourneyStringProperty $state 'zip_sha256') -ceq $script:JourneyZipSha256) 'upload state ZIP SHA-256'
    Assert-Journey ((Get-JourneyStringProperty $state 'upload_url') -ceq "$($script:JourneyServiceBaseUrl)/api/v1/attachments/$attachmentId/content") 'upload state loopback URL'
    $headerNames = Get-JourneyProperty $state 'explicit_descriptor_headers' -Required
    Assert-JourneyStringArray $headerNames 'upload state explicit descriptor headers'
    Assert-JourneyExactStrings $headerNames @('Idempotency-Key', 'Content-Type', 'Content-Length', 'X-Content-SHA256') 'upload state explicit descriptor headers'
    Assert-Journey ((Get-JourneyIntegerProperty $state 'http_status') -eq 200) 'upload state HTTP status'

    # Bind this handoff to the exact prepared attachment from phase 1.  A
    # self-consistent upload-state file for some other Case must never be able
    # to redirect phase 3.
    $phase1State = Read-JourneyPhase1StateValidated $EvidenceRoot
    Assert-Journey ((Get-JourneyStringProperty $phase1State 'case_id') -ceq $caseId) 'upload state case_id does not match phase1 state'
    Assert-Journey ((Get-JourneyStringProperty $phase1State 'attachment_id') -ceq $attachmentId) 'upload state attachment_id does not match phase1 state'
    Assert-Journey ($uploadRevision -gt (Get-JourneyIntegerProperty $phase1State 'prepared_case_revision')) 'upload state revision must advance beyond prepare revision'
    $phase1Descriptor = Get-JourneyProperty $phase1State 'upload_descriptor' -Required
    Assert-Journey ((Get-JourneyStringProperty $state 'upload_url') -ceq (Get-JourneyStringProperty $phase1Descriptor 'url')) 'upload state URL does not match phase1 descriptor'
    return $state
}

function Resolve-JourneyPhase1PrepareAttempts {
    param([Parameter(Mandatory = $true)][object[]]$Records)

    $attempts = @($Records | Where-Object { $_.tool_name -ceq 'problem_locator_prepare_attachment' })
    $successful = @()
    $failed = @()
    foreach ($attempt in $attempts) {
        $result = Get-JourneyProperty $attempt 'result' -Required
        Assert-JourneyJsonObject $result 'prepare attempt Envelope'
        Assert-JourneyExactProperties $result @('ok', 'data', 'error') 'prepare attempt Envelope'
        if (Get-JourneyBooleanProperty $result 'ok') {
            $successful += $attempt
        }
        else {
            $failed += $attempt
        }
    }

    Assert-Journey ($successful.Count -eq 1) 'phase1 must contain exactly one successful prepare_attachment call'
    Assert-Journey ($failed.Count -le 1) 'phase1 allows at most one recoverable prepare_attachment validation correction'
    $corrections = @()
    if ($failed.Count -eq 1) {
        $failure = $failed[0]
        $failureInput = Get-JourneyProperty $failure 'input' -Required
        $inputIsEmpty = $null -eq $failureInput
        if (-not $inputIsEmpty -and $failureInput -is [PSCustomObject]) {
            $inputIsEmpty = @($failureInput.PSObject.Properties).Count -eq 0
        }
        Assert-Journey $inputIsEmpty 'recoverable prepare_attachment validation correction must have empty input'
        $failureResult = Get-JourneyProperty $failure 'result' -Required
        Assert-Journey ($null -eq (Get-JourneyProperty $failureResult 'data' -Required)) 'recoverable validation correction data must be null'
        $error = Get-JourneyProperty $failureResult 'error' -Required
        Assert-JourneyJsonObject $error 'recoverable validation correction error'
        Assert-Journey ((Get-JourneyStringProperty $error 'code') -ceq 'VALIDATION_ERROR') 'only VALIDATION_ERROR is recoverable'
        Assert-Journey ($failure.ordinal -lt $successful[0].ordinal) 'validation correction must precede the successful prepare call'
        $corrections = @([PSCustomObject][ordered]@{
            tool_name = 'problem_locator_prepare_attachment'
            error_code = 'VALIDATION_ERROR'
            failed_ordinal = [int]$failure.ordinal
            successful_ordinal = [int]$successful[0].ordinal
            zero_side_effect_required = $true
        })
    }

    return [PSCustomObject][ordered]@{
        successful = $successful[0]
        corrections = $corrections
        failed_ordinals = @($failed | ForEach-Object { [int]$_.ordinal })
    }
}

function Resolve-JourneyEmptyGetCorrections {
    param(
        [Parameter(Mandatory = $true)][object[]]$Records,
        [Parameter(Mandatory = $true)][ValidateRange(0, 3)][int]$MaximumCorrections,
        [Parameter(Mandatory = $true)][string]$PhaseLabel
    )

    $failed = @($Records | Where-Object {
        if ($_.tool_name -cne 'problem_locator_get_case') { return $false }
        $result = Get-JourneyProperty $_ 'result' -Required
        return -not (Get-JourneyBooleanProperty $result 'ok')
    })
    Assert-Journey ($failed.Count -le $MaximumCorrections) "$PhaseLabel allows at most $MaximumCorrections recoverable empty get_case validation corrections"
    $successful = @($Records | Where-Object {
        if ($_.tool_name -cne 'problem_locator_get_case') { return $false }
        $result = Get-JourneyProperty $_ 'result' -Required
        return Get-JourneyBooleanProperty $result 'ok'
    })

    $corrections = @()
    foreach ($failure in $failed) {
        $input = Get-JourneyProperty $failure 'input' -Required
        Assert-JourneyJsonObject $input "$PhaseLabel recoverable get_case input"
        Assert-Journey (@($input.PSObject.Properties).Count -eq 0) "$PhaseLabel recoverable get_case input must be exactly an empty object"
        $result = Get-JourneyProperty $failure 'result' -Required
        Assert-JourneyExactProperties $result @('ok', 'data', 'error') "$PhaseLabel recoverable get_case Envelope"
        Assert-Journey ($null -eq (Get-JourneyProperty $result 'data' -Required)) "$PhaseLabel recoverable get_case data must be null"
        $error = Get-JourneyProperty $result 'error' -Required
        Assert-JourneyExactProperties $error @('code', 'message', 'details', 'retryable') "$PhaseLabel recoverable get_case error"
        Assert-Journey ((Get-JourneyStringProperty $error 'code') -ceq 'VALIDATION_ERROR') "$PhaseLabel recoverable get_case code"
        Assert-Journey ((Get-JourneyStringProperty $error 'message') -ceq 'Request validation failed.') "$PhaseLabel recoverable get_case message"
        Assert-Journey (-not (Get-JourneyBooleanProperty $error 'retryable')) "$PhaseLabel recoverable get_case retryable"
        $details = Get-JourneyProperty $error 'details' -Required
        Assert-JourneyJsonArray $details "$PhaseLabel recoverable get_case details"
        Assert-Journey (@($details).Count -eq 1) "$PhaseLabel recoverable get_case detail count"
        $detail = @($details)[0]
        Assert-JourneyExactProperties $detail @('field', 'resource_type', 'resource_id', 'resource_ref', 'expected', 'actual', 'limit', 'observed') "$PhaseLabel recoverable get_case detail"
        Assert-Journey ((Get-JourneyStringProperty $detail 'field') -ceq 'case_id') "$PhaseLabel recoverable get_case field"
        Assert-Journey ((Get-JourneyStringProperty $detail 'expected') -ceq 'missing: Field required') "$PhaseLabel recoverable get_case expected"
        Assert-Journey ((Get-JourneyStringProperty $detail 'actual') -ceq '{}') "$PhaseLabel recoverable get_case actual"
        foreach ($name in @('resource_type', 'resource_id', 'resource_ref', 'limit', 'observed')) {
            Assert-Journey ($null -eq (Get-JourneyProperty $detail $name -Required)) "$PhaseLabel recoverable get_case $name must be null"
        }
        $nextSuccess = @($successful | Where-Object { [int]$_.ordinal -gt [int]$failure.ordinal } | Sort-Object ordinal | Select-Object -First 1)
        Assert-Journey ($nextSuccess.Count -eq 1) "$PhaseLabel empty get_case must be followed by a successful get_case"
        $corrections += [PSCustomObject][ordered]@{
            tool_name = 'problem_locator_get_case'
            error_code = 'VALIDATION_ERROR'
            failed_ordinal = [int]$failure.ordinal
            successful_ordinal = [int]$nextSuccess[0].ordinal
            zero_side_effect_required = $true
        }
    }

    return [PSCustomObject][ordered]@{
        corrections = $corrections
        failed_ordinals = @($failed | ForEach-Object { [int]$_.ordinal })
    }
}

function Invoke-JourneyPhase1Validation {
    param($Audit, [string]$EvidenceRoot)
    $ids = Get-JourneyRequestIds $EvidenceRoot
    $records = @($Audit.mcp_records)
    $prepareSelection = Resolve-JourneyPhase1PrepareAttempts -Records $records
    $getSelection = Resolve-JourneyEmptyGetCorrections -Records $records -MaximumCorrections 3 -PhaseLabel 'phase1'
    $recoverableOrdinals = @($prepareSelection.failed_ordinals) + @($getSelection.failed_ordinals)
    $validationCorrections = @($prepareSelection.corrections) + @($getSelection.corrections)
    $successfulRecords = @()
    foreach ($record in $records) {
        Assert-Journey (@('problem_locator_create_case', 'problem_locator_get_case', 'problem_locator_submit_supplement', 'problem_locator_prepare_attachment') -ccontains $record.tool_name) "phase1 unexpected business tool $($record.tool_name)"
        if ($recoverableOrdinals -contains [int]$record.ordinal) { continue }
        [void](Get-JourneySuccessData $record)
        $successfulRecords += $record
    }
    $creates = @($records | Where-Object { $_.tool_name -ceq 'problem_locator_create_case' })
    $submits = @($records | Where-Object { $_.tool_name -ceq 'problem_locator_submit_supplement' })
    $prepares = @($prepareSelection.successful)
    Assert-Journey ($creates.Count -eq 1) 'phase1 must create exactly one Case'
    Assert-Journey ($submits.Count -eq 1) 'phase1 must submit parameter group A exactly once'
    Assert-Journey ($prepares.Count -eq 1) 'phase1 must prepare exactly one attachment'
    Assert-Journey ($records[-1].tool_name -ceq 'problem_locator_prepare_attachment') 'prepare_attachment must be the final phase1 MCP call'

    $create = $creates[0]
    Assert-Journey ($records[0].tool_name -ceq 'problem_locator_create_case') 'create_case must be the first phase1 MCP call'
    $createInput = $create.input
    Assert-JourneyExactProperties $createInput @('request_id', 'problem_spec', 'initial_user_facts', 'wait_seconds') 'create_case input'
    Assert-Journey ((Get-JourneyStringProperty $createInput 'request_id') -ceq $ids.create) 'create request_id'
    Assert-JourneyProblemSpecInput (Get-JourneyProperty $createInput 'problem_spec' -Required)
    $initialFacts = Get-JourneyProperty $createInput 'initial_user_facts' -Required
    Assert-JourneyJsonArray $initialFacts 'create initial_user_facts'
    Assert-Journey (@($initialFacts).Count -eq 0) 'create initial_user_facts must be empty'
    Assert-Journey ((Get-JourneyIntegerProperty $createInput 'wait_seconds') -eq 0) 'create wait_seconds must be zero'
    $createData = Get-JourneySuccessData $create
    Assert-JourneyApplicationResponseShape $createData 'create_case'
    $createReceipt = Get-JourneyProperty $createData 'business_receipt' -Required
    $caseId = Get-JourneyStringProperty $createReceipt 'case_id'
    Assert-JourneyUuid $caseId 'created case_id'
    $createRevision = Assert-JourneyReceipt $createData 'CreateCase' 'RUNNING' $caseId $caseId $true 'create_case'
    Assert-Journey ($createRevision -eq 1) 'clean create_case receipt revision must be 1'

    $getRecords = @($successfulRecords | Where-Object { $_.tool_name -ceq 'problem_locator_get_case' })
    Assert-Journey ($getRecords.Count -ge 2) 'phase1 requires explicit polls for group A and log_archive'
    foreach ($record in $getRecords) {
        Assert-JourneyGetArguments $record $caseId
    }
    $groupNames = @('caller_service', 'server_service', 'rpc_method', 'problem_time')
    $groupKinds = @('INPUT', 'INPUT', 'INPUT', 'INPUT')
    $groupRecord = @($getRecords | Where-Object { $_.ordinal -gt $create.ordinal -and (Test-JourneyCaseWithOpenNames $_ 'WAITING_INPUT' $groupNames $groupKinds) } | Select-Object -First 1)
    Assert-Journey ($groupRecord.Count -eq 1) 'phase1 never observed the exact parameter group A'
    $groupData = Get-JourneySuccessData $groupRecord[0]
    $groupView = Get-JourneyProperty $groupData 'case_view' -Required
    Assert-JourneyOpenRequirements $groupView $groupNames $groupKinds 'group A'

    $submit = $submits[0]
    Assert-Journey ($submit.ordinal -gt $groupRecord[0].ordinal) 'group A submission must follow its authoritative Case view'
    $submitInput = $submit.input
    Assert-JourneyExactProperties $submitInput @('request_id', 'case_id', 'expected_case_revision', 'inputs', 'attachment_ids', 'wait_seconds') 'group A submit input'
    Assert-Journey ((Get-JourneyStringProperty $submitInput 'request_id') -ceq $ids.submit_a) 'group A request_id'
    Assert-Journey ((Get-JourneyStringProperty $submitInput 'case_id') -ceq $caseId) 'group A case_id'
    Assert-Journey ((Get-JourneyIntegerProperty $submitInput 'expected_case_revision') -eq (Get-JourneyIntegerProperty $groupView 'case_revision')) 'group A uses latest Case revision'
    $inputs = Get-JourneyProperty $submitInput 'inputs' -Required
    Assert-JourneyJsonObject $inputs 'group A inputs'
    $expectedInputs = [ordered]@{
        caller_service = 'checkout-synthetic'
        server_service = 'inventory-synthetic'
        rpc_method = 'ReserveStock'
        problem_time = '2026-07-31T00:00:03.000Z'
    }
    $actualInputNames = @($inputs.PSObject.Properties.Name | Sort-Object)
    $expectedInputNames = @($expectedInputs.Keys | Sort-Object)
    Assert-JourneyExactStrings $actualInputNames $expectedInputNames 'group A input keys'
    foreach ($name in $expectedInputs.Keys) {
        Assert-Journey ((Get-JourneyStringProperty $inputs $name) -ceq $expectedInputs[$name]) "group A input $name"
    }
    $groupAttachmentIds = Get-JourneyProperty $submitInput 'attachment_ids' -Required
    Assert-JourneyStringArray $groupAttachmentIds 'group A attachment_ids'
    Assert-Journey (@($groupAttachmentIds).Count -eq 0) 'group A attachment_ids'
    Assert-Journey ((Get-JourneyIntegerProperty $submitInput 'wait_seconds') -eq 0) 'group A wait_seconds'
    $submitResponse = Get-JourneyRecordApplicationResponse $submit
    $groupSubmitRevision = Assert-JourneyReceipt $submitResponse 'SubmitSupplement' 'RUNNING' $caseId $caseId $true 'group A submit'
    Assert-Journey ($groupSubmitRevision -gt (Get-JourneyIntegerProperty $submitInput 'expected_case_revision')) 'group A write must strictly advance Case revision'

    $logRecord = @($getRecords | Where-Object { $_.ordinal -gt $submit.ordinal -and (Test-JourneyCaseWithOpenNames $_ 'WAITING_ATTACHMENT' @('log_archive') @('ATTACHMENT')) } | Select-Object -First 1)
    Assert-Journey ($logRecord.Count -eq 1) 'phase1 never observed the unique log_archive requirement'
    $logData = Get-JourneySuccessData $logRecord[0]
    $logView = Get-JourneyProperty $logData 'case_view' -Required
    Assert-JourneyOpenRequirements $logView @('log_archive') @('ATTACHMENT') 'log archive'
    Assert-JourneySkillRef $logView
    $userFacts = Get-JourneyProperty $logView 'user_facts' -Required
    Assert-JourneyJsonArray $userFacts 'persisted user_facts'
    $factNames = @($userFacts | ForEach-Object { Get-JourneyStringProperty (Get-JourneyProperty $_ 'provenance' -Required) 'input_name' } | Sort-Object)
    Assert-JourneyExactStrings $factNames $expectedInputNames 'persisted group A fact names'

    $prepare = $prepares[0]
    Assert-Journey ($prepare.ordinal -gt $logRecord[0].ordinal) 'prepare must follow log_archive requirement'
    $prepareInput = $prepare.input
    Assert-JourneyExactProperties $prepareInput @('request_id', 'case_id', 'expected_case_revision', 'name', 'content_type', 'declared_size', 'declared_sha256') 'prepare input'
    Assert-Journey ((Get-JourneyStringProperty $prepareInput 'request_id') -ceq $ids.prepare) 'prepare request_id'
    Assert-Journey ((Get-JourneyStringProperty $prepareInput 'case_id') -ceq $caseId) 'prepare case_id'
    Assert-Journey ((Get-JourneyIntegerProperty $prepareInput 'expected_case_revision') -eq (Get-JourneyIntegerProperty $logView 'case_revision')) 'prepare uses latest Case revision'
    Assert-Journey ((Get-JourneyStringProperty $prepareInput 'name') -ceq $script:JourneyZipName) 'prepare archive name'
    Assert-Journey ((Get-JourneyStringProperty $prepareInput 'content_type') -ceq 'application/zip') 'prepare content_type'
    Assert-Journey ((Get-JourneyIntegerProperty $prepareInput 'declared_size') -eq $script:JourneyZipSize) 'prepare declared_size'
    Assert-Journey ((Get-JourneyStringProperty $prepareInput 'declared_sha256') -ceq $script:JourneyZipSha256) 'prepare declared_sha256'

    $prepareData = Get-JourneySuccessData $prepare
    Assert-JourneyExactProperties $prepareData @('application_response', 'upload') 'prepare data'
    $applicationResponse = Get-JourneyProperty $prepareData 'application_response' -Required
    Assert-JourneyApplicationResponseShape $applicationResponse 'prepare'
    $prepareReceipt = Get-JourneyProperty $applicationResponse 'business_receipt' -Required
    $descriptor = Get-JourneyProperty $prepareData 'upload' -Required
    Assert-JourneyExactProperties $descriptor @('attachment_id', 'method', 'url', 'required_headers', 'max_bytes', 'expires_at') 'UploadDescriptor'
    $attachmentId = Get-JourneyStringProperty $descriptor 'attachment_id'
    Assert-JourneyUuid $attachmentId 'prepared attachment_id'
    $prepareRevision = Assert-JourneyReceipt $applicationResponse 'PrepareAttachment' 'UPLOADING' $attachmentId $caseId $false 'prepare'
    Assert-Journey ($prepareRevision -gt (Get-JourneyIntegerProperty $prepareInput 'expected_case_revision')) 'prepare write must strictly advance Case revision'
    Assert-Journey ((Get-JourneyStringProperty $descriptor 'method') -ceq 'PUT') 'UploadDescriptor method'
    Assert-Journey ((Get-JourneyStringProperty $descriptor 'url') -ceq "$($script:JourneyServiceBaseUrl)/api/v1/attachments/$attachmentId/content") 'UploadDescriptor URL must remain loopback and exact'
    Assert-Journey ((Get-JourneyIntegerProperty $descriptor 'max_bytes') -eq $script:JourneyMaxAttachmentBytes) 'UploadDescriptor max_bytes'
    Assert-Journey (Test-JourneyNull (Get-JourneyProperty $descriptor 'expires_at' -Required)) 'UploadDescriptor expires_at'
    $headers = Get-JourneyProperty $descriptor 'required_headers' -Required
    $expectedHeaderNames = @('Content-Length', 'Content-Type', 'Idempotency-Key', 'X-Content-SHA256')
    Assert-JourneyExactProperties $headers $expectedHeaderNames 'UploadDescriptor required_headers'
    Assert-Journey ((Get-JourneyStringProperty $headers 'Idempotency-Key') -ceq $attachmentId) 'Idempotency-Key header'
    Assert-Journey ((Get-JourneyStringProperty $headers 'Content-Type') -ceq 'application/zip') 'Content-Type header'
    Assert-Journey ((Get-JourneyStringProperty $headers 'Content-Length') -ceq [string]$script:JourneyZipSize) 'Content-Length header'
    Assert-Journey ((Get-JourneyStringProperty $headers 'X-Content-SHA256') -ceq $script:JourneyZipSha256) 'X-Content-SHA256 header'
    [void](Assert-JourneyCaseIdentityAndRevisionOrder $successfulRecords $caseId 0)

    return [PSCustomObject][ordered]@{
        schema_version = 1
        attempt = Get-JourneyAttemptLabel $EvidenceRoot
        case_id = $caseId
        attachment_id = $attachmentId
        prepared_case_revision = $prepareRevision
        selected_skill_ref = Get-JourneyProperty $logView 'selected_skill_ref' -Required
        upload_descriptor = $descriptor
        request_ids = $ids
        phase1_mcp_call_count = $records.Count
        validation_corrections = @($validationCorrections)
    }
}

function Invoke-JourneyUpload {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $phase1State = Read-JourneyPhase1StateValidated $EvidenceRoot
    $zipPath = Join-Path $EvidenceRoot $script:JourneyZipName
    Assert-Journey (Test-Path -LiteralPath $zipPath -PathType Leaf) 'real ZIP is absent from the attempt evidence directory'
    $zip = Get-Item -LiteralPath $zipPath
    Assert-Journey ($zip.Length -eq $script:JourneyZipSize) 'real ZIP byte count'
    $digest = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-Journey ($digest -ceq $script:JourneyZipSha256) 'real ZIP SHA-256'

    $caseId = Get-JourneyStringProperty $phase1State 'case_id'
    $attachmentId = Get-JourneyStringProperty $phase1State 'attachment_id'
    Assert-JourneyUuid $caseId 'upload case_id'
    Assert-JourneyUuid $attachmentId 'upload attachment_id'
    $descriptor = Get-JourneyProperty $phase1State 'upload_descriptor' -Required
    Assert-JourneyExactProperties $descriptor @('attachment_id', 'method', 'url', 'required_headers', 'max_bytes', 'expires_at') 'upload descriptor'
    Assert-Journey ((Get-JourneyStringProperty $descriptor 'attachment_id') -ceq $attachmentId) 'upload descriptor attachment_id'
    Assert-Journey ((Get-JourneyStringProperty $descriptor 'method') -ceq 'PUT') 'upload descriptor method'
    Assert-Journey ((Get-JourneyIntegerProperty $descriptor 'max_bytes') -eq $script:JourneyMaxAttachmentBytes) 'upload descriptor max_bytes'
    Assert-Journey ($null -eq (Get-JourneyProperty $descriptor 'expires_at' -Required)) 'upload descriptor expires_at'
    $url = Get-JourneyStringProperty $descriptor 'url'
    Assert-Journey ($url -ceq "$($script:JourneyServiceBaseUrl)/api/v1/attachments/$attachmentId/content") 'upload URL'
    $headers = Get-JourneyProperty $descriptor 'required_headers' -Required
    Assert-JourneyExactProperties $headers @('Content-Length', 'Content-Type', 'Idempotency-Key', 'X-Content-SHA256') 'upload descriptor headers'
    $expectedHeaderValues = [ordered]@{
        'Idempotency-Key' = $attachmentId
        'Content-Type' = 'application/zip'
        'Content-Length' = [string]$script:JourneyZipSize
        'X-Content-SHA256' = $script:JourneyZipSha256
    }
    foreach ($name in $expectedHeaderValues.Keys) {
        $value = Get-JourneyStringProperty $headers $name
        Assert-Journey ($value -ceq $expectedHeaderValues[$name]) "upload header $name"
        Assert-Journey ($value -notmatch '[\r\n]') "upload header $name contains line breaks"
    }

    $curl = 'C:\Windows\System32\curl.exe'
    $stdoutPath = Join-Path $EvidenceRoot 'upload.curl.stdout.txt'
    $stderrPath = Join-Path $EvidenceRoot 'upload.curl.stderr.txt'
    $responsePath = Join-Path $EvidenceRoot 'upload.response.json'
    $responseHeadersPath = Join-Path $EvidenceRoot 'upload.response.headers.txt'
    $uploadStatePath = Join-Path $EvidenceRoot 'upload-state.json'
    Assert-JourneyReservedUnused $responsePath
    Assert-JourneyReservedUnused $responseHeadersPath
    Assert-JourneyReservedUnused $uploadStatePath
    $curlArguments = @(
        '--silent',
        '--show-error',
        '--fail-with-body',
        '--globoff',
        '--max-filesize', [string]$script:JourneyMaxCurlJsonBytes,
        '--connect-timeout', [string]$script:JourneyCurlConnectTimeoutSeconds,
        '--max-time', [string]$script:JourneyCurlMaxTimeSeconds,
        '--request', 'PUT',
        '--header', "Idempotency-Key: $($expectedHeaderValues['Idempotency-Key'])",
        '--header', "Content-Type: $($expectedHeaderValues['Content-Type'])",
        '--header', "Content-Length: $($expectedHeaderValues['Content-Length'])",
        '--header', "X-Content-SHA256: $($expectedHeaderValues['X-Content-SHA256'])",
        '--upload-file', $zipPath,
        '--dump-header', $responseHeadersPath,
        '--output', $responsePath,
        '--write-out', "%{http_code}`n",
        '--', $url
    )
    $exitCode = Invoke-JourneyCapturedProcess -FilePath $curl -Arguments $curlArguments -WorkingDirectory $script:JourneyRepoRoot -StdoutPath $stdoutPath -StderrPath $stderrPath -TimeoutSeconds $script:JourneyCurlProcessTimeoutSeconds
    Complete-JourneyExternalOutput $responsePath
    Complete-JourneyExternalOutput $responseHeadersPath
    Assert-Journey ($exitCode -eq 0) 'curl upload exit code'
    $httpCode = [System.IO.File]::ReadAllText($stdoutPath, $script:JourneyUtf8).Trim()
    Assert-Journey ($httpCode -ceq '200') 'curl upload HTTP status'
    $uploadEnvelope = Read-JourneyJson $responsePath
    Assert-JourneyExactProperties $uploadEnvelope @('ok', 'data', 'error') 'upload response Envelope'
    Assert-Journey (Get-JourneyBooleanProperty $uploadEnvelope 'ok') 'upload response ok'
    Assert-Journey ($null -eq (Get-JourneyProperty $uploadEnvelope 'error' -Required)) 'upload response error must be null'
    $data = Get-JourneyProperty $uploadEnvelope 'data' -Required
    Assert-JourneyExactProperties $data @('attachment_id', 'case_id', 'status', 'case_revision') 'upload response data'
    Assert-Journey ((Get-JourneyStringProperty $data 'case_id') -ceq $caseId) 'upload response case_id'
    Assert-Journey ((Get-JourneyStringProperty $data 'attachment_id') -ceq $attachmentId) 'upload response attachment_id'
    Assert-Journey ((Get-JourneyStringProperty $data 'status') -ceq 'READY') 'upload response status'
    $caseRevision = Get-JourneyIntegerProperty $data 'case_revision'
    Assert-Journey ($caseRevision -gt (Get-JourneyIntegerProperty $phase1State 'prepared_case_revision')) 'upload must advance the Case revision'
    $state = [PSCustomObject][ordered]@{
        schema_version = 1
        attempt = Get-JourneyAttemptLabel $EvidenceRoot
        case_id = $caseId
        attachment_id = $attachmentId
        status = 'READY'
        case_revision = $caseRevision
        zip_size = $script:JourneyZipSize
        zip_sha256 = $script:JourneyZipSha256
        upload_url = $url
        explicit_descriptor_headers = @($expectedHeaderValues.Keys)
        http_status = 200
    }
    Write-JourneyJson -Path $uploadStatePath -Value $state
    return $state
}

function Invoke-JourneyPhase3Validation {
    param($Audit, [string]$EvidenceRoot)
    $uploadState = Read-JourneyUploadStateValidated $EvidenceRoot
    $ids = Get-JourneyRequestIds $EvidenceRoot
    $caseId = Get-JourneyStringProperty $uploadState 'case_id'
    $attachmentId = Get-JourneyStringProperty $uploadState 'attachment_id'
    $uploadRevision = Get-JourneyIntegerProperty $uploadState 'case_revision'
    Assert-JourneyUuid $caseId 'phase3 case_id'
    Assert-JourneyUuid $attachmentId 'phase3 attachment_id'
    Assert-Journey ((Get-JourneyStringProperty $uploadState 'status') -ceq 'READY') 'phase3 attachment status'
    $records = @($Audit.mcp_records)
    $getSelection = Resolve-JourneyEmptyGetCorrections -Records $records -MaximumCorrections 3 -PhaseLabel 'phase3'
    $recoverableOrdinals = @($getSelection.failed_ordinals)
    $successfulRecords = @()
    foreach ($record in $records) {
        Assert-Journey (@('problem_locator_submit_supplement', 'problem_locator_get_case', 'problem_locator_list_artifacts') -ccontains $record.tool_name) "phase3 unexpected business tool $($record.tool_name)"
        if ($recoverableOrdinals -contains [int]$record.ordinal) { continue }
        [void](Get-JourneySuccessData $record)
        $successfulRecords += $record
    }
    $submits = @($successfulRecords | Where-Object { $_.tool_name -ceq 'problem_locator_submit_supplement' })
    $gets = @($successfulRecords | Where-Object { $_.tool_name -ceq 'problem_locator_get_case' })
    $lists = @($successfulRecords | Where-Object { $_.tool_name -ceq 'problem_locator_list_artifacts' })
    Assert-Journey ($submits.Count -eq 2) 'phase3 requires exactly two supplements'
    Assert-Journey ($gets.Count -ge 3) 'phase3 requires explicit order, REVIEWING, and RESOLVED polls'
    Assert-Journey ($lists.Count -eq 1) 'phase3 requires exactly one public artifact list'
    Assert-Journey ($records[0].tool_name -ceq 'problem_locator_submit_supplement') 'READY attachment submission must be phase3 first MCP call'
    Assert-Journey ($records[-1].tool_name -ceq 'problem_locator_list_artifacts') 'artifact list must be the final phase3 MCP call'

    $attachmentSubmit = $submits[0]
    $attachInput = $attachmentSubmit.input
    Assert-JourneyExactProperties $attachInput @('request_id', 'case_id', 'expected_case_revision', 'inputs', 'attachment_ids', 'wait_seconds') 'attachment submit input'
    Assert-Journey ((Get-JourneyStringProperty $attachInput 'request_id') -ceq $ids.submit_attachment) 'attachment submit request_id'
    Assert-Journey ((Get-JourneyStringProperty $attachInput 'case_id') -ceq $caseId) 'attachment submit case_id'
    Assert-Journey ((Get-JourneyIntegerProperty $attachInput 'expected_case_revision') -eq $uploadRevision) 'attachment submit upload revision'
    $attachmentInputs = Get-JourneyProperty $attachInput 'inputs' -Required
    Assert-JourneyJsonObject $attachmentInputs 'attachment submit inputs'
    $attachmentInputProperties = @($attachmentInputs.PSObject.Properties)
    Assert-Journey ($attachmentInputProperties.Count -eq 0) 'attachment submit inputs must be empty'
    $submittedAttachmentIds = Get-JourneyProperty $attachInput 'attachment_ids' -Required
    Assert-JourneyStringArray $submittedAttachmentIds 'attachment submit IDs'
    Assert-JourneyExactStrings $submittedAttachmentIds @($attachmentId) 'attachment submit IDs'
    Assert-Journey ((Get-JourneyIntegerProperty $attachInput 'wait_seconds') -eq 0) 'attachment submit wait_seconds'
    $attachmentResponse = Get-JourneyRecordApplicationResponse $attachmentSubmit
    $attachmentSubmitRevision = Assert-JourneyReceipt $attachmentResponse 'SubmitSupplement' 'RUNNING' $caseId $caseId $true 'attachment submit'
    Assert-Journey ($attachmentSubmitRevision -gt (Get-JourneyIntegerProperty $attachInput 'expected_case_revision')) 'attachment submit must strictly advance Case revision'

    foreach ($record in $gets) {
        Assert-JourneyGetArguments $record $caseId
    }
    $orderRecord = @($gets | Where-Object { $_.ordinal -gt $attachmentSubmit.ordinal -and (Test-JourneyCaseWithOpenNames $_ 'WAITING_INPUT' @('order_id') @('INPUT')) } | Select-Object -First 1)
    Assert-Journey ($orderRecord.Count -eq 1) 'phase3 never observed the unique order_id requirement'
    $orderView = Get-JourneyProperty (Get-JourneySuccessData $orderRecord[0]) 'case_view' -Required
    Assert-JourneySkillRef $orderView

    $orderSubmit = $submits[1]
    Assert-Journey ($orderSubmit.ordinal -gt $orderRecord[0].ordinal) 'order submission must follow authoritative requirement'
    $orderInput = $orderSubmit.input
    Assert-JourneyExactProperties $orderInput @('request_id', 'case_id', 'expected_case_revision', 'inputs', 'attachment_ids', 'wait_seconds') 'order submit input'
    Assert-Journey ((Get-JourneyStringProperty $orderInput 'request_id') -ceq $ids.submit_order) 'order submit request_id'
    Assert-Journey ((Get-JourneyStringProperty $orderInput 'case_id') -ceq $caseId) 'order submit case_id'
    Assert-Journey ((Get-JourneyIntegerProperty $orderInput 'expected_case_revision') -eq (Get-JourneyIntegerProperty $orderView 'case_revision')) 'order submit latest revision'
    $orderInputs = Get-JourneyProperty $orderInput 'inputs' -Required
    Assert-JourneyExactProperties $orderInputs @('order_id') 'order inputs'
    Assert-Journey ((Get-JourneyStringProperty $orderInputs 'order_id') -ceq 'synthetic-order-0001') 'order_id value'
    $orderAttachmentIds = Get-JourneyProperty $orderInput 'attachment_ids' -Required
    Assert-JourneyStringArray $orderAttachmentIds 'order submit attachment_ids'
    Assert-Journey (@($orderAttachmentIds).Count -eq 0) 'order submit attachment_ids'
    Assert-Journey ((Get-JourneyIntegerProperty $orderInput 'wait_seconds') -eq 0) 'order submit wait_seconds'
    $orderResponse = Get-JourneyRecordApplicationResponse $orderSubmit
    $orderSubmitRevision = Assert-JourneyReceipt $orderResponse 'SubmitSupplement' 'RUNNING' $caseId $caseId $true 'order submit'
    Assert-Journey ($orderSubmitRevision -gt (Get-JourneyIntegerProperty $orderInput 'expected_case_revision')) 'order submit must strictly advance Case revision'

    $postOrderGets = @($gets | Where-Object { $_.ordinal -gt $orderSubmit.ordinal })
    $statusViews = @()
    foreach ($record in $postOrderGets) {
        $view = Get-JourneyProperty (Get-JourneySuccessData $record) 'case_view' -Required
        $statusViews += [PSCustomObject]@{ ordinal = $record.ordinal; status = Get-JourneyStringProperty $view 'status'; view = $view }
    }
    $reviewing = @($statusViews | Where-Object { $_.status -ceq 'REVIEWING' })
    $resolved = @($statusViews | Where-Object { $_.status -ceq 'RESOLVED' })
    Assert-Journey ($reviewing.Count -ge 1) 'phase3 must observe REVIEWING through an authoritative get_case result'
    Assert-Journey ($resolved.Count -ge 1) 'phase3 must observe RESOLVED through an authoritative get_case result'
    $resolvedState = $resolved[-1]
    Assert-Journey ($reviewing[0].ordinal -lt $resolvedState.ordinal) 'REVIEWING must be observed before RESOLVED'
    $resolvedView = $resolvedState.view
    Assert-JourneySkillRef $resolvedView
    Assert-Journey ($null -eq (Get-JourneyProperty $resolvedView 'failure' -Required)) 'resolved Case failure must be null'
    $finalResult = Get-JourneyProperty $resolvedView 'final_result' -Required
    Assert-JourneyJsonObject $finalResult 'final_result'
    Assert-Journey ((Get-JourneyStringProperty $finalResult 'status') -ceq 'ACCEPTED') 'final result status'
    $supportingValue = Get-JourneyProperty $finalResult 'supporting_evidence_refs' -Required
    Assert-JourneyStringArray $supportingValue 'final supporting Evidence refs'
    $supporting = @($supportingValue)
    Assert-Journey ($supporting.Count -gt 0) 'final result supporting Evidence'
    foreach ($evidenceId in $supporting) {
        Assert-JourneyUuid $evidenceId 'final supporting Evidence ID'
    }
    $mappingsValue = Get-JourneyProperty $finalResult 'completion_criteria_mapping' -Required
    Assert-JourneyJsonArray $mappingsValue 'completion_criteria_mapping'
    $mappings = @($mappingsValue)
    Assert-Journey ($mappings.Count -eq 1) 'final completion criterion count'
    foreach ($mapping in $mappings) {
        Assert-Journey (Get-JourneyBooleanProperty $mapping 'satisfied') 'completion criterion satisfied'
        $mappingEvidence = Get-JourneyProperty $mapping 'evidence_refs' -Required
        Assert-JourneyStringArray $mappingEvidence 'completion criterion Evidence mappings'
        Assert-Journey (@($mappingEvidence).Count -gt 0) 'completion criterion Evidence mappings'
    }

    $list = $lists[0]
    Assert-Journey ($list.ordinal -gt $resolvedState.ordinal) 'artifact list must follow RESOLVED observation'
    Assert-JourneyExactProperties $list.input @('case_id') 'artifact list input'
    Assert-Journey ((Get-JourneyStringProperty $list.input 'case_id') -ceq $caseId) 'artifact list case_id'
    $listData = Get-JourneySuccessData $list
    Assert-JourneyExactProperties $listData @('artifacts') 'artifact list data'
    $artifactsValue = Get-JourneyProperty $listData 'artifacts' -Required
    Assert-JourneyJsonArray $artifactsValue 'public artifact list'
    $artifacts = @($artifactsValue)
    Assert-Journey ($artifacts.Count -eq 2) 'public artifact list must contain exactly two artifacts'
    $resultArtifacts = @($artifacts | Where-Object { (Get-JourneyStringProperty $_ 'name') -ceq 'diagnosis-result.json' })
    $archiveArtifacts = @($artifacts | Where-Object { (Get-JourneyStringProperty $_ 'name') -ceq 'result.zip' })
    Assert-Journey ($resultArtifacts.Count -eq 1) 'public artifact list must contain exactly one diagnosis result'
    Assert-Journey ($archiveArtifacts.Count -eq 1) 'public artifact list must contain exactly one result archive'
    $artifact = $resultArtifacts[0]
    $archive = $archiveArtifacts[0]
    Assert-JourneyExactProperties $artifact @('artifact_id', 'name', 'content_type', 'size', 'sha256', 'created_at', 'download_url') 'USER_RESULT ArtifactView'
    Assert-JourneyExactProperties $archive @('artifact_id', 'name', 'content_type', 'size', 'sha256', 'created_at', 'download_url') 'USER_RESULT_ARCHIVE ArtifactView'
    $artifactId = Get-JourneyStringProperty $artifact 'artifact_id'
    Assert-JourneyUuid $artifactId 'USER_RESULT artifact_id'
    Assert-Journey ((Get-JourneyStringProperty $artifact 'name') -ceq 'diagnosis-result.json') 'USER_RESULT name'
    Assert-Journey ((Get-JourneyStringProperty $artifact 'content_type') -ceq 'application/json') 'USER_RESULT content_type'
    Assert-Journey ((Get-JourneyIntegerProperty $artifact 'size') -gt 0) 'USER_RESULT size'
    $artifactSha = Get-JourneyStringProperty $artifact 'sha256'
    Assert-Journey ($artifactSha -cmatch $script:JourneySha256Pattern) 'USER_RESULT SHA-256'
    $expectedUrl = "$($script:JourneyServiceBaseUrl)/api/v1/artifacts/$artifactId/content?case_id=$caseId"
    Assert-Journey ((Get-JourneyStringProperty $artifact 'download_url') -ceq $expectedUrl) 'USER_RESULT download URL'
    $archiveId = Get-JourneyStringProperty $archive 'artifact_id'
    Assert-JourneyUuid $archiveId 'USER_RESULT_ARCHIVE artifact_id'
    Assert-Journey ($archiveId -cne $artifactId) 'public artifact IDs must be distinct'
    Assert-Journey ((Get-JourneyStringProperty $archive 'content_type') -ceq 'application/zip') 'USER_RESULT_ARCHIVE content_type'
    Assert-Journey ((Get-JourneyIntegerProperty $archive 'size') -gt 0) 'USER_RESULT_ARCHIVE size'
    $archiveSha = Get-JourneyStringProperty $archive 'sha256'
    Assert-Journey ($archiveSha -cmatch $script:JourneySha256Pattern) 'USER_RESULT_ARCHIVE SHA-256'
    $expectedArchiveUrl = "$($script:JourneyServiceBaseUrl)/api/v1/artifacts/$archiveId/content?case_id=$caseId"
    Assert-Journey ((Get-JourneyStringProperty $archive 'download_url') -ceq $expectedArchiveUrl) 'USER_RESULT_ARCHIVE download URL'

    $caseArtifactsValue = Get-JourneyProperty $resolvedView 'artifacts' -Required
    Assert-JourneyJsonArray $caseArtifactsValue 'resolved Case artifacts'
    $caseArtifacts = @($caseArtifactsValue)
    Assert-Journey ($caseArtifacts.Count -eq 2) 'resolved Case must expose exactly two downloadable ArtifactSummaries'
    $resultSummaries = @($caseArtifacts | Where-Object { (Get-JourneyStringProperty $_ 'kind') -ceq 'USER_RESULT' })
    $archiveSummaries = @($caseArtifacts | Where-Object { (Get-JourneyStringProperty $_ 'kind') -ceq 'USER_RESULT_ARCHIVE' })
    Assert-Journey ($resultSummaries.Count -eq 1) 'resolved Case USER_RESULT summary count'
    Assert-Journey ($archiveSummaries.Count -eq 1) 'resolved Case USER_RESULT_ARCHIVE summary count'
    $summary = $resultSummaries[0]
    $archiveSummary = $archiveSummaries[0]
    Assert-JourneyExactProperties $summary @('artifact_id', 'kind', 'name', 'content_type', 'resource_kind', 'size', 'sha256', 'created_by_job_id', 'created_at', 'downloadable') 'resolved ArtifactSummary'
    Assert-Journey ((Get-JourneyStringProperty $summary 'artifact_id') -ceq $artifactId) 'ArtifactSummary/View artifact_id'
    Assert-Journey ((Get-JourneyStringProperty $summary 'kind') -ceq 'USER_RESULT') 'ArtifactSummary kind'
    Assert-Journey ((Get-JourneyStringProperty $summary 'resource_kind') -ceq 'FILE') 'ArtifactSummary resource_kind'
    Assert-Journey (Get-JourneyBooleanProperty $summary 'downloadable') 'ArtifactSummary downloadable'
    Assert-Journey ((Get-JourneyStringProperty $summary 'name') -ceq (Get-JourneyStringProperty $artifact 'name')) 'ArtifactSummary/View name'
    Assert-Journey ((Get-JourneyStringProperty $summary 'content_type') -ceq (Get-JourneyStringProperty $artifact 'content_type')) 'ArtifactSummary/View content_type'
    Assert-Journey ((Get-JourneyIntegerProperty $summary 'size') -eq (Get-JourneyIntegerProperty $artifact 'size')) 'ArtifactSummary/View size'
    Assert-Journey ((Get-JourneyStringProperty $summary 'sha256') -ceq $artifactSha) 'ArtifactSummary/View sha256'
    Assert-Journey ((Get-JourneyStringProperty $summary 'created_at') -ceq (Get-JourneyStringProperty $artifact 'created_at')) 'ArtifactSummary/View created_at'
    Assert-JourneyUuid (Get-JourneyStringProperty $summary 'created_by_job_id') 'ArtifactSummary created_by_job_id'
    Assert-JourneyExactProperties $archiveSummary @('artifact_id', 'kind', 'name', 'content_type', 'resource_kind', 'size', 'sha256', 'created_by_job_id', 'created_at', 'downloadable') 'resolved archive ArtifactSummary'
    Assert-Journey ((Get-JourneyStringProperty $archiveSummary 'artifact_id') -ceq $archiveId) 'archive ArtifactSummary/View artifact_id'
    Assert-Journey ((Get-JourneyStringProperty $archiveSummary 'kind') -ceq 'USER_RESULT_ARCHIVE') 'archive ArtifactSummary kind'
    Assert-Journey ((Get-JourneyStringProperty $archiveSummary 'resource_kind') -ceq 'FILE') 'archive ArtifactSummary resource_kind'
    Assert-Journey (Get-JourneyBooleanProperty $archiveSummary 'downloadable') 'archive ArtifactSummary downloadable'
    Assert-Journey ((Get-JourneyStringProperty $archiveSummary 'name') -ceq (Get-JourneyStringProperty $archive 'name')) 'archive ArtifactSummary/View name'
    Assert-Journey ((Get-JourneyStringProperty $archiveSummary 'content_type') -ceq (Get-JourneyStringProperty $archive 'content_type')) 'archive ArtifactSummary/View content_type'
    Assert-Journey ((Get-JourneyIntegerProperty $archiveSummary 'size') -eq (Get-JourneyIntegerProperty $archive 'size')) 'archive ArtifactSummary/View size'
    Assert-Journey ((Get-JourneyStringProperty $archiveSummary 'sha256') -ceq $archiveSha) 'archive ArtifactSummary/View sha256'
    Assert-Journey ((Get-JourneyStringProperty $archiveSummary 'created_at') -ceq (Get-JourneyStringProperty $archive 'created_at')) 'archive ArtifactSummary/View created_at'
    Assert-JourneyUuid (Get-JourneyStringProperty $archiveSummary 'created_by_job_id') 'archive ArtifactSummary created_by_job_id'
    [void](Assert-JourneyCaseIdentityAndRevisionOrder $successfulRecords $caseId $uploadRevision)

    return [PSCustomObject][ordered]@{
        schema_version = 1
        attempt = Get-JourneyAttemptLabel $EvidenceRoot
        case_id = $caseId
        attachment_id = $attachmentId
        resolved_case_revision = Get-JourneyIntegerProperty $resolvedView 'case_revision'
        diagnosis_state_revision = Get-JourneyIntegerProperty $resolvedView 'diagnosis_state_revision'
        selected_skill_ref = Get-JourneyProperty $resolvedView 'selected_skill_ref' -Required
        final_result = $finalResult
        observed_statuses = @($statusViews | ForEach-Object { $_.status })
        public_artifact = $artifact
        public_result_archive = $archive
        request_ids = $ids
        phase3_mcp_call_count = $records.Count
        validation_corrections = @($getSelection.corrections)
    }
}

function Invoke-JourneyClaudePhase {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('phase1', 'phase3')][string]$Phase,
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][string]$Prompt
    )
    Confirm-JourneyClaudeVersion $EvidenceRoot
    $promptPath = Join-Path $EvidenceRoot "$Phase.prompt.txt"
    $stdoutPath = Join-Path $EvidenceRoot "$Phase.stream-json.stdout.ndjson"
    $stderrPath = Join-Path $EvidenceRoot "$Phase.stderr.txt"
    $hookPath = Join-Path $EvidenceRoot "$Phase.client-dfx.jsonl"
    $auditPath = Join-Path $EvidenceRoot "$Phase.authoritative.json"
    Assert-JourneyReservedUnused $promptPath
    Assert-JourneyReservedUnused $stdoutPath
    Assert-JourneyReservedUnused $stderrPath
    Assert-JourneyReservedUnused $hookPath
    Assert-JourneyReservedUnused $auditPath
    Write-JourneyUtf8 -Path $promptPath -Text ($Prompt + "`n")
    $arguments = Get-JourneyClaudeArguments -Prompt $Prompt -Phase $Phase
    $timeoutSeconds = if ($Phase -ceq 'phase1') {
        $script:JourneyClaudePhase1TimeoutSeconds
    }
    else {
        $script:JourneyClaudePhase3TimeoutSeconds
    }
    $hadLogPath = Test-Path Env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE
    $previousLogPath = $env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE
    $hadNoProxy = Test-Path Env:NO_PROXY
    $previousNoProxy = $env:NO_PROXY
    $hadLowerNoProxy = Test-Path Env:no_proxy
    $previousLowerNoProxy = $env:no_proxy
    $hadHttpProxy = Test-Path Env:HTTP_PROXY
    $previousHttpProxy = $env:HTTP_PROXY
    $hadHttpsProxy = Test-Path Env:HTTPS_PROXY
    $previousHttpsProxy = $env:HTTPS_PROXY
    $hadLowerHttpProxy = Test-Path Env:http_proxy
    $previousLowerHttpProxy = $env:http_proxy
    $hadLowerHttpsProxy = Test-Path Env:https_proxy
    $previousLowerHttpsProxy = $env:https_proxy
    try {
        $env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE = $hookPath
        $noProxyParts = @(Get-JourneyNoProxyParts -PreviousNoProxy $previousNoProxy -PreviousLowerNoProxy $previousLowerNoProxy)
        $env:NO_PROXY = $noProxyParts -join ','
        $env:no_proxy = $env:NO_PROXY
        Remove-Item Env:HTTP_PROXY -ErrorAction SilentlyContinue
        Remove-Item Env:http_proxy -ErrorAction SilentlyContinue
        Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue
        Remove-Item Env:https_proxy -ErrorAction SilentlyContinue
        $exitCode = Invoke-JourneyCapturedProcess -FilePath $script:JourneyClaudeExe -Arguments $arguments -WorkingDirectory $script:JourneyRepoRoot -StdoutPath $stdoutPath -StderrPath $stderrPath -TimeoutSeconds $timeoutSeconds
    }
    finally {
        if ($hadLogPath) { $env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE = $previousLogPath } else { Remove-Item Env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE -ErrorAction SilentlyContinue }
        if ($hadNoProxy) { $env:NO_PROXY = $previousNoProxy } else { Remove-Item Env:NO_PROXY -ErrorAction SilentlyContinue }
        if ($hadLowerNoProxy) { $env:no_proxy = $previousLowerNoProxy } else { Remove-Item Env:no_proxy -ErrorAction SilentlyContinue }
        if ($hadHttpProxy) { $env:HTTP_PROXY = $previousHttpProxy } else { Remove-Item Env:HTTP_PROXY -ErrorAction SilentlyContinue }
        if ($hadHttpsProxy) { $env:HTTPS_PROXY = $previousHttpsProxy } else { Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue }
        if ($hadLowerHttpProxy) { $env:http_proxy = $previousLowerHttpProxy } else { Remove-Item Env:http_proxy -ErrorAction SilentlyContinue }
        if ($hadLowerHttpsProxy) { $env:https_proxy = $previousLowerHttpsProxy } else { Remove-Item Env:https_proxy -ErrorAction SilentlyContinue }
    }
    Assert-Journey ($exitCode -eq 0) "$Phase Claude exit code"
    $audit = Read-JourneyClaudeAudit $stdoutPath
    Assert-JourneyHookEvidence -Path $hookPath -Audit $audit
    Complete-JourneyExternalOutput $hookPath
    Write-JourneyJson -Path $auditPath -Value $audit
    return $audit
}

function Invoke-JourneyHookFailureProbe {
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][string]$CaseId
    )
    Confirm-JourneyClaudeVersion $EvidenceRoot
    $promptPath = Join-Path $EvidenceRoot 'hook-failure.prompt.txt'
    $stdoutPath = Join-Path $EvidenceRoot 'hook-failure.stream-json.stdout.ndjson'
    $stderrPath = Join-Path $EvidenceRoot 'hook-failure.stderr.txt'
    $debugPath = Join-Path $EvidenceRoot 'hook-failure.claude-debug.log'
    $auditPath = Join-Path $EvidenceRoot 'hook-failure.authoritative.json'
    Assert-JourneyReservedUnused $promptPath
    Assert-JourneyReservedUnused $stdoutPath
    Assert-JourneyReservedUnused $stderrPath
    Assert-JourneyReservedUnused $debugPath
    Assert-JourneyReservedUnused $auditPath
    $prompt = @"
Perform the fail-open logging probe for Problem Locator. Use only the Skill tool and the seven problem_locator Remote MCP tools.

0. Your first action MUST call the Skill tool with skill=problem-locator-client (exact input {"skill":"problem-locator-client"}).
1. Call problem_locator_get_case exactly once with case_id "$CaseId", wait_for_job_id null, and wait_seconds 0.
2. Stop immediately after its successful structured tool_result. Do not call any other business tool and do not modify the Case.
"@
    Write-JourneyUtf8 -Path $promptPath -Text ($prompt + "`n")
    $arguments = Get-JourneyClaudeArguments -Prompt $prompt -Phase phase3 -DebugFile $debugPath
    $hadLogPath = Test-Path Env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE
    $previousLogPath = $env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE
    $hadNoProxy = Test-Path Env:NO_PROXY
    $previousNoProxy = $env:NO_PROXY
    $hadLowerNoProxy = Test-Path Env:no_proxy
    $previousLowerNoProxy = $env:no_proxy
    $hadHttpProxy = Test-Path Env:HTTP_PROXY
    $previousHttpProxy = $env:HTTP_PROXY
    $hadHttpsProxy = Test-Path Env:HTTPS_PROXY
    $previousHttpsProxy = $env:HTTPS_PROXY
    $hadLowerHttpProxy = Test-Path Env:http_proxy
    $previousLowerHttpProxy = $env:http_proxy
    $hadLowerHttpsProxy = Test-Path Env:https_proxy
    $previousLowerHttpsProxy = $env:https_proxy
    try {
        # A directory is an absolute but unwritable JSONL target. Both matching
        # Hook invocations must fail with exit 1 while Claude continues the MCP call.
        $env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE = $EvidenceRoot
        $noProxyParts = @(Get-JourneyNoProxyParts -PreviousNoProxy $previousNoProxy -PreviousLowerNoProxy $previousLowerNoProxy)
        $env:NO_PROXY = $noProxyParts -join ','
        $env:no_proxy = $env:NO_PROXY
        Remove-Item Env:HTTP_PROXY -ErrorAction SilentlyContinue
        Remove-Item Env:http_proxy -ErrorAction SilentlyContinue
        Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue
        Remove-Item Env:https_proxy -ErrorAction SilentlyContinue
        $exitCode = Invoke-JourneyCapturedProcess -FilePath $script:JourneyClaudeExe -Arguments $arguments -WorkingDirectory $script:JourneyRepoRoot -StdoutPath $stdoutPath -StderrPath $stderrPath -TimeoutSeconds $script:JourneyClaudePhase1TimeoutSeconds
    }
    finally {
        if ($hadLogPath) { $env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE = $previousLogPath } else { Remove-Item Env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE -ErrorAction SilentlyContinue }
        if ($hadNoProxy) { $env:NO_PROXY = $previousNoProxy } else { Remove-Item Env:NO_PROXY -ErrorAction SilentlyContinue }
        if ($hadLowerNoProxy) { $env:no_proxy = $previousLowerNoProxy } else { Remove-Item Env:no_proxy -ErrorAction SilentlyContinue }
        if ($hadHttpProxy) { $env:HTTP_PROXY = $previousHttpProxy } else { Remove-Item Env:HTTP_PROXY -ErrorAction SilentlyContinue }
        if ($hadHttpsProxy) { $env:HTTPS_PROXY = $previousHttpsProxy } else { Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue }
        if ($hadLowerHttpProxy) { $env:http_proxy = $previousLowerHttpProxy } else { Remove-Item Env:http_proxy -ErrorAction SilentlyContinue }
        if ($hadLowerHttpsProxy) { $env:https_proxy = $previousLowerHttpsProxy } else { Remove-Item Env:https_proxy -ErrorAction SilentlyContinue }
    }
    Assert-Journey ($exitCode -eq 0) 'Hook write failure must not block Claude or MCP'
    Protect-JourneySensitiveOutput $debugPath
    Complete-JourneyExternalOutput $debugPath
    $hookFailureText = [System.IO.File]::ReadAllText($debugPath, $script:JourneyUtf8)
    Assert-Journey ($hookFailureText.Contains('problem-locator client DFX logging failed:')) 'real Claude output must expose the expected Hook logging failure'
    $audit = Read-JourneyClaudeAudit $stdoutPath
    $records = @($audit.mcp_records)
    Assert-Journey ($records.Count -eq 1) 'Hook failure probe must make exactly one MCP call'
    Assert-Journey ($records[0].tool_name -ceq 'problem_locator_get_case') 'Hook failure probe tool'
    Assert-Journey ((Get-JourneyStringProperty $records[0].input 'case_id') -ceq $CaseId) 'Hook failure probe case_id'
    [void](Get-JourneySuccessData $records[0])
    Write-JourneyJson -Path $auditPath -Value ([PSCustomObject][ordered]@{
        schema_version = 1
        hook_logging_failure_observed = $true
        mcp_request_completed = $true
        audit = $audit
    })
    return $audit
}
