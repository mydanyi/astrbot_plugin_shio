# 星汐（Shio）

> **星光沉入潮汐，YHN-04B-009 的意识仍在数字海岸醒着。**

星汐是面向 AstrBot 的 typed 群聊心智编排插件。它把当前身份、回复目标、引用、记忆来源、能力权限、人格、情绪、参与决策、工具证据、输出校验和真实发送回执放进同一条可审计链；内置亚托莉示例，但运行机制不绑定单一角色。

0.5.10 修复重启后群聊回顾上下文丢失及空上下文编造：同群已验证的人类公开消息保留有界持久化尾部；明确索要群聊回顾却没有可验证记录时，只会诚实说明无法可靠概括。0.5.9 的群聊并发修复、0.5.8 的称名唤醒设置和 0.5.7 的 22 类 Meme 情境路由保持不变。

## 已闭环能力

- **可信身份和目标**：用平台、机器人账号、会话、群和真实 sender ID 建立 current-turn binding；昵称、自称、引用、记忆和模型文本都不能提升主人权限或替换当前发言人。
- **直接、引用和多模态同链**：文本、引用文本、quoted image、仅图片、原生 caption/unavailable 和一次 repair 共用 typed MediaContext；URL、base64 和文件路径不进入 prompt、trace 或持久状态。
- **自然参与**：明确 @／私聊／称名进入直接回复；未点名机会只能经 Address、Attention、Participation、Cadence 决定为 `MAY_JOIN`、`REACT_ONLY`、`WAIT` 或 `NO_ACTION`，模型不能自行抢话。
- **主动发起**：冷场开题已有 typed scheduler，但 `proactive_initiation_enabled` 默认关闭，群白名单留空时永远不发起；开启后仍受观察期、活跃时段、空闲、冷却、日限额、generation、推理预算和真实发送回执约束。
- **可替换人格与连续情绪**：PersonaPackage 只决定身份资料、关系规则和表达；continuous Affect 与关系状态只投影措辞，不能改变事实、目标或权限。
- **受控资料能力**：普通用户只看到管理员精确允许且分类为只读的检索工具；AnySearch 结果必须有当前轮 sealed evidence，没有真实工具结果就不能声称“已经查过”。
- **Meme 与自然气泡**：Meme Manager 只有 exact runtime／plan／expression permit 才能执行；星汐设置控制补图总开关、普通安全闲聊准入回合数与冷却，Meme Manager 自己的“表情出现概率”继续作为唯一最终概率；线上资源包 22 类均有 code-owned 高置信情境入口，执行器只接受一个密封类别标签，无法可靠匹配时不发图，绝不统一兜底 happy；文字、图片和一至三条完整语义气泡按最终 segments 发送，失败段不会记作成功。
- **输出与并发安全**：协议／隐藏通道／伪工具调用、关系漂移、无依据自传和媒体编造会阻断或至多进行一次无工具 repair；新消息会取消旧 generation，迟到 Provider／工具结果不得发送。
- **学习有审核边界**：只从 exact、高置信、多 reviewer、无敏感内容的聚合反馈产生候选；默认不改变核心人格，必须经过 code-owned review/shadow/有限启用并可即时撤销。
- **主人动作默认关闭**：主人 ID 只是提出动作的资格。四个主人动作适配器保持关闭，production runtime allowlist 为空；文件读取／grep／记忆写入不能靠改一个 UI 开关启用，Shell 永久硬关闭。

## 处理流程

```text
AstrBot event
  → TurnEnvelope / Principal / IngressAdmission
  → Address / Reference / Media / Memory scope
  → Opportunity / Participation / Affect / Relationship
  → CapabilityPolicy / ActionPlanner
  → ReplyComposer 或 sealed evidence acquisition
  → SemanticGuard / OutputValidator / optional one-shot Repair
  → PresentationHandoff / exact segments / SendReceipt
  → privacy-minimal continuity, cadence and reviewed feedback
```

插件启用后没有 v1、shadow、owner-only 或 AstrBot 原回复旁路。typed 准备失败时该轮停止，不会把半成品交给旧 Planner、旧 Replyer 或延迟补答队列。

## 使用要求

- AstrBot `>=4.26.7,<5`（主要生产验收版本 4.27.2）；
- Python 3.12+；
- QQ `aiocqhttp` / NapCat 是当前主要验证平台；
- 一个可用的聊天模型 Provider；需要联网时 Provider 必须支持 AstrBot 工具调用；
- 可选 LivingMemory、AnySearch、Meme Manager；详见 [兼容性说明](docs/COMPATIBILITY.md)。

## 安装与升级

1. 下载 `astrbot_plugin_shio_v0.5.10_upload.zip`。
2. 在 AstrBot WebUI 上传插件；如果装过独立 `astrbot_plugin_agent_guard`，先停用或卸载。
3. 重启 AstrBot，进入星汐配置页，先在专用测试群核对人格、群员隔离、普通聊天、引用／图片和只读搜索。
4. 只有需要识别主人资格时才填写 `owner_ids`；公开默认名单为空。填写名单不会自动启用任何动作。
5. 保持主人动作总开关和四个逐项开关关闭；保持主动发起关闭，除非你已经逐项理解并验证对应策略。

升级前请备份 `/AstrBot/data`。旧 Planner、StyleRetriever、recovery queue、architecture rollout 等遗留字段不会重新激活旧代码；确认 0.5.10 正常后可从旧配置中移除。上传包只能有一个顶层 `astrbot_plugin_shio/` 目录。

## 推荐配置

当前 `_conf_schema.json` 有 51 个实际字段，分为推理预算、重启 continuity、称名、Meme 补图、主动策略、反馈、主人动作、可信机器人、权限、Persona、上下文、气泡和诊断。

| 配置 | 建议值 |
|---|---|
| `enabled` | 开启 |
| `natural_name_wake_enabled` / `natural_name_wake_mode` | 开启；可选“自然语言判断（natural）”或“关键词出现即唤醒（contains）” |
| `permission_guard_enabled` | 开启 |
| `guest_allowed_tools` | 只保留亲自审核过的只读工具；默认 `anysearch_search`、`anysearch_extract` |
| `owner_ids` | 不用主人资格时留空；需要时只填真实平台 ID |
| `owner_action_enabled` 与四个适配器开关 | 全部关闭 |
| `proactive_initiation_enabled` | 关闭；白名单留空 |
| `meme_complement_enabled` | 开启；关闭后星汐文字回复不再申请补图 |
| `meme_complement_cadence_turns` / `meme_complement_cooldown_turns` | 默认 4 / 4；只控制星汐准入节奏，最终出图概率仍在 Meme Manager 设置 |
| `persona_name` | 默认亚托莉；也可选择暖晴、苏澄或中性基准 |
| `prefer_livingmemory_group_history` | 安装并核对 LivingMemory 后开启 |
| 推理预算 | 默认并行 4、等待 128、排队 30 秒、活动 300 秒 |
| `enable_chat_bubbles` / `chat_max_bubbles` | 开启 / 3；宿主分段回复二选一 |
| `debug_log` | 平时关闭 |

配置的完整含义以 WebUI schema 为准；[配置审计](docs/CONFIG_AUDIT.md) 固定了字段闭集与高风险默认值。

## 权限与主人动作

| 主体 | 普通聊天 | 白名单只读检索 | 文件／记忆写入／Shell／服务器 |
|---|---:|---:|---:|
| 已验证主人 | 允许 | 允许 | 默认拒绝；当前生产配置不得启用 |
| 普通群友 | 允许 | 仅在本轮确有资料需求时允许 | 拒绝 |
| sender ID 缺失／来源不可信 | 可失败关闭或最小聊天 | 拒绝 | 拒绝 |

主人动作只由当前可信主人私聊中的 closed operation 语义产生 proposal；没有 `/agent`、`/task`、`/chat` 或 `/role` 快捷旁路。proposal 不保留原始完整工具集，也不绕过适配器、运行时一致性、确认、持久化 lifecycle 和真实投递门。

星汐不是安全沙箱。Docker／宿主最小权限、只读挂载、网络与密钥隔离、第三方插件权限仍由管理员负责。

## 群聊参与和主动发起

- 直接 @、私聊、引用和明确称名优先进入直接回复；
- 未点名开放群聊只有在对象／话题相关、不是他人私聊、不是工具或主人动作、cadence 允许时才可接话；
- `REACT_ONLY` 只发布轻量 expression intent，不偷偷升级成文字回复；
- 主动发起只能从当前公共 scene 与 Persona 公共兴趣选题，不读取最后发言者的个人事实或 LivingMemory 私密资料；
- 新消息、直接唤醒、外部 stop 或 generation 漂移都会取消尚未发送的旧输出。

## 人格、关系和学习

公开包包含四份同 schema Persona：

- `assets/personas/atri.json`：亚托莉（默认）；
- `assets/personas/warm_companion.json`：暖晴；
- `assets/personas/su_cheng.json`：苏澄；
- `assets/personas/neutral_minimal.json`：中性基准。

人格包无权声明当前用户是主人、开放工具或伪造事实。关系状态只影响表达亲疏；学习只保存聚合证据、候选与 review 状态，不保存完整群聊作为“新人格”。

## LivingMemory、ReNeBan 与 X-01

星汐只消费带来源、作用域和主体的 LivingMemory 资料；其他用户个人事实、群聊中的 owner-private 事实、被 ban／bot／plugin 来源污染的资料都不能进入当前主体。ReNeBan、Parser、AnySearch、Meme Manager 的缺失、异常或接口漂移均有显式降级／失败关闭测试。

外部限制 `X-01` 仍存在：当前 LivingMemory 的被动捕获时序可能早于 ReNeBan 与星汐准入。因此星汐能保证被 ban 消息在自身 ledger、Scene、Affect、Learning、Tool 和 Reply 中零消费／零提交，但不能声称 LivingMemory 零存储。P10 后的可选 O1 尚未执行；未经用户确认不会更新或修改第三方插件，也不会创建上游 PR。

## 多模态与 repair

- direct、quoted、Reply-ID fallback、multiple、text+image、仅图片、native caption 和 unavailable 都绑定 exact source message/sender；
- 仅图片正文为空时，只有原生 chain 的 image/audio evidence 才会生成代码固定的 current-message 描述，普通空消息仍拒绝；
- Provider 的原生 URL／base64 只留在 opaque transport，不进入公开 typed context；
- repair 必须复用同一 MediaContext、Persona、ContentIntent、target 和原生 transport，且最多一次、无工具。

## 持久状态与隐私

星汐不会持久化失败草稿或 `pending_replies.json`。插件数据目录可能包含：

- `social_state.json`：作用域化互动统计；
- behavior／learning candidate、activation 与撤销状态；
- `continuity/runtime_continuity.json`：不可逆 scope/subject 指纹、revision 和 cadence；
- `proactive/proactive_state.json`：主动策略的不可逆群指纹、配额和冷却；
- `public_group_ledger.json`：仅含已准入、同群且发送者已验证的人类公开入站消息；每群最多 32 条、最多 128 个 scope，用于重启后的显式群聊回顾；
- owner lifecycle journal／install secret：即使动作关闭也按最小本地权限管理，正文、路径和参数不进入 trace。

除上述有界公开群聊尾部外，其他状态不保存完整群聊正文。所有插件数据仍属于敏感、可关联运行数据，备份或提交 Issue 前必须脱敏。详情见 [隐私与安全](docs/PRIVACY_AND_SECURITY.md) 和 [安全策略](SECURITY.md)。

## 已知限制

- AstrBot 5 与其他平台适配器尚未纳入兼容承诺；
- Provider 是否真正调用检索工具仍取决于其 AstrBot tool-call 支持；
- 工具白名单不能替代宿主和第三方服务的权限隔离；
- `X-01` 仍是外部插件生命周期限制；O1 尚未执行；
- 四个主人动作适配器保持关闭，当前发布不承诺真实文件读取、grep 或记忆写入。

## 开发与测试

从插件父目录执行：

```powershell
python -X utf8 -m unittest discover -s astrbot_plugin_shio/tests -p "test_*.py"
python -X utf8 -m compileall -q astrbot_plugin_shio
```

当前 Windows 与隔离 AstrBot Linux 自动化均超过 1,200 项；发布前还必须经过 P10 固定矩阵、隐私扫描、container 门和 FNOS 备份／哈希／加载／WebUI／自然流量验收。公开复现步骤见 [测试指南](docs/TESTING.md)。

## 反馈

请提供 AstrBot／星汐／相关插件版本、平台和 Provider 类型、可复现步骤及完整脱敏日志区间。不要提交 API Key、Cookie、Token、真实私聊、未脱敏账号／群号、内网路径或他人记忆。

## 致谢与声明

“规划与表达分离”的思路受到 [MaiBot](https://github.com/MaiM-with-u/MaiBot) 等项目启发；星汐代码针对 AstrBot 插件接口独立实现。亚托莉示例仅用于非商业角色化测试，相关角色与作品权利属于其权利人。插件代码使用 [MIT License](LICENSE)。
