# P8-04 生产环境只读预检报告

## 结论

P8-04 已完成，部署前环境预检通过。FNOS 上 `astrbot` 容器持续运行、重启计数为 0、没有 OOM 或重启中状态，AstrBot WebUI 返回 HTTP 200。线上仍是 AstrBot 4.27.2 与星汐 0.4.6，确认尚未加载本轮本地 v2 重构。

本层只执行查询、哈希和日志计数；没有创建目录、上传文件、重载插件、重启容器或修改配置。

## 连接与回落路径

- `trim-cli` 首先按技能流程尝试远程 WSS，只读连接未建立；
- 改用内网 `ws://192.168.50.38:5666` 后成功到达 FNOS，但 Docker 端点返回设备错误码 `135168`；
- 该错误与此前记录一致，不能推断容器停止；
- 随后使用已配置的 `FNOS` SSH 主机别名和专用密钥执行同等只读核对，目标主机仍为 `192.168.50.38`。

## 运行健康状态

| 检查项 | 结果 |
| --- | --- |
| 容器 | `astrbot`，running |
| Running / Restarting / OOMKilled / Dead | `true / false / false / false` |
| RestartCount | `0` |
| StartedAt | `2026-08-16T23:47:06.773106915Z` |
| 容器镜像 | `soulter/astrbot:latest` |
| AstrBot 版本 | `4.27.2` |
| 星汐线上版本 | `0.4.6` |
| 线上插件目录 | `/AstrBot/data/plugins/astrbot_plugin_shio` 存在 |
| WebUI | `http://192.168.50.38:6185/`，HTTP 200 |
| Docker Healthcheck | 镜像未定义通用 healthcheck；以运行状态、WebUI 和日志共同判断 |

最近 70 分钟日志只做脱敏计数：没有 Traceback；有 6 行星汐 error/fail 类关键词，均与 Provider/发送/协议关键词重叠。为避免回显真实群聊和模型正文，本层没有复制原始日志。这些计数说明旧生产链仍有需要本轮重构替换的输出异常，但没有证据表明容器或 WebUI 不健康。

## 文件哈希基线

只比较运行所需的根目录 Python/配置、`core/*.py` 和 `assets/*.(json|yaml)`，排除 tests、`.github` 与 `__pycache__`：

| 分类 | 数量 |
| --- | ---: |
| 线上运行文件 | 17 |
| 本地运行文件 | 47 |
| 共同路径 | 17 |
| 字节级相同 | 10 |
| 共同路径但内容已修改 | 7 |
| 本地新增、线上尚无 | 30 |
| 线上独有 | 0 |

已修改的 7 个共同路径：

- `_conf_schema.json`
- `main.py`
- `core/conversation_runtime.py`
- `core/planner.py`
- `core/recovery_queue.py`
- `core/response_guard.py`
- `core/style_retriever.py`

线上没有本轮新增的 30 个 v2 core/persona 文件，符合“生产仍为 v1”的预期。`metadata.yaml` 哈希仍一致，所以本地尚未提前修改版本号。

关键部署前哈希：

| 文件 | 线上 SHA-256 | 本地 SHA-256 |
| --- | --- | --- |
| `main.py` | `5d7ca1ca4566552c90d7c52f857d1497e1564da265214a136b92e109cda077dc` | `8a2572db67df08837399d0b3438139de39a220d58bfe0f7214c91ef93e0d5ef5` |
| `_conf_schema.json` | `510a60253b8f4a9d33ee5dd02f188ab8d7a5579d6712b51c4567e70c5a3641b4` | `d1cd1af97197ea4baf6b4632e22f6c450de20145d7f1ebe55d58a7e328b00722` |
| `metadata.yaml` | `966d7a2538674f5416bc3798905ff98dd7be0153348a69d2035a797e785f9f01` | 相同 |

## 本地验证

- Git HEAD：`f525cc3`；分支：`agent/fix-meme-json-protocol-leak`；
- 完整回归：499 项运行成功，496 项通过，3 项为保留到 P8-09 的旧 v1 expected failure；
- `git diff --check`：通过；
- 未执行 Git/GitHub 写操作。

## 下一入口

P8-05：在 FNOS 上以包含 UTC 时间戳的明确路径备份整个线上星汐目录；备份后重新读取文件数、目录大小、`main.py`、schema 和 metadata 哈希，全部与当前线上基线一致才允许进入 P8-06。备份失败或不可读则停止。
