# 自动选材偏好

27保存F6与FB，分别对应已有Config中的AutoSelectConfig和IntegrationAutoSelectConfig。原生总是同时发送两个有符号32位整数，服务按完整值替换，保留符号位及尚未解释的其他位。保存偏好不改变角色、仓库、材料或合成结果。

响应为SYSTEM与PGC，PGC的A1:S携带F6/FB，A2:S携带十进制值；36完整配置使用原生整数列回读。提交前同时预检两种响应，CAS冲突后重读当前存档重试，保留并发修改的其他字段。相同值不产生新的存档修订版。现有存储字段和schema不变。

报文严格校验公共头、两个必需业务字段、类型、重复列和整数范围。已损坏的配置拒绝覆盖，不使用默认值掩盖存档问题。旧版只含部分可选配置的其他更新路径保持兼容。

控制台`config.auto_select.set`复用27的`UnitActionService.set_auto_select`，F6和FB必须同时给出完整int32，未解释的位原样保留，回读36。

证据为`aigis_research/permanent-20261101/auto-select-settings-contract.md/json`。`artifacts/auto-select-tests.xml`包含35项专项、42项既有角色操作及1项CLI检查；`artifacts/auto-select-config-regression-tests.xml`包含55项关联配置检查，全部通过。HTTP测试使用内存ASGI客户端，覆盖原生编码、重复提交、重开存档和36回读。未启动游戏或监听服务，原生菜单仍待验收。
