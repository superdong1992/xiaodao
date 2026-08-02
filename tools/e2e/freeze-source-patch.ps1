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
$pathList = Join-Path $toolRoot 'product-patch-files.txt'
$workRoot = Join-Path $RepoRoot ('.tmp\e2e-patch-freeze-' + [Guid]::NewGuid().ToString('N'))
$overlay = Join-Path $workRoot 'overlay'
$validation = Join-Path $workRoot 'validation'
$newPaths = @(
    'tests/e2e/test_real_diagnose_agent_contract_gate.py',
    'tests/e2e/test_real_route_agent_contract_gate.py'
)

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

function Get-CanonicalPatch([string]$Clone, [string[]]$Paths) {
    $lines = @(& git.exe -C $Clone -c core.autocrlf=false diff --binary --no-ext-diff -- @Paths)
    if ($LASTEXITCODE -ne 0) { throw 'E2E_PATCH_FREEZE_DIFF' }
    $text = ($lines -join "`n") + "`n"
    Assert-Freeze (-not $text.Contains("`r")) 'CR'
    $chunks = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
    $matches = [regex]::Matches($text, '(?m)^diff --git a/(.+?) b/(.+?)\n')
    Assert-Freeze ($matches.Count -eq $Paths.Count) 'CHUNK_COUNT'
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
    Assert-Freeze ($actual.Count -eq $Paths.Count) 'PATH_COUNT'
    for ($index = 0; $index -lt $Paths.Count; $index++) {
        Assert-Freeze ($actual[$index] -ceq $Paths[$index]) "PATH_SET_$index"
    }
    return [String]::Concat([string[]]@($actual | ForEach-Object { $chunks[$_] }))
}

Assert-Freeze (Test-Path -LiteralPath $RepoRoot -PathType Container) 'REPO'
Assert-Freeze (Test-Path -LiteralPath $EvidenceRoot -PathType Container) 'EVIDENCE'
Assert-Freeze (Test-Path -LiteralPath $pathList -PathType Leaf) 'PATH_LIST'
$paths = [string[]]@([IO.File]::ReadAllLines($pathList, $utf8) | Where-Object { $_ })
$paths = [string[]]@($paths | Sort-Object -Unique)
Assert-Freeze ($paths.Count -eq 32) 'EXPECTED_32_PATHS'
foreach ($relative in $paths) {
    Assert-Freeze (-not [IO.Path]::IsPathRooted($relative)) 'ROOTED_PATH'
    Assert-Freeze ($relative -cmatch '^[A-Za-z0-9._/-]+$') 'PATH_SHAPE'
    $source = Join-Path $RepoRoot ($relative.Replace('/', '\'))
    Assert-Freeze (Test-Path -LiteralPath $source -PathType Leaf) "SOURCE_$relative"
}

[IO.Directory]::CreateDirectory($workRoot) | Out-Null
try {
    Invoke-FreezeGit @('clone', '--shared', '--no-checkout', '--quiet', '--', $RepoRoot, $overlay)
    Invoke-FreezeGit @('-C', $overlay, '-c', 'core.autocrlf=false', 'checkout', '--quiet', '--detach', $BaseCommit)
    foreach ($relative in $paths) {
        $source = Join-Path $RepoRoot ($relative.Replace('/', '\'))
        $destination = Join-Path $overlay ($relative.Replace('/', '\'))
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
        [IO.File]::WriteAllBytes($destination, (Get-NormalizedBytes $source))
    }
    Invoke-FreezeGit ([string[]](@('-C', $overlay, '-c', 'core.autocrlf=false', 'add', '-N', '--') + $newPaths))
    $patchBytes = $utf8.GetBytes((Get-CanonicalPatch $overlay $paths))
    $patchPath = Join-Path $EvidenceRoot 'source-input.patch'
    Assert-Freeze (-not (Test-Path -LiteralPath $patchPath)) 'PATCH_EXISTS'
    [IO.File]::WriteAllBytes($patchPath, $patchBytes)

    Invoke-FreezeGit @('clone', '--shared', '--no-checkout', '--quiet', '--', $RepoRoot, $validation)
    Invoke-FreezeGit @('-C', $validation, '-c', 'core.autocrlf=false', 'checkout', '--quiet', '--detach', $BaseCommit)
    Invoke-FreezeGit @('-C', $validation, '-c', 'core.autocrlf=false', 'apply', '--check', '--whitespace=error-all', $patchPath)
    Invoke-FreezeGit @('-C', $validation, '-c', 'core.autocrlf=false', 'apply', '--whitespace=error-all', $patchPath)
    Invoke-FreezeGit ([string[]](@('-C', $validation, '-c', 'core.autocrlf=false', 'add', '-N', '--') + $newPaths))
    Invoke-FreezeGit @('-C', $validation, '-c', 'core.autocrlf=false', 'diff', '--check')
    $rehashBytes = $utf8.GetBytes((Get-CanonicalPatch $validation $paths))
    Assert-Freeze ([Linq.Enumerable]::SequenceEqual([byte[]]$patchBytes, [byte[]]$rehashBytes)) 'REHASH'

    $sha = (Get-FileHash -LiteralPath $patchPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $size = (Get-Item -LiteralPath $patchPath).Length
    [IO.File]::WriteAllText((Join-Path $EvidenceRoot 'source-input.patch.sha256'), "$sha  /evidence/source-input.patch`n", $utf8)
    [IO.File]::WriteAllText((Join-Path $EvidenceRoot 'source-patch-host-freeze.txt'), "source_patch_files=32`nsource_patch_bytes=$size`nsource_patch_sha256=$sha`nbase_commit=$BaseCommit`nline_ending_mode=canonical-lf`nuser_submodules=excluded`nhandoff_s08=excluded`n", $utf8)
    Write-Output "E2E_SOURCE_PATCH_FROZEN sha256=$sha bytes=$size files=32"
}
finally {
    if (Test-Path -LiteralPath $workRoot -PathType Container) {
        $allowedPrefix = [IO.Path]::GetFullPath((Join-Path $RepoRoot '.tmp\e2e-patch-freeze-'))
        $resolvedWorkRoot = [IO.Path]::GetFullPath($workRoot)
        Assert-Freeze ($resolvedWorkRoot.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) 'CLEANUP_TARGET'
        Remove-Item -LiteralPath $resolvedWorkRoot -Recurse -Force
    }
}
