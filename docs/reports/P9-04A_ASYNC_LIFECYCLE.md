# P9-04A 异步任务与活动推理生命周期

## 结论

P9-04A 已完成 Windows 与隔离 AstrBot Linux container 验证，候选未部署生产。

本子阶段只封闭进程内异步生命周期：星汐拥有的 generation task、scope lane、主动调度 task、推理 waiter 与活动 permit 现在都有明确的关闭边界；插件重复 `terminate()` 幂等。模型调用超过活动时限后不会释放预算给更多请求，也不会发送迟到结果；只有原调用真正返回并释放 exact permit 后，预算才恢复。这使 Provider 卡住时失败关闭，而不是形成后台请求风暴。

P9-04 的持久化部分尚未完成。conversation revision、参与节奏与冷启动恢复属于唯一下一入口 P9-04B。

## 正式红灯与实现期回归

- 正式红灯共 4 项，首轮均因缺少生命周期 API 报错：generation registry 没有 `close/closed`，scope coordinator 没有 `close/closed`，inference budget 没有活动超时/sweep，plugin terminate 没有完整关闭全部星汐自有后台任务。
- 根修后四项红灯转绿，并扩到 generation/scope/inference/proactive/pipeline 相关 `121/121`。
- 首次全量发现 P6 三项回归：P6 fixture 对 `main.time.monotonic` 的临时替换间接污染了活动推理超时时钟，合法结果被误判为迟到。推理预算改为模块加载时固定的内部单调时钟后，P6 `3/3` 与全量恢复全绿。该修复同时隔离了业务层时间替换对全局预算的影响。
- 隔离 container 首次发现命令的 PowerShell/SSH 引号使 `-p` pattern 被错误解释，runner 报 `Ran 0 tests`；修正为默认 discovery 后真实执行 `1217` 项并全部通过。该 runner 失误未计作代码通过。

## 实现

### 1. Generation task 所有权

- `GenerationTaskRegistry` 现在把 cancel-safe 与不可安全取消的 Provider awaitable 都转成星汐拥有的 Task 并登记。
- 新 generation 仍只取消明确 `cancel_safe=True` 的旧任务；插件关闭则取消并 `gather` 全部星汐拥有的任务，包括平时不能因 supersede 取消的外部 Provider 调用。
- 关闭后拒绝新任务；构造 Task 与登记之间发生关闭也会先取消并回收 Task，不留下 orphan coroutine。

### 2. Scope coordinator 关闭

- `TurnScopeCoordinator` 登记所有 active/waiting 调用，`close()` 原子关闭 admission、取消并等待现有任务。
- lane/global semaphore、waiting/active 计数在取消和任意异常路径仍由 `finally` 释放；关闭后不能重新启动。
- trace 只增加 closed/bounded/count，不显示 scope 或 Principal 内容。

### 3. 活动推理超时

- 新配置 `inference_active_timeout_seconds`，默认 300 秒，schema/运行时范围 5～1800 秒；authority 内部合同支持更短值用于确定性测试。
- 活动 permit 超时后变为 `expired`，但仍占用真实 active slot，并阻断新 admission；因此不能用“逻辑超时”伪装外部调用已经结束，也不会超额并发。
- 原 Provider 晚到后释放 exact permit，容量才恢复；该结果返回 `False`/`inference_active_timeout`，主生成、repair 与 proactive 都丢弃迟到结果，绝不发送。
- `close()` 会关闭 waiter 与 active/expired permit，重复关闭安全。

### 4. 主动任务与插件终止

- `ProactiveSchedulerRuntime.shutdown()` 在 stop admission 后取消并等待全部 runtime-owned task，不只取消当前标记为 cancel-safe 的组任务。
- `ShioPlugin.terminate()` 固定依次停止 scheduler loop、shutdown 主动 runtime、关闭 generation registry、scope coordinator、inference budget，最后关闭 owner lifecycle store 与 flush runtime。
- 重复 `terminate()` 不重启任何已关闭 authority，也不残留 active/waiting task。

## 测试证据

- 正式生命周期红灯转绿：`4/4`。
- generation/scope/inference/proactive focused：`36/36`。
- P6 时钟隔离 + 生命周期 + 主链迟到结果：`40/40`。
- generation/scope/inference/proactive/pipeline related：`121/121`。
- Windows 完整发现：`1217/1217`，skipped 7。
- 隔离 AstrBot Linux container 完整发现：`1217/1217`。
- `compileall`、JSON schema parse、merge-marker、trailing-whitespace 与 `git diff --check`：通过。
- 生产只读复核：container `running=true/restart=0`、WebUI HTTP 200；线上 `main.py` 哈希仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`。

## 冻结哈希

- `core/generation_cancellation.py`: `1F8728BF0F94499944AAAC3ED3B23CF08D5702717982A3823D98850BDA4D3F49`
- `core/scope_concurrency.py`: `B4A5632BAA0DB68A9C985B2DB1BFF1639BAD53860C6B24262DE8ECE1F785E665`
- `core/inference_budget.py`: `5406E4F2361DA5E2E95C79A77AF65C5ED3F36FB40D2C22C9B54AE0A13DC63EEF`
- `core/proactive_runtime.py`: `A36AA1D62D2738EB869B6D593FE8D416868AB4448D006D1AA7C5C448DC7B49D0`
- `main.py`: `F1CB24E4C59E09A75313D924D9178F05519F6BDD88B00F167E996374BB337C1E`
- `tests/test_generation_cancellation.py`: `385D68BF04A082967E2B55A648B93D95030103A592EB823855527730366541DF`
- `tests/test_p9_scope_concurrency.py`: `86538286DD298ADF27F169105DA122CE59277F60804E63A1B16830B1A6133CD2`
- `tests/test_p9_inference_budget.py`: `CC3C2D00F3B837AC9FBCE4CC47586BE4FC4BE92D530891E6AC38DE7D3DF517CB`
- `tests/test_proactive_runtime.py`: `CCBC92648FEF9614C999F82AE79CEEB570014A0FB17F5E649874CB11227B4349`
- `tests/test_pipeline.py`: `1B517EEC1A780BC228F21722D74F1D4B5C38D3D6038609EFD91A3DF82AB6E551`
- `_conf_schema.json`: `BCD15A5641AFA4FE51800F5CA38EC23D8C4DDF53F502770230FD3AF47C5FBF1B`
- 本地候选包：`C:\Users\45928\AppData\Local\Temp\shio-p904a-20260819-v1.tar`，SHA256 `33A229C2C8BA40E2CBD941BB6DD3908785DBD7E6084E3683E1A748D03F781AE5`。

## 生产与边界

- 候选只进入 container `/tmp/shio-p904a-20260819-v1`；验证后 host/container staging 均已删除。未覆盖插件目录、未 reload、未 restart。
- 本阶段不声称冷启动状态已经恢复；conversation revision 与 participation cadence 仍是进程内状态，必须由 P9-04B 解决。
- 主动 cooldown/day/replay 已由 P7 的持久策略承担；P9-04B 只做 code-owned 核对与重启矩阵，不新增第二套 cooldown 真相源。
- owner adapter 继续全关；未修改 AstrBot core、LivingMemory、Meme Manager 或其他插件；未执行 Git 写操作。

## 下一入口

唯一下一入口是 **P9-04B revision 与参与节奏冷启动恢复**：建立 privacy-minimal、bounded、atomic 的 code-owned 持久状态，只保存不可逆 scope/subject 指纹、revision 与 cadence 所需数值；接入 `ConversationRevisionBook` 和 `ParticipationCadenceAuthority`，并为 Affect/GroupScene 的首轮重启 bootstrap 建立 exact continuity。随后跑重复 restart、损坏/版本漂移 fail-closed、512/1024 churn 与 Windows/container 全量。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复；P10 前不请用户测试。
