# P3-08E5 Main All-off Wiring

## 1. 结论

P3-08E5 已把 owner-action authority 图接入 `main.py` 的真实 typed 热路径，但没有启用任何生产动作适配器：

- plugin 初始化时创建唯一的 `AcceptedTurnAuthority`、`OwnerActionRouter`、`PlannedActionAuthority`、`OwnerActionController` 与 `ActionOutcomeAuthority`；lifecycle store 与 durable finalizer 按首次主人动作惰性创建，并在 terminate 时关闭。
- 每个 accepted turn 立即 fan-out 固定的 OWNER_ACTION/AFFECT_STATE ticket；Affect 当前未接生产，因此由 main 明确 `SKIPPED`，未匹配 owner action 的 OWNER ticket 同样 typed finalize，不留下普通聊天 ledger 债。
- exact current-message owner proposal 优先进入 `EXECUTE_ACTION`，与 AnySearch/`GroundingFact` 分支互斥。
- 总开关和四个 adapter 开关默认全关；配置在 plugin 初始化时冻结。Shell 即使 UI flag 为 true 仍代码级 hard-disabled。
- production runtime collector allowlist 仍为空。因此 UI 手动打开 master/read 标志也只生成 canonical `DENIED/NOT_STARTED` outcome，不会取得工具、编译 AdapterDraft 或调用 executor。
- 只有 exact `PresentationHandoff.final_segments` 全部在实际 send terminal 成功后，main 才签 actual-send evidence、写 durable ACK、消费 Controller/Outcome 两张票并清敏感 lineage。

本层没有改 FNOS，没有启用 LivingMemory 写入、artifact read/grep 或 Shell，也没有执行 Git 写操作。

## 2. 配置与权限边界

`_conf_schema.json` 新增 owner-action 总开关、四个逐 adapter 开关、artifact root/path flavor 与 shell family。默认值全部失败关闭。

`owner_ids` 只表示可信主人资格，不再描述为完整 Agent 或 Shell 权限。README、架构与隐私文档同步修正：主人身份不是 execution authority；工具和动作仍需 exact typed authority、运行时一致性与回执链。

配置对象在 plugin 初始化后被修改，不会改变已冻结的 `owner_action_enabled` 或 `AdapterConfig`。

## 3. 热路径

### 3.1 Accepted turn 生命周期

Ingress canonical accept 后立即 dispatch。main 使用 exact OWNER ticket 路由当前正文，并明确终结未使用的 AFFECT ticket。地址、场景或 typed gate 后续失败时，已匹配的 owner route 也会以 typed no-op 终结。

Ingress accepted decision 的 binding 改为 exact canonical `ConversationEvent.binding` identity，避免用字段相等副本破坏 ticket→plan→controller provenance。

### 3.2 All-off owner action

当前消息精确命中 read/grep/memory/shell proposal 后，Planner 生成 canonical `EXECUTE_ACTION`。main 在动作执行分支之前检查冻结配置和 production conformance：

- master off：`owner_action_disabled`；
- 单 adapter off：`owner_action_adapter_disabled`；
- UI 开启但 production allowlist 为空：`owner_action_runtime_conformance_unavailable`。

三条路径都由 Controller 发布 exact denial，再投影 `ActionOutcomeIntent`；不调用工具、AdapterDraft、executor，也不把 outcome 塞进 Grounding。

### 3.3 展示与真实发送

`EXECUTE_ACTION` 使用 C2 同一 Composer/Guard/Validator/Presentation 链，Provider 与 repair 阶段工具预算均为零。动作 presentation 使用预先封印的 exact segment tuple；首气泡发送失败会清空 AstrBot result chain，禁止宿主随后把未被 send ledger 追踪的聚合文本自动发送。

自动发送只在最后一个 segment 的成功 observation 后进入 E4D durable finalization。发送、Provider、repair 或展示失败不会伪造 ACK，也不会重试动作；当前 all-off 路径无外部副作用。失败轮的 exact terminal graph 保持 fail-closed，由已有容量上限和重启恢复约束，后续 E6 继续验证真实宿主 hook。

## 4. 关键 API

- `main.py:396` `_build_owner_action_adapter_config`
- `main.py:1667` `_bind_accepted_turn_authority`
- `main.py:2028` `_prepare_disabled_owner_action_outcome`
- `main.py:4154` `_finalize_owner_action_delivery`
- `main.py:4229` `confirm_automatic_send_observation`
- `main.py:4291` `terminate`
- `core/runtime_invariant.py:133` `EXECUTE_ACTION` zero-tool budget

## 5. 测试证据

- Ingress + pipeline focused：`95/95`。
- owner-action/runtime/ingress/pipeline related：`263/263`。
- Windows full discover：`1055/1055`，skipped `7`。
- WSL full discover：`1055/1055`，无 skip。
- `compileall`：通过。
- repository `git diff --check`：通过。

正式回归覆盖：

- schema all-off、启动时配置冻结与 Shell hard-off；
- 300 个普通主人 turn 的双 ticket 终结与 bounded accepted-turn ledger；
- exact read proposal 走 `EXECUTE_ACTION` canonical denial，Provider/tool 调用均为零；
- UI master/read/shell 打开仍不能绕过空 production allowlist；
- single/multi-bubble actual send 后 durable ACK 与 Controller/Outcome 清理；
- 首 segment send failure 零 ACK、零未追踪 AstrBot fallback；
- accepted event 与 decision 使用 exact canonical binding identity。

## 6. 冻结哈希

- `main.py` `3D457E3643E4A33059478C8BD6481DCFF451F9431E8920F5088E9EB998F627F0`
- `_conf_schema.json` `AA93A22C1C4BAF86964E6A3AEB3011BA3DE2F9D51D407DEC25E6442DD136A6DB`
- `core/ingress_admission.py` `2DBB251183657ECEC53625FF3DFA2F8B4A53EF2C78F12E1399E9895E63540A77`
- `core/runtime_invariant.py` `476F0D20E4C7EDF8A1CD2A66941819684A5796BFF375C14E5ED6294C5A814714`
- `tests/test_pipeline.py` `BE6AC54AE05750750720EC64D4197359B9E960CACE0D1525FAE9DE08B08A0F13`
- `tests/test_ingress_admission.py` `D561F5FBCB63E4ED25C4DFFE4E0D47A59E4F976D19D5AA1E67F6B47AA1B5DEFF`

## 7. 下一入口

下一唯一入口是 P3-08E6：

1. 在隔离 Linux 容器中运行完整测试与包结构检查；
2. 对 FNOS 做只读版本、源哈希、配置、挂载与 live hook 检查；
3. 备份线上星汐插件及配置，仅部署星汐；
4. 核对部署哈希、AstrBot 加载、WebUI schema、初始化和新日志；
5. 以普通聊天、只读搜索和 owner-action all-off denial 做在线回归。

live same-handle output authority、LivingMemory private/current-owner scope 与 AstrBot audited build fingerprint 仍未过门，四个 adapter 必须继续关闭。
