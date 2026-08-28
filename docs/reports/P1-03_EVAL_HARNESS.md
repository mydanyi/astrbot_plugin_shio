# P1-03 三层行为评测 Harness 报告

## 结论

P1-03 的评测基础设施已完成。P1-02 的同一套 `24` 个合成 fixture 现在可以由确定性合同 runner、严格 Stub 集成 runner 和显式启用的可选外部模型 runner 消费；默认入口不创建网络连接，也不调用真实 Provider、插件或工具。

本任务只建立测试基础设施，不接入 `main.py`，不改变生产回答、人格、准入、工具授权或发送行为。

## 先红后绿证据

- 先新增 `tests/test_p1_eval_harness.py`；第一次定向运行因 `tests.harness.assertions` 尚不存在而稳定失败。
- 实现 harness 后，同一定向命令 `8/8` 通过。
- 新测试覆盖默认零网络、语义断言、固定时钟、SideEffectLedger、ScriptedProvider 顺序与预算、Stub runner、外部模型双重门和报告隐私。

## 三层职责

### 1. 确定性合同层

- 逐个消费 P1 fixture，比较枚举、布尔、计数和结构化语义字段。
- 只要求 expected 字段是 observed 的语义子集；额外可见措辞不会导致逐字台词比较。
- mismatch 只输出字段路径 reason code，不回显 expected/actual 值。
- 使用固定 `0 ms` 结果，不读取系统时钟，不调用网络、Provider、插件或工具。

### 2. 严格 Stub 集成层

- `FixedClock` 只允许显式前进，禁止负向或隐式墙钟变化。
- `SideEffectLedger` 只有 effect kind、reason code 和固定时间；没有正文、sender/message ID、URL、路径或工具参数字段。
- `ScriptedProvider` 的脚本长度必须等于调用预算；调用顺序、case 绑定、预算和脚本耗尽均失败关闭。
- 非模型插件替身同样按 fixture 声明顺序和精确次数消费；整个层不加载真实插件或工具。

### 3. 可选外部模型层

- 默认未启用；必须同时提供 `--allow-external-model` 和本地配置文件。
- 缺 opt-in、缺配置、缺 transport、缺凭据环境变量或预算不足都会在网络前安全拒绝，并只返回 reason code。
- 配置只保存 HTTPS endpoint、模型别名、凭据环境变量名、超时和总调用预算；密钥只从该环境变量读取。
- 发给模型的是合成 case code、维度、Stub 状态及待返回字段名，不发送 fixture 的 expected 值。
- P1 当前仅验证外部适配合同；后续阶段必须接入真实生产-bound observation 后，才能把该层结果计作行为质量证据。

## 脱敏报告合同

`render_report()` 使用闭集 schema，仅包含：

- 合成 `case_id` 与 `required_phase`；
- pass/fail、reason code、断言数；
- 模型/Stub/模拟 side-effect 调用计数和确定性耗时；
- 聚合 case/pass/fail/call 计数。

报告不包含对话正文、真实 sender/message/UMO ID、scope/turn/media alias、模型名称、endpoint、URL、路径、凭据、Prompt、工具名、工具参数、请求体或响应体。

## 运行入口

```powershell
python -X utf8 scripts/run_shio_behavior_eval.py
python -X utf8 scripts/run_shio_behavior_eval.py --tier stub
python -X utf8 scripts/run_shio_behavior_eval.py --tier external --allow-external-model --external-model-config <local-config>
```

第三条只有在操作者显式选择外部评测并准备本地配置后才可能创建网络 transport；前两条始终离线。

## 验证

- 定向单元：`8/8` 通过。
- CLI 确定性单 case：通过，模型 `0`、Stub `0`、side effect `0`。
- CLI Stub 单 case：通过，严格记录模型 `1`、Stub `1`、模拟 effect `2`。
- CLI 外部层无 opt-in：在网络前以 `external_model_opt_in_required` 拒绝，退出码 `2`。
- 新增 Python 文件全部通过 `py_compile`。
- 相关文件通过 `git diff --check`；未执行任何 Git/GitHub 写操作。
- P1 汇合后的仓库完整回归：`412/412` 通过。

## 设计边界

- 确定性与 Stub 层当前是 P1 harness/fixture 合同自检，不冒充 P2～P10 的生产行为已经实现。
- runner 允许后续阶段注入生产-bound observation，再复用相同 semantic assertion 和 report schema。
- 外部模型层是显式可选项，不能成为默认单测或发布门的隐式网络依赖。
- 未修改 `main.py`、计划文件、生产配置、FNOS、第三方插件、产品 trace 或插件 conformance 实现。
