# P3-08E2A：Owner Artifact Output Guard

日期：2026-08-18（Asia/Hong_Kong）

## 1. 本层结果

本层新增一个未接生产的、代码拥有的 artifact 输出安全门。它只接受显式 dedicated root、显式 POSIX/Windows flavor、同一目标的 typed pre/post stat proof 和完整未截断 bytes；只有路径、身份和完整内容全部通过后，才签发 module-sealed `SafeArtifactOutput`。

本层没有接 `main.py`、owner controller、P3-08D adapter、AstrBot tool 或 FNOS。它不读取文件，也不把可公开构造的 stat shape 冒充 live filesystem authority。首版 live collector/executor 仍必须在后续层单独完成并现场证明。

核心边界：

| 边界 | 当前实现 |
|---|---|
| source 上限 | 最多 32,768 bytes；可配置值只能收窄 |
| visible 上限 | 最多 4,096 Unicode characters；完整 source 扫描后才可截断 |
| 扩展名 | exact built-in tuple，仅 `.md`、`.rst`、`.txt` |
| flavor | 只接受 exact `POSIX` 或 `WINDOWS`；不猜宿主系统，不接受 WSL 混用 |
| stat | exact typed pre/post snapshot，全部身份与安全字段必须一致 |
| 输出 | module-sealed exact object；公开对象不含正文或路径映射 |

## 2. 先红后绿

先新增专门测试，模块尚不存在时稳定红灯：

```text
ImportError: cannot import name 'owner_action_output_guard'
Ran 1 test
FAILED (errors=1)
```

完成最小实现及三轮独立审计反馈收口后：

```text
python -m unittest astrbot_plugin_shio.tests.test_owner_action_output_guard
Ran 46 tests
OK
```

当前相关 owner action 模块：

```text
python -m unittest \
  astrbot_plugin_shio.tests.test_owner_action_output_guard \
  astrbot_plugin_shio.tests.test_owner_action_adapters \
  astrbot_plugin_shio.tests.test_owner_action_controller
Ran 90 tests
OK
```

第二轮终审先新增 structured secret、compact root 和 policy subclass 红灯；修复前 37 项稳定产生 6 failures。修复后再加入低熵非空值与 null/redaction 语义边界，最终为 40/40。并发 D collector 迁移期间，相邻 adapters/controller 测试曾统一因 canonical issuer 旧新签名错配而失败；本层没有修改 collector、adapter 或 controller 测试来掩盖该过渡状态。

第三轮独立终审包含 11 个 probes、归并为 6 个根因。正式红测落地后，46 项稳定产生 5 failures、1 error；统一根修后为 46/46。`compileall` 对本层模块和专门测试通过。

共享工作树的并发 D/controller 迁移完成后已重新运行统一 discovery：841/841，0 failures、0 errors。本层没有越界修改这些并发文件。

## 3. Dedicated root 与路径门

`ArtifactOutputPolicy`（`core/owner_action_output_guard.py:386`）是 frozen/slotted typed policy。构造和每次 guard 消费都会重新验证，因而 `object.__setattr__` 或恶意 tuple/str 子类不能绕过：

- root 不能为空、盘根或宽泛 home/workspace/system/data/plugin/config/AstrBot 区域；
- root 与目标都拒绝 dot component、traversal、NUL/control/format 字符和 NFKC 歧义；
- credential/secret/token/cookie/session/auth/provider/config/key 及 compact 变体在 component 层拒绝；
- 只允许模块内 exact built-in extension tuple；外层必须是 exact tuple，每个元素必须是 exact built-in str，不能用自定义 equality/contains；
- POSIX 拒绝反斜线和双根；Windows 拒绝 UNC、device/extended/NT namespace、ADS、传统设备名和 console device name、尾随点/空格、非法 Win32 字符、跨盘和 flavor 混用；
- Windows 首版只接受 ASCII path。这样不会用 Python Unicode `casefold()` 伪装 NTFS/Win32 upcase 语义，也不会把多字符展开后的不同目录绑定成同一 digest。

路径 digest 由 `digest_artifact_path()`（line 312）按明确 flavor 产生。`_validate_target_path()`（line 595）同时比较 Windows drive 与 component prefix；字符串前缀相似不构成 under-root 证明。root 还会把每个 component 压成 ASCII compact form，因此 `AstrBotData/AstrBotStore` 一类无分隔拼接在 root 固定返回 `artifact_root_too_broad`，在 POSIX/Windows target component 固定返回 `artifact_path_sensitive`。

## 4. Typed pre/post identity proof

`ArtifactStatSnapshot`（line 453）只保存 digest、stat identity 和安全布尔值，不保存 raw path。`ArtifactIdentityProof`（line 538）固定一对 exact snapshot。`_validate_identity_proof()`（line 630）要求：

- policy root digest、target path digest、trusted-root marker 全部 exact 匹配；
- pre/post 的 root identity、device/file identity、size、mtime、link count 和所有安全位完全一致；
- source byte length 与 snapshot size 一致；
- regular file、under root、symlink/reparse/junction free、mount-escape free；
- `nlink == 1`、root only trusted writers、replacement protected。

任一 pre/post 差异、path copy、root swap、hardlink、symlink/reparse/junction 或 proof 字段被 `object.__setattr__` 改成错误类型都会失败关闭。snapshot/proof 的 repr 和 trace 只含有限计数/布尔状态，不含 path、root marker、file identity 或真实 ID。

重要边界：这些 typed records 当前仍是 shape，不是 live authority。后续 collector 必须从可信 root handle、组件级 lstat/fstat 和 read 前后同一打开对象产生它们；模型、P3-08D public `RuntimeEvidence`、普通插件或 tool 返回值不得自行声明这些布尔值。root marker 也必须由 code-owned collector/config authority 绑定，不能来自正文或模型参数。

## 5. 完整内容扫描

`guard_artifact_output()`（line 1090）先验证 source byte cap、路径和 identity proof，再让 `_decode_and_scan_source()`（line 843）处理完整 bytes。只有全量扫描通过，才取前 4,096 characters 作为 visible text；没有先截断后扫描，也没有命中后局部 redaction。

结构化键值扫描先在完整、最多 32,768-byte 的 source 上执行有限规范化：解 JSON `\\uXXXX` 和标准 escape、合并显式 line continuation、再 NFKC；随后用分离的 quoted/unquoted 结构 delimiter 识别 env/JSON/YAML/TOML 风格 assignment。key 会移除 Unicode/非既有字符集 separator 并压成 ASCII compact form，再按闭集 sensitive marker 判断。因此它不依赖同一行正则或 `\b`，并覆盖多行 delimiter/value、YAML folded/block value、JSON unicode-escaped/standard-escaped key、供应商前缀和任意 separator 组合。解析异常统一失败关闭。敏感 key 下的低熵非空值仍会拦截；只有确认为非值的未引用 null/unset/nil/none、明确 redacted/removed/masked 和全掩码可通过，quoted null 以及 YAML block/folded scalar 中的 null/unset 都是非空文本，不能冒充 semantic null。

扫描失败关闭：

- strict UTF-8；除 TAB/CR/LF 外拒绝 NUL、Unicode control/format/surrogate；
- PEM/PGP/private-key header，包括 `PRIVATE KEY` 后存在扩展 block suffix；
- Authorization/Proxy-Authorization/Cookie/Set-Cookie，包括 quoted JSON key 与 Basic/Bearer value；
- JWT，包括短合法 payload segment；
- 常见 cloud/GitHub/Slack/Google/Stripe/OpenAI 风格 key；
- quoted/unquoted API key、token、secret、password、DSN、credential URI；
- 长高熵 token；合法 UUID v1-v8 明确排除，避免普通 request reference 被误杀。

scanner 自身异常统一变成固定 `artifact_secret_scan_failed`，不回显异常正文。跨 visible/chunk 边界的 secret 仍会被发现，因为 scanner 始终接收完整原始 text。

所有确认的 secret detector 命中统一返回 `artifact_secret_detected`；扩展策略、compact AstrBot root/target、Win32 console reserved name 分别固定为 `artifact_extension_policy_invalid`、`artifact_root_too_broad`、`artifact_path_sensitive`、`artifact_path_reserved`。reason code 不携带 key、value、path 或 detector 原文。

## 6. Sealed `SafeArtifactOutput`

`SafeArtifactOutput`（line 891）使用一次性 issuer seal、weak canonical registry 和 module-private material。公开字段只有 source/visible counts、line count、完整 source digest、truncated 和 canonical 状态；没有 `text`、`raw`、`path` 或公开 mapping。

私有 `_open_safe_artifact_output()`（line 1069）只接受 registry 中同一个 exact canonical object，并只返回已经扫描且限长的 visible text。`_safe_output_matches_material()`（line 996）把所有公开 metadata 与 module-private expected tuple 再比一次，因此即使调用者用 `object.__setattr__` 绕过 frozen dataclass：

- `is_canonical` 立即变为 false；
- `trace_metadata()` 固定失败关闭，不输出被植入的 path/text；
- repr 只显示 invalid/canonical=false，不回显被植入值；
- private friend 拒绝打开。

`dataclasses.replace()`、直接构造、伪造或脱离 registry 的对象也不能获得 canonical status。

## 7. 独立对抗审计收口

第一轮独立只读审计实际复现了五个阻塞：compact sensitive filename/root、frozen output 的 `object.__setattr__` 篡改、恶意 tuple extension、Windows `casefold` 多字符碰撞、quoted JSON Authorization/短 JWT/短 password 漏扫。另发现普通 UUID 误杀和 Win32 非法字符未在词法门关闭。

上述样本现已全部变成专门回归。第二轮独立终审又发现三组阻塞：多行/折行/JSON unicode-escaped/vendor-prefixed structured key、compact AstrBot root，以及 exact tuple 内的 str subclass equality。第三轮独立终审执行 11 个 probes，归并出 6 个根因：structured separator/escape、YAML block scalar 语义、扩展 private-key header、UUID v6-v8、compact AstrBot target 和 Win32 console reserved name。正式红灯为 46 项中的 5 failures、1 error，统一根修后当前通过 46/46：

- compact `apikey/privatekey/authToken` 与 AstrBot/API-key root 变体拒绝；
- output metadata 篡改后 canonical/trace/friend 全部失败关闭，repr 无泄漏；
- extension tuple/element subclass 在 policy 构造处拒绝，其他 policy authority 字段也要求 exact built-in type；
- Windows 非 ASCII casefold-expanding path 和非法字符拒绝；
- quoted Authorization、短 JWT、短 password 拒绝；
- 普通规范 UUID 可正常通过。
- 多行、折行、JSON unicode-escaped、env/JSON/YAML vendor key 使用统一 closed scanner，并统一返回 `artifact_secret_detected`。
- compact AstrBot root 统一返回 `artifact_root_too_broad`。
- compact AstrBot target 在 POSIX/Windows 均返回 `artifact_path_sensitive`，root reason 不变。
- JSON Unicode/standard escape 与 YAML 非既有 separator 都进入同一 structured scanner。
- YAML block/folded scalar 的低熵非空值不再被误认成 semantic null。
- PGP/扩展 suffix private-key block header统一返回 `artifact_secret_detected`。
- UUID v6-v8 与既有合法 UUID 一样作为普通 reference 通过。
- Win32 console reserved name 固定返回 `artifact_path_reserved`。

第三轮修复后的再次独立终审仍由 root 在本层结束释放 slot 后另派；本报告不把 focused 46/46、related 90/90 或 full 841/841 冒充 blocker=0。

## 8. 发布边界

- 本层未接 production main/controller/adapter/runtime tool。
- 本层未访问 live FNOS，未修改 AstrBot、LivingMemory、AnySearch 或其他插件。
- 本层未执行 Git add/commit/push。
- live collector、handle-based TOCTOU proof、runtime timeout、direct-send/background/Handoff/MCP 隔离和 receipt 接线仍属于后续 E2/E3 层；缺任一项时 artifact adapter 必须保持关闭。
