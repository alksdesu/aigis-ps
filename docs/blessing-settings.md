# 战神加护设置

148已接入筛选及已持有加护的批量启用/关闭。目录使用当前242种战神加护、对应展示配置和菜单脚本，不按物品名前缀放行。筛选0/1/2/3分别对应全部、职业、属性、编队。物品名与库存一样按256字节旧上限读取；148只携带255字节原生单元内的名称，名称更长的加护仍可持有和读回，但不进入可设置集合，列在目录status的`unsupported_items`。

## 请求与保存

BGB内FI为可选I单值；ItemID:S与Enable:I同序列，必须同时出现或同时省略。Enable仅接受0/1。成对空序列作为本地兼容输入接受；原生没有修改项时通常省略两列。未知项、重复项、列长不同或未拥有物品都会整批拒绝，关闭未拥有加护也不例外。

筛选写入Config._BattleGodBlessFilter，通过PGC的FE同步。开关写入POSSESSION.Enabled，响应字段带d，不是请求的Enable。响应同时给出原有Count，不能只发开关而丢失该行数量。

筛选与开关在一次CAS事务中提交，提前验证36完整Config、129库存、137启动集合和148响应容量。失败不修改任何一项。独立blessing_history保存有限回执及最近256个已处理请求摘要（CT不作排序，旧版水位历史读取时自动升级）；精确重试要求修订版、状态摘要、依赖指纹和当前标准响应均匹配。无变化请求只返回权威值，不写回执或增加修订版；回执已淘汰但摘要仍在的重复请求，即使此时已无变化也回409 blessing_request_stale。

## 库存兼容

库存行沿用ItemID、Count，新增可选Enabled整数0/1。旧行缺省仍表示关闭，不批量改写或自动开启。数量增减保留已有开关，Count归零也保留设置；原生效果同时要求Count>0及Enabled非零，所以空库存不会激活效果。

所有非空POSSESSION都输出ItemID、Count、Enabled三列，行里没有Enabled时填0；空库存表仍不带列。原生`901BD0`沿用会话内首个非空POSSESSION的列集，所以启动库存（129/137）、148和其余回包统一带这一列，首次切换加护后148回送的Enabled不会被丢弃到重启。旧回执里保存的两列POSSESSION仍按原字节核对与重放：礼物领取（1/2/117）重放时的库存核对，以及按当前状态重建整份回包再比对的任务、登录奖励、等级奖励、投票、礼包和服装商店回执，先按三列比对，不等时再按两列旧形状比对，任一相等即按原字节重放，都不相等时回各自的历史无效错误（503，礼物领取为`present_history_invalid`）。两列旧形状只在选中行都没有Enabled时成立，相关行已有Enabled的旧回执本就是三列，此时两列回执被拒绝。礼包、任务EP、等级奖励、登录投票券和投票按同一构造（`startup_response`）核对启动库存；战斗回包（81塔开战、83、76、66、68）和其他直接保存回包字节的接口原样重放。只有会话里第一份非空库存来自两列旧回执的重放时，之后切换的开关仍要重启后才显示。

已逐一检查邮件、商店、送礼、觉醒、宝珠兑换、出售和主线奖励的库存写入。它们都经过共享PossessionService加减；皮肤解锁和取得计数继续只看持有量，不误用Enabled。

## 控制台与边界

控制台`blessings.set`调用与148相同的内核`BlessingService.set_settings`，校验顺序与148一致：依赖指纹、FI取值、ItemID与Enable的格式（非空名称、0/1，与148解析共用同一规则，失败同为400 invalid_request_fields）、玩家状态、已存筛选与`BlessingCatalog.validate`，再经`PossessionService.set_enabled`写开关并写_BattleGodBlessFilter。筛选与开关经预览、两步提交和审计在同一次提交中写入，不写blessing_history，读回36、129、137；字段归属表中Config._BattleGodBlessFilter只归`blessings.set`。底层set_enabled负责库存和标志校验，不代替加护目录的资格校验。

本批实现开关持久化及原生回读，没有运行游戏或修改正式存档。客户端已有按加护开关检查的能力条件；ConfigID1967的战斗挂接、各条件/效果和实机结果尚未验收。另一套限时经验/金币祝福与这些持有型物品独立。

## 验证

- 共享库存test_possessions 116项、Enabled边界test_possession_enabled 86项通过。
- 加护目录test_blessing_catalog 54项通过。
- 148服务test_blessing_service 72项、七类库存消费者test_blessing_consumers 34项通过。
- 原生编码HTTP与重开联动test_blessing_integration 10项通过。

计数取自2026-11-04中央全量运行（除smoke外全部通过）。artifacts/下的possession-enabled-tests.xml等报告与blessing-doctor.json诊断是2026-11-01实现当轮的产物，用例数是当时的。
