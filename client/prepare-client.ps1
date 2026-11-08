[CmdletBinding()]
param(
    [string]$SourceDirectory = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'aigisr_exe'),
    [string]$RuntimeDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime\client'),
    [string]$ResourceDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime\resources'),
    [string]$MasterDirectory = '',
    [string]$EventPassRevivalFile = '',
    [string]$EventLoginRulesFile = '',
    [string]$PurchasedLoginRulesFile = '',
    [string]$MissionWindowsFile = '',
    [string]$CommandTextOverridesFile = '',
    [switch]$InspectOnly,
    [switch]$Refresh
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $projectRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $projectRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    & (Join-Path $projectRoot 'setup.ps1')
}
$action = if ($InspectOnly) { 'inspect' } else { 'prepare' }
$clientArguments = @('-B', (Join-Path $PSScriptRoot 'prepare_client.py'), $action, '--source', $SourceDirectory, '--runtime', $RuntimeDirectory, '--resource-dir', $ResourceDirectory)
if ($MasterDirectory) { $clientArguments += @('--master-dir', $MasterDirectory) }
if ($EventPassRevivalFile) { $clientArguments += @('--event-pass-revival', $EventPassRevivalFile) }
if ($EventLoginRulesFile) { $clientArguments += @('--event-login-rules', $EventLoginRulesFile) }
if ($PurchasedLoginRulesFile) { $clientArguments += @('--purchased-login-rules', $PurchasedLoginRulesFile) }
if ($MissionWindowsFile) { $clientArguments += @('--mission-windows', $MissionWindowsFile) }
if ($CommandTextOverridesFile) { $clientArguments += @('--command-text-overrides', $CommandTextOverridesFile) }
if ($Refresh) {
    $clientArguments += '--refresh'
}
& $pythonPath @clientArguments
if ($LASTEXITCODE -ne 0) {
    throw 'Client preparation failed. Existing files were not overwritten.'
}
