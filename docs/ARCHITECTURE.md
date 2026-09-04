# 星汐 0.5 架构

本文描述当前源码，而不是旧版公开 README 中的 Planner/Replyer 架构。

## 设计原则

星汐是 AstrBot 插件，只增强官方聊天链：

1. 只接受 AstrBot 提供的真实事件。
2. 正式回复始终通过 `event.request_llm()` 进入 AstrBot。
3. 不复制管理员、Persona、conversation、Provider fallback、工具执行、沙箱、媒体或平台适配器。
4. 插件只在当前事件上补充事实、等待批次、删减群聊工具、检查最终文本和安排后续文字气泡。
5. 辅助模型不带工具、不发送消息、不写主 conversation。

## 责任边界

| 责任 | AstrBot / 其它插件 | Shio |
| --- | --- | --- |
| Master 身份 | AstrBot `event.is_admin()` | 只读取结果 |
| Persona 与正式 conversation | AstrBot | 不创建第二份 |
| 正式模型和 fallback | AstrBot | 不接管 |
| 工具注册、确认、执行和沙箱 | AstrBot / 工具插件 | 仅删减普通群友当前可见工具 |
| 群聊上下文注入 | 应由 AstrBot 官方链负责 | 当前实现仍待纠偏 |
| 当前回合身份与时间事实 | 提供事件字段 | 捕获一次并注入当前请求 |
| 未点名群聊是否参与 | 提供真实事件 | 无工具辅助判断 |
| 连续文本等待 | 提供逐条事件 | 按真实会话维护短期内存批次 |
| 最终文本审核 | 提供最终响应 Hook | 可审核和替换同一文本 |
| 标准首发和网络适配 | AstrBot | 不替代 |
| 后续文字气泡 | 提供公开 `event.send()` | 仅在安全条件下编排 |
| 表情包 | Meme Manager | 不选图、不发送图片 |
| 长期记忆 | LivingMemory 等 | 不接管 |

## 源码布局

### `main.py`

当前 3,646 行，是插件入口和运行时编排层，包含：

- 初始化、PluginKV 读取和插件卸载隔离；
- 聊天范围判断；
- 连续文本批次；
- 名称语义和自然参与辅助模型；
- AstrBot LLM request、response、agent、decorating、after-send 和 tool Hook；
- 群聊当前工具可见性；
- 最终审核与修改；
- Master 故障摘要；
- 文本布局和后续气泡发送。

这里仍然存在明显的结构债务：入口、批次、辅助模型、审核、告警和气泡都集中在同一个类中。修复当前上下文问题时不要顺手整体搬家；后续应通过独立、行为保持的重构逐步拆分。

### `core/sys001.py`

当前 1,183 行，主要保存可独立验证的领域对象和纯规则：

- `TurnSnapshot`：当前真实事件的身份、会话、时间、@ 和 Reply 事实；
- 入口允许/拒绝与直接/自然来源判断；
- `REPLY / WAIT / NO_ACTION` 解析；
- 自然参与冷却、频率和退避；
- 官方群历史行投影；
- 群聊 ToolSet 单向删减；
- 最终结果与工具结果分类；
- 文本分段和 Unicode 边界保护；
- Master 摘要状态。

该模块不应增加 Provider 循环、平台发送、权限执行或 AstrBot 私有对象。

### `core/active_event_fence.py`

用弱引用跟踪当前插件实例拥有的事件。插件重载或终止后，旧实例的迟到 Hook 不应继续发送气泡、写状态或处理同一个事件。

### `tests/`

测试覆盖纯规则、官方 Pipeline 回放、自然参与节奏、KV 生命周期、重载隔离、气泡发送、Meme Hook 顺序和设置 schema。测试数量很多不等于生产行为已经验收；与 AstrBot、QQ、Provider 和第三方插件有关的链路仍要在真实 Docker 环境验证。

## 运行链路

### 1. 真实事件进入

`request_event_bound_reply()` 接收 AstrBot 的真实 `AstrMessageEvent`：

- 已被命令处理器解析的事件不接管；
- 先检查普通私聊、群聊和禁用名单；
- 图片、文件、语音、视频和 Reply 等官方边界组件保留 AstrBot 的事件边界；
- @、Reply-to-self、官方唤醒和获准私聊属于明确回复；
- 未点名群聊根据名称唤醒和自然参与设置决定是否继续。

### 2. 连续文本形成批次

同一真实会话最多维护一个当前批次和一个等待批次。每条新文本重置安静窗口：

- 群聊收集同一会话中所有参与者的连续文本；
- 私聊只涉及同一对端；
- 最后一个真实事件是 watermark；
- 只有 watermark 继续一次 `event.request_llm()`；
- 早期文本以批次事实提供，不合成新的 AstrBot 事件。

批次只保存在内存中。插件重载时清空，不持久化聊天正文或媒体。

### 3. 自然参与前置判断

未点名群消息在进入正式 Agent 前调用辅助 Provider。输入要求严格 JSON：

- `REPLY`：继续正式请求；
- `WAIT`：当前不回复，也不写回复节奏；
- `NO_ACTION`：当前不回复，并为同一会话写入短暂退避；
- 非法输出、超时、取消或 Provider 不可用：保持安静。

这个调用使用 `func_tool=None`，不建立主 conversation，也不发送消息。

### 4. 进入 AstrBot 正式请求

通过真实事件调用：

```python
event.request_llm(prompt=current_text, conversation=conversation)
```

AstrBot 因此继续负责 Persona、conversation、正式 Provider、fallback 和工具循环。

### 5. 请求 Hook

`attach_turn_and_project_capabilities()`：

- 把 `TurnSnapshot` 追加到 system prompt；
- 只对 AstrBot 已确认的 Master 附加可选表达规则；
- 对普通群友当前 `ToolSet` 做单向删减；
- 当前还会调用官方群历史投影并改写 `req.contexts`，这是待修问题。

### 6. Agent 与最终文本

AstrBot 完成正式 Agent 和工具链。Shio 只观察工具结果，不把 post-tool Hook 当成动作成功回执。

`observe_final_agent_response()` 可对同一条最终文本执行：

1. 审核；
2. 可选完整文本替换；
3. 再次审核；
4. 通过、耗尽或过期终态。

辅助审核使用总 deadline；不会另起一次主回复。

### 7. 文本布局与发送

`layout_text_components()` 在 decorating 阶段选择单段、按句或模型分段。布局必须满足：

- 所有分段拼回后与原文完全一致；
- 不破坏 Unicode 组合字符；
- 不依赖会被 AstrBot 或 Meme Manager 删除的首尾空白；
- 段数在配置范围内。

AstrBot 继续执行结果装饰和第一条标准发送。关闭 AstrBot 内置分段时，`send_remaining_bubbles()` 才可能通过公开 `event.send()` 发送余下文字。TTS、文本转图片、QQ forward、取消、重载、过期或发送异常会让 Shio 保留标准单链或停止后续气泡。

Meme Manager 的 after-send Hook 位于后面；只有余下文字成功完成后，才允许它继续单独发图。

### 8. 终态和自然参与节奏

`record_standard_send()` 记录的是 AstrBot RespondStage 已完成，不是 QQ 网络送达回执。自然参与的回复次数和冷却只在这一终态提交。

PluginKV 写入有 8 秒上限，失败时保持关闭。插件不会据此重试或补发聊天消息。

## 当前上下文缺陷

`_apply_official_group_history()` 当前会先清空群聊 `req.contexts`，再写入持久化群聊历史投影和当前批次。这会带来两个问题：

1. 临时群聊历史可能随 AstrBot Agent 结果保存进长期 conversation；
2. 代码绕开了 AstrBot 的 `group_icl_enable` 当前请求注入链路。

后续修复必须从 AstrBot 4.27.4 官方源码确认：

- `extra_user_content_parts` 在何处生成和消费；
- conversation 在什么阶段保存；
- 当前真实消息是否已包含在官方群聊注入中；
- 图片转述和群聊媒体如何进入当前请求；
- 连续批次怎样避免与官方群聊注入重复。

不要为了保留 sender ID、历史行 ID 或 watermark 而再次把官方持久化历史复制到 `req.contexts`。如果官方接口不提供某个字段，应降低插件能力或只使用当前真实事件，不能建立第二套群聊上下文系统。

## 后续模块拆分建议

在上下文修复稳定后，可以按以下边界逐步拆分 `main.py`：

| 建议模块 | 职责 |
| --- | --- |
| `runtime/ingress.py` | 范围、直接回复与名称唤醒 |
| `runtime/batching.py` | 连续消息批次与边界事件 |
| `runtime/auxiliary.py` | 辅助 Provider 路由、deadline 和取消 |
| `runtime/review.py` | 最终文本审核与修改 |
| `runtime/master_alert.py` | Master 绑定、计数与勿扰 |
| `runtime/presentation.py` | 文本布局、官方冲突检查和余下气泡 |

拆分时必须保持：

- Hook 优先级和公开入口不变；
- 同一事件只进入一次正式请求；
- 旧实例 fence 不变；
- 不引入新的后台回复、Provider、权限或发送 owner；
- 每次只移动一个有明确测试边界的职责。

## 兼容与验证

当前只声明 AstrBot `4.27.4`。真实验证需要使用完整 AstrBot Pipeline，而不是只构造本地 `ProviderRequest`：

1. 从真实平台事件进入；
2. 执行官方 Process、Agent、ResultDecorate 和 Respond 阶段；
3. 验证 conversation 持久化前后的数据；
4. 验证官方群聊上下文、图片转述、工具调用、气泡和 Meme Hook 顺序；
5. 在 Docker 中重载插件，确认旧实例不会继续发送或写状态。

本地测试通过、WebUI 200 或插件成功加载都不能替代真实 QQ 验收。
