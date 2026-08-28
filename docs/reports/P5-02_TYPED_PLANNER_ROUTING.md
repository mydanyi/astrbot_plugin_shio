# P5-02 结构化小型 Planner 路由报告

## 结论

P5-02 已完成。新增 `PlannerRoutingSignals` 与 `PlannerRoutingDecision`，将“是否调用 Planner”从模糊文本判断改成代码拥有的结构信号。快路径未命中本身不再自动触发 Planner。

## 唯一 Planner 触发条件

只有以下四类 typed signal 可以分配一次 `small_structured` Planner：

- 多个不同 reply target 候选；
- 多个关系候选或明确关系冲突 ID；
- 非空的请求能力分类，需要工具规划；
- 非空的事实冲突 ID。

多个条件同时出现时仍只有一次 Planner 预算。

## 路由类型

- `local_fast_path`：Planner 0 + ReplyComposer 1；
- `direct_reply_composer`：普通非快路径回复仍为 Planner 0 + ReplyComposer 1；
- `small_planner`：Planner 1 + ReplyComposer 1；
- `special_handler`：紧急风险不经过 Planner，由专门处理预算 1；
- `blocked_replan`：目标、身份、能力策略、affect 或 persona 结构错误时，当前生成预算 0。

任何路由的阻塞模型调用都不超过 2 次。

## 关键边界

- 精确的单个引用目标不会因为“出现引用”就浪费 Planner；
- 长消息、普通信息请求或快路径未命中的其他原因不能自行取得 Planner 预算；
- 结构身份/能力错配的优先级高于工具规划，不能用 Planner 修补权限错误；
- Planner route 只分配预算，不直接调用模型。

## 验证

- P5-02 定向：`11` 项通过；
- 覆盖本地快路径、普通直达、单/多目标、关系冲突、工具能力、事实冲突、组合上限、身份闭锁和紧急处理；
- 完整回归：`349` 项运行成功，`346` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 未接入生产调用链，未部署，未执行 Git/GitHub 写操作。

## 下一入口

P5-03：实现本地表达素材检索器。只从当前人格包、当前 trigger、situational tags、可信关系距离和类型化 trajectory 评分，返回最多 3 条行为候选；近期重复素材抑制，零匹配必须合法返回空集合，不能回退到固定口头禅或其他人格素材。
