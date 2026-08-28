# P3-08C / E0B / E3B / E3C0：Canonical Owner Action Controller

日期：2026-08-18（Asia/Hong_Kong）

## 结论

`OwnerActionController` 已完成模块级 authority、隐私、幂等、确认和单次 lease 合同，并迁移到 E3A/E3B 的 exact route：

```text
exact OWNER_ACTION ticket + issuer-owned AcceptedTurnContext
  + opaque canonical OwnerActionRouteDecision
  + EXECUTE_ACTION PlannedAction
→ controller-sealed PlannedOwnerActionRoute
→ exact canonical AdapterDraft + private typed material
→ OwnerActionRequest / OwnerActionDenial
→ optional private PendingConfirmation
→ current-message PendingResolutionRoute
→ one-shot ExecutionLease
→ canonical ActionReceipt
```

模块不接收 raw AdmissionProof / AdmissionResult，不发现或执行真实工具，尚未接 `main.py` 或生产。

## Authority 边界

Controller 构造时持有同一个长期：

- `AcceptedTurnAuthority`；
- `OwnerActionRouter`；
- `PlannedActionAuthority`。

入口只接受 exact `AcceptedTurnConsumer.OWNER_ACTION` ticket。Controller 内部用 `context_for()` 取得 issuer-owned exact `AcceptedTurnContext`，并从该 context 验证：human、plugin source none、private scope、空 group ID、configured owner verification source、owner relationship，以及 envelope/principal/binding 的 sender、scope、session、content digest 全部一致。

wrong consumer、ticket copy、cross-dispatch、cross-authority、replay、伪事件、guest、群聊 owner、昵称/正文自称、引用冒充、错误 relationship/source 都在 ledger 发布前拒绝。源码不导入或调用 raw admission proof API。

## EXECUTE_ACTION lineage

`seal_action_route(ticket, planned_action, route_decision)`：

- 只接受 exact opaque E3A route decision；公开 proposal 单独传入没有 authority；
- 只接受 `StructuralOutcome.CONTINUE + ActionKind.EXECUTE_ACTION`；
- 只接受同一个长期 `PlannedActionAuthority.inspect_plan()` 验过的 exact canonical plan；公开重建、copy、跨 authority 或嵌套字段篡改均拒绝；
- PlannedAction binding、capability、exact `OwnerActionOperation` 必须与 router inspect 得到的 canonical proposal 一致；
- controller 不生成新的 action ID；`PlannedOwnerActionRoute.action_id` 和 `OwnerActionRequest.action_id` 都继承 origin `PlannedAction.action_id`；
- copy、cross-controller、cross-ticket、changed proposal/action/binding/operation 均拒绝；
- seal 只 inspect 并登记，不提前 claim route，因此 adapter 编译失败前 route 仍可走明确 denial。

Controller 的公开 route/confirmation/lease/status 等 shape 已增加 exact Enum 门；裸字符串 `"execute_action"`、`"confirm"`、capability/operation/status lookalike 均不能作为 typed authority。

## Adapter 与参数隐私

`issue_request()` 只接受 exact canonical P3-08D `AdapterDraft`。Controller 通过私有 friend opener 取得同一 `_AdapterMaterial` identity，并验证：

- draft 与 current binding 一致；
- capability/operation 与 sealed route 一致；
- descriptor 是 closed registry 中同一 exact object；
- source interface digest、call budget=1、private-owner-only 均成立。

调用方不能分别传 parameter Mapping、parameter digest、路径、命令、记忆正文或 runtime attestation。typed parameter record、runtime evidence、draft/material identity 只存在 controller 私有 `_ActionRecord`。request、pending、route、lease、denial、receipt 的 repr/trace 不泄露这些内容。

## Request、denial 与两权威 composite

成功分支会先完整预构造并验证 request/record，再调用 Router 公开的 `claim_route_and_ticket()`：

1. Router lock 内 exact inspect route/ticket/context；
2. 调用 Router 持有的长期 `AcceptedTurnAuthority.claim_ticket()`；
3. authority 成功后执行不会产生业务拒绝的 route terminal 标记；
4. Controller 只做已预构造对象的私有 ledger 发布。

authority claim 失败时 route 保持 active；成功返回时 route 与 OWNER_ACTION ticket 都 terminal。旧 public `claim_route()` 已删除，Controller 的 request/denial 分支都不能再按顺序分别 claim 两个 authority。

明确失败分支可用 `deny_action_route()` 生成 controller-canonical `OwnerActionDenial`：固定 `DENIED + NOT_STARTED`，绑定 exact origin action/route/binding，不伪造 adapter 或参数字段；`inspect_denial()` 只认本 controller 签发的 exact identity。

E3C0R 进一步要求所有 authority 同时满足 exact identity 与注册时完整 snapshot。route、request、denial、pending resolution、receipt、base/current pending、lease 和 continuation 在每次 inspector/claim/resolve/complete 前都会复核原始 shape；frozen 对象经 `object.__setattr__` 修改 deadline、digest、decision、status/effect、nested binding/output 后立即 fail closed。

Router 同时提供 `finalize_noop_route()`：NO_MATCH、AMBIGUOUS、IDENTITY_REJECTED、PRIVATE_REQUIRED、REFERENCE_REJECTED、普通 owner chat，以及 matched-but-policy-denied 都可在不制造 request、receipt 或输出的前提下终结 route + ticket。

## 二轮确认

- `start_confirmation()` 生成 canonical `CONFIRMATION_REQUIRED` receipt 与 exact pending，不执行；
- `match_pending(ticket, now)` 只按可信 sender/scope/session 的私有 ledger 匹配，0→NONE、1→exact handle、>1→AMBIGUOUS 且零选择；
- 模型、正文、pending ID 不能选择 pending；A→B→A、跨群/用户/session 都失败关闭；
- `route_pending_resolution()` 只解析与 current ticket content digest 一致、无引用的当前正文；shell 闭集是确认/拒绝/取消执行，memory 闭集是确认/拒绝/取消保存；否定、疑问、附加文本、歧义、引用、过长正文均 `UNCLEAR` 且不消费 ticket；
- exact `PendingResolutionRoute` 私有绑定 current ticket/context、pending/request、parameter digest 和 decision；copy/cross-controller/changed body/replay 均拒绝；
- Planner 对 exact route 生成当前轮 canonical plan：CONFIRM 是 current `EXECUTE_ACTION`；DENY/CANCEL 是 current `REPLY`，不会伪装成执行；
- `resolve_pending()` 必须同时消费 exact route、同一 current ticket 和同一 `PlannedActionAuthority` 的 exact current plan；copy、cross authority、wrong binding/target/action/operation 与 mutation 在状态发布前拒绝；
- Controller 签发 opaque `OwnerActionContinuationLineage`，私有绑定 current plan/binding、origin request/action、resolution route/decision 与 eventual canonical receipt identity；
- receipt 的 `action_id` 始终保留 origin request action ID，`binding` 是当前确认轮，`origin_binding` 是首轮；lineage inspector 可返回 exact current plan、origin request、route 与 eventual receipt，但 repr/trace 不含正文、参数或这些 ID；
- `sweep_expired(now)` 不借用新的主人消息 ticket，只在私有 ledger 终结过期 pending 为 `STALE/NOT_STARTED/attempt=0`。

Controller 测试的二轮流程使用合同原生允许二轮确认的 `MEMORY_WRITE_LITERAL`，并通过冻结的 canonical runtime collector + private owner scope evidence 构造 D-owned draft。测试夹具仅在 `try/finally` 内临时把 exact memory descriptor 的 confirmation policy 切为 `SECOND_TURN_REQUIRED`，draft 签发后立即恢复；这不代表生产 memory adapter 已启用。Shell 始终 hard-off，没有伪造或执行 Shell。

## 并发、幂等和执行

- idempotency key 重复时不重发 request；
- 同一 request 并发 claim 只有一个 exact `ExecutionLease`；
- stale-before-start 生成零执行 receipt；
- lease 只能完成一次；terminal claim 返回同一 canonical receipt；
- timeout 不自动重试，partial/unknown effect 保真；
- receipt 只能由 contracts 私有 issuer 经 controller 唯一调用点签发；copy/forge/cross-controller 拒绝；
- ledger 有硬上限，满时 fail closed，不驱逐仍可能重放的证据。

## 公开接口

- `OwnerActionController`
- `OwnerActionContinuationLineage` / `OwnerActionContinuationInspection`
- `PlannedOwnerActionRoute`
- `OwnerActionDenial`
- `OwnerActionRequestLineageInspection` / `OwnerActionDenialInspection` / `OwnerActionReceiptInspection`
- `OwnerActionLedgerState` / `OwnerActionInspection`
- `PendingDecision` / `PendingMatchStatus` / `PendingMatch`
- `PendingResolutionStatus` / `PendingResolutionMatch` / `PendingResolutionRoute`
- `ActionClaimStatus` / `ActionClaim`
- `ExecutionLease`

关键方法：`seal_action_route`、`issue_request`、`deny_action_route`、`inspect_denial`、`inspect_request`、`inspect_receipt`、`inspect_request_lineage`、`inspect_denial_lineage`、`inspect_receipt_lineage`、`start_confirmation`、`match_pending`、`route_pending_resolution`、`inspect_pending_resolution_route`、`resolve_pending`、`continuation_lineage_for`、`inspect_continuation_lineage`、`sweep_expired`、`claim_execution`、`complete_execution`。

## 验证（E3C0R）

- owner action contracts + Controller focused：56/56；
- accepted authority / behavior / planner / router / contracts / Controller / adapters / runtime collector / executor：181/181；
- 完整 discovery：889/889；
- `compileall` 与 touched-file trailing-whitespace 检查通过。

覆盖 exact/copy/cross authority、wrong consumer、guest/group/self-claim、route/action/binding/capability/operation mismatch、canonical D draft、参数不泄露、并发 claim、A→B→A、0/1/>1 pending、closed confirmation parser、expiry 不吞新 authority、timeout/partial/unknown、receipt forgery、bounded ledger 与 bare-string Enum。

## 生产前硬阻塞

1. E2C sealed completion 尚未替换 `complete_execution(lease, raw status/effect/output...)`；公开 `ActionOutput` 不是 authority，raw completion API 禁止接生产。
2. `ActionOutcome`、ContentIntent、renderer、SemanticGuard 与 main 唯一热路径尚未接线。
3. terminal/tombstone 尚无 TTL 清理策略。当前 hard bound 保安全但长期会 fail closed 形成可用性耗尽；不能淘汰 pending/in-progress，也不能让被淘汰旧对象重新获得 authority。
4. `claim_execution()` 仍由 caller 提供 raw `current_binding`；后续执行接线必须从 controller-private request/continuation lineage 派生授权上下文。
5. 线上 LivingMemory 仍是 legacy scope/member 权限证据，`MEMORY_WRITE_LITERAL` 生产必须保持不可执行，直到 private exact scope + admin wrapper live conformance 成立。

因此本报告不能作为“主人完整工具已上线”或“生产已修复”的证据。
