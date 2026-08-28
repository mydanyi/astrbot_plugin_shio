# P8-12 代码级 typed-only 完整替换报告

## 结论

星汐现在只有一条可运行的 typed 对话链。旧 Planner、旧 Replyer/风格检索、旧恢复补答、未点名主动生成、旧架构模式开关以及发送前的固定可见故障话术均已从当前代码和线上插件目录删除。

这次不是继续给旧链补条件，而是把旧执行路径物理移除，并以当前完整源码重新执行本地、AstrBot 容器暂存副本和线上实际目录三层验收。

## 反复出现旧表现的根因

1. 上一轮生产部署使用“覆盖若干文件”，Docker 数据卷不会自动删除本地已经删掉的线上文件。部署前只读检查确认线上仍有 11 个已废弃模块，配置仍保留 89 项字段，其中包括 3 个架构迁移字段。
2. 旧输出守卫在识别到协议、推理或身份泄漏后，会把结果改写成固定的可见故障句。此前的“语言模块打结”“暂时校准失误”类回复就是这类路径产生的机械话术；日志虽然记录为 blocked，用户端仍会看到一条无关回复。
3. 新版已改为一次 ReplyComposer 生成、一次严重违规修复；修复仍不合格时整轮闭锁，不再向群里发送固定机器故障台词。

## 当前唯一运行链

`可信事件发送者 → ReplyTarget → 身份分区历史/引用 → CapabilityPolicy → affect/人格表达候选 → ReplyComposer → OutputValidator → 最终发送边界`

- 当前发送者只能来自 AstrBot 事件的可信 sender ID；昵称、自称、引用和历史文本不能提升为主人。
- 主人保留完整 Agent/插件能力；群友只保留配置且经过实现来源审计的聊天只读/展示工具。
- LivingMemory 只提供带来源的历史候选；当前目标和关系由星汐重新绑定，召回内容不能替换当前发言者。
- 人格由可替换 persona package 提供。亚托莉只是当前生产参考人格，通用核心不把其他人格改写成亚托莉。
- 社交反馈只以有界小权重调整同情境表达候选排序，不自动改写人格包，也不产生新模板句。

## 删除和收敛

- 删除旧 Planner、Planner v2、旧 models/prompts/style retriever、recovery queue、architecture mode、participation/planner router、performance budget 和旧默认表达文件。
- 删除 `off/shadow/owner_private` 等运行模式读取点；插件启用即 typed-only，typed 准备失败即关闭本轮。
- 删除旧安全降级动作和通用可见 fallback；协议、隐藏推理、身份混淆在最终发送边界只会闭锁。
- 删除未点名主动接话、主动开题和延迟补答生成器；保留的自然称名功能只把明确直呼提升为正常直接对话。
- `_conf_schema.json` 从线上旧配置的 89 项收敛为 23 项真实字段，schema 与生产读取点审计为 0 orphan。

## 自动化验收

| 层级 | 结果 |
| --- | --- |
| Windows 本地完整测试 | 363/363，OK |
| AstrBot 容器独立暂存副本 | 363/363，OK |
| FNOS 线上实际插件目录 | 363/363，OK |
| `compileall` | 本地与容器通过 |
| 禁止旧文件扫描 | 0 |
| 禁止旧运行符号扫描 | 0 |
| schema 字段 / orphan | 23 / 0 |
| `git diff --check` | 通过 |

线上 `main.py` SHA-256：

`1050cfe634a740dee9a127fbe9c8a7c7a659361193af686ac986ef740a1e0f90`

线上 `_conf_schema.json` SHA-256：

`d99ca3cb89e622773724a6711412189aaf7ff36fa3177463239859e79e8a6937`

两项均与本地文件一致。

## FNOS 部署结果

- 目标：`192.168.50.38`，容器 `astrbot`。
- 线上目录：`/AstrBot/data/plugins/astrbot_plugin_shio`。
- 恢复快照：`/AstrBot/data/backups/shio/astrbot_plugin_shio-P8-12-pre-typed-only-20260817T170344Z`。
- 配置快照：`/AstrBot/data/backups/shio/astrbot_plugin_shio_config-P8-12-pre-typed-only-20260817T170344Z.json`。
- 快照只用于部署事故恢复，不是运行入口；当前代码没有旧架构回退开关。
- 线上配置：89 → 23，移除 66 个旧字段；既有主人白名单与群友聊天工具白名单原值保留。
- 新 StartedAt：`2026-08-17T17:04:04.543141879Z`。
- 容器：running，restart 0；WebUI：HTTP 200。
- 重启窗口：Traceback 0、ERROR 0、Exception 0、旧运行标记 0；星汐加载标记 3。

未修改 LivingMemory、表情包插件、AstrBot 核心、Docker Compose、Provider 或 FNOS 其他配置；未执行 Git/GitHub 写操作。

## 尚待被动确认

报告收尾时，重启后还没有自然产生新的直接对话，因此 `typed_reply.prepared` 和发送事件均为 0，闭锁/异常事件也均为 0。本轮不主动向群里制造消息；下一次自然对话只需只读核对结构化事件即可，不应再以此为理由恢复旧代码或固定兜底话术。
