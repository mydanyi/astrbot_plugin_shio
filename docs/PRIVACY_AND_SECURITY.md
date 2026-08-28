# 隐私与安全说明（0.5.10）

## 当前轮会处理什么

| 数据 | 用途 | Shio 默认持久化 |
|---|---|---:|
| 平台、bot、session/group、sender、message ID | current scope、目标、权限、并发 | 仅 HMAC 指纹／有界计数或 revision |
| 当前消息和身份可证明的近期上下文 | ContentIntent 与回复 | 同群 verified 人类公开入站消息保留有界原文尾部；其他上下文不复制 |
| LivingMemory 候选事实 | scoped grounding | Shio 不复制长期记忆 |
| direct/quoted 图片／语音 transport | 当前生成和一次 repair | 不持久化 URL、base64、path |
| Persona 资产 | 身份、情绪和表达 | 随插件发布 |
| 互动／反馈／学习候选 | 聚合关系与 reviewed expression | 聚合值、摘要和 review 状态 |
| 推理／发送／产品 trace | 诊断与预算 | content-free stage/count/digest |

在线聊天模型、搜索、记忆和其他 Provider 可能收到管理员允许的当前内容；请分别核对这些服务的数据保留、训练与地区政策。

## 插件数据目录

可能出现的持久文件包括：

- `social_state.json`：作用域化互动次数、反馈和表达权重；
- `behavior_outcomes.json`、`learned_behavior_artifacts.json`、`learned_behavior_activation.json`：聚合候选、review/activation/revoke；
- `continuity/runtime_continuity.json`：安装密钥域下不可逆 scope/subject fingerprint、revision 与 cadence；
- `proactive/proactive_state.json`：不可逆群 fingerprint、配额、冷却以及每群有界的近期 topic/final SHA-256 摘要；不保存主动成稿或类别标记正文；
- `public_group_ledger.json`：重启后显式群聊回顾所需的公开消息尾部；每群最多 32 条、最多 128 个 scope，只保存 accepted + verified human inbound，不保存助手输出、工具结果、引用或第三方 legacy history；
- `owner_action/` 下的 install secret 与 lifecycle journal：operation/status/effect/attempted 等最小闭集。

`public_group_ledger.json` 会保存上述有界公开群聊原文尾部；其余文件不保存完整群聊副本、失败草稿或 `pending_replies.json`。所有文件都属于可关联运行数据，备份、迁移、删除或提交 Issue 时必须按敏感文件处理。删除 continuity/proactive/owner journal 可能破坏重启防重放，不应在服务运行时手工操作。

## 身份与权限

- 主人身份只来自当前 AstrBot event sender ID 与 `owner_ids` 的 exact match；昵称、自称、引用、Persona、系统提示和 LivingMemory 不能授权。
- 主人身份只是动作资格。`owner_action_enabled` 和四个逐项开关默认 false；live runtime conformance、参数、确认、lifecycle、completion、ActionOutcome 与真实 delivery 缺一即拒绝。
- Shell 永久硬关闭。普通群友只能得到管理员精确允许、能力分类为只读且本轮有资料需求的工具。
- Shio 不是宿主沙箱，不能替代 Docker、文件权限、网络隔离、密钥分离和第三方插件自己的权限模型。

## 媒体与输出

- MediaContext 只含 item ID、kind、origin、source message/sender、availability 与 caption digest；原 locator 在 opaque Provider transport 中短暂保留。
- 仅图片消息只用代码固定描述建立 current binding，不把 URL 或 component repr 当成用户正文。
- SemanticGuard、OutputValidator 和 final Presentation 检查协议、身份、关系、事实、ActionOutcome、媒体 claim 与 exact segments；repair 最多一次且无工具。
- 只有真实发送成功的 segment 才进入 SentReply／反馈；失败、取消和 stale 输出不参与学习。

## 日志

默认结构化日志只应包含 schema、stage、closed status、计数、布尔和不可逆 digest。`permission_audit_log` 记录被移除工具数量与脱敏作用域，不记录工具参数、正文或原始身份。0.5.0 还把 `openai/httpx/httpcore` 的敏感 DEBUG logger 提升到 WARNING，避免 AstrBot root DEBUG bridge 输出 Provider 请求体。

提交 Issue 前移除：

- API Key、Cookie、Token、QR／登录材料；
- 真实 QQ／群／bot ID、昵称和私聊；
- 内网地址、路径、journal root、命令和工具参数；
- LivingMemory 原文与其他人的个人事实。

使用稳定占位符（如 `<OWNER_ID>`、`<GROUP_A>`、`<USER_B>`）保留关系结构，不要上传整份 Docker log。

## 主动参与与学习

- 未点名参与只消费 current public scene 和 accepted human lineage；other-person、bot/plugin、banned、owner action 与 stale generation 不得进入。
- `proactive_initiation_enabled` 默认关闭；即使开启也只使用公共 scene 和 Persona 公共兴趣，不读取最后用户的个人事实或 owner-private memory。
- 学习候选必须高置信、多 reviewer、内容安全、作用域正确，并经 review authority；默认不自动改写 Persona，activation 可撤销。

## X-01 与 O1

外部 `X-01` 仍存在：LivingMemory 的被动捕获可能早于 ReNeBan 和 Shio admission。0.5.0 只承诺 banned/bot/plugin 消息在 Shio ledger、Scene、Affect、Learning、Tool、Reply 中零消费／零提交，不宣传 LivingMemory 零存储。

可选 O1 尚未执行。未经用户在 P10 完成后确认，不更新 LivingMemory、不修改 ReNeBan 或其他第三方、不创建上游 PR。

## 验证口径

当前 Windows 与隔离 AstrBot Linux 自动化超过 1,200+ 项，并固定 privacy fixture scan、日志 shape、media locator、owner output、restart continuity 与 plugin degradation 门。自动化不替代最小权限部署和 FNOS 真实加载／WebUI／自然流量观察。
