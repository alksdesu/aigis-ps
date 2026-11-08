# aigis_ps

千年戦争アイギスR 本地私服 + Windows 桌面控制台。协议、主表、资源分发和存档都在本机，游戏服务监听 `127.0.0.1:18103`。

## 布局

```text
<任意目录>\
├─ aigis_ps\      本仓库
└─ aigisr_exe\    DMM 原版完整目录（含 .cache）
```

两者同级。服务和准备工具都从 `aigisr_exe` 读原版文件，装完也要保留。

仓库只有源码和当前版本的公共种子：主表快照、资源索引、固定启动资源。游戏本体、资源缓存、存档和 `.venv` 不入库。

## 依赖

- Windows x64，Python ≥ 3.12（按 3.12 验证）
- [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
- 原版：用 DMM GAME PLAYER 安装 千年戦争アイギス R（[官方说明](https://aigis1000.jp/notification/detail/id/10864)），启动一次跑完更新后退出，把整个目录复制为 `aigisr_exe`。支持的客户端 SHA256 见 [changelog.md](changelog.md)。

## 安装

在 `aigis_ps` 下打开 PowerShell：

```powershell
# 原版指纹必须为 True，否则等适配
(Get-FileHash ..\aigisr_exe\Aigis_r.exe).Hash -eq (Get-Content .\src\aigis_ps\data\protocol-contracts.json -Raw | ConvertFrom-Json).input_sha256

.\setup.ps1 -IncludeDesktop               # 建 .venv、装依赖
.\start-console.ps1 -Check                # 自检桌面依赖，不开服
.\collect-resources.ps1 -Scope all        # 按配套索引补齐资源，可断点重跑
.\client\prepare-client.ps1 -MasterDirectory .\runtime\server\masterdata   # 生成 runtime\client 本地副本
.\build-console.ps1                       # 可选：生成 AigisConsole.exe
```

- 脚本被执行策略拦截时，先运行 `Set-ExecutionPolicy -Scope Process Bypass`，只对当前窗口生效。
- `collect-resources` 完成时任务状态为 `status: complete`、`failed: []`，记录在 `runtime/resources/jobs/`。已校验的文件重跑时复用。
- `prepare-client` 可先加 `-InspectOnly` 预检。运行时原版和本地游戏都要退出，完成后磁盘至少要剩 2 GiB。

## 运行

打开 `AigisConsole.exe`（或运行 `.\open-console.ps1`），进入「启动与存档」：

1. 首次使用：输入名称，点「创建第一份存档」，新档自动选中。
2. 点「启动服务」，等状态变成「运行中」。
3. 点「启动游戏」。

退出顺序：游戏 → 服务 → 控制台。切档和新建都要求游戏、服务已停；新建后点新档的「切换到此存档」才会切过去。

存档名为 1–64 个可打印字符，不可重名。新档只复制公共主表和配置，不带进度；该档首次开服时生成数据库。

### 命令行

```powershell
.\start-server.ps1 -Check                 # 只检查，不开服
.\start-server.ps1                        # 服务 :18103，管理台 :18104（-NoAdmin 关闭）
.\client\start-client.ps1                 # 启动 runtime\client 副本
```

`start-server.ps1` 默认用 `runtime\server`，`-DataDirectory runtime\saves\<ID>` 指定其他存档。各项本地规则用 `-*RulesFile` 参数加载，见 [technical-reference.md](docs/technical-reference.md)。

## 数据

| 路径 | 内容 |
|---|---|
| `runtime/saves/<ID>/` | 各存档的数据库、配置、主表指针、备份 |
| `runtime/desktop/profiles.json` | 存档列表与当前选择 |
| `runtime/server/` | 默认数据目录，旧版单档也在这里 |
| `runtime/resources/` | 资源库，各档共用 |
| `runtime/client/` | 本地游戏副本，各档共用 |
| `runtime/desktop/*.log` | 控制台与服务日志 |

- 备份：开服后在「备份」页建手动备份；自动备份只保留最新一份。
- 迁移：停掉所有进程后整体搬，保持同级布局，`.venv` 在新机器上重建。SQLite 的 `-wal`、`-shm` 文件随主文件一起搬。

## 更新

官服出新版不等于这里已适配，以 [changelog.md](changelog.md) 的支持版本为准：

1. 在「备份」页建手动备份，然后退出游戏、停服、关控制台。
2. 把仓库更新到适配版本，同步更新原版目录。
3. 重跑 `collect-resources.ps1 -Scope all` 和 `client\prepare-client.ps1`。
4. 按该版本 changelog 条目迁移存档。

`runtime/saves`、`runtime/desktop`、`runtime/server` 是玩家数据，更新时保留。

## 排错

| 现象 | 处理 |
|---|---|
| 没有 `AigisConsole.exe` | 运行 `.\build-console.ps1`，或用 `.\open-console.ps1` |
| 控制台没窗口或闪退 | 运行 `.\start-console.ps1 -Check`，看 `runtime/desktop/launcher.log`、`desktop.log` |
| 原版指纹不匹配 | 该版本未适配，见 changelog |
| 资源下载失败 | 修好网络后重跑，已校验的文件会复用 |
| 副本与服务资源版本不一致 | 重跑 `prepare-client.ps1` |
| 18103 端口被占用 | 关掉另一个控制台或 `start-server.ps1` 实例 |
| 数据库被占用或日志错误 | 停掉占用的进程，不要手删 WAL/SHM |
| 缺少能量时钟标记 | 见 [energy-clock.md](docs/energy-clock.md) |
| 界面还是旧版 | 完全关闭控制台后重开 |

开服失败时，「启动与存档」页会显示错误和本次日志路径。

## 文档

- [changelog.md](changelog.md)：版本记录与支持的客户端
- [desktop-console.md](docs/desktop-console.md)：桌面控制台
- [admin-console.md](docs/admin-console.md)：存档编辑、备份与恢复
- [technical-reference.md](docs/technical-reference.md)：协议与命令行
- [release-preparation.md](docs/release-preparation.md)：公共资料采集、新版适配与发布
- [maintenance.md](docs/maintenance.md)：目录用途与维护约定
- [docs/README.md](docs/README.md)：全部文档
