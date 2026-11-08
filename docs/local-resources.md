# 私服有效资源视图

原始公共资源和缓存保持不变。`load_resource_view`读取已验证的基础索引及CardData，在内存生成私服需要的改动，再由服务端业务目录、本地CDN和客户端准备工具共用这一视图。

当前常开两项修改：PresentUnitData.atb追加初始角色模板（本节）；Shop2ItemV2.atb把客户端只对其发108的商品（ProductType 2且Type 4，按规则选取、不写死ShopID）的表头行销售窗口改为1/2毫秒（见下一节）。按配置才生效的覆盖各见其文档：活动通行证复刻及其礼包文案（`event-pass-revival.md`）、活动签到复刻（`event-login-rewards.md`）、购买后每日领奖礼包文案（`purchased-login-rewards.md`）、任务窗口复刻（`mission-windows.md`）与命令文本覆盖`command-text-overrides`（`entrust.md`）。

PresentUnitData.atb保留原始2039行及其字节，追加四个初始角色模板。ALTB头、schema和原记录保持，ALL4压缩形式及参数保留。新模板ID固定，不按当前最大ID重新编号：

| 原CardID | 本地PresentID |
|---|---|
| 1605 | 2000000001 |
| 1777 | 2000000002 |
| 1785 | 2000000003 |
| 2761 | 2000000004 |

原生按有符号32位PresentID查找，并不把它用作数组下标。已有同ID、同内容时不重复追加；同ID不同内容时拒绝加载。后续官方即使增加同卡的低ID模板，本地旧ID仍保留，已经入箱的邮件不会变成另一角色。

这些本地模板只作发货途径，不当作官服发放证据：投票目录判断同名候选身份时按`LOCAL_TEMPLATE_CARDS`的PresentID与CardID同时匹配排除这些行，因此有效视图与原始资源选出同一组候选卡号，见`vote-events.md`。

## 商店召唤108的销售窗口关闭

有效视图常开这一覆盖：读取基础Shop2ItemV2.atb，按与`TradeCatalog`和原生819320相同的规则划分商品（TradeCount比上一行严格递增的行并入上一商品，作为续档行），选出表头行ProductType为2且Type为4的商品，把表头行的StartDateTime、EndDateTime写成1和2（从0001-01-01日本时间起算的毫秒，即最早的、已结束的非空窗口；0表示不限期）。按规则选取，不写死ShopID；当前只选中ShopID 15「レジェンド召喚」（表头行下标2676，3个价位档，原窗口两端都不限）。表的列名、类型和顺序必须与原生一致（客户端按列位置读取），TradeCount必须为正，所选商品的ShopID必须为正且不重复，否则拒绝构造有效视图。写入后重新解析，确认只有这两个单元格改变，ALL4容器参数保留。这一覆盖在所有可选覆盖之后执行，并登记占用Shop2ItemV2.atb；其他覆盖已替换该表时拒绝。

原生在售判断818200对窗口已结束的商品返回假：两个商品列表都不生成它，购买入口80ACF0弹出原生的期间外提示，不发出108。所以不造卡池，不伪造成功，108仍然没有处理器，回501。关闭的ShopID、原窗口、源与结果SHA-256由`ResourceView.shop_window_closure_metadata`给出，readiness的`shop_window_closure`照录（视图里没有商店表时为`{enabled: false}`），不计入startup_ready；客户端准备工具把它记在清单的`shop_window_closure`节中，verify与launch时核对，控制台`runtime.versions`的`client_manifest.overlays`显示已准备的客户端是否带这一节。依据见aigis_research/console-20261102/empty-summon-schedule.md第7节，未经实机核对。

这一覆盖改变了有效版本（本机由`fa42d5e7b63838d7b03e2f3b11a2f9f71ec9d177`变为`c69b95cddfc67d06987ece5be71e4615c2073767`）。已准备的客户端必须重新运行准备工具，启动工具会因版本不符拒绝旧客户端；正在运行的客户端会在下一次菜单心跳后回到标题、重新下载索引。所有以有效版本为身份的目录内容版本都随之改变：升级前提交、升级后才重发的回执按既有规则视为过期（普通交易除外，见`trade-flow.md`）；升级前开始的战斗在结算时回409 `quest_snapshot_changed`。升级前请先结束或退出进行中的战斗，并等挂起的请求完成。

## 索引、哈希与缓存

有效版本由基础版本、基础索引哈希及实际替换内容共同决定。只有变更条目的group、MD5和size更新，所有条目顺序、flags及原生缓存键保持；生成的PC索引重新解析校验。没有实际内容变化时保留原始版本和索引。

业务读取和本地CDN从同一有效索引取得替换内容。内存替换同样校验size/MD5，不允许用替换机制绕过哈希，也不回写原缓存。/assets逻辑名称读取与/client-assets对象读取返回相同字节。

读取失败按原因分码：名称不在当前有效索引（/assets另含`--asset-manifest`补充的资源）中回404 `asset_not_found`；已在索引中、但资源库缓存和原版目录都还没有这份正文时回404 `asset_not_cached`，补齐缓存后即可读取；找到的正文与索引的size/MD5不符时回409 `asset_changed`。/client-assets的对象不在原生快照中时回404 `cdn_object_not_found`，候选正文都没缓存时同样回`asset_not_cached`。主页背景与GF通用标志目录读取原生命令表时，只把`asset_not_found`当作原生客户端也会跳过的缺表；已索引但未缓存的命令表对它们算不可用，两者因此报未就绪（`background_catalog_invalid`、`universal_flags_catalog_invalid`），应先补齐资源缓存。委任目录经任务目录读取命令表，缺表与未缓存的表都记为`quest_asset_unavailable`并跳过（见其status的`commands.missing_tables`），行为不变。命令索引的内容版本仍把未缓存的表按缺表记录，已有内容版本和旧回执不受影响。

启动在创建App或存档前检查有效资源/CDN及启动数据就绪。`--asset-manifest`只能补充有效索引以外的资源；已索引逻辑名或64位缓存键始终使用有效视图，校验失败不会回退旧清单。

客户端准备计划、实际复制和复制后的版本守卫共用有效视图，替换内容及对应.md5进入独立客户端缓存。`--master-dir`或`prepare-client.ps1 -MasterDirectory`可指定配套公共主表；默认仍在111项目下。启动工具比较已准备客户端与服务端的有效版本，避免混用。

## 更新和当前状态

更新基础资源与公共主表后，重新构造有效视图，检查模板冲突，再准备对应的独立客户端。有效版本因此改变时与“商店召唤108的销售窗口关闭”一节同理：更新前先结束或退出进行中的战斗、等挂起的请求完成，更新后重新运行准备工具，启动工具会拒绝版本不符的旧客户端。索引下载工具仍管理原始公共索引；不会把本地派生索引伪装成官方下载记录。

2026-11-01的历史独立客户端缓存按商店召唤窗口关闭前的有效视图重建：版本`fa42d5e7b63838d7b03e2f3b11a2f9f71ec9d177`，24094项资源，48193个文件。准备工具完成大小及哈希校验，旧副本保留在`runtime/client.backup-20261101T002431Z-fc7f65df`；未修改原版客户端。历史记录见`artifacts/client-resource-refresh.log`。2026-11-06已启用基础资源版本`86164496048d9b4329259cfb248a9d3ff283bb36`，24019项已独立汇入`runtime/resources/.cache`；升级备份位于`runtime/backups/client-upgrade-20261106T130021Z`。新版副本已准备、验证并发布：有效资源版本`6d9554288063047475ca96a4626468621480190f`，24019项正文共11,519,326,920字节，旧副本保留为`runtime/client.backup-20261106T133440Z-7b08302e`。详情见`runtime/client/client-manifest.json`与`tmp/probe/new-resources/prepared-client-upgrade.json`；本轮游戏和服务端未启动，正式SQLite未读取。

这份缓存当时的离线启动校验已通过，见`artifacts/client-resource-refresh-verification.json`。该操作使用`launch --dry-run`，没有启动游戏、请求监听服务或重启现有服务；原生画面仍待用户恢复实机后验收。

模板52项、索引47项分别通过定向检查；有效视图、CDN与二觉领取关联97项通过。`artifacts/resource-launch-contract-tests.xml`记录准备工具和启动版本守卫的56项通过、1项跳过；包含真实服务器健康响应大小及新旧版本拒绝行为。所有检查只使用后台请求或小型临时客户端夹具。
