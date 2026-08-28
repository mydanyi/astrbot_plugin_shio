# P5-08 最终语义到表情插件交接报告

## 结论

P5-08 已完成，P5“一次生成、自然表达和延迟预算”阶段闭环。新增 typed `PresentationHandoff`，在不修改 Meme Manager 的前提下，只把已经通过 validator 的最终语义与情绪/行为标签交给本地呈现能力。

## 交接条件

必须同时满足：

- Composer result 已通过 `OutputValidatorV2`；
- 最终可见文本非空；
- result target message ID 与 `LocalChatPlan` 一致；
- capability policy principal 与本轮主体一致；
- capability policy conversation mode 与本轮一致；
- policy 明确允许 `LOCAL_PRESENTATION`；
- 本地呈现预算不为 0；
- 不是 quiet topic。

任一条件失败都返回空 handoff，候选调用预算为 0。

## 交接内容

可用 handoff 只包含：

- 最终可见文本及不可逆 digest；
- affect trigger、表层/次级情绪和 hidden concern 标签；
- topic return 与 trajectory behavior 标签；
- 最多一次 presentation candidate 调用预算。

不包含 Planner 草稿、LivingMemory、sender/owner ID、工具参数、工具协议、人格原始资料或修复过程。trace metadata 只含布尔值、计数和 digest，不含正文或目标 ID。

## 第三方边界

- 未修改 Meme Manager 或任何第三方插件；
- 未改 AstrBot core；
- handoff 不调用工具，只提供星汐侧的 typed 输入；
- 实际选图仍由现有本地表情插件完成，允许插件不选图。

## 验证

- P5-08 定向：`6` 项通过；
- 覆盖成功交接、validator/空结果拒绝、能力/目标错配、quiet 禁用、脱敏 trace 和第三方隔离；
- 完整回归：`400` 项运行成功，`397` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 未接入生产工具链，未部署，未执行 Git/GitHub 写操作。

## P5 阶段结果

简单聊天本地快路径、严格 Planner 路由、0～3 本地表达候选、单次 ReplyComposer、最小 validator、一次修复状态机、调用/本地延迟预算以及最终表情语义交接均已建立。v2 simple 稳态预算为一次模型生成，P5 新路径仍保持本地 shadow。

## 下一入口

P6-01：查明当前 AstrBot `event.send`、最终 `MessageEventResult` 和平台适配器的返回值是否包含真实平台 message ID。优先审计当前版本源码/官方接口；如果不能可靠得到，定义内部 `reply_id`、每个 segment 的发送尝试/成功状态和可选 `platform_message_id`，绝不从时间或日志文本猜 ID。
