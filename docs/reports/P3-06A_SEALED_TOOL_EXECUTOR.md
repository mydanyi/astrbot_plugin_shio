# P3-06A AstrBot 密封取证执行器

日期：2026-08-18  
范围：只新增 `core/astrbot_tool_executor.py`、`tests/test_astrbot_tool_executor.py` 与本报告；未接 `main.py`，未修改 AstrBot、AnySearch 或其他插件，未部署，未执行 Git 写操作。

## 1. 结论

P3-06A 的纯执行适配层已经完成。它不再把 AnySearch 工具箱交给最终 Persona Renderer，也不依赖 AstrBot 的模型工具循环重新进入星汐。输入只能是 P3-04 已生成的同轮 `AcquisitionRequest`；成功时输出一个由代码生成 call ID、且兼容 `adapt_tool_call_results()` 的单 call/单 result batch。失败、超时、停止、过期、空结果、多结果和直接发送形态均返回 `batch=None`，因此不能伪造 P3-05 `GroundingFact`。

本层仍是纯模块交付。P3-06 原子热路径切换必须在主链中显式调用它，并确保最终 Renderer 的 `ToolSet` 为空。

## 2. 先红后绿

先新增完整测试，再运行：

```text
python -m unittest astrbot_plugin_shio.tests.test_astrbot_tool_executor -v
```

初始稳定红灯：

```text
ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.astrbot_tool_executor'
Ran 1 test ... FAILED (errors=1)
```

首轮实现后：

```text
Ran 15 tests in 0.048s
OK
```

主链接线后的独立安全复核进一步发现 AstrBot 本地工具可以通过真实 `event.send()` 在 executor 产出结果前直接发送。执行器因此改为只传入不持有真实 event/context 的 `_SealedEventProxy`，并把“任何发送尝试”本身视为失败。补充回归后本模块为 `18/18`。

## 3. 执行边界

### 3.1 唯一允许路径

- 仅接受真实 `AcquisitionRequest`。
- 仅接受 `SEARCH / EXTRACT / BATCH_SEARCH` 三种 P3-04 AnySearch 形状。
- exact tool 必须分别是 `anysearch_search / anysearch_extract / anysearch_batch_search`。
- capability 必须是 `PUBLIC_WEB_READ`，source 必须是 `astrbot_plugin_anysearch`，call budget 必须为 1。
- 重新计算 request-shape digest；替换或增加实参会在 schema/执行前闭锁。
- runtime `ToolSet` 或 iterable 中必须恰好存在一个同名原对象；不按名称取内部 `_wrapped`，也不绕过 AstrBot 的 `_PermissionGuardedTool`。
- runtime 对象必须 active、非 background，并经现有 capability classifier 与精确 AnySearch module token 再次证明为 `PUBLIC_READ`。
- `HandoffTool`、`MCPTool`、background、重名、缺失、伪来源和非 allowlist 名称均零执行。

### 3.2 实参与 Hook

- 实参只由 `request.materialize_arguments()` 生成。
- 工具 schema 被深拷贝，不修改 runtime tool；所有 object 节点强制 `additionalProperties=false`。
- 生产默认延迟导入 `jsonschema.Draft202012Validator`，校验 schema 以及 required/type/enum/额外键。
- 生产取证不调用 AstrBot 通用 `on_tool_start`，因为该 hook 会拿到 exact tool/event，且不是权限边界；测试注入 hook 只收到实参深拷贝和隔离上下文。
- Hook 完成后重新选择同一原对象、重新生成实参并再次进行严格 schema 校验。Hook 抛错、超时、停止事件或使 generation epoch 过期时，不调用 executor。

### 3.3 执行和结果

- 生产默认延迟导入 `FunctionToolExecutor.execute`、`ContextWrapper` 和 `AstrAgentContext`；模块导入本身不要求本地测试环境安装 AstrBot。
- executor 的 run context 被重建为 `context=None`，event 是无真实 event/context 引用的 `_SealedEventProxy`；`result/extras/stop` 只写代理本地状态，`send/send_*` 在真实发送发生前拒绝。
- request 的代码 deadline 是整个 Hook + executor 的外层上限；过期前不会扩展或重置预算。
- 只接受一个非 `None`、含非空文本内容的 CallToolResult-like 结果。
- `None` 视为直接发送形态并拒绝；即使工具捕获代理的发送异常后返回合法结果，只要 `send_attempted=True` 仍拒绝。两个及以上结果、裸字符串、异常、显式 error、`error:` 结果和空内容均不生成 batch。
- 成功 batch 的函数名与参数来自 sealed request，call ID 由 `secrets.token_hex(32)` 生成，结果正文只保存在 repr-hidden content 中。
- outcome、batch、call、result 和 trace 的 repr 不包含 query、URL、正文、call ID、action ID、scope、sender 或 message ID。

## 4. 公开 API 与 P3-06 接线方式

```python
outcome = await execute_sealed_acquisition(
    request=acquisition_request,
    runtime_tools=request_tool_set,
    run_context=astrbot_run_context,
    clock=time.monotonic,
    event_stopped=event.is_stopped,
    epoch_current=lambda binding: generation_epochs.is_current(...),
)
```

成功时：

```python
typed_results = adapt_tool_call_results(
    outcome.batch,
    tools=runtime_tools,
    scope_key=acquisition_request.binding.scope_key,
    target_sender_key=acquisition_request.binding.current_sender_key,
    acquisition_request=acquisition_request,
    observed_at=time.monotonic(),
)
```

随后只能把 `adapt_grounding_evidence()` 接受的安全 facts 附加到同轮 ContentIntent；原 batch、CallToolResult、参数、工具 schema 和工具对象不得进入最终 Persona Renderer。`epoch_current` 在生产接线中是必需门，缺失会返回 `GUARD_FAILED`。

## 5. 覆盖矩阵

首轮 15 项加 3 项发送隔离回归，共 `18/18`，覆盖：

- search/extract/batch 三种 brokered 形状；
- `_PermissionGuardedTool` 原对象执行，未 unwrap；
- Hook 篡改 copy 后实际执行仍使用重新生成实参；
- runtime 重名、非 allowlist、selection/runtime 伪来源；
- Handoff/background/MCP；
- schema 缺属性、缺 required 实参、错 type、错 enum、递归 extra-key hardening；
- request shape 被增加参数；
- 初始 stop/stale 与 Hook 后 stop/stale 零 executor；
- deadline 已过与真实外层 asyncio timeout；
- None、多结果、显式错误、空结果、裸结果与 executor 异常；
- 工具、测试 hook 以及“捕获发送异常后仍返回合法结果”的 handler 均不能触达真实 event，也不能把该次尝试伪装成成功；
- 成功 batch 经 `adapt_tool_call_results()` 与 `adapt_grounding_evidence()` 形成同轮 `GroundingFact`；
- query、正文、scope/sender/message/action/call ID 不进入 repr/trace。

相关工具链回归：

```text
test_tool_broker + test_tool_result + test_grounding_adapter
+ test_astrbot_tool_executor
Ran 50 tests
OK
```

`compileall` 与本层 `git diff --check` 通过。

P3-06 原子主链接线完成后曾统一达到 `628/628`；P3-07 首轮接线后的共享快照为 `659/659`。后者随后由独立审计发现 P3-07 覆盖不足，因此只作为执行器未回归的证据，不冒充后续阶段已验收。`compileall` 和仓库内 `git diff --check` 通过，尚未部署。

## 6. 尚未完成与风险

- 本层只执行公开只读取证。主人副作用能力虽然在 P3-01/P3-04 合同层可达，但必须另建代码拥有的参数适配器与 `ActionReceipt`，不能伪装成 `GroundingFact`，本执行器明确不执行。
- `_PermissionGuardedTool` 必须原样交给 AstrBot executor，才能保留 AstrBot 自身逐次权限检查；主链接线不得取得其内部 `_wrapped`。
- AnySearch 若改变 exact 名称、module source 或 JSON schema，会 fail closed；P10 插件共存验收必须用线上实际对象验证。
- AstrBot 的 hook 异常原生工具循环会记录后继续，本执行器选择更严格的 fail closed；主链应根据 typed status 让角色自然说明“这次没能可靠查证”，不能暴露内部错误。
- 本执行器已接入唯一 Action→Content→Knowledge→Broker→Grounding→Persona 热路径，最终 Renderer tools=0。下一入口仍是完成 P3-07 语义守卫，再进入 P3-08 主人逐 capability Action Adapter。
