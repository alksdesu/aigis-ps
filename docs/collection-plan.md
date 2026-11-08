# Aigis 资源采集清单

当前已启用2026-11-06版本`86164496048d9b4329259cfb248a9d3ff283bb36`，24019项资源已独立汇入`runtime/resources/.cache`并核对完成。下文保留首次采集的范围与方法；当前升级状态见`development-plan.md`。

2026-11-01采集基线：`1bc03a049659a4543c70e324efc7847b957b0276`，来自原版 PC 客户端 AssetPath 设置函数；清单共有 24094 个独立资源，声明总量 11556588364 字节。原有缓存 783 个文件全部匹配此版本，无过期项；首次比对缺少 23311 个文件，共 10709723638 字节。

**本次全量采集已完成。** 最终逐文件复核：原版有效缓存 783 个，新增有效缓存 23311 个，缺失 0、过期 0、待下载 0。下载进程已退出。结果证据保存在 `runtime/resources/validation/collection-complete.json`。

机器清单位于 `runtime/resources/collection-plan.json`，列出资源名、MD5、大小、缓存键、分类、采集范围、状态与引用证据。每次运行 `resources-plan` 都重新校验已有文件。

| 内容 | 当前范围 | 处理方式 |
|---|---|---|
| 公共主表 | CardData 2930 行，QuestTable 3582 行 | 已采集并导入，更新时再采 |
| 版本与资源索引 | 原生 PC 六列清单，旧版24094项 | 该次版本已采；固定 CDN 地址批量下载 |
| 教程与主线基础 | startup 范围 1658 个资源 | 已齐，包含 Map9002；114 条主线和9条教程的地图成员匹配 |
| 地图包 | maps 范围 2779 个 | 已齐，全部通过大小、MD5和容器解析；任务分类2/12/13/14/15/16/17按8C5360使用`Map{MissionID:04d}_{MapNo:04d}.aar`，2026-11-03集成检查中3582关全部可构建，见`docs/battle-modes.md` |
| 剧情包与分区文本 | story 范围 1121 个资源 | 已齐，5个父包与1116个分区均已导出 |
| 角色素材、纹理、音频、其他资源 | all 范围的其余条目 | 已全量补齐本次版本清单 |
| 剧情解锁、已读、奖励、任务推进 | 服务端业务状态 | 继续静态分析并实现，资源下载不能代替状态逻辑 |

## 已导出的剧情库

`runtime/story-library/story-index.json` 已索引五个父包：HarlemText、HarlemEventText0、HarlemEventText1、EventArc、EventHomeArc。共有 6513 个文本成员、1137 个事件序列、65677 条演出指令、21200 条结构化对话，814 条关卡事件关联与127条角色篇章关联均匹配。

这些已缓存内容不需要逐段播放即可导出。导出结果约140.5 MB，原始资源未改写。尚未据此证明全历史剧情均覆盖，当前 CDN 清单以外的已移除内容另行记录缺口。

新增分区索引为 `runtime/story-library/mission-partitions-1bc03a049659a4543c70e324efc7847b957b0276/mission-story-index.json`。BattleTalkEvent、QuestNameText、MessageText各372项，合计1116项全部完成，缺失、过期、导出错误均为0。可使用 `artifacts/export_mission_stories.py` 复查或续跑。

## 采集命令

需要同时补采主表、资源版本、业务响应与 70/71 调度时，使用 [官服一键采集](capture-all.md)：

```powershell
.\setup.ps1 -IncludeCapture
.\client\capture-all.ps1 -Mode Check
.\client\capture-all.ps1 -Mode Wait -Timeout 7200
```

先等待，再从 DMM 手动启动新的原版，结束时按 Ctrl+C 并等待收尾。汇总 `run.json` 位于 `runtime/capture-runs` 下，主表与版本仍写入 `runtime/captures/content-*`。一键入口不自动下载资源或导入主表，也不能保证特殊账号、日期和各个页面的样本一次齐全；尚待实机验证。

只更新两张公共主表和资源版本时，原有统一内容入口继续可用：

```powershell
.\setup.ps1 -IncludeCapture
.\client\capture-content.ps1 -Mode Check
.\client\capture-content.ps1 -Mode Wait
```

结果保存为 `runtime/captures/content-*/bundle.json`；只有 `complete=true` 才表示三项都齐。`master_manifest` 指向既有主表导入器所需目录中的 manifest，`resource_context` 指向下面资源采集命令所需的版本文件。原来的单项采集器继续保留。

召唤、战斗、签到、礼包目录这类官服业务响应也可单独用 `client/capture-responses.ps1` 采：按注册号、预设或全部被动采集，在游戏进程内去掉凭据后写入 `runtime/response-captures`。一键入口使用其中的 `All` 范围；登录、令牌、账号联动、付费和反作弊接口一律不采，用法与过滤规则见 `response-capture.md`。

```powershell
.\capture-resource-index.ps1 -Check
.\capture-resource-index.ps1
.\collect-resources.ps1 -ContextFile '.\runtime\resource-contexts\具体文件.json' -PlanOnly
.\collect-resources.ps1 -Scope all -Workers 4
```

版本采集只读取已验证调用点的40位公共版本编号，得到后自动解除挂钩。代理消息超出本地队列容量时，版本采集以 `resource_queue_overflow` 失败退出，与主表、统一内容入口、调度采集和响应采集的 `event_queue_overflow`、`content_queue_overflow`、`schedule_queue_overflow`、`response_queue_overflow` 一致，不会丢掉消息后继续等待，重新运行即可。单项采集器的采集时限从代理加载完成起算，附加进程和加载代理的时间不计入（统一内容入口、调度采集与响应采集的总时限另含等待原版启动的时间）。一键入口 `capture-all.ps1` 则额外用总截止时间约束附加、加载与采集，`ContentTimeout` 从代理加载完成起算，也不能越过总截止时间。到期时已送达的消息仍会处理，之后到达的不再延长采集。资源采集写入 `runtime/resources/.cache`，使用资源名SHA256作文件名，同时保存原生 `.md5` 校验文件。

每个任务在 `runtime/resources/jobs` 保存状态和事件日志。已完成文件在重跑时校验后复用；单个未完成文件重新下载，不宣称支持压缩传输中的任意字节续传。临时内容只有通过HTTP解压后大小与MD5检查，才进入正式缓存。

采集和资源版本激活共用跨进程锁，运行中的采集固定使用同一份索引快照。只有一个采集任务可写入该资源库；进度文件和缓存的原子替换会对Windows短暂读取锁进行有界重试。原版已有有效资源可作为回退，不会被过期的补充缓存遮住。

原生 PC 索引使用六列：`group, content_md5, flags, size, sha256_name, name`。HTTP gzip 解码之后再 XOR 0xDA。网页五列清单仅用于格式研究，不会被激活为当前 PC 版本。
