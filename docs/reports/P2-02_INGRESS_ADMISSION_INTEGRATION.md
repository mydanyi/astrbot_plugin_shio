# P2-02 Ingress Admission 生产接线报告

日期：2026-08-18  
状态：本地实现与完整回归完成；尚未部署

## 1. 根因

旧 `NaturalNameWakeFilter` 在 AstrBot `WakingCheck` 阶段直接调用星汐运行时：它会创建 generation epoch、取消旧任务、写入 `ConversationRuntime`，并可能触发反馈/学习观察。这个阶段早于 ReNeBan 的 `StarRequest` handler，因此随后被 ban、识别为自身/机器人或插件回声的事件仍可能已经污染星汐状态。

## 2. 新接线

入口现在固定为两段：

1. `WakingCheck` 只创建 frozen、无正文/身份值的 `IngressWakeCandidate`；不得创建 envelope/revision/epoch，不写 runtime/ledger/学习，不提升自然称名。
2. `StarRequest` 以 priority `90` 运行星汐准入。它位于 ReNeBan `114`、Group Verification `100` 之后和 Parser `0` 之前。只有 `ACCEPT_HUMAN` 才提交 conversation revision、推进 generation epoch、取消旧任务并写入群运行时。

普通群消息会激活这个准入 handler，但不会设置 AstrBot 的 `is_at_or_wake_command`，所以不会因为“被观察”而自动调用默认 LLM。`DROP_SELF`、`DROP_KNOWN_BOT`、`DROP_PLUGIN_ECHO` 和 gate 降级只让星汐零消费，不调用 `event.stop_event()`，不会越权阻断其他插件。

## 3. 身份与外部 gate

- 当前 adapter self ID 自动归类 `SELF`。
- 其他机器人只接受精确 `platform_id|sender_id` 配置或 sender-bound typed adapter flag；昵称、群名片和正文“我是机器人/主人”均无效。
- PluginSource 必须是 sealed typed evidence；正文不能伪造 Parser/Meme/Shio 来源。
- ReNeBan 只负责自己的 ban verdict，星汐不读取其私有数据。星汐通过 AstrBot 公共 metadata/handler registry 验证：
  - plugin：`astrbot_plugin_reneban`
  - handler：`data.plugins.astrbot_plugin_reneban.main_filter_banned_users`
  - priority：`114`
- ReNeBan 缺失、禁用、接口变化、handler 不存在或顺序错误时，人类入口为 `DEGRADED_EXTERNAL_GATE`，revision/epoch/runtime/ledger 均不写。

## 4. 自然称名

自然称名候选只在成功准入后提升：

- `这个问题你怎么看，亚托莉？`：准入后 direct wake。
- `笨蛋萝卜子，你这次是大修`：识别“笨蛋”为情绪呼语，准入后 direct wake。
- `我觉得笨蛋萝卜子这个称呼很有趣`：仍是 mention/about-self 候选，不在入口层强制回复；后续由 Address/Participation 决定是否接梗。
- `我刚买了《ATRI》`：作品名语境，不提升。

## 5. 产品 trace

P1 产品 trace 已接入真实入口。候选阶段没有 trace 或状态写；正式准入后按 `ingress → sender_source` 记录。拒绝事件立即形成唯一 `dropped` 终态；接受事件绑定同一 conversation revision、generation epoch 和 trace ID，留给后续阶段续写。trace 不含消息正文、昵称、原始 sender/group/message ID 或工具参数。

## 6. 测试证据

先红：新增管线测试因缺少 `prepare_ingress_candidate`/`admit_inbound_event` 稳定失败。  
绿灯：

- ConversationEvent/IngressAdmission/TrustedBotRegistry/ReNeBan adapter 定向测试通过。
- 自身、已登记机器人、正文伪装、ReNeBan 缺失/禁用、候选零副作用、普通提及、句尾直呼、情绪呼语均有生产接线回归。
- `GenerationEpochRegistry.next_epoch()` 为只读；exact expected advance 失败不推进状态。
- 完整测试：`478/478`。
- `_conf_schema.json` 可解析；`git diff --check` 在仓库根目录无诊断。

## 7. 边界与下一步

- 本任务没有修改 ReNeBan、LivingMemory、Parser、AstrBot 核心或 Docker 配置。
- ReNeBan 在 LivingMemory 被动捕获之后才 stop 的生态边界仍是 `X-01`，继续留在 P10 后可选 O1，不阻塞主线。
- 尚未部署。下一接线项是 P2-08：删除生产相邻 user→assistant 归属推断，统一 assistant/tool/plugin 历史来源。
- 未执行 `git add`、`commit`、`push` 或任何 GitHub 写操作。
