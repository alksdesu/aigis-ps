# 秘书选择

注册121接收根级`A7:I`，保存普通持有单位的GetIndex；0表示取消。资格为原CardID的BonusType非0且当前Love至少100。锁定、编队、显示类型、性别与兵营位置不额外改变这项资格，聚合仓库数量不构成可选实例。

`GuideService`冻结CardData和两张命令表所需的数据。资格表达式变更会使该服务明确不可用；单卡出现尚未支持的评级命令时只拒绝该卡，避免任意执行资源脚本。未来资源更新后应先核对变化再扩展受支持表达式。

只修改现有Config._GuideUnitIndex，响应SYSTEM和PGC.C1，36完整配置恢复该值。相同值不增加修订版，CAS冲突重新核对当前持有、好感和身份；121和36均在保存前完成编码预检。21/22仍只接受原来的教程或声音配置，不能借新增C1映射绕过秘书资格。

所选实例消失后保留旧C1。已核对原生A35450在查不到GetIndex时恢复默认显示，没有写回C1；不替官服补造删除策略。现有GetIndex/UniqueID高水位继续保留，后续发卡不会误用原秘书身份。新号默认取消，不自动将王子设为秘书。

控制台`guide.set`（设置域，写入归属Config._GuideUnitIndex，预检回读36）调用`GuideService.set_selection`，与121使用同一组校验。`get_index`取0–2147483647，0表示取消，不代表GetIndex 0的王子；非0时须为普通持有实例，原CardID的BonusType非0且Love至少100，拒绝码与121相同：guide_not_owned 409、guide_ineligible 409、guide_card_unavailable 503、guide_rating_unsupported 503。控制台不写游戏回执；121本身没有回执账本，控制台写入前已发出的121晚到时照常生效，以最后写入为准。旧C1指向已不存在的实例时，控制台显示该GetIndex而单位为空，可以直接取消；重复选择当前秘书不改变修订版，本来没有秘书时再取消只给警告。

50项模块定向测试通过；`tests/test_permanent_home_integration.py`另验证实际送礼0→100、设为秘书、重复保存、36重开回读、取消，以及出售后保留C1和新单位不复用身份。测试通过ASGI执行，未启动游戏或监听服务。原生界面尚未验收。

静态证据：`aigis_research/permanent-20261101/guide-selection-contract.md`及同名JSON。
