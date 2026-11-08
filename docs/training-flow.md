# 角色合成后台

注册5已接入普通实例、Type1银仓和Type2圣灵仓库素材，包含当前特殊圣灵、Child素材、技能/减费贡献、条件判断及已存转职/觉醒角色的成长派生。觉醒交易使用独立7/8/9接口，见evolution-flow.md；经典转职6在当前客户端不可用。尚未进行原生界面验收。

## 状态与规则

- BASE和APPEND读取七列身份，同时核对GetIndex、UniqueID和CardID。单次1至10份素材，拒绝自己、重复实例、锁定素材、王子和任何编队中的素材；锁定的培养目标仍可使用，王子不能作为培养目标，服务入口和`TrainingCatalog.quote`各自拒绝。
- APPEND普通素材可以来自已解锁的额外兵营，StorageIndex必须与实际AE兵营位一致；与觉醒共用`resolve_barracks_material`，CAS重试重新核对。Type1/2聚合仓库继续走独立数量表。兵营规则参与回执内容指纹。
- BASE限定第一兵营，这与原生一致：合成目标菜单只取当前兵营表，离开兵舍移动菜单时表栈已清空；BASE序列化把StorageIndex固定为0，单表PPU接收器也无法应用额外兵营目标。额外兵营实例须先经118移回第一兵营，这类请求仍回501且不改错误码。
- 金币价格使用合成前显示等级：每份`160+40*Level`。普通技能按有效SkillID匹配，cost按原CardID匹配；目标ChildCardID命中时改用素材Child字段，其他特殊贡献继续叠加。先消费剩余经验，再加直接等级；直接等级会清剩余经验。
- PPU返回A7/A4/A6/AA/B2/B3六列绝对值。这里更新原目标，不使用追加角色的响应语义。
- 用户选择保留溢出经验：累计Exp继续增加，显示等级受职业上限约束；以后转职提高上限时重新派生。int32容量不足时整笔拒绝。`--training-rules`或启动脚本的`-TrainingRulesFile`可加载`{"retain_overflow_experience":true}`；false只限制新增经验，不删除旧的溢出经验。
- 旧档技能等级0且具有有效技能时，按原生派生规则从有效等级1计算。

`TrainingCatalog.quote_materials`（不依赖协议请求，`quote`只是5的适配层）、`training_math`、`training_effects`及`training_recipe`不改存档，供控制台预览和共享操作层复用。交易在一个CAS提交内扣钱、更新目标、删实例/扣库存和记回执。原21列单位结构不变；`training_history`使用共享请求账本（schema_version=2）：最多16个回执，另记最近256个已处理请求摘要，不按客户端CT排序；时钟回拨或2038年后CT为负时照常受理。旧v1（只有回执）和旧v2（CT水位）读取时自动升级，旧回执仍可精确重放。回执已淘汰但仍在摘要窗口内的旧请求返回409 `training_request_stale`，不会再次扣聚合库存。精确响应只在对应修订版和内容版本复用。额外兵营容量与培养目录的可选仓库输入（妖精乡、可写的银之馆，以及妖精乡的额外兵营容量）在回执提交时缺席、之后才接入时，回执摘要按提交时的组合核对，旧回执照常精确重放；依赖被移除、其余依赖变化或摘要不属于任何可达组合时回409 `training_retry_stale`。被拒的5都会写WARNING日志：原生A7FDF0只在收到200后返回，任何HTTP错误都弹出通信错误并按原包重发，本地删除素材只在成功后执行；仓库余量不足时另记StorageType、CardID、本次选择数和现有数，错误码与回执不变。

删除前通过`GiftService.preserve_history`冻结旧好感度达标基线，避免旧角色尚未送礼就被消耗后，重新获得同角色时重复领奖。该过程不发奖励；CardStatus、剧情历史、礼物、UX和UniqueID高水位保留。保存前验证41完整名单及金币状态。

原生启动读取拒绝金币与账号经验同时为0。账号尚无经验时，恰好花光最后金币的交易会明确拒绝，保留全部素材，不擅自赠送经验。

## 特效

UnitSpecialty和AbilityConfig是两个编号域：前者8/9/10涉及倍率，31涉及同职业经验；后者99至105、138至143、214涉及配方、成长与加成。54至59号圣灵按稀有组计算250%倍率；技能/cost贡献按原生顺序相加；独占、满级、同稀有组及当前138条件表达式均已接入。选择前缀比较完整等级及剩余经验，不能将“未跨级”误判为“无增长”。

cfgcmbcmp/ignore按资源解释，当前普通基础倍率100%。CMB从持有状态复算并取最大值，不叠加重复来源、不相信客户端百分数。普通持有Card416提供10%；当前54种圣灵仓库名单不含416，416也不具备新名声登记资格。名声库现已按CardId和五个正库存桶解析，聚合来源使用原生A7=-1；正常非空库不再阻断合成，不能伪造当前不可新增的416寄存流程。

当前2930张卡的静态派生引用均可解析，1409条Child关系全部存在。原生空rating命令默认允许，所以394/456不再误拦；原生能力查找未命中返回无效果，合法的ClassAbility1=500等引用不再当作卡片损坏。该覆盖证明引用与派生可用，不代表每张卡都能作为素材，也不代替原生验收。

## 圣灵仓库

122读取、123存入、124批量存入、125取出已实现，当前CfgFairyFarm共54种，每种上限取OnSystemCommand的600。保存为`fairy_farm_schema=1`和`tables.fairy_farm`数量表，旧缺失/空表兼容，未知非空结构不丢弃。仓库存取的客户端发送前会清缓存，因此这些接口回完整54行PFF；合成5只回本次变化的卡，最后一份须显式Count=0。124另外返回实际移除名单和结果码，具体策略见unit-consumption.md。

123已支持从同一已解锁额外兵营手选存入；124的BRK.IDX=0/1/2只操作对应兵营，省略BRK操作全部已解锁兵营。先按范围筛选，再沿既有GetIndex递增策略选择可存数量；只忽略AE兵营位检查标准成长，不忽略锁定或其它状态。125仍发至第一兵营，不记录原兵营。56项新增范围检查、27项既有兼容检查及3项原生编码HTTP联动通过，后者见`artifacts/fairy-barracks-integration-tests.xml`。

存取、合成均原子提交，取出创建新GetIndex/UniqueID；存入前保留角色历史与编号高水位。123手选存入与原生选择一致只拒绝编队中的单位，锁定或成长过的圣灵照常存入、只保留数量；124批量自行选取，仍保留锁定、编队与非默认成长/好感等属性的实例，这是明确的本地保护政策。仓库只存CardID/Count，不能冒充保存原实例全部属性。

## 控制台

5的事务内核为`TrainingService.train(state, revision, request, now=)`，选材、保护、报价、经`wallet_money.money_after_debit`扣费（余额不足回`training_money_insufficient`，恰好用尽且经验为0时回`training_native_balance`）、仓库扣除、删除素材（保留UniqueID高水位）、更新目标与任务计数都在内核中完成。`TrainingCatalog.quote_materials(player, target, materials, state=, boost=None)`不指定加成来源时，按原生菜单提供的最大持有加成报价；`ownership_bonus(player, state)`返回最大加成百分比与全部同额来源，扫描遇到损坏引用时与报价一样返回503 `training_growth_reference`。成长描述公开为`describe(unit)`。职业、稀有度、技能与经验表另以只读视图`classes`、`rarities`、`skills`、`experience`公开，每次读取当前表；觉醒目录、名声目录、成长商店与单位详情只经这些视图读取。觉醒报价（`EvolutionCatalog.quote`）读取目标、素材和觉醒后单位的成长描述，控制台成长编辑（`UnitGrowthEditor`）读取等级、等级上限、经验阈值和技能上限，二者都只经`describe`；持有加成只经`ownership_bonus`查询。

控制台命令`units.train`按最大持有加成代为合成，按原生规则拒绝，不读也不写`training_history`；`tools.training`预览给出保护检查、价格、余额、加成来源、等级前后与上限以及字段变化。

## 剩余工作

1. Type1银仓合成结果页部分Tag点击的缓存刷新；额外兵营素材已接入，服务端仓库扣除及126完整回读已通过验证，普通继续路径的126→122已静态确认。额外兵营目标按原生一致排除，不再列为待做。
2. 原生一致排除与资源绊线：临时单位覆盖（CUnitDynamicPropertyTable）只在战斗内建立并在收尾销毁，菜单期恒为空表，所以存档携带非空`unit_dynamic_overrides`时继续501，不静默丢弃：先在控制台的`units.overrides`视图里诊断，再用`units.overrides.remove`整键移除（需输入确认短语`REMOVE UNIT OVERRIDES`，提交前自动备份并记入审计），移除后合成5、商店成长113、银兑换106恢复正常，守卫本身不变；含影响214的角色能力变体在当前资源中无一可达，新的效果条件或系统表达式当前为空集，两者都保留501绊线，资源更新后复核。名声库结构和持有查询已经接入。
3. 觉醒交易的关联状态和原生验收；当前正编号模板与技能切换已接入，7/8/9边界见evolution-flow.md。
4. 自动填充/恢复选择的原生验收；现前缀按请求序列检查，手动选材保序链已有证据。
5. 原生预览、完成动画、素材消失和退出重进验收，等用户恢复实机操作。

## 证据

静态资料位于`aigis_research/permanent-20261101`，包括training-contract、training-special-contract、training-child-contract、training-condition-predicates、training-multiplier-contract、training-defaults-review及unit-storage-eligibility。

定向测试覆盖协议、float32和阈值、交易、保护、CAS竞争、旧好感记录、原生编码HTTP、41重读及重建服务重试。测试存档在111项目内；没有改正式存档、原版客户端或启动游戏。各专项结果保存于`artifacts/training-*.xml`，只读检查见`artifacts/training-doctor.json`。

本次收口的82项关联测试见`artifacts/training-final-tests.xml`。其中真实流程证明仓库存入、重复聚合材料消费、耗光显式零值、取出新身份和重开去重；不表示已验证原生窗口动画。静态全表覆盖范围和限制写在`artifacts/training-resource-coverage.json`。

额外兵营素材的72项合成/觉醒/主页关联检查见`artifacts/material-barracks-tests.xml`，覆盖原生编码转移→合成/觉醒→41读回、伪报位置、未解锁、受保护素材及CAS移动冲突。聚合仓库关联检查另随恢复接口联动报告保留；没有扩大全项目测试范围。
