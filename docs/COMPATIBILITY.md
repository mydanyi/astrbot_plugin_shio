# 兼容性说明

## 已验证基线

| 项目 | 状态 |
|---|---|
| AstrBot 4.26.7、4.27.2 | 已验证 |
| Python 3.12、3.13 | 自动化测试 |
| QQ aiocqhttp / NapCat | 主要测试平台 |
| 其他平台适配器 | 尚未验证，欢迎反馈 |

`metadata.yaml` 当前声明 `astrbot_version: ">=4.26.7,<5"`。AstrBot 5 的事件与 Provider API 尚未纳入兼容承诺。

## 常见插件组合

| 插件 | 建议 | 注意事项 |
|---|---|---|
| LivingMemory | 当前记忆方案 | 推荐使用 `extra_user_content` 注入；记忆只供 Planner 提炼，不覆盖当前事件身份 |
| AnySearch | 可保留 | 默认示例白名单只含普通搜索与网页提取 |
| Meme Manager 4.15.1 | 可保留 | 语义 Tool 模式须把 `search_memes` 加入星汐普通用户只读白名单；星汐会把它作为本地表达工具单独保留 |
| 独立 AgentGuard | 停用或卸载 | 功能已合并；双重守卫可能导致只读搜索被提前移除 |
| AstrBot 平台分段回复 | 二选一 | 与星汐气泡同时开启可能二次拆分 |
| AstrBot 内置主动回复 | 建议关闭 | 与星汐自然接话同时开启会形成两套独立概率与冷却，可能重复回复 |
| 其他主动聊天插件 | 先分群测试 | 如果也会监听全部群消息或主动发送，需避免同群重复接话 |

## Provider 要求

### Planner

- 能稳定返回 JSON 最佳。
- 可以使用便宜、响应快的聊天模型。
- 留空时使用当前会话 Provider。

### Replyer

- 主回复使用 AstrBot 当前会话模型。
- 普通用户联网时，模型必须支持 AstrBot 工具调用。
- DeepSeek V4 偶发把 DSML 工具协议写入正文时，星汐会先无工具重写，仍失败则阻断；不会把协议文本当作可执行工具调用。
- Meme Manager 调用被模型写成 `<search_memes ... />`、`search_memes(...)` 或 `search_memes{...}` 多行伪调用时，星汐会在气泡拆分前整体移除调用并保留同轮正常台词；本轮其他真实工具名也使用同一结构化守卫。
- llama.cpp／Gemma 若把 `channel`／`message`／`start`／`end` 隐藏模板标记写入正文，星汐会清理历史轮次、尝试无工具重写，并在发送前再次闭锁。
- “违规重写模型”只在严重格式或 OOC 问题出现时调用。
- 完全静默主动话题使用“违规重写模型”配置的 Provider；留空时使用该群当前会话 Provider，并且不提供工具。

### Embedding 与 Reranker

- 都是可选项；不配置时仍可工作。
- Embedding 失败会退回本地文字匹配。
- Reranker 失败会沿用初筛结果。
- Provider 刚添加后可能需要重载插件或重启 AstrBot，配置下拉框才会刷新。

## 推荐加载关系

星汐依赖 AstrBot 事件优先级在 LLM 请求阶段先完成权限裁决，再改写角色回复。对 Meme Manager 的语义 Tool 模式，星汐只识别该插件本轮写入的激活状态、完整语义提示词标记和精确工具名 `search_memes`；缺少任一条件都不会自行恢复工具。若其他插件也在同一阶段重写 `func_tool`、`contexts`、`prompt` 或 `system_prompt`，请在反馈中列出插件名称、版本与完整脱敏日志。

未点名自然接话使用 AstrBot `CustomFilter` 单独唤醒星汐处理器；它不会把 `is_at_or_wake_command` 置为真，因此不会顺带触发默认 LLM。完全静默主动话题依赖 `Context.send_message`，目前 QQ 官方 API 明确不支持该接口；该平台只能使用由新消息触发的自然接话。
