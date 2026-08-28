# P3-08E3C0：Route/Ticket Composite 与确认轮 Current-Plan Lineage

日期：2026-08-18（Asia/Hong_Kong）

## 结论

本层只闭合 owner action 的 authority 提交边界与二轮确认 lineage，不接 `main.py`、ContentIntent、Composer、SemanticGuard、执行器或真实工具。

已完成：

1. `OwnerActionRouter.claim_route_and_ticket()` 是 route + OWNER_ACTION ticket 的唯一公开 composite claim；
2. `finalize_noop_route()` 让无动作、拒绝、歧义、policy-denied 与普通聊天都能零输出终结 authority；
3. `PlannedActionAuthority` 把公开 `PlannedAction` 收紧为 exact、bounded、可复核的 Planner 产物；
4. 二轮 `PendingResolutionRoute` 进入 current structural gate/policy/ReplyTarget，生成当前轮新 plan/action ID；
5. Controller 签发 exact `OwnerActionContinuationLineage`，同时保留 current plan 与 origin request/action/receipt 两条身份链。

## Authority composite

```text
Router lock
  → exact canonical route/ticket/context inspect
  → AcceptedTurnAuthority.claim_ticket(exact OWNER_ACTION ticket)
  → infallible local route.claimed = true
  → return with both authorities terminal
```

authority claim 抛错时，route 不会标记 claimed。成功返回后，Controller 的 request/denial 只剩预构造记录的私有 dict 发布，不再调用第二个独立 claim。旧 public `claim_route()` 已移除。

`finalize_noop_route()` 不创建 request、denial、receipt、Content 或假结果；它只调用同一 composite。测试覆盖：

- `MATCHED` 但下游 policy 拒绝；
- `NO_MATCH` 普通 owner chat；
- `AMBIGUOUS`；
- `IDENTITY_REJECTED`；
- `PRIVATE_REQUIRED`；
- `REFERENCE_REJECTED`。

## Canonical PlannedAction

`PlannedActionAuthority` 保存 exact plan、nested ActionDecision、binding、ReplyTarget identity 和完整 snapshot，并重新计算 `_action_digest`。以下对象都不是 authority：

- 公开构造的同形 plan；
- copy/deepcopy；
- 另一 authority 的 plan；
- 通过 `object.__setattr__` 修改 action ID、ActionDecision、operation、binding 或 target 的原对象。

Planner 只在 exact owner route / confirmation route 通过 structural gate、current ReplyTarget 与 configured-owner capability policy 后登记 canonical plan。registry 有硬上限；淘汰后的旧 exact object fail closed。

## 二轮 plan 与 continuation

```text
exact PendingResolutionRoute + current typed gate/policy/target
  ├─ CONFIRM → current EXECUTE_ACTION(capability + operation)
  └─ DENY/CANCEL → current REPLY(no capability/operation)
        ↓
Controller.resolve_pending(exact current canonical plan)
        ↓
OwnerActionContinuationLineage
  = exact current plan/binding/action_id
  + exact origin request/origin action_id
  + exact resolution route/decision
  + eventual canonical receipt identity
```

current plan action ID 由当前 binding 计算，必须不同于 origin request action ID。receipt 仍以 origin action ID 作为 `action_id`，以当前确认轮作为 `binding`，并保留首轮 `origin_binding`。因此 renderer/outcome 后续可明确区分“这轮在回应哪条确认消息”和“实际授权的是哪个原始动作”。

lineage 是 controller-issued opaque exact handle。copy、cross-controller、字段 mutation 与 resolution replay 拒绝。repr/trace 不含正文、参数、message/sender、request/action digest 或 outcome 文本。

## E3C0R authority snapshot 复核

独立终审证明“frozen dataclass + identity registry”本身不足以抵抗 `object.__setattr__`。E3C0R 已给 route、request、denial、pending resolution、receipt 统一增加注册时完整 snapshot，并让 continuation inspector 重新检查 origin request、resolution route 与 eventual receipt。系统性扩展还覆盖 base/current `PendingConfirmation` 和 `ExecutionLease`。

同时新增 request/denial/receipt 到 exact origin canonical plan/route 的 public lineage inspector，供 ActionOutcome 使用；下游不能读取私有 ledger，也不能只凭 action ID 或字段相等证明 plan lineage。详细证据见 `P3-08E3C0R_AUTHORITY_SNAPSHOT_REVIEW.md`。

## Ledger 生命周期

- Router：hard `max_routes`；只淘汰已 terminal route，active route 满时 fail closed；no-op composite 避免普通 accepted turn 长期占 active slot。
- PlannedActionAuthority：hard `max_plans`；按最旧记录 fail-closed 淘汰，旧 exact plan 不会恢复 canonical。
- Controller request / resolution / continuation：数量受 `max_ledger_entries` 与一 request 一 continuation 关系约束；pending/in-progress 不淘汰。
- terminal/tombstone 尚无 TTL sweep，这是可用性而非 authority 绕过问题，仍为后续硬阻塞。

## 先红后绿

红灯覆盖：

- Router 缺 composite/no-op，authority 失败会留下不明 route 状态；
- 旧 standalone `claim_route()` 可绕开 ticket terminalization；
- Controller request/denial 顺序 claim 两个 authority；
- public/frozen PlannedAction 可被 copy、跨 authority或 `object.__setattr__` 篡改；
- confirmation 没有 current plan，receipt 只有 origin lineage；
- DENY/CANCEL 会被误当执行计划；
- lineage copy/cross-controller/mutation/replay 与 eventual receipt identity 未验证。

E3C0R 收口后的最终验证：

- owner action contracts + Controller focused：56/56；
- accepted authority / behavior / planner / router / contracts / Controller / adapters / runtime collector / executor：181/181；
- 完整 discovery：889/889；
- `compileall` 通过；touched-file trailing-whitespace scan 无命中。

## 仍阻塞生产

本报告不是上线或部署证据。以下仍未闭合：

1. Controller `complete_execution()` 仍接受 raw status/effect/output；E2C sealed completion/blueprint 必须成为唯一完成 authority；
2. `ActionOutcome` 与 ContentIntent/Composer/final renderer/SemanticGuard 尚未绑定 continuation lineage；
3. `main.py` 没有唯一热路径接线，也没有为每个 accepted owner turn 调用 route/no-op composite；
4. Controller terminal/tombstone TTL 清理尚未实现；
5. FNOS live adapter fingerprint、LivingMemory private exact scope 与 admin wrapper conformance 尚未满足，memory write 必须继续 fail closed。

本层未修改 Git/FNOS、未执行真实工具、未声明生产可用。
