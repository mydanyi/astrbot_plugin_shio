# P10-16 群聊上下文与并发回复热修

## 状态

- 阶段：完成并部署
- 生产版本：0.5.9
- 恢复入口：`SHIO_MASTER_PLAN.md` 第 0、7、12 节与本报告

## 现场证据与根因

用户发送明确点名问题“他们上面在聊啥？”后没有收到回复。生产日志证明该轮已经通过消息准入，LivingMemory 也完成了长期记忆召回，但在模型调用之前发生两项独立问题：

1. 同群另一条人类消息在目标轮完成前推进了最新 `GroupSceneSnapshot`。Participation 最终检查错误地要求原目标仍是“当前最新场景”，因此 exact 已密封 assessment 被拒为 `ContractViolation`，主链以 `opportunity_attention_unbound` fail closed，模型和发送阶段都没有发生。
2. LivingMemory 的近期群聊 public reader 因接口变化降级为 `public_reader_unavailable`；Shio 自己虽然配置了 `inject_verified_context=true`，但此前 ledger 只在准备回复时记录当前轮，而且 Replyer 只读取当前发言者线程，不读取已组装的公开群聊背景。结果是长期记忆能出现，实时“上面在聊什么”却经常为零上下文。

## 红灯

- 原目标 assessment 密封后插入同群新消息，旧实现报 `scene_snapshot_not_canonical`。
- 明确直呼正在生成时插入另一个人的 `NO_ACTION` 消息，旧实现推进 generation 并取消旧回复。
- 两位群友先聊天、主人随后询问“他们上面在聊啥”，旧实现的 Replyer prompt 没有 `public_group_context`。

三组失败均先稳定复现，再进入实现。

## 实现

1. `GroupSceneBook` 为每条已接受人类消息保留有界 exact scene binding；Participation 可复核目标轮自己的 canonical 历史场景，而不是要求它永远是最新场景。copy、字段篡改、跨事件和超出有界记录仍 fail closed。
2. generation 分为两类：`MUST_REPLY/MAY_JOIN/REACT_ONLY` 继续推进 current generation 并取消旧任务；`WAIT/NO_ACTION` 只获得不可执行的 passive snapshot，用于规范生成零模型计划，但不能使正在生成的明确回复过期。
3. 每条通过准入的同群人类消息都会进入 Shio 自己的 256 条有界内存 ledger，不再依赖 LivingMemory 私有近期读取接口。
4. 仅当当前群消息明确询问“他们/大家/群里/上面/前面/刚才/之前聊了什么”时，才选取同群、来源已验证、严格早于当前消息的公开人类内容；跨群、未知来源、当前轮之后的内容全部排除。
5. Replyer 新增独立 `public_group_context`，不与当前发言者的 `verified_thread` 混合；系统约束禁止把其他发言者冒充为当前用户或扩写未出现的事实。OutputValidator 从同一 exact assembled context 派生允许引用片段。
6. LivingMemory 长期记忆仍经 Memory Policy 独立消费；本热修没有修改 LivingMemory、AstrBot 核心或其他插件，也不读取其他用户私有记忆来回答群聊回顾。

## 验证

- 核心红绿与并发/上下文 targeted：`36/36`。
- GroupScene、Participation、Pipeline、ReplyComposer、OutputValidator related：`145/145`。
- 发布表面与确定性候选：`12/12`。
- Windows full：`1282/1282`，skipped 7；compileall、diff-check 通过。
- deterministic 0.5.9 candidate：95 文件、510940 bytes，连续两次 SHA256 均为 `2D45292209474873E11E6113CE176A314C5B1D6E562081BC69085FDD13DA20C8`。
- FNOS 隔离 candidate：`1282/1282`，117.197 秒。
- 部署后 production live-copy：`1282/1282`，47.792 秒。

## 生产部署

- 写前版本 0.5.8；容器 running=true、restarting=false、OOMKilled=false、RestartCount=0，WebUI HTTP 200。
- 备份：`/AstrBot/data/backups/shio/P10-16-group-context-20260819T192807Z`，包含原 live 插件、原配置、候选 ZIP/manifest、测试 harness、部署与 source verifier、durable `deploy.state`。
- 部署事务：`PREPARED -> CONTAINER_STOPPED -> OLD_PLUGIN_SAVED -> NEW_PLUGIN_LIVE -> CONTAINER_STARTED -> VERIFIED`；失败会恢复 0.5.8 并重启。
- 生产配置完全未改：部署前后 SHA256 均为 `BAA5CE74443544420AD85229F9EFEE3E57BEAA112AA3C68475269935B7C06F92`；51 项，称名模式 `contains`，上下文开关 true、16 条、9000 字符、LivingMemory 优先项 true。
- 当前 live 95/95 文件与 0.5.9 manifest 一致；StartedAt `2026-08-19T19:29:20.449270068Z`，running=true、restarting=false、OOMKilled=false、RestartCount=0。
- 新启动窗口 Traceback 0、ERROR/CRITICAL 0；0.5.9 加载一次，WebUI HTTP 200。

## 当前边界与下一验证

公开群聊 ledger 是进程内有界状态；重启后的第一轮可使用 AstrBot 提供的、带 verified sender 与同群标识的原生历史。若平台没有提供原生历史，重启后的公开上下文会从新到达的群消息重新累积，不会为追求回顾而放宽身份或跨群边界。

下一步只需用户重新发一条自然问题（例如“亚托莉，他们刚才在聊什么？”）。若仍异常，从 StartedAt `2026-08-19T19:29:20.449270068Z` 后按该轮 `typed_reply.prepared` 的 `context_record_count`、Participation 与发送终态精确归因；不再先改 Prompt 或 LivingMemory。O1 仍未激活。
