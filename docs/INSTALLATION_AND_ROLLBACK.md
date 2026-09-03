# 安装前检查与回滚框架

这是未来本地安装前的检查清单，不是已执行部署记录。SYS-001 只读取全新 `sys001` 配置；不会读取、迁移或自动删除旧 0.5.x 配置和 state。

## 安装前配置所有权

- AstrBot 是 Persona、`admins_id`、工具权限、内置指令禁用、沙箱、当前 Provider 与 `fallback_chat_models` 的唯一 owner。Shio 不复制这些权限或主回复灾备。
- 群聊近期历史必须在 AstrBot 开启 `provider_ltm_settings.group_message_history_enable`，并关闭 `group_icl_enable`；历史不可用时 Shio 只提供当前真实 event，不回退到不可净化的 conversation 文本。连续文本首条和后续都会等待安静窗口；媒体/Reply与明确命令继续走各自 AstrBot 官方链。媒体/Reply没有官方终态时，同 scope 文本会保持静默到插件重载。
- LivingMemory 独立于 Shio；Shio 不读写或过滤其持久数据。黑名单仅按当前结构化 sender ID 处理。
- 使用 Shio 多气泡时关闭 AstrBot 内置 `segmented_reply`；Shio 会保留 AstrBot 标准首条并依次提交后续文字。配置 Shio 的“气泡间最短等待秒数”和“气泡间最长等待秒数”（可都为 0，最长不得小于最短）。单气泡不受影响；Meme 图片另发、在全部文字之后且不计数。若因其他需求启用 AstrBot 分段，只能使用仅 LLM、regex `(?s).+`、无清理规则的兼容形状，否则 Shio 会报告冲突；Shio 不改写此设置。
- 固定审计的 Meme Manager 为 4.15.4 / `3a4cac134abf22a8617eda837bd2c9a9c1b90b1f`；仍由它的 emotion/semantic 模式选图，多文本组件需其 separate-send 设置。
- Master 告警默认关闭。启用前 Master 必须先发送真实私聊，使官方 UMO 被绑定；勿扰使用固定 `+08:00`。`submitted` 只是提交到 AstrBot 主动消息接口，不是网络送达；失败不重试。

群冷场续题与私聊主动关心（D-074）不在本候选中，没有配置或状态。

## 备份、验收与回滚

安装前备份 AstrBot 配置、Shio 新配置、已安装插件清单和相关可恢复数据。安装后由 Owner 分别验证普通私聊、群 direct、自然参与、媒体、segmented reply、Meme 及 Master 绑定路径；这些不是当前本地候选的生产证明。

如需回滚，停止使用候选版本、恢复安装前备份的配置与插件版本，并重新验证 AstrBot 标准对话。部署、重启和实际回滚都需 Owner 单独明确批准；本文不提供自动删除、生产路径或重启命令。
