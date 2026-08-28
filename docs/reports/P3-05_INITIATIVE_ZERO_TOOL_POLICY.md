# P3-05 主动发言零工具策略报告

## 结论

P3-05 已完成。`ambient_join` 与 `quiet_topic` 不再复用直接回复或目标用户的能力策略；即使主动接话目标是真实主人，也固定按非主人主动发言处理。

## 策略

### 主动接话 `ambient_join`

- 默认外部工具预算为 `0`。
- 不允许聊天历史检索、记忆、文件、写入、Shell、设备、Agent、媒体生成或 unknown。
- 本地 `search_memes` 只有同时满足以下条件才成为候选：
  - Meme Manager 对当前请求设置可信激活状态；
  - 模式为 tool；
  - 原请求包含完整、成对的语义提示标记；
  - 工具真实存在且来源匹配 Meme Manager；
  - `guest_allowed_tools` 精确包含 `search_memes`。
- 满足后也只有 `1` 次本地 presentation 预算，不产生外部权限。

### 安静话题 `quiet_topic`

- 外部工具预算固定为 `0`。
- 本地 presentation 预算固定为 `0`。
- Provider 调用继续显式传入 `func_tool=None`；运行前增加 policy 不变量检查。

### 未来公开事实查询

模型支持显式 `public_web_query_budget`，且强制封顶为 `1`。本层在实际主链调用时始终传 `0`，因此没有放宽生产主动发言联网行为。以后若启用，仍必须通过 P3-02/P3-03 的公共读取、精确名称和可信来源三重边界。

## 发现的 v1 差异

v1 的表情恢复辅助函数会让“主动接话目标恰好是主人”绕过群友 `search_memes` 白名单。它不会恢复 Shell 或 Agent，但仍属于不应存在的 owner 继承。P3 shadow 现在会将其记录为 `legacy_only`；生产切换留到 P8，当前不改变线上行为。

## 回归覆盖

- 主动目标为主人时仍无 owner/full/external 能力。
- 公开搜索即使在普通群友白名单中，主动接话预算为 0 时也不可用。
- 可信激活且明确配置的本地表情候选可用，预算为 1。
- owner 目标绕过群友表情配置的 v1/v2 差异可观测。
- 未来公开查询预算即使传入过大值也封顶为 1。
- quiet topic 即使误传 presentation 请求仍固定零工具。
- 既有 quiet topic Provider `func_tool=None` 回归继续通过。

## 验证

- 针对性：`107` 项通过。
- 完整回归：`262` 项运行成功，`259` 项通过，`3` 项为迁移计划中保留的 v1 expected failure。
- `git diff --check`：通过。
- 未部署、未修改第三方插件、未执行 Git/GitHub 写操作。

## 下一入口

P3-06：将工具调用结果适配为 typed tool result，明确 provenance、目标和可见性；Replyer 只读规范化结果，不得复制工具协议、调用参数或内部 envelope 到最终回复。
