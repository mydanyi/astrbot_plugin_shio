# P2-03 GroupScene / ParticipantState 生产接线报告

## 结论

P2-03 已实现并接入生产热路径。新增 `core/group_scene.py`，以 `ConversationEvent.binding.conversation_revision` 为每群顺序权威，建立有界 `GroupSceneSnapshot` 和按可信 `sender_key` 隔离的 `ParticipantState`；`main.py` 只在 P2-02 准入成功后记录人类消息，只在最后一个自动发送节点取得 terminal 成功回执后记录星汐回复。

本层不读取 AstrBot raw history，也不替代 P2-05 LivingMemory 策略。P2-08 已验证历史归一化结果不能旁路写入 Scene；自身消息、可信机器人、plugin echo 与 ReNeBan 降级均新增生产级 Scene 零写入断言。

## 先红后绿

- 先新增 `tests/test_group_scene.py`；
- 第一次定向运行稳定失败：`ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.group_scene'`；
- 实现纯模块后，同一定向测试转为 `9/9` 通过。
- 生产接线前新增端到端用例，首次稳定失败于 `main.SHIO_GROUP_SCENE_SNAPSHOT` 不存在；接线后同一用例验证 admitted human 与 terminal Shio receipt 依次进入 Scene。
- 接线时发现 `ConversationEvent` 与 pipeline 各自生成 trace，导致真实 `SentReplyRecord.trace_id` 永远不能匹配 accepted target；`start_pipeline_trace()` 现接收并严格校验 admission binding 的 32 位十六进制 trace，同一轮只使用一个 trace。

## 准入合同

### 人类 inbound

`GroupSceneBook.record_human()` 只有在下列条件全部成立时才更新：

1. 输入是已提交的 frozen `ConversationEvent`；
2. 同 binding 的 `IngressDecision.disposition == ACCEPT_HUMAN`，且 `allows_state_mutation` 为真；
3. event/decision 的 `SenderKind` 都是 `HUMAN`；
4. `PluginSource == NONE`，chat type 为 group；
5. 调用者提供的 public content SHA-256 与 `ConversationEvent.content_digest` 完全一致；
6. conversation revision 恰好等于该 scope 当前 Scene revision + 1；
7. 个人事实候选的 subject 必须精确等于当前 event sender key。

因此 banned、self、known bot、Parser、Meme Manager、其他 plugin echo、binding 错配、重复 revision、跳号 revision、正文错绑均只返回闭集拒绝原因，不创建 scope，也不改变已有 snapshot。

### 星汐 outbound

机器人内容只有同时满足以下证据才进入公共 Scene：

- 调用方明确声明闭集 `SceneEntrySource.SHIO_OUTBOUND`；
- 输入为 `SentReplyRecord`，receipt 已 terminal，至少一个 segment 真实 `SUCCEEDED`，成功时间与 segment 内容 SHA-256 有效；
- reply ID 是内部 `shio-*` ID，`target_source_kind == current_inbound`；
- receipt 的 scope、session、target message、target sender、target content digest、reference、trace ID 全部与此前已接受的人类 target 一致；
- receipt 未重复消费。

Scene 只保存真实成功 segment 的可见文本；失败 segment、Parser/Meme/外部插件/known bot/banned source/unknown assistant、legacy source 和错 target receipt 全部拒绝。Shio outbound 不伪造新 conversation revision，而是绑定其 target 的 revision，同时只增加目标 participant 的成功回复计数。

## 公共话题与个人事实

- `PublicTopic` 保存群内公开可见的人类消息或已成功发送的星汐回复；
- `PersonalFact` 只能存在于同一 sender key 的 `ParticipantState.personal_facts`；
- GroupScene 不从公开正文自行抽取个人事实，也不允许候选把 subject 改绑到另一名群友；
- A→B→A 得到 revision `1/2/3`，A 的 turn/fact 只累积到 A，B 只累积到 B；
- 相同平台用户在不同群使用不同 scope/sender key，两群 snapshot 都独立从 revision 1 开始。

这只是状态隔离合同。个人事实是否可由 LivingMemory 召回、是否相关和能否进入当前回复，仍属于 P2-05 `MemoryDecision`，不能由本模块提前决定。

## 有界状态与原子性

`GroupSceneBook` 分别限制：

- public topics；
- participants；
- 每名 participant 的 personal facts；
- 可用于 outbound 精确绑定的 accepted targets；
- 已消费 outbound receipt IDs。

所有对外 snapshot、topic、participant、fact 和 mutation result 都是 frozen/slots。方法先完成来源、binding、内容、revision 和 fact subject 校验，再一次性替换 scope state；snapshot/查询缺失 scope 不创建状态，所有 reject 均为零写。

生产端在 `_admission_lock` 内按 revision 顺序执行 `record_human()`，避免并发的 revision 2 抢先写入 Scene；结构性拒绝不会继续写旧 `ConversationRuntime`、不会提升自然称名唤醒，后续生成也因 `group_scene_unbound` 失败关闭。私聊不创建 GroupScene。

发送端只在 `after_message_sent` 已确认最后自动 segment 成功后调用 `record_outbound()`。多气泡中间发送不会提前把尚未完成的回复标成公共场景；Scene 只消费唯一 terminal receipt，重复 hook 不会重复记账。

## 隐私

- scope/sender/message/target/trace、正文、个人事实和内容 digest 均不进入对象 repr；
- trace 只包含闭集 source/status/reason、revision、计数和布尔结果；
- trace/repr 不输出原始 ID、群聊正文、个人事实、URL、工具参数或插件 payload；
- rejected result 不持有原始 event、decision、receipt 或候选正文。

## 验证

- P2-03 纯模块定向测试：`9/9` 通过；
- 生产 Scene、pipeline trace、self/known-bot/ReNeBan 零写组合回归：`15/15` 通过；
- 覆盖 ACCEPT_HUMAN、banned/bot/plugin 拒绝、Parser/Meme/unknown assistant 拒绝、成功/失败/错源/错 target/重复 receipt、A→B→A、跨群、容量边界、revision stale/gap、个人事实改绑和 repr/trace 脱敏；
- P2-03～P2-06 并行接线稳定后的完整本地回归：`526/526` 通过；`compileall` 与 `git diff --check` 通过；
- 修改仅限本地星汐代码、测试、报告和计划；未修改生产配置、FNOS、AstrBot 核心或第三方插件，未执行 Git/GitHub 写操作。

## 后续消费边界

- 后续 Address/Participation 只能消费本层 event-local snapshot，禁止从 raw assistant history、相邻位置、日志文本或模型自称重建来源；
- `SceneEntrySource.SHIO_OUTBOUND` 必须由可信星汐发送适配层填写；外部插件不得把自己的输出包装成该来源；
- P2-08 normalizer 的 drop 结果不得旁路进入 Scene；
- P2-04 Address 只能读取本层公开 Scene 和当前 sender 的 participant 视图，不能把其他 participant 的个人事实并入当前主体。
