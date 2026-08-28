# P5-06 一次修复与失败闭锁报告

## 结论

P5-06 已完成。新增有状态 `RepairController`，把输出修复从开放式“再重写一次看看”变成严格预算状态机。

## 状态机

- validator pass：直接发送，修复预算 0；
- 首次严重违规且 target 仍可信：唯一一次 `generate_once`，修复预算 1；
- target binding mismatch：立即 block，不允许 Planner、Composer 或 repair 猜目标；
- 修复后仍失败：不再分配模型调用；
- direct reply 只有在调用方提供“同一 validator 已通过”的本地降级文本时才可 `safe_degrade`；
- ambient/quiet 修复失败固定 block，不发送模板插话。

`repair_attempts_used >= 1` 时永远不能再次进入 `generate_once`。

## 修复请求

唯一 repair request：

- 只包含 issue codes、确定性解析后的 rejected visible text 和原 Composer 的生成数据；
- 不接收也不复制原始 hidden thought/analysis；
- 明确只修协议、身份关系、事实、语言、必需语义或空回复；
- 禁止重新设计风格、增加口头禅或扩写人设；
- `generation_call_budget=1`；
- `routine_style_rewrite_budget=0`。

## 验证

- P5-06 定向：`10` 项通过；
- 覆盖 pass、首次修复、空回复、target 闭锁、二次失败、已/未验证降级、ambient、超额 attempts 和 repair request；
- 完整回归：`388` 项运行成功，`385` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 未接入生产修复链，未部署，未执行 Git/GitHub 写操作。

## 下一入口

P5-07：对照 P0 调用基线，固化 simple/local、direct、small planner、special 和 repair 的模型调用预算；运行本地 fast path→routing→Composer request 微基准并报告 p50/p95。此层只能证明调用结构和本地开销，真实端到端生产 p95 必须等 P8 同口径影子样本。
