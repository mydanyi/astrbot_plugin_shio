# P3-08E2B：Code-owned Live Runtime Conformance Collector

日期：2026-08-18（Asia/Hong_Kong）

## 1. 结果

本层把 P3-08D 的 `RuntimeConformanceEvidence` 从“公开 typed bool shape”收紧为只可由 exact runtime collector 签发的一次性 canonical authority。模型、`Mapping`、普通布尔、同字段 dataclass、`copy`、`dataclasses.replace`、跨 collector candidate、跨 operation、跨 binding/epoch 和 replay 都不能形成 `AdapterDraft`。

生产 audited allowlist 目前是不可变空 `MappingProxyType({})`，且不保留可被同模块变量再次修改的 backing dict。四项生产状态保持失败关闭：

| Operation | E2B 生产状态 | 原因 |
|---|---|---|
| `artifact_read_exact` | 无 candidate | live 4.27.2 source fingerprint 漂移；FileRead 还可能触发转换写文件，未满足纯文本只读合同 |
| `artifact_grep` | 无 candidate | 没有源侧 32 KiB cap、literal-only 与 hard deadline 的 live 证明 |
| `memory_write_literal` | 无 candidate | live LivingMemory 为 `legacy` scope，memorize 默认 `member`，没有 exact admin/private scope 证明 |
| `sandbox_shell_once` | 永远无 candidate | descriptor 与 collector 双重代码级 hard-disabled |

没有把当前现场硬写成 GREEN；没有接 `main.py`、Controller executor 或配置；没有访问/修改 FNOS，没有修改 AstrBot、LivingMemory 或其他插件，也没有 Git 写操作。

## 2. 先红后绿

实现前定向测试稳定失败：

```text
ImportError: cannot import name 'owner_action_runtime_collector'
Ran 1 test
FAILED (errors=1)
```

实现及 canonical adapter fixture 迁移后：

```text
python -m unittest astrbot_plugin_shio.tests.test_owner_action_runtime_collector
Ran 16 tests
OK

python -m unittest \
  astrbot_plugin_shio.tests.test_owner_action_runtime_collector \
  astrbot_plugin_shio.tests.test_owner_action_adapters
Ran 36 tests
OK

python -m unittest \
  astrbot_plugin_shio.tests.test_owner_action_runtime_collector \
  astrbot_plugin_shio.tests.test_owner_action_adapters \
  astrbot_plugin_shio.tests.test_owner_action_controller \
  astrbot_plugin_shio.tests.test_owner_action_contracts \
  astrbot_plugin_shio.tests.test_capability_policy
Ran 104 tests
OK

python -m unittest discover -s astrbot_plugin_shio/tests -p 'test_*.py'
Ran 834 tests
OK
```

限定 `compileall` 与全工作树 `git diff --check` 通过。第一次统一完整测试与 P3-08E3B 的 Controller/confirmation 合同迁移并发，当时有 16 个 `operation_confirmation_policy_mismatch`，全部位于 E3B 正在修改的 Controller fixture/合同，不在 E2B 文件；本层没有把该次并发结果冒充绿灯。E3B 冻结后已重新运行统一发现并得到上面的 `834/834`。

## 3. 生产 allowlist 与 code-owned resolver

- `RuntimeConformanceCollector(manager)` 的公开 `collect` 只有 `(binding, operation)`，没有 `allowlist`、evidence mapping、active/verified 布尔或 arbitrary fields。
- 生产 profile 保存在 module registry，不读取调用方事后写入的 `_specs`、`_source_file` 或 `_implementation_version` 属性。
- 生产 allowlist 是直接构造的 immutable empty `MappingProxyType({})`；源码和行为测试同时确认不存在 `_PRODUCTION_AUDITED_SPECS` backing dict。
- source file resolver 使用 `inspect.getsourcefile(type(exact_tool))`、真实文件、规范路径摘要与全文件 SHA-256；implementation version 只从 code-owned immutable version table 解析。当前 version table 同样为空，未来只增加 spec 而忘记增加 version pin 仍失败关闭。
- 私有测试 manifest seam 会验证 operation 对应 descriptor 的 exact source qualname、canonical schema digest 和 implementation version；它不修改生产 profile，也不能为 Shell 签 manifest。

## 4. Builtin exact object

Artifact read/grep candidate 必须满足：

1. exact manager 具有 `get_builtin_tool(exact_name)`；
2. 连续两次取得同一个 object identity；
3. manager 的 plugin/MCP inventory 中没有同名的另一个对象；
4. `name`、`active is True`、`is_background_task is False`、handler source、`handler is None` 全部精确；
5. class module/qualname、canonical schema SHA-256、implementation version、source path digest 和 source file fingerprint 与 code audit spec 完全一致。

active、schema、identity、version、qualname、source file 或 fingerprint 任一漂移均无 candidate。

## 5. LivingMemory exact wrapper

Memory candidate 不接受 raw `func_list` 工具直接执行。Collector 必须从 `get_full_tool_set()` 取得 exact：

```text
astrbot.core.provider.func_tool_manager._PermissionGuardedTool
```

并证明：

- full tool set 中 wrapper name 精确且 active/non-background；
- `wrapper._wrapped is raw_tool`，raw 同名对象唯一；
- permission probe 对 guest 返回拒绝、对 admin 返回允许，即 dashboard effective permission 为 `admin`；
- code-owned scope probe 精确返回 `private_user`；
- raw tool source/qualname/schema/version/file fingerprint 与 audit spec 一致。

unwrapped、member permission 或 `legacy` scope 均失败关闭。当前 FNOS 正好命中后两项，因此本层不会签发 live memory candidate。

## 6. Candidate、Proof、Evidence 与 exact tool lineage

```text
exact manager object
→ module-sealed RuntimeToolCandidate(binding + epoch + operation)
→ module-sealed RuntimeOperationProof
→ atomic candidate+proof claim
→ canonical RuntimeConformanceEvidence
→ AdapterDraft
→ private retained exact-tool opener（留给 E2C sealed executor）
```

- Candidate 的 public 字段不含 tool object；实际 exact tool/source tool 只存在 module-private weak material registry。
- Candidate 与 proof 使用固定锁序在同一个 critical section 中复核并同时消费。两个 candidate 并发争用同一 proof 时只有一个成功；失败方 candidate 不会被半消费，可配 fresh proof 继续。
- Binding 与 operation 在 collect、candidate、evidence claim 和 retained-object opener 均要求 exact `DecisionBinding` / `OwnerActionOperation` type；派生 binding 与 bare string 不能借字符串相等进入 authority 链。
- Adapter evidence issuer 不接受 kwargs 或字段 mapping，只接受 exact collector + candidate + proof；旧 `_issue_test_runtime_evidence(**values)` 后门已删除。
- Evidence issuance 后另有 evidence→candidate→exact retained tool 的私有 lineage。Manager 后续同名替换不会让 executor 重新按名选中替代对象；opener 只能返回首次 attested 的 exact identity。
- Opener 会再次核对 candidate snapshot、tool/source identity、name/active/background/handler/schema、wrapper relationship、source path digest 和 file fingerprint。原对象被修改即失败关闭。
- Opener 是 one-shot，并要求同 collector、同 binding、同 operation；copy、跨 collector、自洽字段伪造、缺 material 与 replay 均拒绝。

## 7. 篡改后的安全输出

`RuntimeToolCandidate`、`RuntimeOperationProof` 和 `RuntimeConformanceEvidence` 的 `trace_metadata()`/`repr()` 均先校验 canonical snapshot，再读取 operation、adapter/tool name 或其他字符串。`object.__setattr__` 篡改后：

- trace 直接以固定 reason code 失败；
- repr 只返回固定 `canonical=False, details_hidden=True`；
- 不回显攻击者写入的 Enum 替代值、路径、scope、工具名或自由文本。

## 8. 现场证据与不启用决定

只读现场证据已记录在 `P3-08B_RUNTIME_ADAPTER_AUDIT.md`：

- AstrBot 4.27.2 live `fs.py` SHA-256：`a710570b358ba2466f6f13c5bf2c3f6bfd60173c617aaa680efe36af33538ba7`，与 reviewed tag 不同；
- FileRead 支持转换路径且转换可能写入 `converted_files/.../text.txt`；
- Grep 在完整结果返回后才应用 result limit；
- LivingMemory 2.5.7 为 `legacy` scope，memorize 没有 exact admin permission 证明；
- Shell 继续 hard-off。

因此 production audited operation count 必须继续为 `0`。

## 9. Python 同进程边界

Python 私有名、module registry 和 exact identity 是防误接线、模型/Mapping 伪造、copy/replay 与可审计 TOCTOU 的代码合同，不是抵抗同一解释器内任意恶意插件的进程级安全边界。拥有任意 Python 执行能力的恶意插件可以反射私有对象；真正对抗该威胁需要独立进程/容器、最小 OS 权限和受控 IPC。E2B 没有把下划线私有性夸大成 sandbox，但也没有留下公开 kwargs 直签入口。

## 10. 下一接口

E2C sealed executor 只能从 canonical `AdapterDraft` 的私有 material 取得 evidence，再以 exact collector/binding/operation 调用 one-shot retained-object opener；不得按 tool name 重新查询 manager，不得使用 `FunctionToolExecutor` 通用 hook/Agent 路径。所有 adapter 在 executor、output guard、配置和生产接线完成前继续默认关闭。
