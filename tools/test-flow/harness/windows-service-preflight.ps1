param([string]$EvidenceRoot = $PSScriptRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http
Add-Type -AssemblyName System.Web.Extensions

$EvidenceRoot = [System.IO.Path]::GetFullPath($EvidenceRoot)
$outputPath = Join-Path $EvidenceRoot 'windows-live-ready-preflight.json'
if (Test-Path -LiteralPath $outputPath) {
    throw 'refusing to overwrite Windows service preflight evidence'
}

function Assert-ExactKeys {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Code
    )
    if (-not ($Object -is [System.Collections.IDictionary])) { throw $Code }
    $keys = @($Object.Keys | ForEach-Object { [string]$_ })
    if ($keys.Count -ne $Expected.Count) { throw $Code }
    foreach ($key in $Expected) {
        if (-not ($keys -ccontains $key)) { throw $Code }
    }
}

function Assert-BooleanTrue {
    param($Value, [string]$Code)
    if (-not ($Value -is [bool]) -or $Value -ne $true) { throw $Code }
}

$handler = New-Object System.Net.Http.HttpClientHandler
$handler.UseProxy = $false
$handler.Proxy = $null
$handler.AllowAutoRedirect = $false
$handler.UseCookies = $false
$client = New-Object System.Net.Http.HttpClient($handler)
$client.Timeout = [TimeSpan]::FromSeconds(10)
$serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$serializer.MaxJsonLength = 65536
$serializer.RecursionLimit = 32
$strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)

function Invoke-StrictJsonGet {
    param([Parameter(Mandatory = $true)][string]$Url)
    $response = $client.GetAsync($Url).GetAwaiter().GetResult()
    try {
        if ([int]$response.StatusCode -ne 200) { throw 'WINDOWS_HTTP_STATUS' }
        if ($null -eq $response.Content.Headers.ContentType) { throw 'WINDOWS_CONTENT_TYPE' }
        if ($response.Content.Headers.ContentType.MediaType -cne 'application/json') { throw 'WINDOWS_CONTENT_TYPE' }
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        if ($bytes.Length -eq 0 -or $bytes.Length -gt 65536) { throw 'WINDOWS_BODY_SIZE' }
        $text = $strictUtf8.GetString($bytes)
        $parsed = $serializer.DeserializeObject($text)
        if (-not ($parsed -is [System.Collections.IDictionary])) { throw 'WINDOWS_JSON_ROOT_TYPE' }
        return $parsed
    }
    finally {
        $response.Dispose()
    }
}

try {
    $live = Invoke-StrictJsonGet -Url 'http://127.0.0.1:18000/live'
    Assert-ExactKeys -Object $live -Expected @('ok', 'data', 'error') -Code 'WINDOWS_LIVE_ENVELOPE_KEYS'
    Assert-BooleanTrue -Value $live['ok'] -Code 'WINDOWS_LIVE_OK_TYPE'
    if ($null -ne $live['error']) { throw 'WINDOWS_LIVE_ERROR' }
    Assert-ExactKeys -Object $live['data'] -Expected @('status') -Code 'WINDOWS_LIVE_DATA_KEYS'
    if (-not ($live['data']['status'] -is [string]) -or $live['data']['status'] -cne 'live') { throw 'WINDOWS_LIVE_STATUS' }

    $ready = Invoke-StrictJsonGet -Url 'http://127.0.0.1:18000/ready'
    Assert-ExactKeys -Object $ready -Expected @('ok', 'data', 'error') -Code 'WINDOWS_READY_ENVELOPE_KEYS'
    Assert-BooleanTrue -Value $ready['ok'] -Code 'WINDOWS_READY_OK_TYPE'
    if ($null -ne $ready['error']) { throw 'WINDOWS_READY_ERROR' }
    Assert-ExactKeys -Object $ready['data'] -Expected @('ready', 'checks', 'error') -Code 'WINDOWS_READY_DATA_KEYS'
    Assert-BooleanTrue -Value $ready['data']['ready'] -Code 'WINDOWS_READY_VALUE_TYPE'
    if ($null -ne $ready['data']['error']) { throw 'WINDOWS_READY_REPORT_ERROR' }
    $checks = $ready['data']['checks']
    if (-not ($checks -is [System.Collections.IList]) -or $checks.Count -ne 5) { throw 'WINDOWS_READY_CHECK_COUNT' }
    $expectedNames = @('CONFIG', 'INSTANCE_LOCK', 'STATE', 'DATA_DIRECTORIES', 'RECOVERY')
    for ($index = 0; $index -lt $expectedNames.Count; $index++) {
        $check = $checks[$index]
        Assert-ExactKeys -Object $check -Expected @('name', 'passed', 'message') -Code 'WINDOWS_READY_CHECK_KEYS'
        if (-not ($check['name'] -is [string]) -or $check['name'] -cne $expectedNames[$index]) { throw 'WINDOWS_READY_CHECK_NAME' }
        Assert-BooleanTrue -Value $check['passed'] -Code 'WINDOWS_READY_CHECK_PASSED_TYPE'
        if ($null -ne $check['message']) { throw 'WINDOWS_READY_CHECK_MESSAGE' }
    }

    $report = [PSCustomObject][ordered]@{
        schema_version = 1
        base_url = 'http://127.0.0.1:18000'
        http_client = 'System.Net.Http.HttpClient'
        proxy_used = $false
        redirects_allowed = $false
        live_status = 200
        live_body_strict = $true
        ready_status = 200
        ready_body_strict = $true
        ready_check_names = $expectedNames
        ready_check_count = 5
        all_ready_checks_passed = $true
    }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $bytes = $utf8.GetBytes((($report | ConvertTo-Json -Depth 5) + "`n"))
    $stream = [System.IO.File]::Open($outputPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    Write-Output 'WINDOWS_LIVE_READY_PREFLIGHT_PASSED'
}
finally {
    $client.Dispose()
    $handler.Dispose()
}
