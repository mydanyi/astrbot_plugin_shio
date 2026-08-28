# P2 阶段完成报告

## 结论

P2 的消息准入、历史来源、群聊场景、群聊/私聊地址、LivingMemory 消费、AstrBot 原生媒体和当前问题语义锚点已经在本地新版唯一 typed 底座上闭环。P2 不再只是 dataclass 或离线模块：关键能力已进入 `main.py` 的真实 AstrBot request/send 路径，并有完整回归证据。

本阶段没有部署 FNOS，没有修改 AstrBot 核心或第三方插件，也没有执行 Git/GitHub 写操作。

## 已完成层

| 层 | 生产结果 | 独立报告 |
|---|---|---|
| P2-01 ConversationEvent | accepted-only revision、generation epoch、sealed source | `P2-01_CONVERSATION_EVENT.md` |
| P2-02 Ingress Admission | ReNeBan 后 priority 90 准入；self/known bot/plugin/gate degradation 零星汐消费 | `P2-02_INGRESS_ADMISSION_INTEGRATION.md` |
| P2-08 History Normalizer | 只有 terminal successful Shio receipt 能进入角色线程；legacy/unknown/plugin/tool 跨轮 drop | `P2-08_HISTORY_NORMALIZER.md` |
| P2-03 GroupScene | accepted human 与成功 Shio outbound 同 revision 记录；公共/个人分区 | `P2-03_GROUP_SCENE.md` |
| P2-04 Address | group 多类对象识别；private 仅凭可信通道 `DIRECT_SELF / PRIVATE_CHANNEL` | `P2-04_ADDRESS_RESOLVER.md` |
| P2-05 Memory Policy | 每轮一个决策、最多一次公开读取；当前主体/公共事实分离，第三方私有接口移除 | `P2-05_MEMORY_POLICY.md` |
| P2-06 Media Adapter | direct/quoted/reply-ID/native caption 同源绑定；原始 locator 只留 transport | `P2-06_MEDIA_ADAPTER.md` |
| P2-07 Current Anchor | media→anchor→memory/history；完整 canonical digest、语言与一次 repair 同锚 | `P2-07_CURRENT_QUESTION_ANCHOR.md` |

## 关键不变量

- banned/self/known bot/plugin echo 或 ReNeBan 合同降级不会推进星汐 revision、Scene、Address、Memory、工具或发送。
- 当前 sender、owner/peer、scope、message、content digest、revision、epoch 和 trace 使用同一 `DecisionBinding`。
- 群聊 Address 不把“谈论角色”折叠成“直接叫角色”；私聊 Address 不从昵称、自称、Prompt 或历史猜测。
- 当前问题和媒体先于 LivingMemory/history 固定；他人事实、旧角色输出和引用对象不能替换当前发言者或当前问题。
- AstrBot 继续负责原生多模态下载、引用回查、caption 与 Provider transport；星汐不建立第二套默认 VLM。
- 默认中文，只有肯定式明确要求才切换英文；否定或讨论“为什么说英文”仍中文。
- 词面 coverage 只作 telemetry；自然同义表达不会因没复述关键词而被误修，只有可证明复制旧上下文才触发一次 repair。
- 长消息用完整 canonical 原文绑定，只有模型展示副本有界；`/chat`、`/role` 不再制造第二份 digest。

## 验证

各任务均保留先红后绿证据。P2-07 收口时完整套件为 `570/570`；随后接入 P2-04B 私聊生产 Address，并加入 P3-04 已完成的纯模块测试后，当前统一完整回归为：

```text
Ran 581 tests in 2.041s
OK
```

私聊 Address + resolver + production pipeline 的相关组合为 `83/83`。P2-07 核心 45 项、生产 pipeline 64 项、媒体/记忆/P8 相关 49 项均通过。阶段相关文件通过 compile/whitespace 检查；最终候选仍会在 P3～P10 结束后再次执行完整 `compileall` 与 `git diff --check`。

## 已知外部限制

`X-01` 仍成立：AstrBot 当前 hook 生命周期中，LivingMemory 被动捕获可能早于 ReNeBan 的 StarRequest stop 排队。P2 保证被拒内容不被星汐消费，但不能把它写成 LivingMemory 生态“零存储”。该问题仍只在 P10-07 提醒后、用户确认激活的 O1 中处理，不阻塞主线。

## 下一入口

进入 P3：收口 owner broad capability Action、完成 Tool Result→GroundingFact，然后原子删除 `LocalChatPlan/fast_path` 热路径，接入 Action→Content→Knowledge→Tool→Grounding→Renderer 唯一链；不保留旧版、shadow 或 fallback。
