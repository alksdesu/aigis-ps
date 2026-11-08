[CmdletBinding()]
param(
    [ValidateSet('Check', 'Wait', 'Pid')]
    [string]$Mode = 'Check',
    [int]$ProcessId = 0,
    [ValidateRange(1, 14400)]
    [int]$Timeout = 1800,
    [int[]]$Operations = @(),
    [string]$Preset = '',
    [switch]$All,
    [string]$Executable = '',
    [string]$OutputDirectory = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $projectRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $projectRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'Project Python environment is missing.' }
if ($Mode -ne 'Pid' -and $ProcessId -ne 0) { throw 'ProcessId requires Mode Pid.' }
$scopes = @(($Operations.Count -gt 0), [bool]$Preset, $All.IsPresent) | Where-Object { $_ }
if ($scopes.Count -gt 1) { throw 'Choose only one of Operations, Preset or All.' }
if ($Mode -ne 'Check' -and $scopes.Count -eq 0) { throw 'Choose Operations, Preset or All.' }
$captureArguments = @((Join-Path $PSScriptRoot 'capture_responses.py'), '--timeout', $Timeout.ToString())
switch ($Mode) {
    'Check' { $captureArguments += '--check' }
    'Wait' { $captureArguments += '--wait' }
    'Pid' {
        if ($ProcessId -le 0) { throw 'ProcessId must be positive.' }
        $captureArguments += @('--pid', $ProcessId.ToString())
    }
}
if ($Operations.Count -gt 0) { $captureArguments += @('--operations') + ($Operations | ForEach-Object { $_.ToString() }) }
if ($Preset) { $captureArguments += @('--preset', $Preset) }
if ($All) { $captureArguments += '--all' }
if ($Executable) { $captureArguments += @('--exe', $Executable) }
if ($OutputDirectory) { $captureArguments += @('--output', $OutputDirectory) }
& $pythonPath @captureArguments
exit $LASTEXITCODE
