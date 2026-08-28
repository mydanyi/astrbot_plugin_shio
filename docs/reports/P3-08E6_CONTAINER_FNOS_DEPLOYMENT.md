# P3-08E6 Container and FNOS Deployment

## 1. 结论

P3-08E6 首次自动化部署只通过了静态、加载与 WebUI 门，**没有通过真实消息端到端门**。2026-08-19 用户验收时确认机器人无法回复；事故检查发现每条真人消息都被 ReNeBan conformance gate 以 `interface_changed` fail-closed 丢弃。已执行可逆回滚、定位根因、加入回归并重新部署精确修复；修复后一条真实普通消息已完成 typed reply、两段发送与 pipeline sent 闭环。其余 AnySearch 与 owner-action all-off 场景仍需分项验收：

- Windows、WSL 和隔离 Linux 容器的完整 `1055` 项测试全部通过；
- 最小生产包在 Python 3.12 容器内解包、结构检查、全包编译通过；
- FNOS 先做只读版本、配置、源哈希与运行状态检查，再完整备份线上星汐目录和配置；
- 仅替换 `astrbot_plugin_shio`，没有修改 AstrBot 核心、LivingMemory、Meme Manager 或其他插件；
- AstrBot 容器重启后从正式路径加载星汐，WebUI 可访问，schema 显示五个 owner-action 开关全部默认关闭；
- 本地生产包与线上 81 个非 bytecode 文件的内容清单摘要完全一致；
- 首次检查只匹配了 `[ERROR]`，遗漏 AstrBot 实际使用的 `[ERRO]`，因此原“错误行数为 0”结论撤销；后续检查必须同时覆盖 `[ERRO]`、`[ERROR]`、critical、traceback，并验证真实 ingress/send trace。

交互式对话效果由用户下一步在真实私聊／测试群验收。本报告不把“加载成功”冒充用户体验验收。

## 2. 本地与隔离容器

### 2.1 本地

- Windows full：`1055/1055`，skipped `7`。
- WSL Python 3.12 full：`1055/1055`，无 skip。
- `compileall` 与 `git diff --check`：通过。

### 2.2 隔离 Linux 容器

第一次复用本机既有镜像时，镜像只有 Python 3.10，`dataclass(..., weakref_slot=True)` 在导入阶段失败。项目与 AstrBot 目标明确要求 Python 3.12+，因此没有降级代码或加兼容旁路。

随后使用官方 `python:3.12-slim`，运行约束为：

- non-root `65534:65534`；
- root filesystem read-only；
- source bind mount read-only；
- `--network none`；
- 4 CPU、4 GiB memory、512 pids；
- 仅 `/tmp` 为受限 tmpfs。

同一 full discover 最终 `1055/1055` 通过。

## 3. 生产包

最小包只含顶层 package、`main.py`、metadata/schema、persona assets、`core/` 和公开说明文件；不含 tests、Git metadata、`__pycache__` 或 `.pyc`。

- archive：`astrbot_plugin_shio_e5_20260818.tar.gz`
- bytes：`375179`
- SHA-256：`047AE51285D747B4651406E82FACEF9C072F1099F8A4D86BF3A9ABCB9468A775`
- production files：`81`
- packaged `main.py`：`3D457E3643E4A33059478C8BD6481DCFF451F9431E8920F5088E9EB998F627F0`
- packaged schema：`AA93A22C1C4BAF86964E6A3AEB3011BA3DE2F9D51D407DEC25E6442DD136A6DB`
- unpacked package compile：通过。

## 4. FNOS 只读预检

目标：`192.168.50.38`，AstrBot container `astrbot`。

- FNOS host：`MINISFORUM-N5`；
- AstrBot image：`soulter/astrbot:latest`；
- AstrBot：`4.27.2`；
- Python：`3.12.13`；
- data mount：`/vol3/1000/Docker/Astrbot/data -> /AstrBot/data`；
- 线上星汐 metadata：`0.4.6`；
- LivingMemory：`2.5.7`，`memory_scope_mode=legacy`；
- AstrBot live `fs.py` SHA-256：`A710570B358BA2466F6F13C5BF2C3F6BFD60173C617AAA680EFE36AF33538BA7`；
- 旧星汐 `main.py`：`1050CFE634A740DEE9A127FBE9C8A7C7A659361193AF686AC986EF740A1E0F90`；
- 旧 schema：`D99CA3CB89E622773724A6711412189AAF7FF36FA3177463239859E79E8A6937`。

`trim-cli` 的系统请求可达，但 Docker endpoint 仍返回已知 `errno 135168`；按真机 workflow 使用已配置的 `FNOS` SSH 别名完成等价只读与部署验证。

live `fs.py` fingerprint 漂移、LivingMemory legacy scope 与 grep source-side cap 仍未过门，所以 production audited operation count 保持 `0`，四 adapter 均未启用。

## 5. 备份与部署

UTC 备份目录：

`/vol3/1000/Docker/Astrbot/data/backups/shio/P3-08E6-pre-20260818T122934Z`

其中保留：

- 完整 `astrbot_plugin_shio/` 副本；
- `astrbot_plugin_shio_config.json`；
- 原运行目录的 `live-pre-switch/`；
- 已校验的 candidate archive。

备份的 main/schema/config 哈希与写前线上值一致，旧插件文件数为 `218`。

候选先解到备份区，在真实 AstrBot 容器内从 candidate path import 成功，确认 all-off schema 后才同卷切换。切换使用目录级 rename；若 candidate rename 失败则立即恢复旧目录。切换成功后重启 `astrbot` container。

没有修改现有配置值。AstrBot 重启时按 schema 自动新增：

- 8 个 `owner_action_*` 键，五个 enabled 值全部为 `false`，root/flavor/family 为空；
- 既有 schema 中缺失的 `trusted_bot_identities` 默认键。

备份与新配置比较：removed keys `0`，changed existing keys `0`。

## 6. 线上验证

- container status：running；
- WebUI `http://192.168.50.38:6185/`：HTTP `200`；
- 未认证 status API：HTTP `401`，说明保护仍在；
- startup log：`Loading plugin astrbot_plugin_shio ...`；
- loaded plugin：`astrbot_plugin_shio (0.4.6) by Danyi`；
- live import origin：`/AstrBot/data/plugins/astrbot_plugin_shio/main.py`；
- `ActionKind.EXECUTE_ACTION`：存在；
- `PRODUCTION_AUDITED_OPERATION_COUNT=0`；
- schema owner-action keys：`8`；enabled keys：`5`；all-off：`true`；
- deployed `main.py` 与 schema 哈希分别等于 production package；
- local runtime files：`81`；remote runtime files：`81`；
- 两端 runtime manifest SHA-256：`FAD8B97410A8F93615CD5150D16AF5BE42BD3D01772D81955F0D34AD26A8A786`；
- 原重启检查误将 `[ERROR]` 当作唯一错误标签，漏掉 `[ERRO]`；本项原通过结论无效。事故修复后新启动日志无 traceback/星汐 `[ERRO]`，WebUI 仍为 HTTP `200`，但仍以真实消息闭环作为最终门。

## 7. 回滚点

若用户验收出现阻塞回归，应停止继续试验，使用本次备份：

1. 停止或重启窗口内将当前 plugin dir 移出正式路径；
2. 把 `live-pre-switch/` 原子移回 `data/plugins/astrbot_plugin_shio`；
3. 用备份 config 覆盖前先再次确认当前配置差异；
4. 重启 AstrBot；
5. 复核旧 main/schema/config 哈希和插件加载日志。

2026-08-19 已按本节流程执行一次回滚：新候选移到明确的故障保留目录，`live-pre-switch/` 恢复到正式插件路径并重启成功；没有覆盖当前配置，也没有删除任何备份。完成根因修复后，恢复版 `0.4.6` 又被保存为独立回退点，再将只修改 ReNeBan adapter 的候选原子切回正式路径。

## 8. 2026-08-19 真人消息全量丢弃事故与修复

### 8.1 现象与根因

线上每条真人消息都产生：

- `ingress.reneban_gate_degraded`；
- `plugin_status=interface_changed`；
- `handler_present=true`、`handler_enabled=false`、`handler_priority=null`；
- 随后 `ingress.dropped` / `degraded_external_gate`。

ReNeBan `v1.2.0` 的真实 ban handler 没有改名或改优先级，仍是 `filter_banned_users`、priority `114`。错误来自星汐 adapter 的候选筛选：旧实现把“module 相同 **或** handler name 相同 **或** full name 相同”的所有 AdapterMessage handler 都计入候选。ReNeBan 同一模块实际注册了多条命令与事件 handler，因此候选数不为 1，被错误判成接口变化。

### 8.2 修复边界

修复只改变候选选择：先按 exact `handler_full_name` 选中唯一 ban gate，再继续逐项校验 exact module、name、event type、enabled 与 priority `114`。它没有读取 ReNeBan 私有 ban 数据，没有放宽插件激活、handler enabled 或执行顺序要求。

正式回归新增“同一 ReNeBan 模块存在无关命令 handler”场景，先稳定得到 `INTERFACE_CHANGED` 红灯，修复后转为 `VERIFIED`。最终：adapter `10/10`、入口/生产管线 `108/108`、完整 `1056/1056`（skipped 7）通过。

### 8.3 生产恢复与当前门

- 故障候选保留于 P3-08E6 备份树，没有删除；
- 回滚后的旧线上 `main.py` 哈希为 `1050CFE634A740DEE9A127FBE9C8A7C7A659361193AF686AC986EF740A1E0F90`；
- 修复候选重新上线后的 `main.py` 哈希为 `3D457E3643E4A33059478C8BD6481DCFF451F9431E8920F5088E9EB998F627F0`；
- 修复后的 `core/plugin_adapters/reneban.py` 哈希为 `7C3AB8A4D0E9F9D975F0260D01963F60DF4C04885F967FAC9B64FFDD350BF83E`；
- 容器 running，WebUI HTTP `200`，新启动日志无星汐 `[ERRO]` 或 traceback；
- 真实普通消息于 `01:52:57` 进入 typed reply，两段均 `send.succeeded`，`01:53:00` 记录 `pipeline outcome=sent`，总延迟约 `2192 ms`；同一复核窗口星汐 error `0`、ReNeBan degraded/drop `0`。

普通消息闭环门现已通过。随后真实 AnySearch 只读查询也通过：`action_kind=use_tool`，工具调用 `1`、工具失败 `0`、GroundingFact `8`、两段均 `send.succeeded`，最终 `pipeline outcome=sent`，总延迟约 `3982 ms`。下一门只剩 owner-action all-off denial；三项没有全部完成前，不把 P3-08E6 全场景写成用户验收完成。

### 8.4 第三方 SDK DEBUG 请求体泄漏与收口

AnySearch 验收的日志复核暴露了另一个生产隐私缺口：AstrBot 4.27.2 的 root logger 与 console sink 固定为 DEBUG，但 noisy logger 集合不包含 `openai/httpx/httpcore`。因此 `openai._base_client` 把完整请求体写入 Docker 日志；系统配置 `log_level=INFO` 与星汐插件日志级别 INFO 都不能拦截这条第三方 child logger 记录。

没有修改 AstrBot 核心。星汐在插件初始化最前面将 `openai`、`openai._base_client`、`httpx`、`httpcore` 固定为 WARNING。正式测试先把四者设为 DEBUG，确认旧实现 4 个断言失败；修复后转绿。生产管线 `92/92`、完整 `1057/1057`（skipped 7）通过。仅替换星汐 `main.py` 并重启，线上新哈希为 `6DB614C9001F49616895FCC54F6E9542A10781658E2582681E14F3FC83C9BEA7`。

以新容器启动时间为边界的复核结果：OpenAI request DEBUG `0`、httpcore DEBUG `0`、星汐 error `0`、traceback `0`、ReNeBan degraded/drop `0`。重启前已写入的历史日志不会在本阶段擅自删除；后续日志留存/清理若需要执行，必须单独确认精确目标与保留策略。

### 8.5 owner-action 私聊首次命中的持久化目录权限故障

群聊中的同一读取文本于 `02:04:34` 被正确隔离为普通 `reply`，两段发送成功，未进入主人动作。随后主人私聊于 `02:12:08` 精确命中 owner-action route，但 `_prepare_disabled_owner_action_outcome()` 在创建 durable denial 前失败关闭，结构化日志为 `owner_action.denial_prepare_failed` / `RuntimeError`，因此该轮按 typed-only 合同停止传播且没有空口伪造拒绝回复。

根因不是模型、路由或 adapter：FNOS 数据卷中既存的 `owner_action/`、`lifecycle/` 与首次 lock 文件权限分别为 `0755` / `0755` / `0700`，而 durable lifecycle contract 强制专用目录 `0700`、lock/journal/anchor `0600`。部署时只验证了代码、配置与日志，没有预建并验证这组首次惰性创建的运行时文件，形成了假绿。

现场先核对两个目录均为 exact 真实目录、解析路径不漂移、owner 与容器 EUID 一致，再显式收紧为 `0700`；lock 收紧为 `0600`。随后在同一 live container 以既有 32-byte install secret 重新打开 store，得到 `enabled=True`、`failure_code=none`、`record_count=0`，并确认 journal、anchor、lock 全为 `0600`。没有打开任何 owner-action/adapter 开关，没有读取目标文件，也没有重放已结束的用户事件。

今后的 FNOS 部署门必须在真人 owner-action 验收前额外验证：专用 data root/lifecycle root 为 `0700`，install secret/lock/journal/anchor 为 `0600`，store 能以生产 secret 完成一次空账本 open/trace/close。缺任一项即不得要求用户测试，也不得把 all-off denial 写成可用。

### 8.6 all-off denial 生成失败后的确定性呈现兜底

权限修复后的第二次主人私聊于 `02:17:14` 进入 `action_kind=execute_action`，durable denial 准备成功且没有星汐 error/traceback；但首次模型输出和一次模型 repair 都没有通过 15-kind action-outcome 语义矩阵，日志终点为 `typed_reply.repair_rejected`。守卫正确阻止了错误成功/不确定措辞，却因生产链缺少 code-owned deterministic fallback 再次形成空回复。

新增正式生产复现：初始输出与 provider repair 都声称“读取成功”，旧实现稳定得到空字符串；修复后，只有 exact canonical `ActionOutcomeKind.DENIED + has_output=False` 可进入本地固定句 `这个我不能替你做，所以没动。`。该句不是绕过守卫：它占用唯一 REPAIR phase，继续经过同一 `parse_reply_composer_output`、完整 validator、Presentation、final-send seal、实际发送观察与 durable finalize。其他 outcome 仍 fail closed，不新增通用 fallback，也不调用工具或第二个模型。

验证：5 项 owner-action 发送/失败/回收定向通过；pipeline + ActionOutcome/C2 `135/135`；Windows full `1058/1058`（skipped 7）；`py_compile` 与 diff check 通过。仅替换星汐 `main.py`，旧线上文件备份为 `live-hotfixes/main.py.pre-owner-denial-fallback-20260819T0217Z`；新 live hash 为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`。容器重启后插件正常加载、WebUI `200`；部署代码又在容器独立临时进程中跑同一复现，`1/1` 通过。真实用户发送与 durable finalize 仍需下一条新事件确认，不能把旧事件重放或冒充验收。

## 9. P10 延后验收边界

本节场景是 P10 最终真人验收清单，不是 P3 完成后立即要求用户执行的前置门。2026-08-19 曾把 P3-08E6 阶段性生产冒烟误写成当前用户验收入口，导致用户在总体 P4～P9 尚未完成时被要求反复测试；这是执行顺序错误，现已撤回。P3 的真实消息只作为工程冒烟与故障发现证据，后续自动推进 P4～P9，到 P10 再统一请用户验证效果。

建议用户依次发送：

1. 一条普通闲聊，确认自然人格回复；
2. 一条需要公开资料的查询，确认既有 AnySearch 只读链；
3. **仅在已配置主人 ID 的机器人私聊**发送 `请读取 path=/srv/safe/notes.txt`，确认进入明确的“未执行／没有改动”拒绝表达，而不是声称读取成功；群聊中的相同文本只用于验证 private gate，不等价于 owner-action denial 验收；
4. 可选再发一个 Shell 或保存记忆请求，确认同样失败关闭且没有副作用。

验收前不要在 WebUI 打开任何 owner-action 开关；即使误开，当前 production allowlist 为空且 Shell 代码级 hard-off，但保持全关最符合本阶段冻结边界。
