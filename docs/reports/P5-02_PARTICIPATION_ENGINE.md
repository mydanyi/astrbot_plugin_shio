# P5-02 Participation Engine 报告

## 1. 结论

P5-02 已完成 Windows 与隔离 AstrBot Linux container 门，候选未部署。

未点名群消息现在先在 ingress 热路径获得 exact `OpportunityAttention`，再由独立 `ParticipationAuthority` 综合自我相关、Persona 兴趣、可信关系、GroupScene 节奏、打断成本、回应价值和近期存在感，签发 canonical `ParticipationAssessment` 与 typed `ParticipationDecision`。只有 `MAY_JOIN` 会把当前事件提升进既有 typed 回复链；P5-02 不直接生成文本、不调用额外模型，也不提前实现冷却、React 或生成取消。

## 2. 根因与红灯

P5-01 只完成了 Attention 分流，`CANDIDATE` 仍固定 `NO_ACTION`。如果把 Participation 留在 `on_llm_request` 中计算，未被 AstrBot 唤醒的普通群消息不会进入该钩子，所谓自然参与实际上不可达。

正式红灯以缺少 `core.participation_engine` 的 `ModuleNotFoundError` 固定。新增测试预先锁定：

- `DIRECT_SELF` 永远 `MUST_REPLY`；
- `ABOUT_SELF` 可进入 `MAY_JOIN`；
- `OPEN_GROUP` 必须命中当前 Persona 的显式兴趣并通过价值/打断成本门；
- `OTHER_PERSON` 与 `UNCERTAIN` 默认 `NO_ACTION`；
- 同一消息在亚托莉与中性 Persona 下产生不同 Participation，但身份、权限和场景代码不变；
- assessment copy、跨 authority、外层/嵌套字段篡改、scene 篡改全部 fail closed；
- `MAY_JOIN` 只提升当前轮并进入 typed `REPLY`，工具集保持空；
- known bot 等拒绝来源不产生 Participation 或唤醒。

## 3. 实现

### 3.1 Persona-owned interests

`PersonaPackage` 新增 `participation_interests`，元素为 `ParticipationInterest(id, keywords, weight)`。加载器和 validator 校验 ID、非空关键词、等价重复、长度与 `(0, 1]` 浮点权重。

兴趣只描述角色愿意参与的公共话题，不授予工具、身份或副作用权限。通用引擎没有硬编码“食物”“机器人”或特定角色名：

- 亚托莉资产声明共同用餐、机器人/AI、海边与日常；
- 苏澄资产声明清楚解释与克制幽默；
- 中性 Persona 显式为空，因此不会仅因开放问题主动插话。

### 3.2 Canonical GroupScene

`GroupSceneBook.inspect_current_human_scene()` 新增 code-owned 当前场景检查：

- exact `ConversationEvent` identity；
- exact 当前 `GroupSceneSnapshot` identity；
- topic、participant、personal fact 的完整 typed integrity snapshot；
- scope/session/message/sender/content/trace/reference/revision 与当前事件一致。

公开 frozen scene 即使通过 `object.__setattr__` 修改，也不能继续成为 Participation 依据。后续新消息推进 scene 后，旧 assessment 会安全失效；P5-05 再补显式生成取消与重规划。

### 3.3 Participation authority

`ParticipationAuthority` 只消费 exact canonical `OpportunityAttentionDecision`，通过 `OpportunityAttentionAuthority.context_for()` 取得同一 accepted turn context，不重新相信 caller 提供的 raw Address、Principal 或 Binding。

评估输入与结果如下：

| 输入 | 代码拥有的投影 |
|---|---|
| self relevance | DIRECT=1.0，ABOUT_SELF=0.95，OPEN_GROUP=0.2，其余 0 |
| Persona interest | 当前 Persona 显式关键词的最大权重 |
| relationship | exact Principal role 映射到 Persona relationship warmth |
| GroupScene rhythm | bounded public-topic density |
| recent presence | 当前 sender 的 Shio reply / human turn 比率 |
| interruption cost | address 基线 + rhythm + recent presence |
| response value | self relevance / Persona interest / relationship 的闭合组合 |

决策保持闭集：

- REQUIRED / DIRECT → `MUST_REPLY`；
- WAIT / OTHER_PERSON / UNCERTAIN → `NO_ACTION`；
- ABOUT_SELF → 只有回应价值明显高于打断成本时 `MAY_JOIN`；
- OPEN_GROUP → 还必须命中权重至少 0.65 的 Persona 兴趣。

`cooldown_remaining_s` 在本层固定为 0；去抖、频率、冷却和 no-action 退避留给 P5-03。

Assessment 由 weak exact ledger 登记，保存 opportunity、Persona participation inputs、scene 与 attention/participation nested snapshot。copy、跨 runtime、重复签发、任意外层或嵌套 mutation 均不能通过 `inspect`；repr/trace 不显示消息、关键词、sender、scope 或 Persona 原文。

### 3.4 热路径与零工具

Participation 在 `admit_ingress_event()` 内、场景和 Attention 均 canonical 后立即签发。仅 `ParticipationLevel.MAY_JOIN` 才设置当前事件 wake 标志；这一步不发消息、不调用 Provider、不打开工具。

`build_persona_reply()` 不再现场重算 Participation，而是 exact inspect ingress 保存的 assessment，并直接使用其中的 `AttentionDecision` / `ParticipationDecision`。对 `MAY_JOIN`，KnowledgeGap 固定为 `NONE`（`opportunity_join_zero_tool`），因此 Planner 只能得到普通 typed `REPLY`，不会进入 AnySearch acquisition；最终 Persona Renderer 原有工具集也继续为空。

## 4. 验证

- 正式红：`core.participation_engine` 缺失，targeted import error；
- P5-02 targeted：`5/5`；
- P5 Attention + Participation：`9/9`；
- GroupScene / Planner / Capability / Persona / P4 / P5 / Pipeline related：`174/174`；
- Windows full：`1090/1090`，skipped 7；
- 隔离 AstrBot Python 3.12 container full：`1090/1090`；
- `py_compile`、`compileall`、Persona JSON、merge marker 与 trailing whitespace：通过。

完整回归先暴露两个旧阶段断言：Address 测试仍要求 ABOUT_SELF 不提升，P3 测试仍要求所有非 DIRECT 都 `NO_ACTION`。两处只按 P5-02 新合同迁移预期，未增加旁路；之后 full 全绿。

隔离候选仅复制到 container `/tmp/shio-p502-root` 运行，完成后远端 host/container staging 与远端 tar 已删除。生产未写入、未重启；AstrBot running=true、restart=0、WebUI 200，live `main.py` SHA256 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`。本地复用 staging archive `C:\Users\45928\AppData\Local\Temp\shio-p404-candidate.tar` 仍保留，供后续阶段重建。

## 5. 变更范围

- `core/persona.py`
- `core/group_scene.py`
- `core/opportunity_attention.py`
- `core/participation_engine.py`（新增）
- `assets/personas/atri.json`
- `assets/personas/su_cheng.json`
- `assets/personas/neutral_minimal.json`
- `main.py`
- `tests/test_persona.py`
- `tests/test_p5_participation_engine.py`（新增）
- `tests/test_p5_attention_gate.py`
- `tests/test_address_pipeline.py`
- `tests/test_p3_atomic_hot_path.py`

无 Git 写操作、无 FNOS 生产写入、无 AstrBot 重启。

## 6. 未完成边界与恢复入口

P5-02 只完成“单轮是否参与”的安全判定。当前 recent presence 已进入分数，但尚无跨轮 cooldown、连续抢话抑制、no-action 退避或持久频率预算。

下一唯一入口是 **P5-03 去抖、频率、冷却和 no-action 退避**：必须在 Participation Authority 后建立 per-scope / per-sender bounded temporal state，防止逐句抢话；DIRECT 继续不受自然参与冷却影响，OTHER_PERSON/UNCERTAIN 不得借冷却状态升级。P5-03 不提前实现 React-only 或生成取消。中断后从本段恢复，不重做 P5-02。只有 P10 综合生产验收时才请用户统一测试效果。
