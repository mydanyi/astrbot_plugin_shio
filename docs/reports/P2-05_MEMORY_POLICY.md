# P2-05 LivingMemory typed adapter 与逐轮 MemoryPolicy

## 1. 结果

P2-05 纯模块与生产接线均已完成。每个被 P2-02 确认为 `ACCEPT_HUMAN` 的 group/private conversation revision 现在会在 `main.py` 的 target 阶段之后、Scene/Media/Prompt 之前，通过唯一 `MemoryPolicy` 入口得到：

- 恰好一个 P1 `MemoryDecision`；
- 恰好一个 P1 `ExternalPluginEvidence`；
- 当前主体事实与公共背景的严格分区；
- 当前消息始终排在记忆参考之前；
- 同一 binding 重入时复用同一结果，近期历史读取最多一次；
- LivingMemory 缺失、禁用、超时、错误、接口变化和 hook 顺序异常的显式降级；
- 生产 Prompt 只消费 policy 选出的 facts，不再直接解析 ProviderRequest 或私下读取 LivingMemory 历史。

旧 `_livingmemory_contexts` 已删除。生产路径不再读取 `metadata.star_cls`、initializer、conversation manager 或 `get_messages` 私有链，也不再把 LivingMemory 原始消息作为第二份 history 注入。没有修改 LivingMemory、AstrBot 核心或其他第三方插件。

## 2. 审核基线

实现前重新核对了以下边界：

1. P1 `MemoryDecision` 已禁止其他主体和已知不安全来源进入 `selected_facts`，`SKIP/DEGRADED` 必须为零事实、零预算。
2. P1 `ExternalPluginEvidence` 只有 `VERIFIED + trusted` 才能成为 `CURRENT_TURN_REFERENCE`，其他状态必须 `DROP` 且没有消费者。
3. P2-01/P2-02 只有 `ACCEPT_HUMAN` 会产生已提交的 `ConversationEvent`；banned/self/known-bot/plugin-echo/外部门降级都不能进入记忆消费。
4. 原生产代码同时看到 LivingMemory 私有近期消息和 ProviderRequest 中已经提供的自动召回；如果再分别盲查，会形成重复读取和两套事实来源。接线后只有 policy 的一次归一化入口。
5. LivingMemory 当前默认的 `<RAG-Faiss-Memory>` 文本没有足够的结构化主体、scope 和相关度证明，不能从正文、昵称、参与者文字或相邻消息猜主体。

新的 adapter 只接受 P1 conformance 的闭集状态与调用方注入的一次性公开 reader。AstrBot 当前可验证的生产公共面只能确认插件 `name/activated`；未发现 LivingMemory 对星汐公开的版本化 recent reader。因此 active 但无 reader 时显式标记 `INTERFACE_CHANGED/public_reader_unavailable`，绝不通过私有对象“补读”。

## 3. 纯模块流程

```text
IngressDecision(ACCEPT_HUMAN) + ConversationEvent(binding)
  -> MemoryPolicy single-flight cache
  -> LivingMemoryAdapter.collect
       provided recall：读取已存在的 ProviderRequest 结构，零新增 read
       recent history：可选公开 reader，最多一次
  -> provenance / sender / source / relevance / scope filter
  -> current-subject facts + public-background facts
  -> MemoryDecision + ExternalPluginEvidence
  -> FactSelection(selected only)
  -> Reply Composer Prompt（current_message 在前，memory facts 在后）
```

同一 `DecisionBinding` 的并发或重复调用由 `ShioPlugin` 持有的同一个长期 `MemoryPolicy` 实例串行化并复用首个 immutable result。`_ensure_memory_policy_result` 同时检查 event extra 的 binding；不同 binding 的陈旧结果会失败闭锁。

## 4. 模式与读取预算

| 条件 | `MemoryMode` | 近期 reader | 已提供召回 | 事实预算 |
|---|---|---:|---:|---:|
| 不需要近期或长期记忆 | `SKIP` | 0 | 不消费 | 0 |
| 只需要近期连续性 | `RECENT_ONLY` | 最多 1 | 不消费 | 1～20 |
| 当前问题需要长期事实，且有公开 recent reader | `SEMANTIC_RECALL` | 最多 1 | 合并当前 ProviderRequest 已提供结果，不再发起第二次语义查询 | 1～20 |
| active 插件、无公开 recent reader、已有当前轮结构化 provided recall | `SEMANTIC_RECALL` | 0 | 只消费同主体、过阈值的当轮 recall；reader 独立标记 degraded/provided_only | 1～20 |
| 插件状态或读取异常 | `DEGRADED` | 0 或失败的 1 次 | 不消费 | 0 |

`provided recall + recent` 在 adapter 内统一为候选，再按规范化正文、主体和 scope 去重；不会形成两个 `MemoryDecision`。当插件已由 AstrBot 公共 metadata 验证为 active、但没有公开 recent reader 时，结构化 `fake_recall_*` 仍可在零读取下作为当轮 transport 使用；trace 同时保留 `memory_recent_reader_status=interface_changed`、`public_reader_unavailable` reason code、`memory_provided_only=true`。若插件缺失/禁用，外观相同的 payload 不受信任并被清空。

## 5. 外部状态闭集

| 状态 | 决策 | evidence 可见性 | 消费结果 |
|---|---|---|---|
| `VERIFIED` | `SKIP/RECENT_ONLY/SEMANTIC_RECALL` | `CURRENT_TURN_REFERENCE` | 仅允许 `CURRENT_TURN` |
| `MISSING` | `DEGRADED` | `DROP` | 零事实、零读取 |
| `DISABLED` | `DEGRADED` | `DROP` | 零事实、零读取 |
| `TIMEOUT` | `DEGRADED` | `DROP` | 一次失败读取后闭锁 |
| `ERROR` | `DEGRADED` | `DROP` | 一次失败读取后闭锁；不回显异常详情 |
| `INTERFACE_CHANGED` | `DEGRADED` | `DROP` | 零事实，或一次返回形态验证失败后闭锁 |
| `HOOK_ORDER_INVALID` | `DEGRADED` | `DROP` | 零事实、零读取 |

这些状态由公开注册/conformance 边界提供；adapter 不自行探入第三方对象来“猜可用”。唯一的部分可用情况是“公共 metadata 已验证 active + 当轮 ProviderRequest 已有结构化 recall + recent reader 不公开”：此时 `ExternalPluginEvidence` 只证明被 policy 筛过的当轮 recall，reader 不可用则由独立、闭集、无正文 trace 字段记录，不能被误写成 recent read 成功。

## 6. 主体、来源与 scope 规则

### 6.1 当前主体

- `subject_key` 必须是当前 binding scope 下的结构化 sender key。
- 只有与 `binding.current_sender_key` 完全相等、且 confidence/relevance 达阈值的个人事实可进入当前主体参考。
- owner 与 peer 使用相同的主体隔离和相关度阈值；owner 身份不会扩大跨用户记忆读取范围。

### 6.2 公共背景

- 无主体内容只有显式 `group/group_background/global/public` scope 才是候选。
- 群聊可使用当前 scope 下的高相关 group/public 背景；私聊拒绝 group 背景，只允许显式 global/public。
- 无主体 personal、未知 scope、低 confidence 或低 relevance 一律丢弃，不能降级冒充公共背景。
- LivingMemory 默认的未结构化 RAG 文本只累计 `unstructured_recall` 脱敏计数，不保存正文、不进入 Prompt。

### 6.3 硬排除

以下候选在 adapter/policy 边界即丢弃，不能成为当前主体事实或公共背景：

- 其他 sender 的个人事实；
- banned 标记；
- self/known-bot/automation/assistant 内容；
- Parser、Keywords、progress、management、Meme query/presentation、tool/plugin echo；
- 外 scope、外 session、缺主体的近期消息；
- 低相关个人文本。

当前消息不作为 adapter 输入正文保存。`MemoryPolicyResult.context_order` 固定以 `current_message` 开头，记忆只作为后置参考，不能覆盖当前问题。

## 7. 隐私与可观测性

- `LivingMemoryReadRequest` transport 可在调用时访问 session/scope/binding，但自定义 repr 不显示任何 ID。
- adapter candidate/result 与 policy result 的 repr 只显示闭集状态、布尔值和计数，不显示正文、sender、session、message、scope、异常详情或插件对象。
- `trace_metadata()` 只输出模式、状态、预算、计数、group/private、owner/peer 等闭集分类。
- 排除项只记录 reason→count；被排除正文不保留到 `MemoryPolicyResult`、trace 或报告。
- 结构化被选事实只存在于 typed decision 与当前轮 `FactSelection` 中，供 Reply Composer 使用；不进入 trace、日志或学习。
- `main.py` 的 memory trace 只记录闭集状态、reason code、provided_only、读取/排除/选中计数；不会记录 recall 正文、异常详情、原始 ID 或第三方对象。
- `_activate_typed_reply_request` 清空原 `req.contexts`，因此未选中的 provided recall 不会绕过 policy 随原始 ProviderRequest 进入模型。

## 8. 测试驱动证据

纯模块先添加 `tests/test_memory_policy.py`，首次运行稳定失败：

```text
ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.memory_policy'
```

生产接线再先添加 4 个 `tests/test_pipeline.py` 红灯，首次运行均因 `main.SHIO_LIVINGMEMORY_ADAPTER` / `main.SHIO_MEMORY_POLICY_RESULT` 尚不存在而失败；随后完成接线，并补齐 active-no-reader provided-only 与 missing-plugin fake payload 对照。覆盖：

- group/private × owner/peer；
- `recent_only/semantic_recall/skip/degraded`；
- provided recall 与 recent 合并去重、一次读取和重复调用幂等；
- missing/disabled/timeout/error/interface_changed/hook_order；
- 其他用户、banned、bot、plugin、无主体、低相关和跨 scope；
- 当前消息顺序与 repr/trace 脱敏；
- 非 `ACCEPT_HUMAN` 零读取；
- active + 无 public reader：无 provided recall 时显式 `INTERFACE_CHANGED`，有同主体结构化当轮 recall 时零 recent read 消费安全事实，并单独记录 reader degraded/provided_only；
- missing plugin 即使携带相同 fake recall payload 也不得信任。

验证结果：

- P2-05 纯模块定向：`13/13` 通过。
- P2-05 生产新增场景：`6/6` 通过。
- 完整 pipeline 模块：`59/59` 通过。
- P1 behavior/context/plugin contract、P2 conversation/admission/history 与旧 context regression：`122/122` 通过。
- P2-03～P2-06 并行接线稳定后的完整本地回归：`526/526` 通过。
- `py_compile` 与 `compileall`（`main.py`、adapter、policy、memory/pipeline tests）通过；相关文件 `git diff --check` 通过。

## 9. 文件与生产边界

纯模块阶段新增：

- `core/plugin_adapters/livingmemory.py`
- `core/memory_policy.py`
- `tests/test_memory_policy.py`
- `docs/reports/P2-05_MEMORY_POLICY.md`

生产接线修改：

- `main.py`
- `tests/test_pipeline.py`

当前生产不变量：

1. 在 P2-02 `ACCEPT_HUMAN` 之后、Scene/Affect/Prompt 之前调用一次；
2. 从 P1 conformance/公开注册边界传入闭集插件状态；
3. 通过公开、版本化 transport 注入一次性 recent reader，不从星汐散落访问第三方私有对象；
4. 复用运行时单例 `MemoryPolicy`，让同一 binding 的重入返回同一 decision/evidence；
5. 只把 `selected_facts` 作为当前消息之后的参考，不恢复旧 `adapt_livingmemory_facts` 的无主体 public grounding 行为；
6. 保持 `X-01`：星汐这里只保证 banned 内容零消费，LivingMemory 被动捕获前的 ReNeBan 支持仍留给 P10 后可选 O1。

本阶段未修改总计划、第三方插件、AstrBot 核心或生产环境，未执行任何 Git 写操作。LivingMemory 若要提供 recent history，后续应增加明确版本与绑定字段的公共 reader 接口；在此之前生产会显式 degraded，而不是恢复私有读取。
