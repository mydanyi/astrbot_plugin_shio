# P7-01 typed proactive trigger 报告

## 1. 结论

P7-01 已完成；候选未部署，也没有启用主动发言。

新增的 `ProactiveTriggerAuthority` 只签发四层 exact object：明确群目标、scheduler observation、proactive source、typed-only candidate。它与 inbound `ConversationEvent`、`AcceptedTurnAuthority`、Principal、sender、user message、reply reference 和 admission ticket 完全分离；不能把最后一位发言者解释成触发者、主人或回复目标。

P7-01 没有 claim、Planner、模型或发送入口。每个 candidate 的 closed disposition 固定为 `TYPED_ONLY`，`model_authorized=False`、`send_authorized=False`。P7-02 必须另行增加 policy authority，不能把本层 canonical candidate 直接当成发言许可。

## 2. 正式红灯

先新增 `tests/test_proactive_trigger.py`，旧代码稳定得到：

- `ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.proactive_trigger'`；
- `Ran 1 test / FAILED (errors=1)`。

这证明原代码没有独立 proactive source，只能依赖 inbound event 路径或另造非 typed 状态。

## 3. 根修

### 3.1 明确群目标，不含人类身份

`ProactiveGroupTarget` 只保存平台、bot、group、UMO 与 group scope。四层公开字段集合明确排除 sender、principal、owner、message、message ID、reply reference、admission 和 ticket。

目标、observation、source、candidate 都关闭 public constructor，并由同一 authority 的 exact-object registry、issuer weak reference、seal 和全字段快照共同验证。copy/deepcopy/pickle/object shell、跨 authority、字段篡改和嵌套替换全部 fail closed。

### 3.2 独立 scheduler observation 与 generation

每次 `observe_group()` 产生 code-owned scheduler tick；`issue_source()` 和 `issue_candidate()` 各自 one-shot。proactive generation 按 group scope 独立递增：同群新 candidate 使旧 candidate stale，其他群不受影响；group LRU 和 weak object records 均有明确上限。

这一 generation 不读取也不修改 inbound `GenerationEpochRegistry`，因此不会伪造用户 turn，也不会借用当前/最后一位用户的 revision。

### 3.3 默认零副作用

本模块没有模型、Planner、工具、send、main、FNOS 或 AstrBot hook 依赖。canonical candidate 只证明“某个明确群目标有一条 typed scheduler observation”，不证明白名单、活跃时段、空闲、冷却、日限额、话题选择或发送授权。

同一 source 的 8 线程并发签发只有一个 winner；weak records 在完整链释放后回收，不会为了维持 authority 强持群消息或用户对象。trace/repr 只输出 schema、闭集 kind/generation/count/canonical boolean 与脱敏 scope digest。

## 4. 验证证据

- formal red：模块缺失，`Ran 1 / errors=1`；
- P7-01 targeted：`8/8`；
- identity/event/accepted-turn/generation/planner/runtime related：`98/98`；
- Windows full：`1132/1132`，skipped 7；
- 隔离 AstrBot Python 3.12 container targeted：`8/8`；
- 隔离 container full：`1132/1132`；
- `py_compile`、`compileall`、静态禁用入口扫描和 whitespace/diff check 通过；
- FNOS/container `/tmp/shio-p701-*` staging 已清理；
- 生产插件没有覆盖、重启或配置变更；`main.py` 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`，container `running=true / restart=0`，WebUI 200。

候选冻结哈希：

- `core/proactive_trigger.py`: `0639e9064e2925d91a7c73fe639b698541f6972aeaad99ef18e3f1c68b3f258c`；
- `tests/test_proactive_trigger.py`: `ced2cb76394881793f64f9aea689a83e6bf16b003cff45eece07401f1bdac5ac`。

本地 staging archive `C:\Users\45928\AppData\Local\Temp\shio-p701-candidate-20260819a.tar` 不在仓库、FNOS 或容器内，也未部署。

## 5. 改动范围

- 新增 `core/proactive_trigger.py`；
- 新增 `tests/test_proactive_trigger.py`；
- 新增本报告；
- 更新 `SHIO_MASTER_PLAN.md` 的阶段状态和唯一下一入口。

未修改 `main.py`、配置 schema、第三方插件、AstrBot 核心、FNOS 生产插件或 Git 状态。

## 6. 下一唯一入口

下一唯一入口是 **P7-02 群白名单、活跃时段、观察期、空闲、冷却和日限额**：新增 code-owned、持久化且可审计的 proactive policy/scheduler state；空白名单必须默认零 candidate admission、零模型、零发送，重启不能重复触发。P7-01 的 typed-only candidate 不得被直接升级成发送许可。

中断后从本报告与 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P7-01。只有 P10 综合生产验收才请用户统一测试效果。
