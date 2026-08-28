# P3-08E1：ContentIntent GroundingFact 严格类型门

日期：2026-08-18（Asia/Hong_Kong）

## 1. 根因

`ContentIntent.__post_init__()` 原来只读取 `grounding_facts` 每一项的 `.binding`。因此任何碰巧带有同轮 binding 的对象都能通过，包括：

- canonical `ActionReceipt`；
- `GroundingFact` 子类；
- 任意自定义 binding carrier。

这会破坏 P3-08 的结果分流：公开资料取证只能形成 `GroundingFact`，主人动作只能形成独立 `ActionReceipt/ActionOutput`，二者绝不能因字段形状相似而互换。

## 2. 先红后绿

先新增 `tests/test_content_intent_grounding_type.py`。修复前 4 项测试稳定产生 3 个 failure 和 1 个 error：

```text
- canonical ActionReceipt 没有被拒绝；
- GroundingFact 子类没有被拒绝；
- 自定义 BindingCarrier 没有被拒绝；
- 普通 object 在访问 .binding 时泄漏 AttributeError。

Ran 4 tests
FAILED (failures=3, errors=1)
```

修复后：

```text
python -m unittest astrbot_plugin_shio.tests.test_content_intent_grounding_type
Ran 4 tests
OK
```

## 3. 修复

只在 `core/contracts/context.py` 的 `ContentIntent` 构造边界增加一道 exact-type 检查：

```python
if any(type(fact) is not GroundingFact for fact in self.grounding_facts):
    raise ContractViolation("grounding_fact_type_invalid")
```

该检查位于 binding 访问之前，所以未知对象不再产生 `AttributeError`。随后原有 `grounding_fact_binding_mismatch` 检查继续执行，合法类型也不能跨 revision/binding 使用。

实现直接引用同模块先定义的 `GroundingFact`，没有 import `ActionReceipt`，因此没有新增 contracts 循环依赖。它不是按类名、字段或 duck typing 判断，也不允许子类绕过。

## 4. 验证

相关合同、构建、取证、语义守卫和 owner action 回归：

```text
python -m unittest \
  astrbot_plugin_shio.tests.test_content_intent_grounding_type \
  astrbot_plugin_shio.tests.test_content_intent_builder \
  astrbot_plugin_shio.tests.test_grounding_adapter \
  astrbot_plugin_shio.tests.test_semantic_guard \
  astrbot_plugin_shio.tests.test_owner_action_contracts \
  astrbot_plugin_shio.tests.test_behavior_contracts

Ran 87 tests
OK
```

共享工作树完整发现：

```text
python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py'
Ran 734 tests
OK
```

新增矩阵同时证明：

- 多个 exact `GroundingFact` 仍可合法进入同一 `ContentIntent`；
- exact `GroundingFact` 的 binding 错配仍返回原有 `grounding_fact_binding_mismatch`；
- canonical `ActionReceipt` 即使 binding 完全相同也固定返回 `grounding_fact_type_invalid`。

## 5. 边界

- 本层没有把 receipt 转换、包装或摘要成 GroundingFact。
- ActionReceipt 永远走独立 owner-action receipt 路径；不能进入 `ContentIntent.grounding_facts`、`attach_grounding_facts()` 或公开证据事实链。
- 本层没有修改 owner action contracts/controller/adapters、main、planner、配置、Git 或 FNOS。
