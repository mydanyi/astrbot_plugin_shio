# P10-06 FNOS 精确部署、自动回滚与线上验收

## 结论

P10-06 的生产部署与自动验收已完成。FNOS 上的星汐已经从 0.4.6 / P3 更新为 P10-05 冻结的 0.5.0 候选；95 个源文件逐项匹配 manifest，线上配置从 32 项迁移为 48-field closed schema，高风险 owner/proactive 开关全部保持关闭。

部署过程中第一次写后验收真实触发自动回滚；回滚恢复了旧插件、旧配置、容器和 WebUI。根因不是候选启动失败，而是验收脚本错误地把新进程正常生成的 `__pycache__/*.pyc` 当成旧运行分支。修正为“95 个源文件必须精确一致，额外文件只允许闭集 pyc”后第二次部署通过。两次尝试都有 durable phase journal，断线后可恢复判断，不依赖当前 Codex 会话存活。

## 写前备份

时间戳：`20260819T032503Z`

- 插件备份：`/vol3/1000/Docker/Astrbot/data/backups/shio/astrbot_plugin_shio-P10-06-pre-20260819T032503Z`
- 配置备份：`/vol3/1000/Docker/Astrbot/data/backups/shio/astrbot_plugin_shio_config-P10-06-pre-20260819T032503Z.json`
- 插件备份文件数：150；与写前 live 目录 `diff -qr` 一致；
- 备份 `main.py` SHA-256：`583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`；
- 原配置/备份 SHA-256：`6A5ABBE43DE367AE26E35548B9875E0A48C1A896B5A6539BE114B0C3C01ED2C9`；
- 插件、配置、备份目录与 staging 全部位于同一 filesystem device，目录和配置交换使用同设备 rename。

备份放在 `/AstrBot/data/backups/shio` 对应 host 路径中，不在插件扫描目录。

## 可中断部署事务

远端部署脚本先生成同 filesystem staging，再停止容器并按阶段写入：

`PREPARED → CONTAINER_STOPPED → OLD_PLUGIN_SAVED → NEW_PLUGIN_LIVE → OLD_CONFIG_SAVED → NEW_CONFIG_LIVE → CONTAINER_STARTED → VERIFIED`

每个 phase 使用临时文件、`sync` 和 rename 更新到备份目录。`EXIT/HUP/INT/TERM` 或任一命令失败时，脚本会停止容器、恢复 old plugin/config、重新启动并等待 WebUI。脚本通过 `nohup` 独立于本会话运行。

持久化状态：

- 首次尝试：`P10-06-deploy-20260819T032503Z.state` → `ROLLED_BACK`；
- 正式成功：`P10-06-deploy-20260819T033100Z.state` → `VERIFIED`。

## 首次尝试与回滚证据

首次候选已成功启动：WebUI 200、候选新时间窗 0 Traceback、0 ERROR、两类旧 owner-action error 均 0、插件加载 marker 4。写后源文件检查却看到 181 个文件：95 个候选源文件全部存在且摘要正确，另外 86 个均为 Python import 正常生成的 pyc。

旧验收断言要求“运行中目录总文件数仍等于 95”，因此主动失败并进入回滚。回滚后：

- `main.py` 恢复为 `583BF681…DB5C`；
- metadata 恢复为 0.4.6；
- 配置恢复为 `6A5ABBE4…ED2C9`；
- container running=true、RestartCount=0、WebUI 200；
- old plugin/config swap 均已复位；候选失败副本在最终成功后按精确备份路径清理。

这证明回滚链不是纸面方案，已经在真实写后失败中执行成功。

## 正式部署结果

第二次尝试把验收规则修正为：

- manifest 内 95 个源文件必须全部存在，字节数和 SHA-256 必须逐项一致；
- manifest 外只允许路径中含 `__pycache__` 且后缀为 `.pyc` 的运行派生文件；
- 任何旧 `.py`、备份、文档、测试、脚本或其他文件仍会失败并回滚。

当前线上事实：

| 项目 | 值 |
|---|---|
| metadata | `0.5.0` |
| source files | 95/95 exact |
| derived pyc | 86，闭集允许 |
| `main.py` SHA-256 | `E7A30D51CD7C8B22CB90EADDC55BF3CA6B3A3671FD18C3912C68020006B4A875` |
| `_conf_schema.json` SHA-256 | `D5551F575C81E9FD626001D8760221E492048F1CD522FB78C76DAC7072AB39AF` |
| `metadata.yaml` SHA-256 | `EC0A6F27400738444D397005197327648FBDABECC916B61D2716B42C803142D0` |
| StartedAt | `2026-08-19T03:32:27.392566129Z` |
| container | running=true, restarting=false, OOMKilled=false, RestartCount=0 |
| WebUI | HTTP 200 |

线上不再有 P3 的 74 个候选外源文件；运行时 pyc 不被错误解释为旧代码分支。

## 配置迁移

迁移完全在 staging 内完成并先由 host Python 和 AstrBot Python 3.12 双重解析／import：

- 32 个旧字段中仍在 schema 的有效值原样保留；
- 17 个新字段使用候选 schema 的 exact typed default；
- 删除唯一 orphan `owner_chat_prefixes`；
- 结果严格等于 48 个 schema key，顺序与 schema 一致；
- owner action 总开关、四个 adapter 开关和 proactive 总开关全部 false；
- proactive allowlist 仍为空；
- 没有输出、改写或记录 owner ID 的实际内容。

线上新配置：

- field count：48；
- SHA-256：`0BD94B0A218A3D6381C47480EFCD58890A3713B1F65B36093D852B89DBE16A72`；
- mode：`0600`，owner `root:root`（原文件为不必要的 `0755`，本次收紧为仅 owner 可读写）。

## 线上实际副本测试

不是只复用上传前源码。验收从 `/AstrBot/data/plugins/astrbot_plugin_shio` 复制当前 95 个真实源文件到容器临时目录，再加入冻结的 tests/scripts/docs 作为 harness：

- fixed capability/outcome runner：24/24；
- Persona A/B runner：6/6；
- plugin/gate/multimodal runner：29/29；
- AstrBot Python 3.12 full：`1255/1255`；
- 当前 live `main.py` 摘要再次确认与候选一致。

第一次组装 harness 时目录名不是 `astrbot_plugin_shio`，runner 在执行任何测试前以 `p10_test_resolution_failed` 关闭；改为正确包层级后全部通过。该错误只存在于 `/tmp` 验收副本，没有改代码或线上插件。

## 新启动窗日志

正式 0.5.0 StartedAt 之后：

- Traceback header：0；
- 通用 ERROR pattern：0；
- plugin load marker：4；
- `owner_action.denial_prepare_failed`：0；
- `owner_action.delivery_finalize_failed`：0；
- typed prepare/fail-closed/state-missing/blocked/repair/provider/final-semantic/non-typed error：均 0；
- `send.manual_failed`：0。

观察窗口尚无自然消息：`typed_reply.prepared=0`、`send.succeeded=0`。本阶段没有伪造用户消息、没有向群里主动发测试内容。因此这两个 0 只能说明“暂无样本”，不能冒充真实对话效果已经验收。

## 清理

- host/container `/tmp` 的 P10-05/P10-06 source、candidate、deploy 和 live-test staging 已按精确路径删除；
- 首次失败候选副本已删除；
- 两个 phase journal 与正式写前插件／配置备份保留；
- Windows 最终候选 ZIP + manifest 继续保留在 P10-05 报告所列目录；
- 未修改 AstrBot core、LivingMemory、Meme Manager、其他插件、Compose、Provider 或 Git。

## 中断恢复与唯一下一入口

P10-06 已完成代码级和运行级自动验收。若之后线上异常，可用本报告的 150-file plugin backup 与原配置恢复，并以 state journal 确认上次事务结果。

唯一下一入口是 **P10-07 最终综合验收与提醒门**：

1. 最后一次只读核对 0.5.0 哈希、48-field all-off 配置、备份、journal、container/WebUI 与新日志；
2. 汇总 P4～P10 全部报告和当前已知限制；
3. 明确 O1 尚未执行，并只向用户提供选择，不自动修改 LivingMemory 或创建 PR；
4. 到这一门才请用户做一次统一效果测试，补足当前 0 条自然流量样本。

中断后从本报告与 `SHIO_MASTER_PLAN.md` 第 12 节继续，不重做 P4～P10-06。
