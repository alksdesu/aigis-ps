[CmdletBinding()]
param(
    [string]$RuntimeDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime\client'),
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $projectRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $projectRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    & (Join-Path $projectRoot 'setup.ps1')
}
$clientArguments = @('-B', (Join-Path $PSScriptRoot 'prepare_client.py'), 'launch', '--runtime', $RuntimeDirectory)
if ($DryRun) {
    $clientArguments += '--dry-run'
}
& $pythonPath @clientArguments
if ($LASTEXITCODE -ne 0) {
    throw 'Client startup checks failed. The game was not started.'
}
