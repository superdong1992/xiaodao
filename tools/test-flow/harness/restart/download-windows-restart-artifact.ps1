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
$resultState = Invoke-RestartArtifactDownload -EvidenceRoot $EvidenceRoot -Summary $restart -ArtifactProperty 'public_artifact' -Prefix 'restart-download' -BodyName 'restart-diagnosis-result.json' -ExpectedContentType 'application/json' -JsonObject
$archiveState = Invoke-RestartArtifactDownload -EvidenceRoot $EvidenceRoot -Summary $restart -ArtifactProperty 'public_result_archive' -Prefix 'restart-archive-download' -BodyName 'restart-result.zip' -ExpectedContentType 'application/zip'
$state = [PSCustomObject][ordered]@{
    schema_version = 1
    result = $resultState
    archive = $archiveState
}
Write-RestartJson -Path (Join-Path $EvidenceRoot 'restart-download-verification.json') -Value $state

Write-Output "restart_download=passed case_id=$($resultState.case_id) artifact_id=$($resultState.artifact_id) sha256=$($resultState.sha256) archive_artifact_id=$($archiveState.artifact_id) archive_sha256=$($archiveState.sha256)"
