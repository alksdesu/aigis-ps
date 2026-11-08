[CmdletBinding()]
param(
    [ValidateSet('Check', 'Wait')]
    [string]$Mode = 'Check',
    [ValidateRange(1, 14400)]
    [int]$Timeout = 7200,
    [ValidateRange(1, 3600)]
    [int]$ContentTimeout = 600,
    [string]$Executable = '',
    [string]$OutputDirectory = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $projectRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $projectRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'Project Python environment is missing.' }
$captureArguments = @((Join-Path $PSScriptRoot 'capture_all.py'), '--timeout', $Timeout.ToString(),
    '--content-timeout', $ContentTimeout.ToString())
switch ($Mode) {
    'Check' { $captureArguments += '--check' }
    'Wait' { $captureArguments += '--wait' }
}
if ($Executable) { $captureArguments += @('--exe', $Executable) }
if ($OutputDirectory) { $captureArguments += @('--output', $OutputDirectory) }
& $pythonPath @captureArguments
exit $LASTEXITCODE
