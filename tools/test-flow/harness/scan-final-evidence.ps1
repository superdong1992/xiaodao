param(
    [string]$EvidenceRoot = $PSScriptRoot,
    [string]$SettingsPath = 'C:\Users\admin\.claude\settings.json',
    [ValidateSet('pre-cleanup-secret-scan.json', 'pre-final-secret-scan.json', 'final-secret-scan.json')]
    [string]$OutputName = 'final-secret-scan.json'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Web.Extensions

$EvidenceRoot = [System.IO.Path]::GetFullPath($EvidenceRoot)
$SettingsPath = [System.IO.Path]::GetFullPath($SettingsPath)
$outputPath = Join-Path $EvidenceRoot $OutputName
if (-not (Test-Path -LiteralPath $EvidenceRoot -PathType Container)) { throw 'EVIDENCE_ROOT_ABSENT' }
if (-not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) { throw 'SETTINGS_ABSENT' }
if (Test-Path -LiteralPath $outputPath) { throw 'FINAL_SCAN_OUTPUT_EXISTS' }

$evidenceRootItem = Get-Item -LiteralPath $EvidenceRoot -Force
if (($evidenceRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'EVIDENCE_ROOT_REPARSE_POINT' }

$settingsItem = Get-Item -LiteralPath $SettingsPath -Force
if (($settingsItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'SETTINGS_REPARSE_POINT' }
$serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$serializer.MaxJsonLength = 1048576
$settingsText = [System.IO.File]::ReadAllText($SettingsPath, (New-Object System.Text.UTF8Encoding($false, $true)))
foreach ($requiredKey in @('env', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL')) {
    $pattern = '"' + [regex]::Escape($requiredKey) + '"\s*:'
    if ([regex]::Matches($settingsText, $pattern, [Text.RegularExpressions.RegexOptions]::CultureInvariant).Count -ne 1) { throw 'SETTINGS_RAW_KEY_COUNT' }
}
$settings = $serializer.DeserializeObject($settingsText)
if (-not ($settings -is [System.Collections.IDictionary])) { throw 'SETTINGS_ROOT' }

function Get-FinalScanDictionaryValueOrdinal {
    param([System.Collections.IDictionary]$Dictionary, [string]$Name, [string]$Code)
    $matchingKey = $null
    $matchingKeyCount = 0
    foreach ($candidateKey in $Dictionary.Keys) {
        if (-not ($candidateKey -is [string])) { throw "${Code}_NON_STRING_KEY" }
        if ($candidateKey -ceq $Name) {
            $matchingKey = $candidateKey
            $matchingKeyCount++
        }
    }
    if ($matchingKeyCount -ne 1) { throw $Code }
    return $Dictionary[$matchingKey]
}

$envObject = Get-FinalScanDictionaryValueOrdinal $settings 'env' 'SETTINGS_ENV_KEY'
if (-not ($envObject -is [System.Collections.IDictionary])) { throw 'SETTINGS_ENV' }
$token = Get-FinalScanDictionaryValueOrdinal $envObject 'ANTHROPIC_AUTH_TOKEN' 'SETTINGS_TOKEN_KEY'
$baseUrl = Get-FinalScanDictionaryValueOrdinal $envObject 'ANTHROPIC_BASE_URL' 'SETTINGS_BASE_URL_KEY'
if (-not ($token -is [string]) -or $token.Length -lt 16) { throw 'SETTINGS_TOKEN' }
if (-not ($baseUrl -is [string]) -or -not $baseUrl.StartsWith('https://', [System.StringComparison]::Ordinal)) { throw 'SETTINGS_BASE_URL' }
$utf8 = New-Object System.Text.UTF8Encoding($false, $true)
$needles = @($utf8.GetBytes($token), $utf8.GetBytes($baseUrl))

function Test-ByteSequence {
    param([byte[]]$Buffer, [int]$Count, [byte[]]$Needle)
    if ($Needle.Length -eq 0 -or $Count -lt $Needle.Length) { return $false }
    $limit = $Count - $Needle.Length
    for ($offset = 0; $offset -le $limit; $offset++) {
        if ($Buffer[$offset] -ne $Needle[0]) { continue }
        $matches = $true
        for ($index = 1; $index -lt $Needle.Length; $index++) {
            if ($Buffer[$offset + $index] -ne $Needle[$index]) {
                $matches = $false
                break
            }
        }
        if ($matches) { return $true }
    }
    return $false
}

function Test-FileForSensitiveValue {
    param([string]$Path, [object[]]$Sequences)
    $maxLength = ($Sequences | ForEach-Object { $_.Length } | Measure-Object -Maximum).Maximum
    $chunkSize = 1048576
    $readBuffer = New-Object byte[] $chunkSize
    $overlap = New-Object byte[] 0
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        while (($read = $stream.Read($readBuffer, 0, $readBuffer.Length)) -gt 0) {
            $combined = New-Object byte[] ($overlap.Length + $read)
            if ($overlap.Length -gt 0) { [System.Array]::Copy($overlap, 0, $combined, 0, $overlap.Length) }
            [System.Array]::Copy($readBuffer, 0, $combined, $overlap.Length, $read)
            foreach ($sequence in $Sequences) {
                if (Test-ByteSequence -Buffer $combined -Count $combined.Length -Needle $sequence) { return $true }
            }
            $keep = [Math]::Min([Math]::Max(0, $maxLength - 1), $combined.Length)
            $overlap = New-Object byte[] $keep
            if ($keep -gt 0) { [System.Array]::Copy($combined, $combined.Length - $keep, $overlap, 0, $keep) }
        }
    }
    finally {
        $stream.Dispose()
    }
    return $false
}

$rootPrefix = $EvidenceRoot.TrimEnd('\') + '\'
$directoryQueue = New-Object 'System.Collections.Generic.Queue[System.IO.DirectoryInfo]'
$fileList = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'
$directoryQueue.Enqueue($evidenceRootItem)
while ($directoryQueue.Count -gt 0) {
    $directory = $directoryQueue.Dequeue()
    foreach ($entry in @(Get-ChildItem -LiteralPath $directory.FullName -Force | Sort-Object FullName)) {
        $entryPath = [System.IO.Path]::GetFullPath($entry.FullName)
        if (-not $entryPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'SCAN_PATH_ESCAPE' }
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'SCAN_REPARSE_POINT' }
        if ($entry.PSIsContainer) { $directoryQueue.Enqueue([System.IO.DirectoryInfo]$entry) }
        else { $fileList.Add([System.IO.FileInfo]$entry) }
    }
}
$files = @($fileList.ToArray() | Sort-Object FullName)
$hits = 0
$bytesScanned = [Int64]0
foreach ($file in $files) {
    $fullPath = [System.IO.Path]::GetFullPath($file.FullName)
    if (-not $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'SCAN_PATH_ESCAPE' }
    if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'SCAN_REPARSE_POINT' }
    if ($fullPath -ieq $outputPath) { throw 'SCAN_OUTPUT_PRESENT' }
    $bytesScanned += $file.Length
    if (Test-FileForSensitiveValue -Path $fullPath -Sequences $needles) { $hits++ }
}

$report = [PSCustomObject][ordered]@{
    schema_version = 1
    status = $(if ($hits -eq 0) { 'PASS' } else { 'FAIL' })
    files_scanned = $files.Count
    bytes_scanned = $bytesScanned
    sensitive_values_checked = $needles.Count
    sensitive_value_occurrences = $hits
}
$json = ($report | ConvertTo-Json -Depth 5 -Compress) + "`n"
$bytes = $utf8.GetBytes($json)
$output = [System.IO.File]::Open($outputPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
try {
    $output.Write($bytes, 0, $bytes.Length)
    $output.Flush($true)
}
finally {
    $output.Dispose()
}
if ($hits -ne 0) { throw 'FINAL_SECRET_SCAN_NONZERO' }
Write-Output 'FINAL_SECRET_SCAN_PASSED'
