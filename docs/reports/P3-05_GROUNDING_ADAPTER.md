# P3-05 Typed Tool Result → GroundingFact 纯模块报告

## 结论

P3-05 纯模块已完成。`core/tool_result.py` 现在可以把一次 AstrBot `ToolCallsResult` 绑定到 P3-04 `AcquisitionRequest` 的完整 `DecisionBinding`、`action_id`、request-shape、唯一 exact tool、attested source、capability、scope、current sender、deadline 和代码时钟；`core/grounding_adapter.py` 只有在这些事实与实际 call/result 全部一致时，才把有界安全 claim 转为同轮 `GroundingFact`。

本阶段没有接入 `main.py`，没有修改 P3-04 Tool Broker、Planner、计划表、旧链、FNOS 或第三方插件，也没有执行 Git/GitHub 写操作。P3-06 原子接线前，当前生产调用仍只能得到 unbound compatibility result，而 unbound result 固定不能产生 GroundingFact。

## 先红后绿

- 先新增 `tests/test_grounding_adapter.py`；第一次定向运行稳定失败：`ModuleNotFoundError: astrbot_plugin_shio.core.grounding_adapter`。
- 第一轮实现后，Grounding + ToolResult 定向测试 `17/17` 通过。
- 安全复核发现仅把 Broker 的 action/shape 字段贴到结果上仍不足以证明实际执行参数；新增红测后，实际 `tool_call.function.arguments` 的篡改、额外字段、缺参和重复 call 均稳定不能通过。
- 增加 canonical argument attestation 与 bound-result 代码时钟门后，Grounding、ToolResult、Tool Broker 与 ContentIntent 相关测试 `39/39` 通过。

## Tool Result 绑定

Broker-bound `TypedToolResult` 额外携带以下不可见权威字段：

- 完整 `DecisionBinding`，包含 current message/sender/scope、conversation revision 与 generation epoch；
- 64 位 `action_id` 与 64 位 request-shape digest；
- acquisition deadline 与结果接收代码时钟；
- actual call arguments 的 64 位 canonical digest 与 attested 标志；
- exact tool、selection attested source、capability、side effect；
- scope、current sender、call/result match status；
- 64 位 content digest。

实际参数只用于和 `AcquisitionRequest.materialize_arguments()` 做 canonical 精确比较；参数正文不保存、不进入 repr/trace，也不进入结果 content。dict 和 JSON-string 两种 provider 参数形态在结构等价时可通过；值被改、额外字段、缺字段、非法形态均得到 `ARGUMENT_MISMATCH` 且零 facts。

一次 acquisition 必须恰好有一个 call、一个 matching `tool_call_id` result。missing、orphan、duplicate call、duplicate result、tool error、empty result、late result 和错误 exact tool 都是显式失败 provenance，不能降级为普通文本证据。

未带 `AcquisitionRequest` 的旧调用仍可生成只供迁移期统计的 unbound envelope，但它没有 action/source/argument attestation，P3-05 固定拒绝。带 request 时必须显式提供结果接收代码时钟，防止默认时间绕过 deadline。

## Typed EvidenceOutcome

`adapt_grounding_evidence()` 对每次取证返回 frozen/slots、repr-safe 的 `EvidenceOutcome`。成功状态是 `ACCEPTED`；失败状态包括：

- `MISSING_RESULT`、`ORPHAN_RESULT`、`MULTIPLE_RESULTS`；
- `TOOL_ERROR`、`TIMEOUT`、`UNSAFE_CONTENT`、`INVALID_RESULT`；
- `STALE_BINDING`、`OTHER_SENDER`、`BINDING_MISMATCH`；
- `ACTION_MISMATCH`、`ARGUMENT_MISMATCH`、`DEADLINE_MISMATCH`；
- `TOOL_MISMATCH`、`CAPABILITY_MISMATCH`、`SOURCE_MISMATCH`、`VISIBILITY_DENIED`。

除 `ACCEPTED` 外所有结果都强制 `facts=()`。下一 conversation revision/generation epoch、其他 sender、其他 scope、旧 action、旧 deadline 或旧 result 都不能跨轮复用。

## 安全 claim 提取

Renderer 不再接收 `TypedToolResult.as_reference()` 的 XML，也不接收 raw JSON。兼容方法和 `render_tool_result_references()` 固定输出空字符串；P3-06 只能把 `EvidenceOutcome.facts` 接入 ContentIntent。

claim 提取规则：

- structured result 只从 `title`、`snippet`、`text`、`content`、`summary`、`description`、`answer` 等闭集字段读取标量；debug、arguments、query、ID 和未知字段不进入 claim；
- plain-text result 可按行形成候选；JSON-looking/protocol-call residue 不降级成普通文字；
- 剥离 channel/tool/function/Shio envelope、工具调用语法、HTML/XML tag、URL/data URI、Windows/Unix path、API key/token/password/JWT/GitHub-token 样式和 JSON 包裹；
- 单 claim 最长 600 字符，每次最多 8 条，稳定去重；无安全 claim 时返回 `UNSAFE_CONTENT` 和零 facts。

每个 `GroundingFact` 使用同一 binding、64 位 source digest、稳定 fact ID、结果实际 observed_at 和按只读来源给出的有界 confidence。source digest 覆盖 action、shape、exact tool/source、call/result ID 和 content digest，但 trace/repr 不公开这些原值。

## ContentIntent 兼容

成功 facts 已通过现有 `attach_grounding_facts()` 接入测试：

- current anchor 的 required atoms 不变；
- answer language 不变；
- media IDs 不变；
- facts 必须同 binding 且 fact ID 唯一；
- Grounding 只能追加证据，不能重写当前问题语义。

## 隐私与可观测性

`TypedToolResult` 和 `EvidenceOutcome` 使用自定义 repr，只显示 bound/status/capability/count，不显示 content、claim、scope、sender、message、action、call/result ID、source path、URL 或参数。trace 只有闭集 outcome、capability/count/bool，不回显正文、URL、Token 或工具参数。

旧 16 位 result content digest 已升级为完整 64 位 SHA-256。测试覆盖 raw JSON、协议标签、URL、路径、token、参数、call/result ID、scope/sender 和 payload 不进入 claim/repr/trace。

## 验证

- P3-05 Grounding + ToolResult：`20/20` 通过；
- P3-05 + P3-04 Tool Broker + ContentIntent：`39/39` 通过；
- 四个任务文件 `compileall` 通过；
- 并发 P4-01 文件稳定后统一完整测试：`608/608` 通过；
- `git diff --check` 通过；五个本阶段未跟踪文件另经尾随空白扫描为 0；
- 未部署，未执行 Git/GitHub 写操作。

## P3-06 接线要求

P3-06 执行适配器必须：

1. 保存并传回本轮唯一 `AcquisitionRequest`，不得从 event payload 文本重建；
2. 在工具结果接收时向 `adapt_tool_call_results()` 同时传入 request、实际 runtime tool inventory 与代码 `observed_at`；
3. 传入 current `DecisionBinding` 和代码 `now` 调用 `adapt_grounding_evidence()`；
4. 只有 `ACCEPTED` 的 facts 可交给 `attach_grounding_facts()`；失败 outcome 只能让上游自然说明未取得证据或停止，不得复制 raw result；
5. final Persona Renderer 的 tools 必须为空，不能再次取得原始 ToolCallsResult、argument payload 或 XML reference；
6. 当前 binding/epoch 变化、deadline 超时或外部 gate 停止时丢弃结果，不进入下一轮。
