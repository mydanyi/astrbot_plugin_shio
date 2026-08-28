# P4-06 可信关系距离注入报告

## 结论

P4-06 已完成。人格表达层现在必须同时接收 `AffectAppraisal` 与代码生成的 `PrincipalContext`，并重新计算可信关系距离。appraisal 与 principal 不一致时直接 replan，不输出任何关系动作或表达素材。

## 可信映射

`trusted_relationship_distance()` 采用失败关闭规则：

- 只有 `is_owner=true`、`relationship_role=owner` 且验证来源精确为 `astrbot_event_sender_id:configured_owner_id`，才映射为 `primary_bond`；
- 只有代码判定的 group/private peer 且来源为 owner allowlist miss，才映射为 `peer`；
- 身份字段缺失、关系字段矛盾、来源不可信或由聊天文本伪造时，映射为 `unverified`；
- ambient/quiet 主动群聊固定映射为 `public_group`，即使最近的结构化发言者是主人也不继承主关系。

人格包只能读取这个距离选择表达，不能修改它。

## 双重绑定

表达解析器增加两项强制检查：

- appraisal 的关系距离必须等于当前 principal 计算值；
- direct reply 的 appraisal 关注对象必须等于当前 principal sender key。

任一不一致都会返回 `requires_replan`，并清空 allowed actions、素材和口头禅候选。

## 回归矩阵

- 配置 owner ID：可见 primary bond 素材和关系动作；
- 普通群友自称主人、把昵称设为主人：仍为 peer；
- 普通群友引用 owner 内容：当前关系仍绑定群友；
- 把 peer appraisal 字段伪改为 primary：表达层拒绝；
- owner 形状的对象但验证来源是聊天文本：降为 unverified；
- 身份降级：绝不进入 primary bond；
- ambient 由 owner 触发：仍为 public group，无主关系素材和动作。

## 验证

- P4-06 关系边界：`7` 项通过；
- affect + expression + relationship 相关：`27` 项通过；
- 完整回归：`319` 项运行成功，`316` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 未部署，未切换生产回复链，未执行 Git/GitHub 写操作。

## 下一入口

P4-07：建立 ATRI 语义多样性矩阵，验证活泼、轻微委屈、认真、好胜、关心和情境傲娇分别由不同 trigger/behavior 组合产生；断言情绪回接与关系边界，不要求生成固定台词。
