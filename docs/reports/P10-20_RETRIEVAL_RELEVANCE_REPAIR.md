# P10-20 日常话语误检索与污染依据修复

## 事故结论

生产中的脱敏原始消息是“醒醒起床，让我检查一下身体，看看有没有修好”。最终回复却讨论抑郁、治疗师和身体活动。该轮群历史读取成功；首稿 provider 是当前 AstrBot 默认 Gemma，最终可见文本经过 Shio 的一次 repair。

同句受控 A/B 中，Gemma 裸问和最小 Persona 均围绕起床、检查与修复状态回答；只有完整 Shio 链偏题。因此不能用弱模型结案。

## 中断前事实时间线

1. 当前消息进入 Shio，verified group history 为 16 条。
2. `decide_knowledge_gap` 将该消息判为外部查证；原因是宽松正则在“检查一下”内部匹配了“查一下”。
3. Shio 执行一次只读检索并接受 8 条 grounding facts；返回内容与当前角色互动无关。
4. Gemma 首稿被 `grounding_evidence_drift` 拒绝；隐私安全日志不保存首稿正文，因此具体原句不可恢复。
5. repair provider 收到同一批错误 grounding facts，并返回可见 completion。
6. answer obligation 对该候选返回零 issue；两段偏题文本完成发送。

## 修复前假设

- H1（已证实）：裸子串“查一下”导致日常动词“检查一下”误触发外部检索。
- H2（已证实）：grounding adapter 只校验绑定、来源、参数、安全与时效，不校验 claims 和原查询的主题相关性。
- H3（已证实）：answer obligation 只覆盖问题复述、新实体澄清、能力与物理边界，未覆盖这类多动作日常请求；repair 因而可以忠实回答错误依据而不回答当前话语。
- C1（已排除）：Gemma 本身对该句必然偏题。同 provider 同句 A/B 均保持主题。
- C2（已排除）：群聊上下文完全缺失。该轮 verified history 和 provider context 均非零。
- C3（不足以解释）：LivingMemory 单独污染。该轮可观察到 Shio 自己的 `use_tool -> 8 facts -> repair` 完整充分链路。

## 修复合同

1. “检查一下／排查一下／自查一下是否修好”不构成外部查证；“请联网查证／帮我查一下公开事实／句首查一下”仍构成明确检索。
2. 搜索与知识库结果在进入 `GroundingFact` 前必须通过内容无关、确定性的最低主题相关性门；只有一个宽泛词重合的长查询拒绝并产生零 facts。精确 URL 抽取继续依赖 URL 绑定，不套用查询词门。
3. 当前消息同时包含唤醒、身体检查与修复状态等多个日常动作时，最终候选至少回应其中两个；只谈外部医学建议必须拒绝。自然的“我醒了／来吧／已经正常”等近邻表达必须通过。
4. 初稿与唯一 repair 使用同一最终校验合同；repair 不能绕过新增义务。
5. 日志只记录 reason code 和计数，不记录查询、claims、正文、群号或账号。

## 验收边界

单元、固定矩阵、全量、候选与生产启动健康只证明代码和部署层。真实群中同类消息的最终可见语义仍须用户确认，未确认前不得称端到端完成。

## 已实施修复

1. `knowledge_gap.py` 将显式查证改为有边界的请求短语；保留“请联网查证／帮我查一下／句首查一下”，排除“检查一下／排查一下／自查一下”。
2. `grounding_adapter.py` 增加 `IRRELEVANT_RESULT` 与内容无关的最低词汇相关性门；无关 claims 不再成为 facts，也不会流入首稿或 repair。
3. `answer_obligation.py` 增加唤醒、身体／状态检查、修复状态三类日常义务；命中两类以上时要求候选至少回应两类。
4. `output_validator_v2.py` 在 INITIAL 和 REPAIR 两阶段应用同一义务，避免修复模型绕过当前问题。
5. P-051、P-052、P-053 已写入踩坑账本；中文子串正反例、来源与相关性分离、初稿／repair 同合同已写入审查手册和 release surface 测试。

## 本地回归证据

- 修复前真实事故测试：29 项中 3 failures + 1 error，分别暴露误检索、无关依据接受与答非所问零 issue。
- 修复后 focused 54/54、受影响面 201/201。
- Windows 全量 1345/1345，skipped 7。
- 固定矩阵 24/24、Persona A/B 6/6、多模态 29/29、行为矩阵 24/24（68 assertions）。
- candidate/release 13/13，compileall 与 `git diff --check` 通过。

## 候选与生产证据

- 版本：Shio 0.5.20；候选 100 文件、550715 bytes。
- ZIP SHA-256：`8bc1b6662b1e383d88364a8464f9da05cd3079d47026e79d21ff3d3ecb5f40af`。
- manifest SHA-256：`bc2124751f536e5321b70441053f2cfa5c14274d281905b80665b5c1687bf2ba`。
- 本地、NAS 暂存与部署后 live 文件集／逐文件 hash 均通过同一 manifest 校验。
- 事务备份：`/AstrBot/data/backups/shio/P10-20-retrieval-relevance-20260824T151740Z`；状态 `VERIFIED`，保留 0.5.19 精确回滚源。
- 只替换 Shio 目录；配置保持 7 组/56 项，逐值、逐字节与 SHA-256 `cf075e6638b1d41c620d263cef7decfbdc98188c3fc291522ca907903eb9ff6b` 不变。
- Meme Manager 仍为 4.15.1，metadata/main/handler 三个 hash 部署前后完全一致；没有修改、打包或部署该插件。
- AstrBot running=true、RestartCount=0、OOM=false、WebUI 200；Shio 0.5.20 加载成功，启动窗口错误为 0。唯一 WARN 是 Manager 原有 WebDAV 图床配置缺失，与本轮无关。
- 线上 live 三项无正文语义探针通过：误检索=false、无关 evidence 接受=false、错误 repair 通过=false。

## 尚未完成的真实消息门

仍需用户发送一条同类日常复合话语，再结合无正文 trace 与用户可见回复确认：没有误检索、没有无关 facts、首稿或 repair 正面回应当前话语。该门通过前，P10-20 不标记端到端完成。
