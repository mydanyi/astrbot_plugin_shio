# P6-02 发送成功后再记录回复观察

## 结论

普通群聊回复不再在 LLM 输出守卫阶段调用 `record_bot_reply()`。这个阶段只说明文本已经生成，不能证明任何气泡真正发出。

现在三条发送路径统一满足“先得到明确发送成功，再写成功观察”：

- 普通单条/长回复：在 AstrBot `after_message_sent` 事件触发后确认。
- 普通多气泡：每次 `await event.send(...)` 正常返回后确认该气泡；交给 AstrBot 自动发送的最后一条在 `after_message_sent` 后确认。
- 恢复补答和主动话题：原代码已经检查 `Context.send_message()` 的布尔结果，本层保留其成功后记录时序。

## 根因

旧路径在 `on_llm_response` 中完成清洗后立即调用 `record_bot_reply()`。此时仍可能发生：

- 最终结果被发送前守卫替换；
- 气泡拆分改变群内可见分段；
- 前置气泡发送失败；
- AstrBot 最终自动发送失败；
- 实际发送文本与记录文本不同。

因此旧观察可能把“准备发送”误当成“发送成功”，并污染后续反馈、冷却和表达权重。

## 实现

### 1. 删除提前记录

`guard_persona_reply()` 只保留最终文本与 trace，不再写入运行时回复观察。

### 2. 分段发送状态

`dispatch_chat_bubbles()` 使用 P6-01 的内部发送账本：

- 每个真实发送尝试先进入 `attempted`；
- `event.send()` 抛异常时只写 `failed`，不写成功观察；
- 手动气泡成功后立即进入 `succeeded`；
- 自动发送部分在 `after_message_sent` 后进入 `succeeded`；
- 手动发送失败后，实际回退为一条合并消息时，账本追加新的真实 fallback segment，不把失败气泡伪装为成功。

### 3. 同一回复不重复计数

`ConversationRuntime.record_bot_reply()` 新增可选 `internal_reply_id`。同一内部回复的后续成功气泡只更新累计可见文本与观察窗口，不重复增加用户互动次数。

### 4. 守卫替换文本

协议泄漏、内部推理泄漏和普通群友身份混淆被发送前守卫替换为安全文本时，记录的是替换后的真实可见文本，不是守卫前草稿。

## 验证

新增/更新回归测试覆盖：

- 单条回复在发送后钩子前没有成功观察；
- 多气泡逐条成功后才进入观察；
- 中途失败气泡保持 `failed` 且不写成功观察；
- 合并 fallback 发送成功后记录真实合并内容；
- 同一内部回复的多个成功 segment 只增加一次互动计数；
- 动态追加真实 fallback segment。

完整测试：413 项执行，410 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

- AstrBot 通用 API 仍没有平台 message ID，本层没有猜测或伪造。
- 当前 typed 账本保存在进程内，没有写原始群聊到仓库。
- 本层没有修改 AstrBot、LivingMemory、Meme Manager 或其他插件。
- 本层没有部署，没有 Git 写操作。

## 下一层

P6-03 将把当前内部发送状态提升为正式 `SentReplyRecord`：保存准确目标、真实成功/失败 segments、实际可见文本和可选平台 ID，并提供内容安全的查询/trace 边界。
