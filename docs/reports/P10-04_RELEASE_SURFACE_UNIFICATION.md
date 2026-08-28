# P10-04 README/schema/metadata/版本统一

## 结论

P10-04 已完成。公开版本统一为 `0.5.0`，README、metadata、CHANGELOG、配置审计、架构、兼容性、隐私和测试指南现在描述同一条 P4～P10-03 typed 运行链。高风险 owner/proactive 默认值、`X-01`、O1 未执行、四个 Persona、direct/quoted/image-only media、未点名参与、主动发起、推理预算和重启 continuity 都有明确口径。

本阶段没有部署。线上仍为 `0.4.6` 与 P3 稳定 `main.py`；P10-04 只在 Windows 和无网络、候选目录只读挂载的一次性 AstrBot Linux container 验证。

## 首轮红灯

新建 `tests/test_p10_release_surface.py` 后首轮 `4/4` 失败：

1. metadata、README 安装包与 CHANGELOG 仍是 `0.4.6`，没有 `0.5.0` 单一版本；
2. README 仍宣称 `363` 项自动化、`23` 个配置字段，并写未点名参与／主动开题尚未实现；
3. `permission_audit_log` hint 错称会记录部分工具名，生产实际只记录数量与脱敏 digest；
4. README 没有明确 `X-01`、O1 未执行和当前四个主人动作适配器 all-off 的发布边界。

完成第一轮文档统一后，逐字段静态门又发现 `owner_chat_prefixes` 在生产代码中完全没有读取点；旧 README 对 `/agent`、`/task`、`/chat`、`/role` 的快捷语义也没有运行实现。这是 P10-04 的第二个真实缺口，不以伪造读取保住原数字。

## 根修

### 版本和 metadata

- `metadata.yaml`、README 安装包名与 CHANGELOG 统一为 `0.5.0`；
- metadata help 不再要求普通聊天用户必须填写主人 ID；
- description 明确 typed identity、自然参与、多模态、受控检索和默认关闭的主人动作。

### 配置闭集

- 删除孤儿字段 `owner_chat_prefixes`，配置闭集从 49 收敛为 **48**；
- `owner_action_sandbox_shell_once_enabled` 仍保留为 UI 审计项，生产明确读取后丢弃，永不进入 AdapterConfig；
- owner master + 四个 adapter 以及 proactive master 继续默认 false，主动群 allowlist 默认为空；
- permission audit hint 改为数量＋脱敏作用域，不宣称记录名称或参数；
- release-surface test 逐项检查 48 个字段都有 description/type/default、生产字符串读取点和配置审计条目。

### 公开说明

README 与五份 public docs 统一说明：

- 未点名参与和 React 已由 P5 typed 重建；
- 冷场主动发起已由 P7 typed 重建，但默认关闭且空 allowlist 永不触发；
- 四个 Persona、continuous Affect、Relationship projection 与 reviewed learning 不改变身份／权限／事实；
- AnySearch、Meme Manager、LivingMemory、ReNeBan、Parser 的 exact/degraded 边界；
- direct/quoted/仅图片与 same-media repair；
- owner proposal 没有 slash 快捷授权；四个 adapter 保持关闭，Shell 永久硬关闭；
- `X-01` 只承诺 Shio 零消费／零提交，不冒充 LivingMemory 零存储；
- 可选 O1 尚未执行，未经用户确认不修改第三方或创建 PR；
- 48 个配置字段、1,200+ 自动化与线上部署必须另走备份／哈希／加载／WebUI／自然流量门。

## Release-surface gate

`tests/test_p10_release_surface.py` 固定：

- 单一 `0.5.0` 版本；
- 48-field schema 与高风险 all-off；
- 每个 schema 字段的 public shape、生产读取和审计条目；
- public docs 不再出现 363/158/23 等旧口径；
- owner、X-01、O1、多模态、proactive 边界；
- README 和五份 public docs 的本地 Markdown 链接全部可解析。

## 验证

- Release surface：`6/6`；
- config/core/proactive/pipeline/P10 focused：`169/169`；
- Windows full：`1250/1250`，skipped 7；
- 隔离 AstrBot Python 3.12 Linux release surface：`6/6`；
- 隔离 AstrBot Python 3.12 Linux full：`1250/1250`；
- `compileall`、schema/P10 JSON parse、merge-marker、`git diff --check`：通过；
- 验证归档：`C:\Users\45928\AppData\Local\Temp\shio-p1004-20260819-v1.tar`，SHA256 `43907AABB50AEA819EE8A0E2F464339864CCB6A41ACB541A64EEAEBC92D36684`。

## 关键哈希

- `metadata.yaml`: `EC0A6F27400738444D397005197327648FBDABECC916B61D2716B42C803142D0`
- `_conf_schema.json`: `D5551F575C81E9FD626001D8760221E492048F1CD522FB78C76DAC7072AB39AF`
- `README.md`: `BD7218ED819EDD8AAD543BCF24ACD35E63C81A3C188774C6C9A18F560785915F`
- `CHANGELOG.md`: `2E4D3286D3A747AB9C80BBBAA8D4E0A37AE97ED5EFC650FABC1632C44A55DB85`
- `docs/CONFIG_AUDIT.md`: `E75A7ACD79F2DC6764B90DAEB3EE62B311DAD2E7F569C681A3A4C5746FAB3CCA`
- `docs/ARCHITECTURE.md`: `3EEC01D0DC733A09B3180B8A2C414164E5EBD9BA63B3364BE77041D25108328C`
- `docs/COMPATIBILITY.md`: `508382A4D6BBDED616C09EF890E8D44C3E452E90806C61E326F291CF61E01502`
- `docs/PRIVACY_AND_SECURITY.md`: `8976E69853272D6D68AFFFBCA4E9A6AFC211B0C4752014C106554B3BAEE7F94C`
- `docs/TESTING.md`: `C419B36A88F52A5B15E056D9303F1A97EF601EED634E7B69ED275804E153C98B`
- `tests/test_p10_release_surface.py`: `FF5FBF232C91CADCC2AEF2CE1A8D97FCD8475D2143BC746935505BA8AF747BAC`
- `main.py`: `E7A30D51CD7C8B22CB90EADDC55BF3CA6B3A3671FD18C3912C68020006B4A875`

## 线上只读核对

- production container：`running=true`、`restart=0`；
- WebUI HTTP：`200`；
- production `metadata.yaml`：仍为 `0.4.6`；
- production `main.py`：`583BF681D28BBA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`；
- 临时 FNOS staging 已删除，没有 reload、restart 或覆盖。

## 边界与下一入口

P10-04 只完成 release surface，不代表部署。唯一下一入口是 **P10-05 本地／容器／线上只读完整门**：从冻结工作树生成最终候选包与 manifest，复跑所有 P10 runners、full suites、静态／隐私／包结构／可复现哈希，并做部署前 FNOS 只读 preflight；任何失败都不得进入 P10-06。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P10-04。
