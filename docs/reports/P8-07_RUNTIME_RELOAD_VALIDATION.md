# P8-07 生产重载与真实运行验证报告

## 结论

P8-07 已完成。`astrbot` 已重启并实际加载 P8-06 部署的新星汐代码；启动日志、模块来源、运行文件哈希、WebUI 和启动后日志均通过验证，没有触发回滚。

统一 v2 模式当前明确为 `off`，旧 `typed_context_v2_mode` 也为 `off`。因此本层只发布已经接入生产的身份、发送、并发、协议清理和可观测性基础，不会提前启用尚未完成范围门控的 typed ReplyComposer 链。

## 容器重载

| 检查项 | 结果 |
| --- | --- |
| 容器名预检 | 精确为 `/astrbot` |
| Restart 命令 | `docker restart astrbot` |
| 新 StartedAt | `2026-08-17T13:52:00.075383246Z` |
| Running / Restarting | `true / false` |
| OOMKilled / Dead | `false / false` |
| RestartCount | `0`（Docker 手动 restart 未计入策略重启次数，StartedAt 已证明发生重载） |
| WebUI 首次探测 | HTTP 200，第一次即就绪 |

## 插件加载证据

启动窗口日志明确出现：

- `Loading plugin astrbot_plugin_shio ...`
- `Plugin astrbot_plugin_shio (0.4.6) by Danyi`
- `hook(on_astrbot_loaded) -> astrbot_plugin_shio - refresh_provider_selectors`

启动窗口共 195 行，Traceback=0、ERROR=0。

新进程随后从 `core.observability:172` 输出 `[Shio/trace]`，字段使用 `group_digest`，证明实际运行的是新可观测性模块，不是只替换了磁盘文件。以插件加载完成时间为界重新统计：

- structured Shio trace：8 条；
- Traceback：0；
- ERROR：0；
- `group=<真实数字>`：0；
- `sender=<真实数字>`：0。

重启边界前缓存的一条旧 v1 日志仍带原始 group 字段，但加载完成后的新日志已全部切换为 digest；报告没有保存或回显该真实 ID。

## 运行文件与模块验证

- 本地运行文件：47；
- 线上运行文件：47；
- 缺失：0；
- 额外：0；
- SHA-256 不一致：0；
- 47/47 精确一致。

容器内以 AstrBot 的实际包路径导入并验证：

- `data.plugins.astrbot_plugin_shio.core.architecture_mode`：成功；
- `persona`：成功加载 `atri_default`；
- `send_receipt`：成功；
- 当前有效模式：`off`。

## 脱敏配置核对

只读取配置状态和数量，不回显真实 ID 或工具名：

| 配置 | 状态 |
| --- | --- |
| 星汐 enabled | true |
| `architecture_v2_mode` | off |
| `typed_context_v2_mode` | off |
| owner ID 数量 | 2 |
| 普通群友工具配置数量 | 7 |

## 回滚与临时目录

P8-05 备份仍保持原 59 文件和 manifest，未触发回滚。部署临时目录当前仍保留在 Windows、FNOS `/tmp` 和容器 `/tmp` 中用于本轮审计；它们不在 AstrBot plugins 扫描路径内。待 P8-08 指定范围验证结束后再按精确路径统一清理，不影响当前运行。

## 下一入口

P8-08：实现并验证真正的范围门控，不能只把配置从 off 改成 enabled。先在代码中加入可信 scope gate，使 `enabled` 只对指定 owner/private 测试范围激活完整 typed identity/context/plan/ReplyComposer/Validator，其他会话保持 shadow/off；补回归并重新部署后，才允许配置指定测试范围。禁止直接全群开启。
