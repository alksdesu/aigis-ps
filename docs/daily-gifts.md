# DailyGift 定期赠礼

DailyGift已接入115和116的登录事务。它使用独立资格记录，将奖励送入普通礼物箱；领取后再由现有邮件流程增加资产。没有新建EBNS或虚构客户端签到页，也不在114登录认证时发奖。

## 原生证据与本地规则

当前DailyGiftConfig有319条外层记录、295个日期窗口，最后结束日为2026-09-03。原生加载器632760保留外层ID_DailyGift，按内层ID_Gift保存奖励数组；两层都可有0值续行，相同内层ID可以追加多份奖励。日期是YYYYMMDD整数。ID_Gift当前全为1，不能作为全表领取身份。

静态检查确认客户端仍加载这张表，但未找到从目录到实际发放的业务调用链。通用RewardEntry提供邮件转换规则，一条直接引用文案明确写出每日赠送两晶；这些证据不能证明全部官服资格算法。以下是明确的本地规则：

- 以日本时间凌晨4点划分活动日，有效日期包含首日和末日。末日包含已由官方期间证实（2019-02-07 EXNOA新闻稿把末日的领取截止写到次日3:59，2025-02-04官方X同式；核对见`aigis_research/events-20261101/login-window-boundaries.md`），与现行`start_date <= day <= end_date`一致，代码不变。
- 每个ID_DailyGift、每个实际访问的有效日取得一次资格；不同ID同日分别处理。
- 没有访问的日期不补发，旧档不从普通印章或现有邮件推断历史资格。
- 邮件容量不足或奖励暂不可用时，按已访问日期分别保留待发，不阻止记录下次实际登录的资格。Type22物品名超过255字节原生单元时按501 `daily_gift_reward_unsupported`保留待发，不让邮件写入方的503中止整次115或116。
- 已取得的待发资格不因到期、关闭新发放或更改选择列表消失。

104条源记录起止同日，不能按末日排他处理。源Type_Gift5表示奖励大类，实际邮件Type取Type_Item；不会把全部DailyGift错发成PPT.Type5。详见`aigis_research/events-20261101/daily-gift-contract.md`及`daily-gift-resources.md`。

## 配置与复刻

服务接受`--daily-gift-rules`，启动脚本接受`-DailyGiftRulesFile`。`config/daily-gift-rules.example.json`为默认规则，`gift_ids=null`保留全部真实源条目，仍检查每条的原始日期；所以当前默认没有活动赠礼自动发放。

`config/daily-gift-revival.example.json`展示显式复刻：选择ID8的两晶和ID319的一份料理，并把有效日期设为2026-11-01至11-07。使用时可调整起止日期。`revivals`仅覆盖服务器资格窗口，不覆盖原资源、标题或其他活动；`gift_ids`仍决定最终选择，不会因添加复刻窗口扩大显式列表。

`enabled=false`停止取得新资格，已经取得的pending仍会尝试投递。修改窗口不清除同一ID同一天的领取记录；重新登录、换接口或重启也不会多领。客户端未发现直接使用DailyGift窗口的活动页，因此这个功能不要求重建独立客户端资源。

## 奖励覆盖

当前314条记录已有可用的真实发货路径，包括免费结晶80条、单位模板93条、金币18条、料理与物品123条。Type1参数是PresentUnitData模板号，不是角色数量。

Type22的零参数仅在已验证的非堆叠料理或召唤券上解释为一件；正数保留真实数量，未知零数量道具不自动变成一件。111条料理先投递，单封领取时才恢复一份最大魅力，不在入箱时提前恢复。

5条旧券仍缺完整概率表，保留原奖励和pending，不替换卡池或把残余候选归一化：

| DailyGift ID | 物品 | 缺失完整数据的池 |
| --- | --- | --- |
|114|item_smn_ticket_70001|34|
|116|item_smn_ticket_10006|35|
|137|item_smn_ticket_80002|52|
|161|item_smn_ticket_85007|75|
|163|item_smn_ticket_19006|75|

## 保存与兼容

`daily_gift`版本1按gift_id和earned_day记录固定奖励摘要、当时的资格窗口、pending或delivered、实际投递日及邮件ID。同组多份奖励整组准备、预检和入箱；不发生只发一半却整组标记完成的情况。

115使用版本5投递标记，116使用版本6，DailyGift邮件和其他来源分别校验。旧115版本1–4、116版本1–5保留原内容摘要和响应重建路径；旧包重试不读取新时间，也不附加新赠礼。新请求沿用一次时间捕获和单次CAS提交，并在提交前检查邮件及响应容量。DailyGift分节的内容指纹由`content_version_for`按礼物箱配置求值，礼物箱的可选依赖或称号目录在115/116回执提交之后才接入时，旧回执照常逐字节重放；可重放的组合与409边界见`login-rewards.md`与`rank-rewards.md`。

奖励定义改变或源记录移除后，旧pending保留并报告原因，不用新奖励偷偷替换；用控制台的`daily_gift.resolve_changed`按gift_id处理（确认短语`MIGRATE DAILY GIFT <gift_id> <ACTION>`，自动备份）：`redeliver`按当前定义立即投递该ID所有定义变化的待发日，投递失败的那一天继续pending、reason改记失败码，之后由115/116的结算重试；`void`保留这条记录，status仍为pending、reason写`daily_gift_voided`，以后不再投递，那一天也不会重新计入，后面的日子照常获得；当前目录已没有这个礼物时redeliver回409 `daily_gift_source_unavailable`，只能void。历史已领记录保留，`events.daily_gift`视图分别显示资格日、投递日、冻结定义和待发原因，作废的那一天显示为`voided`（原因`daily_gift_voided`），定义变化或源记录已移除的待发另附`repair`节（`command`为这条命令，`targets`按gift_id列出，同一gift_id只列一次）；不能删记录来重领。

## 验证边界

203项相关定向检查通过，覆盖原始319条、续行和同组多奖、三种实际领取效果、115/116共享防重、日期边界、并发、容量恢复、源变更、旧回执及重开。按当前用例名汇总各批最新结果，没有重跑全量；4个已替换的旧目录用例不计入。详细结果存入`artifacts/daily-gift-validation.json`。

没有启动游戏或实际服务、修改原版客户端或正式存档。后端协议检查不等于原生界面验收；DailyGift的本地规则也不等于已恢复官服算法。
