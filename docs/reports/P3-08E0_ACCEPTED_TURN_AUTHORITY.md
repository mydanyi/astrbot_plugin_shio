# P3-08E0：AcceptedTurnAuthority 一次接纳、多消费者 authority fan-out

日期：2026-08-18（Asia/Hong_Kong）

## 1. 本层结果

本层新增 `core/accepted_turn_authority.py`，把一个仍处于 active 状态的 canonical `AdmissionProof` 原子转换为一组代码固定的、彼此独立的一次性 consumer ticket：

- `AcceptedTurnConsumer.OWNER_ACTION`；
- `AcceptedTurnConsumer.AFFECT_STATE`。

同一 long-lived `IngressAdmissionController` 的 proof 只由 `AcceptedTurnAuthority.dispatch()` claim 一次。之后 OwnerAction 与 AffectState 不再需要争抢原 proof，而应在后续接线层各自 claim 自己的 exact ticket。

这是一层纯 authority/shape 实现：不读取消息正文，不判断主人，不判断群聊/私聊，不运行模型、工具或插件，也不产生业务动作。`OWNER_ACTION` 只是固定 consumer 路由名，不在 dispatcher 内编码 owner 身份、关系或权限语义。

## 2. 冻结代码只读审计

接线前只读检查了三个现有模块：

1. `IngressAdmissionController.inspect_admission_proof()` 只接受当前 controller registry 中 exact canonical `AdmissionResult`/`AdmissionProof`；`claim_admission_proof()` 会把 proof 从 active registry 移到 consumed registry，第二次固定 replay。
2. `affect_state.issue_affect_admission_evidence()` 先 inspect；`AffectStateBook.record_human()` 再 inspect，成功变更前直接 claim 同一 admission proof。
3. `OwnerActionController._validate_admission()` 先 inspect；`issue_request()` 与 `resolve_pending()` 成功路径直接 claim 同一 admission proof。

因此旧结构不是偶发顺序问题，而是 authority 拓扑冲突：同一轮只存在一个 one-shot proof，OwnerAction 与 AffectState 都把自己当成唯一终端 consumer，先成功者必然令后成功者得到 `admission_proof_replayed`。

本层没有修改以上冻结模块；现有生产路径仍保持原状，不能把新增模块描述成已经修复线上争抢。

## 3. 公开合同与固定闭集

公开符号：

```python
AcceptedTurnConsumer
ACCEPTED_TURN_CONSUMERS
AcceptedTurnContext
AcceptedTurnTicket
AcceptedTurnDispatch
AcceptedTurnAuthority
```

核心调用面：

```python
authority = AcceptedTurnAuthority(admission_controller, max_turns=256)
dispatch = authority.dispatch(canonical_admission)
ticket = authority.ticket_for(dispatch, AcceptedTurnConsumer.AFFECT_STATE)
context = authority.context_for(
    ticket,
    consumer=AcceptedTurnConsumer.AFFECT_STATE,
    binding=dispatch.binding,
)
authority.claim_ticket(
    ticket,
    consumer=AcceptedTurnConsumer.AFFECT_STATE,
    binding=dispatch.binding,
    context=context,
)
```

内部实际使用 private `_FIXED_CONSUMERS`，公开 tuple 只是只读视图语义；即使调用方重绑公开模块属性，也不能改变 dispatcher 实际签发的固定集合。调用方不能传 consumer list，也不能要求只签发其中一张票。

`AcceptedTurnContext`、`AcceptedTurnTicket` 和 `AcceptedTurnDispatch` 都是 `init=False`、module-sealed、issuer-bound 的 opaque dataclass。`AcceptedTurnContext` 只投影 canonical admission 的 binding、结构化 envelope、principal、sender/plugin source 与正文 digest，不保存正文；consumer 不再通过重验公开 `ConversationEvent` 字段来猜测验真主体。Authority 用 per-instance canonical registry 加 exact object identity 验证：

- `copy.copy()`、`dataclasses.replace()`/自行构造、跨 authority 使用均失败关闭；
- dispatch、ticket、canonical committed `DecisionBinding` 都要求 exact identity，并再次核对签发时冻结的 binding parts/digest；
- context 必须是该 authority 为这张 ticket 签发的 exact object；自洽的 copied/forged event 或 context 不能替换 principal/relationship；
- 每个 consumer ticket 有独立 one-shot claimed 位，一张票被 claim 不会消费另一张；
- consumer 类型必须是 exact enum，字符串、另一个 consumer 或被篡改字段不能降级通过。

## 4. 原子性

`dispatch()` 的顺序固定为：

1. 用同一个 `IngressAdmissionController` inspect exact canonical active proof；
2. 验证 accepted conversation/binding/proof lineage；
3. 预计算容量、整组 dispatch/ticket 对象及其私有 record，但不写 canonical registry；
4. 调用 `claim_admission_proof()` 恰好一次；
5. 仍在同一 authority lock 内，一次发布完整 dispatch 与全部 consumer ticket。

所以 proof claim 之前发生的 invalid admission、copy、ledger full、构造/校验错误或 synthetic claim failure 都不会留下可领取的半组 tickets；claim 本身失败时 authority registry 仍为零。两个 authority 或多个线程同时竞争同一 proof 时，Ingress controller 的 canonical one-shot claim 只允许一个完整 dispatch 成功，其余调用没有 authority 状态可见。

claim ticket 时所有 exact type、issuer、seal、registry identity、consumer、binding identity及冻结 digest 校验都发生在 `claimed=True` 之前；错误 consumer、cross-turn 或 copy 不会误消费原票。

## 5. 有界 ledger

`max_turns` 必须为正整数，默认 256。每轮固定两张票，因此 registry 上界为 `max_turns` 个 dispatch 与 `2 * max_turns` 张 ticket。

- 尚未 claim 或只 claim 一张票的 turn 不可驱逐；容量满时新 proof 保持 active，返回 `accepted_turn_ledger_full`。
- 两张票都 claim 后，该 turn 才成为 terminal；下一次合法 dispatch 可把最旧 terminal turn 连同两张票作为一个单位驱逐。
- 驱逐后的旧对象不可能重新变成 canonical，后续 replay 仍失败关闭。

`trace_metadata()` 与自定义 `repr` 只输出 schema、consumer 闭集值、revision、布尔和计数；不输出 scope/session/message/sender ID、正文、binding digest、proof digest 或 ticket digest。私有 ledger record 也关闭 dataclass repr。

## 6. 先红后绿与验证

先新增 `tests/test_accepted_turn_authority.py`，实现不存在时定向运行稳定失败：

```text
ModuleNotFoundError: No module named
'astrbot_plugin_shio.core.accepted_turn_authority'

Ran 1 test
FAILED (errors=1)
```

实现后的定向矩阵覆盖：固定完整 consumer set、proof 单次 claim、每 consumer 独立 one-shot、admission/dispatch/ticket/binding copy、cross-dispatcher、cross-turn、错误 consumer、replay、claim failure 零发布、full/partial ledger 保留 proof、terminal whole-turn eviction、trace/repr 脱敏，以及 dispatch/claim 并发竞争。

```text
python -m unittest astrbot_plugin_shio.tests.test_accepted_turn_authority
Ran 19 tests
OK

python -m unittest \
  astrbot_plugin_shio.tests.test_accepted_turn_authority \
  astrbot_plugin_shio.tests.test_ingress_admission \
  astrbot_plugin_shio.tests.test_affect_state \
  astrbot_plugin_shio.tests.test_owner_action_controller
Ran 64 tests
OK

python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py'
Ran 754 tests
OK
```

其中 `754/754` 是加入 context authority 前的 E0 首次完整基线；context exact-identity 加固后的 focused `19/19` 已由 root 复跑，E0A/E0B 消费者迁移完成后必须再统一刷新相关与完整计数，不能拿旧完整计数冒充新 API 已全仓接通。

新增 module/test 的 `py_compile` 与三文件 whitespace/diff 检查通过。本次最终完整发现没有并发共享工作树导致的无关红灯；两项 authority 并发测试也稳定为单成功、其余 fail closed。

## 7. 生产未接线与下一步

本层刻意没有修改 `main.py`、`affect_state.py`、`owner_action_controller.py` 或配置，所以当前生产尚未消费 ticket，旧 controller 仍会直接争抢 AdmissionProof。下一接线层必须作为一个原子迁移完成：

1. bootstrap 对同一个 long-lived `IngressAdmissionController` 只创建一个 long-lived `AcceptedTurnAuthority`；
2. 每个 canonical accepted turn 只调用一次 `dispatch()`，并保存 exact complete dispatch/tickets；
3. AffectState 改为消费 exact `AFFECT_STATE` ticket，移除其 direct proof inspect/claim authority；
4. OwnerAction 首次请求和确认 follow-up 各消费对应 turn 的 exact `OWNER_ACTION` ticket，移除 direct proof inspect/claim authority；
5. 所有失败分支都必须明确决定未消费 ticket 的生命周期，并补 main/controller 原子接线、重启、有界清理及 end-to-end 回归。

在上述迁移、完整测试与部署验证完成前，E0 只能标记为 authority fan-out 合同已实现，不能标记为生产争抢已修复。本层没有 Git 写入，也没有访问或部署 FNOS。
