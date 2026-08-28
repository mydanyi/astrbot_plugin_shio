# P3-08E4A AcceptedTurn 双 ticket 终结

日期：2026-08-18（Asia/Hong_Kong）

## 1. 结论

本层在纯本地 `AcceptedTurnAuthority` 内闭合了“consumer 没有领取 ticket 时如何明确终结”的生命周期缺口。一个 canonical accepted turn 仍固定签发 `OWNER_ACTION` 与 `AFFECT_STATE` 两张 exact ticket；每张 ticket 现在只能在以下两个互斥终态中选择一个：

- 被原 consumer 通过 `claim_ticket()` 领取；
- 未领取时由编排层通过 typed finalization 终结。

新增的 `AcceptedTurnDisposition` 是严格闭集：

- `SKIPPED`：本轮该 consumer 不适用；
- `ABORTED`：编排异常或提前停止，consumer 未开始；
- `REJECTED`：consumer 前置合同拒绝，ticket 未领取。

字符串 lookalike、copy、cross-dispatch、cross-binding、cross-authority、replay 和并发竞争全部失败关闭。只有两张 ticket 都进入 terminal，整轮才可淘汰；未领取、部分领取或部分 finalization 的轮次都不能被容量驱逐。

终审加固后，issuer snapshot 不再使用 Python 宽松 `==` 作为完整性边界。`DecisionBinding`、`TurnEnvelope`、`PrincipalContext`、context、dispatch、ticket 与 finalization 的每个权威字段都先验证 exact builtin/Enum/dataclass 类型，再比较 issuer 保存值；`True == 1`、`int/float/str/tuple` 子类、truthy-but-equal-false 对象和字段删除全部 typed 拒绝，且失败不消费 ticket。单票与整轮 finalization 现在都对 publish 写后抛出的任意 `BaseException` 回滚 ticket 与 receipt registry，恢复后可重试。

本层没有接 `main.py`、Affect、OwnerAction、配置、容器或 FNOS，也没有执行 Git/GitHub 写操作。

## 2. 根因

E0 的 ledger 只把 `claimed=True` 当作 consumer terminal。实际产品中，大量正常聊天不会走主人动作；异常提前退出也可能令 Affect 或 OwnerAction 没有机会领取各自 ticket。只要任意一张票一直未 claim，`_TurnRecord.complete` 永远为 false，terminal turn 无法淘汰，long-lived authority 最终必然报 `accepted_turn_ledger_full`。

不能用“把未使用票也 claim 掉”修补：claim 表示真实 consumer 已接管 authority，和“没有执行/没有接管”语义不同，也会破坏后续审计。因此需要独立、typed、exact、one-shot 的未领取终结合同。

## 3. 公开合同

新增公开类型：

```python
AcceptedTurnDisposition
AcceptedTurnFinalization
```

新增公开 API：

```python
authority.finalize_unclaimed_ticket(
    exact_ticket,
    consumer=AcceptedTurnConsumer.OWNER_ACTION,
    binding=exact_binding,
    context=exact_context,
    disposition=AcceptedTurnDisposition.SKIPPED,
)

authority.finalize_unclaimed_turn(
    exact_dispatch,
    binding=exact_binding,
    disposition=AcceptedTurnDisposition.ABORTED,
)
```

`finalize_unclaimed_ticket()` 要求 issuer registry 中同一 exact ticket、consumer、binding 与 context。context 的 binding、envelope、principal、sender kind、plugin source、content digest 均有 issuer-owned 独立快照；envelope/principal 使用显式字段元组，不调用 caller-controlled `deepcopy`/`astuple`。快照比较同时要求 exact 类型与值：所有字符串均为 builtin `str`，revision/epoch 为 builtin `int`，owner 标志为 builtin `bool`，timestamp 只允许 finite builtin `int|float`，degradation 为 builtin `tuple[str, ...]`，Enum 和 dataclass 外壳均为 exact class。替换 context、等价子类、`True == 1`、truthy equality 对象、嵌套字段篡改和任意权威 slot 删除都会在 claim/finalize 前转换为 typed corruption，不向外逸出 `AttributeError`。`finalize_unclaimed_turn()` 要求同一 exact dispatch 与 binding，并在同一 authority lock 内一次性预构造、发布该轮所有仍未 terminal 的 finalization；已 claim 的 ticket 保持原终态，不会被伪装成 skip/abort/reject。

`AcceptedTurnFinalization` 是 `init=False`、module-sealed、issuer-bound、exact-identity 的 opaque receipt。`inspect_finalization()` 只认可仍在同一 authority 有界 registry 内的原对象；copy、跨 authority 或字段篡改均不是 canonical receipt。

## 4. 状态机与淘汰

单 ticket 状态：

```text
UNCLAIMED
  -> CLAIMED
  -> FINALIZED(SKIPPED | ABORTED | REJECTED)
```

`CLAIMED` 与 `FINALIZED` 互斥且各自 one-shot。任一终态后的 claim/finalize/replay 都固定返回 `accepted_turn_ticket_replayed`。

整轮 `complete` 现在定义为“两张 ticket 均 terminal”，不是“两张都 claimed”。因此以下组合均为可淘汰终态：

- claimed + claimed；
- claimed + finalized；
- finalized + claimed；
- finalized + finalized。

零 terminal、只 claim 一张或只 finalize 一张仍是 active，容量满时新 admission proof 保持未消费并返回 ledger full。驱逐以整轮为单位，同时删除 dispatch、两张 ticket 与对应 finalization receipt 的 canonical registry entry；旧对象不能复活。

## 5. 并发与失败关闭

`claim_ticket()`、`finalize_unclaimed_ticket()` 和 `finalize_unclaimed_turn()` 共用 authority 的 `RLock`。所有 exact type、issuer、registry identity、consumer、binding、context、snapshot 与 terminal 状态检查都在写入前完成。

同一张 ticket 上 claim 与 finalize、finalize 与 finalize 并发时都只有一个赢家，其余固定 replay；不会同时形成 claim 和 finalization。单票与整轮 API 都在锁内先验证全部 exact authority，再构造 receipt；若 publish 在写 ticket、registry 或两者之后抛出任何 `BaseException`，共用 rollback 会清除该批 prepared receipt 在 ticket 与 registry 中的全部痕迹后原样抛出。因此单票写后异常和整轮第二次 publish 写后异常都保持零部分终结，恢复后可安全重试。

## 6. 隐私与可观测性

`AcceptedTurnFinalization.__repr__()` 与 `trace_metadata()` 只输出 schema、consumer、disposition、revision 与布尔状态。Authority trace 新增：

- `finalized_ticket_count`；
- `terminal_ticket_count`。

不会输出 scope/session/message/sender ID、正文、binding digest、ticket digest 或 finalization digest。新增测试把脱敏 marker 同时穿过 authority、dispatch、ticket、context、finalization 的 repr/trace，均无泄漏；即使 canonical nested binding 的 revision 被篡改为字符串，context trace/repr 也只输出闭集哨兵 `0`，不会回显该字符串。终审又逐字段删除 public context/ticket/dispatch/finalization 的 27 个字段并调用 trace/repr，全部保持闭集输出且零异常。

前一轮独立复审先复现了三项缺口：context principal/envelope 替换仍可领取、nested binding revision 可经 trace/repr 回显、整轮第二次 publish 注入异常后残留一张 terminal ticket。三项均先转正式红测；旧实现运行 30 项时得到 6 个 subfailure，随后才实施完整快照、闭集 trace 与整轮事务回滚。补充的 nested slot 删除红测又证明旧 helper 会逸出 `AttributeError`，实现改为统一转换成 `ContractViolation` 后才转绿。

本轮终审再次复现两类 blocker：第一，旧快照只比较宽松 `==`，把 `principal.is_owner=False` 换成 `bool=True` 但与 `False` 相等的对象后仍可 claim；`binding.conversation_revision=True`、`envelope.timestamp=True`、同值 `str` 子类以及 ticket/dispatch/finalization 同值错类型也全部可穿透。第二，单票 publisher 写入后抛出 `BaseException` 会残留一张 terminal ticket。正式 red 为 `38` 项中的 `14 failures + 5 errors`；5 errors 是 binding/context/ticket/dispatch/finalization 权威字段删除逸出 `AttributeError`。根修后同一 38 项全部通过。

## 7. 先红后绿

先修改 `tests/test_accepted_turn_authority.py` 引入新合同，旧实现稳定红灯：

```text
ImportError: cannot import name 'AcceptedTurnDisposition'
Ran 1 test
FAILED (errors=1)
```

实现后 focused 覆盖 39 项，新增边界包括：

- 三种 exact enum disposition 与字符串拒绝；
- 单 ticket finalization、整轮 finalization、claim/finalize 双向互斥；
- ticket/context/dispatch copy、cross-dispatch、cross-binding、cross-authority、replay；
- claim 与 finalize、finalize 与 finalize 的 8 线程竞争均只有一个成功；
- partial claim 与 partial finalization 均不可驱逐；
- 连续 512 轮正常 skip、异常 abort/reject、claim+abort 混合路径不触发 ledger full；
- finalization receipt exact inspection、copy/deepcopy、disposition/字段快照与 repr/trace 脱敏；
- public no-arg constructor shell 即使复制全部公开字段、digest 与 seal 仍不能通过 exact registry authentication；
- exact context 外层及 nested envelope/principal 字段篡改拒绝；嵌套字段删除也转换为 typed failure，恢复 canonical 字段后仍可正常 claim；
- whole-turn 第二次 publish 故障注入后零部分 terminal，移除故障后可一次终结两票；
- single-ticket publish 写后抛 `BaseException` 也零 terminal/零 receipt，移除故障后可正常终结；
- 55 个 snapshot/public 字段的等价错类型 mutation 与 55 个字段删除窄探针全部 typed 拒绝；恢复原字段后 canonical 对象仍可使用；
- 1024 轮 claim/finalize/churn 后 `turn_count=4`、`active_turn_count=0`、`ticket_count=8`，ledger 保持有界。

## 8. 验证证据

```text
focused:
python -m unittest astrbot_plugin_shio.tests.test_accepted_turn_authority -v
Ran 39 tests
OK

related:
python -m unittest \
  astrbot_plugin_shio.tests.test_accepted_turn_authority \
  astrbot_plugin_shio.tests.test_affect_state \
  astrbot_plugin_shio.tests.test_owner_action_router \
  astrbot_plugin_shio.tests.test_owner_action_controller -v
Ran 106 tests
OK

full（并行 E4C 终审红灯窗口，仅记录，不作为 E4A 绿灯）：
python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py' -q
Ran 1024 tests
FAILED (failures=2, errors=1, skipped=3)

三项失败全部位于 `test_owner_action_lifecycle` 的并行终审红测：exact numeric subclass、post-replace `BaseException` disable/reconcile、init `BaseException` lock release；E4A focused/related 均为绿灯。根任务将在 E4C 冻结后独立重跑全仓，本报告不把并行 full 冒充通过。

python -m compileall -q astrbot_plugin_shio
exit 0

git diff --check
exit 0
```

E4A 初次实现冻结时曾以仓库既有 discovery 口径通过 977/977；两轮加固后的当前 focused/related 已分别通过 39/39、106/106。当前全仓复跑与 E4C 正式红测并发，故如上如实记录而不冒充绿灯；E4A 最终全仓证据由根任务在 E4C 冻结后补验，并必须再由未参与修复的执行者独立终审。

## 9. 改动范围

仅修改：

- `core/accepted_turn_authority.py`；
- `tests/test_accepted_turn_authority.py`。

新增本报告：

- `docs/reports/P3-08E4A_ACCEPTED_TURN_FINALIZATION.md`。

没有修改 Affect、OwnerAction、Router、Main、配置、第三方插件、Docker 或 FNOS。

## 10. 后续门

E4A 只提供 authority 生命周期出口，不代表生产已经逐轮调用。下一层应在 E5 原子接线前继续闭合 E4 其余债：

1. OwnerAction/ActionOutcome active material 与最小 tombstone 分离；
2. delivery ack/reclaim 后清 route、参数、路径、命令、输出与 admission 强引用；
3. mutating action crash-in-progress 重启后固定 `UNKNOWN`，不得自动重试；
4. 最终由 E5 的唯一编排路径保证每个 accepted turn 的两张 ticket 都 claim 或 typed finalize。

上述生命周期、配置/main、容器/FNOS 均未在本层执行；production adapter 与 live output authority 继续 hard-off。
