# P2-04 Address Resolver 与生产接线报告

## 结论

P2-04 群聊与 P2-04B 私聊结构化 Address 的纯模块及本地生产接线均已完成。`core/address_resolver.py` 只把已准入的当前人类 `ConversationEvent` 解析为 P1 `AddressDecision`，不决定 Attention、Participation、Action 或是否发送。

群聊准入成功后，pipeline 会在当前 AstrBot event 上写入同 binding、event-local 的 `SHIO_ADDRESS_DECISION`。私聊由 `resolve_private_address(event, ingress)` 依据可信 private channel 直接返回 `DIRECT_SELF / PRIVATE_CHANNEL`，同样写入当前 event；它不创建或伪造 GroupScene。接线没有恢复旧 Planner、shadow、回退链或生成器，也没有改变既有 name-wake 断言。

## 先红后绿

- 先新增 `tests/test_address_resolver.py`；
- 第一次定向运行稳定失败：`ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.address_resolver'`；
- 实现后发现并修正两个合同级红灯：结构化 other mention 不能在缺少 reference message 时填写 `referenced_sender_key`；“大家继续吧”必须命中显式开放群体标记；
- 纯模块定向测试最终 `11/11` 通过；
- 生产接线先新增 `tests/test_address_pipeline.py`，首次运行 6 个测试方法产生 8 个稳定失败，正向场景全部失败于准入后 `SHIO_ADDRESS_DECISION is None`；同时 self、known-bot、ReNeBan degraded 和 private 零 Address 边界已经通过；
- 群聊接线完成后，同一生产测试由红灯转为 `6/6` 通过，未以放宽测试规避失败；
- P2-04B 先新增私聊合同测试，稳定红灯分别为缺少 `resolve_private_address` 的 `ImportError` 和缺少 `AddressEvidence.PRIVATE_CHANNEL` 的 `AttributeError`；
- 新增闭集 evidence、私聊 resolver 及 repr 脱敏后，Address resolver + behavior contracts 定向测试 `32/32` 通过。

## 生产接线

1. 只从 AstrBot 结构化消息组件提取地址证据：`At.qq` 生成 `StructuredMentionEvidence`，`Reply.id + Reply.sender_id` 与 `TurnEnvelope.reply_to_*` 共同生成 `StructuredReplyEvidence`；昵称、正文自称和引用正文都不能制造 sender identity；
2. 仅当 P2-02 准入得到 `ACCEPT_HUMAN` 且 P2-03 已写入同 scope、同 conversation revision 的成功 `GroupSceneSnapshot` 时调用 resolver；
3. 成功结果仅写到当前 event 的 `SHIO_ADDRESS_DECISION`，不会写入跨事件共享缓存；
4. private 轮次只调用两参数 `resolve_private_address(ConversationEvent, IngressDecision)`，以可信通道产生 `DIRECT_SELF / PRIVATE_CHANNEL`；不调用 group-only resolver，也不创建 GroupScene；
5. self、known-bot、ReNeBan gate degraded 等未获准事件不会得到 `ConversationEvent` 或 Address，保持零 Address 写入；
6. 构建阶段对群聊和私聊都再次要求 Address 是 `AddressDecision` 且 `address.binding == admitted_event.binding`，否则以 `address_unbound` 阻断 typed turn；
7. 构建阶段只记录脱敏的 `address_decision.trace_metadata()`，不把正文、别名、`At.qq`、Reply ID、sender ID 或引用正文写入 address trace。

## 输入硬门

resolver 只接受：

1. frozen `ConversationEvent`；
2. 与 event 完全相同 binding 的 `IngressDecision`；
3. `ACCEPT_HUMAN + HUMAN + PluginSource.NONE`；
4. SHA-256 与 event content digest 一致的当前可见正文；
5. group conversation；
6. 可选、但必须绑定当前 message 的结构化 mention/reply；
7. 可选 `IngressWakeCandidate`；它只能佐证当前正文中已存在的自然呼语，不能凭候选自身制造别名；
8. 可选 `GroupSceneSnapshot`；提供时必须与当前 scope/revision 一致，并包含当前 sender participant。

任一准入、binding、正文、结构化 evidence 或 Scene 错配都固定失败关闭，不产生降级 Address 结果。

私聊 `resolve_private_address(event, ingress)` 的硬门独立于群聊文本 resolver：

1. 只接受 frozen `ConversationEvent` 与同 binding `IngressDecision`；
2. 必须是 `ACCEPT_HUMAN + HUMAN + PluginSource.NONE`；
3. `TurnEnvelope.chat_type` 必须严格为 `private`；group 误调用、drop、binding 错配、unknown sender 或外部 plugin source 全部失败关闭；
4. 函数签名只有 `event, ingress`，没有正文、昵称、自称、历史或 Persona 参数；
5. 通过硬门后由代码权威返回 `DIRECT_SELF / PRIVATE_CHANNEL`，不进行文本分类。

## 决策优先级

解析顺序固定为：

1. private channel 通过独立硬门 → `DIRECT_SELF / PRIVATE_CHANNEL`；
2. 群聊结构化 @ 当前 bot → `DIRECT_SELF / STRUCTURED_MENTION`；
3. 群聊结构化 reply 当前 bot → `DIRECT_SELF / REPLY_TO_SELF`；
4. 群聊当前可见正文中的可信呼语 → `DIRECT_SELF / VOCATIVE_ALIAS`；
5. 群聊结构化 @ 或 reply 其他人 → `OTHER_PERSON / OTHER_PERSON_TARGET`；
6. 群聊当前正文只是谈到角色 → `ABOUT_SELF / THIRD_PERSON_REFERENCE`；
7. “大家/各位/你们/有没有人/谁知道”等群体标记或无目标开放问题 → `OPEN_GROUP`；
8. 证据不足 → `UNCERTAIN / NONE`。

结构化 self 证据优先于文本；文本别名不能覆盖结构化 other target，除非当前正文确实以呼语重新直接叫角色。

## 关键语义

| 当前消息 | AddressDecision | 说明 |
|---|---|---|
| `笨蛋萝卜子，你这次是大修` | `DIRECT_SELF` | 情绪前缀 + 当前正文呼语 |
| `我觉得笨蛋萝卜子这个称呼很有趣` | `ABOUT_SELF` | 在谈论角色/称呼，不是直接呼叫 |

`ABOUT_SELF` 不等于“不抢话”或“永远沉默”。本层只判断对象；P5 Attention/Participation 可以继续把该消息判为 `MAY_JOIN`、`REACT_ONLY` 或 `NO_ACTION`。

## 文本与引用边界

- 句首、句尾、中间带停顿的称名，以及“笨蛋/喂/哼”等句首情绪呼语均有直接地址规则；
- “角色设定、称呼、名字、原作”等第三人称/元讨论保留为 `ABOUT_SELF`；
- URL、行内代码和 fenced code 在别名匹配前等长遮罩；
- `《ATRI》` 等书名号和明确作品购买/游玩语境不会被当作角色地址；
- quoted content 从不参与当前正文别名匹配；引用中只有角色名、当前正文没有时，不会误判 `DIRECT_SELF`；
- 结构化 reply 的 message/sender 必须与 `TurnEnvelope` 一致，引用其他人时得到 `OTHER_PERSON`。

## GroupScene 边界

GroupScene 只提供同 scope/revision 的公共场景一致性证明。生产接线把刚成功记录的 snapshot 直接交给 resolver；构建阶段也再次检查 Scene 与 admitted event 的 scope/revision。Address Resolver 不读取 `ParticipantState.personal_facts`，也不根据其他 participant 的个人信息、关系或记忆改变当前地址结果。个人事实继续留在 P2-05 MemoryDecision 边界。

## 隐私与可观测

- mention/reply evidence 为 frozen/slots，原始 message/sender/quoted content 不进入 repr；
- 输出复用 P1 `AddressDecision` 和当前 `DecisionBinding`；`AddressDecision` 的 binding 与 reference ID 字段不进入 repr；
- `trace_metadata()` 只包含 Address kind、confidence、evidence 数量、meta/reference 布尔，不含正文、别名、scope/message/sender ID 或 quoted content；
- resolver 不保存 GroupScene、候选正文、引用正文或模型输出；
- private resolver 根本不接收正文、昵称、自称、历史或 Persona，trace/repr 均不回显 session/message/sender/scope ID；
- pipeline 的 address stage 只展开上述脱敏 metadata；解析异常只记录异常类型，不回显原始结构化参数。

## 验证

- P2-04 定向测试：`11/11` 通过；
- P2-04 生产接线测试：`6/6` 通过；纯模块与接线合计 `17/17` 通过；
- P2-04B 私聊纯模块新增 `2/2` 测试；Address resolver + behavior contracts 合计 `32/32` 通过；
- 私聊生产接线把原“private 零 Address”红测改为可信 `DIRECT_SELF / PRIVATE_CHANNEL` 验收；Address resolver + Address pipeline + 完整 pipeline `83/83` 通过；
- P2-04B 相关回归（Address、behavior、pipeline、ConversationEvent、IngressAdmission、GroupScene、P1 fixture、name-wake）：`76/76` 通过；
- 接线完成时 Address + pipeline 相关回归：`76/76` 通过；
- P2-03～P2-06 并行接线稳定后的完整本地回归：`526/526` 通过；
- 覆盖结构化 self/other @、self/other reply、quoted-only name、句首/句尾/句中呼语、情绪呼语、关键 ABOUT_SELF、第三人称、open group、uncertain、URL/code/title、name-wake 佐证、Scene binding 与 trace 隐私；
- resolver 与两组 Address 测试通过 `compileall`，统一 `git diff --check` 通过；
- 接线只修改星汐 `main.py` 与对应回归测试，没有修改配置、FNOS、AstrBot 核心或第三方插件；
- 当前成果仍是本地代码与测试结果，尚未部署到 FNOS；未执行 Git/GitHub 写操作。

## 后续边界

- 当前生产接线必须继续使用 P2-02 的真实 admission result、AstrBot adapter 的结构化 mention/reply 和同 revision P2-03 Scene；禁止从扁平正文重建 @/reply；
- 私聊生产链已固定只使用准入后的 `ConversationEvent + IngressDecision`；后续阶段不得把正文、昵称、自称、历史或 Persona 重新加入私聊地址证据；
- P2-05 记忆不能覆盖 Address；P2-06 media sender 也不能改变当前 human principal；
- P5 才能根据 `ABOUT_SELF`、`OPEN_GROUP`、打断成本与群聊节奏决定是否自然参与；本模块不得提前等价为“必须回复”或“必须沉默”。
