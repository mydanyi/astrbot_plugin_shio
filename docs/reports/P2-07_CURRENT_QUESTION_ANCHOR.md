# P2-07 当前问题语义锚点生产接线报告

## 结论

P2-07 已从纯模块接入新版生产管线。每个 `ACCEPT_HUMAN` turn 都在任何 LivingMemory、history 或 grounding 注入前，以 admitted canonical 原文和同一个 `DecisionBinding` 构造唯一 `CurrentQuestionAnchor`；非准入 turn 不创建锚点。生产顺序已固定为：

```text
admission → typed media → current question anchor → memory policy → history/context
          → local plan → composer → validator → optional one-shot repair
```

锚点负责“这一轮究竟在问什么”，人格只决定“怎么说”。历史、记忆、工具事实和人格均不能改写当前发送者、问题、否定、媒体来源或回答语言。

## 生产合同

- canonical 原文与 `DecisionBinding.current_content_digest` 必须完全一致；message、sender、scope、session、content digest 不一致时失败关闭。
- 超过 4000 字的消息仍用完整 canonical 原文做 admission、ReplyTarget 和 anchor 绑定；只有模型 Prompt 的展示副本被有界为 4000 字，并同时保留开头和结尾。
- owner 的 `/chat`、`/role` 前缀不再在 admission 后剥离，因此不会制造第二份正文或 digest 漂移。
- P2-06 的 typed `MediaContext` 先建立，anchor 只保存同一 binding 下的 typed `media_item_id`；URL、路径、base64 和 Provider 临时定位符不会进入 anchor、trace 或 Prompt。
- Composer 要求 anchor、ReplyTarget 和 MediaContext 完整同 binding，并把 `current_anchor`、`current_message` 放在 verified history、memory facts 和 persona 表达材料之前。
- repair 复用原 `ReplyComposerRequest`、同一个 anchor 对象和同一组媒体 transport arrays，不重新解释当前问题。
- 默认语言为 `zh-CN`；只有肯定式、明确要求英文才为 `en`。`不要/别/不用英文`、询问“为什么用英文”以及 `Why did you answer in English?` 都保持中文。
- trace 只记录分类、计数、覆盖布尔值和安全 taxonomy `answer_language_code`，不记录正文、实体值、media item ID、真实 sender/message/session ID 或原始 digest。

## 偏题检测边界

`coverage()` 仍是保守的词面 telemetry：自然同义改写即使零命中也不会因此阻断或修复。`current_question_context_drift` 只有同时满足以下条件才是 repairable：

1. 当前 anchor 含明确的 TOPIC、ENTITY 或 MEDIA_REFERENCE；
2. 最终回复没有任何当前 topic 的词面支持；
3. 最终回复可被确定为原样复制了允许进入模型、但不是 current message 的旧 context fragment。

ACTION 单独存在不算高置信偏题证据。因此“告诉我你记得的内容”可以直接返回一条合法记忆，不会因为没有复述“告诉”而误修。Anchor/target binding mismatch 属于 blocking，并在进入 repair 之前立即闭锁。

## 测试先红证据

生产测试先于接线创建，初始稳定失败于没有 `SHIO_CURRENT_QUESTION_ANCHOR`、Composer 不接受 anchor、Validator 无法取得 coverage。边界测试又分别捕获了以下真实回归：

- 4 个否定或元讨论英文样例被旧正则错误判断为 `en`；
- 超过 4000 字的合法消息因 presentation 截断而发生 `anchor_content_binding_mismatch`；
- Composer 会接受 session/content digest 漂移的 anchor；
- ACTION-only 的合法记忆回答被误判为旧话题复制。

修复后验证：

```text
current_question_anchor + reply_composer + output_validator_v2 + repair_controller
Ran 45 tests ... OK

test_pipeline
Ran 64 tests ... OK

media adapter + memory policy + presentation + P8 scenario/offline + repair
Ran 49 tests ... OK

full unittest discovery
Ran 570 tests ... OK
```

覆盖项包括 direct/owner `/chat`/长消息、中文与明确英文、否定与元讨论英文、typed media IDs、media→anchor→memory 顺序、自然同义零覆盖、明确旧 context 漂移、ACTION-only 合法记忆、同 anchor repair、non-accept 零 anchor、trace 隐私与完整 binding 失败关闭。

## 当前边界

- 本层没有把 `SemanticRequirement.alternatives` 当作关键词模板，也没有恢复旧 planner、shadow、旧版 fallback 或例句驱动的角色模板。
- 本层只对可证明的旧 context 原样复制做修复；一般语义等价和角色质量仍应由后续离线/真实模型 eval 评估，而不是扩大正则拦截。
- 本层未部署、未修改第三方插件，未执行 `git add`、`git commit`、`git push` 或任何 GitHub 写操作。
