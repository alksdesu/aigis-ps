# 投票活动

55号目录、57号投票交易以及137号票史与当日票刷新已接入。目录来自第16届资源文案，明确作为本地复刻配置使用。A/B/C及决胜组可完成扣票、累计、奖励入箱及实际领取，已接通晋级冻结、结果快照、普通票到期兑换金币邮件、最终截止后未用强袭挑战权折金和最多投票角色料理回赠；关卡产票已接入战斗与委任结算，默认历史窗口下不产票；强袭战斗仍待实现，原生界面尚未验收。

## 目录与配置

当前目录解析四组取票、投票和结果时间，A/B/C共956个候选名称，以及52个固定奖励点和4条重复奖励规则。938名候选可按名称和稀有度唯一对应CardID；默认`candidate_policy=present_template`对另18名采用唯一被原生正常发货模板引用的卡号，全部956名可用，A/B/C分别318/319/319。每个选择保留备选和PresentID证据，不冒充官服VCC名单。`strict`模式仍保留18名待明确；原生模板缺失或多个备选均有原生模板时也保持未决，进入展示期返回`vote_candidates_unresolved`，不发送残缺名单。

身份证据只统计原生PresentUnitData行。有效资源视图追加的四个本地初始角色模板（见`docs/local-resources.md`）只是发货途径，不证明官服发放过该卡，因此不计入；本地行须PresentID与CardID同时符合`LOCAL_TEMPLATE_CARDS`，保留ID指向其他卡时仍算原生证据，`status.local_resolution.excluded_present_ids`列出实际排除的本地PresentID。服务端的有效视图与原始资源因此选出同一组卡号，其中闇エルフの女王ドロテア、一目連、揺るがぬ黒き騎士デシウス分别为408、1783、2752。奖励单位由唯一名称确定卡号，模板只决定发货途径，不受这一排除影响；当前奖励卡都没有本地模板，奖励行与原始资源一致。目录内容版本仍只由资源哈希、本地规则和固定策略名构成，这一证据口径不改变该版本；由它派生的关卡产票、委任及登录领奖内容摘要不变，已存回执保持可重放。存在未决候选时投票、晋级冻结、当日票和关卡产票都会拒绝或跳过，不会留下依赖未决结果的存档记录。

服务参数为`--vote-rules`，启动脚本参数为`-VoteRulesFile`。默认示例为`config/vote-rules.example.json`，固定复刻示例为`config/vote-revival.example.json`。`revival_start=null`保留2026年8月至9月的原始历史窗口，所以当前默认没有开放投票；显式复刻以带时区的ISO秒级时间统一平移全部组窗口。`candidate_overrides`优先于默认模板策略，按完整角色名指定其备选CardID，只接受该名字及稀有度实际匹配的卡，不自动取最小编号。

原文的结束分钟转换为下一分钟的排他端点，响应使用JST的`YYYY/MM/DD HH:MM:SS`。取票和投票窗口分别保留，换组后旧组仍有一小时投票时间；决胜取票比正式投票早两小时。起止时间不会在每次启动时自动重置。

本地配置默认等级门槛1、每角色每组上限999、每15票增加一次强袭资格、结果展示7天，技强化圣灵使用模板6。投票还要求主线1004有通关星数，与菜单入口一致。等级及展示期是本地政策，不声称来自官服55配置。

## 原生响应

55输出SYSTEM、VEC事件、VGC分组、VCC候选和VRC奖励配置。活动关闭时输出四个有类型的空配置表，配置接收器会替换旧内容。候选Count使用当前本地玩家按对应票据保存的累计值，明确是单机统计，不伪造官服全服票数。

VRC中单位由非零CardID及成长字段表示；物品由GiftType0、非空ItemID及Amount表示。内部52个固定点和4条重复规则合并为52个原生奖励行：ABC最后的Count200、决胜Count300均带Repeat100。Repeat是循环票数间隔，不是布尔值，也不是额外一份同档奖励。原生57的同名VRC是本次已发奖励展示；菜单GetVoteReward不另发领取请求。

55和57的ASSAULT读取已保存的强袭资格；Next按当前投票组的累计票数计算，重叠窗口取较新投票组显示。次数保留到后续强袭战斗消费，不以显示成功代替开战实现；最终截止后已结算折金的次数从Remain扣除，pending期间仍按原次数显示。

VEC总活动窗覆盖至结果展示结束，公告子窗只覆盖最后投票截止至最终结果公布之间的等待期，结果窗独立设置。原生普通投票入口要求非公告期，结果入口同时要求总活动窗和结果窗有效，所以这三类时间不能复用。此调整只改变响应投影，投票仍由VGC组窗口严格限制，旧目录与奖励指纹保持稳定。

55、57和137在观察到组投票截止后冻结排名；55只保存排名，不暗中变更库存或发邮件。A/B/C各取前十且包含第十名所有同票者晋级；完全零票也按同票处理，因此单人未投票复刻最多有956名决胜候选。决胜候选另加GroupID4行，原ABC票数保持冻结，票史按决胜票种独立累计。最终结果也保存快照，排序为票数降序、CardID升序，不声称是官服全服排名。

`vote_finalists`版本1保存源摘要、候选定义、原结算截止、实际首次观察时间、全候选票数及晋级名单。离线跨过截止时以首次观察到的现存PVH建立基线，不伪造历史冻结时间。改复刻日期不能解冻或再投已冻结组；冻结票数与PVH不一致会明确报错。默认历史窗口下，从未参加且没有快照的空存档不会自动冻结，保留以后首次显式复刻的机会。原生界面对结束后全零票组仍显示“统计中”，这是现有客户端判断，不通过造票绕过。

## 投票、奖励和当日票

57严格接受GID、CID和CNT。验证组窗口、候选、1004通关、等级、每角色上限及库存后，优先消耗至多一张有效当日票，再扣该组普通票。固定门槛和循环奖按本组所有角色累计票数判断：ABC在200后每100票追加，决胜在300后每100票追加。决胜资格独立核对前三组冻结名单，不把动态名单写入旧奖励定义。

扣票、PVH、所有本次奖励邮件、永久领奖记录、强袭资格及回执在同一次CAS内提交。任何奖励、邮箱容量或响应序列化失败都会整笔回滚，不先扣票。资产在领取邮件后到账；57的VRC只展示本次已经入箱的奖励，不再另发一次。单位的技能和阶段显示与现有统一初始化一致，模板技能0实际归一为1。

`vote_rewards`版本1按票据保存旧累计基线、冻结定义、各奖点的邮件ID与展示快照。旧累计不追补奖励；每组跨过15票整数倍增加强袭次数，旧基线不补次数。全活动剩余次数上限999，溢出明确记录为discarded；spent预留给后续实际战斗。改复刻时间不清历史、不重新发奖；修改候选或奖励定义后保留旧记录，新投票被拒直到用控制台的`votes.adopt_reward_definition`（确认短语`MIGRATE VOTE REWARDS`，自动备份）采用当前定义：已发记录保留、不追补。

当日票为`item_10000000`。实际观察到的有效活动日发一张，JST04日切，缺席日期不补；切组和同日改窗不重新发。115、116、137与57共用同一日票账本，115/116在新请求的原领奖事务内刷新并返回完整POSSESSION；消耗或过期的旧票明确返回0，保留其它库存的Enabled。115/116/57的非空POSSESSION恒带Enabled（缺省行为0）；存档中只有两列的旧回执重放时先按三列、再按旧两列规则重建比对，任一与存档字节相同即原样返回。原生日检已有115调用链，菜单转换触发该链时可刷新，不声称所有界面在04:00瞬间实时更新。55不接收POSSESSION，所以只允许冻结排名，不能通过55暗中发票期待客户端刷新。

旧存档已有一张日票而无日期账本时，有效活动内将它作为当日基线，不额外加票；活动外保留并报告日期未知，不猜测后删除。已有日期证据的旧日票跨日过期。`item_replacement_vote_ticket`是强袭奖励按当前组替换的占位符，与当日库存不同。

普通组票按资源原文每张兑换50金币，通过一封金币邮件入箱；原文的具体维护时刻不可得，本地采用组投票结束后的首次137或57观察。先冻结余额与兑换定义，整组扣票及邮件一次提交。邮箱满时保存pending且不扣票，释放容量后再试；即使关闭活动或移动复刻日期，已取得的pending资格仍继续处理。57如同时结算旧组票，会返回完整POSSESSION及邮件快照，防止客户端残留旧票余额。

`vote_expiry`版本1永久保存资格时间、票数、比例、状态和邮件ID。未参加且无库存行的零票不制造记录；参与后零票记closed。pending、settled和closed均不允许复用该票种重开投票；已结算票重新出现非零库存时明确报错，用控制台的`votes.reopen_settled`迁移（确认短语`MIGRATE <n> VOTE TICKET(S)`，自动备份）：旧结算及其116通知移入归档（永久可读），重新出现的库存在下一次期满观察（137或57）时作为新实例折算，命令效果里的`converts_on_next_observation`标明会不会折算；当前没有投票组使用该票种时库存保留、不折算，命令给出警告；一起重现的券必须在同一次命令里一起重开，否则重启检查拒绝提交。已领走邮件不删除结算证明。只为当前四组创建新兑换资格，不批量猜测其他历史票种。116的`ItemExchange.Gold`仅控制兑换提示弹窗（SystemText 4651，大于0才显示，文案不含金额），不承担领取确认；现于折算提交后的第一次新116下发一次，值为尚未提示的已结算折算金币之和，没有新折算不发该组，提示证明另存于`vote_expiry_notifications`，规则见`docs/vote-login-refresh.md`。

强袭挑战权折金由`vote_assault_expiry`版本1保存。活动最终投票截止后的第一次137或57观察（与普通票折金同一规则；截止后57在观察前即以`vote_inactive`拒绝，实际由下一次137触发；55只读不折算）把本活动未使用的挑战权按(earned−spent)×3000G合成一封金币邮件，描述为本地文案「強襲ミッション挑戦権変換」。比例从SystemTextAssaultInfomation「挑戦権1回につき3000Gへと変換されます。」校验，文案变化时投票事务返回503。记录冻结earned、spent、数量、比例、原文与复刻截止时间、观察与结算时间及邮件ID；满箱时pending并在后续观察重试，规则关闭时不创建新折算，但已有pending照常重试；剩余为零不制造记录；已领走的邮件不删除结算证明，领取仍按金币上限规则。折算后该活动不再开放投票（409 `vote_assault_closed`），earned或spent再变化返回503。此折算不进入普通票`vote_expiry`的折算列表，也不触发`ItemExchange`提示；折金结算后的第一次新116下发一次`RET(msid)`提示（6996B0对Type_Quest 7弹出SystemText 4585），116版本见`docs/rank-rewards.md`。目前尚无战斗消费，spent恒为0。

存档已有`vote_expiry_notifications`、`vote_assault_expiry`或`vote_assault_notifications`时，未装配投票交易服务与`vote_expiry`等交易领域一样使初始化返回503 `vote_transactions_unavailable`，投票服务整体缺席时回503 `vote_service_unavailable`；交易服务已装配而对应提示服务缺失时，分别回503 `vote_expiry_notifications_unavailable`或`vote_assault_notifications_unavailable`，不丢弃提示证明。

## 关卡产票

83开战结算与76委任经共享的战斗活动挂点`BattleEventRewards`产票（见`docs/battle-modes.md`），`VoteQuestTickets`是唯一的票源提供者：

- 比例与上限从SystemTextVoteInfomation与SystemTextVoteInfomation5（第1425/1421行）的文案用正则解析，当前为每15魅力1张、持有上限999；各组票的Stack必须等于999，文案或Stack不符时提供者构造失败。
- 每次成功结算发runs×floor(QuestTable魅力/15)张：83的runs为本场Boost次数，76为委任次数；零魅力不发（zero_charisma），不足一档不发（below_rate），余数不累计。
- 发票窗口为各组散布期的半开区间，按GroupID升序取第一个覆盖判定时刻的组，含复刻平移：83的判定时刻是开战时刻（active.started_at；官方aigis1000.jp id 11237：跨期间的通关按开始挑战时所在的期间发票），76委任按执行时刻。挂点status（readiness.main_quests.event_rewards）的rules.ticket_clock为`result_attempt_start_entrust_execution`，不进内容版本。
- 前提是投票规则enabled，且窗口开放时没有未决候选；不可发原因的判定顺序为disabled→no_window→candidates_unresolved→vote_ticket_closed→zero_charisma→below_rate/issued。该组已有`vote_expiry`记录（pending、settled或closed）时为vote_ticket_closed，不再发该票，战斗照常完成，因为已冻结的到期记录不能再增加库存。
- 票与掉落合并为唯一一次`credit_capped`，超过999的部分丢弃并回IOF；拆分IOF时溢出先记在票上。
- 默认`revival_start=null`时第16届窗口已结束，报no_window，所以当前不产票。当前资源在默认策略下没有未决候选；改用`strict`或资源变化后出现未决时，复刻前用`candidate_overrides`指定，否则既不能投票也不产票。

journal history的event_rewards回执含schema、content_version、context（now精确到秒：83记开战时刻，76记执行时刻）、items、vote_tickets{error, item_id, count, reason}、event_param{rewritten, points, previous, value, changed}与discarded{vote_tickets}。提供者未挂接时reason为unavailable并附错误码：默认`vote_quest_tickets_not_attached`，votes.transactions缺失时为`vote_quest_ticket_dependencies_unavailable`，提供者构造失败时为其自身的码（如`vote_quest_ticket_definition_invalid`）；这些情况下主线与委任照常可用，只是不产票，readiness.main_quests.event_rewards.vote_tickets报{attached, error, status}。挂点的内容版本随之记下缺席原因码并嵌在委任76的回执摘要里：未挂接、依赖不可用、提供者自身的三种构造失败，以及构造期原样传出的`vote_catalog_changed`、`vote_candidates_unresolved`与`vote_expiry_dependencies_changed`（合为`battle_events.ABSENT_PROVIDER_ERRORS`）下提交的委任回执，在提供者之后挂接时照常重放；其他原因（例如构造期传出礼物箱的就绪码）下提交的回执回409 `entrust_retry_stale`，见`entrust.md`。

提供者的错误码：503`vote_quest_ticket_source_invalid`、`vote_quest_ticket_definition_invalid`、`vote_quest_ticket_dependencies_changed`、`vote_quest_ticket_request_invalid`、`vote_quest_ticket_clock_invalid`；人工发票`grant(state, item_id, count)`只接受本届组票与正整数数量，否则400`vote_quest_ticket_item_invalid`或`vote_quest_ticket_count_invalid`，遇到已关闭的组回409`vote_ticket_closed`，再经一次`credit_capped`入账。`status(now=None, *, state=None)`给出ready、error、content_version、rate、tickets_per_rate、cap、text_rows、source_hashes、groups（含源散布期）、now、current_group、unavailable_reason、state_error、policy与native_tested=false；不传state时报不出vote_ticket_closed。

## 票史与兼容

PVH保留小写五列`key/cardId/groupId/count/itemId`，key为`CRC32(ItemID + ':' + CardID)`的有符号32位值。客户端按key增量更新，空PVH不会清旧历史；137发完整保存快照。新建PVH行的itemId不超过原生单元255字节，已存256字节的行仍可读取和累加。

137在投票目录或交易依赖未就绪时仍返回已存配置（存档PVH与库存），不刷新当日票，并在启动诊断记为degraded；55和57照常拒绝。观察或预检失败（如存档损坏、`vote_provider_inconsistent`）和三次CAS冲突仍拒绝并记为refused。137不校验单位卡片引用。其他写入接口提交前预检137回读时，投票目录未就绪同样按存档PVH处理，每次预检记一条WARNING（`Startup state was served outside a recorded load`），不再因此拒绝写入；55、57以及投票票券和料理的发放仍由各自的就绪检查拒绝。活动通行证依赖未就绪时，137与这些预检同样按存档EventPassStatus降级，见`event-passes.md`。

`vote_state`版本1逐行保存固定定义摘要、旧基线及绝对累计，与原始PVH一一核对。旧合法非空票史建立基线，不追补奖励；旧空票史保持空。更新不能降低累计、换票据归属组或跨定义覆盖；重复键、CRC碰撞、非法字段和未知版本均拒绝。后续控制台应使用同一校验与事务入口，不能删除基线来重领。

CLI安装票史provider。没有对应服务却存在非空票史或领域记录时，初始化明确失败并保留数据。料理回赠增加独立的角色料理目录和Type21支持；保留旧恢复料理目录及登录领奖版本1至5的内容指纹，新增登录版本6承载投票料理通知。

57保留16份精确回执和256个已处理请求摘要，客户端CT只作墙钟，不参与排序，时钟回拨后的新投票照常受理。相同请求在提交后的同一修订版原样返回，不读取新时钟；领取邮件、冻结排名或跨日刷新等改变存档后，旧包返回409而不再次扣发；回执被淘汰但摘要仍在时回409 `vote_request_stale`。旧水位格式在读取时升级，旧回执缺少的料理定义按无料理读取；升级前已被挤出且不在同秒集合内的旧请求不再算已处理。57的拒绝都以WARNING记录（`Vote 57 refused with <code>`）。永久领奖证明不依赖邮件仍在箱中；领域账本被移除时不会自动重建成无领奖的旧档基线。决胜回执必须保留三组晋级快照及被投角色的入围证明，删除全部或部分快照会拒绝加载；验证旧回执只使用原冻结证明，不强制套用当前源定义。称号目录接入后又移除时，按含称号的道具定义版本提交的57回执无法重算摘要，同样回409 `vote_retry_stale`。交易能力使用新内容摘要，并保留此前ABC交易摘要的精确重试路径，不为重试执行新折算或冻结。交易内容摘要版本4在版本3（未接入料理服务时即版本2）之后加入强袭挑战权折金及其57/137观察点。57回执按提交时的内容摘要精确重放：版本1、2，以及版本3、4在接入与未接入料理服务时各自的摘要都保留重试路径，之后接入料理服务不会让此前未接料理时提交的回执变成409；摘要嵌入的投票奖励版本另按其可达版本核对，称号目录后接入改变道具定义的版本时，这些摘要同样可以重放。不属于任何已提交配置的摘要仍回409 `vote_retry_stale`。移除料理服务后，按料理摘要提交的回执无法重算摘要，返回409；存档已有料理进度或回执记录了料理定义时以503拒绝。

## 最多投票角色的料理回赠

客户端可以确认料理角色、名称、到货通知及领取后的恢复效果，但没有官服角色选择和发件资格算法。因此以下规则明确属于本地实现：每组独立统计功能启用后成功提交的新投票，最低1票、最多票角色优先，同票取较小CardID，每组发1份；门槛和份数可通过`--vote-cooking-rules`或启动脚本`-VoteCookingRulesFile`配置，示例见`config/vote-cooking-rules.example.json`。最低票数范围1至999，份数1至10。

首次新57把已有PVH保存为基线，不追补旧历史；关闭料理期间的新票只增加基线，不抵消此前已取得的新票。ABC与决胜使用不同票种身份，即使同一角色也分别计算。每组捕获首次投票时的结果开始时间，之后首次115或116观察到该时刻才冻结赢家；每个请求至多投递一组，其他已达时的组保留待发。两接口共用发件证明，137及目录读取不提前发料理。

`vote_cooking`版本1保存每组原始票数基线、当前PVH、原窗口、源目录及料理资源摘要、门槛、份数、赢家、状态和邮件证明。冻结参数会重算定义摘要，字段损坏时拒绝发件。邮箱满时整组保留pending，不发半份；已领取的邮件仍保留永久证明。关闭配置、提高门槛或调整复刻日期不撤销旧资格，也不重复发件。修改已跟踪组的门槛、份数或内容定义后，新投票会明确要求迁移：料理定义用`votes.adopt_cooking_definition`（确认短语`MIGRATE VOTE COOKING`，自动备份）采用当前定义、已发记录保留不追补，已存投票的组定义用`votes.adopt_vote_definition`（按票种，`MIGRATE VOTE DEFINITION`）改采当前投票组，并一并重冻结全部排名快照；冻结排名与当前候选或计票对不上时回409 `vote_finalists_refreeze_refused`，票数历史保留；不能通过改配置悄悄重定义已记录的票。资源版本变化时待发料理保留`vote_cooking_recipe_changed`，用`votes.adopt_cooking_definition`按当前料理定义立即投递，邮箱放不下的组按当前定义继续待发、由115重试；定义未变、只因邮箱满而待发的组不属迁移，该命令回409 `vote_cooking_adopt_unneeded`，由115自然重试。

发件采用Type21、Content为纯CardID、高位形态0，每份独立一封。2930张主表角色可映射HomeCooking 0至10，956名投票候选全部有名称与料理映射；HomeCooking 0是有效的爱心便当。当前客户端Type23专用路径在预检分支缺失，不能正常从列表点击领取，因此Type21是有静态依据的本地发件方式，不声称官服也使用此类型。无需修改原版客户端。

通知使用`VoteGiftParam(CardId:I, Count:I, ReceiveTextID:I)`，文本为SystemText物理行号5006，邮件说明来自5005。进一步追踪确认实际弹窗读取位于116之后：115能够接收通知表，不等于已经显示。新增独立`vote_cooking_notifications`保存通知交接，116每次至多取一个已发未通知组；即使邮件已在115发出或已被领取，也只补通知，不重新发邮件。后续新116返回同列空表，避免旧缓存重复弹窗。116发件、通知记录和回执同事务，编码失败全部回滚。

115与116的新领奖版本7包含日票观察，116还保存本次新发料理、本次通知及累计通知证明数量。cli默认装配LetsTD（开关全关），因此115新写入列表式版本8，固定部分不变，`letstd_grants`为空列表，回包子项不变，只有LetsTD不可用时才写版本7；116在此之后还接入普通票与强袭挑战权折金提示，默认写版本10，见`docs/rank-rewards.md`。后续空通知请求仍保留永久证明数量，不能删通知域后重弹。保留两个接口原版本1至6的原内容摘要和精确重试字节；旧包不读取新时钟、不额外刷新日票或补发料理。115继续发送其既有通知表，但是否显示以116交接记录和原生消费链为准；服务端通知已提交也不等于用户已实际看到画面。

原生领取使用1，随后42刷新礼物箱。服务端按权威邮件结算自然恢复，再增加一份当前最大魅力，遵循既有默认2000上限；Type21仅允许逐封领取，批量和混合请求拒绝。Content是CardID，绝不作为恢复倍数。投票料理不增加普通登录料理的CC/CD或月额度。领取后的精确重试不重复恢复，邮件删除后仍能验证投票发放凭据。

原生与资源证据见`aigis_research/events-20261101/vote-cooking-contract.md`、`vote-cooking-resources.md`和`vote-ticket-login-contract.md`；料理发放的原验证汇总保留在`artifacts/vote-cooking-validation.json`。本轮登录日票及通知交接见`docs/vote-login-refresh.md`和`artifacts/vote-login-validation.json`。后台接口链验证与原生画面验收分开，尚未启动游戏验证料理通知及点击表现。

## 剩余实现

- 强袭次数消费和战斗结算。
- 原生日检覆盖界面、料理通知及领取、全零票组结束显示和116兑换提示弹窗的实机验收。

关卡票源已核对：原文支持成功通关每15魅力一票、零消耗不发、999溢出舍弃；余数与跨窗结算按上文的规则执行（余数不累计为本地规则；83按开战时刻判定，依据官方id 11237；76按执行时刻判定），官服的取整、按表定还是实扣魅力、999时是否回IOF仍待实机确认。当前QuestTable缺少全部15条强袭关卡定义，第16届对应25000；不能只凭清单和奖励组就宣称可开战。完整来源与后续结算边界见`aigis_research/events-20261101/vote-ticket-sources.md`。

资源与原生证据分别见`aigis_research/events-20261101/vote-resources.md`、`vote-contract.md`、`vote-candidate-resolution.md`及`vote-results-contract.md`。首次目录148项、ABC交易201项的历史结果分别保留在`artifacts/vote-validation.json`和`artifacts/vote-transactions-validation.json`。

本次决胜与到期结算完成219项去重后的定向检查，见`artifacts/vote-closing-validation.json`；涵盖48项到期兑换、38项排名与永久资格证明、10项决胜奖励校验、15项收尾集成、33项旧交易、24项目录接口和51项奖励回归。全部计数来自真实JUnit逐项记录，不是将多轮重复执行相加，也未重跑全项目测试。原客户端、正式存档和IDA数据库的大小、修改时间与SHA256均未变；没有启动游戏、实际服务或采集器，原生界面尚未验收。
