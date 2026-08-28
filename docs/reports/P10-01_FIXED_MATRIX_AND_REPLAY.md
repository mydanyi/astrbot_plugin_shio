# P10-01 固定能力矩阵与脱敏运行回放

## 结论

P10-01 已完成。固定矩阵覆盖 `CapabilityClass` 全部 11 个 ID、`ProductOutcome` 全部 9 个终态，并把每一行绑定到当前仓库内一个可执行的生产热路径回归。可重复 runner 在 Windows 与隔离 AstrBot Linux container 均输出：11 capabilities、9 outcomes、19 evidence tests、合计 24 tests，全部通过且报告只含计数。

本阶段没有部署候选。线上只做只读日志观察；原始日志没有复制进仓库，fixture 只保存白名单字段、枚举、分桶和聚合计数。

## 固定能力矩阵

| Capability | 群友策略 | verified owner policy | 当前 production disposition |
|---|---:|---:|---|
| `public_web_read` | exact configured + attested source 可用 | policy 可用 | current knowledge gap 的 sealed AnySearch |
| `chat_retrieval` | exact configured + attested source 可用 | policy 可用 | LivingMemory typed projection |
| `local_presentation` | exact configured + attested source 可用 | policy 可用 | presentation-only Meme path |
| `media_generation` | 拒绝 | policy 可用 | 不向 Persona Renderer 暴露 |
| `memory_write` | 拒绝 | policy 可用 | owner adapter 默认全关 |
| `artifact_read` | 拒绝 | policy 可用 | owner adapter 默认全关 |
| `artifact_write` | 拒绝 | policy 可用 | 当前无 owner operation |
| `shell_exec` | 拒绝 | policy 可用 | 代码级 hard-disabled |
| `device_control` | 拒绝 | policy 可用 | 不向 Persona Renderer 暴露 |
| `agent_full` | 拒绝 | policy 可用 | 不向 Persona Renderer 暴露 |
| `unknown` | 拒绝 | policy 可用 | production route 拒绝 unknown |

这里刻意区分“CapabilityPolicy 分类/身份策略”与“production execution route”。owner policy 允许某 capability 不等于 Persona Renderer 会拿到该工具，也不等于 adapter 已启用。

## 脱敏运行回放

- 固定 `ProductTrace` 回放覆盖 `accepted/dropped/no_action/reacted/sent/blocked/cancelled/degraded/failed` 九个终态；stage 顺序、必需终态证据、闭集 status/reason 均由真实 ProductTrace 合同重验。
- 每个 replay case 关联一个现有热路径测试，覆盖 current direct reply、self drop、wait/no-tool、local reaction、exact send receipt、协议阻断、stale cancellation、LivingMemory 降级与 Provider failure。
- 近 168 小时 FNOS 只读聚合观察到 `pipeline.metrics=58`、`send.succeeded=110`，抽取的安全 shape 包含一次 27-stage/2-send 成功链和一次 2-segment send；所有 trace、subject/target digest、正文、路径和时间戳均删除。
- 同一窗口观察到两条旧 production owner-action 错误类别：`denial_prepare_failed/RuntimeError` 与 `delivery_finalize_failed/ContractViolation`。本地候选分别有 canonical denial 与 durable delivery 回归，但在 P10-06 部署并观察新时间窗之前，不宣称线上错误已经消失。
- 全 AstrBot container 同窗口有 `792` 个 Traceback header。该数字横跨所有插件与历史过程，不能归因为星汐；P10-03/P10-06 将以候选部署时间为界，只核对新产生且能归属星汐的异常。

## 可重复入口

```powershell
python -X utf8 astrbot_plugin_shio/scripts/run_p10_fixed_matrix.py
```

输出固定为 content-free JSON。当前结果：

```json
{"capability_count":11,"error_count":0,"evidence_test_count":19,"executed_test_count":24,"failure_count":0,"passed":true,"privacy":"content_free_counts_only","runtime_outcome_count":9,"schema_version":1,"skipped_count":0}
```

## 验证

- P10 matrix 合同：`5/5`。
- 固定 evidence suite：`19/19`。
- runner 聚合：`24/24`。
- Windows full：`1232/1232`，skipped 7。
- 隔离 AstrBot Linux container runner：`24/24`；full：`1232/1232`。
- `py_compile`、merge-marker、privacy scan、`git diff --check`：通过。
- 候选归档：`C:\Users\45928\AppData\Local\Temp\shio-p1001-20260819-v1.tar`，SHA256 `B86110CC2F892A995F1EF80B3CFAE06D3F639B42B6C1335DDD219EC494967E59`。
- FNOS staging 已清理；production container 继续 `running=true/restart=0`，没有覆盖、reload 或 restart。

## 实现资产

- `tests/fixtures/p10/acceptance_matrix.json`：11 capability、9 terminal replay、production safe aggregates。
- `tests/test_p10_fixed_matrix.py`：strict schema/privacy、真实 classifier/policy、evidence resolution、ProductTrace replay。
- `scripts/run_p10_fixed_matrix.py`：固定 suite 与 content-free 计数报告；内部测试输出被隔离，不把路径或异常正文带进结果。

## 边界

- 本阶段证明固定矩阵、运行合同和脱敏 replay 可重复，不代替多人格语言质量评测。
- production 日志观察来自当前 P3 线上版本；P10 候选尚未部署。旧错误类别只有在 P10-06 新时间窗为零后才能判生产修复。
- owner adapter 继续全关；没有修改第三方插件，没有 Git 写操作。

## 下一入口

唯一下一入口是 **P10-02 多人格 A/B 评测**：用同一组事实/意图/关系/情绪输入分别运行 primary、warm、calm、neutral Persona，验证事实与 action 不漂移、表达差异明显、中文自然且不退化成通用傲娇模板。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复。
