# P3-02 ContentIntent 纯合同层报告

## 结论

已新增 persona 无关的 `core/content_intent_builder.py`。它把 P2-07 的 `CurrentQuestionAnchor`、当前 `ReplyTarget`、P2-05 已筛选记忆和 P2-06 `MediaContext` 绑定为同一轮不可变 `ContentIntent`。当前问题的语义原子、回答语言和媒体 ID 只来自当前锚点；历史、引用和 LivingMemory 只能作为已筛上下文存在，不能追加或替换必答语义。

本任务只完成纯 typed 层，未接 `main.py`，也没有保留或扩展 `LocalChatPlan`。生产切换统一留给 P3-06。

## 红灯证据

先新增 `tests/test_content_intent_builder.py`，在实现模块前运行：

```text
Ran 8 tests
FAILED (errors=8)
ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.content_intent_builder'
```

## 实现边界

- `build_content_intent_seed(...)` 强制 Anchor、ReplyTarget、MemoryPolicyResult、MediaContext 使用同一 `DecisionBinding`。
- `required_atoms` 完整继承当前锚点；记忆内容不会变成当前问题。
- 引用消息 ID 只保留在 `ReplyTarget` 的结构化引用边，不会进入必答语义。
- 默认 `zh-CN`，仅继承 P2-07 已确认的显式语言要求。
- typed media item ID 必须与锚点精确一致；缺失、错序或跨轮媒体失败关闭。
- `attach_grounding_facts(...)` 只能追加同一 binding、唯一 fact ID 的 `GroundingFact`，并保持 kind、目标、语义原子、媒体和语言完全不变。
- 模块不导入角色包，不包含亚托莉或任何固定台词。
- repr 与 trace 只暴露闭集种类、计数和布尔量，不回显正文、消息 ID、sender key 或事实内容。

## 验证

```text
tests.test_content_intent_builder: 8/8 OK
ContentIntent + contracts + current anchor + memory + media: 57/57 OK
```

覆盖包括：

- 当前 Python 问题不会被旧 Rust/Java 记忆覆盖；
- 引用旧消息不替换当前问题；
- 否定、动作、实体、媒体 ID 和回答语言保持；
- statement 与 question/request 的内容意图类型区分；
- memory/media/target/grounding 跨 binding 全部失败关闭；
- duplicate grounding 拒绝；
- grounding 前后核心语义不变；
- 源码无角色依赖，trace/repr 脱敏。

## 未完成项

- P3-03 仍需产生 typed `KnowledgeGapDecision`。
- P3-04/P3-05 仍需完成精确工具选择和结果 grounding。
- P3-06 才会让最终 Composer 直接消费 `ContentIntent` 并删除旧 `LocalChatPlan` 热路径。
- 完整套件、容器候选和 FNOS 部署在对应阶段统一执行。

未执行任何 Git 写操作，未部署。
