# P7-02 安全生成取消与不可取消降级

## 结论

新 epoch 到来时，星汐现在会主动取消自己创建、且 Provider 明确声明 `supports_cancellation=true` 的旧 generation 子任务。Provider 没有该声明时不强杀，旧调用可以完成，但结果仍由 P7-01 丢弃。

“Provider 不能取消”不再等于“旧答案可以发送”。

## 所有权边界

`GenerationTaskRegistry` 只登记 `ShioPlugin` 通过 registry 创建的子任务：

- Planner 调用；
- 严重输出违规后的单次修复调用。

不会登记或取消：

- AstrBot 主事件任务；
- AstrBot 自己发起的最终 Replyer 调用；
- LivingMemory、Meme Manager 或其他插件任务；
- Provider 未明确声明 cancel-safe 的调用。

## 能力判定

仅接受两种明确声明：

- `provider.supports_cancellation is True`；
- `provider.metadata["supports_cancellation"] is True`。

不会根据 Provider 名称、SDK 类型、是否为异步函数或超时行为猜测取消能力。

Planner 同时配置主/备用 Provider 时，只有所有可能被调用的 Provider 都明确支持取消，整个 Planner 子任务才标记为 cancel-safe。

## 取消行为

- 新 epoch 只取消同 scope、epoch 更旧的安全子任务。
- 跨 session 不取消。
- registry 取消产生 typed `SupersededGeneration`，不会把 `CancelledError` 误传播成整个 AstrBot 服务取消。
- Planner 被安全取消时，星汐终止该旧 event 的继续传播。
- 修复生成被取消时，completion 清空且不发送未修复旧文本。
- 子任务完成、失败或取消后都从 registry 清理。

## 不可取消降级

未声明能力的 Provider 直接在原 await 中运行，不注册为可取消任务。新 epoch 到来后：

- 调用不被错误强杀；
- 完成结果先经过 epoch validation；
- stale completion/result chain 置空；
- 不进入气泡、发送或反馈链。

## 验证

回归覆盖：

- 同 scope 安全旧任务被取消并转换为 `SupersededGeneration`；
- 不安全任务不被取消且正常清理；
- 跨 scope 不取消；
- 只有显式 capability 被接受；
- 真实 Planner 并发测试中 cancel-safe Provider 收到取消、旧 event 停止；
- 普通 Provider 完成后旧结果仍被丢弃。

完整测试：465 项执行，462 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

- AstrBot 框架拥有的最终 Replyer 调用不由星汐强行取消；P7-01 已保证其过时结果无法发送。
- 本层没有部署，没有修改 Provider、AstrBot 或第三方插件，没有 Git 写操作。

## 下一层

P7-03 将引入每轮回复唯一、脱敏的 trace ID，贯穿目标、记忆、计划、工具、输出、气泡、发送和反馈，便于从日志定位一条完整链路。
