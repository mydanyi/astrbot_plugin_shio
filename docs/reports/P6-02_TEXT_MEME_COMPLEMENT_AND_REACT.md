# P6-02 文本/表情互补与 React action 报告

## 1. 结论

P6-02 已完成，候选未部署。

`REACT` 现在不是一个只停掉文字模型的占位决策：它在同一 accepted turn 内取得 exact current `PlannedAction`、canonical `ExpressionIntent`、registry-owned generation 与现场 Meme Manager conformance，只有 `VERIFIED` 才签 one-shot permit，并且只调用 Meme Manager 4.15.1 的 `compat_prepare_message` / `compat_send_prepared_message`。每次最多发送一张图片，永不调用旧 `search_memes` 语义选择链。

普通文字回复只在 code-owned 轻松语气规则命中时提升为 `TEXT_AND_MEME`；严肃问题、请求、错误排查、关怀、owner action 和超长文本保持纯文字。本层只发布互补意图，文字回复后的实际 Meme 顺序与 presentation receipt 留给 P6-04。

## 2. 根因与边界

P6-01 已证明唯一运行时对象和执行许可，但明确留下三个未闭合点：

1. `REACT` 热路径没有现场 collect / prepare / claim / execute；
2. 普通回复没有 code-owned text/meme complement 决策；
3. 没有 success、suppressed、failed、timeout 或 stale 的 canonical receipt。

如果直接复用 Meme Manager 的旧语义搜索工具，会形成第二条模型选择路径，并把 query、候选、caption、tag 或 raw result 带回星汐上下文。因此 P6-02 固定使用闭集表达 marker 和兼容执行面，不接收任何 query/candidate/tool 参数。

## 3. 实现

### 3.1 Code-owned complement policy

`decide_text_meme_complement()` 只接受 canonical `REPLY` plan 与 exact 当前消息摘要。它只对短、轻松、非问题、非请求、非错误、非严肃/关怀文本返回 eligible；输出仅为 typed decision，不含搜索词或候选材料。

热路径据此由同一个 `ExpressionIntentAuthority` 签发 `TEXT` 或 `TEXT_AND_MEME`。canonical `ReplyComposerRequest` 持有该 exact expression，raw `dataclasses.replace()` 不能把纯文本 request 偷换成 Meme request。

### 3.2 React 唯一执行链

`_execute_react_presentation()`：

- 清空旧 semantic meme extra，禁止旧选择结果成为 fallback；
- 从当前 AstrBot context 现场 collect exact unique Meme Manager；
- runtime 缺失、禁用、重复、接口或 source hash 漂移时签 typed `SUPPRESSED` receipt；
- verified 时依次 prepare lease、claim permit、执行 compat surface；
- prepare 后与 send 后都重验同一 generation；
- send 固定 `send_text=False, send_images=True`，最多一张图片；
- provider 零调用，失败不回退搜索或第二模型。

### 3.3 Receipt 与异常闭合

新增 sealed `MemeExecutionReceipt`，状态闭集为：

- `SUPPRESSED`
- `SUCCEEDED`
- `FAILED`
- `TIMED_OUT`
- `STALE_BEFORE_SEND`
- `STALE_AFTER_SEND`

receipt 只暴露 kind、状态、0/1 attempt、0/1 success 和闭集 reason；异常文本、消息、候选与工具材料不进入 repr/trace。exact primitive type、record type、identity 和 snapshot 都会在 inspection 时复核，`True == 1` 这类等价型篡改不能穿透。

prepare 后 generation 已过期时，只调用 Meme Manager 的零文字零图片 cleanup，并签 `STALE_BEFORE_SEND`；同一 permit 并发执行只有一个赢家。send 已成功但 generation 随后过期则签 `STALE_AFTER_SEND`，不得谎称未发送。

## 4. 验证证据

- P6-02 targeted：`12/12`；
- P6 / presentation / composer / guard / validator / repair / P5 reaction / pipeline related：`179/179`；
- Windows full：`1117/1117`，skipped 7；
- 隔离 AstrBot Python 3.12 container targeted：`12/12`；
- 隔离 AstrBot Python 3.12 container full：`1117/1117`；
- `compileall`、限定 `git diff --check` 与 selection-material 静态扫描通过；
- 隔离容器与 fnOS `/tmp` candidate 已清理；
- 生产 `main.py` SHA-256 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`；
- 生产容器 `running=true`、`restart=0`、WebUI 200；未覆盖、未重载、未重启。

容器脚本尾部第一次 WebUI `curl` 因 PowerShell here-string 的行尾进入 URL 返回 000；测试与生产哈希已在此前完成，随后用独立 exact URL 只读重跑得到 200。该命令问题不计作产品失败。

本地 staging archive `C:\Users\45928\AppData\Local\Temp\shio-p602-candidate-20260819a.tar` 的精确删除被执行策略拒绝，已记录为本地临时残留；它不在仓库、FNOS 或容器内，也未部署。

候选冻结哈希：

- `core/meme_presentation.py`: `9ac04d29908d5cfc4f9ae8768069efb2b36f158ccd95e32462a9d491b194fa81`；
- `core/action_planner.py`: `095d640862cb71dfe68a8925f223258dbcb2ce415388cbe1c6da758052003405`；
- `core/presentation_handoff.py`: `531211656004d8a86728a280ff13a5adb17b68afeb6ffbd14096795fb53c46c9`；
- `main.py`: `b150de835064c6c2436b2893b53c13a7c62ab088c7042f2ec515f6b4de5a50d4`；
- `tests/test_p6_meme_presentation_contract.py`: `a6c01a0d78827f9491a2cf06e71d1a070ea691fe988fb0344852b8c4132014a8`；
- `tests/test_presentation_handoff.py`: `e552349466ac5352a4a34dd01e241cd7cea93850700b297356d5a932c1f44f7d`。

## 5. 改动范围

- 扩展 `core/meme_presentation.py`；
- 修改 `main.py` 接入 REACT 热路径与文字互补意图；
- 修改 `core/presentation_handoff.py` 接受 canonical `TEXT_AND_MEME` 文字呈现；
- 修改 `tests/test_p6_meme_presentation_contract.py`；
- 机械更新 `tests/test_presentation_handoff.py` 的 raw-copy 防绕断言；
- 新增本报告并更新 `SHIO_MASTER_PLAN.md`。

未修改第三方插件、AstrBot 核心、配置 schema、FNOS 生产插件或 Git 状态。

## 6. 下一唯一入口

下一唯一入口是 **P6-03 语义安全拆泡**：在 canonical composer/validator/guard/presentation 链内建立 code-owned segmentation；每个最终 bubble 都必须保持主谓、因果、否定范围、当前话题与 action outcome 自足，首泡不能依赖后泡纠正；增删、重排、二次拆分和 post-guard mutation 必须 fail closed。中断后从本报告与 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4、P5、P6-01 或 P6-02。只有 P10 才请用户统一测试效果。
