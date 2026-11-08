[CmdletBinding()]
param(
    [ValidateSet('Check', 'Wait', 'Pid')]
    [string]$Mode = 'Check',
    [int]$ProcessId = 0,
    [ValidateRange(1, 3600)]
    [int]$Timeout = 600,
    [string]$Executable = '',
    [string]$OutputDirectory = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $projectRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $projectRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Project Python environment is missing.'
}
if ($Mode -ne 'Pid' -and $ProcessId -ne 0) {
    throw 'ProcessId requires Mode Pid.'
}
$collectorArgs = @((Join-Path $PSScriptRoot 'capture_content.py'), '--timeout', $Timeout.ToString())
switch ($Mode) {
    'Check' { $collectorArgs += '--check' }
    'Wait' { $collectorArgs += '--wait' }
    'Pid' {
        if ($ProcessId -le 0) { throw 'ProcessId must be positive.' }
        $collectorArgs += @('--pid', $ProcessId.ToString())
    }
}
if ($Executable) { $collectorArgs += @('--exe', $Executable) }
if ($OutputDirectory) { $collectorArgs += @('--output', $OutputDirectory) }
& $pythonPath @collectorArgs
exit $LASTEXITCODE
