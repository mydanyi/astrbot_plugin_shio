# P3-08E4B ActionOutcome / Controller 本地回收与中断恢复报告

## 1. 结论

E4B 当前是纯本地合同层的**终审修复候选**，不接 `main.py`、运行配置、FNOS、Git 或生产发送路径。首轮独立终审发现的公开呈现、record exact-type 与三条生命周期事务原子性问题已先红测、再根修并通过回归；仍须由不同审计者完成最终独立复核，本文不把“自审全绿”写成“已独立冻结”。

本层解决两个长期强引用根因：

1. `ActionOutcomeAuthority` 原先把已消费 outcome、exact request / receipt / denial / continuation 和 Composer request 永久留在最多 256 项的 active ledger；
2. `OwnerActionController` 原先把 terminal / aborted action 的 request、adapter draft/material、parameter record、route、ticket/context、lease、receipt、pending resolution 和 continuation 永久留在多个索引。

当前结果：

- Composer 构建前失败可以显式 `abandon_before_composer`；
- exact Composer claim 只能在显式 delivery-terminal ack 后回收，不能用 abandon 淘汰正在 Composer / Guard / Repair / Presentation 使用的 outcome；
- PREPARED request 可以显式 abort；
- terminal receipt / denial / continuation 只有在 exact outcome 已进入 reclaim 状态后才能释放 Controller lineage；
- interrupted read 固定收敛为 `FAILED / NO_SIDE_EFFECT`，interrupted mutation 固定收敛为 `EFFECT_UNKNOWN / UNKNOWN`，两者都 `attempted=True` 且永不自动重试；
- active outcome / Controller ledger 在连续 512 次签发、回收后不会 `ledger_full`；
- 回收后只保留闭集、无正文的 typed tombstone 和不可逆 source fingerprint，不保留 path、command、memory literal、tool output、真实身份或原始 digest；
- `ActionOutcomeIntent` 与 Controller tombstone 的公开 `repr()` / `trace_metadata()` 均只读取 closure-owned canonical projection；copy、合法异 Enum、`bool == int`、Truthy marker、字段删除、record impostor 均 typed fail-closed，恢复原字段后 canonical 路径可继续；
- `abort_prepared`、`recover_interrupted_execution`、`release_terminal_lineage` 的多索引提交均在 Controller 锁内使用可回滚事务；即使 dict hook 已先写/先删再抛 `BaseException`，所有索引与 record 字段也恢复到调用前状态，同一 canonical source 可重试；
- Action / Route / Receipt / Lease / Denial / PendingResolution / Continuation / Tombstone registry record 均在解引用前做 exact class、完整 slots 与 exact tuple snapshot 检查，mirror 只接受 exact 二元 tuple；
- C0/C1/C2 的 exact authority、single claim、same-plan、same-consumer、output hard-off 均未弱化。

“active ledger 有界”不等于“整个生命周期内存有界”：retired source、Controller tombstone 与 idempotency tombstone 仍随回收次数线性增长。这是 E4D 的明确部署硬门，而不是本层已闭合事实。

## 2. 修改边界

仅修改：

- `core/action_outcome.py`
- `core/owner_action_controller.py`
- `tests/test_action_outcome.py`
- `tests/test_owner_action_controller.py`
- 本报告

未修改：

- ContentIntent / Composer / Guard / Validator / Repair / Presentation；
- `main.py`、配置、runtime wiring；
- E3C1A executor / contracts；
- E4C durable lifecycle store；
- Git、GitHub、FNOS、线上插件。

## 3. ActionOutcome 生命周期

### 3.1 新增公开终态 API

```text
ActionOutcomeAuthority.abandon_before_composer(
    outcome,
    *,
    current_planned_action,
)

ActionOutcomeAuthority.acknowledge_delivery_terminal(
    outcome,
    current_planned_action,
    *,
    consumer,
)
```

`abandon_before_composer` 只接受：

- exact canonical outcome；
- exact current plan；
- `consumer_kind=NONE`；
- 尚未 generic consume、尚未 Composer claim 的 active record。

`acknowledge_delivery_terminal` 只接受：

- exact canonical outcome；
- exact current plan；
- exact、已由 C1 vault mint 并 claim 的 `ReplyComposerRequest`；
- 完整 Composer snapshot 再检查通过。

因此：

- build validation 失败后可 abandon；
- 一旦 Composer claim 成功，abandon 永久拒绝；
- Guard / Repair / Presentation 仍可反复 non-consuming inspect 同一个 exact pair；
- 只有 trusted E5 wiring 明确报告 delivery terminal 后，才调用 ack；本层没有把“创建了 Composer request”误当成“已经发送”。

### 3.2 active state 与 reclaim state 分离

closure vault 使用四类不公开弱/强状态：

- active `_OutcomeRecord` tuple；
- 每 authority 的 retired source HMAC fingerprint frozenset；
- exact outcome 弱键 reclaim proof。
- exact outcome 弱键 canonical presentation projection；projection 只保存闭集 kind / operation 与两个 exact bool，并弱持 authority。

回收时：

1. 先复核 Controller-scoped exact authority；
2. 复核 current plan、receipt / denial / continuation lineage；
3. 对 delivery ack 再复核 exact Composer request 与完整 snapshot；
4. 在 vault lock 内一次性从 active tuple 删除；
5. 写入不可逆 source fingerprint 和不强持 outcome 的 weak exact proof。

reclaim proof 不保存 source、request、receipt、lineage、plan 或 Composer request。重复、copy、cross-authority、cross-plan 和并发 loser 均失败关闭。source fingerprint 在 active record 删除后继续阻止同一 canonical source 二次签发。

`ActionOutcomeIntent` 自身不再直接读取可被 `object.__setattr__` 改写的字段做公开呈现。`trace_metadata()` / `repr()` 先在 closure registry 中确认 exact identity，再确认 active record 或 exact reclaim record、exact snapshot 与 sanitized projection 一致。合法异 Enum、外来同值 Enum、`True == 1`、Truthy 等价对象、copy 与字段删除都只得到 typed `action_outcome_corrupt`，不会显示 marker、伪 outcome 或裸 `AttributeError`；恢复原字段后 active inspect、abandon、reclaimed source inspect 均可继续。

`trace_metadata()` 现在分别报告：

- `outcome_count`：active count；
- `consumed_count`：仍处 active state 的 consumed count；
- `reclaimed_count`：retired source count；
- `ledger_bounded`：只判断 active ledger 是否超过注册 limit。

### 3.3 512 次连续回收

正式测试连续创建 512 个 exact denial outcome，每次执行：

```text
issue → abandon_before_composer → Controller release_terminal_lineage
```

最终：

- `outcome_count=0`；
- `consumed_count=0`；
- `reclaimed_count=512`；
- 未触发原 256 active cap。

## 4. Controller 生命周期

### 4.1 typed tombstone

新增 closed `OwnerActionLifecycleKind` 和普通构造关闭的 `OwnerActionLifecycleTombstone`。公开事实只有：

- kind；
- exact closed operation；
- terminal status；
- effect state；
- attempted；
- `retry_blocked=True`；
- hidden reclaimed time；
- hidden irreversible lifecycle fingerprint。

`repr()` / `trace_metadata()` 只显示 closure-owned canonical projection 中的闭集枚举、exact 布尔值和 `has_*`，不直接读取 public object 当前字段，也不显示正文、路径、command、memory literal、request/action/idempotency digest、sender/scope/session/message ID 或 fingerprint 本体。

Controller 对 tombstone 做 exact identity + exact-type/value immutable snapshot 检查；普通 constructor、copy、cross-controller、合法异 Enum、`bool→int`、str subclass、Truthy marker、字段删除与外来 `_LifecycleTombstoneRecord` 均不能成为 canonical tombstone。回滚产生但已脱离 Controller `_tombstones` 的 orphan presentation record 也只能 typed fail-closed，不能通过 `repr()` / `trace_metadata()` 暴露伪状态；恢复原 record/字段后 inspect 可继续。

### 4.2 PREPARED abort

```text
OwnerActionController.abort_prepared(request, *, aborted_at)
```

只允许 exact `PREPARED` request，且必须不存在 pending、lease、receipt、continuation、resolution route。提交前完整复核 request / route / ticket mirror / idempotency 索引；成功后：

- active ledger 删除 request record；
- route 和 route-by-ticket 删除；
- idempotency key 改为最小 tombstone record，继续拒绝重复动作；
- adapter draft/material/parameter 不再被 Controller 强持。

并发八次只有一个 tombstone winner；其余请求因 request 已非 canonical 而失败。

### 4.3 interrupted execution recovery

```text
OwnerActionController.recover_interrupted_execution(
    lease,
    *,
    recovered_at,
)
```

caller 不能传 status、effect、attempted、result 或 retry 标志。Controller 以完整 `OwnerActionOperation` 闭集代码映射：

| operation | recovery status | effect | attempted |
|---|---|---|---|
| `ARTIFACT_READ_EXACT` | `FAILED` | `NO_SIDE_EFFECT` | true |
| `ARTIFACT_GREP` | `FAILED` | `NO_SIDE_EFFECT` | true |
| `MEMORY_WRITE_LITERAL` | `EFFECT_UNKNOWN` | `UNKNOWN` | true |
| `SANDBOX_SHELL_ONCE` | `EFFECT_UNKNOWN` | `UNKNOWN` | true |

未来 enum 新增而映射未同步时 module import 和 runtime recheck 都失败关闭。

恢复只签 terminal receipt；不调用 adapter / executor，不创建第二 lease，不重试。receipt registry、ActionRecord 的 `lease_consumed/state/latest_receipt`，以及 continuation 的 `receipt/receipt_snapshot` 在同一回滚事务内提交。receipt mapping 即使先写成功再抛 `KeyboardInterrupt`，read 与 confirmed mutation 两条路径都恢复为原 `IN_PROGRESS`、原 confirmation-required receipt 与未绑定 continuation；恢复故障 mapping 后同一 lease 可再次 recovery，并只产生一个 terminal receipt。重复、copy lease、cross Controller、时间回退和并发 loser 均失败。随后再次 `claim_execution` 只返回 canonical terminal receipt。

### 4.4 terminal lineage release

```text
OwnerActionController.release_terminal_lineage(
    receipt | denial | continuation,
    *,
    outcome,
    outcome_authority,
    reclaimed_at,
)
```

提交门：

1. exact Controller-scoped ActionOutcomeAuthority；
2. exact receipt / denial / continuation；
3. action 确为 terminal，`CONFIRMATION_REQUIRED` 明确不算 action terminal；
4. exact outcome 已通过 abandon 或 delivery-terminal ack 进入 reclaim proof；
5. request / route / ticket / receipt / lease / pending-resolution / continuation 所有索引及 mirror 完整一致。

成功后删除：

- request ledger；
- adapter draft/material/parameter strong refs；
- initial route、ticket/context mirror；
- lease；
- 所有 receipt，包括早期 confirmation-required receipt；
- pending resolution route 与 ticket mirror；
- continuation lineage；
- denial route graph。

idempotency key 保留到 typed tombstone，确保 release 不是“允许重新执行”。

receipt、denial、continuation 三种 source 的释放均以完整图验证为 prepare 阶段，再用同一 Controller transaction 提交。正式 fault injection 覆盖：receipt index 先删后抛、denial 的最终 route mirror 先删后抛、continuation 在 receipts / resolution mirrors 已删后于 continuation index 先删后抛。三者都完整恢复 request、route、receipt、lease、resolution、continuation、idempotency 与 tombstone indexes，原 source 仍 canonical，随后可重试成功。

confirmation-required delivery ack 只回收当次 outcome，Controller pending request、canonical confirmation receipt 和后续 resolution 能力保持 active；正式测试锁定此区别。

### 4.5 canonical registry record exact guard

Controller 所有 canonical registry validator 现在先校验 record exact class 与完整 `__slots__`，再读取字段；所有保存的 integrity snapshot 必须是 exact tuple，route / resolution / continuation mirror 必须是 exact 二元 tuple。覆盖：

- `_ActionRecord`；
- `_RouteRecord`；
- `_ReceiptRecord`；
- `_LeaseRecord`；
- `_DenialRecord`；
- `_PendingResolutionRouteRecord`；
- `_ContinuationLineageRecord`；
- `_LifecycleTombstoneRecord`。

正式测试把每类 registry value 替换成可转真、可同值比较、可转发全部字段的 impostor，并分别把 snapshot 替换成 Truthy 同值对象或删除 snapshot slot。所有 inspector / lifecycle entrypoint 均抛 typed `ContractViolation`，不消费 lease、route、pending、source 或 idempotency record；恢复 exact record 后 request inspect、lease recovery、pending resolution、continuation inspect、abort 与 reclaimed idempotency 拒绝均按原语义继续。

## 5. 失败原子性和对抗矩阵

正式测试覆盖：

- outcome / request / receipt / lease / Composer request / tombstone copy；
- cross-authority、cross-controller、cross-plan；
- wrong exact consumer；
- abandon claimed outcome；
- ack unclaimed outcome；
- release before outcome reclaim；
- release confirmation-required request；
- duplicate source、duplicate ack、duplicate abort、duplicate recovery、duplicate release；
- 八线程 ack / abort / recovery / release 单赢家；
- invalid recovery time 不改变 IN_PROGRESS；
- failed release 不删除 receipt/request/outcome active state；
- dict subclass callback 在 `__setitem__` / `__delitem__` 已完成真实写删后抛 `KeyboardInterrupt`，abort / recovery / receipt-denial-continuation release 仍完整回滚；
- recovery rollback 同时覆盖无 continuation 的 read 与已确认 continuation mutation；
- registry whole-record Truthy impostor、snapshot Truthy 等价物、exact tuple subclass/非 tuple mirror 与字段删除均 typed 拒绝，恢复后原 canonical path 可重试；
- ActionOutcome / tombstone 公开 trace/repr 对 copy、合法异 Enum、同值外来 Enum、`bool==1`、str subclass、Truthy marker 与字段删除 fail-closed；
- continuation release 清除 origin request、resolution route、continuation 和所有 receipts；
- Composer consumer、AdapterDraft、ActionReceipt 在最后一个外部强引用删除后可 GC；
- tombstone repr/trace 不含敏感字段。

E4B 新增的三条 Controller lifecycle mutator 都先完成 exact graph validation，再在单一 Controller lock 内以全相关索引快照事务提交；`BaseException` 触发时用 unbound built-in `dict.clear/update` 绕过故障 subclass hook 回滚原 mapping object 与相关 record fields。ActionOutcome reclaim 继续只在 closure lock 内对不可替换 tuple / WeakKeyDictionary 状态提交。失败路径不保留半状态。

## 6. TDD 与验证结果

使用 Codex bundled Python，在仓库父目录运行。

### 6.1 baseline、首轮实现与终审正式红灯

首轮 E4B 实现曾以 focused 77/77 全绿进入独立终审。终审随后先固定正式红测，确认并复现：

1. tombstone `repr/trace` 可被 marker、合法异 Enum、`bool→int`、字段删除与非 canonical record 欺骗或触发裸异常；
2. `abort_prepared` 的 idempotency mapping 先写后抛 `KeyboardInterrupt` 会留下 tombstone/idempotency 半提交；
3. `recover_interrupted_execution` 的 receipt mapping 先写后抛会留下 TERMINAL / consumed lease 与缺失 canonical receipt；
4. `release_terminal_lineage` 的 receipt mapping 先删后抛会留下已回收 tombstone 但未完整删除的图；
5. `ActionOutcomeIntent` 的公开呈现接受 copy / 合法异 Enum 伪状态，字段删除产生裸 `AttributeError`；
6. Controller canonical registry 可接受转发同字段的 Truthy impostor record，snapshot 同值等价物依赖 Python `==`。

四条初始 blocker test 在根修前全部失败；随后新增的 ActionOutcome canonical presentation 与 8 类 Controller record 综合 exact-guard test 也分别先失败。所有红灯均在 E4B 四个代码/测试文件内固定后再修改实现，没有以“原 77 项全绿”覆盖终审反例。

### 6.2 E4B focused

根修并补充 confirmation-required、continuation mutation rollback、receipt/denial/continuation late-fault、canonical presentation、record/snapshot/mirror exact guard 后：

```text
python -m unittest \
  astrbot_plugin_shio.tests.test_action_outcome \
  astrbot_plugin_shio.tests.test_owner_action_controller -q

Ran 86 tests
OK
```

### 6.3 E3C1A

```text
Ran 136 tests
OK
```

模块：owner-action contracts / Controller / executor / output guard。

### 6.4 C1 Content / Composer

```text
Ran 73 tests
OK
```

模块：ActionOutcome / delivery / ContentIntent builder / ReplyComposer。

### 6.5 C2 Guard / Repair / Presentation

```text
Ran 98 tests
OK
```

模块：semantic guard / output validator v2 / repair controller / presentation handoff / delivery pipeline。

### 6.6 compile / whitespace / diff

```text
4 target Python files py_compile: exit 0
python -m compileall -q astrbot_plugin_shio: exit 0
git diff --check: exit 0
target files merge marker / trailing whitespace scan: no match
```

仓库当前把 E4B 五个目标文件列为 untracked；因此 `git diff --check` 只能证明 tracked diff 无 whitespace error，不能替代本文的 scope hash。最终报告另列五文件 SHA-256，并在所有验证后复算两次确认稳定。未执行 add / commit / checkout / reset 等 Git 写操作。

### 6.7 当前全仓状态

E4C 已冻结后，无排除执行完整 discover：

```text
python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py' -q

Ran 1039 tests
OK (skipped=6)
```

本结果包含共享树当前冻结的 E4A / E4C 与全部既有测试。它证明当前工作区没有已观测回归；E4B 仍等待另一位审计者按新增红测之外的独立思路复核后才可宣布最终冻结。

## 7. 尚未闭合：E4D durable ack 集成硬门

E4B 已让 **active** outcome / Controller ledger 有界，但没有虚假宣称整个进程生命周期已经有界：

- `authority_retired_sources` 会按已回收 source 线性增长；
- Controller `_tombstones` 会按 aborted / released action 线性增长；
- idempotency tombstone record 必须继续存在，否则同一动作可能被当成新动作重新执行。

E4C 当前使用独立 install secret 计算 durable request fingerprint；E4B 使用独立 Controller / vault secret。E4C `acknowledge_delivery` 返回的是公开 snapshot，不是 Controller 可直接信任的 exact durable-ack capability。因此 E4B **没有** 增加接受 caller bool、caller snapshot 或任意 fingerprint 的危险 prune 入口。

E4D 必须新增代码拥有的 exact integration：

```text
Controller canonical tombstone
  → exact request digest 仅在受信投影时交给 E4C
  → E4C exact handle terminal + delivery ack
  → code-owned durable-ack capability
  → Controller / ActionOutcome finalize_reclaim
  → 删除 in-memory tombstone / retired-source entry
```

只有 E4C durable terminal 与 delivery ack 均成功后才能 prune；active、IN_PROGRESS、pending confirmation、Composer/Guard/Presentation in-flight record 永远不得被 TTL 或容量压力淘汰。

在 E4D 和 E5 wiring 完成前：

- 不接 `main.py`；
- 不部署 FNOS；
- 不声称总 lifecycle memory bounded；
- 不把 E4B 当作生产发布门已通过。

## 8. 威胁边界

本层防止 public/private object field copy、clone、mutation、cross-runtime、cross-plan、replay、并发重复提交和 caller-authored status/effect/retry。ActionOutcome authority 状态继续留在 closure vault，不暴露可替换 mapping、record、limit 或 generic setter。

与 C0 一致，本层不声称抵抗同一 Python 进程中任意 bytecode/closure-cell/`ctypes`/module replacement；该威胁需要进程/容器隔离和受控 IPC。
