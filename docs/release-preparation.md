# 维护者：公共资料与版本发布

本文面向维护者，用于采集公共主表、准备资源版本索引，以及适配和验证新客户端。GitHub 保留源码、工具、文档和当前版本的 17 个公共种子文件，不放游戏本体、大型缓存或玩家数据。普通玩家按 [README](../README.md) 下载受支持的原版，在本机收集资源并准备副本；不需要采集或导入主表。

下面所有命令都在项目根目录 `aigis_ps` 的 PowerShell 中运行。玩家通过官方 DMM GAME PLAYER 下载原版；只有维护者采集或更新公共资料时需要使用下面的采集流程。已有存档环境更新时，应先退出游戏、停止服务并核对当前存档的主表与配置；下述初次准备命令不能代替已有存档的升级流程。

当前版本的公共主表、资源索引和固定启动资源由维护者提供，文件范围见下文的 GitHub 清单。它们是版本匹配的输入资料，不包含完整游戏资源库，也不创建玩家存档。

## 从原版准备配套资源

### 1. 安装环境和原版游戏

1. 安装 64 位 Python。项目要求 Python 3.12 或更新版本，目前按 Python 3.12 环境验证。可从 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/)获取；安装器如提供 **Add python.exe to PATH**，请勾选。安装完成后重新打开 PowerShell。
2. 安装 [Microsoft WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)；64 位 Windows 可选择 Evergreen Standalone Installer 的 x64 版本。它用于显示独立控制台窗口。
3. 使用能正常登录游戏的 DMM 账号，通过官方 DMM GAME PLAYER 安装 **千年戦争アイギス R for GAME PLAYER**。可参考[官方 PC 版发布说明](https://aigis1000.jp/notification/detail/id/10864)。从 DMM 正常启动一次，等待游戏更新和基础资源下载完成，然后退出游戏。

原版更新后可能已超出本项目支持范围。后续检查若报告客户端指纹不匹配，需先适配对应版本，不能靠改文件名或绕过检查继续。

客户端、下载资源和本地副本会占用数十 GB；请放在空间充足的磁盘。副本准备工具会计算需复制的容量，并要求复制后至少剩余 2 GiB。不要把项目放在压缩包内部运行。

### 2. 摆好目录，打开 PowerShell

选择一个工作目录，例如 `D:\AigisLocal`，按以下结构放置：

```text
D:\AigisLocal\
├─ aigis_ps\                 ← 本项目，里面能找到 setup.ps1
│  ├─ src\
│  ├─ client\
│  ├─ config\
│  ├─ runtime\resources\native-bootstrap\  ← 默认附带的固定启动资源
│  └─ setup.ps1
└─ aigisr_exe\               ← 整个原版客户端目录，包括下载的缓存
   └─ Aigis_r.exe
```

盘符和最外层文件夹名称可以不同，但当前桌面入口要求原版目录名为 `aigisr_exe`，且与 `aigis_ps` 同级。保留原版完整目录，不要只复制 EXE，也不要使用目录链接代替。如果原版安装在别处，可在原版退出后复制完整目录；后续采集时仍应启动那份实际由 DMM 使用的原版，见第 4 步。

在资源管理器进入 **`aigis_ps` 文件夹**，点击地址栏，输入 `powershell` 并回车。下面所有命令都在这个窗口中逐段执行；每段成功后再进行下一段。命令前的 `.\` 表示当前文件夹，不需要删掉。

先检查位置和 Python：

```powershell
Get-Location
Test-Path .\setup.ps1
python --version
```

应看到当前目录以 `aigis_ps` 结尾、第二条结果是 `True`，以及 `Python 3.12.x` 或更新版本。如果 `python` 无法识别或打开 Microsoft Store，请先修复 Python 安装和 PATH。

如果 PowerShell 提示“禁止运行脚本”，可仅对当前窗口执行，再重试原命令：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

关闭此窗口后该设置失效。不要在步骤报错后继续执行后面的命令。

### 3. 安装项目依赖并生成控制台

```powershell
.\setup.ps1 -IncludeCapture -IncludeDesktop
.\build-console.ps1
.\start-console.ps1 -Check
```

第一条会创建本项目的 `.venv` 并安装服务、采集和桌面依赖，需要联网。第二条使用 Windows .NET Framework 的 C# 编译器生成 `AigisConsole.exe`。第三条只检查桌面依赖，**不会启动服务，也不会创建玩家存档**。

`setup.ps1` 不会安装 Python、WebView2 或游戏。如果编译器不存在，需安装或修复 Windows .NET Framework 4.x 环境后重试构建。项目的 `.venv` 应在本机生成，不要从另一台电脑直接搬过来使用。

### 4. 采集公共主表和资源版本

此步骤只采集公开角色表、任务表和资源版本，不要求有旧存档，不需要抽卡或逐个进入玩法。

先检查默认位置的原版客户端：

```powershell
.\client\capture-content.ps1 -Mode Check
```

检查成功后，先确认原版已经退出，再开始等待：

```powershell
.\client\capture-content.ps1 -Mode Wait
```

保持 PowerShell 开着，**随后从 DMM GAME PLAYER 启动新的原版游戏进程**。正常登录、等待资源加载；采集齐全后工具会自动结束，默认最多等待 10 分钟。

如果 DMM 实际启动的原版在其他目录，上面两条命令都要加上它的真实 EXE 路径，例如：

```powershell
.\client\capture-content.ps1 -Mode Check -Executable 'D:\DMMGames\aigisr_exe\Aigis_r.exe'
.\client\capture-content.ps1 -Mode Wait -Executable 'D:\DMMGames\aigisr_exe\Aigis_r.exe'
```

这些命令是默认两条命令的替代写法。用于运行本地服务的同级 `aigisr_exe` 目录仍需与采集的原版版本、缓存一致；复制后原版又更新时，应重新同步并检查。

采集结果位于 `runtime\captures\content-日期时间-…`，其中应有：

```text
content-…\
├─ bundle.json
├─ resource-context.json
└─ masters\
   └─ 采集ID\
      ├─ manifest.json
      └─ …
```

记录本次 **`content-…` 文件夹的完整路径**，不要选 `masters` 子目录。只有 `bundle.json` 中 `complete` 为 `true` 才能继续；失败或超时的目录不能当作完整结果使用。采集完成后退出原版游戏。

如果刚才通过 `-Executable` 采集的是另一位置的原版，请在退出原版后、进行第 5 步之前，把该原版的完整目录（包括 `.cache` 缓存）同步到项目同级的 `aigisr_exe`。不要只复制更新后的 EXE。

### 5. 导入公共主表并下载其余资源

在同一个 PowerShell 窗口执行下面这段。提示输入路径时，粘贴上一步的 `content-…` 完整路径，不带引号：

```powershell
$captureDirectory = Read-Host '请输入本次 content-... 文件夹的完整路径，不带引号'
$captureBundle = Get-Content -LiteralPath (Join-Path $captureDirectory 'bundle.json') -Raw | ConvertFrom-Json
if ($captureBundle.complete -ne $true) { throw '本次采集不完整，请重新采集。' }
$masterCaptureDirectory = Split-Path -Parent (Join-Path $captureDirectory $captureBundle.master_manifest)
.\.venv\Scripts\python.exe -m aigis_ps.cli import-masters $masterCaptureDirectory
if ($LASTEXITCODE -ne 0) { throw '主表导入失败，请先处理上面的错误。' }
.\collect-resources.ps1 -ContextFile (Join-Path $captureDirectory 'resource-context.json') -Scope all
```

主表导入成功会输出 `activated: true`。资源收集需要联网，耗时取决于缓存和网速；确认最终结果为 `status: complete`、`failed: []`（失败列表为空）。中断后可重新执行收集命令，已有且校验通过的资源会复用。

这里的 `runtime/server/masterdata` 只是**首档需要的公共资料来源**，导入不会创建玩家数据库或自动登记存档。资源收集会复用原版缓存，因此“收集完成”表示原版目录与 `runtime/resources` 合起来具备所需资源；仍应保留原版目录。

### 6. 确认默认附带的固定启动资源

固定启动资源随项目默认附带，**不需要用户自行寻找、下载或放入**。完整解压项目后，以下位置应已包含这些文件：

```text
aigis_ps\runtime\resources\native-bootstrap\
├─ manifest.json
├─ LoadIcon.aar
├─ MainFontFix.aft
├─ OverlayWindow.atx
├─ SoundSeFix.aar
├─ FixSeMenuOK.mp3
├─ FixSeMenuOK.ogg
└─ GameInfoText.bin
```

其中是 7 个资源文件和 1 份配套清单，已与支持的客户端版本配套。保留原样即可；第 5 步的常规资源收集用于准备其余资源。

需要确认文件完整性时，可在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -B -c "from pathlib import Path; from aigis_ps.masterdata import MasterDataStore; from aigis_ps.native_cdn import load_bootstrap; objects, info = load_bootstrap(Path('runtime/resources/native-bootstrap'), MasterDataStore.for_data_dir(Path('runtime/server')).expected_hash); print('固定启动资源校验通过：', len(objects), '+ 1')"
```

成功会显示“固定启动资源校验通过：6 + 1”。这条检查只读取公共资源，不打开玩家数据库。若缺文件或校验失败，从同版本项目包重新解压 `runtime/resources/native-bootstrap` 后再校验，保持资源正文与清单配套。

### 7. 准备本地游戏副本

确认原版和本地游戏都已退出，先检查准备条件：

```powershell
.\client\prepare-client.ps1 -MasterDirectory .\runtime\server\masterdata -InspectOnly
```

检查成功后，再执行实际准备：

```powershell
.\client\prepare-client.ps1 -MasterDirectory .\runtime\server\masterdata
```

工具会校验支持版本、公共主表、资源及磁盘空间，然后把本地游戏副本放到 `runtime/client`。完成后应有 `runtime/client/Aigis_r.exe` 和 `runtime/client/client-manifest.json`；复制过程中不要开游戏或移动文件。

后面玩本地存档时，从控制台点击「启动游戏」，使用的是这个副本。原版由 DMM 启动，用于官方更新和必要的公共资料采集。不要混用两者的启动入口。

## GitHub 保留哪些文件

[.gitignore](../.gitignore) 对 `runtime` 默认忽略，只精确放行当前版本的 17 个公共种子文件。目录例外仅用于到达这些文件，不能改成放行整个 `runtime`、主表快照目录或资源目录。

| 内容 | 当前精确范围 | 文件数 |
|---|---|---|
| 公共主表 | `runtime/server/masterdata/active.json`；`snapshots/9159442edcc59db92d37856e/` 下的 `manifest.json`、`CardData.xml`、`CardData.source.xml`、`QuestTable.xml`、`QuestTable.source.xml` | 6 |
| 资源索引 | `runtime/resources/active-index.json`；`indexes/86164496048d9b4329259cfb248a9d3ff283bb36-6f2aabd8a946e831/` 下的 `manifest.json`、`index.bin` | 3 |
| 固定启动资源 | `runtime/resources/native-bootstrap/` 下的 `manifest.json` 和上文列出的 7 个文件 | 8 |

源码、脚本、配置示例和依赖声明正常入库。当前公共种子不包含 `runtime/server/config`；没有生效配置指针时使用内建规则。若将来需要发布自定义公共规则，先明确其文件范围及兼容性，再单独调整清单，不把玩家的整个配置目录直接加入仓库。

以下内容不放 GitHub：原版 `Aigis_r.exe` 及原版目录、原版和项目的大型 `.cache`、`runtime/client`、玩家数据库及 WAL/SHM、`runtime/saves`、备份、个人采集记录、桌面登记文件、令牌、日志、窗口缓存、`.venv` 和测试产物。`AigisConsole.exe` 是本项目的桌面启动器，可以用 `build-console.ps1` 本机构建；它与游戏本体不同，当前源码流程不依赖仓库内附带该 EXE。

忽略规则不会自动移除已经跟踪的文件。准备提交前检查实际暂存清单，确认只有上述公共文件被纳入，不能仅凭 `.gitignore` 判断发布内容安全。这是选择发布内容的要求，不是删除开发机存档或资源的指令。

## 玩家使用维护者提供的公共资料

玩家不执行上文第 4、5 步的采集和导入。下载与本项目支持档案一致的原版后，保留项目中维护者提供的公共种子，在项目根目录执行：

```powershell
.\setup.ps1 -IncludeDesktop
.\collect-resources.ps1 -Scope all
.\client\prepare-client.ps1 -MasterDirectory .\runtime\server\masterdata -InspectOnly
.\client\prepare-client.ps1 -MasterDirectory .\runtime\server\masterdata
.\open-console.ps1
```

逐条确认成功后再执行下一条。`collect-resources.ps1` 不传 `-ContextFile` 时直接使用已提供的活动索引，复用本机原版缓存并补齐缺少的资源；不要求玩家采集资源版本。`.venv`、大缓存和本地客户端副本都在玩家本机生成。安装环境、原版指纹检查和首次建档详见 [README](../README.md)。

## 支持新版客户端与发布更新

当前默认支持的组合为：

| 项目 | 值 |
|---|---|
| 客户端显示版本 | 5.0.1 |
| 原版 SHA256 | `d0be510fb38b1d6dfe63c3562c97e45f49d318bf7e155dee309d88efe963f92b` |
| 公共主表快照 | `9159442edcc59db92d37856e` |
| 基础资源版本 | `86164496048d9b4329259cfb248a9d3ff283bb36` |
| 基础索引条目 | 24,019 |

兼容性以明确文件指纹及配套资料为准。相同显示版本也可能有不同文件；未知或改过的 EXE 仍被拒绝。代码保留旧版明确档案，不代表当前 GitHub 公共种子同时为所有旧版提供完整资源组合。

维护者支持新版时按以下顺序处理：

1. 在隔离工作目录保存新版原版指纹，核对客户端补丁位置、协议注册与布局、主表 schema、固定启动资源以及采集挂点。为确认支持的文件新增明确档案，保留需要兼容的旧档案；不能只追加 SHA256 或放宽校验后宣称支持。
2. 使用上文采集步骤取得新版公共主表和资源上下文。先在隔离候选目录导入主表、生成索引和收集资源，核对新旧表、资源增删以及受影响玩法。上文第 5 步的默认命令会激活默认目录中的资料，不能直接用于尝试更新已有玩家环境。
3. 准备并验证新版客户端副本，确认副本、服务、公共主表、基础索引与生效资源版本一致。基础资源版本和应用本地覆盖后生成的有效资源版本可能不同，更新记录应区分两者。
4. 更新当前公共种子的精确白名单：同时替换主表与资源的活动指针、对应快照/索引、清单摘要和固定启动资源的客户端指纹。新指针必须能在本次发布的文件中完整解析，不能只改 `active` 或保留指向未发布快照的引用。
5. 对已有存档单独编写迁移说明，列出涉及的主表、配置、玩家状态和旧回执影响，明确备份、验证、切换及失败回退步骤。GitHub 更新公共种子不会自动升级 `runtime/saves/<ID>` 的主表和配置；`runtime/server` 也可能保存旧档，不能默认覆盖安全。
6. 完成下面的隔离验收，再在 [changelog.md](../changelog.md) 记录支持的客户端指纹、资源版本、主表快照、存档影响、玩家操作和已验证范围。发布版本号、日期和 tag 仅在实际发布时填写；没有完成的验证明确保留为待办。

## 发布前验收

- 在干净的隔离目录验证源码和 17 个公共种子的布局，用玩家流程安装依赖、复用或下载资源、准备客户端。确认没有附带维护者的虚拟环境、玩家档案、凭据或大缓存。
- 首次打开控制台应无存档；手动创建首档后自动选中，不自动开服。验证服务启动、正常停止、后续新建与切档，以及停服时不发送存档管理请求。
- 涉及旧档兼容时，按该次迁移说明在独立测试副本验证旧档、配置及回执。不得对正在运行的数据库直接复制主文件；先正常停服，或使用受支持的备份入口取得一致备份。迁移验证不在开发机正式档上进行。
- 实际启动受支持的原生客户端，验证进入主页及本次改动影响的操作。离线 `doctor`、单元测试或服务健康检查通过，不能替代这项验收；跨机器验证也须单独记录。

截至本次记录，已完成新版副本离线验证、桌面控制台和新档服务启动验证，尚未完成原生游戏进入主页或完整游玩验收。GitHub 发布说明应保留这一边界，不把本地准备成功写成完整游戏兼容。

## 开发测试

```powershell
.\setup.ps1 -IncludeTests
. .\environment.ps1
$projectPython = Initialize-AigisEnvironment -ProjectRoot $PWD.Path
& $projectPython -m pytest tests/test_protocol.py tests/test_masterdata.py --basetemp=tmp/selected-tests -q
```

进一步参阅[资源视图](local-resources.md)、[协议与命令行参考](technical-reference.md)和[桌面控制台实现](desktop-console.md)。
