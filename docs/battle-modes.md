# 战斗模式底座

QuestService按QuestTable.Type选择模式策略，统一处理77/78/81/82开战、80/83结算及ntwh57oy退出。当前启用Type0主线、1挑战、2曜日、4战术指南、6大討伐、8复刻、9魔神降臨、10交流、11神獣、14塔、15練習、16ヒストリー、18帝国大戦与19女神の復活，其中0/9/18支持★4，6为计分模式；Type3/7/13没有可玩关卡，开战前回501。开战在任务准入之后、Boost与扣费之前判定事件任务奖励族：出现没有模式结算的族（计分阶梯、紧急任务阶梯或活动后配布）时回501`quest_event_reward_unsupported`且不扣费（当前只有Type6结算计分阶梯），见事件任务奖励一节。委任76覆盖审定类型中原生门开放且策略可用的Type，当前为0/2/8/16/18，见`docs/entrust.md`；塔见`docs/tower.md`，任务窗口复刻见`docs/mission-windows.md`。未重启服务或操作游戏窗口。

## 模式注册

`quest_modes.MODE_SPECS`记录每个Type的任务分类，并派生MissionConfig资源、计分模式（6/7/13）、是否计GoldGet（分类2/10/11/12/13不计）、★4等级门槛（Type0/9为50，Type3/18为5）及是否属于审定的委任范围（`ENTRUST_TYPES`={0,2,8,16,18}，运行时仍需原生门与策略）。每个`quest_mode_*.py`声明一个策略；注册表必须覆盖全部已知Type，停用策略必须写明原因。门槛指客户端菜单要求的通关关卡。

| Type | 策略 | 状态 | 委任 |
|---|---|---|---|
| 0 | story | 启用，支持★4 | 提供 |
| 1 | challenge | 启用；门槛1004 | 不提供 |
| 2 | daily | 启用；不设门槛关（门户横幅脚本的1004只在客户端），按日开放 | 提供 |
| 4 | tutorial | 启用；门槛1004，必须有QuestUnitTable固定队伍 | 不提供 |
| 6 | subjugation | 启用，计分模式；门槛1004，Boost.V 1–3，协力任务回501 | 不提供 |
| 8 | reproduce | 启用；门槛1004，复刻任务按批次窗口开放，9032随ヒストリー任务920014常驻并受其公开条件约束 | 提供 |
| 9 | devil | 启用，支持★4；门槛1004，另有Tag150菜单等级门槛 | 不提供 |
| 10 | harlem | 启用；门槛1003，须持有已觉醒的配对卡 | 不提供 |
| 11 | raid | 启用；门槛1004，另有Tag120菜单等级门槛 | 不提供 |
| 14 | tower | 启用；门槛1004，须塔已揭示并持有挑战物品 | 不提供 |
| 15 | training | 启用；门槛1004 | 不提供 |
| 16 | history | 启用；门槛1004，另有CfgMissionHistoryTable公开条件 | 提供，取决于900001是否开放，另受同一公开条件约束 |
| 18 | empire | 启用，支持★4；门槛1004，另有Tag801入口门 | 提供 |
| 19 | goddess | 启用；无门槛 | 不提供 |
| 3/7/13 | unavailable | 主表没有Type3/7关卡，強襲任务的关卡不在主表，回501 | 不提供 |

策略接口：开战与委任都经`quest_modes.admit`入场，先查前置关（409`quest_locked`）与关卡Rank（409`quest_rank_required`），再调`check_start`。`check_start`依次检查启用、门槛关、任务选择、日开放（`day_open`，仅`timed_conditions`策略）、特殊条件与★4，并返回通过的任务，子类经super()扩展；入场之后开战与委任都先经事件任务奖励守卫（模式策略没有在`event_reward_families`中声明结算的族回501`quest_event_reward_unsupported`），再进入`start_boost`或扣费；`start_boost`返回本场生效的Boost次数，默认1；`prepare_start`在扣费后返回新存档与附加子项；`settle`返回新存档、附加子项、需并入POSSESSION的物品（须为ItemConfigTable已知物品，与委任一致），以及presents.enqueue格式的礼物（免费结晶、金币、物品、单位模板），由基础结算投递或排队。两个钩子与基础结算同一CAS提交，返回值不合法回`quest_mode_hook_invalid`且不改存档。`score_result`为计分模式给出写入记录的MaxRank（8CEAF0/8CEC40解码的打包值）、分数与明细，默认回503；`entry_open(catalog, now=, platform_code=)`是模式的菜单入口门（默认恒真；帝国覆写为Tag801横幅与页签的可显示加开放），委任目录的availability单独读它并以`mode_entry_closed`计入阻断直方图，覆写者必须在`check_start`里以同一判定拒绝（帝国回409`quest_closed`）；`local_rules`返回进入`support_status[*].local_rules`的本地口径，默认为空。上下文带`services`句柄`ModeServices`（礼物箱、单位状态、持有物、塔服务与大討伐目录），模式包据此判断依赖是否可用；`SettleContext`另带boost、score与本场之前的记录previous_record，`ScoreContext`带结算前记录的副本、boost、mission_id与服务句柄。结算请求`QuestFinish`带BL、AL、HL与出击记录`sallies`（SallyRecord八个字段：invite_index、plain_cost、sally_cost、prop_count与token/guest/hold/avatar四个单位标志）。`QuestCatalog.resource_rows`与`archive_rows`为模式包读取额外原生表及归档成员表并记录摘要。`mission_matcher`见任务联动。

`undeliverable_rewards(catalog, services)`默认返回None，表示该模式开战不预检奖励；返回元组时列出全部无法邮寄的预检奖励（重复档按跨越一次检查）。`QuestService.status()`（readiness.main_quests）的`mode_reward_delivery`只收录已启用、返回元组或抛ProtocolError的模式，键为整数Type，值为`{"ready": 列表是否为空, "undeliverable": [...]}`；钩子抛ProtocolError时记为`{"ready": false, "error": 码}`，返回非元组或含非对象项时error为`quest_mode_hook_invalid`。当前只有大討伐（6）与女神（19）实现该钩子，默认票池下两项都为ready且列表为空；塔在结算时把档位转入deferred，不在开战预检，所以不列入。该项在生成构建时计算一次，/health不含此项。大討伐与女神的开战预检与该钩子共用`present_backlog.require_deliverable`：先`require_bounded`（说明文字与物品ID不超过原生单元255字节，否则503`quest_present_unsupported`），再对空礼物箱调用`presents.preview`；开战把失败包装为501（`quest_subjugation_reward_unsupported`或`quest_goddess_reward_unsupported`，cause为原错误），钩子把错误码记为reason。塔的`TowerService.deliverable`调用同一函数并把错误码作为deferred原因；`PresentBacklog.send`只复用`require_bounded`，空箱预览失败仍包装为503`quest_present_unsupported`。

任务选择由`MissionWindows.require_playable`按MissionID升序检查列出该关的任务：对请求平台登记且OPEN、可选的`MissionSelector.accepts`、任务最低等级、RequestQuestIds与KeyQuestID；没有可玩任务时按到达的最远阶段回`quest_closed`、selector的码、`quest_rank_required`或`quest_locked`。selector不带choose时停在第一个可玩任务；带choose时先收集全部通过者，choose必须按身份返回其中之一，否则503`quest_mission_selector_invalid`；accepts抛出的错误原样传出。开战不带selector，委任用它接入菜单门并优先选免券任务。

委任不开战，模式经两段钩子参与：发奖前`entrust_rewards`调整本批抽选结果，默认实现按该关DropBonus调用与结算相同的纯函数`drop_bonus.bonus_grant`，没有增量时原样返回同一个draw，覆写须先调super()；产票与EventParam不经钩子，由底座挂点在钩子之后计算，增量按钩子items的同一口径校验；发奖后`entrust_settle`不得改动quest_state（含mode_progress），也不得发礼物。策略在类上覆写check_start、start_boost、prepare_start、score_result或settle时，必须把名字写进类属性`entrust_parity`（frozenset，registry构建时校验声明的名确实被覆写），否则`entrust_blocker()`给出原因，该Type不进入委任；委任传request=None，读取request的覆写不能声明check_start。当前Empire、History与Reproduce声明{check_start}（入口门与History公开条件都不读request），Subjugation、Devil、Harlem、Raid、Tower、Goddess的blocker非空。倍率类奖励必须是两侧钩子共同调用的纯函数，不能只写进QuestService.finish或按quest_type分支，否则blocker察觉不到，委任会漏掉倍率。细节见`docs/entrust.md`。

平台码由`quest_modes.require_platform`统一校验，须为0..31的int，否则400`quest_platform_invalid`；调用方为`MissionWindow.enabled`的整数掩码分支、全局Tag解析的全部方法与委任76的请求解析。Enable的读法按任务分类而不是按值类型（各分类加载器的原生读法）：分类1 Emergency、2 Subjugation、3 Reproduce、8 DevilAdvent、10 Raid、11 Assault、12 Tower按平台位掩码`(1<<平台码)&Enable`（8CEEA0；掩码分类里布尔真值1只启用平台0），其余分类按布尔`GetInt≠0`（B09FE0），分类4 DailyReproduce不读表、恒启用；当前资源布尔分类都是bool列、掩码分类都是int列，两种判定结果相同，分类读法保证资源换型时仍与客户端一致。`MissionWindow.listed`只表示Enable在某个平台非零，按平台判断登记一律用`enabled(platform)`（8D8140的+80）；`archive()`、playable_quests、帝国`_owner`与quest_titles仍按listed判断，行为不变。

全局Tag解析由`QuestCatalog.mission_tags()`缓存的`MissionTagIndex`提供，`mission_registry()`给出同一张登记表：分类0–3、5–17共32张任务表，分类14/17的成员取QuestIdList，分类4由服务端数据构造，不入表；分类14–17按config行建任务，其余只为MissionQuestList列出的任务建任务；Tower不读TagID；跨分类重复的MissionID回503。tag≤0无结果；否则按MissionID升序，取第一个客户端建出、对请求平台启用且TagID≤tag≤int32(TagID+TagIDRange)的任务，开放与否都算（8D75A0），结果按（tag，平台）记忆。`is_open`按8CB420判断，不加宽限；`displayable`另要求至少一个成员关当天679B10为真（8CB810，成员关在QuestTable缺行时按关闭）；`rank_reached`比较成员关的最低Rank（991E30，无成员时为300），不看是否开放。魔神、神獣、帝国的菜单门与委任的Tag菜单门共用它，登记表任一张构建失败时这些模式开战回原503；`preview_mission_tags(overrides)`只做影响预览，不替换服务的登记表与Tag索引。

## 模式包

- 挑战（1，任务800001）：validate_row要求Type 1、UnitList 0、BonusType为100–107中能解析成gift物品的一种（当前全部为102 ルビー）。Rank 50的2050–2061由QuestTable.Rank检查；无★4；普通敌人上的PrizeEnemyDropPercent客户端不读，掉落按本地`drop_basis_points`。
- 曜日（2）：timed策略，按679B10逐日开放并给300秒跨日宽限。validate_row要求Type 2、UnitList 0、_HoldingParam 0..127、OpenWeek 0..10、StartOpenDay/EndOpenDay≥0。BonusNum为0时首次★3按`first_three_star_crystals`发结晶。新手任务10002按派生规格只计101/111/121/135/138。不设门槛关（用户决定）：任务顶部横幅、オーブ确认与觉醒オーブ列表三条原生入口本身不查关卡，门户横幅脚本查1004；1004前静态上到不了任务顶部，觉醒入口在1003通关前因启动器不可用、之后被教程守卫禁用，只有关卡列表的オーブ掉落入口没有教程守卫（故事列表是否列出オーブ掉落未核）；开战与委任仍查前置关、关卡Rank 5、任务窗口与平台位、日开放。
- 大討伐（6，任务400183/400212/400587）：计分模式，分数、奖励与规则文件见计分结算。validate_row要求Type 6、Treasure1–5为0、BonusType 0、UnitList 0。check_start在底座检查后依次要求：大討伐目录可用（否则409`quest_subjugation_unavailable`）；所属任务是普通讨伐（SubjugationType 0），协力任务（SubjugationType非0，当前17个，Enable全为0）回501`quest_mode_unsupported`，任务窗口补丁也不能重开协力任务（见`docs/mission-windows.md`）；SubjugationMissionQuestList的StartButtonID非空，无开始按钮的首关6401/6611/30018回409`quest_locked`，服务端也不为它们合成记录；礼物箱服务可用（409`quest_present_unavailable`）；本场可达的讨伐奖励档都能投递，否则501`quest_subjugation_reward_unsupported`且不扣费。按资源窗口，400587（30018–30023）于2026-10-29 15:00至11-12 10:59:59 JST开放；400183与400212已于2022-12-01结束，二者同属`Subjugation:400183`批次，可一起重开。无★4，78/82回`quest_hard_unavailable`。大討伐目录的status传入当前时刻时列出Enable≠0、未结束的协力大討伐（`cooperative`：SubjugationType 1/2；玩家选中它、客户端载入其任务菜单时不经确认就发143/147，本地没有处理器、回501，客户端每次确认弹窗后原样重发），readiness的`auto_requests.cooperative_subjugations`同步列出，期望为空；目录读不出时它为null，`auto_requests.ready`为false，serve另记一条WARNING。
- 复刻（8）：成员来自ReproduceMissionConfig与ヒストリー任务。validate_row要求Type 8、_HardLevel 0（无★4）、BonusType 0、BonusNum 0，UnitList非0时须能解析QuestUnitTable固定队伍（当前6747、7894、8254），首次★3奖励恰为按`first_three_star_crystals`发的结晶。207关全部可建：其中206关属28个复刻任务，原窗口都已结束，默认回409`quest_closed`，由任务窗口规则按`Reproduce:300084`、`Reproduce:300217`、`Reproduce:320180`三个批次重开；9032属ヒストリー任务920014，地图为`Map920014_3809.aar`，前置关4030，随该任务常驻开放，开战与委任时要求920014已公开（d≥1且前置920013合计≥11星），ReproducePolicy为此覆写check_start并在entrust_parity中声明。4807–4813是唯一带条件候选的关，见地图与掉落候选；19关带DropBonus。7208等关的收集奖励是Stack为0的召唤券（如item_smn_ticket_19007），按既有规则入POSSESSION、只受int32限制。分类3计GoldGet。属委任审定集，207关都可委任，其中204关每次1张委任券，6385–6387无券。
- 魔神降臨（9）：validate_row要求Type 9且Treasure1–5全为0。check_start在底座检查后追加菜单等级门槛（OnCommandMissionTypeDevil的`IsReachedRequiredRank("Tag",150)`，991E30）：经全局Tag解析取覆盖150的任务，分类不限，玩家等级须达到其成员关卡的最低Rank（当前500090，Rank 50；平台0–12解析到它，13–31没有任务）；等级不足或没有任务覆盖时回409`quest_rank_required`，平台码非法时400`quest_platform_invalid`，登记表构建失败时原样回503。MissionID更小的其他分类任务若覆盖150，会决定魔神菜单等级；只有config行、没有DevilAdventMissionQuestList行的魔神任务客户端不会建出，因而不参与。★4门槛Rank 50。2026-11-02共58关可打，★4为9504/9508/9510。
- 交流（10）：EventPersonConfig03（pevconfig.aar）每关恰好配对一张Kind=1的卡，带出现条件（_AppearQuestID/_AppearExpression）的行回501；validate_row另要求UnitList为正且固定队伍可解析。开战须持有配对卡且ClassChangeCount≥2；首通尚未发生时需要CardStatus服务（409`quest_card_status_unavailable`）。首通写CardStatus.A7|=4并把该卡整行作为最后一个子项返回；重复通关不改存档。不写LookHarlemFlag、WatchEventFlag与好感。
- 神獣（11，任务分类10，19个任务）：validate_row要求Type 11、Treasure1–5为0、_HardLevel 0、BonusType 3、UnitList 0，不符回503；IsUseRaidTeam非0仍由目录回501。开战路径为77AB70先查`DoQuestPlayable::Raid`，再进入Func_RaidSallyView→DoQuestStart_Raid→88AA20。门槛取OnCommandMissionTypeRaid行1的1004（任务顶部的神獣横幅不查关卡，但1004前到不了任务顶部；QuestMenuTop行77的Tag120行在本客户端不被求值）；check_start在底座检查后追加同一脚本的菜单等级门槛`IsReachedRequiredRank("Tag",120)`：经全局Tag解析按请求平台取覆盖120的任务（分类不限），玩家等级须达到其成员关卡的最低Rank，当前解析到900170（Rank 20），平台13–31解析不到任务；不足或没有任务覆盖时回409`quest_rank_required`，消息为“Player rank has not reached the raid menu rank of Tag 120”，与关卡Rank的消息不同。真实资源92关全为Rank 20，所以Tag门与关卡Rank同值。★3发BonusType3单位（57关为133，19关为250，另16关各发一种），有经验与金币，分类10不计GoldGet；无★4，78/82回`quest_hard_unavailable`；Boost接受并忽略；BL可列出神獣编成全部分组的单位，任务队伍只计存档中的单位。2026-11-03有11个常驻任务55关开放（批次`Raid:900170`），8个限时任务37关已结束，可按`Raid:900380`、`Raid:900456`、`Raid:900483`、`Raid:900580`批次重开。`RaidPolicy.describe(catalog, now=, platform_code=)`给出Tag120解析与各神獣任务的开放状态。
- 塔（14，任务110001英傑、110002統帥、110003悪霊）：validate_row要求Type 14、UnitList 0、Treasure1–5为0、恰好属于一个启用的塔任务，且共享计分表与该塔分数奖励表都有本层。check_start在底座检查后要求塔服务与礼物箱服务可用（409`quest_tower_unavailable`）、塔服务读同一份关卡目录（503`quest_tower_catalog_mismatch`）、所属塔已揭示（DiscloseQuestID为0或已通关，否则409`quest_locked`）、挑战物品持有加待补发至少1个（409`quest_tower_consume_shortage`）；prepare_start先惰性结算并冻结1个挑战物品的成本（不扣减）、回POSSESSION当前未扣数；settle在成功结算时扣掉冻结的挑战物品、计分、更新层最好分与塔合计（严格刷新时保存该层HighScoreTeam快照）、发到期档并回九列TowerResult。78/82回`quest_hard_unavailable`，失败、撤退、放弃不扣挑战物品（官方id 9370）。协议、计分、奖励与存档见`docs/tower.md`。
- 練習（15，任务910001/910002）：validate_row要求Type 15、UnitList 0、BonusType 4（无★奖励）。不扣魅力与体力，经验和金币为0，GoldGet不计。结束路径与底座口径一致（静态闭合）：地图脚本设计好的结局（木人击破、讲解角色离场、DoMissionSucceed）都走成功结算83，星级按击破与阵亡照常取1–3；ntwh57oy只来自暂停菜单ギブアップ（`QuestID,-1`）或生命0（`QuestID,0`），練習关defHP全为99且七张图都没有DoMissionFailed。エネミーテスト910002（9911–9919）与其他練習一样只受任务窗口约束：原生窗口已于2026-03-01 23:59:59 JST结束，之后由窗口检查回409`quest_closed`，用任务窗口规则的`Training:910002`批次重开后与普通練習相同。9918是唯一带掉落行的練習关，7个候选（槽位1,2,2,3,3,4,5）全部指向空Treasure，取得时只置记录位。新手任务10008由練習规格计数。
- ヒストリー（16）：成员来自CfgMissionHistoryTable的QuestIdList（4125只在这里出现）。开战镜像CfgMissionHistoryTable的公开条件：起始日取GameParam2.GameStartDateTime的日期部分（空串或"0"按2016-04-14），公开天数d=LastLoginDate−起始日（32位，不读时钟，见`docs/energy-clock.md`）；组首行（表行序）DisclosureConditionDays−d>0时整组未公开；DisclosureConditionMissionId为0或不在本组时任务直接公开，否则还要DisclosureConditionDays≤d，且前置任务成员MaxRank之和≥单精度阈值（总星×Rank/100再加0x3F7FFFFE后截断；总星为每个成员3星，_HardLevel≠0加1，QuestIdList重复项计两次，前置任务在本平台未登记时两者按0）。表中没有该任务、组未公开与任务未公开都回409`quest_closed`，消息各不相同；表缺列、非int32或MissionId重复时History定义回503`quest_mode_resource_invalid`，存档日期缺失或非法回503`quest_state_invalid`。开战不求值Tag302；委任76经同一check_start受同一条件约束；复刻9032经920014开战与委任时同样检查。任务10009由历史规格计数。History任务的TagID都是302，经全局Tag解析到900001，只影响委任菜单门`IsEntrust_14`。DropBonusConfig按QuestID对任意Type生效，当前2003个ヒストリー关与19个复刻关都是trade_item_002000在JST周一/三/六×1.5：83在结算的掉落账本阶段由底座调用、76经`entrust_rewards`默认实现调用同一纯函数`drop_bonus.bonus_grant`；整表故障时全部按无加成放行，行级故障只关该关，见结算。
- 帝国大戦（18）：validate_row要求该关唯一启用的成员任务属于Empire分类；有_HardLevel时★4奖励须为所属任务EmpireMissionConfig.DarkCrystalItemId的物品（当前54关均为闇水晶×100）。★4门槛Rank 5。check_start在继承的检查之后要求Tag801入口门：门户横幅`CMissionPortalMenu::Empire::UpdateBannerVisible`与页签`CMissionPortalMenu::EmpireTop::IsCreateTab_PageEmpire`都是`IsCleardQuest(1004) && HasAnyDisplayableQuest("Tag",801) && IsOpenMission("Tag",801)`，所以Tag801解析到的任务（当前940012）须当天可显示且开放，按原生&&的顺序先判displayable、再判is_open，不满足回409`quest_closed`。普通开战、★4开战与Type18委任都经过这道门；EmpireTop的`IsReachedRequiredRank`只管置灰，不镜像。关闭940012的窗口会使全部84个普通开战、54个★4开战与委任报quest_closed；该窗口只能经资源覆盖同时修改客户端与服务端的EmpireMissionConfig，任务窗口规则不覆盖帝国分类。只属布尔Enable帝国任务的42关遇到非法平台码时也前置回400。PT3帝国页642–645可计数，含★4。
- 女神の復活（19）：无门槛、无★4，开放等级50由QuestTable.Rank决定。结算把全部Type 19记录（含横幅任务950000的16000）的MaxRank之和写入GoddessRevivalTotalStarCount，变化时回PGP2键值子项。服务端另在`quest_state.mode_progress.goddess = {"granted": N}`记录已发放的最高总星，与结算同一CAS提交，不发给客户端：只发阈值落在（granted，新总星］的GoddessRevivalRewardConfig档，之后granted取两者较大值，所以总星回落再回升不会重发；没有记录的存档以当时的GoddessRevivalTotalStarCount为granted，第一次女神结算即写入记录。礼物箱满时排队的档位也算已发。其他模式的开战、结算与撤退原样保留这条记录。RewardType 22发物品，RewardType 1的RewardParam是PresentUnitData模板（9173E0→630BA0），说明文字为“女神の復活クエスト報酬”（SystemText 6267）。第5/10/30/50/55档的召唤券（池418）默认可投递。开战需要礼物箱服务（409），并预检（granted，本场可达总星］内的档位：先按原生单元255字节检查说明文字与物品ID，再用空礼物箱预览，任一档不通过即回501`quest_goddess_reward_unsupported`且不扣费（超长时cause为`quest_present_unsupported`）；显式`--item-ticket-rules`不含418时即属此类，此时`mode_reward_delivery`中Type 19一项为`{"ready": false, "undeliverable": [...]}`，逐项列出threshold、reward_type、param、item_id与reason（缺池为`summon_ticket_pool_disabled`）；NeedStarNum大于已存总星时锁关（784C60/757890，当前全为0）。

## 地图与掉落候选

按8C5360，任务分类2/12/13/14/15/16/17的地图为`Map{MissionID:04d}_{MapNo:04d}.aar`，取唯一列出（Enable≠0）的成员任务；多个列出任务给出不同地图包回503`quest_mission_ambiguous`，没有列出任务回409`quest_closed`。其余分类用`Map{MapNo:04d}.aar`。ヒストリー（14）与女神（17）的成员来自Config.QuestIdList逗号列表，允许末尾逗号。Type6/8/14/15/16/18/19共2483关的地图都可读取。

Entry的PrizeCardID为1–255：1–5对应Treasure1–5并置记录槽位位，Treasure为0时只置位不发奖；大于5的槽位照常抽选，但不发奖、不置位；超过255回501`quest_candidate_unsupported`。奖励行的EnemyID与Loop须≥1，PrizeEnemySpawnPercent与PrizeEnemyDropPercent须为非负整数，否则503`quest_entry_invalid`；PrizeEnemySpawnPercent客户端不读，只校验整数。行过滤（67BF70）与预定敌数（5D5FF0）只在`quest_entries`实现一次，掉落候选与大討伐计分共用：

- 敌人ID按无符号分段：0–999为普通段；6000–6999只在记录MaxRank≤2时纳入；7000–7999是特殊候选，RT的ATTR另或2，PrizeEnemyDropPercent非0时它是覆盖码（5D33E0），只发覆盖码，不置槽位位，也不参与收集判定，为0时退回Treasure并置位；8000–8999由客户端按日期本地掷骰，服务端无法核对，回501`quest_candidate_unsupported`；1000–5999、≥9000与负数（只可能出现在非奖励行）属跳过段。奖励行落在跳过段时不成为候选，按地图/Entry与分段计入`QuestCatalog.status().excluded_prize_rows`（当前资源为空）。
- IsAppear缺列或为空视为无条件；只接受无参的`GetNum_QuestStar()`与0..2147483647十进制数的比较（`>=`、`<=`、`>`、`<`、`==`，分号可省略），换算成星级区间后与分段区间取交，交集为空时不成为候选；`!=`、带参调用、数值越界与其他表达式回501`quest_candidate_unsupported`。
- 开战取该关记录的原始MaxRank（int32≥0，无记录为0，计分关为打包值），用`candidates_for`选出候选，期望RT由`candidate_attributes`按这组候选计算，active另存`candidate_stars`；结算只用它恢复同一组候选，发奖、置位、兵营预检与TreasureInfo都用这组候选，奖励码经`reward_code`、是否置位经`marks_slot`判定。条件成立的特殊候选与普通候选一样按`drop_basis_points`抽选。按记录选出的候选超过32个回501`quest_candidates_overflow`；83的取得掩码超过7位回409`quest_drop_mismatch`（4A1F40最多记录7个）。`treasure_code`遇到条件或覆盖候选回501，漏改的消费方会显式报错，不会发错奖。
- 真实资源只有ゴールドラッシュ的4807–4813带条件奖励行：每关一行`>= 3`的特殊敌人（7002–7006），槽位1，覆盖码437（单位），记录3★时进入候选。特殊行在Entry中的位置因关而异，排在前面时其后候选的RT序号随之后移。另有8行`<= 2`的6000段非奖励行，不影响候选；8000段的行为0。

## 开战

- 难度只由注册号决定：77/81普通，78/82为★4。配对固定为77/78→80、81/82→83，不符回`quest_result_profile_mismatch`。
- 非Type0关卡允许Rank0，前置关可跨类型；StartOpenDay/EndOpenDay/OpenWeek/_HoldingParam只有声明`timed_conditions`的策略放行。IsUseRaidTeam按int32解析，非0回501`quest_condition_unsupported`。
- 任务窗口按8CB420：Start、End都为0时常开；now<Start未开放；now≥End已结束；有Start而End为0视为已结束。窗口比较用客户端时钟（AAC970整秒落在4 ms网格），不是服务端原始时刻。Enable按分类读布尔或平台位掩码（见「模式注册」）；位掩码分支经`require_platform`校验，经Tag解析的魔神、神獣与帝国菜单门对非法平台码前置拒绝。任务最低等级取成员关卡Rank的最小值；RequestQuestIds所列关卡须达到RequestClearRank，为0时要求三星。日期超出可表示范围的任务行按资源无效处理。
- KeyQuestID只由Story与Empire任务读取（8D83A0、8C8020），该关未通关时回`quest_locked`。
- 日开放按679B10：JST日数为`date.toordinal()-1`，星期日为1，两者经`native_calendar`取`business_calendar`的JST民用日，客户端日历（JST 1970-01-01至3001-01-19）之外的时刻回503`quest_clock_invalid`；_HoldingParam非0时按星期位掩码判断，否则OpenWeek 0每天、1–7单日、8为日与六、9为二与五、10为四与五；StartOpenDay、EndOpenDay都非0时为左闭右开区间。客户端出发时才复查，所以服务端允许300秒跨日宽限。
- SpecialMissionPlayCondition按632A90：类型1区间MaxRank合计、2区间通关数、3区间合计≥3×关数；其他类型跳过，不锁关。委任使用同一份解析与判定。
- ★4解锁要求`_HardLevel`非0、策略支持★4、MaxRank≥3、每个非空Treasure槽位位齐、玩家等级达到门槛。78/82不带候选掉落，抽选为空。
- RT的ATTR由开战与委任共用的`quest_modes.candidate_attributes`计算：按65D900，槽位1–5看对应位，其他槽位在记录存在且AchieveFlag非0时视为已取得，再与候选special_attr按位或。
- Boost.V由策略的`start_boost`决定：大討伐按`boost_policy`返回请求的V（1–3），魅力与体力按×V预检并冻结成本，active.mode另记boost；其他模式返回1，Boost.V接受并忽略，成本按单次冻结，数值记入`active.mode.boost_ignored`。底座只接受钩子返回1或请求的V，否则503`quest_mode_hook_invalid`（77A690按4316C0发出的Boost.V预检费用）。掉落不因Boost重抽。
- 扣费时点按官方口径（aigis1000.jp id 9370，2022-06-16维护后）：开战只按现值≥V×成本预检（不足照旧回409），不扣减，魅力、体力与挑战权只在83成功结算时按开战冻结的成本扣一次；失败（ntwh57oy `QuestID,0`）、ギブアップ（`QuestID,-1`）、リトライ（直接重新开战，旧active被替换）都不扣，リトライ多少次都只在通关时扣一次；大討伐ギブアップ消耗0。新开战把冻结成本写进active的`charge`字段（值`settle`表示结算时扣）；没有该字段的旧active是旧规则下开战时已扣过的，结算不再扣、退出不退。结算先按当前时刻结算回复，再扣费、后入账（升级回满在扣费之后）；余额此时低于冻结成本时扣到0并写WARNING，不拒绝结算。81/82回包带当前未扣余额。
- 开战快照本难度的★奖励列表；未取得的★单位奖励与掉落一起预留兵营技术空间。★结晶将超出钱包而又没有礼物箱服务时拒绝开战。active另记`mission_id`与`candidate_stars`。
- 显式复刻窗口只经`--mission-windows`规则补丁有效资源，见`docs/mission-windows.md`。`QuestCatalog`的`window_overrides`参数只作测试设施，生产装配不传；同时传入时服务端改用覆写窗口，会与客户端读到的资源不一致。

## ★奖励

按679D70：普通读BonusType/BonusNum/NormalMode_RewardItemID，Type3为单位CardID=BonusNum、数量1，其余数量=BonusNum；★4读HardMode_RewardType/Count/CardID/ItemID，Type与Count都为0时视为结晶1；Type10两种难度都追加结晶3。Type100–107经SystemText名称和ItemConfig EffectType4解析为gift物品；≥100000且Type%1000=22时按ItemID发物品。无法发放的组合在开战前回501。

普通★3置0x00100000，★4置0x00200000并记MaxRank4，每档只发一次。★单位按QuestTable.UnitLevel计算经验，战术指南为0，与1级同为0经验。计分类型（6/7/13）的★奖励为空：461E80不为它们记★位，不会触发首次★奖励。

## 结算

- 非计分关的星级须属于本难度集合：普通1/2/3，★4为1/2/4，否则400`quest_result_invalid`；计分关见计分结算。击破数上限100000。
- QR合并通关位、槽位、收集位与星级位，保留未知位并回完整行。收集完成按460020在写入本次槽位位之后判断：Treasure4为0、其余四个非0、槽位1/2/3齐且bit0未置；槽位5的位就是bit0，同场取得槽位5后不再发收集奖励。
- 金币按公开资料口径（用户决定）：单次金币 = ⌊(QuestTable.Gold×gold_percent/100 + GG次数×每次GG金额) × 星级倍率 × 能力倍率 × MoneyUp倍率⌋，整数有理运算、最后一次向下取整；金币 = 单次金币×V，V为开战冻结的Boost次数（委任为次数），GG只在V=1时计入。GG次数 = min(GoldGet, GG.I, max_gold_events)，不计GoldGet的模式为0，数值不一致时取小值而不报错，以免客户端原包重发。每次GG金额 = gold_per_event×(100+BL真实单位中InfluenceType 88的最大Param1)/100（单位能力按4D5110取其图像替代卡的能力；ゴールドゲットプラス130G、ゴールデンミミック120G）。星级倍率★1×1、★2×1.2、★3×1.5（★4难度结果4按★3、2按★2计；计分模式不判星、不乘）。能力倍率 = (100+BL真实单位中InfluenceType 78的最大Param1)/100（单位能力按4D5110取其图像替代卡的能力，王子309固定取本卡的Ability，GG金额同此），同类只取最大，访客、临时、固定队伍与替身不计；委任按持有单位计（含额外兵营）、星级按★3。MoneyUp：Config.MoneyUpCloseDateTime晚于当前JST时×1.5，作用于基础金币加GG。入账经`wallet_money`以全游戏金币上限99,999,999（GOLD_LIMIT）封顶，超出部分丢弃；余额已高于上限的旧存档保持原值，不因金币拒绝结算。REWARD.GOLD回实际入账额，PGP回绝对余额。图像替代卡在readiness的`main_quests.gold_abilities.image_aliases`为true时接入（83与76共用；礼物服务的依赖未就绪、礼物目录或图像别名表构建失败时为false，按单位本卡取能力）；资源若出现unsupported的图像别名，带这张卡出击的83与持有它时的76回503 `image_alias_unsupported`，客户端弹窗重发，当前资源的unsupported为空。
- 经验：单次经验由开战结算与委任共用的`QuestService.experience_reward`计算，结算使用开战时冻结的规则。经验先按experience_percent取整；Config.ExperienceUpCloseDateTime按JST解读，晚于当前时间时再按`(int)((float)x*1.2f)`计（6604F0，单精度，如4000003→4800004）。金币全部按上一条的单一公式整数有理运算、一次取整，MoneyUpCloseDateTime同样按JST、按结算时刻判定（660440判定；客户端77C1E0只在选关预估里对基础金币做单精度×1.5f，结算金额取PGP差值，与服务端取整无冲突）；Boost.V>1时重复的是加成后的单次值。经验或金币合计超过int32时不回400：经验最多记到末级，金币按99,999,999封顶，超出部分丢弃。
- DropType4/6/7/8与★物品进POSSESSION并回绝对数，超Stack部分丢弃并回IOF；Stack为0的物品（召唤券）不设上限，只受int32限制，与原生交换入账一致。
- DropBonus（905A00/905B40/9055E0）：DropBonusConfig按QuestID适用任意Type，同一QuestID只取首段连续行，组内同一ItemType取首行；只支持ItemType1（905340的文本1415358870在ItemConfigTable中唯一对应trade_item_002000）、ItemID0与Operation1，主表中没有的98行和20个非连续组只计数。83在取得槽解码之后、唯一一次credit_capped之前由底座调用`drop_bonus.bonus_grant`：按开战时刻（active.started_at）的JST星期（周日=1）取值，只乘本次取得槽掉落中该物品的汇总量，入账min(max(原数, floor(float32值×原数)), 2147483647)，每次请求取整一次；加成后的数量写进int32列，与MoneyUp一样截在int32上限，超过Stack的部分照常丢弃并回IOF；★奖励、收集奖励、模式礼物与★4不乘。当日值为正时回BONUS(ItemType, Value)，本次没有掉落该物品也回，值为0不回；TreasureInfo保持未乘的数量。整表读不到或列结构不符时，全部关按无加成开战、结算与委任，不回BONUS，`QuestCatalog.drop_bonus_status()`报error，journal记drop_bonus=null，结果缓存到重启；某关首组的值非有限、为负或组合不受支持时只关该关，`mode()`回503`quest_drop_bonus_invalid`或501`quest_drop_bonus_unsupported`，开战与委任都拒绝；ItemConfigTable中该文本不再唯一对应一个物品时，全部带加成的关回501。当前资源没有行级故障。
- 战斗活动挂点`BattleEventRewards`是关卡产票与EventParam的唯一组合点，83与76共用，按“产票→EventParam”的顺序执行，不含DropBonus。83在掉落、DropBonus与单位入账之后、唯一一次credit_capped之前调用它：产票按QuestTable魅力与Boost次数计数，按开战时刻（active.started_at）选发票组，规则见`docs/vote-events.md`；EventParam每次结算都写为min(原值+DropType5, 1800)（460020每次结算前在本地累加并封顶1800），有DropType5掉落或数值被改写时回PGP.AF。挂点只读存档；产票提供者抛出的错误让结算失败，未挂接提供者时不产票并在status报原因。挂点的内容版本记录提供者的版本或缺席原因码，嵌在委任76的回执摘要里；`replayable_content_versions()`在当前版本之外给出`ABSENT_PROVIDER_ERRORS`各码的缺席版本（未挂接、依赖不可用、提供者自身的三种构造失败，以及构造期原样传出的投票目录与过期服务就绪码），所以提供者后挂接时旧委任回执照常重放；因其他原因缺席时提交的回执仍回409`entrust_retry_stale`，见`docs/entrust.md`。83的结算重放不核对挂点版本。挂点的错误码均为503：`battle_event_context_invalid`、`battle_event_grant_invalid`、`battle_event_provider_invalid`、`battle_event_receipt_invalid`、`battle_event_clock_invalid`。
- 结晶按458330只比较免费桶：免费合计加本次结晶不超过1000时入钱包并回完整PFC；否则整笔入礼物箱，说明为“★３報酬”或“★４チャレンジ報酬”。
- 礼物箱已满时，溢出结晶与模式礼物存入`quest_state.pending_presents`：每项为`{reward, copies}`，完全相同的奖励（含数额）只增加份数、从不累加数额；种类上限等于礼物箱容量500，份数不超过int32，超出时结算回409`quest_present_backlog`。以后任一战斗结算先按项的顺序补投，每份单独成一封邮件，保持各场结算的原始数额，领取上限照常适用；遇到容量错误（`present_capacity`、`present_identity_exhausted`、礼物列表超过42号响应上限的`response_too_large`）即按减半批次重试并停止，保留剩余份数。只有本场已遇到容量拒绝时，新的战斗礼物才排到队尾；相同奖励的新份数并入较早的项，所以顺序按项保持。委任、领取与登录不触发补投。入队前先按空礼物箱预检，无法投递的礼物直接拒绝结算（503`quest_present_unsupported`），不会写进队列；已排队的礼物若因资源变化不再被礼物箱接受，只保留在队列并写警告日志，不阻塞其他礼物。奖励说明与物品ID按UTF-8字节计：新入队的礼物须放进原生单元的255字节，否则拒绝结算（503`quest_present_unsupported`；模式钩子返回的礼物回503`quest_mode_hook_invalid`）。已存队列仍按256字节读取，旧档照常加载：256字节的说明照常补投，客户端显示时截到255字节；256字节的物品ID因礼物箱不为放不进原生单元的物品ID新建邮件行（`present_item_unreplayable`），会留在队列并写警告日志，不阻塞其他礼物。
- 掉落、DropBonus增量、产票、★物品与模式settle的物品合并为一个POSSESSION，恒带Enabled列（ItemID/Count/Enabled，保留已存开关，缺省行为0），与76、66、68和塔开战一致，已缓存的两列旧结算回复按存档字节原样重放；丢弃量合并为一个IOF；升阶邮件、补投、★结晶与模式礼物合并为一个PPT；BONUS排在PPT之后，模式附加子项排在最后。TREASURE在开战、委任与结算中都是显式空表：客户端收下后不读，不填DropID，抽中次数只经TreasureInfo回报。
- 单位奖励回PPU新行与PBP.UX。
- journal的成功记录另存candidate_stars、drop_bonus（没有DropBonus行、整表故障或★4时为null，否则为{value, increments}）、event_rewards（挂点回执，溢出先记在票上）、kills_reported、boost、score与score_details。

控制台只读入口：`QuestService.status()`的boost_policy、score_policy、candidate_policy、drop_bonus_policy、event_reward_policy、event_rewards、event_mission_rewards、mode_reward_delivery、money_policy、fixed_team_policy，以及restart_policy、exit_policy、charge_policy（扣费时点口径）与platform_codes_seen（进程内见过的平台码计数与最近一次，用于核对本地客户端实际发送的PlatformCode）；`QuestService.preview_event_rewards(quest_id, source=, runs=, now=, bonus_instant=, drop_items=, state=)`在存档副本上返回DropBonus与挂点回执，不写盘，bonus_instant决定DropBonus取哪一天、now决定发票窗口（对应83的开战时刻或76的执行时刻），参数非法回400`quest_event_preview_invalid`；`QuestCatalog.candidate_view(quest_id, max_rank, record=None)`给出按记录选出的候选、RT序号、奖励码与是否置位（参数非法回400`quest_candidate_view_invalid`），`drop_bonus_status()`给出available、error、行与索引计数（rows、quests、indexed_quests、duplicate_rows、ignored_groups、orphan_rows）、failures、weekday_profiles、rules与coverage_by_type，available为false时应作为告警。

## 计分结算

计分模式由`MODE_SPECS`的score_mode标记（6/7/13），当前只有Type 6大討伐可玩。

- 结算的TT第二项须等于第三项（击破数）且为普通难度，否则400`quest_result_invalid`。星级按0处理：只置CLEAR与本场槽位位，不写★位，不发★奖励与PFC。
- 底座以`ScoreContext`调用`score_result`；返回值须是非负int32的MaxRank、非负分数与可JSON化的明细，否则503`quest_mode_hook_invalid`。明细经JSON往返后存入journal，非字符串键变为字符串，撞键时回503。QR回写入该值的整行。经验与基础金币×V（Type 6均为0），产票按V次计；任务载荷stars为0、score_mode为true、kills为上报原值。
- 大討伐单场分数为`int(ceilf(f32(k)×f))`（45A7D0，474B20）：上报击破数小于预定敌数时f取ClearBonusFactor，否则取CompleteBonusFactor，≤1.0的系数钳为1.0（8D94D0）。预定敌数是67BF70纳入的Entry行Loop之和，减去`_Attribute`按逗号、顿号或空白拆分后含“置物”“分身”的敌人行（客户端另会并入Enemy.Attribute的位，本地未建模，现有资源无差别）；EnemyID为0或超出Enemy表的行照常计入；纳入行必须全为无条件行，否则503`quest_definition_invalid`。当前只有30023的系数为1.3f/1.5；预定敌数6401–6405与6611–6615为100/100/300/400/500，30018–30023为100/100/300/500/750/751，与NumberOfEnemyText“出現敵数：N”一致。
- 记录的MaxRank为打包值`(total<<12)|best`（8CEAF0、8CEC40、8CEBC0、8CECC0）：best'=min(max(best, s_best), 4095)，total'=min(total+s_total, L)，L为TotalScoreLimit，SubjugationType 1时为99999（8D8AE0）；当前400183/400212为10000，400587为7500。任务最高分取成员记录低12位的最大值，任务累计分为成员累计之和并封顶L。
- `SubjugationRules`经`--subjugation-rules`或start-server.ps1的`-SubjugationRulesFile`加载，示例`config/subjugation-rules.example.json`等于默认值，严格键，16 KiB以内，缺省为clamp与total_only；按严格JSON读取，重复键、NaN/Infinity或溢出成无穷的数字、嵌套超过64层都拒绝，命令行加载时服务报错退出，经配置注册表加载时报`config_rules_invalid`，嵌套过深报`config_nesting_too_deep`。`kills_policy`：`clamp`取k=min(击破数, 预定敌数)；`raw`取k=击破数，与原生结果页显示一致；系数的选择都看上报击破数。`boost_policy`：`total_only`扣费×V、累计分加score×V、最高分取单场score；`total_and_best`另把最高分改取min(score×V, 4095)；两者下Boost.V只接受1–3（缺省为1），其他值回400`quest_boost_invalid`；`ignore`接受任意V，费用×1，V记入boost_ignored。修改需要重启，不回算已结算的记录与水位；score_details记expected、factor、kills_scored、kills_over_expected、本关best/total、规则与规则版本。
- 讨伐奖励从结算起点（见下一条）判定跨越，服务端水位保存在`quest_state.mode_progress.subjugation[MissionID]={best,total}`。奖励阶梯取自共享的`EventMissionCatalog`（`quests.event_missions`，开战与委任的事件任务奖励守卫用同一份目录，原生60FDB0口径）：按ID_Mission升序第一个[ID_QuestFirst, ID_QuestLast]覆盖该关的条目（60FD30），ID_ResultReward指向RewardConfig组，ID_Reward为0的续行并入上一组；当前EMC 183/212/587对应组123/141/462，各16/16/33档。Type_Condition只接受4（任务累计分）、6（任务最高分）与7（累计分每P点）：4与6在before<P≤after时发一次；7先按Param_ValidCondition 1/3（6334F0）变换起止分，再发⌊after/P⌋−⌊before/P⌋次。Type_MainIncentive须为5；Type_SubIncentive 1发单位（Param_Incentive为PresentUnitData模板）、2发金币、7/14发旧式物品（legacy_item）、22发ID_Item物品（数量至少1）。类型7档只接受Sub22（任意Param），或Param_Incentive为1的Sub2/7/14：633810对其他礼物显示跨越次数，630BA0还把Sub1的次数当卡号，显示与发放会对不上（当前RewardConfig的335个类型7行都是Param为1的Sub14或Sub22）。只有类型7档会多次跨越，同一档多次跨越合并为一封：Sub22为max(1, Param)×次数，Sub2/7/14为次数；说明文字取SystemText 154「スコアクエスト報酬」。不符合以上约束的行在首次读取该任务的奖励档时回503`quest_subjugation_reward_invalid`，只有该任务不可用；SystemText 154为空时，需要发奖的开战预检与结算回同一码。
- 结算起点逐项取水位与本场之前记录（含本关自己的旧记录）的较大值，没有水位时就是记录：记录即客户端结果页的BeforeSummary，记录或水位已达到的档都不发；记录被抬到水位之上时差额档按已达处理、不补发，记录被调低时已发档也不重发。结算后水位取起点与新任务分的较大值，只升不降，重复结算不重发。水位与本场之前的记录不一致时结算照常完成，并写一条WARNING日志（任务、关卡、起点、水位与记录的best/total）。开战前以同一起点、以记录的任务分加上单场上限×V为可达分（水位高于记录时不在水位上再加一场），逐档先按原生单元（说明与物品ID不超过255字节）再用空礼物箱预检，任一档无法投递即回501`quest_subjugation_reward_unsupported`，不扣费。单场上限是一场可报告的最高分：clamp取预定敌数与预定敌数−1两个击破数中分数较高者，raw取100000与预定敌数−1中较高者（30018为100000、30023为150000），最高分累计时仍饱和到4095。30023的1127档召唤券item_smn_ticket_85144属池464，默认票池包含它；显式`--item-ticket-rules`不含464时，clamp下30023回501，raw下30019–30023都回501，30018因是首关先回409。`mode_reward_delivery`中Type 6一项逐档列出当前无法投递的阶梯档（mission_id、阶梯字段与reason），缺池464时列出400587的1127档；讨伐目录、礼物箱服务或任一任务的阶梯读取失败时整项为`{"ready": false, "error": 码}`。
- 控制台只读入口为`SubjugationCatalog.status(now=None)`与`describe(state, now)`：window.source为resource（有效资源，含任务窗口补丁）或override（只出现在测试设施window_overrides下）；describe的basis为下一场结算的起点{best, total}，mismatch按分项列出水位与记录不一致的方向（records_above_watermark：记录高于水位，差额档按已达；records_below_watermark：记录低于水位，已发档不重发），一致或没有水位时为空，记录非法时回503`quest_state_invalid`；各档按起点给出reached/unreached与next_at，remaining从记录算起；每个任务的next为{best, total}（best只看类型6档、total看类型4/7档，各取剩余分最小的档，同距取后者，没有可达档时为null），以及首关记录。status不读存档，只在reward_policy与`support_status[6].local_rules`中说明起点与不一致的处理口径。status的cooperative列出Enable≠0的协力大討伐（SubjugationType 1/2，不论成员是否在QuestTable中），每项给出mission_id、subjugation_type、registration（类型1为143、类型2为147，即载入该任务菜单时客户端自动发送的注册号）、window（enable、start_at、end_at、source）和state。传入now时去掉窗口已结束的任务，state为upcoming或open；不传时全部保留，state为null；readiness.subjugation按服务时钟传入now。已列入但读不出SubjugationType的行，以及读不出起止时间的协力行，以{mission_id, error}列入，同时ready为false。now须带时区，否则回503`quest_clock_invalid`。当前资源的17个协力任务Enable全为0，cooperative为空；一旦非空，玩家选中该任务就会陷入弹窗循环（143/147回501，客户端每次确认后原样重发）。

## 任务联动

服务启动时由GloryConditionConfig、SystemText、原生类型名表9888A0、QuestNameText标题与CardData为每条ConditionType2派生通关规格（`quest_clear_spec.py`），不按ConditionID分支：

1. 文本按SystemText行位置取，去掉颜色码与“ビギナー任務N：”首行；有10行Recode_Index与位置不同，不能按Recode_Index取文。
2. QuestType-2不限类型；QuestID非0时限定该关并核对其类型。
3. QuestID为0时，从“いずれかの…クリア”（“を”可省略，1169原文就缺）或“…を[COUNT]回クリア”取被包含的最长原生类型名，种类与标题读到“を”为止；字段为-2时据此收窄类型，与字段不同则保留字段并记kind_conflict。文本含“いずれかの”却取不到类型短语时记`kind_phrase_unresolved`，不放宽成不限关卡。
4. 文本带“の<后缀>”或“Lv.N”时，在该类型可开战关卡中按标题匹配；标题按各任务自己的QuestNameText读取，任一标题匹配即可。
5. 队伍：非中性字段只能是一个Genus或Assign，文本须为“<属性名>属性をN人編成で”或“<属性名>属性のみ編成で”，属性名等于本行GroupNameTextId；MemberCountCondition须有“[MEMBER]人以下編成”文本，按人数上限判定。

任一步失败或缺资源时该条unresolved，永不计数，并在任务状态`progress.clear_specs`中列出原因。当前资源229条全部解析：默认18、主线105、魔神77（各对应18关Lv标题）、塔22、日常1（10002→101/111/121/135/138）、训练1、历史1、帝国4（642–645文本写“緊急”，记kind_conflict）。队伍规则99条：3人以上3条、4人以上21条、8人以上9条、のみ66条。规格摘要计入任务content_version。

匹配顺序：规格unresolved直接不计；本次关卡类型的策略认领同类型规格时按规格判定类型、关卡集合与队伍；没有策略认领时，带类型的规格（字段给出或文本收窄）不计，不限类型的规格按规格判定关卡、人数上限与Genus/Assign队伍，PT100无QuestID的一步不计；没有规格的条目走共享规则——QuestType-2计任意关卡（PT100无QuestID的一步除外），QuestType0只计主线，QuestID为0或相等，队伍字段非中性不计。最后统一要求星级≥ClearRankCondition；HardClearRankCondition客户端不读，非0时仍须★4结果且星级达到。

quest_clear载荷增加`mission_id`、`team`、`score_mode`与`kills`；星级0只允许计分模式。team只含BL中GetIndex、UniqueID、CardID都与存档一致的单位，排除王子1/309；替身是4CF530复制的存档首个王子行（GetIndex在UX+1000～UX+1499，CardID与UniqueID同该王子），没有存档对应，访客、临时与固定队伍不在BL计数内。按用户决定，以下情况只写警告日志、不拒绝结算：对不上存档的BL行（替身除外）；不是王子副本的替身；固定队伍旁的替身超出原生数量（54E0A0只在Type10所选单位旁复制一个王子，其他类型的固定队伍没有替身）；HL与QuestUnitTable不符（CardID>10000取%10000，GetIndex自UX+3001按表序，AA=CostMod，行序不限；没有固定队伍的关卡HL应为空）。QuestUnitTable读不到时记一条无法核对，结算照常完成。委任76每次记录一次quest_clear，载荷带真实quest_type与归属任务mission_id，不带team与kills，队伍条件不成立；10002、10009与642–645因此可以经委任推进。PT3帝国页642–645按永久周期计数，54分页(3,3)可查询。10008由練習、10009由历史、642–645由帝国、魔神属性挑战由魔神规格计数，塔的22条属性编成挑战由塔规格计数；复刻、大討伐与神獣没有策略认领的规格，按不限类型的规格与共享规则计数，计分模式的载荷stars为0，只满足ClearRankCondition为0的条目。

## 事件任务奖励

`event_mission_rewards.EventMissionCatalog`按原生60FDB0只读加载EmcMissionConfig、RewardConfig与SpecialMissionRewardConfig，只用于开战与委任的判定和报告，不发放任何奖励；QuestService构造时建一份，委任经`quests.event_missions`共用。

- 条目：ID_Mission非0且不同于上一个条目号时开条目，已有的同号条目由后出现的行改写列；ID_Mission为0或与上一个条目号相同的行只把ID_Save/ID_ResultReward并入当前条目（当前EMC 18由这样的续行带上ID_Save 13）；第一个条目之前的行跳过。
- 组：ID_Reward非0且不同于上一个组头时开组，挂到按ID_Mission升序第一个列出它的条目，没有条目列出时丢弃；重复组头合并，SpecialMissionRewardConfig的Days/Time取最后一个组头行；ID_Incentive为0或不变时沿用当前奖励，奖励按奖励号排序；Type_Condition为0时沿用最近的非0值，不随组头重置。
- 查找按60FD30取ID_Mission升序第一个覆盖QuestID的条目，阶梯取该条目奖励表的第一个键，该组被其他条目先占时为空。单元格不是int32（ID_Item不是字符串）或缺列时整个目录回503`quest_mode_resource_invalid`，资源缺失回503`quest_asset_unavailable`；既不是计分类型、也不是分类1任务的开战不读这三张表。

奖励族分三类：score为计分类型（6/7/13）且阶梯含Type_Condition 4/6/7；emergency为开战任务分类1、且当前关阶梯含判定键（成员最小QuestID的条目Type_Quest为2时为5即EventParam，否则为9）；after_event为分类1任务的任一成员条目挂有带奖励行的SpecialMissionRewardConfig组。各模式在`ModePolicy.event_reward_families`上声明本模式结算时会发放的奖励族，默认为空；注册表要求它是frozenset、元素只能取这三个族名，否则回503`quest_mode_registry_invalid`；该属性不进入策略`status()`与`support_status`，守卫按关卡Type经`quest_modes.policy`读取声明。目前只有大討伐声明score（邮寄阶梯奖励），status的`settled`按QuestTable.Type列出各模式声明的族（`{"6": ["score"]}`），并计入content_version。77/78/81/82在任务准入（含模式check_start）之后、Boost与扣费之前，76在admission阶段准入之后调用`require_settled`；关卡所属模式没有声明的族都算未结算，出现时回501`quest_event_reward_unsupported`，不扣魅力、体力、券或道具，不写回执。精确重放与已处理请求在判定之前应答。当前资源score族16关都是Type 6，emergency与after_event为0，没有被拒的关卡。

ヒストリー与复刻的SpecialMissionRewardConfig组只在status的`not_promised`中报告，不发放：ヒストリー的奖励文本承诺的是交换所道具或掉落，复刻任务保留原活动的配布日期。只被非计分关、非紧急任务引用的阶梯（当前为EMC 27的组1，关卡4094–4101）结果页不显示，同样只报告。含条件10的未挂接组归等级奖励（cfgRnkbns），其余未挂接组不进入任何候选、预览或礼物。116不为这些配布回RET（116的RET只用于人気投票強襲挑战权折金的提示，见`docs/vote-login-refresh.md`），GrowthCampaign奖励侧不实现。`QuestService.status()`的`event_mission_rewards`给出资源指纹、content_version、行数、组挂接、各族关卡数、refused、not_promised与failures；`describe(quest_id)`给出关卡所属模式声明的族（`settled`）、条目、阶梯、SpecialMissionRewardConfig组，以及各任务窗口的族、其中未结算的族（`unsettled`）、判定键与最小成员条目，未知关卡回404`quest_not_found`。saves原因按关卡所属任务窗口的分类给出，关卡不属于任何任务窗口时describe的`not_promised`不含saves（阶梯原因不受此限）。

## 旧存档

active新增`mode`与`mission_id`。缺少mode的旧active只能是普通Type0，按普通难度结算；规则缺gold_per_event按0、缺max_gold_events按1000处理；缺mission_id时载荷为null。quest_state可带`pending_presents`（每项`{reward, copies}`，按完整奖励去重，说明与物品ID不超过256字节）与`mode_progress`（键为已注册策略名，值为对象，各项由所属策略在读取时严格校验），结构不符回503`quest_state_invalid`。settle钩子只能改写自己策略名下的进度项，形状不对回503`quest_mode_hook_invalid`，对quest_state的其他改动一律丢弃。mode与当前目录不符时拒绝结算。女神记录缺失时以已存GoddessRevivalTotalStarCount初始化。quest_state为schema 2，以最近256个已处理的开战与结算请求摘要（processed_keys）代替CT水位，旧schema 1读取时升级，见`docs/main-quest-backend.md`。

active另记`candidate_stars`（开战时该关记录的原始MaxRank）；mode只在boost>1时写第5个键boost，旧的4键mode按boost=1结算，5键且boost>1时boost_ignored必须为null。缺少candidate_stars的旧active只有全为无条件候选的关可以回退到静态候选，其余回503`quest_state_invalid`（只有4807–4813，此前不可开战）；存档候选缺少min_stars、max_stars与override_code时按默认值补齐后再比较；保存的started_at非法时回503`quest_state_invalid`（含★4结算；DropBonus的星期与关卡产票的发票组都按它取）。`mode_progress`另有`subjugation`项，按MissionID保存best与total，缺失时用本场之前的记录初始化，不补发历史奖励。Type 6记录的MaxRank为打包值，此前Type 6开战即回501，旧存档没有这类记录。战斗模式唯一新增的顶层存档域是`tower_state`，见`docs/tower.md`；塔服务不可用而存档已有该域时，`ServerServices.initialize`回503`tower_service_unavailable`。任务窗口规则会产生新的有效资源版本，旧版本下开始的场次结算回409`quest_snapshot_changed`，见`docs/mission-windows.md`；108常开的商店召唤关窗同样改变有效资源版本，见`docs/main-quest-backend.md`。

## 本地规则

| 规则 | 默认值 |
|---|---|
| GoldGet每次金币 | `gold_per_event` 100 |
| 每场GoldGet次数上限 | `max_gold_events` 1000 |
| 金币入账 | 以99,999,999封顶，超出部分丢弃，已高于上限的旧余额不变 |
| 金币口径 | 跟随公开资料（用户决定）：星级×1/1.2/1.5、InfluenceType 78/88能力各取最大（按图像替代卡取能力）、MoneyUp×1.5作用于基础加GG，整数有理运算一次向下取整 |
| 扣费时点 | 官方口径（id 9370）：失败、ギブアップ、リトライ不扣，83成功结算时按开战冻结的成本扣一次，Boost按×V；旧规则已扣的active不再扣、退出不退 |
| ★结晶Count≤0 | `first_three_star_crystals` 1枚 |
| 结晶溢出 | 整笔入礼物箱；礼物箱满时按份排队补投 |
| 物品超Stack | 丢弃并回IOF |
| Stack为0的物品（召唤券） | 直接入POSSESSION，不设上限（用户决定） |
| 编队与存档不符 | 只记日志，不拒绝结算，含替身与固定队伍核对（用户决定） |
| 委任76范围 | 审定集0/2/8/16/18中原生类型门为真、策略启用且没有未声明开战钩子覆写的Type；当前全部提供（用户决定），见`docs/entrust.md` |
| 平台码 | 0..31的int，否则400`quest_platform_invalid`，开战与委任同码 |
| Tag解析 | 全局32张任务表，MissionID最小者优先，开放与否都算；菜单门判开放时不加宽限 |
| Boost.V | 大討伐按`boost_policy`，默认total_only（用户决定）；其他模式接受并忽略 |
| 任务窗口 | 按资源原始时间；分类2/3/10/13可经`--mission-windows`按批次改写（用户决定），见`docs/mission-windows.md` |
| 日开放跨日宽限 | 300秒 |
| 任务队伍条件 | 只计存档中的BL真实单位，不含王子 |
| 曜日门槛 | 不设；任务顶部横幅与两条オーブ入口不查关卡，门户横幅的1004只在客户端（用户决定） |
| 交流菜单门槛 | 1003通关（9B76C0置灰交流按钮） |
| 挑战、大討伐、复刻、魔神、神獣、塔、練習、历史、帝国菜单门槛 | 1004通关（客户端的1004在入口；任务顶部横幅与各类型OnDecision不查关卡，但1004前到不了任务顶部，服务端1004为镜像） |
| 普通敌人PrizeEnemyDropPercent | 不读，按`drop_basis_points` |
| 条件掉落候选 | 按开战时记录的原始MaxRank选取并存candidate_stars，特殊候选与普通候选同率抽选 |
| 跳过分段的奖励行 | 1000–5999与≥9000不成为候选，计入excluded_prize_rows（用户决定） |
| 槽号大于5的候选 | 照常抽选，不发奖、不置位（用户决定） |
| 单场取得 | 最多7个，超出回409 |
| 練習エネミーテスト | 只受任务窗口约束（用户决定） |
| 練習通关 | 计入“ミッション又はクエストをクリア”类任意通关任务 |
| 历史公开条件 | 镜像CfgMissionHistoryTable的组公开与任务公开，未公开回409`quest_closed`，开战与委任都生效；不加Tag302门，不提供提前解锁（用户决定） |
| DropBonus | 按QuestID适用任意Type（用户决定）；83取开战、76取委任时刻的JST星期，每次请求汇总后取整一次（用户决定）；整表故障按无加成放行并报告（用户决定），行级故障只关该关 |
| 关卡产票 | 每次魅力÷15向下取整×次数，余数不累计；83按开战时刻所在的投票组发票（官方id 11237），76按执行时刻，见`docs/vote-events.md` |
| EventParam | 83每次结算改写，76只在有DropType5时改写，封顶1800（用户决定） |
| 大討伐超额击破 | `kills_policy` clamp（用户决定） |
| 大討伐首关 | 无开始按钮的首关拒绝开战，不合成记录（用户决定） |
| 大討伐奖励起点 | 逐项取水位与本场之前记录的较大值，没有水位取记录；已达到的档都不发，水位与记录不一致只在describe标出并写结算日志（用户决定） |
| 讨伐类型7档 | 只接受Sub22，或Param为1的Sub2/7/14，其余在建表时回503（用户决定） |
| 事件任务奖励 | 只有大討伐在`event_reward_families`中声明计分阶梯；其他族开战与委任都在扣费前回501，ヒストリー与复刻的活动后配布不发（用户决定） |
| 神獣Tag120菜单等级 | 镜像，经全局Tag解析，开战回`quest_rank_required` |
| 塔 | `TowerRules`，见`docs/tower.md` |
| 帝国入口Tag 801 | 镜像：须可显示且开放，否则quest_closed，开战与委任都生效（用户决定） |
| 交流带出现条件的配对行 | 回501 |
| 交流重复通关 | 不补写CardStatus.A7 |
| 女神总星 | 全部Type 19记录MaxRank之和（用户决定） |
| 女神档位 | 按服务端已发最高总星只发一次（用户决定） |

## 未完成与证据

以下只能靠实机或抓包确认，本地按上文规则执行：官方复刻排期（公开资料只给通则：复刻多在イベントヒストリー进行，魔神按周轮换，各任务具体排期无资料）；复刻207关与塔123关BonusType0/BonusNum0的首次★3结晶数（公开资料写复刻继承通关信息、初★3发1个结晶，塔首★3有结晶但每层数量无资料，现行first_three_star_crystals=1保持）；DropBonus的取整与聚合粒度、星期按开战还是结算取值、没有掉落刻水晶时是否回BONUS、76是否回BONUS；437覆盖奖励的官方掉率与官服写入的TreasureInfo；Stack为0的召唤券的官方入账通道；Type8委任的官方产出；塔的计分细节（王子人数与令牌不计已由公开资料定下，见`docs/tower.md`）、首访库存、官方HighScoreTeam的单位范围与顺序、HighScorePlayer是否为本人、QUEST=0的应答、RewardType 3神聖結晶在官服属免费还是付费；关卡产票的取整与余数、按表定还是实扣魅力、999时是否回IOF；大討伐Boost.V与超额击破的服务端效果、奖励的投递通道与文本、类型7多次跨越的拆分、首关记录与400183/400212的复刻排期，以及菜单能否发出Boost.V=3；协力大討伐143的DefeatCount；Type13強襲与Type7计分模式；GrowthCampaign期间的战斗券掉落；战斗结算中的ASSIGNBONUS、PRI、TowerDefenceCampaignRewardParam与LetsTDRewardParam。

实机验收还需确认显示：结果页的票图标与数字、83/76的BONUS横幅与委任结果页的刻水晶数量、IOF丢弃提示、塔门户与TowerResult及HighScore面板、大討伐结果页的打包分数与奖励列表（含首关不写记录时的任务分数）、资源补丁后复刻与大討伐入口是否出现、神獣三组编成时BL的形状与Tag120门的菜单表现。胜负与击破数由客户端上报。★4弹窗、礼物箱文案、战术指南固定队伍、任务队伍条件与各模式菜单未经实机验收。

合同与反编译证据位于`aigis_research/battle-modes-20261102`的settlement-contract、hard-mode-boost、devil-raid、permanent-modes、reproduce-harlem、blocked-economy-missions、entrust-scope、foundation-review、raid-cumulative-contract.md及cumulative-rewards-contract.md（事件任务奖励），任务顶部横幅与五类1004镜像见同目录的permanent-modes/mission-top-entry-gates.md，曜日门槛、塔高分与槽号的依据见`aigis_research/permanent-20261101/menu-tower-drop-contract.md`。定向测试为test_quest_modes、test_mission_tags、test_quest_clear_spec、test_quest_settlement、test_quest_score_settlement、test_quest_catalog、test_quest_entries、test_drop_bonus、test_battle_events、test_event_mission_rewards、test_quest_protocol、test_quest_star_rewards、test_quest_battle_modes、test_mission_state、test_present_backlog、test_history_disclosure、test_subjugation_rules、test_tower_catalog、test_tower_state、test_tower_service、test_mission_window_revival、test_vote_quest_tickets、各`test_quest_mode_*`及主线、委任、任务相关文件，使用真实公共主表与资源；未运行无关全量测试。
