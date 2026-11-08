# 塔兑换

塔商店使用独立接口65和69，不属于普通Shop2兑换。当前目录176行组成170件商品，其中90个单位、66个称号道具、8个好感礼物已接入扣币、发货和购买记录；其余6个召唤商品需要完整卡池，暂时在扣币前明确拒绝。这里的“接入”指后台契约和事务，不代表已经通过原生实机验收。

## 协议与资格

65接收`ITEM:S`，值为三种`tower_crystal_110001/110002/110003`之一；69接收`SHOP:I`和`NUM:I`。两者返回`TowerTradeHistory`的`ID:I/TradeCount:I`。TradeCount是当月（业务月）次数，本月没买过的保留记录回0。65会先清空客户端历史，而菜单同时读取三种塔商品，因此回复包含全部已购买记录及保留的旧商品记录，不能只返回当前货币的购买次数。

69返回本次新增单位的PPU、需要时的PBP.UX，以及消耗货币和奖励道具的POSSESSION绝对数量。客户端已有Enabled值保留，库存变成零时仍显式返回。单位列表41、库存129和登录配置137继续从同一存档读取，发货前检查完整回复可编码。

商品ID、限购、SingleBuyOnly、奖励和货币都取组内首行。下一次购买序号为当月次数加1，单价取不超过该序号的最后一个TradeCount档位。例如商品10000当月首次20,000、第二次起15,000，跨月恢复首次价。当前四个变价商品都要求单买；未来出现允许批量且跨越不同单价的配置时明确暂停该请求，不猜测官服计价。

DiscloseEmblemIDs是PlayerTitle物理行号，对应所有表达式都需满足；不是GroupId，也不要求正在装备该称号。DiscloseGeneralItemID还需实际持有。两个日期都存在时才使用左闭右开的兑换窗口。称号重复购买受库存Stack约束，角色和物品容量不足均零提交。

## 本地规则与存档

- 兑换次数按业务月重置：每月1日04:00 JST（JST04换日的业务月，官方id 6956：「交換数は毎月1日4:00にリセット」，变价商品当月第二次起降价、跨月恢复）。记录保存`{period, count, lifetime}`：count是当月次数，lifetime是终身累计；时钟回拨到某商品已存月份之前时，65仍按已存月计数返回，69对该商品回409 `tower_trade_clock_backwards`，直到时钟回到该月，不重开计数。服务器时刻落在客户端日历之外时65/69回503 `tower_trade_clock_invalid`。这里的服务器时刻取装配时的`bootstrap.clock`，与其它游戏服务同源；普通交易同样如此。
- 旧记录（按月重置之前保存的累计数）读取时转为终身计数（lifetime），当月从0计；旧回执按其累计口径的历史版本（`legacy_content_version`）照常精确重放。
- 单位按当前CardData默认初始值、level=0、evolution_count=0发放；这是已注明的本地初始化规则。
- 不凭空补塔币，也不跳过称号或道具条件。塔币的战斗来源已接通：各塔按TotalScore每日生成塔结晶，经68领取；层分数奖励档中的塔结晶经礼物箱发放，见`docs/tower.md`。
- `tower_trade_history`保存商品身份指纹与计数记录；商品改价、限额调整或资源版本变化保留次数，旧商品移除保留记录；同ID改作别的奖励或货币（商品身份变化）时存档照常可读，但只要有一件商品被换用，65与69整体回503 `tower_trade_product_reused`并记WARNING（原生客户端弹通信错误并原包重发），直到用控制台的`tower_trade.rebind_product`把每件被换用商品的历史改绑到当前商品。多件时每件一条命令，可放同一批，效果的`still_reused`列出其余待改绑的商品；商品不在当前目录回404 `tower_trade_product_unknown`，没有记录或身份未变回409 `tower_trade_rebind_unneeded`——`carry`决定当月次数保留还是从0起，终身累计都保留，命令要确认短语`REBIND <product_id>`并自动备份。内容切换报告的`notices`（`tower_trade_product_reused`）与doctor的`readiness.saved_tower_trades`会提前列出被换用的商品（`reused_products`，附拒绝的号、修复命令与客户端表现），两处都不阻断；doctor这一项只在塔兑换服务可用、已有存档且能量时钟就绪时出现，读取失败为`{"error": 码}`（如`tower_trade_history_invalid`，没有错误码时为`saved_tower_trades_unreadable`），不计入startup_ready。
- `tower_trade_receipts`保存最近16次精确回复、最近256个已处理请求摘要（旧版同秒集合读取时自动升级）和完整购买账本摘要。扣币、发货、限购及回执在同一次CAS保存；冲突后重读余额重新校验。同修订版精确重试返回原回复，其他旧请求明确失败。65/69被拒时以WARNING记录。
- 账本与回执相互校验；部分删除或损坏阻止继续购买。摘要用于发现不一致，不是对本机完整存档篡改的密码学认证。
- 奖励道具名超过原生格子255字节的商品（物品表仍按256字节读取）记为`item_name_exceeds_native_cell`，与召唤券商品一样在扣币前按501拒绝（69回`tower_trade_grant_unsupported`）；当前资源没有这类商品，目录指纹不变。

旧存档没有塔域时视为未购买，读取不创建领域或补币。有塔历史但依赖缺失时启动明确失败，不静默丢弃历史。目录、状态、发货和事务分模块，控制台复用它们提供`events.tower_trades`实时视图与`tower_trade.rebind_product`命令。

## 尚未接通

商品10009/10010/10011/12011/12012/12013使用三种十连召唤券。原生69成功后直接使用本次PPU进入召唤演出，不会再请求另一个抽卡接口。本地池31/32/33目前概率不完整，不能把残缺概率归一化，也不能把Stack=0的券当普通库存或邮件奖励。

塔关联赠礼和原生UI仍需后续实现或验收；塔战斗与塔币产出已接入，实机验收见`docs/tower.md`。静态证据见`aigis_research/events-20261101/tower-trade-contract.md`；逐商品能力和源指纹见同目录`tower-trade-resource-capabilities.json`。定向验证结果汇总到`artifacts/tower-trade-validation.json`。
