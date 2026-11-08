# 塔

塔Type 14的门户查询66、67，塔结晶领取68，以及各层的开战与结算已接入。三座塔为英傑110001（10001–10045）、統帥110002（11001–11045）与悪霊110003（12001–12033），共用挑战物品`tower_consume_item_110001`（Stack 30），塔结晶为`tower_crystal_110001/110002/110003`（EffectType 6，Stack 999999），也是塔兑换65/69的货币，见`docs/tower-exchange.md`。客户端不计分，只显示服务端下发的TowerResult；计分、生成、补发与奖励都是服务端规则，未经实机验收，未启动游戏或服务。

## 入场与扣费

开战须同时满足：1004已通关（塔门户只要求1004：OnCommandMissionTypeTower行1，IsPlayableTowerMission只查任务窗口与日期，不查Rank；123关全为Rank 70，那是77A690按QuestTable第7列拦的逐层关卡门，服务端已有同一校验，入口不另加Rank门）；所属塔已揭示（DiscloseQuestID为0或该关已通关，7F0020；統帥与悪霊要求10001）；该层的AppearCondition、关卡Rank与塔任务窗口（三座塔Enable 8191、Start=End=0，常驻）；兵营有空位；挑战物品“持有+待补发”至少1个。缺少塔服务或礼物箱服务回409`quest_tower_unavailable`，塔服务读的不是同一份关卡目录回503`quest_tower_catalog_mismatch`，未揭示回409`quest_locked`，挑战物品不足回409`quest_tower_consume_shortage`；78/82回`quest_hard_unavailable`。Type 14不在委任审定集。

开战成功时先惰性结算并冻结1个该塔ConsumeItemID的成本（7FF4C0只在持有为正时出发；官方id 9370：挑战权只在通关时消耗），不扣减，开战响应回POSSESSION当前未扣数（含同次补发的变化，列为ItemID/Count/Enabled，缺省行为0）。塔层83成功结算时扣掉冻结的1个挑战物品，同次补发改变的挑战物品与产票经83的POSSESSION回报，同样带Enabled；已缓存的两列开战与结算回复按存档字节原样重放。开战与结算都回显式空的TREASURE（客户端不读，见`docs/battle-modes.md`）。战败、撤退与ギブアップ（ntwh57oy）、リトライ都不扣，リトライ多少次都只在通关时扣一次；旧规则下开战时已扣的active结算不再扣、退出不退；精确重放沿用开战缓存，不重复扣。层的validate_row要求Type 14、UnitList 0、Treasure1–5为0、恰好属于一个启用的塔任务，且共享计分表与该塔的分数奖励表都有本层，不符回503`quest_definition_invalid`。

## 计分与结果

参数读共享表`TowerScoreCalcConfig.atb`（4641E0），不读各塔的副本。每项为`point = clamp(F1 − F2·x, Min, Max>0 ? Max : ∞)`，F1或F2不为正时该项为1；`Score = floor(StarCountPoint[星] × 费用项 × 人数项 × 出击项)`，MinScore、MaxScore大于0时再夹取。float32单元格按能还原同一float32的最短十进制精确计算，所以2.5−0.1×5恰为2；Support系数是十进制文本，按原文计算。

- ConsumeCost为全部Sally记录的SallyCost之和。
- MemberCount为BL中A8≥0、GetIndex<UX+1000的不同单位（默认不含王子：wikiwiki与seesaawiki的182500分算例都写王子不计编成人数、照计费用，`member_count_prince`可开回计入），加上AL中不同单位数×MemberCountFactorSupport。
- SallyCount为非令牌的Sally记录数，访客记录按SallyCountFactorSupport计。
- 支援单位在编成与出击两项都按系数换算，依据为SystemText 6273。

当前StarCountPoint为2500/5000/10000；英傑计费用（7.5−0.02x，下限1）与人数（2.5−0.1x），不计出击；統帥计人数（2.5−0.1x）与出击（7.5−0.45x，下限1），不计费用；悪霊计人数（2.5−0.05x）与出击（5−0.085x，下限1）；三座塔的MemberCountFactorSupport都是0.25，統帥与悪霊的SallyCountFactorSupport也是0.25。例：英傑10001★3、王子加4名单位、没有出击记录时，王子不计人数，人数项2.5−0.1×4=2.1，费用项7.5，出击项1，分数为floor(10000×7.5×2.1)=157500。

层最好分取历史最大值，塔的TotalScore为本塔各层最好分之和，各层可记录的最高分之和不超过int32。TowerResult按列名有九列（A68FE0）：ConsumeCost、StarScore、Score、TotalScore为I，MemberCount、ConsumeCostScore、MemberCountScore、SallyCount、SallyCountScore为F；Score是本场分数，TotalScore是结算后的本塔合计。每次结算顺序为：惰性结算已过的天数、计分、更新层最好分与合计（严格刷新最好分时同时保存该层HighScoreTeam快照）、发放到期档位，因此本场分数只在当日生成计入之后才改变TotalScore。

## 生成与补发

两个日键都是`business_calendar.civil_day(时刻 − 规则小时)`，即按JST小时换日的原生日序号（规则小时为4时等于营业日`business_day`）：生成与补发日键都默认4点（`generation_hour`、`replenish_hour`；官方id 6956写塔结晶每天4点自动生成，id 10938写挑战权每日4:00自动付与3回分。客户端A40090的JST零点排程只是本地推送通知的排程，与服务端生成时刻无关）。66、67、68、塔开战与塔结算之前惰性结算，读类请求也会写存档，三次CAS冲突回409`profile_changed`；控制台在运行中切换两个小时字段前，也会按旧小时结算一次已有塔进度的存档。时钟回拨不回退已结算的天数；时刻减去规则小时后落在客户端日历（JST 1970-01-01至3001-01-19）之外时回503`tower_clock_invalid`。

- 塔结晶：只对已过1004、已揭示、窗口开放的塔生成。每过一天向StackCrystal加当前TotalScore档（Score≤TotalScore的最高行，首行Score为−1）的GenerationCount，封顶该档PossessionLimit；已超出当档上限的旧库存保留，合计下降后由更高档留下的库存也保留，合法上界为当前档及更高档PossessionLimit的最大值。生成表各53行，从Score −1（每天250、上限1500）到7,800,000（每天770、上限4620）。
- 挑战物品：每过一天补发共用该物品的已开放塔DailyAutoAddCount之和，单次补发先截断到Stack（30，与官方id 10938「毎日4:00に自動で3回分付与され、合計30回分まで保持可能」一致；wikiwiki与seesaawiki的「最大20回」是悪霊の塔实装前的旧资料）再经`credit_capped`入账；当前只有英傑为3，即每天+3。日增量之和为0的物品不建立日键。
- 首访：某座塔第一次满足生成条件时建立记录，两个日键都设为前一天，所以首次访问即结算一天（250塔结晶、+3挑战物品）；`first_access_settles_day`为false时不补这一天。首次访问时的库存无公开资料，首访结算一天是用户决定（旧资料「开放即20次」与之冲突，不采纳）。

## 领取68

68接收MISSION（1到塔数个MissionID，不得重复，须是已配置的塔，否则400`tower_request_invalid`）。对每座塔发min(StackCrystal, Stack−持有−本次已发)，多座塔用同一种结晶时共享剩余空间，余量留在StackCrystal；还没有记录的塔领到0。回复为SYSTEM、POSSESSION（所领塔的结晶，加上同次补发改变的挑战物品，带Enabled列）与所领塔的StackCrystal；回复超过64 KiB回503`tower_response_capacity`。

68使用共享请求账本：请求摘要、提交后的修订版、内容版本与回复一同保存，保留16份回执与最近256个已处理请求摘要，客户端CT只是墙钟。同修订版、同内容版本的重发原样回放（RewardType 3开始发放之前写下的回执按`status().legacy_content_version`同样回放）；修订版或内容版本变了回409`tower_retry_stale`（TowerRules变化也会改变内容版本；只有按旧默认值`generation_hour` 0、`member_count_prince` true写下的回执，在规则其余字段不变时照常回放）；回执已淘汰但摘要仍在回409`tower_request_stale`。没有领到也没有结算变化时直接回复，不写回执，重发时重新计算，不会重复发放。66、67的惰性结算本身幂等，在同一次CAS中重算；83沿用战斗响应缓存。

## 查询66、67

66接收QUEST：

- QUEST=0（7EBE40进门户时对每座塔各发一次）：回SYSTEM、PersonalRecord 0/0、全部塔的StackCrystal，以及挑战物品与全部塔结晶的POSSESSION（带Enabled列）。
- QUEST为某层：PersonalRecord回本层最好分与本塔合计（A69590）；`high_score`为self且本层最好分大于0时，有快照则先附该层的HighScoreTeam，再附HighScorePlayer（Score为本层最好分、Exp为玩家经验、Title与Blessing为0）；再回本塔的StackCrystal与该塔物品的POSSESSION（带Enabled列）。
- 其他值回400`tower_request_invalid`。

`high_score`为omit时HighScoreTeam与HighScorePlayer都不发；8004D0在Score≤0时不显示面板，所以这也是原生定义过的状态。HighScoreTeam回该层最好分那次结算保存的快照：只有严格高于原最好分的83才保存或替换，同分或更低不改；取该次BL中A8≥0、GetIndex/UniqueID/CardID都与存档一致的真实单位（替身与AL不计），排除王子（CardID 1、309：A54330会自己补王子行，服务端放了会显示成普通单位），按BL的A8升序、UniqueID去重，最多15行；各列取存档该单位当时的A1–B1（A8为存档的OrganizationIndex；A66E60按列名读A1–B0，B1有列才读，B2–B5不发）。只有王子出战时快照为空，66回零行HighScoreTeam，显示与省略相同。没有快照（控制台手设的最好分，或保存快照之前就有的最好分）时省略HighScoreTeam，王子格照常显示。HighScoreTeam排在HighScorePlayer之前，与A53FE0登记顺序一致。116继续不发TowerHighScore，表空时70A990直接走OnNotifiedHighScorePrize。67不带业务字段，惰性结算后只回SYSTEM。

## 奖励

- 分数档：`TowerScoreRewardConfig{MissionID}`中非0 QuestID开组，其后QuestID为0的行并入该组（95B3C0）；每座塔45组、每组10档，本塔以外楼层的组保留在表中但永远不会达成、发放或进入deferred（悪霊只有33层可玩，12034–12045的组不用）。说明文字“階層スコア報酬”（SystemText 4600）。可玩层分数档里的塔结晶合计为49500/51500/37100。
- 总星档：`TowerTotalStarRewardConfig{MissionID}`按本塔各层MaxRank之和（8CD430）判定，每座塔135档。说明文字“塔合計★数報酬”。
- RewardType1发GeneralItemID物品；RewardType2发单位，取该CardID的PresentID>0中最小的PresentUnitData模板（当前29张卡都与初始模板一致），没有模板时记`tower_reward_template_unknown`；RewardType 3是神聖結晶（9590D0固定取systxt_0031），按Count作免费神聖結晶发进礼物箱（礼物Type 0，说明文字随档位来源），与1、2类走同一套跨档判定与deferred重试；该行须Count>0、CardID为0、GeneralItemID为空，否则目录整体回503`tower_catalog_invalid`。RewardType 3每塔36档，都在非5倍数层的RewardID 10、90000分处，悪霊有27档在可玩层。RewardType 1–3以外的类型记为`tower_reward_type_unknown`（当前资源没有）。
- 跨越的档入礼物箱，经战斗礼物队列投递，礼物箱满时排队补投；已领按档记录。全部楼层9万分、总星满时，三座塔可发放581/583/428档（其中神聖結晶36/36/27档）。

可投递性在每次塔结算、deferred重试与控制台重投时经`present_backlog.require_deliverable`实时判定：先按战斗礼物队列的有界形式（`require_bounded`，说明文字与物品ID不超过255字节）检查，不符记`quest_present_unsupported`，再用`presents.preview`对空礼物箱判定；不使用构造期结果，所以塔服务与召唤券服务的构造顺序不影响结果。不能投递的档不发、不记已领，连同原因码记入deferred；之后任意一座塔的结算都会对每座已开始的塔先重试其deferred，再判定该塔新跨越的档；控制台的`redeliver_deferred`仍按单塔执行。当前总星档中的召唤券（英傑5档item_smn_ticket_18003、10档18002、50与100档18001，統帥与悪霊的50与100档18001）属池31/32/33，这些池不完整、未启用，记为`summon_ticket_pool_disabled`（英傑[5,10,50,100]、統帥[50,100]、悪霊[50]，悪霊最多99星），启用池后的下一次塔结算补发。

## 存档与迁移

唯一新增的存档域`tower_state`（schema 2）由服务端权威保存，客户端不存这些数据：

- `consume`：挑战物品ID→{day}，补发日键；
- `towers`：MissionID→{floors（层→最好分，只收本塔正分）、total（须等于floors之和）、stack（不超过当前档及更高档的PossessionLimit最大值）、generation_day、score_rewards（层→已领RewardID）、star_rewards（已领星数档）、deferred（{source, key, reason}，不得与已领重复）、teams（层→HighScoreTeam快照：只收已有最好分的本塔楼层，每层至多15行，每行是A1–B1对应的17个单位字段，不含王子，UniqueID不重复）}；
- `receipts`：68的请求账本。

读取时严格校验，不符回503`tower_state_invalid`；quest_records损坏（QuestID、MaxRank、AchieveFlag非法、重复或行数超过65535）回503`tower_records_invalid`。旧存档没有该域时视为未开始，读取时不建域、不补发；某座塔第一次满足条件时，在66/67/68或塔开战中建立该塔记录。Type 14此前开战即回501，旧存档不可能有塔进度，不需要回填。塔服务不可用而存档已有`tower_state`时，`ServerServices.initialize`回503`tower_service_unavailable`，与trade、tower_trade等带历史的服务一致；启动时还会逐份校验68回执的回复。schema 1的旧`tower_state`读取时视为各塔teams为空，只读不改写，下一次写入时存为schema 2；旧存档deferred中原因为`tower_reward_type_unknown`的RewardType 3档，会在下一次任意塔结算或控制台`redeliver_deferred`时按神聖結晶补发。

## 装配与规则

`TowerCatalog`只读资源并校验：塔任务与成员层、共享计分行、分数档续行、总星档、生成表、物品Stack与EffectType（须与持有物服务读同一份ItemConfigTable）、各层最高分之和不超过int32；不符回503`tower_catalog_invalid`。`TowerService`设为`quests.tower`，装配后66/67/68才会路由；依赖缺失时不装配，readiness.towers为{ready:false, error}。任务窗口核对不一致时塔服务也不装配，见`docs/mission-windows.md`。

内部一致性故障回503：`tower_dependencies_invalid`、`tower_rules_invalid`、`tower_deliverable_invalid`、`tower_holdings_invalid`；计分输入非法回400`tower_score_invalid`，存档分值非法回503同码；`tower_reward_type_unknown`与`tower_reward_template_unknown`为501，只作为deferred的原因出现。

`TowerRules`经`--tower-rules`或start-server.ps1的`-TowerRulesFile`加载，示例`config/tower-rules.example.json`等于默认值，未知键拒绝，16 KiB以内，按严格JSON读取（重复键、NaN/Infinity或溢出成无穷的数字、嵌套超过64层都拒绝：命令行加载时服务报错退出，经配置注册表加载时报`config_rules_invalid`，嵌套过深报`config_nesting_too_deep`）。命令行文件修改需要重启；经控制台配置切换修改时，`generation_hour`、`replenish_hour`属time_accumulating，切换前由运行中的世代按旧规则结算塔时钟（gated档，只能运行中apply，不能暂存），其余字段不需要结算，单独修改时一般属hot档：

| 字段 | 默认值 |
|---|---|
| `generation_hour` | 4（官方id 6956：塔结晶每天4点生成；A40090的零点排程只是本地推送） |
| `replenish_hour` | 4（官方id 10938：挑战权每日4:00付与） |
| `first_access_settles_day` | true，首访即结算一天（用户决定） |
| `high_score` | self，回本人本层最好分；可切omit（用户决定） |
| `member_count_prince` | false，王子不计人数（wikiwiki与seesaawiki算例） |

配置注册表把两个小时字段记为随时间累积、首访字段记为首次基线，其余字段每次操作冻结：中途修改会改变尚未结算的日数或新塔的起点，不回算已结算的天数。经控制台在运行中切换两个小时字段时，先按旧小时结算再换代，已过的日界既不重算也不漏算；从未进入过塔的存档不受影响。

## 控制台

只读：`TowerService.status()`给出目录摘要、生效的TowerRules与规则版本、已实现的66/67/68、本地口径、回执上限、内容版本与`legacy_content_version`；`describe(state, now)`给出每座塔的可进入与揭示状态、各层最好分、TotalScore、总星、StackCrystal、当前生成档与上限、两个日键及下次结算时间、待结算天数、已领档、deferred档及其实时原因、到期档、各层HighScoreTeam快照的CardID、挑战物品的持有与补发、68回执数及回执须匹配的内容版本；`support_status[14].local_rules`给出静态口径。

写操作是纯状态变换，由控制台按审计CAS提交：

- `set_floor_best(state, quest_id, score)`：设置或清除（0）层最好分，不超过该层可记录的最高分，重算合计；新分数达到的档记为已领、不补发；新分数与原最好分不同时清除该层HighScoreTeam快照；新合计对应的库存上界低于现有库存时回400`tower_admin_invalid`（先调低库存），塔还没有记录时回409`tower_not_started`。
- `adjust_stack(state, mission_id, stack)`：不超过当前生成档的上限。
- `settle_now(state, now)`：补齐已过的天数后再对开放中的塔强制结算一天，返回新存档、有补发的挑战物品与本次是否加天；没有开放的塔时不加天。
- `redeliver_deferred(state, mission_id)`：重判并直接入箱，礼物箱满时回409`present_capacity`且不改存档；启用券池31/32/33之后使用。
- `reset_rewards(state, mission_id, sources)`：清除分数档、总星档或两者的已领记录，返回被清的档，可能导致重复发奖。

五个写操作在控制台登记为battle域的`towers.floor_best.set`、`towers.stack.adjust`、`towers.settle_now`、`towers.deferred.redeliver`与`towers.rewards.reset`（reset要确认短语`RESET TOWER <mission_id>`并自动备份，重复发奖提示见警告），有进行中战斗时一律先拒绝；只读入口经战斗页的`towers.overview`视图展示。挑战物品与塔结晶的增减走PossessionService或礼物，不直接改`tower_state`；不得删除68回执来重放领取。TowerRules可经控制台配置切换修改（见`docs/admin-console.md`「换代分档」），命令行文件修改仍需重启。

## 本地规则与未完成项

| 规则 | 默认值 |
|---|---|
| 开战扣费 | 官方口径（id 9370）：开战只预检并冻结1个挑战物品，失败、放弃、重开不扣，83成功结算时扣一次 |
| 生成与补发 | JST 4点生成与补发（官方id 6956、10938），惰性结算 |
| 计分 | float32单元格按最短十进制精确计算，Support在人数与出击两项都按系数 |
| TowerResult.Score | 本场分数，TotalScore回结算后合计 |
| 单位奖励模板 | 该卡PresentID>0中最小的模板 |
| 不能投递的档 | 记deferred，任意塔结算时重试 |
| HighScoreTeam | 严格刷新最好分时保存快照，排除王子，最多15行（用户决定） |
| RewardType 3 | 免费神聖結晶（礼物Type 0）（用户决定） |
| TowerHighScore | 116不发（用户决定） |

已由公开资料定下、不再列为待确认：王子不计编成人数（wikiwiki、seesaawiki算例，`member_count_prince`默认false）；令牌不计分（官方id 10938「トークンはスコア計算に含まれません」，与SallyCount排除令牌一致）；结晶每天4点生成、挑战权每日4:00付与且上限30（官方id 6956、10938）；失败与放弃不消耗挑战权、只在通关时扣（官方id 9370）。以下仍只能靠实机或抓包确认：费用取SallyCost还是PlainCost、Support系数的取整、TowerResult.Score回本场还是最好分；首次访问时的库存；官方HighScoreTeam包含哪些单位与顺序（是否含AL访客、按编成序还是出击序、同分以谁为准）及行内A8的取值、HighScorePlayer是全服排名还是本人记录、116的TowerHighScore与NotifyHighScorePrize的触发方式、官服对QUEST=0的66如何应答、RewardType 3的神聖結晶在官服属免费还是付费、66/68中StackCrystal与POSSESSION的行集合；BonusType0/BonusNum0各层首次★3的结晶数（公开资料写塔首★3有结晶，但每层数量无资料，本地沿用`first_three_star_crystals`）。计分的算术口径也没有原生依据：与float32逐步计算或double计算相比，部分输入相差1分，落在整数档位线上时会改变档位。惰性结算会把窗口关闭期间的天数也计入，当前三座塔都常驻，暂不受影响。塔门户、TowerResult与HighScore面板仍待实机验收。

静态证据见`aigis_research/battle-modes-20261102/blocked-economy-missions`与`aigis_research/permanent-20261101/menu-tower-drop-contract.md`。定向测试为test_tower_catalog、test_tower_state、test_tower_service与test_quest_mode_tower，使用真实公共主表与资源；未运行全量测试。
