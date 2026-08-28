# P9-04B 重启状态连续性

## 结论

P9-04B 已完成 Windows 与隔离 AstrBot Linux container 验证，P9 至此全部完成；候选未部署生产。

星汐现在把 conversation revision 与未点名参与节奏保存为 code-owned、HMAC 封印、容量有界的最小状态。重启后的第一轮群消息能从持久 revision 继续，Affect 与 GroupScene 不会因内存冷启动把合法下一轮误判为 gap；同一群友的参与冷却和退避也按墙钟经过时间恢复。状态损坏、安装密钥漂移、热运行文件替换或容量耗尽时，未点名参与失败关闭，但当前消息的明确直答仍可继续。

P7 的 proactive cooldown/day/replay 继续使用原有唯一持久真相源；send receipt 和性能窗口明确保持进程内：新实例不能确认旧发送 segment，性能统计从空窗口重新开始。没有新增第二套主动策略状态。

## 正式红灯与实现期回归

- 初始红灯为 `core.runtime_continuity` 不存在；新增 focused 测试先以 `ModuleNotFoundError` 失败。
- Windows 实现后 focused `8/8`、相关 `181/181`、完整发现通过。
- 隔离 Linux 首次完整发现暴露真实 POSIX 原子写边界：`os.replace()` 已成功后抛出 `KeyboardInterrupt` 时，旧实现尚未来得及把新 state 文件收紧为 `0600`，重启会安全拒绝但无法恢复已提交 revision。
- 根修把权限放到 publish 之前：临时文件改为随机名称、`O_EXCL` 独占创建、创建时即 `0600`、flush/fsync 后才 replace；晚中断后重启能读取已提交 revision，并拒绝从旧 base 重放。
- 补充 send/performance 重启边界后，Windows 与 Linux 最终均为 `1227/1227`。

## 实现

### 1. Privacy-minimal continuity store

- `RuntimeContinuityStore` 只落盘域分离 HMAC scope 指纹、scope+sender 指纹、revision、join age、no-action streak 与 backoff remaining；不落盘群 ID、用户 ID、消息 ID、正文、trace、路径或 Prompt。
- state 使用安装密钥的 whole-state HMAC；重复 JSON key、非有限数值、未知 schema、超限条目、密钥丢失/替换和非普通文件均失败关闭。
- 默认上限为 2048 scopes / 256 subjects，运行时硬上限 8192；满时不驱逐既有 continuity，不为新未点名参与放宽策略。
- 同进程热重载使用 exact root owner；新实例在旧实例关闭前保持 standby，旧实例关闭后才重新加载并接管。

### 2. Revision 与冷启动 bootstrap

- `ConversationRevisionBook` 首次访问 scope 时从 store 懒恢复 revision；accepted commit 先尝试持久化，再更新本地 revision。
- persistence 不可用时当前明确消息仍在本进程内推进，避免状态文件故障把整台机器人变成零回复；同一 store 状态会让未点名参与立即转为 WAIT。
- `GroupSceneBook` 与 `AffectStateBook` 只在内存尚无 scope state、且当前 revision 恰为 restored+1 时建立空 baseline；后续 gap/stale 规则不放宽。

### 3. Participation cadence

- `ParticipationCadenceAuthority` 按 exact scope+sender 恢复 join window、cooldown 与 exponential no-action backoff。
- 持久化使用 join age/backoff remaining 加 wall-clock elapsed 重建到新进程 monotonic clock，不比较两个进程不可比的绝对 monotonic 数值。
- persistence 不可用、容量满或 commit 失败时，MAY_JOIN/NO_ACTION 路径统一 WAIT；MUST_REPLY 保持 current-message direct reply。

### 4. 其他重启边界

- proactive observation/cooldown/day/replay 仍由 P7 `ProactivePolicyState` 持久化，已有重启、密钥漂移与损坏 fail-closed 回归。
- `InternalSendReceiptLedger` 不持久化；fresh ledger 对旧 segment 只返回 unknown，不能把重启前未确认发送补记为成功。
- `PerformanceWindow` 是有界、content-free 的运行期窗口；fresh process 从零计数，不把旧进程延迟样本伪装成当前健康度。

## 测试证据

- continuity focused：`8/8`。
- continuity + proactive + send restart + performance restart：`37/37`。
- Windows 完整发现：`1227/1227`，skipped 7。
- 隔离 AstrBot Linux container：首次 `1226/1227` 暴露 POSIX mode ordering；根修后 targeted `37/37`、完整发现 `1227/1227`。
- `compileall`、JSON schema parse、merge-marker、trailing-whitespace 与 `git diff --check`：通过。
- 候选归档：`C:\Users\45928\AppData\Local\Temp\shio-p904b-20260819-v3.tar`，SHA256 `12B059A8E0906B3775549AC6B0DD4C250B2816A4534341DE7F5A9991406E66A0`。
- FNOS staging 已清理；production 未覆盖、未 reload、未 restart。只读复核为 container `running=true/restart=0`、WebUI HTTP 200、线上 `main.py` 仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`。

## 冻结哈希

- `core/runtime_continuity.py`: `C9AE2298F8B49DE665F3CBC5F6725E5C152A2BF28FB1966CDDD0E195C3177B20`
- `core/conversation_event.py`: `33DE22DCAAD64448C2241753E81AB22CD373BFC361F9AE9D9296DD49AA0D2648`
- `core/participation_cadence.py`: `72EA67FB9B105B662C6C6F685535DFF664C8B885169D02B00FF70A1E031C9FA4`
- `core/group_scene.py`: `9D411E9154602650E55A53717CFE125A1E2E0C016A89C70511E6C40E238C9CAA`
- `core/affect_state.py`: `ED6DFD06F00825AFB09A4EA02B781D5A016B8843DA8733A6CA0D096FE06C0AF7`
- `main.py`: `72FB7C47C2B07F334CE3E6F3DFABDFB3A7ADA930D02AEB4635C7AB1BE2929F9C`
- `_conf_schema.json`: `F5C5E443ED5396CA84DD43D116F5DDE9BAD377827CE492EB0F18157A7A7C41C3`
- `tests/test_p9_runtime_continuity.py`: `B6B24C0B5026AD87881F592119C79D6C4B2399FC876684CB40EF0317620A2622`
- `tests/test_send_receipt.py`: `AC09DF13D1E5FA4383E11ACA0CF561826E9BF5DE66A1076C316E68B5377DA588`
- `tests/test_p9_performance_metrics.py`: `03DFFB1FFCFA9F476E77B2CC73D938DCF09B8CB5A35ED6748B7DB2E71EC55B12`

## 边界

- continuity store 的 writer 排他覆盖同一 AstrBot 进程内的正常加载与热重载；P10 生产验收必须继续确认同一 data root 只有一个 AstrBot plugin process，不把本模块描述为通用多进程数据库。
- 持久化 I/O 完全失败时，不可能同时承诺“当前明确消息一定回复”和“该轮 revision 一定跨 crash 落盘”；当前产品选择是明确直答可用、未点名参与 fail-closed，并用 message/binding lineage 防止仅凭 revision 取得 authority。
- owner action adapter 仍全关；未修改 AstrBot core、LivingMemory、Meme Manager 或其他插件；未执行 Git 写操作。

## 下一入口

唯一下一入口是 **P10-01 固定矩阵与脱敏真实日志回放**。P10 将在同一候选上依次完成固定能力矩阵、多人格 A/B、生产插件共存/多模态、README/schema/metadata、Windows/container/线上完整门、FNOS 备份部署与自然流量验收。只有 P10 综合门完成后才请用户统一测试效果；中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P9。
