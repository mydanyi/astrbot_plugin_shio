# P10-10 主动 scheduler 热重载恢复

状态：**完成并部署（0.5.3）**。中断恢复以本报告和 `SHIO_MASTER_PLAN.md` 第 12 节为准。

## 1. 生产根因

- 2026-08-19 17:20:41（Asia/Hong_Kong），WebUI 已把 `proactive_initiation_enabled` 保存为 `true`，两个目标群仍在白名单。
- 保存配置触发 AstrBot 对插件执行 `terminate` / `load`。新插件实例正常创建，观察链也继续更新持久状态，但动态插件重载不会再次派发全局 `on_astrbot_loaded`。
- 旧实现只在 `start_proactive_scheduler` lifecycle hook 中创建 scheduler task，因此热重载后的新实例没有 scheduler；这解释了 `last_trigger_at=0`、`daily_count=0` 且没有 Provider、terminal、send 或 scheduler error。
- 至少一个群当时已经满足观察期和空闲期，故根因不在活跃时段、观察、空闲、冷却或日限额。

## 2. 修复

- `main.py` 新增 `_ensure_proactive_scheduler_started()`，在插件实例构造末尾、已绑定运行事件循环时启动 scheduler。
- 原 `on_astrbot_loaded` hook 保留为冷启动兼容入口，但只调用同一个幂等 helper；运行中的 task、重复 hook 和重复调用都不会创建第二个 task。
- 总开关关闭、白名单为空、policy 非 operational 或当前没有 running loop 时继续零 task，未放宽主动发言策略。
- 增加 `proactive.scheduler_started` 脱敏结构化日志，仅记录固定调度间隔，不记录群 ID、消息或 prompt。
- 发布版本从 0.5.2 提升到 0.5.3，README、CHANGELOG、metadata 与候选包测试同步。

## 3. 红绿证据

- 红灯：`test_hot_reload_instance_self_starts_without_global_loaded_hook` 在修复前真实失败，启用配置的新实例在不调用 global loaded hook 时 `_proactive_scheduler_task is None`。
- 定向：`1/1`。
- proactive focused：`12/12`。
- proactive + P9 + pipeline related：`151/151`。
- release/candidate/proactive：`23/23`。
- Windows full：`1258/1258`，skipped 7。
- 隔离 FNOS AstrBot container full：`1258/1258`，47.527 秒。
- `compileall`、candidate verifier、`git diff --check` 均通过。

## 4. 候选包

- ZIP：`astrbot_plugin_shio_v0.5.3_upload.zip`
  - 95 files
  - 504169 bytes
  - SHA256 `68C95C1D8078A4F855DF4D16FFFC7DF0AF4B53B8F4DD0CDD280221A8D3D31EE3`
- manifest SHA256：`F2D3D6C793CC5CEDE267C9530BE3E0BE70E6742E68B094C5A8E74F79B8C4E21C`
- 完整测试源 TAR SHA256：`F7D16FC73FE530F430C2EFE0462719E060A3B8611B11DB3FDD409E41DA880239`
- 容器内 verifier 结果：`verified=true`、`release_version=0.5.3`、`file_count=95`。

## 5. 生产备份与部署

- 备份根：`/AstrBot/data/backups/shio/P10-10-proactive-hotfix-20260819T132136Z`
- `plugin-copy/`：部署前 0.5.2 完整插件副本，181 files。
- `live-moved-original/`：目录切换时保留的原始 0.5.2 live 目录。
- `astrbot_plugin_shio_config.json`：部署前配置副本，SHA256 `09528ABB3BDB988EA50DC515E5085EC3AE642DAB7C702E8F6807EF557C62D769`。
- 先在同卷 staging 解包并验证，再把 live 目录移动到备份根、把 95-file candidate 移入 live；未删除旧目录。
- 重启前 live `main.py` SHA256 `249932D82E55A8D739092681124C79CD9E5FCCA5CE470F2AC4E13799FDC3A986`，`metadata.yaml` SHA256 `DAD354737245E70A6B57F55F0172F54D31BB052A8A7E104EF4C808589A2C7AB7`。

## 6. 重启后生产验收

- AstrBot `StartedAt=2026-08-19T13:22:21.256110865Z`，container running，`RestartCount=0`，WebUI HTTP 200。
- 日志明确加载 `astrbot_plugin_shio (0.5.3)`。
- 启动窗口 `proactive.scheduler_started=1`、`start_proactive_scheduler hook=1`；构造入口先创建 task，随后 hook 幂等复用，没有第二条 started 事件。
- 启动窗口 `Traceback=0`、`ERROR/Exception=0`。
- 配置保持：总开关开启、白名单 2 群、活跃时段 09:00-23:00（UTC+8）、观察 30 分钟、空闲 20 分钟、冷却 180 分钟、每群每日 1 次、scheduler 60 秒。
- manifest 中 95 个发布文件全部哈希/大小一致；运行后新增 86 个文件全部为 Python `__pycache__/*.pyc`，无非缓存额外文件。

## 7. 真人流量边界

- 本次没有伪造群消息或强行发送主动对话。自动门已经证明 task 存在且唯一，但“实际主动消息成功送达”仍需自然流量满足策略后观察。
- 完整容器重启会清空 scheduler 的内存群目标；下一条白名单群内的已准入人类消息会重新注册该群。此后至少空闲 20 分钟，并同时满足 09:00-23:00、观察期、冷却和日限额，才有资格发起。
- 因此部署后立即没有主动消息是策略预期，不再是 scheduler 缺失；若自然条件满足后仍未触发，应从本报告的 `StartedAt` 新窗口继续查 `proactive.terminal` / send 日志。

## 8. 边界

- 未修改 AstrBot 核心、全局 Provider 回退、LivingMemory、Meme Manager、Docker Compose、FNOS 其他服务或 Git。
- 可回滚：停止/重启 AstrBot 前后均可用备份根的 `live-moved-original/` 恢复 0.5.2，并恢复同目录配置副本。
