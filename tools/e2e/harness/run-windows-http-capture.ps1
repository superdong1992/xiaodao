param(
    [Parameter(Mandatory = $true)][string]$EvidenceRoot,
    [Parameter(Mandatory = $true)][ValidateSet('Before', 'After')][string]$Phase
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'windows-http-capture-lib.ps1')

$EvidenceRoot = [System.IO.Path]::GetFullPath($EvidenceRoot)
Confirm-HcEvidenceRoot $EvidenceRoot

# This self-integrity check and all phase input checks happen before output
# reservation and before the only allowed network process, loopback curl.exe.
Confirm-HcDriverManifest $PSScriptRoot

if ($Phase -ceq 'Before') {
    $result = Invoke-HcBeforePhase -EvidenceRoot $EvidenceRoot
}
else {
    $result = Invoke-HcAfterPhase -EvidenceRoot $EvidenceRoot
}

Write-Output "http_capture=$($result.phase) status=passed case_id=$($result.case_id) public_artifact_id=$($result.artifact_id) public_sha256=$($result.sha256) archive_artifact_id=$($result.archive_artifact_id) archive_sha256=$($result.archive_sha256)"
if ($Phase -ceq 'After') {
    Write-Output "internal_artifact_isolation=passed artifact_id=$($result.internal_artifact_id) http_code=$($result.internal_http_code)"
}
