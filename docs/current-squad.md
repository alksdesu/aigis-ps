# 当前普通编队保存

120已接入当前20支普通编队及突袭组的选择、名称、称号及教程标志保存。两份当前菜单资源均明确MaxTeamCount=20，服务核对编号0..19的按钮映射，PlayerStateService的TN读取校验也采用同一范围。TGI其他历史组仍可读取，不因普通范围改变而批量删除。

## 请求与回读

请求必须有OI:I或RTN:I其中一个，以及A3:S和A4:I，可选TL:I=1。OI是普通顶层编队编号；RTN是突袭顶层编队编号，须等于两份突袭视图资源一致声明的组（当前64），否则回400。A3为名称，A4是完整PlayerTitle物理编号，不是位域或GroupId。响应只有SYSTEM和空的更新型PPU，不新增单位，不追加没有接收器的TGI/PGC。

保存更新BattleParam.TN（RTN请求改为更新RTN，TN不变）及目标TGI顶层组的全部Name/Flags；地址、成员、子队和其他组保持。目标组没有记录时，以team0、slot1..15、UnitIndex=-1生成15个空槽，供以后72重建名称和称号；这是明确本地持久化表示，不是线上120发送了15行。

39回读当前TN与RTN，72回读完整TGI，36回读请求TL后MenuTutorialFlags按signed int32 OR6的结果，41角色字段保持。所有这些完整响应和120本次响应在提交前编码检查；39、72、41的检查直接调用PlayerStateService读取时所用的同一编码，预检字节与客户端随后读到的回包相同。请求带PU明确返回501未支持；原生当前路径不发送它，不把旧AF更新当成20队成员保存。

## 名称、资格与旧数据

新名称采用最多12个Unicode标量，并拒绝CR/LF、反斜杠和单引号；不按12个UTF-8字节截断。初始空名称可保留，已有有界旧名称原样回传不强制收紧；读取TGI继续沿既有128字节上限。原生字体的完整字形校验尚未复现，因此这属于明示本地输入规则。

称号选择调用EmblemCatalog.require_eligible，不用提示列或物品展示编号推断解锁。73的新组/新A4也使用同一资格校验；未改变的旧A4和74导入继续兼容，见emblems.md。

如果legacy_squad_imported=false、没有group0、且任何旧OrganizationIndex2非零，120会拒绝并等待现有74导入路径；不把待迁旧成员掩盖为空队。其他成功120设置legacy_squad_imported=true，防止晚到74覆盖新元数据。AF/A8及角色其他21个字段不改变。

## 控制台保存

控制台`squads.save`把成员、元数据、当前选择分成三个可选部分，至少给一个，同一次提交里依次应用。组号只接受客户端的普通0–19队和突袭组：0–254内的其他组号回400 invalid_request_fields，超出该范围时参数校验回422 admin_params_invalid。元数据复用120的`CurrentSquadService.set_metadata`：新名称不超过12个标量，禁止换行、反斜杠和单引号；称号是PlayerTitle物理行，用EmblemCatalog校验资格；目标组为空时生成team0 slot1–15的空槽。控制台还拒绝原生XML装不下的字符，回422。当前选择复用`select`：0–19写TN，突袭组写RTN。成员复用73的`PlayerStateService.replace_squads`严格模式：成员必须是第一兵营持有的单位，已消耗的单位回400 squad_unit_missing，不会被静默清空；没列出的槽位删除；名称和称号沿用该组唯一的已存值，不唯一时回422 admin_squad_metadata_required。旧编队待导入时任何部分都回409 current_squad_legacy_pending。保存后legacy_squad_imported为真，不写TL，不生成120回执，回读39/72；提交推进修订版，已由服务器提交的120原包重发时回409 `current_squad_retry_stale`。GetIndex 0（王子）在客户端编队中不显示，控制台对此给出警告。

## 原子性与验证

元数据、TN或RTN、TL和有界回执在同次CAS提交。每次新成功操作记录回执，精确重试要求提交revision、内容版本、状态摘要和规范响应一致，不回退后来的选择。并发重读后会重新核对资格和迁移状态；保留16条回执、最近256个已处理请求摘要，CT不作排序，旧版水位历史读取时自动升级。

定向检查覆盖OI19、20越界、跨子队元数据、空槽、未解锁称号、两个资源映射例外、负高位TL、旧AF导入与晚74、容量回滚、精确重放及并发。真实资源HTTP验证了120→39/72/36及重开恢复，未启动游戏或监听服务。

原生契约见aigis_research/permanent-20261101/current-squad-contract.md。测试为artifacts/current-squad-service-tests.xml、current-squad-service-boundary-tests.xml、current-squad-integration-tests.xml和current-squad-player-regression-tests.xml。原生界面仍待用户恢复实机后验收；非空PU及特殊组另留范围。

本批编队、称号及相关旧功能共458项最新定向结果通过，汇总于artifacts/team-emblem-verified-tests.xml；报告保留来源，修正夹具后只重跑受影响项。artifacts/team-emblem-readiness.json确认当前服务、称号目录与启动依赖就绪，未启动监听器或游戏。
