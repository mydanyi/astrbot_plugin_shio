# 兼容性说明（0.5.23）

## 已验证基线

| 项目 | 状态 |
|---|---|
| AstrBot 4.26.7、4.27.2 | 已验证；生产主门为 4.27.2 |
| Python 3.12、3.13 | 自动化覆盖；隔离容器为 3.12 |
| QQ aiocqhttp / NapCat | 主要验证平台 |
| 其他平台适配器 / AstrBot 5 | 尚未承诺 |

`metadata.yaml` 声明 `astrbot_version: ">=4.26.7,<5"`。当前 P10 自动化超过 1,200+ 项，但其他平台仍需真实 event/send 适配验证。

## 常见插件组合

| 插件 | 当前建议 | 0.5.0 边界 |
|---|---|---|
| LivingMemory 2.5.7 | 可保留 | Shio 只消费带 source/scope/subject 的资料；legacy owner-private scope 不满足 memory-write adapter；`X-01` 仍存在 |
| AnySearch v0.3 | 可保留 | 默认只开放 `anysearch_search`、`anysearch_extract`；exact tool/runtime/schema 与当前轮 evidence 缺一即降级 |
| 官方 Meme Manager 4.15.1 | 配对必需 | 普通／自然回复使用其既有 `on_llm_response`、`on_decorating_result`、`after_message_sent` 钩子；主动／冷场由生成模型从 Manager 当前资源包的 22 个官方类别描述中给出一个隐藏类别，Shio 校验并剥离后只调用公开 `compat_prepare_message` 与 `compat_send_prepared_message`。Manager 继续独占概率、资源、构建和发送。Shio 校验版本、唯一运行实例、公开异步方法签名与绑定，不要求整文件 hash 相同，不自造 semantic compat，也不部署定制 Manager |
| ReNeBan v1.2.0 | 可保留 | exact hook conformance 才能形成 verified gate；missing/error/interface drift 时 Shio fail closed |
| Parser v1.5.1 | 可保留 | 其自动输出不能回灌成人类／角色历史；接口漂移显式 degraded |
| Group Verification / Recall Cancel / Keywords Reply | 现场共存观察 | 非 Shio authority；自动／管理输出仍按来源合同 drop 或隔离 |
| 独立 AgentGuard | 停用或卸载 | 双重改写 `func_tool` 会破坏 Shio exact policy |
| AstrBot 平台分段回复 | 二选一 | 与 Shio exact segments 同时开启会二次拆分 |

P10-03 固定了 LivingMemory、AnySearch、Meme Manager、ReNeBan、Parser/gates 的 present/missing/error/timeout/interface-change 矩阵，以及 direct、quote、quoted image、仅图片和 same-media repair。

## Provider 与工具

- 普通被点名回复与群聊自然接话始终使用 AstrBot 当前会话 Provider；`replyer_provider_id` 只指定冷场主动续题和首稿被拦后的单次 repair，留空时这两阶段也使用当前会话 Provider。
- 未点名自然接话会在最终回复前增加至多一次 participation 语义调用。该调用使用原生文本 contexts 与纯 current body，`image_urls=[]`、`audio_urls=[]`、`func_tool=None`、无 tool result、无重试／repair／第二 Provider；Provider 必须返回五键严格 JSON，旧的自由文本或 reasoning-only 输出会失败关闭。
- 需要检索时 Provider 必须支持 AstrBot tool call；模型文本声称“已搜索”不能替代 sealed tool result。
- 工具结果必须绑定 exact scope、sender、target、generation 和 broker permit；迟到或跨轮结果丢弃。
- 最终回复若含 DSML、Meme XML/Python/JSON、裸参数、hidden channel 或工具模板，只允许一次无工具 repair，仍失败则不发送。
- direct/quoted/image-only media 使用 AstrBot 原生 transport；URL/base64 不进入 typed prompt 或日志。

## 参与、主动发起与 owner action

- 自然称名和未点名参与都进入同一 typed admission/target/guard/send 链，不创建旧回复器。明确称名／Reply／@ 星汐不消耗 participation 调用；明确 Reply／@ 其他真实成员也不调用且不抢话。
- 未点名候选在语义前仍受功能、allowlist、可信上下文、continuity、capacity、cooldown、窗口和 backoff 约束。语义 `WAIT` 不消费 cadence 状态，`NO_ACTION` 只产生有界 backoff；迟到结果不会取消较新的回复或进入发送链。
- 主动发起已经实现，但 `proactive_initiation_enabled` 默认关闭且空 allowlist 永不触发；不要与 AstrBot 内置主动回复同时开启。
- 主动类别 compatibility 是 category transport，不是 Manager 的逐图 caption/tag/vector 语义检索；二者必须在日志和验收中分开。缺少、重复、未知或泄漏到正文后的类别标记会触发唯一一次同上下文 repair，仍失败则不发送该轮主动内容。
- 四个 owner action 适配器保持默认关闭。现场 runtime allowlist 为空、LivingMemory 为 legacy scope，Shell 永久硬关闭，因此 0.5.0 不承诺真实 owner action 执行。

## 外部限制

`X-01`：LivingMemory 被动捕获可能早于 ReNeBan/Shio admission。Shio 能保证自身 ledger/Scene/Affect/Learning/Tool/Reply 零消费和零提交，不能保证 LivingMemory 零存储。可选 O1 尚未执行，且只有 P10 完成后经用户确认才允许更新第三方并验证上游方案。
