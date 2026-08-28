# P3-01 / P3-08E3B / P3-08E3C0：Typed Action Planner 报告

日期：2026-08-18（Asia/Hong_Kong）

## 结论

Action Planner 的 owner 动作语义已从旧的“模型 broad capability → `USE_TOOL`”改成独立、不可混淆的 `EXECUTE_ACTION`：

```text
accepted current turn
  + structural gate
  + exact OwnerActionRouter
  + exact OWNER_ACTION ticket
  + opaque canonical OwnerActionRouteDecision
  + verified owner CapabilityPolicy
→ EXECUTE_ACTION(capability + exact OwnerActionOperation + exact ReplyTarget)
```

`USE_TOOL` 现在只表示 KnowledgeGap 驱动的知识取证，只有 broad capability，没有 operation。模型、公开 `OwnerActionProposal`、昵称、自称、历史文本或 Persona 都不能制造 `EXECUTE_ACTION`。

本层没有接入 `main.py`，没有执行工具，没有改变配置、FNOS 或 Git 状态。

## 合同变化

`core/contracts/behavior.py`：

- `ActionKind` 新增 `EXECUTE_ACTION`；
- `ActionDecision.operation_intent` 为闭集 `OwnerActionOperation | None`，不是字符串；
- `EXECUTE_ACTION` 必须同时有 exact current `ReplyTarget`、非空 capability 和 exact operation；
- `USE_TOOL` 必须有 capability，operation 必须为 `None`；
- 其余 Action 的 capability 和 operation 必须为空；
- 模型 payload 的 `operation` / `operation_intent` 与其他 authority 字段直接拒绝，模型 `execute_action` 也直接拒绝；
- `AddressKind`、`AddressEvidence`、`AttentionLevel`、`ParticipationLevel`、`ActionKind`、`ModelActionSuggestion.action_hint` 均做 exact Enum 类型门；
- `ReplyTarget` 必须是 exact `ReplyTarget`，同形恶意 carrier 不能通过。

`PlannedAction.action_id` 的摘要现在包含 `operation_intent.value`。同一 binding、同一 capability 下，`ARTIFACT_READ_EXACT` 与 `ARTIFACT_GREP` 会生成不同 action ID；operation 不再只靠后续阶段隐式区分。

E3C0 新增长期 `PlannedActionAuthority`。公开 `PlannedAction` 只是 shape；只有经同一个 Planner authority 登记的 exact object 才能进入 Controller。authority 会保存 exact plan / ActionDecision / binding / ReplyTarget identity 与完整 snapshot，并重新计算 action digest。copy/deepcopy、公开重建、跨 authority、`object.__setattr__` 篡改 action ID、operation、binding、嵌套 ActionDecision 或 ReplyTarget 全部失败关闭。ledger 有硬上限；旧 exact plan 被淘汰后不会恢复 authority。

## Planner 流程

第一段 `structural_action_gate()` 仍优先处理：

- denied ingress / ignore / no participation → `NO_ACTION`；
- cooldown wait → `WAIT`；
- react-only → `REACT`；
- 正常直接回复场景 → `CONTINUE`。

这些结构门不会因为 owner route 而跳过。`WAIT`、`NO_ACTION`、`REACT` 已经成立时，owner route 不会升级动作。

第二段 `reconcile_action()` 在 `CONTINUE` 且 chat 权限允许后，按以下顺序处理：

1. 只有 `owner_action_router`、`owner_action_ticket`、`owner_action_route` 三者同时存在才检查 owner 动作；缺一直接拒绝；
2. 类型必须分别为 exact `OwnerActionRouter`、`AcceptedTurnTicket`、opaque `OwnerActionRouteDecision`；
3. Planner 只调用 router 的 `inspect_route()`，不相信 decision 的可见字段，也不接受 proposal 单独作为 authority；
4. route 必须是当前 ticket、当前 binding 的 exact canonical `MATCHED` handle；copy、跨 router、跨 authority、跨 ticket、proposal replacement 全部由 E3A authority 拒绝；
5. policy 必须证明 configured owner、owner relationship、`agent_full`、非 degraded、允许 proposal capability 且调用预算至少为 1；
6. 满足后生成 `EXECUTE_ACTION`，精确携带 proposal capability、proposal operation 和 current ReplyTarget；
7. owner route 存在但 policy 不满足时保守生成 `REPLY`，绝不降成普通 `USE_TOOL`；
8. 没有 matched owner route 时，才继续 KnowledgeGap 分支。

KnowledgeGap 分支保持 capability-only `USE_TOOL`：需要真实 gap、policy 允许、预算足够、route 配置成立；operation 永远为空。旧的 owner broad/model suggestion → `USE_TOOL` 可达分支已删除。

二轮确认也在同一 structural gate、current ReplyTarget、configured-owner policy 与 `PlannedActionAuthority` 下规划：

- `CONFIRM` → current-binding `EXECUTE_ACTION`，携带原 request 的 capability/operation，但生成当前轮的新 action ID；
- `DENY` / `CANCEL` → current-binding `REPLY`，capability/operation 为空，不把拒绝或取消伪装成执行；
- 三种 plan 都绑定 exact controller-issued `PendingResolutionRoute`；copy、cross-controller、wrong binding/policy/target 不能生成 canonical continuation plan。

## 身份与模型边界

- Planner 不从正文、称呼、Persona 或历史猜主人；只消费上游 typed policy 与 E3A exact route authority；
- guest、群聊 owner policy、untrusted verification source、degraded owner、copy/mismatch route 都不能得到 `EXECUTE_ACTION`；
- 模型不能提交 owner、target、binding、tool、source、arguments、budget、operation 等字段；
- typed `ModelActionSuggestion(action_hint=EXECUTE_ACTION)` 也拒绝，不只拒绝 Mapping payload；
- 模型 broad capability 在没有 KnowledgeGap 时不能制造 `USE_TOOL`。

## 先红后绿与验证

本轮红灯依次覆盖：

- `ActionKind.EXECUTE_ACTION` 不存在；
- operation 未进入 action digest；
- 旧 owner broad/model hint 仍能走 `USE_TOOL`；
- Planner 直接接受可复制的公开 proposal；
- route provenance 接入前缺 exact router/ticket/opaque handle；
- bare-string ActionKind / operation 和同形 ReplyTarget 可绕过部分 `str Enum` 分支。

E3C0 最终验证：

- Planner + Router + Controller focused：61/61；
- accepted authority / behavior / planner / router / controller / adapters / runtime collector：136/136；
- owner contracts / output guard / executor：83/83；
- 完整 discovery：866/866；
- `compileall` 与 touched-file trailing-whitespace 检查通过。

## 未接生产与硬阻塞

本报告只证明离线 typed Planner 合同，不代表主人动作已上线。

- E3C0 已闭合首轮 route/ticket composite、no-op finalization、canonical plan integrity、确认轮 current plan 与 origin lineage；
- `ActionOutcome`、ContentIntent、Composer/final renderer、SemanticGuard 与 `main.py` 唯一热路径仍未接入；
- Controller 旧 `complete_execution()` 仍接收 raw status/effect/output，必须由 E2C sealed completion authority 替代后才可生产；
- terminal/tombstone TTL 清理与 production adapter live conformance 仍未闭合。

因此 E3C0 仍只是离线 authority/lineage 合同，不是生产上线证据。
