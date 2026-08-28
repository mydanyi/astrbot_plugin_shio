# P4-05 情境触发表达轨迹报告

## 结论

P4-05 已完成。新增通用 `PersonaExpressionPlan` 和解析器，把 `AffectAppraisal` 与当前 `PersonaPackage` 组合成类型化表达轨迹。表达不再由一个泛化“口语化/傲娇”标签决定，而由本轮具体 trigger、关系距离、人格素材和近期口头禅使用共同决定。

## 类型化轨迹

表达计划包含：

- 当前人格包和版本；
- 情绪 trigger 与关系距离；
- 表层反应、隐藏在意的自然露出、必须回接的话题动作；
- 应避免的行为；
- 与 trigger、关系距离匹配的表达素材；
- 按 trigger 和近期使用次数筛选的口头禅候选；
- 不可行动时的 replan 原因。

人格包无对应情绪规则、关系规则缺失、人格包校验失败或 appraisal 已降级时，不套用兜底风格，直接返回 `requires_replan`。

## ATRI 情境矩阵

- neutral：直接自然回应，禁止强行否认、强塞口头禅和介绍人设；
- information request：先回答当前问题，禁止从无关记忆答题或为了角色感绕开事实；
- praise：先有真实高兴反应，再软化接住；可以选择一次情境口头禅，但禁止整段持续否认；
- correction/mistake：短暂慌张后必须给出明确修正，禁止只有借口没有修复；
- user needs care：先认真安慰，禁止性能炫耀和强行玩梗；
- apology：自然接受并软化关系，不延长赌气；
- 近期已经使用同一句口头禅时，本轮候选自动抑制。

## 关系素材泄漏修复

测试阶段发现，单凭 trigger 会同时选中通用 praise 素材和 primary bond 亲密素材。已为 `ExpressionMaterial` 增加可选 `relationship_distances`：

- 主关系亲密素材只对 `primary_bond` 可见；
- 朋友边界素材只对 peer/public/unverified 可见；
- 未声明关系范围的通用素材可跨关系复用。

因此同一个“被夸”情境下，普通群友和主关系对象会共享基础情绪，但不会共享专属亲密外显。

## 非 ATRI 对照

同一解析器可以处理苏澄人格的信息问答轨迹，其素材和行为完全来自苏澄资产，不生成 ATRI 口头禅。缺少 praise 规则时明确 replan，不偷偷回退到 ATRI 或泛用傲娇模板。

## 验证

- P4-05 定向：`10` 项通过；
- 相关人格/schema 定向：`28` 项通过；
- 完整回归：`312` 项运行成功，`309` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 通用解析器源码不含特定角色专名和固定口癖；
- 未部署，未切换生产回复链，未执行 Git/GitHub 写操作。

## 下一入口

P4-06：把关系距离作为由 `PrincipalContext` 计算出的强类型事实传入人格表达层；表达计划必须验证 appraisal 与可信 principal 的关系一致，任何人格资产、自称、昵称或历史内容都不能把 peer/unverified 升级成 primary bond。
