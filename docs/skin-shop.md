# 独立服装商店

156已接入独立SkinShop交易。当前38条商品中36条符合可发放物品定义；新版get_skin_00046/00047已有Stack0定义，可识别已持有库存的解锁，但不允许新发放、邮件领取或购买。2026-11-01日本时间的有效窗口内有10条，其中8条可发放。历史商品保留目录，但不绕过售卖时间。

## 原生契约

请求为SKIN/ShopId:I，实际值取SkinShop.ConfigId，允许负数；不能使用RangeKey、DisplayId或真正的SkinId代替。服务端读取价格和奖励，不接受客户端自报金额、数量或收货角色。

购买成功回SYSTEM、完整PFC/PPC钱包快照、对应POSSESSION（ItemID/Count/Enabled，缺省行为0）、GF，以及空SkinShopPurchase容器。专用结果对象没有已证实的业务字段，不能虚构Result/Success列。购买后客户端立即重建可见商品和外观缓存，因此本包携带全部权威变化，不依赖额外请求补状态。进入商店前的126读取已由现有银仓接口提供。存档中只有两列POSSESSION的旧回执仍按原字节重放。

商品名SkinNNNN、外观名skin_NNNN、购买标志PurchasedSkin_NNNN使用ButtonImageId作为编号。三组名称是从哈希关系推导，并对当前38行逐项核验，并非从明文源码抄出。新资源不匹配时明确拒绝；没有使用任意CRC碰撞别名。已有存档若有同哈希标志，继续保留其原名称。新写入的标志名须在255字节原生单元格内；超出且存档中尚无该名称的商品以501 skin_shop_product_unsupported拒绝，不扣款，并照常记录拒绝日志。按旧512字节上限已保存的同名标志仍可更新。当前PurchasedSkin_NNNN均远小于该上限。服装物品名与库存一样按256字节旧上限读取；购买会新建库存行，名称超过255字节原生单元的商品保留在目录中但标为不可购买，同样回501 skin_shop_product_unsupported。

## 原子购买

按照已确定本地政策先扣免费结晶，再扣付费结晶，同类先扣较早取得的桶。售卖窗口按客户端整秒时钟判断（服务端时刻先落到客户端4 ms网格的整秒，再与OpenDate/CloseDate比较），开始及结束时刻均包含（内容版本的窗口规则名为`inclusive_native_clock`，旧的JST时刻比较版本在可重放集合里，旧回执照常精确重放）；空日期表示对应方向不限制。成功后只授予一个唯一服装物品，并将对应GF标志写为1。

两个目录公开的`resource_sha256`里ItemConfigTable.atb的口径不同：商店目录（SkinShopCatalog）是原文件字节（可能是ALL4压缩后的）的哈希，外观目录（UnitSkinCatalog）是物品定义解压后数据的哈希，等于持有物服务的`asset_sha256`。就绪检查按持有物服务`is_ready`并以解压口径核对其`asset_sha256`、外观目录`is_ready`且内容版本未变、再比对SkinPackage.atb哈希的顺序进行，任一不符回503 `skin_shop_catalog_changed`。156与status在此之上还做服务级就绪检查：依次要求商店目录、持有物、任务服务与外观目录都已就绪，可购商品的物品都在外观目录的计数物品里，任务进度的服装物品集合与外观目录一致，两个目录的SkinPackage.atb哈希相同，内容指纹未变；任一不满足回503 `skin_shop_dependencies_changed`。

已有正数量物品，或购买标志为任意非零值，都会拒绝再次付款。钱包不足、时间窗关闭、缺物品定义、取得任务依赖缺失、响应容量不足等均在提交前失败。扣晶、物品、购买标志、skin_acquisition_history、mission_state和回执使用同一次CAS保存。角色实例和当前外观选择不自动改写；取得后可通过既有10号详情接口选择外观。

156现同时累计真实结晶月消费，并把消费政策加入内容指纹。该接口没有PGP2接收器，因此不添加无人接收的C1回包；后台数值由35/33/115后续读取取得，不能据此承诺购买界面立即刷新印章消费。新动作的目录、任务和交换历史预检使用同一捕获时间；缓存只验证已提交状态，不重新取时钟或再次累计，详见login-rewards.md。

独立skin_shop_history使用共享请求账本：有界回执加最近256个已处理请求摘要，不按CT排序，旧版水位历史自动升级；被拒的156写WARNING日志；原生7C32B0只在A7FDF0收到HTTP 200后才执行ExecutePurchaseComplete，被拒时弹出通信错误并按原包重发，直到服务端接受。重复请求只在提交修订版、依赖及状态摘要一致时重建原响应。任务的可选依赖（名声登记、道具券、EP积分、关卡通关规格）在回执提交之后才接入时，回执记录的任务内容版本按任务服务给出的可达版本集合核对，旧回执照常按原字节重放；皮肤商店要求任务带服装取得，服装取得不会后接入。任务的可达版本还包括名声目录与礼物箱道具定义的嵌套变体（妖精乡、可写的银之馆或妖精乡的额外兵营容量后接入，称号目录后接入），以及任务与通行证窗口按JST时刻比较时的旧窗口变体，见`missions.md`。这些依赖被移除或版本变化，以及其余依赖变化时，仍回409 skin_shop_retry_stale。已成功交易在售卖窗口结束后仍可作精确重放，不会再扣款；新购买继续检查时间窗。

## 通用标志

GF使用ID:S、Value:I，按ID的CRC32匹配。Value允许有符号整数，不能把整个集合简化成布尔表。空GF不清旧值，写0才明确撤销对应标志。客户端会把ID再次作为字符串写回，因此不能仅凭整数ID可查到就把PurchaseFlag哈希用ID:I长期存入。

universal_flags_schema=1标识非空本地存储，旧空集合不需改写。共享层检查非法名称、重复和CRC冲突，保存时保留无关标志。137通过独立provider返回同一份持久化GF；缺少provider时非空数据明确失败，不伪装成空集合。没有推测或重置旧的未知非空结构。

138上传服务只写资源推导出的白名单名称；PurchasedSkin_前缀及与SkinShop.PurchaseFlag同哈希的名称一律不经138改写，购买标志只由156写入；规则与接线状态见universal-flags.md。

## 验证与边界

通用标志26项、目录72个独立用例、交易55项、原生编码HTTP和重开8项、旧启动数据回归8项通过。目录初轮一个ALL4测试夹具错误修正后只重跑该项，两份真实报告均保留。报告为artifacts/universal-flags-tests.xml、skin-shop-catalog-tests.xml、skin-shop-catalog-all4-tests.xml、skin-shop-service-tests.xml、skin-shop-integration-tests.xml和skin-shop-bootstrap-regression-tests.xml。

启动诊断见artifacts/skin-shop-doctor.json。没有启动游戏或监听服务，没有改写原客户端或正式存档，没有执行项目全量测试。原生菜单点击、外观素材渲染和完整主页运行仍待实机验收。
