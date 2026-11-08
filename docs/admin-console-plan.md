# 控制台预留与验收要求

用户明确要求保留控制台、数据修改和更新适配能力。控制台已作为同进程的本机管理面接入serve，路由、命令、视图与用法见`docs/admin-console.md`；本文件记录各领域管理操作必须遵守的接口与验收约束，新增命令按这些约束复用游戏内核。

## 数据与操作入口

控制台需要覆盖玩家角色/形态、等级与技能、好感度、剧情解锁与已读状态、队伍、背包、货币、礼物、任务、活动和本地规则。具体字段以当前客户端与主表可支持的结构为准；不把一个显示字段当成全部游戏效果。

游戏协议和控制台应复用同一套校验及状态变更命令。已有的角色/配置校验、物品入账与扣除、钱包更新和筛选状态函数是基础。仍内嵌在协议处理器中的事务逻辑，在接管理页面前提取到共享命令层；不让管理页面通过伪造游戏请求或直接修改SQLite绕过校验。

自动选材偏好复用`patch_configuration`校验两个有符号32位值，保留尚未解释的位；偏好修改与实际选材、合成交易分开。显示为开关的界面也不能把完整F6/FB字段缩成布尔，提交后通过27响应或36完整配置回读。控制台`config.auto_select.set`一次提交完整的F6/FB两个有符号32位值，未解释的位原样保留；`config.sort.set`一次提交F1/F4/F5三个字段，字节上限是36固定字符串长度减1字节终止符，XML装不下的字符回422。

主页背景复用`BackgroundCatalog.guide_id/selection`预览当前秘书和可选范围，`merge_backgrounds`按CardID更新完整行并保留其他绑定。SaveTime会影响原生新活动背景优先级，不能把它当成无关显示字段；普通选择新动作更新JST保存时间，重试保留原时间。背景历史、场景已读与角色归属彼此独立；管理侧若允许覆盖日期或使用内部页面背景，应作为显式管理操作，不伪造135普通菜单请求或清除旧回执。

编队称号使用PlayerTitle物理行编号，不能使用GroupId、级别或物品的展示EmblemID代替。管理端预览资格复用EmblemCatalog；83种道具领取只增加库存，不自动选择称号或发王子实例。当前20队的名称/称号由TGI同组所有行保持一致，空队需要可重建的空槽表示；管理侧编辑成员、当前TN和元数据需明确区分，并保留旧迁移标记及回执。控制台`squads.save`把成员、元数据和当前TN/RTN选择作为三个可选部分，在同一次提交中依次应用，不写游戏回执；提交推进修订版，已由服务器提交的120原包重发时回409 `current_squad_retry_stale`。

合成的只读报价为`TrainingCatalog.quote_materials`（不依赖协议请求）与`training_math`，`quote`只是原生5的适配层；控制台成长消耗沿用原生素材保护（王子/锁定/编队）与UniqueID高水位，`units.evolve`和`units.train`已接入。控制台删除/消耗单位前应复用`GiftService.preserve_history`，冻结旧档已达成但尚未建立账本的好感奖励档位。经验编辑需区分累计经验和受职业上限约束的显示等级；当前用户选择保留溢出经验。

圣灵仓库以CardID/Count保存，独立于持有单位实例。管理侧修改库存可复用FairyFarmService的读取、扣除及响应校验；取出仍发新实例并递增编号。不能把仓库数量编辑解释为恢复原单位的成长、锁定或好感。普通合成和聚合库存请求已有持久去重，管理操作需要独立operation ID，不能挪用客户端CT替代管理审计。已实现：`fairy.adjust_stock`（只改数量，只有减少时需要确认短语和自动备份）与`fairy.withdraw`（发新实例并递增编号），均经plan/commit并带独立op_id。

出售提供UnitSaleCatalog.quote纯报价，自动范围与价格来自固定内容快照；出售事务保留人物历史且不复用已消耗身份。单位身份列已抽为unit_reference共享解析，管理命令仍应使用内部校验入口，不通过拼造原生网络包执行删除或加币。`units.sell`已实现，按原生手动出售规则经`unit_sale.UnitSale`内核执行。

银仓使用同类数量接口及独立回执，取出按标准新实例重建。SilverHouseRules.reward_count控制每个达到门槛的存入单位返还数量，默认1；这是本地配置，不改客户端资源说明。配置变更进入内容版本指纹，旧交易不按新规则重复发奖。

兵营容量由`UnitCapacity`按固定资源和当前存档计算。正常发卡要复用兵营空间检查与`credit_units`的技术检查，并保留进行中战斗的`unit_reservations`，不能因管理端发卡挤掉已开战的奖励空间。跨兵营变更分别核对受影响兵营，不以总人数不变代替检查。

新角色默认值由`NativeUnitInitializer`计算，`PlayerStateService.initial_unit_values`可用于预览，`credit_units`负责新身份、名册与奖励预留。英杰/隐藏初始职业不等于阶段0，不能在控制台另造一套普通卡默认值。编辑已有角色与新发角色是不同操作；默认初始化更新不迁移已拥有角色或重发已领取奖励。

邮件发放复用`PresentService.enqueue/claim`和`PresentItemCatalog`，固定邮件类型与通用ItemID共享库存上限。目录仅开放已验证的被动物品；管理端不能把恢复药、召唤券或皮肤副作用省略成一次库存增加。金币、物品、单位和免费结晶支持同事务领取；单独修改余额不等于成功领取邮件，也不能删除旧回执以允许重复发奖。

Type16券发放现可使用`PresentService.enqueue`的summon_ticket奖励；数量拆成独立邮件，领取使用专用事务，不能经过普通claim绕过随机结果冻结。控制台可用`SummonPoolCatalog`展示原始概率和完整性，缺失概率不归一化；默认仅启用350。强回执保存已选候选与实例，重新发券必须是新管理动作，不能删除回执或复用已消耗的PresentID。直接指定发卡和随机领券是不同操作。

月消费模块已接85/17/112/156，保留真实int64累计数，C1只是每半部饱和32767的原生显示投影。管理侧日后编辑余额不等于消费，退款不自动减去累计数；非零旧C1导入需要显式月份基线。统计与签到资格独立，旧档不能因为首次建立消费账本就补发七日新手奖。115的新旧账号资格、印章已领日期和新手已领奖数应独立展示，手工调余额或月消费不能顺带清空这些记录。

恢复品使用独立的`charisma_recovery`奖励类型与`PresentRecoveryCatalog`，通过`RecoveryService.restore_state`计算真实能量变化。每封只恢复一份最大值，不提供含糊的amount倍率；料理按已确认政策只能单封领取，事件播放仍由客户端完成。管理端不能以普通库存信用代替这条流程。

银单位兑换使用`SilverTradeCatalog`报价及素材资格，并由共享交易服务原子扣费、消耗素材和发放新实例。控制台需要区分兑换与直接发卡，不能把兑换写成对旧实例CardID的覆盖；普通交易与106的指纹和回执都要保留。

直接一觉商品使用`ShopGrowthCatalog`及共享经验策略，更新已有实例而不分配新编号。编辑成长后不追补奖励的场景可复用`GiftService.record_current_thresholds`，单独记录已达成档位；它不会发奖或写剧情已读。Card80→99属于有原生证据的特例，不是允许任意替换CardID的通用入口。

能量锚点使用独立的JST编码标记。控制台预览/编辑应复用`energy_clock`转换与校验，旧档导入必须声明源编码和来源偏移，不能只修改日期小数或整体重写所有时间。迁移复用`energy_migration`的精确计划、备份和事务校验；状态转换不能清空CT、领奖或交易回执。

觉醒报价与素材分配使用`EvolutionCatalog`、`EvolutionMaterials`；首奖由独立`EvolutionRewards`台账维护。初始化旧二觉历史与完整养成模块是否可用分开，删除或出售旧角色前不能丢掉此基线；出售与`units.remove`都在删除前调用`evolution_rewards.initialize`固化它。更改角色职业和等级不等于重新取得首奖，控制台需分别展示进度与领奖记录。一觉默认只携带溢出经验，二觉保留当前经验。

10号详情操作已经区分相对技能切换与绝对显示设置。控制台应显示最终技能状态并使用独立操作ID，不能重复发送SC=1来表示“保持开启”。服装资格复用`UnitSkinCatalog`读取当前持有量；编辑解锁物品与选择显示形态是两个动作。资源定义编辑需经有效资源视图生成新索引和版本，不能仅修改缓存正文而保留旧哈希。

服装发放后须在同一存档事务内调用`MissionService.record_acquisitions(before,updated,now)`，保留`skin_acquisition_history`防重复计数；不能只写POSSESSION后另发任务请求。旧拥有物作为基线，不自动补历史奖励。战神加护Count只表示归属，Enabled开关应复用已接入的`BlessingCatalog.validate`与`PossessionService.set_enabled`；不能把Count=1当作已启用，也不能绕过持有资格与完整响应预检。

GF通用标志应复用`universal_flags`的读取、哈希冲突校验和更新函数，不将Value限制成布尔或清空无关项。156商品的购买标志与实际服装库存是两个状态；管理动作若允许重新购买，应同时说明是否改变购买标志、持有物和取得历史，不能只清回执来绕过一次性购买规则。

名声登记时间、实例Famed和五阶段寄存数量也是独立状态。登记须复用FameCatalog资格及CardStatus更新，再由MissionService.record_fame记录新增阶段；不能把低阶展示许可等同于补写登记日期。寄存和取回走FameRepository及新身份分配，保留旧奖励历史与高水位，不用删除回执实现复制单位。

每个变更使用玩家修订版进行CAS，返回实际修改结果与新修订版；批量改动作为一个事务。角色实例用UniqueID/GetIndex明确选择，内容定义用固定主表快照标识。角色新增、礼物入账、奖励领取等可重复提交操作保留操作ID，避免点击或网络重试重复发放。

未知原生字段允许保留和展示，尚未核实含义的字段不得擅自作为新功能的写入开关。剧情解锁、已观看、关卡通关和奖励领取分别建模；调好感度不能无条件把全部剧情标为看过。

`EventViewRequest`是表版/里版交接的待播指针，23/24保存和清除不等于剧情解锁或完成。管理端应单独展示待播内容，修改时沿用Config与回执的同一事务；不能删除`view_event_history`后重放旧清除。原协议没有目标nonce，多客户端编辑需增加明确的预期版本检查。

Type22召唤券管理通过`ItemSummonTicketService`与`SummonGrants`，不得把抽数当邮件Content或普通库存量。批量发券先拆为Content1的独立邮件；领取检查全部单位位置并在一次事务发卡。控制台启用新池时必须验证完整概率、基础道具形状及全部候选初始化，不能通过修改helper对象扩大现有服务的启用范围。

Type20自选券通过`SelectTicketService`，管理发放指定SelectionConfigID并按数量拆封。选择时同时绑定原组序号与CardID，不能用任意角色ID替换配置候选。升级资源保留组内顺序和评级规则依赖；自选与随机发卡共同使用`UnitGrants`，不绕过身份、容量和旧状态保护。

玩家等级奖励通过`RankRewardService`记录基线、观察等级、各RewardID的语义摘要和实际投递邮件。正常等级增长与发奖在同一CAS中处理，缺池或空间不足保留pending。管理端须分别展示待发与已发，回退等级及修订历史奖励走显式迁移；不通过删除`rank_rewards`、清回执或修改教程提示位实现重领。既有无账本存档默认不追补。

活动通行证分别管理普通物品的EP、高级资格物品、两轨已领点位和`event_pass_state`实际发件记录。正常领奖不扣EP，取得高级资格后按已满足积分补领高级轨。控制台修改目录、资格或积分不能清空已领记录；本地复刻必须同步客户端Status/窗口与任务积分来源。116自动补领已使用独立本地规则和PPT.A9通知类别；管理端应展示`event_pass_automation.pending`及其原因，不通过删水位或改通知类别重发。已实现：`event_pass.grant_points`（按58/59口径丢弃超限与窗口外的EP）、`event_pass.claim`（与159同一内核补领）、`event_pass.rebind_definition`（定义变化的显式迁移），`events.event_pass`视图展示两轨已领与pending及原因。

登录额外料理使用`login_cooking`账本，登录日与CD民用日分开保存。管理端调整额度、旧基线或料理种类时必须保持CC/CD投影与已发记录一致，不能通过清零CC或删除`login_reward_delivery`实现重复领取；新增发放应走独立管理操作ID与同一邮件事务。已实现：`login_cooking.migrate`按当前世代阈值在本业务月重算额度（无需改动时只写审计），停服场景另有`login-cooking-migrate-plan`/`-apply`离线命令，运行中的资源阈值变化由配置切换中的料理迁移处理；停服改了阈值再启动的存档照常加载，只有115在迁移之前拒绝，可直接用`login_cooking.migrate`迁移。

## 热重载

- 玩家状态：提交后下一次服务器读取即使用新修订版。客户端缓存还需通过已支持的刷新/回读流程同步，不能只验证数据库变化便宣称画面已更新。
- 本地规则：解析、校验整份新配置后原子切换；新操作使用新规则，已开始的经济操作或战斗保留其规则快照。
- 主表与活动：先导入版本化快照、校验引用与资源，再按content档切换服务快照：默认暂存到下次启动；运行中切换要求客户端已关闭且没有进行中的战斗，不保留处理中操作依赖的旧版本，切换栅栏内先排空在途请求（最多5秒，超时放弃本次切换），因此不会混用两版卡片、物品或奖励。
- 资源：按内容清单校验大小和哈希，允许增量补齐；不能在客户端加载中覆盖同名但不同内容的缓存对象。
- 原生程序或插件变更：单独说明是否需要重启客户端，不把所有更新都承诺成无重启热更新。

serve按roll_forward → 解析配置集 → 构建首代 → GenerationRuntime的顺序运行。原生操作和`/client-assets`在代租约内读取当前代，并上报客户端活动（`/health`、`/local/status`不上报）；切换栅栏期间超过准入等待的请求返回503 `generation_switching`。未带`--admin-port`时只服务首代的固定处理器，不能切代。

配置注册表登记全部规则文件键（如`letstd-rewards`、`serial-code-rules`），配置集与切换界面自动列出它们。切换按三档评估：代码、客户端指纹、主表快照或基础/有效资源版本变化属content；含随时间累积的字段、初始化会改写存档、需要签到料理迁移或新手奖励总数重建属gated；其余属hot。gated/content切换在栅栏内与审计同一事务写入，存档会变化时写入前自动备份，状态不变时不备份。主表经控制台导入为不可变快照，可暂存或按content档激活，`import-masters --stage`同样只暂存。每个键的消费域列出指纹直接或经其他服务的内容版本嵌入该规则的readiness域，只用于描述和切换报告，切换总是构建完整的新世代：例如切换`vote-rules`会改变entrust、login_rewards、main_quests、rank_rewards、vote_assault_notifications、vote_cooking、vote_cooking_notifications、vote_expiry_notifications与votes九个域的内容版本，因为投票规则经任务奖励、委任、115/116与116通知进入它们的指纹。

普通活动签到使用`EventLoginRules`、`EventLoginCatalog`与共享`EventLoginService`，115/116共用`event_login`记录。后续管理侧需展示已发档位、资格日、投递日和pending原因；修改日期不重置序号，修复奖励来源不补缺席日。复刻窗口必须同步生成客户端资源，不能只改服务器资格。回归与购买绑定活动保留独立资格域，不能通过改Type值冒充已验证资格。已实现：`events.event_login`视图逐档列出已发、作废与pending及原因；定义变化的pending用`event_login.resolve_changed`按当前定义补发或作废，作废的档记为已用、不发邮件，序号照常前进。

回归签到使用独立`comeback_login`轮次，需分别管理真实观察基线、轮次资格、源档位、冻结期限和奖励指纹。关闭发奖不停止已有观察记录；旧轮pending不能因新轮开启而删除，补发旧轮时不制造新轮通知。管理侧修复D0/D1必须与轮次投影一致，不能只改这两个字段实现重领。已实现：`events.comeback_login`视图按轮次列出各档与pending原因；定义变化的pending用`comeback_login.resolve_changed`补发（同时把该轮之后未领各档改绑到当前定义）或作废（不发邮件、不发通知），同一活动当天已实发过一档时只改定义、次日补发。

购买日奖使用独立`purchased_login`实例。出售开关不取消已经取得的资格，原购买时间和逐档奖励指纹不可随意重置；提高累计购买上限也不能叠加当前同活动的有效或pending资格。管理侧合并礼包列表时仍须返回完整PPF，不能用单类购买记录覆盖另一类。已实现：`events.purchased_login`视图按购买列出各档；定义变化的pending用`purchased_login.resolve_changed`处理，规则同回归签到。

DailyGift使用`daily_gift`逐ID、逐资格日的独立记录。配置选择与包含首末日的复刻窗口只控制新资格，已得pending继续兑付。控制台应显示冻结奖励摘要、当时窗口、取得日和投递日，并复用DailyGiftService；不得通过删账本、更改ID_Gift或切换窗口使同一天重新发放。未知条件和缺失卡池应保留可诊断的待发原因。已实现：`events.daily_gift`视图逐ID、逐资格日列出状态、原因与冻结定义核对；定义变化的pending用`daily_gift.resolve_changed`补发或作废，作废的记录以原因`daily_gift_voided`留在账本里（视图显示为`voided`），同一资格日不再发放。

通行证复刻使用`EventPassRevivalRules`生成客户端与服务端共用的有效资源版本。后续修改必须先生成并校验完整资源，再切换快照；不能仅改服务器积分绑定。`MissionEpBindings`仅迁移可证明只有EP变化的未领取任务，保留完成进度与已领奖状态；`MissionPassPoints`统一处理入账和容量。管理操作不能通过清除绑定标记或领奖回执补发同一任务积分。已实现：`event-pass-revival`键在配置注册表中，复刻资源经控制台校验与content档切换生效，绑定迁移经`events.mission_ep_bindings`视图核对。

本地礼包复用`PackageCatalog`、`PackagePurchases`与`PackageService`。购买身份按普通/高级物品对应关系保存，不依赖可变文案、窗口或列表编号；管理库存与管理购买次数是不同操作。赠送已有资格不自动清空限购记录，移除资格也不重新开放旧礼包。当前免费礼包和原生正价平台流程保持独立，控制台不能把本地发放记录写成平台付款记录。

棋盘管理应复用`sugoroku`的按月RewardID账本和同一发货事务。不能只把Status改回0后另发一次邮件，也不能删除历史来实现重领。容量管理走`monthly_capacity`，明确编辑当前月还是下月，并同步原生GameParam字段；先结算月界自然恢复再调整，旧档非零未知月份需要显式迁移。已实现：`sugoroku.claim`与63共用领取内核代领本月奖励，`capacity.grant_next`先结算到当前业务月、只给下月加容量并要求操作者写明生效月份。

## 记录与恢复

委任复用`EntrustCatalog`和`EntrustService`，范围、门与规则见`docs/entrust.md`。`entrust_history`与普通战斗场次独立，编辑时保持已付费active和其预留奖励；不能用清回执替代资产更正，也不得删除回执来重发。关卡开放、三星及收集条件来自持久记录，批量奖励还会触发任务和等级邮件，管理入口应通过同一事务校验而非只改界面数字。

委任的只读接口已经具备：`EntrustService.status()`给出内容版本、本地规则与内存中的平台码统计；`EntrustCatalog.status()`给出审定范围、原生门、菜单路由与门、券、静态排除与降级项；`availability(now=, platform_code=, tags=)`给出与玩家无关的即时可委任计数，传入`QuestCatalog.preview_mission_tags(overrides)`可预览窗口修改的影响；`describe(quest_id, now=, platform_code=)`给出单关的成员任务，带时钟与平台时另给即时门结果；`validate()`由控制台显式触发全量构建，约10秒，逐关持锁（已实现：`POST /admin/api/v1/entrust/validations`启动后台作业、GET查询进度，同一时间只跑一个，世代切换后旧作业标stale）；`EntrustService.preview(profile, ...)`对单个存档按76的检查链做不写入的逐阶段预演（已实现：`battle.entrust_quest`与`battle.entrust_preview`视图）。status中的missing_tables、invalid_tables、invalid_entries、unverified_missions与transient_failures等降级项表示资源缺失或不可验证，应先补齐资源（resources-collect），不能靠改服务端规则绕过。

委任相关编辑须与客户端资源或master同步：任务窗口必须经资源覆盖同时修改客户端的*MissionConfig，只改服务端窗口会让客户端菜单门与服务端判定不一致；分类2/3/10/13（大討伐、复刻、神獣、練習）走`--mission-windows`规则文件（见`docs/mission-windows.md`），其他分类仍用资源覆盖（table_cell_patch）；900001的窗口整体开关ヒストリー委任的菜单门，也开关Type8的9032委任（入场另受CfgMissionHistoryTable公开条件约束；要两端都公开全部组，按原生规则把GameStartDateTime设为空串或更早日期并重启客户端，控制台不提供提前解锁入口），940012的窗口开关整个帝国（开战与委任）；Tag解析本身不看窗口，改任务的Enable、TagID或TagIDRange才会改变归属。成长活动免券窗口走`mission-windows`规则文件的`growth_campaigns`节（按MissionID整组，生成客户端资源补丁并回读核对，窗口判定与客户端时钟同源，见`docs/mission-windows.md`），不再单独覆盖GrowthCampaignConfig.atb；窗口时刻与资源日期一样落到客户端时钟网格，晚于3001-01-19的时刻会落到1970-01-01，长期免券应留空窗口。HasEntrustByType、IsEntrust、DoReturn_Type等命令文本走`command-text-overrides`配置键（`command_text_overlay.load_command_text_overrides`，只能把门改得更关、不能扩大委任类型），同时改客户端与服务端。DisableEntrust走master快照，EntrustRules只经`--entrust-rules`配置文件修改；审定集是代码常量，控制台不能扩大。编辑后按QuestCatalog→EntrustCatalog→EntrustService的顺序重建：控制台的配置或内容切换构建新一代完成这一步，资源与主表类改动属content档，需要关闭客户端并按切换结果的提示重新准备客户端资源。任何编辑都会改变内容版本，旧请求的精确重试变为`entrust_retry_stale`。委任券不足时通过PresentService邮件发放item_consignment_ticket，不直接改持有量。

塔兑换复用`TowerTradeCatalog`、`TowerTradeHistory`、`TowerTradeGrants`和`TowerTradeService`。控制台需区分改余额、补发奖励和修复购买次数，不能删`tower_trade_history`绕过限购；编辑后同步验证`tower_trade_receipts`的完整账本摘要。目录改价保留累计次数，同ID改作不同奖励或货币必须显式迁移。六个缺失完整召唤池的商品不能通过管理开关伪装已实现。兑换次数按每月1日04:00 JST重置（见`docs/tower-exchange.md`）。已实现：`events.tower_trades`实时视图展示账本，同ID改作别的商品时用`tower_trade.rebind_product`改绑；改绑之前65与69整体拒绝，配置切换报告的`notices`会提前列出候选世代下被换用的商品。

战斗只读入口：`QuestService.status()`给出boost_policy、score_policy、candidate_policy、drop_bonus_policy、event_reward_policy、event_rewards与money_policy，journal成功记录带boost、score、score_details、candidate_stars、drop_bonus与event_rewards；`QuestService.preview_event_rewards`在存档副本上预演DropBonus与关卡产票，`QuestCatalog.candidate_view`按给定MaxRank列出候选，`drop_bonus_status()`另给各Type的覆盖数。`drop_bonus_status()`的available为false应作为告警：整表不可用期间结算的场次按无加成入账，journal记drop_bonus=null，恢复后不补发；ItemConfigTable中刻水晶文本不再唯一对应一个物品时，全部带加成的关回501。已实现：战斗页经`battle.journal`、`battle.records`、`battle.record_archive`视图展示，battle域命令覆盖中止（`quests.journal.abort`）、投递待发邮件（`quests.journal.flush_pending`）、记录编辑与归档（`quests.record.edit`、`quests.record.edit_with_rewards`、`quests.records.archive_missing`、`quests.records.restore_archived`）。

塔复用`TowerCatalog`、`TowerService`与`tower_state`，规则与只读接口见`docs/tower.md`。编辑走`set_floor_best`、`adjust_stack`、`settle_now`、`redeliver_deferred`与`reset_rewards`等纯状态变换，由控制台按审计CAS提交：改层最好分时新达到的档记为已领、不补发，分数与原最好分不同时清除该层HighScoreTeam快照，`reset_rewards`会让已领档重新可发，必须明示可能重复发奖；挑战物品与塔结晶的增减走PossessionService或礼物，不直接改`tower_state`，不得删除68回执来重放领取；deferred档在启用对应券池后用`redeliver_deferred`重投，礼物箱满时不改存档。TowerRules经`--tower-rules`或配置集中的同名键修改：两个小时字段随时间累积，控制台切换前由运行中的世代按旧规则结算塔时钟（gated档），只能运行中apply、不能暂存；首访字段只影响尚未建立记录的塔，已结算的天数不回算。已实现：五条towers命令与`towers.overview`视图，见`docs/admin-console.md`「战斗（battle）」。

大討伐复用`SubjugationCatalog`与`quest_state.mode_progress.subjugation`水位。只读入口为`SubjugationCatalog.status()`、`describe(state, now)`与`support_status[6].local_rules`，describe报告水位与记录的差异、各档的next_at与remaining以及首关记录。结算只抬高水位、从不降低，不能靠降低水位悄悄重发已发档（`subjugation.watermark.resync`按记录重设水位，可升可降，降低后再次跨越的档会重发，属显式操作，下调时命令结果给重复发奖警告）；把成员记录改到水位之上时，下一场结算从记录起算，差额档按已达处理、不补发（与客户端结果页一致），需要补发只能由控制台显式寄送；describe的basis给出下一场的起点，mismatch标出水位与记录不一致的分项，结算时也会写一条WARNING。SubjugationRules经`--subjugation-rules`或控制台配置切换修改，不回算已结算的记录与水位；关闭票池464会使30023（clamp）或30019–30023（raw）开战回501，这种情况可以直接在readiness的`main_quests.mode_reward_delivery`中看到；女神档位缺池418时同理。已实现：`subjugation.watermark.resync`、`subjugation.rewards.redeliver`、`subjugation.progress.reset`与`battle.subjugation`、`battle.subjugation_progress`视图。

任务窗口的修改流程：用`mission_window_batches(base_reader)`列出可配置批次（reader读未打补丁的基础资源），写好规则后用`build_mission_window_revival(base_reader, rules)`预校验，再经控制台按content档切换（运行中切换或暂存到下次启动），或停服换文件后重启服务，并用同一份文件重新prepare客户端；生效后看readiness.mission_windows的verification与error。改窗口会产生新的有效资源版本，进行中的战斗结算回409`quest_snapshot_changed`，委任旧回执精确重试回`entrust_retry_stale`；客户端资源更新后须先核对批次键。已实现：战斗页的`battle.windows`视图列出各批次的原生与生效窗口和状态，批次预设编辑器把整份规则文档经`POST /config/validations`提交校验，报告与切换在配置页完成。

关卡产票由`VoteQuestTickets`经战斗活动挂点发放，规则见`docs/vote-events.md`。人工补票用`VoteQuestTickets.grant(state, item_id, count)`：只接受本届组票与正整数数量（否则400），该组已有`vote_expiry`记录时回409`vote_ticket_closed`，再经一次`credit_capped`按999封顶；须在审计CAS中与其他改动一起提交，不直接改库存，也不伪造战斗结算。`status(now, state=)`给出当前发票组与不可发原因，不传state时报不出`vote_ticket_closed`。

管理操作记录操作类型、目标、修改前后修订版、必要字段差异和内容版本；不记录游戏密钥、平台令牌或账号密码。保存前可预览批量修改，支持导入/导出、版本兼容检查和备份恢复。恢复同样通过校验与事务完成，不覆盖游戏正在写入的存档文件。

控制台默认属于本机管理面，与游戏会话身份分开。后续页面与管理API的访问控制、修改审计和错误返回一起验收，不因服务绑定本机就省略管理身份边界。

## 验收

1. 通过控制台改角色、好感度和物品后，游戏读取、效果和重开存档一致，未修改字段不丢失。
2. 批量失败零提交；修订版冲突明确返回；重复提交不重复发放。
3. 解锁剧情不误标已观看，改数值不重复发已领门槛奖励。
4. 热切规则/内容版本后，在途操作使用原快照，新操作使用新快照，数据更新不重置玩家进度。
5. 导入、导出、备份恢复和操作记录均在111项目内完成，并能从记录确定变更来源。
6. 战斗相关实机验收：结果页的票图标与数字、83/76的BONUS横幅与委任结果页的刻水晶数量、IOF丢弃提示、塔门户与TowerResult及HighScore面板、大討伐结果页的打包分数与奖励列表（含首关不写记录时的任务分数）、资源补丁后复刻与大討伐入口是否出现、神獣三组编成时BL的形状与Tag120门的菜单表现。

## 投票数据接入约束

投票目录支持通过`VoteRules`配置第16届复刻起点、候选消歧和本地显示政策；`vote-rules`在配置注册表中属资格类时间语义，可经控制台配置切换修改，切换后依赖它的委任、主线、115/116、料理、投票及116通知服务的内容版本随之变化。默认唯一原生正常发货模板策略只计原生PresentUnitData行，有效视图追加的本地模板按PresentID与CardID精确排除，保留18项选择证据：956名全部可用，A/B/C分别318/319/319，没有未决候选，复刻示例开窗无需`candidate_overrides`；strict模式及显式候选覆盖仍可用。PVH使用票据名与CardID的CRC32键，`vote_state`保留旧基线、累计及绑定定义。

57投票与137日票刷新已使用共享纯状态模块和单次CAS。`vote_rewards`保存固定/循环奖的冻结展示、邮件ID、旧基线和强袭资格，`vote_tickets`保存当日票日期及消费状态；控制台应复用这些校验，不通过直接改库存绕过日切账本。`vote_finalists`保存排名与晋级快照，`vote_expiry`保存每组到期余额和金币邮件结算；修改冻结后的PVH、恢复已折算票库存或删除已领邮件证明都须明确迁移。不得降低累计、变更既有票据归属组或删除领域账本重领；修改候选/奖励需显式迁移，单纯调整复刻时间不清历史，也不重开已关闭票种。关卡产票已接入战斗与委任结算，默认历史窗口下不产票；强袭战斗仍待接入，不能通过改显示字段冒充玩法完成。

`vote_cooking`独立保存每组新票基线、捕获的结果时间、冻结规则和料理邮件证明；控制台应展示tracking、pending、delivered及ineligible状态。关闭发放不删除已经取得的资格，修改门槛和份数不回写旧定义。调整PVH需同步处理料理基线及晋级快照，不能只清通知、删料理邮件或重置旧登录回执来重发。Type21的Content为角色ID，管理发件也应复用同一目录及邮件入口，避免把ID当作恢复份数。

`vote_cooking_notifications`独立记录116已经提交的通知，不能把邮件已发、已领和通知提交视为同一状态；本地管理应分别展示，投票页的`votes.cooking`视图在账本之外单列通知记录表。115/116的新日票观察和116累计通知数量是永久领域的消费证明，修改余额或清理记录时须一起校验，不允许删域后恢复当日票或再次通知旧奖励。普通票到期与强袭挑战权折金的116提示同样各有独立永久账本（`vote_expiry_notifications`、`vote_assault_notifications`），控制台的`votes.expiry`与`votes.assault`视图只读展示，各自的通知记录表与账本分开列出，不能删账本重弹提示。
