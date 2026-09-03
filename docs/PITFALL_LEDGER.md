# SYS-001 坑位台账

| ID | 禁止误判 | 当前合同 |
| --- | --- | --- |
| S-001 | 第二 Provider、合成 event 或第二发送链 | 只从真实 event `request_llm` 进入官方链。 |
| S-002 | 猜测身份或 history provenance | 单一 snapshot 只用官方字段；CR-013 保持。 |
| S-003 | 复制权限或授予 Master | Master 只来自 `event.is_admin()`；群 ToolSet 仅单向删减。 |
| S-004 | 恢复工具或投影 Skill | 不从全局库取回；Skill/非 agentic KB 来源未知。 |
| S-005 | post-tool/final hook 当成功或 attempt | 仅保守观察，不形成回执或告警。 |
| S-006 | 把 D-089 的余段编排误判为第二 adapter 或网络 receipt | 以本表后唯一的 D-089 owner 合同为准；它不等于网络送达或第二发送 owner。 |
| S-007 | 空名单/友好名单扩大范围 | 空 allow 拒绝普通用户；Master bypass，友好不升权。 |
| S-008 | 自然 cadence 当网络送达或扩大 D-074 | 仅同 scope RespondStage cadence；冷场和私聊主动零残留。 |
| S-009 | conversation role/content 当可按 QQ 净化的群历史 | 仅读取官方群 `PlatformMessageHistory`；支持 AstrBot SQLite 的 naive UTC 时间，仍拒绝不可解释值；历史不可用只保留当前 event，LivingMemory 不过滤。 |
| S-010 | 文本窗口绕过、跨 event 媒体拼接、旧 event 二次请求或后台补发 | 所有获准文本进入同一安静窗口，唯一 watermark identity 继续一次；Image/File/Record/Video/Reply 和明确命令保持 AstrBot 官方边界。scope 只在官方终态释放，无终态保持失败关闭到重载，状态无正文/媒体，重载静默清空。 |
| S-011 | 让带工具主 Agent 再决定自然静默 | REPLY 前唯一无工具辅助判定拥有 `WAIT`／`NO_ACTION`；主 Agent 只能生成正常可见结果。 |
| S-012 | 辅助 Provider 单条 lookup 异常抹掉后备 | current／显式／fallback route 各自隔离，保留健康对象并在每次 await 后验证 event epoch／token。 |
| S-013 | 并发非 REPLY 候选回退 scope 活动 generation，或丢失 NO_ACTION 退避 | 每 scope 将预判候选序号与已进入主 Agent 的活动 generation 分离；WAIT／invalid／timeout 只退休自身候选；NO_ACTION 在释放当前文本 batch 前用同一 PluginKV cadence key 写短暂退避，但不取消已启动 turn、计成功次数或计回复冷却。 |
| S-014 | 把公开 KV 当 CAS，或恢复 pending／event lifecycle | 只依赖固定 AstrBot 4.27.4 同进程 FIFO＋overlay；同 key pending→committed，重启仅恢复 committed cadence，8 秒超时 fail-closed。 |

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
# R10 timer ownership note

Shio 不建立延迟回复 timer。唯一的插件 timer 是 D-080 的有界 Master 告警勿扰期调度；它不参与自然参与、连续窗口或文本发送，也不代表网络送达。
