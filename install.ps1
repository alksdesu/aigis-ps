[CmdletBinding()]
param([switch]$Check, [switch]$NoShortcut, [switch]$NoLaunch)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$projectRoot = $PSScriptRoot
$workspace = Split-Path $projectRoot -Parent
$clientDirectory = Join-Path $workspace 'aigisr_exe'
$clientExecutable = Join-Path $clientDirectory 'Aigis_r.exe'
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$masterDirectory = Join-Path $projectRoot 'runtime\server\masterdata'
$clientManifest = Join-Path $projectRoot 'runtime\client\client-manifest.json'
$consoleExecutable = Join-Path $projectRoot 'AigisConsole.exe'
$consoleFallback = Join-Path $projectRoot 'start.cmd'
$logPath = Join-Path $projectRoot 'runtime\install.log'
$pythonVersion = '3.12'
$pythonPackage = 'Python.Python.3.12'
$webViewPackage = 'Microsoft.EdgeWebView2Runtime'
$webViewClient = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$dmmConfig = Join-Path $env:APPDATA 'dmmgameplayer5\dmmgame.cnf'
$dmmProduct = 'aigisr_exe'
$gameProcess = 'Aigis_r'
$spareBytes = 2GB
$shortcutName = 'Aigis 私服.lnk'
$totalSteps = 8
$script:currentStep = 0

function Write-Step([string]$Text) {
    $script:currentStep++
    Write-Host ("[{0}/{1}] {2}" -f $script:currentStep, $totalSteps, $Text) -ForegroundColor Cyan
}

function Write-Note([string]$Text) { Write-Host "      $Text" }

function Write-Planned([string]$Text) { Write-Host "      [检查模式] $Text" -ForegroundColor Yellow }

function Invoke-Quiet([string]$Path, [string[]]$Arguments) {
    # Windows PowerShell 在 Stop 模式下会把被重定向的原生 stderr 当作终止错误
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Path @Arguments 2>$null
        return [pscustomobject]@{ Code = $LASTEXITCODE; Output = ("$output").Trim() }
    } catch {
        return [pscustomobject]@{ Code = -1; Output = '' }
    }
}

function Get-FreeBytes([string]$Path) {
    return [IO.DriveInfo]::new([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Path))).AvailableFreeSpace
}

function Format-Size([double]$Bytes) { return '{0:N1} GB' -f ($Bytes / 1GB) }

function Assert-Space([double]$Needed, [string]$Purpose) {
    $free = Get-FreeBytes $workspace
    if ($free -lt $Needed + $spareBytes) {
        throw "$Purpose 需要 $(Format-Size $Needed)，另需保留 $(Format-Size $spareBytes)；当前只剩 $(Format-Size $free)。"
    }
}

function Assert-GameStopped {
    if (@(Get-Process -Name $gameProcess -ErrorAction SilentlyContinue).Count) {
        throw '游戏正在运行，请先退出原版和本地游戏后重新运行。'
    }
}

function Test-Interpreter([string]$Path) {
    if (-not $Path -or $Path -like '*\WindowsApps\*' -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $probe = Invoke-Quiet $Path @('-c', "import struct,sys;print('%d.%d-%d' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8))")
    return $probe.Code -eq 0 -and $probe.Output -eq "$pythonVersion-64"
}

function Find-Python {
    $candidates = [Collections.Generic.List[string]]::new()
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $probe = Invoke-Quiet $launcher.Source @("-$pythonVersion", '-c', 'import sys;print(sys.executable)')
        if ($probe.Code -eq 0) { $candidates.Add($probe.Output) }
    }
    foreach ($command in @(Get-Command python -All -ErrorAction SilentlyContinue)) { $candidates.Add($command.Source) }
    # winget 装完后当前窗口的 PATH 尚未刷新
    $candidates.Add((Join-Path $env:LOCALAPPDATA ('Programs\Python\Python' + $pythonVersion.Replace('.', '') + '\python.exe')))
    foreach ($candidate in $candidates) {
        if (Test-Interpreter $candidate) { return $candidate }
    }
    return $null
}

function Test-WebView2 {
    $keys = @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$webViewClient",
              "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$webViewClient",
              "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$webViewClient")
    foreach ($key in $keys) {
        $item = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
        if ($null -ne $item -and $item.PSObject.Properties['pv'] -and $item.pv -and $item.pv -ne '0.0.0.0') { return $true }
    }
    return $false
}

function Install-Package([string]$Id, [string]$Name, [switch]$User) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { throw "没有找到 winget，请手动安装 $Name 后重新运行。" }
    Write-Note "正在用 winget 安装 $Name…"
    $arguments = @('install', '--id', $Id, '--exact', '--silent', '--accept-package-agreements', '--accept-source-agreements')
    if ($User) { $arguments += @('--scope', 'user') }
    & $winget.Source @arguments | Out-Host
}

function Find-DmmClient {
    if (-not (Test-Path -LiteralPath $dmmConfig -PathType Leaf)) { return $null }
    try { $config = Get-Content -LiteralPath $dmmConfig -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
    foreach ($entry in @($config.contents)) {
        try {
            if ($entry.productId -eq $dmmProduct -and $entry.detail.path) { return [string]$entry.detail.path }
        } catch {
            continue
        }
    }
    return $null
}

function Get-SupportedHash {
    $contracts = Join-Path $projectRoot 'src\aigis_ps\data\protocol-contracts.json'
    return (Get-Content -LiteralPath $contracts -Raw -Encoding UTF8 | ConvertFrom-Json).input_sha256
}

function Test-Supported([string]$Directory) {
    $executable = Join-Path $Directory 'Aigis_r.exe'
    return (Test-Path -LiteralPath $executable -PathType Leaf) -and
        (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash -eq (Get-SupportedHash)
}

function Assert-Supported([string]$Directory) {
    if (-not (Test-Supported $Directory)) {
        $actual = (Get-FileHash -LiteralPath (Join-Path $Directory 'Aigis_r.exe') -Algorithm SHA256).Hash
        throw "原版版本不受支持：$actual。当前支持 $(Get-SupportedHash)，新版适配见 changelog.md。"
    }
    Write-Note '版本与项目配套。'
}

function Copy-Client([string]$Source, [switch]$Mirror) {
    $bytes = (Get-ChildItem -LiteralPath $Source -Recurse -File -Force | Measure-Object Length -Sum).Sum
    Write-Note "DMM 安装位置：$Source（$(Format-Size $bytes)）"
    Assert-Supported $Source
    if ($Check) { Write-Planned "将同步到 $clientDirectory，之后 DMM 更新游戏不会影响私服。"; return }
    Assert-GameStopped
    Assert-Space $bytes '复制原版'
    $mode = if ($Mirror) { '/MIR' } else { '/E' }
    & robocopy $Source $clientDirectory $mode /COPY:DAT /DCOPY:T /R:2 /W:2 /NFL /NDL /NJH /NP | Out-Host
    if ($LASTEXITCODE -ge 8) { throw "复制原版失败（robocopy $LASTEXITCODE）。" }
    Assert-Supported $clientDirectory
}

function Test-SameDirectory([string]$First, [string]$Second) {
    $normalize = { param($Path) [IO.Path]::GetFullPath($Path).TrimEnd('\') }
    return (& $normalize $First) -ieq (& $normalize $Second)
}

function Get-ResourceSummary {
    . (Join-Path $projectRoot 'environment.ps1')
    $null = Initialize-AigisEnvironment -ProjectRoot $projectRoot
    $planText = & $venvPython -m aigis_ps.cli resources-plan | Out-String
    if ($LASTEXITCODE -ne 0) { throw '无法统计需要下载的资源。' }
    return ($planText | ConvertFrom-Json).summary
}

function Unblock-ProjectFiles {
    $roots = @('client', 'config', 'src') | ForEach-Object { Join-Path $projectRoot $_ }
    @(Get-ChildItem -LiteralPath $projectRoot -File) + @($roots | ForEach-Object { Get-ChildItem -LiteralPath $_ -File -Recurse }) |
        Unblock-File -ErrorAction SilentlyContinue
}

function Step-Python {
    Write-Step 'Python 环境'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        Write-Note '项目环境 .venv 已存在。'
        return
    }
    $python = Find-Python
    if (-not $python) {
        if ($Check) { Write-Planned "将用 winget 安装 Python $pythonVersion（当前用户，无需管理员）。"; return }
        Install-Package $pythonPackage "Python $pythonVersion" -User
        $python = Find-Python
        if (-not $python) { throw "Python $pythonVersion 安装后仍未找到，请重开窗口后重新运行。" }
    }
    Write-Note "使用 $python"
    if ($Check) { Write-Planned '将创建项目环境 .venv。'; return }
    & $python -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '无法创建项目环境 .venv。' }
}

function Step-WebView2 {
    Write-Step 'WebView2 运行库'
    if (Test-WebView2) { Write-Note '已安装。'; return }
    if ($Check) { Write-Planned '将用 winget 安装 WebView2 Runtime。'; return }
    Install-Package $webViewPackage 'WebView2 Runtime'
    if (-not (Test-WebView2)) { throw 'WebView2 Runtime 安装后仍未检测到。' }
}

function Step-Client {
    Write-Step '原版客户端'
    $source = Find-DmmClient
    $installed = $source -and (Test-Path -LiteralPath (Join-Path $source 'Aigis_r.exe') -PathType Leaf)
    if (Test-Path -LiteralPath $clientExecutable -PathType Leaf) {
        Write-Note "使用 $clientDirectory"
        if ((Test-Supported $clientDirectory) -or -not $installed -or (Test-SameDirectory $source $clientDirectory) -or -not (Test-Supported $source)) {
            Assert-Supported $clientDirectory
            return
        }
        Write-Note '这份原版不是当前支持的版本，用 DMM 里的新版覆盖。'
        Copy-Client $source -Mirror
        return
    }
    if (Test-Path -LiteralPath $clientDirectory) {
        throw "$clientDirectory 已存在但没有 Aigis_r.exe；请移走它，或放入完整的原版目录。"
    }
    if (-not $installed) {
        throw '没有找到原版。请用 DMM GAME PLAYER 安装「千年戦争アイギス R」并启动一次完成更新，退出后重新运行。'
    }
    Copy-Client $source
}

function Step-Dependencies {
    Write-Step '项目依赖'
    if ($Check) {
        try {
            & (Join-Path $projectRoot 'start-console.ps1') -Check
        } catch {
            Write-Planned '将安装或补全桌面依赖。'
        }
        return
    }
    Unblock-ProjectFiles
    & (Join-Path $projectRoot 'setup.ps1') -IncludeDesktop
    & (Join-Path $projectRoot 'start-console.ps1') -Check
}

function Step-Resources {
    Write-Step '游戏资源'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf) -or -not (Test-Path -LiteralPath $clientExecutable -PathType Leaf)) {
        Write-Planned '装好依赖和原版后统计并下载。'
        return
    }
    try {
        $summary = Get-ResourceSummary
    } catch {
        if ($Check) { Write-Planned '装好依赖后统计并下载。'; return }
        throw
    }
    # 本地副本会把整套资源再复制一份，下载前一并预留，避免下完才发现空间不够
    $copyBytes = if (Test-Path -LiteralPath $clientManifest -PathType Leaf) { 0 } else { $summary.total_bytes }
    $neededBytes = $summary.download_bytes + $copyBytes
    Write-Note "共 $($summary.total) 项，需下载 $($summary.download) 项（$(Format-Size $summary.download_bytes)）；连同本地副本需要 $(Format-Size $neededBytes)。"
    if ($Check) {
        Write-Note "当前可用 $(Format-Size (Get-FreeBytes $workspace))。"
        if ($summary.download) { Write-Planned '将下载缺少的资源，中断后重新运行会接着下载。' }
        return
    }
    Assert-Space $neededBytes '下载资源并准备本地副本'
    & (Join-Path $projectRoot 'collect-resources.ps1') -Scope all
}

function Step-LocalClient {
    Write-Step '本地游戏副本'
    if ($Check) {
        $state = if (Test-Path -LiteralPath $clientManifest -PathType Leaf) { '已有副本，运行时会核对是否最新' } else { '将生成 runtime\client' }
        Write-Planned "$state。"
        return
    }
    Assert-GameStopped
    & (Join-Path $projectRoot 'client\prepare-client.ps1') -MasterDirectory $masterDirectory
}

function Step-Console {
    Write-Step '桌面控制台'
    if ($Check) {
        $state = if (Test-Path -LiteralPath $consoleExecutable -PathType Leaf) { '已有 AigisConsole.exe' } else { '将生成 AigisConsole.exe' }
        Write-Planned "$state$(if (-not $NoShortcut) { '；将在桌面创建快捷方式' })。"
        return
    }
    try {
        & (Join-Path $projectRoot 'build-console.ps1') | Out-Host
    } catch {
        Write-Note "生成 AigisConsole.exe 失败，改用 start.cmd 启动：$($_.Exception.Message)"
    }
    $target = if (Test-Path -LiteralPath $consoleExecutable -PathType Leaf) { $consoleExecutable } else { $consoleFallback }
    if (-not $NoShortcut) {
        $link = Join-Path ([Environment]::GetFolderPath('Desktop')) $shortcutName
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
        $shortcut.TargetPath = $target
        $shortcut.WorkingDirectory = $projectRoot
        $shortcut.Save()
        Write-Note "桌面快捷方式：$link"
    }
    return $target
}

function Step-Finish([string]$Target) {
    Write-Step '完成'
    if ($Check) { Write-Note '检查完成：没有安装、下载或复制任何东西。'; return }
    Write-Note '在控制台里：创建存档 → 启动服务 → 启动游戏。'
    if (-not $NoLaunch) { Start-Process -FilePath $Target -WorkingDirectory $projectRoot | Out-Null }
}

New-Item -ItemType Directory -Force -Path (Split-Path $logPath -Parent) | Out-Null
Start-Transcript -LiteralPath $logPath -Force | Out-Null
try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw '需要 64 位 Windows。' }
    Step-Python
    Step-WebView2
    Step-Client
    Step-Dependencies
    Step-Resources
    Step-LocalClient
    $target = Step-Console
    Step-Finish $target
    $exitCode = 0
} catch {
    Write-Host ''
    Write-Host "安装没有完成：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "问题解决后重新运行 install.cmd，已完成的步骤会跳过。日志：$logPath"
    $exitCode = 1
} finally {
    Stop-Transcript | Out-Null
}
exit $exitCode
