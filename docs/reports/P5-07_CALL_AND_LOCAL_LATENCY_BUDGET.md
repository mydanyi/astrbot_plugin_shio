# P5-07 调用预算与本地延迟对照报告

## 结论

P5-07 已完成。v2 调用结构已与 P0 基线形成代码级对照：普通聊天稳态从最低 4 次 Provider/模型调用降为 1 次 ReplyComposer，复杂结构场景从最低 4 次降为最多 1 次 small Planner + 1 次 ReplyComposer。

## 调用数对照

| 场景 | P0 最低调用 | v2 稳态预算 | 首次严重违规上限 |
| --- | ---: | ---: | ---: |
| simple/local 或普通 direct | 4 | 1 | 2（唯一一次 repair） |
| complex structured | 4 | 2 | 3（异常 repair，不属于稳态） |
| target/identity blocked | 不适用 | 0 | 0 |

普通聊天稳态调用数减少 3 次，即 75%。P0 严重违规普通聊天最低 5 次；v2 simple 首次严重违规最多 2 次。

以上是调用预算，不代表目前线上已经切换。当前生产仍是 v1。

## 热路径依赖

P5 本地路径模块不导入 `style_retriever`、Embedding Provider 或 Reranker。简单聊天在模型生成前只执行：

1. fast path；
2. planner route（结果为 Planner 0）；
3. 本地表达候选；
4. ReplyComposer request 构造。

## 本地微基准

本机 Python 运行 100 次预热后测量 2000 次完整“fast path → routing → Composer request”纯本地循环：

| 指标 | 结果 |
| --- | ---: |
| p50 | 0.4834 ms |
| p95 | 0.5867 ms |
| max | 1.5288 ms |
| 实际 Provider 调用 | 0 |
| 计划 Planner 调用 | 0 |
| 计划 Composer 调用 | 1 |

这只证明本地重构逻辑没有引入可观的预模型阻塞，不是生产端到端 p95。

## 真实 p95 边界

P0 已明确旧日志没有同一目标消息贯穿输入、Planner、Replyer、守卫和发送的统一 trace，因此当前不能可靠声称真实 p95 已改善。生产端到端 p50/p95 必须等 P8 影子部署后，按同一 target/trace 采集至少 30 个普通直接聊天样本，再与 v1 同口径比较。

## 验证

- P5-07 定向：`6` 项通过；
- 本地微基准：2000 次完成，预算断言通过；
- 完整回归：`394` 项运行成功，`391` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 未接入生产链，未部署，未执行 Git/GitHub 写操作。

## 下一入口

P5-08：保持现有 Meme Manager 不变，在最终 Composer 输出通过 validator 后生成 typed presentation handoff；只传最终可见语义、情绪/行为标签和最多一次本地呈现预算，不传 Planner 草稿、记忆、身份 ID、工具参数或内部协议。
