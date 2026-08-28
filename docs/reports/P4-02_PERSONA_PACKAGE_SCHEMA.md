# P4-02 通用人格包 Schema 与校验报告

## 结论

P4-02 已完成。新增 `core/persona.py`，通用 schema 明确了人格内容与代码权限的边界，并为后续 ATRI 和其他角色提供同一套校验。

## Schema

`PersonaPackage` 包含：

- package ID、semver、显示名和身份摘要；
- 带稳定 ID 和权重的核心性格；
- primary bond、peer、public group、unverified 四类关系外显规则；
- 按 `AffectTrigger` 绑定的表层行为、隐藏在意外显和避免行为；
- 首选语言、是否响应明确外语请求、默认长度、格式偏好和带情境/频率的口头禅；
- 带 trigger/behavior 标签的表达素材；
- 标注 `canon`、`author_config` 或 `adaptation_note` 来源的角色事实。

## 权限边界

人格包只决定表达，不决定身份和工具授权：

- 不包含 owner ID 或 sender ID 配置；
- 不能声明 Agent、Shell、设备、文件写入或工具 allowlist；
- 不能用人格文本要求绕过身份或权限验证；
- primary bond 的排他恋爱、亲密接触、专属称呼、占有和私密特权动作不能开放给 peer/public/unverified；
- 最终关系距离仍来自代码提供的 `PrincipalContext`。

## 校验规则

- package/内部 ID 格式与 semver；
- 必填文本和至少一个核心性格；
- 关系距离缺失、重复、allowed/forbidden 冲突；
- 主关系专属动作越界；
- 情绪 trigger 重复和行为冲突；
- locale、trait 权重、warmth 范围；
- 口头禅必须有情境，近期频率最多 2；
- trait/material/fact 重复 ID；
- 表达素材必须同时绑定情境与行为；
- 角色事实必须有受支持来源；
- 人格文本中的工具协议和运行时权限声明。

## 验证

- P4-02 定向：`10` 项通过。
- 完整回归：`290` 项运行成功，`287` 项通过，`3` 项为迁移计划中保留的 v1 expected failure。
- `git diff --check`：通过。
- 通用 schema 源码测试保证不含特定角色专名和固定口癖。
- 未迁移或切换现有 ATRI Prompt，未部署，未执行 Git/GitHub 写操作。

## 下一入口

P4-03：只读盘点当前 core Prompt、默认 voice card、atri-lore-skill 与表达资产，将可信且不冲突的内容迁移为 ATRI `PersonaPackage`；保留 canon/author/adaptation 来源，去除权限声明和无条件固定口癖，先通过 schema 和快照测试，不切换生产 Prompt。
