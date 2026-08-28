# P5-01 Opportunity Attention 报告

## 1. 结论

P5-01 已完成本地与隔离 AstrBot Linux container 门，候选未部署。

本阶段只回答“这一条 accepted human message 是否值得进入后续参与判断”，不回答“是否真的插话”。`DIRECT_SELF` 必须进入，`ABOUT_SELF` / `OPEN_GROUP` 只成为候选，`OTHER_PERSON` / `UNCERTAIN` 等待；候选仍固定映射为 `ParticipationLevel.NO_ACTION`，因此 P5-01 不会新增非直呼回复。

## 2. 原问题

P3 热路径直接在 `main.py` 把 `DIRECT_SELF` 写成 `FORCE / MUST_REPLY`，其余地址统一写成 `IGNORE / NO_ACTION`。这能安全直答，但存在两个 P5 阻塞：

1. `AddressDecision` 是公开 dataclass，字段自洽或 copy 不能证明来自当前 resolver；
2. Accepted turn 固定 fan-out 只有 Owner Action 与 Affect State，没有独立 Attention consumer authority。

正式红灯先以缺少 `core.opportunity_attention` 的 `ModuleNotFoundError` 固定；测试同时预先锁定五类地址映射、copy/cross/mutation 失权、候选零 prompt/零模型/零工具、拒绝来源零状态。

## 3. 实现

### 3.1 Accepted turn 第三张固定子票

`AcceptedTurnConsumer.OPPORTUNITY_ATTENTION` 进入 code-owned 固定 fan-out。原 admission proof 仍只 claim 一次，三张 ticket 独立 one-shot；未使用 ticket 可继续走 E4A typed finalization。

`AcceptedTurnContext` 新增 exact、content-free `ConversationEvent` identity，并在每次 ticket inspection/claim 前复核 exact event、ingress、binding、envelope、principal、sender kind、plugin source 与 content digest，防止 copied event 或嵌套替换成为 current-turn authority。

### 3.2 Resolver-owned exact address

`AddressResolutionAuthority` 是 runtime-local issuer：

- 只有其 `resolve_private` / `resolve_group` 会登记 canonical address；
- resolution 必须绑定 exact `AcceptedTurnContext` 与 exact canonical `ConversationEvent`；
- weak identity ledger 保存完整 address/binding snapshot；
- copy、跨 authority、字段篡改、等值替换均不能通过 `inspect`；
- public `AddressDecision` shape 本身不再被 P5 当作 authority。

### 3.3 独立 OpportunityAttention

新增 `OpportunityAttentionAuthority`、`OpportunityAttentionDecision` 与闭集 `OpportunityAttentionLevel`：

| AddressKind | Attention | P5-01 Participation |
|---|---|---|
| `DIRECT_SELF` | `REQUIRED` | `MUST_REPLY` |
| `ABOUT_SELF` | `CANDIDATE` | `NO_ACTION`（延后到 P5-02） |
| `OPEN_GROUP` | `CANDIDATE` | `NO_ACTION`（延后到 P5-02） |
| `OTHER_PERSON` | `WAIT` | `NO_ACTION` |
| `UNCERTAIN` | `WAIT` | `NO_ACTION` |

签发必须同时重验 exact Attention ticket、AcceptedTurnContext 与 resolver-canonical address，最后一步 claim ticket。Opportunity decision 使用 weak exact identity ledger 与完整 snapshot；copy、cross-authority、address/opportunity mutation 全部 fail closed。

### 3.4 热路径

`main.py` 在 ingress admission 成功并得到 address 后立即签发 OpportunityAttention；后续 Planner 前必须 exact inspect。现有 `AttentionDecision` 只是经过 authority 校验后的 Planner 投影：

- REQUIRED → `FORCE`；
- CANDIDATE → `CONSIDER`；
- WAIT → `IGNORE`。

Participation 仍只有 REQUIRED 为 `MUST_REPLY`，其余固定 `NO_ACTION`。P5-01 不调用额外模型、不构造参与文本、不打开工具。

Self、known bot、plugin echo 和外部门禁拒绝不会产生 accepted dispatch，因此没有 Attention ticket、Address、Scene、Affect、Learning、工具或回复副作用。Ban 仍由 priority-114 ReNeBan 外部门禁先行；本阶段未修改第三方插件。

## 4. 验证

- 正式红：`core.opportunity_attention` 缺失，targeted import error；
- P5-01 targeted：`4/4`（含五类映射与多组 subtests）；
- AcceptedTurn / Address / P3 hot path / P4 Affect / P5 Attention related：`76/76`；
- Windows full：`1085/1085`，skipped 7；
- AstrBot Python 3.12 隔离 container full：`1085/1085`；
- `py_compile` / `compileall` / merge marker / trailing whitespace：通过。

隔离容器第一次 staging 命令因远端 `awk` 转义提前退出，第二次 pattern 带入字面引号而发现 `0` 项；两次都未执行候选测试，也未修改生产。修正为独立临时根与无引号 pattern 后完成 `1085/1085`。

生产前后均为：AstrBot running=true、restart=0、WebUI 200，live `main.py` SHA256 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`。

## 5. 变更范围

- `core/accepted_turn_authority.py`
- `core/contracts/behavior.py`
- `core/address_resolver.py`
- `core/opportunity_attention.py`（新增）
- `main.py`
- `tests/test_accepted_turn_authority.py`
- `tests/test_p4_affect_hot_path.py`
- `tests/test_p5_attention_gate.py`（新增）

无 Git 写操作、无 FNOS 生产写入、无 AstrBot 重启。

## 6. 未完成边界与恢复入口

P5-01 不代表“未点名自然参与”已经生效。当前 CANDIDATE 仍为 `NO_ACTION`。

下一唯一入口是 **P5-02 Participation Engine**：只消费 exact canonical OpportunityAttention；综合 Persona interest、当前 self relevance、关系、GroupScene 节奏、打断成本、回应价值和近期存在感，输出独立 typed `ParticipationDecision`。必须保持 DIRECT=`MUST_REPLY`，OTHER_PERSON/UNCERTAIN 默认不参与，并且不得在 P5-02 内直接生成文本。中断后从这里恢复，不重做 P5-01。
