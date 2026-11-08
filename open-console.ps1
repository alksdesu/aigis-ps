[CmdletBinding()]
param(
    [string]$DataDirectory = (Join-Path $PSScriptRoot 'runtime\server'),
    [switch]$PrintUrl,
    [switch]$Browser
)

$ErrorActionPreference = 'Stop'
if (-not ($Browser -or $PrintUrl)) {
    if ($PSBoundParameters.ContainsKey('DataDirectory')) {
        throw 'Choose a save in the desktop console, or use -Browser -DataDirectory for an existing web console.'
    }
    & (Join-Path $PSScriptRoot 'start-console.ps1')
    return
}
. (Join-Path $PSScriptRoot 'environment.ps1')
$null = Initialize-AigisEnvironment -ProjectRoot $PSScriptRoot
$workspaceRoot = [System.IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent)).TrimEnd('\') + '\'
$resolvedData = [System.IO.Path]::GetFullPath($DataDirectory)
if (-not $resolvedData.StartsWith($workspaceRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The data directory must remain inside this workspace.'
}
$accessFile = Join-Path $resolvedData 'admin\access.json'
if (-not (Test-Path -LiteralPath $accessFile -PathType Leaf)) {
    throw "No console access file exists at $accessFile. Start the server with the admin console enabled first."
}
$access = Get-Content -LiteralPath $accessFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($access.schema_version -ne 1) {
    throw 'The console access file has an unsupported schema version.'
}
$url = [string]$access.url
if ($url -cnotmatch '^http://(127\.0\.0\.1|\[::1\]):[0-9]{1,5}/admin/#token=[A-Za-z0-9_-]{32,256}\z') {
    throw 'The console access file does not hold a loopback console address.'
}
$ownerId = 0
if (-not [int]::TryParse([string]$access.pid, [ref]$ownerId) -or $null -eq (Get-Process -Id $ownerId -ErrorAction SilentlyContinue)) {
    throw 'The server that wrote the console access file is no longer running. Start the server again.'
}
if ($PrintUrl) {
    Write-Output $url
    return
}

function Find-Edge {
    $candidates = @()
    foreach ($root in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA)) {
        if ($root) { $candidates += Join-Path $root 'Microsoft\Edge\Application\msedge.exe' }
    }
    foreach ($key in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
                       'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe')) {
        $registered = (Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue).'(default)'
        if ($registered) { $candidates += $registered }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

$edge = Find-Edge
if (-not $edge) {
    Write-Output 'Microsoft Edge was not found. Open this address in a browser profile kept inside the workspace:'
    Write-Output $url
    return
}
$browserProfile = Join-Path $PSScriptRoot 'runtime\console-browser'
New-Item -ItemType Directory -Force -Path $browserProfile | Out-Null
$edgeArguments = @("--user-data-dir=`"$browserProfile`"", "--app=$url", '--no-first-run', '--no-default-browser-check')
Start-Process -FilePath $edge -ArgumentList $edgeArguments | Out-Null
Write-Output "The admin console opened in Microsoft Edge with the browser profile in $browserProfile."
