[CmdletBinding()]
param(
    [ValidateSet('Check', 'Wait', 'Pid')]
    [string]$Mode = 'Check',
    [int]$ProcessId = 0,
    [ValidateRange(1, 3600)]
    [int]$Timeout = 900,
    [string]$Executable = '',
    [string]$OutputDirectory = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $projectRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $projectRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'Project Python environment is missing.' }
if ($Mode -ne 'Pid' -and $ProcessId -ne 0) { throw 'ProcessId requires Mode Pid.' }
$captureArguments = @((Join-Path $PSScriptRoot 'capture_schedule.py'), '--timeout', $Timeout.ToString())
switch ($Mode) {
    'Check' { $captureArguments += '--check' }
    'Wait' { $captureArguments += '--wait' }
    'Pid' {
        if ($ProcessId -le 0) { throw 'ProcessId must be positive.' }
        $captureArguments += @('--pid', $ProcessId.ToString())
    }
}
if ($Executable) { $captureArguments += @('--exe', $Executable) }
if ($OutputDirectory) { $captureArguments += @('--output', $OutputDirectory) }
& $pythonPath @captureArguments
exit $LASTEXITCODE
