# 管理控制台

本机管理面：管理API与游戏服务同一进程，单独监听回环端口，用来查看存档与运行状态、按计划修改存档、校验并切换配置集与主表、备份恢复以及脱敏导出。存档编辑先预览、再凭计划提交，写库只经过审计事务和备份接口。独立桌面应用嵌入同一套页面并代理管理API，另提供服务、游戏和多存档启动入口。页面资源在`src/aigis_ps/admin_ui`，无需构建，不加载CDN或外部字体。

## 启动

双击项目根目录的`AigisConsole.exe`，或运行`.\open-console.ps1`，打开独立的WebView2桌面窗口。窗口默认不开服，先在「启动与存档」选择存档，点击「启动服务」，等待运行后再点击「启动游戏」。停服期间其他管理页面提示先启动服务，不读取玩家存档。

首次部署桌面入口：

```powershell
.\setup.ps1 -IncludeDesktop
.\build-console.ps1
.\start-console.ps1 -Check
.\open-console.ps1
```

桌面入口需要Windows WebView2 Runtime。`build-console.ps1`在项目根目录生成`AigisConsole.exe`，它使用项目`.venv`，不是包含全部依赖的单文件发行包。`start-console.ps1 -Check`只检查桌面依赖，不启动游戏服务、不打开玩家存档。

新建或切换存档前，先退出游戏，再停止服务。首次无存档时由用户命名创建，从`runtime/server`的配套公共主表和可选配置建立首档并自动选中；后续新档只继承当前档的公共资料，创建后仍需主动选择。各新档位于`runtime/saves/<ID>`，不会复制玩家数据库。已有登记及旧版`runtime/server`中的存档继续保留，不会强制重建。服务启动后，所有存档编辑、备份、恢复和配置功能沿用本篇规则，作用于当前所选档。桌面应用的目录、退出行为和故障排查见[桌面控制台](desktop-console.md)。

以下为独立命令行服务的启动方式；桌面应用通过自己的启动页管理服务，不需要先执行这些命令。

`start-server.ps1`默认在`127.0.0.1:18104`同时启动控制台：`-AdminPort`改端口，`-AdminHost ::1`改用IPv6回环，`-NoAdmin`只启动游戏服务，`-Check`（doctor）不启动控制台。直接使用命令行时要显式开启：`python -m aigis_ps.cli serve --admin-port 18104 [--admin-host ::1]`；`--admin-host`不能单独使用，控制台端口必须与游戏端口不同。游戏端口18103及其中间件不变；管理路由固定为`/admin`、`/admin/static`和`/admin/api/v1`。

serve的启动顺序（第2、3、7步只在开启控制台时执行）：

1. 检查存档的能量时钟标记；
2. 装配真实的预检与重启兼容检查，任一为空实现即拒绝启动；
3. 预先独占绑定控制台端口（Windows设置`SO_EXCLUSIVEADDRUSE`），端口被占用直接失败；
4. 已有数据库时按切换记录前滚配置集与主表指针（见「前滚」）；
5. 解析配置集，构建首个服务代（下称世代）并通过启动闸门；
6. 初始化存档；
7. 在后台线程启动控制台（10秒内未就绪即失败），写入`access.json`并在终端打印带令牌的地址；
8. 启动游戏服务。

退出时游戏服务先停止，随后删除`access.json`并停止控制台线程。端口被占用、检查为空实现或控制台未能启动时，serve以退出码1失败，不会只启动一半。控制台监听器关闭代理头处理，不信任`X-Forwarded-*`，自身origin始终取连接的实际scheme加通过校验的Host。

## 身份与打开方式

每次启动用`secrets.token_urlsafe(32)`生成新令牌，写入`<data_dir>/admin/access.json`，字段为`schema_version`、带`#token=`片段的`url`、`token`、`admin_host`、`admin_port`、`pid`和`created_at`。正常退出时只有持有该令牌的进程才删除这个文件。令牌只放在`Authorization: Bearer`头里，不进查询串、日志或cookie；URL片段不随请求发送，页面读出令牌后用`replaceState`清掉，只保存在内存中。管理端从不使用游戏的UK/MK。

`.\open-console.ps1`默认打开独立桌面应用，不读取已有服务的`access.json`。需要连接独立命令行服务时使用`-Browser`：它读取`access.json`（要求`schema_version`为1、地址是回环控制台、写入它的进程仍在运行），以Edge应用窗口打开，浏览器配置放在`runtime\console-browser`；找不到Edge时打印地址。`-PrintUrl`只打印已有服务的地址，`-Browser -DataDirectory`可指定工作区内其他数据目录；默认桌面入口在窗口内选择存档，不接受`-DataDirectory`。用普通浏览器配置打开带令牌的地址时，地址可能被系统或浏览器记录到C盘（例如跳转列表）；令牌每次启动都会更换，这一残留风险可以接受。

`doctor`输出的`admin`节给出`ui_installed`、`ui_pages`、`preflight_noop`、`restart_check_noop`和`console`；`console`取`access.json`中的`admin_host`、`admin_port`、`pid`、`created_at`，不含令牌，没有该文件时为null，文件不可读时为`{"error": "admin_access_unreadable"}`。`config`节给出生效的配置集，解析失败时为`{"error": {code, message}}`，这时readiness只按命令行文件计算，`startup_ready`为false。

`readiness`节是serve装配同一配置时的逐域readiness（控制台`runtime.readiness`与`/readiness/{domain}`读的是运行中世代的同一份结果），另加`saved_energy_clock`，它计入startup_ready。以下三个域只报告、不计入startup_ready：`auto_requests`给出客户端不经玩家操作就会发送的号（`operations`）、其中没有处理器的号（`unhandled`，期望为空）、列入任务菜单且窗口未结束的协力大討伐（`cooperative_subjugations`）与客户端表现（`client`），有缺口、协力行读不出或大討伐目录读不出（此时`cooperative_subjugations`为null）时`ready`为false，见`implementation.md`；`summon_schedule`报告70/71的显式空调度；`shop_window_closure`报告108的关窗覆盖（关闭的商品、原窗口、源与结果SHA-256），没有覆盖时为`{"enabled": false}`，见`local-resources.md`。已存体力时钟就绪时另附只读的存档诊断，都不计入startup_ready，读取失败时各为`{"error": 码}`：背景服务可用时的`saved_backgrounds`（见`background-settings.md`）、`saved_business_calendar`（见`energy-clock.md`）、登录奖励服务可用时的`saved_login_rewards`与塔兑换服务可用时的`saved_tower_trades`（见`tower-exchange.md`）。`saved_login_rewards`给出新手总数的`saved_total`、`current_total`与`changed`（不符时附`refused_operations`、`repair`即`login_rewards.rebase_beginner`、`client`），料理阈值的`cooking`（`saved_border`、`current_border`、`changed`，不符时附`refused_operations`、`repair`即`login_cooking.migrate`、`offline_repair`即两条离线迁移命令、`client`），以及待发印章容量的`pending_stamps`（`count`、`capacity`、`remaining`、`warning`，满额时附`refused_operations`与`code`，这时只有需要新记一条待发印章的115被拒），见`login-rewards.md`与`login-cooking.md`。

## 请求校验

中间件按以下顺序处理每个请求，任一步失败立即返回：

1. Host必须精确等于`127.0.0.1`、`localhost`或`[::1]`加管理端口，否则400 `admin_host_invalid`；Host、Origin、Authorization等头重复出现也是400（`admin_header_repeated`）。
2. 自身origin取已通过校验的`scheme://Host`；带Origin时必须完全相等，`http://127.0.0.1:18103`这类同主机其他端口一律403 `admin_origin_denied`。页面的fetch以`referrerPolicy: "same-origin"`发起：no-referrer策略会让浏览器把POST的Origin序列化为`null`，过不了精确比较；same-origin下同源请求带真实Origin，Referer最多只有不含令牌的`/admin/`地址（令牌在URL片段里，从不进Referer）。
3. `Sec-Fetch-Site`为`same-origin`或`none`时放行，其他值403 `admin_fetch_site_denied`；不带该头视为非浏览器客户端，照样要求令牌。
4. OPTIONS一律405 `admin_method_not_allowed`，不开CORS。
5. 除GET/HEAD的`/admin/`页面和`/admin/static/`资源外都要求Bearer令牌，用`compare_digest`比较，失败401 `admin_unauthorized`并带`WWW-Authenticate: Bearer`。
6. 写方法只收`application/json`（可带`charset=utf-8`），否则415 `admin_content_type_unsupported`。
7. 请求体流式读取，声明或实际超过1 MiB返回413 `admin_body_too_large`；Content-Length不是字节数（`admin_body_invalid`）或请求体传完前断开（`admin_body_incomplete`）返回400。
8. JSON重复键（`admin_json_duplicate_key`）、NaN/Infinity或溢出成无穷的数字（`admin_json_invalid`）、嵌套超过64层（含解析时的RecursionError，`admin_json_too_deep`）都返回400。

WebSocket连接直接以1008关闭。参数模型使用pydantic strict加`extra=forbid`，不合规返回422 `admin_params_invalid`并在`fields`中列出位置。所有响应都带`Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store`以及COOP/CORP `same-origin`。FastAPI的docs、redoc和openapi均关闭。

错误统一为：

```json
{"error":{"code":"admin_revision_conflict","message":"...","current_revision":3}}
```

`fields`、`current_revision`和`details`只在有内容时出现；未知路径返回404 `admin_not_found`，路径存在而方法不支持时405 `admin_method_not_allowed`；未处理的异常返回500 `internal_error`，消息只给异常类型或通用说明。

## 路由

以下路径都在`/admin/api/v1`下：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/session` | 管理身份、控制台地址、服务端时间（UTC与JST）、玩家ID与修订版、当前世代、预检与重启检查是否为空实现、客户端闸门状态 |
| GET | `/runtime` | 世代运行时状态（当前与排空中的世代、栅栏、构建槽、候选世代、客户端空闲时间）、世代身份、闸门问题、readiness域名 |
| GET | `/readiness/{domain}` | 单个域的完整readiness（世代构建时的结果，启动诊断实时合并），已脱敏；未知域404 `admin_readiness_unknown` |
| GET | `/commands` | 命令目录：JSON Schema、域、是否破坏性、是否按参数判定、命令组（`group`）、可用性及原因，以及共享字段归属 |
| POST | `/plans` | `{"commands":[{"operation","params"}]}`，返回计划（见「计划与提交」） |
| POST | `/commits` | `{"plan_id","plan_digest","expected_revision","op_id","confirmations"}` |
| GET | `/audit`、`/audit/failures` | 审计与失败记录，`cursor`（1到2^63−1）、`limit`（1–200，默认50）分页，已脱敏 |
| GET | `/requests` | 最近的游戏请求记录，`limit`为1–200，已脱敏 |
| GET | `/views` | 视图目录：按名称排序列出每个视图的`name`、`domain`、`live`和参数JSON Schema（`params_schema`，不收参数的视图为null） |
| GET | `/views/{name}` | 读视图，参数以JSON放在`params`查询参数中（不超过8 KiB） |
| GET | `/ui-pages` | 页面清单，来自`admin_ui/pages/*.js` |
| GET | `/backups` | 备份列表及保留规则，分页同`/audit`；列出前先清理过期的自动备份 |
| POST | `/backups` | `{"reason"}`，创建手动备份 |
| POST | `/restores/preview` | `{"backup"}`，恢复预览 |
| POST | `/restores` | 执行恢复 |
| POST | `/exports` | `{}`，写出脱敏导出 |
| GET | `/config` | 运行中的配置集、active指针、暂存、主表状态、最近20次切换、当前校验作业、客户端告警、是否正在切换 |
| POST | `/config/validations` | 配置校验作业 |
| GET | `/jobs/{job_id}` | 校验作业进度与切换报告 |
| POST | `/config/switches` | 按报告切换或暂存 |
| POST | `/config/unstage` | 撤销暂存的配置集 |
| GET | `/entrust/validations` | 委任校验作业状态：世代号与作业的进度、结果、耗时，世代已切换时`stale`为true |
| POST | `/entrust/validations` | `{}`，启动委任校验作业；已有作业在跑时409 `admin_entrust_validation_busy`，无委任服务时503 `admin_entrust_unavailable` |
| GET | `/content` | 主表快照：在用与磁盘active并列、快照列表（`offset`、`limit`≤500） |
| GET | `/content/captures` | 可导入的采集目录 |
| POST | `/content/imports` | `{"capture"}`，把采集暂存为主表快照，不激活 |
| POST | `/content/validations` | 带`master_snapshot`的配置校验作业 |
| POST | `/content/unstage` | 撤销暂存的主表（可连同配置集） |

`/admin/`返回`admin_ui/index.html`，`/admin/static/`提供同目录下的页面资源；页面尚未安装时返回404 `admin_ui_missing`。

游戏端口上的`/health`是启动器探活用的紧凑报告：readiness每个域只保留顶层的数值、布尔值、空值和不含空白的标识串，`bootstrap`另保留`startup_blockers`、`unavailable`和`startup_diagnostics`。`client/start-client.ps1`拒收超过64 KiB的探活响应，`/health`的体积预算是32 KiB，由测试在配齐全部操作的真实readiness上检查。`*_policy`、`refusals`这类带空白的说明文字只在`/local/status`和控制台（`runtime.readiness`视图与`/readiness/{domain}`）出现，嵌套明细只在`/local/status`和`/readiness/{domain}`。这几处的readiness都取世代构建时的缓存，只有`bootstrap.startup_diagnostics`每次请求实时合并。新增readiness字段时，要给启动器判断的值写成数值、布尔值或标识串。

## 计划与提交

### 命令

每条命令有：带点号的操作名（如`units.grant`）；界面分组用的域；pydantic strict参数模型（`/commands`导出其JSON Schema驱动表单）；受影响的预检域（touched）；共享表写字段（writes，见「字段归属」）；是否破坏性，可以固定，也可以按参数判定（`/commands`中`destructive_by_params`为true，例如只有减少库存才算破坏性）；破坏性命令的确认短语由参数算出。`/commands`的`destructive`只表示这条命令可能需要确认，按参数判定的命令以计划的`confirmations`为准；`group`是共享字段的命令组，取操作名的整段前缀（如`view_event`），没有时为null。命令所需的服务在当前世代缺席时，目录标为不可用并给出原因，预览返回503 `admin_command_unavailable`。

一批最多100条命令，依次作用于同一份状态深拷贝；任一步抛错整批零提交，`details`带`step`和`operation`。命令只调用各服务公开的内核，不构造游戏请求。

### 预览

`POST /plans`在当前世代租约内读取存档，用冻结的服务端时间执行整批命令，返回：

- `plan_id`（只有可提交的计划才保存并带ID）、`plan_digest`、`committable`、`expected_revision`、`generation_id`；
- `context`：冻结的UTC与JST时间、原生取得日（与邮件、关卡奖励一样取JST民用日`business_calendar.civil_day`，0点换日，不读宿主时区）、修订版、世代号、各域内容版本和`actor=local-admin`；
- `commands`（已脱敏的规范化参数）、`touched`、`writes`；
- `confirmations`：每条破坏性步骤的`{step, operation, phrase}`；
- `diff`：按业务键组织的差异（单位按UniqueID，持有物按ItemID，邮件按PresentID，表行按主键，其余按JSON Pointer），默认最多500项、单值16 KiB、合计512 KiB，超限截断并带标记；
- `steps`：每步的效果与警告；
- `preflight`、`restart_check`与`audit`：`{"ok": true, ...}`或`{"ok": false, "error": {...}}`；`audit`在预览时就按提交的规则检查审计记录体积（预览时还没有`op_id`，用与真实`op_id`等长的固定占位UUIDv4量），超限时`error.code`为`admin_audit_too_large`（`error.details.fields`列出超限字段的字节数，`error.details.limit`为上限），预检失败时没有可量的记录，记为`{"ok": false, "skipped": "preflight"}`；三项都通过才`committable`；
- `state_sha256_before`/`state_sha256_after`和`auto_backup`（这次提交是否会自动备份）。

计划保存在进程内，最多32份（满了挤掉最旧的），600秒过期。`plan_digest`覆盖规范化参数、冻结的上下文、`expected_revision`、世代号和差异摘要。

### 提交

`POST /commits`依次执行：

1. 按`op_id`只读查询已提交记录：载荷相同就原样返回并标`replayed: true`，不同返回409 `admin_operation_conflict`；这一步不依赖计划是否还在。
2. 取计划：缺失409 `plan_expired`；摘要或修订版与计划不符409 `plan_changed`。
3. 核对确认短语：必须按步骤顺序逐字给出，缺失或不符422 `admin_confirmation_required`，`details.required`列出所需短语。
4. 状态已被游戏改写时409 `admin_revision_conflict`并附`current_revision`。
5. 用冻结时间在当前状态上重放：世代号、内容版本、原生取得日或JST日期变化时409 `plan_stale`（`details.changed`列出变化项）；重放结果与预览不同时409 `plan_changed`。
6. 预检，再做重启兼容检查。
7. 检查审计记录体积，超限422 `admin_audit_too_large`，不备份也不写入。
8. 需要时自动备份，然后在同一个`BEGIN IMMEDIATE`中完成修订版CAS、状态写入和审计。

整批没有改变状态时只写审计，修订版不变，也不备份。结果包含`op_id`、`kind=command`、`plan_digest`、世代号、前后修订版与状态sha256、每步效果、客户端回读请求号（`readback`）、`backup_before_commit`和`replayed`。

`op_id`是规范的小写UUIDv4，界面在拿到确定结果前的重试都沿用同一个；并发提交撞上同一`op_id`时按第1步返回。管理操作不写任何游戏回执、`processed_keys`或CT水位；客户端在途请求重放时会因修订版变化收到`*_retry_stale`，不会重复发放，所以修改存档最好在客户端空闲时进行。

管线其他错误码：`admin_batch_invalid`（批次为空或超过100条）、`admin_command_unknown`、`admin_params_invalid`（`fields`带`step`、`loc`、`type`、`msg`）、`admin_op_id_invalid`、`admin_plan_digest_invalid`、`admin_revision_invalid`、`admin_revision_exhausted`、`admin_command_result_invalid`、`admin_preflight_invalid`。

## 预检与重启兼容检查

预检按批次触及的域，对候选状态编码客户端会读回的响应。任一响应编码失败返回409 `admin_preflight_failed`，`details`为`{response, domains, error}`，内层错误如`response_xml_invalid`、`response_too_large`。成功时`readback`列出各域对应的请求号，即提交后客户端需要重新读取的内容。

| 域 | 读回 | 域 | 读回 |
|---|---|---|---|
| `backgrounds` | 137 | `perused` | 137 |
| `card_status` | 51 | `possessions` | 129、137 |
| `comeback_login` | 33、35、42 | `presents` | 42 |
| `config` | 36 | `purchased_login` | 42、46、47 |
| `crystals` | 43、44 | `quest_records` | `quest_record`直连路由 |
| `daily_gift` | 42 | `quest_state` | `quest_record`直连路由（客户端不读战斗日志本身，改动经关卡记录可见） |
| `event_login` | 42 | `rank_rewards` | 42 |
| `event_pass` | 137、158 | `silver` | 126 |
| `fairy` | 122 | `spending` | 33、35 |
| `fame` | 137 | `squads` | 39、72 |
| `filters` | 137 | `towers` | 66（先按冻结时刻结算塔时钟再编码） |
| `game_param` | 33、34、35 | `units` | 41（含进行中战斗的预留试算） |
| `login_cooking` | 33、35、42 | `universal_flags` | 137 |
| `login_rewards` | 33、35、42 | `vote` | 137 |
| `missions` | 54 | | |
| `packages` | 46、47 | | |

以下五个域不预检，域表里写明理由：`evolution_rewards`（客户端从不读觉醒奖励账本，由重启检查验证）、`gifts`（客户端从不读好感奖励账本，下次使用前由送礼服务验证）、`letstd`（客户端从不读LetsTD账本，奖励只以邮件（42）到达）、`sugoroku`（62每次读取都会按当月重新同步棋盘，存储的棋盘账本由重启检查验证）、`unit_overrides`（客户端从不读`unit_dynamic_overrides`，只有合成、银交换和商店成长的守卫读取）。命令触及这些域时直接沿用域表的理由，不再各自声明。`tower_trade`不在域表里：塔兑换次数只经兑换画面的65/69到达客户端，`tower_trade.rebind_product`在命令自己的`preflight_none`里写明理由，并在同一事务内重读改写后的历史与购买回执。服务缺席的读回不编码，例如没有CardStatus服务时不检查51。预检对少数响应比服务端下发更严，例如42按礼物箱的编码上限、43/44先核对结晶钱包。全域检查约需2.5秒，主要花在`quest_record`；恢复和离线命令都做全域检查。

重启兼容检查把候选状态规范化为存档形式，交给当前世代的`initialize`：必须不抛错且结果完全相等，否则409 `admin_restart_incompatible`，`details.error`给出拒绝原因，或`details.diff`给出会被改写的差异（最多100项、单值4 KiB、合计128 KiB）。因此任何管理提交都不会写出让serve起不来或在下次启动时被改写的存档。

## 客户端闸门与切换栅栏

客户端活动只看原生协议请求（`/api`）和原生资源请求（`/client-assets`），`/health`和`/local/*`不计。需要客户端关闭的操作（运行中恢复、content档运行中切换）要求请求里`client_closed`为true（否则422 `admin_client_closed_required`），并且240秒内没有客户端活动（否则409 `admin_client_active`，`details`给出`idle_seconds`与`quiet_seconds`）。240秒取自客户端的空闲节奏：挂在菜单的客户端每180秒发一次心跳，240覆盖一个心跳周期并留60秒余量；标题与登录画面没有周期请求，任何阈值都区分不了，仍靠`client_closed`确认与之后的重启提示兜底。恢复改变了状态时，闸门在观察到下一次客户端请求之前持续提示重启客户端（`restart_required`和`notices`）；content切换的告警见「切换」。

带状态迁移的切换和运行中恢复在切换栅栏内完成：栅栏期间新的游戏请求最多等待10秒，超时返回503 `generation_switching`；在途请求最多排空5秒，超时放弃本次操作并返回409 `switch_drain_timeout`。换代后最多保留一个仍在服务旧请求的排空中世代，它30秒后仍未排空时，下一次换代返回409 `generation_draining`。同一时间只允许一个栅栏（409 `generation_switch_busy`）。

## 自动备份与保留

自动备份在所有检查（含审计体积）通过之后、写入之前进行，并且只在这次写入会改变存档时进行：自动备份只保留1份，不改变存档的操作不能替换它。

- 命令提交：只有确实改变状态，且含破坏性步骤或多于一步时才备份；单条按参数判定为非破坏性的命令（如只增加库存）既不需要短语，也不备份。
- 运行中恢复：备份内容与当前状态不同时，在切换栅栏内、写入前备份当前库；相同时不备份，只写审计（预览的`auto_backup`给出是否会备份）。
- gated与content档的运行中切换：迁移或初始化会改写存档时，进入栅栏前备份（报告的`requirements.backup`为true）；存档不变时不备份。栅栏内重新评估时存档变得需要改写、而进栅栏前没有备份（修订版已核对未变，只有期间的时间推移会造成这种情况），返回409 `config_report_stale`，`details.changed`为`["state_sha256"]`，要重新校验。
- 离线恢复：备份内容与当前存档不同时应用前备份；相同时不备份、不推进修订版，只写审计。
- 离线料理迁移：应用前总是备份（没有改动时不生成计划）。

同一`op_id`的重放不再备份。备份失败返回503 `admin_backup_failed`，不写入任何状态。

自动备份只保留最新1份，超过1天即删除，因此可能一份不剩；手动备份永不自动删除。清理在创建任何备份和列出备份时进行，只删除数据目录内按`admin-auto-<32位hex>.sqlite3`命名的文件，其他路径只撤销登记，旁边还有日志文件的备份留到之后再删。`GET /backups`的`retention`给出`auto_keep: 1`、`auto_lifetime_seconds: 86400`、`manual_pruned: false`。手动备份用`POST /backups {"reason"}`，原因为1–200个可打印字符。

备份用SQLite在线备份接口复制到备份目录（默认`<data_dir>/backups`，必须在数据目录内，否则400 `admin_backup_path_invalid`）下的`admin-{auto,manual}-<32位hex>.sqlite3`（30秒内必须完成），改成非WAL模式并与运行中档案逐行比对，再按读取备份的同一套规则复核（应用ID、`user_version`为1、`integrity_check`、存档JSON不得有重复键、NaN/Infinity、溢出成无穷的数字或超过64层嵌套）后登记sha256。登记字段为`kind`（auto或manual）、`reason`、`op_id`、`path`、`sha256`、`revision`、`created_at`。读取不满足这套规则的备份返回400 `admin_backup_invalid`；实时状态本身不满足时备份返回503 `admin_backup_failed`，不留文件也不登记，需要先自动备份的提交、恢复和切换随之被拒。服务端写存档始终经过`allow_nan=False`的序列化，正常存档不会出现这种情况。

## 恢复

恢复分预览和执行两步，只接受数据目录（`runtime/server`）内的备份，拒绝运行中的库文件本身（400 `admin_backup_path_invalid`）；`backup`可以是绝对路径，也可以是相对数据目录的路径；旁边有日志文件的备份返回409 `admin_backup_busy`。

预览返回`restore_digest`、`expected_revision`、世代号、当前`player_id`、备份信息（路径、sha256、修订版、`player_id`）、`cross_account`、前后状态sha256、`auto_backup`（执行时是否先自动备份，即备份内容是否与当前状态不同）、所需确认短语、差异，以及五项检查（`energy_clock`、`battle`、`restart_check`、`preflight`、`audit`）和`committable`。`audit`按执行时要写的审计记录检查体积，预检失败时记为`{"ok": false, "skipped": "preflight"}`；五项都通过才`committable`。

执行请求：

```json
{"backup":"backups/admin-manual-....sqlite3","restore_digest":"<预览返回>","expected_revision":5,
 "op_id":"<UUIDv4>","client_closed":true,"confirmations":["RESTORE 5"]}
```

顺序为：按`op_id`查重 → 读备份，核对修订版（`admin_revision_conflict`）与预览摘要（`plan_changed`） → 能量时钟标记检查 → 客户端闸门 → 确认短语`RESTORE <修订版>`，跨账号时第二条为`restore another account` → 备份中的进行中战斗检查 → 用当前世代`initialize`做重启兼容检查 → 全域预检 → 进入切换栅栏，再次核对世代（`plan_stale`）与修订版 → 检查审计记录体积（超限422 `admin_audit_too_large`，不备份也不写入） → 状态会变化时自动备份（原因`before restore`） → 以`kind=restore`写入状态与审计。修订版继续递增，不替换运行中的库文件；备份内容与当前状态相同时不备份、修订版不变，只写审计，结果的`backup_before_restore`为null。自动备份只保留最新1份，恢复来源若是自动备份会被这次新备份清理，所以备份放在栅栏内、所有可能拒绝恢复的检查之后。

备份中的进行中战斗与运行世代一致（开战时冻结的主表快照与资源版本都与当前世代相同）时不阻断恢复：恢复后这场战斗照常结算。对不上运行世代时返回409 `admin_battle_abort_required`，`details.battle`列出战斗身份，先用`quests.journal.abort`中止再恢复。content切换不看世代一致性，只要有进行中的战斗就拒绝（见「切换」）。缺少能量时钟标记的旧备份返回409 `admin_restore_clock_required`，要先用`energy-clock-plan`/`energy-clock-apply`转换。`runtime/backups`下的两份旧备份位于数据目录之外，修订版为0且缺能量时钟标记，恢复前要先放进`runtime/server`并完成转换。

## 离线命令

以下命令都要求服务端已停止，数据库固定为`<data-dir>/aigis.sqlite3`，自动备份默认写入`<data-dir>/backups`（`--backup-directory`可改，但必须在数据目录内），并接受serve的全部规则参数以及`--asset-dir`、`--resource-dir`。数据库文件不存在时，plan和apply都回400 `admin_offline_path_invalid`（消息`Offline commands need the existing local database file`，恢复与签到料理迁移共用）；路径经过链接/junction时plan同样回这个错误，apply则在核对计划所指的数据库时先以`ValueError`拒绝；数据库旁有`-wal`、`-shm`或`-journal`时先报409 `admin_offline_database_busy`；确认已停服后加`--checkpoint`执行`PRAGMA wal_checkpoint(TRUNCATE)`清理，仍被占用就中止，不会破坏数据。库能读出之后，命令先按serve的规则前滚配置集与主表指针（见「前滚」），再按前滚后的配置集完整构建一个世代，用其`initialize`做重启兼容检查，并用真实预检编码全部读回响应，所以离线命令看到的配置与下一次启动相同。计划必须写到独立的`.json`文件，且只能用于生成它的数据目录；计划被改动返回400。命令失败时以1退出并输出`{"error": "<异常类型>", "message": ...}`：本节带错误码的拒绝显示为`ProtocolError`或其子类（如重启兼容检查与预检的`AdminError`、配置解析的`ConfigError`），另带`code`与`status`，即本节列出的错误码与状态码；计划输出路径或数据目录不符这类参数错误显示为`ValueError`，不带这两项。

恢复：

```powershell
python -m aigis_ps.cli admin-restore-plan --data-dir <数据目录> --backup <备份.sqlite3> --output <计划.json> [--checkpoint]
python -m aigis_ps.cli admin-restore-apply --data-dir <数据目录> --plan <计划.json> [--backup-directory <目录>] [--confirm "restore another account"] [--checkpoint]
```

plan读取当前库（读不出时503 `admin_offline_database_invalid`）和数据目录内的备份，检查能量时钟标记（缺标记时409 `admin_restore_clock_required`）、重启兼容和预检，写出精确计划：数据库路径、当前修订版与状态sha256、备份路径与sha256、目标状态sha256、差异与摘要、是否跨账号。apply复核计划完整性（`admin_restore_plan_invalid`）；跨账号必须带`--confirm "restore another account"`（409 `admin_restore_confirmation_required`）；库或备份在出计划后变化返回409 `admin_restore_stale`；通过后，状态会变化时自动备份（原因`before offline restore`）并以`kind=restore_offline`写入状态与审计；备份内容与当前存档相同时不备份、不推进修订版，只写审计，结果的`backup_before_restore`为null。重复执行（包括状态不变的恢复）返回`already_applied`。修订版已到上限时，会改写存档的恢复在plan时回409 `admin_revision_exhausted`，状态不变的恢复不受此限。离线恢复不检查客户端闸门和进行中战斗，只靠重启兼容检查拦下下一次启动会拒绝的存档。

签到料理迁移：

```powershell
python -m aigis_ps.cli login-cooking-migrate-plan --data-dir <数据目录> --output <计划.json> [--checkpoint]
python -m aigis_ps.cli login-cooking-migrate-apply --data-dir <数据目录> --plan <计划.json> [--backup-directory <目录>] [--checkpoint]
```

plan按下一次启动的资源给出的签到料理阈值重算本月额度（已结束的月份只换阈值），验证下一次启动会原样接受迁移后的存档、全部读回都能编码，再写出计划。apply重新核对阈值、修订版、存档摘要和整份计划，全部一致才自动备份（原因`before offline login cooking migration`）并以`kind=login_cooking_offline`写入审计；被拒时既不备份也不写库，重复执行返回`already_applied`。常见拒绝：

- 不需要迁移：409 `admin_login_cooking_current`，没有改动就不生成计划文件；
- 新额度低于本月已发份数：409 `login_cooking_quota_below_issued`；
- 登录奖励资源不可用：503 `login_reward_service_unavailable`，下一次启动的阈值未知；
- 存档还没有料理账本：503 `login_cooking_required`，这类存档下次启动会按新阈值建账，不需要迁移；
- apply时资源阈值、存档或迁移结果与计划不同：409 `admin_cooking_plan_stale`，消息以「plan the migration again」结尾；
- 计划被改动：400 `admin_cooking_plan_invalid`。

`energy-clock-apply`在同一事务里写一条`kind=energy_migration`的审计。

## 导出

导出写到`<data_dir>/exports/admin-export-<hex>.json`，只返回路径、字节数、sha256、修订版和创建时间，不触发浏览器下载；写不进数据目录时503 `admin_export_failed`。内容为带`"importable": false`标记的脱敏JSON（`format`为`aigis-ps-admin-export`），附世代号、`player_id`、修订版与状态sha256：`user_key`、`method_key`、`bootstrap_token`打码，缓存报文中的SYSTEM.ExchangeKey等密钥打码，无法解析的报文只保留字节数与sha256。导出不是SQLite库，恢复接口会拒绝它，也没有任何导入入口。

## 审计与失败记录

`admin_operations`与`profiles`在同一个SQLite文件、同一个事务中提交，记录`op_id`、游标序号、种类（`command`、`restore`、`config`、`restore_offline`、`login_cooking_offline`、`energy_migration`）、`actor=local-admin`、脱敏的操作参数、`payload_digest`（`op_id`与计划摘要的sha256）、目标、前后修订版、前后状态sha256、有长度上限的脱敏差异（最多200项、单值2 KiB、合计64 KiB）、世代号、内容版本和结果；配置切换另记切换序号。每个JSON字段不超过1 MiB：命令提交、运行中恢复和gated/content切换在自动备份之前先检查体积，超限返回422 `admin_audit_too_large`，不备份、不写入，以免占掉唯一一份自动备份，写入时再连同备份登记量一次；计划预览（`audit`）、恢复预览（`checks.audit`）与配置切换报告（`checks.audit`）也按同一规则先量一次，超限时不可提交。

提交、恢复、手动备份、导出、配置切换与撤销、主表导入与撤销失败时写入`admin_failures`（最多保留2000行，消息最多1000字，明细最多64 KiB），含错误码、状态码和可识别的`op_id`；记录失败本身不会替换返回给界面的错误。审计不随请求记录裁剪，也不保存UK、MK、令牌或平台凭据。

`admin_operations`、`admin_failures`、`config_generations`、`backups`四张表只在第一次管理写入（或`energy-clock-apply`）的事务里创建，同时会出现SQLite自带的`sqlite_sequence`表；`user_version`保持1，`profiles`、`requests`表结构不变，旧版服务端照常打开和读写。

脱敏规则：`user_key`、`method_key`、`bootstrap_token`显示为`[redacted]`；缓存报文中的SYSTEM.ExchangeKey、LI.UK、LGI.A7打码；回执报文默认只显示字节数与sha256。视图、审计、失败记录、请求记录和计划都经过同一套脱敏。缓存报文的识别与`alxml.loads`的读取规则一致：文本在可选的UTF-8 BOM之后跳过空白、注释和处理指令，以`<?xml`或`<DA`开头的按缓存报文处理，`response`字段里的base64报文按解码后的字节同样判断；状态浏览、视图、审计差异和导出都按这一规则摘要或打码。

## 配置集与切换

### 配置来源

配置集是不可变目录`<data_dir>/config/sets/<24位hex>/`：显式设置的键各存一个规范化JSON，另有`manifest.json`，按写入时的注册表列出每个键的`mode`（default或explicit），显式键另记sha256和规则内容版本，清单还记`source`、`parent`、`created_at`；配置集的ID由显式文档的摘要决定。`<data_dir>/config/active.json`记录配置集、清单sha256与递增的切换序号，用独占锁加原子替换切换；`staging/`放校验中的文档。所有路径都经`checked_path`限制在数据目录内，拒绝符号链接和目录联接。

解析时以`active.json`为基线；命令行规则文件参数逐键覆盖，只对当前进程有效，被覆盖的键在控制台只读（`read_only=true`，配置集同时有值时`shadowed=true`），校验请求改到这些键返回409 `config_key_overridden`，启动后命令行文件在磁盘上变了返回409 `config_override_changed`，要重启服务才生效。没有`active.json`时全部取代码默认值；`mode=default`的键随代码默认值变化，世代身份里冻结的是解析后的实际值。示例文件永远不会自动套用，只登记在注册表的`examples`里，供界面作为模板显式载入。

文档先经严格预解析：utf-8-sig、尺寸上限、重复键（报错带JSON Pointer路径）、顶层必须是对象、数字有限、嵌套不超过64层，`bootstrap_token`这类本地生成的字段不能配置（`config_secret_field`）；规范化后写入staging，再交给现有加载器完成同一套校验。错误码包括`config_duplicate_key`、`config_json_invalid`、`config_not_object`、`config_too_large`、`config_encoding_invalid`、`config_number_invalid`、`config_secret_field`、`config_rules_invalid`、`config_file_unreadable`和`config_key_unknown`。

`config_nesting_too_deep`（400）同时覆盖两种来源：配置集文档超过64层，以及命令行传入的规则文件在加载器里嵌套超过64层（含解析时的RecursionError；命令行文件不经严格预解析）。控制台的配置描述把这类错误标在对应键的`error`上，不会整体失败。

配置集清单列出当前注册表不认识的键、文件与清单不符或清单损坏时，返回503 `config_set_invalid`；经`active.json`读取时报503 `config_pointer_invalid`，消息里带原因，serve因此拒绝启动，doctor的`config`节报错。每份清单都按写入时的注册表列出全部键（包括default模式），所以代码回退到注册表键更少的版本后，新版本创建的所有配置集（包括全默认的空配置集）都会被拒；启动时的前滚还会把指针重新指向最新一次切换记录的配置集，删掉`active.json`也绕不过去。这是有意的失败即关，不会在不认识的键上静默改用默认值；恢复办法是回到认识这些键的代码版本。从未经控制台切换过配置的数据目录不受影响。

### 注册表

| 键 | 命令行 / PowerShell | 时间语义 | 消费域 |
|---|---|---|---|
| `rules` | `--rules` / `-RulesFile` | new_save | bootstrap |
| `quest-rules` | `--quest-rules` / `-QuestRulesFile` | frozen_per_operation，`charisma_recovery_seconds`为time_accumulating | entrust、login_rewards、main_quests、recovery、sugoroku |
| `training-rules` | `--training-rules` / `-TrainingRulesFile` | frozen_per_operation | entrust、evolution、fame、missions、skin_shop、training、unit_detail |
| `silver-house-rules` | `--silver-house-rules` / `-SilverHouseRulesFile` | frozen_per_operation | entrust、evolution、fame、missions、silver_house、skin_shop、training、unit_detail |
| `evolution-rules` | `--evolution-rules` / `-EvolutionRulesFile` | frozen_per_operation | entrust、evolution、fame、missions、skin_shop、trades |
| `recovery-rules` | `--recovery-rules` / `-RecoveryRulesFile` | frozen_per_operation | login_rewards、recovery |
| `trade-rules` | `--trade-rules` / `-TradeRulesFile` | eligibility | trades |
| `ticket-rules` | `--ticket-rules` / `-TicketRulesFile` | eligibility | comeback_logins、daily_gifts、entrust、event_logins、login_rewards、packages、purchased_logins、rank_rewards、serial_codes、summon_tickets |
| `item-ticket-rules` | `--item-ticket-rules` / `-ItemTicketRulesFile` | eligibility | comeback_logins、daily_gifts、entrust、event_logins、event_passes、fame、item_summon_tickets、letstd、login_rewards、main_quests、missions、packages、purchased_logins、rank_rewards、serial_codes、skin_shop |
| `login-rules` | `--login-rules` / `-LoginRulesFile` | first_baseline，`cooking_item_id`、`cooking_sender`为eligibility | login_rewards |
| `event-pass-rules` | `--event-pass-rules` / `-EventPassRulesFile` | eligibility | event_passes、rank_rewards |
| `event-pass-revival` | `--event-pass-revival` / `-EventPassRevivalFile` | resource（涉及资源） | entrust、event_pass_revival、event_passes、fame、missions、native_cdn、packages、rank_rewards、skin_shop |
| `event-login-rules` | `--event-login-rules` / `-EventLoginRulesFile` | eligibility（涉及资源） | comeback_logins、event_logins、login_rewards、native_cdn、packages、purchased_logins、rank_rewards |
| `comeback-login-rules` | `--comeback-login-rules` / `-ComebackLoginRulesFile` | eligibility | comeback_logins、login_rewards、rank_rewards |
| `purchased-login-rules` | `--purchased-login-rules` / `-PurchasedLoginRulesFile` | eligibility（涉及资源） | login_rewards、native_cdn、packages、purchased_logins、rank_rewards |
| `daily-gift-rules` | `--daily-gift-rules` / `-DailyGiftRulesFile` | eligibility | daily_gifts、login_rewards、rank_rewards |
| `vote-rules` | `--vote-rules` / `-VoteRulesFile` | eligibility | entrust、login_rewards、main_quests、rank_rewards、vote_assault_notifications、vote_cooking、vote_cooking_notifications、vote_expiry_notifications、votes |
| `vote-cooking-rules` | `--vote-cooking-rules` / `-VoteCookingRulesFile` | eligibility | login_rewards、rank_rewards、vote_cooking、vote_cooking_notifications、votes |
| `entrust-rules` | `--entrust-rules` / `-EntrustRulesFile` | frozen_per_operation | entrust |
| `mission-windows` | `--mission-windows` / `-MissionWindowsFile` | resource（涉及资源） | entrust、main_quests、mission_windows、native_cdn、subjugation |
| `command-text-overrides` | `--command-text-overrides` / `-CommandTextOverridesFile` | resource（涉及资源） | entrust、native_cdn |
| `tower-rules` | `--tower-rules` / `-TowerRulesFile` | frozen_per_operation，`generation_hour`、`replenish_hour`为time_accumulating，`first_access_settles_day`为first_baseline | main_quests、towers |
| `subjugation-rules` | `--subjugation-rules` / `-SubjugationRulesFile` | frozen_per_operation | main_quests、subjugation |
| `serial-code-rules` | `--serial-code-rules` / `-SerialCodeRulesFile` | eligibility | serial_codes |
| `letstd-rewards` | `--letstd-rewards` / `-LetsTDRewardsFile` | eligibility | letstd、login_rewards、rank_rewards |

`GET /config`和`rules.config`视图给出注册表的实时描述（默认值、生效值、来源、内容版本、尺寸上限、示例文件），以它们为准。「涉及资源」只是界面提示，是否真的改了资源以世代身份比较为准。

消费域列出指纹直接或经其他服务的内容版本嵌入该规则的readiness域，传递消费者也在内：例如`ticket-rules`经礼物箱进入DailyGift、回归、活动签到与购买日奖这几个115/116子服务、序列码与116的基础摘要，委任又嵌入116的基础摘要，所以entrust也列在其中（LetsTD只嵌入礼物箱的物品目录与通用道具召唤券版本，所以不在其中）。消费域只用于注册表描述和切换报告（报告逐键给出`domains`），不参与档位判定；切换总是构建完整的新世代，各域内容版本的实际变化以报告的`content_versions`为准。

### 换代分档

候选世代与当前世代的身份比较，加上候选`initialize`在当前存档上的干跑结果，决定切换档位：

- content：代码摘要、客户端指纹、主表快照、基础或有效资源版本任一变化。客户端从准备好的文件读取这些内容，代码也只随进程重载，所以都要重启客户端。
- gated：有time_accumulating字段变化、候选`initialize`会改写存档，或需要签到料理迁移、新手奖励总数重建。
- hot：其余情况，只换服务端规则。

`quest-rules.charisma_recovery_seconds`变化时，先由运行中的世代按旧速率结算能量，再换代，结算结果与审计同一事务提交。`tower-rules`的`generation_hour`或`replenish_hour`变化时同样先由运行中的世代按旧规则结算塔时钟（存档还没有`tower_state`或运行中世代没有塔服务时，没有可结算的天数，塔记录在之后的66/67/68或塔开战中按新小时建立），再换代；这次结算与66/67/68一样，会为当时已开放、尚无记录的塔按旧规则建立记录。每项结算记在报告的`migration.energy`与结果的`energy`中，给出键、字段、前后值和结算时刻。今后新增的time_accumulating字段若没有对应的迁移，校验报告的`migration`检查失败（`config_migration_unsupported`），两种模式都不能切换。资源或主表变化使候选世代的签到料理阈值与存档账本冻结的阈值不同时，评估会在候选初始化之前按切换时刻迁移料理账本：账本月、消费月和切换时刻同属一个业务月时按存档上月消费重算本月额度，否则只换阈值，额度在下次月切时按新阈值重置；报告的`reasons.cooking`为true，`migration.cooking`给出`period`、`issued`、阈值与额度的前后值和`migrated_at`。本月已发份数高于新额度（`login_cooking_quota_below_issued`）或切换时刻早于存档时间水位（`login_cooking_clock_backwards`）时`migration`检查失败。候选世代的新手奖励总数与存档签到账本冻结的总数不同时，评估同样在候选初始化之前按`login_rewards.rebase_beginner`的规则重建账本（已领数高于新总数时封顶）；报告的`reasons.beginner`为true，`migration.beginner`给出`total`与`claimed`的前后值。

### 校验作业

`POST /config/validations`：

```json
{"changes":{"quest-rules":{"...":"..."},"trade-rules":null},"base_set":null,"op_id":"<UUIDv4>"}
```

`changes`把键映射为完整文档，`null`表示删除显式设置、回到默认；`base_set`缺省时取运行中的配置集。先严格解析并在staging生成不可变的候选配置集，再由运行时在后台构建候选世代（单一构建槽，忙时409 `generation_build_busy`；约需16秒，期间会争抢解释器，游戏请求可能短暂变慢；候选保留600秒）。同一`op_id`和相同请求返回已有作业，请求不同返回409 `admin_operation_conflict`；新的校验会替换旧作业。切换取走候选后、提交写库时被拒（如审计记录超限、修订版冲突），hot、gated、content与stage都会把候选世代归还原处并保留原有的600秒期限，可以直接重试、不用重建；其间已有新候选或新构建完成时，旧候选不再恢复。

`GET /jobs/{job_id}`返回作业进度；构建成功且候选仍在时附切换报告，未知或已被替换的作业404 `config_job_unknown`，候选过期时`candidate_expired`为true。报告包含：

- `report_id`、`revision`、运行中的配置集、active指针、暂存状态、主表计划；
- 当前与候选世代的身份，以及候选的启动闸门问题；
- `tier`、`identity_changes`、`reasons`（`identity`、`accumulating`、`initialize`、`cooking`、`beginner`）和逐键变化（来源、模式、规则、内容版本、时间语义）；
- `migration`：time_accumulating字段的结算（`energy`，能量与塔时钟）、料理迁移、新手奖励重建（`beginner`：`total`与`claimed`的前后值）、存档差异与前后sha256；
- `notices`：不阻断的提示，不进`blockers`，也不改档位，没有提示时为空数组；目前只有候选世代下被换用的塔兑换商品：`{"code": "tower_trade_product_reused", "reused_products": [...], "refused_operations": [65, 69], "repair": "tower_trade.rebind_product", "client": ...}`，即改绑之前65与69整体拒绝（不只是被换用的商品）、客户端弹通信错误并原包重发；候选没有塔兑换服务、候选初始化失败或兑换历史读不出时没有这条；
- `checks`：`serve_gate`、`master_pin`、`migration`、`restart_check`、`preflight`（带`readback`）和`audit`；`audit`与计划预览的`audit`形状相同，生成报告时按默认模式的切换审计记录量一次体积，超限时apply与stage都被阻断，预检失败时没有可量的记录，为`{"ok": false, "skipped": "preflight"}`；
- `battle`：是否有进行中战斗、本档是否要求没有战斗；
- `default_mode`（content档为stage，其余为apply）以及`modes.apply`/`modes.stage`各自的`allowed`、`blockers`、`confirmations`；
- `requirements`（是否备份：gated/content档且存档会变化时才为true；是否进栅栏；是否要求客户端关闭）、被命令行覆盖的键、各域内容版本新旧值与`receipts_invalidated`、逐域可用性的倒退与变化；
- content档的`client_actions`：要求重启客户端、配置集目录，以及`client/prepare-client.ps1`的参数与完整命令；
- 当前的客户端闸门状态（`client`，含静默阈值`quiet_seconds`）。

同一状态下最多缓存8份报告；世代、指针、主表、修订版任一变化都会生成新报告。

### 切换

`POST /config/switches`：

```json
{"report_id":"<64位hex>","op_id":"<UUIDv4>","mode":"apply","confirm":{"client_closed":true,"confirmations":["APPLY CONTENT 5"]}}
```

`mode`缺省时取报告的`default_mode`。执行前核对：运行中世代、active指针与主表指针都没变（否则409 `config_report_stale`，`details.changed`列出变化项）、候选仍在（409 `generation_candidate_missing`）、报告列出的阻断项、存档现在需要的档位没变（409 `config_tier_changed`）、切换序号没有倒退（409 `config_sequence_regressed`）。同一时间只允许一个切换（409 `config_switch_busy`，指针文件锁被占用时409 `config_pointer_busy`）。

- hot（apply）：不备份、不进栅栏、不核对修订版；前一个旧世代仍在排空时409 `generation_draining`。写一条`kind=config`的审计与切换记录，换引用，再原子替换`active.json`。换代前已取得租约的请求在旧世代内完成。
- gated（apply）：先检查审计体积，存档会变化时自动备份，进入切换栅栏，再次核对世代、主表、修订版和评估结果，在同一事务里提交迁移后的存档、审计与切换序号，然后换引用、替换`active.json`。有time_accumulating字段变化时要求没有进行中的战斗。
- content（apply）：在gated的基础上要求客户端闸门和确认短语`APPLY CONTENT <修订版>`，并要求没有进行中的战斗。切换后`GET /config`的`client_warning`持续给出`client_actions`，直到观察到客户端启动序列请求（31、114、142、152）；应按其中的命令重新运行`prepare-client`并重启客户端。
- stage：只写切换记录（档位记为`staged`）和`active.json`，下次启动生效，不备份。有time_accumulating字段变化、需要料理迁移或新手奖励重建时拒绝（409 `config_stage_unsupported`，`details`给出字段、前后阈值或前后总数）：已累积的时间要由运行中的旧世代按旧规则结算，所以time_accumulating字段只能在运行中apply；料理账本则是运行中的旧世代在115拒绝已迁移的账本、下次启动后115会拒绝未迁移的账本，要在运行中apply，或停服后用离线料理迁移；新手奖励账本同样要随切换在运行中重建。停服改了料理阈值或新手奖励总数再启动的，存档照常加载，但115在迁移之前拒绝、客户端弹窗重发，要先用控制台的`login_cooking.migrate`或`login_rewards.rebase_beginner`迁移。

结果包含`mode`、`tier`、`report_id`、新旧配置集、换代前后的世代号、变化的键、失效的回执内容、`client_actions`、主表激活，gated/content档另有前后修订版与状态sha256、time_accumulating字段的结算（`energy`）、料理迁移（`cooking`）、新手奖励重建（`beginner`：`total`与`claimed`的前后值）、`readback`和`backup_before_switch`（存档不变时为null）；`pointer`给出指针现状，`published`为false时`publish_error`说明原因。提交之后指针没能移动时切换本身不报错：配置指针在下次启动时前滚；主表指针在下一次不指定快照的切换时补发，否则同样在下次启动时前滚。

存在已暂存、尚未生效的配置集时，切换到其他配置集（包括再暂存另一个）都返回409 `config_staged_pending`，`details.staged_set`给出暂存的配置集；暂存的主表同样阻止不带该快照的切换（`details.staged_master`）。`POST /config/unstage {"staged_set","op_id"}`把指针改回运行中的配置集；暂存已不存在返回409 `config_not_staged`，暂存的已是另一个配置集返回409 `config_staged_changed`。

hot切换后，客户端在途请求重放会因内容版本变化收到`*_retry_stale`（409），这是预期行为；pending账本可能因定义变化转为`*_definition_changed`而挂起，用「迁移」节的对应命令处理（如`*.resolve_changed`、`login_rewards.resolve_pending_stamps`、`missions.rebuild_changed`、`event_pass.rebind_definition`、`votes.adopt_*`），LetsTD用「LetsTD」节的`letstd.rearm`。配置切换不写游戏回执。

### 前滚

serve启动时，如果库中最新的已提交切换序号大于`active.json`的序号（或指针缺失、不可读），先把指针前滚到该配置集再构建首代；最新一次切换记录了主表激活、而主表指针仍是替换前的字节时，也先把主表指针前滚到目标快照。提交后任何时刻崩溃都不会出现新状态配旧配置。离线命令在构建世代之前按同一规则前滚。主表指针不可读或目标快照损坏时启动失败，这时用`import-masters`重新导入并激活一份采集即可恢复。

### 主表暂存

控制台只列出数据目录同级的`captures`目录（默认就是采集工具写入的`runtime/captures`），采集按目录名引用，名称只允许字母、数字和`. _ -`，链接和目录联接一律拒绝，不联网。`POST /content/imports`把一份已完成的采集做成不可变快照，放到`runtime/server/masterdata/snapshots/<24位hex>/`，不移动`masterdata/active.json`；同一份采集重复导入得到同一个快照。采集不完整、与客户端指纹不符、清单损坏或嵌套过深时返回422 `content_capture_invalid`，目录不存在404 `content_capture_unknown`，读取采集或写入数据目录出现I/O失败（磁盘满、权限不足、文件被占用）时返回503 `content_import_failed`，排除问题后可以直接重试。命令行`import-masters`默认导入后立即激活，加`--stage`时与控制台一样只暂存。

`GET /content`把「在用」（当前世代的主表快照）和「磁盘active」（`masterdata/active.json`）并排列出，并标出下次启动会用哪个快照、是否已暂存、是否在等待前滚。激活时用`POST /content/validations`提交，参数与配置校验相同再加`master_snapshot`，可以在同一次校验里同时改配置；然后经`/jobs`拿报告，再用`/config/switches`提交。主表变化属于content档，默认只暂存、下次启动生效；运行中应用的条件与要求同content档。构建器没有按钉住的快照构建时，报告的`master_pin`检查报`config_candidate_master_mismatch`；新主表如果去掉了存档引用的关卡，会被重启兼容检查拦下。

主表一旦暂存，不带该快照的切换都返回`config_staged_pending`。`POST /content/unstage {"staged_snapshot","staged_set"?,"op_id"}`撤销暂存：只撤主表时保留下次启动本来要用的配置集，带`staged_set`时连同配置集一起撤。服务运行中用命令行直接激活新主表，同样会被视为等待下次启动的已暂存主表。快照没有删除入口。

## 视图

`GET /views`列出全部视图及其参数Schema；`GET /views/{name}?params=<JSON>`在同一个世代租约内读取一份存档快照，响应为`{view, revision, generation_id, server_now, data}`。普通视图的ETag为`"<revision>:<generation_id>"`，支持`If-None-Match`返回304；标为实时的视图跟随服务端时钟、磁盘文件或管理表变化，不带ETag，不要用`If-None-Match`轮询。视图从不调用会写库的读路由，输出全部脱敏；投影结果标`projected=true`、`persisted=false`，不落库。分页参数为`offset`≥0、`limit` 1–500（默认100）；历史视图用`cursor`（1到2^63−1）和`limit`（1–200，默认50）。错误：未知视图404 `admin_view_unknown`，参数非法或给不收参数的视图传参422 `admin_params_invalid`，JSON Pointer格式错误422 `admin_pointer_invalid`，位置不存在404 `admin_pointer_missing`（消息指出第一个不存在的位置），所需服务缺席503 `admin_view_service_unavailable`；多数视图的分节以`{"ok": ...}`形式各自报告，单节失败不影响其余部分。

| 视图 | 实时 | 内容 |
|---|---|---|
| `runtime.generation` | 是 | 世代身份、闸门问题、运行时状态、客户端闸门 |
| `runtime.config` | 是 | 配置指针（运行中、active、暂存）与主表状态（`masterdata`：`in_use`、`disk`、`error`、`roll_forward_pending`、`next_start`即下次启动会用的快照，`staged`为与在用快照不同时的`next_start`，否则为null） |
| `runtime.readiness` | 是 | 逐域readiness的顶层字段，启动诊断实时合并；完整明细用`/readiness/{domain}`；`auto_requests`域的`ready`在客户端自动发送的号有缺口、协力大討伐行读不出或大討伐目录读不出（此时`cooperative_subjugations`为null）时为false，用`/readiness/auto_requests`可看到`operations`、`unhandled`与`cooperative_subjugations`；`summon_schedule`报告70/71的空调度，`shop_window_closure`报告108关窗覆盖（没有覆盖时为`enabled=false`） |
| `runtime.operations` | | 已配置处理器、已登记接口数、未实现的接口号 |
| `runtime.requests` | 是 | 最近的游戏请求（`limit` 1–200），已脱敏 |
| `runtime.versions` | 是 | `runtime/client/client-manifest.json`与当前世代的客户端指纹、资源版本、覆盖层（`event_pass_revival`、`event_login_revival`、`purchased_login`、`mission_windows`、`command_texts`、`shop_window_closure`；关窗记录没有`rules_hash`，恒为null）对照，在用主表与磁盘active对照；清单按严格JSON读取，重复键、NaN或嵌套过深时`valid`为false，文件不存在时为null |
| `state.browse` | | 按JSON Pointer分页浏览存档；嵌套或超过500项的容器只给类型与大小，附所在账本读函数的结构校验结果 |
| `state.fields` | | 字段字典与共享字段归属，位语义分verified、named、unknown三级 |
| `history.audit`、`history.failures`、`history.generations` | 是 | 审计、失败记录、配置切换历史 |
| `account.summary` | | game_param解码与锚点、等级、结晶分桶与合计、月消费、月容量、能量时钟标记、业务日历标记（顶层`business_calendar`；旧档没有此键，`marker`为null，表示其日期按旧宿主日历写入，只报告不阻断；标记损坏时该节报`business_calendar_invalid`） |
| `account.projection` | 是 | 按服务端时间推算的能量（不落库）、逻辑日（JST 04:00换日的业务日）与原生取得日（JST民用日） |
| `items.holdings` | | 持有物：名称、Stack、分类、Enabled及对应命令 |
| `items.catalog` | | 按分类（可用`item_class`筛选）列出物品定义与持有量 |
| `presents.mailbox` | | 邮件逐行解码（模板、池、卡或物品），PPT.A9通知类别只读；`bundled_tickets`列出仍装着多张券、要用`presents.split_ticket_mail`拆分的邮件ID |
| `presents.backlog` | | 战斗待发邮件，只读，只由下一次战斗结算投递 |
| `presents.receipts` | | 领取回执，报文只保留字节数与sha256 |
| `progress.missions` | 是 | 任务进度投影（周期、页签、可领、未读）与脱敏的任务历史 |
| `progress.rank_rewards` | | 等级奖励的基线、观察等级、待发及原因、已发 |
| `progress.login_rewards`、`progress.login_cooking` | | 登录奖励账本与暂存印章、签到料理账本（`cooking_sender`为affection时料理账本另有`last_sender`，即最近一次发件的送出者CardID）；两个视图另按当前世代核对存档冻结的规则：`progress.login_rewards`的`rules`节给出`saved_total`（没有登录账本时为null）、`current_total`与`changed`，不符时附`refused_operations`（[115]）、`repair`（`login_rewards.rebase_beginner`）与`client`；`stamp_capacity`节给出待发印章的`count`、`capacity`、`remaining`与`warning`，满额时附`refused_operations`与`code`（`login_stamp_pending_capacity`），这时只有需要新记一条待发印章的115被拒；`progress.login_cooking`的`rules`节给出`saved_border`（没有料理账本时为null）、`current_border`与`changed`，不符时附`refused_operations`、`repair`（`login_cooking.migrate`）、`offline_repair`与`client`。这几节除各带`ok`外与doctor的`readiness.saved_login_rewards`同形，各自降级：当前世代没有登录奖励服务时只有它们报`admin_view_service_unavailable`，账本照常显示；规则不符时`progress.login_cooking`顶层的`ok`仍为true，由`rules.changed`表达 |
| `settings.config` | | 配置行逐字段，旁注归属命令；无命令或只读的字段标出原因 |
| `settings.event_view` | | EventViewRequest原值与解析 |
| `settings.flags` | | GF通用标志（含名称哈希）与服装商店购买标志对照 |
| `settings.perused`、`settings.backgrounds` | | 已读标志；背景行与只读诊断 |
| `settings.card_status` | | CardStatus字段归属、A7剧情位解码、B0–B4登记时间 |
| `units.roster` | | 单位分页、旗标解码、保护原因、成长上限 |
| `units.barracks` | | 兵营容量与占用、可发放空位、进行中战斗的预留 |
| `units.squads` | | 编队、当前队与突袭组、旧编队是否已导入 |
| `units.storage` | | 名声、圣灵、银仓库存（只读） |
| `units.ledgers` | | 觉醒奖励与好感账本；尚未落库的账本给出下一次写入会保存的基线（投影）；两本账都经服务的公开读取器（`EvolutionRewards.read`、`GiftService.read`）读取，不复制整份存档 |
| `units.overrides` | | `unit_dynamic_overrides`诊断，见「单位」 |
| `rules.config` | 是 | 注册表逐键描述，主表、资源、客户端清单和各域内容版本对照 |
| `battle.windows` | 是 | 任务窗口：按分类列出每个批次的原生窗口（基础资源）与生效窗口（`mission-windows`规则覆盖后），状态为UPCOMING（未开始）、OPEN（开放中）或ENDED（已结束），按开始时刻含、结束时刻不含，用客户端时钟（整秒落在4毫秒网格上）判定；`growth_campaigns`节给出成长活动免券窗口（窗口经客户端日期解析、同样按客户端时钟判定）；`preset`给出当前规则全文，供批次预设；读不到基础资源时`ok`为false（`admin_windows_resources_unavailable`），资源索引已不是本世代的基础版本时为`admin_windows_base_changed` |
| `battle.journal` | 是 | 战斗日志：进行中战斗（active）与待发邮件（pending_presents），只读 |
| `battle.records` | | 关卡记录分页（可按`quest_id`筛选），逐行解码 |
| `battle.record_archive` | 是 | `quest_record_archive`归档行、运行中主表缺失的记录ID，以及候选世代（暂存主表）下会缺失的记录ID |
| `battle.entrust` | 是 | 委任目录状态与委任记录 |
| `battle.entrust_availability` | 是 | 委任目录按Type的可用性（可带`platform_code`，默认9，即本地客户端恒发的平台码） |
| `battle.entrust_quest`、`battle.entrust_preview` | 是 | 单关委任定义（`quest_id`、可选`platform_code`，默认9）与产出预览（另带`quantity`，默认1） |
| `towers.overview` | 是 | 塔服务状态与存档塔记录（库存、层最好分、总分、已领奖励、deferred） |
| `battle.subjugation`、`battle.subjugation_progress` | 是 | 大討伐共享奖励目录状态与存档讨伐进度；目录状态按视图时钟读取，`cooperative`列出已列入任务菜单、窗口未结束的协力大討伐（载入其任务菜单时客户端自动发143或147，期望为空，见`docs/battle-modes.md`），每项带`mission_id`、`subjugation_type`、`registration`、`window`与`state`（upcoming或open），已结束的不再列出，配置行读不出的只有`mission_id`与`error`；readiness的`subjugation`按服务端时钟给出同一列表 |

`state.browse`在根节点给出每个顶层键的`ledgers`，浏览某个键时给出该键的`ledger`：`claimed`表示键已在`admin_fields.STATE_KEYS`登记归属域（`domain`），`checked`表示登记时带了读函数，带读函数的键附`valid`与`error`（同一读函数只执行一次；读函数出错只标在对应的键上，其他账本照常校验）。服务端会写入的顶层键都已登记，未登记的键`claimed`为false。账本键用各服务的公开读取器校验，包括`login_rewards`、`login_cooking`、`evolution_rewards`、`event_pass_automation`、`event_pass_delivery`、`mission_ep_bindings`、`package_purchases`、`trade_history`、`tower_trade_history`、`tower_state`、`quest_record_archive`（`quest_record_admin.read_archive`，归quest_records域）、各投票账本（`vote_rewards`、`vote_tickets`、`vote_finalists`、`vote_expiry`、`vote_assault_expiry`、`vote_cooking`）与116通知记录（`vote_expiry_notifications`、`vote_assault_notifications`、`vote_cooking_notifications`）；这些读函数不复制整份存档，所需服务缺席时该键的`error`为`admin_view_service_unavailable`。`login_rewards`与`login_cooking`两键用对照当前规则的读取器，存档的新手总数或料理阈值与当前规则不同时`valid`为false，`error`分别为`login_rewards_rules_changed`、`login_cooking_rules_changed`。各服务的回执历史（除`trade_history`、`tower_trade_history`与`skin_acquisition_history`外的`*_history`，以及`trade_receipts`、`tower_trade_receipts`、`vote_transactions`、`vote_last_action`、`present_claim_history_schema`）只登记、不校验：回执要按存档修订版逐条核对，读函数拿不到修订版。`quest_state`、`mission_state`、`skin_acquisition_history`、`startup_collections_schema`与`mission_pass_delivery`、`package_delivery`、`login_reward_delivery`、`rank_reward_delivery`只由属主私下或在初始化整份存档副本时检查，同样只登记（`event_pass_delivery`有公开读取器，照常校验）；`created_local`是建档时写入的墙钟文本，只登记；好感账本`gift_state`的校验也要修订版，由`units.ledgers`经`GiftService.read`负责；`unit_dynamic_overrides`由`units.overrides`诊断。顶层`business_calendar`归account域，由`business_calendar.read_marker`校验：没有标记的旧档不出现这个键；标记损坏时该键报`business_calendar_invalid`，其他账本照常校验。

活动视图共11个：`events.event_login`、`events.comeback_login`（实时）、`events.purchased_login`（实时）、`events.daily_gift`、`events.login_rewards`（新手总数变化时附`repair`节、料理阈值变化时附`cooking_repair`节，给出修复命令、被拒的请求号与客户端表现；`pending_stamps`节给出待发印章条数、上限1098与剩余不足30条的预警）、`events.event_pass`、`events.mission_ep_bindings`、`events.sugoroku`（实时，在副本上按当月投影棋盘）、`events.packages`、`events.trades`（实时）、`events.tower_trades`（实时，按当月显示次数，并列出保存的所属月与终身累计；有被换用的商品时整本读取失败，附`diagnosis`）。投票视图共8个：`votes.history`、`votes.rewards`、`votes.tickets`、`votes.rankings`、`votes.expiry`、`votes.assault`、`votes.cooking`、`votes.receipts`。这些账本视图统一返回：`service`（名称、是否可用、内容版本）、`ledger`（各顶层键是否存在及读函数结果）、`summary`（状态、原因、定义状态的计数）、分页的`entries`（键、状态、原因、定义是否与当前世代一致、明细）和`readback`（客户端回读请求号及不预检的理由），另加各自的分节；整本读取失败时附`diagnosis`，只分页列出被拒的成员。

`events.login_rewards`的`frozen_rules.beginner_total`为changed时，`repair`节的`command`为`login_rewards.rebase_beginner`、`refused_operations`为`[115]`、`client`说明客户端弹通信错误并原包重发；`frozen_rules.cooking_border`为changed时，`cooking_repair`节形状相同，`command`为`login_cooking.migrate`，另带`offline`（`login-cooking-migrate-plan`、`login-cooking-migrate-apply`）。`pending_stamps`节给出`count`、`capacity`（1098）、`remaining`与`warning`（剩余不足30条，约一个月的登录日），剩余为0时另带`code`（`login_stamp_pending_capacity`）与`refused_operations`（[115]）：这时只有当天印章需要新记一条待发的新签到日的115以这个码被拒，同日再次请求、当天印章能正常入箱的签到日照常成功，已有记录的补发重试只更新原因。读取成功时这一节`ok`为true；服务缺席或读取失败时只有它`ok`为false并附`error`，视图其余部分照常返回。活动签到、回归签到、购买日奖与DailyGift视图在有冻结定义已变化或无法核对的pending时附`repair`节，`command`为对应的`*.resolve_changed`，`targets`列出要处理的`campaign_id`、`round_id`、`purchase_id`或`gift_id`（同一目标只列一次），对应服务缺席时不出现；`events.login_rewards`的待发印章同样附`stamp_repair`节（`login_rewards.resolve_pending_stamps`，按`month`列出）；`events.event_pass`有身份核对为changed的通行证时同样附`repair`节，`command`为`event_pass.rebind_definition`，`targets`按`pass_id`逐个列出这些通行证；身份已与当前定义一致或已不在目录里的通行证不列出，改绑会拒绝它们。活动签到、回归签到和购买日奖视图把迁移命令作废的档列为`voided`（`mail_ids`为空），轮次与购买的分节分别给出`delivered`与`voided`计数；DailyGift视图把作废的条目同样列为`voided`。

`votes.assault`先确认挑战权折算账本与存档里的挑战权和金币邮件一致，再按活动编号列出pending与settled事件，明细里的`gold`为`amount × rate`。`votes.expiry`、`votes.assault`、`votes.cooking`另有`notifications`分节，给出`ok`与`records`：`records`是116已提交通知的永久记录，键分别是物品ID、活动编号和投票券物品ID；读取失败时`ok`为false并带`error`，`records`为null。三个视图的账本与通知都经各服务的只读读取器取得，读取时不调用initialize，也不复制整份存档。

LetsTD视图见「LetsTD本地模拟」，预览工具见「预览工具」。

## 命令目录

所有命令都经计划与提交执行，不写游戏回执。失败码沿用对应原生内核的错误码，控制台自身的拒绝以`admin_`开头。「破坏性」一栏给出确认短语，`<n>`为条目数，单数时名词不加S。

### 货币（currencies）

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `wallet.gold.credit` | `amount` | 加金币，加后余额超过99,999,999时整笔拒绝（409 `admin_gold_capacity`），不截断；存档已超上限的旧余额只能往下调 | 否 |
| `wallet.gold.debit` | `amount` | 超过余额409 `admin_gold_insufficient`；经验为0时不能扣到0（409 `admin_gold_native_balance`） | `DEBIT <amount> GOLD` |
| `wallet.gold.set` | `value`、`current` | `current`是这次编辑所依据的存档余额，不符时409 `admin_gold_current_changed`；受同样的上限与原生规则约束；与当前值相同时只给警告，不推进修订版 | 仅`value<current`时：`SET GOLD TO <value>` |
| `wallet.crystals.credit_free` | `amount` | 只加到领取一封free_crystal邮件能加到的量（付费≤1000、免费加本次≤1000、合计加本次≤2000），超出409 `admin_free_crystal_capacity`，`details.space`给出余量，其余改用`presents.enqueue`发free_crystal邮件 | 否 |
| `wallet.crystals.credit_paid` | `amount` | 仅本地操作，不记录平台购买与月消费；付费加本次≤1000、合计加本次≤2000且免费未超1000，否则409 `admin_paid_crystal_capacity` | 否 |
| `wallet.crystals.debit` | `amount`、`order` | `order`为`free_first`（默认，与服务端自身扣费相同：先免费后付费，同类先扣较早取得日）、`paid_first`或`paid_only`；不改月消费账本及其投影 | `DEBIT <n> CRYSTAL(S) FREE FIRST`等 |

### 账号（account）

| 命令 | 参数 | 说明 |
|---|---|---|
| `wallet.experience.add` | `amount` | 按关卡结算的同一规则累计经验、重算等级、升级补满魅力，等级奖励邮件在同一次提交中发出；经验最多加到最后一级阈值减1，被截断时给警告；放不下的等级奖励留在pending |
| `energy.restore` | 无 | 先结算自然恢复，再加一次当前上限（等级上限加扩展），不花结晶、不计月消费；超过魅力上限设置时409 `recovery_cap_exceeded` |
| `capacity.grant_next` | `amount`、`month` | 只授予下个月的扩展魅力：`month`必须是JST业务月（04:00换日）的下一个月（`YYYY-MM`），否则422 `admin_capacity_month`，`details`给出业务月与生效月；下月上限会超过int32时409 `admin_capacity_overflow` |

不提供直接设定等级、重建基线或不发奖的改等级命令；UserName只读。

### 物品与礼物箱（items、presents）

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `possessions.credit` | `items[{item_id,count}]` | 只接受分类为passive的库存物品，其余422 `admin_item_not_stock`，`details.items[].use`指出应改用的命令（服装用`skins.grant`，所有权物品、称号、恢复品和召唤券用`presents.enqueue`，开包券和其他类别为null）；委任券（`EntrustCatalog.ticket_item_ids`，没有委任服务或目录一张券都没列出时回落为`item_consignment_ticket`）只能经邮件补充 | 否 |
| `possessions.debit` | `items[{item_id,count}]` | 同样只接受passive物品，`use`为null（控制台不提供移除其他物品的命令）；委任券可以扣减 | `DEBIT <n> ITEM(S)` |
| `skins.grant` | `item_id`、`purchase_flag` | 发放服装并在同一次写入中记录任务取得；`purchase_flag`必填，为true时同时把出售该服装的商店商品标为已购（需要服装商店服务，商店不卖该服装时422 `admin_skin_not_sold`），为false时对仍显示未购的商品给警告；非服装422 `admin_skin_item_invalid`，已持有409 `admin_skin_owned` | 否 |
| `presents.enqueue` | `rewards` | 支持全部奖励类型，逐条预览，拒绝时`details.reward`指出第几条；券按张拆成独立邮件；ReceiveAction=3的开包券一律拒绝（422 `admin_present_pack_ticket`） | 否 |

物品分类为passive、skin、ownership、recovery、item_ticket、emblem、event_pass、vote_ticket、stack_zero和unknown；unknown表示资源中存在、但没有已验证目录认领的物品。任何命令都不写PPT.A9通知类别。

### 任务与奖励（progress）

| 命令 | 参数 | 说明 |
|---|---|---|
| `rank_rewards.retry_pending` | 无 | 由等级奖励服务重新投递pending的等级奖励，返回已投递邮件和仍pending的原因；没有可投递的时只给警告 |
| `missions.mark_read` | `ids` | 与60一样按54快照判定，只把已完成、未读的任务（不论是否已领）标为已读；锁定的顺序任务在快照中显示为未完成，保持未读；效果列出`marked`与`skipped`，有跳过的ID时给警告，什么也没标时不保存任何东西。`progress.missions`的未读列用同一口径 |
| `missions.claim` | `ids` | 代玩家领取已完成、未领的任务奖励，与59同一内核：奖励经礼物服务入箱或直接入账，通行证点数与58/59一样超过印章上限或窗口外时丢弃；未完成、已领或窗口外（任务窗口与通行证窗口一样按客户端整秒时钟判断）的任务跳过并给警告，投递方式不支持的任务保持已完成未领（与游戏一致）；效果列出领取、跳过、邮件ID、邮箱前后数与通行证点数审计 |
| `event_pass.grant_points` | `pass_id`、`amount` | 直接加通行证点数（EP即通行证道具的持有数）：超过道具Stack或通行证窗口外（窗口按客户端整秒时钟判断，EndDate当秒内仍在窗口内）的部分丢弃并给警告，与58/59丢弃EP的口径一致 |
| `event_pass.claim` | `pass_id` | 代玩家领取当前点数已到达、尚未投递的通行证奖励，与159同一内核；全部投递过时只给警告，不保存任何东西（与159一致）；效果列出投递明细、邮件ID与邮箱前后数 |

### 设置（settings）

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `config.sound.set` | A2、A3、C2、C3、F9、FA、FD全部给出 | 与21/22同一套规则：A2/A3只取0/1，其余int32；取值非法400 `invalid_configuration_request` | 否 |
| `config.sort.set` | F1、F4、F5 | 沿用25的长度约束 | 否 |
| `config.auto_select.set` | F6、FB | 完整int32，未解释的位原样保留（27） | 否 |
| `config.damage_display.set` | `enabled` | 伤害显示标志（F7）：客户端在战斗结束时上报，PGC没有setter，这是唯一的服务端写入口；只取0/1 | 否 |
| `guide.set` | `get_index`（0表示取消） | 与121同一组资格规则：普通持有单位，原CardID的BonusType≠0且Love≥100；本来没有秘书时取消只给警告，不推进修订版 | 否 |
| `blessings.set` | `FI`、`settings[{ItemID,Enable}]`，至少一项 | 与148同一内核；FI越界、Enable非0/1或名称为空400 `invalid_request_fields` | 否 |
| `view_event.set` | `CardId`、`Id`、`Mode`、`Male` | 与23同一内核，EventViewRequest与观看历史同一事务 | 否 |
| `view_event.clear` | 无 | 与24同一内核；待播已空时只给警告，不推进修订版 | 否 |
| `flags.set` | `flags[{ID,Value}]`、`purchase_flag_ack` | 名称哈希冲突409 `universal_flag_name_conflict`，值为int32，其他标志不变；改到服装商店商品的购买标志时必须带`purchase_flag_ack=true`（否则422 `admin_purchase_flag_ack_required`），差异列出该物品的持有量和取得记录，本命令不改持有物；需要服装商店目录 | 仅`purchase_flag_ack=true`（确认156购买标志）时：`SET <n> PURCHASE FLAG(S)` |
| `perused.set` | `events` | 已读标志，清除必须显式写`FLG=0` | 否 |
| `backgrounds.merge` | `rows[{CardID,ConfigID,SaveTime}]` | 行格式由背景存档内核校验，非法时400 `background_updates_invalid`；只接受可选的已验证背景（409 `admin_background_rows_unsupported`，`details.findings`列出原因）；`SaveTime`必须显式给出，格式`YYYY/MM/DD HH:MM:SS`（JST），不能晚于服务端JST时刻（409 `admin_background_save_time_future`） | 否 |
| `stories.set_unlock` | `cards[{CardID,set_bits,clear_bits}]` | 只开放CardStatus.A7的bit0、bit1（前两篇角色剧情）和bit2（交流首通），逐位设置或清除，其余位、WatchLoveEv、已读、Love、关卡记录都不动，也不发奖；存档还没有好感奖励账本且改动翻转了bit0/1时，先冻结已达到的好感档基线 | 仅含清除剧情位时：`CLEAR STORIES <n> CARD(S)`（只设置位不要短语，也不备份） |

### 单位（units）

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `units.grant` | `card_id`（>1）、`level`、`evolution_count`（0或2）、`count`（1–100） | 经发卡内核检查兵营容量、进行中战斗的预留和初始化一致性；需要兵营容量服务；错误码以`admin_unit_grant_`开头 | 否 |
| `units.preview_initial` | `card_id`、`level`、`evolution_count` | 只在效果中给出新单位的初始值，不改状态 | 否 |
| `units.remove` | `units[{get_index,unique_id}]` | 先固化二觉首奖基线（觉醒奖励服务不可用时拒绝）、再保存好感历史，保留`last_unique_id`高水位；拒绝王子、锁定、编队成员、当前队（含突袭组）、秘书和战斗预留中的单位（409 `admin_unit_removal_protected`，`details`列出原因），不提供强制删除 | `REMOVE <n> UNIT(S)` |
| `units.edit_growth` | `get_index`、`unique_id`及`experience`、`love`、`skill_level`至少一项 | Exp以职业上限等级的阈值为上限，溢出按合成规则保留或截断（截断时警告）；Love上限150或100，越界409 `admin_unit_growth_limit`；SkillLevel为1到技能上限；王子随账号等级成长，不可编辑（501 `admin_unit_growth_unsupported`）；记录好感基线，不发奖，不改WatchLoveEv、剧情已读、CardID、ClassID | 否 |
| `units.set_display` | `get_index`、`unique_id`、`display` | 与10相同的绝对显示内核；GetIndex与UniqueID对不上409 `admin_unit_display_missing` | 否 |
| `units.set_lock` | `units[{get_index,unique_id,locked}]` | 与119同一内核；任一单位对不上409 `admin_unit_lock_missing` | 否 |
| `squads.save` | `group`、`members`、`metadata`、`select`，至少给一部分 | 成员、元数据（名称与PlayerTitle物理行，经称号目录校验）、当前选择分开提交，同一次提交里依次应用；组号只接受普通0–19队和突袭组（否则400 `invalid_request_fields`）；旧编队待导入时拒绝任何部分保存（409 `current_squad_legacy_pending`）；只改成员而该组没有唯一已存名称时422 `admin_squad_metadata_required`；成员必须是持有单位（否则400 `squad_unit_missing`）；GetIndex 0照原生接受并给警告 | 否 |
| `units.evolve` | `target`、`operation`、`branch`、`materials` | 代玩家一觉、技觉、二觉（二觉须选分支1或2），规则与原生相同 | `EVOLVE UNIT <GetIndex> WITH <n> MATERIAL(S)` |
| `units.train` | `target`、`materials` | 代玩家合成，规则与原生相同 | `TRAIN UNIT <GetIndex> WITH <n> MATERIAL(S)` |
| `units.sell` | `units[{get_index,unique_id,card_id}]`，最多4096个 | 按原生手动出售规则卖出，不套用界面的10个上限；与原生74一样只卖第一兵营的单位，其余501 `admin_sale_storage_unsupported`；金币超过99,999,999的部分丢弃并给警告（效果列出报价、入账、丢弃与余额），彩虹币直接入持有物，超Stack整批拒绝；王子、锁定、编队成员被拒（`admin_sale_unit_protected`），秘书与战斗预留卡可卖；需要出售、送礼、觉醒奖励三个服务 | `SELL <n> UNIT(S)` |
| `units.overrides.remove` | 无 | 删除整个`unit_dynamic_overrides`键，不依赖任何服务；审计差异记录删除前的值；键缺失409 `admin_unit_overrides_absent`；null或空值也可以删，但会提示它未阻断任何养成流程 | `REMOVE UNIT OVERRIDES` |

`units.evolve`和`units.train`的目标与兵营素材要给出GetIndex、UniqueID、CardID，仓库素材只给来源与CardID；目标只能在第一兵营，兵营素材按所在兵营引用，仓库库存按数量扣除，不参与保护检查。受保护的素材在`details.protected`中列出原因。`units.remove`、`units.sell`、`units.set_lock`、`fame.register`、`fame.deposit`、`barracks.transfer`以及`units.evolve`、`units.train`的目标与兵营素材都在参数阶段拒绝重复的GetIndex或UniqueID（422 `admin_params_invalid`）；`units.remove`、`units.evolve`、`units.train`的保护拒绝以`details.protected`列出`[{GetIndex, protections}]`；`units.evolve`、`units.train`、`units.sell`的单位参数都是`get_index`、`unique_id`、`card_id`三项。效果包括字段变化、价格、金币前后、消耗的单位、仓库扣除数、首奖PresentID（仅觉醒）、加成与等级（仅合成）和`last_unique_id`。两者都不读也不写原生的觉醒、合成回执日志，对应的预览工具为`tools.evolution`和`tools.training`。

`units.sell`不写出售回执，客户端在途的旧出售请求重放时会得到`sale_retry_stale`或`sale_unit_missing`，应在客户端空闲时操作。

`unit_dynamic_overrides`是原生只存在于战斗内存、服务端没有写入者的外来数据，非空时合成（5）、银交换（106）和商店成长（113）都会拒绝。`units.overrides`视图把它分为absent、empty（null、`{}`、`[]`）、entries（非空对象或数组）和invalid（其他类型）：entries三处都回501；invalid时106、113回503，值为真时5也回501；empty不阻断。视图同时给出条目数、字节数、sha256和是否阻断，不超过16 KiB时显示原值，更大的只给摘要，全文用`state.browse`的`/unit_dynamic_overrides`分页查看。需要长期保留原值时，移除前先做手动备份。

### 仓库与兵营（storage）

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `fame.register` | `units` | 与149同一内核，要求相同的等级门槛；按GetIndex与UniqueID选择，不匹配409 `admin_fame_unit_missing`；逐个按资格登记，不合格的跳过并给警告，已登记的不改登记时间；新登记照常计入名声任务 | 否 |
| `fame.deposit` | `units` | 与150规则相同，一次最多为原生多选上限，超出422 `admin_fame_deposit_limit`（优先于等级门槛） | `DEPOSIT <n> UNIT(S)` |
| `fame.withdraw` | `card_id`、`stage`、`count` | 与151规则相同，可一次取多个新实例；每个新实例都重新检查库存、名册空间和进行中战斗的奖励预留 | 否 |
| `fame.adjust_stock` | `card_id`、`stage`、`delta`（非0） | 直接改名声库存数量；增加须已登记且能经151取回，且不超过单阶段上限；减少不能低于0，满桶和超限旧数据都可以减少 | 仅`delta<0`时：`ADJUST FAME STOCK <CardID>/<stage> BY <±n>` |
| `barracks.grant_slot` | 无 | 只把HaveUnitMaxSizeBuy的高16位加一，低16位旧购买计数不动；不扣结晶、不计月消费、不检查等级，最多两座额外兵营 | 否 |
| `barracks.transfer` | `destination`、`units` | 与118同规则；单位已在目标兵营时只给警告 | 否 |
| `silver.adjust_stock` | `card_id`、`delta`（非0） | 直接改银仓单卡数量：增加须满足127的材料条件和单卡上限，减少不能低于0 | 仅`delta<0`时：`ADJUST SILVER STOCK <CardID> BY <±n>` |
| `silver.withdraw` | `card_id`、`count` | 与128规则相同，可一次取多个新实例 | 否 |
| `fairy.adjust_stock` | `card_id`、`delta`（非0） | 圣灵库存只按数量保存，增加数量不会恢复任何原实例的成长、锁定或好感 | 仅`delta<0`时：`ADJUST FAIRY STOCK <CardID> BY <±n>` |
| `fairy.withdraw` | `card_id`、`count` | 发新实例并递增编号，`count`上限同原生125 | 否 |

这些命令不写名声、兵营、银仓、圣灵仓库的游戏回执。`fame.register`、`fame.deposit`、`fame.withdraw`在内核前按149–151的顺序检查存档：依赖是否在启动后变化、单位表、GameParam、等级、名声仓、CardStatus；GameParam不可加载（例如金币为负）时与原生处理器一样回503 `invalid_player_state`。`barracks.transfer`与118一样先解析选择，再检查就绪、单位表和GameParam。

### 活动（events）

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `sugoroku.claim` | `ids`（1–4096个互不重复的ID） | 与63共用领取内核：先同步本月棋盘，只领已到达、未领取、可投递的奖励，下月容量直接记账，其余奖励入箱；效果列出`period`、领取结果与邮件ID、已领、不在本月棋盘、不支持投递的ID、邮箱前后数与上限、下月容量前后值；什么也领不到时不保存任何东西 | 否 |
| `login_cooking.migrate` | 无 | 按当前世代的签到料理阈值和服务端时间迁移账本，只在本业务月重算额度；效果列出账本月、消费月、上月消费、基线来源、已发份数、阈值与额度前后值和剩余份数；无需改动时照常生成计划并给警告，提交只写审计，不备份、不推进修订版 | `MIGRATE LOGIN COOKING` |

运行中改阈值由配置切换自带料理迁移；停服改了阈值再启动的，存档照常加载，只有115在迁移之前拒绝（客户端弹窗重发），用本命令迁移到当前世代的阈值即可，也可以在启动前用离线料理迁移。

### 战斗（battle）

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `quests.record.edit` | `QuestID`及`MaxRank`、`AchieveFlag`、`RemainAssault`、`FirstDefeatDate`、`Shown`至少一项 | 编辑单行关卡记录，关卡必须在运行中主表里；只改给出的字段，不发任何奖励 | 否 |
| `quests.record.edit_with_rewards` | 同上加`stars`、`hard` | 先按同一内核改记录（可以不带字段改动），再在同一次提交中结算：总是按`stars`、`hard`记一次quest_clear任务事件（不带队伍与击破数）；首次★奖励只在`stars`等于该难度的★数（普通3、★4难度4）且这次编辑把`AchieveFlag`的对应★位从无到有置上时发放（物品超Stack丢弃并警告，结晶放不进钱包时改发free_crystal邮件），不改★位就不发；随后结算等级奖励；计分关409 `quest_record_rewards_unsupported`；需要任务、关卡、等级奖励等服务 | 否 |
| `quests.journal.abort` | 无 | 中止进行中的战斗（与ギブアップ相同：关闭active，不扣费、不发奖，待发邮件保留）；效果给出被中止战斗的身份 | `ABORT BATTLE` |
| `quests.journal.flush_pending` | 无 | 立即投递战斗日志里的待发邮件（正常只由下一次战斗结算投递）；邮箱装不下的行保留在日志里并给警告；没有待发时只给警告 | 否 |
| `quests.records.archive_missing` | `master_snapshot` | 把指定主表快照（通常是暂存快照）里不存在的关卡记录移入`quest_record_archive`侧表，不删除；配合去掉关卡的新主表使用，归档后重启兼容检查不再拦截该切换；没有缺失时只给警告 | `ARCHIVE MISSING RECORDS <快照>` |
| `quests.records.restore_archived` | `quest_ids` | 按ID把归档的记录放回关卡记录表；运行中主表没有该关卡的行保持归档并给警告 | 否 |

战斗命令都在`battle`域。塔命令作用于塔账本（`tower_state`），有进行中战斗时一律先拒绝（`admin_battle_abort_required`）：

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `towers.floor_best.set` | `quest_id`、`score` | 直接设某层最好分（0到该层计分上限）；手设的最好分没有对局，分数变化时原保存的HighScoreTeam快照随之删除；新分数已越过、尚未领取的计分奖励档直接记为已领、不发奖（已在deferred里等待的档除外）；总分、已领计分奖励在效果中给出前后值；新总分允许的塔结晶库存上限低于现有库存时400 `tower_admin_invalid`（先用`towers.stack.adjust`降库存），塔还没有记录时409 `tower_not_started` | 否 |
| `towers.stack.adjust` | `mission_id`、`stack` | 直接设塔结晶库存（StackCrystal，68可领取的存量），上限为该塔当前生成档的PossessionLimit；不改挑战道具 | 否 |
| `towers.settle_now` | 无 | 先把塔时钟结算到当前时刻，再对开放中的塔强制多结算一天（补一天挑战道具、累积一天塔结晶），效果列出道具持有与各塔库存的前后值；没有开放的塔时不加天并给警告 | 否 |
| `towers.deferred.redeliver` | `mission_id` | 重投该塔deferred的奖励档；仍投不进的留在deferred并给警告，由下一次塔结算重试 | 否 |
| `towers.rewards.reset` | `mission_id`、`sources`（score、star的子集，默认全部） | 清除该塔已领奖励档的记录。被清的档视为从未发过：下一次结算、重投或跨档都会再次发奖，等于允许重复领取 | `RESET TOWER <mission_id>` |

大討伐命令作用于讨伐进度（共享奖励目录的水位），同一任务有进行中战斗时409 `quest_battle_active`：

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `subjugation.watermark.resync` | `mission_id` | 把已发奖水位重设为成员关卡记录当前的任务分数（最高分与累计分，可升可降），不发邮件；用于手工改过记录之后。水位降低后，之后的结算会再次跨越并发放其上的档；任一分项被下调时给重复发奖警告，与`subjugation.progress.reset`同类 | 否 |
| `subjugation.rewards.redeliver` | `mission_id` | 记录高于水位的轴按奖励阶梯逐档补发成邮件（经礼物箱投递，邮箱放不下时整笔回409 `present_capacity`、水位不动）；补发后水位按最高分与累计分两轴各取水位与记录的较大值推进，永不下降；水位已覆盖记录或还没有水位时不发邮件，只给警告（没有水位时按记录建立水位） | 否 |
| `subjugation.progress.reset` | `mission_id` | 把该任务成员关卡记录的MaxRank（打包的讨伐分数）清零并删除水位。之后的结算会再次跨越每个奖励档，等于允许重复领取 | `RESET SUBJUGATION <mission_id>` |

### 投票（votes）

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `vote_tickets.grant` | `item_id`、`count` | 发投票券（关卡掉落同款通道）：按投票券持有上限截断，丢弃量在效果与警告中给出；只接受投票组的券，日常票不属于任何投票组，与其他物品一样回400 `vote_quest_ticket_item_invalid`；该组券已到期结算时409 `vote_ticket_closed` | 否 |

投票账本的迁移命令（`votes.reopen_settled`、`votes.adopt_*`）见「迁移」。

### 迁移

显式迁移命令：定义或主表变化时，服务端从不自动改写旧账本，pending只是停发并给出原因；要采用新定义就用下面的命令，都要确认短语，提交前都自动备份。

| 命令 | 参数 | 说明 | 确认短语 |
|---|---|---|---|
| `presents.split_ticket_mail` | `present_id` | 把一封Content>1的旧券邮件拆成逐张邮件（现行投递口径），邮箱容量不够时拒绝；效果的`still_bundled`列出其余仍待拆的邮件。Content>1的召唤券（Type16）邮件会让42与整个邮箱回503 `present_state_invalid`，预检也因此不过，所以有多封时要在同一批里依次全部拆完；礼物箱页在严格读取失败时仍按邮箱视图的`bundled_tickets`列出待拆的PresentID，「全部拆开」把它们放进同一个计划一次预览和提交，单拆其中一封时预检因严格读取仍失败而不能提交 | `MIGRATE TICKET MAIL <present_id>` |
| `missions.rebuild_changed` | 无 | 任务定义变化的迁移：定义变化而周期类型不变时按当前定义重建该任务当期的进度行；周期类型变化时删除旧类型的未领行、把已领行移入`mission_state.superseded`留档（不参与判定），新类型从当前周期开始；只处理当前生效任务与新手顺序任务，下线任务复刻生效后需再执行一次 | `MIGRATE MISSIONS` |
| `rank_rewards.resolve_changed` | `rank`、`action`（redeliver或void） | 处理定义变化的pending等级奖励：redeliver按当前定义改写摘要并立即补发，邮箱装不下时按新定义继续pending；void删除该pending条目，已观察等级不变，之后不再发放（审计保留前后值） | `MIGRATE RANK <rank> REWARDS <ACTION>` |
| `login_rewards.rebase_beginner` | 无 | 新手签到规则变化时（存档照常加载，115回503 `login_rewards_rules_changed`，客户端弹窗并原包重发）按当前规则重建基线；运行中切换到新手总数不同的世代时切换自带这次重建，不需要本命令 | `MIGRATE BEGINNER LOGIN REWARDS` |
| `event_pass.rebind_definition` | `pass_id` | 把通行证账本改绑到当前定义，已投递记录保留；改绑之前，已有投递记录的通行证有新到达的档位可领时，159与`event_pass.claim`回503 `event_pass_definition_changed`（159记WARNING点名本命令，客户端弹窗并原包重发），116自动补领把它留在待处理，不中止整次请求 | `MIGRATE EVENT PASS` |
| `daily_gift.resolve_changed` | `gift_id`、`action`（redeliver或void） | 处理定义变化的pending DailyGift：redeliver按当前定义立即投递该gift_id所有定义变化的待发日，投递失败的那一天继续pending、reason记失败码，由115/116结算重试；void保留记录（status仍为pending、reason为`daily_gift_voided`，视图显示为voided），以后不再投递，同一资格日不再计入；当前目录已没有该礼物时redeliver回409 `daily_gift_source_unavailable`，只能void | `MIGRATE DAILY GIFT <gift_id> <ACTION>` |
| `event_login.resolve_changed` | `campaign_id`、`action`（redeliver或void） | 处理定义变化的pending活动签到奖励（含活动已退休或该档已无定义）：redeliver按当前定义改写待发摘要并立即补发，邮箱装不下或奖励暂不支持时按新定义继续pending，由115/116重试；void把该档记为已用而不发邮件（`deliveries`中该档`mail_ids`为空、投递日取资格日，不计入当天实发），下一个签到日照常领下一档；活动已退休或该档已无定义时redeliver回409 `event_login_definition_missing`，只能void；没有定义变化的pending时回409 `event_login_resolve_unneeded`；效果列出档位、资格日和处理前后的状态、定义、原因、投递日与邮件ID | `MIGRATE EVENT LOGIN <campaign_id> <ACTION>` |
| `comeback_login.resolve_changed` | `round_id`、`action`（redeliver或void） | 处理定义变化的pending回归签到奖励：redeliver把该轮待发档及其后未领各档的冻结定义改为当前定义（当前目录缺的档保留旧定义）并立即补发；同一活动当天已实发过一档时只改定义，按`comeback_login_daily_limit`继续pending，次日由115/116补发；void与活动签到相同，记为已用（`notify`为false）；档位已从目录消失或该档已无定义时redeliver回409 `comeback_login_definition_missing`；效果另列`rebound_ordinals`（改了定义的档）与`still_changed`（之后仍与当前定义不符的档，待其成为pending时再处理） | `MIGRATE COMEBACK ROUND <round_id> <ACTION>` |
| `purchased_login.resolve_changed` | `purchase_id`、`action`（redeliver或void） | 购买日奖的同类处理，规则同回归签到：当天已实发时按`purchased_login_daily_limit`继续pending；活动已不在出售目录或该档已无定义时redeliver回409 `purchased_login_definition_missing`，只能void；void后该资格不再有pending，期满后可按购买上限再次购买 | `MIGRATE PURCHASED LOGIN <purchase_id> <ACTION>` |
| `login_rewards.resolve_pending_stamps` | `month`（签到日所在的YYYY-MM）、`action`（redeliver或void） | 处理该月定义变化的待发印章，即记录时已有定义、现在与当前印章表不同或表里已没有该位置的印章（记录时还没有定义的印章不在此列，资源补齐后由115自动补发）：redeliver按当前定义立即补发并删除记录，邮箱装不下或奖励暂不支持时以新定义保留、原因记为失败码，由下一次115补发；当前表没有定义的印章不处理，列在`still_changed`，全部没有定义时回409 `login_stamp_definition_missing`；void删除记录、不发邮件，签到日与印章次数不变；没有可处理的印章时回409 `login_stamp_resolve_unneeded` | `MIGRATE LOGIN STAMPS <month> <ACTION>` |
| `tower_trade.rebind_product` | `product_id`、`carry` | 同一商品ID（组内首行ID）在资源更新后改作别的奖励或货币时，65与69整体回503 `tower_trade_product_reused`（不只是被换用的商品），直到每件被换用商品都改绑到当前商品（效果的`still_reused`列出其余待改绑的商品）；`carry`为true时保留本月购买数，为false时本月从0起，终身累计都保留；没有记录或已属当前商品时409 `tower_trade_rebind_unneeded`；兑换次数只经65/69在兑换画面下发，没有预检回读，命令在同一事务内复核改写后的历史与回执 | `REBIND <product_id>` |
| `votes.reopen_settled` | `item_ids` | 已结算票种重新出现库存时：旧结算与116通知移入归档，库存在下一次期满观察（137或57）时作为新实例折算（`converts_on_next_observation`）；没有投票组使用该票种时库存保留不折算并给警告；一起重现的券必须一起重开 | `MIGRATE <n> VOTE TICKET(S)` |
| `votes.adopt_reward_definition` | 无 | 投票奖励定义采用当前版本，已发记录保留、不追补 | `MIGRATE VOTE REWARDS` |
| `votes.adopt_cooking_definition` | 无 | 投票料理定义采用当前版本：跟踪中的组改用当前参数；pending组保留已冻结的胜者与份数，只换用当前料理来源并立即尝试投递；旧定义留在该组的`superseded`中，已发的组不变；因定义变化待发的料理按当前定义立即投递，放不下的保持待发由115重试；定义未变、只因邮箱满而待发的料理不属迁移，命令回409 `vote_cooking_adopt_unneeded` | `MIGRATE VOTE COOKING` |
| `votes.adopt_vote_definition` | `item_id` | 已存投票改采当前投票组定义 | `MIGRATE VOTE DEFINITION` |
| `sugoroku.establish_capacity_baseline` | `period`（YYYY-MM） | 为带旧扩展魅力、没有容量基线的存档建立指定业务月的基线 | `MIGRATE CAPACITY <period>` |

没有可迁移对象时各命令回409：`present_split_unneeded`、`mission_rebuild_unneeded`、`rank_reward_resolve_unneeded`、`login_rewards_rebase_unneeded`、`event_pass_rebind_unneeded`、`daily_gift_resolve_unneeded`、`event_login_resolve_unneeded`、`comeback_login_resolve_unneeded`、`purchased_login_resolve_unneeded`、`login_stamp_resolve_unneeded`、`tower_trade_rebind_unneeded`、`vote_expiry_reopen_unneeded`、`vote_rewards_adopt_unneeded`、`vote_cooking_adopt_unneeded`、`vote_definition_adopt_unneeded`与`capacity_baseline_unneeded`。当前目录已没有对应定义时，redeliver回409 `rank_reward_definition_missing`、`daily_gift_source_unavailable`、`event_login_definition_missing`、`comeback_login_definition_missing`、`purchased_login_definition_missing`或`login_stamp_definition_missing`，只能改用void（等级奖励按等级、待发印章按月，全部没有定义时才拒绝，否则照常补发有定义的，没有定义的列在`still_changed`）；`event_pass.rebind_definition`的通行证不在当前目录时回501 `event_pass_unknown`。`rank_rewards.resolve_changed`与`missions.rebuild_changed`属progress域（任务与奖励页）；`login_rewards.rebase_beginner`、`event_pass.rebind_definition`、`daily_gift.resolve_changed`、`event_login.resolve_changed`、`comeback_login.resolve_changed`、`purchased_login.resolve_changed`、`login_rewards.resolve_pending_stamps`、`tower_trade.rebind_product`与`sugoroku.establish_capacity_baseline`属events域（活动页）；`votes.*`属votes域（投票页）；`presents.split_ticket_mail`在礼物箱页。

`login_cooking.migrate`（见「活动」）属同一类。迁移命令与其他命令一样走预览与提交，不写游戏回执。

### LetsTD（letstd）

| 命令 | 参数 | 说明 | 破坏性 |
|---|---|---|---|
| `letstd.rearm` | `partner`、`group`（1或2） | 并入资源新增的行，重置定义变化且尚未投递的行，已投递而定义变化的行只标记需要适配，不重发；没有行变化时只更新规则与资源指纹 | `REARM <PARTNER> <GROUPNAME>`，例如`REARM OSHIRORE NEWCHALLENGE` |

## LetsTD本地模拟

LetsTD页面固定以「【ローカル模擬】」本地模拟标注开头：这是明确标注的本地模拟合作方活动，未验证任何真实跨游戏账号或购买，标签写死在代码里，不能通过配置去掉。开关不在页面上直接改，而是在配置页的`letstd-rewards`键里修改，属hot档，只改服务端规则、不迁移存档；关闭开关只暂停该组投递（含pending），已发奖励不收回。

`native_popup`开关旁写明：原生弹窗文案来自客户端资源，无法加本地标签，打开后游戏内会显示与真实联动相同的提示；关闭时115/116不下发`LetsTDRewardParam`。三家合作方全开并打开弹窗时，一次115可能连续弹出十几个模态框，建议一组一组开启。

视图：

- `letstd.overview`：本地模拟标注、当前世代的开关及其来源、`native_popup`说明、各组计数；
- `letstd.ledger`：账本视图的通用格式，另有`groups`与`catalog`两节；每组给出武装与重新武装的时间，以及武装时的规则指纹和资源指纹是否与当前一致（只作对照，任何开关变化都会改变规则指纹，不需要因此重新武装）；逐行列出RewardId、ConfigId、类型、名称、数量、StartDate、状态、原因、重试、定义核对、邮件、投递时间、经115还是116、是否带弹窗；
- `letstd.deliverability`：用礼物服务逐行预判能否投递，不拷贝状态；全部105行约1.5秒，只在修订版或世代变化后重算；
- `letstd.projection`（实时，`source`为115或116）：在副本上模拟下一次结算的LetsTD部分，并预览打开弹窗后的原生弹窗（合作方按oshirore、monmusutd、rlyeh排列，同一合作方两组合并；行序为结晶、金币、物品、单位；数量为1用Single，否则用Multi，金币只用Single；每4行一个弹窗）。它不运行同一次115/116里先结算的其他奖励，也不做完整响应的编码校验；首次全量约6秒，期间占用一个世代租约，所以页面只在点击时计算。

## 预览工具

`tools`域共16个只读视图，全部标为实时、按需计算、不落库：`tools.orb_exchange`、`tools.evolution`、`tools.training`、`tools.unit_sale`、`tools.silver_trade`、`tools.growth_shop`、`tools.unit_skins`、`tools.backgrounds`、`tools.emblems`、`tools.fame`、`tools.unit_capacity`、`tools.summon_pools`、`tools.summon_pool`、`tools.item_ticket`、`tools.presents`和`tools.energy_clock`。它们复用各服务已有的报价与资格钩子，按服务端时间推算。约定：钩子拒绝时仍返回200，`data.ok=false`并带`error.code`/`message`；参数非法时仍是422 `admin_params_invalid`；所需服务缺席时`data.ok=false`加`admin_view_service_unavailable`。

- `tools.unit_sale`：除金币、彩虹币及其物品外，还给出`money.before`、`money.after`、`money.discarded`，按与游戏侧出售相同的99,999,999封顶规则推算：超出部分丢弃，已超上限的旧余额不会被入账压低。每个单位另给`protections`（`units.sell`所用手动出售规则会拒绝的保护：prince、locked、squad，写法同`units.roster`）、`barracks`（从0起的兵营序号）与`storage_supported`（是否在第一兵营）；`storage_supported`为false的单位会被`units.sell`拒绝。
- `tools.evolution`、`tools.training`：目标与兵营素材的摘要同样带`barracks`和`storage_supported`。目标只能在第一兵营，否则服务回501 `evolution_storage_unsupported`或`training_storage_unsupported`；兵营素材在服务接入兵营容量时可以来自已解锁的兵营（未解锁的回409 `evolution_storage_locked`、`training_storage_locked`），未接入时同样只认第一兵营；仓库素材按数量扣除，不带这两项，也不参与保护检查。另给保护检查和报价，觉醒还给素材槽位。
- `tools.silver_trade`：返回`quote`、`material`、`barracks`三节，`barracks`对应原生106交换前的兵营空位检查（不足时为`unit_roster_capacity`）；`material`另带素材的`barracks`与`storage_supported`，素材只能在第一兵营（否则501 `silver_trade_storage_unsupported`）。银兑换与成长商店的报价按交易服务的顺序检查：先购买历史，再商品，再本地重置策略（未配置时501 `trade_reset_unconfigured`），错误码优先级与游戏一致。
- `tools.growth_shop`：返回`quote`与`values`，`values.target`带目标的`barracks`与`storage_supported`，目标只能在第一兵营（否则501 `shop_growth_storage_unsupported`）。
- `tools.summon_pool`：给出原始概率和完整性，池不完整时不做归一化。
- `tools.energy_clock`：能量时钟标记与指定时刻（`at`，缺省为服务端时间）的转换预览。

## 字段归属

配置行和CardStatus中每个可写字段只归一条命令，各自调用原服务的校验内核；同组命令（如`view_event.set`与`view_event.clear`）共享同一字段。注册表在启动时检查归属，第二个写入方会让启动失败，`/commands`的`field_owners`和`settings.config`视图都给出这张表；同组命令共享的字段在`field_owners`里显示组内最先登记的命令，组外命令不能声明这些字段。

| 字段 | 线上名 | 命令 |
|---|---|---|
| `UseBgm`、`UseSe`、`_VolumeBgm`、`_VolumeSe`、`BattlePlaceMagnetFlag`、`BattleAcceleratorFlag`、`BattleDClickSkillDisableFlag` | A2、A3、C2、C3、F9、FA、FD | `config.sound.set` |
| `_FlagSort`、`_FlagMissionFilter`、`TypeOfSortOrder` | F1、F4、F5 | `config.sort.set` |
| `AutoSelectConfig`、`IntegrationAutoSelectConfig` | F6、FB | `config.auto_select.set` |
| `_GuideUnitIndex` | C1 | `guide.set` |
| `HaveUnitMaxSizeBuy` | A5 | `barracks.grant_slot` |
| `EventViewRequest` | F8 | `view_event.set`、`view_event.clear` |
| `_BattleGodBlessFilter` | FE | `blessings.set` |
| `DamageDispEnable` | F7 | `config.damage_display.set` |
| CardStatus `A7` | | `stories.set_unlock` |
| CardStatus `B0`–`B4` | | `fame.register` |

MenuTutorialFlags（A4）、CardStatus A1/A3/A5和UserName只读。不提供通用的`config.patch`或`card_status.update`。

控制台模块只使用服务的公开接缝，由守卫测试检查（见「扩展约定」）：`BootstrapService.table_rows`按表规则校验后返回副本；`player_row(state)`按写入路径的GameParam检查（Money与Experience同为0、负的Money/NowCharisma/NowActionPoint、Rank与Experience不符、能量时钟标记）校验后返回存档中的原行，不复制；`system_container`、`possessions_container`；`configuration_container`不含SYSTEM，需要按137拼回；bootstrap模块的`configuration_updates`与21/22使用同一套规则，错误顺序也相同；`PlayerStateService.validate`返回原对象，`validate_player`与`validate_squads`分别校验玩家对象和编队行。预检编码读回响应同样只用公开接缝：`BootstrapService.columns`、`player_row`、`game_param2_fields_response`与`public_master`，以及任务服务的`preflight_snapshot`和礼包服务的`read_response`。

## 界面

页面按`/ui-pages`清单动态加载，按各页的`order`排序，地址为`#/<页面文件名>`：运行状态、存档浏览、审计记录、配置、备份与恢复、账号、货币、物品、礼物箱、单位、仓库与兵营、设置、任务与奖励、战斗、活动、投票、运行规则、预览工具、LetsTD本地模拟。各领域页展示对应视图，并按域从命令目录动态列出可用命令（progress域的命令在任务与奖励页），共享字段旁显示归属命令，只读字段不给编辑控件。

写操作统一走同一流程：预览 → 显示差异以及预检、重启检查与审计体积检查的结果（体积超限时列出超限字段名和字节数，提交按钮随`committable`禁用；预检未通过时注明没有检查审计体积） → 用`crypto.randomUUID`生成`op_id`，拿到确定结果前的重试都沿用它 → 破坏性步骤弹出模态框，手输确认短语 → 提交 → 显示新修订版、`replayed`标记和客户端回读请求号。按参数判定破坏性的命令带「按参数确认」标记。`wallet.gold.set`的`current`在表单打开时和`account.summary`的余额每次刷新（含提交后的自动刷新）时自动填入最新读到的余额；最近一次读取之后游戏又改了金币时，预览会收到409 `admin_gold_current_changed`（`details`给出`money`与`current`；已经预览过的计划在提交时先撞上修订版核对，回`admin_revision_conflict`），刷新余额后重新预览即可。账号页显示业务日历标记：已标记、旧档未标记（只报告，不影响使用）或标记损坏时的错误码。恢复预览同样列出审计检查，并按`auto_backup`标出是否会自动备份：目标与当前存档相同时不备份，执行结果注明存档与修订号都没有改变。

带ETag的视图按页面间隔（默认15秒）用`If-None-Match`轮询，读取失败时按间隔重试；实时视图不带ETag，标有「实时计算」徽标，读到一次成功答复后不再自动轮询、点击刷新才重新计算，读取失败后也不自动重试，在视图上显示错误与「重试」按钮，点击后才再读；还没有读到过答复的视图按实时视图处理。预览工具只在点击时读取，参数表单由`GET /views`给出的`params_schema`生成，视图目录读取失败时显示错误与「重试」，目录里没有某个工具的视图时该工具显示`admin_view_unknown`；分页类工具沿用上一次提交的参数翻页，过时的答复会被丢弃。运行状态页的就绪明细带「刷新」按钮，只重新读取世代构建时算出的结果（启动诊断实时合并），不会重算。窗口隐藏时停止轮询，收到401后不再重试。配置页支持编辑 → 校验 → 报告 → 按档切换或暂存，显示暂存状态并可撤销，并接入主表暂存：「主表快照与采集」面板展开后才读取，在用与磁盘active并列，列出全部快照与采集目录；「导入为快照」只写入数据目录，导入后自动选作校验目标，校验经`/content/validations`走同一套作业与切换流程；撤销主表暂存有单独的按钮。

各页的具体呈现：

- 设置页把命令放在对应分节旁：客户端设置旁是`config.sound.set`、`config.damage_display.set`、`config.sort.set`、`config.auto_select.set`与`guide.set`；加护一节显示`_BattleGodBlessFilter`，旁边是`blessings.set`，并链接到物品页的持有物（Enabled状态）；EventViewRequest旁是共用这个字段的`view_event.set`与`view_event.clear`；通用标志、已读、背景、卡片剧情各自旁放对应命令。背景诊断只读，`backgrounds.merge`的SaveTime由操作者明确填写。
- 单位页在存档有`unit_dynamic_overrides`时置顶显示形态、条目数、字节数、sha256和是否阻断成长流程，值过大时链接到存档浏览页；移除经预览与提交执行`units.overrides.remove`，需要逐字输入确认短语，提交前自动备份。
- 礼物箱页在`presents.mailbox`的`bundled_tickets`非空时，在邮箱一节置顶提示这些邮件各装着多张券、游戏内领不了，其中Content>1的召唤券（Type16）邮件还会让42与整个邮箱回503，这时多封要在同一个计划里一起拆，列出PresentID并提供「全部拆开（n 封）」按钮：一个计划包含每封邮件的`presents.split_ticket_mail`，走同一套预览、确认短语与提交，结果未知时不能开始新的预览；严格读取失败的错误照常显示在提示下方。
- 仓库与兵营页列出名声、圣灵、银仓库存，以及兵营容量、还能授予的单位数和进行中战斗预留的单位授予；库存调整只在减少时需要确认短语与自动备份。
- 预览工具页的出售报价把余额变化、99,999,999上限与丢弃额放在一起，单位表有「兵营」（从0起的兵营序号）与「可出售」两列；觉醒与合成的目标和兵营素材、银兑换的素材、成长商店的目标显示「兵营」与「可用」两列，仓库素材显示「—」；任一项为否时按「预览工具」一节的规则给出提示，这些单位会被服务端拒绝。
- 战斗页依次是窗口、战斗日志、关卡记录（含归档对照）、委任、英傑の塔、大討伐与battle域命令面板。窗口一节按分类列出每个批次的原生窗口与生效窗口（UPCOMING/OPEN/ENDED徽标），并带批次预设编辑器：预设是完整的`mission-windows`规则文档（按批次键整组、成长活动按MissionID），「校验预设」把它作为该键的变更提交`POST /config/validations`，报告与切换都在配置页完成（content档默认暂存到下次启动，切换后要重新运行`prepare-client`并重启客户端）。委任一节给出目录状态、按Type的可用性与单关查询器（查看定义、预览产出）；委任校验作业没有页面控件，经路由表中的`/entrust/validations`启动与查询，同一时间只跑一个作业，世代切换后旧作业标为`stale`、只能重新启动。
- 任务与奖励页的登录奖励一节在账本与待补的登录印章之后依次显示「新手奖励总数核对」（`rules`）与「待发印章容量」（`stamp_capacity`），登录料理一节在账本之后显示「料理阈值核对」（`rules`），各节按通用字段列表列出视图给出的字段（不含`ok`）；规则不符（`changed`为true）时在登录奖励或登录料理一节的账本上方置顶警告，写明修复命令（`login_rewards.rebase_beginner`或`login_cooking.migrate`）、完成之前115被拒以及客户端弹出通信错误并原包重发；读取失败的节只在自己的标题下显示错误。
- 活动页按页签显示11个活动账本，有修复命令的页签在说明里写明：定义变化的待发奖励用对应的`*.resolve_changed`或`login_rewards.resolve_pending_stamps`，登录奖励页签另写明料理阈值与新手总数变化后的`login_cooking.migrate`、`login_rewards.rebase_beginner`以及完成之前115拒绝、客户端弹窗重发，通行证页签写明手动领取新档位（159与`event_pass.claim`）在改绑之前以`event_pass_definition_changed`拒绝、116自动补领把它留在待处理，以及`event_pass.rebind_definition`，塔兑换页签写明改绑之前65与69整体拒绝和`tower_trade.rebind_product`；这些修复命令都是破坏性操作，要手输确认短语；视图的`repair`、`stamp_repair`、`cooking_repair`与`pending_stamps`节分别以「修复」「待发印章修复」「料理门槛修复」「待发印章容量」为标题显示，作废的档显示为「已作废」。带`targets`的`repair`与`stamp_repair`节先列出`command`，再把`targets`逐行列成表（列名为`campaign_id`、`round_id`、`purchase_id`、`gift_id`、`month`或`pass_id`，同一目标只列一行）；登录奖励页签新手总数变化的`repair`节没有`targets`，按通用字段列表显示`command`、`refused_operations`与`client`。
- 投票页的投票券过期、强袭挑战权、投票料理三个账本另有通知记录表，首列分别为物品ID、活动编号、投票券物品ID，其余字段逐列显示，通知时刻`notified_at`按JST显示（Unix毫秒与ISO文本两种存储形态都转换）；读取失败时显示错误，没有记录时显示「没有通知记录。」。命令一节列出votes域的全部命令（`vote_tickets.grant`、`votes.reopen_settled`与`votes.adopt_*`迁移命令）；账本视图只读，定义变化或已结算票种重新出现库存时由这些破坏性迁移命令改写。
- 配置页的切换报告在检查一节列出「审计记录体积」，超限时列出字段名和字节数；迁移切换在分档原因里列出「料理阈值变化，存档的料理账本随切换迁移」或「新手奖励总数变化，存档的登录奖励账本随切换重建」，「存档迁移」一节给出按旧规则结算的时间累积字段、料理账本的业务月、阈值与本月额度的前后值、本月已发份数和迁移时刻，以及新手奖励总数与已领新手奖励的前后值；含时间累积规则、料理阈值或新手奖励总数变化的切换不能暂存（`config_stage_unsupported`）。报告的`notices`逐条显示为警告条：被换用的塔兑换商品会列出商品ID、65与69在改绑之前整体拒绝、客户端弹出通信错误并原包重发，以及修复命令`tower_trade.rebind_product`，提示不影响切换按钮；没有专门文案的提示显示代码与明细。运行中切换对客户端静默的要求按报告的`client.quiet_seconds`显示时长；`admin_client_active`的提示不写死秒数，指向状态页的「静默阈值」或报错详情里的`quiet_seconds`。计划保留份数与期限、候选世代期限、审计字段上限、自动备份保留份数和切换栅栏的等待时长在`api.js`各有一份镜像常量（`PLAN_CAPACITY`、`PLAN_TTL_SECONDS`、`CANDIDATE_TTL_SECONDS`、`AUDIT_FIELD_BYTES`、`AUTO_BACKUP_KEEP`、`ADMIT_TIMEOUT_SECONDS`），`api.js`的报错提示、计划流程、配置页、备份页与单位页的文字由它们拼出：自动备份份数都引用`AUTO_BACKUP_KEEP`，只有备份页的保留说明读`GET /backups`返回的`retention.auto_keep`。手动备份原因的上限由备份页的`MAX_REASON`镜像`storage.MAX_BACKUP_REASON`，输入提示与校验文字都由它拼出。测试核对这些镜像与服务端常量相等，并拒绝任何控制台脚本再写死自动备份份数，也拒绝备份页在`MAX_REASON`之外另写原因上限。

## 扩展约定

管理模块按文件名自动发现，各包不共同编辑清单，每个模块导出`register(registry)`：

- `admin_cmd_*`：登记`AdminCommand`。参数模型及其嵌套模型都必须是strict加`extra=forbid`；touched中每个域都要在预检域表里有条目，或在`preflight_none`里写明理由（不得豁免已映射的域）；客户端从不读的账本应在预检域表里登记为不预检域并写明理由，而不是写在命令的`preflight_none`里；共享表字段用`表.字段`声明（如`config.UseBgm`、`card_status.A7`），每个字段只能有一个写入方，声明了相同`group`（操作名的整段前缀）的同组命令可共享；破坏性标记与确认短语必须同时给出。违反任一条都会让注册失败，serve随之起不来。命令可用性与参数辅助（`requires`、`requires_all`、`first_unavailable`、`required_service`、`missing_service`、`require_distinct`、`counted`、`BOOLEAN_HINT`、`COMMAND_UNAVAILABLE`）由`admin_command`提供。命令包合入后要复跑预检覆盖测试。发邮件的命令只能经礼物服务的`preview`/`enqueue`生成邮件行，不得直接写`tables.presents`，否则会绕过开包券等投递拒绝。
- `admin_views_*`：登记`AdminView`；`read(context, params)`中的`context`提供`profile`、`state`、`revision`、租约内的`generation`、冻结的`now`和`services`。结果跟随服务端时钟、磁盘文件或管理表变化的视图必须标`live=True`，否则会被ETag缓存。视图只经服务的公开读取器取数：守卫测试按语法树扫描全部`admin_*`模块，不允许访问其他对象以`_`开头的成员（dunder除外）、导入私有名，或经`getattr`这类反射取私有名。
- `admin_routes_*`：登记`AdminRoute(name, build)`，`build(services)`返回挂在`/admin/api/v1`下的`APIRouter`；不同模块重复声明同一方法与路径时启动失败。

页面模块放在`admin_ui/pages/<name>.js`，导出`Object.freeze({ title, order, mount(root, { signal, params, navigate, session }) })`，`mount`可以返回清理函数。页面没有内联脚本或样式，颜色、间距、字号都用`:root`的CSS变量，不写`!important`。

## 安全边界

- 只绑定回环地址，端口独占；令牌每次启动更换，只走Authorization头；Host、Origin、Sec-Fetch-Site三重校验，加上只收JSON和自定义Authorization头，跨站请求无法成功。
- 不伪造游戏请求，不直写SQLite：写入只经过`Store.commit_admin`、`record_admin_failure`和备份接口（列出备份会顺带清理过期自动备份），离线命令同样只用这些接口。
- 边界守卫测试基于导入图：从所有`admin_*`模块出发，连同内核模块`config_registry`、`config_sets`、`generation`、`item_classes`、`quest_record_admin`、`unit_removal`、`unit_growth_edit`、`wallet_money`和`letstd_*`（现为`letstd_catalog`、`letstd_rules`、`letstd_service`、`letstd_state`）一并扫描；只被管理代码引用、游戏入口不经管理模块就到不了的项目模块也一并扫描；游戏侧模块指从`cli`出发、不经管理与内核模块就能到达的模块，`quest_modes`按名称加载的关卡模式模块（`MODE_MODULES`）也算游戏侧。禁止构造`RequestEnvelope`、经注册表解码、调用`encode_wire`、`prepare_request`、`update_state`、`.connection(`，禁止用`getattr`或字符串键间接取这些名字，禁止导入`sqlite3`、`urllib`、`http.client`，禁止调用上述接口以外的Store写方法；扫描集合为空时测试失败。
- 离线运行：不暴露`resources-index`、`resources-collect`这类会联网的命令；界面没有任何外链；所有产物留在数据目录内，不触发浏览器下载或上传；导出脱敏且不能导入；备份文件含密钥，只能放在数据目录内（默认`runtime/server/backups`）。

## 限制与不支持的操作

- 不提供删除领域账本、清空回执、删除未领邮件或修改PPT.A9通知类别的命令；pending只能由原服务重试，或由显式列出的迁移命令处理（见「迁移」）。
- 战斗命令只覆盖日志、记录、塔账本与讨伐水位（中止、投递待发邮件、编辑关卡记录与归档、塔与大討伐的修正），不提供代玩家开战或结算的命令；复刻窗口在战斗页预设、经`/config`校验后按批次整组切换或暂存，改到服务资源时属content档。
- 不提供74旧编队导入命令，旧数据由客户端首次保存时导入；待导入期间`squads.save`拒绝写入。
- `unit_dynamic_overrides`不是领域账本，允许经`units.overrides.remove`手工移除。
- 不提供直接设定等级、改名、编辑教程标志、代玩家领取召唤券、编辑任务进度或提前解锁ヒストリー公开条件的命令。
- 主表快照没有删除入口；time_accumulating字段（`quest-rules.charisma_recovery_seconds`以及`tower-rules`的`generation_hour`、`replenish_hour`）只能在运行中apply，不能暂存到下次启动；改变签到料理阈值或新手奖励总数的切换同样只能在运行中apply（`config_stage_unsupported`）。
- 页面请求以`referrerPolicy: "same-origin"`发起，修复了no-referrer策略下POST的Origin被序列化为`null`、过不了精确比较的问题；三种Host（`127.0.0.1`、`localhost`、`[::1]`）的浏览器实测按用户决定不做。
- 客户端对切换栅栏的容忍：栅栏最多让请求等10秒，短于WinINet的30秒接收超时（客户端只设了连接超时10秒、连接重试2次、退避5秒，没有应用层看门狗），所以栅栏内的请求要么在新世代正常处理，要么收到503 `generation_switching`并弹「通信エラー…番号：503」，玩家确认后原包重发、由回执精确重放兜底，不会静默重试。挂机客户端的周期请求只有菜单场景每180秒的心跳、每1800秒的153和有日志时每30秒的作弊上报，心跳与153都只读；即便如此，修改存档仍应在客户端空闲时进行——在途写请求重放会收到`*_retry_stale`。资源相关变更（content档）仍要求重启客户端：客户端虽会在下一次菜单心跳发现AssetPath变化后自行回标题重建资源索引，但已载入的表是否全部重读未逐一核实。
