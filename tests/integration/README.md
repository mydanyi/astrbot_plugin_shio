# 真实 AstrBot QQ 回复链路回归

`qq_delivery.py` 在 AstrBot 4.27.4 Docker 环境中运行，不是 WebUI 可用性检查。

实际执行的部分：

- AstrBot 插件加载器、官方内置插件、Shio，可选 Meme Manager 5.0.0；
- `AiocqhttpAdapter.convert_message/create_event` 解析 QQ 消息；
- 官方 WakingCheck、Process、ResultDecorate、Respond 四个阶段；
- OpenAI Provider、ToolLoop、MainAgentHooks、工具调用和官方 SQLite conversation 保存；
- QQ 适配器向 OneBot WebSocket 发出的文字和图片请求；
- 官方 WebChatAdapter 原生流式输出。

外部边界使用合成数据：本地 OpenAI HTTP/SSE 服务提供确定的模型输出，本地 OneBot WebSocket 对端记录发送并返回成功。配置、Provider 选择和 Persona 管理器使用测试夹具。测试容器 `--network none`，不会连接 QQ 账号、生产模型或生产数据库。

它覆盖本次修复的真实调用顺序，但不代表所有 Pipeline 前置阶段、真实 NapCat、QQ 网络送达、图片临时 URL、模型内容质量或语义检索服务已通过验证。

## 运行

需要含 AstrBot 4.27.4、pytest/pytest-subtests 和所测 Meme 插件依赖的镜像。本次使用既有验证镜像 `local/astrbot-bug003-test-env:rc12`。从插件目录执行，例如：

```sh
docker run --rm --network none \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/AstrBot \
  -e QQ_FIXTURE_MEME=1 \
  -v "$PWD:/AstrBot/data/plugins/astrbot_plugin_shio:ro" \
  -v "$MEME_SOURCE:/AstrBot/data/plugins/meme_manager:ro" \
  -w /AstrBot "$ASTRBOT_TEST_IMAGE" \
  python data/plugins/astrbot_plugin_shio/tests/integration/qq_delivery.py
```

`MEME_SOURCE` 指向仅含 Meme 源码的目录，不应指向生产 data。`ASTRBOT_TEST_IMAGE` 指向准备好的验证镜像。省略 Meme 挂载并设 `QQ_FIXTURE_MEME=0` 可验证只有 Shio 的链路。

成功必须同时满足退出码 0 和最后一行 `QQ_PIPELINE_PASS`；不能只看模型请求成功日志。

Meme 测试关闭情感辅助和语义检索，使用容器内生成的测试图片验证标记清理、分段后图文顺序；不会把类别选图的测试冒充生产向量检索验证。

## 旧测试边界

0.5.5 新增两种引用回复（可用工具和不存在的工具）及四行合并场景，合计 23 场景。所有气泡直接对比原始发送文字，不再先 strip 再比较；内部合法换行保留，气泡首尾空白不允许。旧版 0.5.4 的这两类引用场景均多发草稿，原始气泡也确实带尾换行；修复后这些失败用例通过。

0.5.4 时组合共有 20 个场景。加载顺序按生产设置为 Meme、Shio、官方内置聊天插件，防止相同优先级造成假通过；包含跨发送者三消息 ICL、空私聊通知、复杂 Unicode 三气泡加图片，以及 Meme 辅助选图收到当前用户消息但历史不重复的回归。最后一项使用固定模型返回验证数据传递；真实模型是否遵循纯文字要求另由实际 QQ 验证。独立真实 QQ 结果见 ../../docs/reports/2026-09-07-bubbles-memes-054.md，不与本目录的合成外部服务混淆。

仓库还留有依赖历史 R 编号压缩包、旧 Meme 4.15.4 和本机绝对路径的回放测试。直接运行整个 `tests/` 在没有这些外部材料时会失败。不要将它们删除、改成无条件通过，也不要把本次当前回归子集称为全量绿测。

本次单元回归子集排除以下五个依赖旧压缩包的文件，以及两个依赖外部工作树/旧候选的测试：

- `test_r14_recorded_official_replay.py`
- `test_r15_meme_official_recorded_replay.py`
- `test_r28_disabled_bubble_official_chain.py`
- `test_r29_disabled_bubble_single_event_chain.py`
- `test_r30_official_config_and_priority.py`
- `test_r26_official_plugin_save_preserves_provider_values_and_order`
- `test_frozen_r35_candidate_has_the_three_replaced_r36_paths`

这些旧版回放不能替代本目录中当前 AstrBot + 当前 Meme 的组合验证。
