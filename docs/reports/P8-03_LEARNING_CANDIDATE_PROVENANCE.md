# P8-03 场景—行为—结果候选

## 结论

P8-03 已完成 Windows 与隔离 AstrBot Linux container 候选验证，候选未部署。

旧 `BehaviorOutcomeCluster → LearnedBehaviorArtifact` 脚手架现在上收为 module-owned exact `LearningCandidate`。候选完整保留聚合来源、opaque Persona、situation、relationship scope、behavior、样本构成、结果权重、置信度与生成时间，并永久标记为 `candidate/aggregated_only`。生成候选不会改 Persona 包、Prompt、CapabilityPolicy 或表达分数；运行时注册的 activation 仍默认 `disabled`，有效调整为精确 0。

## 正式红灯

新增 `tests/test_p8_learning_candidates.py` 后，首轮因 `core.learning_candidate` 不存在而导入失败：`ModuleNotFoundError`。这证明旧 artifact 虽有字段，却仍是公开可构造 dataclass，缺少 store-owned exact provenance、copy/cross-authority/replay 边界和独立 candidate-only authority。

## 实现

### 1. 完整 provenance

`LearningCandidate` 固定包含：

- source kind `aggregated_feedback_cluster` 与 source schema `behavior_outcomes.v1`；
- privacy class `aggregated_only`；
- candidate-only status；
- opaque `persona-<16 hex>`；
- structured situation、relationship scope 与 behavior ID；
- sample/high/low confidence counts；
- positive/negative weight、score、confidence、generated time。

不包含原始消息、完整回复、用户/群/消息 ID、reply ID、trace ID、Persona 名称、voice card、路径、URL、token 或自由说明。

### 2. exact candidate authority

- `LearnedBehaviorArtifactStore` 为 refresh/load 后的每个 candidate artifact 保存 exact object + complete snapshot；同字段 copy、字典替换和字段 mutation 不能冒充 store-owned source。
- `LearningCandidateAuthority.issue_for_store()` 对同一 exact store 只返回同一 authority；public constructor、copy、cross-store/cross-authority 输入不能签 candidate。
- candidate 由 closure vault 自行构造，caller 不能提交 candidate、snapshot 或 arbitrary Mapping 进行注册。
- 重复 refresh 对未变化的 exact artifact 返回同一个 candidate；candidate copy、status mutation 或 source 漂移均 fail closed。

### 3. 隐私与默认不生效

- Persona key 必须是不可逆短指纹；situation/behavior 只接受有界结构化 token；空格原话、绝对路径、URL 和真实名称形状在 artifact validation 阶段被排除。
- trace/repr 只显示来源类别、privacy/status、计数、score/confidence 与布尔适用域，不显示任何 persona/situation/relationship/behavior 值。
- `ConversationRuntime.flush()` 在聚类/artifact 刷新后只生成 exact candidate；`learning_candidates()` 供 P8-04 审核层读取。
- candidate authority 没有 `enable` API。现有 activation 注册仍是 `disabled`；测试确认 candidate 生成后实际 expression score 为 0。

## 测试证据

- P8-03 + artifact + cluster + activation + ConversationRuntime：`30/30`。
- Windows 完整发现：`1175/1175`，skipped 7。
- 隔离 AstrBot Linux container focused：`30/30`。
- 隔离 AstrBot Linux container 完整发现：`1175/1175`。
- `compileall`、merge-marker/trailing-whitespace static check：通过。

## 冻结哈希

- `core/learning_artifact.py`: `DF37CB40339D53D396463D8D6C24FA37E91638683AA80792813EC1649A0E2E2A`
- `core/learning_candidate.py`: `401214EF6D3AA4AD6EE9DE6FA22CA78A05CDD996A49F4216265AC66BF3205E28`
- `core/conversation_runtime.py`: `962DD5A79BA27BCA4CB6982CC612C545855366516AC2ACFC85384C68A06BE41D`
- `tests/test_p8_learning_candidates.py`: `8CC83587B968D052035D8AADC7C6149ED1D78B7C1FF9FF7285C118B4E95F5572`

## 生产与边界

- 未修改 AstrBot core、LivingMemory、Meme Manager 或其他第三方插件。
- 未部署、未重载、未重启生产容器；线上 `main.py` 仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`，container `running=true/restart=0`，WebUI 200。
- FNOS host 与容器内 P8-03 临时 staging 已删除；Windows 本地 tar 保留在 `C:\Users\45928\AppData\Local\Temp\shio-p803-20260819.tar`，不在仓库或插件加载路径。
- 本层不宣称旧邻接文本 feedback 已达到 P8-05 防投毒标准；它只能形成 disabled candidate，不能自动改变行为。
- 未执行 Git 写操作。

## 下一入口

唯一下一入口是 **P8-04 审核、灰度、启用、撤销**：以 exact candidate 为唯一输入建立显式审核 decision、shadow 观察、有限启用和即时撤销；默认不自动改变核心人格。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P8-03。
