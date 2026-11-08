# 通用标志上传（138）

`universal_flags_catalog.py`与`universal_flags_service.py`实现138 CSaveUniversalFlags：客户端把本地改过的GF行整批上传，服务端只收资源能推出的名称与取值，单调写入`tables.universal_flags`，137读回同一份GF。cli.py在资源读取器可用时用有效资源视图构建目录与服务并注册138；目录或依赖不可用时138不注册、回501，readiness的`universal_flags`给出`{"ready": false, "error": 码}`，不计入startup_ready。138只写137已校验的GF表，没有自己的存档域，服务缺席时旧存档照常载入。

## 原生格式

请求在公共头后只有并列的`ID:S`与`VALUE:I`两列，同一请求内名称按有符号CRC32唯一，原生顺序为CRC升序；PlatformCode只有一份。检查脏表与构包之间若数据被重置，两列可能都不出现，按无操作处理。回包只登记SYSTEM与GF两个接收器：GF按ID合并、整行替换，未出现的行保留，空GF不清旧表。914B10不读回包内容，在A7FDF0返回后才清空脏表，而A7FDF0只在收到HTTP 200后返回；非200时HTTP层先重试3次、每次间隔5秒，仍失败则66E8B0弹出通信错误并按原包重发，04:00翻日作业还要同步等待保存结束。

## 可写名称

白名单在启动时从客户端实际收到的资源推导，对应原生四个写入方：

- 脚本字面量`SetUniversalFlags("Flags=<名>","Value=<整数>")`：扫描106张命令表的`_Cmnd`、RotationTriggerConfig.Command、HomeTrigger的Expression与Cmnd、RecommendMenuConfig的CompleteExpression与AfterCommand、FilterCommand.Expression、FilterGroup的DisplayExpression与ExecuteExpression。两个参数顺序不限，键名与十进制int32值必须是规范字面量，值即允许值。
- CfgMissionPortalConfigTable的非空FlagName，值1；FlagName为空的条目原生视为已读，不会写入。
- CfgMissionHistoryTable每个GroupId对应的`AppeardHistoryGroupFlag_<GroupId>`（拼写沿用原生），值1。
- RecommendMenuConfig每一行解码后的UniversalFlag，值1；按用户决定全表放行，不按现役脚本可达的组筛选。

当前资源推导出2、13、12、16共43个名称，取值都是1，与研究结论一致。命令列用共享命令键（即CRC32("OnCommand")的大端字节），HomeTrigger、Recommend、FilterCommand、FilterGroup各用自身名称的CRC32大端字节；解码结果须是合法UTF-8，除制表符与换行外不得含控制字符，否则目录失败关闭。

原生98B790只取字符串参数，经A42C20以上限1按第一个`=`分出键与值，所以名称里可以再含`=`，这一点与推导一致；按伪代码，值为空的`"Flags="`会写出名称`=`，推导把它当空名排除，未实测。参数不是字面量、字面参数不是恰好两个或整数写法不规范的写入调用记入status的dynamic_writers：原生会解析多余参数、缺少Value时写0，非规范整数如何换算没有地址级证据，这些写法一律不推导名称。字面值0（单调规则无法执行）、空名、超过255字节（原生行表单元格）的名称记入excluded_writes。二者都只排除对应写入，不影响其他名称。

受保护名称永不经138写入：`PurchasedSkin_`与`PaymentBonusObtained:id=`前缀，以及CRC等于任一SkinShop.PurchaseFlag（当前38个）或PaymentBonus名称（当前3个）的名称。推导出的名称落入受保护范围，或白名单内部出现CRC冲突时，目录以503 universal_flags_catalog_conflict失败关闭；任一来源表缺失、字段不符或解析失败时以503 universal_flags_catalog_invalid失败关闭。资源索引中缺失的命令表按原生视为不存在，status的absent_command_tables列出它们。已登记在资源索引、但本地尚未缓存的命令表（读取回`asset_not_cached`）不算缺表：服务端读不到原生会执行的脚本，目录以503 universal_flags_catalog_invalid失败关闭，138不注册，readiness报告该码。

## 写入与回包

逐行判定。白名单外、受保护、取值不符，以及空名或超过255字节的行不入库也不回显，请求仍回200，含被拒行的请求各记一条WARNING并计入status的uploads计数；空名与超长名先于其他原因判定，日志只记字节数，status不留原文。改回4xx会让客户端弹出通信错误并按原包重发，同一请求每次都会再被拒绝，客户端停在重发循环里。

接受的行只在存储缺失或为0时写入允许值，已有非零值（含负数）保持不变；读者都把非零视为已读，单调写入使重放、乱序和旧包都无害，所以不设回执台账。回包为SYSTEM加本次接受名称的GF行，按请求顺序、取提交后的存储值；没有接受项时只回SYSTEM。没有实际变化时不写存档，修订版不变。服务端不复核Rank、账号天数或推荐日期等客户端条件。

只出现一列、两列行数不等、类型错误、行数超出1至4096、VALUE越出int32、名称或其CRC重复、PlatformCode多于一份、出现其他业务字段时回400 invalid_request_fields，这些形状原生发不出。有接受行时才读取已存GF：存储里已有同CRC的其他名称回409 universal_flag_name_conflict，GF满65535行仍要新增时回409 universal_flag_capacity，存档损坏回503 universal_flags_invalid；只含软拒行或两列都缺省的请求不读GF。138回包或写入后的完整137回包超出原生接收容量时回500 response_too_large。以上情况存档均不改动。被拒请求记WARNING；原生客户端弹出通信错误并按原包重发，直到服务端接受。

提交前预检138回包与完整137回包，并核对137的GF与存储一致，再以CAS提交，冲突最多重试3次；重试时按最新存档重新判定，所以与156交错到达时保留对方写入的购买标志。

## 存档兼容

存储沿用`tables.universal_flags`的ID、Value行与`universal_flags_schema=1`。旧档的无版本空集合在首次接受写入时补上版本；未知历史名称、Value 0与负值及原有行序原样保留。138只新增行或把0改为允许值，不删除、不改名、不降值，156写入的购买标志不受影响。

## 原生一致与未证实

请求与回包形状、GF合并语义、四个写入方的名称来源、解码键和受保护名称的读取方式来自客户端代码与资源。软拒、只收1或脚本字面值、单调写入、推荐已读全表放行、免回执和不复核条件都是本地规则。官服138的回包与校验方式、官服137是否带有本地资源推不出的名称、重试用尽后的界面表现、首页快捷入口与轮播横幅的SetAlreadyFlag是否真的写入任务入口标记、Filter表达式是否走同一脚本命令表，都尚未证实；提示不再弹出、NEW标记不再重现需实机验收。

## 验证

目录117项、服务41项定向测试通过，覆盖真实资源推导、合成资源的各类失败关闭、解析边界、255字节边界、按哈希保护的购买与付款标志、超长名称不进日志与status、单调写入、软拒、CAS交错、GF容量与137/138回包容量、依赖失效、原生编码HTTP与重开后137读回。变异检查中仍能存活的只有不可达的防御分支：解析器已拒绝的重复列、行数对齐后多余的VALUE上限，以及目录不可变时每轮重试的就绪复查。没有运行全项目测试，没有启动游戏、客户端或服务器。
