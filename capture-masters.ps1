[CmdletBinding()]
param([switch]$Check, [int]$ProcessId = 0, [int]$Timeout = 600)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $PSScriptRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    & (Join-Path $PSScriptRoot 'setup.ps1') -IncludeCapture
}
$captureArguments = @((Join-Path $PSScriptRoot 'client\capture_masters.py'), '--timeout', $Timeout)
if ($Check) { $captureArguments += '--check' }
elseif ($ProcessId -gt 0) { $captureArguments += @('--pid', $ProcessId) }
else { $captureArguments += '--wait' }
& $pythonPath @captureArguments
if ($LASTEXITCODE -ne 0) { throw 'Master-data capture did not complete.' }
