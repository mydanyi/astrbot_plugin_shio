# P3-06B 最终回复协议防泄漏补强报告

## 结论

本轮补强关闭了末端 response guard 对动态工具名的四类遗漏：工具名与参数跨行、动态 XML 调用、独占裸工具名，以及独占 `工具名:` 节点。修复只作用于发送前协议清理，不改变 Action、ToolBroker、sealed acquisition、Persona Renderer 或发送流程。

该补强不能单独证明 P3-06 全部安全边界已经闭合。因此，本报告只验收 response protocol guard，不替代 sealed executor、ToolBroker 或线上 AnySearch 的独立验收。本轮并行复核中，sealed executor 已改为隔离 event/run context、跳过生产 `on_tool_start`，并在 hook 与 executor 后检查直接发送尝试；该结论记录在 P3-06 主报告/相应执行器测试中，不冒充为本补强的实现成果。

## 修复前红灯

以下内容会被 `contains_tool_protocol()` 判定为协议，但清理后仍残留独占工具名，或根本未被识别：

- `anysearch_search` 与下一行 JSON 参数；
- `search_memes` 与下一行 JSON 参数；
- `anysearch_search` 与下一行括号参数；
- `<anysearch_search ... />`、成对动态 XML、未闭合动态 XML；
- 独占 `anysearch_search` 与 `anysearch_search:`。

这会破坏发送守卫的不变量：检测到泄漏后，清理结果必须不再含有同一个协议节点，不能把裸工具名作为聊天气泡发送。

## 实现边界

`core/response_guard.py` 现在：

1. 允许结构化工具名与 `{...}` / `(...)` 参数之间出现一个换行及可选中英文冒号；
2. 识别并删除代码闭集中的动态 XML 自闭合、成对和未闭合节点；
3. 将整条输出只有工具名（可带冒号）视为独占协议节点；
4. 将独占 `工具名:` 行视为明确协议残片；
5. 只有在同轮已经删除结构化调用、XML 或孤立参数时，才继续删除相邻的无冒号裸名行；
6. 保留普通技术说明、反引号中的函数名以及带列表语义的工具说明，避免把正常讨论当作调用协议。

工具名仍来自调用方传入的闭集，并经过安全名称校验；没有引入对任意英文标识符的宽泛过滤。

## 回归覆盖

`tests/test_core.py` 新增三组回归：

- 跨行 JSON / 括号参数清理后不留裸名；
- 动态 XML、独占裸名和冒号节点全部失败关闭；
- 普通技术 prose、内联代码和列表说明保持原样。

红灯阶段三组测试共有 9 个子用例失败。实现后：

- response guard 新增定向测试：`3/3`；
- 当前完整 `tests.test_core`：`49/49`；
- response guard 定向 + sealed executor + ToolBroker + ToolResult + Grounding：`53/53`；
- 本轮较早的 core/executor/broker/grounding/output validator/reply composer/pipeline/P3 atomic 联合回归：`196/196`。

随后执行的仓库全量 discovery 共收集 `656` 项，失败项全部来自仍在并行接线的 P3-07 semantic guard/repair controller（`6 failed + 2 errors`），没有 response guard、executor、broker 或 grounding 失败。P3-07 收口后仍必须重新跑全量测试；在此之前不能宣称全仓测试全绿。

## 剩余边界与后续验收

以下问题不在本轮 response guard 修改范围内，不能因为本报告变绿而冒充完成：

- ToolBroker 已拒绝 localhost、私网、链路本地、多播、保留地址和歧义数字主机；DNS rebinding 与跨域重定向不能由纯 URL 语法守卫证明。若 AnySearch 是远端 API 代抓，它们属于供应商执行边界，不是 FNOS 本机直连 SSRF，但仍需在实际插件合同与线上 conformance 中确认；
- 当前 fnOS 只读入口返回 `errno 135168`，未能读取目标机实际 AnySearch handler，也未完成本报告对应代码的线上部署与真机 search/extract 验证；
- owner broad capability 当前只有合同层表达，生产热路径尚没有按能力逐类封装的副作用执行适配器。

本地 response protocol guard 可以单独验收通过；P3-06/P3-07 全阶段状态仍应以各自报告、全量测试和线上 conformance 的合并证据为准。
