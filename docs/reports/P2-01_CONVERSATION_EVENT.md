# P2-01 ConversationEvent 与会话 revision 报告

## 结论

P2-01 已完成纯生产类型与单元测试。新增不可变的 `IngressEvent → ConversationEvent` 两阶段结构，以及按 scope 独立、提交时原子校验的 `ConversationRevisionBook`。本任务没有接入 `main.py`，也没有提前实现 P2-02 的准入策略。

## 先红后绿

- 先新增 `tests/test_conversation_event.py`。
- 第一次定向运行因 `core.conversation_event` 尚不存在而稳定失败。
- 实现后同一测试转为 `7/7` 通过。

## 类型边界

### IngressEvent

- 复用既有 `TurnEnvelope`、`PrincipalContext` 和 P1 `SenderKind`。
- 只保存 SHA-256 `content_digest`，对象中没有正文、昵称、群名片或自称字段。
- 结构化 envelope 与 principal 的 sender 必须一致；缺 scope/session/message/sender 时失败关闭。
- 创建时生成 32 位十六进制 trace ID，并绑定 revision candidate 与 generation epoch。

### ConversationEvent

- 只有 accepted commit 才产生。
- 复用 P1 `DecisionBinding`，强制 scope、session、message、sender、content digest、revision、epoch 和 trace 全部与 IngressEvent 相同。
- 两阶段对象及 binding 均为 frozen dataclass。

## PluginSource 可信来源

- `PluginSource` 是闭集枚举，包含 none、Shio receipt、Parser、Keywords、Progress、Management、Meme Manager 和可信其他插件类别。
- 正文中的 `[parser]`、自称来源、昵称或“我是主人”等字符串不会改变 plugin source、sender kind 或 principal。
- 非 `NONE` 来源只能通过 `issue_plugin_source_evidence()` 生成的 sealed typed evidence 进入；原始枚举、字符串或缺失 evidence 均失败关闭。
- plugin evidence 携带源事件 SHA-256，不保存源正文或外部 ID。
- P2-02 才负责建立实际 adapter/registry 与 ingress admission；本任务没有按昵称识别机器人或插件。

## Revision 机制

- `peek(scope)` 只读取当前值并返回 `base + next` candidate，不创建 scope 状态。
- rejected commit 先验证 scope/staleness，再返回 `None`，不写 revision。
- accepted commit 在锁内比较 base/current 后原子递增；同 scope 的 A→B→A 得到 `1/2/3`。
- 不同群 scope 独立从 `1` 开始。
- 同一 base 的第二个 candidate 被视为 stale；调用者声明的 scope 与 event scope 不一致时失败关闭。
- `accepted` 必须是实际布尔值，避免整数或其他 truthy 值意外提交。

## Trace 隐私

trace metadata 只包含：

- 随机 trace ID；
- scope/message/sender 的诊断摘要；
- 仅记录内容已绑定的布尔值；SHA-256 只保留在内部 binding，不写入 trace；
- SenderKind、PluginSource、revision/epoch 和布尔状态。

测试确认 trace 不包含正文、原始 message/sender/session/scope ID。事件对象不提供正文回读字段。

## 验证

- P2-01 定向测试：`7/7` 通过。
- 仓库完整回归：`419/419` 通过。
- 覆盖不可变性、SHA-256、trace 隐私、principal/scope 绑定、可信插件来源、peek/reject 零写入、A→B→A、多群隔离、stale/cross-scope 和缺失结构身份。
- 新模块通过 `compileall`，相关文件通过 `git diff --check`。
- 未修改 `main.py`、计划文件、FNOS、配置或第三方插件；未执行 Git/GitHub 写操作。

## 设计边界

- 当前 `accepted` 是供 P2-02 准入层消费的提交门，不代表 ReNeBan/self/known-bot/plugin-echo admission 已实现。
- 当前模块不建立 GroupScene、TrustedBotRegistry、Address、Memory、Media 或历史归一化逻辑。
- 未接生产热路径，因此本报告只证明类型、revision 和隐私合同成立，不宣称线上行为发生变化。
