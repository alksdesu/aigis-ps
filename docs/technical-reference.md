# 协议、资源与命令行参考

这是针对当前 `aigisr_exe/Aigis_r.exe` 的独立本地服务。协议来自当前 PC 客户端的静态分析，保存数据使用本项目自己的 SQLite 文件。

当前已具备原生 Snappy/XOR/HS 协议、本地登录、初始化与设置响应、版本状态与心跳、资源索引及解析、主表导入和 PC 副本准备工具。2026-11-06 已启用 5.0.1 客户端对应的公共主表快照 `9159442edcc59db92d37856e`：`CardData` 2932 行、`QuestTable` 3524 行。**尚未完成私服原生客户端进入主页的验证**。具体实现范围由 `doctor` 输出及 [实现记录](implementation.md) 记录。

当前资源版本 `86164496048d9b4329259cfb248a9d3ff283bb36` 的 24,019 个清单项已全部核对并独立汇入 `runtime/resources/.cache`，已切换活动索引，不再依赖原版缓存。升级备份已列入用户确认的清理清单；是否仍在磁盘上以清理结果报告为准。剧情父包和任务分区可直接批量读取，无需逐段播放来采集正文；解锁、触发及进度逻辑仍需另行实现。

本次兼容更新保留已移除的 84 个关卡历史记录，当前回包仅投影当前主表；料理文本键从当前资源解析。服装 46/47 的 Stack0 定义可识别已有解锁，但不支持新发放或购买。`BlessingConfig` 从 258 行变为 36 行不代表删除战神加护的 242 个库存定义。回归资格默认开放尚未实施，Type3、任务 1637 等新增玩法仍未实现，不能据资源升级宣称全游玩可用。

## 本机启动

所有命令在项目根目录下执行。需要 Python 3.12，桌面窗口另需 Windows WebView2 Runtime；`install.cmd` 会自动装齐，见 [README](../README.md)。

日常使用可双击项目根目录的 `AigisConsole.exe`，或运行 `.\open-console.ps1`。它在独立的 WebView2 应用窗口中显示控制台，打开窗口时默认不开服。先在「启动与存档」选择存档，点击「启动服务」，等状态变成「运行中」后再点「启动游戏」。原有的存档编辑、配置、备份和恢复页面在服务启动后继续使用。

首次安装桌面依赖或重新生成启动器：

```powershell
.\setup.ps1 -IncludeDesktop
.\build-console.ps1
.\start-console.ps1 -Check
.\open-console.ps1
```

「新建独立存档」只继承当前存档已落盘的公共主表和配置，不复制玩家数据库、角色、进度或奖励。新建后在列表中选择它，再启动服务初始化新玩家。新建或切换前必须先退出游戏、停止服务；默认档仍使用 `runtime/server`，新增档使用 `runtime/saves/<ID>`。详细操作和文件位置见 [桌面控制台](desktop-console.md)。

需要在终端单独管理服务时，保留原有入口：

```powershell
.\setup.ps1
.\start-server.ps1 -Check
.\start-server.ps1
```

服务默认监听 `127.0.0.1:18103`。`-Check` 只检查资源与实现状态，不创建玩家存档，也不启动服务。服务进程可用 Ctrl+C 停止。

`start-server.ps1` 同时在 `127.0.0.1:18104` 启动管理控制台，带令牌的地址打印在终端并写入 `runtime/server/admin/access.json`（正常退出时删除）。`-AdminPort`、`-AdminHost`（`127.0.0.1` 或 `::1`）改端口和地址，`-NoAdmin` 只启动游戏服务，`-Check` 不启动控制台；直接用命令行时要显式加 `serve --admin-port 18104`。连接这个已启动的服务可用 `.\open-console.ps1 -Browser` 保留原 Edge 入口，浏览器配置放在 `runtime\console-browser`；`-PrintUrl` 只打印该服务的地址，`-Browser -DataDirectory` 指向其他数据目录。省略这些参数时打开独立桌面应用。令牌每次启动都会更换。用法、安全模型和全部命令见 [管理控制台](admin-console.md)。

`-Check`（`doctor`）的输出另含 `config`（当前生效的配置集；解析失败时为 `{"error": {code, message}}`，这时 readiness 只按命令行文件计算，`startup_ready` 为 false）和 `admin`（界面是否安装、页面清单、预检与重启检查是否为空实现，以及 `access.json` 中除令牌外的地址、端口、进程号和创建时间）。

- `GET /health`：启动器探活用的紧凑报告：服务标识、各域 readiness 的状态值与标识，以及 `native_cdn`。
- `GET /local/status`：完整 readiness、本地存档版本、已配置处理器及未实现接口。
- `GET /local/requests`：最近请求的接口、字段路径、状态码，不含字段值。
- `POST /api/<8字符路径>`：客户端原生协议。
- `GET /assets/<资源名或文件名哈希>`：索引通过校验的本地资源；不在索引中回 404 `asset_not_found`，已索引但本地还没缓存回 404 `asset_not_cached`。
- `GET/HEAD /client-assets/<版本>/<PC清单名>`：原生 PC 资源清单，保留 XOR 编码。
- `GET/HEAD /client-assets/<对象组>/<MD5>`：原生资源正文及 6 个固定启动对象，支持单段 Range 请求。
- `GET/HEAD /client-assets/000/_GameInfoText`：经校验的原生提示文本表，保留 ALTB 格式。

`health.status=ok` 仅说明服务正在运行；数据缺失由各域的 `ready`、`error` 等状态值反映。`/health` 的 `readiness` 每个域只保留顶层的数值、布尔值、空值和不含空白的标识串（代码、状态、版本），bootstrap 另保留 `startup_blockers`、`unavailable`、`startup_diagnostics`；嵌套明细、未实现契约和各域的说明文字（`*_policy`、`refusals` 等）只在 `/local/status` 和控制台里给出。`client/start-client.ps1` 拒收超过 64 KiB 的健康响应，所以 `/health` 的体积预算为 32 KiB。

两个状态接口的 readiness 是当前服务代构建时的结果，只有 `bootstrap.startup_diagnostics` 实时更新；控制台切换配置后两者随新一代变化，`/assets/` 始终读取启动时那一代的资源。serve 的启动闸门由 bootstrap、玩家状态、物品定义、礼物模板、客户端指纹、原生资源视图和 `startup_ready` 判定，readiness 只贡献汇总的 `startup_ready`。后台请求测试通过不能代替原生客户端实测。

命令行子命令（`start-server.ps1` 也经 `python -m aigis_ps.cli` 启动）遇到无法解析的输入（含嵌套过深）时，输出 `{"error": 类型名, "message": …}` 并以退出码 1 结束，不打印 traceback；带错误码的拒绝（`ProtocolError` 及其子类，如 `ConfigError`）另带 `code` 与 `status`。

## 存档与本地规则

默认存档是 `runtime/server/aigis.sqlite3`。账号、用户密钥和方法密钥只在首次建档时生成，服务重启不会重新生成。玩家状态带修订号，写入时检查冲突。请求记录最多保留 1000 条。

初始金额 1000、等级 1、经验 0、魅力按 `RankUp.atb` 得到 60、行动力 0；教程标志保持未完成。新存档提供原生默认角色 CardID 1，其职业和技能来自当前公共主表；礼物和结晶为空。这些是明确的本地起始规则。

```powershell
.\start-server.ps1 -RulesFile '.\config\local-rules.example.json'
```

规则文件可设置金额、等级、经验、魅力、行动力、名字，只影响新建存档。等级和经验必须符合当前等级表。金额不得超过 99,999,999（与全游戏金币上限一致），超出时启动报 `Invalid local rule: money`，控制台校验配置集时报 `config_rules_invalid`。现有存档不会被配置覆盖；运行中查看和修改存档使用管理控制台。

各类本地规则也可以在控制台里校验、保存成配置集并切换。`runtime/server/config/active.json` 指向的配置集是基线，没有它时全部取代码默认值；命令行规则文件参数（如 `-QuestRulesFile` / `--quest-rules`）逐键覆盖，只对当前进程有效，被覆盖的键在控制台只读。示例文件不会自动套用。配置集文档先经严格预解析：重复键回 `config_duplicate_key`，NaN/Infinity 或溢出成无穷的数回 `config_number_invalid`，超过 64 层的嵌套回 `config_nesting_too_deep`；命令行规则文件不经预解析，由各自加载器按同一严格 JSON 读取，启动时遇到这类错误直接报错退出，经配置注册表读取时（如控制台的配置描述）重复键与非有限数回 `config_rules_invalid`，嵌套过深回 `config_nesting_too_deep`。`masterdata/active.json`、快照清单与资源库清单同样按严格 JSON 读取。

控制台在写入前自动备份：命令提交只在确实改变状态、且含破坏性步骤或多于一步时备份；恢复和 gated、content 档的配置切换在存档会变化时先自动备份，状态不变时不备份，唯一一份自动备份保持不变。自动备份只保留最新 1 份、超过 1 天删除，需要长期保留的状态请在控制台做手动备份。服务起不来时，可在停服后用 `admin-restore-plan` / `admin-restore-apply` 从数据目录内的备份离线恢复，用法见管理控制台文档的「离线命令」一节。

需要独立测试存档时，可用 `-DataDirectory` 指定 `111` 内另一个目录。不要让两个服务进程同时操作同一玩家存档。

## 公共主表

采集工具已完成首次真实采集，抓齐两表后自动解除挂钩并正常退出。当前活动主表快照为 `9159442edcc59db92d37856e`。需要更新公共主表时可执行以下步骤：

```powershell
.\setup.ps1 -IncludeCapture
.\capture-masters.ps1 -Check
.\capture-masters.ps1
```

第二条只检查客户端指纹和挂钩位置。第三条会等待新启动的原版进程；随后从 DMM 正常启动原版，加载到对应公共表时保存 `CardData.xml`、`QuestTable.xml`。仅允许两个已验证调用点的公共表，拒绝未知字段。两表齐全后自动分离并退出，不采集请求、账号字段或命令行凭据。超时会留下已验证的部分数据，但部分采集不能激活。采集时限从代理加载完成起算；到期时已送达的数据仍会处理，之后到达的不再延长采集。

采集目录位于 `runtime/captures`。确认该目录的 `manifest.json` 为 `complete: true` 后导入：

```powershell
.\.venv\Scripts\python.exe -m aigis_ps.cli import-masters '.\runtime\captures\具体目录'
.\start-server.ps1 -Check
```

导入会核对客户端指纹、内容哈希、公共字段白名单、必要列和每列行数。两表都有效后才原子切换 `masterdata/active.json`；失败时保留旧快照。重复导入不会重复建档或发放奖励，已有主表版本保存在 `masterdata/snapshots`。主表响应使用不可变快照，导入不需要改代码。

运行中的服务固定使用启动时的主表快照，命令行导入后要重启服务才生效；服务运行中直接激活时，控制台会把新快照当作等待下次启动的已暂存主表。`import-masters <capture> [--data-dir DIR] [--stage]` 加 `--stage` 时只校验并保存快照，不切换 `masterdata/active.json`，输出的 `activated` 为 false；控制台的导入入口同样只暂存。暂存的快照在控制台的内容切换里激活（默认暂存到下次启动，确认客户端已关闭后也可运行中切换）。主表变更后要重启客户端。

`missing_schema_columns` 会列出未出现的可选列，不会补造值。旧版本主表和零行占位表不能替代当前数据。

## 官服响应采集

一次原版启动同时采公共主表、资源版本、全部允许的业务响应，并导出首份完整的 70/71 调度配对，可用一键入口：

```powershell
.\client\capture-all.ps1 -Mode Check
.\client\capture-all.ps1 -Mode Wait -Timeout 7200
```

先运行等待命令，再从 DMM 手动启动新的原版进程；结束时按 Ctrl+C，等待采集器保存结果后退出。默认模式为 `Check`，只提供 `Check`、`Wait`。汇总在 `runtime/capture-runs` 下的 `run.json`，三个分项分别报告状态；正常停止不代表全部采集任务完成。操作路线、时限与输出说明见 [官服一键采集](capture-all.md)，这一入口尚未实机验证。

召唤、战斗、签到、礼包目录等官服响应用通用采集器被动记录，在游戏进程内去掉凭据后写入 `runtime/response-captures`：

```powershell
.\client\capture-responses.ps1 -Mode Check -Preset summon
.\client\capture-responses.ps1 -Mode Wait -Preset summon
```

范围可用 `-Operations`（注册号）、`-Preset`（summon、battle、login、shop、card-status）或 `-All` 三选一。登录、令牌、账号联动、付费和反作弊接口一律不采。只补采召唤调度 70/71 时可用 `client/capture-schedule.ps1`；一键入口已包含同轮配对导出。详见 `docs/response-capture.md`。

## PC 副本

准备工具已适配当前 5.0.1 原版 SHA256 `d0be510fb38b1d6dfe63c3562c97e45f49d318bf7e155dee309d88efe963f92b`，并保留旧版 `babe8210a8652fb25b1c61056ee500b05bd9a3ae37f55d1682a2fb8b850802ba` 的明确档案。按原版指纹选择补丁位置，未知或改过的版本仍停止处理。新版副本已完成准备与验证，`runtime/client` 已发布；2026-11-07 已验证桌面控制台使用新建独立存档启动服务，游戏进入主页仍待实测。

```powershell
.\client\prepare-client.ps1 -InspectOnly
.\client\prepare-client.ps1
.\client\start-client.ps1 -DryRun
```

准备工具将程序、4 个原版启动资源及完整的 24,019 项索引缓存物理复制到 `runtime/client`，约需 11.6 GB 独立磁盘空间，并要求复制后仍有至少 2 GiB 可用空间。它排除原版的账号加密文件，不使用硬链接或目录链接；原版及 `runtime/resources` 资源库保持不变。准备期间固定资源版本，逐项验证正文的大小、MD5 和副本 SHA256，全部完成后才发布客户端目录。

副本中的 API 前缀替换为 `http://127.0.0.1:18103/api/`，CDN 前缀替换为 `http://127.0.0.1:18103/client-assets/`，分别保持原有 27 和 37 字节长度及 NUL 终止符。新版 API/CDN 文件偏移分别为 `0xA6D8F0`、`0xA6D90C`，补丁后 SHA256 为 `48ab67a9d3397637f43c716a58ec7ba4745442288f245efb652ffa0d0ff28f96`。已发布副本的有效资源版本为 `6d9554288063047475ca96a4626468621480190f`，24,019 项正文共 11,519,326,920 字节，旧副本已列入升级备份清理清单。副本使用本地占位身份连接服务，原始 EXE 不变。

同客户端与资源版本的已验证副本会直接复用并保留缓存。切换客户端或资源版本、升级旧 API-only 副本或使用 `-Refresh` 时，旧目录保留为 `client.backup-<时间>-<随机值>`，新目录发布失败会恢复旧名称。`-InspectOnly` 检查复制计划及资源存在性、大小，不执行复制。规则文件、清单无法解析或嵌套过深时，准备工具输出 `Client preparation failed: …` 并以 1 退出，已有文件不被覆盖。

`client/prepare-client.ps1` 与 `client/start-client.ps1` 和其他入口脚本一样先点源 `environment.ps1`：只用项目 `.venv` 的解释器，缺少时先运行 `setup.ps1`，不会退回系统 Python；临时目录、pip 缓存与字节码缓存都在项目内，`start-client.ps1` 拉起的游戏进程也以项目 `tmp` 作为 TEMP/TMP。

正式实测时才运行 `client/start-client.ps1`。启动前会核验副本、服务标识，以及 `native_cdn.ready`、本地 CDN 路径和与副本一致的资源版本；`-DryRun` 不访问服务，也不启动游戏。该副本固定使用端口 18103，仅修改服务端口不会改变副本地址。最终离线 `doctor` 的 `source_supported`、`startup_ready` 均为 true，`not_ready=[]`。三批回归出现的失败均已修复并通过补测，累计成功执行 3822 次（含重复补测，不是独立用例数），1 项跳过；记录见 `runtime/client-upgrade.json`、`tmp/probe/new-resources/prepared-client-upgrade.json` 及同目录 `test-upgrade-{state,rewards,catalogs}{-final}.log`。**尚未完成私服原生客户端进入主页或完整离线运行的实机验证**。路由证据及复制约束见 [原生客户端接入说明](../client/native-client-routing.md) 和 [API 地址审计](../client/api-routing-audit.md)。

## 资源分析与验证

```powershell
.\.venv\Scripts\python.exe -m aigis_ps.cli inventory --output '.\artifacts\inventory'
.\.venv\Scripts\python.exe -m aigis_ps.cli export Map1001.aar --output '.\artifacts\map-export'
```

资源工具支持已验证的 ALL4 解压、ALRD/ALTB 表结构及 ALAR 包内目录，未知类型会保留诊断。输出与原资源目录分离，已有导出不会静默覆盖。

资源采集范围、续采命令和剧情索引见 [采集清单](collection-plan.md)。当前 PC 资源版本已采到，可以按公开清单批量补缓存；现有剧情包已批量导出到 `runtime/story-library`，无需逐段播放采集正文。

心跳 `AssetPath` 使用已确认的 40 位版本目录，不能填写完整 URL。独立客户端会把该版本与已替换的本地 CDN 根拼接。服务从 `runtime/resources/active-index.json` 读取并固定本次运行的索引；资源版本更新后应重新准备客户端并重启服务，使两端版本一致。

原生 CDN 服务保留 PC 索引的 XOR 编码及资源容器原始字节。索引外的 6 个固定启动对象与 `GameInfoText` 已单独获取、校验并保存于 `runtime/resources/native-bootstrap`，由该目录的 `manifest.json` 建立路由白名单。固定 CDN 对象与原版目录中同名文件可能采用不同内容，服务按各自实际 MD5 返回，不混用两者。`GameInfoText` 是 23 行的原生 ALTB 消息表，不能用任意公告字符串替代。

依赖、缓存、临时文件和测试报告均放在本项目内。定向测试示例：

```powershell
.\setup.ps1 -IncludeTests
. .\environment.ps1
$projectPython = Initialize-AigisEnvironment -ProjectRoot $PWD.Path
& $projectPython -m pytest tests/test_protocol.py tests/test_masterdata.py --basetemp=tmp/selected-tests -q
```

`tests/test_server_smoke.py` 会用原版目录中已有的公开资源启动短时本地服务，测试自身独立存档，再关闭该进程；它不会打开游戏。报告位于 `artifacts`，不运行旧 DotAbyss 项目的测试。
