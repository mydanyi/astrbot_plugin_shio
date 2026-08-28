# P3-08E3C1C0：Controller-scoped ActionOutcomeAuthority

日期：2026-08-18（Asia/Hong_Kong）

状态：**两轮独立审计继续发现窄 API blocker；第三轮系统根修与完整回归已通过，重新冻结等待终审；未接生产**

## 1. 本层边界

本层只修改：

- `core/owner_action_controller.py`
- `core/action_outcome.py`
- `tests/test_action_outcome.py`

并新增本报告。没有修改 ContentIntent、ReplyComposer、SemanticGuard、Repair、Presentation、`main.py`、Executor、owner-action contracts、配置或线上环境；没有执行 Git/GitHub 写操作。

本层关闭 E3C1B 独立审计复现的 authority 唯一性缺口：过去同一个 Controller 与同一个 `PlannedActionAuthority` 可以创建两个 `ActionOutcomeAuthority`，两个 authority 各自持有 source ledger，因而能对同一 canonical receipt 各签一次 outcome。现在 authority 的唯一性由 Controller 实例本身原子登记，不再由调用方约定。

## 2. 先红后绿

先把新合同写入测试，再运行定向测试。实现前真实红灯为：

```text
ImportError: cannot import name 'ActionOutcomeOperation'
Ran 1 test
FAILED (errors=1)
```

新增红测同时冻结：

- 普通 constructor 必须关闭；
- 同 runtime pair 串行与 8 路并发只能签发一个 authority；
- copy/deepcopy/pickle/完整 slots 的 `object.__new__` clone 必须拒绝；
- exact Controller、exact plan authority、limit 与内部 ledger/lock shape 漂移必须拒绝；
- Controller↔authority 强引用环必须可由 GC 回收；
- module reload 后的新 Controller graph 必须能重新签发；
- owner operation 必须精确、完整投影到安全闭集；
- intent 的公开字段、repr 与 trace 只能出现闭集 operation 与布尔值。

首版实现后本层定向 `22/22` 转绿，但独立对抗审计继续从 private mutable state 复现四个真实 blocker：

```text
source ledger clear -> same receipt second issue accepted
record.consumed=False -> same outcome second consume accepted
Controller mirror slots clear -> second authority issued
full-slots clone replaces mirror -> clone accepted as canonical
Ran 4 tests
FAILED (failures=4)
```

同一根因还允许把 copied outcome 写入 copied mutable record，再插入 `_records`，使原对象与 copy 分别消费。上述攻击全部转为正式回归；没有把 private 字段排除出威胁模型。根修后本层定向 `29/29` 转绿。

第一次根修把 mutable dict 改成 module-global `WeakKeyDictionary -> immutable tuple` 后，复审进一步证明“value 不可变”仍不等于“registry slot 不可替换”：直接删除 registration、把 state 替换为空 tuple、unconsumed tuple 或单一 forged record，仍可重签/重放/接纳 copy。最终实现因此继续移除所有 module-global registry/lock/state 对象，把 WeakKey registries 与锁封入工厂闭包，只暴露窄且单向的原子操作；这一轮才是本报告以下描述的最终方案。

第二轮 closure-vault 复审又证明“窄”仍不等于“安全”：旧 `_lookup_persistent_record` 暴露完整 record，旧 `_append_persistent_record` 接受 caller-built record 与 caller-selected `max_outcomes`，旧 `_consume_persistent_record` 信任 caller 返回的 record；此外旧 `_claim_runtime_authority` 接受 caller-built authority。对抗者可用两个 exact receipt 把 copied outcome 重绑到第二 receipt、直接绕 current-plan/Controller 复核消费、以较大 limit 越过登记上限，或用 `object.__new__` 填 slots 后 claim 成 canonical authority。四项正式红灯为：

```text
copied outcome + second exact receipt raw append -> accepted
raw consume without current-plan/Controller reinspection -> accepted
registered limit=1 + caller limit=2 raw append -> accepted
object.__new__ authority + private claim + Controller mirrors -> canonical
Ran 4 tests
FAILED (failures=4)
```

最终根修删除 candidate claim、raw record lookup/append/consume 与 full-state snapshot API。vault 现在自行 mint authority、outcome 与 record；只接收 exact Controller/source/current plan 等可由 canonical inspector 重新验证的输入，上限只从 closure registration 派生。四项转绿后，本层定向共 `33/33` 通过。

## 3. Controller-scoped 唯一 authority

### 3.1 唯一签发入口

`ActionOutcomeAuthority(...)` 永久抛出 `action_outcome_authority_constructor_forbidden`。唯一入口为：

```python
ActionOutcomeAuthority.issue_for_runtime(
    controller,
    planned_action_authority,
    max_outcomes=...,
)
```

该入口直接调用 vault 的完整 `issue_runtime(controller, planned_action_authority, max_outcomes)` 操作；不再在 vault 外构造 candidate authority，也没有 claim/candidate-registration API。vault 固定使用 Controller lock → vault lock 的唯一锁序，自行 mint canonical authority 并原子建立：

- exact Controller weak key；
- authority weak reference；
- exact `PlannedActionAuthority` object；
- registration 与 authority instance seal；
- `max_outcomes`；
- 一个 authority weak key 对应的空 immutable state tuple。

Controller slots 只保存同一 registration 的可校验镜像，不再是唯一权威。即使三个镜像 slot 被清空，WeakKey registration 仍使第二次签发抛出 `action_outcome_authority_already_issued`；即使把镜像 authority 替换为完整 slots clone，registry 中的 exact weak reference 仍拒绝 clone。不会返回旧实例，也没有 reset、replace 或 unbind API。错误 plan authority 在登记前 fail-closed。直接调用 module-private runtime issue 也只会执行同一安全 mint 语义，无法传入 candidate；第二次同样拒绝。

### 3.2 每次消费前重新复核

以下所有 public authority 操作在读取 Controller lineage 或本地 ledger 之前，先调用 Controller exact registration inspector：

- `issue_from_receipt`
- `issue_from_denial`
- `issue_from_continuation`
- `inspect_outcome`
- `consume_outcome`

closure-vault 的 safe issue/inspect/consume/metrics 操作还会再次调用 Controller registration inspector；safe issue 随后重新运行对应 Controller lineage 与 exact `PlannedActionAuthority` inspector，safe inspect/consume 在取出内部 record 后重新验证 current canonical plan/source lineage。`trace_metadata`/`repr` 同样不能被非 canonical clone 使用。

copy、deepcopy 与 pickle 有显式拒绝；填满所有 slots 的 `object.__new__` clone 即使持有相同内部引用，也因 Controller 保存的 exact authority identity 不同而拒绝。

### 3.3 Immutable persistent outcome state

Authority 实例不再具有 `_records`、`_source_records` 或 per-instance `_lock`。两张 `WeakKeyDictionary` 与唯一 registry lock 只存在于一次性 vault builder 的闭包 cell，不是 module global、Controller field 或 Authority field；builder 完成绑定后也从 module namespace 删除。vault 只导出完整的 runtime issue/inspect、outcome issue/inspect/consume、聚合 metrics 与只读 registry counts。它不导出 candidate claim、record lookup/append、raw consume、full-state snapshot 或 generic set/replace/clear/pop/reset/unconsume 入口。state 值严格为 built-in immutable tuple，每项 record 是不可原地修改的 `NamedTuple`。issue 只能由 vault 内部新建 fresh intent/record 后原子替换整份 state；consume 只能由 vault 内部把 exact canonical record 的 `consumed=False` 单向变成新 `consumed=True` record 并替换整份 state。

state 每次读取都验证双向 exact 约束：

- outcome 在 state 中恰好出现一次；
- `(source kind, exact source object)` 在 state 中恰好出现一次；
- receipt/denial/continuation source object 必须与对应 typed record 字段为同一对象；
- outcome 完整 snapshot、operation 投影和 consumed bool 类型必须成立。

因此 clear/reset/registry replacement API 已从对象和 module globals 物理消失；调用方既拿不到 vault record/state，也没有安装 caller-built intent/record 的入口，copy 始终 non-canonical。safe issue 在锁内对 source/outcome 双向唯一与登记 limit 做原子复验，limit 不能由 caller 覆盖；safe consume 本身执行 exact registration、current plan、source lineage、canonical outcome 与 false→true 复核。registration built-in tuple 的 `object.__setattr__` 攻击及 raw API 缺失均有正式回归。

### 3.4 生命周期与 reload

Controller 与 authority 都支持 weak reference。Controller 持 authority、authority 持 Controller 的运行图由 Python cycle GC 正常回收；测试删除最后一个外部强引用并 `gc.collect()` 后，两端 weakref 均为 `None`。

本实现没有数值 ID registry，也没有可从 module global 取得并改写的 mapping/lock/state 对象。vault 内两张 `WeakKeyDictionary` 使用 exact object 弱键；Controller registration value 只持 authority weakref、exact plan authority、limit 与 seal，state value 不反指 authority/Controller。测试在新 graph 回收前后通过只读 counts 同时确认两张 registry 长度恢复，证明 value 不阻止 GC。每个新 Controller graph 相互隔离；独立 subprocess 真实执行 `importlib.reload(action_outcome)` 后，旧 Controller 的不可空 registration 镜像仍阻止第二 authority，而全新的 Controller graph 可以签发新版本 authority。

威胁边界明确为：防本合同覆盖的 public/private authority、outcome、Controller mirror field 篡改，防 module-global registry/state 篡改、copy/clone/pickle/cross-runtime/replay、caller-built record/outcome 注入和 limit override；不声称防御任意 Python 代码重写函数 bytecode、直接读取或改写 closure cell、重新绑定受信模块函数、`ctypes` 改内存或替换已加载模块本身，否则任何纯 Python authority 都可被同进程任意代码重写。

## 4. 安全 operation 投影

新增公开闭集 `ActionOutcomeOperation`：

| 内部 `OwnerActionOperation` | 公开 `ActionOutcomeOperation` |
|---|---|
| `ARTIFACT_READ_EXACT` | `read_artifact` |
| `ARTIFACT_GREP` | `search_artifact` |
| `MEMORY_WRITE_LITERAL` | `save_memory` |
| `SANDBOX_SHELL_ONCE` | `run_sandbox_command` |

映射使用不可变 `MappingProxyType`。模块加载和每次投影均要求 mapping key 集合与 `OwnerActionOperation` 枚举集合精确相等；未来新增内部 operation 而未补投影时 fail-closed，不会退回自由字符串或隐式默认项。

`ActionOutcomeIntent` 的公开字段现在严格为：

- `kind`
- `operation`
- `attempted`
- `has_output`

`repr()` 与 `trace_metadata()` 只包含这两个闭集枚举和两个 bool。内部 adapter operation、ID、digest、reason、路径、参数、正文与 output 仍不公开。对 `operation` 的 `object.__setattr__` 修改由 exact snapshot 检出。

## 5. 原有 E3C1B 合同保持

- receipt、denial、continuation 三条来源继续使用 Controller canonical lineage inspector；
- current plan、origin request/action、capability/operation、resolution 与 terminal receipt 继续 exact-object 闭合；
- 同一 source 串行/并发仍只能签一个 outcome；
- outcome 仍是一次性 consume；
- `ActionReceipt.output is not None` 继续 hard reject，live same-handle output authority 未绕过；
- ActionOutcome 仍不导入、不生成、不伪装 `GroundingFact`；
- CONFIRM 使用 current canonical `EXECUTE_ACTION`，DENY/CANCEL 使用 current canonical `REPLY`。

## 6. 验证结果

使用 Codex bundled Python，在仓库父目录运行。

### 6.1 本层定向

```text
python -m unittest astrbot_plugin_shio.tests.test_action_outcome -q
Ran 33 tests in 0.205s
OK
```

### 6.2 E3C1A focused

```text
python -m unittest \
  astrbot_plugin_shio.tests.test_owner_action_contracts \
  astrbot_plugin_shio.tests.test_owner_action_controller \
  astrbot_plugin_shio.tests.test_owner_action_executor \
  astrbot_plugin_shio.tests.test_owner_action_output_guard -q

Ran 127 tests in 0.191s
OK
```

### 6.3 Owner Action 相关回归

```text
python -m unittest <owner-action related modules> -q
Ran 255 tests in 0.439s
OK
```

### 6.4 全仓与静态检查

```text
python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py' -q
Ran 917 tests in 1.186s
OK
```

另外：

- 三个变更 Python 文件 `compileall -q` 通过；
- subprocess 的真实 module reload / old Controller 拒绝二签 / new graph 签发探针通过；
- 没有保留旧 constructor 兼容入口；
- 没有数值 ID registry 或 module-global registry/lock/state；vault 的 key/value/GC/reload 合同均有正式回归；
- 没有 candidate claim、caller-built record/outcome 安装、raw record lookup/consume、full-state snapshot 或 caller limit 参数；
- AST/static gate 确认 module-level assignment 不创建 `WeakKeyDictionary`/`RLock`，源码无 clear/pop/setdefault 与 generic state reset/replace/unconsume 函数；
- 限定文件 whitespace/diff check 通过。

## 7. 尚未解除的门

本层只关闭 authority 唯一性和 operation 投影，不宣称 E3C1C 或生产完成：

1. `ActionOutcomeIntent` 尚未接 Content/Composer/Guard/Repair/Presentation。
2. Evidence 与 ActionOutcome 的互斥仍需在后续 semantic-delivery 接线中证明。
3. live same-open-handle output authority 仍未建立，非空 output 继续 hard-off。
4. E4 生命周期/tombstone、E5 `main.py`/配置原子接线、E6 容器/FNOS 门均未执行。
5. 本层必须先经过独立 clone/cross-runtime/concurrency/reload/GC/operation-map 审计；blocker 非零时不得进入语义交付接线。

下一入口：独立审计 E3C1C0。只有 blocker=0 后，才能使用这一个 Controller-canonical authority 把 exact `ActionOutcomeIntent` 接入 Content/Composer/Guard/Repair/Presentation。
