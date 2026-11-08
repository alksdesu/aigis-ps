# 委任

注册76是独立的一次性交易，不伪造77/81开战再80/83结算。委任覆盖用户审定的Type中原生门开放、模式策略可用者：当前提供Type0主线、2曜日、8复刻、16ヒストリー与18帝国大戦。后台实现与定向测试已完成；原生委任按钮与实机流程仍待用户通知后验收，未运行游戏或监听服务，没有改写真实存档及原版文件。

## 范围

审定集`quest_modes.ENTRUST_TYPES`={0,2,8,16,18}是代码常量，`ModeSpec.entrust`与`support_status`的entrust键都表示“属于审定集”，控制台不能扩大。目录考察MODE_SPECS、QuestTable中出现的类型与`HasEntrustByType_0..255`命中者的并集，逐Type依次判定：

1. 不在审定集：409`entrust_not_offered`（消息带Type）。原生类型门为真的另记入status的`unaudited_native_offers`（当前[3,17]），不放行。
2. Type≠0且类型门键歧义：pending，503`entrust_source_invalid`。
3. Type≠0且`HasEntrustByType_<Type>`不是字面`1;`：409`entrust_not_offered`。Type0不求值（6791D0），它的类型门只在status报告。
4. 模式策略未启用：pending，501`quest_mode_unsupported`，原因取策略reason。
5. `entrust_blocker()`非空：pending，501`quest_mode_unsupported`，原因取blocker。
6. 该Type的任务窗口构建失败：pending，503`entrust_source_invalid`，附原错误码。
7. 其余为offered。

两道原生门（行号0起）：

| 门 | 键 | 表与行 | _ID | 文本 |
|---|---|---|---|---|
| 类型门（6791D0） | `HasEntrustByType_2/3/8/16/17/18` | OnSystemCommand 30/31/32/33/34/40 | 1512763177 / 757981119 / -1157691849 / 1954388820 / 58371010 / -1815759277 | `1;` |
| 菜单门（776AF0） | `FCMenuQuestTypeList::IsEntrust_0/5/1/3` | OnCommandMenuQuestCommon 34/125/126/127 | 2052496187 / 171732916 / 223456173 / -480261503 | `1;` |
| 菜单门 | `FCMenuQuestTypeList::IsEntrust_4/14/15/16` | OnCommandMenuQuestCommon 128/129/130/143 | 2101042978 / 1541182918 / 752592208 / -1244465942 | `IsOpenMission( "Tag" , $Tag );` |

当前结果：offered为[0,2,8,16,18]，pending为空，静态索引114/28/207/2016/84关，`validate()`后共2449个产品，没有排除项；其余13个Type为409`entrust_not_offered`，其中有关卡的为1:61、4:9、6:16、9:343、10:452、11:92、14:123、15:16、19:21。

Type8随ReproducePolicy启用而offered：它在类上覆写的check_start已在`entrust_parity`中声明，委任代码不用改，复刻包设定的门槛关1004也对委任生效。委任与开战一样按关卡记录的原始MaxRank选候选，4807–4813不再排除，见“与开战共用的入场、奖励和任务”。206个复刻关的任务窗口都已结束，没有任务窗口规则时只有9032（属ヒストリー任务920014）开放：2026-11-03 12:00 JST平台1的availability中，Type8的206关被任务窗口挡住。9032经920014入场时同样要求920014已公开（d≥1且前置920013合计≥11星），ReproducePolicy为此覆写check_start并在entrust_parity中声明。实测9032要求门槛关1004与前置关4030，委任2次扣2张券。

## 菜单门与任务登记

候选任务m（分类c）的菜单门：

1. 查`CmnMenu::DoReturn_Type_<c>`，文本须为`SetUpdaterFunc("Func_X");`。
2. `Func_GeneralQuestMenu`的作用域链为CMenuQuestGeneral→FCMenuQuestTypeList→CmnMenu，`Func_HistoryQuestMenu`为CHistoryQuestMenu→FCMenuQuestTypeList→CmnMenu；其他菜单或无行时没有链，门为假并在status报告。
3. 沿链取第一个命中的`<作用域>::IsEntrust_<c>`：`1;`为真；`IsOpenMission("Tag",$Tag);`以m的TagID交给全局Tag解析，用服务端JST时钟按8CB420判OPEN，不加宽限；其他文本为假。

命令按名字的有符号CRC32，在91CDA0登记的106张表（`data/command-tables.json`）中查_ID。读不到的表按原生9027A0视同无表（missing）；读得到但ALTB解析失败，或缺少int32的_ID与string_ref的_Cmnd列，同样视同无表（invalid）；允许多余列，两种情况都写入status与内容版本。

命令文本可经`command-text-overrides`配置键覆盖（`config/command-text-overrides.example.json`：`tables`按表名给出`_ID`（或`#行号`）到新文本的映射，最多256条）：覆盖生成命令表的资源补丁，客户端与服务端读同一份结果。覆盖只能把门改得更关、不能开得更松（`overrides_never_open_a_command_or_entrust_gate_further`）：把`HasEntrustByType_t`或`IsEntrust_c`从字面真改成`0;`可以关掉对应委任，反向改写被拒绝，所以委任类型不能经覆盖扩大。同一键跨表或表内多命中时报`native_command_ambiguous`，只挂起依赖该键的Type或分类，不复刻91DEE0按外层表名CRC的先后。文本分类只忽略记号之间的ASCII空白，记号或字符串字面量内部的空白、全角空格都使文本按假处理；_Cmnd无法解码或为空引用时只影响该行。当前106张表全部可读，共4718行，重复_ID只有-906346476与431188304，都与委任无关。规则经start-server.ps1的`-CommandTextOverridesFile`或CLI的`--command-text-overrides`加载，客户端准备用prepare-client.ps1的`-CommandTextOverridesFile`（prepare_client.py的`--command-text-overrides`），两端必须传同一份文件；有改写行时客户端清单记录`command_texts`节，prepare的verify与launch在给出该参数时核对请求的规则与清单记录一致。readiness的`command_texts`未配置时为{enabled:false}，配置时为补丁元数据：schema_version、rules、rules_hash、policy、各表的source/effective sha256、逐行改写记录rows与委任门分类entrust_gates。

当前路由：分类0/1/3/4/5/6/7/8/10/13/15/16/17为General，14为History，2为Subjugation、12为TowerTop（都没有链），9文本为空，11无行；General与History链上只有FCMenuQuestTypeList有IsEntrust行。审定类型涉及的分类为0/1/3/5/14/16：0/1/3/5为字面真，14经Tag302解析到900001，16的TagID 902–912与801解析到任务自身。分类4（DailyReproduce）不属审定类型，它的门恒假：任务由服务端数据构造，$Tag恒为0。路由或门键歧义，或Tag门所需的全局登记不可用时，该分类的任务在请求时报503`entrust_source_invalid`，其他分类不受影响。

全局任务登记由`QuestCatalog.mission_tags()`缓存，合成分类0–3、5–17共32张MissionConfig与MissionQuestList表；分类14/17的成员取Config.QuestIdList，分类4由服务端数据构造，不入表。分类14–17按config行建任务，其余只为自身MissionQuestList列出的MissionID建任务，只有config行的任务客户端不会建出（当前200554、320118，Enable都为0）；Tower加载器不读TagID。登记谓词为`enabled(PlatformCode)`：布尔Enable取原值，整数取`(1<<平台)&Enable`。Tag解析：tag≤0无结果；否则按MissionID升序，取第一个客户端建出、对请求平台启用且TagID≤tag≤int32(TagID+TagIDRange)（32位回绕）的任务，开放与否都算（8D75A0）；结果按（tag，平台）记忆，最多4096条。跨分类出现重复MissionID时登记报503`quest_mode_resource_invalid`。

当前资源有587个任务，客户端建出585个。Tag302在32个平台都解析到900001（戦術指南任务，Enable为真、Start=End=0，覆盖302的任务共248个），801→940012，902–912→940001–940011。因此2016个ヒストリー关的委任菜单门只取决于900001是否开放，改动900001的窗口会整体开关ヒストリー委任（Type8的9032也随之开关）；入场还经HistoryPolicy.check_start镜像CfgMissionHistoryTable的组公开与任务公开（见`docs/battle-modes.md`），未公开的关回409`quest_closed`，不扣券、不改存档；菜单门在前，900001关闭时仍先报`entrust_menu_closed`；帝国分类16的门各看自身任务，分类1的Emergency任务200384 Enable为0，不登记。

## 归属任务与券

76请求不带MissionID。候选为该Type的ModeSpec分类中列出该关的任务，按MissionID升序依次检查：对请求平台已登记且OPEN、菜单门、任务最低等级、RequestQuestIds与KeyQuestID。全部失败时按到达的最远阶段报409：`quest_closed`、`entrust_menu_closed`、`quest_rank_required`、`quest_locked`。归属任务M*：消耗券的关在全部通过者中取MissionID最小的免券任务，没有时取MissionID最小者；无券关停在第一个通过的任务，不查成长活动表。M*用于免券判定、quest_clear的mission_id与诊断；当前审定类型每关最多一个已登记任务，4125按QuestIdList属于920025。在该Type分类中没有任何任务列出的关静态排除为409`entrust_no_mission`（当前0个）；有任务列出但全部未登记或在窗口外时报409`quest_closed`，前缀地图类型在构建时就得到这个码。

券按列名读QuestTable：ConsignmentTicketID（空串或字面null为无券）与ConsignmentTicketConsumptionCount，原生列号50/51，master XML中的物理位置为44/45。只对offered Type的关逐行校验：数量为int32且≥0，数量>0时必须有券，券须是堆叠上限为正的已知物品，DisableEntrust为int32且≥0；不符的关单独排除并报503`entrust_source_invalid`，不影响整个目录（当前资源没有这种行）。券费为M*免券时0，否则数量×Boost.V，发奖前按整批一次扣除；持有不足时409`entrust_ticket_insufficient`，整笔不提交；实扣时POSSESSION回券的绝对数量，扣到0也列出。当前Type2/16/18每次1张item_consignment_ticket，Type8的204关每次1张、6385–6387无券，Type0无券。

免券按GrowthCampaignConfig（93BB90、93BD50、93BF40）：按行序处理，MissionID为0时沿用上一行（首行之前为0）；同一（MissionID，ID）后行覆盖前行；QuestID为0时覆盖整个任务；DateOpen或DateClose未设即免券，否则DateOpen≤时钟<DateClose时免券。时钟按客户端AAC970取JST整秒加float32日内分数、按4 ms取整，预览用的覆写也落到同一网格。需要判定免券而成长活动表不可验证时报503`entrust_source_invalid`。

日期按AACF90解析：

- 空串或任何算出0 ms的写法都视为未设。
- 分别按`%d/%d/%d %d:%d:%d`与`%d-%d-%d %d:%d:%d`扫描，取转换字段多的一种，相等时取`-`。
- 只扫到1个字段：整串按atof取数值天数，×86400000后加0.5截断，十六进制不复刻。
- 扫到3个以上字段且时分秒全为0：日期分支（AACB20），不做4 ms取整。
- 扫到3个以上字段且任一时分秒非0：缺省字段为0，按float32求日内毫秒（分钟系数比1/1440小一个ulp），加0.5截断后按4 ms取整。
- 日数按`_mktime64`（当日09:00 JST）求，月日越界按mktime归一化，如2026/2/30→2026-03-02、2026/13/1→2027-01-01。客户端UCRT要求进位前后年份都在1969–3002且0≤t≤0x793582AFF，所以有效日期为1970-01-01至3001-01-19，越界落到1970-01-01。
- 原生结果不确定的写法判为不可验证：扫不到字段或只扫到2个、3个以上字段时有字段超出int32、时分秒为负、十六进制天数、结果非有限或超出int64（date_undetermined）；超出日历的结果记为date_unsupported。这两种都只把所属任务记为unverified，该任务的带券关在需要判定免券时报503。表读不到、缺列或MissionID不是int32时整表unverified。

当前54行全部为2026/8/20 11:00至2026/8/27 11:00 JST，沿用后为700101×7、700102×7、700103×8、700104×8、700105×8、700106×2、700201×14，28个（任务，关卡）对都属Type2，已经过期；该窗口内的2026-08-21 12:00，24个开放的曜日关免券。ItemID/Count/Ratio（活动期间的券掉落）只展示，不发放。

本地复刻免券窗口走`mission-windows`规则文件的`growth_campaigns`节（见`docs/mission-windows.md`）：按MissionID整组给出窗口，生成GrowthCampaignConfig.atb的资源补丁并回读核对，客户端与服务端从同一份补丁后的资源判定，两端同源；窗口时刻与上面的日期解析一样落到客户端时钟网格。

## 检查顺序与错误码

76依次检查：request（QI、Rev、Boost 1–3，PlatformCode经`require_platform`）→resource_version→authentication→exact_replay（不读时钟）→processed_request（最近256个已处理请求摘要）→product（Type准入、静态排除、按需构建）→eligibility（6791D0）→admission（前置关、关卡Rank、门槛关、任务选择含菜单门、679B10、特殊条件与模式覆写（ヒストリー与9032的CfgMissionHistoryTable公开条件、帝国Tag801门）；事件任务奖励族未结算时回501`quest_event_reward_unsupported`，与开战相同）→candidates（RT）→unit_space→energy_settlement→ticket（判定免券并扣除）→charisma_capacity（单次魅力上限）→stamina（行动力>0仍报501）→charisma→draw（首轮冻结；CAS重试之间记录MaxRank改变时按新候选集合重抽）→entrust_rewards→battle_events（关卡产票与EventParam）→grant→experience_and_gold→entrust_settle→missions→rank_rewards→preflight→response→receipt（回执与CAS）。阶段名与`entrust_service.CHECK_ORDER`及preview的checks一致。

时钟在首轮的product与eligibility之后读一次，CAS重试沿用。原生按钮链为6791D0→776AF0→77A690（Closed→Rank→PerfectClear→OverFlowUnit→券→魅力→体力）；入场内部沿用开战的“前置关、等级在先”，顺序只影响多重失败时报哪个码。

| 状态 | 码 |
|---|---|
| 400 | `quest_request_invalid`（请求结构、公共头或字段类型不合法，与开战共用解析），`entrust_request_invalid`（QI非正、Rev为空或超过256字节、Boost.V不在1–3、RT列不全），`quest_platform_invalid`（PlatformCode不是0..31的int，与开战同码） |
| 404 | `quest_not_found`（未知关卡） |
| 409 | `entrust_resource_mismatch`、`entrust_not_offered`、`entrust_no_mission`、`entrust_disabled`、`entrust_not_eligible`、`entrust_special_condition`、`quest_locked`、`quest_rank_required`、`quest_closed`、`entrust_menu_closed`、`quest_candidates_mismatch`、`unit_roster_capacity`、`entrust_ticket_insufficient`、`entrust_energy_capacity`、`quest_energy_insufficient`、`entrust_retry_stale`、`entrust_request_stale`、`profile_changed` |
| 501 | `quest_mode_unsupported`、`entrust_reward_unsupported`、`entrust_stamina_capacity_unverified`、`quest_event_reward_unsupported`，以及按需构建原样传出的501（如`quest_candidate_unsupported`） |
| 503 | `entrust_catalog_invalid`、`entrust_catalog_changed`、`entrust_source_invalid`、`entrust_product_invalid`、`entrust_dependencies_invalid`、`entrust_dependencies_changed`、`entrust_history_invalid`、`entrust_state_invalid`、`entrust_random_invalid`、`entrust_response_capacity`、`quest_mission_selector_invalid`、`quest_mode_hook_invalid`、`quest_clock_invalid`、`battle_event_grant_invalid`（挂点返回未知物品）及战斗活动挂点的其他503，以及按需构建原样传出的503（如`quest_asset_unavailable`） |

## 与开战共用的入场、奖励和任务

入场调用`quest_modes.admit`，与77/81开战是同一入口，StartContext带hard=False、请求的PlatformCode、request=None与目录给出的selector：先查AppearCondition前置关（409`quest_locked`，“The prerequisite quest has not been cleared”），再查关卡Rank（409`quest_rank_required`），然后调`policy.check_start`：策略启用、门槛关（曜日不设）、带selector的任务选择、679B10日开放（含300秒跨日宽限）、SpecialMissionPlayCondition与模式覆写。Type0同样检查Story任务窗口、任务最低等级与KeyQuestID；Type18另经帝国Tag801入口门，Type16与9032另经CfgMissionHistoryTable公开条件，见`docs/battle-modes.md`。入场通过后在同一阶段判定所选任务的事件任务奖励族，未结算时回501`quest_event_reward_unsupported`，先于券与魅力扣除，preview给出同一结果；当前offered关卡都不属于这类族。存档GameParam校验先于入场，损坏的存档先报503。

资格沿用6791D0：DisableEntrust为0（否则409`entrust_disabled`），关卡记录存在且MaxRank≥3、Treasure1–5每个非零槽位的收集位齐全（否则409`entrust_not_eligible`）；同一步还检查SpecialMissionPlayCondition（否则409`entrust_special_condition`，与开战共用判定）。不查Rank≥50（只属★4），也不查通关位0x01000000。当前offered关卡都没有额外条件。

RT非空时按`quest_modes.candidate_attributes`核对：槽位1–5看对应位，其余槽位看记录的AchieveFlag是否非0（65D900），再与候选special_attr按位或；核对排在入场之后。委任要求第一兵营有空位，单次魅力成本不能超过有效最大值（含当月扩展并先结算跨月状态）；批量总成本可以超过最大值，但必须已有足够实际余额，不能依赖本批升级后的恢复先垫付。当前offered关卡单次魅力5–150，行动力全为0。

奖励码取该关记录的原始MaxRank用`candidates_for`选出的候选，与开战相同：4807–4813的记录达到3星后多一个7000段奖励敌人，它在RT中占槽1的位置（TYPE为1，ATTR为2再或已取得位），发覆盖码437对应的单位，不置槽位位，也不参与收集判定；委任要求MaxRank≥3，所以这7关每轮都为437按`drop_basis_points`掷一次，但不保证掉落。只在3星以下出现的6000段候选不进入委任。目录的`describe`仍列出全部候选的静态奖励码。

每次委任对每个奖励码按`drop_basis_points`各抽一次，结果组成EntrustDraw：counts是各DropID的本批命中次数（TreasureInfo口径），units与items是另乘资源DropCount后的实际入账量（当前少数编码的DropCount为2/3/5）。经发奖前钩子调整、再加上战斗活动挂点的关卡产票后入账：单位按QuestTable.UnitLevel经`credit_units`以新GetIndex/UniqueID发放，收到多个单位可以暂超兵营玩法容量，但必须满足总行数、身份编号和原生响应容量；物品与票合并经一次`credit_capped`按堆叠封顶，超出部分丢弃并回IOF，不再整笔拒绝。挂点按钩子调整后的counts汇总DropType5点数，只在有点数时把EventParam改写为min(原值+点数, 1800)并在PGP带AF。经验与金币分别由`QuestService.experience_reward`与`gold_reward`算单次值再乘次数：经验先按experience_percent取整，ExperienceUpCloseDateTime晚于JST当前时刻且`EntrustRules.experience_up`为真（默认）时再按单精度×1.2f；金币与结算同一公式（`quest_economy.quest_gold`），每轮按★3倍率、不计GoldGet，能力倍率取全部兵营持有单位中InfluenceType 78的最大值（单位能力按4D5110取其图像替代卡的能力），MoneyUpCloseDateTime晚于JST当前时刻时×1.5（跟随结算，无单独开关），各倍率按整数有理数相乘、最后只取整一次。金币经`credit_quest`以99,999,999封顶，超出部分丢弃，余额已高于上限时保持原值，不再因金币拒绝整批；REWARD.GOLD回实际入账额。经验或金币的批量合计超过int32时同样不回400：经验最多记到末级，金币按99,999,999封顶，与结算一致。整批入账后统一处理升级与恢复，升阶邮件仍由RankRewardService处理，等级10的固定单位及自选券邮件使用原有永久领奖证明。

委任不发首通、首次三星或收集完成奖励，不修改关卡记录、quest_state、剧情位和教程位。每次各记录一次quest_clear，载荷为quest_id、真实quest_type、stars 3、hard false、mission_id（M*）与score_mode，不带team与kills，所以队伍条件不成立；不替用户领取任务奖励。真实类型使10002（如101）、10009（4125）与帝国PT3 642–645（如7317）可以经委任推进，载荷写死类型0时它们都不推进；三次批量时日常任务1605/1606/1607分别累计到1/2/3，已有进度不回算。

回复同步权威PGP、PBP.UX、本次新增PPU、REWARD、TREASURE（显式空表，客户端不读）与TreasureInfo；掉落、关卡产票、结算钩子的item_ids与实扣的券合并为一个POSSESSION，回绝对数量及Enabled；溢出回IOF，等级邮件回新增PPT，模式子项先放发奖前钩子的（含DropBonus的BONUS）、再放结算钩子的。76没有PFC接收器，不发首次三星结晶，也不伪造该节点。34、41、51、129、137及任务、等级奖励回复在提交前预检。

## 模式钩子

委任不开战，模式通过两段钩子参与，两段拿到的都是state的深拷贝：

- 发奖前`ModePolicy.entrust_rewards(EntrustContext, EntrustDraw)`返回`EntrustRewards(draw, children)`。默认实现套用该关DropBonus：按委任时刻的JST星期，对本批items的合计调用与开战结算相同的纯函数`drop_bonus.bonus_grant`，每次请求只取整一次（如9个变13个，而不是3次各取整得12个），加成后的合计截到2147483647（与83相同），counts不乘；没有增量时原样返回同一个draw；当日倍率为正时附BONUS(ItemType, Value)子项（当前为1与1.5），本批没有掉落该物品时也附；覆写须先调super()。返回值要求：counts的键属于按记录MaxRank选出的奖励码集合、值为非负int32，为0的DropID不进TreasureInfo；units每项正好含card_id、level与count，count为正；items为已知物品且数量为正；children为Container。否则503`quest_mode_hook_invalid`。关卡产票与EventParam不属于钩子，由底座的战斗活动挂点在钩子之后计算，票按钩子items的同一口径校验，未知物品回503`battle_event_grant_invalid`。TreasureInfo、POSSESSION与IOF都按调整后的结果只算一次。当前带DropBonus的委任关是2003个ヒストリー关与19个复刻关（trade_item_002000，JST周一/三/六×1.5）；实测周三4125委任3次，trade_item_002000从30变为45并回BONUS，TreasureInfo的次数不变。整表不可用时不加成，只在状态中报出，委任回执结构不变。
- 发奖后`ModePolicy.entrust_settle(EntrustContext)`返回SettleOutcome，默认原样返回。按开战结算钩子的标准校验，item_ids须为已知物品；另要求返回state的quest_state与传入时深相等（含mode_progress），presents为空，否则503`quest_mode_hook_invalid`。

对齐声明：策略在类上覆写check_start、start_boost、prepare_start、score_result、settle中的任何一个，都必须写进类属性`entrust_parity`（frozenset），否则`entrust_blocker()`返回“Mode <name> overrides <hooks> without a declared entrust counterpart”，该Type为pending（501）。声明的名必须确实被覆写，registry构建时校验，否则503`quest_mode_registry_invalid`；只认类级覆写，实例属性不算。委任传request=None，读取request的check_start覆写不能声明check_start，于是该Type只会pending，不会在运行时出错。守护测试要求审定集中已启用策略的blocker为None。当前Empire、History与Reproduce声明{check_start}（帝国入口门与CfgMissionHistoryTable公开条件都不读request）；Subjugation、Devil、Harlem、Raid、Tower、Goddess有blocker，但不在审定集。以后的倍率类奖励必须经entrust_rewards接入，并与结算共用纯函数。

## 降级与启动

资源问题按最小范围失败即关。整目录503`entrust_catalog_invalid`只保留结构性校验：QuestTable委任五列缺失、类型不符、未对齐或ID重复；possessions未就绪；掉落表不一致；SpecialMissionPlayCondition不符（与开战共用）。这些情形会让带entrust_history的存档无法启动：构建服务时委任为空，initialize报503`entrust_unavailable`，进程返回1，与其他带历史的服务同一约定。

| 来源与故障 | 影响范围 | 码 |
|---|---|---|
| 命令表读不到或列不符 | 视同无表，status报告 | — |
| `HasEntrustByType_t`歧义 | Type t pending | 503 `entrust_source_invalid` |
| `DoReturn_Type_c`或`IsEntrust_c`歧义 | 分类c的任务，请求时 | 503 `entrust_source_invalid` |
| 全局登记或Tag解析失败 | Tag门分类的任务，请求时；字面真分类照常；魔神、神獣、帝国开战回原503 | 503 `entrust_source_invalid` |
| 某Type任务窗口失败 | Type pending | 503 `entrust_source_invalid` |
| 某关券或禁用字段非法 | 该关 | 503 `entrust_source_invalid` |
| 成长活动表整表不可验证 | 需要判定免券的所有带券关 | 503 `entrust_source_invalid` |
| 成长活动某条目不可验证 | 所属任务的带券关 | 503 `entrust_source_invalid` |
| DropBonusConfig整表不可用 | 全部关按无加成委任，不回BONUS，`QuestCatalog.drop_bonus_status()`报error | — |
| 关卡产票提供者未挂接或构造失败 | 全部关不产票，readiness.main_quests.event_rewards.vote_tickets报原因 | — |
| 关卡定义、地图、奖励或该关DropBonus行构建失败 | 该关，按下节缓存或重试 | 原码 |

## 按需验证与内容版本

启动时构建并纳入内容版本的静态部分：范围、命令门、菜单路由与门、Tag索引、成长活动表、QuestTable委任字段、offered Type的任务窗口与成员、静态排除，以及各Type的范围判定与策略状态快照；静态部分的失败冻结到重启。关卡定义、地图与奖励在首次`product()`时构建：成功缓存；409/501失败作为确定性结果缓存；503不缓存，下次重试，与QuestCatalog.get一致。构建只在单关期间持目录锁，锁序固定为先目录、后QuestCatalog；status、availability与就绪检查都在锁内取快照。`validate()`逐关构建，全量约10秒，补全按需验证之前不完整的排除列表。关卡定义由resource_version与master快照固定，不逐关纳入内容版本。

EntrustCatalog的内容版本为schema 4，与时间无关；EntrustService的内容policy为4，并纳入EntrustRules、次数范围、检查顺序、金币、物品与任务载荷口径，以及战斗活动挂点与DropBonus表的内容版本（status的reward_sources）。委任金币改用公开倍率之前提交的回执按policy 3、不含金币口径的摘要核对，照常逐字节重放。这一重放以有效资源版本不变为前提：108常开关窗改变了有效资源版本，升级前提交、升级后才重发的委任请求所带Rev与当前版本不符，在查回执之前就回409`entrust_resource_mismatch`，不重放；升级前先等挂起的委任请求完成。每个CAS轮次只做一次目录校验与一次内容摘要，构造服务时同样校验目录。任何一项变化都会让旧请求的精确重试返回409`entrust_retry_stale`，内容policy升级前的回执也是如此；新请求正常，旧回执保留，回执校验仍然通过。检查顺序中原watermark阶段改名processed_request不算变化：服务另按旧阶段名算出`legacy_content_version`，改名前写下的回执在修订版与其余依赖不变时仍逐字节重放。任务的可选依赖（服装取得、名声登记、EP积分；EP积分只在任务目录含EP条目时进入摘要）在回执提交之后才接入时，回执记录的任务内容版本按任务服务给出的可达版本集合核对（新旧阶段名都算），旧回执照常逐字节重放；服务端只在QuestService与RankRewardService都可用时构建委任，有QuestService时任务总带关卡通关规格，RankRewardService也只在道具券服务可用时构建，所以关卡通关规格与道具券不会后接入。任务服务的可达版本还包括名声目录与礼物箱道具定义的嵌套变体，覆盖妖精乡、可写的银之馆或妖精乡的额外兵营容量后接入，以及称号目录后接入；任务与通行证窗口按JST时刻比较时的旧窗口变体同样在内。116的版本按`RankRewardService.replayable_content_versions()`核对，覆盖称号目录后接入。战斗活动挂点的版本按投票关卡券提供者的缺席原因核对：提交时提供者未挂接（`vote_quest_tickets_not_attached`）、依赖不可用（`vote_quest_ticket_dependencies_unavailable`）、自身构造失败（`vote_quest_ticket_source_invalid`、`vote_quest_ticket_definition_invalid`、`vote_quest_ticket_dependencies_changed`），或构造期原样传出投票目录与过期服务的就绪码（`vote_catalog_changed`、`vote_candidates_unresolved`、`vote_expiry_dependencies_changed`），之后提供者挂接时旧回执照常重放。三者按任意组合核对。以下情况仍回409`entrust_retry_stale`：提供者因其他原因缺席时提交的回执（例如构造期传出礼物箱的就绪码）；任务服务或116服务本身前后有无变化；这些依赖被移除或版本变化；其余依赖变化。审定Type的策略状态或开战钩子覆写在运行中变化时，目录报503`entrust_catalog_changed`直到重启。

## 存档、重试和旧战斗

`entrust_history`使用共享RequestLedger（schema 2），保存最近16份回执（请求摘要、内容版本、状态摘要、提交后修订版、关卡、次数、回复及其摘要）和最近256个已处理请求摘要；客户端CT只作墙钟，不参与排序，时钟回拨或2038年后的负CT照常受理。旧schema 1（CT水位与同秒摘要）读取时自动升级，下次写入保存为新格式，旧回执照常逐字节重放；回执已被挤出但摘要仍在时回409`entrust_request_stale`；升级前已被挤出且不在同秒集合内的旧请求不再算已处理。不会改变QuestDefinition和旧active.definition格式。EntrustRules只放在配置文件里，平台码统计只在内存中。

相同修订版的精确重试不读时钟、不重新抽选、不重新扣费或计任务。CAS冲突后重新检查资格、余额和容量，沿用该请求首次抽选结果；完整候选通过后才一次保存。响应内容也保存摘要，损坏回执不静默重做交易。

已有普通战斗场次保持不变，潜在奖励预留继续保护编号及容量，委任后仍可正常结算旧场次。这是明确的本地并行策略，原生证据只证明UI串行。旧active缺少预留信息时沿用现有服务的明确拒绝，不删除已付费场次来绕过检查。

## 控制台接口

- `EntrustService.status()`（`/local/status`的readiness.entrust；`/health`只保留其中顶层的数值、布尔值、空值与不含空白的标识串）：ready、implemented_operations[76]、content_version、quantity[1,2,3]、local_rules（QuestRules、EntrustRules，以及mission_choice、free_ticket、check_order、day_grace_seconds 300、platform的固定描述）、platform_codes_seen{counts, last}、scope、battle_policy、catalog、history_limit（16）、processed_request_limit（256）、request_order、replay_policy（依赖后接入时的重放口径）、gold_policy、candidate_policy、drop_bonus_policy、event_reward_policy、event_mission_reward_policy、reward_sources（event_rewards与drop_bonus的内容版本）与native_tested=false。平台码统计所有解析成功的76请求，含精确重放与之后失败的请求；越界的码在计数前就被拒绝；只在内存中，重启清零。
- `EntrustCatalog.status()`：有界摘要。顶层为ready、error、schema、content_version、resource_version、audited_types、offered_types、pending_types{Type: {code, status, reason}}、unaudited_native_offers、refused_types（最多32项）与refused_type_count、commands、growth_campaign、mission_tags、sources和native_tested；types[t]含state、exclusion、quests（该Type关卡数）、native_gate、policy（策略status加entrust_blocker）、windows_error、categories（路由、作用域、菜单门与任务数）、indexed、validated、all_validated、excluded、transient_failures、disabled与ticket。
- `EntrustCatalog.ticket_item_ids`：已登记委任关（offered且有任务）所列委任券物品的集合（frozenset，当前只有item_consignment_ticket），控制台据此判断只能经邮件补充的物品。
- `EntrustCatalog.availability(now=, platform_code=, tags=None)`：与玩家无关的即时可委任计数，按Type给出indexed、open、free与阻断直方图（not_registered、mission_window、entrust_menu_closed、day_closed、mode_entry_closed、source_invalid，取各任务走得最远的阶段，每种最多列32关）；mode_entry_closed是策略入口门（`ModePolicy.entry_open`，如帝国的Tag801菜单入口）关闭，入场在check_start里以同一判定回409。传入`QuestCatalog.preview_mission_tags(overrides)`可以预览窗口修改的影响。它不反映事件任务奖励守卫、按需构建失败和与玩家相关的门。
- `EntrustCatalog.describe(quest_id, now=None, platform_code=None)`：单关静态信息（Type状态与拒绝码、成员任务的Enable原值、窗口、Tag、路由与菜单门、券、禁用、全部候选的静态奖励码与conditional），以及可选的即时状态（是否登记、是否OPEN、菜单门结果、Tag归属、当前是否免券及不可验证原因，另有该关的日开放`day_open`与策略入口门`entry_open`）。
- `EntrustCatalog.validate(quest_types=None)`：由控制台显式触发全量构建，逐关持锁，76至多等一关的构建；返回各Type的产品数与排除数。控制台的入口是校验作业`POST /admin/api/v1/entrust/validations`（GET查询进度与结果）：后台单作业，逐Type报告done/total，世代切换后旧作业标stale、只能重新启动。
- `EntrustService.preview(profile, quest_id, quantity=1, now=None, platform_code=)`：针对`store.profile()`的dry-run，按76的同一检查链返回ok、首个失败的code、message、status与stage、归属任务M*、券（item、per_run、free、cost）、魅力（单次与总计）及逐阶段checks。不鉴权、不抽签、不写存档，只检查到扣魅力为止。
- `QuestCatalog.mission_tags().status(now, platform_code, consumers)`与`explain(kind, value, now=, platform_code=)`：展示Tag解析链、被遮蔽的任务、override的known（任务在登记表中）与effective（客户端会建出且某平台Enable非零，修改才会影响门），以及反向依赖，例如900001←Tag302←CHistoryQuestMenu链上的IsEntrust_14←ヒストリー委任2016关与Type8的9032；500090←Tag150←魔神菜单；940012←Tag801←帝国门户横幅与PageEmpire页签←全部Type18开战与委任。
- `NativeCommandIndex.status()`：表数、行数、missing与invalid及原因、重复_ID、content_version；`quest_menu(c)`给出各分类的菜单路由。
- `QuestCatalog.preview_mission_tags(overrides)`与`GrowthCampaign.preview(overrides)`只做影响预览，不改服务状态。例如关闭900001会使ヒストリー委任从2016关降到0，Type8的9032也关闭，Type18不受影响。

编辑约束见`docs/admin-console-plan.md`的“记录与恢复”。

## 本地规则

EntrustRules通过start-server.ps1的`-EntrustRulesFile`或CLI的`--entrust-rules`加载，示例`config/entrust-rules.example.json`为`{"experience_up": true}`，未知键、重复键、NaN/Infinity或溢出成无穷的数字、嵌套超过64层均拒绝。

| 规则 | 默认值 |
|---|---|
| 委任范围 | 审定集{0,2,8,16,18}中原生类型门为字面真、策略启用且没有未声明开战钩子覆写的Type；当前0/2/8/16/18（用户决定） |
| 归属任务与免券 | 任一可用任务免券即免，归属取MissionID最小的免券任务，否则取MissionID最小者；无券关不查成长活动表（用户决定） |
| Type0入场 | 与其他类型同走任务入场：Story窗口、任务最低等级、KeyQuestID（用户决定） |
| 经验加成 | `experience_up`默认true，跟随结算的ExperienceUpCloseDateTime×1.2f（用户决定） |
| 金币 | 与结算同一公式（见`docs/battle-modes.md`结算一节）：每轮按★3倍率，不计GG，能力倍率按持有单位中InfluenceType 78的最大值（含额外兵营；单位能力按4D5110取其图像替代卡的能力），MoneyUp跟随结算的MoneyUpCloseDateTime，无单独开关（用户决定：委任跟随结算） |
| 金币入账 | 以99,999,999封顶，超出部分丢弃，与结算一致 |
| 候选 | 按关卡记录的原始MaxRank选取，与开战一致 |
| DropBonus | 默认`entrust_rewards`按委任时刻的JST星期对整次请求汇总后取整一次（用户决定） |
| 关卡产票与EventParam | 钩子之后由战斗活动挂点计算：按委任执行时刻选发票组，窗口开放时每轮floor(魅力/15)张票（见`docs/vote-events.md`），EventParam只在有DropType5时改写，封顶1800 |
| 物品溢出 | 按堆叠封顶并回IOF，与结算一致（用户决定） |
| 错误码 | 非提供类型409`entrust_not_offered`，未知关卡404`quest_not_found`，PlatformCode越界400`quest_platform_invalid`，资源降级503`entrust_source_invalid`（用户决定） |
| 命令表 | 读全部106张登记表，缺表视同无表，键歧义只挂起依赖的范围（用户决定） |
| 目录验证 | 按需验证，另由控制台调用validate()（用户决定） |
| 菜单门Tag | 按通用Tag解析，不写死900001，不加宽限 |
| 日开放跨日宽限 | 300秒，沿用开战；周一00:00–00:04:59之间28个曜日关都可委任 |
| 无成员关卡 | 静态排除409`entrust_no_mission` |
| 掉率、经验、金币倍率 | 沿用QuestRules |
| 次数 | Boost.V为1–3，省略时为1 |
| 请求顺序 | 按完整请求摘要去重，客户端CT只作墙钟，时钟回拨照常受理 |
| 事件任务奖励 | 未结算的族在admission回501，与开战相同（用户决定） |
| 旧战斗 | 与已付费active并行，保留其奖励预留 |

## 未验证项与证据

- 委任入口（已静态闭合）：QuestSelect_MainPanel的更新器6A6C10取子控件WD03，分类≠0时调67FD00隐藏、分类0显示并按6791D0上色，分类2两者都不处理；106张命令表没有任何一行把`MnWin01::WD03`设为可见，779C00只切换启用位与颜色、不改可见性，所以曜日（5）、复刻（3）、History（14）、Empire（16）列表的WD03始终隐藏，委任经列表格子直选进入：7821A0在已通关格子上显示QENT（6791D0为真）或QCLE，点击走DoQuestDirectSelect→7832C0，菜单门（776AF0）与资格门（6791D0）同样生效，服务端准入不需要区分入口。公开资料同向：官方aigis1000.jp id 8422（2021-04-01生效）写曜日、緊急、復刻、デイリー復刻可委任，大討伐、魔神、大総力戦、強襲、チャレンジ、戦術指南、交流、英傑の塔不可委任；wikiwiki写曜日与イベントヒストリー用委任券委任、帝国大戦委任按持有帝国单位最大数计，与可委任集合{0,2,8,16,18}一致。
- 平台码（已静态闭合）：本地客户端恒发PlatformCode 9——AppConfigDmm::init（A9D020）写+4="Platform_GamePlayerExe_R18"、+8=9，游戏初始化66EC00调vt+4完成写入，6246B0把该值写进每个注册请求；任务Enable掩码判定8CEEA0读同一值。当前资源的掩码值（8191、2047、0）对平台1与9给出相同的启用集合；status的平台码统计可核对实际请求。
- 客户端时区：AACF90按本机时区解释当日09:00，UTC+10及以东的客户端会早一天，本地按JST；AAC970读客户端本机时钟，服务端用自己的时钟代替。
- DropBonus对本批items只乘一次，Boost>1时取整可能与逐次计算不同，原生行为未核实。成长活动期间的券掉落未接，其调用方46DB00/46F340未追。
- 免券偏好可能比客户端77A620按当前任务判定少收，不会多收；当前每关最多一个任务，不会触发。
- 入口门与菜单门在请求时求值，原生在显示菜单时求值、出发时不再检查，所以窗口在玩家进入列表之后才关闭时，服务端比原生更严。
- 菜单作用域链只映射了General与History两条；资源若把审定分类改路由到其他菜单，委任失败即关，需要补链。命令表跨表重复的先后规则未复刻，出现时需要人工确认后放行；服务端资源缺表而客户端有表时，服务端比客户端更严。
- preview只检查到扣魅力为止，之后的失败（单位身份耗尽、结算钩子、任务记录、预检与回包超限）预测不到。
- 对齐清单为check_start、start_boost、prepare_start、score_result、settle五个开战钩子：模式在类上覆写其中任何一个都必须写进`entrust_parity`声明，否则该Type pending；声明的名必须确实被覆写，registry构建时校验。目录另按原先的三钩子清单（check_start、prepare_start、settle）算出`legacy_content_version`，`replayable_content_versions()`同时给出两者，清单扩充之前提交的回执照常重放。
- 官服委任是否产票、是否改写EventParam、是否回BONUS，以及437覆盖奖励在委任中的掉率与TreasureInfo，都未经实机确认；本地按开战同一挂点与候选规则执行。

原生依据：`aigis_research/battle-modes-20261102/entrust-scope`的contract、verify-1、verify-2与history-gate，以及`aigis_research/battle-results-20261101/entrust-contract.md`和`entrust-resources.md/json`。定向测试覆盖命令表、成长活动、任务登记、开战入场与奖励、魔神、帝国、复刻、委任目录、委任事务、战斗活动挂点、原生编码HTTP与CLI，使用真实公共主表与资源；未运行无关全量测试。
