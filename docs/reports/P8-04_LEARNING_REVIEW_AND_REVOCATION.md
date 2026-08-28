# P8-04 学习审核、灰度、启用与撤销

## 结论

P8-04 已完成 Windows 与隔离 AstrBot Linux container 候选验证，未部署生产。

学习项现在只能从 `LearningCandidateAuthority` 签发的 exact aggregated candidate 进入 `LearningReviewAuthority`。默认状态固定为 `disabled`；`shadow` 只计算 proposed adjustment、不改变实际表达；`enable` 的绝对权重上限固定为 `0.15`；`revoke` 立即把 proposed/effective adjustment 同时归零。候选的 `candidate` 状态、Persona 包、Prompt、身份、权限和 Action policy 均不会被审核链改写。

## 正式红灯

新增 `tests/test_p8_learning_review.py` 后，首轮因 `core.learning_review` 不存在而以 `ModuleNotFoundError` 失败。这证明 P8-03 只有 candidate-only provenance，尚无显式审核 authority、shadow/enable/revoke 决策边界和撤销入口。

随后关闭旧 `LearningActivationStore.set_state(raw_artifact_id, ...)` 时，旧 activation 单元测试按预期暴露裸 ID 调用；这些夹具已迁移为 exact artifact store → exact candidate authority → exact review authority，未保留兼容旁路。

## 实现

### 1. exact candidate-only 审核 authority

- 新增 `LearningReviewKind.SHADOW/ENABLE/REVOKE` 与 module-owned `LearningReviewAuthority`。
- 同一 exact candidate authority + activation store 只签发同一个审核 authority；public constructor、copy、cross-runtime authority 均不能通过 registry inspection。
- `review()` 必须接 exact candidate、exact closed kind、CAS revision、结构化 reason、有限时间和可选 cap。
- activation 的内部更新 seam 也必须重验同一 exact review authority，不能只拿 candidate authority 绕过审核层。
- `ConversationRuntime` 只初始化长生命周期审核 authority，不自动调用审核或启用任何 candidate。

### 2. 默认关闭、shadow、有限启用和即时撤销

- 新 candidate 的持久化 activation 默认 `disabled`，proposed/effective 都是精确 `0.0`。
- `shadow` 才计算有界 proposed adjustment，但 effective 固定为 `0.0`。
- `enable` 才令 effective 等于 proposed，绝对值硬封顶 `0.15`；旧 `0.25` 上限已收紧。
- `revoke` 使用同一 revision CAS 立即转回 `disabled`，两种 adjustment 同时归零。
- 因当前 activation 文件没有 code-owned 签名，`enabled` 不跨重启恢复：磁盘中的 enabled row 会失败关闭，重新注册为默认 disabled；shadow/disabled 可恢复。这避免修改本地 JSON 绕过显式审核。

### 3. activation 完整性与持久化边界

- 移除实例上可同时替换的 `_activation_snapshots` 字典。
- activation exact object、完整字段快照和 store identity 由 closure vault 保存；public mapping copy/substitution、字段 mutation、跨 store 输入均失败关闭。
- flush 必须先逐项通过 vault inspection；载入只接受 exact JSON primitive 类型、结构化 reason、有限数值和 `0..0.15` cap。
- Candidate 仍固定为 `candidate/aggregated_only`，审核不会把 candidate 本体改写成 enabled 状态。

## 测试证据

- P8-04 + activation + candidate + artifact + ConversationRuntime focused：`31/31`。
- Windows 完整发现：`1181/1181`，skipped 7。
- 隔离 AstrBot Linux container focused：`31/31`。
- 隔离 AstrBot Linux container 完整发现：`1181/1181`。
- `compileall`、merge-marker、trailing-whitespace、限定 `git diff --check`：通过。
- 负面矩阵覆盖：裸 artifact ID、缺失/复制 review authority、candidate copy、stale revision、activation 字段 mutation、mapping substitution、restart-enabled fail-closed。

## 冻结哈希

- `core/learning_activation.py`: `824CBE1FB822BA06C3C31A38F721A9898D8990E3B45A0E53CBECF3509ABC0C75`
- `core/learning_review.py`: `35FCBCD2B11A14D3DFC1069E5F9FD499C520A3EF75D8B03FD978A671D38C4EF1`
- `core/conversation_runtime.py`: `C5140AC436C237BDE90C692C41C2E783B5E6B71C0FF0F51D617963437138E701`
- `tests/test_learning_activation.py`: `B8EF3D5863342C036E088646171EF6BD260E076DAA3DE8A97683636FF62F9021`
- `tests/test_p8_learning_review.py`: `80D49F4299C807FC5234290E4D1134F4375446DCB5FB4DB1E23F80ED6AAA1105`

## 生产与边界

- 未修改 AstrBot core、LivingMemory、Meme Manager 或其他第三方插件。
- 未部署、未重载、未重启生产容器；线上 `main.py` 仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`，container `running=true/restart=0`，WebUI 200。
- FNOS host 与容器内 P8-04 staging 已删除；Windows 本地候选包保留在 `C:\Users\45928\AppData\Local\Temp\shio-p804-20260819-v2.tar`，不在仓库或插件加载路径。
- P8-04 只证明审核和撤销边界；尚未证明上游反馈聚合不会被高频群友、恶意 Prompt、脏话或私密信息投毒，该问题由 P8-05 处理。
- 未执行 Git 写操作。

## 下一入口

唯一下一入口是 **P8-05 防投毒**：先建立 poisoning threat matrix 和失败测试，再让高频单一主体、恶意 Prompt、脏话、私密/标识信息和低置信群体信号无法主导聚类、candidate 或审核输入。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P8-04；只有 P10 综合生产验收完成后才请用户统一测试效果。
