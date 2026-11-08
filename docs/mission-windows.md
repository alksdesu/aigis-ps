# 任务窗口复刻

客户端按*MissionConfig的StartDateTime/EndDateTime判断任务是否开放（8CB420），官服复刻排期无法从客户端取得。本地复刻用一份规则文件按批次改写大討伐（分类2）、复刻（3）、神獣（10）与練習（13）四类任务的窗口，另可按MissionID整组改写成长活动免券窗口（GrowthCampaignConfig，见「成长活动免券窗口」）：规则先补丁有效资源，客户端与服务端读同一份结果。不给规则文件时不复刻，所有任务按资源原始窗口开放。未经实机验收，未启动游戏或服务。

## 规则文件

规则经start-server.ps1的`-MissionWindowsFile`或CLI的`--mission-windows`加载，客户端准备用prepare-client.ps1的`-MissionWindowsFile`（prepare_client.py的`--mission-windows`），两端必须传同一份文件。示例`config/mission-windows.example.json`的批次部分：

```json
{"enabled": true, "batches": {
  "Reproduce:300084": {"start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"},
  "Reproduce:300217": {"start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"},
  "Reproduce:320180": {"start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"}}}
```

示例另有`growth_campaigns`节，按资源原值列出当前7个成长活动（700101–700106与700201，均为2026-08-20 11:00至08-27 11:00 JST），不产生补丁。

顶层为`enabled`与`batches`两个必备键，加可选的`growth_campaigns`，文件不超过16 KiB，批次与成长活动窗口各最多256条。重复键、未知键、NaN/Infinity或溢出成无穷的数字、超过64层的嵌套都拒绝（与大討伐、塔规则共用同一严格JSON读取）：命令行加载时服务报错退出，经配置注册表加载时嵌套超深报`config_nesting_too_deep`，其余报`config_rules_invalid`；控制台暂存的文件先经严格预解析，分别报`config_duplicate_key`、`config_number_invalid`与`config_nesting_too_deep`。批次键为`<Kind>:<该批最小MissionID>`，Kind只能是Subjugation、Reproduce、Raid、Training。每批恰有`start_at`与`end_at`：两者同给或同为null；时间为带显式时区偏移的ISO 8601整秒，统一换算到JST，须晚于0001-01-01 JST，且start早于end。`enabled`为false，或`batches`与`growth_campaigns`都为空时不改资源；`batches`为空时不读取批次清单。

批次由资源推出：同一分类内Enable≠0、在MissionQuestList中有成员、不是协力大討伐（SubjugationMissionConfig的SubjugationType为1或2），且在未打补丁的基础资源里原Start/End相同的任务组成一批，同批所有成员写入同一窗口，不能单独改其中一个任务。协力大討伐不进入任何批次，补丁也不改它的窗口；与它同窗口的普通讨伐照常成批，批次键取其中最小的普通讨伐MissionID。原因是玩家选中协力大討伐、客户端载入其任务菜单时，7697A0（类型1）和74DEC0（类型2）不经确认就发出143/147，服务器没有处理器，回501后客户端会在每次确认弹窗后原样重发。以下键会被拒绝，错误信息写明原因：不是该批最小成员（并给出正确的键）、Enable为0、Enable≠0的协力大討伐（并给出该任务菜单会发送的注册号）、没有成员、不属于该Kind的任务表（并给出实际所在的表）、同一MissionID出现在多张任务表。读取批次清单时（规则含批次，或控制台列出批次），SubjugationMissionConfig须带int32的SubjugationType列，否则以「Mission window resource columns differ from the native contract: SubjugationMissionConfig.atb」拒绝。

## 窗口语义

8CB420含起不含止：End写10:59:59表示最后一个开放秒是10:59:58。Start、End都为0时常驻；End为0而Start有值时任务一开始就结束。规则中两者同为null即写0/0，任务常驻；规则不能只给一端。

补丁按资源单元类型9（datetime_raw）写StartDateTime/EndDateTime：值为自0001-01-01 JST起的毫秒数，0表示未设，按低、高32位小端写入。生成后回读核对：表结构不变，只有被配置成员行的这两格改变，且写入值等于规则窗口。通用写入器`table_cell_patch`支持int32、日序、datetime_raw与字符串单元，bool、float等其他类型仍拒绝。

## 当前批次

| 批次键 | 任务表 | 成员 | 关卡 | 原窗口（JST） |
|---|---|---|---|---|
| Subjugation:400183 | SubjugationMissionConfig | 400183、400212 | 10 | 已于2022-12-01 23:59:59结束 |
| Subjugation:400587 | SubjugationMissionConfig | 400587 | 6 | 2026-10-29 15:00至11-12 10:59:59 |
| Reproduce:300084 | ReproduceMissionConfig | 13个任务 | 93 | 2025-12-29 15:00至2026-01-08 11:00 |
| Reproduce:300217 | ReproduceMissionConfig | 12个任务 | 92 | 2026-01-08 15:00至01-15 10:59:59 |
| Reproduce:320180 | ReproduceMissionConfig | 320180、320390、320424 | 21 | 2026-10-01 15:00至10-15 10:59:59 |
| Raid:900170 | RaidMissionConfig | 11个任务 | 55 | 常驻 |
| Raid:900380 | RaidMissionConfig | 900380、900405、900429 | 15 | 2026-04-30 15:00至05-14 10:59:59 |
| Raid:900456 | RaidMissionConfig | 900456、900531、900542 | 13 | 2026-09-03 15:00至09-10 10:59:59 |
| Raid:900483 | RaidMissionConfig | 900483 | 5 | 2026-07-16 15:00至07-30 10:59:59 |
| Raid:900580 | RaidMissionConfig | 900580 | 4 | 2026-09-24 15:00至10-08 10:59:59 |
| Training:910001 | TrainingMissionConfig | 910001 | 7 | 常驻 |
| Training:910002 | TrainingMissionConfig | 910002 | 9 | 已于2026-03-01 23:59:59结束 |

复刻的9032属ヒストリー任务920014，不在复刻批次内，随该任务常驻。塔（分类12）、帝国、ヒストリー与其他分类不在白名单内，它们的窗口只能经资源覆盖同时修改客户端与服务端。

## 成长活动免券窗口

`growth_campaigns`节把GrowthCampaignConfig.atb的免券窗口按MissionID整组改写：

```json
{"enabled": true, "batches": {},
 "growth_campaigns": {"700101": {"start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"}}}
```

- 键是正的原生MissionID，窗口写进该任务在表中的全部行（当前资源7个任务每个任务各行窗口相同），不能单独改其中一行；两端同给或同为null（null即未设，恒免券）。
- 窗口判定与批次不同，走客户端的日期解析与时钟（AACF90/AAC970）：DateOpen≤时钟<DateClose，时刻落到客户端时钟网格，所以窗口必须在客户端日历内（1970-01-01至3001-01-19 JST），规则加载时就拒绝界外值。
- 补丁只改配置任务行的DateOpen/DateClose两格，客户端已按配置窗口读出的原文不改写；生成后回读核对：表结构与其他格不变，补丁后的表再经客户端日期解析读出的窗口必须等于规则窗口，不符报`growth_campaign_windows_mismatch`。表本身不可验证（GrowthCampaign.unverified）时拒绝打补丁。
- 消费方为委任的免券判定（服务端）与客户端的77A620预检，两端读同一份补丁后的资源，同源；委任目录的内容版本随之变化。启动时在装配委任之前由`verify_growth_campaign_windows`回读核对，不符时委任不装配，readiness.entrust报`growth_campaign_windows_mismatch`；readiness.mission_windows的`growth_campaigns`节给出表、窗口口径、源与生效sha256，以及各任务按客户端时钟算出的state与original_state。

## 唯一来源与启动核对

补丁是窗口的唯一来源：有效资源版本随之改变，客户端经本地CDN拿到补丁后的表，服务端的任务窗口、Tag解析、大討伐目录与委任目录读同一份资源。`QuestCatalog`的`window_overrides`参数只作测试设施，生产装配不传；同时传入会让服务端与客户端不一致。同一张表已被其他本地覆盖替换时，资源视图拒绝加载。

服务启动时`verify_mission_windows`逐个成员回读实际下发的表，与规则窗口比较。一致时readiness.mission_windows为`describe(now)`：rules、rules_hash、各表的source/effective sha256、各批次的键、成员、原窗口、新窗口、state与original_state（upcoming/open/ended），以及verification（verified、members、served_sha256、matches_build）；没有规则时为{enabled:false}。不一致时再带error`quest_windows_mismatch`，verification.mismatches逐批列出，主线、塔、大討伐与委任服务都不装配，关卡产票也不挂接；这不影响startup_ready，但state按规则窗口计算，与实际下发的表不同，应以mismatches为准。规则让资源视图本身加载失败时（例如客户端更新后批次键不再是最小成员），readiness.mission_windows显示{enabled:false}，原因写在native_cdn.error，startup_ready为false。

## 版本影响

配置注册表把任务窗口规则登记为资源语义。规则改变了某张表的窗口时，会产生新的有效资源版本：

- 旧版本下开始的战斗结算回409`quest_snapshot_changed`，需要按原快照结算或重新开战；
- 委任旧回执的精确重试回409`entrust_retry_stale`，新请求正常；
- 客户端必须重新准备资源：prepare生成新快照，旧运行目录留作备份；prepare、inspect、verify与launch都会核对请求的规则与运行目录记录是否一致，规则窗口与原窗口相同时资源快照不变，但仍要重新prepare写入记录。只配置`growth_campaigns`、没有batches的规则同样写入清单的mission_windows节并核对（成长活动表、窗口口径、源与生效sha256及各任务窗口）。

改规则的顺序是：先用同一份文件重新prepare客户端，再重启服务。也可以在控制台把规则作为`mission-windows`键校验后按content档切换（默认暂存到下次启动；运行中应用要求客户端已关闭），之后同样按切换结果的提示重新prepare并重启客户端，见`docs/admin-console.md`的「切换」。

## 控制台流程

- `mission_window_batches(read_resource)`列出全部可配置批次，含成员、成员关卡数与原窗口；`read_resource`必须读未打补丁的基础资源。
- `build_mission_window_revival(read_resource, rules)`预校验整份规则并生成补丁，校验结论与启动相同。
- `MissionWindowRevival.describe(now)`给出每批新窗口与原窗口的upcoming/open/ended。
- 客户端资源更新后，先用`mission_window_batches`核对规则中的批次键，再重新prepare与重启。

窗口开放后各模式照常检查自己的条件：大討伐仍拒绝无开始按钮的首关，复刻关与委任按任务最低等级、前置关与菜单门判定，神獣仍查Tag120等级门。重开大討伐批次不清零成员关卡记录与讨伐水位（用户决定），奖励从两者逐项的较大值继续，已达到的档不重发。`EntrustCatalog.availability`可在重启前后核对Type 8的开放关数。

## 本地规则与未完成项

| 规则 | 默认值 |
|---|---|
| 复刻窗口 | 不给规则文件时不复刻（用户决定） |
| 可改写的分类 | 大討伐、复刻、神獣、練習 |
| 批次 | 按资源原窗口自动分组，整批改写 |
| 示例 | 三个Reproduce批次于2026-11-02 15:00至11-16 10:59:59 JST开放；成长活动窗口保持资源原值，不打补丁 |

官方复刻排期与各复刻任务的开放时长只能靠实机或抓包确认；资源补丁后复刻与大討伐入口是否出现仍待实机验收。实现见`mission_window_revival.py`；定向测试为test_mission_window_revival，以及CLI与客户端准备的相关用例，使用真实公共主表与资源。
