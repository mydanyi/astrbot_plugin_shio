# 兼容性说明

## 当前支持范围

| 项目 | 状态 |
| --- | --- |
| Shio | `0.5.1` 主线，包含此前 `0.5.5` 修复候选的修复 |
| AstrBot | 仅 `4.27.4` |
| 平台 | QQ `aiocqhttp` |
| Python | 跟随 AstrBot 4.27.4 运行环境 |
| Meme Manager | 可选；0.5.1 合并前 Docker 组合验证加载的真实插件为 5.0.2；下文保留此前按 5.0.0 核对的协作说明 |
| LivingMemory | 可选；由其自身和 AstrBot 管理 |

`metadata.yaml` 使用 `astrbot_version: "==4.27.4"`。这不是保守的展示文字，而是当前实现依赖固定 Hook、配置形状、PluginKV 和发送顺序的真实边界。

## AstrBot 4.27.4 依赖点

Shio 当前使用：

- `AstrMessageEvent` 的结构化消息、发送者、会话、@、Reply 和管理员信息；
- `event.request_llm()`；
- `OnLLMRequest`、`OnLLMResponse`、Agent 开始/结束、结果装饰和 after-send Hook；
- `ProviderRequest.func_tool` 与 `ToolSet`；
- AstrBot 当前 Provider 和按 ID 选择的 Provider；
- PluginKV；
- 官方 conversation 和标准 Respond 链；
- 平台公开 `event.send()`。

升级到其它 AstrBot 版本前，应逐项检查这些接口和实际调用顺序，并在真实 Docker Pipeline 中验证。不能只把兼容范围改成 `<5`。

## Provider

正式聊天模型、工具调用和 `fallback_chat_models` 完全由 AstrBot 管理。

Shio 的辅助模型仅用于：

- 智能名称判断；
- 自然参与判断；
- 最终文本审核与修改；
- 模型分段。

辅助调用不带工具、不创建第二个正式 conversation、不发送消息。设置页通过 AstrBot 的 `select_provider` 和 `select_providers` 选择已有模型。

## Meme Manager

Shio 不直接选择或发送表情包。当前协作目标是：

1. Shio 先处理最终审核文本；Meme 识别标记并清理文本后，Shio 再做文字布局（Meme 5.0.0 会合并相邻文本组件，不能在它之前分段）；
2. AstrBot 发送标准文字；
3. Shio 在安全条件下发送余下文字气泡；
4. Meme Manager 再根据自身设置单独发图。

0.5.4 还对接 Meme 5.0.0 的辅助选图事件上下文：Meme 的请求 Hook 先生成 `meme_manager_emotion_context_lines`，Shio 在自己的后置请求 Hook 中补充当前 `ProviderRequest.prompt`，避免只读旧历史而遗漏本轮“不要图片”等要求。该列表仅存于当前事件，不追加到 `req.contexts` 或 conversation。Meme 未建立此列表时不处理，主模型工具选图模式不受影响。此事件键属于已核对的 Meme 版本协作点，并非 AstrBot 通用 API；升级 Meme 必须重测。

如果后续文字取消、过期或发送失败，Shio 会停止事件的后续 Hook，避免只剩一张脱离文字的表情包。

0.5.5 在 Meme 的 `llm` 语义模式下，先移除主聊天模型产生的历史图片 ID 标记，再交给 Meme 原生辅助选择。`tool` 模式不做这项清理，仍允许主模型选择本轮候选。模式键同样是 Meme 5.0.0 的版本协作点。该版本的语义选图不使用分类模式的概率开关；频率由选图提示决定，不能把概率 100 当作每轮必发图的保证。

其它 Meme Manager 版本或不同 Hook 优先级需要重新验证。

## AstrBot 分段回复

使用 Shio 控制多气泡时，应关闭 AstrBot 内置 `segmented_reply`，避免二次拆分。

如果 AstrBot 分段仍开启，Shio 只在固定兼容形状下完全交还官方发送；其它形状会记录冲突并避免重复发送。Shio 不会自动修改 AstrBot 设置。

## TTS、文本转图片和 QQ 转发

这些功能属于 AstrBot ResultDecorate。只要它们可能继续改写完整结果，Shio 就不能提前拿走余下文字气泡，而应保留完整官方单链。

因此，设置了多气泡上限不代表所有回复都会拆开。

## LivingMemory

LivingMemory 是独立的长期记忆来源。Shio 不读取、迁移或过滤它的持久数据，也不会把自己的普通群友黑名单伪装成 LivingMemory 的权限规则。

如果记忆内容已经进入 AstrBot 请求，Shio 只能保持该官方/插件链的既有对象，不能保证第三方记忆本身带有可验证的发送者身份。

## 群聊上下文

0.5.1 不读取 `message_history_manager`，不改写 `ProviderRequest.contexts`：

- 持久化群聊记录的官方用途是保存记录和提供查询工具；
- `group_icl_enable` 才是当前请求的官方群聊上下文注入；
- 官方 `GroupChatContext` 注入的 `TextPart` 由 Shio 请求 Hook 标记为 `mark_as_temp()`，避免随 conversation 保存；
- 关闭官方注入时只补当前连续批次的更早真实消息，同样使用临时标记；
- 图片转述仍完全按 AstrBot 官方配置工作，本次纯文本回归不代表真实 QQ 图片下载已验证。

需要群聊上下文时开启官方注入。持久化记录开关仅影响 AstrBot 官方记录/查询工具，Shio 不以它作为自动上下文来源。插件不替用户更改这两项生产设置，也不清理旧 conversation。

## 平台媒体

图片、语音、文件和视频的下载与预处理属于 AstrBot 和平台适配器。QQ 临时媒体 URL 下载 400 不能由 Shio 修复或绕过；媒体失败时 Shio 也不会伪造图片内容。

## 明确不兼容

- AstrBot 4.27.4 以外的未审计版本；
- 非 `aiocqhttp` 平台；
- 依赖旧 Shio 0.4.x Planner、表达库、主动话题或故障补答数据的配置；
- 期望 Shio 提供独立管理员、工具执行器、沙箱或平台发送器的用法；
- 把旧配置字段自动迁移到 `sys001` 的安装方式。

## 验证边界

轻量单元测试只能验证规则和回归条件。下列内容必须使用真实 AstrBot 4.27.4 Docker 环境：

- 官方群聊上下文注入和 conversation 保存；
- 图片转述和 QQ 媒体；
- Provider 工具调用与 fallback；
- ResultDecorate、分段、后续气泡和 Meme 顺序；
- 插件重载和旧实例隔离。

Docker 验证通过仍不等于真实 QQ 用户验收。
