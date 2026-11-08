# 登录额外料理

115已接入额外恢复品，和普通印章、新手奖励一起入箱。当前月总额度为`max(0, min(25, (上月消费 - 250) // 15))`，250从已验证的唯一正CookGainBorder取得。250、264消费无份数，265为一份，625及以上为25份。额度不会每天重复发满。

每个JST04:00登录日最多发一封，缺席不补领多份，剩余额度不带到下月。当天普通印章发不出时（缺月表或缺位置、命中未启用或不完整的券池等），印章记入`login_stamp_pending`，料理照常发放，不因印章拒绝整笔，见`login-rewards.md`。额度已用完则正常继续发普通印章。

## 物品与领取

发件走`login-rules`的`cooking_sender`两种模式：

- `item`（默认）：发`item_cook_logbo_00004`，Type22、Content1。它是当前资源中已验证的通用魅力恢复药，没有指定角色头像或食用事件；这是明确的本地种类选择。可用`cooking_item_id`换成其他PresentRecoveryCatalog支持的恢复品，未验证物品、改变的效果或缺失恢复依赖明确拒绝，不发一个以后无法使用的代替物。
- `affection`：按公开资料的官方规则发角色料理（Type21，Content为送出者CardID）：在全部兵营的持有女性单位（CardData.Kind=1，客户端GetGender的口径）中取好感最高者，好感100以上一律视为同值；并列时以玩家ID、登录日与发放序号做确定性随机抽一名，同一封邮件重算结果不变；没有女性单位时由政務官アンナ（CardID 188）送出。送出者目录经投票料理目录的同一份CardData校验（每个女性单位与アンナ都须有料理定义，不符回503 `login_cooking_sender_catalog_invalid`；投票料理目录或CardData不可用时回503 `login_cooking_sender_unavailable`）。发件时选出的送出者记在料理账本的`last_sender`，料理邮件的CardID取自它。

两种模式都计入115的内容指纹：切换模式改变内容版本，旧回执按原字节精确重放、不重新选人，新请求按新模式发件。affection模式的回执还要求料理目录仍接入且内容未变：切回item模式后撤掉料理目录，或料理目录的CardData、NameText等来源资源更新后，这些回执的原包重发回409 `login_reward_retry_stale`，与投票料理依赖同一口径；仍为affection模式时料理目录不可用会使登录奖励服务整体不装配（readiness报`login_cooking_sender_unavailable`），已有登录账本的存档随之以503 `login_reward_service_unavailable`拒绝启动。

可在`--login-rules`或启动脚本`-LoginRulesFile`配置，示例在`config/login-rules.example.json`。

115只入箱，不立即增加体力、不扣晶、不写库存或剧情已读。之后1号接口单封领取，复用恢复服务：先结算自然恢复与月度扩容，再增加一份当时的最大魅力，并检查配置总上限。117批领不会吃料理，料理邮件保留待单封领取；重复请求不会再次恢复。

## 日期与账本

`login_cooking`保存版本、所属月、冻结阈值和额度、已发份数、最后登录日、最后民用发件日、时间水位及基线来源。CC对应当前月已发数；CD对应实际发件瞬间的JST民用日期，编码为`date.toordinal()-1`。去重使用独立的JST04登录日，不能直接用CD。因此03:59与04:00可属于不同登录日，CD却相同。登录日、CD所用的JST民用日与所属月都由`business_calendar`统一计算（`business_date`、`civil_date`与`month_key`），服务器时刻落在客户端日历（JST 1970-01-01至3001-01-19）之外时回`login_cooking_clock_invalid`。

月切重置当前月份数及额度，保留上次实际发件日期。33/34/35通过同一个月切提供器依次结算消费、容量与料理，再统一预检回包、提交。任一阶段失败都不保存部分月切。同月读取不更新时间水位或修订版，也不发奖励。

旧档CC/CD为0时可建立零基线。非零时必须通过`legacy_cooking_period`明确月份，不能从CD的公历月反推，因为月初凌晨可能仍属前月登录周期。明确的旧月记录会保留至正常月切；同月旧已领份数不追补。新本地账号不能通过该参数携带非零初始记录。

既有成功115当天升级不会补发料理。新策略纳入115内容指纹，旧成功请求保持过期，不重新分配印章或药。正常新请求的登录日、CC/CD、全部新增邮件、最后发件清单和回执使用同一个CAS。`login_reward_delivery`独立记录最后一次实际发出的邮件编号，回执重放必须与其一致，包括无新邮件的成功请求。

## 阈值迁移

资源中唯一的正CookGainBorder变化后，存档冻结的阈值与之不符：存档照常加载，33/34/35的月度准备照常结算月消费、跳过料理同步、不改料理账本，也不记degraded；只有115在迁移前回503 `login_cooking_rules_changed`，并记一条WARNING，写明存档阈值、当前阈值、客户端弹通信错误并原包重发，以及运行中用`login_cooking.migrate`（确认短语`MIGRATE LOGIN COOKING`）、停服时用`login-cooking-migrate-plan`/`login-cooking-migrate-apply`修复。控制台`events.login_rewards`的`cooking_repair`节（`command`、`offline`、`refused_operations`、`client`）与doctor的`readiness.saved_login_rewards.cooking`给出同样的修复命令、被拒的请求号与客户端后果，`progress.login_cooking`视图的`rules`节给出与doctor同形的核对，`state.browse`的`login_cooking`账本报同一错误码；停服换资源后重启也会出现这种状态。doctor这一节报告`saved_border`（没有料理账本时为null）、`current_border`与`changed`，变化时附`refused_operations`、`repair`、`offline_repair`与`client`。存档的上月消费基线变化后，同月的115以503 `login_cooking_invalid`拒绝，33/34/35的月度准备同样失败，按存档原样应答并在启动诊断记为degraded。两种情况都要显式迁移，不静默改已领份数。

`migrate_cooking`改用新阈值；只有料理账本、消费账本和迁移时刻属于同一个业务月时，才按存档上月消费重算本月额度。已发份数、最后领取日、CC/CD和时间水位都不变。账本所属月已结束时只换阈值，额度在下一次月切按新阈值重算，所以停服跨月后也能迁移。新额度低于本月已发份数时拒绝（409 `login_cooking_quota_below_issued`），显式旧档基线除外，需要等下个业务月再迁移。迁移时刻早于存档时间水位时回409 `login_cooking_clock_backwards`。

迁移有三个入口，规则相同：

- 控制台`login_cooking.migrate`用当前世代的阈值和服务器时间执行，属破坏性操作，确认短语`MIGRATE LOGIN COOKING`，执行前自动备份。账本已用当前阈值、本月额度也无需重算时，计划照常生成并带警告，提交只写审计，不备份也不推进修订号。停服换资源后重启时存档阈值可能与运行中的世代不同，它把账本迁到当前阈值；阈值一致时只处理消费基线变化后的本月额度重算。
- 资源或主表变化使候选世代的阈值与存档不同时，控制台配置切换在候选初始化之前，以切换时刻一并执行迁移，报告`reasons.cooking`与`migration.cooking`。这类切换至少属于gated档，写入前自动备份；不能暂存（409 `config_stage_unsupported`），因为运行中旧世代的115会拒绝已迁移的账本，而下次启动后115会拒绝未迁移的账本。超发或时钟倒退记为迁移检查失败，不允许切换。
- 服务停止时用`login-cooking-migrate-plan`/`login-cooking-migrate-apply`按下一次启动的资源迁移。plan验证迁移后的存档会被下一次启动原样接受、所有客户端读取都能编码，再写出只属于该数据目录的独立计划文件；不需要迁移时回409 `admin_login_cooking_current`，不生成计划。apply重新核对阈值、修订号、存档摘要和整份计划，任一变化回409 `admin_cooking_plan_stale`；通过后先自动备份再写入审计，重复执行返回already_applied。命令参数见`docs/admin-console.md`。

## 验证范围

定向测试涵盖0/1/25份额度、迟登录、月切、03:59/04:00同CD、旧非零基线、邮件容量及编码失败、CAS竞争、配置变化、重启回执、实际吃药与消费/容量/料理三账本联动。原生料理界面尚未实机验收；本轮没有启动游戏或服务，也没有修改实际存档。

原生依据见`aigis_research/permanent-20261101/login-cooking-contract.md`。现有`item`和`affection`模式均可作为私服规则使用，默认仍发通用药；角色料理通过既有`cooking_sender`配置启用。2026-11-06用户已接受本地奖励时机与资格设计，官服具体邮件形态、同卡副本候选权重及活动、回归料理资格不再阻塞开发。沿用现有月额度、每日投递、确定性选人与防重实现，未据此宣称官服一致性或实机验收完成。

本轮221个独立定向用例的最新结果全部通过，汇总`artifacts/login-cooking-verified-tests.xml`列出分批来源。`login-cooking-doctor.json`确认默认配置、启动与登录依赖就绪，以及实际数据库大小和修改时间未变。未运行全项目测试。
