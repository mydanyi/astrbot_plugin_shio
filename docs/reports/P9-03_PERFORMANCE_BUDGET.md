# P9-03 模型调用与延迟预算

## 结论

P9-03 已完成 Windows 与隔离 AstrBot Linux container 验证，候选未部署生产。

星汐现在使用一个长生命周期、内容无关且有界的 `PerformanceWindow` 统计本地编排、推理排队、首次 Provider、repair Provider、主动 Provider、首气泡与完整回复的最近窗口 P50/P95/max。模型调用按 primary/repair/proactive 分开计数，失败另计；窗口不保存消息、Prompt、发送者、scope、trace ID、文件路径或模型输出。

P9-02 已经从 authority 层固定每个 exact turn 最多一次 primary 和一次 repair、每个 proactive request 一次调用；P9-03 将这三条真实调用路径与实际发送路径接入同一个性能窗口，并补了普通轮、repair 和 proactive 热路径断言。

## 正式红灯与实现期回归

- 首轮正式测试因 `core.performance_metrics` 不存在而以 `ModuleNotFoundError` 失败。
- 接 repair 性能观察时，`permit_observer` 一度被错误传给外层 scope queue，导致 5 条 repair 回归在 Provider 调用前失败、表现为零调用与空回复。该问题由真实 pipeline 测试捕获；参数已移入 `InferenceBudgetAuthority.run_event_call`，原 5 条回归和完整相关矩阵随后全绿。
- 最终测试固定：最近窗口淘汰、nearest-rank P50/P95、并发写入有界、exact Enum/数值类型、无内容 trace、普通主生成、单次 repair、单次主动调用、首个成功气泡与完整发送。

## 实现

### 1. 内容无关有界窗口

- 新增 `core/performance_metrics.py`。
- `LatencyKind` 闭集：`LOCAL_ORCHESTRATION`、`INFERENCE_QUEUE`、`PRIMARY_PROVIDER`、`REPAIR_PROVIDER`、`PROACTIVE_PROVIDER`、`FIRST_BUBBLE`、`FULL_REPLY`。
- `ModelCallKind` 闭集：`PRIMARY`、`REPAIR`、`PROACTIVE`。
- 每类延迟只保留最近 `performance_window_samples` 个 float 毫秒值；默认 512，schema 与运行时冻结范围 16～8192。
- 快照只公开 count、P50、P95、max、模型调用数、失败数和 bounded flag。

### 2. 真实热路径

- primary：typed Composer request 完成且 PRIMARY permit 已取得后记一次调用；从 trace start 到 permit 后记录本地编排，从 permit wait 记录推理排队，从 provider 开始到 `on_llm_response` 记录首次 Provider。
- repair：scope lane 与 REPAIR permit 均已取得后才创建 Provider coroutine；实际 Provider 调用由观察器计时，失败也闭合计数并释放 permit。
- proactive：scope lane 与最低优先级 permit 后才计时实际 `provider.text_chat`，失败不重试。
- send：只有真实成功的首段才记录 `FIRST_BUBBLE`；最后自动段收到 AstrBot `after_message_sent` 成功回调后记录 `FULL_REPLY`。多气泡前段仍由 exact send receipt 逐段确认。
- 每次 pipeline metrics 快照会附带当前全局性能窗口的脱敏 aggregate；debug 日志不含原文或身份材料。

## 测试证据

- P9-03 focused：`5/5`。
- P9 performance + inference + pipeline + proactive related：`109/109`。
- Windows 完整发现：`1213/1213`，skipped 7。
- 隔离 AstrBot Linux container 完整发现：`1213/1213`。
- `compileall`、JSON schema parse、merge-marker、trailing-whitespace 与 `git diff --check`：通过。
- 生产只读复核：container `running=true/restart=0`、WebUI 200；线上 `main.py` 哈希仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`。

## 冻结哈希

- `core/performance_metrics.py`: `F5C3AE799D68E2CA907667C84E9239932F80821AAB5CC6A36659760B81E43E7A`
- `tests/test_p9_performance_metrics.py`: `172427D4B71F368ADBC8C5ED574E4A2D0FFB2274484AA4861DA2487CE81FDDDC`
- `core/inference_budget.py`: `F5F86CC92495AFD7EC76854A9AD30F32557FD0F3B57EB6602D9F4A5285621769`
- `main.py`: `71C1FDC3EEA9F0D96E0ADDF8C39B376F9EB778DA49246DEEFD11C215F8DA5733`
- `tests/test_pipeline.py`: `24F4FE9E1E6C937339B3608A3AA05650E4BF7FB05A50C93DD3FE9463A452D910`
- `tests/test_proactive_runtime.py`: `2EC886E3F1314367E02B9B7141E6569D15AAEEBF8E16ECFCAA954949F284933E`
- `_conf_schema.json`: `A5513359302D3FA0B014D285FFBE7DAE7E9FC90E96A375EEF321256430EDDE40`
- 本地候选包：`C:\Users\45928\AppData\Local\Temp\shio-p903-20260819-v2.tar`，SHA256 `AE3E7747A7D45D3C5BB794EAC6393B15198636914FB71F95960587E63F9ABD00`。

## 生产与边界

- 候选只进入 container `/tmp` 隔离目录；验证后远端 host/container staging 已删除。未覆盖插件目录、未 reload、未 restart。
- 性能窗口是进程内 aggregate，不持久化，也不承担冷启动恢复；后台任务、cooldown、revision、未返回 primary permit 和重启后不重复属于 P9-04。
- 当前统计是工程测量能力，不把本地/fixture 数值冒充真实生产 SLO；真实 P50/P95 与自然流量预算在 P10 统一验收。
- owner adapter 继续全关；未修改 AstrBot core、LivingMemory、Meme Manager 或其他插件；未执行 Git 写操作。

## 下一入口

唯一下一入口是 **P9-04 状态生命周期与重启恢复**：盘点并封闭首次生成 permit、scope/inference waiter、主动 scheduler、cooldown、generation revision、send observation 和性能窗口的启动/终止/取消边界；建立重复 start/terminate、stale completion、重启恢复与长时间 churn 红绿矩阵。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复；P10 前不请用户测试效果。
