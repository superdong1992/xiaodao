Set-StrictMode -Version Latest

$script:HcServiceBaseUrl = 'http://127.0.0.1:18000'
$script:HcCurlExe = 'C:\Windows\System32\curl.exe'
$script:HcConnectTimeoutSeconds = 10
$script:HcMaxTimeSeconds = 120
$script:HcInternalMaxBytes = 65536
$script:HcAttemptPattern = '^attempt[0-9]+-[0-9]{8}-[0-9]{6}$'
$script:HcUuidPattern = '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
$script:HcSha256Pattern = '^[0-9a-f]{64}$'
$script:HcUtf8 = New-Object System.Text.UTF8Encoding($false)
$script:HcStrictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
$script:HcReservedOutputs = @{}
$script:HcCompletedOutputs = @{}

function Assert-Hc {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw "HTTP capture assertion failed: $Message"
    }
}

function Test-HcProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    return $null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]
}

function Get-HcProperty {
    param(
        $Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [switch]$Required
    )
    if (-not (Test-HcProperty $Object $Name)) {
        if ($Required) {
            throw "HTTP capture assertion failed: required JSON property '$Name' is absent"
        }
        return $null
    }
    $value = $Object.PSObject.Properties[$Name].Value
    if ($value -is [System.Array]) {
        return ,$value
    }
    return $value
}

function Get-HcStringProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    $value = Get-HcProperty $Object $Name -Required
    Assert-Hc ($value -is [string]) "$Name must be a JSON string"
    return $value
}

function Get-HcBooleanProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    $value = Get-HcProperty $Object $Name -Required
    Assert-Hc ($value -is [bool]) "$Name must be a JSON boolean"
    return $value
}

function Get-HcIntegerProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)
    $value = Get-HcProperty $Object $Name -Required
    $integerTypes = @([byte], [sbyte], [int16], [uint16], [int32], [uint32], [int64], [uint64])
    Assert-Hc ($null -ne $value -and $integerTypes -contains $value.GetType()) "$Name must be a JSON integer"
    return [int64]$value
}

function Assert-HcJsonObject {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Hc ($Value -is [System.Management.Automation.PSCustomObject]) "$Label must be a JSON object"
}

function Assert-HcJsonArray {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Hc ($Value -is [System.Array]) "$Label must be a JSON array"
}

function Assert-HcStringArray {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-HcJsonArray $Value $Label
    foreach ($item in @($Value)) {
        Assert-Hc ($item -is [string]) "$Label entries must be strings"
    }
}

function Assert-HcExactStrings {
    param($Actual, [string[]]$Expected, [Parameter(Mandatory = $true)][string]$Label)
    $actualItems = @($Actual)
    Assert-Hc ($actualItems.Count -eq $Expected.Count) "$Label count"
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        Assert-Hc ([string]$actualItems[$index] -ceq $Expected[$index]) "$Label[$index]"
    }
}

function Assert-HcExactProperties {
    param($Object, [string[]]$Expected, [Parameter(Mandatory = $true)][string]$Label)
    Assert-HcJsonObject $Object $Label
    Assert-HcExactStrings @($Object.PSObject.Properties.Name | Sort-Object) @($Expected | Sort-Object) "$Label properties"
}

function Assert-HcUuid {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Hc ($Value -is [string] -and $Value -cmatch $script:HcUuidPattern) "$Label must be a lowercase UUID"
}

function Assert-HcSha256 {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Hc ($Value -is [string] -and $Value -cmatch $script:HcSha256Pattern) "$Label must be a lowercase SHA-256"
}

function Assert-HcOrdinaryFile {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Hc (Test-Path -LiteralPath $Path -PathType Leaf) "$Label is absent"
    $item = Get-Item -LiteralPath $Path -Force
    Assert-Hc (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) "$Label must not be a reparse point"
    Assert-Hc ($item.Length -le 134217728) "$Label exceeds the 128 MiB local audit bound"
    return $item
}

function Read-HcBytes {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    [void](Assert-HcOrdinaryFile -Path $Path -Label $Label)
    return [System.IO.File]::ReadAllBytes($Path)
}

function Read-HcUtf8Text {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $bytes = Read-HcBytes -Path $Path -Label $Label
    Assert-Hc (-not ($bytes.Length -ge 3 -and $bytes[0] -eq 0xef -and $bytes[1] -eq 0xbb -and $bytes[2] -eq 0xbf)) "$Label must not contain a UTF-8 BOM"
    try {
        return $script:HcStrictUtf8.GetString($bytes)
    }
    catch {
        throw "HTTP capture assertion failed: $Label is not strict UTF-8"
    }
}

function Read-HcJson {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $text = Read-HcUtf8Text -Path $Path -Label $Label
    try {
        return $text | ConvertFrom-Json
    }
    catch {
        throw "HTTP capture assertion failed: $Label is not valid JSON"
    }
}

function Add-HcCanonicalString {
    param(
        [Parameter(Mandatory = $true)][System.Text.StringBuilder]$Builder,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )
    [void]$Builder.Append('"')
    for ($index = 0; $index -lt $Value.Length; $index++) {
        $character = $Value[$index]
        $code = [int]$character
        if ($code -ge 0xd800 -and $code -le 0xdbff) {
            Assert-Hc ($index + 1 -lt $Value.Length) 'canonical JSON contains an unpaired high surrogate'
            $low = [int]$Value[$index + 1]
            Assert-Hc ($low -ge 0xdc00 -and $low -le 0xdfff) 'canonical JSON contains an unpaired high surrogate'
            [void]$Builder.Append($character)
            [void]$Builder.Append($Value[$index + 1])
            $index++
            continue
        }
        Assert-Hc (-not ($code -ge 0xdc00 -and $code -le 0xdfff)) 'canonical JSON contains an unpaired low surrogate'
        if ($code -eq 0x22) { [void]$Builder.Append('\"'); continue }
        if ($code -eq 0x5c) { [void]$Builder.Append('\\'); continue }
        if ($code -eq 0x08) { [void]$Builder.Append('\b'); continue }
        if ($code -eq 0x09) { [void]$Builder.Append('\t'); continue }
        if ($code -eq 0x0a) { [void]$Builder.Append('\n'); continue }
        if ($code -eq 0x0c) { [void]$Builder.Append('\f'); continue }
        if ($code -eq 0x0d) { [void]$Builder.Append('\r'); continue }
        if ($code -lt 0x20) {
            [void]$Builder.Append('\u')
            [void]$Builder.Append($code.ToString('x4', [System.Globalization.CultureInfo]::InvariantCulture))
        }
        else {
            [void]$Builder.Append($character)
        }
    }
    [void]$Builder.Append('"')
}

function Add-HcCanonicalValue {
    param(
        [Parameter(Mandatory = $true)][System.Text.StringBuilder]$Builder,
        [AllowNull()]$Value
    )
    if ($null -eq $Value) {
        [void]$Builder.Append('null')
        return
    }
    if ($Value -is [bool]) {
        if ($Value) { [void]$Builder.Append('true') } else { [void]$Builder.Append('false') }
        return
    }
    if ($Value -is [string]) {
        Add-HcCanonicalString -Builder $Builder -Value $Value
        return
    }
    $integerTypes = @([byte], [sbyte], [int16], [uint16], [int32], [uint32], [int64], [uint64])
    if ($integerTypes -contains $Value.GetType()) {
        [void]$Builder.Append(([System.Convert]::ToString($Value, [System.Globalization.CultureInfo]::InvariantCulture)))
        return
    }
    if ($Value -is [double] -or $Value -is [single]) {
        $number = [double]$Value
        Assert-Hc (-not [double]::IsNaN($number) -and -not [double]::IsInfinity($number)) 'canonical JSON contains a non-finite number'
        $numberText = $number.ToString('R', [System.Globalization.CultureInfo]::InvariantCulture).Replace('E', 'e')
        if ($numberText -notmatch '[.e]') {
            $numberText += '.0'
        }
        [void]$Builder.Append($numberText)
        return
    }
    if ($Value -is [decimal]) {
        $numberText = $Value.ToString([System.Globalization.CultureInfo]::InvariantCulture).Replace('E', 'e')
        Assert-Hc ($numberText -cmatch '^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$') 'canonical JSON contains an invalid decimal number'
        [void]$Builder.Append($numberText)
        return
    }
    if ($Value -is [System.Array]) {
        [void]$Builder.Append('[')
        $items = @($Value)
        for ($index = 0; $index -lt $items.Count; $index++) {
            if ($index -gt 0) { [void]$Builder.Append(',') }
            Add-HcCanonicalValue -Builder $Builder -Value $items[$index]
        }
        [void]$Builder.Append(']')
        return
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        [void]$Builder.Append('{')
        $properties = @($Value.PSObject.Properties)
        [string[]]$names = @($properties | ForEach-Object { [string]$_.Name })
        [System.Array]::Sort($names, [System.StringComparer]::Ordinal)
        for ($index = 0; $index -lt $names.Count; $index++) {
            if ($index -gt 0) { [void]$Builder.Append(',') }
            Add-HcCanonicalString -Builder $Builder -Value $names[$index]
            [void]$Builder.Append(':')
            $property = $null
            foreach ($candidate in $properties) {
                if ([string]$candidate.Name -ceq $names[$index]) {
                    $property = $candidate
                    break
                }
            }
            Assert-Hc ($null -ne $property) "canonical JSON property disappeared: $($names[$index])"
            Add-HcCanonicalValue -Builder $Builder -Value $property.Value
        }
        [void]$Builder.Append('}')
        return
    }
    throw "HTTP capture assertion failed: canonical JSON contains unsupported value type $($Value.GetType().FullName)"
}

function Read-HcCanonicalJson {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $text = Read-HcUtf8Text -Path $Path -Label $Label
    $value = Read-HcJson -Path $Path -Label $Label
    $builder = New-Object System.Text.StringBuilder
    Add-HcCanonicalValue -Builder $builder -Value $value
    [void]$builder.Append("`n")
    Assert-Hc ($builder.ToString() -ceq $text) "$Label must use V1 Canonical JSON bytes"
    return $value
}

function Get-HcCanonicalText {
    param([AllowNull()]$Value)
    $builder = New-Object System.Text.StringBuilder
    Add-HcCanonicalValue -Builder $builder -Value $Value
    return $builder.ToString()
}

function Assert-HcJsonEquivalent {
    param($Actual, $Expected, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Hc ((Get-HcCanonicalText $Actual) -ceq (Get-HcCanonicalText $Expected)) "$Label differs"
}

function Get-HcBeforeOutputNames {
    return @(
        'diagnosis-result.before.json',
        'diagnosis-result.before.headers',
        'diagnosis-result.before.meta.json',
        'diagnosis-result.before.curl.stdout.txt',
        'diagnosis-result.before.curl.stderr.txt',
        'result-archive.before.zip',
        'result-archive.before.headers',
        'result-archive.before.meta.json',
        'result-archive.before.curl.stdout.txt',
        'result-archive.before.curl.stderr.txt'
    )
}

function Get-HcAfterOutputNames {
    return @(
        'diagnosis-result.after.json',
        'diagnosis-result.after.headers',
        'diagnosis-result.after.meta.json',
        'diagnosis-result.after.curl.stdout.txt',
        'diagnosis-result.after.curl.stderr.txt',
        'result-archive.after.zip',
        'result-archive.after.headers',
        'result-archive.after.meta.json',
        'result-archive.after.curl.stdout.txt',
        'result-archive.after.curl.stderr.txt',
        'internal-logparse.after.headers',
        'internal-logparse.after.meta.json',
        'internal-logparse.after.body.json',
        'internal-logparse.after.curl.stdout.txt',
        'internal-logparse.after.curl.stderr.txt'
    )
}

function Get-HcAllOutputNames {
    return @((Get-HcBeforeOutputNames) + (Get-HcAfterOutputNames))
}

function New-HcOutputReservations {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot, [Parameter(Mandatory = $true)][string[]]$Names)
    Assert-Hc (@($Names | Sort-Object -Unique).Count -eq $Names.Count) 'planned HTTP capture outputs must be unique'
    $prefix = [System.IO.Path]::GetFullPath($EvidenceRoot).TrimEnd('\') + '\'
    $paths = @()
    foreach ($name in $Names) {
        Assert-Hc ([System.IO.Path]::GetFileName($name) -ceq $name) "output must be a plain filename: $name"
        $path = [System.IO.Path]::GetFullPath((Join-Path $EvidenceRoot $name))
        Assert-Hc ($path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) "output escaped EvidenceRoot: $name"
        Assert-Hc (-not (Test-Path -LiteralPath $path)) "output already exists: $name"
        $paths += $path
    }
    foreach ($path in $paths) {
        $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $stream.Dispose()
        $key = $path.ToLowerInvariant()
        $script:HcReservedOutputs[$key] = $true
        $script:HcCompletedOutputs[$key] = $false
    }
}

function Assert-HcReservedUnused {
    param([Parameter(Mandatory = $true)][string]$Path)
    $key = [System.IO.Path]::GetFullPath($Path).ToLowerInvariant()
    Assert-Hc ($script:HcReservedOutputs.ContainsKey($key)) "output was not atomically reserved: $Path"
    Assert-Hc (-not [bool]$script:HcCompletedOutputs[$key]) "output reservation already consumed: $Path"
}

function Complete-HcExternalOutput {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-HcReservedUnused $Path
    Assert-Hc (Test-Path -LiteralPath $Path -PathType Leaf) "external output is absent: $Path"
    $item = Get-Item -LiteralPath $Path -Force
    Assert-Hc (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) "external output became a reparse point: $Path"
    $script:HcCompletedOutputs[[System.IO.Path]::GetFullPath($Path).ToLowerInvariant()] = $true
}

function Write-HcUtf8 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    Assert-HcReservedUnused $Path
    $bytes = $script:HcUtf8.GetBytes($Text)
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Truncate, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    $script:HcCompletedOutputs[[System.IO.Path]::GetFullPath($Path).ToLowerInvariant()] = $true
}

function Write-HcJson {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)]$Value)
    Write-HcUtf8 -Path $Path -Text (($Value | ConvertTo-Json -Depth 30) + "`n")
}

function Get-HcAttemptLabel {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $leaf = Split-Path -Leaf ([System.IO.Path]::GetFullPath($EvidenceRoot))
    Assert-Hc ($leaf -cmatch $script:HcAttemptPattern) 'EvidenceRoot must use a clean attempt directory name'
    return ($leaf -split '-', 2)[0]
}

function Confirm-HcEvidenceRoot {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    Assert-Hc (Test-Path -LiteralPath $EvidenceRoot -PathType Container) 'EvidenceRoot is absent'
    $item = Get-Item -LiteralPath $EvidenceRoot -Force
    Assert-Hc (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) 'EvidenceRoot must not be a reparse point'
    [void](Get-HcAttemptLabel $EvidenceRoot)
}

function Confirm-HcManifestFiles {
    param($Files, [string[]]$ExpectedNames, [string]$Root, [string]$Label)
    Assert-HcJsonArray $Files "$Label files"
    $names = @($Files | ForEach-Object { Get-HcStringProperty $_ 'name' } | Sort-Object)
    Assert-HcExactStrings $names @($ExpectedNames | Sort-Object) "$Label filenames"
    foreach ($record in @($Files)) {
        Assert-HcExactProperties $record @('name', 'size', 'sha256') "$Label file record"
        $name = Get-HcStringProperty $record 'name'
        Assert-Hc ([System.IO.Path]::GetFileName($name) -ceq $name) "$Label filename"
        $path = Join-Path $Root $name
        $item = Assert-HcOrdinaryFile -Path $path -Label "$Label source $name"
        Assert-Hc ((Get-HcIntegerProperty $record 'size') -eq $item.Length) "$Label size mismatch: $name"
        $expectedHash = Get-HcStringProperty $record 'sha256'
        Assert-HcSha256 $expectedHash "$Label SHA-256 $name"
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        Assert-Hc ($actualHash -ceq $expectedHash) "$Label SHA-256 mismatch: $name"
    }
}

function Confirm-HcDriverManifest {
    param([Parameter(Mandatory = $true)][string]$DriverRoot)
    $manifest = Read-HcJson -Path (Join-Path $DriverRoot 'windows-http-capture-driver-manifest.json') -Label 'HTTP capture driver manifest'
    Assert-HcExactProperties $manifest @('schema_version', 'static_check', 'network_or_claude_invoked', 'reads_settings_or_environment', 'http_runtime_contacts_loopback_only', 'curl_executable', 'service_base_url', 'phases', 'all_runtime_outputs_create_new', 'source_of_public_artifact', 'source_of_internal_artifact', 'possible_runtime_outputs', 'files') 'HTTP capture driver manifest'
    Assert-Hc ((Get-HcIntegerProperty $manifest 'schema_version') -eq 1) 'HTTP capture manifest schema_version'
    Assert-Hc ((Get-HcStringProperty $manifest 'static_check') -ceq 'passed') 'HTTP capture manifest static_check'
    Assert-Hc (-not (Get-HcBooleanProperty $manifest 'network_or_claude_invoked')) 'HTTP capture static check must be offline'
    Assert-Hc (-not (Get-HcBooleanProperty $manifest 'reads_settings_or_environment')) 'HTTP capture driver must not read settings or environment'
    Assert-Hc (Get-HcBooleanProperty $manifest 'http_runtime_contacts_loopback_only') 'HTTP capture must be loopback-only'
    Assert-Hc ((Get-HcStringProperty $manifest 'curl_executable') -ceq $script:HcCurlExe) 'HTTP capture curl executable'
    Assert-Hc ((Get-HcStringProperty $manifest 'service_base_url') -ceq $script:HcServiceBaseUrl) 'HTTP capture service base URL'
    Assert-Hc (Get-HcBooleanProperty $manifest 'all_runtime_outputs_create_new') 'HTTP capture CreateNew declaration'
    Assert-Hc ((Get-HcStringProperty $manifest 'source_of_public_artifact') -ceq 'validated authoritative journey summaries') 'HTTP capture public source declaration'
    Assert-Hc ((Get-HcStringProperty $manifest 'source_of_internal_artifact') -ceq 'unique LOGPARSE_RUN in the primary CaseAggregate from canonical state-export.before.json') 'HTTP capture internal source declaration'
    $phases = Get-HcProperty $manifest 'phases' -Required
    Assert-HcStringArray $phases 'HTTP capture phases'
    Assert-HcExactStrings $phases @('Before', 'After') 'HTTP capture phases'
    $outputs = Get-HcProperty $manifest 'possible_runtime_outputs' -Required
    Assert-HcStringArray $outputs 'HTTP capture output inventory'
    Assert-HcExactStrings @($outputs | Sort-Object) @(Get-HcAllOutputNames | Sort-Object) 'HTTP capture output inventory'
    Confirm-HcManifestFiles -Files (Get-HcProperty $manifest 'files' -Required) -ExpectedNames @('README-http-capture.md', 'run-windows-http-capture.ps1', 'static-check-http-capture.ps1', 'windows-http-capture-lib.ps1') -Root $DriverRoot -Label 'HTTP capture driver manifest'
}

function Confirm-HcJourneyManifest {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $manifest = Read-HcJson -Path (Join-Path $EvidenceRoot 'windows-journey-driver-manifest.json') -Label 'journey driver manifest'
    Assert-HcExactProperties $manifest @('schema_version', 'static_check', 'network_or_claude_invoked', 'reads_or_copies_secret_settings', 'inline_strict_mcp', 'stdout_stderr_separated', 'authoritative_source', 'user_text_event_regression', 'mixed_or_multiple_tool_result_fail_closed', 'possible_runtime_outputs', 'files') 'journey driver manifest'
    Assert-Hc ((Get-HcIntegerProperty $manifest 'schema_version') -eq 1) 'journey manifest schema_version'
    Assert-Hc ((Get-HcStringProperty $manifest 'static_check') -ceq 'passed') 'journey manifest static_check'
    Assert-Hc (-not (Get-HcBooleanProperty $manifest 'network_or_claude_invoked')) 'journey static check offline declaration'
    Assert-Hc (-not (Get-HcBooleanProperty $manifest 'reads_or_copies_secret_settings')) 'journey secret declaration'
    Assert-Hc (Get-HcBooleanProperty $manifest 'inline_strict_mcp') 'journey strict MCP declaration'
    Assert-Hc (Get-HcBooleanProperty $manifest 'stdout_stderr_separated') 'journey stdout/stderr declaration'
    Assert-Hc ((Get-HcStringProperty $manifest 'authoritative_source') -ceq 'stream-json tool_use/tool_result pairs only') 'journey authoritative source'
    Assert-Hc ((Get-HcStringProperty $manifest 'user_text_event_regression') -ceq 'passed') 'journey user text regression'
    Assert-Hc (Get-HcBooleanProperty $manifest 'mixed_or_multiple_tool_result_fail_closed') 'journey mixed/multiple tool_result fail closed'
    $expectedOutputs = @(
        'windows-claude-version.stdout.txt', 'windows-claude-version.stderr.txt',
        'phase1.prompt.txt', 'phase1.stream-json.stdout.ndjson', 'phase1.stderr.txt', 'phase1.authoritative.json', 'phase1-state.json',
        'upload.curl.stdout.txt', 'upload.curl.stderr.txt', 'upload.response.json', 'upload.response.headers.txt', 'upload-state.json',
        'phase3.prompt.txt', 'phase3.stream-json.stdout.ndjson', 'phase3.stderr.txt', 'phase3.authoritative.json', 'journey-authoritative-summary.json'
    )
    $outputs = Get-HcProperty $manifest 'possible_runtime_outputs' -Required
    Assert-HcStringArray $outputs 'journey manifest outputs'
    Assert-HcExactStrings @($outputs | Sort-Object) @($expectedOutputs | Sort-Object) 'journey manifest outputs'
    Confirm-HcManifestFiles -Files (Get-HcProperty $manifest 'files' -Required) -ExpectedNames @('README.md', 'run-windows-journey.ps1', 'static-check.ps1', 'windows-journey-lib.ps1') -Root $EvidenceRoot -Label 'journey driver manifest'
}

function Assert-HcSelectedSkill {
    param($Skill, [Parameter(Mandatory = $true)][string]$Label)
    Assert-HcExactProperties $Skill @('id', 'version', 'content_hash') $Label
    Assert-Hc ((Get-HcStringProperty $Skill 'id') -ceq 'diagnosis-skill/diagnose-service-takeover') "$Label id"
    Assert-Hc ((Get-HcStringProperty $Skill 'version') -ceq '4.0.0') "$Label version"
    Assert-Hc ((Get-HcStringProperty $Skill 'content_hash') -ceq 'eaa059e98e2fde9b923e0bce3e860422b2944aeabe939b57920793f70337b618') "$Label content hash"
}

function Assert-HcFinalResult {
    param($Result, [Parameter(Mandatory = $true)][string]$Label)
    Assert-HcExactProperties $Result @('conclusion_id', 'revision', 'content_hash', 'statement', 'supporting_evidence_refs', 'completion_criteria_mapping', 'proposed_by_job_id', 'status') $Label
    Assert-HcUuid (Get-HcStringProperty $Result 'conclusion_id') "$Label conclusion_id"
    Assert-Hc ((Get-HcIntegerProperty $Result 'revision') -gt 0) "$Label revision"
    Assert-HcSha256 (Get-HcStringProperty $Result 'content_hash') "$Label content_hash"
    Assert-Hc (-not [string]::IsNullOrWhiteSpace((Get-HcStringProperty $Result 'statement'))) "$Label statement"
    Assert-HcUuid (Get-HcStringProperty $Result 'proposed_by_job_id') "$Label proposed_by_job_id"
    Assert-Hc ((Get-HcStringProperty $Result 'status') -ceq 'ACCEPTED') "$Label accepted status"
    $support = Get-HcProperty $Result 'supporting_evidence_refs' -Required
    Assert-HcStringArray $support "$Label supporting evidence"
    Assert-Hc (@($support).Count -gt 0) "$Label supporting evidence must be nonempty"
    foreach ($id in @($support)) { Assert-HcUuid $id "$Label supporting evidence ID" }
    $mappings = Get-HcProperty $Result 'completion_criteria_mapping' -Required
    Assert-HcJsonArray $mappings "$Label completion mappings"
    Assert-Hc (@($mappings).Count -gt 0) "$Label completion mappings must be nonempty"
    $index = 0
    foreach ($mapping in @($mappings)) {
        Assert-HcExactProperties $mapping @('criterion_index', 'criterion', 'satisfied', 'evidence_refs', 'explanation') "$Label completion mapping"
        Assert-Hc ((Get-HcIntegerProperty $mapping 'criterion_index') -eq $index) "$Label criterion index"
        Assert-Hc (Get-HcBooleanProperty $mapping 'satisfied') "$Label criterion satisfied"
        Assert-Hc (-not [string]::IsNullOrWhiteSpace((Get-HcStringProperty $mapping 'criterion'))) "$Label criterion"
        Assert-Hc (-not [string]::IsNullOrWhiteSpace((Get-HcStringProperty $mapping 'explanation'))) "$Label explanation"
        $refs = Get-HcProperty $mapping 'evidence_refs' -Required
        Assert-HcStringArray $refs "$Label criterion evidence"
        Assert-Hc (@($refs).Count -gt 0) "$Label criterion evidence must be nonempty"
        foreach ($id in @($refs)) { Assert-HcUuid $id "$Label criterion evidence ID" }
        $index++
    }
}

function Assert-HcArtifactView {
    param(
        $Artifact,
        [Parameter(Mandatory = $true)][string]$CaseId,
        [Parameter(Mandatory = $true)][string]$ExpectedName,
        [Parameter(Mandatory = $true)][string]$ExpectedContentType,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-HcExactProperties $Artifact @('artifact_id', 'kind', 'name', 'content_type', 'size', 'sha256', 'created_at', 'download_url') $Label
    $artifactId = Get-HcStringProperty $Artifact 'artifact_id'
    Assert-HcUuid $artifactId "$Label artifact_id"
    $expectedKind = if ($ExpectedName -ceq 'diagnosis-result.json') { 'USER_RESULT' } else { 'USER_RESULT_ARCHIVE' }
    Assert-Hc ((Get-HcStringProperty $Artifact 'kind') -ceq $expectedKind) "$Label kind"
    Assert-Hc ((Get-HcStringProperty $Artifact 'name') -ceq $ExpectedName) "$Label name"
    Assert-Hc ((Get-HcStringProperty $Artifact 'content_type') -ceq $ExpectedContentType) "$Label content type"
    $size = Get-HcIntegerProperty $Artifact 'size'
    Assert-Hc ($size -gt 0 -and $size -le 16777216) "$Label size must be within the 16 MiB public result bound"
    Assert-HcSha256 (Get-HcStringProperty $Artifact 'sha256') "$Label SHA-256"
    Assert-Hc (-not [string]::IsNullOrWhiteSpace((Get-HcStringProperty $Artifact 'created_at'))) "$Label created_at"
    $expected = "$($script:HcServiceBaseUrl)/api/v1/artifacts/$artifactId/content?case_id=$CaseId"
    Assert-Hc ((Get-HcStringProperty $Artifact 'download_url') -ceq $expected) "$Label exact loopback URL"
}

function Read-HcJourneySummary {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $summary = Read-HcJson -Path (Join-Path $EvidenceRoot 'journey-authoritative-summary.json') -Label 'journey authoritative summary'
    Assert-HcExactProperties $summary @('schema_version', 'scenario', 'attempt', 'case_id', 'attachment_id', 'diagnosis_job_id', 'resolved_case_revision', 'diagnosis_state_revision', 'selected_skill_ref', 'final_result', 'observed_statuses', 'public_artifact', 'public_result_archive', 'request_ids', 'phase3_mcp_call_count', 'validation_corrections') 'journey authoritative summary'
    Assert-Hc ((Get-HcIntegerProperty $summary 'schema_version') -eq 1) 'journey summary schema_version'
    Assert-Hc ((Get-HcStringProperty $summary 'scenario') -ceq 'CrossJob') 'primary journey scenario'
    $attempt = Get-HcAttemptLabel $EvidenceRoot
    Assert-Hc ((Get-HcStringProperty $summary 'attempt') -ceq $attempt) 'journey summary attempt'
    $caseId = Get-HcStringProperty $summary 'case_id'
    Assert-HcUuid $caseId 'journey case_id'
    Assert-HcUuid (Get-HcStringProperty $summary 'attachment_id') 'journey attachment_id'
    Assert-HcUuid (Get-HcStringProperty $summary 'diagnosis_job_id') 'journey diagnosis_job_id'
    Assert-Hc ((Get-HcIntegerProperty $summary 'resolved_case_revision') -gt 0) 'journey resolved revision'
    Assert-Hc ((Get-HcIntegerProperty $summary 'diagnosis_state_revision') -gt 0) 'journey diagnosis revision'
    Assert-HcSelectedSkill (Get-HcProperty $summary 'selected_skill_ref' -Required) 'journey selected Skill'
    Assert-HcFinalResult (Get-HcProperty $summary 'final_result' -Required) 'journey final result'
    Assert-HcArtifactView (Get-HcProperty $summary 'public_artifact' -Required) $caseId 'diagnosis-result.json' 'application/json' 'journey public result ArtifactView'
    Assert-HcArtifactView (Get-HcProperty $summary 'public_result_archive' -Required) $caseId 'result.zip' 'application/zip' 'journey public archive ArtifactView'
    $statuses = Get-HcProperty $summary 'observed_statuses' -Required
    Assert-HcStringArray $statuses 'journey observed statuses'
    Assert-Hc (@($statuses).Count -ge 2) 'journey status count'
    Assert-Hc (@($statuses) -ccontains 'REVIEWING') 'journey must observe REVIEWING'
    Assert-Hc (@($statuses)[@($statuses).Count - 1] -ceq 'RESOLVED') 'journey final status'
    $requestIds = Get-HcProperty $summary 'request_ids' -Required
    Assert-HcExactProperties $requestIds @('create', 'submit_a', 'prepare', 'submit_attachment', 'submit_order') 'journey request IDs'
    Assert-Hc ((Get-HcStringProperty $requestIds 'create') -ceq "$attempt-windows-create-v1") 'journey create request ID'
    Assert-Hc ((Get-HcStringProperty $requestIds 'submit_a') -ceq "$attempt-windows-submit-a-v1") 'journey submit A request ID'
    Assert-Hc ((Get-HcStringProperty $requestIds 'prepare') -ceq "$attempt-windows-prepare-log-v1") 'journey prepare request ID'
    Assert-Hc ((Get-HcStringProperty $requestIds 'submit_attachment') -ceq "$attempt-windows-submit-attachment-v1") 'journey attachment request ID'
    Assert-Hc ((Get-HcStringProperty $requestIds 'submit_order') -ceq "$attempt-windows-submit-order-v1") 'journey order request ID'
    Assert-Hc ((Get-HcIntegerProperty $summary 'phase3_mcp_call_count') -gt 0) 'journey phase3 MCP call count'
    return $summary
}

function Confirm-HcRestartManifest {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $root = Join-Path $EvidenceRoot 'restart'
    Assert-Hc (Test-Path -LiteralPath $root -PathType Container) 'restart evidence directory is absent'
    $rootItem = Get-Item -LiteralPath $root -Force
    Assert-Hc (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) 'restart evidence directory must not be a reparse point'
    $manifest = Read-HcJson -Path (Join-Path $root 'windows-restart-driver-manifest.json') -Label 'restart driver manifest'
    Assert-HcExactProperties $manifest @('schema_version', 'static_check', 'network_or_claude_invoked', 'reads_or_copies_secret_settings', 'inline_strict_mcp', 'claude_business_tools', 'first_tool', 'authoritative_source', 'user_text_event_regression', 'mixed_or_multiple_tool_result_fail_closed', 'all_runtime_outputs_create_new', 'possible_runtime_outputs', 'files') 'restart driver manifest'
    Assert-Hc ((Get-HcIntegerProperty $manifest 'schema_version') -eq 1) 'restart manifest schema_version'
    Assert-Hc ((Get-HcStringProperty $manifest 'static_check') -ceq 'passed') 'restart manifest static_check'
    Assert-Hc (-not (Get-HcBooleanProperty $manifest 'network_or_claude_invoked')) 'restart static check offline declaration'
    Assert-Hc (-not (Get-HcBooleanProperty $manifest 'reads_or_copies_secret_settings')) 'restart secret declaration'
    Assert-Hc (Get-HcBooleanProperty $manifest 'inline_strict_mcp') 'restart strict MCP declaration'
    Assert-Hc (Get-HcBooleanProperty $manifest 'all_runtime_outputs_create_new') 'restart CreateNew declaration'
    Assert-Hc ((Get-HcStringProperty $manifest 'first_tool') -ceq 'Skill(problem-locator-client)') 'restart first tool declaration'
    Assert-Hc ((Get-HcStringProperty $manifest 'authoritative_source') -ceq 'uniquely correlated stream-json tool_use/tool_result structuredContent only') 'restart authoritative source'
    Assert-Hc ((Get-HcStringProperty $manifest 'user_text_event_regression') -ceq 'passed') 'restart user text regression'
    Assert-Hc (Get-HcBooleanProperty $manifest 'mixed_or_multiple_tool_result_fail_closed') 'restart mixed/multiple tool_result fail closed'
    $tools = Get-HcProperty $manifest 'claude_business_tools' -Required
    Assert-HcStringArray $tools 'restart business tools'
    Assert-HcExactStrings $tools @('problem_locator_get_case', 'problem_locator_list_artifacts') 'restart business tools'
    $expectedOutputs = @(
        'windows-restart-claude-version.stdout.txt', 'windows-restart-claude-version.stderr.txt',
        'restart.prompt.txt', 'restart.stream-json.stdout.ndjson', 'restart.stderr.txt', 'restart.authoritative.json', 'restart-authoritative-summary.json',
        'restart-download.curl.stdout.txt', 'restart-download.curl.stderr.txt', 'restart-download.response.headers.txt', 'restart-diagnosis-result.json',
        'restart-archive-download.curl.stdout.txt', 'restart-archive-download.curl.stderr.txt', 'restart-archive-download.response.headers.txt', 'restart-result.zip',
        'restart-download-verification.json'
    )
    $outputs = Get-HcProperty $manifest 'possible_runtime_outputs' -Required
    Assert-HcStringArray $outputs 'restart manifest outputs'
    Assert-HcExactStrings @($outputs | Sort-Object) @($expectedOutputs | Sort-Object) 'restart manifest outputs'
    Confirm-HcManifestFiles -Files (Get-HcProperty $manifest 'files' -Required) -ExpectedNames @('README-restart.md', 'download-windows-restart-artifact.ps1', 'run-windows-restart-verify.ps1', 'static-check-restart.ps1', 'windows-restart-lib.ps1') -Root $root -Label 'restart driver manifest'
}

function Read-HcRestartSummary {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot, $BeforeSummary)
    $summaryPath = Join-Path $EvidenceRoot 'restart-authoritative-summary.json'
    $summary = Read-HcJson -Path $summaryPath -Label 'restart authoritative summary'
    Assert-HcExactProperties $summary @('schema_version', 'attempt', 'pre_restart_summary_sha256', 'case_id', 'attachment_id', 'resolved_case_revision', 'diagnosis_state_revision', 'selected_skill_ref', 'final_result', 'public_artifact', 'public_result_archive', 'get_case_wait_timed_out', 'mcp_call_order', 'claude_version', 'model_alias', 'effective_model', 'persistence_unchanged') 'restart authoritative summary'
    Assert-Hc ((Get-HcIntegerProperty $summary 'schema_version') -eq 1) 'restart summary schema_version'
    Assert-Hc ((Get-HcStringProperty $summary 'attempt') -ceq (Get-HcAttemptLabel $EvidenceRoot)) 'restart summary attempt'
    $beforePath = Join-Path $EvidenceRoot 'journey-authoritative-summary.json'
    $beforeHash = (Get-FileHash -LiteralPath $beforePath -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-Hc ((Get-HcStringProperty $summary 'pre_restart_summary_sha256') -ceq $beforeHash) 'restart summary pre-summary SHA-256'
    Assert-Hc ((Get-HcStringProperty $summary 'case_id') -ceq (Get-HcStringProperty $BeforeSummary 'case_id')) 'restart/pre case_id'
    Assert-Hc ((Get-HcStringProperty $summary 'attachment_id') -ceq (Get-HcStringProperty $BeforeSummary 'attachment_id')) 'restart/pre attachment_id'
    Assert-Hc ((Get-HcIntegerProperty $summary 'resolved_case_revision') -eq (Get-HcIntegerProperty $BeforeSummary 'resolved_case_revision')) 'restart/pre Case revision'
    Assert-Hc ((Get-HcIntegerProperty $summary 'diagnosis_state_revision') -eq (Get-HcIntegerProperty $BeforeSummary 'diagnosis_state_revision')) 'restart/pre diagnosis revision'
    Assert-HcSelectedSkill (Get-HcProperty $summary 'selected_skill_ref' -Required) 'restart selected Skill'
    Assert-HcFinalResult (Get-HcProperty $summary 'final_result' -Required) 'restart final result'
    $caseId = Get-HcStringProperty $summary 'case_id'
    Assert-HcArtifactView (Get-HcProperty $summary 'public_artifact' -Required) $caseId 'diagnosis-result.json' 'application/json' 'restart public result ArtifactView'
    Assert-HcArtifactView (Get-HcProperty $summary 'public_result_archive' -Required) $caseId 'result.zip' 'application/zip' 'restart public archive ArtifactView'
    Assert-HcJsonEquivalent (Get-HcProperty $summary 'selected_skill_ref' -Required) (Get-HcProperty $BeforeSummary 'selected_skill_ref' -Required) 'restart/pre selected Skill'
    Assert-HcJsonEquivalent (Get-HcProperty $summary 'final_result' -Required) (Get-HcProperty $BeforeSummary 'final_result' -Required) 'restart/pre final result'
    Assert-HcJsonEquivalent (Get-HcProperty $summary 'public_artifact' -Required) (Get-HcProperty $BeforeSummary 'public_artifact' -Required) 'restart/pre public ArtifactView'
    Assert-HcJsonEquivalent (Get-HcProperty $summary 'public_result_archive' -Required) (Get-HcProperty $BeforeSummary 'public_result_archive' -Required) 'restart/pre public archive ArtifactView'
    Assert-Hc (-not (Get-HcBooleanProperty $summary 'get_case_wait_timed_out')) 'restart wait timeout flag'
    Assert-Hc (Get-HcBooleanProperty $summary 'persistence_unchanged') 'restart persistence flag'
    $order = Get-HcProperty $summary 'mcp_call_order' -Required
    Assert-HcStringArray $order 'restart MCP call order'
    Assert-HcExactStrings $order @('problem_locator_get_case', 'problem_locator_list_artifacts') 'restart MCP call order'
    Assert-Hc ((Get-HcStringProperty $summary 'claude_version') -ceq '2.1.89') 'restart Claude version'
    Assert-Hc ((Get-HcStringProperty $summary 'model_alias') -ceq 'haiku') 'restart model alias'
    Assert-Hc ((Get-HcStringProperty $summary 'effective_model') -ceq 'deepseek-v4-flash[1m]') 'restart effective model'
    return $summary
}

function ConvertTo-HcWindowsArgument {
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

function Invoke-HcCapturedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath
    )
    Assert-Hc (Test-Path -LiteralPath $FilePath -PathType Leaf) "executable is absent: $FilePath"
    Assert-Hc (Test-Path -LiteralPath $WorkingDirectory -PathType Container) 'working directory is absent'
    Assert-HcReservedUnused $StdoutPath
    Assert-HcReservedUnused $StderrPath
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $FilePath
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = $script:HcUtf8
    $start.StandardErrorEncoding = $script:HcUtf8
    $environment = $start.Environment
    if ($null -eq $environment) {
        # Windows PowerShell 5.1 can return null on the first access while it
        # lazily initializes the ProcessStartInfo environment adapter.
        $environment = $start.Environment
    }
    Assert-Hc ($null -ne $environment) 'failed to initialize the child process environment'
    $environment.Clear()
    $environment['SystemRoot'] = 'C:\Windows'
    $environment['WINDIR'] = 'C:\Windows'
    $start.Arguments = (($Arguments | ForEach-Object { ConvertTo-HcWindowsArgument ([string]$_) }) -join ' ')
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    Assert-Hc ($process.Start()) "failed to start $FilePath"
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(($script:HcMaxTimeSeconds + 15) * 1000)) {
        try { $process.Kill() } catch { }
        throw 'HTTP capture assertion failed: curl exceeded the outer process deadline'
    }
    $process.WaitForExit()
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    Write-HcUtf8 -Path $StdoutPath -Text $stdout
    Write-HcUtf8 -Path $StderrPath -Text $stderr
    return $process.ExitCode
}

function Assert-HcExactLoopbackUrl {
    param([Parameter(Mandatory = $true)][string]$Url, [Parameter(Mandatory = $true)][string]$Expected, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Hc ($Url -ceq $Expected) "$Label exact URL"
    try { $uri = New-Object System.Uri($Url, [System.UriKind]::Absolute) } catch { throw "HTTP capture assertion failed: $Label invalid URL" }
    Assert-Hc ($uri.Scheme -ceq 'http') "$Label scheme"
    Assert-Hc ($uri.Host -ceq '127.0.0.1') "$Label host"
    Assert-Hc ($uri.Port -eq 18000) "$Label port"
    Assert-Hc ([string]::IsNullOrEmpty($uri.UserInfo)) "$Label user info"
    Assert-Hc ([string]::IsNullOrEmpty($uri.Fragment)) "$Label fragment"
}

function Invoke-HcCurlGet {
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][int64]$MaxFilesize,
        [Parameter(Mandatory = $true)][string]$BodyPath,
        [Parameter(Mandatory = $true)][string]$HeadersPath,
        [Parameter(Mandatory = $true)][string]$MetaPath,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath
    )
    Assert-Hc ($MaxFilesize -gt 0) 'curl max-filesize must be positive'
    Assert-HcReservedUnused $BodyPath
    Assert-HcReservedUnused $HeadersPath
    Assert-HcReservedUnused $MetaPath
    $arguments = @(
        '--disable',
        '--silent', '--show-error',
        '--request', 'GET',
        '--proto', '=http',
        '--proto-default', 'http',
        '--proxy', '',
        '--noproxy', '*',
        '--max-redirs', '0',
        '--connect-timeout', [string]$script:HcConnectTimeoutSeconds,
        '--max-time', [string]$script:HcMaxTimeSeconds,
        '--max-filesize', [string]$MaxFilesize,
        '--header', 'Accept: application/json',
        '--header', 'Accept-Encoding: identity',
        '--dump-header', $HeadersPath,
        '--output', $BodyPath,
        '--write-out', '%{http_code}|%{num_redirects}|%{size_download}|%{url_effective}',
        $Url
    )
    $exitCode = Invoke-HcCapturedProcess -FilePath $script:HcCurlExe -Arguments $arguments -WorkingDirectory $EvidenceRoot -StdoutPath $StdoutPath -StderrPath $StderrPath
    Complete-HcExternalOutput $HeadersPath
    Complete-HcExternalOutput $BodyPath
    Assert-Hc ($exitCode -eq 0) 'curl GET exit code'
    $writeOut = [System.IO.File]::ReadAllText($StdoutPath, $script:HcUtf8).TrimEnd("`r", "`n")
    $matched = [regex]::Match($writeOut, '^(?<code>[0-9]{3})\|(?<redirects>[0-9]+)\|(?<size>[0-9]+)\|(?<url>http://127\.0\.0\.1:18000/[^\r\n|]+)$', [System.Text.RegularExpressions.RegexOptions]::CultureInvariant)
    Assert-Hc ($matched.Success) 'curl write-out must contain exactly four bounded fields'
    $httpCode = 0
    $redirects = 0
    $size = [int64]0
    Assert-Hc ([int]::TryParse($matched.Groups['code'].Value, [ref]$httpCode)) 'curl HTTP code integer'
    Assert-Hc ([int]::TryParse($matched.Groups['redirects'].Value, [ref]$redirects)) 'curl redirect count integer'
    Assert-Hc ([int64]::TryParse($matched.Groups['size'].Value, [ref]$size)) 'curl downloaded size integer'
    $effectiveUrl = $matched.Groups['url'].Value
    $meta = [PSCustomObject][ordered]@{
        http_code = $httpCode
        num_redirects = $redirects
        size_download = $size
        url_effective = $effectiveUrl
    }
    Write-HcJson -Path $MetaPath -Value $meta
    $writtenMeta = Read-HcJson -Path $MetaPath -Label 'curl metadata'
    Assert-HcExactProperties $writtenMeta @('http_code', 'num_redirects', 'size_download', 'url_effective') 'curl metadata'
    Assert-Hc ((Get-HcIntegerProperty $writtenMeta 'http_code') -eq $httpCode) 'curl metadata HTTP code'
    Assert-Hc ((Get-HcIntegerProperty $writtenMeta 'num_redirects') -eq $redirects) 'curl metadata redirect count'
    Assert-Hc ((Get-HcIntegerProperty $writtenMeta 'size_download') -eq $size) 'curl metadata size'
    Assert-Hc ((Get-HcStringProperty $writtenMeta 'url_effective') -ceq $effectiveUrl) 'curl metadata effective URL'
    return $writtenMeta
}

function Read-HcHeaderCapture {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][int]$ExpectedStatus, [Parameter(Mandatory = $true)][string]$Label)
    $bytes = Read-HcBytes -Path $Path -Label $Label
    $text = [System.Text.Encoding]::GetEncoding(28591).GetString($bytes)
    Assert-Hc (-not ($text.Replace("`r`n", '').Contains("`r"))) "$Label contains a bare CR"
    $normalized = $text.Replace("`r`n", "`n")
    $blocks = @($normalized -split "`n`n" | Where-Object { $_.Length -gt 0 })
    Assert-Hc ($blocks.Count -eq 1) "$Label must contain exactly one HTTP response block"
    $lines = @($blocks[0] -split "`n")
    Assert-Hc ($lines.Count -ge 1) "$Label status line"
    Assert-Hc ($lines[0] -cmatch '^HTTP/(?:1\.[01]|2) ([0-9]{3})(?: .*)?$') "$Label status line format"
    Assert-Hc ([int]$Matches[1] -eq $ExpectedStatus) "$Label HTTP status"
    $headers = @{}
    for ($index = 1; $index -lt $lines.Count; $index++) {
        $line = $lines[$index]
        Assert-Hc (-not [string]::IsNullOrEmpty($line)) "$Label unexpected blank header"
        Assert-Hc (-not [char]::IsWhiteSpace($line[0])) "$Label folded header is forbidden"
        $colon = $line.IndexOf(':')
        Assert-Hc ($colon -gt 0) "$Label malformed header"
        $name = $line.Substring(0, $colon)
        Assert-Hc ($name -cmatch "^[!#$%&'*+.^_``|~0-9A-Za-z-]+$") "$Label invalid header name"
        $lower = $name.ToLowerInvariant()
        Assert-Hc (-not $headers.ContainsKey($lower)) "$Label duplicate header $lower"
        $headers[$lower] = $line.Substring($colon + 1).Trim(' ', "`t")
    }
    Assert-Hc (-not $headers.ContainsKey('location')) "$Label Location header is forbidden"
    return [PSCustomObject]@{ status = $ExpectedStatus; headers = $headers }
}

function Assert-HcPublicCapture {
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][ValidateSet('before', 'after')][string]$Label,
        $Summary,
        [Parameter(Mandatory = $true)][ValidateSet('public_artifact', 'public_result_archive')][string]$ArtifactProperty,
        [Parameter(Mandatory = $true)][string]$Stem,
        [Parameter(Mandatory = $true)][string]$BodyExtension,
        [Parameter(Mandatory = $true)][string]$ExpectedContentType,
        [switch]$CanonicalJson
    )
    $artifact = Get-HcProperty $Summary $ArtifactProperty -Required
    $caseId = Get-HcStringProperty $Summary 'case_id'
    $artifactId = Get-HcStringProperty $artifact 'artifact_id'
    $expectedUrl = "$($script:HcServiceBaseUrl)/api/v1/artifacts/$artifactId/content?case_id=$caseId"
    $expectedSize = Get-HcIntegerProperty $artifact 'size'
    $expectedHash = Get-HcStringProperty $artifact 'sha256'
    $bodyPath = Join-Path $EvidenceRoot "$Stem.$Label.$BodyExtension"
    $headersPath = Join-Path $EvidenceRoot "$Stem.$Label.headers"
    $metaPath = Join-Path $EvidenceRoot "$Stem.$Label.meta.json"
    $headers = Read-HcHeaderCapture -Path $headersPath -ExpectedStatus 200 -Label "$Label public response headers"
    $meta = Read-HcJson -Path $metaPath -Label "$Label public curl metadata"
    Assert-HcExactProperties $meta @('http_code', 'num_redirects', 'size_download', 'url_effective') "$Label public curl metadata"
    Assert-Hc ((Get-HcIntegerProperty $meta 'http_code') -eq 200) "$Label public meta status"
    Assert-Hc ((Get-HcIntegerProperty $meta 'num_redirects') -eq 0) "$Label public redirects"
    Assert-Hc ((Get-HcIntegerProperty $meta 'size_download') -eq $expectedSize) "$Label public downloaded size metadata"
    Assert-HcExactLoopbackUrl -Url (Get-HcStringProperty $meta 'url_effective') -Expected $expectedUrl -Label "$Label public effective URL"
    Assert-Hc ($headers.headers.ContainsKey('content-type')) "$Label public Content-Type absent"
    Assert-Hc ($headers.headers['content-type'] -ceq $ExpectedContentType) "$Label $Stem Content-Type"
    Assert-Hc ($headers.headers.ContainsKey('content-length')) "$Label public Content-Length absent"
    Assert-Hc ($headers.headers['content-length'] -ceq [string]$expectedSize) "$Label public Content-Length"
    Assert-Hc ($headers.headers.ContainsKey('x-content-sha256')) "$Label public X-Content-SHA256 absent"
    Assert-Hc ($headers.headers['x-content-sha256'] -ceq $expectedHash) "$Label public X-Content-SHA256"
    $body = Read-HcBytes -Path $bodyPath -Label "$Label $Stem"
    Assert-Hc ($body.Length -eq $expectedSize) "$Label $Stem size"
    $actualHash = (Get-FileHash -LiteralPath $bodyPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-Hc ($actualHash -ceq $expectedHash) "$Label $Stem SHA-256"
    if ($CanonicalJson) { [void](Read-HcCanonicalJson -Path $bodyPath -Label "$Label diagnosis result") }
    return [PSCustomObject]@{ body = $body; sha256 = $actualHash; artifact_id = $artifactId; case_id = $caseId }
}

function Invoke-HcPublicCapture {
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][ValidateSet('before', 'after')][string]$Label,
        $Summary,
        [Parameter(Mandatory = $true)][ValidateSet('public_artifact', 'public_result_archive')][string]$ArtifactProperty,
        [Parameter(Mandatory = $true)][string]$Stem,
        [Parameter(Mandatory = $true)][string]$BodyExtension,
        [Parameter(Mandatory = $true)][string]$ExpectedContentType,
        [switch]$CanonicalJson
    )
    $artifact = Get-HcProperty $Summary $ArtifactProperty -Required
    $caseId = Get-HcStringProperty $Summary 'case_id'
    $artifactId = Get-HcStringProperty $artifact 'artifact_id'
    $url = Get-HcStringProperty $artifact 'download_url'
    $expected = "$($script:HcServiceBaseUrl)/api/v1/artifacts/$artifactId/content?case_id=$caseId"
    Assert-HcExactLoopbackUrl -Url $url -Expected $expected -Label "$Label public ArtifactView"
    $bodyPath = Join-Path $EvidenceRoot "$Stem.$Label.$BodyExtension"
    $headersPath = Join-Path $EvidenceRoot "$Stem.$Label.headers"
    $metaPath = Join-Path $EvidenceRoot "$Stem.$Label.meta.json"
    $stdoutPath = Join-Path $EvidenceRoot "$Stem.$Label.curl.stdout.txt"
    $stderrPath = Join-Path $EvidenceRoot "$Stem.$Label.curl.stderr.txt"
    [void](Invoke-HcCurlGet -EvidenceRoot $EvidenceRoot -Url $url -MaxFilesize (Get-HcIntegerProperty $artifact 'size') -BodyPath $bodyPath -HeadersPath $headersPath -MetaPath $metaPath -StdoutPath $stdoutPath -StderrPath $stderrPath)
    return Assert-HcPublicCapture -EvidenceRoot $EvidenceRoot -Label $Label -Summary $Summary -ArtifactProperty $ArtifactProperty -Stem $Stem -BodyExtension $BodyExtension -ExpectedContentType $ExpectedContentType -CanonicalJson:$CanonicalJson
}

function Read-HcInternalLogparseArtifact {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot, [Parameter(Mandatory = $true)][string]$ExpectedCaseId)
    $export = Read-HcCanonicalJson -Path (Join-Path $EvidenceRoot 'state-export.before.json') -Label 'before StateExport'
    Assert-HcExactProperties $export @('export_schema_version', 'schema_version', 'contract_revision', 'source_generation', 'installation_id', 'object_counts', 'state', 'resources') 'StateExport'
    Assert-Hc ((Get-HcIntegerProperty $export 'export_schema_version') -eq 3) 'StateExport export_schema_version'
    Assert-Hc ((Get-HcIntegerProperty $export 'schema_version') -eq 3) 'StateExport schema_version'
    Assert-Hc (-not [string]::IsNullOrWhiteSpace((Get-HcStringProperty $export 'contract_revision'))) 'StateExport contract_revision'
    Assert-Hc ((Get-HcIntegerProperty $export 'source_generation') -gt 0) 'StateExport source_generation'
    Assert-HcUuid (Get-HcStringProperty $export 'installation_id') 'StateExport installation_id'
    $counts = Get-HcProperty $export 'object_counts' -Required
    Assert-HcExactProperties $counts @('cases', 'jobs', 'outcomes', 'outcome_processing_records', 'execution_failure_records', 'attachments', 'evidence', 'artifacts', 'idempotency_records', 'runtime_epochs', 'recovery_processing_records') 'StateExport object_counts'
    Assert-Hc ((Get-HcIntegerProperty $counts 'cases') -eq 2) 'StateExport Case count'
    Assert-Hc ((Get-HcIntegerProperty $counts 'artifacts') -eq 6) 'StateExport Artifact count'
    Assert-Hc ((Get-HcIntegerProperty $counts 'execution_failure_records') -eq 0) 'StateExport execution failure count'
    $state = Get-HcProperty $export 'state' -Required
    Assert-HcExactProperties $state @('schema_version', 'contract_revision', 'generation', 'installation_id', 'created_at', 'updated_at', 'runtime_epochs', 'recovery_processing_records', 'cases', 'idempotency_records') 'StateFile'
    Assert-Hc ((Get-HcIntegerProperty $state 'schema_version') -eq 3) 'StateFile schema_version'
    Assert-Hc ((Get-HcStringProperty $state 'contract_revision') -ceq (Get-HcStringProperty $export 'contract_revision')) 'StateFile contract revision'
    Assert-Hc ((Get-HcIntegerProperty $state 'generation') -eq (Get-HcIntegerProperty $export 'source_generation')) 'StateFile generation'
    Assert-Hc ((Get-HcStringProperty $state 'installation_id') -ceq (Get-HcStringProperty $export 'installation_id')) 'StateFile installation ID'
    $cases = Get-HcProperty $state 'cases' -Required
    Assert-HcJsonObject $cases 'StateFile cases'
    Assert-Hc ($null -ne $cases.PSObject.Properties[$ExpectedCaseId]) 'primary Case must exist in StateFile'
    $aggregate = $cases.PSObject.Properties[$ExpectedCaseId].Value
    Assert-HcExactProperties $aggregate @('case', 'jobs', 'outcomes', 'outcome_processing_records', 'execution_failure_records', 'attachments', 'evidence', 'artifacts') 'CaseAggregate'
    $case = Get-HcProperty $aggregate 'case' -Required
    Assert-HcJsonObject $case 'CaseAggregate case'
    Assert-Hc ((Get-HcStringProperty $case 'case_id') -ceq $ExpectedCaseId) 'CaseAggregate case_id'
    Assert-Hc ((Get-HcStringProperty $case 'status') -ceq 'RESOLVED') 'CaseAggregate status'
    $failures = Get-HcProperty $aggregate 'execution_failure_records' -Required
    Assert-HcJsonObject $failures 'CaseAggregate execution failures'
    Assert-Hc (@($failures.PSObject.Properties).Count -eq 0) 'CaseAggregate execution failures must be empty'
    $artifacts = Get-HcProperty $aggregate 'artifacts' -Required
    Assert-HcJsonObject $artifacts 'CaseAggregate artifacts'
    $records = @($artifacts.PSObject.Properties | ForEach-Object { $_.Value })
    Assert-Hc ($records.Count -eq 3) 'CaseAggregate must contain exactly three artifacts'
    $logparse = @($records | Where-Object { (Get-HcStringProperty $_ 'kind') -ceq 'LOGPARSE_RUN' })
    $userResults = @($records | Where-Object { (Get-HcStringProperty $_ 'kind') -ceq 'USER_RESULT' })
    $userResultArchives = @($records | Where-Object { (Get-HcStringProperty $_ 'kind') -ceq 'USER_RESULT_ARCHIVE' })
    Assert-Hc ($logparse.Count -eq 1) 'CaseAggregate must contain exactly one LOGPARSE_RUN'
    Assert-Hc ($userResults.Count -eq 1) 'CaseAggregate must contain exactly one USER_RESULT'
    Assert-Hc ($userResultArchives.Count -eq 1) 'CaseAggregate must contain exactly one USER_RESULT_ARCHIVE'
    $artifact = $logparse[0]
    Assert-HcExactProperties $artifact @('artifact_id', 'case_id', 'kind', 'name', 'content_type', 'resource_kind', 'size', 'sha256', 'storage_key', 'metadata', 'created_by_job_id', 'created_at') 'LOGPARSE_RUN Artifact'
    $artifactId = Get-HcStringProperty $artifact 'artifact_id'
    Assert-HcUuid $artifactId 'LOGPARSE_RUN artifact_id'
    Assert-Hc ((Get-HcStringProperty $artifact 'case_id') -ceq $ExpectedCaseId) 'LOGPARSE_RUN case_id'
    Assert-Hc ((Get-HcStringProperty $artifact 'kind') -ceq 'LOGPARSE_RUN') 'LOGPARSE_RUN kind'
    Assert-Hc ((Get-HcStringProperty $artifact 'content_type') -ceq 'application/vnd.problem-locator.logparse-run+directory') 'LOGPARSE_RUN content type'
    Assert-Hc ((Get-HcStringProperty $artifact 'resource_kind') -ceq 'DIRECTORY') 'LOGPARSE_RUN resource kind'
    Assert-HcSha256 (Get-HcStringProperty $artifact 'sha256') 'LOGPARSE_RUN SHA-256'
    Assert-HcUuid (Get-HcStringProperty $artifact 'created_by_job_id') 'LOGPARSE_RUN producing Job ID'
    Assert-Hc ($artifacts.PSObject.Properties[$artifactId].Value -eq $artifact) 'LOGPARSE_RUN map key must equal artifact_id'
    return $artifact
}

function Assert-HcInternalErrorBody {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$InternalArtifactId)
    $text = Read-HcUtf8Text -Path $Path -Label 'internal LOGPARSE_RUN response body'
    Assert-Hc (-not $text.Contains($InternalArtifactId)) 'internal LOGPARSE_RUN ID leaked in response body'
    $payload = Read-HcJson -Path $Path -Label 'internal LOGPARSE_RUN response body'
    Assert-HcExactProperties $payload @('ok', 'data', 'error') 'internal error envelope'
    Assert-Hc (-not (Get-HcBooleanProperty $payload 'ok')) 'internal error envelope ok'
    Assert-Hc ($null -eq (Get-HcProperty $payload 'data' -Required)) 'internal error envelope data'
    $error = Get-HcProperty $payload 'error' -Required
    Assert-HcExactProperties $error @('code', 'message', 'details', 'retryable') 'internal ApplicationError'
    Assert-Hc ((Get-HcStringProperty $error 'code') -ceq 'ARTIFACT_NOT_FOUND') 'internal error code'
    Assert-Hc ((Get-HcStringProperty $error 'message') -ceq 'The downloadable Artifact does not exist.') 'internal error message'
    Assert-Hc (-not (Get-HcBooleanProperty $error 'retryable')) 'internal error retryable'
    $details = Get-HcProperty $error 'details' -Required
    Assert-HcJsonArray $details 'internal error details'
    Assert-Hc (@($details).Count -eq 0) 'internal error details must be empty'
    return [System.IO.File]::ReadAllBytes($Path)
}

function Invoke-HcInternalCapture {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot, $BeforeSummary)
    $caseId = Get-HcStringProperty $BeforeSummary 'case_id'
    $artifact = Read-HcInternalLogparseArtifact -EvidenceRoot $EvidenceRoot -ExpectedCaseId $caseId
    $artifactId = Get-HcStringProperty $artifact 'artifact_id'
    $url = "$($script:HcServiceBaseUrl)/api/v1/artifacts/$artifactId/content?case_id=$caseId"
    Assert-HcExactLoopbackUrl -Url $url -Expected $url -Label 'internal LOGPARSE_RUN URL'
    $bodyPath = Join-Path $EvidenceRoot 'internal-logparse.after.body.json'
    $headersPath = Join-Path $EvidenceRoot 'internal-logparse.after.headers'
    $metaPath = Join-Path $EvidenceRoot 'internal-logparse.after.meta.json'
    $stdoutPath = Join-Path $EvidenceRoot 'internal-logparse.after.curl.stdout.txt'
    $stderrPath = Join-Path $EvidenceRoot 'internal-logparse.after.curl.stderr.txt'
    [void](Invoke-HcCurlGet -EvidenceRoot $EvidenceRoot -Url $url -MaxFilesize $script:HcInternalMaxBytes -BodyPath $bodyPath -HeadersPath $headersPath -MetaPath $metaPath -StdoutPath $stdoutPath -StderrPath $stderrPath)
    $meta = Read-HcJson -Path $metaPath -Label 'internal LOGPARSE_RUN curl metadata'
    Assert-HcExactProperties $meta @('http_code', 'num_redirects', 'size_download', 'url_effective') 'internal LOGPARSE_RUN curl metadata'
    Assert-Hc ((Get-HcIntegerProperty $meta 'http_code') -eq 404) 'internal LOGPARSE_RUN metadata status'
    Assert-Hc ((Get-HcIntegerProperty $meta 'num_redirects') -eq 0) 'internal LOGPARSE_RUN redirects'
    Assert-HcExactLoopbackUrl -Url (Get-HcStringProperty $meta 'url_effective') -Expected $url -Label 'internal LOGPARSE_RUN effective URL'
    $headers = Read-HcHeaderCapture -Path $headersPath -ExpectedStatus 404 -Label 'internal LOGPARSE_RUN response headers'
    Assert-Hc ($headers.headers.ContainsKey('content-type')) 'internal LOGPARSE_RUN Content-Type absent'
    Assert-Hc ($headers.headers['content-type'] -ceq 'application/json') 'internal LOGPARSE_RUN Content-Type'
    Assert-Hc ($headers.headers.ContainsKey('content-length')) 'internal LOGPARSE_RUN Content-Length absent'
    Assert-Hc (-not $headers.headers.ContainsKey('x-content-sha256')) 'internal LOGPARSE_RUN must not expose X-Content-SHA256'
    $body = Assert-HcInternalErrorBody -Path $bodyPath -InternalArtifactId $artifactId
    Assert-Hc ($headers.headers['content-length'] -ceq [string]$body.Length) 'internal LOGPARSE_RUN Content-Length'
    Assert-Hc ((Get-HcIntegerProperty $meta 'size_download') -eq $body.Length) 'internal LOGPARSE_RUN downloaded size metadata'
    return [PSCustomObject]@{ artifact_id = $artifactId; case_id = $caseId; http_code = 404 }
}

function Assert-HcBytesEqual {
    param([byte[]]$Before, [byte[]]$After, [Parameter(Mandatory = $true)][string]$Label)
    Assert-Hc ($Before.Length -eq $After.Length) "$Label length"
    for ($index = 0; $index -lt $Before.Length; $index++) {
        Assert-Hc ($Before[$index] -eq $After[$index]) "$Label byte offset $index"
    }
}

function Invoke-HcBeforePhase {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    Confirm-HcJourneyManifest $EvidenceRoot
    $summary = Read-HcJourneySummary $EvidenceRoot
    New-HcOutputReservations -EvidenceRoot $EvidenceRoot -Names (Get-HcBeforeOutputNames)
    $capture = Invoke-HcPublicCapture -EvidenceRoot $EvidenceRoot -Label 'before' -Summary $summary -ArtifactProperty 'public_artifact' -Stem 'diagnosis-result' -BodyExtension 'json' -ExpectedContentType 'application/json' -CanonicalJson
    $archiveCapture = Invoke-HcPublicCapture -EvidenceRoot $EvidenceRoot -Label 'before' -Summary $summary -ArtifactProperty 'public_result_archive' -Stem 'result-archive' -BodyExtension 'zip' -ExpectedContentType 'application/zip'
    return [PSCustomObject]@{ phase = 'Before'; case_id = $capture.case_id; artifact_id = $capture.artifact_id; sha256 = $capture.sha256; archive_artifact_id = $archiveCapture.artifact_id; archive_sha256 = $archiveCapture.sha256; http_code = 200 }
}

function Invoke-HcAfterPhase {
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    Confirm-HcJourneyManifest $EvidenceRoot
    $beforeSummary = Read-HcJourneySummary $EvidenceRoot
    $beforeCapture = Assert-HcPublicCapture -EvidenceRoot $EvidenceRoot -Label 'before' -Summary $beforeSummary -ArtifactProperty 'public_artifact' -Stem 'diagnosis-result' -BodyExtension 'json' -ExpectedContentType 'application/json' -CanonicalJson
    $beforeArchiveCapture = Assert-HcPublicCapture -EvidenceRoot $EvidenceRoot -Label 'before' -Summary $beforeSummary -ArtifactProperty 'public_result_archive' -Stem 'result-archive' -BodyExtension 'zip' -ExpectedContentType 'application/zip'
    Confirm-HcRestartManifest $EvidenceRoot
    $restartSummary = Read-HcRestartSummary -EvidenceRoot $EvidenceRoot -BeforeSummary $beforeSummary
    [void](Read-HcInternalLogparseArtifact -EvidenceRoot $EvidenceRoot -ExpectedCaseId (Get-HcStringProperty $beforeSummary 'case_id'))
    New-HcOutputReservations -EvidenceRoot $EvidenceRoot -Names (Get-HcAfterOutputNames)
    $afterCapture = Invoke-HcPublicCapture -EvidenceRoot $EvidenceRoot -Label 'after' -Summary $restartSummary -ArtifactProperty 'public_artifact' -Stem 'diagnosis-result' -BodyExtension 'json' -ExpectedContentType 'application/json' -CanonicalJson
    $afterArchiveCapture = Invoke-HcPublicCapture -EvidenceRoot $EvidenceRoot -Label 'after' -Summary $restartSummary -ArtifactProperty 'public_result_archive' -Stem 'result-archive' -BodyExtension 'zip' -ExpectedContentType 'application/zip'
    Assert-HcBytesEqual -Before $beforeCapture.body -After $afterCapture.body -Label 'public result before/after restart'
    Assert-Hc ($beforeCapture.sha256 -ceq $afterCapture.sha256) 'public result hash before/after restart'
    Assert-HcBytesEqual -Before $beforeArchiveCapture.body -After $afterArchiveCapture.body -Label 'public archive before/after restart'
    Assert-Hc ($beforeArchiveCapture.sha256 -ceq $afterArchiveCapture.sha256) 'public archive hash before/after restart'
    $internal = Invoke-HcInternalCapture -EvidenceRoot $EvidenceRoot -BeforeSummary $beforeSummary
    return [PSCustomObject]@{ phase = 'After'; case_id = $afterCapture.case_id; artifact_id = $afterCapture.artifact_id; sha256 = $afterCapture.sha256; archive_artifact_id = $afterArchiveCapture.artifact_id; archive_sha256 = $afterArchiveCapture.sha256; http_code = 200; internal_artifact_id = $internal.artifact_id; internal_http_code = 404 }
}
