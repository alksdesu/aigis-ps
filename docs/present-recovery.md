# 恢复药与料理邮件

已接通常规Type8恢复药和当前172种EffectType11料理。料理包括114种无剧情项、58种带剧情项，后者引用54个实际事件包。后台单领、容量、回滚和重开已定向验证；原生播放及页面操作仍待实机验收。

## 恢复量与领取流程

每封邮件恢复一份当前最大魅力，即RankUp.CharismaMax加ExtendedMaxCharisma。先结算正常经过时间的自然恢复，再增加这份完整数量。邮件A2不作为倍率；Type8的A2用于装饰显示，当前仅发放并接受已核实的普通值0，带角色装饰值仍单列未支持。

料理说明经cfgImplantText映射到SystemText明确描述“恢复最大值分”，客户端预检也检查当前值加最大值不超过2000。`RecoveryService.restore_state`是邮件与85结晶恢复共用的纯候选入口；邮件不会调用扣晶交易。默认上限2000与现有RecoveryRules共用，可以配置；修改后台上限不会改写客户端内置的2000预检。

根据用户确认，CanAllGet=0的恢复品仅允许逐封领取。1/2请求必须仅含这封邮件，混合多封请求明确拒绝，并保留所有邮件和资产；117批领跳过恢复品并保留邮件，其余邮件照常领取，只选了恢复品时回空成功、不写回执。普通单位、金币、结晶和被动物品原有批领方式保持不变。这里的单领政策依据资源标记与用户选择，不声称已观察到官服批领实现。

无剧情料理直接领取。54个事件包已逐项验证：支持的演出命令后仅执行一次无参数DoPeruseSavePresent，原生随后请求1和42，没有75。客户端负责播放，服务端在1号交易原子恢复并移除邮件；不虚构额外PEV、剧情收藏或首读奖励。

## 共享接口与回执

服务器内部可用以下奖励输入：

```python
{"kind": "charisma_recovery", "description": "recovery reward"}
{"kind": "charisma_recovery", "item_id": "item_cook_logbo_00001", "description": "cooking reward"}
```

第一种生成Type8/Content0；第二种生成Type22/Content1并保存ItemID。接口不接受amount，避免误把一封恢复邮件解释成多份库存。目录读取ItemConfig、两张文本表和事件包，冻结对应内容指纹；未知恢复效果、资源变化或未支持装饰不会自动降级成普通物品入账。

领取时只捕获一次当前时间。恢复、删除邮件和持久回执同一次CAS；上限不足、时钟回拨、编码超限或并发状态变化均重新校验或回滚。成功返回完整PGP和空的新单位PPU，不发送POSSESSION、PPC/PFC、PEV或PPT；42负责读取删除后的完整邮箱。

沿用旧邮件结构及schema1回执。缓存PGP必须与当前存档的规范金币或恢复回包一致，结晶缓存也须匹配当前分类余额或旧单日桶格式。85本身的缓存同样与当前完整响应核对，防止合法XML携带错误体力/结晶值。旧金币和结晶回执格式仍被保留。

## 验证与边界

恢复目录97项、真实资源HTTP27项、原有恢复和邮件关联196项分批通过。缓存错误数值、缺列、重复容器另新增12项并复测1项正常重放；85自身的回执校验新增7项通过。配置和172项覆盖见`artifacts/present-recovery-doctor.json`，关联结果见`artifacts/present-recovery-related-tests.xml`与`artifacts/recovery-receipt-state-tests.xml`。

尚未接入旧Stamina相关的Type9/15，以及Type8带角色装饰的特殊显示内容；Type21已作为投票料理回赠的载体接入（Content为纯CardID、逐封领取，见`vote-events.md`「发件采用Type21」与原生领取一节），签到料理的`cooking_sender`为affection时也用它发件（见`login-cooking.md`）；86在当前资源下原生不可达，按原生一致保持501，见`recovery-flow.md`。当前新增功能不增加初始福利，不操作原版账号，也不自动启动游戏或服务。

原生证据见`aigis_research/permanent-20261101/present-recovery-contract.md`。
