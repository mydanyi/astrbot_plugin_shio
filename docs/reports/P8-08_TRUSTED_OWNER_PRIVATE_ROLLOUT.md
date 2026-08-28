# P8-08 可信主人私聊渐进启用报告

## 结论

P8-08 的首批生产范围已完成：统一 v2 现在只会在 `architecture_v2_mode=enabled`、`architecture_v2_rollout_scope=owner_private`，且当前事件同时满足“结构化 sender ID 命中 owner allowlist、可信来源为 AstrBot 当前事件、私聊、完整 message/session/scope/target、非主动发言、简单纯文本、无引用、无需联网/工具/任务/多模态”的条件时接管。

群聊、群友、主动发言、引用消息、缺失目标、联网/资料查询、复杂任务、多模态和未匹配到独立人格包的轮次不会部分进入 v2，而是整轮保持 shadow 并继续旧链路。没有直接全群开启。

## 根因与本层修复

此前 `architecture_v2_mode=enabled` 永远以 `activation_ready=False` 解析，因此只能 shadow；同时生产 `build_persona_reply()` 仍先调用旧 Planner，typed context/plan 只做比较，ReplyComposer、OutputValidator、发送后 typed ledger 都没有形成一条真实生产链。

本层完成以下根因修复：

1. 新增 fail-closed `ArchitectureV2RolloutGate`，只接受事件内结构化身份，不接受昵称、自称、引用关系或 Prompt 推断。
2. 增加唯一发布配置 `architecture_v2_rollout_scope=off|owner_private`；默认仍为 `off`。
3. 对符合条件的简单主人私聊，使用本地 `LocalChatPlan`，不调用旧 Planner；由 AstrBot 原有最终 Provider 调用承载一次 typed ReplyComposer 生成，不另起第二条发送链。
4. ReplyComposer 只接收明确属于当前对象的 typed thread、当前主体高置信事实和公共来源事实；他人事实只进入禁止片段校验，不进入生成 Prompt。
5. 响应改走 `OutputValidatorV2`；有效结果不进行常规口语化重写，严重违规最多一次无工具 repair，第二次仍失败即闭锁。
6. 最终语义/情绪形成 typed presentation handoff；本首批范围不强制调用第三方表情工具，群聊现有表情路径未改变。
7. 私聊和群聊的真实成功发送现在都会写入 `ConversationLedger.record_outbound()`；发送失败或未确认不会写成功记录。
8. 需要联网的语句增加保守识别，避免主人私聊被错误归入零工具快路径。

## 范围与回退矩阵

| 场景 | 结果 |
| --- | --- |
| 可信主人、私聊、简单纯文本、完整当前目标 | 完整 typed Composer/Validator/发送记录 |
| 主人群聊 | shadow + 旧链路 |
| 群友私聊或群聊 | shadow + 旧链路 |
| 昵称/自称主人、伪造 principal 来源 | 拒绝激活 |
| 主动参与/安静话题 | 拒绝激活 |
| 引用旧消息、缺 message/session/scope/sender | 整轮回退 |
| 联网/搜索/实时资料、复杂任务 | 整轮回退，保留原 Agent/工具链 |
| 图片、音频等多模态 | 整轮回退 |
| 当前 `persona_name` 没有匹配的人格包 | 整轮回退，避免误套 ATRI |

## 测试证据

- 新增 `tests/test_architecture_rollout.py`，覆盖 scope off、可信 owner/private、owner/group、guest/private、ambient、伪造来源、principal/envelope 错配和缺目标。
- 生产管线回归覆盖：零旧 Planner、无独立发送、双气泡只形成一个逻辑回复、私聊发送后记录、跨用户历史/记忆剔除、联网整轮回退、引用/缺目标回退、最多一次无工具修复。
- 相关定向测试：`161` 项通过。
- 完整测试：`508` 项运行成功，`505` 项通过，`3` 项为计划保留到旧链路清理阶段的 v1 `expectedFailure`。
- `compileall`、配置 JSON 解析、89/89 字段审计和 `git diff --check` 通过。
- 未执行 `git add`、`git commit`、`git push`、PR、合并或 Release。

## 生产预检与备份

- `trim-cli` Docker 请求仍返回 FNOS 设备码 `135168`；按技能回退到现有 SSH 登录态。
- 写前：容器 running、RestartCount 0、WebUI HTTP 200、星汐 `0.4.6`、最近 30 分钟 0 Traceback/0 ERROR。
- 插件备份：`/AstrBot/data/backups/shio/astrbot_plugin_shio-P8-08-preenable-20260817T142251Z`
- 配置备份：`/AstrBot/data/backups/shio/astrbot_plugin_shio_config-P8-08-preenable-20260817T142251Z.json`
- 插件源/备份：均 108 个实际文件，完整内容 manifest 均为 `c4f0e7c10eff3a27462e3240a73144ad6c7d88d5e289024b239b6aca4caa750c`。
- 配置源/备份旧哈希：均为 `5eff20961506a8658ec234bf67ed928325510b89e7c1347a86152cd5b6023ea4`。
- 首次备份命令因远端引号把目标路径包入空格而在 `cp` 创建目录前失败，线上未改变；随后使用显式无空格绝对路径重试并完成上述复核。

## 最小部署

只更新以下 5 个星汐运行文件：

| 文件 | 线上 SHA-256 |
| --- | --- |
| `main.py` | `a994a610e20c7d67adf7aa2dcd6bf15eafdcd45f13531e889589d1f7f9eeb911` |
| `_conf_schema.json` | `6ea085e42a68a30ba18deda9174b362fe334afbae4b2f5dad349d2547c87a1dc` |
| `core/architecture_mode.py` | `05334c86a5980e59948a8bc7b36da26c61993a11c86cb1d2e1c3d32a0d43b4b6` |
| `core/fast_path.py` | `2af51f5b285d5dcdddff7c16403ef2d8af92f5c2bc065783594001bbce99d1d5` |
| `core/reply_composer.py` | `4947b5e57ff0659626e64976d9a31c3d60f575e958089b76d4736ca0edc61855` |

Windows → FNOS host → 容器暂存区 → 线上路径四处逐文件哈希一致；容器内 4 个 Python 文件编译通过。暂存区的 `__pycache__` 没有部署。星汐配置只改变 `architecture_v2_mode=enabled` 和 `architecture_v2_rollout_scope=owner_private`，保留 UTF-8 BOM；新配置哈希为 `bd9456e18c50d310553b8fd2c944d511baee224bf4f120d8fe1aa50878a7c362`。

## 重载验证

- 重启前 StartedAt：`2026-08-17T13:52:00.075383246Z`。
- 重启后 StartedAt：`2026-08-17T14:24:59.108734467Z`。
- 状态：running=true、restarting=false、OOMKilled=false、dead=false、RestartCount=0。
- 星汐 `0.4.6` 加载 1 次，`refresh_provider_selectors` hook 运行 1 次。
- 启动后 0 Traceback、0 ERROR、0 raw group ID、0 raw sender ID。
- WebUI HTTP 200，响应长度 4128。
- 容器内合成对照：owner/private gate=true，owner/group gate=false，完整 ready 时 effective mode=enabled。
- 回滚未触发。

## 仍需观察的边界

没有伪造一条真实用户消息去冒充线上主人，因此本层证明了代码、容器加载、配置和合成运行门控，尚未把“第一条真实 owner/private 最终回复”写成已观察事实。下一入口必须只读查看重启后新日志并累计真实样本；没有样本就不改代码。

群聊目前仍走旧链路，旧相邻归属 bug 也因此尚未从群聊生产路径删除。必须先满足 owner/private 观察门槛，再设计显式群范围的下一批 rollout；在群范围迁移完成前禁止执行 P8-09 删除旧链路。

本轮暂存目录清理命令被本机执行策略拒绝，未强行绕过；它们不在 AstrBot 插件扫描路径中：

- Windows：`C:\Users\45928\AppData\Local\Temp\shio-p8-08-20260817T142403Z`
- FNOS host：`/tmp/shio-p8-08-20260817T142403Z`
- 容器：`/tmp/shio-p8-08-20260817T142403Z`

## 回滚边界

出现认错人、跨主体记忆、协议泄漏、双发、连续空回复、启动错误或 owner/private 工具任务被误接管时，恢复本报告的插件备份与配置备份，重启 `astrbot`，并重新验证 StartedAt、哈希、WebUI 和加载日志。不得只切配置却保留无法解释的代码分叉。
