# P10-05 最终候选包与部署前只读验收

## 结论

P10-05 已完成。0.5.0 最终候选已被构造成逐字节可复现的最小运行 ZIP，并由外置 content-only manifest 绑定。Windows 与 AstrBot Python 3.12 Linux 容器使用同一冻结源码完成全部 P10 runners 和完整测试；实际 ZIP 又在容器内独立完成路径、元数据、逐文件摘要、只读编译和 `astrbot_plugin_shio.main` 导入验证。

本阶段没有覆盖线上插件、没有修改线上配置、没有重启容器。FNOS 仍运行 0.4.6 / P3 稳定版本。

## 真实红灯与根修

### 1. 候选构建器缺失

新增 `tests/test_p10_candidate_package.py` 后，第一轮因 `scripts.build_p10_candidate` 不存在而 `ModuleNotFoundError`。这证明此前各阶段的临时 TAR 只能验证源码，尚没有可交付、可复现、可独立验真的上传包。

### 2. ZIP 条目没有全局规范排序

构建器首版把四个根文件放在前面，测试真实失败于 ZIP 条目顺序与全局排序不一致。实现随后固定为：源文件集合闭集校验后，按 package-relative POSIX 路径全局排序；时间戳固定为 ZIP epoch；权限固定为 regular file `0644`；压缩参数固定。两次不同目录构建现在逐字节一致。

### 3. 测试源码与实际上传 ZIP 之间缺少独立验证

新增 `verify_p10_candidate.py` 和严格 verifier。它不读取工作树，只接受 ZIP 与 manifest，并拒绝：

- 重复 JSON key、非有限值、错误字段或类型；
- 包名／版本／总摘要／大小不一致；
- 重复、乱序、反斜线、绝对或 `..` 路径；
- 非单一 `astrbot_plugin_shio/` 顶层、目录条目、symlink、错误权限或时间戳；
- 候选闭集外文件；
- 任一成员字节数或 SHA-256 与 manifest 不一致。

正式回归同时篡改 manifest 摘要和 ZIP 尾部字节，两种情况都失败关闭。

## 最小运行包

候选只包含：

- 根运行文件：`__init__.py`、`_conf_schema.json`、`main.py`、`metadata.yaml`；
- 所有 `core/**/*.py`；
- 四个 `assets/personas/*.json`。

开发目录、测试、脚本、计划、报告、README、Git、缓存和编译产物全部不进入上传 ZIP。

| 项目 | 值 |
|---|---:|
| release version | `0.5.0` |
| 文件数 | 95 |
| Python 文件数 | 89 |
| 解压字节数 | 2,425,934 |
| ZIP 字节数 | 502,129 |
| ZIP SHA-256 | `8E804DC028606F35BAB2282BA976A201971FC2063EB499C3263261658DDF943E` |
| manifest SHA-256 | `B930867A9906D6DB983C18DDBEC15ACAC724A45F211A312AE39BA0E80425C7A7` |

本地候选目录：

`C:\Users\45928\AppData\Local\Temp\shio-p1005-20260819-111351`

其中上传文件名固定为 `astrbot_plugin_shio_v0.5.0_upload.zip`，外置 manifest 为同 basename 的 `.manifest.json`。

## 综合验证

### P10 runners

Windows 与 AstrBot Linux 容器均得到相同结果：

- fixed capability/outcome matrix：24/24，11 Capability、9 ProductOutcome、19 evidence；
- four-Persona A/B：6/6；
- plugin/gate/multimodal matrix：29/29，15 plugin、4 gate、5 flow、24 evidence。

### 完整测试

- Windows：`1255/1255`，skipped 7；
- AstrBot Python 3.12 Linux：`1255/1255`，无 skip；
- candidate package focused：`5/5`；
- 容器实际 ZIP verifier：通过；
- 容器从 ZIP 临时解压后只读 `compile()`：89/89；
- 容器从 ZIP 临时解压后 import `astrbot_plugin_shio.main`：通过；
- 本地 `compileall`、17 个 JSON 文件解析、merge-marker、`git diff --check`：通过。

## FNOS 只读 preflight

### 当前运行态

- host 插件目录精确解析为 `/vol3/1000/Docker/Astrbot/data/plugins/astrbot_plugin_shio`；
- container 插件目录精确解析为 `/AstrBot/data/plugins/astrbot_plugin_shio`；
- 容器 running=true、restarting=false、OOMKilled=false、RestartCount=0；
- StartedAt=`2026-08-18T18:22:37.928046419Z`；
- WebUI HTTP 200；
- 可用空间约 882,583,044 KiB；备份目录存在；
- 线上仍为 metadata `0.4.6`，`main.py` SHA-256 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`。

### 文件替换边界

线上有 150 个文件，候选有 95 个：

- 共有 76 个；44 个字节相同，32 个将更新；
- 候选新增 19 个；
- 线上有 74 个候选外文件，包括 66 个旧 `core` 文件、缓存、旧公开文档和 `main.py.pre-log-privacy-fix`。

因此 P10-06 禁止 `cp -a stage/. live/` 式覆盖。必须先完整备份，再把已验签的 95 文件目录作为整体精确替换；否则会保留旧运行分支。

### 配置迁移边界

线上配置是有效 JSON，共 32 项。与候选 48-field schema 比较：

- 17 个新字段缺失，均须从候选 schema 的 typed default 补齐；
- 唯一 orphan 是已在 P10-04 删除的 `owner_chat_prefixes`，部署时须移除；
- 当前五个 owner action 总／分开关均为 false；
- proactive 总开关未存在，候选默认必须补为 false；群白名单为空；
- 现有 owner ID 与其他仍受支持的值只能原样保留，不输出或重写其内容。

### 日志基线

从当前 StartedAt 到 preflight：全 AstrBot 日志有 4 个 Traceback header、0 条通用 ERROR pattern、23 条星汐 marker；旧线上 `delivery_finalize_failed` 出现 1 次，`denial_prepare_failed` 为 0，`typed_reply.prepared` 为 3。这里只登记 content-free 计数，不能把全容器 Traceback 归因给星汐。

P10-06 必须以新 StartedAt 为零点重新统计；候选部署前的旧计数不能算候选失败，也不能被宣称已修复。

## 代码与测试摘要

- `scripts/build_p10_candidate.py`: `92C3DCD0E6A06E321D34471433BD5110958EE7653DE0249A688A7AAF7841698F`
- `scripts/verify_p10_candidate.py`: `A359FEA83C0BC9555BE1CDB2B9DFB787A3D46C2CA005CEAA3A24225D8EA6EED3`
- `tests/test_p10_candidate_package.py`: `C16C2FC3C117CB57CEE6245532E11EBA208F0B412FE600EC9DDAEFF250300D86`
- container test-source TAR: `87C2860958C7DD12074597DF03069921B789EAA1D7320F5BC99833A408F22D68`

## 中断恢复与唯一下一入口

P10-05 已把唯一可部署输入固定为上述 ZIP + manifest；后续不得从移动工作树重新临时打包后直接上线。

唯一下一入口是 **P10-06 FNOS 备份、精确部署、配置迁移、哈希／加载／WebUI／新日志／回滚验收**：

1. 写前重验候选与 manifest 摘要；
2. 原子备份线上 150 文件目录和原始配置，并逐项校验；
3. 在 host 和 container staging 重新运行候选 verifier；
4. 生成仅含 48 schema 字段的配置候选，保留既有有效值、补 17 个默认、删 1 个 orphan，并保持 owner/proactive all-off；
5. 用候选 95 文件目录整体替换星汐，绝不覆盖残留旧分支；
6. 重启后验证 StartedAt、RestartCount、95/95 manifest、0.5.0、48-field config、插件加载、WebUI 与新时间窗日志；
7. 任一关键门失败立即恢复本阶段写前备份并重新验证。

P10-06 完成前不请用户测试；中断后从本报告与 `SHIO_MASTER_PLAN.md` 第 12 节继续。
