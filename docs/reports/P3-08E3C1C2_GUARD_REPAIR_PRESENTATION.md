# P3-08E3C1C2 Guard / Repair / Presentation / FINAL 交付闭包报告

日期：2026-08-18

状态：本地实现与冻结候选验证完成，等待独立终审结论

范围：只修改星汐本地仓库；未部署 FNOS，未修改第三方插件，未执行 Git 写操作

## 1. 本层目标与结论

本层把 C1 已签发的同一个 canonical `ReplyComposerRequest`、同一个 `PlannedAction`、同一个 `ActionOutcomeIntent` 和同一个 `ActionOutcomeAuthority` 贯穿：

```text
ContentIntent
→ ReplyComposerRequest
→ SemanticGuardContract
→ INITIAL validation
→ 可选且唯一一次 REPAIR
→ PresentationHandoff
→ SemanticValidationSeal
→ FINAL_SEND exact bytes consume
```

动作路径不再只比较 `action_id`、digest 或字段等值；每层都重新检查 exact object identity、canonical vault authority、完整 nested snapshot、当前 plan 和 Composer consumer。普通 chat、AnySearch Evidence 路径继续显式使用 `action_outcome=None`、`action_outcome_authority=None`，没有被动作状态模板污染。

最终效果：

- Evidence 与 ActionOutcome 冲突、跨 request/plan/authority、copy/replace/rebuild、nested mutation 和 presentation 字节替换均 blocking，零 repair；
- 自然语言动作状态矛盾只在 INITIAL 获得一次受限 repair；REPAIR 再漂移或 FINAL_SEND 漂移直接阻断；
- `confirmation_required`、拒绝、取消、过时、成功、无副作用失败、部分失败、超时和效果未知均由同一代码级语义矩阵解释；
- 多气泡动作回复的第一个状态气泡必须独立自足，后续气泡仍全量扫描矛盾；
- 合法 `EXECUTE_ACTION` outcome presentation 被允许；只有 DENIED/CANCELLED 的合法 continuation `REPLY` 可携带 outcome；普通 `REPLY` 可无 outcome，`USE_TOOL` 不得携带 outcome；
- 同一个 exact request 跨所有 `SemanticGuardController` 实例最多签一张 final seal，并且只能 terminal consume 一次。

## 2. 根因

此前 P3-07 的 Guard/Validator/Repair/Presentation 只对普通 typed reply 做语义和媒体检查，尚未取得 C1 ActionOutcome authority。若直接用公开 dataclass、字段摘要或 caller 提供的 report/context 继续接线，会留下以下旁路：

1. report severity、context fragments、Composer result bubbles/visible text 可替换；
2. 同 request 可重复 INITIAL、重复签 repair permit 或跨 Controller 重复签 final seal；
3. presentation 可由 public/private raw mint 重建，最终文本可 append/truncate/reorder；
4. partial/unknown/timeout 可被说成确定成功或确定无效果；
5. 多气泡聚合后才完整会让首气泡在后续发送失败时暴露错误确定态；
6. quote、question、条件句和肯定回指若只靠拆句关键词，会同时产生误拦与漏拦。

因此本层没有再加一个末端字符串过滤器，而是闭合 canonical authority、完整验证 ticket、一次性 repair permit、presentation snapshot 和最终交付 ledger。

## 3. Exact identity 与 canonical authority

### 3.1 SemanticGuardContract

`SemanticGuardContract` 必填并 exact 绑定：

- `composer_request`
- `planned_action`
- `content_intent`
- `current_question_anchor`
- `media_context`
- `current_message`
- `evidence_outcome`
- `action_outcome`
- `action_outcome_authority`

构造和每次 Guard 验证都会调用 Composer canonical inspector。存在 outcome 时，再调用 `authority.inspect_for_composer(outcome, plan, consumer=request)`；Evidence 与 Outcome 互斥。closure vault 自行登记 canonical contract，没有 raw register/candidate/state API。

### 3.2 OutputValidationContext 与 validation ticket

`OutputValidationContext` 改为 module-owned self-mint：public builder 不再接受 caller 提供的 `grounding_facts`、`forbidden_fact_fragments`、`context_reference_fragments`。三组事实只从 exact `composer_request.assembled_context`、`content_seed.intent` 和 exact contract 内部派生，并完整快照。

validator 在 INITIAL/REPAIR 每个阶段重新验证：

- exact request/contract/context/outcome/authority/plan；
- `raw_output` 的确定性重新解析结果与完整 `ReplyComposerResult` snapshot；
- target、sender、owner、anchor、grounding、other-subject facts、context reference、关系和协议守卫；
-完整 report snapshot，包括每项 `(code, severity, detail)`、AnchorCoverage 与 SemanticGuardReport。

只有 closure 内部完整运行 validator 后才能签 validation ticket；private issuer 不接受 caller report 或 pass bit。同一 request 每阶段只允许一张 ticket，同 request 只能有一份 canonical context。

### 3.3 RepairPermit

repair authority 以 exact Composer request/contract 为一次性键，而不是 public report 对象：

- 只有 canonical INITIAL、非 blocking、repairable report 可签一次；
- permit 绑定 exact rejected visible digest、request、contract、outcome 与 authority；
- REPAIR validation 会消费该 permit，失败或 GC 均不会为同一 request 重开额度；
- repair prompt 只公开闭集 `operation/kind/attempted/has_output`，固定中文约束；不重新 claim、不调用工具、不带 receipt、正文、路径、参数或计数。

### 3.4 Presentation 与 FINAL seal

`PresentationHandoff` 由 module closure 自行 mint，builder 与 private mint 使用同一完整 typed 校验。handoff 封存：

- exact request、contract、result、validation ticket、outcome、authority；
- exact `final_visible_text`；
- exact `final_segments` tuple；
-文本 digest 与包含气泡边界/顺序的 segment digest。

FINAL_SEND 必须重新得到完全相同的 segment tuple 和完整 visible bytes；增删、替换、重排、重拆、append、truncate、copy、rebuild 和 seal replay 均失败关闭。module-owned seal ledger 以 exact request 为 lifetime tombstone，跨 Controller 并发只能 issue 一次、consume 一次；第一次错误 consume 同样 terminal，不能用第二次尝试撞过验证。

## 4. 统一动作状态语义矩阵

`core/action_outcome.py` 是 Composer 与 Guard 共同使用的唯一状态语义源。矩阵按 started/result/effect/control/stale-timeout/output 命题格闭合，不在 Composer 与 Guard 各维护一份漂移映射。

| kind | 必须表达 | 关键禁止 |
|---|---|---|
| `confirmation_required` | 等确认、未开始 | attempted/success/failure/partial/timeout/unknown |
| `denied` | 代码拒绝、未执行 | 编造执行、成功或失败尝试 |
| `cancelled` | 已取消、未执行 | 已提交、已完成或已尝试 |
| `stale_not_started` | 已过时、未开始 | attempted/result/effect |
| `succeeded_read` | 只读成功 | 未开始、失败、写入/提交、任何 output body |
| `succeeded_committed` | 成功且确实提交 | 未开始、失败、未提交、无效果、部分、未知 |
| `failed_no_effect` | 已尝试、失败、确认无副作用 | 成功、未开始、提交、部分、未知 |
| `failed_partial` | 失败且可能部分效果 | 完整成功、未开始、确定无效果、确定提交 |
| `timed_out` | 已尝试、超时、效果未知 | 确定成功/失败/提交/无效果、未开始 |
| `effect_unknown` | 已尝试、效果未知 | 确定成功/失败/提交/无效果、未开始 |
| `stale_no_effect` | 已尝试、过时、确认无效果 | success/committed/partial/unknown/not-started |
| `stale_not_committed` | 已尝试、过时、确认未提交 | committed/success/not-started |
| `stale_committed` | 已尝试、过时、确认已提交 | not-committed/no-effect/partial/unknown/not-started |
| `stale_partial` | 已尝试、过时、部分效果 | complete success/no-effect/not-started |
| `stale_effect_unknown` | 已尝试、过时、效果未知 | definite success/failure/effect/not-started |

全部 15 kind 的 `has_output=False` 统一禁止具体正文、绝对路径、参数和计数；read/search operation 额外禁止写入或 committed 声称。

语义 lexer 先保护平衡引号与问句范围，再按 quote 外标点/转折切局部命题；未闭合引号失败关闭。条件/疑问/引用本身不算事实断言，但其后的肯定回指不能洗掉命题。引用/问句 pending 使用 `AFFIRM / DENY / SUSPEND / NONE` 四值分类；同一句最多允许两个中性 discourse clause 的 bounded DEFER，跨句、预算耗尽、明确否定、不确定、纯引用或新直接状态命题都会清除 pending。

## 5. 多气泡与 repair 规则

- required 状态必须在第一个 status bubble 内完整成立，不能依赖第二个气泡补充“可能部分生效/效果未知”；
- 所有气泡继续扫描 forbidden proposition；后续气泡声称成功仍会阻断；
- INITIAL 的自然状态矛盾可 repair 一次；身份、binding、copy、substitution、nested mutation、跨 request/plan/outcome 均 blocking、零 repair；
- repair 后只允许 REPAIR phase 的新 canonical pass ticket 进入 Presentation，旧 INITIAL result/report 不可混用；
- repair 不触发 action replay，失败动作不会再次执行。

## 6. 机械迁移（无语义扩权）

以下接线是现有 typed 链的机械迁移，未新增 owner action 执行、工具调用、模型调用、配置或发送分支：

1. `main.py`
   - 现有 `SemanticGuardContract` 显式传入 exact `composer_request` 与当前 request 上的 outcome/authority；普通 chat 为 `None`；
   - 现有 validation context 改用 self-derived facts，trace 计数读取 `validation_context.grounding_facts`；
   - 现有 Presentation builder 显式传 exact request/contract/outcome/authority；final seal consume 使用 actual segments 与 bytes；
   - stage-state 只增加 exact identity 断言，没有接通 production owner adapter。
2. `tests/test_pipeline.py`
   - fixture 不再事后 `replace(composer_request, reply_shape=...)`；在 production builder 前 patch `choose_reply_shape`，一次 mint canonical request；
   -攻击测试改为在更早 constructor/inspector 边界断言拒绝且零发送，未弱化断言。
3. `tests/test_p8_offline_acceptance.py`、`tests/test_p8_scenario_matrix.py`
   - 现有单 fixture 显式传 exact request，并对普通 chat 显式传 `outcome=None/authority=None`；不改场景语义。
4. SemanticGuard/Validator/Repair/Presentation 旧 fixture
   - 全部改走 production canonical builder/validator；没有保留 optional default、raw report、raw context 或兼容 constructor。

## 7. 审计驱动的红灯与根修

| 红灯 | 根修 |
|---|---|
| report severity 可从 BLOCKING 改成 REPAIRABLE | 完整 issue/report snapshot；篡改后 inspector 与 permit 均拒绝 |
| caller 注入假 grounding/context facts | context self-mint，三组 fragments 从 exact typed source 内部派生 |
| public `ReplyComposerResult` split-brain | validator 确定性重解析 raw 并核对完整 result snapshot |
| private validator/presentation/repair mint 可绕 public 语义 | closure 内自算 report/payload/values，private path 使用相同完整 typed 参数和校验 |
| 同 request 多 context、多 INITIAL、多 repair、多 seal | request-lifetime tombstone、phase ledger、one-shot permit、module-global seal ledger |
| partial/unknown/timeout 被“处理好了”等同义词确定化 | 中央 proposition lattice 与自然完成式命题 |
| `has_output=False` 泄漏第一行、路径、offset、计数 | 15-kind 统一 OUTPUT_BODY forbidden |
| 引号/问句被逗号先拆坏 | 单遍 quote-aware lexer，保留 question/boundary scope，unclosed quote fail-closed |
| 引用/问句后的“是的/当然/结论成立”等肯定回指洗掉假成功 | 四值 reference classifier + bounded same-sentence DEFER；否定/悬置反例不恢复 |
| 正常 GC 被当作 vault corruption | 不可再安全使用的 weak record 安全丢弃；live exact inspection 继续 fail-closed；request tombstone 只保留必要一次性语义 |

## 8. 验证证据

使用 Codex bundled Python，在仓库父目录运行。

### 8.1 本层 focused

```text
python -m pytest -q \
  astrbot_plugin_shio/tests/test_semantic_guard.py \
  astrbot_plugin_shio/tests/test_output_validator_v2.py \
  astrbot_plugin_shio/tests/test_repair_controller.py \
  astrbot_plugin_shio/tests/test_presentation_handoff.py \
  astrbot_plugin_shio/tests/test_action_outcome_delivery_pipeline.py

98 passed, 268 subtests passed
```

覆盖 15 kind 正/负矩阵、互斥命题格、quote/condition/question/reference、first-bubble、B10、raw/copy/mutation/cross-object、one-shot repair、跨 Controller 并发 seal、segment tamper、GC 后新 turn。

### 8.2 typed delivery + main/P8

```text
上述 focused + test_pipeline.py + 两个 P8 fixture
186 passed, 289 subtests passed
```

### 8.3 既有门

```text
C0 ActionOutcome                         33/33
C1 Content/Composer                      64/64
E3C1A sealed completion/output authority 127/127
P3-07 related                            138/138（39 subtests）
```

### 8.4 全仓与静态检查

```text
python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py' -q
Ran 969 tests
OK

python -m compileall -q astrbot_plugin_shio
OK

git diff --check
OK
```

## 9. 修改文件

核心：

- `core/action_outcome.py`
- `core/reply_composer.py`
- `core/semantic_guard.py`
- `core/output_validator_v2.py`
- `core/repair_controller.py`
- `core/presentation_handoff.py`

机械接线：

- `main.py`

测试：

- `tests/test_semantic_guard.py`
- `tests/test_output_validator_v2.py`
- `tests/test_repair_controller.py`
- `tests/test_presentation_handoff.py`
- `tests/test_action_outcome_delivery.py`
- `tests/test_action_outcome_delivery_pipeline.py`
- `tests/test_pipeline.py`
- `tests/test_p8_offline_acceptance.py`
- `tests/test_p8_scenario_matrix.py`

文档：

- `docs/reports/P3-08E3C1C2_GUARD_REPAIR_PRESENTATION.md`
- `SHIO_MASTER_PLAN.md`

## 10. 明确未覆盖与 E4 债

1. `ActionOutcomeAuthority` 当前仍会强持 action request/source graph；完成一条 outcome chain 后，C2 的 semantic contract、validation 与 seal tombstone 可能随 request lifetime 保留。本层只证明正常 report/repair/presentation/seal GC 不会造成 vault corruption 或跨 turn DoS，**不宣称 action request 已释放全部槽位**。
2. 上述 active material 与最小 durable tombstone 的分离、terminal delivery ack 后 route/material/secret 清理、crash-in-progress 写动作转 UNKNOWN 且不自动重试，属于 P3-08E4。
3. `has_output=True` 仍 hard-off；live same-handle output authority 未在本层开启。
4. `main.py` 只接现有 typed reply 的 exact identity 字段；没有新增 production owner-action adapter 分支。E5 main/config 与 E6 容器/FNOS 仍未执行。
5. 本层动作状态自然语言 evaluator 只处理高置信中文局部命题；不把任意长隐喻或低置信语用猜测当安全 authority。
6. 未部署 FNOS，未验证线上 AstrBot/插件真实运行；本报告只证明本地候选。

## 11. 下一唯一入口

独立终审 blocker=0 后进入 **P3-08E4 生命周期与持久化**：将 active material 与最小 tombstone 分离，建立 delivery-ack/reclaim，清理 route/material/secret 强引用，并对 mutating crash-in-progress 产生 UNKNOWN、禁止自动重试。E4 完成前不进入 E5 main/config，不部署 FNOS。
