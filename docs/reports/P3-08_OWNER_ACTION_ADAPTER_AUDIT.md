# P3-08：主人高权限 Action Adapter 与 ActionReceipt 只读审计

审计日期：2026-08-18（Asia/Hong_Kong）

## 1. 范围与结论

本报告只审计星汐当前共享工作树以及仓库内既有 AstrBot/插件盘点资料。本轮没有修改代码、测试、计划、配置或其他文档，没有运行 Git，没有访问或改变 FNOS，也没有写入真实 sender/group/message ID、聊天正文、工具参数、路径、凭据或完整工具结果。

仓库内工具数量、插件版本和线上插件清单来自之前的只读快照，不是本轮 live inventory。它们只能用于设计 adapter 候选和测试矩阵；任何能力在实际启用前仍须重新核对 candidate runtime 的 exact tool object、来源、版本、schema、上下文需求、返回类型和副作用。

审计结论：

1. 当前 typed 生产热路径能安全闭环的外部调用只有 AnySearch 公开取证。
2. 可信 owner 的 broad capability 合同已经存在，但高权限动作在生产中不可达。
3. 即使强行令 Planner 产出 owner `USE_TOOL`，主链仍会构造 AnySearch request shape，并把结果送往 `EvidenceOutcome -> GroundingFact`，不是副作用动作链。
4. P3-08 不能通过恢复完整工具箱、通用参数透传或模型工具循环完成。必须按 capability/operation 建立代码拥有的 exact adapter、确认与幂等策略、sealed executor 和 typed `ActionReceipt`。
5. 最终 Persona Renderer 必须继续保持零工具；Handoff、MCP、background、direct-send、旧 fast/shadow/legacy 路径不得恢复。
6. 在 adapter 不存在、配置未启用或 runtime conformance 不成立时，即使 sender 是可信 owner，也必须失败关闭。

## 2. 当前生产不可达点

### 2.1 Main 热路径没有提供 owner action suggestion

`main.py:2529-2549` 构造 `ContentIntentSeed`、`KnowledgeGapDecision` 并调用 `plan_action()`，但没有传入 `model_suggestion`。

`core/action_planner.py:213-228` 的 `_owner_tool_intent()` 只有收到高置信、闭集 broad capability suggestion 才会产生 owner tool intent；`core/action_planner.py:483-491` 才会将它写入 structural gate；`core/action_planner.py:609-630` 才可能形成 owner `USE_TOOL`。

因此当前生产调用中：

- 普通知识缺口可以进入公开取证；
- 非知识类 owner 高权限动作始终回落到普通 `REPLY`；
- Planner 合同存在不等于生产能力已启用。

### 2.2 Main 把所有 `USE_TOOL` 当成 AnySearch

`main.py:2641-2679` 对任何 `ActionKind.USE_TOOL` 都执行同一逻辑：

- 从当前消息寻找 URL；
- 有 URL 时构造 extract request；
- 否则构造 search request；
- 两者的 capability 都是 `PUBLIC_WEB_READ`。

所以即使 owner action 分支被强行触发，Shell、文件、记忆写入、媒体生成、设备控制或 Agent capability 都会与 request shape 冲突，无法形成正确的 typed action request。

### 2.3 Owner 的 configured names 不是 adapter 配置

`main.py:2644-2652` 对 owner 把当前全部 runtime tool name 当成 `configured_names`。这会把“工具存在”误当成“用户配置允许且已有安全 adapter”。

P3-08 必须改为：

- broad `CapabilityPolicy` 只说明主体是否有资格请求某类能力；
- adapter registry 决定该 operation 是否存在安全实现；
- 配置决定该 adapter 是否启用及其作用域；
- Tool Broker 只选择 adapter 固定的 exact tool；
- 四层同时通过才可执行。

### 2.4 当前 inventory 合并会隐藏同名冲突

`main.py:1154-1181` 合并 request tool set 与 AstrBot 全局工具集，并按名称保留第一个对象。这对 P3-08 不够安全：

- 同名不同来源工具可能被隐藏；
- request 内对象可能遮住 canonical manager 对象；
- 无法证明执行时仍是审计过的 exact object；
- 非 builtin 工具的 AstrBot 权限包装器可能被错误替换或解包。

Owner action executor 应只从 canonical tool manager 获取对象。可以按对象身份消除同一个对象的重复引用，但同名不同对象必须判定为 ambiguous 并失败关闭。

### 2.5 Generic capability shape 是禁止边界

`core/tool_broker.py:297-324` 的 `build_capability_request_shape()` 接受任意 scalar mapping。虽然它当前没有接入生产，但这正是 P3-08 禁止恢复的 generic parameter pass-through。

P3-08 不应由一个公共 API 接收任意参数字典。每个 adapter 必须拥有独立 typed parameter dataclass 和独立 builder；模型、Prompt、历史和调用方都不能直接指定工具参数名、默认值、exact tool、source、deadline 或预算。

### 2.6 Broker 只按 broad capability 选择 owner 工具

`core/tool_broker.py:517-557` 的 owner 分支在 configured names 中寻找同 capability 候选，并不绑定某个 adapter 的 exact tool、operation 和接口指纹。

P3-08 Broker 应接收 sealed `OwnerActionRequest`，其中只携带 adapter 已经签发的：

- closed adapter/operation identity；
- exact tool identity；
- source/interface attestation digest；
- code-owned normalized parameter digest；
- binding、deadline、call budget、confirmation proof 和 idempotency key。

Broker 不接受模型传来的 exact tool 或 argument mapping。

### 2.7 CapabilityPolicy 是 entitlement，不是执行 authority

`core/capability_policy.py:259-307` 对结构化验证通过的 owner 打开所有 broad capability，并将外部调用预算设为不限；`core/capability_policy.py:310-324` 对 owner 直接允许 active tool，不继续验证来源或副作用。

这个行为可用于表达“owner 有资格申请完整能力”，但不能直接授权 P3-08 执行。P3-08 每次 action 的实际预算仍必须固定为一次，并经过 adapter/source/schema/confirmation/idempotency/runtime type 全部硬门。

### 2.8 Sealed executor 当前只支持 AnySearch

`core/astrbot_tool_executor.py:22-28` 只登记 AnySearch 三个名称；`core/astrbot_tool_executor.py:335-384` 的 preflight 固定要求 `PUBLIC_WEB_READ` 与对应 request shape；`core/astrbot_tool_executor.py:418-458` 再次验证 exact name、来源、capability、副作用和 runtime 类型。

这是正确的公开取证边界，不应通过删除检查把它泛化。P3-08 应新增 action executor，或抽取共享的 sealed local-function kernel，再由每个 adapter 提供自己的严格 preflight。

`core/astrbot_tool_executor.py:36-147` 使用隔离 event proxy、阻断发送，并不向工具暴露完整插件 Context。很多 AstrBot builtin 可能依赖 Context、workspace、sandbox 或 booter，因此需要逐 adapter 审计并提供最小窄接口；不能把整个 live AstrBot Context 重新暴露给任意工具。

### 2.9 Grounding 不能承载动作效果

`core/grounding_adapter.py:319-465` 只接受公开网页或聊天检索型结果，并将安全引用转换为同轮 `GroundingFact`。文件写入、Shell、媒体生成、设备控制和 Agent delegation 都不应扩展进入这里。

`core/content_intent_builder.py:155-188` 的 `attach_grounding_facts()` 也只应接收已验证事实。副作用结果必须形成独立 `ActionReceipt/ActionOutput`，不能冒充公开事实、角色记忆或用户事实。

### 2.10 ReplyComposer 与 SemanticGuard 只有 EvidenceOutcome

`main.py:1758-1776` 的 `_activate_planned_reply_request()` 只接受 `EvidenceOutcome`；`core/reply_composer.py:217-237` 要求所有 `USE_TOOL` 都带证据结果；`main.py:1930-1936` 和 `core/semantic_guard.py:31-66` 也只绑定 EvidenceOutcome。

因此 P3-08 必须新增与 Evidence 完全分离的 receipt 输入和校验：

- `REPLY`：既无 EvidenceOutcome，也无 ActionReceipt；
- `USE_TOOL`：只能有 EvidenceOutcome；
- owner effect action：只能有 ActionReceipt；
- 两类结果同时出现、互相转换或 action result 进入 GroundingFact 都必须阻断。

建议新增 `ActionKind.EXECUTE_ACTION`，继续让 `USE_TOOL` 专指证据获取。若不增加枚举，也必须增加等价的 closed purpose 字段，不能靠 capability 字符串猜路线。

### 2.11 Final Renderer 的零工具不变量正确

`main.py:1982-1988` 在最终 Persona 请求前清空 `func_tool` 和 `tool_calls_result`。P3-08 必须保留此行为：

- planning/classification model 最多输出闭集软建议；
- Tool Broker 与 executor 完全由代码调用；
- Renderer 只获得当前语义骨架、人格表达信息，以及经代码裁决的 safe receipt summary；
- repair 也不得再次触发 action。

## 3. AstrBot 工具类型与静态能力快照

### 3.1 AstrBot 类型边界

仓库审计所对齐的 AstrBot 版本将工具区分为本地 `FunctionTool`、`HandoffTool` 和 `MCPTool`。本地 FunctionTool 还可能标记为 background，并可能通过 `MessageEventResult`、event result 或直接发送改变消息状态。第三方 FunctionTool 还可能由 AstrBot 权限包装器包裹。

P3-08 唯一可以进入 exact adapter executor 的底层对象必须同时满足：

1. 来自 canonical manager；
2. exact name 唯一；
3. active；
4. 是本地 FunctionTool 或 AstrBot 权限包装后的本地 FunctionTool；
5. 不是 HandoffTool；
6. 不是 MCPTool；
7. 不是 background task；
8. source、handler、schema 和 adapter 固定指纹一致；
9. AstrBot 权限包装器保持原样，不绕过或解包；
10. 返回一个有界结构化结果，不是 `MessageEventResult`、`None`、零结果、多结果或 direct-send。

Handoff、MCP、background 和 direct-send 在 P3-08 中保持硬拒绝，不因 owner 身份放宽。

### 3.2 静态数量快照不是 live inventory

`docs/TOOL_CAPABILITY_AUDIT.md:28-32` 记录的最后一次只读盘点为：

- AstrBot builtin 注册表：40；
- 已安装插件源码工具：10；
- 动态 MCP：数量不固定。

`docs/reports/P0R-03_ASTRBOT_PLUGIN_MULTIMODAL_INTEGRATION_AUDIT.md:23-39` 记录了当时的插件版本与职责，包括 LivingMemory、AnySearch、Meme Manager、ReNeBan、Parser、OmniDraw、QQ 管理和 restart。

这些数值与版本不是本轮实时验证结果。P3-08 报告和测试不得把它们写成“当前线上已经确认”；启用任何 adapter 前都要重新取得无敏感信息的 runtime descriptor，并进行 candidate conformance。

### 3.3 星汐静态工具分类目录

`core/capability_policy.py:354-457` 的 audited catalog 覆盖：

| Capability | 静态候选类型 | 当前 P3-08 结论 |
| --- | --- | --- |
| `public_web_read` | AnySearch、AstrBot Web Search、网页提取 | 现有 AnySearch 取证链继续使用；不是 owner action |
| `chat_retrieval` | 知识库、LivingMemory recall、群历史 | 需独立 scope/evidence 适配；P3-08 不扩权给 guest |
| `local_presentation` | Meme Manager | 由 P6 presentation 闭环处理 |
| `media_generation` | image/selfie/video | P3-08 首批不启用 |
| `memory_write` | LivingMemory memorize | 首批候选，须 exact conformance |
| `artifact_read` | file read、grep、download | file read/grep 为首批候选；download 继续拒绝 |
| `artifact_write` | write、edit、upload | 后续，当前失败关闭 |
| `shell_exec` | one-shot shell、session、Python、IPython | 只考虑 one-shot sandbox adapter；其余失败关闭 |
| `device_control` | CUA、Browser、future task、send-message、moderation | 后续，当前失败关闭 |
| `agent_full` | skill candidate/release/history workflow | 后续，当前失败关闭 |
| `unknown` | 未知动态工具和 alias | 永久默认失败关闭 |

该 catalog 是分类输入，不是运行时授权，也不是接口稳定性证明。

### 3.4 现有星汐配置缺口

`_conf_schema.json:57-83` 只有 owner 身份白名单、权限守卫和 guest exact tool 白名单；guest 默认只配置 AnySearch search/extract。当前没有：

- owner action 总开关；
- 逐 adapter 启用项；
- artifact roots；
- sandbox runtime 约束；
- confirmation TTL；
- action timeout/output cap；
- idempotency/receipt ledger 设置；
- adapter interface/version pin。

`owner_ids` 只能建立可信主体身份，不能同时充当 action allowlist。配置也不应新增 generic `owner_allowed_tools`；每个 adapter 应有独立开关和所需的最小结构化策略。

## 4. P3-08 目标架构

建议热路径为：

```text
Current inbound message + trusted PrincipalContext
  -> explicit owner-action semantic proposal
  -> typed ActionPlanner（broad capability + closed operation）
  -> code-owned parameter adapter
  -> exact Tool Broker（canonical object + source/schema attestation）
  -> optional pending confirmation
  -> atomic idempotency claim
  -> sealed local FunctionTool executor
  -> typed ActionReceipt / ActionOutput
  -> zero-tool Persona Renderer
  -> SemanticGuard / OutputValidator
  -> normal presentation and send receipt
```

Guest 路径保持：

```text
Current inbound message
  -> KnowledgeGapDecision
  -> configured exact AnySearch acquisition
  -> EvidenceOutcome / GroundingFact
  -> zero-tool Persona Renderer
```

两条结果路径必须互斥，不能把 action output 重新包装为 GroundingFact。

## 5. Code-owned parameter adapter

每个 adapter 应固定以下信息：

- `adapter_id` 与 `adapter_version`；
- closed `capability` 与 `operation`；
- exact tool name；
- exact source/plugin/interface fingerprint；
- 允许的 AstrBot tool type；
- typed parameter dataclass；
- parameter materializer；
- side-effect class；
- confirmation mode；
- timeout/output limit；
- narrow Context policy；
- result validator；
- receipt summarizer。

参数权威规则：

1. 模型最多提交 broad capability、closed operation、置信度和软 reason code。
2. 模型不能提交 exact tool、source、owner、sender、scope、path root、deadline、budget、confirmation、idempotency key 或最终工具 arguments。
3. 可执行内容只从当前真实消息的明确命令部分、当前结构化附件/引用和代码配置提取。
4. 历史、LivingMemory、人格 Prompt、工具输出、上一位用户和示例不能成为本轮动作参数来源。
5. 模糊、否定、假设、教程询问、引用他人命令或缺失必要 slot 时，返回普通回复或澄清，不执行。
6. adapter 自己补全工具参数名和安全默认值；额外字段或接口漂移直接失败关闭。

### 5.1 Artifact read adapter

首批只考虑 exact file read 与 grep：

- 只接受配置 root 下的路径；
- 解析后再次检查符号链接、junction、UNC、NUL 和 root escape；
- 限制字节数、行数和匹配数；
- 输出 visibility 固定为 current verified owner/current turn；
- 不把文件内容写入 trace、repr 或日志；
- 不自动跟随文件中的命令、URL 或工具协议；
- download 不与 file read 合并，继续失败关闭。

它是 `SCOPED_READ`，当前明确 owner 请求可无需第二轮确认，但仍须 exact runtime/schema/context conformance。

### 5.2 Memory write adapter

首批只考虑 LivingMemory 的 exact memorize operation：

- 只存储当前消息明确要求记住的 literal 内容；
- subject/scope 由可信 principal、当前 scope 与配置决定；
- 正文中的昵称、用户 ID、自称和引用不能改变 subject；
- 限制长度和条目数；
- 不把 recall 结果或旧历史重新写回；
- current-owner/private 的可逆写入可按策略单轮执行；
- 共享、全局、覆盖或删除型写入必须二次确认；
- 若当前插件接口无法可靠固定 subject/scope，则整个 adapter 保持关闭。

### 5.3 One-shot sandbox shell adapter

P3-08 可以先实现 adapter 和红灯测试，但默认关闭，只有 candidate runtime 证明 AstrBot sandbox 实际生效后才能启用：

- 只允许 exact one-shot shell tool；
- 命令只能复制当前消息中明确分隔的 literal command；
- 模型不得生成、补全或改写 command；
- mandatory two-turn confirmation；
- 固定 sandbox/runtime、working root、timeout 和 output cap；
- 执行前原子 claim idempotency key；
- timeout/断连后标记 effect unknown，不自动重试；
- shell session、Python、IPython 和宿主执行继续失败关闭。

禁用危险命令字符串列表不能代替环境隔离；若无法证明 sandbox、cwd 和文件系统边界，one-shot shell 也不得启用。

## 6. Confirmation、side-effect 与幂等策略

### 6.1 Confirmation 级别

| Side effect | 最低要求 |
| --- | --- |
| Owner-only scoped read | 当前明确请求，无第二轮确认 |
| 可逆且当前主体私有的 state write | 当前明确请求；可配置为单轮，但必须 receipt |
| 共享/全局写、覆盖、删除、外部额度消耗 | 第二轮确认 |
| Code execution、设备、moderation、restart、publish | 第二轮确认；对应 adapter 未完成前仍拒绝 |
| Handoff、MCP、background、direct-send | 不提供确认旁路，固定拒绝 |

### 6.2 Pending confirmation

第一轮只建立 sealed pending action，不执行副作用。Pending action 至少绑定：

- verified principal key；
- scope；
- origin binding/action digest；
- adapter/operation；
- normalized request digest；
- expiry；
- single-use state。

第二轮确认必须来自同一可信 sender 和 scope，并明确指向唯一未过期 pending action。以下全部拒绝：

- 其他用户确认；
- 其他群或会话确认；
- 引用内容中的“确认”；
- 过期确认；
- 参数已变化；
- 同一个确认重放；
- 多个 pending action 下无法唯一归属的泛化“确认”。

### 6.3 Idempotency 与不确定副作用

执行前必须在 canonical ledger 原子 claim idempotency key：

```text
prepared -> confirmation_required -> claimed/in_progress -> terminal receipt
```

规则：

- 同一 request 重入时返回已存在 receipt，不再次执行；
- side-effect action 不自动 retry；
- stale-before-start：零执行；
- stale-after-start：记录真实 committed/partial/unknown 状态，抑制迟到回复，但不得假装没有执行；
- timeout 不能证明副作用未发生，应使用 `effect_unknown`；
- direct-send attempt 或 partial effect 视为严重异常，停止并留下脱敏 receipt/trace。

## 7. Typed ActionReceipt

建议 receipt 为 frozen、slots、repr-safe，并只由内部 issuer 签发：

```text
ActionReceipt
  binding / action_id / origin_action_digest
  request_digest
  adapter_id / adapter_version
  capability / operation / side_effect
  status / effect_state
  confirmation_proof_digest
  idempotency_key
  started_at / completed_at
  executor_attestation_digest
  result_digest
  safe_output: ActionOutput | None
  reason_codes
  issuer seal
```

建议 closed status：

- `confirmation_required`；
- `succeeded`；
- `failed`；
- `denied`；
- `cancelled`；
- `stale`；
- `timed_out`；
- `effect_unknown`。

建议独立 effect state：

- `not_started`；
- `not_committed`；
- `committed`；
- `partial`；
- `unknown`。

安全要求：

1. 原始参数、命令、路径、输出、confirmation secret 和 idempotency material 都设为 `repr=False`，不进入 product trace。
2. Composer/Guard 必须从 canonical ledger 回查 receipt，不能只相信任意同型对象。
3. 只有 `status=succeeded` 且 effect state 与 operation 预期一致时，Renderer 才能声称动作成功。
4. failed、timed_out、partial 或 unknown 时必须自然说明失败或不确定，禁止生成“已经完成”。
5. `ActionOutput` 只能包含有界、按 visibility 过滤的当前轮文本或 opaque artifact/media ref。
6. Receipt 和 ActionOutput 不能进入 `attach_grounding_facts()`，不能被 LivingMemory 当作用户事实或角色人格素材。

## 8. P3-08 首批与继续失败关闭边界

### 8.1 首批实现候选

首批优先级：

1. Action contracts、adapter registry、全关闭配置；
2. confirmation/idempotency ledger 与 canonical receipt issuer；
3. exact local FunctionTool executor；
4. artifact read/grep adapter；
5. scoped LivingMemory memorize adapter；
6. one-shot sandbox shell adapter 与测试，但默认禁用；
7. Main/Composer/SemanticGuard 原子接线。

即使完成代码，任何 adapter 在 candidate conformance 前仍保持配置关闭。首批目标不是一次开放“owner 全工具”，而是证明逐 adapter 增量启用不会恢复工具循环和 generic pass-through。

### 8.2 继续失败关闭

以下能力不属于首批可启用范围：

- artifact write/edit/upload/download；
- generate image/selfie/video；
- shell session、Python、IPython、宿主 Shell；
- CUA、Browser、future task；
- send-message 与任何 direct-send；
- QQ moderation、ban、restart；
- Docker、NAS、系统、设备控制；
- skill candidate/release、publish、rollback、完整 Agent delegation；
- HandoffTool；
- MCPTool 与动态 MCP alias；
- background/auto-push 工具；
- unknown、source unattested、duplicate exact name、schema drift 工具。

媒体生成功能后续只有在 adapter 能强制返回结构化结果、不直接发送，并与 P6 PresentationReceipt 闭环时才可重新评估。视频 background/auto-push 继续拒绝。

## 9. 最小文件边界

建议新增：

- `core/contracts/owner_action.py`：semantic proposal、sealed request、pending confirmation、ActionReceipt/ActionOutput；
- `core/owner_action_adapters.py`：闭集 registry 与逐 operation 参数适配器；
- `core/owner_action_executor.py`：canonical exact selection、runtime type guard、confirmation/idempotency ledger、sealed execution、receipt issuer。

建议修改：

- `core/contracts/behavior.py`：增加 `EXECUTE_ACTION` 或等价 closed purpose；
- `core/action_planner.py`：只接 broad capability/operation soft proposal；
- `core/tool_broker.py`：owner 按 exact adapter 选择，禁止 public generic argument builder；
- `core/capability_policy.py`：保持 broad entitlement，与 adapter authorization 分离；
- `main.py`：Evidence 与 Action 两条互斥分支；
- `core/reply_composer.py`：单独接收 safe receipt；
- `core/semantic_guard.py`、`core/output_validator_v2.py`：校验 receipt 与成功声明；
- `core/product_trace.py`：增加脱敏 `ACTION_RECEIPT` stage；
- `_conf_schema.json`：逐 adapter 开关和最小策略，不增加 generic owner tool list。

最终 Renderer 的 `ToolSet([])`、旧 tool result 清理和一次 repair 上限必须保持不变。

## 10. 依赖顺序

1. 完成并冻结 P3-07 语义/媒体守卫，避免并发修改同一 Composer/Guard 热点。
2. 先新增 authority、explicitness、adapter、confirmation、idempotency、receipt 红灯测试。
3. 建立 owner action contracts 与全关闭 adapter registry。
4. 建立 confirmation/idempotency ledger 和 canonical receipt issuer。
5. 接入 broad semantic proposal；模型仍不可决定 exact tool/args。
6. 修改 Tool Broker，使 owner action 只按 adapter 固定 exact tool/source/schema 选择。
7. 建 sealed local FunctionTool executor，并保留 Handoff/MCP/background/direct-send 硬拒绝。
8. 按 artifact read、memory write、sandbox shell 顺序逐个实现 adapter。
9. 原子接入 Main、ReplyComposer、SemanticGuard、OutputValidator 与 ProductTrace。
10. 运行定向测试、完整测试、compile/whitespace 检查。
11. 部署阶段另行做 candidate runtime conformance；任何不匹配都停止部署。

## 11. 红灯矩阵

### 11.1 Authority 与当前主体

- guest 请求任意高权限 capability：零 action；
- 昵称、自称、群名片、引用内容冒充 owner：零 action；
- missing sender、identity degraded：零 action；
- ambient/proactive/quiet 轮次：零 owner action；
- 上一位 owner 的 pending/action 被下一位用户继承：阻断；
- 当前 sender、scope、message、content、revision、epoch 任一错绑：阻断。

### 11.2 Explicitness 与参数来源

- 假设、教程、讨论、引用命令、否定命令：零执行；
- 历史或 LivingMemory 中出现动作文本：零执行；
- 当前请求缺少必要 slot：澄清或普通回复；
- 模型提交 tool/source/args/owner/scope/deadline/budget：拒绝；
- 模型补全或改写 Shell command：拒绝；
- 旧工具结果作为新参数：拒绝。

### 11.3 Adapter、source 与 runtime object

- adapter missing/disabled：拒绝；
- exact tool missing/inactive：拒绝；
- 同名不同对象：ambiguous，拒绝；
- request tool 遮住 canonical object：拒绝；
- source clone、alias、handler 改变：拒绝；
- schema required/property/type/default 改变：拒绝；
- AstrBot permission wrapper 被绕过或解包：拒绝；
- Handoff、MCP、background：拒绝。

### 11.4 参数硬化

- 路径 traversal、symlink/junction escape、UNC、NUL、root escape：拒绝；
- 超长内容、超大读取、超多匹配：拒绝；
- 工具 schema 中出现 adapter 未声明的新字段：拒绝；
- 参数带隐藏 owner/source/scope/confirmation 字段：拒绝；
- 共享/全局写未确认：拒绝；
- sandbox/cwd/timeout 无法证明：Shell 拒绝。

### 11.5 Confirmation 与重放

- 其他 sender 或其他 scope 确认：拒绝；
- 引用里的确认：拒绝；
- 过期确认：拒绝；
- request digest 已改变：拒绝；
- 多个 pending action 下泛化确认：拒绝；
- 同一 confirmation 重放：不再执行；
- pending action 被取消后确认：拒绝。

### 11.6 执行与副作用

- event stopped 或 stale-before-start：零执行；
- timeout/异常：不自动 retry；
- stale-after-start：记录 committed/partial/unknown，禁止再次执行；
- 零结果、多结果、`None`、`MessageEventResult`：拒绝；
- event/send side channel 被触发：direct-send violation；
- 输出超过上限：截断或拒绝，不记录原文；
- partial/unknown effect 被描述成未执行或成功：阻断。

### 11.7 Receipt、Renderer 与修复

- forged receipt 或 issuer seal 无效：阻断；
- receipt binding/action/request digest 不匹配：阻断；
- receipt 被转换为 GroundingFact：阻断；
- failure/timeout/unknown 回复声称“已经完成”：阻断；
- 原参数、路径、命令、输出、令牌进入 repr/trace/log：阻断；
- Renderer 获得非空 func tools：阻断；
- Renderer 收到旧 `tool_calls_result`：阻断；
- repair 再次规划或执行 action：阻断；
- 多气泡改变 receipt 状态或对象：阻断。

### 11.8 New-only 架构

- 恢复 generic parameter pass-through：阻断；
- 模型直接选择 exact tool 或 arguments：阻断；
- 恢复不受控 Agent tool loop：阻断；
- 恢复 Handoff/background/direct-send：阻断；
- 恢复旧 fast/shadow/legacy reply fallback：阻断；
- action 失败后退回旧链执行：阻断。

## 12. 报告与实施验收口径

P3-08 后续每个 adapter 的实施报告至少要记录：

- adapter/operation 与副作用类别；
- 使用的静态源码依据和 candidate runtime conformance 状态；
- exact object/source/schema 是否唯一并匹配；
- confirmation、idempotency、timeout、output 与 narrow Context 策略；
- 定向与完整测试结果；
- adapter 是否仍配置关闭；
- 未支持能力的 fail-closed 列表；
- 不含真实 ID、聊天正文、工具参数、路径、凭据或原始结果。

只有以下条件全部成立，才能把某个 adapter 标为可部署：

1. 合同与红灯测试先失败后通过；
2. 完整测试通过；
3. candidate runtime conformance 通过；
4. exact adapter 配置明确启用；
5. 无 legacy/Handoff/background/direct-send 旁路；
6. Persona Renderer 仍为零工具；
7. action result 只进入 typed ActionReceipt，不进入 GroundingFact。

在首个 adapter 达到上述条件前，P3-08 状态应写作“审计完成、实现待执行”，不能写成“主人完整 Agent 已生产可用”。
