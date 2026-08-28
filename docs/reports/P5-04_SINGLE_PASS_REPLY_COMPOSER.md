# P5-04 单次 ReplyComposer 契约报告

## 结论

P5-04 已完成。新增 typed `ReplyComposerRequest/Result`，一次生成同时处理当前语义、人格、关系距离、情绪轨迹、可选表达行为和气泡计划。调用预算固定为生成 1 次、常规风格重写 0 次。

## 请求边界

生成前强制验证：

- current message 摘要与 `ReplyTarget` 一致；
- `LocalChatPlan.principal_key` 与目标 sender key 一致；
- 当前 persona package 与 expression package 一致；
- reply shape 只能是 chat bubbles 或 long form；
- chat bubble 上限 clamp 到 1～3。

目标 message/sender ID 保存在请求对象中用于发送绑定，但不会写进模型 Prompt。模型只看到当前昵称、当前消息和最小表达数据。

## 单次生成数据

同一个请求包含：

- 人格摘要和核心 trait 行为描述；
- 首选语言、长度和格式偏好；
- 可信关系距离对应的称呼与边界风格；
- affect trigger、表层/次级情绪、隐藏在意；
- 完整 trajectory、avoid 和 topic return；
- 0～3 条行为候选；
- 仅作为可选项的情境口头禅。

候选为空时明确要求自然作答，不补固定句或其他角色模板。

## 结果解析

`parse_reply_composer_output()` 只做本地确定性清理：

- chat 输出按限制拆为自然气泡；
- long form 保留完整段落；
- 记录原始输出 digest；
- `rewrites_performed=0`。

解析 API 没有模型、Provider、polisher、oralizer 或 rewrite 回调。

## 隐藏通道修复

定向测试发现旧 `clean_response()` 能检测但不能移除 `<|channel|>thought/final`。新增 `strip_hidden_channel_sections()`：存在明确 final 段时只保留 final 内容，thought/analysis/commentary 本地丢弃，不调用第二个模型。

## 验证

- P5-04 定向：`10` 项通过；
- 与旧 guard/pipeline 相关：`168` 项运行成功（含 3 项 expected failure）；
- 完整回归：`368` 项运行成功，`365` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 未接入生产生成链，未部署，未执行 Git/GitHub 写操作。

## 下一入口

P5-05：建立最小 `OutputValidatorV2`。只检查协议/隐藏推理、目标和关系身份越权、无依据事实、意外语言切换、必需语义缺失或禁用事实出现；不再把普通语气、没有口头禅、情绪强弱、长度偏好和轻微重复当成重写理由。
