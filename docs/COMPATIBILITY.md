# 兼容性说明

## 已验证基线

| 项目 | 状态 |
|---|---|
| AstrBot 4.26.7 | 已验证 |
| Python 3.12、3.13 | 自动化测试 |
| QQ aiocqhttp / NapCat | 主要测试平台 |
| 其他平台适配器 | 尚未验证，欢迎反馈 |

`metadata.yaml` 当前声明 `astrbot_version: ">=4.26.7,<5"`。AstrBot 5 的事件与 Provider API 尚未纳入兼容承诺。

## 常见插件组合

| 插件 | 建议 | 注意事项 |
|---|---|---|
| LivingMemory | 当前记忆方案 | 推荐使用 `extra_user_content` 注入；记忆只供 Planner 提炼，不覆盖当前事件身份 |
| AnySearch | 可保留 | 默认示例白名单只含普通搜索与网页提取 |
| 独立 AgentGuard | 停用或卸载 | 功能已合并；双重守卫可能导致只读搜索被提前移除 |
| AstrBot 平台分段回复 | 二选一 | 与星汐气泡同时开启可能二次拆分 |

## Provider 要求

### Planner

- 能稳定返回 JSON 最佳。
- 可以使用便宜、响应快的聊天模型。
- 留空时使用当前会话 Provider。

### Replyer

- 主回复使用 AstrBot 当前会话模型。
- 普通用户联网时，模型必须支持 AstrBot 工具调用。
- “违规重写模型”只在严重格式或 OOC 问题出现时调用。

### Embedding 与 Reranker

- 都是可选项；不配置时仍可工作。
- Embedding 失败会退回本地文字匹配。
- Reranker 失败会沿用初筛结果。
- Provider 刚添加后可能需要重载插件或重启 AstrBot，配置下拉框才会刷新。

## 推荐加载关系

星汐依赖 AstrBot 事件优先级在 LLM 请求阶段先完成权限裁决，再改写角色回复。若其他插件也在同一阶段重写 `func_tool`、`contexts`、`prompt` 或 `system_prompt`，请在反馈中列出插件名称、版本与完整脱敏日志。
