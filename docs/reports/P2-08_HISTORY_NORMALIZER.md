# P2-08 历史来源归一化与生产接线报告

## 结论

P2-08 已完成纯归一化内核和生产接线。`core/history_normalizer.py` 用强类型 receipt、target、source 和 current-action binding 决定 assistant/tool/plugin 历史是否可见；`main.py` 已不再导入或调用 `isolate_replyer_contexts()`，不会读取“前后紧邻的 user 是谁”来推断回复归属。

生产 `verified_thread` 和近期回复去重现在共用同一条归一化结果：只有星汐自身真实成功的发送回执与 ledger 全字段绑定后，assistant 文本才可进入当前对象的角色线程。仅在 AstrBot/LivingMemory 旧 history 中声明 `target_sender_id` 的 assistant 文本一律不可信，即使该字段恰好指向当前用户也会 drop。

## 先红证据

先创建 `tests/test_history_normalizer.py`，再运行：

```text
python -m unittest astrbot_plugin_shio.tests.test_history_normalizer -v
```

实现前的稳定失败为：

```text
ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.history_normalizer'
Ran 1 test ... FAILED (errors=1)
```

这证明测试先于实现存在，且会在新模块缺失时真实报错。

## 归一化合同

### assistant

只有同时满足下列条件的 assistant 段才进入 `CHARACTER_THREAD`：

1. `LedgerRecord.role == ASSISTANT` 且 `source_kind == OUTBOUND`；
2. ledger source/message ID 完全一致，并且存在唯一的 `SentReplyRecord` segment；
3. segment 真实为 `SUCCEEDED`，receipt 已终结且有有效成功时间；
4. receipt、ledger 的 scope、session、target sender、target message 与 segment 全部相互绑定；
5. ledger 正文与已成功 segment 的正文/digest 一致；
6. target sender 精确是当前人物线程的 sender。

receipt 缺失、失败、未终结、多个冲突回执、绑定不一致、legacy assistant 或未知 assistant 全部降为 `EXTERNAL_ASSISTANT + DROP`，且 drop 结果不保留正文。

A→B→延迟 assistant(A) 回归明确验证：B 的线程 drop 该 assistant；A 的后续线程仍可依 target+receipt 识别它。记录在列表中的相邻位置不参与裁决。

### tool/plugin

tool 结果只有在下列绑定全部成立时成为 `CURRENT_TURN_REFERENCE`：

- `ExternalPluginEvidence.binding` 与当前 `DecisionBinding` 完全相同，因而 scope、sender、message、revision、epoch 和 trace 均相同；
- candidate action ID 与 current action ID 相同；
- ledger 是当前 scope/session 中经验证的 `TOOL_RESULT`；
- tool name/call ID 精确匹配 ledger source ID；
- evidence 符合 P1 闭集插件合同。

因为 binding 包含当前 message/revision/sender，上一轮 tool 结果在下一轮必然 drop，即使 scope 和 sender 没变也不会滑入新问题。

| 来源 | P1 证据 | 历史结果 | 允许消费者 |
|---|---|---|---|
| LivingMemory | `MEMORY_REFERENCE` | 仅当前 action 参考 | `CURRENT_TURN` |
| AnySearch | `GROUNDING_FACT` | 仅当前 action 参考 | `CURRENT_TURN` |
| Meme Manager | `PRESENTATION_EFFECT` | drop（query/candidate/progress 不回灌） | 无历史消费者 |
| Parser | `EXTERNAL_OUTPUT` | drop（下载/解析/进度不回灌） | 无 |
| ReNeBan | `GATE_DECISION` | drop（只属当时 ingress gate） | 无 |
| admin/未知外部插件 | 非闭集来源 | drop | 无 |

所有归一化历史都在数据合同层禁止 `PUBLIC_SCENE`、`PERSONAL_MEMORY`、`PERSONA` 和 `LEARNING`。这不依赖 Prompt 提示模型“不要使用”。

## 隐私与可观测性

- context/candidate 中的 binding、action ID、tool coordinates 和 ledger record 均不进入 repr。
- normalized item 的正文字段不进入 repr。
- `trace_metadata()` 只输出 origin、disposition、visibility、消费者计数、有无正文和闭集 reason；不输出正文、scope/session/message/sender/action/reply/tool-call ID。
- drop 结果强制 content/digest/consumers 为空，避免下游误用被拒绝数据。

## 测试证据

```text
python -m unittest astrbot_plugin_shio.tests.test_history_normalizer -v
Ran 7 tests ... OK

python -m unittest \
  astrbot_plugin_shio.tests.test_history_normalizer \
  astrbot_plugin_shio.tests.test_conversation_ledger \
  astrbot_plugin_shio.tests.test_send_receipt \
  astrbot_plugin_shio.tests.test_p1_plugin_conformance \
  astrbot_plugin_shio.tests.test_conversation_event
Ran 50 tests ... OK
```

覆盖矩阵包含：receipt present/missing/failed，A/B 相邻与延迟 assistant，current/next-turn/other-sender tool，LivingMemory/Meme/Parser/AnySearch/ReNeBan，admin/未知外部输出，P1 合同不匹配，消费者限制和 trace 脱敏。

## 生产接线与新增根因修复

- `_build_typed_context()` 只接受与当前 `ConversationEvent`/`DecisionBinding` 一致的历史候选。
- 当前用户的历史 user 轮次必须有明确 sender；其他用户只在它是本轮精确引用消息时可见。
- assistant 候选必须来自 `ConversationLedger.OUTBOUND`，并与唯一 `SentReplyRecord` 的 scope、session、target sender、target message、segment ID、正文和 digest 全部一致。
- `ReplyComposer.verified_thread` 与重复抑制的 `recent_replies` 都从上述结果构造；旧的相邻归属函数已从生产入口移除，不再存在第二条 raw-history 旁路。
- 接线测试暴露并修复了一个原有合同错位：`SendSegmentAttempt.visible_text_digest` 曾截断为 16 位，而 ledger 使用完整 SHA-256，导致真实发送回执永远无法通过精确绑定。该字段现统一为完整 SHA-256；日志/trace 仍只输出计数与布尔值，不输出 digest 或正文。

测试先红：修改生产期望后，旧实现会把无发送回执的 legacy assistant 写入 prompt，定向测试稳定失败；接线后结果如下：

```text
python -m unittest \
  astrbot_plugin_shio.tests.test_pipeline.PipelineTests.test_typed_owner_private_rejects_legacy_assistant_without_send_receipt \
  astrbot_plugin_shio.tests.test_pipeline.PipelineTests.test_verified_sent_reply_is_visible_only_to_its_exact_target \
  astrbot_plugin_shio.tests.test_history_normalizer \
  astrbot_plugin_shio.tests.test_send_receipt -v
Ran 21 tests ... OK

python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py' -v
Ran 479 tests ... OK
```

## 剩余边界

- P2-08 已关闭 assistant 历史的生产旁路；typed plugin/tool 的 current-action 可见性仍要随 P2-05/P3 Tool Broker 接入，不能把旧 tool history 直接带到下一轮。
- 群公共话题不从 legacy assistant 或相邻记录重建；它由下一层 P2-03 `GroupScene` 仅消费 admitted human 与 receipt-proven Shio outbound。
- 本报告证明本地代码和测试闭环，不代表 FNOS 已部署；线上状态只在 P10 前完整验收并完成备份部署后更新。
