# P9-02 全局推理预算与优先级

## 结论

P9-02 已完成 Windows 与隔离 AstrBot Linux container 验证，候选未部署生产。

星汐的首次 Renderer、一次 repair 和主动开题此前分别由 AstrBot 或星汐自己的异步路径发起，只有单轮调用数限制，没有一个共同的全局并发、排队、超时和优先级所有者。现在新增长生命周期 `InferenceBudgetAuthority`：结构化直接/@与主人动作优先于普通参与，普通参与优先于主动开题；默认同时最多 4 个模型调用、128 个等待者、排队 30 秒。拒绝、超时、取消和 stale 路径均在调用 work factory 前终止，不创建 Provider coroutine。

## 正式红灯

首轮正式测试因 `core.inference_budget` 不存在而以 `ModuleNotFoundError` 失败。实现过程中又复现并关闭两项真实边界：

- 普通 canonical `PlannedAction` 原先没有保留结构阶段的 direct/participation 来源，预算层无法诚实派生优先级；Planner 现在把 structural reason 与 reconcile reason 一并封入 canonical plan digest。
- exact permit 持有期间，若 bounded `PlannedActionAuthority` 淘汰旧计划，清理若再次要求计划仍 canonical 会粘住全局槽位；release 现在只重验 exact permit 自身，授权/排队仍在调用前重验 exact current plan。

正式矩阵固定以下行为：direct 请求可越过更早到达的 ordinary waiter；每个 exact event 只允许一次 primary 和一次 repair；队列满/超时/stale 不创建 work；`CancelledError`、`KeyboardInterrupt`、`SystemExit` 释放容量；permit copy、mutation、cross-authority 失败关闭且 trace 不含 scope/message；close 终结 waiter 和 active slot；计划 authority 淘汰不阻塞 exact permit 清理；main 必须接入 primary、repair、proactive 三条真实路径。

## 实现

### 1. Code-owned permit 与固定优先级

- 新增 `core/inference_budget.py`，公开 `InferencePriority.DIRECT/PARTICIPATION/PROACTIVE`、`InferencePurpose.PRIMARY/REPAIR/PROACTIVE` 和 issuer-owned `InferencePermit`。
- inbound priority 只从同一 `PlannedActionAuthority` 的 exact canonical plan派生，caller API没有 priority 参数；`EXECUTE_ACTION` 或结构原因 `direct_force_must_reply` 为 DIRECT，其余参与回复为 PARTICIPATION。
- proactive 只接受 exact `ProactiveComposerRequest` 与其 exact `ProactiveExecutionAuthority`，固定最低优先级。
- 排队按 priority rank 后 sequence 排序；同优先级 FIFO。work factory 只在 exact permit 已签发后调用。

### 2. 全局调用与失败预算

- 默认 `max_active=4`、`max_waiters=128`、`queue_timeout_seconds=30`；配置 schema 提供范围受限的三个键，插件初始化时冻结，运行中不逐阶段读取可变配置。
- 同一 `(scope, generation epoch)` 只有 PRIMARY 与 REPAIR 两个闭集调用位；REPAIR 必须在 PRIMARY 已经登记之后，重复调用失败关闭。
- stale waiter 在取得 slot 前重验 generation epoch 和 canonical plan；超时、取消、队列满会撤销未使用的调用位。
- provider work 内的普通异常与任意 `BaseException` 都通过 `finally` 释放 permit。terminate 关闭 authority、唤醒所有 waiter并清空 active 计数。

### 3. 真实主流程

- `on_llm_request` 在完整 typed Composer request 激活后取得 PRIMARY permit；该 permit 覆盖 AstrBot 管理的首次 provider 调用，并在 `on_llm_response` 的第一步释放。
- repair 在 P9-01 same-scope lane 内再取得一次 REPAIR permit，Provider coroutine 由 permit 后的工厂创建。
- proactive 整轮仍由 P9-01 scope lane串行，实际 `provider.text_chat` 另由最低优先级 PROACTIVE permit包裹。
- provider error/stale 也会先释放 primary permit；active typed event 缺少 exact permit 时 guard 失败关闭，不接受未预算模型输出。

## 测试证据

- P9-02 focused：`9/9`。
- Planner/P9-01/Pipeline/Proactive related：`121/121`。
- Windows 完整发现：`1207/1207`，skipped 7。
- 隔离 AstrBot Linux container 完整发现：`1207/1207`。
- JSON schema parse、`compileall`、merge-marker 与 trailing-whitespace 检查：通过。
- 生产只读复核：container `running=true/restart=0`、WebUI 200；线上 `main.py` 哈希仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`。

## 冻结哈希

- `core/inference_budget.py`: `02BA9815B37C1CC8DF0497A2A5337F8DE9F4F1517A1411A2BFDB94BE6795D56F`
- `core/action_planner.py`: `B0A9D2167B1EC6E5C97D6E38C563BCCA77563B6A24D577FE333CCE98B98227C2`
- `tests/test_p9_inference_budget.py`: `64AA4B0A7FEAD06E2182C252FA1D32ADBFD86F4E5CD9231167D838FE7A80AF1D`
- `main.py`: `5978CBFD7BAAE77EED1F0D731E2E518A7786CEB0DA48C70748FC7CA56A57ED7F`
- `_conf_schema.json`: `FD09E7A3EC58307E7FF9D900A327683A9AE492185327A80141C9168B03A4A28D`
- 本地候选包：`C:\Users\45928\AppData\Local\Temp\shio-p902-20260819.tar`，SHA256 `FFEACDB6AB90D615EE696EBAAC47E513EDD731D9EF8A97FF12BC2277A1636AEF`。

## 生产与边界

- 候选仅进入 container `/tmp` 隔离目录，验证后已删除；未覆盖插件目录、未 reload、未 restart。
- P9-02 记录的是 admission wait 和调用计数，不是端到端 P50/P95 或首气泡延迟；这些属于 P9-03。
- 长时间没有 `on_llm_response` 的首次 provider permit、历史 tombstone 与冷启动恢复属于 P9-04；terminate 已能安全关闭，但本阶段不冒充完整生命周期已完成。
- owner adapter 继续全关；未修改 AstrBot core、LivingMemory、Meme Manager 或其他插件；未执行 Git 写操作。

## 下一入口

唯一下一入口是 **P9-03 模型调用与延迟预算**：建立不含内容的本地编排、排队、首次 provider、repair、首气泡与完整发送计时；以有界 histogram/quantile 快照输出 P50/P95，并固定无工具普通聊天一次主生成、repair最多一次、主动轮一次模型调用。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复；P10 前不请用户测试效果。
