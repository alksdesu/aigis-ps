# LetsTD本地模拟

`letstd_rules.py`、`letstd_catalog.py`、`letstd_state.py`与`letstd_service.py`把LetsTD跨游戏奖励做成明确标注的本地模拟合作方活动，由115与116共用一个服务投递（`login_reward_service.py`、`rank_rewards.py`），cli.py、config_registry.py与start-server.ps1已接入。它不验证任何真实跨游戏账号、合作方进度或购买，不收集也不伪造合作方身份，不声称与合作方游戏联动；status固定报告`simulated: true`、`native_tested: false`。LetsTD没有自己的协议编号，serve不为它新增处理器。默认六个开关全关，此时115/116回包的子项与不装配LetsTD时完全相同。

## 资源来源

- 经有效资源视图读取`LetsTDRewards.atb`，逐列核对13列类型（ConfigId、StartDate、EndDate、ServiceType、RewardGroupId、RewardId、ItemType、CardId、GeneralItemID、Count、SystemTextID、ReceiveTextIdSingle、ReceiveTextIdMulti），标题取`SystemText.atb`中SystemTextID所指的行。ServiceType只能是三家合作方，组只能是1或2，ConfigId与(合作方, 组, RewardId)都须唯一；任何一项不合时整表拒绝（503 `letstd_catalog_invalid`），LetsTD不装配。
- 合作方按原生顺序为oshirore（城プロRE）、monmusutd（モンスター娘TD）、rlyeh（ルルイエ少女隊）。组1即newChallenge，标题「タワーディフェンスやろうぜ！キャンペーン報酬(合作方名)」；组2即newPurchase，标题「やろうぜパック購入特典(合作方名)」；资源里的标题都分成两行。
- 当前资源共105行，每家35行：组1共25行（ItemType 1/2/3/4分别12/9/3/1行），组2共10行（ItemType 1/2分别7/3行，RewardId 7缺号）。StartDate为2025/08/05 16:00（69行）、2026/04/02 11:30（9行）、2026/09/18 11:30（27行），EndDate全部为空。

## 原生子项与显示条件

- 115/116可以带LetsTDRewardParam（客户端据此重载CLetsTDRewardsTable并重建一次性的通知实体）和TowerDefenceCampaignRewardParam（ServiceId:S、Count:I，合作方分发神聖結晶的通知）。LetsTD的通知只在115/116流程末尾显示，实体用一次即销毁。弹窗标题由LetsTDCPTitleFormat套入合作方名组成，每家合作方的弹窗依次列出结晶、金币、一般物品和单位，每个弹窗4行。战斗结算回包也登记了这两个子项；战斗与委任回包都不下发它们。
- 不下发TowerDefenceCampaignRewardParam：资源里没有分发数量，rlyeh也没有对应文本；一旦下发，之后每个115/116都要带零行表。零行与省略只在同一会话先下发过非空表时才可区分（接收器收到时清空并重建映射，读者只复制不清空，省略时映射保留上次内容），本地从不下发，所以同样不可观察，不需要采集官服115/116。也不设置LetsTDServiceIds，不改动LetsTDRewards.atb。
- native_popup关闭（默认）时不带LetsTDRewardParam，奖励只以邮件出现。打开后，本次有新投递的每家合作方一行，列为ServiceId:S、newChallenge:S、newPurchase:S，后两列是本次新投递的RewardId（升序、逗号分隔，该组无新投递时为空串），作为115回包的最后一个子项追加；116中它后面只可能再有普通票折金的ItemExchange与强袭挑战权折金的RET。没有新投递时省略整个子项。省略与零行表在客户端不可区分：引用通知实体的只有91C290（据表重建）、6654E0（弹窗读取）与663C50（流程末尾显示后销毁），省略时不重建、零行时重建出空实体，两者都不弹窗；按客户端不可观察结案，不需要采集官服115/116（静态依据见`aigis_research/console-20261102/client-timing.md`第5节）。

## native_popup为什么默认关闭

原生弹窗文案来自客户端资源，加不上本地标签，打开后游戏显示的通知与真实联动没有区别。三家六组全开且打开native_popup时，一次115可能连续弹出十几个模态框，因此默认关闭，并建议一组一组开启。

## 奖励映射

- 奖励只走礼物邮件。Description为「【ローカル模擬】」加该行标题（多行合并为一行），例如「【ローカル模擬】タワーディフェンスやろうぜ！キャンペーン報酬(城プロRE)」。标签写死在代码里，不能通过配置去掉。
- ItemType 1：Type22物品邮件，ItemID取GeneralItemID。召唤券经礼物箱内的物品券服务校验，按张拆成每张一封；当前57行中48行是召唤券（池338、341、343、344、426、461、463），其余9行是gift_0003_diamond、gift_0103_ring与trade_item_005000。
- ItemType 2：单位邮件，CardId按初始模板映射（97→24、136→62、250→173、397→375、1269→1043），Count固定为1。
- ItemType 3：免费神聖結晶邮件（每行5或7个）。ItemType 4：金币邮件（每行5,000,000）。领取时仍按礼物箱的现有上限检查。

## 开关与一次性

- 规则文件是`{"schema_version": 1, "native_popup": false, "partners": {合作方: {"newChallenge": 布尔, "newPurchase": 布尔}}}`：三家各两组共6个开关加native_popup，全部默认关，没写出的开关视为关。文件不超过8 KiB，未知字段、重复键、NaN/Infinity或溢出成无穷的数字、嵌套超过64层，或schema_version不是1时拒绝加载。
- 开关打开后，第一次115或116结算时武装该组：冻结当时的行集和每行定义摘要，并记下规则版本、资源sha256与`source: local_simulation`。每个RewardId永久最多投递一次。
- 关闭开关即暂停该组，连同其中的pending一起暂停；已发奖励不收回。重新打开后继续投未投递的行，不重发。资源新增的行，以及定义变化且尚未投递的行，要在控制台执行需要确认短语的`letstd.rearm`才会并入；已投递行的定义变化只标记needs_adaptation，不重发。
- 115与116共用同一个账本，先到先发，在同一次CAS里提交。115里LetsTD排在每日投票票观察之后；116里排在料理、折金与挑战权提示之后，容量预检已计入这些提示。
- StartDate、EndDate按JST解释（客户端不读这两个字段，这是假设）。StartDate晚于当前时间的行记为scheduled，到时再投；EndDate为空表示没有截止，非空且已过的行记为ended，不投递。
- 投递粒度是单个RewardId：先整批预检提交，失败再逐行回退，结果与纯逐行投递相同。放不下的行转pending并记原因：容量类原因（邮箱已满或礼物ID用尽、回包过大、账本已满）每次结算都重试；其他原因（如召唤券池未启用`summon_ticket_pool_disabled`、奖励类型不支持）只在依赖指纹变化后重试。依赖指纹包含目录、礼物模板、物品目录与物品券服务，不含开关和native_popup。

## 默认可投数量

按当前默认启用的44个物品券池，六组全开时105行全部投递，共114封邮件（Type22物品66封、单位36封、免费神聖結晶9封、金币3封），0行pending。用`--item-ticket-rules`显式去掉上述券池时，对应的券行转pending，池重新启用后在下一次115/116自动补发。

首次结算全部105行需要数秒：空邮箱时115约8秒、116约9秒，邮箱只剩约60格时可能超过30秒。客户端的时间上限是静态确定的：没有应用层看门狗，WinINet只设了连接超时10秒（重连2次、退避5秒），接收与发送沿用默认的30秒；30秒以内正常收到应答，没有任何超时或重试；超过30秒时发送调用因接收超时失败，弹「通信エラー…番号：-4」模态框，玩家确认后原包重发，按回执精确重放，不会重复投递。初始化之后没有“3次×5秒静默重试”（那只是PE初值，游戏初始化把重试次数清为0）。

## 账本、回执与存档

- 顶层`letstd`账本只在某组开关打开、并且之后来了一次115/116时才出现。
- 装配了LetsTD时，`login_reward_delivery`与`rank_reward_delivery`写列表式版本8（`sections`显式列出各节并带`letstd_grants`；116接入折金与挑战权提示后写版本9、10，同样带这一节），开关全关时也是如此，`letstd_grants`为空列表，回包子项不变；版本1至7的旧回执照旧逐字节重放。未装配LetsTD时115继续写版本1至7；116在接入折金或挑战权提示时仍写版本9、10，只是`sections`里没有`letstd_grants`。
- 规则热切换后，进行中的115/116精确重放回409 `*_retry_stale`；initialize、月度刷新与80/83/76不受影响。
- LetsTD分节的内容版本由`content_version_for`按礼物箱配置求值，只取道具定义与道具券服务：道具券服务在115回执提交之后才接入（116的基础摘要必需道具券，缺道具券时没有116服务），或称号目录后接入改变道具定义的版本时，旧回执照常逐字节重放；没有道具定义的配置当时不会有LetsTD服务。可重放的组合与409边界见`login-rewards.md`与`rank-rewards.md`。
- 启动时服务存在则校验账本，损坏时回503 `letstd_state_invalid`。服务不可用时，存档里有`letstd`账本，或最近一次115/116标记里有非空`letstd_grants`，启动按503 `letstd_unavailable`拒绝，不丢弃记录；没有这些记录的存档照常载入，包括开关全关时写下的版本8标记（`letstd_grants`为空）。
- 目录或依赖构造失败时LetsTD不装配：115/116照常工作，标记都不带`letstd_grants`（115写版本1至7，116按上一条写版本1至7或9、10），`readiness.letstd`报告`{"ready": false, "error": 码}`。

## 配置与就绪

- 由`--letstd-rewards`读取（serve与doctor都有，启动脚本`-LetsTDRewardsFile`），配置注册表键`letstd-rewards`，时间语义为资格类，切换属hot档，不涉及客户端资源，不需要重新准备客户端。`config/letstd-rewards.example.json`写出全部开关且全为关。
- 配置集只写部分开关时按规范形保存，effective视图总是列出全部6个开关和native_popup；用命令行参数覆盖时，该键在控制台只读。回退到不认识`letstd-rewards`的代码版本后，含该键的配置集会以503 `config_set_invalid`拒绝加载。
- `readiness.letstd`直接取服务的status：ready、error、content_version、dependency_version、simulated、native_tested、verification、native_popup、rules、各合作方各组的开关与行数、目录状态、标签、`label_configurable: false`、`delivery: mail`与物品通道；服务不可用时为`{"ready": false, "error": 码}`。它不计入startup_ready，但115的status内嵌LetsTD状态、116的status带`letstd`布尔，两者的ready都取决于LetsTD是否就绪。
- 控制台提供开关、账本、可投性预判、投递投影与原生弹窗预览视图，以及`letstd.rearm`命令。

## 未实机验证

邮件里本地标签的显示效果与打开native_popup后的原生弹窗要等实机运行确认。LetsTDRewardParam的位置不影响客户端读取（静态闭合）：响应分派按子项名的std::map字节序遍历，与出现顺序无关，见`aigis_research/battle-modes-20261102/settlement-contract/contract.md`「响应分派通则」。首次结算耗时下客户端的行为已静态确定（见上文30秒口径），实机只需看实际耗时。
