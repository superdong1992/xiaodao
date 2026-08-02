param([string]$EvidenceRoot = $PSScriptRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'windows-restart-lib.ps1')

$EvidenceRoot = [System.IO.Path]::GetFullPath($EvidenceRoot)
Assert-Restart (Test-Path -LiteralPath $EvidenceRoot -PathType Container) 'evidence directory is absent'

# Validate the full read-only handoff before reserving files or contacting HTTP.
Confirm-RestartDriverManifest $PSScriptRoot
Confirm-PreRestartJourneyManifest $EvidenceRoot
$pre = Read-PreRestartSummaryValidated $EvidenceRoot
Confirm-RestartClientSkill
$restart = Read-RestartSummaryValidated -EvidenceRoot $EvidenceRoot -PreSummary $pre

New-RestartOutputReservations -EvidenceRoot $EvidenceRoot -Names (Get-RestartDownloadOutputNames)
$state = Invoke-RestartArtifactDownload -EvidenceRoot $EvidenceRoot -Summary $restart
Write-RestartJson -Path (Join-Path $EvidenceRoot 'restart-download-verification.json') -Value $state

Write-Output "restart_download=passed case_id=$($state.case_id) artifact_id=$($state.artifact_id) sha256=$($state.sha256)"
