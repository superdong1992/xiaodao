[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $script:Utf8
[Console]::OutputEncoding = $script:Utf8

$script:ToolPrefixes = @(
    'mcp__problem-locator__',
    'problem_locator_'
)
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

function Get-OptionalProperty {
    param($Object, [Parameter(Mandatory = $true)][string]$Name)

    if ($null -eq $Object) {
        return $null
    }
    return $Object.PSObject.Properties[$Name]
}

function Get-LogicalToolName {
    param([Parameter(Mandatory = $true)][string]$FullToolName)

    foreach ($prefix in $script:ToolPrefixes) {
        if ($FullToolName.StartsWith($prefix, [StringComparison]::Ordinal)) {
            return $FullToolName.Substring($prefix.Length)
        }
    }
    return $null
}

function Convert-JsonObjectString {
    param([Parameter(Mandatory = $true)][string]$Value)

    $trimmed = $Value.Trim()
    if (-not $trimmed.StartsWith('{', [StringComparison]::Ordinal)) {
        return $null
    }
    try {
        $parsed = $Value | ConvertFrom-Json
    }
    catch {
        return $null
    }
    if (
        $parsed -is [Collections.IDictionary] -or
        $parsed -is [Management.Automation.PSCustomObject]
    ) {
        return $parsed
    }
    return $null
}

function Convert-JsonArrayString {
    param([Parameter(Mandatory = $true)][string]$Value)

    $trimmed = $Value.Trim()
    if (-not $trimmed.StartsWith('[', [StringComparison]::Ordinal)) {
        return $null
    }
    try {
        $parsed = $Value | ConvertFrom-Json
    }
    catch {
        return $null
    }
    # Windows PowerShell may enumerate a one-item JSON array. Re-wrap the
    # parsed result because the source root was proven to be an array.
    return [pscustomobject]@{ Value = @($parsed) }
}

function Convert-ObjectPropertyOnce {
    param(
        $Arguments,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $property = Get-OptionalProperty $Arguments $Name
    if ($null -eq $property -or $property.Value -isnot [string]) {
        return $false
    }
    $converted = Convert-JsonObjectString $property.Value
    if ($null -eq $converted) {
        return $false
    }
    $property.Value = $converted
    return $true
}

function Convert-InitialUserFactsOnce {
    param($Arguments)

    $property = Get-OptionalProperty $Arguments 'initial_user_facts'
    if ($null -eq $property) {
        return $false
    }

    $changed = $false
    $facts = $property.Value
    if ($facts -is [string]) {
        $wrapper = Convert-JsonArrayString $facts
        if ($null -eq $wrapper) {
            return $false
        }
        $facts = @($wrapper.Value)
        $changed = $true
    }
    elseif ($facts -isnot [Array]) {
        return $false
    }

    $convertedFacts = @()
    foreach ($fact in @($facts)) {
        if ($fact -is [string]) {
            $converted = Convert-JsonObjectString $fact
            if ($null -ne $converted) {
                $convertedFacts += $converted
                $changed = $true
                continue
            }
        }
        $convertedFacts += $fact
    }
    if ($changed) {
        $property.Value = @($convertedFacts)
    }
    return $changed
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

$eventProperty = Get-OptionalProperty $hookInput 'hook_event_name'
if ($null -eq $eventProperty -or $eventProperty.Value -cne 'PreToolUse') {
    exit 0
}
$toolProperty = Get-OptionalProperty $hookInput 'tool_name'
if ($null -eq $toolProperty -or $toolProperty.Value -isnot [string]) {
    exit 0
}
$logicalTool = Get-LogicalToolName $toolProperty.Value
if ($null -eq $logicalTool -or -not $script:AllowedTools.Contains($logicalTool)) {
    exit 0
}
$inputProperty = Get-OptionalProperty $hookInput 'tool_input'
if ($null -eq $inputProperty) {
    exit 0
}
$arguments = $inputProperty.Value
if (
    $arguments -isnot [Collections.IDictionary] -and
    $arguments -isnot [Management.Automation.PSCustomObject]
) {
    exit 0
}

try {
    $changed = $false
    if ($logicalTool -ceq 'problem_locator_create_case') {
        if (Convert-ObjectPropertyOnce $arguments 'problem_spec') {
            $changed = $true
        }
        if (Convert-InitialUserFactsOnce $arguments) {
            $changed = $true
        }
    }
    elseif ($logicalTool -ceq 'problem_locator_submit_supplement') {
        if (Convert-ObjectPropertyOnce $arguments 'inputs') {
            $changed = $true
        }
    }

    if ($changed) {
        [ordered]@{
            hookSpecificOutput = [ordered]@{
                hookEventName = 'PreToolUse'
                updatedInput = $arguments
            }
        } | ConvertTo-Json -Depth 100 -Compress
    }
    exit 0
}
catch {
    [Console]::Error.WriteLine(
        "problem-locator client compatibility Hook failed: $($_.Exception.Message)"
    )
    exit 1
}
