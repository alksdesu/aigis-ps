[CmdletBinding()]
param([switch]$Check)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $PSScriptRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Run .\setup.ps1 -IncludeDesktop first.'
}
$env:PYTHONPATH = Join-Path $PSScriptRoot 'src'
if ($Check) {
    & $pythonPath -B -m aigis_ps.desktop --check
    if ($LASTEXITCODE -ne 0) { throw 'Desktop dependencies are unavailable. Run .\setup.ps1 -IncludeDesktop.' }
    return
}
$pythonWindow = Join-Path (Split-Path $pythonPath -Parent) 'pythonw.exe'
Start-Process -FilePath $pythonWindow -ArgumentList @('-B', '-m', 'aigis_ps.desktop') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null
Write-Output 'The Aigis desktop console is opening. It does not start a game service until you choose Start.'
