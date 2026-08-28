# P7-05 阶段化流水线指标

## 结论

星汐现在可以把一条 trace 拆成 ingress、target、context、planner、tool、replyer、guard、bubble、send 和 concurrency 十类阶段指标。每类指标独立统计阶段次数、耗时、调用、失败、守卫、修复、过时丢弃和发送结果。

以后看到“答非所问”时，不再只能从一段长日志猜测：

- target/context 异常优先查认人、引用和记忆归属；
- planner/tool 异常优先查计划或外部资料；
- replyer/guard 异常优先查生成、口语化和修复；
- bubble/send 异常优先查拆分、重复、漏发或平台发送；
- concurrency stale drop 表示旧问题已被新消息取代，不能发送。

## 强类型指标

新增 `core/pipeline_metrics.py`：

- `PipelinePhase`：固定阶段分类；
- `PhaseMetric`：单阶段聚合；
- `PipelineMetricsSnapshot`：一条 trace 的不可变指标快照；
- `summarize_pipeline_metrics()`：从 `TraceStage.elapsed_ms` 计算相邻阶段实际耗时；
- `store_pipeline_metrics()`：把终态快照挂到当前 event，供本地诊断或测试读取。

快照不复制任何 stage metadata 字符串、聊天正文或真实 ID，只保留 trace ID 和数字/分类指标。

## 调用与失败计数

- Planner 通过回调记录真实 Provider 尝试次数及失败次数；主 Provider 失败再走备用时计为两次，而不是只记录配置候选数。
- Tool 读取 typed tool result 的调用、成功和 orphan 状态；失败与 orphan 重合时只计一次失败事件。
- Replyer 的 AstrBot `role=err` 记为 typed `replyer_failed`，不把 Provider 错误正文写入指标。
- Guard 记录命中数量、严重命中和是否发起单次修复；修复另记真实调用数、结果或失败类型。
- Bubble 记录规划的实际段数。
- Send 区分 handoff/attempt、success 和 failed；先失败再由自动单条发送成功会标记为 `sent_with_failure`。
- stale generation 独立计入 concurrency，不和 Provider/发送失败混为一类。

## 终态

当前终态分类：

- `in_progress`；
- `guarded`；
- `ready_to_send`；
- `sent`；
- `sent_with_failure`；
- `send_failed`；
- `stale_drop`；
- `missing_trace`。

自动发送确认或 stale 丢弃时会保存快照；开启 debug log 时额外输出一条 `pipeline.metrics` 结构化脱敏记录。

## 验证

回归覆盖：

- 正常 target/context/planner/tool/replyer/guard/bubble/send 全链路的分段耗时；
- Planner 真实调用计数；
- 两个 tool result 中一个失败/orphan 的归因；
- Replyer `role=err` 的阶段失败；
- 守卫命中与单次修复；
- stale generation 独立丢弃；
- 手动气泡发送失败后自动发送成功标记 `sent_with_failure`；
- 三气泡全部发送成功的 planned/success 数量；
- 指标序列化不包含原始 stage 指纹字段或正文。

完整测试：475 项执行，472 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

- 阶段耗时使用 event 内单调时钟的累计值之差，不用于跨主机时间比较。
- 指标只诊断链路，不参与身份认证、权限判定或模型 Prompt。
- 本层没有部署，没有修改 AstrBot、LivingMemory、Meme Manager 或 Provider，没有 Git 写操作。

## 下一层

P7-06 将建立组合并发回归：快速连续提问、用户切换、引用旧消息、慢 Provider/工具返回、新消息打断、气泡发送中断，验证旧答案不会重复、漏发或绑定给新用户。
