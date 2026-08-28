# P3-08E3C1C1：ActionOutcome → Content / ReplyComposer exact claim

日期：2026-08-18（Asia/Hong_Kong）

状态：**第一次独立复审的 2 个 blocker 已根修；本地完整回归通过并重新冻结，等待复审确认；未接 Guard/Repair/Presentation/main，未部署**

## 1. 本层边界

本层把 E3C1C0 的唯一 `ActionOutcomeAuthority` 接到 ContentIntent 与最终 Persona Composer，但不把动作结果伪装成搜索证据，也不让 Persona 改写代码确认的动作状态。

主要实现文件：

- `core/action_outcome.py`
- `core/content_intent_builder.py`
- `core/reply_composer.py`
- `tests/test_action_outcome_delivery.py`

为使 `ReplyComposerRequest` 的五个新增 required exact 字段没有默认旁路，经主执行者明确批准，以下三个既有测试各只机械迁移一个直接 constructor：

- `tests/test_output_validator_v2.py`
- `tests/test_p8_offline_acceptance.py`
- `tests/test_repair_controller.py`

第一次实现的机械迁移只显式补入 `planned_action`、`content_seed`、`evidence_outcome`、`action_outcome`、`action_outcome_authority`。独立复审要求 `current_question_anchor` 同样成为 required exact 字段后，`test_repair_controller.py` 的孤立 raw 形状 fixture 又机械补入由既有 `semantic_turn()` 产生的 exact anchor；另外两个 fixture 原本已经显式传入 exact anchor。没有修改断言、fixture 语义或测试流程。除此之外没有扩大文件范围。

本层没有修改 Controller、Executor、Owner Action contracts、SemanticGuard、Validator、Repair、Presentation、`main.py`、配置、FNOS 或 Git/GitHub 状态。

## 2. 先红后绿

先新增交付链测试，初始稳定红灯为：

```text
ImportError: cannot import name 'attach_action_outcome'
Ran 1 test
FAILED (errors=1)
```

红测冻结了以下行为：

1. Content attachment 只做 exact authority/current plan/source-lineage inspect，不消费 outcome；
2. `GroundingFact` / `EvidenceOutcome` 与 `ActionOutcomeIntent` 代码级互斥；
3. request 的所有形状、Prompt 和 deterministic 输入完成后，builder 最后一步才 claim；
4. 构建失败不 consume，不存在回滚或预签 delivery handle；
5. exact request copy、字段 mutation、copied outcome、cross substitution、二次 claim 全拒；
6. direct denial、confirmation-required、direct terminal、CONFIRM、DENY、CANCEL 均以正确 current plan 进入 Composer；
7. 普通聊天继续无 outcome，既有 AnySearch Evidence 路径不变。

实现过程中的两处测试夹具错误也被 E2C 合同正确拒绝：只读动作不能伪造“成功但无 output”，也不能伪造不合法 partial 形状。夹具改为合同允许的 terminal 组合，没有放宽生产代码。

第一次冻结后的独立复审发现两个 blocker，并先补为正式红测：

```text
Ran 2 tests
FAILED (failures=1, errors=1)
```

- B1：`current_question_anchor.default is None`，raw constructor 可以省略 anchor；
- B2：Composer 没有 module-owned canonical issuer，`dataclasses.replace()`、逐字段 raw constructor 与旧 private unclaimed builder 的结果都可能直接 claim。

根修后的同一组 blocker 测试为 2/2 OK；无默认值回退，也没有增加 caller candidate register。

## 3. ContentIntent：动作状态不是 Grounding

`ContentIntentSeed` 新增 repr-hidden exact：

- `action_outcome: ActionOutcomeIntent | None`
- `action_outcome_authority: ActionOutcomeAuthority | None`

`attach_action_outcome()` 要求：

- exact `ContentIntentSeed`、outcome、唯一 authority 与 current `PlannedAction`；
- plan binding、ReplyTarget 与 ContentIntent 完整一致；
- authority 对 current plan、Controller source 和 continuation lineage 重新 inspect；
- attachment 不消费 outcome；
- seed 不能已有 Grounding，也不能重复 attachment。

反向边界同样关闭：一旦 seed 持有 outcome，`attach_grounding_facts()` 必须失败。`ActionOutcomeIntent` 从未进入 `ContentIntent.grounding_facts`，原有 `GroundingFact` exact-type gate 不变。

## 4. ReplyComposerRequest：required exact fields 与动作矩阵

`ReplyComposerRequest` 把以下六个字段改为 required constructor 输入，不用默认值隐藏遗漏：

- exact current `PlannedAction | None`
- exact `ContentIntentSeed | None`
- exact `EvidenceOutcome | None`
- exact `ActionOutcomeIntent | None`
- exact unique `ActionOutcomeAuthority | None`
- exact `CurrentQuestionAnchor`（不可省略、不可为 `None`、不可用子类或同名 duck object）

生产 builder 始终填入 exact plan/content；测试层的孤立 validator/repair fixtures 必须显式说明没有 outcome。

动作矩阵固定为：

| current plan | Evidence | ActionOutcome | 结果 |
|---|---:|---:|---|
| 普通 `REPLY` | 无 | 无 | 允许 |
| AnySearch `USE_TOOL` | exact | 无 | 允许，沿用 Grounding 路径 |
| `EXECUTE_ACTION` | 无 | exact | 允许动作状态回复 |
| DENY/CANCEL continuation `REPLY` | 无 | exact denied/cancelled | 允许 |
| 任意 plan | 有 | 有 | blocking |
| `EXECUTE_ACTION` | 无 | 无 | blocking |
| `REPLY` | 无 | 非 denied/cancelled outcome | blocking |

`has_output=True` 继续 hard-off；本层没有用公共 `ActionOutput` 或 Prompt 文本绕过 live same-handle output authority。

## 5. Module-owned Composer vault、原子 claim 与完整快照

旧的 `_build_reply_composer_request_unclaimed()` 与 caller-candidate deterministic validator 已删除。`reply_composer` 现在拥有一个 closure-local vault；lock、弱引用 records、固定容量与 stored snapshot 都不暴露，也没有 raw register、caller-selected limit、reset 或 snapshot 安装入口。

public builder 与 module-private mint 使用完全相同的唯一路径：

1. 从 typed source inputs 校验 exact plan/target/binding/content/anchor/media/persona/evidence/outcome；
2. 固定 reply shape、候选、上下文、媒体、Persona 与 Prompt；
3. vault 检查固定 live capacity；
4. **vault 自己**构造 exact `ReplyComposerRequest`，立即保存覆盖全部 dataclass 字段的 identity + integrity snapshot；
5. public builder 对 action request 最后调用唯一 authority 的 `claim_for_composer(outcome, current_plan, consumer=request)`。

private mint 不接收 `ReplyComposerRequest`、candidate、snapshot、record 或 register 参数；直接调用它仍会执行与 public builder 相同的全部 typed、shape 与 Prompt 校验。普通 chat 与 AnySearch request 也由同一个 vault mint，不能从 raw constructor 获得 canonical 身份。

Composer vault 使用 closure-local immutable weak-reference record tuple，以 `ref() is request` 匹配 exact 对象，不使用 id-only key、module-global dict 或 dataclass value hash。record 不强持 request；普通 request 失去外部引用并 GC 后会释放容量。固定容量满时在构造 canonical request 和消费 outcome 前 fail closed。

ActionOutcome closure vault 在同一个原子状态变换中保存：

- exact request object；
- request 每个 dataclass 字段及所有嵌套 dataclass/tuple/mapping 的完整不可变快照；
- exact current plan、outcome 与 unique authority；
- consumer kind=`reply_composer` 和单向 consumed 状态。

ActionOutcome vault 不暴露 caller-built record、raw append、reset、replace、unconsume 或 snapshot 安装入口。claim/inspect 通过函数局部 import 严格调用 Composer canonical inspector；先验 canonical identity，再验 exact outcome/authority/current plan/content 与完整 snapshot。raw constructor、request copy、cross request、request/plan/content/anchor/persona/Prompt/budget 或嵌套字段变化全部失败关闭，而且拒绝不会 consume 原 outcome。`inspect_for_composer()` 只重验，不再次 consume。

ActionOutcome closure 的 consumed record 按原合同保存 exact claimed request 与独立完整 snapshot；Composer closure 保存的 canonical record则只弱引用 request。两边 snapshot 都覆盖每个 request 字段，测试还把 snapshot 字段名集合与 `dataclasses.fields(ReplyComposerRequest)` 做严格全集相等检查，新增字段不能静默漏封。

局部 import 只用于解除 `action_outcome` ↔ `reply_composer` 模块加载环；入口仍使用 `type(...) is ReplyComposerRequest`，没有 duck typing。

## 6. Persona 可见语义

公开 action operation 与 outcome kind 各有一张完整、只读、模块加载时检查全集相等的中文提示表。Prompt 只加入：

- 代码固定的 `operation_hint`；
- 代码固定的 `status_hint`；
- `attempted`；
- 固定为 false 的 `has_displayable_output`。

不加入 adapter/action/request/pending ID、digest、path、command、query、memory literal、reason、receipt 或 output。Prompt 明确：Persona 只能自然包装状态，不能把成功说成失败、把 partial/unknown 说成确定状态、把未执行说成已执行，或编造未提供正文。

## 7. 验证结果

使用 Codex bundled Python，在仓库父目录运行。

### 7.1 本层 delivery

```text
python -m unittest astrbot_plugin_shio.tests.test_action_outcome_delivery -q
Ran 11 tests
OK
```

覆盖 direct denial、confirmation-required、direct failed terminal、CONFIRM success、DENY/CANCEL continuation、普通 chat canonical mint、Evidence 互斥、失败前不 consume、并发 claim、cross-plan/cross-authority、raw/replace request、完整 snapshot 字段集合、prompt/anchor/plan/content/persona/budget 等值替换、private mint 全校验、固定容量与普通 request GC 释放槽位。

### 7.2 C0 + Content + Composer

```text
python -m unittest \
  astrbot_plugin_shio.tests.test_action_outcome \
  astrbot_plugin_shio.tests.test_action_outcome_delivery \
  astrbot_plugin_shio.tests.test_content_intent_builder \
  astrbot_plugin_shio.tests.test_reply_composer -q

Ran 64 tests
OK
```

### 7.3 E3C1A focused

```text
python -m unittest \
  astrbot_plugin_shio.tests.test_owner_action_contracts \
  astrbot_plugin_shio.tests.test_owner_action_controller \
  astrbot_plugin_shio.tests.test_owner_action_executor \
  astrbot_plugin_shio.tests.test_owner_action_output_guard -q

Ran 127 tests
OK
```

### 7.4 Owner / Content / Composer related

```text
Ran 286 tests
OK
```

### 7.5 全仓与静态检查

```text
python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py' -q
Ran 928 tests
OK
```

另外：

- 相关 Python 文件 `compileall -q` 通过；
- 三个边界扩展 fixture 静态检查确认只做 required exact 字段机械迁移；
- `action_outcome` first 与 `reply_composer` first 两种冷启动 import 顺序均通过；
- 限定文件 trailing-whitespace 检查通过；
- tracked diff 的 `git diff --check` 通过；
- 没有执行 Git/GitHub 写操作。

## 8. 尚未解除的门

本层不宣称语义交付链或生产完成：

1. C2 尚需把 exact outcome/request claim 接到 SemanticGuard、一次 Repair 与 Presentation/FINAL_SEND；
2. ActionReceipt 证明动作状态，不等于可见 SendReceipt；
3. live same-open-handle safe output authority 仍缺失，非空 output 继续 hard-off；
4. E4 生命周期/tombstone、E5 main/config 原子接线、E6 容器/FNOS 未执行；
5. 本次 blocker 根修后的新冻结快照仍须独立复审确认，blocker 非零不得进入 C2。

下一入口：E3C1C1 blocker 根修复审。只有 blocker=0 后，才能把同一个 exact request/outcome 继续绑定 Guard、Repair 与 Presentation。
