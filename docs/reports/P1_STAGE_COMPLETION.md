# P1 行为契约与评测地基阶段报告

## 阶段结论

P1 已完成，P2 可以开始。星汐现在拥有一套 Persona 无关、模型不可覆盖权威字段的行为合同，以及能够持续复用的合成评测、产品 Trace 和生产插件共存门禁。

P1 明确没有接入 `main.py` 的真实回答链，因此本阶段不宣称已经修复生产中的认错人、串记忆、图片归属或自然称名问题；这些合同和失败场景是 P2 接线的硬约束，避免继续围绕单个日志补正则或 Prompt。

## 交付

| 任务 | 结果 | 报告 |
|---|---|---|
| P1-01 typed 行为合同 | 共同 binding；Ingress、来源、Address、Attention、Participation、Action、Memory、Knowledge、Media、Content、Expression、Presentation 闭集 | `P1-01_TYPED_BEHAVIOR_CONTRACTS.md` |
| P1-02 完整 fixture 矩阵 | 24 个合成场景，15 个维度闭集全覆盖，12 个关键交叉 case，严格隐私门 | `P1-02_FIXTURE_MATRIX.md` |
| P1-03 三层评测 harness | 确定性、严格 Stub、显式 opt-in 外部模型；同一 fixture、语义断言、调用预算和脱敏报告 | `P1-03_EVAL_HARNESS.md` |
| P1-04 产品 Trace | 17 个闭集阶段、唯一终态、revision/epoch、effect receipt；修复观测元数据泄漏和 blocked 误分类 | `P1-04_PRODUCT_TRACE.md` |
| P1-05 插件 conformance | LivingMemory、ReNeBan、AnySearch、Meme、Parser 的六状态、hook 顺序、历史和 effect 预算 | `P1-05_PLUGIN_CONFORMANCE.md` |

## 统一验证

- P1 重点测试：`37/37` 通过；
- 仓库完整测试：`412/412` 通过；
- 24-case 确定性 CLI：退出码 `0`；
- 24-case Stub CLI：退出码 `0`；
- 外部模型未显式 opt-in：联网前拒绝，退出码 `2`；
- `compileall core tests scripts`：通过；
- `git diff --check`：通过；
- 未部署、未修改第三方插件、未执行 Git/GitHub 写操作。

## P2 入口

从 P2-01 开始建立不可变生产 `ConversationEvent`、revision 和真实来源适配；P2-02 必须把准入移到 `runtime.ingest`、Scene、记忆消费、Affect、Learning、工具和回复之前。P2 完成前不直接调自然称名 Prompt、主动接话阈值或人格台词。
