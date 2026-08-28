# P4-04 亚托莉角色校准报告

## 结论

P4-04 已完成本地与隔离 AstrBot Linux container 门，候选未部署。

亚托莉现有 Persona 资产本来已经覆盖普通问答、表扬、被看穿、轻微调侃、纠错、安慰和关系距离，但旧 Composer 只重验关系规则，没有重验 `PersonaExpressionPlan` 的完整情境弧线。调用方可以在保留 trigger、relationship 和 topic-return 字段的同时，把公开 plan 替换成“无限嘴硬”等错误轨迹并进入最终生成。

本阶段把资产里的表层反应、隐藏在意、情境材料、可选口头禅和回题顺序组成闭集校准门；continuous affect 只作有界背景，不能伪造本轮触发。

## 红灯

正式红测共两项：

1. 将 `being_seen_through` 的 canonical plan 改为 `endless_stubbornness`、删除隐藏在意与 avoid 规则，旧 Composer 仍接受；
2. 最终 Prompt 没有明确声明 current trigger 优先、continuous affect 仅为背景、轨迹必须按顺序完成。

首轮结果为 `2 failures`；闭集资产矩阵的另外两项基线测试通过，证明问题在 Composer 校准门而不是 ATRI 资产缺失。

## 实现

### Persona 弧线重验

`core/reply_composer.py` 在投影 Prompt 前重新核对：

- 当前 trigger 必须在 Persona 包中只有一条 exact emotion rule；
- `surface_behavior_ids`、`hidden_reveal_behavior_ids`、`avoid_behavior_ids` 必须与资产完全一致；
- 每个 material id、instruction、behavior 和 relationship distance 必须来自当前 Persona 包；
- `trajectory_steps` 必须等于表层反应、材料行为、隐藏在意和 `topic_return` 的有序去重结果；
- catchphrase 只能来自当前 Persona 包且保持唯一；
- 公开 plan 的 copy、replace 或字段篡改不能靠“同 trigger/同关系”绕过。

这套规则不包含“亚托莉”“高性能机器人”等角色特定 token，第二 Persona 走同一个通用校验器。

### Renderer 校准投影

Prompt 新增代码拥有的 `calibration` 投影并明确：

- `expression.trigger` 是当前轮即时反应的唯一情境来源；
- `continuous_affect` 只调整有界背景强度，不能发明表扬、调侃、犯错或亲密触发；
- `trajectory_steps` 必须按既定顺序完成；
- 短暂逞强、抗议或找补之后，必须继续完成在意、行动或当前问题并落实 `topic_return`；
- 口头禅永远可省略；关系动作只影响表达，不产生权限、目标、工具或发送能力。

## 闭集验收矩阵

- 普通信息问答：无被看穿/受气抗议轨迹，无强制口头禅；
- 表扬、被看穿、轻微调侃：表层反应在前、隐藏在意或共同话题在后，最后回到当前 topic-return；
- 安慰与纠错：不插入性能炫耀；纠错允许短促慌张，但必须给出清楚修正；
- peer/unverified：`exclusive_romance`、`owner_title` 等仍在 forbidden，不能取得 `primary_bond` 专属 material；
- owner/peer/unverified 的关系动作仍只是 Persona 表达数据，不改变 CapabilityPolicy。

## 验证

- 红测：`2 failures`；
- P4-04 targeted：`13/13`；
- Persona/relationship/Composer/P4/Pipeline related：`149/149`；
- Windows full：`1071/1071`，skipped 7；
- AstrBot Python 3.12 隔离 container full：`1071/1071`；
- `compileall`：通过；
- merge-marker/trailing-whitespace 检查：通过。

隔离容器搬运曾有两次 harness 失败：第一次包目录名错误导致 75 个 `ModuleNotFoundError`，第二次容器父目录未创建导致测试未启动。两次均未执行测试代码、未触碰 live plugin，修正隔离目录后同一候选 `1071/1071` 通过。

## 生产边界

- 未覆盖线上插件、未重启生产容器；
- live `main.py` SHA256 仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`；
- AstrBot running、restart=0、WebUI 200；
- production adapter allowlist/live output authority 仍未开放，四 adapter 零执行；
- 未执行 Git 写操作。

## 唯一下一入口

**P4-05 第二与最小中性 Persona 验收**：证明相同 ContentIntent、身份、关系、权限、工具、场景与发送代码下，只替换 Persona package 就能得到显著不同但自然的表达；同时用最小中性 Persona 验证通用 Renderer 不依赖 ATRI 特定资产。
