# 主页背景保存

135现已支持普通主页菜单的背景选择，137恢复完整PRM。当前资源共有53条定义，其中43条属于普通菜单的默认、活动、私室和固定四类；可选还受当前时间与秘书资格约束，不表示43条同时开放。其余10条（兵舎、神殿、酒場、塔、名声）是其他页面的场景背景：这些页面只经ChangeBg等脚本显示，原生不经135保存，背景选择菜单是135的唯一发送方，因此保存请求以501 background_unsupported明确拒绝，与原生一致。

目录从PrivateRoomConfigTable.atb读取，验证字段、DefineName与ID的CRC对应、重复、日期和CardData引用。规则不把当前条数作为未来版本的硬限制；主表、目录与命令表进入固定内容指纹。目录名与新保存的配置名最多255个UTF-8字节（原生行表单元格）；按旧256字节上限保存的行仍可读回。

## 可选类别

可选类别从资源推导。按原生顺序沿CMenuEtcExchangeBg、CmnMenu作用域查找OnPreAssemble与OnAssemble（106个命令表，复用NativeCommandIndex），只接受纯字面量的`MnObj_SetParam("FilterType=Category")`与`MnObj_SetParam("FilterItem=…")`，同一键按脚本顺序取最后一次；写这两个键的脚本只能由字面量MnObj_SetParam语句组成，这是本地失败关闭规则：其他语句可能让赋值带条件。FilterItem按逗号原样切分、丢弃空项，每项须是目录中已有类别的名称。任一键未写或切分后没有非空项时，与72DAC0一致使用可执行文件在缺省时硬编码的四类，不再检查另一个键的值。

两个键都已写且有非空项时，FilterType不是Category、名称不命中、数字项、首尾带空白的项均以503 background_catalog_invalid失败关闭。以下来源问题同样失败关闭：非字面量的MnObj_SetParam、写过滤键的脚本含其他语句、命令行重复，或上述脚本之外的任何命令行以全键文本、CRC32数值键、`FilterType=`/`FilterItem=`参数写这两个键（后者不区分所属类，所以被本类行遮蔽的CmnMenu行也会触发）。资源索引中缺失的命令表按原生视为不存在；存在但读取失败或格式不符的表同样失败关闭。已登记在资源索引、但本地尚未缓存的命令表（读取回`asset_not_cached`）不算缺表：服务端读不到原生会执行的脚本，目录同样以503 background_catalog_invalid失败关闭，由readiness报告。

当前资源推导自OnCommandMenuOthers.atb第62行，结果与回退四类相同，行为不变。若资源在FilterItem中加入兵舎，system_unit即可经135保存并由137读回。status的categories报告来源（resource_script或executable_fallback）、脚本所在表与行、表SHA256和缺失的命令表。内容指纹因此变化一次，升级前在途的135精确重试会返回409过期，不影响存档。

## 保存与恢复

原生请求为CardID:I与ConfigID:S两列。固定背景system_fix只更新全局键0；其他选择在有有效秘书时同时更新0和秘书的归一CardID。先按Config._GuideUnitIndex找到GetIndex实例，再用原CardData.OriginID非零值、否则原CardID。C1=0仍会查找GetIndex0，初始王子存在时默认请求为[0,1]；仅实例不存在才视为无秘书。此路径与121取消后的显示回退不同，不使用图像别名，也不限制秘书仍在第一兵营。

目录CardID非零时必须匹配当前秘书。开放窗口与皮肤商店同源，按客户端整秒时钟判断（服务端时刻先落到客户端4 ms网格的整秒，再与Begin/End比较），两端包含（内容版本的窗口规则名为`inclusive_native_clock`，旧的JST时刻比较版本在可重放集合里，旧回执照常精确重放）；SaveTime按原生格式保存到秒。服务不重写客户端背景优先级算法。

状态使用tables.backgrounds与backgrounds_schema=1，每行为CardID、ConfigID、SaveTime。135返回触及的完整行，137返回全部行。按CardID替换对应完整行，保留其他角色设置。已版本化的未知历史名字可读回，由原生处理过期或失效后的回退；新提交必须在当前目录并通过资格检查。无版本空表兼容，不自动清空或猜测非空未知结构。技术容量为4096行。

## 时间、重试与事务

每次新选择都会记录当前JST时间；同一背景的新点击也有意义，因为SaveTime决定是否让更新开放的活动背景覆盖它。两次新动作落在同一秒时，即使三列数据一样，也分别持久记录回执。完全相同的同秒原包无法区分为两次动作，按一次请求处理。

精确重试需匹配已提交修订版、内容版本、状态摘要和重建响应；不会重新读取时钟或刷新日期。发生其他状态变更后的旧包返回过期冲突，不用过时PRM覆盖客户端。回执保留16条，另保留最近256个已处理请求摘要，客户端CT只是墙钟时间，不作排序；回执淘汰后重复的已处理请求返回409 background_request_stale，旧版CT水位历史读取时自动升级。

成功候选先验证135及完整137的原生回包容量，核对137提供器的PRM与实际存档一致，再以CAS提交。重试会重验当前秘书、状态及资格；一个动作只捕获一次服务器时间。并发同包只提交一次。服务器时间早于既有保存时间时拒绝提交，避免优先级倒退。目录、状态、保存和回读均不改钱包、名册、剧情或奖励。

## 异常PRM诊断

`BackgroundService.diagnose(state)`只读标注已保存的行，最多列出32条并给出各类计数：unknown_name（不在当前目录）、page_background（属于未开放的页面类别，主页会直接显示该页面背景）、fixed_outside_global（system_fix存在非0键下）、non_normalized_key（键不是任何秘书经一次OriginID换算得到的身份，含主表不存在的CardID，原生永远读不到）、private_room_mismatch（私室名存在别的非0键下）。0键下的私室行是原生135的正常写法，不标注。诊断不判错、不改写存档，137照常回读全部行，135只覆盖触及的CardID。修复由操作者通过控制台显式执行，本服务不自动迁移。

doctor的`readiness.saved_backgrounds`对正式存档只读运行同一诊断，给出rows、abnormal_rows、kinds、findings（最多32条）和truncated，读取失败时为`{"error": 码}`；只在背景服务可用且已存体力时钟就绪时出现，不计入startup_ready。控制台视图`settings.backgrounds`分页列出已存行并附带同一诊断。控制台`backgrounds.merge`按CardID合并完整行（CardID、ConfigID、SaveTime），只写入或覆盖，不删除已有行；行必须是当前目录中可选的背景，否则返回409 `admin_background_rows_unsupported`并列出findings。SaveTime由操作者填写，格式为`YYYY/MM/DD HH:MM:SS`（JST），不能晚于服务器时间，否则返回409 `admin_background_save_time_future`：原生保存拒绝比已存时间更早的选择，未来时间会锁住该菜单。

## 验证与剩余边界

定向结果均通过，没有运行全项目测试：

| 范围 | 数量 | 结果文件 |
|---|---:|---|
| 目录、身份和日期 | 102 | artifacts/background-catalog-tests.xml |
| 统一名称字节边界 | 4 | artifacts/background-catalog-boundary-tests.xml |
| 纯状态与PRM | 97 | artifacts/background-state-tests.xml |
| 协议、回执、事务与并发 | 83 | artifacts/background-service-tests.xml |
| 真实资源HTTP及重开 | 11 | artifacts/background-integration-tests.xml |
| 启动提供器及CLI初始化 | 7 | artifacts/background-startup-tests.xml |
| 既有提供器与集合兼容 | 37 | artifacts/background-related-regression-tests.xml |

artifacts/background-doctor.json确认当前目录和服务就绪；artifacts/permanent-interface-inventory.json记录135已装配。原生证据及交叉审查分别见aigis_research/permanent-20261101下的background-settings-contract.md与background-service-review.md。

资源推导类别、页面拒绝、开放兵舎后的135/137往返与异常行诊断由test_background_catalog.py和test_background_service.py覆盖。

本批未启动游戏、重启服务、修改原版或实际存档。背景画面、切换动画及原生重登仍需实机验收。其他页面原生只显示不保存，本服务不提供页面背景的保存、回放或调度；异常PRM行只诊断不迁移。FilterItem切分不去空白、空SetScene脚本视为成功均为静态读出，未经实机验证。
