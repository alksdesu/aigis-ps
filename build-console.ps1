[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    $compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
$source = Join-Path $PSScriptRoot 'client\desktop-launcher.cs'
$target = Join-Path $PSScriptRoot 'AigisConsole.exe'
& $compiler /nologo /target:winexe /optimize+ /reference:System.Windows.Forms.dll "/out:$target" $source
if ($LASTEXITCODE -ne 0) { throw 'Unable to build the desktop launcher.' }
Write-Output "Built $target"
