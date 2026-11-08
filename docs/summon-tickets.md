# 召唤券邮件

Type16普通签到券350已完成后台入箱、读取和单封领取。普通召唤菜单的CSS/CSP官服调度仍未采集，也未因本功能而开放：70/71回显式空调度（无阵容、无召唤活动、无召唤包），召唤12/13仍不实现，不生成替代卡池，见`schedule-capture.md`。

`PresentService.enqueue`接受`kind=summon_ticket`、`pool_id`、`count`和`description`。count拆成独立邮件，每封Content=1、SummonID为卡池号。42的PPT使用A4:I输出SummonID，混合邮件在该列填0；旧邮件的字段与库存行为保留。旧的Type16 Content>1邮件与道具券、自选券一样可用控制台的`presents.split_ticket_mail`拆成逐张邮件后领取。未拆前这类Type16邮件会让42与整个邮箱回503 `present_state_invalid`；有多封时要在同一批命令里全部拆完，`presents.mailbox`视图的`bundled_tickets`列出待拆的邮件。

领取只接受原生1/2等价协议的单个RT/PT2/PresentID，不接受客户端提交卡池或角色；1/2多封混领明确拒绝。117批领跳过召唤券，券邮件留待单封领取，不抽卡也不报错；原生批领列表是否会带上这类邮件仍未证实，跳过属于本地政策。成功回SYSTEM、PBP的UX以及一个完整21字段新增PPU，客户端随后读取42移除邮件，并由新增单位列表播放专用召唤演出。不新增IOF、RML或虚构结果组。

## 卡池与默认规则

SummonConfig.atb按非零_ID开组、0续行解析，当前479池，其中187组概率完整。完整性容差为总和距100不超过0.001个百分点，用于资源概率小数的表示误差；不完整池拒绝抽取。随机权重保留原始浮点值的精确二进制比例并转换为整数，不把292个残缺组重新归一化。

默认启用350：508个候选，原始概率总和100.00003905780613%，189个Lv1、202个Lv10、57个Lv20、60个Lv30，EvolutionCount均0。统一原生发卡初始化按候选等级计算实际经验和初始职业，不能把等级写进Exp。重复卡也发新实例，不覆盖旧卡；本地不追加未经证实的补偿，原生Type16演出明确将Rainbow置0。

CLI可通过`--ticket-rules`指定JSON，例如`{"enabled_pools":[350]}`。显式配置其他池时仍需完整概率及每个候选的初始化校验；这只定义本地券能力，不等于还原其官服保底、附赠或活动资格。空列表禁用发券。配置和目录指纹固定在一次服务构造中，更换规则需要重新构造服务。

已知但未启用或不完整池的历史邮件仍可按资源名显示，领取明确拒绝。未知池或缺少券模块时不返回无依据的邮件内容。Type22通用道具券已独立接入，默认启用44个完整池、83种券，池清单及来源见`item-summon-tickets.md`；Type20自选券当前223个配置的领取已接入，见`select-tickets.md`。特殊券附加规则仍待核实。

Type16和Type22共用`SummonGrants`验证候选池；底层`UnitGrants`与Type20共用初始字段、身份连续性、容量及完整状态检查。各类券的回执保持独立，批量初始化复用数据并在边界检查版本。启动脚本通过`-TicketRulesFile`转发原有`--ticket-rules`，`-ItemTicketRulesFile`只控制Type22的启用池。

## 原子性与验证

领券先检查兵营、技术数量、身份上限和战斗奖励预留，再抽取一次。CAS重读沿用该候选，不重抽；并发中的另一份相同请求命中已提交回执。发卡、删券与回执同一事务提交，提交前校验本次响应和完整41/42容量。

16条持久回执记录候选索引、新身份、内容版本、状态摘要和完整响应；另保留最近256个已处理请求摘要，按完整请求识别，不依赖客户端CT，旧schema1历史读取时自动升级。回执被淘汰后重发的旧包仍交给券服务，返回`summon_ticket_request_stale`并记录警告，不落入通用领取。精确重试重建相同PPU并核对原生初始字段，不再调用随机源。过时状态、变更的规则和损坏回执拒绝重放，不让旧PPU覆盖新状态。

定向验证：83项卡池检查、57项券事务检查、16项邮件/HTTP/重开/CLI检查，以及157项既有邮件、物品、恢复品和启动检查通过。报告分别为artifacts/summon-pool-verified-tests.xml、summon-ticket-verified-tests.xml、summon-ticket-mail-verified-tests.xml及summon-ticket-related-tests.xml。前三份汇总保留了来源报告，以最后实际执行结果覆盖同名用例；只重跑了修正夹具及新增影响用例，没有运行全项目测试。

artifacts/summon-ticket-doctor.json确认券服务和启动依赖就绪。原生证据及审查在aigis_research/permanent-20261101/summon-ticket-contract.md和summon-ticket-implementation-review.md。本批未启动游戏、重启服务或改动正式存档，专用召唤动画仍待实机验收。
