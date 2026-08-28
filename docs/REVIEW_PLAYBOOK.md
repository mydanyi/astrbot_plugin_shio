# Shio 缺陷审查手册

这份手册解决的不是“测试数量不足”，而是同一作者容易沿用同一错误假设、从而让代码和测试一起通过的问题。每次缺陷修复都必须留下可以反驳实现者的独立证据。

## 1. 建立事实时间线

先按事件顺序记录：入站消息、唤醒来源、上下文条数与来源、主模型开始／结束、初稿可见长度、初稿 issue、repair provider、repair 返回形态、最终校验、发送回执。日志缺字段时，结论只能写“当前不可恢复”，不能猜异常内容。

同时列出至少三个假设：用户看到的直接原因、一个上游原因、一个并发／配置／插件交互原因。用只读证据逐一排除。不得先改最像问题的正则，再用该正则自己的测试证明问题解决。

## 2. 画清独立状态机

直接回复、称名唤醒、自然参与、冷场主动发起、React/Meme、工具检索、repair 和 final send 是独立流程。逐项确认：

- 谁拥有开关、概率、白名单、冷却与 provider；
- 输入上下文来自哪里，是否早于当前消息，是否同群同主体；
- 哪一步推进 revision/epoch，哪一步允许取消；
- 哪一步产生模型 token，哪一步得到可见 completion；
- 哪一步签发最终发送 authority，哪一步记录真实 receipt。

一个流程修好不能推定另一个流程同时修好。

“独立状态机”只表示触发、频率和取消策略分开，不表示可以复制生成与呈现实现。自然参与和冷场主动续题每次改动都必须画出共享下游：可信上下文／时间 → KnowledgeGap 与 sealed read → 完整 Persona → validator／repair → semantic segments → SendReceipt → Meme Manager。若另一路重新直接调用 Provider 或 `send_message`，必须先证明它仍通过同一合同与适配器，否则按新旁路缺陷处理。

自然参与还必须单独画出前置链：accepted event → Address → semantic candidate → cadence read-only preflight → current-session Provider 一次严格判断 → cadence finalize → reaction → generation promotion。审查时搜索问号／问句词、正则、二字交集、Persona interest、Owner warmth 与概率，确认它们都没有成为语义前的放行／拒绝 oracle；明确受话星汐和明确受话其他真实人物都应零 participation 调用。

## 3. 四向回归矩阵

每个事故至少覆盖：

1. **原始失败样本**：使用脱敏后的真实消息顺序、标点和时间关系。
2. **正确邻近样本**：相同话题下本应允许的自然改写。
3. **反向失败样本**：检查修复是否从误放行变成误拦截，或反过来。
4. **变体样本**：标点、空格、XML/JSON/Python/裸参数、同义词、不同 provider 返回形态、消息插队等至少一个变体。

对语义问题必须分别检查“旧问题相同”和“旧问题只改一个数字／否定对象”。对 provider 问题必须分别检查：抛异常、返回对象但 completion 为空、只有 reasoning、返回可见内容但解析失败、解析成功但校验失败。

自然参与 Provider 还要逐项拒绝 `None`、`err` role、重复键、缺键、多余键、非法 decision／target／anchor／reason／confidence；每个 canonical request 最多一次 permit／调用，没有 repair 或 fallback。测试人物必须使用真实显示名和结构化 Reply／多个 @ 关系，禁止用 A/B/C、路人或“成员 N”代替“谁在和谁说话”的 oracle。

### 3.1 同一句问题的模型／插件三段式 A/B

当用户怀疑“换模型导致变蠢”或插件导致答非所问时，不允许直接凭模型名、速度或 token 统计归因。对脱敏后的同一句当前问题固定执行：

1. **真实首稿模型裸问**：确认模型在没有 Shio 的情况下是否能抓住问题对象和直接回答；
2. **真实首稿模型 + 最小可信 message context**：只加入解决指代所需的紧邻上文，保持 role 和顺序，不加入 Persona／插件合同；
3. **完整 Shio 线上输出**：读取同一轮的实际 provider、上下文状态、Composer 计数、grounding、validator/repair 与 final bytes。

强模型可以作为第四个对照，但不能代替第一项；否则只证明强模型更强，不能证明原模型或插件谁造成回归。每个阶段必须记录实际 provider 所有者：AstrBot 默认、Shio 主动／repair、视觉、embedding 和其他插件各自分开，不得按 `replyer_provider_id` 等字段名称推断全部链路。

A/B 不能只比“是否出现问题关键词”，至少人工或独立判据检查：是否先回答当前问题、对象／动作／否定／指代是否保留、互动中的施事者、受事者和完成状态是否保持、是否只是复述问题、是否引入上文没有的新实体、能力／物理边界是否被 Persona 幻想替换。若原模型裸问或最小上下文能答而完整插件失败，插件链为主因；原模型也失败只能说明模型是竞争原因，仍需检查插件是否进一步放大。

主人聊天关系与主人外部动作权限必须分开审查。`primary_bond`、主人称谓和允许的关系表达只决定普通聊天怎样说；工具、文件、设备、记忆写入和其他外部动作仍只认 capability policy 与真实 action outcome。不得因为动作开关关闭而拒绝主人正常话题，也不得因为主人可以聊亲密、害羞或私人话题就授权任何外部操作。

## 4. 审查可观测性

一条最终 `empty_visible_reply` 不能覆盖真正的初始 issue。repair 诊断至少保留以下无正文信息：

- initial issue count 与 primary issue code；
- repair provider 的不可逆 digest；
- 是否收到 response object；
- visible/reasoning 字符数；
- provider 提供时的 output token count；
- provider exception、reasoning-only、empty completion、parse/validation rejection；
- 是否使用 code-owned fallback，以及最终是否发送。

异常只记录类型，不记录异常消息。正文、prompt、真实账号／群号、URL、路径和密钥不得进入 trace。

## 5. 差异复审

测试通过后重新阅读 `git diff`，逐项搜索：

- `except Exception` 后是否丢失原因或改成空字符串；
- `completion_text = ""` 是否会吞掉一个已经消费 token 的用户回合；
- 新 guard 是否同时有 false-positive 与 false-negative 测试；
- 是否把另一插件／AstrBot 已拥有的设置复制到 Shio；
- 每个生成阶段是否真的使用网页声称／操作者以为的 provider，还是只在主动或 repair 分支读取；
- 主动文字是否只证明了内部 send loop 成功，却在外层 scope/concurrency 封装返回前被自身 scene revision 变化降级；必须同时断言外层终态、同群冷却/日次数和近期摘要已提交，不能用可见消息或内部 `proactive.terminal` 代替；
- 并发/currentness 校验是否区分副作用前后的语义：发送前要求 scene/current generation 仍当前，发送后只校验不可变请求完整性与 exact scope；不得要求副作用之后仍保持副作用之前的 scene revision；
- participation cadence 是否在 Provider 前只做 read-only preflight；`WAIT` 是否零 join／零 backoff，`NO_ACTION` 是否只更新有界 backoff，只有 current `REPLY` 才提交 join。排队和飞行中 scene 更新、Provider 取消／失败、重复 claim/finalize 后，旧 generation 必须保持未取消，预算容量必须释放；
- participation Provider 是否来自当前会话而非 `replyer_provider_id`，并明确传入空图片／音频、空工具和零重试；scene→canonical messages 是否与 proactive 复用同一 helper，current body 是否只出现一次且人物 metadata 无内部 ID；
- 多群 allowlist 是否逐群覆盖“兴趣命中”和“兴趣未命中但有实质性公开讨论”；不能把 Persona 兴趣关键词变成 WebUI 看不见的第二个群白名单，也不能用 aggregate tracked/admitted count 代替每群 reason；
- 是否清空原生 message contexts 后把历史、Persona 和合同塞进单个大 user prompt；若是，是否用当前最弱生产模型做过三段式 A/B；
- 原生 contexts 是否真的进入首次生成和唯一 repair，并保持同一 role、顺序、scope 与数量；不得只在 Composer 对象或日志计数中存在；
- 平台／legacy history adapter 是否把 `sender_name`、昵称、引用发送者或 @ 显示名拼进语义正文；平台精确匹配表示与模型表示必须分离，Provider message content 只能是原始正文。真实显示名、reply/reference 和零到多个 @ 必须进入同一份 system-owned 不可信 metadata，稳定 `sender_key` 只在内部关联，禁止把 A/B/C、序号成员、哈希或账号当人物姓名。必须成对测试“昵称本身像话题／指令”不进入正文，以及用户真的在正文说出同一句话时完整保留；
- platform-history 对账的 digest 是否来自结构化 component projector 导出的纯正文，而非拼回 Reply 显示名或 `@`显示名的展示串；accepted fixture 必须由真实 ingress 或 `ConversationLedger.record_inbound()` 生成，禁止手工把姓名写进 accepted content。message admission 与 relation-name enrichment 必须是两道独立证据门：前者只按同 scope 的 source-verified sender、纯正文 digest、时间窗口、未重用与唯一候选接纳，模糊的同 sender／body／window 多候选不得按最近 timestamp、Reply 存在性或 At 数量猜；错 sender／scope、坏正文 component 或重用同一 accepted message仍须 fail closed；
- platform-history row 的 Reply／At 显示名是否只在 structured Reply `sender_id`／At `qq` 经既有 scope-bound identity 归一后与具体 accepted target key 精确对应时补入；禁止按 relation presence/count、昵称、component 位置或 `zip` 绑定。accepted 关系及目标顺序必须保留，已有可信姓名不得被冲突 row 覆盖；错 target、缺 ID、重复 key 或缺名只让相应姓名保持 accepted 值／`unavailable`。联合回归须覆盖 Reply 换目标、等数量 At 换集合、At 顺序交换、同名不同 stable key、row／accepted 姓名冲突、ID 有／无及不同 relation 多候选，并穿过 accepted row→assembled history→实际 Composer／Provider；普通 plain 内的字面 `@姓名`／`[回复 …]` 不得做字符串剔除；
- platform-history 中未匹配 accepted relation 的 Reply `sender_id`／At `qq` 是否虽不补名，却仍遗漏于 request-wide 显示名拒绝集；source admission 与 deny-only privacy 必须分门：先只按 sender／纯正文／time／唯一候选接纳并确定最终 rows，再把这些 rows 的全部原始 relation target ID 交给既有 identity/display 规范化所有者，且须早于任何 row speaker／relation display 投影。不得据此创建 participant、挑 source、改 accepted target／顺序、补名或猜人；用同一请求的跨行 speaker、跨关系 display、大小写／装饰及短 ID exact／分隔／自然包含变体，联合断言 actual ordinary Composer system／user／contexts 不泄漏，可信名／`unavailable`、纯正文和 relation 顺序不变；
- 人物 metadata 是否逐条保留同人改名、两人同名与有证据的受话关系，并用消息序号边而非人物代号表达关联；显示名或目标证据缺失时是否明确为 `unavailable`／`unspecified`，而不是按昵称、正文、自称或相邻消息猜人；普通 Composer 与主动 scene/runtime 必须复用同一 canonical contract；
- 当前入站记录是否虽然正确落入 ledger／scene，却因“历史必须排除当前消息”而连带丢失本轮 Reply／@；Composer 必须把它作为同一 canonical contract 的独立 `current_message` 人物／受话投影，且不得再次放进历史 Provider contexts。联合测试必须绑定同一个 current target 调用 `build_reply_composer_request()`，禁止人为创建后续消息把事故记录降格为历史后再宣称当前轮通过；
- 真实 AstrBot Reply 适配是否优先读取 production contract 的 `sender_nickname`，而不是由测试自造 `sender_name`／`name` 后让实现与 fixture 共同通过；Reply `sender_nickname → sender_name → name` 和 At `name → display_name` 的每个候选必须先经同一有界／strip 非空判定，再按显式优先级选择。第一个非空主字段若是或包含 raw ID，必须 unavailable 并立即停止 fallback；严格 fixture 要同测纯空白主字段+有效 fallback、全缺失、正常主优先、raw 主字段停降级，并覆盖同一 current event 的 Reply、Reply+单／多 @、同名不同人、纯正文和内部 ID 不泄漏；
- system／Provider 数据中是否出现原始 sender ID、`sender_key`、群号、bot／account ID 或内部关联 hash；不能只将每个 display field 与该人自己的完整 ID 精确比较。唯一 identity/display-name 规范化所有者必须从当前请求／上下文的既有 scope-bound keys 导出所有受保护 raw literals，任一显示候选等于另一参与者／群／bot ID 或以 `昵称(raw-id)` 形式包含任一受保护值时都必须降级 unavailable。current ingress、platform-history、ordinary Composer 与 proactive 必须复用同一所有者联合断言 system／user／contexts 无受保护字面，同时纯正文、accepted 关系顺序和稳定同一性不变；显示名中的控制字符、换行和伪结束标记仍要有界、清洗并按不可信 JSON 转义。普通 Composer 与 proactive system prompt 必须复用同一 canonical 编码／framing 所有者；用精确 `[对话人物与受话关系｜代码生成的不可信数据]`、`[人物元数据结束]` 及大小写／空白／控制近邻、普通方括号姓名和 80 字符边界，断言最终 prompt 中代码边界各只出现一次，同时 JSON 解析仍还原真实姓名。不存在的 HTML 式标记不能充当 oracle；
- Persona 与真实能力若迁移到 system role，repair 是否继承 exact 原 system prompt；不得让修复阶段只看到 user payload；
- validator 是否只证明关键词存在，仍放过问题复述、无关新实体或 Persona 压过事实边界；
- 能力快照是否来自权限过滤后的本轮工具清单，而不是 schema、默认配置或 Persona 自述；
- 自然语言工具触发是否只做了无边界子串匹配；每个中文动词正例都必须有嵌入其他词中的负例，例如“查一下”与“检查一下／排查一下”；
- sealed tool result 是否只证明 provenance 而没有证明 query relevance；搜索／KB 至少要测无关结果、单宽泛词重合、实体相关、常见同义表达和 URL 精确抽取，旧夹具不得用 `valid evidence` 之类占位文本冒充有效依据；
- repair 是否只是更忠实地改写 grounding facts，却仍未回应当前消息；初稿与 repair 必须复用同一个最终回答义务，组合式日常话语要同时测误放和自然省略误拦；
- 当前互动是否只保留了动作关键词，却交换了施事者、受事者或把 pending 编成 completed；模型 A/B、初稿 validator 与唯一 repair 必须逐层使用同一语义角色合同；
- current_user→assistant/pending 是否虽然没有显式角色交换，却被角色自报状态替换；最终回复必须有可观察的允许、邀请或继续动作，不能把“没有命中反向正则”当成正向履约；
- 主人聊天关系是否被错误绑定到默认关闭的外部动作开关；前者应自然回应，后者仍不得越权；
- 跨插件功能的代码、设置、概率、资源和发送分别由谁拥有；用户只授权消费者插件时，是否误改、打包、迁移或部署了提供者插件；
- 跨插件调用是否先核对生产实际公开接口与绑定签名，而不是为通过本地测试自造一个第三方方法；是否把最终校验文本交给唯一选择所有者；不得伪造消息来源、调用私有方法或复制第二份概率／逐图选择逻辑；
- 同一事件的插件钩子是否按真实数值优先级执行；不能只看装饰器名称或单插件单测。若消费者会 repair，提供者的选图必须发生在最终校验之后，且正常链不得再用 post-send 兼容接口补发第二次；
- Manager 模式是否按实际首稿 provider 的 modalities 分支验证：semantic-tool、semantic-LLM、legacy-category、unavailable 四条不能互相替代。索引 ready 只证明资源可用，100% 只证明候选产生后的概率命中；还必须断言请求提示未被消费者覆盖、最终候选非空、repair 只交回新标记、未消费标记不会泄漏到可见消息；
- 主动 category compatibility 是否用 Manager 当前全部官方类别的描述合同驱动，而不是由 Shio 用窄正则或固定 happy 预选；缺失、多重、未知和尾随控制标记必须 fail closed，类别必须在可见发送前剥离，Shio 不能复制 Manager 的概率、资源选择或发送逻辑；
- 跨插件模式中的“提示词存在”和“工具存在”是否被误合并成同一布尔状态；每种模式都必须用生产形状的严格容器拒绝 `None`／错类型成员，且断言 legacy prompt 可存在于空 ToolSet、semantic tool 则必须同时拥有有效工具。fake 不得比生产 Pydantic 类型更宽松；
- 框架是否在插件请求钩子异常后继续执行其他插件并最终发送；真实消息有回复或图片不等于当前插件成功，必须逐插件核对 prepared/guard/repair/send 阶段和异常归属；
- 第三方版本相同但文件 hash 不同时，是否把“精确运行文件不一致”误判成必须覆盖第三方；只要公开合同满足，应保留生产提供者并用部署前后 hash 不变证明未触碰；
- 语义向量候选是否仍经过 Manager 的情感选择器与 exact candidate ID 校验；选择器拒绝时是否确实无图；
- 测试命令是否真正完成收集和执行；缺依赖、导入失败或零收集必须单列，不能算红绿证据；
- schema、默认值、迁移、运行读取、README、版本与发布测试是否一致；
- 测试是否包含真实账号、群号、私聊正文、Provider URL 或密钥；
- 本地源码、压缩包、生产目录和实际加载版本是否一致。

## 6. 分层验收

按层记录结果，不允许合并成一句“已验证”：

1. 纯函数／合同测试；
2. 真实事故回放与邻近变体；
3. 全量自动化、compile、固定矩阵、候选包与隐私扫描；
4. 隔离 AstrBot 容器加载；
5. 生产备份、配置哈希、源文件哈希、插件加载与 WebUI；
6. 真实群消息触发、最终可见回复、日志阶段和发送回执。

第 6 层未做时，只能说“部署态健康，等待真实消息验收”。

部署材料本身也必须闭包审查：部署脚本引用的候选 ZIP、manifest、版本化 live/config verifier 和语义 probe 都要在当前工作区真实存在、版本一致，并由测试枚举验证；旧备份中曾经存在或某次远端执行成功不能替代本次本地闭包证据。

跨插件功能必须先判定发布所有权。只有两边都由本项目维护、都确实需要修改且都在用户授权范围内时，才生成配对候选。若修复只属于消费者，必须只生成消费者候选；提供者只做只读版本、唯一实例、公开方法签名、钩子顺序和关键源码 hash 核对，并在部署后证明这些 hash 未变。不得因为本地第三方副本与生产 hash 不同就替换生产第三方，也不得让自造的兼容接口进入测试夹具后反过来要求第三方实现。

对跨模块功能还要保留一条“同请求联合断言”：不得用上下文单测、工具单测、气泡单测和 Meme 单测各自通过来推断集成完成；同一个 canonical request 必须能观察到所需上下文、检索选择、Persona 投影、segments、逐段回执和可选 Meme 终态。

## 7. 审查方法失效后的重审

当一个新缺陷证明旧审查方法和实现共享了同一个错误假设时，受影响范围内的旧结论失效。不得只补一条新用例后继续引用此前的“全绿”；必须重新运行完整相关套件，并逐个分类旧测试暴露的问题：

- 当前实现违反产品合同：修实现，并补原始失败、反向失败和近邻变体；
- 旧测试绕过后来新增的开关、白名单、上下文或状态迁移：修测试夹具和断言；
- 测试装配缺文件或运行环境不同：补齐显式 harness 清单，从零装配后全部重跑；
- 测试与实现都来自同一假设：必须引入能反驳该假设的竞争用例，不能用测试数量代替独立性。

不得拆除生产安全门。为了让旧测试重新变绿而放宽或删除这些门槛，属于新的审查缺陷；任何此类修改都必须停止并回到当前产品合同重新判断。

内容无关日志只能证明阶段、计数、路由与发送终态，不能证明回复语义正确。语义质量需要用户确认或隐私安全的可见产物；否则只能写“发送层通过”。LivingMemory recall 与 verified group history 也必须分开判定：可选 memory reader 降级，不等于群聊历史没有注入。

## 8. 中断与交接

任何未完成工作都更新仓库根目录 `HANDOFF.md`：当前目标、已证实事实、已改文件、已跑测试、生产是否变化、下一条安全命令、仍需用户完成的真实验收。新的坑同步写入 `PITFALL_LEDGER.md`，避免下一次会话从聊天记录重新考古。

## 9. 提示词角色迁移审查

把数据从 user prompt 移入 system prompt、或从扁平 JSON 改成 provider messages，不是纯字符串重排。每次迁移至少逐一验证：

1. 首次生成收到公共 system 约束、exact Persona、代码能力快照和 canonical user payload；
2. 当前消息不重复进入历史，其他成员不会被冒充为当前发言者；
3. 唯一 repair 继承相同 system、contexts、媒体和语义合同；
4. 测试比较的是 role 与所有权，不是旧标记的字符串位置；
5. 日志只记录 owner、digest 与 count，不记录 Persona、上下文正文或 prompt。

## 10. Typed attribution（R11）

对任何包含不同 canonical 历史发言者的归因敏感请求，检查 Composer 的 Provider 输出是否为单一 envelope，且 `visible_text` 与 `attribution.segments` 被分别处理。每个 segment 必须带 actor 的 raw platform/sender、非空 predicate、已知 polarity、局部 scope 与不重复的 request-local evidence IDs。validator 必须只从同一请求的 canonical history 解析这些 IDs，并精确比对证据消息的 raw actor；不可接受缺失、重复、越界、伪造、错 actor 或纯文本回退。检查 INITIAL、唯一 REPAIR 和 final-send 都复用同一 validator ticket；最终可见回复只发送剥离后的 `visible_text`，默认不泄漏 raw IDs，显示名仍只来自已验证 metadata。

## 11. Semantic risk preflight（R12）

审查同一 selected Provider route 是否在 PRIMARY 前恰好运行一次严格 JSON risk 调用，并以完整 canonical current/history（正文、raw platform/sender/account/scope、display、message ID、timestamp/order、Reply/@、assistant_self）构造 prompt。`ATTRIBUTION_REQUIRED` 必须在 mint 前写入不可变 Composer request，使 validator、唯一 repair 与 final-send 强制 typed segments；`NONE` 保持自然正文；空、非法、异常、路由或 snapshot 不一致必须 `UNCERTAIN` fail-close。不得以旧正文 regex、问号、动作或否定词覆盖决策。对 predicate/raw-ID 防泄漏使用既有边界 identity matcher，固定验证短 ID `1` 不误拦“12点”、而 token-boundary 的真实 `1` 仍拒绝；同测第二人物与代词夹带、current evidence、跨请求重放及 repair/final binding。
