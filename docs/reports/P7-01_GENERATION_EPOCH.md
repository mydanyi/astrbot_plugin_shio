# P7-01 Session Generation Epoch

## 结论

每个 typed scope/session 现在都有独立 `generation_epoch`。同一会话出现新入站消息时 epoch 原子递增；旧请求即使稍后生成完成，也会在 LLM 输出守卫和最终发送边界被丢弃。

不同群/私聊 session 互不失效。

## 绑定

- `GenerationEpochSnapshot` 只包含 scope、session、epoch 和开始时间。
- 同一 event 只绑定一次，WakingStage 和 LLM request hook 不会重复递增。
- 群聊中所有有效入站事件在正文解析前推进 epoch，因此图片/空文本事件也能打断旧 generation。
- 私聊在进入星汐普通回复链时推进 epoch。
- 缺 scope 或 session 时闭锁，不创建“全局默认会话”。

## 发送守卫

检查点：

1. LLM response 进入口语化/修复守卫前；
2. `on_decorating_result` 进入气泡处理前；
3. 每个手动气泡发送前；
4. 最终自动消息交还 AstrBot 前。

命中过时 epoch 时：

- completion 置空；
- 清除表情包选择；
- 最终 result chain 清空；
- 不创建成功发送观察；
- 写脱敏 `stale_generation_drop` trace stage。

如果第一条气泡发送成功后才来了新消息，已经发出的内容无法撤回，但其余旧气泡会立即停止，最终链清空。

## 并发和资源边界

- registry 使用锁保护原子递增/验证。
- 最多保留 2048 个 scope，避免无界增长。
- snapshot trace 不输出 scope/session/消息 ID，只输出 epoch 和字段存在性。

## 验证

回归覆盖：

- 同 session 新消息使旧 snapshot stale；
- 不同 session 互不影响；
- 同 event 不重复推进；
- 缺 scope/session 闭锁；
- 旧 LLM result 在 guard 前丢弃；
- 旧 result 在最终发送边界清空；
- 气泡发送过程中被新消息打断时停止剩余气泡；
- trace 不含 scope/session 值。

完整测试：459 项执行，456 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

- P7-01 保证“不发送旧结果”，但仍可能让不支持取消的 Provider 把旧请求算完；P7-02 处理主动取消和完成后丢弃策略。
- 本层没有部署，没有修改 AstrBot/Provider/第三方插件，没有 Git 写操作。

## 下一层

P7-02 将建立 cancellation registry：支持取消的本地异步阶段在 epoch 变化后主动中断；Provider 不暴露取消能力时仍由 P7-01 完成后丢弃，绝不因为无法取消而发送旧答案。
