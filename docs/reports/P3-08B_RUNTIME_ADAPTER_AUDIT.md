# P3-08B：首批 Owner Runtime Adapter 静态审计

审计日期：2026-08-18（Asia/Hong_Kong）

## 1. 范围、证据边界与结论

本报告只审计以下四个首批 adapter 候选：

- `artifact_read_exact`
- `artifact_grep`
- `memory_write_literal`
- `sandbox_shell_once`

本轮只读取星汐共享工作树、仓库内 vendor 快照和既有报告。只新增本报告；没有修改代码、测试、计划、配置或其他文档，没有执行 Git 写操作，没有连接、检查、部署或改变 FNOS。

仓库内实际存在的源码只有：

- AstrBot 4.26.7，vendor commit `fed2984`，版本见 `../../work/vendor/AstrBot-v4.26.7/pyproject.toml:1-7`；
- LivingMemory 2.5.3，vendor commit `b613b8e`，版本见 `../../work/vendor/LivingMemory/metadata.yaml:1-5`。

既有生产盘点报告曾记录 AstrBot 4.27.2 与 LivingMemory 2.5.7，见 `docs/reports/P0R-03_ASTRBOT_PLUGIN_MULTIMODAL_INTEGRATION_AUDIT.md:5,23-26`。这只是此前快照，不是本轮 live 结果。本地没有找到对应 4.27.2/2.5.7 的完整运行源码，所以本文给出的类、schema、handler 和 helper 哈希只能作为实现依据与离线 fixture，不能作为当前 FNOS conformance 证明。

### 1.1 后续 FNOS 只读补充（2026-08-18）

主任务随后通过已配置的 FNOS SSH 入口只读检查了 `astrbot` 容器，未修改容器、配置或插件。现场确认 AstrBot 包版本为 `4.27.2`，并取得以下实际源文件 SHA256：

| 线上文件 | SHA256 |
| --- | --- |
| `/AstrBot/astrbot/core/tools/computer_tools/fs.py` | `a710570b358ba2466f6f13c5bf2c3f6bfd60173c617aaa680efe36af33538ba7` |
| `/AstrBot/astrbot/core/provider/func_tool_manager.py` | `b393878f4d8cc6c477412d2538e29cbd02ca2b30b94421304255e6455e18cb48` |
| `/AstrBot/astrbot/core/agent/tool_executor.py` | `f061a0541fc968a1952d0506d5ea5e2fe96f28caa07d0ee625a38f9d5cad9d7a` |
| `/AstrBot/astrbot/core/astr_agent_tool_exec.py` | `98fb3076669e540ec7fa6c633fa08f32717958831e372a7a79bede8e21855bb5` |

现场 `FileReadTool` 明确宣称支持 image/PDF/docx/epub，并把读取交给 `read_file_tool_result()`；当前 `file_read_utils.py` 的转换分支会在 workspace 的 `converted_files/.../text.txt` 写入转换结果。因此它不能仅凭“read”名称归类为无副作用读取，首版仍只能接受星汐独立证明为普通 UTF-8 文本的目标。现场 `GrepTool` 仍在 `sb.fs.search_files()` 返回完整 `content` 后才应用 `result_limit`，没有 32 KiB 源侧输出上限，也没有证明 literal-only 与 hard deadline。两项都不能因为版本号和 schema 相同就自动进入 allowlist。

此前同轮现场盘点还确认 LivingMemory 2.5.7 采用 `legacy` memory scope，且 memorize 工具缺少已证明的 exact admin permission；因此 `memory_write_literal` 同样继续失败关闭。上述 live 证据只把“旧快照未知”推进为“当前候选明确不满足启用门”，没有签发 runtime conformance，也没有启用任何 adapter。

结论：

| Adapter | 静态候选 | 当前状态 |
| --- | --- | --- |
| `artifact_read_exact` | AstrBot builtin `astrbot_file_read_tool` | 可实现；仅纯文本、受信 root、private owner；candidate conformance 前关闭 |
| `artifact_grep` | AstrBot builtin `astrbot_grep_tool` | 可实现红灯；当前存在调用后才截断输出问题，未证明前置边界前关闭 |
| `memory_write_literal` | LivingMemory `memorize_long_term_memory` | 可实现；仅可信 owner 私有 scope，必须保留 permission wrapper；candidate conformance 前关闭 |
| `sandbox_shell_once` | AstrBot builtin `astrbot_execute_shell` | 当前接口无法证明 cwd、主体隔离和进程终止，保持 hard-disabled |

四项首批 rollout 均固定为可信 owner 的私聊直接请求。群聊即使发送者是 owner 也不执行，以免文件正文、搜索命中、命令输出或其他私有结果被整个群看见。不得用 direct-send 绕过正常发送链私发结果。

## 2. AstrBot 工具对象与选择边界

AstrBot 4.26.7 的 `FunctionTool` 可以返回字符串，也可以返回 MCP `CallToolResult`，并具有 `active` 与 `is_background_task` 状态，见 `../../work/vendor/AstrBot-v4.26.7/astrbot/core/agent/tool.py:15-16,40-74`。`ContextWrapper` 自带完整消息列表和默认 120 秒 tool timeout，见 `../../work/vendor/AstrBot-v4.26.7/astrbot/core/agent/run_context.py:12-20`；`AstrAgentContext` 又包含完整 Star Context 与 event，见 `../../work/vendor/AstrBot-v4.26.7/astrbot/core/astr_agent_context.py:9-18`。首批 executor 不能把这些 live 对象原样交给候选工具。

Builtin 选择必须使用 canonical builtin registry 与 `get_builtin_tool()`，见：

- `../../work/vendor/AstrBot-v4.26.7/astrbot/core/tools/registry.py:201-280`
- `../../work/vendor/AstrBot-v4.26.7/astrbot/core/provider/func_tool_manager.py:417-445`

不能只调用 `get_func(name)`：它优先返回同名 active plugin/MCP 工具，可能遮蔽 builtin，见 `func_tool_manager.py:397-415`。每次 conformance 都要同时枚举 non-builtin 列表，发现同名对象、alias 或来源冲突立即失败关闭。

LivingMemory 属于 non-builtin。AstrBot 通过 `_PermissionGuardedTool` 包装第三方工具并在每次调用前检查 dashboard permission，见 `func_tool_manager.py:213-284,496-513`。Executor 必须调用 canonical wrapper，不能解包或直接执行 raw LivingMemory 对象。

所有四项都必须使用冻结的最小 event/context proxy：只暴露当前可信 principal、platform、私聊 UMO、真实 AstrBot admin 状态、读取 candidate config 所需的窄接口；消息历史为空。`send`、`send_*`、设置 event result、stop 和其他直接发送/状态改变入口全部拒绝，任何尝试都产生 `DIRECT_SEND_FORBIDDEN`。不运行通用 tool hook，也不把原 live event 传入工具。

## 3. `artifact_read_exact`

### 3.1 精确静态合同

- exact tool：`astrbot_file_read_tool`
- class：`astrbot.core.tools.computer_tools.fs.FileReadTool`
- source：`../../work/vendor/AstrBot-v4.26.7/astrbot/core/tools/computer_tools/fs.py:295-389`
- capability catalog：`core/capability_policy.py:398-405`
- runtime config gate：`provider_settings.computer_use_runtime in {local, sandbox}`，见 `fs.py:65-70`
- parameters：必填 `path: string`；可选 `offset: integer >= 0`、`limit: integer >= 1`，见 `fs.py:300-321`
- context：读取 UMO、角色、当前配置、workspace/DB 与 booter，见 `fs.py:334-384`
- direct-send：当前 handler 没有
- background：继承 `FunctionTool.is_background_task=False`
- return：普通 `str`，或图片场景的 MCP `CallToolResult`

旧实现的 owner/admin local 路径并不受 builtin root 限制；`_is_restricted_env()` 只在 local、require-admin 且当前 event 非 admin 时返回 true，见 `fs.py:151-159`。sandbox 更不会经过该 root 检查。因此 Shio adapter 必须拥有独立 root authority，不能把 AstrBot 自带检查当成 owner action 边界。

### 3.2 隐藏写入与返回类型阻断

底层 reader 支持 text、image、PDF、docx、epub。图片会返回 MCP image result，见 `../../work/vendor/AstrBot-v4.26.7/astrbot/core/computer/file_read_utils.py:645-712`。本地文档解析结果过大时还会在 workspace 创建 `converted_files/.../text.txt`，见 `file_read_utils.py:531-549,599-642`。

所以 `artifact_read_exact` 第一版必须在调用前完成文件类型与 magic sniff，只接受已存在的普通文本文件；PDF、docx、epub、图片、其他二进制、特殊设备和未知类型一律拒绝。返回值必须是单个有界字符串；MCP result、`MessageEventResult`、`None`、多结果或错误字符串均失败关闭。

AstrBot 自带上限是输出 128 KiB、25,000 tokens，完整文本文件 256 KiB，见 `file_read_utils.py:28-31,480-513`。这些值仍过宽，adapter 必须使用独立、更小的 `max_target_bytes`、`max_output_bytes`、`max_lines`、`max_offset` 和 deadline，并在返回后再次验证 UTF-8 字节数。

### 3.3 Code-owned 参数

只允许从当前可信 owner 的私聊直接消息中提取一个明确分隔的路径，或使用当前轮已经绑定的单个结构化附件。禁止从引用正文、历史、LivingMemory、人格 Prompt、模型建议、工具结果或示例取得路径。

`offset` 与 `limit` 只能来自当前消息中闭集、无符号十进制 slot；缺省值与上限由 adapter 配置决定。最终传给工具的 mapping 固定为 `path/offset/limit`，额外字段拒绝。确认模式为当前轮明确请求，不需要第二轮确认。

## 4. `artifact_grep`

### 4.1 精确静态合同

- exact tool：`astrbot_grep_tool`
- class：`astrbot.core.tools.computer_tools.fs.GrepTool`
- source：`../../work/vendor/AstrBot-v4.26.7/astrbot/core/tools/computer_tools/fs.py:555-788`
- capability catalog：`core/capability_policy.py:398-405`
- runtime config gate：与 FileRead 相同
- parameters：必填 `pattern`；可选 `path`、`glob`、`-A`、`-B`、`-C`、`result_limit`，见 `fs.py:560-599`
- handler：接受 `**kwargs`，见 `fs.py:719-727`；adapter 必须拒绝 schema 外字段
- direct-send：当前 handler 没有
- background：继承 false；底层搜索为 awaited 调用
- return：单个字符串

### 4.2 搜索实现与边界缺口

local Python 3.14 路径使用 argv 形式启动 `rg` 并固定 30 秒 timeout；低于 3.14 时走 `python-ripgrep`，没有显式 subprocess timeout，见 `../../work/vendor/AstrBot-v4.26.7/astrbot/core/computer/booters/local.py:247-331`。

sandbox 路径使用 POSIX `shlex.join` 生成 `rg`/`grep` shell 命令，并在没有 `rg` 时降级到 `grep`，见 `../../work/vendor/AstrBot-v4.26.7/astrbot/core/computer/booters/shipyard_search_file_util.py:30-147`。这不是 Windows cmd 或 PowerShell quoting 合同；candidate shell 不是已证明的 POSIX shell 时必须拒绝。

`result_limit` 在 `sb.fs.search_files()` 已返回完整字符串后才生效，见 `fs.py:759-783`；底层 `capture_output`/shell result 也已经聚合完整 stdout。因此 `max_results` 不能防止前置内存放大。

首版只提供 literal search：当前消息给出单个明确 literal，adapter 负责生成 runtime 已验证的转义 pattern；不开放任意 regex。`glob`、上下文行数和 result limit 均由配置或闭集 allowlist 生成。必须要求：

- exact runtime 已证明使用 `rg`，不允许不透明 fallback；
- 目标 root 在调用前可计算并满足总文件数、总字节数上限；
- 搜索可被硬 deadline 终止；
- 源端或 runtime profile 有输出上限，而不是只在返回后截断。

任一条件无法证明时，`artifact_grep` 保持关闭。

## 5. `memory_write_literal`

### 5.1 精确静态合同

- exact tool：`memorize_long_term_memory`
- class：`astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.MemoryMemorizeTool`
- source：`../../work/vendor/LivingMemory/core/tools/memory_memorize_tool.py:33-182`
- capability catalog：`core/capability_policy.py:393-396`
- registration gate：`agent_tools.enable_memorize_tool`，默认 false，见 `../../work/vendor/LivingMemory/core/base/config_validator.py:135-143`
- registration：核心组件就绪后才调用 `context.add_llm_tools()`；开关变化需要重载，见 `../../work/vendor/LivingMemory/main.py:226-256`
- parameters：必填 `memory`；可选 `topics`、`key_facts`、`sentiment`、`importance`、`reason`，见 `memory_memorize_tool.py:50-88`
- context：从 proxy event 读取 access、UMO、sender/platform、message type，并使用插件持有的 Context、memory engine、memory processor
- direct-send/background：当前 handler 均无
- return：JSON 字符串，成功结果包含 memory ID、content、importance、session scope 和 persona，见 `memory_memorize_tool.py:168-177`

### 5.2 Subject 与 scope 必须由代码决定

工具使用 `event.unified_msg_origin`，再调用 `resolve_memory_scope(config, event)` 与 `get_persona_id()`，见 `memory_memorize_tool.py:121-166`。LivingMemory 的 scope 可以按配置落入 session、user 或 global；user identity 还可受配置 aliases 影响，见：

- `../../work/vendor/LivingMemory/core/memory_scope.py:64-82`
- `../../work/vendor/LivingMemory/core/memory_scope.py:98-156`

首版只接受可信 owner 的私聊 direct turn。Adapter 在副作用前用冻结 principal/current event 独立计算 `expected_scope` 和 expected persona policy，并要求插件当前配置解析结果逐字一致。global、群/共享 scope、缺失 sender/platform、nickname fallback、未知 alias、identity degraded 全部拒绝。

正文中的昵称、用户 ID、自称、引用、角色扮演或“替某人保存”不能改变 subject/scope。正文只提供要保存的 literal content；存储主体始终是当前可信 principal。Adapter 固定：

- `topics=[]`
- `key_facts=[]`
- `sentiment=neutral`
- `importance` 为代码配置的闭集值
- `reason` 为代码拥有的 reason code 或空字符串

工具会 trim summary，并通过 MemoryProcessor 构造 canonical content，见 `../../work/vendor/LivingMemory/core/processors/memory_processor.py:811-895,920-943`。Receipt 只能说明 LivingMemory 接受了一条当前 owner 私有记录，不能回显正文。

### 5.3 Permission、幂等与效果不确定

LivingMemory 开启 memorize tool 后会把它注册到 AstrBot 全局工具管理器。AstrBot 4.26.7 对 non-builtin 的默认 permission 是 `member`，见 `func_tool_manager.py:451-481`。Candidate conformance 必须证明 exact `memorize_long_term_memory` 的 dashboard permission 为 `admin`、当前可信 owner 同时是 AstrBot 实际 admin，并且普通用户无法从星汐以外的 Agent 路径调用它。

MemoryEngine 是多阶段写入。文档索引完成后，图索引可以失败并被标记为待修复，但调用最终仍可能返回 memory ID，见 `../../work/vendor/LivingMemory/core/managers/memory_engine.py:1230-1376`。因此：

- 调用前必须原子 claim idempotency key；
- timeout、取消或断连后状态为 `effect_unknown`，绝不自动重试；
- 成功 receipt 只表示主记录被接受，不声称所有索引均健康或已经可召回；
- raw JSON 中的 content、scope、persona 和 ID 只用于内部校验，随后缩减为 opaque、脱敏 receipt。

当前 helper 会记录 UMO/persona 调试信息，见 `../../work/vendor/LivingMemory/core/utils/__init__.py:175-233`；工具异常也会写 traceback，见 `memory_memorize_tool.py:178-182`。Runtime logging conformance 必须确认生产日志不会把真实 ID、正文、内部路径或异常敏感内容带入星汐 product trace；不能把 raw tool result 继续传给 Renderer。

## 6. `sandbox_shell_once`

### 6.1 精确静态合同

- exact tool：`astrbot_execute_shell`
- class：`astrbot.core.tools.computer_tools.shell.ExecuteShellTool`
- source：`../../work/vendor/AstrBot-v4.26.7/astrbot/core/tools/computer_tools/shell.py:54-163`
- capability catalog：`core/capability_policy.py:417-424`
- runtime config gate：`computer_use_runtime in {local, sandbox}`，见 `shell.py:23-25`
- parameters：必填 `command`；可选 `background`、`timeout`、`env`，见 `shell.py:59-86`
- context：admin guard、candidate config、UMO、workspace/booter
- direct-send：当前 handler 没有
- `is_background_task`：false，但参数可要求 background，literal command 也可自行 detach
- return：底层 result 的 JSON 字符串，异常时返回普通错误字符串

Adapter 必须固定 `background=false`、`env={}`、正整数 timeout，并对单个 JSON result 做 closed-schema 验证。`command` 字段或原始 stdout/stderr 不进入 repr/trace；输出只在 private owner 当前轮按字节上限呈现。

### 6.2 当前 hard-disabled 的结构原因

第一，sandbox 调用将 `cwd=None` 传给 `sb.shell.exec()`；只有 local runtime 才设置 workspace cwd，见 `shell.py:99-131`。Schema 描述反而建议把 `cd working_dir &&` 拼入 command，见 `shell.py:63-66`，但 adapter 合同禁止改写用户明确引用的 literal command。当前接口无法同时证明 cwd 和 command digest。

第二，`get_booter()` 按 UMO 缓存 sandbox，见 `../../work/vendor/AstrBot-v4.26.7/astrbot/core/computer/computer_client.py:542-657`。它不是 per-action、per-principal ephemeral sandbox；群聊或其他共用 UMO 的路径存在状态继承风险。

第三，即使 adapter 传 `background=false`，命令自身仍可用 `nohup`、`setsid`、`disown`、Windows `start`/`Start-Process` 或尾随 `&` 脱离。当前 `_is_self_detached_command()` 只用于避免二次 background 包装，不会阻止这些命令，见 `shell.py:110-130,143-163`。危险字符串列表不能可靠解析多种 shell，也不能代替进程树监管和 sandbox 销毁。

所以当前 builtin 必须 hard-disabled。重新评估至少需要 candidate runtime 同时证明：

1. exact runtime 值为 `sandbox`，不能把其他非 local 字符串当作 sandbox；
2. exact booter class/profile 与 shell family 固定；
3. 独立 `cwd` 参数或不可变默认 cwd，且等于 adapter 配置 root；
4. per-action/per-principal sandbox，执行后销毁并 kill 完整进程树；
5. 无宿主挂载、凭据、Docker socket、设备或跨 namespace 入口；
6. 网络策略、CPU、内存、进程、磁盘、timeout 和 stdout/stderr 均在源端受限；
7. timeout/断连后不自动重试，receipt 标为 partial/unknown；
8. 普通用户和其他 Agent 路径无法获得该工具。

命令只接受当前 private owner 消息中唯一、明确、语言标签与配置 shell family 一致的 fenced block。Adapter 去除语法围栏后保留内部原始字节并签 digest；不补全、不插入 `cd`、不插值、不转义到另一种 shell、不把 Windows 路径自动转换成 WSL 路径，也不允许用 `wsl.exe` 或 UNC 绕过 sandbox namespace。

Shell 固定 mandatory two-turn confirmation。Pending proof 至少绑定 principal、private scope、origin/message revision、adapter/operation、shell family、booter profile、working root digest、literal command digest、deadline、expiry 和 single-use state。

## 7. Windows、WSL、symlink 与 TOCTOU 路径合同

Artifact adapter 必须由配置显式声明 `path_flavor` 与 runtime namespace，例如 Windows、POSIX 或 WSL-POSIX；不得根据运行 Codex 的桌面系统推断 AstrBot candidate 的路径语义。

共同规则：

1. 拒绝 NUL、控制字符、空路径、环境变量、`~` 展开、任意 `..` 组件和混合/未知 path flavor。
2. Windows 拒绝 UNC、extended/device namespace、NT object path、WSL UNC、drive-relative path、ADS、保留设备名以及尾随空格或点。
3. WSL 只接受显式配置的 POSIX root；不自动在 Windows drive、`/mnt/...` 和 WSL UNC 之间转换。
4. 先用指定 flavor 做 lexical 校验，再解析已存在的目标；root membership 必须使用真实规范化路径和对应平台的大小写语义，不能用字符串前缀。
5. 对 root 到 target 的每个组件执行 lstat/reparse 检查；拒绝 symlink、junction、mount/reparse escape。普通文件额外拒绝多 hardlink。
6. 目标必须在调用前存在；FileRead 只接受 regular text file，Grep 只接受受信 regular directory/file tree。

旧 AstrBot 使用 host `pathlib.Path` 与 `resolve(strict=False)`，见 `../../work/vendor/AstrBot-v4.26.7/astrbot/core/tools/computer_tools/fs.py:162-217`；hardlink 检查只在 restricted local member 路径启用，见 `fs.py:220-282`。这不足以保护 owner action。

即使 adapter 在调用前完成检查，底层工具仍会按字符串路径重新打开目标，存在检查后替换的 TOCTOU。首版只能允许不受非可信主体写入的静态 root。若目标 root 可被其他用户、群成员、插件或外部进程同时改变，必须等待 secure-open/handle 型接口，不能靠重复 `resolve()` 宣称安全。

## 8. 建议的逐 Adapter 配置

配置必须逐 adapter 独立，不增加 generic owner tool list 或 arbitrary arguments mapping。

| Adapter | 必需配置 |
| --- | --- |
| `artifact_read_exact` | 独立 enabled；runtime/path flavor；root ID 与 canonical root；可信写者策略；plain-text allowlist；target/output/line/path 上限；deadline；`private_owner_only=true` |
| `artifact_grep` | 上述 root 配置；`literal_only=true`；require-rg；pattern/glob/context/result 上限；tree files/bytes 上限；源端 output cap；deadline；`private_owner_only=true` |
| `memory_write_literal` | 独立 enabled；exact plugin/tool/version pin；private scope mode；max content bytes；固定 importance；timeout；idempotency TTL；dashboard permission pin；`private_owner_only=true` |
| `sandbox_shell_once` | enabled 默认且当前固定 false；exact booter/profile/shell；ephemeral policy；root/cwd；resource/network/output limits；confirmation TTL；idempotency；`private_owner_only=true` |

Artifact scoped read 与 private memory write可由当前轮明确请求触发；Shell 必须二次确认。共享/global memory write 不属于首批，不能通过修改确认级别临时开放。

## 9. Runtime conformance 指纹

每次 candidate 启动或重载后，executor 必须重新构造 conformance descriptor 并签 digest，至少包括：

- AstrBot version/build、Python version、OS/path flavor；
- plugin ID/version/activation state；
- exact tool name、canonical object identity、active state；
- object type与 class module/qualname；
- `handler_module_path`、handler/call override digest；
- 完整 canonical Draft 2020-12 schema digest；
- 所有影响 side effect 的 helper/module digest；
- `is_background_task` 与 tool-specific background 参数；
- source config gate 与 dashboard permission；
- runtime/booter/profile/shell/cwd/namespace/resource policy；
- narrow context 所需的方法集合与 event proxy 身份绑定；
- 无 direct-send 的动态探针；
- fixture 调用的返回类型、字段、数量、error/timeout/cancel 行为；
- 同名 raw/plugin/MCP/Handoff/alias 枚举结果；
- global tool exposure 探针，证明 guest 与其他非授权 Agent 零可达。

任何字段缺失、漂移或无法读取都失败关闭。不得把旧 snapshot digest 自动升级成当前 runtime allowlist。

旧快照 canonical schema SHA256，仅供离线测试 fixture：

| Tool class | Schema SHA256 |
| --- | --- |
| `FileReadTool` | `c2699988e29253645d9f26e93197def1675b1022bd0b1109f016f637d3ffe22e` |
| `GrepTool` | `b7e65c466697b0e807ef7ae85d871f1bc82baa5f9235d980a9e2e3612d6e745c` |
| `ExecuteShellTool` | `596fbad7c4464e5900a17e8682d9e90a2ea703540afa7cd9fa0e6a95e0487397` |
| `MemoryMemorizeTool` | `d9443ee69a71449877f2afc49b8f9a5a25972f14dd66677977583ba6d9f7ef8b` |

## 10. 必须失败关闭的共同条件

- adapter missing、disabled 或 operation 不在闭集；
- 非可信 owner、非私聊、identity degraded、stale binding 或 proactive/ambient 轮次；
- 当前消息没有单一明确 literal slot，或参数来自引用、历史、记忆、模型、示例、工具输出；
- exact tool missing/inactive、同名冲突、来源/handler/schema/helper 漂移；
- raw LivingMemory 工具绕过 permission wrapper；
- Handoff、MCP、background、direct-send 或通用 tool hook；
- candidate config/runtime/booter/cwd/scope/permission 无法证明；
- guest 或其他 Agent 路径仍能调用 owner-only exact tool；
- 路径 traversal、UNC/device/ADS、foreign flavor、symlink/junction/reparse、hardlink alias、root escape 或 TOCTOU root 不可信；
- 非纯文本 read、任意 regex grep、未受限 corpus/output；
- Memory expected subject/scope 与插件解析结果不一致；
- Shell 未完成二次确认、confirmation 重放或 command digest 改变；
- timeout、取消、异常、零结果、多结果、`None`、未知字段、返回类型不匹配、输出超限；
- 原始正文、路径、命令、ID、scope、persona、stdout/stderr、凭据或完整返回进入 repr、trace、日志、GroundingFact、LivingMemory 回写或 Renderer；
- partial/unknown effect 被描述成成功或被自动重试。

## 11. 验收状态

本报告只完成 P3-08B 静态 runtime adapter 审计，没有证明任何 adapter 可部署或可在线启用。后续 executor 实现应先建立全关闭 registry、typed parameter builders、canonical wrapper/builtin selection、confirmation/idempotency ledger、sealed executor 与上述红灯；部署阶段再单独取得 candidate runtime descriptor。

在 live descriptor、私聊 owner-only、global exposure、permission、路径、输出、scope 与 sandbox conformance 全部通过以前，四个 adapter 均保持配置关闭；`sandbox_shell_once` 还必须保持代码级 hard-disabled。
