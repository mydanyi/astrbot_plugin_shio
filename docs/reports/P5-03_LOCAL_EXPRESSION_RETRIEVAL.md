# P5-03 本地表达候选检索报告

## 结论

P5-03 已完成。新增纯代码 `retrieve_expression_candidates()`，从当前人格包的行为素材中返回 0～3 条 typed 候选。零候选是合法、可观察的正常结果，不会回退到固定口头禅、通用傲娇模板或其他人格资产。

## 检索输入

评分只使用：

- 当前 `PersonaPackage`；
- 当前可行动的 `PersonaExpressionPlan`；
- affect trigger；
- 显式 situational tags；
- 可信关系距离已经筛出的素材；
- 当前 trajectory 中的 behavior IDs；
- 近期已经使用的 material IDs。

不读取原始群聊全文、LivingMemory、昵称、工具参数或其他人格包。

## 候选结构

每条 `LocalExpressionCandidate` 包含 material ID、行为指令、behavior IDs、命中标签、是否为关系专属以及确定性分数。返回上限在代码中强制 clamp 到 3。

排序优先级为：当前 trigger、situational tag、关系专属匹配、trajectory 行为重合；同分按资产原始顺序稳定排序。

## 空结果

以下情况正常返回空集合并提供原因码：

- 当前情境没有匹配素材；
- 匹配素材近期都用过；
- 调用方把候选预算设为 0；
- expression 已降级；
- 当前 package 与 expression package 不一致。

空集合不会使快路径失败。ReplyComposer 应直接依据人格、情绪轨迹和当前消息自然生成，不补模板。

## 快路径接入

`LocalChatPlan` 已改为携带 typed `expression_candidates`，原 material ID 列表由实际检索结果生成，不再直接截取所有匹配素材。

## 验证

- P5-03 定向：`9` 项通过；
- expression retrieval + fast path：`19` 项通过；
- 覆盖空结果、peer/owner 关系排序、situational tag、上限、近期抑制、零预算、非 ATRI 和 package/plan 错配；
- 完整回归：`358` 项运行成功，`355` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 未接入生产调用链，未部署，未执行 Git/GitHub 写操作。

## 下一入口

P5-04：定义单次 `ReplyComposer` 请求/结果契约。一次生成同时完成语义、人格表达和气泡计划；候选为行为提示而非必须照抄台词；调用预算固定生成 1、常规风格重写 0，并提供无模型的解析器供 P5-05 validator 使用。
