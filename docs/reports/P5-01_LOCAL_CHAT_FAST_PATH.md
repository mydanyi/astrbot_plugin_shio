# P5-01 简单聊天本地快路径报告

## 结论

P5-01 已完成。新增纯代码 `build_local_chat_fast_path()`，在 ReplyComposer 之前把本轮目标、可信主体、能力策略、基础 affect 和人格表达候选绑定成 `LocalChatPlan`。该路径没有模型、Provider、Planner、Memory 或 facts 回调入口。

## 快路径产物

`LocalChatPlan` 固定包含：

- 精确 `ReplyTarget`；
- 当前 `PrincipalContext.sender_key`；
- 当前 `CapabilityPolicy.policy_kind`；
- 由可信 principal 计算的关系距离；
- `AffectAppraisal`；
- `PersonaExpressionPlan`；
- 最多三条本地表达素材 ID；
- `planner_call_budget=0`；
- `tool_call_budget=0`；
- `fact_ids=()`；
- 后续只需要一次 ReplyComposer。

owner 即使拥有完整能力，在简单社交快路径中同样是零工具预算；权限更高不会让普通聊天自动调用外部能力。

## 可进入场景

当前只允许短、直接、目标明确的社交 trigger：neutral、praise、playful provocation、being seen through、concern、care、apology 和 gratitude。

## 退出场景

- information、action、correction、disagreement 等复杂 intent；
- 程序、脚本、文件、部署、服务器等明显执行内容；
- URL、代码块或长消息；
- 自伤/急性身体危险信号；
- 引用消息（本层先保守退出）；
- reply target、principal、capability policy、relationship 或 conversation mode 不一致；
- 身份、策略、affect 或 persona expression 降级。

退出快路径不等于拒绝，只表示交给 P5-02 的路由决定是否需要小型 Planner、直接 ReplyComposer 或专门处理。

## 验证

- P5-01 定向：`10` 项通过；
- 覆盖 peer、owner、ATRI、非 ATRI、信息/动作/纠错、编程、紧急风险、引用、长消息、目标和策略错配；
- 完整回归：`338` 项运行成功，`335` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 未接入生产调用链，未部署，未执行 Git/GitHub 写操作。

## 下一入口

P5-02：增加结构化 Planner 路由。只有多目标歧义、复杂关系、工具规划或事实冲突能获得一次 small planner 预算；普通信息回复和精确引用可以零 Planner 直达 ReplyComposer，结构身份错误关闭，紧急风险走专门处理，总阻塞调用上限为 Planner 1 + ReplyComposer 1。
