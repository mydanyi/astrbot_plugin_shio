# 星汐 0.5.0 公开测试指南

先在专用测试群进行，不要直接给普通群友开放未经审计的工具，也不要开启主人动作适配器。

## 测试前

1. 记录 AstrBot、星汐、平台、Provider、LivingMemory、AnySearch、Meme Manager、ReNeBan 和 Parser 版本。
2. 停用独立 `astrbot_plugin_agent_guard`，避免双重改写工具集。
3. 普通用户白名单只保留确认过的只读工具。
4. 保持 `owner_action_enabled` 与四个适配器为 false；Shell 永久硬关闭。
5. 保持 `proactive_initiation_enabled=false`；主动发起单独验收时才使用一个专用群白名单。
6. 调试日志只在复现期临时开启，导出前脱敏。

## 核心矩阵

### 身份、来源和目标

- 两位成员 A→B→A 快速交替，确认每轮 sender、关系、记忆和回复目标正确。
- 普通群友改昵称、自称主人、引用主人台词，确认不会获得 owner qualification。
- self、known bot、unknown automation、plugin echo、banned 和 external stop 均不得进入 Shio ledger、Scene、Affect、Learning、Tool 或 Reply。
- 引用另一位成员时 reference 可用，但当前授权主体仍是发送者。

### 直接、未点名参与和主动发起

- @、私聊、开头／句中／句尾称名进入直接回复；作品名、URL、代码和谈论角色不应误唤醒。
- 未点名开放群聊分别覆盖 `MAY_JOIN`、`REACT_ONLY`、`WAIT`、`NO_ACTION`；他人私聊、严肃工具轮、owner action 和冷却状态不得抢话。
- React 只能发送 exact expression，不应同时生成隐藏文本。
- 主动发起默认零任务／零发送；开启至少两个专用群后分别验证观察、时段、空闲、冷却、日限额、restart/hot-reload replay、新消息取消和逐群脱敏 reason。allowlist/tracked count 相等不能替代两个群各自真实执行证据。
- `MAY_JOIN` 的稳定知识／黑话问题只选一次 AstrBot KB，明确实时问题只选一次公网搜索；检索失败保持沉默，最终 responder 的 `func_tool` 必须为空。
- 冷场主动续题使用 WebUI 的完整规则、完整公开 Persona、服务器时间和匿名可信群上下文；稳定知识／实时事实按同一边界检索，不能为找话题而搜索。
- 用同一个 proactive request 同时断言：上下文来源、可选 sealed evidence、完整 Persona、配置的最少气泡、每段回执、一个且仅一个被剥离的官方 22 类标记，以及文字全成功后调用共享 Meme Manager transport。各模块分别通过不能替代这条集成用例。
- 主动发送验收必须同时观察内部 `proactive.terminal=sent`、外层 `proactive.outer_terminal=sent`、同群持久冷却/日次数和 topic/final digest；只看群里出现文字会漏掉“已发出但调度器记失败”。连续两轮要覆盖相同话题、近重复成稿、换新话题和跨群相同文本。

### 记忆、关系和学习

- 当前用户 personal fact 可用；另一用户事实、群内 owner-private、无主体私有事实不得进入。
- LivingMemory missing/timeout/脏召回时显式 degraded，不静默扩大原生历史信任。
- 关系状态只改变措辞，不应改变 owner、action、tool 或事实。
- feedback 必须在真实 SentReply 后才产生；单人刷屏、Prompt injection、敏感内容和跨 scope reviewer 不得 activation。

### 多模态

- direct image、quoted image、Reply-ID fallback、multiple、text+image、仅图片、native caption、unavailable 各跑一轮。
- 断言 source message/sender 与 origin 正确；unavailable 不编造画面。
- Prompt、trace、持久文件与异常中不得出现 URL、base64、文件路径或 caption 原文。
- 一次 repair 必须复用 exact MediaContext 和原始 transport；二次 repair 拒绝。

### 搜索、Meme 和权限

- AnySearch present/missing/interface changed/timeout；只有真实 sealed result 才允许“已查证”表述。
- Meme Manager present/missing/source drift/execution error；文字互补与 React-only 都只能执行一次 exact permit。
- 主动／冷场遍历 Manager 当前 22 类合同，验证全部可达、缺失/多重/未知/尾随标记 fail closed、标记零可见泄漏；100% 概率只在类别候选到达 Manager 后才有意义。
- 群友请求写文件、Shell、代码执行、浏览器或服务器时，工具在 Provider 前移除。
- 当前可信主人私聊中的 closed operation 语义才可形成 typed proposal；`/agent`、`/task` 等前缀没有快捷授权语义。all-off 配置应生成 canonical denial 并自然呈现，绝不执行。

### 输出、repair 和发送

- 注入 DSML、Meme XML/Python/JSON、hidden channel、裸参数、多节点拆分协议，确认 final bytes 无泄漏。
- 身份／关系漂移、无依据自传、伪媒体内容、伪 ActionOutcome 应阻断或只 repair 一次。
- 多气泡逐段发送；中间失败后未发段不能进入 SentReply 或反馈。
- 新消息在慢 Provider／tool／Meme／bubble 间到达，旧 generation 不得继续发送。
- 相似问题只改变一个数字、比较对象或否定范围时，旧答案不得因表面相似而复用；完全相同的问题允许再次得到相同答案。
- repair provider 抛异常、返回纯 reasoning、空 completion、协议正文和语义错误正文必须分别断言；不能统一只检查最终 `empty_visible_reply`。

### 重启、容量与性能

- runtime continuity 恢复 revision/cadence，不把新轮当旧轮，也不持久化原始 scope/sender。
- proactive quota、owner lifecycle、send receipt、learning activation/revoke 重启后保持 no-replay。
- inference active/waiter/queue/timeout 有界；active/pending 不因容量被驱逐。
- shutdown 后 scheduler、owned tasks 与 background calls 不遗留异常。

## X-01

`X-01` 必须单独记账：当前测试能证明 banned 消息在 Shio 内零消费／零提交，不能据此写成 LivingMemory 零存储。可选 O1 尚未执行；不要为了通过 Shio 测试临时修改第三方插件。

## 自动化

从插件父目录执行：

```powershell
python -X utf8 -m unittest discover -s astrbot_plugin_shio/tests -p "test_*.py"
python -X utf8 -m compileall -q astrbot_plugin_shio
python -X utf8 astrbot_plugin_shio/scripts/run_p10_fixed_matrix.py
python -X utf8 astrbot_plugin_shio/scripts/run_p10_persona_ab.py
python -X utf8 astrbot_plugin_shio/scripts/run_p10_plugin_multimodal.py
```

P10 候选必须同时经过 Windows 和无网络、候选目录只读挂载的 AstrBot Python 3.12 container。当前基线为 1,200+ 项；README、metadata、schema、CHANGELOG 和 public docs 另有 release-surface gate。

每个生产事故还必须按 [缺陷审查手册](REVIEW_PLAYBOOK.md) 保留原始失败、正确邻近、反向失败和至少一个变体，并检查 [坑位台账](PITFALL_LEDGER.md) 中的既有失败模式。单测、HTTP 200、容器 healthy 或插件 loaded 均不能代替真实消息与发送回执。

## 反馈材料

保留从 event 到 final send 的完整时间段，但只提交：closed stage/status、计数、不可逆 digest、插件版本和可复现步骤。删除 API Key、Cookie、Token、原始 Provider 请求体、真实账号／群号、聊天正文、内网路径和 LivingMemory 原文。
