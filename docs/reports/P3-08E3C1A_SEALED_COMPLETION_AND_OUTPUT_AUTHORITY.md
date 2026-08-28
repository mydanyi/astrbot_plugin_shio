# P3-08E3C1A：Sealed Completion 与 ActionOutput Authority

> 状态：**本地实现、测试、静态检查全部通过，已冻结等待独立审计**
>
> 日期：2026-08-18
>
> 边界：未接 `main.py`、Composer、SemanticGuard、Repair、Presentation；未修改 adapter/collector/ActionOutcome 实现；未部署 FNOS；未执行任何 Git 写操作。

## 1. 本层关闭的问题

E2C 已经能把一次受限工具调用收束成 opaque blueprint，但 Controller 过去仍接受调用方直接传入 `status/effect_state/result_digest/completed_at/output/reason_codes`。这意味着“谁能描述完成结果”与“谁真实执行了动作”没有物理合一；同时 Executor 为确认 lease 正在执行而直接读取 Controller 的 `_lock/_ledger/_leases`，异常前调用也可能让 canonical lease 永久停在 `IN_PROGRESS`。

E3C1A 将完成权威收束为一条链：

```text
exact Controller attempt
  -> exact Executor + request + lease + draft + collector
  -> one-shot sealed OwnerExecutionBlueprint
  -> Controller-only exact opener
  -> canonical ActionReceipt
```

公开 `ActionOutput` 现在只是一种数据形状，不再是输出权威。非空 receipt output 必须由模块 issuer 针对同一个 exact request 签发，并通过完整快照与对象身份复验。由于当前还没有“同一打开文件句柄上的读取与 fstat”证明，生产正向输出仍保持 hard-off。

## 2. 先红后绿证据

实现前先建立三类失败探针，均按预期为红：

1. `OwnerActionController.complete_execution` 仍公开 raw completion 字段，而不是 `(lease, blueprint)`。
2. `ActionOutput` 没有 canonical authority 身份，公开 factory 的对象可以直接进入私有 receipt issuer。
3. Executor 源码直接访问 Controller `_lock/_ledger/_leases`，没有受支持的 exact-attempt inspector。

随后先写 sealed completion/output authority 测试，再修改实现。最终接口、攻击矩阵、Owner Action 相关回归和全仓回归全部转绿。

## 3. 分层实现

### 3.1 Controller 完成入口物理删除 raw authority

`OwnerActionController.complete_execution` 的唯一签名现在是：

```python
complete_execution(self, lease, blueprint)
```

旧式 `status/effect_state/result_digest/completed_at/output/reason_codes` 关键字不再存在，调用会由 Python 直接抛出 `TypeError`。Controller 在自身锁内先复验 E3C0R 的完整 request/lease 快照，再以 exact Controller、request、lease 打开 blueprint；只有成功打开后才签 receipt 并把 lease 原子地置为 `TERMINAL`。

### 3.2 最小 Controller friend inspector

新增 `_inspect_exact_execution_attempt(request, lease, adapter_draft, adapter_material)`。它：

- 复用 E3C0R 的完整 request/lease/record 快照复验；
- 只接受同一 exact request、lease、draft、material、parameter record；
- 只接受尚未消费且状态为 `IN_PROGRESS` 的 canonical attempt；
- 返回 `None`，不泄露 ledger、lease record、adapter authority 或任何可变内部对象。

Executor 不再出现 `controller._lock`、`controller._ledger` 或 `controller._leases` 访问。

### 3.3 One-shot sealed blueprint

blueprint 的私有 material 同时绑定：

- exact Executor 与 exact Controller；
- exact request 与完整嵌套 binding 快照；
- exact lease 与完整嵌套 binding 快照；
- exact draft/collector identity，以及 draft 完整状态；
- completion 的闭集 kind/status/effect、digest、时间、output authority 与 reason 快照。

公开构造、复制、深复制、字段修改、跨 request、跨 lease、跨 Controller 和重放均不能打开 blueprint。跨 lineage 的失败发生在 one-shot 标记之前，因此不会消费正确 blueprint；正确谱系仍可随后完成。Controller 锁将并发完成串行化，同一 lease 最多签出一个 terminal receipt。

### 3.4 canonical attempt 必须闭合

一旦 exact canonical attempt 被 Executor 登记，它只能出现两种结果：

1. 返回一个 canonical blueprint；
2. Controller 已经持有 terminal receipt。

以下调用前失败全部映射为 sealed `PRE_CALL_TERMINATED`，由 Controller 记录为 `DENIED / NOT_STARTED / attempt_count=0`：

- production executor hard-off；
- 参数 material 漂移或编译失败；
- runtime evidence 不合格；
- schema 不匹配；
- tool 类型/来源/后台任务/插件 hook 不合格；
- clock 非法或早于 lease；
- 调用前 deadline 已过；
- LivingMemory 写入尚未完成独立 live 审计。

真正进入调用后的 read/write 结果保持语义保真：read 失败是 `FAILED/NO_SIDE_EFFECT`，read 超时是 `TIMED_OUT/NO_SIDE_EFFECT`；mutation 的确定失败是 `FAILED/NOT_COMMITTED`，无法证明副作用时是 `EFFECT_UNKNOWN/UNKNOWN`，测试合同仍覆盖 `PARTIAL`。执行后 `STALE` 只保留给既有纯合同 fixture，生产 blueprint 明确拒绝它，避免把当前绑定过期伪装成动作事实。

Python 3.11+ 的 `asyncio.CancelledError` 直接继承 `BaseException`。本层单独捕获外部取消：read 取消返回 `FAILED/NO_SIDE_EFFECT` blueprint，mutation 取消返回 `EFFECT_UNKNOWN/UNKNOWN` blueprint；调用方仍须把该 blueprint 交给 Controller 终结 lease，不能留下 `IN_PROGRESS`。

### 3.5 ActionOutput authority

`ActionOutput` 现在具备模块级 issuer 与 exact identity registry：

- public constructor、`from_safe_text`、`from_artifact_ref` 只生成非 canonical 形状；
- canonical issuer 保存 exact request object、完整 request/binding 快照、exact output object 与完整 output 快照；
- copy、`dataclasses.replace`、`object.__setattr__` 篡改和同字段不同 request 均失去 authority；
- `_issue_action_receipt` 对非空 output 再次要求 exact request/output authority；
- canonical `SafeArtifactOutput` 仍不是 `ActionOutput`，在 completion 构造阶段即被 exact-type gate 拒绝，不能冒充最终输出。

当前生产调用 `_issue_action_output` 会得到 `action_output_live_authority_unavailable`。测试专用 issuer 只用于合同 fixture，未进入公开 `__all__` 或生产 `execute()` 路径。

### 3.6 锁序与异步边界

锁序固定为：

```text
Controller lock -> blueprint registry lock -> receipt issuer lock
```

Controller 持锁期间没有 `await`、callback 或 tool 调用。Executor 只在真正调用工具前完成所有 Controller/blueprint 复验，工具调用时不持上述锁。

## 4. 攻击矩阵

| 攻击/异常 | 预期结果 | 验证结果 |
|---|---|---|
| 旧 raw completion 调用 | `TypeError` | 通过 |
| fake/public blueprint | 拒绝，不终结 lease | 通过 |
| blueprint copy/deepcopy | 拒绝 | 通过 |
| blueprint 字段或嵌套 material 修改 | 拒绝，不消费正确 authority | 通过 |
| cross-request / cross-lease | 拒绝，不消费 | 通过 |
| cross-controller | 错误 Controller 拒绝，正确 Controller 随后可完成 | 通过 |
| blueprint replay | 第二次拒绝 | 通过 |
| 8 路并发完成 | 仅 1 个 terminal receipt | 通过 |
| 参数/schema/runtime/tool 调用前失败 | terminal `DENIED/NOT_STARTED/0` | 通过 |
| 工具 await 期间外部取消 | 返回可终结 blueprint，不遗留 `IN_PROGRESS` | 通过 |
| public ActionOutput | 非 canonical，receipt/blueprint 拒绝 | 通过 |
| ActionOutput copy/mutation/cross-request | authority 失效 | 通过 |
| canonical SafeArtifactOutput 冒充 | exact-type gate 拒绝 | 通过 |
| production 正向 output | hard-off | 通过 |

## 5. 验证结果

使用 Codex bundled Python，在仓库父目录运行。

### 5.1 本层 focused

```text
python -m unittest \
  astrbot_plugin_shio.tests.test_owner_action_contracts \
  astrbot_plugin_shio.tests.test_owner_action_controller \
  astrbot_plugin_shio.tests.test_owner_action_executor \
  astrbot_plugin_shio.tests.test_owner_action_output_guard -q

Ran 127 tests in 0.184s
OK
```

### 5.2 Owner Action 相关回归

```text
python -m unittest \
  astrbot_plugin_shio.tests.test_behavior_contracts \
  astrbot_plugin_shio.tests.test_action_planner \
  astrbot_plugin_shio.tests.test_owner_action_contracts \
  astrbot_plugin_shio.tests.test_owner_action_router \
  astrbot_plugin_shio.tests.test_owner_action_adapters \
  astrbot_plugin_shio.tests.test_owner_action_runtime_collector \
  astrbot_plugin_shio.tests.test_owner_action_output_guard \
  astrbot_plugin_shio.tests.test_owner_action_executor \
  astrbot_plugin_shio.tests.test_owner_action_controller \
  astrbot_plugin_shio.tests.test_action_outcome \
  astrbot_plugin_shio.tests.test_content_intent_grounding_type -q

Ran 237 tests in 0.269s
OK
```

`test_action_outcome.py` 只把 7 处 raw completion fixture 迁移到 sealed blueprint helper；15 个 ActionOutcome 语义、公开字段与断言未改变。集成层另有 1 处 ContentIntent 类型门 fixture 机械迁移到 test-only canonical output issuer，测试目的与断言未改变。

### 5.3 全仓回归与静态检查

```text
python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py' -q
Ran 899 tests in 1.010s
OK
```

另外：

- 指定 core/test 文件 `compileall -q` 通过；
- 静态扫描确认 Executor 无 Controller `_lock/_ledger/_leases` 访问；
- 静态扫描确认生产测试调用没有 raw `complete_execution` 字段；
- 指定变更文件 whitespace/diff check 通过。

## 6. 关键 API 位置

- `core/contracts/owner_action.py:679`：`ActionOutput`
- `core/contracts/owner_action.py:922`：private output issuer（生产 hard-off）
- `core/contracts/owner_action.py:987`：exact output opener
- `core/contracts/owner_action.py:1395`：receipt issuer 再验 output authority
- `core/owner_action_controller.py:1289`：exact-attempt friend inspector
- `core/owner_action_controller.py:2536`：sealed-only `complete_execution`
- `core/owner_action_executor.py:162`：opaque one-shot blueprint
- `core/owner_action_executor.py:410`：blueprint issuer
- `core/owner_action_executor.py:470`：Controller-only one-shot opener
- `core/owner_action_executor.py:743`：cancelled read/mutation 保真映射
- `core/owner_action_executor.py:1176`：canonical-attempt closure 与 production hard-off

## 7. 尚未解除的生产门

本层只完成执行完成权威，不代表 owner action 已可上线：

1. 尚无 live same-open-handle artifact proof，所有生产正向 output/success 继续 hard-off。
2. LivingMemory write 的真实 admin/private scope 与 mutation result contract 尚未完成独立审计。
3. Shell 仍无候选 adapter，代码级禁用。
4. canonical ActionOutcomeAuthority 尚未作为生产单例接入 ContentIntent/Composer/Guard/Repair/Presentation。
5. `main.py`、配置、FNOS 与线上 AstrBot 均未修改或重载。

因此下一层只能在独立审计本层 blocker=0 后，继续做 canonical ActionOutcome 的单例接线与交付语义闭合；不得绕开输出 hard-off，也不得把 ActionReceipt/ActionOutcome 混入 GroundingFact。
