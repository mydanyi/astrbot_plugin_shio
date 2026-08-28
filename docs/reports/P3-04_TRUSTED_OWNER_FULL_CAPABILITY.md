# P3-04 可信主人完整能力报告

## 结论

P3-04 已完成。v2 shadow 只有在当前直接回复的 `PrincipalContext` 同时满足可信 AstrBot sender ID、配置 owner allowlist 命中、owner 关系和完整 sender key 时，才生成主人完整能力策略。

## 实现

- 新增 `build_owner_capability_policy()`。
- 已验证主人在直接回复中可使用所有活动能力：公共读取、聊天检索、表达、媒体生成、记忆写入、工件读写、Shell、设备控制、完整 Agent 和未分类插件工具。
- owner full 不受普通群友的精确名称配置限制；它仍只作用于 AstrBot 当前提供的活动工具，不凭空安装或创建工具。
- `is_owner=True` 若来自文本、未知来源、错误关系、缺失 sender key 或非直接回复模式，会生成 degraded owner policy，外部能力全部失败关闭。
- shadow 对比使用主人原请求中的工具名，不改变现有主人请求对象和 ToolSet。

## 身份边界

以下内容均不是主人权限来源：

- 昵称或群名片写“主人”；
- 消息中自称“我是主人”；
- 引用主人的历史消息；
- Prompt、LivingMemory 或相邻用户上下文中的身份文字；
- 主动接话运行时恰好选中主人作为目标。

身份只由结构化 sender ID 与配置 owner allowlist 裁决。

## 回归覆盖

- 可信主人可使用所有能力和 unknown 工具。
- 伪造 owner flag 但验证来源不可信时拒绝。
- 主人在 ambient 模式也不能继承 full policy。
- 管线 shadow 保留主人原请求对象并达到 v1/v2 授权一致。
- 普通群友文本自称主人仍得到 guest policy 和 `owner=false`。
- 既有昵称、自称、引用和恢复路径身份矩阵继续通过。

## 验证

- 针对性：`116` 项通过。
- 完整回归：`256` 项运行成功，`253` 项通过，`3` 项为迁移计划中保留的 v1 expected failure。
- `git diff --check`：通过。
- 未部署、未修改第三方插件、未执行 Git/GitHub 写操作。

## 剩余风险与下一入口

- ambient/quiet 当前生产代码已有禁工具分支，但 v2 policy 尚未统一表达该模式，也尚未覆盖“受控公开事实”预算。
- 下一入口：P3-05，为主动接话和安静话题建立独立 policy；默认零工具，且永不继承目标用户的 owner 权限。
