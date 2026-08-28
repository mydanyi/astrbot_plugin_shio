# P2-02A TrustedBotRegistry 阶段报告

## 结论

P2-02A 的纯注册表子任务已完成。新增的 `TrustedBotRegistry` 只接受三种可信证据：当前 adapter self ID、显式配置的 `(platform_id, sender_id)` 精确对，以及可选的、与发送者绑定的 typed adapter bot flag。昵称、群名片、消息正文和“我是机器人”等自称均不在输入合同中。

本子任务没有修改 `main.py`、`name_wake_filter.py` 或第三方插件，也没有将注册表接入生产 hook。因此这是 P2-02 的可测纯内核，不代表生产准入链已切换。

## 红灯证据

先创建 `tests/test_trusted_bot_registry.py`，再运行：

```text
python -m unittest astrbot_plugin_shio.tests.test_trusted_bot_registry -v
```

实现前的预期失败为：

```text
ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.trusted_bot_registry'
Ran 1 test ... FAILED (errors=1)
```

该红灯证明测试在新模块存在前确实会失败，不是在已有实现上补测试。

## 合同与实现

| 领域 | 硬约束 |
|---|---|
| 当前 adapter self | `sender_id == current_adapter_self_id` 时结果为 `SenderKind.SELF` |
| 显式 bot 配置 | 只按 `(platform_id, sender_id)` 整对精确命中；不支持通配符或跨平台 sender ID 推断 |
| typed adapter flag | 只接受由边界签发函数产生、与当前 platform/sender 完全绑定且 `is_bot=True` 的 flag |
| 未证明身份 | fail closed 为 `SenderKind.UNKNOWN`，不擅自当作人类，也不擅自当作 bot |
| 可见文本 | nickname、group card、message text、quoted claim 和模型输出都不是注册表字段 |
| 不可变性 | observation、decision、flag 和 registry 全部是 `frozen=True, slots=True` |
| 可观测性 | trace 只输出闭集分类、布尔状态、计数和 16 位不可逆 digest；原始 platform/sender/self ID 不进入 trace 或 repr |

信任优先级固定为：当前 adapter self → 显式配置 → 已绑定的肯定 typed flag → unknown。这个顺序避免同一身份在多种证据同时存在时产生不稳定分类。

## 测试覆盖

定向测试覆盖：

1. adapter self 被识别为 `SELF`。
2. 显式配置必须 platform/sender 整对匹配。
3. typed flag 只在肯定、已绑定时生效。
4. flag 不能改绑到另一发送者，裸 `bool` 不能作为证据。
5. 昵称、群名片和正文自称无法进入合同。
6. 全部结构 frozen/slotted，typed flag 不能直接构造。
7. trace 不含原始 ID，只含 digest/分类/计数。
8. 空 ID、通配符、非字符串 ID 和非布尔 flag 全部 fail closed。

## 验证结果

```text
python -m unittest astrbot_plugin_shio.tests.test_trusted_bot_registry -v
Ran 8 tests ... OK

python -m compileall -q \
  astrbot_plugin_shio/core/trusted_bot_registry.py \
  astrbot_plugin_shio/tests/test_trusted_bot_registry.py
exit 0

python -m unittest \
  astrbot_plugin_shio.tests.test_trusted_bot_registry \
  astrbot_plugin_shio.tests.test_conversation_event \
  astrbot_plugin_shio.tests.test_behavior_contracts
Ran 33 tests ... OK
```

## ReNeBan 后续接线锚点

P0 生产快照记录 ReNeBan `metadata.name=astrbot_plugin_reneban`、版本 `1.2.0`、线上源文件 `/AstrBot/data/plugins/astrbot_plugin_reneban/main.py`。当前上游 v1.2.0 的全消息 handler 是 `filter_banned_users`，装饰器 priority 为 `114`。AstrBot v4.26.7 loader 依据插件目录和 `main.py` 组合出 module path `data.plugins.astrbot_plugin_reneban.main`，并以 `<awaitable.__module__>_<awaitable.__name__>` 构造 handler full name，因此该快照对应：

```text
module_path:       data.plugins.astrbot_plugin_reneban.main
handler_name:      filter_banned_users
handler_full_name: data.plugins.astrbot_plugin_reneban.main_filter_banned_users
priority:          114
```

这些锚点仅用于后续版本化 adapter/hook 接线，本子任务未直接导入 ReNeBan 私有实现、未解析其黑名单文件。

## 剩余边界

- typed flag 签发函数必须在后续接线中只由可信 adapter 结构化信号调用；不能把消息字段或模型判断转换成 flag。
- P2-02 后续还需在 ReNeBan/Group Verification 的停止结果之后、任何星汐 ledger/runtime/name-wake 副作用之前完成生产接线。
- 未知发送者仍需由更高层 ingress admission 结合事件类型决定是否可作为人类对话轮；注册表本身不做这个推断。
