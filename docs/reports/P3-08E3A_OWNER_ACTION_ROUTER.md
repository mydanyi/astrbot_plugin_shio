# P3-08E3A：主人动作 current-message code-owned Router

日期：2026-08-18（Asia/Hong_Kong）

## 结果

新增 `core/owner_action_router.py`，把主人当前轮的明确动作请求保守分类为一个闭集 `OwnerActionOperation`：

- `artifact_read_exact`；
- `artifact_grep`；
- `memory_write_literal`；
- `sandbox_shell_once`。

Router 不读取历史、记忆、引用正文或昵称，不运行模型，不解析/保存参数，不判断 adapter 是否可用，也不 claim `OWNER_ACTION` ticket。它只通过 long-lived `AcceptedTurnAuthority.context_for()` 取得该 exact ticket 的 canonical `AcceptedTurnContext`，再核对当前正文 SHA-256、可信主人 policy、private channel 与无结构化引用。

E3A hardening 将原先可直接构造的 `OwnerActionRouteDecision`
收紧为长生命周期 `OwnerActionRouter` 签发的 opaque handle：

- `route_owner_action(router, ticket, current_message=...)` 只接受显式的 long-lived Router；
- `inspect_route(decision, ticket=...)` 是 non-consuming exact-object 检查，Planner/Adapter 可多次只读；
- `claim_route(decision, ticket=...)` 是一次性下游权威转移，它不代替 Controller 对 `AcceptedTurnTicket` 的最终 claim；
- public constructor、`dataclasses.replace`、copy/deepcopy、cross-router、cross-authority、cross-ticket、replay 以及字段自洽伪造均失败关闭；
- 每个 record 绑定 exact ticket、canonical context、exact proposal object、binding identity、module seal、per-router issuer seal 与 registry identity；只比较公开字段不算权威。

`decision.proposal` 仍是无参数、无工具名的只读内容提案，可被多阶段读取，但它单独不构成授权。下游必须携带签发 Router、exact decision 和 exact ticket 进入 inspect/claim，不得通过重验 proposal 字段冒充 provenance。

## 根因与边界

旧 `ActionPlanner` 把主人 broad capability 复用成 `USE_TOOL`，而生产又没有传模型 suggestion；即使可达，也会与 AnySearch 取证混在同一动作类型。参数若交给模型生成，还会重新打开 target/权限/参数漂移。

另一个结构缺口是，原始 Router 返回普通 frozen dataclass。攻击者可以复制、`replace`或按公开字段重建一个看似自洽的 Proposal/Decision；如果 Controller 只复查 binding/capability/operation，就会把“字段相等”误当成“代码权威签发”。E3A hardening 用 module-owned identity + registry provenance 修复这个根因。

本层只解决“当前消息究竟是否明确选择了一个闭集动作”这一件事：

- Proposal 只有 binding、capability、operation、confidence 与闭集 reason；
- path、literal、memory 正文、command、tool name 和 generic Mapping 均不进入 Proposal；
- D adapter compiler 后续仍必须从同一 current message 独立重解析参数，并执行 config/runtime conformance；
- group owner 返回 `PRIVATE_REQUIRED`，普通群友和文本自称 owner 返回 `IDENTITY_REJECTED`；
- reply/quote、否定、教程、示例、假设和讨论语境不产生动作；
- 多个动作信号返回 `AMBIGUOUS`，不会猜一个执行。

因此 `MATCHED` 只表示“发现明确动作意图”，不表示已授权、已启用、已执行或成功。

## 红灯与验证

首次 Router 实现的红灯为：

```text
ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.owner_action_router'
```

E3A hardening 先增加 canonical provenance 回归，首次稳定红灯为：

```text
ImportError: cannot import name 'OwnerActionRouter'
```

实现后：

```text
python -m unittest astrbot_plugin_shio.tests.test_owner_action_router
Ran 15 tests
OK

python -m unittest \
  astrbot_plugin_shio.tests.test_owner_action_router \
  astrbot_plugin_shio.tests.test_owner_action_contracts \
  astrbot_plugin_shio.tests.test_capability_policy \
  astrbot_plugin_shio.tests.test_accepted_turn_authority \
  astrbot_plugin_shio.tests.test_owner_action_controller
Ran 101 tests
OK

python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py'
Ran 805 tests
OK
```

覆盖四种闭集 operation、参数不进入 proposal/repr/trace、群友与伪主人、群聊主人、引用消息、否定/讨论/示例、歧义、正文 digest 漂移、ticket copy，以及 public constructor/replace/copy/cross-router/cross-ticket/cross-authority/replay/自洽字段伪造。`compileall`通过；对本层 3 个 untracked 文件的 `git diff --no-index --check` 无空白诊断。测试不含真实用户 ID、真实路径或群聊正文。

## 未完成与下一入口

本层未修改 `ActionKind`、`ActionPlanner`、Controller、main、配置或热路径，也未启用任何 adapter。下一层必须新增独立 `EXECUTE_ACTION`，把 exact operation 纳入 action digest；随后才可把 Router 的 exact `MATCHED` handle 交给 D compiler、Controller 和 sealed executor。Controller 必须消费 Router 的 exact inspect/claim API，不能只验 Proposal 字段。Adapter 缺失/关闭时必须产生 typed 未执行结果，不能掉回模型假装成功。

本层没有 Git 写入、FNOS 修改或部署。
