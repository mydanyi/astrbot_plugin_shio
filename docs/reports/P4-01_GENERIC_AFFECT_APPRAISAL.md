# P4-01 通用情感评估模型报告

## 结论

P4-01 已完成。新增 `core/affect.py`，情感内核只描述情境和反应轨迹，不生成角色台词，也不包含任何特定角色名称、口癖或世界观。

## 模型字段

`AffectAppraisal` 包含：

- `trigger`：本轮触发情境；
- `focus_sender_key`：可信关注对象；
- `relationship_distance`：主要关系、普通同伴、公共群聊或未验证；
- `surface_emotion` / `secondary_emotion`：表层和次级情绪；
- `hidden_concern`：能力感、关系、对方状态、纠正误解、修复信任或边界；
- `behavioral_tendency`：回答、承认、反应、软化、澄清、修复、安慰、帮助、保持边界等动作；
- `topic_return`：情绪之后如何回到当前问题或群聊话题；
- reply target 的 message ID/content digest、置信度和降级原因。

## 情境覆盖

- 普通中性问答；
- 夸奖；
- 逗弄和被看穿；
- 认错人、答错或前后矛盾；
- 对机器人表达关心；
- 用户需要安慰；
- 道歉、感谢、分歧；
- 信息请求与行动请求；
- 主动接入公共话题与安静后发起话题。

这些轨迹只表达“先反应、再软化、再回题”等结构，不指定任何固定台词。傲娇、冷静、温柔或其他人格如何外显，留给后续 `PersonaPackage`。

## 身份与目标边界

- 关系距离只读取 `PrincipalContext`。
- 直接回复必须有可执行 `ReplyTarget`。
- principal sender 与 target sender 不一致时，关注对象置空并要求 replan。
- 当前消息 digest 与目标 digest 不一致时要求 replan。
- ambient/quiet 统一为公共群聊关系，不继承目标 owner。

## 回归覆盖

- 10 组情感、关系、目标和主动群聊矩阵。
- 认错/答非所问必须走承认、修复和继续，不允许只做防御。
- 被看穿必须包含反应和软化，不建模为持续否认。
- 用户难受时以对方状态为隐藏关注并回到后续照顾。
- 测试直接读取 `core/affect.py`，确保不存在特定角色专名和固定口癖。

## 验证

- P4-01 定向：`10` 项通过。
- 完整回归：`280` 项运行成功，`277` 项通过，`3` 项为迁移计划中保留的 v1 expected failure。
- `git diff --check`：通过。
- 未修改生产 Prompt、未部署、未执行 Git/GitHub 写操作。

## 下一入口

P4-02：定义通用 `PersonaPackage` schema 与校验器，检测缺字段、ID/版本问题、互相冲突的关系规则、越权 owner 配置、固定口癖滥用和表达素材越界；仍不把具体人格硬编码进通用内核。
