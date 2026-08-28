# P4-04 非 ATRI 人格替换验证报告

## 结论

P4-04 已完成。新增最小原创测试人格包 `assets/personas/su_cheng.json`，它与 ATRI 人格包使用同一 loader、schema、关系距离和情绪触发类型，同时保持完全不同的性格、表达素材和口头禅策略。

## 测试人格特征

- 核心方向是耐心清晰、安静温暖和低频干幽默；
- 只配置 neutral、information request、user needs care、disagreement 和 ambient shared topic 五类必要情绪规则；
- 不配置任何固定口头禅；
- 不开放主关系专属恋爱动作；
- 事实仅来自该测试资产自己的 author config；
- 不包含 ATRI 专名、高性能、校准或原作资料。

这证明 schema 不要求所有角色都表现成 ATRI，也不把“傲娇”当成通用默认风格。

## 替换边界

加载两个人格包时没有修改或引用：

- `planner_v2.py`；
- `identity.py`；
- `capability_policy.py`；
- `response_guard.py`。

测试确保测试人格的 package ID 和显示名没有硬编码进以上管线文件，两个角色资产也不互相包含角色标志词。

## 验证

- P4-04 定向：`4` 项通过；
- 完整回归：`302` 项运行成功，`299` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- 两个人格包均由同一个 `load_persona_package()` 成功加载；
- 未部署，未切换生产 Prompt，未执行 Git/GitHub 写操作。

## 下一入口

P4-05：将 `AffectAppraisal` 与 `PersonaPackage` 组合为类型化表达轨迹，确保傲娇只由表扬、被看穿、调侃或犯错等情境触发；普通问答、认真解释、道歉和关心不被强行改写为嘴硬模板。
