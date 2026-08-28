# P3-06 类型化工具结果与协议隔离报告

## 结论

P3-06 已完成。星汐新增 `TypedToolResult` shadow 适配层，把 AstrBot 工具调用结果与函数参数、可见聊天和 assistant 历史分开。

## 上游结构核对

根据 AstrBot 官方开发文档，工具完成后可取得 `FunctionTool`、参数和 `CallToolResult`；历史 `ToolCallsResult` 结构包含 `tool_calls_info.tool_calls` 与按 `tool_call_id` 配对的 `tool_calls_result`。本适配器兼容对象和 dict 变体，但有意不读取 function 的 `arguments` 字段。

参考：

- https://github.com/AstrBotDevs/AstrBot/wiki/en-dev-star-guides-listen-message-event
- https://github.com/AstrBotDevs/AstrBot/issues/1813

## 数据模型

每个 typed result 只保留：

- `call_id` / `result_id`；
- `tool_name`；
- `CapabilityClass` / `SideEffectClass`；
- `scope_key` / 当前 `target_sender_key`；
- `source_kind`；
- `visibility`；
- 截断后的结果正文及不可逆 digest；
- 成功状态与配对 provenance。

模型没有参数字段。API key、Cookie、query 参数、路径参数和函数 arguments 不会进入 typed object、reference renderer 或 trace。

## 可见性

- 公共网页读取、聊天检索：`reference_only`，可在未来 v2 Replyer 中作为带来源只读资料。
- 本地表情检索：`local_presentation`，不作为文字事实注入。
- 写入、Shell、设备和完整 Agent：`internal_only`。
- 未知或无法配对结果：`untrusted`。

P3 仍为 shadow，本层不替换 AstrBot 现有工具循环，也不把第二份回复发给用户。

## 协议隔离

- 新增 `<shio_tool_result_reference>` 内部 envelope renderer，正文和属性均转义。
- 输出守卫把完整或残缺 typed result envelope 视为工具协议；完整块可从正常回答旁边移除，残缺块触发既有协议闭锁。
- trace 只记录数量、成功数、reference 数、orphan 数和能力计数，不记录正文、真实 ID、调用参数或 call ID。

## 回归覆盖

- AstrBot 对象结构和 dict provider 变体。
- 参数中放置敏感值，确认 typed object、renderer 和 trace 都不包含。
- call/result 正确配对、missing result、orphan result。
- 公共读取、聊天检索、本地表达、Shell 和 unknown 的可见性分离。
- 恶意闭合标签内容被 HTML 转义。
- typed envelope 泄漏时发送前移除。
- 实际管线在工具返回后生成 event-local typed shadow 和脱敏 trace stage，正常可见答案保持不变。

## 验证

- 针对性：`164` 项运行成功（`161` pass + `3` expected failure）。
- 完整回归：`270` 项运行成功，`267` 项通过，`3` 项为迁移计划中保留的 v1 expected failure。
- `git diff --check`：通过。
- 未部署、未修改 AstrBot/第三方插件、未执行 Git/GitHub 写操作。

## P3 阶段结论

P3-01～P3-06 已形成完整的 shadow 能力边界：工具分类、群友策略、别名/嵌套防逃逸、可信主人、主动发言和 typed result 均有类型与回归。生产仍由 v1 授权链路裁决，切换必须等 P4～P7 及 P8 影子验收完成。

下一入口：P4-01，建立与具体角色无关的 `AffectAppraisal`，先描述情境、关注对象、关系距离、表层情绪、隐藏在意、行为倾向和回接话题，不包含亚托莉专名或固定台词。
