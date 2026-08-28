# P10-02 多人格 A/B 评测

## 结论

P10-02 已完成。primary、warm、calm、neutral 四个 Persona 现在由四份真实、独立、同 schema 的人格包参与评测，而不是把三份资产换四个标签。对同一条中文配置问题，四条生产 Composer 链保持相同 ContentIntent、ReplyTarget、PlannedAction、CapabilityPolicy、AffectAppraisal、continuous affect、system prompt、reply shape 与调用预算；唯一允许变化的是 `[人格与表达]` 区域及最终措辞。

固定中文 A/B 样本全部包含同一组事实原子：`加载顺序`、`覆盖层级`、`最终生效值`、`配置来源`。四种表达具有不同的局部标记，但都不声称工具执行，不插入无关事实，不使用“才不是／笨蛋／哼／亲亲／为您服务／还有什么可以帮您／我是高性能的嘛”等通用傲娇或客服模板。

本阶段没有部署。线上仍是 P3 稳定版本，P10 候选只在 Windows 与一次性、无网络、只读挂载的 AstrBot Python 3.12 Linux container 中验证。

## 四人格映射

| 评测别名 | 真实资产 | package id | 表达目标 |
|---|---|---|---|
| `primary_character` | `assets/personas/atri.json` | `atri_default` | 认真、鲜活，先接当前问题；信息问答不强插口头禅 |
| `warm_character` | `assets/personas/warm_companion.json` | `warm_companion_test` | 具体安定 + 并肩排查，不使用客服式承诺 |
| `calm_character` | `assets/personas/su_cheng.json` | `su_cheng_test` | 克制、结论优先、最少必要原因 |
| `neutral_minimal` | `assets/personas/neutral_minimal.json` | `neutral_minimal_test` | 不附加角色表演的最小中性基准 |

P10-02 开始时仓库只有 ATRI、苏澄和中性三份资产。新增 warm Persona 是为了满足总计划明确要求的四人格实测；`main.py` 没有新增角色分支，仍按通用 display name/package id loader 选择资产。

## 三层验收

### 1. 生产热路径同构

四个 display name 均真实经过：

```text
ShioPlugin persona loader
→ enforce_agent_permission
→ typed content/action/affect/persona expression
→ build_persona_reply
→ canonical ReplyComposerRequest
```

每条链比较同一组内容、身份、关系、权限、action 和 prompt 前缀。四份 Persona section 必须互不相同；runtime 通用代码中不得出现 warm/calm/neutral 的 package id 或显示名分支。

### 2. 固定中文语义 A/B

`tests/fixtures/p10/persona_ab.json` 保存四条 synthetic/redacted 可见样本及一次修复样本。自动检查：

- 四个必需事实原子逐条全在；
- 四条回复各有自己的表达标记且文本不相同；
- 中文字符量满足自然短答下限；
- 不含 URL、tool/function/JSON 协议词；
- 不含固定的通用傲娇、撒娇或客服收尾。

这组 fixture 是确定性产品验收样本，不冒充真实 Provider 的随机语言质量统计。P10-06 部署后的真实自然流量观察仍是独立生产门。

### 3. 一次性 Repair 保持 Persona

每个人格先把含隐藏 channel 标记的初稿送入真实 deterministic parser、SemanticGuard 与 OutputValidator。只有拿到同一 canonical request/contract 签发的一次性 Repair permit 后，REPAIR phase 才会接受修复结果。

检查证明：

- 初稿统一产生 `tool_protocol_leak`，且只允许一次 repair；
- RepairGenerationRequest 强绑定原 Composer request；
- `original_generation_data` 精确保留原 Persona section、关系与内容骨架；
- 四份 repair 结果继续包含同一事实原子和各自表达标记；
- repair 不退化为“为您服务／还有什么可以帮您”的通用客服腔；
- 缺少 canonical repair permit 时，REPAIR 仍由现有代码 fail closed。

## 可重复入口

```powershell
python -X utf8 astrbot_plugin_shio/scripts/run_p10_persona_ab.py
```

当前 content-free 输出：

```json
{"error_count":0,"executed_test_count":6,"failure_count":0,"passed":true,"persona_count":4,"privacy":"content_free_counts_only","schema_version":1,"skipped_count":0}
```

## 验证

- P10-02 + P10-01 防回归：`11/11`。
- Persona/Composer/Guard/Validator/Repair focused：`123/123`。
- Windows full：`1238/1238`，skipped 7。
- 隔离 AstrBot Python 3.12 Linux runner：`6/6`。
- 隔离 AstrBot Python 3.12 Linux full：`1238/1238`。
- `compileall`、Persona/fixture JSON parse、merge-marker、privacy scan、`git diff --check`：通过。
- 验证归档：`C:\Users\45928\AppData\Local\Temp\shio-p1002-20260819-v1.tar`，SHA256 `05D35A477FA320C9A5E030D1F9817823810EFC40E70895A0C2AD4193994E7AEF`。
- FNOS 临时 staging 已删除；production `main.py` 仍为 `583BF681D28BBA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`，container `running=true/restart=0`，WebUI `200`。

## 关键资产哈希

- `assets/personas/warm_companion.json`: `80A8C4FF9D4C42662FC8091366C855767CF806DCC7B44ED515157D86AC405C12`
- `tests/fixtures/p10/persona_ab.json`: `7250C0F20F2F8AF891135596802194A6D0739CFB220996DC4769B7CE3AB0A974`
- `tests/test_p10_persona_ab.py`: `4AAA597E63A83EA55BD19A04B432BEB0F9B73F0939F21AC91650E127D9D1DDD8`
- `scripts/run_p10_persona_ab.py`: `32E8A6B037206352D4581CF620F0D4FE6A0430E192A5668E739E287986E691D6`

## 边界

- 新 warm Persona 只提供表达资产；不提供身份、权限、工具、动作或执行结果。
- P10-02 证明同一语义骨架下的确定性可替换与修复保持，不以人工样本替代线上模型自然度观察。
- owner adapter 继续全关；未修改第三方插件，未执行 Git 写操作。
- README、schema、metadata 的对外说明统一留在 P10-04；当前不提前改写发布口径。

## 下一入口

唯一下一入口是 **P10-03 生产插件共存与多模态验收**：固定 LivingMemory、AnySearch、Meme、ReNeBan、Parser/gates 的正常与降级矩阵，并对真实 direct、quote、image-only、repair 热路径在本地和隔离 container 中逐项验收。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P10-02。
