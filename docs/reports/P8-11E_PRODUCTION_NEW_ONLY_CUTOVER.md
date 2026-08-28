# P8-11E FNOS 生产 new-only 切换

## 只读预检

- 目标：`192.168.50.38`，容器 `astrbot`。
- 切换前：running、restart 0、WebUI HTTP 200。
- 切换前配置：`architecture_v2_mode=enabled`、`architecture_v2_rollout_scope=owner_private`。
- 六个待更新运行文件均与线上旧哈希不同，范围与 P8-11A～D 改动一致。

## 备份

- 插件目录：`/AstrBot/data/backups/shio/astrbot_plugin_shio-P8-11E-pre-new-only-20260817T154051Z`
- 配置：`/AstrBot/data/backups/shio/astrbot_plugin_shio_config-P8-11E-pre-new-only-20260817T154051Z.json`
- 备份文件数 115；备份 `main.py`、`architecture_mode.py` 与配置哈希已验证。

## 最小部署

只更新星汐以下文件：

- `_conf_schema.json`
- `main.py`
- `core/architecture_mode.py`
- `core/fast_path.py`
- `core/reply_composer.py`
- `core/context_builder.py`

Windows 本地、FNOS host 暂存区、容器暂存区和线上目标四层 SHA-256 一致；容器暂存区与线上目标均编译通过。配置仅把 rollout scope 从 `owner_private` 原子改为 `all`，保留 `enabled` 和现有其他字段。

## 重载验证

- 新 StartedAt：`2026-08-17T15:41:36.232906306Z`
- 状态：running，restart 0。
- 配置：`enabled + all`。
- WebUI：HTTP 200。
- 容器内合成门：owner group、guest group、guest private 均为 `all_direct_eligible`。
- 新 StartedAt 后日志：Traceback 0、ERROR 0、旧 Planner 标记 0、typed fail-closed 0。

## 容器完整验收

将线上实际插件目录复制到独立容器临时目录，只在临时副本加入本地测试与 P0 基线文档；最终使用 AstrBot 容器 Python 运行 516/516 通过。第一次临时验收误发现线上残留旧测试，第二次缺少只读基线文档，这两次均是验收目录组装问题；修正后的唯一测试目录完整通过，线上插件和服务进程未被这些临时文件修改。

## 首轮真实流量观察

报告收尾前自然产生 2 次 `typed_reply.prepared`，并记录 3 次 `send.succeeded`（多气泡可使发送数大于回复轮数）。同一窗口内 typed blocked 0、state missing 0、repair rejected 0、Traceback 0、ERROR 0、旧 Planner 标记 0。观察只统计结构化事件名，未读取或保存原始对话内容，也没有主动向群里制造测试消息。

未修改 LivingMemory、表情包插件、AstrBot 核心、Provider、Compose 或 FNOS 其他配置；未执行 Git/GitHub 写操作。
