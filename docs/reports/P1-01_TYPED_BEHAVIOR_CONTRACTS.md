# P1-01 typed 行为合同报告

## 结论

P1-01 已完成。新增 `core/contracts/` 纯合同包，统一绑定 Ingress、插件来源、Address、Attention、Participation、Action、Memory、KnowledgeGap、Media、Content、Expression 和 Presentation。该包没有接入 `main.py`，本阶段不改变任何线上回答行为。

## 根因与边界

原有 `TurnEnvelope`、`ReplyTarget`、`CapabilityPolicy`、`AffectAppraisal`、`TypedToolResult` 和发送回执可以复用，但以下局部类型不能冒充完整行为合同：

- `NameWakeDecision` 只区分 direct/mention/none，不能决定参与或动作；
- `LocalChatPlan` 固定为直接回复参数包，不会选择 React/Wait/Tool/NoAction/Initiate；
- `PresentationHandoff` 没有真实下游消费，不能作为发送回执；
- 旧 `SemanticRequirement` 是末端词面检查，不是当前问题的语义意图。

因此没有恢复旧 `planner_v2.py`、`SpeechPlanV2`、participation filter、shadow/回退开关或生成后二次口语化。

## 新合同

- `DecisionBinding`：所有当前轮决策共享 scope、session、当前消息、当前发送者、内容摘要、conversation revision、generation epoch 和 trace；跨用户、跨群、跨 revision/epoch 混配直接失败。
- `IngressDecision`：只有 `ACCEPT_HUMAN + HUMAN` 能进入星汐状态；banned/self/known-bot/plugin-echo/外部 gate 降级均不能写状态。
- `ExternalPluginEvidence`：插件状态、来源、history visibility 和允许消费者显式化；未验证、超时、接口漂移或不可信证据不能进入当前轮、Scene、记忆、Persona 或学习。
- Address、Attention、Participation、Action 分层；Address 不再自动等于必须回复。
- `ActionDecision`：Reply/React/Tool 必须绑定同一 `ReplyTarget`；Initiate 只能绑定 public scope；Wait/NoAction 不得伪装成可见回复。
- `MemoryDecision`：已选择事实不能来自其他主体或 banned/bot/plugin 自动输出。
- `KnowledgeGapDecision`：只描述证据需要，不包含授权结论；授权仍由 `CapabilityPolicy` 独立裁决。
- `MediaContext`：绑定 direct/quoted/quoted fallback 的来源消息和发送者；结构中没有 URL、path、base64 或 locator，repair 复用同一媒体项目。
- `ContentIntent`：保存语义原子和禁止原子，不比较固定角色台词。
- `ExpressionIntent/PresentationReceipt`：表达意图不含可见台词、表情 query 或工具参数；成功必须有真实 effect receipt，同一轮最多一个 Meme effect。

模型只能反序列化为不含身份、目标、权限、插件来源和媒体归属的软建议；出现受保护字段会被拒绝。

## 验证

- P1-01 新增测试：`18/18` 通过；
- 完整回归：`381/381` 通过；
- `compileall core/contracts`：通过；
- `git diff --check`：通过；
- 未修改 `main.py`、配置、FNOS 或任何第三方插件；未部署；未执行 Git/GitHub 写操作。

## 下一入口

P1-02：建立覆盖总计划第 9 节所有维度的合成 fixture manifest 和关键交叉场景；先完成 schema、覆盖与隐私 gate，再进入三层评测 harness。
