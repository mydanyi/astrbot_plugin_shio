# P3-08D：Code-owned Owner Action Adapter Registry

日期：2026-08-18（Asia/Hong_Kong）

## 1. 本层结果

本层新增了纯编译器式的主人动作 adapter registry：模型只能提交同一 `DecisionBinding` 的 `OwnerActionProposal`，参数只能从当前 canonical message 提取；代码再用逐 operation 的固定合同和 typed runtime conformance evidence 生成 module-sealed `AdapterDraft`。本层不访问真实文件、不执行工具、不写记忆，也不修改 `main.py`、Controller、Planner、配置 schema、FNOS 或 Git。

四个闭集 operation：

| Operation | 固定 exact tool | 本层状态 |
|---|---|---|
| `artifact_read_exact` | `astrbot_file_read_tool` | compiler 完成；默认关闭；缺 candidate evidence 失败关闭 |
| `artifact_grep` | `astrbot_grep_tool` | compiler 完成；默认关闭；前置规模/输出证据缺失失败关闭 |
| `memory_write_literal` | `memorize_long_term_memory` | compiler 完成；默认关闭；只允许当前 owner 私聊 private/user scope token |
| `sandbox_shell_once` | `astrbot_execute_shell` | parser/conformance 红线完成；即使配置 true 和 evidence 完整仍代码级 hard-disabled |

## 2. 先红后绿

先新增 `tests/test_owner_action_adapters.py`，实现不存在时定向运行稳定失败：

```text
ImportError: cannot import name 'owner_action_adapters'
Ran 1 test
FAILED (errors=1)
```

实现后：

```text
python -m unittest astrbot_plugin_shio.tests.test_owner_action_adapters
Ran 20 tests
OK

python -m unittest \
  astrbot_plugin_shio.tests.test_owner_action_adapters \
  astrbot_plugin_shio.tests.test_owner_action_contracts
Ran 41 tests
OK
```

`compileall` 对新增模块与测试通过。第一次共享工作树完整发现运行到 729 项时，P3-08C Controller 正在并发迁移其测试 helper，出现 16 个旧接口过渡错误（旧 `adapter_attestations`、`parameter_digest` 调用面和临时 parameter record 名称）；这些错误不在本层三个文件，本层没有越界修改 Controller，也没有把该次并发运行冒充完整绿灯。

P3-08C 收口并单独通过 18/18 后，本层重新运行统一发现：

```text
python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py'
Ran 729 tests
OK
```

因此最终交付基线是 729/729；上面的并发失败只保留为共享工作树测试纪律记录，不是当前未解决错误。

## 3. 固定 registry 与公开入口

- `AdapterDescriptor` 固定 adapter ID/version、capability、operation、side effect、confirmation、exact tool、plugin/source、implementation/interface version、schema digest、call budget 与 private-owner-only。
- `OWNER_ACTION_ADAPTER_REGISTRY` 是只读 `MappingProxyType`，只含四个 closed operation。
- `AdapterConfig` 四个 enabled 默认全部为 `False`；没有 generic owner tool list，也没有 argument mapping。
- 唯一公开 compiler 是：

```python
compile_owner_action_draft(
    proposal,
    current_message,
    config,
    runtime_evidence=None,
)
```

签名没有 history、reference、memory、nickname、model arguments、tool name、source、scope、deadline、budget 或 arbitrary mapping。`current_message` 的 SHA-256 必须等于 proposal binding 的 `current_content_digest`。

`AdapterDraft` 只公开 closed metadata、`parameter_digest`、`source_interface_digest` 和 canonical 状态。私有参数保存在四个 typed record 之一；module-private friend `_open_adapter_draft(draft)` 只接受 exact canonical draft，并返回同一个 `_AdapterMaterial` identity，供后续 Controller 私存。它不返回通用 Mapping，且不在 `__all__`。

Draft 使用一次性 issuer seal、weak canonical registry 和 exact object identity。`dataclasses.replace()`、同型伪造或脱离 module registry 的对象不能成为 canonical draft。

## 4. Artifact read：路径、类型与 TOCTOU

`artifact_read_exact` 只接受当前消息中唯一的显式 `path="..."`，以及闭集十进制 `offset`/`limit`。默认与上限由代码固定；未知 slot、第二条路径、负数或越界均拒绝。

路径由配置显式指定单一 flavor：Windows、POSIX 或 WSL-POSIX。实现不根据 Codex/宿主系统猜运行时命名空间，并拒绝：

- `..` traversal、root escape；
- UNC、extended/device/NT namespace；
- Windows ADS、保留设备名、drive-relative、尾随点/空格；
- NUL/control、环境变量、`~`；
- Windows/POSIX 混用以及 WSL `/mnt/<drive>` 自动跨命名空间；
- PDF/docx/epub、图片、二进制、可执行文件、无扩展或不在纯文本扩展闭集的目标。

本模块不做假 IO 检查。Draft 必须附带同 revision、同 root/path digest 的 `ArtifactPathConformance`，并证明：目标存在、realpath 在 root、每组件 lstat、无 symlink/reparse/mount escape、普通文件 hardlink 唯一、root 仅可信写者、替换受控、纯文本 magic、extension tree 受控，以及非空 preflight token。缺少任一 symlink/reparse/TOCTOU 证据即失败关闭。root/path/preflight digest 同时进入私有 parameter digest 和 source-interface attestation digest。

## 5. Artifact grep：literal-only 与前置预算

`artifact_grep` 只接受当前消息中唯一 `path="..."` 与 `literal="..."`。不开放 `pattern`、regex、glob、context 或 result-limit 参数。用户 literal 由代码 `re.escape()`，再与代码固定值一起进入私有 record：

- 固定纯文本 glob；
- 前后各 2 行；
- 50 个结果；
- 最多 2,000 个文件；
- 最多 64 MiB corpus；
- 源端最多 32 KiB 输出。

Runtime evidence 必须证明 exact `rg`、literal escape、argv/POSIX shell contract、无 fallback、调用前 corpus budget、硬 deadline 和源端 output cap。只在工具返回后截断不算 conformance。

## 6. Memory write：当前 literal 与主体/scope

`memory_write_literal` 只接受当前消息开头的明确 `记住：<literal>`。正文只能提供 literal；以下权威字段不能来自正文：subject、sender、owner、user ID、scope、global/group/shared alias。`替他人/给他人保存`、引用、否定、教程或示例全部拒绝。

代码固定工具字段：

```text
topics=()
key_facts=()
sentiment=neutral
importance=0.6
reason=owner_explicit_literal
```

Controller/runtime 必须提供当前可信 owner 的 expected private/user scope token；LivingMemory 实际 resolved token 必须相同。还必须证明 private chat、current owner subject、identity 未降级、global/shared alias 关闭、AstrBot admin 权限和 permission wrapper 保留。Scope token 与 literal 只以隐藏值/摘要进入 typed record，不出现在 repr 或 trace。

## 7. Shell：解析存在，执行仍关闭

Shell 只识别当前消息中唯一 fenced shell block，并要求围栏外有明确“执行/运行/run/execute”动词。语言标签必须与配置 shell family 完全一致；多 block、跨 shell、`nohup`、`setsid`、`disown`、单 `&`、`Start-Process`、Windows `start` 和 WSL 跳转均拒绝。

即使 `sandbox_shell_once_enabled=True`，还必须先证明：

- exact `sandbox` runtime 与固定 booter/profile/shell；
- per-action ephemeral、per-principal isolation、固定 cwd；
- 完整 process-tree kill；
- network disabled；
- CPU/memory/process/disk/source-output 限额；
- 无 host mount、credential、Docker socket、device；
- guest/其他 Agent 路径不可达。

旧本地 4.26.7 interface evidence 在 implementation-version 门直接失败。即使所有上述 evidence 完整，本层仍返回 `adapter_hard_disabled`；没有任何 shell draft 可执行。

## 8. Runtime evidence 的权限边界（E2B 收紧补记）

P3-08D 初版的 `RuntimeConformanceEvidence` 只是 typed shape。P3-08E2B 已把它收紧为 exact runtime collector 才能签发的一次性 canonical authority：公开构造、布尔/Mapping、自洽字段伪造、copy/replace、cross collector/operation/binding 和 replay 都不能形成 Draft。完整 collector、生产空 allowlist 与 retained exact-tool lineage 见 `P3-08E2B_RUNTIME_COLLECTOR.md`。

registry 中的 4.27.2/2.5.7 version pin 与旧快照 schema digest 仍不能写成“当前 FNOS 已通过”。生产 audited allowlist 当前不可变且为空；四项 adapter 全部保持关闭，Shell 额外 hard-disabled。

因此后续启用任一 adapter 前仍必须由 code-owned Controller/executor 完成：

1. canonical runtime collector（E2B 已完成，生产 allowlist 为空）；
2. exact tool object identity、同名冲突、active/type/source/handler/helper/schema 探针；
3. permission wrapper、Handoff/MCP/background/direct-send 与 global exposure 探针；
4. candidate 容器的真实 path/scope/output/timeout/return conformance；
5. 将 exact `AdapterDraft`、`_AdapterMaterial` 与 runtime attestation identity 私存并贯穿 Request/claim/Receipt。

`AdapterConfig` 也只是 compiler gate，不等于最终生产 `_conf_schema.json`：生产仍需 global off、逐 adapter enabled、timeout/output、confirmation TTL、idempotency/receipt ledger、root writer policy 和 candidate attestation pin。群聊 owner/private gating 属于 Controller；本模块不会仅凭消息文字判定主人或私聊。

## 9. 隐私与未完成边界

- Draft、参数 record、config、path/scope/sandbox evidence 的 repr/trace 只含闭集值、布尔、计数或“has digest”；不回显路径、命令、memory literal、scope token 或原始 tool result。
- 本层没有公开 argument materializer；没有文件访问、搜索、Shell、LivingMemory 调用或副作用。
- 本层没有形成 `OwnerActionRequest`、Pending/claim、executor、`ActionReceipt/ActionOutput` 或 Renderer 输入。这些仍属于 P3-08C/E 及后续原子热路径接线。
- 四个 adapter 默认均关闭；Shell 额外 hard-disabled。没有 candidate runtime conformance 和生产 schema 前，不得标成可部署或“主人完整工具已可用”。

## 10. 下一接口

Controller 应只接收 exact canonical `AdapterDraft`，通过 private friend 取得并私存同一 `_AdapterMaterial` identity；不得由调用方分别传 parameter record、parameter digest 或 attestation。Request 使用 draft 的 closed descriptor、`parameter_digest` 与 `source_interface_digest`，后续 executor 再按 exact typed record 实现逐 adapter 调用；最终 Persona Renderer 继续保持零工具，ActionReceipt 不得转换为 GroundingFact。
