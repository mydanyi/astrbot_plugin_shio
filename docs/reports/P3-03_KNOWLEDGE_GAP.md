# P3-03 KnowledgeGapDecision 纯模块报告

## 结论

已新增 `core/knowledge_gap.py`，在选择任何具体工具之前，把当前消息归为 `NONE / UNKNOWN_TERM / TIME_SENSITIVE / EXPLICIT_VERIFY / CONFLICTING_EVIDENCE`。决策只绑定 P3-02 的同轮 `ContentIntentSeed` 与 digest 一致的当前消息；它最多请求一次 `PUBLIC_WEB_READ`，不包含工具名、来源、参数或授权结论。

本任务只完成纯 typed 层，未接 `main.py`，未调用网络，也未改变生产工具箱。

## 红灯证据

先新增 `tests/test_knowledge_gap.py`。实现前 8 项均因模块不存在稳定失败：

```text
Ran 8 tests
FAILED (errors=8)
ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.knowledge_gap'
```

首轮实现后又补了两项反机械搜索红灯：“我现在很难过，该怎么办？”和“现在这个项目应该怎么修？”曾被宽泛“现在”规则误判为时效事实；测试先稳定失败 2 项，再将规则收紧为明确最新/实时信号，或时间词与汇率、天气、新闻、版本、政策等时效事实对象的组合。

## 决策顺序

1. 当前消息明确说不要/不用联网、搜索或查证：`NONE`，尊重本轮拒绝。
2. 代码已经观察到证据冲突：`CONFLICTING_EVIDENCE`。
3. 当前消息明确要求查证、核实、联网或来源：`EXPLICIT_VERIFY`。
4. 明确新梗、网络用语、黑话、俚语或缩写语境：`UNKNOWN_TERM`。
5. 问答意图中出现明确最新/实时或时间词与时效事实对象组合：`TIME_SENSITIVE`。
6. 其余普通常识、技术问答和情感聊天：`NONE`。

硬规则不能被模型软提示降低。可选 `KnowledgeGapSuggestion` 只允许在无硬规则时提出 `UNKNOWN_TERM` 或 `NONE`；它不能选择时效、查证、冲突、具体工具、来源、参数或预算，置信度不足时被忽略。

## 边界

- 所有非 `NONE` 决策只表达 `CapabilityClass.PUBLIC_WEB_READ`，预算固定为 1。
- 这不是授权：P3-04 仍必须同时验证 Principal、CapabilityPolicy、配置 allowlist、实际工具来源和运行状态。
- 当前消息正文 SHA-256 与 binding 不一致时立即失败关闭。
- trace 只有 need、布尔量、预算和 reason 数量，不包含正文、message ID 或 sender key。
- 不把“今天很难过”“现在怎么修”误作联网理由；不因普通常识或情感交流每轮搜索。

## 验证

```text
tests.test_knowledge_gap: 8/8 OK
```

覆盖普通常识、情感聊天、陌生网络语、实时汇率/最新版本、明确查证、拒绝联网、冲突资料、软提示边界、digest 错绑和 trace 隐私。

## 未完成项

- P3-04 Tool Broker 才能选择已配置且来源可证明的唯一具体工具。
- P3-05 才能把成功结果转换为同轮 `GroundingFact`。
- P3-06 才会接入生产热路径；普通直答应保持零分类模型调用，搜索路径受总预算约束。
- 真实模型对罕见但无显式“梗/黑话”标志的词，只能通过闭集软建议补充，不能直接制造工具调用。

未执行任何 Git 写操作，未部署。
