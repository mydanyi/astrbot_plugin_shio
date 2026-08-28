# P8-06 最小星汐运行包部署报告

## 结论

P8-06 已完成。已将本地重构后的最小运行包部署到 `/AstrBot/data/plugins/astrbot_plugin_shio`，只覆盖星汐运行文件；没有修改其他插件、AstrBot core、Docker Compose、Provider 或 FNOS 配置。

本层结束时磁盘文件已经更新，但 AstrBot Python 进程尚未重载，因此不能把 P8-06 单独视为运行态发布完成。P8-07 必须立即重载并验证实际加载状态。

## 部署包范围

| 内容 | 数量 |
| --- | ---: |
| 根运行文件 | 4 |
| `core/*.py` | 40 |
| 人格/表达 JSON | 3 |
| 总文件数 | 47 |
| 总字节数 | 645223 |
| 禁止目录命中 | 0 |

根运行文件只有：

- `__init__.py`
- `_conf_schema.json`
- `main.py`
- `metadata.yaml`

明确排除了 `.git`、`.github`、tests、docs、reports、scripts、`__pycache__`、README/CHANGELOG 和其他开发文件。

## 本地门禁

- 完整回归：499 项运行成功，496 项通过，3 项为保留到 P8-09 的旧 v1 expected failure；
- `git diff --check`：通过；
- 本地临时包：`C:\Users\45928\AppData\Local\Temp\shio-p8-06-6d6136b5617648409774fe3d25a78650\astrbot_plugin_shio`；
- 本地计算 manifest 摘要：`e9f3a5d9cba5955046a5fdeadc4b10aae6a2c8ae6fb763506ff6ed417e241e79`。

说明：FNOS 的 `sort` 排序口径导致聚合 manifest 与 Windows 口径不同，所以没有错误地用聚合值判失败；随后逐路径比较全部 47 个 SHA-256，Windows → FNOS host 与 Windows → 容器两个阶段均为 47/47 精确一致，无缺失、无额外、无哈希差异。

## 双段临时上传

1. Windows 临时包上传至 FNOS：`/tmp/shio-p8-06-20260817T134851Z/astrbot_plugin_shio`；
2. FNOS 临时包复制到容器：`/tmp/shio-p8-06-20260817T134851Z`；
3. 容器内 42 个 Python 文件全部通过只读 `compile()` 语法验证；
4. 临时包确认 47 个文件且不含禁止目录后才覆盖线上星汐。

## 替换保护

- 实际源、线上目标和 P8-05 备份都先用 `readlink -f` 校验为精确路径；
- 备份 `main.py` 必须仍匹配旧生产 SHA-256 才允许写入；
- 使用 `cp -a stage/. live/` 定向覆盖星汐，不删除整个 plugins 根；
- P8-04 已确认线上没有本地已删除的运行文件，因此覆盖后不会残留旧 runtime 模块；
- 写入后按运行文件白名单重新枚举，线上 47/47 与本地逐文件哈希一致。

## 部署后磁盘证据

| 文件 | 线上 SHA-256 | 本地 SHA-256 |
| --- | --- | --- |
| `main.py` | `8a2572db67df08837399d0b3438139de39a220d58bfe0f7214c91ef93e0d5ef5` | 相同 |
| `_conf_schema.json` | `d1cd1af97197ea4baf6b4632e22f6c450de20145d7f1ebe55d58a7e328b00722` | 相同 |
| `metadata.yaml` | `966d7a2538674f5416bc3798905ff98dd7be0153348a69d2035a797e785f9f01` | 相同 |

P8-05 回滚备份仍为 59 文件，manifest 仍是 `d9f22a06458a4376f5ecf0f61ba3e9e3eb8253546edc5c8ad5b915b2327cb11e`，旧 `main.py` 哈希未改变。

## 写后即时健康

- 容器仍 running=true、restarting=false、RestartCount=0；
- StartedAt 未改变，证明本层没有偷偷重启；
- WebUI 仍为 HTTP 200。

## 下一入口

P8-07：重启 `astrbot` 使新文件真正加载；验证 StartedAt/RestartCount、插件加载日志、47 个运行文件哈希、WebUI HTTP 200、星汐关键初始化和新日志无 Traceback。任一关键验证失败，立即使用 P8-05 备份回滚。
