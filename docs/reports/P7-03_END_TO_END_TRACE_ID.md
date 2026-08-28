# P7-03 端到端脱敏 Trace ID

## 结论

星汐现在为每个入站 event 创建独立、随机的 128-bit 内部 trace ID，并用强类型 `TraceContext` 记录流水线阶段。目标选择、上下文、计划、工具结果、原始回复、最终回复、气泡规划、发送交接/结果、已发送记录和反馈证据可以沿同一 trace 关联。

trace 用于内部诊断，不复用 AstrBot session、用户 ID、群 ID或平台 message ID，也不把这些真实标识作为 trace ID。

## 实现

- `TraceContext` 持有 `trace_id`、单调计时起点和强类型 `TraceStage` 列表。
- 每次 `start_pipeline_trace()` 都通过 `secrets.token_hex(16)` 创建新的 32 位十六进制 ID；不同 event 不共享上下文。
- 保留旧 `_shio_pipeline_trace` 字典快照，避免破坏已有本地诊断与测试；它由强类型上下文单向生成。
- 新增独立 `target` 阶段，并把气泡规划、手动发送尝试/失败/成功、自动发送交接/成功写入流水线阶段。
- `InternalReplySend`、`SentReplyRecord` 和 `ReplyObservationTracker` 保存原始回复 trace ID。
- `FeedbackEvidence.reply_trace_id` 指回被评价的那次回复，而不是把后来反馈消息的事件身份误当成原回复身份。

## 隐私边界

输入阶段只保存：

- 消息、发送者、群的不可逆短摘要；
- 字符数、聊天类型、owner/ambient 布尔值；
- 由 typed envelope 提供的无正文结构化 metadata。

回复阶段只保存摘要、长度、气泡数量、调用/守卫状态等结构信息。测试确认序列化 trace 中没有原始聊天正文、真实 sender ID 或真实 group ID。

原文仍只存在于必要的当前生成/发送对象中，不会因为可观测性额外进入 trace。

## 发送与反馈关联

- 每个气泡先产生 `bubble_planned`，随后记录手动 `send_attempt` 或自动 `send_handoff`。
- 只有成功回调写入 `send_success` 并形成实际发送观察。
- `SentReplyRecord.trace_id` 与 event 的 `TraceContext.trace_id` 一致。
- 后续高/低置信反馈均保留 `reply_trace_id`，因此可以定位它评价的是哪次真实发送，而不依赖相邻消息猜测。

## 验证

新增或扩展回归覆盖：

- 两个 event 得到不同且格式正确的随机 trace ID；
- typed context 与兼容字典快照同步；
- trace 序列化结果不包含聊天正文、真实用户/群 ID；
- target 阶段先于 context/plan；
- 三气泡的 planned、attempt/handoff、success 阶段计数准确；
- `SentReplyRecord`、运行时发送观察、反馈证据保持同一回复 trace ID。

完整测试：468 项执行，465 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

- 本层不记录完整模型请求体、Token、Cookie、API Key、base64、真实用户/群/message ID 或聊天正文。
- AstrBot 当前通用发送接口没有可移植的平台 message ID 回执；trace ID 是星汐内部关联键，不冒充平台回执。
- 本层没有部署，没有修改 AstrBot、LivingMemory、Meme Manager 或 Provider，没有 Git 写操作。

## 下一层

P7-04 将统一 trace metadata 的允许字段和摘要规则，并让结构化日志只输出 trace ID、source kind、摘要、调用次数、阶段耗时和守卫命中，禁止敏感键和值进入日志。
