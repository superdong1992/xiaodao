Set-StrictMode -Version Latest

function Get-E2EBusinessPatchIdentity {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$PatchPath)

    $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
    $fullPath = [IO.Path]::GetFullPath($PatchPath)
    $text = [IO.File]::ReadAllText($fullPath, $strictUtf8)
    if ([string]::IsNullOrEmpty($text) -or -not $text.StartsWith('diff --git ')) {
        throw 'E2E_BUSINESS_PATCH_EMPTY_OR_MALFORMED'
    }
    if ($text.Contains("`r")) {
        throw 'E2E_BUSINESS_PATCH_NON_CANONICAL_LINE_ENDINGS'
    }

    $sections = @([regex]::Split($text, '(?m)(?=^diff --git )') | Where-Object { $_.Length -gt 0 })
    $productionSections = [Collections.Generic.List[string]]::new()
    $productionPaths = [Collections.Generic.List[string]]::new()
    foreach ($section in $sections) {
        $header = [regex]::Match(
            $section,
            '\Adiff --git a/(?<path>[A-Za-z0-9._/-]+) b/\k<path>\n'
        )
        if (-not $header.Success) {
            throw 'E2E_BUSINESS_PATCH_UNSUPPORTED_HEADER'
        }
        $relative = $header.Groups['path'].Value
        if ($relative -cmatch '(^|/)\.\.?(/|$)') {
            throw 'E2E_BUSINESS_PATCH_UNSAFE_PATH'
        }
        if (-not $relative.StartsWith('tests/', [StringComparison]::Ordinal)) {
            $productionPaths.Add($relative)
            $productionSections.Add($section)
        }
    }
    if ($productionSections.Count -eq 0) {
        throw 'E2E_BUSINESS_PATCH_HAS_NO_PRODUCTION_FILES'
    }

    $payload = $productionSections -join ''
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $hasher.ComputeHash($strictUtf8.GetBytes($payload))
    }
    finally {
        $hasher.Dispose()
    }
    [PSCustomObject][ordered]@{
        sha256 = ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
        production_file_count = $productionPaths.Count
        total_file_count = $sections.Count
        production_files = [string[]]$productionPaths
    }
}
