# P3-08E3C0R：Owner Action Authority 不可变快照复核

日期：2026-08-18（Asia/Hong_Kong）

## 结论

E3C0 独立终审发现的五类 `object.__setattr__` 绕过已经按同一根因收口：公开 frozen dataclass 只是一种 shape，Controller/contract issuer 只有在“exact object identity + 注册时完整不可变 snapshot + 每次 inspector 重验”同时成立时才承认 authority。

本层仍不接 `main.py`、ContentIntent、Composer、SemanticGuard 或真实工具；未修改 Git/FNOS。

## 快照覆盖

| Authority | 注册时保存 | 每次使用前重验 |
|---|---|---|
| `PlannedOwnerActionRoute` | exact plan、action ID、route digest、capability、operation、完整 binding、exact router decision identity | canonical plan authority、route digest 重算、plan/action/route 一致性 |
| `OwnerActionRequest` | 所有公开字段、完整 nested binding、reason tuple、deadline、idempotency、adapter-derived shape | request digest 重算、exact origin route/plan、adapter shape、完整 snapshot |
| `OwnerActionDenial` | binding、action/denial digest、status/effect、time、reasons | 固定 `DENIED/NOT_STARTED`、denial digest 重算、exact origin route/plan |
| `PendingResolutionRoute` | route digest、decision、capability/operation、完整 binding、exact pending/request/ticket/context identities | resolve 前与消费后均使用同一 integrity checker；replay 状态不跳过 snapshot |
| `ActionReceipt` | contracts issuer 保存全字段、origin/current binding、status/effect、times、reasons、nested output 全字段；Controller 再保存 receipt snapshot | `is_canonical`、Controller inspector、continuation inspector 都重算并比较 |
| `PendingConfirmation` | base/current pending 的 request、binding、expiry、parameter/request digest、confirmation binding/proof/consumed time | match、route、resolve、sweep、execution 前重验；不能延长 TTL 或改绑 |
| `ExecutionLease` | request digest、binding、lease digest、capability/operation、issued/deadline | repeated claim 与 completion 前重验；mutation 不能签发 receipt |
| continuation | lineage 自身、exact current plan、origin request snapshot、resolution route snapshot、eventual receipt snapshot | 每次 inspection 逐层调用 canonical inspector，不受 resolution replay 状态影响 |

`ActionReceipt.is_canonical` 不再只检查 marker + weak identity registry。contract issuer 额外使用 weak-key snapshot registry，因此 status/effect mutation、nested `ActionOutput` 正文/digest/binding mutation、copy/forge 都会返回 false。

## Origin plan/route 公开链路

ActionOutcome 不允许读取 Controller 私有 ledger，也不能用 action ID 或字段相等自行证明 origin plan。E3C0R 增加三个 public, parameter-free lineage inspector：

- `inspect_request_lineage(request) -> OwnerActionRequestLineageInspection`
- `inspect_denial_lineage(denial) -> OwnerActionDenialInspection`
- `inspect_receipt_lineage(receipt) -> OwnerActionReceiptInspection`

它们在返回前复核 exact request/denial/receipt、完整 snapshot、origin route snapshot 与同一 `PlannedActionAuthority` 的 exact plan。即使同一 authority 发布两份字段和 action ID 完全相同但 identity 不同的 canonical plan，inspector 也只返回最初登记的 exact origin plan。

原有 `inspect_request`、`inspect_denial`、`inspect_receipt` 保持兼容；新 inspection 的 repr/trace 只包含 capability、operation 和布尔 lineage 标志，不包含 sender/scope/message/action/request digest、正文、路径、命令或参数。

## 红灯与回归

最初红测稳定复现：

1. route action ID / digest 被改后仍可 issue request；
2. pending resolution digest 被改后仍可 resolve，消费后 decision 被改仍可 inspect lineage；
3. request deadline 从 200 改为 10000 后，`now=500` 仍能取得 lease；
4. denial 可从 `DENIED/NOT_STARTED` 改成成功/已提交；
5. receipt status/effect 与 nested output mutation 后仍被视为 canonical；
6. origin request operation mutation没有被 continuation 检出；
7. pending expiry/parameter 与 lease binding/deadline mutation缺少统一门。

收口后的 focused 基线：

- owner action contracts + Controller：56/56；
- accepted authority / behavior / planner / router / contracts / Controller / adapters / runtime collector / executor：181/181；
- 完整 discovery（含已迁移到 public lineage inspector 的 ActionOutcome）：889/889；
- `compileall` 通过。

## 仍阻塞生产

1. E2C executor 仍通过临时 private friend tuple view 打开 lease，尚未在真实调用前消费 Controller 的公开 snapshot inspector；production executor 当前 hard blocked。E3C1A 必须以 sealed blueprint/completion 替换该临时接口。
2. `complete_execution()` 仍接受 raw status/effect/output；公开 `ActionOutput` 不是 authority。
3. ActionOutcome/Content/main 唯一热路径与终止可见回复尚未在本报告中接线。
4. terminal/tombstone TTL 清理尚未实现。
5. FNOS live adapter fingerprint、LivingMemory private exact scope 与 admin wrapper conformance 尚未满足，memory write 必须继续 fail closed。

因此本报告只证明模块级 authority snapshot 收口，不证明生产执行或部署完成。
