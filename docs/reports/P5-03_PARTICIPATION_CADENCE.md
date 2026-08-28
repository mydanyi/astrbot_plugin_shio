# P5-03 Participation Cadence 报告

## 1. 结论

P5-03 已完成 Windows 与隔离 AstrBot Linux container 门，候选未部署。

P5-02 的单轮 `MAY_JOIN` 现在必须再经过 runtime-local `ParticipationCadenceAuthority`。同一 scope/sender 的自然参与受最短间隔、滑动窗口频率和连续 no-action 退避约束；被抑制的候选输出 typed `WAIT`，不会提升事件、不会生成 prompt、不会调用模型或工具。`DIRECT_SELF / MUST_REPLY` 始终绕过自然参与冷却。

## 2. 红灯与边界

正式红灯以缺少 `core.participation_cadence` 的 `ModuleNotFoundError` 固定，测试预先覆盖：

- 第一次兴趣命中可 `MAY_JOIN`；
- 冷却内的第二次候选为 `WAIT`；
- 冷却后可再次参与；
- 5 分钟滑窗最多选择 2 次；
- 窗口过期后恢复；
- ordinary no-action 建立短退避，候选在退避期内为 `WAIT`；
- 同期 DIRECT 仍 `MUST_REPLY`；
- WAIT 热路径为零 prompt、零模型、零工具；
- cadence copy、跨 runtime、nested decision mutation 全部失权；
- temporal subject state 保持最多 256 项，trace 不含 sender、scope 或正文。

P5-03 不统计真实发送成功率；它按“已选择自然参与”保守占用频率预算。这样生成或发送失败不会导致紧接着连续重试抢话。P5-04 才实现 React-only，P5-05 才实现生成中断与重规划。

## 3. 实现

### 3.1 Canonical cadence decision

新增 `ParticipationCadenceDecision` 与 `ParticipationCadenceAuthority`：

- 每个 exact `ParticipationAuthority` 只允许一个 cadence authority；
- 输入必须是 exact canonical `ParticipationAssessment`；
- 同一 assessment 只能签发一次；
- outer/nested 完整 snapshot、copy/cross-runtime/replay/mutation fail closed；
- weak decision ledger 与 closure-owned temporal state 不向调用方公开 key 或历史时间；
- repr/trace 只显示闭集 level、计数、布尔值和 cooldown 秒数。

### 3.2 节奏规则

当前 code-owned 默认值：

| 规则 | 默认值 |
|---|---:|
| 同 sender 自然参与最短间隔 | 45 秒 |
| 频率滑窗 | 300 秒 |
| 每个滑窗最多自然参与 | 2 次 |
| no-action 退避基数 | 2 秒 |
| no-action 最大退避 | 30 秒 |
| temporal subject 上限 | 256 |

连续 `NO_ACTION` 采用有上限的指数退避；下一条本来可 `MAY_JOIN` 的候选若仍在退避、最短间隔或滑窗限制内，则改为 typed `WAIT` 并给出剩余秒数。候选通过后清零 no-action streak。时钟倒退对自然候选 fail closed 为 WAIT；DIRECT 仍不被压制。

subject ledger 到上限后不会驱逐既有频率记录让攻击者绕过冷却；新 subject 安全降为 WAIT。状态只影响相同 scope/sender，不能把其他人的节奏或 owner 关系借给当前发送者。

### 3.3 热路径

`main.py` 在 P5-02 assessment 签发后立即签发 cadence decision，并将两者都放入当前 event。只有 cadence 后仍为 `MAY_JOIN` 才提升 wake 标志。

模型请求钩子必须同时 exact inspect assessment 与 cadence，并验证 cadence 的 base/binding 正是当前轮；Planner 使用 cadence 的最终 `ParticipationDecision`。`WAIT` 走现有 terminal action 路径：清空 prompt、contexts、media 与 tools，停止当前事件，不进入 Persona Renderer。

## 4. 验证

- 正式红：`core.participation_cadence` 缺失，targeted import error；
- P5-03 targeted：`5/5`；
- P5-01/02/03 合并：`14/14`；
- Windows full：`1095/1095`，skipped 7；
- 隔离 AstrBot Python 3.12 container full：`1095/1095`；
- `py_compile`、`compileall`、merge marker 与 trailing whitespace：通过。

隔离候选只复制到 container `/tmp/shio-p503-root`；测试后远端 host/container staging 与远端 tar 全部删除。生产未写入、未重启；AstrBot running=true、restart=0、WebUI 200，live `main.py` SHA256 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`。

## 5. 变更范围

- `core/participation_cadence.py`（新增）
- `main.py`
- `tests/test_p5_participation_cadence.py`（新增）

无 Git 写操作、无 FNOS 生产写入、无 AstrBot 重启。

## 6. 恢复入口

下一唯一入口是 **P5-04 React-only**：只消费 exact cadence decision；为轻量候选建立 typed `REACT` 与 ExpressionIntent，不得把公开模型建议、Meme 查询结果或 raw event 当作 action authority。严肃、DIRECT、工具/主人动作、OTHER_PERSON、UNCERTAIN 和冷却 WAIT 均不得被降级或升级成 React。P5-04 仍不实现生成取消；P5-05 单独处理新消息中断与重规划。中断后从本段恢复，不重做 P5-03；只有 P10 才请用户统一测试效果。
