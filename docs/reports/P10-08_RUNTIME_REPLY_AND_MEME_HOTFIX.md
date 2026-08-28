# P10-08 真实流量回复与表情热修

## 当前状态

- 状态：0.5.1 的文字静默修复已由自然流量验证；Meme 通路虽被触发但暴露分类键适配错误，已由 0.5.2 / `P10-09_MEME_CATEGORY_ADAPTER_HOTFIX.md` 接续修复并部署。
- 恢复入口：若会话中断，不再重复开发或部署；先读取本报告的“线上冻结事实”，再只核对新消息日志与自然回复/表情发送结果。
- 范围：只改星汐的 typed 回复编排、输出校验选择和 Meme presentation 合同；不改 AstrBot 核心、全局 Provider/fallback、Meme Manager、LivingMemory、配置或其他插件。
- Git：只读，不执行 add/commit/push。

## 已确认根因

1. 普通 direct `REPLY` 的初稿若触发一次修复，修复结果仍被拒时，`main.py` 当前清空 completion 后返回；生产日志只出现 `typed_reply.repair_rejected`，用户看到静默。
2. typed 主链在生成前清空模型工具是既定安全边界，因此 Meme Manager 的 `search_memes` 模型工具不会被调用。星汐已有发送后、一次性、代码持有的兼容执行器，但准入仅接受极窄显式轻松词，真实流量几乎全部落入 `no_complementary_cue`。

## 目标合同

- 不增加第二次修复模型调用。
- 普通、无工具、无媒体、无 ActionOutcome 的 statement 轮次，模型修复候选若仍不合法，允许改选一条 code-owned、无用户正文/事实/路径/参数的诚实重试提示；最终候选仍必须消费同一 repair permit，并通过 parser、OutputValidator、SemanticGuard、Presentation 和 FINAL_SEND seal。
- 问题、请求、工具结果、媒体、owner action、ambient/proactive 轮次不使用该兜底，继续 fail closed。
- Meme 不恢复任何模型工具，不公开 query/candidate/tag；只在已绑定的 `REPLY`、非严肃/非请求上下文中，由代码根据显式轻松信号、typed affect 或低频稳定采样签发最多一次补图资格。
- Meme 只在 exact 文字发送成功后执行；运行时 conformance、generation epoch、一次性 permit、超时和发送回执边界保持不变。

## 中断检查点

| 检查点 | 状态 | 证据 |
|---|---|---|
| 生产只读归因 | 已完成 | 静默轮次为 repair rejected；Meme 索引健康但模型工具调用为 0，代码补图准入过窄 |
| 历史/总计划完整复核 | 已完成 | 未恢复旧执行器、固定 reasoning fallback 或模型 Meme 工具 |
| 失败回归 | 已完成 | 定向 3 项：2 个真实失败、1 个保留边界通过；失败分别为二次候选后 completion 仍为空、稳定限频轻量陈述仍为 TEXT |
| 最小实现 | 已完成 | 无效模型 repair 仅做无 authority preflight，最终只签一个 canonical REPAIR；Meme 为显式信号或摘要稳定 1/8，标记限于开心/温柔/安慰 |
| 定向/相关/全量 | 已完成 | 红测转绿 4/4；相关 159/159；全仓 1257/1257，skipped=7 |
| 0.5.1 确定性候选 | 已完成 | 95 文件；ZIP SHA256 `F50EDA120CDF789BDFB8318E03D57DCEDF3A99F1B5720BD8FD7C3FC971933C96`；manifest SHA256 `D4D2505C2D769BF6B6C46AEAFA892A501666521D4D0F8F2D6B2DE8543760182E` |
| FNOS 候选隔离验证 | 已完成 | WebUI 200；manifest/ZIP exact；容器内 `1257/1257`、compileall 全绿。首次测试附包漏带 `SECURITY.md` 只造成 1 个文档链接装配失败，补齐后从头复跑全绿 |
| FNOS 备份/部署/验证 | 已完成 | 首次事务因校验脚本错误读取 `.State.RestartCount` 自动回滚并确认恢复 0.5.0；修正为 `.RestartCount` 后第二次事务到达 `VERIFIED`，见下节 |

## 线上冻结事实

- 生效版本：`0.5.1`。
- 容器：`running=true`、`restarting=false`、`RestartCount=0`，`StartedAt=2026-08-19T05:42:54.542994372Z`。
- live `main.py` SHA256：`69196569A952EB70AA110392C888EF306213F3A5C1010AFA4EEABC25FE54DADD`。
- live `_conf_schema.json` SHA256：`D5551F575C81E9FD626001D8760221E492048F1CD522FB78C76DAC7072AB39AF`。
- live `metadata.yaml` SHA256：`2D42D223E5271971F1870C40DCBC1FFEE84EDD2EFCC06B3990FDD5C050A3B951`。
- 配置与 schema 均为 48 字段，双向差集为空；`replyer_provider_id=''`，本热修没有创建 DeepSeek 备用设置。
- WebUI：`HTTP 200`。
- 启动日志：`astrbot_plugin_shio (0.5.1)` 正常加载并运行 `start_proactive_scheduler` hook，未出现 Shio 加载失败或 traceback。
- 插件备份：`/vol3/1000/Docker/Astrbot/data/backups/shio/astrbot_plugin_shio-P10-08-pre-20260819T054211Z`。
- 配置备份：`/vol3/1000/Docker/Astrbot/data/backups/shio/astrbot_plugin_shio_config-P10-08-pre-20260819T054211Z.json`。
- durable journal：`/vol3/1000/Docker/Astrbot/data/backups/shio/p10-08-20260819T053642Z/p10-08-deploy-20260819T054211Z.journal`，终态为 `VERIFIED`。
- 全局 Provider 回退、余额与 DeepSeek fallback 属 AstrBot 全局设置，本次没有修改。

## 下一步唯一入口

文字静默验收已通过；Meme 后续唯一入口迁移到 `P10-09_MEME_CATEGORY_ADAPTER_HOTFIX.md`。不要继续以本报告中的 0.5.1 中文展示词作为兼容接口分类键。
