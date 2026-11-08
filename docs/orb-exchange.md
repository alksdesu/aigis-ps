# 宝珠兑换

注册139使用根级`ITEM:S`指定获得宝珠、`BASE:S`指定消耗宝珠。价格与生成量来自输出行，原料数量来自输入行，不接受客户端价格或数量。当前资源提供7种`evolve_orb_100001`至`100007`，排除同类换同类后共42个有向组合；目前每次15000金币、5枚原料换1枚目标。

`OrbExchangeService.quote`为只读报价接口，返回冻结结果，可供后续控制台复用。主表和物品定义以指纹绑定，使用同一有效资源视图；旧宝珠与未定义物品不能代入。主表与物品定义仍按256字节读取宝珠名；目标宝珠的ItemName超过原生255字节单元（`possessions.MAX_ITEM_NAME_BYTES`）时目录照常建立，报价与兑换在未知物品、同物品两项检查之后、读余额之前回501 `orb_exchange_item_unsupported`，存档不变；源物品是已有的存档行，沿用256字节存档名口径。status的`unsupported_item_names`列出这些名字，`name_policy`说明口径；内容指纹不变，旧回执照常重放。当前7种宝珠名都远小于上限。现阶段无兑换次数限制属于本地规则，不能据此断言官服没有限制。

提交前检查余额、输入数量和输出Stack上限，并预检完整137配置及本次139响应。扣款、扣物品、入账和回执一次CAS提交，冲突重新读取和报价。保留原生启动对金币/经验不能同时为零的要求，不修改角色或编队。金币扣款由`wallet_money.money_after_debit`按本接口的拒绝码计算：余额不足回`orb_exchange_money_insufficient`，恰好用尽且经验为0时回`orb_exchange_native_balance`；139只扣不加金币。

响应仅包含SYSTEM、PGP和POSSESSION。139的PGP由行格式接收器CGameParamReceiver读取，按列名写字段，因此回`A1:I=[新金币绝对值]`，不能用其他接口的`A1:S/A2:S`键值形，否则客户端金币变0、经验被写成金币；内容指纹策略升为2，旧键值形回执不再重放。POSSESSION回输入和输出两种物品的最终持有量，归零也显式返回，137可恢复完整库存。后续觉醒直接消费同一库存。

首次成功时增加`orb_exchange_history`，schema为2，保存最近16条完整回执及最近256个已处理请求摘要；CT不作排序，旧版schema1读取时自动升级。相同请求只在原修订版和资源指纹未变时重放；过期、淘汰或已被其他操作改变状态的旧请求拒绝再次执行。旧存档无此字段时自动兼容。被拒的139写WARNING日志：原生86F3C0只在A7FDF0收到HTTP 200后才进入完成流程，被拒时弹出通信错误并按原包重发，直到服务端接受。

`tests/test_orb_exchange.py`共92项专项分批通过，覆盖42方向、不同输入/输出配比、余额、上限、格式、失败不写、重试及CAS。`artifacts/orb-exchange-integration-tests.xml`中16项检查通过，包含原生编码HTTP、137回读、重开存档、兑换后觉醒、CLI接入及既有启动保护。原生游戏界面尚未验收。

静态证据：`aigis_research/permanent-20261101/shop-exchange-contract.md`及同名JSON。
