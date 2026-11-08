# 登录奖励与月消费

115每日签到事务已接通，普通印章奖励先进入礼物箱，350券随后走已完成的单封领取。新本地账号另有七次新手奖励，旧账号保留已知日期和印章次数，不追补历史或自动获得新手资格。原生界面仍待实机验收。

`LoginRewardCatalog`读取九档配置、九张印章表及BeginnerBonus，提供grade、stamp、beginner和奖励到邮件参数的转换。当前共有837条印章记录和7条新手记录；月份只有10、11、12，11月第31条为无效占位。档位根据上月结晶消费和OpenDate选择，OpenDate必须严格早于当前时刻。缺月、缺位置、占位和未核实奖励明确报告，不借其他月份或档位的奖励填补；115据此把当天印章记为待发，见“缺表待发”。

普通档第1/14次使用350券，其入箱及领取已完成，见summon-tickets.md。高档使用的1、51、349池不完整，命中这些池的印章按原档位记为待发；额外料理已按每日一份、月总额度及可配置本地物品接入，见login-cooking.md。普通活动签到已作为共享结算器接入115/116，见event-login-rewards.md；回归签到已独立实现，见comeback-login-rewards.md；购买绑定资格也已通过独立免费日奖礼包接入，见purchased-login-rewards.md。目录保留原始类型和参数，没有把这些数据改成普通档奖励。目录124项定向测试通过，报告artifacts/login-reward-catalog-tests.xml。

## 月消费状态层

`crystal_spending.py`提供initialize_spending、settle_spending、spend_crystals、spending_response和read_spending，85恢复、17兵营购买、112结晶商品及156服装购买均已接入。110项纯状态定向检查通过，结果artifacts/crystal-spending-verified-tests.xml保留各实际来源报告；交易入口另有联动与回执检查。

本地规则为JST每月1日04:00切换、实际消费累计保留int64，每个C1半部饱和32767。业务月起点由`business_calendar.month_start`统一给出（每月1日的换日小时`BUSINESS_DAY_START_HOUR`），结晶消费账本与月度容量账本共用这一个实现。相邻月将current移入previous，跨多个空月则previous归0；UTC毫秒时间不允许倒退。C1必须与账本投影一致，未知schema或异常值拒绝覆盖。免费/付费实际扣除均计入，余额入账、退款和免费恢复不自动变成消费。

新账号可显式建立new_local基线。旧零C1默认从接入周期开始，不追补；非零C1需要显式月份，不从余额或建号时间猜历史。消费账本不改变LastLoginDate、印章次数或七日新手资格。

## 签到与存档

Bootstrap新建状态时显式种入new_local资格、零消费及料理基线；读取旧存档保留其来源。初始化已有账本不读取新时钟、不改领奖或消费进度，重开不会因此使精确回执失效。login_rewards保存原生A5/A6对应的已领日期、月序号，以及独立的beginner_total/claimed/eligible；资源的新手奖励总数变化后存档照常加载，115回503 `login_rewards_rules_changed`并记WARNING（原生客户端弹通信错误并原包重发，直到修复），控制台`events.login_rewards`视图的`repair`节给出修复命令、被拒的请求号与客户端后果，`progress.login_rewards`视图的`rules`节给出同一核对（与doctor同形），`state.browse`的`login_rewards`账本报同一错误码，doctor的`readiness.saved_login_rewards`同样提示；停服换资源后重启也会出现这种状态。用控制台的`login_rewards.rebase_beginner`按当前规则重建基线（确认短语`MIGRATE BEGINNER LOGIN REWARDS`，提交前自动备份；已领数高于新总数时封顶并给警告），不自动改写。doctor的`readiness.saved_login_rewards`只读报告`saved_total`、`current_total`与`changed`（没有登录账本的存档`saved_total`为null），变化时附`refused_operations`（[115]）、`repair`（`login_rewards.rebase_beginner`）与`client`；同一节另有料理阈值的`cooking`（见`login-cooking.md`）与待发印章容量的`pending_stamps`（见“缺表待发”）。这一节只在登录奖励服务可用、已有存档且能量时钟就绪时出现，读取失败为`{"error": 码}`（没有错误码时为`saved_login_rewards_unreadable`），不计入startup_ready。经控制台配置切换换到新手奖励总数不同的世代时，切换在同一事务里自带这次重建（报告的`reasons.beginner`与`migration.beginner`，至少为gated档，不能暂存）。

签到日为JST04:00到次日03:59，同日不再次发奖；每个新签到日序号增加1，缺席日期不补，换月从1开始。签到日与印章月份由`business_calendar`统一计算（`business_date`与`month_key`），结晶消费账本的月份也取同一JST04营业日的年月（`business_month`）；服务器时刻落在客户端日历（JST 1970-01-01至3001-01-19）之外时分别回`login_rewards_clock_invalid`与`crystal_spending_clock_invalid`。115仅接受原生零SHOP/NUM，服务器决定日期、档位和奖励。邮件容量或任何完整响应不满足时整个候选回滚，连签到日期都不会先提交；印章本身缺表、缺池或未支持时只把印章记为待发。

115同一次CAS提交印章进度、料理额度、新邮件、消费月切与强回执。PGP2使用A1:S/A2:S键值，返回A5/A6/C1/CC/CD；新手奖励用BBNS的GetCount:I/GetRemain:I两列；PPT只包含本次新增邮件。没有钱包或单位入账时不发送空PPC/PFC/PPU，也不发送会清空活动状态的空PLC。

每个成功请求都记录回执，包括同日无新奖励的请求，防止该包跨日重试时变成新一次领奖。精确重试要求提交修订版、内容版本、状态摘要和重建响应一致，不取新时间、不分配新邮件号。保留16条回执，另保留最近256个已处理请求摘要，客户端CT不作排序，旧版CT水位历史读取时自动升级，平铺的来源字段由共享账本归入sources；回执淘汰后的已处理同包返回409 `login_reward_request_stale`并记WARNING。新动作只捕获一次时间并沿用于CAS重读；时钟倒退明确拒绝。可选来源服务在回执提交之后才接入时，旧回执按提交时的配置精确重放。回执`sources`之外的服务视为当时没有。较早的固定来源对缺席服务只写空占位，所以重放接受当前已接入服务的各种子集，而最后一个固定来源和列表式版本8的LetsTD必须当时存在。命中后按那份配置重建回包，所以料理资源失败期间提交的回执在料理恢复后仍原字节重放，后接入的回归服务也不会给旧回包补回归参数。礼物箱配置也计入枚举：115的基础摘要必需道具定义、召唤券与回复道具目录，缺任一项的配置当时不会有115服务；道具券、自选券在回执提交时缺席、之后接入，或称号目录后接入改变道具定义的版本时，基础摘要与公共活动、回归、购买日奖、DailyGift、LetsTD各分节摘要都按提交时的礼物箱配置求值（各子服务的`content_version_for`），旧回执照常精确重放。不属于任何已提交配置的摘要仍回409 `login_reward_retry_stale`。回执记录了非空来源，或其最后的DailyGift、料理、日票来源所需的服务已被移除时，按原规则先回503；其余已移除的服务回409。

公共活动、回归和购买日奖由独立共享服务接入。115投递标记版本2分开普通与公共活动邮件，版本3增加回归轮次，版本4增加购买资格来源；旧标记继续按原内容重放，不补观察或奖励。公共活动容量预检包含最终必需的回归参数，避免追加字段后才导致整笔失败。详见`event-login-rewards.md`、`comeback-login-rewards.md`与`purchased-login-rewards.md`。

DailyGift使用版本5标记，按独立gift_id和资格日校验邮件来源。它不产生EBNS，沿用PPT及普通邮件领取；旧版本1–4不追加赠礼或观察新日期，详见`daily-gifts.md`。投票料理与日票观察分别使用版本6、7，见`vote-events.md`；装配LetsTD时写列表式版本8，在任一固定版本的来源之后列出`letstd_grants`，cli默认装配（开关全关时为空列表），见`letstd-local-simulation.md`。

## 缺表待发

印章缺月表或缺位置、命中未启用或不完整的券池、单位模板缺失或奖励形态未核实时，115仍照常推进A5/A6，新手、料理、普通活动、回归、购买日奖、DailyGift和投票日票照常结算，只把当天印章记入`login_stamp_pending`。每条记录保存签到日、当日按上月消费和OpenDate选出的档位、月份、月序号、原因，以及当时已能解析的奖励定义摘要；不借其他月份，不降档，也不悄悄跳过。

之后任何一次115（包括同日再次请求）都会按签到日顺序重试待发印章：先发当天奖励，再补发，每个签到日成功入箱一次后即从记录删除。每次最多补发31条，每补一条都预检完整响应；邮箱或响应容量不足时停止补发，其余保留并更新原因。记录时已知定义的印章若在资源更新后定义改变，保持待发并标记login_reward_definition_changed，不替换成新奖励。这类印章用控制台的`login_rewards.resolve_pending_stamps`按签到月处理（破坏性操作，确认短语`MIGRATE LOGIN STAMPS <YYYY-MM> <ACTION>`，提交前自动备份），`events.login_rewards`视图的`stamp_repair`节列出待处理的月份：`redeliver`按当前定义立即补发，邮箱装不下或奖励暂不支持时（与115相同的失败）以新定义保留、由下一次115补发；当前印章表已没有该位置的只能`void`；`void`删除记录、不发邮件，签到日与印章次数不变。记录时还没有定义的印章不在此列，资源补齐后由115自动补发。

待发记录上限1098条（三年签到日），超过时115返回503 login_stamp_pending_capacity，须先更新资源。控制台`events.login_rewards`的`pending_stamps`节、`progress.login_rewards`的`stamp_capacity`节与doctor的`readiness.saved_login_rewards.pending_stamps`报告条数`count`、上限`capacity`与剩余`remaining`，剩余不足30条（约一个月的签到日）时`warning`为true，已满时另附`code`（`login_stamp_pending_capacity`）与被拒的请求号`refused_operations`（[115]）。已满时并非每个115都被拒：只有当天印章需要新记一条待发的新签到日以这个码拒绝，客户端弹通信错误并原包重发；同日再次请求、当天印章能正常入箱的签到日照常成功，已有记录的补发重试只更新原因。没有可用档位、当天奖励的邮件容量、完整响应超限及存档损坏仍使整个请求失败。旧存档没有这个键时按空记录处理；投递标记、回执结构和内容版本均未因此改变，旧回执照常精确重放。

## 消费接口差异

85/17回PGP2的S/S键值C1，112回C1:I平列，156没有PGP2接收器，只能由后续35/33/115取得最新值。对应业务预检完整GameParam2，成功、失败和缓存重试均保留扣款与统计的一致性。

85与17使用独立购买内容指纹，免费restore_state及118搬移的原规则指纹保留；112仅晶支付分支换新内容版本；156换到包含消费政策的版本。旧付费回执和已处理请求摘要保留，不能因升级重新扣款或补记一笔消费。免费恢复、金币/道具交换和退款入账不自动计入月消费。

非零旧C1且没有账本时，可通过CLI `--login-rules`指定明确基线，例如`{"legacy_spending_period":"2026-10"}`。这不是重置开关；已有账本不会被该参数重写，未来月份、异常位值和未知结构仍拒绝。正式旧档应先备份并检查来源，不能从剩余钱包猜已经消费的数量。

## 验证与剩余边界

新增签到状态、事务和真实HTTP测试覆盖新旧账号、七次新手奖励、同日重复、缺席、JST日/月边界、350券领取、重开、失败回滚和并发。四个消费入口另验证余额、累计数、响应格式、旧回执与免费路径兼容。相关报告位于artifacts/login-reward-*.xml、recovery-barracks-monthly-tests.xml、barracks-monthly-compatibility-tests.xml及shop-monthly-spending-combined-tests.xml；仅运行定向检查。

本批283项最新定向结果汇总于artifacts/login-spending-verified-tests.xml，全部通过；来源报告仍保留，未运行全项目测试。其中实际HTTP连续走完当前10–12月92个普通签到日，验证全部对应奖励入箱，新手额外奖励始终只发七次。历史商品测试使用与存档创建相符的模拟时间，保持生产时间回退检查有效。

当前1–9月缺奖励表，高档部分券池不完整；这些印章按“缺表待发”记录并在资源补齐后补发，不降为普通档、不借月，也不偷偷漏发。额外料理的每日一份和月总额度已接入，官方秘书菜品选择尚未还原；默认通用恢复药的本地规则详见login-cooking.md。普通活动已接入独立逐活动记录与待发重试，回归签到已接入独立轮次，购买绑定日奖已有独立资格与领奖服务，见purchased-login-rewards.md。156消费后立即刷新印章界面的原生触发也尚未证实。

本批没有启动游戏或重启服务，没有改动正式存档。完成后台验证不等于原生签到动画、奖励提示及完整主页已经验收。

原生合同与影响面分别见aigis_research/permanent-20261101/login-rewards-contract.md、summon-ticket-contract.md及crystal-monthly-spend-review.md。普通召唤调度仍按用户要求等待后续采集。
