# P6-06 情境—行为—结果聚类

## 结论

星汐已建立脱敏的增量行为聚类链路。它不学习完整台词，只统计“某个人格在某种情境和关系范围内采用某个 behavior 后，真实反馈结果如何”。

单个样本不会形成可用倾向；同一桶至少累计 3 个样本才进入 `eligible_clusters()`。

## typed 样本

`SituationBehaviorResultSample` 只包含：

- `persona_key`：由当前 persona name + voice card 生成的不可逆 SHA-256 短指纹；
- `situation_id`：typed affect trigger，例如 praise、correction_or_mistake；
- `relationship_scope`：owner / public_group / peer；
- `behavior_ids`：实际表达候选 ID；
- feedback signal、confidence、evidence source、弱/强权重和时间。

不包含：

- 原始用户消息；
- 完整机器人回复；
- 用户/群/消息 ID；
- reviewer 或内部 reply ID；
- persona name 或 voice card 原文。

## 分桶方式

聚类键固定为：

`persona_key × situation_id × relationship_scope × behavior_id`

不同人格、情境、主人/群友关系和行为永不合桶，防止把“对主人有效的亲密表达”学到普通群友场景，也防止一个角色的习惯污染另一个角色。

## 结果权重

- 目标本人高置信反馈：1.0；
- 其他用户精确引用/reaction：0.35；
- 普通低置信群体反应：0.2。

聚类分别累计 positive/negative weight、样本数、高/低置信样本数和最近观察时间。单个群体反应不能等价于目标本人的明确反馈。

## 运行与持久化

- 聚类更新是每次已归因反馈后的 O(behavior count) 本地增量操作，不增加模型调用。
- 聚合结果保存到运行数据目录 `behavior_outcomes.json`，不写入仓库。
- 最多保留 2048 个 cluster，达到容量时淘汰最旧项。
- 状态文件采用临时文件 + 原子替换。

## 生产链路接入

- 生成阶段从可信 principal、ReplyTarget、typed affect appraisal、当前 persona 配置和实际 expression IDs 建立 `LearningContext`。
- 只有发送成功后，该 context 才进入 `ReplyObservation`。
- 只有 P6-04/05 已分类的反馈证据才会更新聚类。
- 没有发送成功、没有 feedback、没有 behavior ID 或缺少 typed context 时不产生样本。

## 验证

回归覆盖：

- 单样本不能成为 eligible cluster；
- 3 个同桶样本形成稳定聚类；
- persona/situation/relationship/behavior 严格分桶；
- 低置信群体反馈使用弱权重；
- 持久化和重载不包含 persona 原文、voice card、reviewer、reply 或台词；
- `ConversationRuntime` 真实反馈路径累计 3 个样本后才形成 cluster；
- 生产请求 payload 中学习 context 为 typed、脱敏字段。

完整测试：437 项执行，434 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

- eligible cluster 仍不是生产 Prompt 或自动权重；P6-07/P6-08 还要增加来源、置信度、启停和回滚边界。
- 本层没有部署，没有修改第三方插件，没有 Git 写操作。

## 下一层

P6-07 将从合格 cluster 生成带来源、persona、scope、样本数和置信度的 learned artifact；任何缺字段、单样本或跨 persona/scope 的产物都必须拒绝。
