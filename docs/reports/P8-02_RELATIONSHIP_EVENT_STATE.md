# P8-02 具体事件驱动的关系状态

## 结论

P8-02 已完成 Windows 与隔离 AstrBot Linux container 候选验证，候选未部署。

星汐现在为每个 exact `scope_key + current_sender_key` 维护独立、可解释且有界的关系状态。状态只接受三类代码确认事件：canonical accepted human inbound、由当前消息重新计算的 affect trigger，以及与该 inbound 目标完全一致的 successful Shio send receipt。关系投影只进入 ReplyComposer 的 `relationship_progress` 表达上下文，不参与 Principal、主人判定、CapabilityPolicy、工具权限、动作目标或发送能力。

## 正式红灯

新增 `tests/test_relationship_state.py` 后，首轮因 `core.relationship_state` 不存在而在导入阶段失败：`ModuleNotFoundError`。这证明此前 `ConversationRuntime.InteractionProfile.affinity` 只是旧持久字段，没有 accepted-turn authority、exact send receipt、发送目标绑定、canonical render context 或生产 Composer 热路径。

四路 accepted-turn fan-out 落下后，旧测试中固定三张 ticket 的假设出现 7 failures + 2 errors；全部按固定消费者闭集机械迁移为 OWNER_ACTION、AFFECT_STATE、RELATIONSHIP_STATE、OPPORTUNITY_ATTENTION，没有加入可变调用方 fan-out。Windows 首轮完整发现另暴露 P4/P5 旧 `ticket_count=3` 断言 6 项，机械更新后全绿。

## 实现

### 1. exact accepted inbound

- `AcceptedTurnConsumer.RELATIONSHIP_STATE` 成为固定第四消费者，每轮 raw admission proof 仍只由 dispatcher claim 一次。
- `RelationshipStateBook.record_human()` 只接 exact relationship ticket，并从 `AcceptedTurnAuthority.context_for()` 取得 canonical event、Principal 和 binding；caller 不能另传 sender、scope 或 owner 身份。
- 当前消息 SHA-256 必须等于 binding 的 current content digest；关系事件的 positive/negative/neutral 分类由代码对当前消息重新运行 `appraise_affect()`，不接受模型自报或历史邻接消息冒充。
- state 以 scope + sender 双键隔离，revision 与时钟倒退 fail closed。

### 2. exact successful send receipt

- `ShioReceiptEvidence` 已从可复制的 `_seal` shape 加固为 module-owned exact issuance vault；copy、public shell、nested receipt mutation 与非 canonical receipt 全部拒绝。
- 只有全部 segment 成功、terminal、target source 为 current inbound，且 scope/session/message/sender/content/trace 与已接受 inbound 精确一致时，才记录 `successful_reply`。
- reply receipt 单次消费；重复 receipt 只返回 REJECTED，不重复累计。

### 3. 可解释关系状态与表达投影

- 闭集 band：`neutral`、`warm`、`cautious`。
- 状态只保存有界 affinity、交互/正向/负向/成功回复计数、最近 4 个 closed reason code、revision 与时间；不保存消息正文、真实 ID、路径、参数或工具结果。
- `RelationshipState` 与 `RelationshipRenderContext` 均为 module-issued exact 对象，copy、字段 mutation、nested binding mutation、跨 sender/scope/current revision 使用均 fail closed。
- ReplyComposer 只收到 `relationship_progress` 的 band、bounded affinity、reason codes 和计数。system prompt 明确它只能调整温暖、克制或谨慎的表达，绝不能改变 `is_owner`、relationship role、allowed/forbidden actions、工具或发送权限。

## 测试证据

- Relationship + AcceptedTurn：`44/44`。
- Affect + Relationship + ReplyComposer + real Pipeline：`118/118`。
- P4/P5 fan-out compatibility：`10/10`。
- Windows 完整发现：`1169/1169`，skipped 7。
- 隔离 AstrBot Linux container focused：`157/157`。
- 隔离 AstrBot Linux container 完整发现：`1169/1169`。
- `compileall`、schema JSON、merge-marker/trailing-whitespace static check：通过。

## 冻结哈希

- `core/accepted_turn_authority.py`: `BB88F8DCED65081932EE8DE64C34BEB71D34A42630E1C13A1C1CE7B4FAC6C63A`
- `core/affect_state.py`: `1FADAEEED5CBE3C553A0AC12EA21ED2E4FA4D7D614F092C38D58F32093D4E9A9`
- `core/relationship_state.py`: `06D4A9603479E62544A2AAB5AF5BB802AE5EAE603F176626297AEAB76D2A7AE4`
- `core/reply_composer.py`: `0E9078A93222863893830597E805EF40E8F4A7A86FFF953E01C960BE6C3D190A`
- `main.py`: `1D148AF700F5F4103081A753F6BE22C4D395254AD924FB26D6F8DBB2EF1DFB80`
- `tests/test_relationship_state.py`: `69B923F0216D16C432BB2F837247428A44FD1C77F4A5E9338CAF2615AFE1CEFA`
- `tests/test_pipeline.py`: `31A3A46B8A59090924000E23A18EF6DB4E67BA7C5A809B88F543E0F9405DCA1A`

其余 fan-out 与 receipt fixture 的机械迁移哈希保留在工作树，可由完整发现与本报告前述命令重新验证。

## 生产与边界

- 未修改 AstrBot core、LivingMemory、Meme Manager 或其他第三方插件。
- 未部署、未重载、未重启生产容器；线上 `main.py` 仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`，container `running=true/restart=0`，WebUI 200。
- FNOS host 与容器内 P8-02 临时 staging 已删除；Windows 本地 tar 保留在 `C:\Users\45928\AppData\Local\Temp\shio-p802-20260819.tar`，不在仓库或插件加载路径。
- 当前没有把 legacy `ConversationRuntime.feedback_evidence` 当作关系 authority；其邻接窗口与 public dataclass 不能替代 accepted current-message event。P8-03 若消费行为反馈，必须先建立来源、样本、置信度与隐私闭集。
- 未执行 Git 写操作。

## 下一入口

唯一下一入口是 **P8-03 场景—行为—结果候选**：建立带来源、Persona、scope、样本数、置信度与隐私过滤的候选学习记录；候选不得直接修改核心人格或自动生效。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P8-02。
