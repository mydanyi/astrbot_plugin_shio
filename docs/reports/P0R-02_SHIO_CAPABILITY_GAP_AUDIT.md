# P0R-02 星汐当前代码能力与缺口审计

## 审核基线

- 当前稳定基线：P8-12 typed-only 直答链。
- 本轮发现方向错误后，未完成的 P8-13 局部主动参与代码已撤回，未部署。
- 完整本地测试：`363/363` 通过。
- 这 363 项主要证明类型、安全、权限、发送和并发边界；不等于已经具备 MaiBot 式真人对话体系。

## 当前真实热路径

```text
AstrBot 直接唤醒 / 自然称名被升级为直接唤醒
→ TurnEnvelope / PrincipalContext
→ ReplyTarget / ReferenceContext
→ 身份隔离历史与 LivingMemory facts
→ CapabilityPolicy
→ 单轮 AffectAppraisal + PersonaExpressionPlan
→ LocalChatPlan（不是行为 Planner）
→ 单次 ReplyComposer
→ OutputValidator / 最多一次严重错误修复
→ 气泡拆分、generation epoch、发送回执
```

这是一条可靠的“被唤醒后安全直答”链，不是持续群聊心智循环。

## 已验证并应保留的底座

| 能力 | 结论 | 代码证据 |
|---|---|---|
| 可信发送者与 scope | 已实现 | `core/identity.py` 的 `TurnEnvelope`、`PrincipalContext`、`build_scope_key()` |
| 主人/群友判定 | 已实现 | 只认结构化 sender ID 与 owner allowlist；昵称、自称、引用不提权 |
| 群友聊天工具与主人高权限 | 已实现（直接轮次） | `core/capability_policy.py` 的来源、能力、副作用与精确白名单裁决 |
| 当前消息与引用目标绑定 | 已实现（直接轮次） | `core/context_assembler.py` 的 `ReplyTarget`/`ReferenceContext` |
| 工具结果 typed 化 | 已实现 | `core/tool_result.py`；结果绑定 scope 与目标，不传工具参数到可见回复 |
| 协议/思维标签/主人关系越界守卫 | 已实现 | `core/output_validator_v2.py`、`core/response_guard.py` |
| 过时回复取消与发送前复核 | 已实现（直接轮次） | `core/generation_epoch.py`、`generation_cancellation.py` |
| 逐气泡发送回执和成功后学习记账 | 已实现 | `core/send_receipt.py`、`main.py` 发送阶段 |

## 结构性缺口

### 1. 模块存在不等于产品能力闭环

- `assemble_context_views()` 构造了群聊 `planner_records/public_background`，但当前没有行为 Planner，ReplyComposer 只读取 `replyer_thread`。所以能隔离他人，却不能真正理解完整公共群聊。
- 亚托莉人格包包含角色事实，但 ReplyComposer 没有消费 `character_facts`；原 AstrBot system persona 又会被 typed Composer prompt 替换。角色原作知识和长期动机没有完整进入热路径。
- Persona 关系规则里的允许/禁止动作没有全部进入生成数据，主人/群友表达主要依靠 `boundary_style` 和末端文本守卫。
- `PresentationHandoff` 被创建和记录，但仓库内没有明确下游消费闭环；当前表情效果主要来自第三方 Meme Manager 自身。
- 学习候选默认 disabled，运行链无用户可见激活/审核闭环；`InteractionProfile.affinity` 只存不读。

### 2. 当前不是“感知—决策—行动”架构

- `build_direct_chat_plan()` 固定 Planner 调用预算为 0；它只是回复参数封装，不会选择 Reply/Wait/React/Tool/Initiate。
- `ConversationRuntime` 明确没有参与生成器。
- README 明确承认未点名接话和主动开题已删除。
- `metadata.yaml` 仍宣称有“主动参与、说话规划”，与代码不符。

### 3. 自然称名承担了不该承担的职责

`classify_name_wake()` 只输出 `direct/mention/none`；CustomFilter 对 mention 返回 False。结果是“是否语法上直接叫我”被错误等价成“我是否愿意参与”。需要把 Address、Attention、Participation、Action 四层拆开。

### 4. 角色与情绪只做到单轮表达素材

- Affect 主要靠当前一句正则映射一个 trigger，没有短期情绪状态、惯性、衰减、对象和多轮余韵。
- Affect 不读取群聊背景、上轮情绪、关系经历或机器人自身行为结果。
- 人格包可替换的 loader/schema 已有，但角色事实、关系动作、兴趣和参与偏好未完整接入。
- 因此继续添加“才没有”“高性能机器人”等台词只会加重模板感。

### 5. 目标 ID 正确不等于语义回答当前问题

`OutputValidationContext.semantic_requirements` 运行时通常为空。Validator 能确认 target ID，却不能稳定验证回答是否覆盖当前问题的核心语义。这会出现“对象没有串，内容却像在回答上一轮”的假安全。

### 6. LivingMemory 无主体召回仍可能污染

明确带主体的他人事实会进入禁止区，这是正确的；但无法解析主体的召回会被降级成 group/public background，随后可能作为 grounding fact 进入 Composer。星汐必须对无主体、低相关度或与当前问题冲突的召回进行隔离，不能修改 LivingMemory 本身来掩盖消费边界问题。

## 当前能力矩阵

| 能力 | 状态 | 决策 |
|---|---|---|
| 可信身份、主人判定 | 已验证 | 保留并扩展到所有新行为 |
| 群友联网/资料查询、主人高权限 | 已验证（直接轮次） | 保留；新行为必须复用同一 Broker |
| 当前目标与引用 | 已验证（直接轮次） | 保留；所有 Action 必须显式绑定目标或公共 scope |
| 群聊多人上下文 | 部分 | 公共场景进入决策层，个人事实仍隔离 |
| 自然称名 | 部分/职责混杂 | 改为 Address Signal，不再直接等价回复 |
| 未点名接话 | 缺失 | 新建 Participation Engine |
| 冷场主动开题 | 缺失 | 在参与层稳定后新增 Quiet Initiation |
| Wait/No-action/React | 缺失 | 作为一等行为实现 |
| 行为 Planner | 缺失 | 新建 typed Action Planner，不恢复旧 Planner |
| 当前问题语义计划 | 缺失 | 新建 Content Intent 与语义锚点 |
| 人格包 | 部分 | 接通角色事实、动作边界、兴趣和表达映射 |
| 连续情绪 | 缺失 | 新建通用 Affect State，Persona 只负责外显 |
| 自然表达 | 部分 | 保留单次最终生成；由行为/语义/情绪提供结构输入 |
| 去复读 | 部分且未完整接热路径 | 建立语义/结构/口癖三层重复检测 |
| 表情协同 | 证据不足 | 建立 React/ExpressionIntent 到第三方插件的真实消费契约 |
| 反馈与学习 | 脚手架 | 后置，增加审核、启用、撤销和权限不可学习约束 |
| 发送、并发、协议边界 | 已验证（直接轮次） | 泛化到主动、等待和表情动作 |
| 性能 | 部分 | 增加真实 P50/P95、模型调用次数和全局并发预算 |
| 配置/说明 | 不一致 | 按实际能力修正；不再把脚手架写成已实现 |
| 行为评测 | 缺失 | 新建多用户、多人格、参与/沉默、记忆、工具、并发矩阵 |

## 结论

P8-12 是值得保留的安全 typed 直答底座，但不是 MaiBot 对标体系。后续不能继续围绕截图逐条加正则；必须按新的总计划先完成行为模型和能力闭环，再扩展未点名参与、主动开题、连续情绪、表情和学习。
