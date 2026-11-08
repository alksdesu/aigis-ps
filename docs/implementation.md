# 实现与验证记录

## 协议层

当前协议表保留 156 个注册编号，按请求路径、请求 XOR、ST、响应 XOR 和标志区分。同线格式别名共享处理器，不直接套用旧 AigisPlayer 的编号含义。

- 请求：有界原生 Snappy 解码、请求 XOR、ST 匹配、HS 校验。
- 响应：带声明的列式 XML、原生 Snappy 压缩、独立响应 XOR。
- 两个公共主表入口接收裸 `Rev` 文档，使用独立响应限制。
- XML/解压长度、节点数量、原生固定接收缓冲区分别检查。
- 未实现返回明确错误，缺少数据返回不可用，不生成虚假的空成功结果。

当前客户端发不出的注册号不设处理器，按未实现回501，与原生一致：99是送礼菜单基类的编号，女、男两个物品菜单都覆盖为100/101；145（删号信息）只能从标题按钮WD10进入，当前标题面板没有这个控件，SelectData还把它放进禁用表；144（解除数据连携）的三条发送路径分别被恒假分支跳过、挂在没有任何资源引用的系统命令上、挂在从不被调用的会话虚函数上。144/145的发送点都不读结果。依据见aigis_research/login-states-20261101/remove-account-contract.md与aigis_research/native-audit-20261102/findings.json。86（旧体力恢复）受当前资源门控：关卡菜单与旧对话两个发送方要求关卡的QuestTable.ActionPoint非零，商店首页发送方要求WidgetTag 1001按钮，当前资源都没有；资源前提由`tests/test_recovery_service.py`守护，详见recovery-flow.md。

另有两类注册号同样不设处理器、按未实现回501。一类没有任何原生发送者：11、28、30、37、40、48、49、52、53、56、61、92–98、131–134、140、141，166个A7FDF0直接调用点与全部动态取号路径都不产生它们，A7FDF0也没有函数指针引用；29与31线上签名相同，由31的处理器应答，不在此列。另一类按原生或研究推荐不开放：143/147（协力大討伐）只在SubjugationType为1/2时由7697A0/74DEC0发送，当前启用的3个大討伐SubjugationType都是0，协力任务的Enable全为0，击破数又是全服聚合值；两者在载入所选协力大討伐的任务菜单时就发出，不经玩家确认，所以大討伐目录status的`cooperative`列出已列入菜单且窗口未结束的协力任务（期望为空，当前资源下为空），任务窗口补丁也不让协力任务进入批次，并拒绝指向它们的键；6（经典转职）的资格链4CB070→4CB000→4CAE60恒返回false；16（所持枠購入）不开放，现行容量公式65FDF0不读HaveUnitMaxSizeBuy低16位，也不造「扣晶不加容量」的规则；18/19/20/87/88/89/90（旧限时加护）默认不开放，商店首页的BuyExpBoost/BuyGoldBoost是空命令，旧加护商店的入口未证实；91/154（付费）不实现，6CAA40只在Price>0时走付费链，本地目录全部Price=0，客户端只走46→50，也不伪造支付成功。12/13（召唤）只在玩家点召唤按钮后由7E5ED0发出，按钮需要CSS阵容行；70回显式空调度后召唤页不生成单元格，执行召唤在查不到阵容时返回SummonIDNotFound而不发请求，召唤本身待官服调度采集后实现，不造卡池。108（商店召唤）只在玩家点购买并确认后，由80ACF0启动80DBA0发出，而且只对ProductType 2、Type 4的商品发出；常开的资源覆盖把这类商品（当前ShopID 15）的销售窗口改为已结束，原生在售判断818200让商品列表不显示它，购买入口弹出期间外提示，客户端在有效资源下发不出108，服务端维持501，见local-resources.md、trade-flow.md与aigis_research/console-20261102/empty-summon-schedule.md第7节。依据见aigis_research/native-audit-20261102/work/callsite_class.json、aigis_research/permanent-20261101/evolution-contract.md、unit-capacity-contract.md、timed-blessing-contract.md、aigis_research/backlog-20261102/backlog.md与aigis_research/native-audit-20261102/parts/shop.md。`tests/test_native_faithful_refusals.py`按决定分组列出上述全部编号（native_unreachable、no_native_sender、coop_subjugation、classic_class_change、unit_slot_purchase、timed_blessing_shop、paid_purchase、empty_summon_schedule、closed_shop_summon），取`cli.services()`和serve实际装配的处理器，逐号核对它及其同线别名没有处理器、HTTP回501 not_implemented且不写存档，并要求每个编号只属于一个决定，还要求serve装配后没有处理器的注册号恰好是这些决定的并集（当前44个）；另核对目前没有列入任务菜单的协力大討伐、关窗覆盖列出的商品恰好是资源里全部ProductType 2且Type 4的商品且其销售窗口已结束、关窗前的交易目录版本仍在可重放集合中；以后要开放其中任何编号，须先改对应的决定。这些结论只来自静态反编译和资源扫描，没有在实机上核对；客户端版本变化后需要重新研究。

上述维持501的号中，客户端不经玩家操作就会发出的只有143/147，前提是任务菜单列入了协力大討伐。其余只在玩家操作后才发：12/13要在召唤页点召唤按钮，空调度下召唤页没有按钮；108要在商店点购买并确认，常开的资源覆盖已把ProductType 2且Type 4商品的销售窗口改为已结束，玩家点不到，详见trade-flow.md；16、18–20与87–90要玩家点商店首页或旧加护商店的购买入口才发；6、86、91/154也要玩家操作，而且在当前客户端与资源下发不出；99、144、145与没有原生发送者的号根本不会发出。玩家真的点到一个回501的入口时，客户端同样弹出通信错误并在每次确认后原包重发。

142在身份交换之前调用，不鉴权，SYSTEM.ExchangeKey直接给出当前方法密钥。原生同样在标题阶段取得这个值，31之前客户端会清空，31下发的也是同一个值，没有状态分叉。本地安全边界是回环地址：服务只监听127.0.0.1或::1，中间件拒绝非回环Host（400 invalid_host）和带Origin的浏览器请求（403 cross_origin_denied），只有本机进程能取到它。方法密钥不是独立凭据：鉴权接口同时校验UI与UK，UK只能凭启动令牌经152取得；142本身不改存档。

45、146、153、155、102/103由`session_stub_service`处理。它们是原生启动、主页和定时流程必发的请求，发送点都不读结果，但HTTP非200会弹通信错误并原样重发，所以只拒绝原生客户端发不出的请求体，全部不写存档。请求形状来自静态反编译：45的Platform由A5AA30写成PlatformCode加0x100位；146由A63FC0写C6，即全局F2ABA8，66E510发送前调用的642E60从DMM会话+0x80重新载入它（session_flags的两个值写入F2AB70/F2AB74，与C6无关）；只有DMM会员信息（644530读dmmPremiumReceiverId）、会员面板WD84/WD85（CLoginStateSelectData，只在644470为真且DmmSession+0xCA非0时启用）或WD90/WD91（CLoginStateDmmPremiumInfo）会把它置为1或2，两个面板都只在DMM会员路径出现，会话+0x80只由F2ABA8写回；本服务不下发DMM会员信息，所以本地为0，其余值回501 dmm_premium_receiver_unsupported；153/155共用A64E10，只追加VI/OT并保留公共的UI/UK/MK，而152的A64B50删掉UI/UK/MK，所以`BootstrapService.platform_identity`对152拒绝游戏身份字段，153/155传`keeps_game_identity=True`，只做与152相同的启动身份和GPVI/GPOT一致性检查，UI/UK/MK只由公共头校验形状、不比对值：153的定时器由66EC00在游戏初始化时登记、每1800秒触发，而UI/UK要等642E60才从DMM会话复制，在标题画面停留时仍为空串，VI/OT则取自启动参数/viewer_id=与/onetime_token=，客户端缺任一项不会进入游戏；102/103由A5EC80写一个CH容器，HK（检测键，int）与LG（日志文本）逐条配对，键取自按无符号int排序的std::map，因此唯一且按无符号升序。响应组按各准备函数的登记：45回SYSTEM和零行SCHEME（Url列，CSchemeUrl清空链接表，主页不打开外部链接）；146、153只回SYSTEM；155回SYSTEM和default组，CGetDmmPointReceiver只按字符串解析Point，本地为"0"；102/103不登记任何组，照惯例回SYSTEM。45和146计入启动诊断，被拒时出现在`startup_diagnostics`。cli.py总是构建`SessionStubService`并把这6个号并入serve的处理器，readiness的`session_stubs`给出各项口径。102记WARNING、103记ERROR（103发出后原生客户端自行停止），日志最多列16条、每条256字节并转义控制字符，不对账号做任何处理。以上未经实机核对。已知边界：102/103按通用请求上限校验（最多4096条、每条LG不超过4096字节），4A1420与9D6F00写入的文本不超过256字节，994840按脚本模板替换出的文本长度未核实；作弊检查定时器同样在游戏初始化时登记（每30秒），若复制UI/UK之前就产生检测日志，会按身份不符回403并被原样重发，是否会发生未核实。

70/71由`summon_schedule`服务应答。菜单场景入口每次进入都发70（只有72CF80判定优先处理观看事件时走另一分支），主页轮播横幅的加载任务依次发71、70、46，都不经玩家操作，非200同样弹出通信错误并原包重发。两号都只收公共头，回SYSTEM加无子节点的业务组（70：CSS、FPR、SH、GacyaStatus、LCC、PLC；71：CSP，组名取自`schedule_capture.SCHEDULE_GROUPS`），编码为`<CSS></CSS>`这类空元素，不列列定义；鉴权之外不读写存档，也不取时钟。原生A7EE90把无子节点的组当作零行并以空表替换旧内容，FPR、SH的入口先把计数清零，所以客户端不列出任何阵容、召唤活动与召唤包，主页指向召唤阵容的横幅不显示，召唤页不生成召唤按钮。召唤11/12/13仍不实现，不造卡池；将来实现召唤时70必须回完整的当前状态，因为召唤成功后客户端会重读70，继续回空组会清掉刚写入的计数。readiness的`summon_schedule`给出两号的组与口径，不计入startup_ready。以上未经实机核对，依据见aigis_research/console-20261102/empty-summon-schedule.md。

原生启动加载器对35、36、38、39、41、43、44、51、54、137重发到成功为止：任何4xx/5xx都会让客户端在加载处弹出「通信エラー…番号：<状态码>」，每次确认后原包重发；收到200而解码或应用失败时才无提示重发。serve合并全部处理器后，把这些编号统一经bootstrap的同一启动诊断登记表包装（`cli.NATIVE_STARTUP_LOADS`）：bootstrap自己的加载、任务54与投票版137已经包装过，重复包装原样返回；玩家状态的39、41由此计入，被拒时与其他启动加载一样出现在`startup_diagnostics`。

readiness与doctor的`auto_requests`列出客户端不经玩家操作就会发送的号：`cli.AUTO_SENT_OPERATIONS`合并了上述启动加载、`session_stub_service`处理的启动、主页与定时请求、主页与菜单入口的46、54、55、62、70、71，进入任务菜单时（CMenuQuestTop与13个CType*菜单的载入钩子）发的130，进入塔门户时每座塔各发一次的66与进入塔顶菜单时发的67（所选关卡是塔层时塔顶菜单还可能再发66），进入素材类菜单（进化、转职、觉醒、合成、技能觉醒、选择券）时先后发的126与122，进入银之馆与皮肤商店时发的126，打开所选任务属女神復活（分类17）的任务列表菜单时发的157，任务剧情（EventArc末行DoPeruseEvent）播放结束或被跳过时发的84，以及每个新营业日第一次到主页、开战或停在菜单跨过04:00时的115；大討伐目录status的`cooperative`列出的协力任务所发的143/147也并入`operations`。`unhandled`是其中当前没有处理器的号，期望为空；`cooperative_subjugations`原样给出那份列表；`client`写明客户端此时停在通信错误弹窗、每次确认后原包重发；`ready`在`unhandled`为空且大討伐目录与协力大討伐的配置行都可读时为真；目录读不出时`cooperative_subjugations`为null，serve另记一条WARNING。可选服务（任务54、奖励棋盘62、礼包46、投票55、任务记录84/130/157（随主线任务服务构造）、塔66/67、妖精乡122、银之馆126）构造失败不阻止启动（任务与礼包只在活动通行证复刻开启时计入startup_ready），这些号随之回501，只在这里与日志WARNING中显出；登录奖励构造失败时115同样列入`unhandled`，但login_rewards本身计入startup_ready，serve拒绝启动。serve在启动闸门通过后，`unhandled`非空时记一次WARNING，写明号码与客户端表现；列出且其号没有处理器的每个协力大討伐、每个读不出的SubjugationMissionConfig行再各记一条。`auto_requests`不计入startup_ready；按当前资源共29个号，`unhandled`与`cooperative_subjugations`都为空。

## 当前处理器

| 注册编号 | 内容 |
|---|---|
| 142 | 登录前标题配置；明确采用无远端覆盖的本地规则；不鉴权，安全边界见协议层 |
| 152 | 本地平台启动身份交换，LI 的 UK/UI/NN 字符串列 |
| 153、155 | 一次性令牌延期与DMM点数；只校验152的启动身份，公共UI/UK/MK不比对值（登录前为空）；153回SYSTEM，155另回default组Point="0"；不保存令牌期限 |
| 146 | DMM会员受领ID；只受理C6=0（本地不下发DMM会员信息），其余回501；不写存档 |
| 45 | 进主页时的链接表；Platform须为PlatformCode加0x100位；回SYSTEM与零行SCHEME，本地无外链 |
| 102、103 | 客户端作弊检测日志；CH内HK/LG配对、键按无符号升序唯一；限长写日志后回SYSTEM，不处分账号 |
| 70、71 | 召唤调度与召唤包；显式空调度：70回SYSTEM与无子节点的CSS/FPR/SH/GacyaStatus/LCC/PLC，71回SYSTEM与无子节点的CSP，不含阵容、不造卡池；只收公共头，鉴权之外不读写存档，不取时钟；菜单入口与主页横幅不经玩家操作就会发出，非200会让客户端停在通信错误弹窗；召唤11/12/13不实现 |
| 21、22 | 同线格式配置保存；声音选项组或教程完整位掩码，PGC增量回写 |
| 25 | 原样持久化排序F1/F4/F5；PGC回F1/F4，F5由36恢复 |
| 26 | 新筛选按FilterNameHash更新完整组选择，137返回全部PlayerFilterParam快照 |
| 119 | 多角色锁定，保留其他标志和属性，完整PPU只更新第一兵营实例；不走发卡追加 |
| 100、101 | 送礼、好感阶段、剧情历史、奖励入箱和持久重试；原CardID每档一次；99原生不可达，不设处理器 |
| 75 | 主页事件的已读确认；只受理脚本实际执行DoPeruseSave的事件ID（当前135个，含两种150通用演出），仅写PEV，不发奖 |
| 136 | 剧情回顾的已读确认；ConfigID须由发送DoPeruseSaveEpisode的主页事件包实际保存，按其EventID置PEV已读，137读回；不发EventEpisodeConfig的经验 |
| 84、130、157 | 关卡记录接口，见下文「84/130/157」 |
| 1、2、117 | 本批圣灵模板和免费结晶领取；1/2同线格式，117领取上传的邮件集合 |
| 31，兼容同线格式 29 | 本地方法密钥、SYSTEM |
| 114 | 本地登录菜单、13 列 LGI；A3（IsSucceeded）回0，与原生普通DMM账号一致：标题只显示开始入口与「ID:10<玩家ID>」，没有第二数据槽（データ2），数据槽绑定端点（客户端里地址为空串）不可达、不需要本地提供；A6/A10保持玩家ID，A3=0时客户端不读 |
| 34 | GameParam，22 列；存档行可序列化且Money、Experience不同为0就回存档原行，月度准备、33/35预编码、进度一致性与CAS冲突只记degraded，因为拒绝会让客户端停在通信错误弹窗循环（原生只在200应答解码失败或Money与Experience同为0时才走新号分支） |
| 33 | PGP2 当前事件标记；_EvCmb位0为0的旧档先按客户端回退规则转换并保存 |
| 35 | GameParam2，30 列 |
| 36 | Config，30 列 |
| 38 | ReproductionItem，5 列 |
| 39 | 持久化战斗参数 VN/UX/TN/RTN；新档RTN取OnCommandMenuRaidTeamView/RaidSallyView一致声明的突袭组（当前64）；只要求已核实的BattleParam schema并校验存档结构，不依赖CardData；被拒时以WARNING记录（原生启动循环会重发） |
| 41 | 持久化角色的21个原生字段，职业和技能引用当前主表 |
| 5 | 普通/特殊/Child素材、经验/技能/减费合成及Type2库存消费；边界见training-flow.md |
| 3、4 | 手动及自动出售，金币/彩虹水晶结算、保护与持久回执 |
| 122、123、124、125 | 圣灵仓库读取、存入、批量存入与取出，54种库存、每种600上限及持久去重 |
| 126、127、128 | 银仓PSH读取、存入返还与最低状态取出，保留历史、数量及身份高水位 |
| 72 | 启动编队读取，全量SYSTEM/PBP/TGI，不修改存档 |
| 73 | 按顶层编队保存快照并保留其他编队，TL教学位同次提交；成员GetIndex不在名册但在0..UX之间时按空槽保存并记WARNING，回包TGI快照随即清掉客户端编队中的这些成员；从未发放过的GetIndex（大于UX，或-1空槽以外的负值）与重复地址仍回400 |
| 74 | 一次旧编队迁移；原生54D260按成员数补的空槽与成员重叠时（先成员行、后同名同称号A1=-1的补槽行），重复地址并入该成员，73仍严格拒收；保存后重复调用保留现有编队 |
| 42 | 完整PPT未领邮件快照；当前支持四种圣灵模板及免费结晶 |
| 43、44 | 免费/付费结晶分别读取 |
| 51 | CardStatus完整十列历史，支持非空快照，保留未知字段含义和原值 |
| 129 | 本地持有物快照，支持已定义ItemID的非空POSSESSION，保留已有账号 |
| 137 | 10组启动配置集合与SYSTEM；POSSESSION、PlayerFilterParam、PEV、ExchangeHistory、GF、FameDepository、PRM、EventPassStatus、PVH经各自的提供者支持非空；RecommendPurchaseList及提供者缺席的组保留空集合边界，已存非空行回503 collection_rows_unavailable |
| 77、81 / 80、83 | 普通主线开战/结算后台初版，范围与未验收项见main-quest-backend.md |
| ntwh57oy | 普通主线失败/撤退的独立确认，原生协议无场次号 |
| AekvKZk6 | 版本状态，明确的本地兼容值 2/400，无维护或限制 |
| jWbtv5NR | 心跳、已采集的资源版本目录、SYSTEM、菜单阻断空集合 |
| foi6moes | 有效QuestTable ID的持久关卡记录；缺失行按未完成补齐，不重置已有进度 |
| E935RTof | 裸 UI 请求，返回有界 ASCII 两整数；本地普通账号规则为0,0 |
| GRs733a4、QxZpjdfV | 两份已导入的公共主表 |

日期使用原生日计数，时间使用 float32 的一天比例。DateTime 使用客户端接受的日历格式或数值文本 0，避免空节点和 ISO 时间引起原生解析问题。

客户端通用解析只保留 I/B/S/F/D 值，DT、I64、P 和嵌套容器只建空列；通用行表接收器（A7EE90/A7F380）遇到空列会让同组后续列写错位。`alxml.GENERIC_ROW_TABLES` 登记了每个注册号都使用该接收器的根级组，编码时这些组只接受上述五种类型且各列等长，否则直接失败；按当前各构造点逐一核对均满足，守卫不改变任何现有回包。PGP、PGP2、REWARD 在部分注册号另有键值或自定义接收器，根级列（34/35/36 等自解析加载响应）也不经该接收器，均不在守卫范围内。

84/130/157由`QuestRecordService`提供：84（通用事件确认）只接受`EventArc.aar`里由DoPeruseEvent命令点名的关卡，对QuestTable里的目标关卡置AchieveFlag的0x40000000位、保留其他位与列，不在QuestTable的关卡只回SYSTEM，未点名的回404 `peruse_quest_unknown`，事件资源不可用时回503 `peruse_quest_catalog_unavailable`，不发奖；130（DailyReproduce）回零行Mission，不读写关卡记录，本地不提供每日复刻任务；157（Shown保存）只接受女神の復活（Type19）关卡，其他关卡回404 `quest_not_found`，置Shown并回完整QR行。84与157缺失记录都按未游玩补行，记录没有变化时不写库，与并发保存冲突时按修订版最多尝试3次。readiness的`quest_records`给出三个接口的口径（`peruse_policy`、`reproduce_policy`、`shown_policy`）与写入口径`write_policy`，以及84可确认的关卡集合`perusable_quests`（`quest_count`与`quest_digest`；事件资源不可用时为`{ready: false, error}`，整项的`ready`随之为假）；关卡服务缺席时整项为`{ready: false, error}`。这一项不计入startup_ready。

按当前资源装配时，serve为156个注册编号中的112个设置处理器，另有7条直连路由（4个独立启动处理器、1个主线退出处理器和2个主表提供器），其余44个回501。同线格式的编号29使用31的处理器，21/22与1/2各自共享同一个处理器对象。这不代表已接入接口的所有分支全部完成。具体进展以`/local/status`的`configured_handlers`与`unimplemented_operations`为准。

配置保存只接受原生已证实的两种字段组，不开放购买扩容和到期时间等其他加载字段。A4是完整signed int32位掩码；73的TL=1则在当前掩码上OR 6，与编队一起提交。73只替换请求中出现的顶层编队，并返回合并后的全部编队；空请求不是删除。72在启动时先于BattleParam版本检查，空编队可能使客户端继续74初始化，不能只实现保存而缺少读取。

119使用已有角色GetIndex索引，只把请求GeneralPurposeFlags的bit1合并到当前值，忽略请求中其余可能过期的标志。该操作的PPU接收器覆盖已有角色，缺少必填列会写零，因此响应包含受影响单位的21列当前记录；该接收器只在第一兵营按GetIndex定位，第二/第三兵营单位的行不回，以免改写第一兵营最后一个单位。与战斗发卡PPU追加语义不同。全部编号及响应容量验证通过后才提交，重复设置或空PU不写入。25分别保存三个有界字符串，按原生固定字段检查UTF-8字节长度；不拆分、不重排字符串，也不把它当成26的新筛选结构。新增可选配置字段不会成为旧编队教程位调用的必填项。控制台`units.set_lock`按(get_index, unique_id)选择单位，任何一个对不上回409 `admin_unit_lock_missing`；它复用119的`set_locks`，只改GeneralPurposeFlags的LOCK_FLAG=2，第二、第三兵营的单位和王子也可以锁，回读41。`config.sort.set`复用25的`set_sort`，写_FlagSort、_FlagMissionFilter、TypeOfSortOrder，回读36。

26的FilterNameHash和SaveValue两列必须等长、哈希唯一，名称可省略且按CRC32归组。SaveValue是逗号分隔的int32选择集合；空串清该组，空Filter不清旧组。服务端按哈希更新并保留漏传组是明确本地兼容规则。存档增加filter_settings_schema=1，旧空集合直接可读，非空未知结构拒绝覆盖。137会先清原生筛选表，因此返回完整PlayerFilterParam快照，名称未知时填空串使三列对齐。26返回SYSTEM和空PGC，不伪造Config.F2。保存前同时编码26确认和137完整启动响应，防止超容量设置使下次登录失败。

物品持有量保存为`tables.possessions`中的ItemID字符串和Count绝对数量，读取时按当前ItemConfig的1370个定义验证；CRC32数值ID不能代替网络字符串ItemID。纯发放辅助返回完整新状态，供结算同一次CAS合并，129/137从同一存档读取。各回包里的非空POSSESSION都带ItemID/Count/Enabled三列（行里没有Enabled时填0），两列旧回执按原字节核对与重放，见`blessing-settings.md`。558种正Stack定义可按明确的本地上限新增；812种Stack=0暂不推断新增发放语义。旧存量高于Stack仍可读，溢出发放不会截断或先提交一半。四种当前主线礼物的发放、协议回读和存档重开已验证。

背包已增加debit纯函数，整批校验已知ItemID、正int32成本与余额，再返回完整深拷贝。数量不足不会扣去其他项目，扣至零仍保留可同步的Count=0。已有Stack=0或高于Stack的余额可消费；不改变新增入账的Stack限制。送礼已复用该扣除层，其他物品的具体使用效果仍需分别实现。

送礼现已接入该扣除层，完整原子更新实例Love/WatchLoveEv、CardStatus、礼盒与去重台账。CardStatus增量是整行覆盖，必须回完整十列；图鉴B0..B4为Unix秒，不能当领奖位。所有好感奖励先进入礼物盒，水晶在领取后才入钱包；单领与批领的PFC格式不同。非零ITEM、未证实的隐藏礼物类别和未知奖励效果仍明确拒绝。其余本地规则、旧档迁移、重试及容量限制见`gift-flow.md`。

75保存主页事件的已读ID：`peruse_state.PeruseCatalog`从EventHomeArc推出脚本里实际执行DoPeruseSave的事件（当前317个主页事件包中135个，含evTrustFemale150/evTrustMale150），只接受这些ID，写perused_events_schema=1及ID/FLG完整记录，不发奖。非零已读标志保留，重复确认不写修订版；137按原生PEV合并恢复全量历史。超出客户端契约的包逐个排除并回503 peruse_event_excluded，其余照常；没有任何包保存的ID回501 peruse_event_unsupported。初始空集合兼容，未知非空schema、CRC身份冲突与越界回包均拒绝。

136保存剧情回顾的已读：`peruse_episode.EpisodeCatalog`读取EventEpisodeConfig，只保留由发送DoPeruseSaveEpisode的主页事件包实际保存的ConfigID（包里给出Save参数时以它为准），当前127条全部可用；_evSampleEpisode.aar没有剧情条目启动它。ConfigID映射到所在剧情的EventID，与75共用同一份PEV和回写逻辑，137读回；EventEpisodeConfig的Exp不发放。不在目录内的ConfigID回501 peruse_episode_unsupported，被排除的回503 peruse_episode_excluded，目录不可用时回503，都不写存档。cli.py用同一个有效资源视图读取器构建75与136的两份目录，readiness的`peruse`与`peruse_episodes`分别给出目录状态；136没有自己的存档域。

单位发放辅助`credit_units`按ExperienceTable累计阈值、cfgRarity倍率及原生float32舍入换算经验，按职业/稀有度上限截断是明确的本地规则。GetIndex从UX递增，UniqueID通过可选last_unique_id高水位避免删除后复用；旧存档在首次发放时从现有最大值初始化。发放前校验完整41号名单的4096人数及原生编码容量，返回本次新增单位供之后的PPU使用，不能将整份名单作为PPU重复追加。

免费结晶辅助按获得日（JST民用日，由`business_calendar.civil_day`给出）更新`tables.crystals`，保留所有付费桶和其他免费日期；`free_crystal_response`输出完整PFC快照。日期沿用原生日计数，旧Date=0保留而新入账不得用0假装永久。单位、物品和结晶组合为同一候选状态后提交的联合测试已通过，重新读取41/44/129/137及重开存档均一致。普通主线后台初版已接入这些辅助函数，实际范围和未验边界见`main-quest-backend.md`。

新存档提供原生默认角色CardID 1，当前主表对应ClassID 9900、SkillID 59；GetIndex 0属于王子的特殊位置，不能把它当作普通编队成员已可用的证据。其他持有物、活动状态和菜单阻断集合为空。非空集合尚未全部恢复，遇到不支持的记录会返回明确错误，不丢弃数据。新增玩家状态通过版本标记及CAS补入旧本地存档，保留已有金额、密钥、教程进度和未知字段。

2026-11-01已采集并导入真实公共主表，尚未完成私服原生客户端主页验收。关卡记录读取仅补充有效主表ID的未完成状态，不选择虚构ID、不授予通关；普通主线成功状态由独立结算处理器写入。战斗的原生验收、新号完整流程及其他模式仍待完成，按开发计划排在常驻功能与活动之后。

首次采集的 CardData 为 2930 行、98 列，QuestTable 为 3582 行、48 列。实际响应分别省略了 schema 中的 5 个 Prize 字段，以及 Idx/R1–R5，保留原始响应，不为这些列补造值。该次历史快照为 `f9ff479309dc38a84ffb59da`；采集正文、manifest 和验证报告位于 `runtime/captures/20261031T164620Z-57436-2ee97bad`。

语义检查中，2930条角色的初始职业引用全部匹配本地ClassData。资源全量补齐后，当前索引的2779个地图包均通过大小、MD5和容器解析；1549条关卡候选匹配所需Entry/Location，包含全部114条主线和9条战术指南关卡。首次新账号教程实际经过主线1001–1004以及教程位0、1，不能用战术指南替代。另2033条关卡要按任务组前缀或别名关联，属于映射逻辑，不是下载缺失或文件损坏；战斗底座按任务分类的地图包命名规则与QuestIdList成员解析这些关联（见`docs/battle-modes.md`），2026-11-03的集成检查中14个启用类型的3582关全部可构建。

2026-11-01版本的24094个资源曾全部复核可用，新增23311个缓存文件、10709723638字节。五个剧情父包与1116个剧情分区已批量导出。剧情解锁、已读回写、奖励和玩法流程仍需服务端实现；资源采集完成不代表私服原生玩法已验收。

## 数据与更新

协议元数据、主表 schema 和客户端 SHA256 随代码保存。新的 EXE 必须重新确认协议和接入位置；程序不会猜测新偏移或继续修改未知版本。2026-11-06已新增5.0.1原版SHA256 `d0be510fb38b1d6dfe63c3562c97e45f49d318bf7e155dee309d88efe963f92b` 的显式档案，保留旧版支持；156条路由与已核对公共管线一致，API/CDN补丁偏移更新为`0xA6D8F0`/`0xA6D90C`。活动主表已切换为`9159442edcc59db92d37856e`（2932/3524行），24019项新版资源已独立汇入`runtime/resources/.cache`。升级备份为`runtime/backups/client-upgrade-20261106T130021Z`；新版副本已准备、验证并发布，目标SHA256为`48ab67a9d3397637f43c716a58ec7ba4745442288f245efb652ffa0d0ff28f96`，有效资源版本为`6d9554288063047475ca96a4626468621480190f`；旧副本保留在`runtime/client.backup-20261106T133440Z-7b08302e`。最终离线doctor就绪检查通过，三批回归失败已修复并补测通过，累计成功执行3822次（含补测重复，非独立用例数），1项跳过，详见`runtime/client-upgrade.json`。本轮游戏和服务端未启动，正式SQLite未读取。相同客户端版本下的新公共主表通过版本化快照导入。

`MasterDataStore`把每份核对过的两表采集保存为不可变快照，`masterdata/active.json`指针决定启动使用哪一份：`import_capture(activate=)`导入采集（命令行`import-masters`默认同时激活，加`--stage`只保存快照），`activate(identifier, expected=)`按预期指针切换，`snapshot`/`status`、`list_snapshots`、`inspect_capture`与`pointer`/`pointer_digest`供控制台只读查看；指针写入共用`masterdata/pointer.lock`，active.json格式不变。控制台导入只暂存，按content档切换后才生效；最近一次切换记录了主表激活而指针仍是旧字节时，serve启动先把指针前滚到目标快照。active.json与快照manifest按严格JSON读取，重复键、非有限数和超过64层的嵌套都判为无效。

主表增加关卡时，可补入新的未完成记录；若新主表移除了已有记录的 ID，重启兼容检查会拦下切换，用控制台的`quests.records.archive_missing`把这些记录归档到`quest_record_archive`侧表（可按ID经`quests.records.restore_archived`恢复，从不删除）后再切换。

存档与资源分开：公共主表更新不会重置账号。SQLite 拒绝其他应用数据库与未来 schema 版本；后续数据迁移需要显式编写。

18个模块只读单张命令表：17个按_ID查键、扫描整表或固定整表字节，package_catalog对OnCommandMenuShop.atb取指纹。只有当所读的键在106张登记表中至多出现一次、而且就在所读的表里时，结果才与原生91DEE0的全表查找一致。`tests/test_native_commands.py`用真实资源列出这些键，评级与图像别名按全部CardData卡号展开，整表依赖按整表的行计入；客户端更新造成重复，或把键移到别的表时，测试失败并点名模块。另有源码扫描测试，要求每个在源码里写出登记命令表名的模块都列入清单。`tests/test_system_literals.py`再按语法树检查全部服务端模块：按名字的带符号CRC32只在`system_literals`里计算，`IsCanUseRating_`前缀只出现在`RATING_COMMAND`的定义里。

## 模块约定

服务与目录用`status()["ready"]`对外报告就绪。消费方只需判断依赖是否就绪时调用提供方的`is_ready()`：它只回答布尔值，不构建完整status（status还要算计数、摘要和说明），结论必须与`status()["ready"]`相同，各提供方都有契约测试；多数实现写成`errors.caught(self._ready) is None`，`_ready`抛出的ProtocolError就是不就绪的原因，消费方据此拒绝时沿用原有错误码。`SelectTicketCatalog`、`UnitGrants`、`SummonGrants`已有的`ready()`是抛出拒绝的检查，所以布尔检查统一叫`is_ready()`。需要status其他字段（资源哈希、内容版本等）的地方仍读status；`native_cdn.status()`只做常数时间计数，启动检查照旧读它的`ready`。

控制台模块只用服务的公开接缝，守卫测试按语法树检查全部`admin_*`模块不访问其他对象的私有成员。被其他模块调用的接缝逐步改成公开名：公开方法委托私有实现，测试对私有实现打的monkeypatch照常生效。已公开的有：`PlayerStateService`的`authenticate`（UI/UK不符回403 `invalid_identity`，MK不符回403 `invalid_method_key`）、`validate_player`、`validate_squads`、`validate_grant_identity`、`read_asset`、只读的`classes`与`cards()`（映射与其中每一行都是只读视图，写入抛TypeError），以及`unit_initializer()`；`PossessionService`的`known`（物品身份判断）、`stack`（物品的Stack上限；未知物品回400 `possession_item_unknown`，校验存档行时回503）、`counts`（返回副本）和`is_ready`；`BootstrapService`的`columns`、`player_row(state)`、`game_param2_fields_response`与`public_master`；任务服务的`preflight_snapshot`与礼包服务的`read_response`，供控制台预检读回。测试对这些接缝只在包装私有实现（monkeypatch）时读取对应的私有成员，其余读取都用公开名，替身提供src实际调用的公开名。仍被其他模块调用的私有接缝有：bootstrap的`_authenticate`等其余接缝，各服务核对依赖就绪、需要拒绝原因时调用的依赖`_ready()`，礼物箱子服务调用的`presents._prepare_reward_rows`，以及委任目录读取任务服务的`_asset`与`_drop_table`。

原生数值基元集中在只依赖标准库的叶子模块`native_numbers`：`INT32_MIN`、`INT32_MAX`、`UINT32_MASK`；`int32(value)`只接受`type(value) is int`且在有符号32位范围内的值，bool不算；`signed32(value)`按32位补码折回有符号值；`float32(value)`做单精度往返。`config_state`与`request_fields`在`player_state`载入时就被导入，所以只能从这个叶子模块取这些名字；其余src模块与测试也直接从这里导入，不经`player_state`、`possessions`、`energy_clock`等同样使用这些名字的模块转手。`local_io`的严格JSON读取（`deserialize`等）与`quest_modes`的任务标志位同样只从所属模块导入；`tests/test_import_owners.py`按语法树沿导入链追到定义处守卫这三个模块的名字，只有所属模块自己的测试可以从被测模块取它导入的名字。src里其余模块不再手写32位边界（0x7FFFFFFF、0x80000000、0xFFFFFFFF、0x100000000、2 ** 31、2 ** 32、1 << 31、1 << 32）：行内判断与手写区间的辅助函数都改用这里的名字，区间辅助函数先用`int32`判断有符号32位范围再比较下界（如`int32(value) and value >= minimum`，抛错的写成`if not int32(value) or value < minimum`）；带`maximum`形参的区间辅助函数（月度容量、投票料理、投票过期、投票报酬、双六状态）保留函数体，默认上界写成`INT32_MAX`，因为调用方会传更小的上限或64位上界。`tests/test_native_numbers.py`的语法树守卫逐个模块检查；移位或乘方的指数写成本模块顶层赋值为31或32的名字时（含元组解包与带注解的赋值，如`bits = 32`后写`(1 << bits) - 1`）同样算手写边界，控制台位表（`admin_fields`）的掩码与位数也取自`UINT32_MASK`；回归、购买、活动签到、每日礼物与通行证自动结算规则构造器的行内上界，以及本地礼包编号与确认键（`purchased_package_id`、`local_package_id`、`confirmation_key`）在`INT32_MAX`两侧的取舍也由这个文件钉住；例外只有`native_numbers`自身、`system_literals`（`command_id`保留手写的CRC符号折叠，有符号CRC守卫按这种写法识别）与`al_format`（`info & 0x80000000`是位标志，不是区间）。64位上界不在守卫范围内。

依赖CardData原始字节的目录（主页背景、自选券、商店成长、银交换、塔兑换、召唤池）用`public_tables.BytesDigest`记住上一次的摘要：传入的仍是同一个bytes对象时沿用，否则重新计算sha256；只记住精确的`bytes`类型，bytearray与bytes子类每次重算。它只省去就绪检查里重复的哈希，摘要值、错误码与内容版本都不变。

## 已完成的验证

- 旧版原始EXE指纹、等长API/CDN前缀、20个API和45个CDN直接引用已核对；当时24094项资源曾复制并校验到旧版独立PC副本。新版准备状态见上文。
- 当前本机启动 schema 与等级表可解析，包含新补充的 QuestRecord 结构。
- 独立服务完成两轮协议登录及重启，每轮20个请求成功，身份、密钥、余额、教程位和声音设置保留；包含142、152、31、114及会话状态的实际字段形状。
- 真实资源和公共主表的HTTP联合测试覆盖72/73/74与21/22，重开存档后分组编队及配置一致，旧迁移不会覆盖新编辑。原子失败、未知字段、容量和CAS冲突另有定向测试。
- 本地资源HTTP读取已验证；后续618920的原生拼接证据纠正了心跳语义：AssetPath必须为版本目录，不能是完整URL。修复已覆盖心跳、启动状态和CLI配置。
- 主表导入覆盖无效字段、必要列缺失、哈希不符、错误 JSON、大小上限、链接路径和并发同版本导入。
- 首次真实采集的两张公共表通过哈希、字段、ID 与协议回读检查；3582 条关卡记录生成后重复读取不重置。
- 原版公共表采集完成后采集器已解除挂钩并退出。独立私服客户端已运行，成功读取本地启动信息表，发现缺失的142号接口并完成修复；用户通过Esc停止窗口操作后尚未进行修复后的原生重试。

定向测试报告保存在项目 `artifacts/*.xml`。服务器进程验证产生的存档在 `tmp/server-verification`，不属于正式玩家存档。
