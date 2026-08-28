# P6-03 语义安全拆泡报告

## 1. 结论

P6-03 已完成，候选未部署。

聊天气泡现在只在 code-owned、语义自足的边界生成：引号和括号内部不拆；因果、条件结果、否定修正、失败/部分生效/不确定状态的转折以及依赖前文的话题从句会原子合并。普通人格转折仍可独立成泡，不会因为开头有“不过”就机械合并。

最重要的结构修复是：发送前不再从 guarded `visible_text` 第二次调用 splitter。`PresentationHandoff.final_segments` 是唯一 canonical segment boundary；FINAL_SEND guard 和发送循环都消费同一个 exact tuple。

## 2. 正式红灯

新增 `tests/test_p6_semantic_bubbles.py` 后，旧实现稳定得到 `9 failures + 1 error`：

- `不是…… / 是因为……` 被拆成两个气泡；
- `没有完全成功 / 不过可能部分影响 / 所以不能说完全没动` 被拆成三个气泡；
- `如果…… / 才……` 条件与结果分离；
- 中文引号内问号被当成边界，右引号落到下一泡；
- 话题从句 `其中一个 / 另一个` 被单独发送；
- `True`、0、浮点和字符串等含糊 limit 没有统一 fail closed；
- `dispatch_chat_bubbles()` 在 presentation seal 后再次调用 `split_chat_bubbles()`，形成第二条边界解释路径。

## 3. 根修

### 3.1 Quote-aware sentence scan

`core/response_guard.py` 的 splitter 改为单遍扫描：

- 识别中文单双引号、书名式引号、圆/方括号与 ASCII 双引号；
- 只在所有 quote/bracket 都闭合后接受句号、问号、感叹号或换行边界；
- 未闭合引用保留在同一 bubble，不把后续内容误当成独立陈述；
- `text` 与 `max_bubbles` 使用 exact type，bubble limit 固定 1～8。

### 3.2 Semantic dependency merge

候选句先按依赖关系合并，再做只合不拆的上限收敛：

- `因为 / 所以 / 是因为 / 因此 / 否则`；
- `如果 / 只有 / 除非 / 一旦` 与其 `才 / 就` 结果；
- `不是 / 并非` 与 `而是 / 是因为`；
- `虽然` 与尚未出现的转折结果；
- `其中 / 另一个 / 前者 / 后者 / 对此` 等话题依赖；
- 带失败、部分、可能、不确定、未提交等状态词的纠错转折。

普通 `不过被你夸，我还是开心` 仍可独立发送；`不过可能已经产生部分影响` 必须和前一状态合并。这避免用粗词表牺牲自然对话节奏。

### 3.3 Canonical final segments

`main.py` 的 FINAL_SEND 阶段现在直接读取 `presentation.final_segments`。该 tuple 已绑定 exact composer request、validation result、semantic contract、visible bytes、segment digest 和 action outcome；增删、重排、二次拆分、跨 request 替换与 post-guard mutation都会使 canonical inspection/FINAL_SEND consumption 失败。

`SemanticGuardReport.trace_metadata()` 把 FINAL_SEND 的 blocking segment/byte mismatch 明确标为 drift，便于在不泄露正文的 trace 中区分生产发送前漂移。

## 4. 验证证据

- formal red：`9 failures + 1 error`；
- P6-03 targeted：`4/4`；
- P6-03/core/composer/presentation/guard/pipeline related：`188/188`；
- 加 action outcome、validator、repair、send receipt 的扩展 related：`230/230`；
- Windows full：`1121/1121`，skipped 7；
- 隔离 AstrBot Python 3.12 container targeted：`4/4`；
- 隔离 AstrBot Python 3.12 container full：`1121/1121`；
- `compileall` 与限定 `git diff --check` 通过；
- fnOS/container `/tmp` candidate 已清理；
- 生产 `main.py` SHA-256 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`；
- 生产容器 `running=true`、`restart=0`、WebUI 200。

本地 staging archive `C:\Users\45928\AppData\Local\Temp\shio-p603-candidate-20260819a.tar` 未删除；与 P6-02 相同，精确 `Remove-Item` 会被执行策略拒绝。它不在仓库、FNOS 或容器内，也未部署。

候选冻结哈希：

- `core/response_guard.py`: `545143936f6bad5e536fe3734bea42bb0fadc3a78a000514d7430e4af71a0234`；
- `core/semantic_guard.py`: `1f0da7b5a05d4dac7aaf32796f6daa7dafb2dd82a14a6ccf42549b69d6718308`；
- `main.py`: `b1d304c2f006c84d2ff4bb0ef532d648c6e591c05df9d0f47160f45873f43d2b`；
- `tests/test_p6_semantic_bubbles.py`: `39f0c9418bff727b0086dd266e4110da0d695fc0bb0113d96241c82874adc399`；
- `tests/test_pipeline.py`: `d69cf501ed2702c8230e0fc6eb37ed72f492a33e1ecb64e4da4a51e58b90c3d1`。

## 5. 改动范围

- 修改 `core/response_guard.py`；
- 修改 `core/semantic_guard.py`；
- 修改 `main.py`，删除 FINAL_SEND 二次拆分；
- 新增 `tests/test_p6_semantic_bubbles.py`；
- 更新 `tests/test_pipeline.py` 的因果原子发送预期；
- 新增本报告并更新 `SHIO_MASTER_PLAN.md`。

未修改第三方插件、AstrBot 核心、配置 schema、FNOS 生产插件或 Git 状态。

## 6. 下一唯一入口

下一唯一入口是 **P6-04 发送顺序、去重与 presentation receipt**：让文字与可选 Meme complement 共用一次 code-owned presentation transaction；每个 exact segment/Meme 只尝试一次，文字成功后才允许 complement，真实 send terminal evidence 后才记账；失败、取消、generation 过期和 automatic/manual send 分支都必须形成 closed receipt，且 Meme query/candidate/raw result 永不进入下一轮历史。中断后从本报告与 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4、P5 或 P6-01～P6-03。只有 P10 才请用户统一测试效果。
