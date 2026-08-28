# P1-02 合成行为 fixture 矩阵报告

## 结论

P1-02 已完成。新增一套版本化、只含合成别名且可由后续各阶段共同消费的行为 fixture 矩阵，共 `24` 个场景；它覆盖总计划第 9 节定义的 `15` 个评测维度，并把关键交叉场景设为 manifest 硬门。

本阶段只建立评测资产，不接入 `main.py`，不改变线上回答行为。

## 资产

- `tests/fixtures/p1/manifest.json`：固定 schema 版本、隐私等级、15 个维度的闭集值、6 个场景文件和 12 个必须存在的交叉场景。
- `tests/fixtures/p1/ingress_identity.json`：主人、群友伪装主人、banned/self/known-bot/plugin-echo/外部门禁和未知自动化降级。
- `tests/fixtures/p1/address_participation.json`：结构化提及、引用、自然称名、谈论机器人、群友互聊、表情回应与静默。
- `tests/fixtures/p1/memory_knowledge_tools.json`：当前主体、他人记忆污染、陌生词、时效事实、显式查证、搜索失败和权限边界。
- `tests/fixtures/p1/media_presentation.json`：直接图、引用图、reply-id fallback、多图、不可用媒体、repair 和 Meme 单 effect。
- `tests/fixtures/p1/persona_affect_output.json`：多 Persona 同语义 A/B、情绪轨迹、翻译/语言练习及意外英文切换修复。
- `tests/fixtures/p1/concurrency_send.json`：慢工具过期结果和重启恢复的重复发送边界。

## Schema 与隐私门

- 每个 case 必须填写全部 15 个维度、`P2`～`P10` 的真实落地阶段、合成输入、Stub 状态和语义期望。
- 维度值必须来自 manifest 闭集；重复 case、缺字段、未知值、未覆盖枚举或缺失关键交叉场景均失败。
- 不使用 `expectedFailure`/`xfail` 隐藏未来阶段；`required_phase` 明确其实施归属。
- fixture 隐私扫描 fail-closed：拒绝 URL、data URL、base64、Bearer、API key、Cookie/Token、真实长数字 ID、UMO、绝对路径、完整请求体、原始群聊和 Prompt 等高风险键值。
- 所有 sender、scope、turn、media 和 persona 都使用合成别名；没有把真实群聊全文或用户 ID 写入仓库。

## 验证

- P1-02 定向测试：`5/5` 通过；
- fixture：`24` 个唯一场景、`15` 个维度闭集值全部覆盖、`7` 个 JSON 文件通过隐私门；
- 完整回归：`386/386` 通过；
- 未修改 `main.py`、配置、FNOS 或第三方插件；未部署；未执行 Git/GitHub 写操作。

## 下一入口

P1-03：让同一套 fixture 被确定性 runner、严格 Stub 集成 runner 和显式启用的可选真实模型 runner 共同消费；默认测试必须零网络，并生成不含正文的聚合报告。
