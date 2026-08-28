# P8-05 学习防投毒

## 结论

P8-05 已完成 Windows 与隔离 AstrBot Linux container 候选验证，未部署生产；P8 整阶段至此完成。

旧链允许同一 reviewer 跨多轮反复累计样本，低置信旁观者反馈也能在达到数量阈值后生成 candidate；历史 `expression_feedback/group_expression_feedback` 还会绕过 P8-04 审核直接影响表达分数。现在所有行为影响只能来自 exact candidate 经显式审核后启用的 adjustment。Candidate 形成必须同时满足：code-owned exact feedback evidence、目标本人高置信反馈、安全闭集短反馈、每 cluster 至少 3 个不同 reviewer，以及安装私钥 HMAC 完整性。

## 正式红灯

新增 `tests/test_p8_learning_poisoning.py` 后，首轮因 `core.learning_poison_guard` 不存在而以 `ModuleNotFoundError` 失败。红测固定了以下漏洞：

- 同一 reviewer 20 次重复反馈不得形成 candidate；
- 旁观者/低置信反馈不得进入行为聚类；
- Prompt 注入、脏话、密码、手机号和具体正文样式不得进入学习；
- raw/copy/cross-guard/replay admission 必须失败关闭；
- reviewer 身份不得落盘，重启后去重仍有效；
- public cluster mutation、mapping substitution 和磁盘样本篡改不得铸造 eligible cluster；
- 未审核的历史 feedback map 不得继续直接改变表达分数。

## 实现

### 1. exact feedback 与安全文本门

- `FeedbackEvidence` 改为 module-owned exact object；public constructor、copy、字段 mutation 和伪造对象不能通过 inspection。
- 新增 `LearningPoisoningGuard` 与 exact one-shot `LearningFeedbackAdmission`。
- Admission 只接受 `HIGH + is_target_user + affects_target_profile` 的 target follow-up/reply/reaction evidence。
- 学习文本只接受与 evidence signal 一致的闭集短反馈；任意附加指令、自由正文、控制字符、长文本、脏话或私密/凭据样式整体拒绝。
- Admission 不保存 feedback 原文；repr/trace 不显示 reviewer、文本、安装 secret 或 applicability 值。

### 2. 多主体去重与候选门

- reviewer key 仅通过本安装 32-byte secret 的 HMAC 指纹进入聚类；原始 reviewer/sender/reply/message/trace 值从不进入 cluster、artifact、candidate 或日志。
- 每个 `(persona, situation, relationship, behavior)` cluster 对同一 reviewer 指纹终身只计一次，最多保留 64 个 opaque 指纹。
- eligible cluster 必须至少 3 个不同 reviewer，且全部样本为 high-confidence，low-confidence 必须为 0。
- raw `observe()`/`observe_feedback()` 永久关闭；只有 exact one-shot admission 能更新 cluster，copy/cross-store/replay 均失败关闭。
- Artifact source schema 从 `behavior_outcomes.v1` 升为 `behavior_outcomes.v2`；旧低置信或无 reviewer-diversity provenance artifact 不能加载为新 candidate。

### 3. 持久化完整性与重启

- cluster persistence 升为 schema v2，并以安装 secret 对 canonical rows 签 HMAC。
- JSON 重复键、错误 primitive 类型、reviewer token 形状错误、计数/权重不一致、MAC 不符或磁盘内容篡改均整份失败关闭。
- ConversationRuntime 启动后立即从已验签的 eligible clusters 重建 artifact store，因此旧/伪造 artifact 文件不能单独保活 candidate。
- 重启后使用同一 secret，可继续识别已有 reviewer HMAC；同一 reviewer 再反馈不会增加 sample/reviewer count。

### 4. 审核是唯一生效路径

- `expression_feedback` 与 `group_expression_feedback` 继续保留为观测/迁移数据，但不再直接进入 `expression_feedback_scores()`。
- Runtime 计算行为调整时只遍历 exact `LearningCandidateAuthority` candidate，并通过同一 `LearningReviewAuthority.adjustments_for()` 取得 effective adjustment。
- 因此 default-disabled、shadow 或任何未审核旧反馈的有效行为增量均为精确 0；只有 P8-04 显式 enable 且完整 lineage 未损坏时才产生有界效果。

## 测试证据

- Feedback + cluster + poisoning + ConversationRuntime + P8 candidate/review/activation/artifact focused：`52/52`。
- Windows 完整发现：`1189/1189`，skipped 7。
- 隔离 AstrBot Linux container focused：`52/52`。
- 隔离 AstrBot Linux container 完整发现：`1189/1189`。
- `compileall`、merge-marker、trailing-whitespace、限定 `git diff --check`：通过。
- 全仓 raw `behavior_learning.observe()/observe_feedback()` production caller：0。

## 冻结哈希

- `core/feedback.py`: `74741BA50034DD10F6A0D3EE92727256F9A5B0AE2B5CB85A6A098C73C2E8DA6D`
- `core/learning_cluster.py`: `F4E1C15F750DB3EBC1F7E4C26D0C92B9B8BA7BF15DA6416059AA953328F77072`
- `core/learning_poison_guard.py`: `240BCFC12FD0C1C6BD536F8B35F51B2F2E08B6B08A6D5C0114AA1B56F2DCD322`
- `core/learning_artifact.py`: `1F22C0043B90BABE22F3FDF20805401633C5ED567C44633BB3DB47DB579A80C9`
- `core/conversation_runtime.py`: `B08BE59295A56EA222F9FF664968382E40D4640857AFE06E421979F8DBF860D7`
- `tests/test_learning_cluster.py`: `5F798DD30AC2DD6C6B19CC0F22071C59412F5B74095794F103AC653338439F5C`
- `tests/test_p8_learning_poisoning.py`: `8B5AF1BDACDAC1402874DC5E4A69E2C0357E40B51B11E4F0BB6FFA3E124CA716`
- `tests/test_p8_learning_candidates.py`: `869D28F10E52279AAD3B8985FF9ECF65AD1DB33BE592422FF0C112DCC4BB62C5`
- `tests/test_learning_artifact.py`: `ADA7D33E874E1227C18E83EA0710A2B65DD0593275FB22250396C835665062A8`

## 生产与边界

- 未修改 AstrBot core、LivingMemory、Meme Manager 或其他第三方插件。
- 未部署、未重载、未重启生产容器；线上 `main.py` 仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`，container `running=true/restart=0`，WebUI 200。
- FNOS host 与容器内 P8-05 staging 已删除；Windows 本地候选包保留在 `C:\Users\45928\AppData\Local\Temp\shio-p805-20260819.tar`，不在仓库或插件加载路径。
- P8 完成不等于生产部署完成；P9 并发/预算/延迟/重启恢复与 P10 综合发布门仍未执行。
- 未执行 Git 写操作。

## 下一入口

唯一下一入口是 **P9-01 会话内串行、会话间受控并行**：先建立 same-scope ordering、cross-scope mutable-principal isolation、并发取消/任务上限和异常释放的失败矩阵，再接真实 direct/react/proactive/action 运行路径。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P8；只有 P10 综合生产验收完成后才请用户统一测试效果。
