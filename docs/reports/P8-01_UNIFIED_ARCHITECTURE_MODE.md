# P8-01 统一架构 v2 模式报告

## 结论

P8-01 已完成。新增统一 `architecture_v2_mode=off|shadow|enabled` 决策层，类型化身份、上下文、计划、ReplyComposer 和 OutputValidator 不再各自读取零散开关。当前生产激活门仍关闭，因此误设 `enabled` 也只会安全保持 shadow，不会形成 v1/v2 混合发送。

## 模式语义

- `off`：不计算 typed 比较链；继续使用现有 v1。
- `shadow`：只在本地计算 typed identity/context/plan 和脱敏指标；不激活 ReplyComposer/OutputValidator，不增加最终生成或发送。
- `enabled`：表示请求完整 typed 链；只有调用方显式声明完整发布门已就绪时才真正激活。当前 `main.py` 固定未就绪，所以有效模式保持 shadow，等待 P8-08 指定范围发布。

统一决策同时固化最终生成预算 1、发送预算 1。invalid mode 失败关闭为 off。

## 旧配置兼容

保留 `typed_context_v2_mode` 作为只读兼容字段。考虑 AstrBot 可能把新字段默认值 `off` 自动写入旧配置：旧字段仍为 `shadow` 时继续 shadow，避免升级后悄悄丢失既有验证状态；当新旧字段都为 off 时才关闭。

`docs/CONFIG_AUDIT.md` 已同步为 88 个字段，并明确旧开关的移除方向。

## 无双发证明

新增集成回归覆盖统一 `shadow` 和“请求 enabled、有效 shadow”两种情况：

- 既有 v1 Planner 仅调用 1 次；
- typed shadow 全部为确定性本地计算；
- 没有额外 Provider 调用；
- 没有独立 `event.send`；
- 事件记录 requested/effective mode、配置来源以及单次生成/发送预算。

## 验证

- P8-01 定向：8 项通过；
- 完整回归：485 项运行成功，482 项通过，3 项为迁移计划中保留的 v1 expected failure；
- `git diff --check`：通过；
- 未部署，未执行 Git/GitHub 写操作。

## 下一入口

P8-02：把 P0 脱敏失败案例固化为统一离线验收集，验证目标、身份、事实主体、协议、语言、人格边界、发送和并发；运行完整测试并给出每类样例的通过证据。
