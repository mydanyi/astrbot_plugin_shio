# P6-04 发送顺序、去重与 presentation receipt 报告

## 1. 结论

P6-04 已完成，P6 阶段闭合；候选未部署。

所有 typed 文本现在从第一段起就登记完整 canonical `PresentationHandoff.final_segments`。manual 多泡和 automatic 最后一泡消费同一 send record；任一 exact segment 最多尝试一次。手动发送失败后会清空剩余 AstrBot result，不再把相同字节交给自动发送重试。

`TEXT_AND_MEME` 的互补 Meme 只会在 `after_message_sent` 证明最后一段文本真实成功、全部 presentation segments 都是 `SUCCEEDED`、同一 generation 仍 current 后执行。新消息抢先时只签 `STALE_BEFORE_SEND`；没有 terminal text evidence、发送失败或 hook 未到达时，Meme Manager 零调用。

## 2. 正式红灯

新增 `tests/test_p6_presentation_transaction.py` 后，旧实现稳定 `3/3 failures`：

1. 文本发送终态后没有执行互补 Meme，也没有 canonical receipt；
2. manual 第一泡失败后，完整文本仍留在 result chain，可能由 AstrBot 自动路径再次发送；
3. automatic 文本发送后 generation 已 stale 时，没有 closed Meme receipt。

这些不是 UI 时序问题，而是 presentation ownership 被分散在 splitter、manual send、automatic hook 与 Meme executor 四个位置造成的结构缺口。

## 3. 根修

### 3.1 完整 presentation 预登记

`_plan_send_segment()` 不再为普通回复调用 `begin_reply(first_segment)` 再逐条 `append_segment()`。所有 typed reply 都必须携带 canonical `PresentationHandoff`，并通过 `begin_presentation_reply()` 一次登记 exact final tuple、目标、request、semantic contract 与 digest。

后续每一泡只能从该预登记 tuple 中取得唯一 `PLANNED` segment。找不到、重复、顺序错或 presentation 缺失时清空 result，不能无 receipt 发送。

### 3.2 单次发送与失败终止

- manual segment：`PLANNED → ATTEMPTED → SUCCEEDED|FAILED`；
- automatic 最后一段：handoff 时进入 `ATTEMPTED`，只有 `after_message_sent` 才进入 `SUCCEEDED`；
- manual 任一段失败后，剩余 `PLANNED` 保持未尝试并清空 AstrBot result；
- 不再把 failed segment 或 aggregate text 交给 automatic fallback；
- 重复 automatic hook 因 pending ID 已清空而幂等返回，Meme 不会二次执行。

### 3.3 Terminal evidence → Meme complement

`InternalSendReceiptLedger.issue_presentation_send_terminal_evidence()` 是既有 E4 sealed send evidence 的通用入口；底层 exact capability 保留历史类名 `OwnerActionSendTerminalEvidence`，但 ordinary 与 action presentation 使用相同 canonical vault 和全成功检查。

`MemeExecutionAuthority.prepare()` 对 `MEME_COMPLEMENT` 新增强制三元绑定：

- exact canonical `PresentationHandoff`；
- exact `InternalSendReceiptLedger`；
- exact all-success terminal evidence。

三者必须反向指向同一 composer request、planned action 与 expression。`REACTION` 不允许偷带 text evidence；互补 Meme 没有 evidence 就不能 prepare。

### 3.4 Stale 与隐私

文本已经真实发出但新消息先到时，旧 generation 不能执行 Meme；authority 只签 `STALE_BEFORE_SEND / attempt=0 / success=0`。如果 Meme send 后才变 stale，则沿用 P6-02 的 `STALE_AFTER_SEND`，诚实记录已经发送。

conversation ledger、runtime feedback、group scene、affect 与 learning 只观察成功文本 segments。闭集 marker、Meme query、candidate、caption、tag、raw result、临时文件和工具参数都不进入历史、trace 或学习。

## 4. 验证证据

- formal red：`3/3 failures`；
- P6-04 targeted：`3/3`；
- P6/send receipt/pipeline related：`110/110`；
- 加 semantic/action outcome/ledger 的扩展 related：`199/199`；
- Windows full：`1124/1124`，skipped 7；
- 隔离 AstrBot Python 3.12 container targeted：`3/3`；
- 隔离 AstrBot Python 3.12 container full：`1124/1124`；
- `py_compile`、`compileall`、限定 `git diff --check` 通过；
- fnOS/container `/tmp` candidate 已清理；
- 生产 `main.py` SHA-256 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`；
- 生产容器 `running=true`、`restart=0`、WebUI 200。

一次扩展 related 命令在插件目录执行 package-qualified unittest，得到 9 个 `ModuleNotFoundError`；同组在仓库父目录重跑为 `199/199`。这是测试工作目录错误，不是实现失败。

本地 staging archive `C:\Users\45928\AppData\Local\Temp\shio-p604-candidate-20260819a.tar` 未删除；精确删除受执行策略限制。它不在仓库、FNOS 或容器内，也未部署。

候选冻结哈希：

- `core/meme_presentation.py`: `e26b609ddee39c7d3085d487aa620a56c3e647e56c9a8146302341b4ee07e08d`；
- `core/send_receipt.py`: `dab6a6b341091fbd285c0fc2e46819d838975f17daa7ffadc5449c922a1ad994`；
- `main.py`: `9d763dcafa79c08bd373547272418982fdb671ca77698785dab23676b51b2ccf`；
- `tests/test_p6_presentation_transaction.py`: `9076084bbe2b2e7de7fc3e7700fb5af54b1ee0ac01ca407cd890025a474ddc9b`；
- `tests/test_pipeline.py`: `9f500af3aeecb7f925cece8fd983d787f8ebad82b16e03c88bdcccb0ab97ceb7`。

## 5. 改动范围

- 扩展 `core/send_receipt.py`；
- 扩展 `core/meme_presentation.py`；
- 修改 `main.py` 的 exact presentation 登记、manual/automatic failure、terminal hook 与 complement 调度；
- 新增 `tests/test_p6_presentation_transaction.py`；
- 更新 `tests/test_pipeline.py` 的 no-retry 预期；
- 新增本报告并更新 `SHIO_MASTER_PLAN.md`。

未修改第三方插件、AstrBot 核心、配置 schema、FNOS 生产插件或 Git 状态。

## 6. 下一唯一入口

P6 已完成。下一唯一入口是 **P7-01 typed proactive trigger**：建立不伪造用户消息、不绑定“最后一位用户”为主人、不可走 inbound admission shortcut 的 code-owned proactive turn；触发只形成 typed candidate，默认不发送、不调用模型，必须携带独立 proactive source/target/generation authority。中断后从本报告与 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P6。只有 P10 才请用户统一测试效果。
