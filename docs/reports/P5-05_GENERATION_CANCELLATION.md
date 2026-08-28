# P5-05 新消息打断与重规划报告

## 1. 结论

P5-05 已完成，P5 阶段闭合。候选通过 Windows 与隔离 AstrBot Python 3.12 container 门，但未部署。

同一 scope 出现新的 accepted human turn 后，旧 DIRECT、`MAY_JOIN` 与 `REACT_ONLY` 共用的 generation epoch 都会失效：

- cancel-safe 星汐子任务会被取消；
- 不支持取消的 Provider/awaitable 可以完成底层工作，但返回值必须在 exact epoch 再验后丢弃；
- 迟到的 tool result、repair、guard 结果、FINAL_SEND 和多气泡剩余段均不得发送；
- 不同 scope 的当前轮次互不取消。

P5-05 不执行 Meme；P6 的 Expression executor 必须消费同一 exact generation authority。

## 2. 正式红灯

新增 `tests/test_p5_participation_cancellation.py` 后，4 项中 2 项稳定失败：

1. `GenerationEpochSnapshot(...)` 可由公开 constructor 任意构造；
2. 旧 canonical snapshot 可通过 `object.__setattr__(epoch=current_epoch)` 被 `validate()` 错误接受为当前轮次。

这意味着旧轮次即使自然地被新消息取代，同进程中的复制/篡改对象仍可能伪装成当前发送 authority。原 `GenerationTaskRegistry.cancel_older()` 也接受 caller-supplied snapshot，不能作为 code-owned 取消边界。

## 3. 根修

### 3.1 Registry-owned epoch snapshot

`core/generation_epoch.py` 现在：

- `GenerationEpochSnapshot` 的普通 constructor 关闭；
- snapshot 只能由 exact `GenerationEpochRegistry.advance()` 自行 mint；
- registry 以 weak exact-object record 保存 scope、session、epoch、clock、registry identity 与 seal；
- `validate()` 先重验 exact canonical identity 和完整字段快照，再比较当前 epoch；
- copy、public shell、字段删除、等值替换、跨 registry 与旧 epoch 改成当前值均 fail closed；
- `repr/trace_metadata` 对 corrupt snapshot 只返回闭集 invalid 状态，不回显 scope/session/marker；
- clock、scope limit、expected epoch 与 envelope 都使用 exact type/有界校验。

### 3.2 Registry-bound task cancellation

`core/generation_cancellation.py` 现在：

- `GenerationTaskRegistry` 必须绑定一个 exact `GenerationEpochRegistry`；
- `cancel_older()` 只接受该 registry 当前的 canonical snapshot；
- cancel-safe 子任务仍按同 scope、较旧 epoch 精确取消；
- cancel-unsafe 子任务返回后必须再次通过 current epoch 校验，否则抛 `SupersededGeneration`；
- cross-scope task 不被取消；
- task 表的登记、删除与计数受锁保护。

### 3.3 P5 热路径验收

生产 orchestration 在 accepted turn 成功提交 generation epoch 后才执行 P5 attention/participation/cadence/reaction。每个 accepted human turn 都会推进同 scope epoch并调用 `cancel_older()`；因此即使新消息最终选择 WAIT/NO_ACTION，也不会允许旧自然接话追上来。

新增测试验证：

- 新同群 direct turn 会在 guard 前清空旧 `MAY_JOIN` 文本结果；
- 新同群任意 accepted human turn 会使旧 `REACT_ONLY` authority 失效；
- 另一群的新消息不会使旧群 React 失效；
- 旧 snapshot 改写为当前 epoch 仍为 corrupt；
- scope marker 篡改不进入 repr/trace。

## 4. 验证证据

- formal red：4 项中 2 failures；
- generation epoch/cancellation + P5-05 targeted：`14/14`；
- P5/pipeline/tool/final-send related：`133/133`；
- Windows full：`1105/1105`，skipped 7；
- 隔离 AstrBot Python 3.12 container full：`1105/1105`；
- 最后 marker/trace 加固后的隔离 container targeted：`14/14`；
- `py_compile`、`compileall`、限定 `git diff --check` 通过；
- FNOS/container 暂存均按精确 `/tmp` 路径清理；
- 生产容器保持 `running=true`、`restart=0`、WebUI 200；
- 生产 `main.py` SHA-256 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`，未覆盖、未重载、未重启。

候选冻结哈希：

- `core/generation_epoch.py`: `e59b79ea1a973b46e09e0e1a50beafa3ba86f47c0250a053265e7bfdb6927572`；
- `core/generation_cancellation.py`: `e8f8b41ae53fc8ac9b5bd8a130db4a0d119873dedf7bec584f91048c9e438c22`；
- `tests/test_generation_cancellation.py`: `7decd36d7c90736357b5104ae124e0dbdd4eb07fc1a0868180877dca4b61ca55`；
- `tests/test_p5_participation_cancellation.py`: `e3633a1b307ab55d08402073309d30169cb57bcf42078a618b816c9daf21458c`；
- `main.py`: `0f883b2363af92905b37b6cec640728740093bdb3e05792e8079adae797b3b0f`。

## 5. 改动范围

- 修改 `core/generation_epoch.py`；
- 修改 `core/generation_cancellation.py`；
- 修改 `main.py`，让 task registry 显式绑定 exact epoch registry；
- 修改 `tests/test_generation_cancellation.py` 迁移到 canonical snapshot；
- 新增 `tests/test_p5_participation_cancellation.py`；
- 新增本报告并更新 `SHIO_MASTER_PLAN.md`。

未修改第三方插件、AstrBot 核心、配置 schema、FNOS 生产插件或 Git 状态。

## 6. 下一唯一入口

P5 已完成。下一唯一入口是 **P6-01 ExpressionIntent → Meme Manager 唯一执行契约**：只消费 exact current `PlannedAction`、`ExpressionIntent` 与 generation authority；不得把 Planner 草稿、Meme query/candidate/tag/raw result、私有记忆或工具参数带入执行器、历史、trace 或学习；未取得 live canonical Meme Manager exact-object conformance 时保持 fail closed。中断后从本段和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P5。只有 P10 才请用户统一测试效果。
