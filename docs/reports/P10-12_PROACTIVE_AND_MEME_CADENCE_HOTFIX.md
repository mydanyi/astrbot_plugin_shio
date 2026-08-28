# P10-12 主动终态与 Meme 会话节奏热修

状态：完成并部署（0.5.5）。

## 1. 真实流量结论

- 主动对话并非完全没有发送。生产平台日志证明 22:48:39 向一个已配置白名单群成功发送，约四秒后同群成员产生新消息。当天该群日限额为 1，且活跃窗口在 23:00 结束，所以之后没有第二次主动发送属于策略结果。
- Shio 当时的 `proactive.terminal` 却显示 presentation noncanonical、segment count 0、send unauthorized。根因是成功发送后先更新 group scene revision，再调用旧 presentation 的默认 trace inspector；旧 scene-bound presentation 因 revision 前移而正确失效，却被错误当成发送当刻状态记录。
- 线上两轮自然文字回复都产生 `meme.complement_decision`，分别因严肃／请求语境和没有补图 cue 而在执行器之前拒绝。旧普通路径对每条消息独立按 content digest 固定 1/8，连续聊天不会累计，因此用户多轮对话仍可能始终不可达。

本报告不记录真实群号、用户 ID、消息正文、模型文本或 Meme 查询。

## 2. 红灯与根修

### 主动终态

正式回归先要求成功发送日志必须同时包含 `outcome=sent`、发送当刻 canonical presentation、授权成功和精确 segment count；旧实现因没有闭集 `outcome` 直接失败。

修复后，`complete_send()` 返回后立即获取一次 immutable terminal metadata，再更新 group scene；结构化日志改用 observability allowlist 支持的 `outcome` 字段。场景前移不再改写真实发送当刻的诊断事实。

### Meme cadence

正式回归先证明第四个同 scope／同 sender 的安全普通回合仍返回 `no_complementary_cue`。根修引入 long-lived `MemeComplementCadence`：

- 同一 scope、同一 sender 连续四个安全普通回合可获得一次 `conversation_cadence_complement`；
- 触发后四个安全普通回合处于 `cadence_cooldown`，不连续补图；
- 请求、严肃、医疗、风险、攻击或辱骂语境重置 cadence 并继续 fail closed；
- 不跨 sender 累计；旧 revision／replay 不推进；容量固定有界；trace 只输出计数和阈值；
- 明确玩笑、双关、接梗和轻量 cue 继续走 0.5.4 已冻结的即时准入，不削弱安全硬门；
- 模型仍无 Meme 工具，实际图片仍须经过 exact plan、expression、generation、runtime、presentation 与一次性 send authority。

## 3. 验证

- 新增关键回归：`3/3`，0.052 秒；Meme + proactive 模块：`30/30`。
- release/candidate/Meme/proactive/main pipeline：`127/127`。
- Windows full：`1263/1263`，skipped 7，53.680 秒。
- 隔离 FNOS AstrBot Python 3.12 candidate full：`1263/1263`，46.957 秒；candidate verifier 与 compileall 通过。
- 部署后从生产 live 95-file 副本重建测试树：`1263/1263`，47.187 秒；compileall 通过。
- deterministic candidate：95 files；ZIP SHA256 `BA13A52DBFEE315C6E25E8C28E59BED55C6F050E32EFA5F637EB4A5BA399A8EC`；manifest SHA256 `1578C8816AD7758098A66625C7BC7F88186FEBA4504C8A180C0CEE534F2484A4`。
- 本地 compileall、candidate verifier、`git diff --check` 与目标文件尾随空白检查通过。

## 4. 生产部署

- 写前备份：`/AstrBot/data/backups/shio/P10-12-proactive-meme-cadence-20260819T170853Z`（host 对应同名目录）。
- 备份含完整旧插件副本、原 live 原子移出目录、配置副本、候选 ZIP／manifest 与 durable deploy state；失败路径会自动恢复旧 live 并重启。
- 0.5.5 从同 filesystem 已验签 staging 整体替换；live manifest 95/95 source exact，额外 86 个文件全部为 `__pycache__/*.pyc`。
- 配置 SHA256 部署前后均为 `09528ABB3BDB988EA50DC515E5085EC3AE642DAB7C702E8F6807EF557C62D769`；48 fields、主动总开关仍为 true、白名单仍为 2 项，未输出或改写其实际值。
- StartedAt：`2026-08-19T17:09:03.875825593Z`；running=true、restarting=false、OOMKilled=false、RestartCount=0；WebUI HTTP 200。
- 新启动窗口：0 Traceback、0 ERROR；0.5.5 load marker 1；`proactive.scheduler_started` 1；Meme/proactive 初始化失败 0。
- 未修改 AstrBot 核心、全局 Provider 回退、LivingMemory、Meme Manager、资源包、Docker Compose 或其他插件。

## 5. 运行边界与中断恢复

部署后的真实自然流量已经完成闭环验收：同一 scope／sender 的前三个安全普通回合均为 `eligible=false / no_complementary_cue`；第四回合在 01:13:28 精确转为 `eligible=true / conversation_cadence_complement`，01:13:31 产生 canonical `meme.presentation_terminal`：`succeeded / image_sent / attempt_count=1 / success_count=1`。NapCat 同秒真实平台出站日志包含独立图片消息；随后四轮均为 `cadence_cooldown`，没有重复补图。报告不记录真实群名、群号、机器人账号或消息正文。

若会话中断，从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复。回滚时停止容器，把当前 live 移到备份内新的保留目录，再将本报告备份下的 `live-moved-original/` 原子移回并重启；配置无需迁移。O1 仍未激活。
