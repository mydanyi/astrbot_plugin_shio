# SYS-001 审查手册

确认真实 event 只进入一次 `request_llm`，Persona/conversation/fallback/media/Respond 仍由 AstrBot。检查 snapshot 官方字段、空名单、block 优先和 Master bypass；私聊 ToolSet 原对象不变，群聊仅从当前 ToolSet 单向删减，Skill 不伪造来源。

检查 post-tool 仅观察、最终 hook 不代表 Provider attempt；审核必须是有界的审核→修复→重新审核，耗尽默认 `review_exhausted` 并阻止文本，最后回复只可由明确开关允许且不计告警。自然 `WAIT`、非法和不可用判定不改变 cadence；`NO_ACTION` 在释放当前文本 batch 前以同一 scope PluginKV key 的 pending→committed 写入短暂退避，不增加成功次数或回复冷却；普通自然文本只在同 event/generation/message binding 的 RespondStage 完成后一次性提交成功 cadence；direct 不能递增自然 generation。多 Plain 不自动等于多消息；不存在网络 receipt、发送补偿或第二 adapter。D-089 owner 以本段后唯一的结构化合同为准。确认 `model` 分段失败完整降级、`segmented_reply` 只读检查官方 `content_cleanup_rule`，以及 Shio 100000 优先级先于固定 Meme Manager 4.15.4 的 99999 正常 hook。D-074 来源在 schema、代码和测试中零残留；其余 CR 缺口不得旁路补齐。

<!-- SYS001_D089_OWNER_CONTRACT_BEGIN -->
```json
{
  "contract": "SYS001-D089-owner-v1",
  "astrbot_owner": ["adapter", "standard_first_send", "official_segmented"],
  "shio_when": {"fixed_safe": true, "official_segmented": "disabled"},
  "shio_action": ["public_event_send", "orchestrate_remaining_bubbles", "await_remaining_bubbles"],
  "shio_not_owner": ["network_receipt", "retry", "compensation", "second_adapter"]
}
```
<!-- SYS001_D089_OWNER_CONTRACT_END -->
<!-- SYS001_D089_OWNER_RENDER_BEGIN -->
D-089 owner 合同：AstrBot 拥有 adapter、标准首发和官方 segmented；只有 fixed-safe 且官方 segmented 关闭时，Shio 才通过公开 `event.send()` 编排并等待剩余气泡。Shio 不拥有网络 receipt、retry、compensation 或第二 adapter。
<!-- SYS001_D089_OWNER_RENDER_END -->

检查自然参与只有前置无工具辅助调用可返回 `WAIT`／`NO_ACTION`；REPLY 后主 Agent 不再收到或解释静默协议，即使先调用工具再输出同字面量也必须保持普通可见文本。名称／自然分类器共享 snapshot、结构化 @／Reply、时间及按黑名单净化并排除当前行的官方群历史；`DIRECT_OTHER`是谨慎判定事实，不得由缺失上下文擅自改写。

检查同一 scope 并发预判时，候选序号不等于已进入主 Agent 的活动 generation：E2/E3 的 `WAIT`、`NO_ACTION`、invalid、timeout、取消或迟到只能退休各自候选，不能回退 E1；E1 仍必须经审核并在标准 RespondStage 完成后恰好提交一次 cadence。后续真实 `REPLY` 仍应提高活动 generation 并 fence 旧 E1 的 cadence，重载/epoch 使迟到候选静默失效。

检查 D-075 KV 只使用公开 PluginKVStoreMixin：固定 4.27.4 的同进程 FIFO/overlay 是兼容前提，不得表述为任意 KV 的 CAS。每次 cadence 写必须同 key `pending` 后同 transaction `committed`；初始化只接受 committed，且 KV await/termination drain 的 8 秒上限、timeout/cancel fail-closed 与 overlay/durable restart 矩阵都必须有实际回归。

检查群聊 request 的 `contexts` 只来自官方 `message_history_manager` 行投影：黑名单 sender 整行消失，bot 为 `assistant_self`，AstrBot SQLite 支持的 naive UTC `created_at` 规范为 aware UTC，非 datetime 或不可解释时区仍拒绝；旧行缺少平台 message ID 或 Reply sender/time 不得猜测，官方群历史关闭/读取失败时 contexts 为空而非回退 conversation 文本，LivingMemory 字段保持原对象。检查获准文本的首条和后续都等待、每 scope 只有最后真实 watermark identity 进入一次请求，deadline 每条重置，watermark 不重复进入 contexts，批次行不和官方群历史重复。Image/File/Record/Video/Reply 是官方边界 event，命令保持官方 handler，不得由 Shio 排队/重放。无终态 hook 时 scope 必须保持失败关闭到重载，不得以私有 deadline 释放或声称取消工具；状态不得保存正文/媒体，容量和重载必须静默失败关闭。所有辅助 purpose 的 current／显式／fallback Provider lookup 要逐 route 隔离；错误、超时、取消或迟到不得抹掉健康 route 或改写 event。
# R10 timer ownership check

确认 Shio 没有第二延迟回复 timer；唯一允许的 `asyncio.sleep`/timer 是 D-080 Master 告警的有界勿扰期 worker，不能用于自然参与、连续窗口或发送循环。
