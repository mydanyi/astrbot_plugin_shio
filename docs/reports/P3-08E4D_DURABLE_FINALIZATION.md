# P3-08E4D Durable Finalization

## 1. 结论

P3-08E4D 已在本地闭合以下生命周期：

- 只有绑定 exact canonical `PresentationHandoff`、逐 segment 全部真实发送成功的 `OwnerActionSendTerminalEvidence`，才能把已展示的终态动作写成 durable delivery acknowledgement。
- durable acknowledgement 只发布两张 fixed-consumer one-shot ticket：Controller 清敏感 lineage，ActionOutcome 清 reclaim/source fence；两张票全部消费后再清最小 Controller tombstone，并释放 lifecycle prune hold。
- `CONFIRMATION_REQUIRED` 只签单独的 pending-outcome retire ticket；它回收当前确认提示的 Outcome，但保持 origin request、pending 与 confirmation receipt。
- never-started PREPARED request 必须先写 durable `CANCELLED/NOT_STARTED/attempted=false`，再以 exact prepared-abort ticket 清 Controller；durable ACK 后清 tombstone/idempotency fence。
- terminal、prepared-abort 与 pending-delivery 三条路径均拒绝 public receipt、copy/deepcopy、cross-ledger、cross-controller、cross-authority、wrong consumer 与 replay。
- lifecycle public/implicit prune 同时检查 E4D hold；未完成 ticket 的 durable record 不可被 TTL 或容量回收。

本层没有接入 `main.py`，没有启用任何 production owner adapter，没有访问 FNOS，也没有进行 Git 写操作。

## 2. 原始缺口

E4A/E4B/E4C 各自闭合了 ticket finalization、内存 reclaim 与 durable journal，但三者之间此前没有 code-owned 原子桥：

1. C2 FINAL seal 在 `event.send` 之前消费，不能证明消息实际发送成功。
2. public `SentReplyRecord` 不是 canonical authority，不能作为 delivery ACK 依据。
3. Controller terminal release 之前没有 durable ticket gate，可能先清内存、后写 journal。
4. ActionOutcome reclaim 与 Controller tombstone 没有双消费者完成协议，长期运行会线性保留 source/tombstone/idempotency。
5. PREPARED abort 可直接清 Controller，没有先落 durable no-retry fence。
6. confirmation-required 的提示回复与最终动作终态不同；若复用双票会错误删除跨轮 pending request。

## 3. 实现

### 3.1 actual-send authority

`core/send_receipt.py` 新增：

- `OwnerActionSendTerminalEvidence`；
- `InternalSendReceiptLedger.begin_presentation_reply()`；
- `issue_owner_action_send_terminal_evidence()`；
- module-owned exact presentation/send/evidence vault。

证据绑定 exact presentation、composer request、outcome authority、target 与 `final_segments` 边界；只有每个 exact segment 都是 `SUCCEEDED` 才可签发。普通 public receipt、失败/缺失/重排 segment、另一 ledger 或变异 presentation 均失败关闭。presentation-bound reply 不再作为容量驱逐兜底对象。

### 3.2 durable two-ticket finalization

`core/owner_action_durable_finalize.py` 新增 store/controller/outcome-authority scoped authority：

- `acknowledge_delivered()`：actual-send evidence + exact terminal source + exact lifecycle terminal facts；
- `acknowledge_abandoned()`：仅限从未进入 Composer 的 code-owned abandoned outcome；
- `CONTROLLER_RELEASE` 与 `OUTCOME_RETIRE` fixed tickets；
- `acknowledge_pending_delivered()`：仅签 `PENDING_OUTCOME_RETIRE`；
- `prepare_abort()` / `complete_prepared_abort()`：durable prepared-abort protocol；
- lifecycle hold、ticket/dispatch exact registry、single-consume 与 bounded dispatch ledger。

API 不接收 `LifecycleSnapshot`、raw status/effect、bool success、Mapping 或 caller-provided digest 来替代 exact objects。

### 3.3 Controller 与 Outcome reclaim

`OwnerActionController.release_terminal_lineage()` 现在强制 exact durable Controller ticket。清 receipt/request/route/lease/continuation 后只留下最小 tombstone；ActionOutcome exact ticket消费后删除 reclaim/source fence，最后 `purge_durable_lifecycle_tombstone()` 删除 tombstone 与 idempotency index。

`abort_prepared()` 同样强制 durable prepared-abort ticket；`purge_prepared_abort_tombstone()` 只在 durable ACK 后执行。

所有 Controller 多 mapping mutation 继续由 `_ControllerMutationTransaction` 包裹；ticket claim/removal 是事务最后一步，`Exception`、`KeyboardInterrupt`、`SystemExit` 的先写/先删后抛回归由 E4B/E4C 既有矩阵覆盖。

## 4. 关键 API

- `core/send_receipt.py:185` `OwnerActionSendTerminalEvidence`
- `core/send_receipt.py:611` `begin_presentation_reply`
- `core/send_receipt.py:795` `issue_owner_action_send_terminal_evidence`
- `core/owner_action_durable_finalize.py:107` `OwnerActionDurableFinalizeAuthority`
- `core/owner_action_durable_finalize.py:193` `prepare_abort`
- `core/owner_action_durable_finalize.py:215` `acknowledge_pending_delivered`
- `core/owner_action_controller.py:2530` `purge_prepared_abort_tombstone`
- `core/owner_action_controller.py:3562` ticket-gated `release_terminal_lineage`
- `core/owner_action_controller.py:3924` `purge_durable_lifecycle_tombstone`
- `core/action_outcome.py:2448` `finalize_reclaimed_outcome`
- `core/owner_action_lifecycle.py:1656` hold-aware `_prunable`

## 5. 测试证据

- E4D focused：`6/6`。
- send/lifecycle/E4D/Outcome/Controller related：`139/139`，Windows platform skips `7`。
- 终态 outcome churn：`512` 轮后 Outcome active/reclaim、Controller ledger/tombstone、durable dispatch 均为 `0`，journal TTL prune 后 record 为 `0`。
- prepared-abort churn：`512` 轮后 Controller ledger/route/tombstone/idempotency、durable dispatch 均为 `0`，journal TTL prune 后 record 为 `0`。
- Windows full discover：`1048/1048`，skipped `7`。
- WSL full discover：`1048/1048`，无 skip。
- `compileall`：通过。
- repository `git diff --check`：通过。

## 6. 冻结哈希

- `core/send_receipt.py` `2B55286A05E7CA075975F91E334C8D8830436F92A55F01AE118F05D5038C1498`
- `core/owner_action_lifecycle.py` `EF67B0800B20BFE76DAC6317796140C14E8D9E5267E24B4A8CD0C3A593A86709`
- `core/owner_action_durable_finalize.py` `C014E2661227A81B29AD0CEEDF74BDEAD4F76816DB42FCDD05D88FCF2E09EA1B`
- `core/action_outcome.py` `5A2F6B943E69934B5EB14F5EC34F40D4C9CA46C559F2EF0325AEB7BA14A3DCE8`
- `core/owner_action_controller.py` `5CAC826AB95B5BAA194E4E1494C786CF125CFF9A4060867F7EE49C19D503C34E`
- `tests/test_owner_action_durable_finalize.py` `1BC699E30E1E3E479E7B88F3220778BC3775A3CB5379BDBA3173B4E31763C8CC`
- `tests/harness/owner_action_durable.py` `E0D75B94CFC7AB5E6E375CF948E8E25E81D9D19CF743A6A2B30D6BE3F80754A1`
- `tests/test_action_outcome.py` `09EFE68AF6AB2F16FFE123554B928453B023BD010962AA90C69F911F100C9FBC`
- `tests/test_owner_action_controller.py` `ECFB23E58C10A605F8BE7A037D95233E021768333706BE47CE8B42FE7CAAA79E`

## 7. 剩余硬门

下一唯一入口是 P3-08E5：

- plugin instance 初始化时冻结 all-off owner-action config；
- 建立 AcceptedTurn/Router/Planner/Controller/Lifecycle/Outcome/Delivery 的长生命周期对象图；
- 每个 accepted turn 的 OWNER_ACTION/AFFECT 两张票都必须 claim 或 typed finalize；
- `EXECUTE_ACTION` 与 AnySearch/Grounding 分支互斥；
- actual send hook 成功后才调用 E4D delivery ACK；
- shutdown/reload 进行 durable recovery，mutation IN_PROGRESS 映射 UNKNOWN 且绝不自动重试。

production collector allowlist 仍为空，live output authority 尚未通过；四个 adapter 继续全关闭，Shell 继续代码级 hard-disabled。
