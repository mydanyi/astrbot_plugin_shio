# P7-04 结构化脱敏日志边界

## 结论

星汐的 pipeline trace 和新增运行日志现在共用一套失败关闭的 metadata 策略。可观测性只保留 trace ID、结构化类别、不可逆摘要、计数、布尔状态和耗时；未知字符串不再靠截断“假装脱敏”，而是直接拒绝。

本层同时清理了主回复链、主动话题、补答队列、表达检索和行为学习中的旧日志变量，避免直接打印群号、sender ID、完整 plan、工具列表或异常消息。

## 根因

P7-03 之前的 `record_pipeline_stage()` 对非数字字段统一执行 `str(value)[:80]`。这可以限制长度，但不能阻止以下内容进入 trace：

- 聊天正文、Prompt 或完整请求片段；
- sender/group/message/session 等真实 ID；
- Authorization、Token、Cookie、API Key、Secret、Password；
- base64 或 Provider 异常消息里夹带的请求/响应内容。

主链旧 debug log 还曾直接输出真实 group/sender、工具名列表和 `plan.to_dict()`；后台任务日志也曾直接输出真实群/用户 ID 与完整异常文本。

## 统一策略

新增 `core/observability.py`：

- `diagnostic_digest()`：对主体、群、目标、Provider 和内部记录 ID 生成不可逆 16 位摘要；
- `safe_exception_kind()`：只返回异常类名，绝不返回异常消息；
- `sanitize_trace_metadata()`：执行统一、失败关闭的 metadata 规则；
- `structured_log()`：输出单行 JSON 诊断记录，并验证 32 位十六进制 trace ID。

字符串字段只有以下情况可以保留：

- 已验证的 `*_digest` / `*_fingerprint`；
- source/status/kind/model/route/code/mode 等受控分类字段；
- 受控的 failure kind、action、outcome、trigger。

正文、Prompt、request/response body、原始身份字段、凭证字段及未知字符串均丢弃。数字和布尔字段仅在键名不是原始身份/凭证字段时保留。

## 主链日志

`reply.prepared` 与 `reply.guard` 现在能够关联：

- event 的 trace ID；
- `source_kind`；
- 当前主体摘要与目标 message 摘要；
- Planner/Replyer 历史条数、表达数、允许工具数；
- 实际工具结果数、守卫命中数、是否严重、是否尝试单次修复；
- 从入站开始计算的阶段耗时。

发送成功日志只记录 trace ID、主体/目标摘要、段数、字符数和耗时；发送或修复失败只记录异常类型。

## 旧日志清理

已把含变量数据的以下日志迁移到统一结构化入口：

- 自然唤醒、群聊参与决策；
- 主动话题排队、Provider 尝试、让位与发送；
- 补答入队、失败、引用降级与发送；
- 权限工具阻断、联网工具开放/缺失、表达工具保留；
- 请求准备、输出修复、身份/关系/情绪守卫；
- 表情引用清理、气泡发送、发送观察；
- 表达库、Embedding、Reranker、学习状态与队列持久化错误。

固定、无变量的运行提示仍可保留普通文本日志；它们不包含用户内容、真实 ID 或凭证。

## 验证

新增回归覆盖：

- prompt、content、Authorization、API Key、request body、raw sender/message ID 被拒绝；
- 数字形式的 message ID 同样被拒绝，不能绕过字符串过滤；
- 只有合法摘要、计数、布尔、类别、耗时进入结构化日志；
- 异常消息含 Bearer secret 时只留下 `RuntimeError`；
- 真实主回复 debug 链生成 `reply.prepared` 与 `reply.guard`，包含同一 trace ID、守卫计数和 latency，但不含聊天正文、真实 sender ID、真实 message ID 或完整 plan；
- trace 快照对敏感键和值同样失败关闭。

静态复查未发现 `logger.exception`，也未发现仍以 `group=%s`、`sender=%s` 或 `message_id=%s` 输出真实标识的调用。

完整测试：472 项执行，469 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

- 摘要用于同一日志窗口内诊断，不作为身份认证或持久化用户画像键。
- 不把 trace ID 当成平台 message ID 回执。
- 本层没有部署，没有修改 AstrBot、LivingMemory、Meme Manager 或 Provider，没有 Git 写操作。

## 下一层

P7-05 将基于现有 TraceStage 建立阶段化指标汇总，分别统计 target/context/planner/tool/replyer/guard/bubble/send 的次数、耗时、失败和丢弃原因，使一次答非所问可以快速定位到破坏语义的具体阶段。
