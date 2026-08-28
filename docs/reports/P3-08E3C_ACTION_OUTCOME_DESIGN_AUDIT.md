# P3-08E3C：ActionOutcome 与可见交付链设计审计

日期：2026-08-18（Asia/Hong_Kong）

## 结论

主人动作不能从 `EXECUTE_ACTION` 直接跳到 Persona 文本。生产接线前必须形成五个互不替代的代码权威：

1. planner-owned canonical `PlannedAction`；
2. Router route + OWNER_ACTION ticket 的复合终结；
3. 当前确认轮 plan 到 origin request/action 的 continuation lineage；
4. E2C code-owned single-attempt execution blueprint；
5. module-sealed `ActionOutcomeIntent`。

上述任一项缺失时，`main.py` 的 owner action 分支继续禁止启用。

## A. Route 与 ticket

- `OwnerActionRouter.claim_route_and_ticket()` 是 request/denial/no-op 的唯一 public composite 边界；Authority 拒绝时 route 必须保持 active，成功后本地状态翻转必须不可失败。
- matched、no-match、ambiguous、identity/private/reference 拒绝、policy-denied 与普通 owner chat 都必须终结 OWNER_ACTION ticket；不能让 unused ticket 填满 bounded ledger。
- Controller 必须在自身锁内完成全部校验和对象预构造，再调用 composite，随后只能执行不会产生业务异常的本地发布。
- `main.py` 不得分别调用 Router claim 与 `AcceptedTurnAuthority.claim_ticket()`，也不得持有或拼接跨模块私锁。

## B. PlannedAction 与确认双 lineage

- public frozen dataclass 不是 authority；`object.__setattr__`、copy、cross-authority、nested action/target/binding mutation 必须由 `PlannedActionAuthority` 拒绝。
- initial owner action 的 plan 必须是当前 binding 的 canonical `EXECUTE_ACTION`，且 operation/capability 进入 action digest。
- confirmation：CONFIRM 才是当前 binding 的 canonical `EXECUTE_ACTION`；DENY/CANCEL 是当前 binding 的 canonical `REPLY`，不能把拒绝或取消冒充一次执行。
- `OwnerActionContinuationLineage` 必须同时绑定 exact current plan/action ID、origin request/action ID、exact resolution route、decision、operation 与 capability。
- lease、receipt 和后续 ActionOutcome 只能从 Controller 私有 record 取得该 lineage；调用方不得补裸 action ID/current binding。

## C. Controller 与 E2C blueprint

现有 `complete_execution(lease, status, effect_state, result_digest, output, ...)` 是生产阻塞。最终接口必须只消费 exact canonical `OwnerExecutionBlueprint`：

- pre-call terminal：DENIED + NOT_STARTED + attempt_count=0；
- executed：闭集 status/effect + attempt_count=1；
- 调用方不能补 status/effect/output 或把异常遗留成永久 IN_PROGRESS。

锁序固定：

```text
Controller -> Router -> AcceptedTurnAuthority
Controller -> E2C blueprint registry
```

不得反向取锁，不得在锁内 await、调用插件或执行任意 callback。

## D. ActionOutput 与 ActionOutcomeIntent

- public `ActionOutput.from_safe_text()` / `from_artifact_ref()` 及 public `safe_text` 不是来源证明；E2C 必须提供 exact safe-output issuance，copy、mutation、cross-request 一律拒绝。
- `ActionOutcomeIntent` 只能由 canonical receipt、denial 或可见 no-op 结果投影，且必须 module-sealed、exact-object、不可重放。
- public 层只暴露闭集 operation/outcome/attempted/has_output；receipt、denial、lineage 与 safe output 保持私有。
- `ActionReceipt`/`ActionOutcomeIntent` 永不放入 `ContentIntent.grounding_facts`；Grounding exact-type gate保持不变。
- 建议的闭集可见语义：confirmation_required、denied/cancelled/stale_not_started、succeeded_read/succeeded_committed、failed_no_effect/failed_partial、timed_out、effect_unknown。

## E. Persona、Guard、Repair 与 Presentation

- `ReplyComposerRequest` 接 exact `ActionOutcomeIntent | None`，并与 `EvidenceOutcome` 互斥；Persona 只生成自然的人格化状态包装句。
- 安全文件正文由 deterministic Presentation 段附加，不让模型重写、总结或在 repair 中看到原文。
- `SemanticGuardContract` 绑定 current plan、outcome、binding 与 lineage；任何替换是 blocking、零 repair。
- Guard 必须阻止假成功、假失败，以及把 PARTIAL/UNKNOWN/TIMED_OUT 说成确定成功或确定未执行；自然同义表达不应误杀。
- Repair 复用同一 outcome identity，零工具、零重新执行。
- `PresentationHandoff` 必须支持 typed persona/output segments；owner-only target 再验证。
- E3C0A 已单独闭合最终展示 exact-byte seal：FINAL_SEND、issued seal、Presentation 和实际聚合文本摘要必须一致。

## F. 发送与生命周期

- ActionReceipt 证明动作状态；SendReceipt/PresentationReceipt 证明可见消息交付，二者不可混用。
- 动作成功后即使 render/send 失败，也不得重跑；同一 idempotency 只能返回原 canonical receipt。
- 多气泡部分发送是 delivery partial，不是 action partial。
- generation 过期只停止旧回复发送，不能删除 terminal action receipt。
- confirmation prompt 发送失败不自动确认/取消；pending 等待 TTL。
- owner output 不写入 LivingMemory，不转为 Grounding，不后台自动补发。

生命周期另由 E4 闭合：active material 与最小 durable tombstone 分离；terminal send ack 后清参数、路径、命令、输出、route 和 admission 强引用；mutating crash-in-progress 启动后标 UNKNOWN，绝不自动重试。

## 红灯矩阵

- composite 注入失败时 route/ticket 均保持 active；request/denial/no-op 并发只有一个成功；
- plan/action/binding/target/action ID mutation、copy、cross-authority 全拒；
- confirmation 缺 current plan、错 decision/operation/origin、cross-turn/replay 全拒且不消费 pending；
- fake/copy/cross-request/replayed blueprint 与 public ActionOutput 全拒；raw completion kwargs 从签名物理消失；
- receipt/outcome 伪装 Grounding 全拒；
- safe output append/truncate/reorder、跨气泡替换、post-presentation mutation 在 FINAL_SEND 全拒；
- send failure/partial send 不触发动作重试。

本报告是只读架构审计和实施合同，不宣称生产接线完成；未修改 FNOS，未执行 Git/GitHub 写操作。
