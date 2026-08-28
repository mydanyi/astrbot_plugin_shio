# P4-01 通用 AffectState 纯模块报告（含 P3-08E0A ticket 迁移）

更新：2026-08-19（Asia/Hong_Kong）

## 结论

P4-01 已完成纯模块、生产热路径接线和隔离 AstrBot 容器门。`core/affect_state.py` 建立了按 `scope + agent subject + current sender object` 隔离的 frozen/slots 连续情绪状态，并实现有界刺激、惯性、确定性时钟衰减、原因、更新时间和版本。P3-08E0A 已把它从“直接 inspect/claim canonical AdmissionProof”原子迁移为只消费 `AcceptedTurnConsumer.AFFECT_STATE` 的 exact one-shot ticket；2026-08-19 的热路径接线又把 `main.py` 中原先固定 `SKIPPED` 的 AFFECT ticket 改为真实 evidence→state claim，并在 actual terminal send receipt 后平复。

`core.affect.appraise_affect()` 仍负责本轮强类型情境轨迹；`AffectStateBook` 负责跨轮连续状态。P4-01 只完成可靠记录与回执平复，尚未把连续状态数值直接交给 Persona/Composer；这一消费边界明确留给 P4-02/P4-03，避免在同一阶段混入角色专属解释。

## 2026-08-19 热路径接线追加记录

### 原因与实现

此前 `ShioPlugin._bind_accepted_turn_authority()` 虽然取得 exact `AFFECT_STATE` ticket/context，却立即以 `AcceptedTurnDisposition.SKIPPED` 终结，所以纯模块从未进入真实聊天链。现在：

1. `ShioPlugin` 初始化时创建唯一 long-lived `AffectStateBook`，与 ingress、Owner Router 共用同一 `AcceptedTurnAuthority`；
2. canonical admission dispatch 后，代码用 exact dispatch/ticket 签 `AffectAdmissionEvidence`，再把同一 current-turn `AffectAppraisal` 投影到 closed `ModelAffectAppraisal` impulse；
3. `record_human()` 在任何 Provider/Composer 调用前 claim AFFECT ticket，并把 typed mutation 保存在 event-local extra；Owner NOOP route 同样 canonical claim，因此普通轮次为两票 claimed、零 skipped；
4. `build_persona_reply()` 要求 mutation 是 exact `ACCEPTED_HUMAN` 且 revision 等于 current binding，否则本轮以 `affect_state_unbound` 闭锁；
5. 发送计划、attempt 或 presentation seal 都不能平复情绪。只有 `after_message_sent` 已把最终 automatic segment 标记成功、内部 ledger 形成 terminal `SentReplyRecord` 后，才签 `ShioReceiptEvidence` 并调用 `record_shio_receipt()`；
6. 多气泡中间段不会提前平复；旧 generation、无发送、失败回执、跨目标和重复 receipt 仍由既有 exact contract 拒绝。

当前 trigger→state impulse 是通用 closed 映射：表扬/感谢→positive social，分歧→negative social，逗弄/被看穿→playful，关心→care，纠错/道歉→repair，其余→neutral fact。映射不含 Persona 名、台词、身份、权限或工具字段。

### 先红后绿与验证

新增 `tests/test_p4_affect_hot_path.py`，首次定向稳定得到 `1 failure + 2 errors`：`ShioPlugin` 不存在 `affect_states`，证明当时 main 仍未接连续状态。实现后：

```text
P4-01 hot path                         3/3
Affect/AcceptedTurn/Send/Owner/Pipeline 210/210
Windows full                           1061/1061 (skipped 7)
AstrBot Linux container full           1061/1061
py_compile + git diff --check          PASS
```

新回归锁定：positive state 有界；同 sender 下一轮版本递增且保留衰减余韵；另一个 sender 从独立 baseline 开始；长时间投影回归 baseline；send planning/attempt 不改变 state；actual terminal receipt 后 cause 变为 `SHIO_OUTBOUND_SETTLE`、version +1、intensity 降低；mutation repr/trace 不含正文或 sender ID。

容器候选包经 Windows→FNOS→AstrBot container 两段 SHA-256 核对后，只解压到容器 `/tmp` 并执行完整测试；测试通过后远端临时目录已删除。没有覆盖线上插件、没有重启容器。只读复核确认线上 `main.py` 仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`，container `running=true`、restart `0`、WebUI `200`。

### 恢复入口

P4-01 已冻结，下一唯一入口是 P4-02：把 Persona facts/价值/兴趣/关系动作规则作为 code-owned、只读、无授权能力的 typed projection 接入 ContentIntent/ReplyComposer。P4-02 不得通过 Persona 资产改变 owner、capability、tool、target 或 send authority；连续 Affect 的 Persona 外显合并留 P4-03。

## 先红后绿

- 先新增 `tests/test_affect_state.py`；
- 第一次定向运行稳定失败：`ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.affect_state'`；
- 初版实现后的 11 项中 2 项暴露同一数值校验错误：合法负 valence 误用了“时钟不可为负”的校验器；
- 拆分 finite-number 与 nonnegative-clock 校验后定向测试 `11/11` 通过；
- 安全复核发现 `IngressDecision`、`AdmissionResult` 与 `SentReplyRecord` 都是公开 dataclass，单靠值一致不能作为颁发证明；本模块增加 opaque `AffectAdmissionEvidence` 与 `ShioReceiptEvidence`，其中 receipt 已通过 canonical send-ledger lookup 关闭伪造路径；
- 同次复核还修复模型 key strip 后可能接受空白别名的问题，并对 receipt 的 tuple、segment 类型、成功状态、内部 reply ID、时间和正文 digest 逐项验证；新增 forged/malformed receipt、rejected admission、空白别名和非字符串 key 回归后仍为 `11/11`。
- 二次只读复核确认 receipt 与模型 key 两项已经关闭，但指出 admission issuer 仍不可信；随后 P2-02D 先新增公开伪造、隐藏字段复制、跨 admission、跨 controller、wrong-event、gate/proof replay 红测，再把 VERIFIED gate issuer 收回同一个 `IngressAdmissionController`，绑定 exact event 后才在 canonical revision commit 后签发一次性 proof，关闭该阻塞。
- P3-08E0A 迁移先修改测试合同，旧实现稳定产生 9 个 error：`AffectStateBook` 仍要求 `admission_controller`，`issue_affect_admission_evidence()` 仍要求 raw `AdmissionResult`，新 `dispatch`/ticket 参数不存在。
- 初次 ticket 迁移后 11/11 曾通过，但安全复核用 `copy.copy(canonical ConversationEvent)` 证明：只重验同一个 binding 的公开字段不能证明 event canonical identity。该红灯没有用更多字段比较掩盖；上游 E0 增加由 canonical admission 一次投影的 exact `AcceptedTurnContext`、`context_for()` 与 claim context identity 门后，Affect issuer 完全删除 caller event 参数，再转绿。
- 新增 claim synthetic failure、stale clock 后重试、copy ticket/dispatch/context、cross-dispatcher、错误 consumer/binding、raw Result/Proof、replay 和结构身份漂移矩阵；所有失败均在 Affect 状态写入前结束。

## AffectState 合同

`AffectState` 为 frozen/slots，包含：

- `scope_key`：可信 ConversationEvent 的结构化 scope；
- `subject_key`：代码根据同一 scope 与可信 bot ID 生成的 agent subject；
- `object_key`：当前可信 sender key；
- `valence`：`[-0.95, 0.95]`；
- `arousal`：`[0, 0.95]`；
- `intensity`：`[0, 0.85]`；
- 闭集 `AffectCause`；
- `inertia`：`[0.05, 0.90]`；
- code-owned `updated_at`；
- 每个 subject/object 状态的单调 `version`；
- 最后一次绑定的人类 `conversation_revision`。

subject 必须属于本 scope 的 agent，object 必须属于本 scope 的 user；跨群 object 无法构造为同一状态。状态簿按 `(scope_key, object_key)` 存储，A→B→A 分别更新 A/B/A，B 的情绪不会成为 A 的初值，相同 sender ID 在另一个群也使用独立状态。

## 人类事件准入与零写边界

`record_human()` 不接受 `AdmissionResult`、`AdmissionProof`、caller `ConversationEvent` 或公开 decision；只接受 module-sealed `AffectAdmissionEvidence`。`core/affect_state.py` 已删除对 `ingress_admission` 的 import，`AffectAdmissionEvidence` 也不再保存 admission/proof/event。

证据入口固定为：

```python
issue_affect_admission_evidence(
    exact_affect_ticket,
    dispatch=exact_dispatch,
    accepted_turn_authority=long_lived_authority,
)
```

入口先用 `ticket_for()` 证明 ticket 属于该 exact dispatch 且 consumer 恰为 `AFFECT_STATE`，再用 `context_for()` 从 authority 取得 `AcceptedTurnContext`。这个 context 是上游在 canonical AdmissionResult proof 被消费前一次生成的 content-free projection，包含 exact binding、结构化 envelope/principal、sender kind、plugin source 与 content digest；调用方不能传 event、principal、scope、sender 或 context 替代品。

仅靠 binding 相等仍不够：copy 出来的、字段完全自洽的 `ConversationEvent` 已经没有 API 参数可以进入；copy/伪造的 context 即使字段全部相同，也会在 `claim_ticket(..., context=exact_context)` 的 exact identity 门失败。Affect 另外复核 scope/bot/chat、sender key、principal、binding、HUMAN、`PluginSource.NONE`、inbound、非 degraded 与可信结构 sender source，防止 canonical context 对象被低层篡改后降级使用。

`AffectStateBook` 只绑定同一个 long-lived `AcceptedTurnAuthority`。`record_human()` 的执行顺序是：验证 sealed evidence/authority/context/ticket → revision/clock/identity 硬门 → 解析闭集 soft appraisal → 计算完整 next state 与 target record → claim exact `AFFECT_STATE` ticket/context → 一次写入 state、scope revision 与 target ledger。claim 之前没有状态写；claim 之后没有业务校验或模型调用。synthetic claim failure、copy、cross-dispatch、错误 consumer/binding/context、raw Result/Proof 与 replay 均已证明零写；stale clock 返回 typed rejection后，同一张票仍可在合法时钟重试成功。

banned、self、known bot、plugin echo 或 degraded 输入在上游无法形成 accepted dispatch/Affect ticket。本 scope 内 stale/revision gap/stale clock 仍返回 typed rejection，模型 appraisal 只在 sealed ticket/context 与 revision/clock 硬门之后解析。

## Verified Shio receipt

`record_shio_receipt()` 也不直接接受公开 `SentReplyRecord`。`issue_shio_receipt_evidence()` 必须先通过 `InternalSendReceiptLedger.sent_reply_record(internal_reply_id)` canonical lookup 取得同一个回执，并签发 module-sealed evidence。随后只有以下条件同时成立才做一次“发送后平复”：

- `reply_id` 是星汐内部回执 ID，target source 为 `current_inbound`；
- 回执精确命中此前已准入 event 的 scope/session/message/sender/content digest/trace/reference；
- 回执 terminal 且至少一个 segment 实际 succeeded；
- 每个成功 segment 的 internal reply ID、完成时间、可见正文 digest 与回执一致；
- 回执对象的状态仍绑定该 target revision，旧 target 不能越过同一用户的新轮次；
- receipt ID 未消费过，clock 不早于 state 或真实 sent time。

验证通过后只降低 arousal/intensity，并把 cause 记为 `SHIO_OUTBOUND_SETTLE`；不会从出站正文重新推断人物关系或情绪。failed、伪造 seal、malformed、重复、跨用户、跨群、错 trace/target 和 late-old-target 全部零写。这里的“verified”指星汐现有 canonical send ledger 已记录成功；通用 AstrBot 发送目前没有可携平台 message ID 的跨适配器证明，本阶段没有伪造更强保证。

## 有界更新、惯性与衰减

模型 soft appraisal 命中闭集且 confidence 至少 `0.60` 时，代码把它映射为通用 target valence/arousal/intensity/inertia，再使用有界凸更新：

```text
next = current + response * (target - current)
```

`response` 由代码限制在 `0.75` 以下。相同刺激连续出现只会收敛到 target，不会线性叠加越过全局上限。

时间衰减由显式 code clock 与固定 `300s` interval 决定：

```text
retention = inertia ** (elapsed / 300s)
```

valence、arousal、intensity 随时间回归零；足够接近零时 cause 回到 baseline。普通事实与低置信 appraisal 不制造新刺激，只保留或衰减已有状态。`project_state()` 可按固定时钟预览衰减后的 frozen state，但不提交版本或修改状态簿。

## 模型软输入边界

`ModelAffectAppraisal` 只有两个字段：

- 闭集 `appraisal_hint`：neutral fact、positive/negative social、playful、care、repair；
- `[0,1]` confidence。

Mapping parser 要求原始 key 必须是无首尾空白的字符串，且字段集合精确等于这两个字段；空白别名、重复归一 key 与非字符串 key 同样拒绝。owner、identity、scope、subject/object、target、binding、sender/message、time、updated_at、version、valence/arousal/intensity/inertia/cause 以及任意未知字段全部拒绝。模型不能直接设置数值状态、原因、对象、时钟或版本。

## 通用性与隐私

新模块不导入 Persona，不包含角色名、角色台词、口头禅或特定人格规则。它只产生通用 affect state；P4-02/P4-03 才负责把同一状态映射为可替换 Persona 的不同表达。

scope、subject 和 object 均为 `repr=False`。状态与 mutation trace 只包含数值、闭集 cause、revision/version、状态布尔和拒绝原因，不含正文、昵称、原始 scope/sender/message ID、回执文本或模型 payload。

## 验证

P3-08E0A 当前冻结结果：

```text
python -m unittest astrbot_plugin_shio.tests.test_affect_state
Ran 12 tests
OK

python -m unittest \
  astrbot_plugin_shio.tests.test_accepted_turn_authority \
  astrbot_plugin_shio.tests.test_affect_state \
  astrbot_plugin_shio.tests.test_ingress_admission \
  astrbot_plugin_shio.tests.test_send_receipt
Ran 57 tests
OK
```

覆盖 frozen/slots、subject/object/scope 隔离、A→B→A、跨群、fixed Affect ticket、raw Result/Proof 拒绝、ticket/dispatch/context copy、cross-dispatcher、wrong consumer/binding、replay、claim failure、stale/gap/clock、普通事实、低置信、模型权威字段与 key 别名、重复刺激上限、确定性衰减、verified/forged/malformed/failed/duplicate/cross-user/late receipt 与 repr/trace 隐私。

共享工作树第一次完整发现运行了 756 项，产生 21 个 error，全部位于并发中的 P3-08E0B `test_owner_action_controller.setUp`：测试侧已开始传 `AcceptedTurnAuthority`，当时 controller 侧仍要求 `IngressAdmissionController`，统一错误为 `ingress_admission_controller_required`。E0A focused/相关 57 项无失败；本报告没有把该次并发快照冒充全量绿灯，也没有越界修改 OwnerAction。E0B 收口后的统一 full 由 root 复跑。

以下为 2026-08-18 E0A 纯模块历史范围：`core/affect_state.py` 与 `tests/test_affect_state.py` 当时已通过 `py_compile` 和 whitespace 检查；E0A 只修改本模块、对应测试与本报告，没有修改 `main.py`。2026-08-19 热路径接线的实际范围与验证以本报告顶部追加记录为准；仍未部署、未执行 Git/GitHub 写操作。

历史 P4-01 基线为定向 11/11、最终全量 628/628；这些数字仅记录原纯模块阶段，不代表本次共享工作树的当前全量结果。

## 已完成接线与后续边界

- main 已把消费 ingress proof 的同一个 long-lived `AcceptedTurnAuthority` 传给 Affect evidence issuer 与 `AffectStateBook`；临时创建另一 authority、另一个 dispatch 或 ticket/context 副本仍会失败关闭；
- main 已对每个 canonical accepted turn 恰好 dispatch 一次，并把同一 dispatch 的 `AFFECT_STATE` ticket 交给本模块；相关热路径与容器回归由 2026-08-19 追加记录锁定；
- P4-02/P4-03 才能把 AffectState 与 Persona facts、关系动作和 Renderer 接通；
- 每个 ACCEPT_HUMAN event 必须继续按 scope revision 顺序送入状态簿，否则 revision gap 固定失败关闭；
- 出站适配器只能提交 `InternalSendReceiptLedger` 产生的实际回执，不能用时间邻近、相似正文或日志猜测补回执；
- 状态持久化、重启恢复、跨进程并发和生命周期淘汰属于 P9，不在本纯模块阶段伪造实现。
