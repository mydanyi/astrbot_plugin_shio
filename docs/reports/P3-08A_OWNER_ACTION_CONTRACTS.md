# P3-08A：主人动作 typed contracts

实施日期：2026-08-18（Asia/Hong_Kong）

## 1. 结论

P3-08A 已建立主人动作的纯类型合同与回归测试，但**没有接入生产执行链，也没有授予任何主人高权限能力**。

本层只回答以下问题：

1. 模型最多能提出什么样的低权限意图；
2. 代码选择 adapter 后，请求、二次确认、输出和回执必须具有什么封闭形状；
3. 哪些状态、效果、身份绑定、时间边界和输出组合属于不可能形状；
4. 哪些值不得进入模型 payload、通用参数字典、repr 或 trace；
5. 后续 canonical controller 接线前，哪些对象仍然只能视为数据 shape，不能视为执行 authority。

当前四个 closed operation 是：

- `artifact_read_exact`；
- `artifact_grep`；
- `memory_write_literal`；
- `sandbox_shell_once`。

它们只是 P3-08 首批候选操作的合同名称。没有 adapter registry、可信 owner authority、隐藏参数记录、执行器和默认关闭配置时，四项均保持生产不可达、失败关闭。

## 2. 本层文件边界

本次只改变以下四个文件：

- 新增 `core/contracts/owner_action.py`；
- `core/contracts/__init__.py` 只增加公开类型和 parser export；
- 新增 `tests/test_owner_action_contracts.py`；
- 新增本报告。

没有修改 `main.py`、`action_planner.py`、`tool_broker.py`、`capability_policy.py`、配置、executor、Grounding、Composer、SemanticGuard 或既有 P3-07 文件。

没有运行 Git，没有访问或部署 FNOS，没有写入真实身份、群聊正文、路径、命令、参数、凭据或生产结果。

## 3. 合同表面

| 合同 | 可携带内容 | 明确禁止 |
| --- | --- | --- |
| `OwnerActionProposal` | 当前 `DecisionBinding`、broad capability、closed operation、置信度、reason code | exact tool、source、adapter、参数、路径、命令、owner 声明、deadline、budget、确认材料、幂等材料 |
| `OwnerActionRequest` | binding、action/adapter identity、capability/operation/side-effect、参数和请求 digest、deadline、confirmation policy、digest 化幂等键、固定一次预算 | raw parameter mapping、tool name、source、path、command、任意 call budget |
| `PendingConfirmation` | 原 request、同 sender/scope/session lineage、request/parameter digest、TTL、后续确认 binding 和 proof digest shape | 跨用户、跨群、跨 session、同消息、旧 revision/epoch、截止时刻及之后、变更后的 digest |
| `ActionOutput` | 与完成 binding/request digest 绑定的有界 owner-only safe text，或单个 opaque artifact ref | public visibility、混合 text/ref、NUL、超长文本、未绑定输出 |
| `ActionReceipt` | origin/completion binding、action/request/adapter/capability/operation/side-effect、status/effect、attempt、幂等/source/result/confirmation digest、reason、可选 output | 公开构造冒充 issuer 回执、跨 request 输出、错误 status/effect/attempt/time/attestation 组合、Grounding 转换 |

所有公开 dataclass 都使用 `frozen=True` 和 `slots=True`；模型输入不接受通用参数 `Mapping` 透传。唯一的 `Mapping` 入口只用于解析四字段的低权限 proposal，并在构造 proposal 前拒绝 authority/parameter key 和未知 key。

## 4. Proposal 与 authority key 防线

模型 proposal 只允许：

- `capability_intent`；
- `operation_intent`；
- `confidence`；
- `reason_codes`。

parser 会先规范化 key 并拒绝重复 key，再拒绝以下类别的字段：

- exact tool、plugin/source/interface；
- args、arguments、parameters、path、command；
- sender、principal、owner、binding、scope/session/message；
- action/adapter/request/parameter identity；
- deadline、timeout、budget；
- confirmation、token、idempotency。

proposal 只表达“当前消息可能希望执行某类 closed operation”。它不能选工具、生成参数、证明 owner、设置预算或直接触发执行。

## 5. Request 与参数边界

`OwnerActionRequest` 的公开字段没有 raw parameter、path、command、tool 或 source。参数只表现为 `parameter_digest`，完整请求表现为 `request_digest`。

请求合同还强制：

- operation 与 broad capability 一一匹配；
- operation 与 side-effect 一一匹配；
- operation 只能使用其 closed confirmation policy；
- `call_budget` 必须是严格整数 `1`，布尔值、零和多次预算均拒绝；
- deadline 必须有限、晚于 issued time，且生命周期有上限；
- `now == deadline` 已视为过期；
- action、request、parameter、idempotency 均只接受固定长度 digest shape；
- adapter id/version 只接受有界 safe name。

但是，**公开可构造的 `OwnerActionRequest` 仍然只是 request shape，不是 authority**。它没有证明 sender 是可信 owner，也没有证明 adapter、参数 digest 或 idempotency material 来自 canonical controller。任何生产下游若仅凭这个 dataclass 执行，都是 P3-08A 明确禁止的旁路。

## 6. 二次确认 shape

需要二次确认的 operation 必须先形成 `PendingConfirmation`。合同验证：

- request 本身声明 `SECOND_TURN_REQUIRED`；
- pending 的 binding、action、sender、scope、request digest 和 parameter digest 与原 request 一致；
- issued/expiry 位于 request deadline 内；
- TTL 有硬上限；
- `now == expires_at` 已视为过期；
- 确认消息必须来自相同 sender、scope 和 session；
- 确认必须是不同消息、不同 content digest，并使用更高 conversation revision 和 generation epoch；
- 已标记 consumed 的 shape 不允许再次消费；
- 二轮 `ActionReceipt` 必须携带同一个 request object 对应的 pending shape；
- 执行回执必须使用 pending 中的完成 binding 和 confirmation proof digest；
- `CONFIRMATION_REQUIRED`、确认前取消/拒绝或启动前 stale 不能伪装成已确认。

`CONFIRMATION_REQUIRED` 仍严格绑定原始请求轮；未消费 pending 的 `CANCELLED`、`DENIED` 或启动前 `STALE` 可以绑定原轮，也可以绑定同 sender/scope/session 的后续新消息，但不得带 confirmation proof、attempt、source/result attestation 或副作用。这样既能表达主人下一轮取消，也不会把取消误记成确认。

这里仍没有实现并发安全的 canonical pending ledger。原始未消费 frozen 对象的原子 claim、多个 pending 的唯一选择、取消、过期清理和 proof 生成必须由 P3-08C controller 在锁内完成。调用者提供的 confirmation shape 不能单独成为执行 authority。

## 7. 输出 lineage 与隐私

`ActionOutput` 支持两种 closed shape：

1. 最大 `4096` 字符的 owner-only safe text；
2. 一个固定长度的 opaque artifact ref。

输出现在同时绑定完成 `DecisionBinding` 与 `request_digest`。`ActionReceipt` 会拒绝其他 sender/scope/epoch 或其他 request 的输出拼接。

repr 和 trace 不包含：

- sender/scope/message；
- action/request/parameter/idempotency digest；
- safe text 正文；
- artifact ref；
- source/result/confirmation digest；
- issued/deadline/TTL 时间。

`ActionOutput.safe_text` 是为后续 owner-only Composer materialization 保留的有界字段，不得交给通用 serializer、日志、ProductTrace、Grounding 或群友可见链。P3-08C 必须让 controller 对当前 owner binding 做 materialization 检查；若后续发现通用序列化无法可靠隔离，应将正文迁入 controller private record，仅在合同中保留 opaque handle。

## 8. Receipt 状态—效果矩阵

| Status | 合法 effect | Attempt | source/result attestation | Output |
| --- | --- | ---: | --- | --- |
| `CONFIRMATION_REQUIRED` | `NOT_STARTED` | 0 | 禁止 | 禁止 |
| `DENIED` / `CANCELLED` | `NOT_STARTED` | 0 | 禁止 | 禁止 |
| `STALE`（启动前） | `NOT_STARTED` | 0 | 禁止 | 禁止 |
| `SUCCEEDED`（scoped read） | `NO_SIDE_EFFECT` | 1 | 必须 | 必须 |
| `SUCCEEDED`（mutation/code） | `COMMITTED` | 1 | 必须 | 可选 |
| `FAILED`（read） | `NO_SIDE_EFFECT` | 1 | 必须 | 禁止 |
| `FAILED`（mutation/code） | `NOT_COMMITTED` 或 `PARTIAL` | 1 | 必须 | 禁止 |
| `TIMED_OUT`（read） | `NO_SIDE_EFFECT` | 1 | 必须 | 禁止 |
| `TIMED_OUT`（mutation/code） | `NOT_COMMITTED`、`PARTIAL` 或 `UNKNOWN` | 1 | 必须 | 禁止 |
| `STALE`（启动后 read） | `NO_SIDE_EFFECT` | 1 | 必须 | 禁止 |
| `STALE`（启动后 mutation/code） | `NOT_COMMITTED`、`COMMITTED`、`PARTIAL` 或 `UNKNOWN` | 1 | 必须 | 禁止 |
| `EFFECT_UNKNOWN`（mutation/code） | `UNKNOWN` | 1 | 必须 | 禁止 |

附加不变量：

- attempt 只能是 `0` 或 `1`；
- attempt 0 必须使用零执行时间，且不能携带 source/result attestation；
- attempt 1 必须有有限的开始/完成时间、source interface digest 和 result digest；
- 完成时间不得早于开始时间；
- 非成功状态必须有 closed reason code；
- read 成功不能伪装成 committed；
- partial/unknown/timed-out/stale-after-start 不能产生成功话术；
- second-turn 执行必须匹配 consumed pending shape；
- trace 只报告 `has_source_attestation` / `has_confirmation_proof`，不声称它们已经被 production controller 验真。

## 9. Receipt issuer seam 的准确含义

`ActionReceipt` 的公开构造器要求 issuer seal；普通构造、`dataclasses.replace()` 和复制后的对象不能自动登记为本模块 issuer-constructed receipt。合同模块使用一次性 construction seal 与 exact-object weak registry，为后续 controller 接入预留 issuer seam。

这层 seam **不等于生产 authority 或 executor attestation**：

- 当前私有 issuer helper 只供合同测试和后续 controller 适配；
- 下划线函数不是 Python 进程内的安全边界；
- 它尚未证明可信 owner、当前明确请求、canonical adapter、private parameter record、exact runtime tool、真实执行结果或幂等 ledger；
- `is_canonical` 在 P3-08A 里最多表示“由合同 issuer seam 构造且仍是 exact object”，不能被解释为“可以执行”或“执行结果已可信”。

P3-08C 必须把签发和 inspect 收归 per-controller issuer seal、nonce、exact-object registry 和 canonical private record。controller 之外不得调用 issuer helper，生产代码也不得仅凭 `is_canonical` 接受动作结果。

## 10. Grounding 隔离

本层没有提供 `to_grounding_fact()`、`as_grounding_fact()` 或任何 ActionReceipt → GroundingFact 转换，`owner_action.__all__` 也不包含 Grounding 类型。

已知接线红灯：当前 `ContentIntent` 的公开构造路径没有对 `grounding_facts` 中每个元素执行严格 `isinstance(GroundingFact)`。本轮按文件边界没有修改 `context.py`。在 ActionReceipt 接入任何生产 Composer/ContentIntent 前，必须先增加直接注入红测并闭合该类型门；在此之前 ActionReceipt 不得进入 ContentIntent/Grounding。

## 11. 验证记录

### 11.1 首次红测

新增合同测试后、实现合同前，使用 bundled Python 运行定向模块：

- 结果：导入 `ActionConfirmationPolicy` 失败；
- 结论：测试确实先于实现变红，而不是假绿。

### 11.2 独立复核后的第二轮红—绿

独立只读复核指出 output lineage、attempt/attestation truth、stale-after-start 和等号 TTL 边界仍不完整。先扩充红测，再实现收紧：

- 收紧前：新增用例因缺少 `call_budget` 等合同字段而失败；
- 冻结版终审又发现二轮取消/拒绝/启动前 stale 不能绑定后续主人消息；新增矩阵先稳定复现 3 个 `receipt_pending_shape_mismatch`，再最小修复；
- 定向合同测试：`21/21 OK`；
- 合同 + 既有 behavior contracts：通过；
- 完整 discovery：`691/691 OK`；
- 新合同和测试文件 `py_compile`：通过。

测试覆盖：

- 四个 operation 的 capability/side-effect/policy 映射；
- 模型 authority/parameter key 拒绝；
- 不存在 generic arguments/path/command/tool/source 字段；
- 跨用户、跨群、跨 session、跨 epoch/revision；
- 确认错绑、过期等号、变更 request/parameter digest、已消费 shape 重放；
- 二轮取消、拒绝和启动前 stale 的后续同身份 binding；
- output 跨 binding/request 拼接；
- status/effect/attempt/time/source/result/output 组合；
- bounded text、opaque ref、owner-only visibility；
- frozen、slots、repr/trace 脱敏；
- receipt public constructor 和 `dataclasses.replace()` 不能冒充 issuer-constructed exact object；
- 不存在 Grounding 转换 API。

## 12. P3-08C 前必须保持失败关闭

P3-08A 完成不代表主人动作可上线。以下条件缺少任意一项时，所有四个 operation 都必须失败关闭：

1. 可信 owner authority 必须来自结构化身份控制器，不能来自昵称、自称或可构造布尔值；
2. 当前明确请求必须与 proposal、planned action、binding、revision 和 epoch 形成 canonical lineage；
3. 每个 operation 必须有独立 code-owned parameter adapter，不能恢复 generic mapping；
4. raw path、command、literal memory、idempotency material 只能留在 controller private record；
5. Request/Pending/Output/Receipt 必须由 per-controller exact-object registry 签发并 inspect；
6. Pending 必须在锁内原子 claim，proof 由 controller 生成；
7. Idempotency ledger 必须保证同一 request 最多执行一次，重入返回同一 exact receipt；
8. exact runtime tool/source/interface/schema 必须通过 candidate conformance；
9. Handoff、MCP、background、direct-send、旧 fast/shadow/legacy 继续硬拒绝；
10. 所有 owner action 配置必须默认关闭，再逐 adapter 显式启用；
11. Grounding 类型门、Composer/SemanticGuard/Final validator 必须先有红测；
12. 生产接线、容器部署和 FNOS 验证必须在后续独立阶段完成。

因此本阶段的准确状态是：**typed contract green，production authority absent，production execution fail closed**。
