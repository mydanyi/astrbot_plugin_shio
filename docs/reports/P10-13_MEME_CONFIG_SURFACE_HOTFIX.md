# P10-13 Meme 配置表面与情绪分类热修

状态：完成并部署（0.5.6）。

## 1. 用户可见问题与根因

本轮由两个真实 UI／群聊现象触发：

1. Meme Manager 配置页有“表情出现概率”，但 Shio 0.5.5 又在代码里固定四回合准入、四回合冷却，Shio WebUI 没有对应设置。这使实际频率成为“隐藏 cadence × 可见 Manager 概率”，用户无法从配置页完整解释行为。
2. 群友用“笨蛋都不会觉得自己是笨蛋”轻度挑衅后，角色给出防御性／抗议回复，却发送了“开心”图片。线上 `atri-expression-pack` 实际有 `annoyed`、`angry`、`shy`、`awkward`、`reject`、`confused` 等分类；旧 Shio 只映射 `happy/love/encourage`，其他 typed 情绪全部 `return happy`。

此外，旧 Affect 规则未识别“笨蛋／傻瓜／呆子”这类直接轻度挑衅，该截图场景因此先被投影成 `neutral/calm`，进一步触发错误 happy 兜底。

本报告不记录真实群号、用户 ID、机器人账号、原始日志正文或资源文件内容。

## 2. 修复

### 配置所有权

Shio 新增三个真实 WebUI 字段：

- `meme_complement_enabled`：是否申请文字后的 Meme 补图；
- `meme_complement_cadence_turns`：普通安全闲聊准入回合数，范围 1～16；
- `meme_complement_cooldown_turns`：补图机会后的冷却回合数，范围 1～32。

它们只控制 Shio 的申请机会和 cadence。最终是否出图继续只由 Meme Manager 的 `emotions_probability` 控制；Shio 没有新增第二个概率字段，也没有修改 Meme Manager 配置。

### 情绪分类

- 直接轻度“笨蛋／小笨蛋／傻瓜／呆子”进入现有 `playful_provocation + respect_boundary` 轨迹；
- boundary／轻度挑衅 → `annoyed`；
- 分歧／纠正误解 → `reject`；
- 被说中心思或夸奖后的害羞 → `shy`；
- 自身答错、修复信任 → `awkward`；
- 关怀 → `encourage`；感谢、道歉、温暖 → `love`；可靠 pleased → `happy`；
- `neutral/calm`、严肃或未知组合无可靠类别时返回 suppression，不再统一兜底 happy。

映射只读取 current-turn canonical `ExpressionIntent` 中由 AffectAppraisal 投影的 closed tags，不读取模型自由文本，不让模型决定分类。所有返回键均已在生产 `atri-expression-pack` manifest 中只读核对。

## 3. 红灯与验证

正式红灯先稳定复现：

- 截图原句仍被判为 `neutral`；
- `playful_provocation/startled/embarrassed/respect_boundary` 仍返回 `happy`；
- 端到端 ExpressionIntent 中没有 `playful_provocation`；
- 配置实现前，`MemeComplementCadence(enabled=...)` 不存在，插件忽略三个字段，schema 仍为 48 项。

修复后：

- 配置／Affect／Meme／presentation／release／candidate focused：`50/50`；
- Windows full：`1267/1267`，skipped 7，50.609 秒；
- 隔离 FNOS candidate 首轮只有 harness 漏带 `SECURITY.md` 和 `LICENSE` 的两项文档链接失败，补齐只读测试资料后：`1267/1267`，47.194 秒；
- 部署后从真实 live 目录重建测试副本：`1267/1267`，47.051 秒；
- compileall、candidate verifier、95-file manifest、`git diff --check` 全绿；
- deterministic candidate ZIP SHA256：`FCEC9B2B612D837E262651BFA48A558E72BA222D3266EB37104D91E525B20FCA`；manifest SHA256：`5D6728E0AB1D6ADBEC8D42B575DA3B08B976FCF628A4312D42B5B6C46B05C564`。

## 4. 生产部署

- 写前线上为 0.5.5、48 字段；三个 Shio Meme 字段均不存在；Meme Manager 的 `emotions_probability=50`、`mixed_message_probability=50`。
- 备份：`/AstrBot/data/backups/shio/P10-13-meme-config-emotion-20260819T173639Z`；包含原 live 插件、两份写前配置副本、候选 ZIP／manifest、失败保留位置和 durable `deploy.state`。
- 部署采用同一数据卷 rename，阶段为 `PREPARED → CONTAINER_STOPPED → OLD_PLUGIN_SAVED → NEW_PLUGIN_LIVE → OLD_CONFIG_SAVED → NEW_CONFIG_LIVE → CONTAINER_STARTED → VERIFIED`；异常会恢复旧插件／配置并重启。
- 配置严格 48→51，只新增 `true / 4 / 4`；其余 48 项逐值保留。写前配置 SHA256 `09528ABB…D769`，迁移后 `910EC9F0…1609`。
- 线上版本 0.5.6；95/95 source exact，额外 86 项全部为 `.pyc`；StartedAt `2026-08-19T17:38:57.466445371Z`；container running=true、restarting=false、OOMKilled=false、RestartCount=0；WebUI HTTP 200。
- 新启动窗口：0 Traceback、0 ERROR、0 Meme 初始化失败、0 typed prepare failure；0.5.6 marker 1，主动 scheduler 仍按原配置启动 1 次。
- 配置现为 51 字段，Meme Shio 设置 true/4/4；Meme Manager 概率仍为 50。owner action 总开关仍为 false；原有逐项值未借本热修改写。
- 未修改 AstrBot 核心、全局 Provider 回退、LivingMemory、Meme Manager、资源包、Compose 或其他插件。

## 5. 运行边界与中断恢复

截图原句和完整 typed 路径已在 candidate 与 production live-copy 测试中证明选择 `annoyed`，不是 `happy`。本阶段没有伪造 QQ 用户消息；真实平台下一次是否实际出图仍受 Meme Manager 当前 50% 概率约束，所以单条消息“不出图”不能解释为分类失败。出图时应按 typed 情境选择分类。

若会话中断，从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复。部署事务状态为 `VERIFIED`；需要回退时必须先停止容器，把当前 live 移入本备份的新保留路径，再将 `live-moved-original/` 和 `live-config-original.json` 原子移回，随后重启并验收。O1 仍未激活。
