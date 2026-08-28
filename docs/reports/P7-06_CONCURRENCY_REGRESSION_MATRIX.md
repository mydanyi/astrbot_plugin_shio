# P7-06 组合并发回归矩阵

## 结论

P7 的并发与过时回复保护已经通过组合链路验证：快速连续三位用户、慢 Provider、迟到工具结果、引用旧消息和发送中途出现新消息时，旧 generation 不会进入工具解析、气泡或发送；当前发送者、被引用者和主人身份保持分离；成功发送的每个气泡只有一条唯一 segment 记录。

本层没有靠增加 Prompt 修复并发问题，验证对象是强类型 target/principal/reference、session epoch、取消能力、最终发送边界和 send ledger。

## 新增组合回归

### 1. 迟到工具结果 + 用户切换

流程：

1. 群友 A 发起需要联网的旧问题，Planner 为其开放只读搜索工具；
2. 群友 B 的新问题推进同 session epoch；
3. A 的工具结果和模型答案随后才返回；
4. Guard 在解析 tool result 前发现旧 epoch。

结果：A 的 completion 清空，没有创建 typed tool result，也没有 `tool_result` 阶段；trace 终态为 `stale_drop`。迟到的资料不能被复制到 B 的回复或重新激活 A 的旧答案。

### 2. 当前主人引用旧群友消息

当前发送者 B 是可信 owner，引用对象是群友 A 的旧消息。实际主链确认：

- `PrincipalContext` 仍是 B/owner；
- `ReplyTarget` 是 B 当前的新 message；
- `ReferenceContext` 是 A 的旧 message；
- target sender key 与 reference sender key 不相等；
- Planner target 仍绑定 B。

引用谁不再等于“当前是谁”，也不会让被引用者获得当前消息权限。

### 3. 三位用户快速连续提问 + 多气泡发送

A、B、C 的三个 event 连续推进 epoch，然后依次尝试进入最终发送边界：

- A、B 的 result chain 被清空，均无任何发送；
- C 只发送自己的三个气泡；
- 两个手动气泡和一个自动气泡各有唯一 segment ID；
- 三个 event 的 trace ID 互不相同；
- 指标终态依次为 `stale_drop`、`stale_drop`、`sent`。

## 既有并发回归一并复核

定向套件同时执行：

- cancel-safe Planner 在新消息到来时被星汐安全取消；
- 未声明可取消能力的 Provider 可完成调用，但旧结果随后被丢弃；
- 新消息在气泡发送中途到来时，后续旧气泡停止；
- 不同 scope 不互相取消；
- direct target、quoted reference 和 provenance memory 保持分离；
- 延迟 assistant 历史不能按相邻顺序归给下一位用户。

并发、generation、context 与 P2 回归定向集共 38 项通过。

## 验证

完整测试：478 项执行，475 通过，3 项既有 expected failure。`git diff --check` 通过。

本次完整 discover 曾发现一个测试基础设施问题：把 `test_pipeline` 作为包再次导入会使 unittest 以两个模块名重复加载同一套 AstrBot stub，造成测试全局状态污染。已将组合回归并入原 pipeline test 模块并删除重复导入文件；随后完整 discover 恢复稳定通过。该问题只存在于测试装载方式，不是生产插件行为。

## 已确认边界

- AstrBot 通用发送接口仍没有平台 message ID 回执，不能验证“平台实际展示的每条消息 ID”；当前保证的是星汐内部 segment 的唯一性与发送回调边界。
- 已经发送完的历史回复在更晚时间收到平台 reaction 的长期并存窗口属于反馈存储策略，不属于 generation 并发；P6 当前只保留每 scope 最近一次观察。
- 本层没有部署，没有修改 AstrBot、LivingMemory、Meme Manager 或 Provider，没有 Git 写操作。

## 下一层

P8-01 将把已完成的 v2 身份、上下文、计划、守卫和指标统一到一个 `architecture_v2_mode=off|shadow|enabled` 总开关。shadow 只计算和比较 typed 结果，不生成或发送第二份回复，避免现有多个独立 feature flag 漂移。
