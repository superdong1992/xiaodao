[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $script:Utf8
[Console]::OutputEncoding = $script:Utf8

$script:SchemaVersion = 1
$script:HookVersion = '1.0.2'
$script:ToolPrefix = 'mcp__problem-locator__'
$script:AllowedTools = [Collections.Generic.HashSet[string]]::new(
    [StringComparer]::Ordinal
)
foreach ($toolName in @(
    'problem_locator_create_case',
    'problem_locator_prepare_attachment',
    'problem_locator_submit_supplement',
    'problem_locator_get_case',
    'problem_locator_resume_case',
    'problem_locator_cancel_case',
    'problem_locator_list_artifacts'
)) {
    [void]$script:AllowedTools.Add($toolName)
}

function Get-OptionalValue {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)

    if ($null -eq $Object) {
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Get-JsonType {
    param($Value)

    if ($null -eq $Value) { return 'null' }
    if ($Value -is [string]) { return 'string' }
    if ($Value -is [bool]) { return 'boolean' }
    if (
        $Value -is [byte] -or $Value -is [sbyte] -or
        $Value -is [int16] -or $Value -is [uint16] -or
        $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64] -or
        $Value -is [single] -or $Value -is [double] -or
        $Value -is [decimal]
    ) { return 'number' }
    if ($Value -is [Array]) { return 'array' }
    if (
        $Value -is [Collections.IDictionary] -or
        $Value -is [Management.Automation.PSCustomObject]
    ) { return 'object' }
    return 'object'
}

function Get-ArgumentTypes {
    param($Arguments)

    $types = [ordered]@{}
    if ($null -eq $Arguments) {
        return $types
    }
    foreach ($property in $Arguments.PSObject.Properties) {
        $types[$property.Name] = Get-JsonType $property.Value
    }
    return $types
}

function Resolve-LogPath {
    param($HookInput)

    $configured = $env:PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE
    if (-not [string]::IsNullOrWhiteSpace($configured)) {
        if (-not [IO.Path]::IsPathRooted($configured)) {
            throw 'PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE must be an absolute path'
        }
        return [IO.Path]::GetFullPath($configured)
    }

    $projectRoot = $env:CLAUDE_PROJECT_DIR
    if ([string]::IsNullOrWhiteSpace($projectRoot)) {
        $projectRoot = Get-OptionalValue $HookInput 'cwd'
    }
    if ([string]::IsNullOrWhiteSpace($projectRoot)) {
        $projectRoot = [Environment]::CurrentDirectory
    }
    return [IO.Path]::GetFullPath(
        [IO.Path]::Combine($projectRoot, '.problem-locator', 'client-dfx.jsonl')
    )
}

function Get-MutexName {
    param([Parameter(Mandatory = $true)][string]$LogPath)

    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($LogPath.ToLowerInvariant())
        $digest = $sha256.ComputeHash($bytes)
    }
    finally {
        $sha256.Dispose()
    }
    $hex = -join ($digest | ForEach-Object { $_.ToString('x2') })
    return "Local\ProblemLocatorClientDfx-$hex"
}

function Write-JsonLine {
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)]$Record
    )

    $directory = [IO.Path]::GetDirectoryName($LogPath)
    if ([string]::IsNullOrWhiteSpace($directory)) {
        throw 'client DFX log path has no parent directory'
    }
    [void][IO.Directory]::CreateDirectory($directory)

    $mutex = [Threading.Mutex]::new($false, (Get-MutexName $LogPath))
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds(4))
        }
        catch [Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if (-not $acquired) {
            throw 'timed out acquiring the client DFX log mutex'
        }

        $json = $Record | ConvertTo-Json -Depth 100 -Compress
        $encoding = [Text.UTF8Encoding]::new($false)
        $payload = $encoding.GetBytes($json + "`n")
        $stream = [IO.File]::Open(
            $LogPath,
            [IO.FileMode]::Append,
            [IO.FileAccess]::Write,
            [IO.FileShare]::Read
        )
        try {
            $stream.Write($payload, 0, $payload.Length)
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }
    }
    finally {
        if ($acquired) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

$rawInput = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($rawInput)) {
    exit 0
}
try {
    $hookInput = $rawInput | ConvertFrom-Json
}
catch {
    exit 0
}

$hookEventName = Get-OptionalValue $hookInput 'hook_event_name'
$eventName = switch ($hookEventName) {
    'PreToolUse' { 'client.hook.tool.started' }
    'PostToolUse' { 'client.hook.tool.returned' }
    'PostToolUseFailure' { 'client.hook.tool.failed' }
    default { $null }
}
if ($null -eq $eventName) {
    exit 0
}

$fullToolName = Get-OptionalValue $hookInput 'tool_name'
if (
    $fullToolName -isnot [string] -or
    -not $fullToolName.StartsWith($script:ToolPrefix, [StringComparison]::Ordinal)
) {
    exit 0
}
$logicalTool = $fullToolName.Substring($script:ToolPrefix.Length)
if (-not $script:AllowedTools.Contains($logicalTool)) {
    exit 0
}

$arguments = Get-OptionalValue $hookInput 'tool_input'
if ($null -eq $arguments) {
    $arguments = [pscustomobject]@{}
}
$toolUseId = Get-OptionalValue $hookInput 'tool_use_id'
$requestId = Get-OptionalValue $arguments 'request_id'
$caseId = Get-OptionalValue $arguments 'case_id'
$operationId = if ($requestId -is [string] -and $requestId.Length -gt 0) {
    $requestId
}
elseif ($caseId -is [string] -and $caseId.Length -gt 0) {
    "${logicalTool}:$caseId"
}
else {
    $toolUseId
}

$record = [ordered]@{
    schema_version = $script:SchemaVersion
    timestamp = [DateTime]::UtcNow.ToString(
        'o',
        [Globalization.CultureInfo]::InvariantCulture
    )
    event = $eventName
    source = 'claude_code_hook'
    hook_version = $script:HookVersion
    session_id = Get-OptionalValue $hookInput 'session_id'
    tool_use_id = $toolUseId
    tool_name = $fullToolName
    logical_tool = $logicalTool
    operation_id = $operationId
    arguments = $arguments
    argument_json_types = Get-ArgumentTypes $arguments
}
foreach ($optionalName in @(
    'prompt_id',
    'permission_mode',
    'agent_id',
    'agent_type'
)) {
    $optionalValue = Get-OptionalValue $hookInput $optionalName
    if ($null -ne $optionalValue) {
        $record[$optionalName] = $optionalValue
    }
}

if ($hookEventName -eq 'PostToolUse') {
    $record['tool_response'] = Get-OptionalValue $hookInput 'tool_response'
    $duration = Get-OptionalValue $hookInput 'duration_ms'
    if ($null -ne $duration) {
        $record['duration_ms'] = $duration
    }
}
elseif ($hookEventName -eq 'PostToolUseFailure') {
    $record['error'] = Get-OptionalValue $hookInput 'error'
    $interrupted = Get-OptionalValue $hookInput 'is_interrupt'
    if ($null -ne $interrupted) {
        $record['is_interrupt'] = $interrupted
    }
    $duration = Get-OptionalValue $hookInput 'duration_ms'
    if ($null -ne $duration) {
        $record['duration_ms'] = $duration
    }
}

try {
    Write-JsonLine (Resolve-LogPath $hookInput) $record
}
catch {
    [Console]::Error.WriteLine(
        'problem-locator client DFX logging failed: ' + $_.Exception.Message
    )
    exit 1
}
exit 0
