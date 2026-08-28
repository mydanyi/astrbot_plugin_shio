# P2-06 AstrBot 原生多模态适配报告

## 结论

P2-06 已实现并接入生产热路径。`core/astrbot_media_adapter.py` 复用 AstrBot 已完成的 MessageChain/Reply 解析、图片下载与压缩、Reply-ID 回退、caption 和 ProviderRequest 多模态数组，不下载媒体、不调用第二套 VLM；`main.py` 在原生请求完成后、清理临时 Prompt 内容前生成同一 revision 的 typed `MediaContext`。

本层把两类数据严格分开：

1. `MediaContext/MediaItem`：只保存当前 revision 绑定、direct/quoted/quoted_fallback 来源、可信发送者、统一 provider index、可用性和 caption SHA-256；
2. `NativeMediaTransport`：只为 Provider 传输和一次 repair 保留原始 `image_urls/audio_urls`。自定义 `repr` 与 trace 只显示计数，locator 不进入 typed context、trace、学习数据或安全 Prompt 证据。

## 实现边界

`adapt_astrbot_media()` 接收：

- P1 的不可变 `DecisionBinding`；
- AstrBot 原生 `ProviderRequest` 兼容对象；
- 原始 MessageChain、`event.message_obj.message` 或 Reply chain 兼容结构；
- 可选的可信 sender-key resolver。

适配规则：

- 顶层 `Image/Record` 绑定当前消息和当前发送者；
- Reply 内嵌媒体绑定 Reply ID 与 Reply sender；
- Reply 只有 ID、AstrBot 已把回退图片加入 ProviderRequest 时，绑定为 `quoted_fallback`；
- provider index 按 AstrBot transport 的图片数组顺序、随后音频数组顺序统一编号，满足 `MediaContext` 的唯一索引约束；
- 原生 caption 只在来源唯一可归属时使用，typed context 仅保存摘要；含 URL、路径、base64 locator 的 caption 不进入安全 Prompt 证据；
- 缺少 transport 与可信 caption 时显式为 `UNAVAILABLE`，并产生 `native_media_unavailable`，不制造视觉结论；
- item ID 由 revision、来源、类型和 provider index 决定，不依赖临时 URL/路径，因此 repair 或本地解析路径变化不会改变 item ID；
- `for_repair()` 复用同一不可变 context，`restore_request_media()` 原样恢复首次适配时的 image/audio 数组。

无法从 Reply 取得消息 ID 或可信 sender 时，适配器记录 `quoted_media_source_unresolved` 并拒绝猜测；无法给额外 Provider 媒体证明来源时保留 transport 但不伪造 typed item。

## 测试驱动证据

首次定向执行在实现文件不存在时按预期失败：

```text
ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.astrbot_media_adapter'
```

实现后的 `tests/test_astrbot_media_adapter.py` 覆盖：

1. direct 文本+图片与仅图片；
2. quoted 媒体的消息和发送者绑定；
3. Reply-ID-only 图片回退；
4. 多图片、音频和 quoted 混合顺序；
5. 原生 caption 仅摘要进入 typed context；
6. unavailable 显式降级；
7. repair 复用 item IDs 和原始 image/audio 数组；
8. URL、Windows 路径和 base64 只存在于 transport，不出现在 typed trace、repr 或安全 Prompt 证据；
9. caption 内混入 URL/绝对路径等 locator 时失败关闭，不把 locator 送入安全 Prompt 证据。

## 验证结果

- P2-06 定向测试：`9/9` 通过；
- P2-06 + P1 行为合同联测：`27/27` 通过；
- 纯模块阶段完整本地回归：`477/477` 通过；P2-03～P2-06 并行生产接线稳定后的完整本地回归：`526/526` 通过；
- 相关 Python 文件 `compileall`：通过；
- 未连接 Provider、未发送消息、未调用真实图片理解；
- 生产接线修改了星汐 `main.py`、Reply Composer、管线测试和本报告；未修改 AstrBot 核心或第三方插件；
- 未部署，未执行 Git/GitHub 写操作。

## 生产接线

- 每个 admitted turn 都产生一个 `AstrBotMediaAdaptation`，无附件时也有绑定当前 `DecisionBinding` 的空 context，不能沿用上一轮媒体。
- Reply Composer 不再只接收无来源的 image/audio 数量，而是接收 direct/quoted/quoted_fallback、raw/caption/unavailable 和来源关系；模型 Prompt 不含 message/sender ID、URL、路径或 base64。
- AstrBot 原生 caption 在 `extra_user_content_parts` 被清理前提取；typed context 只保留摘要，安全 caption 正文作为当轮视觉证据进入 Composer，因此非视觉 Provider 不再丢失图片语义。
- 首次生成继续使用 AstrBot 原始 `image_urls/audio_urls`；一次 repair 复用同一 `MediaContext` 和完全相同的 transport 数组，不再硬编码为空。
- raw locator 已从通用 `SHIO_PAYLOAD` 移除，只保存在自定义不可打印的 `NativeMediaTransport`；fail-closed 路径会清空 Provider 媒体数组。
- unavailable 只向 Composer 表达“当前不可见”，具体自然请求重发仍由后续 ContentIntent/Persona 层决定，适配器不生成角色台词。

生产测试先红稳定复现三项缺口：缺少 `SHIO_MEDIA_ADAPTATION`、caption 清理后消失、repair 的 `image_urls=[]`。接线后：

```text
python -m unittest \
  astrbot_plugin_shio.tests.test_astrbot_media_adapter \
  astrbot_plugin_shio.tests.test_reply_composer \
  astrbot_plugin_shio.tests.test_repair_controller \
  astrbot_plugin_shio.tests.test_pipeline -v
Ran 79 tests ... OK
```

本地只验证请求构造与确定性 Stub；真实 Provider 图片理解能力留到候选容器和 FNOS 部署后的用户图片场景验收。本阶段仍未部署，也未执行 Git/GitHub 写操作。
