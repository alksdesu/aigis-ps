function Initialize-AigisEnvironment {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot)

    $resolvedRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
    $env:TEMP = Join-Path $resolvedRoot 'tmp'
    $env:TMP = $env:TEMP
    $env:PIP_CACHE_DIR = Join-Path $resolvedRoot '.cache\pip'
    $env:PYTHONPYCACHEPREFIX = Join-Path $resolvedRoot '.cache\pycache'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:PYTHONUTF8 = '1'
    foreach ($directory in @($env:TEMP, $env:PIP_CACHE_DIR, $env:PYTHONPYCACHEPREFIX)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    return (Join-Path $resolvedRoot '.venv\Scripts\python.exe')
}

