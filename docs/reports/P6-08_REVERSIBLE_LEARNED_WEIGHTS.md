# P6-08 可撤销学习权重

## 结论

学习产物现在具有显式、可审计、可回滚的启停状态。所有新 artifact 默认 `disabled`；只有通过结构化 API 明确设为 `enabled` 后，才可以以弱权重进入表达排序。禁用后影响立即归零。

不存在“累计到 3 个样本就自动改 Prompt”的路径。

## 状态模型

每个 artifact 的 `ArtifactActivation` 保存：

- artifact ID；
- `disabled / shadow / enabled`；
- 最大权重；
- revision；
- 更新时间；
- 结构化 reason code。

默认注册状态：

`disabled, weight_cap=0.15, revision=0, candidate_default_disabled`

## 启停和回滚

- `shadow` 可以计算 proposed adjustment，但实际权重始终为 0。
- `enabled` 才能产生有效调整。
- `disabled` 立即归零，不需要删除 cluster 或 artifact。
- 状态修改必须携带 expected revision，旧操作不能覆盖新操作。
- reason 只能使用小写结构化 code，不能把原始聊天或人工说明写进状态文件。
- 非候选/已孤立 artifact 不能启用。

## 权重边界

- 单 artifact `weight_cap` 必须位于 `[0, 0.25]`。
- 实际值为 `artifact.score × artifact.confidence × weight_cap`，并再次按 cap 截断。
- persona、situation、relationship scope、behavior ID 必须全部精确匹配；跨人格、主人/群友或跨情境时强制为 0。
- 合并到 StyleRetriever 前再次限制总 feedback score 到 `[-2, 2]`。

## 表达排序接入

普通生产聊天现在使用统一 `expression_feedback_scores()`：

1. 目标本人高置信历史反馈；
2. 当前 scope 的弱群体信号（最大绝对值 0.25）；
3. 只有显式 enabled 且完整适用范围匹配的 learned artifact。

StyleRetriever 原有排序公式不变。第三方表情包插件、Persona Prompt 和权限策略不受学习权重控制。

## 持久化

状态保存到运行数据目录 `learned_behavior_activation.json`：

- 默认策略明确为 disabled；
- 只保存 artifact ID、状态、cap、revision、时间和 reason code；
- 不保存群聊、回复、用户、persona 原文或 message ID；
- 使用临时文件 + 原子替换。

## 验证

回归覆盖：

- 候选默认 disabled；
- enable 产生有界弱权重；
- disable 后精确归零；
- shadow 有 proposed、无实际影响；
- 错 persona/scope 不生效；
- stale revision、非结构化 reason 和越界 cap 被拒绝；
- 激活状态安全保存/重载；
- `ConversationRuntime` 合并分数可启用并完整回滚；
- 群体信号只作为弱分量。

完整测试：451 项执行，448 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

- 当前没有 WebUI 自动开关，也没有自动启用策略；这符合“先观察、再人工决定”的发布边界。
- 本层没有部署，没有修改第三方插件，没有 Git 写操作。

## 下一层

P7-01 将为每个 session 引入 generation epoch。新入站消息改变 epoch，旧 epoch 的生成结果即使稍后完成，也不得进入发送阶段。
