# P3-04 typed Tool Broker 纯模块报告

## 结论

P3-04 纯模块已完成。新增 `core/tool_broker.py`，只在同 binding `PlannedAction(USE_TOOL)`、KnowledgeGap、当前可信 CapabilityPolicy、运行时 ToolClassification、独立 configured allowlist 与代码签发 request shape 全部一致时，选择唯一 exact tool 并构造一次 `AcquisitionRequest`。

本阶段没有接入 `main.py`，没有改变 AstrBot 工具循环，也没有触碰 `LocalChatPlan`、旧授权链、Planner、shadow 或 fallback。没有修改现有 action/planner/capability contracts。

## 先红后绿

- 先新增 `tests/test_tool_broker.py`；
- 第一次定向运行稳定失败：`ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.tool_broker'`；
- 实现 Broker 后定向测试 `11/11` 通过；
- 安全复核发现现有 CapabilityPolicy 的来源 hint 是通用子串匹配，Broker 若直接照搬可能接受带相似插件名的克隆模块；Broker 单独收紧为 module path token 级证明，并增加 `evil.astrbot_plugin_anysearch_clone` 回归；
- Tool Broker + Action Planner + CapabilityPolicy + behavior contracts 相关回归 `65/65` 通过。

## 输入硬门

`broker_tool_request()` 必须同时收到：

1. frozen/slots `PlannedAction`，且 `ActionKind` 必须是 `USE_TOOL`；
2. 与 Action 完全同 binding 的 `KnowledgeGapDecision`；
3. `principal_key` 命中当前 sender、`conversation_mode=direct_reply` 的可信 `CapabilityPolicy`；
4. 当前运行时 `ToolClassification` 集合；
5. 独立的代码配置 allowlist；
6. 由本模块工厂签发、带内部 seal、同 binding 的 `AcquisitionRequestShape`；
7. 代码时钟 `now`。

Action、KnowledgeGap、Policy principal 或 request shape 任一跨 sender/scope/message/content/revision/epoch 都失败关闭。非 `USE_TOOL` Action 与普通群友 `KnowledgeNeed.NONE` 固定零工具。

## 普通群友 AnySearch 路由

普通群友只允许 `PUBLIC_WEB_READ`，且必须有真实 KnowledgeGap、Policy 允许、运行时来源证明、active、接口匹配，并同时存在于 Policy 配置与 Broker configured allowlist。

| 代码 request shape | 唯一工具 | 必需接口 | 规则 |
|---|---|---|---|
| `SEARCH` | `anysearch_search` | `query` | 普通单问题；纯单 URL 不允许伪装成 search |
| `EXTRACT` | `anysearch_extract` | `url` | 仅显式单个 `http/https` URL |
| `BATCH_SEARCH` | `anysearch_batch_search` | `queries` | 2～5 个不同查询，且 batch 必须显式配置 |

batch 缺失时不会退回多次 search，extract 缺失时不会把 URL 改为 search，search 缺失也不会换用其他公开搜索插件。[AnySearch 公开接口](https://github.com/anysearch-ai/anysearch-mcp-server)同样以 `query`、`url`、`queries` 区分 search、extract 与 batch；本地实际 runtime descriptor 仍是最终接口证据。

## 可信 owner 精确高权限动作

Owner 只有同时满足以下条件才能选择非知识类高权限工具：

- Policy 必须是可信 AstrBot sender ID + configured owner allowlist 生成的 owner/full policy；
- 当前 `PlannedAction` 已明确为对应 capability 的 `USE_TOOL`；
- `KnowledgeGap=NONE`，表明这是当前明确能力动作而不是伪造知识缺口；
- code-sealed `CAPABILITY_ACTION` request shape 的 capability 与 Action 相同；
- configured allowlist 与 runtime inventory 中该 capability 只能剩一个来源已证明的候选；
- 参数名必须属于该实际 runtime descriptor 的参数集合。

Broker 只输出这一个工具，不把完整工具箱交给模型或 Renderer。若同一 capability 有两个已配置候选，固定按 ambiguous 失败关闭。把 guest policy 的 `is_owner/shell_exec/agent_full` 字段手工改真也不能通过 owner 的 policy kind、relationship 与 verification source 联合证明。

普通群友即使把文件读写、Shell、程序执行、设备控制、管理或 Agent 工具写入 allowlist，或给危险包装器改成 AnySearch 名称，也会在 capability、重新分类、source 或 interface 硬门失败。

## Runtime classification 与来源证明

Broker 不直接相信调用方传入的 `ToolClassification`：

1. 使用实际 `ToolDescriptor` 重新运行 `classify_descriptor()`；
2. 对 configured 候选要求重算结果与传入 classification 完全相同；
3. exact runtime name 必须命中独立 allowlist；
4. `decide_tool()` 必须允许；
5. audited source hint 必须按 module path token 命中，而不是宽松子串；
6. AnySearch 还必须精确证明 `astrbot_plugin_anysearch` source token；
7. active 与 request-kind 必需参数必须存在。

因此 missing、disabled、interface changed、source mismatch、同名未知插件、相似插件名克隆、classification forgery、危险语义 masquerade 与 duplicate exact name 全部失败关闭，且没有 legacy/fallback 路径。

## 代码签发参数

`AcquisitionRequestShape` 为 `init=False + internal seal + frozen/slots`：

- search 只保存一个 query；
- extract 只保存一个经过 URL 结构校验的 URL；
- batch 保存 2～5 个去重查询；
- owner capability action 保存代码提供的 closed scalar 参数，并拒绝 owner/binding/tool/source/deadline/budget 等权威字段。

shape digest 同时覆盖完整 DecisionBinding、request kind、capability 与规范化参数。Broker 只从 shape 生成实际工具参数，模型不能提交 exact tool、source、arguments、deadline 或 budget。

## 输出与隐私

输出由两个 frozen/slots 类型组成：

- `ToolSelection`：隐藏 binding/action ID 的 repr 字段，并绑定唯一 exact tool、attested source、capability、固定 deadline 与 `call_budget=1`；
- `AcquisitionRequest`：绑定 selection、request kind、shape digest 与 repr-hidden immutable argument payload。

deadline 固定为 `now + 20s`，外部不能传 deadline；一次 acquisition 的 call budget 永远是 1，即使 KnowledgeGap 的上游预算为 2。query、URL、owner 参数、scope/session/message/sender ID、content digest 与 action ID 不进入 repr/trace。只有执行适配器显式调用 `materialize_arguments()` 时才得到一次参数副本。

## 模型边界

Broker API 没有 model、model suggestion、tool name、exact tool、source、arguments、deadline 或 budget 参数。模型最多在 P3-01 提交受限软 Action hint，但不能构造 `PlannedAction(USE_TOOL)` 的 binding/capability，也不能取得 request-shape seal。

## 验证

- P3-04 定向测试：`11/11` 通过；
- P3-04 + P3-01 + CapabilityPolicy + behavior contracts 相关回归：`65/65` 通过；
- 覆盖 guest search/extract/batch、owner 精确 Shell、self-claim 不提权、无 gap/非 UseTool 零工具、action/gap/policy/shape 错绑、deadline、budget=1、stale、missing、disabled、interface changed、source mismatch、name masquerade、同名未知插件、相似名称克隆与无 attestation；
- 新模块与测试通过 `compileall`，三个任务文件通过 diff whitespace check；
- 未修改 `main.py`、计划、现有 contracts、旧链路、配置、FNOS 或第三方插件；未部署；未执行 Git/GitHub 写操作。

## 后续边界

- P3-05 才把一次真实执行结果转为同 action/binding 的 GroundingFact；
- P3-06 原子切换前，本 Broker 不进入生产热路径；
- 执行适配器必须再次校验 deadline、action_id、call_budget 与当前 generation epoch，不能缓存或复用参数；
- AnySearch 或 AstrBot 插件接口升级后，必须先更新 runtime descriptor 回归，禁止为了“先能用”而添加名称 fallback。
