# P8-05 线上星汐完整备份报告

## 结论

P8-05 已完成。已在 AstrBot 数据卷的专用备份目录创建线上星汐 0.4.6 的完整副本，并验证源目录与备份目录的文件数、占用大小、全文件 manifest 及三个关键文件哈希完全一致。备份可读，容器与 WebUI 未受影响。

本层没有修改线上插件目录，没有上传本地 v2 文件，没有重载插件或重启容器。

## 精确备份位置

`/AstrBot/data/backups/shio/astrbot_plugin_shio-P8-05-20260817T134338Z`

备份放在 `/AstrBot/data/backups/shio/`，不放在 `/AstrBot/data/plugins/` 下，避免 AstrBot 后续扫描到重复插件副本。

## 写前保护

- 源路径解析为 `/AstrBot/data/plugins/astrbot_plugin_shio`；
- 数据根解析为 `/AstrBot/data`；
- 目标严格匹配 `/AstrBot/data/backups/shio/astrbot_plugin_shio-P8-05-*`；
- 写前确认目标不存在；
- 只执行 `mkdir -p` 专用备份根和一次 `cp -a`；
- 未执行删除、覆盖或移动。

## 完整性验证

| 检查项 | 源目录 | 备份目录 | 结果 |
| --- | --- | --- | --- |
| 文件数 | 59 | 59 | 一致 |
| `du -sk` | 4864 KiB | 4864 KiB | 一致 |
| 全文件排序 manifest SHA-256 | `d9f22a06458a4376f5ecf0f61ba3e9e3eb8253546edc5c8ad5b915b2327cb11e` | 相同 | 一致 |
| `main.py` | `5d7ca1ca4566552c90d7c52f857d1497e1564da265214a136b92e109cda077dc` | 相同 | 一致 |
| `_conf_schema.json` | `510a60253b8f4a9d33ee5dd02f188ab8d7a5579d6712b51c4567e70c5a3641b4` | 相同 | 一致 |
| `metadata.yaml` | `966d7a2538674f5416bc3798905ff98dd7be0153348a69d2035a797e785f9f01` | 相同 | 一致 |
| 三个关键文件可读 | 是 | 是 | 通过 |

## 写后健康回归

- `astrbot`：running=true、restarting=false、RestartCount=0；
- WebUI：HTTP 200，响应长度 4128；
- 未发生容器重启。

## 回滚边界

该备份是 P8-04 所核对的旧生产 v1 精确副本。若后续 P8-06～P8-08 部署或范围启用失败，只能在停用当前插件加载后，以这个明确路径恢复；不得按时间相邻、模糊目录名或最新文件猜测回滚源。

## 下一入口

P8-06：只更新星汐插件。部署前应先生成本地运行文件清单和哈希，排除 `.git`、tests、docs、reports、缓存与开发资产；上传到远端临时目录后再次核对 manifest，最后才以可回滚方式替换线上星汐目录。该步骤会改变生产代码，当前不得跳过范围确认、临时目录校验或备份引用。
