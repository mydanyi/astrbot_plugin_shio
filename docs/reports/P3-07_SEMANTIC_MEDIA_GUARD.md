# P3-07 语义与媒体守卫

> 2026-08-18 E3C 复审补充：最终展示字节闭合见
> `P3-08E3C0A_EXACT_PRESENTATION_BYTES.md`。FINAL_SEND 现在还必须与已签发
> `PresentationHandoff` 的完整正文摘要逐字一致，发送前清理或同义改写不能复用旧 seal。

## 当前结论

P3-07 的本地实现和自测已经收口，正在等待独立安全审计终审。生产 typed 热路径现在只接受一份不可变 `SemanticGuardContract`：它从初稿校验贯穿唯一一次 repair 到发送前终检，不再把可选字符串 `semantic_requirements` 或其他 legacy 字段当成第二套语义权威。

本层只处理代码能够高置信证明的两类问题：

1. `PlannedAction`、`ContentIntent`、`CurrentQuestionAnchor`、`MediaContext`、`EvidenceOutcome`、当前正文或回复目标发生结构错绑；
2. 回复出现明确竞争断言，例如把“不要启动旧服务”说成“现在启动旧服务”、把 Docker 明确答成 Python、把“总结日志”说成“已经删除日志”、否认实际已绑定的媒体、为不可用媒体编造画面、改写已验证数值，或把失败取证说成已经查证成功。

仅仅没有逐字命中、自然同义改写、代词、省略、情绪、口癖或普通风格差异，不会触发 repair。

## 单一 typed 合同与代码拥有的阶段凭证

`core/semantic_guard.py` 新增并闭合：

- `SemanticGuardContract`：绑定最终 `PlannedAction`、`ContentIntent`、同一个 `CurrentQuestionAnchor`、同一个 `MediaContext`、可选 `EvidenceOutcome` 和当前正文；正文、真实 ID 不进入 repr/trace。
- `SemanticGuardPhase`：`INITIAL`、`REPAIR`、`FINAL_SEND`。
- `SemanticGuardReport`：只保存可见文本 SHA-256、闭集 issue、阶段、计数和布尔 telemetry。
- `SemanticGuardController` 与 `SemanticValidationSeal`：只有通过 `INITIAL` 或 `REPAIR` 的代码路径能登记凭证；凭证按对象身份绑定原合同、原 `PresentationHandoff` 和已校验摘要，只能在 `FINAL_SEND` 成功消费一次。复制合同、重建 presentation、伪造或重放 seal 均失败。
- `validate_semantic_media_guard()`：先校验 typed 结构，再检测高置信显式矛盾。

以下结构错误全部立即 blocking，repair 预算为 0：

- PlannedAction、Intent、Anchor、Media 或 Evidence 不同 binding；
- ReplyTarget 与 binding 不一致；
- 当前正文 SHA-256 与 binding 不一致；
- Intent 没有原样保持 Anchor atoms 或 answer language；
- Intent、Anchor 与 MediaContext 的 media item IDs 或顺序不一致；
- Evidence action ID 不属于当前 PlannedAction；
- ContentIntent 有 GroundingFact 却没有 EvidenceOutcome；
- accepted Evidence 的完整 GroundingFact tuple 与 ContentIntent 不一致；
- failed Evidence 却携带 GroundingFact。

空可见回复被明确归类为 repairable，可获得唯一一次 repair；任何被标为 blocking 的 issue 都不会被 controller 特判放行。

## 高置信语义规则与误杀边界

| 类别 | repairable 的明确证据 | 有意放行 |
|---|---|---|
| 否定 | 当前轮明确禁止同一动作，同一局部命题中却有第一人称执行或明确完成证据 | “不要启动”→“先别运行”；“现在启动可能冲突，所以我不会动”等后果说明 |
| 对象 | 当前实体缺失，回复以另一个长实体作明确判断主语 | “它是……”；短缩写或含数字别名，例如 Kubernetes→K8s |
| 行动 | 当前要求总结等只读动作，回复却声称已对同一对象执行删除、部署等副作用动作 | 条件句、疑问和建议，例如“如果现在删除会丢失证据”“你是想现在删除吗？我不建议” |
| 语言 | ContentIntent 为 zh-CN 却输出完整外语；明确要求 en 却主要输出中文 | 专名、代码、URL、短英文词 |
| Grounding | 同一个版本、端口或价格槽位出现与 accepted fact 明确竞争的数值 | 自然改写；冲突来源均已进入 accepted facts；`$19.99`=`19.99美元`、`1999元`=`1999块`、`2,999`=`2999`；一侧省略币种但数字相同也放行，只有双方显式币种不同时才判币种冲突 |
| Evidence | timeout/error 等失败结果却声称已经搜索、核实或验证成功 | “查证失败/没查到”“搜索超时”“已尝试查证但超时”，包括分句表达 |
| 媒体 | RAW_MEDIA 已绑定却声称“你没发图/没有收到图片”；UNAVAILABLE 却声称截图显示具体内容 | RAW_MEDIA 的“没看清/没看懂”；UNAVAILABLE 的“看起来像猫但不确定”；图片内容本身不存在某元素 |

独立误杀复核把判断统一收窄到局部语义命题，而不是堆全局关键词豁免：动作、视觉断言和 Evidence 都先按标点及转折连接词分段，再判断同一段中的明确执行、视觉强断言或成功取证。于是前一段出现“不建议/如果/不确定/超时”不能掩盖后一段“我已经删除/图里显示/我已经验证”，反过来纯条件、后果说明和诚实失败不会误杀。媒体否认只识别明确的发送/接收/消息附件缺失，不会把“图片里没有错误码”误判为没收到图；“已经尝试查证。可是搜索超时”“搜索了一下，不过没有得到可靠结果”“没找到可靠资料”都不会被当成成功取证。规则不把 transport 中存在图片等同于模型必然看懂图片，因此不会逼模型编造。

## 生产接线

1. `main.py` 在最终 ContentIntent、媒体和 Evidence 全部确定后构造一次合同，并放入 `OutputValidationContext` 和事件 extra。
2. `_guard_typed_reply()` 要求事件合同、ValidationContext、Composer Anchor/Action、ContentIntent、MediaContext 和 Evidence 复用同一对象或同一可信 binding。
3. 初稿以 `INITIAL` 校验；repairable 问题最多生成一次。
4. `RepairGenerationRequest` 强制携带同一个合同和同序 media item IDs；其 repr 不包含正文、真实 ID、system prompt 或 user prompt。
5. repair 使用同一个 `AstrBotMediaAdaptation` 和原始 image/audio transport arrays，Renderer 工具仍固定为空。
6. repair 输出以 `REPAIR` 再验；仍有任意问题即阻断，不存在第二次生成或旧答案 fallback。
7. 通过初稿或 repair 后，代码登记一次 stage seal，并绑定原 `PresentationHandoff`。
8. dispatch 先完成既有协议清理，再用换行聚合多个文本节点，保留否定与下一节点动作之间的边界。
9. `FINAL_SEND` 同时核对当前合同、ValidationContext、Composer、PlannedAction、ContentIntent、Media、Evidence、原 presentation 和 stage seal；对实际 aggregate visible text 重验并一次性消费 seal。跳过前置 guard、替换自洽合同、重建 presentation、后置语义篡改或重放都会清空发送链。
10. stale、provider error、非 typed、非 LLM、无文本与 inactive 等终止路径均撤销 outstanding seal，避免跨轮复用。

`core/output_validator_v2.py` 的生产 validation API 要求真实 typed `SemanticGuardContract`，不存在 optional bypass，也没有 legacy `SemanticRequirement` 分支。其他用户事实禁止片段仍保留，职责是阻止跨用户事实泄漏，不承担当前问题语义判断。

## 红灯与回归证据

实施过程保留了两个可复现红灯阶段：

- 第一轮 semantic seam：15 项中 12 项稳定红；接生产前相关矩阵出现 5 failures 与 4 errors，证明 typed context/report、blocking repair、同合同媒体 repair 和生产接线尚未存在。
- 独立审计要求转成测试后：120 项中 16 项稳定红，覆盖替换合同、缺少阶段 seal、blocking 空回复策略、数值等价、媒体/取证误杀、Evidence/PlannedAction 绑定、节点边界和 repr 隐私。

最终本地验证：

- P3-07 定向：semantic guard、validator、repair、pipeline、P8 offline/scenario 共 135/135 通过；
- 完整 `unittest discover`：670/670 通过；
- `compileall -q astrbot_plugin_shio`：通过；
- `git diff --check`：通过；
- `main.py` 与 `core/` 生产源码扫描：无 `SemanticRequirement`、`semantic_requirements`、optional `semantic_contract` 或 `if context.semantic_contract is not None`。

冻结后由独立只读审计再次逐项复现：

- 自建自然表达误杀/漏拦矩阵 `23/23`；
- 合同替换、跳过初检、跨节点掩盖、Evidence/action/fact 错绑、presentation 重建与 seal 重放等结构旁路 `16/16`；
- 复跑 focused `135/135` 与完整 `670/670`；
- 审计期间源码时间戳稳定，最终阻塞项为 0。

关键回归覆盖：

| 验收项 | 覆盖层 |
|---|---|
| Binding、Target、PlannedAction、Anchor/Intent、media IDs 与顺序 | semantic_guard；validator；pipeline blocking/零 repair |
| Evidence action ID、完整 GroundingFact 一致性、缺 Evidence | semantic_guard typed 单元 |
| 否定、对象、行动、双向回答语言 | semantic_guard；OutputValidator 阶段透传 |
| 版本竞争值、价格格式等价与自然改写 | semantic_guard typed GroundingFact 单元 |
| timeout 冒充成功，以及诚实失败/尝试/超时 | semantic_guard typed EvidenceOutcome 单元 |
| 多文本节点边界与发送前语义翻转 | semantic_guard aggregate；pipeline final-send |
| RAW_MEDIA、UNAVAILABLE、诚实降级和内容缺失 | semantic_guard；pipeline 原 transport repair |
| repair 成功且同合同/同媒体；repair 后再次漂移 | pipeline：一次调用后通过或阻断，无第二次调用 |
| guard 后替换合同、presentation、文本或重放 seal | pipeline 与 seal controller 单元 |
| contract/context/request/report/trace repr 隐私 | semantic_guard、validator、repair 单元 |

## 修改文件

- `core/semantic_guard.py`（新增）
- `tests/test_semantic_guard.py`（新增）
- `core/output_validator_v2.py`
- `tests/test_output_validator_v2.py`
- `core/repair_controller.py`
- `tests/test_repair_controller.py`
- `main.py`
- `tests/test_pipeline.py`
- `tests/test_p8_offline_acceptance.py`
- `tests/test_p8_scenario_matrix.py`
- `docs/reports/P3-07_SEMANTIC_MEDIA_GUARD.md`（本报告）

## 明确未覆盖与后续边界

- 自由文本事实中无法由稳定 slot 证明的矛盾，不在运行时靠关键词猜测；应进入离线 eval/聚类，而不是扩大通用正则。
- 跨语言长实体别名、复杂反问、隐喻和语用推断只在可证明时拦截；不确定就放行并观察，避免把人类表达压成模板。
- 本轮没有调用真实多模态 Provider，不能据本地测试宣称线上图片理解质量已经验证。
- 本轮没有修改 AstrBot 核心、LivingMemory、其他插件、计划、Git 或 FNOS，也没有部署。
