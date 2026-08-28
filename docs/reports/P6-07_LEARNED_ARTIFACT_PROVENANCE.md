# P6-07 学习产物来源与适用范围

## 结论

P6-06 的 eligible cluster 现在只能转换为不可变、完整校验的 `LearnedBehaviorArtifact`。每个产物都有明确来源、适用人格、情境、关系范围、behavior、样本构成和置信度，并固定为 `candidate`，不会自动进入生产。

## 必填来源

每个 artifact 包含：

- schema version；
- deterministic artifact ID；
- source kind `aggregated_feedback_cluster`；
- source schema `behavior_outcomes.v1`；
- opaque persona key；
- situation ID；
- relationship scope；
- behavior ID；
- sample/high/low confidence counts；
- positive/negative weights、score、confidence；
- cluster 最近观察时间作为生成时间；
- activation state `candidate`。

## 置信度

置信度同时考虑：

- 样本量，20 个样本达到样本强度上限；
- 高置信样本占比；
- 分数本身只表示结果方向，不替代证据强度。

低置信群体样本可以贡献统计，但不能伪装成目标本人的高置信证据。

## 拒绝条件

以下产物无法生成或加载：

- 样本少于 3；
- persona/situation/relationship/behavior 缺失；
- relationship scope 不在 owner/public_group/peer；
- high + low count 与 sample count 不一致；
- 非有限数值、负权重、越界 score/confidence；
- 缺失生成时间；
- 新产物试图直接标记为 enabled；
- artifact ID 与不同适用范围发生冲突。

## 持久化

候选产物保存到运行数据目录 `learned_behavior_artifacts.json`：

- 顶层策略固定 `candidate_only`；
- 只保存聚合 provenance；
- 不包含原始问题、完整回复、reviewer、用户/群/消息 ID、persona name 或 voice card；
- 加载时逐条重新验证，非法项跳过。

## 验证

回归覆盖完整 provenance、单样本拒绝、缺字段/计数/非有限值拒绝、persona/scope artifact ID 隔离、candidate-only 持久化和脱敏 trace。

完整测试：443 项执行，440 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

本层只生成候选，不自动启用、不改 Prompt；启停与局部回滚由 P6-08 处理。没有部署、第三方修改或 Git 写操作。
