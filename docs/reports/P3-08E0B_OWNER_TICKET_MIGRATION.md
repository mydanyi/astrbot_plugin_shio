# P3-08E0B：Owner Action Consumer Ticket 迁移

日期：2026-08-18（Asia/Hong_Kong）

## 1. 结论

`OwnerActionController` 已从排他消费 raw `AdmissionProof` 原子迁移为只消费 `AcceptedTurnConsumer.OWNER_ACTION` 的 exact canonical child ticket。

```text
IngressAdmissionController
  → AcceptedTurnAuthority.dispatch() 唯一消费 raw AdmissionProof
  → exact OWNER_ACTION ticket
  → authority-owned exact AcceptedTurnContext
  → OwnerActionController
      ├─ seal PlannedOwnerActionRoute（不消费 ticket）
      ├─ issue OwnerActionRequest（最后一步单次 claim ticket）
      └─ confirmation route/resolve（最后一步单次 claim 当前轮 ticket）
```

Controller 不再接收 caller 提供的 `AdmissionResult` 或 `AcceptedTurnContext`，也不再持有 `IngressAdmissionController`。它从同一 long-lived `AcceptedTurnAuthority.context_for()` 取得 issuer-owned exact context，并在 claim 时把同一个 context identity 交还 authority。这样 OWNER_ACTION 与 AFFECT_STATE 不再按调用顺序争抢同一 raw proof。

本层没有接 `main.py`、没有执行任何真实工具、没有修改 adapter/Planner/Affect/authority、没有改 FNOS，也没有 Git 写操作。

## 2. 接口与原子边界

### 2.1 当前轮入口

以下入口现在都只接受 exact `AcceptedTurnTicket`：

- `seal_action_route(ticket, planned_action, proposal)`
- `issue_request(ticket, action_route, ...)`
- `match_pending(ticket, now=...)`
- `route_pending_resolution(pending, ticket, current_message=..., now=...)`
- `resolve_pending(resolution_route, ticket, now=...)`

每个入口都固定要求 consumer 为 `OWNER_ACTION`。wrong consumer、copy、跨 authority、跨 dispatch、corrupt binding 和 replay 由 `AcceptedTurnAuthority` 的 canonical registry 失败关闭。

### 2.2 Context 权威

Controller 只从 authority 取得 `AcceptedTurnContext`，并用其中的 exact envelope/principal/source 验证：

- sender kind 必须为 `HUMAN`，plugin source 必须为 `NONE`；
- 可信 principal 必须同时满足 configured owner verification、owner relationship 和结构化 sender 一致；
- 必须是 private scope、空 group ID；scope/sender 由 platform、bot、session、sender 重新计算；
- binding 的 scope/session/sender/content digest 必须与 context 一致；
- 二轮引用状态只从 context exact envelope 读取；caller 正文必须命中 context content digest。

昵称、自称、引用正文、伪造 `AdmissionResult` 或字段自洽的伪事件不再进入 controller 的 authority 表面。

### 2.3 Claim 后发布

首轮 request 在 claim 前完成：

1. exact ticket/context、PlannedAction route、Proposal、AdapterDraft/material、binding、idempotency 和容量校验；
2. canonical request 与私有 `_ActionRecord` blueprint 构造；
3. authority 单次 claim exact OWNER_ACTION ticket + same context；
4. 只做确定性的私有 ledger/idempotency/route 状态发布。

二轮 resolve 在 claim 前完成：

1. exact pending/route/ticket/context、sender/scope/session/revision/epoch/TTL 复核；
2. closed pure phrase 已在 route 阶段从当前正文确定；
3. consumed pending 及 deny/cancel receipt blueprint 构造；
4. authority 单次 claim current OWNER_ACTION ticket；
5. 发布 READY 或 TERMINAL 状态。

任一 claim 失败都不会新增 request、idempotency 或 terminal receipt。`UNCLEAR`、0/many pending、validation failure 都不消费 ticket；unused ticket 的 skip/finalize 属于未来 main dispatcher，不由 controller 伪造。

## 3. 回归矩阵

- raw `AdmissionProof`：源码静态断言不存在 `AdmissionResult`、`IngressAdmissionController`、`inspect_admission_proof`、`claim_admission_proof`；动态测试在 raw proof 方法被强制抛错时仍可正常 issue；
- consumer authority：wrong consumer、ticket copy、cross-authority、cross-dispatch、replay、伪造 AdmissionResult 全部零 request ledger 写；
- binding/lineage：route 必须与同一 exact ticket/context；PlannedAction action ID 继续原样进入 request/receipt；
- owner boundary：guest、群聊 owner、昵称/正文自称、引用、自称 owner role、错误 verification 全拒；
- draft failure/idempotency/capacity failure：ticket 保持可由 authority 单次 claim，证明 controller 没有抢占；
- confirmation：match 只做 0/1/many lookup；closed phrase、引用、否定、疑问、附加正文、A→B→A、跨 scope、跨 turn ticket、copy/cross-controller/replay 全覆盖；
- expiry：`sweep_expired()` 不借用新 turn ticket；该 ticket 后续仍可用于新 request；
- 原 C 的 opaque parameter、并发 lease、stale-before-start、timeout/partial/unknown、canonical receipt、redacted repr/trace 与 bounded fail-closed ledger 回归保持绿色。

## 4. 红灯与验证

先把 fixture 和公开调用面迁移到 `AcceptedTurnAuthority`：旧 controller 构造器在 21 个定向测试中稳定报 `ingress_admission_controller_required`，证明红灯确实命中 raw-proof 依赖。

实现后：

- controller focused：**22/22**；
- authority + AffectState + controller + adapters + owner contracts + planner + ingress：**121/121**；
- 完整 discovery：**792/792**；
- `compileall`、限定 diff check 与 raw-proof symbol scan：通过。

## 5. 尚未接生产与剩余硬门

1. `main.py` 还没有唯一 accepted-turn dispatcher、consumer ticket 路由和 unused-ticket skip/finalize；本报告不能证明主人 Action 已上线。
2. confirmation 当前轮虽然绑定 exact ticket/context，但仍需由生产唯一 `EXECUTE_ACTION` continuation 把 current PlannedAction/ContentIntent 与 origin request lineage 同时交给 renderer/guard。
3. live runtime candidate collector、executor-sealed completion、direct-send/Handoff/MCP/background 拒绝和安全输出扫描仍属于 P3-08E2；所有 adapter 应继续默认关闭，Shell 保持代码级禁用。
4. Controller 当前 ledger/route 是进程内硬上限、满时失败关闭；不会驱逐 pending/in-progress 或丢失幂等证据，但跨重启 journal、terminal tombstone TTL 与消费后 route 清理仍需在生产生命周期任务中闭合。
5. LivingMemory 当前 legacy scope、权限证明不足，AstrBot 同版源码哈希漂移；对应 live adapter 不能因本次 ticket 迁移而启用。

下一入口是 P3-08E 的 main dispatcher/EXECUTE_ACTION/receipt delivery 接线与 E2 live conformance/executor，不得恢复 raw AdmissionProof 子系统直连。
