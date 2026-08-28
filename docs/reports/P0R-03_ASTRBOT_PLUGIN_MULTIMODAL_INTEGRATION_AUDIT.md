# P0R-03：AstrBot 插件生态、消息准入与多模态集成审计

## 1. 审计范围与结论

本报告只读核对了 FNOS `192.168.50.38` 上 `astrbot` 容器、AstrBot 4.27.2、线上插件目录以及星汐 typed-only 热路径。审计期间没有修改、重启或部署任何线上服务。

健康基线：

- `astrbot` 容器运行中；只读检查时 CPU 和内存无异常。
- WebUI `http://192.168.50.38:6185/` 返回 HTTP 200。
- 当前问题不是 AstrBot 缺少图片理解，也不只是星汐 Prompt；核心缺口是输入准入、插件来源和多模态来源没有进入同一套 typed 合同。

采用结论：

1. LivingMemory、AnySearch、Meme Manager 继续分别承担记忆、联网资料和表情执行，不在星汐内部复制它们。
2. ReNeBan 继续作为黑名单事实来源；星汐必须在任何自身状态写入前获得准入结果，不能只依赖最终不回复。
3. 自身消息、已登记的其他机器人和插件自动输出必须按可信 sender/source 隔离，不能按昵称或正文猜测。
4. 图片采用 AstrBot 原生多模态链；星汐新增 `MediaContext` 绑定直接/引用来源，不默认另建第二套 VLM。
5. 第三方插件工具/输出必须先归一化为 typed evidence/effect，不能把工具参数、候选、进度文案或自动回复当作用户、角色事实或人格素材。

## 2. 生产插件清单与正式角色

| 插件 | 线上版本 | 正式角色 | 星汐集成边界 |
|---|---:|---|---|
| `astrbot_plugin_livingmemory` | 2.5.7 | 近期历史、长期记忆、自动召回 | 每个合格人类轮次经过 `MemoryDecision`；按 sender/scope/ban/bot/source 过滤；召回不能覆盖当前消息 |
| `astrbot_plugin_anysearch` | 0.3 | 公开联网资料 | `anysearch_search/extract/batch_search` 归为 `public_web_read`；群友可按知识缺口使用 |
| `meme_manager` | 4.15.1 | 表情检索与实际发送 | `search_memes` 仅是 `PresentationEffect`；query、候选 ID、caption、tags 和工具结果不进入后续上下文 |
| `astrbot_plugin_reneban` | 1.2.0 | 黑名单准入事实源 | ban 命中后星汐零 ledger、零记忆消费、零情绪/学习、零工具、零回复；不复制黑名单文件 |
| `astrbot_plugin_parser` | 1.5.1 | 链接/视频解析和媒体直发 | Parser 输出不作为角色发言回灌；当前机器人 self echo 必须被忽略 |
| `Group_Verification_PRO` | 2.4.0 | 入群验证 gate | stop 结果优先于星汐；待验证消息不进入星汐状态 |
| `recall_cancel` | 2.1.3 | 撤回/取消 gate | 撤回后取消旧 generation、工具和待发气泡，不保存半截回复 |
| `keywords_reply` | 1.4.1 | 自动关键词回复 | 自动输出不成为人格历史；需要单响应者归属，避免重复回答 |
| `iamthinking` | 0.1.0 | 思考/完成反应呈现 | 仅作为平台反应，不作为文本、事实或记忆 |
| `astrbot_plugin_omnidraw` | 3.3.23 | 生图/自拍视频/视频动作 | 作为有副作用媒体能力授权；群友默认不可用，主人按策略使用 |
| `astrbot_plugin_qqadmin` | 3.3.0 | QQ 管理 | 高风险管理能力，仅可信主人按策略使用 |
| `astrbot_plugin_restart` | 1.1.1 | 重启/插件重载 | 高风险管理能力，仅可信主人按策略使用 |
| `astrbot_plugin_steam_price_heybox` | 1.2.2 | 显式命令查询 | 命令独立共存；自然聊天资料默认仍走已审计只读工具 |

未知或未来插件不自动获得上下文注入权。工具按实现来源、能力和副作用分类；自动输出默认标为 `external_plugin_output`，除非存在明确的当轮 typed 合同。

## 3. 当前实际生命周期

### 3.1 ReNeBan 与 LivingMemory 的时序差

ReNeBan 的消息 handler 优先级是 114，命中后调用 `event.stop_event()`：

- `/AstrBot/data/plugins/astrbot_plugin_reneban/main.py:1096-1103`
- AstrBot handler 按优先级降序：`/AstrBot/astrbot/core/star/star_handler.py:19-26`
- stopped event 在后续 handler 前中止：`/AstrBot/astrbot/core/pipeline/process_stage/method/star_request.py:36-52`

因此当前能保证：被 ban 用户不会触发星汐最终回复、工具或 Parser。

但 WakingCheck 会先统一求值插件过滤器；LivingMemory 的 `PassiveGroupCaptureFilter` 在这个阶段已经异步安排存储，ReNeBan 要到随后的 StarRequest 才 stop：

- `/AstrBot/astrbot/core/pipeline/waking_check/stage.py:167-236`
- `/AstrBot/data/plugins/astrbot_plugin_livingmemory/core/passive_group_capture.py:120-144`

所以当前不能宣称“被 ban 消息零 LivingMemory 写入”。在星汐 P1～P10 主线不修改第三方插件的边界下，本项目采用两层口径：

1. 星汐硬保证：被 ban 消息不进入星汐 ledger、Scene、MemoryContext、Affect、Learning、Tool 或 Reply；LivingMemory 中已经存在的相应记录也不被星汐消费。
2. 生态严格保证：零 LivingMemory 存储需要 AstrBot/插件共享更早的 `AdmissionDecision`，或调整 LivingMemory 被动捕获入口；这是外部生命周期缺口 `X-01`，不能冒充星汐单独完成，也不阻塞 P1～P10。

本次审计时 ReNeBan 没有公开服务或 HTTP/LLM API，内部查询函数是 `EventUtils.is_banned(...)`（`event_utils.py:42-97`）。可选 O1 实现时必须先更新并重新审计届时最新稳定版；优先使用届时公开合同，无公开接口时只能通过一个隔离的、版本化兼容适配器读取当前插件实例，禁止散落私有导入或直接解析其数据文件。ReNeBan 存在但接口异常时，LivingMemory 捕获应 fail-closed 并留下脱敏诊断，不能悄悄写入。

### 3.2 自身、其他机器人和 Parser

AstrBot 当前 `ignore_bot_self_message=true`，Parser 与其他插件通过当前机器人账号 `event.send()` 的输出不会作为新入站重新进入插件链。Parser 的普通直发也不走 Agent conversation 保存路径。

这只能解决 self echo，不能识别其他 QQ 机器人。AstrBot 事件没有可靠的通用 `is_bot` 标志，因此必须建立：

```text
TrustedBotRegistry
- 当前 AstrBot 各适配器 self ID（自动）
- 用户明确配置的其他机器人 platform + sender ID
- 可选适配器提供的可信 bot 标志
```

昵称、群名片、发言风格和“我是机器人”不能用于判定。命中 registry 的消息在任何星汐状态写入前丢弃，并在 LivingMemory 读取时再次过滤。

星汐自己的自然回复可以作为连续对话的 assistant 历史；Parser、关键词回复、生图进度、管理状态和未知插件输出不能伪装成星汐人格回复。assistant 历史只有能由星汐 send receipt/target/source 证明的内容才进入角色线程，其余按 `external_plugin_output` 排除。

## 4. 记忆、搜索和表情合同

### 4.1 LivingMemory：每轮经过策略，不重复盲查

线上 LivingMemory 已在每次 LLM 请求自动召回，当前 `top_k=5`，并以临时 `extra_user_content` 注入。星汐还会在群聊直接轮次读取其带 sender 的近期历史（`main.py:429-520`）。

正式合同：

```text
MemoryDecision
- recent_context: 每个合格人类轮次都执行作用域筛选
- semantic_recall: 当前问题需要长期事实或明确回忆时使用
- no_recall: 无相关记忆也可自然回复
- degraded: 插件缺失、超时或 provenance 不足
```

“每轮调用记忆”落实为每轮都经过同一策略与来源筛选，不再额外重复执行两次相同语义检索。群聊、私聊都必须覆盖；banned、known bot、其他用户私有、无主体低相关和插件自动输出不得成为当前用户事实。

### 4.2 AnySearch：先识别知识缺口，再取证再口语化

正式工具：

- `anysearch_search`
- `anysearch_extract`
- `anysearch_batch_search`

Action Planner 将“是否需要外部证据”和“当前用户是否获准联网”分开：陌生网络语、时效资料、明确查证、专有概念歧义和低置信事实可触发公开只读搜索；普通常识和纯情感聊天不应机械搜索。

搜索结果先转换为带来源的 `GroundingFact`，再形成 `ContentIntent`，最后由 Persona Renderer 自然口语化。失败/超时应诚实说明，禁止粘贴原始 JSON、搜索协议或假装已经查到。

### 4.3 Meme Manager：唯一表情执行器

星汐只输出情绪、社交动作和 `ExpressionIntent`；Meme Manager 的 `search_memes` 是唯一实际检索/发送器。现有未消费的 `PresentationHandoff` 必须选择“接入该执行器”或“删除冗余路径”，不能与第三方语义 Tool 双调用、双发。

表情结果只影响本轮呈现和 send receipt，不成为用户记忆、角色事实、学习台词或下一轮工具上下文。

## 5. 图片与引用图片的采用方案

### 5.1 AstrBot 原生能力已经存在

AstrBot 4.27.2 已实现：

- 直接 `Image` 组件解析：`/AstrBot/astrbot/core/message/components.py:501-558`
- QQ 引用原消息和 `Reply.chain`：`aiocqhttp_platform_adapter.py:303-339`
- 直接图片进入 `ProviderRequest.image_urls`：`astr_main_agent.py:1420-1433`
- 引用图片进入同一 `image_urls` 并带 quoted 标记：`astr_main_agent.py:1448-1470`
- 只有 Reply ID 时通过 `get_msg/get_forward_msg` 回退：`astr_main_agent.py:1489-1526`
- 引用图片 URL/文件解析：`quoted_message/extractor.py:89-146,181-210` 与 `image_resolver.py:73-129`
- Provider 转为标准多模态内容块：`openai_source.py:1367-1434`

当前默认聊天 Provider 声明支持 image，且中文图片转述 Provider 已配置。因此不建立默认第二 VLM。

### 5.2 星汐当前破坏点

星汐目前保留 `req.image_urls`，但只把总数量交给 ReplyComposer：

- `main.py:1087-1112,1197-1198`
- `core/reply_composer.py:196-221`

随后 `main.py:1150-1155` 清空 `req.extra_user_content_parts`，会删除 AstrBot 已准备的引用正文、直接/引用图片标记和非视觉 Provider 使用的 `<image_caption>`。`ReferenceContext` 也没有媒体字段，严重输出修复在 `main.py:1713-1721` 又固定传 `image_urls=[]`。

因此现有“图片 URL 未被删除”测试只能证明传输还在，不能证明模型理解了图片与当前问题/引用对象的关系。

### 5.3 新 typed 媒体合同

```text
MediaContext
├─ item_id
├─ kind: image | audio
├─ origin: direct | quoted | quoted_fallback
├─ provider_index
├─ source_message_id
├─ source_sender_key
├─ visibility: raw_media | native_caption | unavailable
└─ safe_caption（可选，带来源且不持久化 URL/base64/path）
```

流程：

```text
MessageChain / Reply.chain / ProviderRequest
→ AstrBotMediaAdapter
→ MediaContext + 原生 image_urls
→ ContentIntent
→ Persona Renderer
→ Validator / Repair（继续携带同一媒体证据）
```

直接图片、引用图片和 Reply ID 回退都由 AstrBot 原生解析；星汐只做来源绑定。图片不可用时自然说明并请求重发，不能编造。URL、本地路径和 base64 不进入日志、持久化记忆或普通 Prompt 文本。

## 6. 新能力与阶段影响

新增能力：

- `C-28 Ingress Eligibility / Loop Prevention`
- `C-29 External Plugin Contract / Source Provenance`
- `C-30 Multimodal & Quoted Media Understanding`
- `C-31 Knowledge Gap & Evidence Acquisition`

强化能力：

- `C-12 LivingMemory-backed Per-turn Memory Policy`
- `C-18 ExpressionIntent ↔ Meme Manager Closed Loop`
- `C-25` trace 增加 ingress、sender kind、ban、memory decision、media availability、plugin source、tool evidence 和 presentation receipt。

P1 必须先落成 `IngressDecision`、`SenderKind`、`ExternalPluginEvidence`、`MemoryDecision`、`KnowledgeGapDecision`、`MediaContext` 和 `PresentationReceipt` 合同与插件缺失/超时/顺序矩阵；P2 再实现准入、来源、记忆和多模态地基；P3 实现搜索取证；P6 闭合 Meme Manager；P10 做完整插件共存和真实直接/引用图片验收。

## 7. 必须失败后再修复的验收样例

1. 被 ReNeBan ban 的用户称名：星汐零 ledger、零记忆消费、零情绪/反馈、零工具、零回复；P1～P10 单独报告外部 LivingMemory 被动存储状态，O1 激活后再把零排队/摘要/落库设为独立硬验收。
2. 已登记其他机器人发言：不唤醒、不进入 Scene/Memory、不改变情绪、不触发工具或主动会话。
3. Parser/self echo：不进入人物线程、公共场景、记忆事实或学习；星汐自然回复本身仍可保留为 assistant 连续上下文。
4. 群友询问陌生/时效概念：AnySearch 自动取证一次，最终以角色口吻自然解释，无工具协议。
5. 表情：`search_memes` 最多一次；query/候选/ID 不可见且不进入下一轮。
6. 直接图片、仅图片、引用图片、仅引用图片、多图、文本 Provider caption、Reply ID 回退均能绑定正确当前用户和引用对象。
7. 图片失效或 Provider 无视觉能力时不编造；一次修复仍使用原图或可信 native caption。
8. Group Verification、ReNeBan、撤回 gate 的 stop 决定不可被星汐后续发送绕过。
9. AnySearch、Meme、OmniDraw、QQAdmin 等历史工具消息按能力和来源归一化，不能混成角色台词或用户事实。

## 8. 修改边界和后续入口

- 本审计阶段及星汐 P1～P10 不修改 LivingMemory、Meme Manager、AnySearch、ReNeBan、Parser、AstrBot 核心或其他插件。
- 不在本阶段改星汐运行代码或线上配置。
- P1-01 从完整 typed 合同与失败 fixture 开始；在合同完成前不再用 Prompt/正则修单个截图。
- `X-01`（ReNeBan 晚于 LivingMemory 被动捕获）保留为显式生态缺口；星汐不得把“零消费”写成“零存储”，也不得因此暂停主线。
- 总计划在 P10 后新增可选 O1。P10 完成时先提醒用户，未经确认不自动启动。
- O1 的固定顺序是：按执行日核对最新稳定版与贡献规则 → 备份并在隔离环境验证 → 先更新 LivingMemory 基线/线上版本 → 在 LivingMemory 捕获排队前实现可选 ReNeBan 准入 → 跑缺失/禁用/异常/ban 类型/正常捕获完整矩阵 → 容器真实验证 → 整理最小上游 PR。
- O1 不修改星汐主线语义，也不复制 ReNeBan 黑名单；PR 不能包含原始群聊、真实用户 ID、密钥或无关插件改动。
