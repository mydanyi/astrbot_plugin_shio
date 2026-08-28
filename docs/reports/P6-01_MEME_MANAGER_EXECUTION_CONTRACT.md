# P6-01 ExpressionIntent → Meme Manager 唯一执行契约报告

## 1. 结论

P6-01 已完成，候选未部署。

星汐现在具备一条独立、可验证的 Meme 呈现权威链：

`canonical PlannedAction → canonical ExpressionIntent → current GenerationEpochSnapshot → audited Meme Manager runtime evidence → one-shot execution lease/permit`

本层不调用模型，不调用 `search_memes`，也不接收 query、候选 ID、caption、tags、raw result、私有记忆或工具参数。P6-02 只能从这个 permit 进入 Meme Manager 的 `compat_prepare_message/compat_send_prepared_message` 执行面；旧语义工具选择链不是星汐的执行入口。

## 2. 只读生产核对

通过 FNOS 只读核对确认：

- 线上 Meme Manager 为 `4.15.1`；
- 插件主类为 `MemeSender`；
- 兼容执行面为异步 `compat_prepare_message(event, message)` 与 `compat_send_prepared_message(event, prepared, *, send_text, send_images)`；
- 线上同时存在旧模型 `search_memes` 选择链，因此 P6 必须明确选择唯一执行面，不能双调用；
- 已审计源码 SHA-256：`main.py=028188c6…b2416`，`mixins/event_handlers.py=4867e3f8…23dc`，metadata 为 `ece17c21…e352`。

生产 profile 固定版本、plugin/root/module/class、metadata type、两个 bound coroutine 的参数形状以及两个源码文件摘要。缺插件、禁用、重复实例、接口漂移或 build 漂移都只返回闭集 degraded 结果，不签 runtime evidence。

## 3. 正式红灯

新增 `tests/test_p6_meme_presentation_contract.py` 后首先稳定得到：

```text
ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.meme_presentation'
Ran 1 test / FAILED (errors=1)
```

这证明原代码只有 public `ExpressionIntent` shape 和未消费 handoff，没有 exact expression authority、live runtime conformance、current generation 绑定或 one-shot Meme execution permit。

## 4. 根修

### 4.1 所有热路径计划可被 exact inspect

`plan_action()` 在调用方提供 `PlannedActionAuthority` 时，现在统一发布最终 plan；owner-action 分支的既有发布保持幂等。`main.py` 对所有 typed action 都传入同一个长生命周期 authority，因此 REPLY、USE_TOOL、REACT、NO_ACTION、WAIT 与 EXECUTE_ACTION 不再出现“只有 owner plan canonical”的分裂状态。

### 4.2 ExpressionIntent 由 authority 自行构造

新增 `ExpressionIntentAuthority`：

- 只接受 exact canonical plan 与 plan authority；
- 自行构造并保存 exact plan/binding/target/full-field snapshot；
- 同一 exact plan 只能签一份 intent；
- copy、cross-plan、post-issue nested mutation、错类型与字段漂移 fail closed；
- 有界 ledger 与 content-free trace。

`main.py` 的普通文本与 REACT 两个生产构造点都已迁移到该 authority。

### 4.3 Live conformance 与 one-shot execution

新增 `MemeManagerConformanceCollector` 和 `MemeExecutionAuthority`：

- collector 从 AstrBot public star registry 取得唯一激活 metadata 与 exact `star_cls`；
- exact 核对 version/module/class/bound method/signature/source digest；
- evidence copy、跨 collector、字段 mutation 与 build drift 均失权；
- execution prepare 同时重验 exact plan、exact expression、exact current generation、同 scope/session/epoch 与 exact runtime evidence；
- REACT 只形成 `REACTION`，REPLY 的 meme modality 只形成 `MEME_COMPLEMENT`；
- claim 前再次检查 generation，stale 不消费；成功后一次性 mint permit，replay/copy/cross authority 拒绝；
- lease/permit/trace 没有 query/candidate/caption/tag/raw/tool/private-memory 字段。

## 5. 验证证据

- formal red：导入失败 1 项；
- P6-01 targeted：`6/6`；
- Planner/P5/generation/pipeline/P6 related：`117/117`；
- Windows full：`1111/1111`，skipped 7；
- 隔离 AstrBot Python 3.12 container targeted：`6/6`；
- 隔离 container full：`1111/1111`；
- `compileall`、tracked diff-check 与 untracked whole-file whitespace scan 通过；
- staging 仅位于 `/tmp/shio-p601-root-20260819a`，container/remote host staging 已删除；本地临时 tar 因执行环境删除策略拒绝而保留，不参与运行；
- 生产 container `running=true`、`restart=0`、WebUI 200；
- 生产 `main.py` SHA-256 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`，未覆盖、未 reload、未 restart。

候选哈希：

- `core/action_planner.py`: `095d640862cb71dfe68a8925f223258dbcb2ce415388cbe1c6da758052003405`；
- `core/meme_presentation.py`: `8d4d2804c57b70cafc046ddec1bf45d53e11185ef1798e6156116ee933ed9d14`；
- `main.py`: `6d3121c86781f143583290b6a0a4bc0ae4f236ce26729e801a4a4c966380507e`；
- `tests/test_p6_meme_presentation_contract.py`: `c315a54f58148d361062eb10a6e8954bc5c4bbf2aea86039b91773e9cba79ce9`。

## 6. 边界

P6-01 只建立权威链和运行时合同，尚未在热路径调用 Meme Manager。它不会把“源码哈希已知”冒充“候选已经在生产进程取得 exact star object”。实际 collect、permit 执行、success/failure receipt 与 generation cancel-safe await 属于 P6-02。

未修改 Meme Manager、AstrBot 核心、线上配置、生产插件或 Git 状态。

## 7. 下一唯一入口

**P6-02 文本/表情互补与 React action**：在 REACT 热路径现场 collect exact Meme Manager runtime；只有 VERIFIED 才 prepare/claim 并通过兼容执行面最多发送一张图片。严肃、请求、问题、错误和 owner action 不得发 React；普通文本只在 code-owned complement policy 明确允许时进入 meme modality。执行前后都要重验 current generation，失败只形成 typed suppressed/failed receipt，不能回退到 `search_memes` 或第二模型选择。中断后从本报告与 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P6-01。
