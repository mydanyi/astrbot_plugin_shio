# P4-03 ATRI 人格资产迁移报告

## 结论

P4-03 已完成。现有 ATRI 核心 Prompt、`atri-lore-skill`、默认 voice card 和表达资产中的可信角色内容，已迁移为独立、可校验、可替换的 `assets/personas/atri.json`。本层保持 shadow，未替换生产 Prompt。

## 源材料审核

原有核心 Prompt 同时承担角色设定、可信身份、工具权限、记忆隔离、外部任务、输出协议和表情策略。单份长文本中的这些职责会互相竞争，也使模型可能把“关系表达”误读成“授权事实”。本次迁移只保留以下人格内容：

- 原作或项目接续创作中的角色事实；
- 核心性格及其相对权重；
- 不同关系距离下的外显方式；
- 各情绪触发下的反应轨迹和禁止行为；
- 简体中文、默认长度、格式偏好和低频情境口头禅；
- 从现有表达库抽象出的行为素材。

以下内容没有迁入人格包：

- owner/sender ID、身份验证方式；
- Agent、联网、Shell、文件、设备等能力授权；
- LivingMemory 归属和消息目标判定；
- 工具协议、执行流程和外部副作用；
- 生产 Prompt 切换逻辑。

## 迁移产物

- `assets/personas/atri.json`：ATRI 角色资产；
- `PersonaSource`、`PersonaFact.source_ref`：具体来源清单和事实级来源引用；
- `persona_package_from_mapping()` / `load_persona_package()`：通用 JSON 加载器；
- 来源类型和引用一致性校验；
- ATRI 结构快照、关系边界、口头禅频率、事实来源和运行时授权禁入测试。

人格包覆盖全部 14 个通用 `AffectTrigger`。傲娇不再是默认语气，而只出现在表扬、被看穿、调侃和小错误等具体情境中；普通问答、认真解释、安慰、道歉和群聊插话各有不同轨迹。

## 来源标注

包内来源分为：

- `canon`：由 lore 资料整理的原作人物事实；
- `author_config`：当前 core Prompt 和 voice card 中的项目表达设计；
- `adaptation_note`：单翼关系接续创作和已部署表达资产的抽象迁移。

接续创作明确与官方剧情分开。表达素材只保留行为方向，不依赖固定示例台词。

## 验证

- P4-03 定向：`18` 项通过；
- 完整回归：`298` 项运行成功，`295` 项通过，`3` 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 人格包校验：通过；
- 未部署，未切换生产 Prompt，未执行 Git/GitHub 写操作。

## 下一入口

P4-04：创建最小非 ATRI 测试人格包，使用同一 loader、schema、情绪触发和关系距离验证“更换角色只换资产，不修改 Planner、身份层和守卫”。
