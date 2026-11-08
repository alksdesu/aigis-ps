# 称号道具与资格

当前83种get_emblem道具已接入现有Type22邮件领取。它们均为唯一库存物品，Stack=1、CanAllGet=0、NoAutoDelete=0；已持有时重复邮件保留，不消耗后丢掉奖励。单领是按已确认的本地政策执行，不冒称原生117一定主动过滤这些邮件。本地117批领会跳过称号邮件并保留。

EffectType10下的另外36种物品是投票券，属于其他Category且可堆叠，不在称号领取范围。PresentItemCatalog只接受与EmblemCatalog来自同一ItemConfig快照的明确物品集合，比较来源哈希、名称和展示编号；缺少称号目录时保留既有物品能力，不把整个Effect10放开。默认服务的库存邮件目录现为479项。目录与库存一样按256字节旧上限读取物品名；名称超过255字节原生单元的称号道具不进入邮件目录（列在status的`unsupported_items`），以它为条件的HasPossession仍按已存库存判定。

领取增加绝对POSSESSION.Count并删除成功邮件，之后42/129/137读回；不主动改队伍、王子实例、技能、剧情、GF或任务。重新打开称号列表时，客户端按资格重新计算已得状态。

## 编号与解锁条件

PlayerTitle当前125行、59个组，网络编号是物理0-based行号。GroupId和EmblemLevel只用于分组与等级，A4=0也是有效默认称号。EmblemCatalog保留效果及条件元数据，仅解释当前已有的三类资格表达式：

| 表达式 | 判定 |
|---|---|
| 0xe52a3bdd(n) | 玩家Rank >= n |
| 0x441add5b(q) | 指定QuestRecord的AchieveFlag包含bit24 |
| HasPossession("ItemId=name"); | 精确物品Count > 0 |

不执行任意脚本，不将Condition提示列额外全部AND。未知表达式保留记录并明确不支持，不作为已解锁。当前125行均可解释；旧已通关记录即便对应任务已从公开主表移除，也仍可满足称号条件。

有两处必须保留的资源区别：get_emblem_00034的展示EmblemID是70，但实际按持有解锁的是称号71，称号70仍要求关卡7808；get_emblem_10018展示123，而123仍要求关卡45002。不能从物品EmblemID反推全部资格。

120选择称号必须通过同一目录。73成员保存中，新组或变更到新的A4也校验资格；同组旧A4原值回传、74历史导入继续保留旧数据。把未解锁的旧A4复制到新组并不享有兼容豁免。

## 验证与边界

目录检查覆盖全部当前物品、125行资格、物理编号和资源例外。HTTP检查覆盖取得后可选择、旧邮件/库存读回、重复领取不损失邮件、批领拒绝、排除投票券，以及73入口的新称号校验。

证据在aigis_research/permanent-20261101/emblem-items-contract.md；测试报告为artifacts/emblem-catalog-tests.xml、emblem-catalog-boundary-tests.xml、emblem-rewards-tests.xml及emblem-member-save-tests.xml。修正夹具后仅复跑受影响用例，原报告保留。没有运行全项目测试或游戏。

与编队、邮件旧功能的458项定向结果统一保存在artifacts/team-emblem-verified-tests.xml；新增73验证包含同组旧值兼容与复制到新组时重新核对资格。

本批只保存资格与选择。Effect/EffectPower到王子显示形态和战斗能力的完整运行效果仍属后续战斗验收，不以取得道具等同于已验证全部加成。控制台实现时应复用本目录而不是写一张永久全解锁表。
