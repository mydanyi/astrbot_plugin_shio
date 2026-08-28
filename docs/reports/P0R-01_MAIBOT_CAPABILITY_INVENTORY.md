# P0R-01 MaiBot 完整能力清单与借鉴决策

## 审核范围

- 只读参考仓库：`work/vendor/MaiBot-current`
- 审核提交：`658421499b0e13bbacb348f327010a3b81dd2c62`
- 目标不是复制 MaiBot，而是识别它为何能比普通“被点名后一次生成”的机器人更像持续存在的群聊参与者。

## 总结

MaiBot 的关键不是一个“主动发言”按钮，而是完整的感知—注意—决策—行动—表达—记忆/学习链：

```text
平台消息
→ 规范化、路由、去重
→ 每会话运行时与短期上下文
→ 地址/提及信号
→ 思考必要性与注意力门
→ Planner 选择行为
→ reply / emoji / wait / tool / no-action / proactive
→ Replyer 生成人格化可见表达
→ 效果观察、表达/行为/术语学习与记忆
```

最重要的设计原则是：听到消息、发现消息与自己有关、愿意参与、决定采取什么行动、最终如何说，是五个不同阶段。

## 功能清单

| ID | MaiBot 能力 | 主要源码证据 | 星汐决策 |
|---|---|---|---|
| M-01 | 多平台消息规范化、稳定 session 与技术去重 | `src/chat/message_receive/bot.py:655-797`；`src/platform_io/types.py:183-205`；`src/platform_io/manager.py:577-603` | 借鉴语义，底层复用 AstrBot 事件；补齐明确幂等键和会话 revision |
| M-02 | 每会话常驻运行时、消息队列、上下文恢复 | `src/maisaka/runtime.py:134-216,291-434` | 借鉴有界 GroupScene/ParticipantState，不复制整套平台运行时 |
| M-03 | 结构化 @、回复、昵称/别名检测 | `src/chat/utils/utils.py:162-270` | 结构化信号借鉴；不能照搬无边界 `kw in msg_content` |
| M-04 | 回复必要性/频率门与近期存在感惩罚 | `src/maisaka/turn_scheduler.py:63-132`；`turn_gates.py:38-132`；`reply_necessity.py:136-276` | 重点借鉴；建立独立 Attention/Participation 层 |
| M-05 | 群消息批处理、安静去抖、Planner 打断 | `runtime.py:1035-1085,1635-1759`；`reasoning_engine.py:839-914` | 借鉴；用 typed revision/epoch 实现，不让旧回复追上新消息 |
| M-06 | Reply/Wait/No-action 为一等行为 | `prompts/zh-CN/maisaka_chat.prompt:1-31`；`builtin_tool/wait.py:10-68` | 必须实现；不能再“唤醒即必答” |
| M-07 | Planner 决定做什么，Replyer 决定怎么说 | `reasoning_engine.py:972-1074`；`maisaka_chat.prompt`；`maisaka_replyer.prompt` | 必须实现轻量 typed 版本；不恢复星汐旧 Planner/二次口语化链 |
| M-08 | 回复动作显式绑定目标消息 | `builtin_tool/reply.py:83-194,295-500` | 星汐已有 ReplyTarget，可直接作为地基 |
| M-09 | 表情是独立社交动作 | `builtin_tool/send_emoji.py:50-62`；`emoji_manager.py:843-902` | 借鉴 React/Emoji action；与第三方 Meme Manager 建立 typed 契约 |
| M-10 | 主动插件任务/proactive trigger | `runtime.py:581-659`；`reasoning_engine.py:859-914` | 借鉴 synthetic trigger 概念；不得伪造真实用户或继承最后一位用户权限 |
| M-11 | Focus/跨会话注意力 | `config/official_configs.py:1149-1186`；`focus/manager.py:224-315` | 后置可选；先做单群准确参与，避免提前引入跨会话隐私风险 |
| M-12 | 工具按阶段动态注册与延迟发现 | `builtin_tool/__init__.py:79-186`；`core/tooling.py:262-413` | 借鉴行为工具/外部工具分层；继续由星汐 CapabilityPolicy 硬授权 |
| M-13 | 人格、行为风格、回复风格分离 | `config/official_configs.py:189-281`；`maisaka_generator_base.py:93-116` | 必须借鉴；通用情绪/行为引擎与 Persona Package 分开 |
| M-14 | 人物档案与关系事实 | `person_info/person_info.py:48-248`；`memory/person_profile.py:146-271` | 借鉴稳定人物状态；权限身份永远不能从档案学习 |
| M-15 | 短期/人物/中期/长期记忆分层与方向边界 | `memory/heuristic_injector.py:54-321`；`services/memory_service.py:92-490` | 借鉴作用域、来源和纠错；继续使用 LivingMemory，不复制 A_memorix |
| M-16 | 表达候选与场景化表达学习 | `maisaka_expression_selector.py:95-322`；`official_configs.py:4025-4162` | 借鉴候选有来源/适用情境/审核；禁止死记完整台词 |
| M-17 | 分段发送和打字时延 | `chat/utils/utils.py:600-745`；`builtin_tool/reply.py:448-500` | 有选择地借鉴；低频、可配置，不能形成固定机械模式 |
| M-18 | 行为、表达、术语学习 | `runtime.py:1790-1972`；`learners/*`；`prompts/zh-CN/learn_*.prompt` | 借鉴候选、证据、审核、撤销；先完成行为链再接学习 |
| M-19 | 回复效果观察 | `reply_effect/tracker.py:40-158`；`judge.py:31-48`；`scoring.py:58-81` | 借鉴指标；MaiBot 默认关闭且闭环不完整，星汐不能夸大为在线强化学习 |
| M-20 | 会话内串行、会话间并行、后台学习隔离 | `heartflow_manager.py:18-92`；`reasoning_engine.py:972-1074` | 借鉴；增加全局推理并发预算和优先级 |
| M-21 | 配置热重载、失败回滚和备份 | `config/config.py:239-257,415-529,699-703` | AstrBot 负责基础热重载；星汐负责 schema 兼容和状态迁移测试 |
| M-22 | Planner/工具/耗时/打断观测 | `reasoning_engine.py:916-970`；`monitor/event_store.py:32-108` | 借鉴产品级决策 trace，但必须在持久化前脱敏 |
| M-23 | 插件子进程、能力令牌和背压 | `plugin_runtime/host/*`；`runner/runner_main.py` | 不复制；AstrBot 已提供插件生态。独立进程也不是 OS 安全沙箱 |

## 关键案例的正确分层

“我觉得笨蛋萝卜子这个称呼很有趣”不应被简化为名字正则：

1. 地址层：不是确定的直接呼语，而是 `lexical_reference + meta_reference`。
2. 注意层：高度自我相关，值得让决策层看一眼。
3. 参与层：结合关系、语气、群聊空档、最近存在感和打断成本，通常有较高接梗价值。
4. 行为层：可选短回复、表情、等待或不行动。
5. 表达层：亚托莉人格可自然反击“你才是笨蛋”，其他人格应按各自性格表达。

因此，@ 或直接称名可以强制进入思考，但不能绕过 Planner 强制发送；非直接提及也不能被硬编码为沉默。

## MaiBot 已知限制

- 别名检测缺少词界、句法和元讨论区分。
- 没有清晰、持久、随事件更新和衰减的连续心境状态机。
- `attention_drift` 找到定义但未确认接入主链。
- 回复效果默认关闭，未形成明确的自动行为学习闭环。
- 未发现全局 Planner/LLM 并发预算。
- 插件 Runner 只是进程隔离，不是主机资源安全沙箱。

星汐应借鉴体系，不应复制这些缺陷。
