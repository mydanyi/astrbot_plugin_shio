# P1-05 生产插件合同 Conformance Harness 报告

## 结论

P1-05 已完成。新增一套完全离线、无需导入或修改第三方插件的生产插件合同 harness，固定 LivingMemory、ReNeBan、AnySearch、Meme Manager 和 Parser 的职责、hook、可见性、副作用预算及安全降级口径。

本阶段只增加测试基础设施，不接入 `main.py`，不改变线上回答行为。

## 失败测试先行

先新增 `tests/test_p1_plugin_conformance.py`，首次定向执行因 `tests.harness.hook_runner` 尚不存在而稳定失败：

```text
ModuleNotFoundError: No module named 'astrbot_plugin_shio.tests.harness.hook_runner'
```

随后才实现合同目录和确定性 hook runner。最终 11 项定向测试全部通过。

## 闭集合同

| 插件 | 合同角色 | Evidence | 历史可见性 | 允许消费者 | 每轮副作用上限 |
|---|---|---|---|---|---|
| LivingMemory | 近期/语义记忆参考；外部被动捕获只做时序观测 | `memory_reference` | `current_turn_reference` | `current_turn` | 记忆参考读取 1；外部被动捕获 1 |
| ReNeBan | ban 准入事实源 | `gate_decision` | `drop` | `ingress` | gate 读取 1 |
| AnySearch | 公共资料取证 | `grounding_fact` | `current_turn_reference` | `current_turn` | 公网只读 1 |
| Meme Manager | 唯一表情呈现执行器 | `presentation_effect` | `drop` | `presentation` | 表情发送 1 |
| Parser | 外部媒体/解析直发 | `external_output` | `drop` | 无星汐消费者 | 外部媒体发送 1 |

合同是闭集：未知插件、未知 effect、超预算 effect、未声明 hook、Parser/Meme 进入历史，以及任何插件证据进入 Persona/Learning 均判定为不合规。

## 状态与判定

`PluginState` 固定为：

- `present`
- `missing`
- `disabled`
- `timeout`
- `error`
- `interface_changed`

判定规则：

- `present` 且 interface token、hook phase/priority、evidence、历史、消费者和 effect 预算完全匹配：`conformant`。
- 其余五类状态不产生可见历史、消费者或 effect，且适配器显式安全降级：`degraded`。
- 失败状态仍产生副作用/历史、未安全降级，或 `present` 时接口、hook、消费者、可见性、effect 不符合合同：`nonconformant`。

`interface_changed` 必须由适配器诚实标记并停止使用旧接口；若仍声称 `present`，则接口变化不能静默通过。

## Hook 顺序和 X-01 边界

runner 按 phase 升序、同 phase priority 降序执行，使用合成回调验证当前生命周期：

```text
LivingMemory WakingCheck passive capture
→ ReNeBan StarRequest priority 114
→ Parser StarRequest priority 0
→ Shio/LLM/tool/presentation
```

ban stub 在 ReNeBan 阶段 stop 后，低优先级 Parser 与后续 LivingMemory recall/星汐阶段全部为 `skipped`。但 LivingMemory 的外部被动捕获已经在 WakingCheck 发生，因此 harness 明确保留 `X-01`，不把“星汐零消费”冒充“LivingMemory 零存储”。

## 确定性和隐私

- `VirtualClock` 只做数值推进；不读取墙钟。
- 超时 stub 等待未置位的 `asyncio.Event`，虚拟推进到 deadline 后立即产生 typed `timed_out`，不调用真实 `sleep`。
- 已置位 Event 立即完成，虚拟时间不变化。
- stub 异常只记录异常类型，不保存异常正文、原始群聊、请求体、URL、路径或凭据。
- runner 的 stop、timeout、error 和 skipped 都是显式 typed 结果，不通过真实插件或网络制造故障。

## 验证

- 红灯：新增测试首次因 harness 模块不存在而失败；
- P1-05 定向测试：`11/11` 通过；
- `compileall`：新增两个 harness 模块和测试文件通过；
- `git diff --check`：通过；
- 未修改第三方插件、生产 `main.py`、配置、FNOS 或 Git/GitHub 状态。

P1 并行任务汇合后的完整仓库回归：`412/412` 通过。

## 后续消费方式

P2、P3、P6 和 P10 可直接用同一合同目录构造插件存在、缺失、禁用、超时、错误与接口变化矩阵。生产适配器只有在合同观察为 `conformant` 或已定义的安全 `degraded` 时才可继续；任何 `nonconformant` 结果都应阻止相应 evidence/effect 进入星汐热路径。
