# P8-03 本地组合场景矩阵报告

## 结论

P8-03 已完成。新增 `tests/fixtures/p8_scenario_matrix.json` 和本地组合验收，按真实 v2 组件顺序串起：统一模式 → 可信身份 → 能力策略 → ReplyTarget → 本地快路径/Planner 路由 → ReplyComposer 请求与解析 → OutputValidator → 表情语义 handoff → segment 发送回执。

本层不调用真实模型、不调用真实工具、不发送群消息；检查的是结构、授权和预算能否闭合。

## 场景结果

| 场景 | 结果 | 关键约束 |
| --- | --- | --- |
| 主人群聊、多气泡 | 通过 | owner/primary bond；简单聊天 0 Planner + 1 Composer；2 段均绑定当前目标 |
| 群友群聊、单气泡 | 通过 | guest/peer；不获得主人关系；1 次模型预算 |
| 群友私聊、多气泡 | 通过 | private peer，不因私聊升级成主人；2 段发送记录完整 |
| 群友联网查资料 | 通过 | `PUBLIC_WEB_READ` 允许，`SHELL_EXEC` 拒绝；small planner + Composer 共 2 次模型预算；外部工具最多 2 次 |
| 主人私聊调试 | 通过 | 真实 sender ID 命中 owner allowlist 后保留完整 Agent/Shell 能力 |
| 表情候选 | 通过 | 只允许可信 `LOCAL_PRESENTATION`；最终文本通过 validator 后最多 1 次本地候选调用，不算第二次生成 |
| 主人引用群友旧消息 | 通过 | 当前 owner principal 与引用 guest 分离；目标歧义进入有界 small planner，共 2 次模型预算 |
| 单/多气泡回执 | 通过 | 每个 segment 显式 attempted/succeeded，内部 reply 仍只有一个发送事务 |

## 角色与通用能力边界

组合测试使用当前 ATRI 人格包验证真实首个实现，但身份、能力、目标、路由、Validator、发送回执和模式控制均来自通用 core；测试没有把亚托莉台词写入权限判断或目标选择。

## 验证

- P8-03 组合矩阵：6 项测试、7 类组合场景全部通过；
- 完整回归：499 项运行成功，496 项通过，3 项为明确保留到 P8-09 的 v1 expected failure；
- `git diff --check`：通过；
- 未连接生产 Provider、未调用工具、未部署、未执行 Git/GitHub 写操作。

## 下一入口

P8-04：在任何部署动作前，只读核对 FNOS 容器、AstrBot WebUI、线上插件版本和文件哈希。该步骤只采集脱敏健康证据，不上传、不重载、不修改线上配置；通过后才允许进入 P8-05 备份。
