# P3-06 唯一 typed 原子热路径报告

## 结论

P3-06 已完成生产热路径切换。星汐现在只有一条回复语义链：

`Admission → Address/Attention/Participation → ContentIntent → KnowledgeGap → Action → ToolBroker → sealed acquisition → GroundingFact → Affect/Persona/Expression → final renderer → guard/send`

旧 `LocalChatPlan/fast_path`、旧 Planner 兼容入口、shadow/legacy fallback 和“等待 AstrBot 工具循环再次进入星汐”的 continuation 均已移除。普通直答只生成一次最终回复；需要公开联网取证时，先由代码执行唯一密封 AnySearch，再把同轮证据加入 `ContentIntent`，最后由无工具的 Persona Renderer 生成一次自然回复。

## 结构性修复

### 1. 行为、内容、取证与表达分离

- `PlannedAction` 只决定 `REPLY / USE_TOOL / REACT / WAIT / NO_ACTION`，不写角色台词。
- `ContentIntent` 固定当前问题、目标、必答语义、语言、媒体和同轮证据；历史与记忆不能改写当前焦点。
- `KnowledgeGapDecision` 只判断是否需要公开查证，不选择工具也不授予权限。
- `ToolBroker` 在可信 Principal、CapabilityPolicy、插件来源、运行状态和配置 allowlist 全部一致后，才选择唯一 exact tool。
- Affect、Persona 与 Expression 只在最终内容和证据确定后运行；最终 Renderer 不再决定是否调用工具。

### 2. AstrBot 工具循环不再承担星汐 continuation

AstrBot 的 `on_llm_request` 只在初始 Agent 请求前触发；模型内部工具循环不会重新进入星汐。因此旧 `req.tool_calls_result` 分支并不是可靠的生产第二阶段。本次切换删除了该假设：旧结果固定清空，当前轮是否取证完全由星汐代码控制。

### 3. 密封公开取证

`core/astrbot_tool_executor.py` 当前只接受由 P3-04 代码签发的 AnySearch `search / extract / batch_search` 请求，并保持以下不变量：

- 执行 AstrBot 权限包装后的原工具对象，不 unwrap 权限守卫；
- 在执行前严格验证 exact name、attested source、capability、参数 schema、参数摘要、deadline、event stop 和 generation epoch；
- 生产取证不调用可接触 exact tool/event 的通用 `on_tool_start`；测试注入 hook 也只能看到隔离上下文与参数副本，hook 后重新从 frozen request 生成并验证执行参数；
- `FunctionToolExecutor` 只能得到不持有真实 event/context 的 `_SealedEventProxy`；任何 `send/send_*` 尝试在发生真实发送前抛错，即使工具捕获异常后返回合法结果，也会因 `send_attempted` 失败关闭；
- Handoff、后台任务、MCP、空结果、多结果、错误结果和超时全部失败关闭；
- 成功输出只有一个代码生成 call ID、一个 call 和一个 result，随后必须通过 P3-05 action/binding/source/argument attestation 才能成为 `GroundingFact`。

`extract` 还在 ToolBroker 侧先做 URL 边界校验：IDNA 归一化后再次去除尾点，并拒绝 localhost/localdomain、常见本地域、私网、回环、链路本地、组播、保留地址以及十进制/十六进制混淆主机。DNS 重绑定与远端重定向属于 AnySearch 供应商执行合同，P10 必须用线上实际插件做 conformance，不能由本地字符串校验冒充已闭合。

工具参数、URL、原始 JSON、协议 envelope 和 Token 不进入 Persona prompt、产品 trace、历史、学习或最终回复。

### 4. 最终 Renderer 零工具

无论普通回复还是联网回复，最终 `ProviderRequest.func_tool` 都是空 `ToolSet`，`tool_calls_result` 固定为 `None`。这阻止 Persona 模型绕过 Action/Policy 自选工具，也避免工具协议再次混入口语化输出。

预算固定为：

| Action | acquisition | final model | visible send |
|---|---:|---:|---:|
| `REPLY` | 0 | 1 | 1 |
| `USE_TOOL` | 1 | 1 | 1 |
| `REACT / WAIT / NO_ACTION` | 0 | 0 | 0 |

单次 repair 由独立守卫预算控制，不构成第二条回复链。

### 5. 发送前协议防泄漏

末端 guard 不再读取已删除的 payload `tool_names`，也不依赖最终 Renderer 的空工具集。它只使用代码拥有的闭集：Meme presentation executor、密封 acquisition 支持集，以及同轮 typed `AcquisitionRequest` 的 exact tool name。因而可以在工具未实际执行时也阻断模型幻觉出的同行/跨行 JSON、括号调用、动态 XML、独占裸工具名、`name:` 节点与图片节点后残留的孤立参数片段，同时保留普通技术讨论和内联代码。专项证据见 `P3-06B_RESPONSE_PROTOCOL_GUARD.md`。

## 删除与替换

- 物理删除 `core/fast_path.py` 与 `tests/test_fast_path.py`；
- `core/reply_composer.py` 改为直接消费 Action、ContentIntent、Affect、Persona 和 Expression；
- `core/presentation_handoff.py` 不再接受 LocalChatPlan；
- `core/runtime_invariant.py` 改为按 Action 派生调用/发送预算；
- `main.py` 不保留旧 Planner、旧 typed tool result continuation、shadow 或 fallback；
- 生产源码精确扫描无 `SHIO_LOCAL_CHAT_PLAN`、`SHIO_TYPED_TOOL_RESULTS`、`SpeechPlanV2`、`local_fast_path` 或旧 continuation 命中。

## 验证证据

- sealed executor：`18/18`；
- ToolBroker/ToolResult/Grounding/Executor 相关：`50/50`；
- response protocol guard：`49/49`；
- 上述安全链联合：`99/99`；
- atomic hot path：`8/8`；
- production pipeline：`68/68`；
- atomic + pipeline：`76/76`；
- P3-07 首轮接线后的完整快照：`659/659`；其后独立审计又发现 P3-07 尚未覆盖的合同替换与误杀边界，因此本数值只证明 P3-06 未回归，不代表 P3-07 已验收；
- `compileall` 通过；
- 仓库内 `git diff --check` 通过；
- 未执行 Git/GitHub 写操作，未部署 FNOS。

## 明确未冒充完成的能力

可信主人 broad capability 已能在 Action/ToolBroker 合同层表达，但当前生产密封执行器只承接公开只读取证。Shell、文件写入、设备控制、Agent、媒体生成等高权限工具仍需逐类代码拥有的参数适配器、side-effect policy 和 typed `ActionReceipt`；在这些合同完成前一律失败关闭，不能把副作用结果伪装成 `GroundingFact`。

下一入口是 P3-07 语义与媒体守卫；随后应把 owner action adapters 作为独立任务闭环，再进入 P4/P5。
