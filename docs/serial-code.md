# 序列码（32）

`serial_code_rules.py`与`serial_code_service.py`实现32 CReactorSerialCode：默认对任何码回原生的「无效码」，另可配置本地码表，兑换结果只投进礼物箱。cli.py、config_registry.py与start-server.ps1已接入：未给`--serial-code-rules`（`-SerialCodeRulesFile`）时任何码回-10，服务不可用时32不注册、回501。

## 原生格式

入口是その他菜单的JumpSerialCode，经Func_SerialCode打开CMenuSerialCode。键盘只有数字与小写字母，最多18个字符，空输入不发包。请求在公共头后只有一个`SC`容器，里面恰好一个`CE:S`和一个`APPEND:I`：CE是输入串，每满5个字符插一个`-`（输入5个字符时末尾不带`-`）；APPEND是「第一兵营行数小于容量」的0/1。

回包登记SYSTEM、SERIALCODE、PBP、PPU、PPT五组，结果只看SERIALCODE键值表的STATUS：0无对话框；1单位入手；2、3、4同为「礼物箱已追加」；-10无效码；-11已使用的码；-12单位已满；-13每个账号限一次。文案来自OnCommandMenuSerialCode的ShowDialog，不读MessageBoxText。客户端从不重置STATUS，回包缺这一组会重放上一次的对话框；表外值在±1000内静默，更大的值可能与其他菜单命令发生crc32碰撞。HTTP非200进入「通信错误」弹窗并按原包无限重发，结果对话框不会出现。官方码表与奖励不在客户端里。

## 本地规则

- 码表默认为空：任何形状合法的码回-10，不写存档，不伪造官方码表。
- 每个200回包都带SYSTEM和恰好一行的SERIALCODE（`A1:S["STATUS"]`、`A2:I[值]`）。只发0、2、-10、-11、-13，不发1、-12和表外值。
- 本地码只进礼物箱：成功回2，并附本次新邮件的PPT增量（由PresentService.response生成）；不发PPU与PBP，不走1的直接入兵营。APPEND只校验并记入回执，不参与判定。
- 每个码按次数上限`limit`兑换（默认1），用尽回-13；标为`single_use`的码用尽回-11。单账号下两者只差文案。
- 可选窗口按JST 04:00业务日判定，含起止两天；窗口外回-10，原生没有「已过期」文案。窗口判定先于次数判定。
- 礼物箱放不下（超过500封、PresentID用尽，或加入后42的礼物箱列表超过原生接收容量）、32回包超过128 KiB回执上限或原生接收容量、兑换记录已满时，码不消耗，回0（无对话框）并记WARNING，status的kept_unused按原因计数。
- 启动时逐个码用PresentService预检奖励，并以最大PresentID模拟最宽的回包；奖励列表完全相同的码共用一次预检。发不出去的码列入status的unavailable_codes并各记一条WARNING，兑换时同样回0且不消耗；其他码照常可用。
- 非原生形状一律回400：CE分组不对、含大写或非法字符、超过18个字符、APPEND不是0或1时为serial_code_request_invalid；缺字段、多余字段、类型或值个数不对时为invalid_request_fields。原生客户端发不出这些形状。
- 其余HTTP错误只来自账号或服务端状态，原生客户端同样会按原包重发：身份不符回403；兑换记录、回执或礼物箱存档损坏，启动后奖励依赖变化，时钟无法换算时回503；连续三次写入冲突回409 profile_changed。

## 码表格式

由`--serial-code-rules`读取（启动脚本`-SerialCodeRulesFile`，配置注册表键`serial-code-rules`，时间语义为资格类）。`config/serial-code-rules.example.json`是空表，不发任何东西。示例：

```json
{
  "schema_version": 1,
  "codes": [
    {
      "code": "AIGIS-LOCAL-2026",
      "limit": 1,
      "single_use": false,
      "window": {"start_date": "2026-11-01", "end_date": "2026-11-30"},
      "rewards": [{"kind": "free_crystal", "amount": 5, "description": "シリアルコード特典"}]
    }
  ]
}
```

- `code`：ASCII文本，去掉`-`并转小写后须是1至18个数字或小写字母，规范形不得重复。键盘打不出的码拒绝加载。
- `rewards`：1至500个对象，格式与PresentService的奖励相同（unit、free_crystal、gold、item、legacy_item、summon_ticket、selection_ticket、charisma_recovery、vote_cooking），字段只能是文本或整数，文本须是合法Unicode，description必填。奖励内容与控制台寄信走同一套PresentService.preview/enqueue校验，被拒的奖励（例如ReceiveAction=3的礼包券item_smn_ticket_90001–90067）不会让码表加载失败，只会让该码列入unavailable_codes。寄信时不检查领取上限：单封金币超过presents.GOLD_LIMIT、免费水晶超过钱包上限或物品超过叠加上限时，这封邮件会一直领不出来。
- `limit`：正int32，默认1。`single_use`：布尔，默认false，为true时limit只能是1。
- `window`：可省略；给出时`start_date`与`end_date`都须是规范的YYYY-MM-DD，且起不晚于止。
- 文件上限256 KiB，最多4096个码；未知字段、重复键、NaN/Infinity或溢出成无穷的数字、嵌套超过64层，或schema_version不是1时拒绝加载。

## 存档与重放

只有成功兑换才写存档，其他结果不改修订版。新增两个命名空间，旧档没有时按空处理：

- `serial_code_redemptions`（schema_version 1）：每个码的累计次数、首次与最近一次兑换的业务日，以及最近一次发出的PresentID。码从码表删除后计数保留，重新加回时继续累计；最多65535个码。
- `serial_code_history`：RequestLedger回执，记规范码、APPEND、PresentID与回包，回执键为sha256（32、路由、ST与原始XML）。

启动时`initialize`校验这两个命名空间，损坏时按503拒绝；存档带着它们而服务不可用时，启动按503 `serial_code_service_unavailable`拒绝，不丢弃兑换记录。

同一个包在修订版未变、内容版本与提交时一致时，逐字节返回原回包，不重复发奖。礼物箱的可选依赖（道具定义、回复道具目录、召唤券、道具券、自选券、料理目录）在提交之后才接入时不算内容变化：回执摘要对应提交时的那份依赖子集，旧回执照常重放。PPT增量只取决于存档里的邮件行，所以回包与接入前逐字节相同。道具定义与道具表读自同一份ItemConfigTable，实际不会单独先缺后接，为保持一致也按同样规则处理。道具定义自身的可选输入同样按提交时核对：称号目录在提交之后才接入会改变道具定义的版本，回执摘要按道具定义的可达版本计算，旧回执照常重放。回执已陈旧（期间有其他写入，或码表变化、提交时已接入的奖励依赖已变或被移除）或已出窗只剩处理记录时，同一个包不再发奖，按当前状态回答：码已删除或在窗口外回-10，否则回该码的用尽状态（-13或-11），即使limit仍有余量；这种情况记WARNING。项目其他服务对陈旧重放回409，这里有意不同，因为409会让原生客户端无限重发。处理记录最多保留256个成功兑换，更早的旧包会被当成新请求。

## 状态

status给出ready与error、码数、unavailable_codes、各STATUS的应答计数answers、kept_unused、各项规则说明与回执上限，作为readiness的`serial_codes`输出，不计入startup_ready；服务构建失败时该项为`{"ready": false, "error": 码}`。服务在礼物箱的各券服务挂好之后才构建，因为内容版本包含它们的指纹。被拒请求记WARNING，因为原生客户端会一直重发。

## 原生一致与未验证

请求形状、STATUS取值与对话框、STATUS不重置、HTTP错误重发都来自客户端代码。码表、次数、窗口、只投礼物箱以及用0表示未能投递都是本地规则。官方2、3、4各自的情形、何时回0、-11与-13的区分口径、成功时是否附带PPT、R版官服是否仍受理32都未知；没有实机验证。
