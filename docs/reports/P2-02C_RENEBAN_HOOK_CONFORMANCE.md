# P2-02C ReNeBan Hook Conformance Adapter 报告

## 结论

P2-02C 已完成隔离的 ReNeBan hook conformance adapter。它只检查 AstrBot 的公开插件 metadata 与 handler registry 形状，返回 `PluginEvidenceStatus` 和闭集、无正文的证据；不导入 ReNeBan 实例、不读取黑名单、不调用 ban 查询函数。

本子任务没有接入 `main.py`，因此只证明当前已知 ReNeBan/AstrBot hook 合同可在生产接线前被 fail-closed 验证，不代表 ban verdict 已进入星汐热路径。

## 先红后绿

- 先新增 `tests/test_reneban_adapter.py`。
- 首次定向运行因 `core.plugin_adapters` 不存在而稳定失败。
- 实现 adapter 后，同一测试转为 `8/8` 通过。
- 随后针对真实 AstrBot 注册表 API 增加“registry 不可迭代”的默认 loader 红灯测试；旧实现返回 `ERROR`，修正后测试集转为 `9/9` 通过。

## 固定兼容合同

adapter 只有在以下条件同时成立时返回 `VERIFIED`：

- AstrBot context 提供可调用的 `get_registered_star()`；
- `metadata.name == astrbot_plugin_reneban`；
- metadata 已加载且 `activated is True`；
- metadata 声明预期 handler full name；
- AdapterMessage handler 的 module 为 `data.plugins.astrbot_plugin_reneban.main`；
- handler name 为 `filter_banned_users`；
- full name 为 `data.plugins.astrbot_plugin_reneban.main_filter_banned_users`；
- event type 为 `AdapterMessageEvent`；
- handler `enabled is True`；
- priority 严格为 `114`，并且高于星汐 `90`。

handler registry 通过可注入 `registry_loader(context)` 获取，单元测试不依赖 AstrBot 全局状态。未注入时，adapter 延迟导入 `EventType` 和 `star_handlers_registry`，并调用 `get_handlers_by_event_type(EventType.AdapterMessageEvent, only_activated=False)`，不会假设 `StarHandlerRegistry` 自身可迭代。若存在公开的 `get_handler_by_full_name()`，还会用预期 full name 补取精确 handler，使明确禁用的 handler 仍可被识别为 `DISABLED`，而不是误报为缺失。

## 失败状态映射

| 观测 | 状态 |
|---|---|
| metadata 不存在 | `MISSING` |
| plugin 或 handler 明确禁用 | `DISABLED` |
| context/metadata/registry/handler 结构变化，或 handler 缺失 | `INTERFACE_CHANGED` |
| priority 缺失、类型错误、不等于 114 或不高于 90 | `HOOK_ORDER_INVALID` |
| context getter、registry loader 或迭代过程异常 | `ERROR` |

异常正文不会进入证据；只保留闭集 `ReNeBanEvidenceReason`。

## 隐私与职责边界

`ReNeBanHookEvidence` 是 frozen/slots，只包含：

- `PluginEvidenceStatus`；
- 闭集 reason code；
- loaded/activated/present/enabled 布尔；
- priority 数字。

trace 不包含插件名、module/full name、context、handler、消息正文、sender/message ID、UMO、异常正文或 ban 数据。测试使用会在访问 `star_cls`、handler callable、ban data 时直接失败的对象，确认 adapter 没有读取这些私有表面。

## 验证

- P2-02C 定向测试：`9/9` 通过。
- 覆盖健康合同、插件缺失、插件/handler 禁用、context/metadata/handler 接口变化、handler 缺失、event type、priority、异常与 trace 隐私。
- P1 插件 conformance 和 P2-02B admission 相关回归通过；三组定向测试合计 `30/30` 通过。
- 新模块通过 `compileall`，相关文件通过 `git diff --check`。
- 未修改 `main.py`、计划、其他测试、配置、FNOS 或第三方插件；未执行 Git/GitHub 写操作。

## 后续接线限制

- 这是 hook 结构 conformance，不是 ban verdict 查询；生产接线仍需把该状态与实际事件 stop/gate 事实安全组合。
- 任一非 `VERIFIED` 状态都必须让 P2-02B admission 对 human fail closed。
- `X-01` 不变：ReNeBan StarRequest priority 114 仍晚于 LivingMemory WakingCheck 被动捕获，本 adapter 不能被描述为 LivingMemory 零存储。
