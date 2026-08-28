# P6-01 真实发送回执能力审核

## 结论

当前 AstrBot 通用插件接口不能可靠返回“每个已发送气泡”的平台 message ID。星汐不得再根据时间相邻、消息正文、日志内容或群消息顺序猜测平台 ID。

本层已建立独立的内部 `reply_id -> segment -> attempt/success/failure` 映射。`platform_message_id` 是可选字段，只有未来获得明确的 AstrBot/适配器 typed 回执时才允许写入。

## 官方接口核对

- AstrBot 的事件文档把 `after_message_sent` 定义为消息发送后的事件钩子，但回调只接收事件对象，没有发送回执参数：[官方事件文档](https://github.com/AstrBotDevs/AstrBot/wiki/en-dev-star-guides-listen-message-event)。
- 当前官方 `AstrMessageEvent.send()` 的返回标注为 `None`：[官方源码](https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/astrbot/core/platform/astr_message_event.py)。
- 当前官方 `MessageEventResult` 只描述消息链、结果类型和流式结果，不包含平台消息 ID：[官方源码](https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/astrbot/core/message/message_event_result.py)。
- 当前官方 `Context.send_message()` 只返回是否找到并调用平台的布尔值，不返回平台消息 ID：[官方源码](https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/astrbot/core/star/context.py)。

## 线上只读核对

目标：FNOS `192.168.50.38`，容器 `astrbot`。本轮只读，没有重启、修改或部署。

| 检查项 | 线上结果 |
| --- | --- |
| 容器 | running，restart count = 0 |
| AstrBot | 4.27.2，Python 3.12.13 |
| `AstrMessageEvent.send` | `(MessageChain) -> None` |
| `MessageEventResult` | 无平台 message ID 字段 |
| `Context.send_message` | `-> bool` |
| `AiocqhttpMessageEvent.send` | `-> None` |
| QQ 底层发送 | 调用 `send_group_msg` / `send_private_msg` 后没有保留返回值 |

`trim-cli` 的 FNOS Docker 查询返回设备错误码 `135168`，因此按照仓库已有 SSH 配置 `Host FNOS` 使用只读 `docker inspect` / `docker exec` 完成核对。没有猜测账号，也没有变更容器。

## 本地实现

新增 `core/send_receipt.py`：

- `InternalSendReceiptLedger` 为每次回复生成不可与平台 ID 混淆的内部 ID。
- 每个可见气泡拥有独立 segment ID 和 `planned -> attempted -> succeeded/failed` 状态机。
- 记录精确目标消息、目标 principal、session 和 scope。
- 默认成功只代表明确的发送调用完成，平台 ID 保持为空。
- 平台 ID 与 `PlatformReceiptSource` 必须成对提供；没有明确回执来源时拒绝写入。
- 不提供按正文、时间或日志进行模糊反查的 API。
- trace 只输出长度、状态和计数，不输出消息正文或真实 ID。

## 验证

- 新增 8 个单元测试，覆盖内部 ID、分段状态、目标绑定、失败状态、平台 ID 来源约束和禁止模糊匹配。
- 完整测试：408 项执行，405 通过，3 项既有 expected failure。
- `git diff --check` 通过。

## 边界

- 本层没有修改 AstrBot core、QQ 适配器、LivingMemory 或第三方插件。
- 本层没有把 P6 账本接入生产发送路径；接入和“成功后才观察”属于 P6-02/P6-03。
- 本层没有部署，没有 Git 写操作。

## 下一层

P6-02 将梳理三条现有发送路径，把 `record_bot_reply()` 从生成/守卫完成时移动到真实气泡发送成功以后；失败的气泡不得创建成功观察记录。
