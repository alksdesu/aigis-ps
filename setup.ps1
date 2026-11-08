[CmdletBinding()]
param([switch]$IncludeCapture, [switch]$IncludeTests, [switch]$IncludeDesktop)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $PSScriptRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    $basePython = (Get-Command python -ErrorAction Stop).Source
    & $basePython -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the project Python environment.' }
}
$extras = @()
if ($IncludeCapture) { $extras += 'capture' }
if ($IncludeTests) { $extras += 'test' }
if ($IncludeDesktop) { $extras += 'desktop' }
$package = $PSScriptRoot
if ($extras.Count -gt 0) { $package += '[' + ($extras -join ',') + ']' }
$installArguments = @('-m', 'pip', 'install', '-e', $package)
$constraints = Join-Path $PSScriptRoot 'requirements.lock'
if (Test-Path -LiteralPath $constraints -PathType Leaf) { $installArguments += @('-c', $constraints) }
& $pythonPath @installArguments
if ($LASTEXITCODE -ne 0) { throw 'Unable to install project dependencies.' }

