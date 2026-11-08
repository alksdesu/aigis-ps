# 能量时间与旧存档迁移

原生自然恢复使用JST（UTC+09:00）。新存档、自然恢复、结晶恢复、关卡扣除和升级回满现已共用这套时间编码。后台默认时钟返回带时区的UTC真实瞬时，能量模块显式转换为JST；注入的无时区datetime明确表示JST服务器墙钟，不再依赖运行机器的时区。`tests/test_business_calendar.py`静态检查`src/aigis_ps`不读宿主时区：无时区或时区为None的astimezone/now/fromtimestamp、today、utcnow/utcfromtimestamp、time.localtime/mktime/ctime/strftime/tzset/asctime与time.timezone/altzone/tzname/daylight都算读取，从time导入这些名字也算；未调用就作为默认值、参数或变量保存的时钟（如`clock=datetime.now`）同样算读取。

## 时间范围

`energy_clock`顶层标记只覆盖GameParam的CharismaChangeDate/Time和ActionPointChangeDate/Time。日期是从公元1年开始的日序，时间是float32日内小数；恢复按整秒还原，保留不足一个恢复间隔的余量。

战斗started_at/completed_at和退出重试窗口继续使用真实Unix秒。created_local、GameStartDateTime、首通日期、结晶取得日由`business_calendar`按JST计算，不读宿主时区（字段口径见`FIELD_CALENDARS`）：created_local与GameStartDateTime写建档瞬时的JST墙钟文本（`wall_text`，`YYYY/MM/DD HH:MM:SS`）；结晶取得日（关卡首次★结晶与邮件领取）取JST民用日序（`civil_day`，0点换日）；首通日期FirstDefeatDate取JST04业务日序（`business_day`，660F70：JST 04:00前民用日减一，与LastLoginDate同域）。邮件与关卡奖励都经这几个函数取日期，时钟落在客户端日历之外（JST 1970-01-01至3001-01-19以外）时回503`business_clock_invalid`。新档在顶层写`business_calendar`标记`{"schema_version":1,"utc_offset_minutes":540,"origin":"new_local"}`；未写标记的旧档视为旧宿主日历，保留原值、不改写；已有存档且能量时钟就绪（`saved_energy_clock.status`为`ready`）时，doctor在`readiness.saved_business_calendar`中只读报告其状态：`marked`（同时给出`marker`）、`legacy_host_calendar`（没有标记的旧宿主日历档，`marker`为null）或`invalid`（`error`为`business_calendar_invalid`）；读库失败时为`{"error": 码}`，没有错误码的读取失败为`saved_business_calendar_unreadable`。这一项不计入startup_ready，不阻断启动，也不改写旧档；新库或能量时钟待迁移时不出现这个键。

GameStartDateTime也是History公开天数的起点：开战与委任按客户端规则取它的日期部分（空串或"0"按2016-04-14起算），用GameParam2的LastLoginDate（登录奖励日）减去它，不读当前时钟。新档记录建号瞬时的JST墙钟。没有business_calendar标记的旧档仍是当时的宿主本地墙钟（本机UTC+8宿主上于JST 0–1点建号的存档落在前一天，History早一天公开），不会自动改写；ヒストリー的天数门控保持原生，控制台不提供编辑GameStartDateTime或提前解锁的入口。

JST04营业日与业务月统一由`business_calendar`计算（`business_date`、`business_day`、`business_month`，民用日为`civil_date`、`civil_day`），各模块不自行换算：登录奖励与签到料理、DailyGift、回归、购买日奖、活动签到、投票日票、序列码窗口、月消费、月容量、双六、任务周期与时间水位、首通日期都经它取日；原生日序与星期（`native_calendar.native_day`、`native_weekday`）取JST民用日，塔的两个日键取`civil_day(时刻−规则小时)`，DropBonus按开战或委任时刻的JST星期（周日为1）取值，任务窗口用客户端整秒时钟（`native_clock`）比较StartDateTime/EndDateTime（见`missions.md`）。它们都只用服务端时钟，不经过`energy_clock`编码。

时刻落在客户端日历之外时，邮件与关卡奖励的取得日直接回`business_clock_invalid`，其余模块把它转成自己的错误码：原生日序与星期回503 `quest_clock_invalid`，塔日键回503 `tower_clock_invalid`，任务请求回`mission_clock_invalid`，序列码回503 `serial_code_clock_invalid`，购买日奖礼包的出售窗口回503 `purchased_login_window_invalid`，登录奖励、签到料理、DailyGift、回归、购买日奖、活动签到、投票日票、月消费、月容量与双六分别回`login_rewards_clock_invalid`、`login_cooking_clock_invalid`、`daily_gift_clock_invalid`、`comeback_login_clock_invalid`、`purchased_login_clock_invalid`、`event_login_clock_invalid`、`vote_ticket_clock_invalid`、`crystal_spending_clock_invalid`、`capacity_clock_invalid`与`sugoroku_clock_invalid`。存档里已记录的时间落在日历之外时按对应账本损坏处理，例如任务时间水位回503 `mission_state_invalid`。日历内的结果与原换算逐日相同；实际存档的时间都在日历内，只有手工构造到日历之外的存档会由可加载变为损坏。

回归、购买日奖、活动签到、DailyGift、LetsTD、投票日票、投票过期、登录奖励、签到料理、月消费、月容量与双六存下的Unix毫秒（如账本的`last_time`）都由`energy_clock.unix_milliseconds`求得：自`UNIX_EPOCH`（1970-01-01 UTC）起的毫秒数，向下取整（纪元前1微秒为−1）；UTC日历放不下的时刻抛OverflowError，各模块转成各自的`*_clock_invalid`；已存的毫秒按`UNIX_EPOCH + timedelta(milliseconds=…)`还原。src里一毫秒的步长`MILLISECOND`只在`energy_clock`定义；`native_clock`与`mission_window_revival`都从这里取，后者据此把任务窗口写成自`NATIVE_EPOCH`（0001-01-01 JST）起的毫秒。`tests/test_energy_clock.py`的语法树守卫要求`src/aigis_ps`里`energy_clock`之外的模块不写整数86400000（含乘式中整数常量之积为86400000的写法，如`days * 86400 * 1000`）、`datetime(1970, 1, 1, …)`、`fromtimestamp(0, …)`与`utcfromtimestamp(0)`，也不减去`UNIX_EPOCH`；`native_clock`的浮点日毫秒，以及任务时间水位与交易历史按公元1年起算的毫秒不在此列。任务时间水位（`mission_state`的`last_time`）和交易历史的`last_server_time`存的是自0001-01-01 00:00 UTC起的整毫秒数，统一由`native_clock.day_one_milliseconds`求得：无时区时刻按JST墙钟读（交易会先拒绝无时区时钟），结果向下取整到毫秒；UTC日历放不下的时刻会抛OverflowError，包括早于`UTC_DAY_ONE`或晚于9999-12-31 23:59:59.999999 UTC的时刻，任务把它转成`mission_clock_invalid`，交易转成503 `trade_clock_invalid`。任务水位的上限`MAX_DAY_ONE_MILLISECONDS`对应9999-12-31 23:59:59.999 UTC，即315537897599999；已存的水位按`UTC_DAY_ONE + timedelta(milliseconds=…)`还原。

日期与月份文本的形状由`business_calendar`的三个正则给出：`DAY_TEXT`（`YYYY-MM-DD`，只查形状）、`MONTH_TEXT`（`YYYY-MM`，只查形状，读取方随后构造日期时拒绝01–12以外的月份）与`CALENDAR_MONTH_TEXT`（`YYYY-MM`且月份为01–12，双六回执与塔兑换账本用它）。原生日期时间文本的形状同样由`business_calendar`给出：`NATIVE_SECONDS_TEXT`要求「年/月/日 时:分:秒」，年4位，月、日各1–2位，两个日期分隔符须同为`/`或同为`-`，时、分、秒各2位，背景的Begin/End、登录奖励档位的OpenDate、皮肤商店的OpenDate/CloseDate以及双六的StartDateTime/EndDateTime用它；`NATIVE_MINUTES_TEXT`的日期部分相同，但时为1–2位，秒可以省略，活动通行证目录的StartDate/EndDate、通行证复刻的StartDate/EndDate以及复刻规则的start_at/end_at用它，复刻另外接受带`T`的ISO时刻。两者都只检查形状，月、日、时、分、秒的取值范围在随后构造datetime时检查。`tests/test_business_calendar.py`的语法树守卫要求`src/aigis_ps`里`business_calendar`之外的模块不另写这五种正则，只增减或改换分组括号的写法也算另写。

新存档的标记为：

```json
{"schema_version":1,"encoding":"native_day_fraction_f32_seconds_v1","utc_offset_minutes":540,"origin":"new_local"}
```

迁移后的origin为converted，并记录source_utc_offset_minutes。有效标记不会再次平移；坏标记不会退回默认时区。未标记存档在启动和能量相关读取/交易前明确要求迁移，避免把旧锚点直接按JST使用。标记的键名、schema_version、JST偏移540、两种origin（`new_local`与`converted`）以及来源偏移±840分钟的上限，分别由`energy_clock`的常量`MARKER_KEY`、`SCHEMA_VERSION`、`UTC_OFFSET_MINUTES`、`NEW_ORIGIN`/`CONVERTED_ORIGIN`、`MAX_SOURCE_OFFSET_MINUTES`给出；读标记、转换与迁移计划时，来源偏移是否有效统一由`source_offset_valid`判断，有效值是−840到840之间的整数，布尔值和浮点数都不算。`business_calendar`标记里的`utc_offset_minutes`与`origin`也取自这组常量。

## 预览与应用

迁移工具分为`energy-clock-plan`和`energy-clock-apply`。必须先停服务，确认来源时区，再生成计划。来源偏移单位是分钟：本项目旧中国标准时间为480，日本时间为540。不能从当前机器时区推断任意导入档的来源。

来源日期按本项目旧版日序规则解释：先固定来源墙钟09:00，再将Unix天数向零截断。对于其他偏移，工具还原该日期边界后再转换；不把所有来源都套用简单加一小时。存在不可唯一还原的日期或未知零锚点时拒绝迁移。

在项目目录的PowerShell中先初始化环境：

```powershell
. .\environment.ps1
$taskPython = Initialize-AigisEnvironment -ProjectRoot $PWD.Path
& $taskPython -m aigis_ps.cli energy-clock-plan --database .\runtime\server\aigis.sqlite3 --source-offset-minutes 480 --output .\artifacts\energy-clock-plan.json
& $taskPython -m aigis_ps.cli energy-clock-apply --plan .\artifacts\energy-clock-plan.json --backup-directory .\runtime\backups
```

预览只读，不修改SQLite，也不创建其WAL/SHM副文件。计划包含规范路径、文件身份摘要、预期修订版、状态及账号摘要、四字段前后值和转换后标记，不含账号密钥、完整存档或缓存报文。

应用先重新核对计划，生成并验证完整SQLite备份，再在锁定事务内核对当前快照。只有原计划仍匹配且完整34/PGP预检通过时才写入；修订版加一、updated_at更新。文件替换、状态变化、并发写入、备份或预检失败均拒绝提交。正常重复应用同一计划只返回already_applied。

预览、应用和启动检查都用严格JSON读取存档。遇到重复键、NaN/Infinity、溢出数字（如1e999）、嵌套超过64层或解析递归溢出时，预览和应用返回503 `energy_migration_database_invalid`，启动检查报status=invalid，一律不写库、不生成备份。serve启动前要求启动检查为ready，所以这类存档会被拒绝启动，doctor的startup_ready也为false。

## 保留范围

转换只修改四个能量字段和标记，不结算自然恢复、不发体力、不扣结晶。角色、物品、剧情、邮件、当前余额、全部回执、已处理请求摘要与尚存的时间水位保持原值。

旧回执的原修订版不被重写。迁移后，旧恢复请求明确过期，不会被当作新请求再扣一次；已付费战斗的冻结规则、随机结果、奖励预留和开始时间保留，可以直接结算。无需重开或再次付费。

这是针对本地私服SQLite的工具，不处理DMM账号或原版游戏存档。`doctor`会将资源就绪与已存能量时钟状态一起报告，未迁移旧库不会被误报为可以启动。

## 验证

定向验证覆盖不同带时区表示的同一瞬时、全天float32整秒往返、跨午夜、旧日期生产规则、幂等转换、离线只读、完整备份、陈旧计划、CAS竞争、失败回滚，以及恢复/开战/结算/旧回执联动。相关模块回归354项、后台集成11项、CLI及既有交易联动10项分批通过，未运行全项目测试。

实现依据见`aigis_research/permanent-20261101/natural-recovery-clock-review.md`及`natural-recovery-clock-migration-plan.md`。正式应用结果与备份路径另记录在迁移结果文件中。

## 当前本地存档应用记录

2026-11-01按`artifacts/energy-clock-migration-plan-20261101.json`对`runtime/server/aigis.sqlite3`完成迁移。计划绑定的修订版为0，应用后为1；两组锚点各转换3600秒。完整备份为`runtime/backups/energy-clock-0d929d5b2e994dfbb17953c1401e7d70.sqlite3`。

`artifacts/energy-clock-migration-verification.json`记录独立回读结果：数据库完整性正常、备份哈希匹配、其他存档状态和身份完全一致、请求日志未变化。应用详情在`artifacts/energy-clock-migration-result-20261101.json`。此操作没有启动游戏或服务器。

迁移模块最终55项通过，包含旧09:00日期生产规则的独立反例；能量模块新增/受影响89项通过，其余98项已在此前对应范围验证。`artifacts/energy-clock-doctor.json`保存应用后的启动与能量标记检查。
