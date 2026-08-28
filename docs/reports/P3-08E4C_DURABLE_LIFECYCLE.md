# P3-08E4C Owner Action Durable Lifecycle / Tombstone 报告

日期：2026-08-18

状态：E4C 第三轮独立对抗发现的 B1/B2 已正式红测并根修；Windows/WSL focused 与 1039 项全仓验证完成，等待再次换人独立终审

范围：只新增 `core/owner_action_lifecycle.py`、`tests/test_owner_action_lifecycle.py` 和本报告；没有接入 Controller/Main，没有开启 production adapter，没有部署 FNOS，没有执行 Git 写操作

## 1. 目标与结论

本层建立与 Controller active material 分离的最小 durable lifecycle：

```text
exact Controller request digest（仅调用时输入，不落盘）
+ code-owned operation
+ installation secret
→ HMAC-SHA256 request fingerprint
→ reserve
→ in_progress
→ terminal
→ delivery_ack
→ TTL/count bounded prune
```

冻结候选额外关闭以下持久化边界：

- 同一 resolved root 只有一个 live writer；Windows 使用 lock-file byte lock，POSIX 同时持有 root-directory `flock` 与 lock-file `flock`。进程内或跨进程第二实例都在读取 journal 前非阻塞失败关闭；lock path 被 unlink/replace 也不能创建第二 writer；
- loader 只接受 4 MiB 内、无重复键/NaN/Infinity、exact typed schema、字节级 canonical 的 UTF-8 JSON；
- root/journal/lock 拒绝 symlink/reparse point，journal/lock 拒绝非 regular 或 hard-link 文件；open 前后重复核对 inode/device/link-count/path identity。POSIX root 必须 exact `0700`，journal/lock/temp 必须 exact `0600`；
- terminal status/effect/attempted 必须与 canonical `ActionReceipt` 的 operation/side-effect 矩阵完全一致；
- public handle 的 copy/cross-store/mutation/字段删除再恢复均不能推进状态；即使 primary handle 被 `object.__setattr__` 污染，原 request 的 canonical reserve/snapshot 路径会换发健康 handle，不会被永久毒化。
- `enabled`、`failure_code`、trace、repr、context enter 和全部状态 API 都在同一实例锁内先复核 process-lock/root identity；即使 caller 同时提交非法参数，也不能抢在 lock health 之前取得普通 transition error；
- durable store 的 failure code 是 code-owned 闭集。OSError/ValueError/BaseException、直接 `_disable()` 和 `LifecycleStoreDisabled` 都不能透传异常字符串、root/lock path 或其他 caller material。

journal 只保留：

- schema/generation 和 installation-secret verifier；
- 不可逆、domain-separated 的 request fingerprint；
- 闭集 operation、lifecycle phase、result status、effect、attempted；
- created/updated/terminal/delivery-ack 时间。

明确不保存 request digest、参数、path、command、memory literal、tool output、真实 sender/message/platform ID、ActionOutput 或模型文本。文件损坏、未知 schema、installation secret 不匹配、记录形状不合法、原子写失败均把 store 置为 all-off；不会用空 journal 覆盖坏文件。初始化或持久化边界遇到 `BaseException` 也会在重新抛出前显式释放自身 OS locks，不依赖 GC；`close()` / context manager 是正常 terminate seam，重复 close 幂等，关闭后的 reserve/start/terminal/ack/snapshot/prune 全部失败关闭。所有可观测 failure/exception 只返回闭集原因码，绝不拼接底层异常文本。

## 2. 非 authority 边界

本模块不导入或签发以下任何 authority：

- `OwnerActionRequest`；
- `ExecutionLease`；
- `ActionReceipt`；
- `ActionOutcome` / `ActionOutcomeAuthority`；
- Controller route、pending confirmation 或 adapter material。

`LifecycleHandle` 只是 store-local canonical identity，不是执行许可。`mark_in_progress()` 返回的 `claimed=True` 也只证明 durable start marker 的唯一写入；后续集成必须同时持有 Controller 的 exact canonical request/lease。public terminal seam 接受闭集 status/effect/attempted 并做组合校验，但只记录事实，不把 caller 输入升级成 Receipt 或 Outcome。

## 3. 重启恢复规则

| 持久化状态 | operation | 重启后的 terminal status | effect | attempted | 可自动重跑 |
|---|---|---|---|---:|---:|
| `reserved`（含未来 confirmation pending 投影） | 任意 | `STALE` | `NOT_STARTED` | false | 否 |
| `in_progress` | `ARTIFACT_READ_EXACT` / `ARTIFACT_GREP` | `FAILED` | `NO_SIDE_EFFECT` | true | 否 |
| `in_progress` | `MEMORY_WRITE_LITERAL` / `SANDBOX_SHELL_ONCE` | `EFFECT_UNKNOWN` | `UNKNOWN` | true | 否 |
| `terminal` | 任意 | 原值 | 原值 | 原值 | 否 |
| `delivery_ack` | 任意 | 原值 | 原值 | 原值 | 否；只在 TTL 后可 prune |

read crash 可以代码级确认没有副作用，所以恢复为 failed/no-effect；write 或 code-execution crash 无法安全确认提交状态，必须恢复为 effect-unknown，绝不自动重试。terminal 与 delivery-ack 重启后保持原值，重复 reserve 只能取得 `EXISTING_TERMINAL`。

## 4. 持久化与崩溃边界

每次变化先构造新的 immutable record map，再序列化为 byte-exact canonical JSON，写同目录 user-only 临时文件、flush/fsync，最后 `os.replace` 原子替换；POSIX 通过已加锁的 root-directory fd 继续 fsync parent directory。replace、落盘后 identity 复核和内存 map/generation 发布都在同一 `BaseException` fail-closed 边界内。

验证覆盖：

1. replace 前失败：旧 journal 字节完全不变，临时文件清除，当前 store all-off；用相同 secret 重开可读取旧状态。
2. replace 已成功后分别注入 `RuntimeError`、`KeyboardInterrupt`、`SystemExit`：当前实例立即 all-off，不能把同一 request 再次 `RESERVED`；重开从实际磁盘状态恢复为 stale/not-started。
3. reserve 已落盘、进程在 start 前消失：重启转 stale/not-started。
4. in-progress 已落盘、进程在 terminal 前消失：按 read/mutation 规则恢复，不重跑。
5. terminal 已落盘、进程在 delivery 前消失：重启保留 terminal，不重新 claim。
6. 同一 store 32 线程同时 reserve/start：只有一个 `RESERVED` 和一个 `claimed=True`；32 线程 terminal/ack/prune 保持幂等。
7. 同一 resolved root 的第二 live store（同进程或 spawn 子进程）立即得到 `lifecycle_store_locked`；POSIX lock path 被 unlink 或 replace 后，root-directory lock 仍阻止第二实例误恢复第一实例的 active record，第一实例下次 health/API 检查则 all-off。
8. duplicate key、bool schema/generation/record field、NaN、非 canonical padding、5 MiB 输入、整数 timestamp、`str`/`int`/`float` 子类和 `bool == 1` 混淆全部失败关闭。
9. root/journal/lock 的 symlink、non-regular、hard-link、宽权限以及 lstat→open 间新增 hard-link 全部在状态发布前失败关闭；坏文件字节保持不变。
10. Windows 显式使用 binary fd，避免 CRT newline translation 影响 canonical bytes；Windows 当前账户不能创建 symlink，完整 symlink/FIFO/mode/inode 用例在 WSL/Linux 路径零 skip 复现。
11. lock path 失效后分别以 `enabled`、`failure_code`、trace、repr、context enter、reserve/start/terminal/ack/snapshot/prune 作为第一个入口，全部立即 all-off；无效参数同样不能绕过该复核。OSError/ValueError/RuntimeError/KeyboardInterrupt/SystemExit 的 failure、repr、trace 和 `LifecycleStoreDisabled` 都只出现闭集码，不含异常 marker 或 root/lock path。

## 5. Secret 与脱敏

调用方必须提供 32～256 bytes 的 installation secret；journal 只保存：

```text
HMAC(secret, fixed install-verifier domain)
HMAC(secret, fixed request domain || operation || exact request_digest)
```

secret 和 request digest 本身不写盘。重启用 constant-time compare 验证 secret；secret mismatch 不创建新 ledger、不覆盖旧 ledger。`repr()`、`trace_metadata()`、异常码和 snapshots 均不显示 root path、secret、request digest 或 fingerprint；trace 显式声明 `request_material_visible=False`、`install_secret_visible=False`、`execution_authority=False`、`action_outcome_authority=False`。内部 `_disable()` 也先经过 exact-string allowlist；未知对象、异常文本或被污染的 code 统一收敛到 `lifecycle_fail_closed`。

## 6. Bounds 与 reclaim

- `max_records` 闭集为 1～4096，默认 256；满且无可安全清理项时失败关闭。
- tombstone TTL 为 900 秒～365 天；默认 900 秒，与现有 owner request 最大 lifetime 下界一致。
- pending/reserved、in-progress、未 delivery-ack 的 terminal 绝不被 count eviction。
- 只有 delivery-ack 且同时超过 ack TTL/record lifetime 的最小 tombstone可 prune。
- 1024 轮 reserve→start→terminal→ack→prune（在第 512/1024 轮分别检查）证明 ledger/handle 不无界增长。

TTL 后允许删除 fingerprint 的前提是 Controller exact request 自身已超过最大 lifetime；E5 集成仍必须重新检查 Controller canonical request/deadline，不能把 durable store 当作授权源。

## 7. 测试证据

### 7.1 先红

```text
python -m unittest astrbot_plugin_shio.tests.test_owner_action_lifecycle -v

初始模块红：

ModuleNotFoundError: No module named
'astrbot_plugin_shio.core.owner_action_lifecycle'

独立对抗复审正式红（修复前）：

Ran 19 tests in 3.174s
FAILED (failures=33, errors=1, skipped=1)

冻结终审第二轮正式红：

Ran 27 tests in 12.309s
FAILED (failures=5)

追加 live-lock health 红：

Ran 1 test in 0.013s
FAILED (failures=1)

第三轮独立终审 B1（WSL，修复前）：

Ran 1 test in 0.075s
FAILED (failures=11)

第三轮独立终审 B2（Windows，修复前）：

Ran 2 tests
FAILED (failures=4)

公开 API 无效参数 health-order 红（WSL，修复前）：

Ran 1 test in 0.061s
FAILED (errors=6)
```

第一轮红灯实证包括：26 个 operation/status/effect/attempted 非法组合被接受、`str` 子类重定向 request fingerprint、两个 live store 同时取得 `RESERVED + claimed=True`、第二实例误恢复第一实例 active record、重复 JSON key/bool schema/5 MiB padding 被接受，以及 primary handle mutation 永久毒化 request。随后在 Windows 首次加入 lock 时又捕获 `.lock` fd 未显式关闭导致的 `WinError 32`。

第二轮冻结终审继续抓出并正式红测六个 blocker：`max_records` 恶意 `int` 子类绕过 4096；POSIX `0666` journal 被接受；`os.replace` 成功后 `BaseException` 造成磁盘/内存分叉并允许同请求再次 `RESERVED`；初始化持锁后 `BaseException` 依赖 traceback GC 才释放 fd；journal lstat→open 间新增 hard-link 未被 fstat 检出；live lock path unlink 可创建第二 writer 并把第一 writer 的 active record 错恢复。根修增加 exact numeric boundary、POSIX root-directory lock、持续 path/inode/link/mode 复核，以及覆盖 post-replace/内存发布全过程的 `BaseException` fail-closed。追加红再确认 `enabled`/trace health 查询本身也不能报告已失效 lock 为健康。

第三轮独立终审再确认 2 个 blocker。B1：`failure_code` 是唯一不加实例锁、不复核 live process lock 的观察面，故 lock path 已失效时仍返回空 failure 并保持 `_enabled=True`。B2：health 复核把 `str(exc)` 直接写入 failure code，使 `enabled`、trace、repr、后续 `LifecycleStoreDisabled` 暴露绝对 root/lock path；直接 `_disable()` 同样接受任意字符串。根修引入 code-owned exact-string failure allowlist，所有异常入口按上下文映射闭集码，`_disable()` 二次收口；`enabled`/failure/trace/repr 和全部状态 API 统一在锁内先做 process-lock health。额外红测证明非法参数也不能抢在 health 之前返回 transition error。

第四轮独立终审又确认 3 个 blocker：public call 仍会原样抛出带 root/marker 的底层 `RuntimeError`/`KeyboardInterrupt`/`SystemExit`；live journal unlink 或同 secret 的旧 canonical journal 替换未进入 health identity；POSIX root rename/recreate 可绕 inode `flock` 建立第二 writer。根修后，公开异常保留精确中断类型但重建为空参数且断开 cause；独立 `0600` anchor 持久化 journal generation、完整摘要与 HMAC，health 每次复核 anchor、journal inode/size/mode/content；Linux 另以 abstract AF_UNIX 名称锁住规范化逻辑 root，使 directory inode 被换掉也不能出现第二 writer。anchor 与 journal 的两文件写入若在 replace 后被中断，只在磁盘 journal 与精确 canonical candidate 摘要相等时补齐 anchor；否则实例永久 fail-closed，不把任意磁盘内容升级成 authority。

### 7.2 focused

```text
Windows:
python -m unittest astrbot_plugin_shio.tests.test_owner_action_lifecycle -q

Ran 34 tests in 16.153s
OK (skipped=7)

WSL/Linux:
python3 -m unittest astrbot_plugin_shio.tests.test_owner_action_lifecycle -q

Ran 34 tests in 24.219s
OK
```

覆盖 happy path、最小 journal 字段、secret/request 脱敏、三类 restart recovery、terminal no-rerun、坏 schema/损坏/secret mismatch、32 线程唯一 reserve/start、32 线程 terminal/ack/prune 幂等、同 root 进程内/跨进程 OS lock、live lock unlink/replace、root rename/recreate 逻辑路径锁、journal unlink/旧版本替换、anchor generation+HMAC、root/journal/lock symlink/FIFO/hard-link/mode/inode、close/context/reopen、copy/cross-store/mutated/deleted-restored handle、自愈 canonical handle、完整 terminal 矩阵、strict canonical JSON/size/exact type、replace 前与 replace 后三类 `BaseException` 故障、公开异常文本净化、保留 120 个 init `BaseException` traceback 时 fd `4 → 4`、所有 health surface 和无效参数 health-order、闭集 failure/repr/trace、count/TTL 不淘汰 active/未 ack terminal、1024 轮、GC 后 store map 归零和 repr/trace/snapshot 隐私。另以真实 `reserve→start→terminal` 路径穷举 384 组合，结果为 accepted 42、rejected 342、mismatch 0。

### 7.3 静态检查

```text
python -c "compile(...)" \
  core/owner_action_lifecycle.py \
  tests/test_owner_action_lifecycle.py
compile=OK files=2

git diff --no-index --check -- /dev/null core/owner_action_lifecycle.py
git diff --no-index --check -- /dev/null tests/test_owner_action_lifecycle.py
git diff --no-index --check -- /dev/null docs/reports/P3-08E4C_DURABLE_LIFECYCLE.md
no whitespace-error output（exit 1 仅表示相对空文件存在内容）
```

### 7.4 全仓

```text
python -m unittest discover \
  -s astrbot_plugin_shio/tests \
  -p 'test_*.py' -q

Windows: Ran 1041 tests in 23.435s, OK (skipped=7)
WSL/Linux: Ran 1041 tests in 32.388s, OK
```

以上是共享工作树稳定态结果；独立复审新增红测和 Windows lock-close 修复期间的瞬时 full 失败不冒充最终证据。

### 7.5 持久化敏感字段静态门

```text
forbidden = {
  request_digest, path, command, memory, tool_output,
  sender_id, message_id, platform_id, secret
}

persisted_sensitive_field_gate=OK record_fields=10
```

该门直接检查 code-owned `_RECORD_KEYS` 与禁止持久化字段集合无交集；运行态测试另读取真实 journal 字节，验证 raw request digest、secret 和禁止字段名均不存在。

### 7.6 冻结候选哈希

```text
core/owner_action_lifecycle.py
6eaade9416f072ffc604f05bbba1e4557d443634186aedaa73bf6d8d83ae18cd

tests/test_owner_action_lifecycle.py
5cc0deb892379f25f36c55ecde5a2ddb8b42395ee3c676f9e802e5dea1553ad1
```

## 8. 修改文件

- `core/owner_action_lifecycle.py`
- `tests/test_owner_action_lifecycle.py`
- `docs/reports/P3-08E4C_DURABLE_LIFECYCLE.md`

没有修改 `core/owner_action_controller.py`、`core/action_outcome.py`、`main.py`、配置、第三方插件或现有测试。

## 9. 后续 E4D exact durable-ack integration 硬门

E4C 的 public `LifecycleHandle`、`LifecycleSnapshot`、bool/digest/Mapping 都不是 durable authority，不能据此清理 E4B 的 Controller tombstone 或 Outcome retired-source。E4D 未独立通过前，E4 整体不可部署。接线顺序必须是：

1. Controller inspect exact canonical request/lifecycle tombstone；
2. 用 exact request 的 operation/request digest 调用 `reserve()`；
3. Controller 成功签发 exact execution lease 后，才允许 durable `mark_in_progress()`；
4. Controller 从 sealed completion 生成 canonical receipt 后，才把其 closed status/effect/attempted 投影到 `mark_terminal()`；
5. 同一 outcome 完成 exact final presentation/send receipt 后，才 `acknowledge_delivery()`，并由 E4D 签发 exact store-local durable-ack capability；
6. 只有同一 Controller/Outcome lineage 消费该 exact capability 后，才可释放 active route/parameter/adapter/secret material，并安全 prune 内存 tombstone/retired-source；
7. restart reconciliation 由 Controller读取 snapshot 并签发对应 canonical receipt/outcome，本模块不得自行生成自然语言或发送；
8. durable transition/ack 失败、Controller reclaim 失败、进程重启和 512/1024 轮压力下都必须保持 no-retry 与全 ledger 有界，不能用 public snapshot 冒充 durable acknowledgement。

## 10. 明确未覆盖

1. 本层没有接 Controller/Main/config，因此 production owner action 仍未启用。
2. installation secret 的创建、权限、轮换和生产注入属于 E5 config/lifecycle graph；缺失或错配必须保持 all-off。
3. E5 必须持有一个 long-lived store，并在 terminate 时显式 `close()`；同 root 第二 live writer 已由平台级非阻塞 lock 失败关闭，GC 释放只作为 defense-in-depth。
4. active route/material/Controller vault 的实际释放属于 E4D/E5 exact integration，不由 durable journal持有或伪造。
5. exact durable transition/ack capability、Controller/Outcome 消费与跨重启全 ledger bounded reclaim 尚未实现，是 E4D 部署硬门。
6. 未执行容器/FNOS 验证、未开启 adapter、未执行 Git 写操作。
