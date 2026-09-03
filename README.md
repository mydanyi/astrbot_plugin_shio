# 星汐（Shio）SYS-001

星汐是 AstrBot 插件，不是独立机器人或发送器。当前为本地候选：已做 Codex 自测，尚未独立验证、验收或发布。

## 官方与插件职责

| 责任 | owner |
| --- | --- |
| Persona、conversation、当前 Provider 与 `fallback_chat_models` | AstrBot |
| 管理员、工具权限、内置指令、沙箱 | AstrBot / 工具插件 |
| 媒体、ResultDecorate、Respond 和发送 | AstrBot |
| 真实入口、当前回合事实、名称语义、自然参与 | Shio |
| 群聊当前 ToolSet 单向删减、最终结果观察、文本气泡布局与剩余气泡编排 | Shio |

Shio 不建第二 Provider、第二延迟回复 timer、合成 event 或发送链；仅有 D-080 的有界 Master 告警 timer。私聊 ToolSet 保持 AstrBot 当前对象。

## 未来本地设置依赖

先在 AstrBot 设置 `admins_id`（官方 Master）、Persona、`wake_prefix`、`disable_builtin_commands` 和 `fallback_chat_models`。再设置 Shio：

- `private_allowed_sender_ids`：普通私聊精确名单；每项填写一个 QQ 号，空列表没有普通用户获准。
- `group_allowed_scopes`：普通群总入口名单；每项直接填写一个群号，空列表没有普通群获准。非 Master 时用户或群禁用名单优先；Master 只来自 AstrBot 官方角色。已保存的 R22 会话值仍可兼容，但新设置不要填写、拼接或查询会话标识。
- AstrBot 群近期历史需在其自身配置启用 `provider_ltm_settings.group_message_history_enable`，并关闭会重复注入无结构文本的 `group_icl_enable`。Shio 只读该官方历史、按真实 sender ID 删除黑名单整条记录；不可用时只保留当前真实消息。
- 名称模式及语义模型；自然参与开关、允许自然参与的群号、前置判定辅助模型/备用/超时与说明。自然参与先严格判定 `REPLY`／`WAIT`／`NO_ACTION`，判定阶段没有 ToolSet、主会话或发送，只有 `REPLY` 才进入 AstrBot 主 Agent；`WAIT`、非法或不可用判定均不写 cadence，`NO_ACTION` 会按 `natural_no_action_backoff_base_seconds`／`natural_no_action_backoff_max_seconds` 在同一 scope 持久化短暂退避，但不计回复次数或回复冷却。再配置 `natural_reply_cooldown_seconds`、`natural_frequency_window_minutes`、`natural_max_replies_per_window`。自然 cadence 只按每个真实会话的 RespondStage 完成持久化，不是网络送达确认；群号不会合并不同平台或机器人会话。首次遇到获准群聊时才读取该真实会话的已有状态。候选只兼容已审计的 AstrBot `==4.27.4`；固定版本的公开插件 KV 在同进程按 FIFO 写入、先更新官方 overlay。Shio 在同一 key 先写 `pending`、再写同 transaction 的 `committed`，重启只恢复 committed cadence。每次 KV 等待及终止 drain 上限为 8 秒；超时、异常或取消均 fail-closed。该兼容条件不是任意 KV 的 CAS 或事务保证；升级版本必须重新审计并冻结。
- 友好 sender 与普通结构化能力名单；友好用户不是 Master，`astr_kb_search` 对所有群聊用户保留。
- Master 表达规则及文本组件布局。
- 最终审核范围（core/additional/combined）、辅助 Provider、有界“审核→修复→重新审核”与最后回复开关；耗尽时默认阻止当前文本并按规则告警，只有明确开启最后回复才让同一最终响应继续标准发送，绝不创建第二次发送。
- Master 告警默认关闭。Master 必须先以真实私聊完成官方 UMO 绑定；固定 UTC+8 的可选勿扰时段只延后固定脱敏摘要。`submitted` 仅表示交给 AstrBot 主动发送接口，不代表网络送达；失败不重试，摘要不含正文、QQ 或会话 ID。
- 连续窗口开关、秒数与群数上限：获准的文本消息（普通群友、Master普通发言、文本@／wake及允许私聊）都先等待同一滑动安静窗口；最后真实文本 watermark 只发起一次标准请求，较早文本以独立结构化来源提供。Image/File/Record/Video/Reply 是各自的 AstrBot 官方边界 event，不跨 event 合并；其同 scope 后续文本只在官方终态后开始下一批。明确命令继续由 AstrBot/命令插件处理，不由 Shio 排队。

文本布局可选 `model`、`plugin` 或 `single`。`model` 仅用公开 Provider 生成可逆 JSON 文本边界，失败保留全文单组件；`plugin` 只按句合并且不丢字；`single` 合并连续 Plain。使用 Shio 多气泡时，请关闭 AstrBot 内置分段：AstrBot 标准链仍发送首条，Shio 再通过公开发送接口按“气泡间最短/最长等待”依次发送剩余文字。单气泡不受影响。AstrBot 内置分段若以原样兼容形状开启，则完全保留官方发送，Shio 不会重复发送；若开启但不兼容，会明确报告冲突且不会改写 AstrBot 设置。Meme 图片始终另发、位于全部文字之后，且不计入文本气泡数量。AstrBot 与固定 Meme Manager 会对每个 Plain 做首尾 strip，因此多组件只在每段经该下游语义仍原样时保留；否则安全降回完整单组件。

## 旧配置、限制与回滚

旧 0.5.x 配置/state 完全不读、不迁移，也不自动删除；未来安装前备份 AstrBot/插件配置和相关数据，回滚时恢复备份并移除候选版本。部署或重启需 Owner 另行批准。

- 无网络送达成功回执：自然 cadence 只在 AstrBot RespondStage 完成时同 scope 持久化，某一段的真实网络结果不会被 Shio 推断、重试或补发。
- 旧 AstrBot conversation 的 role/content 不作为可净化群历史；只使用官方群历史中的真实 sender、行 ID、UTC 时间和结构化内容。旧历史缺少平台 message ID 或 Reply sender/time 时会省略，LivingMemory 不受 Shio 过滤。
- D-093 已实现：文本首条与后续都等待安静窗口，watermark 只在 prompt 中表示一次，早期文本和官方群历史行不会重复。scope 只在 AstrBot 官方终态 hook 可证实时释放；若没有终态则保持静默到插件重载，Shio 不接管工具取消或发送。真实安装环境的 AstrBot/Meme 运行顺序仍未独立验证。
- D-078／D-079 已关闭对应选择：Skill、知识库与工具执行／确认／结果继续由 AstrBot 和具体工具插件拥有；Shio 只收窄群聊当前公开 ToolSet，不逐项投影 Skill 或非 agentic 知识库，也不建立动作回执。
- post-tool 是观察而非动作成功回执；主回复第三灾备仍由 AstrBot `fallback_chat_models` 拥有。最终审核可使用有界辅助 Provider 修改同一响应；Master 告警仅在已绑定私聊、阈值达成且非勿扰时通过 AstrBot 官方接口提交。
- 关闭 AstrBot 内置 `segmented_reply` 后，Shio 只会在固定 ResultDecorate 不会再执行 TTS、文本转图片或 QQ 转发的形状下发送剩余文字；这些官方后置处理只要可能改写完整链，Shio 就保留完整标准 RespondStage 单链并记录可读降级状态。剩余文字取消、过期或发送异常时会停止该 event 的后续 hook，避免固定 Meme Manager 单独发送图片；成功时 Meme 图片仍在全部文字之后。真实安装环境的多气泡/Meme 顺序仍需独立验证。
- D-074 的群冷场续题与私聊主动关心暂缓，候选没有字段或状态。

本地测试不证明真实 AstrBot、发送、fallback 或可选第三方顺序；未部署、重启、提交、推送、PR 或发布。
