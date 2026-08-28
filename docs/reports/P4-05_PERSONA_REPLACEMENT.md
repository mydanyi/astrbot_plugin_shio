# P4-05 第二与最小中性 Persona 验收报告

## 结论

P4-05 已完成本地、生产热路径与隔离 AstrBot Linux container 门，候选未部署。

仓库现在有三份同 schema Persona：默认 ATRI、原创第二 Persona 苏澄，以及只包含一个核心特质、两种基础触发、零情境材料、零角色事实、零口头禅的最小中性 Persona。三者都由同一 loader、validator、expression resolver、Composer、Semantic Guard 和发送代码处理；`main.py` 只按配置选择 package，不含角色特定分支。

## 红灯

首轮正式红灯为 `neutral_minimal.json` 不存在，两个测试类在 `setUpClass` 稳定报错。新增资产后转绿；热路径测试初版误读了 intentionally-private `AffectRenderContext.binding`，修正为只比较公开脱敏 trace，不为测试开放私有 binding。

## 实现

### 最小中性 Persona

新增 `assets/personas/neutral_minimal.json`：

- 一个 `direct_clarity` 核心特质；
- primary/peer/unverified 三个必要关系规则，关系只影响语气；
- 仅 `neutral` 与 `information_request` 两个基础情境；
- 零 expression material、零 character fact、零 source、零 catchphrase；
- 没有 ATRI、苏澄、工具、权限、目标或发送语义。

它不是新的生产默认，也不增加 fallback；未覆盖的 trigger 仍按现有通用规则 fail closed/replan。

### 三人格等价骨架

对同一信息问题，测试证明三者共享：

- ContentIntent kind/language/required/forbidden/media/grounding 语义；
- PlannedAction kind 与 ReplyTarget；
- CapabilityPolicy；
- continuous affect 脱敏状态；
- system renderer contract、call budget、reply shape；
- `[当前轮语义与证据]` 完整 Prompt 前缀；
- 同一个生产 `enforce_agent_permission → build_persona_reply` 热路径。

唯一变化是 package id 和 `[人格与表达]` 区域。`main.py` 与通用 core 源码不包含第二/中性 Persona 的 package id 或显示名。

## 验证

- 红灯：缺少最小 Persona 资产，`2 errors`；
- replacement + Composer targeted：`6/6`；
- Persona/Composer/expression/retrieval/hot-path focused：`45/45`；
- production persona replacement hot path：`2/2`；
- Windows full：`1075/1075`，skipped 7；
- AstrBot Python 3.12 隔离 container full：`1075/1075`；
- `compileall`、merge-marker、trailing-whitespace：通过。

## 生产边界

- `persona_name` 已有通用 display-name/package-id 选择路径，本阶段未修改 main；
- 线上仍配置 ATRI，未切换 Persona；
- 未覆盖线上插件、未重启生产容器；
- production adapter allowlist/live output authority 仍未开放，四 adapter 零执行；
- 未执行 Git 写操作。

## 唯一下一入口

**P4-06 去模板/复读**：把可见回复的近期短语、结构骨架和历史复述风险变成代码可检查的 repetition profile；在生成前抑制近期口头禅候选，在验证/单次 repair 中拒绝连续固定开头、高频“才没有/高性能机器人”、结构复读和把历史原句当新回复。
