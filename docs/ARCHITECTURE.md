# 星汐 0.5.25 typed-only 架构

星汐的目标是让可替换数字人格在群聊中自然参与，同时把“当前是谁、回复谁、根据什么、能做什么、最终发了什么”固定在模型不可篡改的 code-owned authority 中。插件启用后只有一条 typed 运行链，没有 v1、shadow、owner-only 或 AstrBot 原回复旁路。

```text
AstrBot event
  → TurnEnvelope / PrincipalContext / IngressAdmission
  → AcceptedTurn fan-out
  → Address / Reference / Media / scoped Memory
  → OpportunityAttention
  → Participation / Cadence / React or Proactive policy
  → Affect / Relationship / Persona expression
  → KnowledgeGap / CapabilityPolicy / PlannedAction
  → ReplyComposer or sealed evidence acquisition
  → SemanticGuard / OutputValidator / one-shot Repair
  → Presentation / exact segments / real SendReceipt
  → continuity, metrics and reviewed feedback
```

## 1. 准入、主体与当前轮

- AstrBot 正常 @、私聊、引用和自然称名先建立 current turn；图片／语音正文为空时只有原生 media chain 才能建立安全的仅媒体描述。
- ReNeBan、self、trusted bot、unknown automation、plugin echo 与外部 stop 在任何 Shio state commit 前分类。
- `DecisionBinding` 固定 scope、session、message、sender、content digest、revision、generation 和 trace；后续对象必须复用 exact binding。
- AcceptedTurn 给 Affect 与 OwnerAction 固定子票；未使用票必须 typed finalize，普通聊天不会填满 ledger。

## 2. 地址、参与和主动发起

- Address 区分 `DIRECT_SELF`、`ABOUT_SELF`、`OTHER_PERSON`、`OPEN_GROUP` 与不确定，不把历史称呼或引用说话人当成当前授权主体。
- Opportunity/Participation/Cadence 只允许直接回复、语义候选、仅 reaction、等待或沉默的闭集结果。`DIRECT_SELF` 直接必回，结构化 Reply／@ 其他人直接等待；`OPEN_GROUP`、`ABOUT_SELF` 与没有结构化受话对象的 `UNCERTAIN` 在确定性门通过后具有相同的语义候选机会。
- 未点名参与不再由问号、问句词、二字重合、Persona 兴趣或 Owner warmth 评分裁决。`ParticipationSemanticAuthority` 绑定 exact accepted binding、current message、同 scope 的 exact scene revision、cadence preflight 与 canonical message digest，只允许当前会话 Provider 进行一次无媒体、无工具、无 repair／fallback 的严格 JSON 判断。
- Cadence 使用两阶段合同：preflight 只读且不消费 join；语义 `REPLY` 的一次 finalize 才提交 join/continuity，`WAIT` 不提交状态，`NO_ACTION` 只提交有界 backoff。Provider 失败、取消、排队／飞行过时及重复 claim/finalize 都失败关闭。
- reaction、generation advance 与 `cancel_older()` 位于语义 `REPLY` 和 cadence commit 之后；语义模块本身没有 Persona、可见生成、气泡、Meme 或发送能力。
- P7 主动发起默认关闭；只从 canonical public scene 选题，Persona 公共兴趣只负责优先排序，没有命中时可续接最近的实质性可信公开话题，绝不复制最后说话者、个人记忆或主人私聊。
- 两套触发状态机继续各自拥有开关、白名单、频率和取消策略；一旦产出 `MAY_JOIN` 或 canonical proactive request，后续共享完整 Persona、服务器时间、密封只读检索、语义气泡、逐段回执和 Meme Manager transport，不再维护弱化生成旁路。

## 3. 上下文、记忆与媒体

- LivingMemory 只作为带来源、作用域和主体的候选资料；当前身份、目标、权限和关系永远来自 event/binding。
- 每条 accepted + verified 的人类公开群消息进入 bounded typed ledger；其最近尾部写入 `public_group_ledger.json`，使重启后的显式群聊回顾继续可用。该恢复面不接受助手、工具、引用或 legacy history，也不跨群。
- ReplyComposer 将筛选后的历史按真实顺序投影为 provider `user/assistant` 消息；当前消息只在当前 user prompt 中出现一次。自然参与与冷场主动续题共用 `model_input_contract.py` 的唯一 scene→`CanonicalModelMessage` 投影；Provider `content` 保持纯正文，真实显示名、Reply 和多个 @ 只进入 code-owned、不可信的 system metadata，内部 sender/scope/bot/account 字面不得进入 prompt 或日志。
- 个人／owner-private 事实必须属于当前 exact sender 与允许会话；其他主体和无来源资料 fail closed。
- 引用内容独立于当前发送者；direct/quoted media 保存 source message/sender 与 availability，原始 URL/base64 只留在 opaque Provider transport。
- `X-01` 表示 LivingMemory 被动捕获可能早于 Shio/ReNeBan 准入；架构只承诺 Shio 零消费／零提交，不宣称外部零存储。O1 尚未执行。

## 4. 能力与主人动作

- 群友只能获得管理员精确允许、能力分类为只读且本轮确有 KnowledgeGap 的检索工具。
- 最终渲染器只读取代码生成的 capability snapshot；它区分“配置过”与“真正进入当前请求”，没有出现在 effective tool inventory 的知识库、联网或外部 Agent 不能被人格文本宣称为可用。
- `group_join` 与 proactive group initiation 最多读取一次：稳定知识／黑话只选 AstrBot KB，明确实时事实只选公网检索；最终 Persona renderer 仍传入空工具集。React、写入、执行、设备与个人记忆能力始终关闭。
- 工具候选必须同时满足 exact runtime identity、schema/source conformance、当前 scope/sender/target/generation 与 one-shot permit。
- 主人身份只提供 proposal 资格；当前正文、私聊、operation、参数、二轮确认、durable lifecycle、executor completion、ActionOutcome、Presentation 和真实 delivery 必须逐层一致。
- `owner_action_enabled` 和四个适配器默认关闭；production live allowlist 为空；Shell 永久代码级硬关闭。

## 5. Persona、Affect、关系和学习

- PersonaPackage 负责“是谁、如何感受、如何表达”，不负责可信身份、权限或执行结果。
- Affect 与 Relationship 都由 accepted human/outbound receipt 产生 continuous state；只作为表达投影，不能改写事实和 action。
- 表达候选本地检索，不调用旧 Embedding/Reranker/StyleRetriever。
- 学习从 exact send 后反馈产生高置信、多 reviewer、无敏感内容的聚合 candidate；review、shadow、有限 activation 和 revoke 都是独立 authority，默认不改变核心人格。

## 6. 生成、修复和呈现

- ReplyComposer request 由 module-owned vault 自行签发，绑定 current plan、ContentIntent、Persona、Affect、Relationship、Media 与能力策略。
- 主生成最多一次；Knowledge acquisition 只通过 sealed broker 执行，最终 Persona renderer 零工具。冷场主动续题如果需要证据而 sealed read 失败，直接进入沉默终态，不以无依据内容降级。
- SemanticGuard 与 OutputValidator 检查当前语义、关系、事实、媒体、语言、协议、ActionOutcome 和 exact segments。
- Repair 只有 canonical validation permit 才能生成一次，复用同一 request/outcome/media/persona，不能再调用工具。
- Presentation handoff 自 mint exact segments；只有实际 segment send terminal evidence 才能写入 SentReply／反馈／动作 delivery ack。
- 普通／自然回复允许 1-3 个语义完整 segments。主动／冷场使用 `proactive_min_bubbles..chat_max_bubbles`，少于最少值的首稿只能在同一请求合同上 repair 一次。Shio 的响应校验优先级高于官方 Meme Manager 4.15.1 的响应钩子，确保其逐图语义检索读取修复后的最终可见文本；Manager 随后装饰结果，Shio 气泡分发保留非文本图片组件。
- 主动／冷场由独立调度器发送，不伪造成普通聊天事件。生成模型必须依据同一公开上下文在末尾提供且只提供一个官方 22 类隐藏标记；validator 校验类别并从可见正文剥离。文字全部成功且硬安全允许后，Shio 调用 Manager 现有的公开 category compatibility transport。概率、资源包、图片构建和实际发送仍归 Manager；Shio 无第二套概率／开关，也不把类别路径冒充逐图向量语义检索。

## 7. 并发、重启和失败策略

- 全局 inference budget 限制 active、waiter、queue timeout 与 active timeout；direct/action 优先于 participation/proactive。每个 canonical participation request 只有一次 `PARTICIPATION` permit；排队期间过时会在 Provider 工作创建前淘汰，调用结束后始终释放容量。
- 每个 scope 有 generation coordinator；新消息取消旧 provider/tool/meme/send，迟到结果丢弃。
- revision 与 participation cadence 使用 privacy-minimal HMAC continuity；公开群聊回顾只持久化 bounded verified inbound tail；主动配额、冷却以及每群近期 topic/final digest 以认证 schema 3 事务状态持久化，热重载旧实例写入前必须重新读取磁盘，不能覆盖新终态；owner lifecycle 分开持久化。
- 主动发送前校验 scene 当前性；真实发送后只能复核请求完整性与同一 scope，不能把机器人自己的 scene revision 前进误判成 stale。调度器只有在外层返回 `SENT` 后才提交配额、冷却和防重复状态。
- terminate 会取消 scheduler 和 owned tasks、等待活动调用有界结束并关闭持久 store。
- 任何 identity、target、authority、runtime、Guard 或 send 失败都停止当前轮；不回退旧代码，不持久化失败草稿，不稍后补发。

## 8. 配置与验证

公开 schema 有 57 个字段；两项主动场景完整规则和主动最少气泡数明确可见，Meme 配置只存在于 Meme Manager，高风险 owner/proactive 默认全部关闭。当前自动化超过 1,200+ 项，固定 capability/outcome/plugin/persona/media/concurrency/restart 矩阵还必须经过隔离 container 与 FNOS 发布门。
