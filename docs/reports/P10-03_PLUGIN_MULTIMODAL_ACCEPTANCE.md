# P10-03 生产插件共存与多模态验收

## 结论

P10-03 已完成。LivingMemory、AnySearch、Meme Manager、ReNeBan、Parser 与外部 gate 的正常、缺失、接口漂移、错误和超时路径已固定为 15 个插件案例、4 个门控案例和 5 个真实消息表面案例。每一行都指向一个可执行的生产合同回归，不以“插件目录存在”或日志中出现插件名冒充运行时 authority。

直接文本、引用文本、引用图片、仅图片和同媒体一次 repair 均通过现有 typed 热路径。P10-03 首次运行暴露了一个真实生产缺口：原生图片消息在正文与 outline 都为空时，会在媒体适配之前被 ingress 当成空消息丢弃。本阶段没有降低测试门，而是把原生消息链中的 direct/quoted image/audio 作为唯一的前置媒体证据，生成代码固定的 `[仅媒体消息]` canonical 描述；普通空消息、纯文本组件和无媒体的引用仍然拒绝。

本阶段没有部署。线上仍是 P3 稳定版本，P10 候选只在 Windows 与一次性、无网络、候选目录只读挂载的 AstrBot Python 3.12 Linux container 中验证。

## 固定矩阵

### 插件共存与降级

`tests/fixtures/p10/plugin_multimodal.json` 固定以下闭集：

- LivingMemory：present / missing / timeout；
- AnySearch：present / interface changed / timeout；
- Meme Manager：present / missing / execution error；
- ReNeBan：present / missing / error / interface changed；
- Parser：present / interface changed；
- gates：verified banned 零 Shio commit、ReNeBan 缺失 fail closed、tool hook stop 零执行、新轮取消旧 generation。

生产只读观察还记录 Group Verification、Recall Cancel 与 Keywords Reply 的存在和 24 小时安全计数，用于证明现场形状与后续 P10-06 检查范围；它们不被提升为 canonical plugin evidence。

### 多模态热路径

五个表面分别覆盖：

1. direct text 经 production typed runtime；
2. quoted text 保持 reference/target 绑定；
3. quoted image 保持被引用 message/sender；
4. image-only 经真实 `ShioPlugin.enforce_agent_permission → build_persona_reply → AstrBotMediaAdaptation → CurrentQuestionAnchor → ContentIntent → ReplyComposerRequest`；
5. repair 复用同一个 exact MediaContext 和原生 transport，不把 URL/base64/path 写入 prompt、trace 或持久状态。

## 仅媒体入口根修

### 红灯

初次 P10-03 runner 为 `29` 个执行项、`2` 个失败（同一 image-only 热路径同时作为模块测试和矩阵 evidence 执行）。`SHIO_TYPED_PIPELINE_ACTIVE` 保持 false，证明旧代码在 `prepare_ingress_candidate()` / `admit_ingress_event()` 阶段提前拒绝了空正文。

### 修复

- `core/astrbot_media_adapter.py` 新增 code-owned `MEDIA_ONLY_CURRENT_MESSAGE` 与 `media_only_current_message()`；
- 只接受原生 chain 中 exact image/audio，或带嵌入 image/audio 的 reply；
- 不读取 provider URL 数组，不把 locator、caption 或 component repr 当作消息正文；
- `main.py` 的 ingress candidate、admission 与后续 build 使用同一个 canonical message 函数，保证 content digest、anchor、participation、composer 完全一致；
- 新增 direct image、audio、quoted image 正向和空 chain、plain text、empty reply 负向回归。

修复后 image-only 绑定一个 direct media item，source message/sender 与当前 turn 一致；prompt 只含 `origin=direct` 等安全 typed 证据，原生 URL 只留在 Provider transport 数组。

## 生产只读核对

- LivingMemory `2.5.7`、AnySearch `v0.3`、Meme Manager `4.15.1`、ReNeBan `v1.2.0`、Parser `v1.5.1`、Group Verification `2.4.0`、Recall Cancel `v2.1.3`、Keywords Reply `v1.4.1` 均在现场 inventory 中存在；
- 24 小时日志安全引用计数均大于 0，但只作现场观测；
- Meme Manager 三个已审计源哈希继续匹配生产 profile；
- production container `running=true`、`restart=0`，WebUI HTTP `200`；
- production `main.py` 仍为 `583BF681D28BBA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`；
- FNOS 临时 staging 已删除，没有 reload、restart 或覆盖线上插件。

## 可重复入口

```powershell
python -X utf8 astrbot_plugin_shio/scripts/run_p10_plugin_multimodal.py
```

当前 content-free 输出：

```json
{"error_count":0,"evidence_test_count":24,"executed_test_count":29,"failure_count":0,"flow_case_count":5,"gate_case_count":4,"passed":true,"plugin_case_count":15,"privacy":"content_free_counts_only","schema_version":1,"skipped_count":0}
```

## 验证

- P10-03 runner：`29/29`；
- Media adapter + production plugin/gate/search/meme/repair focused：`190/190`；
- Windows full：`1244/1244`，skipped 7；
- 隔离 AstrBot Python 3.12 Linux runner：`29/29`；
- 隔离 AstrBot Python 3.12 Linux full：`1244/1244`；
- `compileall`、P10 JSON parse、privacy scan、merge-marker、`git diff --check`：通过；
- 验证归档：`C:\Users\45928\AppData\Local\Temp\shio-p1003-20260819-v1.tar`，SHA256 `7541E61ED4C881745DFD788E1F6A5216D772A1D5FDE0D381CFF33CC81EAD0DBC`。

## 关键哈希

- `main.py`: `818E5EC774B6254289D8BA810A584CC7031D0139DCFAE39BED0C3D9EC1DB0765`
- `core/astrbot_media_adapter.py`: `75FDBA588C47E1A10D0BCA7C6FE7731B91E23C9B727667A0AC312E4350F05612`
- `tests/test_astrbot_media_adapter.py`: `DB64B23FC1A995E353202B705B322B609189FEC23B1AAC9953AD95F1AC7C0D63`
- `tests/fixtures/p10/plugin_multimodal.json`: `895545996D1C20BAFB7EC3EA48D000CCF3C768421AF07DEB3EDB0E1236990349`
- `tests/test_p10_plugin_multimodal.py`: `337E0C32E14C8EE853801E89E5DB0ABE657D2EB75C591B7DB3608998DEB8FE0B`
- `scripts/run_p10_plugin_multimodal.py`: `68F7D27D0AC9B5BE8E83F8118A012C760009277E588A2C5172BC2060033EC030`

## 边界

- `X-01` 仍表示外部 LivingMemory 在 Shio 准入前的捕获时序；本阶段只证明 Shio 零消费/零状态提交，不冒充生态层零存储。
- plugin inventory 与日志计数不是运行时 authority；真实正向调用仍由各模块原有 code-owned evidence 和 exact object 检查决定。
- owner adapter 继续全关；未修改第三方插件，未执行 Git 写操作。
- README、schema、metadata 与发布版本统一留在 P10-04；当前没有提前改变对外能力口径。

## 下一入口

唯一下一入口是 **P10-04 README/schema/metadata/版本统一**：只宣传已经闭环的能力，明确 all-off owner adapter、`X-01` 外部生命周期限制和 O1 未执行状态；核对配置 schema、插件 metadata、README、报告和运行默认值一致。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P10-03。
