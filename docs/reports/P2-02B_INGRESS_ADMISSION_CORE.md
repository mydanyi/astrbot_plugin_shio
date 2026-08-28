# P2-02B IngressAdmission 纯控制器报告

## 结论

P2-02B 已完成纯 `IngressAdmission` 合同与控制器。它复用 P1 `IngressDecision`、`IngressDisposition`、`SenderKind`、`ExternalPluginEvidence`，并接在 P2-01 `IngressEvent` 与 `ConversationRevisionBook` 之间；只有可信 gate 明确确认“human 且未 ban”时才提交 `ConversationEvent` 和 revision。

本子任务没有接入 `main.py`，没有建立真实 ReNeBan adapter、TrustedBotRegistry 或 AstrBot hook 顺序，因此不宣称生产准入已经切换。

## 先红后绿

- 先新增 `tests/test_ingress_admission.py`。
- 第一次定向运行因 `core.ingress_admission` 不存在而稳定失败。
- 实现后同一测试转为 `10/10` 通过。

## 决策表

| SenderKind / gate | disposition | revision |
|---|---|---:|
| human + verified + not banned | `ACCEPT_HUMAN` | 提交 |
| human + verified + banned | `DROP_BANNED` | 不写 |
| self | `DROP_SELF` | 不写 |
| known bot | `DROP_KNOWN_BOT` | 不写 |
| sealed plugin echo | `DROP_PLUGIN_ECHO` | 不写 |
| unknown automation / unknown | `DEGRADED_EXTERNAL_GATE` | 不写 |
| gate missing/disabled/timeout/error/interface changed/hook order invalid | `DEGRADED_EXTERNAL_GATE` | 不写 |

`ConversationRevisionBook.commit()` 的 `accepted` 值只来自 `IngressDecision.allows_state_mutation`，不是来自正文、昵称、自称或 truthy 外部参数。

## Gate 合同

- `GateObservation` 是 frozen/slots 且只能由 `issue_gate_observation()` 签发。
- `VERIFIED` 必须同时带明确布尔 ban verdict 与 source-event SHA-256。
- 非 verified 状态禁止携带 ban verdict，避免超时/缺失被误解释为“未 ban”。
- observation 被转换成 P1 `ExternalPluginEvidence(GATE_DECISION)`；只有 verified evidence 可标记 trusted 并供 `INGRESS` 消费。
- 所有非 verified 状态失败关闭，但用独立 reason code 保留可观测降级原因。

## PluginSource 边界

- `PLUGIN_ECHO` 必须已经由 P2-01 sealed `PluginSourceEvidence` 证明来源。
- 正文中的 `[parser]` 或“我是插件”等自称不能创建 plugin source。
- 原始 `PluginSource` 枚举、字符串或缺失 evidence 不能旁路 admission。

## Revision 与不可变结果

- `AdmissionResult`、`GateObservation`、P1/P2 decision/event 均为 frozen/slots。
- accept 结果必须同时携带与 decision 完全相同 binding 的 `ConversationEvent`。
- drop/degraded 结果禁止携带 `ConversationEvent`，且 revision 保持不变。
- 两个相同 base candidate 中，第一个 accept 后第二个按 stale 失败关闭。

## Trace 隐私

Admission trace 只输出随机 trace ID、scope/message/sender 摘要、content SHA-256、闭集状态、revision candidate/commit 数字。测试确认不包含正文、原始 message/sender/session/scope ID、插件参数或 gate 原始内容。

## 验证

- P2-02B 定向测试：`10/10` 通过。
- 覆盖 human accept、banned、self、known bot、sealed plugin echo、unknown automation/unknown、六种 gate 降级、gate object 缺失、stale 和 trace 隐私。
- 复用 P2-01 与 P1 合同的相关定向回归通过。
- 新模块与测试通过 `compileall`，相关文件通过 `git diff --check`。
- 未修改 `main.py`、`name_wake_filter.py`、`generation_epoch.py`、计划、配置、FNOS 或其他测试；未执行 Git/GitHub 写操作。

## 边界与下一层依赖

- 本控制器尚未从 AstrBot/ReNeBan 真实 hook 签发 `GateObservation`；生产 adapter 和 hook ownership 属于 P2-02 的其他集成子任务。
- `X-01` 仍成立：星汐控制器的零 revision/零消费不能被写成 LivingMemory 捕获前零存储。
- 本任务没有实现昵称机器人识别、TrustedBotRegistry、Scene、Memory、Media、Address 或发送逻辑。
