param([string]$DriverRoot = $PSScriptRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DriverRoot = [System.IO.Path]::GetFullPath($DriverRoot)

$files = @(
    'windows-http-capture-lib.ps1',
    'run-windows-http-capture.ps1',
    'static-check-http-capture.ps1',
    'README-http-capture.md'
)
foreach ($name in $files) {
    $path = Join-Path $DriverRoot $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "missing HTTP capture driver file: $name"
    }
    if (((Get-Item -LiteralPath $path -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "HTTP capture driver source must not be a reparse point: $name"
    }
}

$parseErrors = @()
foreach ($name in @('windows-http-capture-lib.ps1', 'run-windows-http-capture.ps1', 'static-check-http-capture.ps1')) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $DriverRoot $name), [ref]$tokens, [ref]$errors)
    $parseErrors += @($errors)
}
if ($parseErrors.Count -ne 0) {
    throw ('PowerShell 5.1 parse errors: ' + (($parseErrors | ForEach-Object { $_.Message }) -join '; '))
}

. (Join-Path $DriverRoot 'windows-http-capture-lib.ps1')

$hcPropertyProbe = '{"empty":[],"singleton":[{"id":"only"}],"multi":["first","second"],"scalar":"scalar"}' | ConvertFrom-Json
$hcEmpty = Get-HcProperty $hcPropertyProbe 'empty' -Required
$hcSingleton = Get-HcProperty $hcPropertyProbe 'singleton' -Required
$hcMulti = Get-HcProperty $hcPropertyProbe 'multi' -Required
$hcScalar = Get-HcProperty $hcPropertyProbe 'scalar' -Required
if ($hcEmpty -isnot [System.Array] -or @($hcEmpty).Count -ne 0 -or
    -not [object]::ReferenceEquals($hcEmpty, $hcPropertyProbe.PSObject.Properties['empty'].Value)) {
    throw 'Get-HcProperty must preserve an empty JSON array'
}
if ($hcSingleton -isnot [System.Array] -or @($hcSingleton).Count -ne 1 -or $hcSingleton[0].id -cne 'only' -or
    -not [object]::ReferenceEquals($hcSingleton, $hcPropertyProbe.PSObject.Properties['singleton'].Value)) {
    throw 'Get-HcProperty must preserve a singleton JSON array'
}
if ($hcMulti -isnot [System.Array] -or @($hcMulti).Count -ne 2 -or $hcMulti[0] -cne 'first' -or $hcMulti[1] -cne 'second' -or
    -not [object]::ReferenceEquals($hcMulti, $hcPropertyProbe.PSObject.Properties['multi'].Value)) {
    throw 'Get-HcProperty must preserve a multi-element JSON array'
}
if ($hcScalar -isnot [string] -or $hcScalar -cne 'scalar') {
    throw 'Get-HcProperty must preserve a JSON scalar'
}

$auditedFiles = @(
    'windows-http-capture-lib.ps1',
    'run-windows-http-capture.ps1',
    'README-http-capture.md'
)
$sourceText = (($auditedFiles | ForEach-Object { [System.IO.File]::ReadAllText((Join-Path $DriverRoot $_)) }) -join "`n")
$requiredLiterals = @(
    'C:\Windows\System32\curl.exe',
    'http://127.0.0.1:18000',
    "'--disable'",
    "'--proxy', ''",
    "'--noproxy', '*'",
    "'--max-redirs', '0'",
    "'--connect-timeout'",
    "'--max-time'",
    "'--max-filesize'",
    "'--proto', '=http'",
    "'Accept-Encoding: identity'",
    "'%{http_code}|%{num_redirects}|%{size_download}|%{url_effective}'",
    '$start.EnvironmentVariables.Clear()',
    'FileMode]::CreateNew',
    'windows-journey-driver-manifest.json',
    'journey-authoritative-summary.json',
    'windows-restart-driver-manifest.json',
    'restart-authoritative-summary.json',
    'state-export.before.json',
    'LOGPARSE_RUN',
    'ARTIFACT_NOT_FOUND',
    'diagnosis-result.before.json',
    'diagnosis-result.after.json',
    'internal-logparse.after.body.json'
)
foreach ($literal in $requiredLiterals) {
    if (-not $sourceText.Contains($literal)) {
        throw "required HTTP capture literal absent: $literal"
    }
}

$forbiddenPatterns = @(
    '(?i)settings\.json',
    '(?i)ANTHROPIC_(?:API_KEY|AUTH_TOKEN)',
    '(?i)DEEPSEEK(?:_API)?_KEY',
    '(?i)\$env:',
    '(?i)Get-ChildItem\s+(?:-Path\s+)?Env:',
    '(?i)GetEnvironmentVariable',
    '(?i)Invoke-WebRequest',
    '(?i)Invoke-RestMethod',
    '(?i)System\.Net\.Http',
    '(?i)WebClient',
    '(?i)Start-BitsTransfer',
    '(?i)claude\.exe',
    "(?i)'--location'",
    "(?i)'--upload-file'",
    "(?i)'--data(?:-binary|-raw|-urlencode)?'",
    "(?i)'--form'",
    "(?i)'--config'",
    "(?i)'Authorization:",
    "(?i)'Cookie:",
    "(?i)'POST'|(?i)'PUT'|(?i)'PATCH'|(?i)'DELETE'"
)
foreach ($pattern in $forbiddenPatterns) {
    if ($sourceText -match $pattern) {
        throw "forbidden HTTP capture behavior matched: $pattern"
    }
}

$outputs = @(Get-HcAllOutputNames)
if ($outputs.Count -ne 15) {
    throw 'HTTP capture output inventory must contain exactly 15 files'
}
Assert-HcExactStrings @($outputs | Sort-Object) @((Get-HcBeforeOutputNames) + (Get-HcAfterOutputNames) | Sort-Object) 'static HTTP capture output inventory'
foreach ($name in $outputs) {
    if (-not $sourceText.Contains("'$name'")) {
        throw "HTTP capture runtime output is absent from source inventory: $name"
    }
    if (Test-Path -LiteralPath (Join-Path $DriverRoot $name)) {
        throw "HTTP capture template must not contain runtime output: $name"
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
    reads_settings_or_environment = $false
    http_runtime_contacts_loopback_only = $true
    curl_executable = $script:HcCurlExe
    service_base_url = $script:HcServiceBaseUrl
    phases = @('Before', 'After')
    all_runtime_outputs_create_new = $true
    source_of_public_artifact = 'validated authoritative journey summaries'
    source_of_internal_artifact = 'unique LOGPARSE_RUN in canonical state-export.before.json'
    possible_runtime_outputs = $outputs
    files = $manifestFiles
}
$manifestPath = Join-Path $DriverRoot 'windows-http-capture-driver-manifest.json'
if (Test-Path -LiteralPath $manifestPath) {
    throw 'refusing to overwrite HTTP capture static-check manifest'
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
$bytes = $utf8.GetBytes((($manifest | ConvertTo-Json -Depth 20) + "`n"))
$stream = [System.IO.File]::Open($manifestPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
try {
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush($true)
}
finally {
    $stream.Dispose()
}
Confirm-HcDriverManifest -DriverRoot $DriverRoot
Write-Output 'HTTP_CAPTURE_STATIC_CHECK_PASSED'
