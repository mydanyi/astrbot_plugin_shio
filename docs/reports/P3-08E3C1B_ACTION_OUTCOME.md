# P3-08E3C1B：Sealed ActionOutcome 纯语义层

日期：2026-08-18（Asia/Hong_Kong）

状态：**本地纯模块完成；未接生产**

## 1. 本层边界

本层只新增：

- `core/action_outcome.py`
- `tests/test_action_outcome.py`
- 本报告

没有修改 `main.py`、Controller、Executor、合同导出、配置或线上环境；没有执行 Git/GitHub 写操作。

目标是把 Controller 已签发的动作结果转换为一个最小、不可伪造、不可串计划的语义意图，供后续 Content/Composer/Guard 层消费。它不是工具结果，也永远不能转换成 `GroundingFact`。

## 2. 实现结果

### 2.1 最小公开对象

新增 module-sealed `ActionOutcomeIntent`，公开数据字段严格只有：

- `kind`
- `attempted`
- `has_output`

`repr()` 与 `trace_metadata()` 也只呈现这三类信息，不包含正文、路径、消息/用户/动作 ID、digest、参数或 reason。公开构造被关闭；伪造对象、copy、跨 authority 和发布后 `object.__setattr__` 修改均不能通过 canonical inspection。

### 2.2 闭集语义

闭集覆盖：

- `confirmation_required`
- `denied`
- `cancelled`
- `stale_not_started`
- `succeeded_read`
- `succeeded_committed`
- `failed_no_effect`
- `failed_partial`
- `timed_out`
- `effect_unknown`

并额外保留执行后 stale 的不确定状态，不把它们错误压成“成功”或“什么也没发生”：

- `stale_no_effect`
- `stale_not_committed`
- `stale_committed`
- `stale_partial`
- `stale_effect_unknown`

### 2.3 唯一可信来源

`ActionOutcomeAuthority` 只消费 Controller 的公开 canonical lineage inspector：

- direct receipt：`inspect_request_lineage()` + `inspect_receipt_lineage()`
- pre-request denial：`inspect_denial_lineage()`
- second-turn result：`inspect_continuation_lineage()`，并再次以 request/receipt lineage inspector 闭合 origin

同时以 `PlannedActionAuthority.inspect_plan()` 证明 current plan 是同一个 exact canonical object。没有读取 `_ledger`、`_routes`、`_receipts`、`_denials` 或其他 Controller 私有字段。

绑定内容包括：

- exact current canonical `PlannedAction`
- exact origin request，或 denial 的 exact origin action/route
- exact capability 与 operation
- exact canonical receipt/denial/continuation identity

CONFIRM continuation 必须是当前 canonical `EXECUTE_ACTION` plan 加 origin request 的 canonical terminal receipt；DENY/CANCEL continuation 的当前 plan 必须是无 capability/operation 的 canonical `REPLY`。

### 2.4 重放与输出边界

- 同一个 receipt、denial 或 continuation lineage 只能签发一个 outcome；并发签发只有一个赢家。
- outcome 有独立一次性 `consume_outcome()`；二次消费 fail-closed。
- `ActionReceipt.output is not None` 当前一律 hard reject，错误码为 `action_outcome_output_authority_unavailable`。
- 本层不读取、复制、保存或显示 `ActionOutput` 正文/路径；安全输出 same-handle authority 尚未接入。
- 本层不导入或生成 `GroundingFact`，动作结果不会伪装成联网/记忆事实。

## 3. 红灯证据

### 3.1 模块不存在

先新增测试、尚未实现模块时：

```text
ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.action_outcome'
Ran 1 test
FAILED (errors=1)
```

### 3.2 同 digest 不同 exact plan 替换

初稿只比较公开 `action_id`/binding；随后新增对抗测试，在同一 `PlannedActionAuthority` 内生成两份：

- 字段相同
- `action_id` 相同
- 都是 canonical
- Python object identity 不同

替换 origin plan 的红测真实失败：

```text
FAIL: test_same_digest_different_canonical_plan_cannot_substitute_origin_plan
AssertionError: ContractViolation not raised
Ran 1 test
FAILED (failures=1)
```

这证明字段/digest 比对不能冒充 authority。随后改用 Controller 新增并冻结的 request/denial/receipt lineage inspector，receipt 与 denial 两条路径都按 exact origin plan identity 闭锁，红测转绿。

## 4. 绿色验证

使用 Codex bundled Python，在仓库父目录运行：

### 4.1 本层定向

```text
python -m unittest astrbot_plugin_shio.tests.test_action_outcome
Ran 15 tests in 0.033s
OK
```

覆盖：闭集映射、禁止公开构造、三类 canonical 来源、确认/拒绝/取消、partial/unknown/timeout/stale、输出 hard reject、copy、跨 Controller、跨 plan、同 digest 不同 exact plan、对象修改、串行/并发 replay、一次性消费、公开面脱敏与非 Grounding。

### 4.2 Owner Action 相关回归

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
  astrbot_plugin_shio.tests.test_content_intent_grounding_type

Ran 228 tests in 0.243s
OK
```

### 4.3 全仓回归

```text
python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py'
Ran 890 tests in 1.008s
OK
```

另外：

- `py_compile core/action_outcome.py tests/test_action_outcome.py` 通过。
- 三个新增未跟踪文件分别以 `git diff --no-index --check NUL <file>` 检查，均为 `whitespace=clean`。
- 静态检查确认生产模块没有访问 `OwnerActionController` 私有字段。

## 5. 尚未解除的生产门

本层当前**不能接生产**：

1. `ActionReceipt.output` 的 safe same-handle bytes authority 尚未接入，因此所有带输出 receipt 都拒绝；`succeeded_read` 仅保留闭集语义，当前不可交付正文。
2. Controller 完成入口与 sealed Executor blueprint 的最终闭合属于相邻 E3C 子层；本层只证明“Controller canonical”，不能替代执行来源证明。
3. outcome ledger 目前有界且满时 fail-closed，但消费后材料清除、最小 tombstone 与重启恢复属于 P3-08E4。
4. 尚未接 ContentIntent、Composer、SemanticGuard、Presentation、`main.py` 或配置。
5. 未修改/部署 FNOS，未重载 AstrBot，未改变任何线上 adapter 开关。

下一层必须继续保持：动作 outcome 与 AnySearch/LivingMemory Grounding 两条语义通道互斥，最终文字只能由 exact current plan + canonical outcome 合同生成和守卫。
