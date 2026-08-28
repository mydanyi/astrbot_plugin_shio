# P9-01 会话内串行、会话间受控并行

## 结论

P9-01 已完成 Windows 与隔离 AstrBot Linux container 候选验证，未部署生产。

星汐此前只有 generation epoch：新消息能让旧结果在提交/发送前失效，但星汐自己创建的检索、repair、Meme、主动轮和发送 awaitable 仍可同时占用运行资源；epoch 也不能证明同一 scope 的异步副作用不会重叠。现在新增长生命周期 `TurnScopeCoordinator`：同 scope 只有一条工作通道，跨 scope 默认最多 4 条并行通道，所有排队和活动数量都有硬上限，取消及任意 `BaseException` 都在 `finally` 中释放。

## 正式红灯

新增 `tests/test_p9_scope_concurrency.py` 后，首轮因 `core.scope_concurrency` 不存在而以 `ModuleNotFoundError` 失败。红测固定以下边界：

- 同 scope 两项工作按到达顺序执行，第二项不得与第一项重叠；
- 不同 scope 可并行，但不得超过全局上限；
- 协调器不得保存或向 work 传递可变 `PrincipalContext` 对象；
- 等待中取消、work 内 `KeyboardInterrupt`/`SystemExit`、旧 epoch 完成、队列满和 lane 满均失败关闭且释放槽位；
- work 只能在取得 scope lane 与全局 slot 后由工厂创建，拒绝路径不得留下未 await coroutine；
- 64 个不同 scope churn 后仅保留有界 idle lane；
- direct/react/proactive/action 的真实 main 热路径必须全部出现 coordinator 接线。

## 实现

### 1. 单 scope FIFO 与跨 scope 上限

- 新增 `core/scope_concurrency.py`，公开闭集 `ScopeWorkKind.DIRECT/REACT/PROACTIVE/ACTION`。
- 每个 scope 使用独立 `asyncio.Lock`；等待者遵循 `asyncio.Lock` 的 FIFO 获取语义，同一 scope 不会同时执行两项星汐自有异步工作。
- 全局 `asyncio.Semaphore` 默认上限为 4。每个 scope 已先取得自己的 lane，再等待全局 slot，因此任一 scope 最多占用一个活动 slot。
- 默认单 scope 等待上限 16、全局等待上限 128、lane 上限 2048；满载时拒绝新 work，不驱逐活动或等待中的 scope。
- idle lane 使用有界 LRU 回收；64 scope 回归在 `max_scope_lanes=8` 时最终 lane 数不超过 8。

### 2. exact epoch 与 Principal 隔离

- inbound work 只接受同一 `GenerationEpochRegistry` 的 exact current `GenerationEpochSnapshot`。
- scope lane 获取前和 work 完成后都重验 current epoch；旧轮可以完成不可取消的外部动作，但其结果会以 `SupersededGeneration` 丢弃，不能进入后续呈现或发送。
- `PrincipalContext` 在排队前做 exact primitive/type/scope 校验，然后仅保存内部 frozen primitive snapshot；work factory 不接收 Principal，trace 不显示 scope、sender 或 session 值。
- 主动轮只接受 exact `ProactiveComposerRequest` 与其 exact `ProactiveExecutionAuthority`，scope 从 canonical target 派生，不接 caller raw scope。

### 3. 中断与故障释放

- work factory 在 lane/global slot 都取得之后才调用。
- 等待 lane、等待全局 slot、work 执行和完成后 stale 校验均由同一 `finally` 释放 queue、active、semaphore 与 lane 状态。
- `CancelledError`、普通异常、`KeyboardInterrupt` 和 `SystemExit` 都不会粘住 active/waiting slot；原 exact current turn 随后可继续运行。
- 协调器绑定首次使用的 event loop；跨 loop 使用失败关闭，避免同一 `asyncio.Lock`/`Semaphore` 被错误复用。

### 4. main 热路径

`main.py` 初始化一个长生命周期协调器，并接入：

- `execute_sealed_acquisition`：`DIRECT`；
- 一次 repair provider call：普通轮为 `DIRECT`，owner action 为 `ACTION`，并继续复用 generation cancellation；
- 纯 React 与文本发送后的 Meme complement：`REACT`；
- 整个主动轮 provider→校验→单次 send transaction：`PROACTIVE`；
- 手动多气泡 `event.send`：普通轮为 `DIRECT`，owner action 为 `ACTION`。

AstrBot 自身拥有的首次主模型调用和自动发送回调不是星汐创建的 awaitable，P9-01 不伪装能够直接取消它们；它们仍由 exact epoch、FINAL_SEND seal 和 send receipt 阻止旧结果发送。P9-02 将建立跨首次主生成、repair 与 proactive 的全局推理预算/优先级 authority。

## 测试证据

- P9-01 focused：`9/9`。
- generation/participation/proactive/Meme/pipeline related：`129/129`。
- Windows 完整发现：`1198/1198`，skipped 7。
- 隔离 AstrBot Linux container 完整发现：`1198/1198`。
- `compileall`、merge-marker、trailing-whitespace、限定 `git diff --check`：通过。
- 生产只读复核：container `running=true/restart=0`、WebUI 200；线上 `main.py` 哈希仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`。

## 冻结哈希

- `core/scope_concurrency.py`: `F19C2555920EDA1581947B53D2D30A3487B6B04A64E0697E4A3A338B229222D6`
- `tests/test_p9_scope_concurrency.py`: `7390FAE033917E5957B63628E9430D2FEB48B617554DD3DD103A8B90EA546B49`
- `main.py`: `7D259357129C5C269B45D1EAD9B172C11DCAE413D912D90D86AA807D137AE2C4`

## 生产与边界

- P9-01 候选仅进入隔离 `/tmp` staging，验证后已删除；未覆盖插件目录、未重载、未重启生产。
- 本地候选包保留在 `C:\Users\45928\AppData\Local\Temp\shio-p901-20260819-v3.tar`，不在仓库或插件加载路径。
- 固定并行上限解决的是 P9-01 的异步所有权边界；不同类别请求的优先级、模型调用风暴、排队超时和预算指标属于 P9-02。
- 未修改 AstrBot core、LivingMemory、Meme Manager 或其他第三方插件；未执行 Git 写操作。

## 下一入口

唯一下一入口是 **P9-02 全局推理预算与优先级**：建立 code-owned inference permit，优先级固定为结构化直接/@请求高于普通参与、高于 proactive；首次生成、一次 repair 和主动轮共享总并发/排队/超时预算，拒绝或中断不得创建 provider coroutine。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P9-01；只有 P10 综合验收完成后才请用户统一测试效果。
