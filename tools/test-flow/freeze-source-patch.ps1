param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$EvidenceRoot,
    [string]$BaseCommit = 'c31cc03848155d03b9a35776555e413f26b264ad'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false, $true)
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$EvidenceRoot = [IO.Path]::GetFullPath($EvidenceRoot)
$toolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$workRoot = Join-Path $RepoRoot ('.tmp\e2e-patch-freeze-' + [Guid]::NewGuid().ToString('N'))
$overlay = Join-Path $workRoot 'overlay'
$validation = Join-Path $workRoot 'validation'
$newPaths = [Collections.Generic.List[string]]::new()

function Assert-Freeze([bool]$Condition, [string]$Code) {
    if (-not $Condition) { throw "E2E_PATCH_FREEZE_$Code" }
}

function Invoke-FreezeGit([string[]]$Arguments) {
    & git.exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "E2E_PATCH_FREEZE_GIT:$($Arguments[0])" }
}

function Get-NormalizedBytes([string]$Path) {
    $text = [IO.File]::ReadAllText($Path, $utf8)
    return $utf8.GetBytes($text.Replace("`r`n", "`n").Replace("`r", "`n"))
}

function Get-FrozenBytes([string]$Path, [string]$RelativePath) {
    if ($RelativePath.EndsWith('.zip', [StringComparison]::OrdinalIgnoreCase)) {
        return [IO.File]::ReadAllBytes($Path)
    }
    return Get-NormalizedBytes $Path
}

function Get-CanonicalPatch([string]$Clone, [string[]]$Paths) {
    $lines = @(& git.exe -C $Clone -c core.autocrlf=false diff --binary --no-ext-diff --no-renames -- @Paths)
    if ($LASTEXITCODE -ne 0) { throw 'E2E_PATCH_FREEZE_DIFF' }
    $text = ($lines -join "`n") + "`n"
    Assert-Freeze (-not $text.Contains("`r")) 'CR'
    $chunks = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
    $matches = [regex]::Matches($text, '(?m)^diff --git a/(.+?) b/(.+?)\n')
    Assert-Freeze ($matches.Count -gt 0) 'CHUNK_COUNT'
    for ($index = 0; $index -lt $matches.Count; $index++) {
        $left = $matches[$index].Groups[1].Value
        $right = $matches[$index].Groups[2].Value
        Assert-Freeze ($left -ceq $right) 'PATH_PAIR'
        Assert-Freeze (-not $left.StartsWith('.tmp/open-source-baselines/', [StringComparison]::Ordinal)) 'USER_SUBMODULE'
        Assert-Freeze ($left -cne 'handoff/S08.json') 'HANDOFF'
        $end = if ($index + 1 -lt $matches.Count) { $matches[$index + 1].Index } else { $text.Length }
        $chunks.Add($left, $text.Substring($matches[$index].Index, $end - $matches[$index].Index))
    }
    $actual = [string[]]$chunks.Keys
    [Array]::Sort($actual, [StringComparer]::Ordinal)
    $allowed = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($path in $Paths) { [void]$allowed.Add($path) }
    for ($index = 0; $index -lt $actual.Count; $index++) {
        Assert-Freeze ($allowed.Contains($actual[$index])) "PATH_SET_$index"
    }
    return [String]::Concat([string[]]@($actual | ForEach-Object { $chunks[$_] }))
}

Assert-Freeze (Test-Path -LiteralPath $RepoRoot -PathType Container) 'REPO'
Assert-Freeze (Test-Path -LiteralPath $EvidenceRoot -PathType Container) 'EVIDENCE'
$trackedPaths = @(& git.exe -C $RepoRoot -c core.quotepath=false diff --no-renames --name-only $BaseCommit -- .)
Assert-Freeze ($LASTEXITCODE -eq 0) 'TRACKED_INVENTORY'
$untrackedPaths = @(& git.exe -C $RepoRoot -c core.quotepath=false ls-files --others --exclude-standard -- .)
Assert-Freeze ($LASTEXITCODE -eq 0) 'UNTRACKED_INVENTORY'
$paths = [string[]]@(
    @($trackedPaths) + @($untrackedPaths) |
        Where-Object {
            $_ -and
            -not $_.StartsWith('.tmp/', [StringComparison]::Ordinal) -and
            $_ -cne 'handoff/S08.json'
        } |
        Sort-Object -Unique
)
Assert-Freeze ($paths.Count -gt 0) 'EMPTY_ACTUAL_INVENTORY'
$allowedProductPaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
foreach ($path in $paths) { [void]$allowedProductPaths.Add($path) }
$productScopes = [string[]]@('schemas/v2', 'src/problem_locator')
$trackedProductPaths = @(
    & git.exe -C $RepoRoot diff --name-only $BaseCommit -- @productScopes
)
Assert-Freeze ($LASTEXITCODE -eq 0) 'PRODUCT_TRACKED_DIFF'
$untrackedProductPaths = @(
    & git.exe -C $RepoRoot ls-files --others --exclude-standard -- @productScopes
)
Assert-Freeze ($LASTEXITCODE -eq 0) 'PRODUCT_UNTRACKED_DIFF'
$requiredProductPathSet = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
foreach ($relative in @($trackedProductPaths) + @($untrackedProductPaths)) {
    if (-not $relative) { continue }
    Assert-Freeze ($relative -cmatch '^[A-Za-z0-9._/-]+$') 'PRODUCT_PATH_SHAPE'
    [void]$requiredProductPathSet.Add($relative)
}
$requiredProductPaths = [string[]]@($requiredProductPathSet)
[Array]::Sort($requiredProductPaths, [StringComparer]::Ordinal)
for ($index = 0; $index -lt $requiredProductPaths.Count; $index++) {
    Assert-Freeze (
        $allowedProductPaths.Contains($requiredProductPaths[$index])
    ) "PRODUCT_PATH_SET_$index`:$($requiredProductPaths[$index])"
}
$testScopes = [string[]]@('tests')
$trackedTestPaths = @(
    & git.exe -C $RepoRoot diff --name-only $BaseCommit -- @testScopes
)
Assert-Freeze ($LASTEXITCODE -eq 0) 'TEST_TRACKED_DIFF'
$untrackedTestPaths = @(
    & git.exe -C $RepoRoot ls-files --others --exclude-standard -- @testScopes
)
Assert-Freeze ($LASTEXITCODE -eq 0) 'TEST_UNTRACKED_DIFF'
$requiredTestPathSet = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
foreach ($relative in @($trackedTestPaths) + @($untrackedTestPaths)) {
    if (-not $relative) { continue }
    Assert-Freeze ($relative -cmatch '^[A-Za-z0-9._/-]+$') 'TEST_PATH_SHAPE'
    [void]$requiredTestPathSet.Add($relative)
}
$requiredTestPaths = [string[]]@($requiredTestPathSet)
[Array]::Sort($requiredTestPaths, [StringComparer]::Ordinal)
for ($index = 0; $index -lt $requiredTestPaths.Count; $index++) {
    Assert-Freeze (
        $allowedProductPaths.Contains($requiredTestPaths[$index])
    ) "TEST_PATH_SET_$index`:$($requiredTestPaths[$index])"
}
foreach ($relative in $paths) {
    Assert-Freeze (-not [IO.Path]::IsPathRooted($relative)) 'ROOTED_PATH'
    Assert-Freeze ($relative -cmatch '^[A-Za-z0-9._/-]+$') 'PATH_SHAPE'
    $source = Join-Path $RepoRoot ($relative.Replace('/', '\'))
    $baseMatches = @(& git.exe -C $RepoRoot ls-tree --name-only $BaseCommit -- $relative)
    Assert-Freeze ($LASTEXITCODE -eq 0) "BASE_LOOKUP_$relative"
    $existsAtBase = $baseMatches.Count -eq 1 -and $baseMatches[0] -ceq $relative
    $existsAtSource = Test-Path -LiteralPath $source -PathType Leaf
    Assert-Freeze ($existsAtBase -or $existsAtSource) "UNKNOWN_$relative"
    if ($existsAtSource -and -not $existsAtBase) { $newPaths.Add($relative) }
}

[IO.Directory]::CreateDirectory($workRoot) | Out-Null
try {
    Invoke-FreezeGit @('clone', '--shared', '--no-checkout', '--quiet', '--', $RepoRoot, $overlay)
    Invoke-FreezeGit @('-C', $overlay, '-c', 'core.autocrlf=false', 'checkout', '--quiet', '--detach', $BaseCommit)
    foreach ($relative in $paths) {
        $source = Join-Path $RepoRoot ($relative.Replace('/', '\'))
        $destination = Join-Path $overlay ($relative.Replace('/', '\'))
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
            [IO.File]::WriteAllBytes($destination, (Get-FrozenBytes $source $relative))
        }
        else {
            $overlayPrefix = [IO.Path]::GetFullPath($overlay).TrimEnd('\') + '\'
            $resolvedDestination = [IO.Path]::GetFullPath($destination)
            Assert-Freeze ($resolvedDestination.StartsWith($overlayPrefix, [StringComparison]::OrdinalIgnoreCase)) 'DELETE_SCOPE'
            Assert-Freeze (Test-Path -LiteralPath $resolvedDestination -PathType Leaf) "DELETE_SOURCE_$relative"
            Remove-Item -LiteralPath $resolvedDestination -Force
        }
    }
    if ($newPaths.Count -gt 0) {
        Invoke-FreezeGit ([string[]](@('-C', $overlay, '-c', 'core.autocrlf=false', 'add', '-N', '--') + $newPaths.ToArray()))
    }
    $patchBytes = $utf8.GetBytes((Get-CanonicalPatch $overlay $paths))
    $patchFileCount = [regex]::Matches($utf8.GetString($patchBytes), '(?m)^diff --git ').Count
    $patchPath = Join-Path $EvidenceRoot 'source-input.patch'
    Assert-Freeze (-not (Test-Path -LiteralPath $patchPath)) 'PATCH_EXISTS'
    [IO.File]::WriteAllBytes($patchPath, $patchBytes)

    Invoke-FreezeGit @('clone', '--shared', '--no-checkout', '--quiet', '--', $RepoRoot, $validation)
    Invoke-FreezeGit @('-C', $validation, '-c', 'core.autocrlf=false', 'checkout', '--quiet', '--detach', $BaseCommit)
    Invoke-FreezeGit @('-C', $validation, '-c', 'core.autocrlf=false', 'apply', '--check', '--whitespace=error-all', $patchPath)
    Invoke-FreezeGit @('-C', $validation, '-c', 'core.autocrlf=false', 'apply', '--whitespace=error-all', $patchPath)
    if ($newPaths.Count -gt 0) {
        Invoke-FreezeGit ([string[]](@('-C', $validation, '-c', 'core.autocrlf=false', 'add', '-N', '--') + $newPaths.ToArray()))
    }
    Invoke-FreezeGit @('-C', $validation, '-c', 'core.autocrlf=false', 'diff', '--check')
    $rehashBytes = $utf8.GetBytes((Get-CanonicalPatch $validation $paths))
    Assert-Freeze ([Linq.Enumerable]::SequenceEqual([byte[]]$patchBytes, [byte[]]$rehashBytes)) 'REHASH'

    $sha = (Get-FileHash -LiteralPath $patchPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $size = (Get-Item -LiteralPath $patchPath).Length
    [IO.File]::WriteAllText((Join-Path $EvidenceRoot 'source-input.patch.sha256'), "$sha  /evidence/source-input.patch`n", $utf8)
    [IO.File]::WriteAllText((Join-Path $EvidenceRoot 'source.patch.new-files.txt'), (($newPaths.ToArray() -join "`n") + $(if ($newPaths.Count -gt 0) { "`n" } else { "" })), $utf8)
    [IO.File]::WriteAllText((Join-Path $EvidenceRoot 'source-patch-host-freeze.txt'), "source_patch_files=$patchFileCount`nsource_patch_inventory_files=$($paths.Count)`nsource_patch_new_files=$($newPaths.Count)`nsource_patch_bytes=$size`nsource_patch_sha256=$sha`nbase_commit=$BaseCommit`nline_ending_mode=text-canonical-lf,binary-zip-exact`nidentity_inventory=actual-tracked-plus-untracked`nproduction_path_completeness=tracked-plus-untracked`nproduction_path_count=$($requiredProductPaths.Count)`nproduction_path_scopes=schemas/v2,src/problem_locator`ntest_path_completeness=tracked-plus-untracked`ntest_path_count=$($requiredTestPaths.Count)`ntest_path_scope=tests`nuser_submodules=excluded`nhandoff_s08=excluded`n", $utf8)
    Write-Output "E2E_SOURCE_PATCH_FROZEN sha256=$sha bytes=$size files=$patchFileCount inventory=$($paths.Count) new_files=$($newPaths.Count)"
}
finally {
    if (Test-Path -LiteralPath $workRoot -PathType Container) {
        $allowedPrefix = [IO.Path]::GetFullPath((Join-Path $RepoRoot '.tmp\e2e-patch-freeze-'))
        $resolvedWorkRoot = [IO.Path]::GetFullPath($workRoot)
        Assert-Freeze ($resolvedWorkRoot.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) 'CLEANUP_TARGET'
        Remove-Item -LiteralPath $resolvedWorkRoot -Recurse -Force
    }
}
