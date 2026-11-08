[CmdletBinding()]
param(
    [ValidateSet('startup', 'story', 'maps', 'all')][string]$Scope = 'startup',
    [string]$ContextFile = '',
    [switch]$PlanOnly,
    [int]$Workers = 4,
    [double]$MaxGiB = 0
)

$ErrorActionPreference = 'Stop'
if ($MaxGiB -lt 0) { throw 'MaxGiB cannot be negative.' }
. (Join-Path $PSScriptRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $PSScriptRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { & (Join-Path $PSScriptRoot 'setup.ps1') }
if ($ContextFile) {
    & $pythonPath -m aigis_ps.cli resources-index $ContextFile
    if ($LASTEXITCODE -ne 0) { throw 'Unable to import the captured resource version.' }
}
if ($PlanOnly) {
    & $pythonPath -m aigis_ps.cli resources-plan
} else {
    $collectArguments = @('-m', 'aigis_ps.cli', 'resources-collect', '--scope', $Scope, '--workers', $Workers)
    if ($MaxGiB -gt 0) { $collectArguments += @('--max-gib', $MaxGiB.ToString([Globalization.CultureInfo]::InvariantCulture)) }
    & $pythonPath @collectArguments
}
if ($LASTEXITCODE -ne 0) { throw 'Resource collection did not complete; inspect the saved job state.' }
