# P7-04 新消息让位、失败退避与主动发送事务报告

## 1. 结论

P7-04 已完成本地与隔离容器候选验证；候选未部署，线上主动发起仍关闭。

星汐现在具备一条不伪造入站事件、不借用最后发言人身份的 proactive 执行链：exact P7-03 topic plan → code-owned Composer request → 单次零工具 Provider → proactive output guard → 单 segment canonical presentation → InternalSendReceiptLedger → `Context.send_message()`。默认总开关 false、空白名单零任务；只有完整 P7-01～P7-03 authority 仍 current 时才可能进入模型和发送。

## 2. 正式红灯与实施中发现

先新增 `tests/test_proactive_runtime.py`，旧代码稳定得到：

- `ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.proactive_runtime'`；
- `Ran 1 / FAILED (errors=1)`。

首轮失败测试还暴露两处真实边界：

- Provider 失败后若 runtime 只对成功发送设置 `awaiting_human`，同群下一 scheduler tick 会再次调用模型；已改为任何已开始的 terminal 结果都必须等下一位真人消息解锁；
- 只扫描 `clean_response()` 后的文本会把 Markdown code fence 清掉后误放行；现同时扫描原始输出和最终可见文本，协议、推理、路径、凭据、权限/私聊声明和定向 `@` 命中即整项拒绝。

隔离容器首次 P7-03 恢复时出现的 package 层级错误不属于本阶段实现；P7-04 的最终候选直接按正确 package parent 验证。

## 3. 根修

### 3.1 独立 proactive execution authority

新增 issuer-owned `ProactiveExecutionAuthority`、`ProactiveComposerRequest`、`ProactivePresentation` 与闭集 `ProactiveExecutionStatus`。同一 P7-03 plan 只能准备一次；同一 topic runtime 只能签一个 execution authority、一个 scheduler。公开构造、copy、同形 raw object、跨 authority、字段 mutation、二次 output、二次 send claim 和二次 complete 全部失败关闭。

请求只携带 Persona 的公开身份/性格和 P7-03 public topic；不导入 `ConversationEvent`、Admission、Principal、AcceptedTurn、LivingMemory 或 personal facts。Provider 固定 `contexts=[]`、图片/音频空、`func_tool=None`、无 tool result、`request_max_retries=1`。

### 3.2 新消息让位与失败退避

`ProactiveSchedulerRuntime` 按 exact group scope 保存当前 scene/activity generation 和唯一任务：

- Provider 明确声明 cancellation-safe 时，新真人消息主动取消星汐创建的任务；
- Provider 不声明可取消时不强杀，完成结果仍因 scene/generation 过时而丢弃；
- 发送前在 scheduler lock 内执行 current-generation `claim_send`，真人消息先到则零发送，发送先开始则后续消息不能把已经发生的 attempt 伪装成未执行；
- 成功、Provider 失败、输出拒绝或发送失败后都进入 waiting-human，不能连续自说自话；下一位已准入真人消息才解锁；
- 插件 terminate 停止 scheduler，cancel-safe 任务取消；非 cancel-safe Provider 即使继续完成也因 runtime stopped 而零发送。

P7-02 在 admission 时已经持久化 cooldown/day/replay fence，因此重启不会因为 runtime 内存清空而重复同一 observation。

### 3.3 canonical presentation 与 send receipt

主动输出固定为一条、最多 240 字；原始和最终文本都经过既有 `response_guard` 的协议、内部推理、身份混淆、语义拆分与额外 proactive 禁止面。无 repair 第二次模型调用。

`InternalSendReceiptLedger.begin_proactive_presentation_reply()` 不伪造 `ReplyTarget` 或用户 message ID；记录 exact group session/scope、topic digest 与一个 presentation segment。`Context.send_message()` 只有 exact `True` 才记成功，`1` 等 truthy 值、false、异常均记 terminal failure且不重试。成功回执由 exact presentation 重新打开后写回群公共 scene，author 是真实 bot sender key，不增加任何群友 reply count。

### 3.4 scheduler 生命周期与配置

新增 `proactive_scheduler_interval_seconds`，范围在 main 固定为 15～3600 秒。`on_astrbot_loaded` 只在 P7-02 policy operational 时创建一个后台 task；默认全关或白名单空时不创建。重复 hook 不重复建任务，terminate 可幂等停止。

## 4. 验证证据

- formal red：模块缺失，`Ran 1 / errors=1`；
- P7-01～P7-04 targeted：`36/36`；
- proactive/scene/send/generation/persona/pipeline related：`168/168`；
- Windows full：`1160/1160`，skipped 7；
- 隔离 AstrBot Python 3.12 container proactive：`36/36`；
- 隔离 container full：`1160/1160`；
- `compileall`、schema JSON、merge marker、trailing whitespace 与 diff check 通过；
- FNOS/container `/tmp/shio-p704-*` staging 已清理；本地临时 tar 因宿主安全策略保留于 `C:\Users\45928\AppData\Local\Temp\shio-p704-final-20260819.tar`，不在仓库且未部署；
- 生产 `main.py` 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`，container `running=true / restart=0`，WebUI 200。

候选冻结哈希：

- `core/proactive_runtime.py`: `6ed1d6089e15c4dacf06e27dcc0ff29f1d3c78ca0862e38b26a5329218440bb6`；
- `tests/test_proactive_runtime.py`: `a5600aff2c3f7acef8335c08217cfcd4f65530cfdd7f3090c928e183b66d647d`；
- `core/proactive_policy.py`: `1c920ef569a74afc891be22781a8b5bb252b04352f069d38d9164cdad3a5a6a1`；
- `core/proactive_topic.py`: `08514534e467657036f0adab5aad37b50f03d38c7404d2d584436549c676423a`；
- `core/group_scene.py`: `72e65ce85592eb5773638b3e42b7c7f9c4a93f1b510491515e179b0404a8b642`；
- `core/send_receipt.py`: `b72df148a8f491e549d6ac10e774c2c21a868303ef191753ccf4b11ad9083b8e`；
- `main.py`: `11070a66c1e5bb40980ff3c637bc1b989e8ca083bf95db789904a20b43ddff2c`；
- `_conf_schema.json`: `c7bebd4a4e92eebe7b1a8e05b51d96d82b01bf400b9b991a9dc848c4a9d87c16`。

## 5. 改动范围

- 新增 `core/proactive_runtime.py`；
- 新增 `tests/test_proactive_runtime.py`；
- 扩展 `core/proactive_policy.py` 与 `core/proactive_topic.py` 的 post-claim integrity inspector；
- 扩展 `core/send_receipt.py` 与 `core/group_scene.py` 的 proactive exact send/public scene transaction；
- 扩展 `main.py` 的 scheduler lifecycle、Provider/guard/send 热路径和真人消息取消；
- 扩展 `_conf_schema.json`；
- 新增本报告并更新 `SHIO_MASTER_PLAN.md`。

未修改第三方插件、AstrBot 核心、FNOS 生产插件或 Git 状态；没有向任何真实群发送测试消息。

## 6. 下一唯一入口

P7 已完成。下一唯一入口是 **P8-01 群公共/个人/主人私有记忆策略**：在现有 typed MemoryPolicy/ContextAssembler 基础上，明确三类记忆的 canonical scope、来源、召回和输出边界；current message 永远优先，proactive 只能读取群公共记忆，任何跨用户私有泄漏必须为 0。

中断后从本报告与 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P7。只有 P10 综合生产验收才请用户统一测试效果；owner adapter 继续全关，O1 不得提前。
